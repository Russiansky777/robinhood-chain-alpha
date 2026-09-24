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


СТЕЙБЛЫ = tuple(BD.СТАБИЛЬНЫЕ)


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
    стейбл_плюс = стейбл_минус = 0.0
    стейбл_минт = None
    for ключ in set(до) | set(после):
        if ключ[1] in СТЕЙБЛЫ:
            д = (после.get(ключ, 0.0) - до.get(ключ, 0.0))
            стейбл_плюс += max(0.0, д)
            стейбл_минус += max(0.0, -д)
            стейбл_минт = стейбл_минт or ключ[1]
    токен = max(токен_плюс, токен_минус)
    wsol = max(wsol_плюс, wsol_минус)
    стейбл = max(стейбл_плюс, стейбл_минус)
    if токен <= 0:
        return {"known": False, "why_not": "токен в этой сделке не двигался"}
    if wsol <= 0:
        # Нативный SOL: у пулов на кривой (pump.fun) WSOL-счёта нет вовсе.
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        сдвиг = max((abs(int(b) - int(a)) for a, b in zip(pre, post)), default=0)
        wsol = сдвиг / ЛАМПОРТ
    # Котировочная сторона выбирается ОДНА, и её имя возвращается: сравнивать
    # цену в SOL с ценой в USDC нельзя, а проценты к входу сравнивать можно --
    # но только внутри одной котировочной стороны.
    if wsol > 0:
        return {"known": True, "price": wsol / токен, "quote": "SOL",
                 "quote_mint": WSOL, "token_ui": токен, "quote_ui": wsol,
                 # Имя price_sol оставлено для совместимости со старыми записями.
                 "price_sol": wsol / токен}
    if стейбл > 0:
        return {"known": True, "price": стейбл / токен, "quote": "стейбл",
                 "quote_mint": стейбл_минт, "token_ui": токен, "quote_ui": стейбл}
    return {"known": False,
             "why_not": "котировочной стороны в сделке нет -- цену не вывести"}


def источник_по_подписи(state: ST.ExecState, подпись: str) -> str | None:
    """Адрес источника по подписи его транзакции -- из журнала решений."""
    if not подпись:
        return None
    try:
        текст = state.decisions_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in текст.splitlines():
        if подпись not in line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("signature") == подпись and r.get("source"):
            return r["source"]
    return None


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
            квота_входа: str | None = None,
            точки_блоков=ТОЧКИ_БЛОКОВ, точки_секунд=ТОЧКИ_СЕКУНД) -> dict:
    подписи = подписи_пула(helius, пул)
    if подписи and подписи[0].get("known") is False:
        return {"known": False, "why_not": подписи[0].get("why_not")}
    итог = {"known": True, "mint": минт, "pool": пул,
             "entry_price": цена_входа, "entry_price_sol": цена_входа,
             "entry_quote": квота_входа, "points": [], "trades_seen": len(подписи)}
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
        elif квота_входа and ц.get("quote") != квота_входа:
            # Цена в другой котировочной стороне -- это другая величина.
            строка.update(known=False,
                           why_not=(f"сделка в другой котировочной стороне "
                                     f"({ц.get('quote')}), вход был в {квота_входа}"))
        else:
            строка.update(known=True, price=ц["price"], quote=ц.get("quote"),
                           price_sol=ц.get("price_sol"))
            if цена_входа:
                строка["vs_entry_pct"] = round(
                    (ц["price"] - цена_входа) / цена_входа * 100, 2)
        итог["points"].append(строка)
    return итог


def цена_входа_источника(tx: dict, минт: str) -> dict:
    """Цена и котировочная сторона входа источника: (цена, квота)."""
    ц = цена_из_сделки(tx or {}, минт)
    if not ц.get("known"):
        return {"known": False, "why_not": ц.get("why_not")}
    return {"known": True, "price": ц["price"], "quote": ц.get("quote"),
             "quote_mint": ц.get("quote_mint")}


