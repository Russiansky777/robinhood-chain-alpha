#!/usr/bin/env python3
"""Сколько сигналов по каждому строителю пришло за сутки -- ПО ГРУППАМ, числом.

ЗАЧЕМ. Очередь строителей владельца: LaunchLab -> DLMM -> Whirlpool -> AMM v4,
и каждый включается до первой живой сделки. Вопрос "сколько ждать первую сделку
LaunchLab" требует числа, а не впечатления: сколько сигналов по пулам этого
строителя прошло за сутки и по каким группам. Тем же счётом виден и запас по
следующим строителям -- стоит ли их вообще включать.

Считается по журналу решений ПОТОКОМ: адрес программы строителя ищется в строке
подстрокой до разбора JSON (это секунды вместо минут на файле в сотни
мегабайт), а группа берётся из самой записи. Строки теневого замера считаются
ОТДЕЛЬНО от решений по торгующим группам: смешивать их нельзя -- тень не
торгует.

Только чтение: журнал решений. Ни цепи, ни подписи, ни отправки.
"""
from __future__ import annotations

import argparse
import calendar
import io
import glob
import gzip
import json
import os
import sys
import time

# Адреса строителей -> имена владельца. Те же, что в c2_swap_build.
СТРОИТЕЛИ = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "bonding",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pump_amm",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "cpmm",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "damm2",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "dbc",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "clmm",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "launchlab",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "dlmm",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "whirlpool",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "amm_v4",
}
# Группы, которые ТОРГУЮТ полосой. Остальные (log_only, off, kandidaty) считаются
# отдельно: сигнал по ним не станет сделкой, сколько его ни ждать.
#
# ЭТО ТОЛЬКО ЗАПАСНОЙ СПИСОК. Правда о том, кто торгует, лежит в файле групп
# (lane_trades), и она меняется в любой час: на 30.09 sniper_src стоит в этом
# списке, а lane_trades у него выключен -- счёт "торгующих_групп" по константе
# завышал запас по строителю и подталкивал включать его раньше времени.
ТОРГУЮЩИЕ_ЗАПАС = ("lane_s0", "batch5", "sniper_src", "leader")

# СТАДИИ, КОТОРЫЕ НЕ ЯВЛЯЮТСЯ СИГНАЛОМ ВОВСЕ. leg_pool_skip -- каталог ног
# двухшаговой тени, отвергнутых по программе; пишется ОДИН РАЗ ПРИ СТАРТЕ
# службы, а не по чьей-то сделке. Каждый деплой добавляет полный каталог
# заново, поэтому в счёте сигналов такие строки давали сотни там, где
# настоящих сигналов единицы. Считаются отдельным полем, а не выбрасываются
# молча: по нему видно, сколько раз служба стартовала.
СТАДИИ_НЕ_СИГНАЛ = ("leg_pool_skip",)


def торгующие_группы() -> tuple[tuple[str, ...], str]:
    """Кто реально торгует -- из файла групп. Вторым значением -- откуда взято."""
    # ГДЕ ИСКАТЬ МОДУЛЬ ГРУПП. Зонд на хосте живёт в /tmp/vech один, без
    # analysis/ рядом, поэтому один путь "рядом с файлом" давал бы запасной
    # список всегда и молча. Порядок: каталог кода службы из окружения, обычное
    # место службы, затем analysis/ репозитория.
    for кат in (os.environ.get("BLOOM_CODE_DIR", "").strip(),
                 "/home/bot/bloom_executor",
                 os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "..", "analysis")):
        if кат and os.path.isdir(кат) and кат not in sys.path:
            sys.path.insert(0, кат)
    try:
        import bloom_source_groups as SG  # noqa: PLC0415
        гр = SG.загрузить()
    except Exception as e:  # noqa: BLE001 -- любая неясность: запасной список
        return ТОРГУЮЩИЕ_ЗАПАС, f"запасной список: файл групп не прочитан ({e})"
    # КЛЮЧ ПО-РУССКИ. bloom_source_groups.загрузить() отдаёт "политики";
    # "policies" -- имя из отчёта живучести, и по нему всегда выходил запасной
    # список, то есть правка про lane_trades молча не работала.
    политики = None
    if isinstance(гр, dict):
        политики = гр.get("политики") or гр.get("policies")
    if not isinstance(политики, dict) or not политики:
        return ТОРГУЮЩИЕ_ЗАПАС, "запасной список: в файле групп нет политик"
    живые = tuple(sorted(имя for имя, п in политики.items()
                          if isinstance(п, dict) and п.get("lane_trades")))
    if not живые:
        return (), "файл групп: ни одна группа не торгует"
    return живые, "файл групп: lane_trades"


