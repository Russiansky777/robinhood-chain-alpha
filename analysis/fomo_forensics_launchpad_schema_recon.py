#!/usr/bin/env python3
"""Форензика fomo, задание владельца 2026-09-06 п.2: проверить
`poolsfun_robinhood` и другие схемы лаунчпада -- есть ли там свопы по
токенам с `TokenLaunched`, реальный объём таблицы за 30 дней. Если
есть -- это ОСНОВНОЙ источник для п.2/п.3, не `dex.trades` (после
инцидента 238.71 -- см. docs/PROJECT_STATE.md).

Кандидаты (найдены в уже оплаченном раньше результате
`dune_mozila_schema_recon_result.json`, поиск distinct table_schema
по '%robinhood%' -- НЕ гадаем заново, переиспользуем реальные данные):
`poolsfun_robinhood`, `stoxesfun_robinhood` -- оба по конвенции имени
похожи на permissionless-лаунч-платформы в стиле pump.fun ("X.fun").

Метод -- ТОЛЬКО information_schema + LIMIT 100 сэмплы + один бounded
агрегат на подтверждённой таблице -- НИКАКИХ джойнов с dex.trades,
ничего, что попадало бы под новое правило credit_guard
(dex.trades+blockchain='robinhood' форсирует 250 и требует отдельного
'да' -- здесь оно не применяется, схема другая).

Структурная проверка (не совпадение по названию): реальные адреса
токенов fomo.family, уже подтверждённые прямым eth_call как ERC-20
(BUDDY/PICKAZO/AH42/NECKED/LOOP, см.
fomo_forensics_topic0_verify_erc20_result.json) -- ищем эти адреса
в найденной таблице свопов. Если совпадают -- схема реально связана с
теми же токенами, не просто похожа по имени."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "fomo_forensics_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")

from credit_guard import ensure_namespace, remaining_cycle_budget, load_state  # noqa: E402
from dune_client import DuneClient  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/fomo_forensics_launchpad_schema_recon_result.json")
NAMESPACE_BUDGET = 350.0  # владелец, 2026-09-06: поднято с 250 -- но только на чтение из
                          # материализованного результата или схемы лаунчпада, не новые dex.trades-сканы

CANDIDATE_SCHEMAS = ["poolsfun_robinhood", "stoxesfun_robinhood"]
# Реальные адреса токенов fomo.family, уже подтверждённые прямым eth_call
# (fomo_forensics_topic0_verify_erc20_result.json) -- для структурной сверки,
# не совпадения по названию схемы.
KNOWN_FOMO_TOKEN_ADDRESSES_NO_0X = [
    "01600e4fb15591452f13c64fb849e70c4d4ac899",  # BUDDY
    "b3629f80e40500121f2353174ab867b2710a6d7f",  # PICKAZO
    "4c2f292a9a63cb083337688811bb029409569d05",  # AH42 / AfterHours
    "799274718e453b74d6dc2b76ab6f82b7dbd0f43f",  # NECKED
    "6f7d7e90f3925fec0db3e08644bcd30c99c1c3af",  # LOOP
]

SWAP_LIKE_KEYWORDS = ("swap", "trade", "buy", "sell", "purchase", "launch", "graduat", "curve", "pool")


def list_tables(client: DuneClient, schema: str) -> list[dict]:
    sql = f"""select table_name
from information_schema.tables
where table_schema = '{schema}'
order by table_name
limit 100"""
    qid = client.create_query(f"fomo_launchpad_tables_{schema}", sql)
    df = client.run_sql_cached(f"fomo_launchpad_tables_{schema}", sql, query_id=qid,
                                estimated_credits=2.0, expected_max_rows=100, expected_columns=1)
    return df.to_dict("records") if df is not None else []


def list_columns(client: DuneClient, schema: str, table: str) -> list[dict]:
    sql = f"""select column_name, data_type
from information_schema.columns
where table_schema = '{schema}' and table_name = '{table}'
order by ordinal_position
limit 100"""
    qid = client.create_query(f"fomo_launchpad_cols_{schema}_{table}"[:100], sql)
    df = client.run_sql_cached(f"fomo_launchpad_cols_{schema}_{table}"[:100], sql, query_id=qid,
                                estimated_credits=2.0, expected_max_rows=100, expected_columns=2)
    return df.to_dict("records") if df is not None else []


def peek_addr_format(client: DuneClient, schema: str, table: str, addr_col: str) -> str | None:
    """Реальный формат колонки-кандидата на адрес (varbinary vs уже
    varchar-строка) -- НЕ предполагаем по имени/data_type из
    information_schema (там может быть просто 'varchar', даже если
    хранится вариант с обрезкой/регистром) -- смотрим на реальное сырое
    значение через дешёвый LIMIT 5 перед тем, как строить платный запрос
    сравнения. Возвращает одно из: 'hex_no_prefix', 'hex_0x_prefix', None
    (нераспознано -- не гадаем, пропускаем сверку)."""
    sql = f"select {addr_col} as v from {schema}.{table} where {addr_col} is not null limit 5"
    qid = client.create_query(f"fomo_launchpad_peek_{schema}_{table}"[:100], sql)
    df = client.run_sql_cached(f"fomo_launchpad_peek_{schema}_{table}"[:100], sql, query_id=qid,
                                estimated_credits=2.0, expected_max_rows=5, expected_columns=1)
    if df is None or df.empty:
        return None
    sample_val = str(df["v"].iloc[0])
    print(f"[launchpad_recon]   реальный сырой вид {addr_col}: {sample_val!r}")
    if sample_val.lower().startswith("0x"):
        return "hex_0x_prefix"
    if len(sample_val.replace("0x", "")) == 40 and all(c in "0123456789abcdefABCDEF" for c in sample_val):
        return "hex_no_prefix"
    return None


def check_known_addresses_present(client: DuneClient, schema: str, table: str, addr_col: str, addr_format: str) -> dict:
    if addr_format == "hex_0x_prefix":
        addrs_sql = ", ".join(f"'0x{a}'" for a in KNOWN_FOMO_TOKEN_ADDRESSES_NO_0X)
        expr = f"lower({addr_col})"
    else:  # hex_no_prefix
        addrs_sql = ", ".join(f"'{a}'" for a in KNOWN_FOMO_TOKEN_ADDRESSES_NO_0X)
        expr = f"lower({addr_col})"
    sql = f"""select {expr} as addr, count(*) as n
