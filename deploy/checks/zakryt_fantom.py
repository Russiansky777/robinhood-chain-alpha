#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Закрыть ФАНТОМ -- позицию, проданную вне службы, записью по цепи.

ЗАЧЕМ (слово владельца 03.10). Позиция HgqNsyhfFY1f (cand1_03, вход 0.3) была
продана разовым прогоном в 22:22:35Z, токена на её счёте нет, а служба считает
её открытой. Такая позиция называется фантомом, и она дорога втройне:

  1. СТОРОЖ КРУТИТ ПО НЕЙ ПРОДАЖУ КАЖДЫЙ КРУГ. Круг сторожа обходит открытые
     позиции ПОДРЯД, и сетевые попытки по фантому отодвигают срок продажи
     остальных. Живой счёт ночи 02->03.10: держание выросло со 104-115 слотов
     до 155-188 при плане 108, то есть ровно на один период круга (15 с).
  2. ГЕЙТ ПЕРЕЗАПУСКА не пускает деплой, пока есть зависшие, -- а починка
     ехать может только деплоем.
  3. СУТОЧНЫЙ СЧЁТ не видит её результата: она не закрыта, и её -0.25 SOL в
     дневном итоге отсутствует.

ЧТО ЭТОТ МОДУЛЬ ДЕЛАЕТ. Дописывает в журнал позиций ОДНУ закрывающую запись с
причиной "продано вне службы" и подписью продажи. Ничего не подписывает и
никуда не отправляет: продажа уже состоялась на цепи, здесь только учёт.

ЧЕГО ОН НЕ СДЕЛАЕТ НИ ПРИ КАКОМ ВХОДЕ -- закроет позицию, у которой токен ещё
есть. Четыре охраны, и каждая по цепи, а не на слово:
  * позиция существует и НЕ закрыта;
  * названная подпись подтверждена цепью и без ошибки;
  * в этой транзакции участвовал НАШ кошелёк;
  * остаток минта на НАШЕМ счёте -- ноль.
Любая не сошлась -- отказ словами и ни одной записи.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

HELIUS = "https://mainnet.helius-rpc.com"
ПРИЧИНА = "продано вне службы"


def урл() -> str:
    к = (os.environ.get("HELIUS_API_KEY") or os.environ.get("HELIUS_API") or "").strip()
    return f"{HELIUS}/?api-key={к}"


def зов(метод: str, параметры, *, таймаут: float = 45.0) -> dict:
    import urllib.request  # noqa: PLC0415

    тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                        "params": параметры}).encode()
    try:
        зпр = urllib.request.Request(урл(), data=тело,
                                      headers={"content-type": "application/json"})
        with urllib.request.urlopen(зпр, timeout=таймаут) as отв:  # noqa: S310
            д = json.loads(отв.read())
    except Exception as сбой:  # noqa: BLE001
        return {"ok": False, "why_not": f"{type(сбой).__name__}: {str(сбой)[:140]}"}
    if "error" in д:
        return {"ok": False, "why_not": str(д["error"])[:200]}
    return {"ok": True, "result": д.get("result"), "why_not": None}


