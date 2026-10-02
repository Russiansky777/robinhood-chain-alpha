#!/usr/bin/env python3
"""Коллы KOL: есть ли толпа и опережаема ли она. Архив PumpApi, только чтение.

Вход -- data/podbivka/kolly_2026-09-24_10-01.json (копия data/kolly/kolly_2026-09-24_10-01.json ветки
claude/stroiteli, коммит 0d56955): 100 коллов, 68 минтов, 17 авторов, окно 24.09 10:32Z … 01.10 23:58Z,
у всех есть author_wallet.

По каждому коллу (минт, время поста T):
  * покупок и SOL за [T−10 мин, T) и за (T, T+1], (T, T+3], (T, T+10] мин -- все покупки минта в архиве,
    котировка WSOL (SOL-экв. по quoteAmount);
  * цена пула в T, T+1, T+3, T+10 -- по последнему событию минта со временем ≤ точки (цена = x/y состояния
    события: у кривых виртуальные резервы, у x*y=k -- реальные);
  * первые 10 покупателей после T (кошелёк -- трейдер события); повторяющиеся по разным коллам сведены в
    «ходящих за коллами»;
  * покупал ли кошелёк автора (author_wallet) этот минт ДО поста -- и за сколько минут;
  * наш вход T+30 с, выходы T+3 мин и T+10 мин, билет 0.3, п.п. чистыми (0.002 SOL на круг): модель архива
    analysis/podbivka_arhiv_den.модель от события, последнего со временем ≤ T+30 с (вход «S0» = состояние после
    него), горизонты -- в слотах до T+3 и T+10 мин по фактическим слотам событий; Pump AMM -- модель v6.
«Толпа» в таблице: покупок в (T, T+10] ≥ 3 × покупок в [T−10, T) -- то есть втрое к предыдущим десяти минутам.

--den YYYY-MM-DD: один проход по часам суток (+1 час хвоста) -- события ТОЛЬКО 68 минтов (по полю mint),
выход data/podbivka/kolly/kolly_sobr_<день>.json.gz.
--svod: страница docs/podbivka_2026-10-03_kolly.md.
"""
from __future__ import annotations

import argparse
import calendar
import collections
import glob
import gzip
import io
import json
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_arhiv_den as A  # noqa: E402

КОРЕНЬ = A.КОРЕНЬ
П = КОРЕНЬ / "data" / "podbivka"
ПК = П / "kolly"
ВХОД = "kolly_2026-09-24_10-01.json"
ДО, ПОСЛЕ = 600, 600          # секунд до поста и после (окно сбора)
ВХОД_СЕК = 30                  # наш вход: T+30 с
ВЫХОДЫ_МИН = (3, 10)
БИЛЕТ = 0.3
ТОЛПА_РАЗ = 3.0
р_ts = re.compile(r'"timestamp":\s*"?(\d+)')


def коллы() -> list:
    сп = json.loads((П / ВХОД).read_text(encoding="utf-8"))
    for к in сп:
        к["t"] = calendar.timegm(time.strptime(к["t_utc"], "%Y-%m-%dT%H:%M:%SZ"))
    return сп


