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
import time
from pathlib import Path

import pandas as pd
import requests

from config import CONFIG

API_BASE = "https://api.dune.com/api/v1"


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

    def create_query(self, name: str, sql: str) -> int:
        """Создаёт сохранённый запрос на Dune. `sql` должен быть уже
        полностью отрендерен (без `{{...}}`) — см. render_sql выше.
        Возвращает query_id.
        """
        body = {"name": name, "query_sql": sql, "is_private": True}
        result = self._post("/query", json=body)
        return result["query_id"]

    def execute(self, query_id: int) -> str:
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

    def poll_until_done(self, execution_id: str, timeout_s: int = 600, interval_s: int = 3) -> dict:
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

    def get_results_df(self, execution_id: str) -> tuple[pd.DataFrame, dict]:
        result = self._get(f"/execution/{execution_id}/results")
        rows = result.get("result", {}).get("rows", [])
        stats = result.get("result", {}).get("metadata", {})
        print(
            f"[dune] execution {execution_id}: "
            f"{stats.get('total_row_count', len(rows))} rows, "
            f"{stats.get('datapoint_count', 'n/a')} datapoints"
        )
        return pd.DataFrame(rows), stats

    def run_sql_cached(self, name: str, sql: str, query_id: int | None = None, force_refresh: bool = False) -> pd.DataFrame:
        """Выполняет (или переиспользует сохранённый query_id) уже
        полностью отрендеренный запрос, с дисковым кэшем по (name, sql) —
        повторный прогон пайплайна не пережигает кредиты повторно, пока
        текст запроса не поменялся. Каждый вызов (кэш-хит или реальное
        исполнение) пишет запись в self.credit_ledger с фактической
        стоимостью в кредитах, взятой из ответа Dune (`execution_cost_credits`
        в статусе исполнения) — см. docs/DATA_ACCESS.md, "Смоук-тест".
        """
        cache_key = hashlib.sha256(sql.encode()).hexdigest()[:16]
        # CSV, не parquet: избегаем зависимости от pyarrow/fastparquet
        # (первый реальный прогон в CI упал именно на этом — все 5 Dune-
        # запросов честно выполнились и вернули строки, но кэш-запись
        # рухнула ПОСЛЕ, из-за чего результат потерялся). Датафреймы
        # здесь маленькие (агрегаты по кошелькам), CSV более чем годится.
        cache_file = self.cache_dir / f"{name}_{cache_key}.csv"
        if cache_file.exists() and not force_refresh:
            print(f"[dune] cache hit: {cache_file.name}")
            self.credit_ledger.append({"name": name, "credits": 0.0, "cached": True})
            return pd.read_csv(cache_file)

        qid = query_id or self.create_query(name=name, sql=sql)
        execution_id = self.execute(qid)
        status = self.poll_until_done(execution_id)
        df, result_stats = self.get_results_df(execution_id)
        cost = status.get("execution_cost_credits")
        if cost is None:
            cost = result_stats.get("execution_cost_credits")
        self.credit_ledger.append({"name": name, "credits": cost, "cached": False})
        print(f"[dune] {name}: {cost if cost is not None else 'n/a'} credits")
        df.to_csv(cache_file, index=False)
        return df
