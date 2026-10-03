#!/usr/bin/env python3
"""Кто источник этой нашей сделки и когда он продал -- по ЦЕПИ, по подписи нашей покупки.

ЗАЧЕМ (задание владельца 03.10, п.1). Сделки SI и Awake в выгрузку ещё не попали (снимок
17:07Z), а узнать источник, группу и время его продажи нужно.

ИСТОЧНИК БЕРЁТСЯ ИЗ НАШЕГО ЖУРНАЛА, ЕСЛИ ОН ТАМ ЕСТЬ (слово владельца 04.10). «Ближайший
покупатель пула перед нами» ОШИБАЕТСЯ: у сделки SI он дал D8pBNrTq (группа 133), а по журналу
Code-1 источник -- 7JVQMwRj (lane_s0). Поэтому сначала подпись покупки ищется в выгрузках
(поле `source`), и только если её там нет -- читается цепь, и тогда ответ помечен как ДОГАДКА
по цепи. Дальше по подписям источника ищется его продажа этого минта.

Выход: data/podbivka/sdelka_istochnik_<метка>.json. Только чтение цепи.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import podbivka_lider_po_cepi as L  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
SOLы = {"So11111111111111111111111111111111111111112", "native"}
НАШИ = {"4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x",
        "21DqHDDPEfMhK1dHRkV9E8v8KTTSKQGApJAr1irC9j7w"}


def utc(ts) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else "—"


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    import podbivka_sim as S  # noqa: PLC0415
    р = argparse.ArgumentParser()
    р.add_argument("--buy", action="append", default=[], help="подпись нашей покупки (можно несколько)")
    р.add_argument("--imya", action="append", default=[], help="имя сделки под той же позицией")
    р.add_argument("--podpisey", type=int, default=120, help="сколько подписей пула читать до нас")
    р.add_argument("--stranic-koshelka", type=int, default=5,
                   help="страниц подписей нашего кошелька для поиска полной подписи по началу")
    р.add_argument("--metka", default=time.strftime("%Y-%m-%d", time.gmtime()))
    а = р.parse_args()
    реестр = json.loads((П / "arhiv_adresa.json").read_text(encoding="utf-8"))["адреса"]
    # НАШ ЖУРНАЛ: {подпись покупки: (источник, группа, имя токена)} из всех выгрузок
    журнал: dict = {}
    for ф in list((П / "sdelki").glob("*.json")) + [КОРЕНЬ / "data" / "sdelki_polosy_vse_s_2709.json"]:
        if not ф.exists():
            continue
        try:
            д_ = json.loads(ф.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for r_ in (д_.get("ряды") or []):
            if r_.get("buy_sig") and r_.get("source"):
                журнал[r_["buy_sig"]] = {"адрес": r_["source"], "группа": r_.get("group"),
                                         "имя_токена": r_.get("token_name"), "cid": r_.get("cid")}
    print(f"журнал: подписей покупок с источником {len(журнал)}", flush=True)
    уз = S.Узел()
    из_ = []
    with уз.на("helius"):
        # ПОДПИСЬ МОЖЕТ БЫТЬ ДАНА НАЧАЛОМ (владелец пишет «3PgwJCfR…»). Полную берём из
        # подписей НАШИХ кошельков: дешевле и точнее, чем искать по пулу.
        полные: dict = {}
        коротких = [x for x in а.buy if len(x) < 80]
        if коротких:
            найдено = {}
            for кош in sorted(НАШИ):
                до = None
                for _ in range(а.stranic_koshelka):
                    стр = уз.подписи(кош, до=до, limit=1000)
                    if not стр:
                        break
                    for з in стр:
                        for пр in коротких:
                            if з["signature"].startswith(пр):
                                найдено[пр] = з["signature"]
                    if len(найдено) == len(коротких):
                        break
                    до = стр[-1]["signature"]
                if len(найдено) == len(коротких):
                    break
            полные = найдено
            print(f"начала подписей: нашлось {len(полные)} из {len(коротких)}", flush=True)
        for и, подпись_вход in enumerate(а.buy):
            подпись = полные.get(подпись_вход, подпись_вход)
            if len(подпись) < 80:
                из_.append({"имя": (а.imya[и] if и < len(а.imya) else f"сделка {и + 1}"),
                            "наша_покупка": подпись_вход,
                            "why_not": "полная подпись не нашлась в подписях наших кошельков"})
                print(f"{подпись_вход}: полная подпись не нашлась", flush=True)
                continue
            имя = а.imya[и] if и < len(а.imya) else f"сделка {и + 1}"
            стр = {"имя": имя, "наша_покупка": подпись}
            т = уз.tx(подпись)
            if not т:
                стр["why_not"] = "узел не отдал нашу покупку"
                из_.append(стр)
                continue
            s0, bt = т.get("slot"), т.get("blockTime")
            ряды = [r for r in C.token_rows(т).values()
                    if r["owner"] in НАШИ and r["post"] > r["pre"] and r["mint"] not in SOLы]
            if not ряды:
                стр["why_not"] = "в нашей покупке нет прихода токена на наш кошелёк"
                из_.append(стр)
                continue
            минт = max(ряды, key=lambda r: r["post"] - r["pre"])["mint"]
            наш = next(iter({r["owner"] for r in ряды}))
            пул = C.identify_pool(т, наш, минт)
            стр.update(slot=s0, utc=utc(bt), mint=минт, наш_кошелёк=наш,
                       pool_vault=пул.get("pool_vault"), quote_mint=пул.get("quote_mint"))
            if not пул.get("pool_vault"):
                стр["why_not"] = f"пул не опознан: {пул.get('why_not')}"
                из_.append(стр)
                continue
            # свопы пула ДО нас: ищем покупателя из реестра
            стр_подписи = уз.подписи(пул["pool_vault"], до=подпись, limit=min(1000, а.podpisey))
            кандидаты = [з for з in стр_подписи if з.get("err") is None and з.get("blockTime")]
            пак = уз.пакет([з["signature"] for з in кандидаты[:а.podpisey]],
                           {з["signature"]: з.get("blockTime") for з in кандидаты})
            найден = None
            свопов = 0
            for з in кандидаты:
                т2 = пак.get(з["signature"])
                if not т2:
                    continue
                свопов += 1
                покупатели = {r["owner"] for r in C.token_rows(т2).values()
                              if r["mint"] == минт and r["post"] > r["pre"] and r["owner"]}
                свои = покупатели & set(реестр)
                if свои:
                    a = sorted(свои)[0]
                    найден = {"адрес": a, "группы": (реестр.get(a) or {}).get("группы") or [],
                              "его_покупка": з["signature"], "slot": т2.get("slot"),
                              "bt": з.get("blockTime"), "utc": utc(з.get("blockTime")),
                              "слотов_до_нас": (s0 - (т2.get("slot") or 0)),
                              "секунд_до_нас": round((bt or 0) - (з.get("blockTime") or 0), 1)}
                    break
            стр["свопов_пула_прочитано"] = свопов
            из_журнала = журнал.get(подпись)
            if из_журнала:
                стр["источник_из_журнала"] = из_журнала
                стр["источник_по_цепи_догадка"] = найден
                if найден and найден["адрес"] != из_журнала["адрес"]:
                    стр["расхождение"] = ("ближайший покупатель пула не равен источнику журнала: "
                                          f"{найден['адрес'][:8]} против {из_журнала['адрес'][:8]}")
                найден = {"адрес": из_журнала["адрес"],
                          "группы": (реестр.get(из_журнала["адрес"]) or {}).get("группы") or [],
                          "откуда": "журнал полосы", "группа_полосы": из_журнала.get("группа")}
                # время его покупки берём с цепи -- ищем её среди прочитанных свопов пула
                for з in кандидаты:
                    т2 = пак.get(з["signature"])
                    if not т2:
                        continue
                    если_он = {r["owner"] for r in C.token_rows(т2).values()
                               if r["mint"] == минт and r["post"] > r["pre"] and r["owner"]}
                    if найден["адрес"] in если_он:
                        найден.update(его_покупка=з["signature"], slot=т2.get("slot"),
                                      bt=з.get("blockTime"), utc=utc(з.get("blockTime")),
                                      слотов_до_нас=(s0 - (т2.get("slot") or 0)),
                                      секунд_до_нас=round((bt or 0) - (з.get("blockTime") or 0), 1))
                        break
            else:
                стр["источник_откуда"] = "цепь (догадка: ближайший покупатель пула перед нами)"
            стр["источник"] = найден
            if найден:
                # его продажа этого минта после его покупки
                его_bt = найден.get("bt") or 0
                его = [з for з in уз.подписи(найден["адрес"], limit=1000)
                       if з.get("err") is None and (з.get("blockTime") or 0) >= его_bt]
                его.sort(key=lambda x: x.get("blockTime") or 0)
                пак2 = уз.пакет([з["signature"] for з in его[:200]],
                                {з["signature"]: з.get("blockTime") for з in его[:200]})
                продажа = None
                for з in его[:200]:
                    т3 = пак2.get(з["signature"])
                    if not т3:
                        continue
                    ушло = [r for r in C.token_rows(т3).values()
                            if r["mint"] == минт and r["owner"] == найден["адрес"]
                            and r["post"] < r["pre"]]
                    if ушло:
                        продажа = {"подпись": з["signature"], "slot": т3.get("slot"),
                                   "utc": utc(з.get("blockTime")),
                                   "секунд_после_его_покупки": round((з.get("blockTime") or 0) - его_bt, 1),
                                   "слотов_после_его_покупки": ((т3.get("slot") or 0)
                                                                - (найден.get("slot") or 0)),
                                   "токенов_ушло": sum(float(r["pre"] - r["post"]) for r in ушло)}
                        break
                стр["его_продажа"] = продажа
            из_.append(стр)
            print(json.dumps(стр, ensure_ascii=False)[:600], flush=True)
    out = П / f"sdelka_istochnik_{а.metka}.json"
    out.write_text(json.dumps({"сделки": из_, "расход": уз.расход()}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    R.записано(out)
    print(f"{out.name}: сделок {len(из_)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
