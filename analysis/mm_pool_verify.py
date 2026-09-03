#!/usr/bin/env python3
"""Задача MM, владелец 2026-09-03: "Перебор пулов отменяется — адреса
найдены вручную через GeckoTerminal... Проверить каждый адрес одним
вызовом... Расхождения доложить."

Часть A: проверка 11 идентификаторов пулов, данных владельцем. ДВА из
11 (SPY, MSTR) -- 64 hex-символа (32 байта) -- по формату это v4
`poolId` (bytes32), а не адрес. ОСТАЛЬНЫЕ 9 -- 40 hex-символов
(20 байт) -- по формату это обычный адрес, но Uniswap V4 НЕ имеет
per-pool контрактов (синглтон-архитектура, весь стейт в PoolManager) --
что именно 20-байтные "адреса" означают, неизвестно ДО проверки:
не выдумываем (hook-контракт? v3-пул? токен по ошибке?) -- пробуем по
очереди самые вероятные гипотезы одним-двумя вызовами на кандидата и
докладываем, что сработало, честно.

Для 32-байтных (SPY/MSTR) -- прямой запрос Initialize-события по
topic1=poolId на известном PoolManager-синглтоне
(0x8366a39cc670b4001a1121b8f6a443a643e40951, подтверждён в
data/p3_guard_cache/mm_discover_result.json) -- подтверждает
currency0/currency1 НАПРЯМУЮ из события создания пула, затем читает
текущий стейт через extsload (тот же метод, что в
analysis/mm_liquidity_prefilter.py, уже офлайн-проверен).

Часть B: DELL/USO -- почему цена по ощущениям владельца "ровно $1.00,
капитализация $3.2 млрд" -- сверяем реальный оракул (Chainlink-подобный
price feed, r1_token_feed_map.csv) и totalSupply()/decimals() токена
напрямую, без предположений о причине несовпадения с GeckoTerminal.

Только чтение, ключ не используется, транзакций нет.
"""
from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from Crypto.Hash import keccak  # noqa: E402
from eth_abi import decode as abi_decode  # noqa: E402

from alchemy_fallback import _chunked_get_logs, _rpc_call, get_block_number, topic0  # noqa: E402
from mm_liquidity_prefilter import (  # noqa: E402
    QUOTE_DECIMALS, QUOTE_LABEL, eth_usd_price, quote_reserve_raw, read_v3_pool, read_v4_pool,
)

OUT_PATH = Path("data/p3_guard_cache/mm_pool_verify_result.json")
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
INITIALIZE_TOPIC0 = topic0(
    "Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)"
)
GET_RESERVES_SELECTOR = topic0("getReserves()")[:10]

# Идентификаторы, данные владельцем -- ДОСЛОВНО, без исправлений формата.
USER_POOLS = {
    "NVDA": "0xd4eb21209c4d6093f80b5b84f5c45cc093ea14a3",
    "SPY":  "0xfe2a80bb5618fd14984b92ca6d45bf5ba67443ddb1435e28b2e48df2fc1526cd",
    "QQQ":  "0xd60a5d14db690b7afad71f76b108071d7175597d",
    "RDDT": "0xa8744e76aed23b05f0126335e7bd38f7935d19fe",
    "COST": "0x0a2121a50a09ed0796ae81f9c53ff9398355a398",
    "GME":  "0xe2b46c905e12ab8e2f864e4821a4325884c1b126",
    "RBLX": "0x1bdb8e3a79cb1a7f228808739311e23098d33d43",
    "LLY":  "0xd2038788ebe1e0bfd7c0a6112f09778f3aeaeca6",
    "MSTR": "0x319bac87e616a89e241c10aeb8afd4892a852cdd8b373cd9765ecddc40b87cfe",
}
EXTRA_TOKENS = {"DELL": "0xc30c89cb7815a1488b7998d15eec73961707fc5a",
                "USO": "0x02175608f1b5e6b5ed221ccfdc7be197d111d915"}

_request_count = 0


def _count(n: int = 1) -> None:
    global _request_count
    _request_count += n


def _known_token_addresses() -> dict[str, str]:
    """symbol -> адрес, из уже оплаченного и провалидированного реестра
    Sprint R1 (194 токена) -- НЕ новый запрос."""
    out = {}
    with open("data/sprintR1_cache/r1_rwa_full_universe.csv", newline="") as f:
        for row in csv.DictReader(f):
            out[row["token_symbol"]] = row["token_address"].lower()
    return out


def _known_feed_addresses() -> dict[str, str]:
    out = {}
    with open("data/sprintR1_cache/r1_token_feed_map.csv", newline="") as f:
        for row in csv.DictReader(f):
            out[row["symbol"]] = row["feed_address"].lower()
    return out


