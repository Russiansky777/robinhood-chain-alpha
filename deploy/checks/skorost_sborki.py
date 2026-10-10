#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ЧЕТЫРЕ ЧИСЛА РЯДОМ: та же полная сборка на четырёх машинах + железо хоста.

ЗАЧЕМ (слово владельца 10.10, п.1): "Хост: модель CPU, число ядер, steal и
загрузка в момент сделок, версия Python, что ещё крутится рядом... Ту же полную
сборку прогнать на бегунках AMS и Miami (ExtraVM) и в контейнере -- четыре
числа рядом. Если причина -- железо, назвать".

ОТКУДА ВОПРОС. Живьём 10.10 подшаг "сборка" на NL-хосте -- 5.3-6.5 мс, а в
контейнере агента та же сборка на том же образце -- 0.72 мс. Разница ~8x, и от
неё зависит вывод: переписывать код или менять машину. Угадывать нельзя, поэтому
здесь ОДИН модуль гоняет ОДИН и тот же замер где угодно и печатает числа.

ЧТО МЕРИТСЯ
* `--etalon` -- шесть замеров БЕЗ ЕДИНОЙ ЗАВИСИМОСТИ: цикл на чистом Python,
  словари, sha256, base64, json, сортировка. Они идут на любой машине, даже
  там, где нет ни solders, ни наших модулей, и дают честное "во сколько раз эта
  машина медленнее" отдельно от нашего кода.
* `--sborka` -- НАСТОЯЩАЯ полная сборка `bloom_own_send.собрать` и вариант из
  заготовки `вариант_из_zagotovki` на образце из data/c2_pool_samples. Нужны
  наши модули и solders; если их нет -- в отчёте стоит причина, а НЕ число.
* `--zhelezo` -- модель процессора, ядра, частоты, гипервизор, cgroup-квота,
  steal/занято/iowait за измеренное окно (разностью двух снимков /proc/stat, а
  не "с загрузки"), loadavg, давление CPU, версия Python и КТО ЕЩЁ ЕСТ ЯДРА в
  это же окно -- с маской разрешённых ядер каждого процесса.

ПОЧЕМУ ОБРАЗЕЦ -- НЕ КРИВАЯ PUMP.FUN. Живая полоса 10.10 работала по кривой
(6EF8rrecth...), но все шесть образцов кривой в репозитории БЕЗ СЕТИ не
котируются ("неоднозначная котировка"), то есть замер без узла на них
невозможен. Поэтому берётся образец pump AMM (pAMMBay6oce...): тот же прямой
сборщик `build_buy`, та же функция `собрать`, 0 обращений к узлу. Сравнение
машин от этого не страдает -- на всех машинах считается ОДИН И ТОТ ЖЕ образец.

Только чтение: /proc, образцы из репозитория, никакой сети, ни одной подписи в
цепь, ни одной записи на диск кроме --out.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parents[2]


def корень() -> Path:
    """Корень дерева с analysis/ и data/. Переопределяется --koren: на чужой
    машине модуль лежит в распакованном временном каталоге, а не в репозитории,
    и parents[2] указал бы в пустоту."""
    свой = (os.environ.get("SKOROST_KOREN") or "").strip()
    return Path(свой) if свой else КОРЕНЬ
ОБРАЗЕЦ_ПО_УМОЛЧАНИЮ = "data/c2_pool_samples/pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA.json"
ПОВТОРОВ_ПО_УМОЛЧАНИЮ = 31
# ЧТО ЕЩЁ ДОЛЖНО ЛЕЖАТЬ РЯДОМ, КРОМЕ analysis/ И ОБРАЗЦА. Найдено прогоном
# 10.10 23:25Z: на трёх машинах из четырёх сборка отказала
# "FileNotFoundError", и строка была обрезана ровно до "data/solana_buyer_2".
# Весь дефицит -- один файл 6 КБ с метками площадок, который c2_pool_programs
# читает при импорте. Поэтому он проверяется ДО замера и называется по имени.
НУЖНЫЕ_ФАЙЛЫ = (
    "data/solana_buyer_200/prior/current/buyer_100/dex_labels.json",
)
СЕКУНД_ОКНА = 10.0
# НОНС-ПАРА ДЛЯ ЗАМЕРА. Счёт настоящий (он же в самопроверках полосы), но здесь
# в цепь ничего не уходит: сборка только строит байты.
НОНС_ЗАМЕРА = "5ULtHkLzZmLQpMtvhkoJJoJkvYCJqCzZ8wHuv2LSg9Pk"
ЧАЕВЫЕ_ЗАМЕРА = ("jitodontfront111111111111111111111111111111", 1_000_000)
НАШИ_СЛУЖБЫ = ("bloom_detector", "bloom_seller", "bloom-detector", "bloom-seller",
               "c3_kapital_trekker", "ledger", "Runner.Listener", "Runner.Worker")


def _мс(список: list) -> dict:
    """Медиана/минимум/максимум в мс -- и число повторов, чтобы верить числу."""
    if not список:
        return {"med": None, "min": None, "max": None, "n": 0}
    return {"med": round(statistics.median(список), 3),
            "min": round(min(список), 3),
            "max": round(max(список), 3),
            "n": len(список)}


