#!/usr/bin/env python3
"""Владелец (2026-09-16): новый секрет BITQUERY_APIKEY -- токен и id,
сгенерированные напрямую в account.bitquery.io (не через client_credentials
как BITQUERY_API2, который был отозван/невалиден -- 4/4 invalid_client).

Формат заранее не известен -- разбираем по факту, без печати значения:
  1) JSON ({"...": "..."})
  2) готовый Bearer-токен одной строкой (типично ory_at_..., но НЕ
     полагаемся на префикс -- дашборд мог выдать другой формат)
  3) пара client_id+client_secret через известный разделитель -- ТОЛЬКО
     если строка реально делится на ровно 2 непустые части, иначе это
     ложное срабатывание на одиночном токене с посторонним символом.

БЕЗОПАСНОСТЬ (тот же инцидент 12.09, то же требование): секрет и
access_token НЕ печатаются и не пишутся в файл ни в каком виде, ни
частично.

Порядок строгий, не перескакивать (задание владельца):
  0) Схема-интроспекция (бесплатно, не трогает блокчейн-данные) --
     находим ИМЕНА полей для сырых логов/событий и корректное значение
     enum network для Arc, вместо угадывания вслепую.
  1) Остаток баллов -- ищем billing-подобные поля в схеме; честно
     фиксируем, если прямого API нет (уже было установлено в прошлом
     раунде, что такого поля может не быть).
  2) limit:1 запрос, mainnet Arc -- работает ли ключ, тот ли network.
  3) История: свопы ОДНОГО известного пула (AROS) за первый час 16.09
     (00:00-01:00 UTC) -- если Bitquery не декодирует V4 Swap как
     DEXTrades (сингл-контракт PoolManager, не фактори-архитектура V2/V3,
     это структурное отличие, не гарантия отсутствия поддержки, но
     обоснованный риск) -- используем СЫРОЙ Log/Event-запрос по адресу
     PoolManager + topic0=Swap + topic1=pool_id, а не DEXTrades-абстракцию.
  4) Если история есть -- оценка стоимости полной задачи (16 пулов x
     4 окна) ДО запуска, по фактической стоимости шага 3.

Леджер -- data/credits_spent_bitquery.json, по образцу
data/credits_spent.json (Dune) -- копится между прогонами, не
перезаписывается."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

GRAPHQL_ENDPOINT = "https://streaming.bitquery.io/graphql"
OAUTH_ENDPOINT = "https://oauth2.bitquery.io/oauth2/token"
REPO_ROOT_CANDIDATES = [Path("/home/bot/robinhood-chain-alpha"), Path(__file__).parent.parent]

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
AROS_POOL_ID = "0x3d3f72f78bdc6e6510650ea59b508064c897d1c384cdeaf7b3e052ce38b5ba00"
SWAP_TOPIC0 = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"  # keccak256("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"), verified by direct computation, not memorized


def find_repo_root() -> Path:
    for r in REPO_ROOT_CANDIDATES:
        if r.joinpath("data").exists():
            return r
    return REPO_ROOT_CANDIDATES[-1]


def parse_secret(raw: str) -> dict:
    """Возвращает {mode, ..., client_id?, client_secret?, bearer_token?} --
    значения-секреты будут .pop()-нуты вызывающим кодом перед логированием."""
    raw = raw.strip()
    shape: dict = {"total_len": len(raw), "starts_with_brace": raw.startswith("{")}
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
            shape["mode"] = "json"
            shape["json_keys"] = sorted(obj.keys())
            token = obj.get("access_token") or obj.get("token") or obj.get("bearer")
            cid = obj.get("client_id") or obj.get("id")
            csec = obj.get("client_secret") or obj.get("secret")
            if token:
                return {**shape, "bearer_token": token}
            if cid and csec:
                return {**shape, "client_id": cid, "client_secret": csec}
            shape["note"] = "JSON без распознанных ключей (access_token/token/bearer или client_id+client_secret)"
            return shape
        except Exception:  # noqa: BLE001
            shape["parsed_as_json"] = False
    for sep in (":", " ", "\n", ",", "|", ";"):
        if sep in raw:
            parts = [p for p in raw.split(sep) if p]
            if len(parts) == 2:
                shape["mode"] = "id_secret_pair"
                shape["detected_separator"] = repr(sep)
                shape["part1_len"], shape["part2_len"] = len(parts[0]), len(parts[1])
                return {**shape, "client_id": parts[0], "client_secret": parts[1]}
    shape["mode"] = "single_token"
    shape["looks_like_ory_at"] = raw.startswith("ory_at_")
    return {**shape, "bearer_token": raw}


def get_access_token_via_oauth(client_id: str, client_secret: str) -> dict:
    try:
        resp = requests.post(OAUTH_ENDPOINT, data={
            "grant_type": "client_credentials", "client_id": client_id,
            "client_secret": client_secret, "scope": "api",
        }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=20)
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {"non_json_body_len": len(resp.text)}
        safe = {k: v for k, v in body.items() if k != "access_token"}
        safe["access_token_received"] = bool(body.get("access_token"))
        return {"http_status": resp.status_code, "body_safe": safe, "_raw_token": body.get("access_token")}
    except Exception as exc:  # noqa: BLE001
        return {"exception": f"{type(exc).__name__}: {exc}"}


def gql(access_token: str, query: str, variables: dict | None = None) -> dict:
    try:
        payload = {"query": query}
        if variables:
            payload["variables"] = variables
        resp = requests.post(GRAPHQL_ENDPOINT, json=payload,
                              headers={"Content-Type": "application/json",
                                       "Authorization": f"Bearer {access_token}"}, timeout=30)
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {"non_json_body": resp.text[:2000]}
        return {"http_status": resp.status_code, "body": body}
    except Exception as exc:  # noqa: BLE001
        return {"exception": f"{type(exc).__name__}: {exc}"}


def load_ledger(data_dir: Path) -> dict:
    p = data_dir.joinpath("credits_spent_bitquery.json")
    if p.exists():
        return json.loads(p.read_text())
    return {"created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "entries": []}


def save_ledger(data_dir: Path, ledger: dict) -> None:
    data_dir.joinpath("credits_spent_bitquery.json").write_text(json.dumps(ledger, indent=2, ensure_ascii=False))


def log_ledger(ledger: dict, step: str, query_desc: str, response_meta: dict) -> None:
    ledger["entries"].append({
        "at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "step": step, "query": query_desc,
        "http_status": response_meta.get("http_status"),
        "has_errors": bool((response_meta.get("body") or {}).get("errors")),
        "extensions": (response_meta.get("body") or {}).get("extensions"),
    })


def main() -> None:
    root = find_repo_root()
    data_dir = root.joinpath("data")
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    ledger = load_ledger(data_dir)

    raw = os.environ.get("BITQUERY_APIKEY", "")
    result["secret_present"] = bool(raw)
    if not raw:
        result["STOPPED"] = "BITQUERY_APIKEY пуст в окружении"
        print(json.dumps(result, indent=2, ensure_ascii=False))
        data_dir.joinpath("task_arc_bitquery_apikey_probe_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
        return

    parsed = parse_secret(raw)
    bearer_token = parsed.pop("bearer_token", None)
    client_id = parsed.pop("client_id", None)
    client_secret = parsed.pop("client_secret", None)
    result["credential_format_detected"] = parsed

    access_token = None
    if bearer_token:
        access_token = bearer_token
        result["auth_path"] = "ГОТОВЫЙ токен использован напрямую как Bearer (без OAuth2-обмена)"
    elif client_id and client_secret:
        token_resp = get_access_token_via_oauth(client_id, client_secret)
        access_token = token_resp.pop("_raw_token", None)
        result["oauth_token_exchange"] = token_resp
        result["auth_path"] = "пара id+secret -- прошли OAuth2 client_credentials"
    else:
        result["STOPPED"] = "не удалось разобрать формат BITQUERY_APIKEY -- см. credential_format_detected"
        print(json.dumps(result, indent=2, ensure_ascii=False))
        data_dir.joinpath("task_arc_bitquery_apikey_probe_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
        return

    if not access_token:
        result["STOPPED"] = "не получили access_token (см. oauth_token_exchange, если был обмен)"
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        data_dir.joinpath("task_arc_bitquery_apikey_probe_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    # === Шаг 0: интроспекция схемы (бесплатно) -- ищем реальные имена
    # полей для сырых логов/событий и допустимые значения enum network,
    # вместо угадывания вслепую названий типа DEXTrades/Events/Logs. ===
    introspect_q = """
    query {
      __schema {
        queryType { fields { name } }
      }
      networkEnum: __type(name: "evm_network") { enumValues { name } }
    }
    """
    r0 = gql(access_token, introspect_q)
    log_ledger(ledger, "step0_introspection", "schema queryType fields + evm_network enum", r0)
    body0 = r0.get("body") or {}
    qt_fields = [f["name"] for f in (((body0.get("data") or {}).get("__schema") or {}).get("queryType") or {}).get("fields", [])]
    network_enum = (body0.get("data") or {}).get("networkEnum")
    result["step0_introspection"] = {
        "http_status": r0.get("http_status"), "root_query_fields": qt_fields,
        "evm_network_enum_values": [v["name"] for v in network_enum["enumValues"]] if network_enum else None,
        "errors": body0.get("errors"),
    }
    billing_like = [f for f in qt_fields if any(w in f.lower() for w in ("billing", "credit", "usage", "quota", "point"))]

    # === Шаг 1: остаток баллов -- честно, без выдумывания числа ===
    result["step1_credit_balance"] = {
        "billing_like_root_fields_found": billing_like,
        "conclusion": (
            f"Найдены поля-кандидаты: {billing_like} -- нужно проверить отдельно." if billing_like else
            "Прямого поля остатка баллов в корневой схеме НЕТ (подтверждено интроспекцией, не предположение) -- "
            "остаток проверяется только через дашборд account.bitquery.io вручную."
        ),
    }
    ledger.setdefault("starting_balance_known", False)

    # === Шаг 2: limit:1, mainnet Arc -- определяем правильное значение enum ===
    arc_network_value = None
    if result["step0_introspection"]["evm_network_enum_values"]:
        candidates = [v for v in result["step0_introspection"]["evm_network_enum_values"] if "arc" in v.lower()]
        arc_network_value = candidates[0] if len(candidates) == 1 else candidates
    result["step2_arc_network_enum_candidates"] = arc_network_value

    network_value_to_try = arc_network_value if isinstance(arc_network_value, str) else "arc"
    minimal_q = """
    query {
      EVM(network: %s, dataset: combined) {
        Blocks(limit: {count: 1}, orderBy: {descending: Block_Time}) {
          Block { Time Number }
        }
      }
    }
    """ % network_value_to_try
    r2 = gql(access_token, minimal_q)
    log_ledger(ledger, "step2_minimal_arc_query", f"EVM(network: {network_value_to_try}) Blocks limit:1", r2)
    result["step2_minimal_query"] = {"network_tried": network_value_to_try, **r2}

    step2_ok = r2.get("http_status") == 200 and not (r2.get("body") or {}).get("errors")
    if not step2_ok:
        result["STOPPED_AT_STEP2"] = (
            "limit:1 запрос не прошёл (см. step2_minimal_query) -- дальше (история, оценка стоимости) "
            "не идём, чтобы не тратить баллы на заведомо нерабочем network/схеме."
        )
        save_ledger(data_dir, ledger)
        result["ledger_path"] = "data/credits_spent_bitquery.json"
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        data_dir.joinpath("task_arc_bitquery_apikey_probe_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    # === Шаг 3: история -- сырой Log/Event-запрос (не DEXTrades -- V4
    # это singleton PoolManager, не per-pool контракт, DEXTrades-декодер
    # мог не быть настроен под V4) на известном пуле AROS, первый час 16.09 ===
    history_q = """
    query {
      EVM(network: %s, dataset: combined) {
        Logs(
          where: {
            Log: {SmartContract: {is: "%s"}, Signature: {SignatureHash: {is: "%s"}}, Topics: {includes: "%s"}}
            Block: {Time: {since: "2026-09-16T00:00:00Z", till: "2026-09-16T01:00:00Z"}}
          }
          limit: {count: 5}
        ) {
          Block { Time Number }
          Transaction { Hash }
          Log { SmartContract Signature { SignatureHash } }
        }
      }
    }
    """ % (network_value_to_try, POOL_MANAGER, SWAP_TOPIC0, AROS_POOL_ID)
    r3 = gql(access_token, history_q)
    log_ledger(ledger, "step3_history_check", "Logs PoolManager+Swap+pool_id, 16.09 00:00-01:00 UTC", r3)
    result["step3_history_check"] = r3
    history_errors = (r3.get("body") or {}).get("errors")
    history_rows = (((r3.get("body") or {}).get("data") or {}).get("EVM") or {}).get("Logs")
    history_available = r3.get("http_status") == 200 and not history_errors and history_rows is not None
    result["step3_conclusion"] = (
        f"ИСТОРИЯ ЕСТЬ -- {len(history_rows)} строк за первый час 16.09 на пуле AROS." if history_available and history_rows else
        "Пусто (0 строк) -- либо в этот час действительно не было свопов на AROS (маловероятно, пул активный), "
        "либо схема Logs/Signature/Topics подобрана неверно -- см. errors." if history_available else
        f"ОТКАЗ по схеме/тарифу (см. errors: {history_errors}) -- ИСТОРИЯ ЗАКРЫТА для этого пути, Bitquery не подходит "
        "для fee/LVR через этот тип запроса, не пробуем дальше вслепую."
    )

    if not history_available:
        save_ledger(data_dir, ledger)
        result["ledger_path"] = "data/credits_spent_bitquery.json"
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        data_dir.joinpath("task_arc_bitquery_apikey_probe_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    # === Шаг 4: оценка стоимости полной задачи ДО запуска ===
    result["step4_cost_estimate"] = {
        "basis": "1 пул x 1 часовое окно уже стоило 1 запрос (см. step3 extensions, если API их вернул)",
        "full_task_would_be": "16 пулов x 4 окна = 64 аналогичных запроса",
        "extensions_from_step3": (r3.get("body") or {}).get("extensions"),
        "note": (
            "Если API не возвращает cost в extensions (частый случай для Bitquery V2) -- точная оценка "
            "невозможна без реального прогона; честно фиксируем это ограничение, не выдумываем число."
        ),
    }

    save_ledger(data_dir, ledger)
    result["ledger_path"] = "data/credits_spent_bitquery.json"
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    data_dir.joinpath("task_arc_bitquery_apikey_probe_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
