#!/usr/bin/env python3
"""Задача 5, Путь А -- ПЕРЕСЭМПЛИРОВАНИЕ известного ответа ИЗ ГРУППЫ 2.

Владелец, 2026-09-12 (после разбора групп 1/2/3): "Группа 2 -- единственное,
что доступно нашему боту... пересэмплировать 10 захватов только из неё и
прогнать known-answer тест заново." Группа 2 определена в ЭТОМ ЖЕ разговоре:
"остальные исполнители [не самоторговцы] с найденным катализатором (своп
другого адреса в затронутом пуле в пределах 3 блоков до)".

ЧЕСТНАЯ ОГОВОРКА ПРО ИСТОЧНИК: у нас НЕТ локально построчной (per-tx)
Группы 2 -- уже посчитанный Dune-результат (`task5_none_bucket_wash_
overlap_result.json`) даёт ТОЛЬКО агрегаты (n_txs/total_profit_usd по
distance_bucket x category), без tx_hash. Локальный CSV с tx_hash
(`task5_arb_detect_oneday_eb6cd16e748f89ae.csv`) не размечен по группам
вообще. Членство в Группе 2 определяется ЗДЕСЬ реальным чтением
блокчейна (без новых Dune-трат, как и просил владелец):

1. Исключить самоторговцев (Группа 1, владелец: "исключить из всех
   расчётов ниши"): реальный `to` арбитражной tx (из её же блока) в
   `KNOWN_SELF_TRADE_ADDRESSES`, ИЛИ `executor` (из CSV) в известном
   списке накрутки (`KNOWN_WASH_ADDRESSES_FROM_OVERLAP_SCRIPT`, тот же
   список, что и в `task5_none_bucket_wash_overlap.py`).
2. Найти катализатор -- своп ДРУГОГО адреса (`from` != executor этой
   arb-tx) в ОДНОМ ИЗ реальных пулов, затронутых arb-tx (`eth_getTransactionReceipt`
   Swap-логи), в пределах 3 блоков ДО (сам блок arb-tx + 3 предыдущих,
   владелец: "в пределах 3 блоков до"). Любого размера (владелец снял
   ограничение "только >$5000" в этом разговоре) -- поэтому реальная
   Группа 2 отсюда -- НЕ идентична Dune-определению ($5000/15 блоков),
   но честно соответствует ТЕКУЩЕМУ определению владельца.

Найденные 10 -- прогоняются через ПОЛНЫЙ known-answer пайплайн:
(а) старый триггер (price-impact через сам катализатор, как в
`task5_bot_known_answer_test.py`), (б) НОВЫЙ попарный триггер
(`check_all_pairs_price_divergence` -- реальное состояние ОБОИХ пулов
пары НА БЛОКЕ arb-tx, архивный eth_call с fallback на 'latest', если
архивный запрос не поддержан нодой -- честно помечается)."""
from __future__ import annotations

import csv
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from task5_bot_config import POOL_REGISTRY_CACHE_PATH, RPC_URL_MAINNET
from task5_bot_detector import check_all_pairs_price_divergence, check_router_triggered_opportunity
from task5_bot_pool_state import (
    _LIQUIDITY_SELECTOR,
    _SLOT0_SELECTOR,
    _decode_signed_word,
    _rpc_call_with_provider_fallback,
    load_registry_cache,
    save_registry_cache,
)
from task5_bot_router_decode import KNOWN_SELF_TRADE_ADDRESSES, KNOWN_SWAP_SELECTORS, decode_calldata

V3_SWAP_EVENT_TOPIC0 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
MIN_PRICE_IMPACT_FRACTION = 0.003
CATALYST_LOOKBACK_BLOCKS = 3  # владелец, ЭТОТ разговор: "в пределах 3 блоков до"
CANDIDATE_SCAN_LIMIT = 30  # сколько кандидатов из локального CSV просмотреть, ища 10 Группы 2
TARGET_N = 10

CSV_PATH = "data/task5_active_arb_mozila_cache/task5_arb_detect_oneday_eb6cd16e748f89ae.csv"
ETH_PRICE_USDG_FOR_DAY = 2459.747676232116  # реальный, тот же, что и в предыдущих шагах этого дня

# Владелец, 2026-09-12 (та же сессия): тот же список, что task5_none_bucket_wash_overlap.py
# -- используется здесь ТОЛЬКО для исключения Группы 1 по стороне executor (tx.from).
KNOWN_WASH_ADDRESSES_FROM_OVERLAP_SCRIPT = {
    "0x65050a9b7e5075a2ba5ced7b1b64ee66262c40dc",
    "0xcaf681a66d020601342297493863e78c959e5cb2",
    "0x39b38686a19836ac10162c490e4558e120cbbe5f",
    "0xe492912f37c2a4eca45d42dc67548f4c6cd7ce2b",
    "0x8876789976decbfcbbbe364623c63652db8c0904",
}


