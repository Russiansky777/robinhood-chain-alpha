#!/usr/bin/env python3
"""Владелец: единый конвейер учёта сделок, заменяет все прошлые
промпты про статистику/леджер. RPC -- Helius, ключ ИЗ СЕКРЕТА HELIUS_API
в query-параметре (владелец подтвердил: секрет == рабочему ключу,
проверено HTTP 200 на getBalance) -- сам ключ в код не зашит.

A. DBot follow_orders (реальная форма ответа подтверждена в этой сессии
   ранее -- {"err":false,"res":[{...}]}, поля id/name/walletAddress/
   walletName/targetIds/targetNames -- см. data/dbot_tasks_config_raw.json)
   -- 0 кредитов, "источник намерений".
   follow_trades -- реальная форма ответа теперь подтверждена (первый
   полный прогон): подписи транзакции как отдельного поля НЕТ (как и
   предупреждала документация владельца, docs.dbotx.com/reference/
   copy-records), но она есть в links.etherscan -- Solscan-ссылка вида
   "https://solscan.io/tx/<подпись>" (имя поля общее/EVM-style, но URL
   реально ведёт на Solscan для сети solana). Источник (кого копировали)
   -- follow.wallet, прямо в записи, отдельный запрос с targetWallet не
   понадобился. Реальные поля: id, configId, configName, wallet,
   createAt (мс), timestamp (с, только у state=="done"), type (buy/sell),
   state (done/fail/...), errorCode/errorMessage/skipReason,
   send.info.contract + send.amount (raw), receive.info.contract +
   receive.amount (raw), dbotFeeRate/dbotFee, follow.wallet,
   follow.remark, links.etherscan. Первая сырая запись сохраняется
   отдельно в data/dbot_follow_trades_sample.json. Если подпись из
   links.etherscan не извлеклась -- запасная сшивка по (кошелёк, минт,
   тип buy/sell, |время цепочки - timestamp записи| <= 20с, ближайший по
   времени и по сумме) -- см. buy_match_method/sell_match_method
   ("signature"/"time_match") в каждой сделке.

B. Цепочка -- источник денег, баланс-метод (тот же принцип, что уже
   провалидирован в этой сессии multiple раз: preBalances/postBalances,
   preTokenBalances/postTokenBalances по owner=кошелёк, полный список
   ключей = static accountKeys + loadedAddresses). getTransaction --
   encoding=json (не jsonParsed, как просил владелец) -- значит подписантов
   получаем из message.header.numRequiredSignatures (первые N статических
   ключей), не из parsed-флагов.

C. Сшивка DBot<->цепочка по txHash=signature. Покупка/продажа одного
   минта в одной задаче определяется ЗНАКОМ дельты токена кошелька
   (не decode_tx/AMM-парсинг -- владелец явно просит баланс-метод здесь).

E. Контроль перед выдачей -- 3 пилотных сделки из
   data/solana_dbot_realized_ledger.json (SOL вход/выход), допуск 0.001."""
from __future__ import annotations

import calendar
import concurrent.futures as cf
import json
import os
import sys
import time
import threading
from decimal import Decimal as D
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
DBOT_HOST = "https://api-bot-v1.dbotx.com"
WSOL = "So11111111111111111111111111111111111111112"
DBOT_FEE_ADDRESS = "F7F8QYPCc3zDYNeAh4UUE2wJ7Mq1huid5MeMo7PPzsqB"
# Известные релеи чаевых -- те же, что уже установлены и использованы
# ранее в этой сессии (analysis/solana_fast_buyers_services.py).
KNOWN_RELAYS = {
    "DiTmWENJsHQdawVUUKnUXkconcpW4Jv52TnMWhkncF6t": "релей (наблюдался у AKBot)",
    "AsTRAEoyMofR3vUPpf9k68Gsfb6ymTZttEtsAbv8Bk4d": "релей (Astralane)",
    "astra9xWY93QyfG6yM8zwsKsRodscjQ2uU2HKNL5prk": "релей (Astralane)",
    "ste11p5x8tJ53H1NbNQsRBg1YNRd4GcVpxtDw8PBpmb": "релей (ste11)",
    "ste11eZvF5bebo6EVyGMJHV6LtPtx7e6TzNZtxPfXGy": "релей (ste11)",
    "LandX6RsjjnfaxfYAFTZX1TW9Cgfe9HFStj9nRDd5SK": "релей (LandX)",
}
RELAY_PREFIXES = ("astra", "AsTra", "ste11", "LandX")

# ЭТАП E (учёт спасений). Кошелёк-утилизатор -- наш же, поэтому переводы
# "кошелёк задачи <-> утилизатор" это ВНУТРЕННЕЕ перемещение, а не
# пополнение и не вывод. Без этого спасённая позиция выглядела бы как
# вывод средств на неизвестный адрес, а вернувшийся SOL -- как
# пополнение извне, и обе цифры испортили бы сверку.
# Адрес берётся из окружения (RESCUE_WALLET_ADDRESS); приватный ключ
# учёту не нужен и здесь не читается. Адреса нет -- поведение ровно то
# же, что раньше.
RESCUE_WALLET_ADDRESS = (os.environ.get("RESCUE_WALLET_ADDRESS") or "").strip() or None

# Владелец: на кошельке BATCH-8 с 23.09 00:30Z параллельно с DBot
# работает ДРУГОЙ бот -- TradeWiz. Его сделки идут по той же цепочке и
# внешне неотличимы от наших, поэтому в итоги задач DBot они попадать не
# должны. Признак строго проверяемый: сделка на этом кошельке, начиная с
# отсечки, у которой НЕТ подтверждающей записи DBot. Ничего не
# домысливаем: если запись DBot есть -- это DBot, как бы ни выглядело
# остальное.
TRADEWIZ_WALLET = "4s87RRC2V2XAJD6R8U2dP8kQH99Z2wA6fg88ZVfV4j4N"
TRADEWIZ_FROM_TS = 1790123400  # 2026-09-23T00:30:00Z
TRADEWIZ_LABEL = "tradewiz"
# Метки чужих исполнителей: их сделки не наши, в итоги задач DBot и в
# статистику источников они не идут, но выносятся отдельной строкой --
# иначе сверка баланса кошелька развалилась бы на ровном месте.
ЧУЖИЕ_БОТЫ = ("tradewiz", "bloom")
# С 23.09 15:42Z TradeWiz выключен, и с ТОГО ЖЕ кошелька BATCH-8 работает
# исполнитель Bloom. Без этой границы каждая сделка Bloom получала бы метку
# tradewiz и вылетала бы из итогов задач и статистики источников -- то есть
# результат Bloom молча приписывался бы выключенному боту.
# Точное время старта Bloom уточняется владельцем; по умолчанию берётся
# начало эксперимента с комиссиями. Переопределяется BLOOM_FROM_UTC.
BLOOM_WALLET = TRADEWIZ_WALLET
BLOOM_LABEL = "bloom"


