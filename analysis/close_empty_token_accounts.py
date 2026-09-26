#!/usr/bin/env python3
"""Закрыть ПУСТЫЕ токен-счета и вернуть запертую в них ренту.

ЗАЧЕМ. По цепи в 320 незакрытых счетах двух кошельков заперто 0.714136 SOL, и
0.488980 из них заперла сама ночь 25->26.09 (замер: у каждого счёта спрошена
дата рождения, data/token_accounts_age_*.json). Покупка счёт создаёт, продажа
его не закрывает -- деньги не потеряны, они лежат. Вернуть их можно только
закрытием счёта, а это ЗАПИСЬ В ЦЕПЬ, поэтому здесь всё построено так, чтобы
ошибка стоила ноль:

* ПО УМОЛЧАНИЮ НИЧЕГО НЕ ОТПРАВЛЯЕТСЯ. Отправка только при
  CLOSE_ACCOUNTS_LIVE=1; без флага прогон печатает план и суммы;
* ЗАКРЫВАЕМ ТОЛЬКО ПУСТЫЕ. Единственное исключение -- счета WSOL: у них
  остаток это завёрнутый SOL, он возвращается владельцу вместе с рентой
  (признак isNative от самой цепи, а не по имени минта);
* ПОЛУЧАТЕЛЬ РЕНТЫ -- ТОЛЬКО САМ КОШЕЛЁК. Адреса получателя нет ни в
  аргументах, ни в окружении: он равен владельцу счёта по построению;
* ЧУЖОЙ СЧЁТ НЕ ЗАКРЫВАЕТСЯ: владелец счёта обязан совпасть с нашим
  кошельком, а публичный ключ -- с ним же (проверка перед подписью);
* СВЕЖЕСТЬ ЧТЕНИЯ. Остаток перечитывается прямо перед сборкой пакета; чтение
  старше 30 с не годится. Счёт, у которого остаток изменился, из пакета
  выпадает;
* ПРИ ТОРГОВЛЕ НЕ РАБОТАЕМ. Полоса на каждой покупке создаёт счёт WSOL, и
  закрывать счета вслепую посреди сделки нельзя: нужен либо стоящий рубильник
  KILL, либо явное CLOSE_ACCOUNTS_WHILE_TRADING=1.

Ключ -- только из окружения (EXEC_WALLET_KEY / BLOOM_WALLET_KEY для
исполнителя, OWN_SEND_WALLET_KEY для полосы -- ровно те имена, что уже стоят в
окружении службы на хосте). В журнал, вывод и Telegram он не
попадает ни при какой ошибке: наружу идёт только имя класса исключения.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_exec_state as ST  # noqa: E402

WSOL = "So11111111111111111111111111111111111111112"
ТОКЕН = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ТОКЕН22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ПРОГРАММЫ_ТОКЕНА = (ТОКЕН, ТОКЕН22)
ЗАКРЫТЬ = 9                      # CloseAccount у обеих программ токена
СРОК_СВЕЖЕСТИ_S = 30.0
В_ПАКЕТЕ = 12
ПРЕДЕЛ_РАЗМЕРА_TX = 1232
РЕНТА_ОБЫЧНОГО = 0.00203928      # реальную ренту берём из цепи, это только
РЕНТА_ЛАМПОРТОВ = 2_039_280      # ожидание для сверки


def _узел() -> str:
    """Адрес узла: ключ Helius из окружения, как во всех прогонах репозитория.
    Своего адреса в коде нет -- только сборка из ключа."""
    ключ = (os.environ.get("HELIUS_API_KEY") or os.environ.get("HELIUS_API") or "").strip()
    if not ключ:
        raise RuntimeError("ключа узла нет в окружении (HELIUS_API_KEY)")
    return f"https://mainnet.helius-rpc.com/?api-key={ключ}"


def _вызов_узла(метод: str, парам: list, *, срок: float = 25.0):
    """Один вызов JSON-RPC. Отдельно от solana_crowd_scan намеренно: на хосте
    службы того модуля нет, а прогон обязан работать именно там -- ключ кошелька
    полосы живёт только в окружении службы."""
    import requests  # noqa: PLC0415
    от = requests.post(_узел(), json={"jsonrpc": "2.0", "id": 1, "method": метод,
                                      "params": парам}, timeout=срок)
    от.raise_for_status()
    тело = от.json()
    if "error" in тело:
        raise RuntimeError(f"узел ответил ошибкой: {str(тело['error'])[:160]}")
    return тело.get("result")


def живой_режим() -> bool:
    return (os.environ.get("CLOSE_ACCOUNTS_LIVE") or "0").strip() == "1"


def можно_сейчас() -> tuple:
    """Закрывать счета можно либо при стоящем рубильнике, либо по явному флагу.

    Полоса создаёт счёт WSOL на каждой покупке. Закрыть его в момент, когда
    сделка в воздухе, -- это сломать сделку, а не вернуть ренту.
    """
    if (os.environ.get("CLOSE_ACCOUNTS_WHILE_TRADING") or "0").strip() == "1":
        return True, "разрешено явным флагом CLOSE_ACCOUNTS_WHILE_TRADING=1"
    try:
        стоит = ST.kill_file().exists()
    except Exception as exc:  # noqa: BLE001
        return False, f"рубильник не проверить ({type(exc).__name__})"
    if стоит:
        return True, "рубильник KILL стоит -- торговли нет"
    return False, ("рубильник KILL не стоит: полоса создаёт счёт WSOL на каждой "
                   "покупке, закрывать счета посреди торговли нельзя")


def подходит_для_закрытия(счёт: dict, кошелёк: str) -> tuple:
    """(годен, причина_отказа). Решение только по свежему чтению из цепи."""
    if not isinstance(счёт, dict):
        return False, "счёт не разобран"
    if счёт.get("program") not in ПРОГРАММЫ_ТОКЕНА:
        return False, f"программа счёта не токеновая: {счёт.get('program')}"
    if счёт.get("owner") != кошелёк:
        return False, f"владелец счёта {счёт.get('owner')}, а не наш кошелёк"
    сырое = str(счёт.get("amount_raw") if счёт.get("amount_raw") is not None else "")
    if not сырое.isdigit():
        return False, f"остаток не прочитан: {счёт.get('amount_raw')!r}"
    if счёт.get("is_native"):
        # Завёрнутый SOL -- это SOL, он вернётся владельцу вместе с рентой.
        return True, None
    if int(сырое) != 0:
        return False, f"остаток не ноль ({сырое}) -- закрывать нельзя, токен потеряется"
    return True, None


def вернётся_лампортов(счёт: dict) -> int:
    """Сколько лампортов вернёт закрытие: рента счёта плюс завёрнутый SOL."""
    рента = int(счёт.get("lamports") or 0)
    # lamports счёта -- это и есть всё, что на нём лежит (рента, а у WSOL
    # рента плюс завёрнутое). Ничего не складываем дважды.
    return рента


def инструкция_закрытия(счёт: dict, кошелёк: str):
    from solders.instruction import AccountMeta, Instruction  # noqa: PLC0415
    from solders.pubkey import Pubkey  # noqa: PLC0415
    годен, почему = подходит_для_закрытия(счёт, кошелёк)
    if not годен:
        raise ValueError(f"счёт не годен к закрытию: {почему}")
    к = Pubkey.from_string(кошелёк)
    return Instruction(
        Pubkey.from_string(счёт["program"]), bytes([ЗАКРЫТЬ]),
        # ПОЛУЧАТЕЛЬ -- САМ КОШЕЛЁК. Второй метой стоит он же, и другого
        # адреса здесь взять негде: аргумента получателя у функции нет.
        [AccountMeta(Pubkey.from_string(счёт["account"]), False, True),
         AccountMeta(к, False, True),
         AccountMeta(к, True, True)])


def пакеты(счета: list, кошелёк: str, *, в_пакете: int = В_ПАКЕТЕ) -> dict:
    """План: годные счета по пакетам, негодные -- с причинами."""
    годные, отказы = [], []
    for с in счета:
        ок, почему = подходит_для_закрытия(с, кошелёк)
        (годные if ок else отказы).append(с if ок else {**с, "why_not": почему})
    группы = [годные[и:и + в_пакете] for и in range(0, len(годные), в_пакете)]
    return {"пакетов": len(группы), "счетов_годных": len(годные),
            "счетов_отказано": len(отказы),
            "вернётся_лампортов": sum(вернётся_лампортов(с) for с in годные),
            "группы": группы, "отказы": отказы}


def собрать_пакет(группа: list, кошелёк: str, *, cu_units: int = 200_000,
                  cu_price_micro: int = 0) -> dict:
    """Неподписанная транзакция закрытия пакета. Ни подписи, ни отправки."""
    from solders.hash import Hash  # noqa: PLC0415
    from solders.message import MessageV0  # noqa: PLC0415
    from solders.pubkey import Pubkey  # noqa: PLC0415
    from solders.signature import Signature  # noqa: PLC0415
    from solders.transaction import VersionedTransaction  # noqa: PLC0415
    import c2_swap_build as B  # noqa: PLC0415
    ixs = [B.cu_limit(cu_units)]
    if cu_price_micro:
        ixs.append(B.cu_price(cu_price_micro))
    ixs += [инструкция_закрытия(с, кошелёк) for с in группа]
    msg = MessageV0.try_compile(Pubkey.from_string(кошелёк), ixs, [], Hash.default())
    vtx = VersionedTransaction.populate(msg, [Signature.default()] * msg.header.num_required_signatures)
    сырое = bytes(vtx)
    return {"tx_base64": base64.b64encode(сырое).decode(), "size": len(сырое),
            "счетов": len(группа), "n_instructions": len(ixs),
            "вернётся_лампортов": sum(вернётся_лампортов(с) for с in группа)}


def прочитать_счета(rpc, адреса: list, кошелёк: str) -> dict:
    """Свежее чтение: остаток, владелец, программа, лампорты на счёте."""
    из_ = {"utc": time.time(), "счета": [], "why_not": None}
    for и in range(0, len(адреса), 100):
        часть = адреса[и:и + 100]
        try:
            от = rpc("getMultipleAccounts", [часть, {"encoding": "jsonParsed",
                                                     "commitment": "confirmed"}]) or {}
        except Exception as exc:  # noqa: BLE001
            из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:100]}"
            return из_
        for адрес, зн in zip(часть, (от.get("value") or [])):
            if not зн:
                из_["счета"].append({"account": адрес, "why_not": "счёта в цепи нет"})
                continue
            инфо = (((зн.get("data") or {}).get("parsed") or {}).get("info") or {})
            сумма = (инфо.get("tokenAmount") or {})
            из_["счета"].append({
                "account": адрес, "program": зн.get("owner"),
                "lamports": зн.get("lamports"), "mint": инфо.get("mint"),
                "owner": инфо.get("owner"), "is_native": bool(инфо.get("isNative")),
                "amount_raw": сумма.get("amount"), "decimals": сумма.get("decimals")})
    return из_


def свежо(чтение: dict, *, срок: float = СРОК_СВЕЖЕСТИ_S, сейчас: float | None = None) -> bool:
    сейчас = time.time() if сейчас is None else сейчас
    return bool(чтение) and (сейчас - float(чтение.get("utc") or 0)) <= срок


def ключ_кошелька(кошелёк: str) -> dict:
    """Секрет ТОГО кошелька, чьи счета закрываем. Сам секрет наружу не идёт."""
    import bloom_jupiter_sell as J  # noqa: PLC0415
    имена = (("OWN_SEND_WALLET_KEY",) if кошелёк != ST.EXECUTOR_WALLET
             else ("EXEC_WALLET_KEY", "BLOOM_WALLET_KEY"))
    секрет = next(((os.environ.get(и) or "").strip() for и in имена
                   if (os.environ.get(и) or "").strip()), "")
    if not секрет:
        return {"ok": False, "why_not": f"ключа кошелька нет в окружении ({' или '.join(имена)})"}
    п = J.публичный_ключ(секрет)
    if not п.get("ok"):
        return {"ok": False, "why_not": п.get("why_not") or "ключ не разобрался"}
    if п["pubkey"] != кошелёк:
        return {"ok": False, "why_not": f"ключ принадлежит {п['pubkey']}, а счета кошелька "
                                        f"{кошелёк} -- подписывать нельзя"}
    return {"ok": True, "секрет": секрет}


def закрыть_живьём(rpc, кошелёк: str, группы: list, *, секрет: str,
                   ждать_s: float = 30.0, пауза_s: float = 2.0) -> dict:
    """Закрыть пакеты по цепи. Без CLOSE_ACCOUNTS_LIVE=1 сети не касается вовсе.

    Каждый пакет: свежее перечитывание счетов -> отбор годных -> сборка ->
    подпись -> отправка -> подтверждение -> баланс до и после. Первый же сбой
    останавливает прогон: неизвестный результат хуже недоделанной работы.
    """
    из_ = {"ok": False, "why_not": None, "пакетов": 0, "закрыто_счетов": 0,
           "вернулось_лампортов": 0, "шаги": []}
    if not живой_режим():
        из_["why_not"] = "живой режим выключен (CLOSE_ACCOUNTS_LIVE не равен 1)"
        return из_
    можно, почему = можно_сейчас()
    if not можно:
        из_["why_not"] = почему
        return из_
    try:
        from solders.hash import Hash  # noqa: PLC0415
        from solders.message import MessageV0  # noqa: PLC0415
        from solders.transaction import VersionedTransaction  # noqa: PLC0415
        from dbot_rescue import load_rescue_keypair  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"модули подписи не загружены: {type(exc).__name__}"
        return из_
    try:
        kp = load_rescue_keypair(секрет)
    except Exception as exc:  # noqa: BLE001
        # Текст исключения наружу НЕ идёт: в него может попасть сам секрет.
        из_["why_not"] = f"ключ не разобрался ({type(exc).__name__})"
        return из_
    if str(kp.pubkey()) != кошелёк:
        из_["why_not"] = "публичный ключ не совпал с кошельком -- закрытие отменено"
        return из_

    def баланс() -> int:
        от = rpc("getBalance", [кошелёк, {"commitment": "confirmed"}]) or {}
        return int((от.get("value") if isinstance(от, dict) else от) or 0)

    for номер, группа in enumerate(группы, 1):
        шаг = {"пакет": номер, "просили": len(группа), "закрыто": 0, "подпись": None,
               "баланс_до": None, "баланс_после": None, "дельта_лампортов": None,
               "ожидали_лампортов": None, "why_not": None}
        try:
            чтение = прочитать_счета(rpc, [с["account"] for с in группа], кошелёк)
            if чтение.get("why_not") or not свежо(чтение):
                шаг["why_not"] = чтение.get("why_not") or "чтение счетов устарело"
                из_["шаги"].append(шаг)
                из_["why_not"] = шаг["why_not"]
                return из_
            годные = [с for с in чтение["счета"] if подходит_для_закрытия(с, кошелёк)[0]]
            шаг["закрыто"] = len(годные)
            шаг["ожидали_лампортов"] = sum(вернётся_лампортов(с) for с in годные)
            if not годные:
                шаг["why_not"] = "после свежего чтения годных счетов не осталось"
                из_["шаги"].append(шаг)
                continue
            шаг["баланс_до"] = баланс()
            собрано = собрать_пакет(годные, кошелёк)
            bh = ((rpc("getLatestBlockhash", [{"commitment": "finalized"}]) or {})
                  .get("value") or {}).get("blockhash")
            if not bh:
                шаг["why_not"] = "узел не дал blockhash"
                из_["шаги"].append(шаг)
                из_["why_not"] = шаг["why_not"]
                return из_
            сырое = base64.b64decode(собрано["tx_base64"])
            tx = VersionedTransaction.from_bytes(сырое)
            msg = tx.message
            новое = MessageV0(msg.header, msg.account_keys, Hash.from_string(bh),
                              msg.instructions, msg.address_table_lookups)
            подписанная = VersionedTransaction(новое, [kp])
            подпись = rpc("sendTransaction", [
                base64.b64encode(bytes(подписанная)).decode(),
                {"encoding": "base64", "skipPreflight": False, "maxRetries": 3,
                 "preflightCommitment": "confirmed"}])
            шаг["подпись"] = подпись
            срок = time.time() + ждать_s
            села = False
            while time.time() < срок:
                time.sleep(пауза_s)
                ст = ((rpc("getSignatureStatuses", [[подпись], {"searchTransactionHistory": True}])
                       or {}).get("value") or [None])[0]
                if ст and (ст.get("confirmationStatus") in ("confirmed", "finalized")
                           or ст.get("slot")):
                    села = ст.get("err") is None
                    шаг["ошибка_цепи"] = ст.get("err")
                    break
            шаг["баланс_после"] = баланс()
            шаг["дельта_лампортов"] = шаг["баланс_после"] - шаг["баланс_до"]
            if not села:
                шаг["why_not"] = "транзакция не села или села с ошибкой"
                из_["шаги"].append(шаг)
                из_["why_not"] = шаг["why_not"]
                return из_
            из_["пакетов"] += 1
            из_["закрыто_счетов"] += len(годные)
            из_["вернулось_лампортов"] += шаг["дельта_лампортов"]
        except Exception as exc:  # noqa: BLE001
            шаг["why_not"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            из_["шаги"].append(шаг)
            из_["why_not"] = шаг["why_not"]
            return из_
        из_["шаги"].append(шаг)
    из_["ok"] = из_["why_not"] is None
    return из_


def счета_кошелька(rpc, кошелёк: str) -> dict:
    """Токен-счета кошелька прямо из цепи: по одному вызову на программу токена.

    Файл со списком не нужен -- так свежесть обеспечена построением, а не
    надеждой на прошлый прогон.
    """
    из_ = {"utc": time.time(), "счета": [], "why_not": None}
    for программа in ПРОГРАММЫ_ТОКЕНА:
        try:
            от = rpc("getTokenAccountsByOwner", [кошелёк, {"programId": программа},
                                                 {"encoding": "jsonParsed",
                                                  "commitment": "confirmed"}]) or {}
        except Exception as exc:  # noqa: BLE001
            из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:100]}"
            return из_
        for зн in (от.get("value") or []):
            счёт = зн.get("pubkey")
            данные = (((зн.get("account") or {}).get("data") or {}).get("parsed") or {})
            инфо = данные.get("info") or {}
            сумма = инфо.get("tokenAmount") or {}
            из_["счета"].append({
                "account": счёт, "program": (зн.get("account") or {}).get("owner") or программа,
                "lamports": (зн.get("account") or {}).get("lamports"),
                "mint": инфо.get("mint"), "owner": инфо.get("owner"),
                "is_native": bool(инфо.get("isNative")),
                "amount_raw": сумма.get("amount"), "decimals": сумма.get("decimals")})
    return из_


def self_test() -> int:
    проверки = []

    def chk(имя, ок, что=""):
        проверки.append((имя, bool(ок), что))

    кош = ST.EXECUTOR_WALLET
    пустой = {"account": "11111111111111111111111111111112", "program": ТОКЕН,
              "lamports": РЕНТА_ЛАМПОРТОВ, "mint": "M", "owner": кош,
              "is_native": False, "amount_raw": "0"}
    с_остатком = dict(пустой, amount_raw="12345")
    всол = {"account": "11111111111111111111111111111113", "program": ТОКЕН,
            "lamports": 4_078_560, "mint": WSOL, "owner": кош,
            "is_native": True, "amount_raw": "1488440"}
    чужой = dict(пустой, owner="CHUZHOY1111111111111111111111111111111111111")

    chk("пустой счёт годен", подходит_для_закрытия(пустой, кош)[0] is True)
    ок_ост, почему_ост = подходит_для_закрытия(с_остатком, кош)
    chk("счёт с остатком НЕ закрывается, и причина про потерю токена",
        ок_ост is False and "токен потеряется" in (почему_ост or ""), почему_ост)
    chk("счёт WSOL с завёрнутым SOL годен (остаток вернётся владельцу)",
        подходит_для_закрытия(всол, кош)[0] is True)
    ок_ч, почему_ч = подходит_для_закрытия(чужой, кош)
    chk("чужой счёт не закрывается", ок_ч is False and "владелец" in (почему_ч or ""), почему_ч)
    chk("нечитаемый остаток -- отказ, а не ноль по умолчанию",
        подходит_для_закрытия(dict(пустой, amount_raw=None), кош)[0] is False)
    chk("не токеновая программа -- отказ",
        подходит_для_закрытия(dict(пустой, program="XXX"), кош)[0] is False)

    # Получатель ренты -- только сам кошелёк, и подписант тоже он.
    ix = инструкция_закрытия(пустой, кош)
    адреса = [str(m.pubkey) for m in ix.accounts]
    chk("в инструкции закрытия получатель и подписант -- наш кошелёк, и никого больше",
        адреса == [пустой["account"], кош, кош] and bytes(ix.data) == bytes([ЗАКРЫТЬ])
        and [m.is_signer for m in ix.accounts] == [False, False, True], адреса)
    try:
        инструкция_закрытия(с_остатком, кош)
        chk("инструкция на счёт с остатком не собирается", False)
    except ValueError:
        chk("инструкция на счёт с остатком не собирается", True)

    # План: годные, отказанные и сумма возврата.
    план = пакеты([пустой, с_остатком, всол, чужой], кош, в_пакете=2)
    chk("план: годных 2, отказано 2, возврат = сумма лампортов счетов",
        план["счетов_годных"] == 2 and план["счетов_отказано"] == 2
        and план["вернётся_лампортов"] == РЕНТА_ЛАМПОРТОВ + 4_078_560
        and план["пакетов"] == 1, {к: план[к] for к in ("пакетов", "счетов_годных",
                                                         "счетов_отказано",
                                                         "вернётся_лампортов")})
    # Пакет на 15 счетов влезает в предел сети.
    много = [dict(пустой, account=str(__import__("solders").pubkey.Pubkey.default()))] * 0
    try:
        from solders.keypair import Keypair  # noqa: PLC0415
        много = [dict(пустой, account=str(Keypair().pubkey())) for _ in range(15)]
        соб = собрать_пакет(много, кош)
        chk(f"пакет из 15 закрытий: {соб['size']} байт при пределе {ПРЕДЕЛ_РАЗМЕРА_TX}",
            соб["size"] <= ПРЕДЕЛ_РАЗМЕРА_TX and соб["счетов"] == 15, соб["size"])
    except Exception as exc:  # noqa: BLE001
        chk("пакет из 15 закрытий собирается", False, f"{type(exc).__name__}: {exc}")

    # Живой режим по умолчанию выключен; отправки в модуле нет вовсе.
    было = os.environ.pop("CLOSE_ACCOUNTS_LIVE", None)
    try:
        chk("без флага живой режим выключен", живой_режим() is False)
        os.environ["CLOSE_ACCOUNTS_LIVE"] = "1"
        chk("флаг включает живой режим", живой_режим() is True)
    finally:
        os.environ.pop("CLOSE_ACCOUNTS_LIVE", None)
        if было is not None:
            os.environ["CLOSE_ACCOUNTS_LIVE"] = было
    текст = Path(__file__).read_text(encoding="utf-8")
    # Отправка ровно одна и ровно в одном месте. Считаем вызовы, а не слова:
    # в самопроверке имя метода тоже встречается.
    рабочая_часть = текст.split("def self_test", 1)[0]
    chk("отправка в рабочей части ровно одна -- в закрыть_живьём",
        рабочая_часть.count("sendTransaction") == 1,
        рабочая_часть.count("sendTransaction"))

    # БЕЗ ФЛАГА СЕТИ НЕ КАСАЕМСЯ ВОВСЕ: rpc, который на любой вызов кидает.
    def rpc_запрещён(метод, парам):
        raise AssertionError(f"сети касаться нельзя, а позвали {метод}")

    было_live = os.environ.pop("CLOSE_ACCOUNTS_LIVE", None)
    try:
        без_флага = закрыть_живьём(rpc_запрещён, кош, [[пустой]], секрет="не ключ")
        chk("без CLOSE_ACCOUNTS_LIVE=1 отказ ДО любого вызова сети",
            без_флага["ok"] is False and "живой режим выключен" in (без_флага["why_not"] or ""),
            без_флага["why_not"])
        os.environ["CLOSE_ACCOUNTS_LIVE"] = "1"
        было_тр = os.environ.pop("CLOSE_ACCOUNTS_WHILE_TRADING", None)
        было_kf = os.environ.get("BLOOM_KILL_FILE")
        проба = Path(os.environ.get("TMPDIR") or "/tmp") / "close_accounts_kill_probe2"
        try:
            os.environ["BLOOM_KILL_FILE"] = str(проба)
            проба.unlink(missing_ok=True)
            без_kill = закрыть_живьём(rpc_запрещён, кош, [[пустой]], секрет="не ключ")
            chk("с флагом, но без рубильника -- отказ ДО сети",
                без_kill["ok"] is False and "KILL" in (без_kill["why_not"] or ""),
                без_kill["why_not"])
            проба.write_text("kill")
            чужой_ключ = закрыть_живьём(rpc_запрещён, кош, [[пустой]], секрет="не ключ")
            chk("плохой ключ -- отказ ДО сети, и текста исключения наружу нет",
                чужой_ключ["ok"] is False
                and "ключ не разобрался" in (чужой_ключ["why_not"] or "")
                and "не ключ" not in (чужой_ключ["why_not"] or ""),
                чужой_ключ["why_not"])
        finally:
            проба.unlink(missing_ok=True)
            if было_kf is None:
                os.environ.pop("BLOOM_KILL_FILE", None)
            else:
                os.environ["BLOOM_KILL_FILE"] = было_kf
            if было_тр is not None:
                os.environ["CLOSE_ACCOUNTS_WHILE_TRADING"] = было_тр
    finally:
        os.environ.pop("CLOSE_ACCOUNTS_LIVE", None)
        if было_live is not None:
            os.environ["CLOSE_ACCOUNTS_LIVE"] = было_live

    # СПИСОК СЧЕТОВ ИЗ ЦЕПИ: что разобрали, то и решаем закрывать. Ошибка
    # разбора здесь означала бы закрытие не того счёта, поэтому проверка есть.
    ответы = {
        ТОКЕН: {"value": [
            {"pubkey": "11111111111111111111111111111112",
             "account": {"owner": ТОКЕН, "lamports": РЕНТА_ЛАМПОРТОВ,
                         "data": {"parsed": {"info": {
                             "mint": "M", "owner": кош, "isNative": False,
                             "tokenAmount": {"amount": "0", "decimals": 6}}}}}},
            {"pubkey": "11111111111111111111111111111113",
             "account": {"owner": ТОКЕН, "lamports": 4_078_560,
                         "data": {"parsed": {"info": {
                             "mint": WSOL, "owner": кош, "isNative": True,
                             "tokenAmount": {"amount": "1488440", "decimals": 9}}}}}},
            {"pubkey": "11111111111111111111111111111114",
             "account": {"owner": ТОКЕН, "lamports": РЕНТА_ЛАМПОРТОВ,
                         "data": {"parsed": {"info": {
                             "mint": "M2", "owner": кош, "isNative": False,
                             "tokenAmount": {"amount": "777", "decimals": 6}}}}}}]},
        ТОКЕН22: {"value": []},
    }

    def rpc_список(метод, парам):
        assert метод == "getTokenAccountsByOwner", метод
        return ответы[парам[1]["programId"]]

    сп = счета_кошелька(rpc_список, кош)
    план_сп = пакеты(сп["счета"], кош, в_пакете=10)
    chk("список счетов из цепи разобран: три счёта, годных два (третий с остатком)",
        len(сп["счета"]) == 3 and план_сп["счетов_годных"] == 2
        and план_сп["счетов_отказано"] == 1
        and план_сп["вернётся_лампортов"] == РЕНТА_ЛАМПОРТОВ + 4_078_560,
        {"счетов": len(сп["счета"]), **{к: план_сп[к] for к in ("счетов_годных", "счетов_отказано")}})
    chk("у счёта из цепи взяты владелец, минт, признак нативности и остаток",
        сп["счета"][1]["owner"] == кош and сп["счета"][1]["mint"] == WSOL
        and сп["счета"][1]["is_native"] is True and сп["счета"][1]["amount_raw"] == "1488440",
        сп["счета"][1])

    # Свежесть чтения: старое чтение не годится.
    chk("чтение старше 30 с не свежее",
        свежо({"utc": 1000.0}, сейчас=1031.0) is False and свежо({"utc": 1000.0}, сейчас=1029.0) is True)

    # Рубильник: без него и без явного флага закрывать нельзя.
    было_фл = os.environ.pop("CLOSE_ACCOUNTS_WHILE_TRADING", None)
    было_kill = os.environ.get("BLOOM_KILL_FILE")
    tmp = Path(os.environ.get("TMPDIR") or "/tmp") / "close_accounts_kill_probe"
    try:
        os.environ["BLOOM_KILL_FILE"] = str(tmp)
        tmp.unlink(missing_ok=True)
        нельзя, почему_нельзя = можно_сейчас()
        chk("без рубильника и без флага закрывать нельзя",
            нельзя is False and "KILL" in (почему_нельзя or ""), почему_нельзя)
        tmp.write_text("kill")
        chk("при стоящем рубильнике можно", можно_сейчас()[0] is True)
        tmp.unlink(missing_ok=True)
        os.environ["CLOSE_ACCOUNTS_WHILE_TRADING"] = "1"
        chk("явный флаг разрешает и без рубильника", можно_сейчас()[0] is True)
    finally:
        tmp.unlink(missing_ok=True)
        os.environ.pop("CLOSE_ACCOUNTS_WHILE_TRADING", None)
        if было_фл is not None:
            os.environ["CLOSE_ACCOUNTS_WHILE_TRADING"] = было_фл
        if было_kill is None:
            os.environ.pop("BLOOM_KILL_FILE", None)
        else:
            os.environ["BLOOM_KILL_FILE"] = было_kill

    плохих = 0
    for имя, ок, что in проверки:
        print(f"  [{'ok  ' if ок else 'СБОЙ'}] {имя}" + (f"  -> {что}" if not ок and что != "" else ""))
        плохих += (not ок)
    print(f"самопроверка закрытия счетов: {len(проверки) - плохих}/{len(проверки)} пройдено")
    return 0 if плохих == 0 else 1


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--self-test", action="store_true")
    р.add_argument("--accounts", default="",
                   help="файл со списком счетов (пусто -- спросить цепь: так свежесть "
                        "обеспечена построением, а не надеждой на прошлый прогон)")
    р.add_argument("--wallet", default="", help="кошелёк (пусто -- из файла или исполнитель)")
    р.add_argument("--batch", type=int, default=В_ПАКЕТЕ)
    р.add_argument("--max-accounts", type=int, default=0, help="0 -- все годные")
    р.add_argument("--out", default=str(КОРЕНЬ / "data" / "close_accounts_plan.json"))
    р.add_argument("--result", default=str(КОРЕНЬ / "data" / "close_accounts_result.json"))
    р.add_argument("--max-batches", type=int, default=1,
                   help="сколько пакетов закрывать за прогон (1 -- по умолчанию, осторожно)")
    р.add_argument("--first-one", action="store_true",
                   help="первый прогон: закрыть РОВНО ОДИН счёт и сверить возврат")
    а = р.parse_args()
    if а.self_test:
        return self_test()

    кошелёк = а.wallet
    адреса = None
    if а.accounts:
        д = json.loads(Path(а.accounts).read_text(encoding="utf-8"))
        кошелёк = кошелёк or д.get("wallet") or ""
        адреса = [с["account"] for с in (д.get("empty") or []) + (д.get("with_balance") or [])]
    кошелёк = кошелёк or ST.EXECUTOR_WALLET
    if not кошелёк:
        print("СТОП: кошелёк не задан", file=sys.stderr)
        return 2

    вызовов = [0]

    def rpc(метод: str, парам: list):
        вызовов[0] += 1
        return _вызов_узла(метод, парам)

    print(f"кошелёк {кошелёк}; список счетов: "
          + ("из файла" if адреса is not None else "из цепи"))
    if адреса is None:
        найдено = счета_кошелька(rpc, кошелёк)
        if найдено.get("why_not"):
            print(f"СТОП: счета не прочитаны: {найдено['why_not']}", file=sys.stderr)
            return 2
        адреса = [с["account"] for с in найдено["счета"]]
    if а.max_accounts:
        адреса = адреса[:а.max_accounts]
    print(f"счетов к проверке {len(адреса)}")

    чтение = прочитать_счета(rpc, адреса, кошелёк)
    if чтение.get("why_not"):
        print(f"СТОП: счета не прочитаны: {чтение['why_not']}", file=sys.stderr)
        return 2
    план = пакеты(чтение["счета"], кошелёк, в_пакете=(1 if а.first_one else а.batch))
    можно, почему_можно = можно_сейчас()
    итог = {
        "кошелёк": кошелёк, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "живой_режим": живой_режим(), "можно_сейчас": можно, "почему": почему_можно,
        "счетов_проверено": len(чтение["счета"]),
        "счетов_годных": план["счетов_годных"], "счетов_отказано": план["счетов_отказано"],
        "вернётся_sol": round(план["вернётся_лампортов"] / 1_000_000_000, 9),
        "пакетов": план["пакетов"],
        "размеры_пакетов": [собрать_пакет(г, кошелёк)["size"] for г in план["группы"]],
        "отказы": [{"account": о["account"], "why_not": о.get("why_not")} for о in план["отказы"]],
    }
    Path(а.out).write_text(json.dumps(итог, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({к: v for к, v in итог.items() if к != "отказы"}, ensure_ascii=False, indent=1))
    if not живой_режим():
        print("ЖИВОГО РЕЖИМА НЕТ (CLOSE_ACCOUNTS_LIVE не равен 1): ничего не отправлено, "
              "это только план.")
        return 0
    if not можно:
        print(f"СТОП: {почему_можно}", file=sys.stderr)
        return 3
    к = ключ_кошелька(кошелёк)
    if not к.get("ok"):
        print(f"СТОП: {к.get('why_not')}", file=sys.stderr)
        return 4
    группы = план["группы"][:max(1, а.max_batches)] if not а.first_one else план["группы"][:1]
    факт = закрыть_живьём(rpc, кошелёк, группы, секрет=к["секрет"])
    факт["ожидали_sol"] = round(sum(вернётся_лампортов(с) for г in группы for с in г)
                                / 1_000_000_000, 9)
    факт["вернулось_sol"] = round(факт["вернулось_лампортов"] / 1_000_000_000, 9)
    Path(а.result).write_text(json.dumps(факт, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(факт, ensure_ascii=False, indent=1))
    return 0 if факт.get("ok") else 5


if __name__ == "__main__":
    raise SystemExit(main())
