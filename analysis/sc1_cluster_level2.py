#!/usr/bin/env python3
"""Sprint SC1, Шаг 2 (уровень 2): склейка деплоеров в кластеры по
funding_parent -- ПОЛНОСТЬЮ ЛОКАЛЬНО, 0 кредитов, из уже закэшированных
`sc1_august_deployer_counts.csv` (уровень 1) и `sc1_funding_parent.csv`
(Dune, run #9). Одна итерация склейки (владелец: "глубже не строить") --
cluster_id = funding_parent, если найден; иначе сам деплоер (singleton).

Использование: python analysis/sc1_cluster_level2.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

from config import CONFIG

CACHE_DIR = Path(CONFIG.sc1_cache_dir)
OUT_PATH = CACHE_DIR / "sc1_clusters_level2.csv"


def main() -> int:
    counts = pd.read_csv(CACHE_DIR / "sc1_august_deployer_counts.csv")
    fp = pd.read_csv(CACHE_DIR / "sc1_funding_parent.csv")

    merged = counts.merge(fp, on="deployer", how="left")
    merged["cluster_id"] = merged["funding_parent"].fillna(merged["deployer"])

    cluster_sizes = (
        merged.groupby("cluster_id")["n_launches"].sum()
        .sort_values(ascending=False).reset_index()
    )
    cluster_sizes.to_csv(OUT_PATH, index=False)

    n_conveyor = (cluster_sizes["n_launches"] >= CONFIG.sc1_pipeline_min_launches).sum()
    n_solo = (
        (cluster_sizes["n_launches"] >= 1) & (cluster_sizes["n_launches"] <= CONFIG.sc1_solo_max_launches)
    ).sum()

    print(f"[sc1_cluster_level2] Деплоеров с funding_parent: {len(fp)} / {len(counts)}.")
    print(f"[sc1_cluster_level2] Уникальных кластеров (уровень 2): {len(cluster_sizes)} "
          f"(было {len(counts)} деплоеров на уровне 1).")
    print(f"[sc1_cluster_level2] Кластеров-конвейеров (>= {CONFIG.sc1_pipeline_min_launches} запусков): {n_conveyor}")
    print(f"[sc1_cluster_level2] Кластеров-одиночек (1-{CONFIG.sc1_solo_max_launches} запусков): {n_solo}")
    print(f"[sc1_cluster_level2] Сумма launches по кластерам: {cluster_sizes['n_launches'].sum()} "
          f"(сверка -- должно быть 39680).")
    print(f"[sc1_cluster_level2] Топ-10 кластеров:\n{cluster_sizes.head(10).to_string(index=False)}")
    print(f"\n[sc1_cluster_level2] Записано: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
