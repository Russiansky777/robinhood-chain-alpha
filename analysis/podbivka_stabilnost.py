#!/usr/bin/env python3
"""Стабильность адреса в окне: медиана > 0 в обеих половинах и доля суток в плюсе. Только счёт.

Слово владельца 03.10: к правилу кандидатов (n ≥ 20, среднее ≥ +2 п.п., медиана > 0, «толпа есть»)
добавляется стабильность -- медиана при нашем входе больше нуля в КАЖДОЙ половине окна и не меньше 60 %
суток в плюсе (сутки считаются только те, где в окне есть хотя бы одна ячейка).
"""
from __future__ import annotations

import statistics
import time

ДОЛЯ_СУТОК = 0.6


def сутки(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def стабильность(ячейки: list, с_ts: float | None = None, до_ts: float | None = None) -> dict:
    """ячейки -- список (ts, пп). Возвращает числа и флаг «стабилен»."""
    я = sorted((t, v) for t, v in ячейки if v is not None)
    если = {"n": len(я), "суток": 0, "суток_в_плюсе": 0, "доля_суток": None,
            "медиана_1": None, "медиана_2": None, "n_1": 0, "n_2": 0, "стабилен": False}
    if not я:
        return если
    т0 = с_ts if с_ts is not None else я[0][0]
    т1 = до_ts if до_ts is not None else я[-1][0]
    серёдка = т0 + (т1 - т0) / 2
    пол1 = [v for t, v in я if t < серёдка]
    пол2 = [v for t, v in я if t >= серёдка]
    по_суткам: dict = {}
    for t, v in я:
        по_суткам.setdefault(сутки(t), []).append(v)
    в_плюсе = sum(1 for d, v in по_суткам.items() if statistics.median(v) > 0)
    если.update(n_1=len(пол1), n_2=len(пол2),
                медиана_1=round(statistics.median(пол1), 2) if пол1 else None,
                медиана_2=round(statistics.median(пол2), 2) if пол2 else None,
                суток=len(по_суткам), суток_в_плюсе=в_плюсе,
                доля_суток=round(в_плюсе / len(по_суткам), 3) if по_суткам else None)
    если["стабилен"] = bool(пол1 and пол2 and если["медиана_1"] > 0 and если["медиана_2"] > 0
                            and (если["доля_суток"] or 0) >= ДОЛЯ_СУТОК)
    return если
