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
# ДВА РАЗНЫХ ФАЙЛА -- по распоряжению владельца. Раньше оба режима писали
# в один solana_retro_signal.json, и калибровочный прогон затирал выгрузку
# полного: таблица по кошелькам (90 строк) исчезала, оставалась только
# калибровка с пустым списком wallets. Теперь каждый режим пишет в свой
# файл и затирать нечего.
OUT_PATH_FULL = REPO_ROOT / "data" / "solana_retro_signal.json"
OUT_PATH_CALIBRATE = REPO_ROOT / "data" / "solana_retro_signal_calibration.json"


def out_path_for(mode: str) -> Path:
    return OUT_PATH_CALIBRATE if mode == "calibrate" else OUT_PATH_FULL


SIM_CACHE_PATH = REPO_ROOT / "data" / "solana_retro_signal_cache.json"
PROGRESS_PATH = REPO_ROOT / "data" / "solana_retro_signal_progress.json"

# ВЕРСИЯ МЕТОДА. Кэш симуляций хранится вместе с ней, и записи ЧУЖОЙ
# версии при загрузке отбрасываются. Без этого возобновление после
# правки метода тихо смешало бы строки, посчитанные по старому и новому
# правилу, -- а именно из-за этого риска прогон после правки приходилось
# гонять с --no-cache, то есть вообще без возможности возобновиться.
# Поднимать при ЛЮБОМ изменении, влияющем на цифры в строке симуляции.
#   1 -- исходный метод;
#   2 -- E0 без перекрытия (только слот S);
#   3 -- откат входа на цену лидера + окно выхода до T+120с.
METHOD_VERSION = 3
CHECKPOINT_S = 900   # ~15 минут между сохранениями на диск

EXIT_FROM_S = 33
# ОКНО ВЫХОДА = ТО, КОТОРЫМ МЫ РЕАЛЬНО ТОРГУЕМ, T+33..45с.
#
# История этого числа, чтобы не наступить дважды. Прежнее T+33..40с
# структурно не добиралось до конца: 12 блоков при слоте 250-300 мс
# покрывают ~3.2с, и 62 симуляции из 506 уходили в no_exit не потому,
# что сделок не было, а потому что скан не дошёл. Я расширил окно до
# T+120с -- и это оказалось дорого и бесполезно одновременно: строки с
# выходом позже 45с всё равно не идут в медиану по правилу exit_slow,
# то есть добор с 45с до 120с НЕ МЕНЯЕТ НИ ОДНОЙ ИТОГОВОЙ ЦИФРЫ, но
# съедает почти всё время прогона (420 блоков на симуляцию; при 12%
# симуляций без выхода это 155 минут, при 25% -- 316 минут).
# Правильная граница -- горизонт удержания: медиана held_seconds 35с,
# p75 36с, 95.5% в 30-45с.
EXIT_TO_S = 45
EXIT_MAX_BLOCKS = 12          # первый, дешёвый проход
EXIT_DEEP_MAX_BLOCKS = 60     # добор до T+45с: 12с / ~0.28с на слот ~ 43, с запасом 60
EXIT_SLOW_S = 45              # выше -- строка не идёт в основную медиану
# При EXIT_TO_S == EXIT_SLOW_S медленных строк не бывает по построению:
# скан останавливается ровно на границе. Механика exit_slow оставлена --
# она снова заработает, если окно когда-нибудь расширят.
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
    # Сделка самого лидера -- источник цены для отката, когда чужих сделок
    # в окне нет. Берётся из того же разбора блока, тем же способом.
    leader_trade = next((t for t in node["trades"] if t["signature"] == leader_sig), None)
    row["leader_price"] = (leader_trade or {}).get("price_sol_per_token")
    row["leader_sol_size"] = (leader_trade or {}).get("sol_size")

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
                           "entry_sol_size": r["sol_size"], "incomplete": r["incomplete"],
                           "entry_src": "market"}
        elif leader_trade and leader_trade.get("price_sol_per_token") is not None:
            # ОТКАТ НА ЦЕНУ ЛИДЕРА (распоряжение владельца). Отсутствие
            # ЧУЖИХ сделок в окне означает, что цена НЕ СДВИНУЛАСЬ, а не
            # что цены нет. Раньше такая строка уходила в no_entry, и
            # sim_E2 не считался вовсе -- метод молча выбрасывал именно
            # спокойные минты и оставлял людные, то есть был смещён.
            # Цена лидера берётся тем же способом (дельты его балансов).
            scen[name] = {"entry_price": leader_trade["price_sol_per_token"],
                           "entry_slot": slot, "entry_index": leader_index,
                           "entry_signature": leader_sig,
                           "entry_sol_size": leader_trade.get("sol_size"),
                           "incomplete": r["incomplete"], "entry_src": "leader_price",
                           "no_market_entry": True}
        else:
            scen[name] = {"entry_price": None, "no_entry": True, "incomplete": r["incomplete"],
                           "entry_src": None,
                           "почему": ("чужих сделок в окне нет, и цена самой сделки лидера "
                                       "не посчиталась -- подставлять нечего")}
    row["scenarios"] = scen
    row["entry_src_E2"] = (scen.get("E2") or {}).get("entry_src")

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
    # ДВА ПРОХОДА. Первый -- дешёвый (EXIT_MAX_BLOCKS блоков): на прошлом
    # прогоне 444 симуляции из 506 находили выход за 12 блоков с задержкой
    # 33-37с. Глубокий добор до T+120с делается ТОЛЬКО для тех, кому
    # первого прохода не хватило, иначе стоимость прогона выросла бы на
    # порядок на ровном месте.
    exit_row = None
    exit_incomplete = False
    scanned = 0
    hit_cap = False
    window_done = False   # дошли до конца окна по ВРЕМЕНИ -- глубокий проход не нужен
    for limit in (EXIT_MAX_BLOCKS, EXIT_DEEP_MAX_BLOCKS):
        if exit_row is not None or window_done:
            break
        for step in range(scanned, limit):
            node_e = st.get(s_exit + step, mint)
            scanned = step + 1
            if node_e is None:
                exit_incomplete = True
                continue
            ebt = node_e.get("block_time")
            if ebt is None:
                continue
            if ebt < bt + EXIT_FROM_S:
                continue
            if ebt > bt + EXIT_TO_S:
                window_done = True
                break
            cand = next((t for t in node_e["trades"] if t["price_sol_per_token"] is not None), None)
            if cand:
                exit_row = {"exit_price": cand["price_sol_per_token"], "exit_slot": s_exit + step,
                             "exit_signature": cand["signature"], "exit_is_buy": cand["is_buy"],
                             "exit_sol_size": cand["sol_size"], "exit_block_time": ebt,
                             "exit_delay_s": ebt - bt}
                break
        else:
            hit_cap = (limit == EXIT_DEEP_MAX_BLOCKS)
    row["exit_incomplete"] = exit_incomplete
    row["exit_scan_blocks"] = scanned
    row["exit_scan_hit_cap"] = hit_cap
    if not exit_row:
        row["status"] = "no_exit"
        row["exit_note"] = (
            f"в окне T+{EXIT_FROM_S}..{EXIT_TO_S}с за {scanned} блоков сделок с минтом нет"
            + (f"; УПЁРЛИСЬ В ПОТОЛОК {EXIT_DEEP_MAX_BLOCKS} блоков, до T+{EXIT_TO_S}с скан "
               f"мог не дойти" if hit_cap else ""))
        return row
    row.update(exit_row)
    # Медленный выход: нашёлся, но позже, чем мы реально держим позицию
    # (медиана held_seconds 35с, p75 36с). Такие строки НЕ выбрасываются
    # -- они считаются и показываются отдельно, но в основную медиану не
    # идут: это уже не "выход через ~35с", а другой горизонт.
    row["exit_slow"] = exit_row["exit_delay_s"] > EXIT_SLOW_S
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


