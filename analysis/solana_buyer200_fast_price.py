#!/usr/bin/env python3
"""Solana buyer_200 -- Шаг 0/1: ускоренный метод (транзакция вместо блока)
+ точечная сверка с уже готовыми ценами другого ИИ.

Метод НЕ меняется (владелец: "их метод правильный, менять не надо") --
цена = последняя сделка в пуле до момента t, из события свопа; вход =
факт. расход/чистые токены; движение считается от котировки +5с; zero/add
разделение. Меняется ТОЛЬКО способ получения данных:

- `engine.decode_tx(tx)` / `engine.expected_pool_counts(tx)` в их
  `engine.py` УЖЕ работают на ОДНОЙ транзакции (не на блоке) -- их же
  `block_index.summary(slot)` просто скачивает ВЕСЬ блок и прогоняет ТЕ
  ЖЕ функции по каждой транзакции блока, раскладывая по пулам. Раз
  `getSignaturesForAddress(pool)` и так перечисляет ВСЕ его транзакции в
  истинном порядке исполнения -- берём точечный `getTransaction` по
  подписи вместо полного блока. Тот же своп, то же событие, та же цена,
  на порядки меньше данных.
- Троттлинг -- нормальный backoff на 429/5xx, не фиксированные 0.25+1.5с
  на КАЖДЫЙ запрос (у нас свой RPC-доступ, не публичный узел, который их
  резал).
- История пула -- одна на все точки/покупки этого пула (кэш в памяти +
  на диске), не по разу на каждую покупку.

Импортирует `engine`/`price_points.price_event` НАПРЯМУЮ из присланного
архива (data/solana_buyer_200/prior/current/buyer_100) -- декодирование
события НЕ переписано, только способ его получения."""
from __future__ import annotations

import bisect
import hashlib
import json
import os
import sys
import time
from decimal import Decimal as D
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
PRIOR_ROOT = REPO_ROOT / "data" / "solana_buyer_200" / "prior" / "current" / "buyer_100"
OUT_ROOT = REPO_ROOT / "data" / "solana_buyer_200"
CACHE_DIR = OUT_ROOT / "rpc_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(PRIOR_ROOT))
import engine  # noqa: E402  (их decode_tx/expected_pool_counts, НЕ переписаны)

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
HORIZONS_SECONDS = [5, 15, 30, 60, 180, 300]  # секундные точки (задание владельца, Шаг 0) -- длинные горизонты отдельно, через GeckoTerminal

META_PATH = PRIOR_ROOT / "route_meta.json"
META: dict = json.loads(META_PATH.read_text()) if META_PATH.exists() else {}

# --- RPC: throttle с бэкоффом, НЕ фиксированные паузы их rpc.py ---
_PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
_MIN_INTERVAL_S = 0.12  # стартовая цель ~8 req/s -- честно НЕ "долбить", но и не 0.25+1.5=1.75с на запрос
_last_call_at = 0.0
_backoff_s = 0.0


def _endpoint() -> str:
    key = os.environ.get("ALCHEMY_API_KEY", "")
    if key:
        return f"https://solana-mainnet.g.alchemy.com/v2/{key}"
    return _PUBLIC_RPC


def _cache_path(method: str, params: list) -> Path:
    key = hashlib.sha256(json.dumps([method, params], sort_keys=True, default=str).encode()).hexdigest()[:32]
    return CACHE_DIR / f"{method}_{key}.json"


def rpc_call(method: str, params: list, use_cache: bool = True) -> dict:
    global _last_call_at, _backoff_s
    cache_f = _cache_path(method, params)
    if use_cache and cache_f.exists():
        try:
            return json.loads(cache_f.read_text())
        except (ValueError, OSError):
            pass
    url = _endpoint()
    fallback_used = False
    for attempt in range(8):
        wait = max(_MIN_INTERVAL_S - (time.monotonic() - _last_call_at), 0.0) + _backoff_s
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()
        try:
            resp = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                  timeout=30)
        except Exception:  # noqa: BLE001
            _backoff_s = min(max(_backoff_s * 2, 0.5), 10.0)
            continue
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            _backoff_s = min(max(_backoff_s * 2, 0.5), 10.0)
            continue
        if resp.status_code in (401, 403) and not fallback_used and url != _PUBLIC_RPC:
            url = _PUBLIC_RPC
            fallback_used = True
            continue
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        if "error" in body:
            err = body["error"]
            msg = str(err.get("message", "")).lower()
            if "rate" in msg or "429" in msg:
                _backoff_s = min(max(_backoff_s * 2, 0.5), 10.0)
                continue
            raise RuntimeError(f"RPC error {method}: {err}")
        _backoff_s = max(_backoff_s * 0.5, 0.0)  # успех -- ослабляем бэкофф
        result = body.get("result")
        if use_cache:
            cache_f.write_text(json.dumps(result))
        return result
    raise RuntimeError(f"RPC {method} исчерпал попытки (последний backoff={_backoff_s}с)")


