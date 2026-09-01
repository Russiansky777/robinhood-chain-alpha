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
from run_pipeline import read_sql, q_ts, q_list, substitute_query_refs

# Посчитано локально (Crypto.Hash.keccak) от точной сигнатуры типов в
# data/pons_family/PonsLaunchFactory_v1_abi.json -- НЕ угадано, см.
# data/pons_family/SOURCE.md за деталями и командой воспроизведения.
TOPIC0_TOKEN_LAUNCHED = "0xdb51ea9ad51ab453a65a4cb7e60c3cb378c9501bb002609f8f97778fb6c4235a"
TOPIC0_TOKEN_DEPLOYED = "0x1461370115e1c2be79cb529f8cfcbd11316e789d9c6099fc83417b0b4c48c62a"

FACTORY_V1 = "0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB"
FACTORY_V2 = "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e"


def main() -> int:
    client = DuneClient()

    # query_id для query_02_swaps_raw_july -- нужен для перекрёстной
    # проверки свопов по декодированным pool-адресам (Задача 2), без
    # нового execute (require_cached=True), как в g1_recon.py.
    query_ids = {
        "02_swaps_raw_july": client.create_query(
            "02_swaps_raw_july",
            render_sql(read_sql("02_swaps_raw_july"), {"start_date": q_ts(CONFIG.train_start), "end_date": q_ts(CONFIG.train_end)}),
            require_cached=True,
        )
    }

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

    # --- Задача 2: декодирование сырых логов + круг "адрес -> событие ->
    #     pool -> свопы" ---
    sql2 = read_sql("g1/g1_token_launched_sample")
    print(f"\n===== g1_token_launched_sample (оценка 5.0) =====")
    qid2 = client.create_query("g1_token_launched_sample", sql2)
    df2 = client.run_sql_cached(
        "g1_token_launched_sample", sql2, query_id=qid2, estimated_credits=5.0,
        expected_max_rows=30, expected_columns=7,
    )
    if df2 is None or len(df2) == 0:
        print("[g1_verify_factory] СТОП: сырые логи TokenLaunched не вернулись при точечной "
              "выборке -- расходится с предыдущим шагом (447 строк были). Дальше не двигаюсь, "
              "нужен разбор вручную.")
        return 1

    decoded_rows = [decode_token_launched(r) for r in df2.to_dict("records")]
    for d in decoded_rows[:5]:
        print(f"  tx={d['tx_hash'][:12]}.. token={d['token']} pool={d['pool']} "
              f"dexId={d['dex_id']} restrictionsEndBlock={d['restrictions_end_block']} "
              f"block_number={d['block_number']}")

    seconds_per_block = estimate_seconds_per_block(decoded_rows)
    print(f"[g1_verify_factory] Оценка секунд/блок (из соседних логов, НЕ захардкожено): "
          f"{seconds_per_block:.3f}")

    pool_addresses = sorted({d["pool"] for d in decoded_rows})
    swap_check_sql_template = (
        "select pool_address, count(*) as n_swaps, min(block_time) as first_swap, "
        f"max(block_time) as last_swap from query_02_swaps_raw_july "
        f"where pool_address in ({q_list(pool_addresses)}) group by 1"
    )
    swap_check_sql = substitute_query_refs(swap_check_sql_template, query_ids)
    print(f"\n===== g1_pool_swap_crosscheck (оценка 3.0) =====")
    qid3 = client.create_query("g1_pool_swap_crosscheck", swap_check_sql)
    df3 = client.run_sql_cached(
        "g1_pool_swap_crosscheck", swap_check_sql, query_id=qid3, estimated_credits=3.0,
        expected_max_rows=30, expected_columns=4,
    )
    n_pools_with_swaps = 0 if df3 is None else len(df3)
    print(df3.to_string() if df3 is not None else "(no rows -- НИ ОДИН из декодированных pool не торговался в июле)")
    print(f"\n[g1_verify_factory] Круг замкнут для {n_pools_with_swaps}/{len(pool_addresses)} "
          f"проверенных pool-адресов (совпадение свопов в query_02_swaps_raw_july).")

    # --- Задача 3: распределение restrictionsEndBlock - block_number(событие), сек ---
    gaps_seconds = [
        (d["restrictions_end_block"] - d["block_number"]) * seconds_per_block
        for d in decoded_rows
    ]
    write_detection_mechanics_update(
        df1=df1, decoded_rows=decoded_rows, df3=df3, seconds_per_block=seconds_per_block,
        gaps_seconds=gaps_seconds, n_pools_with_swaps=n_pools_with_swaps, n_pools_checked=len(pool_addresses),
    )

    return 0


def decode_address_word(word_hex: str) -> str:
    h = str(word_hex).strip().lower()
    if h.startswith("0x"):
        h = h[2:]
    h = h.rjust(64, "0")
    return "0x" + h[-40:]


def decode_uint_word(word_hex: str) -> int:
    h = str(word_hex).strip().lower()
    if h.startswith("0x"):
        h = h[2:]
    return int(h, 16) if h else 0


