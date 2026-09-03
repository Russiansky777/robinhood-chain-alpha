#!/usr/bin/env python3
"""Владелец, 2026-09-03 (дозапрос после P5-разведки):

1. "P3 — уточнить формулировку: закрыт не потому, что v3 нет, а потому,
   что ни один сток-токен не имеет ОДНОВРЕМЕННО v3- и v4-пула с
   ликвидностью — второй ноги для кросс-версионного арбитража не
   существует. Если найдётся токен с обеими ногами — доложить, это
   меняет вывод." — эта формулировка проверяется здесь напрямую, не
   декларируется: для 7 токенов с уже подтверждённым v3-пулом
   (mm_pool_verify_result.json) ищем v4-ногу (Initialize-событие на
   известном PoolManager, currency0/currency1 == токен), для 2 токенов
   с уже подтверждённым v4-пулом (SPY/MSTR) ищем v3-ногу через
   factory.getPool(token, USDG, fee).

2. "DELL/USO — дешёвая проверка: какая цена в самом пуле (slot0 /
   резервы)... Сравнить с оракулом. Если пул торгует около $1.00 при
   оракуле $485 — доложить немедленно." — для DELL/USO пул ещё не
   найден (в mm_pool_verify.py проверялся только сам ТОКЕН — totalSupply/
   oracle, не пул) — здесь ищем пул (обе версии) тем же методом, что
   в п.1, и читаем его РЕАЛЬНУЮ цену (slot0/extsload), сравниваем с уже
   известной оракульной ($485.50677899 DELL, $141.98025 USO,
   mm_pool_verify_result.json).

Метод для V3-факторики: официальный CREATE2-адрес Uniswap V3 Factory
(analysis/p3_common.py: CANDIDATE_V3_FACTORY, "НЕ подтверждён для
Robinhood Chain напрямую") здесь подтверждается НАПРЯМУЮ — читаем
factory() с уже верифицированного реального v3-пула (NVDA,
mm_pool_verify_result.json), а не полагаемся на детерминированный
адрес вслепую.

getPool(address,address,uint24) — дословная сигнатура
IUniswapV3Factory.sol (Uniswap Labs, стандартна на всех EVM-деплоях
V3) — селектор посчитан локально (Crypto.Hash.keccak), не выдуман.

Только чтение, ключ не используется, транзакций нет. Оценка: ~1 (factory)
+ ~16 (getPool: 4 токена x 4 fee-тира) + ~4-8 на найденные v3-пулы (state)
+ ~18 (Initialize-скан: 9 токенов x 2 направления currency0/currency1,
chunk_size=5_000_000 -> по 1 вызову на диапазон при текущей высоте цепи)
+ ~10-20 на найденные v4-пулы (extsload) + decimals() -- порядка
60-90 запросов, секунды-десятки секунд.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from eth_abi import encode as abi_encode  # noqa: E402

from alchemy_fallback import _chunked_get_logs, _rpc_call, get_block_number, topic0  # noqa: E402
from mm_liquidity_prefilter import read_v3_pool, read_v4_pool  # noqa: E402
from mm_p5_setup import sqrt_price_to_usd  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/mm_p3_v4leg_check_result.json")
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
USDG_DECIMALS = 6
INITIALIZE_TOPIC0 = topic0("Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)")
FIRST_BLOCK = 156309  # тот же нижний край, что mm_pool_verify.py (безопасный старт для Initialize-скана)
FEE_TIERS = [100, 500, 3000, 10000]  # стандартные Uniswap V3 fee tiers (IUniswapV3Factory)

# Уже подтверждён v3-пул с реальной ликвидностью (mm_pool_verify_result.json) -- ищем v4-ногу
V3_CONFIRMED = {
    "NVDA": "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec",
    "QQQ": "0xd5f3879160bc7c32ebb4dc785f8a4f505888de68",
    "RDDT": "0x05b37fb53a299a1b874a619e1c4c404d52c36f4c",
    "COST": "0x4ea005168d7f09a7a0ba9d1def21a479950e44c2",
    "GME": "0x1b0e319c6a659f002271b69db8a7df2f911c153e",
    "RBLX": "0xf0c4bf4c582cb3836e98394b1d4e7b7281101be8",
    "LLY": "0x8005d266423c7ea827372c9c864491e5786600ea",
}
# Уже подтверждён v4-пул с реальной ликвидностью -- ищем v3-ногу
V4_CONFIRMED = {
    "SPY": "0x117cc2133c37b721f49de2a7a74833232b3b4c0c",
    "MSTR": "0xec262a75e413fafd0df80480274532c79d42da09",
}
# Ни одна нога не подтверждена -- ищем обе (дешёвая проверка владельца)
UNKNOWN = {
    "DELL": "0xc30c89cb7815a1488b7998d15eec73961707fc5a",
    "USO": "0x02175608f1b5e6b5ed221ccfdc7be197d111d915",
}
KNOWN_V3_POOL_FOR_FACTORY = "0xd4eb21209c4d6093f80b5b84f5c45cc093ea14a3"  # NVDA -- реальный v3-пул, читаем factory() с него
KNOWN_ORACLE_PRICES = {"DELL": 485.50677899, "USO": 141.98025}  # data/p3_guard_cache/mm_pool_verify_result.json

_request_count = 0


def _count(n: int = 1) -> None:
    global _request_count
    _request_count += n


def _eth_call(to: str, data: str) -> str | None:
    _count()
    try:
        return _rpc_call("eth_call", [{"to": to, "data": data}, "latest"])
    except Exception as e:  # noqa: BLE001
        print(f"[mm_p3_v4leg_check]   eth_call {to} {data[:10]} не удался: {e}")
        return None


def _selector(sig: str) -> str:
    return topic0(sig)[:10]


def _addr_topic(addr: str) -> str:
    return "0x" + "0" * 24 + addr.lower().removeprefix("0x")


def decimals_of(token: str) -> int | None:
    r = _eth_call(token, _selector("decimals()"))
    return int(r, 16) if r else None


def find_v4_pools_for_token(token_addr: str, latest_block: int) -> list[dict]:
    """Initialize-события на известном PoolManager, где пара
    (currency0, currency1) == (token, USDG) в любом порядке -- ТОЧНАЯ
    пара, не "token с любым контрагентом". Важно: фильтр только по
    одной стороне (currency0==token ИЛИ currency1==token, без фиксации
    второй валюты) на практике возвращает тысячи событий на токен --
    Pons V2 создаёт новый hook-контракт (и потому новый pool_id) почти
    на каждый цикл лаунча, но currency0/currency1 сами не привязаны к
    USDG (NVDA/USDG отдельно от NVDA/<прочий-токен-лаунча>) -- см.
    docs/PROJECT_STATE.md ("NVDA — 8270 различных v4 pool_id"). Фильтр
    по ОБЕИМ валютам сразу (topics[2] И topics[3] одновременно) сужает
    до реально интересной пары."""
    tok_topic = _addr_topic(token_addr)
    usdg_topic = _addr_topic(USDG)
    raw = []
    for topics in (
        [INITIALIZE_TOPIC0, None, tok_topic, usdg_topic],
        [INITIALIZE_TOPIC0, None, usdg_topic, tok_topic],
    ):
        logs = list(_chunked_get_logs(
            FIRST_BLOCK, latest_block, topics, chunk_size=5_000_000,
            address=POOL_MANAGER, on_call=lambda lo, hi, n: _count(1),
        ))
        raw.extend(logs)
    dedup: dict[str, dict] = {}
    for log in raw:
        pool_id = str(log["topics"][1]).lower()
        currency0 = "0x" + str(log["topics"][2])[-40:]
        currency1 = "0x" + str(log["topics"][3])[-40:]
        dedup[pool_id] = {
            "pool_id": pool_id, "currency0": currency0, "currency1": currency1,
            "block_number": int(log["blockNumber"], 16), "tx_hash": log["transactionHash"],
        }
    results = []
    for pool_id, entry in dedup.items():
        state = read_v4_pool(POOL_MANAGER, pool_id)
        if state:
            sqrt_price, liquidity = state
            entry["sqrt_price_x96"] = sqrt_price
            entry["liquidity_raw"] = liquidity
            entry["has_liquidity"] = liquidity > 0
        results.append(entry)
    return results


def get_v3_factory() -> str | None:
    r = _eth_call(KNOWN_V3_POOL_FOR_FACTORY, _selector("factory()"))
    if not r:
        return None
    return "0x" + r[-40:]


def find_v3_pools(factory: str, token_addr: str) -> list[dict]:
    """factory.getPool(token, USDG, fee) по стандартным fee-тирам --
    дословная сигнатура IUniswapV3Factory.sol."""
    found = []
    for fee in FEE_TIERS:
        calldata = "0x" + _selector("getPool(address,address,uint24)")[2:] + \
            abi_encode(["address", "address", "uint24"], [token_addr, USDG, fee]).hex()
        r = _eth_call(factory, calldata)
        if not r:
            continue
        pool_addr = "0x" + r[-40:]
        if pool_addr == "0x" + "0" * 40:
            continue
        entry = {"fee": fee, "pool_address": pool_addr}
        state = read_v3_pool(pool_addr)
        if state:
            sqrt_price, liquidity = state
            entry["sqrt_price_x96"] = sqrt_price
            entry["liquidity_raw"] = liquidity
            entry["has_liquidity"] = liquidity > 0
        found.append(entry)
    return found


def _pool_implied_price(token_addr: str, token_dec: int, currency0: str, sqrt_price_x96: int) -> float:
    stock_is_token1 = currency0.lower() != token_addr.lower()  # если token -- НЕ currency0, значит currency1
    return sqrt_price_to_usd(sqrt_price_x96, token_dec if not stock_is_token1 else USDG_DECIMALS,
                              USDG_DECIMALS if not stock_is_token1 else token_dec, stock_is_token1)


def run() -> int:
    t0 = time.time()
    latest = get_block_number()
    _count()

    print("=== П.1: реальный V3 Factory (читаем factory() с уже подтверждённого пула NVDA) ===")
    factory = get_v3_factory()
    print(f"[mm_p3_v4leg_check] V3 Factory = {factory}")

    print("\n=== П.2: v3-подтверждённые токены -- есть ли v4-нога с ликвидностью? ===")
    v3_confirmed_v4_leg = {}
    for sym, token_addr in V3_CONFIRMED.items():
        pools = find_v4_pools_for_token(token_addr, latest)
        with_liq = [p for p in pools if p.get("has_liquidity")]
        v3_confirmed_v4_leg[sym] = {"token_address": token_addr, "v4_pools_found": pools,
                                     "v4_pools_with_liquidity": len(with_liq)}
        print(f"[mm_p3_v4leg_check] {sym}: v4 Initialize-событий найдено {len(pools)}, "
              f"с реальной ликвидностью {len(with_liq)}")

    print("\n=== П.3: v4-подтверждённые токены (SPY/MSTR) -- есть ли v3-нога с ликвидностью? ===")
    v4_confirmed_v3_leg = {}
    if factory:
        for sym, token_addr in V4_CONFIRMED.items():
            pools = find_v3_pools(factory, token_addr)
            with_liq = [p for p in pools if p.get("has_liquidity")]
            v4_confirmed_v3_leg[sym] = {"token_address": token_addr, "v3_pools_found": pools,
                                         "v3_pools_with_liquidity": len(with_liq)}
            print(f"[mm_p3_v4leg_check] {sym}: v3-пулов найдено {len(pools)} (по {len(FEE_TIERS)} fee-тирам), "
                  f"с реальной ликвидностью {len(with_liq)}")
    else:
        print("[mm_p3_v4leg_check] factory() не прочитан -- п.3 пропущен")

    any_both_legs = any(v["v4_pools_with_liquidity"] > 0 for v in v3_confirmed_v4_leg.values()) or \
        any(v["v3_pools_with_liquidity"] > 0 for v in v4_confirmed_v3_leg.values())

    print("\n=== П.4: DELL/USO -- найти пул (обе версии), сравнить с оракулом ===")
    dell_uso = {}
    for sym, token_addr in UNKNOWN.items():
        tok_dec = decimals_of(token_addr)
        v4_pools = find_v4_pools_for_token(token_addr, latest)
        v3_pools = find_v3_pools(factory, token_addr) if factory else []
        oracle_price = KNOWN_ORACLE_PRICES[sym]
        implied = []
        for p in v4_pools:
            if p.get("has_liquidity") and tok_dec is not None:
                price = _pool_implied_price(token_addr, tok_dec, p["currency0"], p["sqrt_price_x96"])
                dev_pct = (price - oracle_price) / oracle_price * 100
                implied.append({"version": "v4", "pool_id": p["pool_id"], "pool_price_usd": price,
                                 "deviation_from_oracle_pct": dev_pct})
        for p in v3_pools:
            if p.get("has_liquidity") and tok_dec is not None:
                t0c = _eth_call(p["pool_address"], _selector("token0()"))
                currency0 = "0x" + t0c[-40:] if t0c else USDG
                price = _pool_implied_price(token_addr, tok_dec, currency0, p["sqrt_price_x96"])
                dev_pct = (price - oracle_price) / oracle_price * 100
                implied.append({"version": "v3", "pool_address": p["pool_address"], "fee": p["fee"],
                                 "pool_price_usd": price, "deviation_from_oracle_pct": dev_pct})
        dell_uso[sym] = {
            "token_address": token_addr, "decimals": tok_dec, "oracle_price_usd": oracle_price,
            "v4_pools_found": v4_pools, "v3_pools_found": v3_pools, "pool_implied_prices": implied,
        }
        if implied:
            for entry in implied:
                print(f"[mm_p3_v4leg_check] {sym} {entry['version']}: пул-цена ${entry['pool_price_usd']:.4f}, "
                      f"оракул ${oracle_price:.4f}, расхождение {entry['deviation_from_oracle_pct']:.2f}%")
        else:
            print(f"[mm_p3_v4leg_check] {sym}: ни один найденный пул не имеет реальной ликвидности "
                  f"(v4 найдено {len(v4_pools)}, v3 найдено {len(v3_pools)}) -- цену пула сравнить не с чем")

    dollar_anomaly = any(
        abs(e["pool_price_usd"] - 1.0) < 0.05 for v in dell_uso.values() for e in v["pool_implied_prices"]
    )

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "v3_factory_confirmed": factory,
        "p3_cross_version_check": {
            "v3_confirmed_tokens_v4_leg": v3_confirmed_v4_leg,
            "v4_confirmed_tokens_v3_leg": v4_confirmed_v3_leg,
            "any_token_with_both_legs_liquid": any_both_legs,
            "verdict": ("НАЙДЕН токен с обеими ногами (v3 И v4 с ликвидностью) -- вывод P3 меняется, см. поля выше"
                        if any_both_legs else
                        "Ни один из 9 проверенных токенов не имеет ОБЕИХ ног (v3 И v4) с реальной ликвидностью "
                        "одновременно -- второй ноги для кросс-версионного арбитража на этих токенах нет"),
        },
        "dell_uso_pool_vs_oracle": dell_uso,
        "dollar_anomaly_confirmed_in_pool": dollar_anomaly,
        "requests_used": _request_count,
        "runtime_s": time.time() - t0,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[mm_p3_v4leg_check] записано {OUT_PATH}, {_request_count} запросов, {time.time()-t0:.0f}с")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