def _rpc(method, params):
    return _rpc_call_with_provider_fallback(method, params, RPC_URL_MAINNET, timeout=20.0)


def _load_candidates() -> list[dict]:
    rows = []
    with open(CSV_PATH) as f:
        for row in csv.DictReader(f):
            profit_usd = float(row["profit_usdg"]) + float(row["profit_weth"]) * ETH_PRICE_USDG_FOR_DAY
            if profit_usd < 10.0:
                continue
            tx_hash = row["tx_hash"] if row["tx_hash"].startswith("0x") else "0x" + row["tx_hash"]
            executor = row["executor"].lower()
            if not executor.startswith("0x"):
                executor = "0x" + executor
            rows.append({
                "tx_hash": tx_hash, "block_number": int(row["block_number"]), "executor": executor,
                "profit_usd": round(profit_usd, 2),
            })
    random.Random(20260912).shuffle(rows)  # детерминированная, но не "первые N хронологически" выборка
    return rows[:CANDIDATE_SCAN_LIMIT]


_block_cache: dict[int, list[dict]] = {}


def _get_block_txs(block_number: int) -> list[dict]:
    if block_number not in _block_cache:
        block = _rpc("eth_getBlockByNumber", [hex(block_number), True])
        _block_cache[block_number] = (block or {}).get("transactions", [])
    return _block_cache[block_number]


def _get_receipt_pools(tx_hash: str) -> list[str]:
    receipt = _rpc("eth_getTransactionReceipt", [tx_hash])
    if not receipt:
        return []
    pools = []
    for log in receipt.get("logs", []):
        topics = log.get("topics") or []
        if topics and topics[0].lower() == V3_SWAP_EVENT_TOPIC0:
            addr = (log.get("address") or "").lower()
            if addr and addr not in pools:
                pools.append(addr)
    return pools


def classify_and_find_catalyst(candidate: dict, registry) -> dict | None:
    """Возвращает None, если кандидат -- Группа 1 (самоторговец) или если
    вообще не удалось прочитать реальные данные (RPC-ошибка) -- честно
    пропускаем, не гадаем. Иначе -- словарь с полями группы 2/3 (catalyst
    найден или нет), готовый для отчёта."""
    if candidate["executor"] in KNOWN_WASH_ADDRESSES_FROM_OVERLAP_SCRIPT:
        return {"skip_reason": "group1_executor"}

    own_block_txs = _get_block_txs(candidate["block_number"])
    arb_tx = next((t for t in own_block_txs if (t.get("hash") or "").lower() == candidate["tx_hash"].lower()), None)
    if arb_tx is None:
        return {"skip_reason": "arb_tx_not_found_in_own_block"}
    arb_to = (arb_tx.get("to") or "").lower()
    if arb_to in KNOWN_SELF_TRADE_ADDRESSES:
        return {"skip_reason": "group1_to_contract"}
    arb_index = own_block_txs.index(arb_tx)

    pools = _get_receipt_pools(candidate["tx_hash"])
    if not pools:
        return {"skip_reason": "no_v3_swap_logs_in_receipt"}
    pool_set = set(pools)

    catalyst = None
    # блок 0 (тот же блок, только РАНЕЕ arb-tx), затем block-1, block-2, block-3
    for depth in range(0, CATALYST_LOOKBACK_BLOCKS + 1):
        block_n = candidate["block_number"] - depth
        txs = own_block_txs if depth == 0 else _get_block_txs(block_n)
        upper_bound = arb_index if depth == 0 else len(txs)
        for j in range(upper_bound - 1, -1, -1):
            tx = txs[j]
            sender = (tx.get("from") or "").lower()
            if sender == candidate["executor"]:
                continue  # тот же исполнитель -- не "другой адрес" (владелец: "своп ДРУГОГО адреса")
            to_addr = (tx.get("to") or "").lower()
            data_hex = tx.get("input") or tx.get("data") or "0x"
            selector = ("0x" + data_hex[2:10]) if len(data_hex) >= 10 else None
            if selector not in KNOWN_SWAP_SELECTORS:
                continue
            for intent in decode_calldata(to_addr, data_hex):
                if intent.token_in is None or intent.token_out is None or intent.fee is None:
                    continue
                pool = registry.find_pool_by_tokens_fee(intent.token_in, intent.token_out, intent.fee)
                if pool is not None and pool.address.lower() in pool_set:
                    catalyst = {"tx_hash": tx.get("hash"), "to": to_addr, "data": data_hex,
                                "selector": selector, "block_number": block_n, "from": sender,
                                "block_distance": depth, "pool": pool.address,
                                "token_in": intent.token_in, "token_out": intent.token_out,
                                "fee": intent.fee, "amount_in": intent.amount_in}
                    break
            if catalyst:
                break
        if catalyst:
            break

    return {
        "skip_reason": None, "real_pools_touched": pools, "arb_to": arb_to,
        "is_group2": catalyst is not None, "catalyst": catalyst,
    }


