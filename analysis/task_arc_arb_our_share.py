#!/usr/bin/env python3
"""Владелец (2026-09-17): постановка "сколько зарабатывают арбитражники
на всей цепи" снимается -- она не отвечает на реальный вопрос. Меряем
ДВА своих параметра: (А) остаются ли вообще незакрытые ценовые
расхождения между парами пулов одного токена, и (Б) успеваем ли мы
физически среагировать быстрее блок-тайма (506мс), с НАШЕГО сервера NL,
без реальных денег и без PRIVATE_KEY_NOX.

=== Часть А: незакрытые возможности ===
Берём до 10 токенов, у каждого из которых ≥2 разных pool_id против USDC
(разные pool_id для той же пары currency0/currency1 ГАРАНТИРОВАННО
отличаются fee/tickSpacing/hooks -- pool_id это keccak-хэш этих полей,
см. PoolManager.sol -- значит любые 2 разных pool_id одного токена это
уже "разные пулы", ничего не нужно перепроверять отдельно).
Источник пар -- уже собранный на этом же VPS реестр Initialize-событий
(task_arc_recon_pools_result.json, 24567 записей), если он ещё лежит на
диске; иначе -- честный ограниченный live-скан (eth_getLogs), с явной
пометкой, что покрытие частичное.

Для каждого выбранного пула читаем slot0 (sqrtPriceX96 + lpFee, ОДНИМ
extsload(bytes32,uint256 nSlots=1) -- feeGrowthGlobal здесь не нужен, нас
интересует только цена и торговая комиссия свопа, не заработок LP) НА
КАЖДОМ блоке часового окна. Чтобы не разориться на количестве вызовов
(N_блоков x N_пулов по одному вызову каждый было бы на порядок дороже
бюджета одного job'а) -- пробуем JSON-RPC batching (отправка МАССИВА
запросов одним HTTP POST, стандартная часть спецификации JSON-RPC 2.0,
не Solidity-контракт Multicall -- проще и без риска ABI-ошибки). Если
нода не поддерживает batching -- честно уменьшаем охват (меньше пар
и/или короче окно) и называем цену полного охвата.

Возможность = |цена_A - цена_B| / min(цена_A,цена_B) минус (fee_A+fee_B)
в pips, умноженное на референсный размер позиции ($200, тот же ориентир,
что и в остальной части сессии) минус РЕАЛЬНАЯ стоимость газа (текущий
eth_gasPrice x медианный реальный gasUsed 2-плечевого цикла из уже
измеренных 5 реальных tx, task_arc_arb_profit_gas_reconcile_result.json)
-- если положительно, это незакрытая возможность. Лайфтайм считается в
последовательных блоках нашей per-block сетки, пока эта разница остаётся
положительной для той же пары.

ЧЕСТНАЯ ОГОВОРКА МЕТОДА: это first-order оценка (относительный разрыв
цены минус относительные комиссии, без учёта price impact исполняющего
свопа) при референсном размере $200, предполагающая размер пренебрежимо
мал относительно ликвидности пулов -- не оптимизация исполняемого
размера. Проверка топологического замыкания цикла здесь не нужна (сами
пулы, не чужие транзакции).

=== Часть Б: успеваем ли мы физически ===
С НАШЕГО сервера NL, без реальных денег:
  1. Детекция нового блока (поллинг eth_blockNumber) -> чтение
     реального состояния пула (extsload) -> локальный расчёт (чистый
     Python, без сети) -> подпись ТЕСТОВЫМ (свежесгенерированным)
     ключом, НЕ PRIVATE_KEY_NOX. Медиана/p90 по нескольким реальным
     блокам.
  2. Отдельно: реальная invalid-nonce транзакция (гарантированно не
     исполнится, комиссий не будет) через eth_sendRawTransaction --
     чистое время доставки до ответа узла."""
from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

import requests
from Crypto.Hash import keccak
from eth_account import Account
from eth_utils import to_checksum_address

RPC = "https://rpc.mainnet.arc.io"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
POOLS_SLOT = 6
USDC_ERC20 = "0x3600000000000000000000000000000000000000"
USDC_DECIMALS = 6
ARC_CHAIN_ID = 5042
BLOCK_TIME_S = 0.506
WINDOW_HOURS = 1.0
MAX_PAIRS = 10
REFERENCE_NOTIONAL_USDC = 200.0
# Реальные gasUsed 5 подтверждённых 2-плечевых циклов
# (task_arc_arb_profit_gas_reconcile_result.json) -- медиана, не выдумано.
REAL_CYCLE_GAS_USED_SAMPLES = [156864, 134544, 156257, 149502, 133485]
N_LATENCY_SAMPLES_BLOCK_PATH = 15
N_LATENCY_SAMPLES_SEND_PATH = 10

