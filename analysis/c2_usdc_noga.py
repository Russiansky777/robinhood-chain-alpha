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
WHY_OTHER_SIDE = ("своп источника шёл в другую сторону -- цену покупки "
                  "из обратного свопа брать нельзя")
WHY_NO_REVERSE = ("сделка источника -- продажа, а переставить стороны у этого типа "
                  "нечем (нога идёт кирпичами c2_swap_build)")
WHY_TYPE = "тип пула не в таблице USDC-ноги"
WHY_OFF = f"USDC-нога выключена флагом {FLAG}"
WHY_SHADOW = "режим тени: транзакция на отправку не собирается"
WHY_EXACT_OUT = "шаг 2 с инструкцией точного выхода -- этим путём не берём"


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
               cu_units: int = 800_000, prioritet_lamporty: int = 1_000_000,
               chaevye_lamporty: int = 1_000_000, chaevye_spiskom: list | None = None,
               chaevye_adres: str | None = None, nons: tuple | None = None,
               nalog_kotirovki_bps=None, min_out_vneshnij: int | None = None,
               gruppa: str | None = None) -> dict:
    """СПИСОК ИНСТРУКЦИЙ двух ног в порядке двухшагового пути -- без компиляции.

    Отдельно от sobrat() по одной причине: порядок инструкций -- это и есть
    механизм, и проверять его надо списком, а не по собранной транзакции (её
    может не быть вовсе: без таблиц адресов две ноги в 1232 байта не влезают).
    """
    iz = {"ok": False, "why_not": None, "route": ROUTE, "steps": 2,
          "rezhim": rezhim(gruppa), "pool_program": None, "label": None, "way": None,
          "quote_mint": USDC, "leg1_pool_program": None, "leg1_template_age_s": None,
          "leg1_min_out": None, "leg2_amount_in": None, "leg2_to_pool": None,
          "quote_fee_bps": None, "min_out": None, "expected_out": None,
          "min_out_from": None, "razvernut": None, "slippage_used": proskalzyvanie,
          "ixs": None, "leg1_entry": None, "tip_account": None, "tips": None,
          "tips_total_lamports": None}
    try:
        C, _PP, SB, B = _kirpichi()
        TS = _dvuhshagovyj()
    except Exception as exc:  # noqa: BLE001
        iz["why_not"] = f"модули сборки не загружены: {type(exc).__name__}"
        return iz
    if kesh_nog is None:
        iz["why_not"] = "кэша шаблонов первой ноги нет -- собирать не из чего"
        return iz
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
            iz.update(min_out=min_out, min_out_from="строитель типа")
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
        iz["why_not"] = f"сборка USDC-ноги: {type(exc).__name__}: {str(exc)[:160]}"
        return iz
    iz.update(ok=True, ixs=ixs, leg1_entry=e)
    return iz


def sobrat(*, tx_istochnika: dict, istochnik: str, mint: str, nash_koshelek: str,
           lamporty: int, kesh_nog, proskalzyvanie: float = 0.35,
           cu_units: int = 800_000, prioritet_lamporty: int = 1_000_000,
           chaevye_lamporty: int = 1_000_000, chaevye_spiskom: list | None = None,
           chaevye_adres: str | None = None, nons: tuple | None = None,
           rpc_call=None, gruppa: str | None = None, nalog_kotirovki_bps=None,
           min_out_vneshnij: int | None = None) -> dict:
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
        gruppa=gruppa)
    iz.update(tx_base64=None, size=None, build_ms=None,
              nonce_account=(str(nons[0]) if nons else None))
    ixs = iz.pop("ixs", None)
    e = iz.pop("leg1_entry", None)
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
        nuzhny = [k for k in SB._lut_keys(e["tx"]) + SB._lut_keys(tx_istochnika)
                  if k not in kesh_nog.luts]
        if nuzhny:
            kesh_nog.load_luts(nuzhny, rpc=rpc_call)
            iz["hot_lut_calls"] = 1
        klyuchi = list(dict.fromkeys(SB._lut_keys(e["tx"])
                                     + SB._lut_keys(tx_istochnika)))
        tablicy = [kesh_nog.luts[k] for k in klyuchi if k in kesh_nog.luts]
        iz["lut_tables"] = len(tablicy)
        msg = MessageV0.try_compile(Pubkey.from_string(nash_koshelek), ixs, tablicy,
                                    Hash.default())
        syroe = bytes(VersionedTransaction.populate(
            msg, [Signature.default()] * msg.header.num_required_signatures))
    except Exception as exc:  # noqa: BLE001
        iz["ok"] = False
        iz["why_not"] = f"сборка USDC-ноги: {type(exc).__name__}: {str(exc)[:160]}"
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
ZHDEM_PROVEROK = 55

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
    if isinstance(zap, dict):
        # Цена -- ЦЕНА ОБРАЗЦА, и это сказано вслух: на пути покупки её читают по
        # остаткам хранилищ, здесь же проверяется арифметика, а не цена.
        zap = dict(zap, price_sol=zap["price_sol_obrazca"], checked_at=_time.time())
        kesh = SB.LegCache({}, None)
        kesh.entries[USDC] = zap
        W = "D3JuFoSXuWEMUUdCtoB5NYWnN87vjJSHtDP5rTD6qnph"
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
            # сколько весит та же транзакция, когда сжимать нечем.
            sb = sobrat(tx_istochnika=tx, istochnik=ist,
                        mint=r.get("минт") or st["base_mint"], nash_koshelek=W,
                        lamporty=10_000_000, kesh_nog=kesh, proskalzyvanie=0.35,
                        chaevye_lamporty=0, chaevye_adres=None,
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
