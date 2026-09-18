#!/usr/bin/env python3
"""Владелец, 2026-09-18: узкая, целевая проверка Bitquery -- НЕ повтор
закрытой линии Arc (см. docs/PROJECT_STATE.md, "Bitquery -- ЗАКРЫТО
ОКОНЧАТЕЛЬНО, 2026-09-17": на том аккаунте Dataset access = realtime,
истории нет вообще). Гипотеза владельца в ЭТОМ раунде другая и УЖЕ
СОВМЕСТИМА с "realtime": по документации Bitquery последние ~30 дней
доступны через отдельный источник Trading.Pairs/Trading.Trades
(секундные свечи + построчные сделки), не через архивный DEXTrades.
Наши покупки 16-17.09 -- внутри этого окна. Проверяем ИМЕННО это, не
переоткрывая уже закрытый архивный вопрос.

Транспорт авторизации УЖЕ ИЗВЕСТЕН для BITQUERY_APIKEY (см.
data/task_arc_bitquery_apikey_probe_result.json, 2026-09-16): заголовок
X-API-KEY, одна строка целиком -- дал реальный 402 (не 401), значит
ключ был опознан. Здесь используем его напрямую, без повторного перебора
форматов.

Порядок (владелец, п.1-2 задания):
  Шаг 0 (бесплатно): интроспекция схемы -- ищем реальные имена полей
    Trading/Pairs/Trades (в корне и под Solana), их аргументы и поля
    возвращаемого типа. НЕ угадываем по памяти прошлых версий API.
  Шаг 1 (один платный запрос): секундные свечи/сделки для ОДНОГО пула
    одной уже посчитанной покупки, окно [target-5, target+305] (наши
    +5..+300с горизонты с запасом), сравнение с реальными ончейн-ценами
    из data/solana_buyer_200/step_extended_result.json.
  Если отказ по тарифу/данным -- честно сказать (не пробовать другие
    источники в этом же прогоне), записать расход в леджер."""
from __future__ import annotations

import calendar
import json
import os
import time
from decimal import Decimal as D
from pathlib import Path

import requests

GRAPHQL_ENDPOINT = "https://streaming.bitquery.io/graphql"
OUT_PATH = Path("data/solana_bitquery_probe_result.json")
LEDGER_PATH = Path("data/credits_spent_bitquery.json")

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# РЕАЛЬНАЯ уже посчитанная покупка (одноногий маршрут -- вся цена идёт
# через ОДИН пул минт/USDC, не композит нескольких пулов), взято из
# data/solana_buyer_200/step_extended_result.json + routes_300.json +
# selected_300.json (скрипт-разведка этой же сессии, не выдумано).
REFERENCE = {
    "signature": "4yh7QkEp4r1kkrpsVD81G6uHfGdQqifuM1Yz5zfAftcaeFtdyztwSASQ4gh2D9zDByp2eBbV4ZMeYfRdDXhWL8Wu",
    "pool": "HMh9syEn37waF4hNYcyMuaPRv6xFTpTdb3MqMEMNppSR",
    "mint": "Lyi47medADEVDd5hxJo1mbxhnBct841sFpcGRyHTuwp",
    "quote_mint": USDC,
    "points": [
        {"seconds": 5, "target": 1789666234, "onchain_slot": 447852398,
         "onchain_signature": "YZogHsgviZ6yCMgQMyu5jJ2TrPmJXr3fofbYUxzvT9faM4daFrnKyZnGK1YAjS3qpgJ9oDhZLCaFn29DtUTzetd",
         "onchain_price_usdc_per_mint": "0.0005442370118868819715403925216"},
        {"seconds": 15, "target": 1789666244, "onchain_slot": 447852430,
         "onchain_signature": "2RMLDpKeshWJ8kaofBkPME62EnX3xyqR6HkvAY6GsZJBTgnMrMYXmPkgS7chJapNM6eisaii9qACidgSSRN8ECLT",
         "onchain_price_usdc_per_mint": "0.0005444364514526689607645346517"},
        {"seconds": 30, "target": 1789666259, "onchain_slot": 447852430,
         "onchain_signature": "2RMLDpKeshWJ8kaofBkPME62EnX3xyqR6HkvAY6GsZJBTgnMrMYXmPkgS7chJapNM6eisaii9qACidgSSRN8ECLT",
         "onchain_price_usdc_per_mint": "0.0005444364514526689607645346517"},
        {"seconds": 60, "target": 1789666289, "onchain_slot": 447852578,
         "onchain_signature": "5CfFVdyukkam1CTfTVib8T6RgnzdC7ALfUNNYSnc2xcNHFQoQ8kcYjE3qpjNQ6oTJAtMD9VhTR7erpDLaNQQdReR",
         "onchain_price_usdc_per_mint": "0.0005409663640615202325927213698"},
        {"seconds": 180, "target": 1789666409, "onchain_slot": 447852841,
         "onchain_signature": "3JbRN2Em39tyjfxqFTrZeL64vbisrtjDLJ9tweoq5ZgmjsijCTKva1pG2SXghAk7NhdvYABnyh8nHFpeGnANMMwt",
         "onchain_price_usdc_per_mint": "0.0005499489002392080835479257555"},
        {"seconds": 300, "target": 1789666529, "onchain_slot": 447853062,
         "onchain_signature": "2sG9bnBEp5EVGEkB9CBs2YAKzBWN486KD4EGrdj7qTVXkkjsakWxh2KwkkxWiTBSnHaYR4jLbV6ndHHKkAiD3CzT",
         "onchain_price_usdc_per_mint": "0.0005499868682294680022191687871"},
    ],
}


