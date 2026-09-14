#!/usr/bin/env python3
"""Задача владельца: определить время жизни арбитражной возможности
использованной в tx 0xcc630e7d... (блок B=62790344, txIndex=4,
executor 0xed4728d8... -- ЧУЖОЙ контракт, не наш). Разрешён ТОЛЬКО
read-only анализ [B-100, B-1] + первые 4 tx блока B. Лимит: 45 минут
и 300 RPC-вызовов (включая batch-элементы и повторы) -- ОБЯЗАТЕЛЬНО
enforced здесь же, не только на словах. Никакого LIVE/деплоя/покупок/
правки продакшена -- наш контракт и V3-поддержку НЕ трогаем, это чисто
историческая реконструкция ЧУЖОЙ сделки.

Структура (в порядке задачи владельца):
  1. Подтвердить валюты/пулы через PoolKey/Initialize-событие и
     token0()/token1(), не только совпадением сумм.
  2. Проверить доступность историч. состояния/симуляции (архивный
     eth_call, debug_traceCall) -- честно назвать недостающее, если
     точный расчёт невозможен.
  3. Воспроизвести результат НЕПОСРЕДСТВЕННО перед арбитражем --
     калибровка по уже известным из receipt суммам, без двойного
     вычета hook-комиссии.
  4. Прибыльность того же маршрута на 100 блоках, основной размер --
     фактический.
  5. Последнее непрерывное прибыльное окно -- БЕЗ бинарного поиска
     (проверяем подряд, допускаем немонотонность). Порядок значимых tx
     в блоке возникновения + проверка предыдущих tx блока B.
  6. Результат в файл, не только в лог.

ЧЕСТНО: сигнатура Swap-события (amount0/amount1) на хук-пулах этого
маршрута НЕ интерпретируется как прямое "направление" без проверки --
у хука 0xe5e702641e... (HOOK_ETH_MOSIAI) есть флаги
BEFORE/AFTER_SWAP_RETURNS_DELTA (см. task5_v4_hook_route_audit.py
decode_hook_permissions) -- то есть хук МОЖЕТ переопределять
фактические дельты, эмитируемое Swap-событие не обязано побайтово
совпадать с реальными ERC-20 Transfer. Реальное направление берём
ТОЛЬКО из Transfer-логов (уже разобраны в предыдущем ответе), Swap
используем только для sqrtPrice/tick/сверки величины."""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

# --- бюджет: ДОЛЖЕН быть установлен ДО импорта любых модулей, которые
# делают `from alchemy_fallback import _rpc_call` (копирует ссылку на
# момент импорта) -- см. предыдущий честный урок этой же сессии про
# monkeypatch до импорта.
import alchemy_fallback as _af  # noqa: E402

RPC_BUDGET = 300
TIME_BUDGET_S = 45 * 60
START_WALL = time.time()
CALL_LOG: list[dict] = []


class BudgetExceeded(Exception):
    pass


_orig_rpc_call = _af._rpc_call
_orig_trading = _af.rpc_call_trading_path


def _budget_check(method: str) -> None:
    if len(CALL_LOG) >= RPC_BUDGET:
        raise BudgetExceeded(f"RPC_BUDGET={RPC_BUDGET} исчерпан перед вызовом {method} "
                              f"(реально сделано {len(CALL_LOG)})")
    elapsed = time.time() - START_WALL
    if elapsed > TIME_BUDGET_S:
        raise BudgetExceeded(f"TIME_BUDGET={TIME_BUDGET_S}с исчерпан перед вызовом {method} "
                              f"(реально прошло {elapsed:.0f}с)")


def _counted_rpc_call(method, params):
    _budget_check(method)
    t0 = time.time()
    try:
        result = _orig_rpc_call(method, params)
        CALL_LOG.append({"method": method, "t": time.time() - START_WALL, "dt": time.time() - t0, "ok": True})
        return result
    except Exception as exc:  # noqa: BLE001
        CALL_LOG.append({"method": method, "t": time.time() - START_WALL, "dt": time.time() - t0,
                          "ok": False, "error": str(exc)})
        raise


