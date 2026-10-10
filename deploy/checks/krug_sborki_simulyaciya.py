#!/usr/bin/env python3
"""КРУГ НАШЕЙ СБОРКИ УЗЛОМ: [ata_idempotent, покупка, продажа] одной транзакцией.

ЗАЧЕМ (слово штаба 09.10, п.2). Прогон близнецов говорит «наша сборка
неотличима от чужой» -- и этого МАЛО. Code-3 прочёл логи и нашёл: часть
«прошедших» близнецов v3 -- это ОБА близнеца, упавшие Custom 1 у ПРОГРАММЫ
ТОКЕНА (не хватило средств у чужого плательщика). Исход одинаковый, а успеха
нет ни у кого. Про нашу раскладку это говорит хорошо (счета программа приняла),
но про то, КУПИТ ЛИ НАШ КОШЕЛЁК, не говорит ничего.

Здесь проверка прямая и без близнецов: от НАШЕГО адреса, нашими деньгами (в
симуляции), три инструкции в одной транзакции -- создать ATA базового минта,
купить, продать. Успех -- это err=null у узла И баланс токена ПОСЛЕ ПОКУПКИ
строго больше нуля. Второе обязательно: транзакция, которая «успешно» купила
ноль токенов, успехом не является, и без чтения баланса этого не видно.

ПОЧЕМУ ДВЕ СИМУЛЯЦИИ НА МИНТ. Баланс нужен ИМЕННО ПОСЛЕ ПОКУПКИ, а в круге
продажа его тут же обнуляет. Поэтому: (а) [ata, покупка] -- и у неё читается
счёт ATA через simulateTransaction(accounts), (б) [ata, покупка, продажа] -- и у
неё смотрится только исход. Одной транзакцией круг тоже проверяется -- это (б).

ПОДПИСЕЙ НЕТ, ОТПРАВКИ НЕТ. sigVerify=false подписи не требует,
sendTransaction в файле нет ни одного, ключей здесь нет. Узел -- ТОЛЬКО
переменной окружения BLOOM_TREKKER_RPC: ключ в argv виден в списке процессов.
"""
from __future__ import annotations

import argparse
import base64
import inspect
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "analysis"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import c3_pump_sborka as СБ  # noqa: E402

КОШЕЛЁК_ПОЛОСЫ = "4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x"
ПОКУПКА = "buy_exact_quote_in_v3"
# РАЗНОВИДНОСТИ ПОКУПКИ, КОТОРЫЕ ИЩЕМ В СДЕЛКЕ ИСТОЧНИКА. Собираем мы всегда
# ПОКУПКОЙ (buy_exact_quote_in_v3): полоса платит фиксированный билет, то есть
# «точный вход». А вот источник (у запусков -- создатель токена) мог купить
# любой из двух v3: оба 17-счётные и имена счетов у них совпадают до единого,
# поэтому минт, программа базы и buyback снимаются с любого. Прогон
# 38014321870 это показал числом: девять подписей из двенадцати ушли в отказ
# "buy_exact_quote_in_v3 в транзакции не найдена" только потому, что создатель
# взял buy_v3, -- проверять было нечего, хотя ворота сигнала такой запуск
# пропускают.
ПОКУПКИ_ИСТОЧНИКА = ("buy_exact_quote_in_v3", "buy_v3")
ПРОДАЖА = "sell_v3"
WHY_НЕТ_УЗЛА = "узел задаётся только окружением BLOOM_TREKKER_RPC"
# ПОТОЛОК ВЕРСИИ ТРАНЗАКЦИИ -- КАК У СЛУЖБЫ, А НЕ НОЛЬ. Здесь стоял 0, и
# прогон 37958277476 это показал числом: все 468 подписей источника
# 4vw54BmA узел отдал ошибкой -32015 "Transaction version (1) is not
# supported", отчёт сказал "покупок v3 у источника нет", и 468 вызовов ушли
# в пустоту. В службе BLOOM_MAX_TX_VERSION по умолчанию 1
# (bloom_detector:318), и читать надо тем же потолком.
ПОТОЛОК_ВЕРСИИ_TX = 1
WHY_НЕТ_BUYBACK = ("получателя buyback нет ни в одном живом образце -- "
                   "адрес наугад послал бы комиссию чужому")


def затереть(т) -> str:
    т = str(т or "")
    return т.split("api-key=")[0] + "api-key=…" if "api-key=" in т else т


def узел() -> str:
    у = os.environ.get("BLOOM_TREKKER_RPC") or ""
    if not у:
        raise SystemExit(f"СТОП: {WHY_НЕТ_УЗЛА}")
    return у


