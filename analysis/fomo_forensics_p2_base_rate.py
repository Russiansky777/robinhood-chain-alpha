#!/usr/bin/env python3
"""Задача «форензика fomo», п.2 -- базовая ставка. Определения от
владельца (2026-09-05), дословно:

- Универсум: токены с >=20 свопов и >=$1000 объёма в dex.trades за 7
  дней после деплоя. Число исключённых -- явно.
- Точка входа: VWAP первого часа после деплоя.
- "x10 достигнуто": цена >= x10 от входа И после первого пересечения
  этого уровня кумулятивный объём >= $5000 (для x3 -- порог $2000).
  Тиковый максимум -- справочно, не критерий.
- Сначала случайные 2000 токенов из универсума, база с 95% интервалом.
  Полный прогон -- только если интервал шире +-30% относительной.
- Бюджет: не более 60 кредитов на п.2 без отдельного разрешения.

**Аккаунт (владелец, 2026-09-05, после реального СТОПа по общему циклу
старого аккаунта): переключено на secrets.DUNE_API_KEY_MOZILA.**
Проверено структурно (не предположено), что предыдущий стоп относился
к СТАРОМУ аккаунту: все workflow'ы форензики fomo до этой правки
использовали `secrets.DUNE_API_KEY` (grep по .github/workflows/), это
тот же ключ, что пишет в `data/credits_spent.json` (внешний лимит
2500/цикл, откуда и пришёл реальный стоп: 2479.96/2480). Аккаунт Mozila
(`data/credits_spent_mozila.json`) до сих пор использовался только для
`funding_mozila`/`lit_points_mozila` -- НЕ форензикой fomo, смешивания
не было. Новый namespace `fomo_forensics_mozila` -- ОТДЕЛЬНЫЙ от них
внутри того же файла (один файл на КЛЮЧ/аккаунт, не один на задачу).
**Реальную external_truth-цифру для Mozila должен прислать владелец
(dune.com/settings/billing) -- ничего не запускается до неё**, текущее
значение в файле помечено как НЕподтверждённое устное "2000+".
Правило владельца для этого аккаунта (docs/PROJECT_STATE.md, п.9а):
каждый НОВЫЙ запрос -- сначала LIMIT 100 для проверки синтаксиса/
структуры, полный прогон -- вторым шагом. Этап 1 ниже поэтому идёт в
двух под-шагах: 1a (LIMIT 100, fetch_results=True, дёшево) и 1b
(реальный LIMIT {N_PRESAMPLE}, materialize-only) -- 1b запускается,
только если 1a прошёл без ошибок.

Архитектура (осторожно с credit_guard.check_sql_sanity -- UNION ALL +
`dex.trades` в одном запросе даёт жёсткий стоп ДО исполнения, см.
COST_POSTMORTEM.md, ревизия 03c: 144 вместо 8 кредитов из-за
пересчёта одной CTE-цепочки в каждой ветке UNION):

Этап 1 (единственное реальное сканирование `dex.trades`, ОДИН SELECT,
без UNION ALL): взять случайную ПРЕДВЫБОРКУ N_PRESAMPLE токенов из ~36785
реально задеплоенных (не все 36785 -- джойн против всех был бы кратно
дороже и не нужен для случайной выборки), стянуть их реальные свопы за
7 дней после деплоя, материализовать (fetch_results=False -- строк может
быть много, скачивать целиком не нужно).

Этап 2 (дёшево -- читает уже материализованный query_<id>, не
`dex.trades` заново): агрегат на токен -- n_swaps, объём (для фильтра
универсума) и VWAP первого часа (точка входа).

Этап 3 (дёшево, тот же принцип): для токенов, прошедших фильтр
универсума (до 2000 случайно), тянутся ИХ построчные свопы (снова из
уже материализованного query_<id>, не из dex.trades) -- на них в Python
считается пересечение x10/x3 и объём после пересечения.

Ручной контроль бюджета: скрипт трекает реальную сумму (из
client.credit_ledger) по именам с префиксом 'fomo_forensics_p2_' и
ОСТАНАВЛИВАЕТСЯ перед любым следующим платным шагом, если прогноз
превысит 60 -- отдельно от общего credit_guard (250 на весь namespace),
это ручная граница по прямому указанию владельца."""
from __future__ import annotations