def get_signatures_for_address(address: str, before: str | None = None, limit: int = 1000) -> list[dict]:
    opts: dict = {"limit": limit}
    if before:
        opts["before"] = before
    return rpc_call("getSignaturesForAddress", [address, opts], use_cache=False) or []


def get_transaction(sig: str) -> dict | None:
    return rpc_call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])


# --- история пула: ОДНА на пул, переиспользуется для всех точек/покупок этого пула ---
_pool_history_cache: dict[str, list[dict]] = {}


def pool_history(pool: str, max_pages: int = 40) -> list[dict]:
    """Полная (или до max_pages*1000) история подписей пула, newest-first,
    как отдаёт сама getSignaturesForAddress -- кэшируется В ПАМЯТИ на весь
    прогон процесса, так что 39 покупок одного минта = ОДИН реальный проход
    по истории его пула, не 39 (Шаг 0.3)."""
    if pool in _pool_history_cache:
        return _pool_history_cache[pool]
    disk_cache = CACHE_DIR / f"pool_history_{pool}.json"
    if disk_cache.exists():
        try:
            hist = json.loads(disk_cache.read_text())
            _pool_history_cache[pool] = hist
            return hist
        except (ValueError, OSError):
            pass
    hist: list[dict] = []
    before = None
    for _ in range(max_pages):
        page = get_signatures_for_address(pool, before=before, limit=1000)
        if not page:
            break
        hist.extend(page)
        before = page[-1]["signature"]
        if len(page) < 1000:
            break
    _pool_history_cache[pool] = hist
    disk_cache.write_text(json.dumps(hist))
    return hist


def price_event(e: dict) -> dict:
    """ТА ЖЕ логика, что price_points.price_event() в присланном архиве --
    портирована буквально (не переписана), только rpc() заменён на НАШ
    throttled rpc_call(). CLMM/launch/DLMM резолвятся через getAccountInfo,
    как и у них; META (route_meta.json) переиспользуется как стартовый кэш
    -- то же самое, реальное состояние, которое они уже накопили."""
    if e.get("p1_per_0") is not None:
        return e
    m = META.get(e["pool"], {})
    if e["kind"] == "cl" and m.get("d0") is not None and m.get("d1") is not None:
        e.update(m0=m["m0"], m1=m["m1"], d0=m["d0"], d1=m["d1"],
                  p1_per_0=str((D(e["sqrt"]) / D(2**64)) ** 2 * D(10) ** (m["d0"] - m["d1"])), status="ok")
        return e
    if e["kind"] == "launch":
        a = rpc_call("getAccountInfo", [e["config"], {"encoding": "base64"}])
        if not a or not a.get("value"):
            return e
        import base64
        raw = base64.b64decode(a["value"]["data"][0])
        e["curve_type"] = raw[16]
        if raw[16] != 0:
            e["status"] = "unsupported_launch_curve"
            return e
        v = e["event"]
        if v["pool_status"] != 0:
            e["status"] = "launch_migration_requires_new_pool"
            return e
        if e.get("d0") is None or e.get("d1") is None:
            return e
        e.update(p1_per_0=str(D(v["virtual_quote"] + v["real_quote_after"]) / D(v["virtual_base"] - v["real_base_after"])
                              * D(10) ** (e["d0"] - e["d1"])), status="ok")
        return e
    if e["kind"] == "dl":
        if "step" not in m:
            a = rpc_call("getAccountInfo", [e["pool"], {"encoding": "base64"}])
            if not a or not a.get("value"):
                return e
            import base64
            import struct
            raw = base64.b64decode(a["value"]["data"][0])
            m.update(step=struct.unpack_from("<H", raw, 80)[0], m0=engine.b58(raw[88:120]), m1=engine.b58(raw[120:152]))
            META[e["pool"]] = m
        d0 = e.get("d0", m.get("d0"))
        d1 = e.get("d1", m.get("d1"))
        if d0 is None or d1 is None:
            return e
        e.update(m0=m["m0"], m1=m["m1"], d0=d0, d1=d1,
                  p1_per_0=str((D(1) + D(m["step"]) / 10000) ** e["event"]["end_bin"] * D(10) ** (d0 - d1)), status="ok")
    return e


