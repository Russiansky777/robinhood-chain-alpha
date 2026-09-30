#!/usr/bin/env python3
"""Шреды против WS: насколько наш сигнал позже последнего шреда слота.

Числа, которые просил владелец (пункт 4, утро 27.09):
  * медиана и p90 разницы "WS - последний шред слота" по ВСЕМ слотам;
  * то же отдельно по слотам, где был НАШ сигнал (transactionSubscribe);
  * шредов принято, слотов покрыто.

Измерительный код: тестов нет по правилу владельца ("самопроверки -- только
денежный путь"). Только чтение: slots.jsonl читается потоком, ничего не
пишется на хост.

ЧАСЫ. Все времена в записи -- монотонные (time.monotonic) ОДНОГО процесса,
поэтому вычитать их друг из друга можно. Стенные часы для этого не годятся:
на них правится время по NTP, и разница в десятки миллисекунд утонула бы.
"""
import argparse
import gzip
import json
import os
import statistics


def строки(путь: str):
    откр = gzip.open if путь.endswith(".gz") else open
    with откр(путь, "rt", encoding="utf-8", errors="replace") as ф:
        for ln in ф:
            ln = ln.strip()
            if ln.startswith("{"):
                try:
                    yield json.loads(ln)
                except ValueError:
                    continue


def процентиль(ряд: list, доля: float):
    if not ряд:
        return None
    р = sorted(ряд)
    i = min(len(р) - 1, max(0, int(round(доля * (len(р) - 1)))))
    return р[i]


def свод(ряд: list, имя: str) -> dict:
    return {"имя": имя, "n": len(ряд),
            "медиана_мс": (round(statistics.median(ряд) * 1000, 2) if ряд else None),
            "p90_мс": (round(процентиль(ряд, 0.9) * 1000, 2) if ряд else None),
            "мин_мс": (round(min(ряд) * 1000, 2) if ряд else None),
            "макс_мс": (round(max(ряд) * 1000, 2) if ряд else None)}


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--out-dir", default="/home/bot/shred_probe_data")
    р.add_argument("--json", default=None, help="куда положить числа (по желанию)")
    # ОКНО ПО СЛОТУ. Файл slots.jsonl НЕ обнуляется при смене поставщика: в нём
    # лежат и слоты BlockRazor с 26-27.09, и слоты Triton с 30.09. Без окна
    # медиана считается по всем 255 тысячам слотов сразу, то есть отвечает про
    # BlockRazor, а не про того поставщика, о котором спрашивают. Такое число
    # выглядит как замер и им не является.
    р.add_argument("--ot-slota", type=int, default=0,
                   help="считать только слоты НЕ НИЖЕ этого (0 -- все)")
    р.add_argument("--do-slota", type=int, default=0,
                   help="считать только слоты НЕ ВЫШЕ этого (0 -- без предела)")
    р.add_argument("--imya-okna", default="",
                   help="как назвать окно в отчёте, например 'Triton с 11:25:32Z'")
    а = р.parse_args()

    пути = [os.path.join(а.out_dir, "slots.jsonl")]
    пути += sorted(os.path.join(а.out_dir, ф) for ф in os.listdir(а.out_dir)
                   if ф.startswith("slots.jsonl.") and ф.endswith(".gz")) \
        if os.path.isdir(а.out_dir) else []

    все, с_сигналом, разброс = [], [], []
    слотов = шредов = с_ws = с_нашим = вне_окна = 0
    первый_слот = последний_слот = None
    for путь in пути:
        if not os.path.exists(путь):
            continue
        for з in строки(путь):
            с = з.get("slot")
            # ВНЕ ОКНА -- НЕ СЧИТАЕМ ВОВСЕ, включая счётчики: иначе "шредов
            # принято" осталось бы про обоих поставщиков, а медиана -- про одного.
            if а.ot_slota and (not isinstance(с, int) or с < а.ot_slota):
                вне_окна += 1
                continue
            if а.do_slota and (not isinstance(с, int) or с > а.do_slota):
                вне_окна += 1
                continue
            слотов += 1
            шредов += int(з.get("shreds") or 0)
            if isinstance(с, int):
                первый_слот = с if первый_слот is None else min(первый_слот, с)
                последний_слот = с if последний_слот is None else max(последний_слот, с)
            посл = з.get("last_mono")
            перв = з.get("first_mono")
            if перв is not None and посл is not None:
                разброс.append(float(посл) - float(перв))
            ws = з.get("ws_slot_mono")
            сиг = з.get("ws_signal_mono")
            if ws is not None:
                с_ws += 1
            if сиг is not None:
                с_нашим += 1
            # РАЗНИЦА СЧИТАЕТСЯ ТОЛЬКО когда в слоте есть И шреды, И WS:
            # иначе это не замер, а дырка в данных.
            if посл is None:
                continue
            if ws is not None:
                все.append(float(ws) - float(посл))
            if сиг is not None:
                с_сигналом.append(float(сиг) - float(посл))

    признак = {}
    п_признака = os.path.join(а.out_dir, "heartbeat.json")
    if os.path.exists(п_признака):
        try:
            with open(п_признака, encoding="utf-8") as ф:
                признак = json.load(ф)
        except ValueError:
            признак = {}

    итог = {
        "каталог": а.out_dir,
        # ОКНО НАЗЫВАЕТСЯ В ОТЧЁТЕ ВСЕГДА, даже когда его нет: иначе по числу
        # нельзя понять, о каком поставщике шредов оно говорит.
        "окно_имя": а.imya_okna or ("все слоты файла" if not (а.ot_slota or а.do_slota)
                                     else "окно по слоту"),
        "окно_от_слота": а.ot_slota or None,
        "окно_до_слота": а.do_slota or None,
        "слотов_вне_окна_пропущено": вне_окна,
        "слотов_в_файле": слотов,
        "шредов_принято_по_слотам": шредов,
        "шредов_принято_по_признаку": признак.get("shreds"),
        "слотов_с_ws": с_ws,
        "слотов_с_нашим_сигналом": с_нашим,
        "слоты_от": первый_слот, "слоты_до": последний_слот,
        "обрывов_ws": признак.get("ws_breaks"),
        "пакетов_вне_окна": признак.get("packets_out_of_window"),
        "ws_минус_последний_шред": свод(все, "по всем слотам"),
        "ws_сигнал_минус_последний_шред": свод(с_сигналом, "по слотам с нашим сигналом"),
        "разброс_шредов_в_слоте": свод(разброс, "последний минус первый шред"),
    }
    print(json.dumps(итог, ensure_ascii=False, indent=1))
    # Три строки словами -- их и читать владельцу.
    for ключ in ("ws_минус_последний_шред", "ws_сигнал_минус_последний_шред"):
        с = итог[ключ]
        print(f"{с['имя']}: n={с['n']}, медиана {с['медиана_мс']} мс, "
              f"p90 {с['p90_мс']} мс")
    print(f"шредов принято {итог['шредов_принято_по_слотам']}, "
          f"слотов покрыто {слотов} (с нашим сигналом {с_нашим})")
    print(f"окно: {итог['окно_имя']}"
          + (f", от слота {а.ot_slota}" if а.ot_slota else "")
          + (f", до слота {а.do_slota}" if а.do_slota else "")
          + f", пропущено вне окна {вне_окна} слотов")
    if а.json:
        with open(а.json, "w", encoding="utf-8") as ф:
            json.dump(итог, ф, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
