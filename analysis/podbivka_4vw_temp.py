#!/usr/bin/env python3
"""Темп vol_4vw: почему живых попыток в час больше, чем сигналов в модели.

Зачем. Живая полоса с 09.10 16:29Z делает около 11 попыток в час по группе vol_4vw,
а модель на том же источнике (4vw54BmA, обычная посадка, билет 0.3, допуск 40 %,
удержание 36 слотов) даёт около 2.7 сигнала в час (65.5 сделок в сутки в проверочной
половине, data/podbivka/volna2.json). Разница вчетверо -- это не расхождение итога, а
разные счётчики: «попытка» живой полосы и «сигнал» модели стоят в разных местах
воронки. Этот проход считает воронку по архиву на ОДНОМ окне и показывает, сколько
покупок источника теряется на каждом сите.

Сита берутся из живой политики группы (data/sources_live.json ветки Code-1, группа
vol_4vw): порог источника 2.0 SOL-экв, не больше 3 слотов от источника, допуск 0.40,
удержание 36 слотов, билет 0.3; площадки -- одиннадцать (amm_v4, bonding, clmm, cpmm,
damm2, dbc, dlmm, launchlab, pump_amm, two_step, whirlpool). Сита модели -- свои:
площадка должна быть из ДЛЯ_МОДЕЛИ подбивки, котировка SOL, покупка первая по минту за
1800 слотов, и в архиве должны быть состояния пула на посадке и на выходе.

Названия площадок печатаются СЫРЫМИ, как их даёт архив: сопоставление с именами живой
политики -- отдельный вопрос, и выдумывать его здесь нечего.

Только чтение архива. Выход: data/podbivka/4vw_temp.json.
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
ГЕРОЙ = "4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9"
WSOL = "So11111111111111111111111111111111111111112"
ПОРОГ_ИСТОЧНИКА = 2.0      # min_target_sol живой политики
СЛОТОВ_ОТ_ИСТОЧНИКА = 3    # max_slots_from_source
ДОПУСК = 0.40              # slippage
УДЕРЖАНИЕ = 36             # hold_slots
БИЛЕТ = 0.3                # lane_sol
ОКНО_ДУБЛЯ = 1800          # «не первая покупка по окну 1800» -- сито модели
ИЗДЕРЖКИ = 0.0009          # чаевые+приоритет круга, как в подбивке
# площадки живой политики -- как есть, для печати рядом с сырыми именами архива
ПЛОЩАДКИ_ЖИВЫЕ = ("amm_v4", "bonding", "clmm", "cpmm", "damm2", "dbc", "dlmm",
                  "launchlab", "pump_amm", "two_step", "whirlpool")

р_block = re.compile(r'"block":\s*(\d+)')
р_action = re.compile(r'"action":\s*"([a-zA-Z_]+)"')
р_pool = re.compile(r'"poolId":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_tip = re.compile(r'"pool":\s*"([a-z0-9\-]+)"')
р_signer = re.compile(r'"txSigner":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_trader = re.compile(r'"trader":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_mint = re.compile(r'"mint":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_qmint = re.compile(r'"quoteMint":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_qam = re.compile(r'"quoteAmount":\s*"?([0-9.eE+-]+)')
р_fee = re.compile(r'"poolFeeRate":\s*"?([0-9.eE+-]+)')
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


def число(м):
    if not м:
        return None
    try:
        return float(м.group(1))
    except ValueError:
        return None


def главное(а) -> int:  # noqa: PLR0912, PLR0915
    import io            # noqa: PLC0415
    import requests      # noqa: PLC0415
    import zstandard     # noqa: PLC0415

    счёт: collections.Counter = collections.Counter()
    площадки: collections.Counter = collections.Counter()
    отказ: collections.Counter = collections.Counter()
    сигналы: list = []           # покупки героя, прошедшие сита модели
    ждут: dict = {}              # poolId -> [сигнал, ...] ждут посадки и выхода
    видел_минт: dict = {}        # mint -> блок последней учтённой покупки
    последний_блок = None

    def читать_час(поток) -> None:
        nonlocal последний_блок
        for стр in io.TextIOWrapper(
                zstandard.ZstdDecompressor().stream_reader(поток.raw),
                encoding="utf-8", errors="ignore"):
            счёт["строк"] += 1
            бм = р_block.search(стр)
            if not бм:
                continue
            блок = int(бм.group(1))
            последний_блок = блок
            пм = р_pool.search(стр)
            pid = пм.group(1) if пм else None
            сост = состояние(стр)
            # 1) добираем посадку и выход для уже найденных сигналов
            if pid in ждут and сост:
                живые = []
                for с in ждут[pid]:
                    if с["посадка"] is None and блок <= с["блок"] + СЛОТОВ_ОТ_ИСТОЧНИКА:
                        с["посадка"] = [сост[0], сост[1], блок]
                    if блок >= с["блок"] + УДЕРЖАНИЕ and с["выход"] is None:
                        с["выход"] = [сост[0], сост[1], блок]
                    if с["выход"] is None:
                        живые.append(с)
                if живые:
                    ждут[pid] = живые
                else:
                    del ждут[pid]
            # 2) ищем покупки героя
            ам = р_action.search(стр)
            if not ам or ам.group(1) != "buy":
                continue
            тм = р_trader.search(стр)
            sм = р_signer.search(стр)
            кто = (тм.group(1) if тм else None) or (sм.group(1) if sм else None)
            if кто != ГЕРОЙ:
                continue
            счёт["покупок_героя"] += 1
            тип = р_tip.search(стр)
            тип = тип.group(1) if тип else None
            площадки[тип or "<нет>"] += 1
            qmм = р_qmint.search(стр)
            qм = р_qam.search(стр)
            квота = qmм.group(1) if qmм else None
            sol = число(qм) if квота == WSOL else None
            мм = р_mint.search(стр)
            минт = мм.group(1) if мм else None
            # --- сита живой политики ------------------------------------------
            if квота != WSOL:
                отказ["живое: котировка не SOL"] += 1
            elif sol is None or sol < ПОРОГ_ИСТОЧНИКА:
                отказ["живое: меньше порога источника 2.0 SOL"] += 1
            else:
                счёт["живое_прошло_порог"] += 1
            # --- сита модели ---------------------------------------------------
            ок_тип = тип in B.ДЛЯ_МОДЕЛИ or тип == "pump-amm"
            if квота != WSOL:
                отказ["модель: котировка не SOL"] += 1
                continue
            if sol is None or sol < ПОРОГ_ИСТОЧНИКА:
                отказ["модель: меньше порога источника 2.0 SOL"] += 1
                continue
            if not ок_тип:
                отказ[f"модель: площадка вне модели ({тип})"] += 1
                continue
            if минт and видел_минт.get(минт) is not None and \
                    блок - видел_минт[минт] < ОКНО_ДУБЛЯ:
                отказ["модель: не первая покупка по минту за 1800 слотов"] += 1
                continue
            if минт:
                видел_минт[минт] = блок
            if not сост:
                отказ["модель: нет состояния пула на строке покупки"] += 1
                continue
            счёт["сигналов_модели"] += 1
            с = {"блок": блок, "минт": минт, "пул": тип, "poolId": pid,
                 "sol_источника": sol, "тариф": число(р_fee.search(стр)),
                 "состояние": [сост[0], сост[1]], "посадка": None, "выход": None}
            сигналы.append(с)
            ждут.setdefault(pid, []).append(с)

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
                    break
                try:
                    читать_час(поток)
                except Exception as exc:  # noqa: BLE001
                    счёт[f"обрыв_{type(exc).__name__}"] += 1
                    if попытка == 2:
                        счёт["обрыв_часов"] += 1
                    continue
            счёт["часов"] += 1
            break
        print(f"  {ч}: строк {счёт['строк']}, покупок героя {счёт['покупок_героя']}, "
              f"сигналов {счёт['сигналов_модели']}", flush=True)

    # ---- круг по модели на тех же сигналах --------------------------------------
    итоги: list = []
    for с in сигналы:
        if not с["посадка"] or not с["выход"]:
            счёт["сигнал_без_посадки_или_выхода"] += 1
            continue
        if с["пул"] in B.XYK:
            # у XYK-пулов формуле нужны f и g -- они подгоняются по хранилищам окна,
            # одним потоком их не взять. Такие сигналы в счёт входят, в круг -- нет.
            счёт["круг_без_f_g_(XYK)"] += 1
            continue
        знак = {"pool": с["пул"],
                "модель": {"fee": с["тариф"] or 0.0,
                           "состояния": {"масштаб": 1.0,
                                         "вход": {"S0_дно": с["посадка"][:2]},
                                         "выход": {str(УДЕРЖАНИЕ): с["выход"][:2]}}}}
        кр = B.сделка_по_состояниям(знак, БИЛЕТ, "S0_дно", УДЕРЖАНИЕ,
                                    издержки=ИЗДЕРЖКИ, к_f=1.0, к_g=1.0, налог_bps=0)
        if кр is None:
            счёт["круг_не_посчитан"] += 1
            continue
        итоги.append({"блок": с["блок"], "минт": с["минт"], "пул": с["пул"],
                      "sol_источника": с["sol_источника"],
                      "итог_sol": кр["итог_sol"], "пп": кр.get("пп"),
                      "посадка_слотов": с["посадка"][2] - с["блок"],
                      "выход_слотов": с["выход"][2] - с["блок"]})
    v = [x["итог_sol"] for x in итоги]
    часов = max(1, счёт["часов"])
    свод = {"сделок": len(итоги), "часов": часов,
            "сделок_в_час": round(len(итоги) / часов, 2),
            "покупок_героя_в_час": round(счёт["покупок_героя"] / часов, 2),
            "живое_прошло_порог_в_час": round(счёт["живое_прошло_порог"] / часов, 2),
            "сигналов_модели_в_час": round(счёт["сигналов_модели"] / часов, 2)}
    if v:
        свод.update({"итог_sol": round(sum(v), 6),
                     "среднее_на_сделку_sol": round(statistics.fmean(v), 6),
                     "медиана_sol": round(statistics.median(v), 6),
                     "в_плюсе_проц": round(100 * sum(1 for x in v if x > 0) / len(v), 1),
                     "худшая_sol": round(min(v), 6), "лучшая_sol": round(max(v), 6)})
    тело = {"что": "темп vol_4vw: воронка от покупок источника до сделок модели",
            "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "окно": {"с": а.s, "часов": а.chasov},
            "источник": ГЕРОЙ,
            "политика_живая": {"порог_источника_sol": ПОРОГ_ИСТОЧНИКА,
                               "слотов_от_источника": СЛОТОВ_ОТ_ИСТОЧНИКА,
                               "допуск": ДОПУСК, "удержание_слотов": УДЕРЖАНИЕ,
                               "билет": БИЛЕТ, "площадки": list(ПЛОЩАДКИ_ЖИВЫЕ)},
            "площадки_модели": sorted(B.ДЛЯ_МОДЕЛИ) + ["pump-amm"],
            "счёт": dict(счёт), "площадки_архива_сырые": dict(площадки.most_common()),
            "отказы": dict(отказ.most_common()), "свод": свод,
            "сделки": итоги[:400]}
    ф = П / f"4vw_temp{а.metka}.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        import podbivka_run as R  # noqa: PLC0415
        R.записано(ф)
    except Exception:  # noqa: BLE001, S110
        pass
    print(json.dumps({"счёт": тело["счёт"], "площадки_архива_сырые": тело["площадки_архива_сырые"],
                      "отказы": тело["отказы"], "свод": свод}, ensure_ascii=False, indent=1),
          flush=True)
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--s", required=True, help="начало окна, YYYY-MM-DDTHH")
    р.add_argument("--chasov", type=int, default=1)
    р.add_argument("--metka", default="")
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
