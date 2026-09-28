#!/usr/bin/env python3
"""Подбивка: разбор одной сделки полосы по цепи (Pygoscelis kerguelensis, 28.09).
Только чтение. Фаза 1 -- цепь (Helius); фаза 2 (7 дней) -- архив PumpApi,
podbivka_arhiv_den.py с --dop-adresa, сводка -- podbivka_pygoscelis_svod.py.

  * наша продажа -- у нашего кошелька в --t (±3 с), наша покупка -- последняя
    покупка того же минта перед ней;
  * пул -- по нашей покупке; вся история хранилища токена пула до s_продажи+150
    (от создания, если влезает в --stranic страниц по 1000);
  * по каждой транзакции пула: слот, индекс в блоке (getBlock signatures),
    кошельки с изменением баланса минта (покупка/продажа), токенов, SOL-экв.,
    цена после (состояние пула из транзакции);
  * источник -- покупатель минта из групп Code-1 в слотах s_наш−3..s_наш до нас
    (если строка Code-1 пришла -- --istochnik);
  * кошельки ступеней (покупатели от сделки источника до пика) и продавцы
    первых 15 с после пика: возраст (самая старая подпись, до --stranic страниц),
    первое входящее пополнение SOL (кто и когда).
Выход: data/podbivka/pygoscelis/faza1.json.
"""
from __future__ import annotations

import argparse
import calendar
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import podbivka_sim as S  # noqa: E402
import podbivka_snaipery as SN  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka" / "pygoscelis"
НАШ = "4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x"
SYSTEM = "11111111111111111111111111111111"
ВЫХОД = {"имя": "faza1.json"}


def подписи_назад(уз, адрес: str, до: str | None, страниц: int, стоп_bt: int | None = None,
                  стоп_слот: int | None = None) -> tuple[list, bool]:
    """Подписи адреса от `до` назад; (список, дошли_до_начала)."""
    из_, курсор = [], до
    for _ in range(страниц):
        стр = уз.подписи(адрес, до=курсор, limit=1000)
        if not стр:
            return из_, True
        из_ += стр
        if len(стр) < 1000:
            return из_, True
        if стоп_bt and (стр[-1].get("blockTime") or 0) < стоп_bt:
            return из_, False
        if стоп_слот and (стр[-1].get("slot") or 0) < стоп_слот:
            return из_, False
        курсор = стр[-1]["signature"]
    return из_, False


def первое_пополнение(уз, кош: str, страниц: int) -> dict:
    сп, до_начала = подписи_назад(уз, кош, None, страниц)
    if not сп:
        return {"подписей": 0}
    стар = сп[-1]
    из_ = {"подписей_просмотрено": len(сп), "дошли_до_первой": до_начала,
           "самая_старая_подпись": стар["signature"], "самая_старая_bt": стар.get("blockTime")}
    if not до_начала:
        return из_
    for з in reversed(сп[-20:]):                       # первые 20 транзакций кошелька
        т = уз.tx(з["signature"])
        if not т:
            continue
        ключи = C.account_keys(т)
        ixs = list(((т.get("transaction") or {}).get("message") or {}).get("instructions") or [])
        for гр in ((т.get("meta") or {}).get("innerInstructions") or []):
            ixs += гр.get("instructions") or []
        for ix in ixs:
            п = ix.get("parsed") if isinstance(ix, dict) else None
            if isinstance(п, dict) and ix.get("programId") == SYSTEM and п.get("type") in ("transfer", "createAccount") \
                    and (п.get("info") or {}).get("destination", (п.get("info") or {}).get("newAccount")) == кош:
                инф = п["info"]
                return {**из_, "пополнил": инф.get("source"), "лампорты": int(инф.get("lamports") or 0),
                        "пополнение_подпись": з["signature"], "пополнение_bt": т.get("blockTime")}
        if ключи and ключи[0] != кош:
            return {**из_, "пополнил": ключи[0], "пополнение_подпись": з["signature"], "пополнение_bt": т.get("blockTime"),
                    "пополнение_вид": "плательщик первой транзакции (перевода SOL не найдено)"}
    return из_


def цена_ст(ст):
    """Цена котировки за сырой токен после события: кривая vs/vt, LaunchLab quote/base, x*y=k x/y."""
    if not ст:
        return None
    for a, b in (("vs", "vt"), ("quote", "base"), ("x", "y")):
        if ст.get(a) is not None and ст.get(b):
            return ст[a] / ст[b]
    return None


