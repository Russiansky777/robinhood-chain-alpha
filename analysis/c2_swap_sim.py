#!/usr/bin/env python3
"""Задача D, шаг 4 (C2): проверка сборки без отправки -- simulateTransaction.

Пользователь -- ключ, сгенерированный на лету (0 SOL, 0 токенов), его
подпись не нужна: sigVerify=false, replaceRecentBlockhash=true. Плательщик
комиссии -- адрес самого источника (у него есть SOL; ключ его не нужен и не
используется -- без проверки подписи узел просто исполняет транзакцию на
текущем состоянии). SOL в котировку не оборачивается: своп идёт из
пустого счёта пользователя, и правильно собранная инструкция обязана дойти
до ошибки БАЛАНСА внутри перевода токена. Любая другая ошибка
(ограничения Anchor на счета, неверный дискриминатор) -- сборка неверна.

Плюс шаг 3: минимум по резервам на 5HdXjagm52... (Raydium Launchlab) --
сколько дала бы наша формула при 35 % и прошла бы покупка.

Метода отправки транзакции здесь нет. Служба c2_build.
"""
from __future__ import annotations

import json
import re
import struct
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import c2_swap_build as B  # noqa: E402

from solders.keypair import Keypair  # noqa: E402

SERVICE = "c2_build"
PER_TYPE = 12
OUR_FAILED = "5HdXjagm52JXeMSWewa9i3SyKsn1LhrFgxiwcwnnhSudmPzVc8mrxK72ebYFPdCAk9inSUzAWruSRvWvTkYWx8xG"
BEQV_BUY = "3egBxswdzyhJnnMe7ubTVwMr6M9ani5jqUikTCs4kXx9tcaxbcWzmmgznnu8mqKbyQJEvzBpJ7p5VasZn71DNJRg"
BAL_RE = re.compile(r"insufficient (funds|lamports)|Error: insufficient|custom program error: 0x1$", re.I)


def classify(value: dict) -> dict:
    logs = value.get("logs") or []
    err = value.get("err")
    tail = logs[-6:]
    if err is None:
        return {"verdict": "success", "tail": tail}
    joined = "\n".join(logs)
    if any(BAL_RE.search(ln) for ln in logs) or "insufficient" in joined.lower():
        return {"verdict": "balance_error", "err": err, "tail": tail}
    # Программа сама сверяет остаток пользователя с тратой: Anchor
    # RequireGteViolated, "Left: 0" (остаток), "Right: <трата>" -- тоже баланс.
    if "RequireGteViolated" in joined and "Program log: Left: 0" in logs:
        return {"verdict": "balance_error", "err": err, "tail": tail, "kind": "require_gte_balance"}
    return {"verdict": "other_error", "err": err, "tail": tail}


def simulate(rpc, tx_b64: str) -> dict:
    res = rpc.call("simulateTransaction", [tx_b64, {"encoding": "base64", "sigVerify": False,
                                                     "replaceRecentBlockhash": True,
                                                     "commitment": "processed"}])
    return (res or {}).get("value") or {}


def launchlab_check(rpc) -> dict:
    """Шаг 3 на 5HdXjagm52: наша формула по резервам после сделки лидера."""
    ours, lead = rpc.get_tx(OUR_FAILED), rpc.get_tx(BEQV_BUY)
    if not ours or not lead:
        return {"ok": False, "why_not": "узел не отдал транзакции"}
    mint = "5MhscascCnJJNsPAsbEFtBMW6VanEmdyEcCx7v9172rS"
    pool = C.identify_pool(lead, C.LEADER_BEQV, mint)
    if not pool["ok"]:
        return {"ok": False, "why_not": f"пул лидера: {pool['why_not']}"}
    ix = next((i for i in B.all_instructions(ours) if i.get("programId") == B.LAUNCHLAB), None)
    if ix is None:
        return {"ok": False, "why_not": "в нашей транзакции нет инструкции Launchlab"}
    data = B.b58decode(ix["data"])
    name = "buy_exact_in" if data[:8] == B.disc("buy_exact_in") else data[:8].hex()
    amount_in, min_out, share_fee = struct.unpack("<QQQ", data[8:32])
    mo = B.launchlab_min_out(lead, amount_in, 0.35)
    if not mo.get("ok"):
        return {"ok": False, "why_not": mo.get("why_not")}
    logs = (ours.get("meta") or {}).get("logMessages") or []
    left = next((int(l.rsplit(":", 1)[1]) for l in logs if l.startswith("Program log: Left:")), None)
    right = next((int(l.rsplit(":", 1)[1]) for l in logs if l.startswith("Program log: Right:")), None)
    return {"ok": True, "our_ix": name, "our_amount_in_quote_raw": amount_in,
            "our_min_out_sent_raw": min_out, "share_fee_rate": share_fee,
            "log_left_raw": left, "log_right_raw": right,
            "lead_index_note": "лидер 155-й в слоте, мы 1172-е",
            "method": "кривая Launchlab на виртуальных резервах из события TradeEvent сделки лидера",
            "fee_rate": mo["fee_rate"], "virtual_reserves_after_leader": mo["virtual_reserves_after"],
            "formula_expected_out_raw": mo["expected_out"],
            "formula_min_out_35pct_raw": mo["min_out"],
            "would_pass_if_actual_out_is_left": (left >= mo["min_out"]) if left is not None else None,
            "slippage_needed_to_pass": (round(1 - left / mo["expected_out"], 4)
                                        if left is not None and mo["expected_out"] else None)}


