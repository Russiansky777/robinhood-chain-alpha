#!/usr/bin/env python3
"""Ночная задача владельца, часть B+C: признаки токсичных токенов и следы
манипуляций -- ТОЛЬКО чтение и счёт по уже собранному кэшу, ни одной
транзакции, ни одного ключа.

Почему один файл и что он НЕ делает.

Файл не ходит в сеть сам по себе и не тянет HELIUS_API_KEY -- в контейнере
его нет. Всё, что требует цепи (mint/freeze authority, налог по маршруту,
топ-10 держателей, возраст минта и пула, толпа В КОНКРЕТНОМ слоте, глубина
пула на момент сделки), оформлено как функция, принимающая `rpc_call`
(callable(method: str, params: list) -> результат JSON-RPC, тот же формат,
что отдаёт узел: для getAccountInfo -- {"context":..,"value":..},
для getSignaturesForAddress/getTokenLargestAccounts -- список/{"value":[]}).
Самопроверка подставляет вместо rpc_call обычную функцию замыкания над
словарём -- сети не видно вовсе. Боевого прогона этих функций в этом
контейнере не было и не будет: ниже везде, где нужна цепь, посчитано
СКОЛЬКО ЭТО СТОИЛО БЫ (тариф Helius Developer, см. analysis/solana_rpc_client.py:
обычный вызов -- 1 кредит, getProgramAccounts -- 10; здесь второе не
используется), а не выдуман результат.

Что читается из уже лежащего кэша (никакой цепи):
  * data/crowd_metric_2026-09-24.json (задача A, 18 источников, 346 покупок
    за 7 дней) -- поставляется через --crowd, путь в /tmp на эту ночь, по
    умолчанию ищется в data/;
  * data/c2_block_position_2026-09-24.json (150 сделок двух лидеров,
    followers[] -- кто и с какой платой встаёт сразу за ними) -- --followers;
  * data/solana_transfer_fee_audit.json -- каталог "минты": программа
    токена, ставка комиссии на перевод (bps), её потолок и ПРЕЖНЯЯ ставка
    (для C4 -- подъём налога), но БЕЗ mint/freeze authority: тот файл их не
    читал, здесь взять неоткуда, кроме цепи;
  * data/solana_tax_groups.json -- асимметрия исхода по маршруту (прямой /
    через таксируемый промежуточный), для контекста к C4/маршрутному налогу.

Цена ошибки. Подмена "нет данных" нулём или средним превратила бы
отсутствие сигнала в сигнал (токен без данных о держателях выглядел бы как
токен с 0% у топ-10, то есть "нетоксичный" -- ровно наоборот того, что
нужно проверить). Поэтому у каждого признака два состояния: значение или
{"known": false, "why_not": "..."}, и группировка (features_b.group_by_feature)
никогда не превращает "неизвестно" в отдельную строку с фиктивным средним --
строка "unknown" есть, но её среднее -- None, если у неё 0 известных
исходов, а не 0.0.

--self-test: >= 20 проверок на синтетике (без сети и без чтения кэша).
Запуск на кэше: --crowd/--followers путь + --out (по умолчанию data/).
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data"

# ------------------------------------------------------------ тариф Helius
# Переписано из analysis/solana_rpc_client.py (docs.helius.dev), а не
# импортировано: этот файл нарочно не тянет ничего из c2_*/solana_rpc_client,
# чтобы посчитанная тут цена не зависела от суточного бюджета C2 и не могла
# случайно потратить чужую квоту.
CREDITS_DEFAULT = 1          # обычный вызов: getAccountInfo, getTransaction,
                              # getBlock, getSignaturesForAddress,
                              # getTokenLargestAccounts -- все по 1
CREDITS_GET_PROGRAM_ACCOUNTS = 10  # не используется ни одной функцией ниже

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
STABLES = (USDC, USDT)

# Пулы вида x*y=k, где остаток хранилища -- это и есть резерв (спот_после
# из задачи A по ним и посчитан). Независимая копия того же списка, что в
# c2_followers_growth.py -- см. её комментарий: сам источник (c2_crowd_metric.py)
# на этой ветке не живёт как импортируемый модуль, а плодить зависимость
# на файл с префиксом c2_ здесь нарочно не стали (см. докстрока модуля).
RESERVE_SPOT_PROGRAMS = {
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "Pump AMM",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
}

LIQUIDITY_DRAIN_PREFIX = "ликвидность пула снята"
AGE_DEFAULT_MAX_PAGES = 5
AGE_PAGE_LIMIT = 1000

NAMED_SNIPERS = (
    "7JVQMwRj82STgsG57spj6vpE6XY3RqG8B64PczVc7jJr",
    "GUiSJYdAs5nyPcTkyZbnnEsL4R6VJKzemcUQgnHWBCgq",
)


# ============================================================ статистика

def median(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def mean(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return (sum(xs) / len(xs)) if xs else None


def pct_from_ratio(ratio: float | None) -> float | None:
    """growth_30s -- это P30/P0 (определение задачи A). В проценты роста/
    падения переводим здесь и только здесь, одной функцией, чтобы не
    разъехаться в знаке между модулями."""
    return None if ratio is None else (ratio - 1.0) * 100.0


def cv(xs: list) -> float | None:
    """Коэффициент вариации std/|среднее| -- низкий у величины, которая
    держится около одного числа (в пользу фиксированной платы), высокий у
    величины, которая гуляет широко (в пользу платы, зависящей от чего-то
    ещё, например объёма). Не вывод, а число для таблицы."""
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    if m == 0:
        return None
    var = sum((x - m) ** 2 for x in xs) / len(xs)
    return round((var ** 0.5) / abs(m), 4)


def quantile(xs: list, q: float) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo, hi = int(pos), min(int(pos) + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] + (xs[hi] - xs[lo]) * frac


# ============================================================ загрузка кэша

def load_crowd_trades(path: Path) -> tuple[list, list]:
    """Разворачивает per_source[].trades[] в плоский список сделок, к
    каждой сделке дописан источник (_source_address/_source_task/
    _source_remark/_source_n_signatures) -- без этого С1 не может оценить
    стоимость дочитывания продаж источника по числу его подписей.

    Возвращает (сделки, метаданные_источников) -- метаданные отдельно,
    потому что нужны один раз на источник, а не на каждую его сделку."""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    trades: list = []
    sources: list = []
    for ps in d.get("per_source") or []:
        addr = ps.get("address")
        sources.append({
            "address": addr, "task": ps.get("task"), "remark": ps.get("remark"),
            "n_signatures": ps.get("n_signatures"), "n_trades": len(ps.get("trades") or []),
        })
        for t in ps.get("trades") or []:
            row = dict(t)
            row["_source_address"] = addr
            row["_source_task"] = ps.get("task")
            row["_source_remark"] = ps.get("remark")
            trades.append(row)
    return trades, sources


def load_followers(path: Path) -> dict:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        "per_trade": d.get("per_trade") or [],
        "followers": d.get("followers") or [],
        "quartiles": d.get("quartiles") or {},
    }


def load_tax_catalog(path: Path) -> dict:
    """{минт: {программа_токена_id, ставка_комиссии_bps, ...}} из
    data/solana_transfer_fee_audit.json -- каталог накоплен ПО ЦЕПИ на
    прошлых своих сделках (getAccountInfo), но не по кошелькам-источникам
    из задачи A, поэтому покрытие частичное: пересечение считается ниже,
    а не предполагается полным. Файла нет -- {}, а не отказ всего модуля:
    остальные признаки от него не зависят."""
    p = Path(path)
    if not p.exists():
        return {}
    d = json.loads(p.read_text(encoding="utf-8"))
    return d.get("минты") or {}


def load_transfer_fee_rows(path: Path) -> list:
    p = Path(path)
    if not p.exists():
        return []
    d = json.loads(p.read_text(encoding="utf-8"))
    return d.get("строки") or []


def load_tax_groups(path: Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def latest(glob_pat: str) -> Path | None:
    cands = sorted(Path(p) for p in glob.glob(glob_pat))
    return cands[-1] if cands else None


# ============================================================ B: признаки из кэша

def quote_kind(trade: dict) -> str | None:
    q = trade.get("quote_mint")
    if q is None:
        return None
    if q == WSOL:
        return "sol"
    if q in STABLES:
        return "stable"
    return "other"


def pool_kind(trade: dict) -> str | None:
    """Тип пула -- по программе. Резервные (x*y=k) называем по имени (тот
    же список, что и в c2_followers_growth.py), остальные -- по первым 8
    символам ID программы: имена CLMM/DLMM/DBC им приписывать не будем,
    потому что ни один файл в репозитории их так не подтверждает -- это
    было бы "признак по названию", а не по данным."""
    pp = trade.get("pool_program")
    if pp is None:
        return None
    return RESERVE_SPOT_PROGRAMS.get(pp, f"non_reserve:{pp[:8]}")


def impact_bucket(trade: dict, threshold: float) -> str | None:
    """Суррогат "покупка в % глубины": growth задачи A уже посчитала
    spot_after/price_0 = impact_source -- для резервных пулов это и есть
    относительный размер покупки (чем больше покупка относительно резерва,
    тем сильнее двигается спот). НЕ то же самое, что доля глубины в SOL --
    только знак и порядок величины; для CLMM/DLMM/кривых поле пустое
    (в задаче A это уже помечено why_no_after) -- там impact_source == None,
    и это НЕ подставляется 0."""
    v = trade.get("impact_source")
    if v is None:
        return None
    return "impact_ge_threshold" if v >= threshold else "impact_lt_threshold"


def spend_bucket(trade: dict, threshold: float) -> str | None:
    v = trade.get("spend_sol_equiv")
    if v is None:
        return None
    return "spend_ge_median" if v >= threshold else "spend_lt_median"


def crowd_bucket(trade: dict, threshold: float) -> str | None:
    v = trade.get("crowd_30s")
    if v is None:
        return None
    return "crowd_ge_median" if v >= threshold else "crowd_lt_median"


def failed_same_block_flag(trade: dict) -> str | None:
    v = trade.get("failed_same_block_wallets")
    if v is None:
        return None
    return "has_failed_in_block" if v > 0 else "no_failed_in_block"


def split_route_flag(trade: dict) -> str | None:
    v = trade.get("split_route")
    if v is None:
        return None
    return "route_split" if v else "route_not_split"


def is_taxable_from_catalog(mint: str, tax_catalog: dict) -> tuple[bool | None, str | None]:
    """Признак "налоговый" -- ТОЛЬКО из ставки bps в каталоге (по цепи),
    никогда из имени/тикера минта. mint отсутствует в каталоге -> (None,
    причина): это специально проверено в самопроверке (self_test, проверка
    "по данным, а не по имени"), потому что имя токена -- ровно то поле,
    которым токсичный токен обманывает."""
    info = tax_catalog.get(mint)
    if info is None:
        return None, "минт не встречался в каталоге data/solana_transfer_fee_audit.json (нужен getAccountInfo)"
    bps = info.get("ставка_комиссии_bps")
    return (bool(bps), None) if bps is not None else (False, None)


def taxable_bucket(trade: dict, tax_catalog: dict) -> str | None:
    is_tax, why_not = is_taxable_from_catalog(trade.get("mint"), tax_catalog)
    if is_tax is None:
        return None
    return "taxable_mint" if is_tax else "non_taxable_mint"


CACHE_FEATURES = {
    "quote_kind": ("котировка (SOL/стейбл/другое)", quote_kind),
    "pool_kind": ("тип пула (резервный/нерезервный)", pool_kind),
    "split_route": ("маршрут дробился на несколько путей", split_route_flag),
    "failed_same_block": ("в блоке есть неудачные транзакции", failed_same_block_flag),
}


def group_by_feature(trades: list, feature_fn, *, horizon_field: str = "growth_30s") -> dict:
    """Для каждого значения feature_fn(сделка) -- горизонт роста. НИ ОДНА
    сделка не теряется: неизвестное значение признака уходит в свою группу
    "unknown", а не выбрасывается из выборки (см. self_test, проверка
    "группировка не теряет сделки": сумма n_total по группам == len(trades)).
    Деление на пустую группу не падает: n==0 -> mean_growth_pct=None."""
    buckets: dict = {}
    for t in trades:
        key = feature_fn(t)
        key_s = "unknown" if key is None else str(key)
        b = buckets.setdefault(key_s, {"n_total": 0, "values": []})
        b["n_total"] += 1
        v = t.get(horizon_field)
        if v is not None:
            b["values"].append(v)
    out = {}
    for key_s, b in buckets.items():
        vals = b["values"]
        n = len(vals)
        out[key_s] = {
            "n_total": b["n_total"],
            "n_known_growth": n,
            "mean_growth_pct": round(pct_from_ratio(mean(vals)), 3) if n else None,
            "median_growth_pct": round(pct_from_ratio(median(vals)), 3) if n else None,
        }
    return out


def flag_toxic_groups(all_groups: dict, *, threshold_pct: float = -5.0, min_n: int = 10) -> list:
    """[{feature, group, n_known_growth, mean_growth_pct}] -- группы со
    средним НИЖЕ threshold_pct и n_known_growth >= min_n. Пустой список --
    честный ответ "в этой выборке такой группы нет", а не повод придумать."""
    out = []
    for feature_name, groups in all_groups.items():
        for group_key, g in groups.items():
            if g["n_known_growth"] >= min_n and g["mean_growth_pct"] is not None \
                    and g["mean_growth_pct"] < threshold_pct:
                out.append({"feature": feature_name, "group": group_key,
                           "n_known_growth": g["n_known_growth"],
                           "mean_growth_pct": g["mean_growth_pct"]})
    return sorted(out, key=lambda r: r["mean_growth_pct"])


def build_b_groups(trades: list, tax_catalog: dict) -> dict:
    """Группировка по ВСЕМ признакам, которые в этом прогоне доступны хотя
    бы частично из кэша (без единого обращения к цепи)."""
    out = {name: group_by_feature(trades, fn) for name, (_, fn) in CACHE_FEATURES.items()}
    out["taxable"] = group_by_feature(trades, lambda t: taxable_bucket(t, tax_catalog))
    imp_thr = 1.0  # spot_after == price_0 -- порог "хоть какой-то положительный импакт"
    spend_vals = [t["spend_sol_equiv"] for t in trades if t.get("spend_sol_equiv") is not None]
    spend_med = median(spend_vals) or 0.0
    crowd_vals = [t["crowd_30s"] for t in trades if t.get("crowd_30s") is not None]
    crowd_med = median(crowd_vals) or 0.0
    out["impact_source"] = group_by_feature(trades, lambda t: impact_bucket(t, imp_thr))
    out["spend_size"] = group_by_feature(trades, lambda t: spend_bucket(t, spend_med))
    out["crowd_30s"] = group_by_feature(trades, lambda t: crowd_bucket(t, crowd_med))
    return out


# ============================================================ B: признаки из цепи

def mint_chain_info(rpc_call, mint: str) -> dict:
    """1 x getAccountInfo(mint, jsonParsed). Даёт: программу токена, mint/
    freeze authority (отозваны, если None), а для Token-2022 с расширением
    transferFeeConfig -- налог (bps, потолок, ПРЕЖНЮЮ ставку) и отозвано ли
    право эту ставку менять (transferFeeConfigAuthority == None). Формат
    разбора -- тот же, что в analysis/solana_transfer_fee_audit.py:mint_info
    (independent read, тот файл не импортирован, чтобы не тянуть его CLI/
    кэш-файлы в этот модуль)."""
    try:
        r = rpc_call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    except RuntimeError as exc:
        return {"ok": False, "why_not": f"getAccountInfo не отдался: {str(exc)[:160]}", "credits": 1}
    val = (r or {}).get("value")
    if not val:
        return {"ok": False, "why_not": "getAccountInfo вернул null -- аккаунта минта нет", "credits": 1}
    info = (((val.get("data") or {}).get("parsed") or {}).get("info") or {})
    if not info:
        return {"ok": False, "why_not": "getAccountInfo не разобрал минт (не SPL Token/Token-2022 mint)",
                "credits": 1}
    exts = info.get("extensions") or []
    tax_state = next((e.get("state") or {} for e in exts
                      if isinstance(e, dict) and e.get("extension") == "transferFeeConfig"), None)
    out = {
        "ok": True, "credits": 1,
        "mint": mint, "token_program": val.get("owner"),
        "supply_raw": info.get("supply"), "decimals": info.get("decimals"),
        "mint_authority": info.get("mintAuthority"),
        "mint_authority_revoked": info.get("mintAuthority") is None,
        "freeze_authority": info.get("freezeAuthority"),
        "freeze_authority_revoked": info.get("freezeAuthority") is None,
        "extensions": [e.get("extension") for e in exts if isinstance(e, dict)],
        "is_taxable": False, "tax_bps": None, "tax_bps_previous": None,
        "tax_max_fee_raw": None, "tax_authority": None, "tax_authority_revoked": None,
    }
    if tax_state:
        newer = tax_state.get("newerTransferFee") or {}
        out["tax_bps"] = newer.get("transferFeeBasisPoints")
        out["tax_bps_previous"] = (tax_state.get("olderTransferFee") or {}).get("transferFeeBasisPoints")
        out["tax_max_fee_raw"] = newer.get("maximumFee")
        out["tax_authority"] = tax_state.get("transferFeeConfigAuthority")
        out["tax_authority_revoked"] = tax_state.get("transferFeeConfigAuthority") is None
        out["is_taxable"] = bool(out["tax_bps"])
    return out


def top10_holder_share(rpc_call, mint: str, supply_raw: str | int | None) -> dict:
    """1 x getTokenLargestAccounts(mint). Доля топ-10 = сумма первых 10
    аккаунтов / supply. supply берётся из mint_chain_info -- отдельного
    вызова на него не нужно. Нет supply -- доля не считается (why_not), а
    не делится на 0."""
    try:
        r = rpc_call("getTokenLargestAccounts", [mint])
    except RuntimeError as exc:
        return {"ok": False, "why_not": f"getTokenLargestAccounts не отдался: {str(exc)[:160]}", "credits": 1}
    accs = (r or {}).get("value") or []
    if not accs:
        return {"ok": False, "why_not": "getTokenLargestAccounts вернул пусто", "credits": 1}
    top10 = accs[:10]
    top10_raw = sum(int(a.get("amount") or 0) for a in top10)
    supply = int(supply_raw) if supply_raw not in (None, "") else 0
    if not supply:
        return {"ok": False, "why_not": "supply минта неизвестен -- доля топ-10 не считается",
                "top10_raw": top10_raw, "credits": 1}
    return {"ok": True, "credits": 1, "top10_raw": top10_raw, "supply_raw": supply,
            "top10_share": top10_raw / supply, "n_accounts_returned": len(accs)}


def address_age(rpc_call, address: str, *, reference_block_time: int | None = None,
                max_pages: int = AGE_DEFAULT_MAX_PAGES, page_limit: int = AGE_PAGE_LIMIT) -> dict:
    """До max_pages страниц getSignaturesForAddress НАЗАД (before=...), пока
    страница не окажется короче page_limit (значит, дошли до самой первой
    подписи адреса) или не кончится потолок страниц. Каждая страница -- 1
    кредит, credits в ответе = сколько реально ушло на ЭТОТ вызов.

    Дошли до конца -> возраст = reference_block_time - oldest_block_time
    (в секундах), known=True. Не дошли -> known=False с точным числом
    прочитанных страниц: возраст "неизвестен", а не "0" и не "очень старый"."""
    before = None
    pages = 0
    oldest = None
    while pages < max_pages:
        params = [address, {"limit": page_limit, **({"before": before} if before else {})}]
        try:
            page = rpc_call("getSignaturesForAddress", params)
        except RuntimeError as exc:
            return {"ok": False, "why_not": f"getSignaturesForAddress не отдался: {str(exc)[:160]}",
                    "credits": pages + 1, "pages_read": pages}
        pages += 1
        page = page or []
        if not page:
            break
        oldest = page[-1]
        if len(page) < page_limit:
            age_s = (None if reference_block_time is None or oldest.get("blockTime") is None
                    else reference_block_time - oldest["blockTime"])
            return {"ok": True, "credits": pages, "pages_read": pages, "fully_paginated": True,
                    "oldest_signature": oldest.get("signature"),
                    "oldest_block_time": oldest.get("blockTime"), "age_seconds": age_s}
        before = oldest.get("signature")
    if oldest is None:
        return {"ok": False, "why_not": "у адреса нет истории подписей", "credits": pages, "pages_read": pages}
    return {"ok": False, "credits": pages, "pages_read": pages,
            "why_not": f"история не выкачана за {max_pages} страниц ({max_pages * page_limit}+ подписей) "
                      "-- возраст неизвестен, а не 'старый'",
            "oldest_block_time_so_far": oldest.get("blockTime")}


def _buyers_in_block(rpc_call, slot: int, mint: str, exclude_wallet: str | None) -> dict:
    """1 x getBlock(slot, full/jsonParsed) -> кошельки-подписанты, у
    которых баланс `mint` вырос в этом блоке. Правило 1 из c2_common.mint_buyers
    (подписант, баланс минта вырос); правило 2 (получатель не подписант, но
    подписант заплатил) НЕ повторено -- это немного занижает счёт для
    маршрутов с посредником-получателем, отмечено в возвращаемом note, а не
    молча. Тариф Helius не зависит от transactionDetails (то же наблюдение,
    что в c2_followers_growth.py) -- полный уровень детализации не дороже."""
    opts = {"encoding": "jsonParsed", "transactionDetails": "full", "rewards": False,
            "maxSupportedTransactionVersion": 1, "commitment": "finalized"}
    try:
        blk = rpc_call("getBlock", [slot, opts])
    except RuntimeError as exc:
        return {"ok": False, "why_not": f"getBlock не отдался: {str(exc)[:160]}", "credits": 1}
    txs = (blk or {}).get("transactions") or []
    buyers = set()
    for tx in txs:
        meta = (tx or {}).get("meta") or {}
        if meta.get("err") is not None:
            continue
        keys = [k.get("pubkey") if isinstance(k, dict) else k
                for k in (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or [])]
        signers = {k.get("pubkey") for k in
                  (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or [])
                  if isinstance(k, dict) and k.get("signer")}
        delta: dict = {}
        for when, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
            for b in meta.get(when) or []:
                if b.get("mint") != mint or not b.get("owner"):
                    continue
                amt = int((b.get("uiTokenAmount") or {}).get("amount") or 0)
                delta[b["owner"]] = delta.get(b["owner"], 0) + sign * amt
        for owner, d in delta.items():
            if d > 0 and owner in signers and owner != exclude_wallet:
                buyers.add(owner)
    return {"ok": True, "credits": 1, "buyer_wallets": sorted(buyers), "n_buyers": len(buyers),
            "note": "правило 2 (получатель != плательщик) не разобрано -- возможное занижение"}


def slot_crowd(rpc_call, slot_source: int, mint: str, source_wallet: str) -> dict:
    """Толпа В СЛОТЕ ИСТОЧНИКА (S) и В СЛЕДУЮЩЕМ (S+1) -- 2 x getBlock, 2
    кредита на сделку. Это НЕ то же самое, что crowd_30s в кэше задачи A
    (там окно 30 секунд, может быть много слотов) -- отдельный, более узкий
    признак, которого в кэше нет вовсе."""
    s0 = _buyers_in_block(rpc_call, slot_source, mint, source_wallet)
    s1 = _buyers_in_block(rpc_call, slot_source + 1, mint, source_wallet)
    return {"slot_source": s0, "slot_source_plus_1": s1,
            "credits": (s0.get("credits", 0) + s1.get("credits", 0))}


def estimate_b_credit_cost(n_trades: int, n_unique_mints: int, n_unique_pools: int, *,
                            age_max_pages: int = AGE_DEFAULT_MAX_PAGES) -> dict:
    """Явная стоимость ОДНОГО прогона всех B-признаков цепи на n_trades
    сигналов (верхняя граница -- возраст может кончиться раньше потолка
    страниц). Тариф -- см. константы CREDITS_DEFAULT/CREDITS_GET_PROGRAM_ACCOUNTS
    в шапке файла (переписаны из analysis/solana_rpc_client.py)."""
    rows = {
        "getTransaction_refetch_route_and_exact_pool_depth": n_trades * CREDITS_DEFAULT,
        "getAccountInfo_mint_authority_tax_max_fee": n_unique_mints * CREDITS_DEFAULT,
        "getTokenLargestAccounts_top10_holders": n_unique_mints * CREDITS_DEFAULT,
        "getSignaturesForAddress_mint_age_capped_pages": (
            n_unique_mints * age_max_pages * CREDITS_DEFAULT),
        "getSignaturesForAddress_pool_age_capped_pages": (
            n_unique_pools * age_max_pages * CREDITS_DEFAULT),
        "getBlock_x2_slot_crowd_S_and_S_plus_1_per_trade": n_trades * 2 * CREDITS_DEFAULT,
    }
    total = sum(rows.values())
    return {
        "n_trades": n_trades, "n_unique_mints": n_unique_mints, "n_unique_pools": n_unique_pools,
        "age_max_pages": age_max_pages, "rows_credits": rows,
        "total_credits_upper_bound": total,
        "note": ("возраст -- верхняя граница: свежие минты обычно укладываются в 1 "
                "страницу (<=1000 подписей), старые/ликвидные упрутся в age_max_pages и "
                "тогда возраст остаётся неизвестен, а не оценкой сверху; getAccountInfo на "
                "ПРОМЕЖУТОЧНЫЕ токены маршрута (налог по маршруту, число шагов) сюда не "
                "включён -- он неизвестен, пока не перечитана getTransaction, и по каталогу "
                "data/solana_tax_groups.json такие маршруты редки (16 из 367 в независимой "
                "выборке исполнителя, ~4.4%), т.е. добавка мала, но не 0"),
    }


# ============================================================ C1: источник как выход

def c1_requirements(sources_meta: list, *, empirical_credits_per_window: float | None = None) -> dict:
    """Продаж источника в кэше нет вовсе (задача A -- только его ПОКУПКИ,
    первый вход >= 2 SOL-экв). Чтобы проверить "продаёт ли источник в
    течение X секунд после своей покупки на чужих покупках":

      1. getSignaturesForAddress(источник) -- вся его история подписей за
         период (уже известна из задачи A: n_signatures на источник), затем
      2. getTransaction на КАЖДУЮ подпись, которая не входит в уже
         классифицированные покупки задачи A (classify_tx их не смотрел),
         чтобы отделить продажи от прочего (переводы, другие DEX-действия);
      3. для каждой найденной продажи -- окно X секунд ДО неё в том же
         пуле: кто в этом окне покупал (тот же метод, что задача A уже
         применяла для crowd_30s ПОСЛЕ покупки -- getSignaturesForAddress
         по pool_vault + getTransaction на кандидатов), и какая доля объёма
         продажи пришлась на эти покупки.

    Шаг 3 по цене -- НЕ оценка с нуля: задача A потратила credits_this_run
    на n_trades покупок этих же 18 источников, то есть уже есть измеренная
    цена ОДНОГО окна "толпа+рост" на этом же кэше (см. crowd_metric_*.json:
    credits_this_run / число покупок). Используем как эмпирический якорь,
    а не как формулу заново."""
    total_sig = sum(s.get("n_signatures") or 0 for s in sources_meta)
    total_trades = sum(s.get("n_trades") or 0 for s in sources_meta)
    pages_step1 = sum(-(-((s.get("n_signatures") or 0)) // AGE_PAGE_LIMIT) for s in sources_meta)
    non_buy_sigs = max(0, total_sig - total_trades)
    step1 = pages_step1 * CREDITS_DEFAULT
    step2 = non_buy_sigs * CREDITS_DEFAULT
    out = {
        "n_sources": len(sources_meta), "total_signatures_7d": total_sig,
        "total_known_buys_7d": total_trades,
        "step1_signatures_pages_credits": step1,
        "step2_classify_non_buy_signatures_credits_upper_bound": step2,
        "step2_note": ("верхняя граница: non_buy_sigs = все подписи источника минус уже "
                      "известные покупки -- часть из них НЕ продажи (переводы, апрувы), "
                      "но дешевле узнать это, чем предполагать"),
    }
    if empirical_credits_per_window is not None:
        # Якорь: столько кредитов задача A реально потратила НА ОДНУ покупку
        # (окно 30с толпы+роста) -- шаг 3 по объёму сравним с ним по сложности.
        step3_per_sell = empirical_credits_per_window
        out["step3_empirical_credits_per_sell_window"] = round(step3_per_sell, 1)
        out["step3_source_of_anchor"] = ("crowd_metric_*.json: credits_this_run / число покупок "
                                         "в этом же прогоне -- измеренная, не предполагаемая цена")
        out["step3_note"] = ("итоговая цена шага 3 = эта цена x число НАЙДЕННЫХ продаж, которое "
                            "неизвестно ДО шага 2 -- поэтому дать один финальный кредит без "
                            "прогона шагов 1-2 нельзя, только цену шагов 1-2 и цену ЗА ОДНУ продажу")
    out["total_credits_steps_1_2"] = step1 + step2
    return out


# ============================================================ C3: снайперы

def followers_frequency(followers: list) -> list:
    """По каждому кошельку-подписчику: сколько раз он встал сразу за
    источником, за какими источниками, и разброс платы (медиана, p25/p75).
    tip_core_lamports -- если он есть, это подмножество tip_lamports
    (см. c2_block_position: tip_core -- переводы на адрес из УЖЕ известного
    короткого списка "чаевых ядра", остальное -- широкий эвристический
    список; здесь просто переносится как отдельное поле, не пересчитывается)."""
    by_wallet: dict = {}
    for f in followers:
        by_wallet.setdefault(f.get("wallet"), []).append(f)
    rows = []
    for wallet, rs in by_wallet.items():
        cu = [r.get("cu_price_micro") for r in rs if r.get("cu_price_micro") is not None]
        pr = [r.get("priority_lamports") for r in rs if r.get("priority_lamports") is not None]
        tip = [r.get("tip_lamports") for r in rs if r.get("tip_lamports") is not None]
        total_fee = [ (r.get("priority_lamports") or 0) + (r.get("tip_lamports") or 0) for r in rs]
        rows.append({
            "wallet": wallet, "n_appearances": len(rs),
            "sources": sorted({r.get("source") for r in rs if r.get("source")}),
            "cu_price_micro_median": median(cu), "cu_price_micro_p25": quantile(cu, 0.25),
            "cu_price_micro_p75": quantile(cu, 0.75),
            "priority_lamports_median": median(pr),
            "tip_lamports_median": median(tip),
            "total_fee_lamports_cv": cv(total_fee),
        })
    return sorted(rows, key=lambda r: -r["n_appearances"])


def named_sniper_report(followers: list, addresses: tuple = NAMED_SNIPERS) -> dict:
    out = {}
    for addr in addresses:
        rows = [f for f in followers if f.get("wallet") == addr]
        if not rows:
            out[addr] = {"found": False, "n_appearances": 0,
                        "why_not": "кошелёк не встретился ни разу в переданном followers[] за этот период"}
            continue
        cu = [r.get("cu_price_micro") for r in rows if r.get("cu_price_micro") is not None]
        pr = [r.get("priority_lamports") for r in rows if r.get("priority_lamports") is not None]
        tip = [r.get("tip_lamports") for r in rows if r.get("tip_lamports") is not None]
        out[addr] = {
            "found": True, "n_appearances": len(rows),
            "sources": sorted({r.get("source") for r in rows if r.get("source")}),
            "rank_after_source_values": [r.get("rank_after_source") for r in rows],
            "cu_price_micro_values": cu, "priority_lamports_values": pr, "tip_lamports_values": tip,
            "cu_price_micro_median": median(cu), "priority_lamports_median": median(pr),
            "tip_lamports_median": median(tip),
        }
    return out


def fee_model_hypothesis(followers: list) -> dict:
    """"Платят фиксированно или пропорционально объёму" -- по кредитам НЕ
    решаемо в кэше: объём копировщика (сколько SOL/токена он сам купил) в
    followers[] отсутствует (в записи есть его priority/tip/cu, но не
    quote_spend). Единственный доступный здесь суррогат -- коэффициент
    вариации платы по кошельку: низкий -- в пользу фиксированной суммы,
    высокий -- против неё (см. cv()). Это НЕ доказывает пропорциональность
    объёму -- только не-фиксированность."""
    fee_rows = [(r.get("priority_lamports") or 0) + (r.get("tip_lamports") or 0) for r in followers
               if r.get("priority_lamports") is not None or r.get("tip_lamports") is not None]
    cu_rows = [r.get("cu_price_micro") for r in followers if r.get("cu_price_micro") is not None]
    n_unique_followers = len({r.get("wallet") for r in followers if r.get("wallet")})
    return {
        "n_follower_rows": len(followers), "n_unique_followers": n_unique_followers,
        "total_fee_lamports_cv_all": cv(fee_rows), "cu_price_micro_cv_all": cv(cu_rows),
        "volume_of_follower_own_trade": {
            "in_cache": False,
            "why_not": ("followers[] хранит cu_price_micro/priority_lamports/tip_lamports его "
                       "транзакции, но не quote_spend (сколько SOL/WSOL/USDC он сам заплатил) -- "
                       "этого поля в c2_block_position нет"),
            "cheap_call_available": True,
            "how": ("getTransaction(signature) на КАЖДУЮ запись followers[] -- 1 кредит; "
                   "объём копировщика тем же методом, что и в задаче A/F "
                   "(quote_spend: SOL+WSOL+USDC/курс по его собственным балансам счёта)"),
            "credits_for_this_run": len(followers),
        },
        "note": ("низкий total_fee_lamports_cv_all при высоком разбросе cu_price_micro_cv_all "
                "говорит в пользу фиксированной СУММЫ платы (не привязанной к тому, что менял "
                "cu_price при разном cu_limit); высокий total_fee_lamports_cv_all -- против "
                "фиксированной суммы, но БЕЗ объёма нельзя отличить 'пропорционально объёму' от "
                "'зависит от чего-то ещё (загрузка сети, конкуренция за слот)'. Наблюдение по "
                "числам этого прогона, не готовый ответ владельцу."),
    }


# ============================================================ C4: манипуляции

def tax_hike_mints(tax_catalog: dict) -> list:
    """Минты, у которых ставка_комиссии_bps (текущая эпоха) отличается от
    ставка_комиссии_прежняя_bps (предыдущая) -- то есть налог МЕНЯЛСЯ.
    Оговорка обязательна: это разница между двумя эпохами Token-2022 на
    момент чтения каталога, а не обязательно "прямо перед/после нашей
    сделки" -- со временем сделки не сверяется здесь."""
    out = []
    for mint, info in tax_catalog.items():
        cur = info.get("ставка_комиссии_bps")
        prev = info.get("ставка_комиссии_прежняя_bps")
        if prev is not None and cur is not None and cur != prev:
            out.append({"mint": mint, "tax_bps_previous": prev, "tax_bps_current": cur,
                       "max_fee_raw": info.get("потолок_комиссии"), "epoch": info.get("эпоха_ставки"),
                       "symbol": info.get("тикер"), "name": info.get("название")})
    return out


