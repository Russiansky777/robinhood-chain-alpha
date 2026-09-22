#!/usr/bin/env python3
"""Владелец: РЕТРО-СИГНАЛ. Симуляция нашей сделки по прошлым покупкам
кошельков: вход в трёх вариантах скорости, выход через ~35 с.

ТОЛЬКО ЧТЕНИЕ ЦЕПОЧКИ. Ни одной сделки: в файле нет ни одного вызова
покупки/продажи и ни одного POST в DBot. Guard (dbot-sold-guard) и зонд
(dbot-detect-probe) не затрагиваются -- всё считается на раннере.

ТРИ СЦЕНАРИЯ ВХОДА (для покупки лидера в слоте S, минт M):
  E0 -- сразу за лидером: первая ЧУЖАЯ покупка M В ТОМ ЖЕ СЛОТЕ S после
        его транзакции по индексу. Если в слоте S такой покупки нет --
        no_entry для E0, отката в S+1 НЕТ. Это потолок достижимого.
  E1 -- через ~0.25с: первая чужая покупка M начиная со слота S+1.
  E2 -- через ~0.5с (так работает DBot сейчас): начиная со слота S+2.
E1/E2 ищут вперёд не более 2 слотов от стартового, иначе no_entry; E0 --
только слот S, без поиска вперёд, поэтому сценарии НЕ ПЕРЕКРЫВАЮТСЯ:
E0 живёт в S, E1 -- в S+1..S+3, E2 -- в S+2..S+4.

Рядом считаются два прежних варианта E0, чтобы разница определений была
видна, а не спрятана: E0_s1 (S и S+1 -- определение до этой правки) и
E0_loose (S..S+2, свободный вариант, перекрывается с E2).

ВЫХОД (общий для трёх сценариев): цена первой сделки с M (покупки ИЛИ
продажи) в окне T+33..T+40 с, где T -- время покупки лидера. Не нашлось
за 12 блоков -- no_exit.

sim_E0/E1/E2 = выход / вход - 1. Издержки НЕ вычитаются: это уровень
"сигнал" из правила владельца.

ПОЧЕМУ ОКНО ВЫХОДА ИМЕННО ТАКОЕ -- проверено по реальным данным до
написания кода: held_seconds наших 265 живых сделок дают медиану 35с,
p25=33, p75=36, и 95.5% попадают в 30-45с. Окно T+33..T+40 -- это не
предположение, а описание того, как мы реально выходим.

ЦЕНА СДЕЛКИ = SOL-эквивалент за токен по дельтам балансов транзакции.
Главным действующим лицом считается владелец, у которого ненулевая
дельта M И который ПОДПИСАНТ транзакции (пулы и их PDA подписантами не
бывают) -- иначе ценой оказалась бы дельта пула, то есть та же сделка с
обратным знаком. SOL-нога: WSOL из token balances + USDC/USDT по курсу
Gecko на момент сделки (тем же способом, что в скане толпы) + нативный
SOL из pre/postBalances; у плательщика комиссии комиссия вычитается,
иначе она попала бы в цену.
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

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_crowd_scan import (  # noqa: E402
    PILOT, PUBLIC_RPC, Rpc, helius_key, scrub, now_utc,
)

DEX_LABELS_PATH = (REPO_ROOT / "data" / "solana_buyer_200" / "prior" / "current"
                    / "buyer_100" / "dex_labels.json")
DEX_PROGRAMS = set(json.loads(DEX_LABELS_PATH.read_text()).keys()) if DEX_LABELS_PATH.exists() else set()

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
STABLES = {USDC, USDT}

ENTRY_LOG_PATH = REPO_ROOT / "data" / "solana_entry_log.json"
CROWD_PATH = REPO_ROOT / "data" / "solana_crowd_scan.json"
OUT_PATH = REPO_ROOT / "data" / "solana_retro_signal.json"
SIM_CACHE_PATH = REPO_ROOT / "data" / "solana_retro_signal_cache.json"

EXIT_FROM_S = 33
EXIT_TO_S = 40
EXIT_MAX_BLOCKS = 12
SCEN_LOOKAHEAD = 2          # не больше 2 слотов от стартового
COST_THRESHOLD_PCT = 2.5    # порог издержек владельца на 0.5 SOL
MIN_LEG_SOL = 0.05          # см. dust_ok(): ниже этого цена сделки -- не цена
SLOT_EST_S = 0.30           # стартовая оценка длительности слота, уточняется по факту

_PRINT_LOCK = threading.Lock()


def log(msg: str) -> None:
    with _PRINT_LOCK:
        print(f"[retro] {scrub(msg)}", flush=True)


# ---------- цена сделки из дельт балансов ----------

_GECKO_LOCK = threading.Lock()
_gecko_orig = fp.find_price_at_gecko


def sol_usd_at(t: int) -> float | None:
    with _GECKO_LOCK:
        try:
            row = _gecko_orig(t, t - 3600, t + 3600)
            v = float(row["event"]["p1_per_0"])
            return v if v > 0 else None
        except Exception:  # noqa: BLE001 -- курса нет, НЕ выдумываем
            return None


def _tb_map(entries, want_mints=None) -> dict:
    """[(owner, mint)] -> uiAmount."""
    out: dict = {}
    for b in entries or []:
        o, m = b.get("owner"), b.get("mint")
        if not o or not m:
            continue
        if want_mints is not None and m not in want_mints:
            continue
        amt = (b.get("uiTokenAmount") or {}).get("uiAmount")
        out[(o, m)] = out.get((o, m), 0.0) + float(amt or 0)
    return out


# Порог привязан к РЕАЛЬНОМУ числу, а не выбран на глаз: рента ATA в
# Solana -- 0.00203928 SOL, и именно такой ногой (0.0021576 SOL = рента
# плюс комиссия) в прошлом выпуске оказались оценены три независимые
# строки с ценой на 4 порядка мимо рынка. Порог 0.01 SOL лежит выше
# ренты с запасом и в 50 раз ниже медианной настоящей сделки (0.5 SOL),
# то есть режет вырожденное, а не рынок.
ATA_RENT_SOL = 0.00203928
MIN_SOL_LEG = 0.01


def tx_trades_for_mint(t: dict, mint: str, index: int, block_time: int | None) -> dict | None:
    """Разбор ОДНОЙ транзакции: кто торговал mint и по какой цене.

    БРАКОВКА ЯВНАЯ, А НЕ ТИХАЯ. Состязательный разбор кода (44 агента,
    10 подтверждённых находок) показал, что прежняя версия оценивала
    ЛЮБУЮ транзакцию, где у подписанта ненулевая дельта минта и
    SOL-нога противоположного знака -- включая обычные переводы, у
    которых единственный расход SOL это рента ATA. В выпуске уже лежали
    три строки с входной ногой ровно 0.0021576 SOL (рентного размера) у
    трёх независимых кошельков и ценой на 4 порядка мимо рынка.

    Четыре проверки, каждая со своей причиной в price_note:
      1. в транзакции есть известная DEX/AMM-программа -- ровно та
         проверка, что стоит в каноническом классификаторе репозитория
         (solana_batch5_rpc_check), где её необходимость уже задокумен-
         тирована: без неё метод засчитывал переводы и дасты;
      2. владелец не двигает больше одного НЕ-котировочного минта --
         маршрут tokenA->SOL->M нетит SOL-ногу почти в ноль и даёт цену
         на порядки ниже настоящей (канонический классификатор такие
         транзакции тоже пропускает как multi_mint);
      3. SOL-нога не меньше MIN_SOL_LEG -- отсекает рентные остатки и
         нетто-плоский арбитраж, где нормальная сумма делится на пыль;
      4. знаки дельт противоположны (обмен, а не раздача).
    """
    meta = t.get("meta") or {}
    if meta.get("err") is not None:
        return None
    txn = t.get("transaction") or {}
    sigs = txn.get("signatures") or []
    keys_raw = txn.get("accountKeys") or ((txn.get("message") or {}).get("accountKeys")) or []
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in keys_raw]
    signers = {k.get("pubkey") for k in keys_raw if isinstance(k, dict) and k.get("signer")}

    pre_all = _tb_map(meta.get("preTokenBalances"))
    post_all = _tb_map(meta.get("postTokenBalances"))
    deltas: dict[str, float] = {}
    for (o, m) in set(pre_all) | set(post_all):
        if m != mint:
            continue
        d = post_all.get((o, m), 0.0) - pre_all.get((o, m), 0.0)
        if d != 0:
            deltas[o] = deltas.get(o, 0.0) + d
    if not deltas:
        return None

    cands = [o for o in deltas if o in signers] or list(deltas)
    owner = max(cands, key=lambda o: abs(deltas[o]))
    token_delta = deltas[owner]

    def bad(note: str) -> dict:
        return {"index": index, "signature": sigs[0] if sigs else None, "owner": owner,
                "token_delta": token_delta, "sol_delta": None,
                "price_sol_per_token": None, "price_note": note,
                "is_buy": token_delta > 0, "sol_size": 0.0}

    # (1) реальная DEX/AMM-программа в транзакции
    if DEX_PROGRAMS and not (set(keys) & DEX_PROGRAMS):
        return bad("нет известной DEX/AMM-программы -- перевод/даст, не сделка")

    # (2) владелец двигает больше одного не-котировочного минта -> маршрут
    other_mints = {m for (o, m) in set(pre_all) | set(post_all)
                   if o == owner and m not in (WSOL, USDC, USDT) and m != mint
                   and (post_all.get((o, m), 0.0) - pre_all.get((o, m), 0.0)) != 0}
    if other_mints:
        return bad(f"многоминтовый маршрут ({len(other_mints)} других минтов) -- SOL-нога нетится")

    wsol = (post_all.get((owner, WSOL), 0.0) - pre_all.get((owner, WSOL), 0.0))
    stable_usd = 0.0
    for st in STABLES:
        stable_usd += post_all.get((owner, st), 0.0) - pre_all.get((owner, st), 0.0)
    native = 0.0
    if owner in keys:
        idx = keys.index(owner)
        pre_b, post_b = meta.get("preBalances") or [], meta.get("postBalances") or []
        if idx < len(pre_b) and idx < len(post_b):
            native = (post_b[idx] - pre_b[idx]) / 1e9
            if idx == 0:
                native += (meta.get("fee") or 0) / 1e9

    stable_sol = 0.0
    if stable_usd:
        if block_time is None:
            return bad("нет blockTime -- курс SOL/USD не взять")
        px = sol_usd_at(block_time)
        if px is None:
            return bad("курс SOL/USD на момент сделки не получен")
        stable_sol = stable_usd / px

    sol_delta = wsol + stable_sol + native
    # (3) SOL-нога должна быть настоящей оплатой, а не рентой/остатком
    if abs(sol_delta) < MIN_SOL_LEG:
        return bad(f"SOL-нога {abs(sol_delta):.9f} < {MIN_SOL_LEG} -- рента или остаток маршрута")
    # (4) обмен, а не раздача
    if (token_delta > 0) == (sol_delta > 0):
        return bad("знаки дельт токена и SOL совпали -- не обмен")

    return {
        "index": index, "signature": sigs[0] if sigs else None, "owner": owner,
        "token_delta": token_delta, "sol_delta": round(sol_delta, 9),
        "price_sol_per_token": abs(sol_delta) / abs(token_delta), "price_note": None,
        "is_buy": token_delta > 0, "sol_size": abs(round(sol_delta, 9)),
    }


# ---------- блоки: кэш по (слот, минт) ----------

class SlotTrades:
    """Для (слот, минт) -- список сделок по возрастанию индекса.

    Кэш по паре, а не по слоту целиком: в скане толпы замерено, что
    совпадений слота у разных кошельков не случилось НИ РАЗУ (0 попаданий
    на 1158 блоков), а держать разобранный блок целиком -- сотни мегабайт.
    Здесь один минт на симуляцию, и все 5 входных блоков спрашивают
    именно его."""

    def __init__(self, rpc: Rpc, capacity: int = 4000) -> None:
        self.rpc = rpc
        self.capacity = capacity
        self._d: OrderedDict[str, dict | None] = OrderedDict()
        self._lock = threading.Lock()
        self._klock: dict[str, threading.Lock] = {}
        self.hits = self.fetched = self.skipped = self.failed = self.outliers = 0
        self.detail = "accounts"
        self.fallback_full = 0

    def _key_lock(self, k: str) -> threading.Lock:
        with self._lock:
            lk = self._klock.get(k)
            if lk is None:
                lk = self._klock[k] = threading.Lock()
            return lk

    def get(self, slot: int, mint: str) -> dict | None:
        """{'trades': [...], 'block_time': int} либо None -- блок не отдался."""
        k = f"{slot}:{mint}"
        with self._lock:
            if k in self._d:
                self._d.move_to_end(k)
                self.hits += 1
                return self._d[k]
        with self._key_lock(k):
            with self._lock:
                if k in self._d:
                    self.hits += 1
                    return self._d[k]
            val = self._fetch(slot, mint)
            with self._lock:
                self._d[k] = val
                self._d.move_to_end(k)
                while len(self._d) > self.capacity:
                    old, _ = self._d.popitem(last=False)
                    self._klock.pop(old, None)
            return val

    def _fetch(self, slot: int, mint: str) -> dict | None:
        for detail in ([self.detail] if self.detail == "accounts" else ["full"]):
            try:
                blk = self.rpc.call("getBlock", [slot, {
                    "encoding": "jsonParsed", "transactionDetails": detail,
                    "maxSupportedTransactionVersion": 1, "rewards": False}])
            except RuntimeError as exc:
                msg = str(exc)
                if any(c in msg for c in ("-32004", "-32007", "-32009")) or \
                        "skipped" in msg or "not available" in msg:
                    self.skipped += 1          # слот пропущен лидером -- честный пустой
                    return {"trades": [], "block_time": None, "skipped": True}
                self.failed += 1
                log(f"getBlock {slot} не отдался: {msg[:140]}")
                return None
            if not blk:
                self.skipped += 1
                return {"trades": [], "block_time": None, "skipped": True}
            bt = blk.get("blockTime")
            trades = []
            for i, t in enumerate(blk.get("transactions") or []):
                r = tx_trades_for_mint(t, mint, i, bt)
                if r:
                    trades.append(r)
            # Цена, улетевшая на два порядка от соседей по тому же минту в
            # том же слоте, -- это вырожденная сделка, а не рынок: за один
            # слот рынок столько не проходит. Бракуем с причиной.
            priced = [t["price_sol_per_token"] for t in trades if t["price_sol_per_token"]]
            if len(priced) >= 3:
                med = statistics.median(priced)
                if med > 0:
                    for t in trades:
                        pr = t["price_sol_per_token"]
                        if pr and (pr > med * 100 or pr < med / 100):
                            t["price_sol_per_token"] = None
                            t["price_note"] = (f"цена {pr:.3e} отличается от медианы слота "
                                                f"{med:.3e} более чем в 100 раз -- вырожденная сделка")
                            self.outliers += 1
            self.fetched += 1
            return {"trades": trades, "block_time": bt}
        return None


def probe_accounts_detail(rpc: Rpc, slot: int) -> dict:
    """Владелец: "сначала проверь, отдаёт ли transactionDetails=accounts
    pre/postBalances". Проверяем на реальном блоке и печатаем факт."""
    out: dict = {"slot": slot}
    try:
        blk = rpc.call("getBlock", [slot, {
            "encoding": "jsonParsed", "transactionDetails": "accounts",
            "maxSupportedTransactionVersion": 1, "rewards": False}])
    except RuntimeError as exc:
        out["error"] = scrub(str(exc))[:200]
        return out
    txs = (blk or {}).get("transactions") or []
    out["n_tx"] = len(txs)
    sample = txs[:50]
    out["есть_preBalances"] = any((t.get("meta") or {}).get("preBalances") for t in sample)
    out["есть_postBalances"] = any((t.get("meta") or {}).get("postBalances") for t in sample)
    out["есть_preTokenBalances"] = any((t.get("meta") or {}).get("preTokenBalances") for t in sample)
    out["есть_fee"] = any((t.get("meta") or {}).get("fee") is not None for t in sample)
    out["есть_accountKeys"] = any(((t.get("transaction") or {}).get("accountKeys")) for t in sample)
    out["есть_флаг_signer"] = any(
        isinstance(k, dict) and "signer" in k
        for t in sample for k in ((t.get("transaction") or {}).get("accountKeys") or []))
    out["есть_blockTime"] = (blk or {}).get("blockTime") is not None
    out["вывод"] = ("accounts отдаёт всё нужное -- full не требуется"
                     if all(out.get(k) for k in ("есть_preBalances", "есть_postBalances",
                                                  "есть_preTokenBalances", "есть_accountKeys",
                                                  "есть_флаг_signer"))
                     else "accounts НЕ отдаёт часть нужного -- нужен full")
    return out