from {schema}.{table}
where {expr} in ({addrs_sql})
group by {expr}"""
    qid = client.create_query(f"fomo_launchpad_addrcheck_{schema}_{table}"[:100], sql)
    df = client.run_sql_cached(f"fomo_launchpad_addrcheck_{schema}_{table}"[:100], sql, query_id=qid,
                                estimated_credits=8.0, expected_max_rows=10, expected_columns=2)
    rows = df.to_dict("records") if df is not None else []
    return {"n_known_addresses_matched": len(rows), "rows": rows}


def volume_30d(client: DuneClient, schema: str, table: str, time_col: str) -> dict:
    sql = f"""select count(*) as n_rows_30d, min({time_col}) as first_seen, max({time_col}) as last_seen
from {schema}.{table}
where {time_col} >= now() - interval '30' day
limit 1"""
    qid = client.create_query(f"fomo_launchpad_vol30d_{schema}_{table}"[:100], sql)
    df = client.run_sql_cached(f"fomo_launchpad_vol30d_{schema}_{table}"[:100], sql, query_id=qid,
                                estimated_credits=10.0, expected_max_rows=1, expected_columns=3)
    rows = df.to_dict("records") if df is not None else []
    return rows[0] if rows else {}


def run() -> int:
    ensure_namespace("fomo_forensics_mozila", NAMESPACE_BUDGET)
    remaining = remaining_cycle_budget(load_state())
    print(f"[launchpad_recon] остаток общего цикла Dune (Mozila): {remaining:.1f} кредитов")

    client = DuneClient()
    out = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "schemas": {}}

    for schema in CANDIDATE_SCHEMAS:
        print(f"\n[launchpad_recon] === схема {schema} ===")
        tables = list_tables(client, schema)
        table_names = [t["table_name"] for t in tables]
        print(f"[launchpad_recon] {schema}: {len(table_names)} таблиц: {table_names}")
        schema_out: dict = {"n_tables": len(table_names), "tables": table_names, "candidate_tables": {}}

        candidates = [t for t in table_names if any(kw in t.lower() for kw in SWAP_LIKE_KEYWORDS)]
        print(f"[launchpad_recon] {schema}: таблицы-кандидаты (по имени): {candidates}")
        schema_out["candidate_table_names"] = candidates

        for table in candidates[:5]:  # не более 5 таблиц на схему -- контроль бюджета
            print(f"\n[launchpad_recon] --- {schema}.{table} ---")
            cols = list_columns(client, schema, table)
            col_names = [c["column_name"] for c in cols]
            print(f"[launchpad_recon] реальные колонки: {col_names}")

            addr_col = next((c for c in col_names if "token" in c.lower() and "address" in c.lower()), None)
            if not addr_col:
                addr_col = next((c for c in col_names if c.lower() in ("contract_address", "token", "address")), None)
            time_col = next((c for c in col_names if "block_time" in c.lower() or c.lower() == "evt_block_time"), None)

            table_out: dict = {"columns": cols, "addr_col_guess": addr_col, "time_col_guess": time_col}

            if addr_col:
                addr_format = peek_addr_format(client, schema, table, addr_col)
                table_out["addr_format_detected"] = addr_format
                if addr_format:
                    match = check_known_addresses_present(client, schema, table, addr_col, addr_format)
                    table_out["known_address_match"] = match
                    print(f"[launchpad_recon] совпадение с известными адресами fomo.family: {match}")
                else:
                    print(f"[launchpad_recon] реальный формат {addr_col} не распознан (не hex-строка) -- "
                          "не гадаем сравнение, пропускаем сверку адресов")
            else:
                print(f"[launchpad_recon] не нашли колонку-кандидата на адрес токена среди {col_names} -- пропускаем сверку адресов")

            if time_col and table_out.get("known_address_match", {}).get("n_known_addresses_matched", 0) > 0:
                vol = volume_30d(client, schema, table, time_col)
                table_out["volume_30d"] = vol
                print(f"[launchpad_recon] реальный объём за 30 дней: {vol}")
            elif time_col:
                print(f"[launchpad_recon] адреса НЕ совпали -- пропускаем платный агрегат объёма за 30д для {schema}.{table} "
                      "(нет структурного подтверждения связи с fomo.family)")

            schema_out["candidate_tables"][table] = table_out

        out["schemas"][schema] = schema_out

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[launchpad_recon] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
