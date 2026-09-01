"""Тонкая обёртка над Dune API v1: создание/выполнение запроса, поллинг
статуса, получение результатов, дисковый кэш и явный, некрасивый отказ
при исчерпании кредитов/лимитов — вместо тихих ретраев.

Намеренно не использую пакет `dune-client`, чтобы не тянуть лишнюю
зависимость с непроверенным (на момент написания, без сетевого доступа)
API — здесь голый `requests` поверх задокументированных REST-эндпоинтов
Dune API v1.

ВАЖНО (см. CHANGELOG в docs/RESULTS.md / commit history): изначально
здесь была попытка использовать нативный механизм параметров Dune
(`{{param}}` в SQL + `parameters` при создании запроса + `query_parameters`
при выполнении). Первый реальный прогон на Dune упал с 400: Dune требует
явно объявлять КАЖДЫЙ `{{param}}`, использованный в SQL, в теле create-
запроса — иначе "the following keys in the query do not have matching
parameters". Вместо того чтобы разбираться в точном формате объявления
параметров (типы/квотинг различаются для date/number/enum и не были
проверяемы без реального аккаунта), переключился на подстановку значений
в текст SQL на стороне Python ДО отправки в Dune (см.
`render_sql`/`run_pipeline.py`) — в финальном SQL, который видит Dune,
плейсхолдеров `{{...}}` уже не остаётся, так что декларировать нечего.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pandas as pd
import requests

from config import CONFIG
from credit_guard import (
    check_before_execute,
    check_before_read,
    check_overrun_after_execute,
    check_sql_sanity,
    namespace as credit_guard_namespace,
    record_execution,
    record_read,
    DEFAULT_ESTIMATE,
)

API_BASE = "https://api.dune.com/api/v1"

# Архитектурный принцип «сырые данные не покидают Dune» (Sprint 1.5,
# ревизия 2 -- см. docs/COST_POSTMORTEM.md): страховка от регрессии --
# если вызывающий код объявляет expected_max_rows выше этого порога без
# явного override, это почти наверняка означает попытку читать
# построчный результат вместо агрегата/сводки, что и стоило 163.98
# кредита за одно чтение 03. По умолчанию отказываем.
DEFAULT_MAX_SAFE_READ_ROWS = 20_000


class DuneCreditsExhausted(RuntimeError):
    """Dune вернул 402/явный сигнал 'кредиты закончились'."""


class DuneRateLimited(RuntimeError):
    """Dune вернул 429."""


def render_sql(template: str, values: dict[str, str]) -> str:
    """Простая текстовая подстановка `{{key}}` -> значение (уже
    правильно отформатированное/заквоченное вызывающей стороной — см.
    run_pipeline.py: q_ts/q_list). Никакого похода в Dune для этого не
    требуется, и на выходе в SQL не остаётся `{{...}}` — значит и Dune
    нечего объявлять как параметры.
    """
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template


class DuneClient:
    def __init__(self, api_key: str | None = None, cache_dir: str | None = None):
        self.api_key = api_key or CONFIG.dune_api_key
        if not self.api_key:
            raise RuntimeError(
                "DUNE_API_KEY не задан. Заполните .env (см. .env.example) "
                "или переменную окружения DUNE_API_KEY перед запуском."
            )
        self.session = requests.Session()
        self.session.headers.update({"X-Dune-API-Key": self.api_key})
        self.cache_dir = Path(cache_dir or CONFIG.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.executions_this_run = 0
        # Леджер фактической стоимости каждого запроса этого прогона —
        # для смоук-теста ("запрос -> кредиты", см. run_pipeline.py и
        # docs/DATA_ACCESS.md). Один элемент на вызов run_sql_cached,
        # включая кэш-хиты (стоимость 0, но видно, что запрос был).
        self.credit_ledger: list[dict] = []

        # Персистентная память query_id по содержимому SQL (sha256 ->
        # query_id). БЕЗ этого create_query создавал новый query_id на
        # КАЖДЫЙ вызов, даже когда SQL не менялся -- в Sprint 1 это
        # привело к тому, что 03_wallet_agg_july пересчитывался ДВАЖДЫ
        # за один прогон (~25 кредитов впустую): ссылающийся на него
        # query_02 каждый раз получал новый id, из-за чего рендер SQL
        # для 03 менялся байт-в-байт, и локальный кэш результатов (по
        # хэшу ИТОГОВОГО SQL) не совпадал, хотя семантика была той же.
        # Теперь query_id стабилен, пока не меняется сам SQL-текст (без
        # учёта query_<id>-ссылок, см. create_query). Файл лежит в
        # cache_dir и переживает прогоны через actions/cache (см.
        # .github/workflows/*.yml) — но даже без него теперь всегда
        # печатается в лог (см. create_query/run_sql_cached), так что
        # id не теряются безвозвратно даже при потере кэша контейнера.
        self.query_id_map_file = self.cache_dir / "query_ids.json"
        self.query_id_map: dict[str, int] = {}
        if self.query_id_map_file.exists():
            try:
                self.query_id_map = json.loads(self.query_id_map_file.read_text())
            except (json.JSONDecodeError, OSError):
                self.query_id_map = {}

        # analysis/output/*.json попадает в .gitignore -- эфемерный кэш,
        # переживает только через actions/cache (и то не всегда, см.
        # docs/COST_POSTMORTEM.md: run #13 выполнился ДО того, как этот
        # шаг появился в workflow, поэтому его query_ids.json умер вместе
        # с контейнером без следа). data/query_ids_recovered.json --
        # ПОСТОЯННОЕ, закоммиченное дополнение: id, однажды восстановленные
        # бесплатным способом (см. analysis/recover_query_ids.py) и больше
        # никогда не теряющиеся. Читается здесь, поверх эфемерного кэша.
        self.permanent_query_id_map_file = Path("data") / "query_ids_recovered.json"
        if self.permanent_query_id_map_file.exists():
            try:
                self.query_id_map.update(json.loads(self.permanent_query_id_map_file.read_text()))
            except (json.JSONDecodeError, OSError):
                pass

    def _save_query_id_map(self) -> None:
        self.query_id_map_file.write_text(json.dumps(self.query_id_map))

    def _commit_permanent(self, path: Path, message: str) -> None:
        """Коммитит и пушит немедленно -- см. ревизия 4 пост-мортема:
        actions/cache пропускает свой post-save шаг, если джоб завершился
        с ненулевым кодом (а BudgetGuardStop всегда так выходит по
        дизайну), из-за чего уже оплаченные результаты (например,
        03b_cohort_selection в run #2) терялись и пересчитывались заново
        в следующей попытке. Постоянный файл в data/ с коммитом сразу
        после записи не зависит от того, чем закончится джоб дальше."""
        import subprocess

        try:
            subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=False)
            subprocess.run(
                ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=False
            )
            subprocess.run(["git", "add", str(path)], check=False)
            diff = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
            if diff.returncode == 0:
                return
            subprocess.run(["git", "commit", "-m", message], check=False)
            subprocess.run(["git", "push"], check=False)
        except Exception as exc:
            print(f"[dune] ПРЕДУПРЕЖДЕНИЕ: не удалось закоммитить {path}: {exc}")

    # ---------- низкоуровневые вызовы ----------

    def _post(self, path: str, **kwargs) -> dict:
        resp = self.session.post(f"{API_BASE}{path}", timeout=60, **kwargs)
        return self._handle_response(resp)

    def _get(self, path: str) -> dict:
        resp = self.session.get(f"{API_BASE}{path}", timeout=60)
        return self._handle_response(resp)

    def _handle_response(self, resp: requests.Response) -> dict:
        if resp.status_code == 402:
            # Тело ответа раньше отбрасывалось -- реальный текст ошибки от
            # Dune (например, "требуется Pro план для этого датасета" vs
            # "недостаточно кредитов") виден только здесь. Добавлено после
            # второго 402 подряд на dex.trades, отфильтрованном по одному
            # дню -- сумма кредитов была явно не в лимите, значит причина
            # не "дорогой скан", и без текста тела это не расследовать.
            raise DuneCreditsExhausted(
                "Dune вернул 402 Payment Required — кредиты free tier "
                "(2500/мес) исчерпаны или запрос слишком дорогой для "
                "текущего плана. НЕ ретраю автоматически. Проверьте баланс "
                "на dune.com/settings/billing и рассмотрите переход на "
                "Alchemy fallback (analysis/alchemy_fallback.py), см. "
                f"docs/DATA_ACCESS.md.\nТело ответа Dune: {resp.text[:1000]}"
            )
        if resp.status_code == 429:
            raise DuneRateLimited(
                "Dune вернул 429 Too Many Requests. Не ретраю молча — "
                "подождите и перезапустите вручную, либо снизьте частоту "
                "вызовов в run_pipeline.py."
            )
        if not resp.ok:
            raise RuntimeError(f"Dune API error {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    # ---------- публичный API ----------

    def create_query(self, name: str, sql: str, require_cached: bool = False) -> int:
        """Создаёт сохранённый запрос на Dune. `sql` должен быть уже
        полностью отрендерен (без `{{...}}`) — см. render_sql выше.
        Возвращает query_id.

        is_private=False (не True, как было изначально): наши запросы
        активно ссылаются друг на друга через `query_<id>`
        (01 -> 02, 03/04 -> 01/02, 05 -> 03/04, см. sql/00_notes.md) — а
        Dune free tier отказывает в этом для приватных запросов:
        "querying private queries is an advanced feature included only
        in our enterprise subscription plans". Публичность здесь значит
        только "виден в списке запросов на dune.com" -- сама SQL-логика
        и так уже открыта в этом репозитории; секретов/данных запрос не
        содержит, только SQL-текст.

        require_cached=True: жёсткий отказ, если SQL не найден в
        query_id_map (т.е. пришлось бы создавать НОВЫЙ query, у
        которого ещё нет исполненного результата). Используется в
        pre-flight проверке Sprint 1.5 ревизии 2: 03b/03c/03d
        ссылаются на query_01/02/03 через query_<id>, рассчитывая на
        УЖЕ материализованный на Dune результат Sprint 1 -- если кэш
        query_ids.json холодный, create_query тихо создал бы новый
        query без результата, и ссылка на него сломалась бы только при
        исполнении 03b (после того как заплатили за него). Лучше упасть
        здесь, до единого платного вызова.
        """
        content_hash = hashlib.sha256(sql.encode()).hexdigest()
        cached_qid = self.query_id_map.get(content_hash)
        if cached_qid is not None:
            print(f"[dune] query_id переиспользован для '{name}': {cached_qid} (без нового create_query)")
            return cached_qid
        if require_cached:
            raise RuntimeError(
                f"[dune] СТОП: query_id для '{name}' не найден в query_id_map.json (кэш "
                "холодный или это первый раз). Требовался УЖЕ материализованный результат "
                "Sprint 1 без нового execute -- см. docs/COST_POSTMORTEM.md, ревизия 2, "
                "pre-flight проверка. Ничего не заплачено. Нужно либо восстановить кэш "
                "(actions/cache с префиксом 'dune-cache-'), либо явно пересчитать 02/01/03 "
                "заново (дорого, требует отдельного решения) -- не делаю это молча."
            )

        body = {"name": name, "query_sql": sql, "is_private": False}
        result = self._post("/query", json=body)
        qid = result["query_id"]
        print(f"[dune] создан query_id={qid} для '{name}'")
        self.query_id_map[content_hash] = qid
        self._save_query_id_map()
        return qid

    def get_execution_status(self, execution_id: str) -> dict:
        return self._get(f"/execution/{execution_id}/status")

    def get_query_definition(self, query_id: int) -> dict:
        """GET /query/{id} -- метаданные сохранённого запроса, включая
        query_sql. Метаданные, не результат исполнения -- не биллится
        (тот же паттерн, что create_query: 0 кредитов на все READ-only
        операции метаданных запроса, только execute() и чтение
        результата платные, см. docs/COST_POSTMORTEM.md). Используется
        для восстановления query_id 01/02 из уже оплаченного execution_id
        04/05 (их SQL содержит query_<id> ссылки на 01/02) без единого
        нового execute -- см. analysis/recover_query_ids.py."""
        return self._get(f"/query/{query_id}")

    def fetch_existing(
        self, execution_id: str, name: str = "unnamed", expected_max_rows: int = DEFAULT_MAX_SAFE_READ_ROWS
    ) -> tuple[pd.DataFrame, dict, dict]:
        """Читает результаты УЖЕ СУЩЕСТВУЮЩЕГО execution_id (например, из
        предыдущего прогона, найденного в логах CI) -- без create_query/
        execute, только status (бесплатно) + results (ПЛАТНО по объёму,
        см. get_results_df/credit_guard.py). НЕ используется в текущей
        Sprint 1.5 ревизии 2 для 03/04 (их полные результаты слишком
        большие -- см. docs/COST_POSTMORTEM.md), оставлено для мелких
        восстановлений при необходимости.
        """
        status = self.get_execution_status(execution_id)
        if status.get("state") != "QUERY_STATE_COMPLETED":
            raise RuntimeError(f"execution {execution_id} не в состоянии COMPLETED: {status}")
        df, result_stats = self.get_results_df(execution_id, name=name, expected_max_rows=expected_max_rows)
        return df, status, result_stats

    def execute(
        self, query_id: int, name: str = "unnamed", estimated_credits: float | None = None, sql: str | None = None
    ) -> str:
        # Бюджетный гард (см. analysis/credit_guard.py, docs/COST_POSTMORTEM.md):
        # проверка ПЕРЕД КАЖДЫМ execute -- жёсткий exit при нарушении лимита
        # Sprint 1.5 или внешней границы биллинг-цикла. Сюда идут ВСЕ вызовы
        # execute() в проекте, включая прямые (не только через
        # run_sql_cached), поэтому гейт стоит именно здесь.
        #
        # Санитарная проверка (ревизия 3, см. docs/COST_POSTMORTEM.md) --
        # ДО проверки бюджета и НЕЗАВИСИМО от остатка лимита: оценка >40
        # кредитов или структурный риск (UNION ALL + тяжёлый источник,
        # паттерн, что дал 144 кредита вместо 8 в 03c) -- жёсткий стоп.
        if sql is not None:
            check_sql_sanity(name, sql, estimated_credits if estimated_credits is not None else DEFAULT_ESTIMATE)
        check_before_execute(name, estimated_credits)
        self.executions_this_run += 1
        # ПОПЫТКА (2026-08-31) явно запросить performance: "small" провалилась
        # немедленным 400: "This performance tier is not available with your
        # subscription. Please upgrade..." -- то есть на free tier можно
        # исполнять ТОЛЬКО дефолтный уровень (Dune называет его "medium",
        # но для free-аккаунта явно указывать его тоже не нужно/нельзя —
        # сама попытка передать performance что-либо, видимо, уже требует
        # платного плана). Возвращено к дефолту: поле не передаётся вовсе.
        result = self._post(f"/query/{query_id}/execute", json={"query_parameters": {}})
        return result["execution_id"]

    def poll_until_done(self, execution_id: str, timeout_s: int = 1800, interval_s: int = 5) -> dict:
        # timeout_s поднят с 600 до 1800 (30 мин): реальный прогон на полном
        # месяце данных (июль, ~30x больше смоук-дня) не уложился в 10 минут
        # на query 02_swaps_raw_july -- TimeoutError, не ошибка исполнения
        # (запрос продолжал висеть в состоянии QUERY_STATE_EXECUTING, не
        # свалился). Смоук на 1 день при этом целиком прошёл за ~21.7
        # кредита, так что это именно вопрос времени выполнения на Dune, не
        # стоимости.
        waited = 0
        while waited < timeout_s:
            status = self._get(f"/execution/{execution_id}/status")
            state = status.get("state")
            if state == "QUERY_STATE_COMPLETED":
                return status
            if state in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
                raise RuntimeError(f"Dune execution {execution_id} завершился со статусом {state}: {status}")
            time.sleep(interval_s)
            waited += interval_s
        raise TimeoutError(f"Dune execution {execution_id} не завершился за {timeout_s}s")

    def get_results_df(
        self,
        execution_id: str,
        name: str = "unnamed",
        expected_max_rows: int = DEFAULT_MAX_SAFE_READ_ROWS,
        expected_columns: int = 10,
    ) -> tuple[pd.DataFrame, dict]:
        """Скачивает результат execution через /execution/.../results --
        ЭТО ПЛАТНАЯ ОПЕРАЦИЯ, биллится по объёму данных отдельно от
        execute() (см. credit_guard.py, ревизия 2 гарда). Гейт ПЕРЕД
        запросом использует `expected_max_rows`/`expected_columns`,
        объявленные вызывающей стороной (у нас нет дешёвого способа
        узнать реальный размер результата ДО оплаты за его чтение) --
        см. docs/COST_POSTMORTEM.md. `expected_max_rows` по умолчанию
        20k -- архитектурный принцип «сырые данные не покидают Dune»:
        любой вызов, объявляющий больше, почти наверняка ошибка
        (попытка читать построчный результат вместо сводки).
        """
        estimate = check_before_read(name, expected_max_rows, expected_columns)
        result = self._get(f"/execution/{execution_id}/results")
        rows = result.get("result", {}).get("rows", [])
        stats = result.get("result", {}).get("metadata", {})
        actual_rows = stats.get("total_row_count", len(rows))
        actual_cols = len(stats.get("column_names", [])) or expected_columns
        print(
            f"[dune] execution {execution_id}: "
            f"{actual_rows} rows, "
            f"{stats.get('datapoint_count', 'n/a')} datapoints"
        )
        if actual_rows > expected_max_rows:
            print(
                f"[credit_guard] ПРЕДУПРЕЖДЕНИЕ: '{name}' вернул {actual_rows} строк, "
                f"что БОЛЬШЕ заявленного expected_max_rows={expected_max_rows} -- оценка "
                f"{estimate:.2f} была занижена, реальная стоимость этого чтения выше. "
                "Пересмотрите SQL/вызов -- это симптом того же паттерна, что вызвал "
                "163.98-кредитное чтение 03 в Sprint 1.5 ревизии 1."
            )
        record_read(name, estimate, actual_rows, actual_cols, execution_id)
        return pd.DataFrame(rows), stats

    def run_sql_cached(
        self,
        name: str,
        sql: str,
        query_id: int | None = None,
        force_refresh: bool = False,
        fetch_results: bool = True,
        estimated_credits: float | None = None,
        expected_max_rows: int = DEFAULT_MAX_SAFE_READ_ROWS,
        expected_columns: int = 10,
    ) -> pd.DataFrame | None:
        """Выполняет (или переиспользует сохранённый query_id) уже
        полностью отрендеренный запрос, с дисковым кэшем по (name, sql) —
        повторный прогон пайплайна не пережигает кредиты повторно, пока
        текст запроса не поменялся. Каждый вызов (кэш-хит или реальное
        исполнение) пишет запись в self.credit_ledger с фактической
        стоимостью в кредитах, взятой из ответа Dune (`execution_cost_credits`
        в статусе исполнения) — см. docs/DATA_ACCESS.md, "Смоук-тест".

        `fetch_results=False` -- запрос исполняется (нужно для
        материализации, чтобы на него могли ссылаться другие запросы
        через `query_<id>`, и чтобы получить реальную стоимость в
        кредитах), но результат НЕ скачивается через `/execution/.../
        results`. Обязательно для шагов, чей DataFrame не используется в
        Python напрямую (только как cross-query ссылка) -- см.
        docs/DATA_ACCESS.md, "1.8 GiB на одном дне свопов": сырые свопы
        даже за один день оказались слишком большими для нефрагментированной
        выгрузки (Dune вернул 400 "Result is too large... use pagination"),
        а нам эти строки в Python и не нужны -- 01/03/04 обращаются к ним
        через `query_02_swaps_raw_july` прямо на стороне Dune.
        """
        cache_key = hashlib.sha256(sql.encode()).hexdigest()[:16]
        # CSV, не parquet: избегаем зависимости от pyarrow/fastparquet
        # (первый реальный прогон в CI упал именно на этом — все 5 Dune-
        # запросов честно выполнились и вернули строки, но кэш-запись
        # рухнула ПОСЛЕ, из-за чего результат потерялся). Датафреймы
        # здесь маленькие (агрегаты по кошелькам), CSV более чем годится.
        cache_file = self.cache_dir / f"{name}_{cache_key}.csv"
        marker_file = self.cache_dir / f"{name}_{cache_key}.done"
        # ПОСТОЯННЫЙ, закоммиченный кэш -- см. docs/COST_POSTMORTEM.md,
        # ревизия 4: actions/cache пропускает сохранение, если джоб
        # завершился с ненулевым кодом (а BudgetGuardStop именно так и
        # выходит по дизайну) -- это заставило 03b_cohort_selection
        # оплачиваться ПОВТОРНО в run #3 (уже успешно посчитан в run #2,
        # но эфемерный кэш умер вместе с проваленным джобом). Постоянный
        # файл в data/ переживает это безусловно, как и query_ids_recovered.json.
        # Директория по бюджетному пространству (см. credit_guard.py) --
        # "sprint15" -> data/sprint15_cache/, "sprintG1" -> data/sprintG1_cache/,
        # так каждый спринт получает свой постоянный кэш, не смешиваясь.
        permanent_dir = Path(f"data/{credit_guard_namespace()}_cache")
        permanent_cache_file = permanent_dir / f"{name}_{cache_key}.csv"
        permanent_marker_file = permanent_dir / f"{name}_{cache_key}.done"

        if fetch_results and permanent_cache_file.exists() and not force_refresh:
            print(f"[dune] ПОСТОЯННЫЙ кэш-хит: {permanent_cache_file}")
            self.credit_ledger.append({"name": name, "credits": 0.0, "cached": True})
            return pd.read_csv(permanent_cache_file)
        if not fetch_results and permanent_marker_file.exists() and not force_refresh:
            print(f"[dune] ПОСТОЯННЫЙ кэш-хит (materialize-only): {permanent_marker_file}")
            self.credit_ledger.append({"name": name, "credits": 0.0, "cached": True})
            return None
        if fetch_results and cache_file.exists() and not force_refresh:
            print(f"[dune] cache hit: {cache_file.name}")
            self.credit_ledger.append({"name": name, "credits": 0.0, "cached": True})
            return pd.read_csv(cache_file)
        if not fetch_results and marker_file.exists() and not force_refresh:
            print(f"[dune] cache hit (materialize-only): {marker_file.name}")
            self.credit_ledger.append({"name": name, "credits": 0.0, "cached": True})
            return None

        qid = query_id or self.create_query(name=name, sql=sql)
        execution_id = self.execute(qid, name=name, estimated_credits=estimated_credits, sql=sql)
        # Печатаем ВСЕГДА, до поллинга -- если что-то дальше упадёт
        # (таймаут, 402 на следующем шаге и т.п.), id всё равно попадёт
        # в лог CI и его можно будет вручную переиспользовать через
        # fetch_existing(), как сделано в analysis/recover_sprint1.py.
        print(f"[dune] {name}: query_id={qid} execution_id={execution_id}")
        try:
            status = self.poll_until_done(execution_id)
        except TimeoutError:
            # Реальная причина run #11 из пост-мортема: execute() уже
            # запустил дорогой запрос на Dune, но наш поллинг сдался раньше
            # исполнения -- запрос мог продолжить исполняться (и списывать
            # кредиты) на стороне Dune, невидимо для нашего леджера. Пишем
            # запись СРАЗУ как "неизвестно", коммитим -- не ждём конца
            # прогона (его может и не быть).
            record_execution(
                f"{name} [ТАЙМАУТ поллинга -- проверьте {execution_id} на dune.com]", None, execution_id,
                estimated_credits=estimated_credits, failure_reason="таймаут поллинга, статус на Dune неизвестен",
            )
            raise
        except RuntimeError as exc:
            # FAILED/CANCELLED -- перезапрашиваем статус (execution_cost_credits
            # в нашем опыте был 0 в этих случаях, но не полагаемся на это
            # без проверки) и фиксируем реальную стоимость, если она есть.
            # Оценку и текст причины падения тоже пишем в леджер (не только
            # факт=0) -- нулевые попытки должны быть прослеживаемы: операция
            # -> оценка -> факт=0 -> причина.
            try:
                failed_status = self.get_execution_status(execution_id)
                failed_cost = failed_status.get("execution_cost_credits")
            except Exception:
                failed_cost = None
            record_execution(
                f"{name} [FAILED]", failed_cost, execution_id,
                estimated_credits=estimated_credits, failure_reason=str(exc)[:500],
            )
            raise
        cost = status.get("execution_cost_credits")
        estimate_used = estimated_credits if estimated_credits is not None else DEFAULT_ESTIMATE

        if not fetch_results:
            self.credit_ledger.append({"name": name, "credits": cost, "cached": False})
            print(f"[dune] {name}: {cost if cost is not None else 'n/a'} credits (материализован, результат не скачивался)")
            marker_file.write_text(execution_id)
            permanent_marker_file.parent.mkdir(parents=True, exist_ok=True)
            permanent_marker_file.write_text(execution_id)
            self._commit_permanent(permanent_marker_file, f"{permanent_dir.name}: материализован '{name}' [automated]")
            record_execution(name, cost, execution_id)
            # Пост-хок "факт > вдвое оценки" (ревизия 3, см.
            # docs/COST_POSTMORTEM.md) -- ПОСЛЕ того, как результат уже
            # закоммичен постоянно и факт попал в credits_spent.json, даже
            # если дальше стоп -- деньги и результат не теряются вместе.
            check_overrun_after_execute(name, estimate_used, cost)
            return None

        df, result_stats = self.get_results_df(
            execution_id, name=name, expected_max_rows=expected_max_rows, expected_columns=expected_columns
        )
        if cost is None:
            cost = result_stats.get("execution_cost_credits")
        self.credit_ledger.append({"name": name, "credits": cost, "cached": False})
        print(f"[dune] {name}: {cost if cost is not None else 'n/a'} credits")
        df.to_csv(cache_file, index=False)
        permanent_cache_file.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(permanent_cache_file, index=False)
        self._commit_permanent(permanent_cache_file, f"{permanent_dir.name}: результат '{name}' ({len(df)} строк) [automated]")
        record_execution(name, cost, execution_id)
        check_overrun_after_execute(name, estimate_used, cost)
        return df
