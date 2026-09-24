#!/usr/bin/env python3
"""Задача E (C2): место внутри блока источника -- кто покупает после него,
сколько платит и сколько роста съедает каждое место.

Выборка: сделки лидера Beqv6... и Brez за 7 дней из задачи A
(data/crowd_metric_<дата>.json). Для каждой -- ПОЛНЫЙ блок источника
(getBlock, transactionDetails=full, jsonParsed): порядок, инструкции
compute budget, переводы SOL, балансы. По каждой успешной покупке того же
минта ПОСЛЕ источника в его блоке:
* место (индекс в блоке и ранг среди покупателей после источника);
* плата за вычисления: SetComputeUnitPrice (микролампорты/CU) и
  SetComputeUnitLimit из инструкций; фактическая приоритетная плата =
  meta.fee - 5000 * подписей;
* чаевые: переводы SOL от подписанта на адреса, которые в этих блоках
  получают чистые переводы SOL от многих разных кошельков (список
  выводится из данных и печатается целиком; имён «Jito»/«сендер» не
  приписываем -- внешнего списка в репозитории нет);
* программа-роутер -- программы верхнего уровня;
* цена исполнения в пуле источника к споту после источника (задача A).
Наши S+0 покупки -- для сравнения (журнал исполнителя: наш слот = слот
источника). Только чтение цепи. Служба c2_block.
"""
from __future__ import annotations

import json
import struct
import sys
import time
from collections import Counter
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402

SERVICE = "c2_block"
SOURCES = {"Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit": "leader",
           "Fvkc2thk1YcAASdR2gi8uf9n67JW9Dqqr9iRd99MDhoB": "Brez"}
CB = "ComputeBudget111111111111111111111111111111"
SYSTEM = "11111111111111111111111111111111"
INFRA = {CB, SYSTEM, "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
         "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb", "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
         "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr", "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo"}
FULL_OPTS = {"encoding": "jsonParsed", "transactionDetails": "full", "rewards": False,
             "maxSupportedTransactionVersion": C.TX_VERSION, "commitment": "finalized"}
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
TIP_MIN_WALLETS = 5


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    b = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + b


def compute_budget(tx: dict) -> dict:
    price = limit = None
    for ix in (((tx.get("transaction") or {}).get("message") or {}).get("instructions") or []):
        if ix.get("programId") != CB or not ix.get("data"):
            continue
        d = b58decode(ix["data"])
        if d[:1] == b"\x03" and len(d) >= 9:
            price = struct.unpack("<Q", d[1:9])[0]
        elif d[:1] == b"\x02" and len(d) >= 5:
            limit = struct.unpack("<I", d[1:5])[0]
    meta = tx.get("meta") or {}
    nsig = len((tx.get("transaction") or {}).get("signatures") or [])
    fee = meta.get("fee")
    return {"cu_price_micro": price, "cu_limit": limit,
            "cu_consumed": meta.get("computeUnitsConsumed"),
            "priority_lamports": (fee - 5000 * nsig) if fee is not None else None}


def sol_transfers(tx: dict) -> list:
    """(from, to, lamports) системных переводов, внешних и вложенных."""
    msg = (tx.get("transaction") or {}).get("message") or {}
    lists = [msg.get("instructions") or []]
    for g in (tx.get("meta") or {}).get("innerInstructions") or []:
        lists.append(g.get("instructions") or [])
    out = []
    for lst in lists:
        for ix in lst:
            p = (ix or {}).get("parsed") or {}
            if (ix or {}).get("program") == "system" and p.get("type") == "transfer":
                i = p.get("info") or {}
                out.append((i.get("source"), i.get("destination"), int(i.get("lamports") or 0)))
    return out


def routers(tx: dict) -> list:
    msg = (tx.get("transaction") or {}).get("message") or {}
    out = []
    for ix in msg.get("instructions") or []:
        pid = (ix or {}).get("programId")
        if pid and pid not in INFRA and pid not in out:
            out.append(pid)
    return out


