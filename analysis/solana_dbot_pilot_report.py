#!/usr/bin/env python3
"""Владелец, 2026-09-18: пилот на Solana запущен через интерфейс DBot
(владелец настроил сам). Эта задача -- ТОЛЬКО данные: забрать историю
ордеров пилотного кошелька через DBot API, дополнить с цепи через
Alchemy (сделка лидера Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit
непосредственно перед нашей по тому же токену), посчитать задержку в
блоках/секундах, дать таблицу. Никаких вызовов на покупку/продажу здесь
нет вообще -- только GET-запросы к DBot и чтение цепи.

БЕЗОПАСНОСТЬ (урок инцидента этой сессии с BITQUERY_APIKEY, см. git log
"SECURITY: redact leaked..."): секрет НИКОГДА не подставляется напрямую
в HTTP-заголовок без проверки на переводы строк/управляющие символы;
любой текст исключения/ответа сервера, который может содержать секрет,
прогоняется через _scrub_all() перед печатью/записью на диск.

Формат ответа DBot API не подтверждён чтением документации (сеть из
этой песочницы на *.dbotx.com заблокирована прокси, подтверждено
повторно) -- только через поисковые сниппеты (см. отчёт в диалоге).
Поэтому это ПЕРВЫЙ РЕАЛЬНЫЙ КОНТАКТ с API: скрипт пробует
задокументированные (по сниппетам) эндпоинты, честно дампит сырые
ответы для диагностики, и адаптируется по факту -- ничего не гадаем
про поля, которых не увидели.

Известные из разведки факты (см. диалог сессии, с источниками):
  - Base URL торгового API: https://api-bot-v1.dbotx.com
  - Заголовок авторизации данных-WS: x-api-key (реальный пример кода) --
    пробуем его же для REST, с фолбэком на Authorization: Bearer.
  - GET /automation/follow_orders -- список копитрейдинг-задач
    пользователя (упомянут в первом же поисковом снаппете).
  - GET /dex/poolinfo?chain=solana&pair={pair} -- инфо по пулу.
"""
from __future__ import annotations

import json
import os
import time
from decimal import Decimal as D
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dbot_pilot_report.json"

DBOT_BASE = "https://api-bot-v1.dbotx.com"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"

_ACTIVE_SECRETS: list[str] = []


def _scrub_all(text: str) -> str:
    for s in _ACTIVE_SECRETS:
        if s:
            text = text.replace(s, "[REDACTED_SECRET]")
    return text


# ---------- DBot REST ----------

def dbot_get(path: str, api_key: str, params: dict | None = None) -> dict:
    """Пробует несколько правдоподобных схем авторизации (см. докстринг) --
    ни одна не подтверждена документацией впрямую, честно фиксируем,
    какая сработала. НИКОГДА не кладём api_key в заголовок, не проверив
    его на управляющие символы (см. докстринг, инцидент с BITQUERY_APIKEY)."""
    if any(c in api_key for c in ("\n", "\r")):
        raise RuntimeError(
            "DBOT_API_KEY содержит перевод строки -- похоже на многострочную метку "
            "дашборда (см. инцидент BITQUERY_APIKEY этой же сессии), не сырой ключ. "
            "Останавливаюсь до ручного разбора формата, не отправляю как есть в заголовок."
        )
    header_variants = [
        {"x-api-key": api_key},
        {"Authorization": f"Bearer {api_key}"},
        {"X-Api-Key": api_key, "apiKey": api_key},
    ]
    last: dict = {}
    for headers in header_variants:
        try:
            resp = requests.get(f"{DBOT_BASE}{path}", params=params or {}, headers=headers, timeout=30)
        except Exception as exc:  # noqa: BLE001
            last = {"exception": _scrub_all(f"{type(exc).__name__}: {exc}")}
            continue
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {"non_json_body": _scrub_all(resp.text[:1000])}
        last = {"http_status": resp.status_code, "body": body, "auth_tried": list(headers.keys())}
        if resp.status_code == 200:
            return last
    return last


# ---------- Solana RPC (Alchemy, тот же паттерн, что и в остальном проекте) ----------

_working_endpoint: tuple[str, str] | None = None
_last_call_at = 0.0
REQUEST_INTERVAL_S = 0.15


