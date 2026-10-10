#!/usr/bin/env python3
"""Почерк 4vw54BmA: кто начал покупать так же. Поиск преемника по архиву.

Почерк задан владельцем: покупка примерно 1--5 SOL в СЕРЕДИНЕ кривой (резерв пула
14--52 SOL), и за ней толпа -- не меньше восьми свопов в том же пуле за два следующих
слота. Проход читает окно архива, собирает все покупки, подходящие под размер и резерв, и
по каждой считает толпу в слотах +1 и +2. Дальше складывает по кошелькам: сколько у кого
таких покупок, какие минты и площадки, какие размеры и резервы.

Сравнение с самим 4vw идёт тем же счётом: он в этом же окне считается наравне со всеми, и
по каждому кандидату печатается, сколько у него общего с ним -- минты, площадки, размер.
Адрес пополнения кандидата архив не знает; он берётся отдельным проходом по цепи
(podbivka_preemnik.py), и список кандидатов для него печатается файлом.

Только чтение архива. Выход: data/podbivka/pocherk.json и pocherk_celi.json.
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

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
ГЕРОЙ = "4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9"
WSOL = "So11111111111111111111111111111111111111112"
SOL_МИН, SOL_МАКС = 1.0, 5.0        # размер покупки почерка
РЕЗ_МИН, РЕЗ_МАКС = 14.0, 52.0      # середина кривой: резерв пула, SOL
ТОЛПА_МИН = 8                        # свопов за два следующих слота
ТОЛПА_СЛОТОВ = 2
ВЕРХ = 60                            # сколько кандидатов печатать

р_block = re.compile(r'"block":\s*(\d+)')
р_action = re.compile(r'"action":\s*"([a-zA-Z_]+)"')
р_pool = re.compile(r'"poolId":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_tip = re.compile(r'"pool":\s*"([a-z0-9\-]+)"')
р_signer = re.compile(r'"txSigner":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_trader = re.compile(r'"trader":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_mint = re.compile(r'"mint":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_qmint = re.compile(r'"quoteMint":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_qam = re.compile(r'"quoteAmount":\s*"?([0-9.eE+-]+)')
р_q = re.compile(r'"(quoteInPool|tokensInPool|vQuoteInBondingCurve|vTokensInBondingCurve)":'
                 r'\s*"?([0-9.eE+-]+)')


def часы(с: str, сколько: int) -> list:
    т = time.strptime(с, "%Y-%m-%dT%H")
    н = int(time.mktime(т)) - time.timezone
    return [time.strftime("%Y/%m/%d/%H", time.gmtime(н + 3600 * k)) for k in range(сколько)]


def резерв(стр: str) -> float | None:
    поля = dict(р_q.findall(стр))
    x = поля.get("vQuoteInBondingCurve") or поля.get("quoteInPool")
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if x > 0 else None


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
    первый_блок_часа: dict = {}
    рубеж: list = [None]          # блок, с которого считается «начал»
    # ждут толпы: ключ (poolId, блок) -> список записей покупок
    ждут: dict = collections.defaultdict(list)
    толпа: collections.Counter = collections.Counter()   # (poolId, блок) -> свопов
    готовые: list = []
    последний_блок = 0

    def закрыть(до_блока: int) -> None:
        """Покупки, у которых окно толпы уже прошло, уходят в готовые."""
        for ключ in [k for k in ждут if k[1] + ТОЛПА_СЛОТОВ < до_блока]:
            с = толпа.pop(ключ, 0)
            for з in ждут.pop(ключ):
                з["толпа"] = с
                if с >= ТОЛПА_МИН:
                    готовые.append(з)
                    счёт["почерк_совпал"] += 1
                else:
                    счёт["толпы_не_было"] += 1

    def читать_час(поток, ч: str) -> None:
        nonlocal последний_блок
        for стр in io.TextIOWrapper(
                zstandard.ZstdDecompressor().stream_reader(поток.raw),
                encoding="utf-8", errors="ignore"):
            счёт["строк"] += 1
            бм = р_block.search(стр)
            if not бм:
                continue
            блок = int(бм.group(1))
            if ч not in первый_блок_часа:
                первый_блок_часа[ч] = блок
                if ч == а.rubezh_chas:
                    рубеж[0] = блок
            if блок > последний_блок:
                последний_блок = блок
                if счёт["строк"] % 20000 == 0:
                    закрыть(блок)
            пм = р_pool.search(стр)
            if not пм:
                continue
            pid = пм.group(1)
            ам = р_action.search(стр)
            д = ам.group(1) if ам else None
            if д in ("buy", "sell"):
                # своп считается в толпу тех покупок, чьё окно он покрывает
                for сдвиг in range(1, ТОЛПА_СЛОТОВ + 1):
                    к = (pid, блок - сдвиг)
                    if к in ждут:
                        толпа[к] += 1
            if д != "buy":
                continue
            qmм = р_qmint.search(стр)
            if (qmм.group(1) if qmм else None) != WSOL:
                счёт["покупка_не_SOL"] += 1
                continue
            sol = число(р_qam.search(стр))
            if sol is None or not (SOL_МИН <= sol <= SOL_МАКС):
                continue
            x = резерв(стр)
            if x is None or not (РЕЗ_МИН <= x <= РЕЗ_МАКС):
                continue
            тм = р_trader.search(стр)
            sм = р_signer.search(стр)
            кто = (тм.group(1) if тм else None) or (sм.group(1) if sм else None)
            if not кто:
                счёт["покупка_без_адреса"] += 1
                continue
            мм = р_mint.search(стр)
            тип = р_tip.search(стр)
            счёт["покупка_по_размеру_и_резерву"] += 1
            ждут[(pid, блок)].append(
                {"кто": кто, "минт": мм.group(1) if мм else None,
                 "пул": тип.group(1) if тип else None, "poolId": pid,
                 "блок": блок, "sol": sol, "резерв": x})

    часы_прочитаны = []
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
                    счёт["часов_нет_в_архиве"] += 1
                    break
                try:
                    читать_час(поток, ч)
                except Exception as exc:  # noqa: BLE001
                    счёт[f"обрыв_{type(exc).__name__}"] += 1
                    if попытка == 2:
                        счёт["обрыв_часов"] += 1
                    continue
            счёт["часов"] += 1
            часы_прочитаны.append(ч)
            break
        print(f"  {ч}: строк {счёт['строк']}, почерк совпал {счёт['почерк_совпал']}",
              flush=True)
    закрыть(последний_блок + ТОЛПА_СЛОТОВ + 1)

    # ---- складываем по кошелькам ------------------------------------------------
    по_кош: dict = collections.defaultdict(
        lambda: {"покупок": 0, "минты": set(), "пулы": collections.Counter(),
                 "sol": [], "резервы": [], "толпа": [], "блоки": [],
                 "до_рубежа": 0, "после_рубежа": 0})
    for з in готовые:
        к = по_кош[з["кто"]]
        к["покупок"] += 1
        if з["минт"]:
            к["минты"].add(з["минт"])
        к["пулы"][з["пул"] or "<нет>"] += 1
        к["sol"].append(з["sol"])
        к["резервы"].append(з["резерв"])
        к["толпа"].append(з["толпа"])
        к["блоки"].append(з["блок"])
        if рубеж[0] is not None:
            к["после_рубежа" if з["блок"] >= рубеж[0] else "до_рубежа"] += 1

    герой = по_кош.get(ГЕРОЙ)
    минты_героя = set(герой["минты"]) if герой else set()
    пулы_героя = set(герой["пулы"]) if герой else set()

    def карточка(адр: str, к: dict) -> dict:
        s = sorted(к["sol"]); r = sorted(к["резервы"])
        общие = sorted(минты_героя & set(к["минты"]))
        return {"адрес": адр, "покупок": к["покупок"], "минтов": len(к["минты"]),
                "площадки": dict(к["пулы"].most_common(6)),
                "sol_медиана": round(statistics.median(s), 4),
                "sol_p10": round(s[len(s) // 10], 4), "sol_p90": round(s[9 * len(s) // 10], 4),
                "резерв_медиана": round(statistics.median(r), 2),
                "толпа_медиана": statistics.median(к["толпа"]),
                "первый_блок": min(к["блоки"]), "последний_блок": max(к["блоки"]),
                "почерка_до_рубежа": к["до_рубежа"],
                "почерка_после_рубежа": к["после_рубежа"],
                # «начал с рубежа» -- весь почерк кошелька лежит после него. Без этого
                # в кандидаты попадут снайперы, которые так покупали и раньше.
                "начал_с_рубежа": bool(рубеж[0] is not None and к["до_рубежа"] == 0
                                       and к["после_рубежа"] > 0),
                "общих_минтов_с_4vw": len(общие),
                "общие_минты_с_4vw": общие[:10],
                "общих_площадок_с_4vw": len(пулы_героя & set(к["пулы"])),
                "это_4vw": адр == ГЕРОЙ}

    карточки = sorted((карточка(а_, к) for а_, к in по_кош.items()),
                      key=lambda x: -x["покупок"])
    новые = [x for x in карточки if x["начал_с_рубежа"] and not x["это_4vw"]]
    тело = {"что": "почерк 4vw54BmA по архиву: покупка 1--5 SOL при резерве 14--52 SOL "
                   "и толпа не меньше 8 свопов за два слота",
            "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "окно": {"с": а.s, "часов": а.chasov},
            "часы_прочитаны": часы_прочитаны,
            "рубеж": {"час": а.rubezh_chas, "блок": рубеж[0],
                      "первый_блок_часа": первый_блок_часа},
            "почерк": {"sol": [SOL_МИН, SOL_МАКС], "резерв": [РЕЗ_МИН, РЕЗ_МАКС],
                       "толпа_мин": ТОЛПА_МИН, "толпа_слотов": ТОЛПА_СЛОТОВ},
            "счёт": dict(счёт),
            "кошельков_с_почерком": len(карточки),
            "начавших_с_рубежа": len(новые),
            "начавшие_с_рубежа": новые[:ВЕРХ],
            "сам_4vw": карточка(ГЕРОЙ, герой) if герой else None,
            "кандидаты": карточки[:ВЕРХ]}
    ф = П / f"pocherk{а.metka}.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    цели = П / f"pocherk_celi{а.metka}.json"
    цели.write_text(json.dumps(
        {"что": "кандидаты в преемники 4vw по почерку -- на проход по цепи за адресом "
                "пополнения и первой транзакцией",
         "окно": тело["окно"], "рубеж": тело["рубеж"]["час"],
         "адреса": [x["адрес"] for x in новые[:ВЕРХ]]
                   or [x["адрес"] for x in карточки[:ВЕРХ] if not x["это_4vw"]]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        import podbivka_run as R  # noqa: PLC0415
        R.записано(ф)
        R.записано(цели)
    except Exception:  # noqa: BLE001, S110
        pass
    print(json.dumps({k: v for k, v in тело.items() if k != "кандидаты"},
                     ensure_ascii=False, indent=1), flush=True)
    print(f"кандидатов {len(карточки)}, начавших с рубежа {len(новые)}; верхние "
          f"начавшие:", flush=True)
    for x in (новые or карточки)[:12]:
        print(f"  {x['адрес'][:12]} покупок {x['покупок']:>4} минтов {x['минтов']:>4} "
              f"sol медиана {x['sol_медиана']:>6} резерв {x['резерв_медиана']:>6} "
              f"толпа {x['толпа_медиана']:>4} до/после рубежа "
              f"{x['почерка_до_рубежа']}/{x['почерка_после_рубежа']} "
              f"общих минтов с 4vw {x['общих_минтов_с_4vw']}"
              f"{'  <- это 4vw' if x['это_4vw'] else ''}", flush=True)
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--s", required=True, help="начало окна архива, YYYY-MM-DDTHH")
    р.add_argument("--chasov", type=int, default=4)
    р.add_argument("--rubezh-chas", default="",
                   help="час архива вида 2026/10/10/03: его первый блок и есть рубеж "
                        "«начал». Кошелёк считается начавшим, только если ВЕСЬ его "
                        "почерк лежит после рубежа -- иначе в кандидаты попадут "
                        "снайперы, покупавшие так и раньше")
    р.add_argument("--metka", default="")
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
