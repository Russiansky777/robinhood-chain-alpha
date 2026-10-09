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
ПРОДАЖА = "sell_v3"
WHY_НЕТ_УЗЛА = "узел задаётся только окружением BLOOM_TREKKER_RPC"
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
    идл = СБ.zagruzit_idl()["pump"]["ix"][ПОКУПКА]
    диск = идл["disc"]
    имена = [а["name"] for а in идл["accounts"]]
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
                                              "maxSupportedTransactionVersion": 0,
                                              "commitment": "confirmed"}])
        if not т["ok"] or not т.get("result"):
            continue
        соо = ((т["result"].get("transaction") or {}).get("message") or {})
        ключи = [k.get("pubkey") if isinstance(k, dict) else k
                 for k in (соо.get("accountKeys") or [])]
        инстр = list(соо.get("instructions") or [])
        for вн in (т["result"].get("meta") or {}).get("innerInstructions") or []:
            инстр.extend(вн.get("instructions") or [])
        for и in инстр:
            прог = и.get("programId")
            данные = и.get("data")
            if прог != СБ.ПРОГ_КРИВОЙ or not isinstance(данные, str):
                continue
            try:
                сырые = СБ.b58d(данные)
            except Exception:  # noqa: BLE001
                continue
            if сырые[:8] != диск:
                continue
            счета = [ключи[с] if isinstance(с, int) and с < len(ключи) else с
                     for с in (и.get("accounts") or [])]
            по_именам = dict(zip(имена, счета, strict=False))
            минт = по_именам.get("base_mint")
            тп = по_именам.get("base_token_program")
            if из_["buyback"] is None and по_именам.get("buyback_fee_recipient"):
                из_["buyback"] = по_именам["buyback_fee_recipient"]
            if (минт and тп == СБ.ПРОГ_ТОКЕНА_2022 and минт not in видели):
                видели.add(минт)
                из_["minty"].append({"mint": минт, "base_token_program": тп,
                                      "iz_podpisi": подпись})
            break
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


def живой(*, skolko: int, koshelek: str, bilet_lamportov: int, cu: int,
          pauza: float) -> dict:
    из_ = {"rezhim": "zhivoj", "uzel": затереть(узел()), "koshelek": koshelek,
            "bilet_lamportov": bilet_lamportov, "minty": [], "proshlo": 0,
            "krasnyh": 0, "why_not": None}
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
    if len(из_["minty"]) < 3:
        из_["why_not"] = (f"минтов всего {len(из_['minty'])}, а нужно 3-5 -- "
                          "проверка не состоялась")
    elif из_["krasnyh"]:
        из_["why_not"] = f"{из_['krasnyh']} минтов из {len(из_['minty'])} красные"
    return из_


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
    п.add_argument("--out", default="")
    а = п.parse_args()
    if а.self_test:
        return самопроверка()
    if not а.live:
        print("СТОП: нужен --live или --self-test", file=sys.stderr)
        return 2
    из_ = живой(skolko=а.skolko, koshelek=а.koshelek,
                 bilet_lamportov=а.bilet_lamportov, cu=а.cu, pauza=а.pauza)
    текст = json.dumps(из_, ensure_ascii=False, indent=1)
    print(текст)
    if а.out:
        Path(а.out).write_text(текст + "\n", encoding="utf-8")
    return 0 if (из_["proshlo"] and not из_["why_not"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
