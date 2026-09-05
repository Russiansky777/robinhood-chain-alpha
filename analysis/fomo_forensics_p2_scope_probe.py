#!/usr/bin/env python3
"""Задача «форензика fomo», п.2 (базовая ставка) -- разведка масштаба
ПЕРЕД тем, как проектировать полный запрос по цене входа/максимуму за
7 дней. Реальный topic0 `TokenLaunched` определён структурно
(docs/PROJECT_STATE.md, "Уже сделано..."): `0x67226bacc...`, но ТОЛЬКО
строки формы (a) -- `topic2 is not null AND data ~= totalSupply-масштаб`
(порог >= 1e26, реальный totalSupply у всех проверенных токенов = 1e27,
запас на разные конфигурации децималов/supply) -- форма (b) того же
topic0 (topic1=0x0, переменный data) -- не деплой, отдельный сигнал,
исключаем явно.

Не тянем построчно тысячи логов -- считаем ТОЛЬКО агрегат (число
уникальных токенов, диапазон времени) за 250 кредитов бюджета. Если N
окажется огромным (десятки тысяч) -- полный расчёт цены-на-час-1/макс-
за-7-дней потребует отдельного, тщательно спроектированного джойна с
`dex.trades`, не в лоб построчно."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "fomo_forensics")

from credit_guard import ensure_namespace, remaining_cycle_budget, load_state  # noqa: E402
from dune_client import DuneClient  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/fomo_forensics_p2_scope_probe_result.json")
BUDGET = 250.0

LAUNCH_CONTRACTS = [
    "0x0000ffffbe8efe702c8703ae3477ff5de3d319c0",
    "0x00004c4ccc709ef590f7c81102c0689f0263d4e9",
]
REAL_TOKEN_LAUNCHED_TOPIC0 = "67226bacccef969dab310a9e55dc1cf821363658e433fd330344f5cc00c79ac8"


def run() -> int:
    ensure_namespace("fomo_forensics", BUDGET)
    remaining = remaining_cycle_budget(load_state())
    print(f"[p2_scope] остаток общего цикла Dune: {remaining:.1f} кредитов")

    client = DuneClient()
    addrs_sql = ", ".join(f"'{a[2:].lower()}'" for a in LAUNCH_CONTRACTS)

    # bigint в Trino ограничен ~9.2e18 -- totalSupply-масштаб (1e27) в
    # data превышает диапазон bigint, приходится сравнивать как varbinary
    # длину/префикс, а не численно кастовать. Проверяем структурно: форма
    # (a) отличается от формы (b) тем, что topic2 НЕ пустой -- этого
    # достаточно как фильтр, без арифметики над data.
    sql = f"""select count(*) as n_launch_rows,
    count(distinct topic1) as n_distinct_tokens,
    min(block_time) as first_seen, max(block_time) as last_seen,
    min(block_number) as min_block, max(block_number) as max_block
from robinhood.logs
where lower(to_hex(contract_address)) in ({addrs_sql})
    and lower(to_hex(topic0)) = '{REAL_TOKEN_LAUNCHED_TOPIC0}'
    and topic2 is not null
    and block_time >= now() - interval '30' day
limit 1"""
    qid = client.create_query("fomo_forensics_p2_scope_probe", sql)
    df = client.run_sql_cached("fomo_forensics_p2_scope_probe", sql, query_id=qid,
                                estimated_credits=10.0, expected_max_rows=1, expected_columns=6)
    rows = df.to_dict("records") if df is not None else []
    result = rows[0] if rows else {}
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))

    out = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "scope": result}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[p2_scope] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
