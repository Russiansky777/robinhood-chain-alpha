#!/usr/bin/env python3
"""«Размер, а не имя»: все покупки ≥ 5 SOL в сыром архиве PumpApi без привязки к кошельку -- модель входа «конец слота».

Слово владельца 01.10 (ночь): из сырого архива за 27–30.09 ВСЕ покупки ≥ 5 / 10 / 20 SOL, без привязки к кошельку →
модель при входе «конец слота» (S0_дно), билет 0.3, выходы +12 / +72 / +108: среднее, медиана, доля в плюсе по трём
корзинам размера и по доле от резерва пула (≥ 5 / 10 / 20 %); отдельно -- та же таблица только для покупок, за которыми
в первые 12 слотов идут ≥ 3 чужих покупки («толпа есть»). Корзины не дробить (≥, накопительно), отбора «лучших» нет.

--den YYYY-MM-DD (бегунок lab-miami, часы с локального диска): один проход по часам суток (+1 час хвоста окна) в
порядке файла. Покупка-сигнал -- событие action=buy, котировка WSOL, quoteAmount ≥ 5 SOL, пул поддержанного моделью
типа (кривые pump / raydium-launchpad, x*y=k pump-amm / raydium-cpmm / meteora-damm-v1), слот -- в сутках. С сигнала
пул «под наблюдением» 160 слотов (как в архиве подбивки): его события пишутся в ряд; когда пул ушёл за s0+160 --
модель analysis/podbivka_arhiv_den.модель на ряду [сигнал … s0+160] и запись, ряд до самого раннего ждущего сигнала
отбрасывается (память -- только живые окна). Толпа -- покупок того же пула в слотах s0+1 … s0+12 от других кошельков
(трейдер -- первый в breakdown, иначе txSigner), любого размера; это знание ПОСЛЕ входа (не признак для входа).
Доля от резерва -- quoteAmount / (резерв котировки после события − quoteAmount); резерв -- тот, по которому модель
считает цену: у кривых виртуальный vQuoteInBondingCurve, у x*y=k quoteInPool.
Выход: data/podbivka/razmer/razmer_<день>.json.gz (строки сигналов + счёт).

--svod: сводит все razmer_*.json.gz → docs/podbivka_2026-10-01_razmer_ne_imya.md. Pump AMM -- только число (как на
прочих страницах: модель его рядов не сверена), в таблицы не входит.
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
П = КОРЕНЬ / "data" / "podbivka" / "razmer"
ПОРОГ = 5.0
ОКНО = 160
ТОЛПА_СЛОТОВ, ТОЛПА_МИН = 12, 3
ВЫХОДЫ = (12, 72, 108)
ТИПЫ = A.XYK | A.КРИВЫЕ
ДЛЯ_ВЫВОДА = ("pump", "raydium-launchpad", "raydium-cpmm", "meteora-damm-v1")
р_action = re.compile(r'"action":\s*"(buy|sell)"')
р_qamt = re.compile(r'"quoteAmount":\s*"?([0-9.eE+-]+)')


def трейдер(e_full: dict) -> str | None:
    бд = [b.get("trader") for b in e_full.get("breakdown") or [] if isinstance(b, dict) and b.get("trader")]
    return бд[0] if бд else e_full.get("txSigner")


def закрыть(с: dict, ряд: list) -> list:
    """Строка сигнала по ряду пула (ряд начинается не позже сигнала)."""
    i0 = next((i for i, e in enumerate(ряд) if e["signature"] == с["signature"] and e["block"] == с["block"]), None)
    if i0 is None:
        return [с["signature"], с["pool"], с["poolId"], с["mint"], с["trader"], с["block"], с["timestamp"], с["sol"],
                с["резерв_до"], с["доля"], None, None, None, None, "сигнал не найден в ряду"]
    окно = [e for e in ряд[i0:] if e["block"] <= с["block"] + ОКНО]
    м = A.модель(с, окно)
    пп = м.get("пп") or {}
    толпа = sum(1 for e in окно if e.get("action") == "buy" and с["block"] < e["block"] <= с["block"] + ТОЛПА_СЛОТОВ
                and e.get("трейдер") != с["trader"])
    return [с["signature"], с["pool"], с["poolId"], с["mint"], с["trader"], с["block"], с["timestamp"], с["sol"],
            с["резерв_до"], с["доля"], толпа] + [пп.get(f"S0_дно|0.3|{h}") for h in ВЫХОДЫ] + [м.get("why_not")]


def день(д: str) -> Path:
    import requests  # noqa: PLC0415
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "zstandard"], check=True)
    import zstandard  # noqa: PLC0415
    A.БИЛЕТЫ, A.ВЫХОДЫ = (0.3,), ВЫХОДЫ
    t0 = calendar.timegm(time.strptime(д, "%Y-%m-%d"))
    лево, право = t0 * 1000, (t0 + 86400) * 1000
    часы = [time.strftime("%Y/%m/%d/%H", time.gmtime(t0 + 3600 * k)) for k in range(25)]      # +1 час хвоста окна
    ряды: dict = {}          # poolId -> [события]
    ждут: dict = {}          # poolId -> [сигналы, ждущие закрытия окна]
    строки: list = []
    счёт = {"строк": 0, "файлов": 0, "ошибки": [], "покупок_от_5_все_котировки": 0, "покупок_от_5_wsol_по_типу": collections.Counter()}

    def закрыть_пул(pid: str, до_слота: int | None) -> None:
        """Закрыть сигналы пула, чьё окно кончилось раньше до_слота (None -- все); подрезать ряд."""
        сп = ждут.get(pid) or []
        ост = []
        for с in сп:
            if до_слота is None or до_слота > с["block"] + ОКНО:
                строки.append(закрыть(с, ряды.get(pid) or []))
            else:
                ост.append(с)
        if ост:
            ждут[pid] = ост
            мин_s0 = min(с["block"] for с in ост)
            ряд = ряды[pid]
            k = 0
            while k < len(ряд) and ряд[k]["block"] < мин_s0:
                k += 1
            if k:
                del ряд[:k]
        else:
            ждут.pop(pid, None)
            ряды.pop(pid, None)

    for ч in часы:
        url = f"https://replay.pumpapi.io/{ч}.jsonl.zst"
        прочитано = 0
        for попытка in range(4):
            try:
                with A.открыть_час(requests, url, ч) as о:
                    if о.status_code != 200:
                        счёт["ошибки"].append(f"{ч}: http {о.status_code}")
                        break
                    if попытка == 0:
                        счёт["файлов"] += 1
                    n_стр = 0
                    for стр in io.TextIOWrapper(zstandard.ZstdDecompressor().stream_reader(о.raw), encoding="utf-8",
                                                errors="replace"):
                        n_стр += 1
                        if n_стр <= прочитано:
                            continue
                        прочитано = n_стр
                        счёт["строк"] += 1
                        пм = A.р_pool.search(стр)
                        if not пм:
                            continue
                        pid = пм.group(1)
                        под_наблюдением = pid in ждут
                        ам = р_action.search(стр)
                        кандидат = False
                        if ам and ам.group(1) == "buy":
                            qa = р_qamt.search(стр)
                            try:
                                кандидат = bool(qa) and float(qa.group(1)) >= ПОРОГ
                            except ValueError:
                                кандидат = False
                        if not (под_наблюдением or кандидат):
                            continue
                        try:
                            e_full = json.loads(стр)
                        except ValueError:
                            continue
                        e = {k: e_full.get(k) for k in A.КЛЮЧИ}
                        e["трейдер"] = трейдер(e_full)
                        блок = e.get("block")
                        if not isinstance(блок, int):
                            continue
                        if под_наблюдением:
                            закрыть_пул(pid, блок)
                            if pid in ждут:
                                ряды[pid].append(e)
                        if not кандидат:
                            continue
                        счёт["покупок_от_5_все_котировки"] += 1
                        ts = e.get("timestamp") or 0
                        if e.get("quoteMint") != A.WSOL or not (лево <= ts < право):
                            continue
                        счёт["покупок_от_5_wsol_по_типу"][e.get("pool")] += 1
                        if e.get("pool") not in ТИПЫ:
                            continue
                        с_ = A.ст(e)
                        q = float(e.get("quoteAmount") or 0)
                        резерв_до = (с_[0] - q) if с_ else None
                        с = {"signature": e["signature"], "pool": e.get("pool"), "poolId": pid, "mint": e.get("mint"),
                             "trader": e["трейдер"], "block": блок, "timestamp": ts, "sol": round(q, 6),
                             "резерв_до": round(резерв_до, 6) if резерв_до else None,
                             "доля": round(q / резерв_до, 6) if резерв_до and резерв_до > 0 else None}
                        if pid not in ждут:
                            ряды[pid] = [e]
                        ждут.setdefault(pid, []).append(с)
                break
            except Exception as exc:  # noqa: BLE001
                if A.os.environ.get("PODB_ARHIV_KESH"):
                    (Path(A.os.environ["PODB_ARHIV_KESH"]) / f"{ч}.jsonl.zst").unlink(missing_ok=True)
                if попытка == 3:
                    счёт["ошибки"].append(f"{ч}: {type(exc).__name__}: {str(exc)[:100]}")
                else:
                    счёт.setdefault("докачки", []).append(f"{ч}: после {прочитано} строк: {type(exc).__name__}")
                    time.sleep(5 * (попытка + 1))
        print(f"{ч}: строк {счёт['строк']}, сигналов закрыто {len(строки)}, пулов под наблюдением {len(ждут)}", flush=True)
    for pid in list(ждут):
        закрыть_пул(pid, None)
    счёт["покупок_от_5_wsol_по_типу"] = dict(счёт["покупок_от_5_wsol_по_типу"])
    П.mkdir(parents=True, exist_ok=True)
    out = П / f"razmer_{д}.json.gz"
    поля = ["signature", "pool", "poolId", "mint", "trader", "block", "timestamp", "sol", "резерв_до", "доля", "толпа_12"] + \
           [f"пп_{h}" for h in ВЫХОДЫ] + ["why_not"]
    out.write_bytes(gzip.compress(json.dumps({"день": д, "часы": часы, "порог_sol": ПОРОГ, "окно_слотов": ОКНО,
                                              "счёт": счёт, "поля": поля, "строки": строки},
                                             ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6))
    return out


def стат(v: list) -> str:
    v = [x for x in v if x is not None]
    if not v:
        return "—"
    return f"{len(v)} / {statistics.mean(v):+.1f} / {statistics.median(v):+.1f} / {100 * sum(1 for x in v if x > 0) / len(v):.0f}%"


def свод() -> int:
    файлы = sorted(glob.glob(str(П / "razmer_*.json.gz")))
    строки, счёт, дни, ошибки = [], collections.Counter(), [], []
    for f in файлы:
        д = json.loads(gzip.decompress(Path(f).read_bytes()))
        и = {k: i for i, k in enumerate(д["поля"])}
        строки += [{k: r[i] for k, i in и.items()} for r in д["строки"]]
        счёт.update(д["счёт"]["покупок_от_5_wsol_по_типу"])
        счёт["все_котировки"] += д["счёт"]["покупок_от_5_все_котировки"]
        дни.append(f"{д['день']} (часов {д['счёт']['файлов']})")
        ошибки += д["счёт"]["ошибки"]
    пр = [с for с in строки if с["pool"] in ДЛЯ_ВЫВОДА and not с["why_not"] and с["пп_72"] is not None]
    амм = [с for с in строки if с["pool"] == "pump-amm"]
    нет = [с for с in строки if с["pool"] in ДЛЯ_ВЫВОДА and (с["why_not"] or с["пп_72"] is None)]

    def таблица(L: list) -> list:
        md = ["| корзина | n | пулов | +12 | +72 | +108 |", "|---|---|---|---|---|---|"]
        for имя, фл in ([(f"размер ≥ {п:g} SOL", lambda с, п=п: с["sol"] >= п) for п in (5, 10, 20)] +
                        [(f"доля резерва ≥ {п:g} %", lambda с, п=п: (с["доля"] or 0) >= п / 100) for п in (5, 10, 20)]):
            K = [с for с in L if фл(с)]
            md.append(f"| {имя} | {len(K)} | {len({с['poolId'] for с in K})} | "
                      + " | ".join(стат([с[f"пп_{h}"] for с in K]) for h in ВЫХОДЫ) + " |")
        return md

    толпа = [с for с in пр if (с["толпа_12"] or 0) >= ТОЛПА_МИН]
    без = [с for с in пр if (с["толпа_12"] or 0) < ТОЛПА_МИН]
    md = ["# Размер, а не имя: все покупки ≥ 5 SOL в архиве PumpApi без привязки к кошельку", "",
          f"Архив PumpApi (сырые часовые файлы, бегунок lab-miami), сутки: {', '.join(дни)}. Сигнал -- КАЖДАЯ покупка "
          "(action=buy) с котировкой WSOL от 5 SOL в пуле кривой pump.fun / LaunchLab или x*y=k CPMM / DAMM v1 -- без "
          "привязки к кошельку, без правила окна, без отбора. Модель analysis/podbivka_arhiv_den.py (та же, что у всех "
          "страниц архива): вход «конец слота» (после последнего события слота покупки), билет 0.3 SOL, выходы +12 / +72 / "
          "+108 слотов, издержки 0.002 SOL на круг, налог Token-2022 не учтён. Корзины накопительные (≥), не дробятся. "
          "Доля резерва -- размер покупки к резерву котировки пула до неё (у кривых -- виртуальный, по нему считается "
          "цена). Ячейка: n / среднее / медиана / в плюсе, п.п. чистыми. Pump AMM -- только число. Сбор: "
          "analysis/podbivka_razmer_ne_imya.py --den, свод -- --svod. Ничего не рекомендуется.", "",
          f"Покупок от 5 SOL в архиве (все котировки): {счёт['все_котировки']}; с котировкой WSOL по типам пула: "
          + ", ".join(f"{k} {v}" for k, v in sorted(((k, v) for k, v in счёт.items() if k != "все_котировки"), key=lambda t: -t[1]))
          + f". В таблицах (кривые, CPMM, DAMM v1, модель посчитана): {len(пр)}; модель не посчитана: {len(нет)}; "
          f"Pump AMM (не в таблицах): {len(амм)}.", "",
          "## Все покупки", ""] + таблица(пр) + [
          "", f"## «Толпа есть»: за покупкой в слотах +1 … +{ТОЛПА_СЛОТОВ} -- ≥ {ТОЛПА_МИН} покупки того же пула с других кошельков", "",
          "Толпа видна только после входа (слоты после покупки) -- это разрез, а не признак для входа.", ""] + таблица(толпа) + [
          "", f"## Для сравнения: толпы нет (< {ТОЛПА_МИН} чужих покупок в +1 … +{ТОЛПА_СЛОТОВ})", ""] + таблица(без)
    if ошибки:
        md += ["", "Ошибки чтения часов: " + "; ".join(ошибки)]
    (КОРЕНЬ / "docs" / "podbivka_2026-10-01_razmer_ne_imya.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    г = р.add_mutually_exclusive_group(required=True)
    г.add_argument("--den", help="сутки YYYY-MM-DD (UTC)")
    г.add_argument("--svod", action="store_true")
    а = р.parse_args()
    if а.svod:
        return свод()
    out = день(а.den)
    import podbivka_run as R  # noqa: PLC0415
    R.записано(out)
    R.пуш(f"Podbivka-2: razmer ne imya {а.den} [automated]", [str(out)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