def зов(метод: str, параметры: list, *, таймаут: float = 30.0) -> dict:
    тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                        "params": параметры}).encode()
    зап = urllib.request.Request(узел(), data=тело,
                                  headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(зап, timeout=таймаут) as отв:
            д = json.loads(отв.read().decode())
    except (urllib.error.URLError, OSError, ValueError) as сбой:
        return {"ok": False, "why_not": f"{type(сбой).__name__}: {затереть(сбой)}"}
    if "error" in д:
        return {"ok": False, "why_not": json.dumps(д["error"], ensure_ascii=False)[:200]}
    return {"ok": True, "result": д.get("result")}


def ata_instrukciya(*, payer: str, owner: str, mint: str,
                    token_program: str) -> dict:
    """CreateIdempotent ATA -- байты и порядок счетов как в денежном пути."""
    return {"programma": СБ.ПРОГ_ATA, "dannye": bytes([1]), "scheta": [
        {"pubkey": payer, "isSigner": True, "isWritable": True},
        {"pubkey": СБ.ata(owner, token_program, mint),
         "isSigner": False, "isWritable": True},
        {"pubkey": owner, "isSigner": False, "isWritable": False},
        {"pubkey": mint, "isSigner": False, "isWritable": False},
        {"pubkey": СБ.СИСТЕМНАЯ, "isSigner": False, "isWritable": False},
        {"pubkey": token_program, "isSigner": False, "isWritable": False}]}


def predel_cu(units: int) -> dict:
    """ComputeBudget: предел CU. Три инструкции в 200 000 по умолчанию не влезают."""
    return {"programma": "ComputeBudget111111111111111111111111111111",
            "dannye": bytes([2]) + struct.pack("<I", int(units)), "scheta": []}


def tx_base64(*, instrukcii: list, platelshchik: str) -> str:
    from solders.hash import Hash  # noqa: PLC0415
    from solders.instruction import AccountMeta, Instruction  # noqa: PLC0415
    from solders.message import MessageV0  # noqa: PLC0415
    from solders.pubkey import Pubkey  # noqa: PLC0415
    from solders.signature import Signature  # noqa: PLC0415
    from solders.transaction import VersionedTransaction  # noqa: PLC0415

    спис = []
    for и in instrukcii:
        metas = [AccountMeta(Pubkey.from_string(а["pubkey"]),
                             is_signer=bool(а["isSigner"]),
                             is_writable=bool(а["isWritable"]))
                 for а in и["scheta"]]
        спис.append(Instruction(Pubkey.from_string(и["programma"]),
                                bytes(и["dannye"]), metas))
    msg = MessageV0.try_compile(Pubkey.from_string(platelshchik), спис, [],
                                Hash.default())
    n = msg.header.num_required_signatures
    vtx = VersionedTransaction.populate(msg, [Signature.default()] * n)
    return base64.b64encode(bytes(vtx)).decode()


def simulirovat(tx64: str, *, scheta: list | None = None) -> dict:
    пар = {"sigVerify": False, "replaceRecentBlockhash": True,
           "commitment": "confirmed", "encoding": "base64"}
    if scheta:
        пар["accounts"] = {"addresses": list(scheta), "encoding": "base64"}
    о = зов("simulateTransaction", [tx64, пар])
    if not о["ok"]:
        return {"ok": False, "why_not": о["why_not"]}
    зн = (о.get("result") or {}).get("value") or {}
    return {"ok": True, "err": зн.get("err"), "logi": зн.get("logs") or [],
            "units": зн.get("unitsConsumed"), "accounts": зн.get("accounts") or []}


def ostatok_tokena(scheta_otveta: list) -> int | None:
    """Количество токена в счёте ATA из simulateTransaction(accounts).

    Разбор СЧЁТА SPL-токена: amount -- u64 начиная с 64-го байта (mint 32,
    owner 32, затем amount). Это раскладка и у Token, и у Token-2022: у
    Token-2022 расширения идут ПОСЛЕ базовых 165 байт, поэтому смещение то же.
    Нет данных -- None, а не ноль: «не прочитали» и «купили ноль» -- разное.
    """
    if not scheta_otveta:
        return None
    зн = scheta_otveta[0]
    if not зн:
        return None
    данные = (зн.get("data") or [None])[0]
    if not данные:
        return None
    сырые = base64.b64decode(данные)
    if len(сырые) < 72:
        return None
    return int.from_bytes(сырые[64:72], "little")


def minty_token2022_s_cepi(*, skolko: int, pauza: float) -> dict:
    """Живые минты кривой на Token-2022 -- из свежих покупок самой программы.

    Берутся подписи программы кривой, в них ищется наша разновидность покупки,
    из счетов берётся base_mint и base_token_program. Выдумывать минты нельзя:
    у закрытой кривой покупка не пройдёт и проверка соврёт.
    """
    из_ = {"minty": [], "buyback": None, "prosmotreno": 0, "why_not": None}
    о = зов("getSignaturesForAddress", [СБ.ПРОГ_КРИВОЙ, {"limit": 1000}])
    if not о["ok"]:
        из_["why_not"] = f"подписи программы не прочитаны: {о['why_not']}"
        return из_
    видели = set()
    for зп in (о.get("result") or []):
        if len(из_["minty"]) >= skolko:
            break
        if зп.get("err"):
            continue
        подпись = зп.get("signature")
        if not подпись:
            continue
        из_["prosmotreno"] += 1
        if pauza:
            time.sleep(pauza)
        т = зов("getTransaction", [подпись, {"encoding": "jsonParsed",
                                              "maxSupportedTransactionVersion": ПОТОЛОК_ВЕРСИИ_TX,
                                              "commitment": "confirmed"}])
        if not т["ok"] or not т.get("result"):
            continue
        # РАЗБОР -- ОДНОЙ ФУНКЦИЕЙ, той же, что разбирает отказанные сигналы.
        к = _kriwaja_iz_tx(т["result"])
        if not к["ok"]:
            continue
        if из_["buyback"] is None and к.get("buyback"):
            из_["buyback"] = к["buyback"]
        if (к["base_token_program"] == СБ.ПРОГ_ТОКЕНА_2022
                and к["mint"] not in видели):
            видели.add(к["mint"])
            # ТРАНЗАКЦИЯ ИСТОЧНИКА ОСТАЁТСЯ ЦЕЛИКОМ: денежный путь
            # (extract_template) работает с ответом узла, а не с минтом.
            из_["minty"].append({
                "mint": к["mint"], "base_token_program": к["base_token_program"],
                "iz_podpisi": подпись, "tx": т["result"],
                "bazovyj_vault": к["bazovyj_vault"]})
    if not из_["minty"]:
        из_["why_not"] = "живых минтов кривой на Token-2022 не нашлось"
    elif not из_["buyback"]:
        из_["why_not"] = WHY_НЕТ_BUYBACK
    return из_


def krug_na_minte(*, mint: str, token_program: str, buyback: str,
                  koshelek: str, bilet_lamportov: int, cu: int,
                  pauza: float) -> dict:
    """Две симуляции: [ata, покупка] с чтением баланса и круг целиком."""
    из_ = {"mint": mint, "ok": False, "why_not": None,
           "ostatok_posle_pokupki": None}
    ата = СБ.ata(koshelek, token_program, mint)
    из_["ata"] = ата
    try:
        пок = СБ.pokupka_krivoj_v3(
            base_mint=mint, user=koshelek, buyback_fee_recipient=buyback,
            base_token_program=token_program,
            spendable_quote_in=int(bilet_lamportov), min_tokens_out=1)
    except СБ.ОшибкаСборки as сбой:
        из_["why_not"] = f"покупка не собралась: {сбой}"
        return из_
    ата_ix = ata_instrukciya(payer=koshelek, owner=koshelek, mint=mint,
                              token_program=token_program)
    пок_ix = {"programma": СБ.ПРОГ_КРИВОЙ, "scheta": пок["accounts"],
              "dannye": bytes.fromhex(пок["data_hex"])}
    # (а) ТОЛЬКО ПОКУПКА -- и читаем ATA: баланс нужен ИМЕННО ПОСЛЕ ПОКУПКИ.
    о_пок = simulirovat(tx_base64(instrukcii=[predel_cu(cu), ата_ix, пок_ix],
                                   platelshchik=koshelek), scheta=[ата])
    if not о_пок["ok"]:
        из_["why_not"] = f"узел не ответил на покупку: {о_пок['why_not']}"
        return из_
    из_["pokupka_err"] = о_пок["err"]
    из_["pokupka_units"] = о_пок["units"]
    из_["ostatok_posle_pokupki"] = ostatok_tokena(о_пок["accounts"])
    if о_пок["err"] is not None:
        из_["why_not"] = "покупка не прошла симуляцию"
        из_["logi"] = о_пок["logi"][-6:]
        return из_
    if not из_["ostatok_posle_pokupki"]:
        из_["why_not"] = ("покупка прошла, а токена на счёте "
                          f"{из_['ostatok_posle_pokupki']!r} -- это не успех")
        из_["logi"] = о_пок["logi"][-6:]
        return из_
    # (б) КРУГ ЦЕЛИКОМ: продаём ровно то, что купили.
    if pauza:
        time.sleep(pauza)
    try:
        прод = СБ.prodazha_krivoj_v3(
            base_mint=mint, user=koshelek, buyback_fee_recipient=buyback,
            base_token_program=token_program,
            amount=int(из_["ostatok_posle_pokupki"]), min_sol_output=1)
    except СБ.ОшибкаСборки as сбой:
        из_["why_not"] = f"продажа не собралась: {сбой}"
        return из_
    прод_ix = {"programma": СБ.ПРОГ_КРИВОЙ, "scheta": прод["accounts"],
               "dannye": bytes.fromhex(прод["data_hex"])}
    о_круг = simulirovat(tx_base64(
        instrukcii=[predel_cu(cu), ата_ix, пок_ix, прод_ix],
        platelshchik=koshelek))
    if not о_круг["ok"]:
        из_["why_not"] = f"узел не ответил на круг: {о_круг['why_not']}"
        return из_
    из_["krug_err"] = о_круг["err"]
    из_["krug_units"] = о_круг["units"]
    if о_круг["err"] is not None:
        из_["why_not"] = "круг не прошёл симуляцию"
        из_["logi"] = о_круг["logi"][-8:]
        return из_
    из_["ok"] = True
    return из_


# ----------------------------------------------- круг ЧЕРЕЗ ДЕНЕЖНЫЙ ПУТЬ

# ЗАЧЕМ ВТОРОЙ КРУГ (п.5 слова штаба 09.10). Круг выше зовёт СБОРЩИК напрямую:
# он доказывает, что байты сборщика узел принимает. Но полоса ходит не в
# сборщик -- она ходит в c2_swap_build.build_buy и c3_prodavec_sborka.подготовить,
# и ровно между ними и сборщиком стоит врезка, которую надо доказать. Поэтому
# здесь те же два шага, но ЧЕРЕЗ ФУНКЦИИ ДЕНЕЖНОГО ПУТИ: шаблон из живой сделки
# источника, покупка -- build_buy, продажа -- подготовить.
#
# ПОЧЕМУ ШАБЛОН ПРОДАЖИ БЕРЁТСЯ ИЗ СДЕЛКИ ИСТОЧНИКА, А НЕ ИЗ НАШЕЙ ПОКУПКИ.
# В работе подготовить получает НАШУ покупку, прочитанную с цепи. В симуляции
# нашей покупки на цепи нет: она не отправлена. Из сделки берутся ровно четыре
# значения -- base_mint, base_token_program, buyback_fee_recipient, quote_mint --
# и у нашей покупки они те же самые, потому что наша покупка собрана по тем же
# именам IDL того же минта. Подменять ответ узла своей выдумкой нельзя, поэтому
# подаётся настоящая транзакция источника, а не слепленная.


def _instrukcii_iz_tx64(tx64: str) -> list:
    """Инструкции обратно из собранной транзакции: программа, счета, байты.

    Нужно, чтобы СКЛЕИТЬ покупку и продажу в одну транзакцию: денежный путь
    отдаёт готовые транзакции по отдельности (в работе их две и подписей две),
    а узел покажет круг только если купля и продажа стоят в одной.
    """
    from solders.transaction import VersionedTransaction  # noqa: PLC0415

    vtx = VersionedTransaction.from_bytes(base64.b64decode(tx64))
    соо = vtx.message
    ключи = [str(k) for k in соо.account_keys]
    из_ = []
    for ци in соо.instructions:
        из_.append({
            "programma": ключи[ци.program_id_index],
            "dannye": bytes(ци.data),
            "scheta": [{"pubkey": ключи[и],
                        "isSigner": bool(соо.is_signer(и)),
                        "isWritable": bool(соо.is_maybe_writable(и))}
                       for и in list(ци.accounts)]})
    return из_


def _bez_budzheta(instrukcii: list) -> list:
    """Без ComputeBudget: два предела CU в одной транзакции -- отказ узла."""
    прог = "ComputeBudget111111111111111111111111111111"
    return [и for и in instrukcii if и["programma"] != прог]


# ОТКАЗЫ ПРОГРАММЫ, КОТОРЫЕ ГОВОРЯТ "КРИВАЯ ЗАКОНЧИЛАСЬ", А НЕ "СЧЕТА НЕ ТЕ".
# Оба номера -- из программы кривой, и оба приходят ПОСЛЕ того, как она приняла
# наш список счетов: 6005 BondingCurveComplete -- кривая добрана и уехала в
# пул, 6004 MintDoesNotMatchBondingCurve -- счёта кривой для этого минта на
# цепи уже нет (после переезда он закрыт, и по адресу лежит не тот минт).
# Отличать это от отказа по раскладке обязательно: отказанные сигналы полосы
# прогоняются СУТКИ СПУСТЯ, и часть кривых к тому моменту закрыта. Считать это
# красным врезки значило бы врать в обе стороны.
ЗАВЕРШЁННАЯ_КРИВАЯ = {
    "6005": "BondingCurveComplete -- кривая добрана и переехала в пул",
    "6004": "MintDoesNotMatchBondingCurve -- счёта кривой этого минта уже нет",
}
# Раскладка счёта кривой -- ИЗ IDL (тип BondingCurve), а не на глаз:
# дискриминатор 8, пять u64 (40), complete bool, creator 32, is_mayhem_mode,
# is_cashback_coin, quote_mint 32. Отсюда смещения.
СМЕЩЕНИЕ_COMPLETE = 48
СМЕЩЕНИЕ_CREATOR = 49
СМЕЩЕНИЕ_MAYHEM = 81
СМЕЩЕНИЕ_QUOTE_MINT = 83
ДЛИНА_СЧЁТА_КРИВОЙ = 166
WSOL = "So11111111111111111111111111111111111111112"
# НУЛЕВОЙ PUBKEY. В счёте кривой им помечена ПУСТОТА поля: у кривой с нативной
# котировкой отдельного минта котировки нет. Считать его "чужой котировкой"
# значило бы ответить ровно наоборот (см. mayhem_mintov).
ПУСТОЙ_PUBKEY = "11111111111111111111111111111111"


def klass_otkaza(logi: list) -> dict:
    """Отказ по раскладке счетов или "кривая закончилась"? По номеру программы."""
    из_ = {"klass": "raskladka", "kod": None, "slovami": None}
    for л in reversed(list(logi or [])):
        if "Error Number:" not in л:
            continue
        хвост = л.split("Error Number:", 1)[1].strip()
        код = хвост.split(".", 1)[0].strip()
        из_["kod"] = код
        if код in ЗАВЕРШЁННАЯ_КРИВАЯ:
            из_.update(klass="krivaya_zakonchilas",
                       slovami=ЗАВЕРШЁННАЯ_КРИВАЯ[код])
        return из_
    return из_


def sostoyanie_krivoj(bazovyj_vault: str, mint: str) -> dict:
    """Счёт кривой этого минта на цепи: есть ли и добрана ли (complete).

    Читается PDA ["bonding-curve", mint] -- тот же, что выводит сборщик.
    Нужно, чтобы "кривая закончилась" было ИЗМЕРЕНО, а не выведено из номера
    ошибки: номер говорит, что ответила программа, а счёт -- что на цепи.
    """
    из_ = {"schyot": None, "est": None, "complete": None, "why_not": None}
    try:
        from solders.pubkey import Pubkey  # noqa: PLC0415

        пда, _ = Pubkey.find_program_address(
            [b"bonding-curve", bytes(Pubkey.from_string(mint))],
            Pubkey.from_string(СБ.ПРОГ_КРИВОЙ))
        из_["schyot"] = str(пда)
    except Exception as сбой:  # noqa: BLE001
        из_["why_not"] = f"PDA кривой не вывелся: {type(сбой).__name__}"
        return из_
    о = зов("getAccountInfo", [из_["schyot"], {"encoding": "base64"}])
    if not о["ok"]:
        из_["why_not"] = f"счёт кривой не прочитан: {о['why_not']}"
        return из_
    зн = (о.get("result") or {}).get("value")
    из_["est"] = bool(зн)
    if not зн:
        return из_
    try:
        сырые = base64.b64decode((зн.get("data") or [""])[0])
    except Exception:  # noqa: BLE001
        из_["why_not"] = "данные счёта не раскодировались"
        return из_
    из_["dlina"] = len(сырые)
    if len(сырые) > СМЕЩЕНИЕ_COMPLETE:
        из_["complete"] = bool(сырые[СМЕЩЕНИЕ_COMPLETE])
    # КОТИРОВКА КРИВОЙ -- ИЗ САМОГО СЧЁТА. Наша сборка v3 кладёт WSOL; если у
    # кривой котировка другая, программа отвечает 6004
    # MintDoesNotMatchBondingCurve -- тем же номером, что и при отсутствии
    # счёта. Отличить одно от другого можно только прочитав поле.
    if len(сырые) >= СМЕЩЕНИЕ_QUOTE_MINT + 32:
        try:
            from solders.pubkey import Pubkey  # noqa: PLC0415

            из_["quote_mint"] = str(Pubkey(
                сырые[СМЕЩЕНИЕ_QUOTE_MINT:СМЕЩЕНИЕ_QUOTE_MINT + 32]))
            из_["quote_wsol"] = (из_["quote_mint"] == WSOL)
            из_["creator"] = str(Pubkey(
                сырые[СМЕЩЕНИЕ_CREATOR:СМЕЩЕНИЕ_CREATOR + 32]))
            из_["is_mayhem_mode"] = bool(сырые[СМЕЩЕНИЕ_MAYHEM])
        except Exception:  # noqa: BLE001
            из_["quote_mint"] = None
    return из_


def chem_kupleno(podpisi: list, *, pauza: float) -> dict:
    """ЧЕМ собрана НАША транзакция: имя инструкции кривой по IDL и счета.

    Только чтение цепи, ни одной симуляции. Нужно, чтобы про СВОЮ севшую
    покупку говорить именем разновидности, а не выводить его из того, что
    полоса отказывала раньше. Разбор -- по дискриминатору против IDL, то есть
    тем же способом, которым врезка узнаёт сделку источника.
    """
    из_ = {"podpisej": len(podpisi), "nashi": [], "why_not": None}
    идл = СБ.zagruzit_idl()["pump"]["ix"]
    по_диску = {зн["disc"].hex(): имя for имя, зн in идл.items()}
    for п in podpisi:
        п = str(п).strip()
        if not п:
            continue
        т = зов("getTransaction", [п, {"encoding": "jsonParsed",
                                        "maxSupportedTransactionVersion": ПОТОЛОК_ВЕРСИИ_TX,
                                        "commitment": "confirmed"}])
        if not т["ok"] or not т.get("result"):
            из_["nashi"].append({"podpis": п, "why_not": f"не прочитана: "
                                                         f"{т.get('why_not')}"})
            continue
        р = т["result"]
        соо = ((р.get("transaction") or {}).get("message") or {})
        ключи = [k.get("pubkey") if isinstance(k, dict) else k
                 for k in (соо.get("accountKeys") or [])]
        инстр = list(соо.get("instructions") or [])
        for вн in (р.get("meta") or {}).get("innerInstructions") or []:
            инстр.extend(вн.get("instructions") or [])
        зп = {"podpis": п, "slot": р.get("slot"), "err": (р.get("meta") or {}).get("err"),
              "krivaya": [], "programmy": sorted({и.get("programId") for и in инстр
                                                  if и.get("programId")})}
        for и in инстр:
            данные = и.get("data")
            if и.get("programId") != СБ.ПРОГ_КРИВОЙ or not isinstance(данные, str):
                continue
            try:
                сырые = СБ.b58d(данные)
            except Exception:  # noqa: BLE001
                continue
            имя = по_диску.get(сырые[:8].hex())
            счета = [ключи[с] if isinstance(с, int) and с < len(ключи) else с
                     for с in (и.get("accounts") or [])]
            стр = {"imya": имя or "НЕ В IDL", "disc": сырые[:8].hex(),
                   "schetov": len(счета)}
            if имя and имя in идл:
                имена = [а["name"] for а in идл[имя]["accounts"]]
                по_именам = dict(zip(имена, счета, strict=False))
                стр.update(base_mint=по_именам.get("base_mint"),
                           base_token_program=по_именам.get("base_token_program"),
                           user=по_именам.get("user"))
            зп["krivaya"].append(стр)
        из_["nashi"].append(зп)
        if pauza:
            time.sleep(pauza)
    if not any(з.get("krivaya") for з in из_["nashi"]):
        из_["why_not"] = "ни в одной транзакции инструкции кривой не нашлось"
    return из_


def krug_denezhnogo_puti(*, tx_istochnika: dict, mint: str, token_program: str,
                         bazovyj_vault: str, koshelek: str,
                         bilet_lamportov: int, cu: int, pauza: float) -> dict:
    """Круг ФУНКЦИЯМИ ПОЛОСЫ: extract_template -> build_buy -> подготовить."""
    import c2_swap_build as SB  # noqa: PLC0415
    import c3_prodavec_sborka as SP  # noqa: PLC0415

    из_ = {"mint": mint, "ok": False, "why_not": None, "put": "denezhnyj",
           "ostatok_posle_pokupki": None}
    if not bazovyj_vault:
        из_["why_not"] = "в сделке источника нет associated_base_bonding_curve"
        return из_
    ш = SB.extract_template(tx_istochnika, SB.BONDING, bazovyj_vault)
    if not ш.get("ok"):
        из_["why_not"] = f"шаблон не снялся: {ш.get('why_not')}"
        return из_
    из_.update(po_idl=ш.get("po_idl"), exact_out=ш.get("exact_out"),
               buyback=ш.get("buyback_fee_recipient"))
    if not ш.get("po_idl"):
        из_["why_not"] = ("шаблон снялся СТАРЫМ путём (po_idl пуст) -- "
                          "врезка не задействована")
        return из_
    ата = SB.ata(koshelek, mint, token_program)
    из_["ata"] = ата
    # МИНИМУМ ВЫХОДА -- ТЕМ ЖЕ СЧЁТОМ, ЧТО В ПОЛОСЕ (min_out_from_reserves,
    # bloom_own_send:3193), А НЕ ЕДИНИЦЕЙ.
    #
    # ЗДЕСЬ СТОЯЛА ЕДИНИЦА, И ПРОГОН 38014760631 ПОКАЗАЛ, ЧЕМ ЭТО ПЛАТИТСЯ.
    # У «точного входа» (buy_exact_quote_in_v3) единица безвредна: это лишь
    # нижний порог, трата задана отдельно. А у «точного ВЫХОДА» (buy_v3)
    # первым аргументом идёт КОЛИЧЕСТВО ТОКЕНА -- и единица означала «купить
    # один сырой юнит»: восемь подписей из двенадцати давали остаток РОВНО 1 и
    # красный круг. Это был отказ ПРОВЕРКИ, а не пути, и по имени поля этого
    # не видно: ostatok_posle_pokupki=1 выглядит как купленный токен.
    мо = SB.min_out_from_reserves(ш, tx_istochnika, int(bilet_lamportov), 0.4)
    из_.update(min_out_ok=bool(мо.get("ok")),
               min_out=мо.get("min_out") if мо.get("ok") else None,
               expected_out=мо.get("expected_out") if мо.get("ok") else None,
               min_out_why_not=None if мо.get("ok") else мо.get("why_not"))
    if мо.get("ok") and int(мо.get("min_out") or 0) > 0:
        _min_out = int(мо["min_out"])
    elif ш.get("exact_out"):
        # ТОЧНЫЙ ВЫХОД БЕЗ СЧЁТА -- ОТКАЗ, А НЕ ЕДИНИЦА: количество токена
        # выдумывать нечем, а единица проверила бы покупку одного юнита.
        из_["why_not"] = (f"точный выход, а минимум по резервам не посчитан: "
                          f"{мо.get('why_not')}")
        return из_
    else:
        _min_out = 1
    из_["min_out_v_sborke"] = _min_out
    try:
        # close_wsol=False: продажа в том же круге пишет на счёт WSOL, а
        # закрытие покупки его бы уже снесло. В работе круг -- две транзакции,
        # и там закрытие покупки на своём месте.
        пок = SB.build_buy(ш, tx_istochnika, user=koshelek, payer=koshelek,
                           amount_in=int(bilet_lamportov), min_out=_min_out,
                           cu_units=int(cu), wrap_sol=True, close_wsol=False)
    except Exception as сбой:  # noqa: BLE001
        из_["why_not"] = f"build_buy: {type(сбой).__name__}: {str(сбой)[:160]}"
        # НАШ СОБСТВЕННЫЙ НАЗВАННЫЙ ОТКАЗ -- НЕ КРАСНОЕ ВРЕЗКИ. Котировка
        # кривой не WSOL значит, что полоса этой кривой купить не может по
        # сути: она платит солами. Сборка отказывает ДО подписи, и это
        # правильное поведение, а не поломка раскладки.
        if "не WSOL" in str(сбой):
            из_.update(klass_otkaza="kotirovka_ne_wsol",
                       otkaz_slovami=str(сбой)[:200], raskladka_prinyata=True)
            из_["krivaya"] = sostoyanie_krivoj(bazovyj_vault, mint)
        return из_
    из_.update(razmer_pokupki=пок.get("size"),
               instrukcij_pokupki=пок.get("n_instructions"),
               base_mint_puti=пок.get("base_mint"),
               quote_mint_puti=пок.get("quote_mint"))
    if пок.get("base_mint") != mint:
        из_["why_not"] = (f"денежный путь взял минт {str(пок.get('base_mint'))[:8]}, "
                          f"а минт сделки {mint[:8]}")
        return из_
    о_пок = simulirovat(пок["tx_base64"], scheta=[ата])
    if not о_пок["ok"]:
        из_["why_not"] = f"узел не ответил на покупку: {о_пок['why_not']}"
        return из_
    из_.update(pokupka_err=о_пок["err"], pokupka_units=о_пок["units"],
               ostatok_posle_pokupki=ostatok_tokena(о_пок["accounts"]))
    if о_пок["err"] is not None:
        из_["logi"] = о_пок["logi"][-8:]
        к = klass_otkaza(о_пок["logi"])
        из_.update(klass_otkaza=к["klass"], kod_otkaza=к["kod"],
                   otkaz_slovami=к["slovami"])
        из_["krivaya"] = sostoyanie_krivoj(bazovyj_vault, mint)
        если_закончилась = (к["klass"] == "krivaya_zakonchilas")
        из_["why_not"] = (
            f"кривая закончилась, повторить сигнал суткам позже нечем: "
            f"{к['slovami']}" if если_закончилась
            else "покупка денежного пути не прошла симуляцию")
        # ЗАКОНЧИВШАЯСЯ КРИВАЯ -- НЕ КРАСНОЕ ВРЕЗКИ, И ПОЛЕ ЭТО ГОВОРИТ.
        # Решает вызывающий: ok остаётся False (покупки не было), но
        # отдельным признаком видно, что программа ПРИНЯЛА раскладку и
        # отказала по состоянию кривой, а не по счетам.
        из_["raskladka_prinyata"] = если_закончилась
        return из_
    if not из_["ostatok_posle_pokupki"]:
        из_["why_not"] = ("покупка прошла, а токена на счёте "
                          f"{из_['ostatok_posle_pokupki']!r} -- это не успех")
        из_["logi"] = о_пок["logi"][-8:]
        return из_
    if pauza:
        time.sleep(pauza)
    прод = SP.подготовить(
        tx_istochnika, программа=SB.BONDING, наш_кошелёк=koshelek,
        база_в=int(из_["ostatok_posle_pokupki"]), минт_базы=mint,
        котировка_типа=1, пол_лампорты=1, продаём_всё=True, cu_units=int(cu))
    из_.update(sposob_prodazhi=прод.get("способ"),
               imya_prodazhi=прод.get("имя_инструкции"),
               programma_bazy=прод.get("программа_базы"),
               zakryt_schyot_tokena=прод.get("закрыт_счёт_токена"),
               zakryt_schyot_wsol=прод.get("закрыт_счёт_wsol"))
    if not прод.get("ok"):
        из_["why_not"] = f"подготовить: {прод.get('why_not')}"
        return из_
    if прод.get("программа_базы") != token_program:
        из_["why_not"] = (f"продажа закрывает счёт программой "
                          f"{str(прод.get('программа_базы'))[:8]}, а минт на "
                          f"{token_program[:8]}")
        return из_
    круг_ix = _instrukcii_iz_tx64(пок["tx_base64"]) + \
        _bez_budzheta(_instrukcii_iz_tx64(прод["tx_base64"]))
    о_круг = simulirovat(tx_base64(instrukcii=круг_ix, platelshchik=koshelek))
    if not о_круг["ok"]:
        из_["why_not"] = f"узел не ответил на круг: {о_круг['why_not']}"
        return из_
    из_.update(krug_err=о_круг["err"], krug_units=о_круг["units"],
               instrukcij_kruga=len(круг_ix))
    if о_круг["err"] is not None:
        из_["why_not"] = "круг денежного пути не прошёл симуляцию"
        из_["logi"] = о_круг["logi"][-10:]
        return из_
    из_["ok"] = True
    return из_


def _kriwaja_iz_tx(t: dict) -> dict:
    """Наша разновидность покупки кривой в ответе узла -- по именам IDL.

    Один разбор на оба применения: поиск живых минтов по программе и разбор
    ИМЕННО ТЕХ сделок, на которых полоса отказала (их подписи приходят из
    журнала решений). Второй раз писать то же значило бы разойтись.
    """
    из_ = {"ok": False, "why_not": None}
    _идл = СБ.zagruzit_idl()["pump"]["ix"]
    по_диску = {_идл[в]["disc"]: [а["name"] for а in _идл[в]["accounts"]]
                for в in ПОКУПКИ_ИСТОЧНИКА}
    соо = ((t or {}).get("transaction") or {}).get("message") or {}
    ключи = [k.get("pubkey") if isinstance(k, dict) else k
             for k in (соо.get("accountKeys") or [])]
    инстр = list(соо.get("instructions") or [])
    for вн in ((t or {}).get("meta") or {}).get("innerInstructions") or []:
        инстр.extend(вн.get("instructions") or [])
    for и in инстр:
        данные = и.get("data")
        if и.get("programId") != СБ.ПРОГ_КРИВОЙ or not isinstance(данные, str):
            continue
        try:
            сырые = СБ.b58d(данные)
        except Exception:  # noqa: BLE001
            continue
        имена = по_диску.get(сырые[:8])
        if имена is None:
            continue
        счета = [ключи[с] if isinstance(с, int) and с < len(ключи) else с
                 for с in (и.get("accounts") or [])]
        по_именам = dict(zip(имена, счета, strict=False))
        if not по_именам.get("base_mint"):
            continue
        return {"ok": True, "why_not": None, "mint": по_именам["base_mint"],
                "base_token_program": по_именам.get("base_token_program"),
                "buyback": по_именам.get("buyback_fee_recipient"),
                "bazovyj_vault": по_именам.get("associated_base_bonding_curve"),
                "pokupka_istochnika": next(
                    (в for в in ПОКУПКИ_ИСТОЧНИКА
                     if _идл[в]["disc"] == сырые[:8]), None)}
    из_["why_not"] = (f"покупки кривой {list(ПОКУПКИ_ИСТОЧНИКА)} в транзакции "
                      "не найдено")
    return из_


def _modul_iz_kommita(ref: str, otnositelno: str, imya: str):
    """Модуль ИЗ КОММИТА, рядом с нынешним. Для сравнения двух версий разбора.

    Берётся git show, кладётся во временный каталог и грузится под другим
    именем -- так в одном прогоне живут обе версии и сравниваются на ОДНИХ
    транзакциях. Иначе "решения совпадают" пришлось бы принимать на слово.
    """
    import importlib.util  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    вых = subprocess.run(["git", "show", f"{ref}:{otnositelno}"],
                          capture_output=True, text=True, check=False)
    if вых.returncode != 0 or not вых.stdout.strip():
        raise RuntimeError(f"git show {ref}:{otnositelno} не дал текста: "
                            f"{(вых.stderr or '')[:160]}")
    кат = Path(tempfile.mkdtemp())
    фл = кат / f"{imya}.py"
    фл.write_text(вых.stdout, encoding="utf-8")
    спец = importlib.util.spec_from_file_location(imya, фл)
    мод = importlib.util.module_from_spec(спец)
    sys.modules[imya] = мод
    спец.loader.exec_module(мод)
    return мод


def _snimok_shablona(ш: dict) -> dict:
    """Что именно сравниваем у шаблона покупки. None -- отказ."""
    if not isinstance(ш, dict):
        return {"ok": False, "why_not": "шаблона нет"}
    return {"ok": bool(ш.get("ok")),
             "why_not": (str(ш.get("why_not"))[:140]
                          if ш.get("why_not") else None),
             "ix": ш.get("ix"), "po_idl": ш.get("po_idl"),
             "exact_out": ш.get("exact_out"),
             "schetov": len(ш.get("accounts") or []),
             "dannye_8": (ш.get("data") or b"")[:8].hex()
                         if isinstance(ш.get("data"), (bytes, bytearray)) else None,
             "arg0": ш.get("arg0"), "arg1": ш.get("arg1")}


# ПОВТОРОВ ЗАМЕРА ВРЕМЕНИ НА ОДНУ ПОДПИСЬ. Разбор идёт десятки микросекунд,
# один прогон измерял бы в основном шум; 25 повторов дают устойчивый минимум и
# стоят миллисекунды на подпись, то есть замер не удлиняет прогон заметно.
ПОВТОРОВ_ЗАМЕРА = 25


def sravnit_versii_razbora(*, podpisi: list, ref_staryj: str,
                            pauza: float) -> dict:
    """ОДНИ транзакции -- ДВЕ версии разбора сделки источника.

    Слово владельца 10.10 (вечер): перед откатом прогнать сегодняшние сигналы
    обеими версиями и сказать, сколько решений расходится, построчно.
    Сравнивается то, что решает судьбу сделки: прошёл ли шаблон, какая
    разновидность инструкции, сколько счетов, первые восемь байт данных и
    аргументы -- то есть ЧЕМ собралась бы наша покупка.
    """
    из_ = {"podpisej": 0, "sovpalo": 0, "razoshlos": 0, "ne_prochitano": 0,
            "ref_staryj": ref_staryj, "sdelki": [], "why_not": None}
    try:
        стар = _modul_iz_kommita(ref_staryj, "analysis/c2_swap_build.py",
                                  "c2_swap_build_staryj")
    except Exception as сбой:  # noqa: BLE001
        из_["why_not"] = f"старая версия не загружена: {сбой}"
        return из_
    import c2_swap_build as нов  # noqa: PLC0415

    for п in podpisi:
        п = str(п).strip()
        if not п:
            continue
        из_["podpisej"] += 1
        т = зов("getTransaction", [п, {
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": ПОТОЛОК_ВЕРСИИ_TX,
            "commitment": "confirmed"}])
        if not т["ok"] or not т.get("result"):
            из_["ne_prochitano"] += 1
            из_["sdelki"].append({"podpis": п, "sovpalo": None,
                                   "why_not": f"не прочитана: {т.get('why_not')}"})
            continue
        tx = т["result"]
        к = _kriwaja_iz_tx(tx)
        хранилище = к.get("bazovyj_vault")
        ряд = {"podpis": п, "mint": к.get("mint"),
                "pokupka_istochnika": к.get("pokupka_istochnika"),
                "hranilishche": хранилище}
        if not хранилище:
            # НЕТ ХРАНИЛИЩА -- СРАВНИВАТЬ НЕЧЕГО, и это тоже надо сказать.
            ряд.update(sovpalo=None, why_not=k_why(к))
            из_["ne_prochitano"] += 1
            из_["sdelki"].append(ряд)
            continue
        снимки = {}
        # ВРЕМЯ РАЗБОРА -- ВОПРОС ВЛАДЕЛЬЦА 10.10 (вечер, п.2в): "сравнение
        # версий проверяло решения, не скорость. Прогнать обе версии на одних
        # и тех же сигналах на время: мс на разбор, медиана и p99".
        #
        # ЗАМЕР БЕЗ СЕТИ: транзакция уже прочитана выше, extract_template --
        # чистый разбор готового словаря. ПОВТОРОВ_ЗАМЕРА прогонов на каждую
        # версию и каждую подпись, из них берётся МИНИМУМ: минимум -- это
        # время самого разбора, а средняя по повторам мерила бы ещё и шум
        # сборщика мусора на бегунке. Разброс сохраняется отдельно, чтобы
        # "минимум" не выглядел подогнанным.
        for имя, мод in (("staraya", стар), ("novaya", нов)):
            try:
                снимки[имя] = _snimok_shablona(
                    мод.extract_template(tx, мод.BONDING, хранилище))
            except Exception as сбой:  # noqa: BLE001
                снимки[имя] = {"ok": False,
                                "why_not": f"{type(сбой).__name__}: "
                                           f"{str(сбой)[:120]}"}
            прогоны = []
            for _ in range(ПОВТОРОВ_ЗАМЕРА):
                _т0 = time.perf_counter()
                try:
                    мод.extract_template(tx, мод.BONDING, хранилище)
                except Exception:  # noqa: BLE001, S110
                    pass
                прогоны.append((time.perf_counter() - _т0) * 1000.0)
                ряд[f"ms_{имя}"] = round(min(прогоны), 4)
                ряд[f"ms_{имя}_max"] = round(max(прогоны), 4)
        ряд["staraya"] = снимки["staraya"]
        ряд["novaya"] = снимки["novaya"]
        if (ряд.get("ms_staraya") is not None
                and ряд.get("ms_novaya") is not None):
            ряд["ms_raznica"] = round(ряд["ms_novaya"] - ряд["ms_staraya"], 4)
        отличия = [к_ for к_ in sorted(set(снимки["staraya"])
                                        | set(снимки["novaya"]))
                    if снимки["staraya"].get(к_) != снимки["novaya"].get(к_)]
        ряд["otlichiya"] = отличия
        ряд["sovpalo"] = not отличия
        из_["sovpalo" if not отличия else "razoshlos"] += 1
        из_["sdelki"].append(ряд)
        if pauza:
            time.sleep(pauza)
    # СВОД ПО ВРЕМЕНИ. Медиана и p99 -- по тем подписям, где обе версии
    # измерились; число таких названо рядом, чтобы медиана не читалась как
    # "по всем".
    пары = [(р["ms_staraya"], р["ms_novaya"]) for р in из_["sdelki"]
            if р.get("ms_staraya") is not None
            and р.get("ms_novaya") is not None]
    if пары:
        из_["vremya"] = {
            "izmereno_podpisej": len(пары),
            "povtorov_na_podpis": ПОВТОРОВ_ЗАМЕРА,
            "staraya_ms_mediana": _mediana([а for а, _ in пары]),
            "staraya_ms_p99": _procentil([а for а, _ in пары], 99),
            "novaya_ms_mediana": _mediana([б for _, б in пары]),
            "novaya_ms_p99": _procentil([б for _, б in пары], 99),
            "raznica_ms_mediana": _mediana([б - а for а, б in пары]),
            "raznica_ms_p99": _procentil([б - а for а, б in пары], 99),
            "chto_znachit": (
                "разница -- это НОВАЯ минус СТАРАЯ на одной и той же "
                "транзакции; плюс значит, что правленый разбор медленнее"),
        }
    else:
        из_["vremya"] = {"izmereno_podpisej": 0,
                          "why_not": "ни одной подписи с обоими замерами"}
    if из_["razoshlos"]:
        из_["why_not"] = (f"решения разошлись на {из_['razoshlos']} подписях "
                           f"из {из_['podpisej']}")
    return из_


def _mediana(числа: list):
    """Медиана без numpy: он на бегунке не нужен ради одного числа."""
    р = sorted(x for x in числа if isinstance(x, (int, float)))
    if not р:
        return None
    н = len(р)
    серёдка = (р[н // 2] if н % 2 else (р[н // 2 - 1] + р[н // 2]) / 2.0)
    return round(серёдка, 4)


def _procentil(числа: list, доля: int):
    """Процентиль методом ближайшего ранга (как у замеров полосы).

    БЕЗ ИНТЕРПОЛЯЦИИ нарочно: p99 по 10 числам -- это НАИБОЛЬШЕЕ из них, и
    притворяться, что мы знаем девяносто девятый процентиль точнее, чем
    позволяет выборка, нечестно. Число измерений печатается рядом.
    """
    р = sorted(x for x in числа if isinstance(x, (int, float)))
    if not р:
        return None
    import math  # noqa: PLC0415

    и = max(0, min(len(р) - 1, math.ceil(доля / 100.0 * len(р)) - 1))
    return round(р[и], 4)


# РАЗМЕРЫ ПРОСТЫХ ТИПОВ BORSH -- ТОЛЬКО ТЕ, ЧТО ЕСТЬ В СЧЁТЕ КРИВОЙ. Чего в
# таблице нет, обрывает счёт смещений с названной причиной: молча пропустить
# неизвестное поле значило бы сдвинуть все следующие и прочитать mayhem не там.
РАЗМЕРЫ_BORSH = {"u8": 1, "i8": 1, "u16": 2, "i16": 2, "u32": 4, "i32": 4,
                  "u64": 8, "i64": 8, "u128": 16, "i128": 16,
                  "bool": 1, "pubkey": 32}


def smeshchenija_krivoj() -> dict:
    """{имя поля: (смещение, тип)} в счёте BondingCurve -- ИЗ IDL, не руками.

    ЗАЧЕМ ИЗ IDL. is_mayhem_mode лежит в счёте кривой девятым полем, и его
    смещение зависит от ВСЕХ предыдущих. Числом в коде оно жило бы до первой
    правки программы pump.fun, а ошибка смещения на один байт читается как
    "mayhem там, где его нет", то есть как настоящий ответ. Поэтому смещения
    складываются по тому же IDL, которым собирается покупка, и первое же
    неизвестное поле обрывает счёт причиной.

    Восемь байт дискриминатора учтены: данные счёта начинаются с него.
    """
    из_ = {"polya": {}, "why_not": None, "razmer": None}
    типы = (СБ.zagruzit_idl()["pump"].get("types")
            or {}) if hasattr(СБ, "zagruzit_idl") else {}
    поля = None
    if isinstance(типы, dict):
        т = типы.get("BondingCurve") or {}
        поля = (т.get("type") or {}).get("fields") or т.get("fields")
    if not поля:
        # IDL в сборщике разложен иначе -- читаем файл IDL напрямую тем же
        # путём, которым его берёт сборщик.
        путь = Path(__file__).resolve().parents[2] / "data" / "idl" / "pump.json"
        if not путь.exists():
            из_["why_not"] = f"IDL кривой не найден: {путь}"
            return из_
        with open(путь, encoding="utf-8") as ф:
            сыро = json.load(ф)
        for т in (сыро.get("types") or []):
            if т.get("name") == "BondingCurve":
                поля = (т.get("type") or {}).get("fields") or []
                break
    if not поля:
        из_["why_not"] = "в IDL нет типа BondingCurve с полями"
        return из_
    сдвиг = 8  # дискриминатор счёта
    for ф_ in поля:
        имя = ф_.get("name")
        тип = ф_.get("type")
        if not isinstance(тип, str) or тип not in РАЗМЕРЫ_BORSH:
            из_["why_not"] = (f"поле {имя}: тип {тип!r} не из простых -- счёт "
                               f"смещений обрываю здесь, чтобы не прочитать "
                               f"следующие поля не там")
            break
        из_["polya"][имя] = (сдвиг, тип)
        сдвиг += РАЗМЕРЫ_BORSH[тип]
    из_["razmer"] = сдвиг
    return из_


def mayhem_mintov(*, minty: list, pauza: float) -> dict:
    """is_mayhem_mode и котировка по счёту кривой каждого минта.

    Вопрос владельца 10.10 (вечер, доп. к п.2д): "Mayhem ли минты дневных
    покупок источника против ночных". Флаг лежит в СЧЁТЕ КРИВОЙ (PDA
    bonding-curve + минт) и ставится при создании, поэтому чтение сейчас даёт
    то же значение, что было на покупке.

    ТОЛЬКО ЧТЕНИЕ: getAccountInfo на счёт кривой, ни одной отправки.
    """
    из_ = {"mintov": 0, "prochitano": 0, "ne_prochitano": 0,
            "mayhem_da": 0, "mayhem_net": 0, "kotirovka_ne_sol": 0,
            "kotirovka_nativnyj_sol": 0, "kotirovka_wsol": 0,
            "stroki": [], "why_not": None}
    см = smeshchenija_krivoj()
    if см.get("why_not") and "is_mayhem_mode" not in см["polya"]:
        из_["why_not"] = f"смещения по IDL не сложились: {см['why_not']}"
        return из_
    из_["smeshchenija_why_not"] = см.get("why_not")
    поле_м = см["polya"].get("is_mayhem_mode")
    поле_к = см["polya"].get("quote_mint")
    поле_с = см["polya"].get("complete")
    if not поле_м:
        из_["why_not"] = "в IDL нет поля is_mayhem_mode"
        return из_
    из_["smeshchenie_mayhem"] = поле_м[0]
    for м in minty:
        м = str(м).strip()
        if not м:
            continue
        из_["mintov"] += 1
        ряд = {"mint": м}
        try:
            кривая, _ = СБ.pda([b"bonding-curve", СБ.b58d(м)], СБ.ПРОГ_КРИВОЙ)
        except Exception as сбой:  # noqa: BLE001
            ряд["why_not"] = f"PDA кривой не выведен: {type(сбой).__name__}"
            из_["ne_prochitano"] += 1
            из_["stroki"].append(ряд)
            continue
        ряд["krivaya"] = str(кривая)
        о = зов("getAccountInfo", [str(кривая), {"encoding": "base64"}])
        знач = ((о.get("result") or {}) or {}).get("value") if о.get("ok") else None
        if not знач:
            ряд["why_not"] = (f"счёт кривой не прочитан: "
                               f"{о.get('why_not') or 'счёта нет'}")
            из_["ne_prochitano"] += 1
            из_["stroki"].append(ряд)
            if pauza:
                time.sleep(pauza)
            continue
        сырые = base64.b64decode((знач.get("data") or ["", ""])[0])
        ряд["bajt_schyota"] = len(сырые)
        сдв, _т = поле_м
        if len(сырые) <= сдв:
            ряд["why_not"] = (f"счёт короче смещения is_mayhem_mode "
                               f"({len(сырые)} <= {сдв})")
            из_["ne_prochitano"] += 1
            из_["stroki"].append(ряд)
            continue
        байт = сырые[сдв]
        # БАЙТ НЕ 0 И НЕ 1 -- НЕ "mayhem выключен", А НЕИЗВЕСТНОЕ. Выдавать
        # мусор за False значило бы ответить владельцу числом, которого нет.
        ряд["is_mayhem_mode"] = (bool(байт) if байт in (0, 1) else None)
        if байт not in (0, 1):
            ряд["why_not"] = (f"на месте is_mayhem_mode байт {байт} -- это не "
                               "bool, смещение или раскладка не те")
        if поле_к and len(сырые) >= поле_к[0] + 32:
            ряд["quote_mint"] = СБ.b58e(сырые[поле_к[0]:поле_к[0] + 32])
        if поле_с and len(сырые) > поле_с[0]:
            ряд["complete"] = bool(сырые[поле_с[0]])
        if ряд.get("is_mayhem_mode") is True:
            из_["mayhem_da"] += 1
        elif ряд.get("is_mayhem_mode") is False:
            из_["mayhem_net"] += 1
        # НУЛЕВОЙ PUBKEY -- ЭТО НАТИВНЫЙ SOL, А НЕ "ЧУЖАЯ КОТИРОВКА".
        # ЖИВОЙ СЛУЧАЙ 10.10: у всех 67 кривых quote_mint =
        # 11111111111111111111111111111111, то есть поле ПУСТОЕ -- у кривой с
        # нативной котировкой минта котировки нет вовсе. Прежний счёт
        # ("не равно WSOL -- значит не SOL") дал бы владельцу "котировка не SOL
        # у 67 из 67", то есть ровно обратное правде.
        _кот = ряд.get("quote_mint")
        ряд["kotirovka"] = ("нативный SOL" if _кот in (ПУСТОЙ_PUBKEY, None)
                             else ("WSOL" if _кот == WSOL else _кот))
        if _кот and _кот not in (WSOL, ПУСТОЙ_PUBKEY):
            из_["kotirovka_ne_sol"] += 1
        elif _кот == ПУСТОЙ_PUBKEY:
            из_["kotirovka_nativnyj_sol"] = (
                int(из_.get("kotirovka_nativnyj_sol") or 0) + 1)
        elif _кот == WSOL:
            из_["kotirovka_wsol"] = int(из_.get("kotirovka_wsol") or 0) + 1
        из_["prochitano"] += 1
        из_["stroki"].append(ряд)
        if pauza:
            time.sleep(pauza)
    return из_


def _blok_po_schetam(слот: int, *, таймаут: float = 90.0) -> dict:
    """Блок одним запросом: подписи В ПОРЯДКЕ БЛОКА и счета каждой сделки.

    transactionDetails="accounts" НАРОЧНО, А НЕ "full". Полный блок кривой
    весит единицы мегабайт, и 69 сделок дали бы сотни мегабайт трафика ради
    двух чисел. Раскладка "accounts" отдаёт подписи и список счетов -- этого
    хватает и на место в блоке, и на счёт соседей по минту, и стоит на
    порядок меньше.
    """
    о = зов("getBlock", [int(слот), {
        "encoding": "jsonParsed",
        "transactionDetails": "accounts",
        "rewards": False,
        "commitment": "confirmed",
        "maxSupportedTransactionVersion": ПОТОЛОК_ВЕРСИИ_TX}], таймаут=таймаут)
    if not о["ok"]:
        return {"ok": False, "why_not": о.get("why_not")}
    б = о.get("result") or {}
    сделки = б.get("transactions") or []
    ряды = []
    for т in сделки:
        тр = т.get("transaction") or {}
        подписи = тр.get("signatures") or []
        счета = [(с.get("pubkey") if isinstance(с, dict) else str(с))
                  for с in (тр.get("accountKeys") or [])]
        ряды.append({"podpis": подписи[0] if подписи else None,
                      "scheta": счета,
                      "err": bool((т.get("meta") or {}).get("err"))})
    return {"ok": True, "vsego": len(ряды), "ryady": ряды,
            "blockTime": б.get("blockTime"), "blockHeight": б.get("blockHeight")}


def mesta_v_bloke(*, vygruzka: str, pauza: float) -> dict:
    """Место сделки ИСТОЧНИКА и нашей в их блоках + соседи по минту.

    Вопрос владельца 10.10 (вечер, п.1): "место транзакции источника в её
    блоке (индекс / размер), ночь против дня. Если днём источник сам садится
    позже в своём блоке -- +40 мс не наши. Из тех же блоков: сколько покупок
    того же минта стояло перед нами".

    ЗАЧЕМ АРХИВНЫЙ УЗЕЛ. Догонялка соседей на хосте отказывала словами
    "getBlock: Block not available for slot ..." -- узел хоста старых блоков
    не держит. Этот прогон идёт с бегунка по Helius, у которого они есть.

    ТОЛЬКО ЧТЕНИЕ: getBlock и ничего больше.
    """
    из_ = {"vygruzka": vygruzka, "sdelok": 0, "prochitano": 0,
            "ne_prochitano": 0, "slotov_sprosheno": 0, "stroki": [],
            "why_not": None}
    путь = Path(vygruzka)
    if not путь.exists():
        из_["why_not"] = f"выгрузки нет: {vygruzka}"
        return из_
    with open(путь, encoding="utf-8") as ф:
        д = json.load(ф)
    ряды = [r for r in (д.get("ряды") or []) if r.get("source_sig")]
    из_["sdelok"] = len(ряды)
    кэш: dict = {}

    def блок(слот):
        if слот in кэш:
            return кэш[слот]
        б = _blok_po_schetam(слот)
        кэш[слот] = б
        из_["slotov_sprosheno"] += 1
        if pauza:
            time.sleep(pauza)
        return б

    for r in ряды:
        стр = {"utc": r.get("utc"), "mint": r.get("mint"),
                "source_sig": r.get("source_sig"),
                "source_slot": r.get("source_slot"),
                "our_slot": r.get("landed_slot") or r.get("our_slot"),
                "s_plus": r.get("s_plus"),
                "itog_po_cepi_sol": r.get("itog_po_cepi_sol")}
        наша = r.get("landed_sig") or r.get("buy_sig")
        # --- БЛОК ИСТОЧНИКА: его место в СВОЁМ блоке
        бс = блок(r.get("source_slot")) if r.get("source_slot") else {
            "ok": False, "why_not": "слота источника в выгрузке нет"}
        if бс.get("ok"):
            подписи = [x["podpis"] for x in бс["ryady"]]
            try:
                и = подписи.index(r["source_sig"])
            except ValueError:
                и = None
            стр["ist_index"] = и
            стр["ist_vsego"] = бс["vsego"]
            стр["ist_dolya"] = (round(и / бс["vsego"], 4)
                                 if и is not None and бс["vsego"] else None)
            if и is None:
                стр["ist_why_not"] = ("подписи источника в блоке его слота "
                                       "нет -- слот в записи не тот")
        else:
            стр["ist_why_not"] = бс.get("why_not")
        # --- НАШ БЛОК: место нашей и соседи по минту ПЕРЕД нами
        сл_наш = стр["our_slot"]
        бн = блок(сл_наш) if сл_наш else {
            "ok": False, "why_not": "слота нашей посадки в выгрузке нет"}
        if бн.get("ok") and наша:
            подписи = [x["podpis"] for x in бн["ryady"]]
            try:
                ин = подписи.index(наша)
            except ValueError:
                ин = None
            стр["nash_index"] = ин
            стр["nash_vsego"] = бн["vsego"]
            стр["nash_dolya"] = (round(ин / бн["vsego"], 4)
                                  if ин is not None and бн["vsego"] else None)
            if ин is None:
                стр["nash_why_not"] = "нашей подписи в блоке нашего слота нет"
            else:
                # СОСЕДИ ПО МИНТУ ПЕРЕД НАМИ. Признак -- сделка той же
                # программы кривой, у которой В СЧЕТАХ есть этот минт. Чужую
                # покупку от чужой продажи здесь не отличить (раскладка
                # "accounts" инструкций не отдаёт), поэтому поле названо
                # "сделок по минту", а не "покупок": врать числом хуже, чем
                # назвать его тем, что оно есть.
                минт = r.get("mint")
                прогр = r.get("pool_program") or СБ.ПРОГ_КРИВОЙ
                до, после = 0, 0
                for н, x in enumerate(бн["ryady"]):
                    if x["podpis"] == наша:
                        continue
                    сч = x["scheta"]
                    if минт in сч and прогр in сч:
                        if н < ин:
                            до += 1
                        else:
                            после += 1
                стр["po_mintu_do_nas"] = до
                стр["po_mintu_posle_nas"] = после
                стр["sosedi_vid"] = ("сделки той же программы кривой с этим "
                                      "минтом в счетах; покупка и продажа не "
                                      "различаются")
        elif not наша:
            стр["nash_why_not"] = "нашей подписи в выгрузке нет"
        else:
            стр["nash_why_not"] = бн.get("why_not")
        if стр.get("ist_index") is not None or стр.get("nash_index") is not None:
            из_["prochitano"] += 1
        else:
            из_["ne_prochitano"] += 1
        из_["stroki"].append(стр)
    return из_


def k_why(к: dict) -> str:
    """Причина, по которой в сделке не нашлось кривой. Короткой строкой."""
    return str((к or {}).get("why_not") or "хранилища базы в сделке нет")[:140]


def otkazy_cherez_denezhnyj_put(*, podpisi: list, koshelek: str,
                                bilet_lamportov: int, cu: int,
                                pauza: float) -> dict:
    """ОТКАЗАННЫЕ СИГНАЛЫ -- тем же денежным путём до симуляции.

    Подписи приходят снаружи (журнал решений полосы за сутки), выдумывать их
    здесь нечем. На каждой: шаблон -> build_buy -> подготовить -> узел.
    """
    из_ = {"podpisej": len(podpisi), "proshlo": 0, "krasnyh": 0,
           "krivaya_zakonchilas": 0, "kotirovka_ne_wsol": 0,
           "ne_razobrano": 0, "sdelki": [], "why_not": None}
    for п in podpisi:
        п = str(п).strip()
        if not п:
            continue
        т = зов("getTransaction", [п, {"encoding": "jsonParsed",
                                        "maxSupportedTransactionVersion": ПОТОЛОК_ВЕРСИИ_TX,
                                        "commitment": "confirmed"}])
        if not т["ok"] or not т.get("result"):
            из_["ne_razobrano"] += 1
            из_["sdelki"].append({"podpis": п, "ok": False,
                                   "why_not": f"транзакция не прочитана: "
                                              f"{т.get('why_not')}"})
            continue
        к = _kriwaja_iz_tx(т["result"])
        if not к["ok"]:
            из_["ne_razobrano"] += 1
            из_["sdelki"].append({"podpis": п, "ok": False,
                                   "why_not": к["why_not"]})
            continue
        р = krug_denezhnogo_puti(
            tx_istochnika=т["result"], mint=к["mint"],
            token_program=к["base_token_program"],
            bazovyj_vault=к["bazovyj_vault"], koshelek=koshelek,
            bilet_lamportov=bilet_lamportov, cu=cu, pauza=pauza)
        р["podpis"] = п
        р["pokupka_istochnika"] = к.get("pokupka_istochnika")
        из_["sdelki"].append(р)
        из_["proshlo"] += 1 if р["ok"] else 0
        if not р["ok"]:
            кл = р.get("klass_otkaza")
            if кл == "kotirovka_ne_wsol":
                из_["kotirovka_ne_wsol"] += 1
            elif р.get("raskladka_prinyata"):
                из_["krivaya_zakonchilas"] += 1
            else:
                из_["krasnyh"] += 1
        if pauza:
            time.sleep(pauza)
    if из_["krasnyh"]:
        из_["why_not"] = (f"{из_['krasnyh']} отказанных сигналов из "
                          f"{из_['podpisej']} красные на денежном пути")
    elif not из_["proshlo"]:
        из_["why_not"] = ("ни один сигнал не прошёл денежным путём: "
                          f"закончившихся кривых {из_['krivaya_zakonchilas']}, "
                          f"котировка не WSOL {из_['kotirovka_ne_wsol']}, "
                          f"не разобрано {из_['ne_razobrano']}")
    return из_


def podpisi_istochnika(*, istochnik: str, chasov: float, predel: int,
                       pauza: float) -> dict:
    """Покупки v3 ИСТОЧНИКА за окно -- с цепи, по его же подписям.

    ЗАЧЕМ НЕ ТОЛЬКО ЖУРНАЛ. Воронка полосы держит ПРИМЕРЫ отказов, а не все
    подписи: за сутки группа vol_4vw отказала 34 раза, а подписей в отчёте
    три. Население у двух списков одно и то же -- сигнал полосы это и есть
    покупка источника, -- и цепь отдаёт его целиком. Подписи из журнала при
    этом никуда не деваются: их можно подать режимом --podpisi.
    """
    из_ = {"istochnik": istochnik, "podpisi": [], "prosmotreno": 0,
           "v_okne": 0, "ne_prochitano": 0, "pusto": 0, "bez_v3": 0,
           "pervyj_otkaz_chteniya": None, "why_not": None}
    о = зов("getSignaturesForAddress", [istochnik, {"limit": int(predel)}])
    if not о["ok"]:
        из_["why_not"] = f"подписи источника не прочитаны: {о['why_not']}"
        return из_
    порог = None
    записи = о.get("result") or []
    if записи and chasov > 0:
        свежая = max((int(з.get("blockTime") or 0) for з in записи), default=0)
        порог = свежая - int(chasov * 3600) if свежая else None
    for з in записи:
        if з.get("err"):
            continue
        вр = int(з.get("blockTime") or 0)
        if порог is not None and вр and вр < порог:
            continue
        из_["v_okne"] += 1
        п = з.get("signature")
        if not п:
            continue
        из_["prosmotreno"] += 1
        if pauza:
            time.sleep(pauza)
        т = зов("getTransaction", [п, {"encoding": "jsonParsed",
                                        "maxSupportedTransactionVersion": ПОТОЛОК_ВЕРСИИ_TX,
                                        "commitment": "confirmed"}])
        if not т["ok"]:
            # ОТКАЗ УЗЛА НЕ ГЛОТАЕТСЯ. Пропуск молча превращал 468 отказов
            # -32015 в вывод "покупок v3 у источника нет": отличить "не
            # читается" от "её там нет" было НЕЧЕМ.
            из_["ne_prochitano"] += 1
            if из_["pervyj_otkaz_chteniya"] is None:
                из_["pervyj_otkaz_chteniya"] = str(т.get("why_not"))[:200]
            continue
        if not т.get("result"):
            из_["pusto"] += 1
            continue
        if _kriwaja_iz_tx(т["result"])["ok"]:
            из_["podpisi"].append(п)
        else:
            из_["bez_v3"] += 1
    if из_["ne_prochitano"]:
        из_["why_not"] = (f"узел не отдал {из_['ne_prochitano']} транзакций из "
                          f"{из_['prosmotreno']}: {из_['pervyj_otkaz_chteniya']}")
    elif not из_["podpisi"]:
        из_["why_not"] = (f"покупок {ПОКУПКА} у источника за окно нет "
                          f"(прочитано {из_['prosmotreno']}, без v3 "
                          f"{из_['bez_v3']})")
    return из_


def живой(*, skolko: int, koshelek: str, bilet_lamportov: int, cu: int,
          pauza: float) -> dict:
    из_ = {"rezhim": "zhivoj", "uzel": затереть(узел()), "koshelek": koshelek,
            "bilet_lamportov": bilet_lamportov, "minty": [], "proshlo": 0,
            "krasnyh": 0, "denezhnyj_put": [], "proshlo_puti": 0,
            "krasnyh_puti": 0, "why_not": None}
    сбор = minty_token2022_s_cepi(skolko=skolko, pauza=pauza)
    из_["prosmotreno_tx"] = сбор["prosmotreno"]
    из_["buyback_iz_obrazca"] = сбор["buyback"]
    if сбор["why_not"]:
        из_["why_not"] = сбор["why_not"]
        return из_
    for м in сбор["minty"]:
        р = krug_na_minte(mint=м["mint"], token_program=м["base_token_program"],
                           buyback=сбор["buyback"], koshelek=koshelek,
                           bilet_lamportov=bilet_lamportov, cu=cu, pauza=pauza)
        р["iz_podpisi"] = м["iz_podpisi"]
        из_["minty"].append(р)
        из_["proshlo"] += 1 if р["ok"] else 0
        из_["krasnyh"] += 0 if р["ok"] else 1
        if pauza:
            time.sleep(pauza)
        # ТОТ ЖЕ МИНТ -- ЧЕРЕЗ ФУНКЦИИ ПОЛОСЫ. Два круга на минт, а не два
        # прогона: иначе сравнивать было бы нечего, минты у них разошлись бы.
        д = krug_denezhnogo_puti(
            tx_istochnika=м.get("tx") or {}, mint=м["mint"],
            token_program=м["base_token_program"],
            bazovyj_vault=м.get("bazovyj_vault"), koshelek=koshelek,
            bilet_lamportov=bilet_lamportov, cu=cu, pauza=pauza)
        д["iz_podpisi"] = м["iz_podpisi"]
        из_["denezhnyj_put"].append(д)
        из_["proshlo_puti"] += 1 if д["ok"] else 0
        из_["krasnyh_puti"] += 0 if д["ok"] else 1
        if pauza:
            time.sleep(pauza)
    if len(из_["minty"]) < 3:
        из_["why_not"] = (f"минтов всего {len(из_['minty'])}, а нужно 3-5 -- "
                          "проверка не состоялась")
    elif из_["krasnyh"]:
        из_["why_not"] = f"{из_['krasnyh']} минтов из {len(из_['minty'])} красные"
    elif из_["krasnyh_puti"]:
        из_["why_not"] = (f"{из_['krasnyh_puti']} минтов из "
                          f"{len(из_['denezhnyj_put'])} красные на ДЕНЕЖНОМ ПУТИ "
                          "(сборщик при этом зелёный -- значит дело во врезке)")
    return из_


def s_tx_version_vezde() -> bool:
    """Ни одного getTransaction с потолком версии мимо константы.

    Проверка ЧИТАЕТ СВОЙ ЖЕ ФАЙЛ: три вызова узла легко разъехаться, а
    разъехавшийся тихо даст "транзакций нет" вместо отказа.
    """
    текст = Path(__file__).read_text(encoding="utf-8")
    всего = текст.count("maxSupportedTransactionVersion")
    по_константе = текст.count(
        '"maxSupportedTransactionVersion": ПОТОЛОК_ВЕРСИИ_TX')
    return всего >= 3 and всего == по_константе + 1   # +1 -- строка в этой проверке


def самопроверка() -> int:
    сбоев = всего = 0

    def chk(имя: str, ок: bool, что=None) -> None:
        nonlocal сбоев, всего
        всего += 1
        print(f"  [{'ok  ' if ок else 'СБОЙ'}] {имя}")
        if not ок:
            сбоев += 1
            if что is not None:
                print(f"         {что!r}")

    chk("потолок версии транзакции -- как у службы (BLOOM_MAX_TX_VERSION=1), "
        "а не ноль: на нуле узел отказал 468 раз подряд",
        ПОТОЛОК_ВЕРСИИ_TX == 1 and s_tx_version_vezde(), ПОТОЛОК_ВЕРСИИ_TX)
    _лог = ["Program log: Instruction: BuyExactQuoteInV3",
            "Program log: AnchorError thrown in programs/pump/src/trade_v3.rs:148. "
            "Error Code: BondingCurveComplete. Error Number: 6005. Error Message: "
            "The bonding curve has completed and liquidity migrated to raydium.",
            "Program 6EF8 failed: custom program error: 0x1775"]
    chk("6005 -- это «кривая закончилась», а не отказ по раскладке",
        klass_otkaza(_лог)["klass"] == "krivaya_zakonchilas"
        and klass_otkaza(_лог)["kod"] == "6005", klass_otkaza(_лог))
    chk("6004 -- тоже «кривая закончилась»: счёта кривой этого минта нет",
        klass_otkaza(["Error Code: MintDoesNotMatchBondingCurve. "
                      "Error Number: 6004. Error Message: x"])["klass"]
        == "krivaya_zakonchilas")
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: 3012 AccountNotInitialized остаётся отказом ПО "
        "РАСКЛАДКЕ -- иначе разбор прятал бы ровно ту поломку, из-за которой "
        "полоса встала",
        klass_otkaza(["Error Code: AccountNotInitialized. Error Number: 3012. "
                      "Error Message: x"])["klass"] == "raskladka")
    chk("наш названный отказ по котировке отделён от красного раскладки: "
        "«не WSOL» в тексте сборки -- это свой отказ до подписи",
        "не WSOL" in "котировка кривой CARDS не WSOL"
        and 'klass_otkaza="kotirovka_ne_wsol"' in
        Path(__file__).read_text(encoding="utf-8"))
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: без номера ошибки класс остаётся «раскладка», а "
        "не «закончилась» -- молчание не оправдание",
        klass_otkaza(["Program failed"])["klass"] == "raskladka"
        and klass_otkaza([])["kod"] is None)
    chk("смещения счёта кривой взяты из раскладки IDL, а не на глаз",
        СМЕЩЕНИЕ_COMPLETE == 8 + 5 * 8
        and СМЕЩЕНИЕ_CREATOR == СМЕЩЕНИЕ_COMPLETE + 1
        and СМЕЩЕНИЕ_MAYHEM == СМЕЩЕНИЕ_CREATOR + 32
        and СМЕЩЕНИЕ_QUOTE_MINT == СМЕЩЕНИЕ_MAYHEM + 2
        and ДЛИНА_СЧЁТА_КРИВОЙ == СМЕЩЕНИЕ_QUOTE_MINT + 32 + 8 + 1 + 1 + 8
        + 8 + 1 + 8 + 8 + 8)
    chk("ключ узла в выводе затирается",
        "СЕК" not in затереть("https://x/?api-key=СЕК"))
    # ОСТАТОК ТОКЕНА: amount -- u64 с 64-го байта счёта SPL.
    сырые = bytes(64) + (12345).to_bytes(8, "little") + bytes(93)
    chk("остаток токена читается из счёта SPL (u64 с 64-го байта)",
        ostatok_tokena([{"data": [base64.b64encode(сырые).decode(), "base64"]}])
        == 12345)
    chk("нет данных счёта -- None, а не ноль: «не прочитали» и «купили ноль» "
        "это разное",
        ostatok_tokena([]) is None and ostatok_tokena([None]) is None
        and ostatok_tokena([{"data": [base64.b64encode(bytes(8)).decode(),
                                       "base64"]}]) is None)
    chk("короткий счёт -- None, а не мусор",
        ostatok_tokena([{"data": [base64.b64encode(bytes(70)).decode(),
                                   "base64"]}]) is None)
    ата = ata_instrukciya(payer="A" * 32, owner="A" * 32, mint="B" * 32,
                           token_program=СБ.ПРОГ_ТОКЕНА_2022)
    chk("инструкция ATA: один байт данных (CreateIdempotent) и шесть счетов, "
        "программа токена последняя -- как в денежном пути",
        ата["dannye"] == bytes([1]) and len(ата["scheta"]) == 6
        and ата["scheta"][5]["pubkey"] == СБ.ПРОГ_ТОКЕНА_2022, ата)
    chk("предел CU -- инструкция ComputeBudget с кодом 2 и числом",
        predel_cu(400_000)["dannye"][:1] == bytes([2])
        and struct.unpack("<I", predel_cu(400_000)["dannye"][1:5])[0] == 400_000)
    chk("два предела CU в одной транзакции отбрасываются -- иначе узел "
        "откажет DuplicateInstruction",
        len(_bez_budzheta([predel_cu(1), ата, predel_cu(2)])) == 1)
    try:
        # АДРЕСА ЗДЕСЬ -- НАСТОЯЩИЕ: solders не примет выдуманную строку, а
        # «A»*32 это не base58-ключ. Минт берётся WSOL -- он есть всегда.
        _кош = КОШЕЛЁК_ПОЛОСЫ
        ата = ata_instrukciya(payer=_кош, owner=_кош,
                               mint="So11111111111111111111111111111111111111112",
                               token_program=СБ.ПРОГ_ТОКЕНА_2022)
        _т64 = tx_base64(instrukcii=[predel_cu(321_000), ата],
                          platelshchik=_кош)
        _назад = _instrukcii_iz_tx64(_т64)
        chk("инструкции разбираются обратно из собранной транзакции: "
            "программы, байты и счета те же",
            [и["programma"] for и in _назад]
            == ["ComputeBudget111111111111111111111111111111", СБ.ПРОГ_ATA]
            and _назад[0]["dannye"] == predel_cu(321_000)["dannye"]
            and [а["pubkey"] for а in _назад[1]["scheta"]]
            == [а["pubkey"] for а in ата["scheta"]], _назад)
        chk("признаки подписи и записи переносятся: плательщик подписывает, "
            "программа токена -- нет",
            _назад[1]["scheta"][0]["isSigner"] is True
            and _назад[1]["scheta"][5]["isSigner"] is False, _назад[1]["scheta"])
        chk("ДОКАЗАННЫЙ КРАСНЫЙ: в транзакции без покупки кривой разбор "
            "отказывает по имени, а не возвращает пустой минт",
            _kriwaja_iz_tx({"transaction": {"message": {
                "accountKeys": [], "instructions": []}}}).get("why_not")
            == (f"покупки кривой {list(ПОКУПКИ_ИСТОЧНИКА)} в транзакции "
                 "не найдено"))
        chk("ищутся ОБА v3: имена счетов у них совпадают, поэтому минт "
            "снимается с любого",
            ПОКУПКИ_ИСТОЧНИКА == ("buy_exact_quote_in_v3", "buy_v3")
            and [а["name"] for а in
                 СБ.zagruzit_idl()["pump"]["ix"]["buy_v3"]["accounts"]]
            == [а["name"] for а in
                СБ.zagruzit_idl()["pump"]["ix"]["buy_exact_quote_in_v3"]
                ["accounts"]])
        chk("склейка покупки и продажи даёт транзакцию без второго предела CU",
            sum(1 for и in (_назад + _bez_budzheta(_назад))
                if и["programma"].startswith("ComputeBudget")) == 1)
    except ImportError:
        chk("solders на машине нет -- склейка не проверялась (это пропуск, "
            "а не зелёный)", False)
    # --- ЗАМЕР ВРЕМЕНИ РАЗБОРА (вопрос владельца 10.10, п.2в) ---
    # МЕДИАНА И ПРОЦЕНТИЛЬ СВЕРЯЮТСЯ НА ЧИСЛАХ, ОТВЕТ У КОТОРЫХ ИЗВЕСТЕН
    # НАПЕРЁД, а не сами с собой: чётная и нечётная длина, p99 по короткой
    # выборке -- это НАИБОЛЬШЕЕ (ранг без интерполяции), p50 на нечётной --
    # серёдка. Пустой список -- None, а не ноль: "нечего мерить" и "ноль
    # миллисекунд" читаются по-разному.
    chk("медиана: нечётная длина -- серёдка, чётная -- полусумма, пусто -- None",
        _mediana([3, 1, 2]) == 2
        and _mediana([4, 1, 2, 3]) == 2.5
        and _mediana([]) is None
        and _mediana(["нечисло", None]) is None)
    chk("процентиль по ближайшему рангу: p99 короткой выборки -- наибольшее, "
        "p50 -- серёдка, p1 -- наименьшее",
        _procentil([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 99) == 10
        and _procentil([1, 2, 3], 50) == 2
        and _procentil([5, 1, 9], 1) == 1
        and _procentil([], 99) is None)
    # ЗАМЕР ПРОВЕДЁН ПО ОБЕИМ ВЕРСИЯМ И НА ОДНОЙ И ТОЙ ЖЕ ТРАНЗАКЦИИ.
    # Текстом -- потому что без узла настоящий прогон здесь не сделать, а
    # оборвать проводку (замерить одну версию или замерить до разбора) можно
    # было бы незаметно.
    _ист_ср = inspect.getsource(sravnit_versii_razbora)
    chk("время мерится у ОБЕИХ версий тем же вызовом и на той же транзакции, "
        "и разница считается как новая минус старая",
        'ряд[f"ms_{имя}"]' in _ист_ср
        and "ПОВТОРОВ_ЗАМЕРА" in _ист_ср
        and 'ряд["ms_novaya"] - ряд["ms_staraya"]' in _ист_ср
        and '"raznica_ms_mediana"' in _ист_ср)
    # --- MAYHEM ПО СЧЁТУ КРИВОЙ (доп. к п.2д, владелец 10.10 вечером) ---
    # СМЕЩЕНИЯ СЛОЖЕНЫ ПО IDL, И ЭТО ПРОВЕРЯЕТСЯ ЧИСЛАМИ. Ошибка на один байт
    # читается как "mayhem там, где его нет", то есть как настоящий ответ, --
    # поэтому проверяются и само смещение, и порядок соседей, и полный размер
    # счёта. Числа взяты из живого IDL кривой (19 полей), а не из головы.
    _см = smeshchenija_krivoj()
    chk(f"смещения счёта кривой из IDL: is_mayhem_mode {_см['polya'].get('is_mayhem_mode')}, "
        f"quote_mint {_см['polya'].get('quote_mint')}, размер {_см['razmer']}",
        _см.get("why_not") is None
        and _см["polya"].get("complete") == (48, "bool")
        and _см["polya"].get("creator") == (49, "pubkey")
        and _см["polya"].get("is_mayhem_mode") == (81, "bool")
        and _см["polya"].get("is_cashback_coin") == (82, "bool")
        and _см["polya"].get("quote_mint") == (83, "pubkey")
        and _см["razmer"] == 166
        and len(_см["polya"]) == 19, _см.get("why_not"))
    # НЕИЗВЕСТНЫЙ ТИП ОБРЫВАЕТ СЧЁТ, А НЕ ПРОПУСКАЕТСЯ: пропуск сдвинул бы
    # все следующие поля и прочитал бы mayhem не там.
    _был_размер = dict(РАЗМЕРЫ_BORSH)
    try:
        РАЗМЕРЫ_BORSH.pop("pubkey", None)
        _обрыв = smeshchenija_krivoj()
        chk("неизвестный тип поля обрывает счёт смещений с названной причиной, "
            "а не пропускается молча",
            _обрыв.get("why_not") is not None
            and "creator" in str(_обрыв["why_not"])
            and "is_mayhem_mode" not in _обрыв["polya"])
    finally:
        РАЗМЕРЫ_BORSH.clear()
        РАЗМЕРЫ_BORSH.update(_был_размер)
    # НУЛЕВОЙ PUBKEY В КОТИРОВКЕ -- НАТИВНЫЙ SOL. Числа проверяются на том
    # самом случае, который меня и поймал 10.10: у всех живых кривых 4vw
    # quote_mint пуст, и прежний счёт сказал бы "котировка не SOL у 67 из 67".
    _ист_м = inspect.getsource(mayhem_mintov)
    chk("пустой pubkey котировки не считается чужой котировкой, и у него своё "
        "имя в ряду",
        'ПУСТОЙ_PUBKEY' in _ист_м
        and '_кот not in (WSOL, ПУСТОЙ_PUBKEY)' in _ист_м
        and 'ряд["kotirovka"]' in _ист_м
        and ПУСТОЙ_PUBKEY == "11111111111111111111111111111111")
    # --- МЕСТА В БЛОКЕ (вопрос владельца 10.10, п.1) ---
    # ПОРЯДОК БЛОКА И РАСКЛАДКА ЗАПРОСА. Индекс в блоке имеет смысл только
    # если мы читаем сделки В ПОРЯДКЕ блока и не теряем их: поэтому проверяется
    # и разбор ответа (подписи и счета по порядку), и то, что запрос просит
    # ИМЕННО accounts -- с "signatures" счетов не будет и соседей по минту не
    # посчитать, а с "full" прогон потащил бы сотни мегабайт.
    _ист_мб = inspect.getsource(_blok_po_schetam)
    chk('блок запрашивается раскладкой "accounts", без наград и с потолком '
        'версии транзакции',
        '"transactionDetails": "accounts"' in _ист_мб
        and '"rewards": False' in _ист_мб
        and 'ПОТОЛОК_ВЕРСИИ_TX' in _ист_мб)
    _ист_мв = inspect.getsource(mesta_v_bloke)
    chk("соседи по минту считаются по ИНДЕКСУ (до нас / после нас), а не по "
        "всему блоку, и своя подпись из счёта исключена",
        'if н < ин:' in _ист_мв
        and 'if x["podpis"] == наша:' in _ист_мв
        and 'continue' in _ист_мв
        and '"po_mintu_do_nas"' in _ист_мв)
    # ПОДПИСИ НЕТ В БЛОКЕ -- ЭТО НАЗВАННЫЙ ОТКАЗ, А НЕ ИНДЕКС None МОЛЧА:
    # иначе "места нет" и "место нулевое" читались бы одинаково.
    chk("подписи нет в блоке -- причина словами, а не пустой индекс молча",
        'подписи источника в блоке его слота' in _ист_мв
        and 'нашей подписи в блоке нашего слота нет' in _ист_мв)
    print(f"самопроверка круга сборки: {всего - сбоев}/{всего} пройдено")
    return 1 if сбоев else 0


def main() -> int:
    п = argparse.ArgumentParser()
    п.add_argument("--self-test", action="store_true")
    п.add_argument("--live", action="store_true")
    п.add_argument("--skolko", type=int, default=5, help="минтов (3-5)")
    п.add_argument("--koshelek", default=КОШЕЛЁК_ПОЛОСЫ)
    п.add_argument("--bilet-lamportov", type=int, default=10_000_000,
                    help="трата покупки в лампортах (0.01 SOL по умолчанию)")
    п.add_argument("--cu", type=int, default=600_000)
    п.add_argument("--pauza", type=float, default=0.12)
    п.add_argument("--istochnik", default="",
                    help="адрес источника: его покупки v3 за окно берутся с "
                         "цепи и прогоняются денежным путём")
    п.add_argument("--chasov", type=float, default=24.0)
    п.add_argument("--predel-podpisej", type=int, default=1000)
    п.add_argument("--podpisi", default="",
                    help="подписи отказанных сигналов через запятую: каждая "
                         "прогоняется денежным путём до симуляции")
    п.add_argument("--chitat", default="",
                    help="ТОЛЬКО ЧТЕНИЕ: подписи наших транзакций -- чем они "
                         "собраны (имя разновидности по IDL), без симуляции")
    п.add_argument("--sravnit-versii", default="",
                    help="подписи сигналов: прогнать разбор ДВУМЯ версиями "
                         "(нынешней и из --ref-staryj) и назвать расхождения")
    п.add_argument("--ref-staryj", default="e222b934^",
                    help="коммит старой версии c2_swap_build для сравнения")
    п.add_argument("--mesta-v-bloke", default="",
                    help="ТОЛЬКО ЧТЕНИЕ: путь к выгрузке сделок -- места в "
                         "блоках источника и наших по архивному узлу")
    п.add_argument("--mayhem", default="",
                    help="ТОЛЬКО ЧТЕНИЕ: минты через запятую -- is_mayhem_mode "
                         "и котировка по СЧЁТУ КРИВОЙ (смещения из IDL)")
    п.add_argument("--out", default="")
    а = п.parse_args()
    if а.self_test:
        return самопроверка()
    # ПОДПИСИ И ИСТОЧНИК -- ЭТО СВОЙ ЖИВОЙ РЕЖИМ, и он НЕ требует --live:
    # --live значит "найди минты сам", а здесь минты названы снаружи. Проверка
    # на --live стояла выше этой ветки и съела весь прогон 37957981526: код 2
    # "нужен --live", ноль вызовов узла, а прогон при этом зелёный.
    if а.sravnit_versii.strip():
        из_ = sravnit_versii_razbora(
            podpisi=[x for x in а.sravnit_versii.replace(";", ",").split(",")
                      if x.strip()],
            ref_staryj=а.ref_staryj, pauza=а.pauza)
        из_["uzel"] = затереть(узел())
        текст = json.dumps(из_, ensure_ascii=False, indent=1)
        print(текст)
        if а.out:
            Path(а.out).write_text(текст + "\n", encoding="utf-8")
        # РАСХОЖДЕНИЕ -- НЕ СБОЙ ПРОГОНА: его и искали. Код 0, чтобы отчёт
        # коммитился, а судить по числам.
        return 0
    if а.mesta_v_bloke.strip():
        из_ = mesta_v_bloke(vygruzka=а.mesta_v_bloke.strip(), pauza=а.pauza)
        из_["uzel"] = затереть(узел())
        текст = json.dumps(из_, ensure_ascii=False, indent=1)
        print(текст)
        if а.out:
            Path(а.out).write_text(текст + "\n", encoding="utf-8")
        return 0
    if а.mayhem.strip():
        из_ = mayhem_mintov(
            minty=[x for x in а.mayhem.replace(";", ",").split(",")
                    if x.strip()],
            pauza=а.pauza)
        из_["uzel"] = затереть(узел())
        текст = json.dumps(из_, ensure_ascii=False, indent=1)
        print(текст)
        if а.out:
            Path(а.out).write_text(текст + "\n", encoding="utf-8")
        # НЕПРОЧИТАННЫЕ -- НЕ СБОЙ ПРОГОНА: отчёт нужен и с частью счетов,
        # а судить по числам prochitano / ne_prochitano.
        return 0
    if а.chitat.strip():
        из_ = chem_kupleno(
            [x for x in а.chitat.replace(";", ",").split(",") if x.strip()],
            pauza=а.pauza)
        из_["uzel"] = затереть(узел())
        текст = json.dumps(из_, ensure_ascii=False, indent=1)
        print(текст)
        if а.out:
            Path(а.out).write_text(текст + "\n", encoding="utf-8")
        return 0 if not из_["why_not"] else 1
    спис = [x for x in а.podpisi.replace(";", ",").split(",") if x.strip()]
    сбор_ист = None
    if а.istochnik.strip():
        сбор_ист = podpisi_istochnika(istochnik=а.istochnik.strip(),
                                      chasov=а.chasov,
                                      predel=а.predel_podpisej, pauza=а.pauza)
        for п_ in сбор_ист["podpisi"]:
            if п_ not in спис:
                спис.append(п_)
    if сбор_ист is not None and сбор_ист.get("why_not") and not спис:
        текст = json.dumps({"sbor_istochnika": сбор_ист}, ensure_ascii=False,
                           indent=1)
        print(текст)
        if а.out:
            Path(а.out).write_text(текст + "\n", encoding="utf-8")
        return 1
    if спис:
        из_ = otkazy_cherez_denezhnyj_put(
            podpisi=спис,
            koshelek=а.koshelek, bilet_lamportov=а.bilet_lamportov,
            cu=а.cu, pauza=а.pauza)
        из_["uzel"] = затереть(узел())
        if сбор_ист is not None:
            из_["sbor_istochnika"] = {k: v for k, v in сбор_ист.items()
                                      if k != "podpisi"}
            из_["sbor_istochnika"]["podpisej_najdeno"] = len(сбор_ист["podpisi"])
        текст = json.dumps(из_, ensure_ascii=False, indent=1)
        print(текст)
        if а.out:
            Path(а.out).write_text(текст + "\n", encoding="utf-8")
        return 0 if (из_["proshlo"] and not из_["why_not"]) else 1
    if not а.live:
        print("СТОП: нужен --live, --self-test, --podpisi или --istochnik",
              file=sys.stderr)
        return 2
    из_ = живой(skolko=а.skolko, koshelek=а.koshelek,
                 bilet_lamportov=а.bilet_lamportov, cu=а.cu, pauza=а.pauza)
    текст = json.dumps(из_, ensure_ascii=False, indent=1)
    print(текст)
    if а.out:
        Path(а.out).write_text(текст + "\n", encoding="utf-8")
    return 0 if (из_["proshlo"] and из_["proshlo_puti"]
                 and not из_["why_not"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
