#!/usr/bin/env python3
"""Правило 11 ТОЛЬКО ПО ЦЕПИ: сколько ушло, сколько вернулось, итог.

СЛОВО ВЛАДЕЛЬЦА 28.09: "Таблица Правила 11 -- только по цепи, без
несчитаемых: у закрытой позиции обе транзакции на цепи, итог = SOL ушло минус
SOL вернулось (комиссии, чаевые, рента включены)... Статус несчитаемый из учёта
убрать; вместо него -- продажа не найдена на цепи с тревогой в TG".

ЧТО СЧИТАЕТСЯ ДЕНЬГАМИ. Изменение НАТИВНОГО баланса НАШИХ счетов в самой
транзакции: кошелёк плюс его токеновые счета (их владелец виден в
meta.pre/postTokenBalances). Ничего не очищается: комиссия, чаевые, приоритет и
рента счетов входят в число, потому что это и есть потраченные деньги. Обёртка
SOL внутри нашего же кошелька не искажает итог -- лампорты уходят на НАШ счёт
WSOL, а он в списке наших.

ЗНАК. В таблице три числа: "ушло" (положительное), "вернулось" (положительное) и
"итог" = вернулось минус ушло, то есть ПРИБЫЛЬ со знаком плюс. Формулировку
владельца "ушло минус вернулось" привожу отдельной графой "расход_минус_возврат"
-- это то же число с обратным знаком, и путать их нельзя.

ЧЕГО ЗДЕСЬ НЕТ. Полей службы (pnl_counted_sol и прочих) в счёте нет вовсе:
таблица нужна как независимая проверка учёта, а не как его пересказ. Ни подписи,
ни отправки: только getTransaction и getSignatureStatuses.
"""
from __future__ import annotations

import argparse
import calendar
import glob
import gzip
import json
import os
import sys
import time
from pathlib import Path

ЛАМПОРТОВ_В_SOL = 1_000_000_000
ПАРТИЯ_СТАТУСОВ = 256
HELIUS = "https://mainnet.helius-rpc.com"


def урл() -> str:
    к = (os.environ.get("HELIUS_API_KEY") or os.environ.get("HELIUS_API") or "").strip()
    return f"{HELIUS}/?api-key={к}"


def зов(метод: str, параметры, *, таймаут: float = 60.0) -> dict:
    import urllib.request  # noqa: PLC0415

    тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                        "params": параметры}).encode()
    try:
        зпр = urllib.request.Request(урл(), data=тело,
                                      headers={"content-type": "application/json"})
        with urllib.request.urlopen(зпр, timeout=таймаут) as отв:  # noqa: S310
            д = json.loads(отв.read())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why_not": f"{type(exc).__name__}: {str(exc)[:160]}"}
    if "error" in д:
        return {"ok": False, "why_not": str(д["error"])[:200]}
    return {"ok": True, "result": д.get("result"), "why_not": None}


# --------------------------------------------------------- деньги по цепи

def наши_индексы(tx: dict, кошелёк: str) -> dict:
    """Индексы НАШИХ счетов: кошелёк и его токеновые счета."""
    сообщение = ((tx or {}).get("transaction") or {}).get("message") or {}
    сырые = сообщение.get("accountKeys") or []
    ключи = [к.get("pubkey") if isinstance(к, dict) else к for к in сырые]
    мета = (tx or {}).get("meta") or {}
    наши = set()
    почему = None
    if кошелёк in ключи:
        наши.add(ключи.index(кошелёк))
    else:
        почему = "кошелька нет среди статических ключей"
    for где in ("preTokenBalances", "postTokenBalances"):
        for з in (мета.get(где) or []):
            if з.get("owner") == кошелёк and isinstance(з.get("accountIndex"), int):
                наши.add(int(з["accountIndex"]))
    return {"индексы": sorted(наши), "ключи": ключи, "why_not": почему}


