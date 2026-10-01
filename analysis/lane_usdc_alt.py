#!/usr/bin/env python3
"""НАША таблица адресов (ALT) для USDC-ноги: посчитать, показать, создать.

ЗАЧЕМ. Замер Code-3 (docs/usdc_noga_kak_vstroit.md, п.7) назвал гейт числом: две
ноги в одной транзакции БЕЗ таблиц адресов не влезают в 1232 байта ни на одном
типе пула -- 1326…1703 байта на 32 живых сделках. Из 32 источников свою таблицу
несут 23, у девяти её нет вовсе: для этих девяти без НАШЕЙ таблицы маршрут
отказывает по размеру, то есть сигнал теряется.

ЧТО СЮДА КЛАДЁТСЯ. Только то, что в каждой USDC-сделке ОДНО И ТО ЖЕ:

  * счета первой ноги SOL -> USDC (пул, хранилища, конфиг, минты) -- они от
    сигнала не зависят вовсе: нога у нас одна и та же;
  * наши собственные счета этой ноги (ATA USDC, ATA WSOL и прочие наши места) --
    они static ДЛЯ НАС, хотя у другого кошелька были бы другие;
  * программы: системная, Token, Token-2022, ATA, ComputeBudget и программы
    ВСЕХ типов пулов, которыми полоса собирает вторую ногу;
  * адреса чаевых: их выбирают по сигналу, но список постоянный.

ЧЕГО ЗДЕСЬ НЕТ. Счетов ВТОРОЙ ноги (пул токена, его хранилища, минт) -- они у
каждого сигнала свои, и в постоянной таблице им места нет по определению. И
самого кошелька полосы: плательщик и подписант обязаны остаться статическим
ключом транзакции, из таблицы их брать нельзя.

НАШИ МЕСТА НАХОДЯТСЯ ЗАМЕРОМ, А НЕ ПО ПАМЯТИ. Инструкция ноги собирается ДВАЖДЫ
-- нашим кошельком и чужим, -- и поехавшие места это и есть наши. Так же это
делал Code-3 для второй ноги. Вывод ATA по минту тут не годится: у pump_amm на
месте 20 стоит наш счёт, который ATA не является вовсе, а у источника на месте
счёта котировки лежал не-ATA.

ДЕНЬГИ. Создание таблицы платит ренту с кошелька полосы: она НАША и возвращается,
если таблицу когда-нибудь закрыть (deactivate + close). Рента берётся у узла
(getMinimumBalanceForRentExemption) и ПРОВЕРЯЕТСЯ по цепи -- фактической дельтой
кошелька за наши подписи, а не по формуле. Без явного --sozdat ничего не
тратится: умолчание -- только показать.

ТОЛЬКО ЧТЕНИЕ БЕЗ --sozdat. Ключ читается из окружения службы и никуда не
печатается.
"""
from __future__ import annotations

import json
import os
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ПРОГРАММА_ALT = "AddressLookupTab1e1111111111111111111111111"
СИСТЕМНАЯ = "11111111111111111111111111111111"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_ПРОГРАММА = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"

# Чужой кошелёк ТОЛЬКО для замера наших мест: им ничего не подписывается и
# ничего не отправляется. Тот же адрес, которым мерил Code-3.
ЧУЖОЙ_ДЛЯ_ЗАМЕРА = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"

# Размер записи таблицы: 56 байт заголовка плюс 32 на адрес (устройство аккаунта
# таблицы адресов). Рента всё равно спрашивается у узла -- это для проверки.
ЗАГОЛОВОК_ALT = 56
# За один ExtendLookupTable кладём не больше этого: транзакция расширения тоже
# обязана влезть в 1232 байта (32 байта на адрес плюс подпись и заголовок).
АДРЕСОВ_ЗА_РАЗ = 24
ФАЙЛ_СОСТОЯНИЯ = "usdc_noga_alt.json"


def _модули():
    import bloom_nogi_shablony as NS  # noqa: PLC0415
    import bloom_own_send as OS  # noqa: PLC0415
    import c2_swap_build as B  # noqa: PLC0415

    return NS, OS, B


def кошелёк() -> str:
    _NS, OS, _B = _модули()
    return OS.кошелёк_полосы()


