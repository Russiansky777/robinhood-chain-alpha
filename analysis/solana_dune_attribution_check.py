#!/usr/bin/env python3
"""Владелец, 2026-09-19: последняя проверка Dune перед окончательным
закрытием -- атрибуция, не покрытие.

Обе прежние "улики структурной дыры" (35 строк, 6 DEX-меток) получены
из выборки, отфильтрованной по trader_id=лидер. Если Dune реально видит
наши 300 транзакций, но с ДРУГИМ trader_id -- проблема в атрибуции, а
не в decoder'е. Проверка: запрос по нашим 300 tx_id БЕЗ фильтра по
трейдеру, отдельно для dex_solana.trades и pumpdotfun_solana.trades."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dune_explorer_check import DuneProbe, pick_working_key, step0_discover_keys  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dune_attribution_check.json"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"


def check_table(probe: DuneProbe, table: str, sigs: list[str]) -> dict:
    sig_list_sql = ",".join(f"'{s}'" for s in sigs)
    sql = f"SELECT tx_id, trader_id, project FROM {table} WHERE tx_id IN ({sig_list_sql})"
    r = probe.run_sql_sync(f"attribution_{table.replace('.', '_')}", sql, timeout_s=300)
    out = {"table": table, "dune_step": {k: v for k, v in r.items() if k != "rows"}}
    if r.get("status") != "ok":
        return out
    rows = r["rows"]
    out["n_found"] = len(rows)
    out["n_distinct_tx_id"] = len({row["tx_id"] for row in rows})
    matching_leader = [row for row in rows if row.get("trader_id") == LEADER_WALLET]
    other_trader = [row for row in rows if row.get("trader_id") != LEADER_WALLET]
    out["n_trader_id_matches_leader"] = len(matching_leader)
    out["n_trader_id_other"] = len(other_trader)
    from collections import Counter
    out["other_trader_ids_sample"] = list({row.get("trader_id") for row in other_trader})[:10]
    out["projects_seen"] = dict(Counter(row.get("project") for row in rows))
    out["sample_rows"] = rows[:10]
    return out


def main() -> None:
    sel = json.loads((REPO_ROOT / "data" / "solana_buyer_200" / "selected_300.json").read_text())
    sigs = [r["signature"] for r in sel]

    discovery = step0_discover_keys()
    key_name = pick_working_key(discovery)
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "step0_key_discovery": discovery, "n_our_signatures": len(sigs)}
    if key_name is None:
        result["HONEST_ANSWER"] = "DUNE_EXPLORER_API не живой -- дальше идти некуда."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[dune_attr] " + result["HONEST_ANSWER"], flush=True)
        return
    probe = DuneProbe(os.environ[key_name])

    result["dex_solana_trades"] = check_table(probe, "dex_solana.trades", sigs)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[dune_attr] dex_solana.trades: найдено по tx_id={result['dex_solana_trades'].get('n_found')}, "
          f"из них trader_id=лидер={result['dex_solana_trades'].get('n_trader_id_matches_leader')}", flush=True)

    result["pumpdotfun_solana_trades"] = check_table(probe, "pumpdotfun_solana.trades", sigs)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[dune_attr] pumpdotfun_solana.trades: найдено по tx_id={result['pumpdotfun_solana_trades'].get('n_found')}, "
          f"из них trader_id=лидер={result['pumpdotfun_solana_trades'].get('n_trader_id_matches_leader')}", flush=True)

    n1 = result["dex_solana_trades"].get("n_found", 0)
    n2 = result["pumpdotfun_solana_trades"].get("n_found", 0)
    if n1 + n2 <= 5:
        result["FINAL_VERDICT"] = (
            f"НЕГОДЕН, ОКОНЧАТЕЛЬНО. По tx_id (без фильтра по trader_id) найдено всего {n1} (dex_solana.trades) + "
            f"{n2} (pumpdotfun_solana.trades) из 300 -- сделки физически отсутствуют в этих таблицах Dune, "
            f"дело не в атрибуции trader_id, а в отсутствии decode/индексации самих транзакций."
        )
    else:
        result["FINAL_VERDICT"] = (
            f"НАЙДЕНО БОЛЬШЕ, ЧЕМ РАНЬШЕ ({n1}+{n2} по tx_id против 1 по trader_id=лидер) -- "
            "смотри trader_id найденных строк: если это не лидер, проблема в атрибуции, не в покрытии. "
            "Нужна доп. проверка связки через подписанта транзакции (solana.transactions), не объявляю "
            "вердикт автоматически."
        )
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[dune_attr] {result['FINAL_VERDICT']}", flush=True)


if __name__ == "__main__":
    main()
