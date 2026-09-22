#!/usr/bin/env python3
"""Владелец, Часть B: ЗОНД ОБНАРУЖЕНИЯ (измерение, не торговля).

Постоянная служба на NL-хосте: слушает сделки кошельков-источников через
вебсокет Helius и записывает, В КАКОЙ МОМЕНТ мы узнали о сделке лидера.
Потом сверяет это с тем, в каком слоте на цепочку села НАША покупка от
DBot. Главный вопрос владельца: сколько слотов проходит между тем, как
МЫ увидели транзакцию источника, и тем, как покупка DBot села в блок.

НИ ОДНОГО вызова покупки/продажи. Из DBot дёргаются только GET
/automation/follow_orders и GET /account/follow_trades. Из цепочки --
только чтение (getTransaction). Никаких POST /swap_orders*, никаких
отмен, никаких изменений задач.

СТОРОЖ ПРОВАЛЕННЫХ ПРОДАЖ НЕ ТРОГАЕТСЯ: у этой службы своё имя юнита
(dbot-detect-probe), свой файл окружения (/etc/dbot-detect-probe/env),
свой каталог состояния (/home/bot/dbot_detect_probe) и свой logrotate.
Пересечений по путям с dbot-sold-guard нет.

ПОЧЕМУ ПОДПИСКА ДЕЛАЕТСЯ ПО ОДНОМУ ИСТОЧНИКУ, А НЕ ОДНИМ accountInclude
СО ВСЕМ СПИСКОМ: при одной подписке на 45 адресов уведомление не
говорит, КАКОЙ из адресов совпал, а с transactionDetails="signatures"
списка счетов в уведомлении тоже нет -- источник пришлось бы
восстанавливать отдельным getTransaction, то есть терять сам смысл
замера "во сколько мы узнали". Поэтому на одном соединении держится по
одной подписке на источник: источник известен в момент получения, а
accountInclude с одним адресом -- это ровно тот же фильтр.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

import requests

try:
    import websockets
except ImportError:  # pragma: no cover
    websockets = None

DBOT_HOST = "https://api-bot-v1.dbotx.com"
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SIG_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{64,90}$")

STATE_DIR = Path(os.environ.get("PROBE_STATE_DIR", "/home/bot/dbot_detect_probe"))
EVENTS_PATH = STATE_DIR / "events.jsonl"
ENRICHED_PATH = STATE_DIR / "enriched.jsonl"
STATUS_PATH = STATE_DIR / "status.json"

SOURCES_REFRESH_S = int(os.environ.get("PROBE_SOURCES_REFRESH_S", "600"))
ENRICH_EVERY_S = int(os.environ.get("PROBE_ENRICH_EVERY_S", "300"))
ENRICH_MIN_AGE_S = int(os.environ.get("PROBE_ENRICH_MIN_AGE_S", "60"))
CLOCK_CHECK_EVERY_S = int(os.environ.get("PROBE_CLOCK_CHECK_EVERY_S", "3600"))

_ACTIVE_SECRETS: list[str] = []


def _scrub_all(text: str) -> str:
    for s in _ACTIVE_SECRETS:
        if s:
            text = text.replace(s, "[REDACTED]")
    return text


class _ScrubbingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _scrub_all(super().format(record))


log = logging.getLogger("detect_probe")


def setup_logging() -> None:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(_ScrubbingFormatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%SZ"))
    logging.Formatter.converter = time.gmtime
    log.addHandler(h)
    log.setLevel(logging.INFO)


def env_key(*names: str) -> tuple[str, str]:
    """Владелец просит HELIUS_API_KEY; в секретах репозитория реально
    заведён HELIUS_API. Пробуем в порядке просьбы и говорим, что сработало."""
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            if any(c in v for c in ("\n", "\r")):
                raise RuntimeError(f"{n} содержит перевод строки -- не похоже на сырой ключ")
            _ACTIVE_SECRETS.append(v)
            return v, n
    raise RuntimeError("не задано ни одно из: " + ", ".join(names))


# ---------- общее состояние ----------

class State:
    def __init__(self) -> None:
        self.current_slot: int | None = None
        self.t_slot_seen: float | None = None      # time.time() когда пришло slotNotification
        self.slot_notifications = 0
        self.sources: dict[str, dict] = {}          # address -> {remark, tasks:[...]}
        self.sources_generation = 0
        self.tx_method: str | None = None           # что РЕАЛЬНО сработало
        self.tx_method_history: list[dict] = []
        self.events_written = 0
        self.dup_skipped = 0
        self.failed_skipped = 0
        self.tx_reconnects = 0
        self.slot_reconnects = 0
        self.started_at = time.time()
        self.clock: dict = {}
        self.last_event_at: float | None = None
        self.seen_sigs: deque[str] = deque(maxlen=50000)
        self.seen_set: set[str] = set()

    def mark_seen(self, sig: str) -> bool:
        """True -- подпись новая (и запомнена), False -- уже была."""
        if sig in self.seen_set:
            return False
        if len(self.seen_sigs) == self.seen_sigs.maxlen:
            self.seen_set.discard(self.seen_sigs[0])
        self.seen_sigs.append(sig)
        self.seen_set.add(sig)
        return True


ST = State()


# ---------- DBot REST (только чтение) ----------

def dbot_get(path: str, params: dict, api_key: str) -> tuple[int | None, dict]:
    for attempt in range(5):
        try:
            resp = requests.get(f"{DBOT_HOST}{path}", params=params,
                                 headers={"X-API-KEY": api_key}, timeout=30)
        except Exception as exc:  # noqa: BLE001
            log.warning("dbot_get %s попытка %d/5: %s", path, attempt + 1, f"{type(exc).__name__}: {exc}")
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code == 429:
            time.sleep(3 * (attempt + 1))
            continue
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {"non_json_body": _scrub_all(resp.text[:500])}
    return None, {}


def extract_items(body) -> list:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in ("res", "data", "results", "list", "items"):
            v = body.get(k)
            if isinstance(v, list):
                return v
    return []


def has_recognized_list_shape(body) -> bool:
    if isinstance(body, list):
        return True
    if isinstance(body, dict):
        return any(isinstance(body.get(k), list) for k in ("res", "data", "results", "list", "items"))
    return False


def fetch_sources(api_key: str) -> dict[str, dict]:
    """Источники живьём из /automation/follow_orders -- targetIds каждой
    задачи. Не хардкодится: добавит владелец BATCH-9 -- подхватится на
    следующем обновлении без передеплоя."""
    status, body = dbot_get("/automation/follow_orders", {}, api_key)
    if status != 200:
        raise RuntimeError(f"follow_orders вернул http={status}")
    if body and not has_recognized_list_shape(body):
        raise RuntimeError("follow_orders: HTTP 200, но форма ответа не распознана: "
                           + _scrub_all(json.dumps(body, ensure_ascii=False, default=str)[:800]))
    out: dict[str, dict] = {}
    for row in extract_items(body):
        if not row.get("enabled", True):
            continue
        names = row.get("targetNames") or []
        for i, addr in enumerate(row.get("targetIds") or []):
            node = out.setdefault(addr, {"remark": None, "tasks": []})
            node["tasks"].append({"task_id": row.get("id") or row.get("_id"),
                                   "task_name": row.get("name"),
                                   "our_wallet": row.get("walletAddress")})
            if not node["remark"] and i < len(names) and names[i]:
                node["remark"] = names[i]
    if not out:
        raise RuntimeError("follow_orders вернул 0 источников -- не подписываюсь на пустоту")
    return out


def fetch_follow_trades(task_id: str, api_key: str, max_pages: int = 3) -> list[dict]:
    """Свежие сделки задачи. Страницы с 0 (тот же подтверждённый баг
    нумерации, что чинили в сторожe и в учёте). Для обогащения нужны
    только самые свежие, поэтому потолок страниц маленький."""
    out: list[dict] = []
    for page in range(max_pages):
        status, body = dbot_get("/account/follow_trades",
                                 {"chain": "solana", "configId": task_id, "size": 20, "page": page}, api_key)
        if status != 200:
            break
        if body and not has_recognized_list_shape(body):
            log.warning("follow_trades стр.%d: HTTP 200, форма НЕ распознана: %s", page,
                        _scrub_all(json.dumps(body, ensure_ascii=False, default=str)[:1000]))
            break
        items = extract_items(body)
        if not items:
            break
        out.extend(items)
    return out


def dbot_signature(rec: dict) -> str | None:
    links = rec.get("links") or {}
    for k in ("etherscan", "dexscreener", "uniswap"):
        url = links.get(k)
        if url and "/tx/" in url:
            sig = url.rsplit("/tx/", 1)[-1].strip()
            if SIG_RE.match(sig):
                return sig
    return None


# ---------- Solana JSON-RPC (только чтение) ----------

def rpc(method: str, params: list, key: str) -> dict | None:
    url = f"https://mainnet.helius-rpc.com/?api-key={key}"
    for attempt in range(4):
        try:
            resp = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                  timeout=30)
        except Exception as exc:  # noqa: BLE001
            log.warning("rpc %s попытка %d/4: %s", method, attempt + 1, f"{type(exc).__name__}: {exc}")
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            time.sleep(2 * (attempt + 1))
            continue
        if not resp.ok:
            log.warning("rpc %s http=%d: %s", method, resp.status_code, _scrub_all(resp.text[:200]))
            return None
        body = resp.json()
        if "error" in body:
            log.warning("rpc %s ошибка: %s", method, _scrub_all(str(body["error"])[:200]))
            return None
        return body.get("result")
    return None


def get_tx(sig: str, key: str) -> dict | None:
    # maxSupportedTransactionVersion=1: на реальном прогоне Части A узел
    # отвечал -32015 "Transaction version (1) is not supported" на
    # потолке 0 -- в блоках сейчас есть транзакции версии 1.
    return rpc("getTransaction", [sig, {"encoding": "jsonParsed",
                                         "maxSupportedTransactionVersion": 1}], key)


def owner_token_deltas(tx: dict, owner: str) -> dict[str, float]:
    meta = tx.get("meta") or {}
    pre: dict[str, float] = {}
    post: dict[str, float] = {}
    for b in meta.get("preTokenBalances") or []:
        if b.get("owner") == owner:
            m = b.get("mint")
            pre[m] = pre.get(m, 0.0) + float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
    for b in meta.get("postTokenBalances") or []:
        if b.get("owner") == owner:
            m = b.get("mint")
            post[m] = post.get(m, 0.0) + float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
    return {m: post.get(m, 0.0) - pre.get(m, 0.0) for m in set(pre) | set(post)}


def sol_delta_for_owner(tx: dict, owner: str) -> float | None:
    msg = ((tx.get("transaction") or {}).get("message")) or {}
    keys = msg.get("accountKeys") or []
    idx = None
    for i, k in enumerate(keys):
        pk = k.get("pubkey") if isinstance(k, dict) else k
        if pk == owner:
            idx = i
            break
    if idx is None:
        return None
    meta = tx.get("meta") or {}
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    if idx >= len(pre) or idx >= len(post):
        return None
    return (post[idx] - pre[idx]) / 1e9


def describe_swap(tx: dict, owner: str) -> dict:
    """Что именно купил источник: минт с наибольшим положительным
    приростом у него, и чем заплатил (SOL и/или USDC)."""
    deltas = owner_token_deltas(tx, owner)
    bought, bought_amount = None, 0.0
    for m, d in deltas.items():
        if m in (WSOL, USDC):
            continue
        if d > bought_amount:
            bought, bought_amount = m, d
    return {
        "bought_mint": bought,
        "bought_amount": round(bought_amount, 9) if bought else None,
        "usdc_delta": round(deltas.get(USDC, 0.0), 6) or None,
        "wsol_delta": round(deltas.get(WSOL, 0.0), 9) or None,
        "sol_delta": (lambda v: round(v, 9) if v is not None else None)(sol_delta_for_owner(tx, owner)),
    }


# ---------- запись событий ----------

def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def record_event(signature: str, slot: int | None, source: str | None, via: str) -> None:
    t_recv = time.time()
    if not ST.mark_seen(signature):
        ST.dup_skipped += 1
        return
    cur, seen_at = ST.current_slot, ST.t_slot_seen
    ev = {
        "t_recv": round(t_recv, 6),
        "t_recv_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t_recv)),
        "signature": signature,
        "slot": slot,
        "source": source,
        "source_remark": (ST.sources.get(source) or {}).get("remark") if source else None,
        "current_slot_at_recv": cur,
        "lag_slots": (cur - slot) if (cur is not None and slot is not None) else None,
        "ms_since_slot_seen": round((t_recv - seen_at) * 1000, 1) if seen_at is not None else None,
        "via": via,
    }
    append_jsonl(EVENTS_PATH, ev)
    ST.events_written += 1
    ST.last_event_at = t_recv
    log.info("событие: %s… src=%s slot=%s lag_slots=%s ms_since_slot=%s via=%s",
             signature[:12], (source or "?")[:10], slot, ev["lag_slots"], ev["ms_since_slot_seen"], via)


# ---------- вебсокеты ----------

def ws_url(key: str, atlas: bool) -> str:
    host = "atlas-mainnet.helius-rpc.com" if atlas else "mainnet.helius-rpc.com"
    return f"wss://{host}/?api-key={key}"


async def slot_watcher(key: str) -> None:
    """slotSubscribe -- часы в слотах. Параметра commitment у него нет:
    он и есть уровень processed (шлёт слот, как только узел его
    обрабатывает) -- так и фиксируем в отчёте, а не делаем вид, что
    передали commitment."""
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(ws_url(key, atlas=False), ping_interval=20,
                                           ping_timeout=20, max_size=8 * 1024 * 1024) as ws:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "slotSubscribe", "params": []}))
                ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                if "error" in ack:
                    raise RuntimeError(f"slotSubscribe отклонён: {ack['error']}")
                log.info("slotSubscribe подтверждён (подписка id=%s)", ack.get("result"))
                backoff = 1.0
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("method") != "slotNotification":
                        continue
                    res = ((msg.get("params") or {}).get("result")) or {}
                    slot = res.get("slot")
                    if isinstance(slot, int):
                        ST.current_slot = slot
                        ST.t_slot_seen = time.time()
                        ST.slot_notifications += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            ST.slot_reconnects += 1
            log.warning("slot_watcher оборвался (%s: %s) -- переподключение через %.0fс",
                        type(exc).__name__, str(exc)[:200], backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


async def _subscribe_transactions(ws, sources: list[str]) -> None:
    for i, addr in enumerate(sources, 1):
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "id": i, "method": "transactionSubscribe",
            "params": [
                {"accountInclude": [addr], "failed": False, "vote": False},
                {"commitment": "processed", "transactionDetails": "signatures",
                 "encoding": "jsonParsed", "showRewards": False},
            ],
        }))


async def _subscribe_logs(ws, sources: list[str]) -> None:
    for i, addr in enumerate(sources, 1):
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "id": i, "method": "logsSubscribe",
            "params": [{"mentions": [addr]}, {"commitment": "processed"}],
        }))


def _sig_and_slot(res: dict) -> tuple[str | None, int | None]:
    """Форма уведомления у transactionSubscribe разнится между версиями
    Helius -- достаём подпись и слот из всех известных мест, а не из
    одного жёстко зашитого пути."""
    slot = res.get("slot")
    if slot is None:
        slot = ((res.get("context") or {}).get("slot"))
    sig = res.get("signature")
    if not sig:
        tx = res.get("transaction") or {}
        inner = tx.get("transaction") if isinstance(tx.get("transaction"), dict) else tx
        sigs = (inner or {}).get("signatures") or []
        sig = sigs[0] if sigs else None
    if not sig:
        sig = ((res.get("value") or {}).get("signature"))
    return (sig if isinstance(sig, str) and SIG_RE.match(sig) else None,
            slot if isinstance(slot, int) else None)


async def tx_watcher(key: str) -> None:
    """Сначала transactionSubscribe (Helius Enhanced Websockets, хост
    atlas-*), как просил владелец. Если он не подтверждается -- честный
    откат на logsSubscribe {mentions:[addr]} на обычном хосте, по одной
    подписке на адрес. Что РЕАЛЬНО сработало -- пишется в лог и в
    status.json, а не предполагается."""
    backoff = 1.0
    prefer_atlas = True
    while True:
        sources = sorted(ST.sources)
        generation = ST.sources_generation
        if not sources:
            await asyncio.sleep(2)
            continue
        atlas = prefer_atlas
        method = "transactionSubscribe" if atlas else "logsSubscribe"
        try:
            async with websockets.connect(ws_url(key, atlas=atlas), ping_interval=20,
                                           ping_timeout=20, max_size=16 * 1024 * 1024) as ws:
                if atlas:
                    await _subscribe_transactions(ws, sources)
                else:
                    await _subscribe_logs(ws, sources)

                acked, rejected = 0, []
                deadline = time.time() + 45
                while acked + len(rejected) < len(sources) and time.time() < deadline:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    msg = json.loads(raw)
                    if "id" not in msg:
                        continue
                    if "error" in msg:
                        rejected.append(msg["error"])
                    else:
                        acked += 1
                if acked == 0:
                    raise RuntimeError(f"{method}: ни одна подписка не подтверждена, "
                                        f"отказы: {str(rejected[:2])[:300]}")
                if rejected:
                    log.warning("%s: подтверждено %d из %d, отказов %d (первый: %s)",
                                method, acked, len(sources), len(rejected), str(rejected[0])[:200])

                ST.tx_method = method
                ST.tx_method_history.append({"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                              "method": method, "acked": acked, "of": len(sources)})
                log.info("РАБОТАЕТ %s: подтверждено %d подписок из %d (хост %s)",
                         method, acked, len(sources), "atlas" if atlas else "mainnet")
                write_status()
                backoff = 1.0

                # по одной подписке на адрес -- id подписки -> адрес
                sub_of: dict[int, str] = {}
                # id запроса == порядковый номер адреса (см. _subscribe_*)
                id_to_addr = {i: a for i, a in enumerate(sources, 1)}

                async for raw in ws:
                    if ST.sources_generation != generation:
                        log.info("список источников изменился -- переподписываюсь")
                        break
                    msg = json.loads(raw)
                    if "id" in msg and "result" in msg and isinstance(msg["result"], int):
                        addr = id_to_addr.get(msg["id"])
                        if addr:
                            sub_of[msg["result"]] = addr
                        continue
                    m = msg.get("method")
                    if m not in ("transactionNotification", "logsNotification"):
                        continue
                    params = msg.get("params") or {}
                    res = params.get("result") or {}
                    sub_id = params.get("subscription")
                    source = sub_of.get(sub_id)
                    if m == "logsNotification":
                        val = res.get("value") or {}
                        if val.get("err") is not None:
                            ST.failed_skipped += 1     # владелец: failed:false
                            continue
                        sig = val.get("signature")
                        slot = (res.get("context") or {}).get("slot")
                        if isinstance(sig, str) and SIG_RE.match(sig):
                            record_event(sig, slot if isinstance(slot, int) else None, source, "logsSubscribe")
                        continue
                    sig, slot = _sig_and_slot(res)
                    if sig:
                        record_event(sig, slot, source, "transactionSubscribe")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            ST.tx_reconnects += 1
            log.warning("tx_watcher (%s) оборвался (%s: %s)", method, type(exc).__name__, str(exc)[:300])
            if atlas:
                # Enhanced Websockets есть не на всех тарифах -- один
                # честный откат, дальше держимся рабочего способа.
                log.warning("перехожу на запасной logsSubscribe (обычный вебсокет)")
                prefer_atlas = False
                await asyncio.sleep(1)
                continue
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


# ---------- фоновые задачи ----------

async def sources_refresher(dbot_key: str) -> None:
    while True:
        try:
            fresh = await asyncio.to_thread(fetch_sources, dbot_key)
            if set(fresh) != set(ST.sources):
                added = sorted(set(fresh) - set(ST.sources))
                gone = sorted(set(ST.sources) - set(fresh))
                ST.sources = fresh
                ST.sources_generation += 1
                log.info("источники обновлены: всего %d (+%d %s, -%d %s)",
                         len(fresh), len(added), [a[:8] for a in added], len(gone), [a[:8] for a in gone])
            else:
                ST.sources = fresh
        except Exception as exc:  # noqa: BLE001
            log.warning("обновление источников не удалось: %s: %s", type(exc).__name__, str(exc)[:200])
        write_status()
        await asyncio.sleep(SOURCES_REFRESH_S)


def enrich_once(helius_key: str, dbot_key: str) -> int:
    """Раз в 5 минут добираем подробности по событиям старше 60с.
    Почему не сразу: наша покупка от DBot к моменту детекта ещё не села
    в блок, и в follow_trades её ещё нет -- мгновенное обогащение просто
    ничего не нашло бы."""
    done = {e.get("signature") for e in read_jsonl(ENRICHED_PATH)}
    events = [e for e in read_jsonl(EVENTS_PATH)
              if e.get("signature") not in done
              and time.time() - float(e.get("t_recv") or 0) > ENRICH_MIN_AGE_S]
    if not events:
        return 0

    # follow_trades тянем один раз на задачу, а не на событие
    trades_by_task: dict[str, list[dict]] = {}
    n = 0
    for ev in events:
        src = ev.get("source")
        out = dict(ev)
        out["enriched_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tx = get_tx(ev["signature"], helius_key)
        if not tx:
            out["enrich_note"] = "getTransaction ничего не вернул (мог быть откат или ещё не финализирована)"
            append_jsonl(ENRICHED_PATH, out)
            n += 1
            continue
        out["source_slot"] = tx.get("slot")
        out["source_block_time"] = tx.get("blockTime")
        out["source_tx_err"] = (tx.get("meta") or {}).get("err") is not None
        if src:
            out.update(describe_swap(tx, src))
        mint = out.get("bought_mint")

        # наша покупка от DBot по этому же минту
        tasks = (ST.sources.get(src) or {}).get("tasks") or [] if src else []
        best = None
        for task in tasks:
            tid = task.get("task_id")
            if not tid:
                continue
            if tid not in trades_by_task:
                trades_by_task[tid] = fetch_follow_trades(tid, dbot_key)
            for rec in trades_by_task[tid]:
                follow = rec.get("follow") or {}
                if follow.get("wallet") != src:
                    continue
                recv = ((follow.get("receive") or {}).get("info") or {}).get("contract")
                send = ((follow.get("send") or {}).get("info") or {}).get("contract")
                our_recv = ((rec.get("receive") or {}).get("info") or {}).get("contract")
                if mint and mint not in (recv, send, our_recv):
                    continue
                created = rec.get("createAt")
                if not isinstance(created, (int, float)):
                    continue
                dt_ms = created - float(ev["t_recv"]) * 1000
                if dt_ms < -60_000 or dt_ms > 600_000:
                    continue
                if best is None or dt_ms < best[0]:
                    best = (dt_ms, rec, task)
        if best is None:
            out["dbot_match"] = "не найдена"
            append_jsonl(ENRICHED_PATH, out)
            n += 1
            continue

        dt_ms, rec, task = best
        our_sig = dbot_signature(rec)
        out["dbot_match"] = "найдена"
        out["dbot_task_name"] = task.get("task_name")
        out["our_wallet"] = task.get("our_wallet")
        out["our_signature"] = our_sig
        out["dbot_record_create_at_ms"] = rec.get("createAt")
        out["ms_detect_to_dbot_record"] = round(dt_ms, 1)
        if our_sig:
            otx = get_tx(our_sig, helius_key)
            if otx:
                out["our_slot"] = otx.get("slot")
                out["our_block_time"] = otx.get("blockTime")
                out["our_tx_err"] = (otx.get("meta") or {}).get("err") is not None
                if isinstance(out.get("our_slot"), int) and isinstance(out.get("source_slot"), int):
                    out["dbot_offset_slots"] = out["our_slot"] - out["source_slot"]
                if isinstance(out.get("our_block_time"), int):
                    out["ms_detect_to_dbot_land"] = round(
                        out["our_block_time"] * 1000 - float(ev["t_recv"]) * 1000, 1)
                    out["ms_detect_to_dbot_land_точность"] = (
                        "blockTime отдаётся с точностью до секунды -- эта величина ГРУБАЯ (±1000 мс); "
                        "точная по времени -- ms_detect_to_dbot_record (createAt DBot в миллисекундах), "
                        "точная по цепочке -- dbot_offset_slots")
        append_jsonl(ENRICHED_PATH, out)
        n += 1
    return n


async def enricher(helius_key: str, dbot_key: str) -> None:
    while True:
        await asyncio.sleep(ENRICH_EVERY_S)
        try:
            n = await asyncio.to_thread(enrich_once, helius_key, dbot_key)
            if n:
                log.info("обогащено событий: %d", n)
                write_status()
        except Exception as exc:  # noqa: BLE001
            log.warning("обогащение упало: %s: %s", type(exc).__name__, str(exc)[:300])


def clock_report() -> dict:
    """Владелец: проверить синхронизацию часов -- иначе t_recv нельзя
    сопоставлять с временем блока. Берём то, что реально есть на хосте."""
    out: dict = {"checked_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    for name, cmd in (("timedatectl", ["timedatectl", "show"]),
                      ("chronyc", ["chronyc", "tracking"]),
                      ("ntpq", ["ntpq", "-p"])):
        if not shutil.which(cmd[0]):
            out[name] = "нет на хосте"
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            out[name] = (r.stdout or r.stderr).strip()[:1200]
        except Exception as exc:  # noqa: BLE001
            out[name] = f"{type(exc).__name__}: {exc}"
    td = out.get("timedatectl") or ""
    for field in ("NTPSynchronized=", "NTP="):
        for line in td.splitlines():
            if line.startswith(field):
                out.setdefault("ntp_synchronized", line.split("=", 1)[1])
    for line in (out.get("chronyc") or "").splitlines():
        if "System time" in line:
            out["chrony_system_time"] = line.strip()
        if "Last offset" in line:
            out["chrony_last_offset"] = line.strip()
    return out


async def clock_watcher() -> None:
    while True:
        try:
            ST.clock = await asyncio.to_thread(clock_report)
            log.info("часы: NTP=%s %s", ST.clock.get("ntp_synchronized", "?"),
                     ST.clock.get("chrony_system_time", ""))
            write_status()
        except Exception as exc:  # noqa: BLE001
            log.warning("проверка часов упала: %s: %s", type(exc).__name__, str(exc)[:200])
        await asyncio.sleep(CLOCK_CHECK_EVERY_S)


def write_status() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps({
        "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "uptime_s": round(time.time() - ST.started_at, 1),
        "tx_method_работает": ST.tx_method,
        "tx_method_история": ST.tx_method_history[-10:],
        "источников": len(ST.sources),
        "событий_записано": ST.events_written,
        "дублей_пропущено": ST.dup_skipped,
        "неудачных_пропущено": ST.failed_skipped,
        "переподключений_tx": ST.tx_reconnects,
        "переподключений_slot": ST.slot_reconnects,
        "slot_уведомлений": ST.slot_notifications,
        "текущий_слот": ST.current_slot,
        "последнее_событие_utc": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ST.last_event_at))
                                   if ST.last_event_at else None),
        "часы": ST.clock,
    }, ensure_ascii=False, indent=2))


async def status_ticker() -> None:
    while True:
        await asyncio.sleep(60)
        write_status()
        log.info("статус: способ=%s источников=%d событий=%d дублей=%d слот=%s переподключений tx/slot=%d/%d",
                 ST.tx_method, len(ST.sources), ST.events_written, ST.dup_skipped,
                 ST.current_slot, ST.tx_reconnects, ST.slot_reconnects)


# ---------- отчёт ----------

def print_report(n_selfcheck: int = 3) -> None:
    events = read_jsonl(EVENTS_PATH)
    enriched = read_jsonl(ENRICHED_PATH)
    print(f"=== ЗОНД ОБНАРУЖЕНИЯ: отчёт на {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ===")
    if STATUS_PATH.exists():
        print("--- состояние службы ---")
        print(STATUS_PATH.read_text())
    print(f"событий всего: {len(events)}; обогащено: {len(enriched)}")
    if not events:
        print("событий нет -- отчитываться не о чем (это честный ноль, а не сбой разбора)")
        return

    def dist(vals):
        out: dict = {}
        for v in vals:
            out[v] = out.get(v, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: str(kv[0])))

    lag = [e["lag_slots"] for e in events if isinstance(e.get("lag_slots"), int)]
    ms = [e["ms_since_slot_seen"] for e in events if isinstance(e.get("ms_since_slot_seen"), (int, float))]
    print("\n--- как быстро мы узнаём о сделке источника ---")
    print(f"lag_slots (текущий слот минус слот транзакции) n={len(lag)}: "
          f"{dist(lag) if lag else 'нет данных'}")
    if lag:
        print(f"  медиана={statistics.median(lag)}")
    if ms:
        print(f"ms_since_slot_seen n={len(ms)}: медиана={round(statistics.median(ms), 1)} "
              f"мин={round(min(ms), 1)} макс={round(max(ms), 1)}")
    print(f"по способу: {dist([e.get('via') for e in events])}")
    print(f"по источникам (топ-10): "
          f"{dict(sorted(dist([e.get('source') for e in events]).items(), key=lambda kv: -kv[1])[:10])}")

    off = [e["dbot_offset_slots"] for e in enriched if isinstance(e.get("dbot_offset_slots"), int)]
    land = [e["ms_detect_to_dbot_land"] for e in enriched
            if isinstance(e.get("ms_detect_to_dbot_land"), (int, float))]
    rec_ms = [e["ms_detect_to_dbot_record"] for e in enriched
              if isinstance(e.get("ms_detect_to_dbot_record"), (int, float))]
    print("\n=== ГЛАВНЫЙ ВОПРОС ВЛАДЕЛЬЦА ===")
    print("Сколько слотов проходит между тем, как МЫ увидели транзакцию источника,")
    print("и тем, как покупка DBot села на цепочку:")
    if off:
        print(f"  dbot_offset_slots n={len(off)}: {dist(off)}")
        print(f"  медиана={statistics.median(off)} слотов (~{statistics.median(off) * 0.275:.2f}с при слоте 250-300мс)")
    else:
        print("  ПОКА НЕТ ДАННЫХ: ни одно событие не сшито с нашей покупкой "
              "(источники могли не торговать, или DBot не скопировал сделку)")
    if rec_ms:
        print(f"  ms_detect_to_dbot_record (детект -> запись DBot, мс точность) n={len(rec_ms)}: "
              f"медиана={round(statistics.median(rec_ms), 1)}")
    if land:
        print(f"  ms_detect_to_dbot_land (детект -> блок нашей покупки, ГРУБО ±1000мс) n={len(land)}: "
              f"медиана={round(statistics.median(land), 1)}")
    print(f"  сшивок с DBot: {dist([e.get('dbot_match') for e in enriched])}")

    print("\n=== САМОПРОВЕРКА: сверить вручную на Solscan ===")
    pool = [e for e in enriched if e.get("dbot_offset_slots") is not None] or enriched or events
    for e in pool[:n_selfcheck]:
        print(f"\n  событие {e.get('t_recv_utc')} источник={e.get('source')}")
        print(f"    транзакция источника: https://solscan.io/tx/{e.get('signature')}")
        print(f"      ожидаем slot={e.get('slot')} (из вебсокета), source_slot={e.get('source_slot')} (из getTransaction)")
        if e.get("our_signature"):
            print(f"    наша покупка:         https://solscan.io/tx/{e.get('our_signature')}")
            print(f"      ожидаем our_slot={e.get('our_slot')}, "
                  f"dbot_offset_slots={e.get('dbot_offset_slots')} = our_slot - source_slot")
        else:
            print("    наша покупка: не сшита -- сверять нечего")
    print("\n  Как сверять: открыть ссылку, найти на странице Solscan поле Slot (Block) и")
    print("  сравнить с ожидаемым числом выше. dbot_offset_slots должен быть ровно разностью.")


# ---------- main ----------

def check_only(helius_key: str, dbot_key: str) -> None:
    print("=== ПРОВЕРКА БЕЗ ЗАПУСКА СЛУЖБЫ (чистое чтение) ===")
    src = fetch_sources(dbot_key)
    print(f"источников из follow_orders: {len(src)}")
    for a, v in sorted(src.items(), key=lambda kv: -len(kv[1]["tasks"]))[:50]:
        print(f"  {a}  remark={v['remark']}  задач={len(v['tasks'])} "
              f"({', '.join(t['task_name'] or '?' for t in v['tasks'])})")
    slot = rpc("getSlot", [{"commitment": "processed"}], helius_key)
    print(f"getSlot(processed) = {slot}")
    print("часы:")
    for k, v in clock_report().items():
        print(f"  {k}: {str(v)[:300]}")
    print("\nВНИМАНИЕ: это только чтение. Ни одного вызова покупки/продажи в этом файле нет.")


async def run_service(helius_key: str, dbot_key: str) -> None:
    if websockets is None:
        raise RuntimeError("не установлен пакет websockets -- служба не может слушать вебсокет")
    ST.sources = await asyncio.to_thread(fetch_sources, dbot_key)
    log.info("старт: источников %d, каталог состояния %s", len(ST.sources), STATE_DIR)
    ST.clock = await asyncio.to_thread(clock_report)
    log.info("часы при старте: NTP=%s", ST.clock.get("ntp_synchronized", "?"))
    write_status()
    tasks = [asyncio.create_task(t) for t in (
        slot_watcher(helius_key),
        tx_watcher(helius_key),
        sources_refresher(dbot_key),
        enricher(helius_key, dbot_key),
        clock_watcher(),
        status_ticker(),
    )]
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check-only", action="store_true", help="показать источники/слот/часы и выйти")
    ap.add_argument("--report", action="store_true", help="отчёт по накопленным событиям и выйти")
    ap.add_argument("--enrich-now", action="store_true", help="один прогон обогащения и выйти")
    args = ap.parse_args()
    setup_logging()

    if args.report:
        print_report()
        return

    helius_key, helius_name = env_key("HELIUS_API_KEY", "HELIUS_API")
    dbot_key, _ = env_key("DBOT_API_KEY")
    log.info("ключ Helius взят из переменной %s", helius_name)

    if args.check_only:
        check_only(helius_key, dbot_key)
        return
    if args.enrich_now:
        ST.sources = fetch_sources(dbot_key)
        print("обогащено:", enrich_once(helius_key, dbot_key))
        return

    try:
        asyncio.run(run_service(helius_key, dbot_key))
    except KeyboardInterrupt:
        log.info("остановлен по сигналу")


if __name__ == "__main__":
    main()
