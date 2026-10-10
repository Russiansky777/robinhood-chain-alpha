#!/usr/bin/env python3
"""vol_4vw: живые сделки против модели на ТЕХ ЖЕ сигналах.

Зачем. По выгрузке Code-1 (data/sdelki_polosy_2026-10-10.json, снято 2026-10-10T06:00Z)
группа vol_4vw сделала 55 сделок в окне 2026-10-09T16:29:20Z .. 2026-10-10T03:48:29Z:
билет 0.3 у 52 и 0.01 у трёх, все пятьдесят пять -- на кривой (резерв виртуальный), итог по
цепи сверен у 44. Модель на том же источнике раньше считалась на СВОЕЙ посадке и своём
удержании, и сравнивать её средние с живыми нельзя. Этот проход берёт каждую живую сделку
и считает круг модели на ЕЁ сигнале: вход -- последнее состояние пула не позже нашего
фактического слота входа (our_slot), выход -- последнее состояние не позже
our_slot + hold_slots_fact, билет -- её size_sol. Дальше модель и цепь сравниваются по
сделкам, одна к одной.

Издержки круга в модели берутся из живой строки, когда они там есть (чаевые и приоритет),
иначе -- замер подбивки. Налог на перевод и комиссию пула живая выгрузка по этим сделкам не
заполнила (оба поля пустые), и в модель они не подставляются -- об этом сказано в выходе.

Только чтение архива и собранных файлов. Выход: data/podbivka/4vw_sverka.json.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_arhiv_den as AD      # noqa: E402
import podbivka_bilet as B           # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
WSOL = "So11111111111111111111111111111111111111112"
ИЗДЕРЖКИ_ПО_УМОЛЧАНИЮ = 0.002015    # замер подбивки на живых сделках
ЗАПАС_СЛОТОВ = 8                     # сколько слотов держать после выхода

р_block = re.compile(r'"block":\s*(\d+)')
р_mint = re.compile(r'"mint":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_tip = re.compile(r'"pool":\s*"([a-z0-9\-]+)"')
р_qmint = re.compile(r'"quoteMint":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_fee = re.compile(r'"poolFeeRate":\s*"?([0-9.eE+-]+)')
р_action = re.compile(r'"action":\s*"([a-zA-Z_]+)"')
р_q = re.compile(r'"(quoteInPool|tokensInPool|vQuoteInBondingCurve|vTokensInBondingCurve)":'
                 r'\s*"?([0-9.eE+-]+)')


def часы(с: str, сколько: int) -> list:
    т = time.strptime(с, "%Y-%m-%dT%H")
    н = int(time.mktime(т)) - time.timezone
    return [time.strftime("%Y/%m/%d/%H", time.gmtime(н + 3600 * k)) for k in range(сколько)]


def состояние(стр: str) -> tuple | None:
    поля = dict(р_q.findall(стр))
    x = поля.get("vQuoteInBondingCurve") or поля.get("quoteInPool")
    y = поля.get("vTokensInBondingCurve") or поля.get("tokensInPool")
    try:
        x, y = float(x), float(y)
    except (TypeError, ValueError):
        return None
    return (x, y) if x > 0 and y > 0 else None


def главное(а) -> int:  # noqa: PLR0912, PLR0915
    import io            # noqa: PLC0415
    import requests      # noqa: PLC0415
    import zstandard     # noqa: PLC0415

    цели = json.loads(Path(а.celi).read_text(encoding="utf-8"))
    сделки = цели.get("сделки") or []
    if not сделки:
        print(f"в {а.celi} нет живых сделок -- сверять нечего", flush=True)
        return 2
    # окно слотов по минту: от слота источника до выхода с запасом
    окна: dict = {}
    for с in сделки:
        м, s0 = с.get("mint"), с.get("source_slot")
        вх = с.get("our_slot") or s0
        дер = с.get("hold_slots_fact") or с.get("hold_slots_plan") or 36
        if not (м and s0 and вх):
            continue
        н, к = int(s0) - 2, int(вх) + int(дер) + ЗАПАС_СЛОТОВ
        if м in окна:
            окна[м] = (min(окна[м][0], н), max(окна[м][1], к))
        else:
            окна[м] = (н, к)
    print(f"живых сделок {len(сделки)}, минтов {len(окна)}", flush=True)

    ряды: dict = collections.defaultdict(list)   # mint -> [(блок, x, y, пул, тариф)]
    счёт: collections.Counter = collections.Counter()

    def читать_час(поток) -> None:
        for стр in io.TextIOWrapper(
                zstandard.ZstdDecompressor().stream_reader(поток.raw),
                encoding="utf-8", errors="ignore"):
            счёт["строк"] += 1
            мм = р_mint.search(стр)
            if not мм:
                continue
            м = мм.group(1)
            о = окна.get(м)
            if о is None:
                continue
            бм = р_block.search(стр)
            if not бм:
                continue
            блок = int(бм.group(1))
            if not (о[0] <= блок <= о[1]):
                счёт["строка_вне_окна"] += 1
                continue
            сост = состояние(стр)
            if not сост:
                счёт["строка_без_состояния"] += 1
                continue
            тип = р_tip.search(стр)
            тариф = р_fee.search(стр)
            ам = р_action.search(стр)
            qm = р_qmint.search(стр)
            ряды[м].append((блок, сост[0], сост[1],
                            тип.group(1) if тип else None,
                            float(тариф.group(1)) if тариф else None,
                            ам.group(1) if ам else None,
                            qm.group(1) if qm else None))
            счёт["строк_минта"] += 1

    for ч in часы(а.s, а.chasov):
        url = f"https://replay.pumpapi.io/{ч}.jsonl.zst"
        for попытка in range(3):
            try:
                о = AD.открыть_час(requests, url, ч)
            except Exception as exc:  # noqa: BLE001
                счёт[f"ошибка_{type(exc).__name__}"] += 1
                break
            with о as поток:
                код = getattr(поток, "status_code", 200)
                if код != 200:
                    счёт[f"http_{код}"] += 1
                    счёт["часов_нет"] += 1
                    break
                try:
                    читать_час(поток)
                except Exception as exc:  # noqa: BLE001
                    счёт[f"обрыв_{type(exc).__name__}"] += 1
                    if попытка == 2:
                        счёт["обрыв_часов"] += 1
                    continue
            счёт["часов"] += 1
            счёт[f"час_{ч}"] = 1
            break
        print(f"  {ч}: строк {счёт['строк']}, строк минтов {счёт['строк_минта']}", flush=True)

    for м in ряды:
        ряды[м].sort(key=lambda r: r[0])

    # ---- круг модели на живом сигнале ------------------------------------------
    пары: list = []
    отк: collections.Counter = collections.Counter()
    for с in сделки:
        м = с.get("mint")
        р = ряды.get(м) or []
        вх_слот = с.get("our_slot") or с.get("source_slot")
        дер = с.get("hold_slots_fact") or с.get("hold_slots_plan") or 36
        б = с.get("size_sol")
        if not (м and вх_слот and б):
            отк["в_живой_строке_нет_слота_или_билета"] += 1
            continue
        if not р:
            отк["минта_нет_в_прочитанных_часах"] += 1
            continue
        до_вх = [x for x in р if x[0] <= int(вх_слот)]
        до_вых = [x for x in р if x[0] <= int(вх_слот) + int(дер)]
        if not до_вх:
            отк["нет_состояния_не_позже_входа"] += 1
            continue
        вх = до_вх[-1]
        вых = до_вых[-1]
        пул = вх[3]
        if пул in B.XYK:
            отк[f"пул_требует_f_g ({пул})"] += 1
            continue
        if вх[6] and вх[6] != WSOL:
            отк["котировка_не_SOL"] += 1
            continue
        изд = ИЗДЕРЖКИ_ПО_УМОЛЧАНИЮ
        чай = с.get("tips_sol")
        при = с.get("priority_lamports")
        if чай is not None or при is not None:
            изд = (чай or 0) + (при or 0) / 1e9
            отк["издержки_из_живой_строки"] += 1
        знак = {"pool": пул,
                "модель": {"fee": вх[4] or 0.0,
                           "состояния": {"масштаб": 1.0,
                                         "вход": {"S0": [вх[1], вх[2], вх[0]]},
                                         "выход": {"H": [вых[1], вых[2], вых[0]]}}}}
        кр = B.сделка_по_состояниям(знак, б, "S0", "H", издержки=изд, к_f=1.0, к_g=1.0)
        if кр is None:
            отк["круг_не_посчитан"] += 1
            continue
        факт = с.get("итог_po_cepi_sol")
        пары.append({"utc": с.get("utc"), "минт": м, "билет": б, "пул": пул,
                     "вх_слот": int(вх_слот), "вх_слот_состояния": вх[0],
                     "удержание": int(дер), "вых_слот_состояния": вых[0],
                     "тариф": вх[4], "издержки": round(изд, 8),
                     "модель_sol": round(кр["итог_sol"], 8),
                     "факт_sol": факт,
                     "сверено_по_цепи": bool(с.get("chain_ok")),
                     "разница_sol": (round(кр["итог_sol"] - факт, 8)
                                     if факт is not None else None)})

    def свод(v: list, поле: str) -> dict:
        z = [x[поле] for x in v if x.get(поле) is not None]
        if not z:
            return {"n": 0}
        return {"n": len(z), "сумма": round(sum(z), 6),
                "среднее": round(statistics.fmean(z), 6),
                "медиана": round(statistics.median(z), 6),
                "в_плюсе": sum(1 for y in z if y > 0),
                "мин": round(min(z), 6), "макс": round(max(z), 6)}

    сверенные = [x for x in пары if x["сверено_по_цепи"] and x["факт_sol"] is not None]
    тело = {"что": "vol_4vw: живые сделки против модели на тех же сигналах",
            "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "окно_архива": {"с": а.s, "часов": а.chasov},
            "цели": {"файл": а.celi, "сделок": len(сделки),
                     "окно_сделок": цели.get("окно_сделок")},
            "счёт_потока": {k: v for k, v in счёт.items() if not k.startswith("час_")},
            "часы_прочитаны": sorted(k[4:] for k in счёт if k.startswith("час_")),
            "отказы": dict(отк),
            "сошлось_пар": len(пары), "сверено_по_цепи": len(сверенные),
            "модель": свод(пары, "модель_sol"),
            "факт": свод(пары, "факт_sol"),
            "модель_на_сверенных": свод(сверенные, "модель_sol"),
            "факт_на_сверенных": свод(сверенные, "факт_sol"),
            "разница_модель_минус_факт": свод(сверенные, "разница_sol"),
            "примечание": "налог на перевод и комиссию пула живая выгрузка по этим "
                          "сделкам не заполнила (поля nalog_tokena_bps и "
                          "komissiya_pula_pct пустые у всех 55) -- в модель они не "
                          "подставлены; тариф взят с архивной строки пула",
            "пары": пары}
    ф = П / f"4vw_sverka{а.metka}.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        import podbivka_run as R  # noqa: PLC0415
        R.записано(ф)
    except Exception:  # noqa: BLE001, S110
        pass
    print(json.dumps({k: v for k, v in тело.items() if k != "пары"},
                     ensure_ascii=False, indent=1), flush=True)
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--celi", default=str(П / "4vw_zhivye_celi.json"))
    р.add_argument("--s", required=True, help="начало окна архива, YYYY-MM-DDTHH")
    р.add_argument("--chasov", type=int, default=12)
    р.add_argument("--metka", default="")
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