# ИНЦИДЕНТ 2026-09-18: requests/urllib3 при InvalidHeader эхом печатает
# ПОЛНОЕ значение заголовка в тексте исключения -- реальный
# BITQUERY_APIKEY оказался многострочной меткой ('Access token - ...\n
# ID - ...'), перевод строки сломал заголовок, и секрет целиком утёк в
# закоммиченный JSON (git log, коммит 0b54a8f, отредактирован
# постфактум). _ACTIVE_SECRETS заполняется в main() ДО первого сетевого
# вызова -- каждая строка, уходящая в out/ledger, прогоняется через
# _scrub_all() против ВСЕХ известных на этот момент значений (и сырого
# env-секрета, и распарсенного токена), а не только текущего api_key.
_ACTIVE_SECRETS: list[str] = []


def _scrub_all(text: str) -> str:
    for secret in _ACTIVE_SECRETS:
        if secret:
            text = text.replace(secret, "[REDACTED_SECRET]")
    return text


def gql(api_key: str, query: str, variables: dict | None = None) -> dict:
    payload: dict = {"query": query}
    if variables:
        payload["variables"] = variables
    try:
        resp = requests.post(GRAPHQL_ENDPOINT, json=payload,
                              headers={"Content-Type": "application/json", "X-API-KEY": api_key}, timeout=30)
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {"non_json_body": _scrub_all(resp.text[:2000])}
        return {"http_status": resp.status_code, "body": body}
    except Exception as exc:  # noqa: BLE001
        return {"exception": _scrub_all(f"{type(exc).__name__}: {exc}")}


def load_ledger() -> dict:
    if LEDGER_PATH.exists():
        try:
            return json.loads(LEDGER_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "entries": []}


def log_ledger(ledger: dict, step: str, query_desc: str, r: dict) -> None:
    body = r.get("body") or {}
    ledger["entries"].append({
        "at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "step": step, "query": query_desc,
        "http_status": r.get("http_status"),
        "exception": r.get("exception"),
        "has_errors": bool(body.get("errors")),
        "errors": body.get("errors"),
        "extensions": body.get("extensions"),
    })
    LEDGER_PATH.write_text(_scrub_all(json.dumps(ledger, indent=2, ensure_ascii=False, default=str)))


