#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ПРОДАЖА ЧЕРЕЗ JUPITER С ЗАКРЫТИЕМ ТОКЕН-СЧЁТА В ТОЙ ЖЕ ТРАНЗАКЦИИ.

ЗАЧЕМ. После продажи токен-счёт остаётся пустым, и в нём лежит наша рента --
около 0.002 SOL за счёт. У кошелька полосы их накопилось ЧЕТЫРЕСТА ЧЕТЫРЕ
(снимок data/bloom_token_accounts.json: 404 счёта, 0.611 SOL ренты, 401 из них
пуст). Уборка отдельной транзакцией стоит ещё одну комиссию и ещё одну
отправку; закрытие В ТОЙ ЖЕ транзакции не стоит ничего, если влезает в размер.

ЧТО ЗДЕСЬ ЕСТЬ. Сборка списка инструкций из ответа Jupiter
(/swap/v1/swap-instructions) с добавленным closeAccount НАШЕГО токен-счёта, и
ЧЕСТНЫЙ размер пакета -- с таблицами адресов, с nonce и с чаевыми. Не влезает
-- говорится числом и словами, ДО отправки.

ЧЕГО ЗДЕСЬ НЕТ. Ни сети, ни подписи, ни отправки: ответ Jupiter приходит
разобранным словарём. Собранный список инструкций отдаётся наружу -- подписывает
и шлёт тот, у кого ключ.

ДВА МЕСТА, ГДЕ ЛЕГКО ПОТЕРЯТЬ ДЕНЬГИ, И ОБА ЗАКРЫТЫ ОТКАЗОМ:
  * ЗАКРЫТЬ СЧЁТ С ОСТАТКОМ -- значит сжечь токены (closeAccount требует
    нулевого остатка, иначе инструкция упадёт и ВСЯ транзакция откатится, то
    есть продажа не состоится). Поэтому закрытие ставится ТОЛЬКО когда продаётся
    ВЕСЬ остаток счёта;
  * ЗАКРЫТЬ НЕ ТОТ СЧЁТ: cleanupInstruction Jupiter закрывает WSOL, а наш
    токен-счёт -- другой. Адрес закрываемого счёта проверяется по той же
    инструкции свопа, где он стоит источником.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent

ПРОГРАММА_ТОКЕНА = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ПРОГРАММА_ТОКЕНА_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ПРОГРАММА_JUPITER = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
СИСТЕМНАЯ = "11111111111111111111111111111111"
ПРЕДЕЛ_РАЗМЕРА_TX = 1232
# closeAccount у программы токена -- номер 9, один байт данных. Счета: сам
# счёт, получатель ренты, владелец.
CLOSE_ACCOUNT_КОД = 9

WHY_НЕТ_СВОПА = "в ответе Jupiter нет swapInstruction"
WHY_НЕ_ВЕСЬ = ("продаётся не весь остаток счёта -- закрывать его нельзя: "
               "closeAccount на счёте с остатком роняет ВСЮ транзакцию")
WHY_НЕ_ВЛЕЗ = "пакет не влезает в размер сети"
WHY_ЧУЖОЙ_СЧЁТ = "закрываемый счёт не наш и не участвует в свопе"


def _cu16(n: int) -> int:
    return 1 if n < 0x80 else (2 if n < 0x4000 else 3)


def razmer_tx(*, podpisej: int, staticheskih_kljuchej: int, instrukcii: list,
              tablicy: list | None = None) -> int:
    """Размер версии 0 байтами -- со СТАТИЧЕСКИМИ ключами и таблицами адресов.

    instrukcii -- список (счетов, байт данных);
    tablicy -- список (адресов_на_запись, адресов_на_чтение) по таблицам.

    СВЕРЕНО С НАСТОЯЩЕЙ СЕРИАЛИЗАЦИЕЙ (solders, 05.10): четыре формы без
    таблиц и одна с таблицей -- расхождение НОЛЬ байт.
    """
    р = _cu16(podpisej) + 64 * int(podpisej)
    р += 1 + 3                                   # версия v0 + заголовок
    р += _cu16(staticheskih_kljuchej) + 32 * int(staticheskih_kljuchej)
    р += 32                                      # blockhash
    р += _cu16(len(instrukcii))
    for счетов, данных in instrukcii:
        р += 1 + _cu16(счетов) + int(счетов) + _cu16(данных) + int(данных)
    т = list(tablicy or [])
    р += _cu16(len(т))
    for на_запись, на_чтение in т:
        р += 32 + _cu16(на_запись) + int(на_запись) + _cu16(на_чтение) + int(на_чтение)
    return р


