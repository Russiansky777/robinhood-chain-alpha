#!/usr/bin/env python3
"""Ночная задача владельца: A -- есть ли деньги в копировании источников на
0.2 SOL, и D -- какие правила пропуска сигналов их прибавляют. Только
арифметика по уже собранному кэшу (data/crowd_metric_*.json,
data/solana_transfer_fee_audit.json, data/solana_tax_groups.json,
data/c2_leg_pools_*.json) -- НИ ОДНОГО обращения к сети. Ключа Helius в
этом контейнере нет, поэтому даже если бы модуль хотел сходить в цепь,
идти было бы некуда; --self-test работает на синтетике, реальный прогон --
на переданных путях к кэшу.

ПОЧЕМУ ВХОДЫ (i)/(ii)/(iii) СОВПАДАЮТ ЧИСЛОМ. Задача просит цену входа
"сразу за источником / голова S+1 / голова S+2" из price_0/spot_after/
impact_source. В кэше только ДВЕ реальные цены ПОСЛЕ сделки источника:
spot_after (спот сразу после неё, t=0) и price_30s/price_60s (t=+30/+60с).
Цены на границе конкретных слотов S+1/S+2 (доли секунды после источника)
в кэше нет вообще -- строить её значило бы интерполировать между t=0 и
t=+30с, а это запрещено условием задачи. Поэтому все три входа используют
ОДНУ И ТУ ЖЕ точку -- spot_after -- как ОПТИМИСТИЧНУЮ НИЖНЮЮ ГРАНИЦУ: цена
на S+1/S+2 не может быть лучше spot_after (после источника цена только
растёт вдоль кривой) и почти наверняка хуже (в блок между источником и
нами могли влезть другие покупатели -- failed_same_block_tx/wallets это
подтверждает, но БЕЗ цены их сделок эту разницу в SOL не перевести, не
выдумывая число). Вывод "(i)==(ii)==(iii) на этих данных" -- честный
результат, а не недоработка: он прямо говорит владельцу, что нужен захват
цены по слотам, если разница между входами важна.

ПОЧЕМУ ТАЙМЕР 28.8 С СЧИТАЕТСЯ ПО ТОЧКЕ 30 С. В кэше только точки
t=0/+30/+60с. 28.8 с -- ближайшая реальная точка -- t=+30с (growth_after_30s),
БЕЗ интерполяции между 30 и 60 с. Разница (1.2 с, ~4% таймера) -- известная
и явно объявленная погрешность метода, а не спрятанная.

ИЗДЕРЖКИ. Bloom 1% на сторону и чаевые 0.002 SOL на сторону -- числа
владельца из условия задачи. Комиссия сети -- протокольная константа
Solana, 5000 lamport за подпись (тот же факт, что и в
c2_followers_growth.py: "фактическая priority fee = meta.fee - 5000 *
число подписей"). Налог маршрута -- ставка_комиссии_bps таргет-минта и,
если катировочный минт сам налоговый, её же для него -- оба взяты из
data/solana_transfer_fee_audit.json (поле "минты", по адресу минта).
КОМИССИЯ ПУЛА (своп-fee AMM) в кэше и в репозитории НЕ НАЙДЕНА как ставка
bps по pool_program (в dex_labels.json только ярлыки программ, не ставки)
-- поэтому она НЕ включена в чистый результат (pool_fee_bps=0 по
умолчанию) и это явно объявлено ниже как пробел данных, а не "пул
бесплатный": реальный чистый результат ниже посчитанного на неизвестную
величину своп-комиссии.

НАЛОГ = "НЕИЗВЕСТНО" -- ЭТО НЕ НОЛЬ. Если минта нет в справочнике
transfer_fee_audit, его налоговый статус неизвестен -- POI считать его
безналоговым запрещено (могли бы получиться завышенные "прибыльные"
сделки на самом деле налоговых токенов). Такие сделки помечаются missing
и не входят ни в среднее, ни в сумму, ни в долю в плюс.

Самопроверка (--self-test) -- только синтетика, без сети и без файлов.
Обычный прогон -- python3 analysis/night_edge_model.py --crowd PATH
--tax-groups PATH --out data/night_edge.json (пути по умолчанию -- в
data/, реальный кэш этой ночи лежит в scratchpad и передаётся явно).
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data"

# --------------------------------------------------------------------- константы

НАШ_РАЗМЕР_SOL = 0.2            # задача владельца: моделируем сделку на 0.2 SOL
НАШ_ТАЙМЕР_S = 28.8             # наш таймер выхода
BLOOM_FEE_ДОЛЯ = 0.01           # 1% на сторону -- слово владельца в условии ночи
ЧАЕВЫЕ_SOL_НА_СТОРОНУ = 0.002   # слово владельца в условии ночи
СЕТЕВАЯ_КОМИССИЯ_SOL = 0.000005  # 5000 lamport / подпись -- база Solana, тот же
                                  # факт что и в c2_followers_growth.py

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
КОТИРОВКИ_SOL = {WSOL}          # прямая котировка к SOL; USDC/USDT -- отдельная
                                 # (не-SOL) котировка, но сами они не Token-2022

SEED = 20260925                 # дата ночи; фиксирован ради воспроизводимости
N_BOOT = 2000                   # задача просит не меньше 2000
ГОРИЗОНТЫ = ("t28_8", "t10s", "t60s", "t5m", "до_первой_продажи_источника")
ВХОДЫ = ("i_сразу_за_источником", "ii_голова_S_plus_1", "iii_голова_S_plus_2")
ПОРОГ_ИСТОЧНИКА_ДЛЯ_РАЗБИВКИ = 3  # то же число, что "порог_источника" в
                                   # data/solana_tax_groups.json -- не выдумано,
                                   # взято из уже принятого в репозитории решения


def _default(glob_pat: str, fallback: str) -> Path:
    cand = sorted(DATA.glob(glob_pat))
    return cand[-1] if cand else DATA / fallback


DEFAULT_CROWD = _default("crowd_metric_2*.json", "crowd_metric.json")
DEFAULT_TAX_GROUPS = DATA / "solana_tax_groups.json"
DEFAULT_TRANSFER_FEE_AUDIT = DATA / "solana_transfer_fee_audit.json"
DEFAULT_LEG_POOLS = _default("c2_leg_pools_2*.json", "c2_leg_pools.json")
DEFAULT_OUT = DATA / "night_edge.json"


# --------------------------------------------------------------------- загрузка

def parse_iso(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def load_json(path) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_crowd_trades(path) -> tuple[list, dict]:
    """per_source[].trades[] -> плоский список с привязкой к источнику.

    Пустой/отсутствующий файл -- пустой список и пустая метка, БЕЗ исключения:
    молчание входа не должно ронять весь прогон (самопроверка "пустой вход не
    падает" именно про это)."""
    d = load_json(path)
    if not d:
        return [], {}
    out = []
    for ps in d.get("per_source") or []:
        addr = ps.get("address")
        remark = ps.get("remark") or addr
        for t in ps.get("trades") or []:
            row = dict(t)
            row["source_address"] = addr
            row["source_remark"] = remark
            out.append(row)
    meta = {"window_from_utc": d.get("window_from_utc"), "window_to_utc": d.get("window_to_utc"),
            "n_sources": len(d.get("per_source") or []), "n_trades_raw": len(out),
            "generated_utc": d.get("generated_utc")}
    return out, meta


def load_mint_tax(path) -> dict:
    d = load_json(path)
    return (d or {}).get("минты") or {}


def load_leg_pool_depth(path) -> dict:
    d = load_json(path)
    pools = (d or {}).get("pools") or {}
    return {k: v.get("sol_depth") for k, v in pools.items() if isinstance(v, dict)}


def mint_tax_info(mint: str | None, minty: dict) -> tuple[bool, bool | None, int | None]:
    """(известно_ли, налоговый_ли, ставка_bps). известно_ли=False -- минта нет
    в справочнике: статус НЕ равен "без налога", это отдельный, честный
    "не знаем" (см. докстринг модуля)."""
    if not mint or mint not in minty:
        return False, None, None
    info = minty[mint]
    taxable = bool(info.get("таксируемый"))
    bps = info.get("ставка_комиссии_bps") if taxable else 0
    return True, taxable, bps


# --------------------------------------------------------------------- сигнал

def hops_proxy(quote_mint: str | None, split_route: bool) -> int:
    """ПРОКСИ числа шагов маршрута -- в кэше нет прямого поля "число хопов".
    1 -- котировка сама SOL (прямой пул); 2 -- котировка другой минт одним
    пулом; 3 -- Jupiter/агрегатор отдал split_route=True (путь собран из >=2
    пулов сразу, это не меньше 3 передач на круг). Это ОПРЕДЕЛЕНИЕ по
    имеющимся булевым полям, а не измеренное число -- помечено в выводе как
    "proxy"."""
    if split_route:
        return 3
    if quote_mint in КОТИРОВКИ_SOL:
        return 1
    return 2


def build_signal(raw: dict, minty: dict, depth_by_quote: dict) -> dict:
    mint = raw.get("mint")
    quote_mint = raw.get("quote_mint")
    quote_is_sol = quote_mint in КОТИРОВКИ_SOL
    t_known, t_taxable, t_bps = mint_tax_info(mint, minty)
    if quote_is_sol:
        q_known, q_taxable, q_bps = True, False, 0
    else:
        q_known, q_taxable, q_bps = mint_tax_info(quote_mint, minty)

    def f(key):
        v = raw.get(key)
        return float(v) if v is not None else None

    return {
        "source_address": raw.get("source_address"), "source_remark": raw.get("source_remark"),
        "signature": raw.get("signature"), "slot": raw.get("slot"),
        "block_time": raw.get("block_time"), "block_time_utc": raw.get("block_time_utc"),
        "mint": mint, "quote_mint": quote_mint, "quote_is_sol": quote_is_sol,
        "spend_sol_equiv": f("spend_sol_equiv"),
        "price_0": f("price_0"), "spot_after": f("spot_after"), "impact_source": f("impact_source"),
        "growth_30s": f("growth_30s"), "growth_60s": f("growth_60s"),
        "growth_after_30s": f("growth_after_30s"), "growth_after_60s": f("growth_after_60s"),
        "crowd_30s": raw.get("crowd_30s"),
        "window_tx_30s": raw.get("window_tx_30s"), "window_tx_60s": raw.get("window_tx_60s"),
        "failed_same_block_tx": raw.get("failed_same_block_tx"),
        "pool_program": raw.get("pool_program"), "split_route": bool(raw.get("split_route")),
        "target_tax_known": t_known, "target_taxable": t_taxable, "target_tax_bps": t_bps,
        "quote_tax_known": q_known, "quote_taxable": q_taxable, "quote_tax_bps": q_bps,
        "leg_pool_sol_depth": None if quote_is_sol else depth_by_quote.get(quote_mint),
        "hops_proxy": hops_proxy(quote_mint, raw.get("split_route")),
        "плохая_серия": "неизвестно",
    }


def аннотировать_серию(signals: list, окно_ч: float = 24.0, мин_историй: int = 2,
                        порог_доля: float = 0.5) -> list:
    """"Плохая серия источника за 24ч": доля ПРЕДЫДУЩИХ (по времени) сделок
    того же источника за 24 ч перед текущей, где growth_30s<1 (а если его
    нет -- growth_60s<1). Меньше мин_историй таких сделок -- "неизвестно"
    (недостаточно истории, это не "хорошая серия"). Порог "большинство"
    (0.5) и окно 24ч -- определение из формулировки владельца, а не
    подобранное на обучающей половине число, поэтому утечки данных
    train->test тут нет: правило одинаково на обеих половинах."""
    by_source: dict = {}
    for s in signals:
        by_source.setdefault(s["source_address"], []).append(s)
    for lst in by_source.values():
        lst.sort(key=lambda s: s.get("block_time") or 0)
        for i, s in enumerate(lst):
            t0 = s.get("block_time")
            if t0 is None:
                s["плохая_серия"] = "неизвестно"
                continue
            history = [h for h in lst[:i] if h.get("block_time") is not None
                       and 0 <= t0 - h["block_time"] <= окно_ч * 3600]

            def лузер(h):
                g = h["growth_30s"] if h["growth_30s"] is not None else h["growth_60s"]
                return None if g is None else (g < 1.0)

            исходы = [x for x in (лузер(h) for h in history) if x is not None]
            if len(исходы) < мин_историй:
                s["плохая_серия"] = "неизвестно"
            else:
                s["плохая_серия"] = "да" if (sum(исходы) / len(исходы)) > порог_доля else "нет"
    return signals


# --------------------------------------------------------------------- цена и издержки

def entry_price_for(sig: dict) -> float | None:
    """Единственная реальная цена ПОСЛЕ источника -- spot_after; см. докстринг
    модуля про (i)/(ii)/(iii)."""
    return sig["spot_after"]


def horizon_growth(sig: dict, horizon: str) -> tuple[float | None, str | None]:
    if horizon == "t28_8":
        g = sig["growth_after_30s"]
        return g, (None if g is not None else
                    "нет growth_after_30s (нет пары spot_after/price_30s или тип пула "
                    "не даёт чистого спота) -- ближайшая к 28.8с реальная точка недоступна")
    if horizon == "t60s":
        g = sig["growth_after_60s"]
        return g, (None if g is not None else "нет growth_after_60s в кэше для этой сделки")
    if horizon == "t10s":
        return None, "в кэше нет цены на 10 с (только точки 0/30/60 с) -- не интерполируется"
    if horizon == "t5m":
        return None, "в кэше нет цены на 5 мин (только точки 0/30/60 с) -- не интерполируется"
    if horizon == "до_первой_продажи_источника":
        return None, "в кэше нет времени/цены первой продажи источника (только его покупки)"
    raise ValueError(horizon)


def net_pnl_sol(entry_price: float | None, growth_after: float | None, sig: dict,
                 size_sol: float = НАШ_РАЗМЕР_SOL, bloom_fee: float = BLOOM_FEE_ДОЛЯ,
                 tip: float = ЧАЕВЫЕ_SOL_НА_СТОРОНУ, network_fee: float = СЕТЕВАЯ_КОМИССИЯ_SOL,
                 pool_fee_bps: float = 0.0) -> tuple[float | None, str | None]:
    """Чистый round-trip результат в SOL для размера size_sol.

    Издержки на каждой стороне снимаются С ТОЙ СУММЫ, которая реально идёт в
    эту сторону: на покупке -- с size_sol, на продаже -- с того, что осталось
    ПОСЛЕ покупки и роста цены. Так издержка учтена РОВНО ОДИН РАЗ на сторону
    (не дважды -- см. самопроверку "издержки не дублируются").

    Налог маршрута = bps таргет-минта (если известен и налоговый) + bps
    котировочного минта, если сама котировка -- налоговый промежуточный
    минт. Комиссия пула pool_fee_bps=0 по умолчанию -- в кэше нет ставок по
    pool_program (см. докстринг модуля), это объявленный пробел, а не 0%
    "по факту"."""
    if entry_price is None or entry_price <= 0:
        return None, "нет spot_after -- цена входа неизвестна"
    if growth_after is None:
        return None, "нет цены на этом горизонте"
    if not sig["target_tax_known"]:
        return None, "налоговый статус минта неизвестен (нет в transfer_fee_audit)"
    if not sig["quote_is_sol"] and not sig["quote_tax_known"]:
        return None, "налоговый статус котировочного минта неизвестен"

    налог_доля = 0.0
    if sig["target_taxable"]:
        налог_доля += (sig["target_tax_bps"] or 0) / 10000.0
    if (not sig["quote_is_sol"]) and sig["quote_taxable"]:
        налог_доля += (sig["quote_tax_bps"] or 0) / 10000.0
    издержка_доля = bloom_fee + (pool_fee_bps / 10000.0) + налог_доля

    издержка_покупки = size_sol * издержка_доля + tip + network_fee
    позиция_после_покупки = size_sol - издержка_покупки
    if позиция_после_покупки <= 0:
        return -size_sol, None  # издержки съели всю ставку -- честный полный убыток, не NaN

    валовая_выручка = позиция_после_покупки * growth_after
    издержка_продажи = валовая_выручка * издержка_доля + tip + network_fee
    чистая_выручка = валовая_выручка - издержка_продажи
    return чистая_выручка - size_sol, None


def compute_a1(signals: list) -> list:
    """По каждому сигналу -- net_sol/missing/why_not на каждом горизонте.
    Один и тот же расчёт обслуживает все три входа (i)/(ii)/(iii), см.
    докстринг модуля."""
    rows = []
    for sig in signals:
        entry = entry_price_for(sig)
        row = {"signature": sig["signature"], "source": sig["source_remark"],
               "source_address": sig["source_address"], "block_time": sig.get("block_time"),
               "block_time_utc": sig["block_time_utc"], "mint": sig["mint"],
               "entry_price_spot_after": entry, "нет_spot_after": entry is None}
        for horizon in ГОРИЗОНТЫ:
            growth, why = horizon_growth(sig, horizon)
            if growth is None:
                net, why2 = None, why
            else:
                net, why2 = net_pnl_sol(entry, growth, sig)
            row[horizon] = {"net_sol": net, "missing": net is None, "why_not": why2 or why}
        rows.append(row)
    return rows


# --------------------------------------------------------------------- статистика
# (свод/без_n_лучших/бутстрэп_среднего -- та же схема, что уже используется в
# analysis/solana_tax_robustness.py для соседней задачи по налогу; повторяю
# методику, а не изобретаю новую, чтобы числа были сравнимы между модулями)

def свод(rows: list, horizon: str) -> dict:
    vals = [r[horizon]["net_sol"] for r in rows if r[horizon]["net_sol"] is not None]
    if not vals:
        return {"сделок": 0, "сделок_без_цифры": len(rows), "среднее_sol": None,
                "медиана_sol": None, "доля_в_плюс": None, "сумма_sol": None}
    плюс = sum(1 for v in vals if v > 0)
    return {"сделок": len(vals), "сделок_без_цифры": len(rows) - len(vals),
            "среднее_sol": round(sum(vals) / len(vals), 6),
            "медиана_sol": round(statistics.median(vals), 6),
            "доля_в_плюс": round(плюс / len(vals), 4),
            "сумма_sol": round(sum(vals), 6)}


def без_n_лучших(rows: list, horizon: str, n: int) -> list:
    known = [r for r in rows if r[horizon]["net_sol"] is not None]
    return sorted(known, key=lambda r: r[horizon]["net_sol"], reverse=True)[n:]


def бутстрэп_среднего(rows: list, horizon: str, n_boot: int = N_BOOT, seed: int = SEED) -> dict:
    """95%-интервал для СРЕДНЕГО результата на сделку, ресэмплинг с
    возвращением. Свой Random(seed) -- не глобальный random -- ради полной
    воспроизводимости при том же входе (проверяется в self_test)."""
    vals = [r[horizon]["net_sol"] for r in rows if r[horizon]["net_sol"] is not None]
    if len(vals) < 2:
        return {"почему": "меньше двух сделок с известным результатом -- интервал не строится"}
    rnd = random.Random(seed)
    n = len(vals)
    средние = []
    for _ in range(n_boot):
        средние.append(sum(vals[rnd.randrange(n)] for _ in range(n)) / n)
    средние.sort()
    lo = средние[int(0.025 * n_boot)]
    hi = средние[int(0.975 * n_boot) - 1]
    факт = sum(vals) / n
    return {"сделок": n, "среднее_sol": round(факт, 6),
            "интервал_95_низ_sol": round(lo, 6), "интервал_95_верх_sol": round(hi, 6),
            "выборок": n_boot, "сид": seed, "ноль_внутри_интервала": bool(lo <= 0 <= hi)}


def вывод_по_интервалу(boot: dict) -> str:
    if "почему" in boot:
        return f"недостаточно данных: {boot['почему']}"
    lo, hi = boot["интервал_95_низ_sol"], boot["интервал_95_верх_sol"]
    if lo > 0:
        return "край есть"
    if hi < 0:
        return "края нет (статистически ниже нуля)"
    return "не отличимо от нуля"


def window_midpoint(meta: dict) -> float | None:
    a, b = parse_iso(meta.get("window_from_utc")), parse_iso(meta.get("window_to_utc"))
    return None if a is None or b is None else (a + b) / 2.0


def split_half(signals: list, mid: float) -> tuple[list, list]:
    first = [s for s in signals if (s.get("block_time") is not None) and s["block_time"] < mid]
    second = [s for s in signals if (s.get("block_time") is not None) and s["block_time"] >= mid]
    return first, second


def a3_block(rows: list, horizon: str) -> dict:
    return {
        "все": свод(rows, horizon),
        "бутстрэп_среднего": бутстрэп_среднего(rows, horizon),
        "вывод": вывод_по_интервалу(бутстрэп_среднего(rows, horizon)),
        "без_3_лучших": свод(без_n_лучших(rows, horizon, 3), horizon),
        "без_5_лучших": свод(без_n_лучших(rows, horizon, 5), horizon),
    }


def compute_a3(rows: list, rows_by_half: tuple[list, list] | None) -> dict:
    out = {}
    for horizon in ("t28_8", "t60s"):
        block = a3_block(rows, horizon)
        if rows_by_half is not None:
            first_rows, second_rows = rows_by_half
            block["первая_половина_окна"] = свод(first_rows, horizon)
            block["первая_половина_бутстрэп"] = бутстрэп_среднего(first_rows, horizon)
            block["вторая_половина_окна"] = свод(second_rows, horizon)
            block["вторая_половина_бутстрэп"] = бутстрэп_среднего(second_rows, horizon)
        out[horizon] = {вход: block for вход in ВХОДЫ}
    return out


def compute_a3_by_source(rows: list) -> dict:
    by_src: dict = {}
    for r in rows:
        by_src.setdefault(r["source"], []).append(r)
    out = {}
    прочие = []
    for src, lst in by_src.items():
        if len(lst) >= ПОРОГ_ИСТОЧНИКА_ДЛЯ_РАЗБИВКИ:
            out[src] = {h: свод(lst, h) for h in ("t28_8", "t60s")}
        else:
            прочие.extend(lst)
    if прочие:
        out["прочие_источники_lt_" + str(ПОРОГ_ИСТОЧНИКА_ДЛЯ_РАЗБИВКИ) + "_сделок"] = \
            {h: свод(прочие, h) for h in ("t28_8", "t60s")}
    return out


# --------------------------------------------------------------------- правила D

def правило_налог_gt0(sig: dict) -> str:
    if not sig["target_tax_known"]:
        return "неизвестно"
    return "отсечь" if sig["target_taxable"] else "оставить"


def правило_налог_gt100bps(sig: dict) -> str:
    if not sig["target_tax_known"]:
        return "неизвестно"
    if not sig["target_taxable"]:
        return "оставить"
    return "отсечь" if (sig["target_tax_bps"] or 0) > 100 else "оставить"


def правило_налоговый_промежуточный(sig: dict) -> str:
    if sig["quote_is_sol"]:
        return "оставить"
    if not sig["quote_tax_known"]:
        return "неизвестно"
    return "отсечь" if sig["quote_taxable"] else "оставить"


def правило_маршрут_ge3_proxy(sig: dict) -> str:
    return "отсечь" if sig["hops_proxy"] >= 3 else "оставить"


def правило_котировка_не_sol(sig: dict) -> str:
    return "отсечь" if not sig["quote_is_sol"] else "оставить"


def правило_покупка_lt(порог_sol: float):
    def rule(sig):
        v = sig["spend_sol_equiv"]
        return "неизвестно" if v is None else ("отсечь" if v < порог_sol else "оставить")
    rule.__name__ = f"покупка_источника_lt_{порог_sol}_sol"
    return rule


def правило_возраст(_sig: dict) -> str:
    # Нет поля времени создания минта НИ В ОДНОМ переданном кэше -- честно
    # "неизвестно" всегда, а не выдуманная дата. Правило числится в отчёте,
    # но не может быть ни рекомендовано, ни отклонено по этим данным.
    return "неизвестно"


def правило_толпа_gt(порог: float | None):
    def rule(sig):
        if порог is None:
            return "неизвестно"
        v = sig["crowd_30s"]
        return "неизвестно" if v is None else ("отсечь" if v > порог else "оставить")
    rule.__name__ = f"толпа_gt_{порог}"
    return rule


def правило_глубина_lt(доля_M: float | None):
    def rule(sig):
        if доля_M is None or sig["quote_is_sol"]:
            return "неизвестно"
        depth, v = sig["leg_pool_sol_depth"], sig["spend_sol_equiv"]
        if not depth or v is None:
            return "неизвестно"
        return "отсечь" if (v / depth) < доля_M else "оставить"
    rule.__name__ = f"глубина_lt_{доля_M}_proxy"
    return rule


def правило_плохая_серия(sig: dict) -> str:
    return {"да": "отсечь", "нет": "оставить"}.get(sig.get("плохая_серия"), "неизвестно")


def AND(rule_a, rule_b, name: str):
    def rule(sig):
        a, b = rule_a(sig), rule_b(sig)
        if a == "неизвестно" or b == "неизвестно":
            return "неизвестно"
        return "отсечь" if (a == "отсечь" and b == "отсечь") else "оставить"
    rule.__name__ = name
    return rule


def применить_правило(signals: list, rule) -> dict:
    группы = {"отсечь": [], "оставить": [], "неизвестно": []}
    for s in signals:
        группы[rule(s)].append(s)
    return группы


def d_rule_report(name: str, rule, train: list, test: list, rows_by_sig: dict,
                   horizon: str = "t28_8") -> dict:
    out = {"правило": name}
    for half_name, half in (("обучение_1_половина", train), ("проверка_2_половина", test)):
        группы = применить_правило(half, rule)
        отсеч_rows = [rows_by_sig[s["signature"]] for s in группы["отсечь"] if s["signature"] in rows_by_sig]
        остав_rows = [rows_by_sig[s["signature"]] for s in группы["оставить"] if s["signature"] in rows_by_sig]
        out[half_name] = {
            "n_отсечь": len(группы["отсечь"]), "n_оставить": len(группы["оставить"]),
            "n_неизвестно": len(группы["неизвестно"]),
            "результат_отсечённых": свод(отсеч_rows, horizon),
            "результат_оставленных": свод(остав_rows, horizon),
            "бутстрэп_оставленных": бутстрэп_среднего(остав_rows, horizon),
            "оставленные_без_3_лучших": свод(без_n_лучших(остав_rows, horizon, 3), horizon),
            "оставленные_без_5_лучших": свод(без_n_лучших(остав_rows, horizon, 5), horizon),
        }
    return out


# --------------------------------------------------------------------- сборка правил

def строить_правила(train: list) -> list:
    """Пороги, которых владелец не задал числом (толпа, глубина пула),
    подбираются ТОЛЬКО по обучающей (первой) половине -- median известных
    значений на train; на test правило применяется с уже зафиксированным
    порогом (иначе это была бы утечка данных из проверочной половины)."""
    train_crowd = sorted(v for s in train if (v := s["crowd_30s"]) is not None)
    порог_толпы = statistics.median(train_crowd) if train_crowd else None

    train_depth_ratio = sorted(
        s["spend_sol_equiv"] / s["leg_pool_sol_depth"]
        for s in train
        if not s["quote_is_sol"] and s["leg_pool_sol_depth"] and s["spend_sol_equiv"] is not None
    )
    порог_глубины = statistics.median(train_depth_ratio) if train_depth_ratio else None

    одиночные = [
        ("налог_токена_gt0", правило_налог_gt0),
        ("налог_токена_gt100bps", правило_налог_gt100bps),
        ("налоговый_промежуточный_минт", правило_налоговый_промежуточный),
        ("маршрут_ge3_шагов_proxy", правило_маршрут_ge3_proxy),
        ("котировка_не_sol", правило_котировка_не_sol),
        ("покупка_источника_lt_2_sol", правило_покупка_lt(2.0)),
        ("покупка_источника_lt_5_sol", правило_покупка_lt(5.0)),
        ("покупка_источника_lt_10_sol", правило_покупка_lt(10.0)),
        ("возраст_токена_lt_10min", правило_возраст),
        ("возраст_токена_lt_1h", правило_возраст),
        (f"толпа_gt_train_median_{порог_толпы}", правило_толпа_gt(порог_толпы)),
        (f"глубина_lt_train_median_{порог_глубины}_proxy", правило_глубина_lt(порог_глубины)),
        ("плохая_серия_источника_24ч", правило_плохая_серия),
    ]
    by_name = dict(одиночные)
    пары = [
        ("пара_налог_gt0_И_покупка_lt5", AND(by_name["налог_токена_gt0"],
                                              by_name["покупка_источника_lt_5_sol"],
                                              "пара_налог_gt0_И_покупка_lt5")),
        ("пара_налог_gt0_И_толпа", AND(by_name["налог_токена_gt0"],
                                        by_name[f"толпа_gt_train_median_{порог_толпы}"],
                                        "пара_налог_gt0_И_толпа")),
        ("пара_маршрут_ge3_И_котировка_не_sol", AND(by_name["маршрут_ge3_шагов_proxy"],
                                                     by_name["котировка_не_sol"],
                                                     "пара_маршрут_ge3_И_котировка_не_sol")),
        ("пара_налог_gt0_И_плохая_серия", AND(by_name["налог_токена_gt0"],
                                               by_name["плохая_серия_источника_24ч"],
                                               "пара_налог_gt0_И_плохая_серия")),
    ]
    return одиночные, пары, {"порог_толпы_crowd_30s": порог_толпы, "порог_глубины_доля": порог_глубины}


# --------------------------------------------------------------------- self-test

def self_test() -> int:
    checks = []

    def chk(name, ok, got=""):
        checks.append((name, bool(ok), got))

    # ---- 1. build_signal / налог -----------------------------------------
    minty = {"TAXED": {"таксируемый": True, "ставка_комиссии_bps": 300},
              "CLEAN": {"таксируемый": False, "ставка_комиссии_bps": None}}
    depth = {"QUOTE1": 100.0}
    raw_taxed = {"mint": "TAXED", "quote_mint": WSOL, "signature": "s1", "block_time": 1000,
                 "block_time_utc": "t1", "spend_sol_equiv": 3.0, "spot_after": "0.001",
                 "growth_after_30s": 1.5, "growth_after_60s": 1.6, "split_route": False}
    sig_taxed = build_signal(raw_taxed, minty, depth)
    chk("известный налоговый минт -> target_taxable True", sig_taxed["target_taxable"] is True)
    chk("ставка снята верно (300bps)", sig_taxed["target_tax_bps"] == 300)
    raw_clean = dict(raw_taxed, mint="CLEAN", signature="s2")
    sig_clean = build_signal(raw_clean, minty, depth)
    chk("известный чистый минт -> target_taxable False", sig_clean["target_taxable"] is False)
    raw_unknown = dict(raw_taxed, mint="ГДЕ_ТО_ДРУГОЕ", signature="s3")
    sig_unknown = build_signal(raw_unknown, minty, depth)
    chk("минта нет в справочнике -> target_tax_known False (не 'без налога')",
        sig_unknown["target_tax_known"] is False)
    chk("и target_taxable у неизвестного -- None, не False", sig_unknown["target_taxable"] is None)

    # ---- 2. hops_proxy ------------------------------------------------------
    chk("котировка SOL -> 1 шаг", hops_proxy(WSOL, False) == 1)
    chk("котировка не-SOL без split -> 2 шага", hops_proxy("QUOTE1", False) == 2)
    chk("split_route=True -> 3 шага (proxy)", hops_proxy("QUOTE1", True) == 3)

    # ---- 3. entry_price / horizon_growth -------------------------------------
    chk("entry_price = spot_after", entry_price_for(sig_taxed) == 0.001)
    sig_no_spot = dict(sig_taxed, spot_after=None)
    chk("нет spot_after -> вход None (не выдуманное число)", entry_price_for(sig_no_spot) is None)
    g, why = horizon_growth(sig_taxed, "t28_8")
    chk("t28_8 берёт growth_after_30s без интерполяции", g == 1.5 and why is None)
    g10, why10 = horizon_growth(sig_taxed, "t10s")
    chk("t10s всегда missing -- в кэше такой точки нет", g10 is None and "10 с" in why10)
    g5m, why5m = horizon_growth(sig_taxed, "t5m")
    chk("t5m всегда missing", g5m is None and "5 мин" in why5m)
    gsell, whysell = horizon_growth(sig_taxed, "до_первой_продажи_источника")
    chk("до первой продажи источника -- всегда missing", gsell is None and "продаж" in whysell)

    # ---- 4. net_pnl_sol: издержки один раз, не дважды ------------------------
    # growth=1.0 (цена не сдвинулась) -> результат = минус ровно суммарные
    # издержки на обе стороны, посчитанные по формуле "раз на сторону".
    net, why = net_pnl_sol(1.0, 1.0, sig_clean, size_sol=1.0, bloom_fee=0.01, tip=0.0,
                            network_fee=0.0, pool_fee_bps=0.0)
    ожидаемо = (1.0 * 0.99) * 0.99 - 1.0  # покупка теряет 1%, продажа теряет 1% от остатка
    chk("издержки применены один раз на сторону (без интерполяции/удвоения)",
        why is None and abs(net - ожидаемо) < 1e-12, (net, ожидаемо))
    net_tip, _ = net_pnl_sol(1.0, 1.0, sig_clean, size_sol=1.0, bloom_fee=0.0, tip=0.05,
                              network_fee=0.0, pool_fee_bps=0.0)
    chk("чаевые сняты РОВНО дважды (по разу на сторону), не 4 раза",
        abs(net_tip - (-0.10)) < 1e-9, net_tip)
    net_missing_price, why_mp = net_pnl_sol(None, 1.2, sig_clean)
    chk("нет цены входа -> None с причиной, не 0", net_missing_price is None and why_mp)
    net_missing_growth, why_mg = net_pnl_sol(1.0, None, sig_clean)
    chk("нет роста на горизонте -> None с причиной", net_missing_growth is None and why_mg)
    net_missing_tax, why_mt = net_pnl_sol(1.0, 1.2, sig_unknown)
    chk("неизвестный налог НЕ считается нулевым -- результат None",
        net_missing_tax is None and "налог" in why_mt)
    net_full_loss, _ = net_pnl_sol(1.0, 0.0, sig_clean, size_sol=1.0, bloom_fee=2.0, tip=0.0,
                                    network_fee=0.0)
    chk("издержки съели всю ставку -> честный -size_sol, а не NaN/крах",
        net_full_loss == -1.0)

    # ---- 5. правила D --------------------------------------------------------
    chk("налог>0 отсекает известный налоговый", правило_налог_gt0(sig_taxed) == "отсечь")
    chk("налог>0 не режет обычный (известный без налога)", правило_налог_gt0(sig_clean) == "оставить")
    chk("налог>0 на неизвестном -> неизвестно, не отсечь", правило_налог_gt0(sig_unknown) == "неизвестно")
    sig_low_bps = build_signal(dict(raw_taxed, mint="LOW"), {"LOW": {"таксируемый": True,
                                                                       "ставка_комиссии_bps": 50}}, depth)
    chk(">100bps не режет 50bps", правило_налог_gt100bps(sig_low_bps) == "оставить")
    chk(">100bps режет 300bps", правило_налог_gt100bps(sig_taxed) == "отсечь")
    r5 = правило_покупка_lt(5.0)
    chk("покупка<5: 3 SOL -> отсечь", r5(dict(sig_clean, spend_sol_equiv=3.0)) == "отсечь")
    chk("покупка<5: 10 SOL -> оставить", r5(dict(sig_clean, spend_sol_equiv=10.0)) == "оставить")
    chk("покупка<5: нет данных -> неизвестно", r5(dict(sig_clean, spend_sol_equiv=None)) == "неизвестно")
    chk("котировка не SOL: WSOL -> оставить", правило_котировка_не_sol(sig_clean) == "оставить")
    sig_other_quote = build_signal(dict(raw_taxed, quote_mint="QUOTE1", mint="CLEAN"), minty, depth)
    chk("котировка не SOL: чужой минт -> отсечь", правило_котировка_не_sol(sig_other_quote) == "отсечь")
    rc = правило_толпа_gt(50.0)
    chk("толпа>50: 80 -> отсечь", rc(dict(sig_clean, crowd_30s=80)) == "отсечь")
    chk("толпа>50: 10 -> оставить", rc(dict(sig_clean, crowd_30s=10)) == "оставить")
    chk("толпа: нет данных -> неизвестно", rc(dict(sig_clean, crowd_30s=None)) == "неизвестно")
    chk("возраст токена: нет данных -> всегда неизвестно", правило_возраст(sig_clean) == "неизвестно")
    rd = правило_глубина_lt(0.01)
    small_buy = dict(sig_other_quote, spend_sol_equiv=0.5, leg_pool_sol_depth=100.0)
    big_buy = dict(sig_other_quote, spend_sol_equiv=5.0, leg_pool_sol_depth=100.0)
    chk("глубина<1%: покупка 0.5% -> отсечь", rd(small_buy) == "отсечь")
    chk("глубина<1%: покупка 5% -> оставить", rd(big_buy) == "оставить")
    chk("глубина: quote_is_sol -> неизвестно (данных по глубине для SOL-пула нет)",
        rd(sig_clean) == "неизвестно")
    chk("глубина: нет данных о depth -> неизвестно",
        rd(dict(sig_other_quote, leg_pool_sol_depth=None, spend_sol_equiv=1.0)) == "неизвестно")

    and_rule = AND(правило_налог_gt0, r5, "и")
    both_cut = dict(sig_taxed, spend_sol_equiv=1.0)
    chk("AND: оба отсекают -> отсечь", and_rule(both_cut) == "отсечь")
    one_unknown = dict(sig_unknown, spend_sol_equiv=1.0)
    chk("AND: один неизвестен -> неизвестно (не оставить и не отсечь)",
        and_rule(one_unknown) == "неизвестно")
    only_one_cuts = dict(sig_taxed, spend_sol_equiv=10.0)
    chk("AND: отсекает только один -> оставить", and_rule(only_one_cuts) == "оставить")

    # ---- 6. плохая серия источника -------------------------------------------
    def T(sig_id, bt, g30):
        return {"source_address": "SRC", "signature": sig_id, "block_time": bt,
                "growth_30s": g30, "growth_60s": None}
    seq = [T("a", 0, 0.8), T("b", 3600, 0.7), T("c", 7200, 1.5)]
    аннотировать_серию(list(seq))
    chk("первая сделка источника -- неизвестно (нет истории)", seq[0]["плохая_серия"] == "неизвестно")
    chk("вторая -- всё ещё неизвестно (мин_историй=2, есть только 1)",
        seq[1]["плохая_серия"] == "неизвестно")
    chk("третья: 2 предыдущих убыточные (100%) -> да",
        seq[2]["плохая_серия"] == "да", seq[2])
    seq2 = [T("a", 0, 1.5), T("b", 3600, 1.4), T("c", 7200, 0.5)]
    аннотировать_серию(list(seq2))
    chk("2 предыдущих в плюс -> нет (не большинство проигрышей)", seq2[2]["плохая_серия"] == "нет")
    seq3 = [T("a", 0, 0.5), T("b", 200000, 1.5)]  # больше 24ч между сделками
    аннотировать_серию(list(seq3))
    chk("предыдущая сделка старше 24ч -> не считается (неизвестно)",
        seq3[1]["плохая_серия"] == "неизвестно")

    # ---- 7. свод / без_n_лучших / бутстрэп -----------------------------------
    def R(net):
        return {"h": {"net_sol": net, "missing": net is None, "why_not": None}}
    rows_stat = [R(1.0), R(0.5), R(0.2), R(-0.1), R(-0.2), R(-0.3)]
    s = свод(rows_stat, "h")
    chk("свод: сумма", abs(s["сумма_sol"] - 1.1) < 1e-9, s)
    chk("свод: доля в плюс = половина", s["доля_в_плюс"] == 0.5)
    chk("свод: среднее", abs(s["среднее_sol"] - 1.1 / 6) < 1e-6, s["среднее_sol"])
    chk("свод: медиана", abs(s["медиана_sol"] - 0.05) < 1e-9, s["медиана_sol"])
    chk("без_1_лучших убирает 1.0", свод(без_n_лучших(rows_stat, "h", 1), "h")["сумма_sol"] == 0.1)
    chk("без_3_лучших уходит в минус", свод(без_n_лучших(rows_stat, "h", 3), "h")["сумма_sol"] < 0)
    chk("пустой список -> сделок=0, не крах", свод([], "h")["сделок"] == 0)
    rows_missing = [R(1.0), {"h": {"net_sol": None, "missing": True, "why_not": "x"}}]
    s_missing = свод(rows_missing, "h")
    chk("missing не превращается в 0 в среднем", s_missing["сделок"] == 1 and s_missing["сделок_без_цифры"] == 1)

    b1 = бутстрэп_среднего(rows_stat, "h", n_boot=2000)
    b2 = бутстрэп_среднего(rows_stat, "h", n_boot=2000)
    chk("бутстрэп воспроизводим при одном сиде (2000 повторов)",
        b1["интервал_95_низ_sol"] == b2["интервал_95_низ_sol"]
        and b1["интервал_95_верх_sol"] == b2["интервал_95_верх_sol"])
    b3 = бутстрэп_среднего(rows_stat, "h", n_boot=2000, seed=SEED + 1)
    chk("другой сид -- другой интервал (иначе он не случайный)",
        b3["интервал_95_низ_sol"] != b1["интервал_95_низ_sol"])
    chk("низ интервала не выше верха", b1["интервал_95_низ_sol"] <= b1["интервал_95_верх_sol"])
    b_empty = бутстрэп_среднего([], "h")
    chk("бутстрэп на пустом входе -- честный отказ, не крах", "почему" in b_empty)
    b_one = бутстрэп_среднего([R(1.0)], "h")
    chk("бутстрэп на одной сделке -- честный отказ", "почему" in b_one)
    chk("вывод по интервалу: явный плюс", вывод_по_интервалу({"интервал_95_низ_sol": 0.1,
                                                                "интервал_95_верх_sol": 0.2}) == "край есть")
    chk("вывод по интервалу: явный минус",
        вывод_по_интервалу({"интервал_95_низ_sol": -0.2, "интервал_95_верх_sol": -0.1})
        == "края нет (статистически ниже нуля)")
    chk("вывод по интервалу: ноль внутри",
        вывод_по_интервалу({"интервал_95_низ_sol": -0.1, "интервал_95_верх_sol": 0.1})
        == "не отличимо от нуля")

    # ---- 8. пустой вход целиком (compute_a1 / загрузка) ----------------------
    chk("compute_a1([]) не падает и даёт []", compute_a1([]) == [])
    trades_empty, meta_empty = load_crowd_trades(DATA / "____нет_такого_файла____.json")
    chk("load_crowd_trades на несуществующем файле -> ([], {}), не исключение",
        trades_empty == [] and meta_empty == {})
    chk("load_mint_tax на отсутствующем файле -> {}",
        load_mint_tax(DATA / "____нет____.json") == {})
    chk("window_midpoint без дат -> None", window_midpoint({}) is None)

    # ---- 9. первая/вторая половина окна ---------------------------------------
    sig_early = dict(sig_clean, block_time=100, signature="e1")
    sig_late = dict(sig_clean, block_time=900, signature="l1")
    first, second = split_half([sig_early, sig_late], 500.0)
    chk("сделка до середины окна попадает в первую половину", first == [sig_early])
    chk("сделка после середины окна попадает во вторую половину", second == [sig_late])

    bad = 0
    for name, ok, got in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {name}" + (f"  -> {got}" if got != "" and not ok else ""))
        bad += (not ok)
    print(f"самопроверка night_edge_model: {len(checks) - bad}/{len(checks)} пройдено")
    if bad:
        raise SystemExit(f"самопроверка не пройдена: {bad} из {len(checks)}")
    return 0


# --------------------------------------------------------------------- main

def main() -> int:
    global N_BOOT  # noqa: PLW0603 -- один явный параметр прогона (--n-boot), не скрытое состояние
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--crowd", default=str(DEFAULT_CROWD))
    ap.add_argument("--tax-groups", default=str(DEFAULT_TAX_GROUPS))
    ap.add_argument("--transfer-fee-audit", default=str(DEFAULT_TRANSFER_FEE_AUDIT))
    ap.add_argument("--leg-pools", default=str(DEFAULT_LEG_POOLS))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--n-boot", type=int, default=N_BOOT)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    N_BOOT = args.n_boot

    t0 = time.time()
    raw_trades, meta = load_crowd_trades(args.crowd)
    if not raw_trades:
        print(f"СТОП: нет сделок в {args.crowd} (файл пуст или не найден) -- считать нечего",
              file=sys.stderr)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps({
            "schema_version": 1, "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "входные_файлы": {"crowd": args.crowd}, "ошибка": "пустой или отсутствующий вход --crowd",
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        return 2

    minty = load_mint_tax(args.transfer_fee_audit)
    depth_by_quote = load_leg_pool_depth(args.leg_pools)
    tax_groups = load_json(args.tax_groups) or {}

    signals = [build_signal(t, minty, depth_by_quote) for t in raw_trades]
    аннотировать_серию(signals)
    rows = compute_a1(signals)
    rows_by_sig = {r["signature"]: r for r in rows}

    mid = window_midpoint(meta)
    if mid is not None:
        train, test = split_half(signals, mid)
        rows_first = [rows_by_sig[s["signature"]] for s in train if s["signature"] in rows_by_sig]
        rows_second = [rows_by_sig[s["signature"]] for s in test if s["signature"] in rows_by_sig]
    else:
        train, test, rows_first, rows_second = signals, [], rows, []

    a3 = compute_a3(rows, (rows_first, rows_second) if mid is not None else None)
    a3_src = compute_a3_by_source(rows)

    одиночные, пары, пороги = строить_правила(train)
    d_report = {"пороги_подобранные_на_первой_половине": пороги}
    for name, rule in одиночные:
        d_report[name] = d_rule_report(name, rule, train, test, rows_by_sig)
    for name, rule in пары:
        d_report[name] = d_rule_report(name, rule, train, test, rows_by_sig)

    n_full_30 = sum(1 for r in rows if r["t28_8"]["net_sol"] is not None)
    n_full_60 = sum(1 for r in rows if r["t60s"]["net_sol"] is not None)

    out = {
        "schema_version": 1,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_s": round(time.time() - t0, 2),
        "входные_файлы": {"crowd": args.crowd, "tax_groups": args.tax_groups,
                            "transfer_fee_audit": args.transfer_fee_audit, "leg_pools": args.leg_pools},
        "окно": meta,
        "n_сигналов_всего": len(signals),
        "n_с_известным_net_на_28_8с": n_full_30, "n_с_известным_net_на_60с": n_full_60,
        "ЧЕСТНЫЕ_ОГОВОРКИ": [
            "Входы (i)/(ii)/(iii) численно совпадают: единственная реальная цена "
            "после источника в кэше -- spot_after; цены на границе слотов S+1/S+2 "
            "нет, интерполяция запрещена условием задачи. См. докстринг модуля.",
            "Таймер 28.8с считается по ближайшей реальной точке 30с "
            "(growth_after_30s), без интерполяции; разница ~1.2с (~4% таймера) "
            "объявлена, не скрыта.",
            "Горизонты 10с/5мин/до первой продажи источника отсутствуют в кэше "
            "целиком и везде помечены missing/why_not -- не 0 и не оценка.",
            "Комиссия пула (swap fee AMM) НЕ включена в net_sol: в кэше и "
            "репозитории нет ставки bps по pool_program, только ярлыки программ. "
            "Реальный чистый результат ниже посчитанного на эту неизвестную "
            "величину.",
            "Налоговый статус минта 'неизвестно' (минта нет в "
            "solana_transfer_fee_audit.json) НЕ считается 'без налога' -- такие "
            "сделки помечены missing и не входят в среднее/сумму/долю в плюс "
            f"({len(rows) - n_full_30} из {len(rows)} сделок без полного net на "
            "горизонте 28.8с).",
            "'Возраст токена' и 'до первой продажи источника' не оцениваются "
            "вообще: ни в одном переданном файле нет времени создания минта или "
            "времени продажи источника.",
            "'Число шагов маршрута' и 'глубина пула' в правилах D -- ПРОКСИ по "
            "имеющимся полям (split_route, глубина пула КОТИРОВОЧНОГО минта к "
            "SOL, а не пула самого таргет-минта), не измеренные величины; см. "
            "hops_proxy/leg_pool_sol_depth в докстрингах.",
        ],
        "A3": a3,
        "A3_по_источникам": a3_src,
        "D_правила": d_report,
        "справочно_налоговые_группы_из_репозитория": {
            "итог_все_сделки_из_solana_tax_groups": tax_groups.get("итог_все"),
        },
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"сигналов всего: {len(signals)}; с известным net на 28.8с: {n_full_30}; "
          f"на 60с: {n_full_60}")
    for horizon in ("t28_8", "t60s"):
        blk = a3[horizon][ВХОДЫ[0]]
        print(f"[{horizon}] все: {blk['все']}; вывод: {blk['вывод']}")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
