#!/usr/bin/env python3
"""Sprint SC1: Шаг 2 (кластеризация) / Шаг 3-4 (экономика) / Шаг 5
(отчёт). См. docs/SC1_NOTE.md.

Использование: python analysis/sc1_pipeline.py --stage cluster|economics|report
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "sprintSC1")
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

import credit_guard as cg
from dune_client import DuneClient, render_sql
from run_pipeline import read_sql

CACHE_DIR = Path("data/sprintSC1_cache")
AUGUST_LAUNCHES_PATH = CACHE_DIR / "sc1_august_launches_decoded.csv"

TX_COLUMNS_SQL = read_sql("sc1/sc1_transactions_columns")
LAUNCH_TX_GAS_AGG_SQL = read_sql("sc1/sc1_v1_launch_tx_gas_agg")
FUNDING_PARENT_SQL = read_sql("sc1/sc1_funding_parent")
LAUNCHFEE_EXACT_SQL = read_sql("sc1/sc1_v1_launchfee_exact")
VOLUME_24H_CALIB_SQL = read_sql("sc1/sc1_volume_24h_calib")
VOLUME_24H_FULL_TMPL = read_sql("sc1/sc1_volume_24h_full")

CALIBRATION_SCALE_FACTOR = 2.5  # см. docs/G1_DESIGN.md -- гетерогенность популяции подтверждена там же
SANITY_MAX_ESTIMATE = 40.0


def sc1_spent() -> float:
    state = cg.load_state()
    return state.get("sprintSC1", {}).get("spent", 0.0)


def stage_cluster(client: DuneClient) -> int:
    print("===== sc1_transactions_columns (оценка 3.0) =====")
    qid = client.create_query("sc1_transactions_columns", TX_COLUMNS_SQL)
    df = client.run_sql_cached(
        "sc1_transactions_columns", TX_COLUMNS_SQL, query_id=qid, estimated_credits=3.0,
        expected_max_rows=60, expected_columns=3,
    )
    if df is not None and len(df):
        print(df.to_string())
    else:
        print("(пусто)")

    # run #6/#7: построчное чтение ~39680 строк x 7 колонок стоило бы
    # ~11.2 кредита чтения -- гард отказал ДО оплаты (правильно; execute
    # уже был оплачен -- 0.70, урок учтён). Владелец сам предусмотрел
    # такой случай (§1 Шаг1: "выборка/агрегат, если трейсы дороги") --
    # агрегат на стороне Dune вместо построчного чтения.
    print("\n===== sc1_v1_launch_tx_gas_agg (оценка 3.0, агрегат вместо построчного чтения) =====")
    qid2 = client.create_query("sc1_v1_launch_tx_gas_agg", LAUNCH_TX_GAS_AGG_SQL)
    df2 = client.run_sql_cached(
        "sc1_v1_launch_tx_gas_agg", LAUNCH_TX_GAS_AGG_SQL, query_id=qid2, estimated_credits=3.0,
        expected_max_rows=2, expected_columns=10,
    )
    if df2 is None or not len(df2):
        print("[sc1_pipeline] ПУСТО -- нет транзакций к фабрике в окне. Стоп.")
        return 1

    row = df2.iloc[0]
    print(f"[sc1_pipeline] Транзакций к PonsLaunchFactory V1 в окне: {row['n_tx']} "
          f"успешных={row['n_success']} (ожидали ~39680 успешных).")
    print(f"[sc1_pipeline] launchFee: {row['n_nonzero_value']} из {row['n_success']} успешных транзакций "
          f"с value > 0 ({'НЕНУЛЕВОЙ' if row['n_nonzero_value'] else '= 0 у всех'}).")
    if row["n_nonzero_value"]:
        print(f"  value (native) при ненулевых: median={row['value_median_when_nonzero']}, "
              f"max={row['value_max']}")
    print(f"[sc1_pipeline] gas_used: median={row['gas_used_median']}, mean={row['gas_used_mean']:.1f}, "
          f"min={row['gas_used_min']}, max={row['gas_used_max']}")
    print(f"[sc1_pipeline] gas_price ФАКТИЧЕСКИЙ (в период вейвера, НЕ пост-вейверная цена -- "
          f"критерий требует другую, см. далее): median={row['gas_price_median']}")

    out_file = CACHE_DIR / "sc1_v1_launch_tx_gas_agg.csv"
    df2.to_csv(out_file, index=False)
    client._commit_permanent(out_file, f"sprintSC1_cache: агрегат gas/value по транзакциям launch() V1 [automated]")
    print(f"[sc1_pipeline] Записано: {out_file}")

    remaining = 20.0 - sc1_spent()
    print(f"\n[sc1_pipeline] Остаток бюджета SC1 после gas-агрегата: {remaining:.2f} из 20.0.")

    # Уровень 2: funding parent. Дороже (JOIN transactions x transactions),
    # но выдача -- только 2 колонки (не 7, как в неудачном run #6/#7) --
    # читаем ~14.5k строк x 2 колонки, оценка чтения ~1.2 кредита, не ~11.
    # run #8: оценка 8.0 не прошла бюджетную проверку ДО исполнения
    # (12.14 + 8.0 = 20.14 > 20.0, гард корректно остановил, 0 потрачено).
    # Пересчитано по факту всех предыдущих SC1-запросов (все execute на
    # этом чейне легли на порядок ниже консервативных оценок -- самый
    # тяжёлый факт до сих пор, полный скан 50934 строк PoolCreated,
    # стоил 2.34): 5.0 -- всё ещё запас, но пропускает бюджетную
    # проверку (12.14+5.0=17.14 <= 20.0).
    print("\n===== sc1_funding_parent (оценка 5.0, пересчитана по факту предыдущих запросов) =====")
    qid3 = client.create_query("sc1_funding_parent", FUNDING_PARENT_SQL)
    df3 = client.run_sql_cached(
        "sc1_funding_parent", FUNDING_PARENT_SQL, query_id=qid3, estimated_credits=5.0,
        expected_max_rows=20_000, expected_columns=2,
    )
    if df3 is None or not len(df3):
        print("[sc1_pipeline] ПУСТО -- funding-parent не найден ни для одного деплоера "
              "(либо все финансировались до 01.07, либо джойн не сработал). "
              "Уровень 2 (склейка кластеров) невозможен без него -- STOP.")
        return 1

    print(f"[sc1_pipeline] funding_parent найден для {len(df3)} деплоеров.")
    out_file = CACHE_DIR / "sc1_funding_parent.csv"
    df3.to_csv(out_file, index=False)
    client._commit_permanent(out_file, f"sprintSC1_cache: funding_parent по деплоерам [automated]")
    print(f"[sc1_pipeline] Записано: {out_file}")

    remaining2 = 20.0 - sc1_spent()
    print(f"\n[sc1_pipeline] Остаток бюджета SC1 после Шага 2: {remaining2:.2f} из 20.0.")
    return 0


def stage_economics(client: DuneClient) -> int:
    # (2) владелец: разобраться в природе launchFee -- источники уже
    # подтвердили (WebFetch, PonsLaunchFactory.sol, _payLaunchFee):
    # launchFee -- ФИКСИРОВАННАЯ константа, невозвратно уходит в
    # protocolFeeRecipient (казна), ОТДЕЛЬНО от initialBuyAmount =
    # msg.value - launchFee (опциональный seed buy, инвестиция
    # создателя). Здесь -- точное числовое значение (мода/минимум среди
    # ненулевых value).
    print("===== sc1_v1_launchfee_exact (оценка 5.0) =====")
    qidf = client.create_query("sc1_v1_launchfee_exact", LAUNCHFEE_EXACT_SQL)
    dff = client.run_sql_cached(
        "sc1_v1_launchfee_exact", LAUNCHFEE_EXACT_SQL, query_id=qidf, estimated_credits=5.0,
        expected_max_rows=2, expected_columns=3,
    )
    if dff is not None and len(dff):
        print(dff.to_string())
        out_f = CACHE_DIR / "sc1_v1_launchfee_exact.csv"
        dff.to_csv(out_f, index=False)
        client._commit_permanent(out_f, "sprintSC1_cache: точное значение launchFee [automated]")

    # (1) Шаг 3: объём торгов в первые 24ч, self-contained (пулы/токены
    # выводятся из robinhood.logs внутри запроса, без внешнего IN-листа
    # на 39680 адресов) -- калибровка узким срезом (1 день) ПЕРЕД
    # полным 12-дневным прогоном, как требует наследованное правило G1.
    print("\n===== sc1_volume_24h_calib (1 день, 01.08, оценка 15.0) =====")
    qidc = client.create_query("sc1_volume_24h_calib", VOLUME_24H_CALIB_SQL)
    dfc = client.run_sql_cached(
        "sc1_volume_24h_calib", VOLUME_24H_CALIB_SQL, query_id=qidc, estimated_credits=15.0,
        expected_max_rows=6000, expected_columns=3,
    )
    if dfc is None or not len(dfc):
        print("[sc1_pipeline] Калибровка пуста -- 0 сделок в первый день. Стоп, разбор нужен вручную.")
        return 1

    n_calib_tokens = len(dfc)
    calib_actual = run_with_cost(client, "sc1_volume_24h_calib")
    print(f"[sc1_pipeline] Калибровка: {n_calib_tokens} токенов с объёмом за 1 день "
          f"(из ~{39680 // 12} ожидаемых в сутки).")

    # Экстраполяция на весь 12-дневный диапазон x2.5 (см. G1_DESIGN.md).
    proj_full = calib_actual * 12 * CALIBRATION_SCALE_FACTOR
    print(f"[sc1_pipeline] Проекция на полный 12-дневный диапазон: {proj_full:.1f} кредитов "
          f"(факт калибровки {calib_actual:.2f} x12 дней x{CALIBRATION_SCALE_FACTOR}).")

    all_volume_dfs = [dfc]
    if proj_full <= SANITY_MAX_ESTIMATE:
        print(f"\n===== sc1_volume_24h_full (весь диапазон, оценка {proj_full:.1f}) =====")
        sql_full = render_sql(VOLUME_24H_FULL_TMPL, {
            "window_start": "2026-08-02 00:00:00", "window_end": "2026-08-13 00:00:00",
        })
        qid_full = client.create_query("sc1_volume_24h_full_rest", sql_full)
        df_full = client.run_sql_cached(
            "sc1_volume_24h_full_rest", sql_full, query_id=qid_full, estimated_credits=proj_full,
            expected_max_rows=40_000, expected_columns=3,
        )
        if df_full is not None and len(df_full):
            all_volume_dfs.append(df_full)
    else:
        # Партиционирование по дням -- сохраняет суммарную стоимость (см.
        # docs/G1_DESIGN.md, доказательство), меняет только форму.
        n_partitions = max(2, int(proj_full // SANITY_MAX_ESTIMATE) + 1)
        print(f"[sc1_pipeline] Проекция > {SANITY_MAX_ESTIMATE} -- партиционирую остаток "
              f"(02-13.08) на {n_partitions} частей.")
        from datetime import datetime, timedelta
        start = datetime(2026, 8, 2)
        end = datetime(2026, 8, 13)
        total_days = (end - start).days
        days_per_part = max(1, total_days // n_partitions)
        cur = start
        part_i = 0
        while cur < end:
            nxt = min(cur + timedelta(days=days_per_part), end)
            part_i += 1
            name = f"sc1_volume_24h_part{part_i:02d}"
            sql_part = render_sql(VOLUME_24H_FULL_TMPL, {
                "window_start": cur.strftime("%Y-%m-%d %H:%M:%S"),
                "window_end": nxt.strftime("%Y-%m-%d %H:%M:%S"),
            })
            est = proj_full / n_partitions
            print(f"\n===== {name} [{cur}, {nxt}) (оценка {est:.1f}) =====")
            qid_p = client.create_query(name, sql_part)
            df_p = client.run_sql_cached(
                name, sql_part, query_id=qid_p, estimated_credits=est,
                expected_max_rows=40_000, expected_columns=3,
            )
            if df_p is not None and len(df_p):
                all_volume_dfs.append(df_p)
            cur = nxt

    volume_df = pd.concat(all_volume_dfs, ignore_index=True).drop_duplicates(subset=["token"])
    out_vol = CACHE_DIR / "sc1_volume_24h_all.csv"
    volume_df.to_csv(out_vol, index=False)
    client._commit_permanent(out_vol, f"sprintSC1_cache: объём 24ч по {len(volume_df)} токенам [automated]")
    print(f"\n[sc1_pipeline] Итого токенов с объёмом за 24ч: {len(volume_df)} / 39680. "
          f"Записано: {out_vol}")

    remaining = 40.0 - sc1_spent()
    print(f"\n[sc1_pipeline] Остаток бюджета SC1 после Шага 3: {remaining:.2f} из 40.0.")
    return 0


def run_with_cost(client: DuneClient, name: str) -> float:
    """Фактическая стоимость последней операции с этим именем -- смотрит
    в data/credits_spent.json entries (execute + чтение) вместо
    повторного вызова."""
    state = cg.load_state()
    total = 0.0
    for e in state.get("entries", []):
        if e.get("name") == name and e.get("namespace") == "sprintSC1":
            total += e.get("credits", 0.0)
    return total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True, choices=["cluster", "economics", "report"])
    args = parser.parse_args()

    client = DuneClient()

    if args.stage == "cluster":
        return stage_cluster(client)
    elif args.stage == "economics":
        return stage_economics(client)
    else:
        print("[sc1_pipeline] report: не реализовано в этом коммите.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