def метки() -> dict:
    м: dict = {}
    for a, x in json.loads((КОРЕНЬ / "data" / "podbivka" / "arhiv_adresa.json").read_text(encoding="utf-8"))["адреса"].items():
        м[a] = {"группы": x.get("группы"), "имя": x.get("имя")}
    for a in ("54uaRuJEc9BHY7uVMXtcf9JWcYDtB75hUeFJWeCxEkBE", "BomGAZnAGwnjs3oaqNHm4Wk5sKctQi83PKVxjRuGGbrm"):
        м.setdefault(a, {"группы": [], "имя": None})
        м[a]["группы"] = sorted(set((м[a].get("группы") or []) + ["снайпер KABUTSTR"]))
    м[НАШ] = {"группы": ["МЫ"], "имя": "наш кошелёк полосы"}
    return м


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--t", default="2026-09-28T14:54:56", help="время нашей продажи (если нет --pokupka)")
    р.add_argument("--pokupka", default="", help="подпись нашей покупки (продажа -- первая продажа минта после неё)")
    р.add_argument("--istochnik", default="")
    р.add_argument("--stranic", type=int, default=15)
    р.add_argument("--metka", default="")
    р.add_argument("--pered", type=int, default=0, help="история пула только от s_наш − pered слотов")
    а = р.parse_args()
    ВЫХОД["имя"] = f"faza1_{а.metka}.json" if а.metka else "faza1.json"
    уз = S.Узел()
    М = метки()
    рез: dict = {"t": а.t, "наш": НАШ, "pokupka": а.pokupka or None}
    with уз.на("helius"):
        # 1. наши сделки
        if а.pokupka:
            tb0 = уз.tx(а.pokupka)
            T0 = (tb0 or {}).get("blockTime") or 0
            сп, _ = подписи_назад(уз, НАШ, None, 20, стоп_bt=T0 - 60)
            окно = [з for з in сп if з.get("err") is None and T0 - 5 <= (з.get("blockTime") or 0) <= T0 + 3600]
        else:
            T = calendar.timegm(time.strptime(а.t, "%Y-%m-%dT%H:%M:%S"))
            сп, _ = подписи_назад(уз, НАШ, None, 10, стоп_bt=T - 1800)
            окно = [з for з in сп if з.get("err") is None and T - 600 <= (з.get("blockTime") or 0) <= T + 10]
        txs = уз.пакет([з["signature"] for з in окно])
        сд = []
        for з in окно:
            т = txs.get(з["signature"])
            for x in SN.разбор(т, НАШ) if т else []:
                сд.append({**x, "signature": з["signature"], "slot": т["slot"], "blockTime": т.get("blockTime")})
        if а.pokupka:
            пк = next((x for x in сд if x["signature"] == а.pokupka and x["сторона"] == "buy"), None)
            прод = sorted([x for x in сд if пк and x["сторона"] == "sell" and x["mint"] == пк["mint"] and x["slot"] >= пк["slot"]],
                          key=lambda x: x["slot"])
        else:
            прод = [x for x in сд if x["сторона"] == "sell" and abs((x["blockTime"] or 0) - T) <= 3]
        if not прод:
            рез["why_not"] = "продажа не найдена"
            рез["наши_сделки_в_окне"] = сд
            return выход(рез, уз)
        пр = прод[0]
        mint = пр["mint"]
        if not а.pokupka:
            пок = [x for x in сд if x["сторона"] == "buy" and x["mint"] == mint and x["slot"] <= пр["slot"]]
            пк = max(пок, key=lambda x: (x["slot"], x["blockTime"] or 0)) if пок else None
        рез.update(mint=mint, продажа=пр, покупка=пк)
        if not пк:
            рез["why_not"] = "наша покупка не найдена"
            return выход(рез, уз)
        tb = уз.tx(пк["signature"])
        пул = C.identify_pool(tb, НАШ, mint)
        режим = "curve" if пул.get("quote_mint") == C.NATIVE_QUOTE else "xyk"
        import c2_pool_programs as PP  # noqa: PLC0415
        прог = PP.pool_program(tb, пул.get("pool_vault"), PP.labels()).get("pool_program") if пул.get("pool_vault") else None
        if прог == S.SB.LAUNCHLAB:
            режим = "launchlab"
        рез.update(пул={k: пул.get(k) for k in ("pool_vault", "quote_vault", "quote_mint")}, режим=режим, программа=прог)
        # 2. история хранилища пула
        опора = S.подпись_после_слота(уз, пр["slot"] + 151)
        стоп = пк["slot"] - а.pered if а.pered else None
        ист, до_начала = подписи_назад(уз, пул["pool_vault"], опора, а.stranic, стоп_слот=стоп)
        ист = [з for з in ист if з.get("err") is None and (з.get("slot") or 0) <= пр["slot"] + 150
               and (стоп is None or (з.get("slot") or 0) >= стоп)]
        ист.reverse()
        рез["история"] = {"подписей": len(ист), "от_создания": до_начала}
        бл = SN.Блоки(уз)
        лента = []
        for и in range(0, len(ист), 100):
            кус = ист[и:и + 100]
            пач = уз.пакет([з["signature"] for з in кус])
            for з in кус:
                т = пач.get(з["signature"])
                if not т:
                    continue
                кош = set(C.mint_buyers(т, mint)) | set(C.mint_sellers(т, mint))
                ст = S.состояние(т, режим, пул, mint)
                цена = цена_ст(ст)
                for w in кош:
                    for x in SN.разбор(т, w):
                        if x["mint"] != mint:
                            continue
                        лента.append({"signature": з["signature"], "slot": т["slot"], "blockTime": т.get("blockTime"),
                                      "кошелёк": w, "сторона": x["сторона"], "токенов": x["токенов"], "dec": x["dec"],
                                      "sol": x["sol_экв"], "цена_после": цена,
                                      "резерв_после": ст, "метка": М.get(w), "плательщик": (C.account_keys(т) or [None])[0]})
        рез["лента"] = лента
        рез["создатель"] = лента[0]["плательщик"] if (лента and до_начала) else None
        # 3. источник
        кандидаты = [x for x in лента if x["сторона"] == "buy" and пк["slot"] - 3 <= x["slot"] <= пк["slot"]
                     and x["кошелёк"] != НАШ and x["signature"] != пк["signature"]]
        if а.istochnik:
            ист_кош = а.istochnik
        else:
            из_групп = [x for x in кандидаты if x["метка"] and set(x["метка"]["группы"] or []) & {"leader", "batch5", "lane_s0"}]
            ист_кош = (из_групп[-1] if из_групп else (кандидаты[-1] if кандидаты else {})).get("кошелёк")
        рез["источник"] = ист_кош
        рез["источник_откуда"] = "аргумент (строка Code-1)" if а.istochnik else "покупатель из групп Code-1 в s_наш−3..s_наш"
        # индексы в блоке -- для слотов от источника до продажи+150
        s_src = min((x["slot"] for x in лента if x["кошелёк"] == ист_кош and x["сторона"] == "buy"
                     and x["slot"] <= пк["slot"]), default=пк["slot"])
        for x in лента:
            if s_src - 2 <= x["slot"] <= пр["slot"] + 150:
                x["индекс"], _ = бл.индекс(x["slot"], x["signature"])
        # 4. пик (наибольшая цена после события от источника до нашей продажи), ступени, падение
        окно_л = [x for x in лента if s_src <= x["slot"] <= пр["slot"] and x["цена_после"]]
        пик = max(окно_л, key=lambda x: x["цена_после"]) if окно_л else None
        рез["пик"] = {k: пик.get(k) for k in ("signature", "slot", "blockTime", "цена_после", "индекс")} if пик else None
        ступени = [x for x in лента if пик and x["сторона"] == "buy" and s_src <= x["slot"] <= пик["slot"]
                   and x["кошелёк"] != НАШ]
        падение = [x for x in лента if пик and x["сторона"] == "sell"
                   and пик["blockTime"] <= (x["blockTime"] or 0) <= пик["blockTime"] + 15
                   and (x["slot"], x.get("индекс") or 0) > (пик["slot"], пик.get("индекс") or 0)]
        кошельки = {ист_кош} | {x["кошелёк"] for x in ступени} | {x["кошелёк"] for x in падение}
        кошельки.discard(НАШ)
        кошельки.discard(None)
        рез["кошельки"] = {}
        for w in sorted(кошельки):
            try:
                рез["кошельки"][w] = {"метка": М.get(w), **первое_пополнение(уз, w, 5)}
            except RuntimeError as exc:
                рез["кошельки"][w] = {"why_not": S.чисто(str(exc))[:120]}
    return выход(рез, уз)


def выход(рез: dict, уз) -> int:
    П.mkdir(parents=True, exist_ok=True)
    рез["расход"] = уз.расход()
    out = П / ВЫХОД["имя"]
    out.write_text(json.dumps(рез, ensure_ascii=False, default=str), encoding="utf-8")
    import podbivka_run as R  # noqa: PLC0415
    R.записано(out)
    print(json.dumps({k: рез.get(k) for k in ("mint", "режим", "программа", "источник", "why_not")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
