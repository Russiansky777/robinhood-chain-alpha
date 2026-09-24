#!/usr/bin/env python3
"""Задача B (C2): сэндвичи на наших покупках -- гейт перед размером 0.5 SOL.
Только чтение цепи. Ни одного ордера, ни одной записи в DBot.

Выборки:
* наши боевые сделки -- копия журнала исполнителя
  (data/bloom_seller_diag.json, positions_rows, mode=live; кошелёк 4s87RRC2...);
* покупки DBot BATCH-5 за N дней (по умолчанию 7) -- по цепи, по кошельку
  задачи 5Y8h877s...: первый вход -- методом проекта classify_tx (без
  порога: DBot покупает на 0.2 SOL), докупка -- рост уже имевшегося минта
  при трате SOL/WSOL.

Сэндвич (определение владельца): в блоке нашей покупки есть кошелёк,
который КУПИЛ тот же минт раньше нас в этом блоке и ПРОДАЛ его позже нас
-- в этом же блоке или в следующем произведённом блоке (пропущенные слоты
не считаются блоком). Порядок -- по индексу в getBlock
(transactionDetails=accounts), не по подписям.

По каждому сэндвичу: адрес, его вход и выход (SOL: натив + WSOL; стейбл --
отдельно в USD, не смешивается), его прибыль в SOL, и наша потеря в п.п.
относительно цены ДО его входа: (наша цена исполнения в нашем пуле / цена
последней сделки в том же пуле перед его входом - 1) * 100. Цена -- цена
исполнения по хранилищам пула, котировка -- своя у пула. Входит в неё и
наше собственное влияние на пул -- это видно отдельной колонкой
«его сдвиг» (его цена в нашем пуле к цене до него), если он торговал в
нашем пуле.

Выход: data/sandwich_<UTC-дата>.csv и data/sandwich_<UTC-дата>.json.
Учёт кредитов: служба c2_sandwich, общий потолок C2 100 000 в сутки.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402

SERVICE = "c2_sandwich"
NEXT_BLOCK_TRIES = 6
BLOCK_OPTS = {"encoding": "jsonParsed", "transactionDetails": "accounts",
              "maxSupportedTransactionVersion": C.TX_VERSION, "rewards": False,
              "commitment": "finalized"}


class TimeUp(Exception):
    pass


def get_block(rpc, slot: int) -> dict | None:
    """Блок или None, если слот пропущен."""
    try:
        return rpc.call("getBlock", [slot, BLOCK_OPTS])
    except RuntimeError as exc:
        if C.is_skipped_slot_error(str(exc)):
            return None
        raise


def next_block(rpc, slot: int) -> tuple[int | None, dict | None]:
    for s in range(slot + 1, slot + 1 + NEXT_BLOCK_TRIES):
        b = get_block(rpc, s)
        if b is not None:
            return s, b
    return None, None


def buy_of_wallet(rc, tx: dict, wallet: str) -> dict | None:
    """Покупка кошелька задачи: первый вход (classify_tx) или докупка."""
    ev = C.classify_first_entry(rc, tx, wallet)
    if ev and ev.get("kind") == "first_entry":
        return {"kind": "first_entry", "mint": ev["mint"], "spend_sol": ev["spend_sol_equiv"]}
    if ev and ev.get("kind") == "rate_missing":
        return {"kind": "rate_missing", "mint": None, "spend_sol": None}
    if (tx.get("meta") or {}).get("err") is not None or wallet not in C.signers(tx):
        return None
    if not (rc.tx_program_ids(tx) & rc.DEX_PROGRAMS):
        return None
    meta = tx.get("meta") or {}
    pre = rc.mint_balance_map(meta.get("preTokenBalances"), wallet)
    post = rc.mint_balance_map(meta.get("postTokenBalances"), wallet)
    grown = [m for m, v in post.items() if m not in (C.WSOL, C.USDC, C.USDT)
             and pre.get(m, D(0)) > 0 and v > pre.get(m, D(0))]
    if len(grown) != 1:
        return None
    qs = C.quote_spend(tx, wallet)
    spend = qs["sol"] + qs["wsol"]
    if spend <= D("0.001"):
        return None
    return {"kind": "add", "mint": grown[0], "spend_sol": float(spend)}


def price_before(rpc, blk_txs: list, j: int, pool: dict) -> dict:
    """Цена последней сделки в нашем пуле перед индексом j блока; если в
    блоке её нет -- по истории хранилища до первой транзакции блока."""
    for k in range(j - 1, -1, -1):
        ev = C.pool_event(blk_txs[k], pool)
        if ev["kind"] == "swap":
            return {"price": ev["price"], "where": f"тот же блок, индекс {k}"}
        if ev["kind"] == "removal":
            return {"price": None, "why": "перед входом снята ликвидность пула"}
    first = C.first_signature(blk_txs[0]) if blk_txs else None
    if not first:
        return {"price": None, "why": "в блоке нет транзакций"}
    page = rpc.signatures(pool["pool_vault"], before=first, limit=10)
    for s in page:
        if s.get("err") is not None:
            continue
        tx = rpc.get_tx(s["signature"])
        if tx is None:
            continue
        ev = C.pool_event(tx, pool)
        if ev["kind"] == "swap":
            return {"price": ev["price"], "where": f"слот {s.get('slot')}"}
        if ev["kind"] == "removal":
            return {"price": None, "why": "перед входом снята ликвидность пула"}
    return {"price": None, "why": "в 10 предыдущих транзакциях хранилища нет сделки"}


def analyze_trade(rpc, kind: str, sig: str, wallet: str, mint: str | None, tx: dict | None,
                  *, sources: set, known: dict) -> dict:
    row = {"kind": kind, "signature": sig, "wallet": wallet, "mint": mint, "slot": None,
           "block_time_utc": None, "spend_sol": None, "index_in_block": None,
           "block_tx_count": None, "next_block_slot": None, "sandwich": "нет данных",
           "n_attackers": 0, "attacker": None, "attacker_is_source": None, "attacker_known_as": None,
           "front_sig": None, "back_sig": None, "back_where": None,
           "attacker_entry_sol": None, "attacker_exit_sol": None, "attacker_entry_usd": None,
           "attacker_exit_usd": None, "attacker_profit_sol": None, "same_pool": None,
           "quote_mint": None, "price_before": None, "our_price": None, "loss_pp": None,
           "front_vs_before_pp": None, "all_attackers": None, "why_no_data": None}
    if tx is None:
        tx = rpc.get_tx(sig)
    if tx is None:
        row["why_no_data"] = "узел не отдал нашу транзакцию"
        return row
    if (tx.get("meta") or {}).get("err") is not None:
        row["why_no_data"] = "наша транзакция неуспешна"
        return row
    if mint is None:
        od = [r["mint"] for r in C.token_rows(tx).values()
              if r["owner"] == wallet and r["post"] > r["pre"]
              and r["mint"] not in (C.WSOL, C.USDC, C.USDT)]
        mint = od[0] if len(set(od)) == 1 else None
        row["mint"] = mint
    if mint is None:
        row["why_no_data"] = "минт покупки не определился однозначно"
        return row
    slot = tx.get("slot")
    row["slot"], row["block_time_utc"] = slot, C.utc(tx.get("blockTime"))
    qs = C.quote_spend(tx, wallet)
    row["spend_sol"] = float(qs["sol"] + qs["wsol"])
    pool = C.identify_pool(tx, wallet, mint)
    if pool["ok"]:
        row["quote_mint"], row["our_price"] = pool["quote_mint"], str(pool["price"])
    blk = get_block(rpc, slot)
    if blk is None:
        row["why_no_data"] = f"блок {slot} не отдался"
        return row
    btx = blk.get("transactions") or []
    row["block_tx_count"] = len(btx)
    idx = next((i for i, t in enumerate(btx) if C.first_signature(t) == sig), None)
    if idx is None:
        row["why_no_data"] = "нашей подписи нет в блоке"
        return row
    row["index_in_block"] = idx
    fronts = []
    for j in range(idx):
        for w in C.mint_buyers(btx[j], mint):
            if w != wallet:
                fronts.append((j, w))
    ns, nb = next_block(rpc, slot)
    row["next_block_slot"] = ns
    later = [(k, "same_block", btx[k]) for k in range(idx + 1, len(btx))]
    later += [(k, "next_block", t) for k, t in enumerate((nb or {}).get("transactions") or [])]
    found = []
    for j, w in fronts:
        back = next(((k, where, t) for k, where, t in later if w in C.mint_sellers(t, mint)), None)
        if back:
            found.append((j, w, back))
    if nb is None and not found:
        row["sandwich"] = "нет данных"
        row["why_no_data"] = f"следующий блок не найден за {NEXT_BLOCK_TRIES} слотов"
        return row
    if not found:
        row["sandwich"] = "нет"
        return row
    row["sandwich"] = "да"
    row["n_attackers"] = len({w for _, w, _ in found})
    row["all_attackers"] = ";".join(sorted({w for _, w, _ in found}))
    # главный -- с наибольшим входом
    def entry(f):
        q = C.quote_spend(btx[f[0]], f[1])
        return q["sol"] + q["wsol"] + q["usd"] / D(1000)
    j, w, (k, where, bt) = max(found, key=entry)
    qin, qout = C.quote_spend(btx[j], w), C.quote_spend(bt, w)
    row.update(attacker=w, front_sig=C.first_signature(btx[j]), back_sig=C.first_signature(bt),
               back_where=where, attacker_is_source=w in sources,
               attacker_known_as=known.get(w),
               attacker_entry_sol=float(qin["sol"] + qin["wsol"]),
               attacker_exit_sol=float(-(qout["sol"] + qout["wsol"])),
               attacker_entry_usd=float(qin["usd"]) if qin["usd"] else None,
               attacker_exit_usd=float(-qout["usd"]) if qout["usd"] else None)
    row["attacker_profit_sol"] = round(row["attacker_exit_sol"] - row["attacker_entry_sol"], 9)
    if not pool["ok"]:
        row["why_no_data"] = f"потеря в п.п.: пул: {pool['why_not']}"
        return row
    fe = C.pool_event(btx[j], pool)
    row["same_pool"] = fe["kind"] == "swap"
    pb = price_before(rpc, btx, j, pool)
    if pb.get("price") is None:
        row["why_no_data"] = f"потеря в п.п.: {pb.get('why')}"
        return row
    row["price_before"] = str(pb["price"])
    row["loss_pp"] = round(float((pool["price"] / pb["price"] - 1) * 100), 4)
    if fe["kind"] == "swap":
        row["front_vs_before_pp"] = round(float((fe["price"] / pb["price"] - 1) * 100), 4)
    return row


def dbot_buys(rpc, rc, wallet: str, days: float, now: int) -> tuple[list, dict]:
    cutoff = now - int(days * 86400)
    sigs, before, complete = [], None, False
    for _ in range(40):
        page = rpc.signatures(wallet, before=before, limit=1000)
        sigs.extend(page)
        if len(page) < 1000 or (page[-1].get("blockTime") or 0) < cutoff:
            complete = True
            break
        before = page[-1]["signature"]
    win = [s for s in sigs if s.get("blockTime") and s["blockTime"] >= cutoff and s.get("err") is None]
    got = rpc.get_txs([s["signature"] for s in win])
    out, st = [], {"signatures_ok": len(win), "scan_complete": complete, "fetch_failed": 0,
                   "first_entry": 0, "add": 0, "rate_missing": 0}
    for s in win:
        tx = got.get(s["signature"])
        if tx is None:
            st["fetch_failed"] += 1
            continue
        b = buy_of_wallet(rc, tx, wallet)
        if not b:
            continue
        st[b["kind"]] += 1
        if b["kind"] == "rate_missing":
            continue
        out.append({"signature": s["signature"], "mint": b["mint"], "tx": tx,
                    "buy_kind": b["kind"]})
    out.sort(key=lambda x: x["tx"].get("slot") or 0)
    return out, st


def summarize(rows: list) -> dict:
    with_data = [r for r in rows if r["sandwich"] in ("да", "нет")]
    sw = [r for r in rows if r["sandwich"] == "да"]
    losses = [r["loss_pp"] for r in sw if r.get("loss_pp") is not None]
    att: dict = {}
    for r in sw:
        for w in (r.get("all_attackers") or "").split(";"):
            if w:
                att[w] = att.get(w, 0) + 1
    return {"n_trades": len(rows), "n_with_data": len(with_data), "n_sandwiched": len(sw),
            "share_sandwiched": round(len(sw) / len(with_data), 4) if with_data else None,
            "median_loss_pp": round(C.median(losses), 4) if losses else None,
            "n_loss_measured": len(losses),
            "no_data_reasons": {k: sum(1 for r in rows if r.get("why_no_data") == k)
                                for k in {r.get("why_no_data") for r in rows if r.get("why_no_data")}},
            "attackers": sorted(att.items(), key=lambda kv: -kv[1])}


COLS = ["kind", "signature", "slot", "block_time_utc", "wallet", "mint", "spend_sol",
        "index_in_block", "block_tx_count", "next_block_slot", "sandwich", "n_attackers",
        "attacker", "attacker_is_source", "attacker_known_as", "front_sig", "back_sig", "back_where",
        "attacker_entry_sol", "attacker_exit_sol", "attacker_entry_usd", "attacker_exit_usd",
        "attacker_profit_sol", "same_pool", "quote_mint", "price_before", "our_price", "loss_pp",
        "front_vs_before_pp", "all_attackers", "why_no_data"]


def write_outputs(date: str, rows: list, meta: dict, out_dir: Path = C.DATA) -> dict:
    p_csv, p_json = out_dir / f"sandwich_{date}.csv", out_dir / f"sandwich_{date}.json"
    with open(p_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in COLS})
    p_json.write_text(json.dumps({"schema_version": 1, **meta, "rows": rows}, ensure_ascii=False,
                                 indent=1, default=str), encoding="utf-8")
    return {"csv": str(p_csv), "json": str(p_json)}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument("--dbot-wallet", default=C.BATCH5_WALLET)
    p.add_argument("--skip-dbot", action="store_true")
    p.add_argument("--time-budget-s", type=int, default=7000)
    a = p.parse_args()
    if a.self_test:
        return self_test()
    key = C.RC.helius_key()[0]
    if not key:
        print("СТОП: ключ Helius не задан (секрет HELIUS_API)", file=sys.stderr)
        return 2
    t0, now, date = time.time(), int(time.time()), C.today_utc()
    C.log(f"C2 задача B: потрачено C2 сегодня до старта {C.c2_spent_today()} из {C.C2_DAILY_BUDGET}")
    rpc = C.C2Rpc(SERVICE, key=key)
    rpc.deadline = time.monotonic() + a.time_budget_s
    orig = rpc.check_budget

    def chk_time(n):
        if rpc.expired():
            raise TimeUp("бюджет времени прогона истёк")
        orig(n)
    rpc.check_budget = chk_time
    rc = C.load_rpc_check()
    C.install_onchain_rate(rc, C.RateBook(rpc))

    sources5, known, tasks_origin = set(), {C.EXECUTOR_WALLET: "bloom_executor"}, None
    dkey = (os.environ.get("DBOT_API_KEY") or "").strip()
    if dkey:
        try:
            tasks = C.DbotReadOnly(dkey).tasks()
            known.update(C.task_wallets(tasks))
            sources5 = {s["address"] for s in C.sources_of_tasks(tasks, ("BATCH-5", "BATCH-3"))}
            tasks_origin = "DBot GET живьём"
        except Exception as exc:  # noqa: BLE001
            tasks_origin = f"DBot GET не удался: {type(exc).__name__}"
    rows, err = [], None
    ours = C.executor_trades()
    try:
        for t in ours:
            r = analyze_trade(rpc, "ours_live", t["signature"], t["wallet"], t["mint"], None,
                              sources=sources5, known=known)
            r["journal_ts_intent_utc"] = t["ts_intent_utc"]
            rows.append(r)
            C.log(f"наша {t['ts_intent_utc']}: сэндвич {r['sandwich']} {r.get('why_no_data') or ''}")
        dstat = None
        if not a.skip_dbot:
            buys, dstat = dbot_buys(rpc, rc, a.dbot_wallet, a.days, now)
            C.log(f"DBot BATCH-5: покупок за {a.days} сут: {len(buys)} ({dstat})")
            for b in buys:
                r = analyze_trade(rpc, f"dbot_batch5_{b['buy_kind']}", b["signature"], a.dbot_wallet,
                                  b["mint"], b["tx"], sources=sources5, known=known)
                rows.append(r)
    except (C.BudgetExceeded, TimeUp) as exc:
        err = f"{type(exc).__name__}: {exc}"
        dstat = locals().get("dstat")
    ours_rows = [r for r in rows if r["kind"] == "ours_live"]
    dbot_rows = [r for r in rows if r["kind"].startswith("dbot_")]
    meta = {"generated_utc": C.utc(time.time()), "window_days": a.days,
            "dbot_wallet": a.dbot_wallet, "dbot_scan": dstat, "tasks_origin": tasks_origin,
            "ours_source": "data/bloom_seller_diag.json positions_rows mode=live",
            "summary_ours": summarize(ours_rows), "summary_dbot_batch5": summarize(dbot_rows),
            "summary_all": summarize(rows), "error": err,
            "credits_this_run": rpc.stats.get("кредитов"), "rpc_calls_by_method": rpc.calls_by_method,
            "c2_usage": C.c2_usage_report(), "elapsed_s": round(time.time() - t0, 1),
            "definition": ("сэндвич: кошелёк купил минт раньше нас в нашем блоке и продал позже нас "
                           "в этом же или следующем произведённом блоке; потеря = наша цена "
                           "исполнения в нашем пуле к цене последней сделки этого пула перед его "
                           "входом, п.п.")}
    paths = write_outputs(date, rows, meta)
    C.log(f"выгрузка: {paths}; кредитов за прогон {rpc.stats.get('кредитов')}; "
          f"C2 сегодня {C.c2_spent_today()}")
    for name in ("summary_ours", "summary_dbot_batch5"):
        print(name, json.dumps(meta[name], ensure_ascii=False, default=str))
    return 0


# ------------------------------------------------------------ самопроверка

class FakeRpc:
    def __init__(self, blocks: dict, sigs: dict | None = None, txs: dict | None = None):
        self.blocks, self.sigs, self.txs = blocks, sigs or {}, txs or {}

    def call(self, method, params, **kw):
        if method == "getBlock":
            if params[0] not in self.blocks:
                raise RuntimeError("RPC error {'code': -32007, 'message': 'Slot skipped'}")
            return self.blocks[params[0]]
        raise RuntimeError(method)

    def get_tx(self, sig):
        return self.txs.get(sig)

    def signatures(self, address, *, before=None, until=None, limit=1000):
        return self.sigs.get(address, [])[:limit]


def self_test() -> int:
    checks: list = []

    def chk(name, ok, got=""):
        checks.append((name, bool(ok), got))

    txs = C.load_real_txs()
    ours = next(t for s, t in txs.items() if s.startswith("2uPwpSAQ"))
    wallet = "BmjAUDbwBMxR5shrmzBtKRwveVahFGFiEH3oTq7QTHnu"
    mint = "87pa2UbBB2dhD4b7CHDPHzzKjrdrUtEcBHueXnVz6CJp"
    pool = C.identify_pool(ours, wallet, mint)
    vault, qv, owner, qm = pool["pool_vault"], pool["quote_vault"], pool["pool_owner"], pool["quote_mint"]
    slot = ours["slot"]
    sig0 = C.first_signature(ours)

    def acc(sig, signer, d_vault=0, d_quote=0, att_mint=0, att_sol=0, att_wsol=0, err=None):
        """Транзакция в режиме accounts: ключи и балансы, без инструкций."""
        keys = [signer, vault, qv, "ATT_ATA", "ATT_WSOL"]
        pre = [{"accountIndex": 1, "owner": owner, "mint": mint,
                "uiTokenAmount": {"amount": "1000000000000000", "decimals": 6}},
               {"accountIndex": 2, "owner": owner, "mint": qm,
                "uiTokenAmount": {"amount": "1000000000000", "decimals": 8}},
               {"accountIndex": 3, "owner": signer, "mint": mint,
                "uiTokenAmount": {"amount": str(10 ** 12), "decimals": 6}},
               {"accountIndex": 4, "owner": signer, "mint": C.WSOL,
                "uiTokenAmount": {"amount": str(10 ** 10), "decimals": 9}}]
        post = [{"accountIndex": 1, "owner": owner, "mint": mint,
                 "uiTokenAmount": {"amount": str(1000000000000000 + d_vault), "decimals": 6}},
                {"accountIndex": 2, "owner": owner, "mint": qm,
                 "uiTokenAmount": {"amount": str(1000000000000 + d_quote), "decimals": 8}},
                {"accountIndex": 3, "owner": signer, "mint": mint,
                 "uiTokenAmount": {"amount": str(10 ** 12 + att_mint), "decimals": 6}},
                {"accountIndex": 4, "owner": signer, "mint": C.WSOL,
                 "uiTokenAmount": {"amount": str(10 ** 10 + att_wsol), "decimals": 9}}]
        return {"transaction": {"signatures": [sig],
                                "accountKeys": [{"pubkey": k, "signer": k == signer} for k in keys]},
                "meta": {"err": err, "preBalances": [5 * 10 ** 9, 0, 0, 0, 0],
                         "postBalances": [5 * 10 ** 9 + att_sol, 0, 0, 0, 0],
                         "preTokenBalances": pre, "postTokenBalances": post}}
    p0 = pool["price"]
    q_of = lambda n_tok, mult: int(D(n_tok) * p0 * D(mult) * D(10) ** 8)  # noqa: E731
    prior = acc("P" * 88, "OTHER", -10 ** 9, q_of(1000, "0.8"), att_mint=10 ** 9)   # сделка до атаки
    front = acc("F" * 88, "ATTACKER", -2 * 10 ** 9, q_of(2000, "0.9"), att_mint=2 * 10 ** 9,
                att_wsol=-1_500_000_000)
    back_same = acc("B" * 88, "ATTACKER", 2 * 10 ** 9, -q_of(2000, "1.05"), att_mint=-2 * 10 ** 9,
                    att_wsol=1_700_000_000)
    bystander = acc("N" * 88, "BUYER2", 0, 0, att_mint=5)
    blocks = {slot: {"transactions": [prior, front, ours, bystander, back_same]}}
    fake = FakeRpc(blocks)
    r = analyze_trade(fake, "t", sig0, wallet, mint, ours, sources={"ATTACKER"}, known={})
    chk("сэндвич в том же блоке найден", r["sandwich"] == "да" and r["attacker"] == "ATTACKER", r)
    chk("вход/выход атакующего в SOL (WSOL 1.5 -> 1.7)",
        abs(r["attacker_entry_sol"] - 1.5) < 1e-9 and abs(r["attacker_exit_sol"] - 1.7) < 1e-9
        and abs(r["attacker_profit_sol"] - 0.2) < 1e-9, r)
    chk("потеря в п.п. от цены сделки ДО его входа (0.8 * P0)",
        abs(r["loss_pp"] - 25.0) < 0.01, r["loss_pp"])
    chk("его сдвиг: 0.9 к 0.8 = +12.5 п.п., в нашем пуле",
        abs(r["front_vs_before_pp"] - 12.5) < 0.01 and r["same_pool"] is True, r)
    chk("источник как атакующий помечен", r["attacker_is_source"] is True)
    chk("индекс в блоке и число транзакций", r["index_in_block"] == 2 and r["block_tx_count"] == 5, r)
    # продажа в СЛЕДУЮЩЕМ блоке, слот +1 пропущен
    blocks2 = {slot: {"transactions": [prior, front, ours]}, slot + 2: {"transactions": [bystander, back_same]}}
    r2 = analyze_trade(FakeRpc(blocks2), "t", sig0, wallet, mint, ours, sources=set(), known={})
    chk("продажа в следующем произведённом блоке (слот +1 пропущен)",
        r2["sandwich"] == "да" and r2["back_where"] == "next_block" and r2["next_block_slot"] == slot + 2, r2)
    # продажа через блок -- не сэндвич
    blocks3 = {slot: {"transactions": [prior, front, ours]}, slot + 1: {"transactions": [bystander]},
               slot + 2: {"transactions": [back_same]}}
    r3 = analyze_trade(FakeRpc(blocks3), "t", sig0, wallet, mint, ours, sources=set(), known={})
    chk("продажа через блок -- не сэндвич", r3["sandwich"] == "нет", r3)
    # покупка ПОСЛЕ нас и продажа -- не сэндвич
    blocks4 = {slot: {"transactions": [prior, ours, front, back_same]}, slot + 1: {"transactions": []}}
    r4 = analyze_trade(FakeRpc(blocks4), "t", sig0, wallet, mint, ours, sources=set(), known={})
    chk("вход после нас -- не сэндвич", r4["sandwich"] == "нет", r4)
    # неуспешная продажа -- не сэндвич
    back_fail = acc("X" * 88, "ATTACKER", 2 * 10 ** 9, -q_of(2000, "1.05"), att_mint=-2 * 10 ** 9,
                    err={"e": 1})
    blocks5 = {slot: {"transactions": [prior, front, ours, back_fail]}, slot + 1: {"transactions": []}}
    r5 = analyze_trade(FakeRpc(blocks5), "t", sig0, wallet, mint, ours, sources=set(), known={})
    chk("неуспешная продажа -- не сэндвич", r5["sandwich"] == "нет", r5)
    # следующего блока нет в 6 слотах и сэндвича в блоке нет -- «нет данных»
    blocks6 = {slot: {"transactions": [prior, front, ours]}}
    r6 = analyze_trade(FakeRpc(blocks6), "t", sig0, wallet, mint, ours, sources=set(), known={})
    chk("нет следующего блока -- «нет данных», не «нет»",
        r6["sandwich"] == "нет данных" and "следующий блок" in (r6["why_no_data"] or ""), r6)
    # цены до входа в блоке нет -- берётся из истории хранилища
    blocks7 = {slot: {"transactions": [front, ours, back_same]}, slot + 1: {"transactions": []}}
    prior_full = dict(prior)
    fake7 = FakeRpc(blocks7, sigs={vault: [{"signature": "P" * 88, "slot": slot - 3, "err": None}]},
                    txs={"P" * 88: prior_full})
    r7 = analyze_trade(fake7, "t", sig0, wallet, mint, ours, sources=set(), known={})
    chk("цена до входа из истории хранилища, если в блоке её нет",
        r7["sandwich"] == "да" and abs(r7["loss_pp"] - 25.0) < 0.01, r7)
    # нашей подписи нет в блоке
    r8 = analyze_trade(FakeRpc({slot: {"transactions": [prior]}}), "t", sig0, wallet, mint, ours,
                       sources=set(), known={})
    chk("нашей подписи нет в блоке -- причина", "нет в блоке" in (r8["why_no_data"] or ""), r8)
    # сводка
    sm = summarize([r, r2, r3, r6])
    chk("сводка: доля по сделкам с данными (2 из 3)", sm["share_sandwiched"] == round(2 / 3, 4), sm)
    chk("сводка: медиана потери", abs(sm["median_loss_pp"] - 25.0) < 0.01, sm)
    chk("сводка: адрес с числом повторов", sm["attackers"] == [("ATTACKER", 2)], sm["attackers"])
    # настоящий журнал -- 7 наших сделок
    chk("наши боевые сделки из журнала: 7", len(C.executor_trades()) == 7)
    # покупка DBot: первый вход по методу проекта (без порога), на настоящей транзакции
    rc = C.load_rpc_check()
    C.install_onchain_rate(rc, C.RateBook(None))
    b = buy_of_wallet(rc, ours, wallet)
    chk("покупка задачи BATCH-3 на настоящей транзакции -- первый вход",
        b and b["kind"] == "first_entry" and b["mint"] == mint, b)
    import tempfile  # noqa: PLC0415
    tmp = Path(tempfile.mkdtemp())
    paths = write_outputs("2099-01-01", [r, r3], {"x": 1}, tmp)
    hdr = Path(paths["csv"]).read_text(encoding="utf-8").splitlines()[0]
    chk("заголовки CSV -- только ASCII", all(ord(c) < 128 for c in hdr), hdr)

    bad = 0
    for name, ok, got in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {name}" + (f"  -> {str(got)[:400]}" if not ok else ""))
        bad += (not ok)
    print(f"самопроверка c2_sandwich: {len(checks) - bad}/{len(checks)} пройдено")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
