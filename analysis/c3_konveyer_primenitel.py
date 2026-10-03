#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ПРИМЕНИТЕЛЬ КОНВЕЙЕРА (Правило 18): из разбора Code-2 -- правка групп, и только
ДОБАВЛЕНИЕ в группу konveyer. Не больше пяти адресов в сутки.

ЧТО ЭТО ДЕЛАЕТ И ЧЕГО НЕ ДЕЛАЕТ. Вход -- файл Code-2 data/konveyer/<дата>.json с
разобранными по цепи источниками. Выход -- ПРАВКА в том самом виде, который
понимает существующий применитель групп (deploy/checks/pravka_grupp.py --plan):
{"pravki": [{"perenesti": адрес, "v": "konveyer", "pometka": ...}, ...]}. Его и
прогоняет Code-1 -- у него самопроверка денежного пути (файл перечитывается ТЕМ
ЖЕ модулем, которым его читает служба, и каждый затронутый адрес обязан отдать
ожидаемую группу). Этот модуль сам файл групп НЕ ПИШЕТ ВОВСЕ.

ТОЛЬКО ДОБАВЛЕНИЕ. Слово владельца: «не больше 5 в сутки, только из раздела
«добавить», только добавление; ничего не снимает и не переводит». Поэтому:
  * в правку уходит ТОЛЬКО perenesti в konveyer и ТОЛЬКО для адресов, которых
    нет ни в одной группе файла. Перенос из группы в группу, снятие, выключение
    и правка полей не выдаются НИКОГДА -- для них здесь нет кода;
  * адрес, который уже где-то лежит, получает отказ ПО ИМЕНИ той группы, а не
    молчаливый пропуск.

ПОЧЕМУ ОТКАЗ -- ЭТО РЕЗУЛЬТАТ, А НЕ ОШИБКА. Пять адресов в сутки -- это предел
расхода, а не план набора. Отказов в выгрузке больше, чем добавлений, и каждый
назван словами: по ним владелец видит, ЧЕМ конвейер заткнуло, и чинит причину, а
не предел.

КАК СЧИТАЕТСЯ ПРЕДЕЛ. Пятёрка -- НА СУТКИ, а не на прогон: журнал
data/konveyer/primenitel_zhurnal.json помнит, что уже выдано за эту дату, и
второй прогон того же файла не добавляет НИ ОДНОГО адреса дважды -- ни в правку,
ни в счёт предела. Повтор виден двумя путями, и оба названы: адрес уже в журнале
(правка выдана) и адрес уже в группе konveyer (правку применили).

ЧУЖАЯ ДАТА -- ОТКАЗ ЦЕЛИКОМ, А НЕ ПО РЯДУ. Дата берётся из ИМЕНИ файла
(<дата>.json) и обязана совпасть с полем «дата» КАЖДОГО ряда. Разошлось -- файл
не применяется вовсе: это значит, что в нём ряды другого дня, и брать из него
пять «свежих» было бы обманом самого себя.

БИТЫЙ ФАЙЛ -- ОТКАЗ СЛОВАМИ И КОД ВЫХОДА НЕ НОЛЬ. Пустое «ок» на битом входе --
это и есть тихая потеря: правка не выдана, а прогон зелёный.

КУСТ. У ряда есть поле «куст». Если В ЭТОМ ЖЕ ФАЙЛЕ есть другой адрес того же
куста, который УЖЕ лежит в торгующей группе, -- отказ по имени: куст уже
действует, второй его источник денег не добавит, а риск удвоит. И внутри одной
партии из куста берётся РОВНО ОДИН адрес. Чего этот модуль НЕ знает: куст адреса,
которого нет в файле Code-2 -- кусты считает он, и по чужим адресам карты у меня
нет. Это сказано в отказе словами, а не обойдено молчанием.

ПОРЯДОК -- ФАЙЛА CODE-2. Он считал медианы и он ранжировал; пересортировывать
его выдачу своим критерием значило бы спорить с разбором, не видя данных. Берутся
первые пять годных в порядке файла, и это сказано в выгрузке.

ЧЕГО ЗДЕСЬ НЕТ. Ни одной сделки, ни одного чтения сети, ни одной правки файла
групп, ни одного снятия и перевода. Прогоняет правку Code-1.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
КАТАЛОГ = КОРЕНЬ / "data" / "konveyer"

ГРУППА = "konveyer"
РАЗДЕЛ_ДОБАВИТЬ = "добавить"
ПРЕДЕЛ_В_СУТКИ = 5
ФАЙЛ_ЖУРНАЛА = "primenitel_zhurnal.json"
ВИД_ДАТЫ = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ОБЯЗАТЕЛЬНЫЕ ПОЛЯ -- СЛОВО ВЛАДЕЛЬЦА, СПИСКОМ. Нет любого -- ряд не берётся:
# считать «в плюсе» без «суток_в_плюсе» или решать по медиане, которой нет, значит
# добавить источник наугад.
ОБЯЗАТЕЛЬНЫЕ_ПОЛЯ = ("адрес", "дата", "раздел", "n", "среднее", "медиана",
                      "в_плюсе", "толпа", "медиана_1", "медиана_2",
                      "суток_в_плюсе", "последние_сутки", "типы_пулов",
                      "доля_usdc", "куст")
# Поля, которые уходят в пометку адреса: по ним потом видно, ЗА ЧТО он взят.
ПОЛЯ_ПОМЕТКИ = ("n", "медиана", "суток_в_плюсе", "в_плюсе", "доля_usdc", "куст")

WHY_NET_FAJLA = "файла входа нет"
WHY_NE_JSON = "вход не разобрался как JSON"
WHY_NET_RYADOV = "во входе нет списка рядов"
WHY_RYAD_NE_SLOVAR = "ряд не словарь"
WHY_IMYA_FAJLA = "имя файла не вида <дата>.json (ГГГГ-ММ-ДД)"
WHY_CHUZHAYA_DATA = "дата ряда не та, что у файла"
WHY_NET_POLYA = "нет обязательного поля"
WHY_NE_RAZDEL = f"раздел не «{РАЗДЕЛ_ДОБАВИТЬ}»"
WHY_TORGUET = "адрес уже в ТОРГУЮЩЕЙ группе"
WHY_LOG_ONLY = "адрес в log_only -- его уже слушают, перевод не наше дело"
WHY_OFF = "адрес в off -- выключен прямым словом, конвейер его не включает"
WHY_UZHE_V_KONVEYERE = f"адрес уже в группе {ГРУППА}"
WHY_V_GRUPPE = "адрес уже в группе"
WHY_KUST_DEJSTVUET = "куст уже действует: его источник в торгующей группе"
WHY_KUST_V_PARTII = "куст уже взят в этой же партии"
WHY_PREDEL = f"суточный предел {ПРЕДЕЛ_В_СУТКИ} исчерпан"
WHY_UZHE_VYDAN = "уже выдан в правку этим применителем"
WHY_NET_GRUPPY = f"в файле групп нет группы {ГРУППА} -- добавлять некуда"
WHY_NET_GRUPP = "файл групп не прочитался"
WHY_V_KONTEJNERE = "адрес лежит в контейнере группы"
WHY_KLYUCH_NE_ADRES = "ключ словаря рядов не совпал с полем «адрес» ряда"
WHY_RAZDELY_NE_SOSHLIS = ("«разделы» Code-2 и поле «раздел» у рядов не сошлись "
                          "по разделу «добавить»")
WHY_ADRESA_SPISKOM = ("у группы konveyer поле addresses -- СПИСОК, а применитель "
                      "групп заменит его словарём и СОТРЁТ всё, что в нём лежит")
WHY_GRUPPA_NE_TORGUET = ("группа konveyer не торгует (сняты оба признака) -- "
                         "добавление в неё денег не даёт, а сутки сгорят")
WHY_ADRES_NE_KLYUCH = "адрес не похож на ключ Solana (32 байта base58)"
WHY_DUBL = "адрес уже взят в этой же партии (дубль в файле Code-2)"
WHY_DATA_DALEKO = "дата файла далеко от сегодняшней"
WHY_PREDEL_SVERH = f"предел больше {ПРЕДЕЛ_В_СУТКИ} не берётся"
WHY_PRAVKA_SVERH_PREDELA = "в правке больше добавлений, чем осталось по журналу"
WHY_ZATROT_PRAVKU = ("правка за эту дату уже записана и не пустая -- пустой её "
                     "не затираем")
WHY_SPOR_RAZMERA = ("у группы konveyer lane_size и lane_sol РАЗНЫЕ -- загрузчик "
                    "служб снимает ей lane_trades, полоса её не торгует, и "
                    "добавление ушло бы в мёртвую группу")
WHY_GRUPPY_CODE2 = ("Code-2 назвал адрес в группах, а раздел «добавить» -- "
                    "противоречие во входе")

# КОНТЕЙНЕРЫ АДРЕСОВ -- ТЕ ЖЕ ЧЕТЫРЕ, ЧТО ЧИСТИТ ПРИМЕНИТЕЛЬ ГРУПП, И ЭТО НЕ
# ПРИДИРКА. bloom_source_groups.загрузить() кладёт в карту «по_адресу» только
# addresses, by_signal и snipers -- dropped_by_credits в неё НЕ ВХОДИТ. А
# pravka_grupp.убрать_адрес() чистит ВСЕ ЧЕТЫРЕ. Значит адрес, лежащий только в
# dropped_by_credits торгующей группы, модулю службы виден как «нигде», и моя
# правка «перенести в konveyer» ТИХО СНЯЛА БЫ его оттуда -- ровно то, чего
# владелец запретил («ничего не снимает и не переводит»). Поэтому состояние
# адреса здесь проверяется по СЫРОМУ файлу и по всем четырём контейнерам, а
# карта модуля службы берётся отдельно и только как второе мнение.
КОНТЕЙНЕРЫ = ("addresses", "by_signal", "snipers", "dropped_by_credits")


# ОКНО ДАТЫ. Файл Code-2 собирается за прошедшие сутки, поэтому годной считается
# дата сегодняшняя или одна из двух предыдущих. Зачем вообще окно: предел «пять в
# сутки» считается ПО ДАТЕ ФАЙЛА, и шесть файлов с разными датами в один реальный
# день дали бы 30 добавлений -- проверка это и нашла. Окно можно сдвинуть
# аргументом, но тогда это видно в выгрузке.
OKNO_DATY_SUTOK = 2


def segodnja_utc() -> str:
    """Сегодняшняя дата UTC. Отдельной функцией -- чтобы её можно было подменить
    в самопроверке и та не ломалась от смены календаря."""
    return time.strftime("%Y-%m-%d", time.gmtime())


def sutok_mezhdu(дата, другая) -> int | None:
    """Сколько суток между двумя датами вида ГГГГ-ММ-ДД. None -- не разобрались."""
    try:
        д = datetime.date.fromisoformat(str(дата))
        о = datetime.date.fromisoformat(str(другая))
    except (TypeError, ValueError):
        return None
    return abs((д - о).days)


def pohozh_na_klyuch(адрес) -> bool:
    """32 байта base58 -- иначе это не ключ Solana, и в файл групп его нельзя.

    Проверка не косметическая: без неё в торгующую группу ложится что угодно --
    None, число, строка с пробелами, -- и служба потом читает такую группу как
    есть. Вид ключа проверяется разбором, а не длиной строки.
    """
    if not isinstance(адрес, str) or not адрес.strip() or адрес != адрес.strip():
        return False
    try:
        from solders.pubkey import Pubkey  # noqa: PLC0415

        return len(bytes(Pubkey.from_string(адрес))) == 32
    except Exception:  # noqa: BLE001
        return False


def _модуль_групп():
    import bloom_source_groups as SG  # noqa: PLC0415
    return SG


def data_iz_imeni(put_: str | Path) -> str | None:
    """Дата из имени файла <дата>.json. None -- имя не того вида."""
    имя = Path(put_).name
    if not имя.endswith(".json"):
        return None
    осн = имя[: -len(".json")]
    return осн if ВИД_ДАТЫ.match(осн) else None


