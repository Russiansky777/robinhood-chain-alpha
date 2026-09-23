#!/usr/bin/env python3
"""СПАСЕНИЕ ЗАВИСШЕЙ ПОЗИЦИИ -- этапы C и D согласованного плана.

Зачем. DBot не агрегатор: он торгует прямую пару mint->SOL. Если
ликвидность минта живёт только в паре с другим токеном, продать позицию
он не может в принципе -- измерено: 4 из 11 зависших сделок. Обходной
путь -- перевести токен с кошелька задачи на СВОЙ кошелёк-утилизатор
(его приватный ключ у нас, а не у DBot) и продать через Jupiter, который
маршрутизирует через промежуточные токены.

ВАЖНОЕ УТОЧНЕНИЕ ВЛАДЕЛЬЦА: оценку "спасение закрывает 4 из 11" НЕ
считать ограничением. Jupiter продаёт и токены с прямыми пулами, и
токены с комиссией за перевод. Реальная область применимости
определяется на первом живом случае, а не этой оценкой.

ГРАНИЦЫ (все проверяются в decide(), а decide() -- в self_test()):
  * RESCUE_LIVE по умолчанию 0. Без явной единицы не отправляется НИЧЕГО:
    считается и печатается план, деньги не двигаются.
  * ПОТОЛОК СУММЫ НА ОДНО СПАСЕНИЕ -- 3 SOL котировки (распоряжение
    владельца). Выше -- стоп и алерт, а не "ну ладно, разок".
  * Порог смысла -- котировка должна давать не меньше 30% от sol_in.
    sol_in неизвестен -- СТОП: нулём его подменять нельзя, иначе порог
    выполнится всегда.
  * Получатель SOL -- ТОЛЬКО кошелёк задачи из /automation/follow_orders,
    взятый живьём. Адреса из истории транзакций не используются никогда:
    в истории бывают поддельные адреса-двойники.
  * Приватный ключ утилизатора -- только из окружения RESCUE_WALLET_KEY,
    никогда в лог, Telegram или репозиторий (scrub + _ACTIVE_SECRETS).

ПОСЛЕДОВАТЕЛЬНОСТЬ (D):
  1. котировка Jupiter на весь остаток;
  2. решение decide() -- порог, потолок, известность входа;
  3. distribute токена с кошелька задачи на утилизатор (5 кредитов);
  4. ожидание прихода до 60с -- по ЦЕПИ, не по ответу API;
  5. свежая котировка Ultra на ФАКТИЧЕСКИ ПРИШЕДШЕЕ количество
     (комиссия за перевод съедает 1-3%, считать по отправленному нельзя),
     подпись, execute;
  6. одна повторная попытка;
  7. возврат SOL на кошелёк задачи минус резерв на комиссии;
  8. закрытие токен-аккаунта (возврат ренты);
  9. Telegram по каждому шагу.

ЧЕГО ЗДЕСЬ НЕТ. Ни одной покупки. Ни одного обращения к зонду
(dbot-detect-probe). Лестниц проскальзывания нет -- slippageBps один и
задаётся конфигом.
"""
from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable

import requests

WSOL_MINT = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
JUP_ULTRA = "https://lite-api.jup.ag/ultra/v1"
JUP_QUOTE_HOSTS = ("https://lite-api.jup.ag/swap/v1/quote",
                   "https://quote-api.jup.ag/v6/quote")

CLOSE_ACCOUNT_IX = 9  # индекс инструкции CloseAccount в SPL Token и Token-2022


# ---------- конфиг ----------

@dataclass
class RescueConfig:
    live: bool = False
    min_share_of_sol_in: float = 0.30
    max_quote_sol: float = 3.0
    return_reserve_sol: float = 0.002
    slippage_bps: int = 3000
    arrive_timeout_s: int = 60
    retries: int = 1

    @classmethod
    def from_env(cls) -> "RescueConfig":
        def f(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, default))
            except (TypeError, ValueError):
                return default
        return cls(
            live=os.environ.get("RESCUE_LIVE", "0").strip() == "1",
            min_share_of_sol_in=f("RESCUE_MIN_SHARE", 0.30),
            max_quote_sol=f("RESCUE_MAX_QUOTE_SOL", 3.0),
            return_reserve_sol=f("RESCUE_RETURN_RESERVE_SOL", 0.002),
            slippage_bps=int(f("RESCUE_SLIPPAGE_BPS", 3000)),
            arrive_timeout_s=int(f("RESCUE_ARRIVE_TIMEOUT_S", 60)),
            retries=int(f("RESCUE_RETRIES", 1)),
        )


# ---------- решение (чистая функция, без сети) ----------

def decide(quote_sol: float | None, sol_in: float | None, cfg: RescueConfig) -> tuple[str, str]:
    """Возвращает ("идём"|"стоп", причина). Ни одного обращения к сети --
    именно поэтому все границы можно доказать в self_test()."""
    if quote_sol is None:
        return "стоп", "котировки нет -- продавать вслепую не буду"
    if quote_sol <= 0:
        return "стоп", f"котировка {quote_sol} SOL -- продавать нечего"
    if quote_sol > cfg.max_quote_sol:
        return "стоп", (f"котировка {quote_sol} SOL выше потолка на одно спасение "
                        f"{cfg.max_quote_sol} SOL -- останавливаюсь и зову владельца")
    if sol_in is None:
        return "стоп", ("вход (sol_in) неизвестен -- порог считать не от чего; "
                        "подставлять ноль нельзя, иначе порог выполнится всегда")
    need = cfg.min_share_of_sol_in * sol_in
    if quote_sol < need:
        return "стоп", (f"котировка {quote_sol} SOL меньше порога "
                        f"{cfg.min_share_of_sol_in:.0%} от входа {sol_in} SOL (нужно >= {need:.9f})")
    return "идём", (f"котировка {quote_sol} SOL >= порога {need:.9f} "
                    f"({cfg.min_share_of_sol_in:.0%} от входа {sol_in}) и <= потолка {cfg.max_quote_sol}")


