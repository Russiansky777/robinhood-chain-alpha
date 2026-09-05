#!/usr/bin/env python3
"""Скринер пулов -- ЧЕСТНЫЙ пересчёт fee APR (владелец, 2026-09-05, после
реального закрытия P6): "APR агрегатора -- гипотеза. Перед входом в любой
пул: fee APR = объём (GT, 7д и 30д) x fee tier / TVL, независимо от
DefiLlama. Расхождение > 2x -- стоп и разбор." Плюс правило для
gauge-форков: "доход незастейканной позиции умножается на долю
unstaked-ликвидности и на (1 - unstakedFee), читать с контракта."

Прямая причина: реальный DefiLlama apyBase для пула P6 (aerodrome-slipstream
USDC/cbBTC, index 8 ниже) был 548.58%/278.08%/178.84% (apyBase/apyBase7d/
apyMean30d) -- реальный пересчёт из часового объёма GT дал fee_apr_24h=
14.24%, fee_apr_7d=11.06% (analysis/p6_fee_mechanism_investigation.py,
data/p3_guard_cache/p6_fee_mechanism_investigation_result.json) -- 12-50x
завышение, напрямую приведшее к закрытию P6. Здесь тот же честный пересчёт
делается для ВСЕХ 23 кандидатов скринера, не только P6.

Метод (тот же примитив, что уже реально проверен на P6 в этой сессии, не
новый непроверенный код):
  1. Реальный fee tier -- eth_call на САМ пул (uniswap-v3, aerodrome-
     slipstream -- одна и та же сигнатура `fee()`, подтверждено WebFetch
     реального исходника CLPool.sol: `function fee() public view override
     returns (uint24) { return ICLFactory(factory).getSwapFee(address(this)); }`
     -- т.е. читает ТЕКУЩИЙ реальный fee живьём, не догадка). Для
     aerodrome-v1 (классический AMM) fee тира на самом пуле НЕТ (подтверждено
     WebFetch реального Pool.sol) -- читаем `factory()` и `stable()` с пула,
     затем `getFee(address,bool)` с фабрики (подтверждено WebFetch реального
     PoolFactory.sol, знаменатель 10000 -- `MAX_FEE=300`=3%). Для
     uniswap-v4 -- метод не применяется (тот же честный отказ, что уже в
     pool_screener_concentration.py: адрес от GT это poolId, не контракт).
  2. Реальный часовой объём и close-цены с GT (OHLCV/hour, окно 30 дней) --
     из ОДНОГО и того же набора точек берём: объём за 7д, объём за 30д, и
     РЕАЛЬНУЮ 30-дневную реализованную sigma (тот же метод, что
     p6_hist_sigma_30d.py) -- НЕ используем старую ~41-дневную sigma из
     pool_screener_sigma_lvr_result.json (там окно = 1000 часовых свечей,
     это не "30-дневная sigma", а другое число, честно отдельно).
  3. fee_apr_gt_Xd = (volume_Xd_usd / X) x fee_fraction x 365 / tvl_usd
     (тот же способ аннуализации, что p6_fee_mechanism_investigation.py:
     fee_apr_24h = vol_24h x FEE x 365 / tvl).
  4. Поправка для gauge-форков (aerodrome-slipstream -- staked/unstaked
     механика подтверждена реальным WebFetch CLPool.sol в этой же сессии):
     fee_apr_corrected = fee_apr_gt_Xd x (unstaked_liquidity/total_liquidity)
     x (1 - unstakedFee) -- staked_liquidity и unstaked_fee читаются
     ЖИВЬЁМ с пула (`stakedLiquidity()`, `unstakedFee()`).
     aerodrome-v1 -- честно ПОМЕЧЕНО "поправка не смоделирована" (механика
     классического Solidly-форка другая -- gauge получает свою долю через
     отдельный внутренний учёт per-LP `supplyIndex`, не через
     liquidity-based split, как в CL -- не совпадает с правилом владельца
     дословно, подгонять нельзя): показанное число -- ВЕРХНЯЯ ГРАНИЦА без
     поправки, явно так и подписано.
  5. LVR при РЕАЛЬНОЙ 30-дневной sigma (не старой): LVR = k x sigma_30d^2/8,
     k -- уже посчитанный ранее (pool_screener_concentration_result.json,
     та же формула, что и для P6: k=9.9 совпадает с тем, что владелец
     назвал "9.9 из скринера" -- ПОДТВЕРЖДЕНО, не пересчитывается заново).
  6. ratio = fee_apr_corrected / LVR, порог >=2. Рядом -- старый apyBase
     DefiLlama (frac) и discrepancy_multiplier = apyBase_frac /
     fee_apr_corrected (>1 = агрегатор завышает, <1 = занижает)."""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import requests