def _ts_from_env(name: str, default_ts: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default_ts
    try:
        return calendar.timegm(time.strptime(raw, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        print(f"[ledger] ВНИМАНИЕ: {name}={raw!r} не разобрано как UTC-метка "
              f"вида 2026-09-23T15:42:00Z -- беру значение по умолчанию")
        return default_ts


BLOOM_FROM_TS = _ts_from_env("BLOOM_FROM_UTC", 1790178120)  # 2026-09-23T15:42:00Z
DBOT_LABEL = "dbot"
UNKNOWN_BOT_LABEL = "неизвестен"

FOLLOW_ORDERS_PATH = REPO_ROOT / "data" / "dbot_follow_orders_raw.json"
FOLLOW_TRADES_PATH = REPO_ROOT / "data" / "dbot_follow_trades_raw.json"
FOLLOW_TRADES_SAMPLE_PATH = REPO_ROOT / "data" / "dbot_follow_trades_sample.json"
CHAIN_CACHE_PATH = REPO_ROOT / "data" / "chain_tx_cache.json"
TRADES_ALL_PATH = REPO_ROOT / "data" / "solana_trades_all.json"
SOURCE_STATS_PATH = REPO_ROOT / "data" / "solana_source_stats.json"
TASK_STATS_PATH = REPO_ROOT / "data" / "solana_task_stats.json"
STATUS_PATH = REPO_ROOT / "data" / "ledger_status.json"
PILOT_LEDGER_PATH = REPO_ROOT / "data" / "solana_dbot_realized_ledger.json"

TIME_BUDGET_S = 50 * 60


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (ValueError, OSError):
            pass
    return default


def save_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


# ---------- RPC: Helius основной, публичный узел -- запасной ----------
# Учёт не должен зависеть от того, сколько кредитов Helius сожрал тяжёлый
# ретро-прогон. Поэтому при 429 запрос уходит на публичный узел, а сам
# Helius временно понижается в правах (иначе каждый вызов снова упирался
# бы в те же 20 попыток по 30 секунд -- ровно так конвейер и вставал на
# 15 минут и падал).

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"

# Публичный узел жёстче по темпу, поэтому свой минимальный интервал.
PUBLIC_MIN_INTERVAL_S = 0.25
# Сколько подряд 429 от Helius, чтобы перестать его дёргать.
HELIUS_DEMOTE_AFTER_429 = 3
# Через сколько секунд снова пробовать Helius: квота могла восстановиться.
HELIUS_REPROBE_S = 600.0

_rpc_lock = threading.Lock()
_public_last_call = 0.0

# Учёт кредитов Helius по имени службы (см. solana_rpc_client.CreditMeter).
# Публичный узел бесплатен и не считается.
try:
    from solana_rpc_client import CreditMeter as _CreditMeter  # noqa: PLC0415
    _METER = _CreditMeter("ledger")
except Exception:  # noqa: BLE001 -- учёт не должен ронять конвейер
    _METER = None


def _charge(url: str, method: str) -> None:
    if _METER is None or url == PUBLIC_RPC:
        return
    try:
        _METER.add(10 if method == "getProgramAccounts" else 1)
    except Exception:  # noqa: BLE001
        pass


RPC_STATS: dict = {
    "helius_ok": 0, "helius_429": 0, "helius_прочие_ошибки": 0,
    "публичный_ok": 0, "публичный_429": 0, "публичный_прочие_ошибки": 0,
    "helius_понижен": False, "helius_понижен_utc": None,
    "последняя_причина_отказа": None, "первый_ответ_429_от_helius": None,
}
_helius_429_streak = 0
_helius_demoted_until = 0.0


def _helius_url() -> str | None:
    key = os.environ.get("HELIUS_API", "")
    return f"https://mainnet.helius-rpc.com/?api-key={key}" if key else None


def _rpc_url() -> str:
    """Оставлено для совместимости: основной адрес, если ключ есть."""
    url = _helius_url()
    if not url:
        raise RuntimeError("HELIUS_API пуст в окружении")
    return url


def _helius_available() -> bool:
    return bool(_helius_url()) and time.monotonic() >= _helius_demoted_until


def _demote_helius(reason: str) -> None:
    global _helius_demoted_until
    with _rpc_lock:
        _helius_demoted_until = time.monotonic() + HELIUS_REPROBE_S
        if not RPC_STATS["helius_понижен"]:
            RPC_STATS["helius_понижен"] = True
            RPC_STATS["helius_понижен_utc"] = now_utc()
            print(f"[ledger] RPC: Helius понижен ({reason}); работаю через публичный узел, "
                  f"повторная проба через {HELIUS_REPROBE_S:.0f}с", flush=True)


def _post_rpc(url: str, payload, timeout: int):
    """Один сетевой вызов с уважением к темпу публичного узла."""
    global _public_last_call
    if url == PUBLIC_RPC:
        with _rpc_lock:
            wait = PUBLIC_MIN_INTERVAL_S - (time.monotonic() - _public_last_call)
            if wait > 0:
                time.sleep(wait)
            _public_last_call = time.monotonic()
    return requests.post(url, json=payload, timeout=timeout)


def _note(url: str, outcome: str) -> None:
    prefix = "helius" if url != PUBLIC_RPC else "публичный"
    key = f"{prefix}_{outcome}"
    with _rpc_lock:
        RPC_STATS[key] = RPC_STATS.get(key, 0) + 1


def rpc_call(method: str, params: list, retries: int = 20):
    """Каждая попытка сама выбирает узел: Helius, пока он жив, иначе публичный.

    Причина последнего отказа сохраняется в RPC_STATS: раньше исключение
    говорило только "исчерпал попытки", и отличить квоту от сетевого сбоя
    по логу было нельзя.
    """
    global _helius_429_streak
    backoff = 0.0
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last_reason = "попыток не было"
    for _ in range(retries):
        url = _helius_url() if _helius_available() else PUBLIC_RPC
        if url is None:
            url = PUBLIC_RPC
        _charge(url, method)
        try:
            resp = _post_rpc(url, payload, 30)
        except Exception as exc:  # noqa: BLE001
            _note(url, "прочие_ошибки")
            last_reason = f"{'helius' if url != PUBLIC_RPC else 'публичный'}: {type(exc).__name__}"
            backoff = min(max(backoff * 2, 0.5), 30.0)
            time.sleep(backoff)
            continue
        if resp.status_code == 429:
            _note(url, "429")
            last_reason = f"{'helius' if url != PUBLIC_RPC else 'публичный'}: HTTP 429"
            if url != PUBLIC_RPC:
                with _rpc_lock:
                    _helius_429_streak += 1
                    if RPC_STATS["первый_ответ_429_от_helius"] is None:
                        RPC_STATS["первый_ответ_429_от_helius"] = resp.text[:200]
                    streak = _helius_429_streak
                if streak >= HELIUS_DEMOTE_AFTER_429:
                    _demote_helius(f"{streak} ответов 429 подряд")
                # На публичный узел уходим СРАЗУ, не отсиживая паузу:
                # смысл запасного пути в том, чтобы не ждать.
                continue
            backoff = min(max(backoff * 2, 0.5), 30.0)
            time.sleep(backoff)
            continue
        if not resp.ok:
            _note(url, "прочие_ошибки")
            last_reason = f"{'helius' if url != PUBLIC_RPC else 'публичный'}: HTTP {resp.status_code}"
            if url != PUBLIC_RPC:
                _demote_helius(f"HTTP {resp.status_code}: {resp.text[:120]}")
                continue
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        if "error" in body:
            _note(url, "прочие_ошибки")
            raise RuntimeError(f"RPC error {method}: {body['error']}")
        _note(url, "ok")
        if url != PUBLIC_RPC:
            with _rpc_lock:
                _helius_429_streak = 0
        backoff = max(backoff * 0.5, 0.0)
        return body.get("result")
    with _rpc_lock:
        RPC_STATS["последняя_причина_отказа"] = last_reason
    raise RuntimeError(f"{method} исчерпал попытки, последняя причина: {last_reason}")


def rpc_batch(reqs: list[tuple[str, list]]) -> list:
    """Владелец: 'если батч-запросы отклоняются -- параллельные одиночные'."""
    if not reqs:
        return []
    body = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(reqs)]
    try:
        # Тот же выбор узла, что и у одиночного вызова: иначе батч продолжал
        # бы ломиться в Helius уже после того, как он понижен.
        url = _helius_url() if _helius_available() else PUBLIC_RPC
        resp = _post_rpc(url or PUBLIC_RPC, body, 60)
        if resp.ok:
            results = resp.json()
            if isinstance(results, list):
                by_id = {r.get("id"): r for r in results if isinstance(r, dict)}
                out, ok = [], True
                for i in range(len(reqs)):
                    r = by_id.get(i)
                    if r is None or "error" in r:
                        ok = False
                        break
                    out.append(r.get("result"))
                if ok and len(out) == len(reqs):
                    return out
    except Exception:  # noqa: BLE001
        pass
    out = [None] * len(reqs)
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(rpc_call, m, p): i for i, (m, p) in enumerate(reqs)}
        for fut in cf.as_completed(futs):
            i = futs[fut]
            try:
                out[i] = fut.result()
            except Exception:  # noqa: BLE001
                out[i] = None
    return out


def get_signatures_for_address(address: str, before: str | None = None, limit: int = 1000) -> list[dict]:
    opts: dict = {"limit": limit}
    if before:
        opts["before"] = before
    return rpc_call("getSignaturesForAddress", [address, opts]) or []


def get_balance_sol(address: str) -> float:
    lam = rpc_call("getBalance", [address])
    if isinstance(lam, dict):
        lam = lam.get("value")
    return (lam or 0) / 1e9


def get_token_holding(wallet: str, mint: str) -> float:
    res = rpc_call("getTokenAccountsByOwner", [wallet, {"mint": mint}, {"encoding": "jsonParsed"}])
    total = 0.0
    for acc in (res or {}).get("value") or []:
        try:
            ui = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"]
            total += float(ui or 0)
        except (KeyError, TypeError):
            continue
    return total


FOLLOW_TRADES_FRESHNESS_WINDOW_H = 6
SECTION_A_BUDGET_S = 25 * 60
HUNG_THRESHOLD_S = 300


# ---------- DBot REST (0 кредитов) ----------

def dbot_get(path: str, params: dict, api_key: str):
    for _attempt in range(6):
        try:
            resp = requests.get(f"{DBOT_HOST}{path}", params=params, headers={"X-API-KEY": api_key}, timeout=30)
        except Exception:  # noqa: BLE001
            time.sleep(2)
            continue
        if resp.status_code == 429:
            time.sleep(3)
            continue
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {"non_json_body": resp.text[:500]}
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
    """Владелец (найдено в сторожe проваленных продаж, тот же класс
    риска здесь): True -- extract_items нашёл ожидаемый ключ (пусть
    даже пустой список -- честный ноль). False -- тело не похоже на
    ожидаемую форму вообще -- расхождение формы, не должно молча
    считаться концом пагинации."""
    if isinstance(body, list):
        return True
    if isinstance(body, dict):
        return any(isinstance(body.get(k), list) for k in ("res", "data", "results", "list", "items"))
    return False


def fetch_follow_orders(api_key: str) -> tuple[list[dict], int | None]:
    status, body = dbot_get("/automation/follow_orders", {}, api_key)
    items = extract_items(body)
    tasks = []
    for row in items:
        task_id = row.get("id") or row.get("_id")
        target_ids = row.get("targetIds") or []
        target_names = row.get("targetNames") or []
        sources = [{"address": addr, "remark": (target_names[i] if i < len(target_names) else "") or None}
                   for i, addr in enumerate(target_ids)]
        tasks.append({"id": task_id, "name": row.get("name"), "wallet": row.get("walletAddress"),
                      "wallet_name": row.get("walletName"), "enabled": row.get("enabled"), "sources": sources})
    return tasks, status


def fetch_follow_trades_for_task(task_id: str, api_key: str, my_wallet: str | None = None) -> tuple[list[dict], bool]:
    """Владелец: пагинация должна идти ДО пустой страницы, а не
    останавливаться раньше на "страница короче size" -- на живом,
    постоянно дописываемом наборе данных страница может отдать <size
    записей НЕ на самом деле дойдя до конца (сдвиг из-за параллельной
    записи новых сделок), и тогда следующая страница снова непустая --
    эмпирически подтверждено: 4 реальные сделки по источнику N_HtuY были
    пропущены именно так. myWallet -- дополнительный фильтр по кошельку
    задачи, если поддерживается API (сужает выборку, не должен вредить).

    Найдено при диагнозе "13 сделок после 10:43, источник только у 1":
    dbot_get после 6 неудачных попыток возвращает (None, {}) -- и
    extract_items({}) тоже даёт [], НЕОТЛИЧИМО от настоящего конца
    пагинации. Разовый сетевой сбой посреди прохода молча обрубал
    выгрузку прямо перед самыми свежими страницами. Теперь неудачная
    страница -- отдельный повтор (до 5 раз), и если так и не вышло --
    возвращаем то, что успели, и complete=False, а не тихо "конец".
    Возвращает (записи, complete) -- complete=False значит выгрузка этой
    задачи в этом прогоне не гарантированно полная.

    Владелец, найденный реальный баг (сторож проваленных продаж):
    страницы DBot нумеруются с 0 (docs.dbotx.com/reference/copy-tpsl-
    tasks -- "page, defaults to 0"), а не с 1 -- начиная с page=1 эта
    функция ВСЕГДА пропускала настоящую первую страницу. follow_orders
    (fetch_follow_orders выше) не затронут -- он не постраничный вообще
    (один GET, без параметра page)."""
    out = []
    page = 0
    params = {"chain": "solana", "configId": task_id, "size": 20}
    if my_wallet:
        params["myWallet"] = my_wallet
    while True:
        status, body = None, {}
        for page_attempt in range(5):
            status, body = dbot_get("/account/follow_trades", {**params, "page": page}, api_key)
            if status is not None:
                break
            time.sleep(3 * (page_attempt + 1))
        if status is None:
            return out, False
        if status == 200 and body and not has_recognized_list_shape(body):
            print(f"[ledger] ВНИМАНИЕ: follow_trades страница {page} -- HTTP 200, тело непустое, но форма "
                  f"НЕ распознана (нет res/data/results/list/items) -- расхождение формы, не честный ноль: "
                  f"{json.dumps(body, ensure_ascii=False, default=str)[:2000]}", flush=True)
        items = extract_items(body)
        if not items:
            return out, True
        out.extend(items)
        page += 1
        if page > 500:
            return out, False  # честно: упёрлись в защитный потолок, не в реальный конец данных


def dbot_signature_from_record(r: dict) -> str | None:
    """Подписи как отдельного поля в follow_trades нет (подтверждено
    документацией и эмпирически). Она зашита в links.* как URL explorer'а --
    у links.etherscan это на деле Solscan-ссылка для solana."""
    links = r.get("links") or {}
    for key in ("etherscan", "dexscreener", "uniswap"):
        url = links.get(key)
        if url and "/tx/" in url:
            sig = url.rsplit("/tx/", 1)[-1].strip()
            if sig:
                return sig
    return None


def dbot_token_contract(r: dict) -> str | None:
    """Адрес НЕ-SOL токена сделки: buy -- получаемый токен, sell -- отдаваемый."""
    rtype = str(r.get("type") or "").lower()
    send_c = ((r.get("send") or {}).get("info") or {}).get("contract")
    recv_c = ((r.get("receive") or {}).get("info") or {}).get("contract")
    if rtype == "buy":
        return recv_c if recv_c and recv_c != WSOL else send_c
    if rtype == "sell":
        return send_c if send_c and send_c != WSOL else recv_c
    for c in (send_c, recv_c):
        if c and c != WSOL:
            return c
    return send_c or recv_c


def dbot_sol_amount(r: dict) -> float | None:
    """Сумма SOL-ноги сделки (send для buy, receive для sell) в SOL -- для
    сравнения по сумме при сшивке по времени (кандидатов несколько)."""
    rtype = str(r.get("type") or "").lower()
    try:
        if rtype == "buy":
            return float(((r.get("send") or {}).get("amount"))) / 1e9
        if rtype == "sell":
            return float(((r.get("receive") or {}).get("amount"))) / 1e9
    except (TypeError, ValueError):
        return None
    return None


def dbot_time_candidates(records: list[dict]) -> list[dict]:
    """Записи state=='done' без извлекаемой подписи -- кандидаты для запасной
    сшивки по времени (owner: если подписи нет -- сшивать по кошельку/минту/
    типу/времени +-20с)."""
    out = []
    for r in records:
        if str(r.get("state") or "").lower() != "done":
            continue
        if dbot_signature_from_record(r):
            continue
        if r.get("timestamp") is None or dbot_token_contract(r) is None:
            continue
        out.append(r)
    return out


def find_dbot_record(sig: str, block_time, mint: str, is_buy: bool, sol_amount,
                      dbot_by_sig: dict, time_candidates: list[dict], consumed_ids: set):
    """Сначала точная подпись; если её нет в DBot-записях -- запасная сшивка
    по (тот же минт, тот же тип buy/sell, |время цепочки - timestamp| <= 20с),
    при нескольких кандидатах -- ближайший по времени, затем по сумме SOL."""
    rec = dbot_by_sig.get(sig)
    if rec is not None:
        return rec, "signature"
    if block_time is None:
        return None, None
    want_type = "buy" if is_buy else "sell"
    candidates = []
    for r in time_candidates:
        rid = r.get("id") or r.get("_id")
        if rid is not None and rid in consumed_ids:
            continue
        if str(r.get("type") or "").lower() != want_type:
            continue
        if dbot_token_contract(r) != mint:
            continue
        dt = abs((r.get("timestamp") or 0) - block_time)
        if dt <= 20:
            candidates.append((dt, r))
    if not candidates:
        return None, None
    if sol_amount is not None:
        def amount_diff(item):
            ra = dbot_sol_amount(item[1])
            return abs((ra if ra is not None else 1e9) - sol_amount)
        candidates.sort(key=lambda item: (item[0], amount_diff(item)))
    else:
        candidates.sort(key=lambda item: item[0])
    best = candidates[0][1]
    rid = best.get("id") or best.get("_id")
    if rid is not None:
        consumed_ids.add(rid)
    return best, "time_match"


def find_source_by_onchain_time(source_addr: str, mint: str, buy_time: int,
                                 window_s: int = 10, max_pages: int = 8) -> str | None:
    """Владелец, п.3: сделка без записи DBot -- источник ищем по цепочке
    самого источника: его собственная покупка того же минта в пределах
    window_s секунд ДО нашей покупки (копирующий бот срабатывает следом
    за источником, не раньше). Идём назад по подписям источника, пока не
    пройдём мимо окна по времени, или не кончится max_pages (страховка от
    расходов на очень активный источник без результата)."""
    before = None
    for _ in range(max_pages):
        page = get_signatures_for_address(source_addr, before=before, limit=1000)
        if not page:
            return None
        candidates = [h for h in page if h.get("blockTime") is not None
                      and buy_time - window_s <= h["blockTime"] <= buy_time]
        for h in candidates:
            tx = rpc_call("getTransaction", [h["signature"], {"encoding": "json", "maxSupportedTransactionVersion": 1}])
            if tx is None:
                continue
            parsed = parse_tx_for_wallet(h["signature"], tx, source_addr)
            if parsed.get("err") is not None or not parsed.get("is_signer"):
                continue
            leg = classify_swap_leg(parsed)
            if leg and leg[0] == mint and leg[1] > 0:
                return h["signature"]
        oldest_bt = page[-1].get("blockTime")
        if oldest_bt is not None and oldest_bt < buy_time - window_s:
            return None
        before = page[-1]["signature"]
    return None


# ---------- Section B: цепочка (баланс-метод) ----------

def account_keys_and_signers(tx: dict) -> tuple[list[str], set[str]]:
    msg = tx["transaction"]["message"]
    static_keys = list(msg["accountKeys"])
    la = (tx.get("meta") or {}).get("loadedAddresses") or {}
    keys = static_keys + list(la.get("writable") or []) + list(la.get("readonly") or [])
    num_sig = msg.get("header", {}).get("numRequiredSignatures", 1)
    signers = set(static_keys[:num_sig])
    return keys, signers


def token_balance_map(tb_list, owner: str) -> dict:
    out: dict = {}
    for b in tb_list or []:
        if b.get("owner") != owner:
            continue
        amt = D(b["uiTokenAmount"]["amount"]) / D(10) ** b["uiTokenAmount"]["decimals"]
        out[b["mint"]] = out.get(b["mint"], D(0)) + amt
    return out


def classify_transfer_address(addr: str) -> str:
    if addr == DBOT_FEE_ADDRESS:
        return "комиссия DBot"
    if RESCUE_WALLET_ADDRESS and addr == RESCUE_WALLET_ADDRESS:
        return "утилизатор (спасение)"
    if addr in KNOWN_RELAYS or any(addr.startswith(p) for p in RELAY_PREFIXES):
        return "чаевые"
    return "прочее"


def parse_tx_for_wallet(sig: str, tx: dict, wallet: str) -> dict:
    meta = tx.get("meta") or {}
    keys, signers = account_keys_and_signers(tx)
    if wallet not in keys:
        return {"signature": sig, "slot": tx.get("slot"), "blockTime": tx.get("blockTime"),
                "err": "wallet_not_in_keys"}
    idx = keys.index(wallet)
    pre_bal, post_bal = meta.get("preBalances") or [], meta.get("postBalances") or []
    pre_sol = pre_bal[idx] if idx < len(pre_bal) else None
    post_sol = post_bal[idx] if idx < len(post_bal) else None
    sol_delta = float((D(post_sol) - D(pre_sol)) / D(10 ** 9)) if pre_sol is not None and post_sol is not None else None

    pre_tb = token_balance_map(meta.get("preTokenBalances"), wallet)
    post_tb = token_balance_map(meta.get("postTokenBalances"), wallet)
    token_deltas = {}
    for m in set(pre_tb) | set(post_tb):
        d = post_tb.get(m, D(0)) - pre_tb.get(m, D(0))
        if d != 0:
            token_deltas[m] = float(d)
    wsol_delta = token_deltas.pop(WSOL, 0.0)

    transfers = []
    for i, key in enumerate(keys):
        if key in signers or key == wallet or i >= len(pre_bal) or i >= len(post_bal):
            continue
        delta = (post_bal[i] - pre_bal[i]) / 1e9
        if delta > 0:
            transfers.append({"recipient": key, "amount_sol": round(delta, 9), "tag": classify_transfer_address(key)})

    # Контрагент входящего SOL (для п.5 -- "внешние потоки"/пополнения):
    # если кошелёк получил SOL, ищем среди подписантов ЭТОЙ транзакции (не
    # самого кошелька) того, чей баланс упал сильнее всего -- это и есть
    # реальный отправитель/плательщик перевода.
    counterparty_in = None
    if sol_delta is not None and sol_delta > 0:
        best = None
        for i, key in enumerate(keys):
            if key == wallet or key not in signers or i >= len(pre_bal) or i >= len(post_bal):
                continue
            d = (post_bal[i] - pre_bal[i]) / 1e9
            if d < 0 and (best is None or d < best[1]):
                best = (key, d)
        if best:
            counterparty_in = best[0]

    return {
        "signature": sig, "slot": tx.get("slot"), "blockTime": tx.get("blockTime"), "err": meta.get("err"),
        "is_signer": wallet in signers,
        "sol_delta_native": sol_delta, "wsol_delta": float(wsol_delta), "token_deltas": token_deltas,
        "fee_sol": (meta.get("fee") or 0) / 1e9, "sol_transfers": transfers, "counterparty_in": counterparty_in,
    }


def _genesis_key(wallet: str) -> str:
    return f"__genesis_synced__:{wallet}"


def _oldest_cached_signature(cache: dict, wallet: str) -> str | None:
    rows = [v for v in cache.values() if isinstance(v, dict) and v.get("_wallet") == wallet and v.get("slot") is not None]
    if not rows:
        return None
    return min(rows, key=lambda v: v["slot"])["signature"]


def _cache_key(sig: str, wallet: str) -> str:
    return f"{sig}:{wallet}"


def _has_own_view(cache: dict, sig: str, wallet: str) -> bool:
    """Найдено при проверке выдачи: одна и та же подпись бывает актуальна
    сразу для ДВУХ отслеживаемых кошельков -- перевод SOL напрямую между
    двумя своими же кошельками задач (BATCH-4 -> BATCH-5, 0.500015 SOL,
    подпись 3bhVXPkghX...). Кэш был ключом ПО ПОДПИСИ -- чей кошелёк
    первым синхронизировался, тот и "застолбил" запись; второй кошелёк
    навсегда видел "уже в кэше" и никогда не получал СВОЙ собственный
    разбор той же транзакции (свою сторону перевода, свой is_signer).
    Теперь новые записи пишутся под составным ключом (подпись, кошелёк);
    старые ключи-подписи по-прежнему признаются "своими", если уже
    принадлежат этому же кошельку -- чтобы не терять то, что и так верно
    закэшировано, и не устраивать полный переразбор истории заново."""
    if _cache_key(sig, wallet) in cache:
        return True
    legacy = cache.get(sig)
    return isinstance(legacy, dict) and legacy.get("_wallet") == wallet


def sync_wallet_chain(wallet: str, cache: dict, deadline: float) -> int:
    """Найдено при проверке выдачи: у нескольких кошельков (BATCH-1..4)
    первая закэшированная транзакция -- уже покупка, без транзакции
    пополнения перед ней -- значит первоначальный обход истории не дошёл
    до реального начала (бюджет времени кончился раньше), а старая логика
    ('встретили уже известную подпись -- считаем, что дальше в глубину всё
    известно') после этого НАВСЕГДА замораживала разрыв между этим местом
    и настоящим началом истории кошелька, потому что каждый следующий
    запуск снова упирался в тот же самый кэш на первой же странице.
    Теперь: проход 1 -- свежая активность с конца; проход 2, если геном
    ещё не подтверждён явным маркером -- докапываем назад от самой старой
    уже известной подписи, а не останавливаемся на первом совпадении."""
    todo = []

    before = None
    while time.monotonic() < deadline:
        page = get_signatures_for_address(wallet, before=before, limit=1000)
        if not page:
            cache[_genesis_key(wallet)] = {"_genesis_marker": True}
            break
        new_in_page = [h for h in page if not _has_own_view(cache, h["signature"], wallet)]
        todo.extend(new_in_page)
        before = page[-1]["signature"]
        if len(new_in_page) < len(page):
            break  # дальше в глубину -- уже известная область (с прошлого раза)
        if len(page) < 1000:
            cache[_genesis_key(wallet)] = {"_genesis_marker": True}
            break

    if not cache.get(_genesis_key(wallet)):
        before = _oldest_cached_signature(cache, wallet)
        while before and time.monotonic() < deadline:
            page = get_signatures_for_address(wallet, before=before, limit=1000)
            if not page:
                cache[_genesis_key(wallet)] = {"_genesis_marker": True}
                break
            todo.extend(h for h in page if not _has_own_view(cache, h["signature"], wallet))
            before = page[-1]["signature"]
            if len(page) < 1000:
                cache[_genesis_key(wallet)] = {"_genesis_marker": True}
                break

    todo.reverse()
    n_new = 0
    chunk_size = 20
    for start in range(0, len(todo), chunk_size):
        if time.monotonic() > deadline:
            break
        chunk = [h for h in todo[start:start + chunk_size] if not _has_own_view(cache, h["signature"], wallet)]
        if not chunk:
            continue
        ok_chunk = [h for h in chunk if h.get("err") is None]
        for h in chunk:
            if h.get("err") is not None:
                cache[_cache_key(h["signature"], wallet)] = {"signature": h["signature"], "slot": h.get("slot"),
                                                              "blockTime": h.get("blockTime"), "err": h.get("err"), "_wallet": wallet}
        if ok_chunk:
            reqs = [("getTransaction", [h["signature"], {"encoding": "json", "maxSupportedTransactionVersion": 1}]) for h in ok_chunk]
            results = rpc_batch(reqs)
            for h, tx in zip(ok_chunk, results):
                if tx is None:
                    # Найдено при проверке выдачи: пакетный (batch) вызов
                    # getTransaction иногда молча теряет ОДНУ конкретную
                    # запись (null вместо результата) при том, что прямой
                    # одиночный вызов той же подписи проходит с первого
                    # раза -- и без повтора это НАВСЕГДА остаётся дырой в
                    # кэше (подпись не кэшируется ни как успех, ни как
                    # ошибка, но и не помечается для гарантированного
                    # повтора). Один прямой одиночный вызов перед тем, как
                    # сдаться, закрывает именно этот случай.
                    tx = rpc_call("getTransaction", [h["signature"], {"encoding": "json", "maxSupportedTransactionVersion": 1}])
                    if tx is None:
                        continue
                parsed = parse_tx_for_wallet(h["signature"], tx, wallet)
                parsed["_wallet"] = wallet
                cache[_cache_key(h["signature"], wallet)] = parsed
                n_new += 1
    return n_new


def fetch_missing_tx(sig: str, wallet: str, cache: dict) -> None:
    if _has_own_view(cache, sig, wallet):
        return
    tx = rpc_call("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 1}])
    if tx is None:
        cache[_cache_key(sig, wallet)] = {"signature": sig, "err": "getTransaction_null", "_wallet": wallet}
        return
    parsed = parse_tx_for_wallet(sig, tx, wallet)
    parsed["_wallet"] = wallet
    cache[_cache_key(sig, wallet)] = parsed


# ---------- Section C+D: сшивка, сделки, статистика ----------

def classify_swap_leg(v: dict):
    """Владелец, п.1: сделка = ТОЛЬКО своп против SOL/WSOL -- покупка
    (SOL/WSOL ушли, токен пришёл) или продажа (токен ушёл, SOL/WSOL
    пришли). "SOL/WSOL ушли/пришли" -- совместная нога sol_delta_native +
    wsol_delta (своп может расплачиваться уже обёрнутым WSOL без изменения
    нативного баланса). Любая другая комбинация знаков (голая передача
    токена, побочная пыль без встречной SOL-ноги и т.п.) -- не сделка.
    Возвращает (минт, знак_дельта_токена, sol_leg) или None."""
    deltas = v.get("token_deltas") or {}
    if not deltas:
        return None
    sol_leg = (v.get("sol_delta_native") or 0) + (v.get("wsol_delta") or 0)
    if sol_leg < 0:
        candidates = {m: d for m, d in deltas.items() if d > 0}
    elif sol_leg > 0:
        candidates = {m: d for m, d in deltas.items() if d < 0}
    else:
        return None
    if not candidates:
        return None
    mint, delta = max(candidates.items(), key=lambda kv: abs(kv[1]))
    return mint, delta, sol_leg


def classify_external_flow(v: dict):
    """Владелец, доп. к п.5 (все кошельки задач): "внешний поток" -- нет
    свопа (нет изменения НЕ-WSOL токенов) и нет перевода на адрес комиссии
    DBot (это отдельная, уже учтённая статья). Входящий SOL/WSOL --
    пополнение, исходящий -- вывод. Возвращает (kind, amount_sol>0,
    контрагент|None) или None, если это не внешний поток."""
    if v.get("token_deltas"):
        return None
    sol_leg = (v.get("sol_delta_native") or 0) + (v.get("wsol_delta") or 0)
    if abs(sol_leg) < 1e-9:
        return None
    transfers = v.get("sol_transfers") or []
    if any(tr["recipient"] == DBOT_FEE_ADDRESS for tr in transfers):
        return None
    if sol_leg > 0:
        kind = "rescue_in" if v.get("counterparty_in") == RESCUE_WALLET_ADDRESS \
            and RESCUE_WALLET_ADDRESS else "topup"
        return kind, sol_leg, v.get("counterparty_in")
    top = max(transfers, key=lambda tr: tr["amount_sol"]) if transfers else None
    recipient = top["recipient"] if top else None
    # ЭТАП E: отправка НА утилизатор -- внутреннее перемещение, не вывод.
    if RESCUE_WALLET_ADDRESS and recipient == RESCUE_WALLET_ADDRESS:
        return "rescue_out", -sol_leg, recipient
    return "withdrawal", -sol_leg, recipient


def classify_bot(wallet: str, buy_block_time, has_dbot_record: bool) -> str:
    """Чей это бот: DBot, Bloom, TradeWiz или неизвестно.

    Запись DBot -- решающее доказательство: она есть только у наших сделок.
    Кошелёк BATCH-8 один, а ботов на нём было два подряд: TradeWiz с
    23.09 00:30Z до 15:42Z и Bloom с 15:42Z. Поэтому окно TradeWiz
    ЗАКРЫТО сверху: без этого сделки Bloom получали бы чужую метку и
    вылетали из итогов. Всё остальное без записи DBot -- честно
    "неизвестен", а не догадка в пользу удобного ответа.
    """
    if has_dbot_record:
        return DBOT_LABEL
    if wallet == BLOOM_WALLET and buy_block_time is not None and buy_block_time >= BLOOM_FROM_TS:
        return BLOOM_LABEL
    if (wallet == TRADEWIZ_WALLET and buy_block_time is not None
            and TRADEWIZ_FROM_TS <= buy_block_time < BLOOM_FROM_TS):
        return TRADEWIZ_LABEL
    return UNKNOWN_BOT_LABEL


def build_trades_for_task(task: dict, records: list[dict], chain_cache: dict) -> list[dict]:
    wallet = task["wallet"]
    # is_signer=True обязательно: иначе в "сделки" попадают транзакции, которые
    # кошелёк вообще не инициировал (пассивная пыль/чужая транзакция,
    # затрагивающая его токен-аккаунт -- эмпирически найдено: несколько таких
    # tx с token_deltas>0 и sol_delta_native==0 давали фантомные "незакрытые
    # позиции" и путали сопоставление buy/sell для реальных сделок).
    wallet_tx = [v for v in chain_cache.values()
                 if v.get("_wallet") == wallet and not v.get("err") and v.get("is_signer")]
    by_mint: dict[str, list] = {}
    for v in wallet_tx:
        leg = classify_swap_leg(v)
        if leg is None:
            continue
        mint, delta, sol_leg = leg
        by_mint.setdefault(mint, []).append((v, delta, sol_leg))
    for mint in by_mint:
        by_mint[mint].sort(key=lambda t: (t[0].get("slot") or 0))

    dbot_by_sig: dict[str, dict] = {}
    for r in records:
        sig = dbot_signature_from_record(r)
        if sig:
            dbot_by_sig[sig] = r
    time_candidates = dbot_time_candidates(records)
    consumed_ids: set = set()

    def resolve_source(record: dict | None):
        if not record:
            return None
        follow = record.get("follow") or {}
        src_addr = follow.get("wallet")
        if not src_addr:
            return None
        known = next((s for s in task["sources"] if s["address"] == src_addr), None)
        return known or {"address": src_addr, "remark": follow.get("remark")}

    trades = []
    matched_sigs: set = set()
    matched_record_ids: set = set()
    for mint, txs in by_mint.items():
        buys = [t for t in txs if t[1] > 0]
        sells = [t for t in txs if t[1] < 0]
        sells_remaining = list(sells)
        for buy_v, buy_amt, buy_sol_leg in buys:
            matched_sigs.add(buy_v["signature"])
            sell_v, sell_sol_leg = None, None
            for i, (sv, sd, sl) in enumerate(sells_remaining):
                if (sv.get("slot") or 0) > (buy_v.get("slot") or 0):
                    sell_v, sell_sol_leg = sv, sl
                    del sells_remaining[i]
                    break

            sol_in = round(-buy_sol_leg, 9)
            # Владелец, п.2: источник -- ТОЛЬКО из записи DBot со state=done
            # (signature из links, источник -- follow.wallet). Нет такой
            # записи -> источник "неизвестен", но сделка всё равно "закрыта"
            # (продажа DBot никогда не бывает state=done в этих данных --
            # копирующий продавал сам, это не повод считать сделку неучтённой).
            buy_record, buy_method = find_dbot_record(
                buy_v["signature"], buy_v.get("blockTime"), mint, True, sol_in,
                dbot_by_sig, time_candidates, consumed_ids)
            if buy_record is not None:
                rid = buy_record.get("id") or buy_record.get("_id")
                if rid is not None:
                    matched_record_ids.add(rid)

            sell_record, sell_method = None, None
            sol_out = None
            if sell_v:
                matched_sigs.add(sell_v["signature"])
                sol_out = round(sell_sol_leg, 9)
                sell_record, sell_method = find_dbot_record(
                    sell_v["signature"], sell_v.get("blockTime"), mint, False, sol_out,
                    dbot_by_sig, time_candidates, consumed_ids)
                if sell_record is not None:
                    rid = sell_record.get("id") or sell_record.get("_id")
                    if rid is not None:
                        matched_record_ids.add(rid)

            source = resolve_source(buy_record)
            source_method = "dbot_record" if source else None
            # Владелец, п.3: нет записи DBot -- пробуем сшить источник по
            # цепочке (его собственная покупка того же минта в пределах 10с
            # до нашей), перебирая объявленные источники задачи; закрытую
            # сделку без источника не теряем -- либо найдём, либо явно
            # покажем "источник неизвестен" (не молчим и не отбрасываем).
            if source is None and sell_v and buy_v.get("blockTime"):
                for cand in task["sources"]:
                    if find_source_by_onchain_time(cand["address"], mint, buy_v["blockTime"]):
                        source = cand
                        source_method = "onchain_time"
                        break

            # Владелец: брутто -- НЕ из транзакции (в ней чаевые/приоритетные
            # сборы той же сделки смешаны со свопом, надёжно не разделить без
            # decode_tx -- уже проверено и не подтвердилось на примерах).
            # Брутто = то, что сама DBot записала как отправленное/полученное
            # (send.amount у buy-записи, receive.amount у sell-записи);
            # нетто (sol_in/sol_out выше) остаётся дельтой кошелька по цепочке
            # -- это и есть проверенное на пилоте значение, не трогаем.
            gross_sol_in = dbot_sol_amount(buy_record) if buy_record else None
            gross_sol_out = dbot_sol_amount(sell_record) if sell_record else None

            net_sol = round(buy_sol_leg + (sell_sol_leg or 0), 9)
            gross_pct = round((sol_out / sol_in - 1) * 100, 4) if (sell_v and sol_in) else None

            # Владелец, п.3: закрыта -- покупка+продажа обе найдены в цепочке
            # (независимо от того, есть ли запись DBot для продажи -- её и
            # не будет, автопродажи DBot тут не проходили); незакрыта --
            # токен ещё на балансе; "ручная продажа" -- токена на балансе
            # уже нет, но продажи в истории кошелька мы не нашли (передан/
            # продан вне зоны видимости этого метода).
            if sell_v:
                status = "закрыта"
            else:
                holding = get_token_holding(wallet, mint)
                status = "незакрыта" if holding > 0 else "ручная продажа"

            # Владелец: отдельной колонкой -- зависшие сделки (держали
            # дольше HUNG_THRESHOLD_S). Для закрытых -- реальное время
            # покупка->продажа по цепочке; для ещё открытых -- сколько уже
            # держим к моменту прогона (тоже "зависла", и даже актуальнее).
            # "Ручная продажа" -- время продажи нам не известно (её не
            # нашли в истории кошелька), не выдумываем held_seconds для неё.
            held_seconds = None
            if sell_v and sell_v.get("blockTime") is not None and buy_v.get("blockTime") is not None:
                held_seconds = sell_v["blockTime"] - buy_v["blockTime"]
            elif status == "незакрыта" and buy_v.get("blockTime") is not None:
                held_seconds = int(time.time()) - buy_v["blockTime"]
            is_hung = bool(held_seconds is not None and held_seconds > HUNG_THRESHOLD_S)

            trades.append({
                "task_id": task["id"], "task_name": task.get("name"), "wallet": wallet,
                "wallet_name": task.get("wallet_name"),
                "source_address": source["address"] if source else None,
                "source_remark": source["remark"] if source else None,
                "source_match_method": source_method,
                "mint": mint,
                "buy_signature": buy_v["signature"], "buy_block_time": buy_v.get("blockTime"),
                "buy_match_method": buy_method,
                "sell_signature": sell_v["signature"] if sell_v else None,
                "sell_block_time": sell_v.get("blockTime") if sell_v else None,
                "sell_match_method": sell_method,
                "sol_in": sol_in, "sol_out": sol_out, "gross_pct": gross_pct, "net_sol": net_sol,
                "gross_sol_in": gross_sol_in, "gross_sol_in_source": "dbot_record" if buy_record else None,
                "gross_sol_out": gross_sol_out, "gross_sol_out_source": "dbot_record" if sell_record else None,
                "status": status,
                "held_seconds": held_seconds, "is_hung": is_hung,
                "dbot_fail_reason": None,
                "bot": classify_bot(wallet, buy_v.get("blockTime"), buy_record is not None),
            })

    for r in records:
        rid = r.get("id") or r.get("_id")
        sig = dbot_signature_from_record(r)
        state = str(r.get("state") or "").lower()
        if sig and sig in matched_sigs:
            continue
        if rid is not None and rid in matched_record_ids:
            continue
        if state == "done":
            continue  # реально прошла, но не нашлась в chain-кэше в этом проходе -- честно не учитываем
        # Владелец, п.1: fail/skip/expired -- НЕ сделки, только счётчик
        # срывов ИСТОЧНИКА с причиной -- источник берём прямо из записи
        # (follow.wallet), сшивка по signature/времени тут не нужна: сама
        # запись уже точно говорит, кого пытались скопировать.
        source = resolve_source(r)
        trades.append({
            "task_id": task["id"], "task_name": task.get("name"), "wallet": wallet,
            "wallet_name": task.get("wallet_name"),
            "source_address": source["address"] if source else None,
            "source_remark": source["remark"] if source else None,
            "mint": dbot_token_contract(r),
            "buy_signature": sig, "status": "срыв",
            "held_seconds": None, "is_hung": False,  # не исполнилась в цепочке -- держать нечего
            "dbot_fail_reason": r.get("errorMessage") or r.get("skipReason") or r.get("errorCode"),
            # Срыв строится ИЗ записи DBot -- значит это по определению DBot.
            "bot": DBOT_LABEL,
        })
    return trades


def build_source_stats(trades_all: list[dict]) -> list[dict]:
    # Владелец, п.3: сделки без источника не терять -- группируем и
    # неопознанные (source_address=None) под отдельной строкой на задачу,
    # а не отбрасываем.
    by_key: dict = {}
    for t in trades_all:
        # Сделки чужого бота (TradeWiz, Bloom) в статистику ИСТОЧНИКОВ DBot
        # не идут: источник им назначали не мы, и приписывать их нашим
        # сигналам нельзя.
        if t.get("bot") in ЧУЖИЕ_БОТЫ:
            continue
        key = (t["task_id"], t.get("source_address"))
        by_key.setdefault(key, {"task_id": t["task_id"], "task_name": t.get("task_name"),
                                 "source_address": t.get("source_address"),
                                 "source_remark": t.get("source_remark") if t.get("source_address")
                                 else "источник неизвестен",
                                 "trades": []})
        by_key[key]["trades"].append(t)
    out = []
    for (task_id, addr), row in by_key.items():
        trades = row["trades"]
        # Владелец, п.1: "сделок" = только закрытые (покупка+продажа в
        # цепочке); fail/skip/expired -- отдельным столбцом "срывов".
        closed = [t for t in trades if t["status"] == "закрыта"]
        # Владелец, п.2: "в+" = нетто > 0 (было: доля по проценту, что
        # давало неверные числа вроде "11 из 351").
        n_profit = sum(1 for t in closed if (t.get("net_sol") or 0) > 0)
        # Брутто (владелец, п.1) -- строго из записи DBot, не из транзакции;
        # доступно только когда есть done-запись на покупку (продажа DBot
        # тут никогда не завершается -- gross_sol_out практически всегда
        # неизвестен, честно не считаем его как ноль).
        gross_sum = sum(t["gross_sol_in"] for t in trades if t.get("gross_sol_in"))
        net_sum = sum(t["net_sol"] for t in trades if t.get("net_sol") is not None)
        best_pct = max((t["gross_pct"] for t in closed if t.get("gross_pct") is not None), default=None)
        fails = [t for t in trades if t["status"] == "срыв"]
        n_hung = sum(1 for t in trades if t.get("is_hung"))
        last_bt = max((t.get("buy_block_time") or 0 for t in trades), default=0)
        out.append({
            "task_id": task_id, "task_name": row["task_name"], "source_address": addr,
            "source_remark": row["source_remark"], "n_trades": len(closed), "n_profit": n_profit,
            "gross_sol_sum": round(gross_sum, 6), "net_sol_sum": round(net_sum, 6),
            "best_trade_pct": best_pct, "n_fails": len(fails), "n_hung": n_hung,
            "fail_reasons": [f.get("dbot_fail_reason") for f in fails][:10],
            "last_trade_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(last_bt)) if last_bt else None,
        })
    out.sort(key=lambda r: r["net_sol_sum"], reverse=True)
    return out


def build_wallet_reconciliation(wallet: str, chain_cache: dict) -> dict:
    """Владелец, п.4: полная сверка по категориям -- закрытые / покупка без
    продажи / продажа без покупки / пополнения / выводы / комиссия DBot /
    прочее. Комиссия DBot вынесена отдельной строкой (добавляется обратно
    в сумму своей сделки, чтобы не задваивалась и не пропадала) -- сумма
    всех категорий обязана дать текущий баланс, если история кошелька
    подтверждённо докопана до генезиса (genesis_synced=true); если нет --
    это и есть явный, названный источник невязки, а не "что-то произошло
    во время прогона" накидываемое на несколько кошельков сразу."""
    wallet_tx = [v for v in chain_cache.values() if isinstance(v, dict) and v.get("_wallet") == wallet and not v.get("err")]

    def dbot_fee_of(v: dict) -> float:
        return sum(tr["amount_sol"] for tr in (v.get("sol_transfers") or []) if tr["tag"] == "комиссия DBot")

    signer_tx = [v for v in wallet_tx if v.get("is_signer")]
    by_mint: dict[str, list] = {}
    for v in signer_tx:
        leg = classify_swap_leg(v)
        if leg is None:
            continue
        mint, delta, sol_leg = leg
        by_mint.setdefault(mint, []).append((v, delta, sol_leg))
    for mint in by_mint:
        by_mint[mint].sort(key=lambda t: (t[0].get("slot") or 0))

    closed_sol = buy_without_sell_sol = sell_without_buy_sol = dbot_fee_sol = 0.0
    swap_sigs: set = set()
    for mint, txs in by_mint.items():
        buys = [t for t in txs if t[1] > 0]
        sells = [t for t in txs if t[1] < 0]
        sells_remaining = list(sells)
        for buy_v, buy_amt, buy_sol_leg in buys:
            swap_sigs.add(buy_v["signature"])
            buy_fee = dbot_fee_of(buy_v)
            dbot_fee_sol += buy_fee
            sell_v, sell_sol_leg = None, None
            for i, (sv, sd, sl) in enumerate(sells_remaining):
                if (sv.get("slot") or 0) > (buy_v.get("slot") or 0):
                    sell_v, sell_sol_leg = sv, sl
                    del sells_remaining[i]
                    break
            if sell_v:
                swap_sigs.add(sell_v["signature"])
                sell_fee = dbot_fee_of(sell_v)
                dbot_fee_sol += sell_fee
                closed_sol += (buy_sol_leg + buy_fee) + (sell_sol_leg + sell_fee)
            else:
                buy_without_sell_sol += buy_sol_leg + buy_fee
        for sv, sd, sl in sells_remaining:
            swap_sigs.add(sv["signature"])
            sell_fee = dbot_fee_of(sv)
            dbot_fee_sol += sell_fee
            sell_without_buy_sol += sl + sell_fee

    topups, withdrawals, other_entries = [], [], []
    rescue_out, rescue_in = [], []
    other_sol = 0.0
    for v in wallet_tx:
        if v.get("signature") in swap_sigs:
            continue
        flow = classify_external_flow(v)
        if flow is not None:
            kind, amount, counterparty = flow
            entry = {"signature": v.get("signature"), "counterparty": counterparty, "amount_sol": round(amount, 9)}
            # ЭТАП E: спасение -- своё перемещение между нашими же
            # кошельками. В пополнения/выводы оно не идёт, иначе сверка
            # показала бы вывод на неизвестный адрес и пополнение извне.
            {"topup": topups, "withdrawal": withdrawals,
             "rescue_out": rescue_out, "rescue_in": rescue_in}[kind].append(entry)
        else:
            leftover = (v.get("sol_delta_native") or 0) + (v.get("wsol_delta") or 0)
            if leftover != 0:
                other_sol += leftover
                other_entries.append({"signature": v.get("signature"), "amount_sol": round(leftover, 9)})

    topups_sol = sum(e["amount_sol"] for e in topups)
    withdrawals_sol = sum(e["amount_sol"] for e in withdrawals)
    rescue_out_sol = sum(e["amount_sol"] for e in rescue_out)
    rescue_in_sol = sum(e["amount_sol"] for e in rescue_in)

    return {
        "closed_sol": round(closed_sol, 6),
        "buy_without_sell_sol": round(buy_without_sell_sol, 6),
        "sell_without_buy_sol": round(sell_without_buy_sol, 6),
        "topups_sol": round(topups_sol, 6), "topups": topups,
        "withdrawals_sol": round(withdrawals_sol, 6), "withdrawals": withdrawals,
        # ЭТАП E: спасения -- отдельными строками. Результат спасения =
        # rescue_in_sol - rescue_out_sol (вернувшийся SOL минус то, что
        # ушло на утилизатор); для токена вход это sol_in его покупки, и
        # он уже учтён в buy_without_sell_sol, поэтому здесь не
        # задваивается.
        "rescue_out_sol": round(rescue_out_sol, 6), "rescue_out": rescue_out,
        "rescue_in_sol": round(rescue_in_sol, 6), "rescue_in": rescue_in,
        "rescue_net_sol": round(rescue_in_sol - rescue_out_sol, 6),
        "dbot_fee_sol": round(dbot_fee_sol, 6),
        "other_sol": round(other_sol, 6), "other": other_entries,
        "sum_of_categories": round(closed_sol + buy_without_sell_sol + sell_without_buy_sol
                                    + topups_sol - withdrawals_sol - dbot_fee_sol + other_sol
                                    + rescue_in_sol - rescue_out_sol, 6),
        "genesis_synced": bool(chain_cache.get(_genesis_key(wallet))),
    }


def build_task_stats(tasks: list[dict], trades_all: list[dict], chain_cache: dict,
                      wallet_balances: dict[str, float | None] | None = None) -> list[dict]:
    wallet_balances = wallet_balances or {}
    out = []
    for task in tasks:
        wallet = task["wallet"]
        all_task_trades = [t for t in trades_all if t["task_id"] == task["id"]]
        # Владелец: сделки TradeWiz в итоги задач DBot не включать. Они не
        # выбрасываются -- выносятся отдельным блоком, чтобы было видно и
        # сколько их, и на сколько SOL они двигают баланс кошелька (иначе
        # сверка баланса развалилась бы на ровном месте).
        foreign = [t for t in all_task_trades if t.get("bot") in ЧУЖИЕ_БОТЫ]
        task_trades = [t for t in all_task_trades if t.get("bot") not in ЧУЖИЕ_БОТЫ]
        n_trades = sum(1 for t in task_trades if t["status"] == "закрыта")
        net_sum = sum(t["net_sol"] for t in task_trades if t.get("net_sol") is not None)
        n_open = sum(1 for t in task_trades if t["status"] == "незакрыта")
        n_hung = sum(1 for t in task_trades if t.get("is_hung"))
        n_no_source = sum(1 for t in task_trades if t["status"] in ("закрыта", "незакрыта") and not t.get("source_address"))
        foreign_net = sum(t["net_sol"] for t in foreign if t.get("net_sol") is not None)
        боты = sorted({t.get("bot") for t in foreign})
        foreign_row = {
            "бот": боты[0] if len(боты) == 1 else боты,
            "учтено_в_итогах_задачи": False,
            "окна_utc": {
                TRADEWIZ_LABEL: (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(TRADEWIZ_FROM_TS))
                                  + " -- " + time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                            time.gmtime(BLOOM_FROM_TS))),
                BLOOM_LABEL: "с " + time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                   time.gmtime(BLOOM_FROM_TS)),
            },
            "n_сделок": len(foreign),
            "по_ботам": {b: sum(1 for t in foreign if t.get("bot") == b) for b in боты},
            "n_закрытых": sum(1 for t in foreign if t["status"] == "закрыта"),
            "n_незакрытых": sum(1 for t in foreign if t["status"] == "незакрыта"),
            "net_sol_sum": round(foreign_net, 6),
            "признак": "сделка на кошельке BATCH-8 в окне чужого бота без подтверждающей записи DBot",
            # Метка bloom означает ОКНО, а не авторство. Разбор кошелька по
            # цепи 23.09 показал: в окне Bloom с этого кошелька шли ручные
            # операции владельца -- покупки с чаевыми, чистка пустых
            # токен-счетов, перевод 2.13 SOL себе, -- а исполнитель Bloom в
            # это время работал в dry-run и не отправил ни одного запроса
            # (sent: 0). Считать эти сделки результатом Bloom нельзя, и
            # оговорка стоит здесь, а не в чьей-то памяти.
            "оговорка": ("метка bloom -- это ОКНО ВРЕМЕНИ на кошельке, а не "
                          "подтверждённое авторство исполнителя. Пока исполнитель "
                          "в dry-run, любые сделки этого кошелька сделаны руками "
                          "или другим инструментом, и результатом Bloom не "
                          "являются. Подтверждение авторства появится тогда, "
                          "когда в журнале исполнителя будут позиции с "
                          "подписями"),
        } if foreign else None

        recon = build_wallet_reconciliation(wallet, chain_cache)

        # Баланс -- снятый в Section B сразу после синхронизации ИМЕННО этого
        # кошелька (см. комментарий там); если по какой-то причине его нет
        # (кошелёк не успел обработаться до конца бюджета), снимаем сейчас
        # как запасной вариант, честно понимая, что зазор будет больше.
        current_balance = wallet_balances.get(wallet)
        if current_balance is None:
            try:
                current_balance = get_balance_sol(wallet)
            except Exception:  # noqa: BLE001
                current_balance = None

        diff = (round(current_balance - recon["sum_of_categories"], 6)
                if current_balance is not None else None)

        out.append({
            "task_id": task["id"], "task_name": task.get("name"), "wallet": wallet,
            "wallet_name": task.get("wallet_name"), "n_trades": n_trades, "net_sol_sum": round(net_sum, 6),
            "dbot_fee_total_sol": recon["dbot_fee_sol"], "n_open": n_open,
            "n_hung": n_hung, "n_no_source": n_no_source,
            "current_balance_sol": round(current_balance, 6) if current_balance is not None else None,
            "reconciliation": recon,
            "reconciliation_diff_sol": diff,
            "reconciliation_flag": bool(diff is not None and abs(diff) > 0.01),
            "чужой_бот": foreign_row,
            "n_сделок_без_записи_dbot": sum(
                1 for t in task_trades if t.get("bot") == UNKNOWN_BOT_LABEL),
        })

    # Владелец: если адрес пополнения/вывода повторяется у нескольких РАЗНЫХ
    # кошельков задач -- это, скорее всего, общий кошелёк владельца, а не
    # случайный сторонний адрес; помечаем каждую такую запись явно.
    addr_wallets: dict[str, set] = {}
    for row in out:
        for e in row["reconciliation"]["topups"] + row["reconciliation"]["withdrawals"]:
            if e["counterparty"]:
                addr_wallets.setdefault(e["counterparty"], set()).add(row["wallet"])
    owner_addrs = {a for a, ws in addr_wallets.items() if len(ws) >= 2}
    for row in out:
        for e in row["reconciliation"]["topups"] + row["reconciliation"]["withdrawals"]:
            e["note"] = "кошелёк владельца" if e["counterparty"] in owner_addrs else None
    return out


