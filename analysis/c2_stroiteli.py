#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""РЕЕСТР СТРОИТЕЛЕЙ ПО ТИПАМ ПУЛОВ -- одна дверь для денежного пути и приёмки.

ЗАЧЕМ ЭТОТ ФАЙЛ. Приёмка Code-1 (deploy/checks/priemka_stroitelya.py) заявляет:
«раскладку инструкции он спрашивает у денежного пути (c2_swap_build.SPECS)… Новый
строитель принимается без единой правки здесь: достаточно, чтобы его программа
была в SPECS денежного пути». Замысел верный, но одна общая таблица SPECS не
описывает Orca Whirlpool: там наши токеновые счета названы A и B, а не
вход/выход, а направление свопа лежит в ДАННЫХ инструкции, не в порядке счетов.
Поэтому:
  * `roles_damm2` при котировке на стороне B положил бы наш ATA стороны входа на
    место token_owner_account_a, а у программы стоит проверка
    `token_owner_account_a.mint == whirlpool.token_mint_a` -- чужой счёт и откат;
  * `swap_instruction` переносит хвост данных источника как есть, а у Whirlpool в
    хвосте байт a_to_b: для нашей покупки его надо пересобрать, а не копировать.
Проверено числом: из 25 пулов сбора Code-2 против SOL у 24 SOL -- это сторона A,
у 1 -- сторона B; то есть один сигнал из двадцати пяти собрался бы с чужим ATA
МОЛЧА. Значит нужна не запись в SPECS, а развилка «у этой программы свой
строитель».

ЧТО ЗДЕСЬ ЕСТЬ. Реестр «адрес программы -> модуль строителя» и четыре
переходника с ТОЧНО ТЕМИ ЖЕ подписями, что у одноимённых функций c2_swap_build:
extract_template, mints_and_vaults, swap_instruction, build_buy. Логика типов
пулов лежит в самих модулях (c2_clmm_stroitel, c2_whirlpool_stroitel,
c2_dbc_stroitel), здесь только выбор модуля и приведение вида.

ПРАВКА ОБЩЕГО КОДА, КОТОРУЮ ЭТО ТРЕБУЕТ (делает Code-1; я общие модули не
правлю). В analysis/c2_swap_build.py семь строк:
  1. рядом с прочими константами программ:
       WHIRLPOOL = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
  2. сразу после литерала SPECS:
       SPECS.update(__import__("c2_stroiteli").specs())
  3. первой строкой spec_of:
       if (tpl or {}).get("спец"): return dict(tpl["спец"])
  4-7. первой строкой extract_template, mints_and_vaults, swap_instruction и
     build_buy -- по одной:
       _м = __import__("c2_stroiteli").модуль(<программа этого вызова>)
       if _м is not None: return __import__("c2_stroiteli").<та же функция>(…те же аргументы…)
В analysis/bloom_own_send.py -- пять строк гейта для WHIRLPOOL (имя типа в
ИМЕНА_ТИПОВ, функция whirlpool_включён, запись в ДОП_ТИПЫ_ПОЛОСЫ, имя в
имя_строителя_по_адресу) -- подробно в docs/stroiteli_whirlpool_kak_vstroit.md.

После этих правок приёмка Code-1 принимает все три типа БЕЗ единой правки в
самой приёмке -- ровно так, как она и задумана: гейт `program not in SPECS`
проходит, spec_of отдаёт раскладку из шаблона, а байт в байт, сборка полосой и
симуляция идут через переходники.

Своих денег модуль не двигает и подписей не делает.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

WSOL = "So11111111111111111111111111111111111111112"

# Адрес программы -> имя модуля строителя. Больше про типы пулов реестр не знает.
МОДУЛИ = {
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "c2_clmm_stroitel",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "c2_whirlpool_stroitel",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "c2_dbc_stroitel",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "c2_ammv4_stroitel",
}
# Программы, у которых ЕСТЬ раскладка в общем SPECS. На них переходники не
# вмешиваются по умолчанию: денежный путь Code-1 их уже собирает, и менять его
# поведение молча -- не дело реестра. Строитель включается флагом самого модуля.
УЖЕ_В_SPECS = ("CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
                "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN")
# Общий рубильник реестра: выключить развилку целиком, не правя код.
ENV_ВЫКЛ = "BLOOM_STROITELI_OFF"


class ОшибкаРеестра(Exception):
    pass


def выключен() -> bool:
    return (os.environ.get(ENV_ВЫКЛ) or "").strip() == "1"