def замер(функция, повторов: int) -> dict:
    """Повторить и вернуть мс. Первый проход ГРЕЕТ и в счёт не идёт."""
    try:
        функция()
    except Exception as сбой:  # noqa: BLE001
        return {"med": None, "min": None, "max": None, "n": 0,
                "why_not": f"{type(сбой).__name__}: {str(сбой)[:200]}"}
    ряд = []
    for _ in range(max(1, int(повторов))):
        т = time.perf_counter()
        функция()
        ряд.append((time.perf_counter() - т) * 1000.0)
    из_ = _мс(ряд)
    из_["why_not"] = None
    return из_


# ---------------------------------------------------------------- ЭТАЛОН

def _эт_цикл() -> int:
    s = 0
    for i in range(200_000):
        s += (i * 3) % 7
    return s


def _эт_словари() -> int:
    д = {}
    for i in range(50_000):
        д[f"k{i & 4095}"] = д.get(f"k{i & 4095}", 0) + i
    return len(д)


def _эт_sha256() -> str:
    кусок = b"x" * 65536
    х = hashlib.sha256()
    for _ in range(64):  # 4 МиБ
        х.update(кусок)
    return х.hexdigest()


def _эт_base64() -> int:
    кусок = b"y" * (1 << 20)
    return len(base64.b64encode(кусок))


_ЭТ_СТРУКТУРА = [{"a": i, "b": f"s{i}", "c": [i, i + 1, i + 2]} for i in range(2000)]


def _эт_json() -> int:
    т = json.dumps(_ЭТ_СТРУКТУРА)
    return len(json.loads(т))


def _эт_сортировка() -> int:
    ряд = [(i * 2654435761) & 0xFFFFFFFF for i in range(200_000)]
    ряд.sort()
    return ряд[0]


ЭТАЛОНЫ = (("cikl_python", _эт_цикл), ("slovari", _эт_словари),
            ("sha256_4mib", _эт_sha256), ("base64_1mib", _эт_base64),
            ("json_2000", _эт_json), ("sortirovka_200k", _эт_сортировка))


def эталон(повторов: int = 7) -> dict:
    """Шесть замеров без единой зависимости -- на любой машине."""
    из_ = {}
    for имя, фн in ЭТАЛОНЫ:
        из_[имя] = замер(фн, повторов)
    return из_


# ---------------------------------------------------------------- ЖЕЛЕЗО

def _чтение(путь: str) -> str:
    try:
        with open(путь, encoding="utf-8", errors="replace") as ф:
            return ф.read()
    except OSError:
        return ""


def процессор() -> dict:
    """Модель, ядра, частоты, гипервизор -- из /proc и /sys, без утилит."""
    из_ = {"model": None, "yader": os.cpu_count(), "mhz": [], "mhz_max_khz": None,
            "flagi_est": [], "kesh_kib": None, "gipervizor": None,
            "arhitektura": platform.machine(), "why_not": None}
    текст = _чтение("/proc/cpuinfo")
    if not текст:
        из_["why_not"] = "/proc/cpuinfo не читается"
        return из_
    флаги = set()
    for строка in текст.splitlines():
        if ":" not in строка:
            continue
        ключ, знач = строка.split(":", 1)
        ключ, знач = ключ.strip().lower(), знач.strip()
        if ключ == "model name" and из_["model"] is None:
            из_["model"] = знач
        elif ключ == "cpu mhz":
            try:
                из_["mhz"].append(round(float(знач), 1))
            except ValueError:
                pass
        elif ключ == "cache size" and из_["kesh_kib"] is None:
            из_["kesh_kib"] = знач
        elif ключ == "flags":
            флаги |= set(знач.split())
    # ТОЛЬКО ТЕ ФЛАГИ, ЧТО МЕНЯЮТ СКОРОСТЬ НАШЕГО КОДА: хеши и широкие регистры.
    из_["flagi_est"] = sorted(ф for ф in ("avx", "avx2", "avx512f", "sha_ni",
                                           "aes", "hypervisor", "constant_tsc")
                               if ф in флаги)
    из_["gipervizor"] = "hypervisor" in флаги
    кгц = _чтение("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq").strip()
    if кгц.isdigit():
        из_["mhz_max_khz"] = int(кгц)
    # CGROUP-КВОТА. Урезанная квота даёт ровно тот же симптом, что слабое ядро,
    # и отличить её можно только этим файлом.
    квота = _чтение("/sys/fs/cgroup/cpu.max").strip() or _чтение(
        "/sys/fs/cgroup/cpu/cpu.cfs_quota_us").strip()
    из_["cgroup_cpu_max"] = квота or None
    из_["v_kontejnere"] = bool(os.path.exists("/.dockerenv")
                                or "docker" in _чтение("/proc/1/cgroup")
                                or "containerd" in _чтение("/proc/1/cgroup"))
    return из_


def питон() -> dict:
    return {"versiya": platform.python_version(),
            "realizaciya": platform.python_implementation(),
            "kompilyator": platform.python_compiler(),
            "ispolnyaemyj": sys.executable,
            "otladochnaya_sborka": hasattr(sys, "gettotalrefcount"),
            "trassirovka_vklyuchena": bool(sys.gettrace() or sys.getprofile()),
            "gc_porogi": __import__("gc").get_threshold()}


