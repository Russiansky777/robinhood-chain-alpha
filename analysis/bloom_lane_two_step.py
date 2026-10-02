#!/usr/bin/env python3
"""Двухшаговая покупка полосы: SOL -> котировочный токен -> целевой токен.

ЗАЧЕМ (финальный план владельца 27.09, п.3). У лидера 74 % покупок идут через
пул, где котировка НЕ SOL: наш одношаговый сборщик такой сигнал честно
отказывает ("котировка пула не SOL -- полоса только одношаговая"). Два шага в
ОДНОЙ транзакции закрывают этот отказ: сначала SOL -> котировочный (Raydium
CPMM или Pump AMM), потом котировочный -> токен (пул источника).

ЧТО ВЗЯТО ГОТОВЫМ, А ЧТО НАПИСАНО ЗДЕСЬ. Разбор шаблонов, подстановка ролей
счетов, инструкции свопов, кэш шаблона первого шага и таблицы адресов -- это
c2_swap_build и c2_shadow_build второй сессии (её теневая сборка от 24.09,
проверенная симуляцией). Здесь -- ДЕНЕЖНАЯ часть, которой у тени не было:
  * минимум выхода на ОБОИХ шагах по проскальзыванию группы;
  * налог переводов котировочного токена (Token-2022): четыре перевода за круг,
    и если налог неизвестен -- ОТКАЗ, а не покупка наугад;
  * чаевые пулу отправителей и приоритет -- как у одношаговой сборки;
  * долговечный nonce -- тот же путь, что у одношаговой;
  * предел размера пакета сети и предел суммы.

САМОПРОВЕРКА СРАВНИВАЕТ СБОРКУ С ТЕНЕВОЙ БАЙТ В БАЙТ: без чаевых и без nonce
наша транзакция обязана совпасть с той, что собирает c2_shadow_build._two_hop.
Разойдутся -- значит мы собрали НЕ то, что проверено симуляцией.

Ни одной отправки в этом модуле. Подпись и отправка -- в bloom_own_send.
"""
from __future__ import annotations

import base64
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ЛАМПОРТОВ_В_SOL = 1_000_000_000
# Программа Token-2022: только у неё бывает налог на каждый перевод.
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
# Запас на первом шаге: сколько котировочного мы обещаем себе получить минимум.
# Число второй сессии (LEG1_HAIRCUT) -- оставляем её же, чтобы два пути не
# расходились в оценке первого шага.
ЗАПАС_ШАГА_1 = 0.05
# Предел размера пакета сети.
ПРЕДЕЛ_РАЗМЕРА_TX = 1232


def _модули():
    import c2_common as C  # noqa: PLC0415
    import c2_pool_programs as PP  # noqa: PLC0415
    import c2_shadow_build as SB  # noqa: PLC0415
    import c2_swap_build as B  # noqa: PLC0415
    return C, PP, SB, B


# ЗАПАС ПО НАЛОГУ КОТИРОВОЧНОГО ТОКЕНА, bps. Решение владельца 28.09: чтение
# налога минта убрать с пути покупки, а если значение всё же нужно -- "не ждать
# ответа, считать с запасом". Единственное место на пути покупки, где без
# ставки нельзя, -- котировочный токен на Token-2022: за круг четыре перевода.
# Запас взят с потолка налогового маршрута (600 bps по умолчанию): выше него
# покупка не идёт вовсе, значит считать по нему -- это считать по худшему из
# допустимых. У SOL и USDC (классический SPL) налога не бывает, и эта ветка на
# них не включается никогда.
ЗАПАС_НАЛОГА_BPS = 600


def налог_известен(программа_токена: str | None, налог_bps, *,
                    запас_bps: int | None = None) -> dict:
    """Можно ли считать налог переводов котировочного токена известным.

    ПОЧЕМУ ТАК СТРОГО. У GP (и любого Token-2022 с налогом) налог берётся с
    КАЖДОГО перевода: в круге их четыре -- пул1 -> мы, мы -> пул2 на входе и
    столько же на выходе. При 3 % это примерно 12 % за круг, и посчитать
    минимум выхода без налога значит купить дороже, чем думаешь. Классический
    SPL Token налога на перевод не имеет вовсе -- там ноль без вопросов.
    """
    if программа_токена != TOKEN_2022:
        return {"ok": True, "bps": 0, "почему": None}
    if isinstance(налог_bps, int) and 0 <= налог_bps <= 10_000:
        return {"ok": True, "bps": int(налог_bps), "почему": None}
    # НЕ ЖДЁМ ОТВЕТА -- СЧИТАЕМ С ЗАПАСОМ (решение владельца 28.09). Прежде
    # сборка тут отказывалась, и с чтением налога, убранным с пути покупки,
    # это означало бы отказ на каждом Token-2022 котировочном токене.
    if запас_bps is None:
        запас_bps = ЗАПАС_НАЛОГА_BPS
    if isinstance(запас_bps, int) and 0 <= запас_bps <= 10_000:
        return {"ok": True, "bps": int(запас_bps), "почему": None,
                 "с_запасом": True}
    return {"ok": False, "bps": None,
            "почему": ("котировочный токен на Token-2022, а налог переводов "
                        "неизвестен -- покупать наугад нельзя")}


def доля_мимо_пула(tpl: dict, tx: dict) -> float | None:
    """Какая доля траты источника НЕ дошла до хранилища пула. Замер, не модель.

    ЗАЧЕМ ИМЕННО ЭТО ЧИСЛО. Потолок BLOOM_LANE_MAX_POOL_FEE поставлен после
    живой сделки 26.09, где пул забрал 50.9 % входа. Проверять надо именно то,
    что забирают: сколько из ушедшего от покупателя легло в хранилище пула, а
    сколько разошлось по счетам комиссий, создателю и налогу перевода.

    ПОЧЕМУ НЕ 1 - fee_factor. fee_factor из min_out_from_reserves -- это
    калибровка модели x*y=k под сделку источника, и она впитывает всё, чего
    модель не видит: другие свопы в той же транзакции, комиссию, снятую с
    ВЫХОДА, несвежие резервы. На живых образцах Pump AMM она даёт 8.7 %, 19.2 %
    и 24.4 % там, где хранилище пула получило 99.1 %, 99.0 % и 98.8 % траты.
    Потолок по такому числу отказал бы половине настоящих покупок Pump AMM.

    Возвращает None, когда замерить нечем (хранилищ нет в балансах, трата не
    видна): молча считать это нулём нельзя -- потолок тогда не проверен.
    """
    try:
        C, _PP, _SB, B = _модули()
        mv = B.mints_and_vaults(tpl, tx)
        if not mv:
            return None
        строки = {r["account"]: r for r in C.token_rows(tx).values()}
        хранилище = строки.get(mv["quote_vault"])
        if not хранилище:
            return None
        пришло_в_пул = int(хранилище["post"]) - int(хранилище["pre"])
        ушло = sum(int(r["post"]) - int(r["pre"]) for a, r in строки.items()
                   if a in tpl["accounts"] and r["mint"] == mv["quote_mint"]
                   and int(r["post"]) > int(r["pre"]))
        if ушло <= 0 or пришло_в_пул <= 0:
            return None
        return max(0.0, 1.0 - float(D(пришло_в_пул) / D(ушло)))
    except Exception:  # noqa: BLE001
        return None


def доля_комиссии(мо: dict, tpl: dict | None = None, tx: dict | None = None):
    """Комиссия пула одним числом для всех типов пула.

    Сосредоточенная ликвидность (DAMM v2, DBC) отдаёт её прямо -- fee_share из
    c2_cl_quote. У пулов x*y=k такого ключа нет вовсе, и раньше денежный путь
    спрашивал только его: потолок BLOOM_LANE_MAX_POOL_FEE не срабатывал на
    CPMM, Pump AMM, Launchlab и кривой НИКОГДА, то есть у 74 % покупок лидера
    защиты от пула с большой комиссией не было. Для них считаем замером --
    доля_мимо_пула по сделке источника.
    """
    if мо.get("fee_share") is not None:
        return float(мо["fee_share"])
    if tpl is None or tx is None:
        return None
    return доля_мимо_пула(tpl, tx)


def после_налога(сумма: int, bps: int) -> int:
    """Сколько дойдёт после одного перевода с налогом bps."""
    if bps <= 0:
        return int(сумма)
    return int(D(сумма) * D(10_000 - bps) / D(10_000))


# ЗНАЧЕНИЕ NONCE ХОДИТ ДВУМЯ ВИДАМИ, И ОДИН ИЗ НИХ СТОИЛ СИГНАЛА. Полоса держит
# его СЛОВАРЁМ (bloom_own_send.нонс_для_сделки: account, authority, blockhash),
# а кирпич B.advance_nonce ждёт ПАРУ (аккаунт, распорядитель) -- так его и зовут
# варианты на nonce (собрать_варианты_нонсом сама делает пару). 02.10 словарь
# ушёл в сборку как есть, нонс[0] дал KeyError: 0, и единственный годный сигнал
# USDC-ноги за сутки потерян не по экономике, а на этом. Поэтому вид приводится
# В ОДНОМ МЕСТЕ, и непонятный вид -- ОТКАЗ СЛОВАМИ: распорядителя не выдумать, а
# собрать без advance_nonce значит отправить транзакцию, которую сеть отвергнет.
def пара_нонса(нонс) -> dict:
    """Значение nonce в виде (аккаунт, распорядитель) -- из пары или из словаря.

    Возвращает {ok, пара, почему}; пара None означает "nonce не передавали", а не
    "не разобрали": второе -- это ok=False со словами.
    """
    if нонс is None:
        return {"ok": True, "пара": None, "почему": None}
    if isinstance(нонс, dict):
        аккаунт, кто = нонс.get("account"), нонс.get("authority")
    elif isinstance(нонс, (list, tuple)) and len(нонс) == 2:
        аккаунт, кто = нонс[0], нонс[1]
    else:
        return {"ok": False, "пара": None,
                 "почему": (f"значение nonce непонятного вида "
                             f"({type(нонс).__name__}) -- advance_nonce собирать "
                             f"нечем")}
    if not аккаунт or not кто:
        return {"ok": False, "пара": None,
                 "почему": ("в значении nonce нет аккаунта или распорядителя -- "
                             "advance_nonce собирать нечем")}
    return {"ok": True, "пара": (str(аккаунт), str(кто)), "почему": None}


# ДЛИНА СТРОКИ СБОЯ. Она уходит в журнал решений и в выгрузку, и там её читают
# глазами: traceback целиком туда не влезет, а одного имени класса мало.
ПРЕДЕЛ_СЛЕДА_СБОЯ = 200


