#!/usr/bin/env python3
"""Сверка по подписям: что транзакция СДЕЛАЛА с нашим кошельком. Только чтение.

ЗАЧЕМ. Владелец 01.10 (вечер): по сделке DBT 16:29:23Z в записи стоит итог
+0.091217606 SOL, а по Solscan продажа дала 0.00324471 WSOL, то есть около
-0.007. Две цифры о одних деньгах -- это не "почти одно и то же", это вопрос,
какая из них считается из чего. Здесь транзакция разбирается по полям meta:
нативный баланс кошелька до и после, изменения ВСЕХ его токеновых счетов и
отдельно счёта WSOL.

ПОЧЕМУ ЭТО ВАЖНО ИМЕННО ДЛЯ WSOL. Закрытие счёта WSOL отдаёт в нативный баланс
ВСЁ, что на нём лежало, вместе с рентой счёта. Если на нём остались деньги от
прежних сделок, нативный прирост в этой транзакции будет больше выручки от
продажи -- и записать его как итог ЭТОЙ сделки значит записать себе чужую
прибыль. Поэтому здесь нативная дельта и дельта WSOL печатаются РАЗНЫМИ числами,
а не одной "дельтой кошелька".

НИЧЕГО НЕ ПОДПИСЫВАЕТ И НЕ ОТПРАВЛЯЕТ. Один getTransaction на подпись.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

WSOL = "So11111111111111111111111111111111111111112"
ЛАМПОРТОВ = 1_000_000_000


def _зов_через_узел(урл: str):
    import urllib.request  # noqa: PLC0415

    def зов(метод, параметры):
        тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                            "params": параметры}).encode()
        з = urllib.request.Request(урл, data=тело,
                                   headers={"content-type": "application/json"})
        with urllib.request.urlopen(з, timeout=30) as о:  # noqa: S310
            return json.loads(о.read())

    return зов


def разобрать(tx: dict, кошелёк: str) -> dict:
    """Что транзакция сделала с этим кошельком. Числа -- из meta, не из догадок."""
    из_: dict = {"ok": False, "why_not": None, "слот": None, "ошибка": None,
                  "комиссия_sol": None, "натив_до_sol": None, "натив_после_sol": None,
                  "натив_дельта_sol": None, "wsol_дельта_sol": None,
                  "токены": {}, "программы": [], "наш_индекс": None}
    if not isinstance(tx, dict) or not tx:
        из_["why_not"] = "узел не отдал транзакцию"
        return из_
    мета = tx.get("meta") or {}
    сооб = (tx.get("transaction") or {}).get("message") or {}
    ключи = [(к.get("pubkey") if isinstance(к, dict) else к)
             for к in (сооб.get("accountKeys") or [])]
    из_["слот"] = tx.get("slot")
    из_["ошибка"] = мета.get("err")
    try:
        из_["комиссия_sol"] = round(int(мета.get("fee") or 0) / ЛАМПОРТОВ, 9)
    except (TypeError, ValueError):
        из_["комиссия_sol"] = None
    if кошелёк in ключи:
        i = ключи.index(кошелёк)
        из_["наш_индекс"] = i
        до = (мета.get("preBalances") or [])
        после = (мета.get("postBalances") or [])
        if i < len(до) and i < len(после):
            из_["натив_до_sol"] = round(int(до[i]) / ЛАМПОРТОВ, 9)
            из_["натив_после_sol"] = round(int(после[i]) / ЛАМПОРТОВ, 9)
            из_["натив_дельта_sol"] = round((int(после[i]) - int(до[i])) / ЛАМПОРТОВ, 9)
    else:
        из_["why_not"] = "нашего кошелька нет в счетах этой транзакции"
    # ТОКЕНОВЫЕ СЧЕТА: до и после по ВЛАДЕЛЬЦУ. Счёт, которого в одном из списков
    # нет, считается нулём ТОЛЬКО на той стороне, где его нет: созданный в этой
    # же транзакции счёт до неё и правда имел ноль.
    def по_владельцу(ряд):
        из__ = {}
        for б in (ряд or []):
            if (б or {}).get("owner") != кошелёк:
                continue
            м = б.get("mint")
            с = ((б.get("uiTokenAmount") or {}).get("amount"))
            try:
                из__[м] = из__.get(м, 0) + int(с)
            except (TypeError, ValueError):
                continue
        return из__

    до_т = по_владельцу(мета.get("preTokenBalances"))
    после_т = по_владельцу(мета.get("postTokenBalances"))
    for м in sorted(set(до_т) | set(после_т)):
        д = после_т.get(м, 0) - до_т.get(м, 0)
        из_["токены"][м] = {"до": до_т.get(м, 0), "после": после_т.get(м, 0),
                             "дельта": д}
    if WSOL in из_["токены"]:
        из_["wsol_дельта_sol"] = round(из_["токены"][WSOL]["дельта"] / ЛАМПОРТОВ, 9)
    прог = []
    for и in (сооб.get("instructions") or []):
        п = (и or {}).get("programId") or (и or {}).get("program")
        if п:
            прог.append(str(п))
    из_["программы"] = прог
    из_["ok"] = из_["натив_дельта_sol"] is not None
    return из_


def итог_сделки(покупка: dict, продажа: dict) -> dict:
    """Итог ПО ДВУМ ПОДПИСЯМ: сколько ушло на покупке и сколько вернулось.

    Нативная дельта продажи НЕ приравнивается к выручке: если в той же
    транзакции закрылся счёт WSOL с чужими остатками, прирост больше выручки.
    Поэтому выручка считается как нативная дельта МИНУС то, что пришло из WSOL
    сверх выхода свопа, и если эти числа расходятся -- расхождение называется.
    """
    из_ = {"ушло_sol": None, "вернулось_натив_sol": None,
            "итог_натив_sol": None, "расхождение": None, "почему": None}
    if покупка.get("натив_дельта_sol") is None or продажа.get("натив_дельта_sol") is None:
        из_["почему"] = "одна из транзакций не разобрана -- итог не считаем"
        return из_
    из_["ушло_sol"] = round(-покупка["натив_дельта_sol"], 9)
    из_["вернулось_натив_sol"] = продажа["натив_дельта_sol"]
    из_["итог_натив_sol"] = round(покупка["натив_дельта_sol"]
                                   + продажа["натив_дельта_sol"], 9)
    w = продажа.get("wsol_дельта_sol")
    if w is not None and w < 0:
        # WSOL на продаже УМЕНЬШИЛСЯ: значит часть нативного прироста -- это
        # распакованный WSOL, а не выручка свопа. Называем число, а не прячем.
        из_["расхождение"] = round(продажа["натив_дельта_sol"] + w, 9)
        из_["почему"] = (f"на продаже счёт WSOL уменьшился на {-w} SOL: столько "
                          "пришло в натив из распаковки, а не от свопа")
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--podpisi", required=True, help="подписи через запятую")
    р.add_argument("--koshelek", required=True)
    р.add_argument("--url", default=None, help="адрес узла (по умолчанию из HELIUS_API_KEY)")
    р.add_argument("--out", default=None)
    а = р.parse_args()
    import os  # noqa: PLC0415

    урл = а.url
    if not урл:
        ключ = (os.environ.get("HELIUS_API_KEY") or os.environ.get("HELIUS_API") or "").strip()
        урл = (f"https://mainnet.helius-rpc.com/?api-key={ключ}" if ключ
               else "https://api.mainnet-beta.solana.com")
    зов = _зов_через_узел(урл)
    итог = {"кошелёк": а.koshelek, "подписи": {}, "why_not": None}
    подписи = [п.strip() for п in а.podpisi.split(",") if п.strip()]
    for п in подписи:
        try:
            о = зов("getTransaction", [п, {"encoding": "jsonParsed",
                                            "maxSupportedTransactionVersion": 1}])
        except Exception as exc:  # noqa: BLE001
            итог["подписи"][п] = {"ok": False,
                                   "why_not": f"{type(exc).__name__}: {str(exc)[:120]}"}
            continue
        if о.get("error"):
            итог["подписи"][п] = {"ok": False, "why_not": str(о["error"])[:200]}
            continue
        итог["подписи"][п] = разобрать(о.get("result") or {}, а.koshelek)
    if len(подписи) == 2:
        итог["итог_по_двум"] = итог_сделки(итог["подписи"][подписи[0]],
                                            итог["подписи"][подписи[1]])
    текст = json.dumps(итог, ensure_ascii=False, indent=1)
    print(текст)
    if а.out:
        Path(а.out).write_text(текст + "\n", encoding="utf-8")
    плохо = [п for п, з in итог["подписи"].items() if not з.get("ok")]
    return 1 if плохо else 0


def self_test() -> int:
    плохо = []

    def chk(имя, усл, факт=""):
        if not усл:
            плохо.append(f"{имя}: {факт}")
        print(("ok   " if усл else "ПЛОХО") + f" {имя}")

    наш = "НАШ"
    чужой = "ЧУЖОЙ"
    покупка = {"slot": 100, "meta": {
        "err": None, "fee": 5000,
        "preBalances": [1_000_000_000, 0], "postBalances": [989_995_000, 0],
        "preTokenBalances": [],
        "postTokenBalances": [{"owner": наш, "mint": "ТОКЕН",
                                "uiTokenAmount": {"amount": "24217523500"}}]},
        "transaction": {"message": {"accountKeys": [{"pubkey": наш}, {"pubkey": чужой}],
                                     "instructions": [{"programId": "CPMM"}]}}}
    п = разобрать(покупка, наш)
    chk("покупка: нативная дельта -0.010005", п["ok"] and п["натив_дельта_sol"] == -0.010005,
        п["натив_дельта_sol"])
    chk("и токен пришёл числом", п["токены"]["ТОКЕН"]["дельта"] == 24217523500)
    chk("комиссия отдельным числом", п["комиссия_sol"] == 5e-06, п["комиссия_sol"])

    # продажа: своп дал 0.00324471, но закрылся WSOL с 0.1015 чужих денег
    продажа = {"slot": 200, "meta": {
        "err": None, "fee": 5000,
        "preBalances": [989_995_000, 0], "postBalances": [1_094_724_906, 0],
        "preTokenBalances": [{"owner": наш, "mint": WSOL,
                               "uiTokenAmount": {"amount": "101500000"}},
                              {"owner": наш, "mint": "ТОКЕН",
                               "uiTokenAmount": {"amount": "24217523500"}}],
        "postTokenBalances": [{"owner": наш, "mint": WSOL,
                                "uiTokenAmount": {"amount": "0"}},
                               {"owner": наш, "mint": "ТОКЕН",
                                "uiTokenAmount": {"amount": "0"}}]},
        "transaction": {"message": {"accountKeys": [{"pubkey": наш}, {"pubkey": чужой}],
                                     "instructions": [{"programId": "JUP"}]}}}
    пр = разобрать(продажа, наш)
    chk("продажа: нативная дельта +0.104729906",
        пр["натив_дельта_sol"] == 0.104729906, пр["натив_дельта_sol"])
    chk("и дельта WSOL отдельно, со знаком минус",
        пр["wsol_дельта_sol"] == -0.1015, пр["wsol_дельта_sol"])
    и = итог_сделки(п, пр)
    chk("итог по двум подписям: ушло 0.010005",
        и["ушло_sol"] == 0.010005, и["ушло_sol"])
    chk("нативный итог +0.094724906 -- и он НЕ выручка сделки",
        и["итог_натив_sol"] == 0.094724906, и["итог_натив_sol"])
    chk("расхождение названо числом: распаковка WSOL",
        и["расхождение"] == 0.003229906 and "WSOL" in и["почему"],
        (и["расхождение"], и["почему"]))

    chk("чужого кошелька в транзакции нет -- отказ словами, а не нули",
        разобрать(покупка, "НИКТО")["why_not"] is not None)
    chk("пустая транзакция -- отказ словами",
        not разобрать({}, наш)["ok"] and разобрать({}, наш)["why_not"])
    и2 = итог_сделки({"натив_дельта_sol": None}, пр)
    chk("одна транзакция не разобрана -- итог не считается вовсе",
        и2["итог_натив_sol"] is None and и2["почему"], и2)

    тело = Path(__file__).read_text(encoding="utf-8").split("def self_test")[0]
    chk("в рабочей части нет отправки и подписи",
        "sendTransaction" not in тело and "Keypair" not in тело, тело.count("send"))

    print(f"\nитог: {'ВСЁ ОК' if not плохо else 'ОТКАЗ'}; проверок плохих {len(плохо)}")
    for с in плохо:
        print("  -", с)
    return 1 if плохо else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    raise SystemExit(main())
