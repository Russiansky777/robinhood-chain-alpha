#!/usr/bin/env python3
"""Владелец: скан толпы за кандидатами. ТОЛЬКО ЧТЕНИЕ ЦЕПОЧКИ, ни одной
сделки -- в этом файле нет ни одного вызова покупки/продажи и ни одного
POST в DBot (из DBot берётся единственный GET /automation/follow_orders).

ЗАЧЕМ. В data/solana_entry_log.json (Часть A) видно: доля сделок в плюс
растёт вместе с числом покупателей, успевших влезть между лидером и нами.
Проверено на реальном файле перед началом работы:
    buyers_between=0    n=34   в плюс 17.6%   медиана сигнала -2.97%
    buyers_between=1-2  n=44   в плюс 34.1%   медиана сигнала -2.97%
    buyers_between=3-5  n=34   в плюс 38.2%   медиана сигнала -3.27%
    buyers_between=6+   n=135  в плюс 60.0%   медиана сигнала +3.89%
Нужна та же метрика для кошельков, которых мы ещё НЕ копировали, --
посчитанная по цепочке, без участия нашей сделки. Это и есть crowd_2.

ЧТО СЧИТАЕМ. Для каждой покупки кошелька: сколько ЧУЖИХ покупателей того
же минта успели купить ПОСЛЕ него до конца слота +2.
    crowd_0      -- в том же слоте, строго после его транзакции по индексу
    crowd_1      -- весь слот +1
    crowd_2_only -- весь слот +2
    crowd_2      -- уникальные покупатели за всё окно (слот..слот+2)
crowd_2 НЕ равен сумме трёх: один и тот же адрес, купивший в двух слотах,
в сумме посчитался бы дважды, а в crowd_2 он один. Рядом считается
crowd_2_tx -- число транзакций, а не адресов: именно с ним сопоставим
живой buyers_between из Части A (там считались транзакции). Обе величины
идут в вывод, чтобы калибровка сравнивала сравнимое.

МЕТОД ОПРЕДЕЛЕНИЯ ПОКУПКИ -- не свой, а ровно тот же, что в прошлом скане
Fomo: analysis/solana_batch5_rpc_check.classify_tx. Баланс минта у
кошелька ДО транзакции 0, после >0; кошелёк среди подписантов (второй
подписант -- нормально, tx_signers отдаёт всех); в транзакции есть
реальная DEX/AMM-программа (без этой проверки метод засчитывал переводы
и дасты); трата в USDC/USDT пересчитывается в SOL по курсу Gecko на
момент сделки. Копия метода здесь НЕ делается -- импортируется, чтобы
скан и прошлый отбор Fomo не разъехались.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal as D
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_batch5_rpc_check import (  # noqa: E402
    LOOKBACK_S, MAX_TX_PER_WALLET, classify_tx,
)

DBOT_HOST = "https://api-bot-v1.dbotx.com"
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
PILOT = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"

ENTRY_LOG_PATH = REPO_ROOT / "data" / "solana_entry_log.json"
FOMO_PASSED_PATH = REPO_ROOT / "data" / "solana_fomo_passed.json"
OUT_PATH = REPO_ROOT / "data" / "solana_crowd_scan.json"
CACHE_PATH = REPO_ROOT / "data" / "solana_crowd_scan_cache.json"

MIN_SPEND_SOL = 2.0
MAX_BUYS_PER_WALLET = 8
CROWD_SLOTS_AHEAD = 2          # слот, +1, +2
LEVELS = ((8, "сильный"), (3, "средний"), (0, "пустой"))

_ACTIVE_SECRETS: list[str] = []
_PRINT_LOCK = threading.Lock()


def scrub(text: str) -> str:
    for s in _ACTIVE_SECRETS:
        if s:
            text = text.replace(s, "[REDACTED]")
    return text


def log(msg: str) -> None:
    with _PRINT_LOCK:
        print(f"[crowd] {scrub(msg)}", flush=True)


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def helius_key() -> tuple[str, str]:
    for name in ("HELIUS_API_KEY", "HELIUS_API"):
        v = os.environ.get(name, "").strip()
        if v:
            if any(c in v for c in ("\n", "\r")):
                raise RuntimeError(f"{name} содержит перевод строки -- не похоже на сырой ключ")
            _ACTIVE_SECRETS.append(v)
            return v, name
    raise RuntimeError("ни HELIUS_API_KEY, ни HELIUS_API не заданы")


# ---------- свой потокобезопасный RPC ----------
# Почему не fp.rpc_call: у него глобальные _last_call_at/_backoff_s без
# блокировок, а владелец просит 3-4 параллельных воркера -- из потоков
# его троттл рассыпался бы, и заодно пострадали бы почасовые workflow,
# которые тот же модуль используют. Здесь свой слой с локом.

class Rpc:
    """Потокобезопасный RPC. Темп подобран по ЗАМЕРУ, а не на глаз: при
    min_interval=0.02 и удвоении бэкоффа до 20с скан толпы дал 1783
    ретрая на 3912 вызовов (45%) и пропускную способность 0.49 вызова/с
    -- ХУЖЕ, чем однопоточная Часть A (0.94/с). Мы сами загоняли себя в
    спираль бэкоффа. Темп 0.12с, множитель 1.5 и потолок 8с держат
    нагрузку под лимитом провайдера."""
    def __init__(self, key: str, min_interval_s: float = 0.12, workers: int = 4,
                  backoff_mult: float = 1.5, backoff_cap: float = 8.0) -> None:
        self.key = key
        self.url = f"https://mainnet.helius-rpc.com/?api-key={key}"
        self.min_interval_s = min_interval_s
        self.backoff_mult = backoff_mult
        self.backoff_cap = backoff_cap
        self._lock = threading.Lock()
        self._last_at = 0.0
        self._backoff = 0.0
        self._local = threading.local()
        self.calls = 0
        self.retries = 0
        self.errors = 0
        self.splits = 0
        self.deadline: float | None = None
        # Запасной путь. Раньше публичный узел здесь включался только
        # вручную, и когда у Helius кончилась квота, прогоны просто
        # упирались в 429 до конца бюджета. Теперь три отказа подряд
        # временно понижают Helius, и вызовы идут на публичный узел.
        self.allow_public = True
        self.demote_after_429 = 3
        self.demote_for_s = 600.0
        self._429_streak = 0
        self._demoted_until = 0.0
        self.first_429_body: str | None = None
        self.demoted_once = False

    def target_url(self) -> str:
        """Helius, пока он не понижен; иначе публичный узел."""
        if not self.allow_public:
            return self.url
        with self._lock:
            demoted = time.monotonic() < self._demoted_until
        return PUBLIC_RPC if demoted else self.url

    def _note_429(self, target: str, body: str) -> None:
        if target == PUBLIC_RPC:
            return
        with self._lock:
            if self.first_429_body is None:
                # Тело нужно, чтобы отличить нехватку кредитов
                # ("max usage reached") от превышения темпа.
                self.first_429_body = scrub(body)[:200]
            self._429_streak += 1
            hit = self._429_streak >= self.demote_after_429 and self.allow_public
            if hit:
                self._demoted_until = time.monotonic() + self.demote_for_s
                first = not self.demoted_once
                self.demoted_once = True
        if hit and first:
            print(f"[rpc] Helius понижен на {self.demote_for_s:.0f}с "
                  f"({self._429_streak} ответов 429 подряд): {self.first_429_body}", flush=True)

    def _note_ok(self, target: str) -> None:
        if target != PUBLIC_RPC:
            with self._lock:
                self._429_streak = 0

    def session(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            self._local.s = s
        return s

    def expired(self) -> bool:
        return self.deadline is not None and time.monotonic() > self.deadline

    def _pace(self) -> None:
        with self._lock:
            wait = max(self.min_interval_s - (time.monotonic() - self._last_at), 0.0) + self._backoff
            self._last_at = time.monotonic() + wait
            self.calls += 1
        if wait > 0:
            time.sleep(wait)

    def _slow_down(self) -> None:
        with self._lock:
            self._backoff = min(max(self._backoff * self.backoff_mult, 0.25), self.backoff_cap)
            self.retries += 1

    def _speed_up(self) -> None:
        with self._lock:
            self._backoff = self._backoff * 0.35 if self._backoff > 0.05 else 0.0

    def call(self, method: str, params: list, url: str | None = None, attempts: int = 8):
        """Возвращает result. Бросает RuntimeError на неустранимой ошибке."""
        pinned = url  # явно заданный адрес (сверка у второго провайдера) не подменяем
        last = None
        for _ in range(attempts):
            if self.expired():
                raise RuntimeError(f"{method}: бюджет времени прогона истёк")
            target = pinned or self.target_url()
            self._pace()
            try:
                resp = self.session().post(
                    target, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=45)
            except Exception as exc:  # noqa: BLE001
                last = f"{type(exc).__name__}: {exc}"
                self._slow_down()
                continue
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                last = f"HTTP {resp.status_code}"
                if resp.status_code == 429:
                    self._note_429(target, resp.text)
                    # Helius только что понижен -- следующая попытка уйдёт
                    # на публичный узел, отсиживать паузу незачем.
                    if target != PUBLIC_RPC and not pinned and self.target_url() == PUBLIC_RPC:
                        continue
                self._slow_down()
                continue
            if not resp.ok:
                raise RuntimeError(f"{method}: HTTP {resp.status_code}: {scrub(resp.text[:200])}")
            body = resp.json()
            if "error" in body:
                err = body["error"]
                msg = str(err.get("message", "")).lower()
                if "rate" in msg or "too many" in msg:
                    last = str(err)[:120]
                    self._slow_down()
                    continue
                raise RuntimeError(f"{method}: RPC error {err}")
            self._speed_up()
            self._note_ok(target)
            return body.get("result")
        self.errors += 1
        raise RuntimeError(f"{method}: исчерпаны попытки, последняя причина: {last}")

    def signatures(self, address: str, before: str | None, limit: int = 1000) -> list[dict]:
        opts: dict = {"limit": limit}
        if before:
            opts["before"] = before
        return self.call("getSignaturesForAddress", [address, opts]) or []

    def transactions(self, sigs: list[str], _depth: int = 0) -> dict[str, dict | None]:
        """Пачка getTransaction одним POST (как в скане 217).

        Найдено на реальном прогоне калибровки: у самых активных
        кошельков (пилот, MaxHuh) пачка из 20 стабильно исчерпывала
        попытки -- ответ слишком большой -- и кошелёк падал целиком со
        статусом "ошибка", то есть терялся из калибровки. Теперь при
        неудаче пачка ДРОБИТСЯ пополам вплоть до одиночных запросов, и
        сдаётся только та подпись, которая действительно не отдаётся."""
        if not sigs:
            return {}
        payload = [{"jsonrpc": "2.0", "id": i, "method": "getTransaction",
                    "params": [s, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}]}
                   for i, s in enumerate(sigs)]
        for _ in range(5):
            if self.expired():
                raise RuntimeError("getTransaction batch: бюджет времени истёк")
            self._pace()
            try:
                resp = self.session().post(self.url, json=payload, timeout=120)
            except Exception:  # noqa: BLE001
                self._slow_down()
                continue
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                self._slow_down()
                continue
            if not resp.ok:
                self._slow_down()
                continue
            try:
                body = resp.json()
            except ValueError:
                self._slow_down()
                continue
            if not isinstance(body, list):
                self._slow_down()
                continue
            self._speed_up()
            out: dict[str, dict | None] = {}
            for item in body:
                i = item.get("id")
                if isinstance(i, int) and 0 <= i < len(sigs):
                    out[sigs[i]] = item.get("result")
            for s in sigs:
                out.setdefault(s, None)
            return out
        if len(sigs) == 1:
            self.errors += 1
            return {sigs[0]: None}      # честно: эта подпись не отдалась, остальные не страдают
        mid = len(sigs) // 2
        with self._lock:
            self.splits += 1
        left = self.transactions(sigs[:mid], _depth + 1)
        right = self.transactions(sigs[mid:], _depth + 1)
        return {**left, **right}


# ---------- блоки: общий кэш на все кошельки ----------

class BlockCache:
    """Слот -> {минт: [(индекс_транзакции, владелец), ...]} по возрастанию
    индекса. Храним ДИСТИЛЛЯТ, а не сырой блок: сырой блок это мегабайты,
    а нужен только список покупателей по каждому минту. Один слот обслужит
    любой минт -- ради этого владелец и просил общий кэш блоков."""

    def __init__(self, rpc: Rpc, capacity: int = 400) -> None:
        self.rpc = rpc
        self.capacity = capacity
        self._data: OrderedDict[int, dict | None] = OrderedDict()
        self._lock = threading.Lock()
        self._slot_locks: dict[int, threading.Lock] = {}
        self.hits = 0
        self.fetched = 0
        self.skipped = 0
        self.failed = 0

    def _slot_lock(self, slot: int) -> threading.Lock:
        with self._lock:
            lk = self._slot_locks.get(slot)
            if lk is None:
                lk = self._slot_locks[slot] = threading.Lock()
            return lk

    def get(self, slot: int) -> dict | None:
        with self._lock:
            if slot in self._data:
                self._data.move_to_end(slot)
                self.hits += 1
                return self._data[slot]
        with self._slot_lock(slot):
            with self._lock:
                if slot in self._data:
                    self._data.move_to_end(slot)
                    self.hits += 1
                    return self._data[slot]
            distilled = self._fetch(slot)
            with self._lock:
                self._data[slot] = distilled
                self._data.move_to_end(slot)
                while len(self._data) > self.capacity:
                    old, _ = self._data.popitem(last=False)
                    self._slot_locks.pop(old, None)
            return distilled

    def _fetch(self, slot: int) -> dict | None:
        try:
            blk = self.rpc.call("getBlock", [slot, {
                "encoding": "jsonParsed",
                "transactionDetails": "accounts",
                # Потолок 1, а не 0: на прогоне Части A потолок 0 давал
                # -32015 "Transaction version (1) is not supported" на
                # ВСЕХ блоках -- в блоках есть транзакции версии 1.
                "maxSupportedTransactionVersion": 1,
                "rewards": False,
            }])
        except RuntimeError as exc:
            msg = str(exc)
            if any(c in msg for c in ("-32004", "-32007", "-32009")) or "skipped" in msg or "not available" in msg:
                self.skipped += 1          # слот пропущен лидером -- честный пустой, не сбой
                return {}
            self.failed += 1
            log(f"getBlock {slot} не отдался: {msg[:160]}")
            return None
        if not blk:
            self.skipped += 1
            return {}
        self.fetched += 1
        return distill_block(blk)


def distill_block(blk: dict) -> dict:
    """Из блока -- только покупатели: {минт: [(индекс, владелец), ...]}."""
    out: dict[str, list[tuple[int, str]]] = {}
    sig_index: dict[str, int] = {}
    for idx, t in enumerate(blk.get("transactions") or []):
        meta = t.get("meta") or {}
        sigs = (t.get("transaction") or {}).get("signatures") or []
        if sigs:
            sig_index.setdefault(sigs[0], idx)
        if meta.get("err") is not None:
            continue
        pre: dict[tuple, float] = {}
        post: dict[tuple, float] = {}
        for b in meta.get("preTokenBalances") or []:
            pre[(b.get("mint"), b.get("owner"), b.get("accountIndex"))] = \
                float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        for b in meta.get("postTokenBalances") or []:
            post[(b.get("mint"), b.get("owner"), b.get("accountIndex"))] = \
                float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        gained: set[tuple[str, str]] = set()
        for k in set(pre) | set(post):
            if post.get(k, 0.0) - pre.get(k, 0.0) > 0:
                mint, owner = k[0], k[1]
                if mint and owner:
                    gained.add((mint, owner))
        for mint, owner in gained:
            out.setdefault(mint, []).append((idx, owner))
    for v in out.values():
        v.sort()
    out["__sig_index__"] = sig_index  # type: ignore[assignment]
    return out


def crowd_for_buy(cache: BlockCache, slot: int, mint: str, wallet: str,
                   signature: str) -> dict:
    """Сколько чужих успело купить тот же минт после нашей транзакции
    до конца слота +2."""
    per_slot: list[dict] = []
    seen_owners: set[str] = set()
    total_tx = 0
    own_index = None
    incomplete = False

    for step in range(CROWD_SLOTS_AHEAD + 1):
        s = slot + step
        d = cache.get(s)
        if d is None:
            incomplete = True
            per_slot.append({"slot": s, "buyers": None})
            continue
        buyers = list(d.get(mint) or [])
        if step == 0:
            own_index = (d.get("__sig_index__") or {}).get(signature)
            if own_index is None:
                return {"error": "своей транзакции нет в блоке её же слота -- не считаю толпу"}
            buyers = [(i, o) for i, o in buyers if i > own_index]
        buyers = [(i, o) for i, o in buyers if o != wallet]
        owners = {o for _, o in buyers}
        seen_owners |= owners
        total_tx += len(buyers)
        per_slot.append({"slot": s, "buyers": len(owners), "tx": len(buyers)})

    return {
        "own_index_in_block": own_index,
        "crowd_0": per_slot[0].get("buyers"),
        "crowd_1": per_slot[1].get("buyers"),
        "crowd_2_only": per_slot[2].get("buyers"),
        "crowd_2": len(seen_owners),
        "crowd_2_tx": total_tx,
        "incomplete": incomplete,
    }


# ---------- покупки кошелька ----------

# Окно скана по умолчанию 72ч (LOOKBACK_S из solana_batch5_rpc_check), но
# владелец просит добивать дыру по кандидатам семидневным окном. Значение
# переопределяется ключом --lookback-hours и хранится здесь, чтобы обе
# функции (страницы подписей и оговорки в выгрузке) видели одно и то же.
ACTIVE_LOOKBACK_S = LOOKBACK_S


def wallet_signatures_72h(rpc: Rpc, address: str) -> tuple[list[dict], bool]:
    """Подписи кошелька за ACTIVE_LOOKBACK_S. Имя оставлено прежним,
    чтобы не разъехаться с вызывающим кодом; окно берётся из переменной."""
    cutoff = int(time.time()) - ACTIVE_LOOKBACK_S
    hist: list[dict] = []
    before = None
    complete = False
    # Страниц пропорционально окну: 12 страниц по 1000 подписей хватало на
    # 72ч, на 7 днях активному кошельку могло не хватить -- и тогда
    # "покупок нет" означало бы "не долистали", а это разные вещи.
    max_pages = max(12, int(12 * ACTIVE_LOOKBACK_S / LOOKBACK_S))
    for _ in range(max_pages):
        page = rpc.signatures(address, before)
        if not page:
            complete = True
            break
        hist.extend(page)
        oldest = page[-1].get("blockTime")
        before = page[-1]["signature"]
        if oldest is not None and oldest <= cutoff:
            complete = True
            break
        if len(page) < 1000:
            complete = True
            break
    return [h for h in hist if h.get("blockTime") is not None and h["blockTime"] >= cutoff], complete


_GECKO_LOCK = threading.Lock()
_gecko_orig = fp.find_price_at_gecko


def _gecko_locked(t, lo, hi):
    # classify_tx зовёт fp.find_price_at_gecko для пересчёта USDC->SOL.
    # fp кэширует свечи в глобальных структурах без блокировок, а мы
    # работаем из 4 потоков -- сериализуем именно этот вызов.
    with _GECKO_LOCK:
        return _gecko_orig(t, lo, hi)


fp.find_price_at_gecko = _gecko_locked


def wallet_buys(rpc: Rpc, address: str) -> dict:
    """Последние до 8 первых покупок >=2 SOL-экв за 72ч.

    Идём от самых свежих подписей пачками по 20 и ОСТАНАВЛИВАЕМСЯ, как
    только набрали 8: у активного кошелька иначе пришлось бы тянуть все
    300 транзакций ради восьми нужных."""
    sigs, complete = wallet_signatures_72h(rpc, address)
    ok = [s["signature"] for s in sigs if s.get("err") is None][:MAX_TX_PER_WALLET]
    buys: list[dict] = []
    n_tx_seen = n_fetch_failed = n_multi_mint = n_chunk_failed = 0
    for i in range(0, len(ok), 20):
        if len(buys) >= MAX_BUYS_PER_WALLET or rpc.expired():
            break
        chunk = ok[i:i + 20]
        try:
            txs = rpc.transactions(chunk)
        except RuntimeError as exc:
            # Бюджет времени истёк посреди кошелька -- отдаём, что успели,
            # и честно помечаем, а не теряем кошелёк целиком.
            n_chunk_failed += 1
            log(f"{address[:10]}..: пачка транзакций не отдалась ({str(exc)[:80]}) -- дальше с тем, что есть")
            break
        for sig in chunk:
            tx = txs.get(sig)
            n_tx_seen += 1
            if tx is None:
                n_fetch_failed += 1
                continue
            ev = classify_tx(tx, address)
            if not ev:
                continue
            if ev.get("kind") == "multi_mint_skipped":
                n_multi_mint += 1
                continue
            if ev.get("kind") != "first_entry":
                continue
            if ev["spend_sol_equiv"] < MIN_SPEND_SOL:
                continue
            buys.append({"signature": ev["signature"], "slot": ev["slot"], "mint": ev["mint"],
                          "spend_sol_equiv": round(ev["spend_sol_equiv"], 6),
                          "block_time": tx.get("blockTime"),
                          "stable_usd_spent": ev.get("stable_usd_spent"),
                          "price_missing": ev.get("price_missing")})
            if len(buys) >= MAX_BUYS_PER_WALLET:
                break
    return {"buys": buys, "n_sigs_72h": len(sigs), "n_tx_examined": n_tx_seen,
            "n_tx_fetch_failed": n_fetch_failed, "n_multi_mint_skipped": n_multi_mint,
            "n_chunk_failed": n_chunk_failed, "signatures_complete": complete}


def scan_one(rpc: Rpc, cache: BlockCache, address: str, meta: dict) -> dict:
    row = {"address": address, "name": meta.get("name"), "status": meta.get("status")}
    try:
        found = wallet_buys(rpc, address)
    except RuntimeError as exc:
        row.update({"status_scan": "ошибка", "note": scrub(str(exc))[:200]})
        return row
    row.update({k: v for k, v in found.items() if k != "buys"})
    buys = found["buys"]
    if not buys:
        row.update({"status_scan": "no_buys", "n_buys": 0,
                     "note": f"нет первых покупок >={MIN_SPEND_SOL} SOL-экв за {ACTIVE_LOOKBACK_S // 3600}ч "
                             f"(просмотрено транзакций: {found['n_tx_examined']})"})
        return row

    rows = []
    for b in buys:
        if rpc.expired():
            break
        c = crowd_for_buy(cache, b["slot"], b["mint"], address, b["signature"])
        rows.append({**b, **c})
    usable = [r for r in rows if r.get("crowd_2") is not None]
    row["buys"] = rows
    row["n_buys"] = len(usable)
    if not usable:
        row.update({"status_scan": "без толпы", "note": "ни по одной покупке не удалось собрать блоки"})
        return row
    c2 = sorted(r["crowd_2"] for r in usable)
    c2tx = sorted(r["crowd_2_tx"] for r in usable)
    spends = sorted(r["spend_sol_equiv"] for r in usable)
    row.update({
        "status_scan": "ok",
        "crowd_2_median": statistics.median(c2),
        "crowd_2_p75": c2[int(len(c2) * 0.75)] if len(c2) > 1 else c2[0],
        "crowd_2_tx_median": statistics.median(c2tx),
        "crowd_0_median": statistics.median([r["crowd_0"] for r in usable if r.get("crowd_0") is not None] or [0]),
        "crowd_1_median": statistics.median([r["crowd_1"] for r in usable if r.get("crowd_1") is not None] or [0]),
        "crowd_2_only_median": statistics.median(
            [r["crowd_2_only"] for r in usable if r.get("crowd_2_only") is not None] or [0]),
        "zero_share": round(sum(1 for x in c2 if x == 0) / len(c2), 4),
        "spend_median_sol": round(statistics.median(spends), 4),
        "n_incomplete": sum(1 for r in usable if r.get("incomplete")),
    })
    row["level"] = level_of(row["crowd_2_median"])
    return row


def level_of(median_c2: float) -> str:
    for threshold, name in LEVELS:
        if median_c2 >= threshold:
            return name
    return "пустой"


# ---------- список кошельков ----------

def dbot_sources(api_key: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        resp = requests.get(f"{DBOT_HOST}/automation/follow_orders",
                             headers={"X-API-KEY": api_key}, timeout=30)
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"follow_orders недоступен: {type(exc).__name__}: {exc}") from exc
    items = body if isinstance(body, list) else next(
        (body.get(k) for k in ("res", "data", "results", "list", "items") if isinstance(body.get(k), list)), None)
    if items is None:
        raise RuntimeError("follow_orders: форма ответа не распознана -- не выдумываю список источников")
    for r in items:
        if not r.get("enabled", True):
            continue
        names = r.get("targetNames") or []
        for i, addr in enumerate(r.get("targetIds") or []):
            node = out.setdefault(addr, {"name": None, "status": "в задаче"})
            if not node["name"] and i < len(names) and names[i]:
                node["name"] = names[i]
    return out


def fomo_candidates() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not FOMO_PASSED_PATH.exists():
        return out
    for line in FOMO_PASSED_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        addr = r.get("address")
        if addr:
            out[addr] = {"name": r.get("name"), "status": "кандидат"}
    return out


def live_buyers_between() -> dict[str, list[int]]:
    if not ENTRY_LOG_PATH.exists():
        return {}
    d = json.loads(ENTRY_LOG_PATH.read_text())
    by: dict[str, list[int]] = {}
    for r in d.get("trades") or []:
        if r.get("buyers_between") is not None and r.get("source_address"):
            by.setdefault(r["source_address"], []).append(r["buyers_between"])
    return by


def build_wallets(api_key: str | None) -> dict[str, dict]:
    wallets: dict[str, dict] = {}
    src = dbot_sources(api_key) if api_key else {}
    wallets.update(src)
    for a, m in fomo_candidates().items():
        if a in wallets:
            wallets[a]["name"] = wallets[a].get("name") or m.get("name")
        else:
            wallets[a] = m
    node = wallets.setdefault(PILOT, {"name": None, "status": "в задаче"})
    node["name"] = node.get("name") or "ПИЛОТ"
    node["is_pilot"] = True
    log(f"кошельков: из follow_orders {len(src)}, из fomo_passed {len(fomo_candidates())}, "
        f"итого уникальных {len(wallets)}")
    return wallets


# ---------- калибровка ----------

def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    try:
        return round(statistics.correlation(xs, ys), 4)
    except Exception:  # noqa: BLE001
        return None


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None

    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    return pearson(ranks(xs), ranks(ys))


def calibration_report(rows: list[dict], live: dict[str, list[int]]) -> dict:
    pilot_row = next((r for r in rows if r["address"] == PILOT), None)
    pilot_live = live.get(PILOT) or []
    pairs = []
    for r in rows:
        lv = live.get(r["address"]) or []
        if len(lv) >= 4 and r.get("crowd_2_median") is not None:
            pairs.append({
                "address": r["address"], "name": r.get("name"),
                "n_live": len(lv), "live_buyers_between_median": statistics.median(lv),
                "n_scan_buys": r["n_buys"],
                "scan_crowd_2_median": r["crowd_2_median"],
                "scan_crowd_2_tx_median": r["crowd_2_tx_median"],
            })
    pairs.sort(key=lambda p: -p["live_buyers_between_median"])
    xs = [p["scan_crowd_2_median"] for p in pairs]
    xs_tx = [p["scan_crowd_2_tx_median"] for p in pairs]
    ys = [p["live_buyers_between_median"] for p in pairs]
    out = {
        "pilot": {
            "address": PILOT,
            "live_buyers_between_median": statistics.median(pilot_live) if pilot_live else None,
            "live_n": len(pilot_live),
            "scan_crowd_2_median": (pilot_row or {}).get("crowd_2_median"),
            "scan_crowd_2_tx_median": (pilot_row or {}).get("crowd_2_tx_median"),
            "scan_n_buys": (pilot_row or {}).get("n_buys"),
            "scan_status": (pilot_row or {}).get("status_scan"),
        },
        "n_pairs": len(pairs),
        "pairs": pairs,
        "pearson_crowd2_vs_live": pearson(xs, ys),
        "spearman_crowd2_vs_live": spearman(xs, ys),
        "pearson_crowd2tx_vs_live": pearson(xs_tx, ys),
        "spearman_crowd2tx_vs_live": spearman(xs_tx, ys),
    }
    p = out["pilot"]
    same_order = None
    if p["live_buyers_between_median"] and p["scan_crowd_2_tx_median"] is not None:
        a, b = float(p["live_buyers_between_median"]), float(p["scan_crowd_2_tx_median"])
        same_order = (max(a, b) / max(min(a, b), 0.5)) <= 3.0
    out["pilot_same_order"] = same_order
    best_rho = max([v for v in (out["spearman_crowd2_vs_live"], out["spearman_crowd2tx_vs_live"])
                    if v is not None] or [0])
    out["best_spearman"] = best_rho
    out["verdict_ok"] = bool(same_order) and best_rho >= 0.3 and len(pairs) >= 5
    return out


# ---------- самопроверка ----------

def self_check(rpc: Rpc, rows: list[dict], n: int = 3) -> dict:
    """Владелец: 3 случайные покупки сверить у ДВУХ провайдеров. Сверка
    сама с собой ничего не доказывает, поэтому слот и число покупателей
    пересчитываются заново по ответу Helius И публичного узла."""
    pool = [(r, b) for r in rows for b in (r.get("buys") or [])
            if b.get("crowd_2") is not None]
    if not pool:
        return {"ok": None, "note": "нет покупок с посчитанной толпой -- сверять нечего"}
    random.seed(20260922)
    sample = random.sample(pool, min(n, len(pool)))
    checks = []
    all_ok = True
    for r, b in sample:
        item = {"address": r["address"], "name": r.get("name"), "signature": b["signature"],
                 "mint": b["mint"], "solscan": f"https://solscan.io/tx/{b['signature']}",
                 "slot_записан": b["slot"], "crowd_2_записан": b["crowd_2"],
                 "crowd_2_tx_записан": b["crowd_2_tx"]}
        for label, url in (("helius", None), ("публичный_узел", PUBLIC_RPC)):
            try:
                tx = rpc.call("getTransaction",
                              [b["signature"], {"encoding": "jsonParsed",
                                                 "maxSupportedTransactionVersion": 1}], url=url)
                item[f"slot_{label}"] = (tx or {}).get("slot")
            except RuntimeError as exc:
                item[f"slot_{label}"] = None
                item[f"slot_{label}_ошибка"] = scrub(str(exc))[:120]
        # пересчёт толпы заново, по свежим блокам у обоих провайдеров
        for label, url in (("helius", None), ("публичный_узел", PUBLIC_RPC)):
            try:
                item[f"crowd_2_tx_{label}"] = recount_crowd_tx(
                    rpc, b["slot"], b["mint"], r["address"], b["signature"], url)
            except RuntimeError as exc:
                item[f"crowd_2_tx_{label}"] = None
                item[f"crowd_{label}_ошибка"] = scrub(str(exc))[:120]
        slots = [item.get("slot_helius"), item.get("slot_публичный_узел")]
        slot_ok = all(s == b["slot"] for s in slots if s is not None) and any(s is not None for s in slots)
        counts = [item.get("crowd_2_tx_helius"), item.get("crowd_2_tx_публичный_узел")]
        count_ok = all(c == b["crowd_2_tx"] for c in counts if c is not None) and any(c is not None for c in counts)
        item["слот_совпал"] = slot_ok
        item["толпа_совпала"] = count_ok
        all_ok = all_ok and slot_ok and count_ok
        checks.append(item)
    return {"ok": all_ok, "checks": checks}


def recount_crowd_tx(rpc: Rpc, slot: int, mint: str, wallet: str, signature: str,
                      url: str | None) -> int:
    total = 0
    own_index = None
    for step in range(CROWD_SLOTS_AHEAD + 1):
        blk = rpc.call("getBlock", [slot + step, {
            "encoding": "jsonParsed", "transactionDetails": "accounts",
            "maxSupportedTransactionVersion": 1, "rewards": False}], url=url)
        if not blk:
            continue
        d = distill_block(blk)
        buyers = list(d.get(mint) or [])
        if step == 0:
            own_index = (d.get("__sig_index__") or {}).get(signature)
            if own_index is None:
                raise RuntimeError("своей транзакции нет в блоке")
            buyers = [(i, o) for i, o in buyers if i > own_index]
        total += sum(1 for _, o in buyers if o != wallet)
    return total


# ---------- main ----------

class WalletCache:
    """Результат по кошельку живёт в файле, чтобы повторный прогон и
    продолжение после исчерпанного бюджета не пересчитывали то, что уже
    посчитано. Возраст записи пишется в вывод -- ничего не выдаётся за
    свежее молча."""

    def __init__(self, path: Path, max_age_s: int) -> None:
        self.path = path
        self.max_age_s = max_age_s
        self._lock = threading.Lock()
        self._data: dict = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text())
            except (ValueError, OSError):
                self._data = {}
        self.hits = 0

    def get(self, address: str) -> dict | None:
        node = self._data.get(address)
        if not node:
            return None
        age = time.time() - float(node.get("cached_at") or 0)
        if age > self.max_age_s:
            return None
        row = dict(node["row"])
        row["из_кэша_секунд_назад"] = int(age)
        with self._lock:
            self.hits += 1
        return row

    def put(self, address: str, row: dict) -> None:
        if row.get("status_scan") not in ("ok", "no_buys"):
            return                      # ошибки не кэшируем -- пусть перепробует
        with self._lock:
            self._data[address] = {"cached_at": time.time(), "row": row}

    def save(self) -> None:
        with self._lock:
            self.path.write_text(json.dumps(self._data, ensure_ascii=False, default=str))


def run_scan(rpc: Rpc, cache: BlockCache, wallets: dict[str, dict], workers: int,
              label: str, wcache: "WalletCache | None" = None) -> list[dict]:
    rows: list[dict] = []
    done = 0
    todo = dict(wallets)
    if wcache is not None:
        for a in list(todo):
            hit = wcache.get(a)
            if hit is not None:
                rows.append(hit)
                del todo[a]
        if rows:
            log(f"{label}: из кэша взято {len(rows)} кошельков, считать надо {len(todo)}")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(scan_one, rpc, cache, a, m): a for a, m in todo.items()}
        for fut in as_completed(futs):
            addr = futs[fut]
            try:
                row = fut.result()
            except Exception as exc:  # noqa: BLE001
                row = {"address": addr, "name": wallets[addr].get("name"),
                        "status": wallets[addr].get("status"),
                        "status_scan": "ошибка", "note": scrub(f"{type(exc).__name__}: {exc}")[:200]}
            rows.append(row)
            if wcache is not None:
                wcache.put(addr, row)
                if done % 10 == 0:
                    wcache.save()
            done += 1
            if done % 5 == 0 or done == len(todo):
                log(f"{label}: {done}/{len(todo)} | блоков из сети={cache.fetched} "
                    f"из кэша={cache.hits} пропущено={cache.skipped} | RPC={rpc.calls} ретраев={rpc.retries}")
    rows.sort(key=lambda r: (-(r.get("crowd_2_median") if r.get("crowd_2_median") is not None else -1),
                              r["address"]))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=("calibrate", "full"), default="calibrate")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--time-budget-s", type=int, default=75 * 60)
    ap.add_argument("--limit", type=int, default=0, help="только N кошельков (отладка)")
    ap.add_argument("--cache-max-age-s", type=int, default=6 * 3600,
                    help="возраст записи кэша кошелька, после которого он пересчитывается")
    ap.add_argument("--no-cache", action="store_true", help="считать всё заново")
    ap.add_argument("--lookback-hours", type=int, default=LOOKBACK_S // 3600,
                    help="окно поиска покупок в часах (по умолчанию 72)")
    ap.add_argument("--only", default="",
                    help="через запятую: сканировать ТОЛЬКО эти адреса (добор дыры по кандидатам)")
    ap.add_argument("--out", default="",
                    help="путь выгрузки; пусто -- data/solana_crowd_scan.json. Для добора "
                         "обязательно задать свой файл, иначе основной скан будет затёрт")
    args = ap.parse_args()

    global ACTIVE_LOOKBACK_S, OUT_PATH
    ACTIVE_LOOKBACK_S = args.lookback_hours * 3600
    if args.out:
        OUT_PATH = Path(args.out) if Path(args.out).is_absolute() else REPO_ROOT / args.out

    started = time.monotonic()
    key, key_name = helius_key()
    os.environ.setdefault("HELIUS_API", key)   # fp.find_price_at_gecko/каскад читают это имя
    rpc = Rpc(key, workers=args.workers)
    rpc.deadline = started + args.time_budget_s
    fp.set_soft_deadline(rpc.deadline)
    cache = BlockCache(rpc)
    log(f"ключ Helius из {key_name}; режим={args.mode}; воркеров={args.workers}; "
        f"бюджет={args.time_budget_s}с")

    dbot_key = os.environ.get("DBOT_API_KEY", "").strip()
    if dbot_key:
        _ACTIVE_SECRETS.append(dbot_key)
    wallets = build_wallets(dbot_key or None)
    live = live_buyers_between()

    if args.mode == "calibrate":
        target = {PILOT: wallets.get(PILOT, {"name": "ПИЛОТ", "status": "в задаче"})}
        for a, m in wallets.items():
            if len(live.get(a) or []) >= 4:
                target[a] = m
        log(f"КАЛИБРОВКА: пилот + источники с >=4 живыми сделками = {len(target)} кошельков")
    else:
        target = dict(wallets)
    only = {a.strip() for a in args.only.split(",") if a.strip()}
    if only:
        missing = only - set(target.keys())
        target = {a: m for a, m in target.items() if a in only}
        log(f"ДОБОР: сканирую только {len(target)} адресов из списка")
        if missing:
            # Честно: адрес из списка, которого нет в наборе кошельков, --
            # это не "просканирован и пусто", а "не сканировался вовсе".
            log(f"ВНИМАНИЕ: {len(missing)} адресов из --only нет в наборе кошельков: "
                f"{', '.join(sorted(missing))}")
    if args.limit:
        target = dict(list(target.items())[:args.limit])

    wcache = None if args.no_cache else WalletCache(CACHE_PATH, args.cache_max_age_s)
    rows = run_scan(rpc, cache, target, args.workers, args.mode, wcache)
    if wcache is not None:
        wcache.save()
    calib = calibration_report(rows, live)
    check = self_check(rpc, rows)

    out = {
        "generated_at_utc": now_utc(),
        "mode": args.mode,
        "ЧЕСТНЫЕ_ОГОВОРКИ": [
            "Только чтение цепочки: ни одного вызова покупки/продажи, из DBot только GET follow_orders.",
            "crowd_2 -- УНИКАЛЬНЫЕ чужие адреса за окно слот..слот+2; crowd_2_tx -- число транзакций. "
            "Живой buyers_between из Части A считал транзакции, поэтому сравнивать с ним честно именно "
            "crowd_2_tx, и калибровка смотрит на обе величины.",
            "Окно скана -- фиксированные 3 слота вперёд. Живой buyers_between измерялся до НАШЕЙ покупки, "
            "а её медианное отставание 2 слота -- окна сопоставимы, но не тождественны.",
            "Покупка определяется тем же кодом, что прошлый отбор Fomo (classify_tx импортируется, не копируется): "
            "баланс минта до транзакции 0, кошелёк среди подписантов, в транзакции есть DEX-программа, "
            "USDC/USDT пересчитаны в SOL по курсу Gecko на момент сделки.",
            "Кошельки без первых покупок >=2 SOL за 72ч помечены no_buys и не попадают в таблицу уровней.",
        ],
        "config": {
            "min_spend_sol": MIN_SPEND_SOL, "max_buys_per_wallet": MAX_BUYS_PER_WALLET,
            "lookback_hours": ACTIVE_LOOKBACK_S // 3600, "slots_ahead": CROWD_SLOTS_AHEAD,
            "только_адреса": sorted(only) or None,
            "workers": args.workers, "helius_key_env_name": key_name,
            "levels": {"сильный": ">=8", "средний": "3-7", "пустой": "<=2"},
        },
        "run_stats": {
            "wallets_scanned": len(rows),
            "blocks_fetched": cache.fetched, "blocks_from_cache": cache.hits,
            "blocks_skipped": cache.skipped, "blocks_failed": cache.failed,
            "rpc_calls": rpc.calls, "rpc_retries": rpc.retries, "rpc_errors": rpc.errors,
            "batch_splits": rpc.splits,
            "wallets_from_cache": (wcache.hits if wcache else 0),
            "elapsed_s": round(time.monotonic() - started, 1),
            "budget_exhausted": rpc.expired(),
        },
        "calibration": calib,
        "self_check": check,
        "wallets": rows,
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print_report(out)


def print_report(out: dict) -> None:
    rows = out["wallets"]
    c = out["calibration"]
    print()
    print("=" * 96)
    print(f"СКАН ТОЛПЫ, режим={out['mode']}, {out['generated_at_utc']}")
    print(f"статистика прогона: {json.dumps(out['run_stats'], ensure_ascii=False)}")
    print()
    p = c["pilot"]
    print("--- КАЛИБРОВКА ПО ПИЛОТУ ---")
    print(f"  живой buyers_between медиана = {p['live_buyers_between_median']} (сделок {p['live_n']})")
    print(f"  скан crowd_2_tx медиана      = {p['scan_crowd_2_tx_median']} (покупок {p['scan_n_buys']}, "
          f"статус {p['scan_status']})")
    print(f"  скан crowd_2 (адреса) медиана= {p['scan_crowd_2_median']}")
    print(f"  один порядок: {p and c['pilot_same_order']}")
    print()
    print(f"--- КАЛИБРОВКА ПО ИСТОЧНИКАМ (>=4 живые сделки): пар {c['n_pairs']} ---")
    if c["pairs"]:
        print(f"  {'адрес':<16}{'имя':<16}{'живых':>6}{'живой med':>11}{'скан n':>8}"
              f"{'crowd_2':>9}{'crowd_2_tx':>12}")
        for q in c["pairs"]:
            print(f"  {q['address'][:14]+'…':<16}{str(q['name'] or '')[:14]:<16}{q['n_live']:>6}"
                  f"{q['live_buyers_between_median']:>11}{q['n_scan_buys']:>8}"
                  f"{q['scan_crowd_2_median']:>9}{q['scan_crowd_2_tx_median']:>12}")
    print(f"  Пирсон  crowd_2 vs живой = {c['pearson_crowd2_vs_live']}   "
          f"crowd_2_tx vs живой = {c['pearson_crowd2tx_vs_live']}")
    print(f"  Спирмен crowd_2 vs живой = {c['spearman_crowd2_vs_live']}   "
          f"crowd_2_tx vs живой = {c['spearman_crowd2tx_vs_live']}")
    print(f"  ВЕРДИКТ КАЛИБРОВКИ: {'СВЯЗЬ ЕСТЬ' if c['verdict_ok'] else 'СВЯЗИ НЕТ'} "
          f"(порог: пилот того же порядка И лучший Спирмен >=0.3 И пар >=5)")
    print()
    ok = [r for r in rows if r.get("status_scan") == "ok"]
    print(f"--- ТАБЛИЦА ПО КОШЕЛЬКАМ (с посчитанной толпой: {len(ok)} из {len(rows)}) ---")
    print(f"  {'адрес':<16}{'имя':<16}{'статус':<11}{'n':>4}{'crowd_2':>9}{'p75':>6}"
          f"{'нулей':>8}{'сумма SOL':>11}  уровень")
    for r in ok:
        print(f"  {r['address'][:14]+'…':<16}{str(r.get('name') or '')[:14]:<16}"
              f"{str(r.get('status') or '')[:10]:<11}{r['n_buys']:>4}{r['crowd_2_median']:>9}"
              f"{r['crowd_2_p75']:>6}{r['zero_share']*100:>7.0f}%{r['spend_median_sol']:>11}  {r['level']}")
    other = [r for r in rows if r.get("status_scan") != "ok"]
    if other:
        from collections import Counter
        print(f"  без таблицы: {dict(Counter(r.get('status_scan') for r in other))}")
    strong_c = [r for r in ok if r["level"] == "сильный" and r.get("status") == "кандидат"]
    print()
    print(f"  КАНДИДАТОВ В «СИЛЬНЫЙ» (crowd_2 >= 8): {len(strong_c)}")
    for r in strong_c[:30]:
        print(f"    {r['address']}  {r.get('name')}  crowd_2={r['crowd_2_median']} n={r['n_buys']}")
    print()
    sc = out["self_check"]
    print(f"--- САМОПРОВЕРКА (2 провайдера): {sc.get('ok')} ---")
    for it in sc.get("checks") or []:
        print(f"  {it['solscan']}")
        print(f"    slot: записан={it['slot_записан']} helius={it.get('slot_helius')} "
              f"публичный={it.get('slot_публичный_узел')} -> {'СОВПАЛ' if it['слот_совпал'] else 'РАСХОЖДЕНИЕ'}")
        print(f"    crowd_2_tx: записан={it['crowd_2_tx_записан']} helius={it.get('crowd_2_tx_helius')} "
              f"публичный={it.get('crowd_2_tx_публичный_узел')} -> "
              f"{'СОВПАЛА' if it['толпа_совпала'] else 'РАСХОЖДЕНИЕ'}")
    print("=" * 96)


if __name__ == "__main__":
    main()