def след_сбоя(exc: BaseException, *, предел: int = ПРЕДЕЛ_СЛЕДА_СБОЯ) -> str:
    """repr исключения И место падения (файл:строка последнего кадра), одной строкой.

    ПОЧЕМУ НЕ type(exc).__name__. Живой сбой 02.10: в записи стояло ровно слово
    "KeyError" -- ни ключа, ни файла, ни строки, и причину по записи назвать было
    нельзя, пришлось повторять путь руками. repr даёт сам ключ (KeyError(0)), а
    последний кадр traceback -- то место, где упало, а не то, где поймали.
    """
    место = "место падения не известно"
    try:
        import traceback as _tb  # noqa: PLC0415

        кадры = _tb.extract_tb(exc.__traceback__)
        if кадры:
            к = кадры[-1]
            место = f"{Path(к.filename).name}:{к.lineno}"
    except Exception:  # noqa: BLE001
        # Разбор traceback сам упал -- место остаётся названным словами, а не
        # пустым: тихо потерять диагностику хуже, чем сказать, что её нет.
        pass
    try:
        тело = repr(exc)
    except Exception:  # noqa: BLE001
        тело = type(exc).__name__
    return f"{тело[:предел]} @ {место}"


def собрать(*, tx_источника: dict, источник: str, минт: str, наш_кошелёк: str,
            лампорты: int, проскальзывание: float = 0.35,
            cu_units: int = 800_000, приоритет_лампорты: int = 1_000_000,
            чаевые_лампорты: int = 1_000_000, чаевые_списком: list | None = None,
            чаевые_адрес: str | None = None, семя: str | None = None,
            нонс: tuple | None = None, rpc_call=None, кэш_ног=None,
            налог_котировки_bps=None, налог_минта=None,
            потолок_комиссии: float | None = None,
            проскальзывание_тонкого: float | None = None,
            порог_тонкого_sol: float | None = None) -> dict:
    """Одна транзакция: SOL -> котировочный -> токен. Без подписи и отправки."""
    из_ = {"ok": False, "why_not": None, "route": "two_step", "steps": 2,
            "pool_program": None, "leg1_pool_program": None, "quote_mint": None,
            "min_out": None, "expected_out": None, "leg1_min_out": None,
            "leg2_amount_in": None, "quote_fee_bps": None,
            "tx_base64": None, "size": None, "build_ms": None,
            "tip_account": None, "tips": None, "tips_total_lamports": None,
            "slippage_thin": None, "slippage_used": проскальзывание,
            "slippage_why": None, "pool_reserve_sol_eq": None,
            "min_out_floor_absent": None}
    t0 = time.perf_counter()
    if not isinstance(лампорты, int) or лампорты <= 0:
        из_["why_not"] = f"размер не положительное целое: {лампорты!r}"
        return из_
    if кэш_ног is None:
        из_["why_not"] = "кэша шаблонов первого шага нет -- собирать не из чего"
        return из_
    # ВИД NONCE ПРИВОДИТСЯ ДО СБОРКИ, А НЕ В СЕРЕДИНЕ ЕЁ: ниже нонс[0] стоит и в
    # advance_nonce, и в ответе, и словарь полосы там давал KeyError: 0 (02.10).
    нп = пара_нонса(нонс)
    if not нп["ok"]:
        из_["why_not"] = нп["почему"]
        return из_
    нонс = нп["пара"]
    try:
        C, PP, SB, B = _модули()
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"модули сборки не загружены: {type(exc).__name__}"
        return из_
    try:
        from solders.hash import Hash  # noqa: PLC0415
        from solders.message import MessageV0  # noqa: PLC0415
        from solders.pubkey import Pubkey  # noqa: PLC0415
        from solders.signature import Signature  # noqa: PLC0415
        from solders.transaction import VersionedTransaction  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"solders не загрузился: {type(exc).__name__}"
        return из_

    try:
        пул = C.identify_pool(tx_источника, источник, минт)
        if not пул.get("ok"):
            из_["why_not"] = f"пул источника: {пул.get('why_not')}"
            return из_
        прог2 = PP.pool_program(tx_источника, пул["pool_vault"], SB._labels())["pool_program"]
        из_["pool_program"] = прог2
        tpl2 = B.extract_template(tx_источника, прог2, пул["pool_vault"])
        if not tpl2.get("ok"):
            из_["why_not"] = f"шаблон шага 2: {tpl2.get('why_not')}"
            return из_
        mv2 = B.mints_and_vaults(tpl2, tx_источника)
        if not mv2:
            из_["why_not"] = "у пула источника не разобрались минты и хранилища"
            return из_
        q = mv2.get("quote_mint")
        из_["quote_mint"] = q
        if q in (C.WSOL, getattr(C, "NATIVE_QUOTE", "native_sol")):
            из_["why_not"] = ("котировка пула -- SOL: это одношаговая покупка, "
                               "двухшаговый путь тут не нужен")
            return из_
        # ТОЧНЫЙ ВЫХОД НЕ БЕРЁМ. У инструкции "точного выхода" аргументы стоят
        # в обратном порядке (минимум и предел траты), и собрать её в двух
        # шагах, не проверив живьём, значит поставить предел не туда.
        точный = (tpl2.get("exact_out") if tpl2.get("exact_out") is not None
                  else (B.spec_of(tpl2) or {}).get("exact_out"))
        if точный:
            из_["why_not"] = ("шаг 2 с инструкцией точного выхода -- двухшаговым "
                               "путём не берём")
            return из_
        e, возраст = кэш_ног.get(q)
        if e is None:
            из_["why_not"] = f"шаблона SOL -> {str(q)[:8]} в кэше нет"
            return из_
        из_["leg1_pool_program"] = e.get("program")
        из_["leg1_template_age_s"] = round(возраст, 1)
        if возраст > SB.LEG_MAX_AGE_S:
            из_["why_not"] = (f"шаблон шага 1 старше {SB.LEG_MAX_AGE_S} с "
                               f"({возраст:.0f} с)")
            return из_
        if not e.get("q_dec") or not e.get("price_sol"):
            из_["why_not"] = "у шаблона шага 1 нет цены котировочного токена"
            return из_
        mv1 = e["mv"]
        # НАЛОГ СПРАШИВАЕМ ТОЛЬКО ЕСЛИ ОН МОЖЕТ БЫТЬ: у классического SPL Token
        # налога на перевод не бывает вовсе, и лишний вызов сети на денежном
        # пути тут не нужен. У Token-2022 -- спрашиваем и, если не ответили,
        # отказываемся.
        if (налог_котировки_bps is None and mv1.get("base_program") == TOKEN_2022
                and callable(налог_минта)):
            try:
                от_сети = налог_минта(q) or {}
                if isinstance(от_сети.get("fee_bps"), int):
                    налог_котировки_bps = int(от_сети["fee_bps"])
                elif от_сети.get("taxed") is False:
                    налог_котировки_bps = 0
                из_["quote_fee_from"] = "сеть"
            except Exception as exc:  # noqa: BLE001
                из_["quote_fee_why_not"] = type(exc).__name__
        нал = налог_известен(mv1.get("base_program"), налог_котировки_bps)
        из_["quote_fee_bps"] = нал.get("bps")
        if нал.get("с_запасом"):
            из_["quote_fee_from"] = "запас (ставка не прочитана)"
        if not нал["ok"]:
            из_["why_not"] = нал["почему"]
            return из_
        bps = int(нал["bps"])
        # ОЖИДАНИЕ ПЕРВОГО ШАГА -- по цене шаблона, с запасом; минимум выхода
        # шага 1 -- это же число: меньше не примем.
        ожидание_q = (D(лампорты) / D(10) ** 9 / e["price_sol"]
                      * D(10) ** int(e["q_dec"]))
        шаг2_вход = int(ожидание_q * D(1 - ЗАПАС_ШАГА_1))
        if шаг2_вход <= 0:
            из_["why_not"] = "ожидаемый выход первого шага нулевой"
            return из_
        # НАЛОГ ПЕРЕВОДА СЪЕДАЕТ ЧАСТЬ ДВАЖДЫ: пул1 -> наш счёт и наш счёт ->
        # пул2. Первое уменьшает то, что у нас будет; второе -- то, что дойдёт
        # до пула. Оба учитываем, иначе шаг 2 упадёт на нехватке или купит
        # дороже расчёта.
        у_нас_будет = после_налога(шаг2_вход, bps)
        дойдёт_до_пула2 = после_налога(у_нас_будет, bps)
        из_["leg1_min_out"] = шаг2_вход
        из_["leg2_amount_in"] = у_нас_будет
        из_["leg2_to_pool"] = дойдёт_до_пула2
        мо = B.min_out_from_reserves(tpl2, tx_источника, дойдёт_до_пула2,
                                     проскальзывание)
        # НАЦЕНКА НА ТОНКОМ ПУЛЕ (слово владельца 27.09 вечером, leader).
        # РЕЗЕРВ ЗДЕСЬ НЕ В SOL: котировка второго шага -- НЕ SOL, и
        # reserves_after[0] выражен в единицах котировочного токена. Чтобы
        # сравнивать с порогом в SOL-эквиваленте, переводим ценой ноги:
        # price_sol -- это SOL за ЦЕЛЫЙ котировочный токен (та же величина, по
        # которой выше считалось ожидание первого шага). Спутать сырые единицы
        # с SOL значило бы считать тонким любой пул с шестью знаками после
        # запятой.
        if мо.get("ok") and проскальзывание_тонкого and порог_тонкого_sol:
            рез_т = мо.get("reserves_after") or мо.get("virtual_reserves_after")
            резерв_sol_экв = None
            if isinstance(рез_т, (list, tuple)) and рез_т:
                try:
                    резерв_sol_экв = float(
                        D(int(рез_т[0])) / D(10) ** int(e["q_dec"]) * e["price_sol"])
                except (TypeError, ValueError, ZeroDivisionError):
                    резерв_sol_экв = None
            из_["pool_reserve_sol_eq"] = резерв_sol_экв
            if (резерв_sol_экв is not None
                    and резерв_sol_экв < float(порог_тонкого_sol)):
                проскальзывание = float(проскальзывание_тонкого)
                из_["slippage_thin"] = True
                из_["slippage_used"] = проскальзывание
                из_["slippage_why"] = (
                    f"резерв котировочной стороны {резерв_sol_экв:.3f} SOL-экв. "
                    f"ниже {порог_тонкого_sol} -- наценка {проскальзывание}")
                мо = B.min_out_from_reserves(tpl2, tx_источника, дойдёт_до_пула2,
                                             проскальзывание)
            else:
                из_["slippage_thin"] = False
                из_["slippage_used"] = проскальзывание
        if not мо.get("ok"):
            из_["why_not"] = f"минимум шага 2 не выдаётся: {мо.get('why_not')}"
            return из_
        if not isinstance(мо.get("min_out"), int) or мо["min_out"] <= 0:
            из_["why_not"] = f"минимум шага 2 не положителен: {мо.get('min_out')!r}"
            return из_
        из_.update(min_out=мо["min_out"], expected_out=мо.get("expected_out"))
        рез = мо.get("reserves_after") or мо.get("virtual_reserves_after")
        if isinstance(рез, (list, tuple)) and рез:
            # У шага 2 котировка НЕ SOL, поэтому резерв тут в единицах
            # котировочного токена -- так и называем, не путая с SOL.
            из_["pool_reserve_quote_raw"] = int(рез[0])
        доля = доля_комиссии(мо, tpl2, tx_источника)
        из_["pool_fee_share"] = доля
        if (потолок_комиссии is not None and доля is not None
                and float(доля) > float(потолок_комиссии)):
            из_["why_not"] = (f"комиссия пула шага 2 {float(доля) * 100:.2f} % выше "
                               f"потолка {float(потолок_комиссии) * 100:.2f} %")
            return из_

        wsol_счёт = B.ata(наш_кошелёк, C.WSOL, mv1["quote_program"])
        ixs = []
        if нонс:
            ixs.append(B.advance_nonce(str(нонс[0]), str(нонс[1])))
        ixs += [B.cu_limit(cu_units), B.cu_price(_цена_cu(приоритет_лампорты, cu_units)),
                B.ata_idempotent(наш_кошелёк, наш_кошелёк, C.WSOL, mv1["quote_program"]),
                B.ata_idempotent(наш_кошелёк, наш_кошелёк, q, mv1["base_program"]),
                B.ata_idempotent(наш_кошелёк, наш_кошелёк, mv2["base_mint"],
                                  mv2["base_program"]),
                B.sol_transfer(наш_кошелёк, wsol_счёт, лампорты),
                B.sync_native(wsol_счёт),
                B.swap_instruction(e["tpl"], e["tx"], наш_кошелёк, лампорты, шаг2_вход),
                B.swap_instruction(tpl2, tx_источника, наш_кошелёк, у_нас_будет,
                                    мо["min_out"])]
        пары_чаевых = _пары_чаевых(чаевые_списком, чаевые_адрес, чаевые_лампорты)
        for адрес_ч, лампорты_ч in пары_чаевых:
            ixs.append(B.sol_transfer(наш_кошелёк, адрес_ч, int(лампорты_ч)))
        if пары_чаевых:
            из_["tip_account"] = пары_чаевых[0][0]
            из_["tips"] = list(пары_чаевых)
            из_["tips_total_lamports"] = sum(int(л) for _, л in пары_чаевых)

        нужны = [k for k in SB._lut_keys(e["tx"]) + SB._lut_keys(tx_источника)
                 if k not in кэш_ног.luts]
        if нужны:
            кэш_ног.load_luts(нужны, rpc=rpc_call)
            из_["hot_lut_calls"] = 1
        ключи = list(dict.fromkeys(SB._lut_keys(e["tx"])
                                    + SB._lut_keys(tx_источника)))
        таблицы = [кэш_ног.luts[k] for k in ключи if k in кэш_ног.luts]
        msg = MessageV0.try_compile(Pubkey.from_string(наш_кошелёк), ixs, таблицы,
                                     Hash.default())
        сырое = bytes(VersionedTransaction.populate(
            msg, [Signature.default()] * msg.header.num_required_signatures))
    except Exception as exc:  # noqa: BLE001
        # ОТКАЗ НАЗЫВАЕТ СЕБЯ ЦЕЛИКОМ: repr (с самим ключом) и файл:строка того
        # кадра, где упало. По прежней записи "KeyError: 0" место было не найти.
        из_["why_not"] = f"сборка двух шагов: {след_сбоя(exc)}"
        return из_
    из_["size"] = len(сырое)
    if len(сырое) > ПРЕДЕЛ_РАЗМЕРА_TX:
        из_["why_not"] = (f"двухшаговая транзакция {len(сырое)} байт при пределе "
                           f"сети {ПРЕДЕЛ_РАЗМЕРА_TX}")
        из_["too_big"] = True
        return из_
    из_.update(ok=True, tx_base64=base64.b64encode(сырое).decode(),
                nonce_account=(str(нонс[0]) if нонс else None),
                build_ms=round((time.perf_counter() - t0) * 1000, 3))
    return из_