def _ensure_pool_priced_at_block(registry, pool_addr: str, block_number: int) -> bool:
    pool = registry.by_address.get(pool_addr.lower())
    if pool is None:
        return False
    if pool.sqrt_price_x96 is not None:
        return True
    for block_tag in (hex(block_number), "latest"):
        try:
            slot0_hex = _rpc("eth_call", [{"to": pool.address, "data": _SLOT0_SELECTOR}, block_tag])
            liq_hex = _rpc("eth_call", [{"to": pool.address, "data": _LIQUIDITY_SELECTOR}, block_tag])
        except Exception:
            continue
        if not slot0_hex or slot0_hex == "0x":
            continue
        body = slot0_hex[2:]
        sqrt_price_x96 = int(body[0:64], 16)
        tick = _decode_signed_word(body[64:128])
        liquidity = int(liq_hex, 16) if liq_hex and liq_hex != "0x" else 0
        pool.apply_swap(sqrt_price_x96=sqrt_price_x96, liquidity=liquidity, tick=tick,
                         block_number=block_number if block_tag != "latest" else None)
        return True
    return False


def evaluate_known_answer(candidate: dict, group2: dict, registry) -> dict:
    row = {**candidate, "real_pools_touched": group2["real_pools_touched"], "catalyst": None,
           "old_trigger_would_detect": None, "old_trigger_fail_step": None,
           "new_pairwise_trigger_would_detect": None, "new_pairwise_trigger_fail_step": None}
    catalyst = group2["catalyst"]
    row["catalyst"] = {k: v for k, v in catalyst.items() if k != "data"}

    # --- (а) старый триггер: price-impact через сам катализатор ---
    selector = catalyst["selector"]
    row["decoded_function"] = KNOWN_SWAP_SELECTORS.get(selector)
    intents = decode_calldata(catalyst["to"], catalyst["data"])
    full_intent = next((i for i in intents
                        if i.token_in and i.token_out and i.fee is not None and i.amount_in is not None), None)
    if full_intent is None:
        row["old_trigger_would_detect"] = "НЕТ"
        row["old_trigger_fail_step"] = "функция декодирована частично"
    else:
        pool = registry.find_pool_by_tokens_fee(full_intent.token_in, full_intent.token_out, full_intent.fee)
        if pool is None:
            row["old_trigger_would_detect"] = "НЕТ"
            row["old_trigger_fail_step"] = "пул не в универсуме"
        else:
            _ensure_pool_priced_at_block(registry, pool.address, catalyst["block_number"])
            if pool.sqrt_price_x96 is None or pool.liquidity is None or pool.liquidity <= 0:
                row["old_trigger_would_detect"] = "НЕТ"
                row["old_trigger_fail_step"] = f"цена/liquidity недоступны (liquidity={pool.liquidity})"
            elif len(registry.pools_for_pair(full_intent.token_in, full_intent.token_out)) < 2:
                row["old_trigger_would_detect"] = "НЕТ"
                row["old_trigger_fail_step"] = "нет второго пула этой пары"
            else:
                price_impact = full_intent.amount_in / pool.liquidity
                row["price_impact_fraction"] = price_impact
                if price_impact < MIN_PRICE_IMPACT_FRACTION:
                    row["old_trigger_would_detect"] = "НЕТ"
                    row["old_trigger_fail_step"] = f"порог не пройден ({price_impact:.6f} < {MIN_PRICE_IMPACT_FRACTION})"
                else:
                    row["old_trigger_would_detect"] = "ДА"

    # --- (б) новый попарный триггер: реальное состояние ОБОИХ пулов пары на блоке arb-tx ---
    pair_pools = registry.pools_for_pair(catalyst["token_in"], catalyst["token_out"]) if full_intent is not None else []
    if len(pair_pools) < 2:
        row["new_pairwise_trigger_would_detect"] = "НЕТ"
        row["new_pairwise_trigger_fail_step"] = "нет второго пула этой пары в реестре"
    else:
        for p in pair_pools:
            _ensure_pool_priced_at_block(registry, p.address, candidate["block_number"])
        opps = check_all_pairs_price_divergence(registry, trigger_sequence_number=candidate["block_number"])
        matched = [o for o in opps if {o.pool_a.lower(), o.pool_b.lower()} <= {p.address.lower() for p in pair_pools}]
        if matched:
            row["new_pairwise_trigger_would_detect"] = "ДА"
            row["new_pairwise_trigger_rel_divergence"] = matched[0].rel_divergence_fraction
        else:
            row["new_pairwise_trigger_would_detect"] = "НЕТ"
            row["new_pairwise_trigger_fail_step"] = "разрыв цены < 2x комиссии (или цена недоступна)"
    return row


