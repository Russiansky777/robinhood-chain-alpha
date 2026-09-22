#!/usr/bin/env python3
"""Владелец, Часть A: ИЗМЕРЕНИЕ (не торговля -- ни одного вызова покупки/
продажи в этом файле). Для каждой нашей сделки из data/solana_trades_all.json
найти транзакцию покупки ИСТОЧНИКА (лидера), посчитать отставание в слотах
и кто успел купить между ними. Выход -- data/solana_entry_log.json.

ПОЧЕМУ ОТДЕЛЬНЫЙ ФАЙЛ, А НЕ analysis/solana_entry_log.py (как просил
владелец): файл с таким именем УЖЕ существует (427 строк) и является
живым модулем -- его импортируют 7 других модулей (в т.ч.
solana_crowd_common.py тянет из него 11 имён), у него есть собственный
main() и собственный workflow run_solana_entry_log.yml. Дописывание
второго main() туда молча сломало бы старый режим и рисковало бы
ImportError у всех импортёров. Поэтому измерение живёт здесь, а ВЫХОДНОЙ
файл -- ровно data/solana_entry_log.json, как и просил владелец (такого
файла в репозитории никогда не было, коллизии данных нет).

ЧЕСТНО О ДВУХ ОПРОВЕРГНУТЫХ ПРЕДПОЛОЖЕНИЯХ ЗАДАЧИ (проверено программно
по реальным данным до написания кода):
  1. "Сначала проверь, есть ли хэш транзакции источника в записи DBot
     (record.follow.*)" -- ЕГО ТАМ НЕТ. Рекурсивный обход всех листьев
     всех 2903 записей data/dbot_follow_trades_raw.json регуляркой
     ^[1-9A-HJ-NP-Za-km-z]{64,90}$ даёт 0 совпадений где-либо, кроме
     record.links.etherscan -- а это НАША подпись, не источника. Код
     ниже всё равно сначала пробует найти подпись в записи DBot (вдруг
     появится позже), но рассчитывать на это нельзя.
  2. Зато follow.send/follow.receive дают полный отпечаток сделки лидера
     (его кошелёк, оба минта, точные raw-суммы) -- по нему транзакция
     источника ищется на цепочке надёжнее, чем по одному времени.

ИСТОЧНИК ПОДПИСЕЙ -- ТОЛЬКО API, НИКАКИХ ВЫДУМАННЫХ ЗНАЧЕНИЙ. Если
транзакция источника не найдена -- в записи будет source_signature=null
и source_lookup="not_found" с причиной, а не подогнанное значение.

КЛЮЧ HELIUS: владелец просит env HELIUS_API_KEY. Реальная проверка
(data/solana_source_a_helius_gate.json, прогон 2026-09-19) показала, что
в GitHub Secrets и на боевом хосте задан HELIUS_API, а HELIUS_API_KEY
НЕ задан. Поэтому читаем HELIUS_API_KEY первым (как просил владелец), с
явным откатом на HELIUS_API, и пишем в отчёт, какое имя реально
сработало. Ключ в репозиторий не кладётся и скрабится в логах."""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402  -- кэш+каскад+бэкофф+батчи

TRADES_PATH = REPO_ROOT / "data" / "solana_trades_all.json"
FOLLOW_TRADES_PATH = REPO_ROOT / "data" / "dbot_follow_trades_raw.json"
CHAIN_CACHE_PATH = REPO_ROOT / "data" / "chain_tx_cache.json"
OUT_PATH = REPO_ROOT / "data" / "solana_entry_log.json"
CACHE_PATH = REPO_ROOT / "data" / "solana_entry_log_offsets_cache.json"

HELIUS_ENHANCED_BASE = "https://api.helius.xyz"

# Владелец, п.4 -- константы программ.
JUPITER_ORDER_ENGINE = "61DFfeTKM7trxYcPQCM78bJ794ddZprZpAwAnLiwTpYH"   # RFQ
DFLOW = "DF1ow4tspfHX9JwWJsAb9epbkA8hmpSEAtxXy1V27QBH"
JUPITER = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
DEX_ORDER = [
    ("raydium_v4", "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"),
    ("raydium_cpmm", "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"),
    ("raydium_clmm", "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"),
    ("meteora_dlmm", "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"),
    ("meteora_damm_v2", "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"),
    ("pumpswap", "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"),
    ("orca_whirlpool", "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"),
]

SIG_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{64,90}$")

# Окно поиска транзакции источника до нашей покупки. Слот ~0.25-0.3с,
# копирующий бот отстаёт на единицы слотов, но берём с большим запасом.
SOURCE_WINDOW_S = 180
MAX_HISTORY_PAGES = 12          # страниц getSignaturesForAddress на источник
MAX_CANDIDATES_PER_TRADE = 40   # сколько кандидатов-подписей разбирать
MAX_SLOT_SCAN = 150             # потолок сканирования блоков на сделку
TIME_BUDGET_S = 50 * 60