# ------------------------------------------- зеркальная продажа (п.3 плана)

# РАСКЛАДКА ПРОДАЖИ У CPMM. Слоты 4/5 (наши счета), 6/7 (хранилища), 8/9
# (программы токена) и 10/11 (минты) стоят в порядке ВХОД/ВЫХОД, а не
# база/котировка. Установлено замером на настоящих сделках, не по документации:
# 34 инструкции swap_base_input из 31 РАЗНОГО пула (data/c2_pool_samples), и у
# ВСЕХ 34 хранилище слота 6 принимало, а хранилище слота 7 отдавало. При этом
# минт слота 10 в 21 случае канонически МЕНЬШЕ минта слота 11, а в 13 --
# БОЛЬШЕ. Пул Raydium CPMM создаётся с каноническим порядком token0 < token1;
# если бы слоты стояли в порядке пула, то в тех 13 случаях принимало бы
# хранилище слота 7. Значит порядок задаёт направление сделки, и продажа -- та
# же инструкция с переставленными четырьмя парами.
# Проверить это можно прогоном analysis/prodazha_razmetka_iz_zhivyh.py.
CPMM_ПАРЫ_НАПРАВЛЕНИЯ = ((4, 5), (6, 7), (8, 9), (10, 11))
# CloseAccount -- инструкция 9 и у SPL Token, и у Token-2022 (та же, что в
# close_empty_token_accounts и dbot_rescue).
ЗАКРЫТЬ_СЧЁТ = 9


def перевернуть_cpmm(tpl: dict, вход_минт: str) -> dict:
    """Шаблон CPMM в обратную сторону: вход -- вход_минт.

    Переставляются ровно четыре пары слотов (см. CPMM_ПАРЫ_НАПРАВЛЕНИЯ).
    Остальные счета -- конфигурация пула, распорядитель, сам пул и observation
    state -- от направления не зависят и переносятся как есть.
    """
    if tpl.get("program") != _CPMM():
        raise ValueError("переворот раскладки проверен только на CPMM")
    a = list(tpl["accounts"])
    for i, j in CPMM_ПАРЫ_НАПРАВЛЕНИЯ:
        a[i], a[j] = a[j], a[i]
    if a[10] != вход_минт:
        raise ValueError(f"после переворота вход {a[10][:8]}, а ждали {вход_минт[:8]}")
    return dict(tpl, accounts=a, force_in=вход_минт, flipped=True)


def _CPMM() -> str:
    _C, _PP, _SB, B = _модули()
    return B.CPMM


def инструкция_закрытия_счёта(счёт: str, владелец: str, программа_токена: str):
    """CloseAccount: рента уходит ВЛАДЕЛЬЦУ, другого получателя взять негде.

    ПОЧЕМУ ЗДЕСЬ, А НЕ close_empty_token_accounts.инструкция_закрытия. Та
    функция отказывается закрывать счёт с ненулевым остатком -- и правильно, её
    зовут отдельной транзакцией. В транзакции продажи остаток становится нулём
    ПЕРЕД закрытием, той же транзакцией: проверять его на сборке нечем и
    незачем, а если продажа не пройдёт -- не пройдёт и закрытие, вся транзакция
    откатится целиком.
    """
    from solders.instruction import AccountMeta, Instruction  # noqa: PLC0415
    from solders.pubkey import Pubkey  # noqa: PLC0415
    в = Pubkey.from_string(владелец)
    return Instruction(Pubkey.from_string(программа_токена), bytes([ЗАКРЫТЬ_СЧЁТ]),
                       [AccountMeta(Pubkey.from_string(счёт), False, True),
                        AccountMeta(в, False, True),
                        AccountMeta(в, True, True)])