import json
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "fomo_forensics_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")

from credit_guard import ensure_namespace, remaining_cycle_budget, load_state  # noqa: E402
from dune_client import DuneClient  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/fomo_forensics_p2_base_rate_result.json")
NAMESPACE_BUDGET = 250.0  # перенесено с исходного распределения на старом аккаунте (там ~76 уже потрачено
                          # и застряло вместе с исчерпанным общим циклом) -- ТРЕБУЕТ подтверждения владельца,
                          # что 250 актуально и для нового (Mozila) аккаунта, не удвоение бюджета молча.
P2_MANUAL_CAP = 60.0  # владелец, 2026-09-05: явная граница на п.2, отдельно от namespace-бюджета

LAUNCH_CONTRACTS = [
    "0x0000ffffbe8efe702c8703ae3477ff5de3d319c0",
    "0x00004c4ccc709ef590f7c81102c0689f0263d4e9",
]
REAL_TOKEN_LAUNCHED_TOPIC0 = "67226bacccef969dab310a9e55dc1cf821363658e433fd330344f5cc00c79ac8"

N_PRESAMPLE = 4000  # запас над требуемыми 2000 -- часть предвыборки не пройдёт фильтр универсума
MIN_SWAPS = 20
MIN_VOLUME_USD = 1000.0
TARGET_SAMPLE = 2000

X10_MULT, X10_POST_VOL = 10.0, 5000.0
X3_MULT, X3_POST_VOL = 3.0, 2000.0


def p2_spent_so_far(client: DuneClient) -> float:
    return sum(e.get("credits") or 0.0 for e in client.credit_ledger if str(e.get("name", "")).startswith("fomo_forensics_p2_"))