def handle_source_trade(src, t, btx, b, rows, per_trade) -> None:
    idx = next((i for i, x in enumerate(btx) if C.first_signature(x) == t["signature"]), None)
    if idx is None:
        per_trade.append({"source": SOURCES[src], "signature": t["signature"],
                          "why_not": b.get("why_not") or "подписи источника нет в блоке"})
        return
    stx = btx[idx]
    pool = C.identify_pool(stx, src, t["mint"])
    spot = D(t["spot_after"]) if t.get("spot_after") else None
    src_cb = compute_budget(stx)
    rank = n_failed = 0
    for j in range(idx + 1, len(btx)):
        x = btx[j]
        if (x.get("meta") or {}).get("err") is not None:
            if t["mint"] in C.account_keys(x):
                n_failed += 1
            continue
        buyers = [w for w in C.mint_buyers(x, t["mint"]) if w != src]
        if not buyers:
            continue
        rank += 1
        sg = C.signers(x)
        ev = C.pool_event(x, pool) if pool["ok"] else {"kind": "нет пула"}
        vs_spot = (float(ev["price"] / spot - 1) if ev.get("kind") == "swap" and ev.get("side") == "buy"
                   and spot else None)
        rows.append({"source": SOURCES[src], "trade": t["signature"], "slot": t["slot"],
                     "source_index": idx, "index": j, "rank_after_source": rank,
                     "wallet": buyers[0], "signature": C.first_signature(x), **compute_budget(x),
                     "_transfers": [(to, lam) for f, to, lam in sol_transfers(x) if f in sg],
                     "routers": routers(x), "same_pool": ev.get("kind") == "swap",
                     "exec_vs_spot_after": vs_spot})
    per_trade.append({"source": SOURCES[src], "signature": t["signature"], "slot": t["slot"],
                      "source_index": idx, "block_tx": len(btx), "buyers_after": rank,
                      "failed_mint_tx_after": n_failed, "source_cu_price": src_cb["cu_price_micro"],
                      "spot_after": t.get("spot_after")})


def handle_ours(t, btx, ours_rows) -> None:
    oi = next((i for i, x in enumerate(btx) if C.first_signature(x) == t["signature"]), None)
    si = next((i for i, x in enumerate(btx) if C.first_signature(x) == t.get("source_sig")), None)
    if oi is None:
        ours_rows.append({"signature": t["signature"], "why_not": "нашей подписи нет в блоке"})
        return
    sg = C.signers(btx[oi])
    between = None
    if si is not None:
        between = sum(1 for j in range(si + 1, oi)
                      if (btx[j].get("meta") or {}).get("err") is None
                      and [w for w in C.mint_buyers(btx[j], t["mint"]) if w != C.EXECUTOR_WALLET])
    ours_rows.append({"signature": t["signature"], "slot": t["slot"], "our_index": oi,
                      "source_index": si, "buyers_between": between, **compute_budget(btx[oi]),
                      "_transfers": [(to, lam) for f, to, lam in sol_transfers(btx[oi]) if f in sg],
                      "routers": routers(btx[oi])})