def сводка_кандидатов(пулы: dict, минт: str) -> list[dict]:
    """Все кандидаты в пул с причиной отказа по каждому.

    Без этой сводки строка "пула нет" непроверяема: не видно, был ли кандидат
    вообще и за что отклонён. Сводка -- это факт из транзакции, а не вывод.
    """
    из = []
    for c in (пулы.get("candidates") or []):
        минты = c.get("mints") or []
        из.append({"address": c.get("address"), "mints_count": len(минты),
                    "has_mint": минт in минты,
                    "with_wsol": bool(c.get("with_wsol")),
                    "dex_programs": c.get("dex_programs") or [],
                    "rejected": c.get("rejected")})
    return из


def _ключи(tx: dict) -> list:
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    вых = []
    for k in msg.get("accountKeys") or []:
        вых.append(k.get("pubkey") if isinstance(k, dict) else k)
    return вых


def _участники(tx: dict, кошелёк: str | None) -> set:
    """Кошельки сделки: подписанты, плательщик и названный кошелёк."""
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    из = {k.get("pubkey") for k in (msg.get("accountKeys") or [])
           if isinstance(k, dict) and k.get("signer") and k.get("pubkey")}
    ключи = _ключи(tx)
    if ключи:
        из.add(ключи[0])
    if кошелёк:
        из.add(кошелёк)
    return {x for x in из if x}


def хранилища_минта(tx: dict, минт: str, *, кошелёк: str | None = None) -> list[dict]:
    """Счета-ХРАНИЛИЩА этого минта в транзакции: адрес счёта, владелец, объём.

    Зачем это нужно отдельно от адреса пула, и почему без этого кривая не
    строилась ни разу. Правило "пул -- владелец хранилищ обеих сторон пары"
    верно для концентрированной ликвидности, но у Raydium AMM v4 и CPMM
    владельцем ВСЕХ хранилищ выступает один служебный PDA на все пулы
    (GpMZbSM2..., проверен по цепи), а адреса самого пула в транзакции нет
    вовсе. Все пять боевых покупок прошли именно через CPMM -- отсюда
    "пула с этим токеном не нашлось" на всех пяти парах.

    Но для кривой адрес пула и не нужен: история подписей ОДНОГО хранилища
    минта -- это ровно операции этого пула, каждый свап трогает это
    хранилище. Это факт из транзакции, а не догадка о пуле.

    Счета участников сделки (источник, наш кошелёк, плательщик, подписанты)
    исключаются: счёт минта у участника -- это его ATA, а не хранилище пула.
    """
    meta = (tx or {}).get("meta") or {}
    ключи = _ключи(tx)
    участники = _участники(tx, кошелёк)
    в_dex = BD._счета_инструкций_dex(tx)
    объём: dict = {}
    владелец: dict = {}
    for где in ("preTokenBalances", "postTokenBalances"):
        for b in meta.get(где) or []:
            if not isinstance(b, dict) or b.get("mint") != минт:
                continue
            i = b.get("accountIndex")
            адрес = ключи[i] if isinstance(i, int) and 0 <= i < len(ключи) else None
            if not адрес:
                continue
            владелец[адрес] = b.get("owner") or владелец.get(адрес)
            объём[адрес] = max(объём.get(адрес, 0.0), _ui(b))
    вых = []
    for адрес, об in объём.items():
        if адрес in участники or владелец.get(адрес) in участники:
            continue
        вых.append({"account": адрес, "owner": владелец.get(адрес),
                     "amount": об, "dex_programs": в_dex.get(адрес, [])})
    # Порядок: сначала встреченные в инструкциях DEX, среди них -- с большим
    # объёмом минта. Объём -- признак того, что это глубокое хранилище пула, а
    # не промежуточный счёт маршрута.
    вых.sort(key=lambda x: (bool(x["dex_programs"]), x["amount"]), reverse=True)
    return вых


