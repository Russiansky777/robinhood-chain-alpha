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
WINDOW_MAX_PAGES = 10       # 10 000 подписей на минт/хранилище за 60 с
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


def sig_window(rpc, address: str, before: str, until: str) -> tuple[list, bool]:
    out, cur = [], before
    for _ in range(WINDOW_MAX_PAGES):
        page = rpc.signatures(address, before=cur, until=until, limit=1000)
        out.extend(page)
        if len(page) < 1000:
            return out, True
        cur = page[-1]["signature"]
    return out, False


# ------------------------------------------------------------ одна покупка

def price_at(rpc, fetched: dict, vault_sigs: list, t_point: int, pool: dict,
             buy_sig: str) -> dict:
    """Цена последней сделки в пуле с blockTime <= t_point (vault_sigs --
    от новых к старым, только успешные, только после покупки)."""
    cands = [s for s in vault_sigs if s["blockTime"] <= t_point]
    for s in cands:
        sig = s["signature"]
        if sig not in fetched:
            fetched.update(rpc.get_txs([sig]))
        tx = fetched.get(sig)
        if tx is None:
            return {"price": None, "why": f"узел не отдал транзакцию пула {sig[:12]}"}
        ev = C.pool_event(tx, pool)
        if ev["kind"] == "swap":
            return {"price": ev["price"], "sig": sig, "block_time": s["blockTime"],
                    "last_trade_is_source": False}
        if ev["kind"] == "removal":
            return {"price": None, "why": f"ликвидность пула снята до точки ({sig[:12]})"}
    return {"price": pool["price"], "sig": buy_sig, "block_time": None,
            "last_trade_is_source": True}


