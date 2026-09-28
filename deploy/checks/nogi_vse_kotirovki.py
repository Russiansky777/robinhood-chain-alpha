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
import base64
import json
import os
import sys
import time
import urllib.parse
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


def пул_через_jupiter(q: str, лампорты: int = 300_000_000) -> dict:
    """Адрес пула SOL -> Q у Jupiter (только УКАЗАТЕЛЬ, проверка -- по цепи).

    ЗАЧЕМ. Пул с постоянным произведением для USDC по истории минта не находится:
    подписи минта USDC -- это переводы и агрегаторы, а не свопы конкретного пула
    (замер 28.09: 300 транзакций, 19 пулов, ни одного CPMM или Pump AMM).
    Jupiter на прямом маршруте называет ammKey -- адрес пула. Дальше мы читаем
    ЕГО СОБСТВЕННЫЕ сделки по цепи и разбираем их теми же функциями, что полоса:
    ни одного числа от Jupiter в деньги не идёт, он только показывает, куда
    смотреть.
    """
    из_ = {"ok": False, "pools": [], "why_not": None}
    базы = ("https://lite-api.jup.ag/swap/v1", "https://api.jup.ag/swap/v1")
    # ПРЯМОЙ МАРШРУТ БЕЗ ОТБОРА ДАЁТ ЧТО УГОДНО: 28.09 у SOL -> USDC это был
    # "Kipseli" -- программа вне нашего словаря. Поэтому спрашиваем ОТДЕЛЬНО по
    # площадкам, которые мы умеем собирать, и берём их адреса пулов.
    отборы = ("Raydium CP", "Raydium", "Pump.fun Amm", "Raydium Launchlab", "")
    for база in базы:
        ошибка = None
        for отбор in отборы:
            запрос = (f"/quote?inputMint={WSOL}&outputMint={q}&amount={лампорты}"
                      f"&slippageBps=50&onlyDirectRoutes=true")
            if отбор:
                запрос += "&dexes=" + urllib.parse.quote(отбор)
            try:
                req = urllib.request.Request(база + запрос,
                                             headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    о = json.loads(r.read().decode())
            except Exception as exc:  # noqa: BLE001
                ошибка = f"{база.split('//')[1].split('/')[0]} [{отбор or 'без отбора'}]: {type(exc).__name__}"
                continue
            for шаг in (о.get("routePlan") or []):
                инф = шаг.get("swapInfo") or {}
                если_есть = {п["pool"] for п in из_["pools"]}
                if инф.get("ammKey") and инф["ammKey"] not in если_есть:
                    из_["pools"].append({"pool": инф["ammKey"],
                                          "label": инф.get("label"),
                                          "dexes": отбор or "без отбора"})
        из_["ok"] = bool(из_["pools"])
        из_["why_not"] = None if из_["ok"] else ошибка
        из_["from"] = база
        if из_["ok"]:
            return из_
    return из_


def образцы_пула(адрес: str, сколько: int, C, B, программы) -> list:
    """Пулы SOL<->Q из сделок КОНКРЕТНОГО пула (адрес -- указатель, не число)."""
    найдено = []
    for sg in подписи(адрес, сколько):
        tx = транзакция(sg)
        if tx is None:
            continue
        for прог, qv, wv, q2 in пулы_ноги_в_tx(tx, C, B, программы):
            if (прог, qv, wv, q2) not in найдено:
                найдено.append((прог, qv, wv, q2))
    return найдено


def глубокий_поиск_пула(q: str, сколько: int, C, B, программы) -> tuple:
    """Пулы SOL<->Q по ИСТОРИИ минта страницами. (кандидаты, прочитано).

    Читаем подписи минта страницами по 100 (before=последняя) и разбираем те же
    инструкции, что и в основном проходе. Останавливаемся, как только нашёлся
    пул с постоянным произведением: больше нам и не нужно.
    """
    найдено, прочитано, before = [], 0, None
    while прочитано < сколько:
        параметры = {"limit": min(100, сколько - прочитано), "commitment": "confirmed"}
        if before:
            параметры["before"] = before
        о = rpc("getSignaturesForAddress", [q, параметры])
        стр = о if isinstance(о, list) else []
        if not стр:
            break
        before = стр[-1].get("signature")
        for x in стр:
            if x.get("err") is not None or not x.get("signature"):
                continue
            tx = транзакция(x["signature"])
            прочитано += 1
            if tx is None:
                continue
            for прог, qv, wv, q2 in пулы_ноги_в_tx(tx, C, B, программы):
                if q2 == q and (прог, qv, wv) not in найдено:
                    найдено.append((прог, qv, wv))
            if any(k[0] in РЕЗЕРВНЫЕ for k in найдено):
                return найдено, прочитано
    return найдено, прочитано


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
    р.add_argument("--podpisey-na-obrazec", type=int, default=60,
                   help="сколько сделок канонического пула перебрать в поисках образца шаблона")
    р.add_argument("--glubokiy-poisk", type=int, default=200,
                   help="для главных котировок без пула с постоянным произведением -- "
                        "сколько сделок минта пролистать страницами")
    р.add_argument("--razbor-podpisi", default="",
                   help="подписи через запятую: разобрать их инструкции (нужно для "
                        "образцов продажи Pump AMM -- наши продажи шли через Jupiter, "
                        "и внутри них лежит настоящая инструкция sell)")
    р.add_argument("--sohranit-obrazcy", default="",
                   help="куда сохранить транзакции-образцы по видам инструкций")
    р.add_argument("--razbor-pula", default="",
                   help="адрес пула: разобрать его сделки по числу счетов инструкции "
                        "(зачем: у Pump AMM встречается 25 счетов вместо 26)")
    р.add_argument("--tolko", default="",
                   help="считать только эти котировки (минты через запятую)")
    р.add_argument("--konfigi-clmm", default="",
                   help="счета amm_config Raydium CLMM через запятую: прочитать "
                         "и разобрать ставку комиссии и шаг тика (только чтение)")
    р.add_argument("--out-konfigi", default="",
                   help="куда положить разбор счетов amm_config")
    р.add_argument("--out-pools", required=True)
    р.add_argument("--out-shablony", required=True)
    р.add_argument("--out-otchet", required=True)
    а = р.parse_args()

    sys.path.insert(0, а.code_dir)
    # СВОЙ КАТАЛОГ ПЕРВЫМ: рядом с прогоном лежат свежие модули, доставленные
    # тем же ssh. Без этого хост брал бы РАЗВЁРНУТУЮ версию, и проверка новой
    # раскладки проверяла бы старый код (так уже вышло 28.09 с докладчиком).
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import c2_common as C  # noqa: PLC0415
    import c2_shadow_build as SB  # noqa: PLC0415
    import c2_swap_build as B  # noqa: PLC0415
    import c2_pool_programs as PP  # noqa: PLC0415

    метки = PP.labels()
    программы = {B.PUMP_AMM, B.CPMM, B.DAMM2, B.LAUNCHLAB, B.DLMM, B.CLMM, B.DBC}
    global программы_разных_владельцев
    программы_разных_владельцев = {B.DLMM, B.DAMM2, B.CLMM, B.DBC}

    # СЧЕТА amm_config RAYDIUM CLMM: только чтение, печать и выход.
    #
    # ЗАЧЕМ. Ставка комиссии пула CLMM НЕ ЛЕЖИТ В СОБЫТИИ СВОПА: программа
    # читает её из счёта amm_config (счёт 1 самой инструкции свопа). По одной
    # сделке ставку решить нельзя -- у сделки, перешедшей границу диапазона
    # ликвидности, решённая доля уходит на проценты в сторону. Поэтому минимум
    # выхода CLMM без этой ставки отказывает, и раскладку счёта надо
    # ПОДТВЕРДИТЬ, а не заявить: у каждого конфига, чью ставку удалось решить
    # по живым сделкам (1000, 2500, 10000, 40000 миллионных), прочитанный
    # trade_fee_rate обязан совпасть с решённым числом.
    if а.konfigi_clmm:
        import c2_cl_quote as CL  # noqa: PLC0415
        список_к = [x.strip() for x in а.konfigi_clmm.split(",") if x.strip()]
        из_к = {"снято_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "зачем": ("ставка комиссии и шаг тика пулов Raydium CLMM из "
                            "счетов amm_config -- денежный путь минимума выхода"),
                 "смещения": CL.СХЕМА_КОНФИГА_CLMM, "конфиги": {}}
        for i in range(0, len(список_к), 100):
            часть = список_к[i:i + 100]
            о = rpc("getMultipleAccounts", [часть, {"encoding": "base64"}])
            значения = (о or {}).get("value") or [] if isinstance(о, dict) else []
            for адрес, v in zip(часть, значения):
                данные_b64 = (((v or {}).get("data") or [None])[0])
                if not данные_b64:
                    из_к["конфиги"][адрес] = {"why_not": "счёта нет или он пуст"}
                    continue
                сырое = base64.b64decode(данные_b64)
                разбор = CL.конфиг_clmm(сырое)
                из_к["конфиги"][адрес] = {
                    "владелец": (v or {}).get("owner"),
                    "байт": len(сырое),
                    "разбор": разбор or None,
                    "why_not": None if разбор else "раскладка не подтвердилась",
                    # ПЕРВЫЕ 96 БАЙТ -- чтобы раскладку можно было вывести
                    # офлайн, если заявленные смещения окажутся не те. Это
                    # ОБЩЕДОСТУПНОЕ состояние счёта настроек, не ключ.
                    "начало_hex": сырое[:96].hex()}
        куда = а.out_konfigi or а.out_otchet
        Path(куда).write_text(json.dumps(из_к, ensure_ascii=False, indent=1),
                               encoding="utf-8")
        print(json.dumps({адрес: (з.get("разбор") or з.get("why_not"))
                           for адрес, з in из_к["конфиги"].items()},
                          ensure_ascii=False)[:2000])
        return 0

    # РАЗБОР ОДНОГО ПУЛА: только чтение, печать и выход. Нужен, чтобы понять
    # РАЗНИЦУ между раскладками счетов одной и той же инструкции.
    if а.razbor_pula or а.razbor_podpisi:
        из_ = {"пул": а.razbor_pula or None,
                "подписи_входом": bool(а.razbor_podpisi), "сделки": []}
        список_подписей = ([x.strip() for x in а.razbor_podpisi.split(",") if x.strip()]
                           if а.razbor_podpisi else подписи(а.razbor_pula, 60))
        образцы_по_видам: dict = {}
        for sg in список_подписей:
            tx = транзакция(sg)
            if tx is None:
                continue
            for ix in B.all_instructions(tx):
                if ix.get("programId") not in программы:
                    continue
                данные = ix.get("data") or ""
                try:
                    сырое = B.b58decode(данные)
                except Exception:  # noqa: BLE001
                    continue
                из_["сделки"].append({
                    "signature": sg, "program": ix["programId"],
                    "disc": сырое[:8].hex(),
                    "счетов": len(ix.get("accounts") or []),
                    "accounts": list(ix.get("accounts") or [])})
                # ОБРАЗЕЦ НА ВИД ИНСТРУКЦИИ -- ЦЕЛИКОМ, чтобы раскладку можно
                # было выводить офлайн, а не гадать по списку адресов.
                ключ_о = f"{ix['programId']}:{сырое[:8].hex()}:{len(ix.get('accounts') or [])}"
                if ключ_о not in образцы_по_видам:
                    образцы_по_видам[ключ_о] = {
                        "signature": sg, "program": ix["programId"],
                        "disc": сырое[:8].hex(),
                        "счетов": len(ix.get("accounts") or []),
                        "accounts": list(ix.get("accounts") or []),
                        "tx": tx}
        по_числу: dict = {}
        for з in из_["сделки"]:
            по_числу.setdefault((з["program"], з["disc"], з["счетов"]), []).append(з)
        из_["свод"] = [{"program": к[0], "disc": к[1], "счетов": к[2], "сколько": len(v),
                         "пример": v[0]["signature"]} for к, v in по_числу.items()]
        # РАЗНИЦА РАСКЛАДОК: сравниваем списки счетов двух видов одного
        # дискриминатора -- что именно пропало в коротком.
        for к1, v1 in по_числу.items():
            for к2, v2 in по_числу.items():
                if к1[:2] != к2[:2] or к1[2] >= к2[2]:
                    continue
                короткий, длинный = v1[0]["accounts"], v2[0]["accounts"]
                из_.setdefault("разница", []).append({
                    "disc": к1[1], "короткий": к1[2], "длинный": к2[2],
                    "нет_в_коротком": [а_ for а_ in длинный if а_ not in короткий],
                    "лишние_в_коротком": [а_ for а_ in короткий if а_ not in длинный],
                    "позиции_расхождения": [i for i, (x, y) in
                                             enumerate(zip(короткий, длинный)) if x != y][:6],
                    "пример_короткий": v1[0]["signature"],
                    "пример_длинный": v2[0]["signature"]})
        if а.sohranit_obrazcy and образцы_по_видам:
            Path(а.sohranit_obrazcy).write_text(
                json.dumps({"снято_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                        time.gmtime()),
                             "зачем": ("образцы по видам инструкций: программа, "
                                        "дискриминатор и число счетов -- для вывода "
                                        "раскладки офлайн"),
                             "obrazcy": образцы_по_видам},
                            ensure_ascii=False, indent=1), encoding="utf-8")
            из_["образцов_сохранено"] = len(образцы_по_видам)
        Path(а.out_otchet).write_text(json.dumps(из_, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
        print(json.dumps({"свод": из_["свод"], "разница": из_.get("разница")},
                          ensure_ascii=False)[:2000])
        return 0

    список = котировки_из_файла(а.kotirovki)
    если_только = [x.strip() for x in (а.tolko or "").split(",") if x.strip()]
    if если_только:
        список = [(q, ч) for q, ч in список if q in если_только]
    elif а.skolko_kotirovok > 0:
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

    def ключ_выбора(k):
        """Сначала пулы с постоянным произведением, потом по глубине WSOL.

        ПОЧЕМУ НЕ ПРОСТО ПО ГЛУБИНЕ. Слово владельца 28.09: собираем ноги
        только через CPMM и Pump AMM. У USDC самый глубокий пул -- DLMM
        (9655 SOL), и выбор по глубине отдавал бы ногу, которую мы не соберём,
        при живом пуле CPMM рядом.
        """
        return (0 if k[0] in РЕЗЕРВНЫЕ else 1,
                -((бал.get(k[2]) or {}).get("ui") or 0))

    for q, ч in список:
        сорт = sorted(кандидаты.get(q, {}), key=ключ_выбора)
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
    глубокий_итог: dict = {}
    for q in главные:
        сорт = sorted([k for k in кандидаты.get(q, {}) if k[0] in РЕЗЕРВНЫЕ],
                      key=lambda k: -((бал.get(k[2]) or {}).get("ui") or 0))
        if not сорт and а.glubokiy_poisk > 0:
            # ГЛУБОКИЙ ПОИСК СТРАНИЦАМИ. Двенадцати свежих сделок минта хватает
            # редким котировкам, а у USDC они все через DLMM и агрегаторы:
            # пул CPMM встречается дальше по истории.
            найдено, прочитано_г = глубокий_поиск_пула(q, а.glubokiy_poisk, C, B,
                                                        программы)
            глубокий_итог[q] = {"транзакций": прочитано_г,
                                 "кандидатов": len(найдено)}
            if not any(k[0] in РЕЗЕРВНЫЕ for k in найдено):
                # УКАЗАТЕЛЬ ОТ JUPITER: адрес пула, дальше только цепь.
                чз = пул_через_jupiter(q)
                глубокий_итог.setdefault(q, {})["jupiter"] = {
                    "ok": чз.get("ok"), "why_not": чз.get("why_not"),
                    "pools": [п.get("pool") for п in чз.get("pools") or []],
                    "labels": [п.get("label") for п in чз.get("pools") or []]}
                for п_ in (чз.get("pools") or []):
                    for прог_, qv_, wv_, q2_ in образцы_пула(п_["pool"], 40, C, B,
                                                              программы):
                        if q2_ == q and (прог_, qv_, wv_) not in найдено:
                            найдено.append((прог_, qv_, wv_))
            if найдено:
                новые_хранилища = sorted({v for k in найдено for v in (k[1], k[2])}
                                          - set(бал))
                if новые_хранилища:
                    бал.update(остатки(новые_хранилища))
                кандидаты.setdefault(q, {}).update({k: 1 for k in найдено})
                сорт = sorted([k for k in найдено if k[0] in РЕЗЕРВНЫЕ],
                              key=lambda k: -((бал.get(k[2]) or {}).get("ui") or 0))
                if сорт and (q not in пулы or пулы[q].get("program") not in РЕЗЕРВНЫЕ):
                    прог_г, qv_г, wv_г = сорт[0]
                    пулы[q] = {"program": прог_г, "q_vault": qv_г, "w_vault": wv_г,
                                "sol_depth": (бал.get(wv_г) or {}).get("ui")}
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
             "главные": отчёт_главных,
             "глубокий_поиск": глубокий_итог}
    Path(а.out_otchet).write_text(json.dumps(итог, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    print(json.dumps({k: v for k, v in итог.items()
                      if k not in ("по_котировкам", "главные")}, ensure_ascii=False))
    for стр in отчёт_главных:
        print(json.dumps(стр, ensure_ascii=False)[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main())