def assert_whitelisted(address: str, whitelist: set[str]) -> None:
    """Единственная дверь, через которую SOL может уйти с утилизатора.
    Список строится живьём из follow_orders; адрес из истории транзакций
    сюда не попадает никогда."""
    if not whitelist:
        raise RuntimeError("белый список пуст -- отправлять некуда, останавливаюсь")
    if address not in whitelist:
        raise RuntimeError(f"адрес {address} НЕ в белом списке кошельков задач -- отправку отклоняю")


# ---------- Jupiter ----------

def quote_sol_for(mint: str, amount_raw: int, slippage_bps: int,
                   scrub: Callable[[str], str] = lambda s: s) -> dict:
    """Котировка mint -> SOL. Только чтение, подпись не нужна.
    Бесплатный тариф Jupiter отклоняет restrictIntermediateTokens=false
    (проверено: NOT_SUPPORTED), поэтому параметр не передаётся."""
    if amount_raw <= 0:
        return {"ошибка": "нулевое количество -- котировать нечего"}
    errors = {}
    for url in JUP_QUOTE_HOSTS:
        params = {"inputMint": mint, "outputMint": WSOL_MINT,
                  "amount": str(amount_raw), "slippageBps": str(slippage_bps)}
        try:
            r = requests.get(url, params=params, timeout=25)
        except Exception as exc:  # noqa: BLE001
            errors[url] = f"сеть: {type(exc).__name__}"
            continue
        if r.status_code != 200:
            errors[url] = f"http={r.status_code}: {scrub(r.text[:200])}"
            continue
        try:
            b = r.json()
        except ValueError:
            errors[url] = "не JSON"
            continue
        if b.get("error") or b.get("errorCode"):
            errors[url] = str(b.get("error") or b.get("errorCode"))
            continue
        out = b.get("outAmount")
        return {"SOL": int(out) / 1e9 if out else None, "outAmount": out,
                "priceImpactPct": b.get("priceImpactPct"),
                "маршрут": " -> ".join(str((rp.get("swapInfo") or {}).get("label"))
                                        for rp in (b.get("routePlan") or [])) or None,
                "хост": url}
    return {"ошибка": "ни один хост Jupiter не дал котировку", "подробности": errors}