def модуль(программа: str | None, *, только_свои: bool = True):
    """Модуль строителя этой программы или None.

    только_свои=True (по умолчанию) отдаёт модуль лишь для программ, которых в
    общем SPECS нет вовсе (сегодня это Whirlpool) ИЛИ чей строитель включён своим
    флагом. Так развилка не подменяет молча уже работающий денежный путь Code-1 у
    CLMM и DBC: пока их флаг выключен, они идут прежней дорогой.
    """
    if not программа or выключен():
        return None
    имя = МОДУЛИ.get(программа)
    if not имя:
        return None
    try:
        м = __import__(имя)
    except Exception:  # noqa: BLE001
        return None
    if не_свой(программа, м) and только_свои:
        return None
    return м


def не_свой(программа: str, м) -> bool:
    """Программа уже покрыта общим SPECS, а свой строитель выключен флагом."""
    if программа not in УЖЕ_В_SPECS:
        return False
    try:
        return not bool(м.включён())
    except Exception:  # noqa: BLE001
        return True


def specs() -> dict:
    """Записи для c2_swap_build.SPECS -- чтобы гейт «program not in SPECS» прошёл.

    Раскладка типа пула здесь НЕ живёт: у Whirlpool она зависит от вида
    инструкции (у swap подписант на месте 1, у swap_v2 -- на месте 3), и одной
    записью это не описать. Поэтому запись минимальная, а настоящую раскладку
    шаблон несёт в поле "спец" (см. spec_of в правке общего кода).
    """
    из_ = {}
    for программа, имя in МОДУЛИ.items():
        if программа in УЖЕ_В_SPECS:
            continue
        try:
            м = __import__(имя)
        except Exception:  # noqa: BLE001
            continue
        из_[программа] = {"label": метка(м), "ix": None, "alt": [],
                           "n_accounts": None, "min_accounts": минимум_счетов(м),
                           "user": [], "user_ata": [], "pda": [], "tail": True,
                           "base_mint": None, "quote_mint": None,
                           "base_vault": None, "quote_vault": None,
                           "строитель": имя}
    return из_


def метка(м) -> str:
    return getattr(м, "МЕТКА", None) or getattr(м, "__name__", "строитель")


def минимум_счетов(м) -> int:
    раскл = getattr(м, "РАСКЛАДКИ", None) or {}
    если_одна = getattr(м, "РАСКЛАДКА", None)
    числа = [int(р.get("счетов") or р.get("фикс") or 0) for р in раскл.values()] \
        if раскл else ([int((если_одна or {}).get("счетов") or 0)] if если_одна else [])
    числа = [ч for ч in числа if ч > 0]
    return min(числа) if числа else 0


# ------------------------------------------------- переходники под c2_swap_build

def _спец(м, tpl: dict) -> dict:
    """Раскладка ЭТОГО шаблона в виде, который понимает c2_swap_build.spec_of.

    Собирается из объявления модуля (наши_места_подробно), а не придумывается:
    место подписанта -> user, токеновые места -> user_ata с уже известными
    минтами и программами (шаблон их несёт после подготовить/шаблон_со_сторонами).
    """
    места = list(м.наши_места_подробно(tpl) or [])
    подписант = [о["место"] for о in места if о.get("минт") is None]
    return {"label": метка(м), "ix": tpl.get("вид"), "alt": [],
            "n_accounts": len(tpl.get("accounts") or []) or None,
            "min_accounts": None, "max_accounts": None,
            "user": подписант, "user_ata": "строитель", "pda": [], "tail": True,
            "base_mint": None, "quote_mint": None,
            "base_vault": None, "quote_vault": None, "строитель": метка(м)}


def extract_template(tx: dict, program: str, pool_vault: str, *,
                      продажа: bool = False) -> dict:
    """Как c2_swap_build.extract_template, но раскладку даёт модуль типа пула.

    pool_vault приёмка и полоса подают перебором по всем счетам инструкции;
    модулю нужен АДРЕС ПУЛА, и он же придёт в этом переборе -- на своём счёте
    шаблон соберётся, на прочих честно не соберётся.

    А ЕЩЁ pool_vault БЫВАЕТ ИМЕННО ХРАНИЛИЩЕМ -- так его подаёт полоса, у которой
    адреса пула нет вовсе (C.identify_pool отдаёт хранилище). Поэтому пробуем
    дважды: сперва как адрес пула, потом как хранилище. Второй заход и решает
    сделку роутера: у неё инструкций этой программы несколько, и по хранилищу
    выбирается ТА, где стоит наш пул, а не первая по счёту.
    """
    м = модуль(program)
    if м is None:
        return {"ok": False, "why_not": f"строителя для {str(program)[:8]} в реестре нет"}
    if продажа:
        return {"ok": False, "why_not": "продажа этим строителем не покрыта"}
    т = м.шаблон(tx, pool_vault)
    if not т.get("ok"):
        по_хранилищу = м.шаблон(tx, None, хранилище=pool_vault)
        if по_хранилищу.get("ok"):
            т = по_хранилищу
    if not т.get("ok"):
        return т
    ш = м.шаблон_со_сторонами(т, tx)
    if not ш.get("ok"):
        return {"ok": False, "why_not": f"стороны пула не восстановились: {ш.get('why_not')}"}
    tpl = dict(ш["шаблон"])
    tpl["спец"] = _спец(м, tpl)
    tpl["продажа"] = False
    tpl.setdefault("signers", [])
    return tpl