def резервы_хранилищ(rpc_call, хранилища: list) -> dict:
    """Живые остатки хранилищ пула на момент сборки продажи.

    ПОЧЕМУ ЖИВЫЕ, А НЕ ИЗ СДЕЛКИ ИСТОЧНИКА. Минимум выхода ПОКУПКИ считается по
    резервам сразу после сделки источника -- между его сделкой и нашей проходят
    единицы слотов. Продажа уходит по таймеру hold_slots (72-150 слотов, это
    30-60 секунд): за это время цена другая, и минимум по тем резервам был бы не
    защитой, а выдумкой. Один вызов сети на продажу мы себе позволяем -- продажа
    не гонка, в отличие от покупки.
    """
    из_ = {"ok": False, "why_not": None, "остатки": {}}
    if not callable(rpc_call):
        из_["why_not"] = "живых резервов взять нечем: rpc_call не передан"
        return из_
    try:
        от = rpc_call("getMultipleAccounts",
                      [list(хранилища), {"encoding": "jsonParsed"}]) or {}
        знач = от.get("value") or []
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"чтение хранилищ не удалось: {type(exc).__name__}"
        return из_
    if len(знач) != len(хранилища):
        из_["why_not"] = f"ответ сети на {len(знач)} счетов из {len(хранилища)}"
        return из_
    for адрес, счёт in zip(хранилища, знач):
        сведения = (((счёт or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
        сумма = (сведения.get("tokenAmount") or {}).get("amount")
        if сумма is None:
            из_["why_not"] = f"у хранилища {адрес[:8]} нет остатка в ответе сети"
            return из_
        из_["остатки"][адрес] = int(сумма)
    из_["ok"] = True
    return из_


def минимум_продажи(*, резерв_входа: int, резерв_выхода: int, сумма_входа: int,
                    доля_мимо: float, проскальзывание: float) -> dict:
    """x*y=k по ЖИВЫМ резервам: сколько выйдет и сколько минимум принимаем."""
    из_ = {"ok": False, "why_not": None, "expected_out": None, "min_out": None}
    if резерв_входа <= 0 or резерв_выхода <= 0:
        из_["why_not"] = f"резервы пула не положительны: {резерв_входа}, {резерв_выхода}"
        return из_
    if сумма_входа <= 0:
        из_["why_not"] = f"продавать нечего: {сумма_входа}"
        return из_
    if not 0.0 <= float(проскальзывание) < 1.0:
        из_["why_not"] = f"проскальзывание вне (0, 1): {проскальзывание!r}"
        return из_
    вошло = D(сумма_входа) * (D(1) - D(str(доля_мимо)))
    ожидание = D(резерв_выхода) * вошло / (D(резерв_входа) + вошло)
    минимум = int(ожидание * (D(1) - D(str(проскальзывание))))
    if минимум <= 0:
        из_["why_not"] = "минимум выхода получился нулевым"
        return из_
    из_.update(ok=True, expected_out=int(ожидание), min_out=минимум)
    return из_


def собрать_продажу(*, tx_источника: dict, источник: str, минт: str,
                    наш_кошелёк: str, токенов: int, проскальзывание: float = 0.35,
                    cu_units: int = 800_000, приоритет_лампорты: int = 1_000_000,
                    чаевые_лампорты: int = 0, чаевые_списком: list | None = None,
                    чаевые_адрес: str | None = None, нонс: tuple | None = None,
                    rpc_call=None, кэш_ног=None, налог_котировки_bps=None,
                    налог_минта=None, закрывать_счёт_токена: bool = True) -> dict:
    """Одна транзакция: токен -> котировочный -> SOL. Без подписи и отправки.

    Зеркало собрать(): те же два пула, обратное направление, тот же налог
    переводов котировочного токена (четыре перевода за круг), тот же путь
    отправки. Отличий от покупки три, и все денежные:

    1. МИНИМУМ ВЫХОДА -- ПО ЖИВЫМ РЕЗЕРВАМ. Продажа уходит по таймеру
       hold_slots, через 30-60 секунд после покупки: резервы из сделки источника
       к этому времени не годятся (см. резервы_хранилищ).
    2. СЧЁТ ТОКЕНА ЗАКРЫВАЕТСЯ ТОЙ ЖЕ ТРАНЗАКЦИЕЙ (п.1б плана): остаток на нём
       становится нулём этой же транзакцией, рента возвращается на кошелёк.
       Отдельной транзакции закрытия (bloom_close_on_sell) для полосы больше не
       нужно -- она стоила ещё один тариф сети.
    3. СЧЁТ WSOL ТОЖЕ ЗАКРЫВАЕТСЯ: выход второго шага приходит ЗАВЁРНУТЫМ, и без
       закрытия SOL остался бы завёрнутым на счёте, а не на кошельке.

    Раскладка обратного направления взята с настоящих сделок (см.
    CPMM_ПАРЫ_НАПРАВЛЕНИЯ) и только для CPMM. Pump AMM продажей не собираем:
    у него направление задаёт ДРУГАЯ инструкция, а ни одной живой продажи Pump
    AMM в образцах нет -- имя инструкции выдумывать на деньгах нельзя.
    """
    из_ = {"ok": False, "why_not": None, "route": "two_step_sell", "steps": 2,
            "pool_program": None, "leg2_pool_program": None, "quote_mint": None,
            "min_out": None, "expected_out": None, "leg1_min_out": None,
            "leg2_amount_in": None, "quote_fee_bps": None, "tx_base64": None,
            "size": None, "build_ms": None, "tip_account": None, "tips": None,
            "tips_total_lamports": None, "closed_accounts": [],
            "pool_fee_share": None, "leg2_fee_share": None,
            "reserves_live": None}
    t0 = time.perf_counter()
    if not isinstance(токенов, int) or токенов <= 0:
        из_["why_not"] = f"количество токена не положительное целое: {токенов!r}"
        return из_
    if кэш_ног is None:
        из_["why_not"] = "кэша шаблонов пула SOL/котировка нет -- собирать не из чего"
        return из_
    # Тот же приём вида nonce, что у покупки: зеркало обязано ломаться и
    # отказывать одинаково, иначе продажа повторит сбой покупки на своём месте.
    нп = пара_нонса(нонс)
    if not нп["ok"]:
        из_["why_not"] = нп["почему"]
        return из_
    нонс = нп["пара"]
    try:
        C, PP, SB, B = _модули()
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"модули сборки не загружены: {type(exc).__name__}"
        return из_
    try:
        from solders.hash import Hash  # noqa: PLC0415
        from solders.message import MessageV0  # noqa: PLC0415
        from solders.pubkey import Pubkey  # noqa: PLC0415
        from solders.signature import Signature  # noqa: PLC0415
        from solders.transaction import VersionedTransaction  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"solders не загрузился: {type(exc).__name__}"
        return из_

    try:
        пул = C.identify_pool(tx_источника, источник, минт)
        if not пул.get("ok"):
            из_["why_not"] = f"пул источника: {пул.get('why_not')}"
            return из_
        прог_т = PP.pool_program(tx_источника, пул["pool_vault"], SB._labels())["pool_program"]
        из_["pool_program"] = прог_т
        if прог_т != B.CPMM:
            из_["why_not"] = ("продажа своим строителем проверена только на CPMM; "
                               f"у пула токена {прог_т[:8]} -- продаёт Jupiter")
            return из_
        tpl_т = B.extract_template(tx_источника, прог_т, пул["pool_vault"])
        if not tpl_т.get("ok"):
            из_["why_not"] = f"шаблон пула токена: {tpl_т.get('why_not')}"
            return из_
        mv_т = B.mints_and_vaults(tpl_т, tx_источника)
        if not mv_т:
            из_["why_not"] = "у пула токена не разобрались минты и хранилища"
            return из_
        q = mv_т.get("quote_mint")
        из_["quote_mint"] = q
        if q in (C.WSOL, getattr(C, "NATIVE_QUOTE", "native_sol")):
            из_["why_not"] = ("котировка пула -- SOL: это одношаговая продажа, "
                               "двухшаговый путь тут не нужен")
            return из_
        if mv_т.get("base_mint") != минт:
            из_["why_not"] = (f"в пуле база {str(mv_т.get('base_mint'))[:8]}, "
                               f"а продаём {минт[:8]}")
            return из_
        e, возраст = кэш_ног.get(q)
        if e is None:
            из_["why_not"] = f"шаблона пула SOL/{str(q)[:8]} в кэше нет"
            return из_
        из_["leg2_pool_program"] = e.get("program")
        из_["leg2_template_age_s"] = round(возраст, 1)
        if e.get("program") != B.CPMM:
            из_["why_not"] = ("продажа своим строителем проверена только на CPMM; "
                               f"у пула котировки {str(e.get('program'))[:8]} -- "
                               "продаёт Jupiter")
            return из_
        if возраст > SB.LEG_MAX_AGE_S:
            из_["why_not"] = (f"шаблон пула котировки старше {SB.LEG_MAX_AGE_S} с "
                               f"({возраст:.0f} с)")
            return из_
        mv_q = e["mv"]
        # НАЛОГ ПЕРЕВОДОВ КОТИРОВОЧНОГО -- ТОТ ЖЕ, ЧТО В ПОКУПКЕ: за круг четыре
        # перевода, на продаже -- вторая пара (пул токена -> мы, мы -> пул SOL).
        if (налог_котировки_bps is None and mv_q.get("base_program") == TOKEN_2022
                and callable(налог_минта)):
            try:
                от_сети = налог_минта(q) or {}
                if isinstance(от_сети.get("fee_bps"), int):
                    налог_котировки_bps = int(от_сети["fee_bps"])
                elif от_сети.get("taxed") is False:
                    налог_котировки_bps = 0
                из_["quote_fee_from"] = "сеть"
            except Exception as exc:  # noqa: BLE001
                из_["quote_fee_why_not"] = type(exc).__name__
        нал = налог_известен(mv_q.get("base_program"), налог_котировки_bps)
        из_["quote_fee_bps"] = нал.get("bps")
        if нал.get("с_запасом"):
            из_["quote_fee_from"] = "запас (ставка не прочитана)"
        if not нал["ok"]:
            из_["why_not"] = нал["почему"]
            return из_
        bps = int(нал["bps"])
        # КОМИССИЯ ОБОИХ ПУЛОВ -- ЗАМЕРОМ по сделкам, из которых взяты шаблоны.
        # У пула x*y=k доля не зависит от направления, а без неё минимум выхода
        # был бы завышен и продажа откатывалась бы каждый раз.
        доля_т = доля_мимо_пула(tpl_т, tx_источника)
        доля_q = доля_мимо_пула(e["tpl"], e["tx"])
        из_["pool_fee_share"], из_["leg2_fee_share"] = доля_т, доля_q
        if доля_т is None or доля_q is None:
            из_["why_not"] = ("комиссия пула не замеряется по сделке-шаблону "
                               f"(токен {доля_т}, котировка {доля_q}) -- минимум "
                               "выхода был бы выдумкой, продаёт Jupiter")
            return из_
        # ЖИВЫЕ РЕЗЕРВЫ ОБОИХ ПУЛОВ. Вход первого шага -- базовое хранилище пула
        # токена, выход -- его хранилище котировки; у пула котировки наоборот:
        # там котировка это WSOL, а база -- наш котировочный токен.
        хран = [mv_т["base_vault"], mv_т["quote_vault"],
                mv_q["base_vault"], mv_q["quote_vault"]]
        рез = резервы_хранилищ(rpc_call, хран)
        if not рез["ok"]:
            из_["why_not"] = f"живые резервы: {рез['why_not']}"
            return из_
        о = рез["остатки"]
        из_["reserves_live"] = {"токен_вход": о[mv_т["base_vault"]],
                                 "котировка_выход": о[mv_т["quote_vault"]],
                                 "котировка_вход": о[mv_q["base_vault"]],
                                 "sol_выход": о[mv_q["quote_vault"]]}
        # ШАГ 1: токен -> котировочный.
        м1 = минимум_продажи(резерв_входа=о[mv_т["base_vault"]],
                             резерв_выхода=о[mv_т["quote_vault"]],
                             сумма_входа=токенов, доля_мимо=доля_т,
                             проскальзывание=проскальзывание)
        if not м1["ok"]:
            из_["why_not"] = f"минимум шага 1: {м1['why_not']}"
            return из_
        из_["leg1_min_out"] = м1["min_out"]
        из_["leg1_expected_out"] = м1["expected_out"]
        # НАЛОГ СЪЕДАЕТ ПЕРЕВОД ПУЛ -> МЫ, а потом наш перевод в пул SOL.
        у_нас_будет = после_налога(м1["min_out"], bps)
        дойдёт_до_пула = после_налога(у_нас_будет, bps)
        из_["leg2_amount_in"] = у_нас_будет
        из_["leg2_to_pool"] = дойдёт_до_пула
        if у_нас_будет <= 0:
            из_["why_not"] = "после налога от первого шага не остаётся ничего"
            return из_
        # ШАГ 2: котировочный -> SOL.
        м2 = минимум_продажи(резерв_входа=о[mv_q["base_vault"]],
                             резерв_выхода=о[mv_q["quote_vault"]],
                             сумма_входа=дойдёт_до_пула, доля_мимо=доля_q,
                             проскальзывание=проскальзывание)
        if not м2["ok"]:
            из_["why_not"] = f"минимум шага 2: {м2['why_not']}"
            return из_
        из_.update(min_out=м2["min_out"], expected_out=м2["expected_out"],
                    min_out_sol=round(м2["min_out"] / ЛАМПОРТОВ_В_SOL, 9),
                    expected_out_sol=round(м2["expected_out"] / ЛАМПОРТОВ_В_SOL, 9))

        счёт_токена = B.ata(наш_кошелёк, минт, mv_т["base_program"])
        счёт_q = B.ata(наш_кошелёк, q, mv_q["base_program"])
        счёт_wsol = B.ata(наш_кошелёк, C.WSOL, mv_q["quote_program"])
        tpl_т_обр = перевернуть_cpmm(tpl_т, минт)
        tpl_q_обр = перевернуть_cpmm(e["tpl"], q)
        ixs = []
        if нонс:
            ixs.append(B.advance_nonce(str(нонс[0]), str(нонс[1])))
        ixs += [B.cu_limit(cu_units),
                B.cu_price(_цена_cu(приоритет_лампорты, cu_units)),
                # Счёт WSOL создаём: выход второго шага приходит на него. Счёт
                # котировочного тоже -- он уже есть после покупки, но
                # idempotent дешевле отказа всей продажи.
                B.ata_idempotent(наш_кошелёк, наш_кошелёк, C.WSOL, mv_q["quote_program"]),
                B.ata_idempotent(наш_кошелёк, наш_кошелёк, q, mv_q["base_program"]),
                B.swap_instruction(tpl_т_обр, tx_источника, наш_кошелёк, токенов,
                                    м1["min_out"]),
                B.swap_instruction(tpl_q_обр, e["tx"], наш_кошелёк, у_нас_будет,
                                    м2["min_out"])]
        # ЗАКРЫТИЕ СЧЕТОВ -- ПОСЛЕ СВОПОВ, в той же транзакции (п.1б).
        if закрывать_счёт_токена:
            ixs.append(инструкция_закрытия_счёта(счёт_токена, наш_кошелёк,
                                                  mv_т["base_program"]))
            из_["closed_accounts"].append(счёт_токена)
        ixs.append(инструкция_закрытия_счёта(счёт_wsol, наш_кошелёк,
                                             mv_q["quote_program"]))
        из_["closed_accounts"].append(счёт_wsol)
        из_["quote_account"] = счёт_q
        пары_чаевых = _пары_чаевых(чаевые_списком, чаевые_адрес, чаевые_лампорты)
        for адрес_ч, лампорты_ч in пары_чаевых:
            ixs.append(B.sol_transfer(наш_кошелёк, адрес_ч, int(лампорты_ч)))
        if пары_чаевых:
            из_["tip_account"] = пары_чаевых[0][0]
            из_["tips"] = list(пары_чаевых)
            из_["tips_total_lamports"] = sum(int(л) for _, л in пары_чаевых)

        нужны = [k for k in SB._lut_keys(e["tx"]) + SB._lut_keys(tx_источника)
                 if k not in кэш_ног.luts]
        if нужны:
            кэш_ног.load_luts(нужны, rpc=rpc_call)
            из_["hot_lut_calls"] = 1
        ключи = list(dict.fromkeys(SB._lut_keys(e["tx"])
                                    + SB._lut_keys(tx_источника)))
        таблицы = [кэш_ног.luts[k] for k in ключи if k in кэш_ног.luts]
        msg = MessageV0.try_compile(Pubkey.from_string(наш_кошелёк), ixs, таблицы,
                                     Hash.default())
        сырое = bytes(VersionedTransaction.populate(
            msg, [Signature.default()] * msg.header.num_required_signatures))
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"сборка продажи: {type(exc).__name__}: {str(exc)[:160]}"
        return из_
    из_["size"] = len(сырое)
    if len(сырое) > ПРЕДЕЛ_РАЗМЕРА_TX:
        из_["why_not"] = (f"продажа {len(сырое)} байт при пределе сети "
                           f"{ПРЕДЕЛ_РАЗМЕРА_TX}")
        из_["too_big"] = True
        return из_
    из_.update(ok=True, tx_base64=base64.b64encode(сырое).decode(),
                nonce_account=(str(нонс[0]) if нонс else None),
                build_ms=round((time.perf_counter() - t0) * 1000, 3))
    return из_


def _цена_cu(приоритет_лампорты: int, cu_units: int) -> int:
    """Микролампорты за единицу вычислений -- одна формула с одношаговой."""
    if cu_units <= 0:
        return 0
    return int(D(приоритет_лампорты) * D(1_000_000) / D(cu_units))


def _пары_чаевых(списком, адрес, лампорты) -> list:
    """Список (адрес, лампорты). Пусто -- чаевых в транзакции нет вовсе."""
    if списком:
        return [(а, int(л)) for а, л in списком]
    if адрес and лампорты:
        return [(адрес, int(лампорты))]
    return []


# --------------------------------------------------------------- самопроверка

def self_test() -> int:
    """Денежная самопроверка: байт в байт с теневой сборкой плюс налог и пределы."""
    import json  # noqa: PLC0415

    проверки = []

    def chk(имя, ок, факт=""):
        проверки.append((имя, bool(ок), факт))

    C, PP, SB, B = _модули()

    # --- налог переводов: классический токен -- ноль без вопросов, Token-2022
    # без числа -- ОТКАЗ, с числом -- принимается.
    chk("классический SPL Token: налог переводов ноль",
        налог_известен("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", None)
        == {"ok": True, "bps": 0, "почему": None},
        налог_известен("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", None))
    # ТОКЕН-2022 БЕЗ СТАВКИ: С 28.09 НЕ ОТКАЗ, А ЗАПАС. Решение владельца:
    # чтение налога убрать с пути покупки, "если используется -- не ждать
    # ответа, считать с запасом". Запас -- потолок налогового маршрута, то есть
    # худшая из ставок, при которой покупка вообще разрешена.
    н2 = налог_известен(TOKEN_2022, None)
    chk(f"Token-2022 без ставки считается с запасом {н2['bps']} bps, а не отказом",
        н2["ok"] is True and н2["bps"] == ЗАПАС_НАЛОГА_BPS
        and н2.get("с_запасом") is True, н2)
    chk("запас можно задать вызовом, и он идёт в расчёт",
        налог_известен(TOKEN_2022, None, запас_bps=900)["bps"] == 900,
        налог_известен(TOKEN_2022, None, запас_bps=900))
    chk("мусорный запас -- отказ, а не молчаливый ноль",
        налог_известен(TOKEN_2022, None, запас_bps=99999)["ok"] is False,
        налог_известен(TOKEN_2022, None, запас_bps=99999))
    chk("Token-2022 с налогом 300 bps -- принимается",
        налог_известен(TOKEN_2022, 300)["bps"] == 300,
        налог_известен(TOKEN_2022, 300))
    chk("налог съедает каждый перевод: 1000 при 300 bps -> 970, дважды -> 940",
        после_налога(1000, 300) == 970 and после_налога(после_налога(1000, 300), 300) == 940,
        (после_налога(1000, 300), после_налога(после_налога(1000, 300), 300)))
    chk("без налога перевод не уменьшается", после_налога(1000, 0) == 1000)

    # --- КОМИССИЯ ПУЛА: замер, а не калибровка модели.
    chk("fee_share (сосредоточенная ликвидность) берётся как есть",
        доля_комиссии({"fee_share": 0.509}) == 0.509)
    chk("без fee_share и без сделки источника доля неизвестна (None), "
        "нулём не притворяется",
        доля_комиссии({"fee_factor": 0.9975}) is None)
    _обр_ком = [x for x in B.load_samples(B.CPMM) if x.get("pool_vault")]
    _замеры = []
    for _x in _обр_ком:
        _t = B.extract_template(_x["tx"], B.CPMM, _x["pool_vault"])
        if not _t.get("ok"):
            continue
        _d = доля_мимо_пула(_t, _x["tx"])
        if _d is None:
            continue
        _mv = B.mints_and_vaults(_t, _x["tx"])
        _стр = {r["account"]: r for r in C.token_rows(_x["tx"]).values()}
        _в_пул = _стр[_mv["quote_vault"]]["post"] - _стр[_mv["quote_vault"]]["pre"]
        _ушло = sum(r["post"] - r["pre"] for a, r in _стр.items()
                    if a in _t["accounts"] and r["mint"] == _mv["quote_mint"]
                    and r["post"] > r["pre"])
        _замеры.append((_d, 1.0 - _в_пул / _ушло, доля_комиссии({}, _t, _x["tx"])))
    chk(f"комиссия замерена на живых образцах CPMM ({len(_замеры)} шт.)",
        len(_замеры) >= 5, len(_замеры))
    if _замеры:
        chk("замер совпадает со счётом «сколько траты легло в хранилище пула»",
            all(abs(a - b) < 1e-12 for a, b, _ in _замеры),
            [(a, b) for a, b, _ in _замеры if abs(a - b) >= 1e-12][:3])
        chk("доля_комиссии без fee_share отдаёт тот же замер",
            all(c == a for a, _, c in _замеры))
        chk("на живых покупках CPMM комиссия ниже потолка 5 % -- защита "
            "включается, не отказывая честным сигналам",
            max(a for a, _, _ in _замеры) < 0.05,
            round(max(a for a, _, _ in _замеры), 6))
    # Пул, забравший половину входа, обязан быть выше потолка: это и есть
    # случай живой сделки 26.09 (50.9 %), из-за которого потолок поставлен.
    _полпула = {"accounts": ["ХРАН", "ЧУЖОЙ"], "program": B.CPMM}
    class _ПодменаB:
        @staticmethod
        def mints_and_vaults(tpl, tx):
            return {"quote_vault": "ХРАН", "quote_mint": "МИНТ"}
    class _ПодменаC:
        @staticmethod
        def token_rows(tx):
            return {0: {"account": "ХРАН", "mint": "МИНТ", "pre": 0, "post": 50},
                    1: {"account": "ЧУЖОЙ", "mint": "МИНТ", "pre": 0, "post": 51}}
    _было = globals()["_модули"]
    globals()["_модули"] = lambda: (_ПодменаC, None, None, _ПодменаB)
    try:
        _дл = доля_мимо_пула(_полпула, {})
    finally:
        globals()["_модули"] = _было
    chk("пул, забравший половину входа: доля мимо пула 50.5 % -- выше потолка 5 %",
        _дл is not None and abs(_дл - 0.5049504950495049) < 1e-12 and _дл > 0.05, _дл)

    # === ЗЕРКАЛЬНАЯ ПРОДАЖА (п.3): переворот раскладки, минимум, закрытие.
    # Раскладка проверяется на ВСЕХ настоящих шаблонах CPMM из образцов.
    _обр_пр = [x for x in B.load_samples(B.CPMM) if x.get("pool_vault")]
    _перевёрнуто = _двойной = _слоты = _мимо = 0
    _пр_ошибки = []
    for _x in _обр_пр:
        _t = B.extract_template(_x["tx"], B.CPMM, _x["pool_vault"])
        if not _t.get("ok"):
            continue
        _mv = B.mints_and_vaults(_t, _x["tx"])
        if not _mv:
            continue
        _вход = _mv["base_mint"]          # продаём базу
        try:
            _o = перевернуть_cpmm(_t, _вход)
        except ValueError as _e:
            _пр_ошибки.append(str(_e))
            continue
        _перевёрнуто += 1
        # ровно четыре пары переставлены, остальное слово в слово
        _пары = {и for п in CPMM_ПАРЫ_НАПРАВЛЕНИЯ for и in п}
        if (all(_o["accounts"][и] == _t["accounts"][ж] and _o["accounts"][ж] == _t["accounts"][и]
                for и, ж in CPMM_ПАРЫ_НАПРАВЛЕНИЯ)
                and all(_o["accounts"][и] == _t["accounts"][и]
                        for и in range(len(_t["accounts"])) if и not in _пары)):
            _слоты += 1
        if перевернуть_cpmm(_o, _mv["quote_mint"])["accounts"] == _t["accounts"]:
            _двойной += 1
        # наши счета встали по направлению: вход -- ATA продаваемого токена
        _мвo = B.mints_and_vaults(_o, _x["tx"])
        _наши = B.user_accounts(_o, _x["tx"], C.EXECUTOR_WALLET)
        if (_мвo["quote_mint"] == _вход
                and _наши.get(4) == B.ata(C.EXECUTOR_WALLET, _вход, _mv["base_program"])
                and _наши.get(5) == B.ata(C.EXECUTOR_WALLET, _mv["quote_mint"],
                                          _mv["quote_program"])
                and _o["accounts"][6] == _mv["base_vault"]
                and _o["accounts"][7] == _mv["quote_vault"]):
            _мимо += 1
    chk(f"переворот раскладки проверен на живых шаблонах CPMM ({_перевёрнуто} шт.)",
        _перевёрнуто >= 5, (_перевёрнуто, _пр_ошибки[:2]))
    if _перевёрнуто:
        chk("переставлены ровно четыре пары слотов, остальные счета слово в слово",
            _слоты == _перевёрнуто, (_слоты, _перевёрнуто))
        chk("двойной переворот возвращает исходную раскладку",
            _двойной == _перевёрнуто, (_двойной, _перевёрнуто))
        chk("после переворота НАШ счёт входа -- ATA продаваемого токена, выхода -- "
            "ATA котировки, хранилища по направлению",
            _мимо == _перевёрнуто, (_мимо, _перевёрнуто))
    _t0 = next((B.extract_template(x["tx"], B.CPMM, x["pool_vault"]) for x in _обр_пр
                if B.extract_template(x["tx"], B.CPMM, x["pool_vault"]).get("ok")), None)
    if _t0:
        try:
            перевернуть_cpmm(_t0, "11111111111111111111111111111111")
            _чужой = "переворот принял чужой минт входа"
        except ValueError as _e:
            _чужой = None
        chk("переворот с чужим минтом входа -- отказ, а не молчаливая подмена",
            _чужой is None, _чужой)
        try:
            перевернуть_cpmm(dict(_t0, program=B.PUMP_AMM), _t0["accounts"][11])
            _не_cpmm = "переворот принял не CPMM"
        except ValueError as _e:
            _не_cpmm = None
        chk("переворот раскладки не CPMM -- отказ (у Pump AMM продажа -- другая "
            "инструкция, живых продаж в образцах нет)",
            _не_cpmm is None, _не_cpmm)

    # --- МИНИМУМ ПРОДАЖИ: числа известны наперёд.
    _м = минимум_продажи(резерв_входа=1000, резерв_выхода=1000, сумма_входа=100,
                         доля_мимо=0.0, проскальзывание=0.0)
    chk("минимум продажи по x*y=k: 100 в пул 1000/1000 даёт 90",
        _м["ok"] and _м["expected_out"] == 90 and _м["min_out"] == 90, _м)
    _мс = минимум_продажи(резерв_входа=1000, резерв_выхода=1000, сумма_входа=100,
                          доля_мимо=0.0, проскальзывание=0.35)
    chk("проскальзывание 35 % опускает минимум до 59, ожидание не меняет",
        _мс["ok"] and _мс["expected_out"] == 90 and _мс["min_out"] == 59, _мс)
    _мк0 = минимум_продажи(резерв_входа=10**9, резерв_выхода=10**9,
                           сумма_входа=10**6, доля_мимо=0.0, проскальзывание=0.0)
    _мк = минимум_продажи(резерв_входа=10**9, резерв_выхода=10**9,
                          сумма_входа=10**6, доля_мимо=0.01, проскальзывание=0.0)
    chk("комиссия пула уменьшает ожидание продажи примерно на свою долю",
        _мк["ok"] and _мк["expected_out"] < _мк0["expected_out"]
        and abs(1 - _мк["expected_out"] / _мк0["expected_out"] - 0.01) < 1e-4,
        (_мк0["expected_out"], _мк["expected_out"]))
    _м2 = минимум_продажи(резерв_входа=1000, резерв_выхода=1000, сумма_входа=200,
                          доля_мимо=0.0, проскальзывание=0.0)
    chk("вдвое больший объём даёт МЕНЬШЕ чем вдвое выхода (влияние на цену)",
        _м2["expected_out"] < 2 * _м["expected_out"], (_м2, _м))
    for _имя, _кв in (("резерв нулевой", dict(резерв_входа=0, резерв_выхода=1000,
                                              сумма_входа=1, доля_мимо=0.0,
                                              проскальзывание=0.0)),
                      ("продавать нечего", dict(резерв_входа=1000, резерв_выхода=1000,
                                                сумма_входа=0, доля_мимо=0.0,
                                                проскальзывание=0.0)),
                      ("проскальзывание 100 %", dict(резерв_входа=1000, резерв_выхода=1000,
                                                     сумма_входа=100, доля_мимо=0.0,
                                                     проскальзывание=1.0))):
        _о = минимум_продажи(**_кв)
        chk(f"минимум продажи отказывает: {_имя}",
            _о["ok"] is False and bool(_о["why_not"]), _о)

    # --- КРУГОВОЙ ОБОРОТ НА ЖИВЫХ ЧИСЛАХ: продать обратно ровно купленное
    # источником должно вернуть МЕНЬШЕ, чем он заплатил (две комиссии пула), но
    # не в разы меньше. Это проверка формулы на настоящих резервах, а не на
    # выдуманных.
    _круги = []
    for _x in _обр_пр:
        _t = B.extract_template(_x["tx"], B.CPMM, _x["pool_vault"])
        if not _t.get("ok"):
            continue
        _мо = B.min_out_from_reserves(_t, _x["tx"], 10_000_000, 0.35)
        _д = доля_мимо_пула(_t, _x["tx"])
        if not _мо.get("ok") or _д is None:
            continue
        _x1, _y1 = _мо["reserves_after"]
        _mv = B.mints_and_vaults(_t, _x["tx"])
        _стр = {r["account"]: r for r in C.token_rows(_x["tx"]).values()}
        _dy = _стр[_mv["base_vault"]]["pre"] - _стр[_mv["base_vault"]]["post"]
        _ушло = sum(r["post"] - r["pre"] for a, r in _стр.items()
                    if a in _t["accounts"] and r["mint"] == _mv["quote_mint"]
                    and r["post"] > r["pre"])
        if _dy <= 0 or _ушло <= 0:
            continue
        _назад = минимум_продажи(резерв_входа=_y1, резерв_выхода=_x1,
                                 сумма_входа=_dy, доля_мимо=_д,
                                 проскальзывание=0.0)
        if not _назад["ok"]:
            continue
        _круги.append(_назад["expected_out"] / _ушло)
    chk(f"круговой оборот посчитан на живых сделках CPMM ({len(_круги)} шт.)",
        len(_круги) >= 5, len(_круги))
    if _круги:
        # ЧТО ЭТО ЗА ПОЛОСА 0.90-1.10. Модель x*y=k воспроизводит настоящий
        # круговой оборот с точностью до 10 %: на 15 из 25 живых сделок оборот
        # выходит меньше уплаченного (комиссия пула дважды), на 10 -- чуть
        # больше, потому что движение хранилищ в тех транзакциях не сводится к
        # одному свопу по кривой (хранилище приросло больше, чем "вошло" в
        # кривую по её же арифметике). ЧЕМ ЭТО ОПАСНО И ЧЕМ НЕТ: завышенное
        # ожидание поднимает минимум выхода, и продажа откатывается целиком --
        # остаток подберёт Jupiter с ценовым полом (правило 4). Заниженное
        # ожидание опустило бы минимум, но его погрешность (до 10 %) заведомо
        # меньше проскальзывания групп (25-40 %), которое владелец задал сам.
        chk("модель воспроизводит живой круговой оборот в пределах 10 %",
            all(0.90 <= к <= 1.10 for к in _круги),
            (round(min(_круги), 4), round(max(_круги), 4),
             sum(1 for к in _круги if к < 1.0), len(_круги)))

    # --- ЖИВЫЕ РЕЗЕРВЫ: разбор ответа сети и отказы.
    def _rpc_рез(метод, парам):
        if метод != "getMultipleAccounts":
            raise RuntimeError(метод)
        return {"value": [{"data": {"parsed": {"info": {
            "tokenAmount": {"amount": str(1000 + и)}}}}}
            for и, _ in enumerate(парам[0])]}
    _рз = резервы_хранилищ(_rpc_рез, ["ХРАН1", "ХРАН2"])
    chk("живые резервы: остатки хранилищ разобраны целыми числами",
        _рз["ok"] and _рз["остатки"] == {"ХРАН1": 1000, "ХРАН2": 1001}, _рз)
    chk("живые резервы: без rpc_call -- отказ, а не ноль",
        резервы_хранилищ(None, ["ХРАН1"])["ok"] is False)
    chk("живые резервы: короткий ответ сети -- отказ",
        резервы_хранилищ(lambda м, п: {"value": []}, ["ХРАН1"])["ok"] is False)
    chk("живые резервы: нет остатка в ответе -- отказ",
        резервы_хранилищ(lambda м, п: {"value": [{"data": {}}]}, ["ХРАН1"])["ok"] is False)

    # --- ЗАКРЫТИЕ СЧЁТА: рента только владельцу, и он же подписант.
    _зик = инструкция_закрытия_счёта("So11111111111111111111111111111111111111112",
                                     C.EXECUTOR_WALLET, B.TOKEN_PROGRAM)
    _меты = [str(m.pubkey) for m in _зик.accounts]
    chk("CloseAccount: код 9, три счёта, рента И подпись -- наш кошелёк",
        bytes(_зик.data) == bytes([9]) and len(_меты) == 3
        and _меты[1] == C.EXECUTOR_WALLET and _меты[2] == C.EXECUTOR_WALLET
        and _зик.accounts[2].is_signer and str(_зик.program_id) == B.TOKEN_PROGRAM,
        (_меты, bytes(_зик.data).hex()))

    # --- фикстуры двух шагов: те же, что у теневой сборки второй сессии.
    htm = "HTmQz7My6MehV7bjhJ6jde8nDND1yvsz68d24LP7YgUQ"
    все = [x for f in sorted(B.SAMPLES_DIR.glob("*.json"))
           for x in json.loads(f.read_text(encoding="utf-8"))]
    leg1_tx = leg1_пул = None
    for x in все:
        for ix in B.all_instructions(x["tx"]):
            if ix.get("programId") != B.DLMM or len(ix["accounts"]) < 16:
                continue
            for qv in ix["accounts"][2:4]:
                tpl = B.extract_template(x["tx"], B.DLMM, qv)
                mv = B.mints_and_vaults(tpl, x["tx"]) if tpl.get("ok") else {}
                if (mv.get("quote_mint") == C.WSOL and mv.get("base_mint") == htm
                        and mv["base_vault"] == qv):
                    leg1_tx, leg1_пул = x["tx"], {"program": B.DLMM, "q_vault": qv}
            if leg1_tx:
                break
        if leg1_tx:
            break
    leg2 = [x for x in все if x.get("quote_mint") == htm and x.get("mint")]
    if not (leg1_tx and leg2):
        chk("образцов двух шагов в data/c2_pool_samples нет -- проверять нечем",
            False, (bool(leg1_tx), len(leg2)))
    else:
        адреса_таблиц = []

        def rpc_т(метод, парам):
            if метод == "getSignaturesForAddress":
                return [{"signature": C.first_signature(leg1_tx), "err": None}]
            if метод == "getTransaction":
                return leg1_tx
            if метод == "getMultipleAccounts":
                return {"value": [{"data": {"parsed": {"info": {
                    "addresses": адреса_таблиц}}}} for _ in парам[0]]}
            raise RuntimeError(метод)

        кэш = SB.LegCache({htm: leg1_пул}, rpc_т, allow_polling=True)
        кэш.refresh_all()
        обр = leg2[0]
        ЛАМП = 20_000_000
        наш = C.EXECUTOR_WALLET
        # ЦЕНА ЕДИНИЦЫ ВЫЧИСЛЕНИЙ у теневой сборки задана прямо (10 000), у нас
        # считается из приоритета: берём приоритет, дающий ровно её.
        приоритет = int(10_000 * 800_000 / 1_000_000)
        # НАЛОГ НУЛЬ -- НАРОЧНО: теневая сборка второй сессии налога переводов
        # не считает вовсе, поэтому байт в байт сравнивать можно только при
        # нуле. Что налог меняет числа -- проверяется ниже отдельно.
        наша = собрать(tx_источника=обр["tx"], источник=обр.get("source") or "",
                        минт=обр["mint"], наш_кошелёк=наш, лампорты=ЛАМП,
                        проскальзывание=0.35, cu_units=800_000,
                        приоритет_лампорты=приоритет, чаевые_лампорты=0,
                        кэш_ног=кэш, rpc_call=rpc_т, налог_котировки_bps=0)
        chk("двухшаговая сборка собралась и назвала оба пула",
            наша["ok"] is True and наша["pool_program"] and наша["leg1_pool_program"],
            наша.get("why_not"))
        chk("минимум выхода положителен и ниже ожидания",
            isinstance(наша.get("min_out"), int) and наша["min_out"] > 0
            and наша["min_out"] < (наша.get("expected_out") or 0),
            (наша.get("min_out"), наша.get("expected_out")))
        chk("минимум первого шага положителен и равен входу второго при нулевом налоге",
            (наша.get("leg1_min_out") or 0) > 0
            and наша.get("leg1_min_out") == наша.get("leg2_amount_in"),
            (наша.get("leg1_min_out"), наша.get("leg2_amount_in")))
        # БАЙТ В БАЙТ С ТЕНЕВОЙ СБОРКОЙ: без чаевых и без nonce расхождений быть
        # не должно -- иначе мы собрали не то, что проверено симуляцией.
        # ЭТАЛОН -- САМА ТЕНЕВАЯ СБОРКА ДВУХ ШАГОВ (_two_hop). shadow_build
        # байты не возвращает (он их симулирует и выбрасывает), поэтому сверяем
        # с тем, что собирает именно она.
        пул_э = C.identify_pool(обр["tx"], обр.get("source") or "", обр["mint"])
        прог_э = PP.pool_program(обр["tx"], пул_э["pool_vault"],
                                  SB._labels())["pool_program"]
        tpl_э = B.extract_template(обр["tx"], прог_э, пул_э["pool_vault"])
        mv_э = B.mints_and_vaults(tpl_э, обр["tx"])
        рес_э: dict = {}
        тень = SB._two_hop(рес_э, обр["tx"], tpl_э, mv_э["quote_mint"], наш, ЛАМП,
                            rpc_т, кэш, 0.35, 800_000, 10_000, time.perf_counter())
        chk("теневая сборка двух шагов дала транзакцию",
            bool(тень and тень.get("tx_base64")), рес_э.get("why_not"))
        chk("наша транзакция БАЙТ В БАЙТ совпала с теневой (без чаевых и nonce)",
            наша.get("tx_base64") and (тень or {}).get("tx_base64")
            and наша["tx_base64"] == тень["tx_base64"],
            (наша.get("size"), (тень or {}).get("size")))
        chk("минимум выхода у обеих сборок один",
            наша.get("min_out") == рес_э.get("min_out"),
            (наша.get("min_out"), рес_э.get("min_out")))
        # ЧАЕВЫЕ И NONCE меняют транзакцию -- и это должно быть видно.
        с_чаевыми = собрать(tx_источника=обр["tx"], источник=обр.get("source") or "",
                             минт=обр["mint"], наш_кошелёк=наш, лампорты=ЛАМП,
                             cu_units=800_000, приоритет_лампорты=приоритет,
                             чаевые_адрес="4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE",
                             чаевые_лампорты=1_000_000, кэш_ног=кэш, rpc_call=rpc_т,
                             налог_котировки_bps=0)
        chk("с чаевыми транзакция другая, адрес и сумма чаевых названы",
            с_чаевыми["ok"] and с_чаевыми["tx_base64"] != наша["tx_base64"]
            and с_чаевыми["tips_total_lamports"] == 1_000_000,
            (с_чаевыми.get("tip_account"), с_чаевыми.get("tips_total_lamports")))
        # NONCE ПРИХОДИТ ДВУМЯ ВИДАМИ, И ОДИН ИЗ НИХ 02.10 СТОИЛ СИГНАЛА. Полоса
        # держит значение СЛОВАРЁМ (нонс_для_сделки), сборка берёт нонс[0]: по
        # сигналу Xk9onqHkpULD в записи вышло "сборка двух шагов: KeyError: 0".
        # Проверка повторяет ровно это -- тем же живым образцом, без сети.
        НС = {"ok": True, "account": "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d",
              "authority": наш, "blockhash": C.first_signature(leg1_tx)[:43],
              "warm": True}
        НП = (НС["account"], НС["authority"])

        def _нонсом(н):
            return собрать(tx_источника=обр["tx"], источник=обр.get("source") or "",
                            минт=обр["mint"], наш_кошелёк=наш, лампорты=ЛАМП,
                            cu_units=800_000, приоритет_лампорты=приоритет,
                            чаевые_лампорты=0, кэш_ног=кэш, rpc_call=rpc_т,
                            налог_котировки_bps=0, нонс=н)

        с_парой, со_словарём = _нонсом(НП), _нонсом(НС)
        chk("с nonce ПАРОЙ сборка идёт и аккаунт nonce назван",
            с_парой["ok"] and с_парой["nonce_account"] == НС["account"]
            and с_парой["tx_base64"] != наша["tx_base64"],
            (с_парой.get("why_not"), с_парой.get("nonce_account")))
        chk("СЛОВАРЬ полосы даёт ТУ ЖЕ транзакцию, что пара, а не KeyError: 0",
            со_словарём["ok"]
            and со_словарём["tx_base64"] == с_парой["tx_base64"]
            and со_словарём["nonce_account"] == НС["account"],
            (со_словарём.get("why_not"), со_словарём.get("nonce_account")))
        бред = _нонсом(7)
        chk("непонятный вид nonce -- отказ СЛОВАМИ до сборки, а не исключение "
            "и не транзакция без advance_nonce",
            бред["ok"] is False and бред["tx_base64"] is None
            and "nonce" in (бред.get("why_not") or ""), бред.get("why_not"))
        без_распорядителя = _нонсом({"account": НС["account"]})
        chk("в словаре nonce нет распорядителя -- отказ словами: его не выдумать",
            без_распорядителя["ok"] is False
            and "распорядител" in (без_распорядителя.get("why_not") or ""),
            без_распорядителя.get("why_not"))
        # СТРОКА СБОЯ ОБЯЗАНА НАЗЫВАТЬ КЛЮЧ И МЕСТО: по записи "KeyError: 0"
        # 02.10 ни того, ни другого было не видно, и причину пришлось искать
        # прогоном вручную. Исключение берётся настоящее, с настоящим traceback.
        try:
            {"а": 1}[0]
            строка_сбоя = "исключения не было"
        except KeyError as сбой:
            строка_сбоя = след_сбоя(сбой)
        chk("след сбоя называет и ключ (repr), и файл:строку падения",
            "KeyError(0)" in строка_сбоя
            and "bloom_lane_two_step.py:" in строка_сбоя, строка_сбоя)
        # ПРЕДЕЛ КОМИССИИ ПУЛА: выше потолка -- отказ до всякой подписи.
        # Потолок ставим ОТНОСИТЕЛЬНО замеренной доли этого самого образца:
        # ставить 0 бессмысленно, когда вся трата источника легла в хранилище
        # пула (доля ровно 0) -- такой пул отказывать не за что.
        _доля_обр = наша.get("pool_fee_share")
        _потолок_т = (float(_доля_обр) / 2) if (_доля_обр or 0) > 0 else 0.0
        отказ_ком = собрать(tx_источника=обр["tx"], источник=обр.get("source") or "",
                             минт=обр["mint"], наш_кошелёк=наш, лампорты=ЛАМП,
                             cu_units=800_000, приоритет_лампорты=приоритет,
                             чаевые_лампорты=0, кэш_ног=кэш, rpc_call=rpc_т,
                             налог_котировки_bps=0, потолок_комиссии=_потолок_т)
        if _доля_обр is None:
            # Замерить нечем (хранилищ нет в балансах сделки): отказывать по
            # неизвестному числу нельзя, но и молчать нельзя -- доля названа
            # в ответе как неизвестная.
            chk("доля комиссии пула шага 2 не замеряется -- потолок не отказывает, "
                "и доля названа как неизвестная",
                отказ_ком["ok"] is True and отказ_ком.get("pool_fee_share") is None,
                (отказ_ком.get("ok"), отказ_ком.get("pool_fee_share")))
        elif float(_доля_обр) > 0:
            chk(f"комиссия пула {float(_доля_обр) * 100:.3f} % выше потолка "
                f"{_потолок_т * 100:.3f} % -- отказ со словами",
                отказ_ком["ok"] is False
                and "потолка" in (отказ_ком.get("why_not") or ""),
                (отказ_ком.get("why_not"), отказ_ком.get("pool_fee_share")))
        else:
            chk("вся трата источника легла в хранилище пула (доля 0) -- потолок "
                "не отказывает, и доля названа нулём, а не неизвестной",
                отказ_ком["ok"] is True and отказ_ком.get("pool_fee_share") == 0.0,
                (отказ_ком.get("ok"), отказ_ком.get("pool_fee_share")))
        # ЗАМЕРЕННАЯ ДОЛЯ ВИДНА В ОТВЕТЕ ВСЕГДА: раньше на этом типе пула она
        # была None, и потолок не срабатывал ни разу.
        chk("доля комиссии пула шага 2 названа числом в ответе сборки",
            isinstance(наша.get("pool_fee_share"), float),
            наша.get("pool_fee_share"))
        # КОТИРОВКА SOL -- не наш путь.
        sol_обр = next((x for x in все if x.get("quote_mint") == C.WSOL
                        and x.get("mint")), None)
        if sol_обр is not None:
            sol_р = собрать(tx_источника=sol_обр["tx"],
                             источник=sol_обр.get("source") or "",
                             минт=sol_обр["mint"], наш_кошелёк=наш, лампорты=ЛАМП,
                             cu_units=800_000, приоритет_лампорты=приоритет,
                             чаевые_лампорты=0, кэш_ног=кэш, rpc_call=rpc_т,
                             налог_котировки_bps=0)
            chk("котировка SOL двухшаговым путём не берётся -- отказ со словами",
                sol_р["ok"] is False
                and ("одношаговая" in (sol_р.get("why_not") or "")
                     or "не покрыт" in (sol_р.get("why_not") or "")),
                sol_р.get("why_not"))
        # НАЛОГ ПЕРЕВОДОВ МЕНЯЕТ ЧИСЛА, И ЭТО ВИДНО. Котировочный токен образца
        # как раз на Token-2022 -- ровно случай GP из плана владельца.
        с_налогом = собрать(tx_источника=обр["tx"], источник=обр.get("source") or "",
                             минт=обр["mint"], наш_кошелёк=наш, лампорты=ЛАМП,
                             cu_units=800_000, приоритет_лампорты=приоритет,
                             чаевые_лампорты=0, кэш_ног=кэш, rpc_call=rpc_т,
                             налог_котировки_bps=300)
        chk("налог 300 bps: до пула второго шага доходит меньше, минимум ниже",
            с_налогом["ok"] is True
            and с_налогом["leg2_amount_in"] < наша["leg2_amount_in"]
            and с_налогом["leg2_to_pool"] < с_налогом["leg2_amount_in"]
            and с_налогом["min_out"] < наша["min_out"],
            {к: с_налогом.get(к) for к in ("leg2_amount_in", "leg2_to_pool",
                                            "min_out", "quote_fee_bps")})
        chk("минимум первого шага налогом НЕ уменьшается: пул1 отдаёт до налога",
            с_налогом["leg1_min_out"] == наша["leg1_min_out"],
            (с_налогом.get("leg1_min_out"), наша.get("leg1_min_out")))
        # КОТИРОВОЧНЫЙ ТОКЕН ОБРАЗЦА -- НА TOKEN-2022, и с 28.09 сборка на нём
        # не отказывается, а считает с запасом: ставка не прочитана (сеть с пути
        # покупки убрана), запас взят с потолка налогового маршрута. Прежде эта
        # же проверка требовала отказа.
        без_ставки = собрать(tx_источника=обр["tx"], источник="", минт=обр["mint"],
                              наш_кошелёк=наш, лампорты=ЛАМП, кэш_ног=кэш,
                              rpc_call=rpc_т)
        chk(f"на настоящем Token-2022 образце сборка идёт с запасом "
            f"({без_ставки.get('quote_fee_bps')} bps, откуда "
            f"{без_ставки.get('quote_fee_from')!r})",
            без_ставки["ok"] is True
            and без_ставки.get("quote_fee_bps") == ЗАПАС_НАЛОГА_BPS
            and "запас" in (без_ставки.get("quote_fee_from") or ""),
            {к: без_ставки.get(к) for к in ("ok", "why_not", "quote_fee_bps",
                                             "quote_fee_from")})
        chk("и запас режет минимум выхода сильнее, чем нулевой налог: "
            "считаем по худшему из допустимых",
            без_ставки.get("min_out") is not None
            and наша.get("min_out") is not None
            and без_ставки["min_out"] <= наша["min_out"],
            (без_ставки.get("min_out"), наша.get("min_out")))

        # БЕЗ КЭША -- отказ, а не исключение.
        без = собрать(tx_источника=обр["tx"], источник="", минт=обр["mint"],
                       наш_кошелёк=наш, лампорты=ЛАМП, приоритет_лампорты=приоритет,
                       чаевые_лампорты=0, кэш_ног=None)
        chk("без кэша шаблонов -- отказ со словами, без исключения",
            без["ok"] is False and "кэша" in (без.get("why_not") or ""),
            без.get("why_not"))
        # --- ПРОДАЖА НА НАСТОЯЩЕЙ ФИКСТУРЕ: отказы там, где собирать нельзя.
        # Пул котировки этой фикстуры -- DLMM, и продажа его НЕ берёт: у DLMM
        # минимум выхода по резервам не выдаётся вовсе, а без минимума продавать
        # нельзя. Остаток в этом случае подберёт Jupiter (правило 4).
        пр_нога = собрать_продажу(tx_источника=обр["tx"],
                                   источник=обр.get("source") or "",
                                   минт=обр["mint"], наш_кошелёк=наш,
                                   токенов=1_000_000, кэш_ног=кэш, rpc_call=rpc_т,
                                   налог_котировки_bps=0)
        chk("продажа: пул котировки не CPMM -- отказ со словами и с Jupiter в "
            "причине, а не сборка наугад",
            пр_нога["ok"] is False
            and "CPMM" in (пр_нога.get("why_not") or "")
            and "Jupiter" in (пр_нога.get("why_not") or ""),
            пр_нога.get("why_not"))
        chk("продажа: тип пула токена назван в ответе даже при отказе",
            пр_нога.get("pool_program") is not None, пр_нога.get("pool_program"))
        пр_ноль = собрать_продажу(tx_источника=обр["tx"],
                                   источник=обр.get("source") or "",
                                   минт=обр["mint"], наш_кошелёк=наш, токенов=0,
                                   кэш_ног=кэш, rpc_call=rpc_т,
                                   налог_котировки_bps=0)
        chk("продажа: нулевое количество токена -- отказ до всякой сети",
            пр_ноль["ok"] is False and "положительное" in (пр_ноль.get("why_not") or ""),
            пр_ноль.get("why_not"))
        пр_без_кэша = собрать_продажу(tx_источника=обр["tx"],
                                       источник=обр.get("source") or "",
                                       минт=обр["mint"], наш_кошелёк=наш,
                                       токенов=1_000_000, кэш_ног=None,
                                       rpc_call=rpc_т, налог_котировки_bps=0)
        chk("продажа: без кэша шаблонов -- отказ со словами",
            пр_без_кэша["ok"] is False and "кэш" in (пр_без_кэша.get("why_not") or ""),
            пр_без_кэша.get("why_not"))
        if sol_обр is not None:
            пр_sol = собрать_продажу(tx_источника=sol_обр["tx"],
                                      источник=sol_обр.get("source") or "",
                                      минт=sol_обр["mint"], наш_кошелёк=наш,
                                      токенов=1_000_000, кэш_ног=кэш, rpc_call=rpc_т,
                                      налог_котировки_bps=0)
            chk("продажа: котировка пула SOL -- двухшаговым путём не берётся",
                пр_sol["ok"] is False
                and ("одношаговая" in (пр_sol.get("why_not") or "")
                     or "CPMM" in (пр_sol.get("why_not") or "")),
                пр_sol.get("why_not"))
        пр_чужой = собрать_продажу(tx_источника=обр["tx"],
                                    источник=обр.get("source") or "",
                                    минт=C.WSOL, наш_кошелёк=наш,
                                    токенов=1_000_000, кэш_ног=кэш, rpc_call=rpc_т,
                                    налог_котировки_bps=0)
        chk("продажа: минт не из этого пула -- отказ, а не продажа чужого токена",
            пр_чужой["ok"] is False, пр_чужой.get("why_not"))

    плохо = [(и, ф) for и, ок, ф in проверки if not ок]
    for имя, ок, факт in проверки:
        print(f"  [{'ok  ' if ок else 'ПЛОХО'}] {имя}"
              + ("" if ок else f" -- факт: {факт}"))
    print(f"самопроверка двухшаговой сборки: {len(проверки) - len(плохо)}/"
          f"{len(проверки)} пройдено")
    return 1 if плохо else 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
