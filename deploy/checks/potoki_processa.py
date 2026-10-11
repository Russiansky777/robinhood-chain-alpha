#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""КТО ИЗ ПОТОКОВ ПРОЦЕССА ЖЖЁТ ПРОЦЕССОР -- по именам, только чтение /proc.

ЗАЧЕМ (слово владельца 11.10, п.1б): "кто именно жжёт CPU в процессе полосы в
момент сделки -- имена потоков и их CPU за окно".

ОТКУДА ВОПРОС. Живьём шесть вариантов покупки стоят 29-40 мс стенного времени
при счёте на 0.5 мс, и замер 10.10 показал, из чего это складывается: при шаге
передачи GIL 0.005 с каждый соперничающий поток Python задерживает наш поток до
5 мс за передачу. Значит ответ "сколько мс мы теряем" зависит от ОДНОГО числа:
сколько потоков в этом процессе реально жгут процессор. Это число не угадывают,
а читают.

КАК. /proc/<pid>/task/<tid>/stat -- счётчики utime и stime КАЖДОГО потока в
тактах с его рождения. Два снимка с интервалом дают расход за окно. Имя потока
берётся из /proc/<pid>/task/<tid>/comm.

ПРО ИМЕНА -- ПРОВЕРЕНО ЗАМЕРОМ, А НЕ ВЗЯТО ИЗ ПАМЯТИ. Сначала я написал здесь,
что CPython ставит имя потока ОС из threading.Thread(name=...). Это НЕВЕРНО для
нашей сборки: прогон с восемью потоками, которым имена заданы явно
(name="zhgu-N"), показал в comm у всех одно и то же "python3". Значит по comm
отличить наши потоки друг от друга НЕЛЬЗЯ, и ответ "кто именно жжёт" даёт не
этот модуль, а разбор кода (кто какие потоки создаёт) плюс соотнесение окон
замера с минутами сделок. Поле bez_imeni_potokov в ответе как раз и называет,
сколько потоков безымянны.

ЭТОТ ЗАМЕР НЕ СТАВИТЬ НА ГОРЯЧИЙ ПУТЬ. Замер 11.10: один снимок девяти потоков
стоит 0.025 мс в тишине и 1034 мс (p90 2449 мс) при восьми соперничающих
потоках -- каждое открытие файла отпускает GIL, и его возврат ждёт шаг
передачи. То есть ровно в той обстановке, которую мы изучаем, этот замер стоил
бы секунду на покупку. Поэтому он живёт ОТДЕЛЬНЫМ процессом и смотрит на службу
снаружи.

Только чтение /proc. Ни одной записи, ни сети, ни сигналов процессу: сам
процесс об этом замере не узнаёт.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ОКНО_ПО_УМОЛЧАНИЮ = 5.0
# ПО КАКОЙ ПОДСТРОКЕ ИЩЕМ ПРОЦЕСС. Имя службы в cmdline, а не в comm: comm у
# всех наших служб -- просто "python3".
ИСКАТЬ_ПО_УМОЛЧАНИЮ = "bloom_detector"
# ВЫШЕ ЭТОГО -- СЧИТАЕМ, ЧТО ПОТОК ЖЖЁТ ПРОЦЕССОР. Порог в процентах одного
# ядра за окно. 1 % -- это 50 мс счёта за 5 с: уже достаточно, чтобы в
# миллисекундном окне покупки встретиться с нашим потоком, но заметно выше
# шума от собственного чтения /proc.
ПОРОГ_ЖЖЁТ_ПРОЦ = 1.0


def _чтение(путь: str) -> str:
    try:
        with open(путь, encoding="utf-8", errors="replace") as ф:
            return ф.read()
    except OSError:
        return ""


def такты_в_секунде() -> int:
    try:
        return int(os.sysconf("SC_CLK_TCK"))
    except (ValueError, OSError, AttributeError):
        return 100


def _свои_pid(корень: str = "/proc") -> set:
    """Свой процесс и цепочка его родителей -- их искать нельзя.

    ЗАЧЕМ. Зонд запускается как `python3 potoki_processa.py --iskat
    bloom_detector`, то есть подстрока поиска есть в ЕГО ЖЕ командной строке (и
    в строке обёртки bash -c). Проход 11.10 нашёл живой случай: в отчёте по
    хосту лежало три совпадения, два из них -- сам зонд. Если служба лежит, без
    этого исключения вернулся бы PID зонда, замер показал бы его один поток, и
    «служба мертва» было бы неотличимо от «в окне было тихо».
    """
    из_ = set()
    пид = os.getpid()
    for _ in range(16):
        if пид <= 0 or пид in из_:
            break
        из_.add(пид)
        род = 0
        стат = _чтение(f"{корень}/{пид}/stat")
        if стат and ")" in стат:
            хвост = стат.rsplit(")", 1)[1].split()
            try:
                род = int(хвост[1])
            except (IndexError, ValueError):
                род = 0
        пид = род
    return из_


