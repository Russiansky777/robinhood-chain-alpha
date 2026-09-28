#!/usr/bin/env python3
"""Пулы ПЕРВОЙ НОГИ (SOL <-> котировка) для ВСЕХ котировок наших источников.

ЗАЧЕМ (слово владельца 28.09, ночь): "подписать на все пулы первой ноги из
data/podbivka/kotirovki_grupp.json (Code-2), не только 57 случайных; кредиты не
ограничены. Для главных котировок (USDC, USDT, GP и топ-10 по частоте) --
статичные шаблоны первой ноги из канонических пулов, обновляемые чтением
состояния раз в N секунд".

ЧТО БЫЛО. Кэш ног жил на файле data/c2_leg_pools_2026-09-24.json -- 57 пулов,
собранных 24.09 по маршрутам источников задачи A. Котировки лидера 28.09 в него
не попали: три покупки BATCH-5 этой ночи (jizz, KARDASHEV, e/acc) встали на
"шаблона SOL -> Xsa62P5m в кэше нет". Список котировок есть у Code-2 -- 146
котировок с частотой по кошелькам, но БЕЗ пулов первой ноги (поле "первая_нога"
пустое во всех 146). Пулы ищет этот прогон.

КАК (только чтение цепи):
  * котировки -- из файла Code-2 (котировки_группы + лидер_7_дней), по частоте;
  * пул SOL <-> Q -- по свежим транзакциям минта Q: инструкции поддерживаемых
    программ, где одно хранилище WSOL, другое Q и владелец хранилищ не
    подписант. Из нескольких берётся пул с самым глубоким хранилищем WSOL
    (getMultipleAccounts по остаткам);
  * для ГЛАВНЫХ котировок сверх этого -- канонический пул с ПОСТОЯННЫМ
    ПРОИЗВЕДЕНИЕМ (Pump AMM, CPMM, AMM v4, LaunchLab) и одна настоящая сделка
    этого пула как образец шаблона: раскладка счетов у таких пулов от цены не
    зависит, поэтому шаблон годен всегда, а цену служба потом читает по
    остаткам хранилищ раз в N секунд. Пулы с сосредоточенной ликвидностью
    (DLMM, CLMM, DAMM v2, DBC) в статичные шаблоны НЕ берутся: там остаток
    хранилища -- не резерв, и цена по нему была бы выдумкой;
  * символ и имя котировки -- DAS getAsset (Helius), чтобы в отчёте владельцу
    стояли USDC/USDT/GP словами, а не base58.

Разбор транзакций -- теми же функциями, что у полосы (c2_swap_build,
c2_shadow_build.LegCache._template_from): образец, который здесь признан
шаблоном, обязан быть шаблоном и в службе, иначе файл ничего не стоит.

Ни одной отправки. Измерительный код и сбор данных (правило 8).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

WSOL = "So11111111111111111111111111111111111111112"
ВЕРСИЯ_TX = 1
# Программы, у которых остаток хранилища -- резерв: цену можно честно взять
# отношением остатков. Остальные -- сосредоточенная ликвидность.
РЕЗЕРВНЫЕ = ("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",   # Pump AMM
             "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",   # Raydium CPMM
             "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",   # Raydium AMM v4
             "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj")    # Raydium LaunchLab
# Котировки, которые владелец назвал главными по имени.
ГЛАВНЫЕ_ПО_ИМЕНИ = ("USDC", "USDT", "GP")
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"


def узел() -> str:
    к = (os.environ.get("HELIUS_API") or os.environ.get("HELIUS_API_KEY") or "").strip()
    if not к:
        raise SystemExit("СБОЙ: ключ Helius не задан в окружении")
    return f"https://mainnet.helius-rpc.com/?api-key={к}"


СЧЁТЧИК = {"вызовов": 0, "ошибок": 0}


def rpc(метод: str, параметры, *, повторов: int = 3):
    тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                       "params": параметры}).encode()
    пауза, последняя = 0.35, None
    for _ in range(повторов):
        СЧЁТЧИК["вызовов"] += 1
        req = urllib.request.Request(узел(), data=тело,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                о = json.loads(r.read().decode())
            if "error" in о:
                последняя = str(о["error"])[:200]
            else:
                return о.get("result")
        except Exception as exc:  # noqa: BLE001
            последняя = type(exc).__name__
        СЧЁТЧИК["ошибок"] += 1
        time.sleep(пауза)
        пауза *= 2
    return {"__почему_нет": последняя}


def транзакция(подпись: str):
    о = rpc("getTransaction", [подпись, {"encoding": "jsonParsed",
                                          "maxSupportedTransactionVersion": ВЕРСИЯ_TX,
                                          "commitment": "confirmed"}])
    return о if isinstance(о, dict) and "meta" in о else None


def подписи(адрес: str, предел: int = 15) -> list:
    о = rpc("getSignaturesForAddress", [адрес, {"limit": предел,
                                                 "commitment": "confirmed"}])
    if not isinstance(о, list):
        return []
    return [x["signature"] for x in о if x.get("err") is None and x.get("signature")]


def остатки(счета: list) -> dict:
    из_ = {}
    for i in range(0, len(счета), 100):
        часть = счета[i:i + 100]
        о = rpc("getMultipleAccounts", [часть, {"encoding": "jsonParsed"}])
        значения = (о or {}).get("value") or [] if isinstance(о, dict) else []
        for а, v in zip(часть, значения):
            инф = ((((v or {}).get("data") or {}).get("parsed") or {}).get("info") or {})
            сумма = (инф.get("tokenAmount") or {})
            из_[а] = {"ui": сумма.get("uiAmount"), "amount": сумма.get("amount"),
                       "dec": сумма.get("decimals"), "mint": инф.get("mint")}
    return из_


def имена(минты: list) -> dict:
    """Символ и имя котировки -- DAS getAsset, по одному вызову на минт."""
    из_ = {}
    for м in минты:
        о = rpc("getAsset", {"id": м})
        мета = (((о or {}).get("content") or {}).get("metadata") or {}) if isinstance(о, dict) else {}
        из_[м] = {"символ": мета.get("symbol"), "имя": мета.get("name")}
    return из_


# ---------------------------------------------------------------- котировки

def котировки_из_файла(путь: str) -> list:
    """[(минт, покупок)] по убыванию частоты: список Code-2 плюс лидер за 7 дней."""
    d = json.loads(Path(путь).read_text(encoding="utf-8"))
    счёт: dict = {}
    for x in d.get("котировки_группы") or []:
        м = x.get("quote_mint")
        if м:
            счёт[м] = max(счёт.get(м, 0), int(x.get("покупок") or 0))
    for x in ((d.get("лидер_7_дней") or {}).get("котировки") or []):
        м = x.get("quote_mint")
        if м:
            счёт[м] = max(счёт.get(м, 0), int(x.get("покупок") or 0))
    счёт.pop(WSOL, None)
    счёт.pop("native_sol", None)
    return sorted(счёт.items(), key=lambda кв: -кв[1])


# ------------------------------------------------------------ поиск пулов ноги

def пулы_ноги_в_tx(tx: dict, C, B, программы) -> list:
    """(программа, хранилище Q, хранилище WSOL, Q) для пулов SOL<->Q в маршруте.

    Правило то же, что у c2_twohop второй сессии (её файл 57 пулов собран им):
    хранилища -- не подписантские счета инструкции, одно WSOL, другое Q.
    """
    строки = {r["account"]: r for r in C.token_rows(tx).values()}
    подписанты = set(C.signers(tx))
    из_ = []
    for ix in B.all_instructions(tx):
        if ix.get("programId") not in программы:
            continue
        свои = [строки[а] for а in (ix.get("accounts") or [])
                if а in строки and строки[а]["owner"] not in подписанты]
        w = [r for r in свои if r["mint"] == WSOL]
        q = [r for r in свои if r["mint"] != WSOL]
        for qr in q:
            for wr in w:
                # У пулов с одним владельцем хранилищ (CPMM, Pump AMM, AMM v4,
                # LaunchLab) требуем совпадения владельца; у DLMM/DAMM v2/CLMM
                # хранилища принадлежат разным счетам программы.
                if qr["owner"] == wr["owner"] or ix["programId"] in программы_разных_владельцев:
                    из_.append((ix["programId"], qr["account"], wr["account"], qr["mint"]))
    return из_


программы_разных_владельцев: set = set()


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--code-dir", default="/home/bot/bloom_executor",
                   help="каталог модулей службы (c2_swap_build, c2_shadow_build)")
    р.add_argument("--kotirovki", required=True, help="файл Code-2 kotirovki_grupp.json")
    р.add_argument("--leg-pools", default="", help="прежний c2_leg_pools_*.json (слияние)")
    р.add_argument("--skolko-kotirovok", type=int, default=0,
                   help="0 -- все котировки файла")
    р.add_argument("--podpisey-na-kotirovku", type=int, default=15)
    р.add_argument("--glavnyh", type=int, default=10,
                   help="сколько котировок по частоте считать главными (плюс USDC, USDT, GP)")
    р.add_argument("--podpisey-na-obrazec", type=int, default=25,
                   help="сколько сделок канонического пула перебрать в поисках образца шаблона")
    р.add_argument("--out-pools", required=True)
    р.add_argument("--out-shablony", required=True)
    р.add_argument("--out-otchet", required=True)
    а = р.parse_args()

    sys.path.insert(0, а.code_dir)
    import c2_common as C  # noqa: PLC0415
    import c2_shadow_build as SB  # noqa: PLC0415
    import c2_swap_build as B  # noqa: PLC0415
    import c2_pool_programs as PP  # noqa: PLC0415

    метки = PP.labels()
    программы = {B.PUMP_AMM, B.CPMM, B.DAMM2, B.LAUNCHLAB, B.DLMM, B.CLMM, B.DBC}
    global программы_разных_владельцев
    программы_разных_владельцев = {B.DLMM, B.DAMM2, B.CLMM, B.DBC}

    список = котировки_из_файла(а.kotirovki)
    if а.skolko_kotirovok > 0:
        список = список[:а.skolko_kotirovok]
    прежние = {}
    if а.leg_pools and Path(а.leg_pools).exists():
        прежние = json.loads(Path(а.leg_pools).read_text(encoding="utf-8")).get("pools") or {}

    t0 = time.time()

    def кандидаты_котировки(q: str):
        """Пулы SOL<->Q по свежим сделкам минта. Сеть -- только чтение."""
        свои, прочитано_q = {}, 0
        for sg in подписи(q, а.podpisey_na_kotirovku):
            tx = транзакция(sg)
            if tx is None:
                continue
            прочитано_q += 1
            for прог, qv, wv, q2 in пулы_ноги_в_tx(tx, C, B, программы):
                if q2 != q:
                    continue
                ключ = (прог, qv, wv)
                свои[ключ] = свои.get(ключ, 0) + 1
        return q, свои, прочитано_q

    # Котировок 146, на каждую до 16 вызовов: последовательно это больше
    # получаса. Восемь потоков -- то же число, что у LegCache.refresh_all.
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
    кандидаты: dict = {}     # Q -> {(программа, q_vault, w_vault): встреч}
    прочитано: dict = {}     # Q -> сколько транзакций прочитано
    with ThreadPoolExecutor(max_workers=8) as ex:
        for q, свои, п in ex.map(кандидаты_котировки, [q for q, _ in список]):
            кандидаты[q] = свои
            прочитано[q] = п

    хранилища = sorted({v for q in кандидаты.values() for k in q for v in (k[1], k[2])})
    бал = остатки(хранилища) if хранилища else {}

    пулы: dict = {}
    отчёт_котировок = []
    for q, ч in список:
        сорт = sorted(кандидаты.get(q, {}),
                      key=lambda k: -((бал.get(k[2]) or {}).get("ui") or 0))
        строка = {"quote_mint": q, "покупок": ч, "транзакций_прочитано": прочитано.get(q, 0),
                   "кандидатов": len(сорт)}
        if сорт:
            прог, qv, wv = сорт[0]
            пулы[q] = {"program": прог, "q_vault": qv, "w_vault": wv,
                        "sol_depth": (бал.get(wv) or {}).get("ui")}
            строка.update(программа=метки.get(прог, прог), q_vault=qv, w_vault=wv,
                           sol_depth=(бал.get(wv) or {}).get("ui"), состояние="пул найден")
        elif q in прежние:
            пулы[q] = прежние[q]
            строка.update(состояние="пул из прежнего файла (свежих сделок минта не нашлось)",
                           программа=метки.get(прежние[q].get("program"), прежние[q].get("program")))
        else:
            строка["состояние"] = ("пула SOL<->Q поддерживаемого типа в свежих сделках "
                                    "минта нет -- первой ноги для этой котировки не будет")
        отчёт_котировок.append(строка)
    # Прежние котировки, которых в списке Code-2 нет вовсе, из файла не теряем:
    # подписка на них уже работала, и снимать её этим прогоном никто не просил.
    добавлено_из_прежних = 0
    for q, п in прежние.items():
        if q not in пулы:
            пулы[q] = п
            добавлено_из_прежних += 1

    # ------------------------------------------------ главные котировки: образцы
    главные = [q for q, _ in список[:max(0, а.glavnyh)]]
    имена_всех = имена([q for q, _ in список[:max(20, а.glavnyh)]])
    for q, инф in имена_всех.items():
        if (инф.get("символ") or "").upper() in ГЛАВНЫЕ_ПО_ИМЕНИ and q not in главные:
            главные.append(q)
    for м in (USDC, USDT):
        if м in dict(список) and м not in главные:
            главные.append(м)

    шаблоны: dict = {}
    отчёт_главных = []
    for q in главные:
        сорт = sorted([k for k in кандидаты.get(q, {}) if k[0] in РЕЗЕРВНЫЕ],
                      key=lambda k: -((бал.get(k[2]) or {}).get("ui") or 0))
        имя = имена_всех.get(q) or {}
        стр = {"quote_mint": q, "символ": имя.get("символ"), "имя": имя.get("имя"),
                "покупок": dict(список).get(q)}
        if not сорт:
            свой = пулы.get(q) or {}
            стр["состояние"] = (
                "канонического пула с постоянным произведением не нашлось; "
                f"лучший пул -- {метки.get(свой.get('program'), свой.get('program'))}: "
                "там остаток хранилища не резерв, цену по остаткам считать нельзя, "
                "статичный шаблон не строим -- шаблон придёт из подписки")
            отчёт_главных.append(стр)
            continue
        прог, qv, wv = сорт[0]
        п = {"program": прог, "q_vault": qv, "w_vault": wv,
             "sol_depth": (бал.get(wv) or {}).get("ui")}
        стр.update(программа=метки.get(прог, прог), q_vault=qv, w_vault=wv,
                    sol_depth=п["sol_depth"])
        # Образец -- ТОЙ ЖЕ функцией, которой служба признаёт шаблон.
        образец, причины = None, {}
        for sg in подписи(qv, а.podpisey_na_obrazec):
            tx = транзакция(sg)
            if tx is None:
                причины["транзакцию узел не отдал"] = причины.get("транзакцию узел не отдал", 0) + 1
                continue
            запись = SB.LegCache._template_from(tx, п, q)
            if isinstance(запись, str):
                причины[запись] = причины.get(запись, 0) + 1
                continue
            образец = {"signature": sg, "tx": tx,
                        "q_dec": запись.get("q_dec"),
                        "price_sol_v_sdelke": float(запись["price_sol"]),
                        "program": прог, "q_vault": qv, "w_vault": wv,
                        "base_program": (запись.get("mv") or {}).get("base_program")}
            break
        if образец is None:
            стр["состояние"] = ("в последних сделках канонического пула шаблона не "
                                 "вышло: " + "; ".join(f"{k} x{v}" for k, v in причины.items()))
        else:
            шаблоны[q] = dict(образец, символ=имя.get("символ"), имя=имя.get("имя"))
            стр["состояние"] = "образец шаблона взят"
            стр["образец"] = образец["signature"]
            стр["цена_в_сделке_sol"] = образец["price_sol_v_sdelke"]
        отчёт_главных.append(стр)

    Path(а.out_pools).write_text(json.dumps(
        {"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "откуда": ("все котировки из data/podbivka/kotirovki_grupp.json (Code-2) "
                     "плюс прежний файл; пул -- самое глубокое хранилище WSOL "
                     "среди пулов SOL<->Q в свежих сделках минта"),
         "pools": пулы}, ensure_ascii=False, indent=1), encoding="utf-8")
    Path(а.out_shablony).write_text(json.dumps(
        {"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "зачем": ("статичные шаблоны первой ноги для главных котировок: "
                    "раскладка счетов у пулов с постоянным произведением от цены "
                    "не зависит, поэтому образец годен всегда, а цена читается "
                    "по остаткам хранилищ раз в N секунд"),
         "shablony": шаблоны}, ensure_ascii=False, indent=1), encoding="utf-8")
    итог = {"снято_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "секунд": round(time.time() - t0, 1),
             "вызовов_узла": СЧЁТЧИК["вызовов"], "ошибок_узла": СЧЁТЧИК["ошибок"],
             "котировок_в_файле_code2": len(список),
             "пулов_нашлось": len(пулы),
             "из_них_из_прежнего_файла": добавлено_из_прежних,
             "главных_котировок": len(главные),
             "статичных_шаблонов": len(шаблоны),
             "по_котировкам": отчёт_котировок,
             "главные": отчёт_главных}
    Path(а.out_otchet).write_text(json.dumps(итог, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    print(json.dumps({k: v for k, v in итог.items()
                      if k not in ("по_котировкам", "главные")}, ensure_ascii=False))
    for стр in отчёт_главных:
        print(json.dumps(стр, ensure_ascii=False)[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main())
