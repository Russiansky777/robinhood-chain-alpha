#!/usr/bin/env python3
"""Зонд покупки через Jupiter: SOL -> токен. Только чтение, ни одной подписи.

ЗАЧЕМ. Владелец 27.09 вне очереди: полоса должна уметь купить, когда котировка
пула не в SOL, -- через Jupiter, тем же клиентом и тёплым соединением, что у
сторожа на продаже, но с ПОДПИСЬЮ НАШИМ КЛЮЧОМ и ОТПРАВКОЙ НАШИМ ПУЛОМ. Значит
транзакцию нам нужно собирать самим, а от Jupiter брать котировку и инструкции.

Имена полей ответов выдумывать нельзя -- их надо взять с живого API. Из
контейнера Claude jup.ag закрыт прокси (CONNECT 403), поэтому зонд идёт с
NL-хоста. Здесь же замер, который просил владелец: время котировки по новому
и по уже открытому соединению.

Что спрашивается:
  1. /swap/v1/quote  SOL -> минт, swapMode=ExactIn -- какие поля отдаёт и как
     называется минимум выхода (otherAmountThreshold) при покупке;
  2. /swap/v1/swap-instructions -- отдаёт ли инструкции отдельно (тогда мы
     собираем транзакцию сами: наши чаевые, наш приоритет, наш blockhash) и
     какие поля у ответа, включая таблицы адресов;
  3. /swap/v1/swap -- запасной путь: готовая транзакция целиком.
Ключей в вывод не попадает: заголовок с ключом не печатается вовсе.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
БАЗЫ = ("https://lite-api.jup.ag/swap/v1", "https://api.jup.ag/swap/v1")


def заголовки(база: str) -> dict:
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    ключ = (os.environ.get("JUPITER_API_KEY") or "").strip()
    if ключ and "api.jup.ag" in база and "lite-api" not in база:
        h["x-api-key"] = ключ
    return h


def запрос(url: str, *, тело=None, заг=None, таймаут=25) -> dict:
    данные = None if тело is None else json.dumps(тело).encode()
    req = urllib.request.Request(url, data=данные, headers=(заг or {}),
                                 method=("POST" if данные else "GET"))
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=таймаут) as r:
            сырое = r.read()
            код = r.status
    except urllib.error.HTTPError as e:
        сырое, код = e.read(), e.code
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "почему": f"сеть: {type(exc).__name__}",
                "мс": round((time.perf_counter() - t0) * 1000, 2)}
    мс = round((time.perf_counter() - t0) * 1000, 2)
    try:
        тело_о = json.loads(сырое.decode("utf-8", "replace"))
    except ValueError:
        return {"ok": False, "http": код, "мс": мс,
                "почему": "не JSON", "начало": сырое[:200].decode("utf-8", "replace")}
    return {"ok": код == 200, "http": код, "мс": мс, "ответ": тело_о}


def поля(о, глубина=0) -> object:
    """Только имена полей и типы: тела маршрутов в отчёт не нужны."""
    if isinstance(о, dict):
        if глубина >= 2:
            return sorted(о.keys())
        return {k: поля(v, глубина + 1) for k, v in о.items()}
    if isinstance(о, list):
        return [поля(о[0], глубина + 1), f"...всего {len(о)}"] if о else []
    return type(о).__name__


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--mint", default=USDC, help="что покупаем за SOL")
    р.add_argument("--sol", type=float, default=0.01)
    р.add_argument("--taker", default=None, help="кошелёк полосы (только в запрос)")
    р.add_argument("--slippage-bps", type=int, default=3500)
    р.add_argument("--povtorov", type=int, default=7, help="замеров котировки")
    р.add_argument("--out", default=None)
    а = р.parse_args()

    лампорты = int(round(а.sol * 1e9))
    итог: dict = {"снято_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "минт": а.mint, "лампорты": лампорты,
                  "slippage_bps": а.slippage_bps,
                  "ключ_jupiter_в_окружении": bool((os.environ.get("JUPITER_API_KEY") or "").strip()),
                  "базы": {}}

    for база in БАЗЫ:
        з = заголовки(база)
        д: dict = {}
        # 1. Котировка SOL -> минт.
        парам = (f"?inputMint={WSOL}&outputMint={а.mint}&amount={лампорты}"
                 f"&slippageBps={а.slippage_bps}&swapMode=ExactIn")
        к = запрос(база + "/quote" + парам, заг=з)
        д["quote"] = {"ok": к.get("ok"), "http": к.get("http"), "мс": к.get("мс"),
                      "почему": к.get("почему")}
        if к.get("ok"):
            о = к["ответ"]
            д["quote"]["поля"] = поля(о)
            д["quote"]["значения"] = {
                k: о.get(k) for k in ("inAmount", "outAmount", "otherAmountThreshold",
                                      "swapMode", "slippageBps", "priceImpactPct")}
            д["quote"]["маршрут"] = [
                {"label": (rp.get("swapInfo") or {}).get("label"),
                 "feeAmount": (rp.get("swapInfo") or {}).get("feeAmount"),
                 "feeMint": (rp.get("swapInfo") or {}).get("feeMint"),
                 "percent": rp.get("percent")}
                for rp in (о.get("routePlan") or [])]
            д["quote"]["шагов_маршрута"] = len(о.get("routePlan") or [])
            # 2. Инструкции отдельно -- это путь, где транзакцию собираем МЫ.
            if а.taker:
                и = запрос(база + "/swap-instructions",
                           тело={"userPublicKey": а.taker, "quoteResponse": о,
                                 "wrapAndUnwrapSol": True,
                                 "useSharedAccounts": False,
                                 "dynamicComputeUnitLimit": False,
                                 "skipUserAccountsRpcCalls": False},
                           заг=з)
                д["swap_instructions"] = {"ok": и.get("ok"), "http": и.get("http"),
                                          "мс": и.get("мс"), "почему": и.get("почему")}
                if и.get("ok"):
                    ои = и["ответ"]
                    д["swap_instructions"]["поля"] = поля(ои)
                    д["swap_instructions"]["таблиц_адресов"] = len(
                        ои.get("addressLookupTableAddresses") or [])
                    д["swap_instructions"]["таблицы"] = (
                        ои.get("addressLookupTableAddresses") or [])
                    for имя in ("computeBudgetInstructions", "setupInstructions",
                                "swapInstruction", "cleanupInstruction",
                                "otherInstructions"):
                        зн = ои.get(имя)
                        д["swap_instructions"][f"{имя}_есть"] = зн is not None
                        if isinstance(зн, list):
                            д["swap_instructions"][f"{имя}_n"] = len(зн)
                            if зн:
                                д["swap_instructions"][f"{имя}_поля"] = sorted(зн[0].keys())
                        elif isinstance(зн, dict):
                            д["swap_instructions"][f"{имя}_поля"] = sorted(зн.keys())
                            д["swap_instructions"][f"{имя}_программа"] = зн.get("programId")
                            д["swap_instructions"][f"{имя}_счетов"] = len(зн.get("accounts") or [])
                else:
                    д["swap_instructions"]["начало_ответа"] = str(
                        (и.get("ответ") or и.get("начало")))[:300]
                # 3. Готовая транзакция -- запасной путь.
                с = запрос(база + "/swap",
                           тело={"userPublicKey": а.taker, "quoteResponse": о,
                                 "wrapAndUnwrapSol": True,
                                 "dynamicComputeUnitLimit": False},
                           заг=з)
                д["swap"] = {"ok": с.get("ok"), "http": с.get("http"), "мс": с.get("мс"),
                             "почему": с.get("почему")}
                if с.get("ok"):
                    д["swap"]["поля"] = sorted((с["ответ"] or {}).keys())
                    тх = (с["ответ"] or {}).get("swapTransaction") or ""
                    д["swap"]["байт_base64"] = len(тх)
            else:
                д["swap_instructions"] = {"почему": "--taker не задан, запрос не делался"}
        else:
            д["quote"]["начало_ответа"] = str((к.get("ответ") or к.get("начало")))[:300]

        # Замер: первый запрос платит DNS+TCP+TLS, дальше соединение тёплое.
        # urllib соединение не держит, поэтому тёплое мерим через requests.
        замеры: list = []
        try:
            import requests  # noqa: PLC0415
            с_ = requests.Session()
            for _ in range(max(1, а.povtorov)):
                t = time.perf_counter()
                try:
                    r = с_.get(база + "/quote", params={
                        "inputMint": WSOL, "outputMint": а.mint,
                        "amount": str(лампорты), "slippageBps": str(а.slippage_bps),
                        "swapMode": "ExactIn"}, headers=з, timeout=25)
                    код = r.status_code
                except Exception as exc:  # noqa: BLE001
                    замеры.append({"мс": None, "почему": type(exc).__name__})
                    continue
                замеры.append({"мс": round((time.perf_counter() - t) * 1000, 2),
                               "http": код})
        except Exception as exc:  # noqa: BLE001
            д["замер_почему"] = f"requests нет: {type(exc).__name__}"
        добрые = [x["мс"] for x in замеры if x.get("мс") is not None and x.get("http") == 200]
        д["замер_котировки"] = {
            "все_мс": замеры,
            "первый_мс": (добрые[0] if добрые else None),
            "тёплая_медиана_мс": (round(statistics.median(добрые[1:]), 2)
                                  if len(добрые) > 1 else None),
            "n_тёплых": max(0, len(добрые) - 1)}
        итог["базы"][база] = д

    print(json.dumps(итог, ensure_ascii=False, indent=1))
    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump(итог, ф, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