def introspect_type(api_key: str, type_name: str) -> dict:
    q = """
    query {
      __type(name: "%s") {
        name kind
        fields {
          name
          args { name type { name kind ofType { name kind } } }
          type { name kind ofType { name kind ofType { name kind } } }
        }
      }
    }
    """ % type_name
    return gql(api_key, q)


def main() -> None:
    out: dict = {"generated_at_utc": None, "reference": REFERENCE}
    ledger = load_ledger()

    raw_secret = os.environ.get("BITQUERY_APIKEY", "")
    out["secret_present"] = bool(raw_secret)
    if not raw_secret:
        out["HONEST_ANSWER"] = "BITQUERY_APIKEY пуст в окружении -- не могу проверить."
        _finish(out)
        return
    _ACTIVE_SECRETS.append(raw_secret)  # скрабировать ДО первого сетевого вызова, не после

    # ИНЦИДЕНТ 2026-09-18: BITQUERY_APIKEY -- это двухстрочная метка
    # дашборда ("Access token - <token>\nID - <uuid>"), не сырой токен.
    # Раньше это отправлялось как есть в заголовок X-API-KEY, перевод
    # строки ломал HTTP-заголовок (InvalidHeader) и эхом печатал секрет
    # в тексте исключения. Разбираем метку и берём ТОЛЬКО сам токен.
    api_key = raw_secret
    for line in raw_secret.splitlines():
        if line.lower().startswith("access token"):
            _, _, token_part = line.partition("-")
            api_key = token_part.strip()
            _ACTIVE_SECRETS.append(api_key)
            out["credential_format_detected"] = "label_format_access_token_line_parsed"
            break
    else:
        if "\n" in raw_secret or "\r" in raw_secret:
            out["HONEST_ANSWER"] = (
                "BITQUERY_APIKEY многострочный, но строка 'Access token - ...' не найдена -- "
                "не угадываю формат вслепую, останавливаюсь до разбора вручную."
            )
            _finish(out)
            return
        out["credential_format_detected"] = "single_line_raw_token"

    # ---------- Шаг 0: интроспекция (бесплатно) ----------
    root_q = "query { __schema { queryType { name fields { name } } } }"
    r0 = gql(api_key, root_q)
    log_ledger(ledger, "step0_root_fields", "queryType.fields (root)", r0)
    body0 = r0.get("body") or {}
    root_fields = [f["name"] for f in (((body0.get("data") or {}).get("__schema") or {}).get("queryType") or {}).get("fields", [])]
    out["step0_root_fields"] = root_fields
    print(f"[bitquery_probe] Корневые поля схемы: {root_fields}")

    if "exception" in r0:
        out["step0_raw_response"] = r0
        out["HONEST_ANSWER"] = f"Сетевой запрос к {GRAPHQL_ENDPOINT} упал ДО получения HTTP-ответа: {r0['exception']} -- дальше не иду."
        print("[bitquery_probe] " + out["HONEST_ANSWER"])
        _finish(out)
        return
    if r0.get("http_status") != 200 or (body0.get("errors")):
        out["step0_raw_response"] = r0
        out["HONEST_ANSWER"] = f"Интроспекция корня не прошла (http={r0.get('http_status')}, errors={body0.get('errors')}) -- дальше не иду."
        print("[bitquery_probe] " + out["HONEST_ANSWER"])
        _finish(out)
        return

    trading_root_names = [f for f in root_fields if "trading" in f.lower()]
    solana_root_names = [f for f in root_fields if f.lower() == "solana"]
    out["step0_trading_candidates_at_root"] = trading_root_names
    out["step0_solana_root_present"] = bool(solana_root_names)

    # Introspect "Trading" type (root-level) if present. Имя ТИПА может
    # отличаться от имени ПОЛЯ -- пробуем несколько правдоподобных
    # написаний, а не одно угаданное.
    trading_fields_info: dict = {}
    type_name_candidates = list(dict.fromkeys(
        [tname for tname in trading_root_names]
        + [tname.capitalize() for tname in trading_root_names]
        + (["Trading"] if trading_root_names else [])
    ))
    for tname_candidate in type_name_candidates:
        r_t = introspect_type(api_key, tname_candidate)
        log_ledger(ledger, "step0_trading_type", f"__type({tname_candidate})", r_t)
        if ((r_t.get("body") or {}).get("data") or {}).get("__type"):
            trading_fields_info[tname_candidate] = r_t
    out["step0_trading_type_introspection"] = trading_fields_info

    # Introspect "Solana" type -- may itself expose Trading/Pairs/Trades subfields.
    solana_type_info = None
    if solana_root_names:
        r_s = introspect_type(api_key, "Solana")
        log_ledger(ledger, "step0_solana_type", "__type(Solana)", r_s)
        solana_type_info = r_s
    out["step0_solana_type_introspection"] = solana_type_info

    def field_names_of(type_result: dict | None) -> list[str]:
        if not type_result:
            return []
        t = ((type_result.get("body") or {}).get("data") or {}).get("__type")
        if not t:
            return []
        return [f["name"] for f in (t.get("fields") or [])]

    trading_subfields: list[str] = []
    for r_t in trading_fields_info.values():
        trading_subfields.extend(field_names_of(r_t))
    solana_subfields = field_names_of(solana_type_info)
    out["step0_trading_subfields"] = trading_subfields
    out["step0_solana_subfields"] = solana_subfields
    print(f"[bitquery_probe] Поля типа Trading (если есть): {trading_subfields}")
    print(f"[bitquery_probe] Поля типа Solana (если есть): {solana_subfields}")

    trades_field = next((f for f in trading_subfields + solana_subfields if "trade" in f.lower()), None)
    pairs_field = next((f for f in trading_subfields + solana_subfields if "pair" in f.lower()), None)
    out["step0_conclusion"] = {"trades_field_guess": trades_field, "pairs_field_guess": pairs_field}

    if not trades_field and not pairs_field:
        out["HONEST_ANSWER"] = (
            "Ни в корне, ни под Solana не нашлось поля с 'trade'/'pair' в имени -- "
            f"см. step0_root_fields={root_fields}, step0_solana_subfields={solana_subfields} "
            "для ручного разбора. Платный запрос НЕ делаю без реального имени поля."
        )
        print("[bitquery_probe] " + out["HONEST_ANSWER"])
        _finish(out)
        return

    # ---------- Шаг 1: ОДИН платный запрос -- Trades для нашего пула/окна ----------
    lo = min(p["target"] for p in REFERENCE["points"]) - 5
    hi = max(p["target"] for p in REFERENCE["points"]) + 5
    lo_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(lo))
    hi_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(hi))

    # Пробуем самую вероятную форму запроса (Solana.<trades_field>) --
    # реальная схема аргументов узнаётся из introspect_type выше, но
    # структура where/фильтров у Bitquery V2 стабильна между DEX-полями
    # (MarketAddress/Currency/Block.Time) -- если этот конкретный запрос
    # откажет по схеме (не по тарифу), ошибка Bitquery называет реальное
    # ожидаемое поле, и это тоже честный, полезный результат.
    field_to_use = trades_field or pairs_field
    namespace_guess = "Solana" if field_to_use in solana_subfields else (trading_root_names[0] if trading_root_names else "Trading")
    trade_q = """
    query {
      %s {
        %s(
          where: {
            Trade: {Market: {MarketAddress: {is: "%s"}}}
            Block: {Time: {since: "%s", till: "%s"}}
          }
          limit: {count: 100}
          orderBy: {ascending: Block_Time}
        ) {
          Block { Time }
          Trade {
            Price
            Side { Currency { MintAddress Symbol } }
            Currency { MintAddress Symbol }
          }
          Transaction { Signature }
        }
      }
    }
    """ % (namespace_guess, field_to_use, REFERENCE["pool"], lo_iso, hi_iso)
    out["step1_query_used"] = trade_q
    r1 = gql(api_key, trade_q)
    log_ledger(ledger, "step1_trades_query", f"{namespace_guess}.{field_to_use} for pool {REFERENCE['pool'][:12]}..", r1)
    out["step1_response"] = r1
    body1 = r1.get("body") or {}
    print(f"[bitquery_probe] Шаг 1: http={r1.get('http_status')} errors={body1.get('errors')}")

    rows = None
    data1 = body1.get("data") or {}
    if namespace_guess in data1 and field_to_use in (data1.get(namespace_guess) or {}):
        rows = data1[namespace_guess][field_to_use]
    out["step1_n_rows"] = len(rows) if rows is not None else None

    if r1.get("http_status") != 200 or body1.get("errors") or rows is None:
        out["HONEST_ANSWER"] = (
            f"Запрос {namespace_guess}.{field_to_use} не прошёл (http={r1.get('http_status')}, "
            f"errors={body1.get('errors')}) -- либо неверная форма запроса под реальную схему "
            "(см. errors -- Bitquery обычно называет ожидаемое поле), либо тарифный отказ "
            "(402/403 в errors/http_status). НЕ пробую другие формы вслепую в этом прогоне -- "
            "нужен отдельный раунд с исправленной схемой, если это ошибка формы."
        )
        print("[bitquery_probe] " + out["HONEST_ANSWER"])
        _finish(out)
        return

    if not rows:
        out["HONEST_ANSWER"] = (
            f"0 строк за 5-минутное окно на пуле, который на цепи в это же время дал 6 реальных "
            "сделок -- либо этот пул/минт не покрыт Bitquery (мало объёма/новый токен), либо "
            "фильтр MarketAddress не соответствует реальной схеме этого поля. Не переводим расчёт "
            "на Bitquery без разбора причины."
        )
        print("[bitquery_probe] " + out["HONEST_ANSWER"])
        _finish(out)
        return

    # ---------- Сверка ----------
    def row_time(row: dict) -> int:
        t = row.get("Block", {}).get("Time")
        return calendar.timegm(time.strptime(t, "%Y-%m-%dT%H:%M:%SZ")) if t else 0

    rows_sorted = sorted(rows, key=row_time)
    comparisons = []
    for p in REFERENCE["points"]:
        t = p["target"]
        at_or_before = [r for r in rows_sorted if row_time(r) <= t]
        if not at_or_before:
            comparisons.append({**p, "bitquery_status": "no_trade_at_or_before_t_in_sample"})
            continue
        last = at_or_before[-1]
        bq_price = last.get("Trade", {}).get("Price")
        if bq_price is None:
            comparisons.append({**p, "bitquery_status": "trade_found_but_no_price_field", "raw_row": last})
            continue
        onchain = D(p["onchain_price_usdc_per_mint"])
        try:
            bq = D(str(bq_price))
            diff_pct = abs(bq - onchain) / onchain * 100
            comparisons.append({**p, "bitquery_status": "ok", "bitquery_trade_time": row_time(last),
                                 "bitquery_price": str(bq), "diff_pct": str(diff_pct)})
            print(f"[bitquery_probe] t={t} (+{p['seconds']}с) onchain={onchain} bitquery={bq} diff={diff_pct:.4f}%")
        except Exception as exc:  # noqa: BLE001
            comparisons.append({**p, "bitquery_status": "price_parse_error", "raw_price": bq_price, "error": str(exc)})
    out["comparisons"] = comparisons
    diffs = [D(c["diff_pct"]) for c in comparisons if c.get("bitquery_status") == "ok"]
    out["n_matched"] = len(diffs)
    if diffs:
        out["max_diff_pct"] = str(max(diffs))
        out["mean_diff_pct"] = str(sum(diffs) / len(diffs))

    _finish(out)


def _finish(out: dict) -> None:
    out["generated_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(_scrub_all(json.dumps(out, ensure_ascii=False, indent=2, default=str)))
    print(f"[bitquery_probe] Записано {OUT_PATH}")


if __name__ == "__main__":
    main()
