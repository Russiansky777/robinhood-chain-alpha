#!/usr/bin/env python3
"""Владелец (2026-09-17): вердикт ЗАКРЫТА из task_arc_arb_our_share.py
отменён -- предрегистрация "прожила 1 блок = взяли в блоке появления"
была неверной (реально: сделка попадает в блок N, взять её раньше N+1
не может НИКТО -- 1-блочный лайфтайм означает конкуренцию за N+1, не
невозможность исполнения). Реальная проблема была в другом: медианный
разрыв $170 при опорной $200 = 85% расхождения цены -- в живых пулах
так не бывает; prescreen отбраковывал только границы тик-диапазона
(MIN/MAX_SQRT_RATIO), но не проверял ДОСТАТОЧНОСТЬ ликвидности.

Переделано:
  1. Жёсткий отбор пар: TVL >= $10000 на КАЖДОЙ стороне пары (не просто
     liquidity>0) И реальная торговля в течение измеряемого часа
     (feeGrowthGlobal вырос между началом и концом часа хотя бы на
     одном из двух пулов) -- живым eth_call на границах часа, не
     предположение.
  2. Реальный исполняемый размер: при срабатывании первого-порядкового
     сигнала (дёшево, как раньше) -- ОДИН доп. extsload (4 слота) на
     оба пула этого блока, затем численная оптимизация (тернарный
     поиск) размера сделки через РЕАЛЬНУЮ формулу свопа AMM (constant-L
     инвариант sqrtP, тот же принцип, что везде в этой сессии) --
     максимизируем (usdc_out - usdc_in - газ) по размеру. Это даёт
     настоящую исполнимую прибыль, не first-order оценку при
     фиксированных $200.
  3. Сопоставление с реальной стоимостью входа в аукцион приоритета:
     реальные средние приоритетные ставки двух известных ботов (2572 и
     3667 gwei, task_arc_txpool_probe_result.json, question6) переведены
     в доллары на сделку через тот же медианный реальный gasUsed.

ЧЕСТНАЯ ОГОВОРКА МЕТОДА: формула свопа предполагает ПОСТОЯННУЮ ликвидность
в диапазоне сделки (валидно для сделок, не пересекающих границу тика) --
для очень крупных относительно пула сделок реальная ликвидность может
измениться на границе тика, это не учтено (стандартное упрощение,
использованное во всех capacity-расчётах этой сессии)."""
from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

import requests
from Crypto.Hash import keccak

RPC = "https://rpc.mainnet.arc.io"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
POOLS_SLOT = 6
USDC_ERC20 = "0x3600000000000000000000000000000000000000"
USDC_DECIMALS = 6
BLOCK_TIME_S = 0.506
WINDOW_HOURS = 1.0
MAX_PAIRS = 10
MIN_TVL_USDC = 10_000.0
EXEC_SEARCH_UPPER_USDC = 20_000.0  # верхняя граница численного поиска исполняемого размера
# Реальные gasUsed 5 подтверждённых 2-плечевых циклов
# (task_arc_arb_profit_gas_reconcile_result.json) -- медиана, не выдумано.
REAL_CYCLE_GAS_USED_SAMPLES = [156864, 134544, 156257, 149502, 133485]
# Реальные средние приоритетные ставки двух известных ботов
# (task_arc_txpool_probe_result.json, question6_priority_fee) -- не выдумано.
REAL_INCUMBENT_PRIORITY_GWEI = {"bot_a_avg": 2572.4722656000004, "bot_b_avg": 3667.1315734996997}

MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342
BOUNDARY_MARGIN = 10_000

REPO_ROOT_CANDIDATES = [Path("/home/bot/robinhood-chain-alpha"), Path(__file__).parent.parent]
MIN_CALL_INTERVAL_S = 0.05
_last_call_ts = [0.0]
_rpc_calls = [0]
TIME_BUDGET_S = 1000.0
PRESCREEN_BUDGET_S = 300.0


def find_repo_root() -> Path:
    for r in REPO_ROOT_CANDIDATES:
        if r.joinpath("data").exists():
            return r
    return REPO_ROOT_CANDIDATES[-1]


def keccak256(data: bytes) -> bytes:
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def _throttle() -> None:
    wait = MIN_CALL_INTERVAL_S - (time.time() - _last_call_ts[0])
    if wait > 0:
        time.sleep(wait)
    _last_call_ts[0] = time.time()