# ---------- время -> слот ----------

class SlotClock:
    def __init__(self, rpc: Rpc) -> None:
        self.rpc = rpc
        self._t: dict[int, int | None] = {}
        self._lock = threading.Lock()
        self.calls = 0

    def block_time(self, slot: int) -> int | None:
        with self._lock:
            if slot in self._t:
                return self._t[slot]
        try:
            v = self.rpc.call("getBlockTime", [slot])
            self.calls += 1
        except RuntimeError:
            v = None
        with self._lock:
            self._t[slot] = v
        return v

    def slot_at(self, s0: int, t0: int, target_t: int, max_probe: int = 6) -> int | None:
        """Слот, чей blockTime ближе всего снизу к target_t. Начинаем с
        оценки по длительности слота и уточняем реальными ответами узла,
        а не верим в константу."""
        dur = SLOT_EST_S
        guess = s0 + int((target_t - t0) / dur)
        for _ in range(max_probe):
            t = None
            for nudge in (0, 1, -1, 2, -2, 3, -3):
                t = self.block_time(guess + nudge)
                if t is not None:
                    guess += nudge
                    break
            if t is None:
                return None
            diff = target_t - t
            if abs(diff) <= 1:
                return guess
            if guess != s0:
                measured = (t - t0) / max(guess - s0, 1)
                if 0.05 < measured < 2.0:
                    dur = measured
            step = int(diff / dur)
            if step == 0:
                step = 1 if diff > 0 else -1
            guess += step
        return guess