REPO_ROOT_CANDIDATES = [Path("/home/bot/robinhood-chain-alpha"), Path(__file__).parent.parent]
MIN_CALL_INTERVAL_S = 0.05
_last_call_ts = [0.0]
_rpc_calls = [0]
TIME_BUDGET_S = 1500.0  # общий потолок части А, оставляет запас на часть Б и job overhead в 30-минутном лимите


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
    """JSON-RPC 2.0 batch -- один HTTP POST с массивом запросов. Возвращает
    список тел ответов В ТОМ ЖЕ ПОРЯДКЕ, что requests_list (сопоставлено
    по id), или None если batching в принципе не сработал (транспортная
    ошибка/не-массив в ответе) -- вызывающий код тогда обязан честно
    упасть на sequential-фолбэк, не притворяться, что данные есть."""
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


def extsload_calldata_1slot(slot_int: int) -> str:
    selector = keccak256(b"extsload(bytes32,uint256)")[:4].hex()
    return "0x" + selector + slot_int.to_bytes(32, "big").hex() + (1).to_bytes(32, "big").hex()


def decode_slot0_result(raw_hex: str | None) -> dict | None:
    if not raw_hex or raw_hex == "0x":
        return None
    data = bytes.fromhex(raw_hex[2:])
    if len(data) < 96:
        return None
    word0 = int.from_bytes(data[64:96], "big")
    return {
        "sqrt_price_x96": word0 & ((1 << 160) - 1),
        "lp_fee_pips": (word0 >> 208) & ((1 << 24) - 1),
    }


def usdc_per_token_human(sqrt_price_x96: int, usdc_is_currency0: bool, token_decimals: int) -> float | None:
    if sqrt_price_x96 == 0:
        return None
    sqrt_p = sqrt_price_x96 / (2 ** 96)
    raw_price = sqrt_p * sqrt_p  # raw token1_per_token0
    if usdc_is_currency0:
        if raw_price == 0:
            return None
        return (10 ** (token_decimals - USDC_DECIMALS)) / raw_price
    return raw_price * (10 ** (token_decimals - USDC_DECIMALS))


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
    """Пары pool_id против USDC-ERC20 identity (0x3600...), сгруппированные
    по не-USDC токену -- источник: уже собранный на ЭТОМ ЖЕ VPS реестр
    Initialize-событий, если файл ещё лежит на диске (создан 2026-09-16,
    тот же постоянный сервер). Живой фолбэк-скан НЕ реализован в этой
    версии -- если файла нет, честно возвращаем пусто и причину, вызывающий
    код обязан прервать часть А с этой причиной, а не выдумывать пары."""
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
            meta["n_tokens_with_multiple_usdc_pools"] = len(multi)
            return [{"token": tok, "pools": rows[:2]} for tok, rows in list(multi.items())[:MAX_PAIRS]], meta
    meta["error"] = "task_arc_recon_pools_result.json не найден ни в data/, ни на постоянном пути VPS -- live-фолбэк-скан не реализован в этой версии"
    return [], meta