def _bajt_dannyh(их: dict) -> int:
    д = (их or {}).get("data")
    if not д:
        return 0
    try:
        return len(base64.b64decode(д, validate=False))
    except (ValueError, TypeError):
        return len(str(д))


def _schetov(их: dict) -> int:
    return len((их or {}).get("accounts") or [])


def instrukcija_zakrytija(*, schjot: str, vladelec: str,
                          poluchatel_renty: str | None = None,
                          programma_tokena: str = ПРОГРАММА_ТОКЕНА) -> dict:
    """closeAccount нашего токен-счёта -- тем же видом, что и у Jupiter."""
    return {"programId": programma_tokena,
            "accounts": [
                {"pubkey": schjot, "isSigner": False, "isWritable": True},
                {"pubkey": poluchatel_renty or vladelec, "isSigner": False,
                 "isWritable": True},
                {"pubkey": vladelec, "isSigner": True, "isWritable": False}],
            "data": base64.b64encode(bytes([CLOSE_ACCOUNT_КОД])).decode()}


def sobrat(otvet: dict, *, nash_koshelek: str, token_schjot: str,
           prodajom_raw: int, ostatok_scheta_raw: int,
           programma_tokena: str = ПРОГРАММА_ТОКЕНА,
           s_nonce: bool = True, s_chaevymi: bool = True,
           nonce_schetov: int = 3, chaevyh_schetov: int = 2) -> dict:
    """СОБРАТЬ ПОРЯДОК ИНСТРУКЦИЙ И СКАЗАТЬ РАЗМЕР. Ничего не подписывает.

    Порядок -- тот, в котором его ждёт сеть и которым пользуется Jupiter:
    nonce (если он), ComputeBudget, setup, своп, cleanup Jupiter (WSOL), НАШЕ
    закрытие токен-счёта, чаевые.
    """
    из_ = {"ok": False, "why_not": None, "instrukcii": [], "zakrytije": None,
            "razmer": None, "vlezet": None, "zapas": None,
            "tablic": 0, "pochemu_bez_zakrytija": None}
    своп = (otvet or {}).get("swapInstruction")
    if not isinstance(своп, dict) or not своп.get("accounts"):
        из_["why_not"] = WHY_НЕТ_СВОПА
        return из_
    порядок: list = []
    if s_nonce:
        порядок.append({"imja": "nonce", "schetov": nonce_schetov, "bajt": 4})
    for их in (otvet.get("computeBudgetInstructions") or []):
        порядок.append({"imja": "computeBudget", "schetov": _schetov(их),
                        "bajt": _bajt_dannyh(их)})
    for их in (otvet.get("setupInstructions") or []):
        порядок.append({"imja": "setup", "schetov": _schetov(их),
                        "bajt": _bajt_dannyh(их)})
    порядок.append({"imja": "swap", "schetov": _schetov(своп),
                    "bajt": _bajt_dannyh(своп)})
    чистка = otvet.get("cleanupInstruction")
    if isinstance(чистка, dict) and чистка.get("accounts"):
        порядок.append({"imja": "cleanup_wsol", "schetov": _schetov(чистка),
                        "bajt": _bajt_dannyh(чистка)})
    # --- НАШЕ ЗАКРЫТИЕ: только если продаём ВЕСЬ остаток и счёт действительно наш.
    закрывать = True
    if int(prodajom_raw) != int(ostatok_scheta_raw):
        закрывать = False
        из_["pochemu_bez_zakrytija"] = (
            f"{WHY_НЕ_ВЕСЬ}: продаём {prodajom_raw} из {ostatok_scheta_raw}")
    else:
        участники = {str(а.get("pubkey")) for а in (своп.get("accounts") or [])
                     if isinstance(а, dict)}
        if token_schjot not in участники:
            закрывать = False
            из_["pochemu_bez_zakrytija"] = (
                f"{WHY_ЧУЖОЙ_СЧЁТ}: {str(token_schjot)[:8]} не стоит в свопе")
    if закрывать:
        зак = instrukcija_zakrytija(schjot=token_schjot, vladelec=nash_koshelek,
                                    programma_tokena=programma_tokena)
        из_["zakrytije"] = зак
        порядок.append({"imja": "close_token", "schetov": 3, "bajt": 1})
    if s_chaevymi:
        порядок.append({"imja": "chaevye", "schetov": chaevyh_schetov,
                        "bajt": 12})
    из_["instrukcii"] = порядок
    # --- РАЗМЕР. Статические ключи -- те, что не уехали в таблицы: считаем по
    # уникальным адресам свопа и служебных инструкций, за вычетом тех, что
    # Jupiter положил в таблицы (их число он сам и отдаёт).
    таблицы = list(otvet.get("addressLookupTableAddresses") or [])
    из_["tablic"] = len(таблицы)
    статических = _staticheskih_kljuchej(otvet, nash_koshelek=nash_koshelek,
                                         s_nonce=s_nonce, s_chaevymi=s_chaevymi,
                                         zakrytije=из_["zakrytije"])
    из_["staticheskih_kljuchej"] = статических
    # Распределение адресов таблицы между «на запись» и «на чтение» в ответе
    # Jupiter не приходит; берётся ХУДШИЙ случай (все на запись), потому что он
    # длиннее на один байт за адрес и ошибаться надо в сторону отказа.
    в_таблицах = max(0, _schetov(своп) - 8)
    пары = [( (в_таблицах // max(1, len(таблицы))) + 1, 0) for _ in таблицы]
    из_["razmer"] = razmer_tx(podpisej=1, staticheskih_kljuchej=статических,
                              instrukcii=[(и["schetov"], и["bajt"]) for и in порядок],
                              tablicy=пары)
    из_["vlezet"] = из_["razmer"] <= ПРЕДЕЛ_РАЗМЕРА_TX
    из_["zapas"] = ПРЕДЕЛ_РАЗМЕРА_TX - из_["razmer"]
    из_["ok"] = bool(из_["vlezet"])
    if not из_["vlezet"]:
        из_["why_not"] = (f"{WHY_НЕ_ВЛЕЗ}: {из_['razmer']} байт при пределе "
                          f"{ПРЕДЕЛ_РАЗМЕРА_TX}")
    return из_


def _staticheskih_kljuchej(otvet: dict, *, nash_koshelek: str, s_nonce: bool,
                           s_chaevymi: bool, zakrytije: dict | None) -> int:
    """Сколько РАЗНЫХ адресов останется в самом сообщении, а не в таблицах."""
    адреса = {nash_koshelek, ПРОГРАММА_JUPITER}
    for ключ in ("computeBudgetInstructions", "setupInstructions",
                 "otherInstructions"):
        for их in (otvet.get(ключ) or []):
            адреса.add(str(их.get("programId")))
            for а in (их.get("accounts") or []):
                адреса.add(str(а.get("pubkey")))
    для_свопа = (otvet.get("swapInstruction") or {})
    адреса.add(str(для_свопа.get("programId")))
    # ЕСЛИ ТАБЛИЦ НЕТ -- СЧЕТА СВОПА СТОЯТ В САМОМ СООБЩЕНИИ, И ЭТО ГЛАВНЫЙ
    # ВЕС ПАКЕТА: 32 байта за адрес. Jupiter отдаёт маршруты и без таблиц, и
    # ровно на них продажа с закрытием перестаёт влезать. Считать их «как бы в
    # таблице» значило бы показать пакет короче, чем он есть, -- то есть
    # отправить и получить отказ сети вместо честного отказа до отправки.
    if not (otvet.get("addressLookupTableAddresses") or []):
        for а in (для_свопа.get("accounts") or []):
            адреса.add(str(а.get("pubkey")))
    чистка = otvet.get("cleanupInstruction") or {}
    for а in (чистка.get("accounts") or []):
        адреса.add(str(а.get("pubkey")))
    if чистка:
        адреса.add(str(чистка.get("programId")))
    if zakrytije:
        адреса.add(str(zakrytije.get("programId")))
        for а in (zakrytije.get("accounts") or []):
            адреса.add(str(а.get("pubkey")))
    if s_nonce:
        адреса.add(СИСТЕМНАЯ)
    if s_chaevymi:
        адреса.add(СИСТЕМНАЯ)
    адреса.discard("None")
    адреса.discard("")
    return len(адреса)


# ------------------------------------------------------------- самопроверка

ZHDEM_PROVEROK = 13
# ФОРМА ОТВЕТА JUPITER -- НЕ ПРИДУМАНА: записана живым зондом в репозитории,
# data/jup_buy_probe.json (lite-api, 200): computeBudgetInstructions 2,
# setupInstructions 4, swapInstruction с 20 счетами, cleanupInstruction есть,
# addressLookupTableAddresses 1. По этой форме и собран образец ниже.
ФАЙЛ_ЗОНДА = "jup_buy_probe.json"
ЖДЁМ_ЗОНДА = {"compute": 2, "setup": 4, "svop_schetov": 20, "tablic": 1}
# ВЕКТОРЫ РАЗМЕРА -- ИЗМЕРЕНЫ НАСТОЯЩЕЙ СЕРИАЛИЗАЦИЕЙ (solders, 05.10),
# расхождение ноль байт; последний -- С ТАБЛИЦЕЙ АДРЕСОВ.
ВЕКТОРЫ_РАЗМЕРА = (
    (1, 10, [(8, 24)], None, 459),
    (1, 11, [(8, 24), (8, 24)], None, 526),
    (1, 13, [(8, 32)] * 4, None, 692),
    (1, 5, [(9, 24)], [(4, 2)], 340),
)


def _obrazec_otveta(*, svop_schetov: int = 20, setup: int = 4,
                    tablic: int = 1, nash: str = "Nash1111111111111111111111",
                    token_schjot: str = "AtaTokena11111111111111111") -> dict:
    """Ответ Jupiter ровно той формы, что записана зондом. Без сети."""
    def их(прог, n, байт):
        return {"programId": прог,
                "accounts": [{"pubkey": f"A{и:025d}", "isSigner": False,
                              "isWritable": True} for и in range(n)],
                "data": base64.b64encode(bytes(байт)).decode()}
    своп = их(ПРОГРАММА_JUPITER, svop_schetov, 40)
    своп["accounts"][1] = {"pubkey": nash, "isSigner": True, "isWritable": True}
    своп["accounts"][2] = {"pubkey": token_schjot, "isSigner": False,
                            "isWritable": True}
    return {
        "computeBudgetInstructions": [их("ComputeBudget111111111111111111111111111111", 0, 5),
                                      их("ComputeBudget111111111111111111111111111111", 0, 9)],
        "setupInstructions": [их("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL", 6, 1)
                              for _ in range(setup)],
        "swapInstruction": своп,
        "cleanupInstruction": их(ПРОГРАММА_ТОКЕНА, 3, 1),
        "addressLookupTableAddresses": [f"T{и:031d}" for и in range(tablic)],
    }


def self_test() -> int:  # noqa: C901, PLR0915
    было, плохо = 0, 0
    упавшие: list = []

    def chk(имя, усл, факт=None):
        nonlocal было, плохо
        было += 1
        if усл:
            print(f"  ok   {имя}")
        else:
            плохо += 1
            упавшие.append(имя)
            print(f" ПЛОХО {имя} -- {факт!r}")

    print("c3_jupiter_prodazha_zakrytie: самопроверка")
    # ------------------------------------------------ РАЗМЕР
    плохие = []
    for п, к, инстр, табл, ждём in ВЕКТОРЫ_РАЗМЕРА:
        р = razmer_tx(podpisej=п, staticheskih_kljuchej=к, instrukcii=инстр,
                      tablicy=табл)
        if р != ждём:
            плохие.append((к, р, ждём))
    chk("размер пакета сверен с НАСТОЯЩЕЙ сериализацией на четырёх формах, "
        "включая форму С ТАБЛИЦЕЙ АДРЕСОВ -- расхождение ноль байт", not плохие,
        плохие)
    # ------------------------------------------------ ФОРМА ОТВЕТА JUPITER
    п_з = КОРЕНЬ / "data" / ФАЙЛ_ЗОНДА
    форма = {}
    if п_з.exists():
        д = json.loads(п_з.read_text(encoding="utf-8"))
        for база, тело in (д.get("базы") or {}).items():  # noqa: B007
            поля = ((тело or {}).get("swap_instructions") or {}).get("поля") or {}
            if поля:
                форма = поля
                break
    chk(f"живой зонд ответа Jupiter на месте: data/{ФАЙЛ_ЗОНДА} -- форма "
        "ответа взята оттуда, а не придумана",
        bool(форма) and "swapInstruction" in форма, list(форма)[:5])
    if форма:
        chk(f"в зонде ровно то, на что рассчитан сборщик: компьют "
            f"{ЖДЁМ_ЗОНДА['compute']}, setup {ЖДЁМ_ЗОНДА['setup']}, у свопа "
            f"{ЖДЁМ_ЗОНДА['svop_schetov']} счетов, cleanup есть",
            "...всего 2" in str(форма.get("computeBudgetInstructions"))
            and "...всего 4" in str(форма.get("setupInstructions"))
            and "...всего 20" in str(форма.get("swapInstruction"))
            and форма.get("cleanupInstruction") is not None, None)
    # ------------------------------------------------ СБОРКА
    НАШ, АТА = "Nash1111111111111111111111", "AtaTokena11111111111111111"
    отв = _obrazec_otveta(nash=НАШ, token_schjot=АТА)
    с = sobrat(отв, nash_koshelek=НАШ, token_schjot=АТА,
               prodajom_raw=1000, ostatok_scheta_raw=1000)
    имена = [и["imja"] for и in с["instrukcii"]]
    chk("порядок инструкций тот, что ждёт сеть: nonce, ComputeBudget, setup, "
        "своп, cleanup WSOL Jupiter, НАШЕ закрытие токен-счёта, чаевые",
        имена == ["nonce", "computeBudget", "computeBudget", "setup", "setup",
                  "setup", "setup", "swap", "cleanup_wsol", "close_token",
                  "chaevye"], имена)
    chk("закрытие собрано НА НАШ токен-счёт, а не на WSOL Jupiter, и владелец "
        "в нём подписант",
        с["zakrytije"]["accounts"][0]["pubkey"] == АТА
        and с["zakrytije"]["accounts"][2]["pubkey"] == НАШ
        and с["zakrytije"]["accounts"][2]["isSigner"] is True,
        с["zakrytije"])
    chk("данные закрытия -- один байт с кодом 9 (closeAccount), а не угаданная "
        "строка",
        base64.b64decode(с["zakrytije"]["data"]) == bytes([CLOSE_ACCOUNT_КОД]),
        с["zakrytije"]["data"])
    chk(f"пакет влезает в {ПРЕДЕЛ_РАЗМЕРА_TX}: {с['razmer']} байт, запас "
        f"{с['zapas']}", с["vlezet"] and с["ok"], с)
    # ------------------------------------------------ ДОКАЗАННЫЕ КРАСНЫЕ
    часть = sobrat(отв, nash_koshelek=НАШ, token_schjot=АТА,
                   prodajom_raw=900, ostatok_scheta_raw=1000)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: продаём НЕ ВЕСЬ остаток -- закрытия НЕТ, и причина "
        "названа; closeAccount на счёте с остатком уронил бы всю продажу",
        часть["zakrytije"] is None
        and WHY_НЕ_ВЕСЬ in (часть["pochemu_bez_zakrytija"] or ""),
        часть.get("pochemu_bez_zakrytija"))
    чужой = sobrat(отв, nash_koshelek=НАШ, token_schjot="ChuzhojSchjot111111111111",
                   prodajom_raw=1000, ostatok_scheta_raw=1000)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: счёт, которого нет в свопе, не закрывается -- "
        "закрыть не тот счёт значит отдать чужую ренту и уронить транзакцию",
        чужой["zakrytije"] is None
        and WHY_ЧУЖОЙ_СЧЁТ in (чужой["pochemu_bez_zakrytija"] or ""),
        чужой.get("pochemu_bez_zakrytija"))
    пусто = sobrat({}, nash_koshelek=НАШ, token_schjot=АТА, prodajom_raw=1,
                   ostatok_scheta_raw=1)
    chk("пустой ответ Jupiter -- отказ по имени, а не сборка из ничего",
        not пусто["ok"] and пусто["why_not"] == WHY_НЕТ_СВОПА, пусто)
    # ------------------------------------------------ ГДЕ НЕ ВЛЕЗАЕТ
    тяжёлый = _obrazec_otveta(svop_schetov=52, setup=8, tablic=0,
                              nash=НАШ, token_schjot=АТА)
    т = sobrat(тяжёлый, nash_koshelek=НАШ, token_schjot=АТА,
               prodajom_raw=1000, ostatok_scheta_raw=1000)
    chk("на тяжёлом маршруте БЕЗ ТАБЛИЦ АДРЕСОВ (52 счёта свопа, 8 setup) "
        "пакет НЕ "
        f"влезает -- и это сказано числом ДО отправки: {т['razmer']} байт",
        not т["vlezet"] and WHY_НЕ_ВЛЕЗ in (т["why_not"] or "")
        and т["zapas"] < 0, т["razmer"])
    без_чаевых = sobrat(тяжёлый, nash_koshelek=НАШ, token_schjot=АТА,
                        prodajom_raw=1000, ostatok_scheta_raw=1000,
                        s_chaevymi=False, s_nonce=False)
    chk("и видно, ЧЕМ платить за место: без nonce и чаевых тот же маршрут "
        f"короче на {т['razmer'] - без_чаевых['razmer']} байт",
        без_чаевых["razmer"] < т["razmer"],
        (т["razmer"], без_чаевых["razmer"]))
    chk("закрытие стоит ровно столько, сколько стоит: три счёта и один байт",
        (lambda б, с_: с_["razmer"] - б["razmer"] > 0)(
            sobrat(отв, nash_koshelek=НАШ, token_schjot=АТА, prodajom_raw=900,
                   ostatok_scheta_raw=1000), с), None)

    if упавшие:
        print("УПАЛИ: " + "; ".join(упавшие))
    print(f"\nпроверок {было}, ждали {ZHDEM_PROVEROK}, не прошло {плохо}")
    if было != ZHDEM_PROVEROK:
        print(" ПЛОХО число проверок разошлось с объявленным")
        return 1
    return 1 if плохо else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--otvet", default=None,
                   help="файл с ответом Jupiter swap-instructions (json)")
    p.add_argument("--koshelek", default=None)
    p.add_argument("--schjot", default=None, help="наш токен-счёт")
    p.add_argument("--prodajom", type=int, default=0)
    p.add_argument("--ostatok", type=int, default=0)
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test:
        return self_test()
    if a.otvet and a.koshelek and a.schjot:
        отв = json.loads(Path(a.otvet).read_text(encoding="utf-8"))
        из_ = sobrat(отв, nash_koshelek=a.koshelek, token_schjot=a.schjot,
                     prodajom_raw=a.prodajom, ostatok_scheta_raw=a.ostatok)
        print(json.dumps(из_, ensure_ascii=False, indent=1))
        return 0 if из_["ok"] else 2
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