def _eth_call(to: str, data: str) -> str | None:
    _count()
    try:
        return _rpc_call("eth_call", [{"to": to, "data": data}, "latest"])
    except Exception as e:  # noqa: BLE001
        print(f"[mm_pool_verify]   eth_call {to} {data[:10]} не удался: {e}")
        return None


def _get_code(addr: str) -> str | None:
    _count()
    try:
        return _rpc_call("eth_getCode", [addr, "latest"])
    except Exception as e:  # noqa: BLE001
        print(f"[mm_pool_verify]   eth_getCode {addr} не удался: {e}")
        return None


def verify_32byte_pool_id(sym: str, pool_id: str, expected_token_addr: str) -> dict:
    """SPY/MSTR-подобные: 64 hex-символа -- по формату v4 poolId. Прямая
    проверка -- запросить Initialize-событие ПО ЭТОМУ poolId (topic1,
    индексированный) на известном PoolManager, получить РЕАЛЬНЫЕ
    currency0/currency1 из момента создания пула, затем прочитать
    текущий стейт (extsload)."""
    _count()
    latest = get_block_number()
    logs = list(_chunked_get_logs(
        156309, latest, [INITIALIZE_TOPIC0, pool_id], chunk_size=2_000_000, address=POOL_MANAGER,
        on_call=lambda lo, hi, n: _count(n),
    ))
    if not logs:
        return {"format": "32-byte (pool_id)", "found_initialize_event": False,
                "verdict": "НЕ НАЙДЕНО Initialize-событие с этим poolId на известном PoolManager -- расхождение"}
    log = logs[0]
    currency0 = "0x" + log["topics"][2][-40:]
    currency1 = "0x" + log["topics"][3][-40:]
    matches_token = expected_token_addr.lower() in (currency0, currency1)
    matches_usdg = USDG in (currency0, currency1)
    state = read_v4_pool(POOL_MANAGER, pool_id)
    result = {
        "format": "32-byte (pool_id)", "found_initialize_event": True,
        "currency0": currency0, "currency1": currency1,
        "matches_expected_token": matches_token, "matches_usdg": matches_usdg,
        "block_number": int(log["blockNumber"], 16), "tx_hash": log["transactionHash"],
    }
    if state:
        sqrt_price, liquidity = state
        result["sqrt_price_x96"] = sqrt_price
        result["liquidity_raw"] = liquidity
        result["has_liquidity"] = liquidity > 0
    verdict = "ПОДТВЕРЖДЕНО" if (matches_token and matches_usdg) else "РАСХОЖДЕНИЕ -- валюты не совпадают с ожиданием"
    result["verdict"] = verdict
    return result


def verify_20byte_candidate(sym: str, addr: str, expected_token_addr: str) -> dict:
    """Формат не определён заранее -- пробуем по очереди: (1) есть ли
    вообще код контракта; (2) Pons-V2-стиль getReserves() (по прецеденту
    NVX, sc1_nvx_chronology.py) -- вероятная гипотеза, раз пулы на v4
    (hook-контракты -- реальные адреса, в отличие от самого poolId);
    (3) фолбэк -- стандартный v3 token0()/token1()/liquidity()."""
    code = _get_code(addr)
    has_code = bool(code) and code != "0x"
    result = {"format": "20-byte (адрес)", "has_bytecode": has_code}
    if not has_code:
        result["verdict"] = "НЕТ КОДА КОНТРАКТА по этому адресу -- расхождение (не пул, не токен)"
        return result

    # Гипотеза 1: Pons-V2-подобный getReserves() -> (uint256, uint256)
    reserves_raw = _eth_call(addr, GET_RESERVES_SELECTOR)
    if reserves_raw and len(reserves_raw) >= 2 + 128:
        try:
            r0, r1 = abi_decode(["uint256", "uint256"], bytes.fromhex(reserves_raw[2:]))
            result["getReserves_hypothesis"] = {"reserve0": r0, "reserve1": r1}
            result["verdict"] = "getReserves() СРАБОТАЛ -- похоже на Pons-V2-подобный curve/hook-контракт"
            return result
        except Exception:  # noqa: BLE001
            pass

    # Гипотеза 2: стандартный v3-пул (token0/token1/liquidity/slot0)
    v3_state = read_v3_pool(addr)
    tok0 = _eth_call(addr, "0x" + topic0("token0()")[2:10])
    tok1 = _eth_call(addr, "0x" + topic0("token1()")[2:10])
    if v3_state and tok0 and tok1:
        t0 = "0x" + tok0[-40:]
        t1 = "0x" + tok1[-40:]
        matches_token = expected_token_addr.lower() in (t0, t1)
        matches_usdg = USDG in (t0, t1)
        sqrt_price, liquidity = v3_state
        result["v3_hypothesis"] = {
            "token0": t0, "token1": t1, "sqrt_price_x96": sqrt_price, "liquidity_raw": liquidity,
            "matches_expected_token": matches_token, "matches_usdg": matches_usdg,
        }
        result["verdict"] = "V3-ПУЛ ПОДТВЕРЖДЁН" if (matches_token and matches_usdg) else "v3-пул, но валюты НЕ совпадают с ожиданием"
        return result

    result["verdict"] = "НЕ УДАЛОСЬ определить тип контракта (ни getReserves(), ни v3-интерфейс не сработали) -- честно не идентифицировано"
    return result


