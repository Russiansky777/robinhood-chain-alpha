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
        # ПОЧЕМУ рвётся соединение. Keepalive (ping_interval=20) стоял с
        # самого начала, значит 221 переподключение за сутки -- не от
        # отсутствия пингов. Без учёта причин чинить было бы гаданием.
        self.tx_close_reasons: dict[str, int] = {}
        self.tx_blind_s = 0.0          # суммарное время без подписки
        self.tx_last_drop_at: float | None = None
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

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"


def rpc(method: str, params: list, key: str, url: str | None = None) -> dict | None:
    url = url or f"https://mainnet.helius-rpc.com/?api-key={key}"
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


# Допуск только на расхождение часов между нашим детектом и меткой
# createAt у DBot. Раньше здесь было -60 000 мс, и запись, созданная до
# минуты РАНЬШЕ детекта, засчитывалась копией этого события.
MATCH_CLOCK_SKEW_MS = -2_000

# Пинги были всегда (ping_interval=20). Поднят только ТАЙМАУТ ожидания
# понга: при 63 подписках и плотном потоке понг может задержаться, и
# тогда соединение рвёт НАШ клиент, а не сервер. 20с -- слишком жёстко.
PING_TIMEOUT_S = 60

# Подписи покупок, уже приписанных событию: одна покупка не может быть
# копией двух разных транзакций источника.
USED_DBOT_SIGS: set[str] = set()


def rpc_checked(method: str, params: list, key: str,
                 url: str | None = None) -> tuple[dict | None, str | None]:
    """То же, что rpc(), но РАЗЛИЧАЕТ исходы: (результат, причина_отказа).

    Зачем. rpc() отдаёт None в четырёх разных случаях: транзакции правда
    нет; HTTP-ошибка; ошибка в теле ответа; исчерпаны попытки. В
    самопроверке это привело к вердикту "РАСХОЖДЕНИЕ" там, где на самом
    деле провайдер просто не ответил, а записанные данные были верны --
    то есть сбой был выдан за факт."""
    url = url or f"https://mainnet.helius-rpc.com/?api-key={key}"
    last = None
    for attempt in range(4):
        try:
            resp = requests.post(url, json={"jsonrpc": "2.0", "id": 1,
                                             "method": method, "params": params}, timeout=30)
        except Exception as exc:  # noqa: BLE001
            last = f"сеть: {type(exc).__name__}"
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            last = f"http={resp.status_code}"
            time.sleep(2 * (attempt + 1))
            continue
        if not resp.ok:
            return None, f"http={resp.status_code}: {_scrub_all(resp.text[:160])}"
        body = resp.json()
        if "error" in body:
            return None, f"ошибка RPC: {_scrub_all(str(body['error'])[:160])}"
        res = body.get("result")
        return res, (None if res is not None else "узел ответил: такой транзакции нет")
    return None, f"исчерпаны попытки, последняя причина: {last}"


def get_tx_checked(sig: str, key: str, url: str | None = None) -> tuple[dict | None, str | None]:
    return rpc_checked("getTransaction", [sig, {"encoding": "jsonParsed",
                                                 "maxSupportedTransactionVersion": 1}], key, url)


def get_tx(sig: str, key: str, url: str | None = None) -> dict | None:
    # maxSupportedTransactionVersion=1: на реальном прогоне Части A узел
    # отвечал -32015 "Transaction version (1) is not supported" на
    # потолке 0 -- в блоках сейчас есть транзакции версии 1.
    return rpc("getTransaction", [sig, {"encoding": "jsonParsed",
                                         "maxSupportedTransactionVersion": 1}], key, url)


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
                                           ping_timeout=PING_TIMEOUT_S, max_size=8 * 1024 * 1024) as ws:
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