def prochitat_vhod(put_: str | Path) -> dict:
    """Файл Code-2 целиком. Битый вход -- отказ СЛОВАМИ, а не пустой список."""
    iz = {"ok": False, "why_not": None, "data": None, "ryady": None,
          "fajl": str(put_), "kustov": None}
    п = Path(put_)
    дата = data_iz_imeni(п)
    if дата is None:
        iz["why_not"] = f"{WHY_IMYA_FAJLA}: {п.name}"
        return iz
    iz["data"] = дата
    if not п.exists():
        iz["why_not"] = f"{WHY_NET_FAJLA}: {п}"
        return iz
    try:
        д = json.loads(п.read_text(encoding="utf-8"))
    except (ValueError, OSError) as сбой:
        iz["why_not"] = f"{WHY_NE_JSON}: {type(сбой).__name__}: {str(сбой)[:120]}"
        return iz
    ряды = None
    if isinstance(д, list):
        ряды = д
    elif isinstance(д, dict):
        # ИМЯ КЛЮЧА НЕ УГАДЫВАЕТСЯ НАОБУМ: берётся первый список словарей, и
        # какой именно ключ взят -- говорится в выгрузке.
        # РЯДЫ У CODE-2 -- СЛОВАРЬ {адрес: ряд}, А НЕ СПИСОК. Так выглядит его
        # первый живой файл (data/konveyer/2026-10-03.json, 48 рядов), и список
        # тоже поддерживается: форму решает файл, а не моё ожидание. У словаря
        # КЛЮЧ ОБЯЗАН СОВПАСТЬ с полем «адрес» ряда -- иначе непонятно, про кого
        # ряд, и это отказ по имени, а не выбор одного из двух.
        for ключ in ("ряды", "адреса", "источники", "rows", "строки"):
            зн = д.get(ключ)
            if isinstance(зн, list):
                ряды, iz["klyuch"] = зн, ключ
                break
            if isinstance(зн, dict) and зн:
                плохие_ключи = [к for к, р in зн.items()
                                if not isinstance(р, dict)
                                or str(р.get("адрес") or "") != str(к)]
                if плохие_ключи:
                    iz["why_not"] = (f"{WHY_KLYUCH_NE_ADRES}: {плохие_ключи[:3]}")
                    return iz
                ряды, iz["klyuch"] = list(зн.values()), ключ
                iz["rjadov_slovarjom"] = len(ряды)
                break
        if ряды is None:
            for ключ, зн in д.items():
                if isinstance(зн, list) and зн and isinstance(зн[0], dict):
                    ряды, iz["klyuch"] = зн, ключ
                    break
    if not isinstance(ряды, list) or not ряды:
        iz["why_not"] = f"{WHY_NET_RYADOV}: {п.name}"
        return iz
    плохие = [i for i, р in enumerate(ряды) if not isinstance(р, dict)]
    if плохие:
        iz["why_not"] = f"{WHY_RYAD_NE_SLOVAR}: места {плохие[:5]}"
        return iz
    # ЧУЖАЯ ДАТА -- ОТКАЗ ЦЕЛИКОМ. Один ряд не того дня значит, что файл собран
    # не за эти сутки, и брать из него «свежие» пять нельзя.
    чужие = sorted({str(р.get("дата")) for р in ряды
                    if str(р.get("дата") or "") != дата})
    if чужие:
        iz["why_not"] = (f"{WHY_CHUZHAYA_DATA}: у файла {дата}, в рядах "
                         f"{чужие[:3]}")
        return iz
    # РАЗДЕЛЫ CODE-2 -- ВТОРОЕ МНЕНИЕ, И ОНО СВЕРЯЕТСЯ. У него в файле есть и
    # «разделы» (имя раздела -> список адресов), и поле «раздел» у каждого ряда.
    # Если они расходятся, непонятно, что считать правдой, -- отказ по имени, а
    # не тихий выбор одного из двух.
    разделы = д.get("разделы") if isinstance(д, dict) else None
    if isinstance(разделы, dict) and isinstance(разделы.get(РАЗДЕЛ_ДОБАВИТЬ), list):
        по_разделам = {str(а) for а in разделы[РАЗДЕЛ_ДОБАВИТЬ]}
        по_рядам = {str(р.get("адрес")) for р in ряды
                    if str(р.get("раздел")) == РАЗДЕЛ_ДОБАВИТЬ}
        if по_разделам != по_рядам:
            iz["why_not"] = (f"{WHY_RAZDELY_NE_SOSHLIS}: в «разделы» "
                             f"{len(по_разделам)}, в рядах {len(по_рядам)}, "
                             f"расхождение "
                             f"{sorted(по_разделам ^ по_рядам)[:3]}")
            return iz
        iz["dobavit_po_razdelam"] = sorted(по_разделам)
    iz.update(ok=True, ryady=ряды)
    if isinstance(д, dict) and isinstance(д.get("кусты"), dict):
        iz["kustov"] = д["кусты"]
    return iz


def gruppy(put_: str | None = None) -> dict:
    """Что лежит в файле групп: адрес -> группа, кто торгует, есть ли konveyer."""
    iz = {"ok": False, "why_not": None, "po_adresu": None, "torgujut": None,
          "est_konveyer": None, "fajl": None}
    SG = _модуль_групп()
    import os  # noqa: PLC0415
    сохр = os.environ.get("BLOOM_SOURCE_GROUPS")
    if put_:
        os.environ["BLOOM_SOURCE_GROUPS"] = str(put_)
    try:
        д = SG.загрузить(заново=True)
    except Exception as сбой:  # noqa: BLE001
        iz["why_not"] = f"{WHY_NET_GRUPP}: {type(сбой).__name__}"
        return iz
    finally:
        if сохр is None:
            os.environ.pop("BLOOM_SOURCE_GROUPS", None)
        else:
            os.environ["BLOOM_SOURCE_GROUPS"] = сохр
    по_адресу = dict(д.get("по_адресу") or {})
    политики = dict(д.get("политики") or {})
    # СЫРОЙ ФАЙЛ -- ПО ВСЕМ ЧЕТЫРЁМ КОНТЕЙНЕРАМ (см. КОНТЕЙНЕРЫ выше).
    по_контейнерам: dict = {}
    путь_файла = д.get("file") or (str(put_) if put_ else None)
    try:
        сыро = json.loads(Path(путь_файла).read_text(encoding="utf-8"))
    except (ValueError, OSError, TypeError):
        сыро = {}
    for имя, тело in (сыро.get("groups") or {}).items():
        if not isinstance(тело, dict):
            continue
        for контейнер in КОНТЕЙНЕРЫ:
            зн = тело.get(контейнер)
            адреса = (list(зн.keys()) if isinstance(зн, dict)
                      else [(x.get("address") if isinstance(x, dict) else x)
                            for x in (зн or [])] if isinstance(зн, list) else [])
            for а in адреса:
                if а:
                    по_контейнерам.setdefault(str(а), (имя, контейнер))
    # ТОРГУЕТ ЛИ ГРУППА -- ТЕМ ЖЕ ПРАВИЛОМ, ЧТО У ПРИМЕНИТЕЛЯ ГРУПП: не торгует
    # только та, у которой СНЯТЫ ОБА признака. Своего правила здесь не заводится:
    # два правила однажды разойдутся, и разойдутся на деньгах.
    торгуют = {имя for имя, п in политики.items()
               if п.get("lane_trades") is not False
               or п.get("bloom_trades") is not False}
    # ВИД ПОЛЯ addresses У ЦЕЛЕВОЙ ГРУППЫ -- ДЕНЕЖНОЕ УСЛОВИЕ, А НЕ МЕЛОЧЬ.
    # Замер: pravka_grupp.перенести делает `if not isinstance(цель["addresses"],
    # dict): цель["addresses"] = {}` -- то есть СПИСОК он заменяет пустым
    # словарём, и все адреса, которые в группе уже лежали, ИСЧЕЗАЮТ. Код выхода
    # при этом 0, а его самопроверка денежного пути смотрит только на
    # затронутый адрес и проходит зелёной.
    тело_цели = ((сыро.get("groups") or {}).get(ГРУППА) or {})
    зн_адресов = тело_цели.get("addresses")
    iz["vid_addresses"] = ("словарь" if isinstance(зн_адресов, dict)
                           else "список" if isinstance(зн_адресов, list)
                           else "нет" if зн_адресов is None else "иной")
    iz["adresov_v_konveyere"] = (len(зн_адресов)
                                 if isinstance(зн_адресов, (dict, list)) else 0)
    # ТОРГУЕТ ЛИ ИМЕННО ПОЛОСА У ЦЕЛЕВОЙ ГРУППЫ. konveyer -- группа ПОЛОСЫ
    # (lane_size 0.01), и для неё важен lane_trades, а не общий признак «хоть
    # чем-то торгует». Отдельно называется спор размера: загрузчик служб при
    # разных lane_size и lane_sol сам снимает lane_trades (bloom_source_groups,
    # «два разных числа -- группа не торгует»), и тогда добавление уходит в
    # мёртвую группу молча.
    п_цели = политики.get(ГРУППА) or {}
    iz["konveyer_lane_torguet"] = (ГРУППА in политики
                                   and п_цели.get("lane_trades") is not False)
    iz["konveyer_spor_razmera"] = п_цели.get("lane_size_spor")
    iz["konveyer_lane_size"] = п_цели.get("lane_size")
    iz.update(ok=True, po_adresu=по_адресу, torgujut=торгуют,
              politiki=политики, est_konveyer=(ГРУППА in политики),
              po_kontejneram=по_контейнерам, fajl=путь_файла,
              konveyer_torguet=(ГРУППА in торгуют))
    # АДРЕСА, ВИДНЫЕ ТОЛЬКО В СЫРОМ ФАЙЛЕ, -- ОТДЕЛЬНЫМ ЧИСЛОМ. Это и есть
    # dropped_by_credits и прочее, чего нет в карте модуля службы.
    iz["tolko_v_fajle"] = sorted(set(по_контейнерам) - set(по_адресу))
    if not iz["est_konveyer"]:
        iz["why_not"] = WHY_NET_GRUPPY
    return iz