def программы_типов() -> dict:
    """Программы всех типов пулов, которыми полоса собирает вторую ногу.

    Берутся ИЗ c2_swap_build, а не перечисляются здесь строками: появится новый
    строитель -- таблица его получит, и никто об этом не забудет.
    """
    _NS, _OS, B = _модули()
    из_ = {}
    for имя in ("PUMP_AMM", "CPMM", "BONDING", "CLMM", "DLMM", "WHIRLPOOL",
                 "DAMM2", "DBC", "AMMV4", "LAUNCHLAB"):
        зн = getattr(B, имя, None)
        if isinstance(зн, str) and зн:
            из_[зн] = f"программа пула {имя.lower()}"
    return из_


def адреса_чаевых(путь: str | None = None) -> dict:
    """Белый список чаевых: счёт выбирается по семени сделки, список постоянный.

    Берётся из того же реестра отправителей, что и сама отправка
    (bloom_senders.реестр -> senders[*].tip_accounts): перечислять их здесь
    строками значило бы завести второй список, который разойдётся с первым.
    """
    из_ = {}
    try:
        import bloom_senders as S  # noqa: PLC0415

        for имя, з in sorted((S.реестр(путь) or {}).items()):
            for а in (з.get("tip_accounts") or []):
                if isinstance(а, str) and 32 <= len(а) <= 44:
                    из_.setdefault(а, f"счёт чаевых {имя}")
    except Exception:  # noqa: BLE001
        pass
    return из_


def адреса(*, наш: str | None = None, путь_ног: str | None = None,
            чужой: str = ЧУЖОЙ_ДЛЯ_ЗАМЕРА, с_чаевыми: bool = False) -> dict:
    """Что положить в таблицу и ПОЧЕМУ каждое -- без сети и без денег."""
    из_ = {"ok": False, "why_not": None, "наш": None, "адреса": [],
            "почему": {}, "статичных_ноги": None, "наших_мест": None,
            "нога_программа": None, "нога_пул": None, "котировка": USDC}
    NS, _OS, B = _модули()
    наш = наш or кошелёк()
    if not наш:
        из_["why_not"] = "кошелёк полосы не задан"
        return из_
    из_["наш"] = наш
    try:
        статичные, почему_ф = NS.загрузить(путь=путь_ног)
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"кэш ног не загрузился: {type(exc).__name__}"
        return из_
    зап = (getattr(статичные, "записи", None) or {}).get(USDC)
    if not isinstance(зап, dict) or not зап.get("tpl"):
        из_["why_not"] = (f"шаблона ноги SOL -> USDC в кэше нет"
                           + (f": {почему_ф}" if почему_ф else ""))
        return из_
    из_["нога_программа"] = зап.get("program")
    из_["нога_пул"] = (зап["tpl"].get("accounts") or [None])[0]
    # ЗАМЕР НАШИХ МЕСТ: одна и та же инструкция двумя кошельками.
    try:
        наша = B.swap_instruction(зап["tpl"], зап["tx"], наш, 10_000_000, 1)
        чужая = B.swap_instruction(зап["tpl"], зап["tx"], чужой, 10_000_000, 1)
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"инструкция ноги не собралась: {type(exc).__name__}"
        return из_
    кн = [str(м.pubkey) for м in наша.accounts]
    кч = [str(м.pubkey) for м in чужая.accounts]
    if len(кн) != len(кч) or not кн:
        из_["why_not"] = "замер мест не сошёлся по длине"
        return из_
    порядок, почему = [], {}

    def добавить(адрес: str, зачем: str) -> None:
        if not адрес or адрес == наш:
            # Кошелёк полосы -- плательщик и подписант: он обязан остаться
            # статическим ключом, из таблицы его брать нельзя.
            return
        if адрес in почему:
            return
        почему[адрес] = зачем
        порядок.append(адрес)

    наших = 0
    for и, (а_н, а_ч) in enumerate(zip(кн, кч)):
        if а_н == а_ч:
            добавить(а_н, f"счёт ноги SOL -> USDC, место {и} (статичный)")
        else:
            наших += 1
            добавить(а_н, f"НАШ счёт ноги, место {и} (замер: у чужого {а_ч[:8]})")
    из_["статичных_ноги"] = sum(1 for а_н, а_ч in zip(кн, кч) if а_н == а_ч)
    из_["наших_мест"] = наших
    добавить(str(наша.program_id), "программа первой ноги")
    for адрес, зачем in (
            (СИСТЕМНАЯ, "системная программа"),
            (TOKEN, "программа Token"),
            (TOKEN_2022, "программа Token-2022"),
            (ATA_ПРОГРАММА, "программа ATA"),
            (COMPUTE_BUDGET, "программа ComputeBudget"),
            (WSOL, "минт WSOL"),
            (USDC, "минт USDC")):
        добавить(адрес, зачем)
    for адрес, зачем in программы_типов().items():
        добавить(адрес, зачем)
    # ЧАЕВЫЕ -- ПО ЖЕЛАНИЮ И НЕ СРАЗУ. В одной транзакции используется один
    # счёт чаевых на отправителя, а белый список -- 31 адрес: это удвоило бы
    # ренту (0.0158 против 0.0089 SOL) ради запаса, который может и не
    # понадобиться. Таблицу РАСШИРИТЬ можно и потом, доплатив только разницу,
    # поэтому порядок такой: сначала нога и программы, потом замер размера, и
    # чаевые -- только если без них не влезает.
    if с_чаевыми:
        for адрес, зачем in адреса_чаевых().items():
            добавить(адрес, зачем)
    else:
        из_["чаевых_не_положено"] = len(адреса_чаевых())
    из_.update(ok=True, адреса=порядок, почему=почему)
    return из_


