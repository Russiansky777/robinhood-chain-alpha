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

# --chain: суточная квота C2 общая на ВСЕ службы c2_* (см. C2Rpc.check_budget
# в analysis/c2_common.py), а владелец на ЭТО исследование выделил не более
# 70% от неё -- остальное нужно другим ночным задачам. В отличие от
# STOP_AT_WARN=False в solana_rpc_client.py (там 70% -- только сигнал,
# "не гасить самим"), здесь владелец явно велел ОСТАНОВИТЬСЯ, а не доложить
# и продолжить -- поэтому это жёсткий стоп, а не предупреждение.
OWNER_RESEARCH_BUDGET = 200_000
OWNER_STOP_FRACTION = 0.70
CHAIN_SERVICE_NAME = "c2_night_leader_stonkfun"


class CreditLimitExceeded(Exception):
    """НЕ RuntimeError и не C2.BudgetExceeded: свой, более ранний предел
    (--credit-limit прогона ИЛИ 70% общей суточной квоты владельца).
    Отдельный класс -- чтобы вызывающий код (compute_leader_exits,
    get_mint_fee_authorities) мог поймать ИМЕННО остановку по кредитам и
    честно завершиться частичным результатом, не путая её со сбоем сети
    (RuntimeError) и не давая ей утонуть в общем except."""


class BudgetedRpc:
    """rpc_call с ДВУМЯ пределами поверх C2Rpc: локальным (--credit-limit
    на этот прогон) и владельческим (не более OWNER_STOP_FRACTION общей
    суточной квоты C2 -- проверяется по РЕАЛЬНОМУ расходу всех служб c2_*
    на диске (C2.c2_spent_today), а не только этим прогоном, иначе можно
    было бы съесть чужой запас, ничего не нарушив локально).

    Cчёт кредитов -- по тарифу analysis/solana_rpc_client.py (RC.credits_for):
    1 за обычный вызов/getTransaction, 10 -- за getProgramAccounts. Считаем
    ДО отправки запроса (пессимистично: сорвавшийся вызов всё равно уходил
    в сеть и мог быть учтён провайдером)."""

    def __init__(self, c2rpc, local_limit: int) -> None:
        self.rpc = c2rpc
        self.local_limit = local_limit
        self.used = 0
        self.calls = 0
        self.stopped_reason: str | None = None

    def __call__(self, method: str, params: list):
        if self.stopped_reason:
            raise CreditLimitExceeded(self.stopped_reason)
        n = C2.RC.credits_for(method)
        if self.used + n > self.local_limit:
            self.stopped_reason = (f"локальный предел --credit-limit={self.local_limit} исчерпан "
                                    f"(потрачено этим прогоном {self.used}, нужно ещё {n})")
            raise CreditLimitExceeded(self.stopped_reason)
        owner_cap = int(OWNER_RESEARCH_BUDGET * OWNER_STOP_FRACTION)
        spent_shared = C2.c2_spent_today(self.rpc.meter.base)
        if spent_shared + n > owner_cap:
            self.stopped_reason = (f"70% суточной квоты C2 ({owner_cap} из {OWNER_RESEARCH_BUDGET}) было бы "
                                    f"превышено (сейчас всеми c2_-службами потрачено {spent_shared}) -- "
                                    "владелец велел останавливаться здесь, не только докладывать")
            raise CreditLimitExceeded(self.stopped_reason)
        result = self.rpc.call(method, params)
        self.used += n
        self.calls += 1
        return result