def строки(путь: str):
    откр = gzip.open if путь.endswith(".gz") else open
    try:
        with откр(путь, "rt", encoding="utf-8", errors="replace") as ф:
            for с in ф:
                с = с.strip()
                if с:
                    yield с
    except OSError:
        return


def момент(текст: str) -> float:
    т = (текст or "").strip()
    if not т:
        return time.time() - 86400.0
    if т.startswith("-") and т.endswith(("h", "m")):
        ч = float(т[1:-1])
        return time.time() - ч * (3600.0 if т.endswith("h") else 60.0)
    for формат in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return float(calendar.timegm(time.strptime(т, формат)))
        except ValueError:
            continue
    return time.time() - 86400.0


def время_записи(з: dict):
    """Время записи решения -- по полю, которое РЕАЛЬНО пишет служба.

    ПОЧЕМУ ЭТО ОТДЕЛЬНАЯ ФУНКЦИЯ. Первая версия смотрела только t_recv_ts, а
    записыватель решений (bloom_exec_state.append_jsonl_fsync) ставит в каждую
    строку ts_utc -- ISO-строку UTC. Из-за этого окно --since НЕ ПРИМЕНЯЛОСЬ
    НИ К ОДНОЙ СТРОКЕ: все записи попадали в "без_времени" и считались целиком
    по всему хранимому журналу. Числа выглядели как суточные, а были за всё
    время -- на таких числах нельзя решать, включать ли строителя.

    Порядок полей: ts_utc (пишет служба всегда), затем числовые метки
    детектора. Возвращает epoch-секунды или None, если времени нет вовсе.
    """
    зн = з.get("ts_utc")
    if isinstance(зн, str) and зн:
        # ДОЛИ СЕКУНДЫ СЧИТАЕМ САМИ: calendar.timegm берёт struct_time, а в нём
        # дробной части нет вовсе -- через strptime("%f") она молча теряется.
        целое, дробь = зн, 0.0
        if "." in зн and зн.endswith("Z"):
            целое, _, хвост = зн[:-1].partition(".")
            целое += "Z"
            try:
                дробь = float("0." + хвост)
            except ValueError:
                дробь = 0.0
        for формат in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ"):
            try:
                return float(calendar.timegm(time.strptime(целое, формат))) + дробь
            except ValueError:
                continue
    for поле in ("ts", "t_recv_ts", "t_decide_ts"):
        зн = з.get(поле)
        if isinstance(зн, (int, float)):
            return float(зн)
    return None


def группа_записи(з: dict) -> str:
    for поле in ("lane_group", "group", "source_task"):
        зн = з.get(поле)
        if isinstance(зн, str) and зн:
            return зн
    return "группы в записи нет"


