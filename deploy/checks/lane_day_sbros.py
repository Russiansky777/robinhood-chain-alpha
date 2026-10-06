#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сброс НАЧАЛА ОТСЧЁТА суточного рубильника полосы (lane_day.json).

ЗАЧЕМ (слово владельца 06.10, после возврата билетов): "обновить начальный
баланс отсчёта на нынешнее время, чтобы рубильник ресетнулся и считал заново
для новой полосы". Рубильник LANE_KILL_DROP_SOL считает просадку от баланса на
00:00 Мадрида. 06.10 в 17:01:31Z он сработал честно: просадка 2.276795 SOL при
пороге 2.0 -- но ВСЯ она набрана СТАРЫМИ билетами (до 2.0 SOL на сделку), а
полоса с 17:01Z торгует новыми (0.1-0.5). Чтобы порог мерил новую полосу, а не
хвост старой, начало отсчёта переносится на СЕЙЧАС.

ЧТО ЭТО НЕ ДЕЛАЕТ. Рубильник (файл KILL_OWN_SEND) этим скриптом НЕ снимается:
его снимает отдельный прогон со своими тремя гейтами. Здесь только начало
отсчёта.

ЧЕСТНОСТЬ ЧИСЛА. Баланс берётся ПО ЦЕПИ тем же вызовом, которым его берёт
служба (bloom_detector.Helius.баланс_sol по кошельку полосы), а не руками из
входа прогона: иначе в начале отсчёта оказалось бы число, которого на цепи
никогда не было. Прежняя запись не исчезает: она уезжает в копию рядом и
печатается в журнал прогона.

САМОПРОВЕРКА ДЕНЕЖНОГО ПУТИ. После записи файл перечитывается ТЕМ ЖЕ модулем,
которым его читает служба (bloom_own_send.порог_kill_по_балансу), и если он не
говорит "просадка около нуля, рубильник не ставим" -- файл возвращается из
копии.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ЗДЕСЬ = Path(__file__).resolve()
КОРЕНЬ = ЗДЕСЬ.parents[2]


def _модули():
    """Модули службы: сперва каталог кода на хосте, потом дерево репозитория."""
    for п in (Path("/home/bot/bloom_executor"), КОРЕНЬ / "analysis"):
        if (п / "bloom_own_send.py").exists() and str(п) not in sys.path:
            sys.path.insert(0, str(п))
    import bloom_exec_state as ST  # noqa: PLC0415
    import bloom_own_send as O  # noqa: PLC0415
    return ST, O


def баланс_по_цепи(O) -> tuple[float | None, str]:
    """Баланс кошелька полосы ПО ЦЕПИ -- тем же вызовом, что у службы."""
    try:
        import bloom_detector as D  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return None, f"модуль детектора не загружен: {type(exc).__name__}"
    if not os.environ.get("HELIUS_API_KEY"):
        return None, "HELIUS_API_KEY в окружении нет -- баланс не спросить"
    try:
        h = D.Helius(служба="lane_day_sbros")
        б = h.баланс_sol(O.кошелёк_полосы())
    except Exception as exc:  # noqa: BLE001
        return None, f"цепь не ответила: {type(exc).__name__}: {str(exc)[:120]}"
    if not isinstance(б, (int, float)):
        return None, "цепь вернула не число"
    return float(б), "getBalance по кошельку полосы"


def показать(ST, O, состояние) -> dict:
    путь = O.путь_суток_полосы(состояние)
    было = {}
    if путь is not None and путь.exists():
        try:
            было = json.loads(путь.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError) as exc:
            было = {"ошибка_чтения": f"{type(exc).__name__}"}
    return {"путь": str(путь), "есть": bool(путь and путь.exists()), "было": было}


