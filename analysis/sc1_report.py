#!/usr/bin/env python3
"""Sprint SC1, Шаг 4-5: сборка юнит-экономики по кластерам и вердикт по
замороженному критерию (с поправкой владельца на полные издержки:
launchFee + газ, не только газ) -- ПОЛНОСТЬЮ ЛОКАЛЬНО, 0 кредитов, из
уже закэшированных CSV.

Вход:
  sc1_august_launches_decoded.csv -- token, deployer (39680 запусков)
  sc1_deployer_to_cluster.csv     -- deployer -> cluster_id (уровень 2)
  sc1_volume_24h_all.csv          -- token -> vol_usd_24h, n_trades_24h
                                      (только 30394/39680 -- у остальных
                                      объём за 24ч точно 0, INNER JOIN
                                      на dex.trades ничего не находит)
  sc1_v1_launchfee_exact.csv      -- launchFee = 0.0005 ETH точно
  sc1_v1_launch_tx_gas_agg.csv    -- gas_used/gas_price медианы
  sc1_eth_usd_price.csv           -- медианный курс ETH/USD

Выход: data/sprintSC1_cache/sc1_cluster_economics.csv + печать вердикта.
Использование: python analysis/sc1_report.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

from config import CONFIG

CACHE_DIR = Path(CONFIG.sc1_cache_dir)


def main() -> int:
    launches = pd.read_csv(CACHE_DIR / "sc1_august_launches_decoded.csv")[["token", "deployer"]]
    dep2cl = pd.read_csv(CACHE_DIR / "sc1_deployer_to_cluster.csv")
    volume = pd.read_csv(CACHE_DIR / "sc1_volume_24h_all.csv")[["token", "vol_usd_24h", "n_trades_24h"]]
    fee_df = pd.read_csv(CACHE_DIR / "sc1_v1_launchfee_exact.csv")
    gas_df = pd.read_csv(CACHE_DIR / "sc1_v1_launch_tx_gas_agg.csv")
    eth_df = pd.read_csv(CACHE_DIR / "sc1_eth_usd_price.csv")

    launch_fee_eth = fee_df["value_min_nonzero"].iloc[0] / 1e18  # 0.0005 ETH точно
    gas_used_median = gas_df["gas_used_median"].iloc[0]
    gas_price_median = gas_df["gas_price_median"].iloc[0]
    gas_cost_eth = gas_used_median * gas_price_median / 1e18
    eth_usd = eth_df["eth_usd_price_median"].iloc[0]

    full_cost_eth_per_launch = launch_fee_eth + gas_cost_eth
    full_cost_usd_per_launch = full_cost_eth_per_launch * eth_usd

    print("===== Входные константы =====")
    print(f"launchFee: {launch_fee_eth} ETH (точно, из мин.ненулевых value)")
    print(f"gas: {gas_used_median:.1f} gas_used(медиана) x {gas_price_median:.2f} wei/gas(медиана) "
          f"= {gas_cost_eth:.8f} ETH")
    print(f"ETH/USD (медиана, август 2026, dex.trades): ${eth_usd:.2f}")
    print(f"Полная невозвратная стоимость 1 запуска: {full_cost_eth_per_launch:.8f} ETH "
          f"= ${full_cost_usd_per_launch:.4f}")

    # token -> deployer -> cluster_id
    tok = launches.merge(dep2cl, on="deployer", how="left")
    # 8695 деплоеров без funding_parent остаются singleton (cluster_id = deployer сам) --
    # это уже посчитано в sc1_deployer_to_cluster.csv (fillna на уровне sc1_cluster_level2.py),
    # но перепроверим на всякий случай (не должно быть NaN).
    missing = tok["cluster_id"].isna().sum()
    if missing:
        print(f"[sc1_report] ВНИМАНИЕ: {missing} токенов без cluster_id -- заполняю деплоером (singleton).")
        tok["cluster_id"] = tok["cluster_id"].fillna(tok["deployer"])

    # объём за 24ч -- LEFT JOIN, у токенов без сделок в окне подставляем 0
    # (это те самые "ноль внешних покупателей за 24ч").
    tok = tok.merge(volume, on="token", how="left")
    tok["vol_usd_24h"] = tok["vol_usd_24h"].fillna(0.0)
    tok["n_trades_24h"] = tok["n_trades_24h"].fillna(0).astype(int)
    tok["has_zero_volume"] = tok["n_trades_24h"] == 0

    # creator fee revenue за 24ч = 1.00% объёма (эмпирический V1 fee-тир, см. SC1_NOTE.md)
    tok["fee_revenue_usd"] = tok["vol_usd_24h"] * CONFIG.sc1_v1_creator_fee_share_of_volume

    cluster = tok.groupby("cluster_id").agg(
        n_launches=("token", "count"),
        fee_revenue_usd=("fee_revenue_usd", "sum"),
        vol_usd_24h=("vol_usd_24h", "sum"),
        n_zero_volume_tokens=("has_zero_volume", "sum"),
    ).reset_index()
    cluster["zero_volume_share"] = cluster["n_zero_volume_tokens"] / cluster["n_launches"]
    cluster["cost_usd"] = cluster["n_launches"] * full_cost_usd_per_launch
    cluster["fee_to_cost_ratio"] = cluster["fee_revenue_usd"] / cluster["cost_usd"]
    cluster["fee_per_launch_usd"] = cluster["fee_revenue_usd"] / cluster["n_launches"]
    cluster["vol_per_launch_usd"] = cluster["vol_usd_24h"] / cluster["n_launches"]

    total_launches = cluster["n_launches"].sum()
    print(f"\n[sc1_report] Сверка: {total_launches} запусков в {len(cluster)} кластерах "
          f"(должно быть 39680 / 8695-8696).")

    out_path = CACHE_DIR / "sc1_cluster_economics.csv"
    cluster.sort_values("n_launches", ascending=False).to_csv(out_path, index=False)
    print(f"[sc1_report] Записано: {out_path}")

    # ---- Критерий 1: медианный конвейер (>=50 запусков) ----
    conveyor = cluster[cluster["n_launches"] >= CONFIG.sc1_pipeline_min_launches].copy()
    solo = cluster[
        (cluster["n_launches"] >= 1) & (cluster["n_launches"] <= CONFIG.sc1_solo_max_launches)
    ].copy()

    print(f"\n===== Конвейеры (>= {CONFIG.sc1_pipeline_min_launches} запусков): {len(conveyor)} штук =====")
    med_fee = conveyor["fee_revenue_usd"].median()
    med_cost = conveyor["cost_usd"].median()
    med_ratio = conveyor["fee_to_cost_ratio"].median()
    print(f"Медианный конвейер: заработал комиссий (24ч-объём x1.00%) ${med_fee:,.2f}, "
          f"заплатил launchFee+газ ${med_cost:,.2f} -- отношение {med_ratio:.3f}x "
          f"(порог владельца: >= {CONFIG.sc1_go_min_fee_to_gas_ratio}x)")
    print(f"Медианный n_launches в конвейере: {conveyor['n_launches'].median():.0f}")
    print(f"Медианная доля токенов кластера с нулём сделок за 24ч: {conveyor['zero_volume_share'].median():.3f}")

    def deciles(df: pd.DataFrame, col: str) -> pd.Series:
        return df[col].quantile([0.1 * i for i in range(1, 10)])

    print("\n-- Децили (конвейеры) --")
    for col in ["fee_revenue_usd", "fee_per_launch_usd", "vol_per_launch_usd", "zero_volume_share"]:
        print(f"{col}:\n{deciles(conveyor, col).to_string()}")

    print(f"\n===== Одиночки (1-{CONFIG.sc1_solo_max_launches} запусков): {len(solo)} штук =====")
    print(f"Медиана fee_revenue_usd (за весь кластер, 1-4 запуска): ${solo['fee_revenue_usd'].median():,.2f}")
    print(f"Медиана fee_per_launch_usd: ${solo['fee_per_launch_usd'].median():,.4f}")
    print(f"Медиана vol_per_launch_usd: ${solo['vol_per_launch_usd'].median():,.2f}")
    print(f"Медианная доля нулевого объёма: {solo['zero_volume_share'].median():.3f}")
    print("\n-- Децили (одиночки) --")
    for col in ["fee_revenue_usd", "fee_per_launch_usd", "vol_per_launch_usd", "zero_volume_share"]:
        print(f"{col}:\n{deciles(solo, col).to_string()}")

    # ---- Критерий 2: концентрация топ-5% ВСЕХ кластеров по суммарным
    # creator-комиссиям августа (независимо от того, конвейер это или нет) ----
    all_sorted = cluster.sort_values("fee_revenue_usd", ascending=False).reset_index(drop=True)
    n_top = max(1, int(len(all_sorted) * CONFIG.sc1_lottery_top_share))
    total_fees = all_sorted["fee_revenue_usd"].sum()
    top_fees = all_sorted.head(n_top)["fee_revenue_usd"].sum()
    top_share = top_fees / total_fees if total_fees > 0 else float("nan")

    print(f"\n===== Концентрация: топ-{CONFIG.sc1_lottery_top_share*100:.0f}% кластеров "
          f"({n_top} из {len(all_sorted)}) =====")
    print(f"Их доля в суммарных creator-комиссиях августа: {top_share*100:.2f}% "
          f"(порог лотереи: >{CONFIG.sc1_lottery_concentration_threshold*100:.0f}%)")
    print(f"Суммарные комиссии всех кластеров августа: ${total_fees:,.2f}")

    # ---- Вердикт по замороженному критерию + поправка владельца ----
    median_go = med_ratio >= CONFIG.sc1_go_min_fee_to_gas_ratio
    is_lottery = top_share > CONFIG.sc1_lottery_concentration_threshold

    if is_lottery:
        verdict = "ЛОТЕРЕЯ (закрыть идею)" if not median_go else "СМЕШАННЫЙ ИСХОД -> трактуется как ЛОТЕРЕЯ"
    else:
        verdict = "GO (конвейер экономически оправдан по критерию)" if median_go else \
            "KILL (медианный конвейер не окупает launchFee+газ 2x)"

    print(f"\n===== ВЕРДИКТ SC1 =====\n{verdict}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