def rpc_endpoints() -> list[tuple[str, str]]:
    out = []
    key = os.environ.get("ALCHEMY_API_KEY", "")
    if key:
        out.append(("alchemy_solana", f"https://solana-mainnet.g.alchemy.com/v2/{key}"))
    out.append(("public_solana", PUBLIC_RPC))
    return out


def rpc_call(method: str, params: list, max_retries: int = 6) -> dict | None:
    global _working_endpoint, _last_call_at
    candidates = [_working_endpoint] if _working_endpoint else rpc_endpoints()
    last_exc = None
    for name, url in candidates:
        for attempt in range(max_retries):
            wait = REQUEST_INTERVAL_S - (time.monotonic() - _last_call_at)
            if wait > 0:
                time.sleep(wait)
            _last_call_at = time.monotonic()
            try:
                resp = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            if resp.status_code in (401, 403):
                last_exc = RuntimeError(f"{name}: HTTP {resp.status_code}")
                break
            if not resp.ok:
                last_exc = RuntimeError(f"{name}: HTTP {resp.status_code} -- {resp.text[:200]}")
                time.sleep(2 * (attempt + 1))
                continue
            body = resp.json()
            if "error" in body:
                err = body["error"]
                if err.get("code") == 429 or "rate" in str(err.get("message", "")).lower():
                    time.sleep(3 * (attempt + 1))
                    continue
                raise RuntimeError(f"{name}: RPC error {err}")
            if _working_endpoint is None:
                _working_endpoint = (name, url)
                print(f"[dbot_pilot] рабочий RPC-эндпоинт: {name}", flush=True)
            return body.get("result")
    raise RuntimeError(f"Все RPC-эндпоинты отказали: {last_exc}")


def get_transaction(sig: str) -> dict | None:
    return rpc_call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])


def find_leader_entry_before(mint: str, before_time: int) -> dict:
    """Ищет сделку лидера по этому минту непосредственно ПЕРЕД our_time.
    Метод идентичен task_solana_wallet_first_buy.py: ATA лидера для этого
    минта -> история подписей ATA -> последняя покупка (рост баланса) на
    момент <= before_time. Честно возвращает status=no_token_account /
    no_purchase_found, если не нашли -- не выдумываем."""
    ata_result = rpc_call("getTokenAccountsByOwner", [LEADER_WALLET, {"mint": mint}, {"encoding": "jsonParsed"}])
    accounts = (ata_result or {}).get("value", [])
    if not accounts:
        return {"status": "no_token_account_for_leader"}

    for acc in accounts:
        ata = acc["pubkey"]
        sigs, before = [], None
        for _ in range(20):  # до 20k подписей на ATA -- честный предел, не бесконечный
            batch = rpc_call("getSignaturesForAddress", [ata, {"limit": 1000, "before": before}]) or []
            if not batch:
                break
            sigs.extend(batch)
            if batch[-1].get("blockTime") and batch[-1]["blockTime"] < before_time - 3600:
                break  # ушли больше чем на час раньше нужного момента -- дальше не нужно
            before = batch[-1]["signature"]
            if len(batch) < 1000:
                break

        candidates = [s for s in sigs if s.get("blockTime") and s["blockTime"] <= before_time and s.get("err") is None]
        candidates.sort(key=lambda s: s["blockTime"], reverse=True)
        for s in candidates[:5]:  # пробуем несколько подряд на случай, если ближайшая -- не покупка (напр. перевод)
            tx = get_transaction(s["signature"])
            if not tx:
                continue
            info = _extract_buy_from_tx(tx, LEADER_WALLET, mint)
            if info:
                info["ata"] = ata
                info["signature"] = s["signature"]
                info["slot"] = tx.get("slot")
                info["block_time"] = tx.get("blockTime")
                info["status"] = "ok"
                return info
    return {"status": "no_purchase_found_in_recent_history"}


