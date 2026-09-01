#!/usr/bin/env python3
"""Sprint SC1, Шаг 1 + Шаг 2 (уровень 1): запуски за август 2026 и
группировка по деплоеру -- ПОЛНОСТЬЮ ЛОКАЛЬНО, 0 кредитов, из уже
закэшированных сырых логов TokenLaunched (Sprint G1,
`g1_token_launched_week0{5,6,7,8,9}_*.csv` -- обе фабрики V1+V2, см.
`sql/g1/g1_token_launched_weekly.sql`). Владелец: "V1-запуски: уже в
кэше (266K событий с деплоерами, если поле деплоера есть)" -- поле
есть (topic2 = deployer, см. analysis/g1_common.decode_token_launched),
декодируем повторно без нового запроса к Dune.

ВАЖНО (найдено этим скриптом): исходный SQL-запрос НЕ выбирал
contract_address (только topic1/2/3/data), поэтому из этого кэша
НЕЛЬЗЯ отличить запуск на V1-фабрике от запуска на V2-фабрике --
разбивка по версии фабрики потребует отдельного (дешёвого) запроса на
Шаге 2/3 с явным SELECT contract_address, если критична для
экономики (см. docs/SC1_NOTE.md).

Использование: python analysis/sc1_launches.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

from config import CONFIG
from g1_common import decode_token_launched

WEEKLY_FILES = [
    "data/sprintG1_cache/g1_token_launched_week05_2026-07-29_2855c76d09f55d3c.csv",
    "data/sprintG1_cache/g1_token_launched_week06_2026-08-05_24c88b01dd81f1d5.csv",
    "data/sprintG1_cache/g1_token_launched_week07_2026-08-12_05fbe737b454a042.csv",
    "data/sprintG1_cache/g1_token_launched_week08_2026-08-19_15f2c83a1a63473a.csv",
    "data/sprintG1_cache/g1_token_launched_week09_2026-08-26_c623627802a58586.csv",
]

OUT_DECODED = Path(CONFIG.sc1_cache_dir) / "sc1_august_launches_decoded.csv"
OUT_DEPLOYER_COUNTS = Path(CONFIG.sc1_cache_dir) / "sc1_august_deployer_counts.csv"


def load_august_launches() -> pd.DataFrame:
    rows: list[dict] = []
    for f in WEEKLY_FILES:
        try:
            df = pd.read_csv(f)
        except pd.errors.EmptyDataError:
            continue
        for r in df.to_dict("records"):
            rows.append(decode_token_launched(r))
    edf = pd.DataFrame(rows)
    edf["block_time"] = pd.to_datetime(edf["block_time"])
    aug = edf[
        (edf["block_time"] >= CONFIG.sc1_month_start) & (edf["block_time"] < CONFIG.sc1_month_end)
    ].copy()
    # Дедуп "по первому событию на токен" -- тот же принцип, что
    # analysis/g1_graduation_events.py (редкие дубли на границах партиций).
    aug = aug.sort_values("block_time").drop_duplicates(subset=["token"], keep="first")
    return aug.reset_index(drop=True)


def main() -> int:
    aug = load_august_launches()
    Path(CONFIG.sc1_cache_dir).mkdir(parents=True, exist_ok=True)
    aug.to_csv(OUT_DECODED, index=False)

    print(f"[sc1_launches] Август 2026: {len(aug)} запусков (уникальных token), "
          f"{aug['deployer'].nunique()} уникальных деплоеров.")
    print(f"[sc1_launches] Период факт: {aug['block_time'].min()} .. {aug['block_time'].max()}")

    counts = (
        aug.groupby("deployer").size().rename("n_launches").reset_index()
        .sort_values("n_launches", ascending=False).reset_index(drop=True)
    )
    counts.to_csv(OUT_DEPLOYER_COUNTS, index=False)

    n_pipeline = (counts["n_launches"] >= CONFIG.sc1_pipeline_min_launches).sum()
    n_solo = (
        (counts["n_launches"] >= 1) & (counts["n_launches"] <= CONFIG.sc1_solo_max_launches)
    ).sum()
    print(f"[sc1_launches] Уровень 1 (по деплоеру, ДО funding-parent склейки):")
    print(f"  Деплоеров с >= {CONFIG.sc1_pipeline_min_launches} запусков: {n_pipeline}")
    print(f"  Деплоеров с 1-{CONFIG.sc1_solo_max_launches} запусками: {n_solo}")
    print(f"  Топ-10 деплоеров:\n{counts.head(10).to_string(index=False)}")
    print(f"\n[sc1_launches] Записано: {OUT_DECODED}, {OUT_DEPLOYER_COUNTS}")
    print("[sc1_launches] ВАЖНО: contract_address (V1 vs V2 фабрика) НЕ в этом кэше -- "
          "см. docstring. Уровень 2 (funding-parent) требует отдельного Dune-запроса.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