def investigate_price_anomaly(sym: str, token_addr: str, feed_addr: str | None) -> dict:
    out = {"token_address": token_addr, "feed_address": feed_addr}
    ts = _eth_call(token_addr, "0x" + topic0("totalSupply()")[2:10])
    dec = _eth_call(token_addr, "0x" + topic0("decimals()")[2:10])
    if ts:
        out["total_supply_raw"] = int(ts, 16)
    if dec:
        out["decimals"] = int(dec, 16)
    if ts and dec:
        out["total_supply_human"] = int(ts, 16) / (10 ** int(dec, 16))
    if feed_addr:
        # latestRoundData() -- стандартный Chainlink-подобный интерфейс
        answer_raw = _eth_call(feed_addr, "0x" + topic0("latestRoundData()")[2:10])
        if answer_raw:
            try:
                decoded = abi_decode(["uint80", "int256", "uint256", "uint256", "uint80"], bytes.fromhex(answer_raw[2:]))
                out["oracle_answer_raw"] = decoded[1]
                feed_dec = _eth_call(feed_addr, "0x" + topic0("decimals()")[2:10])
                if feed_dec:
                    fd = int(feed_dec, 16)
                    out["oracle_decimals"] = fd
                    out["oracle_price_usd"] = decoded[1] / (10 ** fd)
            except Exception as e:  # noqa: BLE001
                print(f"[mm_pool_verify]   latestRoundData decode для {sym} не удался: {e}")
    if "total_supply_human" in out and "oracle_price_usd" in out:
        out["implied_market_cap_usd"] = out["total_supply_human"] * out["oracle_price_usd"]
    return out


def run() -> int:
    t0 = time.time()
    known_tokens = _known_token_addresses()
    known_feeds = _known_feed_addresses()

    print("=== Часть A: проверка 11 идентификаторов от владельца ===")
    verify_results = {}
    for sym, ident in USER_POOLS.items():
        expected = known_tokens.get(sym)
        print(f"[mm_pool_verify] {sym}: {ident} (ожидаемый адрес токена: {expected})")
        if not expected:
            verify_results[sym] = {"error": f"символ {sym} не найден в data/sprintR1_cache/r1_rwa_full_universe.csv -- не проверено"}
            continue
        hexlen = len(ident) - 2
        if hexlen == 64:
            verify_results[sym] = verify_32byte_pool_id(sym, ident.lower(), expected)
        elif hexlen == 40:
            verify_results[sym] = verify_20byte_candidate(sym, ident.lower(), expected)
        else:
            verify_results[sym] = {"error": f"неожиданная длина идентификатора: {hexlen} hex-символов -- расхождение"}
        print(f"[mm_pool_verify]   -> {verify_results[sym].get('verdict', verify_results[sym].get('error'))}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({"part_a_pool_verification": verify_results}, indent=2, default=str, ensure_ascii=False))

    print("\n=== Часть B: DELL/USO -- цена $1.00 / капитализация $3.2 млрд, реальный источник ===")
    price_results = {}
    for sym, addr in EXTRA_TOKENS.items():
        feed = known_feeds.get(sym)
        print(f"[mm_pool_verify] {sym}: токен={addr} фид={feed}")
        price_results[sym] = investigate_price_anomaly(sym, addr, feed)
        print(f"[mm_pool_verify]   -> {json.dumps(price_results[sym], default=str)}")

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "part_a_pool_verification": verify_results,
        "part_b_price_anomaly_dell_uso": price_results,
        "requests_used": _request_count,
        "runtime_s": time.time() - t0,
    }
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[mm_pool_verify] записано {OUT_PATH}, {_request_count} запросов, {time.time()-t0:.0f}с")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