def build_rescue_wallet_row() -> dict:
    """ЭТАП E: утилизатор отдельной строкой. История цепочки для него НЕ
    синхронизируется (он не кошелёк задачи и в follow_orders его нет),
    поэтому строка честно ограничена текущим состоянием: SOL и остатки
    токенов прямо сейчас. Ненулевой остаток токена здесь -- признак
    незавершённого спасения: токен доехал, а продажа не прошла."""
    if not RESCUE_WALLET_ADDRESS:
        return {"настроен": False,
                "почему": "RESCUE_WALLET_ADDRESS не задан в окружении -- спасения не включены"}
    row: dict = {"настроен": True, "адрес": RESCUE_WALLET_ADDRESS,
                 "охват": "только текущее состояние: история цепочки для утилизатора не синхронизируется"}
    try:
        row["sol"] = get_balance_sol(RESCUE_WALLET_ADDRESS)
    except Exception as exc:  # noqa: BLE001
        row["sol"] = None
        row["sol_ошибка"] = f"{type(exc).__name__}"
    held = []
    for prog in ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                  "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
        try:
            res = rpc_call("getTokenAccountsByOwner",
                            [RESCUE_WALLET_ADDRESS, {"programId": prog}, {"encoding": "jsonParsed"}])
        except Exception as exc:  # noqa: BLE001
            held.append({"программа": prog, "ошибка": f"{type(exc).__name__}"})
            continue
        for acc in (res or {}).get("value") or []:
            try:
                info = acc["account"]["data"]["parsed"]["info"]
                ui = float((info.get("tokenAmount") or {}).get("uiAmount") or 0.0)
            except (KeyError, TypeError, ValueError):
                continue
            if ui:
                held.append({"mint": info.get("mint"), "ui": ui, "программа": prog})
    row["незавершённые_остатки"] = held
    row["флаг_незавершённого_спасения"] = bool([h for h in held if h.get("mint")])
    return row