def _handle_notification(msg: dict, sub_of: dict[int, str]) -> None:
    """Разбор одного уведомления. Вынесено отдельно, чтобы события,
    прилетевшие ПОКА ещё подтверждаются остальные подписки, не
    терялись: на 45 адресах подтверждения идут вперемешку с первыми
    уведомлениями, и «пропустим всё, у чего нет id» выбрасывало бы
    ровно те события, ради которых служба и запущена."""
    m = msg.get("method")
    if m not in ("transactionNotification", "logsNotification"):
        return
    params = msg.get("params") or {}
    res = params.get("result") or {}
    source = sub_of.get(params.get("subscription"))
    if m == "logsNotification":
        val = res.get("value") or {}
        if val.get("err") is not None:
            ST.failed_skipped += 1          # владелец: failed:false
            return
        sig = val.get("signature")
        slot = (res.get("context") or {}).get("slot")
        if isinstance(sig, str) and SIG_RE.match(sig):
            record_event(sig, slot if isinstance(slot, int) else None, source, "logsSubscribe")
        return
    sig, slot = _sig_and_slot(res)
    if sig:
        record_event(sig, slot, source, "transactionSubscribe")


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
                                           ping_timeout=PING_TIMEOUT_S, max_size=16 * 1024 * 1024) as ws:
                if atlas:
                    await _subscribe_transactions(ws, sources)
                else:
                    await _subscribe_logs(ws, sources)

                # id запроса == порядковый номер адреса (см. _subscribe_*)
                id_to_addr = {i: a for i, a in enumerate(sources, 1)}
                sub_of: dict[int, str] = {}     # id подписки -> адрес источника
                acked, rejected = 0, []
                confirmed = False
                deadline = time.time() + 45

                async for raw in ws:
                    if ST.sources_generation != generation:
                        log.info("список источников изменился -- переподписываюсь")
                        break
                    msg = json.loads(raw)

                    if "id" in msg and ("result" in msg or "error" in msg):
                        if "error" in msg:
                            rejected.append(msg["error"])
                        else:
                            acked += 1
                            if isinstance(msg.get("result"), int):
                                addr = id_to_addr.get(msg["id"])
                                if addr:
                                    sub_of[msg["result"]] = addr
                    else:
                        _handle_notification(msg, sub_of)

                    if not confirmed:
                        if acked + len(rejected) >= len(sources) or time.time() > deadline:
                            if acked == 0:
                                raise RuntimeError(f"{method}: ни одна подписка не подтверждена, "
                                                    f"отказы: {str(rejected[:2])[:300]}")
                            confirmed = True
                            if rejected:
                                log.warning("%s: подтверждено %d из %d, отказов %d (первый: %s)",
                                            method, acked, len(sources), len(rejected), str(rejected[0])[:200])
                            if ST.tx_last_drop_at is not None:
                                ST.tx_blind_s += time.time() - ST.tx_last_drop_at
                                ST.tx_last_drop_at = None
                            ST.tx_method = method
                            ST.tx_method_history.append(
                                {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                 "method": method, "acked": acked, "of": len(sources)})
                            log.info("РАБОТАЕТ %s: подтверждено %d подписок из %d (хост %s)",
                                     method, acked, len(sources), "atlas" if atlas else "mainnet")
                            write_status()
                            backoff = 1.0
                else:
                    # поток закончился без break -- сервер закрыл соединение
                    raise RuntimeError(f"{method}: сервер закрыл соединение")

                if not confirmed and acked == 0:
                    raise RuntimeError(f"{method}: соединение закрылось до подтверждения подписок")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            ST.tx_reconnects += 1
            ST.tx_last_drop_at = time.time()
            reason = f"{type(exc).__name__}: {str(exc)[:80]}"
            ST.tx_close_reasons[reason] = ST.tx_close_reasons.get(reason, 0) + 1
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
                # ОКНО СШИВКИ. Было -60 000 мс: запись DBot, созданная до
                # минуты РАНЬШЕ нашего детекта, считалась копией этого
                # события. Копия не может возникнуть раньше источника, и
                # именно отсюда взялись отрицательные dbot_offset_slots
                # вплоть до -218 слотов (~61с -- ровно ширина того окна).
                # Оставлен только допуск на расхождение часов.
                if dt_ms < MATCH_CLOCK_SKEW_MS or dt_ms > 600_000:
                    continue
                # Берём ПЕРВУЮ запись после детекта. Прежнее "минимальное
                # dt" при отрицательном окне выбирало самую раннюю, то
                # есть заведомо чужую сделку.
                if best is None or dt_ms < best[0]:
                    best = (dt_ms, rec, task)
        if best is None:
            out["dbot_match"] = "не найдена"
            append_jsonl(ENRICHED_PATH, out)
            n += 1
            continue

        dt_ms, rec, task = best
        our_sig = dbot_signature(rec)
        # ОДНА ПОКУПКА -- ОДНО СОБЫТИЕ. В отчёте за сутки одна и та же
        # наша покупка оказалась приписана трём разным транзакциям
        # источника: каждое событие искало запись независимо и находило
        # ту же самую.
        if our_sig and our_sig in USED_DBOT_SIGS:
            out["dbot_match"] = "дубль_той_же_покупки"
            out["our_signature_дубль"] = our_sig
            out["почему"] = ("эта покупка DBot уже приписана более раннему событию -- "
                              "одна покупка не может быть копией двух разных транзакций")
            append_jsonl(ENRICHED_PATH, out)
            n += 1
            continue
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
                    off = out["our_slot"] - out["source_slot"]
                    if off < 0:
                        # ПРОВЕРКА ПО ЦЕПОЧКЕ. Наша покупка не может сесть
                        # в блок РАНЬШЕ транзакции, которую копирует.
                        # Отрицательное значение -- признак неверной
                        # сшивки, и записывать его как измерение нельзя.
                        out["dbot_match"] = "отклонена_обратный_порядок"
                        out["почему"] = (f"our_slot {out['our_slot']} раньше source_slot "
                                          f"{out['source_slot']} -- копия не может опередить источник")
                        out.pop("our_slot", None)
                        out.pop("our_block_time", None)
                        out["our_signature_отклонена"] = out.pop("our_signature", None)
                        append_jsonl(ENRICHED_PATH, out)
                        n += 1
                        continue
                    out["dbot_offset_slots"] = off
                    USED_DBOT_SIGS.add(our_sig)
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
        "причины_разрыва_tx": dict(sorted(ST.tx_close_reasons.items(), key=lambda kv: -kv[1])[:8]),
        "слепое_время_с": round(ST.tx_blind_s, 1),
        "слепое_время_доля": (round(ST.tx_blind_s / max(time.time() - ST.started_at, 1), 5)
                               if getattr(ST, "started_at", None) else None),
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


