#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Записать ДВИЖЕНИЕ ВЛАДЕЛЬЦА на кошельке полосы -- по подписи, проверенной цепью.

ЗАЧЕМ (слово владельца 28.09 ночью, п.4). Владелец снял свои деньги своим
ключом. Для порога "просадка от 00:00" это выглядит потерей размером со вывод,
и рубильник встал бы на ровном месте. Вывод записывается отдельным списком и
вычитается из просадки -- рубильник считает ТОРГОВЛЮ.

ПОЧЕМУ СУММА БЕРЁТСЯ С ЦЕПИ, А НЕ ИЗ ВХОДА. Число, напечатанное человеком,
проверить нечем, а ошибка в нём прямо ослабляет рубильник. Поэтому на вход
идёт только ПОДПИСЬ: сумма считается как нативная дельта кошелька полосы в
этой транзакции, и записывается лишь если она отрицательна (деньги ушли) и
транзакция без ошибки.

ВВОД (слово владельца 01.10, 23:07 Мадрид: внесено +1.4 SOL около 21:05Z).
Для просадки от 00:00 взнос выглядит ПРИБЫЛЬЮ размером со взнос, то есть даёт
рубильнику ровно столько же фальшивого запаса, сколько вывод давал фальшивой
потери. Поэтому ввод пишется ТЕМ ЖЕ списком, тем же модулем и с тем же
правилом "сумма только с цепи", но видом `ввод`: --vvod требует дельту
ПОЛОЖИТЕЛЬНУЮ, как --podpis без него требует отрицательную.

ПОИСК ПОДПИСИ ПО ЦЕПИ (--najti). Подпись взноса владелец может не прислать, а
искать её глазами в обозревателе -- значит переписывать потом руками. --najti
читает подписи кошелька за окно и называет те, где НАШ баланс вырос, а токены
не двигались вовсе: это и есть простой перевод. Ничего не записывает.

Ничего не подписывает и не отправляет: чтение транзакций и одна запись в
файл состояния.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HELIUS = "https://mainnet.helius-rpc.com"


def урл() -> str:
    к = (os.environ.get("HELIUS_API_KEY2")
         or os.environ.get("HELIUS_API_KEY")
         or os.environ.get("HELIUS_API") or "").strip()
    return f"{HELIUS}/?api-key={к}"


