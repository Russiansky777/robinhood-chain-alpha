#!/usr/bin/env python3
"""Meteora DLMM: вывести смещение bin_step в счёте пула -- по цепи, не по памяти.

ЗАЧЕМ. Строителю N4 нужна цена корзины. Цена корзины в сырых единицах равна
(1 + bin_step/10000)^bin -- это проверено на двух сделках одного пула
(docs/dlmm_sobytie_i_cena.md). Сам bin_step лежит в счёте пула (lbPair),
раскладка которого нам неизвестна. Брать смещение "по памяти" на денежном пути
нельзя, поэтому оно ВЫВОДИТСЯ: перебираются ВСЕ смещения u16 в счёте пула, и
смещение считается найденным, только если одно и то же место у ВСЕХ пулов даёт
цены корзин, между которыми лежит средняя цена сделки источника:

    цена(start_bin) <= (amount_in - fee) / amount_out <= цена(end_bin)

(покупка двигает цену вверх, поэтому средняя цена сделки обязана лежать между
ценой первой и последней корзины), а для сделки, не вышедшей из корзины, обе
границы совпадают и средняя цена обязана совпасть с ценой корзины.

ЭТО ТОТ ЖЕ ПРИЁМ, которым выведена раскладка amm_config у CLMM: не одно
совпадение, а совпадение сразу у всех независимых пулов.

ТОЛЬКО ЧТЕНИЕ: getTransaction по подписям сделок источников и
getMultipleAccounts по счетам пулов. Ни подписи, ни отправки, ни ордера.
Ключ Helius берётся из окружения службы (HELIUS_API_KEY), в отчёт не попадает.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal as D
from decimal import getcontext
from pathlib import Path

getcontext().prec = 60

DLMM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
CPI_ДИСК = bytes.fromhex("e445a52e51cb9a1d")
СВОП_ДИСК = bytes.fromhex("516ce3becdd00ac4")
ДЛИНА_СВОПА = 145
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
# Допуск на однокорзинной сделке. Цена корзины -- точная степень, а средняя цена
# считается по целым лампортам, поэтому ноль требовать нельзя. 1e-4 -- предел,
# который отсекает случайные совпадения: соседние корзины отличаются на
# bin_step/10000, то есть на проценты.
ДОПУСК_ОДНОЙ_КОРЗИНЫ = D("1e-4")
# Разумные пределы шага корзины: 1 (0.01 %) .. 2000 (20 %). Нулевой и огромный
# шаг -- это не шаг, а случайное число в счёте.
ШАГ_МИН, ШАГ_МАКС = 1, 2000


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s or "":
        n = n * 58 + B58.index(ch)
    b = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + b


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\x00"))) + s


def событие_свопа(данные: bytes) -> dict | None:
    """Событие свопа DLMM из данных внутренней инструкции. Раскладка -- из
    docs/dlmm_sobytie_i_cena.md, выведена по 12 живым шагам."""
    if (len(данные) != ДЛИНА_СВОПА or данные[:8] != CPI_ДИСК
            or данные[8:16] != СВОП_ДИСК):
        return None
    т = данные[16:]
    начало, конец = struct.unpack_from("<ii", т, 64)
    вошло, вышло = struct.unpack_from("<QQ", т, 72)
    комиссия, протокол = struct.unpack_from("<QQ", т, 89)
    return {"пул": b58encode(т[0:32]), "start_bin_id": начало,
             "end_bin_id": конец, "amount_in": вошло, "amount_out": вышло,
             "swap_for_y": т[88], "fee": комиссия, "protocol_fee": протокол}


def шаги_dlmm(tx: dict) -> list:
    """Шаги DLMM транзакции: (пул из счетов, переводы, событие)."""
    из_ = []
    for группа in ((tx or {}).get("meta") or {}).get("innerInstructions") or []:
        спис = группа.get("instructions") or []
        for н, их in enumerate(спис):
            if (их.get("programId") or их.get("program")) != DLMM:
                continue
            данные = b58decode(их.get("data") or "")
            if событие_свопа(данные) is not None:
                continue
            высота = их.get("stackHeight") or 2
            счета = их.get("accounts") or []
            переводы, событие = [], None
            for сл in спис[н + 1:]:
                if (сл.get("stackHeight") or 0) <= высота:
                    break
                разбор = сл.get("parsed") or {}
                инфо = разбор.get("info") or {}
                if разбор.get("type") in ("transfer", "transferChecked"):
                    сумма = инфо.get("amount") or (
                        инфо.get("tokenAmount") or {}).get("amount")
                    if сумма is not None:
                        переводы.append(int(сумма))
                elif (сл.get("programId") or сл.get("program")) == DLMM:
                    с_ = событие_свопа(b58decode(сл.get("data") or ""))
                    if с_ is not None:
                        событие = с_
            if событие is not None and len(переводы) >= 2:
                из_.append({"пул_из_счетов": счета[0] if счета else None,
                             "вошло": переводы[0], "вышло": переводы[-1],
                             "событие": событие})
    return из_


def цена(шаг_корзины: int, корзина: int) -> D:
    return (D(1) + D(шаг_корзины) / D(10000)) ** корзина


def подходит(шаг_корзины: int, ряд: dict) -> bool:
    """Ложится ли средняя цена сделки между ценами первой и последней корзины."""
    if not (ШАГ_МИН <= шаг_корзины <= ШАГ_МАКС):
        return False
    с = ряд["событие"]
    вошло_чистыми = с["amount_in"] - с["fee"]
    if вошло_чистыми <= 0 or с["amount_out"] <= 0:
        return False
    средняя = D(вошло_чистыми) / D(с["amount_out"])
    низ = цена(шаг_корзины, min(с["start_bin_id"], с["end_bin_id"]))
    верх = цена(шаг_корзины, max(с["start_bin_id"], с["end_bin_id"]))
    if с["start_bin_id"] == с["end_bin_id"]:
        return abs(средняя / низ - 1) <= ДОПУСК_ОДНОЙ_КОРЗИНЫ
    # Запас на одну корзину с каждой стороны: цена считается по целым
    # лампортам, а границы -- точные степени.
    низ = низ * (D(1) - D(шаг_корзины) / D(10000))
    верх = верх * (D(1) + D(шаг_корзины) / D(10000))
    return низ <= средняя <= верх


class Узел:
    def __init__(self, ключ: str) -> None:
        self.url = f"https://mainnet.helius-rpc.com/?api-key={ключ}"
        self.вызовов = 0

    def зов(self, метод: str, параметры: list):
        тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                            "params": параметры}).encode()
        зап = urllib.request.Request(
            self.url, data=тело, headers={"Content-Type": "application/json"})
        последняя = None
        for попытка in range(3):
            try:
                with urllib.request.urlopen(зап, timeout=30) as о:
                    self.вызовов += 1
                    return json.loads(о.read()).get("result")
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                последняя = exc
                time.sleep(1.0 + попытка)
        raise RuntimeError(f"узел не ответил: {type(последняя).__name__}")

    def транзакция(self, подпись: str):
        return self.зов("getTransaction", [подпись, {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1,
            "commitment": "confirmed"}])

    def счета(self, адреса: list):
        if not адреса:
            return None
        return self.зов("getMultipleAccounts", [адреса, {
            "encoding": "base64", "commitment": "confirmed"}])


def подписи_из_журнала(путь: Path, *, сколько: int, часов: float) -> list:
    """Подписи сделок источников по программе DLMM из журнала решений.

    Журнал читается ПОТОКОМ: он идёт на сотни тысяч строк в сутки.
    """
    порог = time.time() - часов * 3600.0
    из_ = []
    if not путь.exists():
        return из_
    with путь.open(encoding="utf-8", errors="replace") as ф:
        for строка in ф:
            строка = строка.strip()
            # В строке решения программа стоит ИМЕНЕМ (dex_programs), а не
            # адресом: фильтр ловит оба вида, иначе журнал молчит.
            if not строка.startswith("{") or (
                    DLMM not in строка and "Meteora DLMM" not in строка):
                continue
            try:
                з = json.loads(строка)
            except ValueError:
                continue
            т = з.get("t_recv_ts")
            if not isinstance(т, (int, float)) or float(т) < порог:
                continue
            подпись = з.get("signature")
            if подпись and подпись not in из_:
                из_.append(подпись)
    return из_[-сколько:]


def вывести(*, узел: Узел, подписи: list) -> dict:
    ряды = []
    пулы = []
    for подпись in подписи:
        tx = узел.транзакция(подпись)
        if not tx:
            continue
        for шаг in шаги_dlmm(tx):
            с = шаг["событие"]
            if с["пул"] != шаг["пул_из_счетов"]:
                continue                      # раскладка не подтвердилась фактом
            ряды.append(шаг)
            if с["пул"] not in пулы:
                пулы.append(с["пул"])
    из_ = {"подписей": len(подписи), "шагов": len(ряды), "пулов": len(пулы),
            "только_чтение": True}
    if not ряды:
        из_["why_not"] = "шагов DLMM с подтверждённым событием не нашлось"
        return из_
    счета = {}
    for н in range(0, len(пулы), 100):
        о = узел.счета(пулы[н:н + 100])
        for адрес, зап in zip(пулы[н:н + 100], (о or {}).get("value") or []):
            данные = ((зап or {}).get("data") or [None])[0]
            if данные:
                счета[адрес] = base64.b64decode(данные)
    из_["счетов_прочитано"] = len(счета)
    из_["длины_счетов"] = sorted({len(б) for б in счета.values()})
    # Перебор ВСЕХ смещений u16: смещение годно, если у КАЖДОГО шага цена
    # ложится между границами корзин.
    длина = min((len(б) for б in счета.values()), default=0)
    годные = []
    for сдвиг in range(0, max(0, длина - 1)):
        все = True
        значения = {}
        for шаг in ряды:
            б = счета.get(шаг["событие"]["пул"])
            if not б or сдвиг + 2 > len(б):
                все = False
                break
            (шк,) = struct.unpack_from("<H", б, сдвиг)
            значения[шаг["событие"]["пул"]] = шк
            if not подходит(шк, шаг):
                все = False
                break
        if все:
            годные.append({"смещение": сдвиг, "шаги_корзин": значения})
    из_["годные_смещения"] = годные
    из_["итог"] = ("смещение выведено" if len(годные) == 1 else
                    (f"годных смещений {len(годные)} -- одного нет, брать нельзя"
                     if годные else "ни одно смещение не подошло"))
    из_["однокорзинных_шагов"] = sum(
        1 for ш in ряды
        if ш["событие"]["start_bin_id"] == ш["событие"]["end_bin_id"])
    из_["вызовов_rpc"] = узел.вызовов
    return из_


def самопроверка() -> int:
    сбоев = всего = 0

    def chk(имя: str, ок: bool, что=None) -> None:
        nonlocal сбоев, всего
        всего += 1
        print(f"  [{'ok  ' if ок else 'СБОЙ'}] {имя}")
        if not ок:
            сбоев += 1
            if что is not None:
                print(f"         {что!r}")

    chk("цена корзины 0 равна единице", цена(100, 0) == 1)
    chk("цена корзины растёт с номером", цена(100, 1) > цена(100, 0) > цена(100, -1))
    # ПРОВЕРЕННЫЙ ПО ЦЕПИ СЛУЧАЙ (docs/dlmm_sobytie_i_cena.md): пул CJab6RE2,
    # корзина -346, шаг корзины 100, средняя цена сделки 0.031974566.
    ряд = {"событие": {"start_bin_id": -346, "end_bin_id": -346,
                        "amount_in": 3925145835, "fee": 79643894,
                        "amount_out": 120267522239}}
    chk(f"шаг 100 объясняет живую однокорзинную сделку "
        f"(цена корзины {float(цена(100, -346)):.9f})", подходит(100, ряд))
    chk("шаг 50 ту же сделку НЕ объясняет", not подходит(50, ряд))
    chk("шаг 250 ту же сделку НЕ объясняет", not подходит(250, ряд))
    chk("нулевой и огромный шаг отвергаются",
        not подходит(0, ряд) and not подходит(5000, ряд))
    многокорзинный = {"событие": {"start_bin_id": -363, "end_bin_id": -361,
                                   "amount_in": 67946052402, "fee": 2505510683,
                                   "amount_out": 2400885663492}}
    chk("многокорзинная сделка того же пула тоже ложится в границы при шаге 100",
        подходит(100, многокорзинный))
    chk("и не ложится при шаге 400", not подходит(400, многокорзинный))
    print(f"самопроверка вывода шага корзины: {всего - сбоев}/{всего} пройдено")
    return 1 if сбоев else 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--podpisi", dest="подписи", default="")
    р.add_argument("--zhurnal", dest="журнал", default="")
    р.add_argument("--chasov", dest="часов", default="24")
    р.add_argument("--skolko", dest="сколько", default="40")
    р.add_argument("--out", dest="куда", default="")
    р.add_argument("--self-test", action="store_true")
    а = р.parse_args()
    if а.self_test:
        return самопроверка()
    ключ = os.environ.get("HELIUS_API_KEY") or ""
    if not ключ:
        print("нет HELIUS_API_KEY в окружении")
        return 2
    подписи = [x.strip() for x in (а.подписи or "").split(",") if x.strip()]
    if not подписи and а.журнал:
        подписи = подписи_из_журнала(Path(а.журнал), сколько=int(а.сколько),
                                      часов=float(а.часов))
    if not подписи:
        print("нет подписей: задайте --podpisi или --zhurnal")
        return 2
    print(f"подписей к разбору: {len(подписи)}")
    итог = вывести(узел=Узел(ключ), подписи=подписи)
    краткий = {к: v for к, v in итог.items() if к != "годные_смещения"}
    print(json.dumps(краткий, ensure_ascii=False, indent=2))
    for г in (итог.get("годные_смещения") or [])[:5]:
        print(f"  смещение {г['смещение']}: шаги корзин "
              f"{list(г['шаги_корзин'].values())[:8]}")
    if а.куда:
        Path(а.куда).write_text(json.dumps(итог, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
