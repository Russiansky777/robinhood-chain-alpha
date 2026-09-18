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
RPC_CALLS = 0  # честный счётчик реальных HTTP-попыток (включая retry) -- для наблюдаемости


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
    global _last_call_at, _backoff_s, _alchemy_disabled, RPC_CALLS
    cache_f = _cache_path(method, params)
    if use_cache and cache_f.exists():
        try:
            return json.loads(cache_f.read_text())
        except (ValueError, OSError):
            pass
    url = _endpoint()
    fallback_used = False
    # Найдено на расширенном прогоне (300 покупок, 6000+ запросов): под
    # устойчивой продолжительной нагрузкой публичный узел/Alchemy иногда
    # троттлит дольше, чем 8 попыток x потолок 10с (~45с) успевают
    # переждать -- было НЕОБРАБОТАННОЕ исключение, ронявшее весь
    # многочасовой прогон целиком. 20 попыток и потолок 45с -- честно
    # ждём дольше, прежде чем сдаться на этом конкретном вызове.
    max_attempts, backoff_cap = 20, 45.0
    for attempt in range(max_attempts):
        wait = max(_MIN_INTERVAL_S - (time.monotonic() - _last_call_at), 0.0) + _backoff_s
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()
        RPC_CALLS += 1
        try:
            resp = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                  timeout=30)
        except Exception:  # noqa: BLE001
            _backoff_s = min(max(_backoff_s * 2, 0.5), backoff_cap)
            continue
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            _backoff_s = min(max(_backoff_s * 2, 0.5), backoff_cap)
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
                _backoff_s = min(max(_backoff_s * 2, 0.5), backoff_cap)
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
    print(f"[pool_window] pool={pool[:12]}.. окно=[{lo_time},{hi_time}] ({hi_time - lo_time}с) -> "
          f"{len(hist)} подписей (RPC всего: {RPC_CALLS})", flush=True)
    _pool_window_cache[key] = hist
    p.write_text(json.dumps(hist))
    return hist


MERGE_GAP_TOLERANCE_S = 600  # см. pool_windows_needed


def pool_windows_needed(target_sigs: list[str], rows: dict, routes: dict, max_sec: int) -> dict[str, list[tuple[int, int]]]:
    """НЕПЕРЕСЕКАЮЩИЕСЯ сегменты [lo,hi] на пул -- объединяем только
    покупки текущего прогона, чьи диапазоны РЕАЛЬНО пересекаются,
    соприкасаются или разделены МАЛЫМ зазором (<=MERGE_GAP_TOLERANCE_S,
    39/100 покупок делят один минт и, как правило, идут плотно во
    времени -- один проход, не 39), но НЕ сливаем покупки, разнесённые
    по времени на часы/дни через один и тот же высокотрафиковый
    промежуточный пул (см. ensure_pool_window) -- иначе получили бы одно
    окно на весь период сбора вместо суммы маленьких (для пула с сотнями
    tx/с даже пара лишних часов -- это лишние сотни тысяч подписей).

    Найдено на расширенном прогоне: два сегмента ОДНОГО пула разошлись
    всего на 35 секунд (меньше самого горизонта 300с), но не слились --
    потребовался отдельный повторный проход на лишние ~138000 подписей
    ради 35-секундного зазора. MERGE_GAP_TOLERANCE_S=600 закрывает такие
    случаи, не трогая по-настоящему разнесённые (часы/дни) покупки."""
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
            if out and lo <= out[-1][1] + MERGE_GAP_TOLERANCE_S:
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


def _load_existing(out_path: Path) -> dict[tuple[str, int], dict]:
    """Возобновление: НЕ считаем сделанным то, что упало с rpc_error --
    это транзитный сбой (см. run_comparison), должен пересчитаться на
    следующем прогоне, а не застрять навсегда как 'уже готово'."""
    if not out_path.exists():
        return {}
    try:
        prior = json.loads(out_path.read_text())
    except (ValueError, OSError):
        return {}
    return {(r["signature"], r["seconds"]): r for r in prior
            if "seconds" in r and r.get("mine_status") != "rpc_error"}


COMMIT_INTERVAL_S = 12 * 60  # раз в ~10-15 минут, НЕ после каждой точки (владелец: частые коммиты тормозят больше, чем помогают)


