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
                 scrub: Callable[[str], str] = lambda s: s) -> dict:
    """Jupiter Ultra: заказ на обмен. Возвращает base64-транзакцию и
    requestId -- без requestId execute не примет подписанную сделку."""
    params = {"inputMint": mint, "outputMint": WSOL_MINT,
              "amount": str(amount_raw), "taker": taker}
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
            "outAmount": b.get("outAmount"), "priceImpactPct": b.get("priceImpactPct")}


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