def part_a_opportunities(data_dir: Path) -> dict:
    out: dict = {}
    pairs, reg_meta = load_registry_pairs(data_dir)
    out["registry_meta"] = reg_meta
    if not pairs:
        out["STOPPED"] = reg_meta.get("error", "нет доступных пар")
        return out
    out["n_pairs_selected"] = len(pairs)
    out["pairs"] = [{"token": p["token"], "pool_ids": [x["pool_id"] for x in p["pools"]]} for p in pairs]

    dec_cache: dict = {}
    for p in pairs:
        p["token_decimals"] = get_token_decimals(p["token"], dec_cache)
        for pool in p["pools"]:
            pool["state_slot_int"] = state_slot_int(pool["pool_id"])

    batching_ok = probe_batch_support()
    out["batching_supported"] = batching_ok

    latest_body = rpc("eth_blockNumber", [])
    latest = int(latest_body.get("result", "0x0"), 16) if not latest_body.get("error") else None
    out["latest_block"] = latest
    if latest is None:
        out["STOPPED"] = "не удалось получить latest_block"
        return out

    gas_price_body = rpc("eth_gasPrice", [])
    real_gas_price_wei = int(gas_price_body.get("result", "0x0"), 16) if not gas_price_body.get("error") else None
    median_gas_used = statistics.median(REAL_CYCLE_GAS_USED_SAMPLES)
    gas_cost_usdc = (real_gas_price_wei * median_gas_used / 1e18) if real_gas_price_wei else None
    out["gas_assumption"] = {
        "real_eth_gasPrice_wei": real_gas_price_wei,
        "real_median_gas_used_2leg_cycle": median_gas_used,
        "gas_cost_usdc_per_arb_tx": gas_cost_usdc,
        "source": "eth_gasPrice текущий + медиана 5 реальных gasUsed из task_arc_arb_profit_gas_reconcile_result.json",
    }
    if gas_cost_usdc is None:
        out["STOPPED"] = "не удалось получить eth_gasPrice"
        return out

    window_blocks = int(WINDOW_HOURS * 3600 / BLOCK_TIME_S)
    from_block = latest - window_blocks
    out["window"] = {"from_block": from_block, "to_block": latest, "n_blocks_requested": window_blocks}

    all_pools = [(p["token"], i, pool) for p in pairs for i, pool in enumerate(p["pools"])]

    episodes_by_pair: dict = {p["token"]: {"open": None, "closed": []} for p in pairs}
    n_blocks_scanned = 0
    start_ts = time.time()
    stopped_reason = None

    block = from_block
    while block <= latest:
        if time.time() - start_ts > TIME_BUDGET_S:
            stopped_reason = f"часовой бюджет job'а ({TIME_BUDGET_S}с) исчерпан частью А -- честно останавливаемся, не досчитываем"
            break
        block_hex = hex(block)
        results_by_pool_id: dict[str, dict | None] = {}
        if batching_ok:
            reqs = [("eth_call", [{"to": POOL_MANAGER, "data": extsload_calldata_1slot(pool["state_slot_int"])}, block_hex])
                    for _, _, pool in all_pools]
            batch_res = rpc_batch(reqs)
            if batch_res is None:
                batching_ok = False
                out["batching_failed_mid_run_at_block"] = block
            else:
                for (_, _, pool), r in zip(all_pools, batch_res):
                    results_by_pool_id[pool["pool_id"]] = decode_slot0_result(r.get("result")) if "error" not in r else None
        if not batching_ok:
            for _, _, pool in all_pools:
                body = rpc("eth_call", [{"to": POOL_MANAGER, "data": extsload_calldata_1slot(pool["state_slot_int"])}, block_hex])
                results_by_pool_id[pool["pool_id"]] = decode_slot0_result(body.get("result")) if "error" not in body else None

        for p in pairs:
            tok = p["token"]
            pool_a, pool_b = p["pools"]
            sa, sb = results_by_pool_id.get(pool_a["pool_id"]), results_by_pool_id.get(pool_b["pool_id"])
            edge_usdc = None
            if sa and sb:
                price_a = usdc_per_token_human(sa["sqrt_price_x96"], pool_a["usdc_is_currency0"], p["token_decimals"])
                price_b = usdc_per_token_human(sb["sqrt_price_x96"], pool_b["usdc_is_currency0"], p["token_decimals"])
                if price_a and price_b and price_a > 0 and price_b > 0:
                    gap_frac = abs(price_a - price_b) / min(price_a, price_b)
                    combined_fee_frac = (sa["lp_fee_pips"] + sb["lp_fee_pips"]) / 1e6
                    net_edge_frac = gap_frac - combined_fee_frac
                    if net_edge_frac > 0:
                        gross_edge_usdc = net_edge_frac * REFERENCE_NOTIONAL_USDC
                        edge_usdc = gross_edge_usdc - gas_cost_usdc

            ep = episodes_by_pair[tok]
            is_opportunity = edge_usdc is not None and edge_usdc > 0
            if is_opportunity:
                if ep["open"] is None:
                    ep["open"] = {"start_block": block, "size_usdc_at_start": edge_usdc, "n_blocks": 1, "peak_usdc": edge_usdc}
                else:
                    ep["open"]["n_blocks"] += 1
                    ep["open"]["peak_usdc"] = max(ep["open"]["peak_usdc"], edge_usdc)
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
            ep["open"]["note"] = "ещё не закрылась на конец окна -- урезана окном, не реальным исчезновением"
            ep["closed"].append(ep["open"])
            ep["open"] = None

    all_episodes = [
        {**ep, "token": tok} for tok, e in episodes_by_pair.items() for ep in e["closed"]
    ]
    out["n_blocks_scanned"] = n_blocks_scanned
    if stopped_reason:
        out["partial_coverage_reason"] = stopped_reason
    out["n_opportunities_found"] = len(all_episodes)
    if all_episodes:
        sizes = [e["size_usdc_at_start"] for e in all_episodes]
        lifetimes = [e["n_blocks"] for e in all_episodes]
        out["median_size_usdc"] = statistics.median(sizes)
        out["median_lifetime_blocks"] = statistics.median(lifetimes)
        out["n_lived_more_than_1_block"] = sum(1 for lt in lifetimes if lt > 1)
        out["max_size_usdc"] = max(sizes)
        out["max_lifetime_blocks"] = max(lifetimes)
    else:
        out["median_size_usdc"] = None
        out["median_lifetime_blocks"] = None
        out["n_lived_more_than_1_block"] = 0
    out["episodes_sample_first_20"] = all_episodes[:20]
    out["honest_caveat"] = (
        "First-order оценка: относительный разрыв цены минус относительные комиссии, "
        f"при референсном размере ${REFERENCE_NOTIONAL_USDC:.0f} -- предполагает размер пренебрежимо мал "
        "относительно ликвидности пулов (price impact исполняющего свопа НЕ учтён). "
        "Газ -- реальный текущий eth_gasPrice x медианный реальный gasUsed 5 уже измеренных "
        "2-плечевых циклов, не гипотетическое число."
    )
    return out