def найти_pid(искать: str, корень: str = "/proc") -> dict:
    """PID по подстроке командной строки. Несколько совпадений -- назвать все."""
    из_ = {"pid": None, "why_not": None, "vse": [], "iskali": искать,
            "svoi_pid_isklyucheny": sorted(_свои_pid(корень))}
    try:
        имена = [и for и in os.listdir(корень) if и.isdigit()]
    except OSError as сбой:
        из_["why_not"] = f"{корень} не читается: {сбой}"
        return из_
    свои = set(из_["svoi_pid_isklyucheny"])
    for пид in имена:
        if int(пид) in свои:
            continue
        команда = _чтение(f"{корень}/{пид}/cmdline").replace("\0", " ").strip()
        if искать and искать in команда:
            из_["vse"].append({"pid": int(пид), "komanda": команда[:200]})
    if not из_["vse"]:
        из_["why_not"] = f"процесса с подстрокой {искать!r} в cmdline нет"
        return из_
    # САМЫЙ СТАРЫЙ ИЗ СОВПАВШИХ -- это служба, а не её подпроцесс или наш grep.
    из_["vse"].sort(key=lambda з: з["pid"])
    из_["pid"] = из_["vse"][0]["pid"]
    if len(из_["vse"]) > 1:
        из_["ogovorka"] = (f"совпало {len(из_['vse'])} процессов, взят младший "
                            f"pid; остальные названы в vse")
    return из_


def _снимок_потоков(пид: int, корень: str = "/proc") -> dict:
    """{tid: (такты, имя)} по всем потокам процесса."""
    из_ = {}
    каталог = f"{корень}/{пид}/task"
    try:
        тиды = os.listdir(каталог)
    except OSError:
        return из_
    for тид in тиды:
        if not тид.isdigit():
            continue
        стат = _чтение(f"{каталог}/{тид}/stat")
        if not стат or ")" not in стат:
            continue
        хвост = стат.rsplit(")", 1)[1].split()
        try:
            такты = int(хвост[11]) + int(хвост[12])
        except (IndexError, ValueError):
            continue
        имя = (_чтение(f"{каталог}/{тид}/comm").strip()
                or (стат.split("(", 1)[1].rsplit(")", 1)[0] if "(" in стат else тид))
        из_[тид] = (такты, имя)
    return из_


def потоки(*, пид: int, секунд: float = ОКНО_ПО_УМОЛЧАНИЮ,
            корень: str = "/proc") -> dict:
    """Расход процессора КАЖДЫМ потоком за окно -- разностью двух снимков."""
    из_ = {"pid": пид, "sekund": секунд, "yader": os.cpu_count(),
            "potokov": 0, "ryady": [], "why_not": None,
            "takty_v_sekunde": такты_в_секунде()}
    до = _снимок_потоков(пид, корень)
    if not до:
        из_["why_not"] = f"у процесса {пид} не читается каталог потоков"
        return из_
    time.sleep(max(0.0, float(секунд)))
    после = _снимок_потоков(пид, корень)
    тик = float(из_["takty_v_sekunde"])
    ядер = из_["yader"] or 1
    ряды = []
    for тид, (такты, имя) in после.items():
        было = до.get(тид)
        if было is None:
            # НОВЫЙ ПОТОК В ОКНЕ: его расход считать не с чего, и выдумывать
            # нуль нельзя -- он назван отдельно.
            ряды.append({"tid": int(тид), "imya": имя, "sekund_cpu": None,
                          "proc_odnogo_yadra": None, "proc_mashiny": None,
                          "pochemu": "поток появился внутри окна"})
            continue
        дельта = такты - было[0]
        сек = дельта / тик
        ряды.append({"tid": int(тид), "imya": имя,
                      "sekund_cpu": round(сек, 4),
                      "proc_odnogo_yadra": round(сек * 100.0 / max(1e-9, секунд), 3),
                      "proc_mashiny": round(сек * 100.0
                                             / max(1e-9, секунд * ядер), 3),
                      "pochemu": None})
    ряды.sort(key=lambda р: -(р["proc_odnogo_yadra"] or -1.0))
    из_["ryady"] = ряды
    из_["potokov"] = len(ряды)
    из_["potokov_poyavilos_v_okne"] = sum(1 for р in ряды
                                           if р["sekund_cpu"] is None)
    из_["potokov_ischezlo_v_okne"] = len([т for т in до if т not in после])
    # ГЛАВНОЕ ЧИСЛО: сколько потоков жжёт процессор выше порога. Именно от него
    # зависит, что даёт смена шага передачи GIL.
    жгут = [р for р in ряды
             if (р["proc_odnogo_yadra"] or 0.0) >= ПОРОГ_ЖЖЁТ_ПРОЦ]
    из_["porog_zhzhet_proc"] = ПОРОГ_ЖЖЁТ_ПРОЦ
    из_["zhgut_potokov"] = len(жгут)
    из_["zhgut_imena"] = [р["imya"] for р in жгут]
    из_["summa_proc_odnogo_yadra"] = round(
        sum(р["proc_odnogo_yadra"] or 0.0 for р in ряды), 3)
    из_["bez_imeni_potokov"] = sum(1 for р in ряды
                                    if (р["imya"] or "").startswith("python"))
    return из_