def дельта_наших(tx: dict, кошелёк: str) -> dict:
    """Изменение нативного баланса наших счетов в лампортах, без очистки."""
    из_ = {"ok": False, "lamports": None, "fee": None, "счетов": 0,
            "why_not": None}
    мета = (tx or {}).get("meta") or {}
    до = мета.get("preBalances") or []
    после = мета.get("postBalances") or []
    if not до or not после:
        из_["why_not"] = "в транзакции нет балансов до и после"
        return из_
    н = наши_индексы(tx, кошелёк)
    if not н["индексы"]:
        из_["why_not"] = н["why_not"] or "наших счетов в транзакции нет"
        return из_
    сумма = 0
    for и in н["индексы"]:
        if и < len(до) and и < len(после):
            сумма += int(после[и]) - int(до[и])
    из_.update(ok=True, lamports=сумма, fee=int(мета.get("fee") or 0),
                счетов=len(н["индексы"]),
                slot=(tx or {}).get("slot"),
                blockTime=(tx or {}).get("blockTime"),
                err=(мета.get("err") is not None))
    return из_


# --------------------------------------------------------- позиции и подписи

def строки(путь: str):
    открыть = gzip.open if путь.endswith(".gz") else open
    try:
        ф = открыть(путь, "rt", encoding="utf-8")
    except Exception:  # noqa: BLE001
        return
    with ф:
        for строка in ф:
            строка = строка.strip()
            if not строка:
                continue
            try:
                yield json.loads(строка)
            except ValueError:
                continue


def позиции_полосы(state_dir: str, с_ts: float | None,
                    по_ts: float | None = None) -> dict:
    по_cid: dict = {}
    for путь in sorted(glob.glob(str(Path(state_dir) / "positions.jsonl*"))):
        for з in строки(путь):
            cid = з.get("client_order_id")
            if not cid:
                continue
            в = по_cid.setdefault(cid, {})
            for к, зн in з.items():
                if зн is not None:
                    в[к] = зн
    из_: dict = {}
    for cid, п in по_cid.items():
        if not п.get("lane"):
            continue
        т = п.get("ts_sent") or п.get("ts_intent")
        if not isinstance(т, (int, float)):
            continue
        if с_ts and float(т) < float(с_ts):
            continue
        if по_ts and float(т) > float(по_ts):
            continue
        из_[cid] = п
    return из_


def подписи_покупки(п: dict) -> list:
    ряд = [п.get("lane_landed_signature"), п.get("lane_signature"),
            п.get("signature_accepted")]
    for поле in ("lane_signatures", "lane_pool_candidates", "lane_variants"):
        зн = п.get(поле)
        if isinstance(зн, dict):
            ряд += list(зн.values())
        elif isinstance(зн, list):
            ряд += list(зн)
    из_, видели = [], set()
    for с in ряд:
        if isinstance(с, str) and с and с not in видели:
            видели.add(с)
            из_.append(с)
    return из_


def подписи_продажи(п: dict) -> list:
    ряд = [п.get("last_sell_reported"), п.get("jup_signature")]
    зн = п.get("last_sell_signatures")
    if isinstance(зн, list):
        ряд += list(зн)
    из_, видели = [], set()
    for с in ряд:
        if isinstance(с, str) and с and с not in видели:
            видели.add(с)
            из_.append(с)
    return из_


def севшие(подписи: list) -> dict:
    """Какие из подписей есть на цепи: одним getSignatureStatuses на партию."""
    из_: dict = {}
    ряд = [с for с in подписи if с]
    for н in range(0, len(ряд), ПАРТИЯ_СТАТУСОВ):
        часть = ряд[н:н + ПАРТИЯ_СТАТУСОВ]
        о = зов("getSignatureStatuses",
                 [часть, {"searchTransactionHistory": True}])
        if not о["ok"]:
            continue
        for с, зн in zip(часть, ((о["result"] or {}).get("value") or [])):
            if isinstance(зн, dict):
                из_[с] = {"slot": зн.get("slot"), "err": зн.get("err")}
    return из_