# ---------- одна симуляция ----------

def first_foreign_buy(st: SlotTrades, mint: str, leader: str, start_slot: int,
                       after_index: int | None, lookahead: int = SCEN_LOOKAHEAD) -> dict:
    """Первая чужая покупка mint начиная со start_slot, не дальше
    lookahead слотов вперёд."""
    incomplete = False
    for step in range(lookahead + 1):
        s = start_slot + step
        node = st.get(s, mint)
        if node is None:
            incomplete = True
            continue
        for tr in node["trades"]:
            if not tr["is_buy"] or tr["owner"] == leader:
                continue
            if step == 0 and after_index is not None and tr["index"] <= after_index:
                continue
            if tr["price_sol_per_token"] is None:
                continue
            return {"found": True, "slot": s, **tr, "incomplete": incomplete}
    return {"found": False, "incomplete": incomplete}


def simulate(rpc: Rpc, st: SlotTrades, clock: SlotClock, leader: str, mint: str,
              slot: int, leader_sig: str, leader_index: int | None,
              block_time: int | None) -> dict:
    row: dict = {"leader": leader, "mint": mint, "source_slot": slot,
                  "leader_signature": leader_sig}
    node = st.get(slot, mint)
    if node is None:
        return {**row, "status": "no_block", "note": "блок слота лидера не отдался"}
    bt = node.get("block_time") or block_time
    row["block_time"] = bt
    if leader_index is None:
        leader_index = next((t["index"] for t in node["trades"] if t["signature"] == leader_sig), None)
    if leader_index is None:
        return {**row, "status": "no_leader_tx",
                "note": "транзакции лидера нет среди сделок этого минта в его же слоте"}
    row["leader_index"] = leader_index

    # ГОРИЗОНТ E0 -- РОВНО 0 слотов: только слот S. Распоряжение владельца:
    # "E0 = только чужие покупки в слоте S после транзакции лидера; если их
    # нет -- no_entry для E0 (никакого отката в S+1)". Так сценарии перестают
    # перекрываться: E0 -- S, E1 -- S+1.., E2 -- S+2..; раньше E0 с откатом в
    # S+1 совпадал с E1 по входу, и разница E0-E1 выходила ровно 0.0.
    # Прежние определения считаются рядом, чтобы сравнение было видно:
    #   E0_s1    -- S и S+1 (определение до этой правки),
    #   E0_loose -- S..S+2 (свободный вариант, перекрывается с E2).
    scen = {}
    for name, start, after, look in (("E0", slot, leader_index, 0),
                                      ("E0_s1", slot, leader_index, 1),
                                      ("E0_loose", slot, leader_index, SCEN_LOOKAHEAD),
                                      ("E1", slot + 1, None, SCEN_LOOKAHEAD),
                                      ("E2", slot + 2, None, SCEN_LOOKAHEAD)):
        r = first_foreign_buy(st, mint, leader, start, after, look)
        if r.get("found"):
            scen[name] = {"entry_price": r["price_sol_per_token"], "entry_slot": r["slot"],
                           "entry_index": r["index"], "entry_signature": r["signature"],
                           "entry_sol_size": r["sol_size"], "incomplete": r["incomplete"]}
        else:
            scen[name] = {"entry_price": None, "no_entry": True, "incomplete": r["incomplete"]}
    row["scenarios"] = scen

    # Отдельный вопрос владельца: доля случаев, когда в слоте ЛИДЕРА после
    # него вообще был покупатель. Считаем прямо по блоку, НЕ через E0:
    # first_foreign_buy пропускает сделки без пригодной цены, а здесь важен
    # сам факт покупки. Поэтому два счётчика: любой покупатель и покупатель
    # с посчитанной ценой (второй и есть вход E0).
    in_slot = [t for t in node["trades"]
               if t["is_buy"] and t["owner"] != leader and t["index"] > leader_index]
    row["buyers_in_leader_slot"] = len(in_slot)
    row["buyer_in_leader_slot"] = bool(in_slot)
    row["buyer_in_leader_slot_priced"] = any(
        t["price_sol_per_token"] is not None for t in in_slot)

    if bt is None:
        row["status"] = "no_exit"
        row["exit_note"] = "нет blockTime у блока лидера -- окно выхода не привязать ко времени"
        return row
    target = bt + EXIT_FROM_S
    s_exit = clock.slot_at(slot, bt, target)
    if s_exit is None:
        row["status"] = "no_exit"
        row["exit_note"] = "не удалось привязать время T+33с к слоту"
        return row
    exit_row = None
    exit_incomplete = False
    for step in range(EXIT_MAX_BLOCKS):
        node_e = st.get(s_exit + step, mint)
        if node_e is None:
            exit_incomplete = True
            continue
        ebt = node_e.get("block_time")
        if ebt is None:
            continue
        if ebt < bt + EXIT_FROM_S:
            continue
        if ebt > bt + EXIT_TO_S:
            break
        cand = next((t for t in node_e["trades"] if t["price_sol_per_token"] is not None), None)
        if cand:
            exit_row = {"exit_price": cand["price_sol_per_token"], "exit_slot": s_exit + step,
                         "exit_signature": cand["signature"], "exit_is_buy": cand["is_buy"],
                         "exit_sol_size": cand["sol_size"], "exit_block_time": ebt,
                         "exit_delay_s": ebt - bt}
            break
    row["exit_incomplete"] = exit_incomplete
    row["exit_scan_blocks"] = EXIT_MAX_BLOCKS
    if not exit_row:
        row["status"] = "no_exit"
        row["exit_note"] = f"в окне T+{EXIT_FROM_S}..{EXIT_TO_S}с за {EXIT_MAX_BLOCKS} блоков сделок с минтом нет"
        return row
    row.update(exit_row)
    for name in ("E0", "E0_s1", "E0_loose", "E1", "E2"):
        ep = scen[name].get("entry_price")
        row[f"sim_{name}"] = round((exit_row["exit_price"] / ep - 1) * 100, 4) if ep else None
    row["status"] = "ok" if row.get("sim_E2") is not None else "частично"
    return row


