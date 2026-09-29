#!/usr/bin/env python3
"""Подбивка: 54uaRuJE, источник на слот раньше -- индекс источника в полном блоке (v1 включительно).

Сбор 28.09 читал блоки без транзакций v1 -- индекс источника мог быть занижен.
Вход: data/podbivka/snaipery/54ua_cu.json (записи со слотов_до ≥ 1). Только чтение.
Выход: data/podbivka/snaipery/54ua_s1.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka" / "snaipery"


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    З = [з for з in json.loads((П / "54ua_cu.json").read_text(encoding="utf-8"))["записи"] if з["слотов_до"] >= 1]
    уз = S.Узел()
    рез = []
    for з in З:
        with уз.на("helius"):
            try:
                б = уз.вызов("getBlock", [з["слот_источника"], {"transactionDetails": "signatures", "rewards": False,
                                                              "maxSupportedTransactionVersion": 1,
                                                              "commitment": "confirmed"}], срок=40.0)
                с = (б or {}).get("signatures") or []
                рез.append({"signature": з["signature"], "подпись_источника": з["подпись_источника"],
                            "индекс_источника": с.index(з["подпись_источника"]) if з["подпись_источника"] in с else None,
                            "в_блоке": len(с)})
            except RuntimeError as exc:
                рез.append({"signature": з["signature"], "why_not": S.чисто(str(exc))[:160]})
    вых = П / "54ua_s1.json"
    вых.write_text(json.dumps({"записи": рез, "расход": уз.расход()}, ensure_ascii=False, default=str), encoding="utf-8")
    R.записано(вых)
    R.пуш("Podbivka-2: 54uaRuJE indeks istochnika pri S+1 po polnomu bloku [automated]", [str(вых)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
