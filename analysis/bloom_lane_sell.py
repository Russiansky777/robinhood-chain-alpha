#!/usr/bin/env python3
"""Своя продажа одной ногой: Pump AMM, собранная ИЗ НАШЕЙ ЖЕ покупки.

ПОРЯДОК ВЛАДЕЛЬЦА (28.09): "сборка из нашей покупки -> simulateTransaction на
хосте -> одна живая продажа 0.01 -> все группы. Jupiter -- запасной, если свой
не сел за 2 с". Здесь первый шаг: сборка и минимум выхода.

ПОЧЕМУ ИЗ НАШЕЙ ПОКУПКИ, А НЕ ПО РАСКЛАДКЕ. Раскладка продажи Pump AMM
измерена на живых сделках (docs/prodazha_pump_amm_raskladka.md), но ровно
четыре места из 24 на двух пулах разошлись: 9 и 10 -- получатель протокольной
комиссии и его токен-счёт (программа разрешает любого из списка), 22 и 23 --
непонятно, и двух образцов мало. Зато измерено другое, твёрдо: у пула
EoYjH7Ymm5Xx продажа оказалась ПОКУПКОЙ БЕЗ ДВУХ СЧЁТОВ -- индексов 19 и 20
(global_volume_accumulator и user_volume_accumulator), все 24 совпали по
порядку. Значит счета берём из НАШЕЙ покупки того же пула: они наши, они
законные для этого пула, и спорные четыре места не нужно угадывать вовсе.

МИНИМУМ ВЫХОДА -- ПО ЖИВЫМ РЕЗЕРВАМ И С ЗАПАСОМ. Ставка комиссии Pump AMM по
цепи не вывелась (прогон 28.09: решения от 1 % до 18 %, ни одного смещения в
полях пула и конфига), поэтому минимум считается как выход по кривой без
комиссии минус запас, покрывающий комиссию и скольжение. Запас -- это ПОЛ, а не
цена: если настоящая комиссия окажется больше запаса, транзакция откажет, и
продаст Jupiter. Ноль вместо минимума не ставим: своя продажа идёт одной
инструкцией, и защищать выход больше нечем.

ОБЁРТКА SOL. Котировка Pump AMM -- WSOL, и продажа отдаёт её на наш токеновый
счёт. Поэтому в конце транзакции стоит closeAccount нашего счёта WSOL: он
переводит и выручку, и ренту счёта в наш кошелёк нативным SOL. Без него деньги
остались бы на токеновом счёте, а баланс кошелька не изменился.
"""
from __future__ import annotations

import base64
import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import c2_swap_build as SB  # noqa: E402

from solders.hash import Hash  # noqa: E402
from solders.instruction import AccountMeta, Instruction  # noqa: E402
from solders.message import MessageV0  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.signature import Signature  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

# Индексы счетов, которые есть у ПОКУПКИ Pump AMM и которых нет у продажи.
# Измерено на паре покупка/продажа одного пула: 26 - 2 = 24.
ИНДЕКСЫ_ТОЛЬКО_ПОКУПКИ = (19, 20)
СЧЕТОВ_ПРОДАЖИ = 24
ЗАПАС_ВЫХОДА_ПО_УМОЛЧАНИЮ = 0.25
ЗАКРЫТЬ_СЧЁТ = 9          # SPL Token: CloseAccount


def запас_выхода() -> float:
    """Запас минимума выхода долей единицы. 0.25 -- четверть выхода по кривой."""
    try:
        зн = float(os.environ.get("BLOOM_SELL_MIN_OUT_ZAPAS") or
                    ЗАПАС_ВЫХОДА_ПО_УМОЛЧАНИЮ)
    except (TypeError, ValueError):
        return ЗАПАС_ВЫХОДА_ПО_УМОЛЧАНИЮ
    if not 0.0 <= зн < 1.0:
        return ЗАПАС_ВЫХОДА_ПО_УМОЛЧАНИЮ
    return зн


def close_account(счёт: str, куда: str, распорядитель: str) -> Instruction:
    """CloseAccount: у счёта WSOL это и есть разворачивание в нативный SOL."""
    return Instruction(Pubkey.from_string(SB.TOKEN_PROGRAM), bytes([ЗАКРЫТЬ_СЧЁТ]), [
        AccountMeta(Pubkey.from_string(счёт), False, True),
        AccountMeta(Pubkey.from_string(куда), False, True),
        AccountMeta(Pubkey.from_string(распорядитель), True, False)])


