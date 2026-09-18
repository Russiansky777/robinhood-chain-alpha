#!/usr/bin/env python3
"""Владелец, 2026-09-18: проверка гипотезы -- заменить листание истории
пулов по цепи (страницы по 1000 подписей, часы на разбросанные во
времени покупки) на готовую таблицу сделок Solana DEX на Dune.

ВАЖНО (урок Fomo, задание владельца): на Fomo 4 запроса подряд дали 0
строк, потому что сделки атрибутировались на адрес исполнителя
(агрегатор/роутер), а не на пул. Поэтому сначала проверка схемы и
СВЕРКА цен на уже посчитанных по цепи точках, потом решение о переводе
расчёта -- никакого слепого доверия структуре таблицы.

Бюджет: не больше 3 платных запросов (namespace "solana_dune_probe",
20 кредитов -- см. data/credits_spent.json), все с обязывающим
expected_max_rows (см. credit_guard.check_before_read_binding).

Шаг 1: information_schema.tables -- какие схемы/таблицы вообще есть
       для Solana (без угадывания имён -- широкий фильтр по 'solana').
Шаг 2: information_schema.columns -- реальная схема таблиц-кандидатов
       с 'trade' в имени (несколько сразу, одним запросом).
Шаг 3: ОДИН пул (SOL_USDC_POOL, подтверждён на цепи), ОДИН промежуток
       (5 минут одной покупки, для которой у нас есть 6 РЕАЛЬНЫХ
       ончейн-цен на +5/+15/+30/+60/+180/+300с -- из
       data/solana_buyer_200/step_extended_result.json,
       legs_onchain_full, сохранённых ДО миграции этой ноги на Gecko),
       LIMIT 100, сверка каждой из 6 точек с ближайшей сделкой Dune
       на момент t (тот же метод "последняя сделка на/до t", что и у
       нас на цепи).

Результат: data/solana_dune_probe_result.json + честный вывод в лог:
какие таблицы есть, сходятся ли цены, во что обошлась проверка,
и рекомендация (переводить/не переводить/чинить фильтр)."""
from __future__ import annotations

import json
import os
import sys
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "solana_dune_probe")

from dune_client import DuneClient  # noqa: E402

OUT_PATH = Path("data/solana_dune_probe_result.json")

SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
POOL = "3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv"  # подтверждён на цепи: m0=SOL, m1=USDC, CLMM

# РЕАЛЬНЫЕ ончейн-точки одной покупки (сигнатура
# 5y38rMoQsL9fS921RhcAhGiPBEGA19iYfrzGYpKjfusb86hFh9Uazd8ypgkcf4KEoqkekWrTcRVJ9oeFLPZ2m3Se),
# взяты из data/solana_buyer_200/step_extended_result.json (legs_onchain_full,
# сохранены ДО миграции ноги SOL/USDC на GeckoTerminal) -- НЕ выдуманы,
# извлечены скриптом-разведкой из уже закоммиченных данных этой же сессии.
REFERENCE_POINTS = [
    {"seconds": 5, "target": 1789668875, "onchain_slot": 447860733,
     "onchain_signature": "3xBiP1PYCwkErrrfrJY9J5nyfd1cBja6SRc8D5VphxcJsircEqWs6XQV4QurB87jecGAy9GAMWJSdMyN9n9CQovE",
     "onchain_price_usdc_per_sol": "101.3976831153112189589490789"},
    {"seconds": 15, "target": 1789668885, "onchain_slot": 447860758,
     "onchain_signature": "ve7N6sh3ZidXGYBKM2ZS68Vj9ubL3QrZwDoPrLyxWjDDwDeuJoTUgzeuuQYf8vASBrMMHXcUz5gW712WV9twxAg",
     "onchain_price_usdc_per_sol": "101.3993054554198938815758506"},
    {"seconds": 30, "target": 1789668900, "onchain_slot": 447860814,
     "onchain_signature": "3Y63Jp5bAJ1bywkAbn7Pstp1nnzng2WfGMd5Y5VD2dNb3WjxWV63iTgw6ZUX4EWQVDXnh6BFwZp9c8F6fFf6Xbs",
     "onchain_price_usdc_per_sol": "101.4139926700232636534305368"},
    {"seconds": 60, "target": 1789668930, "onchain_slot": 447860909,
     "onchain_signature": "2UubKQsnkRx9vGyUHPGeEXLM5PfgWxErPKixpQJCvkLWXs1oUxV4HtzGTMfeAs3vn9HaDhomwTuz8798BYZuScmu",
     "onchain_price_usdc_per_sol": "101.4847024804766955467140586"},
    {"seconds": 180, "target": 1789669050, "onchain_slot": 447861277,
     "onchain_signature": "44UUgBok7wtQBbrcAaVkkfrEE1txC61NDjUxxmWLF1aQcbZ9em2MCXoF5fGT611N9q5ZXYattcYZfeqF6s7w3bX1",
     "onchain_price_usdc_per_sol": "101.4893498688444455032105755"},
    {"seconds": 300, "target": 1789669170, "onchain_slot": 447861665,
     "onchain_signature": "5WdQWiUYdhwXycHF3cK8TWRz8XEqSnZ4JKxWSviMvTd5121mFXUGvds8wk3anwTnwqp7SdL9ZqhEqcdZSryjKCF",
     "onchain_price_usdc_per_sol": "101.4683043976886629505338269"},
]