def день(д: str) -> int:
    import podbivka_run as R  # noqa: PLC0415
    import requests  # noqa: PLC0415
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "zstandard"], check=True)
    import zstandard  # noqa: PLC0415
    сп = коллы()
    минты = {к["mint"] for к in сп}
    t0 = calendar.timegm(time.strptime(д, "%Y-%m-%d"))
    часы = [time.strftime("%Y/%m/%d/%H", time.gmtime(t0 + 3600 * k)) for k in range(25)]
    # окна сбора: по каждому минту объединение [T-ДО, T+ПОСЛЕ] его коллов, пересечённое с сутками +1 час
    окна: dict = {}
    for к in сп:
        окна.setdefault(к["mint"], []).append((к["t"] - ДО, к["t"] + ПОСЛЕ))
    события: dict = {}
    счёт = {"строк": 0, "файлов": 0, "ошибки": [], "событий": 0, "минтов": 0}
    for чс in часы:
        url = f"https://replay.pumpapi.io/{чс}.jsonl.zst"
        прочитано = 0
        for попытка in range(4):
            try:
                with A.открыть_час(requests, url, чс) as о:
                    if о.status_code != 200:
                        счёт["ошибки"].append(f"{чс}: http {о.status_code}")
                        break
                    if попытка == 0:
                        счёт["файлов"] += 1
                    n = 0
                    for стр in io.TextIOWrapper(zstandard.ZstdDecompressor().stream_reader(о.raw), encoding="utf-8",
                                                errors="replace"):
                        n += 1
                        if n <= прочитано:
                            continue
                        прочитано = n
                        счёт["строк"] += 1
                        мм = A.р_mint.search(стр)
                        if not мм or мм.group(1) not in минты:
                            continue
                        тм = р_ts.search(стр)
                        ts = int(тм.group(1)) / 1000 if тм else 0
                        if not any(a <= ts <= b for a, b in окна[мм.group(1)]):
                            continue
                        try:
                            e_full = json.loads(стр)
                        except ValueError:
                            continue
                        e = {k: e_full.get(k) for k in A.КЛЮЧИ}
                        бд = [b.get("trader") for b in e_full.get("breakdown") or [] if isinstance(b, dict) and b.get("trader")]
                        e["трейдер"] = бд[0] if бд else e.get("txSigner")
                        события.setdefault(мм.group(1), []).append(e)
                        счёт["событий"] += 1
                    break
            except (OSError, RuntimeError, ValueError) as exc:
                if попытка == 3:
                    счёт["ошибки"].append(f"{чс}: {type(exc).__name__} {str(exc)[:120]}")
                else:
                    time.sleep(3 * (попытка + 1))
    счёт["минтов"] = len(события)
    ПК.mkdir(parents=True, exist_ok=True)
    out = ПК / f"kolly_sobr_{д}.json.gz"
    out.write_bytes(gzip.compress(json.dumps({"день": д, "счёт": счёт, "события": события},
                                             ensure_ascii=False).encode()))
    R.записано(out)
    print(out.name, "строк", счёт["строк"], "событий", счёт["событий"], "минтов", счёт["минтов"],
          "ошибки", счёт["ошибки"], flush=True)
    return 0


def цена(e: dict):
    с = A.ст(e)
    return (с[0] / с[1]) if (с and с[1]) else None


