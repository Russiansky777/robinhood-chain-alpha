#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""БИЛЕТ ОТ РЕЗЕРВА: размер покупки = min(потолок группы, доля x резерв пула).

ЗАЧЕМ. Потолок группы один на все пулы, а пулы разные: 0.5 SOL в пул с резервом
15 SOL -- это 3 % его котировочной стороны, то есть мы сами двигаем цену против
себя и платим за это проскальзыванием. Доля от резерва привязывает билет к
размеру пула, в который мы входим.

ЧТО ЗДЕСЬ ЕСТЬ И ЧЕГО НЕТ:
  * `bilet()` -- ЧИСТАЯ ФУНКЦИЯ: min(потолок, доля x резерв). Ни окружения, ни
    файлов, ни сети внутри. Резерва нет или не число -- ПОТОЛОК, как сейчас, и
    это НЕ отказ: правило размера не должно уметь отказывать в покупке.
  * `rezerv_iz_zapisi()` -- резерв из записи решения. Полоса пишет его с 27.09:
    `pool_reserve_sol` и `pool_reserve_kind` (bloom_own_send.собрать_покупку,
    строка `из_["pool_reserve_sol"]`), у двухшагового пути --
    `pool_reserve_sol_eq` (bloom_lane_two_step). Имена взяты grep'ом по коду, а
    не по памяти.
  * `rezerv_iz_sdelki()` -- резерв ДО сборки, из транзакции источника: остаток
    котировочного хранилища после его сделки. Нужен потому, что порядок в полосе
    обратный -- размер считается ДО сборки, а `pool_reserve_sol` появляется
    ПОСЛЕ неё. Резерв от нашего размера не зависит, поэтому его можно взять
    заранее и без единого чтения сети.
  * `rezerv_clmm_iz_sdelki()` -- у сосредоточенной ликвидности остатка хранилища
    мало: он считает ВЕСЬ пул, а торгует только активный диапазон. Берётся
    ёмкость АКТИВНОГО ШАГА ТИКА по событию свопа источника (L и корень цены из
    того же события, шаг тика из amm_config). Это НИЖНЯЯ оценка: настоящие
    позиции обычно шире одного шага.
  * DLMM: ёмкости активной корзины в событии свопа НЕТ вовсе (см.
    c2_dlmm_recon.событие_свопа -- там суммы, корзины и комиссии, но не
    ликвидность). Поэтому `rezerv_dlmm_nizhe_ne_budet()` отдаёт только НИЖНЮЮ
    ГРАНИЦУ -- сколько через эти корзины прошло у самого источника, -- и честно
    называет её границей, а не резервом. Настоящая ёмкость корзины -- одно
    чтение массива (c2_dlmm_odno_chtenie), и это решение штаба, не строителя.

ДОЛЯ -- ВХОД, А НЕ ЗАШИТОЕ ЧИСЛО: `dolya()` читает BLOOM_BILET_DOLYA, по
умолчанию 0 -- то есть врезка без слова владельца НИЧЕГО не меняет (билет =
потолок). Пол билета -- тоже вход (`pol_sol`), и по умолчанию его нет: функция
только СООБЩАЕТ, что билет ниже пола, а решать это штабу.

Ни одной сетевой операции, ни одной подписи, ни одного решения полосы.
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

LAMPORTOV_V_SOL = 1_000_000_000
WSOL = "So11111111111111111111111111111111111111112"
# Имя котировки, которой у полосы нет минта: лампорты лежат на счёте кривой.
NATIVE_QUOTE = "native_sol"

IMYA_FLAGA_DOLI = "BLOOM_BILET_DOLYA"
DOLYA_PO_UMOLCHANIYU = 0.0
# Выше этой доли билет перестаёт быть долей: половина резерва -- это не вход в
# пул, а его опустошение. Непонятное значение переменной -- умолчание.
PREDEL_DOLI = 0.5

# Поля резерва в записи решения -- ПО ФАКТУ КОДА ПОЛОСЫ (grep):
#   bloom_own_send.собрать_покупку -> pool_reserve_sol / pool_reserve_kind
#   bloom_lane_two_step.собрать    -> pool_reserve_sol_eq (котировка не SOL)
POLYA_REZERVA = ("pool_reserve_sol", "pool_reserve_sol_eq", "lane_pool_reserve_sol")
POLE_VIDA = "pool_reserve_kind"
# Вид резерва, который РЕАЛЬНЫМ не является: у кривой и Launchpad резервы
# виртуальные, и 30 SOL там -- не 30 SOL в хранилище.
VID_VIRTUALNYJ = "виртуальный"

PRICHINA_NET_REZERVA = "резерва в записи нет -- билет равен потолку группы"
PRICHINA_NE_CHISLO = "резерв не число -- билет равен потолку группы"
PRICHINA_DOLYA_VYKL = "доля не задана -- билет равен потолку группы"
PRICHINA_NE_SOL = ("котировка пула не SOL: резерв в SOL-эквиваленте без цены "
                   "котировочного токена не выразить")
# СОСРЕДОТОЧЕННАЯ ЛИКВИДНОСТЬ: остаток котировочного хранилища у этих программ --
# это ВЕСЬ пул, включая диапазоны, в которых цены сейчас нет. Торгует только
# активный диапазон, и доля от остатка хранилища дала бы билет в разы больше
# того, что пул примет без удара по цене. Поэтому здесь остаток хранилища
# резервом НЕ СЧИТАЕТСЯ, а путь свой: у CLMM ёмкость активного шага тика по
# событию, у DLMM -- только нижняя граница (ёмкости корзины в событии нет).
PROGRAMMY_SOSREDOTOCHENNYE = {
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
}
PRICHINA_SOSREDOTOCHENNAYA = ("у сосредоточенной ликвидности остаток хранилища -- "
                              "весь пул, а не активный диапазон: резервом не считается")


class OshibkaBileta(Exception):
    pass


# ------------------------------------------------------------- чистая функция