def tax_hike_trade_cost(hike_mints: list, transfer_fee_rows: list) -> dict:
    """Среди СВОИХ закрытых сделок (data/solana_transfer_fee_audit.json:
    строки -- другая выборка, чем crowd_metric, у исполнителя, не у 18
    источников) -- те, что задели хоть один из hike_mints (как цель или как
    промежуточный токен маршрута), и сколько SOL реально удержано на них."""
    hike_set = {m["mint"] for m in hike_mints}
    hits = []
    for row in transfer_fee_rows:
        touched = {row.get("минт")} | set(row.get("промежуточные_таксируемые") or [])
        if touched & hike_set:
            hits.append({
                "task": row.get("задача"), "mint": row.get("минт"),
                "hike_mint_role": "цель" if row.get("минт") in hike_set else "промежуточный",
                "withheld_sol": row.get("удержано_sol"), "net_sol": row.get("net_sol"),
                "gross_pct": row.get("gross_pct"), "held_seconds": row.get("held_seconds"),
                "buy_signature": row.get("подпись_покупки"),
            })
    total_withheld = sum(h["withheld_sol"] for h in hits if h.get("withheld_sol") is not None)
    return {"n_hike_mints": len(hike_mints), "n_trades_touching_hike_mints": len(hits),
            "total_withheld_sol": round(total_withheld, 9), "trades": hits,
            "caveat": ("это СВОИ прошлые сделки исполнителя (не 346 покупок 18 источников из "
                      "crowd_metric) -- каталог по цепи есть только для минтов, которые уже "
                      "встречались исполнителю; момент подъёма ставки внутри held_seconds не "
                      "восстановлен, поэтому причинность 'сделка пострадала ИЗ-ЗА подъёма именно "
                      "в её окне' не установлена -- установлено только совпадение минта")}


