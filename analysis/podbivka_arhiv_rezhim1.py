#!/usr/bin/env python3
"""Подбивка: режим 1 на архиве PumpApi за 7 суток (pyg3_*) -- офлайн.

Сигнал -- первая покупка наших адресов от 2 SOL за SOL в пуле pump / pump-amm /
raydium-cpmm / raydium-launchpad / meteora-damm-v1 (podbivka_arhiv_den.py; модель:
f и g -- медианы окна, кривая -- poolFeeRate; входы S0 сразу за источником,
S0_дно -- конец слота s0 (порядок внутри слота -- по timestamp, оценка), S1;
выходы +6/+12/+24/+36/+72/+150; билеты 0.3 / 0.5; минус 0.002 SOL на круг).
Pump AMM -- оценка: архив не видит часть свопов через сторонние программы (25.09:
300 из 492 таких в 20 расхождениях).

Группы -- data/podbivka/arhiv_adresa.json. «Пропущенные» 133 -- сигналы 133,
которых нет среди наших покупок в data/podbivka/pumpapi/celi.json (для 25.09) и в
строках m4 (data/podbivka/postobr/m4_*.jsonl).
Выход: docs/podbivka_2026-09-28_arhiv_rezhim1.md.
"""
from __future__ import annotations

import glob
import gzip
import json
import statistics
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
ТИПЫ = {"pump": "кривая pump.fun", "raydium-launchpad": "LaunchLab", "pump-amm": "Pump AMM (оценка)",
        "raydium-cpmm": "CPMM", "meteora-damm-v1": "DAMM v1"}
ТИПЫ["pump-amm"] = "Pump AMM (не для вывода)"
СНАЙП_БЕЗ = ("JDFDma1T", "4S9Vbao1", "AFmiexHw", "9emXYGUF", "2CQgjcdN")


