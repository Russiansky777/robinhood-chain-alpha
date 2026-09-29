#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Таблица владельца: "решили / собрали / место минус место источника" -- сегодня против вчера.

ЗАЧЕМ (слово владельца, ночь 29.09, пункт 5): "Измерения по сегодняшним сделкам
против вчера: решили / собрали / место минус место источника; steal и загрузка
ядра детектора по часам; были ли проверки на хосте в минуты сделок. Таблица в
доклад".

Три числа владельца и откуда они берутся БЕЗ досчёта:
  * решили_мс  -- zapis.ts_decision минус zapis.signal_recv_ts: от "сигнал у нас
    в руках" до "решение принято";
  * собрали_мс -- zapis.lane_build_ms (рядом печатаются lane_sign_ms,
    lane_send_ms, lane_variants_ms, lane_nonce_ms, чтобы было видно, какой из
    подшагов съел время);
  * место_разница -- our_block_index минус source_block_index. ОТРИЦАТЕЛЬНОЕ
    значит, что в блоке мы стояли РАНЬШЕ источника.
Плюс s_plus (landed_slot минус source_slot) и наш/источника block_total, чтобы
место читалось долей блока, а не голым номером.

Только чтение: две выгрузки data/sdelki_polosy_*.json, необязательный файл с
процессором, git log этого репозитория и (на хосте) /proc/stat. Сеть не нужна.

Незнание не превращается в ноль: если числа нет -- в выводе None и названная
причина, никогда 0.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time

# Окно совпадения "проверка рядом со сделкой". Владелец просил +-2 минуты
# (ночь 29.09, пункт 5), поэтому значение вынесено в аргумент, а не зашито.
ОКНО_МИНУТ_ПО_УМОЛЧАНИЮ = 2.0

# Ключи верхнего уровня в выгрузке 29.09 записаны по-русски ("ряды"), но
# инструмент выгрузки может смениться, поэтому латинское имя тоже принимается --
# иначе проверка молча покажет ноль сделок вместо ошибки.
ИМЕНА_РЯДОВ = ("ряды", "rjady", "rows")


# --------------------------------------------------------------- чистые числа

