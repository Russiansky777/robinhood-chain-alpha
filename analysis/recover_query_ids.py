#!/usr/bin/env python3
"""Восстанавливает query_id для 01/02/03 БЕСПЛАТНО (только status+query
GET, без execute/чтения результата), не полагаясь на query_id_map.json
из actions/cache -- см. docs/COST_POSTMORTEM.md: run #13 (последний
успешный прогон Sprint 1, единственный источник этих execution_id)
выполнялся ДО того, как в run_pipeline.yml появился шаг actions/cache,
так что его query_id_map.json никогда не сохранялся -- умер вместе с
контейнером. Пре-flight проверка Sprint 1.5 (require_cached=True)
поймала это БЕСПЛАТНО (сработала до единого execute), но без этого
скрипта следующий шаг -- либо пересчитать 02 заново (~103-125
кредитов, дорого), либо найти id как-то иначе.

Метод: у нас есть execution_id для 03 и 04@5мин (из лога run #13,
см. SPRINT1_EXECUTIONS ниже). GET /execution/{id}/status (бесплатно)
даёт query_id ИСПОЛНЕНИЯ. GET /query/{query_id} (тоже бесплатно --
метаданные, не результат) даёт query_sql этого запроса -- а в нём,
как текст, УЖЕ подставлены числовые query_<id> ссылки на 01/02
(sql/04_sniper_insider_exclusions.sql ссылается на ОБА через
query_01_pool_creation_blocks/query_02_swaps_raw_july). Регуляркой
достаём эти числа -- и получаем query_id для 01 И 02 бесплатно, без
единого execute.

Записывает результат в analysis/output/cache/query_ids.json под теми
же sha256(rendered_sql) ключами, что использует DuneClient.create_query
-- при условии, что sql/01/02/03.sql и train_start/train_end в
config.py не менялись с run #13 (commit 390fb7a) -- проверено:
`git diff 390fb7a..HEAD -- sql/01_pool_creation_blocks.sql
sql/02_swaps_raw_july.sql sql/03_wallet_agg_july.sql` пуст для самих
SQL-файлов.

Использование:
    python analysis/recover_query_ids.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import CONFIG
from dune_client import DuneClient, render_sql
from run_pipeline import read_sql, q_ts, q_list

# execution_id из лога run #13 (run_pipeline.yml, run id 33436780374,
# commit 390fb7a) -- единственный успешный прогон, где 03/04@5мин были
# реально исполнены на полном июльском периоде.
EXEC_ID_03 = "01M1CSQ10H865136PGXEWBS6KP"
EXEC_ID_04_5M = "01M1CSRT5K746CDMXPJ4B7YS6V"

QUERY_REF_RE = re.compile(r"query_(\d+)")


def main() -> int:
    client = DuneClient()

    print(f"== GET status для execution_id={EXEC_ID_03} (03_wallet_agg_july) ==")
    status_03 = client.get_execution_status(EXEC_ID_03)
    qid_03 = status_03.get("query_id")
    if qid_03 is None:
        print(f"ОШИБКА: query_id отсутствует в статусе: {status_03}", file=sys.stderr)
        return 1
    print(f"  query_id_03 = {qid_03}")

    print(f"== GET status для execution_id={EXEC_ID_04_5M} (04_sniper_insider_exclusions@5мин) ==")
    status_04 = client.get_execution_status(EXEC_ID_04_5M)
    qid_04 = status_04.get("query_id")
    if qid_04 is None:
        print(f"ОШИБКА: query_id отсутствует в статусе: {status_04}", file=sys.stderr)
        return 1
    print(f"  query_id_04_5m = {qid_04}")

    print(f"== GET query definition для query_id={qid_04} (04@5мин) -- достаём ссылки на 01/02 ==")
    defn_04 = client.get_query_definition(qid_04)
    sql_04 = defn_04.get("query_sql", "")
    refs_04 = sorted(set(int(m) for m in QUERY_REF_RE.findall(sql_04)))
    print(f"  Найдены query_<id> ссылки в SQL текста 04: {refs_04}")
    if len(refs_04) != 2:
        print(
            f"ОШИБКА: ожидалось РОВНО 2 ссылки (на 01 и 02) в SQL 04, найдено {len(refs_04)}: {refs_04}. "
            f"Полный текст:\n{sql_04}",
            file=sys.stderr,
        )
        return 1

    print(f"== GET query definition для query_id={qid_03} (03) -- сверка ссылки на 02 ==")
    defn_03 = client.get_query_definition(qid_03)
    sql_03 = defn_03.get("query_sql", "")
    refs_03 = sorted(set(int(m) for m in QUERY_REF_RE.findall(sql_03)))
    print(f"  Найдена query_<id> ссылка в SQL текста 03: {refs_03}")
    if len(refs_03) != 1 or refs_03[0] not in refs_04:
        print(
            f"ПРЕДУПРЕЖДЕНИЕ: ссылка 03->02 ({refs_03}) не совпадает ни с одной ссылкой в 04 ({refs_04}) "
            "-- возможна путаница 01/02. Проверьте вручную перед продолжением.",
            file=sys.stderr,
        )

    # 02 не ссылается ни на что (читает dex.trades напрямую) -- значит
    # из двух id в 04, тот, что совпадает с 03's единственной ссылкой,
    # это 02; оставшийся -- 01.
    qid_02 = refs_03[0] if refs_03 and refs_03[0] in refs_04 else None
    if qid_02 is None:
        print("ОШИБКА: не удалось однозначно определить, какой из id в 04 -- это 02.", file=sys.stderr)
        return 1
    qid_01 = [q for q in refs_04 if q != qid_02][0]
    print(f"  => query_id_02 = {qid_02}, query_id_01 = {qid_01}")

    # ---- считаем те же content-hash ключи, что построит create_query ----
    train_window = {"start_date": q_ts(CONFIG.train_start), "end_date": q_ts(CONFIG.train_end)}
    sql_02_rendered = render_sql(read_sql("02_swaps_raw_july"), train_window)

    sql_01_template = read_sql("01_pool_creation_blocks")
    sql_01_rendered = sql_01_template.replace("query_02_swaps_raw_july", f"query_{qid_02}")

    base_tokens_sql = q_list(list(CONFIG.base_token_symbols))
    sql_03_template = read_sql("03_wallet_agg_july")
    sql_03_rendered = render_sql(
        sql_03_template.replace("query_02_swaps_raw_july", f"query_{qid_02}"),
        {"base_token_symbols": base_tokens_sql},
    )

    import hashlib
    import json
    import subprocess

    entries = {
        hashlib.sha256(sql_02_rendered.encode()).hexdigest(): qid_02,
        hashlib.sha256(sql_01_rendered.encode()).hexdigest(): qid_01,
        hashlib.sha256(sql_03_rendered.encode()).hexdigest(): qid_03,
    }

    # ПОСТОЯННЫЙ файл (не analysis/output/*.json -- тот в .gitignore, см.
    # dune_client.py). Коммитим прямо здесь, а не полагаемся на финальный
    # шаг workflow -- тот же принцип, что и credit_guard.py: важные факты
    # не должны ждать конца прогона, чтобы попасть в git.
    permanent_file = Path("data") / "query_ids_recovered.json"
    existing = {}
    if permanent_file.exists():
        try:
            existing = json.loads(permanent_file.read_text())
        except (json.JSONDecodeError, OSError):
            existing = {}
    existing.update(entries)
    permanent_file.parent.mkdir(parents=True, exist_ok=True)
    permanent_file.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    print(f"\n[recover_query_ids] Записано в {permanent_file}: {entries}")

    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=False)
        subprocess.run(
            ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=False
        )
        subprocess.run(["git", "add", str(permanent_file)], check=False)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
        if diff.returncode != 0:
            subprocess.run(
                ["git", "commit", "-m", "Recover Sprint 1 query_ids for 01/02/03 [automated, 0 credits]"],
                check=False,
            )
            subprocess.run(["git", "push"], check=False)
            print("[recover_query_ids] Закоммичено и запушено.")
        else:
            print("[recover_query_ids] Без изменений (уже актуально) -- коммит не нужен.")
    except Exception as exc:
        print(f"[recover_query_ids] ПРЕДУПРЕЖДЕНИЕ: не удалось закоммитить: {exc}", file=sys.stderr)

    print("[recover_query_ids] Готово, 0 кредитов потрачено (только status+query GET).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