def свод(из_: dict) -> dict:
    """Что из этого следует для шага передачи GIL -- словами и числом."""
    жгут = из_.get("zhgut_potokov")
    if жгут is None:
        из_["vyvod"] = "нет числа: " + (из_.get("why_not") or "замер не прошёл")
        return из_
    # ЦИФРЫ ИЗ ЗАМЕРА 11.10 (deploy/checks/skorost_sborki.py --sosedi, 4 ядра,
    # шесть вариантов из заготовки): выигрыш смены шага 0.005 -> 0.0005.
    if жгут >= 4:
        из_["vyvod"] = (f"жгущих потоков {жгут}: смена шага передачи GIL "
                         f"0.005 -> 0.0005 даёт порядка 65 мс по медиане")
    elif жгут >= 2:
        из_["vyvod"] = (f"жгущих потоков {жгут}: смена шага даёт около 1 мс по "
                         f"медиане и порядка 10 мс по p90 -- ниже порога "
                         f"владельца 20 мс по медиане")
    elif жгут == 1:
        из_["vyvod"] = ("жгёт один поток (наш же): соперника нет, смена шага "
                         "ничего не даёт")
    else:
        из_["vyvod"] = ("в окне замера не жёг никто: окно попало в тишину, "
                         "замер надо повторить В МОМЕНТ активности источника")
    return из_