def инструкция_покупки(tx: dict, *, программа: str = SB.PUMP_AMM,
                        хранилище: str | None = None) -> dict:
    """Наша инструкция покупки в нашей же транзакции: счета и данные."""
    из_ = {"ok": False, "why_not": None}
    for ix in SB.all_instructions(tx):
        if ix.get("programId") != программа:
            continue
        if хранилище and хранилище not in ix["accounts"]:
            continue
        данные = SB.b58decode(ix["data"])
        # ПРАВО ЗАПИСИ -- И ПО СТАТИЧЕСКИМ КЛЮЧАМ, И ПО АДРЕСАМ ИЗ ТАБЛИЦ. В
        # ответе узла (encoding=json) message.accountKeys несёт только
        # статические ключи, а адреса из таблиц лежат в meta.loadedAddresses.
        # Наши покупки собираются без таблиц, но если покупка окажется с
        # таблицей, счёт молча стал бы "только для чтения", и программа
        # отказала бы уже на живой продаже.
        права = dict(SB.writable_map(tx))
        загруж = ((tx or {}).get("meta") or {}).get("loadedAddresses") or {}
        for а_ in (загруж.get("writable") or []):
            права[а_] = True
        for а_ in (загруж.get("readonly") or []):
            права.setdefault(а_, False)
        return {"ok": True, "program": программа, "accounts": list(ix["accounts"]),
                 "data": данные, "disc": данные[:8].hex(),
                 "writable": права,
                 "из_таблиц": len(загруж.get("writable") or [])
                               + len(загруж.get("readonly") or []),
                 "why_not": None}
    из_["why_not"] = (f"инструкции программы {программа[:8]} в нашей покупке нет"
                       if not хранилище else
                       f"инструкции с хранилищем {хранилище[:8]} в покупке нет")
    return из_


def шаблон_продажи_из_покупки(tx_покупки: dict, *,
                               программа: str = SB.PUMP_AMM,
                               хранилище: str | None = None) -> dict:
    """Продажа = покупка без индексов 19 и 20. Ничего больше не меняем."""
    пок = инструкция_покупки(tx_покупки, программа=программа, хранилище=хранилище)
    if not пок["ok"]:
        return пок
    счета = пок["accounts"]
    if len(счета) <= max(ИНДЕКСЫ_ТОЛЬКО_ПОКУПКИ):
        return {"ok": False,
                 "why_not": (f"в покупке {len(счета)} счетов -- индексы "
                              f"{ИНДЕКСЫ_ТОЛЬКО_ПОКУПКИ} из неё не выбросить")}
    продажа = [а for и, а in enumerate(счета)
                if и not in ИНДЕКСЫ_ТОЛЬКО_ПОКУПКИ]
    if len(продажа) != СЧЕТОВ_ПРОДАЖИ:
        return {"ok": False,
                 "why_not": (f"после выбрасывания вышло {len(продажа)} счетов, "
                              f"а у продажи их {СЧЕТОВ_ПРОДАЖИ}")}
    return {"ok": True, "program": программа, "accounts": продажа,
             "writable": пок["writable"], "disc_покупки": пок["disc"],
             "счетов_покупки": len(счета),
             "из_таблиц": пок.get("из_таблиц") or 0, "why_not": None}


def инструкция_продажи(шаблон: dict, *, наш_кошелёк: str, база_в: int,
                        минимум_выхода: int) -> Instruction:
    """sell(base_amount_in, min_quote_amount_out) -- дискриминатор измерен."""
    metas = []
    for а in шаблон["accounts"]:
        metas.append(AccountMeta(Pubkey.from_string(а),
                                  is_signer=(а == наш_кошелёк),
                                  is_writable=bool(шаблон["writable"].get(а, False))))
    данные = SB.disc("sell") + struct.pack("<QQ", int(база_в), int(минимум_выхода))
    return Instruction(Pubkey.from_string(шаблон["program"]), данные, metas)


