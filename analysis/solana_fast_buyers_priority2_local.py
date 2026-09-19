#!/usr/bin/env python3
"""Владелец, приоритет 2: цена скорости -- ЛОКАЛЬНЫЙ пересчёт из уже
полученных 42 транзакций (data/solana_fast_buyers_services.json),
никаких новых RPC-вызовов.

Приоритет в SOL = compute_unit_price_microlamports * compute_unit_limit
/ 1e6 (микролампорты -> лампорты) / 1e9 (лампорты -> SOL) -- честно
используем CU LIMIT (реально запрошенный потолок), не CU USED (это
поле не сохранено в исходном RPC-дампе, только запрошенный лимит --
явная оговорка).

"Неопознанные" пересчитаны, как просил владелец: ТОЛЬКО переводы
kind='SOL' (нативный лампорт-трансфер на явный адрес) считаются как
"получатели SOL" -- WSOL-переводы (обычно внутрипуловые/протокольные
движения, не отдельный получатель) и program_id (не кошелёк) в долю
не входят."""
from __future__ import annotations

import json
from pathlib import Path
from statistics import median

REPO_ROOT = Path(__file__).resolve().parent.parent
IN_PATH = REPO_ROOT / "data" / "solana_fast_buyers_services.json"
OUT_PATH = REPO_ROOT / "data" / "solana_fast_buyers_priority2_result.json"

DT8_AKBOT_SIG = "65WA3e1VhP8Ynsyg1Jv7onsvDciZSFB7zNzwvpHXQ56gzg4KgqteQr57qKX8q8hTBehyW6mkx2E8sKN66oHQ1Q6t"
EXTERNAL_CLAIM_TIP_SOL = 1.800007105
EXTERNAL_CLAIM_FEE_SOL = 0.010005


def priority_fee_sol(row: dict) -> float | None:
    price, limit = row.get("compute_unit_price_microlamports"), row.get("compute_unit_limit")
    if price is None or limit is None:
        return None
    return price * limit / 1e6 / 1e9


def main() -> None:
    d = json.loads(IN_PATH.read_text())
    rows = d["fast_buyers"]

    enriched = []
    for r in rows:
        pf = priority_fee_sol(r)
        sol_transfers = [t for t in r["transfers"] if t.get("kind") == "SOL"]
        tips_sol = sum(t["amount_sol"] for t in sol_transfers)
        total = (pf or 0) + tips_sol
        size = r.get("size_sol")
        pct = round(total / size * 100, 4) if size else None
        enriched.append({
            "trade_label": r["trade_label"], "signature": r["signature"], "signer": r["signer"],
            "position_from_source": r["position_from_source"], "size_sol": size,
            "priority_fee_sol": pf, "tips_sol": round(tips_sol, 9) if sol_transfers else 0.0,
            "sol_transfers": sol_transfers,
            "total_priority_plus_tips_sol": round(total, 9),
            "pct_of_trade_size": pct,
            "is_bloom": any(t["service"].startswith("Bloom") for t in r["transfers"]),
        })

    # ---------- Позиция -> медиана ----------
    by_pos: dict[int, list[dict]] = {}
    for e in enriched:
        by_pos.setdefault(e["position_from_source"], []).append(e)
    position_table = []
    for pos in sorted(by_pos):
        es = by_pos[pos]
        totals = [e["total_priority_plus_tips_sol"] for e in es]
        pcts = [e["pct_of_trade_size"] for e in es if e["pct_of_trade_size"] is not None]
        position_table.append({
            "position": pos, "n": len(es),
            "median_priority_plus_tips_sol": round(median(totals), 6) if totals else None,
            "median_pct_of_trade_size": round(median(pcts), 4) if pcts else None,
            "n_pct_resolvable": len(pcts),
        })

    bloom_rows = [e for e in enriched if e["is_bloom"]]

    # ---------- Проверка внешнего утверждения по DT8/AKBot ----------
    dt8_row = next((e for e in enriched if e["signature"] == DT8_AKBOT_SIG), None)
    dt8_check = None
    if dt8_row:
        landx_transfers = [t for t in dt8_row["sol_transfers"] if "LandX" in t["destination"] or t["destination"].lower().startswith("landx")]
        got_tip = landx_transfers[0]["amount_sol"] if landx_transfers else None
        got_fee = dt8_row["priority_fee_sol"]
        dt8_check = {
            "signature": DT8_AKBOT_SIG,
            "claimed_tip_sol": EXTERNAL_CLAIM_TIP_SOL, "got_tip_sol": got_tip,
            "tip_match": got_tip is not None and abs(got_tip - EXTERNAL_CLAIM_TIP_SOL) < 1e-6,
            "claimed_fee_sol": EXTERNAL_CLAIM_FEE_SOL, "got_fee_sol": got_fee,
            "fee_match_within_0.001": got_fee is not None and abs(got_fee - EXTERNAL_CLAIM_FEE_SOL) < 0.001,
            "note": "fee -- честно по CU_LIMIT (запрошенный потолок), не CU_USED (не сохранён в исходном RPC-дампе), возможна небольшая погрешность",
        }

    # ---------- "Неопознанные" -- ТОЛЬКО SOL-переводы на явные кошельки ----------
    n_pos1to3_sol_transfers = 0
    n_pos1to3_sol_transfers_identified = 0
    for e in enriched:
        if e["position_from_source"] not in (1, 2, 3):
            continue
        for t in e["sol_transfers"]:
            n_pos1to3_sol_transfers += 1
            if t["service"] != "не опознан":
                n_pos1to3_sol_transfers_identified += 1
    share_identified = round(n_pos1to3_sol_transfers_identified / n_pos1to3_sol_transfers, 4) if n_pos1to3_sol_transfers else None

    result = {
        "note": ("Локальный пересчёт из уже сохранённых 42 транзакций -- новых RPC-вызовов не было. "
                 "priority_fee_sol честно по CU_LIMIT (запрошенный потолок), не CU_USED (недоступен в "
                 "исходном RPC-дампе jsonParsed без отдельного вызова getTransactionStatus/CU meter)."),
        "n_rows": len(enriched),
        "position_table": position_table,
        "bloom_rows": bloom_rows,
        "dt8_akbot_external_claim_check": dt8_check,
        "n_pos1to3_sol_transfers_total": n_pos1to3_sol_transfers,
        "n_pos1to3_sol_transfers_identified": n_pos1to3_sol_transfers_identified,
        "share_pos1to3_sol_transfers_identified": share_identified,
        "share_pos1to3_sol_transfers_unidentified": round(1 - share_identified, 4) if share_identified is not None else None,
        "all_rows": enriched,
    }
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(position_table, ensure_ascii=False, indent=2))
    print("DT8/AKBot check:", json.dumps(dt8_check, ensure_ascii=False, indent=2))
    print(f"доля SOL-переводов на позициях 1-3, отправленных на опознанные адреса: {share_identified}")
    print(f"Bloom rows: {len(bloom_rows)}")


if __name__ == "__main__":
    main()