_ACTIVE_SECRETS: list[str] = []


def scrub(text: str) -> str:
    for s in _ACTIVE_SECRETS:
        if s:
            text = text.replace(s, "[REDACTED]")
    return text


def log(msg: str) -> None:
    print(f"[entry_log] {scrub(msg)}", flush=True)


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (ValueError, OSError):
            pass
    return default


def save_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def helius_key() -> tuple[str, str]:
    """Владелец просит HELIUS_API_KEY; реально в секретах задан HELIUS_API.
    Пробуем в порядке просьбы, возвращаем (ключ, имя_переменной)."""
    for name in ("HELIUS_API_KEY", "HELIUS_API"):
        v = os.environ.get(name, "").strip()
        if v:
            if any(c in v for c in ("\n", "\r")):
                raise RuntimeError(f"{name} содержит перевод строки -- не похоже на сырой ключ")
            _ACTIVE_SECRETS.append(v)
            return v, name
    raise RuntimeError("ни HELIUS_API_KEY, ни HELIUS_API не заданы в окружении")


# ---------- DBot: отпечаток сделки лидера ----------

def our_sig_from_record(rec: dict) -> str | None:
    links = rec.get("links") or {}
    for k in ("etherscan", "dexscreener", "uniswap"):
        url = links.get(k)
        if url and "/tx/" in url:
            sig = url.rsplit("/tx/", 1)[-1].strip()
            if SIG_RE.match(sig):
                return sig
    return None


def find_source_sig_in_record(rec: dict) -> str | None:
    """Предположение владельца: подпись источника может лежать в записи
    DBot. Эмпирически её там нет, но проверяем честно и дёшево: обходим
    все листья, ищем base58-подпись, отличную от НАШЕЙ."""
    ours = our_sig_from_record(rec)
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str) and SIG_RE.match(node) and node != ours:
            found.append(node)

    walk(rec)
    return found[0] if found else None


def build_dbot_index() -> dict[str, dict]:
    """our_buy_signature -> отпечаток сделки лидера из записи DBot."""
    raw = load_json(FOLLOW_TRADES_PATH, {})
    out: dict[str, dict] = {}
    for entry in raw.values():
        rec = entry.get("record") or {}
        sig = our_sig_from_record(rec)
        if not sig:
            continue
        follow = rec.get("follow") or {}
        send = follow.get("send") or {}
        recv = follow.get("receive") or {}
        out[sig] = {
            "follow_wallet": follow.get("wallet"),
            "follow_send_contract": (send.get("info") or {}).get("contract"),
            "follow_send_amount": send.get("amount"),
            "follow_recv_contract": (recv.get("info") or {}).get("contract"),
            "follow_recv_amount": recv.get("amount"),
            "record_create_at_ms": rec.get("createAt"),
            "record_timestamp_s": rec.get("timestamp"),
            "record_type": rec.get("type"),
            "source_sig_in_record": find_source_sig_in_record(rec),
        }
    return out


# ---------- наши слоты: уже есть в chain_tx_cache.json ----------

def build_our_slot_index() -> dict[str, int]:
    cache = load_json(CHAIN_CACHE_PATH, {})
    out: dict[str, int] = {}
    for key, v in cache.items():
        if not isinstance(v, dict):
            continue
        sig, slot = v.get("signature"), v.get("slot")
        if sig and isinstance(slot, int):
            out[sig] = slot
    return out


# ---------- история источника (JSON-RPC, надёжный путь) ----------

def source_needed_windows(work: list[dict]) -> dict[str, dict]:
    """Для каждого источника -- список УЗКИХ окон (по одному на сделку,
    180с до её покупки) и самое старое время, до которого надо долистать.

    Почему не один общий диапазон: у части источников первая и последняя
    сделка разнесены на ~71 час, и «всё между ними» -- это тысячи
    подписей на источник, то есть кэш на десятки мегабайт в репозитории.
    Реально же нужны только окна вокруг наших покупок."""
    raw: dict[str, list[tuple[int, int]]] = {}
    for t in work:
        raw.setdefault(t["source_address"], []).append(
            (t["buy_block_time"] - SOURCE_WINDOW_S, t["buy_block_time"]))
    out: dict[str, dict] = {}
    for src, iv in raw.items():
        iv.sort()
        merged: list[list[int]] = []
        for lo, hi in iv:
            if merged and lo <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        out[src] = {"walk_until": merged[0][0],
                     "intervals": [(a, b) for a, b in merged]}
    return out


def in_intervals(t: int, intervals: list[tuple[int, int]]) -> bool:
    return any(lo <= t <= hi for lo, hi in intervals)