def liquidity_drain_from_cache(trades: list) -> dict:
    """Сколько из 346 покупок задача A НЕ смогла оценить через 30с именно
    потому, что ликвидность пула была снята до точки (why_no_growth_30s/
    why_no_after начинаются с "ликвидность пула снята") -- прямой,
    НЕ придуманный признак слива ликвидности, уже посчитанный в задаче A,
    просто не сведённый в отдельную цифру."""
    hits = []
    for t in trades:
        reasons = [t.get("why_no_growth_30s"), t.get("why_no_after")]
        if any((r or "").startswith(LIQUIDITY_DRAIN_PREFIX) for r in reasons):
            hits.append({"signature": t.get("signature"), "mint": t.get("mint"),
                        "source": t.get("_source_remark") or t.get("_source_address"),
                        "spend_sol_equiv": t.get("spend_sol_equiv"),
                        "why_no_growth_30s": t.get("why_no_growth_30s"),
                        "why_no_after": t.get("why_no_after")})
    return {"n_trades_total": len(trades), "n_liquidity_drained": len(hits), "trades": hits,
            "note": ("для этих сделок growth_30s == None -- в таблицу 'среднее с/без признака' "
                    "они попадают строкой unknown с mean_growth_pct=None, а НЕ как '-100%': "
                    "числового исхода у снятой ликвидности здесь нет, есть только сам факт")}