def _снимок_stat(путь: str = "/proc/stat") -> dict:
    """Первая строка /proc/stat -- такты с загрузки по всем ядрам."""
    for строка in _чтение(путь).splitlines():
        if строка.startswith("cpu "):
            ч = [int(x) for x in строка.split()[1:11] if x.isdigit()]
            имена = ("user", "nice", "system", "idle", "iowait", "irq",
                      "softirq", "steal", "guest", "guest_nice")
            из_ = dict(zip(имена, ч))
            из_["vsego"] = sum(ч[:8])
            return из_
    return {}


def доли_cpu(до: dict, после: dict) -> dict:
    """Проценты за ОКНО -- разностью снимков. /proc/stat с загрузки даёт
    среднее за недели и в минуту сделки ничего не говорит."""
    из_ = {"steal_proc": None, "zanyato_proc": None, "iowait_proc": None,
            "why_not": None}
    всего = int(после.get("vsego") or 0) - int(до.get("vsego") or 0)
    if всего <= 0:
        из_["why_not"] = "окно не дало тактов: снимки одинаковые"
        return из_
    простой = ((после.get("idle", 0) - до.get("idle", 0))
                + (после.get("iowait", 0) - до.get("iowait", 0)))
    из_["steal_proc"] = round((после.get("steal", 0) - до.get("steal", 0))
                               * 100.0 / всего, 4)
    из_["iowait_proc"] = round((после.get("iowait", 0) - до.get("iowait", 0))
                                * 100.0 / всего, 4)
    из_["zanyato_proc"] = round((всего - простой) * 100.0 / всего, 4)
    из_["taktov_v_okne"] = всего
    return из_


def _такты_процесса(pid: str) -> tuple:
    """utime+stime процесса, его имя, nice и маска разрешённых ядер."""
    стат = _чтение(f"/proc/{pid}/stat")
    if not стат or ")" not in стат:
        return None
    хвост = стат.rsplit(")", 1)[1].split()
    try:
        такты = int(хвост[11]) + int(хвост[12])
        найс = int(хвост[16])
    except (IndexError, ValueError):
        return None
    имя = стат.split("(", 1)[1].rsplit(")", 1)[0] if "(" in стат else pid
    # ПОЛНАЯ КОМАНДА, А НЕ comm: у наших служб comm -- просто "python3".
    команда = _чтение(f"/proc/{pid}/cmdline").replace("\0", " ").strip()
    ядра = None
    for строка in _чтение(f"/proc/{pid}/status").splitlines():
        if строка.startswith("Cpus_allowed_list:"):
            ядра = строка.split(":", 1)[1].strip()
    return такты, имя, команда[:160], найс, ядра


def кто_рядом(секунд: float, корень: str = "/proc") -> dict:
    """Кто ЕЩЁ ест ядра в том же окне -- тактами, а не на слово."""
    из_ = {"sekund": секунд, "processy": [], "nashi": {}, "why_not": None}
    def снимок() -> dict:
        д = {}
        try:
            пиды = [и for и in os.listdir(корень) if и.isdigit()]
        except OSError as сбой:
            из_["why_not"] = f"{корень} не читается: {сбой}"
            return д
        for пид in пиды:
            р = _такты_процесса(пид)
            if р:
                д[пид] = р
        return д
    до = снимок()
    if из_["why_not"]:
        return из_
    time.sleep(max(0.0, float(секунд)))
    после = снимок()
    тик = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    ядер = os.cpu_count() or 1
    ряд = []
    for пид, (такты, имя, команда, найс, ядра) in после.items():
        если_до = до.get(пид)
        if not если_до:
            continue
        дельта = такты - если_до[0]
        if дельта <= 0:
            continue
        сек = дельта / float(тик)
        ряд.append({"pid": int(пид), "imya": имя, "komanda": команда,
                     "nice": найс, "yadra_razresheny": ядра,
                     "cpu_proc_odnogo_yadra": round(сек * 100.0 / max(1e-9, секунд), 2),
                     "cpu_proc_mashiny": round(сек * 100.0 / max(1e-9, секунд * ядер), 2)})
    ряд.sort(key=lambda с: -с["cpu_proc_odnogo_yadra"])
    из_["processy"] = ряд[:12]
    for с in ряд:
        for метка in НАШИ_СЛУЖБЫ:
            if метка in с["komanda"] or метка in с["imya"]:
                из_["nashi"].setdefault(метка, []).append(с)
    из_["vseh_s_rashodom"] = len(ряд)
    из_["summa_proc_mashiny"] = round(sum(с["cpu_proc_mashiny"] for с in ряд), 2)
    return из_


def железо(секунд: float = СЕКУНД_ОКНА) -> dict:
    """Железо и нагрузка за ОДНО окно: доли CPU и соседи считаются вместе."""
    из_ = {"processor": процессор(), "python": питон(),
            "uptime_s": None, "loadavg": None, "davlenie_cpu": None,
            "pamyat_kib": None}
    ап = _чтение("/proc/uptime").split()
    if ап:
        try:
            из_["uptime_s"] = round(float(ап[0]), 1)
        except ValueError:
            pass
    ла = _чтение("/proc/loadavg").split()
    if len(ла) >= 3:
        из_["loadavg"] = [float(ла[0]), float(ла[1]), float(ла[2])]
    из_["davlenie_cpu"] = (_чтение("/proc/pressure/cpu").strip() or None)
    for строка in _чтение("/proc/meminfo").splitlines():
        if строка.startswith("MemTotal:"):
            из_["pamyat_kib"] = int(строка.split()[1])
    до = _снимок_stat()
    соседи = кто_рядом(секунд)
    после = _снимок_stat()
    из_["doli_cpu_za_okno"] = доли_cpu(до, после)
    из_["kto_ryadom"] = соседи
    return из_