# ---------- источники симуляций ----------

def live_trades() -> list[dict]:
    d = json.loads(ENTRY_LOG_PATH.read_text())
    out = []
    for r in d.get("trades") or []:
        if r.get("source_slot") and r.get("mint") and r.get("source_signature") \
                and r.get("signal_pct") is not None:
            out.append({"leader": r["source_address"], "mint": r["mint"], "slot": r["source_slot"],
                         "signature": r["source_signature"], "index": None,
                         "block_time": None, "signal_pct": r["signal_pct"],
                         "task_name": r.get("task_name"), "buyers_between": r.get("buyers_between")})
    return out


def crowd_buys() -> tuple[list[dict], dict[str, dict]]:
    d = json.loads(CROWD_PATH.read_text())
    meta: dict[str, dict] = {}
    sims: list[dict] = []
    for w in d.get("wallets") or []:
        if w.get("status_scan") != "ok":
            continue
        is_cand = w.get("status") == "кандидат"
        if is_cand and w.get("n_buys", 0) < 3:
            continue
        meta[w["address"]] = {"name": w.get("name"), "status": w.get("status"),
                               "crowd_2": w.get("crowd_2_median"), "level": w.get("level"),
                               "n_buys": w.get("n_buys")}
        for b in w.get("buys") or []:
            if b.get("crowd_2") is None:
                continue
            sims.append({"leader": w["address"], "mint": b["mint"], "slot": b["slot"],
                          "signature": b["signature"], "index": b.get("own_index_in_block"),
                          "block_time": b.get("block_time"),
                          "spend_sol_equiv": b.get("spend_sol_equiv")})
    return sims, meta


# ---------- агрегация ----------

