#!/usr/bin/env python3
"""Аккаунт долговечного nonce для кошелька полосы: проверить и создать.

ЗАЧЕМ. Вариант пула по слову владельца 25.09 (пункт 3): "по транзакции на
сервис, только его чаевые, один durable nonce". Замок от двойной покупки там --
сам nonce: первая исполнившаяся транзакция его сдвигает, остальные становятся
недействительны. Без аккаунта nonce на цепи варианта нет вовсе.

СКОЛЬКО ЭТО СТОИТ. Аккаунт nonce -- 80 байт, рента по цепи около 0.00144768 SOL
(спрашиваем у узла getMinimumBalanceForRentExemption, а не берём по памяти) плюс
базовый тариф за подпись. Деньги списываются С КОШЕЛЬКА ПОЛОСЫ и остаются на
аккаунте: рента возвращается, если аккаунт когда-нибудь закрыть.

АДРЕС НЕ СЛУЧАЙНЫЙ. Он выводится из кошелька полосы и семени
"bloom-lane-nonce" (create_with_seed). Значит адрес воспроизводим: его можно
посчитать заново на любой машине и сверить, а не хранить в файле и надеяться.

ЧЕГО ЗДЕСЬ НЕТ. Ключ читается из окружения службы на хосте (EXEC_WALLET_KEY или
BLOOM_WALLET_KEY) и НИКУДА не печатается. Создание идёт только по явному
--create: проверка ничего не тратит.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

СЕМЯ = "bloom-lane-nonce"
СИСТЕМНАЯ = "11111111111111111111111111111111"
РАЗМЕР_НОНСА = 80
SYSVAR_RENT = "SysvarRent111111111111111111111111111111111"


def кошелёк() -> str:
    import bloom_own_send as OS  # noqa: PLC0415

    return OS.кошелёк_полосы()


def адрес_нонса(наш: str | None = None) -> dict:
    """Адрес аккаунта nonce -- из кошелька полосы и семени, а не из файла."""
    из_ = {"ok": False, "address": None, "why_not": None, "seed": СЕМЯ}
    наш = наш or кошелёк()
    if not наш:
        из_["why_not"] = "кошелёк полосы не задан"
        return из_
    try:
        from solders.pubkey import Pubkey  # noqa: PLC0415

        адрес = Pubkey.create_with_seed(Pubkey.from_string(наш), СЕМЯ,
                                         Pubkey.from_string(СИСТЕМНАЯ))
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"адрес не посчитан: {type(exc).__name__}"
        return из_
    из_.update(ok=True, address=str(адрес), owner=наш)
    return из_


def состояние(rpc_call, *, адрес: str | None = None) -> dict:
    """Что лежит на этом адресе: nonce, что-то другое или ничего."""
    а = адрес or (адрес_нонса().get("address") or "")
    из_ = {"address": а, "exists": False, "is_nonce": False, "blockhash": None,
            "authority": None, "lamports": None, "why_not": None}
    if not а:
        из_["why_not"] = "адрес не посчитан"
        return из_
    от = rpc_call("getAccountInfo", [а, {"encoding": "jsonParsed",
                                          "commitment": "confirmed"}])
    значение = ((от or {}).get("value") or None)
    if значение is None:
        из_["why_not"] = "аккаунта нет на цепи -- его надо создать"
        return из_
    из_["exists"] = True
    из_["lamports"] = значение.get("lamports")
    разбор = (((значение.get("data") or {}).get("parsed") or {}).get("info") or {})
    если_тип = ((значение.get("data") or {}).get("parsed") or {}).get("type")
    if разбор.get("blockhash") and разбор.get("authority"):
        из_.update(is_nonce=True, blockhash=разбор["blockhash"],
                   authority=разбор["authority"], kind=если_тип)
    else:
        из_["why_not"] = f"на адресе не аккаунт nonce, а {если_тип or 'неизвестно что'}"
    return из_


def создать(rpc_call, *, секрет: str | None = None, наш: str | None = None) -> dict:
    """Создать и инициализировать аккаунт nonce. ЭТО ДЕНЬГИ: рента и тариф.

    Одна транзакция: CreateAccountWithSeed + InitializeNonceAccount. Рента
    спрашивается у узла. Распорядителем ставится кошелёк полосы -- иначе сдвинуть
    nonce нашей подписью будет нельзя.
    """
    из_ = {"ok": False, "why_not": None, "address": None, "signature": None,
            "rent_lamports": None}
    наш = наш or кошелёк()
    а = адрес_нонса(наш)
    if not а.get("ok"):
        из_["why_not"] = а.get("why_not")
        return из_
    из_["address"] = а["address"]
    уже = состояние(rpc_call, адрес=а["address"])
    if уже.get("is_nonce"):
        из_.update(ok=True, already=True, blockhash=уже.get("blockhash"),
                   authority=уже.get("authority"))
        return из_
    if уже.get("exists"):
        из_["why_not"] = (f"на адресе уже есть аккаунт, но это не nonce: "
                           f"{уже.get('why_not')}")
        return из_
    try:
        from solders.hash import Hash  # noqa: PLC0415
        from solders.instruction import AccountMeta, Instruction  # noqa: PLC0415
        from solders.message import MessageV0  # noqa: PLC0415
        from solders.pubkey import Pubkey  # noqa: PLC0415
        from solders.transaction import VersionedTransaction  # noqa: PLC0415
        import struct  # noqa: PLC0415

        from dbot_rescue import load_rescue_keypair  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"модули подписи не загружены: {type(exc).__name__}"
        return из_
    рента = rpc_call("getMinimumBalanceForRentExemption", [РАЗМЕР_НОНСА])
    if not isinstance(рента, int) or рента <= 0:
        из_["why_not"] = f"узел не сказал ренту: {рента!r}"
        return из_
    из_["rent_lamports"] = рента
    хеш = ((rpc_call("getLatestBlockhash", [{"commitment": "finalized"}])
            or {}).get("value") or {}).get("blockhash")
    if not хеш:
        из_["why_not"] = "узел не дал blockhash"
        return из_
    # Ключ -- одним разборщиком на репозиторий (dbot_rescue), и его текст
    # исключений наружу не идёт: в нём может оказаться сам секрет.
    try:
        # КЛЮЧ ИМЕННО КОШЕЛЬКА ПОЛОСЫ и в том же порядке, что у модуля полосы:
        # OWN_SEND_WALLET_KEY первым. Ключ исполнителя -- только откат для
        # старого случая "полоса на кошельке исполнителя"; подписать им nonce
        # кошелька полосы нельзя, и проверка ниже это поймает.
        кп = load_rescue_keypair(секрет if секрет is not None
                                 else (os.environ.get("OWN_SEND_WALLET_KEY")
                                        or os.environ.get("EXEC_WALLET_KEY")
                                        or os.environ.get("BLOOM_WALLET_KEY")))
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"ключ не разобран: {type(exc).__name__}"
        return из_
    if str(кп.pubkey()) != наш:
        из_["why_not"] = (f"ключ не от кошелька полосы: {str(кп.pubkey())[:12]} "
                           f"вместо {наш[:12]}")
        return из_
    # CreateAccountWithSeed: код 3, поля base, seed (строка с длиной), lamports,
    # space, owner. Порядок и типы -- по описанию системной программы.
    семя_байты = СЕМЯ.encode()
    данные_создать = (struct.pack("<I", 3) + bytes(Pubkey.from_string(наш))
                       + struct.pack("<Q", len(семя_байты)) + семя_байты
                       + struct.pack("<QQ", рента, РАЗМЕР_НОНСА)
                       + bytes(Pubkey.from_string(СИСТЕМНАЯ)))
    создать_ix = Instruction(Pubkey.from_string(СИСТЕМНАЯ), данные_создать, [
        AccountMeta(Pubkey.from_string(наш), True, True),
        AccountMeta(Pubkey.from_string(а["address"]), False, True)])
    # InitializeNonceAccount: код 6, распорядитель -- кошелёк полосы.
    данные_инит = struct.pack("<I", 6) + bytes(Pubkey.from_string(наш))
    инит_ix = Instruction(Pubkey.from_string(СИСТЕМНАЯ), данные_инит, [
        AccountMeta(Pubkey.from_string(а["address"]), False, True),
        AccountMeta(Pubkey.from_string(
            "SysvarRecentB1ockHashes11111111111111111111"), False, False),
        AccountMeta(Pubkey.from_string(SYSVAR_RENT), False, False)])
    сообщение = MessageV0.try_compile(Pubkey.from_string(наш),
                                       [создать_ix, инит_ix], [],
                                       Hash.from_string(хеш))
    tx = VersionedTransaction(сообщение, [кп])
    сырое = bytes(tx)
    от = rpc_call("sendTransaction",
                   [base64.b64encode(сырое).decode(),
                    {"encoding": "base64", "skipPreflight": False,
                     "maxRetries": 3}])
    подпись = от if isinstance(от, str) else (от or {}).get("result")
    if not подпись:
        из_["why_not"] = f"узел не принял транзакцию: {str(от)[:200]}"
        return из_
    из_.update(ok=True, signature=подпись, size=len(сырое))
    return из_


def self_test() -> int:
    проверки = []

    def chk(имя, ок, факт=""):
        проверки.append((имя, bool(ок), факт))

    а = адрес_нонса("21DqHDDPEfMhK1dHRkV9E8v8KTTSKQGApJAr1irC9j7w")
    chk("адрес nonce считается из кошелька и семени", а["ok"] and а["address"], а)
    б = адрес_нонса("21DqHDDPEfMhK1dHRkV9E8v8KTTSKQGApJAr1irC9j7w")
    chk("адрес ВОСПРОИЗВОДИМ -- дважды одно и то же",
        а["address"] == б["address"], (а["address"], б["address"]))
    в = адрес_нонса("4s87RRC2V2XAJD6R8U2dP8kQH99Z2wA6fg88ZVfV4j4N")
    chk("у другого кошелька адрес другой", в["address"] != а["address"],
        (а["address"], в["address"]))

    # СОСТОЯНИЕ: нет аккаунта, чужой аккаунт, наш nonce -- три разных ответа.
    chk("нет аккаунта -- так и сказано",
        состояние(lambda м, п: {"value": None}, адрес=а["address"])["why_not"]
        .startswith("аккаунта нет"), "")
    чужое = состояние(lambda м, п: {"value": {"lamports": 5, "data": {
        "parsed": {"type": "mint", "info": {}}}}}, адрес=а["address"])
    chk("на адресе не nonce -- отказ с названием того, что там",
        чужое["exists"] and чужое["is_nonce"] is False
        and "mint" in (чужое["why_not"] or ""), чужое)
    наш = состояние(lambda м, п: {"value": {"lamports": 1447680, "data": {
        "parsed": {"type": "initialized", "info": {
            "blockhash": "3fS2cJ7qkX8xJqAe7pbqKXG9YfC6HkQzLmVnKyU8x7Jd",
            "authority": "21DqHDDPEfMhK1dHRkV9E8v8KTTSKQGApJAr1irC9j7w"}}}}},
        адрес=а["address"])
    chk("наш nonce читается: хеш и распорядитель",
        наш["is_nonce"] and наш["blockhash"].startswith("3fS2")
        and наш["authority"].startswith("21Dq"), наш)
    # СОЗДАНИЕ НЕ ИДЁТ, если аккаунт уже nonce: второй раз платить ренту незачем.
    уже = создать(lambda м, п: {"value": {"lamports": 1447680, "data": {
        "parsed": {"type": "initialized", "info": {
            "blockhash": "3fS2cJ7qkX8xJqAe7pbqKXG9YfC6HkQzLmVnKyU8x7Jd",
            "authority": кошелёк()}}}}}, наш=кошелёк())
    chk("аккаунт уже есть -- создание не повторяется",
        уже["ok"] and уже.get("already") is True, уже)

    плохо = [(и, ф) for и, ок, ф in проверки if not ок]
    for имя, ок, факт in проверки:
        print(f"  [{'ok  ' if ок else 'СБОЙ'}] {имя}"
              + ("" if ок else f" -- факт: {факт}"))
    print(f"самопроверка аккаунта nonce: {len(проверки) - len(плохо)}/"
          f"{len(проверки)} пройдено")
    return 1 if плохо else 0


def main() -> int:
    import argparse

    р = argparse.ArgumentParser(description=__doc__)
    р.add_argument("--self-test", action="store_true")
    р.add_argument("--address-only", action="store_true",
                    help="только посчитать адрес, без узла")
    р.add_argument("--wallet", default=None,
                    help="кошелёк полосы (по умолчанию -- из окружения)")
    р.add_argument("--create", action="store_true",
                    help="СОЗДАТЬ аккаунт nonce (тратит ренту с кошелька полосы)")
    а = р.parse_args()
    if а.self_test:
        return self_test()
    адр = адрес_нонса(а.wallet)
    print(json.dumps(адр, ensure_ascii=False))
    if а.address_only:
        return 0 if адр.get("ok") else 1
    import solana_rpc_client as RPC  # noqa: PLC0415

    клиент = RPC.SolanaRPCClient(service="lane_nonce")
    def зов(метод, параметры):
        return клиент.call(метод, параметры)

    с = состояние(зов, адрес=адр.get("address"))
    print(json.dumps(с, ensure_ascii=False))
    if not а.create:
        return 0 if с.get("is_nonce") else 1
    итог = создать(зов)
    print(json.dumps(итог, ensure_ascii=False))
    return 0 if итог.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
