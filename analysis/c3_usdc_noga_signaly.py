#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""USDC-НОГА ДЛЯ ТИПОВ, КУДА ИДУТ СИГНАЛЫ: Raydium CPMM, Raydium LaunchLab,
Pump AMM и кривая pump.fun v2 -- вход USDC вместо WSOL. Тот же флаг, та же тень.

ЗАЧЕМ. Счёт Code-1 за сутки: 118 сигналов с котировкой USDC, и лежат они НЕ там,
где закрыта USDC-нога. CPMM 44, LaunchLab 38, Pump AMM 25, кривая pump.fun 7 --
114 из 118; у закрытых сейчас типов (CLMM, DLMM, DAMM v2, Whirlpool) их 4.
Модуль analysis/c2_usdc_noga.py сделан для тех шести типов и про эти четыре не
знает вовсе: его таблица TYPES их не содержит, и storona() отвечает «тип пула не
в таблице USDC-ноги». Двухшаговый путь (bloom_lane_two_step) вторую ногу этих
типов СОБРАТЬ умеет -- кирпичами c2_swap_build, -- но у него нет НАШЕЙ таблицы
адресов, а без неё две ноги в 1232 байта не влезают (замер: 1326...1703 байта).
Поэтому здесь -- те же четыре шага, что у c2_usdc_noga, но для этих типов и с
нашей таблицей.

ЧУЖОГО КОДА НЕ ТРОНУТО. c2_usdc_noga.py правил Code-1 (четыре коммита 01--02.10),
c2_swap_build.py и bloom_lane_two_step.py -- общие модули полосы. Поэтому связка
сумм, порядок инструкций, предел CU, вид nonce и чаевые берутся ОТТУДА вызовами, а
не копируются: summy_nog -- c2_usdc_noga, запас первого шага и предел размера --
bloom_lane_two_step, инструкция ноги -- c2_swap_build. Самопроверка сверяет порядок
инструкций с порядком c2_usdc_noga ПОЗИЦИЯ В ПОЗИЦИЮ: если он там поменяется,
здесь это упадёт, а не разойдётся молча.

ЧТО СДЕЛАНО ПО КАЖДОМУ ТИПУ -- ЗАМЕРОМ, НЕ ПАМЯТЬЮ (числа в самопроверке):

  * Raydium CPMM. Вторая нога -- swap_base_input, 13 счетов. Байт в байт на 22
    живых сделках с КОТИРОВОЧНЫМ ТОКЕНОМ. Сделка источника может быть и ПРОДАЖЕЙ:
    у CPMM слоты стоят в порядке вход/выход, и стороны переставляются готовой
    функцией полосы (bloom_lane_two_step.перевернуть_cpmm) -- тогда нога
    собирается, а цена ОТКАЗЫВАЕТ по имени (цену покупки из обратного свопа брать
    нельзя). Отдельным отказом названо поле f > 1: в хранилище CPMM лежат не
    снятые protocol_fees/fund_fees, и у части сделок котировщик покупки обещает
    больше, чем даёт кривая по остаткам.
  * Raydium LaunchLab. buy_exact_in, 18 счетов. Байт в байт на 31 живой сделке с
    котировочным токеном. Цена -- c2_swap_build.launchlab_min_out: виртуальные
    резервы из события TradeEvent, они в единицах КОТИРОВКИ какой бы она ни была,
    нуль чтений.
  * Pump AMM. buy_exact_quote_in, 25 или 26 счетов. Байт в байт на 2 живых
    сделках с котировочным токеном (больше их в образцах нет). Цена -- по
    остаткам хранилищ, нуль чтений.
  * Кривая pump.fun v2. buy_exact_quote_in (дискриминатор c2ab1c46684d5b2f, 27
    счетов) -- та разновидность, у которой котировка ОТДЕЛЬНЫМ МИНТОМ
    (Global.whitelisted_quote_mints). 18-счётная v1 -- отказ по имени: там
    котировка нативный SOL и USDC-ноге делать нечего. Цена -- зеркало продажи
    (c3_kotirovka_prodazhi.вход_кривой) по КОТИРОВОЧНЫМ резервам события.
    Места 14, 15, 20 и 21 выводятся без чтений и без состояния: ATA базы, ATA
    котировки, накопитель объёма (PDA user_volume_accumulator + наш кошелёк) и
    СВЯЗАННЫЙ накопитель -- ATA этого накопителя по минту котировки. Последнее
    важно: в c2_swap_build про место 21 написано «ключ не выводится семенами,
    берётся из состояния», и для USDC такого состояния у нас не будет НИКОГДА
    (файл заполняется ошибками цепи). Замер на живой покупке
    3bn6wPKuLm62t7nqfK8u9iABz8zpAQQhLhznZiPSsRN8 (котировка HiMSSzzwkZ...)
    показал: место 21 РОВНО равно ATA(место 20, минт котировки, программа
    котировки). Поэтому здесь нога собирается своими местами, а Code-1 получает
    это пятью строками (см. docs/usdc_noga_signaly_kak_vstroit.md).

ОБРАЗЦОВ С КОТИРОВКОЙ РОВНО USDC ПО ЭТИМ ЧЕТЫРЁМ ТИПАМ В РЕПОЗИТОРИИ НЕТ, и это
сказано числом, а не обойдено. В сборе Code-2
(data/podbivka/usdc_noga_dlya_code3.json, 22 сигнала) их нет по построению: он
собирал «USDC в пулах НЕ CPMM и НЕ Pump AMM». В его же источнике
(data/podbivka/kotirovki_grupp_2026-10-01.json, 1090 сигналов) с котировкой USDC
лежат pump-amm 3 и raydium-cpmm 1, LaunchLab и кривой -- ноль. Поэтому механизм
проверен на живых сделках с КОТИРОВОЧНЫМ ТОКЕНОМ (не WSOL): это ТОТ ЖЕ путь кода
-- USDC в нём лишь значение минта, -- и ровно те места, что от минта котировки
зависят (наш ATA котировки, связанный накопитель), на них и проверены. ЧТО
ЗАПРОШЕНО У CODE-2: по 6+ живых USDC-свопов на каждый из четырёх типов
(buy с котировкой USDC, tx_jsonParsed + tx_base64), как он отдавал продажи в
data/samples/prodazhi/.

РАЗМЕР ПАКЕТА -- С НАШЕЙ ТАБЛИЦЕЙ АДРЕСОВ, И ЧИСЛО ЗДЕСЬ ВЕРХНЕЕ. Таблица
B1LxDr9ib1ezCSebbxnF73hCSAsVsy23U5fEwrqkfzXY создана Code-1 и сверена по цепи (34
адреса, data/usdc_noga_alt.json); в ней лежат программы ВСЕХ четырёх типов, минт
USDC, наш ATA USDC и все статичные счета первой ноги. Замер идёт без сети:
таблица подаётся замеру из файла репозитория. Образцы несут чужой котировочный
токен, и его минт, наш ATA под него и программа токена в таблице не лежат --
значит на USDC-котировке пакет будет МЕНЬШЕ измеренного, а не больше.

CU НЕ ВЫДУМАН И НЕ ИЗМЕРЕН ЗДЕСЬ. simulateTransaction -- не мой инструмент
(слово владельца), а у живых образцов в CU входит весь чужой маршрут (у этих
четырёх типов чистых сделок «только своп» в образцах нет вовсе: 88 869...541 083
CU на пакет источника). Поэтому предел берётся тот же, что у USDC-ноги:
c2_usdc_noga.cu_nogi() -- умолчание 250 000, переменная BLOOM_USDC_NOGA_CU, замер
Code-1 по цепи 158 733...172 545 CU на ДВЕ ноги. Точное число по этим четырём
типам даёт его прогон deploy/checks/usdc_noga_razmer_cu.py на хосте, когда придут
USDC-образцы.

ПРОДАЖА -- ТОКЕН -> USDC -> SOL, ВОСЕМЬ ИНСТРУКЦИЙ, ЗЕРКАЛО ПОКУПКИ. Первая нога
-- зеркало НАШЕЙ покупки токена (c3_prodavec_sborka), вторая -- зеркало НАШЕЙ
покупки первой ноги (та же покупка без мест 19 и 20). Вход второй ноги -- РОВНО
минимум первой. Минимум второй ноги только переданный: живые резервы пула
SOL/USDC этот модуль не читает. Размер с нашей таблицей: CPMM 1021...1052,
LaunchLab 1130...1161, Pump AMM 1128...1159 -- влезает; кривая 1352 -- НЕ
влезает, и выбор назван числами (десять адресов в таблице -> 1166, либо не
закрывать токеновый счёт -> 1189). Чего нет: живой 23-счётной продажи Pump AMM в
образцах (раскладка снята с нашей 24-счётной), и у Code-2 запрошено 6+ таких.