def analyze_buy(rpc, source: str, ev: dict, tx: dict, *, w1: int, w2: int,
                cap: int, known: dict, probe: dict) -> dict:
    sig, slot, bt, mint = ev["signature"], ev["slot"], tx.get("blockTime"), ev["mint"]
    row = {"signature": sig, "slot": slot, "block_time": bt, "block_time_utc": C.utc(bt),
           "mint": mint, "spend_sol_equiv": round(ev["spend_sol_equiv"], 6),
           "stable_usd_spent": ev.get("stable_usd_spent"),
           "rate_usd_per_sol": ev.get("rate_usd_per_sol"), "rate_source": ev.get("rate_source"),
           "pool_vault": None, "pool_owner": None, "quote_mint": None, "split_route": None,
           "price_0": None, "price_30s": None, "price_30s_sig": None, "last_trade_is_source_30s": None,
           "growth_30s": None, "why_no_growth_30s": None,
           "price_60s": None, "price_60s_sig": None, "last_trade_is_source_60s": None,
           "growth_60s": None, "why_no_growth_60s": None,
           "crowd_30s": None, "crowd_30s_copy_wallets": None, "crowd_30s_rule2": None,
           "why_no_crowd": None, "window_tx_30s": None, "window_tx_60s": None,
           "anchor_slot": None}
    if bt is None:
        row["why_no_growth_30s"] = row["why_no_growth_60s"] = row["why_no_crowd"] = "нет blockTime"
        return row
    pool = C.identify_pool(tx, source, mint)
    row.update(pool_vault=pool["pool_vault"], pool_owner=pool["pool_owner"],
               quote_mint=pool["quote_mint"], split_route=pool["split"],
               price_0=str(pool["price"]) if pool["price"] is not None else None)
    anchor = find_anchor(rpc, slot, bt, bt + w2 + 1)
    row["anchor_slot"] = anchor["slot"]

    # Проверка семантики before= на живых данных: подпись якоря не про наш
    # адрес, и узел обязан отсчитывать по её слоту. Делается один раз.
    with probe["lock"]:
        need_probe = pool["ok"] and probe.get("result") is None
    if need_probe:
        page = rpc.signatures(pool["pool_vault"], before=anchor["signature"], limit=1000)
        found = any(s.get("signature") == sig for s in page)
        slots = [s.get("slot") for s in page if s.get("slot") is not None]
        verdict = ("ok" if found else
                   ("inconclusive" if len(page) == 1000 and slots and min(slots) > slot else "FAIL"))
        with probe["lock"]:
            if probe.get("result") in (None, "inconclusive"):
                probe["result"] = verdict
                probe["detail"] = {"vault": pool["pool_vault"], "buy_sig": sig,
                                   "anchor_slot": anchor["slot"], "returned": len(page),
                                   "buy_found": found}
        if verdict == "FAIL":
            raise RuntimeError("семантика before= у узла не та: подпись покупки не найдена "
                               "в истории хранилища до якоря -- окна считать нельзя")

    mint_sigs, m_ok = sig_window(rpc, mint, anchor["signature"], sig)
    vault_sigs, v_ok = (sig_window(rpc, pool["pool_vault"], anchor["signature"], sig)
                        if pool["ok"] else ([], True))

    def clean(lst):
        out = []
        for s in lst:
            if s.get("err") is not None or s.get("blockTime") is None:
                continue
            if s.get("signature") == sig or (s.get("slot") or 0) < slot:
                raise RuntimeError("until= отдал подпись до покупки -- окно считать нельзя")
            if s["blockTime"] <= bt + w2:
                out.append(s)
        return out
    mint_ok, vault_ok = clean(mint_sigs), clean(vault_sigs)
    union: dict = {}
    for s in mint_ok + vault_ok:
        union.setdefault(s["signature"], s)
    in30 = [s for s in union.values() if s["blockTime"] <= bt + w1]
    row["window_tx_30s"] = len(in30)
    row["window_tx_60s"] = len(union)
    fetched: dict = {}

    if not m_ok or not v_ok:
        row["why_no_crowd"] = "окно не выкачано целиком (> 10 000 подписей)"
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
        pa = price_at(rpc, fetched, vault_ok, bt + w, pool, sig)
        if pa.get("price") is None:
            row[f"why_no_growth_{tag}"] = pa.get("why")
            continue
        row[f"price_{tag}"] = str(pa["price"])
        row[f"price_{tag}_sig"] = pa.get("sig")
        row[f"last_trade_is_source_{tag}"] = pa.get("last_trade_is_source")
        row[f"growth_{tag}"] = round(float(pa["price"] / p0), 6)
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
                with cache_lock:
                    cls[s] = rec
            if (i // CLASSIFY_BATCH) % max(1, log_every // CLASSIFY_BATCH or 1) == 0:
                C.log(f"{src.get('remark') or addr[:8]}: разобрано {min(i + CLASSIFY_BATCH, len(todo))}"
                      f"/{len(todo)} новых транзакций")
        buys = []
        for s in ok:
            rec = cls.get(s)
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
            if hit and hit.get("_w") == [w1, w2, cap]:
                out["trades"].append(hit)
                continue
            tx = buys_tx.get(s) or rpc.get_txs([s]).get(s)
            if tx is None:
                row = {"signature": s, "mint": ev["mint"], "slot": ev["slot"],
                       "why_no_growth_30s": "узел не отдал транзакцию покупки",
                       "why_no_growth_60s": "узел не отдал транзакцию покупки",
                       "why_no_crowd": "узел не отдал транзакцию покупки"}
            else:
                row = analyze_buy(rpc, addr, ev, tx, w1=w1, w2=w2, cap=cap, known=known, probe=probe)
            row["_w"] = [w1, w2, cap]
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


AGG_COLS = ["task", "source", "remark", "trades_7d", "growth_30s_median", "growth_60s_median",
            "share_x2_30s", "crowd_30s_median", "n_growth_30s", "n_growth_60s", "n_crowd_30s",
            "no_growth_30s_reasons", "no_crowd_reasons", "buys_rate_missing", "first_entries_lt2",
            "multi_mint_skipped", "n_signatures_window", "tx_fetch_failed", "scan_complete", "error"]
TRADE_COLS = ["task", "source", "remark", "signature", "slot", "block_time_utc", "mint",
              "spend_sol_equiv", "stable_usd_spent", "rate_usd_per_sol", "rate_source",
              "pool_vault", "pool_owner", "quote_mint", "split_route", "price_0",
              "price_30s", "growth_30s", "last_trade_is_source_30s", "why_no_growth_30s",
              "price_60s", "growth_60s", "last_trade_is_source_60s", "why_no_growth_60s",
              "crowd_30s", "crowd_30s_copy_wallets", "crowd_30s_rule2", "why_no_crowd",
              "window_tx_30s", "window_tx_60s", "price_30s_sig", "price_60s_sig", "anchor_slot"]


def sort_key(a: dict):
    s = a.get("share_x2_30s")
    return (s is None, -(s or 0), a.get("task") or "", a.get("remark") or "")


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
    body = {"schema_version": 1, **meta, "sources_sorted_by_share_x2_30s": rows,
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
    leader_row = None
    if leader_src:
        leader_row = next((x for x in agg if x["source"] == C.LEADER_BEQV), None)
        if leader_in is None:
            agg = [x for x in agg if x["source"] != C.LEADER_BEQV]
    meta = {"generated_utc": C.utc(time.time()), "window_days": a.days,
            "window_from_utc": C.utc(now - int(a.days * 86400)), "window_to_utc": C.utc(now),
            "crowd_window_s": a.crowd_window_s, "window2_s": a.window2_s, "crowd_cap": a.crowd_cap,
            "sources_origin": "DBot GET /automation/follow_orders (живьём)",
            "n_sources_batch3_batch5": len(sources),
            "leader": C.LEADER_BEQV, "leader_in_batch": leader_in["task"] if leader_in else None,
            "before_semantics_probe": {k: v for k, v in probe.items() if k != "lock"},
            "rate_stats": book.stats, "rpc_stats": rpc.stats,
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
                "crowd": "разные чужие кошельки, купившие минт в (T0, T0+30 с]"}}
    paths = write_outputs(date, agg, leader_row, meta, results)
    C.log(f"выгрузка: {paths}")
    C.log(f"кредитов за прогон: {rpc.stats.get('кредитов')}, C2 сегодня: {C.c2_spent_today()}")
    print("--- коротко (сортировка по share_x2_30s) ---")
    for r in sorted(agg, key=sort_key) + ([leader_row] if leader_row and leader_in is None else []):
        print(f"{r['task']:<8} {str(r['remark'])[:14]:<14} n={r['trades_7d']} "
              f"g30={r['growth_30s_median']} g60={r['growth_60s_median']} "
              f"x2={r['share_x2_30s']} crowd={r['crowd_30s_median']} ошибка={r['error']}")
    return 0


# ------------------------------------------------------------ самопроверка

class FakeRpc:
    """Подставной узел: подписи и транзакции из словарей, блоки по слотам."""

    def __init__(self, blocks: dict, sigs_by_addr: dict, txs: dict) -> None:
        self.blocks, self.sigs_by_addr, self.txs = blocks, sigs_by_addr, txs
        self.calls: list = []
        self.stats = {"кредитов": 0}

    def call(self, method, params, **kw):
        self.calls.append(method)
        if method == "getBlock":
            s = params[0]
            if s not in self.blocks:
                raise RuntimeError("getBlock: RPC error {'code': -32007, 'message': 'skipped'}")
            return self.blocks[s]
        raise RuntimeError(f"нет {method}")

    def signatures(self, address, *, before=None, until=None, limit=1000):
        self.calls.append("getSignaturesForAddress")
        lst = self.sigs_by_addr.get(address, [])   # от новых к старым
        i0 = 0
        if before:
            bslot = self.slot_of(before)
            i0 = next((i for i, s in enumerate(lst) if s["slot"] < bslot), len(lst))
        out = []
        for s in lst[i0:]:
            if until and s["signature"] == until:
                break
            if until and s["slot"] < self.slot_of(until):
                break
            out.append(s)
            if len(out) >= limit:
                break
        return out

    def slot_of(self, sig):
        for b_slot, b in self.blocks.items():
            if sig in (b.get("signatures") or []):
                return b_slot
        for lst in self.sigs_by_addr.values():
            for s in lst:
                if s["signature"] == sig:
                    return s["slot"]
        raise KeyError(sig)

    def get_txs(self, sigs):
        self.calls.append("getTransaction")
        return {s: self.txs.get(s) for s in sigs}


def self_test() -> int:
    checks: list = []

    def chk(name, ok, got=""):
        checks.append((name, bool(ok), got))

    txs = C.load_real_txs()
    buy = next(t for s, t in txs.items() if s.startswith("2uPwpSAQ"))
    src = "BmjAUDbwBMxR5shrmzBtKRwveVahFGFiEH3oTq7QTHnu"
    mint = "87pa2UbBB2dhD4b7CHDPHzzKjrdrUtEcBHueXnVz6CJp"
    pool = C.identify_pool(buy, src, mint)
    slot0, bt0, sig0 = buy["slot"], buy["blockTime"], C.first_signature(buy)
    vault, qv = pool["pool_vault"], pool["quote_vault"]
    keys0 = C.account_keys(buy)

    def later(sig, slot, bt, signer, d_vault, d_quote, buyer_gain=0, err=None, recv=None):
        """Сделка в том же пуле: дельты хранилищ (сырые) + прирост покупателю."""
        keys = [signer, vault, qv, "BUYER_ATA", "RECV_ATA"]
        pre = [{"accountIndex": 1, "owner": pool["pool_owner"], "mint": mint,
                "uiTokenAmount": {"amount": "1000000000000000", "decimals": 6}},
               {"accountIndex": 2, "owner": pool["pool_owner"], "mint": pool["quote_mint"],
                "uiTokenAmount": {"amount": "1000000000000", "decimals": 8}}]
        post = [{"accountIndex": 1, "owner": pool["pool_owner"], "mint": mint,
                 "uiTokenAmount": {"amount": str(1000000000000000 + d_vault), "decimals": 6}},
                {"accountIndex": 2, "owner": pool["pool_owner"], "mint": pool["quote_mint"],
                 "uiTokenAmount": {"amount": str(1000000000000 + d_quote), "decimals": 8}}]
        if buyer_gain:
            owner = recv or signer
            post.append({"accountIndex": 3 if not recv else 4, "owner": owner, "mint": mint,
                         "uiTokenAmount": {"amount": str(buyer_gain), "decimals": 6}})
        return {"slot": slot, "blockTime": bt,
                "transaction": {"signatures": [sig], "message": {
                    "accountKeys": [{"pubkey": k, "signer": k == signer} for k in keys],
                    "instructions": [{"programId": "DEX", "accounts": [vault, qv]}]}},
                "meta": {"err": err, "preBalances": [0] * 5, "postBalances": [0] * 5,
                         "preTokenBalances": pre, "postTokenBalances": post,
                         "innerInstructions": []}}
    # цена P0: 3061569e-8 / 162352420501e-6
    p0 = pool["price"]
    # сделка через 10 с: покупка по цене ×2.5 от P0 (кошелёк A)
    dv = -1_000_000_000  # 1000 токенов
    dq = int(D(1000) * p0 * D("2.5") * D(10) ** 8)
    t_a = later("A" * 88, slot0 + 25, bt0 + 10, "WALLET_A", dv, dq, buyer_gain=1_000_000_000)
    # через 20 с: покупка кошельком B, минт в другом пуле (вижу только по минту)
    t_b = later("B" * 88, slot0 + 50, bt0 + 20, "WALLET_B", 0, 0, buyer_gain=5)
    # через 25 с: сам источник докупает -- в толпу не идёт
    t_s = later("C" * 88, slot0 + 62, bt0 + 25, src, 0, 0, buyer_gain=7)
    # через 29 с: неуспешная транзакция -- мимо
    t_f = later("D" * 88, slot0 + 72, bt0 + 29, "WALLET_F", dv, dq, buyer_gain=1, err={"x": 1})
    # через 45 с: продажа по цене ×1.5
    dq2 = int(D(1000) * p0 * D("1.5") * D(10) ** 8)
    t_e = later("E" * 88, slot0 + 112, bt0 + 45, "WALLET_E", 1_000_000_000, -dq2)
    # через 70 с -- вне окна 60 с
    t_g = later("G" * 88, slot0 + 175, bt0 + 70, "WALLET_G", dv, dq * 10, buyer_gain=1)

    def s(t):
        return {"signature": C.first_signature(t), "slot": t["slot"], "blockTime": t["blockTime"],
                "err": t["meta"]["err"]}
    anchor_sig = "Z" * 88
    blocks = {slot0: {"blockTime": bt0, "signatures": [sig0]},
              # якорь: первая догадка slot0+157 пропущена, дальше -- рано, потом поздно
              slot0 + 158: {"blockTime": bt0 + 55, "signatures": ["Y" * 88]},
              slot0 + 185: {"blockTime": bt0 + 65, "signatures": ["X" * 88, anchor_sig]}}
    vault_hist = [s(t_g), s(t_e), s(t_f), s(t_a), {"signature": sig0, "slot": slot0,
                                                   "blockTime": bt0, "err": None}]
    mint_hist = [s(t_g), s(t_f), s(t_s), s(t_b), s(t_a), {"signature": sig0, "slot": slot0,
                                                           "blockTime": bt0, "err": None}]
    fake = FakeRpc(blocks, {vault: vault_hist, mint: mint_hist},
                   {C.first_signature(t): t for t in (t_a, t_b, t_s, t_f, t_e, t_g)})
    # якорь
    an = find_anchor(fake, slot0, bt0, bt0 + 61)
    chk("якорь: пропущенный слот обойдён, взят блок с blockTime >= T0+61",
        an["slot"] == slot0 + 185 and an["signature"] == anchor_sig, an)
    ev = {"signature": sig0, "slot": slot0, "mint": mint, "spend_sol_equiv": 2.01,
          "stable_usd_spent": None, "rate_usd_per_sol": None, "rate_source": None}
    probe = {"lock": threading.Lock(), "result": None}
    row = analyze_buy(fake, src, ev, buy, w1=30, w2=60, cap=1500,
                      known={"WALLET_B": "BATCH-9"}, probe=probe)
    chk("проверка before= на хранилище пройдена", probe["result"] == "ok", probe)
    chk("толпа 30 с = A и B (источник, неуспешная и поздние мимо)", row["crowd_30s"] == 2, row)
    chk("копировщик задачи в толпе опознан", row["crowd_30s_copy_wallets"] == 1, row)
    chk("рост к 30 с = ×2.5 по последней сделке пула (A)",
        row["growth_30s"] is not None and abs(row["growth_30s"] - 2.5) < 1e-4, row["growth_30s"])
    chk("рост к 60 с = ×1.5 (продажа E), сделка за окном не взята",
        row["growth_60s"] is not None and abs(row["growth_60s"] - 1.5) < 1e-4, row["growth_60s"])
    chk("в окне 30 с 3 успешные транзакции (A, B, докупка источника), в окне 60 с 4 (+E)",
        row["window_tx_30s"] == 3 and row["window_tx_60s"] == 4, (row["window_tx_30s"], row["window_tx_60s"]))
    chk("котировка -- xStock, без пересчёта", str(row["quote_mint"]).startswith("Xsa62"))

    # без сделок после источника: рост 1.0, помечено
    fake2 = FakeRpc(blocks, {vault: [vault_hist[-1]], mint: [mint_hist[-1]]}, {})
    row2 = analyze_buy(fake2, src, ev, buy, w1=30, w2=60, cap=1500, known={},
                       probe={"lock": threading.Lock(), "result": "ok"})
    chk("без сделок после -- рост 1.0 и пометка «последняя сделка его»",
        row2["growth_30s"] == 1.0 and row2["last_trade_is_source_30s"] is True
        and row2["crowd_30s"] == 0, row2)
    # предел окна
    row3 = analyze_buy(fake, src, ev, buy, w1=30, w2=60, cap=2, known={},
                       probe={"lock": threading.Lock(), "result": "ok"})
    chk("окно больше предела -- «нет данных», не ноль",
        row3["crowd_30s"] is None and "предела" in (row3["why_no_crowd"] or ""), row3)
    # ликвидность снята до точки 60 с
    t_r = later("R" * 88, slot0 + 100, bt0 + 40, "MIGRATOR", -5_000_000_000, -1_000_000)
    fake4 = FakeRpc(blocks, {vault: [s(t_r), s(t_a), vault_hist[-1]], mint: [s(t_a), mint_hist[-1]]},
                    {C.first_signature(t): t for t in (t_a, t_r)})
    row4 = analyze_buy(fake4, src, ev, buy, w1=30, w2=60, cap=1500, known={},
                       probe={"lock": threading.Lock(), "result": "ok"})
    chk("снятие ликвидности до 60 с -- «нет данных» с причиной, 30 с посчитано",
        row4["growth_60s"] is None and "ликвидность" in (row4["why_no_growth_60s"] or "")
        and row4["growth_30s"] is not None, row4)
    # пул не найден -- причина
    ev_bad = dict(ev, mint="НЕТ_ТАКОГО")
    row5 = analyze_buy(fake2, src, ev_bad, buy, w1=30, w2=60, cap=1500, known={},
                       probe={"lock": threading.Lock(), "result": "ok"})
    chk("нет пула -- «нет данных: пул: ...»",
        row5["growth_30s"] is None and (row5["why_no_growth_30s"] or "").startswith("пул:"), row5)

    # сводка и сортировка
    res = {"task": "BATCH-3", "address": src, "remark": "t", "sig_scan_complete": True,
           "error": None, "n_tx_fetch_failed": 0,
           "trades": [row, row2, row4, row5]}
    ag = aggregate(res)
    chk("trades_7d = число покупок", ag["trades_7d"] == 4, ag)
    chk("share_x2_30s = 2 из 3 посчитанных (×2.5, ×1.0, ×2.5)", abs(ag["share_x2_30s"] - 2 / 3) < 1e-4, ag)
    chk("n_growth_30s = 3 (у сделки без пула роста нет)", ag["n_growth_30s"] == 3, ag)
    chk("медиана роста 30 с по посчитанным", ag["growth_30s_median"] is not None, ag)
    chk("причины «нет данных» перечислены", "пул:" in ag["no_growth_30s_reasons"], ag)
    res_bad = dict(res, sig_scan_complete=False)
    chk("неполный скан -- число сделок не выдаётся за полное",
        "неполно" in str(aggregate(res_bad)["trades_7d"]))
    rows = sorted([ag, dict(ag, remark="z", share_x2_30s=None), dict(ag, remark="y", share_x2_30s=0.9)],
                  key=sort_key)
    chk("сортировка по share_x2_30s, «нет данных» в конце",
        [r["share_x2_30s"] for r in rows][0] == 0.9 and rows[-1]["share_x2_30s"] is None,
        [r["share_x2_30s"] for r in rows])
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
