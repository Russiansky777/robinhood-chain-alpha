#!/usr/bin/env python3
"""Две сделки 0.3 SOL (lane_s0, 30.09 02:37Z и 02:48Z): чужие продажи того же минта в окне удержания. Офлайн.

Вход: data/podbivka/dve_sdelki_vhod.json (строки data/sdelki_polosy_2026-09-30.json Code-1) и
data/podbivka/dve_sdelki_tx.json.gz (analysis/podbivka_tx_po_podpisyam.py --okno: все успешные транзакции минта и
хранилища пула от слота нашей покупки до слота нашей продажи +2). Продажа -- транзакция, где у подписанта (не нашего
кошелька) баланс минта уменьшился; SOL -- изменение его лампортов + WSOL (с комиссией транзакции, если платил он).
Выход: docs/podbivka_2026-09-30_dve_sdelki.md.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
WSOL = "So11111111111111111111111111111111111111112"
НАШ = "4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x"


def ключи(т: dict) -> list:
    msg = т["transaction"]["message"]
    к = [x["pubkey"] if isinstance(x, dict) else x for x in msg["accountKeys"]]
    la = т["meta"].get("loadedAddresses") or {}
    return к + (la.get("writable") or []) + (la.get("readonly") or [])


def подписанты(т: dict) -> list:
    return [x["pubkey"] for x in т["transaction"]["message"]["accountKeys"] if isinstance(x, dict) and x.get("signer")]


def дельта(т: dict, владелец: str, минт: str) -> int:
    м = т["meta"]
    d = 0
    for знак, сп in ((-1, м.get("preTokenBalances") or []), (1, м.get("postTokenBalances") or [])):
        for b in сп:
            if b.get("owner") == владелец and b["mint"] == минт:
                d += знак * int(b["uiTokenAmount"]["amount"])
    return d


def main() -> int:
    вход = json.loads((КОРЕНЬ / "data" / "podbivka" / "dve_sdelki_vhod.json").read_text(encoding="utf-8"))["сделки"]
    д = json.loads(gzip.decompress((КОРЕНЬ / "data" / "podbivka" / "dve_sdelki_tx.json.gz").read_bytes()))
    txs, окна = д["транзакции"], д["окна"]
    md = ["# Две сделки 0.3 SOL (lane_s0, 30.09): чужие продажи того же минта в окне удержания", "",
          "Транзакции минта и хранилища пула от слота нашей покупки до слота нашей продажи (+2), прочитаны по цепи "
          "(analysis/podbivka_tx_po_podpisyam.py --okno), разбор -- analysis/podbivka_dve_sdelki.py. Продажа -- баланс "
          "минта у подписанта уменьшился; SOL -- изменение его лампортов + WSOL. Слоты -- от слота нашей покупки.", ""]
    for с in вход:
        о = окна.get(с["mint"]) or {}
        if о.get("why_not"):
            md += [f"## `{с['mint'][:8]}`: {о['why_not']}", ""]
            continue
        s0, s1 = о["slot_покупки"], о["slot_продажи"]
        продажи = []
        for подп, т in txs.items():
            if not т or подп in (с["buy_sig"], с["sell_sig"]) or not (s0 <= т["slot"] <= s1 + 2):
                continue
            кл = ключи(т)
            for кто in подписанты(т):
                if кто == НАШ:
                    continue
                dt = дельта(т, кто, с["mint"])
                if dt >= 0:
                    continue
                и = кл.index(кто)
                sol = (т["meta"]["postBalances"][и] - т["meta"]["preBalances"][и] + дельта(т, кто, WSOL)) / 1e9
                продажи.append({"slot": т["slot"], "от_покупки": т["slot"] - s0, "кто": кто, "токенов": -dt, "sol": round(sol, 6),
                                "подпись": подп, "после_нашей_продажи": т["slot"] > s1})
                break
        продажи.sort(key=lambda x: (x["slot"], x["подпись"]))
        в_окне = [x for x in продажи if not x["после_нашей_продажи"]]
        md += [f"## `{с['mint'][:8]}…` (источник {с['source'][:8]}, наша покупка `{с['buy_sig'][:8]}`)", "",
               f"Наша покупка -- слот {s0} (слот источника {с['source_slot']}); наша продажа -- слот {s1}, "
               f"**+{s1 - s0} слотов** от покупки (план {int(с['hold_slots_plan'])}, у Code-1 факт {с['hold_slots_fact']}). "
               f"Прочитано транзакций в окне: {о['подписей']} (упавших {о['упавших']}, не читались).", "",
               f"Чужих продаж минта от покупки до нашей продажи: **{len(в_окне)}**, на **{round(sum(x['sol'] for x in в_окне), 6)} SOL**; "
               + (f"первая -- слот +{в_окне[0]['от_покупки']}." if в_окне else "первой нет.") +
               f" У Code-1 в строке: {с['chuzhih_prodazh']} продаж, {с['chuzhie_prodazhi_sol']} SOL.", ""]
        сум = lambda xs: f"{len(xs)} / {round(sum(x['sol'] for x in xs), 3)} SOL"  # noqa: E731
        позже = [x for x in в_окне if x["от_покупки"] > 0]
        md += ["| часть окна | продаж / SOL |", "|---|---|",
               f"| слот нашей покупки (+0; порядок внутри слота по этим данным неизвестен -- часть могла быть до нас) | {сум([x for x in в_окне if x['от_покупки'] == 0])} |",
               f"| +1 … наша продажа | {сум(позже)} |",
               f"| первая после слота покупки | {'+' + str(позже[0]['от_покупки']) if позже else '—'} |",
               f"| из всех в окне -- SOL у продавца уменьшился (продажа не за SOL / маршрут) | {sum(1 for x in в_окне if x['sol'] < 0)} |",
               f"| после нашей продажи (до +2) | {сум([x for x in продажи if x['после_нашей_продажи']])} |", ""]
        if продажи:
            md += ["| слот от покупки | кто | токенов | SOL | подпись | после нашей продажи |", "|---|---|---|---|---|---|"]
            md += [f"| +{x['от_покупки']} | {x['кто'][:8]} | {x['токенов']} | {x['sol']} | `{x['подпись'][:8]}` | "
                   f"{'да' if x['после_нашей_продажи'] else 'нет'} |" for x in продажи]
            md.append("")
    (КОРЕНЬ / "docs" / "podbivka_2026-09-30_dve_sdelki.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
