#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""USDC-НОГА: вторая нога двухшаговой покупки на строителях (CLMM, DLMM, DAMM v2,
Whirlpool, AMM v4, DBC) -- вход USDC вместо WSOL. Флаг выключен, первый режим -- ТЕНЬ.

ЗАЧЕМ. Замер Code-2 по денежным группам 28--29.09: в сутки 9 сигналов с котировкой
USDC в пулах, которые полоса УЖЕ собирает (у CLMM 13 из 18), и все они сейчас
отвергаются одной строкой -- «ни один кандидат не парный к WSOL». Двухшаговый путь
(analysis/bloom_lane_two_step.py) закрывает такой сигнал только когда пул токена --
Raydium CPMM или Pump AMM: вторая нога там собирается кирпичами c2_swap_build, а у
CLMM, DLMM, DAMM v2, Whirlpool, AMM v4 и DBC свои строители со своими раскладками.
Здесь вторая нога обобщена на них.

МЕХАНИЗМ СВЯЗКИ СУММ -- НЕ НОВЫЙ, А ТОТ ЖЕ. Прочитан по коду bloom_lane_two_step
(собрать(), строки 156--390), не по памяти, и берётся ОТТУДА ЖЕ, а не копируется:

    ожидание_q   = лампорты / 1e9 / price_sol * 10**q_dec     (цена ноги из кэша)
    шаг2_вход    = int(ожидание_q * (1 - TS.ЗАПАС_ШАГА_1))    (ЗАПАС_ШАГА_1 = 0.05)
    у_нас_будет  = TS.после_налога(шаг2_вход, bps)            (min_out ноги 1)
    дойдёт       = TS.после_налога(у_нас_будет, bps)          (по нему -- min_out ноги 2)

Обе ноги -- ТОЧНЫЙ ВХОД (ExactIn), в ОДНОЙ транзакции: min_out первой ноги и есть
amount_in второй, поэтому вторая нога не может потратить больше, чем принесла
первая. У USDC (классический SPL Token) налога перевода не бывает -- bps = 0, и
тогда у_нас_будет == шаг2_вход == дойдёт; ветка налога всё равно идёт через
TS.налог_известен, чтобы два пути не расходились, если котировкой окажется
Token-2022. Инструкция «точного выхода» (exact_out) -- отказ, как и у двухшагового.

БИЛЕТ В SOL -> USDC ПО КОТИРОВКЕ ПЕРВОЙ НОГИ. Размер группы задан в SOL; сумма
второй ноги в USDC получается ровно тем же ожиданием_q по цене шаблона первой ноги
из кэша (SB.LegCache: price_sol -- SOL за ЦЕЛЫЙ котировочный токен, q_dec -- знаки
котировки). Своей цены USDC модуль не заводит: цена, по которой считается билет, и
цена, по которой первая нога обязана отдать min_out, -- одно число.

ПОРЯДОК ИНСТРУКЦИЙ -- ТОТ ЖЕ, ЧТО У ДВУХШАГОВОГО, МЕСТО В МЕСТО:
    advance_nonce?, cu_limit, cu_price, ata(WSOL), ata(USDC), ata(база),
    sol_transfer(кошелёк -> счёт WSOL), sync_native, своп ноги 1, своп ноги 2, чаевые
Пределы -- его же: TS.ПРЕДЕЛ_РАЗМЕРА_TX (1232 байта), cu_units по умолчанию 800 000
(полоса передаёт своё число из одной таблицы, BLOOM_LANE_CU_TWO_STEP).

MIN_OUT ВТОРОЙ НОГИ -- ПО ЦЕНЕ СОБЫТИЯ ИСТОЧНИКА, НУЛЬ ЧТЕНИЙ. Источник только что
прошёл по этому пулу: сколько USDC пришло на счета его инструкции и сколько токена
ушло из хранилища -- это и есть его цена с его комиссией. Та же цена применяется к
нашей сумме. Отсюда два следствия, и оба честные:
  * своп источника в ДРУГУЮ сторону (он продавал токен за USDC) -- ОТКАЗ по имени:
    обратную сторону как цену покупки брать нельзя (у DLMM и CLMM это другие
    корзины/тики и другая комиссия). Это не ошибка предсказания, а «сказать нечего»;
  * цена постоянная, наш объём в неё не входит -- значит на нашем размере она
    ЗАВЫШАЕТ выход, и min_out от неё бывает выше факта. Поэтому первый режим --
    тень, а боевой путь (rezhim boj) берёт min_out у САМОГО строителя через его
    подготовить(минт_котировки=USDC): там цена из живого состояния пула.

ЧТО ПРОВЕРЕНО ПО ТИПАМ -- ЧИСЛАМИ, и чего нет. Образцов Code-2
(data/podbivka/usdc_noga_dlya_code3.json) на момент сдачи НЕ СУЩЕСТВУЕТ: сборщик
analysis/podbivka_usdc_noga.py в его ветке есть, выход не записан. Поэтому «байт в
байт по каждому типу, где есть образец» сделано на ЖИВЫХ свопах с котировкой USDC
из его же сборов (data/c3_usdc_noga/obrazcy_usdc_noga.json) и из образцов пулов в
репозитории. Числа -- в docs/usdc_noga_kak_vstroit.md; типы БЕЗ живого USDC-образца
(AMM v4, DBC) как готовые НЕ СДАЮТСЯ.