def _стороны_для_полосы(м, tpl: dict) -> dict:
    """Минты и программы в словах полосы: quote/base, а не A/B и не вход/выход.

    КОТИРОВКА -- ЭТО SOL, ЕСЛИ SOL ЕСТЬ В ПУЛЕ. Иначе котировкой считается та
    сторона, которой платил ИСТОЧНИК: так же решает и общий roles_damm2 (по
    движению хранилищ), и так же читает это место полоса.
    """
    места = list(м.наши_места_подробно(tpl) or [])
    токеновые = [о for о in места if о.get("минт")]
    if len(токеновые) != 2:
        return {}
    (а1, а2) = токеновые
    if а1["минт"] == WSOL:
        кот, баз = а1, а2
    elif а2["минт"] == WSOL:
        кот, баз = а2, а1
    else:
        # Не SOL-пул: котировка -- сторона входа источника. У модулей с
        # вход/выход это первая из двух, у Whirlpool -- по байту a_to_b.
        напр = tpl.get("a_to_b")
        if напр is None:
            кот, баз = а1, а2
        else:
            кот, баз = (а1, а2) if напр else (а2, а1)
    return {"кот": кот, "баз": баз}


def mints_and_vaults(tpl: dict, tx: dict) -> dict:
    """Как c2_swap_build.mints_and_vaults: минты, программы и хранилища сторон."""
    м = модуль((tpl or {}).get("program"))
    if м is None:
        return {}
    с = _стороны_для_полосы(м, tpl)
    if not с:
        return {}
    хр = м.хранилища_сторон(tpl, с["кот"]["минт"], с["баз"]["минт"])
    return {"quote_mint": с["кот"]["минт"], "base_mint": с["баз"]["минт"],
            "quote_program": с["кот"]["программа"], "base_program": с["баз"]["программа"],
            "quote_vault": хр.get("котировки"), "base_vault": хр.get("базы")}


def swap_instruction(tpl: dict, tx: dict, user: str, arg0: int, arg1: int,
                      keep_source_ix: bool = False):
    """Как c2_swap_build.swap_instruction. keep_source_ix=True -- пересборка
    сделки источника байт в байт; иначе наша покупка (arg0 -- вход, arg1 -- минимум)."""
    м = модуль((tpl or {}).get("program"))
    if м is None:
        raise ОшибкаРеестра("строителя для этой программы в реестре нет")
    if keep_source_ix:
        return м.инструкция_свопа(tpl, user=user, amount_in=arg0, min_out=arg1,
                                   как_у_источника=True)
    свой = dict(tpl)
    if "a_to_b" in свой and свой.get("a_to_b") is None:
        с = _стороны_для_полосы(м, tpl)
        if not с:
            raise ОшибкаРеестра("стороны пула не восстановились -- направление не задать")
        свой["a_to_b"] = (с["кот"]["минт"] == свой.get("минт_a"))
    return м.инструкция_свопа(свой, user=user, amount_in=arg0, min_out=arg1)


def build_buy(tpl: dict, tx: dict, *, user: str, payer: str, amount_in: int,
               min_out: int, cu_units: int = 200_000, cu_price_micro: int = 0,
               tip=None, wrap_sol: bool = True, nonce=None,
               tip_first: bool = False) -> dict:
    """Как c2_swap_build.build_buy -- та же подпись, тот же смысл ответа."""
    м = модуль((tpl or {}).get("program"))
    if м is None:
        raise ОшибкаРеестра("строителя для этой программы в реестре нет")
    свой = dict(tpl)
    if "a_to_b" in свой and свой.get("a_to_b") is None:
        с = _стороны_для_полосы(м, tpl)
        if not с:
            raise ОшибкаРеестра("стороны пула не восстановились -- направление не задать")
        свой["a_to_b"] = (с["кот"]["минт"] == свой.get("минт_a"))
    return м.build_buy(свой, tx, user=user, payer=payer, amount_in=amount_in,
                        min_out=min_out, cu_units=cu_units,
                        cu_price_micro=cu_price_micro, tip=tip, wrap_sol=wrap_sol,
                        nonce=nonce, tip_first=tip_first)


