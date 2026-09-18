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
_MIN_INTERVAL_S = 0.12  # стартовая цель ~8 req/s -- честно НЕ "долбить", но и не 0.25+1.5=1.75с на запрос; уточняется alchemy_available()
_last_call_at = 0.0
_backoff_s = 0.0
_alchemy_disabled = False  # см. rpc_call: 401/403 от Alchemy -- не бить туда КАЖДЫЙ раз впустую


def _endpoint() -> str:
    key = os.environ.get("ALCHEMY_API_KEY", "")
    if key and not _alchemy_disabled:
        return f"https://solana-mainnet.g.alchemy.com/v2/{key}"
    return _PUBLIC_RPC


def alchemy_available() -> bool:
    """Один дешёвый вызов на старте (getHealth), НЕ печатает ключ. Найдено
    при диагностике Шага 1: выданный ALCHEMY_API_KEY имел выключенную сеть
    SOLANA_MAINNET (403 на каждый вызов) -- если владелец её включил,
    Alchemy даёт заметно более высокий лимит частоты, чем публичный узел,
    и потолок троттлинга можно honestly поднять, а не только резервный
    fallback на 403."""
    global _MIN_INTERVAL_S
    if not os.environ.get("ALCHEMY_API_KEY") or _alchemy_disabled:
        return False
    try:
        rpc_call("getHealth", [], use_cache=False)
    except RuntimeError:
        return False
    _MIN_INTERVAL_S = 0.03
    return not _alchemy_disabled


def _cache_path(method: str, params: list) -> Path:
    key = hashlib.sha256(json.dumps([method, params], sort_keys=True, default=str).encode()).hexdigest()[:32]
    return CACHE_DIR / f"{method}_{key}.json"


def rpc_call(method: str, params: list, use_cache: bool = True) -> dict:
    global _last_call_at, _backoff_s, _alchemy_disabled
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
            # Найдено при диагностике Шага 1: у выданного ALCHEMY_API_KEY
            # сеть SOLANA_MAINNET не включена в приложении -- это ПОСТОЯННАЯ
            # (не временная) 403 на каждый вызов. Один раз падаем на public,
            # дальше не долбим Alchemy впустую весь оставшийся прогон.
            url = _PUBLIC_RPC
            fallback_used = True
            _alchemy_disabled = True
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


def get_block_signatures(slot: int) -> dict | None:
    """Минимальный getBlock -- только подписи блока (без транзакций), для
    поиска ЯКОРЯ курсора пагинации (см. find_anchor_after)."""
    try:
        return rpc_call("getBlock", [slot, {"transactionDetails": "signatures", "rewards": False,
                                             "maxSupportedTransactionVersion": 1}])
    except RuntimeError as exc:
        if "skipped" in str(exc).lower() or "not available" in str(exc).lower() or "-32004" in str(exc) or "-32007" in str(exc):
            return None
        raise


def find_anchor_after(hi_time: int, safety_margin_s: int = 60) -> tuple[str, int]:
    """Подпись строго новее (hi_time+safety_margin_s) -- служит курсором
    before= для getSignaturesForAddress ЛЮБОГО адреса (RPC не проверяет,
    что курсор принадлежит опрашиваемому аккаунту -- это просто точка на
    временной шкале). Оцениваем нужный слот линейной экстраполяцией от
    текущей головы сети (getSlot -- один дешёвый вызов), затем ПРОВЕРЯЕМ
    реальным blockTime блока и подправляем при недолёте -- так якорь
    гарантированно окажется НЕ РАНЬШЕ нужного момента (Шаг 0.2, владелец:
    "найти подпись примерно на t+300с... как anchor в их price_points.py,
    но окно ограничивать в самом запросе")."""
    target = hi_time + safety_margin_s
    now = int(time.time())
    head_slot = rpc_call("getSlot", [{"commitment": "finalized"}], use_cache=False)
    rate = 2.5  # слотов/сек, средняя по mainnet -- стартовая оценка, уточняется по факту ниже
    guess = max(int(head_slot - (now - target) * rate), 1)
    prev_guess_bt: tuple[int, int] | None = None
    for _ in range(8):
        block = None
        found_slot = None
        for s in range(guess, guess + 30):
            block = get_block_signatures(s)
            if block and block.get("signatures") and block.get("blockTime") is not None:
                found_slot = s
                break
        if block is None or found_slot is None:
            guess += 30
            continue
        bt = block["blockTime"]
        if bt >= target:
            return block["signatures"][0], found_slot
        if prev_guess_bt is not None:
            d_slot = found_slot - prev_guess_bt[0]
            d_time = bt - prev_guess_bt[1]
            if d_slot > 0 and d_time > 0:
                rate = d_slot / d_time
        prev_guess_bt = (found_slot, bt)
        guess = found_slot + max(int((target - bt) * rate) + 5, 30)
    raise RuntimeError(f"не удалось найти якорь новее t={target}")


