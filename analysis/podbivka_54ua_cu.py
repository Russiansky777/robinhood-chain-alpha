#!/usr/bin/env python3
"""Подбивка: 54uaRuJE -- цена за CU у него и у ближайшего источника; индекс источника при S+1.

Покупки -- те же 279 (docs/podbivka_2026-09-28_snaipery.md, поправить() сводки).
  * Источник в том же слоте (229): обе транзакции целиком (getTransaction, json) --
    цена CU из ComputeBudget (мкл/CU), лимит CU, приоритет по формуле Agave
    (podbivka_mesto_v_bloke.разбор), чаевые -- внешние (не вложенные) системные
    переводы подписанта на чужой адрес (то же определение, что в сборе снайперов),
    и «приоритет с чаевыми» = (плата за приоритет + чаевые) × 1e6 / (стоимость + 1).
  * Источник на слот раньше (49 + 1 на два): индекс источника в его блоке и размер
    блока (getBlock, signatures).
Только чтение. Выход: data/podbivka/snaipery/54ua_cu.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_mesto_v_bloke as M  # noqa: E402
import podbivka_sim as S  # noqa: E402
import podbivka_snaipery_svod as SV  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
КОШ = "54uaRuJEc9BHY7uVMXtcf9JWcYDtB75hUeFJWeCxEkBE"
ВХОД = КОРЕНЬ / "data" / "podbivka" / "snaipery" / f"{КОШ}_168ч.json"
ВЫХОД = КОРЕНЬ / "data" / "podbivka" / "snaipery" / "54ua_cu.json"


def чаевые(tx: dict) -> tuple[int, list]:
    """Внешние системные переводы подписанта (счёт 0) на чужие адреса: сумма и получатели."""
    msg = (tx.get("transaction") or {}).get("message") or {}
    la = (tx.get("meta") or {}).get("loadedAddresses") or {}
    все = list(msg.get("accountKeys") or []) + list(la.get("writable") or []) + list(la.get("readonly") or [])
    плат = все[0] if все else None
    сумма, кому = 0, []
    for ix in msg.get("instructions") or []:
        if все[ix["programIdIndex"]] != M.SYSTEM:
            continue
        d = M.данные(ix)
        сч = ix.get("accounts") or []
        if len(d) >= 12 and d[:4] == b"\x02\x00\x00\x00" and len(сч) >= 2 and все[сч[0]] == плат and все[сч[1]] != плат:
            сумма += int.from_bytes(d[4:12], "little")
            кому.append(все[сч[1]])
    return сумма, кому


def разобрать(уз, подпись: str) -> dict:
    т = уз.вызов("getTransaction", [подпись, {"encoding": "json", "maxSupportedTransactionVersion": 1,
                                              "commitment": "confirmed"}], срок=40.0)
    if not т:
        return {"why_not": "транзакция не получена"}
    р = M.разбор_б(т) or {}
    р.pop("_параметры", None)
    ч, кому = чаевые(т)
    р.update(чаевые_lamports=ч, чаевые_кому=кому, cu_факт=(т.get("meta") or {}).get("computeUnitsConsumed"))
    if р.get("стоимость") is not None:
        р["приоритет_с_чаевыми"] = (р["плата_приор"] + ч) * 1_000_000 // (р["стоимость"] + 1)
    return р


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    д = json.loads(ВХОД.read_text(encoding="utf-8"))
    сд, _ = SV.поправить(д["сделки"])
    пок = [с for с in сд if с["сторона"] == "buy" and not с.get("многоминтовая")]
    уз = S.Узел()
    рез, блоки = [], {}
    for n, с in enumerate(пок):
        ист = [x for x in с.get("источники") or [] if x.get("адрес")]
        if not ист:
            continue
        б = max(ист, key=lambda x: (x["слот"], x["индекс"] if x["индекс"] is not None else -1))
        з = {"signature": с["signature"], "slot": с["slot"], "индекс": с.get("индекс"), "в_блоке": с.get("в_блоке"),
             "источник": б["адрес"], "подпись_источника": б["подпись"], "слот_источника": б["слот"],
             "индекс_источника": б["индекс"], "слотов_до": б["слотов_до"], "позиций_до": б.get("позиций_до")}
        with уз.на(S.узел_по_времени(с.get("blockTime"))):
            try:
                if б["слотов_до"] == 0:
                    з["его"] = разобрать(уз, с["signature"])
                    з["источника"] = разобрать(уз, б["подпись"])
                else:
                    if б["слот"] not in блоки:
                        бл = уз.вызов("getBlock", [б["слот"], {"transactionDetails": "signatures", "rewards": False,
                                                              "maxSupportedTransactionVersion": 1,
                                                              "commitment": "confirmed"}], срок=40.0)
                        блоки[б["слот"]] = len((бл or {}).get("signatures") or [])
                    з["в_блоке_источника"] = блоки[б["слот"]]
            except RuntimeError as exc:
                з["why_not"] = S.чисто(str(exc))[:160]
        рез.append(з)
        if n % 25 == 0:
            print(f"{n}/{len(пок)}", flush=True)
    ВЫХОД.write_text(json.dumps({"кошелёк": КОШ, "покупок": len(пок), "записи": рез, "расход": уз.расход()},
                                ensure_ascii=False, default=str), encoding="utf-8")
    R.записано(ВЫХОД)
    R.пуш("Podbivka-2: 54uaRuJE cena CU i indeks istochnika pri S+1 [automated]", [str(ВЫХОД)])
    print(json.dumps(уз.расход(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