def rpc(method: str, params: list, timeout: int = 20) -> dict:
    _throttle()
    _rpc_calls[0] += 1
    try:
        resp = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                              headers={"Content-Type": "application/json"}, timeout=timeout)
        body = resp.json()
        body["_http_status"] = resp.status_code
        return body
    except Exception as exc:  # noqa: BLE001
        return {"error": {"message": f"{type(exc).__name__}: {exc}"}, "_http_status": None}


def rpc_batch(requests_list: list[tuple[str, list]], timeout: int = 25) -> list[dict] | None:
    _throttle()
    _rpc_calls[0] += 1
    payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(requests_list)]
    try:
        resp = requests.post(RPC, json=payload, headers={"Content-Type": "application/json"}, timeout=timeout)
        body = resp.json()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(body, list) or len(body) != len(requests_list):
        return None
    by_id = {item.get("id"): item for item in body if isinstance(item, dict)}
    ordered = []
    for i in range(len(requests_list)):
        if i not in by_id:
            return None
        ordered.append(by_id[i])
    return ordered


def probe_batch_support() -> bool:
    res = rpc_batch([("eth_blockNumber", []), ("eth_chainId", [])])
    if res is None:
        return False
    return all("result" in r for r in res)


def state_slot_int(pool_id_hex: str) -> int:
    pool_id_bytes = bytes.fromhex(pool_id_hex[2:])
    return int.from_bytes(keccak256(pool_id_bytes + POOLS_SLOT.to_bytes(32, "big")), "big")


def extsload_calldata_nslots(slot_int: int, n_slots: int) -> str:
    selector = keccak256(b"extsload(bytes32,uint256)")[:4].hex()
    return "0x" + selector + slot_int.to_bytes(32, "big").hex() + n_slots.to_bytes(32, "big").hex()


def is_boundary_price(sqrt_price_x96: int) -> bool:
    return sqrt_price_x96 <= MIN_SQRT_RATIO + BOUNDARY_MARGIN or sqrt_price_x96 >= MAX_SQRT_RATIO - BOUNDARY_MARGIN


def decode_1slot(raw_hex: str | None) -> dict | None:
    if not raw_hex or raw_hex == "0x":
        return None
    data = bytes.fromhex(raw_hex[2:])
    if len(data) < 96:
        return None
    word0 = int.from_bytes(data[64:96], "big")
    sqrt_price_x96 = word0 & ((1 << 160) - 1)
    if is_boundary_price(sqrt_price_x96):
        return None
    return {"sqrt_price_x96": sqrt_price_x96, "lp_fee_pips": (word0 >> 208) & ((1 << 24) - 1)}


def decode_4slots(raw_hex: str | None) -> dict | None:
    if not raw_hex or raw_hex == "0x":
        return None
    data = bytes.fromhex(raw_hex[2:])
    if len(data) < 192:
        return None
    words = [int.from_bytes(data[64 + i * 32: 64 + (i + 1) * 32], "big") for i in range(4)]
    slot0_raw, fg0, fg1, liq_raw = words
    sqrt_price_x96 = slot0_raw & ((1 << 160) - 1)
    return {
        "sqrt_price_x96": sqrt_price_x96,
        "lp_fee_pips": (slot0_raw >> 208) & ((1 << 24) - 1),
        "fee_growth_global0_x128": fg0,
        "fee_growth_global1_x128": fg1,
        "liquidity": liq_raw & ((1 << 128) - 1),
        "is_boundary_price": is_boundary_price(sqrt_price_x96),
    }


def usdc_per_token_human(sqrt_price_x96: int, usdc_is_currency0: bool, token_decimals: int) -> float | None:
    if sqrt_price_x96 == 0:
        return None
    sqrt_p = sqrt_price_x96 / (2 ** 96)
    raw_price = sqrt_p * sqrt_p
    if usdc_is_currency0:
        if raw_price == 0:
            return None
        return (10 ** (token_decimals - USDC_DECIMALS)) / raw_price
    return raw_price * (10 ** (token_decimals - USDC_DECIMALS))


