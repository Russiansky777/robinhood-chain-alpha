#!/usr/bin/env python3
"""Задача 5, Путь А -- проверка ПРОТИВ ИЗВЕСТНОГО ОТВЕТА (владелец,
2026-09-12, "меняем метод проверки"): "Не 'прогнать и посмотреть, что
видит', а 'проверить против известного ответа'. Взять 10 реальных
прибыльных захватов >= $10 из данных Задачи 5 ... Для каждого: найти
катализатор, прогнать через наш декодер и текущий триггер, ответ --
увидел бы бот да/нет, если нет -- на каком шаге отвалился."

ЧЕСТНАЯ ОГОВОРКА ПРО ИСТОЧНИК 10 ЗАХВАТОВ: в уже существующих результатах
измерений Задачи 5 (`data/task5_active_arb_mozila_cache/task5_arb_detect_
oneday_eb6cd16e748f89ae.csv`, реальный Dune-запрос, 2026-09-11) есть
tx_hash/block_number/executor/profit_usdg/profit_weth -- РЕАЛЬНЫЕ данные,
НЕ пул и НЕ катализатор (эти измерения считали ТОЛЬКО агрегированные
distance_bucket-корзины, см. `task5_latency_signature.py` -- ни один
файл в репозитории не хранит катализатор построчно). 10 строк ниже взяты
ИЗ ЭТОГО РЕАЛЬНОГО CSV (профит >= $10, равномерно по времени первых ~29
минут покрытого окна -- честно: сам CSV покрывает не полный день, а
именно 00:00-00:29 UTC 2026-09-10, это реальный лимит строк исходного
Dune-запроса, не мой выбор). Пул и катализатор для каждой строки --
находятся ЗДЕСЬ, реальным RPC-чтением (`eth_getTransactionReceipt` --
Swap-логи самой arb-транзакции = реальный пул(ы); `eth_getBlockByNumber`
её собственного блока с транзакциями -- поиск катализатора СРЕДИ БОЛЕЕ
РАННИХ транзакций ТОГО ЖЕ БЛОКА, decode_calldata + сверка на тот же
пул). Это РЕАЛЬНЫЕ, не выдуманные данные -- но честно ограниченные
рамками "тот же блок" (distance=0) из экономии на числе RPC-запросов
(владелец: "1 запрос на захват") -- если катализатор был в БОЛЕЕ РАННЕМ
блоке (distance>=1, тоже реальная, но менее многочисленная корзина в
предыдущих измерениях), эта проверка честно покажет "катализатор не
найден в том же блоке", не будет расширять окно поиска без отдельного
разрешения (это раздувает число RPC-запросов кратно)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from task5_bot_config import POOL_REGISTRY_CACHE_PATH, RPC_URL_MAINNET
from task5_bot_pool_state import (
    _LIQUIDITY_SELECTOR,
    _SLOT0_SELECTOR,
    _decode_signed_word,
    _rpc_call_with_provider_fallback,
    load_registry_cache,
    save_registry_cache,
)
from task5_bot_router_decode import KNOWN_SWAP_SELECTORS, decode_calldata

V3_SWAP_EVENT_TOPIC0 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
MIN_PRICE_IMPACT_FRACTION = 0.003  # тот же порог, что PATH_A_MIN_PRICE_IMPACT_FRACTION

# 10 РЕАЛЬНЫХ строк из task5_arb_detect_oneday_eb6cd16e748f89ae.csv (profit_usd_total
# = profit_usdg + profit_weth*eth_price[2459.747676232116], >= $10, равномерно по
# времени первых 20000 строк файла -- см. докстрин выше).
KNOWN_PROFITABLE_CAPTURES = [
    {"tx_hash": "0x7AF12717684AEA0384AD50B9F99270853132B30BA786325F88B590CD64321F82",
     "block_number": 58958203, "executor": "0x47D74784799FBD7BA6C27A82A144B8EEE4911891", "profit_usd": 87.07},
    {"tx_hash": "0x4FBE63CEA101B633A94F904187E553C0D6EE3AB03A005135FDCBC4BC88F40270",
     "block_number": 58961176, "executor": "0x4337023CCE7419F20DB4057B4DC3E2EA0C2151E0", "profit_usd": 949.84},
    {"tx_hash": "0xEC261CC6232D96165784A1A3A4387ADA6DD6BEE703C91D686686226B07576A55",
     "block_number": 58963905, "executor": "0xD71212990D2BE6828E8992171A4BA05DBC439DAE", "profit_usd": 72.36},
    {"tx_hash": "0x64EA87A522046007EAAA2D907A17A0772239A88A5E8CA2F86FD09CE481D4341D",
     "block_number": 58965440, "executor": "0x3A8AE44D0550174A5DA5775AF70D605F6BEEBEC9", "profit_usd": 435.39},
    {"tx_hash": "0x98C476BE0B6D6BF533E8980FBEA8D514D6F264FBCD3F69A20D0629F22554FAE0",
     "block_number": 58967126, "executor": "0x3E9FA24EA146E54AF5A7ED404FA15BF8D762F4E1", "profit_usd": 20.40},
    {"tx_hash": "0x6889940EE34AA61AA82DAAF0F110DD782902331765646338F09181CDE7AD95B4",
     "block_number": 58968391, "executor": "0x94DC4EF2A6B57F2798435CC5F3F2561BE8437CCE", "profit_usd": 63.02},
    {"tx_hash": "0x7C7ADF90FCBA7AF2233A457F10A3EACCECE741AD9D0AA9F225DA18E36DB6837C",
     "block_number": 58969538, "executor": "0x2C022DA442FC8AB0573BD79B4C365EC7D7D44B0D", "profit_usd": 21.73},
    {"tx_hash": "0xA1249A9801A444ED47EF0879509E5DA92F1145F9C2E3C347B26F5E0C22B5B80B",
     "block_number": 58970722, "executor": "0xCA1F2540F7EF87882D00052F0E0B7E201D8C4D18", "profit_usd": 26.01},
    {"tx_hash": "0x7C085AF1C9A3BBD1E25025DEA1021D1F87CA59597D0C41E65917291590DE83D5",
     "block_number": 58972376, "executor": "0x36CE718E4DC6EFB77E5F37F03F85B1E0D125219B", "profit_usd": 49.63},
    {"tx_hash": "0xFE3B006FA57E4C47E179789727CABECD3B901849D262E1233E5ECBD88E36B67F",
     "block_number": 58974572, "executor": "0x9FE8296FDC267E1229A96C51BE8CC86B4517FF3A", "profit_usd": 439.60},
]


def _rpc(method: str, params: list) -> dict | list | None:
    return _rpc_call_with_provider_fallback(method, params, RPC_URL_MAINNET, timeout=20.0)


def _get_receipt_pools(tx_hash: str) -> tuple[list[str], str | None]:
    try:
        receipt = _rpc("eth_getTransactionReceipt", [tx_hash])
    except Exception as exc:
        return [], f"eth_getTransactionReceipt error: {exc}"
    if not receipt:
        return [], "receipt not found (None)"
    pools = []
    for log in receipt.get("logs", []):
        topics = log.get("topics") or []
        if topics and topics[0].lower() == V3_SWAP_EVENT_TOPIC0:
            addr = log.get("address")
            if addr and addr.lower() not in [p.lower() for p in pools]:
                pools.append(addr.lower())
    return pools, None


def _get_block_txs(block_number: int) -> tuple[list[dict], str | None]:
    try:
        block = _rpc("eth_getBlockByNumber", [hex(block_number), True])
    except Exception as exc:
        return [], f"eth_getBlockByNumber error: {exc}"
    if not block:
        return [], "block not found (None)"
    return block.get("transactions", []), None


def analyze_one(capture: dict, registry) -> dict:
    tx_hash = capture["tx_hash"]
    block_number = capture["block_number"]
    row = {
        "tx_hash": tx_hash, "block_number": block_number, "executor": capture["executor"],
        "profit_usd": capture["profit_usd"],
    }

    pools, err = _get_receipt_pools(tx_hash)
    row["real_pools_touched"] = pools
    if err:
        row["error"] = err
        row["would_detect"] = "НЕИЗВЕСТНО (RPC-ошибка при чтении рецепта)"
        row["fail_step"] = "rpc_error_receipt"
        return row
    if not pools:
        row["would_detect"] = "НЕИЗВЕСТНО (ни одного V3 Swap-лога в рецепте -- возможно v4/другая архитектура)"
        row["fail_step"] = "no_v3_swap_logs_in_receipt"
        return row

    block_txs, err2 = _get_block_txs(block_number)
    if err2:
        row["error"] = err2
        row["would_detect"] = "НЕИЗВЕСТНО (RPC-ошибка при чтении блока)"
        row["fail_step"] = "rpc_error_block"
        return row

    arb_index = None
    for i, tx in enumerate(block_txs):
        if tx.get("hash", "").lower() == tx_hash.lower():
            arb_index = i
            break
    if arb_index is None:
        row["would_detect"] = "НЕИЗВЕСТНО (сама arb-транзакция не найдена в собственном блоке -- расхождение block_number)"
        row["fail_step"] = "arb_tx_not_in_own_block"
        return row

    # --- Найти катализатор: более ранняя tx того же блока, decode_calldata которой
    # даёт пул, совпадающий с одним из real_pools_touched. Берём БЛИЖАЙШУЮ (последнюю
    # по индексу) такую tx -- "своп, после которого он случился". ---
    catalyst = None
    pool_set = set(pools)
    for j in range(arb_index - 1, -1, -1):
        tx = block_txs[j]
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
                            "selector": selector, "block_index": j, "pool": pool.address,
                            "token_in": intent.token_in, "token_out": intent.token_out,
                            "fee": intent.fee, "amount_in": intent.amount_in}
                break
        if catalyst:
            break

    if catalyst is None:
        row["catalyst"] = None
        row["would_detect"] = ("НЕТ (катализатор не найден в том же блоке -- либо в более раннем блоке, "
                                "либо это сам arb-бот вошёл без внешнего провоцирующего свопа)")
        row["fail_step"] = "catalyst_not_found_same_block"
        return row

    row["catalyst"] = {k: v for k, v in catalyst.items() if k != "data"}
    row["catalyst_data_len_bytes"] = (len(catalyst["data"]) - 2) // 2

    # --- Шаг 2: прогнать КАТАЛИЗАТОР через наш реальный декодер + текущий триггер ---
    selector = catalyst["selector"]
    if selector not in KNOWN_SWAP_SELECTORS:
        row["would_detect"] = "НЕТ"
        row["fail_step"] = "селектор функции не распознан нашим декодером"
        return row
    row["decoded_function"] = KNOWN_SWAP_SELECTORS.get(selector)

    intents = decode_calldata(catalyst["to"], catalyst["data"])
    full_intent = next((i for i in intents
                        if i.token_in and i.token_out and i.fee is not None and i.amount_in is not None), None)
    if full_intent is None:
        row["would_detect"] = "НЕТ"
        row["fail_step"] = "функция декодирована частично (не все поля intent) -- см. decode_calldata"
        return row

    pool = registry.find_pool_by_tokens_fee(full_intent.token_in, full_intent.token_out, full_intent.fee)
    if pool is None:
        row["would_detect"] = "НЕТ"
        row["fail_step"] = "пул не в универсуме (find_pool_by_tokens_fee вернул None)"
        return row
    row["mapped_pool"] = pool.address

    if pool.sqrt_price_x96 is None or pool.liquidity is None:
        row["would_detect"] = "НЕТ"
        row["fail_step"] = "цена пула недоступна (sqrt_price_x96/liquidity ещё не залиты в реестр)"
        return row
    if pool.liquidity <= 0:
        row["would_detect"] = "НЕТ"
        row["fail_step"] = f"liquidity<=0 ({pool.liquidity}) -- price-impact неопределим"
        return row

    if len(registry.pools_for_pair(full_intent.token_in, full_intent.token_out)) < 2:
        row["would_detect"] = "НЕТ"
        row["fail_step"] = "нет второго пула этой пары в реестре (pools_for_pair < 2)"
        return row

    price_impact = full_intent.amount_in / pool.liquidity
    row["price_impact_fraction"] = price_impact
    if price_impact < MIN_PRICE_IMPACT_FRACTION:
        row["would_detect"] = "НЕТ"
        row["fail_step"] = f"порог не пройден (price_impact={price_impact:.6f} < {MIN_PRICE_IMPACT_FRACTION})"
        return row

    row["would_detect"] = "ДА"
    row["fail_step"] = None

    # --- Кросс-проверка: реальный вызов текущего триггера (не ручное повторение
    # тех же шагов) -- если он расходится с ручным выводом выше, это баг в
    # ЭТОМ тестовом скрипте, честно фиксируем, не тихо доверяем одному пути. ---
    from task5_bot_detector import check_router_triggered_opportunity
    opp = check_router_triggered_opportunity(
        registry, pool, full_intent.token_in, full_intent.token_out, full_intent.amount_in,
        trigger_sequence_number=block_number, min_price_impact_fraction=MIN_PRICE_IMPACT_FRACTION,
        assumed_gas_cost_usd=0.3, exit_token=full_intent.token_out,
        router_to=catalyst["to"], router_function_label=row.get("decoded_function"),
    )
    row["cross_check_real_trigger_fired"] = opp is not None
    if (opp is not None) != (row["would_detect"] == "ДА"):
        row["cross_check_mismatch"] = True
    return row


def _ensure_pool_priced(registry, pool_addr: str, block_number: int | None) -> None:
    """Ленивая заливка цены (та же логика, что боевой lazy_pricing) для КОНКРЕТНОГО
    пула, который встретился в анализе одной строки -- НЕ вся вселенная, честно
    точечный запрос slot0()/liquidity(), по возможности НА ИСТОРИЧЕСКОМ блоке
    капчура (не 'latest') -- ближе к реальному состоянию в момент события. Если
    архивный запрос не поддерживается нодой -- честный fallback на 'latest' с
    пометкой в поле pool.last_update_block (не подменяем молча)."""
    pool = registry.by_address.get(pool_addr.lower())
    if pool is None or pool.sqrt_price_x96 is not None:
        return
    block_tag = hex(block_number) if block_number else "latest"
    try:
        slot0_hex = _rpc("eth_call", [{"to": pool.address, "data": _SLOT0_SELECTOR}, block_tag])
        liq_hex = _rpc("eth_call", [{"to": pool.address, "data": _LIQUIDITY_SELECTOR}, block_tag])
    except Exception:
        block_tag = "latest"
        try:
            slot0_hex = _rpc("eth_call", [{"to": pool.address, "data": _SLOT0_SELECTOR}, block_tag])
            liq_hex = _rpc("eth_call", [{"to": pool.address, "data": _LIQUIDITY_SELECTOR}, block_tag])
        except Exception:
            return
    if not slot0_hex or slot0_hex == "0x":
        return
    body = slot0_hex[2:]
    sqrt_price_x96 = int(body[0:64], 16)
    tick = _decode_signed_word(body[64:128])
    liquidity = int(liq_hex, 16) if liq_hex and liq_hex != "0x" else 0
    pool.apply_swap(sqrt_price_x96=sqrt_price_x96, liquidity=liquidity, tick=tick,
                     block_number=block_number if block_tag != "latest" else None)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", type=str, default=POOL_REGISTRY_CACHE_PATH)
    ap.add_argument("--out", type=str, default="data/task5_bot_known_answer_test_result.json")
    args = ap.parse_args()

    if not Path(args.cache).exists():
        print(f"[known_answer] ЧЕСТНО: кеш реестра {args.cache} не найден.", file=sys.stderr)
        raise SystemExit(1)
    registry = load_registry_cache(args.cache)

    rows = []
    for capture in KNOWN_PROFITABLE_CAPTURES:
        print(f"[known_answer] анализ {capture['tx_hash']} (блок {capture['block_number']})...")
        row = analyze_one(capture, registry)
        # если катализатор нашёлся и его пул не оценен -- дозаливаем ТОЛЬКО его,
        # на историческом блоке капчура, затем пересчитываем шаги цены/порога заново
        if row.get("mapped_pool") and row.get("fail_step", "").startswith("цена пула недоступна"):
            _ensure_pool_priced(registry, row["mapped_pool"], capture["block_number"])
            row = analyze_one(capture, registry)
        rows.append(row)
        print(f"    -> would_detect={row['would_detect']!r} fail_step={row.get('fail_step')!r}")

    save_registry_cache(registry, args.cache)

    n_yes = sum(1 for r in rows if r["would_detect"] == "ДА")
    result = {
        "n_captures": len(rows), "n_would_detect_yes": n_yes,
        "min_price_impact_fraction": MIN_PRICE_IMPACT_FRACTION,
        "rows": rows,
    }
    text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    print(text)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(text)
    print(f"[known_answer] {n_yes}/{len(rows)} -- результат записан в {args.out}")
