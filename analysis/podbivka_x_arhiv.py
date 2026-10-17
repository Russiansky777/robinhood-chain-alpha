#!/usr/bin/env python3
"""Цены по архиву для проверки X: ряд состояний пула на каждый токен выборки.

Один скан архива -- и дальше всё считается офлайн: и вход по первому посту, и контрольный
случайный вход, и настоящий пик (не ограниченный окном суточной производной).

Что пишется на токен:
  * РЯД состояний в окне [создание, создание + --chasov-okna] с прореживанием до одного
    состояния в секунду (последнее в секунде): ts, блок, действие, резервы реальные и
    виртуальные. По нему любой момент входа и выхода считается потом, без нового чтения;
  * ПИК за всё прочитанное окно: максимум цены и его метка -- отдельно от ряда, чтобы не
    хранить все события крупных токенов;
  * тип пула и котировка: цена у кривой считается по виртуальным резервам, у обычных
    пулов -- по реальным (замер 12.10: по реальным цена кривой стремится к нулю, когда
    её распродают, и даёт −99 п.п. там, где −33).

Строки отбираются дешёвым поиском mint/poolId регуляркой; json.loads -- только для
попавших в заказ.

Выход: data/podbivka/x_arhiv<метка>.json.gz (ряды) и ..._svod.json (сводка).
"""
from __future__ import annotations

import argparse
import gzip
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
КРИВЫЕ = {"pump", "raydium-launchpad", "meteora-launchpad", "meteora-dbc", "dbc",
          "launchlab", "bonding"}
р_mint = re.compile(r'"mint":"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_pool = re.compile(r'"poolId":"([1-9A-HJ-NP-Za-km-z]{32,44})"')


def цена(е: dict, пул: str) -> float | None:
    """Цена по базе, какая верна для этого пула: кривая -- виртуальные резервы."""
    если_кривая = (пул or "") in КРИВЫЕ
    пары = ((е.get("vQuoteInBondingCurve"), е.get("vTokensInBondingCurve")),
            (е.get("quoteInPool"), е.get("tokensInPool")))
    if not если_кривая:
        пары = пары[::-1]
    for q, t in пары:
        try:
            q, t = float(q), float(t)
        except (TypeError, ValueError):
            continue
        if q > 0 and t > 0:
            return q / t
    return None


def главное(а) -> int:
    import requests  # noqa: PLC0415
    import zstandard  # noqa: PLC0415

    выб = json.loads(Path(а.vyborka).read_text(encoding="utf-8"))
    ряды_выб = выб["ряды"]
    по_минту = {р["минт"]: р for р in ряды_выб}
    по_пулу = {р["poolId"]: р["минт"] for р in ряды_выб if р.get("poolId")}
    минты = set(по_минту)
    пулы = set(по_пулу)
    окно_мс = int(а.chasov_okna * 3600_000)

    состояние: dict = {м: {"минт": м, "пул": None, "котировка": None,
                           "создание_ts": по_минту[м].get("ts"),
                           "событий": 0, "ряд": {},
                           "пик": None, "пик_ts": None,
                           "первое_ts": None, "последнее_ts": None}
                       for м in минты}

    т0 = time.mktime(time.strptime(а.s, "%Y-%m-%dT%H"))
    часы = [time.strftime("%Y/%m/%d/%H", time.gmtime(т0 + 3600 * k))
            for k in range(а.chasov)]
    счёт = {"строк": 0, "разобрано": 0, "часов": 0, "ошибки": []}
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
                    ключ = None
                    м = р_mint.search(стр)
                    if м and м.group(1) in минты:
                        ключ = м.group(1)
                    else:
                        p = р_pool.search(стр)
                        if p and p.group(1) in пулы:
                            ключ = по_пулу[p.group(1)]
                    if ключ is None:
                        continue
                    счёт["разобрано"] += 1
                    try:
                        е = json.loads(стр)
                    except ValueError:
                        continue
                    ts = е.get("timestamp")
                    if not isinstance(ts, (int, float)):
                        continue
                    ts = int(ts)
                    с = состояние[ключ]
                    с["событий"] += 1
                    с["пул"] = с["пул"] or е.get("pool")
                    с["котировка"] = с["котировка"] or е.get("quoteMint")
                    с["первое_ts"] = min(с["первое_ts"] or ts, ts)
                    с["последнее_ts"] = max(с["последнее_ts"] or ts, ts)
                    ц = цена(е, с["пул"])
                    if ц is None:
                        continue
                    if с["пик"] is None or ц > с["пик"]:
                        с["пик"], с["пик_ts"] = ц, ts
                    созд = с["создание_ts"] or ts
                    if ts < созд or ts > созд + окно_мс:
                        continue
                    # прореживание до одного состояния в секунду: последнее в секунде
                    с["ряд"][ts // 1000] = [
                        ts, е.get("block"), е.get("action"),
                        е.get("quoteInPool"), е.get("tokensInPool"),
                        е.get("vQuoteInBondingCurve"), е.get("vTokensInBondingCurve")]
        except Exception as e:  # noqa: BLE001
            счёт["ошибки"].append(f"{ч}: {type(e).__name__}: {str(e)[:120]}")
        print(f"  {ч}: строк {счёт['строк']}, разобрано {счёт['разобрано']}, "
              f"минут {round((time.time() - нач) / 60, 1)}", flush=True)

    ф = П / f"x_arhiv{а.metka}.json.gz"
    с_рядом = 0
    with gzip.open(ф, "wt", encoding="utf-8") as о:
        for м, с in состояние.items():
            ряд = [с["ряд"][k] for k in sorted(с["ряд"])]
            if ряд:
                с_рядом += 1
            о.write(json.dumps({
                "минт": м, "пул": с["пул"], "котировка": с["котировка"],
                "создание_ts": с["создание_ts"], "событий": с["событий"],
                "пик_цена": с["пик"], "пик_ts": с["пик_ts"],
                "первое_событие_ts": с["первое_ts"],
                "последнее_событие_ts": с["последнее_ts"],
                "окно_часов": а.chasov_okna, "ряд": ряд,
            }, ensure_ascii=False) + "\n")
    свод = {"что": "ряды состояний пулов выборки X и пик за прочитанное окно",
            "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "выборка": Path(а.vyborka).name, "окно_часов_ряда": а.chasov_okna,
            "часы": [часы[0], часы[-1]],
            "часов": len(часы), "счёт": счёт,
            "токенов": len(состояние), "с_рядом": с_рядом,
            "файл_рядов": ф.name}
    фс = П / f"x_arhiv{а.metka}_svod.json"
    фс.write_text(json.dumps(свод, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"токенов {len(состояние)}, с рядом {с_рядом}; {ф} и {фс}", flush=True)
    try:
        import podbivka_run as R  # noqa: PLC0415
        R.записано(ф)
        R.записано(фс)
    except Exception:  # noqa: BLE001, S110
        pass
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--s", required=True, help="первый час окна, YYYY-MM-DDTHH (UTC)")
    р.add_argument("--chasov", type=int, default=26)
    р.add_argument("--chasov-okna", type=float, default=3.0,
                   help="окно ряда от создания, часов")
    р.add_argument("--vyborka", default=str(П / "x_vyborka_top300.json"))
    р.add_argument("--metka", default="")
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