def минимум_по_живым_резервам(*, база_в: int, резерв_базы: int,
                               резерв_котировки: int,
                               запас: float | None = None) -> dict:
    """Пол выхода: кривая без комиссии минус запас. Числа целые, вниз."""
    зп = запас_выхода() if запас is None else float(запас)
    if база_в <= 0 or резерв_базы <= 0 or резерв_котировки <= 0:
        return {"ok": False, "min_out": None,
                 "why_not": "нет живых резервов или количества к продаже"}
    по_кривой = (int(резерв_котировки) * int(база_в)) // (int(резерв_базы) + int(база_в))
    пол = int(по_кривой * (1.0 - зп))
    return {"ok": пол > 0, "min_out": max(0, пол), "po_krivoj": по_кривой,
             "zapas": зп,
             "why_not": None if пол > 0 else "пол выхода вышел нулём"}


def собрать_продажу(*, шаблон: dict, наш_кошелёк: str, база_в: int,
                     минимум_выхода: int, минт_котировки: str,
                     cu_units: int = 170_000, cu_price_micro: int = 0,
                     закрыть_котировку: bool = True) -> dict:
    """Готовая неподписанная транзакция продажи: бюджет, sell, закрытие WSOL."""
    if not шаблон.get("ok"):
        return {"ok": False, "why_not": шаблон.get("why_not") or "шаблона нет"}
    if int(база_в) <= 0:
        return {"ok": False, "why_not": "продавать нечего: количество не больше нуля"}
    ixs = [SB.cu_limit(int(cu_units))]
    if cu_price_micro:
        ixs.append(SB.cu_price(int(cu_price_micro)))
    ixs.append(инструкция_продажи(шаблон, наш_кошелёк=наш_кошелёк,
                                   база_в=база_в, минимум_выхода=минимум_выхода))
    закрыт = None
    if закрыть_котировку and минт_котировки == C.WSOL:
        закрыт = SB.ata(наш_кошелёк, C.WSOL, SB.TOKEN_PROGRAM)
        ixs.append(close_account(закрыт, наш_кошелёк, наш_кошелёк))
    msg = MessageV0.try_compile(Pubkey.from_string(наш_кошелёк), ixs, [],
                                 Hash.default())
    n = msg.header.num_required_signatures
    vtx = VersionedTransaction.populate(msg, [Signature.default()] * n)
    сырое = bytes(vtx)
    return {"ok": True, "why_not": None,
             "tx_base64": base64.b64encode(сырое).decode(), "size": len(сырое),
             "n_instructions": len(ixs), "подписей": n,
             "закрыт_счёт_wsol": закрыт,
             "база_в": int(база_в), "минимум_выхода": int(минимум_выхода)}


# ------------------------------------------------- прогон: сборка и симуляция

def _зов(метод: str, параметры, *, урл: str, таймаут: float = 60.0) -> dict:
    import json as _js  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    тело = _js.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                       "params": параметры}).encode()
    try:
        зпр = urllib.request.Request(
            урл, data=тело, headers={"content-type": "application/json"})
        with urllib.request.urlopen(зпр, timeout=таймаут) as отв:  # noqa: S310
            д = _js.loads(отв.read())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why_not": f"{type(exc).__name__}: {str(exc)[:200]}"}
    if "error" in д:
        return {"ok": False, "why_not": str(д["error"])[:300]}
    return {"ok": True, "result": д.get("result"), "why_not": None}


def остаток_счёта(счёт: str, *, урл: str) -> int | None:
    о = _зов("getTokenAccountBalance", [счёт, {"commitment": "processed"}], урл=урл)
    if not о["ok"]:
        return None
    зн = ((о["result"] or {}).get("value") or {}).get("amount")
    try:
        return int(зн)
    except (TypeError, ValueError):
        return None


