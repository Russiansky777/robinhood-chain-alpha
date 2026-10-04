#!/usr/bin/env python3
"""Отказы 6042 кривой pump.fun ПО ЦЕПИ: уход цены против допуска.

ЗАЧЕМ (слово владельца 04.10, вечер). По каждому отказу 6042 нужно: слот
покупки источника и слот нашей транзакции, сколько чужих покупок по кривой было
между ними, на сколько процентов ушла цена от состояния, по которому мы считали
min_tokens_out, до состояния в нашем слоте, и какой допуск стоит у группы.

ПОЧЕМУ СРАВНЕНИЕ «УХОД ПРОТИВ ДОПУСКА» НЕ ТАВТОЛОГИЯ. Программа отказывает
ровно тогда, когда её выход меньше нашего min_out, и если считать уход ОТ НАШЕГО
ОЖИДАНИЯ, он всегда окажется больше допуска -- проверять нечего. Поэтому
ожидание пересчитывается ЗАНОВО по цепи: берётся событие сделки источника
(виртуальные резервы ПОСЛЕ неё -- ровно то состояние, по которому служба считает
минимум) и той же формулой сборщика (c2_swap_build.bonding_min_out) считается
выход на нашу трату. Дальше три числа рядом:
  * выход по состоянию A (после сделки источника) -- пересчитанный;
  * выход, который программа посчитала в нашем слоте -- из её же лога 6042
    ("Left: X, Right: Y": X -- её выход, Y -- наш минимум);
  * наш min_out из подписанных байтов.
Уход цены = 1 - X / выход_A. Допуск = 1 - min_out / ожидание_A. Если уход меньше
допуска, а отказ всё равно был, значит ожидание в бою считалось НЕ по состоянию
A -- это дефект расчёта min_tokens_out, и его видно числом.

ТОЛЬКО ЧТЕНИЕ: getTransaction и getSignaturesForAddress. Ключей кошельков здесь
нет, узел вычищается из любого текста наружу.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ОПЦИИ_TX = {"encoding": "json", "maxSupportedTransactionVersion": 1,
            "commitment": "confirmed"}
РАЗМЕР_ПАКЕТА = 25
# Сколько подписей минта читать назад, ища сделки между слотами.
ПОДПИСЕЙ_НА_МИНТ = 1000


def чисто(текст) -> str:
    т = str(текст)
    т = re.sub(r"https?://[^\s\"']+", "<узел вычищен>", т)
    т = re.sub(r"api[-_]?key=[A-Za-z0-9-]+", "<ключ вычищен>", т)
    return т


def узел() -> str:
    ключ = (os.environ.get("HELIUS_API_KEY") or os.environ.get("HELIUS_API")
            or "").strip()
    if not ключ:
        raise RuntimeError("ключа узла нет в окружении (HELIUS_API_KEY)")
    return f"https://mainnet.helius-rpc.com/?api-key={ключ}"


# ------------------------------------------------- чистые разборщики

# "Left:" и само число программа печатает ДВУМЯ строками лога подряд
# ("Program log: Left:" / "Program log: 2985264185981"), поэтому между словом и
# числом допускается всё, кроме цифр: так префикс "Program log: " пропускается,
# а чужое число не подхватывается.
ЧИСЛО_ЛЕВОЕ = re.compile(r"Left:\D{0,40}(\d+)")
ЧИСЛО_ПРАВОЕ = re.compile(r"Right:\D{0,40}(\d+)")


def левое_правое_6042(логи) -> dict:
    """Числа из лога отказа 6042: выход программы (Left) и наш минимум (Right).

    Программа печатает их двумя строками подряд
    ("Left: 2985264185981" / "Right: 3181160828596"), поэтому ищем пару в
    склеенном тексте логов. Нет пары -- так и говорим, а не подставляем ноль:
    ноль прочитался бы как «программа дала ноль токенов».
    """
    из_ = {"ok": False, "why_not": None, "vyhod_programmy": None,
           "nash_minimum": None}
    текст = "\n".join(str(л) for л in (логи or []))
    if "6042" not in текст and "SlippageBelow" not in текст:
        из_["why_not"] = "в логах нет ни кода 6042, ни имени ошибки"
    л, п = ЧИСЛО_ЛЕВОЕ.search(текст), ЧИСЛО_ПРАВОЕ.search(текст)
    if not л or not п:
        из_["why_not"] = (из_["why_not"]
                          or "в логах нет пары Left/Right с числами")
        return из_
    из_.update(ok=True, why_not=None, vyhod_programmy=int(л.group(1)),
               nash_minimum=int(п.group(1)))
    return из_


def уход_ceny(*, vyhod_A: int, vyhod_programmy: int) -> float | None:
    """Уход цены от состояния A до нашего слота, в процентах выхода.

    Положительное число -- выход стал МЕНЬШЕ (цена ушла вверх, токенов дают
    меньше). Ноль и минус возможны и означают, что цена не ушла или ушла в нашу
    пользу -- тогда отказ 6042 объяснить уходом нечем.
    """
    if not vyhod_A or vyhod_A <= 0 or vyhod_programmy is None:
        return None
    return round((1 - vyhod_programmy / vyhod_A) * 100, 3)


def сделок_между(события: list, *, slot_ot: int, slot_do: int,
                 индeks_istochnika=None, индeks_nash=None) -> dict:
    """Сколько ЧУЖИХ сделок по кривой прошло между сделкой источника и нашей.

    ОБА КРАЙНИХ СЛОТА ВХОДЯТ. Сделка в слоте источника, но ПОСЛЕ него, прошла
    между ними -- выкидывать её значило бы занижать счёт ровно там, где нас
    чаще всего и обгоняют: наша покупка уходит в тот же слот или следующий.
    Отсечь «до источника» и «после нас» можно только по месту в блоке, а место
    известно не всегда (getSignaturesForAddress его не отдаёт). Поэтому:
    известно -- отсекаем, не известно -- крайний слот считается ЦЕЛИКОМ, и это
    названо полем granicy_po_indeksu, чтобы число читали с этой поправкой.

    Наша собственная транзакция и транзакция источника не считаются никогда.
    """
    из_ = {"pokupok": 0, "prodazh": 0, "vsego_sdelok": 0, "v_nashem_slote": 0,
           "poslednee_sobytie": None,
           "granicy_po_indeksu": {"istochnik": индeks_istochnika is not None,
                                   "nash": индeks_nash is not None}}
    отобранные = []
    for с in события or []:
        сл = с.get("slot")
        if not isinstance(сл, int) or сл < slot_ot or сл > slot_do:
            continue
        if с.get("nash") or с.get("istochnik"):
            continue
        и_ = с.get("index") if isinstance(с.get("index"), int) else None
        if сл == slot_ot and индeks_istochnika is not None and и_ is not None \
                and и_ < индeks_istochnika:
            continue
        if сл == slot_do and индeks_nash is not None and и_ is not None \
                and и_ > индeks_nash:
            continue
        отобранные.append(с)
    отобранные.sort(key=lambda с: (с.get("slot") or 0,
                                   с.get("index") if isinstance(с.get("index"), int)
                                   else 10 ** 9))
    for с in отобранные:
        из_["vsego_sdelok"] += 1
        if с.get("is_buy"):
            из_["pokupok"] += 1
        else:
            из_["prodazh"] += 1
        if с.get("slot") == slot_do:
            из_["v_nashem_slote"] += 1
    if отобранные:
        п = отобранные[-1]
        из_["poslednee_sobytie"] = {
            "slot": п.get("slot"), "index": п.get("index"),
            "is_buy": п.get("is_buy"),
            "virtual_sol_reserves": п.get("virtual_sol_reserves"),
            "virtual_token_reserves": п.get("virtual_token_reserves")}
    return из_


def вердикт(строки: list, *, допуск_запас_pct: float = 0.0) -> dict:
    """Один вывод по всем разобранным случаям.

    Дефект, если ХОТЬ В ОДНОМ случае уход цены МЕНЬШЕ допуска: тогда отказ
    объяснить рынком нельзя. Неразобранные случаи в вердикт не идут и названы
    числом -- молчаливое «значит норма» здесь стоило бы разбора заново.
    """
    из_ = {"razobrano": 0, "ne_razobrano": 0, "uhod_menshe_dopuska": 0,
           "spornye": [], "verdikt": None}
    for с in строки or []:
        у, д = с.get("uhod_ceny_pct"), с.get("dopusk_pct")
        if у is None or д is None:
            из_["ne_razobrano"] += 1
            continue
        из_["razobrano"] += 1
        if float(у) < float(д) - float(допуск_запас_pct):
            из_["uhod_menshe_dopuska"] += 1
            из_["spornye"].append({"mint": с.get("mint"), "utc": с.get("utc"),
                                   "uhod_ceny_pct": у, "dopusk_pct": д})
    if из_["razobrano"] == 0:
        из_["verdikt"] = "нет ни одного разобранного случая -- вывода нет"
    elif из_["uhod_menshe_dopuska"] == 0:
        из_["verdikt"] = ("уход цены больше допуска во всех разобранных "
                          f"случаях ({из_['razobrano']}) -- норма")
    else:
        из_["verdikt"] = (f"уход цены МЕНЬШЕ допуска в {из_['uhod_menshe_dopuska']} "
                          f"из {из_['razobrano']} -- дефект расчёта min_tokens_out")
    return из_


# ------------------------------------------------- сеть

class Узел:
    def __init__(self) -> None:
        self.обращений = 0
        self.ошибок = 0
        self.кэш: dict = {}

    def _пост(self, тело):
        import requests  # noqa: PLC0415
        for попытка in range(5):
            self.обращений += 1
            от = requests.post(узел(), json=тело, timeout=60)
            if от.status_code == 429:
                time.sleep(1.5 * (попытка + 1))
                continue
            от.raise_for_status()
            return от.json()
        raise RuntimeError("узел отвечает 429 пять попыток подряд")

    def tx(self, подписи: list) -> dict:
        из_ = {}
        нужно = [п for п in подписи if п and п not in self.кэш]
        for п in подписи:
            if п in self.кэш:
                из_[п] = self.кэш[п]
        for и in range(0, len(нужно), РАЗМЕР_ПАКЕТА):
            кусок = нужно[и:и + РАЗМЕР_ПАКЕТА]
            тело = [{"jsonrpc": "2.0", "id": j, "method": "getTransaction",
                     "params": [п, ОПЦИИ_TX]} for j, п in enumerate(кусок)]
            ответ = self._пост(тело)
            по_id = {о.get("id"): о for о in
                     (ответ if isinstance(ответ, list) else [ответ])
                     if isinstance(о, dict)}
            for j, п in enumerate(кусок):
                о = по_id.get(j) or {}
                if "error" in о:
                    self.ошибок += 1
                    self.кэш[п] = None
                else:
                    self.кэш[п] = о.get("result")
                из_[п] = self.кэш[п]
            time.sleep(0.15)
        return из_

    def подписи_адреса(self, адрес: str, *, limit: int = ПОДПИСЕЙ_НА_МИНТ) -> list:
        из_ = []
        before = None
        while len(из_) < limit:
            п = {"limit": min(1000, limit - len(из_)), "commitment": "confirmed"}
            if before:
                п["before"] = before
            ответ = self._пост({"jsonrpc": "2.0", "id": 1,
                                "method": "getSignaturesForAddress",
                                "params": [адрес, п]})
            строки = (ответ or {}).get("result") or []
            if not строки:
                break
            из_ += строки
            before = строки[-1].get("signature")
            time.sleep(0.15)
            if len(строки) < п["limit"]:
                break
        return из_


def _самопроверка() -> int:
    проверок = прошло = 0

    def сверить(что, дано, ждали):
        nonlocal проверок, прошло
        проверок += 1
        if дано == ждали:
            прошло += 1
        else:
            print(f"НЕ ПРОШЛО: {что}: дано {дано!r}, ждали {ждали!r}")

    # --- числа из лога 6042: строка ДОСЛОВНО той формы, что печатает программа
    логи = [
        "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]",
        "Program log: Instruction: Buy",
        "Program log: AnchorError occurred. Error Code: BuySlippageBelowMinTokensOut."
        " Error Number: 6042. Error Message: slippage: Too much SOL required to buy"
        " the given amount of tokens..",
        "Program log: Left:",
        "Program log: 2985264185981",
        "Program log: Right:",
        "Program log: 3181160828596",
        "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P failed: custom program"
        " error: 0x179a",
    ]
    л = левое_правое_6042(логи)
    сверить("выход программы и наш минимум прочитаны из логов",
            (л["ok"], л["vyhod_programmy"], л["nash_minimum"]),
            (True, 2985264185981, 3181160828596))
    сверить("без пары Left/Right -- ok=False и причина названа",
            (lambda р: (р["ok"], bool(р["why_not"])))(
                левое_правое_6042(["Program log: Instruction: Buy"])),
            (False, True))
    сверить("пустые логи -- не падение, а причина",
            левое_правое_6042([])["ok"], False)
    сверить("ноль вместо чисел не подставляется",
            левое_правое_6042([])["vyhod_programmy"], None)

    # --- уход цены
    сверить("уход цены: выход упал на 40 %",
            уход_ceny(vyhod_A=1000, vyhod_programmy=600), 40.0)
    сверить("цена не ушла -- ноль",
            уход_ceny(vyhod_A=1000, vyhod_programmy=1000), 0.0)
    сверить("цена ушла в нашу пользу -- минус, а не ноль",
            уход_ceny(vyhod_A=1000, vyhod_programmy=1100), -10.0)
    сверить("без выхода A ухода нет (не ноль)",
            уход_ceny(vyhod_A=0, vyhod_programmy=600), None)

    # --- сделки между слотами
    соб = [
        {"slot": 100, "index": 5, "is_buy": True, "istochnik": True},
        {"slot": 100, "index": 9, "is_buy": True},       # после источника, тот же слот
        {"slot": 101, "index": 2, "is_buy": True},
        {"slot": 101, "index": 7, "is_buy": False,
         "virtual_sol_reserves": 11, "virtual_token_reserves": 22},
        {"slot": 101, "index": 8, "is_buy": True, "nash": True},
        {"slot": 101, "index": 9, "is_buy": True},       # ПОСЛЕ нас -- не считается
        {"slot": 102, "index": 1, "is_buy": True},       # после нашего слота
    ]
    м = сделок_между(соб, slot_ot=100, slot_do=101, индeks_istochnika=5,
                     индeks_nash=8)
    сверить("между слотами: покупок, продаж, всего",
            (м["pokupok"], м["prodazh"], м["vsego_sdelok"]), (2, 1, 3))
    сверить("в нашем слоте считаются только те, что раньше нас",
            м["v_nashem_slote"], 2)
    сверить("в слоте источника считаются только те, что ПОСЛЕ него",
            сделок_между([{"slot": 100, "index": 1, "is_buy": True},
                          {"slot": 100, "index": 9, "is_buy": True}],
                         slot_ot=100, slot_do=100,
                         индeks_istochnika=5)["vsego_sdelok"], 1)
    сверить("границы названы: по индексу или целым слотом",
            м["granicy_po_indeksu"], {"istochnik": True, "nash": True})
    сверить("последнее событие перед нами -- его резервы",
            (м["poslednee_sobytie"]["slot"], м["poslednee_sobytie"]["index"],
             м["poslednee_sobytie"]["virtual_sol_reserves"]), (101, 7, 11))
    сверить("наша и исходная сделки в счёт не идут",
            сделок_между([{"slot": 101, "index": 1, "is_buy": True, "nash": True}],
                         slot_ot=100, slot_do=101)["vsego_sdelok"], 0)
    сверить("без места в блоке весь наш слот считается целиком",
            сделок_между(соб, slot_ot=100, slot_do=101,
                         индeks_nash=None)["vsego_sdelok"], 4)
    сверить("пустой список -- нули и нет последнего события",
            (сделок_между([], slot_ot=1, slot_do=2)["vsego_sdelok"],
             сделок_между([], slot_ot=1, slot_do=2)["poslednee_sobytie"]),
            (0, None))

    # --- вердикт
    в1 = вердикт([{"uhod_ceny_pct": 40.0, "dopusk_pct": 35.0},
                  {"uhod_ceny_pct": 36.0, "dopusk_pct": 35.0}])
    сверить("все уходы больше допуска -- норма",
            (в1["uhod_menshe_dopuska"], "норма" in в1["verdikt"]), (0, True))
    в2 = вердикт([{"uhod_ceny_pct": 40.0, "dopusk_pct": 35.0},
                  {"uhod_ceny_pct": 12.0, "dopusk_pct": 35.0, "mint": "M"}])
    сверить("хоть один уход меньше допуска -- дефект расчёта",
            (в2["uhod_menshe_dopuska"], "дефект" in в2["verdikt"]), (1, True))
    сверить("спорный случай назван минтом", в2["spornye"][0]["mint"], "M")
    в3 = вердикт([{"uhod_ceny_pct": None, "dopusk_pct": 35.0}])
    сверить("неразобранный случай в норму НЕ записывается",
            (в3["razobrano"], в3["ne_razobrano"], "вывода нет" in в3["verdikt"]),
            (0, 1, True))
    сверить("равный допуску уход -- не дефект (строгое меньше)",
            вердикт([{"uhod_ceny_pct": 35.0,
                      "dopusk_pct": 35.0}])["uhod_menshe_dopuska"], 0)

    # --- вычистка узла из текста
    сверить("адрес узла и ключ не уходят наружу",
            чисто("ошибка на https://mainnet.helius-rpc.com/?api-key=СЕКРЕТ"),
            "ошибка на <узел вычищен>")
    print(f"проверок {проверок}, прошло {прошло}, "
          f"не прошло {проверок - прошло}")
    return 0 if прошло == проверок else 1


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--vyzhimka", required=True)
    р.add_argument("--out", default="data/otkazy_6042_razbor.json")
    р.add_argument("--sluchaev", type=int, default=7)
    а = р.parse_args()

    выж = json.loads(Path(а.vyzhimka).read_text(encoding="utf-8"))
    случаи = (выж.get("случаи") or [])[:max(1, а.sluchaev)]
    try:
        import c2_swap_build as B  # noqa: PLC0415
    except Exception as сбой:  # noqa: BLE001
        print(f"СТОП: сборщик не загрузился: {type(сбой).__name__}: {сбой}",
              file=sys.stderr)
        return 3

    у = Узел()
    строки = []
    for с in случаи:
        стр = {к: с.get(к) for к in
               ("utc", "ts_intent_utc", "mint", "token_name", "lane_group",
                "source", "source_sig", "source_slot", "lane_landed_signature",
                "lane_landed_slot", "lane_expected_out", "lane_min_out",
                "sol_in", "our_block_index", "our_block_total",
                "source_block_index", "dopusk_iz_bajtov_pct", "на_кривой")}
        стр["why_not"] = None
        наша = с.get("lane_landed_signature") or c_наша(с)
        txs = у.tx([п for п in (наша, с.get("source_sig")) if п])
        tx_наша, tx_ист = txs.get(наша), txs.get(с.get("source_sig"))
        # --- числа из лога программы
        логи = (((tx_наша or {}).get("meta") or {}).get("logMessages")) or []
        лп = левое_правое_6042(логи)
        стр["vyhod_programmy"] = лп.get("vyhod_programmy")
        стр["minimum_v_bajtah"] = лп.get("nash_minimum")
        стр["log_why_not"] = лп.get("why_not")
        стр["nash_slot_po_cepi"] = (tx_наша or {}).get("slot")
        стр["slot_istochnika_po_cepi"] = (tx_ист or {}).get("slot")
        # --- пересчёт ожидания по состоянию A (после сделки источника)
        трата = с.get("sol_in")
        лампорты = int(round(float(трата) * 1_000_000_000)) if трата else None
        допуск = None
        if isinstance(с.get("lane_expected_out"), int) \
                and isinstance(с.get("lane_min_out"), int) \
                and с["lane_expected_out"] > 0:
            допуск = (1 - с["lane_min_out"] / с["lane_expected_out"])
        if tx_ист is None:
            стр["why_not"] = "сделка источника по цепи не прочитана"
        elif not лампорты:
            стр["why_not"] = "в записи нет траты sol_in -- ожидание не пересчитать"
        else:
            пересчёт = B.bonding_min_out(tx_ист, с.get("mint"), лампорты,
                                         допуск if допуск is not None else 0.0)
            стр["pereschet"] = {к: пересчёт.get(к) for к in
                                ("ok", "why_not", "expected_out", "min_out",
                                 "sol_to_curve", "virtual_reserves_after",
                                 "fee_bps", "creator_fee_bps")}
            if пересчёт.get("ok"):
                стр["vyhod_A"] = пересчёт.get("expected_out")
                оA = пересчёт.get("expected_out")
                ож = с.get("lane_expected_out")
                if оA and isinstance(ож, int):
                    стр["ozhidanie_v_boju_k_A_pct"] = round((ож / оA - 1) * 100, 3)
            else:
                стр["why_not"] = (пересчёт.get("why_not")
                                  or "событие сделки источника не разобрано")
        стр["dopusk_pct"] = (round(допуск * 100, 3) if допуск is not None else None)
        стр["uhod_ceny_pct"] = уход_ceny(
            vyhod_A=стр.get("vyhod_A") or 0,
            vyhod_programmy=стр.get("vyhod_programmy"))
        # --- чужие сделки между слотами
        сл_ист = стр.get("slot_istochnika_po_cepi") or с.get("source_slot")
        сл_наш = стр.get("nash_slot_po_cepi") or с.get("lane_landed_slot")
        if с.get("mint") and isinstance(сл_ист, int) and isinstance(сл_наш, int):
            try:
                подписи = у.подписи_адреса(с["mint"])
            except Exception as сбой:  # noqa: BLE001
                подписи = []
                стр["mezhdu_why_not"] = чисто(f"{type(сбой).__name__}: {сбой}")
            в_окне = [з for з in подписи
                      if isinstance(з.get("slot"), int)
                      and сл_ист <= з["slot"] <= сл_наш]
            стр["podpisej_minta_prochteno"] = len(подписи)
            стр["podpisej_v_okne"] = len(в_окне)
            события = []
            if в_окне:
                tx2 = у.tx([з.get("signature") for з in в_окне])
                for з in в_окне:
                    t = tx2.get(з.get("signature"))
                    if t is None:
                        continue
                    ev = B.pump_trade_event(t, с.get("mint"))
                    события.append({
                        "slot": t.get("slot"),
                        "index": None,
                        "is_buy": bool(ev) if ev else None,
                        "nash": з.get("signature") == наша,
                        "istochnik": з.get("signature") == с.get("source_sig"),
                        "virtual_sol_reserves": (ev or {}).get("virtual_sol_reserves"),
                        "virtual_token_reserves": (ev or {}).get("virtual_token_reserves"),
                    })
            стр["mezhdu"] = сделок_между(события, slot_ot=сл_ист, slot_do=сл_наш)
        строки.append(стр)

    итог = {"snjato_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "svod": {к: выж.get(к) for к in
                     ("since_utc", "snjato_utc", "позиций_просмотрено",
                      "попыток_полосы", "попыток_кривой", "отказов_6042",
                      "по_группам", "по_источникам",
                      "dolya_6042_ot_popytok_krivoj_pct")},
            "sluchaev_razobrano": len(строки), "stroki": строки,
            "uzel": {"obrashcheniy": у.обращений, "oshibok": у.ошибок}}
    итог["vyvod"] = вердикт(строки)
    Path(а.out).parent.mkdir(parents=True, exist_ok=True)
    Path(а.out).write_text(json.dumps(итог, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    print(json.dumps(итог["vyvod"], ensure_ascii=False))
    return 0


def c_наша(с: dict):
    """Подпись нашей транзакции: севшая, иначе отправленная, иначе локальная."""
    for поле in ("lane_landed_signature", "lane_signature",
                 "lane_signature_local"):
        if с.get(поле):
            return с[поле]
    return None


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_самопроверка())
    sys.exit(main())