def разбор(к: dict, ряд: list) -> dict:
    """Все числа одного колла по ряду событий его минта (ряд отсортирован по слоту и времени)."""
    T = к["t"]
    из_ = {"mint": к["mint"], "t_utc": к["t_utc"], "author": к["author_handle"], "post_url": к.get("post_url"),
           "author_wallet": к.get("author_wallet"), "событий_в_окне": len(ряд)}
    def в(a, b, только_покупки=True):
        сп = [e for e in ряд if a < (e.get("timestamp") or 0) / 1000 <= b
              and (e.get("action") == "buy" or not только_покупки)]
        return сп
    до = [e for e in ряд if T - ДО <= (e.get("timestamp") or 0) / 1000 < T and e.get("action") == "buy"]
    из_["до_покупок"] = len(до)
    из_["до_sol"] = round(sum(float(e.get("quoteAmount") or 0) for e in до if e.get("quoteMint") == A.WSOL), 3)
    for мин in (1, 3, 10):
        сп = в(T, T + 60 * мин)
        из_[f"после_{мин}_покупок"] = len(сп)
        из_[f"после_{мин}_sol"] = round(sum(float(e.get("quoteAmount") or 0) for e in сп if e.get("quoteMint") == A.WSOL), 3)
    из_["толпа_x"] = (round(из_["после_10_покупок"] / из_["до_покупок"], 2) if из_["до_покупок"]
                      else (None if not из_["после_10_покупок"] else float("inf")))
    из_["толпа_есть"] = bool(из_["после_10_покупок"] >= ТОЛПА_РАЗ * из_["до_покупок"]) if из_["до_покупок"] \
        else bool(из_["после_10_покупок"] >= ТОЛПА_РАЗ)
    # цена в T, T+1, T+3, T+10
    for имя, сек in (("T", 0), ("T1", 60), ("T3", 180), ("T10", 600)):
        канд = [e for e in ряд if (e.get("timestamp") or 0) / 1000 <= T + сек and цена(e)]
        из_[f"цена_{имя}"] = цена(канд[-1]) if канд else None
    for имя in ("T1", "T3", "T10"):
        p0, p = из_["цена_T"], из_[f"цена_{имя}"]
        из_[f"цена_{имя}_пп"] = round((p / p0 - 1) * 100, 2) if (p0 and p) else None
    # первые 10 покупателей после T
    из_["первые_10"] = [{"кто": e.get("трейдер"), "sol": round(float(e.get("quoteAmount") or 0), 3),
                         "сек": round((e.get("timestamp") or 0) / 1000 - T, 1)}
                        for e in в(T, T + 60 * 10)[:10]]
    # покупал ли кошелёк автора до поста
    а = к.get("author_wallet")
    его = [e for e in ряд if e.get("трейдер") == а and e.get("action") == "buy"
           and (e.get("timestamp") or 0) / 1000 < T]
    из_["автор_покупал_до"] = bool(его)
    из_["автор_минут_до"] = round((T - (его[-1].get("timestamp") or 0) / 1000) / 60, 2) if его else None
    из_["автор_sol_до"] = round(sum(float(e.get("quoteAmount") or 0) for e in его), 3) if его else None
    # наш вход T+30 с, выходы T+3 и T+10 мин
    вх = [e for e in ряд if (e.get("timestamp") or 0) / 1000 <= T + ВХОД_СЕК and A.ст(e)]
    if not вх:
        return {**из_, "why_not": "до T+30 с нет события с состоянием пула"}
    e_вх = вх[-1]
    s_вх = e_вх.get("block")
    гор = {}
    for мин in ВЫХОДЫ_МИН:
        канд = [e for e in ряд if (e.get("timestamp") or 0) / 1000 <= T + 60 * мин and e.get("block")]
        гор[мин] = (канд[-1]["block"] - s_вх + 1) if канд and канд[-1]["block"] >= s_вх else None
    A.БИЛЕТЫ, A.ВЫХОДЫ = (БИЛЕТ,), tuple(h for h in гор.values() if h and h >= 1)
    i0 = ряд.index(e_вх)
    м = A.модель(e_вх, ряд[i0:])
    пп = м.get("пп") or {}
    из_.update(модель_why_not=м.get("why_not"), модель_pump_amm=м.get("модель_pump_amm"),
               вход_слот=s_вх, вход_pool=e_вх.get("pool"))
    for мин in ВЫХОДЫ_МИН:
        h = гор[мин]
        из_[f"наш_вход_{мин}_пп"] = пп.get(f"S0|{БИЛЕТ}|{h}") if h else None
        из_[f"наш_вход_{мин}_слотов"] = h
    return из_


def св(v: list, знак: bool = True) -> str:
    v = [x for x in v if x is not None]
    if not v:
        return "—"
    м = statistics.median(v)
    return f"{м:+.1f}" if знак else f"{м:.1f}"


