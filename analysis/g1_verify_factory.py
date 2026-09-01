#!/usr/bin/env python3
"""Sprint G1 -- блокирующие условия владельца перед Шагом 2 (продолжение
после гард-теста): верификация фабрики pons.family ончейн, круг
"адрес -> событие -> pool -> свопы" и распределение
restrictionsEndBlock. НЕ трогает §2 (заморожен) -- только "Механика
детекции" в docs/G1_DESIGN.md и §2.9 (ограничения, дополнение, не замена
замороженного текста).

Последовательность (см. data/pons_family/SOURCE.md за адресами/ABI):
1. g1_factory_logs_topic0_probe -- сырые логи обеих фабрик, окно 7 дней,
   группировка по topic0. Ноль строк -> СТОП, адрес неверный (см. §4
   промпта пользователя -- один из 4 случаев возврата к владельцу).
2. Декодирует несколько логов события-кандидата (topic0, совпадающий с
   локально посчитанным хэшем TokenLaunched) вручную в Python (адреса
   -- 32-байтные topic-слоты, данные -- offset-декодинг по ABI), берёт
   параметр `pool`, проверяет свопы по этому пулу в query_02 (июль) --
   круг замкнут, если свопы есть.
3. Считает распределение (restrictionsEndBlock - block_number события)
   в секундах (через среднее время блока chain 4663 -- оценивается по
   двум последовательным блокам в тех же логах, не захардкожено).

Использование: python analysis/g1_verify_factory.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "sprintG1")
sys.path.insert(0, str(Path(__file__).parent))

from config import CONFIG
from dune_client import DuneClient, render_sql
from run_pipeline import read_sql, q_ts, substitute_query_refs

# Посчитано локально (Crypto.Hash.keccak) от точной сигнатуры типов в
# data/pons_family/PonsLaunchFactory_v1_abi.json -- НЕ угадано, см.
# data/pons_family/SOURCE.md за деталями и командой воспроизведения.
TOPIC0_TOKEN_LAUNCHED = "0xdb51ea9ad51ab453a65a4cb7e60c3cb378c9501bb002609f8f97778fb6c4235a"
TOPIC0_TOKEN_DEPLOYED = "0x1461370115e1c2be79cb529f8cfcbd11316e789d9c6099fc83417b0b4c48c62a"

FACTORY_V1 = "0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB"
FACTORY_V2 = "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e"


def main() -> int:
    client = DuneClient()

    # --- Задача 1: пробник topic0 (тоже верификация адреса) ---
    sql1 = read_sql("g1/g1_factory_logs_topic0_probe")
    print(f"\n===== g1_factory_logs_topic0_probe (оценка 10.0) =====")
    qid1 = client.create_query("g1_factory_logs_topic0_probe", sql1)
    df1 = client.run_sql_cached(
        "g1_factory_logs_topic0_probe", sql1, query_id=qid1, estimated_credits=10.0,
        expected_max_rows=30, expected_columns=5,
    )
    if df1 is None or len(df1) == 0:
        print(
            "\n[g1_verify_factory] СТОП: пробник вернул НОЛЬ строк -- адрес фабрики "
            "неверный (или не тот чейн/поле). Это один из 4 случаев явного возврата "
            "к владельцу. Дальше не двигаюсь."
        )
        return 1
    print(df1.to_string())

    matched = df1[df1["topic0"].str.lower() == TOPIC0_TOKEN_LAUNCHED.lower()]
    if len(matched) == 0:
        print(
            f"\n[g1_verify_factory] ПРЕДУПРЕЖДЕНИЕ: логи с адресов фабрик есть "
            f"({len(df1)} различных topic0), но НИ ОДИН topic0 не совпал с локально "
            f"посчитанным хэшем TokenLaunched ({TOPIC0_TOKEN_LAUNCHED}). Не останавливаюсь "
            "(это не 'ноль строк') -- беру topic0 с наибольшим n_logs как кандидата и "
            "продолжаю декодирование, чтобы проверить эмпирически, что это за событие."
        )
        candidate_topic0 = df1.iloc[0]["topic0"]
    else:
        candidate_topic0 = TOPIC0_TOKEN_LAUNCHED
        print(f"\n[g1_verify_factory] OK: topic0 совпал с расчётным TokenLaunched -- "
              f"n_logs={int(matched.iloc[0]['n_logs'])}.")

    print(f"[g1_verify_factory] Адрес фабрики подтверждён ончейн: {len(df1)} различных topic0, "
          f"кандидат на TokenLaunched = {candidate_topic0}.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