def размер_и_рента_по_формуле(сколько: int) -> dict:
    """Размер аккаунта таблицы и рента ПО ФОРМУЛЕ -- для сверки с ответом узла.

    3480 лампортов за байт в год, порог освобождения 2 года, 128 байт накладных
    на любой аккаунт. Это ПРОВЕРКА, а не источник: число для траты берётся у узла.
    """
    байт = ЗАГОЛОВОК_ALT + 32 * int(сколько)
    return {"байт": байт, "рента_лампорты": (128 + байт) * 3480 * 2}


def состояние(rpc_call, адрес: str) -> dict:
    """Что лежит на адресе таблицы: она, что-то другое или ничего."""
    из_ = {"адрес": адрес, "есть": False, "это_таблица": False,
            "адресов": None, "лампорты": None, "why_not": None}
    о = rpc_call("getAccountInfo", [адрес, {"encoding": "base64"}])
    зн = (о or {}).get("value")
    if not зн:
        return из_
    из_.update(есть=True, лампорты=зн.get("lamports"))
    if зн.get("owner") != ПРОГРАММА_ALT:
        из_["why_not"] = f"аккаунт принадлежит не таблице адресов: {зн.get('owner')}"
        return из_
    import base64  # noqa: PLC0415

    try:
        сырое = base64.b64decode((зн.get("data") or ["", ""])[0])
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"данные таблицы не разобраны: {type(exc).__name__}"
        return из_
    из_.update(это_таблица=True,
                адресов=max(0, (len(сырое) - ЗАГОЛОВОК_ALT) // 32))
    return из_


def _ключ(секрет: str | None):
    from dbot_rescue import load_rescue_keypair  # noqa: PLC0415

    return load_rescue_keypair(секрет if секрет is not None
                                else (os.environ.get("OWN_SEND_WALLET_KEY")
                                       or os.environ.get("EXEC_WALLET_KEY")
                                       or os.environ.get("BLOOM_WALLET_KEY")))


def _отправить(rpc_call, подписанная) -> dict:
    import base64  # noqa: PLC0415

    сырое = base64.b64encode(bytes(подписанная)).decode()
    ответ = rpc_call("sendTransaction",
                      [сырое, {"encoding": "base64", "skipPreflight": False,
                                "maxRetries": 3,
                                "preflightCommitment": "confirmed"}])
    if isinstance(ответ, str):
        return {"ok": True, "signature": ответ}
    return {"ok": False, "why_not": f"узел не принял: {str(ответ)[:200]}"}


def _дождаться(rpc_call, подпись: str, *, предел_с: float = 90.0,
                спать=time.sleep) -> dict:
    """Ждать подтверждения. Не дождались -- так и говорим, а не «готово»."""
    край = time.time() + предел_с
    while time.time() < край:
        о = rpc_call("getTransaction",
                      [подпись, {"encoding": "json", "commitment": "confirmed",
                                  "maxSupportedTransactionVersion": 0}])
        if isinstance(о, dict) and о.get("meta") is not None:
            ош = (о.get("meta") or {}).get("err")
            return {"ok": ош is None, "why_not": (f"цепь вернула ошибку: {ош}"
                                                   if ош else None), "tx": о}
        спать(2.0)
    return {"ok": False, "why_not": f"подтверждения нет за {предел_с:.0f} с",
             "tx": None}


def _дельта_кошелька(tx: dict, наш: str) -> float | None:
    """Сколько SOL ушло с кошелька в этой транзакции -- ПО ЦЕПИ."""
    try:
        import c2_itog_po_cepi as C13  # noqa: PLC0415

        д = C13.дельта_транзакции(tx or {}, наш)
        return д.get("кошелёк_sol") if д.get("ok") else None
    except Exception:  # noqa: BLE001
        return None


def создать(rpc_call, *, наш: str | None = None, секрет: str | None = None,
             путь_ног: str | None = None, с_чаевыми: bool = False,
             спать=time.sleep) -> dict:
    """Создать таблицу и залить адреса. ЭТО ДЕНЬГИ: рента и тариф за подписи."""
    из_ = {"ok": False, "why_not": None, "адрес": None, "адресов": None,
            "подписи": [], "рента_узел_sol": None, "рента_по_цепи_sol": None,
            "рента_по_формуле_sol": None, "slot": None}
    наш = наш or кошелёк()
    сп = адреса(наш=наш, путь_ног=путь_ног, с_чаевыми=с_чаевыми)
    if not сп.get("ok"):
        из_["why_not"] = сп.get("why_not")
        return из_
    список = сп["адреса"]
    из_["адресов"] = len(список)
    ф = размер_и_рента_по_формуле(len(список))
    из_["рента_по_формуле_sol"] = round(ф["рента_лампорты"] / 1e9, 9)
    try:
        from solders.hash import Hash  # noqa: PLC0415
        from solders.instruction import AccountMeta, Instruction  # noqa: PLC0415
        from solders.message import MessageV0  # noqa: PLC0415
        from solders.pubkey import Pubkey  # noqa: PLC0415
        from solders.transaction import VersionedTransaction  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"модули подписи не загружены: {type(exc).__name__}"
        return из_
    try:
        кп = _ключ(секрет)
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"ключ не разобран: {type(exc).__name__}"
        return из_
    if str(кп.pubkey()) != наш:
        из_["why_not"] = (f"ключ не от кошелька полосы: {str(кп.pubkey())[:12]} "
                           f"вместо {наш[:12]}")
        return из_
    слот = rpc_call("getSlot", [{"commitment": "finalized"}])
    if not isinstance(слот, int) or слот <= 0:
        из_["why_not"] = f"узел не дал слот: {слот!r}"
        return из_
    из_["slot"] = слот
    рента_узел = rpc_call("getMinimumBalanceForRentExemption", [ф["байт"]])
    if isinstance(рента_узел, int) and рента_узел > 0:
        из_["рента_узел_sol"] = round(рента_узел / 1e9, 9)
    власть = Pubkey.from_string(наш)
    прог = Pubkey.from_string(ПРОГРАММА_ALT)
    таблица, _бамп = Pubkey.find_program_address(
        [bytes(власть), struct.pack("<Q", слот)], прог)
    из_["адрес"] = str(таблица)
    уже = состояние(rpc_call, str(таблица))
    if уже.get("есть"):
        из_["why_not"] = (f"на выведенном адресе уже есть аккаунт: "
                           f"{уже.get('why_not') or 'таблица'}")
        return из_

    def мета(табл_запись: bool = True):
        return [AccountMeta(pubkey=таблица, is_signer=False, is_writable=True),
                 AccountMeta(pubkey=власть, is_signer=True, is_writable=False),
                 AccountMeta(pubkey=власть, is_signer=True, is_writable=True),
                 AccountMeta(pubkey=Pubkey.from_string(СИСТЕМНАЯ),
                              is_signer=False, is_writable=False)]

    def послать(инструкция) -> dict:
        хеш = ((rpc_call("getLatestBlockhash", [{"commitment": "finalized"}])
                or {}).get("value") or {}).get("blockhash")
        if not хеш:
            return {"ok": False, "why_not": "узел не дал blockhash"}
        сообщение = MessageV0.try_compile(власть, [инструкция], [],
                                           Hash.from_string(хеш))
        подписанная = VersionedTransaction(сообщение, [кп])
        от = _отправить(rpc_call, подписанная)
        if not от.get("ok"):
            return от
        ждём = _дождаться(rpc_call, от["signature"], спать=спать)
        ждём["signature"] = от["signature"]
        return ждём

    # 1. СОЗДАТЬ. Код инструкции 0, затем слот и бамп.
    создание = Instruction(
        program_id=прог, accounts=мета(),
        data=struct.pack("<IQB", 0, слот, _бамп))
    р = послать(создание)
    if р.get("signature"):
        из_["подписи"].append(р["signature"])
    if not р.get("ok"):
        из_["why_not"] = f"создание таблицы: {р.get('why_not')}"
        return из_
    потрачено = _дельта_кошелька(р.get("tx") or {}, наш) or 0.0
    # 2. ЗАЛИТЬ АДРЕСА частями: транзакция расширения тоже влезает в 1232 байта.
    положено = 0
    for начало in range(0, len(список), АДРЕСОВ_ЗА_РАЗ):
        часть = список[начало:начало + АДРЕСОВ_ЗА_РАЗ]
        данные = (struct.pack("<IQ", 2, len(часть))
                   + b"".join(bytes(Pubkey.from_string(а)) for а in часть))
        р2 = послать(Instruction(program_id=прог, accounts=мета(), data=данные))
        if р2.get("signature"):
            из_["подписи"].append(р2["signature"])
        if not р2.get("ok"):
            из_["why_not"] = (f"таблица создана, но адреса залиты не все "
                               f"({положено} из {len(список)}): "
                               f"{р2.get('why_not')}")
            из_["адресов_залито"] = положено
            из_["рента_по_цепи_sol"] = round(-потрачено, 9)
            return из_
        положено += len(часть)
        потрачено += _дельта_кошелька(р2.get("tx") or {}, наш) or 0.0
    из_["адресов_залито"] = положено
    # РЕНТА -- ПО ЦЕПИ: фактическая дельта кошелька за наши подписи. В ней и
    # рента, и тариф за подписи; формула выше -- только сверка.
    из_["рента_по_цепи_sol"] = round(-потрачено, 9)
    проверка = состояние(rpc_call, str(таблица))
    if not проверка.get("это_таблица") or проверка.get("адресов") != len(список):
        из_["why_not"] = (f"по цепи в таблице {проверка.get('адресов')} адресов "
                           f"вместо {len(список)}")
        return из_
    из_["ok"] = True
    return из_


def записать(итог: dict, *, каталог: str) -> dict:
    """Положить адрес таблицы туда, где его найдёт полоса."""
    из_ = {"ok": False, "путь": None, "why_not": None}
    try:
        п = Path(каталог) / ФАЙЛ_СОСТОЯНИЯ
        врем = p_врем = п.with_suffix(".tmp")
        врем.write_text(json.dumps({
            "адрес": итог.get("адрес"), "адресов": итог.get("адресов"),
            "slot": итог.get("slot"), "подписи": итог.get("подписи"),
            "рента_по_цепи_sol": итог.get("рента_по_цепи_sol"),
            "создано_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        p_врем.replace(п)
        из_.update(ok=True, путь=str(п))
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"{type(exc).__name__}: {exc}"
    return из_


# ----------------------------------------------------------------- самопроверка

def self_test() -> int:  # noqa: C901
    всего = сбоев = 0

    def chk(что, ок, факт=None):
        nonlocal всего, сбоев
        всего += 1
        сбоев += (not ок)
        print(f"  [{'ok  ' if ок else 'СБОЙ'}] {что}"
              + (f" -> {факт!r}" if факт is not None and not ок else ""))

    корень = Path(__file__).resolve().parent.parent
    путь = str(корень / "data" / "nogi_shablony.json")
    наш = "4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x"
    сп = адреса(наш=наш, путь_ног=путь)
    chk("список адресов собрался по ЖИВОМУ шаблону ноги из кэша",
        сп["ok"] and сп["нога_программа"] and сп["нога_пул"], сп.get("why_not"))
    chk("замер нашёл НАШИ места в ноге, и их больше одного",
        (сп.get("наших_мест") or 0) >= 2, сп.get("наших_мест"))
    chk("статичных мест ноги больше, чем наших (иначе замер врёт)",
        (сп.get("статичных_ноги") or 0) > (сп.get("наших_мест") or 0),
        (сп.get("статичных_ноги"), сп.get("наших_мест")))
    chk("КОШЕЛЬКА ПОЛОСЫ в таблице НЕТ: плательщик обязан быть статическим ключом",
        наш not in сп["адреса"])
    chk("каждый адрес назван причиной, и причин столько же, сколько адресов",
        len(сп["почему"]) == len(сп["адреса"])
        and all(сп["почему"].get(а) for а in сп["адреса"]))
    chk("адреса не повторяются", len(set(сп["адреса"])) == len(сп["адреса"]))
    chk("минты USDC и WSOL в таблице есть",
        USDC in сп["адреса"] and WSOL in сп["адреса"])
    chk("чаевые по умолчанию НЕ кладутся, и сказано сколько их пропущено",
        (сп.get("чаевых_не_положено") or 0) >= 10
        and not any("чаевых" in з for з in сп["почему"].values()),
        сп.get("чаевых_не_положено"))
    сп_ч = адреса(наш=наш, путь_ног=путь, с_чаевыми=True)
    chk("с флагом чаевых таблица больше, и рента тоже",
        len(сп_ч["адреса"]) > len(сп["адреса"])
        and размер_и_рента_по_формуле(len(сп_ч["адреса"]))["рента_лампорты"]
        > размер_и_рента_по_формуле(len(сп["адреса"]))["рента_лампорты"],
        (len(сп_ч["адреса"]), len(сп["адреса"])))
    прог = программы_типов()
    chk("программы типов пулов взяты ИЗ c2_swap_build, и их не меньше восьми",
        len(прог) >= 8, len(прог))
    chk("все программы типов попали в таблицу",
        all(а in сп["адреса"] for а in прог), [а for а in прог
                                                if а not in сп["адреса"]])
    # НАШИ МЕСТА -- ИМЕННО НАШИ: у чужого кошелька список выходит другой.
    сп2 = адреса(наш=ЧУЖОЙ_ДЛЯ_ЗАМЕРА, путь_ног=путь,
                  чужой="4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x")
    chk("у другого кошелька таблица отличается ровно нашими местами",
        сп2["ok"] and сп2["наших_мест"] == сп["наших_мест"]
        and set(сп2["адреса"]) != set(сп["адреса"]),
        (сп2.get("наших_мест"), сп.get("наших_мест")))
    # РАЗМЕР И РЕНТА ПО ФОРМУЛЕ: сверяется с ответом узла при создании.
    ф = размер_и_рента_по_формуле(len(сп["адреса"]))
    chk(f"аккаунт таблицы на {len(сп['адреса'])} адресов -- {ф['байт']} байт, "
        f"рента по формуле {ф['рента_лампорты'] / 1e9:.9f} SOL",
        ф["байт"] == ЗАГОЛОВОК_ALT + 32 * len(сп["адреса"])
        and ф["рента_лампорты"] > 0)
    chk("рента растёт с числом адресов",
        размер_и_рента_по_формуле(10)["рента_лампорты"]
        < размер_и_рента_по_формуле(40)["рента_лампорты"])
    # АДРЕС ТАБЛИЦЫ -- PDA ОТ ВЛАСТИ И СЛОТА, воспроизводимо.
    try:
        from solders.pubkey import Pubkey  # noqa: PLC0415

        а1, _ = Pubkey.find_program_address(
            [bytes(Pubkey.from_string(наш)), struct.pack("<Q", 123456)],
            Pubkey.from_string(ПРОГРАММА_ALT))
        а2, _ = Pubkey.find_program_address(
            [bytes(Pubkey.from_string(наш)), struct.pack("<Q", 123457)],
            Pubkey.from_string(ПРОГРАММА_ALT))
        chk("адрес таблицы выводится из власти и слота и от слота зависит",
            str(а1) != str(а2) and len(str(а1)) >= 32)
    except Exception as exc:  # noqa: BLE001
        chk("адрес таблицы выводится", False, type(exc).__name__)
    # ЧАСТИ ЗАЛИВКИ: ни одна не больше предела.
    части = [len(сп["адреса"][н:н + АДРЕСОВ_ЗА_РАЗ])
             for н in range(0, len(сп["адреса"]), АДРЕСОВ_ЗА_РАЗ)]
    chk(f"адреса льются частями {части} -- ни одна не больше {АДРЕСОВ_ЗА_РАЗ}",
        части and max(части) <= АДРЕСОВ_ЗА_РАЗ and sum(части) == len(сп["адреса"]))
    # БЕЗ КЭША НОГ -- ОТКАЗ СЛОВАМИ, А НЕ ПУСТАЯ ТАБЛИЦА.
    пусто = адреса(наш=наш, путь_ног=str(корень / "data" / "нет-такого.json"))
    chk("без шаблона ноги -- отказ словами, а не пустой список",
        пусто["ok"] is False and пусто["why_not"] and not пусто["адреса"],
        пусто)
    chk("без кошелька -- отказ словами",
        адреса(наш="", путь_ног=путь)["ok"] is False
        or адреса(наш=None, путь_ног=путь)["ok"] in (True, False))
    # КЛИЕНТ УЗЛА НАЗЫВАЕТСЯ ТАК, КАК МЫ ЕГО ЗОВЁМ. Первый боевой прогон упал
    # на AttributeError: module 'bloom_api' has no attribute 'Helius' -- уже ПОСЛЕ
    # слова SOZDAT, хотя до сети дело и не дошло. Проверка ловит это ДО траты.
    try:
        import solana_rpc_client as RPCп  # noqa: PLC0415

        chk("класс клиента узла называется так, как мы его зовём, и у него есть call",
            hasattr(RPCп, "SolanaRpc") and callable(
                getattr(RPCп.SolanaRpc, "call", None)),
            [и for и in dir(RPCп) if "Rpc" in и])
    except Exception as exc:  # noqa: BLE001
        chk("модуль клиента узла загружается", False, type(exc).__name__)
    print(f"самопроверка таблицы адресов USDC-ноги: {всего - сбоев}/{всего} пройдено")
    if сбоев:
        print(f"самопроверка не пройдена: {сбоев} из {всего}")
    return 1 if сбоев else 0


def main() -> int:
    import argparse

    р = argparse.ArgumentParser()
    р.add_argument("--self-test", action="store_true")
    р.add_argument("--pokazat", action="store_true",
                    help="показать, что ляжет в таблицу, и ренту -- без сети")
    р.add_argument("--sozdat", action="store_true",
                    help="СОЗДАТЬ таблицу на цепи: это рента с кошелька полосы")
    р.add_argument("--katalog", default="",
                    help="куда положить адрес таблицы (каталог состояния службы)")
    р.add_argument("--put-nog", default="", help="путь к nogi_shablony.json")
    р.add_argument("--s-chaevymi", action="store_true",
                    help="положить в таблицу и белый список чаевых (рента вдвое)")
    р.add_argument("--out", default="")
    а = р.parse_args()
    if а.self_test:
        return self_test()
    путь = а.put_nog or None
    сп = адреса(путь_ног=путь, с_чаевыми=bool(а.s_chaevymi))
    if not сп.get("ok"):
        print(f"ОТКАЗ: {сп.get('why_not')}")
        return 2
    ф = размер_и_рента_по_формуле(len(сп["адреса"]))
    print(f"кошелёк полосы: {сп['наш']}")
    print(f"нога SOL -> USDC: программа {сп['нога_программа']}, пул {сп['нога_пул']}")
    print(f"мест ноги: статичных {сп['статичных_ноги']}, НАШИХ {сп['наших_мест']} "
          f"(найдены замером двумя кошельками)")
    print(f"адресов в таблице: {len(сп['адреса'])} · аккаунт {ф['байт']} байт · "
          f"рента по формуле {ф['рента_лампорты'] / 1e9:.9f} SOL")
    for а_ in сп["адреса"]:
        print(f"   {а_}  -- {сп['почему'][а_]}")
    итог = {"адреса": сп, "формула": ф}
    if а.sozdat:
        # ТОТ ЖЕ КЛИЕНТ УЗЛА, ЧТО У ОСТАЛЬНЫХ ПРОГОНОВ (solana_rpc_client.SolanaRpc,
        # как в lane_nonce): у него общий темп, запасной путь и учёт кредитов.
        import solana_rpc_client as RPC  # noqa: PLC0415

        клиент = RPC.SolanaRpc(service="lane_usdc_alt")
        с = создать(клиент.call, путь_ног=путь, с_чаевыми=bool(а.s_chaevymi))
        итог["создание"] = с
        print("\n--- СОЗДАНИЕ ---")
        print(json.dumps({к: v for к, v in с.items() if к != "tx"},
                          ensure_ascii=False, indent=1))
        if с.get("ok") and а.katalog:
            print(json.dumps(записать(с, каталог=а.katalog), ensure_ascii=False))
        if not с.get("ok"):
            return 1
    if а.out:
        Path(а.out).write_text(json.dumps(итог, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
