#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Записать итог проданной вне службы позиции в СУТКИ ЕЁ ПРОДАЖИ.

ЗАЧЕМ (слово владельца 03.10: "BTN -0.061544696 и Fantasy -0.051704209 --
книжить по цепи в те сутки, где была продажа").

Две позиции 01.10 служба закрыла причиной "продавать нечего: остаток минта 0".
Продажа БЫЛА -- нашим ключом, разовым прогоном через Jupiter -- и её подпись и
итог дописаны в запись шагом zakryt_fantom --dopisat (поля prodano_vne_sluzhby
и vne_sluzhby_sol_net). Чего в записи нет -- денег в СУТОЧНОМ СЧЁТЕ: closed_sol_net
не выставлен, pnl_counted не стоит, и итога этих двух сделок нет ни в одних сутках.

ПОЧЕМУ ОТДЕЛЬНЫЙ МОДУЛЬ, А НЕ ПРОСТАЯ ЗАПИСЬ closed_sol_net. ExecState.update_position
зовёт _учесть_закрытие, а тот -- add_pnl БЕЗ ts, и pnl_path(None) берёт файл
СЕГОДНЯШНИХ суток. Запись closed_sol_net занесла бы -0.0615 и -0.0517 в счёт 03.10,
а не в 01.10 и 02.10, и заодно съела бы часть сегодняшнего плюса. Поэтому здесь
ровно два шага и в таком порядке:

  1. ЗАПИСЬ БЕЗ ПОЛЯ state -- тогда _учесть_закрытие возвращает {} на первой же
     строке и в сегодняшние сутки не попадает ничего;
  2. add_pnl(..., ts=<время продажи>) -- деньги ложатся в суточный файл того дня,
     когда продажа состоялась, по мадридской границе (day_key).

ЧИСЛО НЕ ВЫДУМЫВАЕТСЯ. Оно сверяется по самой записи:
    (натив покупки - комиссия покупки) + итог продажи + запертая рента
и должно сойтись с названным до 1e-9. Не сошлось -- отказ словами и ни одной записи.

ДВЕ УСЛОВНОСТИ, И ОБЕ НАЗВАНЫ. Каноническая формула репозитория
(bloom_exec_state.итог_позиции) считает ЛИКВИДНЫЙ итог -- без возврата ренты:
её деньги заперты в токен-счёте, пока счёт не закрыт. Владелец назвал числа
С рентой (как в таблице полосы, поле итог_sol). Модуль пишет НАЗВАННОЕ число и
кладёт рядом оба: knizhit_itog_sol и knizhit_itog_likvidnyj_sol, чтобы разница
0.00151384 на сделку нигде не была невидимой.

ЗАПИСЬ ЧИТАЕТСЯ ПО ВСЕЙ ИСТОРИИ, А НЕ ЧЕРЕЗ positions(). Найдено сухим прогоном
03.10: ExecState.positions() нарочно читает текущий журнал и РОВНО ОДИН самый
свежий ротированный файл -- его зовут гейты перед каждой сделкой, и тянуть всю
историю туда нельзя. Покупка BTN от 01.10 20:34Z лежит двумя ротациями раньше,
поэтому lane_buy_native_sol через positions() не виден вовсе, и сверка числа
отказала словами ("в записи нет lane_buy_native_sol"). Здесь история читается
целиком и ровно по ОДНОМУ cid: это не горячий путь, а разовое книжение. Охраны от
этого только крепче -- closed_sol_net и pnl_counted ищутся во ВСЕХ строках, а не
в последних.

ОХРАНЫ -- каждая отказывает словами, без записи:
  * позиция существует и ЗАКРЫТА;
  * prodano_vne_sluzhby стоит и vne_sluzhby_sol_net -- число (дописка сделана);
  * closed_sol_net ЕЩЁ НЕ выставлен (иначе сутки считает обычный учёт);
  * pnl_counted не стоит и knizhit_zadnim_chislom не стоит (задвоение хуже
    отсутствия: счёт с выдуманным убытком -- это ложный стоп);
  * сутки, посчитанные из времени продажи, совпадают с названными;
  * названные сутки -- НЕ сегодняшние (сегодняшние считает служба сама).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

ДОПУСК = 1e-9
ПОЛЕ_ОТМЕТКИ = "knizhit_zadnim_chislom"


def разбор_времени(что: str) -> float | None:
    """Время продажи: epoch-секунды или ISO-8601 с Z."""
    т = (что or "").strip()
    if not т:
        return None
    try:
        return float(т)
    except ValueError:
        pass
    import datetime as dt  # noqa: PLC0415
    try:
        ч = dt.datetime.strptime(т.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None
    return ч.timestamp()


def сверка_числа(поз: dict) -> dict:
    """Итог по частям самой записи. Своей арифметики сверх названных слагаемых нет."""
    из_: dict = {"ok": False, "why_not": None}
    натив = поз.get("lane_buy_native_sol")
    комиссия = поз.get("lane_buy_fee_sol")
    рента = поз.get("lane_buy_rent_sol")
    продажа = поз.get("vne_sluzhby_sol_net")
    for имя, з in (("lane_buy_native_sol", натив), ("vne_sluzhby_sol_net", продажа)):
        if з is None:
            из_["why_not"] = f"в записи нет {имя} -- итог по цепи не собрать"
            return из_
    покупка = round(float(натив) - float(комиссия or 0.0), 9)
    ликвидный = round(покупка + float(продажа), 9)
    с_рентой = round(ликвидный + float(рента or 0.0), 9)
    из_.update(ok=True, покупка_по_цепи=покупка, продажа_по_цепи=round(float(продажа), 9),
               рента=round(float(рента or 0.0), 9),
               итог_ликвидный=ликвидный, итог_с_рентой=с_рентой)
    return из_


def книжить(*, состояние, cid: str, сутки: str, ts_продажи: float,
            итог_sol: float, живьём: bool = False,
            сегодня_ключ=None) -> dict:
    из_: dict = {"ok": False, "why_not": None, "cid": cid, "живьём": живьём,
                 "сутки": сутки, "режим": "книжение задним числом"}
    if hasattr(состояние, "позиция_по_всей_истории"):
        поз = состояние.позиция_по_всей_истории(cid) or {}
        из_["откуда_запись"] = "вся история журнала"
    else:
        поз = (состояние.positions() or {}).get(cid) or {}
        из_["откуда_запись"] = "positions()"
    из_["прочитано"] = поз.get("_прочитано")
    if not поз:
        из_["why_not"] = f"позиции {cid} в журнале нет"
        return из_
    из_["состояние"] = поз.get("state")
    если = str(поз.get("state") or "")
    if если != "closed":
        из_["why_not"] = (f"позиция не закрыта (state={если or 'нет'}) -- "
                          "книжить можно только закрытую")
        return из_
    if not поз.get("prodano_vne_sluzhby"):
        из_["why_not"] = ("в записи нет prodano_vne_sluzhby -- сначала дописка "
                          "итога по цепи (zakryt_fantom --dopisat), потом книжение")
        return из_
    if поз.get("closed_sol_net") is not None:
        из_["why_not"] = (f"closed_sol_net уже стоит ({поз['closed_sol_net']}) -- "
                          "сутки такой позиции считает обычный учёт, не этот модуль")
        return из_
    if поз.get("pnl_counted"):
        из_["why_not"] = "pnl_counted уже стоит -- деньги этой позиции уже в счёте"
        return из_
    if поз.get(ПОЛЕ_ОТМЕТКИ):
        из_["why_not"] = (f"{ПОЛЕ_ОТМЕТКИ} уже стоит ({поз[ПОЛЕ_ОТМЕТКИ]}) -- "
                          "эта позиция уже книжена задним числом")
        return из_
    св = сверка_числа(поз)
    из_["сверка"] = св
    if not св.get("ok"):
        из_["why_not"] = св.get("why_not")
        return из_
    if abs(float(св["итог_с_рентой"]) - float(итог_sol)) > ДОПУСК:
        из_["why_not"] = (f"названный итог {итог_sol} не сходится с записью: "
                          f"покупка {св['покупка_по_цепи']} + продажа {св['продажа_по_цепи']}"
                          f" + рента {св['рента']} = {св['итог_с_рентой']}")
        return из_
    день_продажи, беда = состояние.day_key_для(ts_продажи)
    из_.update(сутки_из_времени=день_продажи, часовой_пояс_недоступен=беда)
    if беда:
        из_["why_not"] = ("часовой пояс Мадрида недоступен -- границу суток не "
                          "определить, а книжить не в те сутки хуже, чем не книжить")
        return из_
    if день_продажи != сутки:
        из_["why_not"] = (f"время продажи даёт сутки {день_продажи}, а названы {сутки} -- "
                          "книжить не в те сутки нельзя")
        return из_
    сегодня = (сегодня_ключ if сегодня_ключ is not None
               else состояние.day_key_для(time.time())[0])
    из_["сегодня"] = сегодня
    if сутки == сегодня:
        из_["why_not"] = ("названы СЕГОДНЯШНИЕ сутки -- их считает служба сама, "
                          "задним числом тут писать нечего")
        return из_
    # РАСХОД ПО КАНОНИЧЕСКОЙ ФОРМУЛЕ ЗДЕСЬ ВЫХОДИТ ОТРИЦАТЕЛЬНЫМ, и это не
    # ошибка формулы, а случай, на который она не рассчитана. Формула
    # (bloom_exec_state.итог_позиции) считает расход как "натив минус вход":
    # сколько ушло с кошелька СВЕРХ входа -- чаевые, приоритет, тариф, рента.
    # Она верна, когда билет потрачен целиком: у обычной сделки 01.10 натив
    # -0.10161384 при входе 0.1 даёт расход +0.00261884. У этих двух билет
    # потрачен НЕ целиком (кривая взяла меньше: натив -0.0758 и -0.0656 при
    # билете 0.1), и формула даёт -0.0232 и -0.0334.
    # ОТРИЦАТЕЛЬНЫЙ РАСХОД В СУТОЧНЫЙ СЧЁТ НЕ ИДЁТ: spent_sol там копится, и
    # минус молча УМЕНЬШИЛ бы уже доложенный расход суток. В счёт идёт 0.0, а
    # оба настоящих числа ложатся в саму запись под своими именами.
    расход_формулы = round(-float(поз.get("lane_buy_native_sol") or 0.0)
                           - float(поз.get("sol_in") or 0.0)
                           + float(поз.get("lane_buy_fee_sol") or 0.0), 9)
    отток_покупки = round(-float(поз.get("lane_buy_native_sol") or 0.0), 9)
    расход = расход_формулы if расход_формулы >= 0 else 0.0
    из_["расход_в_счёт"] = расход
    из_["расход_по_формуле"] = расход_формулы
    из_["отток_покупки"] = отток_покупки
    поля = {
        # ПОЛЯ state ЗДЕСЬ НЕТ И БЫТЬ НЕ ДОЛЖНО -- см. docstring.
        "closed_sol_net": св["продажа_по_цепи"],
        "pnl_counted": True,
        "pnl_counted_sol": round(float(итог_sol), 9),
        "pnl_counted_spend_sol": расход,
        "knizhit_rashod_po_formule_sol": расход_формулы,
        "knizhit_ottok_pokupki_sol": отток_покупки,
        ПОЛЕ_ОТМЕТКИ: сутки,
        "knizhit_itog_sol": round(float(итог_sol), 9),
        "knizhit_itog_likvidnyj_sol": св["итог_ликвидный"],
        "knizhit_renta_sol": св["рента"],
        "knizhit_ts_prodazhi": float(ts_продажи),
        "knizhit_pochemu": ("книжено задним числом по слову владельца 03.10: итог по "
                            f"цепи в сутки продажи {сутки}; число с рентой, "
                            "ликвидный итог рядом"
                            + ("; расход по формуле отрицателен (билет потрачен "
                               "не целиком) -- в суточный счёт пошёл 0.0, оба "
                               "настоящих числа в полях knizhit_*"
                               if расход_формулы < 0 else "")),
    }
    из_["запись"] = поля
    из_["счёт_до"] = состояние.pnl(ts_продажи)
    if not живьём:
        из_["why_not"] = "сухой прогон: записи нет (живьём -- KNIZHIT_LIVE=1)"
        return из_
    строка = состояние.update_position(cid, **поля)
    # ЗАПИСЬ БЕЗ state НЕ ДОЛЖНА БЫЛА НИЧЕГО УЧЕСТЬ. Проверяем это по самой
    # вернувшейся строке, а не на слово: если там появился pnl_counted от
    # _учесть_закрытие, значит деньги уже легли в СЕГОДНЯШНИЕ сутки.
    из_["строка_журнала"] = {к: строка.get(к) for к in
                             ("client_order_id", "state", "closed_sol_net",
                              "pnl_counted", "pnl_counted_sol", ПОЛЕ_ОТМЕТКИ,
                              "ts_update_utc")}
    if строка.get("state"):
        из_["why_not"] = ("в записи оказался state -- учёт мог сработать в "
                          "сегодняшние сутки; add_pnl НЕ зван")
        return из_
    из_["счёт_после_записи"] = состояние.pnl(ts_продажи)
    состояние.add_pnl(realized_sol=float(итог_sol), spent_sol=расход, sells=1,
                      ts=float(ts_продажи))
    из_["счёт_после"] = состояние.pnl(ts_продажи)
    из_["счёт_сегодня"] = состояние.pnl(time.time())
    из_.update(ok=True, why_not=None)
    return из_


# ------------------------------------------------------------------ самопроверка

class _Состояние:
    """Поддельное состояние: журнал в памяти, суточные счёты в словаре."""

    def __init__(self, позиции):
        self._поз = позиции
        self.счёты = {}
        self.звали_add_pnl = []

    def positions(self):
        """Как в бою: свежие строки БЕЗ полей покупки (они в старой ротации)."""
        усечённое = {}
        for cid, п in self._поз.items():
            усечённое[cid] = {к: з for к, з in п.items()
                              if к not in ("lane_buy_native_sol", "lane_buy_fee_sol",
                                           "lane_buy_rent_sol")}
        return усечённое

    def позиция_по_всей_истории(self, cid):
        return dict(self._поз.get(cid) or {})

    def day_key_для(self, ts):
        import datetime as dt
        return dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d"), False

    def pnl(self, ts=None):
        return dict(self.счёты.get(self.day_key_для(ts or 0)[0],
                                   {"realized_sol": 0.0, "spent_sol": 0.0, "sells": 0}))

    def add_pnl(self, *, realized_sol=0.0, spent_sol=0.0, buys=0, sells=0, ts=None):
        self.звали_add_pnl.append({"realized_sol": realized_sol, "ts": ts})
        д = self.day_key_для(ts or 0)[0]
        т = self.счёты.setdefault(д, {"realized_sol": 0.0, "spent_sol": 0.0, "sells": 0})
        т["realized_sol"] = round(т["realized_sol"] + realized_sol, 9)
        т["spent_sol"] = round(т["spent_sol"] + spent_sol, 9)
        т["sells"] += sells
        return т

    def update_position(self, cid, **поля):
        # Та же ловушка, что в бою: state=closed -> учёт в СЕГОДНЯШНИЕ сутки.
        строка = {"client_order_id": cid, **поля}
        if str(поля.get("state") or "") == "closed":
            self.add_pnl(realized_sol=-99.0, sells=1, ts=None)
            строка["pnl_counted"] = True
        self._поз.setdefault(cid, {}).update(поля)
        return строка


def self_test() -> int:
    сбоев = []

    def chk(имя, усл, что=""):
        if усл:
            print(f"  [ok  ] {имя}")
        else:
            сбоев.append(имя)
            print(f"  [СБОЙ] {имя} {что}")

    ПРОДАЖА_TS = 1790891913.0           # 01.10 21:58:33Z
    ОСНОВА = {
        "state": "closed", "sol_in": 0.1,
        "lane_buy_native_sol": -0.075775614, "lane_buy_fee_sol": 0.001005,
        "lane_buy_rent_sol": 0.00151384,
        "prodano_vne_sluzhby": True, "vne_sluzhby_sol_net": 0.013722078,
    }
    ИТОГ = -0.061544696

    def зов(правки=None, **кв):
        поз = dict(ОСНОВА); поз.update(правки or {})
        с = _Состояние({"CID": поз})
        арг = {"состояние": с, "cid": "CID", "сутки": "2026-10-01",
               "ts_продажи": ПРОДАЖА_TS, "итог_sol": ИТОГ,
               "живьём": True, "сегодня_ключ": "2026-10-03"}
        арг.update(кв)
        return книжить(**арг), с

    print("-- сверка числа по частям записи")
    св = сверка_числа(ОСНОВА)
    chk("итог с рентой = названный владельцем",
        abs(св["итог_с_рентой"] - ИТОГ) < ДОПУСК, св)
    chk("ликвидный итог ровно на ренту меньше",
        abs(св["итог_ликвидный"] - (ИТОГ - 0.00151384)) < ДОПУСК, св)

    print("-- живая запись")
    р, с = зов()
    chk("книжение прошло", р.get("ok"), р.get("why_not"))
    chk("add_pnl зван РОВНО один раз", len(с.звали_add_pnl) == 1, с.звали_add_pnl)
    chk("add_pnl зван со временем продажи",
        с.звали_add_pnl and с.звали_add_pnl[0]["ts"] == ПРОДАЖА_TS, с.звали_add_pnl)
    chk("деньги легли в сутки 2026-10-01",
        abs(с.счёты.get("2026-10-01", {}).get("realized_sol", 0) - ИТОГ) < ДОПУСК, с.счёты)
    chk("в сегодняшние сутки не легло ничего",
        "2026-10-03" not in с.счёты and "1970-01-01" not in с.счёты, с.счёты)
    chk("поля state в записи НЕТ", "state" not in (р.get("запись") or {}), р.get("запись"))
    chk("ликвидный итог лежит рядом с названным",
        (р["запись"]["knizhit_itog_likvidnyj_sol"]
         != р["запись"]["knizhit_itog_sol"]), р.get("запись"))

    print("-- полная история журнала -- несущая, а не украшение")
    р, с = зов()
    chk("запись взята из всей истории",
        р.get("откуда_запись") == "вся история журнала", р.get("откуда_запись"))
    # ЕСЛИ БЫ КОД ЧИТАЛ positions(), СВЕРКА ОТКАЗАЛА БЫ: поддельный positions()
    # нарочно режет поля покупки, ровно как боевой на старой ротации.
    _с = _Состояние({"CID": dict(ОСНОВА)})
    chk("через positions() поля покупки не видны -- значит история нужна",
        "lane_buy_native_sol" not in _с.positions()["CID"],
        sorted(_с.positions()["CID"]))
    chk("сверка по усечённой записи отказала бы словами",
        not сверка_числа(_с.positions()["CID"]).get("ok"),
        сверка_числа(_с.positions()["CID"]))

    print("-- отрицательный расход в суточный счёт не идёт")
    р, с = зов()
    chk("расход по формуле отрицателен (билет потрачен не целиком)",
        р.get("расход_по_формуле", 0) < 0, р.get("расход_по_формуле"))
    chk("в счёт пошёл 0.0, а не минус", р.get("расход_в_счёт") == 0.0,
        р.get("расход_в_счёт"))
    chk("суточный расход не уменьшился",
        с.счёты.get("2026-10-01", {}).get("spent_sol") == 0.0, с.счёты)
    chk("отток покупки лежит в записи своим числом",
        abs(р["запись"]["knizhit_ottok_pokupki_sol"] - 0.075775614) < ДОПУСК,
        р.get("запись"))
    chk("причина называет подмену расхода",
        "расход по формуле отрицателен" in р["запись"]["knizhit_pochemu"],
        р["запись"]["knizhit_pochemu"])
    р_п, с_п = зов({"lane_buy_native_sol": -0.10161384,
                    "vne_sluzhby_sol_net": 0.098183186},
                   итог_sol=round(-0.10161384 - 0.001005 + 0.098183186 + 0.00151384, 9))
    chk("положительный расход идёт в счёт как есть",
        р_п.get("ok") and abs(р_п.get("расход_в_счёт") - 0.00261884) < ДОПУСК,
        (р_п.get("why_not"), р_п.get("расход_в_счёт")))

    print("-- сухой прогон ничего не пишет")
    р, с = зов(живьём=False)
    chk("сухой: отказ словами", not р.get("ok") and "сухой" in (р.get("why_not") or ""))
    chk("сухой: add_pnl не зван", not с.звали_add_pnl, с.звали_add_pnl)

    print("-- охраны")
    for имя, правки, кв, кусок in (
        ("не закрыта", {"state": "open"}, {}, "не закрыта"),
        ("нет дописки", {"prodano_vne_sluzhby": None}, {}, "prodano_vne_sluzhby"),
        ("нет итога продажи", {"vne_sluzhby_sol_net": None}, {}, "vne_sluzhby_sol_net"),
        ("closed_sol_net уже стоит", {"closed_sol_net": 0.0137}, {}, "closed_sol_net уже стоит"),
        ("pnl_counted уже стоит", {"pnl_counted": True}, {}, "pnl_counted уже стоит"),
        ("уже книжено задним числом", {ПОЛЕ_ОТМЕТКИ: "2026-10-01"}, {}, "уже книжен"),
        ("число не сходится", {}, {"итог_sol": -0.05}, "не сходится с записью"),
        ("сутки не те", {}, {"сутки": "2026-10-02"}, "книжить не в те сутки"),
        ("сутки сегодняшние", {}, {"сутки": "2026-10-01", "сегодня_ключ": "2026-10-01"},
         "СЕГОДНЯШНИЕ"),
    ):
        р, с = зов(правки, **кв)
        chk(f"отказ: {имя}", (not р.get("ok")) and кусок in (р.get("why_not") or ""),
            р.get("why_not"))
        chk(f"отказ: {имя} -- ни одной записи в счёт", not с.звали_add_pnl, с.звали_add_pnl)

    print("-- разбор времени")
    chk("ISO с Z разбирается", разбор_времени("2026-10-01T21:58:33Z") == ПРОДАЖА_TS,
        разбор_времени("2026-10-01T21:58:33Z"))
    chk("epoch разбирается", разбор_времени("1790891913") == ПРОДАЖА_TS)
    chk("мусор даёт None", разбор_времени("вчера") is None)

    print(f"\nвсего сбоев: {len(сбоев)}" + (f" -- {сбоев}" if сбоев else ""))
    return 1 if сбоев else 0


class _Боевое:
    """Обёртка над ExecState: только то, что нужно книжению."""

    def __init__(self):
        import bloom_exec_state as ST  # noqa: PLC0415
        self._ST = ST
        self.с = ST.ExecState()

    def positions(self):
        """Позиции через ExecState -- для тех, кому хватает свежих строк."""
        return self.с.positions()

    def позиция_по_всей_истории(self, cid: str) -> dict:
        """Слить ВСЕ строки журнала по одному cid: текущий файл и все ротации.

        Порядок -- от старых к новым, чтобы поздние строки правили поля поверх
        ранних, ровно как это делает positions(). Ротации узнаются по mtime, а
        не по номеру в имени: номера у logrotate сдвигаются.
        """
        import gzip  # noqa: PLC0415
        путь = self.с.positions_path
        файлы = []
        try:
            файлы = sorted(путь.parent.glob(путь.name + ".*.gz"),
                           key=lambda п: п.stat().st_mtime)
        except OSError:
            файлы = []
        слито: dict = {}
        прочитано = {"файлов": 0, "строк": 0, "строк_этого_cid": 0}
        for ф in [*файлы, путь]:
            if not ф.exists():
                continue
            прочитано["файлов"] += 1
            открыть = (gzip.open if str(ф).endswith(".gz") else open)
            try:
                with открыть(ф, "rt", encoding="utf-8", errors="replace") as fh:
                    for строка in fh:
                        прочитано["строк"] += 1
                        if cid not in строка:
                            continue
                        try:
                            р = json.loads(строка)
                        except ValueError:
                            continue
                        if р.get("client_order_id") != cid:
                            continue
                        прочитано["строк_этого_cid"] += 1
                        слито.update(р)
            except OSError:
                continue
        if слито:
            слито["_прочитано"] = прочитано
        return слито

    def day_key_для(self, ts):
        return self._ST.day_key(ts)

    def pnl(self, ts=None):
        return self.с.pnl(ts)

    def add_pnl(self, **кв):
        return self.с.add_pnl(**кв)

    def update_position(self, cid, **поля):
        return self.с.update_position(cid, **поля)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--cid", default="")
    p.add_argument("--sutki", default="", help="сутки по Мадриду, YYYY-MM-DD")
    p.add_argument("--ts-prodazhi", default="", help="время продажи: ISO с Z или epoch")
    p.add_argument("--itog-sol", default="", help="названный итог позиции по цепи")
    p.add_argument("--out", default="")
    a = p.parse_args()
    if a.self_test:
        return self_test()
    ts = разбор_времени(a.ts_prodazhi)
    if ts is None:
        print(json.dumps({"ok": False, "why_not": f"время продажи не разобрано: "
                                                  f"{a.ts_prodazhi!r}"}, ensure_ascii=False))
        return 2
    try:
        итог = float(a.itog_sol)
    except ValueError:
        print(json.dumps({"ok": False, "why_not": f"итог не число: {a.itog_sol!r}"},
                         ensure_ascii=False))
        return 2
    sys.path.insert(0, os.environ.get("BLOOM_CODE_DIR", "") or ".")
    из_ = книжить(состояние=_Боевое(), cid=a.cid, сутки=a.sutki, ts_продажи=ts,
                  итог_sol=итог, живьём=(os.environ.get("KNIZHIT_LIVE") == "1"))
    текст = json.dumps(из_, ensure_ascii=False, indent=1)
    print(текст)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(текст)
    return 0 if из_.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
