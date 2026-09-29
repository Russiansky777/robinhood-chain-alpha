#!/usr/bin/env python3
"""Подбивка: забор S+0 в деньгах -- 45 сделок полосы 28.09 06Z → 29.09 06Z по цепи (Правило 11).

Вход: data/podbivka/mesto/vhod_2026-09-29.json (data/sdelki_polosy_2026-09-29.json ветки Code-1, только чтение).
Итог сделки = изменение нативного баланса нашего кошелька и всех его токеновых счетов (владелец -- кошелёк;
WSOL, ATA минта) по всем транзакциям сделки -- комиссии, чаевые, рента входят (как в Правиле 11 Code-1).
Транзакции сделки: buy_sig, sell_sig, last_sell_signatures из записи Code-1 и ВСЕ транзакции кошелька в окне,
где у кошелька меняется баланс этого минта (продажи ищутся по подписям кошелька, не по истории пула; так
находятся и ручные продажи владельца). Транзакции, где у кошелька меняются балансы нескольких минтов
(пакетное закрытие пустых счетов), не входят: у кошелька и его счетов вместе рента в них сходится в ноль.
Только чтение, Helius. Выход: data/podbivka/zabor_s0_2026-09-29.json.
"""
from __future__ import annotations

import calendar
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
ВХОД = КОРЕНЬ / "data" / "podbivka" / "mesto" / "vhod_2026-09-29.json"
ВЫХОД = КОРЕНЬ / "data" / "podbivka" / "zabor_s0_2026-09-29.json"
ОКНО = ("2026-09-28T05:30:00Z", "2026-09-29T09:00:00Z")


def ts(s: str) -> float:
    return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))


def наши_дельты(tx: dict, кош: str) -> tuple[int, dict]:
    """Δ лампортов кошелька + всех его токеновых счетов; {минт: Δ сырых токенов кошелька}."""
    keys = C.account_keys(tx)
    meta = tx.get("meta") or {}
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    ряды = C.token_rows(tx)
    свои = {i for i, r in ряды.items() if r.get("owner") == кош and isinstance(i, int)}
    if кош in keys:
        свои.add(keys.index(кош))
    лам = sum(int(post[i]) - int(pre[i]) for i in свои if i < len(pre) and i < len(post))
    минты: dict = {}
    for r in ряды.values():
        if r.get("owner") == кош and r.get("mint") != C.WSOL:
            минты[r["mint"]] = минты.get(r["mint"], 0) + (int(r["post"]) - int(r["pre"]))
    return лам, минты


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    д = json.loads(ВХОД.read_text(encoding="utf-8"))
    ряды = д["ряды"]
    кош = next((р.get("wallet") or (р.get("zapis") or {}).get("wallet") for р in ряды
                if р.get("wallet") or (р.get("zapis") or {}).get("wallet")), None)
    уз = S.Узел()
    t0, t1 = ts(ОКНО[0]), ts(ОКНО[1])
    подп, до = [], None
    with уз.на("helius"):
        while True:
            стр = уз.подписи(кош, до=до, limit=1000)
            if not стр:
                break
            подп += [з for з in стр if t0 <= (з.get("blockTime") or 0) <= t1]
            if (стр[-1].get("blockTime") or 0) < t0 or len(стр) < 1000:
                break
            до = стр[-1]["signature"]
        явные = set()
        for р in ряды:
            з = р.get("zapis") or {}
            явные |= {x for x in [р.get("buy_sig"), р.get("sell_sig"), р.get("landed_sig"), *(з.get("last_sell_signatures") or [])] if x}
        все = sorted({з["signature"] for з in подп} | явные)
        txs = {}
        for и in range(0, len(все), 100):
            txs.update(уз.пакет(все[и:и + 100]))
    по_минту: dict = {}
    разбор = {}
    for s, т in txs.items():
        if not т:
            continue
        лам, минты = наши_дельты(т, кош)
        изм = {m: v for m, v in минты.items() if v != 0}
        разбор[s] = {"slot": т.get("slot"), "blockTime": т.get("blockTime"), "лампорты": лам, "минты": изм,
                     "ошибка": (т.get("meta") or {}).get("err") is not None}
        if len(изм) == 1:
            по_минту.setdefault(next(iter(изм)), []).append(s)
    # один минт может торговаться несколько раз: транзакция -- сделке с последним слотом источника ≤ её слота
    сделки_минта: dict = {}
    for р in ряды:
        сделки_минта.setdefault(р["mint"], []).append(р.get("source_slot") or 0)
    for v in сделки_минта.values():
        v.sort()

    def своя(р, s):
        сл = разбор[s]["slot"] or 0
        сс = сделки_минта[р["mint"]]
        i = max((k for k, x in enumerate(сс) if x <= сл), default=None)
        return i is not None and сс[i] == (р.get("source_slot") or 0)
    рез = []
    for р in ряды:
        з = р.get("zapis") or {}
        m = р["mint"]
        свои = {s for s in по_минту.get(m, []) if своя(р, s)}
        свои |= {x for x in [р.get("buy_sig"), р.get("landed_sig"), р.get("sell_sig"), *(з.get("last_sell_signatures") or [])]
                 if x and x in разбор and len(разбор[x]["минты"]) <= 1}
        # покупка и её окно: транзакции минта от нашей покупки (не раньше слота источника)
        свои = {s for s in свои if (разбор[s]["slot"] or 0) >= (р.get("source_slot") or 0)}
        пр = [s for s in свои if (разбор[s]["минты"].get(m) or 0) < 0]
        рез.append({"cid": р.get("cid"), "группа": р.get("group"), "mint": m, "source_slot": р.get("source_slot"),
                    "landed_slot": р.get("landed_slot"), "chain_ok": р.get("chain_ok"),
                    "итог_code1": р.get("итог_sol"), "транзакций": len(свои),
                    "итог_цепь_sol": sum(разбор[s]["лампорты"] for s in свои) / 1e9,
                    "продажи": sorted((разбор[s]["blockTime"], s) for s in пр),
                    "подписи": sorted(свои)})
    ВЫХОД.write_text(json.dumps({"кошелёк": кош, "окно": ОКНО, "подписей_кошелька": len(подп), "сделки": рез,
                                 "разбор": разбор, "расход": уз.расход()}, ensure_ascii=False, default=str), encoding="utf-8")
    R.записано(ВЫХОД)
    R.пуш("Podbivka-2: zabor S+0 v dengah po cepi 28.09 [automated]", [str(ВЫХОД)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