def собрать_по_нашей_покупке(*, подпись: str, урл: str, наш_кошелёк: str,
                              количество: int | None = None,
                              доля: float = 1.0, запас: float | None = None,
                              cu_units: int = 170_000,
                              cu_price_micro: int = 0) -> dict:
    """Всё вместе: покупка из цепи -> шаблон -> живые резервы -> сборка."""
    из_: dict = {"ok": False, "why_not": None, "подпись_покупки": подпись}
    о = _зов("getTransaction",
              [подпись, {"encoding": "json", "maxSupportedTransactionVersion": 0}],
              урл=урл)
    if not о["ok"] or not о["result"]:
        из_["why_not"] = о.get("why_not") or "покупки по этой подписи в цепи нет"
        return из_
    tx = о["result"]
    шб = шаблон_продажи_из_покупки(tx)
    if not шб["ok"]:
        из_["why_not"] = шб["why_not"]
        return из_
    сч = шб["accounts"]
    # Раскладка первых девяти мест продажи измерена (SPECS_ПРОДАЖИ): пул 0,
    # наш кошелёк 1, config 2, минт базы 3, минт котировки 4, наш счёт базы 5,
    # наш счёт котировки 6, хранилище базы 7, хранилище котировки 8.
    из_.update(пул=сч[0], минт_базы=сч[3], минт_котировки=сч[4],
                наш_счёт_базы=сч[5], наш_счёт_котировки=сч[6],
                хранилище_базы=сч[7], хранилище_котировки=сч[8],
                счетов=len(сч), disc_покупки=шб.get("disc_покупки"))
    if сч[1] != наш_кошелёк:
        из_["why_not"] = ("это не наша покупка: на месте кошелька "
                           f"{сч[1][:8]}, а не {наш_кошелёк[:8]}")
        return из_
    наш_остаток = остаток_счёта(сч[5], урл=урл)
    из_["остаток_базы"] = наш_остаток
    база_в = (int(количество) if количество else
               (int(наш_остаток * float(доля)) if наш_остаток else 0))
    из_["база_в"] = база_в
    if база_в <= 0:
        из_["why_not"] = "на нашем счёте базы нечего продавать"
        return из_
    рез_базы = остаток_счёта(сч[7], урл=урл)
    рез_кот = остаток_счёта(сч[8], урл=урл)
    из_.update(резерв_базы=рез_базы, резерв_котировки=рез_кот)
    м = минимум_по_живым_резервам(база_в=база_в, резерв_базы=рез_базы or 0,
                                   резерв_котировки=рез_кот or 0, запас=запас)
    из_["минимум"] = м
    if not м["ok"]:
        из_["why_not"] = м["why_not"]
        return из_
    сб = собрать_продажу(шаблон=шб, наш_кошелёк=наш_кошелёк, база_в=база_в,
                          минимум_выхода=м["min_out"], минт_котировки=сч[4],
                          cu_units=cu_units, cu_price_micro=cu_price_micro)
    из_["сборка"] = {к: зн for к, зн in сб.items() if к != "tx_base64"}
    if not сб["ok"]:
        из_["why_not"] = сб["why_not"]
        return из_
    из_.update(ok=True, tx_base64=сб["tx_base64"])
    return из_


def симулировать(tx_base64: str, *, урл: str) -> dict:
    """simulateTransaction без подписи и с заменой blockhash -- ничего не уходит."""
    о = _зов("simulateTransaction",
              [tx_base64, {"sigVerify": False, "replaceRecentBlockhash": True,
                            "encoding": "base64", "commitment": "processed",
                            "innerInstructions": False}], урл=урл)
    if not о["ok"]:
        return {"ok": False, "why_not": о["why_not"]}
    зн = (о["result"] or {}).get("value") or {}
    логи = зн.get("logs") or []
    return {"ok": зн.get("err") is None, "err": зн.get("err"),
             "units_consumed": зн.get("unitsConsumed"),
             "логов": len(логи), "логи_хвост": логи[-12:],
             "why_not": (None if зн.get("err") is None else str(зн.get("err"))[:200])}


