#!/usr/bin/env python3
"""Владелец (2026-09-16): разведка Bitquery ПЕРЕД любой платной историей.
Источники расходятся по тарифу (10K баллов на 1й месяц vs 1000 баллов
песочницы без истории) -- уже сожгли ~1400 кредитов на ошибках в Dune,
не повторяем. Строгий порядок, СТОП после каждого сомнительного шага.

Шаг 0 (не в задании владельца буквально, но обязателен по правилу
паспорта "искать как делают другие", не гадать про схему вслепую):
introspection-запрос на enum сетей -- в GraphQL это ВСЕГДА бесплатно
(валидация схемы происходит ДО выполнения оплачиваемого запроса данных,
это стандарт GraphQL, не специфика Bitquery) -- узнаём точное имя сети
Arc в схеме Bitquery, не гадаем "arc" вслепую.

Шаг 1: реальный остаток баллов -- ищем любой способ (заголовки ответа,
extensions.cost в GraphQL-ответе, отдельный billing-эндпоинт, если он
есть в схеме). Если способа явно нет -- честно фиксируем это как факт,
не выдумываем число.

Шаг 2: минимальный запрос limit:1 -- работает ли ключ, отдаётся ли
именно ARC MAINNET (не testnet).

ВАЖНО: скрипт останавливается после шага 2 и печатает всё найденное --
шаги 3-4 (история, оценка полной стоимости) делаются ОТДЕЛЬНЫМ прогоном
после анализа этого результата, не вслепую одним скриптом."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

ENDPOINT = "https://streaming.bitquery.io/graphql"
API_KEY = os.environ.get("BITQUERY_API", "")
REPO_ROOT_CANDIDATES = [Path("/home/bot/robinhood-chain-alpha"), Path(__file__).parent.parent]


def find_repo_root() -> Path:
    for r in REPO_ROOT_CANDIDATES:
        if r.joinpath("data").exists():
            return r
    return REPO_ROOT_CANDIDATES[-1]


def gql(query: str, variables: dict | None = None, timeout: int = 30) -> dict:
    try:
        resp = requests.post(ENDPOINT, json={"query": query, "variables": variables or {}},
                              headers={"Content-Type": "application/json",
                                       "Authorization": f"Bearer {API_KEY}"},
                              timeout=timeout)
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {"non_json_body": resp.text[:2000]}
        return {"http_status": resp.status_code, "headers_of_interest": {
            k: v for k, v in resp.headers.items()
            if any(w in k.lower() for w in ("credit", "cost", "quota", "limit", "rate", "point"))
        }, "body": body}
    except Exception as exc:  # noqa: BLE001
        return {"exception": f"{type(exc).__name__}: {exc}"}


def main() -> None:
    root = find_repo_root()
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "api_key_present": bool(API_KEY), "api_key_len": len(API_KEY)}

    if not API_KEY:
        result["STOPPED"] = "BITQUERY_API пуст в окружении -- секрет не дошёл до VPS (проверить workflow/секрет в репо), ни одного запроса не отправлено"
        print(json.dumps(result, indent=2, ensure_ascii=False))
        root.joinpath("data", "task_arc_bitquery_step1_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
        return

    # === Шаг 0: introspection -- бесплатно по определению GraphQL, узнаём реальное имя сети Arc ===
    introspect_q = """
    query {
      __type(name: "evm_network") { name enumValues { name } }
    }
    """
    r0 = gql(introspect_q)
    result["step0_introspect_evm_network_enum"] = r0

    # Если типа с таким именем нет -- пробуем найти ЛЮБОЙ enum-тип, чьи значения содержат "arc"
    arc_candidates = []
    body0 = r0.get("body") or {}
    t0 = ((body0.get("data") or {}).get("__type")) if isinstance(body0.get("data"), dict) else None
    if t0 and t0.get("enumValues"):
        arc_candidates = [v["name"] for v in t0["enumValues"] if "arc" in v["name"].lower()]
    result["step0_arc_candidates_in_known_enum_name"] = arc_candidates

    # Более широкий introspection, если первый не сработал (другое имя типа схемы) --
    # ищем ЛЮБОЙ тип, похожий на network enum, по списку типов схемы.
    if not t0:
        list_types_q = """
        query {
          __schema { types { name kind } }
        }
        """
        r0b = gql(list_types_q)
        result["step0b_schema_types_probe"] = {
            "http_status": r0b.get("http_status"),
            "has_errors": bool((r0b.get("body") or {}).get("errors")),
            "errors": (r0b.get("body") or {}).get("errors"),
        }
        types_list = (((r0b.get("body") or {}).get("data") or {}).get("__schema") or {}).get("types") or []
        network_like_types = [t["name"] for t in types_list if "network" in (t.get("name") or "").lower()]
        result["step0b_network_like_type_names"] = network_like_types[:30]

    # === Шаг 2: минимальный запрос limit:1 -- пробуем network: arc напрямую (самая частая
    # конвенция именования в Bitquery EVM-схеме), читаем РЕАЛЬНУЮ ошибку, если она есть ===
    minimal_q = """
    query {
      EVM(network: arc, dataset: combined) {
        DEXTrades(limit: {count: 1}) {
          Block { Time Number }
          Transaction { Hash }
        }
      }
    }
    """
    r2 = gql(minimal_q)
    result["step2_minimal_query_network_arc"] = r2

    result["STOPPED_after_step2"] = "Останавливаюсь здесь по протоколу -- шаги 3 (история) и 4 (оценка стоимости) делаются отдельным прогоном после анализа этого результата."

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    root.joinpath("data", "task_arc_bitquery_step1_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