def main() -> int:
    key = C.RC.helius_key()[0]
    if not key:
        print("СТОП: ключ Helius не задан", file=sys.stderr)
        return 2
    rpc = C.C2Rpc(SERVICE, key=key)
    src_file = sorted(C.DATA.glob("crowd_metric_2*.json"))[-1]
    d = json.loads(src_file.read_text(encoding="utf-8"))
    trades = []
    for ps in d["per_source"]:
        if ps["address"] in SOURCES:
            for t in ps.get("trades") or []:
                if t.get("slot") and t.get("signature"):
                    trades.append((ps["address"], t))
    ours = [t for t in C.executor_trades(mode="live")
            if t.get("slot") and t.get("source_slot") and t["slot"] == t["source_slot"]]
    t0 = time.time()
    # Блок за блоком, без хранения блоков: из каждого берём только нужное.
    recv_wallets: dict = {}
    rows, per_trade, ours_rows = [], [], []
    by_slot: dict = {}
    for src, t in trades:
        by_slot.setdefault(t["slot"], []).append(("src", src, t))
    for t in ours:
        by_slot.setdefault(t["slot"], []).append(("ours", None, t))
    for slot in sorted(by_slot):
        try:
            b = rpc.call("getBlock", [slot, FULL_OPTS]) or {}
        except RuntimeError as exc:
            b = {"why_not": str(exc)[:120]}
        btx = b.get("transactions") or []
        for tx in btx:
            sg = C.signers(tx)
            for f, to, lam in sol_transfers(tx):
                if f in sg and lam > 0:
                    recv_wallets.setdefault(to, set()).add(f)
        for kind, src, t in by_slot[slot]:
            if kind == "src":
                handle_source_trade(src, t, btx, b, rows, per_trade)
            else:
                handle_ours(t, btx, ours_rows)
        del b, btx
    tip_accounts = {a for a, ws in recv_wallets.items() if len(ws) >= TIP_MIN_WALLETS}
    # Ядро получателей: адреса, куда шлют переводы >= 200 разных кошельков выборки
    # (плотная группа, резко отделённая от остальных). Чаевые считаются дважды:
    # в ядро и во все частые адреса -- второе шире и включает сервисные сборы.
    core = {a for a, ws in recv_wallets.items() if len(ws) >= 200}
    for r in rows + ours_rows:
        tr = r.pop("_transfers", [])
        r["sol_transfers"] = tr
        r["tip_lamports"] = sum(lam for to, lam in tr if to in tip_accounts)
        r["tip_core_lamports"] = sum(lam for to, lam in tr if to in core)
    # ранговые четверти по каждой сделке
    by_trade: dict = {}
    for r in rows:
        by_trade.setdefault(r["trade"], []).append(r)
    for lst in by_trade.values():
        n = len(lst)
        for r in lst:
            r["quartile"] = min(4, 1 + (r["rank_after_source"] - 1) * 4 // n)
    def q(vals, p):
        v = sorted(x for x in vals if x is not None)
        return v[min(len(v) - 1, int(p * len(v)))] if v else None
    quart = {}
    for k in (1, 2, 3, 4):
        rs = [r for r in rows if r["quartile"] == k]
        quart[k] = {"n": len(rs),
                    "cu_price_p25": q([r["cu_price_micro"] for r in rs], 0.25),
                    "cu_price_median": q([r["cu_price_micro"] for r in rs], 0.5),
                    "cu_price_p75": q([r["cu_price_micro"] for r in rs], 0.75),
                    "priority_lamports_median": q([r["priority_lamports"] for r in rs], 0.5),
                    "tip_lamports_median": q([r["tip_lamports"] for r in rs], 0.5),
                    "tip_core_lamports_median": q([r["tip_core_lamports"] for r in rs], 0.5),
                    "share_with_core_tip": round(sum(1 for r in rs if r["tip_core_lamports"]) / len(rs), 3) if rs else None,
                    "share_with_tip": round(sum(1 for r in rs if r["tip_lamports"]) / len(rs), 3) if rs else None,
                    "exec_vs_spot_after_median": q([r["exec_vs_spot_after"] for r in rs], 0.5),
                    "n_same_pool": sum(1 for r in rs if r["exec_vs_spot_after"] is not None)}
    # порог: плата, при которой покупатель попадал в первую четверть
    thresholds = {}
    for thr in (0, 10_000, 100_000, 500_000, 1_000_000, 5_000_000, 20_000_000, 100_000_000):
        rs = [r for r in rows if (r["cu_price_micro"] or 0) >= thr]
        thresholds[thr] = {"n": len(rs), "share_q1": round(sum(1 for r in rs if r["quartile"] == 1) / len(rs), 3)
                           if rs else None}
    core_list = sorted(core)
    tip_thr = {}
    for thr in (0, 100_000, 1_000_000, 5_000_000, 10_000_000, 30_000_000):
        rs = [r for r in rows if r["tip_core_lamports"] >= thr]
        tip_thr[thr] = {"n": len(rs), "share_q1": round(sum(1 for r in rs if r["quartile"] == 1) / len(rs), 3)
                        if rs else None}
    router_cnt = Counter(tuple(r["routers"][:2]) for r in rows)
    out = {"generated_utc": C.utc(time.time()), "trades": len(per_trade), "rows": len(rows),
           "tip_accounts_from_data": sorted(({"address": a, "distinct_senders": len(recv_wallets[a])}
                                             for a in tip_accounts), key=lambda x: -x["distinct_senders"]),
           "quartiles": quart, "cu_price_thresholds_share_q1": thresholds,
           "tip_core_accounts": core_list, "tip_core_thresholds_share_q1": tip_thr,
           "routers_top": [(list(k), v) for k, v in router_cnt.most_common(12)],
           "ours_s0": ours_rows, "per_trade": per_trade, "followers": rows,
           "credits_this_run": rpc.stats.get("кредитов"), "elapsed_s": round(time.time() - t0, 1),
           "c2_usage": C.c2_usage_report()}
    (C.DATA / f"c2_block_position_{C.today_utc()}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("сделок", len(per_trade), "покупателей после источника", len(rows))
    print("четверти:", json.dumps(quart, ensure_ascii=False))
    print("пороги:", json.dumps(thresholds))
    print("пороги чаевых (ядро):", json.dumps(tip_thr), "ядро:", len(core_list))
    print("наши S+0:", json.dumps(ours_rows, ensure_ascii=False, default=str))
    print("tip-адреса из данных:", len(tip_accounts))
    print(f"кредитов {rpc.stats.get('кредитов')}; C2 сегодня {C.c2_spent_today()}")
    return 0


def self_test() -> int:
    txs = C.load_real_txs()
    t = next(x for s, x in txs.items() if s.startswith("2uPwpSAQ"))
    cb = compute_budget(t)
    ok1 = cb["priority_lamports"] is not None
    ok2 = isinstance(routers(t), list) and "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4" in routers(t)
    print(f"  [{'ok  ' if ok1 else 'СБОЙ'}] плата за вычисления читается ({cb})")
    print(f"  [{'ok  ' if ok2 else 'СБОЙ'}] роутер верхнего уровня -- Jupiter ({routers(t)})")
    print(f"самопроверка c2_block_position: {int(ok1) + int(ok2)}/2 пройдено")
    return 0 if ok1 and ok2 else 1


if __name__ == "__main__":
    raise SystemExit(self_test() if "--self-test" in sys.argv else main())