def _prov(slot: int | None, err: str | None) -> str:
    """Как показать ответ провайдера: число, или ПОЧЕМУ числа нет."""
    return str(slot) if slot is not None else f"НЕ ОТДАЛ ({err or 'причина не названа'})"


def self_check(helius_key: str, n: int = 3) -> bool:
    """Владелец: «перед отчётом прогони самопроверку: возьми 3 события из
    events.jsonl и вручную сверь slot, our_slot и dbot_offset_slots».

    Сверяем НЕ сами с собой: slot из вебсокета проверяется повторным
    getTransaction у Helius И независимым публичным узлом Solana
    (api.mainnet-beta.solana.com) -- другой провайдер, другой ответ, то
    же самое число или расхождение. Ссылки на Solscan печатаются рядом,
    чтобы владелец мог открыть глазами ту же транзакцию.
    """
    enriched = {e["signature"]: e for e in read_jsonl(ENRICHED_PATH) if e.get("signature")}
    events = read_jsonl(EVENTS_PATH)
    preferred = [e for e in events if enriched.get(e.get("signature"), {}).get("dbot_offset_slots") is not None]
    pool = (preferred or [e for e in events if e.get("signature") in enriched] or events)[-n:]
    if not pool:
        print("САМОПРОВЕРКА: событий нет -- проверять нечего (честный ноль, не сбой)")
        return True

    print(f"=== САМОПРОВЕРКА {len(pool)} СОБЫТИЙ: сверка с цепочкой у ДВУХ провайдеров ===")
    all_ok = True
    for ev in pool:
        sig = ev["signature"]
        enr = enriched.get(sig, {})
        print(f"\n-- событие {ev.get('t_recv_utc')} источник={ev.get('source')} ({ev.get('source_remark')})")
        print(f"   транзакция источника: https://solscan.io/tx/{sig}")
        # НЕ ОТДАЛСЯ != РАСХОЖДЕНИЕ. В отчёте за первые сутки Helius не
        # вернул две транзакции из трёх, и самопроверка объявила это
        # расхождением, хотя записанные числа в точности совпадали с
        # независимым публичным узлом. Сбой провайдера выдавался за
        # ошибку в данных. Теперь молчание провайдера названо молчанием
        # и на вердикт не влияет; вердикт делается по тем, кто ответил.
        h, h_err = get_tx_checked(sig, helius_key)
        pub, p_err = get_tx_checked(sig, helius_key, PUBLIC_RPC)
        h_slot = h.get("slot") if h else None
        p_slot = pub.get("slot") if pub else None
        ws_slot = ev.get("slot")
        answered = [x for x in (h_slot, p_slot) if x is not None]
        ok = bool(answered) and all(x == ws_slot for x in answered)
        all_ok = all_ok and ok
        print(f"   slot из вебсокета={ws_slot} | Helius={_prov(h_slot, h_err)} | "
              f"публичный узел={_prov(p_slot, p_err)} -> "
              f"{'СОВПАДАЕТ' if ok else ('РАСХОЖДЕНИЕ' if answered else 'НИКТО НЕ ОТВЕТИЛ')}")
        our_sig = enr.get("our_signature")
        if not our_sig:
            print("   наша покупка не сшита с этим событием -- our_slot/dbot_offset_slots сверять нечего")
            continue
        print(f"   наша покупка: https://solscan.io/tx/{our_sig}")
        oh, oh_err = get_tx_checked(our_sig, helius_key)
        op, op_err = get_tx_checked(our_sig, helius_key, PUBLIC_RPC)
        oh_slot = oh.get("slot") if oh else None
        op_slot = op.get("slot") if op else None
        rec_our = enr.get("our_slot")
        answered2 = [x for x in (oh_slot, op_slot) if x is not None]
        ok2 = bool(answered2) and all(x == rec_our for x in answered2)
        all_ok = all_ok and ok2
        print(f"   our_slot записан={rec_our} | Helius={_prov(oh_slot, oh_err)} | "
              f"публичный узел={_prov(op_slot, op_err)} -> "
              f"{'СОВПАДАЕТ' if ok2 else ('РАСХОЖДЕНИЕ' if answered2 else 'НИКТО НЕ ОТВЕТИЛ')}")
        rec_off = enr.get("dbot_offset_slots")
        src_slot = enr.get("source_slot")
        # Пересчёт по ЛЮБОМУ ответившему провайдеру, а не только по Helius.
        our_now = oh_slot if oh_slot is not None else op_slot
        src_now = h_slot if h_slot is not None else p_slot
        recomputed = (our_now - src_now) if (isinstance(our_now, int) and isinstance(src_now, int)) else None
        if recomputed is None:
            print(f"   dbot_offset_slots записан={rec_off} | пересчитать не у кого -- "
                  f"оба провайдера промолчали (это НЕ расхождение)")
        else:
            ok3 = rec_off == recomputed
            all_ok = all_ok and ok3
            print(f"   dbot_offset_slots записан={rec_off} | пересчитан ({our_now} - {src_now})"
                  f"={recomputed} -> {'СОВПАДАЕТ' if ok3 else 'РАСХОЖДЕНИЕ'}")
            if isinstance(recomputed, int) and recomputed < 0:
                print("   ВНИМАНИЕ: отрицательное смещение -- копия не может опередить источник, "
                      "это неверная сшивка, а не измерение")
        if src_slot is not None and src_now is not None and src_slot != src_now:
            print(f"   ВНИМАНИЕ: source_slot в записи={src_slot}, сейчас цепочка даёт {src_now}")
    print(f"\nИТОГ САМОПРОВЕРКИ: {'все сверенные числа совпали' if all_ok else 'ЕСТЬ РАСХОЖДЕНИЯ -- смотри выше'}")
    return all_ok


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