def local_route_calc_dummy(sqrt_price_x96: int, lp_fee_pips: int, notional_usdc: float) -> float:
    """Чистый Python, без сети -- имитация 'посчитать маршрут по состоянию
    в памяти': конвертировать notional в токен по текущей цене, применить
    комиссию пула, вернуть ожидаемый выход. Не заявляется как реальный
    роутинг-движок (тот не написан для Arc в этой сессии) -- честно мерит
    только СТОИМОСТЬ арифметики такого масштаба, которая является нижней
    границей для любого реального движка."""
    sqrt_p = sqrt_price_x96 / (2 ** 96)
    price_raw = sqrt_p * sqrt_p
    if price_raw <= 0:
        return 0.0
    amount_in = notional_usdc
    amount_out_before_fee = amount_in / price_raw if price_raw > 0 else 0.0
    fee_frac = lp_fee_pips / 1e6
    return amount_out_before_fee * (1 - fee_frac)


def part_b_latency(data_dir: Path) -> dict:
    out: dict = {}
    test_account = Account.create()
    out["test_wallet_address"] = test_account.address
    out["test_wallet_note"] = "свежесгенерированный одноразовый ключ, НЕ PRIVATE_KEY_NOX, без фондирования"

    # --- 1. Детекция блока -> чтение состояния -> локальный расчёт -> подпись ---
    # Берём один реальный pool_id из реестра для чтения состояния (любой живой).
    reg_path_candidates = [data_dir.joinpath("task_arc_recon_pools_result.json"),
                            Path("/home/bot/robinhood-chain-alpha/data/task_arc_recon_pools_result.json")]
    sample_pool_id = None
    for p in reg_path_candidates:
        if p.exists():
            obj = json.loads(p.read_text())
            pools = obj.get("initialize_events", {}).get("pools", [])
            if pools:
                sample_pool_id = pools[0]["pool_id"]
            break
    if sample_pool_id is None:
        out["STOPPED_part1"] = "нет доступного pool_id для замера чтения состояния"
    else:
        slot_int = state_slot_int(sample_pool_id)
        full_path_ms = []
        for _ in range(N_LATENCY_SAMPLES_BLOCK_PATH):
            t_start = time.time()
            last_seen = int(rpc("eth_blockNumber", []).get("result", "0x0"), 16)
            new_block = last_seen
            while new_block == last_seen:
                new_block = int(rpc("eth_blockNumber", []).get("result", "0x0"), 16)
            t_detected = time.time()

            body = rpc("eth_call", [{"to": POOL_MANAGER, "data": extsload_calldata_1slot(slot_int)}, "latest"])
            slot0 = decode_slot0_result(body.get("result"))
            t_read = time.time()

            if slot0:
                _ = local_route_calc_dummy(slot0["sqrt_price_x96"], slot0["lp_fee_pips"], 200.0)
            t_calc = time.time()

            nonce_for_sig = 0
            tx = {
                "to": to_checksum_address(POOL_MANAGER), "value": 0, "gas": 300000, "gasPrice": 1_000_000_000,
                "nonce": nonce_for_sig, "chainId": ARC_CHAIN_ID, "data": "0x" + "00" * 68,
            }
            Account.sign_transaction(tx, test_account.key)
            t_signed = time.time()

            full_path_ms.append({
                "block_detect_ms": (t_detected - t_start) * 1000,
                "state_read_ms": (t_read - t_detected) * 1000,
                "local_calc_ms": (t_calc - t_read) * 1000,
                "sign_ms": (t_signed - t_calc) * 1000,
                "total_ms": (t_signed - t_start) * 1000,
            })
        out["block_to_ready_to_send"] = {
            "n_samples": len(full_path_ms),
            "samples": full_path_ms,
            "median_total_ms": statistics.median([r["total_ms"] for r in full_path_ms]),
            "p90_total_ms": statistics.quantiles([r["total_ms"] for r in full_path_ms], n=10)[8] if len(full_path_ms) >= 10 else max(r["total_ms"] for r in full_path_ms),
            "median_block_detect_ms": statistics.median([r["block_detect_ms"] for r in full_path_ms]),
            "median_state_read_ms": statistics.median([r["state_read_ms"] for r in full_path_ms]),
            "median_local_calc_ms": statistics.median([r["local_calc_ms"] for r in full_path_ms]),
            "median_sign_ms": statistics.median([r["sign_ms"] for r in full_path_ms]),
        }

    # --- 2. Реальная invalid-nonce транзакция -- время доставки до RPC ---
    send_times_ms = []
    for i in range(N_LATENCY_SAMPLES_SEND_PATH):
        tx = {
            "to": to_checksum_address(POOL_MANAGER), "value": 0, "gas": 21000, "gasPrice": 1_000_000_000,
            "nonce": 999_999_999 - i, "chainId": ARC_CHAIN_ID, "data": "0x",
        }
        signed = Account.sign_transaction(tx, test_account.key)
        raw_hex = signed.raw_transaction.hex() if hasattr(signed, "raw_transaction") else signed.rawTransaction.hex()
        if not raw_hex.startswith("0x"):
            raw_hex = "0x" + raw_hex
        t0 = time.time()
        body = rpc("eth_sendRawTransaction", [raw_hex])
        t1 = time.time()
        send_times_ms.append({"round_trip_ms": (t1 - t0) * 1000, "response_error": body.get("error")})
    rtts = [r["round_trip_ms"] for r in send_times_ms]
    out["invalid_nonce_send_roundtrip"] = {
        "n_samples": len(send_times_ms),
        "samples": send_times_ms,
        "median_ms": statistics.median(rtts),
        "p90_ms": statistics.quantiles(rtts, n=10)[8] if len(rtts) >= 10 else max(rtts),
        "note": "Гарантированно НЕ исполнится (nonce ~10^9), 0 реальных средств потрачено -- измеряет только сетевую/узловую задержку ответа.",
    }
    return out