def транзакция(подпись: str) -> dict | None:
    о = зов("getTransaction", [подпись, {"encoding": "jsonParsed",
                                          "maxSupportedTransactionVersion": 1,
                                          "commitment": "confirmed"}])
    return о.get("result") if о.get("ok") else None


def кошелёк_позиции(п: dict, по_умолчанию: str = "") -> str:
    for поле in ("wallet", "lane_wallet", "own_send_wallet"):
        зн = п.get(поле)
        if isinstance(зн, str) and зн:
            return зн
    return по_умолчанию


# --------------------------------------------------------- таблица

def сделка_по_цепи(п: dict, *, кошелёк: str) -> dict:
    """Одна позиция: обе транзакции по цепи и деньги по ним."""
    из_ = {"cid": п.get("client_order_id"), "минт": п.get("mint"),
            "источник": п.get("source"), "имя_источника": п.get("source_name"),
            "группа": п.get("lane_group"), "кошелёк": кошелёк,
            "utc": (time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                   time.gmtime(float(п.get("ts_sent")
                                                      or п.get("ts_intent") or 0)))),
            "строитель": п.get("pool_program"),
            "подпись_покупки": None, "подпись_продажи": None,
            "ушло_sol": None, "вернулось_sol": None, "итог_sol": None,
            "удержание_слотов": None, "статус": None, "тревога": False}
    п_пок, п_прод = подписи_покупки(п), подписи_продажи(п)
    села = севшие(п_пок + п_прод)
    пок = next((с for с in п_пок if с in села), None)
    прод = next((с for с in п_прод if с in села), None)
    из_["подпись_покупки"], из_["подпись_продажи"] = пок, прод
    if not пок:
        из_.update(статус="покупка не найдена на цепи", тревога=True)
        return из_
    tx_пок = транзакция(пок)
    д_пок = дельта_наших(tx_пок or {}, кошелёк)
    if not д_пок["ok"]:
        из_.update(статус=f"покупка не считается: {д_пок['why_not']}", тревога=True)
        return из_
    из_["ушло_sol"] = round(-д_пок["lamports"] / ЛАМПОРТОВ_В_SOL, 9)
    из_["слот_покупки"] = д_пок.get("slot")
    if not прод:
        из_.update(статус="продажа не найдена на цепи", тревога=True)
        return из_
    tx_прод = транзакция(прод)
    д_прод = дельта_наших(tx_прод or {}, кошелёк)
    if not д_прод["ok"]:
        из_.update(статус=f"продажа не считается: {д_прод['why_not']}",
                    тревога=True)
        return из_
    из_["вернулось_sol"] = round(д_прод["lamports"] / ЛАМПОРТОВ_В_SOL, 9)
    из_["слот_продажи"] = д_прод.get("slot")
    итог = (д_пок["lamports"] + д_прод["lamports"]) / ЛАМПОРТОВ_В_SOL
    из_["итог_sol"] = round(итог, 9)
    из_["расход_минус_возврат_sol"] = round(-итог, 9)
    if isinstance(д_пок.get("slot"), int) and isinstance(д_прод.get("slot"), int):
        из_["удержание_слотов"] = int(д_прод["slot"]) - int(д_пок["slot"])
    из_["статус"] = "по цепи"
    return из_