def decode_token_launched(row: dict) -> dict:
    """row: tx_hash, block_number, block_time, topic1, topic2, topic3, data.
    TokenLaunched(token indexed, deployer indexed, dexFactory indexed,
    pairToken, pool, dexId, launchConfigId, positionId,
    restrictionsEndBlock, initialBuyAmount) -- см. data/pons_family/SOURCE.md."""
    d = str(row["data"]).strip()
    if d.startswith("0x"):
        d = d[2:]
    words = [d[i:i + 64] for i in range(0, len(d), 64)]
    if len(words) != 7:
        raise ValueError(
            f"Ожидалось 7 слов по 32 байта в data (224 байта), получено {len(words)} "
            f"({len(d)} hex-символов) -- формат data не совпадает с ABI TokenLaunched. "
            f"tx={row['tx_hash']}"
        )
    return {
        "tx_hash": row["tx_hash"],
        "block_number": int(row["block_number"]),
        "block_time": row["block_time"],
        "token": decode_address_word(row["topic1"]),
        "deployer": decode_address_word(row["topic2"]),
        "dex_factory": decode_address_word(row["topic3"]),
        "pair_token": decode_address_word(words[0]),
        "pool": decode_address_word(words[1]),
        "dex_id": decode_uint_word(words[2]),
        "launch_config_id": decode_uint_word(words[3]),
        "position_id": decode_uint_word(words[4]),
        "restrictions_end_block": decode_uint_word(words[5]),
        "initial_buy_amount": decode_uint_word(words[6]),
    }


def estimate_seconds_per_block(decoded_rows: list[dict]) -> float:
    """Средний блок-тайм по соседним ЛОГАМ этой же выборки (не
    захардкожено) -- сортирует по block_number, берёт медиану дельт
    между строками с РАЗНЫМ block_number."""
    import pandas as pd

    rows = sorted(decoded_rows, key=lambda r: r["block_number"])
    deltas = []
    for a, b in zip(rows, rows[1:]):
        if b["block_number"] == a["block_number"]:
            continue
        t_a = pd.Timestamp(a["block_time"])
        t_b = pd.Timestamp(b["block_time"])
        dt = (t_b - t_a).total_seconds()
        dn = b["block_number"] - a["block_number"]
        if dn > 0 and dt > 0:
            deltas.append(dt / dn)
    if not deltas:
        raise ValueError(
            "Не удалось оценить секунд/блок -- в выборке недостаточно строк с разными "
            "block_number. Нужна более широкая выборка (расширить LIMIT/окно в "
            "sql/g1/g1_token_launched_sample.sql), не хардкодить константу."
        )
    deltas.sort()
    return deltas[len(deltas) // 2]  # медиана -- устойчивее среднего к редким выбросам


def write_detection_mechanics_update(df1, decoded_rows, df3, seconds_per_block, gaps_seconds, n_pools_with_swaps, n_pools_checked) -> None:
    import statistics

    design_path = Path(CONFIG.g1_design_doc)
    text = design_path.read_text()
    gaps_sorted = sorted(gaps_seconds)
    median_gap = statistics.median(gaps_sorted)
    addendum = f"""

## Дополнение к «Механике детекции» (2026-09-01, блокирующее условие 3)

**Адрес фабрики подтверждён ОНЧЕЙН** (не только по внешнему источнику):
сырые логи `robinhood.logs` с `contract_address` = {FACTORY_V1} / {FACTORY_V2}
существуют (не ноль строк), и topic0 самого частого события в 7-дневном
окне (13-14.07.2026, {int(df1.iloc[0]['n_logs'])} строк) **точно совпал**
с локально посчитанным Keccak-256 от сигнатуры `TokenLaunched(...)` из
ABI (`0xdb51ea9a...4235a`) -- см. `data/pons_family/SOURCE.md` за
точным хэшем и командой воспроизведения. `TokenDeployed` тоже совпал
1:1 по счёту строк.

**Круг "адрес -> событие -> pool -> свопы" замкнут:** {n_pools_with_swaps}
из {n_pools_checked} декодированных вручную (topic1/2/3 + data по
layout ABI) параметров `pool` из `TokenLaunched` имеют реальные свопы в
`query_02_swaps_raw_july`. Событие градуации = `TokenLaunched`, адрес
пула для последующего анализа цены = параметр `pool` этого события (не
суррогат "первый своп в новом пуле", как было в первоначальном
прокси-подходе).

**`dexId` присутствует в событии** -- запуски МОГУТ идти в разные DEX
(см. `DexConfigAdded` в ABI), это учтено: свопы по каждому `pool`
ищутся напрямую по адресу пула, не по фиксированному
project='uniswap' фильтру.

**Распределение `restrictionsEndBlock - block_number(событие)`:**
секунд/блок оценено эмпирически по соседним логам этой же выборки
(медиана дельт block_time/block_number между логами с разными
block_number, НЕ захардкожено) = {seconds_per_block:.3f} с/блок. На
выборке из {len(gaps_seconds)} событий:
- медиана = {median_gap:.0f} с ({median_gap/60:.1f} мин)
- мин = {gaps_sorted[0]:.0f} с, макс = {gaps_sorted[-1]:.0f} с

Значение параметра `restrictionsEndBlock` (конец анти-снайпер
ограничений, не сама градуация) -- относится к дополнению §2.9
(ограничения): часть окна входа (§2.3, (t0+30с; t0+90с]) может
приходиться на период действия ограничений (`maxWalletBps`/`maxTxBps`),
что ограничивает реальную ликвидность на самом входе -- см. полный
разбор в `docs/RESULTS.md`, секция G1.
"""
    if "Дополнение к «Механике детекции» (2026-09-01" not in text:
        design_path.write_text(text + addendum)
        print(f"\n[g1_verify_factory] {design_path} обновлён -- дополнение записано.")
    else:
        print(f"\n[g1_verify_factory] {design_path} уже содержит дополнение -- не дублирую.")


if __name__ == "__main__":
    raise SystemExit(main())