def route_asymmetry_summary(tax_groups: dict) -> dict:
    """Контекст к налогу по маршруту из data/solana_tax_groups.json --
    берётся ТОЛЬКО как перевод известных полей в ascii-ключи, а не
    встраивается сырым словарём (у исходного файла ключи кириллицей,
    здесь это запрещено правилом "ключи JSON только ASCII")."""
    raw = (tax_groups or {}).get("асимметрия_маршрутов") or {}

    def pick(cyr_key):
        sub = raw.get(cyr_key)
        if not sub:
            return None
        return {
            "n_trades": sub.get("сделок"), "share_profitable": sub.get("доля_в_плюс"),
            "median_signal_pct": sub.get("медиана_сигнала_pct"),
            "mean_signal_pct": sub.get("среднее_сигнала_pct"),
            "invested_sol": sub.get("вложено_sol"),
            "onchain_result_sol": sub.get("результат_по_цепи_sol"),
            "tax_sol": sub.get("налогов_sol"),
        }

    return {
        "direct_route": pick("обе ноги прямые"),
        "via_taxable_intermediate": pick("обе ноги через промежуточный"),
        "sell_only_via_intermediate": pick("через промежуточный только продажа"),
        "source_file": "data/solana_tax_groups.json",
        "note": ("П&Л ИСПОЛНИТЕЛЯ по его собственным закрытым сделкам (другая выборка, чем "
                "346 покупок 18 источников из crowd_metric) -- контекст к 'налог по маршруту', "
                "не то же самое измерение, что growth_30s"),
    }


