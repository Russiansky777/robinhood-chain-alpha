#!/usr/bin/env python3
"""Задание владельца, 2026-09-06, п.1 -- ответить до всего остального.

Execution 01M1T353SCDK1QPEQXH7SYG4R9 (query_id=8620150, 238.71 кредита,
191 105 985 строк, 13.76 ГБ) -- можно ли ссылаться на него в новых
запросах как на таблицу (`query_<id>`), и сколько стоит одно чтение
выборки. Тестируем ЭМПИРИЧЕСКИ (Dune API v1 не даёт предварительной
оценки стоимости), максимально дёшево: `LIMIT 1`, 5 колонок, без
фильтров/агрегатов -- чтобы дать движку Dune все шансы не сканировать
материализованный результат целиком, если он это умеет.

ВАЖНАЯ СТРУКТУРНАЯ ОГОВОРКА (до результата, чтобы не создать ложных
ожиданий): исходный SELECT, породивший это query_id, был

    select s.token_address, s.deploy_time, t.block_time, t.amount_usd, qty
    from sampled s join dex.trades t on (OR-условие по двум колонкам) ...

-- `token_address` в результате это адрес из СЛУЧАЙНОЙ ПРЕДВЫБОРКИ (100
токенов), не обязательно реальный контрагент строки `dex.trades`; ни
`tx_hash`, ни `token_bought_address`/`token_sold_address` НЕ сохранены
в выборке. Раз реальный результат (191M строк для 100 токенов, тогда
как честный джойн дал бы на порядки меньше) говорит, что OR-условие
скорее всего не отфильтровало по адресу вовсе -- это, вероятно,
ДУБЛИРОВАННЫЙ (до ~100x) слепок dex.trades за перекрывающиеся 7-дневные
окна каждого из 100 токенов, не чистый "dex.trades за 30 дней". Прямая
переиспользуемость КАК ЕСТЬ (без tx_hash для дедупликации) -- под
вопросом независимо от цены чтения. Проверяем это тоже явно ниже
(дёшево -- фильтр по ОДНОМУ уже известному адресу токена)."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "fomo_forensics_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")

from credit_guard import ensure_namespace, remaining_cycle_budget, load_state  # noqa: E402
from dune_client import DuneClient  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/fomo_forensics_check_query_reuse_result.json")
NAMESPACE_BUDGET = 350.0
STUCK_EXECUTION_ID = "01M1T353SCDK1QPEQXH7SYG4R9"
QUERY_ID = 8620150
# Реальный адрес BUDDY, был частью 100-токенной предвыборки, использован
# в query_8620150 -- знаем это точно (не гадаем), см.
# fomo_forensics_topic0_decode_result.json / verify_erc20.
KNOWN_SAMPLE_TOKEN_NO_0X = "01600e4fb15591452f13c64fb849e70c4d4ac899"


def run() -> int:
    ensure_namespace("fomo_forensics_mozila", NAMESPACE_BUDGET)
    remaining = remaining_cycle_budget(load_state())
    print(f"[reuse_check] остаток общего цикла Dune (Mozila): {remaining:.1f} кредитов")

    client = DuneClient()
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "query_id": QUERY_ID, "execution_id": STUCK_EXECUTION_ID}

    # --- Тест 1: минимально возможное чтение (LIMIT 1, без фильтра) ---
    sql1 = f"select * from query_{QUERY_ID} limit 1"
    qid1 = client.create_query("fomo_check_reuse_limit1", sql1)
    df1 = client.run_sql_cached("fomo_check_reuse_limit1", sql1, query_id=qid1,
                                 estimated_credits=20.0, expected_max_rows=1, expected_columns=5)
    cost1 = next((e["credits"] for e in reversed(client.credit_ledger) if e["name"] == "fomo_check_reuse_limit1"), None)
    print(f"[reuse_check] тест 1 (LIMIT 1, без фильтра): стоимость={cost1}, строки={df1.to_dict('records') if df1 is not None else None}")
    out["test1_limit1_no_filter"] = {"cost_credits": cost1, "sample_row": (df1.to_dict("records")[0] if df1 is not None and not df1.empty else None)}

    remaining_after_1 = remaining_cycle_budget(load_state())
    print(f"[reuse_check] остаток после теста 1: {remaining_after_1:.1f}")
    if remaining_after_1 < 40:
        print("[reuse_check] СТОП: остаток < 40 -- тест 2 (фильтр по known-адресу) рискован, не продолжаем без подтверждения владельца.")
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0

    # --- Тест 2: структурная проверка дублирования -- сколько строк на ОДИН известный токен ---
    sql2 = f"""select token_address, count(*) as n_rows
from query_{QUERY_ID}
where token_address = '{KNOWN_SAMPLE_TOKEN_NO_0X}'
group by token_address"""
    qid2 = client.create_query("fomo_check_reuse_one_token_count", sql2)
    df2 = client.run_sql_cached("fomo_check_reuse_one_token_count", sql2, query_id=qid2,
                                 estimated_credits=20.0, expected_max_rows=5, expected_columns=2)
    cost2 = next((e["credits"] for e in reversed(client.credit_ledger) if e["name"] == "fomo_check_reuse_one_token_count"), None)
    rows2 = df2.to_dict("records") if df2 is not None else []
    print(f"[reuse_check] тест 2 (COUNT для одного известного токена BUDDY): стоимость={cost2}, результат={rows2}")
    out["test2_one_token_rowcount"] = {"cost_credits": cost2, "rows": rows2}

    total_test_cost = (cost1 or 0) + (cost2 or 0)
    out["total_test_cost_credits"] = total_test_cost
    n_rows_for_one_token = rows2[0]["n_rows"] if rows2 else None
    out["n_rows_for_one_token"] = n_rows_for_one_token
    out["structural_conclusion"] = (
        f"{n_rows_for_one_token} строк для ОДНОГО токена (BUDDY) из 191 105 985 общих -- "
        f"{'похоже на реальные ~сотни-тысячи сделок ОДНОГО токена (джойн работал корректно по крайней мере для него)' if n_rows_for_one_token and n_rows_for_one_token < 100_000 else 'подозрительно много для одного мем-токена за 7 дней -- согласуется с гипотезой дублирования/неотфильтрованного джойна'}"
        if n_rows_for_one_token is not None else "не удалось получить (см. rows2)"
    )
    print(f"\n[reuse_check] СТРУКТУРНЫЙ ВЫВОД: {out['structural_conclusion']}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[reuse_check] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
