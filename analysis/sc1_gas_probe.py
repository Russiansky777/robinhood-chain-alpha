#!/usr/bin/env python3
"""Перепроверка `eth_estimateGas` для запуска NVX (дозапрос владельца,
2026-09-02: "газ будто бы слишком высокий, перепроверь на ошибку, либо
попробуем с меньшим газом") -- бинарный поиск МИНИМАЛЬНО достаточного
газа через повторные `eth_call` с явным `gas`-капом в объекте вызова.

Это БЕСПЛАТНАЯ симуляция (`eth_call`, не транзакция) -- ничего не
подписывается и не отправляется, ключ не требуется и не читается.
Цель: (1) убедиться, что `eth_estimateGas` не завышен багом нашего
кодирования calldata -- если реальный минимум близок к оценке ноды,
оценка честная; (2) дать прямой ответ, пройдёт ли отправка с меньшим
явным лимитом газа, чем вернул `eth_estimateGas`.

Наружу -- только числа (минимальный проходящий газ, оценка ноды,
разница в %), calldata не публикуется отдельно (уже есть в
data/p3_guard_cache/sc1_launcher_dryrun_NVX.json).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pons_v2_common as v2  # noqa: E402
from sc1_launcher import OUR_WALLET, load_catalog, prepare_one, _git_head_sha  # noqa: E402

CACHE_DIR = Path("data/p3_guard_cache")
OUT_PATH = CACHE_DIR / "sc1_gas_probe_NVX.json"


def binary_search_min_gas(calldata: bytes, value_wei: int, low: int, high: int) -> tuple[int, list[dict]]:
    """Находит минимальный `gas` (в [low, high]) на котором `eth_call`
    ещё проходит без ошибки. Предполагает монотонность (больше газа
    -- шансов пройти не меньше), что верно для чистого исчерпания
    газа, но НЕ гарантированно верно, если revert зависит от газа
    каким-то иным путём (не ожидается в этом контракте, честная
    оговорка). `high` ДОЛЖЕН уже быть известно проходящим (напр.
    eth_estimateGas), иначе функция вернёт high без гарантии, что он
    реально минимален."""
    trace = []
    ok_high, err_high = v2.eth_call_would_succeed(OUR_WALLET, calldata, value_wei, high)
    trace.append({"gas": high, "ok": ok_high, "error": err_high})
    if not ok_high:
        # high сам не проходит -- eth_estimateGas разошёлся с eth_call при том же газе,
        # не гадаем дальше, честно докладываем расхождение как есть.
        return high, trace

    lo, hi = low, high
    while hi - lo > max(1000, int(high * 0.001)):  # точность ~0.1% от estimateGas или 1000 gas, что больше
        mid = (lo + hi) // 2
        ok, err = v2.eth_call_would_succeed(OUR_WALLET, calldata, value_wei, mid)
        trace.append({"gas": mid, "ok": ok, "error": err})
        if ok:
            hi = mid
        else:
            lo = mid + 1
    return hi, trace


def run() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NVX")
    args_probe = ap.parse_args()

    catalog = load_catalog()
    if args_probe.symbol not in catalog:
        print(f"[sc1_gas_probe] символ {args_probe.symbol} не найден в каталоге")
        return 1

    # Тот же namespace параметров, что дефолты sc1_launcher.py (дефолтный
    # dry-run прогон NVX) -- pairToken=native ETH, launchConfigId=auto,
    # creatorTaxBps=0, buybackEnabled=False, description="".
    class _Args:
        pair_token = None
        launch_config_id = None
        creator_tax_bps = 0
        buyback_enabled = False
        description = None
        gas_ceiling_usd = 10**9  # не даём prepare_one самой остановиться по потолку -- это отдельная проверка, не запуск

    sha = _git_head_sha()
    report = prepare_one(args_probe.symbol, catalog[args_probe.symbol], _Args(), sha)
    if report.get("abort_reason") and "_calldata_hex" not in report:
        print(f"[sc1_gas_probe] prepare_one остановился ДО построения calldata: {report['abort_reason']}")
        return 1

    calldata = bytes.fromhex(report["_calldata_hex"][2:])
    value_wei = report["launch_fee_wei"]
    node_estimate = report["gas_units_estimated"]

    print(f"[sc1_gas_probe] eth_estimateGas (нода) = {node_estimate}")
    print(f"[sc1_gas_probe] бинарный поиск минимального проходящего газа через eth_call (бесплатно, без отправки)...")

    # Верхняя граница поиска -- оценка ноды +5% (запас на тот случай,
    # если eth_call и eth_estimateGas на этой ноде считают чуть по-разному) --
    # если и она не проходит, честно докладываем расхождение, не подгоняем.
    high = int(node_estimate * 1.05)
    low = 21_000  # минимум для любой EVM-транзакции

    min_gas, trace = binary_search_min_gas(calldata, value_wei, low, high)

    diff_pct = (node_estimate - min_gas) / node_estimate * 100 if node_estimate else float("nan")
    result = {
        "symbol": args_probe.symbol,
        "node_eth_estimate_gas": node_estimate,
        "verified_min_gas_via_eth_call": min_gas,
        "search_high_bound": high,
        "diff_pct_estimate_vs_min": diff_pct,
        "n_probes": len(trace),
        "trace": trace,
        "verdict": (
            "оценка ноды ТОЧНАЯ (расхождение <1%)" if abs(diff_pct) < 1 else
            f"оценка ноды содержит запас ~{diff_pct:.1f}% -- реальный минимум ниже, но это НЕ обязательно "
            "означает, что estimateGas 'завышен ошибочно': ноды обычно закладывают буфер намеренно"
        ),
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str))
    print(f"\n[sc1_gas_probe] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