def проверка_по_цепи(*, подпись: str, кошелёк: str, минт: str, счёт: str | None,
                      зов_фн=None) -> dict:
    """Четыре охраны по цепи. Чистая логика: сеть приходит функцией."""
    з = зов_фн if зов_фн is not None else зов
    из_: dict = {"ok": False, "why_not": None, "подпись_подтверждена": False,
                 "наш_кошелёк_в_сделке": False, "остаток_raw": None,
                 "счёт": счёт, "слот": None, "дельта_sol": None}
    if not подпись or not кошелёк or not минт:
        из_["why_not"] = "подпись, кошелёк и минт обязательны"
        return из_
    о = з("getTransaction", [подпись, {"encoding": "jsonParsed",
                                        "commitment": "confirmed",
                                        "maxSupportedTransactionVersion": 0}])
    tx = (о or {}).get("result")
    if not (о or {}).get("ok") or not tx:
        из_["why_not"] = (f"подпись не прочитана: "
                          f"{(о or {}).get('why_not') or 'узел не отдал'}")
        return из_
    мета = tx.get("meta") or {}
    if мета.get("err"):
        из_["why_not"] = f"транзакция с ошибкой: {str(мета['err'])[:120]}"
        return из_
    из_["подпись_подтверждена"] = True
    из_["слот"] = tx.get("slot")
    ключи = [(к.get("pubkey") if isinstance(к, dict) else к)
             for к in (((tx.get("transaction") or {}).get("message") or {})
                       .get("accountKeys") or [])]
    загр = мета.get("loadedAddresses") or {}
    ключи += list(загр.get("writable") or []) + list(загр.get("readonly") or [])
    if кошелёк not in ключи:
        из_["why_not"] = "нашего кошелька в этой транзакции нет -- подпись чужая"
        return из_
    из_["наш_кошелёк_в_сделке"] = True
    # НАТИВНАЯ ДЕЛЬТА КОШЕЛЬКА -- справочно, для записи итога.
    try:
        место = ключи.index(кошелёк)
        до = (мета.get("preBalances") or [])[место]
        после = (мета.get("postBalances") or [])[место]
        из_["дельта_sol"] = round((int(после) - int(до)) / 1_000_000_000, 9)
    except Exception:  # noqa: BLE001
        из_["дельта_sol"] = None
    # ОСТАТОК МИНТА НА НАШЕМ СЧЁТЕ. Без счёта спрашиваем по владельцу и минту:
    # счёт мог быть уже закрыт, и тогда остатка нет вовсе -- это тоже ноль.
    if счёт:
        о = з("getTokenAccountBalance", [счёт, {"commitment": "confirmed"}])
        зн = (((о.get("result") or {}).get("value") or {}).get("amount")
              if о.get("ok") else None)
        if о.get("ok") and зн is not None:
            из_["остаток_raw"] = int(зн)
        elif о.get("ok"):
            из_["остаток_raw"] = 0
        else:
            из_["why_not"] = f"остаток счёта не прочитан: {о.get('why_not')}"
            return из_
    else:
        о = з("getTokenAccountsByOwner",
              [кошелёк, {"mint": минт}, {"encoding": "jsonParsed",
                                          "commitment": "confirmed"}])
        if not о.get("ok"):
            из_["why_not"] = f"счета минта не прочитаны: {о.get('why_not')}"
            return из_
        всего = 0
        for сч in ((о.get("result") or {}).get("value") or []):
            данные = (((сч.get("account") or {}).get("data") or {})
                      .get("parsed") or {}).get("info") or {}
            кол = ((данные.get("tokenAmount") or {}).get("amount"))
            try:
                всего += int(кол)
            except (TypeError, ValueError):
                pass
        из_["остаток_raw"] = всего
    if int(из_["остаток_raw"] or 0) > 0:
        из_["why_not"] = (f"остаток минта на нашем счёте {из_['остаток_raw']} -- "
                          "это НЕ фантом, позиция держит токен и закрывать её "
                          "записью нельзя")
        return из_
    из_["ok"] = True
    return из_


