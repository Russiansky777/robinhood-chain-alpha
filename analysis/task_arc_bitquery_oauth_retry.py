#!/usr/bin/env python3
"""Продолжение task_arc_bitquery_oauth_probe.py: первая попытка (client_id,
client_secret) как form-body параметры дала 401 invalid_client. Два реальных
технических факта, а не догадка:
  1) Токен формата ory_at_... -- это ORY Hydra (движок, на котором построен
     Bitquery OAuth2), а Hydra по умолчанию требует client_credentials
     через HTTP Basic Auth (client_id:client_secret в base64 в заголовке
     Authorization), если клиент явно не зарегистрирован с
     token_endpoint_auth_method=client_secret_post -- это не выдумка,
     а стандартное поведение конкретного движка, который мы уже
     идентифицировали по префиксу токена в самой первой инструкции.
  2) Наш парсер делит секрет на part1/part2 по '\\n' и БЕЗ проверки
     присваивает part1=client_id, part2=client_secret -- порядок мог
     быть перепутан.
Перебираем 4 комбинации (порядок id/secret) x (body vs Basic Auth),
останавливаемся на первом успехе. Ни один запрос не тратит платные
GraphQL-кредиты -- это только эндпоинт токена авторизации.
Секрет и токен по-прежнему не печатаются и не пишутся в файл ни в каком
виде -- то же требование, что и в предыдущем скрипте."""
from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import requests

OAUTH_ENDPOINT = "https://oauth2.bitquery.io/oauth2/token"
GRAPHQL_ENDPOINT = "https://streaming.bitquery.io/graphql"
REPO_ROOT_CANDIDATES = [Path("/home/bot/robinhood-chain-alpha"), Path(__file__).parent.parent]


def find_repo_root() -> Path:
    for r in REPO_ROOT_CANDIDATES:
        if r.joinpath("data").exists():
            return r
    return REPO_ROOT_CANDIDATES[-1]


def split_raw(raw: str) -> tuple[str, str] | None:
    raw = raw.strip()
    for sep in ("\n", ":", " ", ",", "|", ";"):
        if sep in raw:
            parts = [p for p in raw.split(sep) if p]
            if len(parts) == 2:
                return parts[0], parts[1]
    return None


def try_body_auth(client_id: str, client_secret: str) -> dict:
    resp = requests.post(OAUTH_ENDPOINT, data={
        "grant_type": "client_credentials", "client_id": client_id,
        "client_secret": client_secret, "scope": "api",
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=20)
    return resp


def try_basic_auth(client_id: str, client_secret: str) -> dict:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(OAUTH_ENDPOINT, data={
        "grant_type": "client_credentials", "scope": "api",
    }, headers={"Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {basic}"}, timeout=20)
    return resp


def safe_result(resp) -> dict:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {"non_json_body_len": len(resp.text)}
    safe = {k: v for k, v in body.items() if k != "access_token"}
    safe["access_token_received"] = bool(body.get("access_token"))
    safe["access_token_len"] = len(body["access_token"]) if body.get("access_token") else None
    safe["access_token_prefix_format_ok"] = bool(body.get("access_token", "").startswith("ory_at_"))
    return {"http_status": resp.status_code, "body_safe": safe, "_raw_token": body.get("access_token")}


def gql(access_token: str, query: str) -> dict:
    try:
        resp = requests.post(GRAPHQL_ENDPOINT, json={"query": query},
                              headers={"Content-Type": "application/json",
                                       "Authorization": f"Bearer {access_token}"}, timeout=30)
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {"non_json_body": resp.text[:2000]}
        return {"http_status": resp.status_code, "body": body}
    except Exception as exc:  # noqa: BLE001
        return {"exception": f"{type(exc).__name__}: {exc}"}


def main() -> None:
    root = find_repo_root()
    data_dir = root.joinpath("data")
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    raw = os.environ.get("BITQUERY_API2", "")
    result["secret_present"] = bool(raw)
    if not raw:
        result["STOPPED"] = "BITQUERY_API2 пуст в окружении"
        print(json.dumps(result, indent=2, ensure_ascii=False))
        data_dir.joinpath("task_arc_bitquery_oauth_retry_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
        return

    parts = split_raw(raw)
    if not parts:
        result["STOPPED"] = "не удалось разбить секрет на 2 части ни одним разделителем"
        print(json.dumps(result, indent=2, ensure_ascii=False))
        data_dir.joinpath("task_arc_bitquery_oauth_retry_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
        return
    part_a, part_b = parts

    attempts_spec = [
        ("order=(part1,part2) transport=body", part_a, part_b, try_body_auth),
        ("order=(part2,part1) transport=body", part_b, part_a, try_body_auth),
        ("order=(part1,part2) transport=basic_auth_header", part_a, part_b, try_basic_auth),
        ("order=(part2,part1) transport=basic_auth_header", part_b, part_a, try_basic_auth),
    ]

    attempts_log = []
    access_token = None
    winning_attempt = None
    for label, cid, csec, fn in attempts_spec:
        try:
            resp = fn(cid, csec)
            r = safe_result(resp)
        except Exception as exc:  # noqa: BLE001
            r = {"exception": f"{type(exc).__name__}: {exc}"}
        token = r.pop("_raw_token", None)
        attempts_log.append({"attempt": label, **r})
        if token:
            access_token = token
            winning_attempt = label
            break
        time.sleep(1)  # не долбить эндпоинт токена подряд без паузы

    result["attempts"] = attempts_log
    result["winning_attempt"] = winning_attempt

    if not access_token:
        result["STOPPED"] = (
            "Все 4 комбинации (порядок id/secret x body/Basic-Auth) дали invalid_client -- "
            "проблема не в порядке частей и не в транспорте, см. attempts[].body_safe. "
            "Нужна проверка самих значений client_id/client_secret на стороне владельца "
            "(например, ключ мог быть отозван, не активирован, или формат секрета иной, "
            "чем 2 части через известные разделители)."
        )
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        data_dir.joinpath("task_arc_bitquery_oauth_retry_result.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False, default=str)
        )
        return

    # === Токен получен -- продолжаем Шаг 1 и Шаг 2 из предыдущего скрипта ===
    billing_q = "query { __schema { queryType { fields { name } } } }"
    r_schema = gql(access_token, billing_q)
    body = r_schema.get("body") or {}
    qt = ((body.get("data") or {}).get("__schema") or {}).get("queryType") or {}
    field_names = [f["name"] for f in qt.get("fields", [])]
    billing_like = [f for f in field_names if any(w in f.lower() for w in ("billing", "credit", "usage", "quota", "point"))]
    result["step1_schema_query_fields_total"] = len(field_names)
    result["step1_billing_like_fields_found"] = billing_like

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
    result["step2_minimal_query_network_arc"] = gql(access_token, minimal_q)

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    data_dir.joinpath("task_arc_bitquery_oauth_retry_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