def reenrich_all(helius_key: str, dbot_key: str) -> int:
    """Пересчитать ВСЕ обогащённые записи заново, исправленной сшивкой.

    Старый файл сохраняется рядом с суффиксом .before_refix -- если
    пересчёт окажется хуже, есть с чем сравнить и куда вернуться.
    Иначе пришлось бы ждать ещё сутки, чтобы увидеть чистое
    распределение."""
    backup = ENRICHED_PATH.with_suffix(ENRICHED_PATH.suffix + ".before_refix")
    if ENRICHED_PATH.exists():
        ENRICHED_PATH.replace(backup)
        log.info("старый обогащённый файл сохранён как %s", backup.name)
    USED_DBOT_SIGS.clear()
    ST.sources = fetch_sources(dbot_key)
    total = 0
    while True:
        n = enrich_once(helius_key, dbot_key)
        total += n
        log.info("пересчёт: обработано %d (всего %d)", n, total)
        if n == 0:
            break
    return total


def missed_events_report(dbot_key: str, hours: int = 24, window_s: int = 120) -> None:
    """Сколько сделок источников мы ПРОПУСТИЛИ во время разрывов.

    Метод: берём покупки DBot за окно, у каждой известен источник
    (follow.wallet), минт и время создания. Если у нас НЕТ ни одного
    события от этого источника в пределах window_s вокруг -- значит
    транзакцию источника зонд не увидел.

    Оговорка, которую надо держать в голове: DBot копирует не всё
    подряд, а у зонда события есть и по тем сделкам, которые DBot не
    копировал. Поэтому это оценка ТОЛЬКО по тем сделкам, на которые DBot
    среагировал -- нижняя граница пропусков, а не полная картина."""
    events = read_jsonl(EVENTS_PATH)
    by_source: dict[str, list[float]] = {}
    for e in events:
        src = e.get("source")
        t = e.get("t_recv")
        if src and isinstance(t, (int, float)):
            by_source.setdefault(src, []).append(float(t))
    for v in by_source.values():
        v.sort()

    cutoff_ms = (time.time() - hours * 3600) * 1000
    sources = fetch_sources(dbot_key)
    tasks: dict[str, dict] = {}
    for addr, info in sources.items():
        for t in (info.get("tasks") or []):
            if t.get("task_id"):
                tasks[t["task_id"]] = t

    seen = matched = missed = 0
    missed_rows: list[dict] = []
    per_task: dict[str, dict] = {}
    for tid, task in tasks.items():
        for rec in fetch_follow_trades(tid, dbot_key, max_pages=20):
            if str(rec.get("type") or "").lower() != "buy":
                continue
            created = rec.get("createAt")
            if not isinstance(created, (int, float)) or created < cutoff_ms:
                continue
            src = (rec.get("follow") or {}).get("wallet")
            if not src or src not in sources:
                continue
            seen += 1
            name = task.get("task_name") or tid
            row = per_task.setdefault(name, {"всего": 0, "есть_событие": 0, "пропущено": 0})
            row["всего"] += 1
            ts = created / 1000.0
            near = [t for t in by_source.get(src, []) if abs(t - ts) <= window_s]
            if near:
                matched += 1
                row["есть_событие"] += 1
            else:
                missed += 1
                row["пропущено"] += 1
                if len(missed_rows) < 15:
                    missed_rows.append({
                        "источник": src, "задача": name,
                        "когда_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
                        "state": rec.get("state")})

    print()
    print("=== ПРОПУСКИ: покупки DBot без события в зонде ===")
    print(f"окно {hours}ч, допуск по времени +-{window_s}с")
    print(f"покупок DBot от наших источников: {seen}")
    print(f"  событие в зонде ЕСТЬ:  {matched}")
    print(f"  события НЕТ (пропуск): {missed}"
          + (f" = {missed / seen:.1%}" if seen else ""))
    print("по задачам:", json.dumps(per_task, ensure_ascii=False))
    if missed_rows:
        print("примеры пропусков:", json.dumps(missed_rows, ensure_ascii=False, indent=1))
    print("ОГОВОРКА: считаются только сделки, на которые DBot среагировал. "
          "Сделки источников, которые DBot не копировал, здесь не видны, "
          "поэтому это НИЖНЯЯ граница пропусков.")


