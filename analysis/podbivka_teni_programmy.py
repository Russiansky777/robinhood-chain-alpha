#!/usr/bin/env python3
"""Тени Code-1 «тип пула не покрыт сборщиком: ojh19oja… / TessVdML…»: программа в транзакции источника -- пул токена
или нога маршрута? Офлайн, по уже прочитанным транзакциям (data/podbivka/teni_tx.json.gz,
analysis/podbivka_tx_po_podpisyam.py --teni) и строкам Code-1 (data/podbivka/teni_vhod.json).

По транзакции:
  * хранилище токена сигнала -- токен-счёт минта сигнала, владелец которого не источник, с изменившимся балансом;
  * программы пула токена -- самые глубокие (stackHeight) инструкции не Token / Token-2022 / ATA / System / Compute
    Budget, в счетах которой есть это хранилище;
  * ojh1 / Tess -- «пул токена», если хранилище токена есть в счетах её вызова; иначе «нога маршрута»: пара минтов
    токен-счетов её вызова.
«Умеем» -- программа есть в списке сборщика Code-1 (vps_health_nl.txt ветки Code-1, passed_in_row_by_pool, 30.09);
«порт Code-2» -- есть модуль котировки / файл для Code-3 на ветке claude/podbivka.
Имена программ -- data/solana_buyer_200/prior/current/buyer_100/dex_labels.json.
Выход: docs/podbivka_2026-09-30_teni_ojh1_tess.md, data/podbivka/teni_razbor.json.
"""
from __future__ import annotations

import collections
import gzip
import json
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
СЛУЖЕБНЫЕ = {"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
             "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL", "11111111111111111111111111111111",
             "ComputeBudget111111111111111111111111111111", "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
             "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo"}
УМЕЕМ_CODE1 = {"6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P", "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",
               "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo", "gatorLx9aC1e5ZWAXscv5QRKiLXnLPLXjftVc81h1Hr",
               "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C", "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
               "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG", "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
               "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"}
