#!/usr/bin/env python3
"""Владелец, 2026-09-18: точечная проверка ОДНОЙ докупки лидера
(GNOME, ~995 USDC, ~18.09.2026 15:31 UTC), которую DBot отказался
копировать с причиной "Smart Money already holds this token". Метод НЕ
новый -- тот же самый find_price_at/plan_route_for_purchase, что и в
основном прогоне (solana_buyer200_fast_price.py /
solana_buyer200_select_extend.py), импортированы напрямую, не
переписаны. Основной прогон 300 покупок не трогает (отдельный выходной
файл, отдельный вызов)."""
from __future__ import annotations

import calendar
import json
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_buyer200_fast_price import (  # noqa: E402
    PRIOR_ROOT, OUT_ROOT, alchemy_available, get_signatures_for_address,
    get_transaction, find_price_at,
)
import solana_buyer200_fast_price as fp  # noqa: E402  (для живого RPC_CALLS, см. select_extend.py)
from solana_buyer200_select_extend import classify, plan_route_for_purchase  # noqa: E402

WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
OUT_PATH = OUT_ROOT / "gnome_pointcheck_result.json"

# Окно поиска сделки лидера: владелец назвал "около 15:31 UTC" -- берём
# запас +/-10 минут вокруг заявленного времени 18.09.2026.
TARGET_TIME_APPROX = calendar.timegm(time.strptime("2026-09-18T15:31:00Z", "%Y-%m-%dT%H:%M:%SZ"))
SEARCH_WINDOW_S = 600
TARGET_USDC_APPROX = D("995")
HORIZONS = [5, 15, 30, 60]

POOL_META_PATH = PRIOR_ROOT.parent / "solana_three_check" / "pool_meta.json"
ROUTE_META_PATH = PRIOR_ROOT / "route_meta.json"


def find_leader_tx() -> dict | None:
    """Листаем подписи кошелька лидера назад от чуть-позже целевого
    времени, ищем транзакцию с classify()==selected, spent близко к 995
    USDC, blockTime в пределах SEARCH_WINDOW_S от TARGET_TIME_APPROX."""
    hi = TARGET_TIME_APPROX + SEARCH_WINDOW_S
    lo = TARGET_TIME_APPROX - SEARCH_WINDOW_S
    before = None
    candidates = []
    for _ in range(20):
        batch = get_signatures_for_address(WALLET, before=before, limit=1000)
        if not batch:
            break
        for s in batch:
            bt = s.get("blockTime")
            if bt is None:
                continue
            if bt > hi:
                continue
            if bt < lo:
                return _pick_best(candidates)
            if s.get("err") is not None:
                continue
            candidates.append(s["signature"])
        before = batch[-1]["signature"]
        if len(batch) < 1000:
            break
    return _pick_best(candidates)


def _pick_best(sig_list: list[str]) -> dict | None:
    best = None
    for sig in sig_list:
        tx = get_transaction(sig)
        if tx is None:
            continue
        row, check = classify(tx, {"transactionIndex": None})
        if row is None:
            continue
        spent = D(row["usdc_spent"])
        if abs(spent - TARGET_USDC_APPROX) <= D("20"):  # запас на округление/сеть
            return {"tx": tx, "row": row, "check": check}
        if best is None:
            best = {"tx": tx, "row": row, "check": check, "spent_diff": abs(spent - TARGET_USDC_APPROX)}
    return best


def main() -> None:
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    alchemy_ok = alchemy_available()
    print(f"[gnome] alchemy_available={alchemy_ok}", flush=True)

    found = find_leader_tx()
    if not found:
        out["HONEST_ANSWER"] = (
            f"Не нашёл ни одной подходящей сделки лидера в окне "
            f"[{TARGET_TIME_APPROX - SEARCH_WINDOW_S},{TARGET_TIME_APPROX + SEARCH_WINDOW_S}] "
            f"(+/-{SEARCH_WINDOW_S}с вокруг заявленных 15:31 UTC 18.09.2026) с суммой близко к "
            f"{TARGET_USDC_APPROX} USDC -- см. классификацию каждой просмотренной подписи, "
            "если нужно расширить окно."
        )
        print("[gnome] " + out["HONEST_ANSWER"], flush=True)
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return

    row, tx = found["row"], found["tx"]
    out["leader_tx"] = {
        "signature": row["signature"], "slot": row["slot"], "block_time": row["time"],
        "mint": row["mint"], "usdc_spent": row["usdc_spent"], "tokens_received": row["tokens_received"],
        "zero_balance_before": row["zero_balance"], "entry_price_usdc": row["entry_usdc"],
    }
    print(f"[gnome] Найдена сделка лидера: {row['signature'][:20]}.. slot={row['slot']} "
          f"time={row['time']} mint={row['mint']} spent={row['usdc_spent']} "
          f"zero_balance_before={row['zero_balance']}", flush=True)
    out["dbot_reason_confirmed"] = (
        f"zero_balance_before={row['zero_balance']} -- {'ПОДТВЕРЖДЕНО: докупка (лидер уже держал токен)' if not row['zero_balance'] else 'ОПРОВЕРГНУТО: это первый вход, не докупка -- см. заявленную причину DBot ещё раз'}"
    )

    meta: dict = {}
    if POOL_META_PATH.exists():
        meta.update(json.loads(POOL_META_PATH.read_text()))
    if ROUTE_META_PATH.exists():
        meta.update(json.loads(ROUTE_META_PATH.read_text()))
    route, _ = plan_route_for_purchase(row["mint"], tx, meta)
    out["route"] = route
    if not route:
        out["HONEST_ANSWER"] = "Маршрут не найден (route_decoder_missing) -- дальше цену считать нечем."
        print("[gnome] " + out["HONEST_ANSWER"], flush=True)
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return
    print(f"[gnome] Маршрут: {route}", flush=True)

    points = {}
    for sec in HORIZONS:
        t = row["time"] + sec
        try:
            value = D(1)
            ok = True
            legs_out = []
            for leg in route:
                p = find_price_at(leg["pool"], t, row["time"], t)
                legs_out.append(p)
                if p.get("status") != "ok":
                    ok = False
                    break
                e = p["event"]
                v = D(e["p1_per_0"])
                if leg["from"] == e["m0"] and leg["to"] == e["m1"]:
                    value *= v
                elif leg["from"] == e["m1"] and leg["to"] == e["m0"]:
                    value /= v
                else:
                    ok = False
                    break
            points[sec] = {"status": "ok" if ok else (legs_out[-1].get("status") if legs_out else "no_legs"),
                           "price": str(value) if ok else None, "legs": legs_out}
        except RuntimeError as exc:
            points[sec] = {"status": "rpc_error", "error": str(exc)[:300]}
        print(f"[gnome] +{sec}с: {points[sec]['status']} price={points[sec].get('price')} "
              f"(RPC всего: {fp.RPC_CALLS})", flush=True)

    out["points"] = points
    p5 = points.get(5, {}).get("price")
    if p5 and D(p5) != 0:
        growth = {}
        for sec in [15, 30, 60]:
            p = points.get(sec, {}).get("price")
            if p:
                growth[sec] = float((D(p) / D(p5) - 1) * 100)
        out["growth_from_plus5s_pct"] = growth
        print(f"[gnome] Движение от +5с: {growth}", flush=True)
    else:
        out["HONEST_ANSWER_GROWTH"] = "Цена на +5с недоступна -- движение посчитать не могу."

    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"[gnome] Записано {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
