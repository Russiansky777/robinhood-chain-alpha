#!/usr/bin/env python3
"""Жива ли запись трендингов на lab-miami: опись, свод по суткам и тонкая выгрузка.

Зачем. В ветке лежат только снимки 12.10 00:38-02:51Z: суточный пуш после этого не
сработал ни разу. Пишется ли что-то на самом бегунке, из контейнера не видно. Этот проход
запускается НА lab-miami маркером data/podbivka/zapusk/miami_*.json и отвечает числами:

  1. опись бегунка -- диск, кэш часов PumpApi (и что из него вычистилось против описи
     12.10 из ветки), venv, каталоги дома;
  2. состояние служб регистратора -- таймеры systemd, последние записи журнала, рабочий
     каталог из юнита (всё только чтением, ничего не правим и не запускаем);
  3. свод записи по суткам и источникам: снимков, байт, коды, окна БЕЗ снимка дольше
     порога (по умолчанию 5 минут), первый и последний снимок;
  4. ТОНКАЯ выгрузка для счёта в ветке: на снимок -- utc, список, код, и по строкам
     только ранг, адрес, объём, ликвидность. Сырое тело выбрасывается (оно и есть вес:
     сутки Jupiter -- около 490 МБ, в git такой файл не влезает вовсе), поэтому тонкая
     копия -- единственный способ посчитать первые появления токенов в ветке.

Только чтение чужих файлов; пишет лишь свои выходы в рабочий каталог задания.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import os
import shutil
import socket
import subprocess
import zlib
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
СЛИМ = П / "trending_slim"
ЮНИТЫ = ("podbivka-trending-min", "podbivka-trending-5min", "podbivka-trending-push")
ПОЛЯ_СТРОКИ = ("по_порядку", "ранг", "адрес", "объём", "ликвидность",
               "ранг_поле", "адрес_поле", "объём_поле", "ликвидность_поле")


def команда(арг: list, таймаут: int = 60) -> str:
    try:
        р = subprocess.run(арг, capture_output=True, text=True, timeout=таймаут)
        о = (р.stdout or "") + (("\n[stderr] " + р.stderr) if р.stderr else "")
        return о.strip()[:4000]
    except Exception as e:  # noqa: BLE001
        return f"[{type(e).__name__}] {str(e)[:200]}"


def службы() -> dict:
    из_ = {"таймеры": команда(["systemctl", "list-timers", "--all", "--no-pager"])}
    for ю in ЮНИТЫ:
        из_[ю] = {
            "активна": команда(["systemctl", "is-active", f"{ю}.service"]),
            "включена": команда(["systemctl", "is-enabled", f"{ю}.timer"]),
            "показать": команда(["systemctl", "show", "-p", "WorkingDirectory",
                                 "-p", "ExecStart", "-p", "ExecMainStatus",
                                 "-p", "NRestarts", f"{ю}.service"]),
            "журнал": команда(["journalctl", "-u", f"{ю}.service", "--since", "-4 days",
                               "--no-pager", "-n", "25"], таймаут=120),
        }
    return из_


def рабочий_каталог(сл: dict) -> Path | None:
    for ю in ЮНИТЫ:
        for стр in (сл.get(ю, {}).get("показать") or "").split("\n"):
            if стр.startswith("WorkingDirectory=") and стр.split("=", 1)[1].strip():
                return Path(стр.split("=", 1)[1].strip())
    for к in ("/opt/robinhood-chain-alpha", str(Path.home() / "robinhood-chain-alpha")):
        if (Path(к) / "data" / "trending").is_dir():
            return Path(к)
    return None


def кэш_опись() -> dict:
    кэш = Path(os.environ.get("PODB_ARHIV_KESH") or (Path.home() / "arhiv_pumpapi"))
    по: dict = collections.defaultdict(lambda: {"часов": 0, "байт": 0})
    всего = часов = 0
    if кэш.is_dir():
        for ф in кэш.rglob("*.jsonl.zst"):
            ч = ф.relative_to(кэш).as_posix()
            сут = ч.rsplit("/", 1)[0] if "/" in ч else ф.stem[:10]
            с = ф.stat()
            по[сут]["часов"] += 1
            по[сут]["байт"] += с.st_size
            всего += с.st_size
            часов += 1
    был = {}
    ф_был = П / "miami_inventar.json"
    if ф_был.exists():
        был = ((json.loads(ф_был.read_text(encoding="utf-8")).get("кэш") or {})
               .get("по_суткам") or {})
    ушло = {к: v for к, v in был.items() if к not in по}
    урезано = {к: {"было_часов": v["часов"], "стало_часов": по[к]["часов"]}
               for к, v in был.items() if к in по and по[к]["часов"] < v["часов"]}
    return {"путь": str(кэш), "часов": часов, "гб": round(всего / 1e9, 2),
            "предел_гб": os.environ.get("PODB_KESH_GB"),
            "по_суткам": {к: {"часов": v["часов"], "гб": round(v["байт"] / 1e9, 2)}
                          for к, v in sorted(по.items())},
            "против_описи_12_10": {"сутки_ушли_целиком": ушло, "сутки_урезаны": урезано,
                                   "суток_было": len(был), "суток_стало": len(по)}}


def свод_записи(каталог: Path, порог_с: int, слим: bool, дни: tuple) -> dict:
    """Снимки по суткам и источникам: число, байт, коды, окна без снимка, тонкая копия."""
    из_: dict = {}
    if слим:
        СЛИМ.mkdir(parents=True, exist_ok=True)
    записанные = []
    for папка in sorted(p for p in каталог.iterdir() if p.is_dir()):
        if папка.name == "pervye":        # таблицы первых появлений -- не снимки
            continue
        # Два раскроя рядом: часовой <источник>/<дата>/<ЧЧ>.jsonl.gz (с 16.10) и старый
        # суточный <источник>/<дата>.jsonl.gz. Дата берётся из папки, если она есть.
        for ф in sorted(папка.rglob("*.jsonl.gz")):
            дата = (ф.parent.name if ф.parent != папка else ф.name.split(".")[0])
            if дни and дата not in дни:
                continue
            по_списку: dict = collections.defaultdict(
                lambda: {"снимков": 0, "коды": collections.Counter(),
                         "ошибки": collections.Counter(), "байт": 0,
                         "элементов": [], "utc": []})
            строк = 0
            сл_ф = СЛИМ / f"{папка.name}_{дата}.jsonl.gz" if слим else None
            о_сл = gzip.open(сл_ф, "wt", encoding="utf-8") if слим else None
            оборван = None
            причина = None
            try:
                if ф.stat().st_size == 0:
                    raise EOFError("файл нулевой длины")
                with gzip.open(ф, "rt", encoding="utf-8") as о:
                    for ln in о:
                        try:
                            с = json.loads(ln)
                        except ValueError:
                            по_списку["[битая строка]"]["снимков"] += 1
                            continue
                        строк += 1
                        к = по_списку[с.get("список") or "?"]
                        к["снимков"] += 1
                        к["коды"][с.get("код")] += 1
                        if с.get("ошибка"):
                            к["ошибки"][str(с["ошибка"])[:60]] += 1
                        к["байт"] += с.get("байт") or 0
                        if с.get("элементов") is not None:
                            к["элементов"].append(с["элементов"])
                        к["utc"].append(с.get("utc"))
                        if о_сл is not None:
                            о_сл.write(json.dumps({
                                "utc": с.get("utc"), "источник": с.get("источник"),
                                "список": с.get("список"), "код": с.get("код"),
                                "элементов": с.get("элементов"),
                                "строки": [{п_: р.get(п_) for п_ in ПОЛЯ_СТРОКИ}
                                           for р in (с.get("строки") or [])],
                            }, ensure_ascii=False) + "\n")
            except (zlib.error, EOFError, OSError) as e:
                # ОБРЫВ GZIP -- не повод потерять сутки. Диск на lab-miami кончился
                # 14-16.10, и 9 файлов из 47 остались с обрезанным хвостом: начало цело.
                # Прочитанное до обрыва идёт в счёт, обрыв записывается числом строк.
                оборван = строк
                причина = f"{type(e).__name__}: {str(e)[:120]}"
                print(f"  ОБРЫВ {ф}: прочитано строк {строк}, {причина}", flush=True)
            finally:
                if о_сл is not None:
                    о_сл.close()
            if сл_ф is not None and сл_ф.exists():
                записанные.append(сл_ф)
            для_суток = {}
            for сп, к in по_списку.items():
                т = sorted(x for x in к["utc"] if x)
                пропуски = []
                for a, b in zip(т, т[1:]):
                    д = (time.mktime(time.strptime(b, "%Y-%m-%dT%H:%M:%SZ"))
                         - time.mktime(time.strptime(a, "%Y-%m-%dT%H:%M:%SZ")))
                    if д > порог_с:
                        пропуски.append({"от": a, "до": b, "минут": round(д / 60, 1)})
                эл = к["элементов"]
                для_суток[сп] = {
                    "снимков": к["снимков"],
                    "коды": {str(x): y for x, y in к["коды"].items()},
                    "ошибки": dict(к["ошибки"]),
                    "байт_ответов": к["байт"],
                    "элементов_медиана": (sorted(эл)[len(эл) // 2] if эл else None),
                    "первый": т[0] if т else None, "последний": т[-1] if т else None,
                    "пропусков": len(пропуски),
                    "минут_без_снимка_всего": round(sum(x["минут"] for x in пропуски), 1),
                    "пропуски": пропуски[:40]}
            из_[f"{папка.name}/{дата}/{ф.name}"] = {
                "файл_байт": ф.stat().st_size,
                "файл_оборван": оборван, "причина_обрыва": причина,
                "изменён": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime(ф.stat().st_mtime)),
                "строк": строк, "по_списку": для_суток}
    оборванных = sum(1 for v in из_.values() if v.get("файл_оборван") is not None)
    return {"по_файлам": из_, "файлов": len(из_), "оборванных_файлов": оборванных,
            "слим_файлов": len(записанные),
            "слим": [str(p.relative_to(КОРЕНЬ)) for p in записанные]}


def главное(а) -> int:
    сл = службы()
    кат = Path(а.katalog) if а.katalog else рабочий_каталог(сл)
    тд = (кат / "data" / "trending") if кат else None
    диск = shutil.disk_usage(str(Path.home()))
    тело = {
        "что": "жива ли запись трендингов на lab-miami: опись, свод, тонкая выгрузка",
        "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "машина": {"hostname": socket.gethostname(), "ядер": os.cpu_count(),
                   "дом": str(Path.home())},
        "диск_гб": {"всего": round(диск.total / 1e9, 1),
                    "занято": round(диск.used / 1e9, 1),
                    "свободно": round(диск.free / 1e9, 1)},
        "кэш": кэш_опись(),
        "службы": сл,
        "каталог_записи": str(тд) if тд else None,
    }
    if тд and тд.is_dir():
        тело["файлы_на_хосте"] = [
            {"файл": str(p.relative_to(тд)), "байт": p.stat().st_size,
             "изменён": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                      time.gmtime(p.stat().st_mtime))}
            for p in sorted(тд.rglob("*.jsonl.gz"))]
        дни = tuple(x for x in (а.dni or "").split(",") if x.strip())
        тело["свод"] = свод_записи(тд, а.porog_s, not а.bez_slima, дни)
    else:
        тело["свод"] = {"нет": "каталога записи не нашлось"}

    ф = П / f"trending_svod{а.metka}.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    пути = [ф] + [КОРЕНЬ / x for x in (тело.get("свод", {}).get("слим") or [])]
    try:
        import sys  # noqa: PLC0415
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import podbivka_run as R  # noqa: PLC0415
        for p in пути:
            R.записано(p)
    except Exception:  # noqa: BLE001, S110
        pass
    кратко = {k: v for k, v in тело.items() if k not in ("службы", "файлы_на_хосте")}
    print(json.dumps(кратко, ensure_ascii=False, indent=1)[:6000], flush=True)
    print("служба min активна:", сл["podbivka-trending-min"]["активна"],
          "| таймер включён:", сл["podbivka-trending-min"]["включена"], flush=True)
    print(f"записано {ф}, тонких файлов {тело.get('свод', {}).get('слим_файлов')}",
          flush=True)
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--katalog", default="", help="корень checkout с data/trending")
    р.add_argument("--porog-s", type=int, default=300, help="окно без снимка, секунд")
    р.add_argument("--dni", default="", help="только эти сутки, через запятую")
    р.add_argument("--bez-slima", action="store_true")
    р.add_argument("--metka", default="")
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
