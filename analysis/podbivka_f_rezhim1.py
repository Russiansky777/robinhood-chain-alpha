#!/usr/bin/env python3
"""Подбивка: f режима 1 (fee_factor симулятора -- трата в счета инструкции пула)
против f по хранилищам на тех же сделках источника Pump AMM. Только чтение.

Режим 1 продаёт в рамке X = x/f с g, калиброванным в той же рамке, поэтому f
в продаже сокращается; результат зависит от f только через число наших
токенов. Если f режима 1 ≈ f по хранилищам (которое сходится с фактом наших
покупок, сверка v6), режим 1 пересчётом g режима 2 не затронут.

Вход: подписи источников -- ночные сделки полосы (сверка v6) и выборка покупок
Pump AMM из data/podbivka/pumpapi/celi.json. Выход: data/podbivka/f_rezhim1.json.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import podbivka as P  # noqa: E402
import podbivka_a3 as A3  # noqa: E402
import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
ВЫБОРКА = 150


def main() -> int:
    уз = S.Узел()
    цели = []
    св = json.loads((КОРЕНЬ / "data" / "podbivka" / "sverka_leader_noch_2026-09-28_v6.json").read_text(encoding="utf-8"))
    for x in св["ряды"]:
        if x.get("src_sig") and x.get("f"):
            цели.append({"signature": x["src_sig"], "wallet": x.get("источник"), "mint": x["mint"], "откуда": "ночь",
                         "blockTime": None})
    ц = json.loads((КОРЕНЬ / "data" / "podbivka" / "pumpapi" / "celi.json").read_text(encoding="utf-8"))["цели"]
    п = sorted((s, x) for s, x in ц.items() if (x.get("программа") or "").startswith("pAMM") and x.get("wallet"))
    шаг = max(1, len(п) // ВЫБОРКА)
    for s, x in п[::шаг][:ВЫБОРКА]:
        цели.append({"signature": s, "wallet": x["wallet"], "mint": x["mint"], "откуда": "25.09", "blockTime": x.get("blockTime")})
    рез = []
    for ц_ in цели:
        with уз.на(S.узел_по_времени(ц_["blockTime"]) if ц_["blockTime"] else "shyft"):
            try:
                tx = уз.tx(ц_["signature"])
            except RuntimeError as exc:
                рез.append({**ц_, "why_not": S.чисто(str(exc))[:120]})
                continue
        if not tx:
            рез.append({**ц_, "why_not": "нет транзакции"})
            continue
        кош = ц_["wallet"] or (C.signers(tx) or [None])[0]
        сим = P.наша_покупка_по_симулятору(tx, кош, ц_["mint"], 500_000_000)
        пул = C.identify_pool(tx, кош, ц_["mint"])
        fх = A3.f_по_хранилищам(tx, пул) if пул.get("pool_vault") and пул.get("quote_vault") else None
        рез.append({**ц_, "program": сим.get("program"), "f1": сим.get("fee_factor"), "fх": fх,
                    "why_not": None if сим.get("fee_factor") and fх else (сим.get("why_not") or "f не считается")})
        print(ц_["откуда"], ц_["signature"][:8], сим.get("fee_factor"), fх, flush=True)
    ок = [r for r in рез if not r.get("why_not") and str(r.get("program", "")).startswith("pAMM")]
    отн = [r["f1"] / r["fх"] for r in ок]
    итог = {"n": len(ок), "f1_к_fх_медиана": statistics.median(отн) if отн else None,
            "p10": sorted(отн)[int(0.1 * len(отн))] if отн else None, "p90": sorted(отн)[int(0.9 * len(отн))] if отн else None,
            "f1_медиана": statistics.median(r["f1"] for r in ок) if ок else None,
            "fх_медиана": statistics.median(r["fх"] for r in ок) if ок else None}
    out = КОРЕНЬ / "data" / "podbivka" / "f_rezhim1.json"
    out.write_text(json.dumps({"итог": итог, "ряды": рез, "расход": уз.расход()}, ensure_ascii=False, indent=1), encoding="utf-8")
    import podbivka_run as R  # noqa: PLC0415
    R.записано(out)
    print(json.dumps(итог, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