ПОРТ_CODE2 = {"LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "DLMM", "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "CLMM",
              "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Whirlpool", "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "DBC",
              "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "AMM v4 (файл, без порта)"}
ЦЕЛИ = {"ojh19ojaKduoJZuaJADhcVGp4xt1TcdAvZmpVsCorch": "ojh1 (Scorch)", "TessVdML9pBGgG9yGks7o4HewRaXVAMuoVj4x83GLQH": "Tess (Tessera V)"}
КОРОТКО = {"So11111111111111111111111111111111111111112": "WSOL", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
           "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT"}


def ключи(т: dict) -> list:
    msg = т["transaction"]["message"]
    к = [x["pubkey"] if isinstance(x, dict) else x for x in msg["accountKeys"]]
    la = т["meta"].get("loadedAddresses") or {}
    return к + (la.get("writable") or []) + (la.get("readonly") or [])


def разбор(т: dict, строка: dict) -> dict:
    кл = ключи(т)
    meta = т["meta"]
    токены: dict = {}
    for b in (meta.get("preTokenBalances") or []):
        токены.setdefault(кл[b["accountIndex"]], {}).update(mint=b["mint"], owner=b.get("owner"), pre=int(b["uiTokenAmount"]["amount"]))
    for b in (meta.get("postTokenBalances") or []):
        токены.setdefault(кл[b["accountIndex"]], {}).update(mint=b["mint"], owner=b.get("owner"), post=int(b["uiTokenAmount"]["amount"]))
    ист, минт = строка["istochnik"], строка["mint"]
    хран = {a for a, v in токены.items() if v.get("mint") == минт and v.get("owner") != ист
            and v.get("pre", 0) != v.get("post", 0)}
    ixs = []
    for k, ix in enumerate(т["transaction"]["message"]["instructions"]):
        ixs.append((1, f"{k}", ix))
    for g in meta.get("innerInstructions") or []:
        for j, ix in enumerate(g["instructions"]):
            ixs.append((ix.get("stackHeight") or 2, f"{g['index']}.{j}", ix))
    пулы_токена = []
    for глубина, место, ix in ixs:
        p = ix.get("programId")
        if p in СЛУЖЕБНЫЕ or not (set(ix.get("accounts") or []) & хран):
            continue
        пулы_токена.append((глубина, место, p))
    г = max((x[0] for x in пулы_токена), default=None)
    пулы = sorted({x[2] for x in пулы_токена if x[0] == г})
    цели = []
    for глубина, место, ix in ixs:
        p = ix.get("programId")
        if p not in ЦЕЛИ:
            continue
        минты = sorted({КОРОТКО.get(токены[a]["mint"], токены[a]["mint"][:6]) for a in ix.get("accounts") or [] if a in токены})
        цели.append({"программа": ЦЕЛИ[p], "место": место, "глубина": глубина,
                     "роль": "пул токена" if set(ix.get("accounts") or []) & хран else "нога маршрута", "минты": минты})
    внешние = [ix.get("programId") for ix in т["transaction"]["message"]["instructions"] if ix.get("programId") not in СЛУЖЕБНЫЕ]
    дельта_ист = {}
    for a, v in токены.items():
        if v.get("owner") == ист and v.get("pre", 0) != v.get("post", 0):
            м = КОРОТКО.get(v["mint"], "токен" if v["mint"] == минт else v["mint"][:6])
            дельта_ист[м] = дельта_ист.get(м, 0) + v.get("post", 0) - v.get("pre", 0)
    return {"err": meta.get("err") is not None, "хранилищ_токена": len(хран), "пулы_токена": пулы, "цели": цели,
            "внешние": внешние, "дельта_источника": дельта_ист}


def main() -> int:
    имена = json.loads((КОРЕНЬ / "data" / "solana_buyer_200" / "prior" / "current" / "buyer_100" / "dex_labels.json").read_text(encoding="utf-8"))
    строки = json.loads((КОРЕНЬ / "data" / "podbivka" / "teni_vhod.json").read_text(encoding="utf-8"))["строки"]
    txs = json.loads(gzip.decompress((КОРЕНЬ / "data" / "podbivka" / "teni_tx.json.gz").read_bytes()))["транзакции"]
    ряды = []
    for с in строки:
        т = txs.get(с["podpis"])
        р = {"podpis": с["podpis"], "программа_Code1": ЦЕЛИ.get(с["programma"]), "котировка_Code1": КОРОТКО.get(с["kotirovka"], с["kotirovka"][:6]),
             "mint": с["mint"], "istochnik": с["istochnik"], "gruppa": с["gruppa"]}
        р.update(разбор(т, с) if т else {"why_not": "транзакция не прочитана"})
        ряды.append(р)
    (КОРЕНЬ / "data" / "podbivka" / "teni_razbor.json").write_text(json.dumps(ряды, ensure_ascii=False, indent=1), encoding="utf-8")
    имя = lambda p: (имена.get(p) or (p[:8] + "…")) if p else "не найден"  # noqa: E731
    md = ["# Тени «тип пула не покрыт сборщиком»: ojh19oja… (Scorch) и TessVdML… (Tessera V) в транзакциях источников", "",
          f"Строк Code-1: {len(строки)} (data/teni_ne_sobrany_po_programmam.json ветки Code-1, с 24.09; копия "
          "data/podbivka/teni_vhod.json). Транзакции прочитаны по подписям (analysis/podbivka_tx_po_podpisyam.py --teni), "
          "разбор -- analysis/podbivka_teni_programmy.py. Хранилище токена -- токен-счёт минта сигнала не у источника с "
          "изменившимся балансом; программа пула токена -- самая глубокая не служебная инструкция с этим хранилищем. "
          "«Умеем» -- программа есть в сборщике Code-1 (vps_health_nl.txt, 30.09); «порт Code-2» -- модуль котировки или "
          "файл для Code-3 на ветке claude/podbivka.", ""]
    нет = [р for р in ряды if р.get("why_not")]
    if нет:
        md += [f"Не прочитано транзакций: {len(нет)}.", ""]
    ок = [р for р in ряды if not р.get("why_not")]
    for цель in ЦЕЛИ.values():
        рр = [р for р in ок if р["программа_Code1"] == цель]
        роли = collections.Counter(("пул токена" if any(ц["роль"] == "пул токена" for ц in р["цели"] if ц["программа"] == цель)
                                    else "нога маршрута" if any(ц["программа"] == цель for ц in р["цели"]) else "вызова нет")
                                   for р in рр)
        md += [f"## {цель}: строк {len(рр)}", "",
               "| роль программы в транзакции источника | сигналов |", "|---|---|"] + [f"| {к} | {v} |" for к, v in роли.most_common()]
        ноги = [р for р in рр if not any(ц["роль"] == "пул токена" for ц in р["цели"] if ц["программа"] == цель)]
        пары = collections.Counter(" / ".join("↔".join(ц["минты"]) for ц in р["цели"] if ц["программа"] == цель) for р in ноги)
        md += ["", "Нога маршрута -- пары минтов её вызова: " + (", ".join(f"{к or '—'} {v}" for к, v in пары.most_common(8)) or "нет"), "",
               "| программа пула токена | сигналов | умеем (Code-1) | порт Code-2 |", "|---|---|---|---|"]
        пулы = collections.Counter(p for р in ноги for p in (р["пулы_токена"] or [None]))
        for p, n in пулы.most_common():
            md.append(f"| {имя(p)} (`{(p or '')[:8]}`) | {n} | {'да' if p in УМЕЕМ_CODE1 else 'нет'} | {ПОРТ_CODE2.get(p, '—')} |")
        кот = collections.Counter((р["котировка_Code1"], "пул" if р not in ноги else "нога") for р in рр)
        md += ["", "Котировка по журналу Code-1 × роль: " + ", ".join(f"{к[0]} ({к[1]}) {v}" for к, v in кот.most_common()), ""]
    (КОРЕНЬ / "docs" / "podbivka_2026-09-30_teni_ojh1_tess.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