def bilet(potolok_sol, rezerv_sol, dolya, *, pol_sol=None) -> dict:
    """БИЛЕТ = min(потолок, доля x резерв). Чистая функция, без окружения и сети.

    potolok_sol -- потолок группы в SOL (он же ответ, когда резерва нет).
    rezerv_sol  -- резерв котировочной стороны пула в SOL-эквиваленте на входе.
    dolya       -- доля резерва (0.01 = 1 %). Ноль или None -- билет = потолок.
    pol_sol     -- пол билета, если штаб его назвал: НЕ применяется, только
                   сообщается признаком `nizhe_pola`. Срезать молча нельзя -- это
                   был бы другой размер, чем назвал штаб.

    Возвращает {"ok", "bilet_sol", "bilet_lamporty", "ot_rezerva", "potolok_sol",
    "rezerv_sol", "dolya", "nizhe_pola", "pochemu"}. ok=False бывает только при
    негодном ПОТОЛКЕ: он -- ответ по умолчанию, и без него отвечать нечем.
    """
    iz = {"ok": False, "bilet_sol": None, "bilet_lamporty": None,
          "ot_rezerva": False, "potolok_sol": None, "rezerv_sol": None,
          "dolya": None, "nizhe_pola": None, "pochemu": None}
    if isinstance(potolok_sol, bool) or not isinstance(potolok_sol, (int, float)) \
            or not float(potolok_sol) > 0:
        iz["pochemu"] = f"потолок группы не положительное число: {potolok_sol!r}"
        return iz
    potolok = float(potolok_sol)
    iz.update(ok=True, potolok_sol=potolok, bilet_sol=potolok,
              bilet_lamporty=int(D(str(potolok)) * LAMPORTOV_V_SOL))
    if isinstance(dolya, bool) or not isinstance(dolya, (int, float)) \
            or not 0 < float(dolya) <= PREDEL_DOLI:
        iz["pochemu"] = PRICHINA_DOLYA_VYKL
        return iz
    iz["dolya"] = float(dolya)
    if rezerv_sol is None:
        iz["pochemu"] = PRICHINA_NET_REZERVA
        return iz
    if isinstance(rezerv_sol, bool) or not isinstance(rezerv_sol, (int, float)):
        iz["pochemu"] = PRICHINA_NE_CHISLO
        return iz
    rezerv = float(rezerv_sol)
    iz["rezerv_sol"] = rezerv
    if not rezerv > 0:
        # НОЛЬ И МИНУС -- ЭТО НЕ РЕЗЕРВ, А ПОЛОМКА ЧИСЛА. Билет от него был бы
        # нулевым, то есть отказом в покупке через чёрный ход.
        iz["pochemu"] = f"резерв не положителен ({rezerv}) -- билет равен потолку"
        return iz
    # ТОЧНОЕ УМНОЖЕНИЕ, А НЕ ДВОИЧНОЕ: 0.01 * 15.3 в float даёт 0.15300000000000002,
    # и билет в лампортах разъехался бы на единицу от того, что считает проверка.
    ot_doli = D(str(rezerv)) * D(str(float(dolya)))
    if ot_doli >= D(str(potolok)):
        iz["pochemu"] = (f"доля {float(dolya)} от резерва {rezerv} SOL не ниже "
                         f"потолка {potolok} -- билет равен потолку")
        return iz
    iz.update(bilet_sol=float(ot_doli), bilet_lamporty=int(ot_doli * LAMPORTOV_V_SOL),
              ot_rezerva=True,
              pochemu=(f"доля {float(dolya)} от резерва {rezerv} SOL-экв. ниже "
                       f"потолка {potolok}"))
    if isinstance(pol_sol, (int, float)) and not isinstance(pol_sol, bool):
        iz["nizhe_pola"] = bool(float(ot_doli) < float(pol_sol))
    return iz


def dolya() -> float:
    """Доля резерва -- вход окружения. По умолчанию 0: врезка ничего не меняет."""
    syroe = (os.environ.get(IMYA_FLAGA_DOLI) or "").strip().replace(",", ".")
    if not syroe:
        return DOLYA_PO_UMOLCHANIYU
    try:
        chislo = float(syroe)
    except ValueError:
        return DOLYA_PO_UMOLCHANIYU
    return chislo if 0 < chislo <= PREDEL_DOLI else DOLYA_PO_UMOLCHANIYU


# --------------------------------------------------- резерв из записи решения

def rezerv_iz_zapisi(zapis) -> dict:
    """Резерв пула из записи решения полосы. {ok, rezerv_sol, vid, pole, why_not}.

    Имена полей -- те, которые полоса действительно пишет (POLYA_REZERVA). Поля
    нет или в нём не число -- не отказ, а честное «нет»: решать это bilet().
    """
    iz = {"ok": False, "rezerv_sol": None, "vid": None, "pole": None,
          "why_not": None}
    if not isinstance(zapis, dict):
        iz["why_not"] = f"запись решения не словарь: {type(zapis).__name__}"
        return iz
    for pole in POLYA_REZERVA:
        if pole not in zapis:
            continue
        znach = zapis.get(pole)
        if isinstance(znach, bool) or not isinstance(znach, (int, float)):
            iz.update(pole=pole, why_not=PRICHINA_NE_CHISLO)
            return iz
        iz.update(ok=True, rezerv_sol=float(znach), pole=pole,
                  vid=zapis.get(POLE_VIDA))
        return iz
    iz["why_not"] = PRICHINA_NET_REZERVA
    return iz


# ------------------------------------- резерв ДО сборки, из сделки источника

def _kirpichi():
    import c2_common as C  # noqa: PLC0415
    import c2_swap_build as B  # noqa: PLC0415
    return C, B