def dust_ok(r: dict, min_leg_sol: float) -> bool:
    """Обе ноги симуляции должны быть осмысленного размера.

    Найдено на реальном прогоне: при пылевых сделках цена вырождается и
    отношение выход/вход взрывается -- в выборке оказались значения до
    +554 989%, и все рекордсмены имеют ногу в 0.0015 SOL. Это не сигнал,
    а деление на почти ноль. Отсечка по размеру ноги, а не по величине
    самого sim_*: обрезать выбросы по результату значит подгонять ответ,
    обрезать по размеру сделки -- отбрасывать заведомо негодные цены.
    Ровно та же болезнь, что стоила нам 94% на продаже STONKY: в тонком
    пуле «цена» ничего не значит."""
    sc = (r.get("scenarios") or {}).get("E2") or {}
    entry = sc.get("entry_sol_size")
    exit_ = r.get("exit_sol_size")
    return (entry or 0) >= min_leg_sol and (exit_ or 0) >= min_leg_sol


def pct(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    v = sorted(vals)
    return v[min(int(len(v) * p), len(v) - 1)]


def share_above(vals: list[float], thr: float) -> float | None:
    return round(sum(1 for x in vals if x > thr) / len(vals), 4) if vals else None


def spearman(xs, ys):
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
    try:
        return round(statistics.correlation(ranks(xs), ranks(ys)), 4)
    except Exception:  # noqa: BLE001
        return None


def pearson(xs, ys):
    if len(xs) < 3:
        return None
    try:
        return round(statistics.correlation(xs, ys), 4)
    except Exception:  # noqa: BLE001
        return None


def calibration(rows: list[dict]) -> dict:
    pairs = [(r["sim_E2"], r["signal_pct"]) for r in rows
             if r.get("sim_E2") is not None and r.get("signal_pct") is not None]
    if not pairs:
        return {"n": 0, "verdict_ok": False, "note": "ни одной пары sim_E2/signal_pct"}
    xs = [a for a, _ in pairs]
    ys = [b for _, b in pairs]
    errs = [a - b for a, b in pairs]
    sign_ok = sum(1 for a, b in pairs if (a > 0) == (b > 0))
    rho = spearman(xs, ys)
    sign_share = round(sign_ok / len(pairs), 4)
    return {
        "n": len(pairs),
        "spearman": rho, "pearson": pearson(xs, ys),
        "median_abs_error_pct": round(statistics.median([abs(e) for e in errs]), 4),
        "median_bias_pct": round(statistics.median(errs), 4),
        "sign_agreement": sign_share,
        "median_sim_E2": round(statistics.median(xs), 4),
        "median_live_signal": round(statistics.median(ys), 4),
        "verdict_ok": bool(rho is not None and rho >= 0.6 and sign_share >= 0.70),
        "порог": "Спирмен >= 0.6 И совпадение знака >= 70%",
    }


def per_wallet(rows: list[dict], meta: dict[str, dict], min_leg_sol: float = MIN_LEG_SOL) -> list[dict]:
    by: dict[str, list[dict]] = {}
    dropped: dict[str, int] = {}
    for r in rows:
        if r.get("sim_E2") is not None and not dust_ok(r, min_leg_sol):
            dropped[r["leader"]] = dropped.get(r["leader"], 0) + 1
            continue
        by.setdefault(r["leader"], []).append(r)
    out = []
    for addr, rs in by.items():
        m = meta.get(addr, {})
        row = {"address": addr, "name": m.get("name"), "status": m.get("status"),
                "crowd_2": m.get("crowd_2"), "level": m.get("level"),
                "n_sim_total": len(rs), "n_отброшено_пыль": dropped.get(addr, 0),
                "n_no_entry": sum(1 for r in rs if (r.get("scenarios") or {}).get("E2", {}).get("no_entry")),
                "n_no_exit": sum(1 for r in rs if r.get("status") == "no_exit"),
                "n_incomplete": sum(1 for r in rs if r.get("exit_incomplete")
                                     or any((r.get("scenarios") or {}).get(k, {}).get("incomplete")
                                            for k in ("E0", "E1", "E2")))}
        for k in ("E0", "E0_s1", "E0_loose", "E1", "E2"):
            v = [r[f"sim_{k}"] for r in rs if r.get(f"sim_{k}") is not None]
            row[f"n_{k}"] = len(v)
            row[f"median_{k}"] = round(statistics.median(v), 4) if v else None
            row[f"share_above_{COST_THRESHOLD_PCT}_{k}"] = share_above(v, COST_THRESHOLD_PCT)
            row[f"n_no_entry_{k}"] = sum(
                1 for r in rs if ((r.get("scenarios") or {}).get(k, {}) or {}).get("no_entry"))
        # Медианы по сценариям считаются по РАЗНЫМ подвыборкам (у E0
        # горизонт короче, поэтому у него больше no_entry), и сравнивать
        # их между собой в одной строке нельзя -- в прогоне это давало
        # переворот знака до 15 п.п. Отдельно считаем медианы по ОБЩЕЙ
        # подвыборке, где есть все три: только их и сопоставляют.
        den_w = [r for r in rs if r.get("buyer_in_leader_slot") is not None]
        row["n_слот_лидера_знам"] = len(den_w)
        row["n_покупатель_в_слоте_лидера"] = sum(1 for r in den_w if r["buyer_in_leader_slot"])
        row["доля_покупатель_в_слоте_лидера"] = round(
            row["n_покупатель_в_слоте_лидера"] / len(den_w) * 100, 2) if den_w else None
        common = [r for r in rs if all(r.get(f"sim_{k}") is not None for k in ("E0", "E1", "E2"))]
        row["n_общих"] = len(common)
        for k in ("E0", "E1", "E2"):
            v = [r[f"sim_{k}"] for r in common]
            row[f"median_{k}_общих"] = round(statistics.median(v), 4) if v else None
        e2 = [r["sim_E2"] for r in rs if r.get("sim_E2") is not None]
        row["n_sim"] = len(e2)
        row["best_E2"] = round(max(e2), 4) if e2 else None
        row["worst_E2"] = round(min(e2), 4) if e2 else None
        out.append(row)
    out.sort(key=lambda r: (r["median_E2"] is None, -(r["median_E2"] or 0)))
    return out


def overall(rows: list[dict], label: str, min_leg_sol: float = MIN_LEG_SOL) -> dict:
    n_all = sum(1 for r in rows if r.get("sim_E2") is not None)
    rows = [r for r in rows if r.get("sim_E2") is None or dust_ok(r, min_leg_sol)]
    o = {"label": label, "мин_нога_SOL": min_leg_sol,
         "отброшено_пылевых": n_all - sum(1 for r in rows if r.get("sim_E2") is not None)}
    for k in ("E0", "E0_s1", "E0_loose", "E1", "E2"):
        v = [r[f"sim_{k}"] for r in rows if r.get(f"sim_{k}") is not None]
        o[f"n_{k}"] = len(v)
        o[f"median_{k}"] = round(statistics.median(v), 4) if v else None
        o[f"p25_{k}"] = round(pct(v, 0.25), 4) if v else None
        o[f"p75_{k}"] = round(pct(v, 0.75), 4) if v else None
        o[f"share_above_{COST_THRESHOLD_PCT}_{k}"] = share_above(v, COST_THRESHOLD_PCT)
    dl = [r["exit_delay_s"] for r in rows if r.get("exit_delay_s") is not None]
    if dl:
        o["задержка_выхода_с"] = {"мин": min(dl), "медиана": statistics.median(dl), "макс": max(dl)}
        o["УСЕЧЕНИЕ_ОКНА"] = (
            f"окно задано T+{EXIT_FROM_S}..{EXIT_TO_S}с, но {EXIT_MAX_BLOCKS} блоков при слоте "
            f"250-300 мс покрывают лишь ~{EXIT_MAX_BLOCKS*0.27:.1f}с: фактический максимум "
            f"задержки {max(dl)}с, до T+{EXIT_TO_S}с скан структурно не доходит")
    # Доля случаев, когда в слоте лидера ПОСЛЕ него вообще был покупатель.
    # Знаменатель -- только те симуляции, где блок лидера отдался и его
    # транзакция в нём нашлась (иначе вопрос не определён).
    den = [r for r in rows if r.get("buyer_in_leader_slot") is not None]
    o["покупатель_в_слоте_лидера"] = {
        "знаменатель_симуляций": len(den),
        "n_был_покупатель": sum(1 for r in den if r["buyer_in_leader_slot"]),
        "доля_был_покупатель": round(
            sum(1 for r in den if r["buyer_in_leader_slot"]) / len(den) * 100, 2) if den else None,
        "n_с_посчитанной_ценой": sum(1 for r in den if r.get("buyer_in_leader_slot_priced")),
        "доля_с_посчитанной_ценой": round(
            sum(1 for r in den if r.get("buyer_in_leader_slot_priced")) / len(den) * 100, 2) if den else None,
        "медиана_покупателей_в_слоте": statistics.median(
            [r.get("buyers_in_leader_slot") or 0 for r in den]) if den else None,
        "пояснение": ("вторая доля -- это и есть доля симуляций, где у E0 "
                       "есть вход: цена считается не по всякой покупке"),
    }
    both = [(r["sim_E0"], r["sim_E1"], r["sim_E2"]) for r in rows
            if None not in (r.get("sim_E0"), r.get("sim_E1"), r.get("sim_E2"))]
    o["n_все_три"] = len(both)
    if both:
        o["медиана_E0_минус_E2"] = round(statistics.median([a - c for a, _, c in both]), 4)
        o["медиана_E1_минус_E2"] = round(statistics.median([b - c for _, b, c in both]), 4)
        o["медиана_E0_минус_E1"] = round(statistics.median([a - b for a, b, _ in both]), 4)
    return o


# ---------- самопроверка ----------

def self_check(rpc: Rpc, rows: list[dict], n: int = 3) -> dict:
    pool = [r for r in rows if r.get("sim_E2") is not None]
    if not pool:
        return {"ok": None, "note": "нет завершённых симуляций -- сверять нечего"}
    random.seed(20260922)
    sample = random.sample(pool, min(n, len(pool)))
    checks, all_ok = [], True
    for r in sample:
        item = {"leader": r["leader"], "mint": r["mint"], "source_slot": r["source_slot"],
                 "solscan_лидер": f"https://solscan.io/tx/{r['leader_signature']}"}
        for prov, url in (("helius", None), ("публичный_узел", PUBLIC_RPC)):
            st2 = SlotTrades(Rpc(rpc.key) if url is None else Rpc(rpc.key), capacity=64)
            st2.rpc = rpc
            prices = {}
            try:
                for name in ("E0", "E1", "E2"):
                    sc = (r.get("scenarios") or {}).get(name) or {}
                    if sc.get("entry_price") is None:
                        prices[name] = None
                        continue
                    prices[name] = recompute_price(rpc, sc["entry_slot"], r["mint"],
                                                    sc["entry_signature"], url)
                prices["exit"] = recompute_price(rpc, r["exit_slot"], r["mint"],
                                                  r["exit_signature"], url)
            except RuntimeError as exc:
                item[f"{prov}_ошибка"] = scrub(str(exc))[:140]
            item[prov] = prices
        rec = {k: (r.get("scenarios") or {}).get(k, {}).get("entry_price") for k in ("E0", "E1", "E2")}
        rec["exit"] = r.get("exit_price")
        item["записано"] = rec
        ok = True
        for k in ("E0", "E1", "E2", "exit"):
            want = rec.get(k)
            for prov in ("helius", "публичный_узел"):
                got = (item.get(prov) or {}).get(k)
                if want is None or got is None:
                    continue
                if abs(got - want) > max(abs(want) * 1e-9, 1e-18):
                    ok = False
        item["совпало"] = ok
        all_ok = all_ok and ok
        checks.append(item)
    return {"ok": all_ok, "checks": checks}


def recompute_price(rpc: Rpc, slot: int, mint: str, signature: str, url: str | None) -> float | None:
    blk = rpc.call("getBlock", [slot, {"encoding": "jsonParsed", "transactionDetails": "accounts",
                                        "maxSupportedTransactionVersion": 1, "rewards": False}], url=url)
    if not blk:
        return None
    bt = blk.get("blockTime")
    for i, t in enumerate(blk.get("transactions") or []):
        sigs = ((t.get("transaction") or {}).get("signatures")) or []
        if sigs and sigs[0] == signature:
            r = tx_trades_for_mint(t, mint, i, bt)
            return r["price_sol_per_token"] if r else None
    return None


# ---------- прогон ----------

class SimCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._d: dict = {}
        if path.exists():
            try:
                self._d = json.loads(path.read_text())
            except (ValueError, OSError):
                self._d = {}
        self.hits = 0

    def get(self, sig: str) -> dict | None:
        v = self._d.get(sig)
        if v is not None:
            with self._lock:
                self.hits += 1
        return v

    def put(self, sig: str, row: dict) -> None:
        with self._lock:
            self._d[sig] = row

    def save(self) -> None:
        with self._lock:
            self.path.write_text(json.dumps(self._d, ensure_ascii=False, default=str))


def run(rpc: Rpc, st: SlotTrades, clock: SlotClock, items: list[dict], workers: int,
        label: str, cache: SimCache | None) -> list[dict]:
    rows: list[dict] = []
    todo = []
    for it in items:
        hit = cache.get(it["signature"]) if cache else None
        if hit is not None:
            rows.append({**hit, **{k: v for k, v in it.items()
                                    if k in ("signal_pct", "task_name", "buyers_between", "spend_sol_equiv")}})
        else:
            todo.append(it)
    if cache and rows:
        log(f"{label}: из кэша {len(rows)}, считать {len(todo)}")
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(simulate, rpc, st, clock, it["leader"], it["mint"], it["slot"],
                           it["signature"], it.get("index"), it.get("block_time")): it
                for it in todo}
        for fut in as_completed(futs):
            it = futs[fut]
            try:
                row = fut.result()
            except Exception as exc:  # noqa: BLE001
                row = {"leader": it["leader"], "mint": it["mint"], "source_slot": it["slot"],
                        "leader_signature": it["signature"], "status": "ошибка",
                        "note": scrub(f"{type(exc).__name__}: {exc}")[:200]}
            if cache and row.get("status") in ("ok", "частично", "no_exit", "no_leader_tx"):
                cache.put(it["signature"], row)
            row.update({k: v for k, v in it.items()
                        if k in ("signal_pct", "task_name", "buyers_between", "spend_sol_equiv")})
            rows.append(row)
            done += 1
            if done % 20 == 0 or done == len(todo):
                log(f"{label}: {done}/{len(todo)} | блоков={st.fetched} из кэша={st.hits} "
                    f"пропущено={st.skipped} | getBlockTime={clock.calls} | RPC={rpc.calls} "
                    f"ретраев={rpc.retries}")
                if cache:
                    cache.save()
    if cache:
        cache.save()
    return rows