def source_history(source: str, window: dict, cache: dict,
                    max_pages: int, stats: dict) -> list[dict]:
    """Подписи источника внутри нужного диапазона времени.

    getSignaturesForAddress умеет листать только от свежих к старым, так
    что дойти до сделки трёхдневной давности можно только страницами по
    1000. Поэтому: (а) в кэш кладём ТОЛЬКО записи внутри нужного окна
    (остальные десятки тысяч выбрасываем -- иначе файл кэша вырастет до
    десятков мегабайт), (б) храним курсор before и covered_until_time,
    чтобы следующий прогон продолжил с места остановки, а не начинал от
    вершины заново."""
    node = cache.setdefault("source_history", {}).setdefault(source, {
        "entries": [], "cursor": None, "covered_until_time": None,
        "pages_walked": 0, "complete": False, "exhausted": False,
    })
    lo, intervals = window["walk_until"], window["intervals"]
    covered = node.get("covered_until_time")
    if node.get("exhausted") or (covered is not None and covered <= lo):
        node["complete"] = True
        return node["entries"]

    seen = {e["signature"] for e in node["entries"]}
    before = node.get("cursor")
    pages = 0
    while pages < max_pages:
        if fp.soft_deadline_exceeded():
            break
        params: list = [source, {"limit": 1000}]
        if before:
            params[1]["before"] = before
        try:
            page = fp.rpc_call("getSignaturesForAddress", params, use_cache=False) or []
        except Exception as exc:  # noqa: BLE001
            log(f"история {source[:10]}..: сбой getSignaturesForAddress: {type(exc).__name__}: {exc}")
            stats["history_errors"] = stats.get("history_errors", 0) + 1
            break
        pages += 1
        node["pages_walked"] = node.get("pages_walked", 0) + 1
        if not page:
            node["exhausted"] = True          # у кошелька больше нет истории
            break
        for h in page:
            bt = h.get("blockTime")
            sig = h.get("signature")
            if not sig or sig in seen:
                continue
            if bt is not None and in_intervals(bt, intervals):
                seen.add(sig)
                node["entries"].append({"signature": sig, "slot": h.get("slot"),
                                         "blockTime": bt, "err": h.get("err") is not None})
        before = page[-1].get("signature")
        node["cursor"] = before
        oldest = page[-1].get("blockTime")
        if oldest is not None:
            node["covered_until_time"] = oldest
            if oldest <= lo:
                break
        if len(page) < 1000:
            node["exhausted"] = True
            break

    covered = node.get("covered_until_time")
    node["complete"] = bool(node.get("exhausted")) or (covered is not None and covered <= lo)
    if not node["complete"]:
        stats["history_incomplete_sources"] = sorted(
            set(stats.get("history_incomplete_sources", [])) | {source})
    node["entries"].sort(key=lambda e: (e.get("blockTime") or 0), reverse=True)
    return node["entries"]


