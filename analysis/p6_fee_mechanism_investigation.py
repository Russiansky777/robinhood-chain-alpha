"""P6 -- 3 read-only проверки реального механизма комиссий (владелец,
2026-09-05, "перестановка отложена, сначала три проверки"). НИКАКИХ
транзакций, позиция не трогается.

1. Реальный fee APR пула из часового объёма GT (24ч и 7д) x fee_tier x
   365 / TVL -- не DefiLlama.
2. Наши реальные комиссии (eth_call collect-симуляция) против ОЖИДАЕМЫХ
   по реальному объёму за тот же период x fee_tier x НАША ДОЛЯ АКТИВНОЙ
   (не застейканной) ликвидности -- см. п.3 ниже, почему "доля" здесь не
   наивная liquidity/liquidity().
3. Механика комиссий Aerodrome Slipstream -- ПРОЧИТАН РЕАЛЬНЫЙ ИСХОДНИК
   (WebFetch, github.com/aerodrome-finance/slipstream/contracts/core/
   CLPool.sol, 2026-09-05), НЕ документация:
   - `calculateFees()` в swap(): комиссия РЕАЛЬНО делится между staked
     (в gauge, `stakedLiquidity`) и unstaked (нам) ликвидностью.
   - Если есть unstaked-ликвидность, СВЕРХУ снимается ДОПОЛНИТЕЛЬНАЯ
     `unstakedFee` (per-million доля, `applyUnstakedFees()`):
       stakedFee_cut = unstakedFeeAmount_raw * unstakedFee() / 1_000_000
       feeGrowthGlobalX128 += (unstakedFeeAmount_raw - stakedFee_cut) * Q128 / (liquidity - stakedLiquidity)
     -- т.е. НАША (unstaked) доля роста feeGrowthGlobal считается от
     ПОСЛЕ-ВЫЧЕТА суммы, делённой на (liquidity - stakedLiquidity), НЕ
     на весь liquidity(). Дальше -- `position.update()` с
     feeGrowthInside, тот же путь, что Uniswap v3 core (это ЧАСТЬ
     механики РАБОТАЕТ как v3 -- разница только в ЧТО именно делится).
   - `unstakedFee()` и `stakedLiquidity` -- РЕАЛЬНЫЕ ончейн-значения,
     читаются здесь напрямую (public state var / public view), не
     предполагаются.
"""
import json
import math
import time
from pathlib import Path

import requests
from eth_abi import decode as abi_decode, encode as abi_encode
from eth_utils import to_checksum_address
from Crypto.Hash import keccak

OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "p3_guard_cache" / "p6_fee_mechanism_investigation_result.json"
STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "p6_live_position_state.json"

BASE_RPC = "https://mainnet.base.org"
WALLET = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"
NFPM = "0x827922686190790b37229fd06084350E74485b72"
POOL_ADDRESS = "0x3e66e55e97ce60096f74b7c475e8249f2d31a9fb"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CBBTC = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"
USDC_DECIMALS, CBBTC_DECIMALS = 6, 8
TOKEN_ID = 76445294
POOL_FEE_FRACTION = 0.00033  # 0.033%, docs/PROJECT_STATE.md "Скринер пулов"

GT_NETWORK = "base"
GT_RATE_LIMIT_BACKOFF_S = 65.0
GT_RATE_LIMIT_MAX_RETRIES = 2

RPC_MIN_INTERVAL_S = 1.0
RPC_RETRY_BACKOFF_S = 15.0
RPC_MAX_RETRIES = 3
_last_rpc_call = 0.0


def _selector(sig: str) -> str:
    k = keccak.new(digest_bits=256)
    k.update(sig.encode())
    return "0x" + k.hexdigest()[:8]


def rpc(method: str, params: list):
    global _last_rpc_call
    wait = _last_rpc_call + RPC_MIN_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    for attempt in range(RPC_MAX_RETRIES + 1):
        _last_rpc_call = time.monotonic()
        r = requests.post(BASE_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
        if r.status_code == 429 and attempt < RPC_MAX_RETRIES:
            time.sleep(RPC_RETRY_BACKOFF_S)
            continue
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"{method} {params}: {body['error']}")
        return body["result"]
    raise RuntimeError("RPC 429 после ретраев")