def закрыть(*, состояние, cid: str, подпись: str, минт: str, кошелёк: str,
            счёт: str | None = None, итог_sol=None, живьём: bool = False,
            зов_фн=None, сейчас: float | None = None) -> dict:
    """Одна закрывающая запись. Пишет ТОЛЬКО поля учёта продажи."""
    сейчас = float(сейчас if сейчас is not None else time.time())
    из_: dict = {"ok": False, "why_not": None, "cid": cid, "живьём": живьём,
                 "цепь": None, "запись": None}
    позиции = состояние.positions()
    поз = (позиции or {}).get(cid)
    if not поз:
        из_["why_not"] = f"позиции {cid} в журнале нет"
        return из_
    из_["состояние_до"] = поз.get("state")
    if str(поз.get("state") or "") in ("closed",):
        из_["why_not"] = "позиция уже закрыта -- писать нечего"
        return из_
    if минт and поз.get("mint") and поз["mint"] != минт:
        из_["why_not"] = (f"минт позиции {поз['mint']} не тот, что назван "
                          f"({минт}) -- закрывать не ту позицию нельзя")
        return из_
    ц = проверка_по_цепи(подпись=подпись, кошелёк=кошелёк,
                          минт=(минт or поз.get("mint") or ""), счёт=счёт,
                          зов_фн=зов_фн)
    из_["цепь"] = ц
    if not ц.get("ok"):
        из_["why_not"] = ц.get("why_not")
        return из_
    чистый = (float(итог_sol) if итог_sol is not None
              else (ц.get("дельта_sol") if ц.get("дельта_sol") is not None else None))
    поля = {
        "state": "closed",
        "ts_closed": сейчас,
        "closed_reason": (f"{ПРИЧИНА}: разовый прогон, подпись {подпись[:12]}…, "
                           f"остаток минта 0 по цепи"),
        "closed_sol_net": чистый,
        # ДОКАЗАТЕЛЬСТВА ПРОДАЖИ -- В ТЕХ ЖЕ ПОЛЯХ, ЧТО У ОБЫЧНОГО ЗАКРЫТИЯ:
        # иначе признак аварии и сверки прочитали бы позицию как непроданную.
        "lane_chain_sell_sig": подпись,
        "last_sell_reported": True,
        "last_sell_signatures": [подпись],
        "last_sell_outcome": {"known": True, "ok": True, "signature": подпись,
                               "sol_delta_net": чистый,
                               "откуда": "разовый прогон вне службы"},
        "sell_ostatok_raw": 0,
        "prodano_vne_sluzhby": True,
    }
    из_["запись"] = поля
    if not живьём:
        из_["why_not"] = ("сухой прогон: записи нет (живьём -- "
                           "ZAKRYT_FANTOM_LIVE=1)")
        return из_
    строка = состояние.update_position(cid, **поля)
    из_.update(ok=True, why_not=None, строка_журнала={
        к: строка.get(к) for к in ("client_order_id", "state", "closed_reason",
                                    "closed_sol_net", "ts_update_utc",
                                    "pnl_counted", "realized_sol")})
    return из_


ПРИЧИНА_ДОПИСКИ = "итог по цепи дописан: продано вне службы"


