#!/usr/bin/env python3
"""Цены по архиву PumpApi для проверки трендингов: состояния пула на нужные моменты.

Один скан суток -- и дальше всё считается офлайн. На каждое первое появление адреса в
списке (таблицы data/trending/pervye/<дата>.jsonl.gz) пишутся состояния пула ровно на те
моменты, какие нужны правилу и контролю:

  * «м»   -- на метку появления (ликвидность на момент появления, для описательной таблицы);
  * «в»   -- вход: метка + 60 с (честная цена S0_дно -- последнее событие НЕ ПОЗЖЕ метки);
  * «в5 / в15 / в60» -- выходы +5 / +15 / +60 минут от входа;
  * «с»   -- случайный вход: метка + r, r из [60, 3600] с, зерно от (дата, адрес, список),
    и его выходы «с5 / с15 / с60» -- контроль «что даёт само сидение в этих токенах».

Состояние -- [ts, quoteInPool, tokensInPool, vQuoteInBondingCurve, vTokensInBondingCurve]:
обе базы рядом, выбор базы -- потом по типу пула (у кривой pump.fun цена по реальным
резервам стремится к нулю при распродаже и даёт -99 п.п. там, где -33).

Отдельно собираются события create: задержка от создания до первого появления. Адреса для
создания берутся по ВСЕМ суткам таблиц pervye (минт мог родиться в другие сутки окна),
а состояния -- только по --den.

Строки отбираются дешёвым поиском mint/poolId регуляркой; json.loads -- только для
попавших в заказ. Только чтение. Выход: data/podbivka/trending_ceny_<день>.json.gz.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import hashlib
import io
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_arhiv_den as А  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
ПЕРВЫЕ = КОРЕНЬ / "data" / "trending" / "pervye"
ЗАДЕРЖКА_ВХОДА_С = 60
ВЫХОДЫ_С = (300, 900, 3600)
СЛУЧ_ОТ, СЛУЧ_ДО = 60, 3600
р_адрес = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
р_mint = re.compile(r'"mint":"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_pool = re.compile(r'"poolId":"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_ts = re.compile(r'"timestamp":(\d+)')
# action отсеивает transfer/distributeFees и прочее до json.loads: в потоке 27.09 на
# transfer приходилась почти половина событий, а разбор строки архива -- самая дорогая
# часть прохода.
р_action = re.compile(r'"action":"(buy|sell|create)"')


def мс(utc: str) -> int:
    return int(time.mktime(time.strptime(utc, "%Y-%m-%dT%H:%M:%SZ")) * 1000)


def случайное_смещение(дата: str, адрес: str, список: str) -> int:
    """Смещение контрольного входа: зерно от самой тройки, одно и то же при повторе."""
    х = hashlib.sha256(f"{дата}|{адрес}|{список}".encode()).digest()
    ч = int.from_bytes(х[:8], "big")
    return СЛУЧ_ОТ + ч % (СЛУЧ_ДО - СЛУЧ_ОТ + 1)


def читать_первые(ф: Path) -> list:
    ряды = []
    with gzip.open(ф, "rb") as о:
        for б in о:
            try:
                р = json.loads(б.decode("utf-8", "replace"))
            except ValueError:
                continue
            if р.get("вид_строки") or "список" not in р:
                continue
            ряды.append(р)
    return ряды


def заказ(день: str, окно: tuple) -> tuple:
    """(появления дня, адреса всех суток ОКНА) -- адреса окна нужны для create."""
    все = set()
    for ф in sorted(glob.glob(str(ПЕРВЫЕ / "*.jsonl.gz"))):
        if not (окно[0] <= Path(ф).name[:10] <= окно[1]):
            continue
        for р in читать_первые(Path(ф)):
            а = р.get("адрес")
            if а and р.get("вид") in ("минт", "пул") and р_адрес.match(а):
                все.add(а)
    появления = []
    видел = set()
    for р in читать_первые(ПЕРВЫЕ / f"{день}.jsonl.gz"):
        а, сп = р.get("адрес"), р.get("список")
        if not (а and сп) or р.get("вид") not in ("минт", "пул"):
            continue
        if not р_адрес.match(а) or (а, сп) in видел:
            continue
        видел.add((а, сп))
        т = мс(р["utc"])
        r = случайное_смещение(день, а, сп)
        появления.append({"а": а, "с": сп, "вид": р["вид"], "t": т, "r": r,
                          "ранг": р.get("ранг"), "объём": р.get("объём"),
                          "ликвидность_списка": р.get("ликвидность"),
                          "от_предыдущего_снимка_с": р.get("от_предыдущего_снимка_с"),
                          "к": {}})
    return появления, все


def главное(а) -> int:
    import requests  # noqa: PLC0415
    import zstandard  # noqa: PLC0415

    появления, все_адреса = заказ(а.den, (а.okno_s, а.okno_po))
    цели: dict = {}
    for i, п in enumerate(появления):
        вх = п["t"] + ЗАДЕРЖКА_ВХОДА_С * 1000
        сл = п["t"] + п["r"] * 1000
        точки = [(п["t"], "м"), (вх, "в"), (сл, "с")]
        for ш in ВЫХОДЫ_С:
            точки.append((вх + ш * 1000, f"в{ш // 60}"))
            точки.append((сл + ш * 1000, f"с{ш // 60}"))
        ц = цели.setdefault(п["а"], {"кп": [], "и": 0, "посл": None, "пул": None,
                                     "кот": None, "событий": 0,
                                     "первое": None, "последнее": None})
        for т, к in точки:
            ц["кп"].append((т, i, к))
    for ц in цели.values():
        ц["кп"].sort()

    создания: dict = {}
    т0 = time.mktime(time.strptime(f"{а.den}T00", "%Y-%m-%dT%H"))
    часы = [time.strftime("%Y/%m/%d/%H", time.gmtime(т0 + 3600 * k))
            for k in range(а.chasov)]
    счёт = {"строк": 0, "событий_целей": 0, "часов": 0, "ошибки": []}
    нач = time.time()
    for ч in часы:
        url = f"https://replay.pumpapi.io/{ч}.jsonl.zst"
        try:
            with А.открыть_час(requests, url, ч) as о:
                if о.status_code != 200:
                    счёт["ошибки"].append(f"{ч}: http {о.status_code}")
                    continue
                счёт["часов"] += 1
                дек = zstandard.ZstdDecompressor().stream_reader(о.raw)
                for стр in io.TextIOWrapper(дек, encoding="utf-8", errors="replace"):
                    счёт["строк"] += 1
                    if not р_action.search(стр):
                        continue
                    ключ = None
                    м = р_mint.search(стр)
                    if м and м.group(1) in все_адреса:
                        ключ = м.group(1)
                    else:
                        p = р_pool.search(стр)
                        if p and p.group(1) in все_адреса:
                            ключ = p.group(1)
                    if ключ is None:
                        continue
                    try:
                        е = json.loads(стр)
                    except ValueError:
                        continue
                    ts = е.get("timestamp")
                    if ts is None:
                        мт = р_ts.search(стр)
                        ts = int(мт.group(1)) if мт else None
                    if not isinstance(ts, (int, float)):
                        continue
                    ts = int(ts)
                    if е.get("action") == "create":
                        стар = создания.get(ключ)
                        if стар is None or ts < стар:
                            создания[ключ] = ts
                    ц = цели.get(ключ)
                    if ц is None:
                        continue
                    счёт["событий_целей"] += 1
                    ц["событий"] += 1
                    ц["пул"] = ц["пул"] or е.get("pool")
                    ц["кот"] = ц["кот"] or е.get("quoteMint")
                    ц["первое"] = min(ц["первое"] or ts, ts)
                    ц["последнее"] = max(ц["последнее"] or ts, ts)
                    while ц["и"] < len(ц["кп"]) and ц["кп"][ц["и"]][0] < ts:
                        _т, i, к = ц["кп"][ц["и"]]
                        появления[i]["к"][к] = ц["посл"]
                        ц["и"] += 1
                    ц["посл"] = [ts, е.get("quoteInPool"), е.get("tokensInPool"),
                                 е.get("vQuoteInBondingCurve"),
                                 е.get("vTokensInBondingCurve")]
        except Exception as e:  # noqa: BLE001
            счёт["ошибки"].append(f"{ч}: {type(e).__name__}: {str(e)[:120]}")
        print(f"  {ч}: строк {счёт['строк']}, событий целей {счёт['событий_целей']}, "
              f"минут {round((time.time() - нач) / 60, 1)}", flush=True)

    # хвост: точки позже последнего события -- последнее известное состояние
    for ц in цели.values():
        while ц["и"] < len(ц["кп"]):
            _, i, к = ц["кп"][ц["и"]]
            появления[i]["к"][к] = ц["посл"]
            ц["и"] += 1

    тело = {"что": "состояния пулов на моменты правила и контроля (трендинги)",
            "день": а.den, "часов": len(часы), "когда": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "задержка_входа_с": ЗАДЕРЖКА_ВХОДА_С, "выходы_с": list(ВЫХОДЫ_С),
            "случайный_вход_с": [СЛУЧ_ОТ, СЛУЧ_ДО],
            "окно_суток": [а.okno_s, а.okno_po],
            "счёт": счёт, "адресов_заказано": len(цели),
            "адресов_в_архиве": sum(1 for ц in цели.values() if ц["событий"]),
            "созданий_найдено": len(создания),
            "создания": создания,
            "адреса": {к: {"пул": ц["пул"], "котировка": ц["кот"],
                           "событий": ц["событий"], "первое": ц["первое"],
                           "последнее": ц["последнее"]} for к, ц in цели.items()},
            "появления": появления}
    ф = П / f"trending_ceny_{а.den}.json.gz"
    with gzip.open(ф, "wt", encoding="utf-8") as о:
        json.dump(тело, о, ensure_ascii=False, separators=(",", ":"))
    try:
        import podbivka_run as R  # noqa: PLC0415
        R.записано(ф)
    except Exception:  # noqa: BLE001, S110
        pass
    print(f"появлений {len(появления)}, адресов {len(цели)}, "
          f"в архиве {тело['адресов_в_архиве']}, создания {len(создания)}; записано {ф}",
          flush=True)
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--den", required=True, help="сутки YYYY-MM-DD (UTC)")
    р.add_argument("--chasov", type=int, default=26,
                   help="часов от начала суток: 24 + 2 на хвост выхода +60 мин")
    р.add_argument("--okno-s", default="2026-10-17", help="первые сутки окна таблиц")
    р.add_argument("--okno-po", default="2026-10-24", help="последние сутки окна таблиц")
    р.add_argument("--metka", default="")
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