def _git_commit_progress(label: str, paths: list[Path]) -> None:
    """Best-effort промежуточный коммит -- переживает обрыв джобы/таймаут
    без потери уже посчитанного. Ошибки глотаются (это не точка отказа
    основного расчёта), следующая попытка -- через COMMIT_INTERVAL_S."""
    import subprocess
    try:
        existing = [str(p) for p in paths if p.exists()]
        if not existing:
            return
        subprocess.run(["git", "add", *existing], check=True, cwd=REPO_ROOT)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO_ROOT)
        if diff.returncode == 0:
            return  # нечего коммитить
        subprocess.run(["git", "commit", "-m", f"Solana buyer_200: промежуточный прогресс {label} [automated]"],
                        check=True, cwd=REPO_ROOT)
        subprocess.run(["git", "pull", "--rebase", "origin", os.environ.get("GITHUB_REF_NAME", "HEAD")],
                        check=False, cwd=REPO_ROOT)
        subprocess.run(["git", "push"], check=True, cwd=REPO_ROOT)
        print(f"[{label}] промежуточный коммит выполнен", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[{label}] промежуточный коммит НЕ удался (не критично, продолжаем): {exc}", flush=True)


def run_comparison(label: str, target_sigs: list[str], rows: dict, routes: dict,
                    ref_by_sig_sec: dict, out_path: Path, only_ref_ok: bool = False,
                    commit_paths: list[Path] | None = None) -> list[dict]:
    """Общее тело сверки для Шага 1 (17->N сигнатур), Шага 2 (19 сигнатур,
    только точки, где у эталона status=ok) и расширенного прогона на до
    300 покупок. only_ref_ok=True пропускает секунды, для которых у
    эталона нет готовой цены.

    Возобновляемо между запусками джобы: если out_path уже содержит
    результат для (signature,seconds) -- НЕ пересчитываем и не грузим
    под это окно пула заново; переносим как есть. Пишем на диск после
    КАЖДОЙ точки (дёшево), коммитим -- редко (см. COMMIT_INTERVAL_S)."""
    already = _load_existing(out_path)
    print(f"[{label}] уже посчитано ранее (возобновление): {len(already)} точек", flush=True)

    def sig_fully_done(sig: str) -> bool:
        secs_needed = [sec for sec in HORIZONS_SECONDS
                       if not only_ref_ok or ref_by_sig_sec.get((sig, sec), {}).get("status") == "ok"]
        return bool(secs_needed) and all((sig, sec) in already for sec in secs_needed)

    pending_sigs = [s for s in target_sigs if not sig_fully_done(s)]
    print(f"[{label}] покупок всего={len(target_sigs)}, уже полностью закрыто={len(target_sigs) - len(pending_sigs)}, "
          f"осталось={len(pending_sigs)}", flush=True)

    # ВАЖНО: окна считаем (для корректного объединения диапазонов между
    # покупками одного пула), но НЕ грузим здесь заранее списком -- при
    # 300 покупках и десятках пулов эта предзагрузка сама по себе может
    # не уложиться в один таймаут джобы, и тогда ни одна точка не
    # сохранится и не закоммитится. Вместо этого ensure_pool_window()
    # грузит каждый сегмент ЛЕНИВО, при первом реальном обращении -- то
    # есть по ходу обработки покупок, каждая из которых сразу же
    # считается и сохраняется (см. цикл ниже).
    windows = pool_windows_needed(pending_sigs, rows, routes, max(HORIZONS_SECONDS))
    print(f"[{label}] {len(windows)} пул(ов), {sum(len(v) for v in windows.values())} непересекающихся сегмент(ов) "
          f"-- будут догружены лениво по ходу покупок (Шаг 0.2/0.3)", flush=True)

    comparison: list[dict] = list(already.values())
    started_at = time.monotonic()
    last_commit_at = started_at
    points_done = 0
    purchases_closed = len(target_sigs) - len(pending_sigs)
    paths_to_commit = commit_paths if commit_paths is not None else [out_path, META_PATH]

    for sig in target_sigs:
        if sig not in pending_sigs:
            continue
        row = rows[sig]
        route = routes.get(sig)
        for sec in HORIZONS_SECONDS:
            if (sig, sec) in already:
                continue
            ref = ref_by_sig_sec.get((sig, sec), {})
            if only_ref_ok and ref.get("status") != "ok":
                continue
            t = row["time"] + sec
            if not route:
                entry = dict(signature=sig, seconds=sec, status="no_route")
            else:
                try:
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
                    entry = dict(
                        signature=sig, seconds=sec,
                        mine_status="ok" if ok else (legs_out[-1].get("status") if legs_out else "no_legs"),
                        mine_price=mine_price, mine_slot=mine_slot, mine_signature=mine_sig,
                        ref_status=ref.get("status"), ref_price=ref.get("price_usdc"),
                        ref_slot=ref_leg.get("slot"), ref_signature=ref_leg.get("signature"),
                        match_price=match_price, match_slot=match_slot, match_signature=match_sig,
                        legs=legs_out,
                    )
                except RuntimeError as exc:
                    # Найдено на расширенном прогоне: устойчивый сбой RPC
                    # (после увеличенных 20 попыток/45с потолка) на ОДНОЙ
                    # точке не должен ронять весь многочасовой прогон --
                    # честно помечаем точку и идём дальше, остальные 250+
                    # покупок не должны из-за этого простаивать.
                    entry = dict(signature=sig, seconds=sec, mine_status="rpc_error",
                                  mine_error=str(exc)[:300])
                    print(f"[{label}] {sig[:12]}.. sec={sec} RPC-ошибка, помечено rpc_error: {exc}", flush=True)
            comparison.append(entry)
            already[(sig, sec)] = entry
            points_done += 1
            out_path.write_text(json.dumps(comparison, indent=2, default=str, ensure_ascii=False))

            if points_done % 20 == 0:
                n_ok = sum(1 for e in comparison if e.get("mine_status") == "ok")
                elapsed = time.monotonic() - started_at
                print(f"[{label}] прогресс: {points_done} новых точек в этом прогоне "
                      f"({len(comparison)} всего, {n_ok} mine_status=ok), покупок закрыто={purchases_closed}, "
                      f"RPC-запросов={RPC_CALLS}, elapsed={elapsed:.0f}с", flush=True)

            print(f"[{label}] {sig[:12]}.. sec={sec} mine={entry.get('mine_price')} ref={ref.get('price_usdc')} "
                  f"match_price={entry.get('match_price')} match_slot={entry.get('match_slot')}", flush=True)

        purchases_closed += 1
        if time.monotonic() - last_commit_at >= COMMIT_INTERVAL_S:
            META_PATH.write_text(json.dumps(META, indent=2))
            _git_commit_progress(label, paths_to_commit)
            last_commit_at = time.monotonic()

    return comparison


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["step1", "step2", "step_extended"], default="step1")
    ap.add_argument("--n", type=int, default=5)
    args = ap.parse_args()

    alchemy_ok = alchemy_available()
    print(f"[{args.mode}] alchemy_available={alchemy_ok} (ключ не печатается)", flush=True)

    reference = json.loads((PRIOR_ROOT / "price_points.json").read_text())
    ref_by_sig_sec = {(r["signature"], r["seconds"]): r for r in reference}

    if args.mode in ("step1", "step2"):
        rows = {r["signature"]: r for r in json.loads((PRIOR_ROOT / "selected.json").read_text())}
        routes = {r["signature"]: r["route"] for r in json.loads((PRIOR_ROOT / "routes.json").read_text())}
        if args.mode == "step1":
            target_sigs = json.loads((PRIOR_ROOT / "analysis.json").read_text())["meta"]["full_5min_signatures"][:args.n]
            out_path = OUT_ROOT / "step1_comparison_result.json"
            run_comparison("step1", target_sigs, rows, routes, ref_by_sig_sec, out_path)
        else:
            # Шаг 2: только те покупки, где у эталона ЕСТЬ хотя бы одна точка
            # status=ok (19 сигнатур -> 140/1000 точек, которые они реально
            # досчитали) -- остальные 81 покупка тут ничего не дали бы
            # сравнить, это уже расширенный прогон.
            target_sigs = sorted({sig for (sig, _sec), r in ref_by_sig_sec.items() if r.get("status") == "ok"})
            out_path = OUT_ROOT / "step2_comparison_result.json"
            run_comparison("step2", target_sigs, rows, routes, ref_by_sig_sec, out_path, only_ref_ok=True)
    else:
        # Расширенный прогон: ВСЕ отобранные покупки (до 300, включая
        # исходные 100) -- их собственный routes.json тут не при чём,
        # используем расширенные selected_300.json/routes_300.json
        # (см. analysis/solana_buyer200_select_extend.py).
        EXT_ROOT = OUT_ROOT
        rows = {r["signature"]: r for r in json.loads((EXT_ROOT / "selected_300.json").read_text())}
        routes = {r["signature"]: r["route"] for r in json.loads((EXT_ROOT / "routes_300.json").read_text())}
        target_sigs = list(rows.keys())
        out_path = OUT_ROOT / "step_extended_result.json"
        run_comparison("step_extended", target_sigs, rows, routes, ref_by_sig_sec, out_path)

    META_PATH.write_text(json.dumps(META, indent=2))
    final = json.loads(out_path.read_text())
    n_total = len(final)
    n_ok = sum(1 for c in final if c.get("mine_status") == "ok")
    n_match = sum(1 for c in final if c.get("match_price") and c.get("match_slot") and c.get("match_signature"))
    print(json.dumps({"n_total": n_total, "n_mine_ok": n_ok, "n_full_match": n_match,
                       "rpc_calls": RPC_CALLS, "out_path": str(out_path)}, indent=2))
