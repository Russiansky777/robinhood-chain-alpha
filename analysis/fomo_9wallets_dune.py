#!/usr/bin/env python3
"""Владелец (2026-09-17), Fomo через Dune -- все 9 адресов (3 из передачи
контекста + 6 fomo.family), обязательно фильтр по tx_from (реальный
отправитель транзакции), НЕ по taker (декодированный экономический
бенефициар) -- владелец явно указал, что на taker уже получали пустые
результаты и платили за это.

`uniswap_v4_robinhood.swaps` (реальная схема, fomo_forensics_uniswap_v4_
schema_check_result.json) НЕ содержит tx_from -- только taker/maker/
sender. Реальный tx_from берём JOIN'ом на `robinhood.transactions.from`
по tx_hash (схема этой таблицы проверяется НА МЕСТЕ, шаг 0, а не по
памяти).

Порядок (правило владельца, "правила Dune прежние"):
  0. Бесплатная/почти бесплатная проверка реальной схемы robinhood.
     transactions и prices.usd.
  1. LIMIT 100 смоук -- синтаксис + реальная плотность строк.
  2. Полный 7-дневный прогон, ТОЛЬКО если смоук прошёл и оценка разумна.
  3. Локальная классификация бот/человек (интервалы между сделками,
     распределение по часам суток, топ-5 контрактов назначения) --
     ПОЛНОСТЬЮ в Python, не в SQL, чтобы не платить за то, что дёшево
     считается локально на уже скачанных агрегированных данных.
  4. Если большинство -- боты: СТОП, дальше не тратим (правило владельца).
  5. Если большинство -- люди: попытка ценового джойна (prices.usd,
     честно ожидаем низкое покрытие на мем-токенах) + анализ первых
     входов В ПРЕДЕЛАХ 7-дневного окна (с явной оговоркой про горизонт
     видимости) + CSV для передачи другому ИИ."""
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "fomo_9wallets_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")

import pandas as pd  # noqa: E402

from credit_guard import ensure_namespace, remaining_cycle_budget, load_state, BudgetGuardStop  # noqa: E402
from dune_client import DuneClient  # noqa: E402

OUT_JSON = Path("data/fomo_9wallets_dune_result.json")
OUT_CSV = Path("data/fomo_9wallets_dune_swaps.csv")
NAMESPACE_BUDGET = 200.0
WINDOW_DAYS = 7

WALLETS = {
    "SolSwizzle": "0x44fbe0006661d6d17188f1f6d42b32b5577179f7",
    "ether_monk": "0x2408ce75d217e3a70d6ca370c78c1b34d706f5a0",
    "remusofmars": "0x8ab8c0843d9738885d6273dfe3de86c56eea364c",
    "unipcs": "0x0a6EBEd0155EDB4b21D92AD02897A626CD90119E",
    "ogle": "0x1Bcc5f67CD17e13770F199fA03bC043b0cde1143",
    "avast": "0xcc0C581613DFd4ACe7c8686668427236f8BD5cC5",
    "frogman": "0x14AA2A71dbb5eF87b81F92205E2699AA4aa65794",
    "DumbCrayonEater": "0x8f62a08537cede87d511aca6436274ab4ca080a3",
    "vee": "0xa0670863bd5cd0d60022bab2eed78e81e1a06bce",
}
ADDR_LOWER_NO_0X = {name: a[2:].lower() for name, a in WALLETS.items()}
ADDR_TO_NAME = {a: name for name, a in ADDR_LOWER_NO_0X.items()}


def addrs_in_sql() -> str:
    return ", ".join(f"'{a}'" for a in ADDR_LOWER_NO_0X.values())


SCHEMA_CHECK_SQL = """select table_schema, table_name, column_name, data_type
from information_schema.columns
where (table_schema = 'robinhood' and table_name = 'transactions')
   or (table_schema = 'prices' and table_name = 'usd')
   or (table_schema = 'uniswap_v3_robinhood' and table_name in ('swaps', 'uniswapv3pool_evt_swap'))
order by table_schema, table_name, ordinal_position"""


