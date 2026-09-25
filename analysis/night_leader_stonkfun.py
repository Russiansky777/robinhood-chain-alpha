#!/usr/bin/env python3
"""Ночной разбор для владельца: лидер Beqv6 (E), наши сделки через GP (F),
площадка StonkFun (G). ТОЛЬКО ЧТЕНИЕ ЦЕПИ, ни одной транзакции, ни одного
ключа. Торговый код (bloom_*.py, c2_*.py, solana_rpc_client.py, деплой) не
трогается вообще -- этот файл его только читает как справочник форматов.

Почему именно так:

* В контейнере НЕТ HELIUS_API_KEY -- собственного узла у модуля нет и не
  будет. Всё, что нужно спросить у цепи, идёт через `rpc_call` (сигнатура
  как в c2_shadow_build.py: `функция(method, params) -> result`), который
  передаёт ВЫЗЫВАЮЩИЙ. Без него функции честно отвечают why_not и (где это
  осмысленно) числом кредитов, а не тихо гадают по кэшу.
* Самопроверка -- на синтетике и на НАСТОЯЩИХ файлах репозитория
  (data/solana_transfer_fee_audit.json, data/solana_tax_groups.json,
  data/bloom_report.json, data/solana_santa_trade_autopsy.json,
  data/solana_buyer_200/.../metadata_accounts.json) -- ровно как у
  c2_common.py: то, что уже есть в git, не нужно выдумывать заново.
* Толстый кэш толпы (--crowd) -- временный файл сессии C2 вне репозитория,
  поэтому самопроверка на него НЕ полагается: он есть только для боевого
  прогона с --out.
* Ни одно число в отчёте не подставляется "на глаз". Где данных нет --
  поле "missing": true и "why_not" с точной причиной (нет rpc_call, нет
  каталога журналов, нет минта в локальном индексе и т.п.). Оценки кредитов
  -- по документированному тарифу Helius из analysis/solana_rpc_client.py
  (1 кредит за обычный вызов и getTransaction/getBlock, 10 -- за
  getProgramAccounts) и по РЕАЛЬНЫМ числам из уже готового кэша толпы, а не
  из головы.

Цена ошибки: это ночной аналитический отчёт для решения "копировать или
нет" -- накрутка цифр здесь означает reallокацию реальных SOL по неверной
причине. Поэтому там, где вывод не железный (n=4, n=11), это явно написано
рядом с числом.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C2  # noqa: E402 -- identify_pool/quote_spend/owner_mint_delta как есть

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data"

AUDIT_PATH = DATA / "solana_transfer_fee_audit.json"
TAX_GROUPS_PATH = DATA / "solana_tax_groups.json"
BLOOM_REPORT_PATH = DATA / "bloom_report.json"
SANTA_AUTOPSY_PATH = DATA / "solana_santa_trade_autopsy.json"
MINT_EXT_CACHE_PATH = DATA / "solana_buyer_200/prior/current/solana_three_check/metadata_accounts.json"

LEADER_BEQV = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
GP_MINT = "HTmQz7My6MehV7bjhJ6jde8nDND1yvsz68d24LP7YgUQ"
PICKAXE_MINT = "6QxMcEpYULAUs4Qa28ui2GJ55daY2KqFLRJXHEosNPAu"
# Транзакция лидера, названная владельцем: наш source_signature на неё есть
# в data/bloom_report.json (position_rows[14]) -- найдена по тексту, не выдумана.
PICKAXE_EVENT_SIG = "2Nm7Ef1QUsoAZ8AUvC4d34oNbtEuZqbLfL8vy8ihVSv9dRyPZbz4umEsY4TV9r1FDTbw4cdrapKs7pyP7Vj3qtcA"
PICKAXE_EVENT_APPROX_UTC = "2026-09-25T00:31:00Z"  # со слов владельца, минутная точность

NAMED_TAX_MINTS = {
    "SANTA": "3c7mmVSyEH8jfZXgxvpLsETtko1Y16DyRJ5XYB4snhGt",
    "CRACKER": "4rkGWJNSUPBcMicXMRAzohEyeJLFG8gUjwiWaz7Pddr3",
    "PURPLE": "MYQZzuiiRyy38hY8X7KQN8XyE67y2GYWs63cqjDjgLs",
    "RED": "65dw58ugEt3EN9uNuJ2CCyWz7SENe2hnVv9dNHyUjz8x",
    "GP": GP_MINT,
    "PICKAXE": PICKAXE_MINT,
}

# Тариф Helius, как задокументирован в analysis/solana_rpc_client.py
# (docs.helius.dev): обычный вызов/getTransaction/getBlock -- 1 кредит,
# getProgramAccounts -- 10, getMultipleAccounts особо не выделен -> 1 за
# ВЫЗОВ (не за счёт в нём). Цена ошибки завысить оценку -- владелец
# откажется от дешёвой проверки, посчитав её дорогой.
CREDIT_DEFAULT = 1
CREDIT_GET_PROGRAM_ACCOUNTS = 10

# G2: окно "покупка -> продажа", которое просил проверить владелец.
G2_WINDOW_S = 28.8


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S', time.gmtime())}Z] {msg}", flush=True)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def median(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    return statistics.median(xs)


def mean(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    return statistics.fmean(xs)


def r6(x):
    return None if x is None else round(float(x), 6)


# =============================================================== налоговый индекс

def load_tax_index(audit_path: Path = AUDIT_PATH) -> dict:
    """{минт: {taxed, ticker, name, fee_bps, token_program}} из audit'а.

    Источник классификации -- САМ минт в цепочке (расширение
    transferFeeConfig), не имя и не тикер: имя -- маркетинг, ставка
    комиссии -- факт. Кто вызовет classify_group() с тикером вместо этого
    индекса, немедленно поймает "unknown", а не тихую ошибку."""
    try:
        d = read_json(audit_path)
    except (OSError, ValueError) as exc:
        return {"__error__": f"не прочитан {audit_path}: {exc}"}
    out = {}
    for mint, info in (d.get("минты") or {}).items():
        out[mint] = {
            "taxed": bool(info.get("таксируемый")),
            "ticker": info.get("тикер"),
            "name": info.get("название"),
            "fee_bps": info.get("ставка_комиссии_bps"),
            "token_program": info.get("программа_токена"),
        }
    return out


def classify_group(mint: str, tax_index: dict) -> str:
    """"tax" / "normal" / "unknown" -- СТРОГО по данным индекса.

    Нет минта в индексе -> "unknown", а не догадка по тому, что тикер
    похож на мемную налоговую монету. Цена ошибки здесь -- ровно та
    гипотеза владельца, которую нас попросили проверить, а не подтвердить
    заранее."""
    info = tax_index.get(mint) if isinstance(tax_index, dict) else None
    if not info:
        return "unknown"
    return "tax" if info.get("taxed") else "normal"


# =============================================================== задача E: лидер

def load_leader_entries(crowd_path: Path, leader: str = LEADER_BEQV) -> dict:
    """Покупки лидера из кэша толпы (--crowd). Кэш содержит ТОЛЬКО покупки
    (первые входы >= 2 SOL-экв) -- выходов там нет и не будет, это не баг
    загрузчика, это устройство кэша (см. LEADER_EXIT_RECIPE ниже)."""
    try:
        d = read_json(crowd_path)
    except (OSError, ValueError) as exc:
        return {"ok": False, "why_not": f"кэш толпы не прочитан ({crowd_path}): {exc}"}
    per_source = d.get("per_source") or []
    row = next((s for s in per_source if s.get("address") == leader), None)
    if row is None:
        return {"ok": False, "why_not": f"источник {leader} не найден в per_source кэша"}
    trades = row.get("trades") or []
    meta = {
        "task": row.get("task"), "remark": row.get("remark"),
        "n_trades": len(trades),
        "window_from_utc": d.get("window_from_utc"), "window_to_utc": d.get("window_to_utc"),
        "window_days": d.get("window_days"),
        "n_signatures_window": (d.get("leader_row") or {}).get("n_signatures_window"),
        "crowd_cache_generated_utc": d.get("generated_utc"),
    }
    return {"ok": True, "trades": trades, "meta": meta}


def group_leader_entries(trades: list, tax_index: dict) -> dict:
    out = {"tax": [], "normal": [], "unknown": []}
    for t in trades:
        out[classify_group(t.get("mint"), tax_index)].append(t)
    return out


def size_stats(trades: list) -> dict:
    """Размер входа (spend_sol_equiv) -- РЕАЛЬНАЯ величина из кэша, доступна
    на всех сделках без цепи. Пустая группа не делит на ноль -- median()/
    mean() отдают None, а не бросают исключение."""
    sizes = [t.get("spend_sol_equiv") for t in trades if t.get("spend_sol_equiv") is not None]
    return {"n": len(trades), "n_with_size": len(sizes),
            "median_sol": r6(median(sizes)), "mean_sol": r6(mean(sizes))}


def proxy_growth_stats(trades: list) -> dict:
    """Рост цены пула через 30/60с после входа -- РЕАЛЬНЫЙ прокси из кэша,
    но это НЕ итог закрытой сделки: лидер мог продать раньше 30с, позже
    60с, или всё ещё держать. Помечено growth_after_30s_is_not_pnl, чтобы
    его нельзя было молча принять за "итог после налогов"."""
    g30 = [t.get("growth_30s") for t in trades if t.get("growth_30s") is not None]
    g60 = [t.get("growth_60s") for t in trades if t.get("growth_60s") is not None]
    ga30 = [t.get("growth_after_30s") for t in trades if t.get("growth_after_30s") is not None]
    return {"median_growth_30s": r6(median(g30)), "median_growth_60s": r6(median(g60)),
            "median_growth_after_30s": r6(median(ga30)), "n_growth_30s": len(g30),
            "growth_after_30s_is_not_pnl": True}


def hypothesis_size_bigger_on_normal(groups: dict) -> dict:
    """Проверка гипотезы владельца ЧИСЛОМ: на обычных заходит крупнее?
    Часть про "и именно на них плюс" здесь НЕ проверяется -- для неё нужен
    итог закрытой сделки, а не размер входа (см. exit_reconstruction)."""
    tax_sizes = [t.get("spend_sol_equiv") for t in groups.get("tax", []) if t.get("spend_sol_equiv") is not None]
    norm_sizes = [t.get("spend_sol_equiv") for t in groups.get("normal", []) if t.get("spend_sol_equiv") is not None]
    if not tax_sizes or not norm_sizes:
        return {"ok": False,
                "why_not": f"пустая группа (tax={len(tax_sizes)}, normal={len(norm_sizes)}) -- сравнивать не с чем"}
    tax_med, norm_med = median(tax_sizes), median(norm_sizes)
    return {"ok": True, "median_size_tax_sol": r6(tax_med), "median_size_normal_sol": r6(norm_med),
            "normal_bigger": bool(norm_med > tax_med),
            "diff_pct_of_tax": r6((norm_med - tax_med) / tax_med * 100) if tax_med else None,
            "profit_part_confirmed": False,
            "profit_part_why_not": "нужен итог ЗАКРЫТОЙ сделки лидера (вход/выход в SOL-экв после налога) "
                                    "-- выходов в кэше толпы нет, см. exit_reconstruction"}


def pickaxe_history(trades: list, mint: str = PICKAXE_MINT,
                     event_sig: str = PICKAXE_EVENT_SIG, cutoff_utc: str | None = None) -> dict:
    """Первый вход или повторный заход после полной продажи?

    По методике самого кэша (classify_tx, definitions.trade): каждая
    строка в trades -- ПЕРВЫЙ вход, т.е. баланс минта был 0 непосредственно
    перед этой покупкой. Если по PICKAXE таких строк уже несколько ДО
    события владельца -- значит между ними были полные продажи, и запись
    2Nm7Ef1Q... по построению НЕ первый вход, а минимум N+1-й цикл.
    Проверка на РЕАЛЬНОЙ методике кэша, а не догадка."""
    hits = sorted((t for t in trades if t.get("mint") == mint),
                  key=lambda t: t.get("block_time") or 0)
    prior = [t for t in hits if event_sig not in (t.get("signature") or "")]
    if not hits:
        return {"ok": False, "why_not": "PICKAXE не встретился среди покупок лидера в окне кэша",
                "n_prior_confirmed_entries": 0}
    return {
        "ok": True,
        "n_prior_confirmed_entries": len(prior),
        "prior_entries": [{"signature": t.get("signature"), "block_time_utc": t.get("block_time_utc"),
                            "spend_sol_equiv": t.get("spend_sol_equiv")} for t in prior],
        "verdict": ("повторный заход (минимум %d-й цикл): по методике кэша каждая запись -- "
                    "первый вход, т.е. между ними были полные продажи" % (len(prior) + 1))
                   if prior else "в 7-дневном окне кэша до события повторных первых входов не найдено "
                                  "(могли быть раньше, окно кэша короче истории кошелька)",
        "caveat": "кэш -- окно 17.09-24.09; событие владельца (2Nm7Ef1Q..., ~%s) вне этого окна и "
                  "само по себе в trades не попадает (кэш собран ДО него); момент(ы) продажи между "
                  "входами кэш не показывает -- это уже требует rpc_call (см. compute_leader_exits)"
                  % (cutoff_utc or PICKAXE_EVENT_APPROX_UTC),
    }


LEADER_EXIT_RECIPE = {
    "why_not_in_cache": "кэш толпы строился под первый вход (classify_tx, >=2 SOL-экв); "
                         "продажи в его методику не входили вовсе.",
    "steps": [
        "1) getSignaturesForAddress(лидер, {before, limit<=1000}) постранично назад, "
        "пока не дойдём до подписи самой ранней нужной покупки (until=её подпись) -- "
        "это ЕДИНСТВЕННЫЙ способ получить полный список подписей кошелька между покупкой и 'сейчас', "
        "т.к. API отдаёт только 'before', не 'after'.",
        "2) getTransaction(jsonParsed) на КАЖДУЮ подпись из этого списка -- заранее неизвестно, "
        "какая из них продажа нужного минта, decode нужен на все.",
        "3) На каждой tx: c2_common.owner_mint_delta(tx, mint)[лидер] < 0 -- это и есть продажа; "
        "c2_common.quote_spend(tx, лидер) (со знаком наоборот -- см. quote_received ниже) даёт SOL/WSOL/USD "
        "назад, c2_common.identify_pool(tx, лидер, mint, side='sell') -- пул и цену исполнения.",
        "4) held_seconds = блок продажи - блок покупки; итог_sol = получено - потрачено (оба уже по цепи, "
        "т.е. УЖЕ после комиссии на перевод, если минт таксируемый -- отдельно вычитать налог не нужно).",
    ],
    "crowd_at_exit_credit_note": "это ОТДЕЛЬНЫЙ, более дорогой шаг (getSignaturesForAddress + "
        "getTransaction на активность ПУЛА/минта в [выход, выход+30/60с]) -- см. credit_estimate.tier2",
}


def quote_received(tx: dict, wallet: str) -> dict:
    """То же, что c2_common.quote_spend, но для стороны ПОЛУЧЕНИЯ (продажа).

    quote_spend всегда возвращает "сколько потрачено" (инвертирует знак
    лампортов), поэтому на продаже (дельта лампортов положительна) он отдал
    бы отрицательное число. Инвертируем обратно, а не переписываем
    quote_spend -- у него есть самопроверка в c2_common и звать его как
    есть безопаснее, чем чинить дважды."""
    s = C2.quote_spend(tx, wallet)
    return {"sol": -s["sol"], "wsol": -s["wsol"], "usd": -s["usd"]}


def estimate_exit_credit_cost(meta: dict, trades: list, days: int = 14) -> dict:
    """Оценка кредитов по РЕАЛЬНЫМ числам уже готового кэша толпы, а не
    с потолка. Формула -- 1 кредит на обычный вызов/getTransaction
    (см. CREDIT_DEFAULT, тариф в analysis/solana_rpc_client.py)."""
    n_sig_window_7d = meta.get("n_signatures_window")
    window_days = meta.get("window_days") or 7
    scale = days / window_days if window_days else 1.0
    tier1 = None
    if isinstance(n_sig_window_7d, (int, float)) and n_sig_window_7d > 0:
        n_sig = n_sig_window_7d * scale
        pages = max(1, -(-int(n_sig) // 1000))  # ceil
        tier1 = {
            "n_signatures_estimate": round(n_sig),
            "getSignaturesForAddress_credits": pages * CREDIT_DEFAULT,
            "getTransaction_credits": round(n_sig) * CREDIT_DEFAULT,
            "total_credits": pages * CREDIT_DEFAULT + round(n_sig) * CREDIT_DEFAULT,
            "number_source": "leader_row.n_signatures_window из --crowd кэша, "
                              "масштабировано на %d дн." % days,
        }
    else:
        tier1 = {"why_not": "в кэше нет n_signatures_window -- оценка невозможна без него"}
    crowd_30s = [t.get("window_tx_30s") for t in trades if t.get("window_tx_30s") is not None]
    tier2_note = None
    if crowd_30s:
        tier2_note = {
            "sum_window_tx_30s_at_entry_actual": sum(crowd_30s),
            "meaning": "это РЕАЛЬНО потраченные вызовы на толпу ПРИ ВХОДЕ (уже оплачены, видны в c2_usage "
                       "прогона, построившего --crowd) -- ориентир порядка величины для толпы ПРИ ВЫХОДЕ, "
                       "НЕ прогноз: на выходе хайп обычно спадает, но по факту это не измерено.",
            "caveat": "может быть меньше (спад хайпа) или сравнимо -- без прогона неизвестно",
        }
    return {"tier1_find_exit_and_price": tier1,
            "tier2_crowd_at_exit_estimate": tier2_note or {"why_not": "в сделках нет window_tx_30s"},
            "c2_daily_budget": 200_000,
            "conclusion": "Tier 1 дёшев (единицы процентов суточного бюджета C2) и достаточен для самого "
                          "вопроса 'где именно плюс'; Tier 2 дороже на порядок и нужен только если "
                          "понадобится толпа на выходе, а не сам итог сделки."}


def compute_leader_exits(rpc_call, trades: list, *, leader: str = LEADER_BEQV,
                          page_limit: int = 1000, max_pages: int = 60) -> dict:
    """Реальная реализация LEADER_EXIT_RECIPE. Без rpc_call -- честный
    отказ, а не пустой список (пустой список неотличим от "выходов не
    было", а это не так)."""
    if rpc_call is None:
        return {"ok": False, "why_not": "нет rpc_call -- см. LEADER_EXIT_RECIPE и estimate_exit_credit_cost",
                "trades": []}
    if not trades:
        return {"ok": False, "why_not": "пустой список сделок лидера -- нечего закрывать", "trades": []}
    by_sig = {t["signature"]: t for t in trades if t.get("signature")}
    earliest_sig = min(trades, key=lambda t: t.get("block_time") or 0).get("signature")
    sigs: list = []
    before = None
    for _ in range(max_pages):
        params = [leader, {"limit": page_limit, "commitment": "finalized"}]
        if before:
            params[1]["before"] = before
        page = rpc_call("getSignaturesForAddress", params) or []
        if not page:
            break
        sigs.extend(page)
        before = page[-1].get("signature")
        if any(p.get("signature") == earliest_sig for p in page):
            break
    sigs.sort(key=lambda s: s.get("slot") or 0)  # старые -> новые, для прохода вперёд от покупки
    index_of = {s["signature"]: i for i, s in enumerate(sigs) if s.get("signature")}
    results = []
    for t in trades:
        sig, mint = t.get("signature"), t.get("mint")
        start = index_of.get(sig)
        if start is None:
            results.append({"signature": sig, "mint": mint, "ok": False,
                             "why_not": "подпись входа не попала в собранное окно подписей кошелька"})
            continue
        found = None
        for j in range(start + 1, len(sigs)):
            s = sigs[j]
            if s.get("err") is not None:
                continue
            tx = rpc_call("getTransaction", [s["signature"], {"encoding": "jsonParsed",
                          "maxSupportedTransactionVersion": 1, "commitment": "finalized"}])
            if not tx:
                continue
            delta = C2.owner_mint_delta(tx, mint).get(leader)
            if delta is not None and delta < 0:
                recv = quote_received(tx, leader)
                pool = C2.identify_pool(tx, leader, mint, side="sell")
                found = {"exit_signature": s["signature"], "exit_slot": s.get("slot"),
                         "sol_received": r6(recv["sol"] + recv["wsol"]),
                         "pool_ok": pool.get("ok"), "pool_why_not": pool.get("why_not"),
                         "price": str(pool.get("price")) if pool.get("price") is not None else None}
                break
        if found:
            in_sol = t.get("spend_sol_equiv")
            out_sol = found["sol_received"]
            found.update({"signature": sig, "mint": mint, "ok": True,
                          "sol_in": in_sol,
                          "net_sol": r6(out_sol - in_sol) if (in_sol is not None and out_sol is not None) else None,
                          "held_slots": found["exit_slot"] - t.get("slot") if found.get("exit_slot") and t.get("slot") else None})
            results.append(found)
        else:
            results.append({"signature": sig, "mint": mint, "ok": False,
                             "why_not": "выход не найден в собранном окне подписей (ещё держит или окно короче)"})
    return {"ok": True, "n_signatures_fetched": len(sigs), "trades": results}


def run_task_e(crowd_path: Path, tax_index: dict, rpc_call=None) -> dict:
    loaded = load_leader_entries(crowd_path)
    if not loaded.get("ok"):
        return {"ok": False, "why_not": loaded.get("why_not")}
    trades, meta = loaded["trades"], loaded["meta"]
    groups = group_leader_entries(trades, tax_index)
    group_report = {}
    for g in ("tax", "normal", "unknown"):
        group_report[g] = {"entry_size": size_stats(groups[g]), "price_proxy_30_60s": proxy_growth_stats(groups[g])}
    exits = compute_leader_exits(rpc_call, trades, leader=LEADER_BEQV)
    return {
        "ok": True,
        "leader": LEADER_BEQV,
        "leader_meta": meta,
        "groups_n": {g: len(v) for g, v in groups.items()},
        "groups": group_report,
        "hypothesis_size_bigger_on_normal": hypothesis_size_bigger_on_normal(groups),
        "pickaxe": pickaxe_history(trades),
        "exit_reconstruction": exits if exits.get("ok") else {
            **exits, "recipe": LEADER_EXIT_RECIPE,
            "credit_estimate_7d": estimate_exit_credit_cost(meta, trades, days=7),
            "credit_estimate_14d": estimate_exit_credit_cost(meta, trades, days=14),
        },
    }


# =============================================================== задача F: наши сделки через GP

def load_gp_route_rows(audit_path: Path = AUDIT_PATH, gp_mint: str = GP_MINT) -> list:
    """Наши закрытые сделки, где маршрут ФИЗИЧЕСКИ шёл через GP (GP в
    переводов_по_минтам ИЛИ в промежуточные_*), -- не "купили GP", а
    "GP стоял внутри маршрута к другому токену"."""
    try:
        d = read_json(audit_path)
    except (OSError, ValueError) as exc:
        return [{"__error__": f"не прочитан {audit_path}: {exc}"}]
    out = []
    for r in d.get("строки") or []:
        transfers = r.get("переводов_по_минтам") or {}
        via_gp = (gp_mint in transfers or gp_mint in (r.get("промежуточные_покупка") or [])
                  or gp_mint in (r.get("промежуточные_продажа") or [])
                  or gp_mint in (r.get("промежуточные_таксируемые") or []))
        if not via_gp or r.get("минт") == gp_mint:
            continue
        sol_in = r.get("sol_in")
        tax_sol = r.get("удержано_sol")
        out.append({
            "task": r.get("задача"), "source": r.get("источник"), "source_remark": r.get("источник_метка"),
            "wallet": r.get("кошелёк"), "target_mint": r.get("минт"),
            "buy_signature": r.get("подпись_покупки"), "sell_signature": r.get("подпись_продажи"),
            "n_transfers_target": transfers.get(r.get("минт")), "n_transfers_gp": transfers.get(gp_mint),
            "sol_in": sol_in, "sol_out": r.get("sol_out"), "net_sol": r.get("net_sol"),
            "tax_withheld_sol": tax_sol,
            "tax_pct_of_stake": r6(tax_sol / sol_in * 100) if (sol_in and tax_sol is not None) else None,
            "held_seconds": r.get("held_seconds"), "gross_pct_signal": r.get("gross_pct"),
            "tax_incomplete_no_rate": r.get("налог_неполон_нет_курса"),
        })
    return out


def translate_intermediate_token_stats(info: dict) -> dict:
    """Ключи "промежуточные_токены"[минт] из audit'а -- в ASCII. Прямой
    **spread сюда нельзя: у audit'а свои (кириллические) ключи, а наш
    выходной JSON обязан быть ASCII-only по ключам."""
    return {"trades": info.get("сделок"), "sum_onchain_sol": info.get("сумма_по_цепи_sol"),
            "withheld_sol": info.get("удержано_sol"), "transfers": info.get("переводов"),
            "no_rate": info.get("без_курса"), "ticker": info.get("тикер"),
            "name": info.get("название"), "fee_bps": info.get("ставка_комиссии_bps"),
            "token_program": info.get("программа_токена")}


def gp_intermediate_summary(audit_path: Path = AUDIT_PATH, gp_mint: str = GP_MINT) -> dict:
    try:
        d = read_json(audit_path)
    except (OSError, ValueError) as exc:
        return {"ok": False, "why_not": str(exc)}
    info = (d.get("промежуточные_токены") or {}).get(gp_mint)
    if not info:
        return {"ok": False, "why_not": "GP не встретился в промежуточные_токены audit'а"}
    return {"ok": True, "source_file": str(AUDIT_PATH.relative_to(REPO_ROOT)),
            "collected_utc": d.get("собрано_utc"), **translate_intermediate_token_stats(info)}


def group_summary_gp_routes(rows: list) -> dict:
    real_rows = [r for r in rows if "__error__" not in r]
    if not real_rows:
        return {"n": 0, "why_not": "нет ни одной строки с маршрутом через GP"}
    tax_pcts = [r["tax_pct_of_stake"] for r in real_rows if r["tax_pct_of_stake"] is not None]
    net = [r["net_sol"] for r in real_rows if r["net_sol"] is not None]
    n_pos = sum(1 for x in net if x > 0)
    return {
        "n": len(real_rows), "n_with_net_sol": len(net),
        "median_tax_pct_of_stake": r6(median(tax_pcts)), "mean_tax_pct_of_stake": r6(mean(tax_pcts)),
        "n_positive_net_sol": n_pos,
        "share_positive": r6(n_pos / len(net)) if net else None,
        "median_net_sol": r6(median(net)), "sum_net_sol": r6(sum(net)) if net else None,
        "times_price_move_covered_tax": f"{n_pos} из {len(net)} -- это ровно случаи, "
            "где движение цены с запасом перекрыло и налог GP, и налог целевого токена, и любую "
            "просадку пула (net_sol уже посчитан по цепи ПОСЛЕ всех вычетов)",
    }


def find_post_pickaxe_candidate(bloom_report_path: Path = BLOOM_REPORT_PATH,
                                 pickaxe_event_sig: str = PICKAXE_EVENT_SIG) -> dict:
    """Сильно плюсовая сделка "после PICKAXE 25.09": следующая по слоту
    позиция в data/bloom_report.json (position_rows) после сделки, чей
    source_signature -- транзакция лидера, названная владельцем.

    Это НЕ доказательство маршрута через GP -- целевой минт этой сделки не
    встретился ни в data/solana_transfer_fee_audit.json (собран ДО неё,
    23.09 21:23Z), ни в кэшах сырых транзакций репозитория: подтвердить
    "через GP" можно только getTransaction по её own_signature."""
    try:
        d = read_json(bloom_report_path)
    except (OSError, ValueError) as exc:
        return {"ok": False, "why_not": f"не прочитан {bloom_report_path}: {exc}"}
    rows = d.get("position_rows") or []
    pickaxe_row = next((r for r in rows if r.get("source_signature") == pickaxe_event_sig), None)
    if pickaxe_row is None:
        return {"ok": False, "why_not": f"строка с source_signature={pickaxe_event_sig[:12]}... "
                                         f"не найдена в {bloom_report_path}"}
    pickaxe_slot = pickaxe_row.get("our_slot")
    later = sorted((r for r in rows if (r.get("our_slot") or -1) > (pickaxe_slot or -1)),
                    key=lambda r: r["our_slot"])
    if not later:
        return {"ok": True, "pickaxe_row": pickaxe_row, "candidate": None,
                "why_not_candidate": "в снимке нет сделок после PICKAXE по слоту"}
    cand = later[0]
    sol_in, sol_back_net = cand.get("sol_in"), cand.get("sol_back_net")
    pct = r6((sol_back_net - sol_in) / sol_in * 100) if (sol_in and sol_back_net is not None) else None
    return {
        "ok": True,
        "source_file": str(bloom_report_path.relative_to(REPO_ROOT)) + " (built_utc=%s)" % d.get("built_utc"),
        "pickaxe_row": {"mint": pickaxe_row.get("mint"), "sol_in": pickaxe_row.get("sol_in"),
                        "sol_back_net": pickaxe_row.get("sol_back_net"),
                        "our_slot": pickaxe_row.get("our_slot"),
                        "note": "наша сделка на PICKAXE самого была УБЫТОЧНОЙ (-15.0%), "
                                "это отдельно от 'сильно плюсовой' сделки ПОСЛЕ неё"},
        "candidate": {"mint": cand.get("mint"), "sol_in": sol_in, "sol_back_net": sol_back_net,
                      "net_pct": pct, "our_slot": cand.get("our_slot"), "our_signature": cand.get("our_signature")},
        "gp_route_confirmed": False,
        "why_not_gp_route_confirmed": "минт кандидата отсутствует в data/solana_transfer_fee_audit.json "
            "(собран 23.09 21:23Z, кандидат -- из окна ПОСЛЕ этого) и не найден ни в одном локальном "
            "кэше сырых транзакций репозитория; нужен getTransaction(candidate.our_signature) и разбор "
            "переводов по инструкциям -- 1 вызов, 1 кредит",
    }


def enrich_gp_rows_with_leader_crowd(rows: list, leader_trades: list) -> list:
    """Толпа/рост на ВХОДЕ у самого лидера для строк с источником Beqv6 --
    сопоставление ПО МИНТУ (подписи источника в audit'е не сохранены), так
    что при нескольких входах лидера в один минт за 7д привязка не точна по
    времени -- отмечено явно, не подставлено как единственный факт."""
    by_mint: dict = {}
    for t in leader_trades:
        by_mint.setdefault(t.get("mint"), []).append(t)
    out = []
    for r in rows:
        r = dict(r)
        if r.get("source") == LEADER_BEQV:
            hits = by_mint.get(r["target_mint"]) or []
            r["leader_crowd_at_entry"] = ([{"crowd_30s": h.get("crowd_30s"), "growth_30s": h.get("growth_30s"),
                                            "spend_sol_equiv": h.get("spend_sol_equiv"),
                                            "block_time_utc": h.get("block_time_utc")} for h in hits]
                                          if hits else None)
            r["leader_crowd_why_not"] = None if hits else "минт не встретился среди покупок лидера в 7д кэше"
            r["leader_crowd_ambiguous"] = len(hits) > 1
        else:
            r["leader_crowd_at_entry"] = None
            r["leader_crowd_why_not"] = "источник строки -- не Beqv6, кэш толпы по нему не строился"
        out.append(r)
    return out


def bloom_fee_note() -> dict:
    return {
        "tracked_separately": False,
        "why": "в data/solana_transfer_fee_audit.json явно оговорено: 'gross_pct' (сигнал) НЕ равен "
               "результату по цепи, т.к. в цепь уже включены комиссия сети, чаевые и комиссия площадки "
               "-- т.е. любая комиссия Bloom/DBot уже НЕВЫЧТЕНА из sol_in/sol_out ОТДЕЛЬНОЙ строкой, "
               "а сидит внутри разницы между ними.",
        "how_to_get_separately": "getTransaction по buy/sell подписям -> meta.fee (лампорты, сетевая "
                                  "комиссия+приоритет) виден по цепи напрямую; комиссия самого DBot/Bloom "
                                  "как отдельная величина по цепи не видна вообще (она внутри их роутинга) "
                                  "-- нужны их же логи, не RPC.",
        "cost": "1 getTransaction на сделку (уже нужен для net_sol) -- отдельного вызова не требует",
    }


def load_state_dir_journals(state_dir: Path | None) -> dict:
    """positions.jsonl / decisions.jsonl -- журналы исполнителя (см.
    analysis/bloom_exec_state.py: append_jsonl_fsync в те же два файла,
    строки мёржатся по client_order_id ровно как в c2_common.executor_trades).
    Отсутствующий каталог -- honest why_not, не исключение."""
    if state_dir is None:
        return {"ok": False, "why_not": "не передан --state-dir (по умолчанию /home/bot/bloom_executor_live_data "
                                         "-- в этом контейнере такого каталога нет)"}
    state_dir = Path(state_dir)
    if not state_dir.exists():
        return {"ok": False, "why_not": f"каталог не существует: {state_dir}"}
    pos_path, dec_path = state_dir / "positions.jsonl", state_dir / "decisions.jsonl"
    out: dict = {"state_dir": str(state_dir)}
    for name, path in (("positions", pos_path), ("decisions", dec_path)):
        if not path.exists():
            out[name] = {"ok": False, "why_not": f"файла нет: {path}"}
            continue
        rows, bad = [], 0
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                bad += 1
        out[name] = {"ok": True, "n_lines": len(rows), "n_bad_lines": bad}
        if name == "positions":
            merged: dict = {}
            order: list = []
            for r in rows:
                c = r.get("client_order_id")
                if not c:
                    continue
                if c not in merged:
                    order.append(c)
                merged.setdefault(c, {}).update({k: v for k, v in r.items() if v is not None})
            out[name]["n_positions_merged"] = len(order)
            out[name]["rows"] = [merged[c] for c in order]
    return {"ok": True, **out}


def translate_group_stats(info: dict | None) -> dict | None:
    """Ключи одной строки "группы_*"/"асимметрия_маршрутов" из
    data/solana_tax_groups.json -- в ASCII (та же причина, что у
    translate_intermediate_token_stats: свои кириллические ключи нельзя
    прокидывать в наш ASCII-only выход как есть)."""
    if not info:
        return None
    return {"trades": info.get("сделок"), "share_profitable": info.get("доля_в_плюс"),
            "profitable_after_tax": info.get("в_плюсе_после_налога"),
            "profitable_without_tax": info.get("в_плюсе_без_налога"),
            "flipped_by_tax": info.get("перевёрнуто_налогом"),
            "median_signal_pct": info.get("медиана_сигнала_pct"),
            "mean_signal_pct": info.get("среднее_сигнала_pct"),
            "invested_sol": info.get("вложено_sol"), "onchain_result_sol": info.get("результат_по_цепи_sol"),
            "taxes_sol": info.get("налогов_sol"), "result_without_tax_sol": info.get("результат_без_налога_sol"),
            "trades_incomplete_tax": info.get("сделок_с_неполным_налогом")}


def run_task_f(leader_trades: list) -> dict:
    rows = load_gp_route_rows()
    if rows and "__error__" in rows[0]:
        return {"ok": False, "why_not": rows[0]["__error__"]}
    rows = enrich_gp_rows_with_leader_crowd(rows, leader_trades)
    tax_groups = _safe_read(TAX_GROUPS_PATH)
    asymmetry = (tax_groups.get("асимметрия_маршрутов") or {}).get("обе ноги через промежуточный")
    return {
        "ok": True,
        "gp_mint": GP_MINT,
        "gp_intermediate_summary_from_audit": gp_intermediate_summary(),
        "route_trades": rows,
        "group_summary": group_summary_gp_routes(rows),
        "route_asymmetry_all_intermediate_from_repo": translate_group_stats(asymmetry),
        "post_pickaxe_candidate": find_post_pickaxe_candidate(),
        "bloom_fee_note": bloom_fee_note(),
    }


def _safe_read(path: Path) -> dict:
    try:
        return read_json(path)
    except (OSError, ValueError):
        return {}


# =============================================================== задача G: StonkFun

def parse_mint_extensions_value(value: dict) -> dict | None:
    """Один элемент result.value из getAccountInfo/getMultipleAccounts
    (encoding=jsonParsed) -- вытащить transferFeeConfig, если минт
    Token-2022 с таким расширением. None -- не тот тип счёта, а не 0."""
    if not isinstance(value, dict):
        return None
    data = value.get("data")
    parsed = (data or {}).get("parsed") if isinstance(data, dict) else None
    info = (parsed or {}).get("info") if isinstance(parsed, dict) else None
    if not info or "extensions" not in info:
        return None
    exts = {e.get("extension"): e.get("state") for e in info.get("extensions") or []}
    tfc = exts.get("transferFeeConfig")
    tm = exts.get("tokenMetadata") or {}
    return {"mint": tm.get("mint"), "symbol": tm.get("symbol"),
            "has_transfer_fee_config": tfc is not None,
            "config_authority": (tfc or {}).get("transferFeeConfigAuthority"),
            "withdraw_authority": (tfc or {}).get("withdrawWithheldAuthority"),
            "withheld_amount_raw": (tfc or {}).get("withheldAmount"),
            "decimals": info.get("decimals")}


def load_cached_mint_extensions(path: Path = MINT_EXT_CACHE_PATH) -> dict:
    """{минт: разбор} из уже сохранённого в репозитории getMultipleAccounts
    (без единого нового вызова к узлу)."""
    try:
        d = read_json(path)
    except (OSError, ValueError) as exc:
        return {"__error__": f"не прочитан {path}: {exc}"}
    values = ((d.get("result") or {}).get("value")) or []
    out = {}
    for v in values:
        parsed = parse_mint_extensions_value(v)
        if parsed and parsed.get("mint"):
            out[parsed["mint"]] = parsed
    return out


def get_mint_fee_authorities(rpc_call, mints: list, cached: dict | None = None) -> dict:
    """Слить локальный кэш с чтением по rpc_call для минтов, которых там
    нет. getMultipleAccounts -- 1 кредит за ВЫЗОВ (не за счёт в нём,
    см. тариф в шапке файла), поэтому батчим по 100."""
    cached = dict(cached or {})
    missing = [m for m in mints if m not in cached]
    if not missing:
        return {"by_mint": cached, "fetched_fresh": 0, "credits_spent": 0}
    if rpc_call is None:
        out = dict(cached)
        for m in missing:
            out[m] = {"why_not": "нет ни в локальном кэше, ни rpc_call для свежего чтения"}
        return {"by_mint": out, "fetched_fresh": 0, "credits_spent": 0,
                "why_not_missing": f"{len(missing)} минтов не в кэше: нужен rpc_call "
                                    f"(getMultipleAccounts, ~{-(-len(missing)//100)} кредит(ов))"}
    out = dict(cached)
    credits = 0
    for i in range(0, len(missing), 100):
        chunk = missing[i:i + 100]
        res = rpc_call("getMultipleAccounts", [chunk, {"encoding": "jsonParsed"}]) or {}
        credits += CREDIT_DEFAULT
        values = res.get("value") if isinstance(res, dict) else res
        for mint, v in zip(chunk, values or []):
            parsed = parse_mint_extensions_value(v)
            out[mint] = parsed or {"why_not": "счёт не Token-2022 минт с расширениями или не найден"}
    return {"by_mint": out, "fetched_fresh": len(missing), "credits_spent": credits}


def g1_named_mints_report(cached_ext: dict) -> dict:
    """"checked" -- реально ли ПОЛУЧЕН разбор счёта минта, а не просто есть
    ли ключ в словаре: get_mint_fee_authorities() без rpc_call кладёт под
    отсутствующий минт {"why_not": ...} -- это НЕ проверка, и "checked" не
    должен подтверждать проверку, которой не было."""
    out = {}
    for name, mint in NAMED_TAX_MINTS.items():
        info = cached_ext.get(mint)
        checked = bool(info) and "has_transfer_fee_config" in info
        if not checked:
            out[name] = {"mint": mint, "checked": False,
                         "why_not": (info or {}).get("why_not") or "нет в локальном кэше метаданных, нужен rpc_call"}
        else:
            out[name] = {"mint": mint, "checked": True, **{k: v for k, v in info.items() if k != "mint"}}
    return out


def authority_share(named_report: dict) -> dict:
    """Доля минтов с ПОДТВЕРЖДЁННЫМ (проверенным локально или по rpc_call)
    общим коллектором -- знаменатель ТОЛЬКО по проверенным, иначе непроверенные
    молча считались бы "не тот кошелёк"."""
    checked = [v for v in named_report.values() if v.get("checked") and v.get("has_transfer_fee_config")]
    if not checked:
        return {"n_checked": 0, "share_same_authority": None,
                "why_not": "ни один минт из списка не проверен локально -- см. checked=false выше"}
    authorities = [v.get("withdraw_authority") for v in checked]
    common = statistics.mode(authorities) if authorities else None
    n_same = sum(1 for a in authorities if a == common)
    return {"n_checked": len(checked), "common_authority_guess": common,
            "n_same_authority": n_same, "share_same_authority": r6(n_same / len(checked))}


def translate_santa_event(t: dict) -> dict:
    """Строка из "все_сделки_в_окне" (data/solana_santa_trade_autopsy.json)
    -- в ASCII-ключи, той же причины ради, что и другие translate_*."""
    return {"wallet": t.get("кошелёк"), "direction": t.get("направление"), "tokens": t.get("токенов"),
            "sol": t.get("sol"), "price": t.get("цена"), "slot": t.get("слот"),
            "time_utc": t.get("время_utc"), "signature": t.get("подпись"),
            "pool_key": t.get("ключ_пула"), "program": t.get("программа"),
            "pool_price_after": t.get("цена_пула_после"), "pool_price_before": t.get("цена_пула_до")}


def g2_santa_autopsy_example(path: Path = SANTA_AUTOPSY_PATH) -> dict:
    """ЕДИНСТВЕННЫЙ уже готовый в репозитории разбор окна вокруг нашей
    сделки на таксируемом минте (SANTA, кошелёк pointfarmcap) -- анекдот
    на n=1, не статистика по G2 в целом."""
    try:
        d = read_json(path)
    except (OSError, ValueError) as exc:
        return {"ok": False, "why_not": f"не прочитан {path}: {exc}"}
    trades_window = d.get("все_сделки_в_окне") or []
    collector_hits = [t for t in trades_window
                       for auth in ("5KXDF6QnqhBj72hDtJNkkpFaQVUfbFXNybMsp3DiK6tD",)
                       if t.get("кошелёк") == auth]
    return {
        "ok": True, "source_file": str(path.relative_to(REPO_ROOT)), "collected_utc": d.get("собрано_utc"),
        "mint": d.get("минт"), "our_result_pct": d.get("итог_сделки_pct"),
        "n_collector_events_in_window": len(collector_hits),
        "collector_events": [translate_santa_event(t) for t in collector_hits],
        "note": "событие коллектора здесь -- через ~10с ПОСЛЕ нашей ПОКУПКИ (не через 28.8с и не после "
                "продажи); направление 'покупка' у коллектора, пул/сумма в SOL не декодированы "
                "('программа': не определена в источнике) -- это n=1, не подтверждает и не опровергает G2 "
                "в общем виде, только показывает, что коллектор активен рядом с нашими окнами",
        "n1_caveat": True,
    }


def g2_recipe() -> dict:
    return {
        "steps": [
            "getTransaction(подпись_покупки) -> blockTime покупки",
            f"getSignaturesForAddress(владелец_пула_или_минт, {{'limit':200}}) в окне "
            f"[blockTime, blockTime+{G2_WINDOW_S}с]",
            "getTransaction на кандидатов в окне -> c2_common.identify_pool/pool_event, "
            "фильтр: продавец == известный коллектор (withdraw_authority минта)",
            "цена до/после его продажи по тем же хранилищам -> сдвиг цены против нас в %",
        ],
        "window_s": G2_WINDOW_S,
        "cost_per_trade": "порядка 1 (buy tx) + ~1-3 (страницы подписей) + до ~50 (кандидаты в окне) "
                           "кредитов -- на 11 GP-маршрутов это <= ~600 кредитов, дёшево",
    }


def g3_leader_rewards() -> dict:
    return {
        "ok": False,
        "why_not": "локально НИ В ОДНОМ кэше репозитория нет перевода коллектор -> Beqv6 "
                    "(проверено текстовым поиском адреса лидера рядом с известным коллектором "
                    "5KXDF6QnqhBj72hDtJNkkpFaQVUfbFXNybMsp3DiK6tD -- совпадений нет); "
                    "раздача держателям происходит в ПАРНОМ активе (GLDx для GP и т.п.), а не в GP/SOL, "
                    "поэтому её вообще нельзя увидеть в кэше толпы (там только сделки в SOL-экв по DEX)",
        "recipe": ["getSignaturesForAddress(коллектор, {limit:1000}) постранично за 7-14 дней",
                   "getTransaction на каждую -> owner_mint_delta по ПАРНОМУ активу (не GP): "
                   "рост баланса Beqv6 без встречной траты с его стороны = раздача",
                   "сумма таких раздач / итог Beqv6 по таксируемым монетам (из exit_reconstruction) = доля"],
        "cost": "той же формы, что compute_leader_exits, но по кошельку коллектора, а не лидера "
                "-- отдельная оценка нужна после того, как известно, сколько у коллектора подписей за окно",
    }


EXTERNAL_SOURCES = {
    "sources_and_method": "WebFetch на все три адреса заблокирован прокси окружения (EGRESS_BLOCKED: "
        "gp.gold, www.datawallet.com, bitquery.io) -- проверено вызовом, не предположено. Ниже -- "
        "результат WebSearch (индексированные фрагменты страниц), а НЕ дословная выгрузка HTML: "
        "это пересказ/цитаты поисковой выдачи по заявленному URL, не подтверждённые прямым чтением "
        "страницы этим прогоном. Числа ниже -- то, что вернул поиск, не наша агрегация по цепи.",
    "gp.gold": {
        "url": "https://gp.gold/", "method": "websearch_not_webfetch",
        "quotes": [
            "Each GP transfer incurs a 3% tax, with accumulated fees periodically converted by "
            "StonkFun and distributed proportionally to eligible holders in the form of GLDx.",
            "GP, inspired by the in-game currency of the RuneScape game, is paired with tokenized "
            "gold GLDx.",
        ],
        "confirmed_onchain": "3% (300 bps) и Token-2022/transferFeeConfig на GP -- подтверждено "
                                "НЕЗАВИСИМО по цепи (data/solana_buyer_200/.../metadata_accounts.json)",
    },
    "datawallet.com": {
        "url": "https://www.datawallet.com/crypto/stonk-fun-explained", "method": "websearch_not_webfetch",
        "quotes": [
            "A reward coin carries a 1% or 3% Token-2022 transfer tax on every transfer, on any venue. "
            "StonkFun's collector harvests it, sells it for the quote asset and pays every wallet "
            "holding at least $20, pro rata.",
            "The tax is harvested, sold for the quote asset and paid to every wallet holding at "
            "least $20, pro rata, with holders receiving 97.5% and operations receiving 2.5%.",
        ],
    },
    "bitquery.io": {
        "url": "https://bitquery.io/investigations/is-stonkfun-dumping-on-holders", "method": "websearch_not_webfetch",
        "quotes": [
            "StonkFun charges a 1% or 3% tax every time a reward coin moves, with one wallet "
            "collecting it in the coin itself, selling it, and paying holders in whatever the "
            "coin trades against. The selling is the tax at work, and it still hurts.",
            "The wallet that viral posts called a fee drain sold $56.3M of StonkFun coins in 30 "
            "days, but paid $56.2M back out to holders over the same 30 days, in each coin's pair asset.",
            "On some of the busiest coins the tax has taken more than half the supply and sold it "
            "into the pool. 160 coins lost more than half their supply this way.",
            "at least $1.41M of the reward money went to wallets tied to StonkFun instead.",
        ],
    },
}


def g4_verdict(task_f: dict, tax_groups: dict) -> dict:
    itog_vse = (tax_groups.get("группы_все") or {})
    a = itog_vse.get("а: токен без комиссии, маршрут прямой") or {}
    b = itog_vse.get("б: токен таксируемый, маршрут прямой") or {}
    g_gp = task_f.get("group_summary") or {}
    return {
        "numbers": {
            "share_profitable_no_tax_direct_route": a.get("доля_в_плюс"),
            "share_profitable_taxed_direct_route": b.get("доля_в_плюс"),
            "flipped_by_tax_group_b": b.get("перевёрнуто_налогом"),
            "share_profitable_gp_route": g_gp.get("share_positive"),
            "n_gp_route": g_gp.get("n"),
            "median_tax_pct_of_stake_gp_route": g_gp.get("median_tax_pct_of_stake"),
        },
        "verdict": "копировать С УСЛОВИЕМ, не вслепую: "
            "(1) прямой маршрут БЕЗ комиссии -- обычные условия, копировать; "
            "(2) прямой маршрут С комиссией (группа б) -- доля в плюс уже ниже (%.1f%% против %.1f%%), "
            "и часть сделок (%s из %s) перевёрнута налогом из плюса в минус -- копировать только если "
            "независимый сигнал роста явно выше типичного; "
            "(3) маршрут ЧЕРЕЗ GP (двойной налог: GP + целевой минт) -- на имеющихся %s сделках доля в "
            "плюс всего %s%%, медианный налог %.1f%% от ставки -- это САМАЯ дорогая по налогу и слабая "
            "по результату группа из всех измеренных; копировать только когда ожидаемый рост цены явно "
            "выше ~%.0f%% (медианный налог группы), иначе структурно проигрышно ещё до учёта проскальзывания."
            % (
                (b.get("доля_в_плюс") or 0) * 100, (a.get("доля_в_плюс") or 0) * 100,
                b.get("перевёрнуто_налогом"), b.get("сделок"),
                g_gp.get("n"), round((g_gp.get("share_positive") or 0) * 100, 1),
                g_gp.get("median_tax_pct_of_stake") or 0, g_gp.get("median_tax_pct_of_stake") or 0,
            ),
        "external_sources_confirm": "Bitquery: на самых 'горячих' 160 монетах налог продал в пул "
            ">50% предложения; коллектор в среднем возвращает держателям почти всё собранное (56.2 из "
            "56.3M за 30д), но $1.41M ушло в кошельки площадки, а не держателям -- т.е. постоянное "
            "давление продажи структурно реально, а не выдумано нами по 11 сделкам.",
        "small_sample_caveat": "маршрут-через-GP посчитан на %s наших закрытых сделках -- реальное "
            "число, но малое; расширять выводы на все будущие GP-сделки нужно осторожно." % g_gp.get("n"),
    }


def run_task_g(task_f: dict, rpc_call=None) -> dict:
    cached_ext = load_cached_mint_extensions()
    if "__error__" in cached_ext:
        cached_ext = {}
    mints_needed = list(NAMED_TAX_MINTS.values())
    merged = get_mint_fee_authorities(rpc_call, mints_needed, cached=cached_ext)
    named = g1_named_mints_report(merged["by_mint"])
    tax_groups = _safe_read(TAX_GROUPS_PATH)
    return {
        "g1_authorities": {
            "named_mints": named,
            "share": authority_share(named),
            "fetch_meta": {k: v for k, v in merged.items() if k != "by_mint"},
            "all_147_tax_mints_check": {
                "done": False,
                "why_not": "не запрошено rpc_call в этом прогоне",
                "cost_if_rpc_call_given": "getMultipleAccounts батчами по 100 -> ceil(147/100)=2 "
                                           "вызова = 2 кредита (дёшево, окупает точную долю по ВСЕМ "
                                           "147 таксируемым минтам, а не по 6 именованным)",
            },
        },
        "g2_price_impact": {"real_example_n1": g2_santa_autopsy_example(), "recipe": g2_recipe()},
        "g3_leader_rewards": g3_leader_rewards(),
        "external_sources": EXTERNAL_SOURCES,
        "g4_verdict": g4_verdict(task_f, tax_groups),
    }


# =============================================================== запуск

def _assert_ascii_keys(obj, path="$"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str) or not k.isascii():
                raise AssertionError(f"не-ASCII ключ {k!r} по пути {path}")
            _assert_ascii_keys(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _assert_ascii_keys(v, f"{path}[{i}]")


def build_report(crowd_path: Path, state_dir: Path | None, rpc_call=None) -> dict:
    tax_index = load_tax_index()
    task_e = run_task_e(crowd_path, tax_index, rpc_call=rpc_call)
    leader_trades = task_e.get("leader_meta") and (load_leader_entries(crowd_path).get("trades") or [])
    task_f = run_task_f(leader_trades or [])
    task_f["state_dir_journals"] = load_state_dir_journals(state_dir)
    task_g = run_task_g(task_f, rpc_call=rpc_call)
    report = {
        "schema_version": 1,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "inputs": {"crowd_cache": str(crowd_path), "state_dir": str(state_dir) if state_dir else None,
                   "audit": str(AUDIT_PATH.relative_to(REPO_ROOT)),
                   "tax_groups": str(TAX_GROUPS_PATH.relative_to(REPO_ROOT)),
                   "bloom_report": str(BLOOM_REPORT_PATH.relative_to(REPO_ROOT)),
                   "santa_autopsy": str(SANTA_AUTOPSY_PATH.relative_to(REPO_ROOT)),
                   "mint_extensions_cache": str(MINT_EXT_CACHE_PATH.relative_to(REPO_ROOT))},
        "constraints": {"read_only": True, "no_trades": True, "helius_key_in_container": False,
                        "chain_reads_via": "rpc_call, передаваемый вызывающим (см. c2_shadow_build.py)"},
        "task_e_leader": task_e,
        "task_f_our_gp_trades": task_f,
        "task_g_stonkfun": task_g,
    }
    return report


# =============================================================== самопроверка

def self_test() -> int:
    checks: list[tuple[str, bool, object]] = []

    def chk(name, ok, got=""):
        checks.append((name, bool(ok), got))

    # 1-2. median/mean на пустом списке -- None, без деления на ноль.
    chk("median([]) is None (пустая группа не делит на 0)", median([]) is None)
    chk("mean([]) is None", mean([]) is None)
    chk("median на одном значении", median([5]) == 5)

    # 4-5. classify_group -- строго по данным, не по имени.
    tax_idx = {"AAA111": {"taxed": True}, "BBB222": {"taxed": False}}
    chk("classify_group: taxed=True -> tax", classify_group("AAA111", tax_idx) == "tax")
    chk("classify_group: taxed=False -> normal", classify_group("BBB222", tax_idx) == "normal")
    chk("classify_group: имя похоже на налоговое ('SANTACOIN'), но данных нет -> unknown (НЕ по имени)",
        classify_group("SANTACOIN_НЕ_В_ИНДЕКСЕ", tax_idx) == "unknown")
    chk("classify_group: минт БЕЗ похожего на налог имени, но taxed=True в данных -> tax (тоже по данным)",
        classify_group("AAA111", tax_idx) == "tax")

    # group_leader_entries / size_stats / hypothesis -- на синтетике.
    synth_trades = [
        {"mint": "AAA111", "spend_sol_equiv": 10.0, "growth_30s": 1.5, "signature": "s1", "block_time": 100},
        {"mint": "AAA111", "spend_sol_equiv": 20.0, "growth_30s": 1.2, "signature": "s2", "block_time": 200},
        {"mint": "BBB222", "spend_sol_equiv": 100.0, "growth_30s": 1.1, "signature": "s3", "block_time": 300},
        {"mint": "CCC333", "spend_sol_equiv": 5.0, "signature": "s4", "block_time": 400},
    ]
    groups = group_leader_entries(synth_trades, tax_idx)
    chk("group_leader_entries: 2 tax, 1 normal, 1 unknown",
        len(groups["tax"]) == 2 and len(groups["normal"]) == 1 and len(groups["unknown"]) == 1, groups)
    ss = size_stats(groups["tax"])
    chk("size_stats: median размера tax-группы = 15.0", ss["median_sol"] == 15.0, ss)
    ss_empty = size_stats([])
    chk("size_stats на пустой группе: n=0, median=None, без исключения",
        ss_empty == {"n": 0, "n_with_size": 0, "median_sol": None, "mean_sol": None}, ss_empty)
    hb = hypothesis_size_bigger_on_normal({"tax": groups["tax"], "normal": []})
    chk("hypothesis_size_bigger_on_normal: пустая normal-группа -> ok=False, why_not, без деления на 0",
        hb["ok"] is False and "why_not" in hb, hb)
    hb2 = hypothesis_size_bigger_on_normal(groups)
    chk("hypothesis_size_bigger_on_normal: normal(100) > tax(15) -> normal_bigger=True",
        hb2["ok"] and hb2["normal_bigger"] is True, hb2)

    # pickaxe_history: 2 предыдущих первых входа -> "минимум 3-й цикл".
    pk_trades = [
        {"mint": PICKAXE_MINT, "signature": "pk1", "block_time": 100, "block_time_utc": "t1", "spend_sol_equiv": 4},
        {"mint": PICKAXE_MINT, "signature": "pk2", "block_time": 200, "block_time_utc": "t2", "spend_sol_equiv": 8},
        {"mint": "OTHER", "signature": "o1", "block_time": 150},
    ]
    ph = pickaxe_history(pk_trades)
    chk("pickaxe_history: 2 предыдущих входа найдены", ph["ok"] and ph["n_prior_confirmed_entries"] == 2, ph)
    chk("pickaxe_history: пустой список -> ok=False, честно", pickaxe_history([])["ok"] is False)

    # quote_received -- инверсия знака относительно quote_spend на синтетике c2_common.
    def mk(keys, pre_t, post_t, pre_l=None, post_l=None, sg=("W",)):
        n = len(keys)
        return {"slot": 1, "blockTime": 100,
                "transaction": {"signatures": ["S" * 88],
                                "message": {"accountKeys": [{"pubkey": k, "signer": k in sg} for k in keys],
                                            "instructions": []}},
                "meta": {"err": None, "preBalances": pre_l or [0] * n, "postBalances": post_l or [0] * n,
                         "preTokenBalances": pre_t, "postTokenBalances": post_t, "innerInstructions": []}}

    sell_tx = mk(["W", "POOL"], [], [], pre_l=[1_000_000_000, 0], post_l=[1_500_000_000, 0])
    got = quote_received(sell_tx, "W")
    chk("quote_received: продажа (+0.5 SOL лампортов) -> sol=+0.5, не -0.5",
        abs(float(got["sol"]) - 0.5) < 1e-9, got)

    # compute_leader_exits: без rpc_call -- честный отказ, не пустой список молча.
    no_rpc = compute_leader_exits(None, synth_trades)
    chk("compute_leader_exits(rpc_call=None): ok=False с why_not (не путать с 'выходов не было')",
        no_rpc["ok"] is False and "why_not" in no_rpc, no_rpc)
    chk("compute_leader_exits: пустой список сделок -> ok=False", compute_leader_exits(lambda *a: None, [])["ok"] is False)

    # compute_leader_exits: синтетический rpc_call, реальный проход по алгоритму.
    def tb(i, owner, mint, amt, dec=6):
        return {"accountIndex": i, "owner": owner, "mint": mint,
                "uiTokenAmount": {"amount": str(amt), "decimals": dec}}

    buy_sig, sell_sig = "BUYSIG" + "1" * 82, "SELLSIG" + "2" * 81
    leader_w = "LEADERWALLETXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
    entry = [{"signature": buy_sig, "mint": "MNT111", "slot": 10, "spend_sol_equiv": 2.0, "block_time": 1}]
    sig_pages = {None: [{"signature": sell_sig, "slot": 11, "err": None},
                        {"signature": buy_sig, "slot": 10, "err": None}]}
    sell_tx2 = mk(["POOLV", "POOLOWN", leader_w],
                  [tb(0, "POOLOWN", "MNT111", 0), tb(2, leader_w, "MNT111", 500_000)],
                  [tb(0, "POOLOWN", "MNT111", 500_000), tb(2, leader_w, "MNT111", 0)],
                  pre_l=[0, 0, 3_000_000_000], post_l=[0, 0, 3_400_000_000], sg=(leader_w,))

    def fake_rpc(method, params):
        if method == "getSignaturesForAddress":
            return sig_pages.get(params[1].get("before"), [])
        if method == "getTransaction":
            return sell_tx2 if params[0] == sell_sig else None
        raise AssertionError(f"неожиданный метод {method}")

    res = compute_leader_exits(fake_rpc, entry, leader=leader_w)
    chk("compute_leader_exits: синтетика находит выход и sol_received≈0.4",
        res["ok"] and res["trades"][0]["ok"] and abs(res["trades"][0]["sol_received"] - 0.4) < 1e-6, res)

    # load_state_dir_journals: несуществующий каталог -- честный why_not, не исключение.
    st_missing = load_state_dir_journals(Path("/nonexistent/path/for/self_test_only_xyz"))
    chk("load_state_dir_journals: несуществующий каталог -> why_not, без падения", st_missing["ok"] is False, st_missing)
    st_none = load_state_dir_journals(None)
    chk("load_state_dir_journals: --state-dir не передан -> why_not с упоминанием пути по умолчанию",
        st_none["ok"] is False and "bloom_executor_live_data" in st_none["why_not"], st_none)

    # load_state_dir_journals: РЕАЛЬНЫЙ каталог с журналами -- парсится верно (merge по client_order_id).
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    (tmp / "positions.jsonl").write_text(
        json.dumps({"client_order_id": "c1", "state": "intent", "mint": "M1", "sol_in": 0.1}) + "\n" +
        json.dumps({"client_order_id": "c1", "state": "closed", "sol_back": 0.12}) + "\n" +
        "не json совсем\n", encoding="utf-8")
    (tmp / "decisions.jsonl").write_text(json.dumps({"code": "OK"}) + "\n", encoding="utf-8")
    st_ok = load_state_dir_journals(tmp)
    chk("load_state_dir_journals: 1 позиция смёржена из 2 строк, 1 битая строка учтена",
        st_ok["ok"] and st_ok["positions"]["n_positions_merged"] == 1
        and st_ok["positions"]["rows"][0]["mint"] == "M1" and st_ok["positions"]["rows"][0]["sol_back"] == 0.12
        and st_ok["positions"]["n_bad_lines"] == 1, st_ok)

    # parse_mint_extensions_value -- синтетический jsonParsed getMultipleAccounts.
    synth_value = {"data": {"parsed": {"info": {"decimals": 6, "extensions": [
        {"extension": "tokenMetadata", "state": {"mint": "GPMINT111", "symbol": "GP"}},
        {"extension": "transferFeeConfig", "state": {"transferFeeConfigAuthority": "AUTH1",
                                                      "withdrawWithheldAuthority": "AUTH1",
                                                      "withheldAmount": "123"}},
    ]}}}}
    parsed = parse_mint_extensions_value(synth_value)
    chk("parse_mint_extensions_value: authority и withheld вытащены верно",
        parsed and parsed["mint"] == "GPMINT111" and parsed["withdraw_authority"] == "AUTH1"
        and parsed["withheld_amount_raw"] == "123", parsed)
    chk("parse_mint_extensions_value: не тот тип счёта -> None, не исключение",
        parse_mint_extensions_value({"data": {"parsed": {"info": {}}}}) is None)

    # authority_share: пустой checked-список не делит на ноль.
    empty_named = {"X": {"checked": False, "why_not": "нет данных"}}
    ash = authority_share(empty_named)
    chk("authority_share: ни один минт не проверен -> без деления на 0, why_not",
        ash["n_checked"] == 0 and ash["share_same_authority"] is None, ash)
    named_ok = {"A": {"checked": True, "has_transfer_fee_config": True, "withdraw_authority": "Z"},
                "B": {"checked": True, "has_transfer_fee_config": True, "withdraw_authority": "Z"},
                "C": {"checked": True, "has_transfer_fee_config": True, "withdraw_authority": "Q"}}
    ash2 = authority_share(named_ok)
    chk("authority_share: 2 из 3 -- общий кошелёк, доля 0.6667", ash2["n_checked"] == 3
        and abs(ash2["share_same_authority"] - 2 / 3) < 1e-5, ash2)

    # get_mint_fee_authorities: без rpc_call -- честный why_not на недостающих, кэш не трогается.
    gm = get_mint_fee_authorities(None, ["KNOWN", "MISSING"], cached={"KNOWN": {"ok": True}})
    chk("get_mint_fee_authorities: без rpc_call недостающий минт помечен, а не выдуман",
        gm["by_mint"]["MISSING"]["why_not"] and gm["by_mint"]["KNOWN"] == {"ok": True}, gm)

    def fake_rpc_multi(method, params):
        assert method == "getMultipleAccounts"
        return {"value": [synth_value if p == "MISSING2" else None for p in params[0]]}

    gm2 = get_mint_fee_authorities(fake_rpc_multi, ["MISSING2"], cached={})
    chk("get_mint_fee_authorities: с rpc_call недостающий минт разобран и посчитан кредит",
        gm2["credits_spent"] == 1 and gm2["by_mint"]["MISSING2"]["mint"] == "GPMINT111", gm2)

    # Регресс: g1_named_mints_report НЕ должен подтверждать проверку, которой не было
    # (нашлось на реальном прогоне -- "checked": true при отсутствии has_transfer_fee_config).
    global NAMED_TAX_MINTS
    orig_named = NAMED_TAX_MINTS
    NAMED_TAX_MINTS = {"KNOWN": "KNOWNGP", "MISSING_NO_RPC": "NOPE"}
    try:
        rep = g1_named_mints_report({"KNOWNGP": {"has_transfer_fee_config": True, "withdraw_authority": "Z"},
                                     "NOPE": {"why_not": "нет ни в локальном кэше..."}})
    finally:
        NAMED_TAX_MINTS = orig_named
    chk("g1_named_mints_report: минт с why_not-заглушкой -> checked=False, не True",
        rep["MISSING_NO_RPC"]["checked"] is False and rep["KNOWN"]["checked"] is True, rep)

    # НАСТОЯЩИЕ файлы репозитория -- там, где они есть.
    if AUDIT_PATH.exists():
        idx = load_tax_index()
        chk("load_tax_index на настоящем audit'е: GP числится таксируемым 300bps",
            idx.get(GP_MINT, {}).get("taxed") is True and idx.get(GP_MINT, {}).get("fee_bps") == 300,
            idx.get(GP_MINT))
        rows = load_gp_route_rows()
        chk("load_gp_route_rows на настоящем audit'е: находит >=10 строк через GP (было 11 на момент сбора)",
            isinstance(rows, list) and len(rows) >= 10 and all("__error__" not in r for r in rows), len(rows))
        gsum = group_summary_gp_routes(rows)
        chk("group_summary_gp_routes: медианный налог по ставке заметно выше типичных ~3-4% "
            "(двойной налог маршрута через GP)", (gsum.get("median_tax_pct_of_stake") or 0) > 8, gsum)
    if BLOOM_REPORT_PATH.exists():
        cand = find_post_pickaxe_candidate()
        chk("find_post_pickaxe_candidate: строка PICKAXE по source_signature владельца найдена",
            cand.get("ok") and cand.get("pickaxe_row") is not None, cand)
    if MINT_EXT_CACHE_PATH.exists():
        ext = load_cached_mint_extensions()
        chk("load_cached_mint_extensions: GP и CRACKER в кэше делят один withdraw_authority",
            ext.get(GP_MINT, {}).get("withdraw_authority") is not None
            and ext.get(GP_MINT, {}).get("withdraw_authority") == ext.get(NAMED_TAX_MINTS["CRACKER"], {}).get("withdraw_authority"),
            {"gp": ext.get(GP_MINT), "cracker": ext.get(NAMED_TAX_MINTS["CRACKER"])})

    # Итоговый отчёт целиком на синтетическом кэше толпы -- пайплайн не падает, ключи ASCII.
    tmp2 = Path(tempfile.mkdtemp())
    crowd_fixture = {
        "generated_utc": "2026-01-01T00:00:00Z", "window_from_utc": "a", "window_to_utc": "b", "window_days": 7,
        "leader_row": {"n_signatures_window": 700},
        "per_source": [{"address": LEADER_BEQV, "task": "BATCH-5", "remark": "pointfarmcap",
                        "trades": synth_trades}],
    }
    crowd_path = tmp2 / "crowd.json"
    crowd_path.write_text(json.dumps(crowd_fixture, ensure_ascii=False), encoding="utf-8")
    report = build_report(crowd_path, None, rpc_call=None)
    chk("build_report: собирается целиком без исключений на синтетическом кэше",
        report.get("task_e_leader", {}).get("ok") is True)
    try:
        _assert_ascii_keys(report)
        ascii_ok = True
    except AssertionError as exc:
        ascii_ok = False
        print("   не-ASCII ключ:", exc)
    chk("итоговый отчёт: ВСЕ ключи JSON только ASCII", ascii_ok)
    try:
        json.dumps(report, ensure_ascii=False)
        chk("итоговый отчёт сериализуется в JSON без исключений", True)
    except (TypeError, ValueError) as exc:
        chk("итоговый отчёт сериализуется в JSON без исключений", False, str(exc))

    bad = 0
    for name, ok, got in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {name}" + (f"  -> {str(got)[:300]}" if not ok else ""))
        bad += not ok
    print(f"самопроверка night_leader_stonkfun: {len(checks) - bad}/{len(checks)} пройдено")
    return 0 if bad == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--crowd", type=Path, help="путь к crowd_metric_*.json (толпа за источником, C2)")
    ap.add_argument("--out", type=Path, help="куда записать итоговый JSON")
    ap.add_argument("--state-dir", type=Path, default=None,
                    help="каталог журналов исполнителя (positions.jsonl/decisions.jsonl); "
                         "по умолчанию не передаётся -- в контейнере такого каталога нет")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    if not args.crowd or not args.out:
        ap.error("нужны --crowd и --out (или --self-test)")

    log(f"читаю кэш толпы {args.crowd}")
    report = build_report(args.crowd, args.state_dir, rpc_call=None)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"записано {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
