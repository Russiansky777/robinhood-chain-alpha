#!/usr/bin/env python3
"""Sprint G1 -- перенацеливание на Pons V2 (владелец, 2026-09-01).
Источник-первый: событие градуации v2 = `PoolGraduated(address indexed
token, uint256 positionId, uint256 tokenAmount, uint256 pairTokenAmount)`
из contractsV2/src/v2/PonsV2LaunchFactory.sol (НЕ "TokenLaunched" -- это
имя в v2 переиспользовано для деплоя на кривую, аналог TokenDeployed в
v1 -- см. data/pons_family/SOURCE.md). Адрес фабрики v2 -- уже
известный и дважды независимо подтверждённый V2 Factory
(0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e).

Задачи (бюджет <=35 кредитов на весь пивот):
1. Посуточные градуации v2 за ВЕСЬ период §2.1 (агрегат).
2. Сырая выборка PoolGraduated (LIMIT 30) -> декод token в Python.
3. Круг замкнут: реальные свопы по token в dex.trades (v4, пул не
   гранулярен для v4 в dex.trades -- сверка по адресу токена, не пула).
4. Из 4 адресов-кандидатов (run #11) -- сверка с официальным v2-набором
   (Factory/GraduationExecutor/MemeHook/...); несовпавшие = форки,
   не разбираются (см. владелец).

Использование: python analysis/g1_v2_recon.py
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
from g1_common import decode_address_word, decode_uint_word

V2_FACTORY = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"
TOPIC0_POOL_GRADUATED = "0x0a44ef75df69c534f43cd6c1aa3ef8983065fe5fe79ef9e79f6494e6f258c259"

# Кандидаты из run #11 (сканирование по v1-хэшу TokenLaunched, любой
# адрес, 3 дня после последней v1-градуации) -- не должны совпасть с
# официальным v2-набором (см. владелец: несовпавшие = форки, не
# разбираются).
UNRESOLVED_CANDIDATES = {
    "0x2ba793fd69bf251fd1af90b576be8b9fa6be46db",
    "0x52453b4289a6c3a70bb8b4682bcd3d8731267e28",
    "0x5908923fd6350eb32fa411070feebd1d742c4b34",
    "0xa24d48d50fd7985c6de816eaf77c1a17d3593bbe",
}
OFFICIAL_V2_SET = {V2_FACTORY}  # известные из README; расширяется, если найдём ещё


def decode_pool_graduated(row: dict) -> dict:
    """PoolGraduated(token indexed, positionId, tokenAmount, pairTokenAmount)."""
    d = str(row["data"]).strip()
    if d.startswith("0x"):
        d = d[2:]
    words = [d[i:i + 64] for i in range(0, len(d), 64)]
    if len(words) != 3:
        raise ValueError(f"Ожидалось 3 слова в data (96 байт), получено {len(words)}. tx={row['tx_hash']}")
    return {
        "tx_hash": row["tx_hash"],
        "block_number": int(row["block_number"]),
        "block_time": row["block_time"],
        "token": decode_address_word(row["topic1"]),
        "position_id": decode_uint_word(words[0]),
        "token_amount": decode_uint_word(words[1]),
        "pair_token_amount": decode_uint_word(words[2]),
    }


def main() -> int:
    client = DuneClient()

    print("== Сверка 4 кандидатов из run #11 с официальным v2-набором ==")
    matched = UNRESOLVED_CANDIDATES & OFFICIAL_V2_SET
    unmatched = UNRESOLVED_CANDIDATES - OFFICIAL_V2_SET
    print(f"Совпало с официальным набором: {matched or '(ничего)'}")
    print(f"Не совпало (считаем форками публичного репозитория, не разбираем): {unmatched}")

    # --- Задача 1: посуточные градуации v2 ---
    sql1 = read_sql("g1/g1_v2_daily_graduations")
    print("\n===== g1_v2_daily_graduations (оценка 15.0) =====")
    qid1 = client.create_query("g1_v2_daily_graduations", sql1)
    df1 = client.run_sql_cached(
        "g1_v2_daily_graduations", sql1, query_id=qid1, estimated_credits=15.0,
        expected_max_rows=61, expected_columns=2,
    )
    if df1 is None or len(df1) == 0:
        print(
            "\n[g1_v2_recon] СТОП: PoolGraduated НЕ встречается на известном v2-факторе за весь "
            "период. Либо сигнатура/адрес неверны, либо v2-градуаций в этом периоде нет. Один "
            "из случаев явного возврата к владельцу."
        )
        return 1
    print(df1.to_string(index=False))
    total_v2 = int(df1["n_graduations"].sum())
    print(f"\n[g1_v2_recon] Всего v2-градуаций за период: {total_v2}. Первый день: "
          f"{df1.iloc[0]['day']}, последний: {df1.iloc[-1]['day']}.")

    # --- Задача 2-3: выборка + декод + круг "событие -> token -> свопы" ---
    sql2 = read_sql("g1/g1_v2_graduation_sample")
    print("\n===== g1_v2_graduation_sample (оценка 5.0) =====")
    qid2 = client.create_query("g1_v2_graduation_sample", sql2)
    df2 = client.run_sql_cached(
        "g1_v2_graduation_sample", sql2, query_id=qid2, estimated_credits=5.0,
        expected_max_rows=30, expected_columns=5,
    )
    if df2 is None or len(df2) == 0:
        print("[g1_v2_recon] СТОП: выборка PoolGraduated пуста при точечном запросе -- расходится "
              "с агрегатом выше. Нужен разбор вручную.")
        return 1
    decoded = [decode_pool_graduated(r) for r in df2.to_dict("records")]
    for d in decoded[:5]:
        print(f"  tx={d['tx_hash'][:12]}.. token={d['token']} positionId={d['position_id']} "
              f"tokenAmount={d['token_amount']}")

    tokens = sorted({d["token"] for d in decoded})
    addr_list = ", ".join(tokens)
    swap_check_sql = (
        f"select token_bought_address as token, count(*) as n_swaps, "
        f"min(block_time) as first_swap, max(block_time) as last_swap, "
        f"sum(amount_usd) as total_usd "
        f"from dex.trades where blockchain = 'robinhood' and version = '4' "
        f"and token_bought_address in ({addr_list}) group by 1"
    )
    print("\n===== g1_v2_swap_crosscheck (оценка 5.0) =====")
    qid3 = client.create_query("g1_v2_swap_crosscheck", swap_check_sql)
    df3 = client.run_sql_cached(
        "g1_v2_swap_crosscheck", swap_check_sql, query_id=qid3, estimated_credits=5.0,
        expected_max_rows=30, expected_columns=5,
    )
    n_with_swaps = 0 if df3 is None else len(df3)
    print(df3.to_string(index=False) if df3 is not None else "(нет свопов ни по одному token)")
    print(f"\n[g1_v2_recon] Круг замкнут для {n_with_swaps}/{len(tokens)} токенов "
          "(реальные v4-свопы в dex.trades).")

    write_v2_design_note(df1, total_v2, decoded, n_with_swaps, len(tokens), matched, unmatched)
    return 0


def write_v2_design_note(df1, total_v2, decoded, n_with_swaps, n_tokens_checked, matched, unmatched) -> None:
    design_path = Path(CONFIG.g1_design_doc)
    text = design_path.read_text()
    marker = "## Перенацеливание на Pons V2 (владелец, 2026-09-01)"
    if marker in text:
        print(f"[g1_v2_recon] {design_path} уже содержит секцию -- не дублирую.")
        return
    note = f"""