def свод(ряды: list) -> dict:
    считанные = [р for р in ряды if р.get("итог_sol") is not None]
    по_источникам: dict = {}
    for р in считанные:
        ключ = р.get("источник") or р.get("имя_источника") or "источника нет"
        с_ = по_источникам.setdefault(ключ, {
            "адрес": р.get("источник"), "имя": р.get("имя_источника"),
            "n": 0, "сумма_sol": 0.0, "лучшая_sol": None, "в_плюсе": 0,
            "группы": {}})
        с_["n"] += 1
        с_["сумма_sol"] = round(с_["сумма_sol"] + float(р["итог_sol"]), 9)
        if с_["лучшая_sol"] is None or float(р["итог_sol"]) > с_["лучшая_sol"]:
            с_["лучшая_sol"] = round(float(р["итог_sol"]), 9)
        if float(р["итог_sol"]) > 0:
            с_["в_плюсе"] += 1
        гр = р.get("группа") or "?"
        с_["группы"][гр] = int(с_["группы"].get(гр, 0)) + 1
    for _, с_ in по_источникам.items():
        с_["доля_в_плюсе"] = (round(с_["в_плюсе"] / с_["n"], 4) if с_["n"] else None)
    тревоги = [р for р in ряды if р.get("тревога")]
    return {"сделок": len(ряды), "посчитано_по_цепи": len(считанные),
             "сумма_sol": round(sum(float(р["итог_sol"]) for р in считанные), 9),
             "тревог": len(тревоги),
             "по_источникам": dict(sorted(по_источникам.items(),
                                           key=lambda т: -т[1]["сумма_sol"])),
             "тревоги": [{"cid": р["cid"], "минт": р.get("минт"),
                           "статус": р.get("статус"),
                           "подпись_покупки": р.get("подпись_покупки"),
                           "подпись_продажи": р.get("подпись_продажи")}
                          for р in тревоги]}


def разобрать_время(текст: str):
    if not текст:
        return None
    try:
        return float(calendar.timegm(time.strptime(текст, "%Y-%m-%dT%H:%M:%SZ")))
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    р = argparse.ArgumentParser(description=__doc__)
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--s", dest="s", default="2026-09-28T00:21:00Z")
    р.add_argument("--po", dest="po", default="")
    р.add_argument("--istochnik", default="",
                    help="только этот источник (адрес или имя)")
    р.add_argument("--out", default="")
    а = р.parse_args()
    с_ts, по_ts = разобрать_время(а.s), разобрать_время(а.po)
    поз = позиции_полосы(а.state_dir, с_ts, по_ts)
    по_умолчанию = (os.environ.get("OWN_SEND_WALLET")
                     or os.environ.get("BLOOM_LANE_WALLET") or "")
    ряды = []
    for cid, п in sorted(поз.items(),
                          key=lambda т: float(т[1].get("ts_sent")
                                               or т[1].get("ts_intent") or 0)):
        if а.istochnik and а.istochnik not in (п.get("source"),
                                                п.get("source_name")):
            continue
        ряды.append(сделка_по_цепи(п, кошелёк=кошелёк_позиции(п, по_умолчанию)))
    итог = {"с": а.s, "по": а.po or None, "источник": а.istochnik or None,
             "свод": свод(ряды), "сделки": ряды}
    текст = json.dumps(итог, ensure_ascii=False, indent=1)
    if а.out:
        Path(а.out).write_text(текст, encoding="utf-8")
    краткое = {"с": итог["с"], "свод": итог["свод"]}
    print(json.dumps(краткое, ensure_ascii=False, indent=1)[:6000])
    return 0