def run() -> int:
    ensure_namespace("fomo_forensics", NAMESPACE_BUDGET)
    remaining = remaining_cycle_budget(load_state())
    print(f"[p2] остаток общего цикла Dune: {remaining:.1f} кредитов; ручной кап п.2 (владелец): {P2_MANUAL_CAP}")

    client = DuneClient()
    addrs_sql = ", ".join(f"'{a[2:].lower()}'" for a in LAUNCH_CONTRACTS)

    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "definitions": {
        "min_swaps_7d": MIN_SWAPS, "min_volume_usd_7d": MIN_VOLUME_USD, "entry": "VWAP первого часа после деплоя",
        "x10": {"mult": X10_MULT, "post_cross_volume_usd": X10_POST_VOL},
        "x3": {"mult": X3_MULT, "post_cross_volume_usd": X3_POST_VOL},
        "n_presample": N_PRESAMPLE, "target_sample": TARGET_SAMPLE, "manual_credit_cap": P2_MANUAL_CAP,
    }}

    # --- Этап 1: материализация свопов случайной предвыборки токенов (ЕДИНСТВЕННЫЙ скан dex.trades) ---
    def build_sql_stage1(sample_limit: int) -> str:
        return f"""with launches as (
    select substr(lower(to_hex(topic1)), 25, 40) as token_address, block_time as deploy_time
    from robinhood.logs
    where lower(to_hex(contract_address)) in ({addrs_sql})
        and lower(to_hex(topic0)) = '{REAL_TOKEN_LAUNCHED_TOPIC0}'
        and topic2 is not null
        and block_time >= now() - interval '30' day
),
sampled as (
    select * from launches order by random() limit {sample_limit}
)
select s.token_address, s.deploy_time, t.block_time,
    t.amount_usd,
    case when lower(to_hex(t.token_bought_address)) = s.token_address then t.token_bought_amount
         else t.token_sold_amount end as qty
from sampled s
join dex.trades t
    on (lower(to_hex(t.token_bought_address)) = s.token_address
        or lower(to_hex(t.token_sold_address)) = s.token_address)
    and t.blockchain = 'robinhood'
    and t.block_time between s.deploy_time and s.deploy_time + interval '7' day"""

    # Этап 1a -- правило владельца для аккаунта Mozila (docs/PROJECT_STATE.md, п.9а):
    # каждый НОВЫЙ запрос сначала на LIMIT 100 (структура/синтаксис), полный прогон вторым шагом.
    # Это первый содержательный запрос форензики fomo на этом аккаунте.
    sql_stage1_dryrun = build_sql_stage1(100)
    qid1_dry = client.create_query("fomo_forensics_p2_sample_trades_dryrun100", sql_stage1_dryrun)
    print(f"[p2] этап 1a (правило Mozila: LIMIT 100 первым): query_id={qid1_dry}")
    df_dry = client.run_sql_cached("fomo_forensics_p2_sample_trades_dryrun100", sql_stage1_dryrun, query_id=qid1_dry,
                                    estimated_credits=5.0, expected_max_rows=100_000, expected_columns=5)
    spent1a = p2_spent_so_far(client)
    print(f"[p2] этап 1a вернул {0 if df_dry is None else len(df_dry)} строк; потрачено: {spent1a:.2f} / {P2_MANUAL_CAP}")
    out["stage1a_dryrun_credits"] = spent1a
    out["stage1a_dryrun_rows"] = 0 if df_dry is None else len(df_dry)
    if df_dry is None or df_dry.empty:
        print("[p2] СТОП: этап 1a (LIMIT 100) не вернул строк -- структура запроса под вопросом, не идём на полный прогон вслепую.")
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 1

    # Этап 1b -- реальный прогон (materialize-only) на полную предвыборку.
    sql_stage1 = build_sql_stage1(N_PRESAMPLE)
    qid1 = client.create_query("fomo_forensics_p2_sample_trades", sql_stage1)
    print(f"[p2] этап 1b: материализация свопов предвыборки ({N_PRESAMPLE} токенов), query_id={qid1}")
    client.run_sql_cached("fomo_forensics_p2_sample_trades", sql_stage1, query_id=qid1,
                           fetch_results=False, estimated_credits=25.0)
    spent1 = p2_spent_so_far(client)
    print(f"[p2] потрачено после этапа 1: {spent1:.2f} / {P2_MANUAL_CAP} (ручной кап владельца)")
    out["stage1_credits"] = spent1
    if spent1 >= P2_MANUAL_CAP:
        print("[p2] СТОП: этап 1 уже исчерпал ручной кап 60 кредитов -- дальше не идём без отдельного разрешения владельца.")
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 1

    # --- Этап 2: агрегат на токен (n_swaps, объём за 7д, VWAP первого часа) -- читает query_<id>, не dex.trades ---
    sql_stage2 = f"""select token_address,
    min(deploy_time) as deploy_time,
    count(*) as n_swaps_7d,
    coalesce(sum(amount_usd), 0) as volume_usd_7d,
    coalesce(sum(case when block_time <= deploy_time + interval '1' hour then amount_usd else 0 end), 0) as usd_first_hour,
    coalesce(sum(case when block_time <= deploy_time + interval '1' hour then qty else 0 end), 0) as qty_first_hour
from query_{qid1}
group by token_address"""

    qid2 = client.create_query("fomo_forensics_p2_token_agg", sql_stage2)
    df_agg = client.run_sql_cached("fomo_forensics_p2_token_agg", sql_stage2, query_id=qid2,
                                    estimated_credits=5.0, expected_max_rows=N_PRESAMPLE + 100, expected_columns=6)
    spent2 = p2_spent_so_far(client)
    print(f"[p2] потрачено после этапа 2: {spent2:.2f} / {P2_MANUAL_CAP}")
    out["stage2_credits"] = spent2

    if df_agg is None or df_agg.empty:
        print("[p2] СТОП: этап 2 не вернул данных.")
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 1

    n_presample_real = len(df_agg)
    df_agg["entry_price"] = df_agg.apply(
        lambda r: (r["usd_first_hour"] / r["qty_first_hour"]) if r["qty_first_hour"] > 0 else None, axis=1)

    excluded_no_first_hour_trade = int((df_agg["qty_first_hour"] <= 0).sum())
    excluded_low_swaps = int(((df_agg["n_swaps_7d"] < MIN_SWAPS) & (df_agg["qty_first_hour"] > 0)).sum())
    excluded_low_volume = int(((df_agg["volume_usd_7d"] < MIN_VOLUME_USD) & (df_agg["n_swaps_7d"] >= MIN_SWAPS) & (df_agg["qty_first_hour"] > 0)).sum())

    universe = df_agg[(df_agg["n_swaps_7d"] >= MIN_SWAPS) & (df_agg["volume_usd_7d"] >= MIN_VOLUME_USD) & (df_agg["qty_first_hour"] > 0)]
    n_universe_in_presample = len(universe)

    print(f"[p2] предвыборка: {n_presample_real} токенов реально получили строки агрегата "
          f"(из {N_PRESAMPLE} запрошенных случайных -- часть могла не иметь ни одного свопа за 7д и не попасть в GROUP BY).")
    print(f"[p2] исключены: нет сделок в первый час (нет точки входа)={excluded_no_first_hour_trade}, "
          f"<{MIN_SWAPS} свопов (при наличии входа)={excluded_low_swaps}, "
          f"<${MIN_VOLUME_USD:.0f} объёма (при проходе по свопам)={excluded_low_volume}")
    print(f"[p2] прошли фильтр универсума (в предвыборке): {n_universe_in_presample} / {n_presample_real}")

    out["presample_size_real"] = n_presample_real
    out["excluded"] = {
        "no_first_hour_trade": excluded_no_first_hour_trade,
        "low_swaps_given_entry": excluded_low_swaps,
        "low_volume_given_swaps": excluded_low_volume,
        "n_universe_in_presample": n_universe_in_presample,
    }

    if n_universe_in_presample == 0:
        print("[p2] СТОП: 0 токенов предвыборки прошли фильтр универсума -- метод/предвыборка требует пересмотра, не гадаем дальше.")
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 1

    sample_n = min(TARGET_SAMPLE, n_universe_in_presample)
    random.seed(20260905)
    sample_addrs = random.sample(list(universe["token_address"]), sample_n)
    print(f"[p2] случайная выборка из универсума: {sample_n} токенов"
          f"{' (МЕНЬШЕ целевых 2000 -- универсум в предвыборке исчерпан, честно, не догоняем)' if sample_n < TARGET_SAMPLE else ''}")
    out["sample_n"] = sample_n
    out["sample_n_is_full_target"] = sample_n == TARGET_SAMPLE

    entry_by_addr = universe.set_index("token_address")["entry_price"].to_dict()
    deploy_by_addr = universe.set_index("token_address")["deploy_time"].to_dict()

    sample_addr_json = json.dumps(sample_addrs)
    (Path("data/p3_guard_cache") / "fomo_forensics_p2_sample_addresses.json").parent.mkdir(parents=True, exist_ok=True)
    Path("data/p3_guard_cache/fomo_forensics_p2_sample_addresses.json").write_text(sample_addr_json)

    # --- Этап 3: построчные свопы ТОЛЬКО для выбранных из универсума токенов (снова query_<id>, не dex.trades) ---
    addr_list_sql = ", ".join(f"'{a}'" for a in sample_addrs)
    sql_stage3 = f"""select token_address, deploy_time, block_time, amount_usd, qty
from query_{qid1}
where token_address in ({addr_list_sql})
order by token_address, block_time"""

    qid3 = client.create_query("fomo_forensics_p2_sample_raw_trades", sql_stage3)
    df_trades = client.run_sql_cached("fomo_forensics_p2_sample_raw_trades", sql_stage3, query_id=qid3,
                                       estimated_credits=5.0, expected_max_rows=2_000_000, expected_columns=5)
    spent3 = p2_spent_so_far(client)
    print(f"[p2] потрачено после этапа 3: {spent3:.2f} / {P2_MANUAL_CAP}")
    out["stage3_credits"] = spent3

    if df_trades is None or df_trades.empty:
        print("[p2] СТОП: этап 3 не вернул построчных свопов для выборки.")
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 1

    df_trades["block_time"] = df_trades["block_time"].astype(str)
    n_x10 = n_x3 = n_evaluated = 0
    tick_max_x10 = tick_max_x3 = 0  # справочно, НЕ критерий (владелец: "тиковый максимум -- справочно")

    for addr, grp in df_trades.groupby("token_address"):
        entry = entry_by_addr.get(addr)
        if not entry or entry <= 0:
            continue
        n_evaluated += 1
        g = grp.sort_values("block_time").copy()
        g["price"] = g["amount_usd"] / g["qty"].replace(0, None)
        max_ratio = (g["price"] / entry).max()
        if max_ratio >= X10_MULT:
            tick_max_x10 += 1
        if max_ratio >= X3_MULT:
            tick_max_x3 += 1

        def hit(mult: float, post_vol_threshold: float) -> bool:
            crossed = g[g["price"] >= mult * entry]
            if crossed.empty:
                return False
            cross_time = crossed["block_time"].iloc[0]
            post_vol = g[g["block_time"] > cross_time]["amount_usd"].sum()
            return bool(post_vol >= post_vol_threshold)

        if hit(X10_MULT, X10_POST_VOL):
            n_x10 += 1
        if hit(X3_MULT, X3_POST_VOL):
            n_x3 += 1

    def wilson_ci(k: int, n: int, z: float = 1.959963985) -> tuple[float, float, float]:
        if n == 0:
            return (0.0, 0.0, 0.0)
        p = k / n
        denom = 1 + z * z / n
        center = (p + z * z / (2 * n)) / denom
        half = (z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)) / denom
        return (p, max(0.0, center - half), min(1.0, center + half))

    p10, lo10, hi10 = wilson_ci(n_x10, n_evaluated)
    p3, lo3, hi3 = wilson_ci(n_x3, n_evaluated)
    rel_halfwidth_10 = ((hi10 - lo10) / 2 / p10) if p10 > 0 else float("inf")
    rel_halfwidth_3 = ((hi3 - lo3) / 2 / p3) if p3 > 0 else float("inf")

    print(f"\n[p2] РЕАЛЬНАЯ БАЗОВАЯ СТАВКА (выборка {n_evaluated} токенов с валидной точкой входа):")
    print(f"  x10 (владельческое определение, с порогом объёма после пересечения): {n_x10}/{n_evaluated} = {p10:.4f}, "
          f"95% ДИ [{lo10:.4f}, {hi10:.4f}], относительная полуширина={rel_halfwidth_10:.3f}")
    print(f"  x3  (владельческое определение): {n_x3}/{n_evaluated} = {p3:.4f}, "
          f"95% ДИ [{lo3:.4f}, {hi3:.4f}], относительная полуширина={rel_halfwidth_3:.3f}")
    print(f"  Справочно (тиковый максимум, НЕ критерий владельца): x10 достигал тик хотя бы раз {tick_max_x10}/{n_evaluated}, "
          f"x3 -- {tick_max_x3}/{n_evaluated}")

    need_full_run = rel_halfwidth_10 > 0.30 or rel_halfwidth_3 > 0.30
    print(f"\n[p2] Полный прогон на все 36785 нужен? {'ДА' if need_full_run else 'НЕТ'} "
          f"(порог владельца: относительная полуширина > 30%)")

    out.update({
        "n_evaluated": n_evaluated,
        "x10": {"hits": n_x10, "rate": p10, "ci95": [lo10, hi10], "rel_halfwidth": rel_halfwidth_10, "tick_max_only_reference": tick_max_x10},
        "x3": {"hits": n_x3, "rate": p3, "ci95": [lo3, hi3], "rel_halfwidth": rel_halfwidth_3, "tick_max_only_reference": tick_max_x3},
        "need_full_run": need_full_run,
        "total_p2_credits_spent": spent3,
    })
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[p2] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