def circular_wash_note() -> dict:
    """Круговые/отмывочные сделки: НЕ вычислимо из переданного кэша ни в
    каком приближении -- у нас есть только ПОКУПКИ источников (задача A) и
    ИХ ЖЕ последующие копировщики (задача C2/block_position), но нет ни
    продаж источника (см. c1_requirements), ни его связей с другими
    кошельками вне списка 18 источников. Круговая/отмывочная схема по
    определению требует видеть ОБЕ стороны (кто продал -> кому -> обратно),
    чего в этом кэше нет вовсе."""
    return {
        "computable_from_cache": False,
        "why_not": ("нужны продажи источника (см. c1_requirements) И покупки/продажи ДРУГИХ "
                   "кошельков того же минта в достаточном окне, чтобы увидеть, что монеты "
                   "вернулись к тому же контролирующему кошельку -- ни то, ни другое не входит "
                   "в crowd_metric/c2_block_position"),
        "what_is_needed": ("getSignaturesForAddress по каждому подозрительному кошельку + "
                          "getTransaction на каждую подпись, дальше -- граф переводов минта "
                          "между кошельками; стоимость пропорциональна числу подписей "
                          "подозрительных кошельков, конкретной оценки без списка кошельков дать "
                          "нельзя (см. c1_requirements как образец такой оценки для ОДНОГО "
                          "конкретного кошелька)"),
    }


# ============================================================ сборка отчёта

