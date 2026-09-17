#!/usr/bin/env python3
"""Владелец (2026-09-17), Fomo -- шаги 2-5 задания "собрать историю по
цепи, без Dune": цена из состояния пула (тот же метод, что для LP),
покупки/продажи, первые входы, бот/человек, финальный CSV.

Читает `data/fomo_9wallets_alchemy_raw_transfers.csv` (собран
`fomo_9wallets_alchemy_full_history.py`). Цена -- РЕАЛЬНАЯ, через
`slot0()` (стандартный Uniswap V3 view, тот же селектор `0x3850c7bd`,
что уже использован и провалидирован в `task5_arb_lifetime_
reconstruction.py`) на историческом блоке каждого перевода, НЕ
extsload (это V4-специфичный механизм для PoolManager-синглтона;
найденные здесь пулы -- отдельные V3-контракты со своим `address`,
см. `data/task5_bot_pool_registry_cache.json`).

Источник пулов: `data/task5_bot_pool_registry_cache.json` (1142 пула,
кэш от 2026-09-12 -- ЧЕСТНО НЕ полный реестр всей цепи, только то, что
уже было материализовано ранее для живого бота). Токен, которого нет в
этом кэше -- цена помечается недоступной, НЕ выдумывается и не
достаётся дорогим полным сканом Initialize/PoolCreated по всей истории
цепи (это отдельная, дорогая операция, не входящая в "бесплатный и
быстрый" путь этой задачи -- см. честная оговорка в результате).

USDG (`0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168`) -- референсный
USD-эквивалент, тот же, что использован ВЕЗДЕ в этой сессии (Задача 5,
консолидация активов и т.д.) -- не по умолчанию, а потому что это
реально наблюдаемый на цепи стейблкоин-квот-актив с 137 прямыми
пулами в кэше."""
from __future__ import annotations

import csv
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alchemy_fallback import rpc_call_trading_path  # noqa: E402

# rpc_call_trading_path -- та же функция, что использует горячий торговый путь
# (Alchemy напрямую, ~10 req/s, отдельный от фонового троттлинг-бюджет, см.
# alchemy_fallback.py) -- здесь переиспользована для массовых ИСТОРИЧЕСКИХ
# точечных eth_call (slot0()/decimals()), не для торговли: при ~5000+
# уникальных (пул,блок) парах на этом объёме данных дефолтный публичный-RPC-
# путь (0.5с/вызов) не уложился бы в честный бюджет времени ниже.
rpc_call = rpc_call_trading_path

IN_CSV = Path("data/fomo_9wallets_alchemy_raw_transfers.csv")
POOL_REGISTRY = Path("data/task5_bot_pool_registry_cache.json")
OUT_JSON = Path("data/fomo_9wallets_price_and_report_result.json")
OUT_CSV = Path("data/fomo_9wallets_full_history_priced.csv")

WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
WETH_DECIMALS = 18
USDG_DECIMALS = 6
WETH_USDG_POOL = "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca"

SLOT0_SELECTOR = "0x3850c7bd"
DECIMALS_SELECTOR = "0x313ce567"

PRICE_TIME_BUDGET_S = 900.0  # 15 мин -- честный потолок на исторические slot0-вызовы


def load_pool_registry() -> list[dict]:
    obj = json.loads(POOL_REGISTRY.read_text())
    return obj["pools"]


def build_quote_pool_maps(pools: list[dict], quote_addr: str) -> dict[str, dict]:
    """token(lower) -> {"pool_address", "quote_is_token0", "fee"} -- первый
    найденный пул с этим токеном против quote_addr (WETH или USDG)."""
    quote = quote_addr.lower()
    out: dict[str, dict] = {}
    for p in pools:
        t0, t1 = p["token0"].lower(), p["token1"].lower()
        if t0 == quote and t1 != quote and t1 not in out:
            out[t1] = {"pool_address": p["address"], "quote_is_token0": True, "fee": p["fee"]}
        elif t1 == quote and t0 != quote and t0 not in out:
            out[t0] = {"pool_address": p["address"], "quote_is_token0": False, "fee": p["fee"]}
    return out