def eth_call(to: str, data: str, frm: str | None = None):
    p = {"to": to, "data": data}
    if frm:
        p["from"] = frm
    return rpc("eth_call", [p, "latest"])


def erc20_balance(token: str, holder: str) -> int:
    return int(eth_call(token, "0x" + _selector("balanceOf(address)")[2:] + holder[2:].rjust(64, "0").lower()), 16)


def read_pool_state() -> dict:
    slot0 = eth_call(POOL_ADDRESS, _selector("slot0()"))
    liquidity_raw = int(eth_call(POOL_ADDRESS, _selector("liquidity()")), 16)
    tick_spacing_raw = int(eth_call(POOL_ADDRESS, _selector("tickSpacing()")), 16)
    tick_spacing = tick_spacing_raw - (1 << 256) if tick_spacing_raw >= (1 << 255) else tick_spacing_raw
    hexdata = slot0[2:]
    sqrt_price_x96 = int(hexdata[0:64], 16)
    tick_word = int(hexdata[64:128], 16)
    tick = tick_word - (1 << 256) if tick_word >= (1 << 255) else tick_word
    staked_liquidity_raw = int(eth_call(POOL_ADDRESS, _selector("stakedLiquidity()")), 16)
    unstaked_fee_raw = int(eth_call(POOL_ADDRESS, _selector("unstakedFee()")), 16)
    return {"sqrtPriceX96": sqrt_price_x96, "tick": tick, "liquidity_raw": liquidity_raw, "tick_spacing": tick_spacing,
            "staked_liquidity_raw": staked_liquidity_raw, "unstaked_fee_per_million": unstaked_fee_raw}


def price_cbbtc_usd(sqrt_price_x96: int) -> float:
    raw = (sqrt_price_x96 / (2 ** 96)) ** 2
    price_cbbtc_per_usdc = raw * (10 ** (USDC_DECIMALS - CBBTC_DECIMALS))
    return 1.0 / price_cbbtc_per_usdc


def _gt_get_with_retry(url: str, params: dict):
    status, body = None, None
    for attempt in range(GT_RATE_LIMIT_MAX_RETRIES + 1):
        r = requests.get(url, params=params, headers={"Accept": "application/json;version=20230302"}, timeout=20)
        status, body = r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:300])
        if status == 429 and attempt < GT_RATE_LIMIT_MAX_RETRIES:
            time.sleep(GT_RATE_LIMIT_BACKOFF_S)
            continue
        break
    return status, body


def fetch_hourly_volume_usd_since(since_ts_unix: int) -> tuple[float | None, int, int | None, int | None]:
    all_rows: dict[int, list] = {}
    before_ts = None
    hit_older = False
    for _ in range(10):
        params = {"aggregate": 1, "limit": 1000, "currency": "usd", "include_empty_intervals": "true"}
        if before_ts is not None:
            params["before_timestamp"] = before_ts
        status, body = _gt_get_with_retry(f"https://api.geckoterminal.com/api/v2/networks/{GT_NETWORK}/pools/{POOL_ADDRESS}/ohlcv/hour", params)
        if status != 200 or not isinstance(body, dict):
            print(f"[fee_mech] GT hourly OHLCV HTTP {status} -- {str(body)[:200]}")
            break
        rows = body.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        if not rows:
            break
        for row in rows:
            ts = int(row[0])
            if ts >= since_ts_unix:
                all_rows[ts] = row
            else:
                hit_older = True
        oldest_ts = min(int(row[0]) for row in rows)
        if len(rows) < 1000 or hit_older:
            break
        before_ts = oldest_ts
    if not all_rows:
        return None, 0, None, None
    volume_sum = sum(float(row[5]) for row in all_rows.values())
    ts_sorted = sorted(all_rows.keys())
    return volume_sum, len(all_rows), ts_sorted[0], ts_sorted[-1]


def fetch_gt_pool_tvl() -> float | None:
    r = requests.get(f"https://api.geckoterminal.com/api/v2/networks/{GT_NETWORK}/pools/{POOL_ADDRESS}",
                      headers={"Accept": "application/json;version=20230302"}, timeout=20)
    if r.status_code != 200:
        return None
    return float(r.json()["data"]["attributes"]["reserve_in_usd"])