def build_report(args) -> dict:
    crowd_path = Path(args.crowd)
    followers_path = Path(args.followers)
    trades, sources_meta = load_crowd_trades(crowd_path)
    fw = load_followers(followers_path)
    tax_catalog = load_tax_catalog(Path(args.tax_catalog))
    transfer_fee_rows = load_transfer_fee_rows(Path(args.tax_catalog))
    tax_groups = load_tax_groups(Path(args.tax_groups))

    n_trades = len(trades)
    n_mints = len({t["mint"] for t in trades if t.get("mint")})
    n_pools = len({t["pool_vault"] for t in trades if t.get("pool_vault")})

    b_groups = build_b_groups(trades, tax_catalog)
    toxic = flag_toxic_groups(b_groups, threshold_pct=args.threshold_pct, min_n=args.min_n)
    credit_cost = estimate_b_credit_cost(n_trades, n_mints, n_pools, age_max_pages=args.age_max_pages)

    growth_known = sum(1 for t in trades if t.get("growth_30s") is not None)
    n_tax_known = sum(1 for t in trades if t.get("mint") in tax_catalog)

    empirical_anchor = None
    # эмпирическая цена одного окна "толпа+рост" -- credits_this_run самой
    # задачи A, если он записан в её файле (реальный прогон, не оценка).
    try:
        raw = json.loads(crowd_path.read_text(encoding="utf-8"))
        credits_a = raw.get("credits_this_run")
        if credits_a and n_trades:
            empirical_anchor = credits_a / n_trades
    except (OSError, ValueError):
        pass

    followers = fw["followers"]
    hike_mints = tax_hike_mints(tax_catalog)

    report = {
        "schema_version": 1,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "inputs": {
            "crowd_file": str(crowd_path), "followers_file": str(followers_path),
            "tax_catalog_file": args.tax_catalog, "tax_groups_file": args.tax_groups,
            "n_trades": n_trades, "n_unique_mints": n_mints, "n_unique_pools": n_pools,
            "n_sources": len(sources_meta),
        },
        "task_b_toxic_features": {
            "coverage": {
                "growth_30s_known": growth_known, "growth_30s_total": n_trades,
                "tax_known_from_catalog": n_tax_known, "tax_catalog_size": len(tax_catalog),
            },
            "feature_catalog": [
                {"name": "quote_kind", "source": "cache", "field": "quote_mint"},
                {"name": "pool_kind", "source": "cache", "field": "pool_program"},
                {"name": "split_route", "source": "cache", "field": "split_route"},
                {"name": "failed_same_block", "source": "cache", "field": "failed_same_block_wallets"},
                {"name": "impact_source (суррогат доли глубины)", "source": "cache_partial",
                 "field": "impact_source", "n_known": sum(1 for t in trades if t.get("impact_source") is not None)},
                {"name": "spend_size", "source": "cache", "field": "spend_sol_equiv"},
                {"name": "crowd_30s (суррогат толпы в слоте)", "source": "cache_partial",
                 "field": "crowd_30s", "n_known": sum(1 for t in trades if t.get("crowd_30s") is not None)},
                {"name": "taxable (налог самого токена)", "source": "cache_partial",
                 "field": "data/solana_transfer_fee_audit.json:минты", "n_known": n_tax_known},
                {"name": "tax_max_fee", "source": "cache_partial_or_chain",
                 "note": "потолок есть в том же каталоге для тех же n_tax_known минтов"},
                {"name": "route_tax_bps (налог по маршруту)", "source": "chain_required",
                 "rpc": ["getTransaction", "getAccountInfo"]},
                {"name": "tax_authority_revoked", "source": "chain_required", "rpc": ["getAccountInfo"]},
                {"name": "mint_authority", "source": "chain_required", "rpc": ["getAccountInfo"]},
                {"name": "freeze_authority", "source": "chain_required", "rpc": ["getAccountInfo"]},
                {"name": "route_hops (число шагов маршрута)", "source": "chain_required",
                 "rpc": ["getTransaction"]},
                {"name": "pool_depth_sol_equiv (глубина пула, абсолют)", "source": "chain_required",
                 "rpc": ["getTransaction"]},
                {"name": "top10_holder_share", "source": "chain_required",
                 "rpc": ["getTokenLargestAccounts", "getAccountInfo (supply)"]},
                {"name": "token_age / pool_age", "source": "chain_required",
                 "rpc": ["getSignaturesForAddress"]},
                {"name": "bundle_launch (несколько кошельков в первом слоте)",
                 "source": "chain_required", "rpc": ["getSignaturesForAddress", "getBlock"]},
                {"name": "slot_crowd_S_and_S+1", "source": "chain_required", "rpc": ["getBlock", "getBlock"]},
            ],
            "credit_cost_full_chain_pass": credit_cost,
            "groups_mean_growth_with_vs_without": b_groups,
            "groups_below_threshold": {"threshold_pct": args.threshold_pct, "min_n": args.min_n,
                                       "matches": toxic},
        },
        "task_c1_source_as_exit": c1_requirements(sources_meta, empirical_credits_per_window=empirical_anchor),
        "task_c3_snipers": {
            "n_follower_rows": len(followers), "n_unique_followers": len({f.get("wallet") for f in followers if f.get("wallet")}),
            "top_wallets_by_frequency": followers_frequency(followers)[:15],
            "named_snipers": named_sniper_report(followers, NAMED_SNIPERS),
            "fee_model_hypothesis": fee_model_hypothesis(followers),
        },
        "task_c4_manipulation": {
            "tax_hike": {
                "hike_mints": hike_mints,
                "trade_cost": tax_hike_trade_cost(hike_mints, transfer_fee_rows),
            },
            "liquidity_drain": liquidity_drain_from_cache(trades),
            "circular_wash_trades": circular_wash_note(),
            "route_asymmetry_context_from_solana_tax_groups": route_asymmetry_summary(tax_groups),
        },
    }
    return report


def print_summary(report: dict) -> None:
    tb = report["task_b_toxic_features"]
    print("=== Задача B: признаки токсичных токенов (кэш-часть) ===")
    print(f"сделок всего: {report['inputs']['n_trades']}, growth_30s известен у "
          f"{tb['coverage']['growth_30s_known']}")
    print(f"{'признак':22} {'группа':28} {'n_total':8} {'n_growth':9} {'mean%':9} {'median%':9}")
    for feature, groups in tb["groups_mean_growth_with_vs_without"].items():
        for gk, g in groups.items():
            m = "-" if g["mean_growth_pct"] is None else f"{g['mean_growth_pct']:.2f}"
            md = "-" if g["median_growth_pct"] is None else f"{g['median_growth_pct']:.2f}"
            print(f"{feature[:22]:22} {gk[:28]:28} {g['n_total']:8} {g['n_known_growth']:9} {m:9} {md:9}")
    matches = tb["groups_below_threshold"]["matches"]
    print(f"\nгрупп со средним ниже {tb['groups_below_threshold']['threshold_pct']}% "
          f"при n>={tb['groups_below_threshold']['min_n']}: {len(matches)}")
    for m in matches:
        print(f"  {m['feature']} / {m['group']}: n={m['n_known_growth']} mean={m['mean_growth_pct']:.2f}%")
    cc = tb["credit_cost_full_chain_pass"]
    print(f"\nполный прогон B-признаков цепи (верхняя граница): "
          f"{cc['total_credits_upper_bound']} кредитов на {cc['n_trades']} сигналов")

    print("\n=== Задача C1: источник как выход ===")
    c1 = report["task_c1_source_as_exit"]
    print(f"шаги 1-2 (список продаж): {c1['total_credits_steps_1_2']} кредитов "
          f"({c1['n_sources']} источников, {c1['total_signatures_7d']} подписей за 7д)")
    if "step3_empirical_credits_per_sell_window" in c1:
        print(f"шаг 3 (окно на 1 продажу, эмпирический якорь задачи A): "
              f"{c1['step3_empirical_credits_per_sell_window']} кредитов/продажу")

    print("\n=== Задача C3: снайперы ===")
    c3 = report["task_c3_snipers"]
    print(f"followers-строк: {c3['n_follower_rows']}, уникальных кошельков: {c3['n_unique_followers']}")
    for w in c3["top_wallets_by_frequency"][:5]:
        print(f"  {w['wallet'][:10]} n={w['n_appearances']} источники={w['sources']}")
    for addr, r in c3["named_snipers"].items():
        if r["found"]:
            print(f"  {addr[:10]}: найден {r['n_appearances']} раз, tip_median={r['tip_lamports_median']}")
        else:
            print(f"  {addr[:10]}: {r['why_not']}")

    print("\n=== Задача C4: манипуляции ===")
    c4 = report["task_c4_manipulation"]
    print(f"подъём налога: минтов={c4['tax_hike']['trade_cost']['n_hike_mints']}, "
          f"своих сделок задело={c4['tax_hike']['trade_cost']['n_trades_touching_hike_mints']}, "
          f"удержано SOL={c4['tax_hike']['trade_cost']['total_withheld_sol']}")
    ld = c4["liquidity_drain"]
    print(f"слив ликвидности до +30с: {ld['n_liquidity_drained']} из {ld['n_trades_total']} покупок")
    print(f"круговые/отмывочные: {c4['circular_wash_trades']['why_not']}")