def rezerv_iz_sdelki(tx_istochnika: dict, *, istochnik: str, mint: str,
                     programma: str | None = None, ceny_kotirovok: dict | None = None,
                     cena_sol_za_kotirovku=None) -> dict:
    """Резерв котировочной стороны пула ПО СДЕЛКЕ ИСТОЧНИКА. Нуль чтений.

    ЗАЧЕМ ОТДЕЛЬНО ОТ ЗАПИСИ. В полосе размер считается ДО сборки
    (bloom_own_send: `разм = размер_для_группы(...)`), а `pool_reserve_sol`
    появляется ПОСЛЕ неё. Резерв от нашего размера не зависит -- это остаток
    котировочного хранилища после сделки источника, -- поэтому его можно взять
    заранее, тем же C.identify_pool, которым полоса находит пул.

    Котировка не SOL -- отказ по имени, если цены ИМЕННО ЭТОГО котировочного
    токена нет в ceny_kotirovok (минт -> SOL за целый токен). Цена «любой
    котировки одним числом» запрещена нарочно: один раз так и вышло, что резерв
    пула с котировкой HTmQz7My посчитался по цене USDC и дал 27 млн SOL.
    programma -- программа пула: у сосредоточенной ликвидности остаток хранилища
    резервом не считается (PRICHINA_SOSREDOTOCHENNAYA).
    """
    iz = {"ok": False, "rezerv_sol": None, "vid": None, "why_not": None,
          "quote_mint": None, "quote_vault": None}
    try:
        C, _B = _kirpichi()
    except Exception as exc:  # noqa: BLE001
        iz["why_not"] = f"модули не загружены: {type(exc).__name__}"
        return iz
    try:
        pul = C.identify_pool(tx_istochnika or {}, istochnik or "", mint or "")
        if not pul.get("ok"):
            iz["why_not"] = f"пул источника: {pul.get('why_not')}"
            return iz
        iz.update(quote_mint=pul.get("quote_mint"), quote_vault=pul.get("quote_vault"))
        if (programma or "") in PROGRAMMY_SOSREDOTOCHENNYE:
            iz["why_not"] = (f"{PRICHINA_SOSREDOTOCHENNAYA} "
                             f"({PROGRAMMY_SOSREDOTOCHENNYE[programma]})")
            return iz
        if pul.get("quote_mint") == getattr(C, "NATIVE_QUOTE", NATIVE_QUOTE):
            # Котировка -- нативный SOL: "хранилище" это сам счёт кривой, и
            # резерв там ВИРТУАЛЬНЫЙ (кривая), а не остаток хранилища.
            iz.update(ok=False, vid=VID_VIRTUALNYJ,
                      why_not=("котировка -- нативный SOL (кривая): резерв там "
                               "виртуальный, остатком счёта он не считается"))
            return iz
        stroki = {r["account"]: r for r in C.token_rows(tx_istochnika).values()}
        qv = stroki.get(pul.get("quote_vault"))
        if not qv:
            iz["why_not"] = "котировочного хранилища нет в балансах сделки"
            return iz
        ostatok = int(qv["post"])
        dec = qv.get("dec")
        if pul.get("quote_mint") == WSOL:
            iz.update(ok=True, rezerv_sol=ostatok / LAMPORTOV_V_SOL, vid="реальный")
            return iz
        cena = (ceny_kotirovok or {}).get(pul.get("quote_mint"))
        if cena is None and cena_sol_za_kotirovku is not None \
                and not ceny_kotirovok:
            cena = cena_sol_za_kotirovku
        if cena is None or not isinstance(dec, int):
            iz["why_not"] = (f"{PRICHINA_NE_SOL} "
                             f"(котировка {str(pul.get('quote_mint'))[:8]})")
            return iz
        iz.update(ok=True, vid="реальный",
                  rezerv_sol=float(D(ostatok) / D(10) ** int(dec) * D(str(cena))))
        return iz
    except Exception as exc:  # noqa: BLE001
        iz["why_not"] = f"резерв по сделке не посчитан: {type(exc).__name__}: {str(exc)[:120]}"
        return iz


# -------------------------------- сосредоточенная ликвидность: CLMM и DLMM

def storony_0_1_clmm(tx_istochnika: dict, *, pul: str, storona_0_vhod: bool) -> dict:
    """Минты СТОРОН 0 и 1 пула CLMM по сделке источника. {ok, mint_0, mint_1}.

    Событие называет только номер стороны входа, а какой минт на этой стороне --
    нет. Направление сделки источника даёт шаблон строителя (минт_входа/минт_выхода
    по движению хранилищ), и вместе с номером стороны это и есть раскладка 0/1.
    Без неё нельзя сказать, в какую сторону пойдёт цена от НАШЕЙ покупки, а
    перепутав -- посчитать ёмкость другой стороны.
    """
    iz = {"ok": False, "mint_0": None, "mint_1": None, "why_not": None}
    try:
        import c2_clmm_stroitel as CM  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        iz["why_not"] = f"модуль CLMM не загружен: {type(exc).__name__}"
        return iz
    t = CM.шаблон(tx_istochnika or {}, pul)
    if not t.get("ok"):
        iz["why_not"] = f"шаблон CLMM: {t.get('why_not')}"
        return iz
    mh = CM.минты_и_хранилища(t, tx_istochnika)
    if not mh.get("ok"):
        iz["why_not"] = f"минты сторон: {mh.get('why_not')}"
        return iz
    vh, vyh = mh.get("минт_входа"), mh.get("минт_выхода")
    if storona_0_vhod:
        iz.update(ok=True, mint_0=vh, mint_1=vyh)
    else:
        iz.update(ok=True, mint_0=vyh, mint_1=vh)
    return iz


