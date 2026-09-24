#!/usr/bin/env python3
"""C2: двухшаговые маршруты для теневой сборки v2 + глубина толпы. БЕЗ ОТПРАВКИ.

Часть 1 -- кэш шаблонов первого шага (SOL -> промежуточный токен Q):
* откуда пулы SOL<->Q: из маршрутов самих источников (их транзакции из
  задачи A): инструкции поддерживаемых программ, где одно хранилище WSOL,
  другое Q. Глубина -- текущие остатки хранилищ (getMultipleAccounts);
* шаблон -- последняя чужая сделка WSOL -> Q в самом глубоком пуле
  (getSignaturesForAddress по хранилищу Q + getTransaction);
* для пулов, где счета инструкции от цены не зависят (Pump AMM, CPMM,
  DAMM v2, Launchlab), шаблон обновлять незачем; для DLMM и CLMM (бины, тики)
  -- обновление раз в 10 с, отказ при возрасте > 30 с;
* реплей: по каждой сделке задачи A с котировкой Q -- сборка шага 1
  (из кэша) + шага 2 (из транзакции источника) одной транзакцией от
  нашего адреса и simulateTransaction (sigVerify=false).
Часть 2 -- глубина толпы: чистый приток котировки в пул источника за
30 с (остаток хранилища котировки на момент цены P30 минус остаток
сразу после источника) к глубине пула после источника -- для пулов, где
остатки -- резервы (Pump AMM, CPMM, AMM v4). Отвечает на «та же толпа
не двигает цену»: одинаковый приток в долях глубины должен давать
одинаковый рост.
Служба c2_twohop.
"""
from __future__ import annotations

import collections
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import c2_shadow_build as SB  # noqa: E402
import c2_swap_build as B  # noqa: E402