IN_PATH = Path("data/p3_guard_cache/pool_screener_concentration_result.json")
OUT_PATH = Path("data/p3_guard_cache/pool_screener_gt_recompute_result.json")

RPC_ENDPOINTS = {
    "base": "https://mainnet.base.org",
    "arbitrum": "https://arb1.arbitrum.io/rpc",
    "bsc": "https://bsc-dataseed.binance.org/",
}

GT_RATE_LIMIT_BACKOFF_S = 65.0
GT_RATE_LIMIT_MAX_RETRIES = 2
GT_MIN_INTERVAL_S = 2.6
RPC_MIN_INTERVAL_S = 1.5
RPC_RETRY_BACKOFF_S = 15.0
RPC_MAX_RETRIES = 3
DAYS_WINDOW = 30

_last_gt_call = 0.0
_last_rpc_call: dict[str, float] = {}


def _selector(sig: str) -> str:
    from Crypto.Hash import keccak
    k = keccak.new(digest_bits=256)
    k.update(sig.encode())
    return "0x" + k.hexdigest()[:8]


def _throttle_gt() -> None:
    global _last_gt_call
    wait = _last_gt_call + GT_MIN_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_gt_call = time.monotonic()


def _throttle_rpc(network: str) -> None:
    last = _last_rpc_call.get(network, 0.0)
    wait = last + RPC_MIN_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_rpc_call[network] = time.monotonic()


def rpc_call(network: str, to: str, data: str) -> str:
    for attempt in range(RPC_MAX_RETRIES + 1):
        _throttle_rpc(network)
        r = requests.post(RPC_ENDPOINTS[network],
                           json={"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": to, "data": data}, "latest"]},
                           timeout=20)
        if r.status_code == 429 and attempt < RPC_MAX_RETRIES:
            print(f"    RPC {network} 429, жду {RPC_RETRY_BACKOFF_S:.0f}с")
            time.sleep(RPC_RETRY_BACKOFF_S)
            continue
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"eth_call {to} {data}: {body['error']}")
        return body["result"]
    raise RuntimeError(f"RPC {network} 429 после {RPC_MAX_RETRIES + 1} попыток")


def _gt_get_with_retry(url: str, params: dict) -> tuple[int | None, dict | str | None]:
    status, body = None, None
    for attempt in range(GT_RATE_LIMIT_MAX_RETRIES + 1):
        _throttle_gt()
        try:
            r = requests.get(url, params=params, headers={"Accept": "application/json;version=20230302"}, timeout=20)
            status, body = r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:300])
        except Exception as e:  # noqa: BLE001
            return None, str(e)[:300]
        if status == 429 and attempt < GT_RATE_LIMIT_MAX_RETRIES:
            print(f"    GT 429, жду {GT_RATE_LIMIT_BACKOFF_S:.0f}с")
            time.sleep(GT_RATE_LIMIT_BACKOFF_S)
            continue
        break
    return status, body