ЧЕГО ЗДЕСЬ НЕТ. Ни одной отправки, ни одной симуляции, ни одного чтения сети на
горячем пути, ни одной правки чужого файла. Врезка в полосу и круг 0.01 -- Code-1.
"""
from __future__ import annotations

import json
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"

ROUTE = "usdc_noga_signaly"

PROG_CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
PROG_LAUNCHLAB = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
PROG_PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PROG_KRIVAYA = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# СПОСОБ КОТИРОВКИ -- ПО ФАКТУ ТОГО, ЧЕМ СЧИТАЕТСЯ ЦЕНА, а не по типу пула:
#   rezervy   -- остатки хранилищ после сделки источника (x*y=k), нуль чтений;
#   launchlab -- виртуальные резервы события TradeEvent, нуль чтений;
#   krivaya_v2 -- кривая pump.fun по КОТИРОВОЧНЫМ резервам события, нуль чтений.
SPOSOB_REZERVY = "rezervy"
SPOSOB_LAUNCHLAB = "launchlab"
SPOSOB_KRIVAYA_V2 = "krivaya_v2"

# ЧИСЛА СИГНАЛОВ -- СЧЁТ CODE-1 ЗА СУТКИ (слово владельца 03.10). Они здесь не
# для арифметики, а чтобы порядок типов в таблице совпадал с порядком денег.
SIGNALOV_V_SUTKI = {PROG_CPMM: 44, PROG_LAUNCHLAB: 38, PROG_PUMP_AMM: 25,
                    PROG_KRIVAYA: 7}

TIPY = {
    PROG_CPMM: {"label": "Raydium CPMM", "ix": "swap_base_input",
                "kotirovshchik": SPOSOB_REZERVY, "perevorot": True},
    PROG_LAUNCHLAB: {"label": "Raydium LaunchLab", "ix": "buy_exact_in",
                     "kotirovshchik": SPOSOB_LAUNCHLAB, "perevorot": False},
    PROG_PUMP_AMM: {"label": "Pump AMM", "ix": "buy_exact_quote_in",
                    "kotirovshchik": SPOSOB_REZERVY, "perevorot": False},
    PROG_KRIVAYA: {"label": "Кривая pump.fun v2", "ix": "buy_exact_quote_in_v2",
                   "kotirovshchik": SPOSOB_KRIVAYA_V2, "perevorot": False},
}

# КРИВАЯ v2: РАЗНОВИДНОСТЬ И МЕСТА. Дискриминатор -- из живых сделок (его же
# знает c2_swap_build.BONDING_DISCS), 27 счетов, котировка отдельным минтом.
KRIVAYA_V2_DISC = "c2ab1c46684d5b2f"
KRIVAYA_V2_SCHETOV = 27
# Места, которые МЫ подставляем, и чем они выводятся. Проверено на живых сделках:
#   13 -- наш кошелёк (подписант)
#   14 -- ATA(наш, минт базы (место 1), программа базы (место 3))
#   15 -- ATA(наш, минт котировки (место 2), программа котировки (место 4))
#   20 -- PDA(["user_volume_accumulator", наш], программа кривой)
#   21 -- ATA(место 20, минт котировки, программа котировки)
KRIVAYA_V2_MESTA = {"user": 13, "ata_bazy": 14, "ata_kotirovki": 15,
                    "uva": 20, "assoc_uva": 21,
                    "mint_bazy": 1, "mint_kotirovki": 2,
                    "prog_bazy": 3, "prog_kotirovki": 4}
SEMYA_UVA = b"user_volume_accumulator"

WHY_TIP = "тип пула не в таблице USDC-ноги сигнальных типов"
WHY_KRIVAYA_V1 = ("кривая pump.fun без котировочного минта (18 счетов): котировка "
                  "там нативный SOL, и второй ноге в USDC делать нечего")
WHY_KRIVAYA_DISC = ("разновидность инструкции кривой не buy_exact_quote_in_v2 "
                    "(c2ab1c46684d5b2f) -- USDC-нога берёт только её")
WHY_OTHER_SIDE = ("своп источника шёл в другую сторону -- цену покупки "
                  "из обратного свопа брать нельзя")
WHY_NO_REVERSE = ("сделка источника -- продажа, а переставить стороны у этого типа "
                  "нечем (нога идёт кирпичами c2_swap_build)")
WHY_F_BOLSHE_1 = ("котировщик покупки обещает больше, чем даёт кривая по остаткам "
                  "хранилищ (f > 1): в хранилище CPMM лежат не снятые "
                  "protocol_fees/fund_fees, и минимум выхода вышел бы завышенным")
WHY_EXACT_OUT = "шаг 2 с инструкцией точного выхода -- этим путём не берём"
WHY_SOBYTIYA_NET = "события сделки кривой в транзакции источника нет или оно не разобралось"
WHY_KOTIROVKA_SOBYTIYA = ("минт котировки в событии кривой не тот, которым платит "
                          "нога -- считать по нему нельзя")

# НАША ТАБЛИЦА АДРЕСОВ ПОЛОСЫ. Создана и сверена по цепи Code-1 (lane_usdc_alt),
# здесь только читается из файла: ни одного вызова сети.
FAJL_TABLICY = "usdc_noga_alt.json"
TABLICA_ADRESOV = "B1LxDr9ib1ezCSebbxnF73hCSAsVsy23U5fEwrqkfzXY"
ADRESOV_V_TABLICE = 34


class OshibkaNogi(Exception):
    pass


def _moduli():
    import bloom_lane_two_step as TS  # noqa: PLC0415
    import c2_common as C  # noqa: PLC0415
    import c2_shadow_build as SB  # noqa: PLC0415
    import c2_swap_build as B  # noqa: PLC0415
    import c2_usdc_noga as UN  # noqa: PLC0415
    import c3_kotirovka_prodazhi as K  # noqa: PLC0415
    return C, B, UN, TS, SB, K


def tip_znakom(programma: str) -> bool:
    return (programma or "") in TIPY


def kotirovshchik(programma: str) -> str | None:
    return (TIPY.get(programma or "") or {}).get("kotirovshchik")


def rezhim(gruppa: str | None = None) -> str:
    """Режим -- ТОТ ЖЕ, что у USDC-ноги: один флаг на обе таблицы типов."""
    return _moduli()[2].rezhim(gruppa)


def cu_nogi() -> int:
    return _moduli()[2].cu_nogi()


def tablica_polosy(fajl: str | None = None) -> dict:
    """34 адреса нашей таблицы из файла репозитория. Без сети."""
    put_ = Path(fajl) if fajl else (КОРЕНЬ / "data" / FAJL_TABLICY)
    iz = {"ok": False, "why_not": None, "klyuch": None, "adresa": None}
    if not put_.exists():
        iz["why_not"] = f"файла таблицы нет: {put_}"
        return iz
    d = json.loads(put_.read_text(encoding="utf-8"))
    a = ((d.get("адреса") or {}).get("адреса")) or []
    klyuch = (((d.get("принятие") or {}).get("состояние") or {}).get("адрес"))
    if not a or not klyuch:
        iz["why_not"] = "в файле таблицы нет ни адресов, ни ключа"
        return iz
    iz.update(ok=True, klyuch=klyuch, adresa=list(a))
    return iz


def lut_polosy(fajl: str | None = None):
    """Таблица адресов объектом solders -- для замера размера БЕЗ сети."""
    t = tablica_polosy(fajl)
    if not t["ok"]:
        raise OshibkaNogi(t["why_not"])
    from solders.address_lookup_table_account import AddressLookupTableAccount  # noqa: PLC0415
    from solders.pubkey import Pubkey  # noqa: PLC0415
    return AddressLookupTableAccount(
        Pubkey.from_string(t["klyuch"]),
        [Pubkey.from_string(x) for x in t["adresa"]])


# ------------------------------------------------- сторона USDC и шаблон ноги

def storona(tx_istochnika: dict, *, programma: str, pul: str | None = None,
            hranilishche: str | None = None, mint_kotirovki: str = USDC) -> dict:
    """Шаблон второй ноги, развёрнутый входом на USDC. Ни одного чтения сети.

    Вид ответа -- РОВНО как у c2_usdc_noga.storona (ok, why_not, way, label, tpl,
    base_mint, base_program, quote_program, razvernut, exact_out, src_tpl), чтобы
    врезка в полосу была одна на две таблицы типов, а не две разные.
    """
    C, B, UN, TS, _SB, _K = _moduli()
    iz = {"ok": False, "why_not": None, "way": UN.WAY_BRICKS, "label": None,
          "tpl": None, "base_mint": None, "base_program": None,
          "quote_program": None, "razvernut": False, "exact_out": None,
          "src_tpl": None, "kotirovshchik": None, "raznovidnost": None}
    t = TIPY.get(programma or "")
    if not t:
        iz["why_not"] = f"{WHY_TIP}: {str(programma)[:8]}"
        return iz
    iz["label"], iz["kotirovshchik"] = t["label"], t["kotirovshchik"]
    selektor = hranilishche or pul
    if not selektor:
        iz["why_not"] = "ни хранилища, ни адреса пула -- инструкцию ноги не выбрать"
        return iz
    shab = B.extract_template(tx_istochnika, programma, selektor)
    if not shab.get("ok"):
        iz["why_not"] = f"шаблон ноги: {shab.get('why_not')}"
        return iz
    iz["src_tpl"] = shab
    # КРИВАЯ: ТОЛЬКО РАЗНОВИДНОСТЬ С КОТИРОВОЧНЫМ МИНТОМ. Спутать её с 18-счётной
    # значило бы подставить наш кошелёк не туда.
    if programma == PROG_KRIVAYA:
        iz["raznovidnost"] = shab.get("ix")
        if shab.get("ix") != KRIVAYA_V2_DISC:
            iz["why_not"] = (WHY_KRIVAYA_V1 if len(shab["accounts"]) == 18
                             else WHY_KRIVAYA_DISC)
            return iz
        if len(shab["accounts"]) != KRIVAYA_V2_SCHETOV:
            iz["why_not"] = (f"счетов {len(shab['accounts'])}, а у "
                             f"buy_exact_quote_in_v2 их {KRIVAYA_V2_SCHETOV}")
            return iz
    tochnyj = (shab.get("exact_out") if shab.get("exact_out") is not None
               else (B.spec_of(shab) or {}).get("exact_out"))
    iz["exact_out"] = bool(tochnyj)
    if tochnyj:
        iz["why_not"] = WHY_EXACT_OUT
        return iz
    mv = B.mints_and_vaults(shab, tx_istochnika)
    if not mv:
        iz["why_not"] = "у пула источника не разобрались минты и хранилища"
        return iz
    if mv.get("quote_mint") == mint_kotirovki:
        iz.update(ok=True, tpl=shab, base_mint=mv.get("base_mint"),
                  base_program=mv.get("base_program"),
                  quote_program=mv.get("quote_program"), razvernut=False)
        return iz
    # СТОРОНЫ НАОБОРОТ. У CPMM слоты стоят в порядке ВХОД/ВЫХОД (замер полосы на
    # 34 инструкциях из 31 пула), и переставить их умеет готовая функция
    # bloom_lane_two_step.перевернуть_cpmm -- своей здесь не заводится. У
    # остальных трёх типов места именованы (база/котировка), и переставлять их
    # нечем: это ДРУГАЯ инструкция программы.
    if mv.get("base_mint") != mint_kotirovki:
        iz["why_not"] = (f"котировка пула не {mint_kotirovki[:8]}: стороны пула "
                         f"{str(mv.get('quote_mint'))[:8]}/"
                         f"{str(mv.get('base_mint'))[:8]}")
        return iz
    if not t.get("perevorot"):
        iz["why_not"] = WHY_NO_REVERSE
        return iz
    # ПЕРЕВОРОТ ЧУЖОЙ ФУНКЦИЕЙ, И ЕГО ОТКАЗ -- ИСКЛЮЧЕНИЕ, А НЕ ПОЛЕ. Она
    # проверяет сама себя: после перестановки на месте 10 обязан стоять ВХОД,
    # иначе ValueError. Ловим и называем словами, наружу исключение не пускаем.
    try:
        perev = TS.перевернуть_cpmm(shab, mint_kotirovki)
    except (ValueError, KeyError, IndexError) as exc:
        iz["why_not"] = f"стороны CPMM не переставились: {exc}"
        return iz
    mv2 = B.mints_and_vaults(perev, tx_istochnika)
    if not mv2 or mv2.get("quote_mint") != mint_kotirovki:
        iz["why_not"] = ("после перестановки сторон CPMM котировка всё равно не "
                         f"{mint_kotirovki[:8]}")
        return iz
    iz.update(ok=True, tpl=perev, base_mint=mv2.get("base_mint"),
              base_program=mv2.get("base_program"),
              quote_program=mv2.get("quote_program"), razvernut=True)
    return iz


# --------------------------------------------- места второй ноги у кривой v2

def mesta_krivoj_v2(scheta: list, *, nash_koshelek: str) -> dict:
    """{место: адрес} для НАШИХ счетов buy_exact_quote_in_v2. Нуль чтений.

    Связанный накопитель (место 21) здесь ВЫВОДИТСЯ, а не берётся из состояния:
    в c2_swap_build про него написано «ключ не выводится семенами», и для USDC
    такого состояния у нас не будет никогда (файл заполняется ошибками цепи).
    Замер: на живой покупке с котировкой HiMSSzzwkZ... место 21 равно
    ATA(место 20, минт котировки, программа котировки) РОВНО.
    """
    _C, B, _UN, _TS, _SB, _K = _moduli()
    M = KRIVAYA_V2_MESTA
    iz = {"ok": False, "why_not": None, "mesta": None, "soshlos": 0, "provereno": 0,
          "plohie": None}
    if len(scheta) != KRIVAYA_V2_SCHETOV:
        iz["why_not"] = f"счетов {len(scheta)}, ожидалось {KRIVAYA_V2_SCHETOV}"
        return iz
    mint_b, mint_q = scheta[M["mint_bazy"]], scheta[M["mint_kotirovki"]]
    prog_b, prog_q = scheta[M["prog_bazy"]], scheta[M["prog_kotirovki"]]
    uva = B.pda([SEMYA_UVA, "USER"], nash_koshelek, PROG_KRIVAYA)
    mesta = {M["user"]: nash_koshelek,
             M["ata_bazy"]: B.ata(nash_koshelek, mint_b, prog_b),
             M["ata_kotirovki"]: B.ata(nash_koshelek, mint_q, prog_q),
             M["uva"]: uva,
             M["assoc_uva"]: B.ata(uva, mint_q, prog_q)}
    iz["mesta"] = mesta
    # ПЕРЕСБОРКА СДЕЛКИ ИСТОЧНИКА -- ЭТО И ЕСТЬ ПРОВЕРКА ВЫВОДА. Когда кошелёк тот
    # же, все пять мест обязаны совпасть с его счетами; расхождение -- отказ.
    if scheta[M["user"]] == nash_koshelek:
        plohie = [i for i, a in mesta.items() if scheta[i] != a]
        iz.update(soshlos=len(mesta) - len(plohie), provereno=len(mesta),
                  plohie=plohie)
        if plohie:
            iz["why_not"] = (f"вывод мест не сошёлся с покупкой источника: "
                             f"{plohie}")
            return iz
    iz["ok"] = True
    return iz


def instrukciya_nogi(storona_nogi: dict, *, user: str, amount_in: int, min_out: int,
                     tx_istochnika: dict | None = None,
                     kak_u_istochnika: bool = False):
    """Инструкция свопа второй ноги. У кривой v2 -- своими местами, у остальных --
    кирпич c2_swap_build.swap_instruction (его же зовёт c2_usdc_noga).

    kak_u_istochnika=True -- пересборка сделки источника для самопроверки байт в
    байт: его аргументы, его хвост, его пользователь.
    """
    _C, B, UN, _TS, _SB, _K = _moduli()
    if not (storona_nogi or {}).get("ok"):
        raise OshibkaNogi((storona_nogi or {}).get("why_not") or "стороны ноги нет")
    tpl = storona_nogi["tpl"]
    if tpl.get("program") != PROG_KRIVAYA:
        return UN.instrukciya_nogi(storona_nogi, user=user, amount_in=amount_in,
                                   min_out=min_out, tx_istochnika=tx_istochnika or {},
                                   kak_u_istochnika=kak_u_istochnika)
    from solders.instruction import AccountMeta, Instruction  # noqa: PLC0415
    from solders.pubkey import Pubkey  # noqa: PLC0415
    pr = mesta_krivoj_v2(tpl["accounts"], nash_koshelek=user)
    if not pr["ok"]:
        raise OshibkaNogi(pr["why_not"])
    subs = pr["mesta"]
    metas = []
    for i, a in enumerate(tpl["accounts"]):
        adres = subs.get(i, a)
        metas.append(AccountMeta(Pubkey.from_string(adres),
                                 is_signer=(adres == user),
                                 is_writable=(i in subs
                                              or tpl["writable"].get(a, False))))
    # ДИСКРИМИНАТОР И ХВОСТ -- ИЗ СДЕЛКИ ИСТОЧНИКА, как у кирпича: имени
    # разновидности мы не знаем, а угадывать его на деньгах нельзя.
    data = tpl["data"][:8] + struct.pack("<QQ", int(amount_in), int(min_out)) \
        + tpl["data"][24:]
    return Instruction(Pubkey.from_string(tpl["program"]), data, metas)


def polzovatel_istochnika(storona_nogi: dict, tx_istochnika: dict) -> str | None:
    """Кто подписал сделку источника на его месте в этой инструкции."""
    _C, _B, UN, _TS, _SB, _K = _moduli()
    tpl = (storona_nogi or {}).get("tpl") or {}
    if tpl.get("program") == PROG_KRIVAYA:
        сч = tpl.get("accounts") or []
        return сч[KRIVAYA_V2_MESTA["user"]] if len(сч) == KRIVAYA_V2_SCHETOV else None
    return UN.polzovatel_istochnika(storona_nogi, tx_istochnika)


# ------------------------------------------------------------ цена и минимум

def sobytie_krivoj(tx_istochnika: dict, mint_bazy: str | None = None) -> dict:
    """Событие сделки кривой с КОТИРОВОЧНОЙ стороной. Разбор -- свой же модуль
    котировки продажи (c3_kotirovka_prodazhi.событие_кривой): он читает весь
    хвост v2 по IDL и не берёт событие, которое не воспроизвело свою сделку."""
    _C, _B, _UN, _TS, _SB, K = _moduli()
    iz = {"ok": False, "why_not": None, "polya": None, "tokenovaya": None,
          "quote_mint": None}
    ev = K.событие_кривой(tx_istochnika or {}, mint_bazy)
    if not ev.get("ok"):
        iz["why_not"] = f"{WHY_SOBYTIYA_NET}: {ev.get('why_not')}"
        return iz
    p = ev["поля"]
    tok = int(p.get("sol_amount") or 0) == 0 and int(p.get("quote_amount") or 0) > 0
    iz.update(ok=True, polya=p, tokenovaya=tok,
              quote_mint=(p.get("quote_mint") if tok else WSOL))
    return iz


def min_out_boj(storona_nogi: dict, tx_istochnika: dict, *, amount_in: int,
                proskalzyvanie: float, mint_kotirovki: str = USDC) -> dict:
    """Минимум выхода второй ноги -- котировщиком типа, НУЛЬ ЧТЕНИЙ СЕТИ.

    Цена у всех четырёх типов считается из состояния ПОСЛЕ сделки источника: у
    CPMM и Pump AMM -- остатки хранилищ, у LaunchLab и кривой -- виртуальные
    резервы их события. Своего объёма в цене нет ни у одного (пул её не знает до
    сделки), и это закрывает проскальзывание группы, а не этот модуль.
    """
    _C, B, _UN, _TS, _SB, K = _moduli()
    iz = {"ok": False, "why_not": None, "min_out": None, "expected_out": None,
          "put": None, "chtenij": 0, "fee_share": None, "f": None}
    if not (storona_nogi or {}).get("ok"):
        iz["why_not"] = (storona_nogi or {}).get("why_not") or "стороны ноги нет"
        return iz
    tpl = storona_nogi["tpl"]
    sp = kotirovshchik(tpl.get("program") or "")
    iz["put"] = sp
    if not isinstance(amount_in, int) or amount_in <= 0:
        iz["why_not"] = f"сумма второй ноги не положительное целое: {amount_in!r}"
        return iz
    if sp == SPOSOB_REZERVY:
        if storona_nogi.get("razvernut"):
            iz["why_not"] = WHY_OTHER_SIDE
            return iz
        mo = B.min_out_from_reserves(tpl, tx_istochnika, int(amount_in),
                                     float(proskalzyvanie))
        mo = mo if isinstance(mo, dict) else {}
        iz["f"] = mo.get("fee_factor")
        iz["fee_share"] = (1 - float(mo["fee_factor"])
                           if isinstance(mo.get("fee_factor"), (int, float)) else None)
        if not mo.get("ok"):
            iz["why_not"] = mo.get("why_not")
            return iz
        if mo.get("f_vyshe_krivoj"):
            iz["why_not"] = f"{WHY_F_BOLSHE_1}: f = {float(mo['fee_factor']):.6f}"
            return iz
        iz.update(ok=True, min_out=int(mo["min_out"]),
                  expected_out=int(mo["expected_out"]))
        return iz
    if sp == SPOSOB_LAUNCHLAB:
        mo = B.launchlab_min_out(tx_istochnika, int(amount_in), float(proskalzyvanie))
        mo = mo if isinstance(mo, dict) else {}
        if not mo.get("ok"):
            iz["why_not"] = mo.get("why_not")
            return iz
        iz.update(ok=True, min_out=int(mo["min_out"]),
                  expected_out=int(mo["expected_out"]),
                  fee_share=mo.get("fee_rate"))
        return iz
    if sp == SPOSOB_KRIVAYA_V2:
        ev = sobytie_krivoj(tx_istochnika, storona_nogi.get("base_mint"))
        if not ev["ok"]:
            iz["why_not"] = ev["why_not"]
            return iz
        if not ev["tokenovaya"] or ev["quote_mint"] != mint_kotirovki:
            iz["why_not"] = (f"{WHY_KOTIROVKA_SOBYTIYA}: в событии "
                             f"{str(ev['quote_mint'])[:8]}, нога платит "
                             f"{mint_kotirovki[:8]}")
            return iz
        p = ev["polya"]
        k = K.вход_кривой(котировки=int(amount_in),
                          вирт_котировка=int(p["virtual_quote_reserves"]),
                          вирт_токены=int(p["virtual_token_reserves"]),
                          fee_bps=int(p["fee_basis_points"]),
                          creator_bps=int(p["creator_fee_basis_points"]))
        iz["fee_share"] = (int(p["fee_basis_points"])
                           + int(p["creator_fee_basis_points"])) / 10_000
        if not k.get("ok"):
            iz["why_not"] = f"кривая покупки отказала: {k.get('why_not')}"
            return iz
        ozh = int(k["выход"])
        iz.update(ok=True, expected_out=ozh,
                  min_out=int(ozh * (1 - float(proskalzyvanie))),
                  do_krivoj=k.get("до_кривой"))
        return iz
    iz["why_not"] = f"котировщика для {str(tpl.get('program'))[:8]} в таблице нет"
    return iz


def summy_nog(**kw) -> dict:
    """Связка сумм двух ног -- числами c2_usdc_noga (а те -- двухшагового пути)."""
    return _moduli()[2].summy_nog(**kw)


# ------------------------------------------------- список инструкций и сборка

def instrukcii(*, tx_istochnika: dict, istochnik: str, mint: str, nash_koshelek: str,
               lamporty: int, kesh_nog, proskalzyvanie: float = 0.35,
               cu_units: int | None = None, prioritet_lamporty: int = 1_000_000,
               chaevye_lamporty: int = 1_000_000, chaevye_spiskom: list | None = None,
               chaevye_adres: str | None = None, nons: tuple | None = None,
               nalog_kotirovki_bps=None, min_out_vneshnij: int | None = None,
               gruppa: str | None = None, kotirovka: str = "boj",
               mint_kotirovki: str = USDC) -> dict:
    """СПИСОК инструкций двух ног в порядке двухшагового пути -- без компиляции.

    Порядок -- РОВНО тот же, что у c2_usdc_noga.instrukcii (самопроверка сверяет
    его позиция в позицию):
        advance_nonce?, cu_limit, cu_price, ata(WSOL), ata(котировка), ata(база),
        sol_transfer, sync_native, своп ноги 1, своп ноги 2, чаевые
    """
    C, B, UN, TS, SB, _K = _moduli()
    if cu_units is None:
        cu_units = cu_nogi()
    iz = {"ok": False, "why_not": None, "route": ROUTE, "steps": 2,
          "rezhim": rezhim(gruppa), "pool_program": None, "label": None,
          "way": None, "quote_mint": mint_kotirovki, "leg1_pool_program": None,
          "leg1_template_age_s": None, "leg1_min_out": None, "leg2_amount_in": None,
          "leg2_to_pool": None, "quote_fee_bps": None, "min_out": None,
          "expected_out": None, "min_out_from": None, "razvernut": None,
          "slippage_used": proskalzyvanie, "ixs": None, "leg1_entry": None,
          "nonce_para": None, "tip_account": None, "tips": None,
          "tips_total_lamports": None, "kotirovshchik": None}
    if kesh_nog is None:
        iz["why_not"] = "кэша шаблонов первой ноги нет -- собирать не из чего"
        return iz
    np_ = TS.пара_нонса(nons)
    if not np_["ok"]:
        iz["why_not"] = np_["почему"]
        return iz
    nons = np_["пара"]
    iz["nonce_para"] = nons
    try:
        pul = C.identify_pool(tx_istochnika, istochnik, mint)
        if not pul.get("ok"):
            iz["why_not"] = f"пул источника: {pul.get('why_not')}"
            return iz
        prog = UN.programma_pula(tx_istochnika, pul["pool_vault"])
        iz["pool_program"] = prog
        st = storona(tx_istochnika, programma=prog, hranilishche=pul["pool_vault"],
                     mint_kotirovki=mint_kotirovki)
        iz.update(label=st.get("label"), way=st.get("way"),
                  razvernut=st.get("razvernut"), kotirovshchik=st.get("kotirovshchik"))
        if not st.get("ok"):
            iz["why_not"] = st.get("why_not")
            return iz
        e, vozrast = kesh_nog.get(mint_kotirovki)
        if e is None:
            iz["why_not"] = f"шаблона SOL -> {mint_kotirovki[:8]} в кэше нет"
            return iz
        iz["leg1_pool_program"] = e.get("program")
        iz["leg1_template_age_s"] = round(vozrast, 1) if vozrast is not None else None
        if vozrast is not None and vozrast > SB.LEG_MAX_AGE_S:
            iz["why_not"] = (f"шаблон первой ноги старше {SB.LEG_MAX_AGE_S} с "
                             f"({vozrast:.0f} с)")
            return iz
        mv1 = e.get("mv") or {}
        s = summy_nog(lamporty=lamporty, price_sol=e.get("price_sol"),
                      q_dec=e.get("q_dec"), quote_program=mv1.get("base_program"),
                      nalog_bps=nalog_kotirovki_bps)
        iz.update(quote_fee_bps=s.get("quote_fee_bps"),
                  leg1_min_out=s.get("leg1_min_out"),
                  leg2_amount_in=s.get("leg2_amount_in"),
                  leg2_to_pool=s.get("leg2_to_pool"))
        if not s.get("ok"):
            iz["why_not"] = s.get("why_not")
            return iz
        if isinstance(min_out_vneshnij, int) and min_out_vneshnij > 0:
            min_out = int(min_out_vneshnij)
            iz.update(min_out=min_out, min_out_from="передан полосой")
        else:
            kb = min_out_boj(st, tx_istochnika, amount_in=int(s["leg2_to_pool"]),
                             proskalzyvanie=proskalzyvanie,
                             mint_kotirovki=mint_kotirovki)
            iz.update(min_out=kb.get("min_out"), expected_out=kb.get("expected_out"),
                      min_out_from=kb.get("put"), chtenij=kb.get("chtenij"),
                      fee_share=kb.get("fee_share"))
            if not kb.get("ok"):
                iz["why_not"] = kb.get("why_not")
                return iz
            min_out = int(kb["min_out"])
        wsol_schet = B.ata(nash_koshelek, C.WSOL, mv1.get("quote_program"))
        ixs = []
        if nons:
            ixs.append(B.advance_nonce(str(nons[0]), str(nons[1])))
        ixs += [B.cu_limit(cu_units),
                B.cu_price(TS._цена_cu(prioritet_lamporty, cu_units)),
                B.ata_idempotent(nash_koshelek, nash_koshelek, C.WSOL,
                                 mv1.get("quote_program")),
                B.ata_idempotent(nash_koshelek, nash_koshelek, mint_kotirovki,
                                 mv1.get("base_program")),
                B.ata_idempotent(nash_koshelek, nash_koshelek, st["base_mint"],
                                 st["base_program"]),
                B.sol_transfer(nash_koshelek, wsol_schet, lamporty),
                B.sync_native(wsol_schet),
                B.swap_instruction(e["tpl"], e["tx"], nash_koshelek, lamporty,
                                   int(s["leg1_min_out"])),
                instrukciya_nogi(st, user=nash_koshelek,
                                 amount_in=int(s["leg2_amount_in"]),
                                 min_out=min_out, tx_istochnika=tx_istochnika)]
        pary = TS._пары_чаевых(chaevye_spiskom, chaevye_adres, chaevye_lamporty)
        for adres, lamp in pary:
            ixs.append(B.sol_transfer(nash_koshelek, adres, int(lamp)))
        if pary:
            iz["tip_account"] = pary[0][0]
            iz["tips"] = list(pary)
            iz["tips_total_lamports"] = sum(int(l_) for _, l_ in pary)
    except Exception as exc:  # noqa: BLE001
        iz["why_not"] = f"сборка USDC-ноги сигнальных типов: {UN._sled(exc)}"
        return iz
    iz.update(ok=True, ixs=ixs, leg1_entry=e)
    return iz


def sobrat(*, tx_istochnika: dict, istochnik: str, mint: str, nash_koshelek: str,
           lamporty: int, kesh_nog, proskalzyvanie: float = 0.35,
           cu_units: int | None = None, prioritet_lamporty: int = 1_000_000,
           chaevye_lamporty: int = 1_000_000, chaevye_spiskom: list | None = None,
           chaevye_adres: str | None = None, nons: tuple | None = None,
           rpc_call=None, gruppa: str | None = None, nalog_kotirovki_bps=None,
           min_out_vneshnij: int | None = None, nashi_tablicy: list | None = None,
           mint_kotirovki: str = USDC, luts_gotovye: list | None = None) -> dict:
    """ОДНА транзакция: SOL -> USDC -> токен на сигнальном типе. Без подписи.

    luts_gotovye -- таблицы адресов объектами (замер без сети); на денежном пути
    их не передают, там таблицы приходят из кэша ног, как у c2_usdc_noga.
    """
    t0 = time.perf_counter()
    _C, _B, UN, TS, SB, _K = _moduli()
    if rezhim(gruppa) != UN.MODE_LIVE:
        return {"ok": False, "route": ROUTE, "rezhim": rezhim(gruppa),
                "why_not": (UN.WHY_OFF if rezhim(gruppa) == UN.MODE_OFF
                            else UN.WHY_SHADOW),
                "tx_base64": None, "size": None}
    iz = instrukcii(
        tx_istochnika=tx_istochnika, istochnik=istochnik, mint=mint,
        nash_koshelek=nash_koshelek, lamporty=lamporty, kesh_nog=kesh_nog,
        proskalzyvanie=proskalzyvanie, cu_units=cu_units,
        prioritet_lamporty=prioritet_lamporty, chaevye_lamporty=chaevye_lamporty,
        chaevye_spiskom=chaevye_spiskom, chaevye_adres=chaevye_adres, nons=nons,
        nalog_kotirovki_bps=nalog_kotirovki_bps, min_out_vneshnij=min_out_vneshnij,
        gruppa=gruppa, mint_kotirovki=mint_kotirovki)
    ixs = iz.pop("ixs", None)
    e = iz.pop("leg1_entry", None)
    nons = iz.pop("nonce_para", None)
    iz.update(tx_base64=None, size=None, build_ms=None,
              nonce_account=(nons[0] if nons else None))
    if not iz.get("ok") or not ixs:
        iz["ok"] = False
        return iz
    try:
        import base64  # noqa: PLC0415

        from solders.hash import Hash  # noqa: PLC0415
        from solders.message import MessageV0  # noqa: PLC0415
        from solders.pubkey import Pubkey  # noqa: PLC0415
        from solders.signature import Signature  # noqa: PLC0415
        from solders.transaction import VersionedTransaction  # noqa: PLC0415

        svoi = [k for k in (nashi_tablicy or []) if k]
        nuzhny = [k for k in svoi + SB._lut_keys(e["tx"])
                  + SB._lut_keys(tx_istochnika) if k not in kesh_nog.luts]
        if nuzhny and rpc_call is not None:
            kesh_nog.load_luts(nuzhny, rpc=rpc_call)
            iz["hot_lut_calls"] = 1
        klyuchi = list(dict.fromkeys(svoi + SB._lut_keys(e["tx"])
                                     + SB._lut_keys(tx_istochnika)))
        tablicy = [kesh_nog.luts[k] for k in klyuchi if k in kesh_nog.luts]
        tablicy += list(luts_gotovye or [])
        iz["lut_tables"] = len(tablicy)
        iz["nashi_tablicy_vzjaty"] = [k for k in svoi if k in kesh_nog.luts]
        iz["nashi_tablicy_ne_vzjaty"] = [k for k in svoi if k not in kesh_nog.luts]
        msg = MessageV0.try_compile(Pubkey.from_string(nash_koshelek), ixs, tablicy,
                                    Hash.default())
        syroe = bytes(VersionedTransaction.populate(
            msg, [Signature.default()] * msg.header.num_required_signatures))
    except Exception as exc:  # noqa: BLE001
        iz["ok"] = False
        iz["why_not"] = f"сборка USDC-ноги сигнальных типов: {UN._sled(exc)}"
        return iz
    iz["size"] = len(syroe)
    if len(syroe) > TS.ПРЕДЕЛ_РАЗМЕРА_TX:
        iz["ok"] = False
        iz["why_not"] = (f"транзакция {len(syroe)} байт при пределе сети "
                         f"{TS.ПРЕДЕЛ_РАЗМЕРА_TX}")
        iz["too_big"] = True
        return iz
    iz.update(ok=True, tx_base64=base64.b64encode(syroe).decode(),
              build_ms=round((time.perf_counter() - t0) * 1000, 3))
    return iz


# ------------------------------------- продажа: токен -> USDC -> SOL (вторая нога)

# ПРОДАЖА ПЕРВОЙ НОГИ -- ЗЕРКАЛО НАШЕЙ ЖЕ ПОКУПКИ ЭТОЙ НОГИ. Пул SOL -> USDC у
# полосы один (data/nogi_shablony.json, Pump AMM, глубина 21 851 SOL), и продажа
# USDC -> SOL на нём -- это та же покупка без двух мест, 19 и 20
# (user_volume_accumulator и его связанный счёт -- они только у покупки). Замер
# полосы (bloom_lane_sell, раскладка снята с НАШЕЙ собственной продажи): из
# 26-счётной покупки выходит 24 счёта. У нашего пула покупка 25-счётная, и те же
# два места дают 23 -- число, которое c2_swap_build.SPECS_ПРОДАЖИ допускает
# (min_accounts 23), но живой 23-счётной продажи Pump AMM в образцах
# репозитория НЕТ НИ ОДНОЙ. Поэтому шаблон несёт это полем
# zhivym_obrazcom_ne_provereno, а 6+ живых продаж Pump AMM с 23 счетами
# запрошены у Code-2 вместе с USDC-образцами.
PUMP_AMM_MESTA_TOLKO_POKUPKI = (19, 20)
PUMP_AMM_SCHETOV_PRODAZHI = (23, 24)
WHY_NOGA2_SCHETOV = ("продажа первой ноги: после выбрасывания мест 19 и 20 счетов "
                     "не 23 и не 24 -- раскладка не та")
WHY_NOGA2_NET_MIN = ("минимум выхода второй ноги продажи не передан: живых резервов "
                     "пула SOL/USDC этот модуль не читает (сеть -- не его дело)")


def noga_2_prodazhi(zapis_nogi_1: dict) -> dict:
    """Шаблон продажи USDC -> SOL: наша покупка первой ноги без мест 19 и 20."""
    _C, B, _UN, _TS, _SB, _K = _moduli()
    iz = {"ok": False, "why_not": None, "program": None, "accounts": None,
          "writable": None, "schetov_pokupki": None,
          "zhivym_obrazcom_ne_provereno": None}
    tpl = (zapis_nogi_1 or {}).get("tpl") or {}
    if not tpl.get("accounts"):
        iz["why_not"] = "шаблона первой ноги нет -- продажу её зеркалить не из чего"
        return iz
    sch = list(tpl["accounts"])
    iz["program"], iz["schetov_pokupki"] = tpl.get("program"), len(sch)
    if tpl.get("program") != PROG_PUMP_AMM:
        iz["why_not"] = (f"пул первой ноги {str(tpl.get('program'))[:8]}, а зеркало "
                         f"продажи измерено только у Pump AMM")
        return iz
    if len(sch) <= max(PUMP_AMM_MESTA_TOLKO_POKUPKI):
        iz["why_not"] = (f"в покупке ноги {len(sch)} счетов -- места "
                         f"{PUMP_AMM_MESTA_TOLKO_POKUPKI} из неё не выбросить")
        return iz
    prodazha = [a for i, a in enumerate(sch)
                if i not in PUMP_AMM_MESTA_TOLKO_POKUPKI]
    if len(prodazha) not in PUMP_AMM_SCHETOV_PRODAZHI:
        iz["why_not"] = f"{WHY_NOGA2_SCHETOV}: вышло {len(prodazha)}"
        return iz
    iz.update(ok=True, accounts=prodazha, writable=tpl.get("writable") or {},
              zhivym_obrazcom_ne_provereno=(len(prodazha) == 23))
    return iz


def instrukciya_nogi_2_prodazhi(shab: dict, *, nash_koshelek: str, kotirovki_v: int,
                                min_out: int):
    """sell(base_amount_in, min_quote_amount_out) на пуле первой ноги: вход USDC."""
    _C, B, _UN, _TS, _SB, _K = _moduli()
    if not (shab or {}).get("ok"):
        raise OshibkaNogi((shab or {}).get("why_not") or "шаблона продажи ноги нет")
    from solders.instruction import AccountMeta, Instruction  # noqa: PLC0415
    from solders.pubkey import Pubkey  # noqa: PLC0415
    metas = [AccountMeta(Pubkey.from_string(a), is_signer=(a == nash_koshelek),
                         is_writable=bool(shab["writable"].get(a, False)))
             for a in shab["accounts"]]
    data = B.disc("sell") + struct.pack("<QQ", int(kotirovki_v), int(min_out))
    return Instruction(Pubkey.from_string(shab["program"]), data, metas)


def prodazha_instrukcii(*, tx_pokupki: dict, programma: str, nash_koshelek: str,
                        ostatok: int, kesh_nog, mint_bazy: str | None = None,
                        hranilishche: str | None = None,
                        mint_kotirovki: str = USDC,
                        min_out_nogi_1: int | None = None,
                        min_out_nogi_2: int | None = None,
                        proskalzyvanie: float = 0.35,
                        cu_units: int | None = None,
                        prioritet_lamporty: int = 1_000_000,
                        chaevye_lamporty: int = 0,
                        chaevye_spiskom: list | None = None,
                        chaevye_adres: str | None = None,
                        nons: tuple | None = None,
                        zakryvat_schet_tokena: bool = True) -> dict:
    """СПИСОК инструкций продажи токен -> USDC -> SOL. Без компиляции и подписи.

    Первая нога -- зеркало НАШЕЙ покупки токена (c3_prodavec_sborka: шаблон из
    покупки, инструкция типа); вторая -- зеркало НАШЕЙ покупки первой ноги
    (Pump AMM sell на пуле SOL/USDC). Минимумы: первой ноги -- котировщик
    продажи (c3_kotirovka_prodazhi, цена в единицах USDC), второй -- ТОЛЬКО
    переданный: живые резервы пула SOL/USDC читает полоса, не этот модуль.

    Порядок -- зеркало покупки, место в место:
        advance_nonce?, cu_limit, cu_price, ata(USDC), ata(WSOL),
        продажа ноги 1 (токен -> USDC), продажа ноги 2 (USDC -> SOL),
        закрытие счёта токена, закрытие счёта WSOL, чаевые
    """
    C, B, UN, TS, _SB, K = _moduli()
    import c3_prodavec_sborka as S  # noqa: PLC0415
    if cu_units is None:
        cu_units = cu_nogi()
    iz = {"ok": False, "why_not": None, "route": ROUTE + "_prodazha", "steps": 2,
          "rezhim": rezhim(None), "pool_program": programma, "label": None,
          "quote_mint": mint_kotirovki, "min_out_nogi_1": None,
          "min_out_nogi_2": None, "noga_2_amount_in": None,
          "min_out_1_otkuda": None, "ixs": None, "nonce_para": None,
          "zakryt_schet_tokena": None, "zakryt_schet_wsol": None,
          "noga_2_zhivym_obrazcom_ne_proverena": None, "tips": None,
          "tip_account": None, "tips_total_lamports": None}
    if kesh_nog is None:
        iz["why_not"] = "кэша шаблонов первой ноги нет -- вторую ногу зеркалить не из чего"
        return iz
    np_ = TS.пара_нонса(nons)
    if not np_["ok"]:
        iz["why_not"] = np_["почему"]
        return iz
    nons = np_["пара"]
    iz["nonce_para"] = nons
    try:
        if not isinstance(ostatok, int) or ostatok <= 0:
            iz["why_not"] = f"остаток не положительное целое: {ostatok!r}"
            return iz
        sh1 = S.шаблон_продажи(tx_pokupki, программа=programma, минт_базы=mint_bazy,
                              минт_котировки=mint_kotirovki,
                              хранилище=hranilishche)
        iz["label"] = sh1.get("метка")
        if not sh1.get("ok"):
            iz["why_not"] = f"шаблон продажи первой ноги: {sh1.get('why_not')}"
            return iz
        iz["zhivaya_prodazha_s_etoj_kotirovkoj"] = sh1.get(
            "живая_продажа_с_этой_котировкой")
        if isinstance(min_out_nogi_1, int) and min_out_nogi_1 > 0:
            mo1, iz["min_out_1_otkuda"] = int(min_out_nogi_1), "передан полосой"
        else:
            k1 = K.котировка_продажи(programma, пул=None, хранилище=hranilishche,
                                     остаток=int(ostatok), tx_покупки=tx_pokupki,
                                     минт_базы=sh1.get("минт_базы"),
                                     минт_котировки=mint_kotirovki,
                                     проскальзывание=proskalzyvanie)
            iz["min_out_1_otkuda"] = k1.get("путь")
            if not k1.get("ok") or not k1.get("min_out"):
                iz["why_not"] = f"цена продажи первой ноги: {k1.get('why_not')}"
                return iz
            # МИНТ ВЫХОДА НАЗВАН: на кривой с котировочным минтом выход в
            # единицах USDC, а не в лампортах, и спутать их нельзя.
            if k1.get("минт_котировки") not in (None, mint_kotirovki):
                iz["why_not"] = (f"котировщик продажи считал в "
                                 f"{str(k1.get('минт_котировки'))[:8]}, а нога "
                                 f"продаёт в {mint_kotirovki[:8]}")
                return iz
            mo1 = int(k1["min_out"])
        iz["min_out_nogi_1"] = mo1
        e, _vozrast = kesh_nog.get(mint_kotirovki)
        if e is None:
            iz["why_not"] = f"шаблона SOL -> {mint_kotirovki[:8]} в кэше нет"
            return iz
        sh2 = noga_2_prodazhi(e)
        iz["noga_2_zhivym_obrazcom_ne_proverena"] = sh2.get(
            "zhivym_obrazcom_ne_provereno")
        if not sh2.get("ok"):
            iz["why_not"] = f"шаблон продажи второй ноги: {sh2.get('why_not')}"
            return iz
        # ВХОД ВТОРОЙ НОГИ -- МИНИМУМ ПЕРВОЙ, ровно как у покупки: больше, чем
        # принесла первая, вторая потратить не может.
        iz["noga_2_amount_in"] = mo1
        if not (isinstance(min_out_nogi_2, int) and min_out_nogi_2 > 0):
            iz["why_not"] = WHY_NOGA2_NET_MIN
            return iz
        iz["min_out_nogi_2"] = int(min_out_nogi_2)
        mv1 = e.get("mv") or {}
        schet_usdc = B.ata(nash_koshelek, mint_kotirovki, mv1.get("base_program"))
        schet_wsol = B.ata(nash_koshelek, C.WSOL, mv1.get("quote_program"))
        ixs = []
        if nons:
            ixs.append(B.advance_nonce(str(nons[0]), str(nons[1])))
        ixs += [B.cu_limit(cu_units),
                B.cu_price(TS._цена_cu(prioritet_lamporty, cu_units)),
                B.ata_idempotent(nash_koshelek, nash_koshelek, mint_kotirovki,
                                 mv1.get("base_program")),
                B.ata_idempotent(nash_koshelek, nash_koshelek, C.WSOL,
                                 mv1.get("quote_program")),
                S.инструкция_продажи(sh1, наш_кошелёк=nash_koshelek,
                                     база_в=int(ostatok), минимум_выхода=mo1),
                instrukciya_nogi_2_prodazhi(sh2, nash_koshelek=nash_koshelek,
                                            kotirovki_v=mo1,
                                            min_out=int(min_out_nogi_2))]
        # ЗАКРЫТИЕ СЧЕТОВ -- ТОЙ ЖЕ ТРАНЗАКЦИЕЙ, как у зеркальной продажи полосы:
        # токеновый счёт закрывается только когда продан весь остаток, а счёт
        # WSOL закрывается всегда -- иначе SOL остался бы завёрнутым.
        if zakryvat_schet_tokena and sh1.get("минт_базы"):
            schet_bazy = B.ata(nash_koshelek, sh1["минт_базы"],
                               sh1.get("программа_базы") or B.TOKEN_PROGRAM)
            ixs.append(S.инструкция_закрытия(
                schet_bazy, nash_koshelek,
                sh1.get("программа_базы") or B.TOKEN_PROGRAM))
            iz["zakryt_schet_tokena"] = schet_bazy
        ixs.append(S.инструкция_закрытия(schet_wsol, nash_koshelek,
                                         mv1.get("quote_program")))
        iz["zakryt_schet_wsol"] = schet_wsol
        iz["schet_usdc"] = schet_usdc
        pary = TS._пары_чаевых(chaevye_spiskom, chaevye_adres, chaevye_lamporty)
        for adres, lamp in pary:
            ixs.append(B.sol_transfer(nash_koshelek, adres, int(lamp)))
        if pary:
            iz["tip_account"] = pary[0][0]
            iz["tips"] = list(pary)
            iz["tips_total_lamports"] = sum(int(l_) for _, l_ in pary)
    except Exception as exc:  # noqa: BLE001
        iz["why_not"] = f"сборка продажи USDC-ноги: {UN._sled(exc)}"
        return iz
    iz.update(ok=True, ixs=ixs, leg1_entry=e)
    return iz


def prodazha_sobrat(*, luts_gotovye: list | None = None,
                    nashi_tablicy: list | None = None, rpc_call=None,
                    **kw) -> dict:
    """ОДНА транзакция продажи токен -> USDC -> SOL. Без подписи и отправки."""
    t0 = time.perf_counter()
    _C, _B, UN, TS, SB, _K = _moduli()
    if rezhim(kw.get("gruppa")) != UN.MODE_LIVE:
        return {"ok": False, "route": ROUTE + "_prodazha",
                "rezhim": rezhim(kw.get("gruppa")),
                "why_not": (UN.WHY_OFF if rezhim(kw.get("gruppa")) == UN.MODE_OFF
                            else UN.WHY_SHADOW), "tx_base64": None, "size": None}
    kw.pop("gruppa", None)
    iz = prodazha_instrukcii(**kw)
    ixs = iz.pop("ixs", None)
    e = iz.pop("leg1_entry", None)
    iz.update(tx_base64=None, size=None, build_ms=None)
    if not iz.get("ok") or not ixs:
        iz["ok"] = False
        return iz
    try:
        import base64  # noqa: PLC0415

        from solders.hash import Hash  # noqa: PLC0415
        from solders.message import MessageV0  # noqa: PLC0415
        from solders.pubkey import Pubkey  # noqa: PLC0415
        from solders.signature import Signature  # noqa: PLC0415
        from solders.transaction import VersionedTransaction  # noqa: PLC0415

        kesh_nog = kw["kesh_nog"]
        svoi = [k for k in (nashi_tablicy or []) if k]
        nuzhny = [k for k in svoi + SB._lut_keys(e["tx"])
                  + SB._lut_keys(kw.get("tx_pokupki") or {})
                  if k not in kesh_nog.luts]
        if nuzhny and rpc_call is not None:
            kesh_nog.load_luts(nuzhny, rpc=rpc_call)
            iz["hot_lut_calls"] = 1
        klyuchi = list(dict.fromkeys(svoi + SB._lut_keys(e["tx"])
                                     + SB._lut_keys(kw.get("tx_pokupki") or {})))
        tablicy = [kesh_nog.luts[k] for k in klyuchi if k in kesh_nog.luts]
        tablicy += list(luts_gotovye or [])
        iz["lut_tables"] = len(tablicy)
        msg = MessageV0.try_compile(Pubkey.from_string(kw["nash_koshelek"]), ixs,
                                    tablicy, Hash.default())
        syroe = bytes(VersionedTransaction.populate(
            msg, [Signature.default()] * msg.header.num_required_signatures))
    except Exception as exc:  # noqa: BLE001
        iz["ok"] = False
        iz["why_not"] = f"сборка продажи USDC-ноги: {UN._sled(exc)}"
        return iz
    iz["size"] = len(syroe)
    if len(syroe) > TS.ПРЕДЕЛ_РАЗМЕРА_TX:
        iz["ok"] = False
        iz["why_not"] = (f"транзакция продажи {len(syroe)} байт при пределе сети "
                         f"{TS.ПРЕДЕЛ_РАЗМЕРА_TX}")
        iz["too_big"] = True
        return iz
    iz.update(ok=True, tx_base64=base64.b64encode(syroe).decode(),
              build_ms=round((time.perf_counter() - t0) * 1000, 3))
    return iz


# ------------------------------------------------------------- самопроверка

# ЧИСЛО ПРОВЕРОК ОБЪЯВЛЕНО ЗАРАНЕЕ: молчаливый пропуск -- это провал. Если файла
# образцов нет или тип перестал разбираться, проверок станет МЕНЬШЕ, и
# самопроверка упадёт на несовпадении числа, а не промолчит зелёным.
ZHDEM_PROVEROK = 122

# ЗАМЕР ПО ТИПАМ НА ЖИВЫХ СДЕЛКАХ С КОТИРОВОЧНЫМ ТОКЕНОМ (не WSOL). Образцы --
# data/c2_pool_samples/<программа>.json плюс разновидности кривой
# (data/c2_curve_variant_samples.json), их же читает c2_swap_build.load_samples.
# Котировка РОВНО USDC по этим четырём типам в образцах не встречается ни разу --
# запрошено у Code-2 по 6+ живых USDC-свопов на тип.
ZHDEM_PO_TIPAM = {
    # CPMM: цена отказывает на 8 образцах из 22 -- поле f > 1 (не снятые
    # protocol_fees/fund_fees в хранилище), отказ по имени, а не тихая подмена.
    PROG_CPMM: {"obrazcov": 22, "storona": 22, "bajt": 22, "cena": 14,
                "otkaz_ceny": 8},
    PROG_LAUNCHLAB: {"obrazcov": 31, "storona": 31, "bajt": 31, "cena": 31,
                     "otkaz_ceny": 0},
    # Pump AMM: живых сделок с котировочным токеном в образцах всего две (23 из
    # 25 -- с WSOL). Это мало, и так и сказано числом.
    PROG_PUMP_AMM: {"obrazcov": 2, "storona": 2, "bajt": 2, "cena": 2,
                    "otkaz_ceny": 0},
    # Кривая: одна живая покупка с котировочным минтом (HiMSSzzwkZ...). Остальные
    # 12 образцов -- нативная котировка (18 счетов) или WSOL.
    PROG_KRIVAYA: {"obrazcov": 1, "storona": 1, "bajt": 1, "cena": 1,
                   "otkaz_ceny": 0},
}
ZHDEM_BAJT_VSEGO = 56

# РАЗМЕР ПАКЕТА -- ЗАМЕР НА ЭТИХ ЖЕ ОБРАЗЦАХ, с нашей таблицей и С ЧАЕВЫМИ.
# "verh" -- как есть: котировочный токен образца в таблице не лежит.
# "usdc" -- в таблицу добавлены РОВНО те три адреса, которые на USDC-котировке в
# ней уже есть: минт котировки, наш ATA котировки, программа токена котировки.
# Это не догадка о будущем, а арифметика по собранному пакету.
ZHDEM_RAZMEROV = {
    PROG_CPMM: {"verh": (977, 1009), "usdc": (915, 947)},
    PROG_LAUNCHLAB: {"verh": (1086, 1118), "usdc": (1024, 1056)},
    PROG_PUMP_AMM: {"verh": (1086, 1118), "usdc": (1024, 1056)},
    PROG_KRIVAYA: {"verh": (1311, 1311), "usdc": (1249, 1249)},
}
# КРИВАЯ НЕ ВЛЕЗАЕТ, И ЭТО ЧИСЛО, А НЕ МНЕНИЕ: 1249 байт при пределе 1232 даже с
# нашей таблицей. Лекарство измерено: четыре ПОСТОЯННЫХ адреса программы кривой
# (global, global_volume_accumulator, fee_config, event_authority) в таблице
# полосы -- и пакет 1125 байт. Ещё два наших (накопитель объёма и связанный
# накопитель) дают 1063, а четыре получателя комиссий -- 970.
KRIVAYA_MESTA_POSTOYANNYH = (0, 19, 22, 25)
ZHDEM_KRIVAYA_S_DOBAVKOJ = {4: 1125, 6: 1063, 10: 970}
# ПОЧЕМУ НЕ ДВУХШАГОВЫМ ПУТЁМ -- ЧИСЛОМ, А НЕ РАССУЖДЕНИЕМ. bloom_lane_two_step
# вторую ногу этих типов СОБИРАЕТ (кирпичи те же), но отправить её не может: у
# него нет НАШЕЙ таблицы адресов, и пакет выходит за 1232 байта на ВСЕХ живых
# сделках. Эти числа -- замер его же функцией собрать() на тех же образцах и с
# тем же шаблоном первой ноги. У кривой он отказывает раньше -- на цене: её
# котировщик (c2_swap_build.bonding_min_out) считает лампортами, а на токеновой
# котировке обязательная часть события пуста по SOL.
ZHDEM_DVUHSHAGOVYJ = {
    PROG_CPMM: {"razmer": (1453, 1485), "pochemu": "при пределе сети"},
    PROG_LAUNCHLAB: {"razmer": (1562, 1594), "pochemu": "при пределе сети"},
    PROG_PUMP_AMM: {"razmer": (1562, 1594), "pochemu": "при пределе сети"},
    PROG_KRIVAYA: {"razmer": None,
                   "pochemu": "минимум шага 2 не выдаётся"},
}
# ПРОДАЖА: РАЗМЕР ПАКЕТА токен -> USDC -> SOL, с нашей таблицей и с чаевыми.
# Восемь инструкций: cu_limit, cu_price, ata(USDC), ata(WSOL), продажа ноги 1,
# продажа ноги 2, закрытие счёта токена, закрытие счёта WSOL (+ чаевые).
ZHDEM_PRODAZHI = {
    PROG_CPMM: {"verh": (1083, 1083), "usdc": (1021, 1052)},
    PROG_LAUNCHLAB: {"verh": (1192, 1192), "usdc": (1130, 1161)},
    PROG_PUMP_AMM: {"verh": (1190, 1190), "usdc": (1128, 1159)},
    PROG_KRIVAYA: {"verh": (1383, 1383), "usdc": (1352, 1352)},
}
# ПРОДАЖА КРИВОЙ НЕ ВЛЕЗАЕТ ДАЖЕ С ЧЕТЫРЬМЯ ДОБАВЛЕННЫМИ АДРЕСАМИ, и выбор тут
# не мой: либо десять адресов в таблице, либо не закрывать токеновый счёт той же
# транзакцией (рента остаётся на счёте и забирается потом). Числа -- замер.
ZHDEM_PRODAZHA_KRIVOJ = {"4_zakryvaem": 1259, "10_zakryvaem": 1166,
                         "4_bez_zakrytiya": 1189, "10_bez_zakrytiya": 1096}
KOSHELEK_PROVERKI = "D3JuFoSXuWEMUUdCtoB5NYWnN87vjJSHtDP5rTD6qnph"
DRUGOJ_KOSHELEK = "4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x"
# Живой образец кривой с котировочным минтом -- тот, на котором измерены места и
# арифметика. Подпись названа, чтобы проверку можно было повторить руками.
OBRAZEC_KRIVOJ = "3bn6wPKuLm62t7nqfK8u9iABz8zpAQQhLhznZiPSsRN8nasZj7Sizo2gJxT55i2ptPvfvatuuQGdJm7wZQb8dWDe"
KRIVAYA_RASHOZHDENIE = 875_307
KRIVAYA_VYHOD_SOBYTIYA = 26_277_828_094_315


def _obrazcy(programma: str) -> list:
    """Живые сделки типа из образцов репозитория -- теми же глазами, что у полосы."""
    _C, B, _UN, _TS, _SB, _K = _moduli()
    out = []
    for r in B.load_samples(programma):
        tx, vault = r.get("tx"), r.get("pool_vault")
        if not isinstance(tx, dict) or not vault:
            continue
        shab = B.extract_template(tx, programma, vault)
        if not shab.get("ok"):
            continue
        mv = B.mints_and_vaults(shab, tx) or {}
        q = mv.get("quote_mint")
        if (B.spec_of(shab) or {}).get("native_quote") or q in (None, WSOL):
            continue
        out.append({"tx": tx, "vault": vault, "mint": r.get("mint") or mv.get("base_mint"),
                    "quote": q, "quote_program": mv.get("quote_program"),
                    "source": r.get("source"), "tpl": shab})
    return out


def _zapis_nogi_1():
    """Статичный шаблон первой ноги SOL -> USDC из репозитория. Без сети."""
    import bloom_nogi_shablony as NSH  # noqa: PLC0415
    C, _B, _UN, _TS, _SB, _K = _moduli()
    put_ = Path(C.DATA) / "nogi_shablony.json"
    if not put_.exists():
        return None
    obr = (json.loads(put_.read_text(encoding="utf-8")).get("shablony") or {}).get(USDC)
    if not obr:
        return None
    zap = NSH.запись_из_образца(USDC, obr)
    if not isinstance(zap, dict):
        return None
    return dict(zap, price_sol=zap["price_sol_obrazca"], checked_at=time.time())


def _lut_s_dobavkoj(dobavka=()):
    from solders.address_lookup_table_account import AddressLookupTableAccount  # noqa: PLC0415
    from solders.pubkey import Pubkey  # noqa: PLC0415
    t = tablica_polosy()
    adr = list(dict.fromkeys(list(t["adresa"]) + [x for x in dobavka if x]))
    return AddressLookupTableAccount(
        Pubkey.from_string(t["klyuch"]),
        [Pubkey.from_string(a) for a in adr]), len(adr)


def self_test() -> int:  # noqa: C901, PLR0912, PLR0915
    import os  # noqa: PLC0415

    C, B, UN, TS, SB, K = _moduli()
    было, плохо = 0, 0

    def chk(имя, усл, факт=None):
        nonlocal было, плохо
        было += 1
        if усл:
            print(f"  ok   {имя}")
        else:
            плохо += 1
            print(f" ПЛОХО {имя} -- {факт!r}")

    print("c3_usdc_noga_signaly: самопроверка")

    # ------------------------------------------------- 1. таблица типов
    chk("типов в таблице четыре -- те, куда идут USDC-сигналы", len(TIPY) == 4,
        len(TIPY))
    chk("у каждого типа назван котировщик",
        all(kotirovshchik(p) for p in TIPY), [p for p in TIPY if not kotirovshchik(p)])
    chk("сумма сигналов по типам -- 114 из 118 (счёт Code-1)",
        sum(SIGNALOV_V_SUTKI.values()) == 114, sum(SIGNALOV_V_SUTKI.values()))
    chk("ни одного типа c2_usdc_noga здесь не повторено",
        not (set(TIPY) & set(UN.TYPES)), sorted(set(TIPY) & set(UN.TYPES)))
    ст = storona({}, programma="НеПрограмма", pul="x")
    chk("незнакомый тип -- отказ по имени, а не исключение",
        not ст["ok"] and WHY_TIP in (ст["why_not"] or ""), ст.get("why_not"))
    ст = storona({}, programma=PROG_CPMM)
    chk("без хранилища и пула -- отказ словами",
        not ст["ok"] and "ни хранилища" in (ст["why_not"] or ""), ст.get("why_not"))
    chk("режим -- тот же флаг, что у USDC-ноги", rezhim() == UN.rezhim(),
        (rezhim(), UN.rezhim()))
    chk("предел CU -- тот же, что у USDC-ноги", cu_nogi() == UN.cu_nogi(),
        (cu_nogi(), UN.cu_nogi()))

    # ------------------------------------------------- 2. таблица адресов полосы
    т = tablica_polosy()
    chk(f"таблица адресов полосы читается из data/{FAJL_TABLICY}", т["ok"],
        т.get("why_not"))
    chk(f"адресов в таблице {ADRESOV_V_TABLICE}",
        т["ok"] and len(т["adresa"]) == ADRESOV_V_TABLICE,
        len(т["adresa"]) if т["ok"] else None)
    chk("ключ таблицы -- тот, что создан и сверен по цепи Code-1",
        т["ok"] and т["klyuch"] == TABLICA_ADRESOV, т.get("klyuch"))
    нет_в_таблице = [TIPY[p]["label"] for p in TIPY if т["ok"] and p not in т["adresa"]]
    chk("программы всех четырёх типов уже лежат в таблице полосы",
        т["ok"] and not нет_в_таблице, нет_в_таблице)
    chk("минт USDC лежит в таблице полосы", т["ok"] and USDC in т["adresa"], None)

    # ------------------------------------------------- 3. по типам на живых сделках
    байт_всего = 0
    образцы_по_типам = {}
    for p, ждём in ZHDEM_PO_TIPAM.items():
        метка = TIPY[p]["label"]
        обр = _obrazcy(p)
        образцы_по_типам[p] = обр
        chk(f"{метка}: живых сделок с котировочным токеном {ждём['obrazcov']}",
            len(обр) == ждём["obrazcov"], len(обр))
        сторона_ок, байт, целиком, цена, отказ_цены = 0, 0, 0, 0, 0
        вне_наших, причины = [], []
        for о in обр:
            ст = storona(о["tx"], programma=p, hranilishche=о["vault"],
                         mint_kotirovki=о["quote"])
            if not ст["ok"]:
                причины.append(ст["why_not"])
                continue
            сторона_ок += 1
            u = polzovatel_istochnika(ст, о["tx"])
            if not u:
                вне_наших.append(("пользователь источника не найден", None))
                continue
            a0, a1 = struct.unpack_from("<QQ", ст["tpl"]["data"], 8)
            наш = instrukciya_nogi(ст, user=u, amount_in=a0, min_out=a1,
                                   tx_istochnika=о["tx"], kak_u_istochnika=True)
            другой = instrukciya_nogi(ст, user=DRUGOJ_KOSHELEK, amount_in=a0,
                                      min_out=a1, tx_istochnika=о["tx"],
                                      kak_u_istochnika=True)
            получили = [str(м.pubkey) for м in наш.accounts]
            иначе = [str(м.pubkey) for м in другой.accounts]
            его = list(ст["tpl"]["accounts"])
            наши_места = {и for и, (x, y) in enumerate(zip(получили, иначе)) if x != y}
            разн = [и for и, (x, y) in enumerate(zip(его, получили)) if x != y]
            вне = [и for и in разн if и not in наши_места]
            данные = bytes(наш.data) == bytes(ст["tpl"]["data"])
            if данные and not вне:
                байт += 1
                if not разн:
                    целиком += 1
            else:
                вне_наших.append((C.first_signature(о["tx"])[:10], (данные, вне)))
            мо = min_out_boj(ст, о["tx"], amount_in=1_000_000, proskalzyvanie=0.35,
                             mint_kotirovki=о["quote"])
            if мо["ok"]:
                цена += 1
            else:
                отказ_цены += 1
                причины.append(мо["why_not"])
        байт_всего += байт
        chk(f"{метка}: сторона USDC разобралась на {ждём['storona']}",
            сторона_ок == ждём["storona"], (сторона_ок, причины[:2]))
        chk(f"{метка}: байт в байт на {ждём['bajt']}", байт == ждём["bajt"],
            (байт, вне_наших[:2]))
        chk(f"{метка}: ни одного расхождения ВНЕ наших мест", not вне_наших,
            вне_наших[:2])
        chk(f"{метка}: цена посчиталась на {ждём['cena']}", цена == ждём["cena"],
            цена)
        chk(f"{метка}: отказов цены {ждём['otkaz_ceny']}, и у каждого есть причина",
            отказ_цены == ждём["otkaz_ceny"] and all(причины), отказ_цены)
    chk(f"байт в байт всего {ZHDEM_BAJT_VSEGO} живых сделок",
        байт_всего == ZHDEM_BAJT_VSEGO, байт_всего)

    # ------------------------------------------------- 4. места кривой v2
    кр = образцы_по_типам.get(PROG_KRIVAYA) or []
    chk("живой образец кривой с котировочным минтом на месте", len(кр) == 1, len(кр))
    if кр:
        о = кр[0]
        chk("это тот самый образец, на котором измерены места",
            C.first_signature(о["tx"]) == OBRAZEC_KRIVOJ, C.first_signature(о["tx"]))
        ст = storona(о["tx"], programma=PROG_KRIVAYA, hranilishche=о["vault"],
                     mint_kotirovki=о["quote"])
        сч = ст["tpl"]["accounts"]
        u = сч[KRIVAYA_V2_MESTA["user"]]
        пр = mesta_krivoj_v2(сч, nash_koshelek=u)
        chk("места кривой v2 вывелись и сошлись с покупкой источника 5 из 5",
            пр["ok"] and пр["soshlos"] == 5 and пр["provereno"] == 5,
            (пр.get("soshlos"), пр.get("provereno"), пр.get("why_not")))
        # СВЯЗАННЫЙ НАКОПИТЕЛЬ -- ОТДЕЛЬНОЙ ПРОВЕРКОЙ, ПОТОМУ ЧТО В ОБЩЕМ МОДУЛЕ
        # НАПИСАНО, ЧТО ОН НЕ ВЫВОДИТСЯ. Выводится: ATA накопителя по минту котировки.
        uva = B.pda([SEMYA_UVA, "USER"], u, PROG_KRIVAYA)
        chk("место 20 -- PDA(user_volume_accumulator + кошелёк)",
            uva == сч[KRIVAYA_V2_MESTA["uva"]], (uva, сч[KRIVAYA_V2_MESTA["uva"]]))
        асс = B.ata(uva, сч[KRIVAYA_V2_MESTA["mint_kotirovki"]],
                    сч[KRIVAYA_V2_MESTA["prog_kotirovki"]])
        chk("место 21 -- ATA(место 20, минт котировки, программа котировки), а не состояние",
            асс == сч[KRIVAYA_V2_MESTA["assoc_uva"]],
            (асс, сч[KRIVAYA_V2_MESTA["assoc_uva"]]))
        # КИРПИЧ НАШИМ КОШЕЛЬКОМ МЕСТ НЕ ВОССТАНАВЛИВАЕТ -- ПОТОМУ И СВОИ. На
        # кошельке САМОГО ИСТОЧНИКА он справляется: место 21 он просто переносит
        # из его же сделки. А нашим -- идёт за ключом в состояние
        # (c2_swap_build.assoc_uva), и для USDC такого состояния у нас нет.
        chk("кирпич НАШИМ кошельком мест кривой v2 не восстанавливает",
            not B.user_accounts(ст["tpl"], о["tx"], KOSHELEK_PROVERKI),
            B.user_accounts(ст["tpl"], о["tx"], KOSHELEK_PROVERKI))
        наши5 = mesta_krivoj_v2(сч, nash_koshelek=KOSHELEK_PROVERKI)
        chk("а свои -- восстанавливает все пять, без состояния и без чтений",
            наши5["ok"] and len(наши5["mesta"]) == 5, наши5.get("why_not"))
        # ЦЕНА: СОБЫТИЕ ВОСПРОИЗВЕЛО СВОЮ СДЕЛКУ КОТИРОВОЧНОЙ СТОРОНОЙ
        ев = sobytie_krivoj(о["tx"], о["mint"])
        chk("событие кривой разобралось и котировка в нём токеновая",
            ев["ok"] and ев["tokenovaya"] and ев["quote_mint"] == о["quote"],
            (ев.get("why_not"), ев.get("quote_mint")))
        if ев["ok"]:
            п = ев["polya"]
            до = int(п["virtual_quote_reserves"]) - int(п["quote_amount"])
            вт = int(п["virtual_token_reserves"]) + int(п["token_amount"])
            трата = int(п["quote_amount"]) + int(п["fee"]) + int(п["creator_fee"])
            вх = K.вход_кривой(котировки=трата, вирт_котировка=до, вирт_токены=вт,
                               fee_bps=int(п["fee_basis_points"]),
                               creator_bps=int(п["creator_fee_basis_points"]))
            chk("до кривой доходит РОВНО quote_amount сделки источника",
                вх["ok"] and вх["до_кривой"] == int(п["quote_amount"]),
                (вх.get("до_кривой"), п["quote_amount"]))
            chk("комиссии сошлись с ceil(quote * bps) до единицы",
                вх["комиссия"] == int(п["fee"])
                and вх["комиссия_создателя"] == int(п["creator_fee"]),
                (вх.get("комиссия"), п["fee"], вх.get("комиссия_создателя"),
                 п["creator_fee"]))
            chk(f"кривая воспроизвела выход с расхождением {KRIVAYA_RASHOZHDENIE}",
                вх["выход"] - KRIVAYA_VYHOD_SOBYTIYA == KRIVAYA_RASHOZHDENIE,
                вх["выход"] - KRIVAYA_VYHOD_SOBYTIYA)
            chk("расхождение меньше допуска 1e-6 от выхода",
                KRIVAYA_RASHOZHDENIE * 1_000_000 < KRIVAYA_VYHOD_SOBYTIYA, None)
            ст_чужая = min_out_boj(ст, о["tx"], amount_in=1_000_000,
                                   proskalzyvanie=0.35, mint_kotirovki=USDC)
            chk("котировка кривой не тем минтом, которым платит нога -- отказ по имени",
                not ст_чужая["ok"]
                and WHY_KOTIROVKA_SOBYTIYA in (ст_чужая["why_not"] or ""),
                ст_чужая.get("why_not"))
    # 18-счётная разновидность -- отказ по имени
    v1 = [r for r in B.load_samples(PROG_KRIVAYA)
          if isinstance(r.get("tx"), dict)
          and (B.extract_template(r["tx"], PROG_KRIVAYA, r.get("pool_vault")) or {})
          .get("ix") in ("66063d1201daebea", "38fc74089edfcd5f")]
    chk("18-счётные покупки кривой в образцах есть", len(v1) >= 5, len(v1))
    if v1:
        ст1 = storona(v1[0]["tx"], programma=PROG_KRIVAYA,
                      hranilishche=v1[0]["pool_vault"], mint_kotirovki=USDC)
        chk("кривая с нативной котировкой -- отказ по имени, а не сборка",
            not ст1["ok"] and WHY_KRIVAYA_V1 in (ст1["why_not"] or ""),
            ст1.get("why_not"))

    # ------------------------------------------------- 5. переворот сторон CPMM
    цп = образцы_по_типам.get(PROG_CPMM) or []
    if цп:
        о = цп[0]
        ст_п = storona(о["tx"], programma=PROG_CPMM, hranilishche=о["vault"],
                       mint_kotirovki=о["mint"])
        chk("CPMM: просим котировку с выходной стороны -- стороны переставились",
            ст_п["ok"] and ст_п["razvernut"], (ст_п.get("ok"), ст_п.get("why_not")))
        if ст_п["ok"]:
            chk("CPMM: после переворота на месте 10 стоит та котировка, что просили",
                ст_п["tpl"]["accounts"][10] == о["mint"],
                ст_п["tpl"]["accounts"][10])
            дважды = TS.перевернуть_cpmm(ст_п["tpl"], о["quote"])
            chk("CPMM: два переворота -- тот же список счетов (инволюция)",
                list(дважды["accounts"]) == list(о["tpl"]["accounts"]), None)
            мо = min_out_boj(ст_п, о["tx"], amount_in=1_000_000,
                             proskalzyvanie=0.35, mint_kotirovki=о["mint"])
            chk("CPMM развёрнутый: цена ОТКАЗЫВАЕТ по имени (обратный своп)",
                not мо["ok"] and WHY_OTHER_SIDE in (мо["why_not"] or ""),
                мо.get("why_not"))
    # Pump AMM и LaunchLab переворачивать нечем -- отказ по имени
    лл = образцы_по_типам.get(PROG_LAUNCHLAB) or []
    if лл:
        ст_л = storona(лл[0]["tx"], programma=PROG_LAUNCHLAB,
                       hranilishche=лл[0]["vault"], mint_kotirovki=лл[0]["mint"])
        chk("LaunchLab: обратная сторона -- отказ по имени, переворота там нет",
            not ст_л["ok"] and WHY_NO_REVERSE in (ст_л["why_not"] or ""),
            ст_л.get("why_not"))

    # ------------------------------------------------- 6. список инструкций и размер
    зап = _zapis_nogi_1()
    chk("статичный шаблон первой ноги SOL -> USDC в репозитории есть",
        isinstance(зап, dict) and зап.get("q_dec") == 6
        and зап.get("program") == PROG_PUMP_AMM,
        (зап or {}).get("program"))
    сохр = {k: os.environ.get(k) for k in (UN.FLAG, UN.FLAG_GROUPS)}
    os.environ[UN.FLAG] = UN.MODE_LIVE
    os.environ.pop(UN.FLAG_GROUPS, None)
    try:
        порядок_ок, аргументы_ок, размеры = 0, 0, {}
        for p, обр in образцы_по_типам.items():
            размеры[p] = {"verh": [], "usdc": []}
            for о in обр:
                кэш = SB.LegCache({}, None)
                # В КЭШ ПОЛОЖЕН РЕАЛЬНЫЙ ШАБЛОН НОГИ SOL -> USDC ПОД КЛЮЧОМ
                # КОТИРОВКИ ОБРАЗЦА. Это замер РАЗМЕРА и ПОРЯДКА, а не цены:
                # экономической связки между ногами тут нет (котировки разные), и
                # она проверяется отдельно -- на USDC-образцах, которых по этим
                # четырём типам ещё нет. Размер от минта котировки не зависит
                # ничем, кроме попадания адреса в таблицу, и это посчитано ниже
                # отдельной строкой ("usdc").
                кэш.entries[о["quote"]] = зап
                ист = о["source"] or (sorted(C.signers(о["tx"]))[0]
                                      if C.signers(о["tx"]) else None)
                рез = instrukcii(tx_istochnika=о["tx"], istochnik=ист, mint=о["mint"],
                                 nash_koshelek=KOSHELEK_PROVERKI, lamporty=10_000_000,
                                 kesh_nog=кэш, proskalzyvanie=0.35,
                                 chaevye_lamporty=0, chaevye_adres=None,
                                 mint_kotirovki=о["quote"], min_out_vneshnij=1)
                if not рез["ok"]:
                    continue
                ixs = рез["ixs"]
                ждём_прог = ["ComputeBudget111111111111111111111111111111",
                             "ComputeBudget111111111111111111111111111111",
                             B.ATA_PROGRAM, B.ATA_PROGRAM, B.ATA_PROGRAM,
                             B.SYSTEM, (зап["mv"] or {}).get("quote_program"),
                             зап["program"], рез["pool_program"]]
                if len(ixs) == 9 and [str(i.program_id) for i in ixs] == ждём_прог \
                        and bytes(ixs[0].data)[:1] == b"\x02" \
                        and bytes(ixs[1].data)[:1] == b"\x03" \
                        and all(bytes(ixs[j].data) == b"\x01" for j in (2, 3, 4)) \
                        and bytes(ixs[6].data)[:1] == b"\x11":
                    порядок_ок += 1
                l1 = struct.unpack_from("<QQ", bytes(ixs[7].data), 8)
                l2 = struct.unpack_from("<QQ", bytes(ixs[8].data), 8)
                if l1 == (10_000_000, рез["leg1_min_out"]) \
                        and l2 == (рез["leg2_amount_in"], рез["min_out"]) \
                        and рез["leg2_amount_in"] == рез["leg1_min_out"]:
                    аргументы_ок += 1
                нашлось = B.ata(KOSHELEK_PROVERKI, о["quote"], о["quote_program"])
                for имя, добавка in (("verh", ()),
                                     ("usdc", (о["quote"], нашлось,
                                               о["quote_program"]))):
                    L, _n = _lut_s_dobavkoj(добавка)
                    сб = sobrat(tx_istochnika=о["tx"], istochnik=ист, mint=о["mint"],
                                nash_koshelek=KOSHELEK_PROVERKI, lamporty=10_000_000,
                                kesh_nog=кэш, proskalzyvanie=0.35,
                                chaevye_lamporty=1_000_000,
                                chaevye_adres=KOSHELEK_PROVERKI,
                                min_out_vneshnij=1, mint_kotirovki=о["quote"],
                                luts_gotovye=[L])
                    if isinstance(сб.get("size"), int):
                        размеры[p][имя].append(сб["size"])
        собралось = sum(len(v["verh"]) for v in размеры.values())
        chk("порядок инструкций -- девять, место в место как у c2_usdc_noga",
            порядок_ок == собралось and собралось > 0, (порядок_ок, собралось))
        chk("суммы связаны: вход второй ноги -- это минимум первой",
            аргументы_ок == собралось, (аргументы_ок, собралось))
        for p, ждём in ZHDEM_RAZMEROV.items():
            метка = TIPY[p]["label"]
            for имя, (мин, макс) in ждём.items():
                ряд = размеры[p][имя]
                chk(f"{метка}: размер ({имя}) {мин}...{макс} байт",
                    ряд and min(ряд) == мин and max(ряд) == макс,
                    (min(ряд), max(ряд)) if ряд else None)
            влезли = [x for x in размеры[p]["usdc"] if x <= TS.ПРЕДЕЛ_РАЗМЕРА_TX]
            if p == PROG_KRIVAYA:
                chk("кривая v2: с таблицей полосы пакет НЕ влезает в 1232",
                    not влезли, размеры[p]["usdc"])
            else:
                chk(f"{метка}: с таблицей полосы пакет влезает в 1232 целиком",
                    len(влезли) == len(размеры[p]["usdc"]) and влезли,
                    (len(влезли), len(размеры[p]["usdc"])))
        # ЛЕКАРСТВО КРИВОЙ -- ЧИСЛАМИ
        if кр:
            о = кр[0]
            кэш = SB.LegCache({}, None)
            кэш.entries[о["quote"]] = зап
            ист = о["source"] or sorted(C.signers(о["tx"]))[0]
            ст = storona(о["tx"], programma=PROG_KRIVAYA, hranilishche=о["vault"],
                         mint_kotirovki=о["quote"])
            сч = ст["tpl"]["accounts"]
            мест = mesta_krivoj_v2(сч, nash_koshelek=KOSHELEK_PROVERKI)["mesta"]
            как_usdc = [о["quote"],
                        B.ata(KOSHELEK_PROVERKI, о["quote"], о["quote_program"]),
                        о["quote_program"]]
            наборы = {
                4: [сч[i] for i in KRIVAYA_MESTA_POSTOYANNYH],
                6: [сч[i] for i in KRIVAYA_MESTA_POSTOYANNYH]
                + [мест[20], мест[21]],
                10: [сч[i] for i in KRIVAYA_MESTA_POSTOYANNYH]
                + [мест[20], мест[21]] + [сч[i] for i in (6, 7, 8, 9)],
            }
            for сколько, добавка in наборы.items():
                L, _n = _lut_s_dobavkoj(как_usdc + добавка)
                сб = sobrat(tx_istochnika=о["tx"], istochnik=ист, mint=о["mint"],
                            nash_koshelek=KOSHELEK_PROVERKI, lamporty=10_000_000,
                            kesh_nog=кэш, proskalzyvanie=0.35,
                            chaevye_lamporty=1_000_000,
                            chaevye_adres=KOSHELEK_PROVERKI, min_out_vneshnij=1,
                            mint_kotirovki=о["quote"], luts_gotovye=[L])
                ждём_б = ZHDEM_KRIVAYA_S_DOBAVKOJ[сколько]
                chk(f"кривая v2: +{сколько} адресов в таблице -> {ждём_б} байт",
                    сб.get("size") == ждём_б, сб.get("size"))
                chk(f"кривая v2: с +{сколько} адресами пакет влезает в 1232",
                    isinstance(сб.get("size"), int)
                    and сб["size"] <= TS.ПРЕДЕЛ_РАЗМЕРА_TX, сб.get("size"))
    finally:
        for k, v in сохр.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ------------------------------------------- 7. продажа токен -> USDC -> SOL
    зап2 = _zapis_nogi_1()
    н2 = noga_2_prodazhi(зап2 or {})
    chk("продажа второй ноги: зеркало нашей покупки ноги собралось",
        н2["ok"], н2.get("why_not"))
    chk("её покупка 25-счётная, продажа -- 23 счёта (места 19 и 20 выброшены)",
        н2["ok"] and н2["schetov_pokupki"] == 25 and len(н2["accounts"]) == 23,
        (н2.get("schetov_pokupki"), len(н2.get("accounts") or [])))
    chk("23-счётная продажа Pump AMM живым образцом НЕ проверена -- и это сказано",
        н2.get("zhivym_obrazcom_ne_provereno") is True,
        н2.get("zhivym_obrazcom_ne_provereno"))
    chk("выброшены РОВНО места 19 и 20 покупки ноги",
        н2["ok"] and н2["accounts"] == [a for i, a in enumerate(зап2["tpl"]["accounts"])
                                        if i not in PUMP_AMM_MESTA_TOLKO_POKUPKI],
        None)
    os.environ[UN.FLAG] = UN.MODE_LIVE
    try:
        for p, ждём in ZHDEM_PRODAZHI.items():
            метка = TIPY[p]["label"]
            ряды = {"verh": [], "usdc": []}
            порядок, суммы = 0, 0
            без_мин = 0
            for о in образцы_по_типам.get(p) or []:
                кэш = SB.LegCache({}, None)
                кэш.entries[о["quote"]] = зап2
                общие = {"tx_pokupki": о["tx"], "programma": p,
                         "nash_koshelek": KOSHELEK_PROVERKI,
                         "ostatok": 1_000_000_000, "kesh_nog": кэш,
                         "mint_bazy": о["mint"], "hranilishche": о["vault"],
                         "mint_kotirovki": о["quote"]}
                без = prodazha_instrukcii(**общие)
                if not без["ok"] and WHY_NOGA2_NET_MIN in (без["why_not"] or ""):
                    без_мин += 1
                рез = prodazha_instrukcii(**общие, min_out_nogi_2=1)
                if not рез["ok"]:
                    continue
                ixs = рез["ixs"]
                ждём_прог = ["ComputeBudget111111111111111111111111111111",
                             "ComputeBudget111111111111111111111111111111",
                             B.ATA_PROGRAM, B.ATA_PROGRAM, p, PROG_PUMP_AMM]
                if len(ixs) == 8 \
                        and [str(i.program_id) for i in ixs[:6]] == ждём_прог \
                        and bytes(ixs[6].data)[:1] == b"\x09" \
                        and bytes(ixs[7].data)[:1] == b"\x09":
                    порядок += 1
                if рез["noga_2_amount_in"] == рез["min_out_nogi_1"]:
                    суммы += 1
                нашлось = B.ata(KOSHELEK_PROVERKI, о["quote"], о["quote_program"])
                for имя, добавка in (("verh", ()),
                                     ("usdc", (о["quote"], нашлось,
                                               о["quote_program"]))):
                    L, _n = _lut_s_dobavkoj(добавка)
                    сб = prodazha_sobrat(**общие, min_out_nogi_2=1,
                                         luts_gotovye=[L],
                                         chaevye_lamporty=1_000_000,
                                         chaevye_adres=KOSHELEK_PROVERKI)
                    if isinstance(сб.get("size"), int):
                        ряды[имя].append(сб["size"])
            chk(f"{метка}: продажа собралась на всех живых сделках",
                порядок == len(образцы_по_типам[p]) and порядок > 0,
                (порядок, len(образцы_по_типам[p])))
            chk(f"{метка}: вход второй ноги продажи -- это минимум первой",
                суммы == len(образцы_по_типам[p]), суммы)
            chk(f"{метка}: без минимума второй ноги -- отказ по имени, а не продажа",
                без_мин == len(образцы_по_типам[p]), без_мин)
            for имя, (мин, макс) in ждём.items():
                ряд = ряды[имя]
                chk(f"{метка}: размер продажи ({имя}) {мин}...{макс} байт",
                    ряд and min(ряд) == мин and max(ряд) == макс,
                    (min(ряд), max(ряд)) if ряд else None)
            влезли = [x for x in ряды["usdc"] if x <= TS.ПРЕДЕЛ_РАЗМЕРА_TX]
            if p == PROG_KRIVAYA:
                chk("кривая v2: продажа с таблицей полосы НЕ влезает в 1232",
                    not влезли, ряды["usdc"])
            else:
                chk(f"{метка}: продажа влезает в 1232 на всех живых сделках",
                    len(влезли) == len(ряды["usdc"]) and влезли,
                    (len(влезли), len(ряды["usdc"])))
        # ЛЕКАРСТВО ПРОДАЖИ КРИВОЙ -- ЧИСЛАМИ, И ВЫБОР НЕ МОЙ
        if кр:
            о = кр[0]
            кэш = SB.LegCache({}, None)
            кэш.entries[о["quote"]] = зап2
            ст = storona(о["tx"], programma=PROG_KRIVAYA, hranilishche=о["vault"],
                         mint_kotirovki=о["quote"])
            сч = ст["tpl"]["accounts"]
            мест = mesta_krivoj_v2(сч, nash_koshelek=KOSHELEK_PROVERKI)["mesta"]
            как_usdc = [о["quote"],
                        B.ata(KOSHELEK_PROVERKI, о["quote"], о["quote_program"]),
                        о["quote_program"]]
            п4 = [сч[i] for i in KRIVAYA_MESTA_POSTOYANNYH]
            п10 = п4 + [мест[20], мест[21]] + [сч[i] for i in (6, 7, 8, 9)]
            for имя, добавка, закрывать in (("4_zakryvaem", п4, True),
                                            ("10_zakryvaem", п10, True),
                                            ("4_bez_zakrytiya", п4, False),
                                            ("10_bez_zakrytiya", п10, False)):
                L, _n = _lut_s_dobavkoj(как_usdc + добавка)
                сб = prodazha_sobrat(
                    tx_pokupki=о["tx"], programma=PROG_KRIVAYA,
                    nash_koshelek=KOSHELEK_PROVERKI, ostatok=1_000_000_000,
                    kesh_nog=кэш, mint_bazy=о["mint"], hranilishche=о["vault"],
                    mint_kotirovki=о["quote"], min_out_nogi_2=1,
                    zakryvat_schet_tokena=закрывать, luts_gotovye=[L],
                    chaevye_lamporty=1_000_000, chaevye_adres=KOSHELEK_PROVERKI)
                ждём_б = ZHDEM_PRODAZHA_KRIVOJ[имя]
                chk(f"кривая v2: продажа ({имя}) {ждём_б} байт",
                    сб.get("size") == ждём_б, сб.get("size"))
            chk("кривая v2: продажа влезает либо с десятью адресами, либо без "
                "закрытия токенового счёта",
                ZHDEM_PRODAZHA_KRIVOJ["10_zakryvaem"] <= TS.ПРЕДЕЛ_РАЗМЕРА_TX
                and ZHDEM_PRODAZHA_KRIVOJ["4_bez_zakrytiya"] <= TS.ПРЕДЕЛ_РАЗМЕРА_TX
                and ZHDEM_PRODAZHA_KRIVOJ["4_zakryvaem"] > TS.ПРЕДЕЛ_РАЗМЕРА_TX,
                ZHDEM_PRODAZHA_KRIVOJ)
    finally:
        for k, v in сохр.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ------------------------------- 8. почему не двухшаговым путём (числом)
    for p, ждём in ZHDEM_DVUHSHAGOVYJ.items():
        метка = TIPY[p]["label"]
        ряд, причины = [], []
        for о in образцы_по_типам.get(p) or []:
            кэш = SB.LegCache({}, None)
            кэш.entries[о["quote"]] = зап
            ист = о["source"] or (sorted(C.signers(о["tx"]))[0]
                                  if C.signers(о["tx"]) else None)
            дв = TS.собрать(tx_источника=о["tx"], источник=ист, минт=о["mint"],
                            наш_кошелёк=KOSHELEK_PROVERKI, лампорты=10_000_000,
                            кэш_ног=кэш, чаевые_лампорты=0, чаевые_адрес=None,
                            rpc_call=lambda _m, _p: {"value": []})
            причины.append(дв.get("why_not") or "")
            if isinstance(дв.get("size"), int):
                ряд.append(дв["size"])
            chk_ok = not дв.get("ok")
            if not chk_ok:
                причины.append("СОБРАЛОСЬ -- тогда этот модуль не нужен")
        chk(f"{метка}: двухшаговым путём не отправляется ни одна живая сделка",
            причины and all(ждём["pochemu"] in с for с in причины), причины[:2])
        if ждём["razmer"]:
            chk(f"{метка}: его пакет {ждём['razmer'][0]}...{ждём['razmer'][1]} байт "
                f"-- без нашей таблицы",
                ряд and (min(ряд), max(ряд)) == ждём["razmer"],
                (min(ряд), max(ряд)) if ряд else None)

    # ------------------------------------------------- 9. режимы
    os.environ[UN.FLAG] = UN.MODE_OFF
    try:
        сб = sobrat(tx_istochnika={}, istochnik="x", mint="y",
                    nash_koshelek=KOSHELEK_PROVERKI, lamporty=1, kesh_nog=None)
        chk("флаг выключен -- сборки нет и причина названа",
            not сб["ok"] and сб["why_not"] == UN.WHY_OFF, сб.get("why_not"))
        os.environ[UN.FLAG] = UN.MODE_SHADOW
        сб = sobrat(tx_istochnika={}, istochnik="x", mint="y",
                    nash_koshelek=KOSHELEK_PROVERKI, lamporty=1, kesh_nog=None)
        chk("режим тени -- транзакция на отправку не собирается",
            not сб["ok"] and сб["why_not"] == UN.WHY_SHADOW, сб.get("why_not"))
    finally:
        for k, v in сохр.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    рез = instrukcii(tx_istochnika={}, istochnik="x", mint="y",
                     nash_koshelek=KOSHELEK_PROVERKI, lamporty=1, kesh_nog=None)
    chk("без кэша первой ноги -- отказ словами, а не исключение",
        not рез["ok"] and "кэша шаблонов" in (рез["why_not"] or ""),
        рез.get("why_not"))

    print(f"\nпроверок {было}, ждали {ZHDEM_PROVEROK}, не прошло {плохо}")
    if было != ZHDEM_PROVEROK:
        print(" ПЛОХО число проверок разошлось с объявленным -- "
              "молчаливый пропуск считается провалом")
        return 1
    return 1 if плохо else 0


def main() -> int:
    import argparse  # noqa: PLC0415
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--self-test", action="store_true", default=True)
    p.add_argument("--zamer", metavar="ФАЙЛ", default=None,
                   help="записать таблицу замера по типам в файл")
    a = p.parse_args()
    код = self_test()
    if a.zamer:
        put_ = Path(a.zamer)
        put_.parent.mkdir(parents=True, exist_ok=True)
        put_.write_text(json.dumps({
            "что": "USDC-нога для сигнальных типов: что проверено числами",
            "типы": {TIPY[p_]["label"]: {"программа": p_,
                                        "сигналов_в_сутки_Code1": SIGNALOV_V_SUTKI[p_],
                                        "котировщик": TIPY[p_]["kotirovshchik"],
                                        **ZHDEM_PO_TIPAM[p_],
                                        "размер": ZHDEM_RAZMEROV[p_]}
                      for p_ in TIPY},
            "байт_в_байт_всего": ZHDEM_BAJT_VSEGO,
            "продажа_токен_USDC_SOL": {
                TIPY[p_]["label"]: ZHDEM_PRODAZHI[p_] for p_ in TIPY},
            "продажа_кривой_выбор": ZHDEM_PRODAZHA_KRIVOJ,
            "продажа_ноги_2": {
                "как": "зеркало нашей покупки первой ноги без мест 19 и 20",
                "счетов": 23,
                "живым_образцом_не_проверена": True,
                "запрошено_у_Code2": "6+ живых продаж Pump AMM с 23 счетами"},
            "таблица_адресов": {"ключ": TABLICA_ADRESOV, "адресов": ADRESOV_V_TABLICE,
                                "кривой_не_хватает": "1249 байт при пределе 1232",
                                "лекарство": ZHDEM_KRIVAYA_S_DOBAVKOJ},
            "образец_кривой": OBRAZEC_KRIVOJ,
            "двухшаговым_путём_сейчас": {TIPY[p_]["label"]: ZHDEM_DVUHSHAGOVYJ[p_]
                                         for p_ in TIPY},
            "CU": {"предел_ноги_по_умолчанию": _moduli()[2].CU_NOGI_PO_UMOLCHANIYU,
                   "переменная": _moduli()[2].IMYA_FLAGA_CU,
                   "замер_Code1_две_ноги": list(_moduli()[2].CU_ZAMER_CODE1),
                   "здесь_не_мерено": (
                       "simulateTransaction не мой инструмент; у живых образцов в CU "
                       "входит весь чужой маршрут (88 869...541 083 CU на пакет "
                       "источника), чистых сделок «только своп» по этим четырём "
                       "типам в образцах нет")},
            "USDC_образцов_по_этим_типам": 0,
            "запрошено_у_Code2": "по 6+ живых USDC-свопов на каждый тип",
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"записано {put_}")
    return код


if __name__ == "__main__":
    raise SystemExit(main())