def ultra_order(mint: str, amount_raw: int, taker: str,
                 scrub: Callable[[str], str] = lambda s: s,
                 slippage_bps: int | None = None) -> dict:
    """Jupiter Ultra: заказ на обмен. Возвращает base64-транзакцию и
    requestId -- без requestId execute не примет подписанную сделку.

    slippage_bps -- ЗАДАЁТСЯ ЯВНО, когда вызывающему нужен пол по выходу:
    по документации Ultra это параметр запроса, а 0 означает "Ultra сама
    выберет динамическое проскальзывание". Ответ отдаёт otherAmountThreshold
    -- при swapMode ExactIn это минимальный выход выданной транзакции, то
    есть ровно то число, ниже которого подписывать нельзя.
    """
    params = {"inputMint": mint, "outputMint": WSOL_MINT,
              "amount": str(amount_raw), "taker": taker}
    if slippage_bps is not None:
        params["slippageBps"] = str(int(slippage_bps))
    try:
        r = requests.get(f"{JUP_ULTRA}/order", params=params, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return {"ошибка": f"сеть: {type(exc).__name__}"}
    if r.status_code != 200:
        return {"ошибка": f"http={r.status_code}: {scrub(r.text[:300])}"}
    try:
        b = r.json()
    except ValueError:
        return {"ошибка": "не JSON"}
    if not b.get("transaction") or not b.get("requestId"):
        return {"ошибка": f"в ответе нет transaction/requestId: {scrub(json.dumps(b, default=str)[:300])}"}
    return {"transaction": b["transaction"], "requestId": b["requestId"],
            "outAmount": b.get("outAmount"), "priceImpactPct": b.get("priceImpactPct"),
            # Поля, по которым вызывающий проверяет пол по выходу ДО подписи.
            "otherAmountThreshold": b.get("otherAmountThreshold"),
            "slippageBps": b.get("slippageBps"),
            "swapMode": b.get("swapMode"),
            "inAmount": b.get("inAmount"),
            "router": b.get("router") or b.get("swapType") or b.get("mode")}


def ultra_execute(signed_b64: str, request_id: str,
                   scrub: Callable[[str], str] = lambda s: s) -> dict:
    try:
        r = requests.post(f"{JUP_ULTRA}/execute",
                          json={"signedTransaction": signed_b64, "requestId": request_id},
                          timeout=60)
    except Exception as exc:  # noqa: BLE001
        return {"ошибка": f"сеть: {type(exc).__name__}"}
    try:
        b = r.json()
    except ValueError:
        return {"ошибка": f"http={r.status_code}, не JSON: {scrub(r.text[:300])}"}
    return {"http": r.status_code, "ответ": b,
            "signature": b.get("signature") or b.get("txSignature"),
            "status": b.get("status")}


# ---------- подпись (solders импортируется лениво) ----------

def _solders():
    """Лениво: без ключа подпись не нужна, и сторож не должен падать при
    старте только из-за отсутствия библиотеки."""
    from solders.keypair import Keypair  # noqa: PLC0415
    from solders.pubkey import Pubkey  # noqa: PLC0415
    from solders.transaction import VersionedTransaction  # noqa: PLC0415
    return Keypair, Pubkey, VersionedTransaction


def load_rescue_keypair(secret: str):
    """Ключ принимается в base58 (как отдаёт Phantom) или как JSON-массив
    байт (как пишет solana-keygen). Формат определяется по содержимому, а
    не угадывается."""
    Keypair, _, _ = _solders()
    s = (secret or "").strip()
    if not s:
        raise RuntimeError("RESCUE_WALLET_KEY пуст")
    if s.startswith("["):
        try:
            arr = bytes(json.loads(s))
        except (ValueError, TypeError) as exc:
            raise RuntimeError("RESCUE_WALLET_KEY похож на JSON-массив, но не разбирается") from exc
        return Keypair.from_bytes(arr)
    return Keypair.from_base58_string(s)


def sign_versioned_b64(tx_b64: str, keypair) -> str:
    """Подписать base64-транзакцию Jupiter тем же сообщением -- пересборка
    VersionedTransaction из message и подписи, а не правка байт."""
    _, _, VersionedTransaction = _solders()
    raw = base64.b64decode(tx_b64)
    tx = VersionedTransaction.from_bytes(raw)
    signed = VersionedTransaction(tx.message, [keypair])
    return base64.b64encode(bytes(signed)).decode()


def build_sol_transfer(keypair, to_address: str, lamports: int, blockhash_str: str) -> str:
    """Возврат SOL на кошелёк задачи. Обычный SystemProgram.transfer,
    подписанный ключом утилизатора."""
    from solders.system_program import TransferParams, transfer  # noqa: PLC0415
    from solders.message import Message  # noqa: PLC0415
    from solders.transaction import Transaction  # noqa: PLC0415
    from solders.hash import Hash  # noqa: PLC0415
    _, Pubkey, _ = _solders()
    ix = transfer(TransferParams(from_pubkey=keypair.pubkey(),
                                  to_pubkey=Pubkey.from_string(to_address),
                                  lamports=lamports))
    msg = Message.new_with_blockhash([ix], keypair.pubkey(), Hash.from_string(blockhash_str))
    tx = Transaction([keypair], msg, Hash.from_string(blockhash_str))
    return base64.b64encode(bytes(tx)).decode()


def build_close_account(keypair, token_account: str, program_id: str, blockhash_str: str) -> str:
    """Закрыть пустой токен-аккаунт и вернуть ренту владельцу.
    CloseAccount -- инструкция 9 и в SPL Token, и в Token-2022; счета:
    [аккаунт, получатель ренты, владелец-подписант]."""
    from solders.instruction import AccountMeta, Instruction  # noqa: PLC0415
    from solders.message import Message  # noqa: PLC0415
    from solders.transaction import Transaction  # noqa: PLC0415
    from solders.hash import Hash  # noqa: PLC0415
    _, Pubkey, _ = _solders()
    owner = keypair.pubkey()
    ix = Instruction(
        program_id=Pubkey.from_string(program_id),
        accounts=[AccountMeta(Pubkey.from_string(token_account), False, True),
                  AccountMeta(owner, False, True),
                  AccountMeta(owner, True, False)],
        data=bytes([CLOSE_ACCOUNT_IX]),
    )
    msg = Message.new_with_blockhash([ix], owner, Hash.from_string(blockhash_str))
    tx = Transaction([keypair], msg, Hash.from_string(blockhash_str))
    return base64.b64encode(bytes(tx)).decode()


# ---------- цепь: отправка своих транзакций ----------

def send_raw_b64(rpc: Callable, tx_b64: str) -> str:
    """Отправить подписанную транзакцию. Подпись возвращается сразу, но
    это ещё не исполнение -- подтверждение проверяется по балансу."""
    return rpc("sendTransaction", [tx_b64, {"encoding": "base64",
                                              "skipPreflight": False,
                                              "maxRetries": 3}])


def latest_blockhash(rpc: Callable) -> str:
    res = rpc("getLatestBlockhash", [{"commitment": "confirmed"}])
    bh = ((res or {}).get("value") or {}).get("blockhash")
    if not bh:
        raise RuntimeError("getLatestBlockhash не вернул blockhash")
    return bh


def token_accounts(rpc: Callable, owner: str, mint: str) -> list[dict]:
    res = rpc("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
    out = []
    for acc in (res or {}).get("value") or []:
        try:
            info = acc["account"]["data"]["parsed"]["info"]
            ta = info["tokenAmount"]
            out.append({"pubkey": acc["pubkey"], "raw": int(ta["amount"]),
                         "ui": float(ta.get("uiAmount") or 0.0),
                         "decimals": ta.get("decimals"),
                         "program": acc["account"].get("owner")})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def raw_balance(rpc: Callable, owner: str, mint: str) -> tuple[int, int | None]:
    accs = token_accounts(rpc, owner, mint)
    return sum(a["raw"] for a in accs), (accs[0]["decimals"] if accs else None)


def distribute_ok(status: int | None, body) -> tuple[bool, str]:
    """Разбор ответа /coin_tools/distribute. Форма подтверждена живыми
    вызовами этапа B: {"err": false, "res": [{"err": .., "msg": ..,
    "txid": ..}]}. ВНЕШНИЙ err относится к приёму запроса, ВНУТРЕННИЙ --
    к самой отправке, и они расходятся: Token-2022 дал внешний err=false
    при внутреннем err=true и отказе цепи 0x1f. Читать только внешний err
    значит принять отказ за успех."""
    if status != 200:
        return False, f"http={status}"
    if not isinstance(body, dict):
        return False, f"неожиданная форма ответа: {str(body)[:300]}"
    if body.get("err"):
        return False, str(body.get("msg") or body.get("message") or body)[:400]
    res = body.get("res")
    if not isinstance(res, list) or not res:
        return False, f"в ответе нет списка res -- успех не подтверждён: {str(body)[:300]}"
    first = res[0] if isinstance(res[0], dict) else {}
    if first.get("err"):
        return False, str(first.get("msg") or "внутренний err=true без текста")[:400]
    if not first.get("txid"):
        return False, f"нет txid -- отправка не подтверждена: {str(first)[:300]}"
    return True, str(first.get("txid"))


# ---------- продажа через КОНКРЕТНЫЙ ПУЛ (дешевле спасения) ----------

# Распоряжение владельца: прежде чем везти токен на утилизатор, пробуем
# продать его через пул, который нашёл Jupiter. В API DBot pair -- это
# "address of the token or pair" (docs.dbotx.com/reference/create-fast-
# swaps), и на живой пробе DBot ПРИНЯЛ адрес пула: ордер mud14jnb16qkw8
# ушёл в done, транзакция 3iHeMpjq..., баланс токена упал ровно на
# запрошенную долю. Это обходит поломку distribute для Token-2022: ключи
# никуда не переезжают, это обычная продажа через DBot.
POOL_ROUTE_MIN_SHARE_OF_QUOTE = 0.85


def route_steps(mint: str, amount_raw: int, slippage_bps: int,
                 scrub: Callable[[str], str] = lambda s: s) -> dict:
    """Маршрут Jupiter с адресами пулов. Нужны ammKey и outputMint
    каждого шага: первый ammKey отдаём DBot как pair."""
    if amount_raw <= 0:
        return {"ошибка": "нулевое количество -- маршрут не нужен"}
    errs = {}
    for url in JUP_QUOTE_HOSTS:
        try:
            r = requests.get(url, params={"inputMint": mint, "outputMint": WSOL_MINT,
                                           "amount": str(amount_raw),
                                           "slippageBps": str(slippage_bps)}, timeout=25)
        except Exception as exc:  # noqa: BLE001
            errs[url] = f"сеть: {type(exc).__name__}"; continue
        if r.status_code != 200:
            errs[url] = f"http={r.status_code}: {scrub(r.text[:200])}"; continue
        try:
            b = r.json()
        except ValueError:
            errs[url] = "не JSON"; continue
        if b.get("error") or b.get("errorCode"):
            errs[url] = str(b.get("error") or b.get("errorCode")); continue
        steps = []
        for rp in (b.get("routePlan") or []):
            si = rp.get("swapInfo") or {}
            steps.append({"label": si.get("label"), "ammKey": si.get("ammKey"),
                           "inputMint": si.get("inputMint"), "outputMint": si.get("outputMint"),
                           "percent": rp.get("percent")})
        out = b.get("outAmount")
        return {"SOL": int(out) / 1e9 if out else None, "шаги": steps,
                "priceImpactPct": b.get("priceImpactPct")}
    return {"ошибка": "ни один хост Jupiter не дал маршрут", "подробности": errs}


@dataclass
class PoolSellContext:
    wallet: str
    wallet_id: str
    mint: str
    task_name: str
    cfg: RescueConfig
    rpc: Callable
    dbot_post: Callable
    dbot_sell: Callable       # (pair, wallet_id, slippage) -> (status, body, slippage)
    order_wait: Callable      # (ids) -> list[dict]
    order_ids: Callable       # (body) -> list[str]
    max_slippage: float = 0.4
    notify: Callable = lambda text: None
    log: Callable = lambda text: None
    scrub: Callable = lambda s: s
    steps: list = field(default_factory=list)


def _psl(ctx: PoolSellContext, name: str, **data) -> None:
    rec = {"шаг": name, "когда_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **data}
    ctx.steps.append(rec)
    ctx.log(ctx.scrub(f"продажа-через-пул/{name}: {json.dumps(data, ensure_ascii=False, default=str)[:600]}"))


def _sell_via(ctx: PoolSellContext, pair: str, what: str) -> dict:
    """Одна продажа с заданным pair. Успех -- ТОЛЬКО уменьшение баланса в
    цепи. Возвращает разложение выручки, если продажа состоялась."""
    before, dec = raw_balance(ctx.rpc, ctx.wallet, what)
    if before <= 0:
        return {"продано": False, "причина": f"остатка {what} нет"}
    st, resp, slip = ctx.dbot_sell(pair, ctx.wallet_id, ctx.max_slippage)
    ids = ctx.order_ids(resp)
    rows = ctx.order_wait(ids) if ids else []
    _psl(ctx, "продажа", pair=pair, минт=what, http=st, maxSlippage=slip,
         ордера=rows, ответ=json.loads(ctx.scrub(json.dumps(resp, default=str))))
    time.sleep(6)
    after, _ = raw_balance(ctx.rpc, ctx.wallet, what)
    if after >= before:
        return {"продано": False, "причина": "баланс в цепи не уменьшился",
                "ордера": rows, "было_raw": before, "стало_raw": after}
    sig = next((r.get("swapHash") for r in rows if r.get("swapHash")), None)
    res = {"продано": True, "было_raw": before, "стало_raw": after,
            "продано_raw": before - after, "подпись": sig, "ордера": rows}
    if sig:
        res["выручка"] = swap_proceeds_sol(ctx.rpc, sig, ctx.wallet)
    return res


def sell_via_pool(ctx: PoolSellContext) -> dict:
    """Полный путь: маршрут -> продажа через пул первого шага -> если
    промежуточный токен не SOL, вторая продажа его же.

    ГЕЙТ ВЛАДЕЛЬЦА: суммарная выручка обмена должна быть не меньше 85% от
    котировки Jupiter, иначе стоп и алерт. Считается по ВЫРУЧКЕ ОБМЕНА
    (см. swap_proceeds_sol), а не по дельте баланса кошелька: чаевые и
    приоритет почти постоянны и на мелкой позиции перевешивают выручку --
    гейт по дельте срабатывал бы ложно."""
    ctx.steps = []
    raw, dec = raw_balance(ctx.rpc, ctx.wallet, ctx.mint)
    r = route_steps(ctx.mint, raw, ctx.cfg.slippage_bps, ctx.scrub)
    _psl(ctx, "маршрут", остаток_raw=raw, **{k: v for k, v in r.items() if k != "шаги"},
         шагов=len(r.get("шаги") or []))
    if r.get("ошибка") or not r.get("шаги"):
        return {"итог": "стоп", "причина": f"маршрута нет: {r.get('ошибка')}", "шаги": ctx.steps}
    quote = r.get("SOL")
    if not quote:
        return {"итог": "стоп", "причина": "котировка пустая", "шаги": ctx.steps}

    first = r["шаги"][0]
    pair, out_mint = first.get("ammKey"), first.get("outputMint")
    _psl(ctx, "выбран_пул", pair=pair, label=first.get("label"), outputMint=out_mint,
         сразу_в_SOL=(out_mint == WSOL_MINT), котировка_SOL=quote)
    if not pair:
        return {"итог": "стоп", "причина": "у первого шага нет ammKey", "шаги": ctx.steps}

    # ЗАЩИТА ОТ ЗАСТРЕВАНИЯ В ПРОМЕЖУТОЧНОМ ТОКЕНЕ. Если первый шаг ведёт
    # не в SOL, вторая продажа пойдёт по ПРЯМОЙ паре промежуточный->SOL,
    # а прямой пары может не быть -- ровно так и появляются зависшие
    # позиции. Поэтому заранее проверяем, что у промежуточного токена
    # прямой маршрут в SOL существует; нет -- не начинаем вовсе.
    if out_mint and out_mint != WSOL_MINT:
        probe = quote_sol_for(out_mint, 10 ** 6, ctx.cfg.slippage_bps, ctx.scrub)
        _psl(ctx, "проверка_промежуточного", минт=out_mint, котировка=probe)
        if probe.get("ошибка"):
            return {"итог": "стоп",
                    "причина": (f"первый шаг ведёт в {out_mint}, а обратного маршрута в SOL у него "
                                 f"нет ({probe.get('ошибка')}) -- застрянем в промежуточном токене"),
                    "шаги": ctx.steps}

    got = 0.0
    a = _sell_via(ctx, pair, ctx.mint)
    _psl(ctx, "итог_первой_продажи", **{k: v for k, v in a.items() if k != "ордера"})
    if not a.get("продано"):
        return {"итог": "стоп", "причина": f"продажа через пул не прошла: {a.get('причина')}",
                "шаги": ctx.steps}
    got += float((a.get("выручка") or {}).get("выручка_обмена_SOL") or 0.0)

    if out_mint and out_mint != WSOL_MINT:
        b = _sell_via(ctx, out_mint, out_mint)
        _psl(ctx, "итог_второй_продажи", **{k: v for k, v in b.items() if k != "ордера"})
        if not b.get("продано"):
            ctx.notify(f"{ctx.task_name}: {ctx.mint} продан в {out_mint}, но вторая продажа "
                       f"{out_mint} -> SOL не прошла ({b.get('причина')}). Позиция теперь в "
                       f"промежуточном токене -- нужна ручная проверка.")
            return {"итог": "частично", "причина": "застряли в промежуточном токене",
                    "шаги": ctx.steps}
        got += float((b.get("выручка") or {}).get("выручка_обмена_SOL") or 0.0)

    share = got / quote if quote else None
    _psl(ctx, "гейт_85", выручка_SOL=round(got, 9), котировка_SOL=quote,
         доля=round(share, 4) if share is not None else None,
         порог=POOL_ROUTE_MIN_SHARE_OF_QUOTE)
    if share is None or share < POOL_ROUTE_MIN_SHARE_OF_QUOTE:
        ctx.notify(f"{ctx.task_name}: {ctx.mint} продан через пул {pair}, но выручка "
                   f"{got:.9f} SOL это {share:.1%} от котировки {quote:.9f} SOL "
                   f"(порог {POOL_ROUTE_MIN_SHARE_OF_QUOTE:.0%}). Останавливаюсь, дальше вручную."
                   if share is not None else
                   f"{ctx.task_name}: выручку обмена измерить не удалось -- останавливаюсь")
        return {"итог": "продано_но_дёшево", "выручка_SOL": round(got, 9),
                "котировка_SOL": quote, "доля": share, "шаги": ctx.steps}

    ctx.notify(f"{ctx.task_name}: {ctx.mint} продан через пул {pair}. Выручка {got:.9f} SOL, "
               f"{share:.1%} от котировки Jupiter.")
    return {"итог": "продано", "выручка_SOL": round(got, 9), "котировка_SOL": quote,
            "доля": share, "шаги": ctx.steps}


# ---------- измерение выручки свопа (для гейта 85%) ----------

# Чаевые и комиссия платформы уходят обычными переводами внутри той же
# транзакции. Список релеев -- тот же, что в solana_ledger_run.py, он уже
# установлен на реальных данных этого проекта.
DBOT_FEE_ADDRESS = "F7F8QYPCc3zDYNeAh4UUE2wJ7Mq1huid5MeMo7PPzsqB"
KNOWN_TIP_ADDRESSES = {
    "DiTmWENJsHQdawVUUKnUXkconcpW4Jv52TnMWhkncF6t",
    "AsTRAEoyMofR3vUPpf9k68Gsfb6ymTZttEtsAbv8Bk4d",
    "astra9xWY93QyfG6yM8zwsKsRodscjQ2uU2HKNL5prk",
    "ste11p5x8tJ53H1NbNQsRBg1YNRd4GcVpxtDw8PBpmb",
    "ste11eZvF5bebo6EVyGMJHV6LtPtx7e6TzNZtxPfXGy",
    "LandX6RsjjnfaxfYAFTZX1TW9Cgfe9HFStj9nRDd5SK",
}
TIP_PREFIXES = ("astra", "AsTra", "ste11", "LandX")


def _is_tip_address(addr: str) -> bool:
    return (addr == DBOT_FEE_ADDRESS or addr in KNOWN_TIP_ADDRESSES
            or any(addr.startswith(p) for p in TIP_PREFIXES))


def swap_proceeds_sol(rpc: Callable, signature: str, wallet: str) -> dict:
    """Сколько SOL принёс САМ ОБМЕН, отдельно от издержек.

    ПОЧЕМУ НЕ ДЕЛЬТА БАЛАНСА КОШЕЛЬКА. На реальной пробе (продажа пыли
    STONK через адрес пула, tx 3iHeMpjq...) обмен дал ~0.0007 SOL, а
    баланс кошелька УПАЛ на 0.0025 SOL: приоритет, чаевые и комиссия сети
    -- величины почти постоянные и на мелкой позиции перевешивают
    выручку. Гейт "получено >= 85% котировки", посчитанный по дельте
    баланса, срабатывал бы ложно на любой мелочи и был бы искажён
    чаевыми на крупной. Поэтому выручка считается как дельта кошелька
    ПЛЮС обратно комиссия сети, чаевые и комиссия платформы -- то есть
    то, что дал именно обмен.

    Возвращает разложение целиком, чтобы в алерте было видно каждую
    составляющую, а не одно число."""
    tx = rpc("getTransaction", [signature, {"encoding": "jsonParsed",
                                             "maxSupportedTransactionVersion": 1,
                                             "commitment": "confirmed"}])
    if not tx:
        return {"ошибка": "транзакция не отдалась узлом"}
    meta = tx.get("meta") or {}
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in (msg.get("accountKeys") or [])]
    if wallet not in keys:
        return {"ошибка": "кошелька нет среди счетов транзакции"}
    i = keys.index(wallet)
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    if len(pre) <= i or len(post) <= i:
        return {"ошибка": "в транзакции нет балансов по индексу кошелька"}
    native = (post[i] - pre[i]) / 1e9

    wsol = 0.0
    for lst, sign in ((meta.get("preTokenBalances") or [], -1), (meta.get("postTokenBalances") or [], 1)):
        for tb in lst:
            if tb.get("owner") == wallet and tb.get("mint") == WSOL_MINT:
                amt = ((tb.get("uiTokenAmount") or {}).get("uiAmount")) or 0.0
                wsol += sign * float(amt)

    fee = (meta.get("fee") or 0) / 1e9 if keys and keys[0] == wallet else 0.0

    tips = 0.0
    groups = [msg.get("instructions") or []]
    for inner in (meta.get("innerInstructions") or []):
        groups.append(inner.get("instructions") or [])
    for grp in groups:
        for ins in grp:
            info = ((ins or {}).get("parsed") or {}).get("info") or {}
            if not isinstance(info, dict):
                continue
            if info.get("source") != wallet:
                continue
            dest = info.get("destination")
            lam = info.get("lamports")
            if dest and lam and _is_tip_address(dest):
                tips += float(lam) / 1e9

    proceeds = native + wsol + fee + tips
    return {"выручка_обмена_SOL": round(proceeds, 9),
            "дельта_кошелька_SOL": round(native + wsol, 9),
            "комиссия_сети_SOL": round(fee, 9),
            "чаевые_и_платформа_SOL": round(tips, 9),
            "подпись": signature}


# ---------- оркестратор спасения (этап D) ----------

@dataclass
class RescueContext:
    wallet: str                 # кошелёк задачи, откуда спасаем
    wallet_id: str              # walletId в DBot (для distribute)
    mint: str
    task_name: str
    sol_in: float | None        # вход из учёта; None -- значит неизвестен
    whitelist: set[str]         # адреса кошельков задач, живьём из follow_orders
    cfg: RescueConfig
    rpc: Callable               # (method, params) -> result, Helius
    dbot_post: Callable         # (path, body) -> (status, body)
    notify: Callable = lambda text: None
    log: Callable = lambda text: None
    scrub: Callable = lambda s: s
    steps: list = field(default_factory=list)


def _step(ctx: RescueContext, name: str, **data) -> None:
    rec = {"шаг": name, "когда_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **data}
    ctx.steps.append(rec)
    ctx.log(ctx.scrub(f"спасение/{name}: {json.dumps(data, ensure_ascii=False, default=str)[:600]}"))


def rescue_position(ctx: RescueContext, keypair=None) -> dict:
    """Полная последовательность спасения. Ничего не отправляет, пока
    cfg.live не равен True: до этого считается и печатается план."""
    c = ctx.cfg
    ctx.steps = []

    # 1. остаток и котировка
    raw, dec = raw_balance(ctx.rpc, ctx.wallet, ctx.mint)
    _step(ctx, "остаток", raw=raw, decimals=dec)
    q = quote_sol_for(ctx.mint, raw, c.slippage_bps, ctx.scrub)
    _step(ctx, "котировка", **q)

    # 2. решение по порогу и потолку
    verdict, why = decide(q.get("SOL"), ctx.sol_in, c)
    _step(ctx, "решение", вердикт=verdict, причина=why)
    if verdict != "идём":
        ctx.notify(f"Спасение {ctx.mint} на {ctx.wallet} ({ctx.task_name}) НЕ начато: {why}")
        return {"итог": "стоп", "причина": why, "шаги": ctx.steps}

    if not c.live:
        plan = (f"ПЛАН (RESCUE_LIVE выключен, ничего не отправлено): перевести {raw} сырых единиц "
                f"{ctx.mint} с {ctx.wallet} на утилизатор, продать через Jupiter (~{q.get('SOL')} SOL), "
                f"вернуть SOL на {ctx.wallet}")
        _step(ctx, "план", текст=plan)
        ctx.notify(plan)
        return {"итог": "план", "причина": "RESCUE_LIVE не включён", "шаги": ctx.steps}

    if keypair is None:
        raise RuntimeError("боевой режим включён, но ключ утилизатора не передан")
    rescue_addr = str(keypair.pubkey())

    # 3. перевод токена на утилизатор
    amount_ui = raw / (10 ** (dec or 0))
    st, resp = ctx.dbot_post("/coin_tools/distribute", {
        "chain": "solana", "fromWalletId": ctx.wallet_id,
        "toList": [{"address": rescue_addr, "amountUI": amount_ui}], "token": ctx.mint})
    _step(ctx, "distribute", http=st, ответ=json.loads(ctx.scrub(json.dumps(resp, default=str))))
    ok_sent, why_not = distribute_ok(st, resp)
    if not ok_sent:
        msg = ctx.scrub(why_not[:500])
        _step(ctx, "distribute_отказ", сообщение=msg)
        ctx.notify(f"Спасение {ctx.mint}: перевод на утилизатор отклонён -- {msg}")
        return {"итог": "стоп", "причина": f"distribute отклонён: {msg}", "шаги": ctx.steps}

    # 4. ждём ПРИХОДА ПО ЦЕПИ, а не верим ответу API
    deadline = time.time() + c.arrive_timeout_s
    arrived = 0
    while time.time() < deadline:
        time.sleep(5)
        arrived, _ = raw_balance(ctx.rpc, rescue_addr, ctx.mint)
        if arrived > 0:
            break
    _step(ctx, "пришло_на_утилизатор", raw=arrived, отправлено_raw=raw,
          удержано_raw=raw - arrived)
    if arrived <= 0:
        ctx.notify(f"Спасение {ctx.mint}: за {c.arrive_timeout_s}с токен на утилизатор не пришёл")
        return {"итог": "стоп", "причина": "токен не пришёл на утилизатор", "шаги": ctx.steps}

    # 5. продажа через Jupiter Ultra -- по ФАКТИЧЕСКИ пришедшему количеству
    sold = None
    for attempt in range(c.retries + 1):
        order = ultra_order(ctx.mint, arrived, rescue_addr, ctx.scrub)
        if order.get("ошибка"):
            _step(ctx, "ultra_order_ошибка", попытка=attempt + 1, ошибка=order["ошибка"])
            continue
        signed = sign_versioned_b64(order["transaction"], keypair)
        ex = ultra_execute(signed, order["requestId"], ctx.scrub)
        _step(ctx, "ultra_execute", попытка=attempt + 1, status=ex.get("status"),
              signature=ex.get("signature"), http=ex.get("http"))
        left, _ = raw_balance(ctx.rpc, rescue_addr, ctx.mint)
        if left < arrived:
            sold = ex.get("signature")
            break
        _step(ctx, "продажа_не_подтверждена", остаток_raw=left)
    if sold is None:
        ctx.notify(f"Спасение {ctx.mint}: Jupiter не продал за {c.retries + 1} попыток; "
                   f"токен лежит на утилизаторе {rescue_addr}")
        return {"итог": "частично", "причина": "токен на утилизаторе, продажа не прошла",
                "шаги": ctx.steps}

    # 6. возврат SOL на кошелёк задачи -- только через белый список
    assert_whitelisted(ctx.wallet, ctx.whitelist)
    bal_lamports = ((ctx.rpc("getBalance", [rescue_addr]) or {}).get("value") or 0)
    reserve = int(c.return_reserve_sol * 1e9)
    send_lamports = bal_lamports - reserve
    _step(ctx, "возврат_расчёт", баланс_lamports=bal_lamports, резерв_lamports=reserve,
          к_отправке_lamports=send_lamports, получатель=ctx.wallet)
    ret_sig = None
    if send_lamports > 0:
        ret_sig = send_raw_b64(ctx.rpc, build_sol_transfer(
            keypair, ctx.wallet, send_lamports, latest_blockhash(ctx.rpc)))
        _step(ctx, "возврат_отправлен", signature=ret_sig)
    else:
        _step(ctx, "возврат_пропущен", причина="после резерва отправлять нечего")

    # 7. закрыть пустой токен-аккаунт -- вернуть ренту
    for a in token_accounts(ctx.rpc, rescue_addr, ctx.mint):
        if a["raw"] == 0:
            try:
                sig = send_raw_b64(ctx.rpc, build_close_account(
                    keypair, a["pubkey"], a["program"], latest_blockhash(ctx.rpc)))
                _step(ctx, "токен_аккаунт_закрыт", pubkey=a["pubkey"], signature=sig)
            except Exception as exc:  # noqa: BLE001
                _step(ctx, "закрытие_не_удалось", pubkey=a["pubkey"],
                      ошибка=ctx.scrub(f"{type(exc).__name__}: {exc}"))

    ctx.notify(f"Спасение {ctx.mint} на {ctx.wallet} ({ctx.task_name}) завершено. "
               f"Продажа {sold}, возврат {ret_sig}, вернулось "
               f"{send_lamports / 1e9:.9f} SOL при входе {ctx.sol_in} SOL.")
    return {"итог": "спасено", "продажа": sold, "возврат": ret_sig,
            "вернулось_SOL": send_lamports / 1e9, "шаги": ctx.steps}


# ---------- самопроверка границ (без сети, без ключей) ----------

def self_test() -> None:
    cfg = RescueConfig()
    checks: list[tuple[str, bool, str]] = []

    def chk(name: str, cond: bool, got: str = "") -> None:
        checks.append((name, bool(cond), got))

    chk("по умолчанию НЕ боевой", cfg.live is False, str(cfg.live))
    chk("потолок на спасение = 3 SOL", cfg.max_quote_sol == 3.0, str(cfg.max_quote_sol))
    chk("порог = 30% от входа", cfg.min_share_of_sol_in == 0.30, str(cfg.min_share_of_sol_in))

    d, why = decide(3.0001, 0.5, cfg)
    chk("выше потолка -> стоп", d == "стоп" and "потолка" in why, why)
    d, why = decide(3.0, 0.5, cfg)
    chk("ровно потолок -> идём", d == "идём", why)
    d, why = decide(0.2, None, cfg)
    chk("вход неизвестен -> стоп", d == "стоп" and "неизвестен" in why, why)
    d, why = decide(0.1, 0.5, cfg)
    chk("ниже 30% входа -> стоп", d == "стоп" and "порога" in why, why)
    d, why = decide(0.15, 0.5, cfg)
    chk("ровно 30% входа -> идём", d == "идём", why)
    d, why = decide(None, 0.5, cfg)
    chk("нет котировки -> стоп", d == "стоп", why)
    d, why = decide(0.0, 0.5, cfg)
    chk("нулевая котировка -> стоп", d == "стоп", why)

    wl = {"AAA", "BBB"}
    try:
        assert_whitelisted("CCC", wl)
        chk("чужой адрес отклоняется", False, "исключения не было")
    except RuntimeError:
        chk("чужой адрес отклоняется", True)
    try:
        assert_whitelisted("AAA", wl)
        chk("свой адрес проходит", True)
    except RuntimeError as exc:
        chk("свой адрес проходит", False, str(exc))
    try:
        assert_whitelisted("AAA", set())
        chk("пустой белый список отклоняется", False, "исключения не было")
    except RuntimeError:
        chk("пустой белый список отклоняется", True)

    ok, why = distribute_ok(200, {"err": False, "res": [{"err": False, "msg": "OK", "txid": "T"}]})
    chk("distribute: успех с txid", ok and why == "T", why)
    ok, why = distribute_ok(200, {"err": False, "res": [{"err": True, "msg": "0x1f"}]})
    chk("distribute: внешний err=false, внутренний true -> отказ", not ok and "0x1f" in why, why)
    ok, why = distribute_ok(200, {"err": False, "res": [{"err": False, "msg": "OK"}]})
    chk("distribute: нет txid -> не успех", not ok, why)
    ok, why = distribute_ok(200, {"err": True, "msg": "нет прав"})
    chk("distribute: внешний err=true -> отказ", not ok, why)
    ok, why = distribute_ok(500, {})
    chk("distribute: http!=200 -> отказ", not ok, why)
    ok, why = distribute_ok(200, {"err": False})
    chk("distribute: без res -> не успех", not ok, why)

    chk("порог продажи через пул = 85%", POOL_ROUTE_MIN_SHARE_OF_QUOTE == 0.85,
        str(POOL_ROUTE_MIN_SHARE_OF_QUOTE))
    chk("адрес комиссии платформы опознан", _is_tip_address(DBOT_FEE_ADDRESS))
    chk("релей по префиксу опознан", _is_tip_address("astra9xWY93QyfG6yM8zwsKsRodscjQ2uU2HKNL5prk"))
    chk("обычный адрес не считается чаевыми",
        not _is_tip_address("BmjAUDbwBMxR5shrmzBtKRwveVahFGFiEH3oTq7QTHnu"))

    os.environ.pop("RESCUE_LIVE", None)
    chk("из окружения без RESCUE_LIVE -- не боевой", RescueConfig.from_env().live is False)

    bad = 0
    for name, good, got in checks:
        print(f"  [{'ok  ' if good else 'СБОЙ'}] {name}" + (f"  -> {got}" if got and not good else ""))
        if not good:
            bad += 1
    print(f"самопроверка границ спасения: {len(checks) - bad}/{len(checks)} пройдено")
    if bad:
        raise SystemExit(f"самопроверка не пройдена: {bad} из {len(checks)}")


if __name__ == "__main__":
    self_test()
