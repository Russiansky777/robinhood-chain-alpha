#!/usr/bin/env python3
"""Владелец (2026-09-16): новый секрет BITQUERY_API2 содержит и
client_id, и client_secret -- формат не известен заранее (может быть
"id:secret", "id secret", JSON, или что-то ещё), разбираем сами по
длине/разделителям, не гадаем вслепую какой символ.

БЕЗОПАСНОСТЬ (явное требование, был инцидент утечки ключа Alchemy в лог
12.09): ни сырой секрет, ни полученный access_token НЕ печатаются и не
пишутся в файл ни в каком виде -- ни полностью, ни частично, ни первые/
последние N символов. В результат идут только: длины, факт наличия
разделителя, булевы результаты запросов, суммы использованных баллов.

Шаг 1 (баланс) -- честно: прямого API "текущий остаток баллов" может не
быть в Bitquery (не подтверждено доками) -- проверяем по факту через
account API, если такой найдётся в схеме; если нет -- фиксируем это, не
выдумываем число.

Шаг 2 -- limit:1 запрос, mainnet Arc (баннер "Now live -- Arc Mainnet
data is available" уже подтверждён с доков).

Шаг 3-4 в этом скрипте НЕ делаются -- отдельный прогон после анализа
этого результата, чтобы не тратить баллы вслепую при первой ошибке."""
from __future__ import annotations

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


def parse_credentials(raw: str) -> dict:
    """Определяем формат БЕЗ печати самого секрета -- только форма."""
    raw = raw.strip()
    shape: dict = {"total_len": len(raw), "starts_with_brace": raw.startswith("{")}
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
            shape["parsed_as_json"] = True
            shape["json_keys"] = sorted(obj.keys())
            cid = obj.get("client_id") or obj.get("id") or obj.get("clientId")
            csec = obj.get("client_secret") or obj.get("secret") or obj.get("clientSecret")
            shape["client_id_len"] = len(cid) if cid else None
            shape["client_secret_len"] = len(csec) if csec else None
            return {**shape, "client_id": cid, "client_secret": csec}
        except Exception:  # noqa: BLE001
            shape["parsed_as_json"] = False
    for sep in (":", " ", "\n", ",", "|", ";"):
        if sep in raw:
            parts = [p for p in raw.split(sep) if p]
            if len(parts) == 2:
                shape["detected_separator"] = repr(sep)
                shape["part1_len"] = len(parts[0])
                shape["part2_len"] = len(parts[1])
                return {**shape, "client_id": parts[0], "client_secret": parts[1]}
    shape["detected_separator"] = None
    shape["note"] = "не нашли разделитель на 2 части -- вся строка одним куском, не можем разобрать на id+secret"
    return {**shape, "client_id": None, "client_secret": None}


def get_access_token(client_id: str, client_secret: str) -> dict:
    try:
        resp = requests.post(OAUTH_ENDPOINT, data={
            "grant_type": "client_credentials", "client_id": client_id,
            "client_secret": client_secret, "scope": "api",
        }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=20)
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {"non_json_body_len": len(resp.text)}
        # НЕ включаем access_token в возвращаемый словарь ни в каком виде
        safe_body = {k: v for k, v in body.items() if k != "access_token"}
        safe_body["access_token_received"] = bool(body.get("access_token"))
        safe_body["access_token_len"] = len(body["access_token"]) if body.get("access_token") else None
        safe_body["access_token_prefix_format_ok"] = bool(body.get("access_token", "").startswith("ory_at_"))
        return {"http_status": resp.status_code, "body_safe": safe_body,
                "raw_access_token": body.get("access_token")}  # используется локально ниже, НЕ печатается целиком
    except Exception as exc:  # noqa: BLE001
        return {"exception": f"{type(exc).__name__}: {exc}"}


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
    result["secret_total_len"] = len(raw)
    if not raw:
        result["STOPPED"] = "BITQUERY_API2 пуст в окружении"
        print(json.dumps(result, indent=2, ensure_ascii=False))
        data_dir.joinpath("task_arc_bitquery_oauth_probe_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
        return

    parsed = parse_credentials(raw)
    client_id = parsed.pop("client_id", None)
    client_secret = parsed.pop("client_secret", None)
    result["credential_format_detected"] = parsed  # без самих значений, только форма/длины

    if not client_id or not client_secret:
        result["STOPPED"] = "не удалось разобрать BITQUERY_API2 на client_id+client_secret -- см. credential_format_detected, нужна форма от владельца"
        print(json.dumps(result, indent=2, ensure_ascii=False))
        data_dir.joinpath("task_arc_bitquery_oauth_probe_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
        return

    token_resp = get_access_token(client_id, client_secret)
    access_token = token_resp.pop("raw_access_token", None)
    result["oauth_token_exchange"] = token_resp

    if not access_token:
        result["STOPPED"] = "OAuth2 обмен не дал access_token -- см. oauth_token_exchange.body_safe для деталей ошибки"
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        data_dir.joinpath("task_arc_bitquery_oauth_probe_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    # === Шаг 1: попытка узнать остаток баллов -- ищем billing-подобные поля в схеме ===
    billing_q = """
    query { __schema { queryType { fields { name } } } }
    """
    r_schema = gql(access_token, billing_q)
    field_names = []
    body = r_schema.get("body") or {}
    qt = ((body.get("data") or {}).get("__schema") or {}).get("queryType") or {}
    field_names = [f["name"] for f in qt.get("fields", [])]
    billing_like = [f for f in field_names if any(w in f.lower() for w in ("billing", "credit", "usage", "quota", "point"))]
    result["step1_schema_query_fields_total"] = len(field_names)
    result["step1_billing_like_fields_found"] = billing_like
    result["step1_conclusion"] = (
        "Прямого поля для остатка баллов НЕ найдено в корневых query-полях схемы -- см. billing_like_fields_found (пусто = не найдено). "
        "Остаток нужно проверять через дашборд account.bitquery.io вручную, честно фиксируем это ограничение."
    ) if not billing_like else f"Найдены поля-кандидаты: {billing_like} -- проверить отдельно."

    # === Шаг 2: минимальный limit:1 запрос, mainnet Arc ===
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
    r2 = gql(access_token, minimal_q)
    result["step2_minimal_query_network_arc"] = r2

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    data_dir.joinpath("task_arc_bitquery_oauth_probe_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