# --- окно истории пула: сегменты [lo,hi], НЕПЕРЕСЕКАЮЩИЕСЯ, каждый на
# группу покупок текущего прогона, чьи диапазоны реально смыкаются
# (Шаг 0.2+0.3) ---
_pool_window_cache: dict[tuple[str, int, int], list[dict]] = {}


def _segment_cache_path(pool: str, lo_time: int, hi_time: int) -> Path:
    return CACHE_DIR / f"pool_window_{pool}_{lo_time}_{hi_time}.json"


def ensure_pool_window(pool: str, lo_time: int, hi_time: int) -> list[dict]:
    """История подписей пула, newest-first, ограниченная РОВНО этим
    сегментом [lo_time .. hi_time+запас] -- НЕ от текущего момента назад
    (это и было найденным на Шаге 1 багом: активный пул общего минта
    6GmAFSYs... копит десятки тысяч подписей за часы между сбором данных
    и прогоном скрипта). Находим якорь строго новее hi_time
    (find_anchor_after) и листаем НАЗАД строго до lo_time -- окно в
    сотни/тысячи подписей вместо десятков тысяч.

    Кэш -- НА КОНКРЕТНЫЙ СЕГМЕНТ (pool,lo,hi), не просто на пул: если бы
    кэшировали по одному пулу, то у ОБЩЕГО для многих покупок, но при
    этом ещё и сетевого-хайтрафик пула (например, SOL/USDC-пул как
    промежуточное звено маршрута) окно растянулось бы на ВЕСЬ период
    сбора данных между двумя далёкими друг от друга покупками, включая
    часы мёртвого времени между ними -- на пуле с ~100+ tx/с это давало
    бы сотни тысяч лишних подписей. pool_windows_needed() уже разбивает
    диапазоны на НЕПЕРЕСЕКАЮЩИЕСЯ сегменты -- здесь просто фиксированный
    сегмент, без попытки расширения задним числом."""
    key = (pool, lo_time, hi_time)
    hist = _pool_window_cache.get(key)
    if hist is not None:
        return hist
    p = _segment_cache_path(pool, lo_time, hi_time)
    if p.exists():
        try:
            hist = json.loads(p.read_text())
            _pool_window_cache[key] = hist
            return hist
        except (ValueError, OSError):
            pass

    anchor_sig, _anchor_slot = find_anchor_after(hi_time)
    hist = []
    before = anchor_sig
    while True:
        page = get_signatures_for_address(pool, before=before, limit=1000)
        if not page:
            break
        hist.extend(page)
        oldest = page[-1].get("blockTime")
        before = page[-1]["signature"]
        if oldest is not None and oldest <= lo_time:
            break
        if len(page) < 1000:
            break
    _pool_window_cache[key] = hist
    p.write_text(json.dumps(hist))
    return hist


