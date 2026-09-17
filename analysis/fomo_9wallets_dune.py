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
order by table_schema, table_name, ordinal_position"""


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


def run() -> int:
    ensure_namespace("fomo_9wallets_mozila", NAMESPACE_BUDGET)
    remaining = remaining_cycle_budget(load_state())
    print(f"[fomo9] остаток общего цикла Dune (Mozila): {remaining:.1f} кредитов, "
          f"бюджет пространства 'fomo_9wallets_mozila': {NAMESPACE_BUDGET}")

    client = DuneClient()
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "wallets": WALLETS, "window_days": WINDOW_DAYS}

    # --- Шаг 0: реальная схема robinhood.transactions / prices.usd ---
    print("[fomo9] Шаг 0: проверка реальной схемы robinhood.transactions / prices.usd")
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

    # --- Шаг 1: LIMIT 100 смоук ---
    sql_smoke = build_join_sql("limit 100")
    print("[fomo9] Шаг 1 (смоук, LIMIT 100):")
    print(sql_smoke)
    qid1 = client.create_query("fomo9_swaps_smoke100", sql_smoke)
    df_smoke = client.run_sql_cached("fomo9_swaps_smoke100", sql_smoke, query_id=qid1,
                                      estimated_credits=5.0, expected_max_rows=100, expected_columns=13)
    n_smoke = len(df_smoke) if df_smoke is not None else 0
    cost_smoke = next((e["credits"] for e in reversed(client.credit_ledger) if e["name"] == "fomo9_swaps_smoke100"), None)
    print(f"[fomo9] смоук: {n_smoke} строк (лимит 100), реальная стоимость {cost_smoke}")
    out["step1_smoke"] = {"n_rows": n_smoke, "cost": cost_smoke, "sample_first_5": (df_smoke.head(5).to_dict("records") if df_smoke is not None else [])}

    if n_smoke == 0:
        print("[fomo9] СТОП: 0 строк по tx_from за 7 дней на v4 -- как и предупреждал владелец про taker, "
              "но теперь честно проверено по tx_from. Дальше не тратим на этот путь без нового решения.")
        out["decision"] = "0 строк по tx_from -- линия закрыта на этом источнике (v4), дальше не тратим"
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0

    # --- Шаг 2: полный 7-дневный прогон (без LIMIT) ---
    sql_full = build_join_sql("")
    print("\n[fomo9] Шаг 2 (полный прогон, 7 дней):")
    print(sql_full)
    # Оценка: смоук упёрся в LIMIT 100, значит реальных строк может быть
    # заметно больше -- берём консервативную оценку с запасом, но в
    # пределах санитарного потолка гарда (40 по умолчанию для этого
    # пространства, не переопределяем).
    qid2 = client.create_query("fomo9_swaps_7day_full", sql_full)
    try:
        df_full = client.run_sql_cached("fomo9_swaps_7day_full", sql_full, query_id=qid2,
                                         estimated_credits=25.0, expected_max_rows=20000, expected_columns=13)
    except BudgetGuardStop:
        # Реальный execute уже оплачен и записан в леджер ДО этого отказа
        # (execute() платится независимо от того, разрешат ли потом читать
        # результат) -- сам факт "строк оказалось больше 20000 заявленных"
        # ЭТО УЖЕ содержательный ответ ("N адресов торгуют настолько часто,
        # что 7 дней дали >20000 строк" -- само по себе сильный сигнал
        # бот-подобного поведения), не просто сбой скрипта.
        print("[fomo9] СТОП обязывающего гейта чтения: реальных строк оказалось БОЛЬШЕ 20000 заявленных -- "
              "execute уже оплачен и в леджере, /results НЕ читан (0 доп. кредитов). Само по себе это сильный "
              "сигнал: такой объём за 7 дней на 9 адресов нетипичен для людей.")
        out["step2_full"] = {"STOPPED_BY_ROW_SAFETY_GATE": True,
                              "note": ">20000 строк по факту (Dune /status) -- execute оплачен, чтение отказано, "
                                      "сам объём уже сильный признак бот-активности"}
        out["decision"] = "объём >20000 строк за 7 дней -- вероятно боты, нужен новый лимит/агрегация, дальше не читаем без решения"
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0
    cost_full = next((e["credits"] for e in reversed(client.credit_ledger) if e["name"] == "fomo9_swaps_7day_full"), None)
    n_full = len(df_full) if df_full is not None else 0
    print(f"[fomo9] полный прогон: {n_full} строк, реальная стоимость {cost_full}")
    out["step2_full"] = {"n_rows": n_full, "cost": cost_full}

    if df_full is None or n_full == 0:
        out["decision"] = "0 строк на полном 7-дневном окне -- линия закрыта"
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0

    df_full["wallet_name"] = df_full["wallet"].map(ADDR_TO_NAME)
    df_full["block_time"] = pd.to_datetime(df_full["block_time"])
    df_full = df_full.sort_values(["wallet", "block_time"]).reset_index(drop=True)

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
    first_entries = []
    for (wallet_addr, token), grp in df_full.groupby(["wallet", "token_bought"]):
        first_row = grp.sort_values("block_time").iloc[0]
        first_entries.append({
            "wallet": ADDR_TO_NAME.get(wallet_addr, wallet_addr),
            "token": token,
            "first_seen_in_window_at": str(first_row["block_time"]),
            "n_buys_of_this_token_in_window": len(grp),
            "is_first_buy_in_window": True,
            "caveat": "первая покупка ВИДИМАЯ В ЭТОМ 7-дневном окне -- не исключает более раннюю покупку до окна",
        })
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
