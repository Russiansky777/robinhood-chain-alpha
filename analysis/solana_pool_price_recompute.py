#!/usr/bin/env python3
"""Пересчёт наценки от ЦЕНЫ ПУЛА, а не от средней цены исполнения лидера.

Замечание владельца, принятое полностью: точка отсчёта была неверная.
Для пула с x*y=k средняя цена исполнения ровно посередине между ценой до
и ценой после: avg = sqrt(до * после). Значит цена пула сразу после
сделки лидера примерно в (1+p) раз выше его средней цены, и прежние
"наценка к лидеру" и "(в) мы" включали вторую половину следа самого
лидера, то есть были завышены.

Как считаем теперь -- всё по РЕЗЕРВАМ ПУЛА, взятым из балансов хранилищ
в самих транзакциях (preTokenBalances / postTokenBalances):

    (а) лидер:  цена пула ДО его сделки      -> цена пула ПОСЛЕ его сделки
    (б) дрейф:  цена пула после лидера       -> цена пула ПЕРЕД нашей сделкой
    (в) мы:     цена пула перед нами         -> наша СРЕДНЯЯ цена исполнения

Средние цены исполнения лидера и наши остаются отдельными колонками для
справки -- они честные, просто не годятся как граница между вкладами.

Оценка резерва R ~ x/p больше не нужна и не используется: резерв читается
прямо. Если хранилища пула в транзакции не опознаются -- так и пишем, с
причиной по конкретной сделке, без подстановки.

Только чтение цепочки.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from solana_crowd_scan import PUBLIC_RPC, Rpc, helius_key, now_utc, scrub  # noqa: E402

CACHE_PATH = REPO_ROOT / "data" / "solana_pilot_block_autopsy_cache.json"
OUT_PATH = REPO_ROOT / "data" / "solana_pool_price_recompute.json"

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTES = (WSOL, USDC, USDT)
A_SIZE = 7


def _bal_map(entries) -> dict:
    """{(owner, mint): (uiAmount, accountIndex)} из pre/postTokenBalances."""
    out = {}
    for b in entries or []:
        o, m = b.get("owner"), b.get("mint")
        if not o or not m:
            continue
        amt = ((b.get("uiTokenAmount") or {}).get("uiAmount"))
        out[(o, m)] = (float(amt or 0.0), b.get("accountIndex"))
    return out


def pool_reserves(tx: dict, mint: str, trader: str) -> dict:
    """Резервы пула ДО и ПОСЛЕ сделки по балансам его хранилищ.

    Хранилища опознаём так: владелец счёта НЕ трейдер, и этот владелец
    держит и нужный минт, и котировочный актив -- это и есть пара
    хранилищ одного пула. Если таких владельцев несколько (маршрут через
    два пула), берём того, у кого дельта минта наибольшая по модулю:
    именно он принял основной объём.
    """
    meta = (tx or {}).get("meta") or {}
    pre, post = _bal_map(meta.get("preTokenBalances")), _bal_map(meta.get("postTokenBalances"))
    owners_mint = {o for (o, m) in set(pre) | set(post) if m == mint and o != trader}
    best = None
    for o in owners_mint:
        for q in QUOTES:
            if (o, q) not in pre and (o, q) not in post:
                continue
            t0 = pre.get((o, mint), (0.0, None))[0]
            t1 = post.get((o, mint), (0.0, None))[0]
            q0 = pre.get((o, q), (0.0, None))[0]
            q1 = post.get((o, q), (0.0, None))[0]
            if t0 <= 0 or t1 <= 0 or q0 <= 0 or q1 <= 0:
                continue
            cand = {"владелец_хранилищ": o, "котировка": q,
                    "резерв_токена_до": t0, "резерв_токена_после": t1,
                    "резерв_котировки_до": q0, "резерв_котировки_после": q1,
                    "дельта_токена": t1 - t0}
            if best is None or abs(cand["дельта_токена"]) > abs(best["дельта_токена"]):
                best = cand
    if best is None:
        return {"ок": False, "почему": "хранилища пула в транзакции не опознаны: "
                                        "нет владельца, держащего и минт, и котировку "
                                        "с ненулевыми резервами по обе стороны"}
    best["цена_до"] = best["резерв_котировки_до"] / best["резерв_токена_до"]
    best["цена_после"] = best["резерв_котировки_после"] / best["резерв_токена_после"]
    best["ок"] = True
    return best


def exec_price(tx: dict, mint: str, trader: str) -> float | None:
    """Средняя цена исполнения трейдера: его котировочная нога / его токен."""
    meta = (tx or {}).get("meta") or {}
    pre, post = _bal_map(meta.get("preTokenBalances")), _bal_map(meta.get("postTokenBalances"))
    dt = post.get((trader, mint), (0.0, None))[0] - pre.get((trader, mint), (0.0, None))[0]
    if dt == 0:
        return None
    dq = 0.0
    for q in QUOTES:
        dq += post.get((trader, q), (0.0, None))[0] - pre.get((trader, q), (0.0, None))[0]
    if dq == 0:
        # Нога могла пройти нативным SOL -- берём дельту баланса подписанта.
        keys_raw = ((tx.get("transaction") or {}).get("accountKeys")
                    or ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or [])
        keys = [k.get("pubkey") if isinstance(k, dict) else k for k in keys_raw]
        if trader in keys:
            i = keys.index(trader)
            pb, po = meta.get("preBalances") or [], meta.get("postBalances") or []
            if i < len(pb) and i < len(po):
                dq = (po[i] - pb[i]) / 1e9
                if i == 0:
                    dq += (meta.get("fee") or 0) / 1e9
    return abs(dq) / abs(dt) if dq else None


def recompute_one(rpc: Rpc, row: dict, leader: str, wallet: str) -> dict:
    out = {"mint": row.get("mint"), "когда": row.get("наша_покупка_utc"),
           "лидер_sol": row.get("лидер_sol"), "наш_вход_sol": row.get("наш_вход_sol"),
           "итог_gross_pct": row.get("итог_gross_pct"),
           "средняя_цена_лидера_справочно": row.get("лидер_цена"),
           "средняя_цена_наша_справочно": row.get("наша_цена"),
           "прежняя_наценка_к_средней_лидера_pct": row.get("наша_наценка_к_лидеру_pct")}
    sig_lead, sig_ours = row.get("лидер_signature"), row.get("buy_signature")
    if not sig_lead or not sig_ours:
        out["не_удалось"] = "нет подписи лидера или нашей"
        return out
    opts = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}
    try:
        txs = rpc.transactions([sig_lead, sig_ours])
    except RuntimeError as exc:
        out["не_удалось"] = scrub(str(exc))[:160]
        return out
    tl, to = txs.get(sig_lead), txs.get(sig_ours)
    if not tl:
        out["не_удалось"] = "транзакция лидера не отдалась узлом"
        return out
    if not to:
        out["не_удалось"] = "наша транзакция не отдалась узлом"
        return out

    pl = pool_reserves(tl, row["mint"], leader)
    po = pool_reserves(to, row["mint"], wallet)
    out["пул_в_сделке_лидера"] = pl
    out["пул_в_нашей_сделке"] = po
    if not pl.get("ок"):
        out["не_удалось"] = "в сделке лидера: " + pl["почему"]
        return out
    if not po.get("ок"):
        out["не_удалось"] = "в нашей сделке: " + po["почему"]
        return out

    p_before = pl["цена_до"]
    p_after_leader = pl["цена_после"]
    p_before_us = po["цена_до"]
    our_exec = exec_price(to, row["mint"], wallet)
    lead_exec = exec_price(tl, row["mint"], leader)
    out["наша_средняя_цена_исполнения"] = our_exec
    out["средняя_цена_исполнения_лидера"] = lead_exec
    out["резерв_котировки_до_лидера"] = pl["резерв_котировки_до"]
    out["резерв_котировки_после_лидера"] = pl["резерв_котировки_после"]
    out["покупка_лидера_к_резерву"] = (round(row["лидер_sol"] / pl["резерв_котировки_до"], 4)
                                        if row.get("лидер_sol") and pl["резерв_котировки_до"] else None)
    out["наш_вход_к_резерву_перед_нами"] = (round(row["наш_вход_sol"] / po["резерв_котировки_до"], 4)
                                             if row.get("наш_вход_sol") and po["резерв_котировки_до"] else None)

    a = p_after_leader / p_before - 1
    b = p_before_us / p_after_leader - 1
    c = (our_exec / p_before_us - 1) if our_exec else None
    out["а_влияние_лидера_pct"] = round(a * 100, 3)
    out["б_дрейф_до_нас_pct"] = round(b * 100, 3)
    out["в_наше_влияние_pct"] = round(c * 100, 3) if c is not None else None
    if c is not None:
        итог = our_exec / p_before - 1
        out["итог_от_цены_пула_до_лидера_pct"] = round(итог * 100, 3)
        out["произведение_частей_pct"] = round(((1 + a) * (1 + b) * (1 + c) - 1) * 100, 3)
        out["сходимость_пп"] = round(((1 + a) * (1 + b) * (1 + c) - 1 - итог) * 100, 9)
        out["простая_сумма_pct"] = round((a + b + c) * 100, 3)
        out["перекрёстные_члены_пп"] = round(((a + b + c) - итог) * 100, 3)
        # Наценка к ЦЕНЕ ПУЛА после лидера -- то, что раньше считалось от
        # его средней цены и было завышено на вторую половину его следа.
        out["наценка_к_цене_пула_после_лидера_pct"] = round(((1 + b) * (1 + c) - 1) * 100, 3)
        old = row.get("наша_наценка_к_лидеру_pct")
        if old is not None:
            out["насколько_прежняя_наценка_была_завышена_пп"] = round(
                old - out["наценка_к_цене_пула_после_лидера_pct"], 3)
    return out


def _med(vals):
    v = [x for x in vals if x is not None]
    return round(statistics.median(v), 3) if v else None


def summarise(label: str, items: list[dict]) -> dict:
    good = [x for x in items if not x.get("не_удалось")]
    return {
        "выборка": label, "сделок": len(items), "пересчитано": len(good),
        "медиана_а_влияние_лидера_pct": _med([x.get("а_влияние_лидера_pct") for x in good]),
        "медиана_б_дрейф_pct": _med([x.get("б_дрейф_до_нас_pct") for x in good]),
        "медиана_в_наше_влияние_pct": _med([x.get("в_наше_влияние_pct") for x in good]),
        "медиана_наценки_к_пулу_pct": _med(
            [x.get("наценка_к_цене_пула_после_лидера_pct") for x in good]),
        "медиана_прежней_наценки_pct": _med(
            [x.get("прежняя_наценка_к_средней_лидера_pct") for x in good]),
        "медиана_завышения_прежней_наценки_пп": _med(
            [x.get("насколько_прежняя_наценка_была_завышена_пп") for x in good]),
        "медиана_резерва_котировки_до_лидера": _med(
            [x.get("резерв_котировки_до_лидера") for x in good]),
        "медиана_лидер_к_резерву": _med([x.get("покупка_лидера_к_резерву") for x in good]),
        "медиана_наш_вход_к_резерву": _med([x.get("наш_вход_к_резерву_перед_нами") for x in good]),
        "медиана_лидер_sol": _med([x.get("лидер_sol") for x in good]),
        "медиана_итог_gross_pct": _med([x.get("итог_gross_pct") for x in items]),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--leader", default="Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit")
    ap.add_argument("--wallet", default="E1qAJBmrJDhBvm2sV8kfMXFAmgzHuSRNRosPEgMkKiWS")
    ap.add_argument("--a-size", type=int, default=A_SIZE)
    ap.add_argument("--min-interval-s", type=float, default=0.05)
    ap.add_argument("--time-budget-s", type=int, default=30 * 60)
    ap.add_argument("--public-only", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return

    if not CACHE_PATH.exists():
        raise SystemExit(f"нет {CACHE_PATH} -- пересчитывать нечего")
    cache = json.loads(CACHE_PATH.read_text())
    rows = list((cache.get("строки") or {}).values())

    key, key_name = helius_key()
    rpc = Rpc(key, min_interval_s=args.min_interval_s, workers=2, service="разбор_пилота")
    if args.public_only:
        rpc.url = PUBLIC_RPC
        rpc.allow_public = False
    rpc.deadline = time.monotonic() + args.time_budget_s
    print(f"[пересчёт] сделок {len(rows)}, ключ из {key_name}, "
          f"{'только публичный' if args.public_only else 'Helius основной'}", flush=True)

    items = []
    for i, r in enumerate(rows, 1):
        items.append(recompute_one(rpc, r, args.leader, args.wallet))
        print(f"  {i}/{len(rows)} {r.get('mint','')[:10]} "
              f"{'ok' if not items[-1].get('не_удалось') else items[-1]['не_удалось'][:60]}",
              flush=True)

    out = {
        "generated_at_utc": now_utc(),
        "источник": "data/solana_pilot_block_autopsy_cache.json",
        "ЧЕСТНЫЕ_ОГОВОРКИ": [
            "Только чтение цепочки.",
            "Точка отсчёта -- ЦЕНА ПУЛА, а не средняя цена исполнения лидера. Для x*y=k "
            "средняя цена ровно посередине (avg = sqrt(до*после)), поэтому прежняя "
            "наценка включала вторую половину следа самого лидера.",
            "Резервы пула читаются ПРЯМО из preTokenBalances/postTokenBalances хранилищ "
            "в самих транзакциях. Оценка R ~ x/p больше не используется.",
            "Хранилища опознаются как счета владельца, который держит и минт, и котировку; "
            "при маршруте через несколько пулов берётся тот, где дельта минта наибольшая.",
            "Части перемножаются точно: (1+а)(1+б)(1+в) = наша средняя цена / цена пула до "
            "лидера. Простая сумма расходится на перекрёстные члены -- это арифметика "
            "процентов, показано колонкой.",
        ],
        "A": items[:args.a_size], "Б": items[args.a_size:],
        "сводка_A": summarise("A -- серия минусов", items[:args.a_size]),
        "сводка_Б": summarise("Б -- плюсовые 19-21.09", items[args.a_size:]),
        "rpc": {"calls": rpc.calls, "retries": rpc.retries, "кредитов": rpc.credits},
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps({"сводка_A": out["сводка_A"], "сводка_Б": out["сводка_Б"]},
                      ensure_ascii=False, indent=2))


def self_test() -> None:
    checks = []

    def chk(name, ok, got=""):
        checks.append((name, bool(ok), got))

    def TX(pre, post, keys=None, pre_b=None, post_b=None, fee=0):
        return {"meta": {"preTokenBalances": pre, "postTokenBalances": post,
                          "preBalances": pre_b, "postBalances": post_b, "fee": fee},
                "transaction": {"accountKeys": keys or []}}

    def B(owner, mint, amt, idx=0):
        return {"owner": owner, "mint": mint, "uiTokenAmount": {"uiAmount": amt},
                "accountIndex": idx}

    M = "MINT"
    # Пул: 1000 SOL и 1 000 000 токенов; лидер покупает на 100 SOL.
    pre = [B("POOL", M, 1_000_000), B("POOL", WSOL, 1000),
           B("LEADER", M, 0), B("LEADER", WSOL, 100)]
    post = [B("POOL", M, 909_090.909), B("POOL", WSOL, 1100),
            B("LEADER", M, 90_909.091), B("LEADER", WSOL, 0)]
    r = pool_reserves(TX(pre, post), M, "LEADER")
    chk("хранилища пула опознаны", r["ок"] and r["владелец_хранилищ"] == "POOL", str(r)[:120])
    chk("цена пула до", abs(r["цена_до"] - 0.001) < 1e-9, str(r["цена_до"]))
    chk("цена пула после выросла", r["цена_после"] > r["цена_до"])
    chk("цена после = резерв/резерв", abs(r["цена_после"] - 1100 / 909_090.909) < 1e-12)

    ep = exec_price(TX(pre, post), M, "LEADER")
    chk("средняя цена исполнения = нога/токен", abs(ep - 100 / 90_909.091) < 1e-12, str(ep))
    chk("средняя НИЖЕ цены пула после -- это и есть смещение точки отсчёта",
        ep < r["цена_после"], f"{ep} vs {r['цена_после']}")
    # Для x*y=k средняя ровно посередине: avg^2 = до*после
    chk("средняя = sqrt(до*после) с точностью до округления",
        abs(ep * ep - r["цена_до"] * r["цена_после"]) < 1e-12,
        str(ep * ep - r["цена_до"] * r["цена_после"]))

    chk("трейдер не путается с пулом",
        pool_reserves(TX(pre, post), M, "POOL").get("ок") is not True)
    empty = pool_reserves(TX([B("X", M, 5)], [B("X", M, 4)]), M, "TRADER")
    chk("нет котировки -- честный отказ с причиной",
        empty["ок"] is False and "не опознаны" in empty["почему"])

    # Нативная нога SOL, без WSOL у трейдера
    keys = [{"pubkey": "TRADER"}]
    t2 = TX([B("POOL", M, 1000), B("POOL", WSOL, 10), B("TRADER", M, 0)],
            [B("POOL", M, 900), B("POOL", WSOL, 11), B("TRADER", M, 100)],
            keys=keys, pre_b=[2_000_000_000], post_b=[1_000_000_000], fee=0)
    chk("нативная нога подхватывается", abs(exec_price(t2, M, "TRADER") - 0.01) < 1e-9,
        str(exec_price(t2, M, "TRADER")))

    s = summarise("тест", [{"не_удалось": "нет"}])
    chk("нерасшифрованная сделка в медианы не идёт",
        s["пересчитано"] == 0 and s["медиана_а_влияние_лидера_pct"] is None)
    chk("но из счёта не пропадает", s["сделок"] == 1)

    bad = 0
    for n, ok, got in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {n}" + (f"  -> {got}" if got and not ok else ""))
        bad += (not ok)
    print(f"самопроверка пересчёта: {len(checks) - bad}/{len(checks)} пройдено")
    if bad:
        raise SystemExit(f"самопроверка не пройдена: {bad} из {len(checks)}")


if __name__ == "__main__":
    main()