def offsets_report() -> None:
    """Чистое распределение dbot_offset_slots: 0/1/2/3+ и по задачам.
    Отрицательные и заведомо невозможные значения выделены отдельно, а
    не смешаны с измерением."""
    enriched = read_jsonl(ENRICHED_PATH)
    offs = [(e.get("dbot_offset_slots"), e.get("dbot_task_name"))
            for e in enriched if isinstance(e.get("dbot_offset_slots"), int)]
    rejected = sum(1 for e in enriched if e.get("dbot_match") == "отклонена_обратный_порядок")
    dup = sum(1 for e in enriched if e.get("dbot_match") == "дубль_той_же_покупки")
    print()
    print("=== РАСПРЕДЕЛЕНИЕ dbot_offset_slots (после починки сшивки) ===")
    print(f"сшивок с измерением: {len(offs)}; "
          f"отклонено по обратному порядку: {rejected}; дублей одной покупки: {dup}")
    if not offs:
        print("измерений нет -- честный ноль")
        return

    def buckets(vals: list[int]) -> dict:
        b = {"0": 0, "1": 0, "2": 0, "3+": 0, "отрицательные": 0}
        for v in vals:
            if v < 0:
                b["отрицательные"] += 1
            elif v >= 3:
                b["3+"] += 1
            else:
                b[str(v)] += 1
        return b

    allv = [v for v, _ in offs]
    b = buckets(allv)
    n = len(allv)
    print("всего: " + ", ".join(f"{k}={v} ({v / n:.1%})" for k, v in b.items()))
    print(f"медиана={statistics.median(allv)} | p75={sorted(allv)[int(n * 0.75)]} | макс={max(allv)}")
    per: dict[str, list[int]] = {}
    for v, t in offs:
        per.setdefault(t or "(задача неизвестна)", []).append(v)
    print()
    print(f"  {'задача':<16}{'n':>5}{'медиана':>9}{'0':>6}{'1':>6}{'2':>6}{'3+':>6}")
    for t, vals in sorted(per.items(), key=lambda kv: -len(kv[1])):
        bb = buckets(vals)
        print(f"  {t[:15]:<16}{len(vals):>5}{statistics.median(vals):>9}"
              f"{bb['0']:>6}{bb['1']:>6}{bb['2']:>6}{bb['3+']:>6}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check-only", action="store_true", help="показать источники/слот/часы и выйти")
    ap.add_argument("--report", action="store_true", help="отчёт по накопленным событиям и выйти")
    ap.add_argument("--enrich-now", action="store_true", help="один прогон обогащения и выйти")
    ap.add_argument("--selfcheck", action="store_true",
                    help="сверить 3 события с цепочкой у двух провайдеров и выйти")
    ap.add_argument("--reenrich", action="store_true",
                    help="пересчитать ВСЕ обогащённые записи исправленной сшивкой и выйти")
    ap.add_argument("--offsets", action="store_true",
                    help="чистое распределение dbot_offset_slots (0/1/2/3+) и по задачам")
    ap.add_argument("--missed", action="store_true",
                    help="сколько покупок DBot остались без события в зонде (оценка пропусков)")
    args = ap.parse_args()
    setup_logging()

    if args.report and not (args.selfcheck or args.reenrich or args.offsets or args.missed):
        print_report()
        return

    helius_key, helius_name = env_key("HELIUS_API_KEY", "HELIUS_API")
    dbot_key, _ = env_key("DBOT_API_KEY")
    log.info("ключ Helius взят из переменной %s", helius_name)

    if args.check_only:
        check_only(helius_key, dbot_key)
        return
    if args.selfcheck:
        ok = self_check(helius_key)
        if args.report:
            print()
            print_report()
        sys.exit(0 if ok else 1)
    if args.reenrich:
        print("пересчитано записей:", reenrich_all(helius_key, dbot_key))
        offsets_report()
        return
    if args.offsets:
        offsets_report()
        if args.missed:
            missed_events_report(dbot_key)
        return
    if args.missed:
        missed_events_report(dbot_key)
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
