#!/usr/bin/env python3
"""Владелец, 2026-09-19: последний шаг проверки атрибуции Dune.

Прошлый шаг (solana_dune_attribution_check.py) нашёл: 298/300 наших
tx_id ЕСТЬ в dex_solana.trades (732 строки), но только 2 строки несут
trader_id=лидер -- 730 несут ДРУГОЙ trader_id, преимущественно один и
тот же адрес повторяется по многим разным project (meteora/raydium/
bisonfi/...) для ОДНИХ И ТЕХ ЖЕ транзакций. Похоже на то, что
dex_solana.trades даёт trader_id на уровне ОДНОЙ "ноги" мульти-хопового
свопа (промежуточный счёт/пул), а не подписанта всей транзакции.

Проверка: связать наши tx_id с их РЕАЛЬНЫМ подписантом через таблицу
сырых транзакций Solana на Dune. Сначала дёшево (LIMIT 1) смотрим
реальные имена колонок -- не гадаем заранее, чтобы не потратить кредиты
на запрос с неверным именем колонки."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dune_explorer_check import DuneProbe, pick_working_key, step0_discover_keys  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dune_signer_linkage_check.json"
ATTR_PATH = REPO_ROOT / "data" / "solana_dune_attribution_check.json"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"

TX_TABLE_CANDIDATES = ["solana.transactions", "solana_utils.transactions", "tokens_solana.transactions"]

SIG_COL_CANDIDATES = ["id", "tx_id", "signature"]
SIGNER_COL_CANDIDATES = ["signer", "fee_payer", "signers"]


def find_column(cols: list[str], candidates: list[str]) -> str | None:
    for c in candidates:
        if c in cols:
            return c
    for c in cols:
        low = c.lower()
        for cand in candidates:
            if cand in low:
                return c
    return None


def probe_schema(probe: DuneProbe, table: str) -> dict:
    r = probe.run_sql_sync(f"schema_probe_{table.replace('.', '_')}", f"SELECT * FROM {table} LIMIT 1", timeout_s=60)
    out = {"table": table, "status": r.get("status")}
    if r.get("status") == "ok":
        out["columns"] = (r.get("metadata") or {}).get("column_names")
        out["sample_row"] = r["rows"][0] if r.get("rows") else None
    else:
        out["error_preview"] = json.dumps(r.get("status_body") or r.get("execute_body") or r.get("results_body") or r.get("create_body"))[:300]
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
        print("[signer_link] " + result["HONEST_ANSWER"], flush=True)
        return
    probe = DuneProbe(os.environ[key_name])

    result["schema_probes"] = {}
    found_table = None
    sig_col = signer_col = None
    for tbl in TX_TABLE_CANDIDATES:
        sp = probe_schema(probe, tbl)
        result["schema_probes"][tbl] = sp
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print(f"[signer_link] схема {tbl}: {sp.get('status')} cols={sp.get('columns')}", flush=True)
        if sp.get("status") == "ok" and sp.get("columns"):
            s1 = find_column(sp["columns"], SIG_COL_CANDIDATES)
            s2 = find_column(sp["columns"], SIGNER_COL_CANDIDATES)
            if s1 and s2:
                found_table, sig_col, signer_col = tbl, s1, s2
                break

    if found_table is None:
        result["FINAL_VERDICT"] = (
            "НЕ НАЙДЕНА таблица сырых Solana-транзакций с колонками подписи+подписанта среди "
            f"кандидатов {TX_TABLE_CANDIDATES} -- связку через подписанта проверить не удалось. "
            "Возвращаемся к вердикту из solana_dune_attribution_check.py: атрибуция подтверждена "
            "как проблема (298/300 найдено по tx_id, но не по trader_id=лидер), но линковку через "
            "signer технически проверить на доступных таблицах не получилось -- честно, не выдумываем."
        )
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[signer_link] " + result["FINAL_VERDICT"], flush=True)
        return

    result["found_table"] = {"table": found_table, "sig_col": sig_col, "signer_col": signer_col}
    sig_list_sql = ",".join(f"'{s}'" for s in sigs)
    sql = f"SELECT {sig_col} AS tx_id, {signer_col} AS signer FROM {found_table} WHERE {sig_col} IN ({sig_list_sql})"
    r = probe.run_sql_sync(f"signer_linkage_{found_table.replace('.', '_')}", sql, timeout_s=300)
    result["dune_step"] = {k: v for k, v in r.items() if k != "rows"}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if r.get("status") != "ok":
        result["FINAL_VERDICT"] = f"Запрос к {found_table} не выполнился ({r.get('status')}) -- см. dune_step."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[signer_link] " + result["FINAL_VERDICT"], flush=True)
        return

    rows = r["rows"]
    signer_by_tx = {row["tx_id"]: row.get("signer") for row in rows}
    n_found = len(signer_by_tx)
    n_signer_leader = sum(1 for v in signer_by_tx.values() if v == LEADER_WALLET)
    result["n_tx_found_in_tx_table"] = n_found
    result["n_signer_matches_leader"] = n_signer_leader
    result["signer_coverage_by_tx_id"] = round(n_found / len(sigs), 4) if sigs else None
    result["signer_matches_leader_rate_of_found"] = round(n_signer_leader / n_found, 4) if n_found else None

    attr = json.loads(ATTR_PATH.read_text()) if ATTR_PATH.exists() else {}
    dex_rows = ((attr.get("dex_solana_trades") or {}).get("dune_step") or {}).get("rows") or []
    trader_by_tx: dict[str, set] = {}
    project_by_tx: dict[str, set] = {}
    for row in dex_rows:
        trader_by_tx.setdefault(row["tx_id"], set()).add(row.get("trader_id"))
        project_by_tx.setdefault(row["tx_id"], set()).add(row.get("project"))

    linked_ok = 0
    mismatch_examples = []
    for tx_id, signer in signer_by_tx.items():
        if signer != LEADER_WALLET:
            continue
        traders = trader_by_tx.get(tx_id, set())
        if LEADER_WALLET not in traders and traders:
            linked_ok += 1
            if len(mismatch_examples) < 10:
                mismatch_examples.append({"tx_id": tx_id, "signer": signer,
                                           "dex_solana_trader_ids_for_this_tx": sorted(traders),
                                           "projects": sorted(project_by_tx.get(tx_id, set()))})

    result["n_linked_via_signer_but_trader_id_mismatch_in_dex_trades"] = linked_ok
    result["mismatch_examples"] = mismatch_examples

    recomputed_coverage_via_signer = round(n_signer_leader / len(sigs), 4) if sigs else None
    result["recomputed_coverage_via_signer_linkage"] = recomputed_coverage_via_signer

    if n_signer_leader >= 250:
        result["FINAL_VERDICT"] = (
            f"АТРИБУЦИЯ, НЕ ПОКРЫТИЕ -- ОКОНЧАТЕЛЬНО. По {found_table}.{signer_col} у {n_signer_leader}/{len(sigs)} "
            f"({recomputed_coverage_via_signer:.1%}) наших транзакций подписант СОВПАДАЕТ с лидером -- покрытие "
            "реально высокое. dex_solana.trades просто не даёт trader_id на уровне подписанта транзакции для "
            "мульти-хоповых свопов (даёт его на уровне отдельной ноги/пула) -- это дизайн таблицы, не дыра в "
            "индексации. Вывод по прежней задаче (\"негоден по покрытию\") ОТМЕНЯЕТСЯ; корректный вывод: "
            "dex_solana.trades пригоден по ПОКРЫТИЮ через связку с solana.transactions по подписанту, но НЕ "
            "пригоден напрямую по фильтру trader_id -- для восстановления реальных входов лидера нужна либо "
            "агрегация через signer-линковку, либо другой подход к цене/маршруту (что уже и делает основной "
            "конвейер через RPC)."
        )
    elif n_found <= 5:
        result["FINAL_VERDICT"] = (
            f"НЕГОДЕН, ОКОНЧАТЕЛЬНО. {found_table} по {sig_col} нашёл только {n_found}/{len(sigs)} наших "
            "транзакций -- дело не в атрибуции, транзакций физически нет и в этой таблице тоже."
        )
    else:
        result["FINAL_VERDICT"] = (
            f"ЧАСТИЧНО: {n_found}/{len(sigs)} найдено в {found_table}, из них подписант=лидер у "
            f"{n_signer_leader} ({result['signer_matches_leader_rate_of_found']:.1%} от найденных). "
            "Не дотягивает до уверенного 'атрибуция подтверждена' порога -- см. сырые данные."
        )
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[signer_link] {result['FINAL_VERDICT']}", flush=True)


if __name__ == "__main__":
    main()