def topup_launchlab(rpc) -> dict:
    """Добрать настоящие транзакции Launchlab из сделок задачи A (pool_program)."""
    path = B.SAMPLES_DIR / f"{B.LAUNCHLAB}.json"
    have = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    seen = {C.first_signature(x["tx"]) for x in have}
    src = sorted(C.DATA.glob("crowd_metric_2*.json"))[-1]
    d = json.loads(src.read_text(encoding="utf-8"))
    want = [(ps["address"], t) for ps in d["per_source"] for t in ps.get("trades") or []
            if t.get("pool_program") == B.LAUNCHLAB and t["signature"] not in seen][:30]
    got = rpc.get_txs([t["signature"] for _, t in want])
    added = 0
    for addr, t in want:
        tx = got.get(t["signature"])
        if tx:
            have.append({"source": addr, "mint": t.get("mint"), "pool_vault": t.get("pool_vault"),
                         "quote_mint": t.get("quote_mint"), "tx": tx})
            added += 1
    path.write_text(json.dumps(have, ensure_ascii=False), encoding="utf-8")
    return {"added": added, "total": len(have)}


def main() -> int:
    key = C.RC.helius_key()[0]
    if not key:
        print("СТОП: ключ Helius не задан", file=sys.stderr)
        return 2
    rpc = C.C2Rpc(SERVICE, key=key)
    out = {"generated_utc": C.utc(time.time()), "types": {}, "rows": []}
    out["launchlab_topup"] = topup_launchlab(rpc)
    out["rebuild_check"] = {}
    for program, spec in B.SPECS.items():
        res = [B.rebuild_check(x, program) for x in B.load_samples(program)]
        out["rebuild_check"][spec["label"]] = {
            "samples": len(res), "exact": sum(1 for r in res if r["ok"]),
            "not_verifiable": sum(1 for r in res if r["ok"] is None),
            "mismatch": sum(1 for r in res if r["ok"] is False)}
    print("байт в байт:", json.dumps(out["rebuild_check"], ensure_ascii=False))
    for program, spec in B.SPECS.items():
        n = 0
        stat = {"balance_error": 0, "success": 0, "other_error": 0, "skipped": 0, "build_ms": []}
        for s in B.load_samples(program):
            if n >= PER_TYPE:
                break
            t0 = time.perf_counter()
            tpl = B.extract_template(s["tx"], program, s["pool_vault"])
            if not tpl["ok"]:
                continue
            mv = B.mints_and_vaults(tpl, s["tx"])
            amount = 10_000_000 if mv.get("quote_mint") == C.WSOL else 1_000_000
            mo = B.min_out_from_reserves(tpl, s["tx"], amount, 0.35)
            kp = Keypair()
            built = B.build_buy(tpl, s["tx"], user=str(kp.pubkey()), payer=s["source"],
                                amount_in=amount, min_out=mo.get("min_out") or 1,
                                cu_units=400_000, cu_price_micro=10_000, wrap_sol=False)
            ms = (time.perf_counter() - t0) * 1000
            try:
                bal = rpc.call("getBalance", [s["source"]])
                lam = (bal or {}).get("value", 0)
            except RuntimeError:
                lam = 0
            if lam < 5_000_000:
                stat["skipped"] += 1
                out["rows"].append({"type": spec["label"], "source_sig": B.C.first_signature(s["tx"]),
                                    "skipped": "у плательщика (адрес источника) меньше 0.005 SOL"})
                continue
            try:
                v = simulate(rpc, built["tx_base64"])
                cl = classify(v)
            except RuntimeError as exc:
                cl = {"verdict": "other_error", "err": str(exc)[:200], "tail": []}
            n += 1
            stat[cl["verdict"]] += 1
            stat["build_ms"].append(round(ms, 3))
            out["rows"].append({"type": spec["label"], "source_sig": C.first_signature(s["tx"]),
                                "pool_vault": s["pool_vault"], "quote_mint": mv.get("quote_mint"),
                                "min_out": mo, "tx_size": built["size"], "build_ms": round(ms, 3),
                                **cl})
        bm = sorted(stat["build_ms"])
        stat["build_ms_median"] = bm[len(bm) // 2] if bm else None
        stat["build_ms_max"] = bm[-1] if bm else None
        stat["simulated"] = n
        stat.pop("build_ms")
        out["types"][spec["label"]] = stat
        print(spec["label"], stat)
    try:
        out["launchlab_5HdXjagm52"] = launchlab_check(rpc)
    except (RuntimeError, KeyError, StopIteration, ValueError) as exc:
        out["launchlab_5HdXjagm52"] = {"ok": False, "why_not": f"{type(exc).__name__}: {str(exc)[:160]}"}
    print("launchlab:", json.dumps(out["launchlab_5HdXjagm52"], ensure_ascii=False, default=str))
    out["credits_this_run"] = rpc.stats.get("кредитов")
    out["c2_usage"] = C.c2_usage_report()
    (C.DATA / f"c2_swap_sim_{C.today_utc()}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    for r in out["rows"]:
        if r.get("verdict") == "other_error":
            print("ДРУГАЯ ОШИБКА", r["type"], r["source_sig"][:16], r.get("err"), r.get("tail"))
    print(f"кредитов {rpc.stats.get('кредитов')}; C2 сегодня {C.c2_spent_today()}")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        ok = classify({"err": {"InstructionError": [4, {"Custom": 1}]},
                       "logs": ["Program log: Error: insufficient funds"]})["verdict"] == "balance_error"
        ok2 = classify({"err": {"InstructionError": [4, {"Custom": 2006}]},
                        "logs": ["Program log: AnchorError ConstraintSeeds"]})["verdict"] == "other_error"
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] ошибка баланса распознаётся")
        print(f"  [{'ok  ' if ok2 else 'СБОЙ'}] ошибка ограничений счетов -- не баланс")
        print(f"самопроверка c2_swap_sim: {int(ok) + int(ok2)}/2 пройдено")
        raise SystemExit(0 if ok and ok2 else 1)
    raise SystemExit(main())