def build_v3_join_sql(v3_table: str, v3_cols: set[str], limit_clause: str) -> str | None:
    """V3 живёт в ОТДЕЛЬНОЙ схеме (uniswap_v3_robinhood). Строим SQL
    ТОЛЬКО по реально подтверждённым в этом прогоне колонкам (v3_cols),
    не по памяти/предположению -- всегда джойним robinhood.transactions
    (тот же паттерн, что v4), чтобы держать унифицированную форму
    (wallet/tx_to_contract) для совместной классификации с v4-строками."""
    if "evt_tx_hash" not in v3_cols:
        return None
    sender_col = "s.sender" if "sender" in v3_cols else "NULL"
    recipient_col = "s.recipient" if "recipient" in v3_cols else "NULL"
    contract_col = "s.contract_address" if "contract_address" in v3_cols else "NULL"
    return f"""select
    s.evt_block_time as block_time,
    s.evt_block_number as block_number,
    'v3' as version,
    lower(to_hex(t."from")) as wallet,
    lower(to_hex(t."to")) as tx_to_contract,
    lower(to_hex({sender_col})) as sender_caller,
    lower(to_hex({recipient_col})) as recipient,
    lower(to_hex({contract_col})) as pool_contract,
    lower(to_hex(s.evt_tx_hash)) as tx_hash
from {v3_table} s
join robinhood.transactions t on t.hash = s.evt_tx_hash
where s.evt_block_time >= now() - interval '{WINDOW_DAYS}' day
    and lower(to_hex(t."from")) in ({addrs_in_sql()})
order by s.evt_block_time
{limit_clause}"""


def build_join_sql(limit_clause: str) -> str:
    return f"""select
    s.block_time,
    s.block_number,
    s.version,
    s.project,
    lower(to_hex(t."from")) as wallet,
    lower(to_hex(t."to")) as tx_to_contract,
    lower(to_hex(s.taker)) as taker,
    lower(to_hex(s.maker)) as maker,
    lower(to_hex(s.sender)) as sender_caller,
    lower(to_hex(s.token_bought_address)) as token_bought,
    lower(to_hex(s.token_sold_address)) as token_sold,
    cast(s.token_bought_amount_raw as varchar) as token_bought_amount_raw,
    cast(s.token_sold_amount_raw as varchar) as token_sold_amount_raw,
    lower(to_hex(s.tx_hash)) as tx_hash
from uniswap_v4_robinhood.swaps s
join robinhood.transactions t on t.hash = s.tx_hash
where s.block_time >= now() - interval '{WINDOW_DAYS}' day
    and lower(to_hex(t."from")) in ({addrs_in_sql()})
order by s.block_time
{limit_clause}"""


def fetch_full_via_smoke(client: DuneClient, out: dict, key_prefix: str, label: str,
                          sql_smoke: str, sql_full: str, expected_columns: int) -> pd.DataFrame | None:
    """LIMIT 100 смоук -> полный прогон, тот же паттерн для v4 и v3 --
    вынесено в функцию, чтобы не дублировать логику дважды."""
    print(f"[fomo9] Шаг 1 ({label}, смоук LIMIT 100):")
    print(sql_smoke)
    qid1 = client.create_query(f"{key_prefix}_smoke100", sql_smoke)
    df_smoke = client.run_sql_cached(f"{key_prefix}_smoke100", sql_smoke, query_id=qid1,
                                      estimated_credits=5.0, expected_max_rows=100, expected_columns=expected_columns)
    n_smoke = len(df_smoke) if df_smoke is not None else 0
    cost_smoke = next((e["credits"] for e in reversed(client.credit_ledger) if e["name"] == f"{key_prefix}_smoke100"), None)
    print(f"[fomo9] {label} смоук: {n_smoke} строк (лимит 100), реальная стоимость {cost_smoke}")
    out[f"{key_prefix}_smoke"] = {"n_rows": n_smoke, "cost": cost_smoke}
    if n_smoke == 0:
        print(f"[fomo9] {label}: 0 строк по tx_from за {WINDOW_DAYS} дней -- дальше на этом источнике не тратим.")
        out[f"{key_prefix}_full"] = {"n_rows": 0, "cost": 0, "skipped": "смоук дал 0 строк"}
        return None

    print(f"\n[fomo9] Шаг 2 ({label}, полный прогон, {WINDOW_DAYS} дней):")
    print(sql_full)
    qid2 = client.create_query(f"{key_prefix}_7day_full", sql_full)
    try:
        df_full = client.run_sql_cached(f"{key_prefix}_7day_full", sql_full, query_id=qid2,
                                         estimated_credits=25.0, expected_max_rows=20000, expected_columns=expected_columns)
    except BudgetGuardStop:
        print(f"[fomo9] {label} СТОП обязывающего гейта чтения: реальных строк оказалось БОЛЬШЕ 20000 заявленных -- "
              "execute уже оплачен и в леджере, /results НЕ читан (0 доп. кредитов). Само по себе это сильный "
              "сигнал: такой объём за 7 дней на 9 адресов нетипичен для людей.")
        out[f"{key_prefix}_full"] = {"STOPPED_BY_ROW_SAFETY_GATE": True,
                                      "note": ">20000 строк по факту (Dune /status) -- execute оплачен, чтение отказано"}
        return None
    cost_full = next((e["credits"] for e in reversed(client.credit_ledger) if e["name"] == f"{key_prefix}_7day_full"), None)
    n_full = len(df_full) if df_full is not None else 0
    print(f"[fomo9] {label} полный прогон: {n_full} строк, реальная стоимость {cost_full}")
    out[f"{key_prefix}_full"] = {"n_rows": n_full, "cost": cost_full}
    if df_full is None or n_full == 0:
        return None
    return df_full