def pool_windows_needed(target_sigs: list[str], rows: dict, routes: dict, max_sec: int) -> dict[str, list[tuple[int, int]]]:
    """НЕПЕРЕСЕКАЮЩИЕСЯ сегменты [lo,hi] на пул -- объединяем только
    покупки текущего прогона, чьи диапазоны РЕАЛЬНО пересекаются или
    соприкасаются (39/100 покупок делят один минт и, как правило, идут
    плотно во времени -- один проход, не 39), но НЕ сливаем покупки,
    разнесённые по времени на часы через один и тот же
    высокотрафиковый промежуточный пул (см. ensure_pool_window) -- иначе
    получили бы одно окно на весь период сбора вместо суммы маленьких."""
    raw: dict[str, list[tuple[int, int]]] = {}
    for sig in target_sigs:
        row = rows[sig]
        lo, hi = row["time"], row["time"] + max_sec
        for leg in routes.get(sig) or []:
            raw.setdefault(leg["pool"], []).append((lo, hi))

    merged: dict[str, list[tuple[int, int]]] = {}
    for pool, intervals in raw.items():
        intervals.sort()
        out: list[list[int]] = []
        for lo, hi in intervals:
            if out and lo <= out[-1][1]:
                out[-1][1] = max(out[-1][1], hi)
            else:
                out.append([lo, hi])
        merged[pool] = [(lo, hi) for lo, hi in out]
    return merged


def find_window_for(intervals: list[tuple[int, int]], t: int) -> tuple[int, int]:
    """Какой из непересекающихся сегментов пула покрывает момент t --
    по построению pool_windows_needed ровно один должен подходить."""
    for lo, hi in intervals:
        if lo <= t <= hi:
            return lo, hi
    raise RuntimeError(f"t={t} не попадает ни в один загруженный сегмент {intervals}")


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


def find_price_at(pool: str, t: int, lo_time: int, hi_time: int, max_scanned: int = 120) -> dict:
    """Замена их point(pool,t) -- ТА ЖЕ семантика (последняя сделка до t,
    честный статус missing_swap_event/no_historical_swap), но по
    ОТДЕЛЬНЫМ транзакциям через getTransaction, не по полным блокам.
    [lo_time,hi_time] -- окно, УЖЕ гарантированно покрывающее t (см.
    pool_windows_needed/ensure_pool_window).

    ВАЖНО (найдено при сверке Шага 1 на сигнатуре 5y38rMoQ...: их
    point() считает предел в 120 УНИКАЛЬНЫХ БЛОКОВ (dedup по slot через
    seen_slots), а не 120 транзакций -- на активном пуле несколько сделок
    в одном слоте означают, что per-транзакционный счётчик упирался бы в
    120 задолго до 120 РЕАЛЬНЫХ блоков истории, ложно давая
    no_historical_swap/120_..._without_decoded_swap там, где их метод
    находит цену. Дедуп по slot ниже -- это не смена метода, а
    исправление счётчика предела ПОД их же метод."""
    hist = ensure_pool_window(pool, lo_time, hi_time)
    # Первая подпись с blockTime<=t (hist -- newest-first) -- бинарный поиск
    # внутри уже загруженного окна.
    times = [-(h["blockTime"] or -(10**18)) for h in hist]  # отриц. для monotonic возрастания при newest-first
    idx = bisect.bisect_left(times, -t)
    seen_slots: set[int] = set()
    for h in hist[idx:]:
        if h.get("err") is not None or h.get("blockTime") is None or h["blockTime"] > t:
            continue
        slot = h["slot"]
        if slot not in seen_slots:
            if len(seen_slots) >= max_scanned:
                return dict(pool=pool, target=t, status=f"{max_scanned}_blocks_without_decoded_swap", scanned=len(seen_slots))
            seen_slots.add(slot)
        tx = get_transaction(h["signature"])
        if tx is None:
            continue
        ev = engine.decode_tx(tx)
        pool_events = [e for e in ev if e.get("pool") == pool]
        expected = engine.expected_pool_counts(tx).get(pool, 0)
        if pool_events:
            e = price_event(pool_events[-1])
            return dict(pool=pool, target=t, slot=tx["slot"], signature=h["signature"],
                        time=tx["blockTime"], age_seconds=t - tx["blockTime"], scanned=len(seen_slots),
                        event=e, status=e.get("status", "unknown"))
        if expected > 0:
            return dict(pool=pool, target=t, slot=tx["slot"], signature=h["signature"],
                        status="missing_swap_event", scanned=len(seen_slots))
    return dict(pool=pool, target=t, status="no_historical_swap", scanned=len(seen_slots))