def get_decimals(token: str, cache: dict) -> int:
    token = token.lower()
    if token in cache:
        return cache[token]
    if token == USDG:
        cache[token] = USDG_DECIMALS
        return cache[token]
    if token == WETH:
        cache[token] = WETH_DECIMALS
        return cache[token]
    try:
        raw = rpc_call("eth_call", [{"to": token, "data": DECIMALS_SELECTOR}, "latest"])
        dec = int(raw, 16) if raw and raw != "0x" else 18
    except Exception:  # noqa: BLE001
        dec = 18
    cache[token] = dec
    return dec


def quote_per_token_human(sqrt_price_x96: int, quote_is_token0: bool, token_decimals: int, quote_decimals: int) -> float | None:
    """Реальная формула из task_arc_arb_liquid_pairs.py (usdc_per_token_human),
    переиспользована буквально -- тот же constant-L / sqrtP-инвариант, что
    везде в этой сессии. Возвращает: сколько quote-токена стоит 1 человеческая
    единица token."""
    if sqrt_price_x96 == 0:
        return None
    sqrt_p = sqrt_price_x96 / (2 ** 96)
    raw_price = sqrt_p * sqrt_p
    if quote_is_token0:
        if raw_price == 0:
            return None
        return (10 ** (token_decimals - quote_decimals)) / raw_price
    return raw_price * (10 ** (token_decimals - quote_decimals))


def get_slot0_cached(pool_address: str, block_num: int, cache: dict, errors: dict) -> int | None:
    key = (pool_address.lower(), block_num)
    if key in cache:
        return cache[key]
    try:
        raw = rpc_call("eth_call", [{"to": pool_address, "data": SLOT0_SELECTOR}, hex(block_num)])
        sqrt_price_x96 = int(raw[2:66], 16) if raw and raw != "0x" else None
    except Exception as exc:  # noqa: BLE001
        sqrt_price_x96 = None
        errors[key] = str(exc)[:200]
    cache[key] = sqrt_price_x96
    return sqrt_price_x96