def crowd_buys(min_buys: int = 3, extra_paths: list[Path] | None = None) -> tuple[list[dict], dict[str, dict]]:
    """Покупки кошельков из скана толпы.

    min_buys -- порог числа покупок для КАНДИДАТА (у кошельков в задаче
    порога нет). Владелец: добить дыру по кандидатам порогом от 1.

    extra_paths -- дополнительные сканы (например, семидневное окно по
    кошелькам, у которых за 72ч покупок не набралось). Запись из
    дополнительного скана ЗАМЕЩАЕТ основную по тому же адресу, а не
    добавляется к ней: иначе одна и та же покупка попала бы в выборку
    дважды и вес такого кошелька удвоился бы."""
    wallets: dict[str, dict] = {}
    sources: dict[str, str] = {}
    for path, tag in ([(CROWD_PATH, "72ч")] + [(x, "расширенный") for x in (extra_paths or [])]):
        if not path.exists():
            log(f"скана нет, пропускаю: {path}")
            continue
        d = json.loads(path.read_text())
        for w in d.get("wallets") or []:
            if not w.get("address"):
                continue
            wallets[w["address"]] = w
            sources[w["address"]] = tag
        log(f"скан {path.name} ({tag}): кошельков {len(d.get('wallets') or [])}")

    meta: dict[str, dict] = {}
    sims: list[dict] = []
    for w in wallets.values():
        if w.get("status_scan") != "ok":
            continue
        is_cand = w.get("status") == "кандидат"
        if is_cand and w.get("n_buys", 0) < min_buys:
            continue
        meta[w["address"]] = {"name": w.get("name"), "status": w.get("status"),
                               "crowd_2": w.get("crowd_2_median"), "level": w.get("level"),
                               "n_buys": w.get("n_buys"),
                               "окно_скана": sources.get(w["address"])}
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


def exit_ok(r: dict) -> bool:
    """Строка годится в ОСНОВНУЮ медиану, если выход нашёлся не позже
    EXIT_SLOW_S. Медленные не выбрасываются -- они считаются отдельно:
    это другой горизонт удержания, а не тот, которым мы торгуем."""
    return not r.get("exit_slow")