SERVICE = "c2_twohop"
LEG_PROGRAMS = {B.PUMP_AMM, B.CPMM, B.DAMM2, B.LAUNCHLAB, B.DLMM, B.CLMM}
PRICE_DEPENDENT = {B.DLMM, B.CLMM}
RESERVE_PROGRAMS = {"pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA", "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",
                    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"}
CACHE_CYCLES = 7          # 7 обновлений с шагом 10 с = 1 минута живого цикла
OK_VERDICTS = ("would_pass", "balance", "slippage")


def sol_pools_in_tx(tx: dict) -> list:
    """(программа, хранилище Q, хранилище WSOL, Q) для пулов SOL<->Q в маршруте."""
    rows = {r["account"]: r for r in C.token_rows(tx).values()}
    out = []
    for ix in B.all_instructions(tx):
        if ix.get("programId") not in LEG_PROGRAMS:
            continue
        vs = [rows[a] for a in ix["accounts"] if a in rows and rows[a]["owner"] not in C.signers(tx)]
        w = [r for r in vs if r["mint"] == C.WSOL]
        q = [r for r in vs if r["mint"] not in (C.WSOL,)]
        for qr in q:
            for wr in w:
                if qr["owner"] == wr["owner"] or ix["programId"] in (B.DLMM, B.DAMM2, B.CLMM):
                    out.append((ix["programId"], qr["account"], wr["account"], qr["mint"]))
    return out


def main() -> int:
    key = C.RC.helius_key()[0]
    if not key:
        print("СТОП: ключ Helius не задан", file=sys.stderr)
        return 2
    rpc = C.C2Rpc(SERVICE, key=key)
    import c2_pool_programs as PP  # noqa: PLC0415
    lab = PP.labels()
    src = sorted(C.DATA.glob("crowd_metric_2*.json"))[-1]
    d = json.loads(src.read_text(encoding="utf-8"))
    trades = [dict(t, source=ps["address"]) for ps in d["per_source"] for t in ps.get("trades") or []
              if t.get("signature")]
    n_all = len(trades)
    got = rpc.get_txs([t["signature"] for t in trades])
    out: dict = {"generated_utc": C.utc(time.time()), "n_trades": n_all}

    # ---------- часть 2: глубина толпы (нужны транзакции источника и P30)
    p30 = rpc.get_txs([t["price_30s_sig"] for t in trades
                       if t.get("price_30s_sig") and t.get("pool_program") in RESERVE_PROGRAMS
                       and not t.get("last_trade_is_source_30s")])
    depth_rows = []
    for t in trades:
        if t.get("pool_program") not in RESERVE_PROGRAMS or t.get("growth_after_30s") is None:
            continue
        stx = got.get(t["signature"])
        pool = C.identify_pool(stx or {}, t["source"], t["mint"]) if stx else {"ok": False}
        if not pool.get("ok"):
            continue
        qv = pool["quote_vault"]
        rs = {r["account"]: r for r in C.token_rows(stx).values()}
        if qv not in rs:
            continue
        q0 = rs[qv]["post"]
        if t.get("last_trade_is_source_30s"):
            q30 = q0
        else:
            tx30 = p30.get(t.get("price_30s_sig"))
            r30 = {r["account"]: r for r in C.token_rows(tx30 or {}).values()}.get(qv)
            if not r30:
                continue
            q30 = r30["post"]
        dec = rs[qv]["dec"]
        depth_rows.append({"signature": t["signature"], "quote_mint": pool["quote_mint"],
                           "depth_quote": float(C.ui(q0, dec)), "net_inflow_quote": float(C.ui(q30 - q0, dec)),
                           "inflow_to_depth": (q30 - q0) / q0 if q0 else None,
                           "net_inflow_sol": float(C.ui(q30 - q0, dec)) if pool["quote_mint"] == C.WSOL else None,
                           "depth_sol": float(C.ui(q0, dec)) if pool["quote_mint"] == C.WSOL else None,
                           "growth_after_30s": t["growth_after_30s"], "crowd_30s": t.get("crowd_30s")})
    out["depth_rows"] = depth_rows

    # ---------- часть 1: пулы SOL<->Q из маршрутов источников
    pools: dict = {}
    for t in trades:
        for prog, qv, wv, q in sol_pools_in_tx(got.get(t["signature"]) or {}):
            pools.setdefault(q, {}).setdefault((prog, qv, wv), 0)
            pools[q][(prog, qv, wv)] += 1
    need = collections.Counter(t["quote_mint"] for t in trades if t.get("quote_mint")
                               and t["quote_mint"] not in (C.WSOL, C.NATIVE_QUOTE))
    # в маршрутах источников пула SOL<->Q нет -- ищем по свежим сделкам минта Q
    found_by_mint = {}
    for q in [q for q in need if q not in pools]:
        sigs = [x["signature"] for x in rpc.signatures(q, limit=15) if x.get("err") is None][:12]
        for tx in rpc.get_txs(sigs).values():
            for prog, qv, wv, q2 in sol_pools_in_tx(tx or {}):
                if q2 == q:
                    pools.setdefault(q, {}).setdefault((prog, qv, wv), 0)
                    pools[q][(prog, qv, wv)] += 1
                    found_by_mint[q] = True
    out["pools_found_by_mint"] = len(found_by_mint)
    vaults = sorted({v for q in pools.values() for k in q for v in (k[1], k[2])})
    bal = {}
    for i in range(0, len(vaults), 100):
        part = vaults[i:i + 100]
        vals = (rpc.call("getMultipleAccounts", [part, {"encoding": "jsonParsed"}]) or {}).get("value") or []
        for a_, v in zip(part, vals):
            inf = ((((v or {}).get("data") or {}).get("parsed") or {}).get("info") or {})
            bal[a_] = (inf.get("tokenAmount") or {}).get("uiAmount")
    leg_pools, cov = {}, []
    for q, cnt in need.most_common():
        cands = sorted(pools.get(q, {}), key=lambda k: -(bal.get(k[2]) or 0))
        row = {"quote": q, "signals": cnt, "share": round(cnt / n_all, 4)}
        if not cands:
            row["status"] = "нет пула SOL<->Q поддерживаемого типа в маршрутах источников"
        else:
            prog, qv, wv = cands[0]
            leg_pools[q] = {"program": prog, "q_vault": qv, "w_vault": wv, "sol_depth": bal.get(wv)}
            row.update(pool_program=lab.get(prog, prog), q_vault=qv, sol_depth=bal.get(wv),
                       found_by="минт Q" if q in found_by_mint else "маршрут источника",
                       price_dependent=prog in SB.PRICE_DEPENDENT, status="пул есть")
        cov.append(row)
    out["pools_per_quote"] = cov
    (C.DATA / f"c2_leg_pools_{C.today_utc()}.json").write_text(json.dumps(
        {"generated_utc": out["generated_utc"], "pools": leg_pools}, ensure_ascii=False, indent=1),
        encoding="utf-8")

    # ---------- кэш: живой цикл обновления раз в 10 с, замер вызовов
    crpc = C.C2Rpc(SERVICE + "_cache", key=key)
    cache = SB.LegCache(leg_pools, crpc.call, allow_polling=True)
    t_start = time.time()
    statuses = []
    for i in range(CACHE_CYCLES):
        t0 = time.time()
        st = cache.refresh_all()
        statuses.append({"cycle": i, "sec": round(time.time() - t0, 2),
                         "by_status": dict(collections.Counter(st.values()))})
        if i < CACHE_CYCLES - 1:
            time.sleep(max(0.0, SB.LEG_REFRESH_S - (time.time() - t0)))
    minutes = (time.time() - t_start) / 60
    calls = dict(cache.calls)
    out["cache_measure"] = {"pools": len(leg_pools), "cycles": CACHE_CYCLES, "minutes": round(minutes, 2),
                            "calls": calls, "calls_per_min": round(sum(calls.values()) / minutes, 1),
                            "calls_per_min_by_method": {k: round(v / minutes, 1) for k, v in calls.items()},
                            "cycles_detail": statuses,
                            "templates": {q: {"program": lab.get(e["program"], e["program"]),
                                              "trade_age_s": round(time.time() - e["trade_time"], 1)
                                              if e.get("trade_time") else None}
                                          for q, e in cache.entries.items()}}

    # ---------- реплей: настоящий shadow_build, кэш обновляется в фоне
    stop = threading.Event()
    th = threading.Thread(target=cache.run, args=(stop,), daemon=True)
    calls_before = sum(cache.calls.values())
    t_bg = time.time()
    th.start()
    rows = []
    try:
        for t in trades:
            prog, q = t.get("pool_program"), t.get("quote_mint")
            r = {"signature": t["signature"], "pool_program": lab.get(prog, prog), "quote": q}
            if not prog or not q:
                r["outcome"] = "нет данных о пуле в A"
            elif prog not in SB.SUPPORTED:
                r["outcome"] = "тип пула не покрыт сборщиком"
            elif q in (C.WSOL, C.NATIVE_QUOTE):
                r["outcome"] = "один шаг SOL (сборка проверена в D)"
            else:
                stx = got.get(t["signature"])
                if not stx:
                    r["outcome"] = "узел не отдал транзакцию источника"
                else:
                    res = SB.shadow_build(stx, t["source"], t["mint"], C.EXECUTOR_WALLET, 20_000_000, rpc.call,
                                          leg_cache=cache)
                    r.update({k: res.get(k) for k in ("route", "sim_verdict", "why_not", "build_ms", "sim_ms",
                                                      "leg1_flipped",
                                                      "tx_size", "leg1_pool_program", "leg1_template_age_s",
                                                      "leg1_trade_age_s", "hot_lut_calls", "sim_units")})
                    r["sim_logs_tail"] = (res.get("sim_logs_tail") or [])[-3:]
                    if res.get("ok") and res.get("sim_verdict") in OK_VERDICTS:
                        r["outcome"] = "два шага: собрано, программа дошла до сумм"
                    elif res.get("ok") and "PoolMigrated" in " ".join(res.get("sim_logs_tail") or []):
                        r["outcome"] = "шаг 2: пул источника с тех пор мигрировал (реплей старой сделки)"
                    elif res.get("ok"):
                        r["outcome"] = f"два шага: симуляция {res.get('sim_verdict')}"
                    else:
                        r["outcome"] = f"два шага: отказ ({(res.get('why_not') or '')[:60]})"
            rows.append(r)
    finally:
        stop.set()
        th.join(timeout=30)
    bg_min = (time.time() - t_bg) / 60
    out["cache_measure"]["background_calls_per_min"] = round((sum(cache.calls.values()) - calls_before)
                                                             / max(bg_min, 1e-9), 1)
    out["cache_measure"]["background_minutes"] = round(bg_min, 2)
    out["replay"] = rows
    out["template_rejects"] = {q: dict(c) for q, c in cache.reject.items()}
    out["template_rejects_total"] = dict(sum((c for c in cache.reject.values()), collections.Counter()))
    fl = [r for r in rows if r.get("route") == "two_hop" and r.get("sim_verdict")]
    out["flipped_templates"] = {
        k: dict(collections.Counter(r["sim_verdict"] for r in fl if bool(r.get("leg1_flipped")) == k))
        for k in (True, False)}
    legs = [{"tx": e["tx"], "pool_vault": e["mv"]["base_vault"], "source": None, "mint": q,
             "quote_mint": C.WSOL, "flipped": True} for q, e in cache.entries.items() if e["tpl"].get("flipped")]
    if legs:
        (B.SAMPLES_DIR / "legs_flipped.json").write_text(json.dumps(legs, ensure_ascii=False), encoding="utf-8")
    covered = {"один шаг SOL (сборка проверена в D)", "два шага: собрано, программа дошла до сумм"}
    big = {c["quote"] for c in cov if c["share"] >= 0.02}
    by_type: dict = {}
    for r in rows:
        k = r["pool_program"] or "нет данных"
        by_type.setdefault(k, collections.Counter())[r["outcome"]] += 1
    n_cov = sum(r["outcome"] in covered for r in rows)
    out["coverage"] = {
        "n_signals": n_all, "covered": n_cov, "coverage_share": round(n_cov / n_all, 3),
        "one_hop_sol": sum(r["outcome"].startswith("один шаг") for r in rows),
        "two_hop_ok": sum(r["outcome"] == "два шага: собрано, программа дошла до сумм" for r in rows),
        "two_hop_ok_quotes_ge_2pct": sum(r["outcome"] == "два шага: собрано, программа дошла до сумм"
                                         and r["quote"] in big for r in rows),
        "outcomes": dict(collections.Counter(r["outcome"] for r in rows)),
        "by_pool_type": {k: dict(v) for k, v in sorted(by_type.items(), key=lambda kv: -sum(kv[1].values()))}}
    bms = [r["build_ms"] for r in rows if r.get("build_ms") is not None and r.get("route") == "two_hop"]
    out["two_hop_build_ms_median"] = round(C.median(bms), 3) if bms else None
    out["credits_this_run"] = rpc.stats.get("кредитов")
    out["c2_usage"] = C.c2_usage_report()
    (C.DATA / f"c2_twohop_{C.today_utc()}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1,
                                                                        default=str), encoding="utf-8")
    print("покрытие:", json.dumps(out["coverage"], ensure_ascii=False))
    print("кэш:", json.dumps({k: v for k, v in out["cache_measure"].items() if k != "cycles_detail"},
                             ensure_ascii=False)[:1500])
    for k, v in out["coverage"]["by_pool_type"].items():
        print("  ", k, json.dumps(v, ensure_ascii=False))
    print("почему сделки не шаблоны:", json.dumps(out["template_rejects_total"], ensure_ascii=False))
    print("глубина: строк", len(depth_rows))
    print(f"кредитов {rpc.stats.get('кредитов')}; C2 сегодня {C.c2_spent_today()}")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        txs = C.load_real_txs()
        t = next(x for s, x in txs.items() if s.startswith("2uPwpSAQ"))
        p = sol_pools_in_tx(t)
        ok = any(q.startswith("Xsa62P5m") for _, _, _, q in p)
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] пул SOL<->xStock найден в маршруте настоящей транзакции: {[(a[:6], q[:6]) for a, _, _, q in p]}")
        print(f"самопроверка c2_twohop: {int(ok)}/1 пройдено")
        raise SystemExit(0 if ok else 1)
    raise SystemExit(main())
