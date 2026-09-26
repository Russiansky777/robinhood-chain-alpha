#!/usr/bin/env python3
"""Подбивка: глубина истории RPC Shyft -- по одной нашей подписи на день.

Проба 26.09 15:1xZ: транзакцию 18.09 узел не отдал ("result пуст"), блок
того слота -- "missing in long-term storage". Окно 1б начинается 18.09, п.2-3
берут 7 дней: без ответа на вопрос о глубине считать нельзя.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka as P  # noqa: E402
import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent


def main() -> int:
    уз = S.Узел()
    tr = json.loads((КОРЕНЬ / "data" / "solana_trades_all.json").read_text(encoding="utf-8"))
    по_дню: dict = {}
    for т in tr:
        bt = т.get("buy_block_time")
        if bt and т.get("buy_signature"):
            по_дню.setdefault(time.strftime("%Y-%m-%d %H", time.gmtime(bt))[:10], []).append(т)
    итог: dict = {"дни": {}}
    try:
        итог["getFirstAvailableBlock"] = уз.вызов("getFirstAvailableBlock", [])
        итог["getSlot"] = уз.вызов("getSlot", [])
    except RuntimeError as exc:
        итог["ошибка"] = S.чисто(str(exc))
    for день in sorted(по_дню):
        сделки = sorted(по_дню[день], key=lambda т: т["buy_block_time"])
        проб = [сделки[0], сделки[len(сделки) // 2], сделки[-1]]
        отв = []
        for т in проб:
            try:
                tx = уз.вызов("getTransaction", [т["buy_signature"], P.ОПЦИИ_TX], срок=40.0)
                отв.append({"utc": S.utc(т["buy_block_time"]), "ok": bool(tx), "slot": (tx or {}).get("slot")})
            except RuntimeError as exc:
                отв.append({"utc": S.utc(т["buy_block_time"]), "ok": False, "ошибка": S.чисто(str(exc))[:120]})
        итог["дни"][день] = отв
    # before -- чужая подпись, свежая (сегодняшняя сделка), адрес -- Brez.
    свежая = max((т for т in tr if т.get("buy_block_time")), key=lambda т: т["buy_block_time"])
    try:
        стр = уз.подписи("Fvkc2thk1YcAASdR2gi8uf9n67JW9Dqqr9iRd99MDhoB", до=свежая["buy_signature"], limit=3)
        итог["before_чужая_свежая"] = {"штук": len(стр), "слоты": [з.get("slot") for з in стр]}
    except RuntimeError as exc:
        итог["before_чужая_свежая"] = {"ошибка": S.чисто(str(exc))[:120]}
    # Историю подписей адреса узел отдаёт глубже транзакций? Листаем Brez до 18.09.
    до, страниц, старейшая = None, 0, None
    while страниц < 5:
        стр = уз.подписи("Fvkc2thk1YcAASdR2gi8uf9n67JW9Dqqr9iRd99MDhoB", до=до, limit=1000)
        страниц += 1
        if not стр:
            break
        старейшая = стр[-1]
        if len(стр) < 1000:
            break
        до = стр[-1]["signature"]
    итог["подписи_brez_старейшая"] = {"utc": S.utc((старейшая or {}).get("blockTime")),
                                      "страниц": страниц}
    if старейшая:
        tx = уз.вызов("getTransaction", [старейшая["signature"], P.ОПЦИИ_TX], срок=40.0)
        итог["подписи_brez_старейшая"]["tx_отдана"] = bool(tx)
    Path(КОРЕНЬ / "data" / "podbivka" / "shyft_glubina.json").write_text(
        json.dumps(итог, ensure_ascii=False, indent=1), encoding="utf-8")
    print("глубина Shyft:", json.dumps({д: [x["ok"] for x in v] for д, v in итог["дни"].items()},
                                       ensure_ascii=False), итог.get("before_чужая_свежая"),
          итог.get("подписи_brez_старейшая"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