def main() -> None:
    root = find_repo_root()
    data_dir = root.joinpath("data")
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    result["part_a_unclosed_opportunities"] = part_a_opportunities(data_dir)
    result["part_b_our_latency"] = part_b_latency(data_dir)

    budget_ms = BLOCK_TIME_S * 1000
    pa = result["part_a_unclosed_opportunities"]
    pb = result["part_b_our_latency"]
    has_opportunities_2plus_blocks = (pa.get("n_lived_more_than_1_block") or 0) > 0
    our_path_ms = (pb.get("block_to_ready_to_send") or {}).get("median_total_ms")
    if has_opportunities_2plus_blocks and our_path_ms is not None and our_path_ms < 400:
        verdict = "ЖИВА"
    elif not has_opportunities_2plus_blocks and (pa.get("n_opportunities_found") or 0) == 0:
        verdict = "ЗАКРЫТА (нет возможностей вообще на найденных парах)"
    elif has_opportunities_2plus_blocks and (our_path_ms is None or our_path_ms >= 400):
        verdict = "ВОПРОС ИНФРАСТРУКТУРЫ (возможности есть, путь не укладывается)"
    else:
        verdict = "НЕ РЕШЕНО"
    result["budget_ms"] = budget_ms
    result["preregistered_verdict"] = verdict
    result["total_rpc_calls"] = _rpc_calls[0]
    result["total_rpc_calls_note"] = (
        "Счётчик считает HTTP round trips, не под-запросы внутри batch -- один batched "
        "eth_call на N пулов считается за 1, не за N, потому что это одна реальная сетевая операция."
    )

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    data_dir.joinpath("task_arc_arb_our_share_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