def дописать_итог(*, состояние, cid: str, подпись: str, минт: str, кошелёк: str,
                   счёт: str | None = None, итог_sol=None, живьём: bool = False,
                   зов_фн=None, сейчас: float | None = None) -> dict:
    """Дописать подпись и итог по цепи в УЖЕ ЗАКРЫТУЮ запись.

    ЗАЧЕМ (слово владельца 03.10, п.3). BTN и Fantasy Index 6900 служба закрыла
    причиной "продавать нечего: остаток минта 0" -- и это правда: токена на
    счёте уже не было. Но продажа БЫЛА, нашим же ключом через Jupiter, разовым
    прогоном, и её подписи в записи нет. Правило 13 требует числа по цепи, а в
    записи их нет вовсе.

    ЧЕМ ЭТО ОТЛИЧАЕТСЯ ОТ закрыть(). Здесь позиция УЖЕ закрыта, и трогать
    state, ts_closed и closed_sol_net НЕЛЬЗЯ:
      * state и ts_closed уже стоят -- переписывать их значит менять историю;
      * closed_sol_net читает СУТОЧНЫЙ СЧЁТ. Эти две сделки -- от 01.10, их
        сутки давно закрыты и доложены. Дописать в них деньги задним числом
        значит молча переписать уже доложенное число. Поэтому итог идёт в
        СВОЁ поле vne_sluzhby_sol_net, а не в closed_sol_net; что с ним делать
        в учёте -- решает владелец, а не этот модуль.

    ОХРАНЫ. Те же четыре по цепи, плюс две свои:
      * позиция существует и ЗАКРЫТА (открытую закрывает закрыть(), не это);
      * подписи продажи в записи ЕЩЁ НЕТ -- иначе дописка повторилась бы.
    """
    сейчас = float(сейчас if сейчас is not None else time.time())
    из_: dict = {"ok": False, "why_not": None, "cid": cid, "живьём": живьём,
                 "цепь": None, "запись": None, "режим": "дописка"}
    позиции = состояние.positions()
    поз = (позиции or {}).get(cid)
    if not поз:
        из_["why_not"] = f"позиции {cid} в журнале нет"
        return из_
    из_["состояние_до"] = поз.get("state")
    if str(поз.get("state") or "") != "closed":
        из_["why_not"] = ("позиция не закрыта -- дописка только в закрытую "
                           "запись; открытую закрывает закрыть()")
        return из_
    было = str(поз.get("lane_chain_sell_sig") or "")
    if было:
        из_["why_not"] = (f"в записи уже стоит подпись продажи {было[:12]}… -- "
                           "дописывать второй раз нечего")
        return из_
    if минт and поз.get("mint") and поз["mint"] != минт:
        из_["why_not"] = (f"минт позиции {поз['mint']} не тот, что назван "
                           f"({минт}) -- дописывать не в ту запись нельзя")
        return из_
    ц = проверка_по_цепи(подпись=подпись, кошелёк=кошелёк,
                          минт=(минт or поз.get("mint") or ""), счёт=счёт,
                          зов_фн=зов_фн)
    из_["цепь"] = ц
    if not ц.get("ok"):
        из_["why_not"] = ц.get("why_not")
        return из_
    чистый = (float(итог_sol) if итог_sol is not None
              else (ц.get("дельта_sol") if ц.get("дельта_sol") is not None else None))
    прежняя = str(поз.get("closed_reason") or "")
    поля = {
        # STATE, TS_CLOSED И CLOSED_SOL_NET НЕ ТРОГАЕМ -- см. docstring.
        "closed_reason": (f"{прежняя}; {ПРИЧИНА_ДОПИСКИ}, подпись "
                           f"{подпись[:12]}…, слот {ц.get('слот')}")[:600],
        "lane_chain_sell_sig": подпись,
        "last_sell_reported": True,
        "last_sell_signatures": [подпись],
        "last_sell_outcome": {"known": True, "ok": True, "signature": подпись,
                               "slot": ц.get("слот"),
                               "sol_delta_net": чистый,
                               "откуда": "разовый прогон вне службы, "
                                          "дописано по цепи"},
        "sell_ostatok_raw": 0,
        "prodano_vne_sluzhby": True,
        "vne_sluzhby_sol_net": чистый,
        "ts_vne_sluzhby_dopisano": сейчас,
    }
    из_["запись"] = поля
    if not живьём:
        из_["why_not"] = ("сухой прогон: записи нет (живьём -- "
                           "ZAKRYT_FANTOM_LIVE=1)")
        return из_
    строка = состояние.update_position(cid, **поля)
    из_.update(ok=True, why_not=None, строка_журнала={
        к: строка.get(к) for к in ("client_order_id", "state", "closed_reason",
                                    "lane_chain_sell_sig",
                                    "vne_sluzhby_sol_net", "ts_update_utc")})
    return из_


# ------------------------------------------------------------------ самопроверка

class _Состояние:
    def __init__(self, позиции):
        # КОПИЯ ГЛУБОКАЯ, А НЕ ПОВЕРХНОСТНАЯ: при поверхностной закрывающая
        # запись меняла бы ОБРАЗЕЦ позиции, и следующие случаи самопроверки
        # получали бы её уже закрытой -- проверка проверяла бы не то.
        import copy as _copy  # noqa: PLC0415

        self._п = _copy.deepcopy(dict(позиции))
        self.записи = []

    def positions(self):
        return self._п

    def update_position(self, cid, **поля):
        стр = {"client_order_id": cid, "ts_update_utc": "сейчас", **поля}
        self.записи.append(стр)
        self._п.setdefault(cid, {}).update(поля)
        return стр


