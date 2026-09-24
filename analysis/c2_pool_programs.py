#!/usr/bin/env python3
"""Задача D, шаг 1 (C2): по каким DEX-программам идут сигналы buy.

Сигналы -- покупки 18 источников из задачи A (первый вход >= 2 SOL-экв,
7 суток): data/crowd_metric_<дата>.json, per_source[].trades[]. Для каждой
покупки берётся её транзакция (getTransaction), и программа пула -- это
programId той инструкции DEX, в счетах которой стоит хранилище минта
(pool_vault из задачи A). Роутеры (Jupiter, Bloom и т.п.) отдельно: это
программа верхнего уровня.

Настоящие транзакции каждого типа сохраняются в data/c2_pool_samples/ --
на них идут самопроверки сборщика покупки (шаг 2-4), без выдуманных форм.

Выход: data/c2_pool_programs_<дата>.csv / .json. Служба c2_pool.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402

SERVICE = "c2_pool"
LABELS_PATH = C.DATA / "solana_buyer_200" / "prior" / "current" / "buyer_100" / "dex_labels.json"
SAMPLES_DIR = C.DATA / "c2_pool_samples"
SAMPLES_PER_PROGRAM = 25


def labels() -> dict:
    # Только программы пулов (107 из dex_labels.json); роутеры (Jupiter, Bloom)
    # туда не входят и программой пула не считаются.
    return json.loads(LABELS_PATH.read_text(encoding="utf-8"))


def instructions_with_program(tx: dict) -> list:
    """[(programId, set(accounts), stack)] для неразобранных инструкций; stack 1 -- верхний уровень."""
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    out = []
    for ix in msg.get("instructions") or []:
        if isinstance(ix, dict) and isinstance(ix.get("accounts"), list):
            out.append((ix.get("programId"), set(ix["accounts"]), 1))
    for g in ((tx or {}).get("meta") or {}).get("innerInstructions") or []:
        for ix in g.get("instructions") or []:
            if isinstance(ix, dict) and isinstance(ix.get("accounts"), list):
                out.append((ix.get("programId"), set(ix["accounts"]), ix.get("stackHeight") or 2))
    return out


def pool_program(tx: dict, vault: str | None, lab: dict) -> dict:
    ins = instructions_with_program(tx)
    top = []
    for pid, _, st in ins:
        if st == 1 and pid not in top:
            top.append(pid)
    dex = []
    if vault:
        for pid, accs, _ in ins:
            if vault in accs and pid in lab and pid not in dex:
                dex.append(pid)
    return {"pool_programs": dex, "top_level": top,
            "pool_program": dex[0] if len(dex) == 1 else (None if not dex else "|".join(dex)),
            "top_level_labels": [lab.get(x, x) for x in top]}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--crowd-json", default="")
    a = p.parse_args()
    if a.self_test:
        return self_test()
    key = C.RC.helius_key()[0]
    if not key:
        print("СТОП: ключ Helius не задан", file=sys.stderr)
        return 2
    src = Path(a.crowd_json) if a.crowd_json else sorted(C.DATA.glob("crowd_metric_2*.json"))[-1]
    d = json.loads(src.read_text(encoding="utf-8"))
    trades = [(ps.get("remark"), ps.get("address"), t) for ps in d["per_source"] for t in ps.get("trades") or []]
    lab = labels()
    rpc = C.C2Rpc(SERVICE, key=key)
    t0 = time.time()
    got = rpc.get_txs([t["signature"] for _, _, t in trades])
    rows, samples = [], {}
    for remark, addr, t in trades:
        tx = got.get(t["signature"])
        if tx is None:
            rows.append({"source": remark, "signature": t["signature"], "why_not": "узел не отдал"})
            continue
        pp = pool_program(tx, t.get("pool_vault"), lab)
        pid = pp["pool_program"]
        rows.append({"source": remark, "source_address": addr, "signature": t["signature"],
                     "block_time_utc": t.get("block_time_utc"), "mint": t.get("mint"),
                     "pool_vault": t.get("pool_vault"), "pool_owner": t.get("pool_owner"),
                     "quote_mint": t.get("quote_mint"),
                     "quote_is_sol": t.get("quote_mint") in (C.WSOL, C.NATIVE_QUOTE),
                     "pool_program": pid, "pool_label": lab.get(pid) if pid else None,
                     "top_level": ";".join(lab.get(x, x[:8]) for x in pp["top_level"]),
                     "why_not": None if pid else ("нет пула из задачи A" if not t.get("pool_vault")
                                                  else "хранилище не найдено ни в одной инструкции DEX")})
        if pid and "|" not in pid:
            lst = samples.setdefault(pid, [])
            if len(lst) < SAMPLES_PER_PROGRAM:
                lst.append({"source": addr, "mint": t.get("mint"), "pool_vault": t.get("pool_vault"),
                            "quote_mint": t.get("quote_mint"), "tx": tx})
    total = len(rows)
    by: dict = {}
    for r in rows:
        k = r.get("pool_program") or "нет данных"
        b = by.setdefault(k, {"n": 0, "sol_quoted": 0, "example": r["signature"],
                              "label": r.get("pool_label") or r.get("why_not")})
        b["n"] += 1
        b["sol_quoted"] += bool(r.get("quote_is_sol"))
    table = sorted(({"program": k, **v, "share": round(v["n"] / total, 4)} for k, v in by.items()),
                   key=lambda x: -x["n"])
    cum = 0.0
    for x in table:
        cum += x["share"]
        x["cum_share"] = round(cum, 4)
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    for pid, lst in samples.items():
        (SAMPLES_DIR / f"{pid}.json").write_text(json.dumps(lst, ensure_ascii=False), encoding="utf-8")
    date = C.today_utc()
    with open(C.DATA / f"c2_pool_programs_{date}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["program", "label", "n", "share", "cum_share", "sol_quoted", "example"])
        w.writeheader()
        for x in table:
            w.writerow({k: x.get(k) for k in w.fieldnames})
    (C.DATA / f"c2_pool_programs_{date}.json").write_text(json.dumps(
        {"generated_utc": C.utc(time.time()), "source_file": src.name, "n_signals": total,
         "table": table, "rows": rows, "credits_this_run": rpc.stats.get("кредитов"),
         "elapsed_s": round(time.time() - t0, 1), "c2_usage": C.c2_usage_report()},
        ensure_ascii=False, indent=1), encoding="utf-8")
    for x in table:
        print(f"{x['share']:.3f} {x['cum_share']:.3f} n={x['n']} sol={x['sol_quoted']} {x['label']} {x['program']} {x['example'][:20]}")
    print(f"кредитов {rpc.stats.get('кредитов')}; C2 сегодня {C.c2_spent_today()}")
    return 0


def self_test() -> int:
    txs = C.load_real_txs()
    t = next(x for s, x in txs.items() if s.startswith("2uPwpSAQ"))
    lab = labels()
    pp = pool_program(t, "J7vjBbQGeF8FzCMDWhzhSyS8Q3gpAmzxHWYYPbPaA9Wo", lab)
    vault = next(r["account"] for r in C.token_rows(t).values() if str(r["account"]).startswith("J7vjBbQG"))
    pp = pool_program(t, vault, lab)
    ok1 = pp["pool_program"] == "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
    print(f"  [{'ok  ' if ok1 else 'СБОЙ'}] пул 87pa2 на настоящей транзакции -- Raydium CPMM ({pp})")
    ok2 = bool(pp["top_level"])
    print(f"  [{'ok  ' if ok2 else 'СБОЙ'}] программы верхнего уровня видны")
    print(f"самопроверка c2_pool_programs: {int(ok1) + int(ok2)}/2 пройдено")
    return 0 if ok1 and ok2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