# ------------------------------------------------------------------ самопроверка

def self_test() -> int:
    пройдено = провалено = 0

    def chk(что, ок, факт=None):
        nonlocal пройдено, провалено
        print(f"  [{'ok  ' if ок else 'ПРОВАЛ'}] {что}"
              + (f" -> {факт!r}" if факт is not None and not ок else ""))
        пройдено += bool(ок)
        провалено += (not ок)

    WP = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
    CL = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
    DB = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"

    было_выкл = os.environ.pop(ENV_ВЫКЛ, None)
    chk("незнакомая программа -- модуля нет", модуль("X" * 32) is None)
    chk("пустая программа -- модуля нет", модуль(None) is None)
    chk("Whirlpool: модуль есть всегда (в общем SPECS его нет)",
        модуль(WP) is not None)
    # CLMM и DBC уже покрыты общим SPECS: пока их флаг выключен, реестр их не
    # перехватывает -- иначе он молча подменил бы работающий денежный путь.
    for п, флаг in ((CL, "BLOOM_CLMM_STROITEL"), (DB, "BLOOM_DBC_STROITEL")):
        было = os.environ.pop(флаг, None)
        chk(f"{п[:8]}: флаг выключен -- реестр не перехватывает", модуль(п) is None)
        os.environ[флаг] = "1"
        chk(f"{п[:8]}: флаг включён -- модуль отдаётся", модуль(п) is not None)
        os.environ.pop(флаг, None)
        if было is not None:
            os.environ[флаг] = было
    os.environ[ENV_ВЫКЛ] = "1"
    chk("общий рубильник выключает развилку целиком", модуль(WP) is None)
    os.environ.pop(ENV_ВЫКЛ, None)
    if было_выкл is not None:
        os.environ[ENV_ВЫКЛ] = было_выкл

    с = specs()
    chk("в specs() есть Whirlpool -- гейт приёмки «program not in SPECS» пройдёт",
        WP in с, sorted(с))
    chk("CLMM и DBC в specs() НЕ дублируются: их запись уже есть у общего пути",
        CL not in с and DB not in с, sorted(с))
    chk("запись specs() несёт метку и имя модуля",
        bool(с.get(WP, {}).get("label")) and с.get(WP, {}).get("строитель"), с.get(WP))
    chk("минимум счетов взят из раскладок модуля, а не выдуман",
        с.get(WP, {}).get("min_accounts") == 11, с.get(WP))

    chk("продажа этим путём не собирается",
        extract_template({}, WP, "A" * 32, продажа=True)["ok"] is False)
    chk("программы нет в реестре -- честная причина",
        "реестре нет" in (extract_template({}, "X" * 32, "A" * 32).get("why_not") or ""))
    chk("mints_and_vaults на чужой программе -- пусто, а не исключение",
        mints_and_vaults({"program": "X" * 32}, {}) == {})
    for имя, ф in (("swap_instruction", lambda: swap_instruction(
                        {"program": "X" * 32}, {}, "U", 1, 1)),
                    ("build_buy", lambda: build_buy(
                        {"program": "X" * 32}, {}, user="U", payer="U",
                        amount_in=1, min_out=1))):
        try:
            ф()
            ок = False
        except ОшибкаРеестра:
            ок = True
        except Exception:  # noqa: BLE001
            ок = False
        chk(f"{имя} на чужой программе -- ОшибкаРеестра, а не тихий None", ок)

    # Подписи переходников обязаны совпадать с одноимёнными в c2_swap_build:
    # иначе правка «делегировать первой строкой» не подойдёт.
    try:
        import inspect  # noqa: PLC0415

        import c2_swap_build as B  # noqa: PLC0415

        for имя in ("extract_template", "mints_and_vaults", "swap_instruction",
                     "build_buy"):
            наш = list(inspect.signature(globals()[имя]).parameters)
            общий = list(inspect.signature(getattr(B, имя)).parameters)
            chk(f"подпись {имя} совпадает с c2_swap_build", наш == общий,
                {"наш": наш, "общий": общий})
    except Exception as exc:  # noqa: BLE001
        print(f"  [проп ] сверка подписей: {type(exc).__name__}")

    print(f"самопроверка реестра строителей: {пройдено}/{пройдено + провалено} пройдено")
    return 0 if провалено == 0 else 1


if __name__ == "__main__":
    raise SystemExit(self_test() if "--self-test" in sys.argv else 0)
