"""P6 -- одноразовый скан: реальная реализованная годовая sigma cbBTC/USDC
(Aerodrome Slipstream, Base) за последние 30 дней, реальные часовые OHLCV с
GeckoTerminal. Тот же метод, что p5_gt_pool_history.py (владелец назвал
37.47% -- здесь реально перепроверяется, не принимается на веру:
"никогда не выдумывай данные, всегда ищи реальные источники")."""
import json
import math
import time
from pathlib import Path

import requests

OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "p3_guard_cache" / "p6_hist_sigma_30d_result.json"

GT_NETWORK = "base"
GT_POOL_ADDRESS = "0x3e66e55e97ce60096f74b7c475e8249f2d31a9fb"
GT_RATE_LIMIT_BACKOFF_S = 65.0
GT_RATE_LIMIT_MAX_RETRIES = 2
DAYS = 30


def _gt_get_with_retry(url: str, params: dict):
    status, body = None, None
    for attempt in range(GT_RATE_LIMIT_MAX_RETRIES + 1):
        r = requests.get(url, params=params, headers={"Accept": "application/json;version=20230302"}, timeout=20)
        status, body = r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:300])
        if status == 429 and attempt < GT_RATE_LIMIT_MAX_RETRIES:
            print(f"[hist_sigma] 429 (попытка {attempt + 1}) -- жду {GT_RATE_LIMIT_BACKOFF_S:.0f}с")
            time.sleep(GT_RATE_LIMIT_BACKOFF_S)
            continue
        break
    return status, body


def fetch_hourly_closes(days: int) -> list[tuple[int, float]]:
    since_ts = int(time.time()) - days * 86400
    all_rows: dict[int, float] = {}
    before_ts = None
    for _ in range(10):
        params = {"aggregate": 1, "limit": 1000, "currency": "usd", "include_empty_intervals": "true"}
        if before_ts is not None:
            params["before_timestamp"] = before_ts
        status, body = _gt_get_with_retry(f"https://api.geckoterminal.com/api/v2/networks/{GT_NETWORK}/pools/{GT_POOL_ADDRESS}/ohlcv/hour", params)
        if status != 200 or not isinstance(body, dict):
            print(f"[hist_sigma] HTTP {status} -- {str(body)[:200]} -- останов")
            break
        rows = body.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        if not rows:
            break
        hit_older = False
        for row in rows:
            ts = int(row[0])
            close = float(row[4])
            if ts >= since_ts:
                all_rows[ts] = close
            else:
                hit_older = True
        oldest_ts = min(int(row[0]) for row in rows)
        if len(rows) < 1000 or hit_older:
            break
        before_ts = oldest_ts
    return sorted(all_rows.items())


def sigma_realized_annualized(closes: list[tuple[int, float]]) -> dict:
    if len(closes) < 3:
        return {"sigma_realized_annualized": None, "n_points": len(closes), "note": "нужно минимум 3 точки"}
    sum_sq_log_ret = 0.0
    total_years = 0.0
    for (t0, p0), (t1, p1) in zip(closes, closes[1:]):
        dt_years = (t1 - t0) / (365.25 * 24 * 3600)
        if dt_years <= 0 or p0 <= 0 or p1 <= 0:
            continue
        log_ret = math.log(p1 / p0)
        sum_sq_log_ret += log_ret ** 2
        total_years += dt_years
    if total_years <= 0:
        return {"sigma_realized_annualized": None, "n_points": len(closes), "note": "нулевой суммарный интервал"}
    variance_annualized = sum_sq_log_ret / total_years
    return {"sigma_realized_annualized": math.sqrt(variance_annualized), "n_points": len(closes), "total_years_covered": total_years}


def main():
    closes = fetch_hourly_closes(DAYS)
    print(f"[hist_sigma] реально получено {len(closes)} часовых точек за {DAYS} дней "
          f"({time.strftime('%Y-%m-%d', time.gmtime(closes[0][0])) if closes else '?'} .. "
          f"{time.strftime('%Y-%m-%d', time.gmtime(closes[-1][0])) if closes else '?'})")
    info = sigma_realized_annualized(closes)
    print(f"[hist_sigma] реальная реализованная годовая sigma за {DAYS}д = {info.get('sigma_realized_annualized')}")
    result = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "days": DAYS,
              "n_hourly_points": len(closes), **info,
              "owner_stated_value": 0.3747,
              "matches_owner_stated": (abs(info["sigma_realized_annualized"] - 0.3747) < 0.01
                                        if info.get("sigma_realized_annualized") is not None else None)}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[hist_sigma] результат записан в {OUT_PATH}")


if __name__ == "__main__":
    main()