def _extract_buy_from_tx(tx: dict, wallet: str, mint: str) -> dict | None:
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None
    pre_tb, post_tb = meta.get("preTokenBalances") or [], meta.get("postTokenBalances") or []
    pre_by_idx = {r["accountIndex"]: r for r in pre_tb}
    post_by_idx = {r["accountIndex"]: r for r in post_tb}
    token_delta, pre_amt_at_mint = None, None
    for idx, post in post_by_idx.items():
        if post.get("mint") != mint or post.get("owner") != wallet:
            continue
        pre = pre_by_idx.get(idx)
        pre_amt = float(pre["uiTokenAmount"]["uiAmount"]) if pre and pre["uiTokenAmount"]["uiAmount"] is not None else 0.0
        post_amt = float(post["uiTokenAmount"]["uiAmount"]) if post["uiTokenAmount"]["uiAmount"] is not None else 0.0
        if post_amt - pre_amt > 0:
            token_delta = post_amt - pre_amt
            pre_amt_at_mint = pre_amt
            break
    if token_delta is None:
        return None

    paid_usdc = None
    for idx, post in post_by_idx.items():
        if post.get("mint") != USDC_MINT or post.get("owner") != wallet:
            continue
        pre = pre_by_idx.get(idx)
        pre_amt = float(pre["uiTokenAmount"]["uiAmount"]) if pre and pre["uiTokenAmount"]["uiAmount"] is not None else 0.0
        post_amt = float(post["uiTokenAmount"]["uiAmount"]) if post["uiTokenAmount"]["uiAmount"] is not None else 0.0
        if post_amt < pre_amt:
            paid_usdc = pre_amt - post_amt

    return {
        "tokens_received": token_delta,
        "paid_usdc": paid_usdc,
        "zero_balance_before": pre_amt_at_mint == 0.0,
    }


def main() -> None:
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    api_key = os.environ.get("DBOT_API_KEY", "")
    out["dbot_key_present"] = bool(api_key)
    if not api_key:
        out["HONEST_ANSWER"] = "DBOT_API_KEY пуст в окружении."
        _finish(out)
        return
    if not any(c in api_key for c in ("\n", "\r")):
        _ACTIVE_SECRETS.append(api_key)  # многострочный секрет намеренно НЕ добавляем в скраб-лист как есть -- см. dbot_get()

    # ---------- Шаг 1: разведка -- список копитрейдинг-задач (даёт адрес пилотного кошелька) ----------
    r1 = dbot_get("/automation/follow_orders", api_key)
    out["step1_follow_orders_raw"] = r1
    print(f"[dbot_pilot] Шаг 1 (follow_orders): http={r1.get('http_status')} auth={r1.get('auth_tried')}", flush=True)

    if r1.get("http_status") != 200:
        out["HONEST_ANSWER"] = (
            "GET /automation/follow_orders не прошёл ни с одним из опробованных вариантов "
            "авторизации -- см. step1_follow_orders_raw для диагностики (реальный код/тело "
            "ответа DBot, не выдумано). Дальше не иду, пока не подтверждён рабочий способ "
            "авторизации и реальная форма ответа."
        )
        print("[dbot_pilot] " + out["HONEST_ANSWER"], flush=True)
        _finish(out)
        return

    body1 = r1.get("body") or {}
    out["step1_note"] = (
        "Реальная форма ответа DBot (см. step1_follow_orders_raw) -- дальнейший код этого "
        "прогона извлекает адрес кошелька/сделки из неё по факту структуры, не по угаданной "
        "заранее схеме."
    )

    # Пытаемся вытащить список задач максимально defensively -- реальная
    # форма (список? {data:[...]}? {tasks:[...]}?) станет известна только
    # после первого реального ответа (см. step1_follow_orders_raw).
    tasks = body1 if isinstance(body1, list) else (
        body1.get("data") or body1.get("tasks") or body1.get("list") or body1.get("result") or []
    )
    out["step1_n_tasks_found"] = len(tasks) if isinstance(tasks, list) else "не список -- см. raw"

    _finish(out)
    print(f"[dbot_pilot] Записано {OUT_PATH} -- см. step1_follow_orders_raw для реальной схемы, "
          "следующий проход строит таблицу сделок по ней.", flush=True)


def _finish(out: dict) -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(_scrub_all(json.dumps(out, ensure_ascii=False, indent=2, default=str)))


if __name__ == "__main__":
    main()