ЧЕГО ЗДЕСЬ НЕТ. Ни одной отправки, ни одной симуляции, ни одной правки чужого
файла: точки врезки в bloom_own_send описаны страницей, ставит их Code-1.
"""
from __future__ import annotations

import os
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# ФЛАГ -- ASCII, ПО УМОЛЧАНИЮ ВЫКЛЮЧЕН, и у него ТРИ значения, а не два: между
# «выключено» и «покупаем» стоит тень. Правило групп -- как у типов пулов полосы
# (bloom_own_send._тип_по_флагу): список групп СИЛЬНЕЕ общего флага.
FLAG = "BLOOM_USDC_NOGA"
FLAG_GROUPS = "BLOOM_USDC_NOGA_GROUPS"
MODE_OFF = "vykl"
MODE_SHADOW = "ten"
MODE_LIVE = "boj"
MODES = (MODE_OFF, MODE_SHADOW, MODE_LIVE)

ROUTE = "usdc_noga"

PROG_CLMM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
PROG_WHIRLPOOL = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
PROG_AMMV4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
PROG_DBC = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
PROG_DAMM2 = "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"
PROG_DLMM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"

# СПОСОБ -- ПО ФАКТУ КОДА, А НЕ ПО ЖЕЛАНИЮ. "stroitel": у типа есть свой модуль с
# под_покупку() и инструкция_свопа() (он умеет переставить стороны, когда сделка
# источника была продажей). "swap_build": своего модуля с инструкцией нет, нога
# собирается кирпичами c2_swap_build по таблице DYN -- там переворота стороны нет
# вовсе, и сделка источника обязана идти USDC -> токен.
WAY_BUILDER = "stroitel"
WAY_BRICKS = "swap_build"
TYPES = {
    PROG_CLMM: {"label": "Raydium CLMM", "module": "c2_clmm_stroitel", "way": WAY_BUILDER},
    PROG_WHIRLPOOL: {"label": "Orca Whirlpool", "module": "c2_whirlpool_stroitel",
                     "way": WAY_BUILDER},
    PROG_AMMV4: {"label": "Raydium AMM v4", "module": "c2_ammv4_stroitel", "way": WAY_BUILDER},
    PROG_DBC: {"label": "Meteora DBC", "module": "c2_dbc_stroitel", "way": WAY_BUILDER},
    PROG_DAMM2: {"label": "Meteora DAMM v2", "module": None, "way": WAY_BRICKS},
    PROG_DLMM: {"label": "Meteora DLMM", "module": None, "way": WAY_BRICKS},
}

# Отказы -- ПО ИМЕНИ: полоса пишет их в выгрузку, и искать их подстрокой должно
# быть можно. Формулировки не меняются от места к месту, поэтому они здесь.
# БОЕВОЙ КОТИРОВЩИК ПО ТИПУ -- ЦЕНА С УЧЁТОМ НАШЕГО ОБЪЁМА, А НЕ СРЕДНЯЯ ЦЕНА
# СДЕЛКИ ИСТОЧНИКА. Решение владельца 01.10: в бою min_out второй ноги приходит от
# котировщика типа, потому что цена события -- средняя цена ЕГО сделки, а наша
# покупка идёт уже из состояния ПОСЛЕ неё, то есть по худшей цене. Значит цена
# события выход ЗАВЫШАЕТ, и min_out от неё -- это чаевые и приоритет за откат.
#   "stroitel"   -- подготовить(минт_котировки=USDC) модуля типа: живое состояние
#                   пула, и шаблон приходит С НЫНЕШНИМИ массивами (у CLMM и
#                   Whirlpool это обязательно -- массивы источника успели уехать).
#   "kirpichi"   -- c2_swap_build.min_out_from_reserves: у DAMM v2 это цена по
#                   событию свопа И резервам после сделки источника, нуль чтений.
#   "dlmm_bez_chteniy" -- c2_dlmm_bez_chteniy.котировка: нуль чтений, цена из
#                   события (слово владельца: «DLMM без чтений»). У неё выход
#                   считается средней ценой сделки источника, поэтому завышение
#                   остаётся -- его закрывает проскальзывание группы, и число
#                   завышения мерит deploy/checks/usdc_noga_min_out.py.
SPOSOB_STROITEL = "stroitel"
SPOSOB_KIRPICHI = "kirpichi"
SPOSOB_DLMM = "dlmm_bez_chteniy"
KOTIROVSHCHIKI = {
    PROG_CLMM: SPOSOB_STROITEL,
    PROG_WHIRLPOOL: SPOSOB_STROITEL,
    PROG_AMMV4: SPOSOB_STROITEL,
    PROG_DBC: SPOSOB_STROITEL,
    PROG_DAMM2: SPOSOB_KIRPICHI,
    PROG_DLMM: SPOSOB_DLMM,
}

WHY_OTHER_SIDE = ("своп источника шёл в другую сторону -- цену покупки "
                  "из обратного свопа брать нельзя")
WHY_NO_REVERSE = ("сделка источника -- продажа, а переставить стороны у этого типа "
                  "нечем (нога идёт кирпичами c2_swap_build)")
WHY_TYPE = "тип пула не в таблице USDC-ноги"
WHY_OFF = f"USDC-нога выключена флагом {FLAG}"
WHY_SHADOW = "режим тени: транзакция на отправку не собирается"
WHY_EXACT_OUT = "шаг 2 с инструкцией точного выхода -- этим путём не берём"
WHY_NET_KOTIROVSHCHIKA = ("боевого котировщика для этого типа нет -- по цене события "
                          "отправлять нельзя (выход завышен на наш объём)")
WHY_CENA_SOBYTIYA_V_BOJ = ("в бою min_out по цене события не берётся: она не учитывает "
                           "наш объём и завышает выход -- это чаевые и приоритет за откат")

# ПРЕДЕЛ CU НОГИ -- ОТДЕЛЬНЫМ ВХОДОМ (слово владельца 01.10). Замер Code-1 по
# simulateTransaction на живых образцах нашей таблицей адресов: 158 733...172 545 CU
# (deploy/checks/usdc_noga_razmer_cu.py, раздел замера в docs/usdc_noga_kak_vstroit.md).
# Умолчание 250 000 -- тот же потолок, что у двухшагового пути в таблице полосы, и
# это запас 1.45x к измеренному максимуму. Меняется переменной окружения, без
# правки кода и без деплоя.
IMYA_FLAGA_CU = "BLOOM_USDC_NOGA_CU"
CU_NOGI_PO_UMOLCHANIYU = 250_000
CU_ZAMER_CODE1 = (158_733, 172_545)


class OshibkaNogi(Exception):
    pass


# --------------------------------------------------------------------- флаг

def rezhim(gruppa: str | None = None) -> str:
    """Режим USDC-ноги: vykl (умолчание) / ten / boj.

    Список групп СИЛЬНЕЕ общего флага: группа не названа -- путь выключен.
    Молчаливое расширение на все группы дороже пропущенного сигнала.
    """
    spisok = [g.strip() for g in (os.environ.get(FLAG_GROUPS) or "")
              .replace(";", ",").split(",") if g.strip()]
    znach = (os.environ.get(FLAG) or "").strip().lower()
    if znach in ("1", "ten", "shadow", "тень"):
        znach = MODE_SHADOW
    elif znach in ("2", "boj", "live", "бой"):
        znach = MODE_LIVE
    else:
        znach = MODE_OFF
    if spisok:
        # Группа перечислена -- режим общего флага, но не ниже тени: строка
        # «включи для этой группы» без режима означает тень, а не покупку.
        if not gruppa or gruppa not in spisok:
            return MODE_OFF
        return znach if znach != MODE_OFF else MODE_SHADOW
    return znach


def vklyuchena(gruppa: str | None = None) -> bool:
    return rezhim(gruppa) != MODE_OFF


def boj(gruppa: str | None = None) -> bool:
    return rezhim(gruppa) == MODE_LIVE


# ------------------------------------------------------- модули и кирпичи

def _kirpichi():
    import c2_common as C  # noqa: PLC0415
    import c2_pool_programs as PP  # noqa: PLC0415
    import c2_shadow_build as SB  # noqa: PLC0415
    import c2_swap_build as B  # noqa: PLC0415
    return C, PP, SB, B


def _dvuhshagovyj():
    import bloom_lane_two_step as TS  # noqa: PLC0415
    return TS


def _sled(exc: BaseException) -> str:
    """Строка сбоя (repr и файл:строка) -- приёмом двухшагового пути, не своим.

    Обёртка нужна ровно затем, что зовут её ИЗ ОБРАБОТЧИКА: если и разбор упадёт,
    обработчик не имеет права бросить второе исключение -- тогда полоса потеряет
    причину целиком. Поэтому худший случай здесь -- имя класса, но СКАЗАННОЕ.
    """
    try:
        return _dvuhshagovyj().след_сбоя(exc)
    except Exception:  # noqa: BLE001
        return f"{type(exc).__name__} (место падения не разобрано)"


def modul(programma: str):
    """Модуль строителя типа пула или None, если нога идёт кирпичами."""
    t = TYPES.get(programma or "")
    if not t or not t.get("module"):
        return None
    import importlib  # noqa: PLC0415
    return importlib.import_module(t["module"])


def tip_znakom(programma: str) -> bool:
    return (programma or "") in TYPES


def programma_pula(tx: dict, hranilishche: str) -> str | None:
    """Программа пула по хранилищу -- тем же путём, что у двухшагового."""
    _C, PP, SB, _B = _kirpichi()
    return (PP.pool_program(tx, hranilishche, SB._labels()) or {}).get("pool_program")


# ------------------------------------------------- сторона USDC и шаблон ноги

def _storony_iz_pod_pokupku(p: dict) -> dict:
    """Минт и программы сторон из ответа под_покупку -- у всех строителей разный вид.

    CLMM отдаёт ключ «мх» (минт_входа/минт_выхода), Whirlpool, AMM v4 и DBC --
    «минт_базы»/«программа_базы»/«программа_котировки». Читается ФАКТ ответа: имя
    ключа по памяти тут однажды уже стоило измерения, где все пулы вышли
    «прочий/прочий».
    """
    if p.get("минт_базы"):
        return {"base_mint": p.get("минт_базы"),
                "base_program": p.get("программа_базы"),
                "quote_program": p.get("программа_котировки")}
    mh = p.get("мх") or {}
    return {"base_mint": mh.get("минт_выхода"),
            "base_program": mh.get("программа_выхода"),
            "quote_program": mh.get("программа_входа")}


def storona(tx_istochnika: dict, *, programma: str, pul: str | None = None,
            hranilishche: str | None = None, mint_kotirovki: str = USDC) -> dict:
    """Шаблон второй ноги, развёрнутый входом на USDC. Ни одного чтения сети.

    Возвращает {ok, why_not, way, label, tpl, base_mint, base_program,
    quote_program, razvernut, exact_out, src_tpl}. Отказ -- словами того
    строителя, который отказал: своей формулировки тут не заводится.
    """
    iz = {"ok": False, "why_not": None, "way": None, "label": None, "tpl": None,
          "base_mint": None, "base_program": None, "quote_program": None,
          "razvernut": None, "exact_out": None, "src_tpl": None}
    t = TYPES.get(programma or "")
    if not t:
        iz["why_not"] = f"{WHY_TYPE}: {str(programma)[:8]}"
        return iz
    iz["way"], iz["label"] = t["way"], t["label"]
    selektor = hranilishche or pul
    if t["way"] == WAY_BRICKS:
        _C, _PP, _SB, B = _kirpichi()
        if not selektor:
            iz["why_not"] = "ни хранилища, ни адреса пула -- инструкцию ноги не выбрать"
            return iz
        shab = B.extract_template(tx_istochnika, programma, selektor)
        if not shab.get("ok"):
            iz["why_not"] = f"шаблон ноги: {shab.get('why_not')}"
            return iz
        iz["src_tpl"] = shab
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
        if mv.get("quote_mint") != mint_kotirovki:
            iz["why_not"] = (WHY_NO_REVERSE if mv.get("base_mint") == mint_kotirovki
                             else (f"котировка пула не {mint_kotirovki[:8]}: стороны пула "
                                   f"{str(mv.get('quote_mint'))[:8]}/"
                                   f"{str(mv.get('base_mint'))[:8]}"))
            return iz
        iz.update(ok=True, tpl=shab, base_mint=mv.get("base_mint"),
                  base_program=mv.get("base_program"),
                  quote_program=mv.get("quote_program"), razvernut=False)
        return iz

    M = modul(programma)
    if M is None:
        iz["why_not"] = f"модуль строителя {t.get('module')} не загрузился"
        return iz
    shab = M.шаблон(tx_istochnika, pul, хранилище=hranilishche)
    if not shab.get("ok"):
        iz["why_not"] = f"шаблон ноги: {shab.get('why_not')}"
        return iz
    iz["src_tpl"] = shab
    p = M.под_покупку(shab, tx_istochnika, минт_котировки=mint_kotirovki)
    if not p.get("ok"):
        iz["why_not"] = p.get("why_not")
        return iz
    st = _storony_iz_pod_pokupku(p)
    # РАЗВОРОТ СЧИТАЕТСЯ ПО ФАКТУ, А НЕ ПО ОДНОМУ КЛЮЧУ. У CLMM, AMM v4 и DBC он
    # в ответе «развёрнут»; у Whirlpool счета не переставляются вовсе (направление
    # живёт в данных), и развёрнутость -- это a_to_b, не совпавший с источником.
    razvernut = bool(p.get("развёрнут"))
    if p.get("a_to_b") is not None and shab.get("a_to_b_источника") is not None:
        razvernut = bool(p["a_to_b"]) != bool(shab["a_to_b_источника"])
    iz.update(ok=True, tpl=p["шаблон"], razvernut=razvernut,
              exact_out=False, **st)
    return iz


# ------------------------------------------------ цена события и минимум выхода

def cena_sobytiya(tx_istochnika: dict, tpl: dict, *, base_mint: str,
                  quote_mint: str = USDC) -> dict:
    """Цена сделки источника по ЕГО ЖЕ балансам: USDC внутрь, токен наружу.

    «USDC внутрь» -- сумма ПРИБАВОК на счетах USDC, которые стоят в инструкции
    пула: это хранилище плюс счета комиссий, то есть всё, что источник отдал.
    Определение то же, что у spent в c2_swap_build.min_out_from_reserves, чтобы
    два пути не считали цену по-разному. «Токен наружу» -- сумма УБЫЛИ на счетах
    базы этой же инструкции, то есть сколько пул выдал.

    Знаки минтов не нужны: обе суммы сырые, и цена -- их отношение.
    """
    iz = {"ok": False, "why_not": None, "usdc_v_pul": None, "token_iz_pula": None}
    C, _PP, _SB, _B = _kirpichi()
    if not base_mint:
        iz["why_not"] = "минт базы не известен -- цену события не посчитать"
        return iz
    try:
        stroki = {r["account"]: r for r in C.token_rows(tx_istochnika).values()}
    except Exception as exc:  # noqa: BLE001
        iz["why_not"] = f"балансы сделки не разобрались: {type(exc).__name__}"
        return iz
    scheta = set(tpl.get("accounts") or [])
    if not scheta:
        iz["why_not"] = "у шаблона ноги нет счетов"
        return iz
    v_pul = sum(r["post"] - r["pre"] for a, r in stroki.items()
                if a in scheta and r.get("mint") == quote_mint and r["post"] > r["pre"])
    iz_pula = sum(r["pre"] - r["post"] for a, r in stroki.items()
                  if a in scheta and r.get("mint") == base_mint and r["pre"] > r["post"])
    if v_pul <= 0 or iz_pula <= 0:
        iz["why_not"] = WHY_OTHER_SIDE
        return iz
    iz.update(ok=True, usdc_v_pul=int(v_pul), token_iz_pula=int(iz_pula))
    return iz


def min_out_po_sobytiyu(cena: dict, summa_usdc: int, proskalzyvanie: float) -> dict:
    """Минимум выхода в токене по цене события и проскальзыванию группы."""
    iz = {"ok": False, "why_not": None, "expected_out": None, "min_out": None,
          "put": "цена события источника (нуль чтений)"}
    if not (cena or {}).get("ok"):
        iz["why_not"] = (cena or {}).get("why_not") or "цены события нет"
        return iz
    if not isinstance(summa_usdc, int) or summa_usdc <= 0:
        iz["why_not"] = f"сумма USDC не положительное целое: {summa_usdc!r}"
        return iz
    if not 0 <= float(proskalzyvanie) < 1:
        iz["why_not"] = f"проскальзывание вне (0, 1): {proskalzyvanie!r}"
        return iz
    ozhid = (D(summa_usdc) * D(cena["token_iz_pula"]) / D(cena["usdc_v_pul"]))
    mo = int(ozhid * D(1 - float(proskalzyvanie)))
    if mo <= 0:
        iz["why_not"] = "минимум выхода вышел нулевым -- сумма ноги слишком мала"
        return iz
    iz.update(ok=True, expected_out=int(ozhid), min_out=mo)
    return iz


# ------------------------------------------------------------ суммы двух ног

def summy_nog(*, lamporty: int, price_sol, q_dec: int, quote_program: str | None = None,
              nalog_bps=None) -> dict:
    """Билет SOL -> USDC и связка сумм двух ног -- числами двухшагового модуля.

    Ни одна из четырёх величин здесь не заведена заново: запас первого шага и
    вычет налога берутся из bloom_lane_two_step, чтобы два пути не разошлись в
    оценке первой ноги.
    """
    iz = {"ok": False, "why_not": None, "leg1_min_out": None, "leg2_amount_in": None,
          "leg2_to_pool": None, "quote_fee_bps": None, "zapas_shaga_1": None}
    TS = _dvuhshagovyj()
    iz["zapas_shaga_1"] = float(TS.ЗАПАС_ШАГА_1)
    if not isinstance(lamporty, int) or lamporty <= 0:
        iz["why_not"] = f"размер не положительное целое: {lamporty!r}"
        return iz
    if not price_sol or not q_dec:
        iz["why_not"] = "у шаблона первой ноги нет цены котировочного токена"
        return iz
    nal = TS.налог_известен(quote_program, nalog_bps)
    iz["quote_fee_bps"] = nal.get("bps")
    if not nal["ok"]:
        iz["why_not"] = nal["почему"]
        return iz
    bps = int(nal["bps"])
    ozhid_q = D(lamporty) / D(10) ** 9 / D(str(price_sol)) * D(10) ** int(q_dec)
    shag2_vhod = int(ozhid_q * D(1 - TS.ЗАПАС_ШАГА_1))
    if shag2_vhod <= 0:
        iz["why_not"] = "ожидаемый выход первой ноги нулевой"
        return iz
    u_nas_budet = TS.после_налога(shag2_vhod, bps)
    dojdet = TS.после_налога(u_nas_budet, bps)
    if u_nas_budet <= 0 or dojdet <= 0:
        iz["why_not"] = "после налога переводов от первой ноги не остаётся ничего"
        return iz
    iz.update(ok=True, leg1_min_out=shag2_vhod, leg2_amount_in=u_nas_budet,
              leg2_to_pool=dojdet)
    return iz


# ------------------------------------------------------- инструкция второй ноги

def instrukciya_nogi(storona_nogi: dict, *, user: str, amount_in: int, min_out: int,
                     tx_istochnika: dict | None = None, kak_u_istochnika: bool = False):
    """Инструкция свопа второй ноги: у строителя -- его инструкция_свопа, у
    остальных -- кирпич c2_swap_build.swap_instruction. Вход -- USDC.

    kak_u_istochnika=True -- ПЕРЕСБОРКА СДЕЛКИ ИСТОЧНИКА для самопроверки байт в
    байт: его аргументы, его хвост, его пользователь.
    """
    if not (storona_nogi or {}).get("ok"):
        raise OshibkaNogi((storona_nogi or {}).get("why_not") or "стороны ноги нет")
    tpl = storona_nogi["tpl"]
    if storona_nogi["way"] == WAY_BRICKS:
        _C, _PP, _SB, B = _kirpichi()
        return B.swap_instruction(tpl, tx_istochnika or {}, user, int(amount_in),
                                  int(min_out), keep_source_ix=bool(kak_u_istochnika))
    M = modul(tpl.get("program") or "")
    if M is None:
        # Программа шаблона у строителей в ключе не лежит -- берём её по метке типа.
        M = next((modul(p) for p, t in TYPES.items()
                  if t["label"] == storona_nogi.get("label") and t.get("module")), None)
    if M is None:
        raise OshibkaNogi("модуль строителя для инструкции ноги не найден")
    return M.инструкция_свопа(tpl, user=user, amount_in=int(amount_in),
                              min_out=int(min_out),
                              как_у_источника=bool(kak_u_istochnika))


def tx_s_adresami(tx: dict) -> dict:
    """Транзакция из getTransaction(encoding="json") в вид jsonParsed: вместо
    ИНДЕКСОВ -- адреса. Ничего не придумывается.

    ЗАЧЕМ. Образцы Code-2 (data/podbivka/usdc_noga_dlya_code3.json) сняты с
    `encoding: "json"`: в инструкциях там `programIdIndex` и номера счетов, а не
    адреса. Строители и кирпичи ждут адреса (`programId`, `accounts` строками) --
    на индексах они честно отказывают «инструкции пула в транзакции нет», и 22
    образца из 22 выглядели бы как отказ пути. Та же беда 28.09 стоила полосе
    ненайденной СВОЕЙ покупки на продаже (см. bloom_lane_sell, programIdIndex).

    ОТКУДА БЕРУТСЯ АДРЕСА И ПРАВА. Адреса -- ключи самой транзакции: статические
    (`message.accountKeys`) плюс адреса из таблиц (`meta.loadedAddresses`), в
    каноническом порядке «статические, затем записываемые из таблиц, затем
    читаемые» -- том же, по которому индексируются балансы (c2_common.account_keys).
    Подписи и права -- из `message.header` по раскладке сообщения Solana:
    первые numRequiredSignatures -- подписанты (последние numReadonlySignedAccounts
    из них только для чтения), у остальных статических записываемы все, кроме
    последних numReadonlyUnsignedAccounts. Адреса из таблиц подписантами не бывают.

    Права здесь -- не мелочь: `writable_map` кормит метки счетов нашей
    инструкции, и перепутанное право записи -- это отказ программы на живых
    деньгах. Поэтому у перевода есть обратная сверка (см. self_test): настоящая
    jsonParsed-сделка опускается в индексы и поднимается назад, и флаги с
    адресами обязаны совпасть у всех образцов.

    Уже разобранную сделку (есть programId) возвращает как есть.
    """
    t = (tx or {}).get("transaction") or {}
    m = t.get("message") or {}
    ixs = m.get("instructions") or []
    if not ixs or "programId" in (ixs[0] or {}):
        return tx
    syrye = list(m.get("accountKeys") or [])
    if syrye and isinstance(syrye[0], dict):
        return tx
    meta = dict((tx or {}).get("meta") or {})
    zagr = meta.get("loadedAddresses") or {}
    zapis = list(zagr.get("writable") or [])
    chtenie = list(zagr.get("readonly") or [])
    klyuchi = list(syrye) + zapis + chtenie
    h = m.get("header") or {}
    ns = int(h.get("numRequiredSignatures") or 0)
    nrs = int(h.get("numReadonlySignedAccounts") or 0)
    nru = int(h.get("numReadonlyUnsignedAccounts") or 0)
    if not ns or ns > len(syrye):
        raise OshibkaNogi("в сделке нет message.header -- права счетов не вывести")
    novye = []
    for i, a in enumerate(syrye):
        podpisant = i < ns
        if podpisant:
            mozhno = i < ns - nrs
        else:
            mozhno = i < len(syrye) - nru
        novye.append({"pubkey": a, "writable": bool(mozhno), "signer": podpisant,
                      "source": "transaction"})
    for a in zapis:
        novye.append({"pubkey": a, "writable": True, "signer": False,
                      "source": "lookupTable"})
    for a in chtenie:
        novye.append({"pubkey": a, "writable": False, "signer": False,
                      "source": "lookupTable"})

    def _ix(ix: dict) -> dict:
        i = ix.get("programIdIndex")
        nomera = ix.get("accounts") or []
        if not isinstance(i, int) or i >= len(klyuchi)                 or any((not isinstance(j, int)) or j >= len(klyuchi) for j in nomera):
            raise OshibkaNogi("номер счёта в инструкции вне списка ключей сделки")
        novyj = {k: v for k, v in ix.items() if k not in ("programIdIndex", "accounts")}
        novyj["programId"] = klyuchi[i]
        novyj["accounts"] = [klyuchi[j] for j in nomera]
        return novyj

    vnutri = []
    for g in meta.get("innerInstructions") or []:
        vnutri.append(dict(g, instructions=[_ix(x) for x in (g.get("instructions") or [])]))
    meta["innerInstructions"] = vnutri
    return dict(tx, transaction=dict(t, message=dict(m, accountKeys=novye,
                                                     instructions=[_ix(x) for x in ixs])),
                meta=meta)


# Теговые номера инструкций токеновых программ, которыми СОЗДАЮТ счёт: у всех трёх
# минт стоит вторым счётом (SPL Token и Token-2022 одинаково).
TEGI_SOZDANIYA_SCHETA = (1, 16, 18)


def mint_scheta(tx: dict, adres: str) -> str | None:
    """Минт токенового счёта ПО САМОЙ СДЕЛКЕ: сначала балансы, потом создание.

    ЗАЧЕМ ВТОРОЙ ПУТЬ. Часть источников торгует через ВРЕМЕННЫЕ счета, созданные и
    закрытые в этой же транзакции: в pre/postTokenBalances их нет вовсе, и по
    балансам минт не узнать. Живой след -- сделка 5pPyhfPdictT4BvVg4Xgr... (бот
    term9YPb9...): счета мест 3 и 4 создаются (System CreateAccount, затем
    InitializeAccount2) и закрываются (CloseAccount) внутри сделки, а минты у них
    ровно наши -- USDC и база. Без этого пути такая сделка выглядела бы
    расхождением «вне наших мест», то есть ложным провалом типа.
    """
    _C, _PP, _SB, B = _kirpichi()
    C, *_ = _kirpichi()
    for r in C.token_rows(tx).values():
        if r.get("account") == adres and r.get("mint"):
            return r["mint"]
    for ix in B.all_instructions(tx):
        if ix.get("programId") not in (B.TOKEN_PROGRAM, TOKEN_2022):
            continue
        scheta = ix.get("accounts") or []
        if len(scheta) < 2 or scheta[0] != adres:
            continue
        try:
            dannye = B.b58decode(ix.get("data") or "")
        except (ValueError, IndexError):
            continue
        if dannye and dannye[0] in TEGI_SOZDANIYA_SCHETA:
            return scheta[1]
    return None


def polzovatel_istochnika(storona_nogi: dict, tx_istochnika: dict) -> str | None:
    """Кто подписал сделку ИСТОЧНИКА на его месте в этой инструкции.

    Нужен пересборке байт в байт: подставив СВОЙ кошелёк, сравнивать с его
    инструкцией нечего -- разойдутся ровно наши места. У строителей это
    пользователь_шаблона(); у кирпичей место подписанта не объявлено, и оно
    находится ПОДСТАВНЫМ адресом: тот индекс, куда c2_swap_build положил его.
    """
    if not (storona_nogi or {}).get("ok"):
        return None
    tpl = storona_nogi["tpl"]
    if storona_nogi["way"] == WAY_BUILDER:
        M = modul(tpl.get("program") or "")
        return M.пользователь_шаблона(tpl) if M is not None else None
    _C, _PP, _SB, B = _kirpichi()
    metka = "SysvarC1ock11111111111111111111111111111111"
    subs = B.user_accounts(tpl, tx_istochnika, metka) or {}
    mesta = [i for i, a in subs.items() if a == metka]
    return tpl["accounts"][mesta[0]] if len(mesta) == 1 else None


def cu_nogi() -> int:
    """Предел CU второй ноги -- отдельным входом, число из окружения.

    Замер Code-1 по simulateTransaction нашей таблицей адресов: 158 733...172 545 CU
    на живых образцах. Умолчание 250 000 -- запас 1.45x к максимуму и то же число,
    что у двухшагового пути в таблице полосы. Непонятное значение переменной -- это
    умолчание, а не ноль: ноль CU означал бы отказ сети на каждой покупке.
    """
    syroe = (os.environ.get(IMYA_FLAGA_CU) or "").strip()
    try:
        chislo = int(syroe)
    except ValueError:
        return CU_NOGI_PO_UMOLCHANIYU
    return chislo if 0 < chislo <= 1_400_000 else CU_NOGI_PO_UMOLCHANIYU


def min_out_boj(storona_nogi: dict, tx_istochnika: dict, *, amount_in: int,
                proskalzyvanie: float, rpc_call=None, pul: str | None = None,
                hranilishche: str | None = None, mint_bazy: str | None = None,
                gruppa: str | None = None, nalog_vyhoda=None,
                seychas: float | None = None) -> dict:
    """MIN_OUT ВТОРОЙ НОГИ ДЛЯ БОЯ: у котировщика типа, с учётом нашего объёма.

    Возвращает {ok, why_not, min_out, expected_out, put, chtenij, tpl}. `tpl` --
    ЗАМЕНА шаблона ноги, если котировщик её дал (у CLMM и Whirlpool подготовить()
    отдаёт шаблон с НЫНЕШНИМИ массивами тиков: массивы сделки источника к нашей
    покупке уже уехали, и собрать по ним значило бы заплатить за промах).

    Цены события здесь нет вовсе: её место -- тень.
    """
    iz = {"ok": False, "why_not": None, "min_out": None, "expected_out": None,
          "put": None, "chtenij": None, "tpl": None, "fee_share": None}
    if not (storona_nogi or {}).get("ok"):
        iz["why_not"] = (storona_nogi or {}).get("why_not") or "стороны ноги нет"
        return iz
    tpl = storona_nogi["tpl"]
    prog = tpl.get("program") or storona_nogi.get("program")
    if not prog:
        prog = next((p for p, t in TYPES.items()
                     if t["label"] == storona_nogi.get("label")), None)
    sposob = KOTIROVSHCHIKI.get(prog or "")
    iz["put"] = sposob
    if sposob is None:
        iz["why_not"] = WHY_NET_KOTIROVSHCHIKA
        return iz
    if not isinstance(amount_in, int) or amount_in <= 0:
        iz["why_not"] = f"сумма второй ноги не положительное целое: {amount_in!r}"
        return iz
    baza = mint_bazy or storona_nogi.get("base_mint")
    try:
        if sposob == SPOSOB_STROITEL:
            M = modul(prog)
            if M is None:
                iz["why_not"] = WHY_NET_KOTIROVSHCHIKA
                return iz
            d = M.подготовить(tx_istochnika, пул=pul, хранилище_пула=hranilishche,
                              лампорты=int(amount_in),
                              проскальзывание=float(proskalzyvanie),
                              rpc_call=rpc_call, минт_котировки=USDC, минт_базы=baza,
                              группа=gruppa, сейчас=seychas, налог_выхода=nalog_vyhoda)
            d = d if isinstance(d, dict) else {}
            iz.update(chtenij=d.get("чтений"), fee_share=d.get("fee_share"),
                      min_out=d.get("min_out"), expected_out=d.get("expected_out"),
                      put=f"подготовить {TYPES[prog]['module']}")
            if not d.get("ok"):
                iz["why_not"] = f"котировщик типа отказал: {d.get('why_not')}"
                return iz
            iz["tpl"] = d.get("шаблон")
        elif sposob == SPOSOB_KIRPICHI:
            _C, _PP, _SB, B = _kirpichi()
            mo = B.min_out_from_reserves(tpl, tx_istochnika, int(amount_in),
                                         float(proskalzyvanie))
            mo = mo if isinstance(mo, dict) else {}
            iz.update(chtenij=0, min_out=mo.get("min_out"),
                      expected_out=mo.get("expected_out"),
                      put="c2_swap_build.min_out_from_reserves")
            if not mo.get("ok"):
                iz["why_not"] = f"котировщик типа отказал: {mo.get('why_not')}"
                return iz
        else:
            import c2_dlmm_bez_chteniy as DL  # noqa: PLC0415

            # Пул DLMM -- счёт 0 его инструкции; адрес пула полоса знает не всегда,
            # а шаблон ноги знает всегда.
            pul_dlmm = pul or (tpl.get("accounts") or [None])[0]
            k = DL.котировка(пул=pul_dlmm, минт_базы=baza, минт_котировки=USDC,
                             лампорты=int(amount_in),
                             проскальзывание=float(proskalzyvanie),
                             tx_источника=tx_istochnika,
                             программа_выхода=storona_nogi.get("base_program"),
                             налог_выхода=nalog_vyhoda)
            k = k if isinstance(k, dict) else {}
            iz.update(chtenij=k.get("чтений"), min_out=k.get("min_out"),
                      expected_out=k.get("expected_out"),
                      put="c2_dlmm_bez_chteniy.котировка",
                      korzin_dokazano=k.get("корзин_доказано"),
                      korzin_nashego_razmera=k.get("корзин_нашего_размера"))
            if not k.get("ok"):
                iz["why_not"] = f"котировщик типа отказал: {k.get('why_not')}"
                return iz
    except Exception as exc:  # noqa: BLE001
        iz["why_not"] = f"котировщик типа упал: {type(exc).__name__}: {str(exc)[:160]}"
        return iz
    if not isinstance(iz.get("min_out"), int) or iz["min_out"] <= 0:
        iz["why_not"] = f"минимум выхода не положителен: {iz.get('min_out')!r}"
        return iz
    iz["ok"] = True
    return iz


# --------------------------------------------------------------------- тень

def ten(*, tx_istochnika: dict, istochnik: str, mint: str, lamporty: int,
        kesh_nog, proskalzyvanie: float = 0.35, gruppa: str | None = None,
        nalog_kotirovki_bps=None, seychas: float | None = None) -> dict:
    """ЧИСЛА USDC-ноги без транзакции и без сети: годилось бы или нет, и почему.

    Решений не принимает и min_out на отправку не отдаёт -- ровно как тень DLMM
    без чтений: завышение цены события закрывается запасом, а запас -- решение
    штаба, не строителя.
    """
    iz = {"ok": False, "why_not": None, "route": ROUTE, "rezhim": rezhim(gruppa),
          "pool_program": None, "label": None, "way": None, "quote_mint": USDC,
          "leg1_pool_program": None, "leg1_template_age_s": None,
          "leg1_min_out": None, "leg2_amount_in": None, "leg2_to_pool": None,
          "quote_fee_bps": None, "min_out": None, "expected_out": None,
          "usdc_v_pul_istochnika": None, "token_iz_pula_istochnika": None,
          "razvernut": None, "chtenij": 0}
    if not vklyuchena(gruppa):
        iz["why_not"] = WHY_OFF
        return iz
    try:
        C, _PP, _SB, _B = _kirpichi()
        TS = _dvuhshagovyj()
    except Exception as exc:  # noqa: BLE001
        iz["why_not"] = f"модули сборки не загружены: {type(exc).__name__}"
        return iz
    try:
        pul = C.identify_pool(tx_istochnika, istochnik, mint)
        if not pul.get("ok"):
            iz["why_not"] = f"пул источника: {pul.get('why_not')}"
            return iz
        prog = programma_pula(tx_istochnika, pul["pool_vault"])
        iz["pool_program"] = prog
        st = storona(tx_istochnika, programma=prog, hranilishche=pul["pool_vault"])
        iz.update(label=st.get("label"), way=st.get("way"),
                  razvernut=st.get("razvernut"))
        if not st.get("ok"):
            iz["why_not"] = st.get("why_not")
            return iz
        if kesh_nog is None:
            iz["why_not"] = "кэша шаблонов первой ноги нет -- билет в USDC не посчитать"
            return iz
        e, vozrast = kesh_nog.get(USDC)
        if e is None:
            iz["why_not"] = f"шаблона SOL -> {USDC[:8]} в кэше нет"
            return iz
        iz["leg1_pool_program"] = e.get("program")
        iz["leg1_template_age_s"] = round(vozrast, 1) if vozrast is not None else None
        s = summy_nog(lamporty=lamporty, price_sol=e.get("price_sol"),
                      q_dec=e.get("q_dec"),
                      quote_program=(e.get("mv") or {}).get("base_program"),
                      nalog_bps=nalog_kotirovki_bps)
        iz.update(quote_fee_bps=s.get("quote_fee_bps"), leg1_min_out=s.get("leg1_min_out"),
                  leg2_amount_in=s.get("leg2_amount_in"), leg2_to_pool=s.get("leg2_to_pool"))
        if not s.get("ok"):
            iz["why_not"] = s.get("why_not")
            return iz
        c = cena_sobytiya(tx_istochnika, st["tpl"], base_mint=st["base_mint"])
        iz.update(usdc_v_pul_istochnika=c.get("usdc_v_pul"),
                  token_iz_pula_istochnika=c.get("token_iz_pula"))
        mo = min_out_po_sobytiyu(c, int(s["leg2_to_pool"]), proskalzyvanie)
        iz.update(min_out=mo.get("min_out"), expected_out=mo.get("expected_out"),
                  put=mo.get("put"))
        if not mo.get("ok"):
            iz["why_not"] = mo.get("why_not")
            return iz
        if vozrast is not None and vozrast > _SB_vozrast(TS):
            iz["why_not"] = (f"шаблон первой ноги старше {_SB_vozrast(TS)} с "
                             f"({vozrast:.0f} с)")
            return iz
    except Exception as exc:  # noqa: BLE001
        iz["why_not"] = f"тень USDC-ноги: {type(exc).__name__}: {str(exc)[:160]}"
        return iz
    iz["ok"] = True
    return iz


def _SB_vozrast(TS) -> float:
    """Предел свежести шаблона первой ноги -- число двухшагового пути, не своё."""
    _C, _PP, SB, _B = _kirpichi()
    return float(SB.LEG_MAX_AGE_S)


# ------------------------------------------------------------------- сборка

def instrukcii(*, tx_istochnika: dict, istochnik: str, mint: str, nash_koshelek: str,
               lamporty: int, kesh_nog, proskalzyvanie: float = 0.35,
               cu_units: int | None = None, prioritet_lamporty: int = 1_000_000,
               chaevye_lamporty: int = 1_000_000, chaevye_spiskom: list | None = None,
               chaevye_adres: str | None = None, nons: tuple | None = None,
               nalog_kotirovki_bps=None, min_out_vneshnij: int | None = None,
               gruppa: str | None = None, kotirovka: str = "sobytie",
               rpc_call=None, nalog_vyhoda=None, seychas: float | None = None) -> dict:
    """СПИСОК ИНСТРУКЦИЙ двух ног в порядке двухшагового пути -- без компиляции.

    Отдельно от sobrat() по одной причине: порядок инструкций -- это и есть
    механизм, и проверять его надо списком, а не по собранной транзакции (её
    может не быть вовсе: без таблиц адресов две ноги в 1232 байта не влезают).

    kotirovka="boj" -- min_out у котировщика типа (min_out_boj), и шаблон ноги
    берётся ЕГО, если он его дал. kotirovka="sobytie" -- цена события источника:
    это путь ТЕНИ, и на отправку такой min_out не годится.
    cu_units=None -- предел CU из окружения (cu_nogi(), умолчание 250 000).
    """
    if cu_units is None:
        cu_units = cu_nogi()
    iz = {"ok": False, "why_not": None, "route": ROUTE, "steps": 2,
          "rezhim": rezhim(gruppa), "pool_program": None, "label": None, "way": None,
          "quote_mint": USDC, "leg1_pool_program": None, "leg1_template_age_s": None,
          "leg1_min_out": None, "leg2_amount_in": None, "leg2_to_pool": None,
          "quote_fee_bps": None, "min_out": None, "expected_out": None,
          "min_out_from": None, "razvernut": None, "slippage_used": proskalzyvanie,
          "ixs": None, "leg1_entry": None, "nonce_para": None, "tip_account": None,
          "tips": None, "tips_total_lamports": None}
    try:
        C, _PP, SB, B = _kirpichi()
        TS = _dvuhshagovyj()
    except Exception as exc:  # noqa: BLE001
        iz["why_not"] = f"модули сборки не загружены: {type(exc).__name__}"
        return iz
    if kesh_nog is None:
        iz["why_not"] = "кэша шаблонов первой ноги нет -- собирать не из чего"
        return iz
    # ВИД NONCE ПРИВОДИТСЯ ЗДЕСЬ И ОДИН РАЗ -- тем же приёмом двухшагового пути
    # (TS.пара_нонса), а не своим: два приёма однажды разойдутся, и разойдутся на
    # деньгах. Полоса передаёт nonce СЛОВАРЁМ, кирпич advance_nonce ждёт пару, и
    # 02.10 nons[0] по словарю дал KeyError: 0 -- годный сигнал потерян на этом.
    # Приведённая пара уходит и в ответ (nonce_para): sobrat() берёт её ОТТУДА, а
    # не трогает сырой вход второй раз.
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
        prog = programma_pula(tx_istochnika, pul["pool_vault"])
        iz["pool_program"] = prog
        st = storona(tx_istochnika, programma=prog, hranilishche=pul["pool_vault"])
        iz.update(label=st.get("label"), way=st.get("way"), razvernut=st.get("razvernut"))
        if not st.get("ok"):
            iz["why_not"] = st.get("why_not")
            return iz
        e, vozrast = kesh_nog.get(USDC)
        if e is None:
            iz["why_not"] = f"шаблона SOL -> {USDC[:8]} в кэше нет"
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
        iz.update(quote_fee_bps=s.get("quote_fee_bps"), leg1_min_out=s.get("leg1_min_out"),
                  leg2_amount_in=s.get("leg2_amount_in"), leg2_to_pool=s.get("leg2_to_pool"))
        if not s.get("ok"):
            iz["why_not"] = s.get("why_not")
            return iz
        if isinstance(min_out_vneshnij, int) and min_out_vneshnij > 0:
            min_out = int(min_out_vneshnij)
            iz.update(min_out=min_out, min_out_from="передан полосой")
        elif kotirovka == "boj":
            # БОЕВОЙ MIN_OUT -- У КОТИРОВЩИКА ТИПА, и шаблон ноги тоже его: у CLMM
            # и Whirlpool подготовить() отдаёт массивы тиков НЫНЕШНИЕ, а не те, что
            # были у источника.
            kb = min_out_boj(st, tx_istochnika, amount_in=int(s["leg2_to_pool"]),
                             proskalzyvanie=proskalzyvanie, rpc_call=rpc_call,
                             # АДРЕС ПУЛА ПОЛОСА НЕ ЗНАЕТ -- у неё хранилище
                             # (C.identify_pool), и строители ищут пул по нему
                             # сами; так же зовёт их и одношаговая сборка.
                             pul=None, hranilishche=pul["pool_vault"],
                             mint_bazy=st["base_mint"], gruppa=gruppa,
                             nalog_vyhoda=nalog_vyhoda, seychas=seychas)
            iz.update(min_out=kb.get("min_out"), expected_out=kb.get("expected_out"),
                      min_out_from=kb.get("put"), chtenij=kb.get("chtenij"),
                      fee_share=kb.get("fee_share"))
            if not kb.get("ok"):
                iz["why_not"] = kb.get("why_not")
                return iz
            if kb.get("tpl"):
                st = dict(st, tpl=kb["tpl"])
                iz["tpl_ot_kotirovshchika"] = True
            min_out = int(kb["min_out"])
        else:
            c = cena_sobytiya(tx_istochnika, st["tpl"], base_mint=st["base_mint"])
            mo = min_out_po_sobytiyu(c, int(s["leg2_to_pool"]), proskalzyvanie)
            iz.update(min_out=mo.get("min_out"), expected_out=mo.get("expected_out"),
                      min_out_from=mo.get("put"),
                      usdc_v_pul_istochnika=c.get("usdc_v_pul"),
                      token_iz_pula_istochnika=c.get("token_iz_pula"))
            if not mo.get("ok"):
                iz["why_not"] = mo.get("why_not")
                return iz
            min_out = int(mo["min_out"])

        wsol_schet = B.ata(nash_koshelek, C.WSOL, mv1.get("quote_program"))
        ixs = []
        if nons:
            ixs.append(B.advance_nonce(str(nons[0]), str(nons[1])))
        ixs += [B.cu_limit(cu_units),
                B.cu_price(TS._цена_cu(prioritet_lamporty, cu_units)),
                B.ata_idempotent(nash_koshelek, nash_koshelek, C.WSOL,
                                 mv1.get("quote_program")),
                B.ata_idempotent(nash_koshelek, nash_koshelek, USDC,
                                 mv1.get("base_program")),
                B.ata_idempotent(nash_koshelek, nash_koshelek, st["base_mint"],
                                 st["base_program"]),
                B.sol_transfer(nash_koshelek, wsol_schet, lamporty),
                B.sync_native(wsol_schet),
                B.swap_instruction(e["tpl"], e["tx"], nash_koshelek, lamporty,
                                   int(s["leg1_min_out"])),
                instrukciya_nogi(st, user=nash_koshelek,
                                 amount_in=int(s["leg2_amount_in"]), min_out=min_out,
                                 tx_istochnika=tx_istochnika)]
        pary = TS._пары_чаевых(chaevye_spiskom, chaevye_adres, chaevye_lamporty)
        for adres, lamp in pary:
            ixs.append(B.sol_transfer(nash_koshelek, adres, int(lamp)))
        if pary:
            iz["tip_account"] = pary[0][0]
            iz["tips"] = list(pary)
            iz["tips_total_lamports"] = sum(int(lamp_) for _, lamp_ in pary)
    except Exception as exc:  # noqa: BLE001
        # repr И место падения: по одному имени класса причину не назвать (02.10).
        iz["why_not"] = f"сборка USDC-ноги: {_sled(exc)}"
        return iz
    iz.update(ok=True, ixs=ixs, leg1_entry=e)
    return iz


def sobrat(*, tx_istochnika: dict, istochnik: str, mint: str, nash_koshelek: str,
           lamporty: int, kesh_nog, proskalzyvanie: float = 0.35,
           cu_units: int | None = None, prioritet_lamporty: int = 1_000_000,
           chaevye_lamporty: int = 1_000_000, chaevye_spiskom: list | None = None,
           chaevye_adres: str | None = None, nons: tuple | None = None,
           rpc_call=None, gruppa: str | None = None, nalog_kotirovki_bps=None,
           min_out_vneshnij: int | None = None,
           nashi_tablicy: list | None = None, nalog_vyhoda=None,
           seychas: float | None = None) -> dict:
    """ОДНА транзакция: SOL -> USDC -> токен на строителе типа. Без подписи и отправки.

    Порядок инструкций, пределы и связка сумм -- двухшагового пути; вторая нога --
    строителя типа. min_out второй ноги: min_out_vneshnij, если полоса передала
    его от подготовить(минт_котировки=USDC) этого строителя (боевой путь, цена из
    живого состояния), иначе цена события источника.
    """
    t0 = time.perf_counter()
    if rezhim(gruppa) != MODE_LIVE:
        return {"ok": False, "route": ROUTE, "rezhim": rezhim(gruppa),
                "why_not": (WHY_OFF if rezhim(gruppa) == MODE_OFF else WHY_SHADOW),
                "tx_base64": None, "size": None}
    iz = instrukcii(
        tx_istochnika=tx_istochnika, istochnik=istochnik, mint=mint,
        nash_koshelek=nash_koshelek, lamporty=lamporty, kesh_nog=kesh_nog,
        proskalzyvanie=proskalzyvanie, cu_units=cu_units,
        prioritet_lamporty=prioritet_lamporty, chaevye_lamporty=chaevye_lamporty,
        chaevye_spiskom=chaevye_spiskom, chaevye_adres=chaevye_adres, nons=nons,
        nalog_kotirovki_bps=nalog_kotirovki_bps, min_out_vneshnij=min_out_vneshnij,
        gruppa=gruppa,
        # В БОЮ MIN_OUT ТОЛЬКО ОТ КОТИРОВЩИКА ТИПА. Цена события остаётся тени:
        # она не учитывает наш объём и завышает выход (решение владельца 01.10).
        kotirovka="boj", rpc_call=rpc_call, nalog_vyhoda=nalog_vyhoda,
        seychas=seychas)
    ixs = iz.pop("ixs", None)
    e = iz.pop("leg1_entry", None)
    # NONCE В ОТВЕТЕ -- ПРИВЕДЁННАЯ ПАРА ИЗ instrukcii(), А НЕ СЫРОЙ ВХОД. Здесь
    # стояло str(nons[0]) ВНЕ всякого try, и словарь полосы выпускал KeyError: 0
    # наружу из sobrat(): полоса записывала "сборка USDC-ноги не загрузилась:
    # KeyError" и не могла назвать ни ключа, ни места. Вид приводится один раз
    # внутри instrukcii(), и берём мы ровно тот nonce, который попал в инструкции.
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

        _C, _PP, SB, _B = _kirpichi()
        TS = _dvuhshagovyj()
        # НАША ТАБЛИЦА АДРЕСОВ -- ПЕРВОЙ (правка Code-1 01.10). Без неё две ноги не
        # влезают в 1232 байта ни на одном типе пула: замер 1326...1703 байта на 32
        # живых сделках. Таблицы источника и ноги нашего пакета не покрывают: у
        # девяти источников из 32 своей таблицы нет вовсе, а статичные счета ноги
        # SOL -> USDC не лежат ни в одной чужой таблице.
        свои = [к for к in (nashi_tablicy or []) if к]
        nuzhny = [k for k in свои + SB._lut_keys(e["tx"])
                  + SB._lut_keys(tx_istochnika) if k not in kesh_nog.luts]
        if nuzhny:
            kesh_nog.load_luts(nuzhny, rpc=rpc_call)
            iz["hot_lut_calls"] = 1
        klyuchi = list(dict.fromkeys(свои + SB._lut_keys(e["tx"])
                                     + SB._lut_keys(tx_istochnika)))
        tablicy = [kesh_nog.luts[k] for k in klyuchi if k in kesh_nog.luts]
        iz["lut_tables"] = len(tablicy)
        iz["nashi_tablicy_vzjaty"] = [к for к in свои if к in kesh_nog.luts]
        iz["nashi_tablicy_ne_vzjaty"] = [к for к in свои if к not in kesh_nog.luts]
        msg = MessageV0.try_compile(Pubkey.from_string(nash_koshelek), ixs, tablicy,
                                    Hash.default())
        syroe = bytes(VersionedTransaction.populate(
            msg, [Signature.default()] * msg.header.num_required_signatures))
    except Exception as exc:  # noqa: BLE001
        iz["ok"] = False
        # То же, что и у списка инструкций: repr с ключом и файл:строка кадра.
        iz["why_not"] = f"сборка USDC-ноги: {_sled(exc)}"
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


# ------------------------------------------------------------- самопроверка

# ЧИСЛО ПРОВЕРОК ОБЪЯВЛЕНО ЗАРАНЕЕ. Молчаливый пропуск -- это провал: если файла
# живых образцов нет или тип перестал разбираться, проверок станет МЕНЬШЕ, и
# самопроверка упадёт на несовпадении числа, а не промолчит зелёным.
ZHDEM_PROVEROK = 81

# Живые образцы: свопы с котировкой USDC по типам пулов. Числа -- ЗАМЕР, они
# объявлены здесь и сверяются по файлам; разошлось -- провал.
FILE_OBRAZCOV = "c3_usdc_noga/obrazcy_usdc_noga.json"
# (тип: [сторона USDC всего, из них вход у источника])
ZHDEM_PO_TIPAM = {"CLMM": [9, 6], "Whirlpool": [16, 13], "DLMM": [15, 12]}
ZHDEM_DAMM2 = [17, 3]          # образцов пула всего / из них с котировкой USDC
# Пересборка инструкции источника байт в байт по USDC-входным сделкам:
# совпало целиком / разошлось ТОЛЬКО на наших местах и это объяснено.
ZHDEM_BAJT_V_BAJT = 28
ZHDEM_OBYASNENO = 6
# Полные списки инструкций, которые собираются на этих же сделках (у остальных
# C.identify_pool выбирает ДРУГОЙ пул маршрута -- это не отказ ноги).
ZHDEM_SPISKOV = {"CLMM": 6, "Whirlpool": 12, "DLMM": 11, "DAMM v2": 3}
# Размер пакета БЕЗ таблиц адресов (мин, макс) по тем же 32 сделкам. Предел сети
# 1232 байта: без таблиц не влезает НИ ОДНА -- это гейт, а не мелочь.
ZHDEM_RAZMER = (1326, 1703)
# Боевой котировщик на тех же живых сделках: DLMM считает нулём чтений, CLMM без
# узла отказывает (его котировщик -- подготовить(), а он читает состояние пула).
ZHDEM_DLMM_BOJ = 12
ZHDEM_CLMM_BEZ_UZLA = 6
# Образец, где источник торгует ВРЕМЕННЫМИ счетами: их нет в балансах, минт берётся
# из инструкции создания. Наших мест у этой сделки два -- вход и выход.
FILE_VREMENNYH = "c3_usdc_noga/obrazec_vremennye_scheta.json"
ZHDEM_VREMENNYH = 2
# Сверка перевода «адреса -> номера -> адреса» на тех же живых сделках. У 40 из 43
# флаги прав, подписи, адреса и вложенные инструкции совпадают бит в бит. У ТРЁХ
# (DLMM RGaZbh8j..., Whirlpool 32k64Gec..., 2uJCHp4p...) узел помечает ключ
# ПРОГРАММЫ только для чтения ПЕРЕД блоком записываемых, чего раскладка сообщения
# Solana не допускает: по header права выходят сдвинутыми на одно место. Кто из
# двух прав, офлайн не решить, поэтому эти три названы числом, а не спрятаны.
# Деньги от этого не зависят: перевод нужен только образцам Code-2 (encoding=json),
# а полоса получает jsonParsed со своего узла. Сверка байт в байт на образцах
# сравнивает АДРЕСА и ДАННЫЕ -- права в неё не входят.
ZHDEM_KRUG_PRIGODNO = 43
ZHDEM_KRUG_SOVPALO = 40
# Кошелёк самопроверки -- кошелёк опыта забора, не боевой: подписи здесь нет вовсе.
KOSHELEK_PROVERKI = "D3JuFoSXuWEMUUdCtoB5NYWnN87vjJSHtDP5rTD6qnph"


def _obrazcy() -> dict:
    """Живые образцы USDC по типам: {тип: [ряды]}. Файла нет -- пустой разбор."""
    import json  # noqa: PLC0415
    C, _PP, _SB, _B = _kirpichi()
    iz = {"ok": False, "why_not": None, "ryady": [], "damm2_vsego": 0}
    p = Path(C.DATA) / FILE_OBRAZCOV
    if not p.exists():
        iz["why_not"] = f"файла образцов {FILE_OBRAZCOV} нет"
        return iz
    d = json.loads(p.read_text(encoding="utf-8"))
    ryady = [dict(r) for r in (d.get("ряды") or [])]
    # DAMM v2 -- из образцов пулов репозитория: копировать их второй раз незачем.
    pd = Path(C.DATA) / "c2_pool_samples" / f"{PROG_DAMM2}.json"
    if pd.exists():
        obr = json.loads(pd.read_text(encoding="utf-8"))
        iz["damm2_vsego"] = len(obr)
        for x in obr:
            if x.get("quote_mint") != USDC:
                continue
            ryady.append({"tip": "DAMM v2", "program": PROG_DAMM2,
                          "пул": x.get("pool_vault"), "usdc_vhod": True,
                          "транзакция": x.get("tx"), "источник": x.get("source"),
                          "минт": x.get("mint"), "сделка": ""})
    iz.update(ok=True, ryady=ryady)
    return iz


def _v_nomera(tx: dict) -> dict | None:
    """ОБРАТНЫЙ перевод для самопроверки: адреса -> номера, как отдаёт encoding="json".

    Нужен только сверке: настоящая jsonParsed-сделка опускается в номера и
    поднимается назад tx_s_adresami, и флаги с адресами обязаны совпасть. Без
    такой сверки права счетов в переводе проверялись бы на слово, а перепутанное
    право записи -- это отказ программы на живых деньгах.

    None -- сделка не годится для сверки (ключи не в разобранном виде).
    """
    t = (tx or {}).get("transaction") or {}
    m = t.get("message") or {}
    syrye = list(m.get("accountKeys") or [])
    if not syrye or not isinstance(syrye[0], dict):
        return None
    stat = [k for k in syrye if (k.get("source") or "transaction") == "transaction"]
    tabl = [k for k in syrye if (k.get("source") or "transaction") != "transaction"]
    zapis = [k["pubkey"] for k in tabl if k.get("writable")]
    chtenie = [k["pubkey"] for k in tabl if not k.get("writable")]
    # Порядок ключей тот же, что у узла: статические, затем записываемые из
    # таблиц, затем читаемые. Разошёлся -- сверять нечего, честный None.
    if [k["pubkey"] for k in syrye] != [k["pubkey"] for k in stat] + zapis + chtenie:
        return None
    klyuchi = [k["pubkey"] for k in syrye]
    podpisanty = [k for k in stat if k.get("signer")]
    h = {"numRequiredSignatures": len(podpisanty),
         "numReadonlySignedAccounts": sum(1 for k in podpisanty if not k.get("writable")),
         "numReadonlyUnsignedAccounts": sum(1 for k in stat
                                            if not k.get("signer") and not k.get("writable"))}

    def _ix(ix: dict) -> dict:
        novyj = {k: v for k, v in ix.items() if k not in ("programId", "accounts")}
        novyj["programIdIndex"] = klyuchi.index(ix["programId"])
        novyj["accounts"] = [klyuchi.index(a) for a in (ix.get("accounts") or [])]
        return novyj

    meta = dict((tx or {}).get("meta") or {})
    meta["innerInstructions"] = [
        dict(g, instructions=[_ix(x) for x in (g.get("instructions") or [])])
        for g in (meta.get("innerInstructions") or [])]
    meta["loadedAddresses"] = {"writable": zapis, "readonly": chtenie}
    return dict(tx, transaction=dict(t, message=dict(
        m, header=h, accountKeys=[k["pubkey"] for k in stat],
        instructions=[_ix(x) for x in (m.get("instructions") or [])])), meta=meta)


def self_test() -> int:  # noqa: C901, PLR0912, PLR0915
    import json  # noqa: PLC0415
    import struct as _struct  # noqa: PLC0415
    import time as _time  # noqa: PLC0415

    C, _PP, SB, B = _kirpichi()
    TS = _dvuhshagovyj()
    proverki = []

    def chk(chto, ok, fakt=None):
        proverki.append((chto, bool(ok), fakt))

    sohr = {k: os.environ.get(k) for k in (FLAG, FLAG_GROUPS)}

    def flag(znach=None, gruppy=None):
        for k, v in ((FLAG, znach), (FLAG_GROUPS, gruppy)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ------------------------------------------------------------- 1. флаг
    flag()
    chk(f"без {FLAG} режим {MODE_OFF} и путь выключен",
        rezhim() == MODE_OFF and not vklyuchena() and not boj(), rezhim())
    flag("1")
    chk(f"{FLAG}=1 -- тень, а не покупка",
        rezhim() == MODE_SHADOW and vklyuchena() and not boj(), rezhim())
    flag(MODE_SHADOW)
    chk(f"{FLAG}={MODE_SHADOW} -- тень", rezhim() == MODE_SHADOW, rezhim())
    flag(MODE_LIVE)
    chk(f"{FLAG}={MODE_LIVE} -- бой", rezhim() == MODE_LIVE and boj(), rezhim())
    flag("2")
    chk(f"{FLAG}=2 -- бой", rezhim() == MODE_LIVE, rezhim())
    flag("мусор")
    chk("непонятное значение флага -- выключено", rezhim() == MODE_OFF, rezhim())
    flag(None, "lane_s0,lane_cand1")
    chk("группа в списке без режима -- тень", rezhim("lane_s0") == MODE_SHADOW,
        rezhim("lane_s0"))
    chk("группа НЕ в списке -- выключено", rezhim("lane_other") == MODE_OFF,
        rezhim("lane_other"))
    chk("группы не названо вовсе -- выключено", rezhim(None) == MODE_OFF, rezhim(None))
    flag(MODE_LIVE, "lane_s0")
    chk("список групп сильнее общего флага: чужая группа -- выключено",
        rezhim("lane_x") == MODE_OFF and rezhim("lane_s0") == MODE_LIVE,
        (rezhim("lane_x"), rezhim("lane_s0")))
    flag()

    # -------------------------------------------- 2. суммы двух ног (числа Code-1)
    chk("запас первого шага -- число двухшагового пути, не своё",
        summy_nog(lamporty=1, price_sol=0.01, q_dec=6)["zapas_shaga_1"]
        == float(TS.ЗАПАС_ШАГА_1) == 0.05, TS.ЗАПАС_ШАГА_1)
    s0 = summy_nog(lamporty=0, price_sol=0.01, q_dec=6)
    chk("размер не положителен -- отказ словами", not s0["ok"] and s0["why_not"],
        s0["why_not"])
    s1 = summy_nog(lamporty=10_000_000, price_sol=None, q_dec=6)
    chk("нет цены ноги -- отказ словами", not s1["ok"] and "цены" in (s1["why_not"] or ""),
        s1["why_not"])
    LAMP = 10_000_000
    CENA = 0.00836102379939595
    s2 = summy_nog(lamporty=LAMP, price_sol=CENA, q_dec=6)
    ozhid = int(D(LAMP) / D(10) ** 9 / D(str(CENA)) * D(10) ** 6 * D(1 - TS.ЗАПАС_ШАГА_1))
    chk("min_out первой ноги -- ожидание по цене ноги минус запас 5 %",
        s2["ok"] and s2["leg1_min_out"] == ozhid, (s2.get("leg1_min_out"), ozhid))
    chk("USDC -- классический SPL: налога нет, вход второй ноги равен min_out первой",
        s2["quote_fee_bps"] == 0
        and s2["leg1_min_out"] == s2["leg2_amount_in"] == s2["leg2_to_pool"],
        (s2["quote_fee_bps"], s2["leg1_min_out"], s2["leg2_amount_in"]))
    s3 = summy_nog(lamporty=LAMP, price_sol=CENA, q_dec=6, quote_program=TOKEN_2022)
    chk("котировка на Token-2022 без ставки -- запас двухшагового, не отказ",
        s3["ok"] and s3["quote_fee_bps"] == TS.ЗАПАС_НАЛОГА_BPS
        and s3["leg2_amount_in"] < s3["leg1_min_out"]
        and s3["leg2_to_pool"] < s3["leg2_amount_in"],
        (s3.get("quote_fee_bps"), s3.get("leg1_min_out"), s3.get("leg2_amount_in"),
         s3.get("leg2_to_pool")))
    s4 = summy_nog(lamporty=LAMP * 10, price_sol=CENA, q_dec=6)
    chk("размер больше -- билет в USDC больше ровно во столько же раз",
        s4["leg1_min_out"] // 10 in (s2["leg1_min_out"] - 1, s2["leg1_min_out"],
                                     s2["leg1_min_out"] + 1),
        (s2["leg1_min_out"], s4["leg1_min_out"]))

    # ------------------------------------------- 3. минимум выхода по цене события
    cena = {"ok": True, "usdc_v_pul": 1_000_000, "token_iz_pula": 500_000_000}
    m1 = min_out_po_sobytiyu(cena, 1_000_000, 0.35)
    chk("минимум выхода по цене события: ожидание и минимум ниже него",
        m1["ok"] and m1["expected_out"] == 500_000_000
        and m1["min_out"] == int(500_000_000 * 0.65),
        (m1.get("expected_out"), m1.get("min_out")))
    m2 = min_out_po_sobytiyu(cena, 1_000_000, 0.50)
    chk("проскальзывание больше -- минимум ниже", m2["min_out"] < m1["min_out"],
        (m1["min_out"], m2["min_out"]))
    m3 = min_out_po_sobytiyu(cena, 0, 0.35)
    chk("сумма ноги не положительна -- отказ", not m3["ok"] and m3["why_not"], m3["why_not"])
    m4 = min_out_po_sobytiyu(cena, 1_000_000, 1.0)
    chk("проскальзывание 1 и выше -- отказ", not m4["ok"] and m4["why_not"], m4["why_not"])
    m5 = min_out_po_sobytiyu({"ok": False, "why_not": WHY_OTHER_SIDE}, 1_000, 0.35)
    chk("цены события нет -- причина переносится как есть",
        not m5["ok"] and m5["why_not"] == WHY_OTHER_SIDE, m5["why_not"])

    # ------------------------------------------------- 4. отказы без исключений
    st_bad = storona({}, programma="НеПрограмма", pul=None)
    chk("незнакомый тип пула -- отказ по имени, без исключения",
        not st_bad["ok"] and WHY_TYPE in (st_bad["why_not"] or ""), st_bad["why_not"])
    st_bad2 = storona({}, programma=PROG_DAMM2)
    chk("ни хранилища, ни пула -- отказ словами",
        not st_bad2["ok"] and "хранилища" in (st_bad2["why_not"] or ""), st_bad2["why_not"])
    st_bad3 = storona({"meta": {}}, programma=PROG_CLMM, pul="нетакойпул")
    chk("шаблона в сделке нет -- отказ словами",
        not st_bad3["ok"] and st_bad3["why_not"], st_bad3["why_not"])
    chk("таблица типов -- шесть программ, у четырёх свой строитель",
        len(TYPES) == 6
        and sum(1 for t in TYPES.values() if t["way"] == WAY_BUILDER) == 4
        and set(TYPES) == {PROG_CLMM, PROG_WHIRLPOOL, PROG_AMMV4, PROG_DBC,
                           PROG_DAMM2, PROG_DLMM},
        sorted((t["label"], t["way"]) for t in TYPES.values()))
    for prog, t in sorted(TYPES.items()):
        if t["way"] != WAY_BUILDER:
            continue
        M = modul(prog)
        chk(f"строитель {t['label']}: под_покупку и инструкция_свопа на месте",
            M is not None and callable(getattr(M, "под_покупку", None))
            and callable(getattr(M, "инструкция_свопа", None))
            and callable(getattr(M, "пользователь_шаблона", None)), t["module"])
    flag()
    r_off = sobrat(tx_istochnika={}, istochnik="", mint="", nash_koshelek="",
                   lamporty=1, kesh_nog=None)
    chk("флаг выключен -- сборка отказывает и ничего не считает",
        not r_off["ok"] and r_off["why_not"] == WHY_OFF, r_off["why_not"])
    flag(MODE_SHADOW)
    r_ten = sobrat(tx_istochnika={}, istochnik="", mint="", nash_koshelek="",
                   lamporty=1, kesh_nog=None)
    chk("режим тени -- транзакции на отправку нет",
        not r_ten["ok"] and r_ten["why_not"] == WHY_SHADOW
        and r_ten["tx_base64"] is None, r_ten["why_not"])
    t_off = ten(tx_istochnika={}, istochnik="", mint="", lamporty=1, kesh_nog=None)
    chk("тень на пустом входе -- отказ словами, без исключения",
        not t_off["ok"] and t_off["why_not"], t_off["why_not"])
    flag(MODE_LIVE)
    r_nokesh = sobrat(tx_istochnika={}, istochnik="", mint="", nash_koshelek="",
                      lamporty=1, kesh_nog=None)
    chk("боевой режим без кэша ног -- отказ, а не исключение",
        not r_nokesh["ok"] and "кэша" in (r_nokesh["why_not"] or ""),
        r_nokesh["why_not"])

    # ------------------------------------------------- 5. живые образцы по типам
    o = _obrazcy()
    chk("файл живых образцов USDC на месте", o["ok"], o.get("why_not"))
    ryady = o["ryady"]
    po_tipam = {}
    for r in ryady:
        po_tipam.setdefault(r["tip"], [0, 0])
        po_tipam[r["tip"]][0] += 1
        po_tipam[r["tip"]][1] += 1 if r.get("usdc_vhod") else 0
    for tip, zhdem in sorted(ZHDEM_PO_TIPAM.items()):
        chk(f"{tip}: сделок с котировкой USDC {zhdem[0]}, из них вход USDC {zhdem[1]}",
            po_tipam.get(tip) == zhdem, po_tipam.get(tip))
    chk(f"DAMM v2: образцов пула {ZHDEM_DAMM2[0]}, из них с котировкой USDC "
        f"{ZHDEM_DAMM2[1]}",
        o["damm2_vsego"] == ZHDEM_DAMM2[0]
        and po_tipam.get("DAMM v2", [0, 0])[1] == ZHDEM_DAMM2[1],
        (o["damm2_vsego"], po_tipam.get("DAMM v2")))

    bajt, obyasneno, ne_na_nashih, cen_ok, cen_drugaya, bez_reversa = 0, 0, [], 0, 0, 0
    storony_ok, razvernutyh = 0, 0
    for r in ryady:
        tx = r["транзакция"]
        st = storona(tx, programma=r["program"], pul=r.get("пул"))
        if not st["ok"]:
            if WHY_NO_REVERSE in (st["why_not"] or ""):
                bez_reversa += 1
            continue
        storony_ok += 1
        razvernutyh += 1 if st["razvernut"] else 0
        ce = cena_sobytiya(tx, st["tpl"], base_mint=st["base_mint"])
        if ce["ok"]:
            cen_ok += 1
        elif ce["why_not"] == WHY_OTHER_SIDE:
            cen_drugaya += 1
        if not r.get("usdc_vhod"):
            continue
        u = polzovatel_istochnika(st, tx)
        src = st["src_tpl"]
        a0, a1 = _struct.unpack_from("<QQ", src["data"], 8)
        ix = instrukciya_nogi(st, user=u, amount_in=a0, min_out=a1,
                             tx_istochnika=tx, kak_u_istochnika=True)
        got = [str(m.pubkey) for m in ix.accounts]
        esli = list(st["tpl"]["accounts"])
        if bytes(ix.data) == bytes(src["data"]) and got == esli:
            bajt += 1
            continue
        # РАСХОЖДЕНИЕ ДОПУСТИМО ТОЛЬКО НА НАШИХ МЕСТАХ И ТОЛЬКО ОБЪЯСНЁННОЕ.
        # НАШИ МЕСТА НАХОДЯТСЯ НЕ ПО ПАМЯТИ, А ЗАМЕРОМ: собираем ту же
        # инструкцию ДРУГИМ кошельком и смотрим, какие места поехали -- это и
        # есть места, куда строитель подставляет наше. На них у источника стоит
        # счёт того же минта, но не наш ATA: либо его собственный счёт не-ATA,
        # либо чужой вовсе -- сделка шла маршрутизатором, и выход лёг на его счёт.
        DRUGOJ = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
        ix_d = instrukciya_nogi(st, user=DRUGOJ, amount_in=a0, min_out=a1,
                                tx_istochnika=tx, kak_u_istochnika=True)
        drugie = [str(m.pubkey) for m in ix_d.accounts]
        nashi_mesta = {j for j, (x_, y_) in enumerate(zip(got, drugie)) if x_ != y_}
        stroki = {x["account"]: x for x in C.token_rows(tx).values()}
        razn = [j for j, (a, b) in enumerate(zip(esli, got)) if a != b]
        vse_nashi = bool(razn) and set(razn) <= nashi_mesta
        for j in razn:
            rr = stroki.get(esli[j]) or {}
            if rr.get("mint") not in (USDC, st["base_mint"]):
                vse_nashi = False
        if vse_nashi and bytes(ix.data) == bytes(src["data"]):
            obyasneno += 1
        else:
            ne_na_nashih.append((r["tip"], r.get("сделка"), razn,
                                 sorted(nashi_mesta)))
    chk(f"сторона USDC разобралась у {len(ryady) - bez_reversa} сделок из {len(ryady)}",
        storony_ok == len(ryady) - bez_reversa, (storony_ok, len(ryady), bez_reversa))
    chk("продажа источника у типа без переворота стороны -- честный отказ по имени",
        bez_reversa == 3, bez_reversa)
    chk("шаблон развёрнут ровно на сделках-продажах тех типов, где это умеют",
        razvernutyh == 6, razvernutyh)
    chk("цена события считается у всех USDC-входных и ни у одной обратной",
        cen_ok == sum(z[1] for z in ZHDEM_PO_TIPAM.values()) + ZHDEM_DAMM2[1]
        and cen_drugaya == 6, (cen_ok, cen_drugaya))
    chk(f"пересборка инструкции источника байт в байт: {ZHDEM_BAJT_V_BAJT}",
        bajt == ZHDEM_BAJT_V_BAJT, bajt)
    chk(f"разошлось только на наших местах и объяснено: {ZHDEM_OBYASNENO}",
        obyasneno == ZHDEM_OBYASNENO, obyasneno)
    chk("ни одного расхождения вне наших мест", not ne_na_nashih, ne_na_nashih[:3])

    # ------------------------------------- 6. полный список инструкций двух ног
    put_nogi = Path(C.DATA) / "nogi_shablony.json"
    zap = None
    if put_nogi.exists():
        import bloom_nogi_shablony as NSH  # noqa: PLC0415
        obr1 = (json.loads(put_nogi.read_text(encoding="utf-8")).get("shablony")
                or {}).get(USDC)
        zap = NSH.запись_из_образца(USDC, obr1) if obr1 else None
    chk("статичный шаблон первой ноги SOL -> USDC в репозитории есть и разбирается",
        isinstance(zap, dict) and zap.get("q_dec") == 6
        and zap.get("program") == "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
        zap if isinstance(zap, str) else (isinstance(zap, dict) and zap.get("q_dec")))
    spiskov = {}
    kesh = None
    if isinstance(zap, dict):
        # Цена -- ЦЕНА ОБРАЗЦА, и это сказано вслух: на пути покупки её читают по
        # остаткам хранилищ, здесь же проверяется арифметика, а не цена.
        zap = dict(zap, price_sol=zap["price_sol_obrazca"], checked_at=_time.time())
        kesh = SB.LegCache({}, None)
        kesh.entries[USDC] = zap
        W = KOSHELEK_PROVERKI
        poryadok_ok, argumenty_ok, nons_ok, chaevye_ok = 0, 0, 0, 0
        razmery = []
        for r in ryady:
            tx = r["транзакция"]
            st = storona(tx, programma=r["program"], pul=r.get("пул"))
            if not st["ok"] or not r.get("usdc_vhod"):
                continue
            ist = r.get("источник") or (sorted(C.signers(tx))[0] if C.signers(tx) else None)
            res = instrukcii(tx_istochnika=tx, istochnik=ist,
                             mint=r.get("минт") or st["base_mint"], nash_koshelek=W,
                             lamporty=10_000_000, kesh_nog=kesh, proskalzyvanie=0.35,
                             chaevye_lamporty=0, chaevye_adres=None)
            if not res["ok"]:
                continue
            spiskov[r["tip"]] = spiskov.get(r["tip"], 0) + 1
            ixs = res["ixs"]
            progs = [str(i.program_id) for i in ixs]
            zhdem_progs = ["ComputeBudget111111111111111111111111111111",
                           "ComputeBudget111111111111111111111111111111",
                           B.ATA_PROGRAM, B.ATA_PROGRAM, B.ATA_PROGRAM,
                           B.SYSTEM, (zap["mv"] or {}).get("quote_program"),
                           zap["program"], res["pool_program"]]
            if len(ixs) == 9 and progs == zhdem_progs \
                    and bytes(ixs[0].data)[:1] == b"\x02" \
                    and bytes(ixs[1].data)[:1] == b"\x03" \
                    and all(bytes(ixs[j].data) == b"\x01" for j in (2, 3, 4)) \
                    and bytes(ixs[6].data)[:1] == b"\x11":
                poryadok_ok += 1
            l1 = _struct.unpack_from("<QQ", bytes(ixs[7].data), 8)
            l2 = _struct.unpack_from("<QQ", bytes(ixs[8].data), 8)
            if l1 == (10_000_000, res["leg1_min_out"]) \
                    and l2 == (res["leg2_amount_in"], res["min_out"]) \
                    and res["leg2_amount_in"] == res["leg1_min_out"]:
                argumenty_ok += 1
            # РАЗМЕР ПАКЕТА -- ЗАМЕР, А НЕ ПРЕДПОЛОЖЕНИЕ. Таблиц адресов без сети
            # взять негде, поэтому компиляция идёт БЕЗ них: число показывает,
            # сколько весит та же транзакция, когда сжимать нечем (с НАШЕЙ
            # таблицей Code-1 замерил 643...702 байта, см. страницу врезки).
            # min_out передаётся готовым: от него размер не зависит, а боевой
            # котировщик типа без узла отказал бы и замера бы не было.
            sb = sobrat(tx_istochnika=tx, istochnik=ist,
                        mint=r.get("минт") or st["base_mint"], nash_koshelek=W,
                        lamporty=10_000_000, kesh_nog=kesh, proskalzyvanie=0.35,
                        chaevye_lamporty=0, chaevye_adres=None,
                        min_out_vneshnij=res["min_out"],
                        rpc_call=lambda _m, _p: {"value": []})
            if isinstance(sb.get("size"), int):
                razmery.append(sb["size"])
            sn = instrukcii(tx_istochnika=tx, istochnik=ist,
                            mint=r.get("минт") or st["base_mint"], nash_koshelek=W,
                            lamporty=10_000_000, kesh_nog=kesh, proskalzyvanie=0.35,
                            chaevye_lamporty=7_777, chaevye_adres=W,
                            nons=("5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d", W))
            if sn["ok"] and len(sn["ixs"]) == 11 \
                    and str(sn["ixs"][0].program_id) == B.SYSTEM \
                    and bytes(sn["ixs"][0].data)[:1] == b"\x04":
                nons_ok += 1
            if sn["ok"] and str(sn["ixs"][-1].program_id) == B.SYSTEM \
                    and sn["tips_total_lamports"] == 7_777:
                chaevye_ok += 1
        vsego = sum(ZHDEM_SPISKOV.values())
        chk(f"полных списков инструкций собрано {vsego} и у всех порядок "
            f"двухшагового пути",
            poryadok_ok == vsego, (poryadok_ok, vsego))
        chk("суммы стоят в инструкциях: нога 1 (размер, min_out), нога 2 "
            "(вход = min_out ноги 1, min_out)",
            argumenty_ok == vsego, (argumenty_ok, vsego))
        chk("долговечный nonce -- первой инструкцией, чаевые -- последними",
            nons_ok == vsego and chaevye_ok == vsego, (nons_ok, chaevye_ok, vsego))
        chk("БЕЗ таблиц адресов две ноги в 1232 байта не влезают ни на одном типе: "
            f"замер {min(razmery or [0])}--{max(razmery or [0])} байт на {vsego} сделках",
            len(razmery) == vsego
            and min(razmery) == ZHDEM_RAZMER[0] and max(razmery) == ZHDEM_RAZMER[1]
            and min(razmery) > TS.ПРЕДЕЛ_РАЗМЕРА_TX,
            (len(razmery), min(razmery or [0]), max(razmery or [0])))
    for tip, zhdem in sorted(ZHDEM_SPISKOV.items()):
        chk(f"{tip}: списков инструкций {zhdem}", spiskov.get(tip, 0) == zhdem,
            spiskov.get(tip, 0))

    # ------- 6б. ЖИВОЙ СБОЙ 02.10: ПОЛОСА ПЕРЕДАЁТ NONCE СЛОВАРЁМ, А НЕ ПАРОЙ
    # Сигнал Xk9onqHkpULD, группа lane_s0: пул цели Meteora DAMM v2, первая нога
    # Pump AMM, котировка USDC. Тень сказала ГОДНА, а боевая сборка ответила
    # "сборка USDC-ноги не загрузилась: KeyError" -- потому что bloom_own_send
    # отдавал сюда СЛОВАРЬ нонс_для_сделки (account/authority/blockhash), а
    # advance_nonce и ответ брали nons[0]. Единственный годный сигнал USDC-ноги за
    # сутки потерян не по экономике, а на этом. Проверки ниже повторяют тот путь
    # БЕЗ СЕТИ на живых образцах и падают ровно на том дефекте.
    ND = {"ok": True, "account": "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d",
          "authority": KOSHELEK_PROVERKI, "warm": True,
          "blockhash": "GHtXQBsoZHVnNFa9YevAzFr17DJjgHXk3ycTKD5xD3Zi"}
    NP = (ND["account"], ND["authority"])
    chk("nonce не передан -- это пара None, а не отказ",
        TS.пара_нонса(None) == {"ok": True, "пара": None, "почему": None},
        TS.пара_нонса(None))
    chk("пара проходит как есть", TS.пара_нонса(NP)["пара"] == NP,
        TS.пара_нонса(NP))
    chk("СЛОВАРЬ полосы (account/authority) приводится к паре, а не к KeyError",
        TS.пара_нонса(ND)["ok"] and TS.пара_нонса(ND)["пара"] == NP,
        TS.пара_нонса(ND))
    chk("непонятный вид nonce -- отказ СЛОВАМИ, а не пустая пара и не исключение",
        TS.пара_нонса(7)["ok"] is False and TS.пара_нонса(7)["пара"] is None
        and "nonce" in (TS.пара_нонса(7)["почему"] or ""), TS.пара_нонса(7))
    chk("в словаре нет распорядителя -- отказ словами: его не выдумать",
        TS.пара_нонса({"account": ND["account"]})["ok"] is False
        and TS.пара_нонса({"account": ND["account"]})["почему"],
        TS.пара_нонса({"account": ND["account"]}))
    # СТРОКА СБОЯ ОБЯЗАНА НАЗВАТЬ КЛЮЧ И МЕСТО. Ровно этого не хватило 02.10:
    # в записи стояло одно слово "KeyError". Исключение берётся настоящее, с
    # настоящим traceback: по выдуманной строке проверять нечего.
    try:
        {"а": 1}[0]
        сл_сбоя = "исключения не было"
    except KeyError as exc_k:
        сл_сбоя = _sled(exc_k)
    chk("строка сбоя называет и ключ (repr), и файл:строку падения",
        "KeyError(0)" in сл_сбоя and "c2_usdc_noga.py:" in сл_сбоя, сл_сбоя)
    if kesh is not None:
        r_d2 = next((r for r in ryady if r["tip"] == "DAMM v2"
                     and r.get("usdc_vhod")), None)
        st_d2 = (storona(r_d2["транзакция"], programma=r_d2["program"],
                         pul=r_d2.get("пул")) if r_d2 else {"ok": False})
        ist_d2 = ((r_d2.get("источник")
                   or (sorted(C.signers(r_d2["транзакция"]))[0]
                       if C.signers(r_d2["транзакция"]) else ""))
                  if r_d2 else "")

        def _ixs_нонсом(нонс_):
            """Список инструкций живого сигнала DAMM v2 при данном виде nonce."""
            return instrukcii(
                tx_istochnika=r_d2["транзакция"], istochnik=ist_d2,
                mint=r_d2.get("минт") or st_d2["base_mint"],
                nash_koshelek=KOSHELEK_PROVERKI, lamporty=10_000_000,
                kesh_nog=kesh, proskalzyvanie=0.35, chaevye_lamporty=0,
                chaevye_adres=None, nons=нонс_)

        def _слепок(res_):
            return [(str(i.program_id), bytes(i.data),
                     [str(m.pubkey) for m in i.accounts]) for i in (res_["ixs"] or [])]

        try:
            сл_пара = _ixs_нонсом(NP)
            сл_слов = _ixs_нонсом(ND)
            одно = (сл_пара["ok"] and сл_слов["ok"]
                    and _слепок(сл_пара) == _слепок(сл_слов))
            факт = (сл_пара.get("why_not"), сл_слов.get("why_not"))
        except Exception as exc:  # noqa: BLE001
            одно, факт = False, repr(exc)
        chk("DAMM v2 + нога Pump AMM: со СЛОВАРЁМ полосы собирается тот же список "
            "инструкций, что с парой (живой путь сигнала 02.10)", одно, факт)
        try:
            сб_д = sobrat(tx_istochnika=r_d2["транзакция"], istochnik=ist_d2,
                          mint=r_d2.get("минт") or st_d2["base_mint"],
                          nash_koshelek=KOSHELEK_PROVERKI, lamporty=10_000_000,
                          kesh_nog=kesh, proskalzyvanie=0.35, chaevye_lamporty=0,
                          chaevye_adres=None, min_out_vneshnij=сл_пара["min_out"],
                          nons=ND, rpc_call=lambda _m, _p: {"value": []})
            # Отказ по РАЗМЕРУ здесь законный (таблиц адресов офлайн нет), а вот
            # исключения наружу sobrat() не выпускает вовсе: его ответ -- словарь.
            без_исключения = (isinstance(сб_д, dict)
                              and сб_д.get("nonce_account") == ND["account"])
            факт_д = (сб_д.get("why_not"), сб_д.get("nonce_account"))
        except Exception as exc:  # noqa: BLE001
            без_исключения, факт_д = False, repr(exc)
        chk("sobrat() со словарём nonce НЕ бросает исключение и называет аккаунт "
            "nonce в ответе", без_исключения, факт_д)
    # ---------------------------------- 7. боевой режим: котировщик типа, не цена события
    sohr_cu = os.environ.get(IMYA_FLAGA_CU)
    os.environ.pop(IMYA_FLAGA_CU, None)
    chk(f"предел CU ноги без {IMYA_FLAGA_CU} -- {CU_NOGI_PO_UMOLCHANIYU}, и это выше "
        f"замера Code-1 {CU_ZAMER_CODE1[1]}",
        cu_nogi() == CU_NOGI_PO_UMOLCHANIYU > CU_ZAMER_CODE1[1], cu_nogi())
    os.environ[IMYA_FLAGA_CU] = "300000"
    chk("предел CU берётся из окружения", cu_nogi() == 300_000, cu_nogi())
    os.environ[IMYA_FLAGA_CU] = "мусор"
    chk("непонятный предел CU -- умолчание, а не ноль",
        cu_nogi() == CU_NOGI_PO_UMOLCHANIYU, cu_nogi())
    os.environ[IMYA_FLAGA_CU] = "0"
    chk("ноль CU -- умолчание: отправка с нулём CU отказала бы в сети",
        cu_nogi() == CU_NOGI_PO_UMOLCHANIYU, cu_nogi())
    if sohr_cu is None:
        os.environ.pop(IMYA_FLAGA_CU, None)
    else:
        os.environ[IMYA_FLAGA_CU] = sohr_cu
    chk("котировщик назван у всех шести типов и у каждого свой способ",
        set(KOTIROVSHCHIKI) == set(TYPES)
        and KOTIROVSHCHIKI[PROG_CLMM] == KOTIROVSHCHIKI[PROG_WHIRLPOOL]
        == KOTIROVSHCHIKI[PROG_AMMV4] == KOTIROVSHCHIKI[PROG_DBC] == SPOSOB_STROITEL
        and KOTIROVSHCHIKI[PROG_DAMM2] == SPOSOB_KIRPICHI
        and KOTIROVSHCHIKI[PROG_DLMM] == SPOSOB_DLMM,
        sorted(KOTIROVSHCHIKI.items()))
    kb_net = min_out_boj({"ok": False, "why_not": "нет стороны"}, {}, amount_in=1,
                         proskalzyvanie=0.35)
    chk("боевой котировщик без стороны ноги -- отказ словами, без исключения",
        not kb_net["ok"] and kb_net["why_not"], kb_net["why_not"])
    kb_chuzhoj = min_out_boj({"ok": True, "tpl": {"program": "НеПрограмма"},
                              "way": WAY_BRICKS, "label": "нет такого"}, {},
                             amount_in=1, proskalzyvanie=0.35)
    chk("тип без боевого котировщика -- отказ по имени, а не цена события",
        not kb_chuzhoj["ok"] and kb_chuzhoj["why_not"] == WHY_NET_KOTIROVSHCHIKA,
        kb_chuzhoj["why_not"])
    # На живых образцах: DLMM считает боевой min_out нулём чтений, CLMM без узла
    # отказывает (его котировщик -- подготовить(), а он читает состояние пула).
    dlmm_ok, clmm_otkaz, bez_chtenij = 0, 0, 0
    for r in ryady:
        tx = r["транзакция"]
        st = storona(tx, programma=r["program"], pul=r.get("пул"))
        if not st["ok"] or not r.get("usdc_vhod"):
            continue
        kb = min_out_boj(st, tx, amount_in=1_000_000, proskalzyvanie=0.35,
                         rpc_call=None, nalog_vyhoda=(0, None))
        if r["tip"] == "DLMM" and kb.get("ok"):
            dlmm_ok += 1
            bez_chtenij += 1 if kb.get("chtenij") == 0 else 0
        if r["tip"] == "CLMM" and not kb.get("ok"):
            clmm_otkaz += 1
    chk(f"DLMM: боевой min_out считается нулём чтений у {ZHDEM_DLMM_BOJ} сделок",
        dlmm_ok == ZHDEM_DLMM_BOJ and bez_chtenij == ZHDEM_DLMM_BOJ,
        (dlmm_ok, bez_chtenij))
    chk(f"CLMM без узла: боевой котировщик отказывает у всех {ZHDEM_CLMM_BEZ_UZLA} "
        f"-- подготовить() читает состояние пула",
        clmm_otkaz == ZHDEM_CLMM_BEZ_UZLA, clmm_otkaz)
    # Сборка в бою НЕ подменяет отказ котировщика ценой события.
    flag(MODE_LIVE)
    r_clmm = next(r for r in ryady if r["tip"] == "CLMM" and r.get("usdc_vhod"))
    st_clmm = storona(r_clmm["транзакция"], programma=r_clmm["program"],
                      pul=r_clmm.get("пул"))
    ist_clmm = (sorted(C.signers(r_clmm["транзакция"]))[0]
                if C.signers(r_clmm["транзакция"]) else "")
    sb_boj = (sobrat(tx_istochnika=r_clmm["транзакция"], istochnik=ist_clmm,
                     mint=st_clmm["base_mint"], nash_koshelek=KOSHELEK_PROVERKI,
                     lamporty=10_000_000, kesh_nog=kesh, proskalzyvanie=0.35,
                     chaevye_lamporty=0, chaevye_adres=None, rpc_call=None)
              if kesh is not None else {"ok": None})
    chk("в бою отказ котировщика -- это отказ сборки, а не подмена ценой события",
        sb_boj.get("ok") is False and sb_boj.get("tx_base64") is None
        and "цена события" not in str(sb_boj.get("min_out_from") or ""),
        (sb_boj.get("why_not"), sb_boj.get("min_out_from")))
    flag()

    # ------------------------- 8. сделки с НОМЕРАМИ счетов (encoding="json")
    tuda_obratno, idempotentno, prigodno, adresa_te_zhe = 0, 0, 0, 0
    for r in ryady:
        tx = r["транзакция"]
        v_nomera = _v_nomera(tx)
        if v_nomera is None:
            continue
        nazad = tx_s_adresami(v_nomera)
        m0 = (tx["transaction"]["message"] or {})
        m1 = (nazad["transaction"]["message"] or {})
        klyuchi_te_zhe = [(k["pubkey"], bool(k.get("writable")), bool(k.get("signer")))
                          for k in m0["accountKeys"]] == \
                         [(k["pubkey"], bool(k.get("writable")), bool(k.get("signer")))
                          for k in m1["accountKeys"]]
        ix_te_zhe = [(x.get("programId"), list(x.get("accounts") or []))
                     for x in m0["instructions"]] == \
                    [(x.get("programId"), list(x.get("accounts") or []))
                     for x in m1["instructions"]]
        vnutri_te_zhe = [[(x.get("programId"), list(x.get("accounts") or []))
                          for x in (g.get("instructions") or [])]
                         for g in (tx.get("meta") or {}).get("innerInstructions") or []] == \
                        [[(x.get("programId"), list(x.get("accounts") or []))
                          for x in (g.get("instructions") or [])]
                         for g in (nazad.get("meta") or {}).get("innerInstructions") or []]
        prigodno += 1
        tuda_obratno += 1 if (klyuchi_te_zhe and ix_te_zhe and vnutri_te_zhe) else 0
        adresa_te_zhe += 1 if (ix_te_zhe and vnutri_te_zhe) else 0
        idempotentno += 1 if tx_s_adresami(tx) is tx else 0
    chk(f"обратный перевод вышел у всех {ZHDEM_KRUG_PRIGODNO} образцов -- сверять есть что",
        prigodno == ZHDEM_KRUG_PRIGODNO, prigodno)
    chk(f"адреса и вложенные инструкции в круге совпадают у всех "
        f"{ZHDEM_KRUG_PRIGODNO}: байт в байт на образцах от прав не зависит",
        adresa_te_zhe == ZHDEM_KRUG_PRIGODNO, adresa_te_zhe)
    chk(f"права и подписи в круге совпадают у {ZHDEM_KRUG_SOVPALO} из "
        f"{ZHDEM_KRUG_PRIGODNO}; у остальных узел ставит ключ программы только "
        f"для чтения ПЕРЕД записываемыми -- раскладка сообщения такого не допускает",
        tuda_obratno == ZHDEM_KRUG_SOVPALO, tuda_obratno)
    chk("уже разобранную сделку перевод возвращает как есть",
        idempotentno == len(ryady), (idempotentno, len(ryady)))
    try:
        tx_s_adresami({"transaction": {"message": {"accountKeys": ["A"],
                                                   "instructions": [{"programIdIndex": 0,
                                                                     "accounts": []}]}}})
        bez_header = False
    except OshibkaNogi:
        bez_header = True
    chk("сделка без message.header -- исключение по имени, а не выдуманные права",
        bez_header, bez_header)

    # ------------------- 9. минт счёта: по балансам и по инструкции создания
    put_vrem = Path(C.DATA) / FILE_VREMENNYH
    sig_vrem = None
    if put_vrem.exists():
        sig_vrem = (json.loads(put_vrem.read_text(encoding="utf-8")).get("сигнал")
                    or {})
    chk(f"образец со временными счетами на месте ({FILE_VREMENNYH})",
        bool(sig_vrem) and bool(sig_vrem.get("tx")), put_vrem.exists())
    if sig_vrem and sig_vrem.get("tx"):
        tx_v = tx_s_adresami(sig_vrem["tx"])
        st_v = storona(tx_v, programma=sig_vrem.get("программа_пула"),
                       pul=sig_vrem.get("пул"))
        u_v = polzovatel_istochnika(st_v, tx_v) if st_v.get("ok") else None
        iz_balansov = [a for a in (st_v.get("tpl") or {}).get("accounts") or []
                       if any(r["account"] == a and r.get("mint")
                              for r in C.token_rows(tx_v).values())]
        chk("минт счёта по балансам находится", bool(iz_balansov)
            and mint_scheta(tx_v, iz_balansov[0]) is not None,
            len(iz_balansov))
        # Два наших места этой сделки -- счета, созданные и закрытые внутри неё:
        # в балансах их нет, и минт берётся из инструкции создания.
        vne = [a for a in (st_v.get("tpl") or {}).get("accounts") or []
               if not any(r["account"] == a for r in C.token_rows(tx_v).values())
               and mint_scheta(tx_v, a) in (USDC, st_v.get("base_mint"))]
        chk("минт счёта, которого нет в балансах, берётся из инструкции создания "
            f"({ZHDEM_VREMENNYH} счёта у этой сделки)",
            len(vne) == ZHDEM_VREMENNYH and bool(u_v), (len(vne), u_v))

    for k, v in sohr.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    plohih = [p for p in proverki if not p[1]]
    for chto, ok, fakt in proverki:
        print(("ok  " if ok else "НЕТ ") + chto + ("" if ok else f"  -- {fakt!r}"))
    print(f"\nпроверок {len(proverki)}, ждали {ZHDEM_PROVEROK}, не прошло {len(plohih)}")
    if len(proverki) != ZHDEM_PROVEROK:
        print("ЧИСЛО ПРОВЕРОК НЕ СОВПАЛО -- молчаливый пропуск считается провалом")
        return 1
    return 1 if plohih else 0


if __name__ == "__main__":
    raise SystemExit(self_test())