def run() -> int:
    ensure_namespace("fomo_9wallets_mozila", NAMESPACE_BUDGET)
    remaining = remaining_cycle_budget(load_state())
    print(f"[fomo9] остаток общего цикла Dune (Mozila): {remaining:.1f} кредитов, "
          f"бюджет пространства 'fomo_9wallets_mozila': {NAMESPACE_BUDGET}")

    client = DuneClient()
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "wallets": WALLETS, "window_days": WINDOW_DAYS}

    # --- Шаг 0: реальная схема robinhood.transactions / prices.usd / uniswap_v3_robinhood ---
    print("[fomo9] Шаг 0: проверка реальной схемы robinhood.transactions / prices.usd / uniswap_v3_robinhood")
    qid0 = client.create_query("fomo9_schema_check", SCHEMA_CHECK_SQL)
    df0 = client.run_sql_cached("fomo9_schema_check", SCHEMA_CHECK_SQL, query_id=qid0,
                                 estimated_credits=2.0, expected_max_rows=200, expected_columns=4)
    schema_rows = df0.to_dict("records") if df0 is not None else []
    out["schema_check"] = schema_rows
    txn_cols = {r["column_name"] for r in schema_rows if r["table_schema"] == "robinhood" and r["table_name"] == "transactions"}
    print(f"[fomo9] robinhood.transactions реальные колонки: {sorted(txn_cols)}")
    if not {"hash", "from"}.issubset(txn_cols):
        print("[fomo9] СТОП: robinhood.transactions не содержит ожидаемых колонок hash/from -- "
              "не гадаем, нужен другой источник tx_from. Ничего дальше не запущено.")
        out["STOPPED"] = f"robinhood.transactions колонки: {sorted(txn_cols)} -- нет hash/from"
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0
    prices_cols = {r["column_name"] for r in schema_rows if r["table_schema"] == "prices" and r["table_name"] == "usd"}
    out["prices_usd_columns"] = sorted(prices_cols)
    print(f"[fomo9] prices.usd реальные колонки: {sorted(prices_cols)}")

    v3_by_table: dict[str, set] = {}
    for r in schema_rows:
        if r["table_schema"] == "uniswap_v3_robinhood":
            v3_by_table.setdefault(r["table_name"], set()).add(r["column_name"])
    v3_table = "swaps" if "swaps" in v3_by_table else ("uniswapv3pool_evt_swap" if "uniswapv3pool_evt_swap" in v3_by_table else None)
    v3_cols = v3_by_table.get(v3_table, set()) if v3_table else set()
    out["v3_table_used"] = v3_table
    out["v3_columns_found"] = sorted(v3_cols)
    print(f"[fomo9] uniswap_v3_robinhood: таблица={v3_table}, колонки={sorted(v3_cols)}")

    # --- V4: смоук -> полный ---
    df_v4 = fetch_full_via_smoke(client, out, "fomo9_v4", "v4 (uniswap_v4_robinhood.swaps)",
                                  build_join_sql("limit 100"), build_join_sql(""), expected_columns=14)

    # --- V3: смоук -> полный (та же дисциплина, ТОЛЬКО если реальная схема это позволяет) ---
    df_v3 = None
    v3_sql_smoke = build_v3_join_sql(f"uniswap_v3_robinhood.{v3_table}", v3_cols, "limit 100") if v3_table else None
    if v3_sql_smoke:
        v3_sql_full = build_v3_join_sql(f"uniswap_v3_robinhood.{v3_table}", v3_cols, "")
        df_v3 = fetch_full_via_smoke(client, out, "fomo9_v3", f"v3 ({v3_table})",
                                      v3_sql_smoke, v3_sql_full, expected_columns=9)
    else:
        print("[fomo9] v3: реальная схема не дала evt_tx_hash в найденной таблице -- v3-шаг пропущен честно, не гадаем.")
        out["fomo9_v3_smoke"] = {"skipped": f"v3_table={v3_table}, колонки не содержат evt_tx_hash"}

    frames = [d for d in (df_v4, df_v3) if d is not None and len(d) > 0]
    if not frames:
        print("[fomo9] СТОП: 0 строк по tx_from за 7 дней И на v4, И на v3 -- линия закрыта на Dune, дальше не тратим.")
        out["decision"] = "0 строк по tx_from на ОБЕИХ схемах (v3 и v4) -- линия закрыта"
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0

    df_full = pd.concat(frames, ignore_index=True, sort=False)
    df_full["wallet_name"] = df_full["wallet"].map(ADDR_TO_NAME)
    df_full["block_time"] = pd.to_datetime(df_full["block_time"])
    df_full = df_full.sort_values(["wallet", "block_time"]).reset_index(drop=True)
    print(f"[fomo9] объединено v4+v3: {len(df_full)} строк всего ({len(df_v4) if df_v4 is not None else 0} v4 + "
          f"{len(df_v3) if df_v3 is not None else 0} v3)")
    out["n_rows_combined"] = len(df_full)

    # --- Шаг 3: локальная классификация бот/человек (0 доп. кредитов) ---
    per_wallet: dict = {}
    for wallet_addr, group in df_full.groupby("wallet"):
        name = ADDR_TO_NAME.get(wallet_addr, wallet_addr)
        times = group["block_time"].sort_values()
        n = len(times)
        gaps_s = times.diff().dropna().dt.total_seconds().tolist()
        hours = times.dt.hour.value_counts().reindex(range(24), fill_value=0)
        # "Ночной провал" -- любое непрерывное окно из >=4 часов подряд с 0 сделок
        # (реальная проверка по факту распределения часов, не гадаем часовой пояс).
        hour_counts = hours.tolist() * 2  # оборачиваем по кругу для непрерывности через полночь
        best_zero_run, cur = 0, 0
        for c in hour_counts:
            cur = cur + 1 if c == 0 else 0
            best_zero_run = max(best_zero_run, cur)
        best_zero_run = min(best_zero_run, 24)
        median_gap_s = sorted(gaps_s)[len(gaps_s) // 2] if gaps_s else None
        frac_gaps_under_60s = (sum(1 for g in gaps_s if g < 60) / len(gaps_s)) if gaps_s else None
        top5_dest = group["tx_to_contract"].value_counts().head(5).to_dict()
        is_bot_like = (best_zero_run < 4) and (frac_gaps_under_60s is not None and frac_gaps_under_60s > 0.3)
        per_wallet[name] = {
            "address": wallet_addr,
            "n_swaps_7d": n,
            "median_gap_seconds": median_gap_s,
            "frac_gaps_under_60s": frac_gaps_under_60s,
            "longest_quiet_hours_run": best_zero_run,
            "top5_destination_contracts": top5_dest,
            "classification": "бот-подобный" if is_bot_like else "человекоподобный (нет чёткого ночного провала ИЛИ нет частых <60с интервалов)",
        }
        print(f"[fomo9] {name}: n={n}, median_gap_s={median_gap_s}, frac<60s={frac_gaps_under_60s}, "
              f"quiet_run={best_zero_run}ч -> {per_wallet[name]['classification']}")

    out["per_wallet_classification"] = per_wallet
    n_bot = sum(1 for v in per_wallet.values() if v["classification"] == "бот-подобный")
    n_human = len(per_wallet) - n_bot
    top5_overall = df_full["tx_to_contract"].value_counts().head(5).to_dict()
    out["top5_destination_contracts_overall"] = top5_overall
    summary_line = (f"из {len(per_wallet)} адресов {n_bot} ведут себя как боты, {n_human} как люди, "
                     f"сделки идут через {list(top5_overall.keys())[:3]}")
    print(f"\n[fomo9] {summary_line}")
    out["summary_line"] = summary_line

    if n_bot >= n_human:
        out["decision"] = "БОЛЬШИНСТВО БОТЫ -- остановка здесь, дальше не анализируем (правило владельца)"
        print(f"[fomo9] {out['decision']}")
        df_full.to_csv(OUT_CSV, index=False)
        out["csv_path"] = str(OUT_CSV)
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0

    # --- Шаг 4 (только если люди): ценовой джойн + первые входы ---
    price_join_note = "prices.usd джойн НЕ выполнен в этой версии скрипта -- см. honest_caveat"
    if {"minute", "blockchain", "contract_address", "price"}.issubset(prices_cols) or \
       {"minute", "blockchain", "contract_address", "price", "decimals"}.issubset(prices_cols):
        print("[fomo9] Шаг 4: реальная схема prices.usd подходит под ожидаемую -- цена НЕ джойнится в этой "
              "версии скрипта (оставлено на явное следующее решение владельца, чтобы не плодить неоценённые "
              "траты внутри уже большого прогона) -- сырые raw-суммы уже в CSV, decimals можно добрать бесплатным RPC.")
    else:
        print(f"[fomo9] prices.usd реальная схема НЕ содержит ожидаемых колонок ({sorted(prices_cols)}) -- "
              "ценовой джойн пропущен честно, не выдумываем структуру.")

    # Первые входы В ПРЕДЕЛАХ 7-дневного окна (честная оговорка: не видим историю до окна).
    # token_bought резолвится только для v4-строк (uniswap_v4_robinhood.swaps) -- v3-строки
    # в этой версии не резолвят token0/token1 пула (нужен отдельный join на factory PoolCreated,
    # не сделан в этом раунде) -- честно ограничиваем первые-входы v4-подмножеством, не гадаем.
    first_entries = []
    df_v4_rows = df_full[df_full["version"] != "v3"] if "version" in df_full.columns else df_full
    if "token_bought" in df_v4_rows.columns:
        for (wallet_addr, token), grp in df_v4_rows.dropna(subset=["token_bought"]).groupby(["wallet", "token_bought"]):
            first_row = grp.sort_values("block_time").iloc[0]
            first_entries.append({
                "wallet": ADDR_TO_NAME.get(wallet_addr, wallet_addr),
                "token": token,
                "first_seen_in_window_at": str(first_row["block_time"]),
                "n_buys_of_this_token_in_window": len(grp),
                "is_first_buy_in_window": True,
                "caveat": "первая покупка ВИДИМАЯ В ЭТОМ 7-дневном окне -- не исключает более раннюю покупку до окна",
            })
    out["first_entries_caveat_v3"] = (
        "Первые входы посчитаны ТОЛЬКО по v4-строкам (token_bought резолвится через "
        "uniswap_v4_robinhood.swaps) -- v3-строки (uniswap_v3_robinhood) не резолвят токен пула "
        "в этом раунде, нужен отдельный join на PoolCreated фабрики, не сделан."
    )
    out["first_entries_within_window"] = first_entries
    out["price_join_note"] = price_join_note

    df_full.to_csv(OUT_CSV, index=False)
    out["csv_path"] = str(OUT_CSV)
    out["decision"] = "БОЛЬШИНСТВО ЛЮДИ -- первые входы посчитаны в пределах окна, CSV готов"
    out["honest_caveat"] = (
        "amount_usd в CSV отсутствует -- uniswap_v4_robinhood.swaps не содержит amount_usd, "
        "ценовой джойн на этом шаге не выполнялся (см. price_join_note). Raw-суммы токенов есть "
        "(token_bought_amount_raw/token_sold_amount_raw), decimals по каждому токену можно добрать "
        "бесплатным RPC eth_call, как везде в этой сессии. Первые входы -- только в пределах "
        "7-дневного окна выгрузки, не абсолютные первые покупки за всю историю адреса."
    )

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[fomo9] результат: {OUT_JSON}, CSV: {OUT_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