def сбросить(ST, O, состояние, *, баланс_sol: float, причина: str,
             сейчас: float | None = None) -> dict:
    """Перенести начало отсчёта на СЕЙЧАС. Не сошлось -- возврат из копии."""
    из_ = {"ok": False}
    путь = O.путь_суток_полосы(состояние)
    if путь is None:
        из_["why_not"] = "каталог состояния неизвестен"
        return из_
    сейчас = float(сейчас if сейчас is not None else time.time())
    сут = O.начало_суток(сейчас)
    было = {}
    копия = None
    if путь.exists():
        try:
            было = json.loads(путь.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            было = {}
        копия = путь.with_name(путь.name + ".bak-"
                                + time.strftime("%Y%m%dT%H%M%SZ",
                                                 time.gmtime(сейчас)))
        копия.write_bytes(путь.read_bytes())
    запись = {"day": сут["day"], "open_sol": round(float(баланс_sol), 9),
               "open_ts": сейчас, "day_how": сут["how"],
               "sbros_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                           time.gmtime(сейчас)),
               "sbros_prichina": str(причина)[:300],
               "prezhnij_open_sol": было.get("open_sol"),
               "prezhnij_open_ts": было.get("open_ts")}
    врем = путь.with_name(путь.name + ".tmp")
    врем.write_text(json.dumps(запись, ensure_ascii=False), encoding="utf-8")
    os.replace(врем, путь)
    # САМОПРОВЕРКА ДЕНЕЖНОГО ПУТИ: читает служебный модуль, а не этот скрипт.
    пор = O.порог_kill_по_балансу(состояние, баланс_sol=баланс_sol,
                                   сейчас=сейчас)
    # ЧИСЛА СВЕРЯЮТСЯ ЧЕРЕЗ None, А НЕ ЧЕРЕЗ `or`: ровно ноль просадки -- это
    # то, чего мы и ждём, а `пор.get("drop_sol") or 1.0` превратил бы 0.0 в 1.0
    # и ЗАБРАКОВАЛ бы удачный сброс (поймано самопроверкой ниже).
    _от = пор.get("open_sol")
    _пр = пор.get("drop_sol")
    сошлось = (пор.get("ok") and пор.get("kill") is False
                and isinstance(_от, (int, float))
                and abs(float(_от) - запись["open_sol"]) < 1e-9
                and isinstance(_пр, (int, float)) and abs(float(_пр)) < 1e-6)
    из_.update(zapis=запись, kopija=(str(копия) if копия else None),
                porog=пор, bylo=было)
    if not сошлось:
        if копия is not None:
            os.replace(копия, путь)
            из_["vozvrat"] = "файл возвращён из копии: самопроверка не сошлась"
        else:
            путь.unlink(missing_ok=True)
            из_["vozvrat"] = "файл убран: самопроверка не сошлась, копии не было"
        из_["why_not"] = "перечитанный файл не даёт нулевую просадку"
        return из_
    из_["ok"] = True
    return из_


def self_test() -> int:
    """Проверки без сети и без хоста: на временном каталоге состояния."""
    import tempfile  # noqa: PLC0415
    ST, O = _модули()
    всего = [0, 0]

    def chk(имя, усл, что=None):
        всего[1] += 1
        if усл:
            всего[0] += 1
            print(f"  [ok  ] {имя}")
        else:
            print(f"  [СБОЙ] {имя}: {что!r}")

    with tempfile.TemporaryDirectory() as д:
        st = ST.ExecState(base=Path(д))
        путь = O.путь_суток_полосы(st)
        сейчас = 1_760_000_000.0
        сут = O.начало_суток(сейчас)
        путь.write_text(json.dumps({"day": сут["day"], "open_sol": 8.185112693,
                                     "open_ts": сейчас - 60_000.0}),
                         encoding="utf-8")
        # ДО сброса: просадка видна и рубильник сработал бы
        до = O.порог_kill_по_балансу(st, баланс_sol=5.908317283, сейчас=сейчас,
                                      порог=2.0)
        chk("до сброса просадка больше порога -- рубильник сработал бы",
            до.get("kill") is True and round(до["drop_sol"], 6) == 2.276795, до)
        из_ = сбросить(ST, O, st, баланс_sol=5.908317283,
                        причина="проверка", сейчас=сейчас)
        chk("сброс прошёл", из_.get("ok") is True, из_)
        chk("начало отсчёта стало нынешним балансом",
            из_["zapis"]["open_sol"] == 5.908317283, из_.get("zapis"))
        chk("прежняя запись названа в файле, а не потеряна",
            из_["zapis"]["prezhnij_open_sol"] == 8.185112693, из_.get("zapis"))
        chk("копия прежнего файла лежит рядом",
            из_["kopija"] and Path(из_["kopija"]).exists(), из_.get("kopija"))
        после = O.порог_kill_по_балансу(st, баланс_sol=5.908317283,
                                         сейчас=сейчас, порог=2.0)
        chk("после сброса просадка ноль и рубильник НЕ ставится",
            после.get("kill") is False and abs(после["drop_sol"]) < 1e-9, после)
        # ПОРОГ ОСТАЛСЯ ПОРОГОМ: новая просадка того же размера снова рубит.
        снова = O.порог_kill_по_балансу(st, баланс_sol=5.908317283 - 2.1,
                                         сейчас=сейчас + 3600, порог=2.0)
        chk("сброс НЕ отключает рубильник: новая просадка 2.1 снова рубит",
            снова.get("kill") is True, снова)
        # ВОЗВРАТ ИЗ КОПИИ, если перечитанный файл не даёт нулевую просадку:
        # подсовываем баланс, не равный тому, что записан.
        st2 = ST.ExecState(base=Path(д) / "вт")
        п2 = O.путь_суток_полосы(st2)
        п2.write_text(json.dumps({"day": O.начало_суток(сейчас)["day"],
                                   "open_sol": 3.0, "open_ts": сейчас - 10}),
                       encoding="utf-8")
        настоящий = O.порог_kill_по_балансу
        try:
            O.порог_kill_по_балансу = lambda *a, **k: {"ok": True, "kill": True}
            пл = сбросить(ST, O, st2, баланс_sol=2.5, причина="пл",
                           сейчас=сейчас)
        finally:
            O.порог_kill_по_балансу = настоящий
        chk("самопроверка не сошлась -- файл возвращён из копии",
            пл.get("ok") is False and json.loads(
                п2.read_text(encoding="utf-8"))["open_sol"] == 3.0, пл)
    print(f"самопроверка сброса отсчёта полосы: {всего[0]}/{всего[1]} пройдено")
    return 0 if всего[0] == всего[1] else 1


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--self-test", action="store_true")
    р.add_argument("--pokazat", action="store_true")
    р.add_argument("--sbros", action="store_true")
    р.add_argument("--prichina", default="")
    # ПРИЧИНА ФАЙЛОМ, А НЕ ЗНАКАМИ В КОМАНДЕ. Прогон уезжает на хост по ssh от
    # root, и свободный текст владельца в команде -- это апостроф, который
    # однажды станет чужой командой (так уже ломалась правка групп 04.10).
    # Поэтому текст едет отдельным файлом, а в команде остаются литералы.
    р.add_argument("--prichina-fajl", default="")
    а = р.parse_args()
    if getattr(а, "self_test"):
        return self_test()
    ST, O = _модули()
    состояние = ST.ExecState()
    сейчас_показ = показать(ST, O, состояние)
    print("файл суток полосы:", сейчас_показ["путь"])
    print("было:", json.dumps(сейчас_показ["было"], ensure_ascii=False))
    бал, откуда = баланс_по_цепи(O)
    print(f"баланс полосы по цепи: {бал} ({откуда})")
    if not а.sbros:
        if isinstance(бал, (int, float)):
            пор = O.порог_kill_по_балансу(состояние, баланс_sol=бал)
            print("порог сейчас:", json.dumps(пор, ensure_ascii=False))
        return 0
    причина = а.prichina
    if getattr(а, "prichina_fajl", ""):
        try:
            причина = Path(а.prichina_fajl).read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"СБОЙ: причина не читается ({type(exc).__name__})",
                  file=sys.stderr)
            return 2
    if not причина:
        print("СБОЙ: для --sbros нужна --prichina или --prichina-fajl",
              file=sys.stderr)
        return 2
    if not isinstance(бал, (int, float)):
        print(f"СБОЙ: баланса по цепи нет ({откуда}) -- отсчёт не переношу",
              file=sys.stderr)
        return 3
    из_ = сбросить(ST, O, состояние, баланс_sol=бал, причина=причина)
    print(json.dumps(из_, ensure_ascii=False, indent=1))
    if не_ок := (not из_.get("ok")):
        print("СБОЙ: отсчёт не перенесён", file=sys.stderr)
    return 4 if не_ок else 0


if __name__ == "__main__":
    raise SystemExit(main())
