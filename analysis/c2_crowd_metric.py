#!/usr/bin/env python3
"""Задача A (C2): «толпа за источником» по активным источникам BATCH-3 и
BATCH-5 за последние N дней (по умолчанию 7). Только чтение цепи и GET DBot.

По каждой покупке источника -- ПЕРВЫЙ ВХОД >= 2 SOL-экв (метод проекта:
solana_batch5_rpc_check.classify_tx, курс USDC/USDT -> SOL по цепи):
* пул -- тот, где стоит хранилище минта в его транзакции
  (c2_common.identify_pool);
* цена P0 -- цена исполнения его сделки в этом пуле (его блок);
* P30 / P60 -- цена ПОСЛЕДНЕЙ сделки в том же пуле с blockTime <= T0+30 /
  T0+60. Сделок после него не было -- последняя сделка его же, рост 1.0
  (так и помечено). Ликвидность снята раньше точки -- «нет данных»;
* окно берётся по блокам: якорный блок с blockTime >= T0+61 (getBlock,
  только подписи), затем getSignaturesForAddress(минт и хранилище пула,
  before=последняя подпись якорного блока, until=подпись источника);
* толпа -- число РАЗНЫХ чужих кошельков, купивших минт (в любом пуле) в
  (T0, T0+30 с]: подписант с ростом баланса минта, либо подписант,
  заплативший котировкой, когда токен ушёл не ему (c2_common.mint_buyers).

Проценты -- только внутри котировки своего пула (SOL, USDC, xStock...),
без пересчёта. Нет пула, неоднозначная котировка, окно не выкачано,
окно больше предела -- «нет данных» с причиной, не ноль.

Выход: data/crowd_metric_<UTC-дата>.csv (по источникам),
data/crowd_metric_trades_<UTC-дата>.csv (по сделкам),
data/crowd_metric_<UTC-дата>.json (всё, с причинами и расходом).
Кэш возобновления: data/c2_cache/crowd_cache.json.
Учёт кредитов: служба c2_crowd, общий потолок C2 100 000 в сутки.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402

SERVICE = "c2_crowd"
CACHE_PATH = C.DATA / "c2_cache" / "crowd_cache.json"
CACHE_VERSION = 1
MAX_SIG_PAGES = 80          # 80 000 подписей на источник за окно -- выше честное «не выкачано»
WINDOW_MAX_PAGES = 30       # владелец 24.09: 30 000 подписей на минт/хранилище за 60 с
DT8_MAX_LAG = 3             # владелец: окно S+0..S+3 слота
METHOD_VERSION = 3          # меняется -- кэш покупок пересчитывается (толпа и DT8 берутся из прошлого)
CLASSIFY_BATCH = 100


class TimeUp(Exception):
    pass


# ------------------------------------------------------------ окно по блокам

def find_anchor(rpc, slot0: int, bt0: int, t_target: int) -> dict:
    """Первый найденный блок с blockTime >= t_target. Подписи блока -- в
    порядке исполнения, берётся последняя: before= по ней захватывает весь
    блок, кроме неё самой."""
    s = slot0 + max(10, int((t_target - bt0) / 0.4)) + 5
    tried = 0
    while tried < 20:
        tried += 1
        try:
            blk = rpc.call("getBlock", [s, {"transactionDetails": "signatures", "rewards": False,
                                             "maxSupportedTransactionVersion": C.TX_VERSION,
                                             "commitment": "finalized"}])
        except RuntimeError as exc:
            if C.is_skipped_slot_error(str(exc)):
                s += 1
                continue
            raise
        bt = (blk or {}).get("blockTime")
        sigs = (blk or {}).get("signatures") or []
        if bt is None or not sigs:
            s += 1
            continue
        if bt >= t_target:
            return {"slot": s, "block_time": bt, "signature": sigs[-1], "tries": tried}
        rate = (s - slot0) / max(1, bt - bt0)
        s += max(3, int((t_target - bt) * rate) + 3)
    raise RuntimeError(f"якорный блок после {C.utc(t_target)} не найден за 20 попыток")


BLOCK_SIG_OPTS = {"transactionDetails": "signatures", "rewards": False,
                  "maxSupportedTransactionVersion": C.TX_VERSION, "commitment": "finalized"}


def block_signatures(rpc, slot: int) -> list | None:
    """Подписи блока в порядке исполнения; None -- слот пропущен."""
    try:
        blk = rpc.call("getBlock", [slot, BLOCK_SIG_OPTS])
    except RuntimeError as exc:
        if C.is_skipped_slot_error(str(exc)):
            return None
        raise
    return (blk or {}).get("signatures") or []


def sig_window(rpc, address: str, before: str, slot0: int,
               max_pages: int = WINDOW_MAX_PAGES) -> tuple[list, bool]:
    """Подписи адреса от якоря назад, пока не пройдём слот slot0.

    until= не используется: порядок подписей ВНУТРИ слота у узла может
    быть не порядком исполнения (сортировка по подписи), и отсечка «после
    покупки» в её же слоте делается по индексу из getBlock."""
    out, cur = [], before
    for _ in range(max_pages):
        page = rpc.signatures(address, before=cur, limit=1000)
        out.extend(page)
        if len(page) < 1000 or (page[-1].get("slot") or 0) < slot0:
            return out, True
        cur = page[-1]["signature"]
    return out, False


def after_buy(lst: list, slot0: int, pos0: dict, idx_buy: int, t_max: int) -> list:
    """Успешные транзакции строго после покупки (в её слоте -- по индексу
    блока) и не позже t_max по blockTime. Порядок -- как у узла."""
    out = []
    for s in lst:
        sl = s.get("slot")
        if sl is None or sl < slot0:
            continue
        if sl == slot0:
            p = pos0.get(s.get("signature"))
            if p is None or p <= idx_buy:
                continue
        if s.get("err") is not None or s.get("blockTime") is None or s["blockTime"] > t_max:
            continue
        out.append(s)
    return out


# ------------------------------------------------------------ одна покупка

def order_in_slot(rpc, slot: int, sigs: list, pos_cache: dict) -> dict:
    """{подпись: индекс в блоке} для подписей одного слота (getBlock, 1 кредит)."""
    if slot not in pos_cache:
        bs = block_signatures(rpc, slot)
        pos_cache[slot] = {x: i for i, x in enumerate(bs or [])}
    return {x: pos_cache[slot].get(x) for x in sigs}


def price_at(rpc, fetched: dict, vault_sigs: list, t_point: int, pool: dict,
             buy_sig: str, pos_cache: dict) -> dict:
    """Цена ПОСЛЕДНЕЙ сделки в пуле с blockTime <= t_point.

    Кандидаты -- от старших слотов к младшим; внутри слота порядок берётся
    из getBlock, если в слоте больше одной транзакции пула."""
    cands = [s for s in vault_sigs if s["blockTime"] <= t_point]
    slots = sorted({s["slot"] for s in cands}, reverse=True)
    for sl in slots:
        in_slot = [s["signature"] for s in cands if s["slot"] == sl]
        need = [x for x in in_slot if x not in fetched]
        if need:
            fetched.update(rpc.get_txs(need))
        if any(fetched.get(x) is None for x in in_slot):
            return {"price": None, "why": f"узел не отдал транзакцию пула в слоте {sl}"}
        if len(in_slot) > 1:
            pos = order_in_slot(rpc, sl, in_slot, pos_cache)
            if any(v is None for v in pos.values()):
                return {"price": None, "why": f"нет порядка транзакций в слоте {sl}"}
            in_slot.sort(key=lambda x: pos[x], reverse=True)
        for x in in_slot:
            ev = C.pool_event(fetched[x], pool)
            if ev["kind"] == "swap":
                return {"price": ev["price"], "sig": x, "slot": sl, "last_trade_is_source": False}
            if ev["kind"] == "removal":
                return {"price": None, "why": f"ликвидность пула снята до точки ({x[:12]})"}
    return {"price": pool["price"], "sig": buy_sig, "slot": None, "last_trade_is_source": True}


def dt8_check(rpc, anchor_sig: str, slot0: int, pos0: dict, idx_buy: int, mint: str,
              pos_cache: dict, max_lag: int = DT8_MAX_LAG) -> dict:
    """Купил ли DT8 тот же минт в слотах S+0..S+max_lag: по ЕГО истории
    подписей (он есть в каждой своей транзакции), покупка = рост его
    баланса минта. Его инфраструктура не разбирается."""
    out = {"dt8": "нет данных", "dt8_why": None, "dt8_sig": None, "dt8_slot": None,
           "dt8_lag_slots": None, "dt8_index_in_block": None, "dt8_before_source": None}
    lst, ok = sig_window(rpc, C.FAST_COPIER_DT8, anchor_sig, slot0, max_pages=5)
    if not ok:
        out["dt8_why"] = "история DT8 в окне не выкачана (> 5000 подписей)"
        return out
    cands = [s for s in lst if s.get("err") is None and s.get("slot") is not None
             and slot0 <= s["slot"] <= slot0 + max_lag]
    if not cands:
        out["dt8"] = "нет"
        return out
    got = rpc.get_txs([s["signature"] for s in cands])
    if any(got.get(s["signature"]) is None for s in cands):
        out["dt8_why"] = "узел не отдал транзакцию DT8"
        return out
    hits = [s for s in cands
            if C.owner_mint_delta(got[s["signature"]], mint).get(C.FAST_COPIER_DT8, 0) > 0]
    if not hits:
        out["dt8"] = "нет"
        return out
    sl = min(s["slot"] for s in hits)
    in_slot = [s["signature"] for s in hits if s["slot"] == sl]
    pos = ({x: pos0.get(x) for x in in_slot} if sl == slot0
           else order_in_slot(rpc, sl, in_slot, pos_cache))
    known = [x for x in in_slot if pos.get(x) is not None]
    first = min(known, key=lambda x: pos[x]) if known else in_slot[0]
    out.update(dt8="да", dt8_sig=first, dt8_slot=sl, dt8_lag_slots=sl - slot0,
               dt8_index_in_block=pos.get(first),
               dt8_before_source=(sl == slot0 and pos.get(first) is not None
                                  and pos[first] < idx_buy))
    return out


# Пулы, где остатки хранилищ -- это и есть резервы x*y=k (спот = котировка /
# токен). У CLMM/DLMM/DAMM v2 и кривых (pump.fun, Launchlab, DBC) остатки
# хранилищ цену не задают -- там спот по резервам «нет данных».
RESERVE_SPOT_PROGRAMS = {
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "Pump AMM",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
}
CROWD_KEYS = ("crowd_30s", "crowd_30s_copy_wallets", "crowd_30s_rule2", "why_no_crowd")
DT8_KEYS = ("dt8", "dt8_why", "dt8_sig", "dt8_slot", "dt8_lag_slots", "dt8_index_in_block",
            "dt8_before_source")


def pool_program_of(tx: dict, vault: str | None) -> str | None:
    """Программа пула: programId неразобранной инструкции, где стоит хранилище,
    из списка 107 программ DEX (роутеры туда не входят)."""
    if not vault:
        return None
    import c2_pool_programs as PP  # noqa: PLC0415
    return PP.pool_program(tx, vault, PP.labels())["pool_program"]


def spot_after(tx: dict, pool: dict) -> tuple:
    """(спот после сделки источника по остаткам хранилищ, причина если нет)."""
    rows = {r["account"]: r for r in C.token_rows(tx).values()}
    tv = rows.get(pool["pool_vault"])
    if tv is None or not tv["post"]:
        return None, "нет остатка хранилища токена"
    if pool["quote_mint"] == C.NATIVE_QUOTE:
        return None, "котировка -- натив кривой, резервы виртуальные"
    qv = rows.get(pool["quote_vault"])
    if qv is None:
        return None, "нет остатка хранилища котировки"
    return C.ui(qv["post"], qv["dec"]) / C.ui(tv["post"], tv["dec"]), None


def analyze_buy(rpc, source: str, ev: dict, tx: dict, *, w1: int, w2: int,
                cap: int, known: dict, probe: dict, prev: dict | None = None) -> dict:
    sig, slot, bt, mint = ev["signature"], ev["slot"], tx.get("blockTime"), ev["mint"]
    row = {"signature": sig, "slot": slot, "block_time": bt, "block_time_utc": C.utc(bt),
           "mint": mint, "spend_sol_equiv": round(ev["spend_sol_equiv"], 6),
           "stable_usd_spent": ev.get("stable_usd_spent"),
           "rate_usd_per_sol": ev.get("rate_usd_per_sol"), "rate_source": ev.get("rate_source"),
           "index_in_block": None,
           "pool_vault": None, "pool_owner": None, "quote_mint": None, "split_route": None,
           "price_0": None, "price_30s": None, "price_30s_sig": None, "last_trade_is_source_30s": None,
           "growth_30s": None, "why_no_growth_30s": None,
           "price_60s": None, "price_60s_sig": None, "last_trade_is_source_60s": None,
           "growth_60s": None, "why_no_growth_60s": None,
           "crowd_30s": None, "crowd_30s_copy_wallets": None, "crowd_30s_rule2": None,
           "why_no_crowd": None, "window_tx_30s": None, "window_tx_60s": None,
           "anchor_slot": None, "dt8": "нет данных", "dt8_why": None, "dt8_sig": None,
           "dt8_slot": None, "dt8_lag_slots": None, "dt8_index_in_block": None,
           "dt8_before_source": None, "pool_program": None, "spot_after": None,
           "impact_source": None, "growth_after_30s": None, "growth_after_60s": None,
           "why_no_after": None, "failed_same_block_tx": None, "failed_same_block_wallets": None,
           "why_no_failed": None}
    # Уже посчитанные толпа и DT8 (прошлый прогон, те же окна) не пересчитываются.
    reuse_crowd = bool(prev) and prev.get("crowd_30s") is not None
    reuse_dt8 = bool(prev) and prev.get("dt8") in ("да", "нет")

    def all_no(why):
        for k in ("why_no_growth_30s", "why_no_growth_60s", "why_no_crowd", "why_no_after",
                  "why_no_failed"):
            row[k] = row[k] or why
        row["dt8_why"] = row["dt8_why"] or why
        return row
    if bt is None:
        return all_no("нет blockTime")
    pool = C.identify_pool(tx, source, mint)
    row.update(pool_vault=pool["pool_vault"], pool_owner=pool["pool_owner"],
               quote_mint=pool["quote_mint"], split_route=pool["split"],
               price_0=str(pool["price"]) if pool["price"] is not None else None)
    prog = pool_program_of(tx, pool["pool_vault"]) if pool["ok"] else None
    row["pool_program"] = prog
    spot = None
    if not pool["ok"]:
        row["why_no_after"] = f"пул: {pool['why_not']}"
    elif prog not in RESERVE_SPOT_PROGRAMS:
        row["why_no_after"] = f"тип пула {prog} -- остатки хранилищ не дают спот"
    else:
        spot, why = spot_after(tx, pool)
        if spot is None:
            row["why_no_after"] = why
        else:
            row["spot_after"] = str(spot)
            row["impact_source"] = round(float(spot / pool["price"]), 6)
    try:
        sigs0 = block_signatures(rpc, slot)
        if not sigs0 or sig not in sigs0:
            return all_no("нет порядка транзакций в блоке покупки (getBlock)")
        pos0 = {x: i for i, x in enumerate(sigs0)}
        idx_buy = pos0[sig]
        row["index_in_block"] = idx_buy
        pos_cache = {slot: pos0}
        anchor = find_anchor(rpc, slot, bt, bt + w2 + 1)
        row["anchor_slot"] = anchor["slot"]

        # DT8 -- по его собственной истории, независимо от пула и толпы.
        if reuse_dt8:
            row.update({k: prev.get(k) for k in DT8_KEYS})
        else:
            row.update(dt8_check(rpc, anchor["signature"], slot, pos0, idx_buy, mint, pos_cache))

        mint_sigs, m_ok = sig_window(rpc, mint, anchor["signature"], slot)
        vault_sigs, v_ok = (sig_window(rpc, pool["pool_vault"], anchor["signature"], slot)
                            if pool["ok"] else ([], True))
        # Проверка семантики before= на КАЖДОЙ покупке: подпись якоря не про
        # наш адрес, и история хранилища до неё обязана содержать саму
        # покупку -- хранилище в ней участвовало.
        if pool["ok"] and v_ok:
            found = any(s.get("signature") == sig for s in vault_sigs)
            with probe["lock"]:
                probe["checked"] = probe.get("checked", 0) + 1
                probe["failed"] = probe.get("failed", 0) + (not found)
            if not found:
                return all_no("узел: история хранилища до якоря не содержит покупку "
                              "(before= не по слоту) -- окно не доверенное")
    except RuntimeError as exc:
        return all_no(f"окно не построено: {str(exc)[:160]}")

    # Упавшие в блоке источника ПОСЛЕ него транзакции с этим минтом (у других
    # кошельков): как 12 из 15 в слоте 5HdXjagm52. Подписанты -- по самим
    # транзакциям (до 60 штук).
    if m_ok:
        fl = {}
        for x in mint_sigs + vault_sigs:
            if (x.get("slot") == slot and x.get("err") is not None
                    and (pos0.get(x.get("signature")) or -1) > idx_buy):
                fl[x["signature"]] = x
        row["failed_same_block_tx"] = len(fl)
        if fl:
            got_f = rpc.get_txs(list(fl)[:60])
            wallets = set()
            for t in got_f.values():
                for w in C.signers(t or {}):
                    if w != source:
                        wallets.add(w)
            row["failed_same_block_wallets"] = len(wallets)
        else:
            row["failed_same_block_wallets"] = 0
    else:
        row["why_no_failed"] = "история минта в окне не выкачана"
    mint_ok = after_buy(mint_sigs, slot, pos0, idx_buy, bt + w2)
    vault_ok = after_buy(vault_sigs, slot, pos0, idx_buy, bt + w2)
    union: dict = {}
    for s in mint_ok + vault_ok:
        union.setdefault(s["signature"], s)
    in30 = [s for s in union.values() if s["blockTime"] <= bt + w1]
    row["window_tx_30s"] = len(in30)
    row["window_tx_60s"] = len(union)
    fetched: dict = {}

    if reuse_crowd:
        row.update({k: prev.get(k) for k in CROWD_KEYS})
    elif not m_ok or not v_ok:
        row["why_no_crowd"] = f"окно не выкачано целиком (> {WINDOW_MAX_PAGES * 1000} подписей)"
    elif len(in30) > cap:
        row["why_no_crowd"] = f"в окне 30 с {len(in30)} транзакций > предела {cap}"
    else:
        fetched.update(rpc.get_txs([s["signature"] for s in in30]))
        missing = [s for s in in30 if fetched.get(s["signature"]) is None]
        if missing:
            row["why_no_crowd"] = f"узел не отдал {len(missing)} транзакций окна"
        else:
            buyers: dict = {}
            for s in in30:
                for w, rule in C.mint_buyers(fetched[s["signature"]], mint).items():
                    if w != source:
                        buyers.setdefault(w, rule)
            row["crowd_30s"] = len(buyers)
            row["crowd_30s_copy_wallets"] = sum(1 for w in buyers if w in known)
            row["crowd_30s_rule2"] = sum(1 for r in buyers.values() if r == "signer_paid")

    if not pool["ok"]:
        row["why_no_growth_30s"] = row["why_no_growth_60s"] = f"пул: {pool['why_not']}"
        return row
    if not v_ok:
        row["why_no_growth_30s"] = row["why_no_growth_60s"] = "история хранилища в окне не выкачана"
        return row
    p0 = pool["price"]
    for w, tag in ((w1, "30s"), (w2, "60s")):
        try:
            pa = price_at(rpc, fetched, vault_ok, bt + w, pool, sig, pos_cache)
        except RuntimeError as exc:
            pa = {"price": None, "why": f"узел: {str(exc)[:120]}"}
        if pa.get("price") is None:
            row[f"why_no_growth_{tag}"] = pa.get("why")
            continue
        row[f"price_{tag}"] = str(pa["price"])
        row[f"price_{tag}_sig"] = pa.get("sig")
        row[f"last_trade_is_source_{tag}"] = pa.get("last_trade_is_source")
        row[f"growth_{tag}"] = round(float(pa["price"] / p0), 6)
        if spot is not None:
            row[f"growth_after_{tag}"] = round(float(pa["price"] / spot), 6)
    if spot is not None:
        for tag in ("30s", "60s"):
            if row[f"growth_after_{tag}"] is None and not row["why_no_after"]:
                row["why_no_after"] = row[f"why_no_growth_{tag}"]
    return row


# ------------------------------------------------------------ один источник

def scan_source(rpc, rc, src: dict, *, days: float, now: int, cache: dict, cache_lock,
                w1: int, w2: int, cap: int, known: dict, probe: dict) -> dict:
    addr = src["address"]
    cutoff = now - int(days * 86400)
    out = {**src, "sig_scan_complete": False, "n_signatures": 0, "n_signatures_ok": 0,
           "n_tx_fetch_failed": 0, "n_multi_mint_skipped": 0, "n_first_entries_lt2": 0,
           "buys_rate_missing": 0, "rate_missing_sigs": [], "trades": [], "error": None}
    try:
        sigs, before = [], None
        for _ in range(MAX_SIG_PAGES):
            page = rpc.signatures(addr, before=before, limit=1000)
            sigs.extend(page)
            if len(page) < 1000 or (page[-1].get("blockTime") or 0) < cutoff:
                out["sig_scan_complete"] = True
                break
            before = page[-1]["signature"]
        win = [s for s in sigs if s.get("blockTime") is not None and s["blockTime"] >= cutoff]
        out["n_signatures"] = len(win)
        ok = [s["signature"] for s in win if s.get("err") is None]
        out["n_signatures_ok"] = len(ok)
        with cache_lock:
            cls = cache["classified"].setdefault(addr, {})
            todo = [s for s in ok if s not in cls]
        log_every = max(1, len(todo) // 10)
        buys_tx: dict = {}
        local: dict = {}
        for i in range(0, len(todo), CLASSIFY_BATCH):
            part = todo[i:i + CLASSIFY_BATCH]
            got = rpc.get_txs(part)
            for s in part:
                tx = got.get(s)
                if tx is None:
                    out["n_tx_fetch_failed"] += 1
                    continue
                ev = C.classify_first_entry(rc, tx, addr)
                kind = (ev or {}).get("kind")
                if kind in ("first_entry", "rate_missing"):
                    rec = {k: ev.get(k) for k in ("kind", "mint", "spend_sol_equiv", "stable_usd_spent",
                                                  "price_missing", "rate_usd_per_sol", "rate_source",
                                                  "slot", "signature", "block_time")}
                    if kind == "first_entry" and not ev.get("price_missing") and ev["spend_sol_equiv"] >= 2:
                        buys_tx[s] = tx
                else:
                    rec = "m" if kind == "multi_mint_skipped" else "n"
                # «Нет курса» в кэш не кладём: курс мог не отдаться разово,
                # следующий прогон спросит снова.
                unknown_rate = isinstance(rec, dict) and (rec["kind"] == "rate_missing"
                                                          or rec.get("price_missing"))
                with cache_lock:
                    if unknown_rate:
                        local[s] = rec
                    else:
                        cls[s] = rec
            if (i // CLASSIFY_BATCH) % max(1, log_every // CLASSIFY_BATCH) == 0:
                C.log(f"{src.get('remark') or addr[:8]}: разобрано {min(i + CLASSIFY_BATCH, len(todo))}"
                      f"/{len(todo)} новых транзакций")
        buys = []
        for s in ok:
            rec = cls.get(s, local.get(s))
            if rec == "m":
                out["n_multi_mint_skipped"] += 1
            if not isinstance(rec, dict):
                continue
            if rec["kind"] == "rate_missing" or rec.get("price_missing"):
                out["buys_rate_missing"] += 1
                out["rate_missing_sigs"].append(s)
                continue
            if rec["spend_sol_equiv"] < 2:
                out["n_first_entries_lt2"] += 1
                continue
            buys.append(rec)
        buys.sort(key=lambda r: r["slot"])
        for ev in buys:
            s = ev["signature"]
            with cache_lock:
                hit = cache["buys"].get(s)
            if hit and hit.get("_w") == [w1, w2, cap, METHOD_VERSION]:
                out["trades"].append(hit)
                continue
            prev = hit if hit and (hit.get("_w") or [None] * 3)[:3] == [w1, w2, cap] else None
            tx = buys_tx.get(s) or rpc.get_txs([s]).get(s)
            if tx is None:
                row = {"signature": s, "mint": ev["mint"], "slot": ev["slot"],
                       "why_no_growth_30s": "узел не отдал транзакцию покупки",
                       "why_no_growth_60s": "узел не отдал транзакцию покупки",
                       "why_no_crowd": "узел не отдал транзакцию покупки"}
            else:
                row = analyze_buy(rpc, addr, ev, tx, w1=w1, w2=w2, cap=cap, known=known, probe=probe,
                                  prev=prev)
            row["_w"] = [w1, w2, cap, METHOD_VERSION]
            with cache_lock:
                cache["buys"][s] = row
            out["trades"].append(row)
    except (C.BudgetExceeded, TimeUp) as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    except RuntimeError as exc:
        out["error"] = f"RuntimeError: {str(exc)[:300]}"
    return out


# ------------------------------------------------------------ сводка

def aggregate(res: dict) -> dict:
    tr = res.get("trades") or []
    g30 = [t["growth_30s"] for t in tr if t.get("growth_30s") is not None]
    g60 = [t["growth_60s"] for t in tr if t.get("growth_60s") is not None]
    cr = [t["crowd_30s"] for t in tr if t.get("crowd_30s") is not None]
    ga30 = [t["growth_after_30s"] for t in tr if t.get("growth_after_30s") is not None]
    ga60 = [t["growth_after_60s"] for t in tr if t.get("growth_after_60s") is not None]
    imp = [t["impact_source"] for t in tr if t.get("impact_source") is not None]
    fb = [t for t in tr if t.get("failed_same_block_tx") is not None]
    d8 = [t for t in tr if t.get("dt8") in ("да", "нет")]
    d8_yes = [t for t in d8 if t.get("dt8") == "да"]
    d8_lag = [t["dt8_lag_slots"] for t in d8_yes if t.get("dt8_lag_slots") is not None]

    def reasons(key):
        out: dict = {}
        for t in tr:
            w = t.get(key)
            if w:
                out[w] = out.get(w, 0) + 1
        return "; ".join(f"{k}: {v}" for k, v in sorted(out.items(), key=lambda kv: -kv[1]))
    complete = bool(res.get("sig_scan_complete")) and not res.get("error") \
        and not res.get("n_tx_fetch_failed")
    return {
        "task": res.get("task"), "source": res.get("address"), "remark": res.get("remark"),
        "trades_7d": len(tr) if complete else (f">={len(tr)} (неполно)" if tr else "нет данных"),
        "growth_30s_median": round(C.median(g30), 4) if g30 else None,
        "growth_60s_median": round(C.median(g60), 4) if g60 else None,
        "share_x2_30s": round(sum(1 for g in g30 if g >= 2) / len(g30), 4) if g30 else None,
        "crowd_30s_median": C.median(cr) if cr else None,
        "impact_source_median": round(C.median(imp), 4) if imp else None,
        "growth_after_30s_median": round(C.median(ga30), 4) if ga30 else None,
        "growth_after_60s_median": round(C.median(ga60), 4) if ga60 else None,
        "share_x1.3_after_30s": round(sum(1 for g in ga30 if g >= 1.3) / len(ga30), 4) if ga30 else None,
        "n_after_30s": len(ga30),
        "trades_with_failed_same_block": sum(1 for t in fb if t["failed_same_block_tx"] > 0),
        "n_failed_checked": len(fb),
        "no_after_reasons": reasons("why_no_after"),
        "dt8_share": round(len(d8_yes) / len(d8), 4) if d8 else None,
        "dt8_lag_median_slots": C.median(d8_lag) if d8_lag else None,
        "n_dt8_checked": len(d8), "n_dt8_before_source": sum(1 for t in d8_yes if t.get("dt8_before_source")),
        "n_growth_30s": len(g30), "n_growth_60s": len(g60), "n_crowd_30s": len(cr),
        "no_growth_30s_reasons": reasons("why_no_growth_30s"),
        "no_crowd_reasons": reasons("why_no_crowd"),
        "buys_rate_missing": res.get("buys_rate_missing"),
        "first_entries_lt2": res.get("n_first_entries_lt2"),
        "multi_mint_skipped": res.get("n_multi_mint_skipped"),
        "n_signatures_window": res.get("n_signatures"),
        "tx_fetch_failed": res.get("n_tx_fetch_failed"),
        "scan_complete": complete,
        "error": res.get("error"),
    }


AGG_COLS = ["task", "source", "remark", "trades_7d", "growth_after_30s_median",
            "growth_after_60s_median", "share_x1.3_after_30s", "impact_source_median", "n_after_30s",
            "growth_30s_median", "growth_60s_median", "share_x2_30s", "crowd_30s_median",
            "trades_with_failed_same_block", "n_failed_checked", "no_after_reasons", "dt8_share", "dt8_lag_median_slots", "n_dt8_checked",
            "n_dt8_before_source", "n_growth_30s", "n_growth_60s", "n_crowd_30s",
            "no_growth_30s_reasons", "no_crowd_reasons", "buys_rate_missing", "first_entries_lt2",
            "multi_mint_skipped", "n_signatures_window", "tx_fetch_failed", "scan_complete", "error"]
TRADE_COLS = ["task", "source", "remark", "signature", "slot", "index_in_block", "block_time_utc", "mint",
              "spend_sol_equiv", "stable_usd_spent", "rate_usd_per_sol", "rate_source",
              "pool_vault", "pool_owner", "quote_mint", "split_route", "price_0",
              "price_30s", "growth_30s", "last_trade_is_source_30s", "why_no_growth_30s",
              "price_60s", "growth_60s", "last_trade_is_source_60s", "why_no_growth_60s",
              "crowd_30s", "crowd_30s_copy_wallets", "crowd_30s_rule2", "why_no_crowd",
              "pool_program", "spot_after", "impact_source", "growth_after_30s", "growth_after_60s",
              "why_no_after", "failed_same_block_tx", "failed_same_block_wallets", "why_no_failed",
              "dt8", "dt8_slot", "dt8_lag_slots", "dt8_index_in_block", "dt8_before_source", "dt8_sig",
              "dt8_why", "window_tx_30s", "window_tx_60s", "price_30s_sig", "price_60s_sig", "anchor_slot"]


def sort_key(a: dict):
    s = a.get("growth_after_30s_median")
    return (s is None, -(s or 0), a.get("task") or "", a.get("remark") or "")


def failed_block_summary(results: list) -> dict:
    """Теряем ли мы лучшие сделки: сделки источника, где в его блоке после
    него упали чужие транзакции с минтом, против остальных."""
    tr = [t for r in results for t in r.get("trades") or [] if t.get("failed_same_block_tx") is not None]

    def grp(ts):
        g30 = [t["growth_30s"] for t in ts if t.get("growth_30s") is not None]
        ga = [t["growth_after_30s"] for t in ts if t.get("growth_after_30s") is not None]
        return {"n": len(ts), "growth_30s_median": round(C.median(g30), 4) if g30 else None,
                "share_x2_30s": round(sum(g >= 2 for g in g30) / len(g30), 4) if g30 else None,
                "n_growth_30s": len(g30),
                "growth_after_30s_median": round(C.median(ga), 4) if ga else None,
                "n_after_30s": len(ga)}
    with_f = [t for t in tr if t["failed_same_block_tx"] > 0]
    no_f = [t for t in tr if t["failed_same_block_tx"] == 0]
    many = [t for t in tr if t["failed_same_block_tx"] >= 5]
    return {"with_failed": grp(with_f), "without_failed": grp(no_f), "failed_ge5": grp(many),
            "n_checked": len(tr)}


def write_outputs(date: str, rows: list, leader_row: dict | None, meta: dict,
                  results: list, out_dir: Path = C.DATA) -> dict:
    rows = sorted(rows, key=sort_key)
    paths = {"agg": out_dir / f"crowd_metric_{date}.csv",
             "trades": out_dir / f"crowd_metric_trades_{date}.csv",
             "json": out_dir / f"crowd_metric_{date}.json"}
    with open(paths["agg"], "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["row_kind"] + AGG_COLS)
        w.writeheader()
        for r in rows:
            w.writerow({"row_kind": "source", **{k: r.get(k) for k in AGG_COLS}})
        if leader_row:
            w.writerow({"row_kind": "leader", **{k: leader_row.get(k) for k in AGG_COLS}})
    with open(paths["trades"], "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_COLS)
        w.writeheader()
        for res in results:
            for t in res.get("trades") or []:
                w.writerow({**{k: t.get(k) for k in TRADE_COLS}, "task": res.get("task"),
                            "source": res.get("address"), "remark": res.get("remark")})
    body = {"schema_version": 2, **meta, "sources_sorted_by_growth_after_30s": rows,
            "failed_same_block_summary": failed_block_summary(results),
            "leader_row": leader_row,
            "per_source": [{k: v for k, v in r.items() if k != "trades"} | {"trades": [
                {k: v for k, v in t.items() if k != "_w"} for t in r.get("trades") or []]}
                for r in results]}
    paths["json"].write_text(json.dumps(body, ensure_ascii=False, indent=1, default=str),
                             encoding="utf-8")
    return {k: str(v) for k, v in paths.items()}


def load_cache() -> dict:
    try:
        c = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if c.get("version") == CACHE_VERSION:
            return c
    except (OSError, ValueError):
        pass
    return {"version": CACHE_VERSION, "classified": {}, "buys": {}}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":"), default=str),
                   encoding="utf-8")
    tmp.replace(CACHE_PATH)


# ------------------------------------------------------------ main

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument("--only", default="", help="через запятую: только эти источники")
    p.add_argument("--limit-sources", type=int, default=0)
    p.add_argument("--crowd-window-s", type=int, default=30)
    p.add_argument("--window2-s", type=int, default=60)
    p.add_argument("--crowd-cap", type=int, default=1500)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--time-budget-s", type=int, default=17000)
    p.add_argument("--no-leader", action="store_true")
    a = p.parse_args()
    if a.self_test:
        return self_test()

    key = C.RC.helius_key()[0]
    if not key:
        print("СТОП: ключ Helius не задан (секрет HELIUS_API)", file=sys.stderr)
        return 2
    dkey = (os.environ.get("DBOT_API_KEY") or "").strip()
    if not dkey:
        print("СТОП: DBOT_API_KEY не задан -- список источников взять неоткуда", file=sys.stderr)
        return 2
    t_start = time.time()
    date = C.today_utc()
    now = int(t_start)
    C.log(f"C2 задача A: окно {a.days} сут, потрачено C2 сегодня до старта: "
          f"{C.c2_spent_today()} из {C.C2_DAILY_BUDGET}")

    tasks = C.DbotReadOnly(dkey).tasks()
    sources = C.sources_of_tasks(tasks)
    known = C.task_wallets(tasks)
    known[C.EXECUTOR_WALLET] = "bloom_executor"
    tasks_view = [{"name": t.get("name"), "enabled": t.get("enabled"),
                   "walletAddress": t.get("walletAddress"), "targetIds": t.get("targetIds"),
                   "targetNames": t.get("targetNames"), "updateAt": t.get("updateAt")}
                  for t in tasks if isinstance(t, dict)]
    (C.DATA / f"c2_dbot_tasks_{date}.json").write_text(json.dumps(
        {"fetched_utc": C.utc(time.time()), "endpoint": "GET " + C.DBOT_TASKS_PATH,
         "tasks": tasks_view}, ensure_ascii=False, indent=1), encoding="utf-8")
    C.log(f"DBot GET: задач {len(tasks)}, источников BATCH-3+BATCH-5: {len(sources)} "
          f"({sum(s['task'] == 'BATCH-3' for s in sources)} + "
          f"{sum(s['task'] == 'BATCH-5' for s in sources)})")
    leader_in = next((s for s in sources if s["address"] == C.LEADER_BEQV), None)
    work = list(sources)
    if a.only:
        want = {x.strip() for x in a.only.split(",") if x.strip()}
        work = [s for s in work if s["address"] in want]
    if a.limit_sources:
        work = work[:a.limit_sources]
    leader_src = None
    if not a.no_leader:
        leader_src = leader_in or {"address": C.LEADER_BEQV, "task": "нет в BATCH-3/5 по GET",
                                   "remark": "Beqv6"}
        if leader_src["address"] not in {s["address"] for s in work}:
            work.append(leader_src)

    rpc = C.C2Rpc(SERVICE, key=key)
    rpc.deadline = time.monotonic() + a.time_budget_s
    orig_check = rpc.check_budget

    def check_with_time(n):
        if rpc.expired():
            raise TimeUp("бюджет времени прогона истёк")
        orig_check(n)
    rpc.check_budget = check_with_time
    rc = C.load_rpc_check()
    book = C.RateBook(rpc)
    C.install_onchain_rate(rc, book)
    cache = load_cache()
    cache_lock = threading.Lock()
    probe = {"lock": threading.Lock(), "result": None}

    stop_saver = threading.Event()

    def saver():
        while not stop_saver.wait(120):
            with cache_lock:
                snap = json.loads(json.dumps(cache, default=str))
            save_cache(snap)
    threading.Thread(target=saver, daemon=True).start()

    def one(src):
        C.log(f"старт {src['task']} {src.get('remark') or ''} {src['address']}")
        r = scan_source(rpc, rc, src, days=a.days, now=now, cache=cache, cache_lock=cache_lock,
                        w1=a.crowd_window_s, w2=a.window2_s, cap=a.crowd_cap, known=known,
                        probe=probe)
        C.log(f"готово {src.get('remark') or src['address'][:8]}: покупок {len(r['trades'])}, "
              f"ошибка: {r['error']}")
        return r

    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        results = list(ex.map(one, work))
    stop_saver.set()
    save_cache(cache)

    agg = [aggregate(r) for r in results]
    # Лидер -- всегда отдельной строкой и вне сортировки, даже если он в BATCH-5.
    leader_row = None
    if leader_src:
        leader_row = next((x for x in agg if x["source"] == C.LEADER_BEQV), None)
        agg = [x for x in agg if x["source"] != C.LEADER_BEQV]
    meta = {"generated_utc": C.utc(time.time()), "window_days": a.days,
            "window_from_utc": C.utc(now - int(a.days * 86400)), "window_to_utc": C.utc(now),
            "crowd_window_s": a.crowd_window_s, "window2_s": a.window2_s, "crowd_cap": a.crowd_cap,
            "sources_origin": "DBot GET /automation/follow_orders (живьём)",
            "n_sources_batch3_batch5": len(sources),
            "leader": C.LEADER_BEQV, "leader_in_batch": leader_in["task"] if leader_in else None,
            "before_semantics_check": {k: v for k, v in probe.items() if k != "lock"},
            "fast_copier": C.FAST_COPIER_DT8, "fast_copier_window_slots": DT8_MAX_LAG,
            "rate_stats": book.stats, "rpc_stats": rpc.stats,
            "failed_same_block_summary": failed_block_summary(results),
            "rpc_calls_by_method": rpc.calls_by_method,
            "credits_this_run": rpc.stats.get("кредитов"),
            "c2_usage": C.c2_usage_report(),
            "elapsed_s": round(time.time() - t_start, 1),
            "definitions": {
                "trade": "первый вход (баланс минта до сделки 0), трата SOL+WSOL+USDC/USDT по курсу "
                         "по цепи >= 2 SOL-экв (classify_tx)",
                "price": "цена исполнения сделки в пуле источника: |дельта котировки|/|дельта минта| "
                         "по хранилищам; P30/P60 -- последняя сделка в пуле с blockTime <= T0+30/60",
                "growth": "P/P0 в котировке пула, без пересчёта",
                "crowd": "разные чужие кошельки, купившие минт в (T0, T0+30 с]",
                "dt8": "рост баланса минта у DT8hib8... в слотах S..S+3 (по его истории подписей); "
                       "отставание = слот DT8 - слот источника"}}
    paths = write_outputs(date, agg, leader_row, meta, results)
    C.log(f"выгрузка: {paths}")
    C.log(f"кредитов за прогон: {rpc.stats.get('кредитов')}, C2 сегодня: {C.c2_spent_today()}")
    print("--- упавшие в блоке источника:", json.dumps(failed_block_summary(results), ensure_ascii=False))
    print("--- коротко (сортировка по growth_after_30s) ---")
    for r in sorted(agg, key=sort_key) + ([leader_row] if leader_row else []):
        print(f"{r['task']:<8} {str(r['remark'])[:14]:<14} n={r['trades_7d']} "
              f"g30={r['growth_30s_median']} g60={r['growth_60s_median']} "
              f"x2={r['share_x2_30s']} crowd={r['crowd_30s_median']} dt8={r['dt8_share']}/"
              f"{r['dt8_lag_median_slots']} ga30={r['growth_after_30s_median']} imp={r['impact_source_median']} "
              f"x1.3={r['share_x1.3_after_30s']} fail={r['trades_with_failed_same_block']}/{r['n_failed_checked']} "
              f"ошибка={r['error']}")
    return 0


# ------------------------------------------------------------ самопроверка

class FakeRpc:
    """Подставной узел: блоки по слотам (подписи в порядке исполнения),
    история адресов (от новых к старым), транзакции по подписи.
    before= отсчитывается по СЛОТУ подписи, как у настоящего узла."""

    def __init__(self, blocks: dict, sigs_by_addr: dict, txs: dict) -> None:
        self.blocks, self.sigs_by_addr, self.txs = blocks, sigs_by_addr, txs
        self.calls: list = []

    def call(self, method, params, **kw):
        self.calls.append(method)
        if method == "getBlock":
            if params[0] not in self.blocks:
                raise RuntimeError("getBlock: RPC error {'code': -32007, 'message': 'skipped'}")
            return self.blocks[params[0]]
        raise RuntimeError(f"нет {method}")

    def slot_of(self, sig):
        for b_slot, b in self.blocks.items():
            if sig in (b.get("signatures") or []):
                return b_slot
        for lst in self.sigs_by_addr.values():
            for x in lst:
                if x["signature"] == sig:
                    return x["slot"]
        raise KeyError(sig)

    def signatures(self, address, *, before=None, until=None, limit=1000):
        self.calls.append("getSignaturesForAddress")
        lst = self.sigs_by_addr.get(address, [])
        if before:
            bslot = self.slot_of(before)
            lst = [x for x in lst if x["slot"] < bslot]
        return lst[:limit]

    def get_txs(self, sigs):
        self.calls.append("getTransaction")
        return {x: self.txs.get(x) for x in sigs}


def self_test() -> int:
    checks: list = []

    def chk(name, ok, got=""):
        checks.append((name, bool(ok), got))

    txs = C.load_real_txs()
    buy = next(t for x, t in txs.items() if x.startswith("2uPwpSAQ"))
    src = "BmjAUDbwBMxR5shrmzBtKRwveVahFGFiEH3oTq7QTHnu"
    mint = "87pa2UbBB2dhD4b7CHDPHzzKjrdrUtEcBHueXnVz6CJp"
    pool = C.identify_pool(buy, src, mint)
    slot0, bt0, sig0 = buy["slot"], buy["blockTime"], C.first_signature(buy)
    vault, qv = pool["pool_vault"], pool["quote_vault"]
    DT8 = C.FAST_COPIER_DT8

    def later(sig, slot, bt, signer, d_vault, d_quote, buyer_gain=0, err=None, owner=None):
        """Сделка в том же пуле: дельты хранилищ (сырые) + прирост минта."""
        keys = [signer, vault, qv, "BUYER_ATA"]
        pre = [{"accountIndex": 1, "owner": pool["pool_owner"], "mint": mint,
                "uiTokenAmount": {"amount": "1000000000000000", "decimals": 6}},
               {"accountIndex": 2, "owner": pool["pool_owner"], "mint": pool["quote_mint"],
                "uiTokenAmount": {"amount": "1000000000000", "decimals": 8}}]
        post = [{"accountIndex": 1, "owner": pool["pool_owner"], "mint": mint,
                 "uiTokenAmount": {"amount": str(1000000000000000 + d_vault), "decimals": 6}},
                {"accountIndex": 2, "owner": pool["pool_owner"], "mint": pool["quote_mint"],
                 "uiTokenAmount": {"amount": str(1000000000000 + d_quote), "decimals": 8}}]
        if buyer_gain:
            post.append({"accountIndex": 3, "owner": owner or signer, "mint": mint,
                         "uiTokenAmount": {"amount": str(buyer_gain), "decimals": 6}})
        return {"slot": slot, "blockTime": bt,
                "transaction": {"signatures": [sig], "message": {
                    "accountKeys": [{"pubkey": k, "signer": k == signer} for k in keys],
                    "instructions": [{"programId": "DEX", "accounts": [vault, qv]}]}},
                "meta": {"err": err, "preBalances": [0] * 4, "postBalances": [0] * 4,
                         "preTokenBalances": pre, "postTokenBalances": post,
                         "innerInstructions": []}}
    p0 = pool["price"]

    def q(mult, n_tok=1000):
        return int(D(n_tok) * p0 * D(mult) * D(10) ** 8)
    dv = -1_000_000_000  # 1000 токенов
    # в слоте покупки ДО неё -- чужая покупка: не толпа и не цена
    t_pre = later("0" * 88, slot0, bt0, "WALLET_PRE", dv, q("0.5"), buyer_gain=1)
    # в слоте покупки ПОСЛЕ неё -- DT8 покупает (S+0)
    t_d8 = later("8" * 88, slot0, bt0, DT8, dv, q("1.1"), buyer_gain=1_000_000)
    # через 10 с: покупка по цене x2.5 (кошелёк A)
    t_a = later("A" * 88, slot0 + 25, bt0 + 10, "WALLET_A", dv, q("2.5"), buyer_gain=1_000_000_000)
    # в том же слоте, что A, но РАНЬШЕ по блоку -- сделка x9 (не последняя)
    t_a0 = later("a" * 88, slot0 + 25, bt0 + 10, "WALLET_A0", dv, q("9"), buyer_gain=1)
    # через 20 с: покупка кошельком B в другом пуле (видна по минту)
    t_b = later("B" * 88, slot0 + 50, bt0 + 20, "WALLET_B", 0, 0, buyer_gain=5)
    # через 25 с: сам источник докупает -- в толпу не идёт
    t_s = later("C" * 88, slot0 + 62, bt0 + 25, src, 0, 0, buyer_gain=7)
    # через 29 с: неуспешная -- мимо
    t_f = later("D" * 88, slot0 + 72, bt0 + 29, "WALLET_F", dv, q("3"), buyer_gain=1, err={"x": 1})
    # через 45 с: продажа по x1.5
    t_e = later("E" * 88, slot0 + 112, bt0 + 45, "WALLET_E", 1_000_000_000, -q("1.5"))
    # через 70 с -- вне окна 60 с
    t_g = later("G" * 88, slot0 + 175, bt0 + 70, "WALLET_G", dv, q("20"), buyer_gain=1)

    def sg(t):
        return {"signature": C.first_signature(t), "slot": t["slot"], "blockTime": t["blockTime"],
                "err": t["meta"]["err"]}
    anchor_sig = "Z" * 88
    blocks = {slot0: {"blockTime": bt0, "signatures": ["0" * 88, sig0, "8" * 88]},
              slot0 + 25: {"blockTime": bt0 + 10, "signatures": ["a" * 88, "A" * 88]},
              # якорь: первая догадка пропущена, дальше рано, потом поздно
              slot0 + 158: {"blockTime": bt0 + 55, "signatures": ["Y" * 88]},
              slot0 + 185: {"blockTime": bt0 + 65, "signatures": ["X" * 88, anchor_sig]}}
    me = {"signature": sig0, "slot": slot0, "blockTime": bt0, "err": None}
    # узел внутри слота отдаёт подписи НЕ в порядке исполнения (A раньше a)
    vault_hist = [sg(t_g), sg(t_e), sg(t_f), sg(t_a), sg(t_a0), sg(t_d8), me, sg(t_pre)]
    mint_hist = [sg(t_g), sg(t_f), sg(t_s), sg(t_b), sg(t_a), sg(t_a0), sg(t_d8), me, sg(t_pre)]
    dt8_hist = [sg(t_d8)]
    alltx = {C.first_signature(t): t for t in (t_pre, t_d8, t_a, t_a0, t_b, t_s, t_f, t_e, t_g)}
    fake = FakeRpc(blocks, {vault: vault_hist, mint: mint_hist, DT8: dt8_hist}, alltx)
    an = find_anchor(fake, slot0, bt0, bt0 + 61)
    chk("якорь: пропущенный слот обойдён, взят блок с blockTime >= T0+61",
        an["slot"] == slot0 + 185 and an["signature"] == anchor_sig, an)
    ev = {"signature": sig0, "slot": slot0, "mint": mint, "spend_sol_equiv": 2.01,
          "stable_usd_spent": None, "rate_usd_per_sol": None, "rate_source": None}
    probe = {"lock": threading.Lock()}
    row = analyze_buy(fake, src, ev, buy, w1=30, w2=60, cap=1500,
                      known={"WALLET_B": "BATCH-9", DT8: "copier"}, probe=probe)
    chk("проверка before= на покупке пройдена", probe.get("checked") == 1 and probe.get("failed") == 0, probe)
    chk("индекс покупки в блоке -- из getBlock", row["index_in_block"] == 1, row["index_in_block"])
    chk("толпа 30 с = DT8, A0, A, B (до покупки в её слоте, источник, неуспешная, поздние -- мимо)",
        row["crowd_30s"] == 4, row)
    chk("копировщики задач в толпе опознаны (B и DT8)", row["crowd_30s_copy_wallets"] == 2, row)
    chk("рост к 30 с = x2.5: последняя в слоте по getBlock, а не по порядку узла",
        row["growth_30s"] is not None and abs(row["growth_30s"] - 2.5) < 1e-4, row["growth_30s"])
    chk("рост к 60 с = x1.5 (продажа E), сделка за окном не взята",
        row["growth_60s"] is not None and abs(row["growth_60s"] - 1.5) < 1e-4, row["growth_60s"])
    chk("в окне 30 с 5 успешных после покупки, в 60 с 6",
        row["window_tx_30s"] == 5 and row["window_tx_60s"] == 6, (row["window_tx_30s"], row["window_tx_60s"]))
    chk("DT8: купил в S+0 после источника, место в блоке 2",
        row["dt8"] == "да" and row["dt8_lag_slots"] == 0 and row["dt8_index_in_block"] == 2
        and row["dt8_before_source"] is False, row)
    chk("котировка -- xStock, без пересчёта", str(row["quote_mint"]).startswith("Xsa62"))
    sp, why = spot_after(buy, pool)
    # У мелкой покупки в CPMM спот после неё НИЖЕ цены исполнения: в цене
    # исполнения сидит комиссия пула, а сдвиг от мелкой сделки меньше неё.
    chk("спот после покупки посчитан по остаткам хранилищ и близок к цене исполнения (±5 %)",
        sp is not None and abs(float(sp / pool["price"]) - 1) < 0.05, (sp, pool["price"], why))
    chk("программа пула на настоящей транзакции -- Raydium CPMM (резервы дают спот)",
        pool_program_of(buy, vault) in RESERVE_SPOT_PROGRAMS, pool_program_of(buy, vault))

    # DT8 купил на S+4 -- вне окна; и DT8 в S+2 купил ДРУГОЙ минт -- не считается
    t_d8_late = later("9" * 88, slot0 + 4, bt0 + 2, DT8, dv, q("1.2"), buyer_gain=5)
    t_d8_other = later("7" * 88, slot0 + 2, bt0 + 1, DT8, 0, 0)
    fake_b = FakeRpc(blocks, {vault: [me], mint: [me], DT8: [sg(t_d8_late), sg(t_d8_other)]},
                     {C.first_signature(t_d8_late): t_d8_late, C.first_signature(t_d8_other): t_d8_other})
    row_b = analyze_buy(fake_b, src, ev, buy, w1=30, w2=60, cap=1500, known={},
                        probe={"lock": threading.Lock()})
    chk("DT8 вне S+0..S+3 или с другим минтом -- «нет»", row_b["dt8"] == "нет", row_b)
    chk("без сделок после -- рост 1.0 и пометка «последняя сделка его»",
        row_b["growth_30s"] == 1.0 and row_b["last_trade_is_source_30s"] is True
        and row_b["crowd_30s"] == 0, row_b)
    # предел окна
    row3 = analyze_buy(fake, src, ev, buy, w1=30, w2=60, cap=2, known={},
                       probe={"lock": threading.Lock()})
    chk("окно больше предела -- «нет данных», не ноль",
        row3["crowd_30s"] is None and "предела" in (row3["why_no_crowd"] or ""), row3)
    # ликвидность снята до точки 60 с
    t_r = later("R" * 88, slot0 + 100, bt0 + 40, "MIGRATOR", -5_000_000_000, -1_000_000)
    fake4 = FakeRpc(blocks, {vault: [sg(t_r), sg(t_a), me], mint: [sg(t_a), me], DT8: []},
                    {C.first_signature(t): t for t in (t_a, t_r)})
    row4 = analyze_buy(fake4, src, ev, buy, w1=30, w2=60, cap=1500, known={},
                       probe={"lock": threading.Lock()})
    chk("снятие ликвидности до 60 с -- «нет данных» с причиной, 30 с посчитано",
        row4["growth_60s"] is None and "ликвидность" in (row4["why_no_growth_60s"] or "")
        and row4["growth_30s"] is not None, row4)
    # узел отдаёт историю без покупки -- окно не доверенное
    fake5 = FakeRpc(blocks, {vault: [sg(t_a), sg(t_pre)], mint: [sg(t_a)], DT8: []}, alltx)
    pr5 = {"lock": threading.Lock()}
    row5 = analyze_buy(fake5, src, ev, buy, w1=30, w2=60, cap=1500, known={}, probe=pr5)
    chk("история хранилища без самой покупки -- «нет данных», счётчик сбоев",
        row5["crowd_30s"] is None and "before=" in (row5["why_no_crowd"] or "")
        and pr5.get("failed") == 1, (row5["why_no_crowd"], pr5))
    # нет пула -- причина
    row6 = analyze_buy(fake_b, src, dict(ev, mint="НЕТ_ТАКОГО"), buy, w1=30, w2=60, cap=1500,
                       known={}, probe={"lock": threading.Lock()})
    chk("нет пула -- «нет данных: пул: ...»",
        row6["growth_30s"] is None and (row6["why_no_growth_30s"] or "").startswith("пул:"), row6)
    # покупки нет в блоке -- всё «нет данных»
    blocks7 = dict(blocks)
    blocks7[slot0] = {"blockTime": bt0, "signatures": ["0" * 88]}
    row7 = analyze_buy(FakeRpc(blocks7, {}, {}), src, ev, buy, w1=30, w2=60, cap=1500, known={},
                       probe={"lock": threading.Lock()})
    chk("покупки нет в getBlock -- причина везде",
        "порядка" in (row7["why_no_crowd"] or "") and row7["dt8"] == "нет данных", row7)

    # сводка и сортировка
    res = {"task": "BATCH-3", "address": src, "remark": "t", "sig_scan_complete": True,
           "error": None, "n_tx_fetch_failed": 0, "trades": [row, row_b, row4, row6]}
    ag = aggregate(res)
    chk("trades_7d = число покупок", ag["trades_7d"] == 4, ag)
    chk("share_x2_30s = 2 из 3 посчитанных (x2.5, x1.0, x2.5)", abs(ag["share_x2_30s"] - 2 / 3) < 1e-4, ag)
    chk("n_growth_30s = 3 (у сделки без пула роста нет)", ag["n_growth_30s"] == 3, ag)
    chk("DT8: доля 1 из 4 проверенных, отставание 0 слотов",
        ag["dt8_share"] == 0.25 and ag["dt8_lag_median_slots"] == 0 and ag["n_dt8_checked"] == 4, ag)
    chk("причины «нет данных» перечислены", "пул:" in ag["no_growth_30s_reasons"], ag)
    chk("неполный скан -- число сделок не выдаётся за полное",
        "неполно" in str(aggregate(dict(res, sig_scan_complete=False))["trades_7d"]))
    rows = sorted([dict(ag, growth_after_30s_median=1.1), dict(ag, remark="z", growth_after_30s_median=None),
                   dict(ag, remark="y", growth_after_30s_median=1.9)], key=sort_key)
    chk("сортировка по growth_after_30s, «нет данных» в конце",
        rows[0]["growth_after_30s_median"] == 1.9 and rows[-1]["growth_after_30s_median"] is None,
        [r["growth_after_30s_median"] for r in rows])
    import tempfile  # noqa: PLC0415
    tmp = Path(tempfile.mkdtemp())
    paths = write_outputs("2099-01-01", [ag], dict(ag, source=C.LEADER_BEQV), {"x": 1}, [res], tmp)
    txt = Path(paths["agg"]).read_text(encoding="utf-8")
    chk("CSV: строка лидера отдельной строкой", txt.count("leader,") == 1, txt[:200])
    chk("CSV сделок: 4 строки", len(Path(paths["trades"]).read_text(encoding="utf-8").splitlines()) == 5)
    hdr = txt.splitlines()[0]
    chk("заголовки CSV -- только ASCII", all(ord(c) < 128 for c in hdr), hdr)

    bad = 0
    for name, ok, got in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {name}" + (f"  -> {str(got)[:400]}" if not ok else ""))
        bad += (not ok)
    print(f"самопроверка c2_crowd_metric: {len(checks) - bad}/{len(checks)} пройдено")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