def fetch_hourly_ohlcv_30d(network: str, pool_address: str) -> list[tuple[int, float, float]]:
    """(ts, close_usd, volume_usd) за последние DAYS_WINDOW дней, реально с GT."""
    since_ts = int(time.time()) - DAYS_WINDOW * 86400
    all_rows: dict[int, tuple[float, float]] = {}
    before_ts = None
    for _ in range(5):
        params = {"aggregate": 1, "limit": 1000, "currency": "usd", "include_empty_intervals": "true"}
        if before_ts is not None:
            params["before_timestamp"] = before_ts
        status, body = _gt_get_with_retry(f"https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool_address}/ohlcv/hour", params)
        if status != 200 or not isinstance(body, dict):
            print(f"    GT hourly OHLCV: HTTP {status} -- {str(body)[:200]}")
            break
        rows = body.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        if not rows:
            break
        hit_older = False
        for row in rows:
            ts = int(row[0])
            if ts >= since_ts:
                all_rows[ts] = (float(row[4]), float(row[5]))
            else:
                hit_older = True
        oldest_ts = min(int(row[0]) for row in rows)
        if len(rows) < 1000 or hit_older:
            break
        before_ts = oldest_ts
    return sorted((ts, c, v) for ts, (c, v) in all_rows.items())


def sigma_realized_annualized(closes: list[tuple[int, float]]) -> float | None:
    if len(closes) < 3:
        return None
    sum_sq_log_ret, total_years = 0.0, 0.0
    for (t0, p0), (t1, p1) in zip(closes, closes[1:]):
        dt_years = (t1 - t0) / (365.25 * 24 * 3600)
        if dt_years <= 0 or p0 <= 0 or p1 <= 0:
            continue
        sum_sq_log_ret += math.log(p1 / p0) ** 2
        total_years += dt_years
    if total_years <= 0:
        return None
    return math.sqrt(sum_sq_log_ret / total_years)


def get_real_fee_fraction(project: str, network: str, pool_address: str) -> dict:
    r = {"fee_fraction": None, "error": None, "method": None}
    try:
        if project in ("uniswap-v3", "aerodrome-slipstream"):
            fee_raw = int(rpc_call(network, pool_address, _selector("fee()")), 16)
            r["fee_fraction"] = fee_raw / 1_000_000
            r["method"] = "pool.fee() / 1e6 (Uniswap v3 / Aerodrome Slipstream CLPool -- одна и та же сигнатура, подтверждено WebFetch CLPool.sol)"
        elif project == "aerodrome-v1":
            factory_addr = "0x" + rpc_call(network, pool_address, _selector("factory()"))[-40:]
            stable_raw = int(rpc_call(network, pool_address, _selector("stable()")), 16)
            stable = bool(stable_raw)
            data = _selector("getFee(address,bool)")[2:] + pool_address[2:].rjust(64, "0").lower() + str(int(stable)).rjust(64, "0")
            fee_raw = int(rpc_call(network, factory_addr, "0x" + data), 16)
            r["fee_fraction"] = fee_raw / 10_000
            r["method"] = "factory.getFee(pool, stable) / 1e4 (Aerodrome v1 classic AMM -- подтверждено WebFetch Pool.sol + PoolFactory.sol)"
            r["stable"] = stable
            r["factory_address"] = factory_addr
        else:
            r["error"] = f"проект '{project}' -- метод чтения fee tier не реализован (uniswap-v4: см. concentration-скринер, не применяется)"
    except Exception as exc:  # noqa: BLE001
        r["error"] = f"eth_call упал: {str(exc)[:300]}"
    return r