def _counted_trading(method, params):
    _budget_check(method)
    t0 = time.time()
    try:
        result = _orig_trading(method, params)
        CALL_LOG.append({"method": method, "t": time.time() - START_WALL, "dt": time.time() - t0,
                          "ok": True, "lane": "trading"})
        return result
    except Exception as exc:  # noqa: BLE001
        CALL_LOG.append({"method": method, "t": time.time() - START_WALL, "dt": time.time() - t0,
                          "ok": False, "error": str(exc), "lane": "trading"})
        raise


_af._rpc_call = _counted_rpc_call
_af.rpc_call_trading_path = _counted_trading

# теперь можно импортировать всё остальное -- подхватят патченный _rpc_call
from task5_v4_hook_route_audit import fetch_initialize_event, decode_hook_permissions  # noqa: E402
from task5_v4_pool_math import (  # noqa: E402
    PoolKey, pool_id, quote_exact_input_single_calldata, quote_exact_input_multihop_calldata,
    decode_quote_result, decode_v4_swap_log_data,
)

RPC = _af._rpc_call

# --- константы из уже полностью разобранного receipt (предыдущий ответ) ---
TX_HASH = "0xcc630e7d525909dd32453101baf311588813279edb5a97e0d386ea9cc4c2ede3"
B = 62_790_344
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
HOOK = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
TOKEN_X = "0x78b96280c3347e0f58a7147b73eb0ec5ffff025d"
V3_POOL = "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca"
EXECUTOR = "0xed4728d89bd4ef81177d8f5448f9bd1bae4e23e3"
INITIATOR = "0xddbc5dd838f2efeacc6dbd7cd29c0bd805155680"
POOL1_ID = "0x935c1401e40a4eeec0e722f1841cc4be9d7a523cf90cdc52900f9e343304f0fe"
POOL2_ID = "0x0086a03ec05c3115a0bb90e0d00e84b6228ceb518769f205b4d52a3d432a8fa2"

# реально измеренные в receipt величины (raw), ground truth -- НЕ пересчитываются заново
REAL = {
    "leg1_weth_delivered_raw": 593334947664648067,       # log 0x8 IN + log 0x14 OUT (repay) -- то же число
    "leg1_swap_amount1_tokenx_gross_raw": 2195693217737978476107624,  # Swap1 event amount1
    "hook_fee_tokenx_raw": 43913864354759569522152,       # log 0xa, PoolManager->hook
    "tokenx_net_to_executor_raw": 2151779353383218906585472,  # log 0xc == log 0x10
    "usdg_from_leg2_raw": 1589699478,                      # log 0xf == log 0x12
    "leg3_weth_out_raw": 634026043308645301,               # log 0x11
    "net_profit_weth_raw": 40691095643997234,
}
REAL["hook_fee_ratio"] = REAL["hook_fee_tokenx_raw"] / REAL["leg1_swap_amount1_tokenx_gross_raw"]

RESULT: dict = {
    "tx_hash": TX_HASH, "block_B": B, "started_at_utc": time.time(),
    "budget": {"rpc_budget": RPC_BUDGET, "time_budget_s": TIME_BUDGET_S},
    "real_receipt_values": REAL,
    "phase1_pool_identity": {},
    "phase2_feasibility": {},
    "phase3_calibration": {},
    "phase4_100block_scan": {"per_block": [], "coverage_note": None},
    "phase5_origin_search": {},
    "stopped_early": None,
}
OUT_PATH = Path(__file__).parent.parent / "data" / "task5_arb_lifetime_reconstruction_result.json"


def _save():
    RESULT["n_rpc_calls_used"] = len(CALL_LOG)
    RESULT["elapsed_s"] = time.time() - START_WALL
    RESULT["call_log_tail"] = CALL_LOG[-30:]  # не весь лог -- честный хвост для диагностики, не раздувать файл
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(RESULT, indent=2, ensure_ascii=False, default=str))


def eth_call(to: str, data: str, block_tag: str) -> str:
    body = RPC("eth_call", [{"to": to, "data": data}, block_tag])
    if isinstance(body, dict) and body.get("error"):
        raise RuntimeError(str(body["error"]))
    return body if isinstance(body, str) else body.get("result", body)