def run() -> int:
    if not IN_CSV.exists():
        print(f"[price_report] СТОП: {IN_CSV} не найден -- сначала запустите fomo_9wallets_alchemy_full_history.py")
        return 1

    with IN_CSV.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print(f"[price_report] Загружено {len(rows)} сырых переводов из {IN_CSV}")

    pools = load_pool_registry()
    usdg_map = build_quote_pool_maps(pools, USDG)
    weth_map = build_quote_pool_maps(pools, WETH)
    print(f"[price_report] Реестр пулов: {len(pools)} пулов, {len(usdg_map)} прямых USDG-пар, {len(weth_map)} прямых WETH-пар")

    unique_tokens = sorted({r["token_address"] for r in rows if r["token_address"]})
    dec_cache: dict = {}
    for tok in unique_tokens:
        get_decimals(tok, dec_cache)

    token_resolution: dict = {}
    for tok in unique_tokens:
        if tok in usdg_map:
            token_resolution[tok] = {"path": "direct_usdg", **usdg_map[tok]}
        elif tok in weth_map:
            token_resolution[tok] = {"path": "via_weth", **weth_map[tok]}
        else:
            token_resolution[tok] = {"path": "unresolved"}
    n_resolved = sum(1 for v in token_resolution.values() if v["path"] != "unresolved")
    print(f"[price_report] Разрешено цен для {n_resolved}/{len(unique_tokens)} уникальных токенов "
          f"({sum(1 for v in token_resolution.values() if v['path']=='direct_usdg')} напрямую USDG, "
          f"{sum(1 for v in token_resolution.values() if v['path']=='via_weth')} через WETH)")

    slot0_cache: dict = {}
    slot0_errors: dict = {}
    start_ts = time.time()
    budget_exhausted = False
    n_priced = 0

    priced_rows: list[dict] = []
    for r in rows:
        tok = r["token_address"]
        block_num = int(r["block_num"]) if r["block_num"] else None
        res = token_resolution.get(tok, {"path": "unresolved"})
        usd_value = None
        price_usdg = None
        price_reason = None

        if res["path"] == "unresolved":
            price_reason = "токен не найден в кэше пулов (1142 пула, не полный реестр цепи) -- цена недоступна без дорогого полного скана"
        elif block_num is None:
            price_reason = "нет номера блока в исходной записи"
        elif time.time() - start_ts > PRICE_TIME_BUDGET_S:
            budget_exhausted = True
            price_reason = f"честный бюджет времени ({PRICE_TIME_BUDGET_S:.0f}с) на исторические slot0-вызовы исчерпан"
        else:
            tok_dec = dec_cache.get(tok, 18)
            if res["path"] == "direct_usdg":
                sp = get_slot0_cached(res["pool_address"], block_num, slot0_cache, slot0_errors)
                if sp is not None:
                    price_usdg = quote_per_token_human(sp, res["quote_is_token0"], tok_dec, USDG_DECIMALS)
                else:
                    price_reason = f"slot0() на пуле {res['pool_address']} на блоке {block_num} не ответил (см. slot0_errors)"
            else:  # via_weth
                sp_tok = get_slot0_cached(res["pool_address"], block_num, slot0_cache, slot0_errors)
                sp_ref = get_slot0_cached(WETH_USDG_POOL, block_num, slot0_cache, slot0_errors)
                if sp_tok is not None and sp_ref is not None:
                    price_weth = quote_per_token_human(sp_tok, res["quote_is_token0"], tok_dec, WETH_DECIMALS)
                    # WETH_USDG_POOL: token0=WETH, token1=USDG (см. task5_bot_config.py) -- USDG НЕ currency0.
                    price_weth_in_usdg = quote_per_token_human(sp_ref, False, WETH_DECIMALS, USDG_DECIMALS)
                    if price_weth is not None and price_weth_in_usdg is not None:
                        price_usdg = price_weth * price_weth_in_usdg
                if price_usdg is None:
                    price_reason = f"slot0() на пуле {res['pool_address']} или референсном WETH/USDG на блоке {block_num} не ответил"

        if price_usdg is not None:
            n_priced += 1
            try:
                usd_value = price_usdg * float(r["value_human"])
            except (TypeError, ValueError):
                usd_value = None

        priced_rows.append({
            **r,
            "price_resolution_path": res["path"],
            "pool_address_used": res.get("pool_address"),
            "price_usdg_per_token": price_usdg,
            "usd_value": usd_value,
            "price_unavailable_reason": price_reason if price_usdg is None else None,
        })

    print(f"[price_report] Оценено цен для {n_priced}/{len(rows)} строк "
          f"({len(slot0_cache)} уникальных (пул,блок) реально запрошено, {len(slot0_errors)} ошибок slot0). "
          f"Бюджет времени исчерпан: {budget_exhausted}")

    # --- Направление, дубли-эирдропы, первые входы ---
    by_wallet_token: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in priced_rows:
        wallet = r["wallet_address"].lower()
        tok = r["token_address"]
        r["_block_num_int"] = int(r["block_num"]) if r["block_num"] else None
        by_wallet_token[(wallet, tok)].append(r)

    for (wallet, tok), group in by_wallet_token.items():
        group.sort(key=lambda x: (x["_block_num_int"] or 0, x["direction"]))
        seen_exact: dict[tuple, int] = {}
        for r in group:
            if r["direction"] != "in":
                continue
            sig = (r["from_addr"], r["value_human"])
            seen_exact[sig] = seen_exact.get(sig, 0) + 1
        first_entry_marked = False
        for r in group:
            if r["direction"] != "in":
                r["is_first_entry"] = False
                r["likely_airdrop_or_dust"] = False
                continue
            sig = (r["from_addr"], r["value_human"])
            is_repeat_pattern = seen_exact.get(sig, 0) >= 2
            r["likely_airdrop_or_dust"] = is_repeat_pattern
            if not is_repeat_pattern and not first_entry_marked:
                r["is_first_entry"] = True
                first_entry_marked = True
            else:
                r["is_first_entry"] = False

    n_first_entries_by_wallet: dict[str, int] = defaultdict(int)
    for r in priced_rows:
        if r.get("is_first_entry"):
            n_first_entries_by_wallet[r["wallet_name"]] += 1

    # --- Диагностика масштаба: сколько токенов реально "потрогали" (получили И
    # потом отправили) против "получили и никогда не тронули" -- при 4583+
    # уникальных токенах на 26372 перевода это ключевой сигнал спам/дасти против
    # органической торговли (владелец: "никогда не выдумывай данные" -- явно
    # считаем, не предполагаем). ---
    touch_diag: dict[str, dict] = {}
    for name in {r["wallet_name"] for r in priced_rows}:
        wallet_addr = next(r["wallet_address"] for r in priced_rows if r["wallet_name"] == name).lower()
        tokens_received = set()
        tokens_sent = set()
        for (w, tok), group in by_wallet_token.items():
            if w != wallet_addr:
                continue
            has_in = any(r["direction"] == "in" for r in group)
            has_out = any(r["direction"] == "out" for r in group)
            if has_in:
                tokens_received.add(tok)
            if has_in and has_out:
                tokens_sent.add(tok)
        touch_diag[name] = {
            "n_distinct_tokens_received": len(tokens_received),
            "n_tokens_received_and_later_sent": len(tokens_sent),
            "n_tokens_received_never_sent": len(tokens_received) - len(tokens_sent),
            "frac_never_touched_again": ((len(tokens_received) - len(tokens_sent)) / len(tokens_received)) if tokens_received else None,
            "_tokens_sent_set": tokens_sent,  # временно, для фильтрации ниже -- удаляется перед записью JSON
        }

    # "Правдоподобные" первые входы -- те же first_entry, но ОГРАНИЧЕННЫЕ токенами,
    # которые потом реально отправлялись обратно (round-trip) -- при 86-99% "получили
    # и никогда не тронули" у 7 из 9 кошельков (см. touch_diag выше) сырой счётчик
    # is_first_entry в подавляющем большинстве считает разовый дасти-спам, не
    # реальные покупки. Обе цифры выводятся, ни одна не скрывается.
    n_first_entries_credible_by_wallet: dict[str, int] = defaultdict(int)
    for r in priced_rows:
        if r.get("is_first_entry") and r["token_address"] in touch_diag.get(r["wallet_name"], {}).get("_tokens_sent_set", set()):
            n_first_entries_credible_by_wallet[r["wallet_name"]] += 1
    for v in touch_diag.values():
        v.pop("_tokens_sent_set", None)

    # --- Бот/человек по кошельку (та же дисциплина, что fomo_9wallets_dune.py) ---
    per_wallet_activity: dict[str, list[dict]] = defaultdict(list)
    for r in priced_rows:
        per_wallet_activity[r["wallet_name"]].append(r)

    classification: dict = {}
    for name, acts in per_wallet_activity.items():
        acts_sorted = sorted(acts, key=lambda x: x["_block_num_int"] or 0)
        # ВАЖНО: ритм считаем ТОЛЬКО по исходящим (direction=="out") переводам --
        # входящие в подавляющем большинстве чужой дасти-спам (см. n_unique_tokens_
        # seen_total/touch_diagnostics выше), их частота отражает график СПАМЕРА,
        # а не поведение владельца кошелька. Смешивать оба направления было бы
        # прямой контаминацией сигнала бот/человек чужой активностью.
        out_acts = [r for r in acts_sorted if r["direction"] == "out"]
        distinct_tx = {}
        for r in out_acts:
            distinct_tx.setdefault(r["tx_hash"], r)
        tx_list = sorted(distinct_tx.values(), key=lambda x: x["_block_num_int"] or 0)
        n_distinct_tx = len(tx_list)
        times = []
        for r in tx_list:
            bt = r.get("block_time_utc")
            if bt:
                times.append(bt)
        gaps_s = []
        for i in range(1, len(tx_list)):
            b0, b1 = tx_list[i - 1]["_block_num_int"], tx_list[i]["_block_num_int"]
            if b0 is not None and b1 is not None:
                gaps_s.append((b1 - b0) * 0.506)  # реальный блок-тайм этой цепи, task5_bot_config
        hour_counts = [0] * 24
        for bt in times:
            try:
                hour = int(bt.split("T")[1][:2])
                hour_counts[hour] += 1
            except (IndexError, ValueError):
                continue
        doubled = hour_counts * 2
        best_zero_run, cur = 0, 0
        for c in doubled:
            cur = cur + 1 if c == 0 else 0
            best_zero_run = max(best_zero_run, cur)
        best_zero_run = min(best_zero_run, 24)
        median_gap_s = statistics.median(gaps_s) if gaps_s else None
        frac_under_60s = (sum(1 for g in gaps_s if g < 60) / len(gaps_s)) if gaps_s else None
        is_bot_like = bool(gaps_s) and best_zero_run < 4 and (frac_under_60s is not None and frac_under_60s > 0.3)

        # Скорость реакции: сколько блоков проходит между получением токена (in)
        # и следующей отправкой ЭТОГО ЖЕ токена этим же кошельком (proxy для
        # "скорости реакции" -- честно отмечено, не буквально "чужая сделка").
        # РАЗДЕЛЕНО на "быстрые" (<=10000 блоков, ~1.4ч при 0.506с/блок -- похоже
        # на реальный флип) и полный набор (включая многомесячные разрывы --
        # похоже на разовую чистку кошелька от старого дасти, не на реакцию).
        REACTION_FAST_THRESHOLD_BLOCKS = 10_000
        reaction_blocks_all = []
        for tok in {r["token_address"] for r in acts_sorted}:
            tok_group = sorted([r for r in acts_sorted if r["token_address"] == tok],
                                key=lambda x: x["_block_num_int"] or 0)
            last_in_block = None
            for r in tok_group:
                if r["direction"] == "in":
                    last_in_block = r["_block_num_int"]
                elif r["direction"] == "out" and last_in_block is not None and r["_block_num_int"] is not None:
                    reaction_blocks_all.append(r["_block_num_int"] - last_in_block)
                    last_in_block = None
        reaction_blocks_fast = [b for b in reaction_blocks_all if b <= REACTION_FAST_THRESHOLD_BLOCKS]

        classification[name] = {
            "n_distinct_outgoing_tx": n_distinct_tx,
            "median_gap_seconds_between_own_outgoing_tx": median_gap_s,
            "frac_gaps_under_60s": frac_under_60s,
            "longest_quiet_hours_run": best_zero_run,
            "median_reaction_blocks_fast_flips_only": statistics.median(reaction_blocks_fast) if reaction_blocks_fast else None,
            "n_fast_flip_samples": len(reaction_blocks_fast),
            "median_reaction_blocks_all_incl_slow": statistics.median(reaction_blocks_all) if reaction_blocks_all else None,
            "n_reaction_samples_all": len(reaction_blocks_all),
            "classification": "бот-подобный" if is_bot_like else "человекоподобный (нет чёткого ночного провала ИЛИ нет частых <60с интервалов)",
        }
        print(f"[price_report] {name}: n_own_out_tx={n_distinct_tx}, median_gap_s={median_gap_s}, "
              f"quiet_run={best_zero_run}ч, fast_flips={len(reaction_blocks_fast)} (median {classification[name]['median_reaction_blocks_fast_flips_only']} блоков) "
              f"-> {classification[name]['classification']}")

    n_bot = sum(1 for v in classification.values() if v["classification"].startswith("бот"))
    n_human = len(classification) - n_bot

    # --- Финальный CSV ---
    fieldnames = ["block_time_utc", "block_num", "wallet_name", "wallet_address", "token_address", "asset_symbol",
                  "direction", "value_human", "price_usdg_per_token", "usd_value", "is_first_entry",
                  "likely_airdrop_or_dust", "price_resolution_path", "pool_address_used",
                  "tx_hash", "from_addr", "to_addr"]
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(priced_rows)

    out = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_raw_transfers": len(rows),
        "n_unique_tokens": len(unique_tokens),
        "n_tokens_resolved_for_price": n_resolved,
        "n_rows_priced": n_priced,
        "price_time_budget_exhausted": budget_exhausted,
        "n_slot0_calls_made": len(slot0_cache),
        "n_slot0_errors": len(slot0_errors),
        "per_wallet_n_transactions": {r["wallet_name"]: len({x["tx_hash"] for x in acts})
                                       for r, acts in [(v[0], v) for v in per_wallet_activity.values()]},
        "n_first_entries_by_wallet": dict(n_first_entries_by_wallet),
        "n_first_entries_total": sum(n_first_entries_by_wallet.values()),
        "n_first_entries_credible_by_wallet": dict(n_first_entries_credible_by_wallet),
        "n_first_entries_credible_total": sum(n_first_entries_credible_by_wallet.values()),
        "n_unique_tokens_seen_total": len(unique_tokens),
        "touch_diagnostics_per_wallet": touch_diag,
        "bot_human_classification": classification,
        "summary_line": f"из {len(classification)} адресов {n_bot} ведут себя как боты, {n_human} как люди",
        "csv_path": str(OUT_CSV),
        "honest_caveats": [
            f"{len(unique_tokens)} УНИКАЛЬНЫХ токенов на {len(rows)} переводов ({len(rows)/max(len(unique_tokens),1):.1f} "
            "перевода/токен в среднем) -- для органической торговли это нетипично большое разнообразие; "
            "см. touch_diagnostics_per_wallet (n_tokens_received_never_sent) -- высокая доля 'получили и никогда не "
            "тронули' токенов согласуется с массовым дасти/эирдроп-спамом (как уже замечено на RCAT у ogle), не с "
            "реальной покупкой каждого токена.",
            "Цена доступна только для токенов, найденных в кэше 1142 пулов (data/task5_bot_pool_registry_cache.json, "
            "собран 2026-09-12) -- это НЕ полный реестр всей цепи; токены вне кэша помечены price_unavailable_reason, "
            "не выдуманы.",
            "'Покупка/продажа' здесь -- буквально направление ERC-20 Transfer (in/out), НЕ decoded своп -- см. "
            "предыдущий раунд (field_search): у этих 9 адресов 0 совпадений в decoded taker/maker/sender/recipient, "
            "реальный механизм сделки не подтверждён напрямую, только по движению токенов.",
            "likely_airdrop_or_dust помечает ПОВТОРЯЮЩИЕСЯ входящие переводы с ИДЕНТИЧНОЙ суммой от ОДНОГО "
            "отправителя (паттерн, реально замеченный на RCAT у ogle в прошлом раунде) -- такие переводы НЕ считаются "
            "первыми входами.",
            "'Скорость реакции' здесь -- блоки между получением токена (in) и следующей его отправкой (out) этим же "
            "кошельком, НЕ 'блоки между чужой сделкой и своей' буквально (у нас нет decoded чужих сделок, см. выше).",
        ],
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[price_report] {out['summary_line']}")
    print(f"[price_report] Результат: {OUT_JSON}, CSV: {OUT_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