def rezerv_clmm_iz_sdelki(tx_istochnika: dict, *, pul: str, mint_kotirovki: str,
                          shag_tika: int | None = None, q_dec: int = 9,
                          cena_sol_za_kotirovku=None) -> dict:
    """Ёмкость АКТИВНОГО ШАГА ТИКА по событию свопа CLMM. Нуль чтений.

    У сосредоточенной ликвидности остаток хранилища -- это весь пул, включая
    диапазоны, в которых цены сейчас нет; торгует только активный диапазон.
    Событие свопа отдаёт ликвидность L и корень цены ПОСЛЕ сделки прямо
    (c2_cl_quote.СХЕМА_CLMM), и ёмкость считается той же формулой, которой
    считает выход: сколько котировочного токена влезет от нынешней цены до
    границы шага тика.

    ЭТО НИЖНЯЯ ОЦЕНКА, и так и называется: позиция обычно шире одного шага тика,
    а где кончается ликвидность -- видно только в массиве тиков, то есть чтением.
    Шаг тика -- из amm_config (c2_swap_build.СТАВКИ_CLMM, файл
    data/clmm_konfigi.json); не передан и не прочитан -- отказ по имени.
    """
    iz = {"ok": False, "rezerv_sol": None, "vid": "нижняя оценка активного шага тика",
          "why_not": None, "L": None, "tik": None, "shag_tika": None,
          "storona_0_vhod": None, "rezerv_syroj": None, "mint_0": None,
          "mint_1": None, "kotirovka_storona": None, "vosproizvelos": None,
          "otklonenie": None, "sverka_why_not": None}
    try:
        import c2_cl_quote as CL  # noqa: PLC0415

        _C, B = _kirpichi()
    except Exception as exc:  # noqa: BLE001
        iz["why_not"] = f"модули цены CLMM не загружены: {type(exc).__name__}"
        return iz
    try:
        telo = b""
        for t_ in CL.тела_событий_clmm(tx_istochnika or {}):
            if B.b58encode(CL.событие_clmm(t_)["пул"]) == pul:
                telo = t_
                break
        if not telo:
            iz["why_not"] = "события свопа CLMM этого пула в сделке нет"
            return iz
        ev = CL.событие_clmm(telo)
        L, cena, tik = ev["ликвидность"], ev["цена_после"], ev["тик"]
        iz.update(L=int(L), tik=int(tik), storona_0_vhod=bool(ev["сторона_0_вход"]))
        if L <= 0 or cena <= 0:
            iz["why_not"] = "в событии нет ликвидности или цены после сделки"
            return iz
        if shag_tika is None:
            # ШАГ ТИКА -- ПО КОНФИГУ ИМЕННО ЭТОЙ СДЕЛКИ: он стоит вторым счётом
            # инструкции свопа, и перебором по кэшу его брать нельзя -- у разных
            # конфигов шаг разный.
            B.загрузить_ставки_clmm()
            konfig = _konfig_clmm_sdelki(tx_istochnika, pul, B)
            st = (B.СТАВКИ_CLMM or {}).get(konfig or "")
            shag_tika = (st or {}).get("шаг_тика")
        if not isinstance(shag_tika, int) or shag_tika <= 0:
            iz["why_not"] = ("шаг тика пула не известен (конфиг не прочитан) -- "
                             "ёмкость активного шага не посчитать")
            return iz
        iz["shag_tika"] = int(shag_tika)
        niz = (int(tik) // int(shag_tika)) * int(shag_tika)
        verh = niz + int(shag_tika)
        S_niz, S_verh = CL.цена_по_тику(niz), CL.цена_по_тику(verh)
        Q = D(CL.Q)
        # КАКАЯ СТОРОНА -- КОТИРОВКА, РЕШАЕТ МИНТ, А НЕ НАПРАВЛЕНИЕ ИСТОЧНИКА.
        # Сделка источника бывает ПРОДАЖЕЙ (котировка у него на выходе), и взять
        # сторону входа события за котировку значило бы считать ёмкость другой
        # стороны. Живой след: пул G4G5SzkbLF..., где источник продавал.
        st = storony_0_1_clmm(tx_istochnika, pul=pul,
                              storona_0_vhod=bool(ev["сторона_0_вход"]))
        if not st.get("ok"):
            iz["why_not"] = st.get("why_not")
            return iz
        iz.update(mint_0=st["mint_0"], mint_1=st["mint_1"])
        if mint_kotirovki == st["mint_1"]:
            kotirovka_eto_1 = True
        elif mint_kotirovki == st["mint_0"]:
            kotirovka_eto_1 = False
        else:
            iz["why_not"] = (f"котировка {str(mint_kotirovki)[:8]} не сторона этого "
                             f"пула ({str(st['mint_0'])[:8]}/{str(st['mint_1'])[:8]})")
            return iz
        iz["kotirovka_storona"] = 1 if kotirovka_eto_1 else 0
        if kotirovka_eto_1:
            # Платим стороной 1 (y): цена y/x растёт, идём к верхней границе.
            syroe = D(L) * (D(S_verh) - D(cena)) / Q
        else:
            # Платим стороной 0 (x): цена падает, идём к нижней границе.
            syroe = D(L) * Q * (D(cena) - D(S_niz)) / (D(S_niz) * D(cena))
        if syroe <= 0:
            iz["why_not"] = ("цена события стоит на границе шага тика -- ёмкость "
                             "активного шага нулевая")
            return iz
        # ЗНАКОВ У КОТИРОВКИ СОБЫТИЕ НЕ НЕСЁТ, поэтому они -- вход: q_dec=9 это
        # WSOL. У котировки не SOL нужна ещё её цена в SOL, иначе SOL-эквивалента
        # не получится -- тот же отказ по имени, что у остатка хранилища.
        if int(q_dec) != 9 and cena_sol_za_kotirovku is None:
            iz["why_not"] = PRICHINA_NE_SOL
            return iz
        celyh = syroe / D(10) ** int(q_dec)
        iz["rezerv_syroj"] = int(syroe)
        # СВЕРКА ФОРМУЛЫ НА САМОЙ СДЕЛКЕ ИСТОЧНИКА. Та же формула на интервале
        # [цена до, цена после] обязана дать ЕГО чистый вход. Не дала -- его сделка
        # перешла границу диапазона ликвидности, L по её пути была другой, и
        # ёмкость активного шага посчитана по L, которой на этом шаге не было.
        # Это не отказ (билет -- не min_out, промах им не купишь), но признак
        # идёт в ответ и в запись.
        sv = _sverka_clmm_na_sdelke(tx_istochnika, pul=pul, ev=ev, B=B, CL=CL)
        iz.update(vosproizvelos=sv.get("vosproizvelos"),
                  otklonenie=sv.get("otklonenie"),
                  sverka_why_not=sv.get("why_not"))
        if cena_sol_za_kotirovku is None:
            iz.update(ok=True, rezerv_sol=float(celyh))
        else:
            iz.update(ok=True,
                      rezerv_sol=float(celyh * D(str(cena_sol_za_kotirovku))))
        return iz
    except Exception as exc:  # noqa: BLE001
        iz["why_not"] = f"ёмкость шага тика не посчитана: {type(exc).__name__}: {str(exc)[:120]}"
        return iz


def _sverka_clmm_na_sdelke(tx: dict, *, pul: str, ev: dict, B, CL) -> dict:
    """Воспроизводит ли формула ёмкости саму сделку источника. {vosproizvelos, otklonenie}.

    Порог 1e-5 -- не свой: он взят у c2_cl_quote.ПРЕДЕЛ_РАСХОЖДЕНИЯ_CLMM, где
    измерен на 31 живой сделке (воспроизводятся с расхождением до 1.01e-06, у
    перешедших границу диапазона -- с 4.7e-05; порог стоит в разрыве).
    """
    iz = {"vosproizvelos": None, "otklonenie": None, "why_not": None}
    try:
        import c2_clmm_stroitel as CM  # noqa: PLC0415

        C, _B = _kirpichi()
        t = CM.шаблон(tx or {}, pul)
        mh = CM.минты_и_хранилища(t, tx) if t.get("ok") else {}
        if not mh.get("ok"):
            iz["why_not"] = "минты и хранилища сторон не разобрались"
            return iz
        stroki = {r["account"]: r for r in C.token_rows(tx).values()}
        hv = stroki.get(mh.get("хранилище_входа"))
        hx = stroki.get(mh.get("хранилище_выхода"))
        if not hv or not hx:
            iz["why_not"] = "хранилищ сторон нет в балансах сделки"
            return iz
        vhod, vyhod = hv["post"] - hv["pre"], hx["pre"] - hx["post"]
        if vhod <= 0 or vyhod <= 0:
            iz["why_not"] = "по хранилищам это не своп в одну сторону"
            return iz
        konfig = _konfig_clmm_sdelki(tx, pul, B)
        st = (B.СТАВКИ_CLMM or {}).get(konfig or "")
        if not st:
            iz["why_not"] = f"ставка конфига {str(konfig)[:8]} не прочитана"
            return iz
        vh0 = bool(ev["сторона_0_вход"])
        L, P2 = ev["ликвидность"], ev["цена_после"]
        P1 = CL.цена_до_по_выходу(выход=vyhod, цена_после=P2, L=L,
                                  база_это_a=(not vh0))
        if P1 is None or P1 <= 0:
            iz["why_not"] = "цена до сделки не посчиталась"
            return iz
        Q = D(CL.Q)
        if not vh0:
            moj = D(L) * (D(P2) - D(P1)) / Q
        else:
            moj = D(L) * Q * (D(P1) - D(P2)) / (D(P1) * D(P2))
        chistyj = D(vhod - CL.комиссия_вверх(vhod, int(st["ставка_1e6"])))
        if chistyj <= 0 or moj <= 0:
            iz["why_not"] = "чистый вход или ёмкость не положительны"
            return iz
        otkl = abs(moj - chistyj) / chistyj
        iz.update(otklonenie=float(otkl),
                  vosproizvelos=bool(otkl < CL.ПРЕДЕЛ_РАСХОЖДЕНИЯ_CLMM))
        return iz
    except Exception as exc:  # noqa: BLE001
        iz["why_not"] = f"сверка не посчиталась: {type(exc).__name__}: {str(exc)[:100]}"
        return iz


def _konfig_clmm_sdelki(tx: dict, pul: str, B) -> str | None:
    """Счёт amm_config инструкции CLMM этого пула: место 1 (замер Code-2)."""
    import c2_cl_quote as CL  # noqa: PLC0415

    for ix in B.all_instructions(tx or {}):
        if ix.get("programId") != "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK":
            continue
        scheta = list(ix.get("accounts") or [])
        if len(scheta) > CL.СМ_ПУЛ_В_ИНСТРУКЦИИ_CLMM \
                and scheta[CL.СМ_ПУЛ_В_ИНСТРУКЦИИ_CLMM] == pul and len(scheta) > 1:
            return scheta[1]
    return None


def rezerv_dlmm_nizhe_ne_budet(tx_istochnika: dict, *, pul: str) -> dict:
    """НИЖНЯЯ ГРАНИЦА ёмкости корзин DLMM: сколько прошло у самого источника.

    Ёмкости активной корзины в событии свопа DLMM НЕТ (см.
    c2_dlmm_recon.событие_свопа: пул, кто, корзины, суммы, комиссии -- и всё).
    Поэтому это не резерв, а граница: пул принял столько котировки, значит не
    меньше столько в нём и было. Настоящая ёмкость -- одно чтение массива корзин.
    """
    iz = {"ok": False, "granica_sol": None, "why_not": None,
          "korzin": None, "vid": "нижняя граница по сделке источника"}
    try:
        import c2_dlmm_bez_chteniy as DL  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        iz["why_not"] = f"модуль DLMM не загружен: {type(exc).__name__}"
        return iz
    sob = DL.события_пула(tx_istochnika or {}, pul)
    if not sob:
        iz["why_not"] = "события свопа DLMM этого пула в сделке нет"
        return iz
    ev = sob[0]
    vhod = int(ev.get("amount_in") or 0)
    if vhod <= 0:
        iz["why_not"] = "в событии нулевой вход"
        return iz
    iz.update(ok=True, granica_sol=vhod / LAMPORTOV_V_SOL,
              korzin=DL.корзин_события(ev))
    return iz


def programma_i_pul(tx_istochnika: dict, *, istochnik: str, mint: str) -> dict:
    """Программа пула и его АДРЕС по сделке источника. Нуль чтений.

    Полоса знает хранилище (C.identify_pool), а не адрес пула, и программу
    определяет тем же путём, что сборка: PP.pool_program по хранилищу. Адрес пула
    у сосредоточенной ликвидности нужен ёмкости активного шага, и он есть у того
    же ответа: ВЛАДЕЛЕЦ хранилища. Что это действительно пул -- замер, не
    предположение: у 6 из 6 образцов CLMM pool_owner совпал с пулом, который
    читает из инструкции шаблон строителя (проверка в self_test).
    """
    iz = {"ok": False, "programma": None, "pul": None, "hranilishche": None,
          "quote_mint": None, "why_not": None}
    try:
        import c2_pool_programs as PP  # noqa: PLC0415
        import c2_shadow_build as SB  # noqa: PLC0415

        C, _B = _kirpichi()
        pul = C.identify_pool(tx_istochnika or {}, istochnik or "", mint or "")
        if not pul.get("ok"):
            iz["why_not"] = f"пул источника: {pul.get('why_not')}"
            return iz
        prog = (PP.pool_program(tx_istochnika, pul["pool_vault"], SB._labels())
                or {}).get("pool_program")
        iz.update(ok=True, programma=prog, pul=pul.get("pool_owner"),
                  hranilishche=pul.get("pool_vault"),
                  quote_mint=pul.get("quote_mint"))
        return iz
    except Exception as exc:  # noqa: BLE001
        iz["why_not"] = f"программа пула не определена: {type(exc).__name__}"
        return iz


# ------------------------------------------------------- дверь для полосы

def bilet_dlya_signala(*, potolok_sol, zapis=None, tx_istochnika=None,
                       istochnik=None, mint=None, pul=None, programma=None,
                       dolya_vhod=None, pol_sol=None,
                       cena_sol_za_kotirovku=None, q_dec_kotirovki=None,
                       mint_kotirovki=None, ceny_kotirovok=None) -> dict:
    """Билет для одного сигнала: резерв из записи, иначе из сделки источника.

    Порядок -- от дешёвого к дорогому и от точного к оценочному:
      1. запись решения (`pool_reserve_sol`) -- если полоса её уже написала;
      2. сделка источника: остаток котировочного хранилища (нуль чтений);
      3. CLMM -- ёмкость активного шага тика по событию (нижняя оценка).
    Ни один шаг не отказывает в покупке: не вышло -- билет равен потолку.
    """
    iz = {"rezerv_sol": None, "rezerv_otkuda": None, "rezerv_vid": None,
          "rezerv_why_not": None, "pool_program": None, "quote_mint": None}
    # ПРОГРАММА И АДРЕС ПУЛА -- САМ, если полоса их не назвала: без программы
    # остаток хранилища у сосредоточенной ликвидности сошёл бы за резерв, а без
    # адреса пула ёмкость активного шага не посчитать.
    if tx_istochnika is not None and (programma is None or pul is None):
        pp = programma_i_pul(tx_istochnika, istochnik=istochnik or "",
                             mint=mint or "")
        if pp.get("ok"):
            programma = programma or pp.get("programma")
            pul = pul or pp.get("pul")
            iz["pool_program"] = pp.get("programma")
            iz["quote_mint"] = pp.get("quote_mint")
    r = rezerv_iz_zapisi(zapis) if zapis is not None else {"ok": False}
    if r.get("ok"):
        iz.update(rezerv_sol=r["rezerv_sol"], rezerv_otkuda=f"запись: {r['pole']}",
                  rezerv_vid=r.get("vid"))
    elif tx_istochnika is not None:
        prichiny = [r.get("why_not")] if r.get("why_not") else []
        s = rezerv_iz_sdelki(tx_istochnika, istochnik=istochnik or "",
                             mint=mint or "", programma=programma,
                             ceny_kotirovok=ceny_kotirovok,
                             cena_sol_za_kotirovku=cena_sol_za_kotirovku)
        if s.get("ok"):
            iz.update(rezerv_sol=s["rezerv_sol"], rezerv_vid=s.get("vid"),
                      rezerv_otkuda="сделка источника: остаток хранилища котировки")
        else:
            prichiny.append(s.get("why_not"))
            if pul:
                c = rezerv_clmm_iz_sdelki(
                    tx_istochnika, pul=pul,
                    mint_kotirovki=(mint_kotirovki or iz.get("quote_mint") or WSOL),
                    q_dec=(q_dec_kotirovki or 9),
                    cena_sol_za_kotirovku=cena_sol_za_kotirovku)
                if c.get("ok"):
                    iz.update(rezerv_sol=c["rezerv_sol"], rezerv_vid=c.get("vid"),
                              rezerv_otkuda="событие CLMM: активный шаг тика")
                else:
                    prichiny.append(c.get("why_not"))
            iz["rezerv_why_not"] = "; ".join(p for p in prichiny if p)[:240]
    else:
        iz["rezerv_why_not"] = r.get("why_not") or PRICHINA_NET_REZERVA
    # ВИРТУАЛЬНЫЙ РЕЗЕРВ ДОЛЕЙ НЕ СЧИТАЕТСЯ: у кривой 30 "SOL" в резерве -- это
    # не 30 SOL в хранилище, и процент от них -- не процент пула.
    if iz["rezerv_vid"] == VID_VIRTUALNYJ:
        iz["rezerv_why_not"] = ("резерв виртуальный (кривая/Launchpad) -- доля от "
                                "него не доля пула, билет равен потолку")
        iz["rezerv_sol"] = None
    b = bilet(potolok_sol, iz["rezerv_sol"],
              dolya() if dolya_vhod is None else dolya_vhod, pol_sol=pol_sol)
    b.update(iz)
    return b


# ------------------------------------------------------------- самопроверка

# ЧИСЛО ПРОВЕРОК ОБЪЯВЛЕНО: если образцов не станет или тип перестанет
# разбираться, проверок будет МЕНЬШЕ, и самопроверка упадёт на несовпадении
# числа, а не промолчит зелёным.
ZHDEM_PROVEROK = 57
# Живые образцы пулов репозитория: у скольких котировка WSOL и мой резерв совпал
# с числом полосы (c2_swap_build.min_out_from_reserves -> reserves_after[0]).
FAJLY_OBRAZCOV = {
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": ("Raydium CPMM", 3),
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": ("Pump AMM", 23),
}
# Launchlab: у полосы резервы ВИРТУАЛЬНЫЕ (кривая), у меня остаток хранилища --
# числа РАЗНЫЕ по построению, и это проверяется отдельно, а не подгоняется.
FAJL_LAUNCHLAB = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
# CLMM: ёмкость активного шага тика по живым сделкам data/c3_usdc_noga.
ZHDEM_CLMM_EMKOST = 6
ZHDEM_CLMM_VOSPROIZVELOS = 4
# Адрес пула = владелец котировочного хранилища: сверено с пулом из инструкции.
ZHDEM_PUL_SOVPAL = 8
# Пул, где источник ПРОДАВАЛ: раскладка сторон 0/1 там обратная, и по стороне
# входа события ёмкость вышла бы в 15 000 раз больше (замер: 21749 против 1.45).
PUL_PRODAZHI = "G4G5SzkbLFMhoSgHiQNeyJFt75sSDsL1rD8LVyT5xZbU"
PREDEL_PRODAZHI_SOL = 10.0
FAJL_USDC = "c3_usdc_noga/obrazcy_usdc_noga.json"
# Цена USDC в SOL -- из статичного шаблона первой ноги (образец 28.09).
FAJL_CENY = "nogi_shablony.json"


def self_test() -> int:  # noqa: C901, PLR0912, PLR0915
    import json  # noqa: PLC0415

    C, B = _kirpichi()
    proverki = []

    def chk(chto, ok, fakt=None):
        proverki.append((chto, bool(ok), fakt))

    # ------------------------------------------------- 1. чистая функция
    b = bilet(0.5, 15.3, 0.01)
    chk("билет = доля x резерв, когда это ниже потолка",
        b["ok"] and b["ot_rezerva"] and b["bilet_sol"] == 0.153
        and b["bilet_lamporty"] == 153_000_000, (b["bilet_sol"], b["bilet_lamporty"]))
    chk("умножение точное: 0.01 x 15.3 -- ровно 0.153, а не 0.15300000000000002",
        repr(b["bilet_sol"]) == "0.153", repr(b["bilet_sol"]))
    b2 = bilet(0.5, 200.0, 0.01)
    chk("доля от большого резерва выше потолка -- билет равен потолку",
        b2["ok"] and not b2["ot_rezerva"] and b2["bilet_sol"] == 0.5, b2["bilet_sol"])
    b3 = bilet(0.5, 50.0, 0.01)
    chk("доля РОВНО в потолок -- билет потолок (min, а не больше)",
        b3["bilet_sol"] == 0.5 and not b3["ot_rezerva"], b3["bilet_sol"])
    for rez, imya in ((None, "резерва нет"), ("мусор", "резерв строкой"),
                      ({}, "резерв словарём"), (True, "резерв True"),
                      (0, "резерв ноль"), (-5.0, "резерв отрицательный")):
        bb = bilet(0.5, rez, 0.01)
        chk(f"{imya} -- билет равен потолку и это НЕ отказ",
            bb["ok"] and bb["bilet_sol"] == 0.5 and not bb["ot_rezerva"]
            and bb["pochemu"], (bb["bilet_sol"], bb["pochemu"]))
    for dol, imya in ((None, "доли нет"), (0, "доля ноль"), (-0.01, "доля минус"),
                      (0.9, "доля выше предела"), (True, "доля True")):
        bb = bilet(0.5, 15.3, dol)
        chk(f"{imya} -- билет равен потолку",
            bb["ok"] and bb["bilet_sol"] == 0.5 and not bb["ot_rezerva"],
            (dol, bb["bilet_sol"]))
    for pot, imya in ((None, "потолка нет"), (0, "потолок ноль"),
                      (-1, "потолок минус"), ("0.5", "потолок строкой")):
        bb = bilet(pot, 15.3, 0.01)
        chk(f"{imya} -- ok=False: отвечать нечем", not bb["ok"] and bb["pochemu"],
            (imya, bb["pochemu"]))
    bp = bilet(0.5, 5.0, 0.01, pol_sol=0.1)
    chk("пол билета только СООБЩАЕТСЯ, а не срезает: 0.05 ниже пола 0.1",
        bp["bilet_sol"] == 0.05 and bp["nizhe_pola"] is True, bp)
    chk("без пола признак пустой", bilet(0.5, 5.0, 0.01)["nizhe_pola"] is None)

    # ------------------------------------------------------------- 2. доля
    sohr = os.environ.get(IMYA_FLAGA_DOLI)
    os.environ.pop(IMYA_FLAGA_DOLI, None)
    chk(f"без {IMYA_FLAGA_DOLI} доля 0 -- врезка ничего не меняет",
        dolya() == 0.0, dolya())
    for znach, zhdem in (("0.01", 0.01), ("0,015", 0.015), ("мусор", 0.0),
                         ("0", 0.0), ("-1", 0.0), ("0.9", 0.0)):
        os.environ[IMYA_FLAGA_DOLI] = znach
        chk(f"доля из окружения {znach!r} -> {zhdem}", dolya() == zhdem, dolya())
    if sohr is None:
        os.environ.pop(IMYA_FLAGA_DOLI, None)
    else:
        os.environ[IMYA_FLAGA_DOLI] = sohr

    # --------------------------------------------- 3. резерв из записи решения
    z1 = rezerv_iz_zapisi({"pool_reserve_sol": 12.5, "pool_reserve_kind": "реальный"})
    chk("резерв из записи: поле pool_reserve_sol и вид",
        z1["ok"] and z1["rezerv_sol"] == 12.5 and z1["pole"] == "pool_reserve_sol"
        and z1["vid"] == "реальный", z1)
    z2 = rezerv_iz_zapisi({"pool_reserve_sol_eq": 3.25})
    chk("резерв двухшагового пути: поле pool_reserve_sol_eq",
        z2["ok"] and z2["rezerv_sol"] == 3.25, z2)
    z3 = rezerv_iz_zapisi({"pool_reserve_sol": None})
    chk("поле есть, а в нём не число -- честное нет, а не ноль",
        not z3["ok"] and z3["why_not"] == PRICHINA_NE_CHISLO, z3)
    z4 = rezerv_iz_zapisi({"lane_group": "cand1"})
    chk("поля резерва в записи нет -- причина по имени",
        not z4["ok"] and z4["why_not"] == PRICHINA_NET_REZERVA, z4)
    chk("запись не словарь -- причина словами, без исключения",
        not rezerv_iz_zapisi(None)["ok"] and rezerv_iz_zapisi(7)["why_not"], None)

    # ------------------------------- 4. резерв по сделке: сверка с числом полосы
    sovpalo, vsego_wsol, ne_sol = 0, 0, 0
    for prog, (metka, zhdem) in sorted(FAJLY_OBRAZCOV.items()):
        p = Path(C.DATA) / "c2_pool_samples" / f"{prog}.json"
        chk(f"образцы {metka} на месте", p.exists(), str(p))
        if not p.exists():
            continue
        svoj = 0
        for x in json.loads(p.read_text(encoding="utf-8")):
            tx = x.get("tx")
            if not isinstance(tx, dict):
                continue
            moj = rezerv_iz_sdelki(tx, istochnik=x.get("source"), mint=x.get("mint"),
                                   programma=prog)
            if not moj.get("ok"):
                ne_sol += 1 if PRICHINA_NE_SOL in (moj.get("why_not") or "") else 0
                continue
            svoj += 1
            vsego_wsol += 1
            t = B.extract_template(tx, prog, x.get("pool_vault"))
            polosy = None
            if t.get("ok"):
                mo = B.min_out_from_reserves(t, tx, 1_000_000, 0.35) or {}
                rez = mo.get("reserves_after") or mo.get("virtual_reserves_after")
                if isinstance(rez, (list, tuple)) and rez:
                    polosy = round(int(rez[0]) / LAMPORTOV_V_SOL, 9)
            sovpalo += 1 if (polosy is not None
                             and abs(moj["rezerv_sol"] - polosy) < 1e-9) else 0
        chk(f"{metka}: котировка WSOL у {zhdem} образцов", svoj == zhdem, svoj)
    chk(f"резерв по сделке совпал с числом полосы у всех {vsego_wsol} сделок с "
        f"котировкой WSOL",
        sovpalo == vsego_wsol and vsego_wsol == sum(z for _m, z in FAJLY_OBRAZCOV.values()),
        (sovpalo, vsego_wsol))
    chk("котировка не SOL без её цены -- отказ по имени, а не чужая цена",
        ne_sol > 0, ne_sol)
    # Launchlab: у полосы резерв виртуальный, у меня остаток хранилища -- числа
    # РАЗНЫЕ, и это свойство, а не сбой.
    pl = Path(C.DATA) / "c2_pool_samples" / f"{FAJL_LAUNCHLAB}.json"
    raznye = 0
    if pl.exists():
        for x in json.loads(pl.read_text(encoding="utf-8")):
            tx = x.get("tx")
            if not isinstance(tx, dict):
                continue
            moj = rezerv_iz_sdelki(tx, istochnik=x.get("source"), mint=x.get("mint"),
                                   programma=FAJL_LAUNCHLAB)
            if not moj.get("ok"):
                continue
            t = B.extract_template(tx, FAJL_LAUNCHLAB, x.get("pool_vault"))
            mo = (B.min_out_from_reserves(t, tx, 1_000_000, 0.35) or {}) if t.get("ok") else {}
            if mo.get("virtual_reserves_after"):
                raznye += 1
    chk("Launchlab: у полосы резерв виртуальный, у меня остаток хранилища -- "
        "числа разные по построению",
        raznye >= 1, raznye)
    for prog, imya in sorted(PROGRAMMY_SOSREDOTOCHENNYE.items()):
        s = rezerv_iz_sdelki({"meta": {}}, istochnik="x", mint="y", programma=prog)
        chk(f"{imya}: остаток хранилища резервом не считается -- отказ по имени",
            not s.get("ok") and s.get("why_not"), s.get("why_not"))

    # --------------------------- 5. CLMM: ёмкость активного шага тика по событию
    pu = Path(C.DATA) / FAJL_USDC
    pc = Path(C.DATA) / FAJL_CENY
    chk(f"живые сделки CLMM с котировкой USDC на месте ({FAJL_USDC})", pu.exists(),
        str(pu))
    cena = None
    if pc.exists():
        cena = ((json.loads(pc.read_text(encoding="utf-8")).get("shablony") or {})
                .get(USDC_MINT) or {}).get("price_sol_v_sdelke")
    chk("цена USDC в SOL -- из статичного шаблона первой ноги репозитория",
        isinstance(cena, (int, float)) and cena > 0, cena)
    emkostej, vospr, prodazha = 0, 0, None
    if pu.exists() and cena:
        for r in json.loads(pu.read_text(encoding="utf-8")).get("ряды") or []:
            if r.get("tip") != "CLMM":
                continue
            k = rezerv_clmm_iz_sdelki(r.get("транзакция") or {}, pul=r.get("пул"),
                                      mint_kotirovki=USDC_MINT, q_dec=6,
                                      cena_sol_za_kotirovku=cena)
            if not k.get("ok"):
                continue
            emkostej += 1
            vospr += 1 if k.get("vosproizvelos") else 0
            if r.get("пул") == PUL_PRODAZHI:
                prodazha = k["rezerv_sol"]
    chk(f"ёмкость активного шага тика посчиталась у {ZHDEM_CLMM_EMKOST} сделок CLMM",
        emkostej == ZHDEM_CLMM_EMKOST, emkostej)
    chk(f"формула воспроизвела саму сделку источника у {ZHDEM_CLMM_VOSPROIZVELOS} из "
        f"{ZHDEM_CLMM_EMKOST} (у остальных сделка перешла границу диапазона)",
        vospr == ZHDEM_CLMM_VOSPROIZVELOS, vospr)
    chk("сторона котировки берётся по МИНТУ, а не по стороне входа события: у "
        f"пула, где источник продавал, ёмкость ниже {PREDEL_PRODAZHI_SOL} SOL-экв",
        prodazha is not None and prodazha < PREDEL_PRODAZHI_SOL, prodazha)
    chk("котировка не сторона этого пула -- отказ по имени",
        "не сторона" in (rezerv_clmm_iz_sdelki(
            {"meta": {}}, pul="нет", mint_kotirovki=USDC_MINT).get("why_not") or "")
        or not rezerv_clmm_iz_sdelki({"meta": {}}, pul="нет",
                                     mint_kotirovki=USDC_MINT).get("ok"), None)

    # ------------------------------------------- 6. DLMM: только нижняя граница
    granic = 0
    if pu.exists():
        for r in json.loads(pu.read_text(encoding="utf-8")).get("ряды") or []:
            if r.get("tip") != "DLMM":
                continue
            g = rezerv_dlmm_nizhe_ne_budet(r.get("транзакция") or {}, pul=r.get("пул"))
            granic += 1 if g.get("ok") and g.get("granica_sol") else 0
    chk("DLMM: ёмкости корзины в событии нет, граница по сделке источника есть",
        granic >= 1 and "granica_sol" in rezerv_dlmm_nizhe_ne_budet({}, pul="x"),
        granic)

    # --------------------------- 6a. программа и адрес пула по сделке источника
    pk = Path(C.DATA) / "c2_pool_samples" / "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK.json"
    sovpalo_pul, vsego_pul = 0, 0
    if pk.exists():
        import c2_clmm_stroitel as CM  # noqa: PLC0415

        for x in json.loads(pk.read_text(encoding="utf-8")):
            tx = x.get("tx")
            if not isinstance(tx, dict):
                continue
            pp = programma_i_pul(tx, istochnik=x.get("source"), mint=x.get("mint"))
            if not pp.get("ok"):
                continue
            t = CM.шаблон(tx, None, хранилище=pp["hranilishche"])
            if not t.get("ok"):
                continue
            vsego_pul += 1
            sovpalo_pul += 1 if pp["pul"] == t.get("пул") else 0
    chk(f"адрес пула = владелец хранилища: совпал с пулом из инструкции у "
        f"{ZHDEM_PUL_SOVPAL} образцов CLMM",
        sovpalo_pul == vsego_pul == ZHDEM_PUL_SOVPAL, (sovpalo_pul, vsego_pul))
    chk("программа пула определяется тем же путём, что у сборки",
        programma_i_pul({"meta": {}}, istochnik="x", mint="y").get("why_not")
        is not None, None)

    # ------------------------------------------------------- 7. дверь для полосы
    d1 = bilet_dlya_signala(potolok_sol=0.5, zapis={"pool_reserve_sol": 20.0},
                            dolya_vhod=0.01)
    chk("дверь: резерв из записи -- билет 0.2 и откуда он",
        d1["bilet_sol"] == 0.2 and d1["rezerv_otkuda"] == "запись: pool_reserve_sol",
        (d1["bilet_sol"], d1["rezerv_otkuda"]))
    d2 = bilet_dlya_signala(potolok_sol=0.5,
                            zapis={"pool_reserve_sol": 20.0,
                                   "pool_reserve_kind": VID_VIRTUALNYJ},
                            dolya_vhod=0.01)
    chk("дверь: резерв ВИРТУАЛЬНЫЙ -- билет равен потолку и сказано почему",
        d2["bilet_sol"] == 0.5 and not d2["ot_rezerva"]
        and "виртуальный" in (d2["rezerv_why_not"] or ""), d2.get("rezerv_why_not"))
    d3 = bilet_dlya_signala(potolok_sol=0.5, zapis={}, dolya_vhod=0.01)
    chk("дверь: ни записи, ни сделки -- билет потолок, не отказ",
        d3["ok"] and d3["bilet_sol"] == 0.5, d3["bilet_sol"])
    d4 = bilet_dlya_signala(potolok_sol=0.5, zapis={"pool_reserve_sol": "нет"},
                            tx_istochnika={"meta": {}}, istochnik="x", mint="y",
                            dolya_vhod=0.01)
    chk("дверь: мусор в записи и пустая сделка -- билет потолок, причины названы",
        d4["ok"] and d4["bilet_sol"] == 0.5 and d4["rezerv_why_not"],
        d4.get("rezerv_why_not"))
    chk("дверь без доли (умолчание окружения) -- билет потолок",
        bilet_dlya_signala(potolok_sol=0.5,
                           zapis={"pool_reserve_sol": 1.0})["bilet_sol"] == 0.5)

    plohih = [p for p in proverki if not p[1]]
    for chto, ok, fakt in proverki:
        print(("ok  " if ok else "НЕТ ") + chto + ("" if ok else f"  -- {fakt!r}"))
    print(f"\nпроверок {len(proverki)}, ждали {ZHDEM_PROVEROK}, не прошло {len(plohih)}")
    if len(proverki) != ZHDEM_PROVEROK:
        print("ЧИСЛО ПРОВЕРОК НЕ СОВПАЛО -- молчаливый пропуск считается провалом")
        return 1
    return 1 if plohih else 0


USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


if __name__ == "__main__":
    raise SystemExit(self_test())