def zhurnal(put_: str | Path | None = None) -> dict:
    """Что уже выдано в правку -- по датам. Нет файла -- пустой журнал."""
    п = Path(put_) if put_ else (КАТАЛОГ / ФАЙЛ_ЖУРНАЛА)
    if not п.exists():
        return {"файл": str(п), "выдано": {}}
    try:
        д = json.loads(п.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        # БИТЫЙ ЖУРНАЛ НЕ МОЛЧИТ. Считать его пустым значило бы выдать те же
        # пять адресов второй раз, поэтому он называется битым и прогон встанет.
        return {"файл": str(п), "выдано": None, "битый": True}
    выд = д.get("выдано") if isinstance(д, dict) else None
    return {"файл": str(п), "выдано": (выд if isinstance(выд, dict) else {}),
            "битый": not isinstance(выд, dict)}


def pometka(ряд: dict, дата: str) -> str:
    """Пометка адреса в файле групп: за что он взят, числами ряда."""
    части = [f"{к}={ряд.get(к)}" for к in ПОЛЯ_ПОМЕТКИ if к in ряд]
    return f"конвейер {дата} (Правило 18): " + ", ".join(части)


def reshenie(ряд: dict, *, дата: str, состояние: dict) -> dict:
    """Брать ли этот ряд. Отказ -- всегда словами и всегда с именем причины."""
    iz = {"ok": False, "why_not": None, "adres": None, "kust": None}
    iz["adres"] = (str(ряд["адрес"]) if isinstance(ряд.get("адрес"), str)
                   else None)
    iz["kust"] = ряд.get("куст")
    # РАЗДЕЛ ПРОВЕРЯЕТСЯ ПЕРВЫМ, И ЭТО НЕ ПРИДИРКА К ПОРЯДКУ. У Code-2 в файле
    # лежат ВСЕ разобранные адреса, а не только раздел «добавить» (в первом
    # живом файле 47 из 48 -- «действующие не держатся», и у 15 из них числовых
    # полей нет вовсе, потому что считать было нечего). Проверь поля раньше
    # раздела -- и отчёт скажет «нет обязательного поля» про адреса, которых мы
    # и не собирались брать, а настоящая причина («не тот раздел») потеряется.
    if str(ряд.get("раздел")) != РАЗДЕЛ_ДОБАВИТЬ:
        iz["why_not"] = f"{WHY_NE_RAZDEL}: {ряд.get('раздел')!r}"
        return iz
    нет = [к for к in ОБЯЗАТЕЛЬНЫЕ_ПОЛЯ if к not in ряд]
    if нет:
        iz["why_not"] = f"{WHY_NET_POLYA}: {нет}"
        return iz
    адрес = str(ряд["адрес"])
    iz["adres"], iz["kust"] = адрес, ряд.get("куст")
    # ВИД АДРЕСА -- ПРОВЕРЯЕТСЯ, А НЕ ПРЕДПОЛАГАЕТСЯ. В правку уходит строка,
    # которую применитель групп кладёт КЛЮЧОМ в addresses; строку, не являющуюся
    # ключом Solana, он положит ровно так же охотно, самопроверка денежного пути
    # её пропустит (она сверяет группу по тому же ключу), и в живом файле групп
    # навсегда останется мусор, который служба отнесёт к ГРУППЕ ПО УМОЛЧАНИЮ.
    if not pohozh_na_klyuch(адрес):
        iz["why_not"] = f"{WHY_ADRES_NE_KLYUCH}: {адрес[:16]!r}"
        return iz
    # ДУБЛЬ ВНУТРИ ОДНОГО ФАЙЛА. Ряды приходят и СПИСКОМ (вид словарём ключи
    # сам не повторит, список -- повторит), и тогда один адрес дал бы две
    # операции в правке и съел бы две единицы суточного предела за себя одного.
    if адрес in состояние["berjom"]:
        iz["why_not"] = WHY_DUBL
        return iz
    if str(ряд.get("дата")) != дата:
        iz["why_not"] = f"{WHY_CHUZHAYA_DATA}: {ряд.get('дата')} против {дата}"
        return iz
    # ЕСЛИ CODE-2 САМ НАЗВАЛ ГРУППЫ АДРЕСА -- а раздел «добавить», это
    # противоречие в его же файле, и брать такой адрес нельзя.
    гр_c2 = ряд.get("группы")
    if isinstance(гр_c2, (list, tuple)) and гр_c2:
        iz["why_not"] = f"{WHY_GRUPPY_CODE2}: {list(гр_c2)[:3]}"
        return iz
    # СОСТОЯНИЕ АДРЕСА -- ПО ОБЕИМ КАРТАМ. Карта модуля службы (po_adresu) и
    # карта сырого файла по всем контейнерам (po_kontejneram) обязаны сойтись;
    # где они расходятся, берётся СЫРОЙ ФАЙЛ -- потому что правку применяют по
    # нему, и снять оттуда можно то, чего служба не видит.
    в_файле = (состояние.get("po_kontejneram") or {}).get(адрес)
    гр = состояние["po_adresu"].get(адрес) or (в_файле[0] if в_файле else None)
    if в_файле and not состояние["po_adresu"].get(адрес):
        iz["why_not"] = (f"{WHY_V_KONTEJNERE} {в_файле[0]}.{в_файле[1]} -- "
                         f"модуль службы его не видит, а правка СНЯЛА БЫ его "
                         f"оттуда")
        return iz
    if гр == ГРУППА:
        iz["why_not"] = WHY_UZHE_V_KONVEYERE
        return iz
    if гр in состояние["torgujut"]:
        iz["why_not"] = f"{WHY_TORGUET}: {гр}"
        return iz
    if гр == "log_only":
        iz["why_not"] = WHY_LOG_ONLY
        return iz
    if гр == "off":
        iz["why_not"] = WHY_OFF
        return iz
    if гр:
        iz["why_not"] = f"{WHY_V_GRUPPE}: {гр}"
        return iz
    if адрес in состояние["vydano_za_sutki"]:
        iz["why_not"] = f"{WHY_UZHE_VYDAN}: {дата}"
        return iz
    куст = ряд.get("куст")
    действующий = (состояние["kust_torguet"] or {}).get(куст)
    if куст is not None and действующий:
        iz["why_not"] = (f"{WHY_KUST_DEJSTVUET}: куст {куст!r}, источник "
                         f"{str(действующий[0])[:8]} в группе {действующий[1]}")
        return iz
    взят = (состояние["kust_vzjat"] or {}).get(куст)
    if куст is not None and взят:
        iz["why_not"] = f"{WHY_KUST_V_PARTII}: куст {куст!r}, адрес {взят[:8]}"
        return iz
    if len(состояние["berjom"]) >= состояние["predel_ostalsja"]:
        iz["why_not"] = (f"{WHY_PREDEL}: за сутки уже "
                         f"{len(состояние['vydano_za_sutki'])}, в этой партии "
                         f"{len(состояние['berjom'])}")
        return iz
    iz["ok"] = True
    return iz


def primenitel(*, vhod: dict, gr: dict, zh: dict,  # noqa: C901, PLR0912
               predel: int = ПРЕДЕЛ_В_СУТКИ,
               segodnja: str | None = None) -> dict:
    """Решение по всему файлу: кого добавить, кому отказ и почему."""
    iz = {"ok": False, "why_not": None, "data": vhod.get("data"),
          "fajl": vhod.get("fajl"), "dobavleno": None, "otkazy": None,
          "svod": None, "predel": predel, "predel_ostalsja": None}
    # ПРЕДЕЛ СВЕРХ ПЯТИ НЕ БЕРЁТСЯ ВОВСЕ. Пятёрка -- слово владельца, а не
    # значение по умолчанию: --predel 50 на ключе командной строки обошёл бы его
    # молча, и обошёл бы ОДНИМ прогоном. Понижать предел можно (это осторожнее),
    # повышать -- нет.
    try:
        предел_ч = int(predel)
    except (TypeError, ValueError):
        предел_ч = -1
    if предел_ч < 0 or предел_ч > ПРЕДЕЛ_В_СУТКИ:
        iz["why_not"] = f"{WHY_PREDEL_SVERH}: просили {predel!r}"
        return iz
    if not vhod.get("ok"):
        iz["why_not"] = vhod.get("why_not")
        return iz
    if not gr.get("ok") or not gr.get("est_konveyer"):
        iz["why_not"] = gr.get("why_not") or WHY_NET_GRUPPY
        return iz
    # ВИД ПОЛЯ addresses У ЦЕЛЕВОЙ ГРУППЫ -- ДЕНЕЖНОЕ УСЛОВИЕ, НЕ МЕЛОЧЬ.
    # Замер по deploy/checks/pravka_grupp.py:225: `if not isinstance(
    # цель.get("addresses"), dict): цель["addresses"] = {}` -- СПИСОК он
    # заменяет пустым словарём, и каждый адрес, который в группе уже лежал,
    # ИСЧЕЗАЕТ. Код выхода при этом 0, а его самопроверка денежного пути
    # смотрит только на затронутый адрес и проходит зелёной. Проверено своим
    # прогоном: было [{AAA1},{BBB2}] -> стало {"NEW3": ...}.
    if (gr.get("vid_addresses") not in ("словарь", "нет")
            and gr.get("adresov_v_konveyere")):
        iz["why_not"] = (f"{WHY_ADRESA_SPISKOM}: вид {gr.get('vid_addresses')}, "
                         f"в нём {gr.get('adresov_v_konveyere')} адресов")
        return iz
    # ЦЕЛЕВАЯ ГРУППА ОБЯЗАНА ТОРГОВАТЬ ПОЛОСОЙ. Иначе добавление -- не шаг
    # конвейера, а запись в мёртвую группу: отчёт зелёный, адресов прибыло,
    # денег нет. Спор размера называется отдельной причиной, потому что чинится
    # он иначе -- одним полем правды в файле групп.
    if gr.get("konveyer_spor_razmera"):
        iz["why_not"] = f"{WHY_SPOR_RAZMERA}: {gr['konveyer_spor_razmera']}"
        return iz
    if not gr.get("konveyer_lane_torguet"):
        iz["why_not"] = (f"{WHY_GRUPPA_NE_TORGUET}: lane_trades="
                         f"{(gr.get('politiki') or {}).get(ГРУППА, {}).get('lane_trades')!r}")
        return iz
    if zh.get("битый"):
        iz["why_not"] = f"журнал {zh.get('файл')} битый -- повтор не отличить"
        return iz
    дата = vhod["data"]
    # ДАТА ФАЙЛА -- ОКОЛО СЕГОДНЯШНЕЙ. Своя дата у файла уже сверена с рядами,
    # но файл за прошлый месяц сверится сам с собой и пройдёт: журнал за ту дату
    # пуст, значит снова пять «свежих» адресов, посчитанных по мёртвому окну.
    сег = segodnja or segodnja_utc()
    разница = sutok_mezhdu(дата, сег)
    if разница is None or разница > OKNO_DATY_SUTOK:
        iz["why_not"] = (f"{WHY_DATA_DALEKO}: у файла {дата}, сегодня {сег}, "
                         f"разница {разница} суток, окно {OKNO_DATY_SUTOK}")
        return iz
    выдано = list((zh.get("выдано") or {}).get(дата) or [])
    осталось = max(0, int(predel) - len(выдано))
    iz["predel_ostalsja"] = осталось
    # КАРТА КУСТОВ -- ТОЛЬКО ИЗ ФАЙЛА CODE-2. По адресам, которых в файле нет,
    # куста я не знаю: кусты считает он. Поэтому «куст действует» проверяется по
    # тем адресам файла, что уже лежат в торгующих группах, плюс по карте
    # «кусты», если Code-2 её прислал.
    куст_торгует: dict = {}
    for р in vhod["ryady"]:
        if not isinstance(р, dict):
            continue
        а, к = str(р.get("адрес") or ""), р.get("куст")
        г = gr["po_adresu"].get(а)
        if к is not None and г and г in gr["torgujut"]:
            куст_торгует.setdefault(к, (а, г))
    for к, адреса in (vhod.get("kustov") or {}).items():
        if к in куст_торгует or not isinstance(адреса, (list, tuple)):
            continue
        for а in адреса:
            г = gr["po_adresu"].get(str(а))
            if г and г in gr["torgujut"]:
                куст_торгует[к] = (str(а), г)
                break
    состояние = {"po_adresu": gr["po_adresu"], "torgujut": gr["torgujut"],
                 "po_kontejneram": gr.get("po_kontejneram") or {},
                 "vydano_za_sutki": set(выдано), "kust_torguet": куст_торгует,
                 "kust_vzjat": {}, "berjom": [], "predel_ostalsja": осталось}
    добавить, отказы = [], []
    for номер, р in enumerate(vhod["ryady"], 1):
        реш = reshenie(р, дата=дата, состояние=состояние)
        if not реш["ok"]:
            отказы.append({"место": номер, "адрес": реш.get("adres"),
                           "куст": реш.get("kust"), "почему": реш["why_not"]})
            continue
        состояние["berjom"].append(реш["adres"])
        if реш.get("kust") is not None:
            состояние["kust_vzjat"][реш["kust"]] = реш["adres"]
        добавить.append({"адрес": реш["adres"], "куст": реш.get("kust"),
                         "место_в_файле": номер,
                         "пометка": pometka(р, дата),
                         "числа": {к: р.get(к) for к in ПОЛЯ_ПОМЕТКИ if к in р}})
    по_причинам: dict = {}
    for о in отказы:
        имя = str(о["почему"]).split(":")[0]
        по_причинам[имя] = по_причинам.get(имя, 0) + 1
    # КУСТ, КОТОРОГО CODE-2 НЕ НАЗВАЛ, -- ЧИСЛОМ В СВОДКЕ, А НЕ МОЛЧАНИЕМ.
    # В первом живом файле куст null у ВСЕХ 48 рядов (кустов_убрано 0), то есть
    # мой разбор куста по этому файлу не делает НИЧЕГО, и от двух источников
    # одного куста в одной партии защищает только его собственный счёт. Отказом
    # это не делается: так я отверг бы единственный живой адрес, который Code-1
    # уже применил в бою. Но молчать об этом нельзя -- число стоит в сводке.
    без_куста = sum(1 for д in добавить if д.get("куст") is None)
    # ПРЕДЕЛ, ОСТАВШИЙСЯ ПОСЛЕ ЭТОЙ ПАРТИИ, -- ОТДЕЛЬНЫМ ЧИСЛОМ. До партии он
    # ничего читателю не говорит: правка на руках, и вопрос у него один --
    # сколько можно ещё СЕГОДНЯ.
    осталось_после = max(0, осталось - len(добавить))
    iz["predel_ostalsja"] = осталось_после
    iz.update(ok=True, dobavleno=добавить, otkazy=отказы,
              svod={"рядов": len(vhod["ryady"]), "добавлено": len(добавить),
                    "отказов": len(отказы), "по_причинам": по_причинам,
                    "предел": predel, "выдано_за_сутки_до": len(выдано),
                    "предел_остался_до": осталось,
                    "предел_остался": осталось_после,
                    "кустов_не_названо": без_куста,
                    "сегодня": сег})
    return iz


def pravka(реш: dict) -> dict:
    """ПРАВКА для deploy/checks/pravka_grupp.py --plan. Только добавление."""
    правки = [{"perenesti": д["адрес"], "v": ГРУППА, "pometka": д["пометка"]}
              for д in (реш.get("dobavleno") or [])]
    return {"что": (f"конвейер {реш.get('data')}: добавление в группу {ГРУППА} "
                    f"(Правило 18, применитель Code-3)"),
            # ДАТА -- ПОЛЕМ, А НЕ ТОЛЬКО В ИМЕНИ ФАЙЛА. По ней сверка находит
            # запись журнала за эти сутки; имя файла переименовывается одной
            # командой, поле -- нет.
            "дата": реш.get("data"),
            "откуда": реш.get("fajl"),
            "как_применить": ("python3 deploy/checks/pravka_grupp.py --file "
                              "<файл групп> --plan <этот файл>"),
            "pravki": правки}


def zapisat(реш: dict, *, kat: str | Path | None = None,
            zhurnal_put: str | Path | None = None,
            pisat_zhurnal: bool = True) -> dict:
    """Записать правку, выгрузку и журнал. Файл групп НЕ трогается."""
    к = Path(kat) if kat else КАТАЛОГ
    к.mkdir(parents=True, exist_ok=True)
    дата = реш.get("data")
    п_правки = к / f"pravka_{дата}.json"
    п_отчёта = к / f"primenitel_{дата}.json"
    из_ = {"ok": False, "why_not": None, "pravka": str(п_правки),
           "otchjot": str(п_отчёта), "zhurnal": None}
    новая = pravka(реш)
    # ПУСТОЙ ПРАВКОЙ НЕ ЗАТИРАТЬ НЕПУСТУЮ. Второй прогон того же файла за те же
    # сутки даёт НОЛЬ добавлений -- и это правильно (журнал помнит). Но запись
    # такой правки поверх прежней стёрла бы ровно тот файл, который Code-1 ещё
    # не применил или применил и держит как запись о сделанном, а код выхода
    # остался бы нулевым. Тихая потеря правки -- то же, что тихая потеря денег.
    if not новая["pravki"] and п_правки.exists():
        try:
            старая = json.loads(п_правки.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            старая = {}
        if (старая.get("pravki") if isinstance(старая, dict) else None):
            из_["why_not"] = (f"{WHY_ZATROT_PRAVKU}: {п_правки.name}, в ней "
                              f"{len(старая['pravki'])} добавлений")
            return из_
    п_правки.write_text(json.dumps(новая, ensure_ascii=False, indent=1)
                        + "\n", encoding="utf-8")
    п_отчёта.write_text(json.dumps(реш, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    из_["ok"] = True
    if pisat_zhurnal and реш.get("dobavleno"):
        # ЖУРНАЛ НЕ ЕЗДИТ ЗА --kat. Он -- счёт суточного предела; положи его
        # рядом с правкой, и один прогон с --kat /tmp/что-нибудь выдал бы ещё
        # пять адресов сверх пятёрки, ничего не нарушив формально.
        п_ж = Path(zhurnal_put) if zhurnal_put else (КАТАЛОГ / ФАЙЛ_ЖУРНАЛА)
        п_ж.parent.mkdir(parents=True, exist_ok=True)
        ж = zhurnal(п_ж)
        если = dict(ж.get("выдано") or {})
        было = list(если.get(дата) or [])
        если[дата] = было + [д["адрес"] for д in реш["dobavleno"]
                             if д["адрес"] not in было]
        п_ж.write_text(json.dumps(
            {"что": ("что применитель конвейера УЖЕ ВЫДАЛ в правку, по датам: "
                     "по нему считается суточный предел и ловится повтор файла"),
             "обновлено_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "выдано": если}, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
        из_["zhurnal"] = str(п_ж)
    return из_


# --------------------------------------------- сверка правки перед применением

WHY_PRAVKA_NE_JSON = "правка не разобралась как JSON"
WHY_PRAVKA_PUSTA = "в правке нет списка pravki"
WHY_PRAVKA_NE_DOBAVLENIE = "в правке есть операция, которая НЕ добавление"
WHY_PRAVKA_USTARELA = "адрес правки за это время появился в группах"
WHY_PRAVKA_BEZ_DATY = ("у правки нет даты -- ни полем, ни в имени файла; остаток "
                       "суточного предела по журналу не посчитать")


def sverit_pravku(put_pravki: str | Path, *,  # noqa: C901, PLR0911, PLR0912
                  gruppy_put: str | None = None,
                  zhurnal_put: str | Path | None = None) -> dict:
    """ГОДНА ЛИ ПРАВКА ПРЯМО СЕЙЧАС. Прогонять ПЕРЕД pravka_grupp.py.

    ЗАЧЕМ ОТДЕЛЬНЫМ ШАГОМ. Безопасность правки держится на одном: КАЖДЫЙ её
    адрес лежит в файле групп нигде, и тогда perenesti -- чистое добавление. Но
    файл групп живой: между тем, как правка записана, и тем, как Code-1 её
    применил, адрес мог попасть в группу (своим прогоном, правкой владельца,
    чем угодно). Тогда та же операция perenesti СНИМЕТ его оттуда -- ровно то,
    что запрещено. Сверка перечитывает файл групп и говорит, годна ли правка
    СЕЙЧАС; не годна -- применять нельзя, надо прогнать применитель заново.

    Проверяется и вид правки: ни одной операции, кроме добавления в konveyer.
    """
    iz = {"ok": False, "why_not": None, "pravok": None, "godnyh": None,
          "ustarelo": None, "chuzhie_operacii": None, "fajl": str(put_pravki),
          "data": None, "vydano_za_sutki": None, "predel_ostalsja": None}
    п = Path(put_pravki)
    if not п.exists():
        iz["why_not"] = f"{WHY_NET_FAJLA}: {п}"
        return iz
    try:
        д = json.loads(п.read_text(encoding="utf-8"))
    except (ValueError, OSError) as сбой:
        iz["why_not"] = f"{WHY_PRAVKA_NE_JSON}: {type(сбой).__name__}"
        return iz
    правки = д.get("pravki") if isinstance(д, dict) else None
    if not isinstance(правки, list) or not правки:
        iz["why_not"] = f"{WHY_PRAVKA_PUSTA}: {п.name}"
        return iz
    iz["pravok"] = len(правки)
    чужие = [о for о in правки
             if not isinstance(о, dict)
             or set(о) != {"perenesti", "v", "pometka"}
             or о.get("v") != ГРУППА or not о.get("perenesti")]
    iz["chuzhie_operacii"] = чужие
    if чужие:
        iz["why_not"] = f"{WHY_PRAVKA_NE_DOBAVLENIE}: {len(чужие)} из {len(правки)}"
        return iz
    гр = gruppy(gruppy_put)
    if not гр.get("ok"):
        iz["why_not"] = гр.get("why_not") or WHY_NET_GRUPP
        return iz
    if not гр.get("est_konveyer"):
        iz["why_not"] = WHY_NET_GRUPPY
        return iz
    # ТЕ ЖЕ ДЕНЕЖНЫЕ УСЛОВИЯ, ЧТО В ПРИМЕНИТЕЛЕ, И ПРОВЕРЯЮТСЯ ОНИ ЗАНОВО.
    # Применитель смотрел на файл групп ТОГДА, сверка смотрит СЕЙЧАС: вид
    # addresses у группы konveyer мог стать списком (её правил кто-то ещё), и
    # тогда применитель групп сотрёт из неё всё, что уже лежало.
    if (гр.get("vid_addresses") not in ("словарь", "нет")
            and гр.get("adresov_v_konveyere")):
        iz["why_not"] = (f"{WHY_ADRESA_SPISKOM}: вид {гр.get('vid_addresses')}, "
                         f"в нём {гр.get('adresov_v_konveyere')} адресов")
        return iz
    if гр.get("konveyer_spor_razmera"):
        iz["why_not"] = f"{WHY_SPOR_RAZMERA}: {гр['konveyer_spor_razmera']}"
        return iz
    if not гр.get("konveyer_lane_torguet"):
        iz["why_not"] = f"{WHY_GRUPPA_NE_TORGUET}: сверка перед применением"
        return iz
    # ОСТАТОК СУТОЧНОГО ПРЕДЕЛА -- ПО ЖУРНАЛУ, А НЕ ПО ЧИСЛУ ОПЕРАЦИЙ В ПРАВКЕ.
    # Правку можно написать руками, и тогда «только добавление в konveyer» будет
    # соблюдено, а пятёрка -- нет. Адреса, которые этот применитель уже выдал за
    # эти сутки, в журнале лежат, и сверх предела они не считаются дважды.
    дата = д.get("дата") if isinstance(д, dict) else None
    if not дата:
        м = re.search(r"\d{4}-\d{2}-\d{2}", п.name)
        дата = м.group(0) if м else None
    iz["data"] = дата
    if not дата:
        iz["why_not"] = f"{WHY_PRAVKA_BEZ_DATY}: {п.name}"
        return iz
    ж = zhurnal(zhurnal_put)
    if ж.get("битый"):
        iz["why_not"] = (f"журнал {ж.get('файл')} битый -- остаток суточного "
                         f"предела не посчитать")
        return iz
    выдано = list((ж.get("выдано") or {}).get(дата) or [])
    осталось = max(0, ПРЕДЕЛ_В_СУТКИ - len(выдано))
    сверх = [о for о in правки if str(о["perenesti"]) not in выдано]
    iz["vydano_za_sutki"], iz["predel_ostalsja"] = len(выдано), осталось
    if len(сверх) > осталось:
        iz["why_not"] = (f"{WHY_PRAVKA_SVERH_PREDELA}: в правке {len(правки)}, "
                         f"из них не из журнала {len(сверх)}, а за {дата} "
                         f"осталось {осталось} (выдано {len(выдано)} из "
                         f"{ПРЕДЕЛ_В_СУТКИ})")
        return iz
    устарело = []
    for о in правки:
        адрес = str(о["perenesti"])
        где = гр["po_adresu"].get(адрес)
        контейнер = (гр.get("po_kontejneram") or {}).get(адрес)
        if где or контейнер:
            устарело.append({"адрес": адрес, "группа": где,
                             "контейнер": (f"{контейнер[0]}.{контейнер[1]}"
                                           if контейнер else None)})
    iz["ustarelo"] = устарело
    iz["godnyh"] = len(правки) - len(устарело)
    if устарело:
        iz["why_not"] = (f"{WHY_PRAVKA_USTARELA}: {len(устарело)} из "
                         f"{len(правки)} -- применять нельзя, прогнать "
                         f"применитель заново")
        return iz
    iz["ok"] = True
    return iz


# ------------------------------------------------------------- самопроверка

# Число проверок объявлено заранее: меньше -- значит что-то пропущено молча.
ZHDEM_PROVEROK = 86
# АДРЕСА САМОПРОВЕРКИ -- ВЫДУМАННЫЕ, И ЭТО СКАЗАНО ВСЛУХ. Они выводятся из семени
# числом, в цепи их нет, и ни один из них не попадает ни в один файл репозитория:
# самопроверка работает в своём временном каталоге и убирает его за собой.
СЕМЯ_ПРОВЕРКИ = b"konveyer-primenitel-samoproverka"
# ЖИВОЙ ФАЙЛ CODE-2 -- ПЕРВЫЙ, И ОН ПРОВЕРЯЕТСЯ ЦЕЛИКОМ. Числа -- замер по
# файлу, скопированному байт в байт из ветки claude/podbivka.
FAJL_ZHIVOJ = "2026-10-03.json"
FAJL_GRUPP_REPO = "sources_2026-09-25.json"
ZHDEM_ZHIVOGO = {"рядов": 48, "добавлено": 1, "не_тот_razdel": 47,
                 "добавить": ["ALL1V7x5gH59qUHdpAK5M1js9YMGhb94xarqoi8PTKqy"],
                 # КУСТ У ВСЕХ 48 РЯДОВ -- null (кустов_убрано 0 у Code-2). Это
                 # замер, а не догадка: значит мой разбор куста по этому файлу
                 # не делает НИЧЕГО, и число стоит в сводке словами.
                 "кустов_не_названо": 1}


def _адрес(номер: int) -> str:
    import hashlib  # noqa: PLC0415

    from solders.pubkey import Pubkey  # noqa: PLC0415
    сыр = hashlib.sha256(СЕМЯ_ПРОВЕРКИ + bytes([номер])).digest()
    return str(Pubkey.from_bytes(сыр))


def _ряд(номер: int, *, дата: str, раздел: str = РАЗДЕЛ_ДОБАВИТЬ,
         куст=None, адрес: str | None = None) -> dict:
    """Ряд входа со ВСЕМИ обязательными полями. Числа условные -- это фикстура."""
    return {"адрес": адрес or _адрес(номер), "дата": дата, "раздел": раздел,
            "n": 20 + номер, "среднее": 0.11, "медиана": 0.08,
            "в_плюсе": 0.6, "толпа": 3, "медиана_1": 0.07, "медиана_2": 0.09,
            "суток_в_плюсе": 3, "последние_сутки": 0.05,
            "типы_пулов": ["pump_amm", "cpmm"], "доля_usdc": 0.2,
            "куст": (куст if куст is not None else f"kust{номер}")}


def _файл_групп(каталог: Path, *, с_конвейером: bool = True) -> Path:
    """Файл групп для самопроверки: торгующая, log_only, off и konveyer."""
    гр = {
        "batch5": {"lane_size": 0.3, "lane_trades": True, "bloom_trades": False,
                   "addresses": {_адрес(200): "торгующий источник"}},
        "log_only": {"lane_size": None, "lane_trades": False,
                     "bloom_trades": False,
                     "addresses": {_адрес(201): "только лог"}},
        "off": {"lane_size": None, "lane_trades": False, "bloom_trades": False,
                "addresses": {_адрес(202): "выключен"}},
    }
    # АДРЕС ТОЛЬКО В dropped_by_credits -- ЛОВУШКА, И ОНА ПРОВЕРЯЕТСЯ. Модуль
    # службы его в карту не кладёт, а применитель групп из этого контейнера
    # адрес СНИМАЕТ.
    гр["batch5"]["dropped_by_credits"] = [{"address": _адрес(204),
                                           "почему": "кредиты"}]
    if с_конвейером:
        гр[ГРУППА] = {"lane_size": 0.01, "lane_trades": True,
                      "bloom_trades": False,
                      "addresses": {_адрес(203): "уже в конвейере"}}
    каталог.mkdir(parents=True, exist_ok=True)
    п = каталог / "gruppy.json"
    п.write_text(json.dumps({"generated_utc": "самопроверка", "groups": гр},
                            ensure_ascii=False, indent=1), encoding="utf-8")
    return п


def self_test() -> int:  # noqa: C901, PLR0912, PLR0915
    import io as io_mod  # noqa: PLC0415
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    было, плохо = 0, 0

    def chk(имя, усл, факт=None):
        nonlocal было, плохо
        было += 1
        if усл:
            print(f"  ok   {имя}")
        else:
            плохо += 1
            print(f" ПЛОХО {имя} -- {факт!r}")

    print("c3_konveyer_primenitel: самопроверка")
    врем = Path(tempfile.mkdtemp(prefix="konveyer-proverka-"))
    try:
        ДАТА = "2026-10-04"
        П_Ж = врем / ФАЙЛ_ЖУРНАЛА

        def прим(**кв):
            """primenitel с подставленным «сегодня». САМОПРОВЕРКА НЕ СМОТРИТ НА
            КАЛЕНДАРЬ: иначе она позеленеет сегодня и покраснеет через трое
            суток сама по себе, а окно даты проверяется чистой функцией."""
            кв.setdefault("segodnja", (кв.get("vhod") or {}).get("data") or ДАТА)
            return primenitel(**кв)
        п_гр = _файл_групп(врем)
        гр = gruppy(п_гр)
        chk("файл групп самопроверки читается тем же модулем, что у службы",
            гр["ok"], гр.get("why_not"))
        chk("торгующими названы ровно те группы, у которых признак не снят",
            гр["torgujut"] == {"batch5", ГРУППА}, sorted(гр["torgujut"] or []))
        chk(f"группа {ГРУППА} в файле есть", гр["est_konveyer"], None)

        # --------------------------------------------- 1. предел пяти в сутки
        ряды = [_ряд(i, дата=ДАТА) for i in range(1, 7)]
        п_вх = врем / f"{ДАТА}.json"
        п_вх.write_text(json.dumps({"дата": ДАТА, "ряды": ряды},
                                   ensure_ascii=False), encoding="utf-8")
        вх = prochitat_vhod(п_вх)
        chk("вход разобрался и дата взята из имени файла",
            вх["ok"] and вх["data"] == ДАТА, (вх.get("why_not"), вх.get("data")))
        ж0 = {"файл": str(врем / ФАЙЛ_ЖУРНАЛА), "выдано": {}}
        реш = прим(vhod=вх, gr=гр, zh=ж0)
        chk("шесть годных рядов -- добавлено РОВНО пять",
            реш["ok"] and len(реш["dobavleno"]) == ПРЕДЕЛ_В_СУТКИ,
            len(реш.get("dobavleno") or []))
        шестой = [о for о in реш["otkazy"] if WHY_PREDEL.split(":")[0] in о["почему"]]
        chk("шестому отказано ПО ИМЕНИ предела, а не молча",
            len(шестой) == 1, реш["otkazy"])
        chk("порядок -- файла Code-2: взяты первые пять мест",
            [д["место_в_файле"] for д in реш["dobavleno"]] == [1, 2, 3, 4, 5],
            [д["место_в_файле"] for д in реш["dobavleno"]])
        пр = pravka(реш)
        chk("в правке ровно пять операций и ВСЕ -- добавление в konveyer",
            len(пр["pravki"]) == 5
            and all(set(о) == {"perenesti", "v", "pometka"} and о["v"] == ГРУППА
                    for о in пр["pravki"]), пр["pravki"][:1])
        chk("ни одной операции снятия, перевода из группы или правки полей",
            not any(к in о for о in пр["pravki"]
                    for к in ("otklyuchit", "pole", "znachenie", "sozdat",
                              "udalit_pole")), пр["pravki"])
        chk("в пометке стоят числа ряда -- за что адрес взят",
            all("n=" in о["pometka"] and "медиана=" in о["pometka"]
                for о in пр["pravki"]), пр["pravki"][0]["pometka"])

        # ------------------------------- 2. повтор того же файла не добавляет дважды
        зап = zapisat(реш, kat=врем, zhurnal_put=П_Ж)
        chk("правка, отчёт и журнал записаны",
            зап["ok"] and зап["why_not"] is None
            and all(зап[к] for к in ("pravka", "otchjot", "zhurnal")), зап)
        ж1 = zhurnal(зап["zhurnal"])
        chk("журнал помнит ровно пять выданных адресов за эту дату",
            len(ж1["выдано"].get(ДАТА) or []) == 5,
            ж1["выдано"].get(ДАТА))
        реш2 = прим(vhod=prochitat_vhod(п_вх), gr=гр, zh=ж1)
        chk("ПОВТОР того же файла: добавлено ноль",
            реш2["ok"] and not реш2["dobavleno"], реш2.get("dobavleno"))
        chk("и у каждого из пяти причина -- «уже выдан», а не «предел»",
            sum(1 for о in реш2["otkazy"] if WHY_UZHE_VYDAN in о["почему"]) == 5,
            [о["почему"] for о in реш2["otkazy"]][:2])
        chk("предел на вторые сутки тот же, а остаток -- ноль",
            реш2["predel_ostalsja"] == 0, реш2["predel_ostalsja"])
        # ПОВТОР ПОСЛЕ ПРИМЕНЕНИЯ ПРАВКИ -- вторая дорога к тому же: адрес уже
        # лежит в группе, и журнал для этого не нужен вовсе.
        д_гр = json.loads(п_гр.read_text(encoding="utf-8"))
        for о in пр["pravki"]:
            д_гр["groups"][ГРУППА]["addresses"][о["perenesti"]] = о["pometka"]
        п_гр2 = врем / "gruppy_posle.json"
        п_гр2.write_text(json.dumps(д_гр, ensure_ascii=False), encoding="utf-8")
        # ДВЕ ДОРОГИ К ОДНОМУ, И ОНИ НЕ ЗАМЕНЯЮТ ДРУГ ДРУГА. Здесь журнал
        # НАРОЧНО пустой: проверяется, что одного файла групп хватает против
        # ДУБЛЯ (пять уже лежащих адресов получают отказ по имени). Но против
        # перебора пятёрки за сутки файла групп НЕ хватает: шестой адрес,
        # которого в группе нет, станет годным -- и его держит только журнал.
        # Поэтому в бою нужны оба, и это проверяется следующей парой.
        реш3 = прим(vhod=prochitat_vhod(п_вх), gr=gruppy(п_гр2),
                          zh={"файл": "нет", "выдано": {}})
        chk("правка применена -- те же пять получают «уже в konveyer»",
            sum(1 for о in реш3["otkazy"]
                if WHY_UZHE_V_KONVEYERE in о["почему"]) == 5,
            [о["почему"] for о in реш3["otkazy"]][:2])
        chk("без журнала шестой адрес стал бы годным -- значит журнал нужен",
            len(реш3["dobavleno"]) == 1
            and реш3["dobavleno"][0]["место_в_файле"] == 6,
            реш3["dobavleno"])
        реш4 = прим(vhod=prochitat_vhod(п_вх), gr=gruppy(п_гр2), zh=ж1)
        chk("с журналом и применённой правкой добавлять нечего вовсе",
            not реш4["dobavleno"]
            and any(WHY_PREDEL.split(":")[0] in о["почему"]
                    for о in реш4["otkazy"]),
            (реш4["dobavleno"], [о["почему"] for о in реш4["otkazy"]][-1:]))

        # --------------------------------------------- 3. битый вход -- отказ словами
        битые = {
            "не JSON": ("{не json", WHY_NE_JSON),
            "нет рядов": ("{\"ряды\": []}", WHY_NET_RYADOV),
            "ряд не словарь": ("{\"ряды\": [1, 2]}", WHY_RYAD_NE_SLOVAR),
        }
        for имя, (текст, ждём) in битые.items():
            п_б = врем / f"{ДАТА}.json"
            п_б.write_text(текст, encoding="utf-8")
            вб = prochitat_vhod(п_б)
            chk(f"битый вход ({имя}) -- отказ по имени, а не пустое «ок»",
                not вб["ok"] and ждём in (вб["why_not"] or ""), вб.get("why_not"))
            рб = прим(vhod=вб, gr=гр, zh=ж0)
            chk(f"битый вход ({имя}): применитель не ок и причина названа",
                not рб["ok"] and рб["why_not"], рб.get("why_not"))
        п_нет = врем / "2026-10-09.json"
        вн = prochitat_vhod(п_нет)
        chk("файла нет -- отказ по имени",
            not вн["ok"] and WHY_NET_FAJLA in (вн["why_not"] or ""), вн.get("why_not"))
        вим = prochitat_vhod(врем / "konveyer.json")
        chk("имя файла не вида <дата>.json -- отказ по имени",
            not вим["ok"] and WHY_IMYA_FAJLA in (вим["why_not"] or ""),
            вим.get("why_not"))
        п_вх.write_text(json.dumps({"ряды": ряды}, ensure_ascii=False),
                        encoding="utf-8")

        # --------------------------------------------- 4. чужая дата
        п_чуж = врем / "2026-10-05.json"
        п_чуж.write_text(json.dumps({"ряды": [_ряд(1, дата=ДАТА)]},
                                    ensure_ascii=False), encoding="utf-8")
        вч = prochitat_vhod(п_чуж)
        chk("дата ряда не та, что у файла -- отказ ЦЕЛИКОМ и по имени",
            not вч["ok"] and WHY_CHUZHAYA_DATA in (вч["why_not"] or ""),
            вч.get("why_not"))
        рч = прим(vhod=вч, gr=гр, zh=ж0)
        chk("по чужой дате не добавляется НИ ОДНОГО адреса",
            not рч["ok"] and not рч["dobavleno"], рч.get("dobavleno"))

        # --------------------------------------------- 5. отказы по состоянию адреса
        # ЛОВУШКА dropped_by_credits: модуль службы говорит «off» (то есть
        # «нигде»), а применитель групп снял бы адрес из контейнера. Проверяется
        # и то, что модуль службы его действительно НЕ видит -- иначе проверка
        # была бы о другом.
        SGп = _модуль_групп()
        import os as _os  # noqa: PLC0415
        _os.environ["BLOOM_SOURCE_GROUPS"] = str(п_гр)
        SGп.загрузить(заново=True)
        chk("модуль службы адрес из dropped_by_credits НЕ видит (он «off»)",
            SGп.группа(_адрес(204)) == "off", SGп.группа(_адрес(204)))
        _os.environ.pop("BLOOM_SOURCE_GROUPS", None)
        chk("а применитель конвейера видит его в сыром файле",
            _адрес(204) in (гр.get("po_kontejneram") or {})
            and _адрес(204) in (гр.get("tolko_v_fajle") or []),
            гр.get("tolko_v_fajle"))
        в_др = {"ok": True, "data": ДАТА, "fajl": "фикстура",
                "ryady": [_ряд(1, дата=ДАТА, адрес=_адрес(204))]}
        р_др = прим(vhod=в_др, gr=гр, zh=ж0)
        chk("адрес из dropped_by_credits -- отказ по имени контейнера",
            not р_др["dobavleno"]
            and WHY_V_KONTEJNERE in р_др["otkazy"][0]["почему"]
            and "dropped_by_credits" in р_др["otkazy"][0]["почему"],
            р_др["otkazy"])
        случаи = [
            ("уже в торгующей группе", _адрес(200), WHY_TORGUET),
            ("в log_only", _адрес(201), WHY_LOG_ONLY),
            ("в off", _адрес(202), WHY_OFF),
            (f"уже в {ГРУППА}", _адрес(203), WHY_UZHE_V_KONVEYERE),
        ]
        for имя, адр, ждём in случаи:
            в1 = {"ok": True, "data": ДАТА, "fajl": "фикстура",
                  "ryady": [_ряд(1, дата=ДАТА, адрес=адр)]}
            р1 = прим(vhod=в1, gr=гр, zh=ж0)
            chk(f"отказ по имени: {имя}",
                not р1["dobavleno"] and ждём in (р1["otkazy"][0]["почему"] or ""),
                р1["otkazy"])
        # НЕ ТОТ РАЗДЕЛ
        в_р = {"ok": True, "data": ДАТА, "fajl": "фикстура",
               "ryady": [_ряд(1, дата=ДАТА, раздел="смотреть"),
                         _ряд(2, дата=ДАТА, раздел="снять"),
                         _ряд(3, дата=ДАТА)]}
        р_р = прим(vhod=в_р, gr=гр, zh=ж0)
        chk("берётся ТОЛЬКО раздел «добавить», прочим -- отказ по имени",
            len(р_р["dobavleno"]) == 1
            and sum(1 for о in р_р["otkazy"] if WHY_NE_RAZDEL in о["почему"]) == 2,
            (len(р_р["dobavleno"]), [о["почему"] for о in р_р["otkazy"]]))
        # НЕТ ОБЯЗАТЕЛЬНОГО ПОЛЯ -- по каждому полю отдельно. У поля «раздел»
        # отказ ДРУГОЙ и раньше: без него ряд не из раздела «добавить» вовсе
        # (см. порядок проверок в reshenie), и это честнее, чем жаловаться на
        # пропущенное поле у адреса, которого мы и не собирались брать.
        без_поля, без_раздела = 0, 0
        for поле in ОБЯЗАТЕЛЬНЫЕ_ПОЛЯ:
            р = _ряд(1, дата=ДАТА)
            р.pop(поле)
            в_п = {"ok": True, "data": ДАТА, "fajl": "фикстура", "ryady": [р]}
            рп = прим(vhod=в_п, gr=гр, zh=ж0)
            причина = рп["otkazy"][0]["почему"] if рп["otkazy"] else ""
            if поле == "раздел":
                без_раздела += int(not рп["dobavleno"]
                                   and WHY_NE_RAZDEL in причина)
                continue
            if (not рп["dobavleno"] and WHY_NET_POLYA in причина
                    and поле in причина):
                без_поля += 1
        chk(f"без любого из {len(ОБЯЗАТЕЛЬНЫЕ_ПОЛЯ) - 1} полей (кроме раздела) -- "
            f"отказ с именем поля",
            без_поля == len(ОБЯЗАТЕЛЬНЫЕ_ПОЛЯ) - 1, без_поля)
        chk("без поля «раздел» -- отказ «не тот раздел», и он стоит раньше",
            без_раздела == 1, без_раздела)

        # --------------------------------------------- 6. кусты
        в_к = {"ok": True, "data": ДАТА, "fajl": "фикстура",
               "ryady": [_ряд(10, дата=ДАТА, куст="k9",
                              адрес=_адрес(200)),      # уже торгует
                         _ряд(11, дата=ДАТА, куст="k9"),
                         _ряд(12, дата=ДАТА, куст="k8"),
                         _ряд(13, дата=ДАТА, куст="k8")]}
        р_к = прим(vhod=в_к, gr=гр, zh=ж0)
        chk("куст с действующим источником -- отказ по имени",
            sum(1 for о in р_к["otkazy"]
                if WHY_KUST_DEJSTVUET in о["почему"]) == 1,
            [о["почему"] for о in р_к["otkazy"]])
        chk("из куста берётся РОВНО один адрес в партии",
            len(р_к["dobavleno"]) == 1
            and sum(1 for о in р_к["otkazy"]
                    if WHY_KUST_V_PARTII in о["почему"]) == 1,
            (len(р_к["dobavleno"]), [о["почему"] for о in р_к["otkazy"]]))
        # КАРТА КУСТОВ ОТ CODE-2 (если прислал) -- тоже работает
        в_к2 = {"ok": True, "data": ДАТА, "fajl": "фикстура",
                "kustov": {"k7": [_адрес(200)]},
                "ryady": [_ряд(14, дата=ДАТА, куст="k7")]}
        р_к2 = прим(vhod=в_к2, gr=гр, zh=ж0)
        chk("карта кустов Code-2 ловит действующий источник вне рядов",
            not р_к2["dobavleno"]
            and WHY_KUST_DEJSTVUET in р_к2["otkazy"][0]["почему"],
            р_к2["otkazy"])

        # --------------------------------------------- 7. нет группы konveyer
        п_без = _файл_групп(врем / "bez", с_конвейером=False)
        гр_без = gruppy(п_без)
        chk(f"нет группы {ГРУППА} -- применитель отказывает по имени",
            not гр_без["est_konveyer"], гр_без.get("why_not"))
        р_без = прим(vhod={"ok": True, "data": ДАТА, "fajl": "ф",
                                 "ryady": [_ряд(1, дата=ДАТА)]},
                           gr=гр_без, zh=ж0)
        chk("и ни одного адреса не выдаёт",
            not р_без["ok"] and WHY_NET_GRUPPY in (р_без["why_not"] or ""),
            р_без.get("why_not"))

        # ------------------------- 8. битый журнал не превращается в «пустой»
        п_жб = врем / "zhurnal_bityj.json"
        п_жб.write_text("{не json", encoding="utf-8")
        жб = zhurnal(п_жб)
        chk("битый журнал назван битым, а не пустым", жб.get("битый"), жб)
        р_жб = прим(vhod={"ok": True, "data": ДАТА, "fajl": "ф",
                                "ryady": [_ряд(1, дата=ДАТА)]}, gr=гр, zh=жб)
        chk("на битом журнале применитель встаёт, а не выдаёт правку заново",
            not р_жб["ok"] and "битый" in (р_жб["why_not"] or ""),
            р_жб.get("why_not"))

        # ------------------- 9. ПРАВКА ПРОХОДИТ ЧУЖИМ ПРИМЕНИТЕЛЕМ ГРУПП
        # Это и есть сдача: правку применяет deploy/checks/pravka_grupp.py, у
        # него самопроверка денежного пути. Здесь она прогоняется НА КОПИИ файла
        # групп в временном каталоге -- ни одного файла репозитория.
        sys.path.insert(0, str(КОРЕНЬ / "deploy" / "checks"))
        import pravka_grupp as PG  # noqa: PLC0415

        п_копия = врем / "gruppy_dlja_pravki.json"
        shutil.copy2(п_гр, п_копия)
        д_к = json.loads(п_копия.read_text(encoding="utf-8"))
        ряды5 = [_ряд(i, дата=ДАТА) for i in range(1, 6)]
        в5 = {"ok": True, "data": ДАТА, "fajl": "фикстура", "ryady": ряды5}
        р5 = прим(vhod=в5, gr=gruppy(п_копия), zh={"выдано": {}})
        пр5 = pravka(р5)
        ждём_группу, ждём_поля, ждём_списки, ждём_убрано = {}, {}, {}, {}
        коды = [PG.применить(д_к["groups"], о, ждём_группу, ждём_поля,
                             ждём_списки, ждём_убрано)
                for о in пр5["pravki"]]
        chk("чужой применитель групп принял КАЖДУЮ операцию правки (код 0)",
            коды == [0] * 5, коды)
        chk("он ждёт эти адреса именно в konveyer",
            ждём_группу == {д["адрес"]: ГРУППА for д in р5["dobavleno"]},
            ждём_группу)
        chk("ни одной правки полей и ни одного убранного поля он не увидел",
            not ждём_поля and not ждём_убрано, (ждём_поля, ждём_убрано))
        п_копия.write_text(json.dumps(д_к, ensure_ascii=False, indent=1),
                           encoding="utf-8")
        гр_после = gruppy(п_копия)
        в_группе = [а for а, г in гр_после["po_adresu"].items() if г == ГРУППА]
        chk("после применения все пять адресов лежат в konveyer по модулю службы",
            all(д["адрес"] in в_группе for д in р5["dobavleno"]),
            (len(в_группе), [д["адрес"][:8] for д in р5["dobavleno"]]))
        SG = _модуль_групп()
        import os  # noqa: PLC0415
        os.environ["BLOOM_SOURCE_GROUPS"] = str(п_копия)
        SG.загрузить(заново=True)
        одна = SG.группа(р5["dobavleno"][0]["адрес"])
        chk("и bloom_source_groups.группа() отдаёт konveyer для добавленного",
            одна == ГРУППА, одна)
        chk("а чужие адреса остались в своих группах",
            SG.группа(_адрес(200)) == "batch5"
            and SG.группа(_адрес(201)) == "log_only"
            and SG.группа(_адрес(202)) == "off",
            (SG.группа(_адрес(200)), SG.группа(_адрес(201)),
             SG.группа(_адрес(202))))
        os.environ.pop("BLOOM_SOURCE_GROUPS", None)

        # ------------------- 9б. СВЕРКА ПРАВКИ ПЕРЕД ПРИМЕНЕНИЕМ
        # Безопасность правки держится на состоянии файла групп В МОМЕНТ
        # ПРИМЕНЕНИЯ, а не записи. Поэтому отдельный шаг, и он проверяется.
        п_пр = врем / "pravka_dlja_sverki.json"
        п_пр.write_text(json.dumps(пр5, ensure_ascii=False), encoding="utf-8")
        п_чист = врем / "gruppy_chistye.json"
        shutil.copy2(_файл_групп(врем / "chistye"), п_чист)
        св = sverit_pravku(п_пр, gruppy_put=п_чист, zhurnal_put=П_Ж)
        chk("сверка: на чистом файле групп правка годна целиком",
            св["ok"] and св["pravok"] == 5 and св["godnyh"] == 5,
            (св.get("why_not"), св.get("pravok"), св.get("godnyh")))
        # А теперь тот же файл групп, КУДА ПРАВКУ УЖЕ ПРИМЕНИЛИ (или адрес попал
        # туда иначе): сверка обязана отказать, иначе perenesti СНИМЕТ его.
        св2 = sverit_pravku(п_пр, gruppy_put=п_копия, zhurnal_put=П_Ж)
        chk("сверка: адрес уже в группе -- правка УСТАРЕЛА, применять нельзя",
            not св2["ok"] and WHY_PRAVKA_USTARELA in (св2["why_not"] or "")
            and len(св2["ustarelo"]) == 5, (св2.get("why_not"),
                                            св2.get("ustarelo")))
        # Чужая операция в правке -- отказ по имени (даже если её подложили руками)
        п_чуж_пр = врем / "pravka_chuzhaja.json"
        п_чуж_пр.write_text(json.dumps(
            {"pravki": [{"otklyuchit": _адрес(1), "v": "off"}]},
            ensure_ascii=False), encoding="utf-8")
        св3 = sverit_pravku(п_чуж_пр, gruppy_put=п_чист, zhurnal_put=П_Ж)
        chk("сверка: операция не-добавление в правке -- отказ по имени",
            not св3["ok"]
            and WHY_PRAVKA_NE_DOBAVLENIE in (св3["why_not"] or ""),
            св3.get("why_not"))
        п_пуст = врем / "pravka_pustaja.json"
        п_пуст.write_text(json.dumps({"pravki": []}), encoding="utf-8")
        св4 = sverit_pravku(п_пуст, gruppy_put=п_чист, zhurnal_put=П_Ж)
        chk("сверка: пустая правка -- отказ по имени, а не «годна»",
            not св4["ok"] and WHY_PRAVKA_PUSTA in (св4["why_not"] or ""),
            св4.get("why_not"))

        # ------------------- 9в. ЖИВОЙ ФАЙЛ CODE-2, А НЕ ФИКСТУРА
        # Фикстуры проверяют правила, а живой файл -- что форма угадана верно.
        # Это первый файл конвейера (data/konveyer/2026-10-03.json, скопирован
        # байт в байт из ветки claude/podbivka): ряды СЛОВАРЁМ, 48 штук, из них
        # раздел «добавить» -- один, и у 15 рядов числовых полей нет вовсе.
        п_жив = КОРЕНЬ / "data" / "konveyer" / FAJL_ZHIVOJ
        chk(f"живой файл Code-2 на месте: {FAJL_ZHIVOJ}", п_жив.exists(),
            str(п_жив))
        if п_жив.exists():
            вж = prochitat_vhod(п_жив)
            chk("живой файл разобрался, ряды взяты СЛОВАРЁМ",
                вж["ok"] and вж.get("klyuch") == "ряды"
                and вж.get("rjadov_slovarjom") == ZHDEM_ZHIVOGO["рядов"],
                (вж.get("why_not"), вж.get("klyuch"),
                 вж.get("rjadov_slovarjom")))
            chk("и «разделы» Code-2 сошлись с полем «раздел» у рядов",
                вж.get("dobavit_po_razdelam") == ZHDEM_ZHIVOGO["добавить"],
                вж.get("dobavit_po_razdelam"))
            # Файл групп -- КОПИЯ живого плюс группа konveyer: у Code-1 она в
            # бою уже есть, в репозитории её ещё нет.
            д_жг = json.loads((КОРЕНЬ / "data" / FAJL_GRUPP_REPO)
                              .read_text(encoding="utf-8"))
            д_жг["groups"][ГРУППА] = {"lane_size": 0.01, "lane_trades": True,
                                      "bloom_trades": False, "addresses": {}}
            п_жг = врем / "gruppy_s_konveyerom.json"
            п_жг.write_text(json.dumps(д_жг, ensure_ascii=False),
                            encoding="utf-8")
            рж = прим(vhod=вж, gr=gruppy(п_жг), zh={"выдано": {}})
            chk(f"на живом файле добавлено {ZHDEM_ZHIVOGO['добавлено']} -- "
                f"{ZHDEM_ZHIVOGO['добавить'][0][:8]}",
                рж["ok"]
                and [д["адрес"] for д in рж["dobavleno"]]
                == ZHDEM_ZHIVOGO["добавить"],
                [д["адрес"] for д in (рж.get("dobavleno") or [])])
            не_тот = sum(1 for о in рж["otkazy"] if WHY_NE_RAZDEL in о["почему"])
            chk(f"и {ZHDEM_ZHIVOGO['не_тот_razdel']} отказов -- «не тот раздел», "
                f"а не «нет поля»",
                не_тот == ZHDEM_ZHIVOGO["не_тот_razdel"]
                and не_тот + len(рж["dobavleno"]) == ZHDEM_ZHIVOGO["рядов"],
                (не_тот, len(рж["dobavleno"])))
            прж = pravka(рж)
            chk("правка по живому файлу -- одна операция, добавление в konveyer",
                len(прж["pravki"]) == 1
                and set(прж["pravki"][0]) == {"perenesti", "v", "pometka"}
                and прж["pravki"][0]["v"] == ГРУППА,
                прж["pravki"])
            chk("на живом файле кусты НЕ НАЗВАНЫ, и это число в сводке, а "
                "не молчание",
                рж["svod"]["кустов_не_названо"]
                == ZHDEM_ZHIVOGO["кустов_не_названо"],
                рж["svod"].get("кустов_не_названо"))
            chk("в пометке живого адреса -- его числа из файла Code-2",
                "n=22" in прж["pravki"][0]["pometka"]
                and "медиана=1.4" in прж["pravki"][0]["pometka"],
                прж["pravki"][0]["pometka"])


        # ------------------- 9г. ПОСЛЕ ВСТРЕЧНОГО РАЗБОРА (пять сторон)
        # Ниже -- проверки на то, что встречный разбор нашёл, а не на то, что
        # задумано. Каждая названа тем, что теряется без неё.

        # 9г.1 ВИД АДРЕСА. Строка, не являющаяся ключом Solana, уходила в правку
        # и оставалась ключом в живом файле групп навсегда.
        chk("ключ Solana узнаётся, мусор -- нет",
            pohozh_na_klyuch(_адрес(1)) and not pohozh_na_klyuch("None")
            and not pohozh_na_klyuch(" x ") and not pohozh_na_klyuch(None),
            [pohozh_na_klyuch(x) for x in (_адрес(1), "None", " x ", None)])
        в_мусор = {"ok": True, "data": ДАТА, "fajl": "ф",
                   "ryady": [_ряд(1, дата=ДАТА, адрес="None"),
                             _ряд(2, дата=ДАТА, адрес="0" * 44)]}
        р_мусор = прим(vhod=в_мусор, gr=гр, zh=ж0)
        chk("мусорный адрес в ряду -- отказ ПО ИМЕНИ, а не ключ в файле групп",
            р_мусор["ok"] and not р_мусор["dobavleno"]
            and sum(1 for о in р_мусор["otkazy"]
                    if WHY_ADRES_NE_KLYUCH in о["почему"]) == 2,
            р_мусор.get("otkazy"))

        # 9г.2 ДУБЛЬ В ОДНОМ ФАЙЛЕ. Ряды списком повтора не запрещают: один
        # адрес давал ДВЕ операции в правке и съедал две единицы предела.
        в_дубль = {"ok": True, "data": ДАТА, "fajl": "ф",
                   "ryady": [_ряд(1, дата=ДАТА, куст=None),
                             _ряд(1, дата=ДАТА, куст=None)]}
        р_дубль = прим(vhod=в_дубль, gr=гр, zh=ж0)
        chk("один адрес дважды в файле -- взят ОДИН, второму отказ по имени",
            len(р_дубль["dobavleno"]) == 1
            and sum(1 for о in р_дубль["otkazy"] if WHY_DUBL in о["почему"]) == 1,
            (len(р_дубль["dobavleno"]), р_дубль["otkazy"]))

        # 9г.3 ОКНО ДАТЫ. Файл за прошлый месяц сверялся сам с собой и проходил:
        # журнал за ту дату пуст -- значит снова пять «свежих» адресов, считанных
        # по мёртвому окну.
        chk("сутки между датами считаются числом",
            sutok_mezhdu(ДАТА, "2026-10-03") == 1
            and sutok_mezhdu("2026-09-01", "2026-10-03") == 32
            and sutok_mezhdu("не дата", "2026-10-03") is None,
            [sutok_mezhdu(ДАТА, "2026-10-03"),
             sutok_mezhdu("2026-09-01", "2026-10-03"),
             sutok_mezhdu("не дата", "2026-10-03")])
        р_далеко = primenitel(vhod=вх, gr=гр, zh=ж0, segodnja="2026-11-20")
        chk("файл старше окна -- отказ ЦЕЛИКОМ и по имени",
            not р_далеко["ok"] and WHY_DATA_DALEKO in (р_далеко["why_not"] or ""),
            р_далеко.get("why_not"))
        край = (datetime.date.fromisoformat(ДАТА)
                + datetime.timedelta(days=OKNO_DATY_SUTOK)).isoformat()
        р_край = primenitel(vhod=вх, gr=гр, zh=ж0, segodnja=край)
        chk(f"на краю окна (ровно {OKNO_DATY_SUTOK} суток) файл ещё годен",
            р_край["ok"] and len(р_край["dobavleno"]) == ПРЕДЕЛ_В_СУТКИ,
            (р_край.get("why_not"), len(р_край.get("dobavleno") or [])))

        # 9г.4 addresses СПИСКОМ У ЦЕЛЕВОЙ ГРУППЫ -- САМОЕ ДОРОГОЕ ИЗ НАЙДЕННОГО.
        # Проверено своим прогоном pravka_grupp.py: было [{AAA1},{BBB2}] --
        # стало {"NEW3": "конвейер"}, код выхода 0, адреса ПОТЕРЯНЫ.
        д_сп = json.loads(п_чист.read_text(encoding="utf-8"))
        д_сп["groups"][ГРУППА]["addresses"] = [
            {"address": _адрес(210), "почему": "уже в конвейере"},
            {"address": _адрес(211), "почему": "уже в конвейере"}]
        п_сп = врем / "gruppy_spiskom.json"
        п_сп.write_text(json.dumps(д_сп, ensure_ascii=False), encoding="utf-8")
        гр_сп = gruppy(п_сп)
        chk("вид addresses у konveyer назван: СПИСОК, в нём два адреса",
            гр_сп["vid_addresses"] == "список"
            and гр_сп["adresov_v_konveyere"] == 2,
            (гр_сп.get("vid_addresses"), гр_сп.get("adresov_v_konveyere")))
        р_сп = прим(vhod=вх, gr=гр_сп, zh=ж0)
        chk("addresses списком -- применитель отказывает ЦЕЛИКОМ: иначе правка "
            "СНЯЛА БЫ всех, кто в конвейере уже лежит",
            not р_сп["ok"] and WHY_ADRESA_SPISKOM in (р_сп["why_not"] or ""),
            р_сп.get("why_not"))
        св_сп = sverit_pravku(п_пр, gruppy_put=п_сп, zhurnal_put=П_Ж)
        chk("и сверка ПЕРЕД применением отказывает по той же причине: вид поля "
            "мог стать списком уже после того, как правка записана",
            not св_сп["ok"] and WHY_ADRESA_SPISKOM in (св_сп["why_not"] or ""),
            св_сп.get("why_not"))
        д_сп["groups"][ГРУППА]["addresses"] = []
        п_сп0 = врем / "gruppy_spisok_pustoj.json"
        п_сп0.write_text(json.dumps(д_сп, ensure_ascii=False), encoding="utf-8")
        р_сп0 = прим(vhod=вх, gr=gruppy(п_сп0), zh=ж0)
        chk("ПУСТОЙ список addresses -- не отказ: сотрётся ничто",
            р_сп0["ok"] and len(р_сп0["dobavleno"]) == ПРЕДЕЛ_В_СУТКИ,
            р_сп0.get("why_not"))

        # 9г.5 ЦЕЛЕВАЯ ГРУППА ОБЯЗАНА ТОРГОВАТЬ. Иначе добавление -- запись в
        # мёртвую группу: отчёт зелёный, адресов прибыло, денег нет.
        д_мёртв = json.loads(п_чист.read_text(encoding="utf-8"))
        д_мёртв["groups"][ГРУППА].update(lane_trades=False, bloom_trades=False)
        п_мёртв = врем / "gruppy_konveyer_ne_torguet.json"
        п_мёртв.write_text(json.dumps(д_мёртв, ensure_ascii=False),
                           encoding="utf-8")
        р_мёртв = прим(vhod=вх, gr=gruppy(п_мёртв), zh=ж0)
        chk("konveyer со снятыми признаками -- отказ по имени, а не выдача в "
            "мёртвую группу",
            not р_мёртв["ok"]
            and WHY_GRUPPA_NE_TORGUET in (р_мёртв["why_not"] or ""),
            р_мёртв.get("why_not"))
        # СПОР РАЗМЕРА -- ОТДЕЛЬНОЙ ПРИЧИНОЙ: загрузчик служб сам снимает
        # lane_trades, когда lane_size и lane_sol разные, и чинится это одним
        # полем правды в файле групп, а не признаком торговли.
        д_спор = json.loads(п_чист.read_text(encoding="utf-8"))
        д_спор["groups"][ГРУППА].update(lane_size=0.01, lane_sol=0.02)
        п_спор = врем / "gruppy_spor_razmera.json"
        п_спор.write_text(json.dumps(д_спор, ensure_ascii=False),
                          encoding="utf-8")
        гр_спор = gruppy(п_спор)
        р_спор = прим(vhod=вх, gr=гр_спор, zh=ж0)
        chk("разные lane_size и lane_sol у konveyer -- отказ ИМЕНЕМ СПОРА",
            not р_спор["ok"] and WHY_SPOR_RAZMERA in (р_спор["why_not"] or "")
            and гр_спор.get("konveyer_spor_razmera"),
            (р_спор.get("why_not"), гр_спор.get("konveyer_spor_razmera")))

        # 9г.6 ПРЕДЕЛ СВЕРХ ПЯТИ НЕ БЕРЁТСЯ ВОВСЕ -- ключ --predel обходил слово
        # владельца одним прогоном.
        р_сверх = прим(vhod=вх, gr=гр, zh=ж0, predel=50)
        chk("--predel больше пяти -- отказ целиком, а не пятьдесят адресов",
            not р_сверх["ok"] and WHY_PREDEL_SVERH in (р_сверх["why_not"] or ""),
            р_сверх.get("why_not"))
        р_меньше = прим(vhod=вх, gr=гр, zh=ж0, predel=2)
        chk("--predel меньше пяти можно: взято ровно два",
            р_меньше["ok"] and len(р_меньше["dobavleno"]) == 2,
            len(р_меньше.get("dobavleno") or []))

        # 9г.7 СВОДКА ГОВОРИТ ОБ ОСТАТКЕ ПОСЛЕ ПАРТИИ, А НЕ ДО НЕЁ.
        chk("в сводке остаток предела ПОСЛЕ партии (0), и отдельно -- до (5)",
            реш["svod"]["предел_остался"] == 0
            and реш["svod"]["предел_остался_до"] == ПРЕДЕЛ_В_СУТКИ
            and реш["predel_ostalsja"] == 0,
            (реш["svod"].get("предел_остался"),
             реш["svod"].get("предел_остался_до"), реш.get("predel_ostalsja")))

        # 9г.8 ПУСТАЯ ПРАВКА НЕ ЗАТИРАЕТ НЕПУСТУЮ. Второй прогон того же файла
        # даёт ноль добавлений -- и стирал файл, который Code-1 ещё не применил.
        п_зат = врем / "zatirka"
        зап_н = zapisat(реш, kat=п_зат, zhurnal_put=п_зат / ФАЙЛ_ЖУРНАЛА)
        байты_до = (п_зат / f"pravka_{ДАТА}.json").read_bytes()
        зап_п = zapisat(dict(реш, dobavleno=[]), kat=п_зат,
                        zhurnal_put=п_зат / ФАЙЛ_ЖУРНАЛА)
        chk("пустая правка НЕ затирает непустую: отказ словами, байты на месте",
            зап_н["ok"] and not зап_п["ok"]
            and WHY_ZATROT_PRAVKU in (зап_п["why_not"] or "")
            and (п_зат / f"pravka_{ДАТА}.json").read_bytes() == байты_до,
            зап_п)

        # 9г.9 ЖУРНАЛ НЕ ЕДЕТ ЗА --kat. Иначе один прогон с чужим каталогом
        # выдавал ещё пятёрку сверх пятёрки, ничего формально не нарушив.
        мод = sys.modules[__name__]
        каталог_был = мод.КАТАЛОГ
        мод.КАТАЛОГ = врем / "kat_po_umolchaniju"
        try:
            зап_к = zapisat(реш, kat=врем / "drugoj_kat")
        finally:
            мод.КАТАЛОГ = каталог_был
        chk("журнал лёг в каталог ПО УМОЛЧАНИЮ, а не в тот, что дали --kat",
            зап_к["ok"]
            and зап_к["zhurnal"] == str(врем / "kat_po_umolchaniju"
                                        / ФАЙЛ_ЖУРНАЛА)
            and (врем / "kat_po_umolchaniju" / ФАЙЛ_ЖУРНАЛА).exists()
            and not (врем / "drugoj_kat" / ФАЙЛ_ЖУРНАЛА).exists(), зап_к)

        # 9г.10 --pokazat НЕ ВЫДАЧА, И ЭТО СКАЗАНО. Напечатанную правку можно
        # перенаправить в файл и применить, а журнал её не видел.
        import contextlib  # noqa: PLC0415
        кат_п, ж_п = врем / "pokaz", врем / "pokaz_zhurnal.json"
        арг_были, сег_была = sys.argv, мод.segodnja_utc
        sys.argv = ["c3_konveyer_primenitel.py", "--vhod", str(п_вх),
                    "--gruppy", str(п_чист), "--kat", str(кат_п),
                    "--zhurnal", str(ж_п), "--pokazat"]
        мод.segodnja_utc = lambda: ДАТА
        о_вых, о_ош = io_mod.StringIO(), io_mod.StringIO()
        try:
            with contextlib.redirect_stdout(о_вых), \
                    contextlib.redirect_stderr(о_ош):
                код_п = main()
        finally:
            sys.argv, мод.segodnja_utc = арг_были, сег_была
        chk("--pokazat: сказано «НЕ ДЛЯ ПРИМЕНЕНИЯ», и ни правки, ни журнала "
            "на диске не появилось",
            код_п == 0 and "НЕ ДЛЯ ПРИМЕНЕНИЯ" in о_ош.getvalue()
            and not ж_п.exists()
            and not list(кат_п.glob("pravka_*.json")),
            (код_п, о_ош.getvalue()[:160],
             sorted(x.name for x in кат_п.glob("*")) if кат_п.exists() else []))

        # 9г.11 СВЕРКА СЧИТАЕТ ОСТАТОК ПРЕДЕЛА ПО ЖУРНАЛУ. Правку можно написать
        # руками: «только добавление в konveyer» соблюдено, пятёрка -- нет.
        кат_св = врем / "sverh_predela"
        кат_св.mkdir(parents=True, exist_ok=True)
        п_сверх_пр = кат_св / f"pravka_{ДАТА}.json"
        п_сверх_пр.write_text(json.dumps(
            {"дата": ДАТА,
             "pravki": [{"perenesti": _адрес(90), "v": ГРУППА,
                         "pometka": "дописано руками"}]},
            ensure_ascii=False), encoding="utf-8")
        св5 = sverit_pravku(п_сверх_пр, gruppy_put=п_чист, zhurnal_put=П_Ж)
        chk("сверка: адрес сверх суточного остатка -- отказ по имени предела",
            not св5["ok"]
            and WHY_PRAVKA_SVERH_PREDELA in (св5["why_not"] or "")
            and св5["vydano_za_sutki"] == ПРЕДЕЛ_В_СУТКИ
            and св5["predel_ostalsja"] == 0,
            (св5.get("why_not"), св5.get("vydano_za_sutki")))
        chk("а правка этого применителя остаток не превышает: её адреса уже в "
            "журнале, дважды они не считаются",
            sverit_pravku(п_пр, gruppy_put=п_чист,
                          zhurnal_put=П_Ж)["ok"], None)
        п_без_даты = врем / "ruchnaja_pravka.json"
        п_без_даты.write_text(json.dumps(
            {"pravki": [{"perenesti": _адрес(91), "v": ГРУППА,
                         "pometka": "без даты"}]}, ensure_ascii=False),
            encoding="utf-8")
        св6 = sverit_pravku(п_без_даты, gruppy_put=п_чист, zhurnal_put=П_Ж)
        chk("сверка: правка без даты -- отказ, остаток предела не посчитать",
            not св6["ok"] and WHY_PRAVKA_BEZ_DATY in (св6["why_not"] or ""),
            св6.get("why_not"))
        chk("дата лежит В САМОЙ ПРАВКЕ, а не только в имени файла",
            pravka(реш).get("дата") == ДАТА, pravka(реш).get("дата"))

        # ------------------- 10. ничего из репозитория не тронуто
        chk("все файлы самопроверки -- во временном каталоге",
            all(str(врем) in str(x) for x in (зап["pravka"], зап["otchjot"],
                                              зап["zhurnal"])),
            зап)
    finally:
        shutil.rmtree(врем, ignore_errors=True)

    print(f"\nпроверок {было}, ждали {ZHDEM_PROVEROK}, не прошло {плохо}")
    if было != ZHDEM_PROVEROK:
        print(" ПЛОХО число проверок разошлось с объявленным -- "
              "молчаливый пропуск считается провалом")
        return 1
    return 1 if плохо else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--vhod", default=None,
                   help="файл Code-2 data/konveyer/<дата>.json")
    p.add_argument("--gruppy", default=None,
                   help="файл групп (по умолчанию -- тот, что читает служба)")
    p.add_argument("--kat", default=None, help="куда класть правку и отчёт")
    p.add_argument("--zhurnal", default=None,
                   help=("файл журнала выдач; по умолчанию "
                         "data/konveyer/primenitel_zhurnal.json -- и --kat его "
                         "НЕ двигает: иначе суточный предел обходится одним "
                         "ключом командной строки"))
    p.add_argument("--predel", type=int, default=ПРЕДЕЛ_В_СУТКИ)
    p.add_argument("--pokazat", action="store_true",
                   help="только показать решение, ничего не записывать")
    p.add_argument("--sverit", default=None,
                   help="проверить записанную правку перед применением")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    ж_путь = Path(a.zhurnal) if a.zhurnal else (КАТАЛОГ / ФАЙЛ_ЖУРНАЛА)
    if a.sverit:
        св = sverit_pravku(a.sverit, gruppy_put=a.gruppy, zhurnal_put=ж_путь)
        print(json.dumps(св, ensure_ascii=False, indent=1))
        if not св["ok"]:
            print(f"СТОП: {св['why_not']}", file=sys.stderr)
            return 3
        print(f"правка годна: {св['pravok']} добавлений, все адреса в группах "
              f"нигде не лежат")
        return 0
    if a.self_test or not a.vhod:
        return self_test()
    вх = prochitat_vhod(a.vhod)
    гр = gruppy(a.gruppy)
    ж = zhurnal(ж_путь)
    реш = primenitel(vhod=вх, gr=гр, zh=ж, predel=a.predel)
    print(json.dumps({k: v for k, v in реш.items() if k != "otkazy"},
                     ensure_ascii=False, indent=1))
    for о in (реш.get("otkazy") or []):
        print(f"  отказ место {о['место']} "
              f"{str(о.get('адрес'))[:8]}: {о['почему']}")
    if not реш["ok"]:
        print(f"СТОП: {реш['why_not']}", file=sys.stderr)
        return 2
    if a.pokazat:
        print(json.dumps(pravka(реш), ensure_ascii=False, indent=1))
        # ПОКАЗ -- НЕ ВЫДАЧА, И ЭТО СКАЗАНО ВСЛУХ. Журнал не записан, значит эти
        # же адреса выдадутся снова и суточный предел ими НЕ ЗАНЯТ. Применить
        # напечатанное, перенаправив вывод в файл, -- значит выдать пятёрку,
        # которой журнал не видел.
        print("НЕ ДЛЯ ПРИМЕНЕНИЯ: --pokazat только печатает. Журнал "
              f"{ж_путь} НЕ записан, суточный предел этими адресами НЕ занят; "
              "для выдачи прогнать без --pokazat.", file=sys.stderr)
        return 0
    зап = zapisat(реш, kat=a.kat, zhurnal_put=ж_путь)
    print(json.dumps(зап, ensure_ascii=False, indent=1))
    if not зап["ok"]:
        print(f"СТОП: {зап['why_not']}", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
