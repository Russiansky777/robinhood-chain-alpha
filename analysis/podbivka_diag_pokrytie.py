#!/usr/bin/env python3
"""Подбивка: почему Shyft не отдал часть транзакций окна (покрытие 0.11-0.92 в пробе).

Для двух кошельков с худшим покрытием: подписи окна, getTransaction по одной на
Shyft -- время подписи, ответ/текст ошибки (через чисто); не отданные --
ещё раз на Helius (одна проверка, не скан). Итог -- распределение по часам.
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka as P  # noqa: E402
import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent


def main() -> int:
    уз = S.Узел()
    итог = {}
    for кош in ("2CHrnc2LyagAbMaMFgthiDWh7ZZ9zT9TF8WEJf7MNE71", "14sALF21u1PeJM5XDcFe2B3EMFjnLwhXhq9A5BMAEuCz"):
        подп = [з for з in уз.подписи(кош, limit=1000) if not з.get("err")
                and (з.get("blockTime") or 0) >= S.ГРАНЬ_SHYFT]
        по_часу: dict = collections.defaultdict(lambda: {"всего": 0, "не_отдал": 0})
        ошибки = collections.Counter()
        helius_отдал = 0
        проверено_helius = 0
        for з in подп:
            час = S.utc(з["blockTime"])[:13]
            по_часу[час]["всего"] += 1
            try:
                tx = уз.вызов("getTransaction", [з["signature"], P.ОПЦИИ_TX], срок=40.0, узел="shyft")
                причина = None if tx else "result пуст"
            except RuntimeError as exc:
                tx, причина = None, S.чисто(str(exc))[:100]
            if tx:
                continue
            по_часу[час]["не_отдал"] += 1
            ошибки[причина] += 1
            if проверено_helius < 20:
                проверено_helius += 1
                try:
                    if уз.вызов("getTransaction", [з["signature"], P.ОПЦИИ_TX], срок=40.0, узел="helius"):
                        helius_отдал += 1
                except RuntimeError:
                    pass
        итог[кош] = {"подписей": len(подп), "ошибки": dict(ошибки), "по_часу": dict(sorted(по_часу.items())),
                     "helius_отдал_из_20_не_отданных": [helius_отдал, проверено_helius]}
        print(кош[:8], "подписей", len(подп), "ошибки", dict(ошибки), "helius", helius_отдал, "/", проверено_helius)
    (КОРЕНЬ / "data" / "podbivka" / "diag_pokrytie.json").write_text(json.dumps(итог, ensure_ascii=False, indent=1),
                                                                       encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
