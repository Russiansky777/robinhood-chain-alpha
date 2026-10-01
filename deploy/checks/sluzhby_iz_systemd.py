#!/usr/bin/env python3
"""ExecStart служб -> строка проб для klyuchi_pochistit (--sluzhby).

ЗАЧЕМ. Путь к файлу службы угадывать нельзя: он живёт в systemd, и у разных
хостов разный. Поэтому ExecStart читается у самой службы, а здесь из него
собирается команда ПРОВЕРКИ -- со флагами, какие именно эта служба умеет.

ФЛАГИ У КАЖДОЙ СЛУЖБЫ СВОИ. grpc_feed_probe не знает --check-only, у него только
--self-test; у детектора проверка требует файл конфигурации. Один флаг на всех
дал бы отказ, который мы прочитали бы как "служба не стартует без ключа".

ФАЙЛ, КОТОРОГО НЕТ В СПИСКЕ ФЛАГОВ, ПРОПУСКАЕТСЯ МОЛЧА НЕЛЬЗЯ: он уходит в
'пропущено', чтобы пропуск был виден и не читался как 'проба прошла'.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

ФЛАГИ = {
    "bloom_detector.py": "--check-only --config /home/bot/bloom_executor/konfig_snapshot.json",
    "bloom_seller.py": "--check-only",
    "dbot_sold_position_guard.py": "--check-only",
    "solana_detect_probe.py": "--check-only",
    "grpc_feed_probe.py": "--self-test",
}
# argv[]=... у systemd, либо просто строка команды
ФАЙЛ = re.compile(r"(\S*/([A-Za-z0-9_]+\.py))")


def разобрать(строки) -> dict:
    пробы, пропущено = [], []
    for строка in строки:
        строка = (строка or "").strip()
        if not строка or "|" not in строка:
            continue
        юнит, _, exe = строка.partition("|")
        юнит = юнит.strip()
        м = ФАЙЛ.search(exe)
        if not м:
            пропущено.append({"юнит": юнит, "почему": "в ExecStart нет файла .py"})
            continue
        путь, файл = м.group(1), м.group(2)
        флаги = ФЛАГИ.get(файл)
        if not флаги:
            пропущено.append({"юнит": юнит, "файл": файл,
                               "почему": "для этого файла проверка старта не названа"})
            continue
        куски = exe.split()
        питон = куски[0] if куски else "python3"
        пробы.append({"юнит": юнит, "команда": f"{питон} {путь} {флаги}"})
    return {"пробы": пробы, "пропущено": пропущено,
             "строка": ";;".join(f"{п['юнит']}={п['команда']}" for п in пробы)}


def самопроверка() -> int:
    плохо = []

    def chk(имя, усл, *л):
        print(("ok  " if усл else "СБОЙ") + "  " + имя + ("" if усл else "  " + repr(л)))
        if not усл:
            плохо.append(имя)

    р = разобрать([
        "bloom-detector|/home/bot/venv/bin/python -u /home/bot/bloom_executor/bloom_detector.py --serve --config /x.json",
        "grpc-feed-probe|/home/bot/venv/bin/python /home/bot/feed_probe/grpc_feed_probe.py --state-dir /s",
        "redis|/usr/bin/redis-server /etc/redis/redis.conf",
        "чужая|/home/bot/venv/bin/python /home/bot/x/neizvestnyj.py",
        "",
    ])
    по = {п["юнит"]: п["команда"] for п in р["пробы"]}
    chk("детектор: взят его путь и ЕГО флаги с конфигом",
        по["bloom-detector"].endswith("--check-only --config "
                                       "/home/bot/bloom_executor/konfig_snapshot.json")
        and "/home/bot/bloom_executor/bloom_detector.py" in по["bloom-detector"],
        по.get("bloom-detector"))
    chk("фид: только --self-test, --check-only ему не навязан",
        по["grpc-feed-probe"].endswith("--self-test")
        and "--check-only" not in по["grpc-feed-probe"], по.get("grpc-feed-probe"))
    chk("питон взят из ExecStart, а не придуман",
        по["bloom-detector"].startswith("/home/bot/venv/bin/python"))
    chk("служба без .py в ExecStart -- в пропущено, а не в пробы",
        any(x["юнит"] == "redis" for x in р["пропущено"])
        and "redis" not in по, р["пропущено"])
    chk("незнакомый файл -- в пропущено с причиной словами",
        any(x.get("файл") == "neizvestnyj.py" and "не названа" in x["почему"]
            for x in р["пропущено"]), р["пропущено"])
    chk("строка для --sluzhby собрана через ';;'",
        р["строка"].count(";;") == len(р["пробы"]) - 1, р["строка"])
    chk("пустая строка входа ничего не добавила", len(р["пробы"]) == 2)
    print(f"итог: самопроверка {'норма' if not плохо else 'СБОЙ'}; провалов {len(плохо)}")
    return 1 if плохо else 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("fajl", nargs="?", help="файл строк 'юнит|ExecStart'")
    р.add_argument("--json", action="store_true")
    р.add_argument("--self-test", action="store_true")
    а = р.parse_args()
    if а.self_test:
        return самопроверка()
    строки = (open(а.fajl, encoding="utf-8") if а.fajl else sys.stdin)
    из_ = разобрать(строки)
    if а.json:
        print(json.dumps(из_, ensure_ascii=False, indent=1))
    else:
        print(из_["строка"])
        for п in из_["пропущено"]:
            print(f"ПРОПУЩЕНО: {п}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
