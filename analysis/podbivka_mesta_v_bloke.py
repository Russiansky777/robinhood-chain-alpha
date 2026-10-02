#!/usr/bin/env python3
"""Место в блоке для списка подписей: getBlock с transactionDetails=signatures. Только чтение.

Вход: json вида {"слоты": {"<слот>": ["подпись", ...], ...}} (data/podbivka/<--vhod>).
Выход: data/podbivka/<--out> -- {"<слот>": {"всего": N, "места": {"подпись": индекс}}}.
Зачем: в архиве PumpApi места в блоке нет, а вопрос «кто раньше внутри слота» решается только им.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

П = Path(__file__).resolve().parent.parent / "data" / "podbivka"
ВЕРСИЯ_TX = 1


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    import podbivka_sim as S  # noqa: PLC0415
    р = argparse.ArgumentParser()
    р.add_argument("--vhod", required=True)
    р.add_argument("--out", required=True)
    а = р.parse_args()
    вх = json.loads((П / а.vhod).read_text(encoding="utf-8"))
    слоты = вх["слоты"] if isinstance(вх, dict) and "слоты" in вх else вх
    уз = S.Узел()
    из_, счёт = {}, {"слотов": 0, "не_отдано": 0, "подписей_найдено": 0, "подписей_просили": 0}
    with уз.на("helius"):
        for сл, подписи in слоты.items():
            б = уз.вызов("getBlock", [int(сл), {"transactionDetails": "signatures", "rewards": False,
                                                "maxSupportedTransactionVersion": ВЕРСИЯ_TX}], срок=90.0)
            счёт["слотов"] += 1
            сп = (б or {}).get("signatures") or []
            if not сп:
                счёт["не_отдано"] += 1
                continue
            места = {s: и for и, s in enumerate(сп)}
            счёт["подписей_просили"] += len(подписи)
            нашли = {п: места[п] for п in подписи if п in места}
            счёт["подписей_найдено"] += len(нашли)
            из_[str(сл)] = {"всего": len(сп), "места": нашли}
    out = П / а.out
    out.write_text(json.dumps({"счёт": счёт, "слоты": из_, "расход": уз.расход()},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(out)
    print(f"{out.name}: слотов {счёт['слотов']}, не отдано {счёт['не_отдано']}, подписей найдено "
          f"{счёт['подписей_найдено']} из {счёт['подписей_просили']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
