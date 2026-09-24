#!/usr/bin/env python3
"""C2: точечный разбор по цепи -- транзакции по подписи и окно минта.

Повод (владелец, 24.09): по Telegram покупка на 0.2 SOL была в 14:10:08Z,
подпись начинается с 5HdXjagm52, продажа авто-ордером Bloom в 14:11:16Z.
В прогонах B эта подпись есть среди подписей 4s87RRC2..., но узел пометил
её как неуспешную. Нужны факты, а не догадки:
* сама транзакция целиком: ошибка, подписанты, плательщик, программы,
  дельты токенов по владельцам, дельты SOL подписантов, хвост логов,
  место в блоке;
* все транзакции минта в окне [from, to]: кто купил и кто продал, и
  участвует ли в них наш кошелёк -- подписантом или владельцем счёта.

Только чтение цепи. Выход: data/c2_tx_probe_<UTC-дата>.json.
Учёт кредитов: служба c2_probe, общий потолок C2.
"""
from __future__ import annotations

import argparse
import calendar
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import c2_crowd_metric as CM  # noqa: E402

SERVICE = "c2_probe"
WINDOW_TX_CAP = 800


def parse_utc(text: str) -> int:
    return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))


def summarize_tx(tx: dict, watch: str) -> dict:
    """Всё, что транзакция сделала, -- по балансам и логам."""
    meta = (tx or {}).get("meta") or {}
    keys = C.account_keys(tx)
    sg = sorted(C.signers(tx))
    deltas = []
    for r in C.token_rows(tx).values():
        if r["post"] != r["pre"]:
            deltas.append({"owner": r["owner"], "mint": r["mint"], "account": r["account"],
                           "delta": str(C.row_delta(r))})
    lam = {}
    for a in set(sg) | {watch}:
        d = C.lamport_delta(tx, a)
        if d is not None:
            lam[a] = d / 1e9
    progs = []
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    lists = [msg.get("instructions") or []]
    for g in meta.get("innerInstructions") or []:
        lists.append(g.get("instructions") or [])
    for lst in lists:
        for ix in lst:
            pid = (ix or {}).get("programId")
            if pid and pid not in progs:
                progs.append(pid)
    return {"signature": C.first_signature(tx), "slot": tx.get("slot"),
            "block_time_utc": C.utc(tx.get("blockTime")), "err": meta.get("err"),
            "fee_payer": keys[0] if keys else None, "signers": sg,
            "watch_is_signer": watch in sg, "watch_in_account_keys": watch in keys,
            "fee_lamports": meta.get("fee"), "sol_delta_by_signer": lam,
            "token_deltas": deltas, "programs": progs,
            "logs_tail": (meta.get("logMessages") or [])[-30:]}