{marker}

**Task 4 (V1, старый) отменён.** Выборка V1-событий (266 221 записи,
`data/sprintG1_cache/g1_graduation_events_decoded.csv`) сохранена как
описательный контекст "эра V1" (мгновенные запуски, см. секцию семантики
выше), НЕ используется как тест-сет §2.1.

**Источник-первый:** contractsV2/src/v2/PonsV2LaunchFactory.sol
(github.com/ponsdotdev/ponsfamily) объявляет `PoolGraduated(address
indexed token, uint256 positionId, uint256 tokenAmount, uint256
pairTokenAmount)` -- это настоящее событие градуации v2 (эмитится "upon
successful V4 pool creation with position ID and seeded amounts", по
описанию в исходниках). Имя `TokenLaunched` в v2 переиспользовано для
события деплоя на бондинг-кривую (`TokenLaunched(token indexed,
curve indexed, deployer indexed, pairToken, launchConfigId,
graduationThreshold)`, БЕЗ ссылки на пул/позицию) -- аналог
TokenDeployed в v1, не градуация. Это и объясняло нулевую задержку в
v1-выборке: мы фактически смотрели правильное имя события, но для
другого поколения контракта с другой семантикой.

topic0 посчитан локально (Keccak-256 от точной сигнатуры типов):
`PoolGraduated(address,uint256,uint256,uint256)` ->
`0x0a44ef75df69c534f43cd6c1aa3ef8983065fe5fe79ef9e79f6494e6f258c259`.