def get_gauge_correction(project: str, network: str, pool_address: str, total_liquidity_raw: int | None) -> dict:
    """Поправка на staked/unstaked -- ТОЛЬКО для aerodrome-slipstream (CL),
    механика подтверждена реальным WebFetch в этой сессии. Для остальных --
    честно "не применяется" / "не смоделирована", без подгонки."""
    if project != "aerodrome-slipstream":
        return {"applicable": False, "reason": "поправка реализована только для CL (Aerodrome Slipstream) -- staked/unstaked liquidity-based split подтверждён WebFetch CLPool.sol; для aerodrome-v1 (classic) механика другая (per-LP supplyIndex), не смоделирована -- число ниже показывается БЕЗ поправки, как верхняя граница"}
    try:
        staked_raw = int(rpc_call(network, pool_address, _selector("stakedLiquidity()")), 16)
        unstaked_fee_raw = int(rpc_call(network, pool_address, _selector("unstakedFee()")), 16)
        if total_liquidity_raw is None or total_liquidity_raw == 0:
            return {"applicable": True, "error": "total_liquidity_raw неизвестен (нет в кэше concentration)"}
        unstaked_share = (total_liquidity_raw - staked_raw) / total_liquidity_raw
        unstaked_fee_frac = unstaked_fee_raw / 1_000_000
        return {
            "applicable": True, "staked_liquidity_raw": staked_raw, "total_liquidity_raw": total_liquidity_raw,
            "unstaked_share": unstaked_share, "unstaked_fee_per_million": unstaked_fee_raw,
            "unstaked_fee_frac": unstaked_fee_frac, "correction_multiplier": unstaked_share * (1 - unstaked_fee_frac),
        }
    except Exception as exc:  # noqa: BLE001
        return {"applicable": True, "error": f"eth_call упал: {str(exc)[:300]}"}