def find_price_at(pool: str, t: int, max_scanned: int = 120) -> dict:
    """Замена их point(pool,t) -- ТА ЖЕ семантика (последняя сделка до t,
    честный статус missing_swap_event/no_historical_swap), но по
    ОТДЕЛЬНЫМ транзакциям через getTransaction, не по полным блокам."""
    hist = pool_history(pool)
    # Первая подпись с blockTime<=t (hist -- newest-first) -- бинарный поиск,
    # т.к. вся история пула уже загружена целиком (Шаг 0.3: один проход).
    times = [-(h["blockTime"] or -(10**18)) for h in hist]  # отриц. для monotonic возрастания при newest-first
    idx = bisect.bisect_left(times, -t)
    scanned = 0
    for h in hist[idx:]:
        if h.get("err") is not None or h.get("blockTime") is None or h["blockTime"] > t:
            continue
        scanned += 1
        tx = get_transaction(h["signature"])
        if tx is None:
            continue
        ev = engine.decode_tx(tx)
        pool_events = [e for e in ev if e.get("pool") == pool]
        expected = engine.expected_pool_counts(tx).get(pool, 0)
        if pool_events:
            e = price_event(pool_events[-1])
            return dict(pool=pool, target=t, slot=tx["slot"], signature=h["signature"],
                        time=tx["blockTime"], age_seconds=t - tx["blockTime"], scanned=scanned,
                        event=e, status=e.get("status", "unknown"))
        if expected > 0:
            return dict(pool=pool, target=t, slot=tx["slot"], signature=h["signature"],
                        status="missing_swap_event", scanned=scanned)
        if scanned >= max_scanned:
            return dict(pool=pool, target=t, status=f"{max_scanned}_txs_without_decoded_swap", scanned=scanned)
    return dict(pool=pool, target=t, status="no_historical_swap", scanned=scanned)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["step1"], default="step1")
    ap.add_argument("--n", type=int, default=5)
    args = ap.parse_args()

    if args.mode == "step1":
        rows = {r["signature"]: r for r in json.loads((PRIOR_ROOT / "selected.json").read_text())}
        routes = {r["signature"]: r["route"] for r in json.loads((PRIOR_ROOT / "routes.json").read_text())}
        reference = json.loads((PRIOR_ROOT / "price_points.json").read_text())
        ref_by_sig_sec = {(r["signature"], r["seconds"]): r for r in reference}
        target_sigs = json.loads((PRIOR_ROOT / "analysis.json").read_text())["meta"]["full_5min_signatures"][:args.n]

        comparison = []
        for sig in target_sigs:
            row = rows[sig]
            route = routes.get(sig)
            for sec in HORIZONS_SECONDS:
                t = row["time"] + sec
                ref = ref_by_sig_sec.get((sig, sec), {})
                if not route:
                    comparison.append(dict(signature=sig, seconds=sec, status="no_route"))
                    continue
                value = D(1)
                legs_out = []
                ok = True
                for leg in route:
                    p = find_price_at(leg["pool"], t)
                    legs_out.append(p)
                    if p.get("status") != "ok":
                        ok = False
                        break
                    e = p["event"]
                    v = D(e["p1_per_0"])
                    if leg["from"] == e["m0"] and leg["to"] == e["m1"]:
                        value *= v
                    elif leg["from"] == e["m1"] and leg["to"] == e["m0"]:
                        value /= v
                    else:
                        ok = False
                        break
                mine_price = str(value) if ok else None
                mine_slot = legs_out[-1].get("slot") if legs_out else None
                mine_sig = legs_out[-1].get("signature") if legs_out else None
                ref_leg = (ref.get("legs") or [{}])[-1] if ref.get("legs") else {}
                match_price = (mine_price is not None and ref.get("price_usdc") is not None
                               and D(mine_price) == D(ref["price_usdc"]))
                match_slot = mine_slot == ref_leg.get("slot")
                match_sig = mine_sig == ref_leg.get("signature")
                comparison.append(dict(
                    signature=sig, seconds=sec,
                    mine_status="ok" if ok else (legs_out[-1].get("status") if legs_out else "no_legs"),
                    mine_price=mine_price, mine_slot=mine_slot, mine_signature=mine_sig,
                    ref_status=ref.get("status"), ref_price=ref.get("price_usdc"),
                    ref_slot=ref_leg.get("slot"), ref_signature=ref_leg.get("signature"),
                    match_price=match_price, match_slot=match_slot, match_signature=match_sig,
                    legs=legs_out,
                ))
                print(f"[step1] {sig[:12]}.. sec={sec} mine={mine_price} ref={ref.get('price_usdc')} "
                      f"match_price={match_price} match_slot={match_slot}", flush=True)

        out_path = OUT_ROOT / "step1_comparison_result.json"
        out_path.write_text(json.dumps(comparison, indent=2, default=str, ensure_ascii=False))
        META_PATH.write_text(json.dumps(META, indent=2))
        n_total = len(comparison)
        n_match = sum(1 for c in comparison if c.get("match_price") and c.get("match_slot") and c.get("match_signature"))
        print(json.dumps({"n_total": n_total, "n_full_match": n_match, "out_path": str(out_path)}, indent=2))