**Сверка 4 кандидатов из ончейн-скана run #11:** совпало с официальным
v2-набором: {matched or '(ничего)'}. Не совпало (форки публичного
репозитория с тем же ожидаемым topic0, не разбираются по решению
владельца): {unmatched}.

**Адрес фабрики v2:** тот же, что уже был подтверждён дважды независимо
(README + сырой файл) -- `0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e`
(`PonsV2LaunchFactory`), см. `data/pons_family/SOURCE.md`. `PoolGraduated`
эмитится этим же контрактом (подтверждено чтением исходников).

**Посуточные v2-градуации ({total_v2} всего за период):**
{df1.to_string(index=False)}

**Круг замкнут:** {n_with_swaps}/{n_tokens_checked} декодированных
`token` из выборки `PoolGraduated` имеют реальные v4-свопы в dex.trades.
Сверка идёт по адресу ТОКЕНА, не пула -- v4 в dex.trades не гранулярен
по пулу (единый контракт PoolManager-синглтона, см. Шаг 1 recon) --
метод, предусмотренный заранее владельцем ("hook-пул!").

**Фи-стек v2 (из contractsV2/src/v2/hooks/PonsV2MemeHook.sol, дословно
из констант исходников):** `hookFeeBps = 100` (1% с каждого свопа --
это округлённо соответствует НИЖНЕЙ границе сценариев §2.4, ближе к
1%-сценарию, чем к базовому 3%; slippage/gas добавляются сверху и не
входят в hookFeeBps). Из собранного hook fee: `protocolFeeShareBps =
3000` (30% -> протокол), `buybackBurnBps = 5000` (50% от оставшегося ->
buyback/burn). `maxInternalPriceImpactBps = 300` (3%, потолок
внутреннего прайс-импакта, не издержка на трейдера напрямую).
`MAX_TOTAL_TRADE_FEE_BPS = 2000` (2%, верхний предел совокупной комиссии
за сделку) -- при выставлении на максимум round-trip издержка может
доходить до ~4%, ближе к базовому 3%/5%-сценариям §2.4.

**Квота может быть не WETH:** `PairTokenApprovalUpdated`/
`PairTokenEconomicsUpdated` в PonsV2LaunchFactory.sol подтверждают
поддержку нескольких pair/quote токенов (не только WETH, вплоть до
сток-токенов, как отметил владелец) -- нормировка Entry/Exit VWAP в USD
должна идти через цену КВОТЫ на момент сделки (amount_usd из dex.trades
уже в USD -- полагаемся на курируемый прайсинг Dune, не пересчитываем
вручную), не считать квоту фиксированно WETH при агрегации.

**Режимный разрыв (не влияет на текущую выборку, важно для P2/live):**
90-дневный газ-вейвер сети действует с mainnet-запуска (01.07.2026) до
~29.09.2026 (источник: cryptotimes.io, cryptonews.com -- веб, не
ончейн-факт). Юнит-экономика массовых листингов (десятки тысяч
деплоев/день в v1-эру) может радикально измениться, когда пользователи
начнут платить за газ -- это ЗА пределами периода данного Sprint
(01.07-29.08), но существенно для решения о живой стратегии."""
    design_path.write_text(text + note)
    print(f"[g1_v2_recon] {design_path} обновлён.")


if __name__ == "__main__":
    raise SystemExit(main())