def свод() -> int:
    import podbivka_run as R  # noqa: PLC0415
    файлы = sorted(glob.glob(str(ПК / "kolly_sobr_*.json.gz")))
    if not файлы:
        print("нет файлов kolly_sobr_* -- сбор по архиву не закончен", flush=True)
        return 1
    события: dict = {}
    дни = []
    for f in файлы:
        д = json.loads(gzip.decompress(Path(f).read_bytes()))
        дни.append({"день": д["день"], "строк": д["счёт"]["строк"], "ошибки": д["счёт"]["ошибки"]})
        for м, сп in д["события"].items():
            события.setdefault(м, []).extend(сп)
        del д
    for м in события:
        видел, сп = set(), []
        for e in sorted(события[м], key=lambda e: ((e.get("block") or 0), (e.get("timestamp") or 0))):
            if e.get("signature") in видел:
                continue
            видел.add(e.get("signature"))
            сп.append(e)
        события[м] = сп
    сп_к = коллы()
    ряды = []
    for к in сп_к:
        ряд = [e for e in события.get(к["mint"]) or []
               if к["t"] - ДО <= (e.get("timestamp") or 0) / 1000 <= к["t"] + ПОСЛЕ]
        ряды.append(разбор(к, ряд) if ряд else
                    {"mint": к["mint"], "t_utc": к["t_utc"], "author": к["author_handle"],
                     "author_wallet": к.get("author_wallet"), "событий_в_окне": 0,
                     "why_not": "событий минта в окне архива нет"})
    ок = [r for r in ряды if not r.get("why_not")]
    # ходящие за коллами
    ход = collections.Counter()
    за_кем: dict = {}
    for r in ок:
        for x in r["первые_10"]:
            if x["кто"]:
                ход[x["кто"]] += 1
                за_кем.setdefault(x["кто"], set()).add(r["author"])
    авторы = sorted({r["author"] for r in ряды})
    md = ["# Подбивка: коллы KOL -- есть ли толпа и опережаема ли она (24.09 10:32Z – 01.10 23:58Z)", "",
          f"Вход: {len(сп_к)} коллов, {len({к['mint'] for к in сп_к})} минтов, {len(авторы)} авторов "
          f"(`data/kolly/kolly_2026-09-24_10-01.json` ветки claude/stroiteli, коммит 0d56955; копия в "
          f"`data/podbivka/{ВХОД}`). Архив PumpApi: {len(файлы)} суток, строк "
          f"{sum(d['строк'] for d in дни)}, ошибок чтения часов {sum(len(d['ошибки']) for d in дни)}. "
          f"В счёт вошли {len(ок)} коллов из {len(ряды)}; остальные -- без событий минта в окне архива.", "",
          "Определения. «Толпа» -- покупок минта в (T, T+10 мин] не меньше чем втрое к покупкам в "
          "[T−10 мин, T). Цена -- x/y состояния последнего события со временем ≤ точки (у кривых виртуальные "
          "резервы, у x\\*y=k реальные), в п.п. к цене в T. Наш вход -- T+30 с (состояние после последнего "
          "события до этой секунды), выходы T+3 и T+10 мин, билет 0.3, п.п. чистыми (0.002 SOL на круг), "
          "модель архива, Pump AMM -- v6. «Автор покупал до» -- покупка этого минта кошельком author_wallet "
          "раньше T в окне [T−10 мин, T). Ничего не рекомендуется.", "",
          "| KOL | коллов | в счёте | толпа ≥ ×3 | медиана ×толпы | цена +1 | цена +3 | цена +10 | "
          "наш вход +3 | наш вход +10 | автор покупал до |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for a in авторы:
        все = [r for r in ряды if r["author"] == a]
        л = [r for r in все if not r.get("why_not")]
        if not л:
            md.append(f"| `{a}` | {len(все)} | 0 | — | — | — | — | — | — | — | — |")
            continue
        толпа = sum(1 for r in л if r["толпа_есть"])
        x = [r["толпа_x"] for r in л if r["толпа_x"] not in (None, float("inf"))]
        авт = sum(1 for r in л if r["автор_покупал_до"])
        md.append(f"| `{a}` | {len(все)} | {len(л)} | {толпа} / {len(л)} ({100 * толпа / len(л):.0f} %) | "
                  f"{св(x, False)} | {св([r['цена_T1_пп'] for r in л])} | {св([r['цена_T3_пп'] for r in л])} | "
                  f"{св([r['цена_T10_пп'] for r in л])} | {св([r['наш_вход_3_пп'] for r in л])} | "
                  f"{св([r['наш_вход_10_пп'] for r in л])} | {авт} / {len(л)} |")
    вс_толпа = sum(1 for r in ок if r["толпа_есть"])
    вх3 = [r["наш_вход_3_пп"] for r in ок if r["наш_вход_3_пп"] is not None]
    вх10 = [r["наш_вход_10_пп"] for r in ок if r["наш_вход_10_пп"] is not None]
    ц1 = [r["цена_T1_пп"] for r in ок if r["цена_T1_пп"] is not None]
    авт_все = sum(1 for r in ок if r["автор_покупал_до"])
    md += ["", "## Итог одной строкой", "",
           f"Толпа есть: в {вс_толпа} из {len(ок)} коллов ({100 * вс_толпа / len(ок):.0f} %) покупок за десять "
           f"минут после поста втрое больше, чем за десять минут до него; медиана цены к T -- "
           f"{св(ц1)} п.п. через минуту, {св([r['цена_T3_пп'] for r in ок])} через три и "
           f"{св([r['цена_T10_пп'] for r in ок])} через десять. "
           f"Опережаема ли: наш вход через 30 с даёт медиану {св(вх3)} п.п. к T+3 мин (n {len(вх3)}) и "
           f"{св(вх10)} п.п. к T+10 мин (n {len(вх10)}), в плюсе "
           f"{sum(1 for v in вх3 if v > 0)} из {len(вх3)} и {sum(1 for v in вх10 if v > 0)} из {len(вх10)}. "
           f"Кошелёк автора покупал минт до поста в {авт_все} из {len(ок)} коллов.", "",
           "## Ходящие за коллами (в первых 10 покупателях больше одного колла)", "",
           "| кошелёк | коллов | у скольких KOL |", "|---|---|---|"]
    md += [f"| `{w}` | {n} | {len(за_кем[w])} |" for w, n in ход.most_common(40) if n > 1] or ["| — | | |"]
    md += ["", "## По коллам", "",
           "| время T | KOL | минт | до: покупок / SOL | +1 | +3 | +10 (покупок) | ×толпы | цена +1 / +3 / +10 | "
           "наш вход +3 / +10 | автор до поста |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(ряды, key=lambda r: r["t_utc"]):
        if r.get("why_not"):
            md.append(f"| {r['t_utc'][5:16]} | `{r['author']}` | `{r['mint'][:8]}` | {r['why_not']} |||||||")
            continue
        md.append(f"| {r['t_utc'][5:16]} | `{r['author']}` | `{r['mint'][:8]}` | "
                  f"{r['до_покупок']} / {r['до_sol']} | {r['после_1_покупок']} | {r['после_3_покупок']} | "
                  f"{r['после_10_покупок']} | " + (f"{r['толпа_x']}" if r["толпа_x"] is not None else "—") + " | " +
                  " / ".join(f"{r[f'цена_{k}_пп']:+.0f}" if r[f"цена_{k}_пп"] is not None else "—"
                             for k in ("T1", "T3", "T10")) + " | " +
                  " / ".join(f"{r[f'наш_вход_{m}_пп']:+.1f}" if r[f"наш_вход_{m}_пп"] is not None else "—"
                             for m in ВЫХОДЫ_МИН) + " | " +
                  (f"да, за {r['автор_минут_до']} мин на {r['автор_sol_до']} SOL" if r["автор_покупал_до"] else "нет") + " |")
    md.append("")
    out = КОРЕНЬ / "docs" / "podbivka_2026-10-03_kolly.md"
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    R.записано(out)
    дт = ПК / "kolly_svod.json"
    дт.write_text(json.dumps({"дни": дни, "ряды": ряды, "ходящие": ход.most_common(200)},
                             ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(дт)
    print(out.name, "в счёте", len(ок), "из", len(ряды), "толпа", вс_толпа, flush=True)
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--den", default="")
    р.add_argument("--svod", action="store_true")
    а = р.parse_args()
    if а.svod:
        return свод()
    if not а.den:
        р.error("нужен --den YYYY-MM-DD или --svod")
    return день(а.den)


if __name__ == "__main__":
    raise SystemExit(main())