def самопроверка() -> int:
    import tempfile  # noqa: PLC0415
    проверки: list = []

    def chk(имя: str, ок: bool, что=None) -> None:
        проверки.append((имя, bool(ок), что))

    def _сделать_proc(кат: str, пид: int, потоки: dict, команда: str) -> None:
        """Поддельный /proc: {tid: (utime, stime, comm)}."""
        п = Path(кат) / str(пид)
        (п / "task").mkdir(parents=True, exist_ok=True)
        (п / "cmdline").write_text(команда.replace(" ", "\0"), encoding="utf-8")
        for тид, (ут, ст, имя) in потоки.items():
            т = п / "task" / str(тид)
            т.mkdir(parents=True, exist_ok=True)
            # ПОЛЯ stat СЧИТАЮТСЯ ОТ ЗАКРЫВАЮЩЕЙ СКОБКИ, и это легко сбить.
            # Нумерация /proc: 1 pid, 2 comm, 3 state, 4 ppid, ... 14 utime,
            # 15 stime. Читатель берёт текст ПОСЛЕ ")", где индекс 0 -- это
            # поле 3, значит utime лежит по индексу 11, stime -- по 12. Здесь
            # строка начинается с "<tid> (python3) S 1 ", то есть индексы 0 и 1
            # уже заняты state и ppid, и хвост начинается с индекса 2.
            # Поэтому utime -- это хвост[9], а stime -- хвост[10].
            хвост = ["0"] * 30
            хвост[9] = str(ут)
            хвост[10] = str(ст)
            строка = f"{тид} (python3) S 1 " + " ".join(хвост)
            _после = строка.rsplit(")", 1)[1].split()
            assert _после[11] == str(ут) and _после[12] == str(ст), (
                "поддельный stat не сходится с читателем -- проверка стала бы "
                "бессмысленной")
            (т / "stat").write_text(строка, encoding="utf-8")
            (т / "comm").write_text(имя + "\n", encoding="utf-8")

    with tempfile.TemporaryDirectory() as кат:
        _сделать_proc(кат, 111, {111: (0, 0, "python3"), 112: (0, 0, "kolco")},
                       "python3 -u /home/bot/bloom_executor/bloom_detector.py")
        _сделать_proc(кат, 222, {222: (0, 0, "python3")},
                       "python3 /home/bot/bloom_executor/bloom_seller.py")
        н = найти_pid("bloom_detector", корень=кат)
        chk("pid находится по подстроке командной строки, а не по comm",
            н["pid"] == 111 and н["why_not"] is None, н)
        chk("чужая служба по этой подстроке не находится",
            найти_pid("bloom_seller", корень=кат)["pid"] == 222)
        # СВОЙ ПРОЦЕСС НЕ НАХОДИТСЯ. Проверяем на подстроке, которая ТОЧНО
        # есть в нашей командной строке: имя этого самого модуля.
        _своё = найти_pid("potoki_processa", корень="/proc")
        chk("свой же процесс по своей подстроке НЕ находится -- иначе мёртвая "
            "служба читалась бы как тихая",
            os.getpid() not in [з["pid"] for з in _своё["vse"]]
            and os.getpid() in _своё["svoi_pid_isklyucheny"],
            (_своё.get("vse"), _своё.get("svoi_pid_isklyucheny")))
        chk("нет совпадений -- причина словами, а не первый попавшийся pid",
            найти_pid("net_takogo", корень=кат)["why_not"] is not None
            and найти_pid("net_takogo", корень=кат)["pid"] is None)
        chk("несколько совпадений -- берётся младший pid и названа оговорка",
            (lambda: (_сделать_proc(кат, 99, {99: (0, 0, "python3")},
                                     "python3 bloom_detector.py --x"),
                      найти_pid("bloom_detector", корень=кат))[1])()["pid"] == 99)
        м = найти_pid("bloom_detector", корень=кат)
        chk("оговорка о нескольких совпадениях есть в ответе",
            м.get("ogovorka") and len(м["vse"]) == 2, м.get("ogovorka"))

        # РАСХОД СЧИТАЕТСЯ РАЗНОСТЬЮ: поднимаем счётчики между снимками.
        тик = такты_в_секунде()
        _сделать_proc(кат, 333, {333: (0, 0, "glavnyj"), 334: (0, 0, "kolco"),
                                  335: (0, 0, "tihij")},
                       "python3 bloom_detector.py")
        import threading as _th  # noqa: PLC0415
        def подкрутить():
            time.sleep(0.25)
            # за окно 0.5 с: главный получает 0.5 с CPU (100 % ядра),
            # kolco -- 0.05 с (10 %), tihij -- ничего.
            _сделать_proc(кат, 333,
                           {333: (int(0.5 * тик), 0, "glavnyj"),
                            334: (0, int(0.05 * тик), "kolco"),
                            335: (0, 0, "tihij")},
                           "python3 bloom_detector.py")
        п = _th.Thread(target=подкрутить, daemon=True)
        п.start()
        р = свод(потоки(пид=333, секунд=0.5, корень=кат))
        п.join(timeout=3)
        по_именам = {с["imya"]: с for с in р["ryady"]}
        chk("расход потока -- разность счётчиков, и utime со stime складываются",
            abs(по_именам["glavnyj"]["proc_odnogo_yadra"] - 100.0) < 2.0
            and abs(по_именам["kolco"]["proc_odnogo_yadra"] - 10.0) < 2.0,
            {и: с["proc_odnogo_yadra"] for и, с in по_именам.items()})
        chk("тихий поток -- ноль, а не None: его счётчики мы прочли оба раза",
            по_именам["tihij"]["proc_odnogo_yadra"] == 0.0)
        chk("имя потока берётся из comm, а не из stat (там у всех python3)",
            set(по_именам) == {"glavnyj", "kolco", "tihij"}, sorted(по_именам))
        chk("ряды отсортированы от самого жадного",
            [с["imya"] for с in р["ryady"]][0] == "glavnyj")
        chk("число жгущих считается по порогу, и имена названы",
            р["zhgut_potokov"] == 2
            and set(р["zhgut_imena"]) == {"glavnyj", "kolco"},
            (р["zhgut_potokov"], р["zhgut_imena"]))
        chk("вывод про шаг GIL -- из числа жгущих, а не на слово",
            "0.0005" in р["vyvod"] or "p90" in р["vyvod"], р["vyvod"])

        # НОВЫЙ ПОТОК В ОКНЕ -- НЕ НУЛЬ, А ПРИЧИНА.
        _сделать_proc(кат, 444, {444: (0, 0, "glavnyj")},
                       "python3 bloom_detector.py")
        def добавить():
            time.sleep(0.1)
            _сделать_proc(кат, 444, {444: (0, 0, "glavnyj"),
                                      445: (0, 0, "novyj")},
                           "python3 bloom_detector.py")
        п2 = _th.Thread(target=добавить, daemon=True)
        п2.start()
        р2 = потоки(пид=444, секунд=0.3, корень=кат)
        п2.join(timeout=3)
        нов = [с for с in р2["ryady"] if с["imya"] == "novyj"]
        chk("поток, появившийся В ОКНЕ, назван причиной, а не нулём",
            len(нов) == 1 and нов[0]["sekund_cpu"] is None
            and "внутри окна" in (нов[0]["pochemu"] or ""), нов)
        chk("исчезнувшие и появившиеся потоки посчитаны отдельно",
            р2["potokov_poyavilos_v_okne"] == 1
            and р2["potokov_ischezlo_v_okne"] == 0, р2)

        # НЕТ КАТАЛОГА ПОТОКОВ -- ПРИЧИНА, А НЕ ПУСТОЙ СПИСОК.
        пуст = потоки(пид=777777, секунд=0.0, корень=кат)
        chk("нет процесса -- причина словами, а не пустой список молча",
            пуст["why_not"] and not пуст["ryady"], пуст)
        chk("тишина в окне -- вывод говорит повторить замер в момент сделки",
            "тишину" in свод({"zhgut_potokov": 0, "ryady": []})["vyvod"])
        chk("один жгущий (мы сами) -- вывод говорит, что смена шага не даёт",
            "ничего не даёт" in свод({"zhgut_potokov": 1,
                                       "ryady": []})["vyvod"])
        chk("порог жжения назван числом в ответе и объяснён в коде",
            свод(потоки(пид=333, секунд=0.0, корень=кат))
            .get("porog_zhzhet_proc") == 1.0)
        chk("потоки без своего имени видны числом -- значит имя не дали",
            потоки(пид=222, секунд=0.0, корень=кат)["bez_imeni_potokov"] == 1)
        # ИМЕНА ПОТОКОВ В comm -- ПРОВЕРЯЕМ ФАКТОМ НА СЕБЕ, а не верим памяти.
        # Если когда-нибудь сборка Python начнёт ставить имя потока ОС, эта
        # проверка покраснеет, и головное описание надо будет поправить.
        import threading as _th2  # noqa: PLC0415
        _стоп2 = _th2.Event()
        _п2 = _th2.Thread(target=_стоп2.wait, name="imya-proverki", daemon=True)
        _п2.start()
        try:
            _свои = потоки(пид=os.getpid(), секунд=0.0)
            _имена = {с["imya"] for с in _свои["ryady"]}
        finally:
            _стоп2.set()
            _п2.join(timeout=2)
        chk("comm НЕ показывает имя из threading.Thread(name=...) -- именно "
            "поэтому ответ «кто жжёт» даёт разбор кода, а не этот модуль",
            "imya-proverki" not in _имена, sorted(_имена))

    плохих = [(и, ч) for и, ок, ч in проверки if not ок]
    for и, ок, ч in проверки:
        print(f"  [{'ok  ' if ок else 'ПРОВАЛ'}] {и}" + ("" if ок else f" -> {ч}"))
    print(f"самопроверка потоков процесса: "
          f"{len(проверки) - len(плохих)}/{len(проверки)} пройдено")
    return 1 if плохих else 0


def main() -> int:
    п = argparse.ArgumentParser()
    п.add_argument("--self-test", action="store_true")
    п.add_argument("--iskat", default=ИСКАТЬ_ПО_УМОЛЧАНИЮ,
                    help="подстрока командной строки процесса")
    п.add_argument("--pid", type=int, default=0)
    п.add_argument("--sekund", type=float, default=ОКНО_ПО_УМОЛЧАНИЮ)
    п.add_argument("--out", default="")
    а = п.parse_args()
    if а.self_test:
        return самопроверка()
    пид = а.pid
    из_нашли = None
    if not пид:
        из_нашли = найти_pid(а.iskat)
        if not из_нашли.get("pid"):
            текст = json.dumps(из_нашли, ensure_ascii=False, indent=1)
            print(текст)
            if а.out:
                Path(а.out).write_text(текст + "\n", encoding="utf-8")
            return 0
        пид = из_нашли["pid"]
    из_ = свод(потоки(пид=пид, секунд=а.sekund))
    if из_нашли:
        из_["nashli"] = из_нашли
    текст = json.dumps(из_, ensure_ascii=False, indent=1)
    print(текст)
    if а.out:
        Path(а.out).write_text(текст + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
