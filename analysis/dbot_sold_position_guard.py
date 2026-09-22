#!/usr/bin/env python3
"""Владелец: сторож проваленных продаж. Когда DBot 10 раз подряд не
смог продать позицию, TP/SL-задача уходит в state=expired и DBot её
бросает -- токен остаётся на кошельке, пока владелец не заметит вручную
(реальный случай -- ACAT висел 9ч, 9 таких сделок из ~224). Этот скрипт
-- постоянная служба (systemd, NL-хост): каждые 30-60с вычитывает
expired-список DBot, для НАШИХ 9 кошельков (пилот + BATCH-1..8, список
берётся ЖИВЬЁМ из /automation/follow_orders, не хардкодится) проверяет
реальный баланс токена через Helius; если >0 -- пробует продать 100%
через /automation/swap_orders_with_multi_wallets. Каждая попытка -- это
НОВЫЙ вызов (новый маршрут), что и лечит зависания вроде ACAT
(AccountNotEnoughKeys на bin_array в Meteora DLMM из-за устаревшего
маршрута) -- владелец, п.4.

ЧЕСТНОСТЬ ПРО ЭНДПОИНТЫ (по инструкции этой задачи, не проверено этой
сессией напрямую -- сеть на *.dbotx.com из песочницы заблокирована,
подтверждено повторно): /automation/pnl_orders_from_follow_order и
/automation/swap_orders_with_multi_wallets НЕ встречались ни в одном
другом файле этого репозитория (только /automation/follow_orders и
/account/follow_trades подтверждены реальными вызовами в
solana_ledger_run.py). Поля tokenInfo.contract/walletId/walletAddress
-- как дал владелец в задаче. Сырая первая запись expired-списка
сохраняется в data/dbot_sold_position_guard_sample.json при первом же
успешном ответе -- если реальная форма разойдётся с ожидаемой, это
будет видно сразу, а не тихо потеряется.

БЕЗОПАСНОСТЬ: тот же урок, что в solana_dbot_pilot_report.py (инцидент
BITQUERY_APIKEY) -- секрет никогда не подставляется в заголовок без
проверки на переводы строк, и любой текст, который может его содержать,
прогоняется через _scrub_all() перед печатью/записью.

ЭТАП A (границы, по распоряжению владельца):
  1. Потолок проскальзывания ЖЁСТКИЙ -- MAX_SLIPPAGE_CEILING = 0.5. Всё
     выше срезается до потолка (clamp_slippage). Лестниц долей и
     проскальзываний в стороже нет. Повод -- реальный инцидент этой
     сессии: ручная продажа STONKY с maxSlippage 0.99 через пул
     глубиной 0.1418 SOL дала $4.31 вместо $69.15 по другому маршруту.
  2. Успех = БАЛАНС МИНТА В ЦЕПИ УМЕНЬШИЛСЯ. Ответ DBot err:false
     понижен до "принят" и успехом не считается. Если баланс не
     прочитался -- это "неизвестно", а не успех и не отказ.
  3. При неудаче -- GET /automation/swap_orders?ids=... (0 кредитов),
     и в Telegram уходит НАСТОЯЩИЙ errorMessage (на реальных ордерах
     этой сессии он давал ExceededSlippage / E_TOKEN_BALANCE_NOT_ENOUGH).
  4. В алерт о зависании добавлены: котировка Jupiter на весь остаток,
     маршрут, sol_in из выгрузки учёта и ссылка jup.ag для ручной
     продажи. Ничего из этого не выдумывается: не получилось -- в
     тексте написано, почему.
Границы 1-3 проверяются без сети: `--self-test`.

ПРОДАЖА ПО УМОЛЧАНИЮ ВЫКЛЮЧЕНА В КОДЕ (GUARD_LIVE_SELL, по умолчанию
"0") -- тот же принцип, что sc1_launcher.py --confirm-mainnet: реальная
отправка только по явному значению переменной окружения, не по
умолчанию. Конкретное значение на боевом хосте -- в systemd
EnvironmentFile, не здесь."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_ledger_run as ledger  # noqa: E402  -- reuse rpc_call/get_token_holding (Helius)

DBOT_HOST = "https://api-bot-v1.dbotx.com"
SOLANA_CHAIN = "solana"

# Служба живёт ВНЕ git-дерева (иначе почасовые workflow, делающие
# git reset --hard на чужую ветку, выбивают ExecStart из-под неё --
# ровно это уже случалось с зондом). Поэтому оба пути к файлам
# переопределяются окружением, а REPO_ROOT остаётся лишь значением по
# умолчанию для запуска из репозитория.
SAMPLE_PATH = Path(os.environ.get("GUARD_SAMPLE_PATH",
                                   str(REPO_ROOT / "data" / "dbot_sold_position_guard_sample.json")))

DEFAULT_INTERVAL_S = 45
DEFAULT_STUCK_THRESHOLD = 10
DEFAULT_API_DOWN_ALERT_S = 600
DEFAULT_MAX_SLIPPAGE = 0.4
# ЭТАП A, п.1 владельца: потолок проскальзывания ЖЁСТКИЙ. Функция продажи
# принимает slippage, но всё выше потолка срезается до него -- лестниц
# вроде 1.0:0.99 в стороже нет и быть не может. Повод -- реальный
# инцидент этой сессии: ручная продажа STONKY ушла с maxSlippage 0.99
# через пул глубиной 0.1418 SOL и дала $4.31 вместо $69.15 по другому
# маршруту. Потолок -- это защита от повторения, а не настройка.
MAX_SLIPPAGE_CEILING = 0.5
DEFAULT_SELL_RETRIES = 3

WSOL_MINT = "So11111111111111111111111111111111111111112"
# Сколько ждём терминального состояния ордера DBot, прежде чем читать
# баланс: ответ err:false -- это "принят", а не "исполнен".
ORDER_WAIT_S = 45
# Путь к выгрузке учёта: из неё берётся sol_in по (кошелёк, минт) для
# алерта. Файл пишет solana_ledger_run.py. Переопределяется переменной
# окружения, потому что служба живёт вне git-дерева.
TRADES_ALL_PATH = Path(os.environ.get("GUARD_TRADES_PATH", str(REPO_ROOT / "data" / "solana_trades_all.json")))

_ACTIVE_SECRETS: list[str] = []


def _scrub_all(text: str) -> str:
    for s in _ACTIVE_SECRETS:
        if s:
            text = text.replace(s, "[REDACTED_SECRET]")
    return text


log = logging.getLogger("dbot_sold_guard")


class _ScrubbingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _scrub_all(super().format(record))


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_ScrubbingFormatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.handlers = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def check_key_for_injection(key: str, name: str) -> None:
    # Владелец/инцидент этой сессии (BITQUERY_APIKEY): многострочный
    # "ключ" -- признак случайно вставленной подписи дашборда, не сырого
    # значения. Останавливаемся до отправки в заголовок, а не гадаем.
    if any(c in key for c in ("\n", "\r")):
        raise RuntimeError(f"{name} содержит перевод строки -- похоже не на сырой ключ, останавливаюсь")


# ---------- DBot REST ----------

def dbot_get(path: str, params: dict, api_key: str) -> tuple[int | None, dict]:
    for attempt in range(6):
        try:
            resp = requests.get(f"{DBOT_HOST}{path}", params=params, headers={"X-API-KEY": api_key}, timeout=30)
        except Exception as exc:  # noqa: BLE001
            log.warning("dbot_get %s попытка %d/6: %s", path, attempt + 1, _scrub_all(f"{type(exc).__name__}: {exc}"))
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code == 429:
            time.sleep(3 * (attempt + 1))
            continue
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {"non_json_body": _scrub_all(resp.text[:500])}
    return None, {}


def dbot_post(path: str, body: dict, api_key: str) -> tuple[int | None, dict]:
    for attempt in range(3):
        try:
            resp = requests.post(f"{DBOT_HOST}{path}", json=body, headers={"X-API-KEY": api_key}, timeout=30)
        except Exception as exc:  # noqa: BLE001
            log.warning("dbot_post %s попытка %d/3: %s", path, attempt + 1, _scrub_all(f"{type(exc).__name__}: {exc}"))
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code == 429:
            time.sleep(3 * (attempt + 1))
            continue
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {"non_json_body": _scrub_all(resp.text[:500])}
    return None, {}


def extract_items(body) -> list:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in ("res", "data", "results", "list", "items"):
            v = body.get(k)
            if isinstance(v, list):
                return v
    return []


def has_recognized_list_shape(body) -> bool:
    """Владелец, п.2 (баг нумерации страниц дал реальные 11 записей vs
    наш честный "0" -- расхождение формы не должно выглядеть так же,
    как настоящий пустой список). True -- extract_items нашёл ожидаемый
    ключ (пусть даже пустой список внутри -- это честный ноль). False --
    тело вообще не похоже на ожидаемую форму (ни один из res/data/
    results/list/items не нашёлся как список) -- это и есть случай,
    который нельзя молча принимать за ноль."""
    if isinstance(body, list):
        return True
    if isinstance(body, dict):
        return any(isinstance(body.get(k), list) for k in ("res", "data", "results", "list", "items"))
    return False


def fetch_our_wallets(api_key: str) -> dict[str, str]:
    """Владелец: "наши 9 кошельков (пилот + BATCH-1..8)" -- живьём из
    /automation/follow_orders (тот же подтверждённый вызов, что
    solana_ledger_run.py), не хардкодится -- переживает будущий
    BATCH-9 без передеплоя (см. добавление BATCH-6/7/8 в этой же
    сессии). Возвращает {walletAddress: task_name}."""
    status, body = dbot_get("/automation/follow_orders", {}, api_key)
    if status != 200:
        raise RuntimeError(f"follow_orders вернул http={status}, не могу построить список наших кошельков")
    items = extract_items(body)
    out = {}
    for row in items:
        addr = row.get("walletAddress")
        if addr:
            out[addr] = row.get("name") or addr
    if not out:
        raise RuntimeError("follow_orders вернул 0 задач -- пустой список наших кошельков, не продолжаю")
    return out


def fetch_expired_orders(api_key: str) -> tuple[list[dict], bool]:
    """Тот же урок, что в solana_ledger_run.py (диагноз владельца про
    follow_trades): неудачная страница -- НЕ то же самое, что настоящий
    конец списка. dbot_get после 6 неудач возвращает (None, {}) --
    extract_items({})==[] неотличимо от честного конца, поэтому статус
    проверяется отдельно и по неудаче возвращаем complete=False, а не
    тихо считаем, что список кончился.

    Владелец, найденный реальный баг: страницы нумеруются с 0 (docs.
    dbotx.com/reference/copy-tpsl-tasks -- "page, defaults to 0"), а не
    с 1 -- начиная с page=1 мы ВСЕГДА пропускали настоящую первую
    страницу (владелец видел 11 записей в Expired, сторож честно писал
    0 -- это и был весь баг, не сбой API)."""
    out = []
    page = 0
    while True:
        status, body = dbot_get(
            "/automation/pnl_orders_from_follow_order",
            {"chain": SOLANA_CHAIN, "state": "expired", "size": 20, "page": page},
            api_key,
        )
        if status is None:
            return out, False
        if status == 200 and body and not has_recognized_list_shape(body):
            log.warning(
                "expired-страница %d: HTTP 200, тело непустое, но форма НЕ распознана "
                "(нет ни одного из ключей res/data/results/list/items) -- это РАСХОЖДЕНИЕ "
                "ФОРМЫ, не честный ноль. Сырое тело (первые 2000 симв.): %s",
                page, _scrub_all(json.dumps(body, ensure_ascii=False, default=str)[:2000]),
            )
        items = extract_items(body)
        if items and not SAMPLE_PATH.exists():
            SAMPLE_PATH.parent.mkdir(parents=True, exist_ok=True)
            SAMPLE_PATH.write_text(_scrub_all(json.dumps(items[0], ensure_ascii=False, indent=2, default=str)))
            log.info("сырая первая expired-запись сохранена в %s", SAMPLE_PATH.name)
        if not items:
            return out, True
        out.extend(items)
        page += 1
        if page > 500:
            return out, False  # честно: упёрлись в защитный потолок, не в реальный конец


def clamp_slippage(value: float) -> float:
    """Этап A, п.1: всё выше MAX_SLIPPAGE_CEILING срезается до потолка.
    Отдельной функцией -- чтобы правило было в одном месте и его было
    видно в тесте, а не растворялось в теле вызова."""
    if value != value or value <= 0:  # NaN или бессмыслица
        return DEFAULT_MAX_SLIPPAGE
    return min(float(value), MAX_SLIPPAGE_CEILING)


def sell_100_percent(mint: str, wallet_id: str, api_key: str,
                      slippage: float = DEFAULT_MAX_SLIPPAGE) -> tuple[int | None, dict, float]:
    slip = clamp_slippage(slippage)
    if slip != slippage:
        log.warning("проскальзывание %s срезано до потолка %s", slippage, slip)
    body = {
        "chain": SOLANA_CHAIN,
        "pair": mint,
        "walletIdList": [wallet_id],
        "type": "sell",
        "sellPercent": 1.0,
        "maxSlippage": slip,
        "retries": DEFAULT_SELL_RETRIES,
    }
    status, resp = dbot_post("/automation/swap_orders_with_multi_wallets", body, api_key)
    return status, resp, slip


def order_ids_from_response(body) -> list[str]:
    """Идентификаторы ордеров из ответа на продажу. Форма подтверждена
    реальным вызовом в dbot_manual_sell_once.py: {"res": {"ids": [...]}}.
    Если формы нет -- возвращаем пусто и НЕ выдумываем идентификаторы:
    без них мы просто не сможем спросить причину, и это будет видно."""
    if not isinstance(body, dict):
        return []
    res = body.get("res")
    if isinstance(res, dict):
        ids = res.get("ids")
        if isinstance(ids, list):
            return [str(i) for i in ids if i]
    if isinstance(res, list):
        return [str(i) for i in res if isinstance(i, str)]
    return []


def order_status(ids: list[str], api_key: str) -> list[dict]:
    """GET /automation/swap_orders?ids=... -- состояние ордера и ПРИЧИНА
    отказа (docs.dbotx.com/reference/get-swap-order-info): state =
    init/processing/done/fail/expired, плюс swapHash, errorCode,
    errorMessage. 0 кредитов. Без него мы гадали, почему принятый ордер
    не доходит до цепочки. Тот же код, что уже отработал в
    dbot_manual_sell_once.py на реальных ордерах."""
    if not ids:
        return []
    st, body = dbot_get("/automation/swap_orders", {"ids": ",".join(ids)}, api_key)
    if st != 200:
        return [{"id": i, "ошибка_запроса": f"http={st}",
                  "сырое": _scrub_all(str(body)[:300])} for i in ids]
    out = []
    for r in extract_items(body):
        out.append({"id": r.get("id"), "state": r.get("state"), "swapHash": r.get("swapHash"),
                     "errorCode": r.get("errorCode"), "errorMessage": r.get("errorMessage")})
    if not out:
        out = [{"id": i, "ошибка_запроса": "ответ без распознанного списка",
                 "сырое": _scrub_all(json.dumps(body, ensure_ascii=False, default=str)[:400])} for i in ids]
    return out


def wait_order(ids: list[str], api_key: str, timeout_s: int = ORDER_WAIT_S) -> list[dict]:
    """Ждём терминального состояния ордера, а не фиксированную паузу."""
    deadline = time.time() + timeout_s
    last: list[dict] = []
    while True:
        last = order_status(ids, api_key)
        states = {r.get("state") for r in last}
        if not (states & {"init", "processing"}) or time.time() > deadline:
            return last
        time.sleep(3)


def order_reason(rows: list[dict]) -> str:
    """Человеческая причина из состояний ордеров -- именно она идёт в
    Telegram вместо бессодержательного "err=false"."""
    if not rows:
        return "идентификаторов ордера в ответе не было -- причину спросить не у чего"
    parts = []
    for r in rows:
        if r.get("ошибка_запроса"):
            parts.append(f"{r.get('id')}: не смог прочитать состояние ({r['ошибка_запроса']})")
            continue
        bits = [f"state={r.get('state')}"]
        if r.get("errorCode"):
            bits.append(f"errorCode={r['errorCode']}")
        if r.get("errorMessage"):
            bits.append(f"errorMessage={r['errorMessage']}")
        if r.get("swapHash"):
            bits.append(f"tx={r['swapHash']}")
        parts.append(f"{r.get('id')}: " + " ".join(bits))
    return "; ".join(parts)


def parse_sell_response(status: int | None, body: dict) -> tuple[bool, str]:
    """Владелец, п.3: разобрать ответ DBot, не просто залогировать сырое
    тело. Поле err -- тот же подтверждённый паттерн, что у follow_orders/
    follow_trades ({"err": false, "res": [...]}); сообщение -- из
    первого найденного среди msg/message/errorMessage/error/description.
    Реальная форма ЭТОГО конкретного эндпоинта не подтверждена (см.
    докстринг модуля) -- если err вообще отсутствует, не выдумываем
    успех: честно считаем неопределённым/неудачным и печатаем сырое
    тело целиком в audit_log (это делает вызывающий код), не только это
    резюме."""
    if status != 200:
        return False, f"http={status}"
    if not isinstance(body, dict):
        return False, f"неожиданная форма ответа (не объект): {str(body)[:300]}"
    err = body.get("err")
    msg = None
    for k in ("msg", "message", "errorMessage", "error", "description"):
        v = body.get(k)
        if v:
            msg = str(v)
            break
    if err is True:
        return False, msg or "err=true без текста сообщения"
    if err is False:
        # ЭТАП A, п.2 владельца: err:false означает ТОЛЬКО "запрос
        # принят". Реальная продажа подтверждается падением баланса в
        # цепи, и ничем иным. Первый возвращаемый элемент -- "принят",
        # а не "продан"; вызывающий код обязан это различать.
        return True, msg or "принят DBot (не подтверждение продажи)"
    return False, msg or f"поле err отсутствует в ответе, успех не подтверждён: {str(body)[:300]}"


# ---------- диагностика для алерта (этап A, п.4) ----------

def raw_token_balance(wallet: str, mint: str) -> tuple[int, int | None]:
    """Сырой остаток и decimals -- котировка Jupiter считается в сырых
    единицах, а get_token_holding отдаёт uiAmount."""
    res = ledger.rpc_call("getTokenAccountsByOwner",
                           [wallet, {"mint": mint}, {"encoding": "jsonParsed"}])
    total, dec = 0, None
    for acc in (res or {}).get("value") or []:
        try:
            ta = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
            total += int(ta["amount"])
            dec = ta.get("decimals", dec)
        except (KeyError, TypeError, ValueError):
            continue
    return total, dec


def jupiter_quote(mint: str, amount_raw: int, slippage_bps: int = 3000) -> dict:
    """Котировка Jupiter НА ЧТЕНИЕ: есть ли вообще маршрут и по какой
    цене. Подпись не нужна -- она требуется только для отправки.
    Бесплатный тариф Jupiter отклоняет restrictIntermediateTokens=false
    (проверено: NOT_SUPPORTED), поэтому параметр не передаётся."""
    if amount_raw <= 0:
        return {"ошибка": "нулевой остаток -- котировать нечего"}
    hosts = [("lite-api", "https://lite-api.jup.ag/swap/v1/quote"),
             ("quote-api-v6", "https://quote-api.jup.ag/v6/quote")]
    errors = {}
    for name, url in hosts:
        params = {"inputMint": mint, "outputMint": WSOL_MINT, "amount": str(amount_raw),
                   "slippageBps": str(slippage_bps)}
        try:
            r = requests.get(url, params=params, timeout=25)
        except Exception as exc:  # noqa: BLE001
            errors[name] = f"сеть: {type(exc).__name__}"
            continue
        if r.status_code != 200:
            errors[name] = f"http={r.status_code}: {_scrub_all(r.text[:200])}"
            continue
        try:
            b = r.json()
        except ValueError:
            errors[name] = "не JSON"
            continue
        if b.get("error") or b.get("errorCode"):
            errors[name] = str(b.get("error") or b.get("errorCode"))
            continue
        route = " -> ".join(
            f"{(rp.get('swapInfo') or {}).get('label')}"
            for rp in (b.get("routePlan") or [])) or "(маршрут не разобран)"
        out_raw = b.get("outAmount")
        return {"хост": name, "outAmount": out_raw,
                 "SOL": round(int(out_raw) / 1e9, 9) if out_raw else None,
                 "priceImpactPct": b.get("priceImpactPct"),
                 "маршрут": route, "slippageBps": slippage_bps}
    return {"ошибка": "ни один хост Jupiter не дал котировку", "подробности": errors}


def sol_in_from_ledger(wallet: str, mint: str) -> dict:
    """sol_in по (кошелёк, минт) из выгрузки учёта. Файла нет или записи
    нет -- так и пишем, а не подставляем ноль: ноль здесь означал бы
    "вход был бесплатным", и порог спасения посчитался бы неверно."""
    try:
        trades = json.loads(TRADES_ALL_PATH.read_text())
    except FileNotFoundError:
        return {"известно": False, "почему": f"файла учёта нет: {TRADES_ALL_PATH}"}
    except (ValueError, OSError) as exc:
        return {"известно": False, "почему": f"файл учёта не читается: {type(exc).__name__}"}
    best = None
    for t in trades if isinstance(trades, list) else []:
        if t.get("wallet") == wallet and t.get("mint") == mint and t.get("sol_in") is not None:
            if best is None or (t.get("buy_block_time") or 0) > (best.get("buy_block_time") or 0):
                best = t
    if best is None:
        return {"известно": False, "почему": "в выгрузке учёта нет сделки с этим кошельком и минтом"}
    return {"известно": True, "sol_in": best.get("sol_in"),
             "buy_block_time": best.get("buy_block_time"),
             "task_name": best.get("task_name")}


def hang_diagnostics(wallet: str, mint: str) -> dict:
    """Всё, что нужно владельцу в алерте о зависании: котировка Jupiter
    на ВЕСЬ остаток, sol_in из учёта, маршрут и ссылка на ручную
    продажу. Любой сбой здесь не должен ронять сторож -- поэтому каждый
    источник обёрнут и его отказ виден в тексте."""
    d: dict = {"ссылка": f"https://jup.ag/swap/{mint}-SOL"}
    try:
        raw, dec = raw_token_balance(wallet, mint)
        d["сырой_остаток"] = raw
        d["decimals"] = dec
    except Exception as exc:  # noqa: BLE001
        d["сырой_остаток_ошибка"] = _scrub_all(f"{type(exc).__name__}: {exc}")[:200]
        raw = 0
    d["котировка_jupiter"] = jupiter_quote(mint, raw) if raw > 0 else {"ошибка": "остаток не прочитан"}
    d["вход"] = sol_in_from_ledger(wallet, mint)
    return d


def hang_alert_text(mint: str, wallet: str, task_name: str, attempts: int,
                     balance: float, last_err: str, diag: dict) -> str:
    q = diag.get("котировка_jupiter") or {}
    if q.get("SOL") is not None:
        q_line = (f"Jupiter сейчас даёт {q['SOL']} SOL (маршрут: {q.get('маршрут')}, "
                  f"влияние на цену {q.get('priceImpactPct')}, slippageBps {q.get('slippageBps')})")
    else:
        q_line = f"Котировки Jupiter нет: {q.get('ошибка')} {q.get('подробности') or ''}".strip()
    v = diag.get("вход") or {}
    v_line = (f"вход был {v['sol_in']} SOL" if v.get("известно")
              else f"вход неизвестен ({v.get('почему')})")
    return (f"По токену {mint} на кошельке {wallet} ({task_name}) {attempts} кругов подряд "
            f"без продажи (баланс {balance}). Последний ответ DBot: {last_err}\n"
            f"{q_line}\n{v_line}\nПродать вручную: {diag.get('ссылка')}")


# ---------- Telegram (владелец: только 3 типа событий, ничего больше) ----------

def send_telegram(token: str | None, chat_id: str | None, text: str) -> None:
    if not token or not chat_id:
        log.info("TELEGRAM (не настроен, только лог): %s", text)
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning("telegram sendMessage http=%s body=%s", resp.status_code, _scrub_all(resp.text[:300]))
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram sendMessage исключение: %s", _scrub_all(f"{type(exc).__name__}: {exc}"))


# ---------- состояние (SQLite -- п.5 владельца, "чтобы не продавать дважды") ----------

def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS positions (
            key TEXT PRIMARY KEY,
            wallet_address TEXT NOT NULL,
            wallet_id TEXT,
            mint TEXT NOT NULL,
            task_name TEXT,
            state TEXT NOT NULL DEFAULT 'watching',
            first_seen_utc TEXT NOT NULL,
            last_checked_utc TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_balance REAL,
            last_sell_attempt_utc TEXT,
            last_sell_response TEXT,
            closed_utc TEXT,
            stuck_alerted_at_attempts INTEGER NOT NULL DEFAULT 0,
            zero_streak INTEGER NOT NULL DEFAULT 0,
            last_sell_ok INTEGER,
            last_sell_error TEXT,
            sell_fail_alerted INTEGER NOT NULL DEFAULT 0
        )"""
    )
    # Владелец, правки: на VPS уже есть живая база со старой схемой --
    # CREATE TABLE IF NOT EXISTS новые колонки в неё не добавит. Миграция
    # безопасно повторяема (дубликат колонки -- OperationalError, игнор).
    for ddl in (
        "ALTER TABLE positions ADD COLUMN zero_streak INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE positions ADD COLUMN last_sell_ok INTEGER",
        "ALTER TABLE positions ADD COLUMN last_sell_error TEXT",
        "ALTER TABLE positions ADD COLUMN sell_fail_alerted INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    return conn


def record_key(rec: dict, wallet: str, mint: str) -> str:
    rid = rec.get("id") or rec.get("_id")
    return str(rid) if rid else f"{wallet}:{mint}"


def db_get(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM positions WHERE key=?", (key,)).fetchone()


def db_mark_closed(conn: sqlite3.Connection, key: str, wallet: str, wallet_id: str, mint: str, task_name: str) -> None:
    conn.execute(
        """INSERT INTO positions (key, wallet_address, wallet_id, mint, task_name, state, first_seen_utc,
                                   last_checked_utc, last_balance, closed_utc)
           VALUES (?, ?, ?, ?, ?, 'closed', ?, ?, 0, ?)
           ON CONFLICT(key) DO UPDATE SET state='closed', last_checked_utc=excluded.last_checked_utc,
                                           last_balance=0, closed_utc=excluded.closed_utc""",
        (key, wallet, wallet_id, mint, task_name, now_utc(), now_utc(), now_utc()),
    )
    conn.commit()


def db_upsert_watching(conn: sqlite3.Connection, key: str, wallet: str, wallet_id: str, mint: str,
                        task_name: str, balance: float) -> int:
    # zero_streak=0 -- владелец, п.5: ненулевой баланс всегда сбрасывает
    # счётчик подряд-нулевых чтений (закрываем только после ДВУХ подряд).
    conn.execute(
        """INSERT INTO positions (key, wallet_address, wallet_id, mint, task_name, state, first_seen_utc,
                                   last_checked_utc, attempts, last_balance, zero_streak)
           VALUES (?, ?, ?, ?, ?, 'watching', ?, ?, 1, ?, 0)
           ON CONFLICT(key) DO UPDATE SET state='watching', wallet_id=excluded.wallet_id,
                                           last_checked_utc=excluded.last_checked_utc,
                                           attempts=positions.attempts + 1, last_balance=excluded.last_balance,
                                           zero_streak=0""",
        (key, wallet, wallet_id, mint, task_name, now_utc(), now_utc(), balance),
    )
    conn.commit()
    return conn.execute("SELECT attempts FROM positions WHERE key=?", (key,)).fetchone()["attempts"]


def db_bump_zero_streak(conn: sqlite3.Connection, key: str, streak: int) -> None:
    """Владелец, п.5: первое нулевое чтение после watching -- не закрывать
    сразу, запомнить счётчик и ждать ещё одно подряд нулевое чтение."""
    conn.execute("UPDATE positions SET zero_streak=?, last_checked_utc=? WHERE key=?", (streak, now_utc(), key))
    conn.commit()


def db_record_sell(conn: sqlite3.Connection, key: str, response_summary: str, ok: bool, error_text: str) -> None:
    conn.execute(
        "UPDATE positions SET last_sell_attempt_utc=?, last_sell_response=?, last_sell_ok=?, last_sell_error=? "
        "WHERE key=?",
        (now_utc(), response_summary, 1 if ok else 0, error_text, key),
    )
    conn.commit()


def db_mark_sell_fail_alerted(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("UPDATE positions SET sell_fail_alerted=1 WHERE key=?", (key,))
    conn.commit()


def db_mark_stuck_alerted(conn: sqlite3.Connection, key: str, attempts: int) -> None:
    conn.execute("UPDATE positions SET stuck_alerted_at_attempts=? WHERE key=?", (attempts, key))
    conn.commit()


# ---------- аудит-лог (JSONL, вне SQLite -- человекочитаемая история событий) ----------

def audit_log(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {"utc": now_utc(), **event}
    with path.open("a", encoding="utf-8") as f:
        f.write(_scrub_all(json.dumps(event, ensure_ascii=False, default=str)) + "\n")


# ---------- один цикл (используется и --check-only, и боевым циклом) ----------

def run_cycle(conn: sqlite3.Connection | None, audit_path: Path | None, our_wallets: dict[str, str],
              dbot_key: str, live_sell: bool, stuck_threshold: int, telegram_token: str | None,
              telegram_chat_id: str | None, check_only: bool = False) -> tuple[list[dict], bool]:
    """Один проход: выгрузить expired, отфильтровать на наши кошельки,
    проверить баланс через Helius, при необходимости продать/уведомить.
    check_only=True -- ничего не пишет в SQLite и не продаёт (чистое
    чтение для --check-only). Возвращает (строки для таблицы, fetch_ok)."""
    expired, fetch_ok = fetch_expired_orders(dbot_key)
    if not fetch_ok:
        log.warning("expired-список получен НЕПОЛНОСТЬЮ в этом цикле (сбой сети/API посреди пагинации)")
    ours = [r for r in expired if r.get("walletAddress") in our_wallets]
    log.info("expired всего=%d, из них наши кошельки=%d (fetch_ok=%s)", len(expired), len(ours), fetch_ok)

    rows_for_table = []
    for rec in ours:
        wallet = rec.get("walletAddress")
        wallet_id = rec.get("walletId")
        mint = (rec.get("tokenInfo") or {}).get("contract")
        task_name = our_wallets.get(wallet, wallet)
        if not mint or not wallet_id:
            log.warning("expired-запись без tokenInfo.contract или walletId, пропускаю: %s", _scrub_all(str(rec)[:300]))
            continue
        key = record_key(rec, wallet, mint)

        row = None if check_only else db_get(conn, key)
        if row is not None and row["state"] == "closed":
            continue  # уже закрыта в прошлом цикле -- не дёргаем Helius снова

        try:
            balance = ledger.get_token_holding(wallet, mint)
        except Exception as exc:  # noqa: BLE001
            log.warning("Helius getTokenAccountsByOwner упал для %s/%s: %s", wallet[:10], mint[:10],
                        _scrub_all(f"{type(exc).__name__}: {exc}"))
            continue

        rows_for_table.append({"task": task_name, "wallet": wallet, "mint": mint, "balance": balance})

        if check_only:
            continue

        if balance <= 0:
            # Владелец, п.5: закрывать (state=closed + "продал") только
            # после ДВУХ подряд нулевых чтений -- одно нулевое чтение
            # после реально watching-позиции может быть ложным (гонка с
            # Helius/индексацией), а не подтверждённой продажей.
            if row is None:
                # никогда не видели с ненулевым балансом -- честный ноль
                # сразу, закрывать/уведомлять нечего (не "продал", просто
                # нечего держать).
                db_mark_closed(conn, key, wallet, wallet_id, mint, task_name)
                continue
            zero_streak = (row["zero_streak"] or 0) + 1
            if zero_streak < 2:
                db_bump_zero_streak(conn, key, zero_streak)
                log.info("баланс 0 (%d/2 подряд, ещё не закрываю): %s кошелёк=%s токен=%s",
                          zero_streak, task_name, wallet[:10], mint[:10])
                continue
            was_watching = row["state"] == "watching"
            prior_balance = row["last_balance"]
            first_seen = row["first_seen_utc"]
            db_mark_closed(conn, key, wallet, wallet_id, mint, task_name)
            if was_watching:
                msg = (f"Сторож продал зависшую позицию: кошелёк {wallet} ({task_name}), "
                       f"токен {mint}, висела с {first_seen}, баланс до продажи {prior_balance}.")
                log.info(msg)
                send_telegram(telegram_token, telegram_chat_id, msg)
                audit_log(audit_path, {"event": "position_closed", "task": task_name, "wallet": wallet,
                                        "mint": mint, "held_since_utc": first_seen, "balance_before": prior_balance})
            continue

        attempts = db_upsert_watching(conn, key, wallet, wallet_id, mint, task_name, balance)
        log.info("зависшая позиция: %s кошелёк=%s токен=%s баланс=%s попытка=%d",
                  task_name, wallet[:10], mint[:10], balance, attempts)

        if attempts > 0 and attempts % stuck_threshold == 0:
            row2 = db_get(conn, key)
            if row2 is None or row2["stuck_alerted_at_attempts"] != attempts:
                # Владелец, п.3: текст последнего ответа DBot -- в тот же алерт.
                last_err = (row2["last_sell_error"] if row2 else None) or "(ещё ни разу не пробовали продать)"
                # ЭТАП A, п.4: в алерт идёт не только текст DBot, но и
                # ответ на вопрос "а можно ли это вообще продать и за
                # сколько": котировка Jupiter на весь остаток, маршрут,
                # sol_in из учёта и ссылка на ручную продажу.
                diag = hang_diagnostics(wallet, mint)
                msg = hang_alert_text(mint, wallet, task_name, attempts, balance,
                                       _scrub_all(last_err), diag)
                audit_log(audit_path, {"event": "stuck_alert", "task": task_name, "wallet": wallet,
                                        "mint": mint, "attempts": attempts, "balance": balance,
                                        "диагностика": diag})
                log.error(msg)
                send_telegram(telegram_token, telegram_chat_id, msg)
                db_mark_stuck_alerted(conn, key, attempts)

        if live_sell:
            status, resp, slip_used = sell_100_percent(mint, wallet_id, dbot_key)
            accepted, accept_text = parse_sell_response(status, resp)

            # ЭТАП A, п.2: "принят" -- это НЕ "продан". Ждём терминального
            # состояния ордера, затем читаем баланс в цепи. Успехом
            # считается только уменьшение баланса.
            ids = order_ids_from_response(resp)
            order_rows = wait_order(ids, dbot_key) if ids else []
            reason = order_reason(order_rows)
            try:
                balance_after = ledger.get_token_holding(wallet, mint)
            except Exception as exc:  # noqa: BLE001
                balance_after = None
                log.warning("баланс после продажи не прочитался: %s",
                            _scrub_all(f"{type(exc).__name__}: {exc}"))
            # ЭТАП A, п.2: успех = баланс уменьшился. None (не
            # прочитался) -- это НЕ успех и НЕ отказ: неизвестность, её и
            # пишем, чтобы следующий круг перепроверил.
            if balance_after is None:
                sold = None
                verdict = "НЕИЗВЕСТНО: баланс после попытки не прочитался"
            elif balance_after < balance - 1e-12:
                sold = True
                verdict = f"ПРОДАНО: баланс {balance} -> {balance_after}"
            else:
                sold = False
                verdict = f"НЕ ПРОДАНО: баланс не изменился ({balance})"

            err_text = verdict if sold else f"{verdict}; принято={accepted} ({accept_text}); ордер: {reason}"
            summary = _scrub_all(json.dumps(
                {"http_status": status, "maxSlippage": slip_used, "принят": accepted,
                 "ордера": order_rows, "баланс_до": balance, "баланс_после": balance_after},
                ensure_ascii=False, default=str)[:800])
            db_record_sell(conn, key, summary, bool(sold), _scrub_all(err_text))
            audit_log(audit_path, {"event": "sell_attempt", "task": task_name, "wallet": wallet,
                                    "mint": mint, "balance_before": balance,
                                    "balance_after": balance_after, "http_status": status,
                                    "max_slippage": slip_used, "accepted": accepted,
                                    "accept_text": accept_text, "orders": order_rows,
                                    "sold_onchain": sold, "verdict": verdict})
            if sold:
                log.info("продажа ПОДТВЕРЖДЕНА ЦЕПЬЮ: %s", verdict)
            else:
                log.warning("продажа не подтверждена цепью: %s | ордер: %s",
                            verdict, _scrub_all(reason))
                # Владелец, п.3: сообщение в Телеграм сразу при отказе,
                # один раз на позицию (флаг sell_fail_alerted в SQLite).
                # В тексте -- НАСТОЯЩИЙ errorMessage из swap_orders, а не
                # наше "err=false".
                row3 = db_get(conn, key)
                if row3 is None or not row3["sell_fail_alerted"]:
                    fail_msg = (f"Сторож не смог продать {mint} на {wallet} ({task_name}).\n"
                                f"{verdict}\nОтвет DBot на запрос: принят={accepted} ({accept_text})\n"
                                f"Состояние ордера: {_scrub_all(reason)}\n"
                                f"maxSlippage {slip_used} (потолок {MAX_SLIPPAGE_CEILING})")
                    send_telegram(telegram_token, telegram_chat_id, fail_msg)
                    db_mark_sell_fail_alerted(conn, key)
        else:
            log.info("DRY-RUN (GUARD_LIVE_SELL не включён): продал бы 100%% %s на %s", mint[:10], wallet[:10])

    return rows_for_table, fetch_ok


# ---------- main ----------

def load_env_config() -> dict:
    dbot_key = os.environ.get("DBOT_API_KEY", "")
    helius_key = os.environ.get("HELIUS_API", "")
    if not dbot_key:
        raise RuntimeError("DBOT_API_KEY пуст в окружении")
    if not helius_key:
        raise RuntimeError("HELIUS_API пуст в окружении")
    check_key_for_injection(dbot_key, "DBOT_API_KEY")
    check_key_for_injection(helius_key, "HELIUS_API")
    _ACTIVE_SECRETS.append(dbot_key)
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN") or None
    if telegram_token:
        _ACTIVE_SECRETS.append(telegram_token)
    return {
        "dbot_key": dbot_key,
        "interval_s": int(os.environ.get("GUARD_INTERVAL_S", DEFAULT_INTERVAL_S)),
        "live_sell": os.environ.get("GUARD_LIVE_SELL", "0").strip() == "1",
        "stuck_threshold": int(os.environ.get("GUARD_STUCK_THRESHOLD", DEFAULT_STUCK_THRESHOLD)),
        "api_down_alert_s": int(os.environ.get("GUARD_API_DOWN_ALERT_S", DEFAULT_API_DOWN_ALERT_S)),
        "state_dir": Path(os.environ.get("GUARD_STATE_DIR", str(REPO_ROOT / "state"))),
        "telegram_token": telegram_token,
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID") or None,
    }


def print_check_table(rows: list[dict]) -> None:
    if not rows:
        print("Наших зависших позиций в expired-списке нет (0 записей с ненулевым остатком -- продавать нечего).")
        return
    print(f"{'задача':<12} {'кошелёк':<16} {'токен':<16} {'баланс сейчас':>16}")
    for r in rows:
        print(f"{r['task']:<12} {r['wallet'][:12]+'..':<16} {r['mint'][:12]+'..':<16} {r['balance']:>16}")


def self_test() -> None:
    """Границы этапа A -- проверяются без сети и без ключей, чтобы
    правило "выше 0.5 не уходит" было доказано, а не заявлено."""
    checks: list[tuple[str, bool, str]] = []

    def chk(name: str, cond: bool, got: str = "") -> None:
        checks.append((name, bool(cond), got))

    chk("0.4 остаётся 0.4", clamp_slippage(0.4) == 0.4, str(clamp_slippage(0.4)))
    chk("0.5 остаётся 0.5", clamp_slippage(0.5) == 0.5, str(clamp_slippage(0.5)))
    chk("0.99 срезается до 0.5", clamp_slippage(0.99) == MAX_SLIPPAGE_CEILING, str(clamp_slippage(0.99)))
    chk("1.0 срезается до 0.5", clamp_slippage(1.0) == MAX_SLIPPAGE_CEILING, str(clamp_slippage(1.0)))
    chk("0 и мусор -> значение по умолчанию",
        clamp_slippage(0) == DEFAULT_MAX_SLIPPAGE and clamp_slippage(-1) == DEFAULT_MAX_SLIPPAGE,
        f"{clamp_slippage(0)}/{clamp_slippage(-1)}")
    chk("потолок не выше 0.5", MAX_SLIPPAGE_CEILING <= 0.5, str(MAX_SLIPPAGE_CEILING))

    ok, txt = parse_sell_response(200, {"err": False})
    chk("err:false -> принят, а не продан", ok and "не подтверждение продажи" in txt, txt)
    ok2, txt2 = parse_sell_response(200, {"res": {"ids": ["x"]}})
    chk("нет поля err -> не успех", not ok2, txt2)
    chk("http!=200 -> не успех", not parse_sell_response(500, {})[0], "")

    chk("ids из {'res':{'ids':[...]}}",
        order_ids_from_response({"res": {"ids": ["a", "b"]}}) == ["a", "b"], "")
    chk("ids из мусора -> пусто", order_ids_from_response({"res": 1}) == [], "")
    chk("ids из None -> пусто", order_ids_from_response(None) == [], "")

    r = order_reason([{"id": "1", "state": "fail", "errorCode": "E1", "errorMessage": "ExceededSlippage"}])
    chk("причина отказа попадает в текст", "ExceededSlippage" in r and "fail" in r, r)
    chk("без ордеров -- честно сказано", "спросить не у чего" in order_reason([]), "")

    bad = 0
    for name, good, got in checks:
        mark = "ok  " if good else "СБОЙ"
        print(f"  [{mark}] {name}" + (f"  -> {got}" if got and not good else ""))
        if not good:
            bad += 1
    print(f"самопроверка границ: {len(checks) - bad}/{len(checks)} пройдено")
    if bad:
        raise SystemExit(f"самопроверка не пройдена: {bad} проверок из {len(checks)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true",
                         help="Только прочитать expired-список и показать таблицу баланса -- ничего не продаёт, "
                              "не пишет в SQLite.")
    parser.add_argument("--once", action="store_true", help="Один цикл и выход (для теста), не бесконечный цикл.")
    parser.add_argument("--self-test", action="store_true",
                         help="Проверить границы без сети и без ключей (потолок проскальзывания, разбор ответа, "
                              "разбор состояния ордера) и выйти.")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return

    setup_logging()
    cfg = load_env_config()

    our_wallets = fetch_our_wallets(cfg["dbot_key"])
    log.info("наших кошельков (живьём из follow_orders): %d -- %s", len(our_wallets), sorted(our_wallets.values()))

    if args.check_only:
        rows, _ = run_cycle(conn=None, audit_path=None, our_wallets=our_wallets, dbot_key=cfg["dbot_key"],
                             live_sell=False, stuck_threshold=cfg["stuck_threshold"],
                             telegram_token=None, telegram_chat_id=None, check_only=True)
        print_check_table(rows)
        return

    state_dir = cfg["state_dir"]
    db_path = state_dir / "dbot_sold_position_guard.sqlite3"
    audit_path = state_dir / "dbot_sold_position_guard_audit.jsonl"
    conn = open_db(db_path)
    log.info("состояние: %s (SQLite), аудит-лог: %s", db_path, audit_path)
    log.info("GUARD_LIVE_SELL=%s интервал=%dс порог-завис=%d", cfg["live_sell"], cfg["interval_s"], cfg["stuck_threshold"])

    last_dbot_ok_ts = time.monotonic()
    api_down_alerted = False
    cycle_n = 0

    while True:
        cycle_start = time.monotonic()
        cycle_n += 1
        try:
            # Владелец, п.4: список наших кошельков -- каждый цикл (один
            # GET follow_orders), не раз в N кругов -- любое изменение
            # состава задач подхватывается сразу.
            our_wallets = fetch_our_wallets(cfg["dbot_key"])

            _, fetch_ok = run_cycle(conn, audit_path, our_wallets, cfg["dbot_key"], cfg["live_sell"],
                                     cfg["stuck_threshold"], cfg["telegram_token"], cfg["telegram_chat_id"],
                                     check_only=False)

            if fetch_ok:
                last_dbot_ok_ts = time.monotonic()
                if api_down_alerted:
                    send_telegram(cfg["telegram_token"], cfg["telegram_chat_id"], "DBot API снова отвечает.")
                    api_down_alerted = False
            else:
                down_for_s = time.monotonic() - last_dbot_ok_ts
                if down_for_s > cfg["api_down_alert_s"] and not api_down_alerted:
                    msg = f"DBot API недоступен дольше {int(down_for_s / 60)} минут."
                    log.error(msg)
                    send_telegram(cfg["telegram_token"], cfg["telegram_chat_id"], msg)
                    api_down_alerted = True

        except Exception:  # noqa: BLE001
            log.exception("сбой в цикле %d -- продолжаю со следующего (не роняю процесс)", cycle_n)

        if args.once:
            return
        elapsed = time.monotonic() - cycle_start
        time.sleep(max(1.0, cfg["interval_s"] - elapsed))


if __name__ == "__main__":
    main()