def eth_call_raw(call_obj: dict, block_tag: str) -> dict:
    """Возвращает {'ok':True,'result':...} или {'ok':False,'error':...} --
    НЕ бросает исключение на revert (это ОЖИДАЕМЫЙ, информативный исход,
    не сбой инструмента)."""
    try:
        body = RPC("eth_call", [call_obj, block_tag])
        return {"ok": True, "result": body}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


# =====================================================================
# ФАЗА 1: подтверждение валют/пулов через Initialize + token0/token1
# =====================================================================
def phase1_pool_identity() -> None:
    out = RESULT["phase1_pool_identity"]
    try:
        tx_obj = RPC("eth_getTransactionByHash", [TX_HASH])
        out["original_tx"] = {
            "from": tx_obj.get("from"), "to": tx_obj.get("to"), "value": tx_obj.get("value"),
            "gas": tx_obj.get("gas"), "input_len_bytes": (len(tx_obj.get("input", "0x")) - 2) // 2,
            "input_selector": (tx_obj.get("input") or "0x")[:10],
        }
        out["original_tx_input_full"] = tx_obj.get("input")
    except Exception as exc:  # noqa: BLE001
        out["original_tx_error"] = str(exc)

    for name, pid in [("pool1", POOL1_ID), ("pool2", POOL2_ID)]:
        try:
            ev = fetch_initialize_event(pid, B)
            out[name] = ev
            if ev:
                out[name]["hook_permissions"] = decode_hook_permissions(ev["hooks"])
        except Exception as exc:  # noqa: BLE001
            out[name] = {"error": str(exc)}

    # V3 pool: token0()/token1()/fee() -- реальные селекторы стандартного IUniswapV3Pool
    try:
        t0 = eth_call(V3_POOL, "0x0dfe1681", "latest")  # token0()
        t1 = eth_call(V3_POOL, "0xd21220a7", "latest")  # token1()
        fee_raw = eth_call(V3_POOL, "0xddca3f43", "latest")  # fee()
        out["v3_pool"] = {
            "token0": "0x" + t0[-40:], "token1": "0x" + t1[-40:], "fee_pips": int(fee_raw, 16),
        }
    except Exception as exc:  # noqa: BLE001
        out["v3_pool_error"] = str(exc)

    _save()


# =====================================================================
# ФАЗА 2: доступность историч. состояния/симуляции
# =====================================================================
def phase2_feasibility() -> None:
    out = RESULT["phase2_feasibility"]
    input_full = RESULT["phase1_pool_identity"].get("original_tx_input_full")

    # 2а. Точная историческая replay-попытка ОРИГИНАЛЬНОГО вызова на
    # state конца блока B-1 (уже подтверждён как точный pre-state в
    # предыдущем ответе -- 4 предыдущих tx блока B пулов не трогали).
    if input_full:
        r = eth_call_raw({"from": INITIATOR, "to": EXECUTOR, "data": input_full}, hex(B - 1))
        out["replay_original_call_at_B_minus_1"] = r
    else:
        out["replay_original_call_at_B_minus_1"] = {"ok": False, "error": "нет input calldata (см. phase1)"}

    # 2б. То же самое на B-100 -- тест ГЛУБИНЫ архивного доступа.
    if input_full:
        r2 = eth_call_raw({"from": INITIATOR, "to": EXECUTOR, "data": input_full}, hex(B - 100))
        out["replay_original_call_at_B_minus_100"] = r2

    # 2в. debug_traceCall -- ожидаемо недоступен (весь неймспейс debug_
    # уже подтверждён отключённым через debug_traceTransaction в
    # предыдущем ответе), но проверяем честно, не гадаем.
    try:
        trace = RPC("debug_traceCall", [{"from": INITIATOR, "to": EXECUTOR, "data": input_full or "0x"},
                                          hex(B - 1), {"tracer": "callTracer"}])
        out["debug_traceCall_available"] = True
        out["debug_traceCall_sample"] = trace
    except Exception as exc:  # noqa: BLE001
        out["debug_traceCall_available"] = False
        out["debug_traceCall_error"] = str(exc)

    # 2г. Архивный eth_call на простую view-функцию (WETH.balanceOf) на
    # B-100 -- независимый, более простой тест глубины архива (не
    # зависит от того, есть ли calldata/от логики executor'а).
    balance_call = "0x70a08231" + EXECUTOR[2:].rjust(64, "0")
    try:
        bal_b100 = eth_call(WETH, balance_call, hex(B - 100))
        bal_b1 = eth_call(WETH, balance_call, hex(B - 1))
        out["weth_balanceOf_executor"] = {
            "at_B_minus_100": int(bal_b100, 16), "at_B_minus_1": int(bal_b1, 16),
        }
    except Exception as exc:  # noqa: BLE001
        out["weth_balanceOf_error"] = str(exc)

    _save()