# ---------------------------------------------------------------- СБОРКА

def _образец(путь: str) -> dict:
    """Первый образец из файла, который БЕЗ СЕТИ даёт успешную базу."""
    из_ = {"ok": False, "why_not": None, "fajl": путь, "n": None,
            "obrazec": None, "baza": None}
    п = Path(путь)
    if not п.is_absolute():
        п = корень() / путь
    try:
        пачка = json.loads(п.read_text(encoding="utf-8"))
    except (OSError, ValueError) as сбой:
        из_["why_not"] = f"образец не читается: {type(сбой).__name__}: {сбой}"
        return из_
    if not isinstance(пачка, list) or not пачка:
        из_["why_not"] = "в файле образцов нет списка"
        return из_
    из_["vsego_v_fajle"] = len(пачка)
    из_["ok"] = True
    из_["pachka"] = пачка
    return из_


def сборка(*, путь_образца: str = ОБРАЗЕЦ_ПО_УМОЛЧАНИЮ,
            повторов: int = ПОВТОРОВ_ПО_УМОЛЧАНИЮ) -> dict:
    """НАСТОЯЩАЯ полная сборка и вариант из заготовки -- одним и тем же кодом.

    Сеть запрещена жёстко: rpc_call бросает. Если образец требует узла, он
    отбрасывается, и берётся следующий; если не подошёл ни один -- в ответе
    стоит причина, а не число.
    """
    из_ = {"ok": False, "why_not": None, "fajl": путь_образца, "n": None,
            "polnaya_ms": None, "iz_zagotovki_ms": None, "size": None,
            "pool_program": None, "sborshchik": None, "setevyh_popytok": 0}
    # ФАЙЛЫ ДАННЫХ -- ДО ИМПОРТА. Их нехватка видна без модулей, и назвать её
    # надо именем файла, а не обрезанным ModuleNotFoundError.
    нет_файлов = [ф for ф in НУЖНЫЕ_ФАЙЛЫ if not (корень() / ф).exists()]
    if нет_файлов:
        из_["why_not"] = ("рядом с кодом нет нужных файлов данных: "
                           + ", ".join(нет_файлов)
                           + f" (корень {корень()})")
        из_["net_fajlov"] = нет_файлов
        return из_
    корень_кода = str(корень() / "analysis")
    if корень_кода not in sys.path:
        sys.path.insert(0, корень_кода)
    try:
        import bloom_exec_state as ST  # noqa: PLC0415
        import bloom_own_send as OS  # noqa: PLC0415
    except Exception as сбой:  # noqa: BLE001
        из_["why_not"] = (f"модули полосы не загрузились: "
                           f"{type(сбой).__name__}: {str(сбой)[:200]}")
        return из_
    обр_ = _образец(путь_образца)
    if not обр_["ok"]:
        из_["why_not"] = обр_["why_not"]
        return из_
    # ВЕРСИИ -- В ОТЧЁТ. Четыре машины сравнимы только при одном и том же коде
    # и одной и той же сборке solders: байты строит Rust, и его версия -- часть
    # ответа "почему здесь медленнее".
    try:
        import solders  # noqa: PLC0415
        из_["solders"] = getattr(solders, "__version__", None) or "без __version__"
    except Exception as сбой:  # noqa: BLE001
        из_["solders"] = f"не импортируется: {type(сбой).__name__}"
    из_["koren_koda"] = корень_кода
    наш = ST.EXECUTOR_WALLET
    нонс = (НОНС_ЗАМЕРА, наш)
    чаевые = [ЧАЕВЫЕ_ЗАМЕРА]
    счёт = {"n": 0}

    def _без_сети(метод, *_а, **_к):
        счёт["n"] += 1
        raise RuntimeError(f"замер без сети: запрошен {метод}")

    причины = []
    for н, обр in enumerate(обр_["pachka"]):
        сам = обр.get("tx") or обр
        общ = dict(tx_источника=сам, источник=обр.get("source") or "",
                    минт=обр.get("mint") or "", наш_кошелёк=наш,
                    лампорты=10_000_000, семя="ZAMER_SKOROSTI",
                    rpc_call=_без_сети)
        try:
            база = OS.собрать(**общ)
        except Exception as сбой:  # noqa: BLE001
            причины.append(f"#{н}: {type(сбой).__name__}")
            continue
        if not база.get("ok") or not (база.get("_zagotovka") or None):
            причины.append(f"#{н}: {(база.get('why_not') or 'нет заготовки')[:60]}")
            continue
        полная = замер(lambda: OS.собрать(**общ, чаевые_списком=чаевые,
                                            нонс=нонс), повторов)
        короткая = замер(lambda: OS.вариант_из_zagotovki(
            база, сам, чаевые_списком=чаевые, чаевые_вперёд=False,
            нонс=нонс), повторов)
        из_.update(ok=True, n=н, polnaya_ms=полная, iz_zagotovki_ms=короткая,
                    size=база.get("size"), pool_program=база.get("pool_program"),
                    sborshchik=(база.get("_zagotovka") or {}).get("sborshchik"),
                    setevyh_popytok=счёт["n"], povtorov=повторов)
        # ВО СКОЛЬКО РАЗ ЗАГОТОВКА ДЕШЕВЛЕ -- ЧИСЛОМ: ровно это и есть смысл
        # правки "шесть сборок в одну", и ровно это надо сравнивать между машин.
        if полная.get("med") and короткая.get("med"):
            из_["vygoda_raz"] = round(полная["med"] / короткая["med"], 2)
            из_["shest_polnyh_ms"] = round(полная["med"] * 6, 2)
            из_["shest_iz_zagotovki_ms"] = round(короткая["med"] * 6, 2)
        return из_
    из_["why_not"] = ("ни один образец не собрался без сети: "
                       + "; ".join(причины[:6]))
    return из_