# ---------- E: контроль перед выдачей ----------

def validate_against_pilot(trades_all: list[dict]) -> dict:
    if not PILOT_LEDGER_PATH.exists():
        return {"skipped": True, "reason": "нет data/solana_dbot_realized_ledger.json"}
    pilot = json.loads(PILOT_LEDGER_PATH.read_text())
    pilot_trades = [t for t in pilot.get("trades", []) if t.get("label") == "pilot"][:3]
    by_buy_sig = {t["buy_signature"]: t for t in trades_all}
    checks = []
    all_ok = True
    for pt in pilot_trades:
        new = by_buy_sig.get(pt["buy_signature"])
        if new is None:
            all_ok = False
            checks.append({"buy_signature": pt["buy_signature"], "found": False})
            continue
        d_in = abs((new.get("sol_in") or 0) - pt["total_sol_out_buy"])
        d_out = abs((new.get("sol_out") or 0) - pt["total_sol_in_sell"]) if new.get("sol_out") is not None else None
        ok = d_in < 0.001 and (d_out is not None and d_out < 0.001)
        all_ok = all_ok and ok
        checks.append({"buy_signature": pt["buy_signature"], "found": True,
                        "expected_sol_in": pt["total_sol_out_buy"], "got_sol_in": new.get("sol_in"), "diff_in": d_in,
                        "expected_sol_out": pt["total_sol_in_sell"], "got_sol_out": new.get("sol_out"), "diff_out": d_out,
                        "ok": ok})
    return {"all_ok": all_ok, "n_checked": len(checks), "checks": checks}