# Полное задание по расширенному прогону: ~250 покупок ещё не посчитаны,
# нужны только КОРОТКИЕ горизонты (до +300с) -- т.е. по одному 5-минутному
# окну на покупку (окна одного минта внутри 600с сливаются, см.
# solana_buyer200_fast_price.py MERGE_GAP_TOLERANCE_S), плюс отдельно
# долистанные окна для НЕ универсальных промежуточных пулов (свой пул
# минта, напр. zxTpi4BtaWX3). Используется только для оценки цены
# полного перевода в Шаге 5 -- не для решения о структуре таблицы.
N_REMAINING_PURCHASES = 250


def run_probe(client: DuneClient) -> dict:
    """Шаги 1-3 разведки на уже готовом DuneClient (ключ/леджер/namespace
    выбраны вызывающим кодом -- см. solana_dune_key_check.py, который
    сначала проверяет ПРАВА ключа дешёвым запросом, и только на рабочем
    ключе вызывает эту функцию). Возвращает `out` БЕЗ записи на диск --
    запись делает вызывающий код (см. _finish)."""
    out: dict = {"generated_at_utc": None, "pool": POOL, "reference_points": REFERENCE_POINTS}

    # ---------- Запрос 1: какие solana-схемы/таблицы вообще есть ----------
    sql1 = """
    SELECT table_schema, table_name
    FROM information_schema.tables
    WHERE LOWER(table_schema) LIKE '%solana%'
    ORDER BY table_schema, table_name
    """.strip()
    df1 = client.run_sql_cached(
        name="solana_dune_probe_1_tables", sql=sql1, estimated_credits=1.0,
        expected_max_rows=20000, expected_columns=2,
    )
    if df1 is None:
        out["HONEST_ANSWER"] = "Запрос 1 не вернул DataFrame (materialize-only?) -- логическая ошибка вызова, чинить код."
        return out
    out["step1_n_tables_total"] = len(df1)
    out["step1_all_tables"] = df1.to_dict("records")
    print(f"[dune_probe] Шаг 1: {len(df1)} таблиц в *solana*-схемах.")

    trade_like = df1[df1["table_name"].str.contains("trade", case=False, na=False)]
    out["step1_trade_candidate_tables"] = trade_like.to_dict("records")
    print(f"[dune_probe] Шаг 1: {len(trade_like)} таблиц с 'trade' в имени: "
          f"{[f'{r.table_schema}.{r.table_name}' for r in trade_like.itertuples()]}")

    if trade_like.empty:
        out["HONEST_ANSWER"] = (
            "Ни одной таблицы с 'trade' в имени не найдено ни в одной *solana*-схеме -- "
            "см. step1_all_tables для полного списка (может быть другое именование, "
            "например 'swaps' или 'dex_trades'). Дальше не иду -- ручной разбор."
        )
        print("[dune_probe] " + out["HONEST_ANSWER"])
        return out

    # Приоритет: dex_solana.trades (типовое имя единой Dune-таблицы по
    # аналогии с dex.trades для EVM), иначе первый найденный кандидат.
    preferred = trade_like[(trade_like["table_schema"] == "dex_solana") & (trade_like["table_name"] == "trades")]
    candidates = trade_like if preferred.empty else preferred
    # Ограничиваем до 5 кандидатов, чтобы запрос 2 (колонки) не разросся зря.
    candidate_pairs = list(dict.fromkeys(
        (r.table_schema, r.table_name) for r in candidates.itertuples()
    ))[:5]
    out["step1_candidate_pairs_for_columns"] = candidate_pairs

    # ---------- Запрос 2: реальные колонки таблиц-кандидатов ----------
    where_pairs = " OR ".join(
        f"(table_schema = '{s}' AND table_name = '{t}')" for s, t in candidate_pairs
    )
    sql2 = f"""
    SELECT table_schema, table_name, column_name, data_type, ordinal_position
    FROM information_schema.columns
    WHERE {where_pairs}
    ORDER BY table_schema, table_name, ordinal_position
    """.strip()
    df2 = client.run_sql_cached(
        name="solana_dune_probe_2_columns", sql=sql2, estimated_credits=1.0,
        expected_max_rows=2000, expected_columns=5,
    )
    out["step2_columns_by_table"] = df2.to_dict("records")
    print(f"[dune_probe] Шаг 2: {len(df2)} строк колонок по {len(candidate_pairs)} таблицам-кандидатам.")

    # Выбираем таблицу с наибольшим "покрытием" нужных полей.
    def cols_for(schema: str, table: str) -> set[str]:
        sub = df2[(df2["table_schema"] == schema) & (df2["table_name"] == table)]
        return set(sub["column_name"].tolist())

    TIME_CANDS = ["block_time", "evt_block_time", "time"]
    TX_CANDS = ["tx_id", "tx_hash", "evt_tx_hash", "tx_signature"]
    BOUGHT_MINT_CANDS = ["token_bought_mint_address", "token_bought_address"]
    SOLD_MINT_CANDS = ["token_sold_mint_address", "token_sold_address"]
    BOUGHT_AMT_CANDS = ["token_bought_amount"]
    SOLD_AMT_CANDS = ["token_sold_amount"]
    POOL_CANDS = ["project_contract_address", "pool_id", "amm", "pair_address",
                  "project_program_id", "amm_pool", "market_address"]

    def pick(cols: set[str], cands: list[str]) -> str | None:
        return next((c for c in cands if c in cols), None)

    best = None
    for schema, table in candidate_pairs:
        cols = cols_for(schema, table)
        mapping = {
            "time": pick(cols, TIME_CANDS), "tx": pick(cols, TX_CANDS),
            "bought_mint": pick(cols, BOUGHT_MINT_CANDS), "sold_mint": pick(cols, SOLD_MINT_CANDS),
            "bought_amt": pick(cols, BOUGHT_AMT_CANDS), "sold_amt": pick(cols, SOLD_AMT_CANDS),
            "pool": pick(cols, POOL_CANDS),
        }
        n_found = sum(1 for v in mapping.values() if v)
        if best is None or n_found > best[2]:
            best = (schema, table, n_found, mapping, sorted(cols))

    chosen_schema, chosen_table, n_found, mapping, all_cols = best
    out["step2_chosen_table"] = f"{chosen_schema}.{chosen_table}"
    out["step2_chosen_table_all_columns"] = all_cols
    out["step2_field_mapping_guess"] = mapping
    print(f"[dune_probe] Шаг 2: выбрана {chosen_schema}.{chosen_table} ({n_found}/7 нужных полей найдено).")
    print(f"[dune_probe] Маппинг полей: {mapping}")

    required = ["time", "tx", "bought_mint", "sold_mint", "bought_amt", "sold_amt"]
    missing = [k for k in required if not mapping.get(k)]
    if missing:
        out["HONEST_ANSWER"] = (
            f"В {chosen_schema}.{chosen_table} не найдены обязательные поля {missing} "
            "(время/минты/суммы обеих сторон свопа) под известными нам именами -- "
            f"см. step2_chosen_table_all_columns={all_cols} для ручного сопоставления. "
            "Запрос 3 не выполняю -- нет смысла тратить кредиты без нужных колонок."
        )
        print("[dune_probe] " + out["HONEST_ANSWER"])
        return out
    if not mapping.get("pool"):
        out.setdefault("warnings", []).append(
            f"В {chosen_schema}.{chosen_table} НЕТ явного поля адреса пула/AMM среди {POOL_CANDS} -- "
            "запрос 3 отфильтрует ТОЛЬКО по паре минтов (SOL/USDC), без привязки к конкретному пулу "
            f"{POOL}. Если у SOL/USDC на Solana несколько ликвидных пулов, вернутся сделки из ЛЮБОГО "
            "из них, не обязательно нашего -- цены могут разойтись именно по этой причине, не из-за "
            "ошибки атрибуции роутера (урок Fomo), но результат нужно читать с этой оговоркой."
        )
        print("[dune_probe] ПРЕДУПРЕЖДЕНИЕ: " + out["warnings"][-1])

    # ---------- Запрос 3: сэмпл сделок за ОДНО известное окно ----------
    lo = min(r["target"] for r in REFERENCE_POINTS) - 5
    hi = max(r["target"] for r in REFERENCE_POINTS) + 5
    time_col, tx_col = mapping["time"], mapping["tx"]
    bmint, smint = mapping["bought_mint"], mapping["sold_mint"]
    bamt, samt = mapping["bought_amt"], mapping["sold_amt"]
    pool_col = mapping.get("pool")
    select_cols = [time_col, tx_col, bmint, smint, bamt, samt] + ([pool_col] if pool_col else [])
    select_clause = ", ".join(select_cols)
    pool_filter = f"AND {pool_col} = '{POOL}'" if pool_col else ""
    sql3 = f"""
    SELECT {select_clause}
    FROM {chosen_schema}.{chosen_table}
    WHERE {time_col} >= from_unixtime({lo}) AND {time_col} <= from_unixtime({hi})
      AND (({bmint} = '{SOL}' AND {smint} = '{USDC}') OR ({bmint} = '{USDC}' AND {smint} = '{SOL}'))
      {pool_filter}
    ORDER BY {time_col}
    LIMIT 100
    """.strip()
    out["step3_sql"] = sql3
    df3 = client.run_sql_cached(
        name="solana_dune_probe_3_sample", sql=sql3, estimated_credits=8.0,
        expected_max_rows=200, expected_columns=len(select_cols) + 2,
    )
    out["step3_n_rows"] = len(df3)
    out["step3_raw_rows"] = df3.to_dict("records")
    print(f"[dune_probe] Шаг 3: {len(df3)} строк за окно [{lo},{hi}] ({hi-lo}с) "
          f"из {chosen_schema}.{chosen_table} (фильтр по пулу: {'да' if pool_col else 'НЕТ -- см. warnings'}).")

    if df3.empty:
        out["HONEST_ANSWER"] = (
            f"0 строк за 5-минутное окно на пуле, который на цепи в это же время дал 6 реальных "
            f"свопов -- либо {chosen_schema}.{chosen_table} не покрывает этот пул/протокол (проверьте "
            "warnings про отсутствие pool_col и project/dex поле в step2_chosen_table_all_columns), "
            "либо атрибуция здесь тоже идёт на исполнителя/роутер, а не на пул (урок Fomo). "
            "НЕ переводим расчёт на Dune без разбора причины."
        )
        print("[dune_probe] " + out["HONEST_ANSWER"])
        return out

    # ---------- Сверка: для каждой из 6 точек -- последняя сделка Dune на/до t ----------
    import pandas as pd
    df3["_ts"] = pd.to_datetime(df3[time_col], utc=True).astype("int64") // 10**9
    df3 = df3.sort_values("_ts").reset_index(drop=True)

    def dune_price_usdc_per_sol(row) -> D | None:
        b, s = row[bmint], row[smint]
        try:
            ba, sa = D(str(row[bamt])), D(str(row[samt]))
        except Exception:
            return None
        if b == USDC and s == SOL and sa != 0:
            return ba / sa
        if b == SOL and s == USDC and ba != 0:
            return sa / ba
        return None

    comparisons = []
    for ref in REFERENCE_POINTS:
        t = ref["target"]
        at_or_before = df3[df3["_ts"] <= t]
        if at_or_before.empty:
            comparisons.append({**ref, "dune_status": "no_trade_at_or_before_t_in_sample"})
            continue
        last_row = at_or_before.iloc[-1]
        dune_price = dune_price_usdc_per_sol(last_row)
        if dune_price is None:
            comparisons.append({**ref, "dune_status": "trade_found_but_price_undeterminable",
                                 "dune_raw_row": last_row.to_dict()})
            continue
        onchain_price = D(ref["onchain_price_usdc_per_sol"])
        diff_pct = abs(dune_price - onchain_price) / onchain_price * 100
        comparisons.append({
            **ref, "dune_status": "ok",
            "dune_trade_time": int(last_row["_ts"]), "dune_tx": str(last_row[tx_col]),
            "dune_price_usdc_per_sol": str(dune_price), "diff_pct": str(diff_pct),
            "age_seconds": t - int(last_row["_ts"]),
        })
        print(f"[dune_probe] t={t} (+{ref['seconds']}с) onchain={onchain_price} dune={dune_price} "
              f"diff={diff_pct:.4f}% (свеча/сделка {t-int(last_row['_ts'])}с назад)")
    out["comparisons"] = comparisons

    diffs = [D(c["diff_pct"]) for c in comparisons if c.get("dune_status") == "ok"]
    n_matched = len(diffs)
    out["n_matched"] = n_matched
    out["n_total_reference"] = len(REFERENCE_POINTS)
    if diffs:
        out["max_diff_pct"] = str(max(diffs))
        out["mean_diff_pct"] = str(sum(diffs) / len(diffs))

    # ---------- Оценка стоимости полного перевода ----------
    ledger = client.credit_ledger
    real_costs = [e["credits"] for e in ledger if e["credits"] is not None and not e.get("cached")]
    total_probe_cost = sum(c for c in real_costs if c)
    out["credit_ledger"] = ledger
    out["total_probe_cost_credits"] = total_probe_cost
    # Экстраполяция: запрос 3 стоил X кредитов за одно 5-минутное окно ОДНОГО
    # пула (сколько бы сделок в нём ни оказалось после LIMIT 100). Полный
    # перевод -- это не 250 отдельных запросов, а разумно один-два запроса
    # НА ПУЛ с широким OR-объединением всех нужных 5-минутных окон этого
    # пула (у нас уже есть список target-времён по каждой покупке) -- дороже
    # запроса 3 пропорционально ширине объединённого фильтра/скана, но
    # порядок величины даёт именно cost(запрос_3) как нижнюю границу "за
    # пул", умноженную на число различных промежуточных/итоговых пулов
    # среди оставшихся 250 покупок (не совпадает 1:1 с числом покупок --
    # многие делят один и тот же пул, см. MERGE_GAP_TOLERANCE_S в
    # solana_buyer200_fast_price.py).
    query3_cost = next((e["credits"] for e in ledger if e["name"] == "solana_dune_probe_3_sample"), None)
    out["cost_projection_note"] = (
        "НИЖНЯЯ ГРАНИЦА, не точная цена: query3_cost (одно 5-минутное окно "
        f"одного пула) = {query3_cost} кредитов. Честная оценка полного "
        "перевода требует знать РЕАЛЬНОЕ число различных пулов (не покупок) "
        "среди оставшихся ~250 -- это НЕ посчитано в этом пробе (нужен "
        "отдельный подсчёт по routes_300.json), поэтому здесь только "
        "query3_cost как ориентир 'за одно окно одного пула', без "
        "домножения на выдуманное число пулов."
    )

    return out


def _finish(out: dict) -> None:
    import time
    out["generated_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"[dune_probe] Записано {OUT_PATH}")


def main() -> None:
    """Прежнее прямое использование (один ключ, DUNE_API_KEY из env) --
    оставлено для совместимости и ручных перезапусков на уже известном
    рабочем ключе. Проверка прав ключа (403 read-only и т.п.) теперь
    делается ДО этого в solana_dune_key_check.py -- см. его docstring."""
    client = DuneClient()
    out = run_probe(client)
    _finish(out)


if __name__ == "__main__":
    main()