def self_test() -> int:
    плохих = []

    def chk(имя, усл, что=""):
        if усл:
            print(f"ok    {имя}")
        else:
            плохих.append(имя)
            print(f"ПЛОХО {имя}: {что}")

    КОШ, МИНТ, СЧЁТ, ПОДП = "НАШ", "МИНТ", "СЧЁТ", "ПОДПИСЬ_ПРОДАЖИ"

    def цепь(остаток=0, ошибка=None, ключи=(КОШ,), отдал=True):
        def з(метод, парам):
            if метод == "getTransaction":
                if not отдал:
                    return {"ok": False, "why_not": "узел молчит"}
                return {"ok": True, "result": {
                    "slot": 7, "meta": {"err": ошибка,
                                         "preBalances": [1_000_000_000],
                                         "postBalances": [1_049_066_611]},
                    "transaction": {"message": {"accountKeys": list(ключи)}}}}
            if метод == "getTokenAccountBalance":
                return {"ok": True, "result": {"value": {"amount": str(остаток)}}}
            return {"ok": True, "result": {"value": []}}
        return з

    поз = {"ЦИД": {"client_order_id": "ЦИД", "mint": МИНТ, "state": "unsold",
                    "sol_in": 0.3}}

    ст = _Состояние(поз)
    р = закрыть(состояние=ст, cid="ЦИД", подпись=ПОДП, минт=МИНТ, кошелёк=КОШ,
                счёт=СЧЁТ, живьём=False, зов_фн=цепь())
    chk("сухой прогон НИЧЕГО не пишет, но показывает запись",
        not р["ok"] and ст.записи == [] and р["запись"]["state"] == "closed", р)
    chk("и дельта кошелька по цепи попала в итог",
        р["запись"]["closed_sol_net"] == 0.049066611, р["запись"])

    ст = _Состояние(поз)
    р = закрыть(состояние=ст, cid="ЦИД", подпись=ПОДП, минт=МИНТ, кошелёк=КОШ,
                счёт=СЧЁТ, живьём=True, зов_фн=цепь())
    chk("живьём пишется РОВНО одна запись, и она закрывающая",
        р["ok"] and len(ст.записи) == 1 and ст.записи[0]["state"] == "closed", р)
    chk("в записи стоят доказательства продажи, а не только state",
        ст.записи[0]["lane_chain_sell_sig"] == ПОДП
        and ст.записи[0]["last_sell_reported"] is True
        and ст.записи[0]["sell_ostatok_raw"] == 0, ст.записи[0])
    chk("и причина названа словами владельца",
        ПРИЧИНА in ст.записи[0]["closed_reason"], ст.записи[0]["closed_reason"])

    # ОХРАНЫ -- КАЖДАЯ СВОИМ ОТКАЗОМ И БЕЗ ЗАПИСИ.
    ст = _Состояние(поз)
    р = закрыть(состояние=ст, cid="ЦИД", подпись=ПОДП, минт=МИНТ, кошелёк=КОШ,
                счёт=СЧЁТ, живьём=True, зов_фн=цепь(остаток=1_774_333_122_592))
    chk("токен ещё у нас -- ОТКАЗ и ни одной записи",
        not р["ok"] and ст.записи == [] and "НЕ фантом" in (р["why_not"] or ""), р)
    ст = _Состояние(поз)
    р = закрыть(состояние=ст, cid="ЦИД", подпись=ПОДП, минт=МИНТ, кошелёк=КОШ,
                счёт=СЧЁТ, живьём=True, зов_фн=цепь(ключи=("ЧУЖОЙ",)))
    chk("нашего кошелька в сделке нет -- отказ: подпись чужая",
        not р["ok"] and ст.записи == [] and "чужая" in (р["why_not"] or ""), р)
    ст = _Состояние(поз)
    р = закрыть(состояние=ст, cid="ЦИД", подпись=ПОДП, минт=МИНТ, кошелёк=КОШ,
                счёт=СЧЁТ, живьём=True, зов_фн=цепь(ошибка={"InstructionError": 1}))
    chk("транзакция с ошибкой -- отказ, продажи не было",
        not р["ok"] and ст.записи == [] and "с ошибкой" in (р["why_not"] or ""), р)
    ст = _Состояние(поз)
    р = закрыть(состояние=ст, cid="ЦИД", подпись=ПОДП, минт=МИНТ, кошелёк=КОШ,
                счёт=СЧЁТ, живьём=True, зов_фн=цепь(отдал=False))
    chk("узел не отдал подпись -- отказ словами, неясность не закрывает позицию",
        not р["ok"] and ст.записи == [] and "не прочитана" in (р["why_not"] or ""), р)
    ст = _Состояние(поз)
    р = закрыть(состояние=ст, cid="НЕТ_ТАКОЙ", подпись=ПОДП, минт=МИНТ,
                кошелёк=КОШ, счёт=СЧЁТ, живьём=True, зов_фн=цепь())
    chk("позиции нет в журнале -- отказ", not р["ok"] and ст.записи == [], р)
    ст = _Состояние({"ЦИД": {"client_order_id": "ЦИД", "mint": "ДРУГОЙ_МИНТ",
                              "state": "unsold"}})
    р = закрыть(состояние=ст, cid="ЦИД", подпись=ПОДП, минт=МИНТ, кошелёк=КОШ,
                счёт=СЧЁТ, живьём=True, зов_фн=цепь())
    chk("минт позиции не тот, что назван -- отказ: не ту позицию не закрываем",
        not р["ok"] and ст.записи == [] and "не тот" in (р["why_not"] or ""), р)
    ст = _Состояние({"ЦИД": {"client_order_id": "ЦИД", "mint": МИНТ,
                              "state": "closed"}})
    р = закрыть(состояние=ст, cid="ЦИД", подпись=ПОДП, минт=МИНТ, кошелёк=КОШ,
                счёт=СЧЁТ, живьём=True, зов_фн=цепь())
    chk("уже закрытую не закрываем второй раз (суточный счёт не задвоится)",
        not р["ok"] and ст.записи == [], р)
    # ИТОГ МОЖНО НАЗВАТЬ ЯВНО -- когда он посчитан по цепи целиком, а не по
    # нативной дельте одной транзакции.
    ст = _Состояние(поз)
    р = закрыть(состояние=ст, cid="ЦИД", подпись=ПОДП, минт=МИНТ, кошелёк=КОШ,
                счёт=СЧЁТ, итог_sol=-0.250933389, живьём=True, зов_фн=цепь())
    chk("названный итог идёт в запись вместо дельты одной транзакции",
        р["ok"] and ст.записи[0]["closed_sol_net"] == -0.250933389, ст.записи[0])

    # ---- ДОПИСКА В УЖЕ ЗАКРЫТУЮ ЗАПИСЬ (п.3: BTN и Fantasy)
    закрытая = {"ЦИД": {"client_order_id": "ЦИД", "mint": МИНТ,
                         "state": "closed", "ts_closed": 111.0,
                         "closed_sol_net": None,
                         "closed_reason": "продавать нечего: остаток минта 0"}}
    ст = _Состояние(закрытая)
    р = дописать_итог(состояние=ст, cid="ЦИД", подпись=ПОДП, минт=МИНТ,
                       кошелёк=КОШ, счёт=СЧЁТ, живьём=False, зов_фн=цепь())
    chk("дописка: сухой прогон ничего не пишет",
        not р["ok"] and ст.записи == [], р)
    chk("дописка НЕ трогает state, ts_closed и closed_sol_net",
        "state" not in р["запись"] and "ts_closed" not in р["запись"]
        and "closed_sol_net" not in р["запись"], р["запись"])
    chk("дописка кладёт итог в своё поле vne_sluzhby_sol_net",
        р["запись"]["vne_sluzhby_sol_net"] == 0.049066611, р["запись"])
    chk("прежняя причина закрытия сохранена, новая добавлена",
        р["запись"]["closed_reason"].startswith("продавать нечего")
        and ПРИЧИНА_ДОПИСКИ in р["запись"]["closed_reason"],
        р["запись"]["closed_reason"])
    ст = _Состояние(закрытая)
    р = дописать_итог(состояние=ст, cid="ЦИД", подпись=ПОДП, минт=МИНТ,
                       кошелёк=КОШ, счёт=СЧЁТ, итог_sol=-0.061544696,
                       живьём=True, зов_фн=цепь())
    chk("дописка живьём: ровно одна запись, подпись и слот на месте",
        р["ok"] and len(ст.записи) == 1
        and ст.записи[0]["lane_chain_sell_sig"] == ПОДП
        and ст.записи[0]["last_sell_outcome"]["slot"] == 7, р)
    chk("названный итог идёт в vne_sluzhby_sol_net",
        ст.записи[0]["vne_sluzhby_sol_net"] == -0.061544696, ст.записи[0])
    ст = _Состояние(поз)
    р = дописать_итог(состояние=ст, cid="ЦИД", подпись=ПОДП, минт=МИНТ,
                       кошелёк=КОШ, счёт=СЧЁТ, живьём=True, зов_фн=цепь())
    chk("дописка в НЕзакрытую -- отказ: это дело закрыть(), не дописки",
        not р["ok"] and ст.записи == [] and "не закрыта" in (р["why_not"] or ""), р)
    уже = {"ЦИД": {"client_order_id": "ЦИД", "mint": МИНТ, "state": "closed",
                    "lane_chain_sell_sig": "СТАРАЯ_ПОДПИСЬ"}}
    ст = _Состояние(уже)
    р = дописать_итог(состояние=ст, cid="ЦИД", подпись=ПОДП, минт=МИНТ,
                       кошелёк=КОШ, счёт=СЧЁТ, живьём=True, зов_фн=цепь())
    chk("подпись уже стоит -- дописка не повторяется",
        not р["ok"] and ст.записи == [] and "уже стоит" in (р["why_not"] or ""), р)
    ст = _Состояние(закрытая)
    р = дописать_итог(состояние=ст, cid="ЦИД", подпись=ПОДП, минт=МИНТ,
                       кошелёк=КОШ, счёт=СЧЁТ, живьём=True,
                       зов_фн=цепь(остаток=5))
    chk("токен ещё у нас -- дописка тоже отказывает",
        not р["ok"] and ст.записи == [], р)
    ст = _Состояние(закрытая)
    р = дописать_итог(состояние=ст, cid="ЦИД", подпись=ПОДП, минт=МИНТ,
                       кошелёк=КОШ, счёт=СЧЁТ, живьём=True,
                       зов_фн=цепь(ключи=("ЧУЖОЙ",)))
    chk("нашего кошелька в сделке нет -- дописка отказывает",
        not р["ok"] and ст.записи == [] and "чужая" in (р["why_not"] or ""), р)

    print(f"\nитог: проверок плохих {len(плохих)}"
          + ("" if not плохих else ": " + "; ".join(плохих)))
    return 1 if плохих else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--cid", default="", help="client_order_id позиции")
    p.add_argument("--podpis", default="", help="подпись продажи вне службы")
    p.add_argument("--mint", default="")
    p.add_argument("--koshelek", default="")
    p.add_argument("--schet", default="", help="счёт токена (пусто -- спросим по владельцу)")
    p.add_argument("--itog-sol", default="", help="итог по цепи, если посчитан отдельно")
    p.add_argument("--dopisat", action="store_true",
                   help="дописать итог по цепи в УЖЕ ЗАКРЫТУЮ запись "
                        "(state и суточное число не трогаются)")
    p.add_argument("--out", default="")
    a = p.parse_args()
    if a.self_test:
        return self_test()
    sys.path.insert(0, os.environ.get("BLOOM_CODE_DIR") or "/home/bot/bloom_executor")
    import bloom_exec_state as ST  # noqa: PLC0415

    итог_sol = None
    if str(a.itog_sol).strip():
        итог_sol = float(a.itog_sol)
    делать = дописать_итог if a.dopisat else закрыть
    р = делать(состояние=ST.ExecState(), cid=a.cid, подпись=a.podpis,
               минт=a.mint, кошелёк=(a.koshelek or ST.EXECUTOR_WALLET),
               счёт=(a.schet or None), итог_sol=итог_sol,
               живьём=(os.environ.get("ZAKRYT_FANTOM_LIVE") == "1"))
    print(json.dumps(р, ensure_ascii=False, indent=1)[:4000])
    if a.out:
        with open(a.out, "w", encoding="utf-8") as ф:
            json.dump(р, ф, ensure_ascii=False, indent=1)
        print("записано:", a.out)
    return 0 if р.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