def группа_по_источнику() -> tuple:
    """(функция адрес->группа, откуда взято). В записи ТЕНИ группы нет вовсе.

    Запись теневого разбора собирается из stage/signature/mint/source плюс ответ
    shadow_build -- полей lane_group/group/source_task там НЕТ, и группа_записи на
    ней всегда отвечала бы "группы в записи нет". Поэтому группа берётся по
    ИСТОЧНИКУ из файла групп -- того же, что читает гейт полосы. Не вышло взять
    файл -- возвращаем None и говорим причину, а не подставляем "off" молча:
    "off" означает "не торгуем", и спутать его с "не знаем" нельзя.
    """
    for кат in (os.environ.get("BLOOM_CODE_DIR", "").strip(),
                 "/home/bot/bloom_executor",
                 os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "..", "analysis")):
        if not кат or not os.path.isdir(кат):
            continue
        if кат not in sys.path:
            sys.path.insert(0, кат)
        try:
            import bloom_source_groups as SG  # noqa: PLC0415

            по_адресу = dict(SG.загрузить().get("по_адресу") or {})

            def _гр(а, _карта=по_адресу):
                # "OFF" И "АДРЕСА НЕТ В ФАЙЛЕ" -- РАЗНЫЕ ОТВЕТЫ. SG.группа на
                # незнакомом адресе отдаёт "off" по замыслу гейта ("не торгуем
                # ничем"), но в списке для Code-2 это прочиталось бы как "источник
                # в группе off", то есть как факт о настройке. Поэтому отсутствие
                # называется словами.
                if not а:
                    return None, "источника в записи нет"
                if а not in _карта:
                    return None, "адреса нет в файле групп"
                return _карта[а], "файл групп по источнику"

            return _гр, f"файл групп через {кат}"
        except Exception:  # noqa: BLE001, S112
            continue
    return None, "файл групп не прочитан: группа по источнику неизвестна"


def выписка_теней(журнал_каталог: str, с_ts: float, программы: dict) -> dict:
    """Подписи сигналов теневого разбора, которые НЕ собрались, по названным программам.

    ЗАЧЕМ (поручение владельца 30.09, п.4 для Code-2): по двум программам без
    строителя нужен список подписей, чтобы Code-2 закрыла вопрос "пул токена или
    нога маршрута". Список, а не число: по числу этого не решить.

    Поля берутся ИЗ ЗАПИСИ и ничего не досочиняются. Слота в записи тени нет --
    в детекторе она собирается из stage/signature/mint/source плюс ответ
    shadow_build; поэтому слот отдаётся как None с названной причиной, а не
    подставляется из соседней строки наугад.

    Только чтение журнала. Ни цепи, ни подписи, ни отправки.
    """
    по_источнику, откуда_группа = группа_по_источнику()
    из_ = {"с": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(с_ts)),
            "программы": dict(программы), "строк_просмотрено": 0,
            "подходящих_записей": 0, "собранных_пропущено": 0,
            "группа_откуда": откуда_группа,
            "по_программам": {}, "строки": [],
            "почему_нет_слота": ("в записи теневого разбора слота нет: она "
                                  "состоит из stage/signature/mint/source и ответа "
                                  "shadow_build -- слот туда не пишется"),
            "только_чтение": True, "why_not": None}
    пути = sorted(glob.glob(os.path.join(журнал_каталог, "decisions.jsonl*")))
    if not пути:
        из_["why_not"] = f"журнала решений нет: {журнал_каталог}"
        return из_
    for путь in пути:
        try:
            if os.path.getmtime(путь) < с_ts - 86400:
                continue
        except OSError:
            pass
        for с in строки(путь):
            из_["строк_просмотрено"] += 1
            адрес = None
            for а in программы:
                if а in с:
                    адрес = а
                    break
            if адрес is None:
                continue
            try:
                з = json.loads(с)
            except ValueError:
                continue
            if not isinstance(з, dict):
                continue
            if str(з.get("stage") or "") != "shadow":
                continue
            if str(з.get("pool_program") or "") != адрес:
                continue
            т = время_записи(з)
            if т is not None and т < с_ts:
                continue
            # СОБРАЛАСЬ -- НЕ НАШ СЛУЧАЙ. Вопрос владельца про not_built.
            if з.get("ok") and з.get("tx_base64"):
                из_["собранных_пропущено"] += 1
                continue
            из_["подходящих_записей"] += 1
            имя = программы[адрес]
            св = из_["по_программам"].setdefault(имя, {"всего": 0, "причины": {}})
            св["всего"] += 1
            поч = str(з.get("why_not") or "без причины")[:120]
            св["причины"][поч] = int(св["причины"].get(поч, 0)) + 1
            гр_из_записи = группа_записи(з)
            if гр_из_записи != "группы в записи нет":
                гр_знач, гр_откуда = гр_из_записи, "из записи"
            elif по_источнику is not None:
                гр_знач, гр_откуда = по_источнику(з.get("source"))
            else:
                гр_знач, гр_откуда = None, откуда_группа
            из_["строки"].append({
                "podpis": з.get("signature"),
                "slot": з.get("slot"),
                "mint": з.get("mint"),
                "gruppa": гр_знач,
                "gruppa_otkuda": гр_откуда,
                "kotirovka": з.get("quote_mint"),
                "programma": адрес,
                "imya_programmy": имя,
                "metka_pula": з.get("pool_label"),
                "istochnik": з.get("source"),
                "why_not": поч,
                "ts_utc": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(т))
                            if т is not None else None)})
    for св in из_["по_программам"].values():
        св["причины"] = dict(sorted(св["причины"].items(), key=lambda т_: -т_[1])[:10])
    return из_