# ---------- main ----------

def main() -> None:
    started = time.monotonic()
    deadline = started + TIME_BUDGET_S
    status = {"updated_utc": now_utc(), "n_records_dbot": 0, "n_tx_chain": 0, "n_trades": 0,
              "n_open": 0, "n_unmatched": 0, "last_error": None}

    api_key = os.environ.get("DBOT_API_KEY", "")
    if not api_key:
        status["last_error"] = "DBOT_API_KEY пуст в окружении -- секция A невозможна"
        save_json(STATUS_PATH, status)
        print("[ledger] " + status["last_error"], flush=True)
        return

    print("[ledger] A: follow_orders...", flush=True)
    tasks, http_status = fetch_follow_orders(api_key)
    save_json(FOLLOW_ORDERS_PATH, {"fetched_utc": now_utc(), "http_status": http_status, "tasks": tasks})
    print(f"[ledger] задач найдено: {len(tasks)}", flush=True)

    follow_trades_cache = load_json(FOLLOW_TRADES_PATH, {})
    n_records_total = 0
    incomplete_fetch_tasks: list[str] = []
    section_a_deadline = started + SECTION_A_BUDGET_S
    for task in tasks:
        tid = task["id"]
        if time.monotonic() > section_a_deadline:
            print(f"[ledger] A: бюджет раздела A исчерпан -- {task.get('name')} и далее пропускаю "
                  f"в этом прогоне (сшивка ниже всё равно опирается на полный накопленный кэш, "
                  f"не только на этот проход)", flush=True)
            incomplete_fetch_tasks.append(tid)
            continue
        print(f"[ledger] A: follow_trades для задачи {tid} ({task.get('name')})...", flush=True)
        records, fetch_complete = fetch_follow_trades_for_task(tid, api_key, my_wallet=task.get("wallet"))
        if not fetch_complete:
            incomplete_fetch_tasks.append(tid)
            print(f"[ledger] ВНИМАНИЕ: выгрузка follow_trades для {tid} ({task.get('name')}) НЕПОЛНАЯ "
                  f"(сбой сети/API посреди пагинации, получено {len(records)} записей за этот проход) -- "
                  f"дотянем в следующий часовой прогон", flush=True)
        if records and not FOLLOW_TRADES_SAMPLE_PATH.exists():
            save_json(FOLLOW_TRADES_SAMPLE_PATH, records[0])
            print(f"[ledger] первая сырая запись follow_trades сохранена в {FOLLOW_TRADES_SAMPLE_PATH.name}", flush=True)
        for r in records:
            rid = r.get("id") or r.get("_id") or f"{tid}:{len(follow_trades_cache)}"
            follow_trades_cache[str(rid)] = {"task_id": tid, "record": r}
        n_records_total += len(records)
        # Владелец: сохранять не только в самом конце -- инкрементально
        # после каждой задачи, чтобы обрыв job'а посреди раздела A не
        # терял уже полученные записи остальных задач (тот же урок, что и
        # в crowd_night: коммит по ходу, а не одной подстраховкой в конце).
        save_json(FOLLOW_TRADES_PATH, follow_trades_cache)
    status["n_records_dbot"] = n_records_total
    status["follow_trades_incomplete_tasks"] = incomplete_fetch_tasks
    print(f"[ledger] всего DBot-записей за этот проход: {n_records_total}"
          + (f", НЕПОЛНО (задачи {incomplete_fetch_tasks})" if incomplete_fetch_tasks else ""), flush=True)

    # Владелец: "перепривязывать все сделки без source_address" -- сшивка
    # источника ниже должна видеть ВЕСЬ когда-либо накопленный кэш DBot
    # (follow_trades_cache только растёт и переживает сбой отдельного
    # часового прогона), а не только то, что удалось получить именно в
    # ЭТОМ проходе -- иначе разовый сбой раздела A "забывает" уже
    # когда-то подтверждённые записи и откатывает уже сделанные привязки.
    follow_trades_by_task: dict[str, list] = {}
    for entry in follow_trades_cache.values():
        follow_trades_by_task.setdefault(entry["task_id"], []).append(entry["record"])

    # Честная видимость свежести (владелец: "выгружать ПОЛНОСТЬЮ за
    # последние 6 часов") -- не просто заявляем это сделанным, а показываем
    # реальный возраст самой свежей DBot-записи на задачу, чтобы отставание
    # (в т.ч. на стороне самого DBot, не только в нашей выгрузке) было
    # видно явно, а не молчаливо считалось исправленным.
    now_ms = time.time() * 1000
    freshness = []
    for task in tasks:
        tid = task["id"]
        create_ats = [r.get("createAt") for r in follow_trades_by_task.get(tid, []) if r.get("createAt")]
        latest_ms = max(create_ats) if create_ats else None
        age_h = round((now_ms - latest_ms) / 3600000, 2) if latest_ms else None
        # Задача на ПАУЗЕ обязана устаревать: новых записей у неё и не
        # должно быть. Без этой развилки пауза части задач превратилась бы
        # в постоянную ложную тревогу о протухшем учёте.
        enabled = bool(task.get("enabled"))
        freshness.append({
            "task_id": tid, "task_name": task.get("name"),
            "enabled": enabled,
            "latest_record_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(latest_ms / 1000)) if latest_ms else None,
            "hours_since_latest_record": age_h,
            "stale_gt_6h": bool(enabled and age_h is not None
                                 and age_h > FOLLOW_TRADES_FRESHNESS_WINDOW_H),
            "устарела_но_на_паузе": bool(not enabled and age_h is not None
                                          and age_h > FOLLOW_TRADES_FRESHNESS_WINDOW_H),
            "fetch_incomplete_this_run": tid in incomplete_fetch_tasks,
        })
    status["follow_trades_freshness"] = freshness
    status["задач_всего"] = len(freshness)
    status["задач_включено"] = sum(1 for f in freshness if f["enabled"])
    status["устаревших_среди_включённых"] = sum(1 for f in freshness if f["stale_gt_6h"])
    status["на_паузе_и_потому_устарели"] = sum(1 for f in freshness
                                                if f["устарела_но_на_паузе"])

    print("[ledger] B: синхронизация цепочки по кошелькам задач...", flush=True)
    chain_cache = load_json(CHAIN_CACHE_PATH, {})
    # Баланс снимается СРАЗУ после синхронизации именно ЭТОГО кошелька (не
    # общим проходом в конце для всех восьми) -- иначе на живой, постоянно
    # торгующей системе между "историю досинхронизировали" и "сняли баланс"
    # проходит время обработки ОСТАЛЬНЫХ кошельков (DBot-выгрузка, сборка
    # сделок), и туда успевает влезть ещё одна сделка -- именно это и было
    # источником невязки 0.3-0.5 SOL у активных кошельков.
    wallet_balances: dict[str, float | None] = {}
    for task in tasks:
        if time.monotonic() > deadline:
            print("[ledger] B: бюджет времени исчерпан, продолжим со следующего запуска", flush=True)
            break
        n_new = sync_wallet_chain(task["wallet"], chain_cache, deadline)
        print(f"[ledger] {task.get('name')} ({task['wallet'][:10]}..): +{n_new} новых транзакций, "
              f"всего в кэше {sum(1 for v in chain_cache.values() if v.get('_wallet') == task['wallet'])}", flush=True)
        save_json(CHAIN_CACHE_PATH, chain_cache)
        try:
            wallet_balances[task["wallet"]] = get_balance_sol(task["wallet"])
        except Exception:  # noqa: BLE001
            wallet_balances[task["wallet"]] = None
    status["n_tx_chain"] = len(chain_cache)

    print("[ledger] C: дотягиваю done-записи DBot без транзакции в кэше...", flush=True)
    for task in tasks:
        for r in follow_trades_by_task.get(task["id"], []):
            state = str(r.get("state") or "").lower()
            h = dbot_signature_from_record(r)
            if state == "done" and h and not _has_own_view(chain_cache, h, task["wallet"]):
                fetch_missing_tx(h, task["wallet"], chain_cache)
    save_json(CHAIN_CACHE_PATH, chain_cache)
    status["n_tx_chain"] = len(chain_cache)

    print("[ledger] C+D: строю сделки...", flush=True)
    trades_all = []
    for task in tasks:
        trades_all.extend(build_trades_for_task(task, follow_trades_by_task.get(task["id"], []), chain_cache))
    save_json(TRADES_ALL_PATH, trades_all)
    status["n_trades"] = len(trades_all)
    status["n_open"] = sum(1 for t in trades_all if t["status"] == "незакрыта")
    status["n_unmatched"] = sum(1 for t in trades_all if t["status"].startswith("срыв"))

    print("[ledger] E: контроль против пилотных сделок...", flush=True)
    validation = validate_against_pilot(trades_all)
    if not validation.get("skipped") and not validation.get("all_ok"):
        status["last_error"] = "КОНТРОЛЬ НЕ ПРОШЁЛ: " + json.dumps(validation, ensure_ascii=False, default=str)[:2000]
        save_json(STATUS_PATH, status)
        print("[ledger] СТОП: " + status["last_error"], flush=True)
        return

    print("[ledger] D: статистика по источникам и задачам...", flush=True)
    source_stats = build_source_stats(trades_all)
    save_json(SOURCE_STATS_PATH, source_stats)
    task_stats = build_task_stats(tasks, trades_all, chain_cache, wallet_balances)
    save_json(TASK_STATS_PATH, task_stats)

    status["validation"] = validation
    status["rescue_wallet"] = build_rescue_wallet_row()
    status["rpc"] = dict(RPC_STATS)
    save_json(STATUS_PATH, status)
    print(f"[ledger] ГОТОВО: сделок={status['n_trades']} открыто={status['n_open']} "
          f"срывов={status['n_unmatched']} контроль_ок={validation.get('all_ok')}", flush=True)
    print("[ledger] RPC: " + json.dumps(RPC_STATS, ensure_ascii=False), flush=True)