def пул_для_кривой(helius, *, минт: str, tx_источника: dict | None,
                    кошелёк_источника: str | None, подпись_нашей: str | None = None,
                    кошелёк_наш: str | None = None) -> dict:
    """Пул, где этот токен торгуется: сначала транзакция источника, потом наша.

    Почему НАША покупка годится как второй источник адреса пула: кривая
    измеряет цену токена, а не чужую сделку. Наша покупка того же минта
    прошла через пул, и адрес пула в её счетах есть -- при той же
    котировочной стороне это та же величина. Откуда взят адрес, пишется в
    "pool_from": подмены источника без слов не бывает.

    Если пула нет ни там, ни там -- причина по каждой транзакции и полная
    сводка кандидатов, чтобы владелец мог проверить отказ по цепи.
    """
    диаг: list[dict] = []
    причины: list[str] = []

    def попытка(метка: str, tx: dict | None, кош: str | None) -> str | None:
        if not tx:
            причины.append(f"{метка}: узел не отдал транзакцию")
            return None, None, None
        пулы = BD.кандидаты_пулов(tx, минт=минт, кошелёк=кош)
        диаг.extend(dict(c, **{"from": метка})
                     for c in сводка_кандидатов(пулы, минт))
        годные = [c for c in (пулы.get("candidates") or [])
                   if not c.get("rejected") and минт in (c.get("mints") or [])]
        пул = пулы.get("pool_wsol") or (годные[0]["address"] if годные else None)
        if пул:
            return пул, "пул", "владелец хранилищ обеих сторон пары"
        причины.append(f"{метка}: {пулы.get('why_not')}")
        # Пула в транзакции нет -- берём хранилище минта: его история подписей
        # и есть сделки этого пула. Подмены нет, вид адреса назван в pool_kind.
        хран = хранилища_минта(tx, минт, кошелёк=кош)
        диаг.extend({"from": метка, "vault": h["account"], "owner": h["owner"],
                      "amount": h["amount"], "dex_programs": h["dex_programs"]}
                     for h in хран[:4])
        if хран:
            почему = ("хранилище минта с наибольшим объёмом"
                       + (" среди встреченных в инструкциях DEX"
                           if хран[0]["dex_programs"] else
                           " (в инструкциях DEX не встречено)"))
            return хран[0]["account"], "хранилище минта", почему
        причины.append(f"{метка}: хранилищ минта, кроме счетов участников, нет")
        return None, None, None

    for метка, tx, кош in (("транзакция источника", tx_источника, кошелёк_источника),
                            ("наша покупка", None, кошелёк_наш)):
        if метка == "наша покупка":
            if not подпись_нашей:
                continue
            tx = helius.транзакция(подпись_нашей)
        пул, вид, почему = попытка(метка, tx, кош)
        if пул:
            return {"pool": пул, "pool_kind": вид, "pool_from": метка,
                     "pool_why": почему, "candidates": диаг[:8], "why_not": None}
    return {"pool": None, "pool_kind": None, "pool_from": None, "pool_why": None,
             "candidates": диаг[:8],
             "why_not": "; ".join(x for x in причины if x) or "кандидатов нет"}


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
    # Сделка за стейбл даёт цену В СТЕЙБЛЕ, и котировочная сторона названа:
    # смешивать её с ценой в SOL нельзя, и кривая это проверяет отдельно.
    ц_ст = цена_из_сделки(сделка(токенов=1000, sol=2.0, стейбл=True), МИНТ)
    chk("сделка за стейбл даёт цену в стейбле и сторона названа",
        ц_ст["known"] and ц_ст["quote"] == "стейбл", ц_ст)
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

    # Подпись должна быть НА или ПОСЛЕ цели (+1 блок = слот 101), иначе
    # проверяется не то: точка просто не находится.
    к_смесь = кривая(Helius([{"signature": "S1", "slot": 101,
                               "blockTime": 1790000001, "err": None}]),
                      минт=МИНТ, пул="ПУЛ", слот_источника=100,
                      время_источника=1790000000.0, цена_входа=0.002,
                      квота_входа="стейбл")
    chk("точка в другой котировочной стороне в кривую НЕ идёт",
        к_смесь["points"][0]["known"] is False
        and "другой котировочной" in к_смесь["points"][0]["why_not"],
        к_смесь["points"][0])
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

    # --- выбор адреса для кривой: пул, хранилище минта, отказ с причиной ---
    RAY = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
    СЛУЖ = "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL"

    def тx_с_пулом(*, пул="ПУЛ2", кв=WSOL, в_dex=True, плательщик="КОШ",
                    хранилище="ХРАН-МИНТ"):
        """Транзакция свапа: владелец хранилищ -- пул, счета настоящие."""
        ключи = [{"pubkey": плательщик, "signer": True},
                  {"pubkey": хранилище}, {"pubkey": "ХРАН-КВ"}]
        счета = [пул, хранилище, "ХРАН-КВ"] if в_dex else ["ДРУГОЙ"]
        return {"slot": 100, "blockTime": 1790000000,
                 "transaction": {"signatures": ["X"], "message": {
                     "accountKeys": ключи,
                     "instructions": [{"programId": RAY, "accounts": счета}]}},
                 "meta": {"err": None, "fee": 5000,
                           "preBalances": [10 ** 9, 0, 0],
                           "postBalances": [10 ** 9, 0, 0],
                           "preTokenBalances": [бал(пул, МИНТ, 1000, 1),
                                                 бал(пул, кв, 1000, 2, 9)],
                           "postTokenBalances": [бал(пул, МИНТ, 900, 1),
                                                  бал(пул, кв, 1100, 2, 9)]}}

    class HeliusНашаПокупка:
        def __init__(self, tx):
            self.tx, self.спрошено = tx, []

        def транзакция(self, подпись, **kw):
            self.спрошено.append(подпись)
            return self.tx

    в1 = пул_для_кривой(HeliusНашаПокупка(None), минт=МИНТ,
                         tx_источника=тx_с_пулом(), кошелёк_источника="КОШ")
    chk("адрес пула взят из транзакции источника, вид и откуда названы",
        в1["pool"] == "ПУЛ2" and в1["pool_kind"] == "пул"
        and в1["pool_from"] == "транзакция источника", в1)

    # Главный случай боевых пяти пар: Raydium CPMM. Владелец хранилищ -- один
    # служебный PDA на все пулы, адреса пула в транзакции нет. Тогда берётся
    # ХРАНИЛИЩЕ минта: его история -- операции этого же пула.
    в_cpmm = пул_для_кривой(HeliusНашаПокупка(None), минт=МИНТ,
                             tx_источника=тx_с_пулом(пул=СЛУЖ),
                             кошелёк_источника="КОШ")
    chk("пула нет (CPMM: служебный PDA) -- взято хранилище минта, вид назван",
        в_cpmm["pool"] == "ХРАН-МИНТ"
        and в_cpmm["pool_kind"] == "хранилище минта"
        and "объёмом" in (в_cpmm["pool_why"] or ""), в_cpmm)

    # Счёт минта у участника сделки -- это его ATA, а не хранилище пула.
    свой = тx_с_пулом(пул="КОШ")
    chk("ATA участника сделки хранилищем не считается",
        хранилища_минта(свой, МИНТ, кошелёк="КОШ") == [],
        хранилища_минта(свой, МИНТ, кошелёк="КОШ"))

    # В транзакции источника ни пула, ни чужого хранилища -- тогда адрес
    # берётся из НАШЕЙ покупки того же минта.
    h = HeliusНашаПокупка(тx_с_пулом(пул="ПУЛ3"))
    в2 = пул_для_кривой(h, минт=МИНТ, tx_источника=тx_с_пулом(пул="КОШ"),
                         кошелёк_источника="КОШ", подпись_нашей="НАША")
    chk("в транзакции источника нечего взять -- берётся наша покупка",
        в2["pool"] == "ПУЛ3" and в2["pool_from"] == "наша покупка"
        and h.спрошено == ["НАША"], в2)

    # Токен торгуется НЕ к WSOL (пара к стейблу) -- для кривой это годный пул:
    # величину задаёт котировочная сторона входа, а её проверяет сама кривая.
    в3 = пул_для_кривой(HeliusНашаПокупка(None), минт=МИНТ,
                         tx_источника=тx_с_пулом(
                             кв="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
                         кошелёк_источника="КОШ")
    chk("пул к стейблу для кривой годится", в3["pool"] == "ПУЛ2", в3)

    в4 = пул_для_кривой(HeliusНашаПокупка(тx_с_пулом(пул="КОШ")), минт=МИНТ,
                         tx_источника=тx_с_пулом(пул="КОШ"),
                         кошелёк_источника="КОШ", подпись_нашей="НАША")
    chk("ни пула, ни хранилища -- причина по каждой транзакции, а не пустота",
        в4["pool"] is None and "транзакция источника" in в4["why_not"]
        and "наша покупка" in в4["why_not"], в4)
    chk("в сводке кандидатов виден отклонённый и за что",
        в4["candidates"] and в4["candidates"][0].get("rejected"), в4["candidates"])

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
        # Адрес источника в записи позиции не хранится -- берём из журнала
        # решений по подписи. Без него нельзя отделить кошелёк сделки от пула.
        источник = поз.get("source") or источник_по_подписи(state, сиг)
        строка["source"] = источник
        наши_подписи = поз.get("signatures") or []
        выбор = пул_для_кривой(
            helius, минт=минт, tx_источника=tx, кошелёк_источника=источник,
            подпись_нашей=(наши_подписи[0] if наши_подписи else None),
            кошелёк_наш=поз.get("wallet"))
        пул = выбор.get("pool")
        вх = цена_входа_источника(tx, минт)
        строка["entry_price"] = вх.get("price")
        строка["entry_quote"] = вх.get("quote")
        строка["pool"] = пул
        строка["pool_from"] = выбор.get("pool_from")
        строка["pool_kind"] = выбор.get("pool_kind")
        строка["pool_why"] = выбор.get("pool_why")
        строка["pool_candidates"] = выбор.get("candidates")
        if not пул:
            строка["known"] = False
            строка["why_not"] = f"пула с этим токеном не нашлось -- {выбор.get('why_not')}"
            вых.append(строка)
            continue
        строка["curve"] = кривая(helius, минт=минт, пул=пул,
                                  слот_источника=поз.get("source_slot"),
                                  время_источника=tx.get("blockTime"),
                                  цена_входа=вх.get("price"),
                                  квота_входа=вх.get("quote"))
        строка["known"] = bool(строка["curve"].get("known"))
        вых.append(строка)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    ST.atomic_write_json(Path(a.out), {
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rows": вых,
        "note": ("цена считается по СДЕЛКАМ пула, а не по резервам: резервов на "
                  "нужном слоте обычный RPC не отдаёт. Сделки за стейбл в кривую "
                  "не идут -- смешивать цену в SOL и в USDC нельзя. Если "
                  "pool_kind = 'хранилище минта', адрес пула в транзакции "
                  "отсутствует (Raydium AMM v4 и CPMM держат все хранилища на "
                  "одном служебном PDA), и подписи берутся по хранилищу: это "
                  "операции того же пула, но среди них могут быть и ввод/вывод "
                  "ликвидности, а не только свапы. Цена точки считается по всей "
                  "транзакции, поэтому маршрут через несколько пулов даёт "
                  "среднюю по маршруту, а не цену одного пула")})
    print(json.dumps(вых, ensure_ascii=False, indent=1)[:6000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
