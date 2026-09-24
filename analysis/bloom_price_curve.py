#!/usr/bin/env python3
"""Кривая цены токена после покупки источника. Только чтение.

Вопрос владельца: какой горизонт выхода правильный. Ответ может дать только
цена в точках после входа источника: +1, +2, +3, +5, +10 блоков и +5, +10,
28.8, +60 секунд (28.8 с -- срок нашего авто-ордера).

Как считается цена, и почему именно так:

  * цена берётся из СДЕЛОК, а не из резервов пула. Резервы на нужном слоте
    обычным RPC не получить (архивного getAccountInfo по слоту нет), а
    сделка -- это факт: столько-то токена ушло, столько-то SOL пришло;
  * сделки ищутся по подписям ПУЛА: getSignaturesForAddress отдаёт слот и
    время каждой, и по ним выбирается ближайшая на/после каждой цели;
  * цена одной сделки = |изменение котировочной стороны| / |изменение токена|
    по балансам самого пула. Сторона считается котировочной, если это WSOL
    (или нативный SOL пула). Сделка за стейбл в кривую НЕ идёт: смешивать
    цену в SOL и в USDC в одной кривой значит подменить величину;
  * если для точки сделок нет, так и написано: "сделок нет". Ноль или
    последняя известная цена вместо факта не подставляются никогда.

Цена входа источника считается из его же транзакции тем же правилом, и вся
кривая даётся ещё и в процентах к ней -- это и есть ответ про горизонт.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_detector as BD  # noqa: E402
import bloom_exec_state as ST  # noqa: E402

ЛАМПОРТ = 10 ** 9
WSOL = BD.WSOL
ТОЧКИ_БЛОКОВ = (1, 2, 3, 5, 10)
ТОЧКИ_СЕКУНД = (5.0, 10.0, 28.8, 60.0)


def _ui(b: dict) -> float:
    s = (b.get("uiTokenAmount") or {})
    try:
        return int(s.get("amount")) / (10 ** int(s.get("decimals") or 0))
    except (TypeError, ValueError):
        return 0.0


def цена_из_сделки(tx: dict, минт: str) -> dict:
    """Цена токена в SOL по одной сделке. Считается по ВСЕЙ транзакции.

    Берутся суммарные изменения: сколько токена сменило владельцев и сколько
    WSOL/нативного SOL при этом сдвинулось. Знаки не важны -- важна
    величина: цена = |SOL| / |токен|.
    """
    meta = (tx or {}).get("meta") or {}
    if meta.get("err") is not None:
        return {"known": False, "why_not": "транзакция с ошибкой"}

    def свод(записи):
        out = {}
        for b in записи or []:
            if not isinstance(b, dict):
                continue
            ключ = (b.get("owner"), b.get("mint"), b.get("accountIndex"))
            out[ключ] = _ui(b)
        return out

    до, после = свод(meta.get("preTokenBalances")), свод(meta.get("postTokenBalances"))
    токен_плюс = токен_минус = 0.0
    wsol_плюс = wsol_минус = 0.0
    for ключ in set(до) | set(после):
        д = (после.get(ключ, 0.0) - до.get(ключ, 0.0))
        if ключ[1] == минт:
            токен_плюс += max(0.0, д)
            токен_минус += max(0.0, -д)
        elif ключ[1] == WSOL:
            wsol_плюс += max(0.0, д)
            wsol_минус += max(0.0, -д)
    токен = max(токен_плюс, токен_минус)
    wsol = max(wsol_плюс, wsol_минус)
    if токен <= 0:
        return {"known": False, "why_not": "токен в этой сделке не двигался"}
    if wsol <= 0:
        # Нативный SOL: у пулов на кривой (pump.fun) WSOL-счёта нет вовсе.
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        сдвиг = max((abs(int(b) - int(a)) for a, b in zip(pre, post)), default=0)
        wsol = сдвиг / ЛАМПОРТ
        if wsol <= 0:
            return {"known": False,
                     "why_not": "котировочная сторона не в SOL/WSOL -- в кривую не идёт"}
    return {"known": True, "price_sol": wsol / токен,
             "token_ui": токен, "sol_ui": wsol}


def подписи_пула(helius, пул: str, *, предел: int = 1000) -> list:
    try:
        сп = helius.call("getSignaturesForAddress", [пул, {"limit": предел}]) or []
    except Exception as exc:  # noqa: BLE001
        return [{"known": False, "why_not": f"getSignaturesForAddress: {type(exc).__name__}"}]
    return [z for z in сп if isinstance(z, dict) and not z.get("err")]


def ближайшая(подписи: list, *, слот: int | None = None,
               время: float | None = None) -> dict | None:
    """Первая сделка НА или ПОСЛЕ цели. Раньше цели брать нельзя: это была бы
    цена до события, а вопрос -- что было после."""
    годные = []
    for z in подписи:
        if слот is not None and (z.get("slot") or 0) >= слот:
            годные.append((z.get("slot"), z))
        elif время is not None and (z.get("blockTime") or 0) >= время:
            годные.append((z.get("blockTime"), z))
    if not годные:
        return None
    годные.sort(key=lambda x: x[0] or 0)
    return годные[0][1]


def кривая(helius, *, минт: str, пул: str, слот_источника: int,
            время_источника: float | None, цена_входа: float | None,
            точки_блоков=ТОЧКИ_БЛОКОВ, точки_секунд=ТОЧКИ_СЕКУНД) -> dict:
    подписи = подписи_пула(helius, пул)
    if подписи and подписи[0].get("known") is False:
        return {"known": False, "why_not": подписи[0].get("why_not")}
    итог = {"known": True, "mint": минт, "pool": пул,
             "entry_price_sol": цена_входа, "points": [], "trades_seen": len(подписи)}
    цели = [("+{} блок".format(n), {"слот": слот_источника + n}) for n in точки_блоков]
    if время_источника:
        цели += [("+{:g} с".format(s), {"время": время_источника + s}) for s in точки_секунд]
    for имя, цель in цели:
        z = ближайшая(подписи, слот=цель.get("слот"), время=цель.get("время"))
        if not z:
            итог["points"].append({"point": имя, "known": False,
                                    "why_not": "сделок на эту точку и позже нет"})
            continue
        tx = helius.транзакция(z.get("signature"))
        ц = цена_из_сделки(tx or {}, минт)
        строка = {"point": имя, "signature": z.get("signature"),
                   "slot": z.get("slot"), "block_time": z.get("blockTime")}
        if not ц.get("known"):
            строка.update(known=False, why_not=ц.get("why_not"))
        else:
            строка.update(known=True, price_sol=ц["price_sol"])
            if цена_входа:
                строка["vs_entry_pct"] = round(
                    (ц["price_sol"] - цена_входа) / цена_входа * 100, 2)
        итог["points"].append(строка)
    return итог


def цена_входа_источника(tx: dict, минт: str) -> float | None:
    ц = цена_из_сделки(tx or {}, минт)
    return ц.get("price_sol") if ц.get("known") else None


# ------------------------------------------------------------- самопроверка

def self_test() -> None:
    всего = [0, 0]

    def chk(имя, условие, факт=None):
        всего[0] += 1
        if условие:
            всего[1] += 1
            print(f"  [ok  ] {имя}")
        else:
            print(f"  [ПЛОХО] {имя} -- {факт}")

    МИНТ = "МИНТ"

    def бал(owner, минт, raw, idx, dec=6):
        return {"accountIndex": idx, "mint": минт, "owner": owner,
                "uiTokenAmount": {"amount": str(raw), "decimals": dec,
                                   "uiAmount": raw / 10 ** dec}}

    def сделка(*, токенов, sol, подпись="S", slot=100, bt=1790000000,
                ошибка=None, нативный=False, стейбл=False):
        кв = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v" if стейбл else WSOL
        pre = [бал("ПУЛ", МИНТ, int(токенов * 1e6), 1)]
        post = [бал("ПУЛ", МИНТ, 0, 1)]
        if не_нативный := (not нативный):
            pre.append(бал("ПУЛ", кв, 0, 2, 9))
            post.append(бал("ПУЛ", кв, int(sol * 1e9), 2, 9))
        meta = {"err": ошибка, "fee": 5000,
                 "preBalances": [10 ** 9], "postBalances": [10 ** 9],
                 "preTokenBalances": pre, "postTokenBalances": post}
        if нативный:
            meta["preBalances"] = [10 ** 9]
            meta["postBalances"] = [10 ** 9 - int(sol * 1e9)]
        return {"slot": slot, "blockTime": bt,
                 "transaction": {"signatures": [подпись],
                                  "message": {"accountKeys": [{"pubkey": "ПУЛ"}],
                                               "instructions": []}},
                 "meta": meta}

    ц = цена_из_сделки(сделка(токенов=1000, sol=2.0), МИНТ)
    chk("цена по сделке -- SOL на токен", ц["known"] and abs(ц["price_sol"] - 0.002) < 1e-12,
        ц)
    chk("транзакция с ошибкой в цену не идёт",
        цена_из_сделки(сделка(токенов=1000, sol=2.0, ошибка={"X": 1}), МИНТ)["known"] is False)
    chk("сделка за стейбл в кривую не идёт",
        цена_из_сделки(сделка(токенов=1000, sol=2.0, стейбл=True), МИНТ)["known"] is False,
        цена_из_сделки(сделка(токенов=1000, sol=2.0, стейбл=True), МИНТ))
    ц_нат = цена_из_сделки(сделка(токенов=500, sol=1.0, нативный=True), МИНТ)
    chk("пул на кривой без WSOL-счёта: берётся нативный сдвиг",
        ц_нат["known"] and abs(ц_нат["price_sol"] - 0.002) < 1e-9, ц_нат)
    chk("токен не двигался -- цены нет",
        цена_из_сделки(сделка(токенов=0, sol=1.0), МИНТ)["known"] is False)

    подписи = [{"signature": f"S{i}", "slot": 100 + i, "blockTime": 1790000000 + i,
                 "err": None} for i in range(0, 80)]
    chk("ближайшая на/после слота -- именно та",
        ближайшая(подписи, слот=103)["signature"] == "S3")
    chk("раньше цели не берём", ближайшая(подписи, слот=1000) is None)
    chk("по времени тоже на/после",
        ближайшая(подписи, время=1790000005)["signature"] == "S5")

    class Helius:
        def __init__(self, подписи):
            self.подписи = подписи
            self.вызовы = []

        def call(self, метод, параметры):
            self.вызовы.append(метод)
            assert метод == "getSignaturesForAddress"
            return self.подписи

        def транзакция(self, подпись, **kw):
            n = int(подпись[1:])
            # цена растёт на 1 % за блок
            return сделка(токенов=1000, sol=2.0 * (1.01 ** n), подпись=подпись,
                           slot=100 + n, bt=1790000000 + n)

    к = кривая(Helius(подписи), минт=МИНТ, пул="ПУЛ", слот_источника=100,
                время_источника=1790000000.0, цена_входа=0.002)
    имена = [p["point"] for p in к["points"]]
    chk("точки блоков и секунд все на месте",
        имена == ["+1 блок", "+2 блок", "+3 блок", "+5 блок", "+10 блок",
                   "+5 с", "+10 с", "+28.8 с", "+60 с"], имена)
    первая = к["points"][0]
    chk("на +1 блоке цена и процент к входу посчитаны",
        первая["known"] and abs(первая["vs_entry_pct"] - 1.0) < 0.05, первая)
    chk("на +10 блоках рост больше, чем на +1",
        к["points"][4]["vs_entry_pct"] > первая["vs_entry_pct"], к["points"][4])
    последняя = к["points"][-1]
    chk("+60 с считается по сделке этой секунды",
        последняя["known"] and последняя["slot"] == 160, последняя)

    # А вот когда сделок после цели действительно нет -- это сказано прямо, а
    # не подменено последней известной ценой.
    к_коротко = кривая(Helius(подписи[:4]), минт=МИНТ, пул="ПУЛ",
                        слот_источника=100, время_источника=1790000000.0,
                        цена_входа=0.002)
    нет = [p2 for p2 in к_коротко["points"] if p2["known"] is False]
    chk("для точек без сделок сказано 'сделок нет', а не подставлена цена",
        len(нет) >= 4 and all("сделок" in p2["why_not"] for p2 in нет),
        [p2["point"] for p2 in нет])

    class HeliusМолчит:
        def call(self, *a, **kw):
            raise RuntimeError("узел")

        def транзакция(self, *a, **kw):
            return None

    к2 = кривая(HeliusМолчит(), минт=МИНТ, пул="ПУЛ", слот_источника=100,
                 время_источника=None, цена_входа=None)
    chk("узел не отдал подписи -- причина, а не пустая кривая",
        к2["known"] is False and "getSignaturesForAddress" in к2["why_not"], к2)

    print(f"самопроверка кривой цены: {всего[1]}/{всего[0]}"
          f"{' пройдено' if всего[1] == всего[0] else ' ПРОВАЛ'}")
    if всего[1] != всего[0]:
        raise SystemExit(1)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--state-dir", default=None)
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--out", default="data/bloom_price_curve.json")
    a = p.parse_args()
    if a.self_test:
        self_test()
        return 0

    state = ST.ExecState(base=Path(a.state_dir)) if a.state_dir else ST.ExecState()
    helius = BD.Helius(служба="bloom_price_curve")
    позиции = [x for x in state.positions().values() if ST.is_real_mode(x.get("mode"))]
    позиции.sort(key=lambda x: float(x.get("ts_intent") or 0))
    вых = []
    for поз in позиции[-a.limit:]:
        минт, сиг = поз.get("mint"), поз.get("source_sig")
        строка = {"mint": минт, "source_signature": сиг,
                   "source_task": поз.get("source_task")}
        tx = helius.транзакция(сиг) if сиг else None
        if not tx:
            строка["known"] = False
            строка["why_not"] = "узел не отдал транзакцию источника"
            вых.append(строка)
            continue
        пулы = BD.кандидаты_пулов(tx, минт=минт, кошелёк=поз.get("source"))
        пул = пулы.get("pool_wsol")
        цена = цена_входа_источника(tx, минт)
        строка["entry_price_sol"] = цена
        строка["pool"] = пул
        if not пул:
            строка["known"] = False
            строка["why_not"] = ("пула токен/WSOL в транзакции источника нет: "
                                  f"{пулы.get('why_not')}")
            вых.append(строка)
            continue
        строка["curve"] = кривая(helius, минт=минт, пул=пул,
                                  слот_источника=поз.get("source_slot"),
                                  время_источника=tx.get("blockTime"),
                                  цена_входа=цена)
        строка["known"] = bool(строка["curve"].get("known"))
        вых.append(строка)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    ST.atomic_write_json(Path(a.out), {
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rows": вых,
        "note": ("цена считается по СДЕЛКАМ пула, а не по резервам: резервов на "
                  "нужном слоте обычный RPC не отдаёт. Сделки за стейбл в кривую "
                  "не идут -- смешивать цену в SOL и в USDC нельзя")})
    print(json.dumps(вых, ensure_ascii=False, indent=1)[:6000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
