#!/usr/bin/env python3
"""Подбивка: наши сделки 28.09 по цепи (Правило 11, файл Code-1) -- на каком слоте от источника мы купили.

Вход: data/podbivka/lane_s0/pravilo11_po_cepi_20260928.json -- копия
data/pravilo11_po_cepi_20260928.json ветки Code-1 (только чтение).
Для каждой сделки со слотом покупки: подписи минта до нашей покупки в слотах
[s − НАЗАД, s], их транзакции; покупка источника -- последняя, где источник
среди покупателей минта (C.mint_buyers). Лаг = наш слот − слот источника; при
лаге 0 -- индексы обеих в блоке (getBlock, signatures).
Только чтение. Выход: data/podbivka/lane_s0/lag_28.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
ВХОД = КОРЕНЬ / "data" / "podbivka" / "lane_s0" / "pravilo11_po_cepi_20260928.json"
ВЫХОД = КОРЕНЬ / "data" / "podbivka" / "lane_s0" / "lag_28.json"
НАЗАД = 30


def индекс(уз, слот: int, подпись: str):
    б = уз.вызов("getBlock", [слот, {"transactionDetails": "signatures", "rewards": False,
                                     "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}], срок=40.0)
    с = (б or {}).get("signatures") or []
    return (с.index(подпись) if подпись in с else None), len(с)


def одна(уз, сд: dict) -> dict:
    s, ист, минт, наша = сд["слот_покупки"], сд["источник"], сд["минт"], сд["подпись_покупки"]
    канд, до = [], наша
    for _ in range(5):
        стр = уз.подписи(минт, до=до, limit=1000)
        if not стр:
            break
        канд += [з for з in стр if з.get("err") is None and s - НАЗАД <= (з.get("slot") or 0) <= s]
        if (стр[-1].get("slot") or 0) < s - НАЗАД or len(стр) < 1000:
            break
        до = стр[-1]["signature"]
    найдено = None
    for и in range(0, len(канд), 100):
        кусок = канд[и:и + 100]
        txs = уз.пакет([з["signature"] for з in кусок])
        for з in кусок:              # подписи -- от новых к старым: первая найденная -- последняя до нас
            т = txs.get(з["signature"])
            if т and ист in C.mint_buyers(т, минт):
                найдено = з
                break
        if найдено:
            break
    р = {"подпись_покупки": наша, "подписей_минта_в_окне": len(канд)}
    if not найдено:
        return dict(р, why_not=f"покупка источника в [s−{НАЗАД}, s] не найдена")
    р.update(подпись_источника=найдено["signature"], слот_источника=найдено["slot"], лаг=s - найдено["slot"])
    if р["лаг"] == 0:
        и_н, n = индекс(уз, s, наша)
        и_и, _ = индекс(уз, s, найдено["signature"])
        р.update(индекс_наш=и_н, индекс_источника=и_и, в_блоке=n)
    return р


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    д = json.loads(ВХОД.read_text(encoding="utf-8"))
    уз = S.Узел()
    рез = []
    for сд in д["сделки"]:
        if not сд.get("слот_покупки") or not сд.get("подпись_покупки"):
            рез.append({"подпись_покупки": сд.get("подпись_покупки"), "why_not": "нет слота покупки"})
            continue
        with уз.на(S.узел_по_времени(None)):
            try:
                рез.append(одна(уз, сд))
            except RuntimeError as exc:
                рез.append({"подпись_покупки": сд["подпись_покупки"], "why_not": S.чисто(str(exc))[:160]})
        print(рез[-1].get("лаг"), рез[-1].get("why_not") or "", flush=True)
    ВЫХОД.write_text(json.dumps({"сделки": рез, "расход": уз.расход()}, ensure_ascii=False, default=str),
                     encoding="utf-8")
    R.записано(ВЫХОД)
    R.пуш("Podbivka-2: lane_s0 lag nashih pokupok ot istochnika 28.09 [automated]", [str(ВЫХОД)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