def row_usable(r: dict, min_leg_sol: float) -> bool:
    """Единый фильтр для всех сводок: не пыль И не медленный выход.
    Раньше фильтр был только по пыли и применялся в трёх местах
    по-разному -- теперь одно правило в одном месте."""
    return dust_ok(r, min_leg_sol) and exit_ok(r)


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
    slow: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("sim_E2") is not None and not dust_ok(r, min_leg_sol):
            dropped[r["leader"]] = dropped.get(r["leader"], 0) + 1
            continue
        if r.get("sim_E2") is not None and not exit_ok(r):
            slow.setdefault(r["leader"], []).append(r)
            continue
        by.setdefault(r["leader"], []).append(r)
    for addr in slow:
        by.setdefault(addr, [])
    out = []
    for addr, rs in by.items():
        m = meta.get(addr, {})
        row = {"address": addr, "name": m.get("name"), "status": m.get("status"),
                "crowd_2": m.get("crowd_2"), "level": m.get("level"),
                "окно_скана": m.get("окно_скана"), "n_buys_скана": m.get("n_buys"),
                "n_sim_total": len(rs), "n_отброшено_пыль": dropped.get(addr, 0),
                "n_медленных_исключено": len(slow.get(addr) or []),
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
        # Доля симуляций В ПЛЮС -- нужна для фильтра кандидатов владельца
        # ("больше половины симуляций в плюс"). Считается по тем же
        # строкам, что и median_E2.
        row["доля_в_плюс_E2"] = round(sum(1 for x in e2 if x > 0) / len(e2), 4) if e2 else None
        # Откуда взялась цена входа: рынок или цена самой сделки лидера.
        srcs = [(r.get("scenarios") or {}).get("E2", {}).get("entry_src")
                for r in rs if r.get("sim_E2") is not None]
        srcs = [x for x in srcs if x]
        row["n_entry_market"] = sum(1 for x in srcs if x == "market")
        row["n_entry_leader_price"] = sum(1 for x in srcs if x == "leader_price")
        row["доля_leader_price"] = round(row["n_entry_leader_price"] / len(srcs), 4) if srcs else None
        # Медиана E2 ТОЛЬКО по рыночным входам. Откат на цену лидера берёт
        # цену самой сделки лидера -- вход без проскальзывания и без чужой
        # очереди, поэтому он систематически завышает результат (по всему
        # прогону: +3.24% на откате против -0.31% на рынке). Отбирать
        # источники по смешанной медиане значит отбирать по доле отката.
        e2_market = [r["sim_E2"] for r in rs
                      if r.get("sim_E2") is not None
                      and ((r.get("scenarios") or {}).get("E2", {}) or {}).get("entry_src") == "market"]
        row["n_E2_рыночных"] = len(e2_market)
        row["median_E2_рыночная"] = round(statistics.median(e2_market), 4) if e2_market else None
        row["доля_в_плюс_E2_рыночная"] = (round(sum(1 for x in e2_market if x > 0) / len(e2_market), 4)
                                            if e2_market else None)
        # Фактическая задержка выхода -- после расширения окна это уже не
        # константа 33-37с, и её надо видеть по кошельку.
        dl = [r["exit_delay_s"] for r in rs if r.get("exit_delay_s") is not None]
        row["медиана_задержки_выхода_с"] = round(statistics.median(dl), 2) if dl else None
        sl = slow.get(addr) or []
        row["n_медленный_выход"] = len(sl)
        sl_v = [r["sim_E2"] for r in sl if r.get("sim_E2") is not None]
        row["median_E2_медленных"] = round(statistics.median(sl_v), 4) if sl_v else None
        out.append(row)
    out.sort(key=lambda r: (r["median_E2"] is None, -(r["median_E2"] or 0)))
    return out


def overall(rows: list[dict], label: str, min_leg_sol: float = MIN_LEG_SOL) -> dict:
    n_all = sum(1 for r in rows if r.get("sim_E2") is not None)
    dusty = [r for r in rows if r.get("sim_E2") is not None and not dust_ok(r, min_leg_sol)]
    slow = [r for r in rows if r.get("sim_E2") is not None and dust_ok(r, min_leg_sol)
            and not exit_ok(r)]
    rows = [r for r in rows if r.get("sim_E2") is None or row_usable(r, min_leg_sol)]
    o = {"label": label, "мин_нога_SOL": min_leg_sol,
         "отброшено_пылевых": len(dusty),
         "медленный_выход": {
             "порог_с": EXIT_SLOW_S,
             "n_исключено": len(slow),
             "медиана_E2_у_медленных": round(statistics.median(
                 [r["sim_E2"] for r in slow]), 4) if slow else None,
             "медиана_задержки_у_медленных_с": round(statistics.median(
                 [r["exit_delay_s"] for r in slow]), 2) if slow else None,
             "пояснение": ("выход нашёлся, но позже, чем мы реально держим позицию "
                            "(медиана held_seconds 35с). Это другой горизонт, поэтому в основную "
                            "медиану такие строки не идут, но и не выбрасываются")},
         "всего_с_sim_E2_до_фильтров": n_all}
    for k in ("E0", "E0_s1", "E0_loose", "E1", "E2"):
        v = [r[f"sim_{k}"] for r in rows if r.get(f"sim_{k}") is not None]
        o[f"n_{k}"] = len(v)
        o[f"median_{k}"] = round(statistics.median(v), 4) if v else None
        o[f"p25_{k}"] = round(pct(v, 0.25), 4) if v else None
        o[f"p75_{k}"] = round(pct(v, 0.75), 4) if v else None
        o[f"share_above_{COST_THRESHOLD_PCT}_{k}"] = share_above(v, COST_THRESHOLD_PCT)
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

    # РАЗБИВКА ПО ИСТОЧНИКУ ЦЕНЫ ВХОДА. Ровно то, ради чего вводился
    # entry_src: увидеть, насколько метод был смещён, когда молча
    # выбрасывал строки без чужих сделок в окне. Медианы приводятся
    # порознь и вместе -- если они сильно расходятся, прежние цифры
    # были посчитаны по неслучайной подвыборке.
    def _med(v):
        return round(statistics.median(v), 4) if v else None

    by_src: dict = {}
    for k in ("E0", "E1", "E2"):
        vm = [r[f"sim_{k}"] for r in rows if r.get(f"sim_{k}") is not None
              and ((r.get("scenarios") or {}).get(k, {}) or {}).get("entry_src") == "market"]
        vl = [r[f"sim_{k}"] for r in rows if r.get(f"sim_{k}") is not None
              and ((r.get("scenarios") or {}).get(k, {}) or {}).get("entry_src") == "leader_price"]
        by_src[k] = {"n_market": len(vm), "медиана_market": _med(vm),
                     "n_leader_price": len(vl), "медиана_leader_price": _med(vl),
                     "n_вместе": len(vm) + len(vl), "медиана_вместе": _med(vm + vl),
                     "доля_leader_price": round(len(vl) / (len(vm) + len(vl)), 4)
                     if (vm or vl) else None}
    o["по_источнику_входа"] = by_src

    dl = [r["exit_delay_s"] for r in rows if r.get("exit_delay_s") is not None]
    if dl:
        o["задержка_выхода_распределение"] = {
            "n": len(dl), "мин": min(dl), "p25": pct(dl, 0.25), "медиана": statistics.median(dl),
            "p75": pct(dl, 0.75), "p95": pct(dl, 0.95), "макс": max(dl)}
    o["n_упёрлись_в_потолок_скана"] = sum(1 for r in rows if r.get("exit_scan_hit_cap"))
    return o


def truncation_report(rows: list[dict], budget_exhausted: bool) -> dict:
    """Сколько строк ушло в no_exit и сколько из них может быть обрывом
    по бюджету. Для прогонов, где пометки budget_truncated ещё не было,
    даётся ОЦЕНКА по косвенным признакам, и она названа оценкой.

    Признак обрыва в старых данных: строка одновременно упёрлась в
    потолок скана И часть блоков не отдалась. Настоящий глубокий скан,
    ничего не нашедший, доходит до потолка с полными блоками; после
    исчерпания бюджета блоки перестают отдаваться все разом."""
    n = len(rows)
    no_exit = [r for r in rows if r.get("status") == "no_exit"]
    marked = [r for r in rows if r.get("budget_truncated")]
    marked_status = [r for r in rows if r.get("status") == "оборвано_бюджетом"]
    suspect = [r for r in no_exit if r.get("exit_scan_hit_cap") and r.get("exit_incomplete")]
    return {
        "симуляций_всего": n,
        "no_exit": len(no_exit),
        "оборвано_бюджетом_помечено": len(marked_status),
        "помечено_как_затронутые_обрывом_всего": len(marked),
        "бюджет_исчерпан": budget_exhausted,
        "оценка_обрыва_в_no_exit": len(suspect) if not marked_status else None,
        "как_считана_оценка": (
            "строка упёрлась в потолок скана И часть блоков не отдалась -- признак того, что "
            "блоки перестали приходить, а не того, что сделок не было. Применяется только к "
            "прогонам без явной пометки budget_truncated"),
        "честно": ("нижняя оценка: строку, оборвавшуюся до потолка, этот признак не поймает. "
                    "Прогоны с явной пометкой сомнений не оставляют"),
    }


def passing_candidates(wallets: list[dict], min_e2_market: float = 5.0, min_n: int = 4,
                        min_crowd: float = 3.0, min_share_pos: float = 0.5,
                        max_leader_price: float = 0.30) -> dict:
    """Фильтр владельца (уточнён 23.09): медиана E2 ТОЛЬКО по рыночным
    входам >= +5%, доля leader_price <= 30%, n >= 4, толпа >= 3, больше
    половины симуляций в плюс.

    Почему по рыночным: откат на цену лидера берёт цену самой сделки
    лидера, то есть вход без проскальзывания и без чужой очереди, и
    систематически завышает результат. Смешанная медиана отбирала бы
    источники по доле отката, а не по силе сигнала.

    Строго "больше половины" -- ровно 0.5 не проходит, иначе это "не
    меньше половины". Доля leader_price -- "не больше 30%", 0.30 проходит.

    Кошелёк, у которого нет НИ ОДНОГО рыночного входа, фильтр не
    проходит: подтверждать его нечем. Это не отказ в пользу отрицательного
    ответа, а отсутствие доказательства -- и так и помечается отдельно.

    Отдельно возвращается, сколько кошельков отсеял КАЖДЫЙ критерий по
    отдельности: иначе нельзя понять, узок ли фильтр или данных мало."""
    cand = [w for w in wallets if w.get("status") == "кандидат"]
    def ok_e2(w):
        return w.get("median_E2_рыночная") is not None and w["median_E2_рыночная"] >= min_e2_market
    def ok_n(w):
        return (w.get("n_sim") or 0) >= min_n
    def ok_crowd(w):
        return w.get("crowd_2") is not None and w["crowd_2"] >= min_crowd
    def ok_pos(w):
        return w.get("доля_в_плюс_E2") is not None and w["доля_в_плюс_E2"] > min_share_pos
    def ok_lp(w):
        return w.get("доля_leader_price") is not None and w["доля_leader_price"] <= max_leader_price
    passed = [w for w in cand if ok_e2(w) and ok_n(w) and ok_crowd(w) and ok_pos(w) and ok_lp(w)]
    passed.sort(key=lambda w: -(w.get("median_E2_рыночная") or 0))
    return {
        "порог": {"median_E2_рыночная_не_меньше": min_e2_market, "n_sim_не_меньше": min_n,
                   "crowd_2_не_меньше": min_crowd, "доля_в_плюс_строго_больше": min_share_pos,
                   "доля_leader_price_не_больше": max_leader_price},
        "кандидатов_всего": len(cand),
        "кандидатов_без_единого_рыночного_входа": sum(
            1 for w in cand if not (w.get("n_E2_рыночных") or 0)),
        "отсев_по_каждому_критерию_поодиночке": {
            "не_прошли_E2_рыночную": sum(1 for w in cand if not ok_e2(w)),
            "не_прошли_n": sum(1 for w in cand if not ok_n(w)),
            "не_прошли_толпу": sum(1 for w in cand if not ok_crowd(w)),
            "не_прошли_долю_в_плюс": sum(1 for w in cand if not ok_pos(w)),
            "не_прошли_долю_leader_price": sum(1 for w in cand if not ok_lp(w))},
        "прошли": [{"address": w["address"], "name": w.get("name"), "crowd_2": w.get("crowd_2"),
                     "n_sim": w.get("n_sim"),
                     "median_E2_рыночная": w.get("median_E2_рыночная"),
                     "n_E2_рыночных": w.get("n_E2_рыночных"),
                     "median_E2": w.get("median_E2"),
                     "доля_в_плюс_E2": w.get("доля_в_плюс_E2"),
                     "доля_leader_price": w.get("доля_leader_price"),
                     "медиана_задержки_выхода_с": w.get("медиана_задержки_выхода_с"),
                     "окно_скана": w.get("окно_скана")} for w in passed],
    }


def self_test_method() -> None:
    """Границы правки метода -- без сети и без ключей. Проверяется ровно
    то, что меняет цифры: откат на цену лидера, честный отказ, когда
    подставлять нечего, и глубокий добор выхода до T+120с."""
    def T(owner, idx, price, buy=True, sig=None, size=1.0):
        return {"owner": owner, "is_buy": buy, "index": idx,
                "signature": sig or f"s{owner}{idx}",
                "price_sol_per_token": price, "sol_size": size}

    class FakeST:
        def __init__(self, blocks):
            self.blocks = blocks

        def get(self, slot, mint):
            return self.blocks.get(slot)

    class FakeClock:
        def slot_at(self, s0, t0, target, max_probe=6):
            return s0 + (target - t0) * 4      # 0.25с на слот

    checks: list[tuple[str, bool, str]] = []

    def chk(name, cond, got=""):
        checks.append((name, bool(cond), got))

    LEAD, MINT, SLOT, SIG = "L", "M", 1000, "sigL"
    base = {SLOT: {"block_time": 100, "trades": [T(LEAD, 5, 2.0, sig=SIG)]},
            1132: {"block_time": 133, "trades": [T("X", 1, 3.0)]}}

    r = simulate(None, FakeST(dict(base)), FakeClock(), LEAD, MINT, SLOT, SIG, None, 100)
    sc = r["scenarios"]
    chk("чужих сделок нет -> вход по цене лидера", sc["E2"]["entry_src"] == "leader_price")
    chk("цена входа равна цене лидера", sc["E2"]["entry_price"] == 2.0)
    chk("sim_E2 считается (раньше был no_entry)", abs((r.get("sim_E2") or 0) - 50.0) < 1e-6,
        str(r.get("sim_E2")))
    chk("выход 33с не помечен медленным", r.get("exit_slow") is False)

    b2 = dict(base)
    b2[SLOT + 2] = {"block_time": 100, "trades": [T("Y", 1, 2.5)]}
    r2 = simulate(None, FakeST(b2), FakeClock(), LEAD, MINT, SLOT, SIG, None, 100)
    chk("чужая сделка есть -> источник рынок", r2["scenarios"]["E2"]["entry_src"] == "market")
    chk("вход берётся рыночный, не лидерский", r2["scenarios"]["E2"]["entry_price"] == 2.5)
    chk("E0 остаётся на цене лидера (в слоте S чужих нет)",
        r2["scenarios"]["E0"]["entry_src"] == "leader_price")

    b3 = {SLOT: {"block_time": 100, "trades": [T(LEAD, 5, None, sig=SIG)]},
          1132: {"block_time": 133, "trades": [T("X", 1, 3.0)]}}
    r3 = simulate(None, FakeST(b3), FakeClock(), LEAD, MINT, SLOT, SIG, None, 100)
    chk("цены лидера нет -> честный no_entry", r3["scenarios"]["E2"].get("no_entry") is True)
    chk("и sim_E2 не выдумывается", r3.get("sim_E2") is None)

    # Выход ВНУТРИ окна, но дальше дешёвого прохода: глубокий добор обязан
    # его найти. 28 блоков по 0.25с = T+40с.
    b4 = {SLOT: {"block_time": 100, "trades": [T(LEAD, 5, 2.0, sig=SIG)]}}
    for i in range(200):
        b4[1132 + i] = {"block_time": 133 + i // 4, "trades": []}
    b4[1132 + 28] = {"block_time": 140, "trades": [T("X", 1, 4.0)]}
    r4 = simulate(None, FakeST(b4), FakeClock(), LEAD, MINT, SLOT, SIG, None, 100)
    chk("выход за пределами дешёвого прохода найден", r4.get("exit_delay_s") == 40,
        str(r4.get("exit_delay_s")))
    chk("глубокий проход реально был", (r4.get("exit_scan_blocks") or 0) > EXIT_MAX_BLOCKS,
        str(r4.get("exit_scan_blocks")))
    chk("в потолок скана не упёрлись", r4.get("exit_scan_hit_cap") is False)
    chk("выход внутри окна не помечен медленным", r4.get("exit_slow") is False)

    # Выход ПОЗЖЕ окна, которым мы торгуем: теперь честный no_exit, а не
    # строка, которую всё равно выбросил бы фильтр exit_slow. Ровно на
    # этом и экономится время прогона.
    b4b = {SLOT: {"block_time": 100, "trades": [T(LEAD, 5, 2.0, sig=SIG)]}}
    for i in range(300):
        b4b[1132 + i] = {"block_time": 133 + i // 4, "trades": []}
    b4b[1132 + 120] = {"block_time": 163, "trades": [T("X", 1, 4.0)]}
    r4b = simulate(None, FakeST(b4b), FakeClock(), LEAD, MINT, SLOT, SIG, None, 100)
    chk("выход позже T+45с не берётся", r4b["status"] == "no_exit", r4b["status"])
    chk("и скан на него не тратится", (r4b.get("exit_scan_blocks") or 0) <= EXIT_DEEP_MAX_BLOCKS,
        str(r4b.get("exit_scan_blocks")))

    b5 = {SLOT: {"block_time": 100, "trades": [T(LEAD, 5, 2.0, sig=SIG)]}}
    for i in range(500):
        b5[1132 + i] = {"block_time": 133 + i // 4, "trades": []}
    r5 = simulate(None, FakeST(b5), FakeClock(), LEAD, MINT, SLOT, SIG, None, 100)
    chk("сделок нет вовсе -> no_exit", r5["status"] == "no_exit")
    chk("скан остановлен временем окна, а не потолком", r5.get("exit_scan_hit_cap") is False)
    chk("скан не вышел за окно T+120с",
        (r5.get("exit_scan_blocks") or 0) <= 4 * (EXIT_TO_S - EXIT_FROM_S) + 8,
        str(r5.get("exit_scan_blocks")))

    slow_row = {"scenarios": {"E2": {"entry_sol_size": 1.0}}, "exit_sol_size": 1.0,
                "exit_slow": True, "sim_E2": 10.0}
    fast_row = {**slow_row, "exit_slow": False}
    chk("медленный выход не идёт в основную сводку", not row_usable(slow_row, 0.05))
    chk("быстрый идёт", row_usable(fast_row, 0.05))

    pc = passing_candidates([
        {"status": "кандидат", "address": "A", "median_E2": 6.0, "n_sim": 5,
         "crowd_2": 4, "доля_в_плюс_E2": 0.6, "median_E2_рыночная": 6.0,
         "n_E2_рыночных": 5, "доля_leader_price": 0.0},
        {"status": "кандидат", "address": "B", "median_E2": 6.0, "n_sim": 5,
         "crowd_2": 4, "доля_в_плюс_E2": 0.5, "median_E2_рыночная": 6.0,
         "n_E2_рыночных": 5, "доля_leader_price": 0.0},
        {"status": "в задаче", "address": "C", "median_E2": 99.0, "n_sim": 9,
         "crowd_2": 9, "доля_в_плюс_E2": 1.0, "median_E2_рыночная": 99.0,
         "n_E2_рыночных": 9, "доля_leader_price": 0.0},
    ])
    chk("фильтр пропускает подходящего кандидата",
        [x["address"] for x in pc["прошли"]] == ["A"], str(pc["прошли"]))
    chk("ровно половина в плюс НЕ проходит", all(x["address"] != "B" for x in pc["прошли"]))
    chk("кошельки в задаче в список кандидатов не попадают", pc["кандидатов_всего"] == 2)

    # --- возобновление после обрыва ---
    import tempfile  # noqa: PLC0415
    tmp = Path(tempfile.mkdtemp())
    cp = tmp / "cache.json"
    c = SimCache(cp, 3)
    c.put("sigA", {"status": "ok", "sim_E2": 1.0})
    chk("чекпойнт сохраняет записи", c.save() == 1)
    chk("в файле записана версия метода",
        json.loads(cp.read_text()).get("версия_метода") == 3)
    chk("возобновление подхватывает готовое", SimCache(cp, 3).loaded == 1)
    chk("ЧУЖАЯ версия метода отбрасывается, а не смешивается", SimCache(cp, 4).loaded == 0)
    chk("и отброшенное посчитано", SimCache(cp, 4).dropped_other_version == 1)
    c2 = SimCache(cp, 3, ignore_existing=True)
    chk("--no-cache не берёт готовое", c2.loaded == 0)
    c2.put("sigB", {"status": "ok"})
    c2.save()
    chk("--no-cache при этом ПИШЕТ (обрыв больше не стоит всего прогона)",
        SimCache(cp, 3).loaded == 1)
    chk("чекпойнт по времени: сразу не нужен", not SimCache(cp, 3).due(900))
    chk("чекпойнт по времени: при нулевом пороге нужен", SimCache(cp, 3).due(0))
    bad_file = tmp / "bad.json"
    bad_file.write_text("{не json")
    chk("битый кэш не валит запуск", SimCache(bad_file, 3).loaded == 0)
    chk("временный файл после записи убран", not cp.with_suffix(".tmp").exists())

    # --- пометка обрыва по бюджету ---
    tr = truncation_report([
        {"status": "ok", "sim_E2": 1.0},
        {"status": "no_exit", "exit_scan_hit_cap": True, "exit_incomplete": True},
        {"status": "no_exit", "exit_scan_hit_cap": True, "exit_incomplete": False},
        {"status": "no_exit"},
    ], budget_exhausted=True)
    chk("no_exit посчитаны", tr["no_exit"] == 3, str(tr["no_exit"]))
    chk("оценка обрыва ловит потолок+неполные блоки", tr["оценка_обрыва_в_no_exit"] == 1,
        str(tr["оценка_обрыва_в_no_exit"]))
    tr2 = truncation_report([{"status": "оборвано_бюджетом", "budget_truncated": True},
                              {"status": "ok", "sim_E2": 1.0, "budget_truncated": True}],
                             budget_exhausted=True)
    chk("при явной пометке оценка не подменяет факт", tr2["оценка_обрыва_в_no_exit"] is None)
    chk("помечено оборванными ровно незавершённое", tr2["оборвано_бюджетом_помечено"] == 1)
    chk("затронутых обрывом считаем шире", tr2["помечено_как_затронутые_обрывом_всего"] == 2)
    rec_s = {"E0": 1.0, "E1": 2.0, "E2": 2.0, "exit": 3.0}
    c0, m0, p0 = compare_prices(rec_s, {"helius": {}, "публичный_узел": {}})
    chk("пустой ответ провайдера не даёт сверенных пар", (c0, m0, p0) == (0, 0, {}), f"{c0}/{m0}")
    c1, m1, p1 = compare_prices(rec_s, {"helius": dict(rec_s), "публичный_узел": dict(rec_s)})
    chk("полное совпадение: 8 пар, 0 расхождений", (c1, m1) == (8, 0), f"{c1}/{m1}")
    chk("вклад провайдеров считается врозь", p1 == {"helius": 4, "публичный_узел": 4}, str(p1))
    c2, m2, _ = compare_prices(rec_s, {"helius": {"exit": 3.5}})
    chk("расхождение ловится", (c2, m2) == (1, 1), f"{c2}/{m2}")
    c3, m3, _ = compare_prices({"E0": None}, {"helius": {"E0": 1.0}})
    chk("None в записанном не сверяется", (c3, m3) == (0, 0), f"{c3}/{m3}")
    c4, m4, p4 = compare_prices(rec_s, {"helius": {}, "публичный_узел": dict(rec_s)})
    chk("односторонняя сверка видна по разбивке", p4 == {"публичный_узел": 4} and c4 == 4, str(p4))
    # Фильтр кандидатов: отбор идёт по РЫНОЧНОЙ медиане, не по смешанной.
    W_BASE = {"status": "кандидат", "n_sim": 5, "crowd_2": 5.0, "доля_в_плюс_E2": 0.8,
              "доля_leader_price": 0.0, "median_E2": 20.0,
              "median_E2_рыночная": 9.0, "n_E2_рыночных": 5, "address": "A"}
    def W(**kw):
        w = dict(W_BASE); w.update(kw); return w
    r = passing_candidates([W()])
    chk("чистый рыночный кандидат проходит", len(r["прошли"]) == 1, str(len(r["прошли"])))
    r = passing_candidates([W(median_E2=30.0, median_E2_рыночная=1.0)])
    chk("высокая смешанная медиана не спасает при слабой рыночной",
        len(r["прошли"]) == 0, str(r["прошли"]))
    r = passing_candidates([W(доля_leader_price=0.30)])
    chk("ровно 30% отката проходит", len(r["прошли"]) == 1)
    r = passing_candidates([W(доля_leader_price=0.31)])
    chk("31% отката уже нет", len(r["прошли"]) == 0)
    r = passing_candidates([W(median_E2_рыночная=None, n_E2_рыночных=0)])
    chk("без рыночных входов не проходит", len(r["прошли"]) == 0)
    chk("и считается отдельно", r["кандидатов_без_единого_рыночного_входа"] == 1)
    r = passing_candidates([W(доля_в_плюс_E2=0.5)])
    chk("ровно половина в плюс не проходит", len(r["прошли"]) == 0)

    # median_E2_рыночная считается только по строкам с entry_src=market.
    def RW(e2, src):
        return {"leader": "L", "sim_E2": e2, "sim_E0": e2, "sim_E1": e2,
                "exit_delay_s": 33, "exit_sol_size": 1.0, "exit_slow": False,
                "scenarios": {"E2": {"entry_src": src, "entry_sol_size": 1.0}},
                "status": "ok"}
    pw = per_wallet([RW(10.0, "market"), RW(50.0, "leader_price"), RW(20.0, "market")], {})
    chk("рыночная медиана игнорирует откат", pw[0]["median_E2_рыночная"] == 15.0,
        str(pw[0]["median_E2_рыночная"]))
    chk("смешанная медиана осталась прежней", pw[0]["median_E2"] == 20.0,
        str(pw[0]["median_E2"]))
    chk("число рыночных строк посчитано", pw[0]["n_E2_рыночных"] == 2)

    chk("окно выхода равно горизонту удержания", EXIT_TO_S == EXIT_SLOW_S == 45,
        f"{EXIT_TO_S}/{EXIT_SLOW_S}")
    chk("потолок добора соответствует окну", EXIT_DEEP_MAX_BLOCKS == 60,
        str(EXIT_DEEP_MAX_BLOCKS))

    bad = 0
    for name, good, got in checks:
        print(f"  [{'ok  ' if good else 'СБОЙ'}] {name}" + (f"  -> {got}" if got and not good else ""))
        bad += (not good)
    print(f"самопроверка метода: {len(checks) - bad}/{len(checks)} пройдено")
    if bad:
        raise SystemExit(f"самопроверка не пройдена: {bad} из {len(checks)}")


# ---------- самопроверка ----------

def compare_prices(recorded: dict, by_provider: dict) -> tuple[int, int, dict]:
    """Сколько пар цен реально сверено, сколько разошлось и КЕМ сверено.

    Отдельной функцией -- чтобы правило "нечего сверить != совпало" проверялось
    самотестом без сети. Пара считается сверенной, только когда ОБЕ стороны
    дали число; None у любой стороны -- это отсутствие проверки, не успех.

    Разбивка по провайдерам нужна, чтобы "сверено у двух провайдеров" не
    прикрывало случай, когда один из них весь прогон отвечал 429 и вклада
    не дал: 20 пар от одного узла и 2 от другого -- это не то же самое,
    что 11 и 11.
    """
    compared = mismatched = 0
    per: dict[str, int] = {}
    for k in ("E0", "E1", "E2", "exit"):
        want = recorded.get(k)
        for prov, prices in by_provider.items():
            got = (prices or {}).get(k)
            if want is None or got is None:
                continue
            compared += 1
            per[prov] = per.get(prov, 0) + 1
            if abs(got - want) > max(abs(want) * 1e-9, 1e-18):
                mismatched += 1
    return compared, mismatched, per


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
        compared, mismatched, per_prov = compare_prices(
            rec, {prov: item.get(prov) for prov in ("helius", "публичный_узел")})
        ok = mismatched == 0
        item["сверено_пар"] = compared
        item["сверено_пар_по_провайдерам"] = per_prov
        item["расхождений"] = mismatched
        # ВАЖНО: если сверять было нечего (провайдер не ответил, бюджет истёк),
        # это НЕ "совпало". Пустое сравнение с пустым -- не проверка.
        item["совпало"] = ok if compared else None
        item["не_проверено"] = compared == 0
        if compared == 0:
            item.setdefault("почему_не_проверено",
                             item.get("helius_ошибка") or item.get("публичный_узел_ошибка")
                             or "оба провайдера вернули пусто")
        checks.append(item)
    verified = [c for c in checks if c["сверено_пар"]]
    if not verified:
        return {"ok": None, "проверено_строк": 0, "всего_строк": len(checks),
                "note": "самопроверка НЕ выполнена: ни одной пары цен не удалось сверить",
                "checks": checks}
    all_ok = all(c["совпало"] for c in verified)
    per_total: dict[str, int] = {}
    for c in verified:
        for prov, n_pairs in (c.get("сверено_пар_по_провайдерам") or {}).items():
            per_total[prov] = per_total.get(prov, 0) + n_pairs
    res = {"ok": all_ok, "проверено_строк": len(verified), "всего_строк": len(checks),
           "сверено_пар_всего": sum(c["сверено_пар"] for c in verified),
           "сверено_пар_по_провайдерам": per_total, "checks": checks}
    # Один ответивший провайдер -- это всё ещё сверка (записанное против
    # независимого узла), но НЕ "у двух провайдеров". Говорим это прямо.
    отвечали = [p for p, n_pairs in per_total.items() if n_pairs]
    if len(отвечали) < 2:
        res["оговорка"] = ("сверка односторонняя: вклад дал только " + ", ".join(отвечали)
                            + " -- второй провайдер не ответил")
    return res


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
    """Кэш посчитанных симуляций -- он же точка возобновления.

    Формат файла: {"версия_метода": N, "сохранено_utc": ..., "rows": {...}}.
    Записи другой версии метода при загрузке ОТБРАСЫВАЮТСЯ: иначе после
    правки метода возобновление тихо смешало бы старые и новые строки.
    Старый плоский формат (без версии) считается версией 1.

    ignore_existing -- пересчитать всё заново, НО ПРОДОЛЖАЯ ПИСАТЬ: это и
    есть разница с прежним --no-cache, который не писал ничего, и обрыв
    означал потерю всего прогона."""

    def __init__(self, path: Path, method_version: int = METHOD_VERSION,
                  ignore_existing: bool = False) -> None:
        self.path = path
        self.version = method_version
        self._lock = threading.Lock()
        self._d: dict = {}
        self.loaded = 0
        self.dropped_other_version = 0
        if path.exists() and not ignore_existing:
            try:
                raw = json.loads(path.read_text())
            except (ValueError, OSError):
                raw = {}
            if isinstance(raw, dict) and "rows" in raw:
                if raw.get("версия_метода") == method_version:
                    self._d = raw["rows"] or {}
                else:
                    self.dropped_other_version = len(raw.get("rows") or {})
            elif isinstance(raw, dict):
                # плоский формат = версия 1
                if method_version == 1:
                    self._d = raw
                else:
                    self.dropped_other_version = len(raw)
            self.loaded = len(self._d)
        self.hits = 0
        self.saves = 0
        self._last_save = time.monotonic()

    def get(self, sig: str) -> dict | None:
        v = self._d.get(sig)
        if v is not None:
            with self._lock:
                self.hits += 1
        return v

    def put(self, sig: str, row: dict) -> None:
        with self._lock:
            self._d[sig] = row

    def due(self, every_s: float = CHECKPOINT_S) -> bool:
        return (time.monotonic() - self._last_save) >= every_s

    def save(self) -> int:
        """Атомарно: сначала во временный файл, потом подмена. Обрыв
        посреди записи не оставит битый кэш, из которого потом нечего
        возобновлять."""
        with self._lock:
            payload = {"версия_метода": self.version, "сохранено_utc": now_utc(),
                        "n": len(self._d), "rows": self._d}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str))
            os.replace(tmp, self.path)
            self.saves += 1
            self._last_save = time.monotonic()
            return len(self._d)


def push_checkpoint(paths: list[Path], note: str) -> str:
    """Отправить чекпойнт на удалённую ветку ПРЯМО ИЗ ПРОГОНА.

    Зачем так. Кэш, сохранённый только на диск раннера, при гибели
    раннера исчезает вместе с ним -- возобновлять будет не из чего:
    шаг коммита в workflow выполняется лишь после завершения расчёта,
    которого в этом случае не будет. Поэтому чекпойнт уходит в git
    сразу.

    Любая неудача здесь НЕ должна ронять прогон: расчёт важнее
    сохранения, и следующая попытка будет через четверть часа."""
    import subprocess  # noqa: PLC0415 -- нужен только здесь

    def sh(*args: str, check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(args, cwd=str(REPO_ROOT), capture_output=True,
                               text=True, timeout=180, check=check)

    try:
        sh("git", "config", "user.name", "github-actions[bot]")
        sh("git", "config", "user.email", "github-actions[bot]@users.noreply.github.com")
        existing = [str(x.relative_to(REPO_ROOT)) for x in paths if x.exists()]
        if not existing:
            return "нечего сохранять"
        for f in existing:
            sh("git", "add", f)
        if sh("git", "diff", "--cached", "--quiet").returncode == 0:
            return "изменений нет"
        sh("git", "commit", "-m", f"Ретро-сигнал: промежуточное сохранение -- {note} [automated]")
        for attempt in range(3):
            r = sh("git", "push", "origin", "HEAD:claude/nifty-sagan-r0polg")
            if r.returncode == 0:
                return f"выгружено ({', '.join(existing)})"
            sh("git", "pull", "--rebase", "origin", "claude/nifty-sagan-r0polg")
        return "push отклонён 3 раза -- чекпойнт остался только на диске раннера"
    except Exception as exc:  # noqa: BLE001
        return f"ошибка выгрузки ({type(exc).__name__}) -- чекпойнт остался на диске"


def write_progress(path: Path, label: str, done: int, total: int, from_cache: int,
                    rpc: Rpc, st: SlotTrades, started: float) -> None:
    """Небольшой файл состояния: по нему видно, где прогон, пока логи
    задания ещё недоступны (GitHub отдаёт их только по завершении)."""
    try:
        path.write_text(json.dumps({
            "обновлено_utc": now_utc(), "этап": label,
            "готово": done, "всего_считать": total, "взято_из_кэша": from_cache,
            "доля": round(done / total, 4) if total else None,
            "версия_метода": METHOD_VERSION,
            "минут_идёт": round((time.monotonic() - started) / 60, 1),
            "блоков": st.fetched, "блоков_из_кэша": st.hits,
            "rpc_вызовов": rpc.calls, "ретраев": rpc.retries,
            "бюджет_исчерпан": rpc.expired(),
        }, ensure_ascii=False, indent=1))
    except OSError:
        pass


def run(rpc: Rpc, st: SlotTrades, clock: SlotClock, items: list[dict], workers: int,
        label: str, cache: SimCache | None, checkpoint_s: float = CHECKPOINT_S,
        push: bool = False, started: float | None = None) -> list[dict]:
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
    n_from_cache = len(rows)
    started = started if started is not None else time.monotonic()
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
            # ОБРЫВ ПО БЮДЖЕТУ ПОМЕЧАЕТСЯ ЯВНО. После исчерпания бюджета
            # Rpc.call бросает исключение сразу, блоки возвращаются как
            # "не отдались", и симуляция выглядит как no_exit -- то есть
            # обрыв неотличим от честного "сделок не было". Различить
            # построчно нечем, поэтому помечается ВСЁ, обработанное после
            # момента исчерпания: лучше пометить лишнее, чем выдать обрыв
            # за факт.
            if rpc.expired():
                row["budget_truncated"] = True
                if row.get("sim_E2") is None:
                    # Незавершённая строка: её no_exit/ошибка могут быть
                    # следствием обрыва, поэтому статус меняется.
                    row["статус_до_обрыва"] = row.get("status")
                    row["status"] = "оборвано_бюджетом"
                    row["note"] = ("обработана после исчерпания бюджета времени -- "
                                    "отсутствие входа/выхода может быть следствием обрыва, "
                                    "а не отсутствием сделок")
                # Строка С посчитанным sim_E2 завершена корректно: вход и
                # выход найдены до обрыва. Её не выбрасываем -- только
                # помечаем, иначе потеряли бы годные данные.
            # В кэш кладём только доведённые до конца строки: иначе обрыв
            # закрепился бы в кэше и повторный прогон его унаследовал.
            if (cache and not row.get("budget_truncated")
                    and row.get("status") in ("ok", "частично", "no_exit", "no_leader_tx")):
                cache.put(it["signature"], row)
            row.update({k: v for k, v in it.items()
                        if k in ("signal_pct", "task_name", "buyers_between", "spend_sol_equiv")})
            rows.append(row)
            done += 1
            if done % 20 == 0 or done == len(todo):
                log(f"{label}: {done}/{len(todo)} | блоков={st.fetched} из кэша={st.hits} "
                    f"пропущено={st.skipped} | getBlockTime={clock.calls} | RPC={rpc.calls} "
                    f"ретраев={rpc.retries}")
                write_progress(PROGRESS_PATH, label, done, len(todo), n_from_cache,
                                rpc, st, started)
            # ЧЕКПОЙНТ ПО ВРЕМЕНИ, а не только по числу симуляций: на
            # глубоком доборе выхода одна симуляция может идти минутами,
            # и "каждые 20 штук" превращалось бы в час без сохранения.
            if cache and (cache.due(checkpoint_s) or done == len(todo)):
                n = cache.save()
                msg = f"{label}: чекпойнт -- в кэше {n} симуляций"
                if push:
                    msg += "; " + push_checkpoint([cache.path, PROGRESS_PATH],
                                                   f"{label} {done}/{len(todo)}")
                log(msg)
    if cache:
        cache.save()
        if push:
            log(f"{label}: финальный чекпойнт -- {push_checkpoint([cache.path, PROGRESS_PATH], 'финал')}")
    return rows


def reaggregate(min_leg_sol: float, mode: str) -> None:
    """Пересчитать сводку из уже посчитанного файла, не трогая сеть.
    Файл выбирается по режиму -- тому же, в который прогон и писал."""
    path = out_path_for(mode)
    if not path.exists():
        raise SystemExit(f"файла {path} нет -- пересчитывать нечего (режим {mode})")
    out = json.loads(path.read_text())
    rows = out["sims"]
    # Мета берём из ТЕХ ЖЕ сканов, что использовал сам прогон (их список
    # он записал в параметры_отбора): иначе при пересчёте у кошельков из
    # дополнительного скана пропадало окно_скана и строка выглядела бы
    # посчитанной по 72ч, хотя считалась по расширенному окну.
    extra_raw = (out.get("параметры_отбора") or {}).get("дополнительные_сканы") or ""
    extra = [Path(x.strip()) if Path(x.strip()).is_absolute() else REPO_ROOT / x.strip()
             for x in str(extra_raw).split(",") if x.strip()]
    meta: dict[str, dict] = {}
    # Имя переменной цикла НЕ path: оно затирало путь выгрузки, и в конце
    # функции результат уходил в файл последнего скана вместо своего.
    for scan_path, tag in ([(CROWD_PATH, "72ч")] + [(x, "расширенный") for x in extra]):
        if not scan_path.exists():
            log(f"пересчёт: скана нет, пропускаю: {scan_path}")
            continue
        for w in json.loads(scan_path.read_text()).get("wallets") or []:
            if not w.get("address"):
                continue
            meta[w["address"]] = {"name": w.get("name"), "status": w.get("status"),
                                   "crowd_2": w.get("crowd_2_median"), "level": w.get("level"),
                                   "n_buys": w.get("n_buys"), "окно_скана": tag}
    out["config"]["min_leg_sol"] = min_leg_sol
    out["overall"] = overall(rows, "все кошельки", min_leg_sol)
    out["overall_pilot"] = overall([r for r in rows if r["leader"] == PILOT], "пилот", min_leg_sol)
    if out.get("mode") == "full":
        out["wallets"] = per_wallet(rows, meta, min_leg_sol)
        # Фильтр кандидатов пересчитывается вместе с таблицей: иначе после
        # смены порогов в файле оставался список, посчитанный по старым.
        out["кандидаты_прошедшие_фильтр"] = passing_candidates(out["wallets"])
    if out.get("calibration"):
        out["calibration"] = calibration([r for r in rows if row_usable(r, min_leg_sol)
                                           or r.get("sim_E2") is None])
    out["ЧЕСТНЫЕ_ОГОВОРКИ"].append(
        f"Симуляции, где вход или выход мельче {min_leg_sol} SOL, ОТБРОШЕНЫ: при пылевых сделках "
        "цена вырождается и отношение выход/вход взрывается (в сыром прогоне до +554989%). "
        "Отсекается размер сделки, а не величина результата -- иначе это была бы подгонка.")
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    report(out)


def recheck(mode: str, n: int, time_budget_s: int) -> None:
    """Прогнать самопроверку у двух провайдеров по УЖЕ посчитанному файлу.

    Нужен отдельным режимом потому, что в основном прогоне самопроверка идёт
    последней и первой попадает под обрыв бюджета времени: тогда сверять
    оказывается нечего. Здесь бюджет свой и тратится только на 3 блока.
    """
    path = out_path_for(mode)
    if not path.exists():
        raise SystemExit(f"файла {path} нет -- проверять нечего (режим {mode})")
    out = json.loads(path.read_text())
    key, key_name = helius_key()
    rpc = Rpc(key, min_interval_s=0.12, workers=2)
    rpc.deadline = time.monotonic() + time_budget_s
    log(f"ключ Helius из {key_name}; пересверка {n} строк из {path.name}")
    res = self_check(rpc, out["sims"], n=n)
    out["self_check"] = res
    out["self_check"]["выполнена_отдельным_прогоном_utc"] = now_utc()
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    log("самопроверка: " + json.dumps({k: v for k, v in res.items() if k != "checks"},
                                       ensure_ascii=False))
    for it in res.get("checks") or []:
        log(f"  слот {it['source_slot']}: сверено пар={it.get('сверено_пар')} "
            f"расхождений={it.get('расхождений')} совпало={it.get('совпало')}"
            + (f" ({it.get('почему_не_проверено')})" if it.get("не_проверено") else ""))
    if res.get("ok") is not True:
        raise SystemExit("самопроверка не подтверждена -- см. вывод выше")


def main() -> None:
    # Потолок глубокого добора выхода задаётся ключом: если добор съедает
    # весь бюджет времени, его можно понизить, не трогая код.
    global EXIT_DEEP_MAX_BLOCKS
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=("calibrate", "full"), default="calibrate")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--time-budget-s", type=int, default=75 * 60)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-interval-s", type=float, default=0.12)
    ap.add_argument("--no-cache", action="store_true",
                     help="не брать готовые строки из кэша (запись при этом продолжается, "
                          "чтобы прогон оставался возобновляемым)")
    ap.add_argument("--checkpoint-s", type=float, default=CHECKPOINT_S,
                     help="как часто сохранять кэш на диск, секунд")
    ap.add_argument("--only-leaders", default="",
                     help="через запятую: считать ТОЛЬКО эти кошельки-лидеры. Так прогон делится "
                          "на части, если целиком не укладывается в лимит задания")
    ap.add_argument("--seed-cache-from", default="",
                     help="взять готовые симуляции из выгрузки прошлого прогона (её sims) в кэш. "
                          "Спасает работу прогона, который писать кэш ещё не умел")
    ap.add_argument("--exit-deep-max-blocks", type=int, default=EXIT_DEEP_MAX_BLOCKS,
                     help="потолок блоков глубокого добора выхода; понизить, если добор съедает "
                          "весь бюджет времени")
    ap.add_argument("--checkpoint-push", action="store_true",
                     help="выгружать чекпойнт в git прямо из прогона -- иначе он погибнет "
                          "вместе с раннером и возобновлять будет не из чего")
    ap.add_argument("--min-leg-sol", type=float, default=MIN_LEG_SOL)
    ap.add_argument("--min-buys", type=int, default=3,
                     help="порог числа покупок для КАНДИДАТА (в задаче порога нет)")
    ap.add_argument("--extra-crowd", default="",
                     help="через запятую: дополнительные файлы скана толпы (например, 7-дневное "
                          "окно); запись замещает основную по тому же адресу")
    ap.add_argument("--self-test", action="store_true",
                     help="проверить правку метода без сети и без ключей и выйти")
    ap.add_argument("--recheck", action="store_true",
                     help="только самопроверка у двух провайдеров по готовому файлу")
    ap.add_argument("--recheck-n", type=int, default=3)
    ap.add_argument("--reaggregate", action="store_true",
                    help="пересчитать сводку из готового файла, без вызовов сети")
    args = ap.parse_args()

    if args.self_test:
        self_test_method()
        return
    EXIT_DEEP_MAX_BLOCKS = args.exit_deep_max_blocks
    if args.recheck:
        recheck(args.mode, args.recheck_n, args.time_budget_s)
        return
    if args.reaggregate:
        reaggregate(args.min_leg_sol, args.mode)
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

    # --no-cache теперь значит "не брать готовое", а НЕ "не сохранять".
    # Прежнее поведение выключало запись целиком, и обрыв прогона стоил
    # всей работы -- ровно то, что просил починить владелец.
    only_leaders = {a.strip() for a in args.only_leaders.split(",") if a.strip()}
    if only_leaders:
        before = len(items)
        missing = only_leaders - {it["leader"] for it in items}
        items = [it for it in items if it["leader"] in only_leaders]
        log(f"ЧАСТЬ ПРОГОНА: только {len(only_leaders)} лидеров -- {len(items)} симуляций из {before}")
        if missing:
            # Честно: адрес, которого нет среди источников, НЕ просчитан,
            # а не "просчитан и пусто".
            log(f"ВНИМАНИЕ: {len(missing)} адресов из --only-leaders нет среди источников: "
                f"{', '.join(sorted(missing))}")

    cache = SimCache(SIM_CACHE_PATH, METHOD_VERSION, ignore_existing=args.no_cache)
    if args.seed_cache_from:
        seed = Path(args.seed_cache_from)
        seed = seed if seed.is_absolute() else REPO_ROOT / seed
        n_seed = 0
        if seed.exists():
            try:
                prev = json.loads(seed.read_text())
            except (ValueError, OSError) as exc:
                log(f"засев кэша: {seed.name} не читается ({type(exc).__name__}) -- пропускаю")
                prev = {}
            # Берём ТОЛЬКО доведённые до конца строки. Симуляции,
            # оборвавшиеся по исчерпанию бюджета, выглядят как no_exit --
            # если засеять и их, обрыв прошлого прогона навсегда
            # закрепится в данных как "сделок не было".
            for r in (prev.get("sims") or []):
                sig = r.get("leader_signature")
                if sig and r.get("status") in ("ok", "частично") and not r.get("exit_scan_hit_cap"):
                    cache.put(sig, r)
                    n_seed += 1
            log(f"засев кэша из {seed.name}: взято {n_seed} завершённых симуляций "
                f"(строки с исчерпанным бюджетом и упёршиеся в потолок скана НЕ берутся)")
        else:
            log(f"засев кэша: файла {seed} нет")
    if cache.dropped_other_version:
        log(f"кэш: отброшено {cache.dropped_other_version} записей ЧУЖОЙ версии метода "
            f"(нужна {METHOD_VERSION}) -- смешивать старые и новые строки нельзя")
    log(f"кэш: взято готовых {cache.loaded}, версия метода {METHOD_VERSION}, "
        f"чекпойнт каждые {args.checkpoint_s}с, выгрузка в git: {'да' if args.checkpoint_push else 'нет'}")
    if args.mode == "calibrate":
        items = live[:args.limit] if args.limit else live
        meta: dict[str, dict] = {}
        label = "калибровка"
    else:
        extra = [Path(x.strip()) if Path(x.strip()).is_absolute() else REPO_ROOT / x.strip()
                 for x in args.extra_crowd.split(",") if x.strip()]
        items, meta = crowd_buys(args.min_buys, extra)
        if args.limit:
            items = items[:args.limit]
        label = "основной"
    log(f"{label}: симуляций к расчёту {len(items)}")

    rows = run(rpc, st, clock, items, args.workers, label, cache,
                checkpoint_s=args.checkpoint_s, push=args.checkpoint_push, started=started)
    # Калибровка -- на тех же строках, что и сводки: без пыли и без
    # медленных выходов. Раньше она считалась по ВСЕМ строкам, а сводки
    # по отфильтрованным -- и гейт проверял не то, что потом печаталось.
    calib = calibration([r for r in rows if row_usable(r, args.min_leg_sol)]) \
        if args.mode == "calibrate" else None
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
                       "кэш_взято_готовых": (cache.loaded if cache else 0),
                       "кэш_отброшено_чужой_версии": (cache.dropped_other_version if cache else 0),
                       "кэш_сохранений": (cache.saves if cache else 0),
                       "версия_метода": METHOD_VERSION,
                       "elapsed_s": round(time.monotonic() - started, 1),
                       "budget_exhausted": rpc.expired(),
                       "statuses": {k: sum(1 for r in rows if r.get("status") == k)
                                     for k in sorted({r.get("status") for r in rows})}},
        "параметры_отбора": {"min_buys_кандидата": args.min_buys,
                              "дополнительные_сканы": args.extra_crowd or None,
                              "окно_выхода_с": [EXIT_FROM_S, EXIT_TO_S],
                              "потолок_блоков_скана_выхода": EXIT_DEEP_MAX_BLOCKS,
                              "только_лидеры": sorted(only_leaders) or None,
                              "засеяно_из": args.seed_cache_from or None,
                              "порог_медленного_выхода_с": EXIT_SLOW_S},
        "calibration": calib,
        "обрыв_по_бюджету": truncation_report(rows, rpc.expired()),
        "overall": overall(rows, "все кошельки", args.min_leg_sol),
        "overall_pilot": overall([r for r in rows if r["leader"] == PILOT], "пилот", args.min_leg_sol),
        "wallets": wallets,
        "кандидаты_прошедшие_фильтр": passing_candidates(wallets) if wallets else None,
        "self_check": check,
        "sims": rows,
    }
    path = out_path_for(args.mode)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"выгрузка: {path.relative_to(REPO_ROOT)} (режим {args.mode})")
    report(out)