def self_test() -> int:
    """Арифметика денег и выбор подписей. Сети здесь нет."""
    пройдено = провалено = 0

    def chk(что, ок, факт=""):
        nonlocal пройдено, провалено
        if ок:
            пройдено += 1
            print(f"  [ok  ] {что}")
        else:
            провалено += 1
            print(f"  [ПРОВАЛ] {что}: {факт}")

    НАШ = "КОШЕЛЁК"
    # Покупка: с кошелька ушло 0.3 SOL, ещё 0.002 осело на созданном НАШЕМ
    # токен-счёте (рента), комиссия 0.001005 внутри дельты кошелька.
    tx_пок = {"slot": 1000, "blockTime": 100,
               "transaction": {"message": {"accountKeys": [
                   {"pubkey": НАШ}, {"pubkey": "ТОКЕНСЧЁТ"}, {"pubkey": "ПУЛ"}]}},
               "meta": {"fee": 1_005_000, "err": None,
                         "preBalances": [1_000_000_000, 0, 5_000_000_000],
                         "postBalances": [698_995_000, 2_000_000, 5_300_000_000],
                         "preTokenBalances": [],
                         "postTokenBalances": [
                             {"accountIndex": 1, "owner": НАШ, "mint": "МИНТ"}]}}
    д = дельта_наших(tx_пок, НАШ)
    chk("покупка: дельта наших счетов -- кошелёк ПЛЮС наш токен-счёт",
        д["ok"] and д["lamports"] == (698_995_000 - 1_000_000_000) + 2_000_000
        and д["счетов"] == 2, д)
    # Продажа: вернулось 0.34 на кошелёк, токен-счёт закрыт (рента вернулась).
    tx_прод = {"slot": 1108, "blockTime": 130,
                "transaction": {"message": {"accountKeys": [
                    {"pubkey": НАШ}, {"pubkey": "ТОКЕНСЧЁТ"}, {"pubkey": "ПУЛ"}]}},
                "meta": {"fee": 1_005_000, "err": None,
                          "preBalances": [698_995_000, 2_000_000, 5_300_000_000],
                          "postBalances": [1_040_990_000, 0, 4_960_000_000],
                          "preTokenBalances": [
                              {"accountIndex": 1, "owner": НАШ, "mint": "МИНТ"}],
                          "postTokenBalances": []}}
    д2 = дельта_наших(tx_прод, НАШ)
    chk("продажа: вернувшаяся рента закрытого счёта тоже в дельте",
        д2["ok"] and д2["lamports"] == (1_040_990_000 - 698_995_000) - 2_000_000,
        д2)
    chk("комиссия НЕ вычищается: она и есть потраченные деньги",
        д["fee"] == 1_005_000 and д2["fee"] == 1_005_000)
    chk("чужого счёта в наших нет",
        наши_индексы(tx_пок, НАШ)["индексы"] == [0, 1],
        наши_индексы(tx_пок, НАШ)["индексы"])
    chk("кошелька нет в транзакции -- отказ словами",
        дельта_наших(tx_пок, "ЧУЖОЙ")["ok"] is False)

    # Подписи: берутся все варианты, без повторов и пустых.
    п = {"lane_landed_signature": "П1", "lane_signature": "П1",
          "lane_pool_candidates": {"a": "П2", "b": None},
          "last_sell_signatures": ["С1", "С2"], "last_sell_reported": "С2"}
    chk("подписи покупки -- без повторов и пустых",
        подписи_покупки(п) == ["П1", "П2"], подписи_покупки(п))
    chk("подписи продажи -- доложенная первой, потом остальные",
        подписи_продажи(п) == ["С2", "С1"], подписи_продажи(п))

    # Свод: сумма, лучшая, доля в плюсе, тревоги.
    ряды = [{"cid": "1", "итог_sol": 0.05, "источник": "И1", "группа": "g"},
             {"cid": "2", "итог_sol": -0.02, "источник": "И1", "группа": "g"},
             {"cid": "3", "итог_sol": None, "источник": "И2", "группа": "g",
               "статус": "продажа не найдена на цепи", "тревога": True}]
    с = свод(ряды)
    chk("свод: посчитано 2 из 3, сумма 0.03, тревог 1",
        с["посчитано_по_цепи"] == 2 and abs(с["сумма_sol"] - 0.03) < 1e-9
        and с["тревог"] == 1, с)
    chk("по источнику: лучшая 0.05, доля в плюсе 0.5",
        с["по_источникам"]["И1"]["лучшая_sol"] == 0.05
        and с["по_источникам"]["И1"]["доля_в_плюсе"] == 0.5,
        с["по_источникам"]["И1"])
    chk("источник без счёта в таблицу сумм не попадает",
        "И2" not in с["по_источникам"], с["по_источникам"].keys())
    chk("окно читается как UTC",
        разобрать_время("2026-09-28T00:21:00Z") == 1790554860.0)
    print(f"самопроверка Правила 11 по цепи: {пройдено}/{пройдено + провалено} пройдено")
    return 1 if провалено else 0


if __name__ == "__main__":
    raise SystemExit(self_test() if "--self-test" in sys.argv else main())