# ---------------------------------------------------------------- ЧЕТЫРЕ РЯДОМ

# НАСКОЛЬКО ОТНОШЕНИЯ ДОЛЖНЫ СОЙТИСЬ, ЧТОБЫ СКАЗАТЬ "ЭТО ЖЕЛЕЗО". Если машина
# вообще медленнее во столько же раз, во сколько медленнее наша сборка, причина
# -- машина. Если сборка отстаёт заметно сильнее общего эталона, дело не в
# железе, а в нашем коде или окружении. Порог 0.35 -- это "в пределах трети":
# эталон и сборка грузят процессор по-разному, и требовать совпадения один в
# один было бы подгонкой.
РАЗБЕГ_ОТНОШЕНИЙ = 0.35


def _имя_машины(свод_: dict) -> str:
    п = ((свод_.get("zhelezo") or {}).get("processor") or {})
    return f"{свод_.get('mashina')} / {п.get('model')} / {п.get('yader')} яд."


def таблица(своды: dict) -> dict:
    """Четыре числа рядом и вывод: объясняет ли железо разницу.

    `своды` -- {имя машины: свод этой машины}. Отношения считаются к САМОЙ
    БЫСТРОЙ машине набора, а не к какой-то назначенной: иначе вывод зависел бы
    от того, кого назвали образцом.
    """
    из_ = {"mashin": 0, "ryady": [], "why_not": None}
    ряды = []
    for имя, с in (своды or {}).items():
        if not isinstance(с, dict):
            continue
        сб = с.get("sborka") or {}
        эт = с.get("etalon") or {}
        ж = с.get("zhelezo") or {}
        п = ж.get("processor") or {}
        ряды.append({
            "mashina": имя,
            "uzel": с.get("mashina"),
            "cpu": п.get("model"),
            "yader": п.get("yader"),
            "mhz": (п.get("mhz") or [None])[0],
            "python": (ж.get("python") or {}).get("versiya"),
            "solders": сб.get("solders"),
            "polnaya_ms": (сб.get("polnaya_ms") or {}).get("med"),
            "iz_zagotovki_ms": (сб.get("iz_zagotovki_ms") or {}).get("med"),
            "shest_polnyh_ms": сб.get("shest_polnyh_ms"),
            "shest_iz_zagotovki_ms": сб.get("shest_iz_zagotovki_ms"),
            "etalon_cikl_ms": (эт.get("cikl_python") or {}).get("med"),
            "etalon_sha256_ms": (эт.get("sha256_4mib") or {}).get("med"),
            "steal_proc": (ж.get("doli_cpu_za_okno") or {}).get("steal_proc"),
            "zanyato_proc": (ж.get("doli_cpu_za_okno") or {}).get("zanyato_proc"),
            "loadavg1": (ж.get("loadavg") or [None])[0],
            "sborka_why_not": сб.get("why_not")})
    из_["mashin"] = len(ряды)
    if not ряды:
        из_["why_not"] = "ни одного свода не прочитано"
        return из_
    быстрая_сб = min((р["polnaya_ms"] for р in ряды if р["polnaya_ms"]),
                      default=None)
    быстрый_эт = min((р["etalon_cikl_ms"] for р in ряды if р["etalon_cikl_ms"]),
                      default=None)
    for р in ряды:
        р["sborka_raz"] = (round(р["polnaya_ms"] / быстрая_сб, 2)
                            if (р["polnaya_ms"] and быстрая_сб) else None)
        р["etalon_raz"] = (round(р["etalon_cikl_ms"] / быстрый_эт, 2)
                            if (р["etalon_cikl_ms"] and быстрый_эт) else None)
        if р["sborka_raz"] and р["etalon_raz"]:
            разбег = abs(р["sborka_raz"] / р["etalon_raz"] - 1.0)
            р["razbeg"] = round(разбег, 3)
            р["zhelezo_obyasnyaet"] = bool(разбег <= РАЗБЕГ_ОТНОШЕНИЙ)
            р["vyvod"] = ("медленнее ровно настолько, насколько медленнее сама "
                           "машина -- причина в железе"
                           if р["zhelezo_obyasnyaet"] else
                           ("сборка отстаёт сильнее общего эталона -- причина "
                            "НЕ в железе, а в коде или окружении"
                            if р["sborka_raz"] > р["etalon_raz"] else
                            "сборка отстаёт МЕНЬШЕ эталона -- железо не узкое место"))
        else:
            р["razbeg"] = None
            р["zhelezo_obyasnyaet"] = None
            р["vyvod"] = "нет числа: " + (р["sborka_why_not"] or "эталон не снят")
    ряды.sort(key=lambda р: (р["polnaya_ms"] is None, р["polnaya_ms"] or 0))
    из_["ryady"] = ряды
    из_["bystraya_sborka_ms"] = быстрая_сб
    из_["bystryj_etalon_ms"] = быстрый_эт
    return из_


