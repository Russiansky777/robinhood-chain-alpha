#!/usr/bin/env python3
"""Владелец: статус "Поинты Lighter" -- давно висящий вопрос ("сколько
LIT распределено по программе поинтов за недели с выплатами 21.08 и
28.08"). См. docs/P4_RECON.md, раздел 4: путь через бесплатные Ethereum
RPC (eth_getLogs по контракту LIT, окна +-1 день вокруг каждой пятницы)
был закрыт 2026-09-02 -- ДВА разных бесплатных шлюза (cloudflare-eth,
publicnode/llamarpc) отказали по ИНФРАСТРУКТУРНЫМ причинам (не по
отсутствию данных): -32603 Internal error и 403 Forbidden, похоже на
блок IP-диапазона GH Actions runner'ов как датацентрового трафика.

Это НЕ проблема "нет данных" -- Ethereum mainnet полностью публичный и
давно закрыт блоками. Проблема была именно в бесплатных RPC-шлюзах.
Dune индексирует Ethereum mainnet как основную сеть (не только Robinhood
Chain) -- тот же путь (Transfer-логи контракта LIT, окна вокруг
21.08/28.08), но через Dune SQL вместо eth_getLogs, обходит именно эту
инфраструктурную стену, а не какой-то другой путь к тем же данным.

Адрес контракта LIT на Ethereum mainnet -- уже верифицирован (CoinGecko
API, docs/P4_RECON.md раздел 1): 0x232ce3bd40fcd6f80f3d55a522d03f25df784ee2.

Распределяющий адрес(а) программы поинтов НЕ известны заранее (не
задокументированы владельцем/на сайте) -- поэтому подход здесь ОБРАТНЫЙ:
не фильтровать по известному адресу отправителя, а агрегировать топ
отправителей LIT в узких временных окнах вокруг каждой известной пятницы
выплаты и посмотреть, не выделяется ли явно один адрес по объёму --
именно так же, как был бы устроен ончейн-поиск через RPC, если бы он
не упал по инфраструктурной причине.

Правило владельца: сначала LIMIT 100 / разведка схемы, дёшево, прежде
чем содержательный запрос. Отдельный, маленький бюджет (namespace
lit_points_mozila) -- не смешивать с funding_mozila_block2 (Query 3 /
скринер), это отдельная задача с отдельным логом трат.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "lit_points_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")

from credit_guard import ensure_namespace, remaining_cycle_budget, load_state
from dune_client import DuneClient

OUT_PATH = Path("data/p3_guard_cache/dune_lit_points_weekly_result.json")

BUDGET = 30.0  # разведочный, отдельный от funding_mozila_block2 (владелец: логировать отдельно)

LIT_CONTRACT = "0x232ce3bd40fcd6f80f3d55a522d03f25df784ee2"  # CoinGecko, docs/P4_RECON.md раздел 1

# Два известных дня выплат (владелец/новости, docs/P4_RECON.md) -- окна +-1 день,
# тот же диапазон, что был в упавшей RPC-попытке.
PAYOUT_FRIDAYS = ["2026-08-21", "2026-08-28"]

PEEK_TOKENS_TRANSFERS_SQL = f"""
SELECT *
FROM tokens.transfers
WHERE blockchain = 'ethereum' AND contract_address = {LIT_CONTRACT}
LIMIT 5
"""

# erc20_ethereum.evt_transfer -- альтернативная таблица, тоже реально
# существует и содержит данные (проверено 2026-09-04, 12 колонок:
# from/to/value/evt_block_time и т.д., value в raw-единицах), НЕ
# используется в run() ниже -- tokens.transfers выбрана как основная
# (amount уже в человекочитаемых единицах, не нужно применять decimals
# вручную). Оставлено как задокументированный, реально проверенный
# запасной путь, если tokens.transfers перестанет отдавать строки.
PEEK_ERC20_EVT_TRANSFER_SQL = f"""
SELECT *
FROM erc20_ethereum.evt_transfer
WHERE contract_address = {LIT_CONTRACT}
LIMIT 5
"""


def top_senders_window_sql(date_str: str) -> str:
    # Реальные имена колонок подтверждены 2026-09-04 через fetch_existing()
    # на уже оплаченном execute (см. analysis/dune_lit_points_recover_peek.py,
    # data/p3_guard_cache/dune_lit_points_recover_peek_result.json) -- НЕ
    # угаданы. tokens.transfers.amount уже в человекочитаемых единицах
    # (не raw, decimals уже применены Dune) -- amount_raw есть отдельно,
    # но не нужен здесь.
    return f"""