def process_one(c: dict) -> dict:
    entry = {"pool_id": c["pool_id"], "project": c["project"], "chain": c["chain"], "symbol": c["symbol"],
              "tvl_usd": c["tvl_usd"], "apyBase_defillama_pct": c.get("apyBase"),
              "apyBase7d_defillama_pct": None, "apyMean30d_defillama_pct": None,
              "k_from_concentration_screener": c.get("concentration", {}).get("k")}
    gt = c.get("gt_resolution") or {}
    network, address = gt.get("network"), gt.get("resolved_pool_address")
    project = c["project"]

    if project == "uniswap-v4":
        entry["error"] = "uniswap-v4 -- singleton PoolManager, GT-адрес это poolId (bytes32), не пул-контракт -- fee()/OHLCV-по-этому-адресу метод не применяется (тот же честный отказ, что в pool_screener_concentration.py)"
        return entry
    if not network or not address or len(address) != 42:
        entry["error"] = f"нет валидного адреса пула на сети (network={network}, address={address})"
        return entry

    fee_info = get_real_fee_fraction(project, network, address)
    entry["fee_tier"] = fee_info

    print(f"    объём/close за {DAYS_WINDOW}д с GT...")
    rows = fetch_hourly_ohlcv_30d(network, address)
    if not rows:
        entry["error"] = (entry.get("error") or "") + " | GT hourly OHLCV пуст (нет свечей за 30д)"
        return entry
    now_ts = int(time.time())
    vol_7d = sum(v for ts, _, v in rows if ts >= now_ts - 7 * 86400)
    vol_30d = sum(v for ts, _, v in rows if ts >= now_ts - 30 * 86400)
    closes = [(ts, c_) for ts, c_, _ in rows]
    sigma_30d = sigma_realized_annualized(closes)
    entry["gt_window"] = {"n_hourly_points": len(rows), "volume_7d_usd": vol_7d, "volume_30d_usd": vol_30d,
                            "sigma_realized_annualized_30d_real": sigma_30d,
                            "oldest_point_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(rows[0][0])),
                            "newest_point_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(rows[-1][0]))}

    fee_frac = fee_info.get("fee_fraction")
    tvl = c["tvl_usd"]
    if fee_frac is not None and tvl:
        fee_apr_7d = (vol_7d / 7) * fee_frac * 365 / tvl
        fee_apr_30d = (vol_30d / 30) * fee_frac * 365 / tvl
        entry["fee_apr_gt_7d_raw"] = fee_apr_7d
        entry["fee_apr_gt_30d_raw"] = fee_apr_30d

        total_liq_raw = c.get("concentration", {}).get("liquidity_raw")
        gauge = get_gauge_correction(project, network, address, total_liq_raw)
        entry["gauge_correction"] = gauge
        mult = gauge.get("correction_multiplier") if gauge.get("applicable") and gauge.get("error") is None else 1.0
        entry["fee_apr_gt_7d_corrected"] = fee_apr_7d * mult
        entry["fee_apr_gt_30d_corrected"] = fee_apr_30d * mult
        entry["gauge_correction_applied"] = (project == "aerodrome-slipstream" and gauge.get("error") is None)

        k = entry["k_from_concentration_screener"]
        if k is not None and sigma_30d is not None:
            lvr_30d = k * (sigma_30d ** 2) / 8
            entry["lvr_at_real_30d_sigma_frac"] = lvr_30d
            entry["ratio_to_lvr_real_30d_sigma"] = (entry["fee_apr_gt_30d_corrected"] / lvr_30d) if lvr_30d else None
            entry["passes_threshold_2x_gt_real"] = (entry["ratio_to_lvr_real_30d_sigma"] is not None
                                                       and entry["ratio_to_lvr_real_30d_sigma"] >= 2.0)

        apy_base_frac = (c.get("apyBase") or 0.0) / 100.0
        entry["discrepancy_multiplier_defillama_over_gt_real"] = (
            apy_base_frac / entry["fee_apr_gt_30d_corrected"] if entry["fee_apr_gt_30d_corrected"] else None
        )
    else:
        entry["error"] = (entry.get("error") or "") + f" | fee_tier недоступен: {fee_info.get('error')}"
    return entry


def run() -> int:
    data = json.loads(IN_PATH.read_text())
    candidates = data["candidates"]
    results = []
    for i, c in enumerate(candidates):
        print(f"\n=== {i+1}/{len(candidates)}: {c['chain']} {c['project']} {c['symbol']} (pool_id={c['pool_id']}) ===")
        try:
            entry = process_one(c)
        except Exception as exc:  # noqa: BLE001
            entry = {"pool_id": c["pool_id"], "project": c["project"], "chain": c["chain"], "symbol": c["symbol"],
                      "error": f"необработанное исключение: {str(exc)[:300]}"}
        ratio = entry.get("ratio_to_lvr_real_30d_sigma")
        disc = entry.get("discrepancy_multiplier_defillama_over_gt_real")
        print(f"    fee_apr_30d_corrected={entry.get('fee_apr_gt_30d_corrected')} ratio={ratio} "
              f"disc_x_defillama={disc} pass={entry.get('passes_threshold_2x_gt_real')} error={entry.get('error')}")
        results.append(entry)

    ok = [r for r in results if r.get("ratio_to_lvr_real_30d_sigma") is not None]
    ok.sort(key=lambda r: -r["ratio_to_lvr_real_30d_sigma"])
    n_pass = sum(1 for r in ok if r["passes_threshold_2x_gt_real"])

    print(f"\n[gt_recompute] честно посчитано {len(ok)}/{len(results)}, проходят порог >=2 (реальный GT-объём): {n_pass}")
    print("=== Отсортировано по ratio_to_lvr_real_30d_sigma (убывание) ===")
    for r in ok:
        print(f"  {r['chain']:10} {r['project']:22} {r['symbol']:22} "
              f"fee_apr_30d_corr={r['fee_apr_gt_30d_corrected']*100:.2f}% ratio={r['ratio_to_lvr_real_30d_sigma']:.2f} "
              f"defillama_apyBase={r.get('apyBase_defillama_pct')}% disc_x={r.get('discrepancy_multiplier_defillama_over_gt_real')} "
              f"pass={r['passes_threshold_2x_gt_real']}")

    out = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "n_total": len(results), "n_computed": len(ok), "n_pass_threshold_2x_gt_real": n_pass,
           "candidates": results}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[gt_recompute] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