def enhanced_swaps(source: str, key: str, stats: dict, cache: dict) -> list[dict] | None:
    """Helius Enhanced REST, как просил владелец (/v0/addresses/{source}/
    transactions?type=SWAP).

    ЧТО ПОКАЗАЛ РЕАЛЬНЫЙ ПРОГОН (job 35670100424): HTTP 200, ответ
    приходит -- но это ТОЛЬКО последняя страница транзакций адреса
    (самые свежие). Наши сделки старше, поэтому ни одна из них в эту
    страницу не попадает, и путь не даёт ни одного совпадения. Поэтому
    зонд делается ОДИН РАЗ НА ИСТОЧНИК (а не на каждую сделку), его
    охват (сколько записей и до какого времени достаёт) пишется в отчёт
    как измеренный факт, а рабочим путём остаётся JSON-RPC."""
    node = cache.setdefault("enhanced", {})
    if source in node:
        return node[source]
    try:
        resp = requests.get(
            f"{HELIUS_ENHANCED_BASE}/v0/addresses/{source}/transactions",
            params={"api-key": key, "type": "SWAP"},
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        stats["enhanced_error"] = scrub(f"{type(exc).__name__}: {exc}")
        node[source] = None
        return None
    stats["enhanced_last_status"] = resp.status_code
    if resp.status_code != 200:
        stats["enhanced_error"] = scrub(resp.text[:200])
        node[source] = None
        return None
    try:
        body = resp.json()
    except ValueError:
        stats["enhanced_error"] = "non-json body"
        node[source] = None
        return None
    if not isinstance(body, list):
        node[source] = None
        return None
    ts = [b.get("timestamp") for b in body if b.get("timestamp")]
    stats.setdefault("enhanced_coverage", {})[source] = {
        "n": len(body),
        "oldest_ts": min(ts) if ts else None,
        "newest_ts": max(ts) if ts else None,
    }
    node[source] = body
    return body


# ---------- разбор транзакции ----------

def token_delta_for_owner(tx: dict, mint: str, owner: str | None) -> float:
    """Прирост баланса mint у owner (или максимальный положительный по
    любому owner, если owner=None) по post-pre TokenBalances."""
    meta = tx.get("meta") or {}
    pre, post = {}, {}
    for b in meta.get("preTokenBalances") or []:
        if b.get("mint") != mint:
            continue
        if owner is None or b.get("owner") == owner:
            k = (b.get("owner"), b.get("accountIndex"))
            pre[k] = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
    for b in meta.get("postTokenBalances") or []:
        if b.get("mint") != mint:
            continue
        if owner is None or b.get("owner") == owner:
            k = (b.get("owner"), b.get("accountIndex"))
            post[k] = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
    best = 0.0
    for k in set(pre) | set(post):
        d = post.get(k, 0.0) - pre.get(k, 0.0)
        if d > best:
            best = d
    return best


def raw_delta_for_owner(tx: dict, mint: str, owner: str) -> int:
    meta = tx.get("meta") or {}
    pre, post = 0, 0
    for b in meta.get("preTokenBalances") or []:
        if b.get("mint") == mint and b.get("owner") == owner:
            pre += int((b.get("uiTokenAmount") or {}).get("amount") or 0)
    for b in meta.get("postTokenBalances") or []:
        if b.get("mint") == mint and b.get("owner") == owner:
            post += int((b.get("uiTokenAmount") or {}).get("amount") or 0)
    return post - pre


def program_ids(tx: dict) -> tuple[list[str], list[str]]:
    """Программы транзакции: верхний уровень + inner (владелец, п.4)."""
    msg = ((tx.get("transaction") or {}).get("message")) or {}
    keys = msg.get("accountKeys") or []
    key_strs = [k.get("pubkey") if isinstance(k, dict) else k for k in keys]
    la = (tx.get("meta") or {}).get("loadedAddresses") or {}
    key_strs = key_strs + list(la.get("writable") or []) + list(la.get("readonly") or [])

    def resolve(ins: dict) -> str | None:
        pid = ins.get("programId")
        if pid:
            return pid
        idx = ins.get("programIdIndex")
        if isinstance(idx, int) and 0 <= idx < len(key_strs):
            return key_strs[idx]
        return None

    top = [p for p in (resolve(i) for i in (msg.get("instructions") or [])) if p]
    inner = []
    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        for i in group.get("instructions") or []:
            p = resolve(i)
            if p:
                inner.append(p)
    return top, inner


def program_flags(top: list[str], inner: list[str]) -> dict:
    allp = set(top) | set(inner)
    dex = None
    for name, pid in DEX_ORDER:
        if pid in allp:
            dex = name
            break
    return {
        "programs_top_level": sorted(set(top)),
        "programs_inner": sorted(set(inner)),
        "is_rfq": JUPITER_ORDER_ENGINE in allp,
        "via_dflow": DFLOW in allp,
        "via_jupiter": JUPITER in allp,
        "dex": dex,
    }


# ---------- покупатели в блоке ----------

def block_buyers(slot: int, mint: str, cache: dict, stats: dict) -> list[dict] | None:
    """Покупатели mint в блоке slot, по (post-pre)TokenBalances > 0.
    Возвращает список {index, signature, owner, delta} в порядке блока.

    ДВА ОСОЗНАННЫХ ОТЛИЧИЯ ОТ БУКВЫ ЗАДАНИЯ, оба по реальному прогону:

    1. maxSupportedTransactionVersion=1, а НЕ 0 (как в задании). С нулём
       РЕАЛЬНО падали все 12 из 12 вызовов первого прогона (job
       35670100424): "code: -32015, Transaction version (1) is not
       supported by the requesting client". В блоках сейчас есть
       транзакции версии 1, и с потолком 0 узел отказывается отдавать
       блок целиком. С единицей блок отдаётся -- иначе buyers_between не
       посчитать вообще ни по одной сделке.
    2. transactionDetails="accounts" (а не "full"): этот уровень отдаёт
       ровно то, что нужно (подписи + pre/postTokenBalances), но в разы
       меньше трафика. Если у блока не окажется токен-балансов -- честный
       фоллбэк на "full" со счётчиком fallback_to_full."""
    ck = f"{slot}:{mint}"
    node = cache.setdefault("block_buyers", {})
    if ck in node:
        stats["block_cache_hits"] = stats.get("block_cache_hits", 0) + 1
        return node[ck]

    for detail in ("accounts", "full"):
        try:
            blk = fp.rpc_call("getBlock", [slot, {
                "encoding": "jsonParsed",
                "transactionDetails": detail,
                "maxSupportedTransactionVersion": 1,
                "rewards": False,
            }], use_cache=False)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if any(c in msg for c in ("-32004", "-32007", "-32009")) or \
                    "skipped" in msg or "not available" in msg:
                node[ck] = []
                stats["blocks_skipped"] = stats.get("blocks_skipped", 0) + 1
                return []
            stats["block_errors"] = stats.get("block_errors", 0) + 1
            log(f"getBlock {slot} ({detail}) упал: {type(exc).__name__}: {str(exc)[:160]}")
            return None
        if not blk:
            node[ck] = []
            return []
        txs = blk.get("transactions") or []
        has_meta = any((t.get("meta") or {}).get("postTokenBalances") is not None for t in txs)
        if not has_meta and detail == "accounts":
            stats["fallback_to_full"] = stats.get("fallback_to_full", 0) + 1
            continue
        buyers = []
        for idx, t in enumerate(txs):
            meta = t.get("meta") or {}
            if meta.get("err") is not None:
                continue
            pre, post = {}, {}
            for b in meta.get("preTokenBalances") or []:
                if b.get("mint") == mint:
                    k = (b.get("owner"), b.get("accountIndex"))
                    pre[k] = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            for b in meta.get("postTokenBalances") or []:
                if b.get("mint") == mint:
                    k = (b.get("owner"), b.get("accountIndex"))
                    post[k] = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            gain = {}
            for k in set(pre) | set(post):
                d = post.get(k, 0.0) - pre.get(k, 0.0)
                if d > 0:
                    gain[k[0]] = gain.get(k[0], 0.0) + d
            if not gain:
                continue
            sigs = (t.get("transaction") or {}).get("signatures") or []
            owner, delta = max(gain.items(), key=lambda kv: kv[1])
            buyers.append({"index": idx, "signature": sigs[0] if sigs else None,
                           "owner": owner, "delta": round(delta, 9)})
        node[ck] = buyers
        stats["blocks_fetched"] = stats.get("blocks_fetched", 0) + 1
        stats["block_detail_used"] = detail
        return buyers
    return None


# ---------- поиск транзакции источника ----------

def find_source_tx(trade: dict, fingerprint: dict | None, cache: dict, stats: dict,
                    key: str, use_enhanced: bool, window: dict,
                    max_pages: int) -> dict:
    """Возвращает {signature, slot, blockTime, method, note} либо
    {signature: None, method: 'not_found', note: ...}. Подписи ТОЛЬКО из
    API -- ничего не конструируем сами."""
    source = trade["source_address"]
    mint = trade["mint"]
    our_time = trade["buy_block_time"]

    # (1) предположение владельца -- подпись прямо в записи DBot
    if fingerprint and fingerprint.get("source_sig_in_record"):
        sig = fingerprint["source_sig_in_record"]
        tx = fp.get_transaction(sig)
        if tx:
            return {"signature": sig, "slot": tx.get("slot"), "blockTime": tx.get("blockTime"),
                    "method": "dbot_record", "note": "подпись найдена прямо в записи DBot"}

    # (2) Helius Enhanced REST -- пробный путь, см. докстринг
    if use_enhanced:
        swaps = enhanced_swaps(source, key, stats, cache)
        if swaps:
            stats["enhanced_worked"] = True
            best = None
            for s in swaps:
                ts = s.get("timestamp")
                sig = s.get("signature")
                if not (ts and sig and SIG_RE.match(sig)):
                    continue
                if not (our_time - SOURCE_WINDOW_S <= ts <= our_time):
                    continue
                txt = json.dumps(s, default=str)
                if mint not in txt:
                    continue
                if best is None or ts > best[0]:
                    best = (ts, sig)
            if best:
                tx = fp.get_transaction(best[1])
                if tx:
                    return {"signature": best[1], "slot": tx.get("slot"),
                            "blockTime": tx.get("blockTime"), "method": "helius_enhanced",
                            "note": "ближайший SWAP источника по этому минту до нашей покупки"}

    # (3) JSON-RPC: история источника + разбор кандидатов
    hist = source_history(source, window, cache, max_pages, stats)
    cands = [e for e in hist
             if e.get("blockTime") and not e.get("err")
             and our_time - SOURCE_WINDOW_S <= e["blockTime"] <= our_time]
    cands.sort(key=lambda e: e["blockTime"], reverse=True)
    cands = cands[:MAX_CANDIDATES_PER_TRADE]
    if not cands:
        node = (cache.get("source_history") or {}).get(source) or {}
        if not node.get("complete"):
            return {"signature": None, "method": "not_found",
                    "note": "история источника НЕ долистана до нужного времени "
                            f"(пройдено страниц={node.get('pages_walked')}, дошли до "
                            f"{node.get('covered_until_time')}, нужно {our_time - SOURCE_WINDOW_S}) "
                            "-- это предел листания, а не отсутствие сделки"}
        return {"signature": None, "method": "not_found",
                "note": f"в истории источника нет транзакций в окне {SOURCE_WINDOW_S}с до нашей покупки"}

    txs = fp.get_transactions_batch([c["signature"] for c in cands])
    want_raw = None
    if fingerprint and fingerprint.get("follow_recv_contract") == mint:
        try:
            want_raw = int(fingerprint.get("follow_recv_amount"))
        except (TypeError, ValueError):
            want_raw = None

    exact, nearest = None, None
    for c in cands:
        tx = txs.get(c["signature"])
        if not tx or (tx.get("meta") or {}).get("err") is not None:
            continue
        raw = raw_delta_for_owner(tx, mint, source)
        if raw <= 0:
            continue
        cand = {"signature": c["signature"], "slot": tx.get("slot"),
                "blockTime": tx.get("blockTime"), "raw_delta": raw}
        if want_raw is not None and raw == want_raw and exact is None:
            exact = cand
        if nearest is None:
            nearest = cand
    if exact:
        return {**exact, "method": "onchain_amount_exact",
                "note": "совпала raw-сумма из follow.receive.amount записи DBot"}
    if nearest:
        return {**nearest, "method": "onchain_nearest_buy",
                "note": "ближайшая по времени покупка этого минта кошельком источника до нашей"}
    return {"signature": None, "method": "not_found",
            "note": "в окне нет транзакции источника с положительным приростом этого минта"}


# ---------- сводка ----------

def bucket(n: int) -> str:
    return "0" if n == 0 else "1" if n == 1 else "2" if n == 2 else "3+"


def summarize(rows: list[dict]) -> dict:
    measured = [r for r in rows if r.get("offset_slots") is not None]
    with_buyers = [r for r in measured if r.get("buyers_between") is not None]

    def dist(items, keyf):
        out: dict = {}
        for it in items:
            out[keyf(it)] = out.get(keyf(it), 0) + 1
        return dict(sorted(out.items()))

    by_task: dict = {}
    for r in measured:
        d = by_task.setdefault(r["task_name"], {"n": 0, "offset_buckets": {}})
        d["n"] += 1
        b = bucket(r["offset_slots"])
        d["offset_buckets"][b] = d["offset_buckets"].get(b, 0) + 1
    for d in by_task.values():
        d["offset_buckets"] = dict(sorted(d["offset_buckets"].items()))

    by_source: dict = {}
    for r in measured:
        d = by_source.setdefault(r["source_address"], {"n": 0, "offset_buckets": {}, "remark": r.get("source_remark")})
        d["n"] += 1
        b = bucket(r["offset_slots"])
        d["offset_buckets"][b] = d["offset_buckets"].get(b, 0) + 1
    for d in by_source.values():
        d["offset_buckets"] = dict(sorted(d["offset_buckets"].items()))

    def corr(xs, ys):
        if len(xs) < 3:
            return None
        try:
            return round(statistics.correlation(xs, ys), 4)
        except Exception:  # noqa: BLE001
            return None

    sig_rows = [r for r in measured if r.get("signal_pct") is not None]
    sig_buy = [r for r in with_buyers if r.get("signal_pct") is not None]
    rfq_rows = [r for r in rows if r.get("is_rfq") is not None]

    # сигнал по корзинам отставания
    signal_by_offset: dict = {}
    for r in sig_rows:
        b = bucket(r["offset_slots"])
        signal_by_offset.setdefault(b, []).append(r["signal_pct"])
    signal_by_offset = {
        k: {"n": len(v), "median_signal_pct": round(statistics.median(v), 4)}
        for k, v in sorted(signal_by_offset.items())
    }

    return {
        "n_rows": len(rows),
        "n_with_offset": len(measured),
        "n_with_buyers_between": len(with_buyers),
        "slot_time_note": "слот сейчас 250-300 мс -- 1 слот отставания ~ четверть секунды",
        "offset_slots_distribution": dist(measured, lambda r: bucket(r["offset_slots"])),
        "offset_slots_median": round(statistics.median([r["offset_slots"] for r in measured]), 2) if measured else None,
        "offset_slots_by_task": by_task,
        "offset_slots_by_source": by_source,
        "buyers_between_median": round(statistics.median([r["buyers_between"] for r in with_buyers]), 2) if with_buyers else None,
        "buyers_between_distribution": dist(with_buyers, lambda r: bucket(r["buyers_between"])),
        "buyers_same_block_after_us_median": round(statistics.median(
            [r["buyers_same_block_after_us"] for r in with_buyers]), 2) if with_buyers else None,
        "is_rfq_share": round(sum(1 for r in rfq_rows if r["is_rfq"]) / len(rfq_rows), 4) if rfq_rows else None,
        "is_rfq_n": sum(1 for r in rfq_rows if r["is_rfq"]),
        "dex_distribution": dist([r for r in rows if r.get("dex")], lambda r: r["dex"]),
        "via_jupiter_n": sum(1 for r in rows if r.get("via_jupiter")),
        "via_dflow_n": sum(1 for r in rows if r.get("via_dflow")),
        "signal_median_by_offset_bucket": signal_by_offset,
        "corr_offset_vs_signal": corr([r["offset_slots"] for r in sig_rows], [r["signal_pct"] for r in sig_rows]),
        "corr_buyers_between_vs_signal": corr([r["buyers_between"] for r in sig_buy], [r["signal_pct"] for r in sig_buy]),
        "source_lookup_methods": dist(rows, lambda r: r.get("source_lookup") or "none"),
    }


# ---------- main ----------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="обработать только N сделок (для отладки)")
    ap.add_argument("--no-enhanced", action="store_true", help="не пробовать Helius Enhanced REST")
    ap.add_argument("--max-slot-scan", type=int, default=MAX_SLOT_SCAN)
    ap.add_argument("--max-history-pages", type=int, default=MAX_HISTORY_PAGES,
                    help="потолок страниц getSignaturesForAddress на источник ЗА ОДИН прогон "
                         "(курсор сохраняется в кэше, следующий прогон продолжит)")
    ap.add_argument("--time-budget-s", type=int, default=TIME_BUDGET_S)
    args = ap.parse_args()

    started = time.monotonic()
    deadline = started + args.time_budget_s
    fp.set_soft_deadline(deadline)

    key, key_name = helius_key()
    # fp._endpoint() читает ТОЛЬКО HELIUS_API. Если владелец задал ключ под
    # именем HELIUS_API_KEY (как в тексте задачи), без этой строки весь
    # каскад молча свалится на публичный узел и прогон не уложится в бюджет.
    if not os.environ.get("HELIUS_API", "").strip():
        os.environ["HELIUS_API"] = key
    fast = fp.alchemy_available()   # один getHealth: поднимает лимит частоты
    log(f"ключ Helius взят из переменной {key_name}; привилегированный RPC доступен={fast}")

    trades = load_json(TRADES_PATH, [])
    dbot = build_dbot_index()
    our_slots = build_our_slot_index()
    cache = load_json(CACHE_PATH, {})
    stats: dict = {"helius_key_env_name": key_name}

    work = [t for t in trades
            if t.get("source_address") and t.get("mint") and t.get("buy_signature") and t.get("buy_block_time")]
    if args.limit:
        work = work[:args.limit]
    windows = source_needed_windows(work)
    log(f"сделок всего={len(trades)}, рабочий набор (source+mint+buy_signature+buy_block_time)={len(work)}")
    log(f"отпечатков DBot по нашей подписи={len(dbot)}, наших слотов в chain_tx_cache={len(our_slots)}")

    rows: list[dict] = []
    for i, t in enumerate(work, 1):
        if time.monotonic() > deadline:
            log(f"бюджет времени исчерпан на {i}/{len(work)} -- сохраняю, что есть")
            stats["stopped_early_at"] = i
            break

        mint, our_sig = t["mint"], t["buy_signature"]
        fingerprint = dbot.get(our_sig)
        row = {
            "task_id": t.get("task_id"), "task_name": t.get("task_name"),
            "wallet": t.get("wallet"), "source_address": t.get("source_address"),
            "source_remark": t.get("source_remark"), "mint": mint,
            "our_signature": our_sig, "our_block_time": t.get("buy_block_time"),
            "sol_out": t.get("sol_out"), "gross_sol_in": t.get("gross_sol_in"),
            "signal_pct": None, "source_signature": None, "source_lookup": None,
            "source_slot": None, "our_slot": None, "offset_slots": None,
            "our_index_in_block": None, "our_buyer_rank_in_block": None,
            "buyers_between": None, "buyers_same_block_after_us": None,
            "block_scan": None,
        }
        gi, so = t.get("gross_sol_in"), t.get("sol_out")
        if gi not in (None, 0) and so is not None:
            row["signal_pct"] = round((so - gi) / gi * 100, 4)

        try:
            src = find_source_tx(t, fingerprint, cache, stats, key, not args.no_enhanced,
                                  windows[t["source_address"]], args.max_history_pages)
        except Exception as exc:  # noqa: BLE001
            src = {"signature": None, "method": "error", "note": scrub(f"{type(exc).__name__}: {exc}")[:200]}
        row["source_signature"] = src.get("signature")
        row["source_lookup"] = src.get("method")
        row["source_lookup_note"] = src.get("note")
        row["source_slot"] = src.get("slot")

        our_slot = our_slots.get(our_sig)
        if our_slot is None:
            tx = fp.get_transaction(our_sig)
            our_slot = tx.get("slot") if tx else None
        row["our_slot"] = our_slot

        # программы транзакции источника (владелец, п.4)
        if row["source_signature"]:
            stx = fp.get_transaction(row["source_signature"])
            if stx:
                top, inner = program_ids(stx)
                row.update(program_flags(top, inner))

        if row["source_slot"] is not None and our_slot is not None:
            row["offset_slots"] = our_slot - row["source_slot"]

        # покупатели между источником и нами (владелец, п.3)
        off = row["offset_slots"]
        if off is None:
            row["block_scan"] = "нет обоих слотов"
        elif off < 0:
            row["block_scan"] = "наша покупка РАНЬШЕ источника -- сшивка сомнительна, блоки не сканирую"
        elif off > args.max_slot_scan:
            row["block_scan"] = f"диапазон {off} слотов > потолка {args.max_slot_scan} -- не сканирую"
        else:
            ok, ordered = True, []
            for slot in range(row["source_slot"], our_slot + 1):
                if time.monotonic() > deadline:
                    ok = False
                    row["block_scan"] = "бюджет времени исчерпан посреди сканирования блоков"
                    break
                buyers = block_buyers(slot, mint, cache, stats)
                if buyers is None:
                    ok = False
                    row["block_scan"] = f"getBlock({slot}) не отдал блок"
                    break
                for b in buyers:
                    ordered.append({**b, "slot": slot})
            if ok:
                src_pos = next((n for n, b in enumerate(ordered) if b["signature"] == row["source_signature"]), None)
                our_pos = next((n for n, b in enumerate(ordered) if b["signature"] == our_sig), None)
                if our_pos is None:
                    row["block_scan"] = "нашей покупки нет среди покупателей в блоке -- не считаю buyers_between"
                else:
                    row["our_index_in_block"] = ordered[our_pos]["index"]
                    row["our_buyer_rank_in_block"] = sum(
                        1 for b in ordered if b["slot"] == our_slot and b["index"] < ordered[our_pos]["index"])
                    row["buyers_same_block_after_us"] = sum(
                        1 for b in ordered if b["slot"] == our_slot and b["index"] > ordered[our_pos]["index"])
                    if src_pos is None:
                        row["block_scan"] = "транзакции источника нет среди покупателей -- buyers_between не считаю"
                    else:
                        row["buyers_between"] = max(0, our_pos - src_pos - 1)
                        row["block_scan"] = "ok"

        rows.append(row)
        if i % 10 == 0 or i == len(work):
            log(f"{i}/{len(work)}: offset={row['offset_slots']} buyers_between={row['buyers_between']} "
                f"lookup={row['source_lookup']} (блоков из сети={stats.get('blocks_fetched', 0)}, "
                f"из кэша={stats.get('block_cache_hits', 0)})")
            save_json(CACHE_PATH, cache)

    save_json(CACHE_PATH, cache)
    out = {
        "generated_at_utc": now_utc(),
        "HONEST_NOTES": [
            "Измерение, не торговля: в этом скрипте нет ни одного вызова покупки/продажи.",
            "Подписи транзакций источника получены ТОЛЬКО из API (Helius JSON-RPC/Enhanced). "
            "Где не нашлось -- source_signature=null и source_lookup='not_found', ничего не подгонялось.",
            "Проверено программно до написания кода: хэша транзакции источника в записях DBot "
            "(record.follow.*) НЕТ ни в одной из 2903 записей -- предположение задачи не подтвердилось. "
            "Поэтому основной путь -- поиск на цепочке по кошельку источника, с усилением по raw-сумме "
            "из follow.receive.amount.",
            "getBlock вызывается с transactionDetails='accounts' (не 'full'): этот уровень отдаёт ровно "
            "подписи + pre/postTokenBalances, нужные для детекции покупателей, но в разы дешевле по трафику. "
            "При отсутствии токен-балансов honest-фоллбэк на 'full' (счётчик fallback_to_full).",
            "signal_pct = (sol_out - gross_sol_in)/gross_sol_in*100, где sol_out -- НЕТТО-дельта кошелька "
            "по цепочке, а gross_sol_in -- БРУТТО из записи DBot: метрика смешивает нетто-выход с брутто-входом, "
            "это ограничение исходных данных, а не расчёта.",
            "Слот сейчас 250-300 мс: offset_slots=1 это примерно четверть секунды отставания.",
        ],
        "config": {
            "source_window_s": SOURCE_WINDOW_S,
            "max_slot_scan": args.max_slot_scan,
            "max_candidates_per_trade": MAX_CANDIDATES_PER_TRADE,
            "max_history_pages": args.max_history_pages,
            "helius_key_env_name": key_name,
            "enhanced_api_tried": not args.no_enhanced,
        },
        "run_stats": {**stats, "rpc_calls": getattr(fp, "RPC_CALLS", None),
                       "elapsed_s": round(time.monotonic() - started, 1)},
        "summary": summarize(rows),
        "trades": rows,
    }
    save_json(OUT_PATH, out)
    log(f"ГОТОВО: {len(rows)} строк -> {OUT_PATH.name}; "
        f"с offset={out['summary']['n_with_offset']}, с buyers_between={out['summary']['n_with_buyers_between']}")


if __name__ == "__main__":
    main()
