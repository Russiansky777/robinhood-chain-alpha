#!/usr/bin/env python3
"""Запуски мемкоинов: сбор первых секунд жизни токена из архива PumpApi (задача владельца 08.10, В).

Зачем отдельный скрипт, а не правка `podbivka_arhiv_den.py`: тот строит сигнал вокруг сделки
ИСТОЧНИКА и ночью собирает данные по расписанию -- ломать его нельзя. Здесь свой проход по тем
же часовым файлам, но пул берётся с его ПЕРВОГО события в окне, и пишутся первые --slotov
слотов его жизни.

Что пишется на каждый запуск:
  * минт, пул, тип пула, блок и время создания, создатель (txSigner первого события);
  * в блоке создания: сколько покупок, какими кошельками, на сколько SOL, покупал ли создатель;
  * состояния пула для НАШЕГО входа: конец блока создания, блок +1, блок +2 (как S0_дно/S1/S2
    у сделки источника) -- по ним калиброванная модель считает любой билет офлайн;
  * выходы: состояние пула на +20/+40/+60/+90/+120/+160/+230 слотах;
  * волна: разные кошельки-покупатели и объём их покупок за 5/15/30/60 с от создания (по
    времени события, не по слотам), плюс пик цены и слот пика, первая продажа.

Словарь действий потока (`action`) считается всегда: по нему видно, есть ли в архиве событие
создания или пул опознаётся только по первому событию. Это ответ на вопрос «сколько запусков
в сутки по цепи против того, что видит проход».

Только чтение. Выход: data/podbivka/zapuski/<метка>.json.gz и сводка в data/podbivka/zapuski_svod.json.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import io
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
WSOL = "So11111111111111111111111111111111111111112"
ВЫХОДЫ = (20, 40, 60, 90, 120, 160, 230)
ОКНА_С = (5, 15, 30, 60)
КРИВЫЕ = {"pump", "raydium-launchpad"}

р_sig = re.compile(r'"signature":\s*"([1-9A-HJ-NP-Za-km-z]{64,90})"')
р_pool = re.compile(r'"poolId":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_tip = re.compile(r'"pool":\s*"([a-z0-9\-]+)"')
р_signer = re.compile(r'"txSigner":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_trader = re.compile(r'"trader":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_block = re.compile(r'"block":\s*(\d+)')
р_ts = re.compile(r'"timestamp":\s*(\d+)')
р_mint = re.compile(r'"mint":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_qmint = re.compile(r'"quoteMint":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_action = re.compile(r'"action":\s*"([a-zA-Z_]+)"')
р_qam = re.compile(r'"quoteAmount":\s*"?([0-9.eE+-]+)')
р_q = re.compile(r'"(quoteInPool|tokensInPool|vQuoteInBondingCurve|vTokensInBondingCurve)":\s*"?([0-9.eE+-]+)')


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


def запись(з: dict) -> dict:
    """Готовый ряд запуска: состояния входа и выхода, волна, пик."""
    ряд = з["ряд"]
    б0 = з["блок"]
    вход: dict = {}
    for имя, сдвиг in (("створ_дно", 0), ("створ+1", 1), ("створ+2", 2)):
        посл = [e for e in ряд if e["блок"] == б0 + сдвиг and e["сост"]]
        if посл:
            вход[имя] = [посл[-1]["сост"][0], посл[-1]["сост"][1], посл[-1]["блок"]]
    выход: dict = {}
    for H in ВЫХОДЫ:
        до = [e for e in ряд if e["блок"] <= б0 + H - 1 and e["сост"]]
        if до:
            выход[str(H)] = [до[-1]["сост"][0], до[-1]["сост"][1], до[-1]["блок"]]
    волна: dict = {}
    for т in ОКНА_С:
        до_ts = з["ts"] + т * 1000
        пок = [e for e in ряд if e["ts"] <= до_ts and e["действие"] == "buy"]
        волна[f"{т}с"] = {"покупок": len(пок),
                          "кошельков": len({e["кто"] for e in пок if e["кто"]}),
                          "sol": round(sum(e["sol"] or 0 for e in пок), 4)}
    цены = [(e["блок"], e["сост"][0] / e["сост"][1]) for e in ряд if e["сост"]]
    пик = max(цены, key=lambda x: x[1]) if цены else None
    п0 = цены[0][1] if цены else None
    в_блоке = [e for e in ряд if e["блок"] == б0 and e["действие"] == "buy"]
    продажи = [e for e in ряд if e["действие"] == "sell"]
    return {
        "минт": з["минт"], "poolId": з["poolId"], "пул": з["пул"], "блок": б0, "ts": з["ts"],
        "создатель": з["создатель"], "правило": з["правило"], "quoteMint": з["quoteMint"],
        "событий": len(ряд), "вход": вход, "выход": выход, "волна": волна,
        "в_блоке_создания": {"покупок": len(в_блоке),
                             "кошельков": len({e["кто"] for e in в_блоке if e["кто"]}),
                             "sol": round(sum(e["sol"] or 0 for e in в_блоке), 4),
                             "создатель_купил": any(e["кто"] == з["создатель"] for e in в_блоке)},
        "пик": {"слотов": пик[0] - б0, "рост_пп": round(100 * (пик[1] / п0 - 1), 2)}
        if пик and п0 else None,
        "первая_продажа_слотов": (продажи[0]["блок"] - б0) if продажи else None,
        "продаж": len(продажи)}


def main() -> int:  # noqa: PLR0912, PLR0915
    import subprocess  # noqa: PLC0415
    import requests  # noqa: PLC0415
    # в venv облачного прогона стоят только requests и solders; zstandard проход
    # доустанавливает себе сам (podbivka_arhiv_den, строка 435) -- делаем так же
    try:
        import zstandard  # noqa: PLC0415
    except ModuleNotFoundError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "zstandard"], check=True)
        import zstandard  # noqa: PLC0415
    import podbivka_arhiv_den as AD  # noqa: PLC0415
    import podbivka_run as R  # noqa: PLC0415

    р_ = argparse.ArgumentParser()
    р_.add_argument("--s", required=True, help="начало окна, YYYY-MM-DDTHH")
    р_.add_argument("--chasov", type=int, default=24)
    р_.add_argument("--slotov", type=int, default=max(ВЫХОДЫ))
    р_.add_argument("--metka", default="")
    р_.add_argument("--tolko-slovar", action="store_true",
                    help="только словарь действий потока, без сбора запусков")
    а = р_.parse_args()
    метка = а.metka or f"zap_{а.s}"

    словарь: collections.Counter = collections.Counter()
    счёт: collections.Counter = collections.Counter()
    видел_пул: set = set()
    молодые: dict = {}
    готовые: list = []
    первый_блок = None
    последний_блок = None

    for ч in часы(а.s, а.chasov):
        url = f"https://replay.pumpapi.io/{ч}.jsonl.zst"
        try:
            о = AD.открыть_час(requests, url, ч)
        except Exception as exc:  # noqa: BLE001
            счёт[f"ошибка_{type(exc).__name__}"] += 1
            continue
        with о as поток:
            if getattr(поток, "status_code", 200) != 200:
                счёт[f"http_{getattr(поток, 'status_code', '?')}"] += 1
                continue
            счёт["часов"] += 1
            for стр in io.TextIOWrapper(
                    zstandard.ZstdDecompressor().stream_reader(поток.raw),
                    encoding="utf-8", errors="ignore"):
                счёт["строк"] += 1
                ам = р_action.search(стр)
                д = ам.group(1) if ам else "<нет>"
                словарь[д] += 1
                бм = р_block.search(стр)
                if not бм:
                    continue
                блок = int(бм.group(1))
                if первый_блок is None:
                    первый_блок = блок
                последний_блок = блок
                if а.tolko_slovar:
                    continue
                пм = р_pool.search(стр)
                if not пм:
                    continue
                pid = пм.group(1)
                # закрываем молодые пулы, чьё окно кончилось
                if счёт["строк"] % 50000 == 0:
                    for k in [k for k, v in молодые.items() if блок > v["блок"] + а.slotov]:
                        готовые.append(запись(молодые.pop(k)))
                сост = состояние(стр)
                tsм = р_ts.search(стр)
                ts = int(tsм.group(1)) if tsм else 0
                кто = None
                тм = р_trader.search(стр)
                sм = р_signer.search(стр)
                кто = (тм.group(1) if тм else None) or (sм.group(1) if sм else None)
                sol = None
                qм = р_qam.search(стр)
                qmм = р_qmint.search(стр)
                if qм and qmм and qmм.group(1) == WSOL:
                    try:
                        sol = float(qм.group(1))
                    except ValueError:
                        sol = None
                e = {"блок": блок, "ts": ts, "действие": д, "кто": кто, "sol": sol, "сост": сост}
                if pid in молодые:
                    if блок <= молодые[pid]["блок"] + а.slotov:
                        молодые[pid]["ряд"].append(e)
                    else:
                        готовые.append(запись(молодые.pop(pid)))
                    continue
                if pid in видел_пул:
                    continue
                видел_пул.add(pid)
                # первый раз видим пул. Запуск -- если это событие создания, иначе (если в потоке
                # создания нет) -- первое событие пула-кривой в окне, но не в первый час окна:
                # иначе под «запуск» попадут пулы, жившие до начала окна.
                правило = None
                if д not in ("buy", "sell"):
                    правило = f"создание:{д}"
                elif (первый_блок is not None and блок > первый_блок + 3 * а.slotov
                      and (р_tip.search(стр).group(1) if р_tip.search(стр) else "") in КРИВЫЕ):
                    правило = "первое_событие_кривой"
                if правило is None:
                    счёт["пул_не_запуск"] += 1
                    continue
                счёт[правило] += 1
                мм = р_mint.search(стр)
                тип = р_tip.search(стр)
                молодые[pid] = {"poolId": pid, "минт": мм.group(1) if мм else None,
                                "пул": тип.group(1) if тип else None, "блок": блок, "ts": ts,
                                "создатель": кто, "правило": правило,
                                "quoteMint": qmм.group(1) if qmм else None, "ряд": [e]}
    for k in list(молодые):
        готовые.append(запись(молодые.pop(k)))

    пап = П / "zapuski"
    пап.mkdir(parents=True, exist_ok=True)
    вых = пап / f"{метка}.json.gz"
    тело = {"что": "запуски мемкоинов: первые слоты жизни пула", "окно": {"с": а.s, "часов": а.chasov},
            "слотов": а.slotov, "словарь_действий": dict(словарь.most_common()),
            "счёт": dict(счёт), "блоки": {"первый": первый_блок, "последний": последний_блок},
            "запусков": len(готовые), "ряды": готовые}
    with gzip.open(вых, "wt", encoding="utf-8") as ф:
        json.dump(тело, ф, ensure_ascii=False)
    свод = П / "zapuski_svod.json"
    было = {}
    if свод.exists():
        try:
            было = json.loads(свод.read_text(encoding="utf-8"))
        except ValueError:
            было = {}
    было[метка] = {k: v for k, v in тело.items() if k != "ряды"}
    свод.write_text(json.dumps(было, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(вых)
    R.записано(свод)
    print(f"{вых.name}: строк {счёт.get('строк', 0)}, часов {счёт.get('часов', 0)}, "
          f"запусков {len(готовые)}, словарь {dict(словарь.most_common(6))}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