SELECT "from" AS sender, count(*) AS n_transfers, sum(amount) AS total_amount_lit
FROM tokens.transfers
WHERE blockchain = 'ethereum' AND contract_address = {LIT_CONTRACT}
  AND block_time >= TIMESTAMP '{date_str} 00:00:00' - INTERVAL '1' DAY
  AND block_time < TIMESTAMP '{date_str} 00:00:00' + INTERVAL '2' DAY
GROUP BY "from"
ORDER BY total_amount_lit DESC
LIMIT 20
"""


def run() -> int:
    ensure_namespace("lit_points_mozila", BUDGET)
    client = DuneClient()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "steps": {}}

    def run_query_step(step_name: str, sql: str, estimated_credits: float, expected_max_rows: int = 100, expected_columns: int = 10) -> None:
        try:
            df = client.run_sql_cached(
                name=step_name, sql=sql, estimated_credits=estimated_credits,
                expected_max_rows=expected_max_rows, expected_columns=expected_columns,
            )
            result["steps"][step_name] = {
                "rows": df.to_dict(orient="records") if df is not None else None,
                "n_rows": len(df) if df is not None else 0,
            }
            print(f"[lit_points] {step_name}: найдено строк {len(df) if df is not None else 0}")
            if df is not None and len(df):
                print(df.to_string())
        except SystemExit as exc:
            result["steps"][step_name] = {"stopped": True, "reason": str(exc)}
            print(f"[lit_points] {step_name} остановлен гвардом: {exc}")
        except Exception as exc:  # noqa: BLE001
            result["steps"][step_name] = {"failed": True, "reason": str(exc)[:2000]}
            print(f"[lit_points] {step_name} УПАЛ (не гвард, реальная ошибка Dune): {exc}")

    # Схема уже подтверждена 2026-09-04 (analysis/dune_lit_points_recover_peek.py,
    # см. data/p3_guard_cache/dune_lit_points_recover_peek_result.json):
    # tokens.transfers -- 22 реальные колонки, amount уже в человекочитаемых
    # единицах. Пики этой ревизии здесь по-прежнему выполняются (для
    # самодостаточности повторного запуска с нуля), но с исправленной
    # декларацией колонок -- не занижаем её вслепую второй раз.
    print("=== 1. Разведка схемы: tokens.transfers (единая таблица трансферов Dune) ===")
    run_query_step("lit_peek_tokens_transfers", PEEK_TOKENS_TRANSFERS_SQL, 2.0, expected_max_rows=5, expected_columns=25)

    print("\n=== 2. Топ отправителей LIT по окнам вокруг известных пятниц выплат (tokens.transfers) ===")
    for d in PAYOUT_FRIDAYS:
        step = f"lit_top_senders_{d}"
        run_query_step(step, top_senders_window_sql(d), 5.0, expected_max_rows=20, expected_columns=5)

    state = load_state()
    ns_spent = state["lit_points_mozila"]["spent"]
    print(f"\n=== Остаток бюджета lit_points_mozila: {BUDGET - ns_spent:.2f} из {BUDGET} ===")
    print(f"=== Остаток цикла (заявленный, не API-подтверждённый): {remaining_cycle_budget():.2f} ===")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[lit_points] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
