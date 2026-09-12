#!/usr/bin/env python3
"""Задача 5, стадия 2 (V4-адаптация бота): историческое воспроизведение
контрольного маршрута USDG -> MOSIAI -> USDG через два hookless
Uniswap V4-пула, вызовом РЕАЛЬНОГО V4Quoter (0x8dc178ef...) через
наш собственный, уже доверенный RPC-путь (analysis/alchemy_fallback.py
-- публичный RPC первым, Alchemy фолбэком), А НЕ через домены из
приложенного аудита (rpc-robinhood.blockmachine.io / robinscan.io,
которые в этом проекте никогда раньше не использовались).

Контрольная транзакция: 0xd487122244e6c89a0c7b91d48111720575294a6cfeceedfaa3cf105b1fb996d0
(блок исполнения 61248737). Пул A (заём): id 0xcb90948a...
PoolKey(USDG,MOSIAI,fee=70000,tickSpacing=4,hooks=0x0). Пул B
(закрытие): id 0x92ed62e7... PoolKey(..., tickSpacing=3, ...).

ТРЕБОВАНИЕ ВЛАДЕЛЬЦА (дословно): "наш код должен рассчитать этот
маршрут из состояния пулов, не подставляя известный ответ. Сначала
воспроизведи заданный размер, затем проверь самостоятельный выбор
направления и размера сделки. Положительное обнаружение на N-1 и
отрицательный результат заданного размера на N-10 -- контрольные
случаи."

Что делает этот скрипт:
  1. Известный размер (9.437184 USDG) на known-блоках N-1=61248736 и
     N-10=61248727 -- сверка с заявленными 10.242566 / 8.953460 USDG.
  2. САМОСТОЯТЕЛЬНЫЙ выбор направления: на блоке N-1 (где сеть уже дала
     положительный результат для 9.437184 USDG) пробуем цикл В ДРУГОМ
     порядке (сперва pool B, потом pool A) для того же входного размера
     -- ожидаем, что он даст РАВНОЗНАЧНЫЙ или худший результат (это тот
     же самый экономический цикл, реверснутый, -- если бы он был лучше,
     это значило бы, что мы вообще перепутали, какой пул "дёшев", а
     какой "дорог").
  3. САМОСТОЯТЕЛЬНЫЙ выбор размера: на блоке N-1 пробуем НЕСКОЛЬКО
     входных размеров (не только заданный) в направлении A->B и ищем
     локальный оптимум прибыли (грубый шаг), чтобы показать, что наш
     код способен САМ найти прибыльный размер, а не только повторить
     заданный.

Все eth_call -- ЧТЕНИЕ (не транзакции, не подпись, не отправка).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ВАЖНО: до импорта alchemy_fallback/config -- см. task5_true_arbitrageur_scan.py
# за тем же самым честным пробросом переменной окружения.
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import _rpc_call  # noqa: E402

from task5_v4_pool_math import (  # noqa: E402
    PoolKey, decode_quote_result, decode_v4_swap_log_data, pool_id,
    quote_exact_input_single_calldata,
)

V4_QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
MOSIAI = "0xfb6d1a1860277c1399b3141f8b12a1b77257e57a"
USDG_DECIMALS = 6

POOL_A = PoolKey(USDG, MOSIAI, 70000, 4)  # id 0xcb90948a...
POOL_B = PoolKey(USDG, MOSIAI, 70000, 3)  # id 0x92ed62e7...

CONTROL_TX = "0xd487122244e6c89a0c7b91d48111720575294a6cfeceedfaa3cf105b1fb996d0"
CONTROL_TX_BLOCK = 61248737
CONTROL_INPUT_USDG_RAW = 9437184  # 9.437184 USDG
CONTROL_EXPECT_N1_OUTPUT_RAW = 10242566  # заявлено владельцем
CONTROL_EXPECT_N10_OUTPUT_RAW = 8953460  # 9437184 - 483724


def quote_exact_input_single(key: PoolKey, zero_for_one: bool, amount_in: int, block_number: int) -> int:
    """РЕАЛЬНЫЙ eth_call к V4Quoter через наш доверенный RPC-путь
    (alchemy_fallback._rpc_call -- публичный RPC первым, Alchemy
    фолбэком; НЕ домены из приложенного аудита)."""
    calldata = quote_exact_input_single_calldata(key, zero_for_one, amount_in)
    raw = _rpc_call("eth_call", [{"to": V4_QUOTER, "data": calldata}, hex(block_number)])
    amount_out, _gas_estimate = decode_quote_result(raw)
    return amount_out


def route_a_then_b(amount_in_usdg_raw: int, block_number: int) -> dict:
    """USDG -> MOSIAI (pool A, zeroForOne=true) -> USDG (pool B, zeroForOne=false)."""
    mosiai_out = quote_exact_input_single(POOL_A, True, amount_in_usdg_raw, block_number)
    usdg_out = quote_exact_input_single(POOL_B, False, mosiai_out, block_number)
    return {"leg1_mosiai_out": mosiai_out, "leg2_usdg_out": usdg_out,
            "gross_usdg_raw": usdg_out - amount_in_usdg_raw,
            "gross_usdg": (usdg_out - amount_in_usdg_raw) / 10 ** USDG_DECIMALS}


def route_b_then_a(amount_in_usdg_raw: int, block_number: int) -> dict:
    """Реверс: USDG -> MOSIAI (pool B) -> USDG (pool A) -- проверка
    самостоятельного выбора направления."""
    mosiai_out = quote_exact_input_single(POOL_B, True, amount_in_usdg_raw, block_number)
    usdg_out = quote_exact_input_single(POOL_A, False, mosiai_out, block_number)
    return {"leg1_mosiai_out": mosiai_out, "leg2_usdg_out": usdg_out,
            "gross_usdg_raw": usdg_out - amount_in_usdg_raw,
            "gross_usdg": (usdg_out - amount_in_usdg_raw) / 10 ** USDG_DECIMALS}


def verify_pool_ids() -> dict:
    pa, pb = pool_id(POOL_A), pool_id(POOL_B)
    expect_a = "0xcb90948a1ef145614fc4a20418956f7273690abca77804d2a0cf68c7092aea03"
    expect_b = "0x92ed62e7bcdca7aed7e2d17f60621781ae62ff3d80e87dc4702f1cfb17e3534a"
    return {
        "pool_a_computed": pa, "pool_a_expected": expect_a, "pool_a_match": pa.lower() == expect_a.lower(),
        "pool_b_computed": pb, "pool_b_expected": expect_b, "pool_b_match": pb.lower() == expect_b.lower(),
    }


def verify_control_tx_swap_logs() -> dict:
    """Независимо запросить РЕАЛЬНЫЙ receipt контрольной tx и разложить
    её собственные PoolManager.Swap-события (не полагаясь на кэш из
    приложенного аудита) -- сверить amount0/amount1 обеих ног против
    заявленных 9.437184 / 33799.343... / 10.242566."""
    from alchemy_fallback import UNISWAP_V4_SWAP_SIG, topic0
    v4_topic0 = topic0(UNISWAP_V4_SWAP_SIG)
    receipt = _rpc_call("eth_getTransactionReceipt", [CONTROL_TX])
    if receipt is None:
        return {"error": "receipt не получен (RPC вернул null)"}
    swap_logs = []
    for log in receipt.get("logs", []):
        topics = log.get("topics") or []
        if topics and topics[0].lower() == v4_topic0.lower():
            pid = topics[1]
            decoded = decode_v4_swap_log_data(log["data"])
            swap_logs.append({"pool_id": pid, **decoded})
    return {"n_v4_swap_logs": len(swap_logs), "swaps": swap_logs, "block": receipt.get("blockNumber")}


def size_scan_a_then_b(block_number: int, sizes_usdg_raw: list[int]) -> list[dict]:
    out = []
    for amt in sizes_usdg_raw:
        r = route_a_then_b(amt, block_number)
        out.append({"amount_in_usdg_raw": amt, "amount_in_usdg": amt / 10 ** USDG_DECIMALS, **r})
    return out


def main() -> None:
    result: dict = {"control_tx": CONTROL_TX, "control_tx_block": CONTROL_TX_BLOCK}

    print("[v4_quote_replay] шаг 0: сверка PoolKey <-> PoolId (собственный пересчёт, не из аудита)")
    result["pool_id_check"] = verify_pool_ids()
    print(json.dumps(result["pool_id_check"], indent=2))
    if not (result["pool_id_check"]["pool_a_match"] and result["pool_id_check"]["pool_b_match"]):
        print("[v4_quote_replay] ОСТАНОВКА: PoolId не совпал -- дальнейшие quote бессмысленны", file=sys.stderr)
        _save(result)
        sys.exit(1)

    print("[v4_quote_replay] шаг 1: независимая проверка Swap-логов самой контрольной tx (реальный eth_getTransactionReceipt)")
    try:
        result["control_tx_own_swap_logs"] = verify_control_tx_swap_logs()
        print(json.dumps(result["control_tx_own_swap_logs"], indent=2, default=str))
    except Exception as exc:  # noqa: BLE001
        result["control_tx_own_swap_logs"] = {"error": str(exc)}
        print(f"[v4_quote_replay] ошибка получения receipt: {exc}", file=sys.stderr)

    print("[v4_quote_replay] шаг 2: N-1 (блок 61248736) -- воспроизвести заданный размер 9.437184 USDG")
    try:
        n1 = route_a_then_b(CONTROL_INPUT_USDG_RAW, CONTROL_TX_BLOCK - 1)
        n1_match = abs(n1["leg2_usdg_out"] - CONTROL_EXPECT_N1_OUTPUT_RAW) <= 1  # округление до wei допустимо
        result["n_minus_1"] = {"block": CONTROL_TX_BLOCK - 1, **n1,
                                "expected_output_raw": CONTROL_EXPECT_N1_OUTPUT_RAW,
                                "matches_control": n1_match}
        print(json.dumps(result["n_minus_1"], indent=2))
    except Exception as exc:  # noqa: BLE001
        result["n_minus_1"] = {"error": str(exc)}
        print(f"[v4_quote_replay] ошибка quote на N-1: {exc}", file=sys.stderr)

    print("[v4_quote_replay] шаг 3: N-10 (блок 61248727) -- тот же размер, контрольный отрицательный случай")
    try:
        n10 = route_a_then_b(CONTROL_INPUT_USDG_RAW, CONTROL_TX_BLOCK - 10)
        n10_match = abs(n10["leg2_usdg_out"] - CONTROL_EXPECT_N10_OUTPUT_RAW) <= 1
        result["n_minus_10"] = {"block": CONTROL_TX_BLOCK - 10, **n10,
                                 "expected_output_raw": CONTROL_EXPECT_N10_OUTPUT_RAW,
                                 "matches_control": n10_match}
        print(json.dumps(result["n_minus_10"], indent=2))
    except Exception as exc:  # noqa: BLE001
        result["n_minus_10"] = {"error": str(exc)}
        print(f"[v4_quote_replay] ошибка quote на N-10: {exc}", file=sys.stderr)

    print("[v4_quote_replay] шаг 4: самостоятельный выбор НАПРАВЛЕНИЯ на N-1 (реверс: сперва pool B)")
    try:
        rev = route_b_then_a(CONTROL_INPUT_USDG_RAW, CONTROL_TX_BLOCK - 1)
        result["n_minus_1_reversed_direction"] = rev
        print(json.dumps(rev, indent=2))
    except Exception as exc:  # noqa: BLE001
        result["n_minus_1_reversed_direction"] = {"error": str(exc)}
        print(f"[v4_quote_replay] ошибка реверс-направления: {exc}", file=sys.stderr)

    print("[v4_quote_replay] шаг 5: самостоятельный выбор РАЗМЕРА на N-1 (грубый скан)")
    sizes = [1_000_000, 3_000_000, 5_000_000, 7_000_000, 9_437_184, 12_000_000, 15_000_000, 20_000_000, 30_000_000]
    try:
        scan = size_scan_a_then_b(CONTROL_TX_BLOCK - 1, sizes)
        result["n_minus_1_size_scan"] = scan
        best = max(scan, key=lambda r: r["gross_usdg_raw"])
        result["n_minus_1_size_scan_best"] = best
        print(json.dumps(scan, indent=2))
        print("лучший размер из скана:", json.dumps(best, indent=2))
    except Exception as exc:  # noqa: BLE001
        result["n_minus_1_size_scan"] = {"error": str(exc)}
        print(f"[v4_quote_replay] ошибка size-скана: {exc}", file=sys.stderr)

    out_path = Path(__file__).parent.parent / "data" / "task5_v4_quote_replay_result.json"
    _save(result, out_path)
    print(f"[v4_quote_replay] сохранено: {out_path}")


def _save(result: dict, path: Path | None = None) -> None:
    if path is None:
        path = Path(__file__).parent.parent / "data" / "task5_v4_quote_replay_result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
