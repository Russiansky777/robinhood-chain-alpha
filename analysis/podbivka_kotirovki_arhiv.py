#!/usr/bin/env python3
"""Котировки денежных групп за 28–30.09 по архиву PumpApi (USDC / GP / CRACKER / прочие) -- для решения о склейке.

Слово владельца 01.10: по адресам групп lane_s0 (10), batch5 (12), cand1 (9), leader (1) -- только сигналы по правилу
pyg7 (первая покупка пары источник + минт: нет покупки того же минта этим адресом в предыдущие 1800 слотов; от 2 SOL-экв.),
окно 28.09 00:00Z → 30.09 24:00Z. По каждому: минт котировки пула токена × тип пула × группа × источник; n за три дня
и в сутки. Итог -- три строки: «USDC на CPMM или Pump AMM» (двухшаговый путь полосы должен был сработать), «USDC на
других типах» (второй ноги нет), «GP / CRACKER всего». Порог владельца: ≥ 1 в сутки USDC на других типах -- склейку строим.

--den YYYY-MM-DD (бегунок lab-miami, часы архива с локального диска, без ключей): проход по часам суток + 1 час
разгона перед ними (правило окна 1800 слотов ≈ 12 мин). Пишутся ВСЕ покупки адресов групп (трейдер -- в breakdown или
txSigner): подпись, тип пула, пул, минт, котировка, quoteAmount / tokenAmount, слот, время -- и цена котировки в SOL
на этот момент: по последней сделке архива пары «котировка / WSOL» (исполненная цена quoteAmount / tokenAmount сделки
от 0.05 SOL; у пар «WSOL / котировка» -- обратная). Выход: data/podbivka/kotirovki_arhiv/pokupki_<день>.json.gz.
--svod: правило pyg7 по всем покупкам подряд (разгон -- только для окна), промежуточные ноги маршрута отброшены
(покупка минта, который в той же транзакции служит котировкой другой ноги того же трейдера), SOL-экв. = quoteAmount ×
цена котировки → docs/podbivka_2026-10-01_kotirovki_grupp.md и data/podbivka/kotirovki_grupp_2026-10-01.json.
Покрытие архива: pump.fun / LaunchLab / Pump AMM / Raydium CPMM / CLMM / Meteora DLMM / DAMM v1 / v2 / DBC; Orca
Whirlpool, Raydium AMM v4 и редких программ в архиве нет (27.09 это было ~2 % не-SOL покупок групп).
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
ВЫХ = П / "kotirovki_arhiv"
WSOL = A.WSOL
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ИЗВЕСТНЫЕ = {WSOL: "WSOL", USDC: "USDC", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT"}
ОКНО_ДОКУПКИ, ПОРОГ_SOL, МИН_SOL_ЦЕНЫ = 1800, 2.0, 0.05
ДВУХШАГОВЫЕ = {"raydium-cpmm", "pump-amm"}          # USDC на них -- двухшаговый путь полосы (слово владельца)
р_qmint = re.compile(r'"quoteMint":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_mint = re.compile(r'"mint":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_action = re.compile(r'"action":\s*"(buy|sell)"')
р_qamt = re.compile(r'"quoteAmount":\s*"?([0-9.eE+-]+)')
р_tamt = re.compile(r'"tokenAmount":\s*"?([0-9.eE+-]+)')


def адреса() -> dict:
    return json.loads((П / "gruppy_kotirovki_2026-10-01.json").read_text(encoding="utf-8"))["адреса"]


def день(д: str) -> Path:
    import requests  # noqa: PLC0415
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "zstandard"], check=True)
    import zstandard  # noqa: PLC0415
    адр = адреса()
    t0 = calendar.timegm(time.strptime(д, "%Y-%m-%d"))
    часы = [time.strftime("%Y/%m/%d/%H", time.gmtime(t0 + 3600 * k)) for k in range(-1, 24)]   # час разгона + сутки
    цена_sol: dict = {}          # минт -> (SOL за единицу, слот)
    покупки, счёт = [], {"строк": 0, "файлов": 0, "ошибки": []}
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
                        ам = р_action.search(стр)
                        if not ам:
                            continue
                        qм, мм = р_qmint.search(стр), р_mint.search(стр)
                        if qм and мм and (qм.group(1) == WSOL or мм.group(1) == WSOL):
                            qa, ta, бм = р_qamt.search(стр), р_tamt.search(стр), A.р_block.search(стр)
                            try:
                                qv, tv = float(qa.group(1)), float(ta.group(1))
                            except (AttributeError, ValueError):
                                qv = tv = 0.0
                            if qv > 0 and tv > 0 and бм:
                                if qм.group(1) == WSOL and qv >= МИН_SOL_ЦЕНЫ:
                                    цена_sol[мм.group(1)] = (qv / tv, int(бм.group(1)))
                                elif мм.group(1) == WSOL and tv >= МИН_SOL_ЦЕНЫ:
                                    цена_sol[qм.group(1)] = (tv / qv, int(бм.group(1)))
                        if ам.group(1) != "buy":
                            continue
                        sn = A.р_signer.search(стр)
                        трейдеры = set(A.р_trader.findall(стр)) | ({sn.group(1)} if sn else set())
                        наши = [t for t in трейдеры if t in адр]
                        if not наши:
                            continue
                        try:
                            e = json.loads(стр)
                        except ValueError:
                            continue
                        бд = {b.get("trader"): b for b in e.get("breakdown") or [] if isinstance(b, dict)}
                        for t in наши:
                            b = бд.get(t) or {}
                            q = e.get("quoteMint")
                            ц = (1.0, e.get("block")) if q == WSOL else цена_sol.get(q)
                            покупки.append({"trader": t, "signature": e.get("signature"), "pool": e.get("pool"),
                                            "poolId": e.get("poolId"), "mint": e.get("mint"), "quoteMint": q,
                                            "quoteAmount": float(b.get("quoteAmount") or e.get("quoteAmount") or 0),
                                            "tokenAmount": float(b.get("tokenAmount") or e.get("tokenAmount") or 0),
                                            "block": e.get("block"), "timestamp": e.get("timestamp"),
                                            "цена_q_sol": ц[0] if ц else None, "цена_q_слот": ц[1] if ц else None})
                break
            except Exception as exc:  # noqa: BLE001
                if A.os.environ.get("PODB_ARHIV_KESH"):
                    (Path(A.os.environ["PODB_ARHIV_KESH"]) / f"{ч}.jsonl.zst").unlink(missing_ok=True)
                if попытка == 3:
                    счёт["ошибки"].append(f"{ч}: {type(exc).__name__}: {str(exc)[:100]}")
                else:
                    time.sleep(5 * (попытка + 1))
        print(f"{ч}: строк {счёт['строк']}, покупок групп {len(покупки)}", flush=True)
    ВЫХ.mkdir(parents=True, exist_ok=True)
    out = ВЫХ / f"pokupki_{д}.json.gz"
    out.write_bytes(gzip.compress(json.dumps({"день": д, "часы": часы, "счёт": счёт, "покупки": покупки},
                                             ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6))
    return out


def символ(q: str) -> str:
    return ИЗВЕСТНЫЕ.get(q) or СИМВОЛЫ.get(q) or "другое"


СИМВОЛЫ: dict = {}


def свод() -> int:
    адр = адреса()
    # символы котировок -- из файла 27.09 (DAS getAsset там уже прочитан)
    for x in json.loads((П / "kotirovki_grupp.json").read_text(encoding="utf-8")).get("котировки_группы") or []:
        if x.get("символ"):
            СИМВОЛЫ[x["quote_mint"]] = x["символ"].strip()
    все, дни, ошибки = {}, [], []
    for f in sorted(glob.glob(str(ВЫХ / "pokupki_*.json.gz"))):
        д = json.loads(gzip.decompress(Path(f).read_bytes()))
        дни.append(д["день"])
        ошибки += д["счёт"]["ошибки"]
        for x in д["покупки"]:
            все[(x["signature"], x["trader"], x["poolId"], x["mint"])] = x          # разгон соседних суток -- без повторов
    пп = sorted(все.values(), key=lambda x: (x["block"] or 0, x["timestamp"] or 0))
    # промежуточные ноги: минт этой покупки -- котировка другой ноги того же трейдера в той же транзакции
    котировки_tx = collections.defaultdict(set)
    for x in пп:
        котировки_tx[(x["signature"], x["trader"])].add(x["quoteMint"])
    лево = calendar.timegm(time.strptime("2026-09-28", "%Y-%m-%d")) * 1000
    право = calendar.timegm(time.strptime("2026-10-01", "%Y-%m-%d")) * 1000
    последняя: dict = {}
    сигналы, без_цены = [], 0
    for x in пп:
        if x["mint"] in котировки_tx[(x["signature"], x["trader"])] or x["mint"] in ИЗВЕСТНЫЕ:
            continue                                            # промежуточная нога маршрута; покупка SOL / USDC / USDT -- не токен
        кл = (x["trader"], x["mint"])
        пред = последняя.get(кл)
        по_окну = пред is None or (x["block"] or 0) - пред > ОКНО_ДОКУПКИ
        последняя[кл] = x["block"] or 0
        if not по_окну or not (лево <= (x["timestamp"] or 0) < право):
            continue
        sol = x["quoteAmount"] * x["цена_q_sol"] if x["цена_q_sol"] else None
        if sol is None:
            без_цены += 1
            continue
        if sol >= ПОРОГ_SOL:
            сигналы.append({**x, "sol_экв": round(sol, 4), "группа": адр[x["trader"]]["группа"],
                            "день": time.strftime("%Y-%m-%d", time.gmtime(x["timestamp"] / 1000))})
    сутки = 3                                                   # окно владельца -- трое суток 28–30.09
    итог = {
        "USDC на CPMM или Pump AMM": [с for с in сигналы if с["quoteMint"] == USDC and с["pool"] in ДВУХШАГОВЫЕ],
        "USDC на других типах": [с for с in сигналы if с["quoteMint"] == USDC and с["pool"] not in ДВУХШАГОВЫЕ],
        "GP / CRACKER всего": [с for с in сигналы if символ(с["quoteMint"]) in ("GP", "CRACKER")],
    }
    по_дням = lambda L: ", ".join(f"{д[5:]}: {sum(1 for с in L if с['день'] == д)}" for д in ("2026-09-28", "2026-09-29", "2026-09-30"))  # noqa: E731
    md = ["# Котировки денежных групп 28–30.09 (архив PumpApi)", "",
          f"Адреса групп: lane_s0 10, batch5 12, leader 1 (data/sources_2026-09-25.json ветки Code-1, копия 28.09) и cand1 9 "
          f"(data/gruppa_cand1.json, применено 30.09 21:02Z; считается за все трое суток). Окно 28.09 00:00Z → 30.09 24:00Z; "
          f"сутки в архиве: {', '.join(дни)}. Сигнал -- правило pyg7: первая покупка пары адрес + минт (нет покупки того же "
          "минта этим адресом в предыдущие 1800 слотов, любого размера), от 2 SOL-экв.; промежуточные ноги маршрута "
          "(SOL → USDC → токен: покупка USDC) и покупки самих WSOL / USDC / USDT не считаются. SOL-экв. -- quoteAmount × цена котировки в SOL по последней сделке "
          "архива пары котировка / WSOL. Тип пула -- по архиву PumpApi (Orca Whirlpool, Raydium AMM v4 и редких программ в "
          "архиве нет; 27.09 это ~2 % не-SOL покупок групп). Сбор -- analysis/podbivka_kotirovki_arhiv.py (бегунок lab-miami, "
          "без ключей), данные -- data/podbivka/kotirovki_grupp_2026-10-01.json. Ничего не рекомендуется.", "",
          "## Итог (порог владельца: ≥ 1 в сутки USDC на других типах -- строим склейку)", "",
          "| строка | за 3 суток | в сутки | по суткам | по группам |", "|---|---|---|---|---|"]
    for имя, L in итог.items():
        гр = collections.Counter(с["группа"] for с in L)
        md.append(f"| {имя} | {len(L)} | {len(L) / сутки:.1f} | {по_дням(L)} | {', '.join(f'{g} {n}' for g, n in гр.most_common()) or '—'} |")
    n_др = len(итог["USDC на других типах"])
    md += ["", f"**Порог владельца:** USDC на других типах -- {n_др / сутки:.1f} в сутки "
           + ("(≥ 1) -- склейку строим." if n_др / сутки >= 1 else "(< 1) -- склейку не строим.")]
    md += ["", f"Сигналов всего (все котировки): {len(сигналы)} ({len(сигналы) / сутки:.1f} в сутки); из них не WSOL: "
           f"{sum(1 for с in сигналы if с['quoteMint'] != WSOL)}. Покупок без цены котировки в SOL (нет пары с WSOL в архиве к "
           f"этому моменту) -- не оценены: {без_цены}.", "",
           "## Котировка × тип пула × группа", "", "| котировка | минт | тип пула | группа | n за 3 суток | в сутки |",
           "|---|---|---|---|---|---|"]
    кл3 = collections.Counter((символ(с["quoteMint"]), с["quoteMint"], с["pool"], с["группа"]) for с in сигналы)
    for (с_, q, p, g), n in sorted(кл3.items(), key=lambda kv: (kv[0][1] == WSOL, -kv[1])):
        md.append(f"| {с_} | `{q}` | {p} | {g} | {n} | {n / сутки:.1f} |")
    md += ["", "## Не WSOL: по источникам", "", "| котировка | тип пула | группа | источник | n |", "|---|---|---|---|---|"]
    кл4 = collections.Counter((символ(с["quoteMint"]), с["pool"], с["группа"], с["trader"]) for с in сигналы if с["quoteMint"] != WSOL)
    for (с_, p, g, t), n in sorted(кл4.items(), key=lambda kv: -kv[1]):
        md.append(f"| {с_} | {p} | {g} | `{t[:8]}` {адр[t]['имя'] if адр[t]['имя'] != t[:8] else ''} | {n} |")
    if ошибки:
        md += ["", "Ошибки чтения часов: " + "; ".join(ошибки)]
    (КОРЕНЬ / "docs" / "podbivka_2026-10-01_kotirovki_grupp.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (П / "kotirovki_grupp_2026-10-01.json").write_text(json.dumps(
        {"окно": ["2026-09-28T00:00Z", "2026-10-01T00:00Z"], "сутки_в_архиве": дни, "правило": "pyg7: окно 1800 слотов, от 2 SOL-экв.",
         "итог": {k: len(v) for k, v in итог.items()}, "без_цены": без_цены, "сигналы": сигналы}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print("\n".join(md[:20]))
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    г = р.add_mutually_exclusive_group(required=True)
    г.add_argument("--den")
    г.add_argument("--svod", action="store_true")
    а = р.parse_args()
    if а.svod:
        return свод()
    out = день(а.den)
    import podbivka_run as R  # noqa: PLC0415
    R.записано(out)
    R.пуш(f"Podbivka-2: kotirovki grupp {а.den} [automated]", [str(out)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
