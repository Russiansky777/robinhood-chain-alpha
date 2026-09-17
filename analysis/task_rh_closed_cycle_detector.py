#!/usr/bin/env python3
"""Владелец, 2026-09-17: адаптация Arc closed-cycle детектора
(`analysis/task_arc_closed_cycle_detector.py`) на Robinhood Chain --
вопрос владельца: есть ли "треугольные" (>=3 плеча) замкнутые циклы,
какая у них доля от общего числа найденных, экономика (медианная
прибыль) и живучесть в блоках -- дольше ли, чем у двухплечевых.

ОПРЕДЕЛЕНИЕ ЦИКЛА -- то же самое, дословно, что на Arc (см. докстринг
task_arc_closed_cycle_detector.py, предрегистрировано владельцем там):
  1) все Swap одной tx, по logIndex, >=2 плеч (v3 И v4, любая комбинация;
     Uniswap V3-совместимые форки типа Sushiswap/PancakeSwap V3-стиль
     ловятся АВТОМАТИЧЕСКИ -- Swap-топик не фильтруется по адресу/фабрике)
  2) token_in(leg0) == token_out(leg_last) -- цикл-токен
  3) net-flow каждого ПРОМЕЖУТОЧНОГО токена ~0 -- проверяется на РЕАЛЬНЫХ
     Transfer-логах receipt (дорого, только для кандидатов, прошедших
     дешёвый топологический фильтр по событиям), НЕ на событийных
     величинах amount0/amount1 -- тот же урок, что на Arc: величина из
     Swap-события может расходиться с реально settled Transfer на
     хук-пулах.
  4) net-flow цикл-токена > 0
  5) прибыль после газа, в USD: USDG (стейбл на этой цепи, decimals=6)
     напрямую, либо WETH/native (0x000...000) через
     native_usd_price_at_block (WETH-USDG v3 пул), либо implied-цена
     из ноги ТОЙ ЖЕ tx против USDG/WETH, если цикл-токен -- ни то ни
     другое (тот же метод, что на Arc, п.5 её докстринга).

ОТЛИЧИЯ ОТ ARC, ЧЕСТНО:
  - Sign-convention amount0/amount1 НЕ проверялась отдельным новым
    RPC-тестом на этой цепи -- вместо этого используется тот же
    v3/v4-декодер, что уже применялся в этой сессии (`rh_arb_our_share.py`,
    `fomo_short_horizon_wave_and_entry.py`, `task5_pool_map.py`: amount>0
    у v3/v4 Swap = токен ПРИШЁЛ В ПУЛ от трейдера, что и на Arc/канонический
    Uniswap ABI) + перекрёстная сверка на 3 РЕАЛЬНЫХ известных multi-leg
    tx этой цепи (`data/task5_arb_suspect_audit_3tx_result.json`) в
    `sanity_check_known_txs()`.
  - v4 комиссия на КАЖДОЕ плечо берётся из ПОСЛЕДНЕГО слова САМОГО
    Swap-события (реально применённая комиссия за этот конкретный своп),
    а не из Initialize -- обходит проблему динамического флага
    0x800000 (см. паспорт, правило E2: этот флаг -- НЕ ставка, а
    "комиссией управляет хук"; реальная комиссия видна только в событии
    свопа, не в Initialize).
  - PoolCreated(v3)/Initialize(v4) сканируются ПО ВСЕЙ ИСТОРИИ ЦЕПИ,
    адрес-независимо (только topic0) -- ловит любые V3/V4-совместимые
    форки автоматически, без знания их фабрик заранее (Sushiswap/
    PancakeSwap V3-стиль используют тот же топик по ABI).
  - V2-классический стиль (Sushiswap/PancakeSwap V2, own Swap-топик) --
    ТОЛЬКО дешёвая проба на последних блоках (см. `v2_probe`), полный
    пайплайн (PairCreated-скан + token0/token1 через eth_call) НЕ
    строится -- по прямому указанию владельца не тратить на это время,
    если быстро не находится; результат пробы честно указан в отчёте.
  - "Живучесть в блоках" -- Arc-детектор её вообще не считал (там не
    было такого поля). Здесь используется метод из `rh_arb_our_share.py`
    (тот же спот-ценовой edge, slot0 v3 / extsload v4), но НАПРАВЛЕННО
    ПРИМЕНЁННЫЙ к уже найденному реальному маршруту цикла: для каждого
    верифицированного цикла переоцениваем тот же маршрут (тот же порядок
    ног/пулов/направлений, та же комиссия) на блоках
    [cycle_block+1 .. cycle_block+LIFETIME_FORWARD_BLOCKS] -- считаем,
    сколько ПОСЛЕДОВАТЕЛЬНЫХ блоков ПОСЛЕ исполнения та же возможность
    (тот же перекос между теми же пулами) остаётся net-положительной по
    маргинальной (без слиппеджа на размере) спот-цене. lifetime_blocks=0
    значит цикл закрыт СВОЕЙ ЖЕ транзакцией немедленно. Значение,
    достигшее потолка LIFETIME_FORWARD_BLOCKS, помечено как правое
    цензурирование (censored=true) -- честно, не "точное число".
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from Crypto.Hash import keccak  # noqa: E402

from alchemy_fallback import (  # noqa: E402
    rpc_call_trading_path,
    fetch_v3_swap_logs,
    fetch_v4_swap_logs,
    topic0,
    _chunked_get_logs,
    get_block_number,
    UNISWAP_V3_POOL_CREATED_SIG,
    UNISWAP_V4_INITIALIZE_SIG,
)

RPC = rpc_call_trading_path
DATA_DIR = Path(__file__).parent.parent.joinpath("data")
OUT_PATH = DATA_DIR / "task_rh_closed_cycles_result.json"

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"  # тот же адрес, что на Arc (CREATE2, деплой v4 через ту же фабрику)
POOLS_SLOT = 6  # подтверждено этой сессией трижды (rh_arb_our_share.py, fomo_short_horizon_wave_and_entry.py)

USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
USDG_DECIMALS = 6
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
NATIVE = "0x0000000000000000000000000000000000000000"
WETH_DECIMALS = 18
WETH_USDG_POOL_V3 = "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca"

DYNAMIC_FEE_FLAG = 0x800000

# --- Бюджеты по времени (честный самоадаптирующийся скан, GH Actions
# job timeout 35 минут -- см. run_task_rh_closed_cycle_detector.yml) ---
# НАЙДЕНО В ПЕРВОМ РЕАЛЬНОМ ПРОГОНЕ (run 35269631588): v3-скан полной
# истории (0..65,645,405) реально занял 373.98с и нашёл 40,819 v3-пулов
# (сам по себе честно завершился) -- но т.к. v3/v4 делили ОДИН общий
# дедлайн ПОСЛЕДОВАТЕЛЬНО, v4-скан стартовал уже ПОСЛЕ истечения общего
# бюджета и получил n_v4_pools=0 (структурный ноль, не реальный) -- при
# том что v4 даёт БОЛЬШИНСТВО объёма (288,761 из 456,448 событий свопа
# в том же окне). Это увело n_unresolved_pool_skipped до 94,334 из
# 95,958 multi-leg tx и n_topology_candidates до 4 -- НЕ реальный
# результат "циклов нет", а артефакт нулевой v4-карты пулов. Исправлено:
# v3 и v4 теперь получают НЕЗАВИСИМЫЕ бюджеты (не общий дедлайн), плюс
# карта пулов КЭШИРУЕТСЯ на диск (`task_rh_pool_map_cache.json`) --
# повторные прогоны сканируют только НОВЫЕ блоки поверх кэша, не всю
# историю заново.
# v3 теперь почти всегда обслуживается кэшем (см. POOL_MAP_CACHE_PATH --
# первый прогон уже покрыл v3 0..65,645,405 полностью) -- малый бюджет
# на дельту сверх кэша. v4 ещё НИ РАЗУ не проходил полный скан (первый
# прогон получил 0 бюджета структурно, см. комментарий выше) -- отдаём
# ему бОльшую часть.
POOL_DISCOVERY_V3_BUDGET_S = 120.0
POOL_DISCOVERY_V4_BUDGET_S = 600.0
SWAP_SCAN_BUDGET_S = 500.0
RECEIPT_VERIFY_BUDGET_S = 400.0
LIFETIME_BUDGET_S = 150.0
V2_PROBE_BLOCKS = 5000

SWAP_SCAN_STEP_BLOCKS = 2000
MAX_SWAP_SCAN_WINDOW_BLOCKS = 400_000  # мягкий потолок -- честная ВЫБОРКА, не полное покрытие истории

# НАЙДЕНО: multi-leg tx на этой цепи ОЧЕНЬ плотные (95,958 в одних лишь
# 60,000 блоках) -- без потолка приёмка receipt-верификации могла бы
# растянуться на часы. Если топологических кандидатов больше потолка --
# честная равномерная выборка по всему окну (не "первые N"), сохраняя
# репрезентативность распределения по блокам.
MAX_CANDIDATES_TO_VERIFY = 4000

LIFETIME_FORWARD_BLOCKS = 15
MAX_CYCLES_FOR_LIFETIME_CHECK = 50

POOL_MAP_CACHE_PATH = DATA_DIR / "task_rh_pool_map_cache.json"


def word(data_bytes: bytes, i: int) -> bytes:
    return data_bytes[i * 32:(i + 1) * 32]


def to_int_signed(w: bytes) -> int:
    v = int.from_bytes(w, "big")
    return v - 2 ** 256 if v >= 2 ** 255 else v


def to_addr(w: bytes) -> str:
    return "0x" + w[-20:].hex()


def topic_to_addr(t: str) -> str:
    return "0x" + t[-40:]


def keccak256(data: bytes) -> bytes:
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


V3_SLOT0_SELECTOR = "0x" + keccak256(b"slot0()")[:4].hex()
EXTSLOAD_SELECTOR = "0x" + keccak256(b"extsload(bytes32)")[:4].hex()
DECIMALS_SELECTOR = "0x" + keccak256(b"decimals()")[:4].hex()

V3_POOL_CREATED_TOPIC0 = topic0(UNISWAP_V3_POOL_CREATED_SIG)
V4_INITIALIZE_TOPIC0 = topic0(UNISWAP_V4_INITIALIZE_SIG)
TRANSFER_TOPIC0 = topic0("Transfer(address,address,uint256)")
V2_SWAP_SIG = "Swap(address,address,uint256,uint256,uint256,uint256,address)"
V2_SWAP_TOPIC0 = topic0(V2_SWAP_SIG)


# ---------------------------------------------------------------- decode


def decode_v3_pool_created(log: dict) -> dict:
    token0 = topic_to_addr(log["topics"][1]).lower()
    token1 = topic_to_addr(log["topics"][2]).lower()
    fee = int(log["topics"][3], 16)
    data = bytes.fromhex(log["data"][2:])
    pool = to_addr(word(data, 1)).lower()
    return {"pool": pool, "token0": token0, "token1": token1, "fee_pips": fee,
            "block_number": int(log["blockNumber"], 16)}


def decode_v4_initialize(log: dict) -> dict:
    data = bytes.fromhex(log["data"][2:])
    return {
        "pool_id": log["topics"][1].lower(), "currency0": topic_to_addr(log["topics"][2]).lower(),
        "currency1": topic_to_addr(log["topics"][3]).lower(), "fee_pips": int.from_bytes(word(data, 0)[-3:], "big"),
        "hooks": to_addr(word(data, 2)).lower(), "block_number": int(log["blockNumber"], 16),
    }


def decode_v3_swap(log: dict) -> dict:
    data = bytes.fromhex(log["data"][2:])
    return {
        "version": "v3", "pool": log["address"].lower(), "sender": topic_to_addr(log["topics"][1]).lower(),
        "recipient": topic_to_addr(log["topics"][2]).lower(),
        "amount0": to_int_signed(word(data, 0)), "amount1": to_int_signed(word(data, 1)),
        "block_number": int(log["blockNumber"], 16), "log_index": int(log["logIndex"], 16),
        "tx_hash": log["transactionHash"],
    }


def decode_v4_swap(log: dict) -> dict:
    data = bytes.fromhex(log["data"][2:])
    return {
        "version": "v4", "pool_id": log["topics"][1].lower(), "sender": topic_to_addr(log["topics"][2]).lower(),
        "amount0": to_int_signed(word(data, 0)), "amount1": to_int_signed(word(data, 1)),
        "fee_pips_real": int.from_bytes(word(data, 5)[-3:], "big"),
        "block_number": int(log["blockNumber"], 16), "log_index": int(log["logIndex"], 16),
        "tx_hash": log["transactionHash"],
    }


def decode_transfer(log: dict) -> dict | None:
    if len(log.get("topics", [])) < 3:
        return None
    return {"token": log["address"].lower(), "from": topic_to_addr(log["topics"][1]).lower(),
            "to": topic_to_addr(log["topics"][2]).lower(),
            "value": int(log["data"], 16) if log.get("data") and log["data"] != "0x" else 0}


def decode_v3_slot0_price(raw_hex: str | None) -> int | None:
    if not raw_hex or raw_hex == "0x" or len(raw_hex) < 66:
        return None
    return int(raw_hex[2:66], 16)


def decode_v4_slot0_price(raw_hex: str | None) -> int | None:
    if not raw_hex or raw_hex == "0x":
        return None
    data = bytes.fromhex(raw_hex[2:])
    if len(data) < 32:
        return None
    return int.from_bytes(data[0:32], "big") & ((1 << 160) - 1)


def state_slot_int(pool_id_hex: str) -> int:
    return int.from_bytes(keccak256(bytes.fromhex(pool_id_hex[2:]) + POOLS_SLOT.to_bytes(32, "big")), "big")


def price_token1_per_token0(sqrt_price_x96: int, dec0: int, dec1: int) -> float:
    sqrt_p = sqrt_price_x96 / (2 ** 96)
    return (sqrt_p * sqrt_p) * (10 ** (dec0 - dec1))


# ------------------------------------------------------------ RPC helpers


def get_decimals(token: str, cache: dict) -> int:
    t = token.lower()
    if t in cache:
        return cache[t]
    if t == USDG:
        cache[t] = USDG_DECIMALS
        return cache[t]
    if t in (WETH, NATIVE):
        cache[t] = WETH_DECIMALS
        return cache[t]
    try:
        raw = RPC("eth_call", [{"to": t, "data": DECIMALS_SELECTOR}, "latest"])
        dec = int(raw, 16) if raw and raw != "0x" else 18
    except Exception:  # noqa: BLE001
        dec = 18
    cache[t] = dec
    return dec


_native_price_cache: dict[int, float | None] = {}


def native_usd_price_at_block(block_num: int) -> float | None:
    if block_num in _native_price_cache:
        return _native_price_cache[block_num]
    try:
        raw = RPC("eth_call", [{"to": WETH_USDG_POOL_V3, "data": V3_SLOT0_SELECTOR}, hex(block_num)])
        sp = decode_v3_slot0_price(raw)
    except Exception:  # noqa: BLE001
        sp = None
    price = price_token1_per_token0(sp, WETH_DECIMALS, USDG_DECIMALS) if sp else None
    _native_price_cache[block_num] = price
    return price


def usd_value(token: str, raw: int, block_num: int) -> float | None:
    t = token.lower()
    if t == USDG:
        return raw / (10 ** USDG_DECIMALS)
    if t in (WETH, NATIVE):
        native_usd = native_usd_price_at_block(block_num)
        return (raw / (10 ** WETH_DECIMALS)) * native_usd if native_usd is not None else None
    return None


def is_stable_or_native(token: str) -> bool:
    t = token.lower()
    return t in (USDG, WETH, NATIVE)


def time_boxed_chunked_get_logs(from_block: int, to_block: int, topics: list, chunk_size: int,
                                 deadline: float) -> tuple[list, bool, int]:
    """Обёртка над `_chunked_get_logs` с честным потолком по времени --
    останавливается (не бросает исключение) при исчерпании дедлайна,
    возвращает то, что успело накопиться, флаг `hit_deadline` и
    РЕАЛЬНО покрытый до какого блока включительно диапазон (может быть
    < to_block, если бюджет исчерпан на середине -- честно, не выдаёт
    частичное покрытие за полное)."""
    if from_block > to_block:
        return [], False, from_block - 1
    out = []
    block = from_block
    hit_deadline = False
    while block <= to_block:
        if time.time() > deadline:
            hit_deadline = True
            break
        end = min(block + chunk_size - 1, to_block)
        out.extend(_chunked_get_logs(block, end, topics, chunk_size=chunk_size))
        block = end + 1
    covered_to = block - 1
    return out, hit_deadline, covered_to


# --------------------------------------------------------------- phases


KNOWN_REAL_MULTI_LEG_TXS = [
    "0x2264176a23d8ced039d386829c709ce3b3ec335427582179fb7aaedcff4de245",
    "0xd487122244e6c89a0c7b91d48111720575294a6cfeceedfaa3cf105b1fb996d0",
    "0xa1733a2816fe30fbd3de9c8fedbcbe43f54c3dfdbe274bf838eba1f66a2c2236",
]


def sanity_check_known_txs(v3_pool_map: dict, v4_pool_map: dict) -> dict:
    """Владелец, задание п.4 -- ПРОВЕРИТЬ знаковую конвенцию на реальном
    известном примере: прогоняет ВЕСЬ реальный пайплайн этого скрипта
    (decode -> topology -> receipt-верификация) на 3 РЕАЛЬНЫХ известных
    multi-leg-своп tx этой цепи (`data/task5_arb_suspect_audit_3tx_
    result.json`, gas_used 378848/186163/290261) -- не отдельный
    изолированный тест, а прогон на самих продакшн-функциях, поэтому
    любое расхождение конвенции здесь = расхождение и в основном скане."""
    v3_topic = topic0("Swap(address,address,int256,int256,uint160,uint128,int24)")
    v4_topic = topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
    out = {"checked": []}
    for h in KNOWN_REAL_MULTI_LEG_TXS:
        entry: dict = {"tx_hash": h}
        try:
            rec = RPC("eth_getTransactionReceipt", [h])
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)
            out["checked"].append(entry)
            continue
        if not rec:
            entry["error"] = "receipt not found"
            out["checked"].append(entry)
            continue
        logs = rec.get("logs", [])
        v3_logs = [l for l in logs if l.get("topics", [None])[0] == v3_topic]
        v4_logs = [l for l in logs if l.get("topics", [None])[0] == v4_topic]
        entry["n_v3_swap_logs"], entry["n_v4_swap_logs"] = len(v3_logs), len(v4_logs)
        topo = build_topology_candidates(v3_logs, v4_logs, v3_pool_map, v4_pool_map)
        entry["topology_filter"] = topo["topology_filter"]
        cand = topo["candidates"][0] if topo["candidates"] else None
        entry["topology_candidate_found"] = cand is not None
        if cand:
            entry["n_legs"] = len(cand["legs"])
            vr = verify_candidates([cand], time.time() + 20.0)
            entry["is_verified_real_cycle"] = len(vr["verified_cycles"]) > 0
            if vr["verified_cycles"]:
                entry["profit_usd_net"] = vr["verified_cycles"][0]["profit_usd_net"]
        out["checked"].append(entry)
    return out


def time_boxed_chunked_get_logs_backward(from_block: int, to_block: int, topics: list, chunk_size: int,
                                          deadline: float) -> tuple[list, bool, int]:
    """Как `time_boxed_chunked_get_logs`, но идёт от `to_block` НАЗАД к
    `from_block` -- если бюджет кончится на середине, честно покрыты
    САМЫЕ СВЕЖИЕ блоки (ближе к latest_block), а не самые старые. Для
    поиска пулов это важно: своп-скан (см. ниже) тоже смотрит НАЗАД от
    latest_block -- пулы, актуальные ДЛЯ ЭТОГО ОКНА, скорее свежие."""
    if from_block > to_block:
        return [], False, to_block + 1
    out = []
    block = to_block
    hit_deadline = False
    while block >= from_block:
        if time.time() > deadline:
            hit_deadline = True
            break
        start = max(from_block, block - chunk_size + 1)
        out.extend(_chunked_get_logs(start, block, topics, chunk_size=chunk_size))
        block = start - 1
    covered_from = block + 1
    return out, hit_deadline, covered_from


def load_pool_map_cache() -> dict:
    default = {"v3": {"pools": {}, "covered_from_block": None, "covered_to_block": None},
               "v4": {"pools": {}, "covered_from_block": None, "covered_to_block": None}}
    if not POOL_MAP_CACHE_PATH.exists():
        return default
    try:
        d = json.loads(POOL_MAP_CACHE_PATH.read_text())
        for ver in ("v3", "v4"):
            d.setdefault(ver, default[ver])
            d[ver].setdefault("pools", {})
            d[ver].setdefault("covered_from_block", None)
            d[ver].setdefault("covered_to_block", None)
        return d
    except Exception:  # noqa: BLE001
        return default


def save_pool_map_cache(v3_entry: dict, v4_entry: dict) -> None:
    payload = {"v3": v3_entry, "v4": v4_entry, "saved_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    POOL_MAP_CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False, default=str))


def incremental_scan_one_version(cache_entry: dict, topic0_val: str, latest_block: int, budget_s: float,
                                  decode_fn, key_field: str) -> dict:
    """Инкрементальный скан ОДНОЙ версии (v3 PoolCreated / v4 Initialize)
    поверх кэша: (1) сначала закрывает разрыв [covered_to+1 .. latest]
    (новые пулы с прошлого прогона -- маленький, всегда нужен), (2) если
    бюджет остался -- расширяет покрытие НАЗАД [0 .. covered_from-1]
    (старая история, копится через прогоны). Без кэша -- честный
    backward-скан от latest_block вниз, бюджет-ограниченный (см.
    time_boxed_chunked_get_logs_backward -- при нехватке бюджета
    покрыты САМЫЕ СВЕЖИЕ блоки, не самые старые)."""
    t0 = time.time()
    pools = dict(cache_entry.get("pools") or {})
    covered_from = cache_entry.get("covered_from_block")
    covered_to = cache_entry.get("covered_to_block")
    n_new_logs = 0
    hit_any = False
    remaining = budget_s

    if covered_from is None or covered_to is None:
        logs, hit, reached_from = time_boxed_chunked_get_logs_backward(
            0, latest_block, [topic0_val], chunk_size=3_000_000, deadline=time.time() + remaining)
        for log in logs:
            d = decode_fn(log)
            pools[d[key_field]] = d
        n_new_logs += len(logs)
        hit_any = hit
        covered_from, covered_to = reached_from, latest_block
    else:
        if covered_to < latest_block:
            deadline = time.time() + remaining
            logs, hit, reached_to = time_boxed_chunked_get_logs(covered_to + 1, latest_block, [topic0_val],
                                                                  chunk_size=3_000_000, deadline=deadline)
            for log in logs:
                d = decode_fn(log)
                pools[d[key_field]] = d
            n_new_logs += len(logs)
            hit_any = hit_any or hit
            covered_to = reached_to
            remaining = budget_s - (time.time() - t0)
        if remaining > 5.0 and covered_from > 0:
            deadline = time.time() + remaining
            logs, hit, reached_from = time_boxed_chunked_get_logs_backward(
                0, covered_from - 1, [topic0_val], chunk_size=3_000_000, deadline=deadline)
            for log in logs:
                d = decode_fn(log)
                pools[d[key_field]] = d
            n_new_logs += len(logs)
            hit_any = hit_any or hit
            covered_from = reached_from

    return {
        "pools": pools, "covered_from_block": covered_from, "covered_to_block": covered_to,
        "n_new_this_run": n_new_logs, "hit_deadline": hit_any, "runtime_s": time.time() - t0,
        "full_history_covered": covered_from == 0 and covered_to >= latest_block,
    }


def scan_pool_universe(latest_block: int, v3_budget_s: float, v4_budget_s: float) -> dict:
    """PoolCreated(v3, любой фабрики)/Initialize(v4) -- события редкие
    (см. докстринг), большой chunk делает полную историю дёшевой. v3 и
    v4 получают НЕЗАВИСИМЫЕ бюджеты (см. комментарий у констант выше --
    в первом реальном прогоне общий дедлайн привёл к n_v4_pools=0
    структурно, не по факту). Инкрементально поверх кэша на диске,
    приоритет -- СВЕЖИЕ блоки (см. `incremental_scan_one_version`)."""
    t0 = time.time()
    cache = load_pool_map_cache()
    v3_res = incremental_scan_one_version(cache["v3"], V3_POOL_CREATED_TOPIC0, latest_block, v3_budget_s,
                                           decode_v3_pool_created, "pool")
    v4_res = incremental_scan_one_version(cache["v4"], V4_INITIALIZE_TOPIC0, latest_block, v4_budget_s,
                                           decode_v4_initialize, "pool_id")
    save_pool_map_cache(
        {"pools": v3_res["pools"], "covered_from_block": v3_res["covered_from_block"],
         "covered_to_block": v3_res["covered_to_block"]},
        {"pools": v4_res["pools"], "covered_from_block": v4_res["covered_from_block"],
         "covered_to_block": v4_res["covered_to_block"]},
    )
    return {
        "v3_pool_map": v3_res["pools"], "v4_pool_map": v4_res["pools"],
        "meta": {"n_v3_pools": len(v3_res["pools"]), "n_v4_pools": len(v4_res["pools"]),
                 "n_v3_pools_new_this_run": v3_res["n_new_this_run"], "n_v4_pools_new_this_run": v4_res["n_new_this_run"],
                 "runtime_s": time.time() - t0, "v3_hit_deadline": v3_res["hit_deadline"],
                 "v4_hit_deadline": v4_res["hit_deadline"],
                 "v3_covered_range": [v3_res["covered_from_block"], v3_res["covered_to_block"]],
                 "v4_covered_range": [v4_res["covered_from_block"], v4_res["covered_to_block"]],
                 "v3_full_history_covered": v3_res["full_history_covered"],
                 "v4_full_history_covered": v4_res["full_history_covered"],
                 "cache_path": str(POOL_MAP_CACHE_PATH)},
    }


def probe_v2_style(latest_block: int) -> dict:
    """Дешёвая проба на V2-классические (Sushiswap/PancakeSwap V2-форки)
    Swap-события -- НЕ полный пайплайн (см. докстринг модуля)."""
    t0 = time.time()
    from_block = max(0, latest_block - V2_PROBE_BLOCKS)
    try:
        logs = list(_chunked_get_logs(from_block, latest_block, [V2_SWAP_TOPIC0], chunk_size=2000))
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "n_found": None, "range": [from_block, latest_block]}
    distinct_pools = {l["address"].lower() for l in logs}
    return {
        "n_found": len(logs), "n_distinct_pool_addresses": len(distinct_pools),
        "range": [from_block, latest_block], "runtime_s": time.time() - t0,
        "note": ("V2-стиль обнаружен в пробе -- полный пайплайн (PairCreated-скан + "
                 "token0/token1 через eth_call) НЕ построен в этом прогоне, честное "
                 "ограничение по времени владельца." if logs else
                 "V2-стиль НЕ обнаружен в пробе за это окно -- согласуется с более "
                 "ранним наблюдением сессии, что v3+v4 доминируют по объёму на этой цепи."),
    }


def scan_swaps_timeboxed(latest_block: int, budget_deadline: float) -> dict:
    t0 = time.time()
    to_block = latest_block
    covered_from = latest_block + 1
    v3_logs: list = []
    v4_logs: list = []
    n_steps = 0
    while to_block >= 0 and time.time() < budget_deadline and (latest_block - to_block) < MAX_SWAP_SCAN_WINDOW_BLOCKS:
        from_block = max(0, to_block - SWAP_SCAN_STEP_BLOCKS + 1)
        v3_logs.extend(fetch_v3_swap_logs(from_block, to_block))
        v4_logs.extend(fetch_v4_swap_logs(from_block, to_block))
        covered_from = from_block
        to_block = from_block - 1
        n_steps += 1
    return {
        "v3_logs": v3_logs, "v4_logs": v4_logs,
        "meta": {"from_block": covered_from, "to_block": latest_block,
                 "window_blocks": latest_block - covered_from + 1, "n_steps": n_steps,
                 "runtime_s": time.time() - t0, "n_v3_swap_events": len(v3_logs), "n_v4_swap_events": len(v4_logs),
                 "hit_time_budget": time.time() >= budget_deadline,
                 "hit_window_cap": (latest_block - covered_from) >= MAX_SWAP_SCAN_WINDOW_BLOCKS},
    }


def build_topology_candidates(v3_logs: list, v4_logs: list, v3_pool_map: dict, v4_pool_map: dict) -> dict:
    swaps = [decode_v3_swap(l) for l in v3_logs] + [decode_v4_swap(l) for l in v4_logs]
    by_tx: dict[str, list] = defaultdict(list)
    for s in swaps:
        by_tx[s["tx_hash"]].append(s)
    for h in by_tx:
        by_tx[h].sort(key=lambda s: s["log_index"])

    n_multi_leg = n_unresolved_pool = n_ambiguous_leg = n_topology_candidates = 0
    candidates = []
    for tx_hash, legs in by_tx.items():
        if len(legs) < 2:
            continue
        n_multi_leg += 1
        leg_info = []
        ok = True
        for s in legs:
            if s["version"] == "v3":
                meta = v3_pool_map.get(s["pool"])
                if meta is None:
                    n_unresolved_pool += 1
                    ok = False
                    break
                c0, c1, fee_pips = meta["token0"], meta["token1"], meta["fee_pips"]
                pool_key = s["pool"]
            else:
                meta = v4_pool_map.get(s["pool_id"])
                if meta is None:
                    n_unresolved_pool += 1
                    ok = False
                    break
                c0, c1, fee_pips = meta["currency0"], meta["currency1"], s["fee_pips_real"]
                pool_key = s["pool_id"]
            a0, a1 = s["amount0"], s["amount1"]
            if (a0 > 0) == (a1 > 0):
                n_ambiguous_leg += 1
                ok = False
                break
            token_in, token_out = (c0, c1) if a0 > 0 else (c1, c0)
            amt_in, amt_out = (a0, -a1) if a0 > 0 else (a1, -a0)
            leg_info.append({
                "version": s["version"], "pool_key": pool_key, "token_in": token_in, "token_out": token_out,
                "amount_in_event": amt_in, "amount_out_event": amt_out, "fee_pips": fee_pips,
                "sender": s["sender"], "block_number": s["block_number"], "log_index": s["log_index"],
                "token0": c0, "token1": c1,
            })
        if not ok:
            continue
        connected = all(leg_info[i]["token_out"] == leg_info[i + 1]["token_in"] for i in range(len(leg_info) - 1))
        if not connected:
            continue
        cycle_token = leg_info[0]["token_in"]
        if cycle_token != leg_info[-1]["token_out"]:
            continue
        n_topology_candidates += 1
        candidates.append({"tx_hash": tx_hash, "legs": leg_info, "cycle_token_event": cycle_token,
                            "block_number": legs[0]["block_number"]})

    return {
        "candidates": candidates,
        "topology_filter": {"n_multi_leg_txs": n_multi_leg, "n_unresolved_pool_skipped": n_unresolved_pool,
                             "n_ambiguous_leg_skipped": n_ambiguous_leg,
                             "n_topology_candidates": n_topology_candidates},
    }


def verify_candidates(candidates: list, budget_deadline: float) -> dict:
    verified = []
    n_checked = n_capped = n_receipt_missing = 0
    dec_cache: dict[str, int] = {}
    for cand in candidates:
        if time.time() > budget_deadline:
            n_capped += 1
            continue
        try:
            rec = RPC("eth_getTransactionReceipt", [cand["tx_hash"]])
        except Exception:  # noqa: BLE001
            rec = None
        n_checked += 1
        if not rec or rec.get("status") != "0x1":
            n_receipt_missing += 1
            continue

        transfers = [t for t in (decode_transfer(l) for l in rec.get("logs", [])
                                  if l.get("topics", [None])[0] == TRANSFER_TOPIC0) if t]
        tx_from = (rec.get("from") or "").lower()
        senders = {leg["sender"] for leg in cand["legs"]}
        trader_addrs = senders | {tx_from}

        relevant_tokens = {leg["token_in"] for leg in cand["legs"]} | {leg["token_out"] for leg in cand["legs"]}
        real_net: dict[str, int] = defaultdict(int)
        real_vol: dict[str, int] = defaultdict(int)
        for t in transfers:
            if t["token"] not in relevant_tokens:
                continue
            if t["from"] in trader_addrs:
                real_net[t["token"]] -= t["value"]
                real_vol[t["token"]] += t["value"]
            if t["to"] in trader_addrs:
                real_net[t["token"]] += t["value"]
                real_vol[t["token"]] += t["value"]

        cycle_token = cand["cycle_token_event"]
        intermediate_ok = True
        for tok, net in real_net.items():
            if tok == cycle_token:
                continue
            vol = real_vol.get(tok, 0)
            if vol > 0 and abs(net) > 1e-9 * vol:
                intermediate_ok = False
                break
        real_cycle_net = real_net.get(cycle_token, 0)
        if not intermediate_ok or real_cycle_net <= 0:
            continue

        gas_used = int(rec.get("gasUsed", "0x0"), 16)
        eff_gas_price = int(rec.get("effectiveGasPrice", "0x0"), 16)
        gas_cost_native_raw = gas_used * eff_gas_price
        block_num = cand["block_number"]
        gas_cost_usd = usd_value(NATIVE, gas_cost_native_raw, block_num)

        profit_usd_gross = usd_value(cycle_token, real_cycle_net, block_num)
        pricing_method = "cycle_token_is_usdg" if cycle_token == USDG else (
            "cycle_token_is_weth_native" if cycle_token in (WETH, NATIVE) else None)
        if profit_usd_gross is None:
            cyc_dec = get_decimals(cycle_token, dec_cache)
            for leg in cand["legs"]:
                other = leg["token_out"] if leg["token_in"] == cycle_token else (
                    leg["token_in"] if leg["token_out"] == cycle_token else None)
                if other is None or not is_stable_or_native(other):
                    continue
                other_amt_raw = leg["amount_in_event"] if leg["token_in"] == other else leg["amount_out_event"]
                other_usd = usd_value(other, other_amt_raw, block_num)
                token_amt_raw = leg["amount_out_event"] if leg["token_in"] == other else leg["amount_in_event"]
                token_amt = token_amt_raw / (10 ** cyc_dec)
                if other_usd and token_amt:
                    implied_usd_per_token = other_usd / token_amt
                    profit_usd_gross = (real_cycle_net / (10 ** cyc_dec)) * implied_usd_per_token
                    pricing_method = f"implied_from_same_tx_leg_pool_{str(leg['pool_key'])[:12]}"
                    break

        profit_usd_net = (profit_usd_gross - gas_cost_usd) if (profit_usd_gross is not None and gas_cost_usd is not None) else None

        verified.append({
            "tx_hash": cand["tx_hash"], "block_number": block_num, "n_legs": len(cand["legs"]),
            "cycle_token": cycle_token, "gross_profit_cycle_token_raw": real_cycle_net,
            "gas_used": gas_used, "effective_gas_price_wei": eff_gas_price, "gas_cost_usd": gas_cost_usd,
            "profit_usd_gross": profit_usd_gross, "profit_usd_net": profit_usd_net, "pricing_method": pricing_method,
            "senders": sorted(senders), "tx_from": tx_from,
            "route": [{"pool_key": leg["pool_key"], "version": leg["version"], "token_in": leg["token_in"],
                       "token_out": leg["token_out"], "fee_pips": leg["fee_pips"], "token0": leg["token0"],
                       "token1": leg["token1"]} for leg in cand["legs"]],
        })
    return {
        "verified_cycles": verified,
        "verification_meta": {"n_candidates": len(candidates), "n_receipts_checked": n_checked,
                               "n_receipts_capped_by_budget": n_capped,
                               "n_receipt_missing_or_reverted": n_receipt_missing,
                               "n_verified_real_cycles": len(verified)},
    }


def leg_spot_price_token_out_per_in(leg: dict, block_num: int, dec_cache: dict) -> float | None:
    try:
        if leg["version"] == "v3":
            raw = RPC("eth_call", [{"to": leg["pool_key"], "data": V3_SLOT0_SELECTOR}, hex(block_num)])
            sqrt_price = decode_v3_slot0_price(raw)
        else:
            slot_int = state_slot_int(leg["pool_key"])
            calldata = EXTSLOAD_SELECTOR + slot_int.to_bytes(32, "big").hex()
            raw = RPC("eth_call", [{"to": POOL_MANAGER, "data": calldata}, hex(block_num)])
            sqrt_price = decode_v4_slot0_price(raw)
    except Exception:  # noqa: BLE001
        return None
    if not sqrt_price:
        return None
    dec0 = get_decimals(leg["token0"], dec_cache)
    dec1 = get_decimals(leg["token1"], dec_cache)
    price_1_per_0 = price_token1_per_token0(sqrt_price, dec0, dec1)
    if price_1_per_0 <= 0:
        return None
    return price_1_per_0 if leg["token_in"] == leg["token0"] else (1.0 / price_1_per_0)


def measure_lifetime(cycle: dict, budget_deadline: float) -> dict:
    """См. докстринг модуля -- сколько ПОСЛЕДОВАТЕЛЬНЫХ блоков ПОСЛЕ
    исполнения (cycle_block+1 .. +LIFETIME_FORWARD_BLOCKS) та же
    возможность (тот же маршрут/направление/комиссия, маргинальная спот-
    цена без слиппеджа на размере) остаётся net-положительной."""
    dec_cache: dict[str, int] = {}
    combined_fee_frac = sum(leg["fee_pips"] & ~DYNAMIC_FEE_FLAG for leg in cycle["route"]) / 1e6
    lifetime_blocks = 0
    censored = False
    for k in range(1, LIFETIME_FORWARD_BLOCKS + 1):
        if time.time() > budget_deadline:
            return {"lifetime_blocks": lifetime_blocks, "censored": None,
                     "note": "бюджет живучести исчерпан на середине -- честно частично"}
        block_num = cycle["block_number"] + k
        loop_product = 1.0
        ok = True
        for leg in cycle["route"]:
            p = leg_spot_price_token_out_per_in(leg, block_num, dec_cache)
            if p is None:
                ok = False
                break
            loop_product *= p
        if not ok:
            break
        net_edge_frac = loop_product - 1.0 - combined_fee_frac
        if net_edge_frac > 0:
            lifetime_blocks = k
        else:
            break
    else:
        censored = True
    return {"lifetime_blocks": lifetime_blocks, "censored": censored, "combined_fee_frac": combined_fee_frac}


def measure_repeat_occurrence(verified_cycles: list) -> None:
    """Дешёвая (без доп. RPC) вторичная сверка -- ближайшее повторное
    появление ТОЙ ЖЕ последовательности пулов в пределах уже
    отсканированного окна. Мутирует verified_cycles на месте, добавляя
    поле `nearest_repeat_gap_blocks`."""
    by_sig: dict[tuple, list] = defaultdict(list)
    for c in verified_cycles:
        sig = tuple(str(leg["pool_key"]) for leg in c["route"])
        by_sig[sig].append(c)
    for sig, group in by_sig.items():
        blocks = sorted(c["block_number"] for c in group)
        for c in group:
            others = [b for b in blocks if b != c["block_number"]]
            c["nearest_repeat_gap_blocks"] = min((abs(b - c["block_number"]) for b in others), default=None)


def main() -> None:
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    global_start = time.time()

    latest_block = get_block_number()
    result["latest_block"] = latest_block

    result["v2_probe"] = probe_v2_style(latest_block)

    pools = scan_pool_universe(latest_block, POOL_DISCOVERY_V3_BUDGET_S, POOL_DISCOVERY_V4_BUDGET_S)
    result["pool_discovery"] = pools["meta"]
    v3_pool_map, v4_pool_map = pools["v3_pool_map"], pools["v4_pool_map"]
    print(f"[rh_cycles] пулов: v3={len(v3_pool_map)} v4={len(v4_pool_map)}, "
          f"{pools['meta']['runtime_s']:.0f}с, v3_hit_deadline={pools['meta']['v3_hit_deadline']}, "
          f"v4_hit_deadline={pools['meta']['v4_hit_deadline']}")

    result["sanity_check_known_txs"] = sanity_check_known_txs(v3_pool_map, v4_pool_map)
    print(f"[rh_cycles] sanity-check на 3 известных tx: "
          f"{json.dumps(result['sanity_check_known_txs'], ensure_ascii=False, default=str)}")

    swap_deadline = time.time() + SWAP_SCAN_BUDGET_S
    swap_scan = scan_swaps_timeboxed(latest_block, swap_deadline)
    result["swap_scan"] = swap_scan["meta"]
    print(f"[rh_cycles] swap-скан: окно [{swap_scan['meta']['from_block']};{swap_scan['meta']['to_block']}] "
          f"({swap_scan['meta']['window_blocks']} блоков), v3={swap_scan['meta']['n_v3_swap_events']} "
          f"v4={swap_scan['meta']['n_v4_swap_events']}, {swap_scan['meta']['runtime_s']:.0f}с")

    topo = build_topology_candidates(swap_scan["v3_logs"], swap_scan["v4_logs"], v3_pool_map, v4_pool_map)
    result["topology_filter"] = topo["topology_filter"]
    print(f"[rh_cycles] топология: {topo['topology_filter']}")

    candidates = topo["candidates"]
    n_candidates_before_cap = len(candidates)
    if len(candidates) > MAX_CANDIDATES_TO_VERIFY:
        # Честная РАВНОМЕРНАЯ выборка по всему окну (не "первые N" --
        # сохраняет репрезентативность распределения по блокам), см.
        # комментарий у MAX_CANDIDATES_TO_VERIFY.
        step = len(candidates) / MAX_CANDIDATES_TO_VERIFY
        candidates = [candidates[int(i * step)] for i in range(MAX_CANDIDATES_TO_VERIFY)]
    result["candidate_sampling"] = {
        "n_topology_candidates_total": n_candidates_before_cap, "n_sampled_for_verification": len(candidates),
        "was_sampled": n_candidates_before_cap > MAX_CANDIDATES_TO_VERIFY,
    }
    print(f"[rh_cycles] кандидатов для receipt-верификации: {len(candidates)} из {n_candidates_before_cap}")

    verify_deadline = time.time() + RECEIPT_VERIFY_BUDGET_S
    verify_res = verify_candidates(candidates, verify_deadline)
    result["verification"] = verify_res["verification_meta"]
    verified_cycles = verify_res["verified_cycles"]
    print(f"[rh_cycles] верификация: {verify_res['verification_meta']}")

    measure_repeat_occurrence(verified_cycles)

    lifetime_deadline = time.time() + LIFETIME_BUDGET_S
    three_plus = [c for c in verified_cycles if c["n_legs"] >= 3]
    two_leg = [c for c in verified_cycles if c["n_legs"] == 2]
    n_two_leg_sample = max(0, min(len(two_leg), MAX_CYCLES_FOR_LIFETIME_CHECK - len(three_plus)))
    if n_two_leg_sample > 0 and len(two_leg) > n_two_leg_sample:
        step = len(two_leg) / n_two_leg_sample
        two_leg_sample = [two_leg[int(i * step)] for i in range(n_two_leg_sample)]
    else:
        two_leg_sample = two_leg[:n_two_leg_sample]
    lifetime_targets = three_plus + two_leg_sample
    print(f"[rh_cycles] живучесть: измеряем {len(lifetime_targets)} циклов "
          f"({len(three_plus)} 3+-плечевых из {len(three_plus)}, {len(two_leg_sample)} из {len(two_leg)} 2-плечевых)")

    n_lifetime_measured = n_lifetime_capped = 0
    for c in lifetime_targets:
        if time.time() > lifetime_deadline:
            n_lifetime_capped += 1
            continue
        c["lifetime"] = measure_lifetime(c, lifetime_deadline)
        n_lifetime_measured += 1
    result["lifetime_measurement_meta"] = {
        "n_targets": len(lifetime_targets), "n_measured": n_lifetime_measured, "n_capped_by_budget": n_lifetime_capped,
        "lifetime_forward_blocks_cap": LIFETIME_FORWARD_BLOCKS,
        "method": "маргинальная спот-цена (slot0 v3 / extsload v4), тот же принцип, что rh_arb_our_share.py, "
                  "применённый к реально найденному маршруту цикла, блоки cycle_block+1..+cap",
    }
    print(f"[rh_cycles] живучесть измерена: {n_lifetime_measured}/{len(lifetime_targets)}, "
          f"capped={n_lifetime_capped}")

    result["verified_cycles"] = verified_cycles

    # --- Сводка -- прямой ответ на вопрос владельца ---
    n_total = len(verified_cycles)
    legs_hist: dict[int, int] = defaultdict(int)
    for c in verified_cycles:
        legs_hist[c["n_legs"]] += 1
    n_three_plus = sum(v for k, v in legs_hist.items() if k >= 3)

    profits_2leg = [c["profit_usd_net"] for c in verified_cycles if c["n_legs"] == 2 and c["profit_usd_net"] is not None]
    profits_3plus = [c["profit_usd_net"] for c in verified_cycles if c["n_legs"] >= 3 and c["profit_usd_net"] is not None]

    lifetimes_2leg = [c["lifetime"]["lifetime_blocks"] for c in verified_cycles
                       if c["n_legs"] == 2 and "lifetime" in c and c["lifetime"].get("lifetime_blocks") is not None]
    lifetimes_3plus = [c["lifetime"]["lifetime_blocks"] for c in verified_cycles
                        if c["n_legs"] >= 3 and "lifetime" in c and c["lifetime"].get("lifetime_blocks") is not None]

    result["summary"] = {
        "window_scanned": swap_scan["meta"],
        "n_cycles_total": n_total,
        "n_legs_histogram": dict(sorted(legs_hist.items())),
        "n_cycles_3plus_legs": n_three_plus,
        "frac_cycles_3plus_legs": (n_three_plus / n_total) if n_total else None,
        "profit_usd_net_median_2leg": statistics.median(profits_2leg) if profits_2leg else None,
        "profit_usd_net_median_3plus": statistics.median(profits_3plus) if profits_3plus else None,
        "n_priced_2leg": len(profits_2leg), "n_priced_3plus": len(profits_3plus),
        "lifetime_blocks_median_2leg": statistics.median(lifetimes_2leg) if lifetimes_2leg else None,
        "lifetime_blocks_median_3plus": statistics.median(lifetimes_3plus) if lifetimes_3plus else None,
        "n_lifetime_measured_2leg": len(lifetimes_2leg), "n_lifetime_measured_3plus": len(lifetimes_3plus),
        "total_runtime_s": time.time() - global_start,
    }
    if n_total == 0:
        result["HONEST_ANSWER"] = (
            f"0 верифицированных реальных замкнутых циклов найдено в окне "
            f"[{swap_scan['meta']['from_block']};{swap_scan['meta']['to_block']}] "
            f"({swap_scan['meta']['window_blocks']} блоков, выборка, не полная история цепи). "
            f"Это честный результат за это окно, не утверждение про всю историю Robinhood Chain."
        )
    print(f"[rh_cycles] СВОДКА: {json.dumps(result['summary'], indent=2, ensure_ascii=False, default=str)}")

    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[rh_cycles] записано в {OUT_PATH}")


if __name__ == "__main__":
    main()
