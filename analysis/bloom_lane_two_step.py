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


def налог_известен(программа_токена: str | None, налог_bps) -> dict:
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


def собрать(*, tx_источника: dict, источник: str, минт: str, наш_кошелёк: str,
            лампорты: int, проскальзывание: float = 0.35,
            cu_units: int = 800_000, приоритет_лампорты: int = 1_000_000,
            чаевые_лампорты: int = 1_000_000, чаевые_списком: list | None = None,
            чаевые_адрес: str | None = None, семя: str | None = None,
            нонс: tuple | None = None, rpc_call=None, кэш_ног=None,
            налог_котировки_bps=None, налог_минта=None,
            потолок_комиссии: float | None = None) -> dict:
    """Одна транзакция: SOL -> котировочный -> токен. Без подписи и отправки."""
    из_ = {"ok": False, "why_not": None, "route": "two_step", "steps": 2,
            "pool_program": None, "leg1_pool_program": None, "quote_mint": None,
            "min_out": None, "expected_out": None, "leg1_min_out": None,
            "leg2_amount_in": None, "quote_fee_bps": None,
            "tx_base64": None, "size": None, "build_ms": None,
            "tip_account": None, "tips": None, "tips_total_lamports": None}
    t0 = time.perf_counter()
    if not isinstance(лампорты, int) or лампорты <= 0:
        из_["why_not"] = f"размер не положительное целое: {лампорты!r}"
        return из_
    if кэш_ног is None:
        из_["why_not"] = "кэша шаблонов первого шага нет -- собирать не из чего"
        return из_
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
        из_["why_not"] = f"сборка двух шагов: {type(exc).__name__}: {str(exc)[:160]}"
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
    н2 = налог_известен(TOKEN_2022, None)
    chk("Token-2022 без известного налога -- отказ со словами",
        н2["ok"] is False and "неизвестен" in (н2["почему"] or ""), н2)
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
        chk("котировочный токен образца действительно на Token-2022 -- значит "
            "строгое правило проверено на настоящем случае",
            наша.get("quote_fee_bps") == 0
            and собрать(tx_источника=обр["tx"], источник="", минт=обр["mint"],
                         наш_кошелёк=наш, лампорты=ЛАМП, кэш_ног=кэш,
                         rpc_call=rpc_т)["ok"] is False, "")

        # БЕЗ КЭША -- отказ, а не исключение.
        без = собрать(tx_источника=обр["tx"], источник="", минт=обр["mint"],
                       наш_кошелёк=наш, лампорты=ЛАМП, приоритет_лампорты=приоритет,
                       чаевые_лампорты=0, кэш_ног=None)
        chk("без кэша шаблонов -- отказ со словами, без исключения",
            без["ok"] is False and "кэша" in (без.get("why_not") or ""),
            без.get("why_not"))

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