def resolve_chain_rpc(chain: bool, credit_limit: int, *, usage_dir: Path | None = None):
    """Собрать rpc_call для --chain или честно отказать. Без ключа -- НЕ
    исключение, а обычный отрицательный результат (why_not), как и просил
    владелец."""
    if not chain:
        return None, {"ok": False, "why_not": "--chain не передан -- офлайн-режим на кэше/локальных файлах"}
    key, key_name = C2.RC.helius_key()
    if not key:
        return None, {"ok": False, "why_not": "нет HELIUS_API_KEY/HELIUS_API в окружении -- "
                                               "цепь недоступна (ключ не найден ни под одним из двух имён)"}
    rpc = C2.C2Rpc(CHAIN_SERVICE_NAME, key=key, usage_dir=usage_dir or C2.RC.USAGE_DIR)
    budgeted = BudgetedRpc(rpc, local_limit=credit_limit)
    return budgeted, {"ok": True, "service": CHAIN_SERVICE_NAME, "key_env": key_name,
                       "local_credit_limit": credit_limit,
                       "owner_stop_fraction": OWNER_STOP_FRACTION,
                       "owner_stop_credits": int(OWNER_RESEARCH_BUDGET * OWNER_STOP_FRACTION),
                       "c2_daily_budget": OWNER_RESEARCH_BUDGET}


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
        "1) getTransaction(покупка) -- из её pre/postTokenBalances и accountKeys берётся АДРЕС "
        "токен-счёта лидера по этому минту (из покупки, а не через getTokenAccountsByOwner: "
        "после полной продажи счёт закрывают, и по владельцу его уже не найти).",
        "2) getSignaturesForAddress(токен-счёт, {until: подпись покупки, limit<=1000}) -- "
        "только то, что позже покупки и только по этому минту. Так отпадает просмотр всех "
        "подписей кошелька: прогон 25.09 по кошельку съел 20 000 кредитов и дал негодную "
        "стыковку (удержание 3-8 суток в семидневном окне).",
        "2а) getTransaction(jsonParsed) на кандидатов ЭТОГО счёта от старых к новым -- "
        "до первой продажи; обычно это единицы вызовов на сделку.",
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
    # ОЦЕНКА ПО НОВОМУ АЛГОРИТМУ (стыковка по токен-счёту минта): на сделку
    # одна getTransaction покупки, одна getSignaturesForAddress счёта и
    # getTransaction на кандидатов этого счёта. Прежняя оценка считала обход
    # ВСЕХ подписей кошелька -- она и объясняет, почему прогон 25.09 упёрся в
    # 20 000 кредитов, восстановив шесть сделок.
    n_trades = len(trades)
    КАНДИДАТОВ_НА_СДЕЛКУ = 4   # покупка, продажа и пара переводов -- с запасом
    tier1 = {
        "n_trades": n_trades,
        "per_trade_calls": 1 + 1 + КАНДИДАТОВ_НА_СДЕЛКУ,
        "total_credits": n_trades * (2 + КАНДИДАТОВ_НА_СДЕЛКУ) * CREDIT_DEFAULT,
        "number_source": "число сделок из --crowd кэша; на сделку: getTransaction покупки + "
                          "getSignaturesForAddress токен-счёта + до %d getTransaction кандидатов"
                          % КАНДИДАТОВ_НА_СДЕЛКУ,
        "days_note": "окно %d дн. цену не меняет: считается по сделкам кэша, а не по "
                      "подписям кошелька" % days,
    }
    n_sig_window_7d = meta.get("n_signatures_window")
    window_days = meta.get("window_days") or 7
    scale = days / window_days if window_days else 1.0
    if isinstance(n_sig_window_7d, (int, float)) and n_sig_window_7d > 0:
        # Для сравнения: во что обошёлся бы прежний обход всего кошелька.
        n_sig = round(n_sig_window_7d * scale)
        tier1["old_wallet_scan_credits"] = (
            max(1, -(-n_sig // 1000)) * CREDIT_DEFAULT + n_sig * CREDIT_DEFAULT)
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


def хранилище_владельца(tx: dict, owner: str, mint: str) -> str | None:
    """Адрес токен-счёта владельца по этому минту -- из счетов САМОЙ покупки.

    Почему из покупки, а не через getTokenAccountsByOwner: после полной
    продажи счёт закрывают, и по владельцу его уже не найти -- а в покупке
    он точно есть. Индекс счёта берётся из pre/postTokenBalances, адрес --
    из accountKeys по этому индексу.
    """
    ключи = C2.account_keys(tx or {})
    мета = (tx or {}).get("meta") or {}
    for поле in ("postTokenBalances", "preTokenBalances"):
        for б in (мета.get(поле) or []):
            if б.get("owner") == owner and б.get("mint") == mint:
                i = б.get("accountIndex")
                if isinstance(i, int) and 0 <= i < len(ключи):
                    return ключи[i]
    return None


def compute_leader_exits(rpc_call, trades: list, *, leader: str = LEADER_BEQV,
                          sig_limit: int = 1000,
                          max_tx: int | None = None) -> dict:
    """Реальная реализация LEADER_EXIT_RECIPE. Без rpc_call -- честный
    отказ, а не пустой список (пустой список неотличим от "выходов не
    было", а это не так).

    СТЫКОВКА ИДЁТ ПО ТОКЕН-СЧЁТУ МИНТА, а не по всем подписям кошелька.
    Прежняя версия брала подписи лидера целиком и просматривала каждую
    getTransaction подряд, пока не встретит продажу нужного минта: на
    прогоне 25.09 это съело 20 000 кредитов, восстановило 6 "закрытых
    сделок" и выдало удержание 685 тыс. -- 1.69 млн слотов, то есть 3-8
    суток в семидневном окне. Такие числа означали не долгое держание, а
    неверную стыковку: до настоящей продажи проход просто не доходил.

    Теперь на сделку: одна getTransaction (покупка -- из неё берётся адрес
    токен-счёта), одна getSignaturesForAddress ЭТОГО счёта с until=подпись
    покупки (то есть только то, что позже покупки и только по этому минту),
    и getTransaction на несколько кандидатов до первой продажи. Порядок --
    от старых к новым, поэтому найденная продажа и есть ПЕРВАЯ.

    Два независимых предела на дорогую часть: свой `max_tx`
    (--exit-max-tx, "досюда и хватит") и CreditLimitExceeded от переданного
    rpc_call (BudgetedRpc). Оба -- НЕ исключение наружу, а честный
    частичный результат: обработанные сделки остаются с найденным выходом,
    необработанные -- с точной причиной.
    """
    if rpc_call is None:
        return {"ok": False, "why_not": "нет rpc_call -- см. LEADER_EXIT_RECIPE и estimate_exit_credit_cost",
                "trades": []}
    if not trades:
        return {"ok": False, "why_not": "пустой список сделок лидера -- нечего закрывать", "trades": []}
    ПАРАМ_TX = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1,
                 "commitment": "finalized"}
    results: list = []
    tx_calls_used = 0
    sig_calls_used = 0
    sigs_seen = 0
    stopped_reason: str | None = None

    def взять_tx(подпись: str):
        """getTransaction с обоими пределами. Возвращает (tx, причина_стопа)."""
        nonlocal tx_calls_used
        if max_tx is not None and tx_calls_used >= max_tx:
            return None, f"достигнут предел --exit-max-tx={max_tx} (getTransaction-вызовов)"
        try:
            tx = rpc_call("getTransaction", [подпись, dict(ПАРАМ_TX)])
        except CreditLimitExceeded as exc:
            return None, str(exc)
        tx_calls_used += 1
        return tx, None

    for t in trades:
        sig, mint = t.get("signature"), t.get("mint")
        if stopped_reason:
            results.append({"signature": sig, "mint": mint, "ok": False,
                             "why_not": f"не дошли -- {stopped_reason}"})
            continue
        if not sig or not mint:
            results.append({"signature": sig, "mint": mint, "ok": False,
                             "why_not": "в записи кэша нет подписи или минта"})
            continue
        покупка, стоп = взять_tx(sig)
        if стоп:
            stopped_reason = стоп
            results.append({"signature": sig, "mint": mint, "ok": False,
                             "why_not": f"остановлено на этой сделке -- {стоп}"})
            continue
        if not покупка:
            results.append({"signature": sig, "mint": mint, "ok": False,
                             "why_not": "узел не отдал транзакцию покупки"})
            continue
        хран = хранилище_владельца(покупка, leader, mint)
        if not хран:
            results.append({"signature": sig, "mint": mint, "ok": False,
                             "why_not": "в покупке нет токен-счёта лидера по этому минту"})
            continue
        try:
            страница = rpc_call("getSignaturesForAddress",
                                 [хран, {"limit": sig_limit, "until": sig,
                                          "commitment": "finalized"}]) or []
            sig_calls_used += 1
        except CreditLimitExceeded as exc:
            stopped_reason = str(exc)
            results.append({"signature": sig, "mint": mint, "ok": False,
                             "why_not": f"остановлено на этой сделке -- {stopped_reason}"})
            continue
        sigs_seen += len(страница)
        # От старых к новым: первая найденная продажа и есть первая продажа.
        кандидаты = sorted(страница, key=lambda x: x.get("slot") or 0)
        found = None
        for s in кандидаты:
            if s.get("err") is not None:
                continue
            подпись_к = s.get("signature")
            if not подпись_к or подпись_к == sig:
                continue
            tx, стоп = взять_tx(подпись_к)
            if стоп:
                stopped_reason = стоп
                break
            if not tx:
                continue
            delta = C2.owner_mint_delta(tx, mint).get(leader)
            if delta is not None and delta < 0:
                recv = quote_received(tx, leader)
                pool = C2.identify_pool(tx, leader, mint, side="sell")
                found = {"exit_signature": подпись_к, "exit_slot": s.get("slot"),
                          "sol_received": r6(recv["sol"] + recv["wsol"]),
                          "pool_ok": pool.get("ok"), "pool_why_not": pool.get("why_not"),
                          "price": str(pool.get("price")) if pool.get("price") is not None else None,
                          "vault": хран, "candidates_seen": len(кандидаты)}
                break
        if found:
            in_sol = t.get("spend_sol_equiv")
            out_sol = found["sol_received"]
            found.update({"signature": sig, "mint": mint, "ok": True,
                          "sol_in": in_sol,
                          "net_sol": r6(out_sol - in_sol) if (in_sol is not None and out_sol is not None) else None,
                          "held_slots": found["exit_slot"] - t.get("slot") if found.get("exit_slot") and t.get("slot") else None})
            results.append(found)
        elif stopped_reason:
            results.append({"signature": sig, "mint": mint, "ok": False,
                             "why_not": f"остановлено на этой сделке -- {stopped_reason}"})
        else:
            results.append({"signature": sig, "mint": mint, "ok": False,
                             "vault": хран, "candidates_seen": len(кандидаты),
                             "why_not": ("продажи этого минта после покупки нет "
                                          "(ещё держит, либо вышел не продажей)")})
    return {"ok": True, "n_signatures_fetched": sigs_seen,
            "n_getSignatures_calls": sig_calls_used,
            "n_getTransaction_calls": tx_calls_used,
            "partial": stopped_reason is not None, "stopped_reason": stopped_reason,
            "trades": results}


def exit_group_summary(exit_trades: list, tax_index: dict) -> dict:
    """Итог ПОСЛЕ налогов (net_sol по цепи, а не сигнал) по группам
    налоговый/обычный -- прямой ответ на вопрос владельца "на какой группе
    он делает плюс". Группа -- по тому же classify_group (по данным
    минта), что и на входе, чтобы граница E была ОДНОЙ и той же величиной
    по обе стороны сделки."""
    by_group: dict = {"tax": [], "normal": [], "unknown": []}
    for r in exit_trades:
        if not r.get("ok"):
            continue
        by_group[classify_group(r.get("mint"), tax_index)].append(r)
    out = {}
    for g, rows in by_group.items():
        net = [r["net_sol"] for r in rows if r.get("net_sol") is not None]
        held = [r["held_slots"] for r in rows if r.get("held_slots") is not None]
        n_pos = sum(1 for x in net if x > 0)
        out[g] = {"n_closed": len(rows), "n_with_net_sol": len(net),
                  "median_net_sol": r6(median(net)), "mean_net_sol": r6(mean(net)),
                  "n_positive": n_pos, "share_positive": r6(n_pos / len(net)) if net else None,
                  "median_held_slots": r6(median(held))}
    return out


def run_task_e(crowd_path: Path, tax_index: dict, rpc_call=None, exit_max_tx: int | None = None) -> dict:
    loaded = load_leader_entries(crowd_path)
    if not loaded.get("ok"):
        return {"ok": False, "why_not": loaded.get("why_not")}
    trades, meta = loaded["trades"], loaded["meta"]
    groups = group_leader_entries(trades, tax_index)
    group_report = {}
    for g in ("tax", "normal", "unknown"):
        group_report[g] = {"entry_size": size_stats(groups[g]), "price_proxy_30_60s": proxy_growth_stats(groups[g])}
    exits = compute_leader_exits(rpc_call, trades, leader=LEADER_BEQV, max_tx=exit_max_tx)
    if exits.get("ok"):
        exit_block = {**exits, "group_summary_after_tax": exit_group_summary(exits["trades"], tax_index)}
    else:
        exit_block = {**exits, "recipe": LEADER_EXIT_RECIPE,
                      "credit_estimate_7d": estimate_exit_credit_cost(meta, trades, days=7),
                      "credit_estimate_14d": estimate_exit_credit_cost(meta, trades, days=14)}
    if isinstance(rpc_call, BudgetedRpc):
        exit_block["chain_credits_used"] = rpc_call.used
        exit_block["chain_calls_used"] = rpc_call.calls
    return {
        "ok": True,
        "leader": LEADER_BEQV,
        "leader_meta": meta,
        "groups_n": {g: len(v) for g, v in groups.items()},
        "groups": group_report,
        "hypothesis_size_bigger_on_normal": hypothesis_size_bigger_on_normal(groups),
        "pickaxe": pickaxe_history(trades),
        "exit_reconstruction": exit_block,
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
    см. тариф в шапке файла), поэтому батчим по 100.

    CreditLimitExceeded от rpc_call (BudgetedRpc) -- честная остановка на
    достигнутом чанке: минты позже в списке помечены why_not с точной
    причиной, уже разобранные (и кэш) в `by_mint` остаются как есть."""
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
    fetched = 0
    stopped_reason: str | None = None
    for i in range(0, len(missing), 100):
        chunk = missing[i:i + 100]
        try:
            res = rpc_call("getMultipleAccounts", [chunk, {"encoding": "jsonParsed"}]) or {}
        except CreditLimitExceeded as exc:
            stopped_reason = str(exc)
            for m in chunk:
                out[m] = {"why_not": f"не запрошено -- {stopped_reason}"}
            break
        credits += CREDIT_DEFAULT
        fetched += len(chunk)
        values = res.get("value") if isinstance(res, dict) else res
        for mint, v in zip(chunk, values or []):
            parsed = parse_mint_extensions_value(v)
            out[mint] = parsed or {"why_not": "счёт не Token-2022 минт с расширениями или не найден"}
    result = {"by_mint": out, "fetched_fresh": fetched, "credits_spent": credits}
    if stopped_reason:
        result["partial"] = True
        result["stopped_reason"] = stopped_reason
    return result


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


def common_authority_stats(infos: list) -> dict:
    """Доля минтов с ПОДТВЕРЖДЁННЫМ общим коллектором среди ЛЮБОГО набора
    разборов getMultipleAccounts (именованных шести или всех налоговых) --
    знаменатель ТОЛЬКО по реально проверенным (has_transfer_fee_config),
    иначе непроверенные молча считались бы "не тот кошелёк". Пустой
    список -- честный why_not, без деления на 0."""
    checked = [v for v in infos if isinstance(v, dict) and v.get("has_transfer_fee_config")]
    if not checked:
        return {"n_checked": 0, "share_same_authority": None,
                "why_not": "ни один минт из набора не проверен (нет в кэше и нет rpc_call)"}
    authorities = [v.get("withdraw_authority") for v in checked]
    common = statistics.mode(authorities) if authorities else None
    n_same = sum(1 for a in authorities if a == common)
    return {"n_checked": len(checked), "common_authority_guess": common,
            "n_same_authority": n_same, "share_same_authority": r6(n_same / len(checked))}


def authority_share(named_report: dict) -> dict:
    """То же самое, но по именованному отчёту ({"SANTA": {...}, ...}) --
    "checked" там не нужен отдельно: у непроверенных записей просто нет
    ключа has_transfer_fee_config, common_authority_stats их и так отсеет."""
    return common_authority_stats(list(named_report.values()))


def all_taxed_mints(tax_index: dict) -> list:
    """Все минты с taxed=True в индексе (147 на момент сбора audit'а) --
    ТОЛЬКО из данных, не с потолка."""
    return sorted(m for m, info in tax_index.items() if isinstance(info, dict) and info.get("taxed"))


def g1_full_check(rpc_call, tax_index: dict, cached_ext: dict) -> dict:
    """G1 ЦЕЛИКОМ: ВСЕ таксируемые минты нашего индекса + шесть именованных
    владельцем (PICKAXE в индекс как таксируемый не попал -- добавлен явно,
    иначе его бы не проверили вовсе, а владелец спрашивал именно про него).
    getMultipleAccounts батчами по 100 -- на ~150 минтов это 2 вызова."""
    taxed = all_taxed_mints(tax_index)
    mints = sorted(set(taxed) | set(NAMED_TAX_MINTS.values()))
    merged = get_mint_fee_authorities(rpc_call, mints, cached=cached_ext)
    stats = common_authority_stats(list(merged["by_mint"].values()))
    named = g1_named_mints_report(merged["by_mint"])
    return {
        "n_total_tax_mints_in_index": len(taxed),
        "n_mints_requested": len(mints),
        "n_checked": stats["n_checked"],
        "common_authority": stats.get("common_authority_guess"),
        "n_with_common_authority": stats.get("n_same_authority"),
        "share_with_common_authority": stats.get("share_same_authority"),
        "why_not": stats.get("why_not"),
        "named_mints": named,
        "fetch_meta": {k: v for k, v in merged.items() if k != "by_mint"},
    }


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


def run_task_g(task_f: dict, tax_index: dict, rpc_call=None) -> dict:
    cached_ext = load_cached_mint_extensions()
    if "__error__" in cached_ext:
        cached_ext = {}
    full = g1_full_check(rpc_call, tax_index, cached_ext)
    tax_groups = _safe_read(TAX_GROUPS_PATH)
    all_check = {
        "done": rpc_call is not None,
        "n_total_tax_mints_in_index": full["n_total_tax_mints_in_index"],
        "n_mints_requested": full["n_mints_requested"],
        "n_checked": full["n_checked"],
        "common_authority": full["common_authority"],
        "share_with_common_authority": full["share_with_common_authority"],
    }
    if rpc_call is None:
        all_check["why_not"] = "не передан --chain (или нет ключа) -- см. g1_authorities.fetch_meta"
        all_check["cost_if_rpc_call_given"] = ("getMultipleAccounts батчами по 100 -> "
            f"ceil({full['n_mints_requested']}/100)={-(-full['n_mints_requested'] // 100)} "
            "вызова(ов) -- дёшево, окупает точную долю по ВСЕМ таксируемым минтам, а не по 6 именованным")
    elif full.get("fetch_meta", {}).get("partial"):
        all_check["partial"] = True
        all_check["stopped_reason"] = full["fetch_meta"].get("stopped_reason")
    if isinstance(rpc_call, BudgetedRpc):
        all_check["chain_credits_used"] = rpc_call.used
    return {
        "g1_authorities": {
            "named_mints": full["named_mints"],
            "share": {"n_checked": full["n_checked"], "common_authority_guess": full["common_authority"],
                      "n_same_authority": full["n_with_common_authority"],
                      "share_same_authority": full["share_with_common_authority"],
                      "why_not": full.get("why_not")},
            "fetch_meta": full["fetch_meta"],
            "all_147_tax_mints_check": all_check,
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


def build_report(crowd_path: Path, state_dir: Path | None, rpc_call=None, *,
                  exit_max_tx: int | None = None, chain_status: dict | None = None) -> dict:
    tax_index = load_tax_index()
    task_e = run_task_e(crowd_path, tax_index, rpc_call=rpc_call, exit_max_tx=exit_max_tx)
    leader_trades = task_e.get("leader_meta") and (load_leader_entries(crowd_path).get("trades") or [])
    task_f = run_task_f(leader_trades or [])
    task_f["state_dir_journals"] = load_state_dir_journals(state_dir)
    task_g = run_task_g(task_f, tax_index, rpc_call=rpc_call)
    report = {
        "schema_version": 1,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "inputs": {"crowd_cache": str(crowd_path), "state_dir": str(state_dir) if state_dir else None,
                   "audit": str(AUDIT_PATH.relative_to(REPO_ROOT)),
                   "tax_groups": str(TAX_GROUPS_PATH.relative_to(REPO_ROOT)),
                   "bloom_report": str(BLOOM_REPORT_PATH.relative_to(REPO_ROOT)),
                   "santa_autopsy": str(SANTA_AUTOPSY_PATH.relative_to(REPO_ROOT)),
                   "mint_extensions_cache": str(MINT_EXT_CACHE_PATH.relative_to(REPO_ROOT))},
        "constraints": {"read_only": True, "no_trades": True,
                        "chain_reads_via": "rpc_call, передаваемый вызывающим (см. c2_shadow_build.py); "
                                           "--chain строит его через c2_common.C2Rpc с потолком кредитов"},
        "chain": chain_status or {"ok": False, "why_not": "--chain не передан -- офлайн-режим на кэше/локальных файлах"},
        "task_e_leader": task_e,
        "task_f_our_gp_trades": task_f,
        "task_g_stonkfun": task_g,
    }
    if isinstance(rpc_call, BudgetedRpc):
        report["chain"] = {**report["chain"], "credits_used": rpc_call.used, "calls_used": rpc_call.calls,
                           "stopped_reason": rpc_call.stopped_reason}
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
    # СТЫКОВКА ПО ТОКЕН-СЧЁТУ: подписи спрашиваются у счёта минта, а не у
    # кошелька, и с until=подпись покупки. Заглушка это и проверяет: если
    # код вернётся к обходу кошелька, обращение придёт не на тот адрес.
    ВАУЛТ = "LEADERVAULTxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    buy_tx2 = mk(["POOLV", "POOLOWN", leader_w, ВАУЛТ],
                 [tb(0, "POOLOWN", "MNT111", 1_000_000), tb(3, leader_w, "MNT111", 0)],
                 [tb(0, "POOLOWN", "MNT111", 500_000), tb(3, leader_w, "MNT111", 500_000)],
                 pre_l=[0, 0, 3_400_000_000, 0], post_l=[0, 0, 3_000_000_000, 0],
                 sg=(leader_w,))
    sell_tx2 = mk(["POOLV", "POOLOWN", leader_w, ВАУЛТ],
                  [tb(0, "POOLOWN", "MNT111", 0), tb(3, leader_w, "MNT111", 500_000)],
                  [tb(0, "POOLOWN", "MNT111", 500_000), tb(3, leader_w, "MNT111", 0)],
                  pre_l=[0, 0, 3_000_000_000, 0], post_l=[0, 0, 3_400_000_000, 0],
                  sg=(leader_w,))
    спросили: list = []

    def fake_rpc(method, params):
        if method == "getSignaturesForAddress":
            спросили.append((params[0], (params[1] or {}).get("until")))
            if params[0] != ВАУЛТ:
                return []
            return [{"signature": sell_sig, "slot": 11, "err": None}]
        if method == "getTransaction":
            return {buy_sig: buy_tx2, sell_sig: sell_tx2}.get(params[0])
        raise AssertionError(f"неожиданный метод {method}")

    res = compute_leader_exits(fake_rpc, entry, leader=leader_w)
    chk("compute_leader_exits: синтетика находит выход и sol_received≈0.4",
        res["ok"] and res["trades"][0]["ok"] and abs(res["trades"][0]["sol_received"] - 0.4) < 1e-6, res)
    chk("compute_leader_exits: подписи спрошены у ТОКЕН-СЧЁТА минта и только после покупки",
        спросили == [(ВАУЛТ, buy_sig)], спросили)
    chk("compute_leader_exits: адрес счёта и число кандидатов записаны в итог",
        res["trades"][0].get("vault") == ВАУЛТ
        and res["trades"][0].get("candidates_seen") == 1
        and res["n_getTransaction_calls"] == 2, res["trades"][0])
    chk("compute_leader_exits: удержание считается по слотам покупки и продажи",
        res["trades"][0].get("held_slots") == 1, res["trades"][0])
    # Продажи нет вовсе -- это НЕ "выход не найден в окне подписей", а честное
    # "ещё держит либо вышел не продажей", и это разные утверждения.
    держит = compute_leader_exits(
        lambda m, p: (buy_tx2 if m == "getTransaction" and p[0] == buy_sig else
                      ([] if m == "getSignaturesForAddress" else None)),
        entry, leader=leader_w)
    chk("compute_leader_exits: нет продаж по счёту -> ok=False с причиной 'ещё держит'",
        держит["ok"] and держит["trades"][0]["ok"] is False
        and "ещё держит" in держит["trades"][0]["why_not"], держит["trades"][0])

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

    # --chain: common_authority_stats -- пустой набор не делит на 0.
    cas_empty = common_authority_stats([])
    chk("common_authority_stats: пустой набор -> n_checked=0, why_not, без деления на 0",
        cas_empty["n_checked"] == 0 and cas_empty["share_same_authority"] is None, cas_empty)
    cas_ok = common_authority_stats([{"has_transfer_fee_config": True, "withdraw_authority": "Z"},
                                     {"has_transfer_fee_config": True, "withdraw_authority": "Z"},
                                     {"why_not": "не проверено"}])
    chk("common_authority_stats: непроверенные (без has_transfer_fee_config) не портят долю",
        cas_ok["n_checked"] == 2 and cas_ok["share_same_authority"] == 1.0, cas_ok)

    # get_mint_fee_authorities: CreditLimitExceeded посреди чанков -- частичный, честный результат.
    def rpc_limit_on_second_chunk(method, params):
        if method != "getMultipleAccounts":
            raise AssertionError(method)
        if params[0][0] == "M100":
            raise CreditLimitExceeded("тестовый стоп на втором чанке")
        return {"value": [synth_value for _ in params[0]]}

    many_mints = [f"M{i}" for i in range(150)]  # >100 -- гарантированно 2 чанка
    gm3 = get_mint_fee_authorities(rpc_limit_on_second_chunk, many_mints, cached={})
    chk("get_mint_fee_authorities: CreditLimitExceeded на 2-м чанке -> partial, 1-й чанк разобран",
        gm3.get("partial") is True and gm3["credits_spent"] == 1
        and gm3["by_mint"]["M0"].get("mint") == "GPMINT111"
        and "тестовый стоп" in gm3["by_mint"]["M100"]["why_not"], gm3)

    # g1_full_check: сквозной синтетический прогон (свой tax_index, свой rpc_call).
    synth_tax_index = {"TAXMINT1": {"taxed": True}, "TAXMINT2": {"taxed": True}, "NOTAX": {"taxed": False}}
    orig_named2 = NAMED_TAX_MINTS
    NAMED_TAX_MINTS = {"GPTEST": "TAXMINT1", "PICKTEST": "PICKMINT_NOT_IN_INDEX"}
    try:
        def rpc_g1(method, params):
            assert method == "getMultipleAccounts"
            return {"value": [synth_value if p in ("TAXMINT1", "TAXMINT2") else None for p in params[0]]}
        g1 = g1_full_check(rpc_g1, synth_tax_index, cached_ext={})
    finally:
        NAMED_TAX_MINTS = orig_named2
    chk("g1_full_check: PICKAXE-подобный минт вне индекса всё равно запрошен явно",
        g1["n_mints_requested"] == 3 and g1["n_total_tax_mints_in_index"] == 2, g1)
    chk("g1_full_check: доля общего кошелька по реально проверенным = 1.0 (у обоих один и тот же AUTH1)",
        g1["n_checked"] == 2 and g1["share_with_common_authority"] == 1.0, g1)

    # compute_leader_exits: --exit-max-tx=0 -- останов ДО первого getTransaction, честно, не падение.
    def rpc_never_tx(method, params):
        if method == "getSignaturesForAddress":
            return sig_pages.get(params[1].get("before"), [])
        raise AssertionError(f"getTransaction не должен звонить при exit_max_tx=0: {method}")

    res_capped = compute_leader_exits(rpc_never_tx, entry, leader=leader_w, max_tx=0)
    chk("compute_leader_exits: --exit-max-tx=0 -> partial, честная причина, без единого getTransaction",
        res_capped["ok"] and res_capped["partial"] is True and res_capped["n_getTransaction_calls"] == 0
        and "exit-max-tx" in res_capped["trades"][0]["why_not"], res_capped)

    # compute_leader_exits: CreditLimitExceeded на getTransaction -- частичный результат, не исключение наружу.
    def rpc_credit_stop(method, params):
        if method == "getSignaturesForAddress":
            return sig_pages.get(params[1].get("before"), [])
        raise CreditLimitExceeded("тестовый стоп на getTransaction")

    res_stopped = compute_leader_exits(rpc_credit_stop, entry, leader=leader_w)
    chk("compute_leader_exits: CreditLimitExceeded -> partial=True, причина в trades, не наружу",
        res_stopped["ok"] and res_stopped["partial"] is True
        and "тестовый стоп" in res_stopped["trades"][0]["why_not"], res_stopped)

    # exit_group_summary: пустая группа не делит на 0, группировка по данным.
    exits_synth = [{"ok": True, "mint": "AAA111", "net_sol": 1.0, "held_slots": 10},
                   {"ok": True, "mint": "AAA111", "net_sol": -0.5, "held_slots": 20},
                   {"ok": True, "mint": "BBB222", "net_sol": 2.0, "held_slots": 5},
                   {"ok": False, "mint": "AAA111", "why_not": "не в счёт"}]
    egs = exit_group_summary(exits_synth, tax_idx)
    chk("exit_group_summary: tax(2 закрытых, 1 в плюс), normal(1 закрытая, в плюс), unknown(0)",
        egs["tax"]["n_closed"] == 2 and egs["tax"]["n_positive"] == 1
        and egs["normal"]["n_closed"] == 1 and egs["normal"]["share_positive"] == 1.0
        and egs["unknown"]["n_closed"] == 0 and egs["unknown"]["share_positive"] is None, egs)

    # BudgetedRpc: локальный --credit-limit стопорит ДО реального вызова.
    class _FakeC2RpcForTest:
        def __init__(self, base):
            self.meter = type("_M", (), {"base": base})()
            self.calls = []

        def call(self, method, params):
            self.calls.append((method, params))
            return {"ok": True}

    tmp_budget = Path(tempfile.mkdtemp())
    fake_rpc_obj = _FakeC2RpcForTest(tmp_budget)
    brpc = BudgetedRpc(fake_rpc_obj, local_limit=2)
    brpc("getTransaction", ["a"])
    brpc("getTransaction", ["b"])
    try:
        brpc("getTransaction", ["c"])
        chk("BudgetedRpc: локальный --credit-limit стопорит", False)
    except CreditLimitExceeded:
        chk("BudgetedRpc: локальный --credit-limit стопорит (2 прошли, 3-й -- нет)",
            brpc.used == 2 and len(fake_rpc_obj.calls) == 2)
    # Повторный вызов после стопа -- та же причина, без повторной попытки достучаться до узла.
    try:
        brpc("getTransaction", ["d"])
        chk("BudgetedRpc: после остановки повторный вызов тоже отказывает", False)
    except CreditLimitExceeded:
        chk("BudgetedRpc: после остановки повторный вызов тоже отказывает, без нового обращения к узлу",
            len(fake_rpc_obj.calls) == 2)

    # BudgetedRpc: владельческий стоп на 70% ОБЩЕЙ суточной квоты C2 (все службы c2_*, не только эта).
    # Расход "чужой" службы записан статически на диск -- фейковый rpc сам ничего не пишет в учёт
    # (в отличие от настоящего C2Rpc), поэтому порог должен быть уже исчерпан ДО первого вызова.
    tmp_owner = Path(tempfile.mkdtemp())
    at_cap = int(OWNER_RESEARCH_BUDGET * OWNER_STOP_FRACTION)  # ровно порог -- уже занят другой службой
    (tmp_owner / "c2_other_service.json").write_text(
        json.dumps({"дни": {C2.today_utc(): {"c2_other_service": {"кредитов_за_день": at_cap}}}}),
        encoding="utf-8")
    fake_rpc_obj2 = _FakeC2RpcForTest(tmp_owner)
    brpc2 = BudgetedRpc(fake_rpc_obj2, local_limit=1_000_000)  # локальный предел заведомо не мешает
    try:
        brpc2("getTransaction", ["a"])
        chk("BudgetedRpc: 70%-порог общей суточной квоты C2 срабатывает", False)
    except CreditLimitExceeded as exc:
        chk("BudgetedRpc: 70%-порог общей суточной квоты C2 срабатывает раньше локального предела "
            "(чужая служба уже заняла порог, наш вызов -- 0 успешных)",
            "70%" in str(exc) and len(fake_rpc_obj2.calls) == 0, str(exc))
    chk("BudgetedRpc: c2_spent_today видит чужую службу (не только свою)",
        C2.c2_spent_today(tmp_owner) == at_cap, C2.c2_spent_today(tmp_owner))

    # resolve_chain_rpc: без ключа (как в ЭТОМ контейнере) -- честный отказ, не исключение.
    no_key_rpc, no_key_status = resolve_chain_rpc(True, 20_000, usage_dir=Path(tempfile.mkdtemp()))
    chk("resolve_chain_rpc: --chain без ключа в окружении -> ok=False, rpc_call=None, без падения",
        no_key_rpc is None and no_key_status["ok"] is False, no_key_status)
    off_rpc, off_status = resolve_chain_rpc(False, 20_000)
    chk("resolve_chain_rpc: без --chain -> офлайн, ok=False с понятной причиной",
        off_rpc is None and off_status["ok"] is False, off_status)
    # С поддельным ключом (только для этой проверки схемы, без единого сетевого вызова) -- строится BudgetedRpc.
    import os as _os
    _old_env = _os.environ.get("HELIUS_API_KEY")
    _os.environ["HELIUS_API_KEY"] = "test_key_self_test_only"
    try:
        with_key_rpc, with_key_status = resolve_chain_rpc(True, 12_345, usage_dir=Path(tempfile.mkdtemp()))
    finally:
        if _old_env is None:
            _os.environ.pop("HELIUS_API_KEY", None)
        else:
            _os.environ["HELIUS_API_KEY"] = _old_env
    chk("resolve_chain_rpc: с ключом в окружении -> BudgetedRpc собран, ни одного сетевого вызова не сделано",
        isinstance(with_key_rpc, BudgetedRpc) and with_key_status["ok"] is True
        and with_key_status["local_credit_limit"] == 12_345, with_key_status)

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
    chk("build_report: собирается целиком без исключений на синтетическом кэше (офлайн)",
        report.get("task_e_leader", {}).get("ok") is True)

    # Тот же пайплайн, но с рабочим (не BudgetedRpc, обычной функцией) rpc_call -- сквозной --chain путь
    # без сети: getTransaction ничего не находит (честно "выход не найден"), getMultipleAccounts отвечает
    # None на всё (частный случай "минт не Token-2022/не найден" из parse_mint_extensions_value).
    def fake_rpc_pipeline(method, params):
        if method == "getSignaturesForAddress":
            return [{"signature": "s4", "slot": 400, "err": None}, {"signature": "s3", "slot": 300, "err": None},
                    {"signature": "s2", "slot": 200, "err": None}, {"signature": "s1", "slot": 100, "err": None}]
        if method == "getTransaction":
            return None
        if method == "getMultipleAccounts":
            return {"value": [None for _ in params[0]]}
        raise AssertionError(f"неожиданный метод в сквозном тесте: {method}")

    report2 = build_report(crowd_path, None, rpc_call=fake_rpc_pipeline,
                           chain_status={"ok": True, "service": "self_test_fake"})
    chk("build_report (--chain, синтетика): exit_reconstruction реально выполнен (ok=True)",
        report2["task_e_leader"]["exit_reconstruction"]["ok"] is True, report2["task_e_leader"]["exit_reconstruction"])
    chk("build_report (--chain, синтетика): G1 'целиком' помечен done=True",
        report2["task_g_stonkfun"]["g1_authorities"]["all_147_tax_mints_check"]["done"] is True)
    for rep_to_check, label in ((report, "офлайн"), (report2, "--chain синтетика")):
        try:
            _assert_ascii_keys(rep_to_check)
            ascii_ok = True
        except AssertionError as exc:
            ascii_ok = False
            print("   не-ASCII ключ:", exc)
        chk(f"итоговый отчёт ({label}): ВСЕ ключи JSON только ASCII", ascii_ok)
        try:
            json.dumps(rep_to_check, ensure_ascii=False)
            chk(f"итоговый отчёт ({label}): сериализуется в JSON без исключений", True)
        except (TypeError, ValueError) as exc:
            chk(f"итоговый отчёт ({label}): сериализуется в JSON без исключений", False, str(exc))

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
    ap.add_argument("--chain", action="store_true",
                    help="читать цепь по-настоящему через c2_common.C2Rpc (нужен HELIUS_API_KEY/HELIUS_API "
                         "в окружении хоста); без флага -- офлайн-режим на кэше и локальных файлах")
    ap.add_argument("--credit-limit", type=int, default=20_000,
                    help="локальный предел кредитов на ЭТОТ прогон (по умолчанию 20000); "
                         "независимо от него прогон также не превысит 70%% общей суточной квоты C2")
    ap.add_argument("--exit-max-tx", type=int, default=None,
                    help="потолок числа getTransaction-вызовов в реконструкции выходов лидера (E); "
                         "по умолчанию не ограничен отдельно -- только --credit-limit и 70%% квоты")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    if not args.crowd or not args.out:
        ap.error("нужны --crowd и --out (или --self-test)")

    rpc_call, chain_status = resolve_chain_rpc(args.chain, args.credit_limit)
    if args.chain:
        log(f"--chain: {chain_status}")

    log(f"читаю кэш толпы {args.crowd}")
    report = build_report(args.crowd, args.state_dir, rpc_call=rpc_call,
                          exit_max_tx=args.exit_max_tx, chain_status=chain_status)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"записано {args.out}" + (f" (кредитов потрачено: {rpc_call.used})" if isinstance(rpc_call, BudgetedRpc) else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
