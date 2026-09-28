#!/usr/bin/env python3
"""Утренний отчёт по зонду преконфов: доли, опережение, ложные преконфы.

ЗАЧЕМ (владелец 28.09, дополнение по преконфам, п.4 и п.5):
  (а) доля покупок источников за сутки, севших в слоты BAM/Harmonic-лидеров;
  (б) из них -- доля с пришедшим преконфом;
  (в) выигрыш "преконф -> WS" p50/p90 в мс;
  (г) ложные преконфы (пришёл, транзакция не села) -- число;
  (д) сообщений и стоимость по счётчику;
  (п.5) у транзакций источников, по которым преконфа НЕ было в BAM/Harmonic-
        слоте, -- версия транзакции и есть ли наш адрес среди СТАТИЧЕСКИХ
        ключей (в preconfs.proto сказано: фильтры смотрят только статические
        ключи, адреса из таблиц не ловятся).

ЧЬИ СЛОТЫ. Строку версии из getClusterNodes пометить "BAM" нельзя: там нет
ничего, кроме semver (замер 28.09: 4.3.0 у 1866 узлов, 26.9.4, 0.1106.40201 и
так далее -- имени клиента в ответе нет вовсе). Поэтому метка слота берётся
ДВУМЯ способами, и оба честные:
  * наблюдённая -- лидеры тех слотов, по которым фид сам что-то отдал в окне
    зонда (нижняя граница: слот, где под наш фильтр ничего не подошло, так и
    останется неразмеченным);
  * по версии -- если владелец задал подстроки TRITON_VERSII_BAM /
    TRITON_VERSII_HARMONIC, считается и по ним.

СЕТИ РОВНО ДВА ВЫЗОВА НА ПАРТИЮ: getSignatureStatuses (села ли транзакция) и
getTransaction только для разбора статических ключей, с пределом. Ни подписи,
ни отправки.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import triton_validators as TV  # noqa: E402

ПРЕДЕЛ_РАЗБОРА_КЛЮЧЕЙ = 40
ПАРТИЯ_СТАТУСОВ = 256


def _медиана(ряд: list):
    if not ряд:
        return None
    р = sorted(ряд)
    n = len(р)
    return round(р[n // 2] if n % 2 else (р[n // 2 - 1] + р[n // 2]) / 2, 2)


def _p90(ряд: list):
    if not ряд:
        return None
    р = sorted(ряд)
    return round(р[min(len(р) - 1, int(round(0.9 * (len(р) - 1))))], 2)


def преконфы_из_журнала(пути: list, *, с_ts=None, по_ts=None) -> dict:
    """Подпись -> самое раннее появление на фиде. Журнал читается ПОТОКОМ."""
    из_: dict = {}
    видов: dict = {}
    for путь in пути or []:
        п = Path(путь)
        if not п.exists():
            continue
        with п.open(encoding="utf-8") as ф:
            for строка in ф:
                строка = строка.strip()
                if not строка:
                    continue
                try:
                    з = json.loads(строка)
                except ValueError:
                    continue
                видов[з.get("kind")] = видов.get(з.get("kind"), 0) + 1
                if з.get("kind") != "transaction":
                    continue
                t = з.get("t_recv")
                if t is None:
                    continue
                if с_ts is not None and float(t) < float(с_ts):
                    continue
                if по_ts is not None and float(t) > float(по_ts):
                    continue
                подпись = з.get("signature")
                if not подпись:
                    continue
                было = из_.get(подпись)
                if было is None or float(t) < float(было["t_recv"]):
                    из_[подпись] = {"t_recv": float(t), "t_mono": з.get("t_mono"),
                                     "slot": з.get("slot"), "feed": з.get("feed"),
                                     "region": з.get("region"),
                                     "filters": з.get("filters"),
                                     "result": з.get("result")}
    return {"preconfs": из_, "kinds": видов}


def покупки_источников(каталог: str, *, с_ts=None, по_ts=None) -> dict:
    """Покупки источников из журнала решений: kind == buy, потоком.

    kind, а не action: action -- это НАШЕ решение (buy/skip), а вопрос владельца
    про транзакции источников. Проверено на живом журнале: kind buy -- это
    разобранная покупка источника, code NOT_A_BUY -- не покупка.
    """
    из_: dict = {}
    всего = 0
    for путь in sorted(Path(каталог).glob("decisions.jsonl*")):
        открыть = (__import__("gzip").open if путь.suffix == ".gz" else open)
        try:
            ф = открыть(путь, "rt", encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        with ф:
            for строка in ф:
                строка = строка.strip()
                if not строка or '"kind"' not in строка:
                    continue
                try:
                    з = json.loads(строка)
                except ValueError:
                    continue
                if з.get("kind") != "buy":
                    continue
                t = з.get("t_recv_ts") or з.get("t_recv")
                подпись = з.get("signature")
                if not подпись or t is None:
                    continue
                t = float(t)
                if с_ts is not None and t < float(с_ts):
                    continue
                if по_ts is not None and t > float(по_ts):
                    continue
                всего += 1
                было = из_.get(подпись)
                if было is None or t < float(было["t_recv"]):
                    из_[подпись] = {"t_recv": t,
                                     "slot": з.get("source_slot") or з.get("slot"),
                                     "source": з.get("source"),
                                     "group": з.get("source_task") or з.get("mode"),
                                     "tx_version": з.get("tx_version")}
    return {"buys": из_, "rows": всего}


def наблюдённые_лидеры(преконфы: dict, карта: dict) -> dict:
    """Лидеры слотов, по которым фид что-то отдал: наблюдённая метка."""
    по_фидам: dict = {}
    без_лидера = 0
    for _, з in (преконфы or {}).items():
        слот = з.get("slot")
        if слот is None:
            continue
        лидер = карта.get(int(слот))
        if not лидер:
            без_лидера += 1
            continue
        по_фидам.setdefault(з.get("feed") or "?", set()).add(лидер)
    return {"leaders": {ф: sorted(мн) for ф, мн in по_фидам.items()},
             "counts": {ф: len(мн) for ф, мн in по_фидам.items()},
             "slots_without_leader": без_лидера}


def доля_слотов_лидеров(лидеры: set, расписание_: dict) -> float | None:
    всего = sum(len(р or []) for р in (расписание_ or {}).values())
    если = sum(len((расписание_ or {}).get(к) or []) for к in лидеры or ())
    return round(если / всего, 6) if всего else None


def позвать(метод, параметры, *, урл_=None):
    return TV.позвать(метод, параметры, урл_=урл_)


def сели_ли(подписи: list, *, урл_=None) -> dict:
    """getSignatureStatuses партиями: села транзакция или нет.

    searchTransactionHistory=True: преконф мог прийти задолго до отчёта, и
    статус в кеше последних слотов уже не лежит.
    """
    из_ = {"landed": {}, "why_not": None, "calls": 0}
    ряд = [п for п in подписи if п]
    for i in range(0, len(ряд), ПАРТИЯ_СТАТУСОВ):
        часть = ряд[i:i + ПАРТИЯ_СТАТУСОВ]
        отв = позвать("getSignatureStatuses",
                       [часть, {"searchTransactionHistory": True}], урл_=урл_)
        из_["calls"] += 1
        if not отв["ok"]:
            из_["why_not"] = отв["why_not"]
            return из_
        значения = ((отв["result"] or {}).get("value") or [])
        for п, зн in zip(часть, значения):
            из_["landed"][п] = (None if зн is None else
                                 {"slot": зн.get("slot"),
                                  "err": zn_err(зн)})
    return из_


def zn_err(зн: dict):
    о = зн.get("err")
    return None if о is None else str(о)[:120]


def статические_ключи(подпись: str, *, урл_=None) -> dict:
    """Версия транзакции и её СТАТИЧЕСКИЕ ключи против адресов из таблиц."""
    отв = позвать("getTransaction",
                   [подпись, {"encoding": "json",
                               "maxSupportedTransactionVersion": 0}], урл_=урл_)
    if not отв["ok"] or not отв["result"]:
        return {"ok": False, "why_not": отв.get("why_not") or "транзакции нет",
                 "version": None, "static": [], "alt": []}
    р = отв["result"]
    сообщение = ((р.get("transaction") or {}).get("message") or {})
    загруженные = ((р.get("meta") or {}).get("loadedAddresses") or {})
    alt = list(загруженные.get("writable") or []) + list(
        загруженные.get("readonly") or [])
    return {"ok": True, "why_not": None,
             "version": р.get("version"),
             "slot": р.get("slot"),
             "static": list(сообщение.get("accountKeys") or []),
             # ПОДПИСАНТЫ -- ПЕРВЫЕ numRequiredSignatures СТАТИЧЕСКИХ КЛЮЧЕЙ
             # (так же это определено в preconfs.proto про signer_include).
             # Считать подписантом только плательщика неверно: на живой
             # проверке 28.09 три сообщения из шести подошли фильтру
             # podpisanty, а "наш плательщик" показывал ноль.
             "podpisantov": ((сообщение.get("header") or {})
                              .get("numRequiredSignatures")),
             "alt": alt}


def отчёт(*, журналы: list, каталог_решений: str, снимок: dict,
           расписание_: dict, с_ts=None, по_ts=None, урл_=None,
           предел_разбора: int = ПРЕДЕЛ_РАЗБОРА_КЛЮЧЕЙ,
           без_сети: bool = False, расход: dict | None = None) -> dict:
    """Всё, что просил владелец, одним словарём. Числа -- или None с причиной."""
    пре = преконфы_из_журнала(журналы, с_ts=с_ts, по_ts=по_ts)
    преконфы = пре["preconfs"]
    пок = покупки_источников(каталог_решений, с_ts=с_ts, по_ts=по_ts)
    покупки = пок["buys"]
    первый = снимок.get("firstSlot")
    карта = (TV.карта_слотов(расписание_, первый)
             if расписание_ and первый is not None else {})
    метки = снимок.get("metki") or {}
    набл = наблюдённые_лидеры(преконфы, карта)
    лидеры_набл = {ф: set(р) for ф, р in (набл["leaders"] or {}).items()}
    все_набл = set().union(*лидеры_набл.values()) if лидеры_набл else set()

    def метка(слот):
        """Метка слота: сначала наблюдённая, потом по версии."""
        if слот is None:
            return None
        лидер = карта.get(int(слот))
        if not лидер:
            return None
        for ф, мн in лидеры_набл.items():
            if лидер in мн:
                return f"наблюдён:{ф}"
        по_версии = метки.get(лидер)
        if по_версии in (TV.МЕТКА_BAM, TV.МЕТКА_HARMONIC):
            return f"версия:{по_версии}"
        return TV.МЕТКА_ПРОЧИЕ

    в_слотах_фида: list = []
    с_преконфом: list = []
    опережения: list = []
    без_преконфа: list = []
    for п, з in покупки.items():
        м = метка(з.get("slot"))
        if not (м or "").startswith("наблюдён:") and not (м or "").startswith("версия:"):
            continue
        в_слотах_фида.append(п)
        if п in преконфы:
            с_преконфом.append(п)
            опережения.append(round((float(з["t_recv"])
                                      - float(преконфы[п]["t_recv"])) * 1000.0, 2))
        else:
            без_преконфа.append(п)

    из_: dict = {
        "okno": {"s": с_ts, "po": по_ts},
        "a_pokupki_istochnikov": {
            "vsego": len(покупки),
            "strok_v_zhurnale": пок["rows"],
            "v_slotah_fida": len(в_слотах_фида),
            "dolya_v_slotah_fida": (round(len(в_слотах_фида) / len(покупки), 4)
                                     if покупки else None),
            "nablyudyonnyh_liderov": набл["counts"],
            "dolya_slotov_nablyudyonnyh": {
                ф: доля_слотов_лидеров(мн, расписание_)
                for ф, мн in лидеры_набл.items()},
            "slotov_bez_lidera_v_raspisanii": набл["slots_without_leader"]},
        "b_s_prekonfom": {
            "n": len(с_преконфом),
            "dolya": (round(len(с_преконфом) / len(в_слотах_фида), 4)
                       if в_слотах_фида else None)},
        "v_operezhenie_ms": {
            "n": len(опережения),
            "p50": _медиана(опережения),
            "p90": _p90(опережения),
            "dolya_prekonf_ranshe": (round(len([м for м in опережения if м > 0])
                                            / len(опережения), 4)
                                      if опережения else None)},
        "g_lozhnye_prekonfy": {"n": None, "why_not": "сеть выключена"},
        "d_schyotchik": расход or {"why_not": "файл расхода не передан"},
        "p5_bez_prekonfa": {"n": len(без_преконфа), "razobrano": 0, "rows": [],
                             "why_not": "сеть выключена"},
        "prekonfov_v_okne": len(преконфы),
        "vidov_v_zhurnale": пре["kinds"],
        "liderov_nablyudyono_vsego": len(все_набл),
        "pometka_po_versii_zadana": bool((снимок.get("pravilo") or {}).get("zadano")),
    }
    if без_сети:
        return из_

    # (г) ЛОЖНЫЙ ПРЕКОНФ -- пришёл, а транзакция не села. Проверяется по цепи,
    # а не по нашей подписке: "мы её не видели" и "её нет" -- разные ответы.
    ст = сели_ли(sorted(преконфы), урл_=урл_)
    if ст["why_not"]:
        из_["g_lozhnye_prekonfy"] = {"n": None, "why_not": ст["why_not"]}
    else:
        не_село = [п for п, зн in ст["landed"].items() if zn_not_landed(зн)]
        с_ошибкой = [п for п, зн in ст["landed"].items()
                      if zn_landed_with_err(зн)]
        из_["g_lozhnye_prekonfy"] = {
            "n": len(не_село),
            "dolya": (round(len(не_село) / len(преконфы), 4) if преконфы else None),
            "sela_s_oshibkoj": len(с_ошибкой),
            "zaprosov": ст["calls"], "why_not": None}

    # (п.5) ВЕРСИЯ И СТАТИЧЕСКИЕ КЛЮЧИ у тех, по кому преконфа не было.
    строки = []
    for п in без_преконфа[:max(0, int(предел_разбора))]:
        к = статические_ключи(п, урл_=урл_)
        источник = (покупки.get(п) or {}).get("source")
        строки.append({
            "signature": п, "slot": (покупки.get(п) or {}).get("slot"),
            "source": источник,
            "version": к.get("version"),
            "istochnik_v_staticheskih": (bool(источник)
                                          and источник in (к.get("static") or [])),
            "istochnik_v_tablice": (bool(источник)
                                     and источник in (к.get("alt") or [])),
            "staticheskih_klyuchej": len(к.get("static") or []),
            "iz_tablic": len(к.get("alt") or []),
            "why_not": к.get("why_not")})
    из_["p5_bez_prekonfa"] = {
        "n": len(без_преконфа),
        "razobrano": len(строки),
        "predel_razbora": предел_разбора,
        "v_staticheskih": len([с for с in строки
                                if с["istochnik_v_staticheskih"]]),
        "tolko_v_tablice": len([с for с in строки
                                 if с["istochnik_v_tablice"]
                                 and not с["istochnik_v_staticheskih"]]),
        "versii": _счёт([с.get("version") for с in строки]),
        "rows": строки, "why_not": None}
    return из_


def zn_not_landed(зн) -> bool:
    return зн is None


def zn_landed_with_err(зн) -> bool:
    return bool(зн) and bool(зн.get("err"))


def _счёт(ряд: list) -> dict:
    из_: dict = {}
    for з in ряд or []:
        к = "legacy" if з in (None, "legacy") else str(з)
        из_[к] = из_.get(к, 0) + 1
    return из_


def лидеры_фида(преконфы: dict, карта: dict, расписание_: dict,
                 метки: dict | None = None) -> dict:
    """Личности лидеров слотов, по которым фид отдал ХОТЬ ЧТО-ТО.

    Спрос владельца 28.09 (после закрытия Harmonic): список личностей и их доля
    слотов эпохи по расписанию. Это НИЖНЯЯ ГРАНИЦА множества лидеров фида: слот,
    в котором под наш фильтр ничего не подошло, остаётся неразмеченным, и так и
    сказано числом (слотов_без_лидера).
    """
    всего_слотов = sum(len(р or []) for р in (расписание_ or {}).values())
    по_фидам: dict = {}
    без_лидера = 0
    for _, з in (преконфы or {}).items():
        слот = з.get("slot")
        if слот is None:
            continue
        лидер = карта.get(int(слот))
        if not лидер:
            без_лидера += 1
            continue
        ф = з.get("feed") or "?"
        гр = по_фидам.setdefault(ф, {})
        стр = гр.setdefault(лидер, {"identity": лидер, "soobshchenij": 0,
                                      "slotov_fida": set(),
                                      "slotov_epohi": len((расписание_ or {}).get(лидер) or []),
                                      "metka_po_versii": (метки or {}).get(лидер)})
        стр["soobshchenij"] += 1
        стр["slotov_fida"].add(int(слот))
    из_: dict = {"slotov_v_epohe": всего_слотов,
                  "slotov_bez_lidera": без_лидера, "po_fidam": {}}
    for ф, гр in sorted(по_фидам.items()):
        ряд = []
        for лидер, стр in гр.items():
            ряд.append({"identity": лидер,
                         "soobshchenij": стр["soobshchenij"],
                         "slotov_fida": len(стр["slotov_fida"]),
                         "slotov_epohi": стр["slotov_epohi"],
                         "dolya_epohi": (round(стр["slotov_epohi"] / всего_слотов, 6)
                                          if всего_слотов else None),
                         "metka_po_versii": стр["metka_po_versii"]})
        ряд.sort(key=lambda з: -з["slotov_epohi"])
        сумма = sum(з["slotov_epohi"] for з in ряд)
        из_["po_fidam"][ф] = {
            "liderov": len(ряд),
            "slotov_epohi_u_nih": сумма,
            "dolya_epohi_vmeste": (round(сумма / всего_слотов, 6)
                                    if всего_слотов else None),
            "lidery": ряд}
    return из_


def проверка_фильтра(*, журналы: list, адреса: list, окно_s: float = 600.0,
                      предел: int = 30, урл_=None) -> dict:
    """Те ли это транзакции: есть ли НАШ адрес в первых сообщениях фида.

    Решение владельца 28.09: "в первые 10 минут проверить, что сообщения --
    транзакции наших 32 адресов (доля), если нет -- остановить и доложить".
    Берётся выборка ПЕРВЫХ сообщений окна, по каждой подписи спрашивается
    транзакция, и адрес ищется отдельно среди ПОДПИСАНТОВ, среди остальных
    статических ключей и среди адресов из таблиц: по документации фильтры
    смотрят только статические ключи, и различать эти три случая -- весь смысл
    проверки.
    """
    наши = {а for а in (адреса or []) if а}
    пре = преконфы_из_журнала(журналы)["preconfs"]
    if not пре:
        return {"n": 0, "why_not": "в журналах зонда нет транзакций"}
    начало = min(float(з["t_recv"]) for з in пре.values())
    в_окне = sorted(((п, з) for п, з in пре.items()
                      if float(з["t_recv"]) <= начало + float(окно_s)),
                     key=lambda т: float(т[1]["t_recv"]))
    выборка = в_окне[:max(1, int(предел))]
    строки = []
    for п, з in выборка:
        к = статические_ключи(п, урл_=урл_)
        стат = list(к.get("static") or [])
        alt = list(к.get("alt") or [])
        n_подп = к.get("podpisantov")
        n_подп = int(n_подп) if isinstance(n_подп, int) and n_подп > 0 else 1
        наш_подписант = bool(наши & set(стат[:n_подп]))
        строки.append({
            "signature": п, "slot": з.get("slot"), "feed": з.get("feed"),
            "filters": з.get("filters"),
            "nash_podpisant": наш_подписант,
            "nash_platelshchik": bool(наши & set(стат[:1])),
            "podpisantov": n_подп,
            "nash_v_staticheskih": bool(наши & set(стат)),
            "nash_v_tablice": bool(наши & set(alt)),
            "staticheskih": len(стат), "iz_tablic": len(alt),
            "version": к.get("version"), "why_not": к.get("why_not")})
    прочитано = [с for с in строки if с["why_not"] is None]
    наших = [с for с in прочитано if с["nash_v_staticheskih"] or с["nash_v_tablice"]]
    return {"n_v_okne": len(в_окне), "razobrano": len(строки),
             "prochitano": len(прочитано),
             "nashih": len(наших),
             "dolya_nashih": (round(len(наших) / len(прочитано), 4)
                               if прочитано else None),
             "nash_podpisant": len([с for с in прочитано
                                     if с["nash_podpisant"]]),
             "nash_platelshchik": len([с for с in прочитано
                                        if с["nash_platelshchik"]]),
             "tolko_v_tablice": len([с for с in прочитано
                                      if с["nash_v_tablice"]
                                      and not с["nash_v_staticheskih"]]),
             "po_filtram": _счёт([",".join(с.get("filters") or []) or "без имени"
                                   for с in строки]),
             "okno_s": окно_s, "rows": строки, "why_not": None}


def main() -> int:
    import argparse  # noqa: PLC0415

    р = argparse.ArgumentParser(description=__doc__)
    р.add_argument("--zhurnaly", default="", help="журналы зонда через запятую")
    р.add_argument("--resheniya", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--katalog", default=os.environ.get("TRITON_STATE_DIR") or "/tmp")
    р.add_argument("--katalog-validatorov", default="",
                    help=("где лежит снимок валидаторов, если не в каталоге "
                          "зонда (его пишет и отдельный прогон в каталог "
                          "состояния исполнителя)"))
    р.add_argument("--chasov", type=float, default=24.0)
    р.add_argument("--predel-razbora", type=int, default=ПРЕДЕЛ_РАЗБОРА_КЛЮЧЕЙ)
    р.add_argument("--bez-seti", action="store_true")
    р.add_argument("--podpisi", default="",
                    help=("только проверить эти подписи по цепи: слот, села или "
                          "нет и был ли по ней преконф в журнале зонда"))
    р.add_argument("--lidery-fida", action="store_true",
                    help=("только список личностей лидеров слотов, по которым "
                          "фид отдал хоть что-то, и их доля слотов эпохи"))
    р.add_argument("--proverka-filtra", action="store_true",
                    help=("только проверка первых сообщений: есть ли наш адрес "
                          "среди статических ключей (доля)"))
    р.add_argument("--adresa-fajl", default="",
                    help="файл с нашими адресами для проверки фильтра")
    р.add_argument("--okno-s", type=float, default=600.0)
    р.add_argument("--vyborka", type=int, default=30)
    р.add_argument("--out", default="")
    а = р.parse_args()
    сейчас = time.time()
    # ТРИ ПОДПИСИ СО СЛОТОМ И ОТМЕТКОЙ ПРЕКОНФА -- просьба владельца для письма
    # Triton. Отдельный короткий путь: полный отчёт для этого не нужен.
    if а.podpisi:
        подписи = [с.strip() for с in а.podpisi.split(",") if с.strip()]
        журналы_ = [ж.strip() for ж in (а.zhurnaly or "").split(",") if ж.strip()]
        пре_ = преконфы_из_журнала(журналы_)["preconfs"] if журналы_ else {}
        ст_ = сели_ли(подписи)
        строки_ = []
        for п in подписи:
            зн = (ст_["landed"] or {}).get(п)
            строки_.append({"signature": п,
                             "slot": (зн or {}).get("slot"),
                             "sela": зн is not None,
                             "err": (зн or {}).get("err"),
                             "prekonf": ("был" if п in пре_ else "не был"),
                             "prekonf_t_recv": (пре_.get(п) or {}).get("t_recv"),
                             "prekonf_feed": (пре_.get(п) or {}).get("feed")})
        итог_ = {"podpisi": строки_, "zhurnalov": len(журналы_),
                  "prekonfov_v_zhurnalah": len(пре_),
                  "why_not": ст_["why_not"]}
        текст_ = json.dumps(итог_, ensure_ascii=False, indent=1)
        if а.out:
            Path(а.out).write_text(текст_, encoding="utf-8")
        print(текст_)
        return 0
    с_ts = сейчас - а.chasov * 3600.0
    сн = TV.прочитать(а.katalog) or {}
    каталог_вал = а.katalog
    if not сн and а.katalog_validatorov:
        сн = TV.прочитать(а.katalog_validatorov) or {}
        if сн:
            каталог_вал = а.katalog_validatorov
    расп = None
    путь_расп = Path(каталог_вал) / TV.ФАЙЛ_РАСПИСАНИЯ
    if путь_расп.exists():
        try:
            расп = (json.loads(путь_расп.read_text(encoding="utf-8")) or {}).get("schedule")
        except Exception:  # noqa: BLE001
            расп = None
    # ИМЕНА ФАЙЛОВ -- ТЕ ЖЕ, ЧТО ПИШЕТ ЗОНД (путь_суток в
    # triton_preconfs_probe): у каждого фида свой файл (два процесса на один
    # файл теряли обновления друг друга), общий остался от прежних прогонов.
    # Берём большее по каждому числу -- потраченное за сутки не обнуляется.
    расход = None
    for имя in ("triton_preconfs_day_bam.json",
                 "triton_preconfs_day_harmonic.json",
                 "triton_preconfs_day.json"):
        путь_расхода = Path(а.katalog) / имя
        if not путь_расхода.exists():
            continue
        try:
            з = json.loads(путь_расхода.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        расход = расход or {"day": з.get("day"), "bam_messages": 0,
                             "harmonic_slots": 0, "harmonic_seconds": 0.0,
                             "файлов": 0}
        расход["файлов"] += 1
        расход["bam_messages"] = max(int(расход["bam_messages"]),
                                      int(з.get("bam_messages") or 0))
        расход["harmonic_slots"] = max(int(расход["harmonic_slots"]),
                                        int(з.get("harmonic_slots") or 0))
        расход["harmonic_seconds"] = max(float(расход["harmonic_seconds"]),
                                          float(з.get("harmonic_seconds") or 0.0))
    журналы = [ж.strip() for ж in (а.zhurnaly or "").split(",") if ж.strip()]
    if а.lidery_fida:
        пре = преконфы_из_журнала(журналы)["preconfs"]
        первый = сн.get("firstSlot")
        карта = (TV.карта_слотов(расп, первый)
                 if расп and первый is not None else {})
        итог_ = лидеры_фида(пре, карта, расп or {}, сн.get("metki") or {})
        итог_["epoch"] = сн.get("epoch")
        итог_["prekonfov"] = len(пре)
        текст_ = json.dumps(итог_, ensure_ascii=False, indent=1)
        if а.out:
            Path(а.out).write_text(текст_, encoding="utf-8")
        print(текст_[:8000])
        return 0
    if а.proverka_filtra:
        адреса = []
        if а.adresa_fajl:
            адреса = [с.strip() for с
                       in Path(а.adresa_fajl).read_text(encoding="utf-8").split()
                       if с.strip()]
        итог_ = проверка_фильтра(журналы=журналы, адреса=адреса,
                                  окно_s=а.okno_s, предел=а.vyborka)
        итог_["adresov"] = len(адреса)
        текст_ = json.dumps(итог_, ensure_ascii=False, indent=1)
        if а.out:
            Path(а.out).write_text(текст_, encoding="utf-8")
        краткое_ = {к: зн for к, зн in итог_.items() if к != "rows"}
        краткое_["primery"] = (итог_.get("rows") or [])[:5]
        print(json.dumps(краткое_, ensure_ascii=False, indent=1)[:5000])
        return 0
    о = отчёт(журналы=журналы, каталог_решений=а.resheniya, снимок=сн,
               расписание_=расп, с_ts=с_ts, по_ts=сейчас,
               предел_разбора=а.predel_razbora, без_сети=а.bez_seti,
               расход=расход)
    о["snimok_validatorov"] = {"utc": сн.get("utc"), "epoch": сн.get("epoch"),
                                "why_not": сн.get("why_not"),
                                "raspisanie_est": bool(расп)}
    текст = json.dumps(о, ensure_ascii=False, indent=1)
    if а.out:
        Path(а.out).write_text(текст, encoding="utf-8")
    краткое = {к: зн for к, зн in о.items() if к != "p5_bez_prekonfa"}
    краткое["p5_bez_prekonfa"] = {к: зн for к, зн
                                   in (о.get("p5_bez_prekonfa") or {}).items()
                                   if к != "rows"}
    print(json.dumps(краткое, ensure_ascii=False, indent=1)[:6000])
    return 0


def self_test() -> int:
    """Счётная часть: доли, опережение, метка слота. Сети здесь нет."""
    пройдено = провалено = 0

    def chk(что, ок, факт=""):
        nonlocal пройдено, провалено
        if ок:
            пройдено += 1
            print(f"  [ok  ] {что}")
        else:
            провалено += 1
            print(f"  [ПРОВАЛ] {что}: {факт}")

    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as д:
        ж = Path(д) / "zond.jsonl"
        ж.write_text("\n".join(json.dumps(с, ensure_ascii=False) for с in [
            {"kind": "ping", "t_recv": 99.0},
            {"kind": "transaction", "signature": "П1", "slot": 1000,
              "t_recv": 100.0, "t_mono": 5.0, "feed": "bam"},
            {"kind": "transaction", "signature": "П1", "slot": 1000,
              "t_recv": 100.5, "feed": "bam"},
            {"kind": "transaction", "signature": "П9", "slot": 1000,
              "t_recv": 101.0, "feed": "bam"},
            {"kind": "transaction", "signature": "П7", "slot": 5000,
              "t_recv": 102.0, "feed": "harmonic"},
        ]) + "\n", encoding="utf-8")
        пре = преконфы_из_журнала([str(ж)])
        chk("у подписи берётся САМОЕ РАННЕЕ появление",
            пре["preconfs"]["П1"]["t_recv"] == 100.0, пре["preconfs"]["П1"])
        chk("ping в преконфы не попал и виды сосчитаны",
            "П0" not in пре["preconfs"] and пре["kinds"]["ping"] == 1,
            пре["kinds"])

        реш = Path(д) / "decisions.jsonl"
        реш.write_text("\n".join(json.dumps(с, ensure_ascii=False) for с in [
            {"kind": "buy", "signature": "П1", "source_slot": 1000,
              "t_recv_ts": 100.25, "source": "ИСТ1"},
            {"kind": "buy", "signature": "П2", "source_slot": 1001,
              "t_recv_ts": 100.30, "source": "ИСТ2"},
            {"kind": "buy", "signature": "П3", "source_slot": 3000,
              "t_recv_ts": 100.40, "source": "ИСТ3"},
            {"kind": "sell", "signature": "П4", "source_slot": 1000,
              "t_recv_ts": 100.50, "source": "ИСТ1"},
        ]) + "\n", encoding="utf-8")
        пок = покупки_источников(д)
        chk("продажа источника в покупки не попала (3 покупки из 4 строк)",
            len(пок["buys"]) == 3 and "П4" not in пок["buys"], пок["rows"])

        # Расписание: лидер Л1 держит слоты 1000-1001, Л2 -- 3000, Л3 -- 5000.
        снимок = {"firstSlot": 0, "metki": {"Л1": TV.МЕТКА_ПРОЧИЕ,
                                              "Л2": TV.МЕТКА_BAM,
                                              "Л3": TV.МЕТКА_ПРОЧИЕ},
                   "pravilo": {"zadano": True}}
        расп = {"Л1": [1000, 1001], "Л2": [3000], "Л3": [5000]}
        о = отчёт(журналы=[str(ж)], каталог_решений=д, снимок=снимок,
                   расписание_=расп, без_сети=True)
        chk("наблюдённый лидер найден по слоту преконфа",
            о["a_pokupki_istochnikov"]["nablyudyonnyh_liderov"] == {"bam": 1,
                                                                      "harmonic": 1},
            о["a_pokupki_istochnikov"]["nablyudyonnyh_liderov"])
        # П1 и П2 -- слоты Л1 (наблюдён как bam), П3 -- слот Л2 (метка по версии).
        chk("в слотах фида все три покупки: две наблюдённые, одна по версии",
            о["a_pokupki_istochnikov"]["v_slotah_fida"] == 3
            and о["a_pokupki_istochnikov"]["dolya_v_slotah_fida"] == 1.0,
            о["a_pokupki_istochnikov"])
        chk("с преконфом только П1 -- доля 1 из 3",
            о["b_s_prekonfom"]["n"] == 1
            and о["b_s_prekonfom"]["dolya"] == round(1 / 3, 4),
            о["b_s_prekonfom"])
        chk("опережение считается стенными часами и в миллисекундах",
            о["v_operezhenie_ms"]["p50"] == 250.0
            and о["v_operezhenie_ms"]["dolya_prekonf_ranshe"] == 1.0,
            о["v_operezhenie_ms"])
        chk("без сети ложные преконфы и п.5 -- None с причиной, а не нуль",
            о["g_lozhnye_prekonfy"]["n"] is None
            and о["p5_bez_prekonfa"]["n"] == 2,
            (о["g_lozhnye_prekonfy"], о["p5_bez_prekonfa"]["n"]))
        chk("доля слотов наблюдённых лидеров -- от всех слотов расписания",
            о["a_pokupki_istochnikov"]["dolya_slotov_nablyudyonnyh"]["bam"] == 0.5,
            о["a_pokupki_istochnikov"]["dolya_slotov_nablyudyonnyh"])
        chk("слот без лидера в расписании не метится",
            наблюдённые_лидеры({"X": {"slot": 77, "feed": "bam"}},
                                {})["slots_without_leader"] == 1)
        # ЛИДЕРЫ ФИДА: личности и доля слотов эпохи (спрос владельца 28.09).
        лид = лидеры_фида(пре["preconfs"], TV.карта_слотов(расп, 0), расп,
                           снимок["metki"])
        chk("лидеры фида: у bam один лидер и его доля слотов эпохи 0.5",
            лид["po_fidam"]["bam"]["liderov"] == 1
            and лид["po_fidam"]["bam"]["dolya_epohi_vmeste"] == 0.5,
            лид["po_fidam"]["bam"])
        chk("у harmonic свой лидер, и слоты эпохи у них не смешаны",
            лид["po_fidam"]["harmonic"]["lidery"][0]["identity"] == "Л3"
            and лид["po_fidam"]["harmonic"]["lidery"][0]["slotov_epohi"] == 1,
            лид["po_fidam"]["harmonic"])
        chk("сообщений у лидера сосчитано больше, чем слотов фида",
            лид["po_fidam"]["bam"]["lidery"][0]["soobshchenij"] == 2
            and лид["po_fidam"]["bam"]["lidery"][0]["slotov_fida"] == 1,
            лид["po_fidam"]["bam"]["lidery"][0])
        chk("проверка фильтра без журналов -- причина словами, а не ноль",
            проверка_фильтра(журналы=[], адреса=["А"])["why_not"] is not None)
        chk("ложным считается ТОЛЬКО не севшее, а севшее с ошибкой -- отдельно",
            zn_not_landed(None) and not zn_not_landed({"slot": 1})
            and zn_landed_with_err({"slot": 1, "err": "X"})
            and not zn_landed_with_err({"slot": 1, "err": None}))
    print(f"самопроверка отчёта преконфов: {пройдено}/{пройдено + провалено} пройдено")
    return 1 if провалено else 0


if __name__ == "__main__":
    raise SystemExit(self_test() if "--self-test" in sys.argv else main())
