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

SAMPLE_PATH = REPO_ROOT / "data" / "dbot_sold_position_guard_sample.json"

DEFAULT_INTERVAL_S = 45
DEFAULT_STUCK_THRESHOLD = 10
DEFAULT_API_DOWN_ALERT_S = 600
DEFAULT_MAX_SLIPPAGE = 0.4
DEFAULT_SELL_RETRIES = 3

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
    тихо считаем, что список кончился."""
    out = []
    page = 1
    while True:
        status, body = dbot_get(
            "/automation/pnl_orders_from_follow_order",
            {"chain": SOLANA_CHAIN, "state": "expired", "size": 20, "page": page},
            api_key,
        )
        if status is None:
            return out, False
        items = extract_items(body)
        if items and not SAMPLE_PATH.exists():
            SAMPLE_PATH.write_text(_scrub_all(json.dumps(items[0], ensure_ascii=False, indent=2, default=str)))
            log.info("сырая первая expired-запись сохранена в %s", SAMPLE_PATH.name)
        if not items:
            return out, True
        out.extend(items)
        page += 1
        if page > 500:
            return out, False  # честно: упёрлись в защитный потолок, не в реальный конец


def sell_100_percent(mint: str, wallet_id: str, api_key: str) -> tuple[int | None, dict]:
    body = {
        "chain": SOLANA_CHAIN,
        "pair": mint,
        "walletIdList": [wallet_id],
        "type": "sell",
        "sellPercent": 1.0,
        "maxSlippage": DEFAULT_MAX_SLIPPAGE,
        "retries": DEFAULT_SELL_RETRIES,
    }
    return dbot_post("/automation/swap_orders_with_multi_wallets", body, api_key)


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
            stuck_alerted_at_attempts INTEGER NOT NULL DEFAULT 0
        )"""
    )
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
    conn.execute(
        """INSERT INTO positions (key, wallet_address, wallet_id, mint, task_name, state, first_seen_utc,
                                   last_checked_utc, attempts, last_balance)
           VALUES (?, ?, ?, ?, ?, 'watching', ?, ?, 1, ?)
           ON CONFLICT(key) DO UPDATE SET state='watching', wallet_id=excluded.wallet_id,
                                           last_checked_utc=excluded.last_checked_utc,
                                           attempts=positions.attempts + 1, last_balance=excluded.last_balance""",
        (key, wallet, wallet_id, mint, task_name, now_utc(), now_utc(), balance),
    )
    conn.commit()
    return conn.execute("SELECT attempts FROM positions WHERE key=?", (key,)).fetchone()["attempts"]


def db_record_sell(conn: sqlite3.Connection, key: str, response_summary: str) -> None:
    conn.execute(
        "UPDATE positions SET last_sell_attempt_utc=?, last_sell_response=? WHERE key=?",
        (now_utc(), response_summary, key),
    )
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
            was_watching = row is not None and row["state"] == "watching"
            prior_balance = row["last_balance"] if row is not None else None
            first_seen = row["first_seen_utc"] if row is not None else now_utc()
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
                msg = (f"По токену {mint} на кошельке {wallet} ({task_name}) {attempts} кругов подряд "
                       f"без продажи (баланс {balance}) -- нужна ручная помощь.")
                log.error(msg)
                send_telegram(telegram_token, telegram_chat_id, msg)
                db_mark_stuck_alerted(conn, key, attempts)

        if live_sell:
            status, resp = sell_100_percent(mint, wallet_id, dbot_key)
            summary = _scrub_all(json.dumps({"http_status": status, "body": resp}, ensure_ascii=False, default=str)[:500])
            db_record_sell(conn, key, summary)
            audit_log(audit_path, {"event": "sell_attempt", "task": task_name, "wallet": wallet, "mint": mint,
                                    "balance_before": balance, "http_status": status, "response": resp})
            log.info("продажа отправлена: %s", summary)
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
        "wallets_refresh_every": int(os.environ.get("GUARD_WALLETS_REFRESH_CYCLES", 20)),
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true",
                         help="Только прочитать expired-список и показать таблицу баланса -- ничего не продаёт, "
                              "не пишет в SQLite.")
    parser.add_argument("--once", action="store_true", help="Один цикл и выход (для теста), не бесконечный цикл.")
    args = parser.parse_args()

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
            if cycle_n > 1 and cycle_n % cfg["wallets_refresh_every"] == 0:
                our_wallets = fetch_our_wallets(cfg["dbot_key"])
                log.info("список наших кошельков обновлён: %d", len(our_wallets))

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