def run_comparison(label: str, target_sigs: list[str], rows: dict, routes: dict,
                    ref_by_sig_sec: dict, only_ref_ok: bool = False) -> list[dict]:
    """Общее тело сверки для Шага 1 (17->N сигнатур) и Шага 2 (все 100,
    только точки, где у эталона status=ok -- 140/1000 уже посчитанных).
    only_ref_ok=True пропускает секунды, для которых у эталона нет
    готовой цены (Шаг 2 не про них -- это Шаг 3)."""
    windows = pool_windows_needed(target_sigs, rows, routes, max(HORIZONS_SECONDS))
    print(f"[{label}] {len(windows)} пул(ов), {sum(len(v) for v in windows.values())} непересекающихся сегмент(ов) "
          f"-- якорем вперёд->назад (Шаг 0.2/0.3)", flush=True)
    for pool, intervals in windows.items():
        for lo, hi in intervals:
            hist = ensure_pool_window(pool, lo, hi)
            print(f"[{label}] pool={pool[:12]}.. окно=[{lo},{hi}] ({hi - lo}с) -> {len(hist)} подписей", flush=True)

    comparison = []
    for sig in target_sigs:
        row = rows[sig]
        route = routes.get(sig)
        for sec in HORIZONS_SECONDS:
            ref = ref_by_sig_sec.get((sig, sec), {})
            if only_ref_ok and ref.get("status") != "ok":
                continue
            t = row["time"] + sec
            if not route:
                comparison.append(dict(signature=sig, seconds=sec, status="no_route"))
                continue
            value = D(1)
            legs_out = []
            ok = True
            for leg in route:
                lo, hi = find_window_for(windows[leg["pool"]], t)
                p = find_price_at(leg["pool"], t, lo, hi)
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
            print(f"[{label}] {sig[:12]}.. sec={sec} mine={mine_price} ref={ref.get('price_usdc')} "
                  f"match_price={match_price} match_slot={match_slot}", flush=True)
    return comparison


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["step1", "step2"], default="step1")
    ap.add_argument("--n", type=int, default=5)
    args = ap.parse_args()

    alchemy_ok = alchemy_available()
    print(f"[{args.mode}] alchemy_available={alchemy_ok} (ключ не печатается)", flush=True)

    rows = {r["signature"]: r for r in json.loads((PRIOR_ROOT / "selected.json").read_text())}
    routes = {r["signature"]: r["route"] for r in json.loads((PRIOR_ROOT / "routes.json").read_text())}
    reference = json.loads((PRIOR_ROOT / "price_points.json").read_text())
    ref_by_sig_sec = {(r["signature"], r["seconds"]): r for r in reference}

    if args.mode == "step1":
        target_sigs = json.loads((PRIOR_ROOT / "analysis.json").read_text())["meta"]["full_5min_signatures"][:args.n]
        comparison = run_comparison("step1", target_sigs, rows, routes, ref_by_sig_sec)
        out_path = OUT_ROOT / "step1_comparison_result.json"
    else:
        # Шаг 2: только те покупки, где у эталона ЕСТЬ хотя бы одна точка
        # status=ok (19 сигнатур -> 140/1000 точек, которые они реально
        # досчитали) -- остальные 81 покупка тут ничего не дали бы
        # сравнить, это уже Шаг 3.
        target_sigs = sorted({sig for (sig, _sec), r in ref_by_sig_sec.items() if r.get("status") == "ok"})
        comparison = run_comparison("step2", target_sigs, rows, routes, ref_by_sig_sec, only_ref_ok=True)
        out_path = OUT_ROOT / "step2_comparison_result.json"

    out_path.write_text(json.dumps(comparison, indent=2, default=str, ensure_ascii=False))
    META_PATH.write_text(json.dumps(META, indent=2))
    n_total = len(comparison)
    n_match = sum(1 for c in comparison if c.get("match_price") and c.get("match_slot") and c.get("match_signature"))
    print(json.dumps({"n_total": n_total, "n_full_match": n_match, "out_path": str(out_path)}, indent=2))