def ячейка(v: list) -> str:
    v = sorted(x for x in v if x is not None)
    if not v:
        return "—"
    k = -(-len(v) * 5 // 100)
    ус = v[:-k] if len(v) > k else v
    return (f"{len(v)} | {statistics.mean(v):+.1f} | {statistics.mean(ус):+.1f} | {statistics.median(v):+.1f} | "
            f"{100 * sum(1 for x in v if x > 0) / len(v):.0f}%")


def main() -> int:
    import argparse  # noqa: PLC0415
    р = argparse.ArgumentParser()
    р.add_argument("--prefiks", default="pyg3")
    р.add_argument("--vyhod", default="docs/podbivka_2026-09-28_arhiv_rezhim1.md")
    р.add_argument("--sravnimo", action="store_true",
                   help="только сигналы от 2 SOL с котировкой SOL (сравнение прогонов с разным правилом сигнала)")
    а = р.parse_args()
    файлы = sorted(glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "arhiv_den" / f"{а.prefiks}_*.json.gz")))
    сиг, видел, часы, ошибки = [], set(), 0, []
    for f in файлы:
        a = json.loads(gzip.decompress(Path(f).read_bytes()))
        часы += (a.get("счёт") or {}).get("файлов", 0)
        ошибки += [e.split(":")[0] for e in (a.get("счёт") or {}).get("ошибки") or []]
        for с in a.get("сигналы") or []:
            if с["signature"] in видел or (с.get("модель") or {}).get("why_not"):
                continue
            if а.sravnimo and ((с.get("sol") or 0) < 2 or с.get("quoteMint", "So11111111111111111111111111111111111111112")
                               != "So11111111111111111111111111111111111111112"):
                continue
            видел.add(с["signature"])
            сиг.append(с)
    адр = json.loads((КОРЕНЬ / "data" / "podbivka" / "arhiv_adresa.json").read_text(encoding="utf-8"))["адреса"]
    гр = lambda с: set((адр.get(с["trader"]) or {}).get("группы") or [])  # noqa: E731
    всего_с_доп = len(сиг)
    сиг = [с for с in сиг if гр(с)]          # только наши группы (без доп. кошельков разбора Pygoscelis)
    пп = lambda с, k: (с["модель"].get("пп") or {}).get(k)  # noqa: E731
    t0 = min(с["timestamp"] for с in сиг) / 1000 if сиг else 0
    t1 = max(с["timestamp"] for с in сиг) / 1000 if сиг else 0
    md = ["# Подбивка: режим 1 на архиве PumpApi -- 7 суток", "",
          f"Сигналов с моделью: {len(сиг)} ({time.strftime('%d.%m %H:%M', time.gmtime(t0))} – "
          f"{time.strftime('%d.%m %H:%M', time.gmtime(t1))} UTC); часовых файлов {часы}, недочитаны (обрыв связи): "
          + (", ".join(ошибки) if ошибки else "нет") + ". Ячейка: n | среднее | усечённое (без 5 % лучших) | медиана | в плюс; "
          "п.п. чистыми с 0.002 SOL на круг. Наши адреса -- группы Code-1, 543, 133, источники снайперов "
          f"(сигналов всех адресов прогона, включая доп. кошельки разбора Pygoscelis: {всего_с_доп}).", "",
          "**Pump AMM на архиве -- не для вывода**: модель архива берёт цену x/y без лишнего остатка токена в "
          "хранилище (E) и занижает результат (ночные сделки 27.09: модель архива против факта -- 4LmNfJiS −26.4 против "
          "+17.8, 2LZrZ458 −1.1 против +17.2; docs/podbivka_arhiv_den_2026-09-27T06.md, раздел C); к тому же архив видит "
          "не все свопы Pump AMM. Для Pump AMM -- режим 1 по цепи (hvosty) и режим 2 с g до 1.6.", ""]
    # 1. по типу пула: вход × выход, билет 0.5 и 0.3
    for билет in ("0.5", "0.3"):
        md += [f"## 1. По типу пула, билет {билет} SOL, все наши адреса", "",
               "| пул | вход | +6 | +12 | +36 | +72 | +150 |", "|---|---|---|---|---|---|---|"]
        for p, имя in ТИПЫ.items():
            сс = [с for с in сиг if с["pool"] == p]
            if not сс:
                continue
            for вход, ви in (("S0", "сразу за ним"), ("S0_дно", "конец слота"), ("S1", "S+1")):
                md.append(f"| {имя} | {ви} | " + " | ".join(ячейка([пп(с, f'{вход}|{билет}|{h}') for с in сс])
                                                               for h in (6, 12, 36, 72, 150)) + " |")
        md.append("")
    # 2. по группе
    md += ["## 2. По группе: кривая pump.fun и LaunchLab, сразу за ним, билет 0.5", "",
           "| группа | +6 | +12 | +36 | +72 | +150 |", "|---|---|---|---|---|---|"]
    for г in ("133", "543", "снайперские источники", "batch5", "lane_s0", "leader"):
        сс = [с for с in сиг if г in гр(с) and с["pool"] in ("pump", "raydium-launchpad")]
        md.append(f"| {г} | " + " | ".join(ячейка([пп(с, f'S0|0.5|{h}') for с in сс]) for h in (6, 12, 36, 72, 150)) + " |")
    md.append("")
    # 3. источники снайперов по кошелькам
    md += ["## 3. Источники снайперов по кошелькам (кривая, LaunchLab, CPMM -- без Pump AMM; сразу за ним, 0.5 SOL)", "",
           "| кошелёк | пулы | +6 | +36 | +150 | конец слота, +36 | без числа раньше |", "|---|---|---|---|---|---|---|"]
    сн = sorted({с["trader"] for с in сиг if "снайперские источники" in гр(с)})
    for w in сн:
        сс = [с for с in сиг if с["trader"] == w and с["pool"] != "pump-amm"]
        if not сс:
            continue
        пулы = ", ".join(f"{ТИПЫ.get(p, p).split(' (')[0]} {sum(1 for с in сс if с['pool'] == p)}" for p in ТИПЫ if any(с["pool"] == p for с in сс))
        md.append(f"| `{w}` | {пулы} | {ячейка([пп(с, 'S0|0.5|6') for с in сс])} | {ячейка([пп(с, 'S0|0.5|36') for с in сс])} | "
                  f"{ячейка([пп(с, 'S0|0.5|150') for с in сс])} | {ячейка([пп(с, 'S0_дно|0.5|36') for с in сс])} | "
                  f"{'да' if w.startswith(СНАЙП_БЕЗ) else ''} |")
    нет_сигн = [p for p in СНАЙП_БЕЗ if not any(w.startswith(p) for w in сн)]
    md += ["", "«Сразу за ним» на архиве -- состояние после события источника в порядке файла (timestamp мс внутри слота); "
               "индекса в блоке архив не даёт -- оценка, верхняя; «конец слота» -- после последнего события слота s0.",
               "", "Кривая на архиве против цепи (hvosty, режим 1): сразу за ним +72 -- среднее +9.4 / медиана +3.9 (архив, n 13956) "
               "против +6.8 / +1.9 (цепь, n 1905); конец слота +72 -- медиана −3.6 против −2.6.", ""]
    md += ["", "Источники снайперов «без числа» без сигналов в архиве за 7 суток (нет первых покупок от 2 SOL за SOL в "
               "поддержанных пулах): " + (", ".join(нет_сигн) if нет_сигн else "нет") + ".", ""]
    # 4. пропущенные 133
    наши_sig = set()
    ц = json.loads((КОРЕНЬ / "data" / "podbivka" / "pumpapi" / "celi.json").read_text(encoding="utf-8"))["цели"]
    наши_sig |= {s for s, x in ц.items() if x.get("группа") == "наши 133"}
    for fl in glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "postobr" / "m4_*.jsonl")):
        for l in open(fl, encoding="utf-8"):
            try:
                наши_sig.add(json.loads(l)["signature"])
            except (ValueError, KeyError):
                pass
    с133 = [с for с in сиг if "133" in гр(с)]
    d25 = lambda с: time.strftime("%Y-%m-%d", time.gmtime(с["timestamp"] / 1000)) == "2026-09-25"  # noqa: E731
    md += ["## 4. Покупки наших 133: есть в наших файлах / нет (сразу за ним, 0.5 SOL)", "",
           "«В файлах» -- подпись есть в celi.json (наши 133, 25.09) или в строках m4. Сигналы архива -- только за SOL "
           "(покупки за доллары архивом-сигналом не считаются).", "",
           "| выборка | пул | +6 | +36 | +150 |", "|---|---|---|---|---|"]
    for имя, фильтр in (("25.09, нет в файлах", lambda с: d25(с) and с["signature"] not in наши_sig),
                        ("25.09, есть в файлах", lambda с: d25(с) and с["signature"] in наши_sig),
                        ("7 суток, нет в файлах", lambda с: с["signature"] not in наши_sig),
                        ("7 суток, есть в файлах", lambda с: с["signature"] in наши_sig)):
        for p in ("pump", "raydium-launchpad", "pump-amm"):
            сс = [с for с in с133 if фильтр(с) and с["pool"] == p]
            if сс:
                md.append(f"| {имя} | {ТИПЫ[p]} | {ячейка([пп(с, 'S0|0.5|6') for с in сс])} | "
                          f"{ячейка([пп(с, 'S0|0.5|36') for с in сс])} | {ячейка([пп(с, 'S0|0.5|150') for с in сс])} |")
    md += ["", f"Сигналов 133 за 25.09: {sum(1 for с in с133 if d25(с))}, из них нет в наших файлах: "
               f"{sum(1 for с in с133 if d25(с) and с['signature'] not in наши_sig)} (разбор 25.09 насчитал 485 пропущенных "
               "покупок от 2 SOL за SOL или доллары; здесь -- только за SOL и только первые покупки).", ""]
    (КОРЕНЬ / а.vyhod).write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md[:40]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
