#!/usr/bin/env python3
"""Образцы покупок с котировкой USDC -- для Code-3. Офлайн, по готовому прогону цепи.

ЗАЧЕМ. В прогоне лидера по цепи 03.10 все 286 покупок с котировкой USDC остались БЕЗ
размера в SOL-экв: `podbivka_rezhim2.цена_котировочного` вернула «в сделке нет плеча
котировочного к WSOL/USD». У стейблов эта функция должна идти другим путём --
1 USD за единицу по курсу опорного пула SOL/USDC (ветка `q in (USDC, USDT)`), но она
включается только когда курс опорного пула получен; в этом прогоне он не получился.
Здесь собраны сами сделки с сырыми числами, чтобы Code-3 проверил перевод USDC -> SOL
на настоящих данных, а не на выдуманных.

Выход: docs/dlja_code2_usdc_obrazcy_<дата>.md и data/podbivka/usdc_obrazcy_<дата>.json.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
СТЕЙБЛЫ = {USDC: "USDC", USDT: "USDT"}


def utc(ts) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else "—"


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    р = argparse.ArgumentParser()
    р.add_argument("--metka", default="2026-10-03")
    р.add_argument("--obrazcov", type=int, default=20)
    а = р.parse_args()
    д = json.loads((П / "lider_cep_pokupki.json").read_text(encoding="utf-8"))
    пок = д["покупки"]
    курсы = д.get("курсы_котировочных") or {}
    стейбл = [x for x in пок if x.get("quote_mint") in СТЕЙБЛЫ]
    с_числом = [x for x in стейбл if x.get("sol_экв") is not None]
    прог = collections.Counter(x["программа"] for x in стейбл)
    # для сравнения -- котировки, где курс получился
    получились = {q: о for q, о in курсы.items() if q not in СТЕЙБЛЫ
                  and "нет плеча" not in о and "нет" not in о.split(":")[0]}
    обр = sorted(стейбл, key=lambda x: -(x.get("quote_raw") or 0))[:а.obrazcov]
    прочие = [x for x in пок if x.get("quote_mint") in получились and x.get("sol_экв") is not None]
    прочие = sorted(прочие, key=lambda x: -(x.get("sol_экв") or 0))[:5]

    дт = {"метка": а.metka, "откуда": "data/podbivka/lider_cep_pokupki.json (прогон цепи 03.10)",
          "адрес": д.get("адрес"), "окно": {"с": д.get("с_utc"), "до": д.get("до_utc")},
          "покупок_всего": len(пок), "со_стейблом": len(стейбл), "из_них_с_размером": len(с_числом),
          "программы_стейбла": dict(прог),
          "курс_котировочного_сказал": {q: курсы.get(q) for q in СТЕЙБЛЫ if q in курсы},
          "образцы": обр, "образцы_где_курс_получился": прочие,
          "курсы_котировочных_все": курсы}
    out_j = П / f"usdc_obrazcy_{а.metka}.json"
    out_j.write_text(json.dumps(дт, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(out_j)

    md = [f"# Образцы покупок с котировкой USDC -- для Code-3 ({а.metka})", "",
          f"Источник чисел: прогон цепи по лидеру `{(д.get('адрес') or '')[:8]}` "
          f"({д.get('с_utc')} -- {д.get('до_utc')}), файл `data/podbivka/lider_cep_pokupki.json`. "
          f"Покупок всего **{len(пок)}**, из них с котировкой USDC/USDT **{len(стейбл)}**, и у "
          f"**{len(с_числом)}** из них есть размер в SOL-экв.", "",
          "## Что именно не сошлось", "",
          "`podbivka_rezhim2.цена_котировочного` на этих сделках вернула: "
          + "; ".join(f"**{СТЕЙБЛЫ[q]}** -- «{курсы[q]}»" for q in СТЕЙБЛЫ if q in курсы)
          + ". У стейбла в этой функции есть своя ветка -- `1e-6 / курс * 1e9` при "
            "`q in (USDC, USDT)`, то есть 1 USD за единицу по курсу опорного пула SOL/USDC "
            "(`podbivka_sim.КурсПулом.rate_for`). Она не включилась: курс опорного пула на эти "
            "сделки не получен, и функция вышла раньше с причиной от `podbivka_lider_kotirovka."
            "цена_q_в_sol`. Поэтому размер покупки в SOL-экв у всех USDC-сделок пуст.", "",
          f"Программы пулов у USDC-сделок: " + ", ".join(f"{k} {v}" for k, v in прог.most_common())
          + ".", "",
          "## Образцы (по убыванию суммы USDC)", "",
          "| подпись | слот | UTC | минт | программа пула | котировка | сырое (6 знаков) | "
          "в USDC | размер SOL-экв |", "|---|---|---|---|---|---|---|---|---|"]
    for x in обр:
        сыр = x.get("quote_raw")
        md.append(f"| `{x['signature'][:16]}…` | {x.get('slot')} | {utc(x.get('blockTime'))} | "
                  f"`{(x.get('mint') or '')[:8]}` | {x.get('программа')} | "
                  f"{СТЕЙБЛЫ.get(x.get('quote_mint'), '?')} | {сыр if сыр is not None else '—'} | "
                  + (f"{сыр / 1e6:.6f}" if сыр is not None else "—") + " | "
                  + (f"{x['sol_экв']:.6f}" if x.get("sol_экв") is not None else "**нет**") + " |")
    md += ["", "## Для сравнения: котировки, где курс получился", "",
           "| подпись | минт | котировка | откуда курс | сырое | размер SOL-экв |",
           "|---|---|---|---|---|---|"]
    for x in прочие:
        q = x.get("quote_mint")
        md.append(f"| `{x['signature'][:16]}…` | `{(x.get('mint') or '')[:8]}` | `{(q or '')[:8]}` | "
                  f"{курсы.get(q)} | {x.get('quote_raw')} | {x['sol_экв']:.6f} |")
    md += ["", "## Что с этим делать (числа, без рекомендаций)", "",
           "1. У стейбла курс не нужен «из сделки»: 1 USDC = 1 USD, и достаточно курса SOL/USD "
           "на то же время. В коде эта ветка есть, но стоит ПОСЛЕ попытки взять плечо из сделки "
           "и работает только при полученном курсе опорного пула.",
           "2. Курс опорного пула на эти сделки не получен -- это и есть место для проверки "
           "Code-3: `podbivka_sim.КурсПулом.rate_for` (для времени после 24.09 он идёт за курсом "
           "детектора GeckoTerminal из признака жизни, не дальше 12 часов, иначе курс сделки "
           "или медиана часа).",
           f"3. Пока размера нет, эти {len(стейбл)} покупок не попадают ни в порог 2 SOL-экв, ни "
           "в корзины размера, ни в модель -- в таблицах они стоят как «размер не прочитан».", ""]
    out_m = КОРЕНЬ / "docs" / f"dlja_code2_usdc_obrazcy_{а.metka}.md"
    out_m.write_text("\n".join(md) + "\n", encoding="utf-8")
    R.записано(out_m)
    print(out_m.name, "образцов", len(обр), "со стейблом", len(стейбл), "с размером", len(с_числом),
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
