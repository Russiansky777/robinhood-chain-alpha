#!/usr/bin/env python3
"""Владелец, 2026-09-19: v2 связки через подписанта дала шокирующий
результат -- 300/300 наших tx_id НАЙДЕНЫ в solana.transactions (граница
по block_slot починила timeout), но только 1/300 совпал по скалярной
колонке signer. Это невозможно при честной атрибуции: selected_300.json
построен строго через classify()+is_signer(tx, ЛИДЕР) на данных Alchemy
RPC -- лидер ТОЧНО был подписантом каждой из этих 300 транзакций.

Гипотеза: solana.transactions хранит ОДНОГО "signer" (вероятно fee payer /
первый обязательный подписант), а РЕАЛЬНЫЙ список -- в колонке "signers"
(массив, была в схеме v2, просто не использована). Наш лидер вполне может
быть НЕ первым подписантом (см. is_fee_payer/is_signer различие,
установленное в этой же сессии -- транзакции лидера идут через отдельного
плательщика комиссии/спонсора). Проверяем: contains(signers, ЛИДЕР) вместо
signer=ЛИДЕР, на ТОМ ЖЕ ограниченном по block_slot окне -- дёшево,
одна проверка перед перезапуском всей калибровки."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dune_explorer_check import DuneProbe, pick_working_key, step0_discover_keys  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dune_signer_linkage_v3.json"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
TX_TABLE, SIG_COL = "solana.transactions", "id"


def main() -> None:
    sel_rows = json.loads((REPO_ROOT / "data" / "solana_buyer_200" / "selected_300.json").read_text())
    sigs = [r["signature"] for r in sel_rows]
    min_slot, max_slot = min(r["slot"] for r in sel_rows), max(r["slot"] for r in sel_rows)

    discovery = step0_discover_keys()
    key_name = pick_working_key(discovery)
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "step0_key_discovery": discovery, "n_our_signatures": len(sigs)}
    if key_name is None:
        result["HONEST_ANSWER"] = "DUNE_EXPLORER_API не живой."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    probe = DuneProbe(os.environ[key_name])

    sig_list_sql = ",".join(f"'{s}'" for s in sigs)
    # Проверяем ОБЕ колонки разом в одном запросе -- дешевле, чем два прогона.
    sql = (f"SELECT {SIG_COL} AS tx_id, signer AS scalar_signer, signers AS signers_array, "
           f"contains(signers, '{LEADER_WALLET}') AS leader_in_signers_array, "
           f"cardinality(signers) AS n_signers "
           f"FROM {TX_TABLE} WHERE block_slot BETWEEN {min_slot - 1000} AND {max_slot + 1000} "
           f"AND {SIG_COL} IN ({sig_list_sql})")
    r = probe.run_sql_sync("signers_array_test", sql, timeout_s=590)
    result["dune_step"] = {k: v for k, v in r.items() if k != "rows"}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if r.get("status") != "ok":
        result["FINAL_VERDICT"] = f"Запрос не выполнился ({r.get('status')})."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[v3] " + result["FINAL_VERDICT"], flush=True)
        return

    rows = r["rows"]
    n_found = len(rows)
    n_scalar_match = sum(1 for row in rows if row.get("scalar_signer") == LEADER_WALLET)
    n_array_match = sum(1 for row in rows if row.get("leader_in_signers_array"))
    n_signers_dist = {}
    for row in rows:
        n = row.get("n_signers")
        n_signers_dist[n] = n_signers_dist.get(n, 0) + 1
    result["n_found"] = n_found
    result["n_scalar_signer_matches_leader"] = n_scalar_match
    result["n_leader_in_signers_array"] = n_array_match
    result["n_signers_distribution"] = n_signers_dist
    result["sample_rows"] = rows[:5]
    result["recomputed_coverage_via_signers_array"] = round(n_array_match / len(sigs), 4) if sigs else None

    if n_array_match >= 250:
        result["FINAL_VERDICT"] = (
            f"ПОДТВЕРЖДЕНО ЧЕРЕЗ МАССИВ SIGNERS: {n_array_match}/{len(sigs)} "
            f"({result['recomputed_coverage_via_signers_array']:.1%}) -- лидер присутствует среди подписантов, "
            "просто не как единственный/первый (scalar signer, вероятно fee payer/спонсор, почти никогда не "
            "совпадает: {}). Нужно перезапустить калибровку (schlopping/+30с/разметка) с JOIN по "
            "contains(signers, ЛИДЕР), не по signer=ЛИДЕР.".format(n_scalar_match)
        )
    elif n_scalar_match >= 250:
        result["FINAL_VERDICT"] = "Внезапно scalar signer тоже совпал массово -- проверить на дублирование логики."
    else:
        result["FINAL_VERDICT"] = (
            f"Гипотеза НЕ подтвердилась: ни scalar signer ({n_scalar_match}/{len(sigs)}), ни массив signers "
            f"({n_array_match}/{len(sigs)}) не показывают лидера в большинстве строк. Странность solana.transactions "
            "для этих 300 tx_id остаётся необъяснённой -- см. sample_rows для ручного разбора."
        )
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[v3] {result['FINAL_VERDICT']}", flush=True)


if __name__ == "__main__":
    main()