def открытая_позиция_полосы(state_dir: str, *, программа: str = SB.PUMP_AMM,
                             свежесть_s: float | None = 900.0,
                             сейчас: float | None = None) -> dict:
    """Открытая позиция полосы этого типа пула: её покупка и количество.

    ЗАЧЕМ ЖДАТЬ ЖИВУЮ ПОЗИЦИЮ. Симулировать продажу закрытой позиции
    бессмысленно: токенов на счёте нет, и узел ответит про деньги, а не про
    раскладку счетов. Держание полосы -- около 29 секунд, поэтому прогон ждёт
    открытую позицию на хосте и собирает продажу в её окне.
    """
    import glob as _gl  # noqa: PLC0415
    import gzip as _gz  # noqa: PLC0415
    import json as _js  # noqa: PLC0415

    по_cid: dict = {}
    for путь in sorted(_gl.glob(str(Path(state_dir) / "positions.jsonl*"))):
        открыть = _gz.open if путь.endswith(".gz") else open
        try:
            ф = открыть(путь, "rt", encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        with ф:
            for строка in ф:
                строка = строка.strip()
                if not строка:
                    continue
                try:
                    з = _js.loads(строка)
                except ValueError:
                    continue
                cid = з.get("client_order_id")
                if not cid:
                    continue
                в = по_cid.setdefault(cid, {})
                for к, зн in з.items():
                    if зн is not None:
                        в[к] = зн
    для_нас = []
    for cid, п in по_cid.items():
        if not п.get("lane"):
            continue
        if str(п.get("state") or "").lower() not in ("open", "selling"):
            continue
        # ТИП ПУЛА -- СТРОГО ТОТ. Прежде None проходил как "любой", и первый
        # живой прогон 28.09 взял позицию speed_only от 25.09 с pool_program
        # null: у неё в покупке нет инструкции Pump AMM вовсе.
        if программа and п.get("pool_program") != программа:
            continue
        подпись = п.get("lane_landed_signature") or п.get("lane_signature")
        if not подпись:
            continue
        для_нас.append({"cid": cid, "подпись": подпись,
                         "минт": п.get("mint"),
                         "куплено": п.get("lane_bought_raw"),
                         "группа": п.get("lane_group"),
                         "программа": п.get("pool_program"),
                         "ts": п.get("ts_sent") or п.get("ts_intent") or 0})
    для_нас.sort(key=lambda з: -float(з["ts"] or 0))
    # СВЕЖЕСТЬ. Запись, открытая сутки назад, -- это остаток или недозакрытая
    # позиция, а не живое окно держания: симулировать по ней продажу значит
    # мерить не то. Отказ называет возраст самой свежей найденной.
    if для_нас and свежесть_s:
        т = сейчас if сейчас is not None else __import__("time").time()
        свежие = [з for з in для_нас
                   if float(т) - float(з["ts"] or 0) <= float(свежесть_s)]
        if not свежие:
            возраст = round(float(т) - float(для_нас[0]["ts"] or 0), 1)
            return {"ok": False, "открытых": len(для_нас),
                     "why_not": (f"самая свежая открытая позиция старше "
                                  f"{свежесть_s} с (возраст {возраст} с)")}
        для_нас = свежие
    if not для_нас:
        return {"ok": False, "why_not": "открытых позиций полосы этого типа нет"}
    return {"ok": True, "позиция": для_нас[0], "открытых": len(для_нас),
             "why_not": None}


def main() -> int:
    import argparse  # noqa: PLC0415
    import json as _js  # noqa: PLC0415

    р = argparse.ArgumentParser(description=__doc__)
    р.add_argument("--podpis", default="",
                    help="подпись НАШЕЙ севшей покупки того же пула")
    р.add_argument("--zhdat-s", dest="zhdat_s", type=float, default=0.0,
                    help=("ждать ОТКРЫТУЮ позицию полосы столько секунд и взять "
                          "её покупку: у закрытой позиции токенов нет, и узел "
                          "ответит про деньги, а не про раскладку счетов"))
    р.add_argument("--state-dir", dest="state_dir",
                    default="/home/bot/bloom_executor_live_data")
    р.add_argument("--koshelek", default=os.environ.get("BLOOM_LANE_WALLET") or "")
    р.add_argument("--kolichestvo", type=int, default=0,
                    help="сколько базы продать (пусто -- весь остаток x доля)")
    р.add_argument("--dolya", type=float, default=1.0)
    р.add_argument("--zapas", type=float, default=None)
    р.add_argument("--cu", type=int, default=170_000)
    р.add_argument("--cena-cu", dest="cena_cu", type=int, default=0)
    р.add_argument("--simulirovat", default="yes")
    р.add_argument("--out", default="")
    а = р.parse_args()
    ключ = os.environ.get("HELIUS_API_KEY") or ""
    урл = f"https://mainnet.helius-rpc.com/?api-key={ключ}"
    кош = а.koshelek or ""
    if not кош:
        try:
            import bloom_own_send as OSW  # noqa: PLC0415

            кош = OSW.кошелёк_полосы()
        except Exception:  # noqa: BLE001
            кош = ""
    if not кош:
        print("СБОЙ: кошелёк полосы не задан (BLOOM_LANE_WALLET)")
        return 1
    подпись = а.podpis
    ждал = None
    if а.zhdat_s and а.zhdat_s > 0:
        import time as _t  # noqa: PLC0415

        до = _t.monotonic() + float(а.zhdat_s)
        while _t.monotonic() < до:
            п_ = открытая_позиция_полосы(а.state_dir)
            if п_["ok"]:
                подпись = п_["позиция"]["подпись"]
                ждал = п_["позиция"]
                break
            _t.sleep(0.5)
        if ждал is None:
            print(_js.dumps({"ok": False,
                              "why_not": (f"за {а.zhdat_s} с открытой позиции "
                                           "полосы Pump AMM не появилось")},
                             ensure_ascii=False))
            return 1
    if not подпись:
        print("СБОЙ: нужна подпись покупки (--podpis) или ожидание (--zhdat-s)")
        return 1
    сб = собрать_по_нашей_покупке(
        подпись=подпись, урл=урл, наш_кошелёк=кош,
        количество=(а.kolichestvo or None), доля=а.dolya, запас=а.zapas,
        cu_units=а.cu, cu_price_micro=а.cena_cu)
    итог = {к: зн for к, зн in сб.items() if к != "tx_base64"}
    итог["ждал_позицию"] = ждал
    if сб.get("ok") and а.simulirovat == "yes":
        итог["симуляция"] = симулировать(сб["tx_base64"], урл=урл)
    текст = _js.dumps(итог, ensure_ascii=False, indent=1)
    if а.out:
        Path(а.out).write_text(текст, encoding="utf-8")
    print(текст[:6000])
    return 0 if сб.get("ok") and (итог.get("симуляция") or {}).get("ok", True) else 1


def self_test() -> int:
    """Денежный путь: счета, аргументы, минимум, закрытие WSOL."""
    пройдено = провалено = 0

    def chk(что, ок, факт=""):
        nonlocal пройдено, провалено
        if ок:
            пройдено += 1
            print(f"  [ok  ] {что}")
        else:
            провалено += 1
            print(f"  [ПРОВАЛ] {что}: {факт}")

    НАШ = "4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x"
    # 26 счетов покупки: настоящие адреса не нужны, нужна форма и порядок. Но
    # адреса обязаны быть ЗАКОННЫМИ ключами: сборка проходит через solders, и
    # выдуманная строка там падает -- пусть падает в самопроверке, а не в бою.
    def _ключ(i: int) -> str:
        return str(Pubkey.from_bytes(bytes([i + 1] * 32)))

    счета = [_ключ(i) for i in range(26)]
    счета[1] = НАШ
    счета[4] = C.WSOL
    tx = {"transaction": {"message": {
            "accountKeys": [{"pubkey": а, "writable": (и in (0, 5, 6, 7, 8))}
                             for и, а in enumerate(счета)],
            "instructions": [{"programId": SB.PUMP_AMM, "accounts": счета,
                               "data": SB.b58encode(
                                   SB.disc("buy_exact_quote_in")
                                   + struct.pack("<QQ", 10_000_000, 1))}]}},
           "meta": {"innerInstructions": []}}
    шб = шаблон_продажи_из_покупки(tx, хранилище=счета[7])
    chk("шаблон продажи -- 24 счёта из 26",
        шб["ok"] and len(шб["accounts"]) == 24 and шб["счетов_покупки"] == 26, шб)
    chk("выброшены РОВНО индексы 19 и 20",
        счета[19] not in шб["accounts"] and счета[20] not in шб["accounts"]
        and all(а in шб["accounts"] for и, а in enumerate(счета)
                 if и not in (19, 20)), шб.get("accounts"))
    chk("порядок остальных счетов сохранён",
        шб["accounts"][:5] == счета[:5]
        and шб["accounts"][18] == счета[18]
        and шб["accounts"][19] == счета[21], шб["accounts"][:6])
    chk("покупка с 20 счетами -- отказ, а не молчаливая сборка",
        шаблон_продажи_из_покупки(
            {"transaction": {"message": {
                "accountKeys": [{"pubkey": _ключ(i + 40), "writable": False}
                                 for i in range(20)],
                "instructions": [{"programId": SB.PUMP_AMM,
                                   "accounts": [_ключ(i + 40) for i in range(20)],
                                   "data": SB.b58encode(
                                       SB.disc("buy_exact_quote_in")
                                       + struct.pack("<QQ", 1, 1))}]}},
              "meta": {}})["ok"] is False)
    chk("программы пула в транзакции нет -- причина словами",
        шаблон_продажи_из_покупки({"transaction": {"message": {
            "accountKeys": [], "instructions": []}}, "meta": {}})["why_not"]
        is not None)

    # ПРАВО ЗАПИСИ У АДРЕСА ИЗ ТАБЛИЦЫ. Иначе счёт молча стал бы "только для
    # чтения", и отказала бы уже живая продажа.
    tx_alt = {"transaction": {"message": {
                "accountKeys": [{"pubkey": а, "writable": (и in (0, 5))}
                                 for и, а in enumerate(счета[:24])],
                "instructions": [{"programId": SB.PUMP_AMM, "accounts": счета,
                                   "data": SB.b58encode(
                                       SB.disc("buy_exact_quote_in")
                                       + struct.pack("<QQ", 1, 1))}]}},
               "meta": {"loadedAddresses": {"writable": [счета[7], счета[8]],
                                             "readonly": [счета[25]]}}}
    шб_alt = шаблон_продажи_из_покупки(tx_alt)
    chk("права записи взяты и из таблиц адресов",
        шб_alt["ok"] and шб_alt["writable"].get(счета[7]) is True
        and шб_alt["writable"].get(счета[8]) is True
        and шб_alt["из_таблиц"] == 3, шб_alt.get("из_таблиц"))

    # МИНИМУМ ВЫХОДА. Кривая без комиссии: 100 базы в пул с 1000 базы и 2000
    # котировки даёт 2000*100/1100 = 181; с запасом 0.25 пол = 135.
    м = минимум_по_живым_резервам(база_в=100, резерв_базы=1000,
                                   резерв_котировки=2000, запас=0.25)
    chk(f"минимум по живым резервам: кривая {м.get('po_krivoj')}, пол {м.get('min_out')}",
        м["ok"] and м["po_krivoj"] == 181 and м["min_out"] == 135, м)
    chk("нулевые резервы -- отказ с причиной, а не ноль как минимум",
        минимум_по_живым_резервам(база_в=100, резерв_базы=0,
                                   резерв_котировки=2000)["ok"] is False)
    chk("запас из окружения читается и рамки соблюдаются",
        abs(запас_выхода() - ЗАПАС_ВЫХОДА_ПО_УМОЛЧАНИЮ) < 1e-9)
    os.environ["BLOOM_SELL_MIN_OUT_ZAPAS"] = "0.4"
    chk("запас 0.4 из окружения даёт пол 108", 
        минимум_по_живым_резервам(база_в=100, резерв_базы=1000,
                                   резерв_котировки=2000)["min_out"] == 108)
    os.environ["BLOOM_SELL_MIN_OUT_ZAPAS"] = "5"
    chk("негодный запас из окружения не применяется",
        abs(запас_выхода() - ЗАПАС_ВЫХОДА_ПО_УМОЛЧАНИЮ) < 1e-9)
    os.environ.pop("BLOOM_SELL_MIN_OUT_ZAPAS", None)

    сб = собрать_продажу(шаблон=шб, наш_кошелёк=НАШ, база_в=601_488_972_334,
                          минимум_выхода=123_456, минт_котировки=C.WSOL,
                          cu_units=170_000, cu_price_micro=5_882)
    chk("транзакция собралась и подпись одна (наш кошелёк)",
        сб["ok"] and сб["подписей"] == 1, сб)
    chk("инструкций четыре: лимит, цена, продажа, закрытие WSOL",
        сб["n_instructions"] == 4 and сб["закрыт_счёт_wsol"], сб)
    chk("счёт WSOL для закрытия -- ATA нашего кошелька",
        сб["закрыт_счёт_wsol"] == SB.ata(НАШ, C.WSOL, SB.TOKEN_PROGRAM))
    # Аргументы инструкции продажи -- ровно те, что просили.
    сырое = base64.b64decode(сб["tx_base64"])
    chk("в данных продажи стоит дискриминатор sell и оба числа",
        (SB.disc("sell") + struct.pack("<QQ", 601_488_972_334, 123_456)) in сырое,
        сб["tx_base64"][:40])
    chk("нулевое количество не собирается вовсе",
        собрать_продажу(шаблон=шб, наш_кошелёк=НАШ, база_в=0, минимум_выхода=1,
                         минт_котировки=C.WSOL)["ok"] is False)
    сб2 = собрать_продажу(шаблон=шб, наш_кошелёк=НАШ, база_в=10, минимум_выхода=1,
                           минт_котировки=_ключ(99))
    chk("котировка не WSOL -- закрытия счёта нет",
        сб2["ok"] and сб2["закрыт_счёт_wsol"] is None
        and сб2["n_instructions"] == 2, сб2)
    # ВЫБОР ЖИВОЙ ПОЗИЦИИ: закрытая не годится, берётся самая свежая открытая.
    import json as _js2  # noqa: PLC0415
    import tempfile as _tp  # noqa: PLC0415

    with _tp.TemporaryDirectory() as д:
        (Path(д) / "positions.jsonl").write_text("\n".join(
            _js2.dumps(з, ensure_ascii=False) for з in [
                {"client_order_id": "з1", "lane": "own_send", "state": "closed",
                  "pool_program": SB.PUMP_AMM, "lane_landed_signature": "П1",
                  "ts_sent": 100.0},
                {"client_order_id": "з2", "lane": "own_send", "state": "open",
                  "pool_program": SB.PUMP_AMM, "lane_landed_signature": "П2",
                  "ts_sent": 200.0, "lane_bought_raw": 555},
                {"client_order_id": "з3", "lane": "own_send", "state": "open",
                  "pool_program": SB.BONDING, "lane_landed_signature": "П3",
                  "ts_sent": 300.0},
                {"client_order_id": "з4", "lane": None, "state": "open",
                  "pool_program": SB.PUMP_AMM, "lane_landed_signature": "П4",
                  "ts_sent": 400.0},
            ]) + "\n", encoding="utf-8")
        оп = открытая_позиция_полосы(д, свежесть_s=None)
        chk("взята открытая позиция полосы нужного типа, а не закрытая и не чужая",
            оп["ok"] and оп["позиция"]["подпись"] == "П2"
            and оп["позиция"]["куплено"] == 555 and оп["открытых"] == 1, оп)
        chk("другой тип пула не берётся под Pump AMM",
            открытая_позиция_полосы(д, программа=SB.BONDING,
                                     свежесть_s=None)["позиция"]["подпись"]
            == "П3")
        (Path(д) / "positions.jsonl").write_text(
            _js2.dumps({"client_order_id": "з5", "lane": "own_send",
                         "state": "closed", "pool_program": SB.PUMP_AMM,
                         "lane_landed_signature": "П5"}) + "\n",
            encoding="utf-8")
        chk("нет открытых -- отказ словами, а не пустая сборка",
            открытая_позиция_полосы(д, свежесть_s=None)["ok"] is False)
        (Path(д) / "positions.jsonl").write_text("\n".join(
            _js2.dumps(з, ensure_ascii=False) for з in [
                {"client_order_id": "з6", "lane": "own_send", "state": "open",
                  "pool_program": SB.PUMP_AMM, "lane_landed_signature": "П6",
                  "ts_sent": 1000.0},
                {"client_order_id": "з7", "lane": "own_send", "state": "open",
                  "pool_program": None, "lane_landed_signature": "П7",
                  "ts_sent": 9000.0},
            ]) + "\n", encoding="utf-8")
        chk("pool_program null НЕ считается за Pump AMM",
            открытая_позиция_полосы(д, свежесть_s=None)["позиция"]["подпись"]
            == "П6")
        chk("старая открытая запись не берётся, и возраст назван",
            открытая_позиция_полосы(д, свежесть_s=900.0, сейчас=100000.0)["ok"]
            is False
            and "возраст" in (открытая_позиция_полосы(
                д, свежесть_s=900.0, сейчас=100000.0)["why_not"] or ""))
    print(f"самопроверка своей продажи: {пройдено}/{пройдено + провалено} пройдено")
    return 1 if провалено else 0


if __name__ == "__main__":
    raise SystemExit(self_test() if "--self-test" in sys.argv else main())