def зов(метод: str, параметры, *, таймаут: float = 30.0) -> dict:
    """Один вызов узла. Отдельно от модулей службы: на хосте их состав другой,
    и 23:46:40Z прогон упал на отсутствующем helius_client."""
    import urllib.request  # noqa: PLC0415

    тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                        "params": параметры}).encode()
    зпр = urllib.request.Request(урл(), data=тело,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(зпр, timeout=таймаут) as отв:  # noqa: S310
            о = json.loads(отв.read().decode())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why_not": f"{type(exc).__name__}: {str(exc)[:160]}"}
    if "error" in о:
        return {"ok": False, "why_not": f"RPC: {str(о['error'])[:160]}"}
    return {"ok": True, "result": о.get("result")}


def дельта_кошелька(tx: dict, кошелёк: str) -> dict:
    """Нативная дельта кошелька в транзакции: (до, после, дельта) в лампортах."""
    из_ = {"ok": False, "why_not": None}
    мета = (tx or {}).get("meta") or {}
    сообщение = ((tx or {}).get("transaction") or {}).get("message") or {}
    ключи = [(к.get("pubkey") if isinstance(к, dict) else к)
              for к in (сообщение.get("accountKeys") or [])]
    if кошелёк not in ключи:
        из_["why_not"] = "кошелька полосы в транзакции нет вовсе"
        return из_
    и = ключи.index(кошелёк)
    до = (мета.get("preBalances") or [])
    после = (мета.get("postBalances") or [])
    if и >= len(до) or и >= len(после):
        из_["why_not"] = "балансов в мете нет"
        return из_
    из_.update(ok=True, до=int(до[и]), после=int(после[и]),
                дельта=int(после[и]) - int(до[и]), err=мета.get("err"),
                fee=мета.get("fee"))
    return из_


def это_простой_перевод(tx: dict, кошелёк: str) -> dict:
    """Похожа ли транзакция на перевод SOL владельцем НА кошелёк полосы.

    ЧИСТАЯ ФУНКЦИЯ, чтобы правило можно было проверить без сети. Признаки:
    транзакция без ошибки, наш нативный баланс ВЫРОС, и ни один токеновый счёт
    кошелька не изменился -- у покупки или продажи токены двигаются всегда.
    Решает "похоже/не похоже" и НАЗЫВАЕТ, чего не хватило: тихое "нет" здесь
    означало бы потерянный взнос и ослабленный рубильник.
    """
    из_ = {"похоже": False, "why_not": None, "дельта": None, "токенов": 0}
    д = дельта_кошелька(tx, кошелёк)
    if not д["ok"]:
        из_["why_not"] = д["why_not"]
        return из_
    из_["дельта"] = д["дельта"]
    if д.get("err") is not None:
        из_["why_not"] = "транзакция села с ошибкой"
        return из_
    if д["дельта"] <= 0:
        из_["why_not"] = "наш баланс не вырос -- это не взнос"
        return из_
    мета = (tx or {}).get("meta") or {}
    свои = 0
    for где in ("preTokenBalances", "postTokenBalances"):
        for з in (мета.get(где) or []):
            if (з or {}).get("owner") == кошелёк:
                свои += 1
    из_["токенов"] = свои
    if свои:
        из_["why_not"] = (f"в транзакции двигались наши токеновые счета ({свои}) "
                           "-- это сделка, а не перевод")
        return из_
    из_["похоже"] = True
    return из_


def найти_вводы(кошелёк: str, *, сколько: int, с_ts: float | None,
                 до_ts: float | None, минимум_sol: float) -> dict:
    """Подписи, в которых кошелёк полосы ПОЛУЧИЛ SOL простым переводом."""
    из_ = {"ok": False, "why_not": None, "найдено": [], "просмотрено": 0,
            "пропущено": []}
    о = зов("getSignaturesForAddress", [кошелёк, {"limit": int(сколько)}])
    if not о.get("ok"):
        из_["why_not"] = f"подписи кошелька не прочитались: {о.get('why_not')}"
        return из_
    строки = о.get("result") or []
    for с in строки:
        вр = с.get("blockTime")
        if с_ts is not None and isinstance(вр, (int, float)) and float(вр) < с_ts:
            continue
        if до_ts is not None and isinstance(вр, (int, float)) and float(вр) > до_ts:
            continue
        if с.get("err") is not None:
            continue
        из_["просмотрено"] += 1
        от = зов("getTransaction",
                  [с.get("signature"), {"encoding": "jsonParsed",
                                         "maxSupportedTransactionVersion": 1,
                                         "commitment": "finalized"}])
        if not от.get("ok") or not от.get("result"):
            из_["пропущено"].append({"signature": с.get("signature"),
                                      "why_not": от.get("why_not") or "пустой ответ"})
            continue
        пр = это_простой_перевод(от["result"], кошелёк)
        if not пр["похоже"]:
            continue
        sol = пр["дельта"] / 1_000_000_000
        if sol < минимум_sol:
            continue
        из_["найдено"].append({"signature": с.get("signature"),
                                "sol": round(sol, 9), "blockTime": вр,
                                "slot": с.get("slot")})
    из_["ok"] = True
    return из_


def _самопроверка() -> int:
    """Проверяется ровно то, где правило могло бы соврать: сделка, принятая за
    взнос, и взнос, отвергнутый из-за чужих токеновых счетов в транзакции."""
    всего = [0, 0]

    def chk(имя, усл, факт=None):
        всего[1] += 1
        if усл:
            всего[0] += 1
            print(f"  [ok  ] {имя}")
        else:
            print(f"  [ПЛОХО] {имя} -- {факт}")

    К = "KOSHELEK"

    def tx(*, до, после, токены=(), err=None):
        return {"meta": {"preBalances": [до], "postBalances": [после], "err": err,
                          "fee": 5000,
                          "preTokenBalances": list(токены),
                          "postTokenBalances": list(токены)},
                 "transaction": {"message": {"accountKeys": [К]}}}

    chk("простой перевод на кошелёк распознан",
        это_простой_перевод(tx(до=1_000_000_000, после=2_400_000_000), К)["похоже"]
        is True)
    chk("списание не принято за взнос",
        это_простой_перевод(tx(до=2_000_000_000, после=1_000_000_000), К)["why_not"]
        is not None)
    chk("ПОКУПКА не принята за взнос: двигались наши токеновые счета",
        это_простой_перевод(tx(до=1_000_000_000, после=1_100_000_000,
                                токены=[{"owner": К, "accountIndex": 1}]), К)["похоже"]
        is False)
    chk("чужие токеновые счета в транзакции взносу не мешают",
        это_простой_перевод(tx(до=1_000_000_000, после=2_400_000_000,
                                токены=[{"owner": "ЧУЖОЙ", "accountIndex": 1}]),
                             К)["похоже"] is True)
    chk("транзакция с ошибкой взносом не считается",
        это_простой_перевод(tx(до=1_000_000_000, после=2_400_000_000,
                                err={"InstructionError": [0, "X"]}), К)["похоже"]
        is False)
    chk("кошелька в транзакции нет -- сказано словами",
        это_простой_перевод({"meta": {"preBalances": [1], "postBalances": [2]},
                              "transaction": {"message": {"accountKeys": ["ЧУЖОЙ"]}}},
                             К)["why_not"] is not None)
    print(f"самопроверка записи движений владельца: {всего[0]}/{всего[1]} пройдено")
    return 0 if всего[0] == всего[1] else 1


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--podpis", default="", help="подпись транзакции движения")
    р.add_argument("--vvod", action="store_true",
                    help="это ВВОД владельца: дельта кошелька обязана быть ПОЛОЖИТЕЛЬНОЙ")
    р.add_argument("--najti", action="store_true",
                    help="найти подписи взносов по цепи (ничего не пишет)")
    р.add_argument("--skolko", type=int, default=200,
                    help="сколько последних подписей кошелька смотреть при --najti")
    р.add_argument("--s-ts", type=float, default=0.0, help="окно поиска: с (unix)")
    р.add_argument("--do-ts", type=float, default=0.0, help="окно поиска: до (unix)")
    р.add_argument("--minimum-sol", type=float, default=0.05,
                    help="взносы меньше этого при --najti не называются")
    р.add_argument("--self-test", action="store_true")
    р.add_argument("--koshelek", default="", help="кошелёк полосы (пусто -- из окружения)")
    р.add_argument("--primechanie", default="движение владельца")
    р.add_argument("--zapisat", action="store_true",
                    help="без него только показывает, что записал бы")
    а = р.parse_args()
    if а.self_test:
        return _самопроверка()
    if not а.podpis and not а.najti:
        print("СБОЙ: нужна --podpis или --najti")
        return 2

    # МОДУЛИ СЛУЖБЫ ЛЕЖАТ В ЕЁ КАТАЛОГЕ, А НЕ РЯДОМ С ПРОГОНОМ. Прогон
    # доставляется в /tmp, и PYTHONPATH через sudo не всегда доезжает (sudo
    # чистит окружение): 23:43:37Z из-за этого вышло ModuleNotFoundError на
    # bloom_exec_state. Поэтому каталог кода службы добавляется здесь явно.
    for путь in (os.environ.get("BLOOM_CODE_DIR") or "",
                  "/home/bot/bloom_executor",
                  os.path.dirname(os.path.abspath(__file__))):
        if путь and os.path.isdir(путь) and путь not in sys.path:
            sys.path.insert(0, путь)
    import bloom_exec_state as ST  # noqa: PLC0415
    import bloom_own_send as OSW  # noqa: PLC0415

    кошелёк = (а.koshelek or os.environ.get("OWN_SEND_WALLET")
                or os.environ.get("BLOOM_LANE_WALLET") or "")
    if not кошелёк:
        print("СБОЙ: кошелёк полосы не задан")
        return 2
    if а.najti:
        н = найти_вводы(кошелёк, сколько=а.skolko,
                         с_ts=(а.s_ts or None), до_ts=(а.do_ts or None),
                         минимум_sol=а.minimum_sol)
        print(json.dumps(н, ensure_ascii=False, indent=1))
        if not н.get("ok"):
            return 3
        if not н.get("найдено"):
            print("взносов в окне не найдено -- это НЕ значит, что их не было: "
                  f"просмотрено подписей {н.get('просмотрено')}")
        return 0
    о = зов("getTransaction",
             [а.podpis, {"encoding": "jsonParsed",
                          "maxSupportedTransactionVersion": 1,
                          "commitment": "finalized"}])
    if not о.get("ok"):
        print(f"СБОЙ: транзакция не прочиталась: {о.get('why_not')}")
        return 3
    tx = о.get("result")
    if not tx:
        print("СБОЙ: узел вернул пустую транзакцию (подпись не найдена)")
        return 3
    д = дельта_кошелька(tx, кошелёк)
    if not д["ok"]:
        print(f"СБОЙ: {д['why_not']}")
        return 4
    if д.get("err") is not None:
        print(f"СБОЙ: транзакция села с ошибкой {д['err']} -- вывода не было")
        return 5
    if а.vvod:
        # У ВВОДА ЗНАК ОБРАТНЫЙ, И ПРОВЕРКА ТОЖЕ: иначе вывод, поданный с
        # --vvod, был бы записан как взнос и подарил бы рубильнику свой размер.
        пр = это_простой_перевод(tx, кошелёк)
        if not пр["похоже"]:
            print(f"СБОЙ: это не взнос владельца: {пр['why_not']}")
            return 6
        sol = д["дельта"] / 1_000_000_000
    elif д["дельта"] >= 0:
        print(f"СБОЙ: дельта кошелька {д['дельта']} лампортов -- это не вывод")
        return 6
    else:
        sol = -д["дельта"] / 1_000_000_000
    вид = "ввод" if а.vvod else "вывод"
    когда = tx.get("blockTime")
    print(f"{вид} по цепи: {sol:.9f} SOL, слот {tx.get('slot')}, "
          f"blockTime {когда}, комиссия {д.get('fee')}")
    if not а.zapisat:
        print(f"сухой прогон: с --zapisat запишу {вид} в список движений")
        return 0
    состояние = ST.ExecState()
    писалка = OSW.записать_ввод if а.vvod else OSW.записать_вывод
    р_ = писалка(состояние, подпись=а.podpis, sol=sol,
                  когда=(float(когда) if когда else None),
                  примечание=а.primechanie)
    print("запись:", json.dumps(р_, ensure_ascii=False))
    всего = OSW.выводы_владельца(состояние)
    print("движений в списке:", json.dumps(всего, ensure_ascii=False))
    return 0 if р_.get("ok") else 7


if __name__ == "__main__":
    raise SystemExit(main())
