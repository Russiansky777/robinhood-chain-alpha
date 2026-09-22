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
