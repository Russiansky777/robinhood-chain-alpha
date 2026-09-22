#!/usr/bin/env python3
"""СПАСЕНИЕ, ЭТАП B -- проверки ДО того, как на хосте появится ключ.

Смысл этапа: `POST /coin_tools/distribute` -- несущая деталь всей схемы.
Без него токен с кошелька задачи не сдвинуть, потому что приватный ключ
этого кошелька у DBot, а не у нас. Если distribute не работает (или не
работает для Token-2022 с комиссией за перевод) -- спасение невозможно,
и это надо узнать ДО того, как класть приватный ключ на VPS.

ПАРАМЕТРЫ ЭНДПОИНТА -- по документации, выданной владельцем
(docs.dbotx.com/reference/create-token-multisender):
  POST https://api-bot-v1.dbotx.com/coin_tools/distribute   -- 5 кредитов
  chain, fromWalletId, toList[{address, amountUI}], token ("" = нативный
  SOL), gasFeeDelta, maxFeePerGas, priorityFee.
Своими глазами документацию я не читал -- сеть на *.dbotx.com из моего
контейнера заблокирована. Поэтому сырой ответ эндпоинта целиком
сохраняется в выгрузку: если реальная форма разойдётся с ожидаемой, это
будет видно, а не потеряется.

ТРИ ТЕСТА:
  sol        -- 0.001 SOL с одного кошелька задачи на другой.
  token2022  -- пыль реального Token-2022 С КОМИССИЕЙ ЗА ПЕРЕВОД с
                одного кошелька задачи на другой. Тест на SOL про наш
                случай не говорит ничего: 9 из 11 зависших были
                Token-2022, и именно комиссия -- единственный
                подозреваемый сверх маршрута.
  ultra      -- Jupiter Ultra на кошельке-утилизаторе. ТРЕБУЕТ
                RESCUE_WALLET_KEY; пока секрета нет, тест честно
                сообщает, что отложен, и ничего не делает.

БЕЗОПАСНОСТЬ И ГРАНИЦЫ:
  * Получатель -- ТОЛЬКО адрес из /automation/follow_orders, взятый
    живьём. Адрес из истории транзакций не используется никогда: в
    истории бывают поддельные адреса-двойники.
  * Без --confirm ничего не отправляется: печатается ровно то тело,
    которое ушло бы.
  * Потолок суммы на тест зашит в коде (MAX_SOL_PER_TEST), выше -- стоп.
  * Результат проверяется ПО ЦЕПИ (баланс отправителя и получателя до и
    после), а не по ответу API: "принято" -- это не "исполнено".
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "data" / "dbot_rescue_stage_b_tests.json"
RAW_SAMPLE = REPO / "data" / "dbot_distribute_raw_sample.json"

DBOT_HOST = "https://api-bot-v1.dbotx.com"
CHAIN = "solana"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

SOL_TEST_AMOUNT = 0.001
MAX_SOL_PER_TEST = 0.01          # жёсткий потолок этого скрипта
DISTRIBUTE_CREDITS = 5           # по документации владельца

_SECRETS: list[str] = []


def scrub(text: str) -> str:
    for s in _SECRETS:
        if s and len(s) > 6:
            text = text.replace(s, "<секрет>")
    return text


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S', time.gmtime())}Z] {scrub(msg)}", flush=True)


def env(*names: str) -> tuple[str, str]:
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            if "\n" in v or "\r" in v:
                raise RuntimeError(f"{n} содержит перевод строки -- это не сырой ключ, останавливаюсь")
            _SECRETS.append(v)
            return v, n
    raise RuntimeError(f"нет ни одной из переменных окружения: {', '.join(names)}")


# ---------- DBot ----------

def dbot_get(path: str, params: dict, key: str) -> tuple[int | None, dict]:
    for a in range(5):
        try:
            r = requests.get(f"{DBOT_HOST}{path}", params=params,
                             headers={"X-API-KEY": key}, timeout=30)
        except Exception as exc:  # noqa: BLE001
            log(f"dbot_get {path} попытка {a+1}/5: {type(exc).__name__}")
            time.sleep(2 * (a + 1))
            continue
        if r.status_code == 429:
            time.sleep(3 * (a + 1))
            continue
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"non_json_body": scrub(r.text[:500])}
    return None, {}


def dbot_post(path: str, body: dict, key: str) -> tuple[int | None, dict]:
    for a in range(3):
        try:
            r = requests.post(f"{DBOT_HOST}{path}", json=body,
                              headers={"X-API-KEY": key}, timeout=40)
        except Exception as exc:  # noqa: BLE001
            log(f"dbot_post {path} попытка {a+1}/3: {type(exc).__name__}")
            time.sleep(2 * (a + 1))
            continue
        if r.status_code == 429:
            time.sleep(3 * (a + 1))
            continue
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"non_json_body": scrub(r.text[:800])}
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


def fetch_wallets(key: str) -> list[dict]:
    """Белый список: адрес + walletId живьём из follow_orders."""
    st, body = dbot_get("/automation/follow_orders", {}, key)
    if st != 200:
        raise RuntimeError(f"follow_orders вернул http={st} -- список кошельков не построить")
    items = extract_items(body)
    if not items and not isinstance(body, (list, dict)):
        raise RuntimeError("follow_orders: тело не похоже на список")
    out = []
    seen = set()
    for r in items:
        addr, wid = r.get("walletAddress"), r.get("walletId")
        if addr and wid and addr not in seen:
            seen.add(addr)
            out.append({"address": addr, "walletId": wid, "name": r.get("name") or addr})
    if not out:
        raise RuntimeError("follow_orders вернул 0 задач -- белый список пуст, не продолжаю")
    return out


def distribute(from_wallet_id: str, to_address: str, amount_ui: float, token: str,
               key: str) -> tuple[int | None, dict, dict]:
    """token="" -- нативный SOL; иначе адрес минта."""
    body = {
        "chain": CHAIN,
        "fromWalletId": from_wallet_id,
        "toList": [{"address": to_address, "amountUI": amount_ui}],
        "token": token,
    }
    shown = {**body, "fromWalletId": "<walletId>"}
    log(f"ОТПРАВЛЯЮ distribute ({DISTRIBUTE_CREDITS} кредитов): {json.dumps(shown, ensure_ascii=False)}")
    st, resp = dbot_post("/coin_tools/distribute", body, key)
    log(f"ответ: http={st} тело={scrub(json.dumps(resp, ensure_ascii=False, default=str)[:900])}")
    return st, resp, shown


# ---------- цепь ----------

def rpc(method: str, params: list, key: str):
    url = f"https://mainnet.helius-rpc.com/?api-key={key}"
    last = None
    for a in range(5):
        try:
            r = requests.post(url, json={"jsonrpc": "2.0", "id": 1,
                                          "method": method, "params": params}, timeout=45)
        except Exception as exc:  # noqa: BLE001
            last = type(exc).__name__
            time.sleep(1.5 * (a + 1))
            continue
        if r.status_code == 429 or 500 <= r.status_code < 600:
            last = f"http={r.status_code}"
            time.sleep(2 * (a + 1))
            continue
        if not r.ok:
            raise RuntimeError(f"{method}: http={r.status_code}")
        b = r.json()
        if "error" in b:
            raise RuntimeError(f"{method}: {json.dumps(b['error'])[:200]}")
        return b.get("result")
    raise RuntimeError(f"{method}: исчерпаны попытки ({last})")


def sol_balance(addr: str, key: str) -> float:
    res = rpc("getBalance", [addr], key)
    return ((res or {}).get("value") or 0) / 1e9


def token_balance(addr: str, mint: str, key: str) -> tuple[float, int, int | None]:
    res = rpc("getTokenAccountsByOwner", [addr, {"mint": mint}, {"encoding": "jsonParsed"}], key)
    ui, raw, dec = 0.0, 0, None
    for acc in (res or {}).get("value") or []:
        try:
            ta = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
            ui += float(ta.get("uiAmount") or 0.0)
            raw += int(ta.get("amount") or 0)
            dec = ta.get("decimals", dec)
        except (KeyError, TypeError, ValueError):
            continue
    return ui, raw, dec


def mint_info(mint: str, key: str) -> dict:
    res = rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}], key)
    val = (res or {}).get("value") or {}
    info = ((val.get("data") or {}).get("parsed") or {}).get("info", {})
    fee_bps = None
    for e in info.get("extensions") or []:
        if isinstance(e, dict) and e.get("extension") == "transferFeeConfig":
            fee_bps = ((e.get("state") or {}).get("newerTransferFee") or {}).get("transferFeeBasisPoints")
    return {"token2022": val.get("owner") == TOKEN_2022,
            "transfer_fee_bps": fee_bps, "decimals": info.get("decimals")}


def wait_change(check, before, key: str, timeout_s: int = 90, step_s: int = 6) -> tuple[bool, object]:
    """Ждём, пока величина изменится. Возвращаем (изменилась, последнее
    значение). Фиксированную паузу не ставим: подтверждение слота может
    занять и 5 секунд, и минуту."""
    deadline = time.time() + timeout_s
    last = before
    while time.time() < deadline:
        time.sleep(step_s)
        try:
            last = check()
        except RuntimeError as exc:
            log(f"чтение баланса не удалось, повторю: {exc}")
            continue
        if last != before:
            return True, last
    return False, last


# ---------- тесты ----------

def test_sol(wallets: list[dict], dbot_key: str, hel_key: str, confirm: bool) -> dict:
    bal = []
    for w in wallets:
        try:
            bal.append({**w, "sol": sol_balance(w["address"], hel_key)})
        except RuntimeError as exc:
            bal.append({**w, "sol": None, "ошибка": str(exc)[:120]})
    ok = [w for w in bal if w.get("sol") is not None]
    if len(ok) < 2:
        return {"тест": "sol", "итог": "меньше двух кошельков с прочитанным балансом -- не выполняю"}
    ok.sort(key=lambda w: -w["sol"])
    src, dst = ok[0], ok[-1]
    need = SOL_TEST_AMOUNT + 0.001  # запас на комиссию сети
    r: dict = {"тест": "sol", "сумма_SOL": SOL_TEST_AMOUNT,
               "балансы_до": [{"имя": w["name"], "адрес": w["address"], "sol": w["sol"]} for w in bal],
               "отправитель": {"имя": src["name"], "адрес": src["address"], "sol": src["sol"]},
               "получатель": {"имя": dst["name"], "адрес": dst["address"], "sol": dst["sol"]},
               "получатель_в_белом_списке": dst["address"] in {w["address"] for w in wallets}}
    if SOL_TEST_AMOUNT > MAX_SOL_PER_TEST:
        r["итог"] = f"сумма выше потолка скрипта {MAX_SOL_PER_TEST} SOL -- стоп"
        return r
    if src["sol"] < need:
        r["итог"] = f"у самого богатого кошелька {src['sol']} SOL, нужно >= {need} -- не отправляю"
        return r
    if src["address"] == dst["address"]:
        r["итог"] = "отправитель и получатель совпали -- не отправляю"
        return r
    if not confirm:
        r["итог"] = "СУХОЙ ПРОГОН: --confirm не передан, distribute НЕ отправлен"
        r["тело_которое_ушло_бы"] = {"chain": CHAIN, "fromWalletId": "<walletId отправителя>",
                                      "toList": [{"address": dst["address"], "amountUI": SOL_TEST_AMOUNT}],
                                      "token": ""}
        return r

    st, resp, shown = distribute(src["walletId"], dst["address"], SOL_TEST_AMOUNT, "", dbot_key)
    r["http"] = st
    r["ответ"] = json.loads(scrub(json.dumps(resp, ensure_ascii=False, default=str)))
    r["кредитов_списано_по_документации"] = DISTRIBUTE_CREDITS
    changed, after = wait_change(lambda: sol_balance(dst["address"], hel_key), dst["sol"], hel_key)
    r["баланс_получателя_после"] = after
    r["пришло_SOL"] = round(after - dst["sol"], 9) if after is not None else None
    try:
        r["баланс_отправителя_после"] = sol_balance(src["address"], hel_key)
        r["ушло_с_отправителя_SOL"] = round(src["sol"] - r["баланс_отправителя_после"], 9)
    except RuntimeError as exc:
        r["баланс_отправителя_после"] = f"не прочитан: {exc}"
    r["итог"] = ("ПЕРЕВОД ПОДТВЕРЖДЁН ЦЕПЬЮ" if changed else
                 "баланс получателя не изменился за отведённое время -- перевод НЕ подтверждён")
    return r


def test_token2022(wallets: list[dict], dbot_key: str, hel_key: str, confirm: bool,
                    mint: str, share: float) -> dict:
    r: dict = {"тест": "token2022", "минт": mint, "доля_остатка": share}
    r["сведения_о_минте"] = mint_info(mint, hel_key)
    holders = []
    for w in wallets:
        try:
            ui, raw, dec = token_balance(w["address"], mint, hel_key)
        except RuntimeError as exc:
            holders.append({**w, "ошибка": str(exc)[:120]})
            continue
        holders.append({**w, "ui": ui, "raw": raw, "decimals": dec})
    r["остатки"] = [{"имя": h["name"], "адрес": h["address"], "ui": h.get("ui"),
                     "raw": h.get("raw")} for h in holders]
    with_bal = [h for h in holders if (h.get("raw") or 0) > 0]
    if not with_bal:
        r["итог"] = ("ни на одном кошельке задач нет остатка этого минта -- "
                     "тест выполнить не на чем (минт задаётся --t22-mint)")
        return r
    src = max(with_bal, key=lambda h: h["raw"])
    dst = next((w for w in wallets if w["address"] != src["address"]), None)
    if dst is None:
        r["итог"] = "второго кошелька в белом списке нет -- не отправляю"
        return r
    dec = src.get("decimals") or 0
    amount_ui = round(src["ui"] * share, max(dec, 0))
    if amount_ui <= 0:
        # при крошечном остатке доля округляется в ноль -- шлём один
        # наименьший разряд, а не выдумываем сумму
        amount_ui = 10 ** (-dec) if dec else 1
    r["отправитель"] = {"имя": src["name"], "адрес": src["address"], "ui": src["ui"], "raw": src["raw"]}
    r["получатель"] = {"имя": dst["name"], "адрес": dst["address"]}
    r["получатель_в_белом_списке"] = True
    r["сумма_ui"] = amount_ui
    try:
        dst_ui_before, dst_raw_before, _ = token_balance(dst["address"], mint, hel_key)
    except RuntimeError as exc:
        r["итог"] = f"баланс получателя не прочитан: {exc}"
        return r
    r["получатель_до"] = {"ui": dst_ui_before, "raw": dst_raw_before}
    if amount_ui > src["ui"]:
        r["итог"] = "сумма больше остатка -- не отправляю"
        return r
    if not confirm:
        r["итог"] = "СУХОЙ ПРОГОН: --confirm не передан, distribute НЕ отправлен"
        r["тело_которое_ушло_бы"] = {"chain": CHAIN, "fromWalletId": "<walletId отправителя>",
                                      "toList": [{"address": dst["address"], "amountUI": amount_ui}],
                                      "token": mint}
        return r

    st, resp, shown = distribute(src["walletId"], dst["address"], amount_ui, mint, dbot_key)
    r["http"] = st
    r["ответ"] = json.loads(scrub(json.dumps(resp, ensure_ascii=False, default=str)))
    r["кредитов_списано_по_документации"] = DISTRIBUTE_CREDITS
    changed, after_raw = wait_change(
        lambda: token_balance(dst["address"], mint, hel_key)[1], dst_raw_before, hel_key)
    r["получатель_после_raw"] = after_raw
    if changed and after_raw is not None:
        arrived_raw = after_raw - dst_raw_before
        sent_raw = int(round(amount_ui * (10 ** dec)))
        r["отправлено_raw"] = sent_raw
        r["пришло_raw"] = arrived_raw
        r["удержано_raw"] = sent_raw - arrived_raw
        r["удержано_доля_%"] = round((sent_raw - arrived_raw) / sent_raw * 100, 4) if sent_raw else None
        r["итог"] = "ПЕРЕВОД Token-2022 ПОДТВЕРЖДЁН ЦЕПЬЮ"
    else:
        r["итог"] = "баланс получателя не изменился за отведённое время -- перевод НЕ подтверждён"
    return r


def test_ultra() -> dict:
    key = os.environ.get("RESCUE_WALLET_KEY", "").strip()
    if not key:
        return {"тест": "ultra",
                "итог": ("ОТЛОЖЕН: секрета RESCUE_WALLET_KEY ещё нет. Jupiter Ultra подписывает "
                          "транзакцию приватным ключом утилизатора -- без ключа проверить нечего. "
                          "Владелец добавляет секрет после этапов A и B, поэтому этот тест "
                          "выполняется отдельным прогоном уже на этапе C.")}
    return {"тест": "ultra",
            "итог": ("ключ найден, но сам обмен реализуется на этапе C вместе с подписью через "
                      "solders -- сюда он специально не вынесен, чтобы этап B оставался без ключей")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tests", default="sol",
                    help="через запятую: sol, token2022, ultra")
    ap.add_argument("--confirm", action="store_true",
                    help="реально отправить distribute (без него -- сухой прогон)")
    ap.add_argument("--t22-mint", default="PrekqLJvJ3qVdXmBGDiexvwUTF4rLFDa6HWS4HJbw9S",
                    help="минт Token-2022 с комиссией за перевод для теста token2022")
    ap.add_argument("--t22-share", type=float, default=0.25,
                    help="какую долю остатка переслать в тесте token2022")
    args = ap.parse_args()

    dbot_key, dbot_name = env("DBOT_API_KEY", "DBOT_APIKEY")
    hel_key, hel_name = env("HELIUS_API_KEY", "HELIUS_API")
    log(f"ключи: DBot из {dbot_name}, Helius из {hel_name}")
    log(f"режим: {'БОЕВОЙ (--confirm)' if args.confirm else 'СУХОЙ ПРОГОН'}")

    wallets = fetch_wallets(dbot_key)
    for w in wallets:
        _SECRETS.append(w["walletId"])
    log(f"белый список (живьём из follow_orders): {len(wallets)} кошельков -- "
        f"{', '.join(sorted(w['name'] for w in wallets))}")

    report: dict = {"начато_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "режим": "боевой" if args.confirm else "сухой прогон",
                    "белый_список": [{"имя": w["name"], "адрес": w["address"]} for w in wallets],
                    "тесты": []}

    wanted = [t.strip() for t in args.tests.split(",") if t.strip()]
    for t in wanted:
        log(f"--- тест {t} ---")
        if t == "sol":
            res = test_sol(wallets, dbot_key, hel_key, args.confirm)
        elif t == "token2022":
            res = test_token2022(wallets, dbot_key, hel_key, args.confirm,
                                 args.t22_mint, args.t22_share)
        elif t == "ultra":
            res = test_ultra()
        else:
            res = {"тест": t, "итог": "неизвестное имя теста"}
        log(f"итог: {res.get('итог')}")
        report["тесты"].append(res)
        if res.get("ответ") is not None and not RAW_SAMPLE.exists():
            RAW_SAMPLE.write_text(json.dumps(
                {"когда_utc": report["начато_utc"], "тест": t, "сырой_ответ": res["ответ"]},
                ensure_ascii=False, indent=2))
            log(f"сырая форма ответа distribute сохранена: {RAW_SAMPLE.relative_to(REPO)}")

    report["закончено_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print()
    print("=== ЭТАП B, РЕЗУЛЬТАТ ===")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(f"файл: {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