def tvl_usdc_from_state(sqrt_price_x96: int, liquidity: int, usdc_is_currency0: bool) -> float:
    """TVL полного диапазона -- 2x стоимость USDC-стороны (стандартное
    приближение: у full-range позиции обе стороны стоят поровну в USD).
    Не нужны decimals не-USDC токена -- USDC-сторона сама по себе уже
    в известных 6 знаках."""
    sqrt_p = sqrt_price_x96 / (2 ** 96)
    if usdc_is_currency0:
        reserve_usdc_raw = liquidity / sqrt_p
    else:
        reserve_usdc_raw = liquidity * sqrt_p
    return 2.0 * reserve_usdc_raw / (10 ** USDC_DECIMALS)


def get_token_decimals(token: str, cache: dict) -> int:
    token = token.lower()
    if token in cache:
        return cache[token]
    if token == USDC_ERC20.lower():
        cache[token] = USDC_DECIMALS
        return cache[token]
    body = rpc("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])
    result = body.get("result")
    dec = int(result, 16) if result and result != "0x" else 18
    cache[token] = dec
    return dec


def load_registry_pairs(data_dir: Path) -> tuple[list[dict], dict]:
    meta = {"source": None}
    candidates = [
        data_dir.joinpath("task_arc_recon_pools_result.json"),
        Path("/home/bot/robinhood-chain-alpha/data/task_arc_recon_pools_result.json"),
    ]
    for p in candidates:
        if p.exists():
            meta["source"] = str(p)
            obj = json.loads(p.read_text())
            pools = obj.get("initialize_events", {}).get("pools", [])
            meta["n_pools_in_registry"] = len(pools)
            by_token: dict[str, list[dict]] = {}
            for row in pools:
                c0, c1 = row["currency0"].lower(), row["currency1"].lower()
                usdc = USDC_ERC20.lower()
                if c0 == usdc and c1 != usdc:
                    by_token.setdefault(c1, []).append({"pool_id": row["pool_id"], "usdc_is_currency0": True})
                elif c1 == usdc and c0 != usdc:
                    by_token.setdefault(c0, []).append({"pool_id": row["pool_id"], "usdc_is_currency0": False})
            multi = {tok: rows for tok, rows in by_token.items() if len(rows) >= 2}
            meta["n_candidate_tokens_total"] = len(multi)
            return [{"token": tok, "pools": rows[:2]} for tok, rows in multi.items()], meta
    meta["error"] = "task_arc_recon_pools_result.json не найден -- живой фолбэк-скан не реализован"
    return [], meta


# ---------- Реальная формула свопа AMM (constant-L, тот же инвариант sqrtP,
# что везде в сессии), проверена офлайн синтетическими тестами до дисптача ----------

def swap_usdc_to_token(L_raw: float, sqrt_price_x96: float, usdc_is_currency0: bool,
                        token_decimals: int, fee_frac: float, usdc_in_human: float) -> float:
    """sqrt_price_x96 -- РЕАЛЬНОЕ закодированное значение (как из extsload,
    масштаб 2**96) -- масштабируется до фактического отношения ВНУТРИ этой
    функции, тем же способом, что usdc_per_token_human/tvl_usdc_from_state.
    Раньше здесь по ошибке использовалось немасштабированное значение --
    поймано офлайн mock-тестом полного пайплайна ДО реального прогона."""
    sqrt_p = sqrt_price_x96 / (2 ** 96)
    dec0 = USDC_DECIMALS if usdc_is_currency0 else token_decimals
    dec1 = token_decimals if usdc_is_currency0 else USDC_DECIMALS
    if usdc_is_currency0:
        amt_in_after_fee = usdc_in_human * (10 ** dec0) * (1 - fee_frac)
        sqrt_p_new = L_raw / (L_raw / sqrt_p + amt_in_after_fee)
        token_out_raw = L_raw * sqrt_p - L_raw * sqrt_p_new
        return token_out_raw / (10 ** dec1)
    amt_in_after_fee = usdc_in_human * (10 ** dec1) * (1 - fee_frac)
    sqrt_p_new = (L_raw * sqrt_p + amt_in_after_fee) / L_raw
    token_out_raw = L_raw / sqrt_p - L_raw / sqrt_p_new
    return token_out_raw / (10 ** dec0)


def swap_token_to_usdc(L_raw: float, sqrt_price_x96: float, usdc_is_currency0: bool,
                        token_decimals: int, fee_frac: float, token_in_human: float) -> float:
    """См. докстринг swap_usdc_to_token -- та же реальная-масштаб конвенция."""
    sqrt_p = sqrt_price_x96 / (2 ** 96)
    dec0 = USDC_DECIMALS if usdc_is_currency0 else token_decimals
    dec1 = token_decimals if usdc_is_currency0 else USDC_DECIMALS
    if usdc_is_currency0:
        amt_in_after_fee = token_in_human * (10 ** dec1) * (1 - fee_frac)
        sqrt_p_new = sqrt_p + amt_in_after_fee / L_raw
        usdc_out_raw = L_raw * (1 / sqrt_p - 1 / sqrt_p_new)
        return usdc_out_raw / (10 ** dec0)
    amt_in_after_fee = token_in_human * (10 ** dec0) * (1 - fee_frac)
    inv_new = 1 / sqrt_p + amt_in_after_fee / L_raw
    sqrt_p_new = 1 / inv_new
    usdc_out_raw = L_raw * sqrt_p - L_raw * sqrt_p_new
    return usdc_out_raw / (10 ** dec1)


def find_optimal_executable_profit(
    buy_state: dict, sell_state: dict, usdc_is_currency0: bool, token_decimals: int, gas_cost_usdc: float,
    upper_bound: float = EXEC_SEARCH_UPPER_USDC,
) -> dict:
    """Тернарный поиск размера сделки (в USDC), максимизирующего
    (usdc_out - usdc_in - газ) через РЕАЛЬНУЮ формулу свопа (constant-L)
    на дешёвом пуле (buy_state) и дорогом (sell_state). Профиль
    одномодален (растёт, потом падает из-за проскальзывания на обеих
    ногах) -- тернарный поиск безопасен."""
    fee_buy = buy_state["lp_fee_pips"] / 1e6
    fee_sell = sell_state["lp_fee_pips"] / 1e6

    def profit(usdc_in: float) -> float:
        if usdc_in <= 0:
            return 0.0
        token_out = swap_usdc_to_token(buy_state["liquidity"], buy_state["sqrt_price_x96"],
                                        usdc_is_currency0, token_decimals, fee_buy, usdc_in)
        usdc_out = swap_token_to_usdc(sell_state["liquidity"], sell_state["sqrt_price_x96"],
                                       usdc_is_currency0, token_decimals, fee_sell, token_out)
        return usdc_out - usdc_in - gas_cost_usdc

    lo, hi = 0.0, upper_bound
    for _ in range(60):
        m1 = lo + (hi - lo) / 3
        m2 = hi - (hi - lo) / 3
        if profit(m1) < profit(m2):
            lo = m1
        else:
            hi = m2
    best_size = (lo + hi) / 2
    best_profit = profit(best_size)
    hit_upper_bound = best_size > upper_bound * 0.98
    return {"executable_size_usdc": best_size, "executable_profit_usdc": best_profit, "hit_search_upper_bound": hit_upper_bound}


def prescreen_liquid_traded_pairs(all_candidates: list[dict], max_pairs: int,
                                   latest: int, hour_start_block: int) -> tuple[list[dict], dict]:
    """Жёсткий отбор: TVL>=$10000 на ОБЕИХ сторонах пары (реальная
    ликвидность в долларах, не просто >0) И реальная торговля в течение
    измеряемого часа (feeGrowthGlobal вырос между hour_start_block и
    latest хотя бы на одном пуле пары) -- обе проверки живым eth_call,
    не предположение."""
    screen_meta = {"n_candidates_examined": 0, "n_rejected": 0, "rejected_samples": []}
    good: list[dict] = []
    screen_start = time.time()
    for cand in all_candidates:
        if len(good) >= max_pairs:
            break
        if time.time() - screen_start > PRESCREEN_BUDGET_S:
            screen_meta["stopped_reason"] = f"бюджет предотбора ({PRESCREEN_BUDGET_S}с) исчерпан -- берём сколько набралось"
            break
        screen_meta["n_candidates_examined"] += 1
        pool_a, pool_b = cand["pools"]
        ok = True
        reject_reason = None
        states_latest = {}
        states_start = {}
        for pool in (pool_a, pool_b):
            slot_int = state_slot_int(pool["pool_id"])
            body_latest = rpc("eth_call", [{"to": POOL_MANAGER, "data": extsload_calldata_nslots(slot_int, 4)}, "latest"])
            info_latest = decode_4slots(body_latest.get("result")) if "error" not in body_latest else None
            if info_latest is None or info_latest["is_boundary_price"]:
                ok, reject_reason = False, "мёртвый/граничный пул"
                break
            tvl = tvl_usdc_from_state(info_latest["sqrt_price_x96"], info_latest["liquidity"], pool["usdc_is_currency0"])
            if tvl < MIN_TVL_USDC:
                ok, reject_reason = False, f"TVL ${tvl:.0f} < ${MIN_TVL_USDC:.0f}"
                break
            body_start = rpc("eth_call", [{"to": POOL_MANAGER, "data": extsload_calldata_nslots(slot_int, 4)}, hex(hour_start_block)])
            info_start = decode_4slots(body_start.get("result")) if "error" not in body_start else None
            states_latest[pool["pool_id"]] = info_latest
            states_start[pool["pool_id"]] = info_start
        if ok:
            traded = False
            for pool in (pool_a, pool_b):
                il, ist = states_latest.get(pool["pool_id"]), states_start.get(pool["pool_id"])
                if il and ist and (il["fee_growth_global0_x128"] != ist["fee_growth_global0_x128"]
                                    or il["fee_growth_global1_x128"] != ist["fee_growth_global1_x128"]):
                    traded = True
                    break
            if not traded:
                ok, reject_reason = False, "нет реальной торговли в измеряемом часе (feeGrowthGlobal не изменился)"
        if ok:
            cand["_states_latest"] = states_latest
            good.append(cand)
        else:
            screen_meta["n_rejected"] += 1
            if len(screen_meta["rejected_samples"]) < 20:
                screen_meta["rejected_samples"].append({"token": cand["token"], "reason": reject_reason})
    screen_meta["n_qualified"] = len(good)
    return good, screen_meta


def part_a_liquid_pairs(data_dir: Path) -> dict:
    out: dict = {}
    all_candidates, reg_meta = load_registry_pairs(data_dir)
    out["registry_meta"] = reg_meta
    if not all_candidates:
        out["STOPPED"] = reg_meta.get("error", "нет доступных пар")
        return out

    latest_body = rpc("eth_blockNumber", [])
    latest = int(latest_body.get("result", "0x0"), 16) if not latest_body.get("error") else None
    if latest is None:
        out["STOPPED"] = "не удалось получить latest_block"
        return out
    window_blocks = int(WINDOW_HOURS * 3600 / BLOCK_TIME_S)
    from_block = latest - window_blocks
    out["window"] = {"from_block": from_block, "to_block": latest, "n_blocks_requested": window_blocks}

    pairs, screen_meta = prescreen_liquid_traded_pairs(all_candidates, MAX_PAIRS, latest, from_block)
    out["prescreen_meta"] = screen_meta
    out["n_pairs_qualified"] = len(pairs)
    if len(pairs) < 5:
        out["honest_note_few_pairs"] = (
            f"Найдено только {len(pairs)} пар с TVL>=${MIN_TVL_USDC:.0f} на обеих сторонах И реальной торговлей "
            "в измеряемом часе -- это САМО ПО СЕБЕ ответ: на этой цепи почти нет пар, удовлетворяющих обоим "
            "условиям одновременно, независимо от дальнейшего расчёта."
        )
    if not pairs:
        out["STOPPED"] = "0 пар прошли жёсткий отбор (TVL>=$10000 на обеих сторонах И реальная торговля в часе)"
        return out
    out["pairs"] = [{"token": p["token"], "pool_ids": [x["pool_id"] for x in p["pools"]]} for p in pairs]

    dec_cache: dict = {}
    for p in pairs:
        p["token_decimals"] = get_token_decimals(p["token"], dec_cache)
        for pool in p["pools"]:
            pool["state_slot_int"] = state_slot_int(pool["pool_id"])

    batching_ok = probe_batch_support()
    out["batching_supported"] = batching_ok

    gas_price_body = rpc("eth_gasPrice", [])
    real_gas_price_wei = int(gas_price_body.get("result", "0x0"), 16) if not gas_price_body.get("error") else None
    median_gas_used = statistics.median(REAL_CYCLE_GAS_USED_SAMPLES)
    gas_cost_usdc = (real_gas_price_wei * median_gas_used / 1e18) if real_gas_price_wei else None
    out["gas_assumption"] = {
        "real_eth_gasPrice_wei": real_gas_price_wei,
        "real_median_gas_used_2leg_cycle": median_gas_used,
        "gas_cost_usdc_per_arb_tx": gas_cost_usdc,
    }
    if gas_cost_usdc is None:
        out["STOPPED"] = "не удалось получить eth_gasPrice"
        return out

    # Реальная стоимость входа в аукцион приоритета (в $ на сделку) --
    # реальные средние ставки известных ботов, task_arc_txpool_probe_result.json.
    priority_cost_usdc = {}
    for name, gwei in REAL_INCUMBENT_PRIORITY_GWEI.items():
        priority_wei_per_gas = gwei * 1e9
        priority_cost_usdc[name] = priority_wei_per_gas * median_gas_used / 1e18
    out["real_incumbent_priority_cost_usdc_per_tx"] = priority_cost_usdc

    all_pools = [(p["token"], i, pool) for p in pairs for i, pool in enumerate(p["pools"])]
    episodes_by_pair: dict = {p["token"]: {"open": None, "closed": []} for p in pairs}
    n_blocks_scanned = 0
    n_extra_liquidity_reads = 0
    start_ts = time.time()
    stopped_reason = None

    block = from_block
    while block <= latest:
        if time.time() - start_ts > TIME_BUDGET_S:
            stopped_reason = f"часовой бюджет job'а ({TIME_BUDGET_S}с) исчерпан -- честно останавливаемся"
            break
        block_hex = hex(block)
        results_by_pool_id: dict[str, dict | None] = {}
        if batching_ok:
            reqs = [("eth_call", [{"to": POOL_MANAGER, "data": extsload_calldata_nslots(pool["state_slot_int"], 1)}, block_hex])
                    for _, _, pool in all_pools]
            batch_res = rpc_batch(reqs)
            if batch_res is None:
                batching_ok = False
            else:
                for (_, _, pool), r in zip(all_pools, batch_res):
                    results_by_pool_id[pool["pool_id"]] = decode_1slot(r.get("result")) if "error" not in r else None
        if not batching_ok:
            for _, _, pool in all_pools:
                body = rpc("eth_call", [{"to": POOL_MANAGER, "data": extsload_calldata_nslots(pool["state_slot_int"], 1)}, block_hex])
                results_by_pool_id[pool["pool_id"]] = decode_1slot(body.get("result")) if "error" not in body else None

        for p in pairs:
            tok = p["token"]
            pool_a, pool_b = p["pools"]
            sa, sb = results_by_pool_id.get(pool_a["pool_id"]), results_by_pool_id.get(pool_b["pool_id"])
            exec_result = None
            if sa and sb:
                price_a = usdc_per_token_human(sa["sqrt_price_x96"], pool_a["usdc_is_currency0"], p["token_decimals"])
                price_b = usdc_per_token_human(sb["sqrt_price_x96"], pool_b["usdc_is_currency0"], p["token_decimals"])
                if price_a and price_b and price_a > 0 and price_b > 0:
                    gap_frac = abs(price_a - price_b) / min(price_a, price_b)
                    combined_fee_frac = (sa["lp_fee_pips"] + sb["lp_fee_pips"]) / 1e6
                    if gap_frac - combined_fee_frac > 0:
                        # Первого порядка сигнал сработал -- подтверждаем РЕАЛЬНОЙ ликвидностью
                        # и находим настоящий исполняемый размер (доп. чтение, только здесь).
                        cheap_pool, expensive_pool = (pool_a, pool_b) if price_a < price_b else (pool_b, pool_a)
                        slot_cheap, slot_exp = cheap_pool["state_slot_int"], expensive_pool["state_slot_int"]
                        body_cheap = rpc("eth_call", [{"to": POOL_MANAGER, "data": extsload_calldata_nslots(slot_cheap, 4)}, block_hex])
                        body_exp = rpc("eth_call", [{"to": POOL_MANAGER, "data": extsload_calldata_nslots(slot_exp, 4)}, block_hex])
                        n_extra_liquidity_reads += 2
                        state_cheap = decode_4slots(body_cheap.get("result")) if "error" not in body_cheap else None
                        state_exp = decode_4slots(body_exp.get("result")) if "error" not in body_exp else None
                        if state_cheap and state_exp and not state_cheap["is_boundary_price"] and not state_exp["is_boundary_price"]:
                            exec_result = find_optimal_executable_profit(
                                state_cheap, state_exp, cheap_pool["usdc_is_currency0"], p["token_decimals"], gas_cost_usdc,
                            )

            ep = episodes_by_pair[tok]
            is_opportunity = exec_result is not None and exec_result["executable_profit_usdc"] > 0
            if is_opportunity:
                if ep["open"] is None:
                    ep["open"] = {
                        "start_block": block, "size_usdc_at_start": exec_result["executable_size_usdc"],
                        "profit_usdc_at_start": exec_result["executable_profit_usdc"], "n_blocks": 1,
                        "peak_profit_usdc": exec_result["executable_profit_usdc"],
                        "hit_search_upper_bound": exec_result["hit_search_upper_bound"],
                    }
                else:
                    ep["open"]["n_blocks"] += 1
                    ep["open"]["peak_profit_usdc"] = max(ep["open"]["peak_profit_usdc"], exec_result["executable_profit_usdc"])
            else:
                if ep["open"] is not None:
                    ep["open"]["end_block"] = block - 1
                    ep["closed"].append(ep["open"])
                    ep["open"] = None

        n_blocks_scanned += 1
        block += 1

    for p in pairs:
        ep = episodes_by_pair[p["token"]]
        if ep["open"] is not None:
            ep["open"]["end_block"] = block - 1
            ep["open"]["note"] = "ещё не закрылась на конец окна"
            ep["closed"].append(ep["open"])
            ep["open"] = None

    all_episodes = [{**ep, "token": tok} for tok, e in episodes_by_pair.items() for ep in e["closed"]]
    out["n_blocks_scanned"] = n_blocks_scanned
    out["n_extra_liquidity_reads_for_exec_sizing"] = n_extra_liquidity_reads
    if stopped_reason:
        out["partial_coverage_reason"] = stopped_reason
    out["n_opportunities_found"] = len(all_episodes)
    if all_episodes:
        profits = [e["profit_usdc_at_start"] for e in all_episodes]
        sizes = [e["size_usdc_at_start"] for e in all_episodes]
        lifetimes = [e["n_blocks"] for e in all_episodes]
        out["median_executable_profit_usdc"] = statistics.median(profits)
        out["max_executable_profit_usdc"] = max(profits)
        out["median_executable_size_usdc"] = statistics.median(sizes)
        out["median_lifetime_blocks"] = statistics.median(lifetimes)
        out["n_lived_more_than_1_block"] = sum(1 for lt in lifetimes if lt > 1)
        out["frac_profit_over_5usd_after_gas"] = sum(1 for pr in profits if pr > 5.0) / len(profits)
        out["n_hit_search_upper_bound"] = sum(1 for e in all_episodes if e.get("hit_search_upper_bound"))
    else:
        out["median_executable_profit_usdc"] = None
        out["frac_profit_over_5usd_after_gas"] = None
    out["episodes_sample_first_20"] = all_episodes[:20]

    if all_episodes:
        median_profit = out["median_executable_profit_usdc"]
        cost_a, cost_b = priority_cost_usdc["bot_a_avg"], priority_cost_usdc["bot_b_avg"]
        out["comparison_line"] = (
            f"прибыль с возможности ${median_profit:.2f} (медиана) против стоимости приоритета "
            f"${cost_a:.4f} (бот A, {REAL_INCUMBENT_PRIORITY_GWEI['bot_a_avg']:.0f} gwei) / "
            f"${cost_b:.4f} (бот B, {REAL_INCUMBENT_PRIORITY_GWEI['bot_b_avg']:.0f} gwei)"
        )
    out["honest_caveat"] = (
        "Формула свопа предполагает постоянную ликвидность в диапазоне сделки (не пересекает "
        "границу тика) -- стандартное упрощение, использованное во всех capacity-расчётах этой "
        "сессии. Исполняемый размер и прибыль -- результат тернарного поиска по РЕАЛЬНОЙ формуле "
        "AMM (constant-L, sqrtP-инвариант), не first-order оценка при фиксированном notional."
    )
    return out


def main() -> None:
    root = find_repo_root()
    data_dir = root.joinpath("data")
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    result["part_a_liquid_pairs"] = part_a_liquid_pairs(data_dir)
    result["total_rpc_calls"] = _rpc_calls[0]
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    data_dir.joinpath("task_arc_arb_liquid_pairs_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