def свод(*, секунд: float = СЕКУНД_ОКНА, повторов: int = ПОВТОРОВ_ПО_УМОЛЧАНИЮ,
          путь_образца: str = ОБРАЗЕЦ_ПО_УМОЛЧАНИЮ,
          делать_сборку: bool = True) -> dict:
    из_ = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "mashina": platform.node(), "zhelezo": железо(секунд),
            "etalon": эталон()}
    из_["sborka"] = (сборка(путь_образца=путь_образца, повторов=повторов)
                      if делать_сборку
                      else {"ok": False, "why_not": "сборка не запрашивалась"})
    return из_


def самопроверка() -> int:
    проверки: list = []

    def chk(имя: str, ок: bool, что=None) -> None:
        проверки.append((имя, bool(ок), что))

    # ДОЛИ СЧИТАЮТСЯ РАЗНОСТЬЮ, А НЕ "С ЗАГРУЗКИ" -- на синтетических снимках.
    до = {"user": 100, "nice": 0, "system": 0, "idle": 800, "iowait": 50,
          "irq": 0, "softirq": 0, "steal": 50, "vsego": 1000}
    после = {"user": 200, "nice": 0, "system": 0, "idle": 1500, "iowait": 100,
             "irq": 0, "softirq": 0, "steal": 200, "vsego": 2000}
    д = доли_cpu(до, после)
    chk("steal за окно -- 15%% (150 тактов из 1000), а не 10%% с загрузки",
        д["steal_proc"] == 15.0 and д["taktov_v_okne"] == 1000, д)
    chk("занято = всё, кроме idle и iowait",
        д["zanyato_proc"] == 25.0, д)
    chk("iowait за окно назван отдельно", д["iowait_proc"] == 5.0, д)
    chk("одинаковые снимки -- не ноль, а причина словами",
        доли_cpu(до, до)["why_not"] and доли_cpu(до, до)["steal_proc"] is None,
        доли_cpu(до, до))
    chk("пустые снимки -- причина словами, а не делёж на ноль",
        доли_cpu({}, {})["why_not"] is not None)

    # ЗАМЕР НЕ ВРЁТ ПРО СБОЙ: если функция падает, числа нет, есть причина.
    def _падает():
        raise ValueError("нарочно")
    пл = замер(_падает, 3)
    chk("падающая функция -- причина, а не число",
        пл["med"] is None and "ValueError" in (пл["why_not"] or ""), пл)
    хор = замер(lambda: sum(range(1000)), 5)
    chk("рабочая функция -- медиана, минимум, максимум и число повторов",
        хор["n"] == 5 and хор["med"] is not None
        and хор["min"] <= хор["med"] <= хор["max"], хор)

    # ЭТАЛОН ИДЁТ БЕЗ ЕДИНОЙ ЗАВИСИМОСТИ И ДАЁТ ПОЛОЖИТЕЛЬНЫЕ ЧИСЛА.
    эт = эталон(3)
    chk("шесть эталонов без зависимостей -- все с числом",
        len(эт) == 6 and all(в["med"] and в["med"] > 0 for в in эт.values()),
        {к: в["med"] for к, в in эт.items()})
    chk("имена эталонов -- ASCII (отчёт читают на четырёх машинах)",
        all(и.isascii() for и, _ in ЭТАЛОНЫ))

    # ЖЕЛЕЗО ЧИТАЕТСЯ И НЕ ПАДАЕТ ТАМ, ГДЕ ФАЙЛА НЕТ.
    пр = процессор()
    chk("процессор: ядра и архитектура есть всегда",
        (пр["yader"] or 0) >= 1 and пр["arhitektura"], пр)
    chk("нечитаемый /proc/cpuinfo -- причина словами",
        _чтение("/proc/net/nonexistent_file_zzz") == "")
    пт = питон()
    chk("версия Python в отчёте -- отсюда же сравнение машин",
        пт["versiya"] and пт["realizaciya"], пт)

    # КТО РЯДОМ -- РАЗБОР /proc БЕЗ ДОПУЩЕНИЙ: нет каталога -> причина.
    нр = кто_рядом(0.0, корень="/proc/zzz_net_takogo")
    chk("нет каталога процессов -- причина словами, а не пустой список молча",
        нр["why_not"] and not нр["processy"], нр)
    # ОКНО ЗАНИМАЕТСЯ НАРОЧНО. На простаивающей машине за 0.3 с ни один процесс
    # может не набрать ни одного такта, и проверка краснела бы от безделья, а не
    # от ошибки. Поэтому в окне жжёт свой же поток -- тогда такты есть всегда.
    import threading as _th  # noqa: PLC0415
    _стоп = _th.Event()

    def _жечь():
        while not _стоп.is_set():
            sum(range(20_000))
    _поток = _th.Thread(target=_жечь, daemon=True)
    _поток.start()
    try:
        жив = кто_рядом(0.6)
    finally:
        _стоп.set()
        _поток.join(timeout=2.0)
    chk("живой разбор /proc даёт хотя бы один процесс с расходом",
        жив["why_not"] is None and len(жив["processy"]) >= 1,
        [(с["imya"], с["cpu_proc_odnogo_yadra"]) for с in жив["processy"][:3]])
    chk("свой же процесс виден в списке занявших ядро",
        any(с["pid"] == os.getpid() for с in жив["processy"]),
        [с["pid"] for с in жив["processy"][:5]] + [os.getpid()])
    chk("у процесса названы маска ядер и nice -- иначе не видно, делят ли ядро",
        all(("yadra_razresheny" in с and "nice" in с)
            for с in жив["processy"]), жив["processy"][:2])

    # СБОРКА: НЕТ МОДУЛЕЙ ИЛИ ОБРАЗЦА -> ПРИЧИНА, А НЕ ЧИСЛО.
    # НЕХВАТКА ФАЙЛА ДАННЫХ НАЗЫВАЕТСЯ ПО ИМЕНИ, А НЕ ОБРЕЗАННЫМ
    # FileNotFoundError: ровно на этом 10.10 потерялись три машины из четырёх.
    _было_к = os.environ.get("SKOROST_KOREN")
    try:
        os.environ["SKOROST_KOREN"] = "/tmp"
        _нет_д = сборка(повторов=1)
        chk("нет файла меток площадок -- он назван по имени, и числа нет",
            _нет_д["polnaya_ms"] is None
            and "dex_labels.json" in (_нет_д.get("why_not") or ""),
            _нет_д.get("why_not"))
    finally:
        if _было_к is None:
            os.environ.pop("SKOROST_KOREN", None)
        else:
            os.environ["SKOROST_KOREN"] = _было_к
    chk("нужные файлы данных перечислены списком, а не спрятаны в коде",
        len(НУЖНЫЕ_ФАЙЛЫ) >= 1
        and all(ф.startswith("data/") for ф in НУЖНЫЕ_ФАЙЛЫ), НУЖНЫЕ_ФАЙЛЫ)
    нет_обр = сборка(путь_образца="data/zzz_net_takogo_obrazca.json", повторов=1)
    chk("нет файла образцов -- причина словами, число не выдумывается",
        нет_обр["ok"] is False and нет_обр["polnaya_ms"] is None
        and "не читается" in (нет_обр["why_not"] or ""), нет_обр["why_not"])
    # НАСТОЯЩАЯ СБОРКА -- там, где модули есть. Это и есть замер.
    сб = сборка(повторов=5)
    if сб.get("ok"):
        chk("полная сборка и вариант из заготовки -- оба с числом",
            сб["polnaya_ms"]["med"] > 0 and сб["iz_zagotovki_ms"]["med"] > 0, сб)
        chk("замер прошёл БЕЗ СЕТИ: ни одной попытки обращения к узлу",
            сб["setevyh_popytok"] == 0, сб["setevyh_popytok"])
        chk("вариант из заготовки ДЕШЕВЛЕ полной сборки -- в этом вся правка",
            сб["vygoda_raz"] > 1.5, сб.get("vygoda_raz"))
        chk("шесть вариантов обоими путями -- числом, для сравнения с журналом",
            сб["shest_polnyh_ms"] > сб["shest_iz_zagotovki_ms"],
            (сб["shest_polnyh_ms"], сб["shest_iz_zagotovki_ms"]))
    else:
        chk("модулей полосы нет -- замер говорит причину, и это не провал",
            сб["why_not"] is not None, сб["why_not"])

    # КОРЕНЬ ПЕРЕОПРЕДЕЛЯЕТСЯ -- иначе замер на чужой машине искал бы analysis/
    # в несуществующем каталоге и врал бы "модули полосы не загрузились".
    _было = os.environ.get("SKOROST_KOREN")
    try:
        os.environ["SKOROST_KOREN"] = "/tmp/zzz_koren_zamera"
        chk("--koren меняет корень поиска модулей и образцов",
            str(корень()) == "/tmp/zzz_koren_zamera", str(корень()))
        chk("с чужим корнем сборка говорит причину, а не выдумывает число",
            сборка(повторов=1)["polnaya_ms"] is None)
    finally:
        if _было is None:
            os.environ.pop("SKOROST_KOREN", None)
        else:
            os.environ["SKOROST_KOREN"] = _было
    chk("корень вернулся к репозиторию после проверки",
        str(корень()) == str(КОРЕНЬ), str(корень()))

    # ТАБЛИЦА ЧЕТЫРЁХ: отношения к САМОЙ БЫСТРОЙ, и вывод про железо -- на
    # синтетических сводах, чтобы проверять правило, а не чью-то машину.
    def _св(полн, цикл, имя="m"):
        return {"mashina": имя, "zhelezo": {"processor": {"model": "CPU " + имя,
                 "yader": 2, "mhz": [2000.0]}, "python": {"versiya": "3.11.0"},
                 "doli_cpu_za_okno": {"steal_proc": 0.0, "zanyato_proc": 10.0},
                 "loadavg": [0.5, 0.5, 0.5]},
                "etalon": {"cikl_python": {"med": цикл},
                            "sha256_4mib": {"med": цикл}},
                "sborka": {"ok": True, "solders": "0.29.0",
                            "polnaya_ms": {"med": полн},
                            "iz_zagotovki_ms": {"med": полн / 4.0},
                            "shest_polnyh_ms": полн * 6,
                            "shest_iz_zagotovki_ms": полн * 6 / 4.0}}
    т = таблица({"быстрая": _св(0.7, 9.0), "медленная": _св(5.6, 72.0)})
    чист = {р["mashina"]: р for р in т["ryady"]}
    chk("отношения считаются к самой быстрой машине набора",
        чист["быстрая"]["sborka_raz"] == 1.0
        and чист["медленная"]["sborka_raz"] == 8.0, т["ryady"])
    chk("машина медленнее во столько же раз по эталону -- вывод 'причина в железе'",
        чист["медленная"]["zhelezo_obyasnyaet"] is True
        and "в железе" in чист["медленная"]["vyvod"], чист["медленная"])
    т2 = таблица({"быстрая": _св(0.7, 9.0), "кривая": _св(5.6, 11.0)})
    чист2 = {р["mashina"]: р for р in т2["ryady"]}
    chk("сборка отстаёт в 8 раз, а машина лишь в 1.2 -- вывод 'НЕ в железе'",
        чист2["кривая"]["zhelezo_obyasnyaet"] is False
        and "НЕ в железе" in чист2["кривая"]["vyvod"], чист2["кривая"])
    т3 = таблица({"одна": {"mashina": "x", "sborka": {"ok": False,
                   "why_not": "solders нет"}, "etalon": {}, "zhelezo": {}}})
    chk("машина без числа -- причина в строке, а не ноль",
        т3["ryady"][0]["polnaya_ms"] is None
        and "solders нет" in т3["ryady"][0]["vyvod"], т3["ryady"][0])
    chk("пустой набор -- причина словами", таблица({})["why_not"] is not None)
    chk("ряды отсортированы от быстрой к медленной",
        [р["mashina"] for р in т["ryady"]] == ["быстрая", "медленная"])
    chk("порог разбега назван числом и объяснён в коде",
        РАЗБЕГ_ОТНОШЕНИЙ == 0.35)

    # СВОД СОБИРАЕТСЯ ЦЕЛИКОМ И ПЕРЕЖИВАЕТ json.dumps -- его увозят в отчёт.
    с = свод(секунд=0.2, повторов=3)
    chk("свод: машина, железо, эталон и сборка -- все четыре раздела",
        all(к in с for к in ("mashina", "zhelezo", "etalon", "sborka")),
        sorted(с.keys()))
    chk("свод переживает json.dumps -- иначе отчёт не уедет с машины",
        isinstance(json.dumps(с, ensure_ascii=False), str))
    chk("образец по умолчанию -- pump AMM, и причина выбора в описании модуля",
        "pAMMBay6oce" in ОБРАЗЕЦ_ПО_УМОЛЧАНИЮ
        and "6EF8rrecth" in (__doc__ or "")
        and "неоднозначная котировка" in (__doc__ or ""))

    плохих = [(и, ч) for и, ок, ч in проверки if not ок]
    for и, ок, ч in проверки:
        print(f"  [{'ok  ' if ок else 'ПРОВАЛ'}] {и}" + ("" if ок else f" -> {ч}"))
    print(f"самопроверка замера скорости сборки: "
          f"{len(проверки) - len(плохих)}/{len(проверки)} пройдено")
    return 1 if плохих else 0