def медиана(ряд: list):
    """Нижняя медиана: число КОНКРЕТНОЙ сделки, а не среднее двух середин.

    ЗАЧЕМ так: у чётного числа сделок (вчера 28.09 место есть только у 6 из 19)
    обычная медиана дала бы "место 230.5", которого ни у одной сделки не было, и
    владелец в докладе увидел бы придуманное число. Берём элемент с индексом
    (n-1)//2 -- он всегда настоящий. Средних в этой проверке нет вовсе: владелец
    запретил их, один выброс вроде lane_build_ms 48.624 мс (сделка 19:05:05Z
    29.09) утащил бы среднее в сторону.
    """
    ч = sorted(x for x in ряд if isinstance(x, (int, float)) and not isinstance(x, bool))
    if not ч:
        return None
    return ч[(len(ч) - 1) // 2]


def максимум(ряд: list):
    ч = [x for x in ряд if isinstance(x, (int, float)) and not isinstance(x, bool)]
    return max(ч) if ч else None


def мс_между(раньше, позже):
    """Разница двух epoch-секунд в миллисекундах, округление до 3 знаков.

    ЗАЧЕМ проверяется только тип, а не "истинность": в соседней проверке
    skorost_i_cpu.py стоит `if not a or not b: return None`, и ноль там был бы
    принят за отсутствие. Здесь ноль -- законное значение разницы (сделки с
    решением и сигналом в одну и ту же микросекунду возможны), поэтому отсутствие
    определяется ровно отсутствием числа.
    """
    if not isinstance(раньше, (int, float)) or isinstance(раньше, bool):
        return None
    if not isinstance(позже, (int, float)) or isinstance(позже, bool):
        return None
    return round((float(позже) - float(раньше)) * 1000.0, 3)


def место_разница(наше, источника):
    """our_block_index минус source_block_index; None, если хоть одного нет.

    ЗАЧЕМ отдельной функцией: знак этого числа владелец читает как ответ "мы
    раньше или позже источника", и подстановка нуля вместо отсутствующего номера
    превратила бы "мы не знаем, где мы стояли" в "мы стояли ровно там же".
    """
    if not isinstance(наше, int) or isinstance(наше, bool):
        return None
    if not isinstance(источника, int) or isinstance(источника, bool):
        return None
    return наше - источника


def разобрать_iso(строка):
    """ISO-время в epoch-секунды. Смещение обязательно учитывается.

    ЗАЧЕМ: git log --format=%cI на этой ветке отдаёт коммиты и с +00:00, и с
    +02:00 (проверено 29.09: "2026-09-29T21:16:04+02:00" -- это 19:16:04Z). Если
    смещение отбросить, часть проверок уедет на два часа и совпадение с минутой
    сделки станет ложным.
    """
    if not isinstance(строка, str) or not строка.strip():
        return None
    т = строка.strip()
    if т.endswith("Z"):
        т = т[:-1] + "+00:00"
    try:
        д = datetime.datetime.fromisoformat(т)
    except ValueError:
        return None
    if д.tzinfo is None:
        # Время без смещения в выгрузках сделок всегда UTC (поле utc), поэтому
        # доклеиваем UTC явно, а не берём часовой пояс машины, где идёт разбор.
        д = д.replace(tzinfo=datetime.timezone.utc)
    return д.timestamp()


def минуты_рядом(время_сделки, время_проверки, окно_минут: float) -> bool:
    """Попадает ли проверка в +-окно от сделки. Границу считаем попаданием.

    ЗАЧЕМ граница включена: владелец просил "+-2 минуты", и ровно 120 секунд --
    это его "две минуты", а не "чуть больше двух".
    """
    if not isinstance(время_сделки, (int, float)) or not isinstance(время_проверки, (int, float)):
        return False
    return abs(float(время_проверки) - float(время_сделки)) <= окно_минут * 60.0


# ------------------------------------------------------------------- выгрузки

def прочитать_выгрузку(путь: str) -> tuple:
    """(ряды, шапка, ошибка). Ошибка -- строка причины, не исключение."""
    if not путь or not os.path.exists(путь):
        return [], {}, f"файла нет: {путь}"
    try:
        with open(путь, "r", encoding="utf-8") as ф:
            д = json.load(ф)
    except Exception as сбой:  # noqa: BLE001
        return [], {}, f"не разобрать {путь}: {type(сбой).__name__}: {сбой}"
    if not isinstance(д, dict):
        return [], {}, f"верх файла {путь} -- не объект, а {type(д).__name__}"
    for имя in ИМЕНА_РЯДОВ:
        if isinstance(д.get(имя), list):
            шапка = {к: v for к, v in д.items() if к != имя}
            return д[имя], шапка, None
    return [], {}, f"в {путь} нет списка сделок (искали {', '.join(ИМЕНА_РЯДОВ)})"


def наш_block_total(строка: dict, зап: dict) -> tuple:
    """(число, откуда). Иначе (None, причина) -- ноль тут запрещён.

    ЗАЧЕМ вторая попытка через source_block_total: у сделок 29.09 в 15:55:59Z,
    15:23:11Z и 17:15:01Z zapis.block_total пуст (getBlock не отдался), но
    landed_slot равен source_slot -- значит блок ОДИН И ТОТ ЖЕ, и его total уже
    посчитан для источника. Это не досчёт, а то же самое число под другим именем.
    """
    т = зап.get("block_total")
    if isinstance(т, int) and not isinstance(т, bool):
        return т, "zapis.block_total"
    сел = строка.get("landed_slot")
    ист = строка.get("source_slot")
    ист_всего = зап.get("source_block_total")
    if (isinstance(сел, int) and isinstance(ист, int) and сел == ист
            and isinstance(ист_всего, int) and not isinstance(ист_всего, bool)):
        return ист_всего, "source_block_total: тот же блок (landed_slot == source_slot)"
    причина = зап.get("block_why_not") or зап.get("source_block_why_not")
    if причина:
        return None, f"block_total не получен: {str(причина)[:120]}"
    return None, "block_total не получен, причина в записи не названа"


def доля_блока(место, всего):
    if not isinstance(место, int) or not isinstance(всего, int) or всего <= 0:
        return None
    return round(float(место) / float(всего), 4)


def разобрать_сделку(строка: dict) -> dict:
    """Одна строка таблицы владельца. Все неизвестные числа -- None + причина."""
    зап = строка.get("zapis") or {}
    почему: dict = {}

    сигнал = зап.get("signal_recv_ts")
    решение = зап.get("ts_decision")
    решили = мс_между(сигнал, решение)
    if решили is None:
        # ЗАЧЕМ так подробно: в выгрузке 28.09 ts_decision нет ни у одной из 19
        # сделок, и доклад должен показать ИМЕННО это, а не пустое место.
        нет = []
        if not isinstance(сигнал, (int, float)):
            нет.append("signal_recv_ts")
        if not isinstance(решение, (int, float)):
            нет.append("ts_decision")
        почему["reshili_ms"] = "нет полей: " + ", ".join(нет) if нет else "поля не числа"

    # ЗАЧЕМ отдельное имя reshili_po_intent_ms и почему им НЕЛЬЗЯ заменить
    # решили_мс: ts_intent пишется ПОСЛЕ отправки. Сделка 29.09 16:22:49Z:
    # ts_sent 1790698969.7688391, ts_intent 1790698969.7706237 -- intent на 1.8 мс
    # позже отправки. Значит "решили по intent" -- это момент записи, а не момент
    # решения; смешивать два числа в одной колонке нельзя, но и выбросить нельзя:
    # для 28.09 это единственное, чем день вообще сравним.
    решили_по_intent = мс_между(сигнал, зап.get("ts_intent"))
    if решили_по_intent is None:
        почему["reshili_po_intent_ms"] = "нет signal_recv_ts или ts_intent"

    собрали = зап.get("lane_build_ms")
    if not isinstance(собрали, (int, float)) or isinstance(собрали, bool):
        собрали = None
        почему["sobrali_ms"] = "нет поля lane_build_ms"

    подшаги = {}
    for имя in ("lane_sign_ms", "lane_send_ms", "lane_variants_ms", "lane_nonce_ms"):
        v = зап.get(имя)
        подшаги[имя] = v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        if подшаги[имя] is None:
            почему[имя] = "нет поля в записи позиции"

    наше_место = строка.get("our_block_index")
    место_ист = строка.get("source_block_index")
    разница = место_разница(наше_место, место_ист)
    if разница is None:
        нет = []
        if not isinstance(наше_место, int):
            нет.append("our_block_index")
        if not isinstance(место_ист, int):
            нет.append("source_block_index")
        причина = зап.get("block_why_not") or зап.get("source_block_why_not")
        почему["mesto_raznica"] = ("нет полей: " + ", ".join(нет)
                                   + (f"; причина в записи: {str(причина)[:100]}" if причина else ""))

    сел, ист_слот = строка.get("landed_slot"), строка.get("source_slot")
    s_plus = None
    if isinstance(сел, int) and isinstance(ист_слот, int):
        s_plus = сел - ист_слот
    else:
        почему["s_plus"] = "нет landed_slot или source_slot (покупка не села)"

    наш_всего, откуда_всего = наш_block_total(строка, зап)
    ист_всего = зап.get("source_block_total")
    if not isinstance(ист_всего, int) or isinstance(ист_всего, bool):
        ист_всего = None
        почему["source_block_total"] = "нет поля source_block_total"
    if наш_всего is None:
        почему["our_block_total"] = откуда_всего

    время = разобрать_iso(строка.get("utc"))
    if время is None:
        почему["utc"] = f"время сделки не разобрать: {строка.get('utc')!r}"

    return {
        "utc": строка.get("utc"),
        "ts": время,
        "group": строка.get("group"),
        "istochnik": строка.get("source_name") or строка.get("source"),
        "istochnik_imya_est": bool(строка.get("source_name")),
        "size_sol": строка.get("size_sol", строка.get("sol_in")),
        "reshili_ms": решили,
        "reshili_po_intent_ms": решили_по_intent,
        "sobrali_ms": собрали,
        "podshagi_ms": подшаги,
        "mesto_raznica": разница,
        "our_block_index": наше_место if isinstance(наше_место, int) else None,
        "source_block_index": место_ист if isinstance(место_ист, int) else None,
        "our_block_total": наш_всего,
        "our_block_total_otkuda": откуда_всего if наш_всего is not None else None,
        "source_block_total": ист_всего,
        "nasha_dolya_bloka": доля_блока(наше_место if isinstance(наше_место, int) else None, наш_всего),
        "dolya_bloka_istochnika": доля_блока(место_ист if isinstance(место_ист, int) else None, ист_всего),
        "s_plus": s_plus,
        "seen_lag_ms": зап.get("seen_lag_ms"),
        "lane_pool_winner": зап.get("lane_pool_winner"),
        "stroitel": строка.get("stroitel"),
        "cid": строка.get("cid"),
        "pochemu": почему,
    }


def свод_дня(сделки: list, имя_дня: str, шапка: dict, путь: str) -> dict:
    """n / медиана / максимум по трём числам + честный счёт выброшенных строк."""
    свод = {"den": имя_дня, "fajl": путь, "sdelok_v_vygruzke": len(сделки),
            "shapka": шапка, "chisla": {}, "net_chisla": {}}
    for поле in ("reshili_ms", "reshili_po_intent_ms", "sobrali_ms", "mesto_raznica", "s_plus"):
        ряд = [с[поле] for с in сделки if с.get(поле) is not None]
        свод["chisla"][поле] = {
            "n": len(ряд),
            "mediana": медиана(ряд),
            "max": максимум(ряд),
            # ЗАЧЕМ named-причина рядом с пустой медианой: иначе в докладе "нет"
            # выглядит как сбой проверки, а это свойство выгрузки.
            "net_pochemu": (None if ряд else "ни у одной сделки дня нет этого поля"),
        }
        выброшены = [{"utc": с["utc"], "cid": с["cid"],
                      "prichina": с["pochemu"].get(поле, "поле пусто, причина не названа")}
                     for с in строки_без(сделки, поле)]
        свод["net_chisla"][поле] = {"skolko": len(выброшены), "stroki": выброшены}
    без_всех_трёх = [с["utc"] for с in сделки
                     if с.get("reshili_ms") is None and с.get("sobrali_ms") is None
                     and с.get("mesto_raznica") is None]
    свод["bez_vseh_treh_chisel"] = {"skolko": len(без_всех_трёх), "utc": без_всех_трёх}
    return свод


def строки_без(сделки: list, поле: str) -> list:
    return [с for с in сделки if с.get(поле) is None]


def сравнение(свод_сегодня: dict, свод_вчера: dict) -> dict:
    из_ = {}
    for поле in ("reshili_ms", "reshili_po_intent_ms", "sobrali_ms", "mesto_raznica", "s_plus"):
        с = свод_сегодня["chisla"][поле]
        в = свод_вчера["chisla"][поле]
        сдвиг = None
        if с["mediana"] is not None and в["mediana"] is not None:
            # ЗАЧЕМ целое остаётся целым: mesto_raznica и s_plus -- номера и слоты,
            # и "сдвиг медианы 51.0 места" читается как дробное место, которого не
            # бывает. Дробное округляем до 3 знаков, как сами миллисекунды.
            if isinstance(с["mediana"], int) and isinstance(в["mediana"], int):
                сдвиг = с["mediana"] - в["mediana"]
            else:
                сдвиг = round(float(с["mediana"]) - float(в["mediana"]), 3)
        из_[поле] = {
            "segodnja": {"n": с["n"], "mediana": с["mediana"], "max": с["max"]},
            "vchera": {"n": в["n"], "mediana": в["mediana"], "max": в["max"]},
            "sdvig_mediany": сдвиг,
            # ЗАЧЕМ причина вместо нуля: разность медиан без одной из медиан не
            # равна нулю, она не существует.
            # ЗАЧЕМ причина списком: когда медианы нет у ОБОИХ дней, склейка
            # строк дала бы "сегоднявчера" и владелец не понял бы, чего нет.
            "sdvig_pochemu": (None if сдвиг is not None else
                              "медианы нет: " + ", ".join(
                                  [имя for имя, з_ in (("сегодня", с), ("вчера", в))
                                   if з_["mediana"] is None])),
        }
    return из_


# ----------------------------------------------------------------- процессор

def steal_s_zagruzki(путь: str = "/proc/stat") -> dict:
    """Доля steal и занятости с момента загрузки машины из /proc/stat.

    ЗАЧЕМ это здесь, хотя владелец просил ПО ЧАСАМ: файла с часовой историей в
    репозитории нет (проверено 29.09: grep steal по data/*.json и
    deploy/checks/*.py не дал ни одного источника). /proc/stat -- счётчики с
    загрузки, поэтому это СРЕДНЕЕ ЗА ВСЁ ВРЕМЯ РАБОТЫ, а не часовая таблица, и
    так и подписано. На машине разбора файла может не быть -- тогда None и
    причина. Это чтение локального файла, не сеть.
    """
    из_ = {"fajl": путь, "steal_proc": None, "zanjato_proc": None, "pochemu": None,
            "chto_eto": "средняя доля с момента загрузки хоста, НЕ по часам"}
    try:
        with open(путь, "r", encoding="utf-8") as ф:
            первая = ф.readline()
    except Exception as сбой:  # noqa: BLE001
        из_["pochemu"] = f"{путь} не прочитать: {type(сбой).__name__} (разбор идёт не на хосте?)"
        return из_
    поля = первая.split()
    if not поля or поля[0] != "cpu":
        из_["pochemu"] = f"первая строка {путь} не про cpu"
        return из_
    try:
        ч = [float(x) for x in поля[1:]]
    except ValueError:
        из_["pochemu"] = f"числа в {путь} не разобрать"
        return из_
    всего = sum(ч)
    if всего <= 0:
        из_["pochemu"] = "сумма счётчиков нулевая"
        return из_
    простой = ч[3] + (ч[4] if len(ч) > 4 else 0.0)
    из_["zanjato_proc"] = round(100.0 * (всего - простой) / всего, 2)
    if len(ч) >= 8:
        из_["steal_proc"] = round(100.0 * ч[7] / всего, 3)
    else:
        # ЗАЧЕМ: на части ядер поля steal нет вовсе, и ноль здесь соврал бы
        # "гипервизор не отнимал время", хотя мы просто не знаем.
        из_["pochemu"] = "ядро не отдаёт поле steal (в строке cpu меньше 8 чисел)"
    return из_


def часы_процессора(путь: str) -> dict:
    """Таблица по часам из --cpu. Понимает два вида файла, иначе честный отказ.

    Вид 1 -- уже часовой: {"по_часам": [{"час": "2026-09-29T16", "steal_proc": ...,
    "zanjato_proc": ..., "jadro_detektora_proc": ...}, ...]} (допускаются имена
    hour/chas, steal/steal_pct, zanjato/busy, detektor/jadro_detektora_proc).
    Вид 2 -- снимок вроде data/skorost_i_cpu.json: один замер с полем
    "процессор". Его нельзя разложить по часам, и он так и помечается.
    """
    из_ = {"fajl": путь, "est_po_chasam": False, "chasy": [], "snimok": None,
            "pochemu": None}
    if not путь:
        из_["pochemu"] = "аргумент --cpu не задан"
        return из_
    if not os.path.exists(путь):
        из_["pochemu"] = f"файла нет: {путь}"
        return из_
    try:
        with open(путь, "r", encoding="utf-8") as ф:
            д = json.load(ф)
    except Exception as сбой:  # noqa: BLE001
        из_["pochemu"] = f"не разобрать {путь}: {type(сбой).__name__}: {сбой}"
        return из_

    сырые = None
    if isinstance(д, list):
        сырые = д
    elif isinstance(д, dict):
        for имя in ("по_часам", "po_chasam", "hours", "chasy"):
            if isinstance(д.get(имя), list):
                сырые = д[имя]
                break
    if сырые is not None:
        for з in сырые:
            if not isinstance(з, dict):
                continue
            из_["chasy"].append({
                "chas": первое(з, ("час", "chas", "hour")),
                "steal_proc": первое(з, ("steal_proc", "steal", "steal_pct")),
                "zanjato_proc": первое(з, ("zanjato_proc", "занято_проц", "busy", "zanjato")),
                "jadro_detektora_proc": первое(з, ("jadro_detektora_proc", "detektor",
                                                    "проц_одного_ядра", "detector_pct")),
            })
        из_["est_po_chasam"] = bool(из_["chasy"])
        if not из_["chasy"]:
            из_["pochemu"] = f"в {путь} список часов пуст"
        return из_

    if isinstance(д, dict) and isinstance(д.get("процессор"), dict):
        ц = д["процессор"]
        машина = ц.get("машина") or {}
        # ЗАЧЕМ снимок всё же показывается: он настоящий и отвечает на "чем занят
        # процессор", просто не отвечает на "по часам". Владельцу важнее увидеть
        # один честный замер, чем ничего.
        из_["snimok"] = {
            "s": д.get("с"), "po": д.get("по"), "sekund": ц.get("секунд"),
            "jader": ц.get("ядер"), "zanjato_proc": машина.get("занято_проц"),
            "load_avg": ц.get("load_avg"), "steal_proc": None,
            "steal_pochemu": "замер skorost_i_cpu.py не снимает steal вовсе",
            "processy": [{"imya": п.get("имя"), "proc_odnogo_jadra": п.get("проц_одного_ядра"),
                          "proc_vsej_mashiny": п.get("проц_всей_машины")}
                         for п in (ц.get("процессы") or []) if isinstance(п, dict)],
        }
        из_["pochemu"] = (f"{путь} -- ОДИН замер за {ц.get('секунд')} с, часовой разбивки в нём нет")
        return из_

    из_["pochemu"] = f"вид файла {путь} не опознан: нет ни списка часов, ни поля 'процессор'"
    return из_


def первое(з: dict, имена: tuple):
    for имя in имена:
        v = з.get(имя)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


# --------------------------------------------- проверки в минуты сделок (git)

ОГРАНИЧЕНИЕ_КОММИТОВ = (
    "время коммита -- КОНЕЦ прогона, а не его длительность: прогон, начавшийся "
    "за 10 минут до сделки и закончившийся в её минуту, виден как одна точка в "
    "конце. Прогон без коммита не виден вовсе, а коммит доказывает, что прогон "
    "завершился, но не что он шёл на торговом хосте."
)


def коммиты(repo: str, предел: int) -> tuple:
    """[(epoch, тема, автоматический)] всей ветки. (список, ошибка).

    ЗАЧЕМ читается ВЕСЬ лог, а не git log --since: даты коммитов на этой ветке
    идут не по порядку (смесь +00:00 и +02:00, перебазирования), а --since
    обрезает обход по дате и может потерять коммиты внутри окна. Фильтр по окну
    делается в питоне, там дата проверяется у каждой строки.
    """
    команда = ["git", "-C", repo, "log", f"-n{int(предел)}", "--format=%cI%x09%s"]
    try:
        готово = subprocess.run(команда, capture_output=True, text=True, timeout=120)
    except Exception as сбой:  # noqa: BLE001
        return [], f"git log не запустился: {type(сбой).__name__}: {сбой}"
    if готово.returncode != 0:
        return [], f"git log вернул {готово.returncode}: {(готово.stderr or '').strip()[:200]}"
    из_ = []
    не_разобрано = 0
    for строка in (готово.stdout or "").splitlines():
        iso, _, тема = строка.partition("\t")
        т = разобрать_iso(iso)
        if т is None:
            не_разобрано += 1
            continue
        тема = тема.strip()
        из_.append((т, тема, тема.endswith("[automated]")))
    ошибка = f"строк с неразобранной датой: {не_разобрано}" if не_разобрано else None
    return из_, ошибка


def проверки_в_минуты(сделки: list, все_коммиты: list, окно_минут: float,
                       только_автоматические: bool = True) -> dict:
    из_ = {"okno_minut": окно_в_число(окно_минут), "ogranichenie": ОГРАНИЧЕНИЕ_КОММИТОВ,
            "kommitov_prosmotreno": len(все_коммиты),
            "tolko_automated": только_автоматические, "po_sdelkam": []}
    всего = 0
    сделок_без_проверок = 0
    for с in сделки:
        рядом = []
        if с.get("ts") is not None:
            for т, тема, авто in все_коммиты:
                if только_автоматические and not авто:
                    continue
                if минуты_рядом(с["ts"], т, окно_минут):
                    рядом.append({"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(т)),
                                   "tema": тема,
                                   "sdvig_s": round(т - с["ts"], 1)})
        рядом.sort(key=lambda з: abs(з["sdvig_s"]))
        всего += len(рядом)
        if not рядом:
            сделок_без_проверок += 1
        из_["po_sdelkam"].append({
            "utc": с["utc"], "cid": с["cid"], "skolko": len(рядом),
            "proverki": рядом,
            "pochemu": (None if с.get("ts") is not None else
                        "время сделки не разобрать, совпадение не считалось"),
        })
    из_["vsego_sovpadenij"] = всего
    из_["sdelok_bez_proverok"] = сделок_без_проверок
    return из_


def окно_в_число(окно_минут: float) -> float:
    return round(float(окно_минут), 3)


# --------------------------------------------------------------------- печать

def ч(значение, знаков: int = 1, ширина: int = 8) -> str:
    """Число в колонку; отсутствие печатается словом "нет", а не нулём."""
    if значение is None:
        return "нет".rjust(ширина)
    if isinstance(значение, float):
        return f"{значение:.{знаков}f}".rjust(ширина)
    return str(значение).rjust(ширина)


def печать_таблицы(имя_дня: str, сделки: list, проверки: dict) -> None:
    по_cid = {з["cid"]: з for з in (проверки or {}).get("po_sdelkam", [])}
    print(f"\n=== {имя_дня}: сделок {len(сделки)} "
          f"(решили = ts_decision - signal_recv_ts, собрали = lane_build_ms, "
          f"место = наш индекс - индекс источника, минус = мы РАНЬШЕ) ===")
    заголовок = (f"{'utc':20s} {'группа':10s} {'источник':10s} {'size':>5s} "
                 f"{'решили':>8s} {'собрали':>8s} {'подпись':>7s} {'отправка':>8s} "
                 f"{'варианты':>8s} {'nonce':>6s} {'место':>7s} {'наше':>5s}"
                 f"{'/всего':>7s} {'ист.':>5s}{'/всего':>7s} {'s+':>3s} {'пров.':>5s}")
    print(заголовок)
    print("-" * len(заголовок))
    for с in сделки:
        п = с["podshagi_ms"]
        ист = (с["istochnik"] or "нет")[:10]
        print(f"{(с['utc'] or 'нет'):20s} {(с['group'] or 'нет')[:10]:10s} {ист:10s} "
              f"{ч(с['size_sol'], 3, 5)} {ч(с['reshili_ms'], 1, 8)} {ч(с['sobrali_ms'], 3, 8)} "
              f"{ч(п['lane_sign_ms'], 2, 7)} {ч(п['lane_send_ms'], 2, 8)} "
              f"{ч(п['lane_variants_ms'], 2, 8)} {ч(п['lane_nonce_ms'], 3, 6)} "
              f"{ч(с['mesto_raznica'], 0, 7)} {ч(с['our_block_index'], 0, 5)}"
              f"{ч(с['our_block_total'], 0, 7)} {ч(с['source_block_index'], 0, 5)}"
              f"{ч(с['source_block_total'], 0, 7)} {ч(с['s_plus'], 0, 3)} "
              f"{ч((по_cid.get(с['cid']) or {}).get('skolko'), 0, 5)}")


def печать_свода(свод: dict) -> None:
    print(f"\n--- свод {свод['den']} ({свод['fajl']}) ---")
    for поле, з in свод["chisla"].items():
        строка = (f"   {поле:22s} n={з['n']:3d} медиана={з['mediana']} макс={з['max']}")
        if з["net_pochemu"]:
            строка += f"   ПОЧЕМУ ПУСТО: {з['net_pochemu']}"
        print(строка)
    for поле in ("reshili_ms", "sobrali_ms", "mesto_raznica"):
        н = свод["net_chisla"][поле]
        if н["skolko"]:
            причины = sorted({с["prichina"] for с in н["stroki"]})
            print(f"   выброшено из {поле}: {н['skolko']} строк; причины: "
                  + " | ".join(p[:110] for p in причины[:3]))
    б = свод["bez_vseh_treh_chisel"]
    print(f"   строк без всех трёх чисел: {б['skolko']}"
          + (f" ({', '.join(б['utc'][:4])})" if б["utc"] else ""))


def печать_сравнения(ср: dict) -> None:
    print("\n--- сегодня против вчера (только медианы, средних нет) ---")
    print(f"   {'число':24s} {'сегодня n/медиана/макс':32s} {'вчера n/медиана/макс':32s} сдвиг")
    for поле, з in ср.items():
        с, в = з["segodnja"], з["vchera"]
        лев = f"{с['n']}/{с['mediana']}/{с['max']}"
        прав = f"{в['n']}/{в['mediana']}/{в['max']}"
        сдвиг = з["sdvig_mediany"] if з["sdvig_mediany"] is not None else f"нет -- {з['sdvig_pochemu']}"
        print(f"   {поле:24s} {лев:32s} {прав:32s} {сдвиг}")


def печать_процессора(часы: dict, steal: dict) -> None:
    print("\n--- процессор и steal по часам ---")
    if часы.get("est_po_chasam"):
        print(f"   {'час':16s} {'steal %':>8s} {'занято %':>9s} {'ядро детектора %':>18s}")
        for з in часы["chasy"]:
            print(f"   {str(з['chas'] or 'нет'):16s} {ч(з['steal_proc'], 2, 8)} "
                  f"{ч(з['zanjato_proc'], 1, 9)} {ч(з['jadro_detektora_proc'], 1, 18)}")
    else:
        print(f"   ПО ЧАСАМ НЕТ: {часы.get('pochemu')}")
        print("   что даст таблицу: прогон, который раз в час пишет steal и такты "
              "процесса детектора в один файл (счётчики /proc/stat и "
              "/proc/<pid>/stat снимает уже существующий deploy/checks/skorost_i_cpu.py, "
              "но он делает ОДИН замер и steal не снимает); готовый файл подать "
              "этой проверке через --cpu.")
        сн = часы.get("snimok")
        if сн:
            print(f"   есть только снимок {сн.get('s')} .. {сн.get('po')}: "
                  f"ядер {сн.get('jader')}, занято {сн.get('zanjato_proc')} %, "
                  f"load {сн.get('load_avg')}, steal нет ({сн.get('steal_pochemu')})")
            for п in сн.get("processy") or []:
                print(f"      {str(п.get('imya')):26s} {ч(п.get('proc_odnogo_jadra'), 1, 6)} % одного ядра")
    print(f"   steal с загрузки хоста ({steal['fajl']}): {steal['steal_proc']} %, "
          f"занято {steal['zanjato_proc']} % -- {steal['chto_eto']}"
          + (f"; ПОЧЕМУ ПУСТО: {steal['pochemu']}" if steal.get("pochemu") else ""))


def печать_проверок(проверки: dict, ошибка) -> None:
    print("\n--- были ли наши проверки в минуты сделок (коммиты прогонов) ---")
    if ошибка:
        print(f"   ОГОВОРКА git: {ошибка}")
    print(f"   окно +-{проверки['okno_minut']} мин, просмотрено коммитов "
          f"{проверки['kommitov_prosmotreno']}, совпадений {проверки['vsego_sovpadenij']}, "
          f"сделок без проверок рядом {проверки['sdelok_bez_proverok']}")
    print(f"   ОГРАНИЧЕНИЕ: {проверки['ogranichenie']}")
    for з in проверки["po_sdelkam"]:
        if з.get("pochemu"):
            print(f"   {з['utc']}: {з['pochemu']}")
            continue
        print(f"   {з['utc']}: проверок {з['skolko']}")
        for п in з["proverki"][:5]:
            print(f"      {п['utc']} ({п['sdvig_s']:+.0f} с) {п['tema'][:96]}")
        if з["skolko"] > 5:
            print(f"      ... и ещё {з['skolko'] - 5}")


# ----------------------------------------------------------------- самопроверка

def self_test() -> int:
    """Только чистая арифметика: её вранье было бы невидимым в докладе.

    Числа взяты из настоящих выгрузок, прочитанных 29.09: медианы, разницы
    времён, разница мест и совпадение минут. Правило 8 проекта не требует
    самопроверки от измерительного кода, но эти четыре функции решают, какое
    число увидит владелец, поэтому они проверяются.
    """
    сбои = []

    def равно(имя, получили, ждём):
        if получили != ждём:
            сбои.append(f"{имя}: получили {получили!r}, ждём {ждём!r}")
        else:
            print(f"   ok {имя} = {получили!r}")

    # Девять сделок 29.09, решили_мс (посчитано по ts_decision - signal_recv_ts).
    решили_29 = [12.044, 12.615, 13.892, 14.058, 15.534, 16.091, 17.926, 21.886, 24.072]
    равно("медиана решили 29.09 (n=9)", медиана(решили_29), 15.534)
    # lane_build_ms тех же девяти: выброс 48.624 не должен тянуть медиану.
    равно("медиана собрали 29.09 (n=9, выброс 48.624)",
          медиана([1.16, 0.782, 1.206, 0.923, 2.126, 1.141, 0.727, 0.766, 48.624]), 1.141)
    равно("максимум собрали 29.09",
          максимум([1.16, 0.782, 1.206, 0.923, 2.126, 1.141, 0.727, 0.766, 48.624]), 48.624)
    # Место 29.09: минус означает "мы раньше источника".
    равно("медиана место 29.09 (n=9)",
          медиана([281, 430, -41, 355, 1571, -355, 462, -385, -172]), 281)
    # Вчера 28.09 место есть только у шести сделок -- чётное n, нижняя медиана.
    равно("медиана место 28.09 (n=6, чётное)",
          медиана([230, 436, -337, 438, -366, 412]), 230)
    равно("медиана пустого ряда -- None, не 0", медиана([]), None)
    равно("медиана из одного числа", медиана([0.727]), 0.727)
    равно("медиана не считает True за число", медиана([True, False]), None)
    равно("максимум пустого ряда -- None, не 0", максимум([]), None)

    # Настоящая сделка 29.09 16:22:49Z: signal_recv_ts и ts_decision из записи.
    равно("решили_мс сделки 16:22:49Z",
          мс_между(1790698969.749061, 1790698969.7611053), 12.044)
    # Та же сделка: ts_intent ПОЗЖЕ ts_sent, потому им нельзя мерить "решили".
    равно("intent позже отправки (знак разницы)",
          мс_между(1790698969.7688391, 1790698969.7706237), 1.785)
    равно("решили_мс без ts_decision -- None", мс_между(1790698969.749061, None), None)
    равно("решили_мс без signal_recv_ts -- None", мс_между(None, 1790698969.7611053), None)
    равно("нулевая разница -- это 0.0, а не отсутствие", мс_между(1.0, 1.0), 0.0)

    равно("место 16:22:49Z (985-704, мы позже)", место_разница(985, 704), 281)
    равно("место 14:30:36Z (44-85, мы РАНЬШЕ)", место_разница(44, 85), -41)
    равно("место без нашего индекса -- None, не 0", место_разница(None, 704), None)
    равно("место без индекса источника -- None, не 0", место_разница(985, None), None)
    равно("место True не считает индексом", место_разница(True, 704), None)

    # Коммит прогона с ветки: "2026-09-29T21:16:04+02:00" -- это 19:16:04Z.
    равно("разбор коммита со смещением +02:00",
          разобрать_iso("2026-09-29T21:16:04+02:00"),
          разобрать_iso("2026-09-29T19:16:04Z"))
    равно("разбор времени сделки с Z",
          разобрать_iso("2026-09-29T16:22:49Z"), 1790698969.0)
    равно("мусор вместо времени -- None", разобрать_iso("не время"), None)

    сделка = разобрать_iso("2026-09-29T19:05:05Z")
    равно("проверка за 65 с до сделки попадает в +-2 мин",
          минуты_рядом(сделка, сделка - 65, 2.0), True)
    равно("ровно 120 с -- ещё попадание",
          минуты_рядом(сделка, сделка + 120, 2.0), True)
    равно("121 с -- уже нет", минуты_рядом(сделка, сделка + 121, 2.0), False)
    # Коммит 18:59:52Z против сделки 19:05:05Z -- 313 с, мимо окна.
    равно("коммит 18:59:52Z мимо сделки 19:05:05Z",
          минуты_рядом(сделка, разобрать_iso("2026-09-29T18:59:52Z"), 2.0), False)
    равно("без времени сделки совпадения нет",
          минуты_рядом(None, сделка, 2.0), False)

    равно("доля блока 985/1092", доля_блока(985, 1092), 0.902)
    равно("доля блока без total -- None, не 0", доля_блока(985, None), None)

    if сбои:
        print("САМОПРОВЕРКА НЕ ПРОШЛА:")
        for с in сбои:
            print("   " + с)
        return 1
    print("самопроверка: всё сошлось")
    return 0


# ---------------------------------------------------------------------- main

def main() -> int:
    корень_по_умолчанию = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    р = argparse.ArgumentParser(
        description="Таблица владельца: решили / собрали / место минус место источника, "
                    "сегодня против вчера; steal по часам; проверки в минуты сделок.")
    р.add_argument("--repo", default=корень_по_умолчанию,
                   help="корень репозитория (для git log и путей по умолчанию)")
    р.add_argument("--segodnja", default="", help="выгрузка сделок за сегодня")
    р.add_argument("--vchera", default="", help="выгрузка сделок за вчера")
    р.add_argument("--cpu", default="", help="файл с процессором/steal (по часам или снимок)")
    р.add_argument("--bez-proc-stat", action="store_true",
                   help="не читать /proc/stat (полезно, когда разбор идёт не на хосте)")
    р.add_argument("--okno-minut", type=float, default=ОКНО_МИНУТ_ПО_УМОЛЧАНИЮ,
                   help="окно совпадения проверки со сделкой, минут")
    р.add_argument("--vse-kommity", action="store_true",
                   help="считать проверками все коммиты, а не только [automated]")
    р.add_argument("--git-predel", type=int, default=20000,
                   help="сколько коммитов лога просмотреть")
    р.add_argument("--out", default="", help="куда записать JSON")
    р.add_argument("--self-test", action="store_true", help="самопроверка чистых функций")
    а = р.parse_args()

    if а.self_test:
        return self_test()

    сегодня_путь = а.segodnja or os.path.join(а.repo, "data", "sdelki_polosy_2026-09-29.json")
    вчера_путь = а.vchera or os.path.join(а.repo, "data", "sdelki_polosy_2026-09-28.json")
    out_путь = а.out or os.path.join(а.repo, "data", "reshili_sobrali_mesto.json")

    ряды_с, шапка_с, ошибка_с = прочитать_выгрузку(сегодня_путь)
    ряды_в, шапка_в, ошибка_в = прочитать_выгрузку(вчера_путь)
    if ошибка_с:
        print(f"СБОЙ: выгрузка за сегодня не прочитана: {ошибка_с}")
        return 1
    if ошибка_в:
        # ЗАЧЕМ не выход: сегодняшняя таблица нужна владельцу и без вчера, но
        # колонка "вчера" тогда обязана быть пустой с названной причиной.
        print(f"ОГОВОРКА: выгрузка за вчера не прочитана: {ошибка_в}")

    сделки_с = [разобрать_сделку(х) for х in ряды_с if isinstance(х, dict)]
    сделки_в = [разобрать_сделку(х) for х in ряды_в if isinstance(х, dict)]
    # Порядок по времени: в выгрузке 29.09 строки лежат не по возрастанию utc
    # (первая -- 16:22:49Z, третья -- 14:30:36Z), а владелец читает таблицу днём.
    сделки_с.sort(key=lambda с: с["utc"] or "")
    сделки_в.sort(key=lambda с: с["utc"] or "")

    свод_с = свод_дня(сделки_с, "сегодня", шапка_с, сегодня_путь)
    свод_в = свод_дня(сделки_в, "вчера", шапка_в, вчера_путь)
    ср = сравнение(свод_с, свод_в)

    все_коммиты, ошибка_git = коммиты(а.repo, а.git_predel)
    проверки_с = проверки_в_минуты(сделки_с, все_коммиты, а.okno_minut,
                                    только_автоматические=not а.vse_kommity)
    проверки_в = проверки_в_минуты(сделки_в, все_коммиты, а.okno_minut,
                                    только_автоматические=not а.vse_kommity)

    часы = часы_процессора(а.cpu)
    steal = ({"fajl": "/proc/stat", "steal_proc": None, "zanjato_proc": None,
              "pochemu": "чтение /proc/stat отключено ключом --bez-proc-stat",
              "chto_eto": "средняя доля с момента загрузки хоста, НЕ по часам"}
             if а.bez_proc_stat else steal_s_zagruzki())

    печать_таблицы(f"сегодня {os.path.basename(сегодня_путь)}", сделки_с, проверки_с)
    печать_таблицы(f"вчера {os.path.basename(вчера_путь)}", сделки_в, проверки_в)
    печать_свода(свод_с)
    печать_свода(свод_в)
    печать_сравнения(ср)
    печать_процессора(часы, steal)
    печать_проверок(проверки_с, ошибка_git)

    итог = {
        "snjato_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "zachem": "слово владельца, ночь 29.09, пункт 5: решили / собрали / "
                   "место минус место источника, сегодня против вчера; steal и "
                   "загрузка ядра детектора по часам; были ли проверки на хосте "
                   "в минуты сделок",
        "fajly": {"segodnja": сегодня_путь, "vchera": вчера_путь,
                   "cpu": а.cpu or None, "repo": а.repo},
        "oshibki_chtenija": {"segodnja": ошибка_с, "vchera": ошибка_в, "git": ошибка_git},
        "chto_znachat_chisla": {
            "reshili_ms": "zapis.ts_decision - zapis.signal_recv_ts, мс",
            "reshili_po_intent_ms": "zapis.ts_intent - zapis.signal_recv_ts, мс; "
                                     "ДРУГОЕ число: ts_intent пишется после ts_sent, "
                                     "мешать его с reshili_ms нельзя",
            "sobrali_ms": "zapis.lane_build_ms, мс",
            "mesto_raznica": "our_block_index - source_block_index; минус = мы раньше источника",
            "s_plus": "landed_slot - source_slot, слотов",
        },
        "segodnja": {"svod": свод_с, "sdelki": сделки_с, "proverki": проверки_с},
        "vchera": {"svod": свод_в, "sdelki": сделки_в, "proverki": проверки_в},
        "sravnenie": ср,
        "processor": {"po_chasam": часы, "steal_s_zagruzki": steal},
    }
    try:
        with open(out_путь, "w", encoding="utf-8") as ф:
            json.dump(итог, ф, ensure_ascii=False, indent=1)
        print(f"\nзаписано: {out_путь}")
    except Exception as сбой:  # noqa: BLE001
        print(f"\nСБОЙ записи {out_путь}: {type(сбой).__name__}: {сбой}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