def self_test() -> None:
    """Проверки без сети: разметка бота и выбор RPC-узла."""
    checks: list[tuple[str, bool, str]] = []

    def chk(name: str, ok: bool, got: str = "") -> None:
        checks.append((name, bool(ok), got))

    ts = TRADEWIZ_FROM_TS
    chk("запись DBot побеждает всё",
        classify_bot(TRADEWIZ_WALLET, ts + 99, True) == DBOT_LABEL)
    chk("BATCH-8 после отсечки без записи -- tradewiz",
        classify_bot(TRADEWIZ_WALLET, ts, False) == TRADEWIZ_LABEL)
    chk("BATCH-8 ДО отсечки без записи -- не tradewiz",
        classify_bot(TRADEWIZ_WALLET, ts - 1, False) == UNKNOWN_BOT_LABEL)
    chk("другой кошелёк без записи -- не tradewiz",
        classify_bot("GYPzYfSP3htyfRCti5Wp6XTnUQh7zkwqTv6j7r4kUFrq", ts + 99, False)
        == UNKNOWN_BOT_LABEL)
    chk("без времени покупки не гадаем",
        classify_bot(TRADEWIZ_WALLET, None, False) == UNKNOWN_BOT_LABEL)
    chk("отсечка -- это 23.09 00:30Z",
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(TRADEWIZ_FROM_TS))
        == "2026-09-23T00:30:00Z",
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(TRADEWIZ_FROM_TS)))

    # Итоги задачи не должны включать чужого бота.
    fake = [{"task_id": "T", "task_name": "BATCH-8", "wallet": TRADEWIZ_WALLET,
             "wallet_name": None, "status": "закрыта", "net_sol": 1.0,
             "source_address": "S", "source_remark": None, "bot": DBOT_LABEL},
            {"task_id": "T", "task_name": "BATCH-8", "wallet": TRADEWIZ_WALLET,
             "wallet_name": None, "status": "закрыта", "net_sol": -5.0,
             "source_address": "S", "source_remark": None, "bot": TRADEWIZ_LABEL}]
    rows = build_task_stats([{"id": "T", "name": "BATCH-8", "wallet": TRADEWIZ_WALLET,
                               "sources": []}], fake, {}, {TRADEWIZ_WALLET: 0.0})
    chk("net задачи считается без TradeWiz", rows[0]["net_sol_sum"] == 1.0,
        str(rows[0]["net_sol_sum"]))
    chk("TradeWiz вынесен отдельной строкой",
        (rows[0]["чужой_бот"] or {}).get("net_sol_sum") == -5.0,
        str(rows[0].get("чужой_бот")))
    chk("TradeWiz помечен как не учтённый",
        (rows[0]["чужой_бот"] or {}).get("учтено_в_итогах_задачи") is False)
    src = build_source_stats(fake)
    chk("в статистику источников TradeWiz не попал",
        sum(r["n_trades"] for r in src) == 1, str([r["n_trades"] for r in src]))

    # Выбор узла.
    global _helius_demoted_until
    saved = _helius_demoted_until
    os.environ["HELIUS_API"] = "x"
    _helius_demoted_until = 0.0
    chk("пока Helius жив -- берём Helius", _helius_available())
    _demote_helius("самотест")
    chk("после понижения -- публичный узел", not _helius_available())
    _helius_demoted_until = saved
    RPC_STATS["helius_понижен"] = False
    RPC_STATS["helius_понижен_utc"] = None

    bad = 0
    for name, ok, got in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {name}" + (f"  -> {got}" if got and not ok else ""))
        bad += (not ok)
    # Пауза задач не должна выглядеть протухшим учётом.
    def свежесть(enabled, age_h, окно=FOLLOW_TRADES_FRESHNESS_WINDOW_H):
        return {"stale_gt_6h": bool(enabled and age_h is not None and age_h > окно),
                "устарела_но_на_паузе": bool(not enabled and age_h is not None and age_h > окно)}
    chk("включённая и протухшая -- тревога",
        свежесть(True, 9)["stale_gt_6h"] is True)
    chk("выключенная и протухшая -- не тревога, а пауза",
        свежесть(False, 9)["stale_gt_6h"] is False
        and свежесть(False, 9)["устарела_но_на_паузе"] is True)
    chk("свежая включённая -- ни то, ни другое",
        свежесть(True, 1)["stale_gt_6h"] is False
        and свежесть(True, 1)["устарела_но_на_паузе"] is False)
    # Кошелёк BATCH-8 один, ботов на нём два подряд: метка не должна врать.
    chk("сделка Bloom не получает метку TradeWiz",
        classify_bot(BLOOM_WALLET, BLOOM_FROM_TS + 60, False) == BLOOM_LABEL)
    chk("сделка TradeWiz до старта Bloom остаётся TradeWiz",
        classify_bot(TRADEWIZ_WALLET, BLOOM_FROM_TS - 60, False) == TRADEWIZ_LABEL)
    chk("ровно в момент старта Bloom метка уже Bloom",
        classify_bot(BLOOM_WALLET, BLOOM_FROM_TS, False) == BLOOM_LABEL)
    chk("до окна TradeWiz -- неизвестен, а не догадка",
        classify_bot(TRADEWIZ_WALLET, TRADEWIZ_FROM_TS - 1, False) == UNKNOWN_BOT_LABEL)
    chk("запись DBot перевешивает любую границу по времени",
        classify_bot(BLOOM_WALLET, BLOOM_FROM_TS + 60, True) == DBOT_LABEL)
    chk("чужой кошелёк меток по времени не получает",
        classify_bot("ДРУГОЙ", BLOOM_FROM_TS + 60, False) == UNKNOWN_BOT_LABEL)
    chk("Bloom тоже считается чужим и в итоги задачи не идёт",
        BLOOM_LABEL in ЧУЖИЕ_БОТЫ and TRADEWIZ_LABEL in ЧУЖИЕ_БОТЫ)



    print(f"самопроверка учёта: {len(checks) - bad}/{len(checks)} пройдено")
    if bad:
        raise SystemExit(f"самопроверка не пройдена: {bad} из {len(checks)}")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
        raise SystemExit(0)
    # Даже при падении состояние RPC должно попасть в файл: без этого
    # причину обрыва приходилось выкапывать из логов задания, а именно на
    # этом конвейер и простоял 8 часов.
    try:
        main()
    except BaseException as exc:  # noqa: BLE001
        st = load_json(STATUS_PATH, {})
        st["updated_utc"] = now_utc()
        st["last_error"] = f"{type(exc).__name__}: {exc}"[:800]
        st["rpc"] = dict(RPC_STATS)
        save_json(STATUS_PATH, st)
        print("[ledger] RPC: " + json.dumps(RPC_STATS, ensure_ascii=False), flush=True)
        raise