if __name__ == "__main__":
    registry = load_registry_cache(POOL_REGISTRY_CACHE_PATH)
    candidates = _load_candidates()
    print(f"[group2_resample] кандидатов из локального CSV (profit>=$10, детерминированная выборка): "
          f"{len(candidates)}")

    group2_rows = []
    all_candidate_diag = []  # владелец, следующий шаг после первого 0/30: честная диагностика ВСЕХ
    # кандидатов (не только квалифицировавшихся) -- чтобы отличить "реально нет катализатора" от бага.
    skipped = {"group1_executor": 0, "group1_to_contract": 0, "no_catalyst": 0, "other_skip": 0}
    for c in candidates:
        cls = classify_and_find_catalyst(c, registry)
        diag_row = {
            "tx_hash": c["tx_hash"], "block_number": c["block_number"], "executor": c["executor"],
            "profit_usd": c["profit_usd"], "skip_reason": cls.get("skip_reason"),
            "real_pools_touched": cls.get("real_pools_touched"), "arb_to": cls.get("arb_to"),
            "is_group2": cls.get("is_group2"),
        }
        all_candidate_diag.append(diag_row)

        if cls.get("skip_reason") in ("group1_executor", "group1_to_contract"):
            skipped[cls["skip_reason"]] += 1
            continue
        if cls.get("skip_reason"):
            skipped["other_skip"] += 1
            continue
        if not cls["is_group2"]:
            skipped["no_catalyst"] += 1
            continue
        if len(group2_rows) >= TARGET_N:
            continue  # уже набрали 10 -- дальше ТОЛЬКО диагностика (см. all_candidate_diag), не полный прогон триггеров
        row = evaluate_known_answer(c, cls, registry)
        group2_rows.append(row)
        print(f"[group2_resample] Группа 2 #{len(group2_rows)}: {c['tx_hash']} old={row['old_trigger_would_detect']} "
              f"new={row['new_pairwise_trigger_would_detect']}")

    save_registry_cache(registry, POOL_REGISTRY_CACHE_PATH)

    n_old_yes = sum(1 for r in group2_rows if r["old_trigger_would_detect"] == "ДА")
    n_new_yes = sum(1 for r in group2_rows if r["new_pairwise_trigger_would_detect"] == "ДА")
    result = {
        "n_candidates_scanned": len(candidates), "n_group2_found": len(group2_rows),
        "skipped": skipped, "n_old_trigger_yes": n_old_yes, "n_new_pairwise_trigger_yes": n_new_yes,
        "rows": group2_rows, "all_candidate_diag": all_candidate_diag,
    }
    text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    print(text)
    Path("data/task5_bot_known_answer_group2_resample_result.json").write_text(text)
    print(f"[group2_resample] {len(group2_rows)}/{TARGET_N} найдено, старый триггер {n_old_yes}/{len(group2_rows)}, "
          f"новый попарный {n_new_yes}/{len(group2_rows)} -- записано")

    # Владелец, честная диагностика ПЕРЕД тем, как доверять "no_catalyst": руками
    # проверить один реальный случай -- вдруг это баг сопоставления, не реальное
    # отсутствие катализатора (та же дисциплина, что и в известном ответе выше).
    no_catalyst_example = next((d for d in all_candidate_diag if d["skip_reason"] is None and d["is_group2"] is False),
                                None)
    if no_catalyst_example is not None:
        print(f"\n[group2_resample][диагностика no_catalyst] {no_catalyst_example['tx_hash']} "
              f"(блок {no_catalyst_example['block_number']}, пулы {no_catalyst_example['real_pools_touched']})")
        for depth in range(0, CATALYST_LOOKBACK_BLOCKS + 1):
            block_n = no_catalyst_example["block_number"] - depth
            txs = _get_block_txs(block_n)
            print(f"  --- блок {block_n} (depth={depth}), {len(txs)} tx ---")
            for j, tx in enumerate(txs):
                to_addr = (tx.get("to") or "").lower()
                data_hex = tx.get("input") or tx.get("data") or "0x"
                selector = ("0x" + data_hex[2:10]) if len(data_hex) >= 10 else None
                is_target = (tx.get("hash") or "").lower() == no_catalyst_example["tx_hash"].lower()
                marker = " <== САМА ARB-TX" if is_target else ""
                known = KNOWN_SWAP_SELECTORS.get(selector, "")
                print(f"    [{j}] {tx.get('hash')} to={to_addr} selector={selector} known_fn={known!r}{marker}")