def main() -> int:
    п = argparse.ArgumentParser()
    п.add_argument("--self-test", action="store_true")
    п.add_argument("--sekund", type=float, default=СЕКУНД_ОКНА)
    п.add_argument("--povtorov", type=int, default=ПОВТОРОВ_ПО_УМОЛЧАНИЮ)
    п.add_argument("--obrazec", default=ОБРАЗЕЦ_ПО_УМОЛЧАНИЮ)
    п.add_argument("--bez-sborki", action="store_true")
    п.add_argument("--koren", default="",
                    help="корень дерева с analysis/ и data/ (пусто -- рядом с модулем)")
    п.add_argument("--out", default="")
    п.add_argument("--svod", action="append", default=[],
                    help="имya=put k svodu mashiny; с ним модуль НЕ мерит, а "
                         "сводит уже снятые числа в одну таблицу")
    а = п.parse_args()
    if а.koren:
        os.environ["SKOROST_KOREN"] = а.koren
    if а.self_test:
        return самопроверка()
    if а.svod:
        своды = {}
        for пара in а.svod:
            имя, _, путь = пара.partition("=")
            try:
                своды[имя] = json.loads(Path(путь).read_text(encoding="utf-8"))
            except (OSError, ValueError) as сбой:
                своды[имя] = {"mashina": имя, "sborka": {
                    "ok": False, "why_not": f"свод не прочитан: {сбой}"},
                    "etalon": {}, "zhelezo": {}}
        т = таблица(своды)
        текст = json.dumps(т, ensure_ascii=False, indent=1)
        print(текст)
        if а.out:
            Path(а.out).write_text(текст + "\n", encoding="utf-8")
        return 0
    из_ = свод(секунд=а.sekund, повторов=а.povtorov, путь_образца=а.obrazec,
                делать_сборку=not а.bez_sborki)
    текст = json.dumps(из_, ensure_ascii=False, indent=1)
    print(текст)
    if а.out:
        Path(а.out).write_text(текст + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
