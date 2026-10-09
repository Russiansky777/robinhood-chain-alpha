#!/usr/bin/env python3
"""П.4 пакета «сборщики под новые инструкции pump.fun»: СИМУЛЯЦИЯ УЗЛОМ.

ЗАЧЕМ ИМЕННО СИМУЛЯЦИЯ, А НЕ МОИ 229 СВЕРОК. Сборщик
`analysis/c3_pump_sborka.py` доказал на живых транзакциях из репозитория,
что 229 выведенных счетов из 229 совпали адрес в адрес. Но ни одного живого
образца НОВЫХ разновидностей (buy_exact_quote_in_v3 у кривой, 17-счётный
buy_exact_quote_in_v2 у Pump AMM) в репозитории НЕТ -- ровно поэтому полоса
и встала 08.10. Сверять раскладку не с чем: судить о том, принимает ли
программа раскладку СЕЙЧАС, может только сама программа. Это и делает
`simulateTransaction`.

КАК СУДИМ -- БЛИЗНЕЦАМИ, А НЕ «ошибка выглядит безобидно».
Для каждой живой транзакции с цепи берутся ДВЕ инструкции:
  * ЧУЖОЙ ОРИГИНАЛ -- счета и байты ровно те, что стоят в транзакции;
  * НАША КОПИЯ -- собрана нашим сборщиком из минимальных входов.
Обе прогоняются через узел ОДИНАКОВО (один и тот же плательщик -- чужой,
sigVerify=false, replaceRecentBlockhash=true, флаги signer/writable из IDL
у обеих). Исход один и тот же -- наша сборка неотличима от настоящей, и это
«прошло симуляцию». Исход разный -- КРАСНЫЙ, и в отчёте стоит обе ошибки.
Так состояние цепи (резервы уехали, проскальзывание не то) не может выдать
нашу ошибку за чужую и наоборот.

ЧТО ПОДПИСЫВАЕТСЯ И ОТПРАВЛЯЕТСЯ: НИЧЕГО. `sigVerify=false` подписи не
требует, ключей здесь нет вовсе, ни одного `sendTransaction` в файле нет.
Узел приходит ТОЛЬКО переменной окружения BLOOM_TREKKER_RPC: ключ в
командной строке виден в списке процессов любому на машине. Всё, что
печатается про узел, проходит через `zateret` из трекера -- своей копии
затирания здесь нет, чтобы копии не разошлись.

МОЛЧАЛИВЫЙ ПРОПУСК = ПРОВАЛ. Вариант, набравший меньше ЦЕЛЬ_НА_ВАРИАНТ
образцов, попадает в отчёт ПРОВАЛОМ с настоящим числом. Пустой список
образцов -- отказ, а не «ноль из ноля прошло».

ЗАПУСК (одной строкой, на торговом хосте, где цепь открыта):
    BLOOM_TREKKER_RPC=... python3 deploy/checks/pump_sborka_simulyaciya.py --live
Сухой прогон (сети не касается вовсе):
    python3 deploy/checks/pump_sborka_simulyaciya.py --dry-run
Самопроверка оснастки:
    python3 deploy/checks/pump_sborka_simulyaciya.py --self-test
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent.parent

# МОДУЛИ БЕРУТСЯ ИЗ PYTHONPATH (так их кладёт шаблон разового прогона) и
# только если там их нет -- из каталога репозитория. Обратного порядка быть
# не должно: 05.10 sys.path перекрывал PYTHONPATH, и прогон шёл зелёным по
# СТАРОМУ коду службы.
try:
    import c3_pump_sborka as СБ
except ImportError:
    sys.path.append(str(КОРЕНЬ / "analysis"))
    import c3_pump_sborka as СБ

try:
    import c3_kapital_trekker as ТР
except ImportError:
    sys.path.append(str(КОРЕНЬ / "analysis"))
    import c3_kapital_trekker as ТР

zateret = ТР.zateret
uzel_iz_okruzheniya = ТР.uzel_iz_okruzheniya

# П.4: 20+ на каждый вариант.
ЦЕЛЬ_НА_ВАРИАНТ = 20

# Кошелёк полосы. Публичный адрес, ключа здесь нет и не нужно.
КОШЕЛЁК_ПОЛОСЫ = "4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x"

# ВАРИАНТЫ ПО ПУНКТАМ ЗАДАНИЯ.
ВАРИАНТЫ = (
    # п.1 -- кривая 6EF8…
    ("pump", "buy_v3"),
    ("pump", "buy_exact_quote_in_v3"),
    ("pump", "sell_v3"),
    ("pump", "buy_v2"),
    ("pump", "buy_exact_quote_in_v2"),
    ("pump", "sell_v2"),
    # п.2 -- Pump AMM pAMMBay…
    ("pump_amm", "buy_v2"),
    ("pump_amm", "buy_exact_quote_in_v2"),
    ("pump_amm", "sell_v2"),
)

ИСХОД_ПРОШЛО = "ПРОШЛО"
ИСХОД_ЭКОНОМИКА = "ЭКОНОМИКА/СОСТОЯНИЕ"
ИСХОД_РАСКЛАДКА = "РАСКЛАДКА ОТВЕРГНУТА"
ИСХОД_ДО_ПРОГРАММЫ = "ДО ПРОГРАММЫ"

# ИМЕНА ОШИБОК, ПРИ КОТОРЫХ РАСКЛАДКА УЖЕ ПРИНЯТА: программа РАЗОБРАЛА наши
# счета и аргументы и отказала на числах или на состоянии пула. Ни одного
# имени наугад -- самопроверка сверяет КАЖДОЕ с таблицей ошибок закреплённого
# IDL и падает, если такого имени там нет.
ИМЕНА_ЭКОНОМИКИ = {
    "pump": ("TooMuchSolRequired", "TooLittleSolReceived",
             "BondingCurveComplete", "BondingCurveNotComplete"),
    "pump_amm": ("ExceededSlippage", "ZeroBaseAmount", "ZeroQuoteAmount",
                 "TooLittlePoolTokenLiquidity"),
}

# Ошибки УРОВНЯ ТРАНЗАКЦИИ: программа не запускалась, и о раскладке они не
# говорят ничего. Считать их «прошло» нельзя.
ДО_ПРОГРАММЫ = ("AccountNotFound", "InsufficientFundsForRent",
                "BlockhashNotFound", "AlreadyProcessed", "SanitizeFailure",
                "AccountInUse", "AccountLoadedTwice", "TooManyAccountLocks",
                "AddressLookupTableNotFound", "InvalidAddressLookupTableData",
                "InvalidAddressLookupTableIndex", "ProgramAccountNotFound")

WHY_НЕТ_ОБРАЗЦОВ = "образцов не набрано -- судить не о чем"
WHY_НЕТ_BUYBACK = "получателя buyback нет ни в одном живом образце"
WHY_МАЛО = "образцов меньше цели"


# ------------------------------------------------- таблица ошибок IDL

_ОШИБКИ: dict = {}


def oshibki_idl(*, zanovo: bool = False) -> dict:
    """Таблица ошибок из ТОГО ЖЕ закреплённого IDL, что и раскладки."""
    if _ОШИБКИ and not zanovo:
        return _ОШИБКИ
    из_ = {}
    for имя, путь in СБ.ИДЛ_ФАЙЛЫ.items():
        п = СБ.КОРЕНЬ / путь
        if not п.exists():
            raise СБ.ОшибкаСборки(f"{СБ.WHY_НЕТ_IDL}: {путь}")
        д = json.loads(п.read_text(encoding="utf-8"))
        из_[имя] = {int(о["code"]): {"name": о.get("name"), "msg": о.get("msg")}
                    for о in (д.get("errors") or [])}
    _ОШИБКИ.clear()
    _ОШИБКИ.update(из_)
    return _ОШИБКИ


def klass_ishoda(err, *, programma: str) -> dict:
    """ИСХОД СИМУЛЯЦИИ ПО ИМЕНИ, А НЕ ПО ВИДУ. Код без имени -- так и сказан."""
    if err is None:
        return {"ishod": ИСХОД_ПРОШЛО, "kod": None, "imja": None,
                "pochemu": "узел принял инструкцию"}
    # Верхний уровень: строка или {"InstructionError": [i, …]}.
    if isinstance(err, str):
        if err in ДО_ПРОГРАММЫ:
            return {"ishod": ИСХОД_ДО_ПРОГРАММЫ, "kod": None, "imja": err,
                    "pochemu": "отказ уровня транзакции: программа не запускалась"}
        return {"ishod": ИСХОД_РАСКЛАДКА, "kod": None, "imja": err,
                "pochemu": "отказ уровня транзакции, не из списка известных"}
    if not isinstance(err, dict):
        return {"ishod": ИСХОД_РАСКЛАДКА, "kod": None, "imja": str(err)[:80],
                "pochemu": "ответ узла не разобран"}
    пара = err.get("InstructionError")
    if not (isinstance(пара, list) and len(пара) == 2):
        имя = next(iter(err), None)
        if имя in ДО_ПРОГРАММЫ:
            return {"ishod": ИСХОД_ДО_ПРОГРАММЫ, "kod": None, "imja": имя,
                    "pochemu": "отказ уровня транзакции: программа не запускалась"}
        return {"ishod": ИСХОД_РАСКЛАДКА, "kod": None, "imja": str(имя),
                "pochemu": "отказ уровня транзакции, не из списка известных"}
    внутри = пара[1]
    if isinstance(внутри, str):
        return {"ishod": ИСХОД_РАСКЛАДКА, "kod": None, "imja": внутри,
                "pochemu": "ошибка исполнения: раскладка не принята"}
    код = (внутри or {}).get("Custom") if isinstance(внутри, dict) else None
    if код is None:
        return {"ishod": ИСХОД_РАСКЛАДКА, "kod": None, "imja": str(внутри)[:80],
                "pochemu": "ошибка исполнения: раскладка не принята"}
    код = int(код)
    своя = oshibki_idl().get(programma) or {}
    имя = (своя.get(код) or {}).get("name")
    if имя is None:
        # РАМОЧНАЯ ОШИБКА ANCHOR (2000--5999) ИЛИ КОД, КОТОРОГО В IDL НЕТ.
        # Имени ему я НЕ ВЫДУМЫВАЮ: в отчёт уходит число.
        return {"ishod": ИСХОД_РАСКЛАДКА, "kod": код, "imja": None,
                "pochemu": (f"код {код} в таблице ошибок {programma} не описан "
                            "(рамочная Anchor или новее нашего IDL) -- "
                            "считаем раскладку отвергнутой")}
    if имя in (ИМЕНА_ЭКОНОМИКИ.get(programma) or ()):
        return {"ishod": ИСХОД_ЭКОНОМИКА, "kod": код, "imja": имя,
                "pochemu": "программа РАЗОБРАЛА счета и аргументы и отказала "
                           "на числах или состоянии"}
    return {"ishod": ИСХОД_РАСКЛАДКА, "kod": код, "imja": имя,
            "pochemu": "ошибка программы не про числа -- значит про раскладку"}


def bliznecy_soshlis(nash: dict, chuzhoj: dict) -> bool:
    """НАША КОПИЯ НЕОТЛИЧИМА ОТ ЧУЖОГО ОРИГИНАЛА: один и тот же исход."""
    if not (nash and chuzhoj):
        return False
    if nash.get("ishod") == ИСХОД_ДО_ПРОГРАММЫ:
        return False
    return (nash.get("ishod") == chuzhoj.get("ishod")
            and nash.get("kod") == chuzhoj.get("kod")
            and nash.get("imja") == chuzhoj.get("imja"))


# ------------------------------------------------- сборка сообщения v0

def _solders():
    from solders.hash import Hash  # noqa: PLC0415
    from solders.instruction import AccountMeta, Instruction  # noqa: PLC0415
    from solders.message import MessageV0  # noqa: PLC0415
    from solders.pubkey import Pubkey  # noqa: PLC0415
    from solders.signature import Signature  # noqa: PLC0415
    from solders.transaction import VersionedTransaction  # noqa: PLC0415
    return (Hash, AccountMeta, Instruction, MessageV0, Pubkey, Signature,
            VersionedTransaction)


def tx_base64(*, programma: str, scheta: list, dannye: bytes,
              platelshchik: str) -> dict:
    """Одна инструкция в неподписанном v0 -- то, что уходит в симуляцию.

    ФЛАГИ signer/writable БЕРУТСЯ ИЗ IDL У ОБЕИХ инструкций -- и у нашей, и у
    чужого оригинала. Тогда единственная разница между близнецами -- адреса и
    байты данных, то есть ровно то, что мы и проверяем.
    """
    (Hash, AccountMeta, Instruction, MessageV0, Pubkey, Signature,
     VersionedTransaction) = _solders()
    metas = [AccountMeta(Pubkey.from_string(а["pubkey"]),
                         is_signer=bool(а["isSigner"]),
                         is_writable=bool(а["isWritable"])) for а in scheta]
    их = Instruction(Pubkey.from_string(programma), bytes(dannye), metas)
    msg = MessageV0.try_compile(Pubkey.from_string(platelshchik), [их], [],
                                Hash.default())
    n = msg.header.num_required_signatures
    vtx = VersionedTransaction.populate(msg, [Signature.default()] * n)
    сырое = bytes(vtx)
    return {"tx_base64": base64.b64encode(сырое).decode(), "bajt": len(сырое),
            "podpisej": n}


# ------------------------------------------------- наша копия образца

def nasha_kopija(razbor: dict) -> dict:
    """НАША КОПИЯ ЧУЖОЙ ИНСТРУКЦИИ: собрана сборщиком, входы -- из образца.

    Берётся НЕ весь список счетов образца, а только те значения, которые
    сборщик не умеет вывести сам: минты, кошелёк, программы токенов и
    получатели комиссий. Остальные 10--13 счетов сборщик обязан вывести
    PDA-арифметикой -- иначе это не проверка сборщика, а копирование.
    """
    из_ = {"ok": False, "why_not": None}
    имя_п, вариант = razbor["programma"], razbor["variant"]
    сч = razbor["scheta"]
    идл = СБ.zagruzit_idl()[имя_п]["ix"][вариант]
    выводимые = {а["name"] for а in идл["accounts"]
                 if а.get("address") or а.get("pda") or а["name"] in СБ.КАК_ATA}
    кон = {имя: знач for имя, знач in сч.items()
           if знач and имя not in выводимые}
    # Создатель кривой лежит в СЧЁТЕ кривой, а не в транзакции: для sell_v2 и
    # buy*_v2 он нужен семенем. Берём его из события сделки той же транзакции.
    нужен_создатель = any(
        (с.get("path") or "") == "bonding_curve.creator"
        for а in идл["accounts"] for с in ((а.get("pda") or {}).get("seeds") or []))
    if нужен_создатель:
        созд = razbor.get("creator")
        if not созд:
            из_["why_not"] = ("создателя кривой нет: он в СЧЁТЕ кривой, "
                              "а в транзакции его нет")
            return из_
        кон["bonding_curve.creator"] = созд
    арг = {к: в for к, в in (razbor.get("argumenty") or {}).items()
           if not к.startswith("_")}
    try:
        собрано = СБ.sobrat(имя_п, вариант, кон, арг)
    except СБ.ОшибкаСборки as сбой:
        из_["why_not"] = str(сбой)
        return из_
    наши = [а["pubkey"] for а in собрано["accounts"]]
    чужие = [сч.get(а["name"]) for а in идл["accounts"]]
    разошлись = [{"imja": а["name"], "nash": н, "chuzhoj": ч}
                 for а, н, ч in zip(идл["accounts"], наши, чужие, strict=False)
                 if ч and н != ч]
    из_.update(ok=True, sobrano=svodka_sborki(собрано),
               vyvedeno_samostojatelno=len([а for а in собрано["accounts"]
                                            if а["otkuda"] != СБ.ПРОВ_ПЕРЕДАН]),
               peredano=len(кон), raskladka_soshlas=not разошлись,
               razoshlis=разошлись[:4], _sobrano=собрано)
    return из_


def svodka_sborki(собрано: dict) -> dict:
    return {"schetov": собрано["schetov"], "bajt_dannyh": собрано["bajt_dannyh"],
            "data_hex": собрано["data_hex"][:16] + "…"}


# ------------------------------------------------- сбор образцов с цепи

def sobrat_obrazcy(zov, *, stranic: int, na_stranicu: int, predel_tx: int,
                   pauza: float, cel: int, pechat=None) -> dict:
    """ЖИВЫЕ ТРАНЗАКЦИИ С ЦЕПИ, РАЗЛОЖЕННЫЕ ПО ВАРИАНТАМ.

    Подписи берутся у ОБЕИХ программ, транзакции читаются по одной, пока
    каждый вариант не наберёт `cel` -- или пока не упёрлись в `predel_tx`.
    Недобор НЕ скрывается: он уходит в отчёт числом.
    """
    нужно = {f"{п}.{в}": [] for п, в in ВАРИАНТЫ}
    пропущено: dict = {}
    пропущено_по_variantu: dict = {}
    видели = set()
    прочитано = 0
    подписей = 0
    for имя_п in ("pump", "pump_amm"):
        адрес = СБ.programma_po_imeni(имя_п)
        до = None
        for _ in range(stranic):
            парам = {"limit": int(na_stranicu)}
            if до:
                парам["before"] = до
            отв = zov("getSignaturesForAddress", [адрес, парам])
            строки = (отв or {}).get("result") or []
            if not строки:
                break
            до = строки[-1].get("signature")
            подписей += len(строки)
            for с in строки:
                if с.get("err") is not None:
                    continue
                подпись = с.get("signature")
                if not подпись or подпись in видели:
                    continue
                if прочитано >= predel_tx:
                    break
                if all(len(в) >= cel for в in нужно.values()):
                    break
                видели.add(подпись)
                tx = _prochitat_tx(zov, подпись, pauza)
                прочитано += 1
                if not tx:
                    continue
                разбор = СБ.razbor_istochnika(tx)
                if not разбор.get("ok"):
                    continue
                ключ = f"{разбор['programma']}.{разбор['variant']}"
                if ключ not in нужно or len(нужно[ключ]) >= cel:
                    continue
                # ОБРАЗЕЦ СТАРШЕ НЫНЕШНЕГО IDL -- НЕ КРАСНЫЙ И НЕ ПРОПУСК.
                # На цепи есть сделки, собранные ПРЕЖНЕЙ раскладкой: у
                # pump.buy_exact_quote_in_v2 в репозитории два образца с
                # данными на 24 байта, тогда как нынешний IDL требует 25
                # (байта partial_fill в них нет вовсе). Близнеца на таком
                # построить нельзя: наша кодировка по нынешнему IDL
                # ОБЯЗАНА отличаться. Считать это красным значило бы
                # объявить ошибкой чужую старую сделку; молча выбросить --
                # соврать о числе образцов. Поэтому: не берём и СЧИТАЕМ.
                причина = _ne_sravnim(разбор)
                if причина:
                    пропущено[причина] = пропущено.get(причина, 0) + 1
                    пропущено_по_variantu.setdefault(ключ, {})
                    пропущено_по_variantu[ключ][причина] = (
                        пропущено_по_variantu[ключ].get(причина, 0) + 1)
                    continue
                разбор["creator"] = СБ.creator_iz_sobytija(tx)
                разбор["podpis"] = подпись
                разбор["platelshchik"] = _platelshchik(tx)
                нужно[ключ].append(разбор)
                if pechat:
                    pechat(f"  образец {ключ}: {len(нужно[ключ])} из {cel}")
            if прочитано >= predel_tx or all(len(в) >= cel
                                             for в in нужно.values()):
                break
    return {"obrazcy": нужно, "prochitano_tx": прочитано,
            "podpisej_prosmotreno": подписей, "predel_tx": predel_tx,
            "cel": cel, "propushcheno": пропущено,
            "propushcheno_po_variantu": пропущено_по_variantu}


WHY_СТАРАЯ_РАСКЛАДКА = "раскладка образца старше нынешнего IDL -- не сравним"
WHY_ДРУГАЯ_ДЛИНА = "длина данных образца не та, что у нынешнего IDL -- не сравним"


def _ne_sravnim(razbor: dict) -> str | None:
    """Почему этот образец НЕ СРАВНИМ с нынешним IDL. Пусто -- сравним."""
    if not razbor.get("schetov_sovpalo"):
        return WHY_СТАРАЯ_РАСКЛАДКА
    if int((razbor.get("argumenty") or {}).get("_hvost") or 0):
        return WHY_ДРУГАЯ_ДЛИНА
    return None


def _prochitat_tx(zov, podpis: str, pauza: float):
    if pauza:
        time.sleep(pauza)
    отв = zov("getTransaction",
              [podpis, {"encoding": "jsonParsed",
                        "maxSupportedTransactionVersion": 1,
                        "commitment": "finalized"}])
    return (отв or {}).get("result")


def _platelshchik(tx: dict) -> str | None:
    ключи = (((tx or {}).get("transaction") or {}).get("message")
             or {}).get("accountKeys") or []
    for к in ключи:
        if isinstance(к, dict):
            if к.get("signer"):
                return к.get("pubkey")
        elif к:
            return str(к)
    return None


# ------------------------------------------------- прогон симуляции

def simulirovat(zov, tx64: str) -> dict:
    отв = zov("simulateTransaction",
              [tx64, {"sigVerify": False, "replaceRecentBlockhash": True,
                      "encoding": "base64", "commitment": "processed"}])
    if (отв or {}).get("error"):
        return {"ok": False, "why_not": str((отв or {}).get("error"))[:200]}
    зн = ((отв or {}).get("result") or {}).get("value") or {}
    return {"ok": True, "err": зн.get("err"), "units": зн.get("unitsConsumed"),
            "logi": (зн.get("logs") or [])[-6:]}


def proverit_variant(zov, *, imja_programmy: str, variant: str,
                     obrazcy: list, cel: int, pauza: float) -> dict:
    """ОДИН ВАРИАНТ: близнецы на каждом образце, итог числами."""
    из_ = {"variant": f"{imja_programmy}.{variant}", "obrazcov": len(obrazcy),
           "cel": cel, "proverok": 0, "proshlo": 0, "krasnyh": 0,
           "raskladka_soshlas": 0, "bajt_v_bajt": 0, "stroki": [],
           "nedobor": len(obrazcy) < cel, "why_not": None}
    if not obrazcy:
        из_["why_not"] = WHY_НЕТ_ОБРАЗЦОВ
        return из_
    программа = СБ.programma_po_imeni(imja_programmy)
    for разбор in obrazcy:
        строка = {"podpis": разбор.get("podpis"), "ok": False, "why_not": None}
        копия = nasha_kopija(разбор)
        if not копия.get("ok"):
            строка["why_not"] = копия.get("why_not")
            из_["stroki"].append(строка)
            из_["proverok"] += 1
            из_["krasnyh"] += 1
            continue
        собрано = копия["_sobrano"]
        строка["raskladka_soshlas"] = копия["raskladka_soshlas"]
        строка["razoshlis"] = копия["razoshlis"]
        из_["raskladka_soshlas"] += 1 if копия["raskladka_soshlas"] else 0
        # БАЙТЫ ДАННЫХ: наша кодировка против чужой, байт в байт.
        чужие_байты = _dannye_obrazca(разбор, программа)
        строка["bajt_v_bajt"] = (чужие_байты is not None
                                 and bytes.fromhex(собрано["data_hex"]) == чужие_байты)
        из_["bajt_v_bajt"] += 1 if строка["bajt_v_bajt"] else 0
        плательщик = разбор.get("platelshchik")
        if not плательщик:
            строка["why_not"] = "плательщика в транзакции не нашли"
            из_["stroki"].append(строка)
            из_["proverok"] += 1
            из_["krasnyh"] += 1
            continue
        # БЛИЗНЕЦЫ. Чужой оригинал -- адреса и байты ИЗ ТРАНЗАКЦИИ, флаги из IDL.
        идл = СБ.zagruzit_idl()[imja_programmy]["ix"][variant]
        чужие_счета = [{"pubkey": разбор["scheta"].get(а["name"]),
                        "isSigner": bool(а.get("signer")),
                        "isWritable": bool(а.get("writable"))}
                       for а in идл["accounts"]]
        if any(not а["pubkey"] for а in чужие_счета) or чужие_байты is None:
            строка["why_not"] = ("живая раскладка короче нынешнего IDL -- "
                                 "близнеца не построить")
            из_["stroki"].append(строка)
            из_["proverok"] += 1
            из_["krasnyh"] += 1
            continue
        наш_tx = tx_base64(programma=программа, scheta=собрано["accounts"],
                           dannye=bytes.fromhex(собрано["data_hex"]),
                           platelshchik=плательщик)
        чужой_tx = tx_base64(programma=программа, scheta=чужие_счета,
                             dannye=чужие_байты, platelshchik=плательщик)
        if pauza:
            time.sleep(pauza)
        наш = simulirovat(zov, наш_tx["tx_base64"])
        if pauza:
            time.sleep(pauza)
        чужой = simulirovat(zov, чужой_tx["tx_base64"])
        if not (наш.get("ok") and чужой.get("ok")):
            строка["why_not"] = ("узел не ответил на симуляцию: "
                                 f"{zateret(наш.get('why_not') or чужой.get('why_not'))}")
            из_["stroki"].append(строка)
            из_["proverok"] += 1
            из_["krasnyh"] += 1
            continue
        и_наш = klass_ishoda(наш.get("err"), programma=imja_programmy)
        и_чужой = klass_ishoda(чужой.get("err"), programma=imja_programmy)
        сошлись = bliznecy_soshlis(и_наш, и_чужой)
        строка.update(ok=bool(сошлись), nash=и_наш, chuzhoj=и_чужой,
                      units=наш.get("units"),
                      logi=(None if сошлись else наш.get("logi")),
                      why_not=(None if сошлись
                               else "исходы близнецов разошлись"))
        из_["proverok"] += 1
        из_["proshlo"] += 1 if сошлись else 0
        из_["krasnyh"] += 0 if сошлись else 1
        из_["stroki"].append(строка)
    if из_["nedobor"]:
        из_["why_not"] = f"{WHY_МАЛО}: {len(obrazcy)} из {cel}"
    return из_


def _dannye_obrazca(razbor: dict, programma: str) -> bytes | None:
    """Байты чужой инструкции -- из самой транзакции, а не из нашей кодировки."""
    арг = {к: в for к, в in (razbor.get("argumenty") or {}).items()
           if not к.startswith("_")}
    try:
        наши = СБ.zakodirovat_argumenty(razbor["programma"], razbor["variant"], арг)
    except СБ.ОшибкаСборки:
        return None
    хвост = int((razbor.get("argumenty") or {}).get("_hvost") or 0)
    if хвост:
        # В чужих байтах есть хвост, которого наша раскладка не знает: тогда
        # байт в байт НЕ сойдётся, и это надо видеть, а не подгонять.
        return None
    return наши


# ------------------------------------------------- п.4: отказанные минты

def svoja_pokupka_dlja_otkazannyh(zov, *, obrazcy: dict,
                                  koshelek: str, pauza: float) -> dict:
    """ОТКАЗАННЫЕ СИГНАЛЫ vol_4vw 08.10: наша покупка тем вариантом, который
    программа принимает СЕЙЧАС -- на нашем кошельке, симуляцией.

    Получатель buyback не выводится и константой в IDL не стоит: он берётся
    ИЗ ЖИВОГО ОБРАЗЦА той же разновидности. Нет образца -- отказ по имени, а
    не адрес наугад.
    """
    вариант = СБ.ОТКАЗ_08_10["variant"]
    из_ = {"variant": f"pump.{вариант}", "koshelek": koshelek,
           "minty": [], "proverok": 0, "proshlo": 0, "krasnyh": 0,
           "why_not": None}
    живые = obrazcy.get(f"pump.{вариант}") or []
    получатель = next((р["scheta"].get("buyback_fee_recipient") for р in живые
                       if (р.get("scheta") or {}).get("buyback_fee_recipient")),
                      None)
    if not получатель:
        из_["why_not"] = WHY_НЕТ_BUYBACK
        return из_
    из_["buyback_iz_obrazca"] = получатель
    for минт in СБ.ОТКАЗ_08_10["minty"]:
        строка = {"mint": минт, "ok": False, "why_not": None}
        try:
            собрано = СБ.pokupka_krivoj_v3(
                base_mint=минт, user=koshelek,
                buyback_fee_recipient=получатель,
                spendable_quote_in=10_000_000, min_tokens_out=1)
        except СБ.ОшибкаСборки as сбой:
            строка["why_not"] = str(сбой)
            из_["minty"].append(строка)
            из_["proverok"] += 1
            из_["krasnyh"] += 1
            continue
        tx = tx_base64(programma=СБ.ПРОГ_КРИВОЙ, scheta=собрано["accounts"],
                       dannye=bytes.fromhex(собрано["data_hex"]),
                       platelshchik=koshelek)
        if pauza:
            time.sleep(pauza)
        отв = simulirovat(zov, tx["tx_base64"])
        if not отв.get("ok"):
            строка["why_not"] = f"узел не ответил: {zateret(отв.get('why_not'))}"
            из_["minty"].append(строка)
            из_["proverok"] += 1
            из_["krasnyh"] += 1
            continue
        исход = klass_ishoda(отв.get("err"), programma="pump")
        принято = исход["ishod"] in (ИСХОД_ПРОШЛО, ИСХОД_ЭКОНОМИКА)
        строка.update(ok=принято, ishod=исход, schetov=собрано["schetov"],
                      bajt_dannyh=собрано["bajt_dannyh"], units=отв.get("units"),
                      logi=(None if принято else отв.get("logi")),
                      why_not=(None if принято else исход["pochemu"]))
        из_["minty"].append(строка)
        из_["proverok"] += 1
        из_["proshlo"] += 1 if принято else 0
        из_["krasnyh"] += 0 if принято else 1
    return из_


# ------------------------------------------------- прогоны

def suhoj_progon() -> dict:
    """СУХОЙ: сети НЕ КАСАЕТСЯ ВОВСЕ. План числами и чужая самопроверка."""
    зелёных = СБ.self_test()
    таблица = oshibki_idl()
    из_ = {"rezhim": "suhoj", "setevyh_vyzovov": 0,
           "sborshchik_iz": str(Path(СБ.__file__).resolve()),
           "sborshchik_samoproverka_ne_proshlo": зелёных,
           "sborshchik_zhdal_proverok": СБ.ZHDEM_PROVEROK,
           "oshibok_v_idl": {к: len(в) for к, в in таблица.items()},
           "variantov_k_proverke": len(ВАРИАНТЫ),
           "cel_na_variant": ЦЕЛЬ_НА_ВАРИАНТ,
           "simulyacij_zhdjom": len(ВАРИАНТЫ) * ЦЕЛЬ_НА_ВАРИАНТ * 2
                                + len(СБ.ОТКАЗ_08_10["minty"]),
           "minty_otkaza": list(СБ.ОТКАЗ_08_10["minty"]),
           "uzel": zateret(uzel_iz_okruzheniya()),
           "why_not": None}
    if зелёных:
        из_["why_not"] = (f"самопроверка сборщика не зелёная: {зелёных} "
                          "не прошло -- живой прогон бессмысленен")
    return из_


def zhivoj_progon(*, stranic: int, na_stranicu: int, predel_tx: int,
                  pauza: float, cel: int, koshelek: str,
                  zov=None, pechat=print) -> dict:
    """ЖИВОЙ: подписи, транзакции, близнецы, отказанные минты."""
    url = uzel_iz_okruzheniya()
    зов = zov or ТР.rpc_iz_url(url, tajmaut=20.0)
    вызовов = {"n": 0}

    def _зов(метод, параметры):
        вызовов["n"] += 1
        return зов(метод, параметры)

    pechat(f"узел: {zateret(url)}")
    pechat(f"собираю образцы: цель {cel} на каждый из {len(ВАРИАНТЫ)} вариантов")
    сбор = sobrat_obrazcy(_зов, stranic=stranic, na_stranicu=na_stranicu,
                          predel_tx=predel_tx, pauza=pauza, cel=cel,
                          pechat=None)
    pechat(f"прочитано транзакций: {сбор['prochitano_tx']} "
           f"(предел {predel_tx}), подписей просмотрено "
           f"{сбор['podpisej_prosmotreno']}")
    итоги = []
    for имя_п, вариант in ВАРИАНТЫ:
        ключ = f"{имя_п}.{вариант}"
        pechat(f"  {ключ}: образцов {len(сбор['obrazcy'][ключ])}")
        итоги.append(proverit_variant(_зов, imja_programmy=имя_п,
                                      variant=вариант,
                                      obrazcy=сбор["obrazcy"][ключ],
                                      cel=cel, pauza=pauza))
    отказ = svoja_pokupka_dlja_otkazannyh(_зов, obrazcy=сбор["obrazcy"],
                                          koshelek=koshelek, pauza=pauza)
    проверок = sum(и["proverok"] for и in итоги) + отказ["proverok"]
    прошло = sum(и["proshlo"] for и in итоги) + отказ["proshlo"]
    недобор = [и["variant"] for и in итоги if и["nedobor"]]
    из_ = {"rezhim": "zhivoj", "uzel": zateret(url),
           "setevyh_vyzovov": вызовов["n"],
           "prochitano_tx": сбор["prochitano_tx"],
           "propushcheno": сбор["propushcheno"],
           "propushcheno_po_variantu": сбор["propushcheno_po_variantu"],
           "cel_na_variant": cel, "varianty": итоги, "otkazannye": отказ,
           "proverok": проверок, "proshlo": прошло,
           "krasnyh": проверок - прошло, "nedobor": недобор,
           "why_not": None}
    if недобор:
        из_["why_not"] = (f"{WHY_МАЛО}: {len(недобор)} вариантов не набрали "
                          f"{cel} -- {', '.join(недобор)}")
    elif проверок != прошло:
        из_["why_not"] = f"{проверок - прошло} проверок из {проверок} красные"
    return из_


def tablica(итог: dict) -> str:
    """Таблица «вариант -> проверок / прошло симуляцию»."""
    строки = [f"{'вариант':<40} {'образцов':>8} {'проверок':>8} {'прошло':>7} "
              f"{'раскладка':>9} {'байты':>6}"]
    for и in итог.get("varianty") or []:
        строки.append(f"{и['variant']:<40} {и['obrazcov']:>8} {и['proverok']:>8} "
                      f"{и['proshlo']:>7} {и['raskladka_soshlas']:>9} "
                      f"{и['bajt_v_bajt']:>6}")
    о = итог.get("otkazannye") or {}
    if о:
        строки.append(f"{о.get('variant', '?') + ' (минты отказа 08.10)':<40} "
                      f"{len(о.get('minty') or []):>8} {о.get('proverok', 0):>8} "
                      f"{о.get('proshlo', 0):>7} {'-':>9} {'-':>6}")
    строки.append(f"{'ИТОГО':<40} {'':>8} {итог.get('proverok', 0):>8} "
                  f"{итог.get('proshlo', 0):>7}")
    return "\n".join(строки)


# ------------------------------------------------- самопроверка

# ЧИСЛО ОБЪЯВЛЕНО. Проверок стало меньше -- значит проверку убрали и этого
# никто не заметил; больше -- значит добавили и не сказали. И то и другое
# здесь ПРОВАЛ, а не «всё зелено».
ZHDEM_PROVEROK = 25

# ПОДДЕЛЬНЫЕ ОТВЕТЫ УЗЛА: формы ровно те, что отдаёт Solana RPC. Проверяются
# ими и разбор, и счёт вызовов -- без сети.
ОТВЕТ_УСПЕХ = {"result": {"value": {"err": None, "unitsConsumed": 40_000,
                                    "logs": ["Program log: ok"]}}}


def _poddelnyj_uzel(исходы: list):
    """Узел, который отдаёт заготовленные ответы по порядку. Считает вызовы."""
    состояние = {"n": 0, "metody": []}

    def _зов(метод, параметры):  # noqa: ARG001
        состояние["metody"].append(метод)
        и = состояние["n"]
        состояние["n"] += 1
        return исходы[и] if и < len(исходы) else {"result": {"value": {"err": None}}}

    _зов.состояние = состояние
    return _зов


def self_test() -> int:  # noqa: C901, PLR0915
    было, плохо = 0, 0
    упавшие: list = []

    def chk(имя, условие, подробность=None):
        nonlocal было, плохо
        было += 1
        if условие:
            print(f"  ok   {имя}")
        else:
            плохо += 1
            упавшие.append(имя)
            print(f" ПЛОХО {имя} -- {подробность}")

    путь_сб = str(Path(СБ.__file__).resolve())
    chk(f"сборщик взят из НАШЕГО каталога и путь напечатан: {путь_сб}",
        путь_сб.endswith("c3_pump_sborka.py"), путь_сб)

    не_прошло = СБ.self_test()
    chk("самопроверка САМОГО сборщика зелёная -- иначе симулировать нечего: "
        f"{СБ.ZHDEM_PROVEROK} из {СБ.ZHDEM_PROVEROK}",
        не_прошло == 0, не_прошло)

    таблица = oshibki_idl()
    chk("таблица ошибок прочитана из ТОГО ЖЕ закреплённого IDL: "
        f"{len(таблица['pump'])} у кривой и {len(таблица['pump_amm'])} у AMM",
        len(таблица["pump"]) > 50 and len(таблица["pump_amm"]) > 50,
        {к: len(в) for к, в in таблица.items()})

    # НИ ОДНОГО ВЫДУМАННОГО ИМЕНИ. Каждое имя «экономики» обязано стоять в IDL.
    нет_в_idl = []
    for прог, имена in ИМЕНА_ЭКОНОМИКИ.items():
        есть = {о["name"] for о in (таблица.get(прог) or {}).values()}
        нет_в_idl += [f"{прог}.{и}" for и in имена if и not in есть]
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: КАЖДОЕ имя «экономики» сверено с таблицей ошибок "
        f"IDL -- выдуманных ноль ({sum(len(в) for в in ИМЕНА_ЭКОНОМИКИ.values())} имён)",
        not нет_в_idl, нет_в_idl)

    chk("исход без ошибки -- ПРОШЛО",
        klass_ishoda(None, programma="pump")["ishod"] == ИСХОД_ПРОШЛО)

    и6002 = klass_ishoda({"InstructionError": [2, {"Custom": 6002}]},
                         programma="pump")
    chk("Custom 6002 кривой -- это TooMuchSolRequired, и это ЭКОНОМИКА: "
        "программа разобрала счета и отказала на числах",
        и6002["ishod"] == ИСХОД_ЭКОНОМИКА
        and и6002["imja"] == "TooMuchSolRequired", и6002)

    и6004 = klass_ishoda({"InstructionError": [2, {"Custom": 6004}]},
                         programma="pump")
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: Custom 6004 -- MintDoesNotMatchBondingCurve, и это "
        "РАСКЛАДКА, а не «тоже ошибка программы»",
        и6004["ishod"] == ИСХОД_РАСКЛАДКА
        and и6004["imja"] == "MintDoesNotMatchBondingCurve", и6004)

    и6004amm = klass_ishoda({"InstructionError": [2, {"Custom": 6004}]},
                            programma="pump_amm")
    chk("ОДИН КОД -- РАЗНЫЕ ПРОГРАММЫ, РАЗНЫЙ СМЫСЛ: 6004 у AMM это "
        "ExceededSlippage (экономика), у кривой -- раскладка. Ключ всегда пара",
        и6004amm["ishod"] == ИСХОД_ЭКОНОМИКА
        and и6004amm["imja"] == "ExceededSlippage", и6004amm)

    и2006 = klass_ishoda({"InstructionError": [2, {"Custom": 2006}]},
                         programma="pump")
    chk("рамочный код Anchor 2006 в таблице IDL НЕ описан -- имени я ему НЕ "
        "выдумываю, в отчёт уходит число, и исход -- раскладка отвергнута",
        и2006["ishod"] == ИСХОД_РАСКЛАДКА and и2006["imja"] is None
        and и2006["kod"] == 2006, и2006)

    иmal = klass_ishoda({"InstructionError": [2, "NotEnoughAccountKeys"]},
                        programma="pump")
    chk("NotEnoughAccountKeys -- раскладка отвергнута (это и есть отказ 08.10)",
        иmal["ishod"] == ИСХОД_РАСКЛАДКА, иmal)

    иdo = klass_ishoda("AccountNotFound", programma="pump")
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: AccountNotFound -- отказ ДО ПРОГРАММЫ, и он НЕ "
        "считается прошедшим: о раскладке он не говорит ничего",
        иdo["ishod"] == ИСХОД_ДО_ПРОГРАММЫ
        and not bliznecy_soshlis(иdo, иdo), иdo)

    chk("близнецы с одинаковым исходом сходятся, с разным -- нет",
        bliznecy_soshlis(и6002, и6002) and not bliznecy_soshlis(и6002, и6004))

    chk("ДОКАЗАННЫЙ КРАСНЫЙ: пустой список образцов -- отказ по имени, а не "
        "«ноль из ноля прошло»",
        (proverit_variant(None, imja_programmy="pump",
                          variant="buy_exact_quote_in_v3", obrazcy=[],
                          cel=ЦЕЛЬ_НА_ВАРИАНТ, pauza=0)["why_not"]
         == WHY_НЕТ_ОБРАЗЦОВ))

    недобор = proverit_variant(None, imja_programmy="pump",
                               variant="buy_exact_quote_in_v3", obrazcy=[],
                               cel=ЦЕЛЬ_НА_ВАРИАНТ, pauza=0)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: недобор образцов помечен ПРИЗНАКОМ, а не "
        "проглочен -- молчаливый пропуск был бы провалом",
        недобор["nedobor"] is True, недобор)

    пусто = svoja_pokupka_dlja_otkazannyh(None, obrazcy={}, koshelek=КОШЕЛЁК_ПОЛОСЫ,
                                          pauza=0)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: получателя buyback нет ни в одном живом образце "
        "-- своя покупка для минтов отказа НЕ собирается (адрес наугад послал "
        "бы комиссию чужому)",
        пусто["why_not"] == WHY_НЕТ_BUYBACK and пусто["proverok"] == 0, пусто)

    chk(f"минтов отказа 08.10 ровно три, и они названы: "
        f"{', '.join(м[:8] + '…' for м in СБ.ОТКАЗ_08_10['minty'])}",
        len(СБ.ОТКАЗ_08_10["minty"]) == 3)

    # СБОРКА СООБЩЕНИЯ v0 -- НА НАСТОЯЩИХ АДРЕСАХ, ЧЕРЕЗ solders.
    собрано = СБ.pokupka_krivoj_v3(
        base_mint=СБ.ОТКАЗ_08_10["minty"][0], user=КОШЕЛЁК_ПОЛОСЫ,
        buyback_fee_recipient=КОШЕЛЁК_ПОЛОСЫ, spendable_quote_in=10_000_000,
        min_tokens_out=1)
    постр = tx_base64(programma=СБ.ПРОГ_КРИВОЙ, scheta=собрано["accounts"],
                      dannye=bytes.fromhex(собрано["data_hex"]),
                      platelshchik=КОШЕЛЁК_ПОЛОСЫ)
    chk(f"сообщение v0 собирается и влезает в пакет: {постр['bajt']} байт из "
        f"1232, подписей {постр['podpisej']}, счетов {собрано['schetov']}",
        постр["bajt"] < 1232 and постр["podpisej"] == 1
        and собрано["schetov"] == 17, постр)

    chk("база64 транзакции разбирается обратно и первый байт -- число подписей",
        base64.b64decode(постр["tx_base64"])[0] == постр["podpisej"])

    # СУХОЙ ПРОГОН НЕ КАСАЕТСЯ СЕТИ ВОВСЕ.
    сухой = suhoj_progon()
    chk("СУХОЙ прогон делает НОЛЬ сетевых вызовов и говорит это числом",
        сухой["setevyh_vyzovov"] == 0 and сухой["why_not"] is None, сухой)

    chk("узла в аргументах командной строки НЕТ ВОВСЕ -- только окружение "
        "BLOOM_TREKKER_RPC (ключ в argv виден всем на машине)",
        "--uzel" not in _razbor_dovodov_imena()
        and "--rpc" not in _razbor_dovodov_imena())

    секрет = "https://mainnet.helius-rpc.com/?api-key=ABCDEF0123456789"
    chk("всё, что печатается про узел, проходит через затирание трекера: "
        "ключа в выводе не остаётся",
        "ABCDEF" not in zateret(секрет) and zateret(секрет).endswith("/…"),
        zateret(секрет))

    # ПОДДЕЛЬНЫЙ УЗЕЛ: живой путь проверен без сети, включая счёт вызовов.
    образец = _obrazec_dlja_samoproverki()
    if образец:
        узел = _poddelnyj_uzel([ОТВЕТ_УСПЕХ, ОТВЕТ_УСПЕХ])
        итог = proverit_variant(узел, imja_programmy=образец["programma"],
                                variant=образец["variant"], obrazcy=[образец],
                                cel=1, pauza=0)
        chk("поддельный узел: на один образец уходит РОВНО ДВЕ симуляции "
            "(наша копия и чужой оригинал), и при одинаковом исходе проверка "
            "проходит",
            узел.состояние["n"] == 2 and итог["proverok"] == 1
            and итог["proshlo"] == 1
            and узел.состояние["metody"] == ["simulateTransaction"] * 2,
            (узел.состояние, итог["stroki"][:1]))
        узел2 = _poddelnyj_uzel([
            {"result": {"value": {"err": {"InstructionError": [0, {"Custom": 6004}]},
                                  "logs": []}}},
            ОТВЕТ_УСПЕХ])
        итог2 = proverit_variant(узел2, imja_programmy=образец["programma"],
                                 variant=образец["variant"], obrazcy=[образец],
                                 cel=1, pauza=0)
        chk("ДОКАЗАННЫЙ КРАСНЫЙ: наша копия отвергнута там, где чужой оригинал "
            "прошёл -- проверка КРАСНАЯ, и в строке стоят оба исхода",
            итог2["proshlo"] == 0 and итог2["krasnyh"] == 1
            and итог2["stroki"][0]["nash"]["ishod"] == ИСХОД_РАСКЛАДКА
            and итог2["stroki"][0]["chuzhoj"]["ishod"] == ИСХОД_ПРОШЛО,
            итог2["stroki"][:1])
    else:
        chk("образец для поддельного узла найден в репозитории", False,
            "ни одной живой транзакции с раскладкой нынешнего IDL")
        chk("ДОКАЗАННЫЙ КРАСНЫЙ на поддельном узле", False, "образца нет")

    стар = {"programma": "pump", "variant": "buy_exact_quote_in_v2",
            "schetov_sovpalo": False,
            "argumenty": {"_hvost": 0}}
    корот = {"programma": "pump", "variant": "buy_exact_quote_in_v2",
             "schetov_sovpalo": True,
             "argumenty": {"_hvost": -1}}
    годен = {"programma": "pump", "variant": "buy_exact_quote_in_v2",
             "schetov_sovpalo": True, "argumenty": {"_hvost": 0}}
    chk("образец СТАРШЕ нынешнего IDL назван своей причиной и в близнецы не "
        "берётся: ни красный (чужая старая сделка -- не наша ошибка), ни "
        "молчаливый пропуск",
        _ne_sravnim(стар) == WHY_СТАРАЯ_РАСКЛАДКА
        and _ne_sravnim(корот) == WHY_ДРУГАЯ_ДЛИНА
        and _ne_sravnim(годен) is None,
        (_ne_sravnim(стар), _ne_sravnim(корот), _ne_sravnim(годен)))

    # ЧИСЛО ПРОПУЩЕННЫХ ИЗМЕРЕНО НА НАСТОЯЩИХ ОБРАЗЦАХ РЕПОЗИТОРИЯ, а не
    # объявлено: у pump.buy_exact_quote_in_v2 данные на 24 байта вместо 25.
    корочеns = [р for р in (СБ.razbor_istochnika(t) for t in СБ.zhivyje_tranzakcii())
                if р.get("ok") and _ne_sravnim(р) == WHY_ДРУГАЯ_ДЛИНА]
    chk(f"НА ЖИВЫХ ОБРАЗЦАХ: с длиной данных не по нынешнему IDL их {len(корочеns)} "
        "-- и ровно они не попадают в близнецов",
        len(корочеns) == 2, [(р["programma"], р["variant"],
                              р["argumenty"]["_hvost"]) for р in корочеns])

    print(f"\nпроверок {было}, ждали {ZHDEM_PROVEROK}, не прошло {плохо}")
    if упавшие:
        for и in упавшие:
            print(f"УПАЛИ: {и}")
    if было != ZHDEM_PROVEROK:
        print(" ПЛОХО число проверок разошлось с объявленным")
        плохо += 1
    return плохо


def _razbor_dovodov_imena() -> list:
    return [д for д in _parser()._option_string_actions]  # noqa: SLF001


def _obrazec_dlja_samoproverki() -> dict | None:
    """Живая транзакция из репозитория, чья раскладка совпадает с IDL."""
    for tx in СБ.zhivyje_tranzakcii():
        разбор = СБ.razbor_istochnika(tx)
        if not разбор.get("ok") or not разбор.get("schetov_sovpalo"):
            continue
        if int((разбор.get("argumenty") or {}).get("_hvost") or 0):
            continue
        ключ = f"{разбор['programma']}.{разбор['variant']}"
        if ключ not in {f"{п}.{в}" for п, в in ВАРИАНТЫ}:
            continue
        разбор["creator"] = СБ.creator_iz_sobytija(tx)
        разбор["platelshchik"] = _platelshchik(tx)
        разбор["podpis"] = ((tx.get("transaction") or {}).get("signatures")
                            or [None])[0]
        копия = nasha_kopija(разбор)
        if копия.get("ok") and копия.get("raskladka_soshlas") and разбор["platelshchik"]:
            return разбор
    return None


# ------------------------------------------------- командная строка

def _parser() -> argparse.ArgumentParser:
    п = argparse.ArgumentParser(
        description="П.4: симуляция узлом на настоящих транзакциях цепи")
    п.add_argument("--self-test", action="store_true",
                   help="самопроверка оснастки, сети не касается")
    п.add_argument("--dry-run", action="store_true",
                   help="сухой прогон: план числами, НОЛЬ сетевых вызовов")
    п.add_argument("--live", action="store_true",
                   help="живой прогон: подписи, транзакции, симуляция")
    п.add_argument("--cel", type=int, default=ЦЕЛЬ_НА_ВАРИАНТ,
                   help=f"образцов на вариант (по умолчанию {ЦЕЛЬ_НА_ВАРИАНТ})")
    п.add_argument("--stranic", type=int, default=3,
                   help="страниц подписей на программу")
    п.add_argument("--na-stranicu", type=int, default=1000,
                   help="подписей на страницу (предел узла -- 1000)")
    п.add_argument("--predel-tx", type=int, default=900,
                   help="предел чтений getTransaction за прогон")
    п.add_argument("--pauza", type=float, default=0.05,
                   help="пауза между вызовами узла, секунды")
    п.add_argument("--koshelek", default=КОШЕЛЁК_ПОЛОСЫ,
                   help="кошелёк для своей покупки по минтам отказа")
    п.add_argument("--json", action="store_true", help="только JSON")
    return п


def main() -> int:
    дов = _parser().parse_args()
    if дов.self_test:
        return 1 if self_test() else 0
    if дов.live:
        итог = zhivoj_progon(stranic=дов.stranic, na_stranicu=дов.na_stranicu,
                             predel_tx=дов.predel_tx, pauza=дов.pauza,
                             cel=дов.cel, koshelek=дов.koshelek,
                             pechat=(lambda *_: None) if дов.json else print)
        if not дов.json:
            print()
            print(tablica(итог))
            print()
        print(json.dumps(итог, ensure_ascii=False, indent=1, default=str))
        return 0 if итог["why_not"] is None else 1
    итог = suhoj_progon()
    if not дов.json:
        print("СУХОЙ ПРОГОН: цепи не касаюсь. План проверок живого прогона:")
        print(f"  вариантов: {итог['variantov_k_proverke']}, "
              f"цель на вариант: {итог['cel_na_variant']}")
        print(f"  симуляций ждём: {итог['simulyacij_zhdjom']} "
              "(по две на образец -- наша копия и чужой оригинал -- плюс по "
              "одной на каждый минт отказа)")
        print(f"  сетевых вызовов сделано: {итог['setevyh_vyzovov']}")
        print(f"  узел: {итог['uzel']}")
        print("  ЖИВОЙ ПРОГОН: --live (цепь должна быть открыта с машины)")
    print(json.dumps(итог, ensure_ascii=False, indent=1, default=str))
    return 0 if итог["why_not"] is None else 1


if __name__ == "__main__":
    sys.exit(main())
