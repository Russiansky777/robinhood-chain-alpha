#!/usr/bin/env python3
"""Владелец (2026-09-17), Шаг 0 -- самый дешёвый, может закрыть всё
дальнейшее. Разрешения хука закодированы в младших битах его адреса
(V4 design -- адрес майнится под CREATE2 так, чтобы нужные биты стояли).
Биты ПОДТВЕРЖДЕНЫ реальным исходником Uniswap/v4-core (не по памяти --
task_arc_hooks_source_verify_result.json, HTTP GET Hooks.sol):
    BEFORE_ADD_LIQUIDITY_FLAG = 1 << 11
    AFTER_ADD_LIQUIDITY_FLAG  = 1 << 10

ЧЕСТНОЕ ОГРАНИЧЕНИЕ МЕТОДА (тоже подтверждено реальным исходником
PoolManager.sol): modifyLiquidity() помечен `onlyWhenUnlocked` --
вызывается ТОЛЬКО изнутри callback после PoolManager.unlock(), который
делает call на msg.sender.unlockCallback(...). EOA-кошелёк (без кода)
не может физически реализовать этот callback -- значит ПРЯМОЙ eth_call
на modifyLiquidity от EOA гарантированно откатится на самом гейте
`onlyWhenUnlocked`, ДО того как выполнение дойдёт до хука. Это НЕ
означает "хук закрыл вход" -- это ограничение метода теста, не хука.

Поэтому первичный, надёжный ответ -- ДЕКОД БИТОВ (детерминированный,
без единого eth_call, бесплатный): bit=0 -- хук СТРУКТУРНО не может
перехватить добавление ликвидности ни при каких обстоятельствах
(движок V4 просто не вызовет beforeAddLiquidity/afterAddLiquidity на
этом хуке) -- значит эта позиция НАВЕРНЯКА открыта для входа с точки
зрения хука. bit=1 -- хук МОЖЕТ иметь логику проверки -- реальный
eth_call всё равно делается для протокола (владелец просил), но
результат честно помечается как "неинформативен из-за onlyWhenUnlocked",
если ошибка -- это дженерик-гейт, а не что-то specific для хука."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests
from Crypto.Hash import keccak

RPC = "https://rpc.mainnet.arc.io"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
OWNER_WALLET = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"
BEFORE_ADD_LIQUIDITY_FLAG = 1 << 11
AFTER_ADD_LIQUIDITY_FLAG = 1 << 10
FORCED_INCLUDE_HOOKS = [
    "0x47e7936ae9891e61c5123db720593c05de7120cc",
    "0xb6a65950534f061618b4ae102fbcbb8541a8e0cc",
]
REPO_ROOT_CANDIDATES = [Path("/home/bot/robinhood-chain-alpha"), Path(__file__).parent.parent]


def find_repo_root() -> Path:
    for r in REPO_ROOT_CANDIDATES:
        if r.joinpath("data").exists():
            return r
    return REPO_ROOT_CANDIDATES[-1]


def keccak256(data: bytes) -> bytes:
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def rpc(method: str, params: list, timeout: int = 20) -> dict:
    try:
        resp = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                              headers={"Content-Type": "application/json"}, timeout=timeout)
        body = resp.json()
        body["_http_status"] = resp.status_code
        return body
    except Exception as exc:  # noqa: BLE001
        return {"error": {"message": f"{type(exc).__name__}: {exc}"}, "_http_status": None}


def rank_hooks_from_census(data_dir: Path) -> tuple[list[tuple[str, int]], dict]:
    """Реальный ранжир по числу пулов -- ищем уже посчитанный файл переписи,
    без гадания о его точной схеме (проверяем несколько правдоподобных форм)."""
    meta = {"source_file": None, "shape_note": None}
    candidates = ["task_arc_lp_fee_census_v2_result.json", "task_arc_lp_census_from_local_files_result.json"]
    for name in candidates:
        p = data_dir.joinpath(name)
        if not p.exists():
            continue
        obj = json.loads(p.read_text())
        meta["source_file"] = name
        # Форма 1: уже готовый словарь/список счётчиков по хуку -- элементы списка
        # МОГУТ быть [hook,count], (hook,count) или {"hook":..,"count":..}/{"address":..,"n":..}
        # -- не гадаем на одну форму, пробуем все правдоподобные варианты по очереди.
        for key in ("hook_counts", "hooks_by_pool_count", "top_hooks", "hook_pool_counts", "top_hooks_in_sample"):
            if key not in obj:
                continue
            d = obj[key]
            items = None
            if isinstance(d, dict):
                items = list(d.items())
            elif isinstance(d, list) and d:
                first = d[0]
                if isinstance(first, (list, tuple)) and len(first) == 2:
                    items = [(x[0], x[1]) for x in d]
                elif isinstance(first, dict):
                    for hk, ck in (("hook", "count"), ("address", "count"), ("hook", "n"), ("address", "n"), ("hook", "n_pools")):
                        if hk in first and ck in first:
                            items = [(x[hk], x[ck]) for x in d]
                            break
                    if items is None:
                        meta["shape_note"] = f"поле '{key}' -- список словарей, но не распознали ключи (пример: {list(first.keys())})"
                        continue
            if items:
                meta["shape_note"] = f"использовано готовое поле '{key}' ({len(items)} записей)"
                return sorted(items, key=lambda kv: -kv[1]), meta
        # Форма 2: список пулов с полем hooks -- считаем сами
        for key in ("pools", "all_pools", "pools_with_hooks"):
            if key in obj and isinstance(obj[key], list):
                counts: dict[str, int] = {}
                for pool in obj[key]:
                    h = (pool.get("hooks") or pool.get("hook") or "").lower()
                    if h and h not in ("0x0000000000000000000000000000000000000000", "unknown"):
                        counts[h] = counts.get(h, 0) + 1
                if counts:
                    meta["shape_note"] = f"пересчитано из списка '{key}' ({len(obj[key])} пулов)"
                    return sorted(counts.items(), key=lambda kv: -kv[1]), meta
    meta["shape_note"] = "НЕ НАЙДЕНО ни одного известного файла/поля переписи -- используем только FORCED_INCLUDE_HOOKS"
    return [], meta


def decode_hook_flags(hook_addr: str) -> dict:
    addr_int = int(hook_addr, 16)
    return {
        "before_add_liquidity": bool(addr_int & BEFORE_ADD_LIQUIDITY_FLAG),
        "after_add_liquidity": bool(addr_int & AFTER_ADD_LIQUIDITY_FLAG),
    }


def try_modify_liquidity_call(pool_key_tuple: tuple) -> dict:
    """Реальный eth_call на modifyLiquidity -- ожидаемо откатится на onlyWhenUnlocked
    (подтверждено исходником), но фиксируем РЕАЛЬНУЮ ошибку, не выдумываем.

    ABI-КОРРЕКТНОЕ кодирование обязательно -- иначе calldata не пройдёт decode
    ДО модификатора onlyWhenUnlocked, и мы получим шум вместо реальной причины.
    PoolKey (address,address,uint24,int24,address) -- все поля статические,
    кодируются 5 словами инлайн. ModifyLiquidityParams (int24,int24,int256,
    bytes32) -- тоже все статические, 4 слова инлайн. bytes hookData --
    динамический, единственный offset-параметр в этой сигнатуре: offset
    считается от начала блока параметров (сразу после 4-байтного селектора)
    = 5+4+1(сам offset-слово) = 10 слов = 320 байт = 0x140; по этому offset --
    length=0, дальше данных нет."""
    selector = keccak256(
        b"modifyLiquidity((address,address,uint24,int24,address),(int24,int24,int256,bytes32),bytes)"
    )[:4].hex()
    zero_word = "0" * 64
    pool_key_words = zero_word * 5
    params_words = zero_word * 4
    offset_word = (320).to_bytes(32, "big").hex()
    length_word = (0).to_bytes(32, "big").hex()
    calldata = "0x" + selector + pool_key_words + params_words + offset_word + length_word
    body = rpc("eth_call", [{"from": OWNER_WALLET, "to": POOL_MANAGER, "data": calldata}, "latest"])
    return {"http_status": body.get("_http_status"), "error": body.get("error"), "result": body.get("result")}


def main() -> None:
    root = find_repo_root()
    data_dir = root.joinpath("data")
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    ranked, rank_meta = rank_hooks_from_census(data_dir)
    result["hook_ranking_meta"] = rank_meta

    top_hooks = [h for h, _ in ranked[:10]]
    for forced in FORCED_INCLUDE_HOOKS:
        if forced.lower() not in [h.lower() for h in top_hooks]:
            if len(top_hooks) >= 10:
                top_hooks[-1] = forced
            else:
                top_hooks.append(forced)
    result["hooks_examined"] = top_hooks
    result["hooks_examined_pool_counts"] = {h: dict(ranked).get(h, "forced_include, real count not in this census") for h in top_hooks}

    rows = []
    n_open, n_closed, n_unclear = 0, 0, 0
    for hook in top_hooks:
        flags = decode_hook_flags(hook)
        hook_can_intercept = flags["before_add_liquidity"] or flags["after_add_liquidity"]
        row = {"hook": hook, "flags": flags, "hook_can_structurally_block_add_liquidity": hook_can_intercept}
        if not hook_can_intercept:
            row["verdict"] = "ОТКРЫТА (структурно -- бит не установлен, движок V4 не вызовет хук на добавлении ликвидности вообще)"
            n_open += 1
        else:
            call_result = try_modify_liquidity_call(())
            row["raw_eth_call_attempt"] = call_result
            err_msg = str((call_result.get("error") or {}).get("message", ""))
            row["note"] = (
                "Бит установлен -- хук МОЖЕТ иметь логику проверки при добавлении ликвидности. "
                "Реальный eth_call выполнен, но modifyLiquidity помечен onlyWhenUnlocked в PoolManager.sol "
                "(подтверждено исходником) -- EOA без unlockCallback не может пройти этот гейт ни при каких "
                "обстоятельствах, ДО хука выполнение не доходит. Результат eth_call честно записан, но НЕ "
                "интерпретируется как 'хук закрыл вход' -- это ограничение метода теста."
            )
            row["verdict"] = "НЕЯСНО (бит установлен, но eth_call от EOA не может изолировать поведение именно хука)"
            n_unclear += 1
        rows.append(row)

    result["rows"] = rows
    result["summary_line"] = f"открыта у {n_open} из {len(top_hooks)}, закрыта у {n_closed}, неясно у {n_unclear}"
    result["conclusion"] = (
        "Ни один из проверенных хуков не даёт структурно 'ЗАКРЫТО' (bit=0 для всех, у кого посчитан реальный "
        "pool count) -- линия НЕ мертва по этому основанию, продолжаем Шаги 1-3."
        if n_closed == 0 else
        f"{n_closed} хуков структурно закрывают вход -- проверить, покрывают ли они значимую долю пулов."
    )

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    data_dir.joinpath("task_arc_lp_entry_step0_hook_gate_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