def report(out: dict) -> None:
    print()
    print("=" * 104)
    print(f"РЕТРО-СИГНАЛ, режим={out['mode']}, {out['generated_at_utc']}")
    print("проба accounts:", json.dumps(out["проба_accounts"], ensure_ascii=False))
    print("статистика:", json.dumps(out["run_stats"], ensure_ascii=False))
    t = out.get("обрыв_по_бюджету")
    if t:
        print()
        print("--- ОБРЫВ ПО БЮДЖЕТУ ---")
        print(f"  симуляций {t['симуляций_всего']}, no_exit {t['no_exit']}, "
              f"бюджет исчерпан: {t['бюджет_исчерпан']}")
        if t["оборвано_бюджетом_помечено"]:
            print(f"  ПОМЕЧЕНО оборванными: {t['оборвано_бюджетом_помечено']} "
                  f"(затронуто обрывом всего {t['помечено_как_затронутые_обрывом_всего']})")
        elif t["оценка_обрыва_в_no_exit"]:
            print(f"  ОЦЕНКА обрыва внутри no_exit: {t['оценка_обрыва_в_no_exit']} "
                  f"-- {t['как_считана_оценка']}")
            print(f"  {t['честно']}")
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
        if o.get("задержка_выхода_распределение"):
            print(f"  задержка выхода: {json.dumps(o['задержка_выхода_распределение'], ensure_ascii=False)}")
        if o.get("медленный_выход"):
            m = o["медленный_выход"]
            print(f"  медленный выход (> {m['порог_с']}с): исключено {m['n_исключено']}, "
                  f"их медиана E2 {m['медиана_E2_у_медленных']}, "
                  f"медиана задержки {m['медиана_задержки_у_медленных_с']}с")
        if o.get("n_упёрлись_в_потолок_скана"):
            print(f"  ВНИМАНИЕ: {o['n_упёрлись_в_потолок_скана']} симуляций упёрлись в потолок "
                  f"скана {EXIT_DEEP_MAX_BLOCKS} блоков -- до T+{EXIT_TO_S}с могли не дойти")
        for k, v in (o.get("по_источнику_входа") or {}).items():
            print(f"  {k} по источнику входа: рынок n={v['n_market']} медиана={v['медиана_market']} | "
                  f"цена лидера n={v['n_leader_price']} медиана={v['медиана_leader_price']} | "
                  f"вместе n={v['n_вместе']} медиана={v['медиана_вместе']} "
                  f"(доля leader_price {v['доля_leader_price']})")
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