def main():
    result = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    now_ts = int(time.time())

    # ============================= 1. Реальный fee APR пула =============================
    print("=== 1. Реальный fee APR пула из часового объёма GT (24ч и 7д) x 0.033% x 365 / TVL ===")
    vol_24h, n24, e24, l24 = fetch_hourly_volume_usd_since(now_ts - 24 * 3600)
    vol_7d, n7, e7, l7 = fetch_hourly_volume_usd_since(now_ts - 7 * 86400)
    tvl_now = fetch_gt_pool_tvl()
    fee_apr_24h = (vol_24h * POOL_FEE_FRACTION * 365 / tvl_now) if (vol_24h is not None and tvl_now) else None
    fee_apr_7d = ((vol_7d / 7) * POOL_FEE_FRACTION * 365 / tvl_now) if (vol_7d is not None and tvl_now) else None
    print(f"[fee_mech] TVL реально сейчас = ${tvl_now}")
    print(f"[fee_mech] объём 24ч (реально, {n24} часовых свечей) = ${vol_24h} -> fee_apr_24h = {fee_apr_24h}")
    print(f"[fee_mech] объём 7д (реально, {n7} часовых свечей) = ${vol_7d} -> fee_apr_7d = {fee_apr_7d}")
    verdict_1 = None
    if fee_apr_24h is not None:
        pct = fee_apr_24h * 100
        if pct > 50:
            verdict_1 = f"~{pct:.1f}% -- ОЧЕНЬ ВЫСОКИЙ, ближе к гипотезе '180%, наш сбор сломан' -- проверить механизм сбора (см. п.2/3)."
        elif pct < 15:
            verdict_1 = f"~{pct:.1f}% -- НИЗКИЙ, ближе к гипотезе 'DefiLlama врал' -- P6 кандидат на закрытие по предрегистрации."
        else:
            verdict_1 = f"~{pct:.1f}% -- ПРОМЕЖУТОЧНЫЙ, не однозначно ни один из двух крайних исходов."
    print(f"[fee_mech] ВЕРДИКТ П.1: {verdict_1}")
    result["fee_apr"] = {"tvl_usd_now": tvl_now, "volume_24h_usd": vol_24h, "n_candles_24h": n24,
                          "volume_7d_usd": vol_7d, "n_candles_7d": n7,
                          "fee_apr_24h": fee_apr_24h, "fee_apr_7d": fee_apr_7d, "verdict": verdict_1}

    # ============================= 2 и 3. Наши реальные комиссии vs ожидаемые, механика =============================
    print("\n=== 2/3. Реальная механика (исходник CLPool.sol) + наши реальные комиссии vs ожидаемые ===")
    pool = read_pool_state()
    p0 = price_cbbtc_usd(pool["sqrtPriceX96"])
    total_liquidity = pool["liquidity_raw"]
    staked_liquidity = pool["staked_liquidity_raw"]
    unstaked_liquidity = total_liquidity - staked_liquidity
    unstaked_fee_per_million = pool["unstaked_fee_per_million"]
    print(f"[fee_mech] РЕАЛЬНО: liquidity()={total_liquidity} stakedLiquidity()={staked_liquidity} "
          f"unstaked={unstaked_liquidity} ({unstaked_liquidity/total_liquidity*100:.2f}% от total) unstakedFee()={unstaked_fee_per_million}/1_000_000 "
          f"({unstaked_fee_per_million/10000:.2f}%)")

    positions_calldata = "0x" + _selector("positions(uint256)")[2:] + hex(TOKEN_ID)[2:].rjust(64, "0")
    pos_raw = eth_call(NFPM, positions_calldata)
    fields = abi_decode(
        ["uint96", "address", "address", "address", "int24", "int24", "int24",
         "uint128", "uint256", "uint256", "uint128", "uint128"],
        bytes.fromhex(pos_raw[2:]),
    )
    our_liquidity = fields[7]
    print(f"[fee_mech] наша реальная liquidity в позиции {TOKEN_ID} = {our_liquidity}")

    collect_selector = bytes.fromhex(_selector("collect((uint256,address,uint128,uint128))")[2:])
    collect_calldata = collect_selector + abi_encode(
        ["(uint256,address,uint128,uint128)"], [(TOKEN_ID, to_checksum_address(WALLET), 2 ** 128 - 1, 2 ** 128 - 1)],
    )
    collect_resp = rpc("eth_call", [{"to": NFPM, "from": WALLET, "data": "0x" + collect_calldata.hex()}, "latest"])
    fee0_raw, fee1_raw = abi_decode(["uint256", "uint256"], bytes.fromhex(collect_resp[2:]))
    fee0_usdc = fee0_raw / 10 ** USDC_DECIMALS
    fee1_cbbtc = fee1_raw / 10 ** CBBTC_DECIMALS
    real_fees_usd = fee0_usdc + fee1_cbbtc * p0
    print(f"[fee_mech] РЕАЛЬНАЯ eth_call-симуляция collect(MAX) СЕЙЧАС: fee0(USDC)={fee0_usdc} fee1(cbBTC)={fee1_cbbtc} (${real_fees_usd:.6f})")

    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else None
    opened_at_ts = None
    if state and state.get("opened_at_utc"):
        from datetime import datetime
        opened_at_ts = int(datetime.fromisoformat(state["opened_at_utc"].replace("Z", "+00:00")).timestamp())
    vol_since_open, n_since_open, e_since_open, l_since_open = (None, 0, None, None)
    if opened_at_ts:
        vol_since_open, n_since_open, e_since_open, l_since_open = fetch_hourly_volume_usd_since(opened_at_ts)
    print(f"[fee_mech] реальный объём пула с момента открытия позиции ({n_since_open} часовых свечей) = ${vol_since_open}")

    our_share_of_unstaked = (our_liquidity / unstaked_liquidity) if unstaked_liquidity else None
    effective_fee_rate = POOL_FEE_FRACTION * (1 - unstaked_fee_per_million / 1_000_000)
    expected_fees_usd = (vol_since_open * effective_fee_rate * our_share_of_unstaked) if (
        vol_since_open is not None and our_share_of_unstaked is not None) else None
    print(f"[fee_mech] наша доля unstaked-ликвидности = {our_share_of_unstaked} эффективная ставка (после unstakedFee) = {effective_fee_rate}")
    print(f"[fee_mech] ОЖИДАЕМЫЕ комиссии (объём_с_открытия x эффективная_ставка x наша_доля_unstaked) = ${expected_fees_usd}")
    ratio_actual_vs_expected = (real_fees_usd / expected_fees_usd) if expected_fees_usd else None
    print(f"[fee_mech] РЕАЛЬНОЕ/ОЖИДАЕМОЕ = {ratio_actual_vs_expected}")

    result["mechanism"] = {
        "source_confirmed": "CLPool.sol calculateFees()/applyUnstakedFees(), aerodrome-finance/slipstream, WebFetch 2026-09-05 -- "
                             "комиссия делится между staked(gauge)/unstaked(LP), СВЕРХУ unstaked несёт unstakedFee() (per-million) "
                             "в пользу gaugeFee; feeGrowthGlobalX128 += (unstakedAmount-cut)*Q128/(liquidity-stakedLiquidity); "
                             "далее position.update() с feeGrowthInside -- ТА ЖЕ механика, что Uniswap v3 core, "
                             "но от УМЕНЬШЕННОЙ суммы и с ДРУГИМ (меньшим) знаменателем.",
        "total_liquidity_raw": total_liquidity, "staked_liquidity_raw": staked_liquidity,
        "unstaked_liquidity_raw": unstaked_liquidity, "unstaked_fee_per_million": unstaked_fee_per_million,
        "our_liquidity_raw": our_liquidity, "our_share_of_unstaked_liquidity": our_share_of_unstaked,
        "effective_fee_rate_after_unstaked_cut": effective_fee_rate,
    }
    result["our_fees_actual_vs_expected"] = {
        "opened_at_ts": opened_at_ts, "pool_volume_usd_since_open": vol_since_open, "n_hourly_candles_since_open": n_since_open,
        "real_fee0_usdc": fee0_usdc, "real_fee1_cbbtc": fee1_cbbtc, "real_fees_usd": real_fees_usd,
        "expected_fees_usd": expected_fees_usd, "ratio_actual_vs_expected": ratio_actual_vs_expected,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[fee_mech] результат записан в {OUT_PATH}")


if __name__ == "__main__":
    main()