def reaggregate(min_leg_sol: float) -> None:
    """Пересчитать сводку из уже посчитанного файла, не трогая сеть."""
    out = json.loads(OUT_PATH.read_text())
    rows = out["sims"]
    meta: dict[str, dict] = {}
    if CROWD_PATH.exists():
        for w in json.loads(CROWD_PATH.read_text()).get("wallets") or []:
            meta[w["address"]] = {"name": w.get("name"), "status": w.get("status"),
                                   "crowd_2": w.get("crowd_2_median"), "level": w.get("level"),
                                   "n_buys": w.get("n_buys")}
    out["config"]["min_leg_sol"] = min_leg_sol
    out["overall"] = overall(rows, "все кошельки", min_leg_sol)
    out["overall_pilot"] = overall([r for r in rows if r["leader"] == PILOT], "пилот", min_leg_sol)
    if out.get("mode") == "full":
        out["wallets"] = per_wallet(rows, meta, min_leg_sol)
    if out.get("calibration"):
        out["calibration"] = calibration([r for r in rows if dust_ok(r, min_leg_sol) or r.get("sim_E2") is None])
    out["ЧЕСТНЫЕ_ОГОВОРКИ"].append(
        f"Симуляции, где вход или выход мельче {min_leg_sol} SOL, ОТБРОШЕНЫ: при пылевых сделках "
        "цена вырождается и отношение выход/вход взрывается (в сыром прогоне до +554989%). "
        "Отсекается размер сделки, а не величина результата -- иначе это была бы подгонка.")
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    report(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=("calibrate", "full"), default="calibrate")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--time-budget-s", type=int, default=75 * 60)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-interval-s", type=float, default=0.12)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--min-leg-sol", type=float, default=MIN_LEG_SOL)
    ap.add_argument("--reaggregate", action="store_true",
                    help="пересчитать сводку из готового файла, без вызовов сети")
    args = ap.parse_args()

    if args.reaggregate:
        reaggregate(args.min_leg_sol)
        return

    started = time.monotonic()
    key, key_name = helius_key()
    os.environ.setdefault("HELIUS_API", key)
    rpc = Rpc(key, min_interval_s=args.min_interval_s, workers=args.workers)
    rpc.deadline = started + args.time_budget_s
    fp.set_soft_deadline(rpc.deadline)
    st = SlotTrades(rpc)
    clock = SlotClock(rpc)
    log(f"ключ Helius из {key_name}; режим={args.mode}; воркеров={args.workers}")

    live = live_trades()
    probe = probe_accounts_detail(rpc, live[0]["slot"]) if live else {"error": "нет живых сделок"}
    log("ПРОБА transactionDetails=accounts: " + json.dumps(probe, ensure_ascii=False))
    if probe.get("вывод", "").startswith("accounts НЕ"):
        st.detail = "full"
        log("переключаюсь на transactionDetails=full -- accounts не отдаёт нужные поля")

    cache = None if args.no_cache else SimCache(SIM_CACHE_PATH)
    if args.mode == "calibrate":
        items = live[:args.limit] if args.limit else live
        meta: dict[str, dict] = {}
        label = "калибровка"
    else:
        items, meta = crowd_buys()
        if args.limit:
            items = items[:args.limit]
        label = "основной"
    log(f"{label}: симуляций к расчёту {len(items)}")

    rows = run(rpc, st, clock, items, args.workers, label, cache)
    calib = calibration(rows) if args.mode == "calibrate" else None
    wallets = per_wallet(rows, meta, args.min_leg_sol) if args.mode == "full" else []
    check = self_check(rpc, rows)

    out = {
        "generated_at_utc": now_utc(),
        "mode": args.mode,
        "ЧЕСТНЫЕ_ОГОВОРКИ": [
            "Только чтение цепочки: ни одного вызова покупки/продажи.",
            "Окно выхода T+33..T+40с выбрано не на глаз: held_seconds наших 265 живых сделок дают "
            "медиану 35с, p25=33, p75=36, и 95.5% попадают в 30-45с.",
            "Секунды в названиях сценариев даны для слота 250 мс (так с 18.09). Для более ранних "
            "сделок слот был ~300 мс, то есть E1/E2 соответствуют ~0.30с/~0.60с, а не 0.25/0.50. "
            "Сами сценарии заданы В СЛОТАХ и от этого не зависят -- меняется только подпись в секундах.",
            "Цена берётся по дельтам балансов ТРЕЙДЕРА (подписанта), а не по состоянию пула: в неё "
            "входят проскальзывание и чаевые валидатору этого трейдера. У входа и выхода это разные "
            "люди, так что смещение частично гасится, но не исчезает -- это ограничение метода.",
            "Издержки не вычитаются: sim_* -- это уровень «сигнал», как в правиле владельца. "
            "Порог сравнения +2.5% -- издержки на 0.5 SOL.",
            "no_entry/no_exit и incomplete считаются и печатаются отдельно: недоступный блок нигде "
            "не превращается в тихий ноль.",
        ],
        "config": {"exit_window_s": [EXIT_FROM_S, EXIT_TO_S], "exit_max_blocks": EXIT_MAX_BLOCKS,
                    "scenario_lookahead_slots": SCEN_LOOKAHEAD, "cost_threshold_pct": COST_THRESHOLD_PCT,
                    "helius_key_env_name": key_name, "workers": args.workers,
                    "min_interval_s": args.min_interval_s},
        "проба_accounts": probe,
        "run_stats": {"n_sims": len(rows), "blocks_fetched": st.fetched, "blocks_from_cache": st.hits,
                       "blocks_skipped": st.skipped, "blocks_failed": st.failed,
                       "цен_забраковано_выбросом": st.outliers,
                       "getBlockTime_calls": clock.calls, "rpc_calls": rpc.calls,
                       "rpc_retries": rpc.retries, "rpc_errors": rpc.errors,
                       "batch_splits": getattr(rpc, "splits", 0),
                       "sims_from_cache": (cache.hits if cache else 0),
                       "elapsed_s": round(time.monotonic() - started, 1),
                       "budget_exhausted": rpc.expired(),
                       "statuses": {k: sum(1 for r in rows if r.get("status") == k)
                                     for k in sorted({r.get("status") for r in rows})}},
        "calibration": calib,
        "overall": overall(rows, "все кошельки", args.min_leg_sol),
        "overall_pilot": overall([r for r in rows if r["leader"] == PILOT], "пилот", args.min_leg_sol),
        "wallets": wallets,
        "self_check": check,
        "sims": rows,
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    report(out)


def report(out: dict) -> None:
    print()
    print("=" * 104)
    print(f"РЕТРО-СИГНАЛ, режим={out['mode']}, {out['generated_at_utc']}")
    print("проба accounts:", json.dumps(out["проба_accounts"], ensure_ascii=False))
    print("статистика:", json.dumps(out["run_stats"], ensure_ascii=False))
    c = out.get("calibration")
    if c:
        print()
        print("--- КАЛИБРОВКА sim_E2 против живого signal_pct ---")
        for k in ("n", "spearman", "pearson", "median_abs_error_pct", "median_bias_pct",
                   "sign_agreement", "median_sim_E2", "median_live_signal"):
            print(f"  {k} = {c.get(k)}")
        print(f"  ВЕРДИКТ: {'ГЕЙТ ПРОЙДЕН' if c['verdict_ok'] else 'ГЕЙТ НЕ ПРОЙДЕН'} ({c.get('порог')})")
    for key in ("overall", "overall_pilot"):
        o = out[key]
        print()
        print(f"--- ЦЕНА ОПОЗДАНИЯ: {o['label']} ---")
        for k in ("E0", "E0_s1", "E0_loose", "E1", "E2"):
            print(f"  {k:<9}: n={o[f'n_{k}']:<5} медиана={o[f'median_{k}']}%  p25={o[f'p25_{k}']}  "
                  f"p75={o[f'p75_{k}']}  доля>+2.5%={o[f'share_above_2.5_{k}']}")
        if o.get("УСЕЧЕНИЕ_ОКНА"):
            print(f"  задержка выхода: {json.dumps(o['задержка_выхода_с'], ensure_ascii=False)}")
            print(f"  ВНИМАНИЕ: {o['УСЕЧЕНИЕ_ОКНА']}")
        if o.get("n_все_три"):
            print(f"  на общих {o['n_все_три']} симуляциях: E0-E2={o['медиана_E0_минус_E2']} п.п., "
                  f"E1-E2={o['медиана_E1_минус_E2']} п.п., E0-E1={o['медиана_E0_минус_E1']} п.п.")
        b = o.get("покупатель_в_слоте_лидера") or {}
        if b.get("знаменатель_симуляций"):
            print(f"  покупатель в слоте лидера после него: {b['n_был_покупатель']}/"
                  f"{b['знаменатель_симуляций']} = {b['доля_был_покупатель']}%; "
                  f"из них с посчитанной ценой (вход E0 есть): "
                  f"{b['n_с_посчитанной_ценой']} = {b['доля_с_посчитанной_ценой']}%; "
                  f"медиана числа таких покупателей в слоте: {b['медиана_покупателей_в_слоте']}")
    if out["wallets"]:
        print()
        print(f"--- КОШЕЛЬКИ (по медиане sim_E2), всего {len(out['wallets'])} ---")
        print("  медианы E0/E1/E2 -- по ОБЩЕЙ подвыборке (n_общих), иначе они несравнимы между собой")
        print(f"  {'адрес':<46}{'имя':<15}{'статус':<10}{'n':>3}{'общ':>4}{'crd2':>6}"
              f"{'medE0':>8}{'medE1':>8}{'medE2':>8}{'>2.5E2':>8}{'noE0':>5}{'noE2':>5}{'no_ex':>6}")
        for w in out["wallets"]:
            print(f"  {w['address']:<46}{str(w.get('name') or '')[:14]:<15}{str(w.get('status') or '')[:9]:<10}"
                  f"{w['n_sim']:>3}{w.get('n_общих', 0):>4}{str(w.get('crowd_2')):>6}"
                  f"{str(w.get('median_E0_общих')):>8}{str(w.get('median_E1_общих')):>8}"
                  f"{str(w.get('median_E2_общих')):>8}{str(w['share_above_2.5_E2']):>8}"
                  f"{w.get('n_no_entry_E0', 0):>5}{w.get('n_no_entry_E2', 0):>5}{w['n_no_exit']:>6}")
        sel = [w for w in out["wallets"] if w.get("status") == "кандидат"
               and (w.get("crowd_2") or 0) >= 4 and (w.get("median_E2") or -1e9) > COST_THRESHOLD_PCT]
        print()
        print(f"  КАНДИДАТЫ С crowd_2>=4 И медианой sim_E2 > +{COST_THRESHOLD_PCT}%: {len(sel)}")
        for w in sel:
            print(f"    {w['address']}  {w.get('name')}  crowd_2={w['crowd_2']} "
                  f"n={w['n_sim']} medE2={w['median_E2']}% доля>2.5%={w['share_above_2.5_E2']}")
    sc = out["self_check"]
    print()
    print(f"--- САМОПРОВЕРКА (2 провайдера): {sc.get('ok')} ---")
    for it in sc.get("checks") or []:
        print(f"  {it.get('solscan_лидер')}")
        print(f"    записано:  {json.dumps(it.get('записано'), ensure_ascii=False)}")
        print(f"    helius:    {json.dumps(it.get('helius'), ensure_ascii=False)}")
        print(f"    публичный: {json.dumps(it.get('публичный_узел'), ensure_ascii=False)}")
        print(f"    совпало: {it.get('совпало')}")
    print("=" * 104)


if __name__ == "__main__":
    main()