def mint_window(rpc, mint: str, slot_hint: int, bt_hint: int, t_from: int, t_to: int,
                watch: str) -> dict:
    """Все транзакции минта с blockTime в [t_from, t_to]: покупатели, продавцы,
    участие наблюдаемого кошелька."""
    anchor = CM.find_anchor(rpc, slot_hint, bt_hint, t_to + 1)
    start_slot = slot_hint - max(0, int((bt_hint - t_from) / 0.3)) - 50
    sigs, complete = CM.sig_window(rpc, mint, anchor["signature"], start_slot, max_pages=10)
    win = [x for x in sigs if x.get("blockTime") is not None and t_from <= x["blockTime"] <= t_to]
    out = {"anchor_slot": anchor["slot"], "signatures_in_window": len(win),
           "failed_in_window": sum(1 for x in win if x.get("err") is not None),
           "history_complete": complete, "capped": len(win) > WINDOW_TX_CAP, "rows": []}
    win = win[:WINDOW_TX_CAP]
    got = rpc.get_txs([x["signature"] for x in win])
    for x in sorted(win, key=lambda x: (x.get("slot") or 0)):
        tx = got.get(x["signature"])
        if tx is None:
            out["rows"].append({"signature": x["signature"], "why_not": "узел не отдал"})
            continue
        od = C.owner_mint_delta(tx, mint)
        keys = C.account_keys(tx)
        out["rows"].append({
            "signature": x["signature"], "slot": x.get("slot"), "utc": C.utc(x.get("blockTime")),
            "err": (tx.get("meta") or {}).get("err"), "signers": sorted(C.signers(tx)),
            "buyers": sorted(C.mint_buyers(tx, mint)), "sellers": sorted(C.mint_sellers(tx, mint)),
            "mint_delta_by_owner": {o: str(d) for o, d in od.items()},
            "watch_involved": watch in keys or watch in od})
    out["watch_rows"] = [r for r in out["rows"] if r.get("watch_involved")]
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--sigs", default="", help="подписи через запятую")
    p.add_argument("--mint", default="")
    p.add_argument("--from-utc", default="")
    p.add_argument("--to-utc", default="")
    p.add_argument("--watch", default=C.EXECUTOR_WALLET)
    a = p.parse_args()
    if a.self_test:
        return self_test()
    key = C.RC.helius_key()[0]
    if not key:
        print("СТОП: ключ Helius не задан (секрет HELIUS_API)", file=sys.stderr)
        return 2
    rpc = C.C2Rpc(SERVICE, key=key)
    out = {"generated_utc": C.utc(time.time()), "watch": a.watch, "transactions": [],
           "mint": a.mint or None, "window": [a.from_utc, a.to_utc]}
    first = None
    for s in [x.strip() for x in a.sigs.split(",") if x.strip()]:
        try:
            tx = rpc.get_tx(s)
        except RuntimeError as exc:
            out["transactions"].append({"signature": s, "why_not": str(exc)[:160]})
            continue
        if tx is None:
            out["transactions"].append({"signature": s, "why_not": "узел: транзакция не найдена"})
            continue
        row = summarize_tx(tx, a.watch)
        bs = CM.block_signatures(rpc, tx["slot"])
        row["index_in_block"] = bs.index(s) if bs and s in bs else None
        row["block_tx_count"] = len(bs) if bs else None
        out["transactions"].append(row)
        first = first or tx
    if a.mint and a.from_utc and a.to_utc and first is not None:
        try:
            out["mint_window"] = mint_window(rpc, a.mint, first["slot"], first["blockTime"],
                                             parse_utc(a.from_utc), parse_utc(a.to_utc), a.watch)
        except RuntimeError as exc:
            out["mint_window"] = {"why_not": str(exc)[:200]}
    out["credits_this_run"] = rpc.stats.get("кредитов")
    out["c2_usage"] = C.c2_usage_report()
    path = C.DATA / f"c2_tx_probe_{C.today_utc()}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    for t in out["transactions"]:
        print(json.dumps({k: t.get(k) for k in ("signature", "block_time_utc", "slot", "index_in_block",
                                                  "err", "fee_payer", "signers", "watch_is_signer",
                                                  "sol_delta_by_signer", "token_deltas", "why_not")},
                         ensure_ascii=False, default=str))
        for ln in t.get("logs_tail") or []:
            print("    лог:", ln[:200])
    mw = out.get("mint_window") or {}
    print("окно минта:", {k: mw.get(k) for k in ("signatures_in_window", "failed_in_window",
                                                  "history_complete", "capped", "why_not")})
    for r in mw.get("watch_rows") or []:
        print("  наш кошелёк участвует:", json.dumps(r, ensure_ascii=False, default=str))
    print(f"кредитов за прогон {out['credits_this_run']}; C2 сегодня {C.c2_spent_today()}")
    return 0


def self_test() -> int:
    checks = []
    txs = C.load_real_txs()
    t = next(x for s, x in txs.items() if s.startswith("2uPwpSAQ"))
    w = "BmjAUDbwBMxR5shrmzBtKRwveVahFGFiEH3oTq7QTHnu"
    s = summarize_tx(t, w)
    checks.append(("подписант и плательщик распознаны", s["watch_is_signer"] and s["fee_payer"] == w))
    checks.append(("дельты токенов перечислены", len(s["token_deltas"]) >= 5))
    checks.append(("ошибки нет", s["err"] is None))
    checks.append(("дата разбирается", parse_utc("2026-09-24T14:10:00Z") == 1790259000))
    bad = 0
    for name, ok in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {name}")
        bad += (not ok)
    print(f"самопроверка c2_tx_probe: {len(checks) - bad}/{len(checks)} пройдено")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