def посчитать(журнал_каталог: str, с_ts: float) -> dict:
    торгующие, откуда_торгующие = торгующие_группы()
    итог = {"с": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(с_ts)),
             "строк_просмотрено": 0, "записей_со_строителем": 0,
             "без_времени_всего": 0,
             "каталог_ног_при_старте": {},
             "торгующие_группы": list(торгующие),
             "торгующие_откуда": откуда_торгующие,
             "по_строителям": {}, "только_чтение": True, "why_not": None}
    пути = sorted(glob.glob(os.path.join(журнал_каталог, "decisions.jsonl*")))
    if not пути:
        итог["why_not"] = f"журнала решений нет: {журнал_каталог}"
        return итог
    for путь in пути:
        # Архив старше окна не разжимаем: журнал идёт на сотни мегабайт.
        try:
            if os.path.getmtime(путь) < с_ts - 86400:
                continue
        except OSError:
            pass
        for с in строки(путь):
            итог["строк_просмотрено"] += 1
            # ДЕШЁВЫЙ ОТБОР: есть ли в строке вообще адрес какого-то строителя.
            адрес = None
            for а in СТРОИТЕЛИ:
                if а in с:
                    адрес = а
                    break
            if адрес is None:
                continue
            try:
                з = json.loads(с)
            except ValueError:
                continue
            if not isinstance(з, dict):
                continue
            т = время_записи(з)
            if т is not None and т < с_ts:
                continue
            ст_ = str(з.get("stage") or з.get("action") or "?")[:24]
            имя = СТРОИТЕЛИ[адрес]
            if ст_ in СТАДИИ_НЕ_СИГНАЛ:
                # НЕ СИГНАЛ. leg_pool_skip пишется ОДИН РАЗ ПРИ СТАРТЕ службы --
                # по строке на каждую отвергнутую ногу двухшаговой тени
                # (bloom_detector, блок инициализации кэша ног). За сутки с
                # несколькими деплоями это сотни строк на тип, и в счёте
                # "сигналов" они давали 241 clmm вместо одного настоящего.
                кат = итог["каталог_ног_при_старте"]
                кат[имя] = int(кат.get(имя, 0)) + 1
                continue
            итог["записей_со_строителем"] += 1
            св = итог["по_строителям"].setdefault(
                имя, {"всего": 0, "по_группам": {}, "торгующих_групп": 0,
                       "стадии": {}, "без_времени": 0})
            св["всего"] += 1
            if т is None:
                # Строка без времени вовсе: окно к ней не применить, и молчать
                # об этом нельзя -- иначе счёт "за сутки" тихо вберёт старое.
                св["без_времени"] += 1
                итог["без_времени_всего"] += 1
            гр = группа_записи(з)
            св["по_группам"][гр] = int(св["по_группам"].get(гр, 0)) + 1
            if гр in торгующие:
                св["торгующих_групп"] += 1
            св["стадии"][ст_] = int(св["стадии"].get(ст_, 0)) + 1
    for св in итог["по_строителям"].values():
        св["по_группам"] = dict(sorted(св["по_группам"].items(),
                                        key=lambda т_: -т_[1])[:12])
        св["стадии"] = dict(sorted(св["стадии"].items(), key=lambda т_: -т_[1])[:8])
    итог["по_строителям"] = dict(sorted(итог["по_строителям"].items(),
                                         key=lambda т_: -т_[1]["всего"]))
    return итог


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--since", default="", help="'2026-09-28T00:21:00Z', '-24h' или пусто")
    р.add_argument("--out", default="")
    р.add_argument("--vypisat-programmy", default="",
                   help="адреса программ через запятую: выписать подписи НЕсобранных теней")
    р.add_argument("--vypisat-out", default="",
                   help="куда положить выписку (JSON)")
    р.add_argument("--self-test", action="store_true")
    а = р.parse_args()
    if а.self_test:
        return самопроверка()
    с_ts = момент(а.since)
    # ВЫПИСКА -- ОТДЕЛЬНЫЙ РЕЖИМ. Счёт сигналов и список подписей отвечают на
    # разные вопросы, и смешивать их в одном выводе значит заставить читателя
    # угадывать, о чём число.
    if а.vypisat_programmy.strip():
        имена = {}
        for а_ in а.vypisat_programmy.replace(";", ",").split(","):
            а_ = а_.strip()
            if not а_:
                continue
            имена[а_] = СТРОИТЕЛИ.get(а_) or а_[:8]
        if not имена:
            print("СБОЙ: в --vypisat-programmy не нашлось ни одного адреса")
            return 1
        в = выписка_теней(а.state_dir, с_ts, имена)
        краткое = {к: v for к, v in в.items() if к != "строки"}
        краткое["строк_в_выписке"] = len(в["строки"])
        print(json.dumps(краткое, ensure_ascii=False, indent=1))
        for р_ in в["строки"][:20]:
            print(f"  {р_['ts_utc']} {р_['imya_programmy']} {(р_['podpis'] or '')[:24]} "
                  f"минт {(р_['mint'] or '')[:12]} группа {р_['gruppa']} "
                  f"котировка {(р_['kotirovka'] or 'нет')[:12]} -- {р_['why_not'][:70]}")
        if а.vypisat_out:
            with open(а.vypisat_out, "w", encoding="utf-8") as ф:
                json.dump(в, ф, ensure_ascii=False, indent=1)
            print(f"выписка записана: {а.vypisat_out}")
        return 0
    итог = посчитать(а.state_dir, с_ts)
    print(json.dumps(итог, ensure_ascii=False, indent=1)[:6000])
    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump(итог, ф, ensure_ascii=False, indent=1)
    return 0


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

    import tempfile  # noqa: PLC0415

    ЛЛ = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
    with tempfile.TemporaryDirectory() as d:
        с_ = os.path.join(d, "decisions.jsonl")
        # ФОРМА ЗАПИСИ -- КАК У СЛУЖБЫ: ts_utc, а не t_recv_ts. Предыдущая
        # самопроверка писала t_recv_ts, поэтому окно в ней "работало", а на
        # живом журнале не применялось ни к одной строке.
        with open(с_, "w", encoding="utf-8") as ф:
            ф.write(json.dumps({"ts_utc": "2026-09-30T01:00:00Z", "stage": "lane_gate",
                                 "lane_group": "batch5", "pool_program": ЛЛ},
                                ensure_ascii=False) + "\n")
            ф.write(json.dumps({"ts_utc": "2026-09-30T01:00:01Z", "stage": "shadow",
                                 "pool_program": ЛЛ}, ensure_ascii=False) + "\n")
            ф.write(json.dumps({"ts_utc": "2026-09-29T23:00:00Z", "stage": "lane_gate",
                                 "lane_group": "batch5", "pool_program": ЛЛ,
                                 "mint": "STAROE"}, ensure_ascii=False) + "\n")
            ф.write(json.dumps({"ts_utc": "2026-09-30T01:00:02Z", "stage": "lane_gate",
                                 "lane_group": "log_only"}, ensure_ascii=False) + "\n")
        о = посчитать(d, момент("2026-09-30T00:00:00Z"))
        лл = о["по_строителям"].get("launchlab") or {}
        chk(f"строитель найден по адресу, записей {лл.get('всего')}",
            лл.get("всего") == 2, о)
        chk(f"торгующих групп {лл.get('торгующих_групп')} из них",
            лл.get("торгующих_групп") == 1, лл)
        chk("тень считается отдельной стадией, а не группой",
            лл.get("стадии", {}).get("shadow") == 1, лл.get("стадии"))
        chk("запись вне окна не взята (по ts_utc)",
            "STAROE" not in json.dumps(о, ensure_ascii=False), о)
        chk("все взятые записи со временем, без_времени_всего 0",
            о.get("без_времени_всего") == 0, о.get("без_времени_всего"))
        chk("ts_utc разбирается как UTC",
            время_записи({"ts_utc": "2026-09-28T00:21:00Z"}) == 1790554860.0,
            время_записи({"ts_utc": "2026-09-28T00:21:00Z"}))
        chk("ts_utc с долями секунды разбирается",
            время_записи({"ts_utc": "2026-09-28T00:21:00.500Z"}) == 1790554860.5,
            время_записи({"ts_utc": "2026-09-28T00:21:00.500Z"}))
        chk("числовое ts берётся, когда ts_utc нет",
            время_записи({"ts": 123.5}) == 123.5, время_записи({"ts": 123.5}))
        chk("t_recv_ts берётся, когда нет ни ts_utc, ни ts",
            время_записи({"t_recv_ts": 7.0}) == 7.0, время_записи({"t_recv_ts": 7.0}))
        chk("совсем без времени -- None, и это видно в своде",
            время_записи({"stage": "x"}) is None, время_записи({"stage": "x"}))
        chk("битый ts_utc не выдаётся за время",
            время_записи({"ts_utc": "ne-vremya"}) is None,
            время_записи({"ts_utc": "ne-vremya"}))
        chk("строка без адреса строителя не считается",
            "log_only" not in json.dumps(о, ensure_ascii=False), о)
        chk("каталог ног при старте не считается сигналом",
            (о["по_строителям"].get("launchlab") or {}).get("всего") == 2
            and о.get("каталог_ног_при_старте") == {}, о)
        chk("торгующие группы взяты из файла групп либо честно названы запасом",
            isinstance(о.get("торгующие_группы"), list)
            and isinstance(о.get("торгующие_откуда"), str)
            and о["торгующие_откуда"].startswith(("файл групп", "запасной список")),
            (о.get("торгующие_группы"), о.get("торгующие_откуда")))
        chk("в запасном списке sniper_src остался -- но он именно запасной",
            "sniper_src" in ТОРГУЮЩИЕ_ЗАПАС, ТОРГУЮЩИЕ_ЗАПАС)
        с2 = os.path.join(d, "kat")
        os.makedirs(с2, exist_ok=True)
        with open(os.path.join(с2, "decisions.jsonl"), "w", encoding="utf-8") as ф:
            for _ in range(3):
                ф.write(json.dumps({"ts_utc": "2026-09-30T01:00:00Z",
                                     "stage": "leg_pool_skip",
                                     "pool_program": ЛЛ}, ensure_ascii=False) + "\n")
            ф.write(json.dumps({"ts_utc": "2026-09-30T01:00:05Z", "stage": "shadow",
                                 "pool_program": ЛЛ}, ensure_ascii=False) + "\n")
        о2 = посчитать(с2, момент("2026-09-30T00:00:00Z"))
        chk("leg_pool_skip в каталог, а не в сигналы",
            (о2["по_строителям"].get("launchlab") or {}).get("всего") == 1
            and о2.get("каталог_ног_при_старте", {}).get("launchlab") == 3, о2)
        chk("записей_со_строителем не включает каталог ног",
            о2.get("записей_со_строителем") == 1, о2.get("записей_со_строителем"))
        chk("ключ политик читается по-русски",
            '"политики"' in io.open(__file__, encoding="utf-8").read(),
            "политики не ищется")
        chk("время UTC разбирается как UTC",
            момент("2026-09-28T00:21:00Z") == 1790554860.0,
            момент("2026-09-28T00:21:00Z"))
    # ВЫПИСКА ТЕНЕЙ: берёт только stage=shadow нужной программы и только
    # НЕсобранные. Проверяется на поддельном журнале -- иначе пустой список в
    # прогоне нельзя отличить от "фильтр не тот".
    import tempfile  # noqa: PLC0415
    ПР_A = "ojh19ojaKduoJZuaJADhcVGp4xt1TcdAvZmpVsCorch"
    ПР_B = "TessVdML9pBGgG9yGks7o4HewRaXVAMuoVj4x83GLQH"
    with tempfile.TemporaryDirectory() as кат:
        with io.open(os.path.join(кат, "decisions.jsonl"), "w", encoding="utf-8") as ф:
            # 1. Наш случай: тень этой программы, не собралась.
            ф.write(json.dumps({"ts_utc": "2026-09-30T10:00:00Z", "stage": "shadow",
                                 "signature": "ПОДПИСЬ1", "mint": "МИНТ1",
                                 "source": "ИСТ1", "lane_group": "lane_s0",
                                 "pool_program": ПР_A, "quote_mint": "USDC",
                                 "ok": False, "why_not": "нет участка SOL->Q"},
                                ensure_ascii=False) + "\n")
            # 2. Тень той же программы, но СОБРАЛАСЬ -- не наш случай.
            ф.write(json.dumps({"ts_utc": "2026-09-30T10:00:01Z", "stage": "shadow",
                                 "signature": "ПОДПИСЬ2", "mint": "МИНТ2",
                                 "pool_program": ПР_A, "ok": True,
                                 "tx_base64": "БАЗА"}, ensure_ascii=False) + "\n")
            # 3. Другая программа -- мимо фильтра.
            ф.write(json.dumps({"ts_utc": "2026-09-30T10:00:02Z", "stage": "shadow",
                                 "signature": "ПОДПИСЬ3", "pool_program": "ЧУЖАЯ",
                                 "ok": False}, ensure_ascii=False) + "\n")
            # 4. Не тень, а решение полосы той же программы -- мимо.
            ф.write(json.dumps({"ts_utc": "2026-09-30T10:00:03Z", "stage": "gate",
                                 "signature": "ПОДПИСЬ4", "pool_program": ПР_A,
                                 "ok": False}, ensure_ascii=False) + "\n")
            # 5. Вторая программа, не собралась -- наш случай.
            ф.write(json.dumps({"ts_utc": "2026-09-30T10:00:04Z", "stage": "shadow",
                                 "signature": "ПОДПИСЬ5", "mint": "МИНТ5",
                                 "pool_program": ПР_B, "ok": False,
                                 "why_not": "нога маршрута"}, ensure_ascii=False) + "\n")
        в = выписка_теней(кат, 0.0, {ПР_A: "corch", ПР_B: "tessera"})
        равно = [р_["podpis"] for р_ in в["строки"]]
        chk("в выписку попали только НЕсобранные тени нужных программ",
            равно == ["ПОДПИСЬ1", "ПОДПИСЬ5"], равно)
        chk("собранная тень посчитана отдельно, а не выброшена молча",
            в["собранных_пропущено"] == 1, в["собранных_пропущено"])
        chk("поля берутся из записи: минт, группа, котировка",
            в["строки"][0]["mint"] == "МИНТ1"
            and в["строки"][0]["gruppa"] == "lane_s0"
            and в["строки"][0]["kotirovka"] == "USDC", в["строки"][0])
        chk("слота в записи тени нет -- None с названной причиной, а не догадка",
            в["строки"][0]["slot"] is None and "слота нет" in в["почему_нет_слота"])
        chk("причины сгруппированы по программам",
            в["по_программам"]["corch"]["всего"] == 1
            and в["по_программам"]["tessera"]["всего"] == 1, в["по_программам"])

    print(f"самопроверка счёта сигналов по строителям: {всего - сбоев}/{всего} пройдено")
    return 1 if сбоев else 0


if __name__ == "__main__":
    sys.exit(main())