# ============================================================ CLI

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--crowd", default="", help="crowd_metric_*.json задачи A (иначе последний в data/)")
    ap.add_argument("--followers", default="", help="c2_block_position_*.json (иначе последний в data/)")
    ap.add_argument("--tax-catalog", default=str(DATA / "solana_transfer_fee_audit.json"))
    ap.add_argument("--tax-groups", default=str(DATA / "solana_tax_groups.json"))
    ap.add_argument("--out", default=str(DATA / "night_toxic.json"))
    ap.add_argument("--age-max-pages", type=int, default=AGE_DEFAULT_MAX_PAGES)
    ap.add_argument("--threshold-pct", type=float, default=-5.0)
    ap.add_argument("--min-n", type=int, default=10)
    a = ap.parse_args()
    if a.self_test:
        return self_test()

    if not a.crowd:
        p = latest(str(DATA / "crowd_metric_2*.json"))
        if not p:
            print("СТОП: --crowd не задан и data/crowd_metric_*.json не найден", file=sys.stderr)
            return 2
        a.crowd = str(p)
    if not a.followers:
        p = latest(str(DATA / "c2_block_position_2*.json"))
        if not p:
            print("СТОП: --followers не задан и data/c2_block_position_*.json не найден", file=sys.stderr)
            return 2
        a.followers = str(p)

    report = build_report(a)
    out_path = Path(a.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print_summary(report)
    print(f"\nотчёт: {out_path}")
    return 0


# ============================================================ самопроверка

def self_test() -> int:
    checks: list = []

    def chk(name, ok, got=""):
        checks.append((name, bool(ok), got))

    # ---------- 1-3: обязательные три проверки из задания ----------
    trades = [
        {"mint": "M1", "growth_30s": 1.5, "quote_mint": WSOL},
        {"mint": "M2", "growth_30s": 0.8, "quote_mint": USDC},
        {"mint": "M3", "growth_30s": None, "quote_mint": None},  # неизвестный признак И неизвестный рост
    ]
    g = group_by_feature(trades, quote_kind)
    chk("группировка по признаку не теряет сделки (сумма n_total == len(trades))",
        sum(v["n_total"] for v in g.values()) == len(trades), g)

    empty_only_unknown = group_by_feature([{"mint": "X", "growth_30s": None, "quote_mint": None}], quote_kind)
    chk("деление на ноль при пустой группе не падает (mean=None, не исключение)",
        empty_only_unknown["unknown"]["n_known_growth"] == 0
        and empty_only_unknown["unknown"]["mean_growth_pct"] is None, empty_only_unknown)

    catalog_by_data = {
        "SCAMTAX_но_bps_0": {"ставка_комиссии_bps": 0},
        "HONESTNAME_но_bps_500": {"ставка_комиссии_bps": 500},
    }
    is_tax_1, _ = is_taxable_from_catalog("SCAMTAX_но_bps_0", catalog_by_data)
    is_tax_2, _ = is_taxable_from_catalog("HONESTNAME_но_bps_500", catalog_by_data)
    chk("признак 'налоговый' ставится по bps, а не по обманчивому имени минта (bps=0 -> False)",
        is_tax_1 is False, is_tax_1)
    chk("признак 'налоговый' по данным: bps=500 у 'честного' имени -> True",
        is_tax_2 is True, is_tax_2)
    is_tax_3, why3 = is_taxable_from_catalog("НЕТ_В_КАТАЛОГЕ", catalog_by_data)
    chk("минт вне каталога -> неизвестно (None + причина), а не False по умолчанию",
        is_tax_3 is None and bool(why3), (is_tax_3, why3))

    # ---------- 4-6: cache-признаки ----------
    chk("quote_kind: SOL", quote_kind({"quote_mint": WSOL}) == "sol")
    chk("quote_kind: стейбл", quote_kind({"quote_mint": USDC}) == "stable")
    chk("quote_kind: другое", quote_kind({"quote_mint": "XYZ"}) == "other")
    chk("quote_kind: нет данных -> None, не 'other'", quote_kind({"quote_mint": None}) is None)
    chk("pool_kind: резервный по общеизвестному ID -> человекочитаемое имя",
        pool_kind({"pool_program": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"}) == "Pump AMM")
    chk("pool_kind: неизвестная программа -> по ID, а не выдуманное имя",
        pool_kind({"pool_program": "ZZZZZZZZ11111111"}) == "non_reserve:ZZZZZZZZ")

    # ---------- 7: медианный сплит без утечки сделок ----------
    trades_split = [{"spend_sol_equiv": v, "growth_30s": 1.0} for v in (1, 2, 3, 4, None)]
    gsplit = group_by_feature(trades_split, lambda t: spend_bucket(t, 2.5))
    chk("медианный сплит: все 5 сделок распределены (включая None -> unknown)",
        sum(v["n_total"] for v in gsplit.values()) == 5, gsplit)

    # ---------- 8: flag_toxic_groups корректно фильтрует по порогу и n ----------
    fake_groups = {
        "f1": {"a": {"n_total": 20, "n_known_growth": 20, "mean_growth_pct": -8.0, "median_growth_pct": -8.0},
              "b": {"n_total": 5, "n_known_growth": 5, "mean_growth_pct": -50.0, "median_growth_pct": -50.0},
              "c": {"n_total": 20, "n_known_growth": 20, "mean_growth_pct": 10.0, "median_growth_pct": 10.0}},
    }
    flagged = flag_toxic_groups(fake_groups, threshold_pct=-5.0, min_n=10)
    chk("flag_toxic_groups: берёт только группу с n>=10 И mean<порога (b отсеян по n)",
        len(flagged) == 1 and flagged[0]["group"] == "a", flagged)
    chk("flag_toxic_groups: пустой вход -> пустой список, не исключение",
        flag_toxic_groups({}, threshold_pct=-5.0, min_n=10) == [])

    # ---------- 9-13: mint_chain_info на синтетике (rpc_call -- обычная функция) ----------
    def fake_rpc_ok(method, params):
        if method == "getAccountInfo":
            return {"value": {"owner": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
                              "data": {"parsed": {"info": {
                                  "decimals": 6, "supply": "1000000",
                                  "mintAuthority": None, "freezeAuthority": "FRZ111",
                                  "extensions": [{"extension": "transferFeeConfig", "state": {
                                      "transferFeeConfigAuthority": None,
                                      "newerTransferFee": {"transferFeeBasisPoints": 300, "maximumFee": 999},
                                      "olderTransferFee": {"transferFeeBasisPoints": 100}}}]}}}}}
        raise AssertionError(f"неожиданный метод {method}")

    mi = mint_chain_info(fake_rpc_ok, "MINT1")
    chk("mint_chain_info: mint_authority отозван (None -> True)", mi["mint_authority_revoked"] is True, mi)
    chk("mint_chain_info: freeze_authority НЕ отозван", mi["freeze_authority_revoked"] is False, mi)
    chk("mint_chain_info: налог bps/потолок/прежняя ставка разобраны",
        mi["tax_bps"] == 300 and mi["tax_bps_previous"] == 100 and mi["tax_max_fee_raw"] == 999, mi)
    chk("mint_chain_info: право менять налог отозвано (config authority None)",
        mi["tax_authority_revoked"] is True, mi)

    def fake_rpc_null(method, params):
        return {"value": None}

    mi_null = mint_chain_info(fake_rpc_null, "MINT_NOT_EXIST")
    chk("mint_chain_info: несуществующий аккаунт -> ok=False с причиной, не исключение",
        mi_null["ok"] is False and bool(mi_null.get("why_not")), mi_null)

    def fake_rpc_fail(method, params):
        raise RuntimeError("узел молчит (симуляция)")

    mi_fail = mint_chain_info(fake_rpc_fail, "MINTX")
    chk("mint_chain_info: узел упал -> ok=False, не исключение наружу",
        mi_fail["ok"] is False and "молчит" in mi_fail["why_not"], mi_fail)

    # ---------- 14-16: top10_holder_share ----------
    def fake_largest(method, params):
        return {"value": [{"amount": str(100 - i)} for i in range(20)]}

    top10 = top10_holder_share(fake_largest, "MINT1", supply_raw="1000")
    expect_top10 = sum(100 - i for i in range(10))
    chk("top10_holder_share: доля = сумма топ10/supply", top10["ok"] and
        abs(top10["top10_share"] - expect_top10 / 1000) < 1e-9, top10)
    top10_no_supply = top10_holder_share(fake_largest, "MINT1", supply_raw=None)
    chk("top10_holder_share: нет supply -> причина, не ZeroDivisionError",
        top10_no_supply["ok"] is False and "supply" in top10_no_supply["why_not"], top10_no_supply)

    def fake_largest_empty(method, params):
        return {"value": []}
    top10_empty = top10_holder_share(fake_largest_empty, "MINT1", "1000")
    chk("top10_holder_share: пустой список держателей -> причина, не деление на 0",
        top10_empty["ok"] is False, top10_empty)

    # ---------- 17-19: address_age ----------
    def fake_sig_one_page(method, params):
        return [{"signature": f"S{i}", "slot": 100 - i, "blockTime": 1000 - i} for i in range(5)]

    age_ok = address_age(fake_sig_one_page, "ADDR1", reference_block_time=2000, page_limit=1000, max_pages=3)
    chk("address_age: страница короче лимита -> known, возраст = ref - oldest",
        age_ok["ok"] and age_ok["age_seconds"] == 2000 - 996, age_ok)

    def fake_sig_full_pages(method, params):
        # каждая страница ровно limit=2 -- никогда не кончается раньше потолка
        return [{"signature": f"S{params[1].get('before','0')}_{i}", "slot": i, "blockTime": i} for i in range(2)]

    age_capped = address_age(fake_sig_full_pages, "ADDR2", reference_block_time=2000, page_limit=2, max_pages=3)
    chk("address_age: потолок страниц исчерпан -> известно ЧТО неизвестно, а не фиктивный возраст",
        age_capped["ok"] is False and "не выкачана" in age_capped["why_not"] and age_capped["pages_read"] == 3,
        age_capped)

    def fake_sig_fail(method, params):
        raise RuntimeError("нет сети (симуляция)")
    age_fail = address_age(fake_sig_fail, "ADDR3", max_pages=2)
    chk("address_age: сбой узла -> ok=False с причиной, не исключение", age_fail["ok"] is False, age_fail)

    # ---------- 20: _buyers_in_block / slot_crowd на синтетике ----------
    def mk_tx(signer, mint, pre, post, err=None):
        return {"meta": {"err": err,
                        "preTokenBalances": [{"owner": signer, "mint": mint,
                                              "uiTokenAmount": {"amount": str(pre)}}],
                        "postTokenBalances": [{"owner": signer, "mint": mint,
                                               "uiTokenAmount": {"amount": str(post)}}]},
               "transaction": {"message": {"accountKeys": [{"pubkey": signer, "signer": True},
                                                           {"pubkey": "OTHER", "signer": False}]}}}

    def fake_block(method, params):
        slot = params[0]
        if slot == 500:
            return {"transactions": [mk_tx("BUYER1", "MINT1", 0, 100), mk_tx("SOURCE", "MINT1", 0, 50),
                                     mk_tx("FAILER", "MINT1", 0, 100, err={"x": 1})]}
        if slot == 501:
            return {"transactions": [mk_tx("BUYER2", "MINT1", 0, 10)]}
        raise AssertionError(f"неожиданный слот {slot}")

    sc = slot_crowd(fake_block, 500, "MINT1", "SOURCE")
    chk("slot_crowd: источник исключён из своей же толпы", "SOURCE" not in sc["slot_source"]["buyer_wallets"], sc)
    chk("slot_crowd: неудачная транзакция не считается покупкой",
        "FAILER" not in sc["slot_source"]["buyer_wallets"], sc)
    chk("slot_crowd: покупатель слота S найден, слота S+1 -- отдельно",
        sc["slot_source"]["buyer_wallets"] == ["BUYER1"] and sc["slot_source_plus_1"]["buyer_wallets"] == ["BUYER2"],
        sc)
    chk("slot_crowd: 2 кредита на сделку (getBlock x2)", sc["credits"] == 2, sc)

    # ---------- 21: estimate_b_credit_cost -- арифметика ----------
    est = estimate_b_credit_cost(10, 7, 6, age_max_pages=3)
    expect_total = 10 + 7 + 7 + 7 * 3 + 6 * 3 + 10 * 2
    chk("estimate_b_credit_cost: сумма строк совпадает с ручным расчётом",
        est["total_credits_upper_bound"] == expect_total, (est["total_credits_upper_bound"], expect_total))

    # ---------- 22-24: followers_frequency / named_sniper_report / fee_model ----------
    fw = [
        {"wallet": "W1", "source": "leader", "cu_price_micro": 100, "priority_lamports": 10, "tip_lamports": 5},
        {"wallet": "W1", "source": "leader", "cu_price_micro": 200, "priority_lamports": 10, "tip_lamports": 5},
        {"wallet": "W2", "source": "Brez", "cu_price_micro": 50, "priority_lamports": 1, "tip_lamports": 0},
    ]
    freq = followers_frequency(fw)
    chk("followers_frequency: самый частый кошелёк первый, счётчик верный",
        freq[0]["wallet"] == "W1" and freq[0]["n_appearances"] == 2, freq)
    named = named_sniper_report(fw, ("W1", "NOT_PRESENT_WALLET"))
    chk("named_sniper_report: найденный кошелёк -- со статистикой",
        named["W1"]["found"] and named["W1"]["n_appearances"] == 2, named)
    chk("named_sniper_report: отсутствующий кошелёк -- честно 'не найден', а не 0 по умолчанию без объяснения",
        named["NOT_PRESENT_WALLET"]["found"] is False and bool(named["NOT_PRESENT_WALLET"]["why_not"]), named)
    fm = fee_model_hypothesis(fw)
    chk("fee_model_hypothesis: явно помечает отсутствие объёма копировщика и цену дозаполнения",
        fm["volume_of_follower_own_trade"]["in_cache"] is False
        and fm["volume_of_follower_own_trade"]["credits_for_this_run"] == len(fw), fm)

    # ---------- 25-27: C4 ----------
    fake_catalog = {
        "HIKE1": {"ставка_комиссии_bps": 300, "ставка_комиссии_прежняя_bps": 100, "потолок_комиссии": 5},
        "STABLE1": {"ставка_комиссии_bps": 300, "ставка_комиссии_прежняя_bps": 300},
        "NOPREV": {"ставка_комиссии_bps": 300},
    }
    hikes = tax_hike_mints(fake_catalog)
    chk("tax_hike_mints: только реально изменившиеся, не все таксируемые",
        len(hikes) == 1 and hikes[0]["mint"] == "HIKE1", hikes)
    fake_rows = [{"минт": "HIKE1", "удержано_sol": 0.01, "промежуточные_таксируемые": []},
                {"минт": "OTHER", "удержано_sol": 0.5, "промежуточные_таксируемые": ["HIKE1"]},
                {"минт": "OTHER2", "удержано_sol": 0.2, "промежуточные_таксируемые": []}]
    cost = tax_hike_trade_cost(hikes, fake_rows)
    chk("tax_hike_trade_cost: находит и цель, и промежуточную роль, суммирует верно",
        cost["n_trades_touching_hike_mints"] == 2 and abs(cost["total_withheld_sol"] - 0.51) < 1e-9, cost)

    fake_trades_liq = [
        {"signature": "S1", "why_no_growth_30s": "ликвидность пула снята до точки (ABC)", "why_no_after": None},
        {"signature": "S2", "why_no_growth_30s": None, "why_no_after": "ликвидность пула снята до точки (DEF)"},
        {"signature": "S3", "why_no_growth_30s": "пул: неоднозначная котировка", "why_no_after": None},
    ]
    ld = liquidity_drain_from_cache(fake_trades_liq)
    chk("liquidity_drain_from_cache: считает по обеим причинам (before/after), не путает с другими why_not",
        ld["n_liquidity_drained"] == 2, ld)

    # ---------- 28: load_crowd_trades сохраняет метаданные источника ----------
    import tempfile  # noqa: PLC0415
    tmp_dir = Path(tempfile.mkdtemp())
    sample = {"per_source": [
        {"address": "SRC1", "task": "BATCH-5", "remark": "Test", "n_signatures": 42,
         "trades": [{"signature": "SIG1", "mint": "M1", "growth_30s": 1.1}]},
        {"address": "SRC2", "task": "BATCH-3", "remark": None, "n_signatures": 7, "trades": []},
    ]}
    sample_path = tmp_dir / "crowd_sample.json"
    sample_path.write_text(json.dumps(sample), encoding="utf-8")
    tr, meta = load_crowd_trades(sample_path)
    chk("load_crowd_trades: разворачивает per_source в плоский список с меткой источника",
        len(tr) == 1 and tr[0]["_source_address"] == "SRC1" and tr[0]["_source_task"] == "BATCH-5", tr)
    chk("load_crowd_trades: источник без сделок не теряется в metaданных",
        len(meta) == 2 and meta[1]["n_trades"] == 0, meta)

    # ---------- 29: c1_requirements -- арифметика без сети ----------
    c1 = c1_requirements([{"n_signatures": 1500, "n_trades": 20}, {"n_signatures": 500, "n_trades": 5}],
                         empirical_credits_per_window=222.2)
    chk("c1_requirements: страницы считаются потолком 1000 на источник (1500->2 страницы, 500->1)",
        c1["step1_signatures_pages_credits"] == 3, c1)
    chk("c1_requirements: non-buy верхняя граница = сумма подписей минус известные покупки",
        c1["step2_classify_non_buy_signatures_credits_upper_bound"] == (1500 + 500) - (20 + 5), c1)
    chk("c1_requirements: эмпирический якорь передаётся в отчёт как есть, не пересчитывается",
        c1["step3_empirical_credits_per_sell_window"] == 222.2, c1)

    bad = 0
    for name, ok, got in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {name}" + (f"  -> {str(got)[:300]}" if not ok else ""))
        bad += (not ok)
    print(f"самопроверка night_toxic_signs: {len(checks) - bad}/{len(checks)} пройдено")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