# =====================================================================
# ФАЗА 3: калибровка -- воспроизвести результат НЕПОСРЕДСТВЕННО перед
# арбитражем (на блоке B-1), сверить с receipt, БЕЗ двойного вычета
# hook-комиссии.
# =====================================================================
def phase3_calibration() -> None:
    out = RESULT["phase3_calibration"]
    pool1_info = RESULT["phase1_pool_identity"].get("pool1") or {}
    pool2_info = RESULT["phase1_pool_identity"].get("pool2") or {}
    v3_info = RESULT["phase1_pool_identity"].get("v3_pool") or {}

    if not (pool1_info.get("currency0") and pool2_info.get("currency0")):
        out["skipped_reason"] = "Initialize-события пулов не получены в фазе 1 -- калибровка невозможна честно"
        _save()
        return

    key1 = PoolKey(pool1_info["currency0"], pool1_info["currency1"], pool1_info["fee"],
                   pool1_info["tick_spacing"], pool1_info["hooks"])
    key2 = PoolKey(pool2_info["currency0"], pool2_info["currency1"], pool2_info["fee"],
                   pool2_info["tick_spacing"], pool2_info["hooks"])
    out["pool_keys_confirmed"] = {"pool1": key1.__dict__, "pool2": key2.__dict__}
    out["pool_id_recompute_check"] = {
        "pool1_matches": pool_id(key1).lower() == POOL1_ID.lower(),
        "pool2_matches": pool_id(key2).lower() == POOL2_ID.lower(),
    }

    # V4 multihop: exact_currency = TOKEN_X net, реально дошедший до
    # executor (0xc), путь TOKEN_X -> WETH (в pool1, обратное направление
    # относительно currency0/1 квотер выводит сам) -- ОДИН eth_call на
    # обе V4-ноги СРАЗУ (leg1 обратный + leg2). Но у нас направление
    # цикла: TOKEN_X (после hook-комиссии) должен идти В pool2 (->USDG),
    # НЕ обратно в pool1 -- левое плечо (pool1) в этом цикле выдаёт
    # TOKEN_X, не потребляет его. Поэтому multihop-путь здесь НЕ
    # применим один-к-одному (multihop квотирует ПОСЛЕДОВАТЕЛЬНЫЙ путь
    # через одни и те же валюты по цепочке, а наш реальный маршрут --
    # pool1 отдаёт TOKEN_X, pool2 принимает TOKEN_X отдаёт USDG -- это
    # РОВНО последовательный путь WETH->TOKEN_X->USDG, если pool1
    # реально котируется в направлении WETH(in)->TOKEN_X(out)).
    # Проверяем ИМЕННО это -- exact_currency=WETH, path=[TOKEN_X (pool1
    # params), USDG (pool2 params)], exact_amount = REAL leg1 WETH
    # (репай), сравниваем результат с REAL tokenx_net_to_executor и
    # REAL usdg_from_leg2 (последний элемент multihop даёт только
    # финальный amountOut, не промежуточные -- поэтому даём МЕНЬШЕ
    # диагностики, чем раздельные вызовы; пробуем оба варианта).
    try:
        legs = [
            (TOKEN_X, key1.fee, key1.tick_spacing, key1.hooks),
            (USDG, key2.fee, key2.tick_spacing, key2.hooks),
        ]
        calldata = quote_exact_input_multihop_calldata(WETH, legs, REAL["leg1_weth_delivered_raw"])
        raw = eth_call("0x8dc178efb8111bb0973dd9d722ebeff267c98f94", calldata, hex(B - 1))
        amount_out, gas_est = decode_quote_result(raw)
        out["v4_multihop_quote_weth_to_usdg"] = {
            "input_weth_raw": REAL["leg1_weth_delivered_raw"],
            "quoted_usdg_out_raw": amount_out,
            "real_usdg_from_leg2_raw": REAL["usdg_from_leg2_raw"],
            "diff": amount_out - REAL["usdg_from_leg2_raw"],
            "diff_pct": (amount_out - REAL["usdg_from_leg2_raw"]) / REAL["usdg_from_leg2_raw"] * 100
                        if REAL["usdg_from_leg2_raw"] else None,
            "note": "Это ПРЯМОЕ направление (WETH->TOKEN_X->USDG), не то, что реально исполнилось "
                    "(там WETH был ВЫХОДОМ leg1, не входом) -- квотер этого направления НЕ обязан "
                    "совпасть с реальными числами; это тест РАБОТОСПОСОБНОСТИ квотера на этих пулах, "
                    "не прямая калибровка исполненной сделки.",
        }
    except Exception as exc:  # noqa: BLE001
        out["v4_multihop_quote_error"] = str(exc)

    # Прямая калибровка leg1 в РЕАЛЬНО исполненном направлении:
    # TOKEN_X(currency1, in) -> WETH(currency0, out), exact input =
    # REAL gross TOKEN_X (то, что Swap-событие показало как amount1,
    # ДО вычета hook-комиссии) -- если квотер вернёт ВАЛОВУЮ цифру
    # (как уже задокументировано для другого хук-пула в
    # task5_v4_hook_quote_check.py), сравниваем с REAL WETH ГРУБО;
    # если хук режет НА ВХОДЕ, а не на выходе -- пробуем НЕТТО TOKEN_X.
    try:
        zero_for_one_leg1 = int(key1.currency0, 16) < int(TOKEN_X, 16)  # True если currency0==WETH (вход не TOKEN_X)
        # реальное направление: TOKEN_X -> WETH, т.е. currency1->currency0, zero_for_one=False
        for label, amount_in in [("gross_pre_hook", REAL["leg1_swap_amount1_tokenx_gross_raw"]),
                                  ("net_post_hook", REAL["tokenx_net_to_executor_raw"])]:
            calldata = quote_exact_input_single_calldata(key1, False, amount_in)
            raw = eth_call("0x8dc178efb8111bb0973dd9d722ebeff267c98f94", calldata, hex(B - 1))
            amount_out, _ = decode_quote_result(raw)
            out.setdefault("v4_single_leg1_tokenx_to_weth", {})[label] = {
                "amount_in_tokenx_raw": amount_in, "quoted_weth_out_raw": amount_out,
                "real_weth_raw": REAL["leg1_weth_delivered_raw"],
                "diff_pct": (amount_out - REAL["leg1_weth_delivered_raw"]) / REAL["leg1_weth_delivered_raw"] * 100,
            }
    except Exception as exc:  # noqa: BLE001
        out["v4_single_leg1_error"] = str(exc)

    # leg2 калибровка: TOKEN_X(net, post-hook) -> USDG, прямая
    # проверка (это НЕ хук-пул судя по Swap1 fee=0 vs Swap2 fee=30970 --
    # проверяем через hook_permissions в phase1, а не гадаем).
    try:
        zero_for_one_leg2 = int(key2.currency0, 16) < int(TOKEN_X, 16)
        calldata = quote_exact_input_single_calldata(key2, not zero_for_one_leg2 if key2.currency1.lower() == TOKEN_X.lower() else zero_for_one_leg2,
                                                       REAL["tokenx_net_to_executor_raw"])
        # прямее: направление TOKEN_X->USDG эквивалентно currency1->currency0 если currency0==USDG
        zfo = not (key2.currency0.lower() == TOKEN_X.lower())
        calldata = quote_exact_input_single_calldata(key2, zfo, REAL["tokenx_net_to_executor_raw"])
        raw = eth_call("0x8dc178efb8111bb0973dd9d722ebeff267c98f94", calldata, hex(B - 1))
        amount_out, _ = decode_quote_result(raw)
        out["v4_single_leg2_tokenx_to_usdg"] = {
            "amount_in_tokenx_raw": REAL["tokenx_net_to_executor_raw"],
            "quoted_usdg_out_raw": amount_out, "real_usdg_raw": REAL["usdg_from_leg2_raw"],
            "diff_pct": (amount_out - REAL["usdg_from_leg2_raw"]) / REAL["usdg_from_leg2_raw"] * 100,
        }
    except Exception as exc:  # noqa: BLE001
        out["v4_single_leg2_error"] = str(exc)

    # leg3 (V3): slot0()+liquidity() на B-1, честная V3-математика в
    # одном тиковом диапазоне (без пересечения тика -- ПРЕДПОЛОЖЕНИЕ,
    # проверяем калибровкой против реального результата), fee из phase1.
    try:
        slot0_raw = eth_call(V3_POOL, "0x3850c7bd", hex(B - 1))
        liq_raw = eth_call(V3_POOL, "0x1a686502", hex(B - 1))  # liquidity()
        sqrt_price_x96 = int(slot0_raw[2:66], 16)
        liquidity = int(liq_raw, 16)
        fee_pips = v3_info.get("fee_pips")
        token0 = v3_info.get("token0", "").lower()
        usdg_is_token0 = token0 == USDG.lower()
        amount_in = REAL["usdg_from_leg2_raw"]
        amount_in_after_fee = amount_in * (1_000_000 - fee_pips) // 1_000_000 if fee_pips is not None else amount_in
        Q96 = 2 ** 96
        if usdg_is_token0:
            # USDG(token0) in -> WETH(token1) out, zeroForOne=True
            sqrt_p_next = (liquidity * sqrt_price_x96) // (liquidity + (amount_in_after_fee * sqrt_price_x96) // Q96)
            amount_out = liquidity * (sqrt_price_x96 - sqrt_p_next) // Q96
        else:
            # USDG(token1) in -> WETH(token0) out, zeroForOne=False
            sqrt_p_next = sqrt_price_x96 + (amount_in_after_fee * Q96) // liquidity
            amount_out = liquidity * Q96 * (sqrt_p_next - sqrt_price_x96) // (sqrt_price_x96 * sqrt_p_next)
        out["v3_leg3_calibration"] = {
            "sqrt_price_x96_at_B_minus_1": sqrt_price_x96, "liquidity_at_B_minus_1": liquidity,
            "fee_pips": fee_pips, "usdg_is_token0": usdg_is_token0,
            "amount_in_usdg_raw": amount_in, "computed_weth_out_raw": amount_out,
            "real_weth_out_raw": REAL["leg3_weth_out_raw"],
            "diff_pct": (amount_out - REAL["leg3_weth_out_raw"]) / REAL["leg3_weth_out_raw"] * 100,
            "method_caveat": "формула для ОДНОГО тикового диапазона (без пересечения тика) -- "
                              "если реальная сделка пересекла границу тика, результат будет неточным; "
                              "проверяется именно расхождением diff_pct с реальным исходом.",
        }
    except Exception as exc:  # noqa: BLE001
        out["v3_leg3_error"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}"

    _save()


def main() -> None:
    try:
        phase1_pool_identity()
        phase2_feasibility()
        phase3_calibration()
    except BudgetExceeded as exc:
        RESULT["stopped_early"] = f"BudgetExceeded во время фаз 1-3: {exc}"
        _save()
        print(json.dumps(RESULT, indent=2, ensure_ascii=False, default=str)[-4000:])
        return
    except Exception as exc:  # noqa: BLE001
        RESULT["stopped_early"] = f"Необработанная ошибка в фазах 1-3: {type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        _save()
        print(json.dumps(RESULT, indent=2, ensure_ascii=False, default=str)[-4000:])
        return

    _save()
    print(f"[phase1-3 done] calls_used={len(CALL_LOG)} elapsed={time.time()-START_WALL:.0f}s")
    print(json.dumps({"phase1": RESULT["phase1_pool_identity"], "phase2": RESULT["phase2_feasibility"],
                       "phase3": RESULT["phase3_calibration"]}, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
