#!/usr/bin/env python3
"""Задача 5, десятый раунд (правка владельца): "Останови выводы об
отсутствии прибыли. В full_fund_flow_check() обнаружена инверсия знаков --
ты вычитаешь исходные V4 amount0/amount1."

Реальная проверка на tx 0xde38133b2cdf7f3b6307225fce0fad6c0914796a48290dcb5083fe51abdbdf71
(владелец): USDG-дельты -120795955 и +134012759 дают +13216804 raw при
ПРЯМОМ суммировании (БЕЗ унарного минуса) -- это совпадает с реальным
Transfer получателю прибыли. Старый код (`net_trader_flow[c] -=
decoded["amountN"]`) даёт -13216804 -- инверсия знака, подтверждённая
конкретным числом, не гипотеза.

Это ТОЛЬКО правка аналитического скрипта (пересчёт уже сохранённых данных
task5_v4_item3_in_window_control_trade_result.json) -- НИ ОДНОГО нового
RPC-вызова, НИКАКОГО нового скана, НИКАКОЙ форк-реконструкции здесь.
Торговая математика бота (task5_v4_hotpath.py, контракт) НЕ тронута --
владелец явно просил не менять её автоматически.

Шаги (ровно как просил владелец):
  1) пересчитать знаки для 120 кандидатов с ОДНОЙ ненулевой валютой
     (сама структура "какие токены ненулевые" НЕ меняется от знака --
     меняется только ЗНАК того единственного ненулевого значения);
  2) найти ПЕРВОГО (по номеру блока) положительного ПОСЛЕ исправления,
     который НЕ помечен как "неполное покрытие анализатором" (смешанный
     V3/V4 маршрут или WETH<->ETH -- см. п.3), и сверить его Transfer-логи
     (уже сохранены в самом результате -- НЕ новый RPC) с исполнителем и
     получателем прибыли, с учётом возможной комиссии хука;
  3) смешанные V3/V4 и WETH<->ETH -- отдельная категория "неполное
     покрытие анализатором", не "убыток" и не "обычный обмен";
  4) прислать ТОЛЬКО этот один пример -- форк-реконструкция здесь НЕ
     запускается, ждёт отдельного подтверждения."""
import json
from pathlib import Path

RESULT_FILE = Path(__file__).parent.parent / "data" / "task5_v4_item3_in_window_control_trade_result.json"

NATIVE = "0x0000000000000000000000000000000000000000"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USER_CITED_TX = "0xde38133b2cdf7f3b6307225fce0fad6c0914796a48290dcb5083fe51abdbdf71"


def recompute_corrected_net_flow(legs: list[dict]) -> tuple[dict[str, int], bool]:
    """ИСПРАВЛЕННЫЙ пересчёт: ПРЯМОЕ суммирование decoded amount0/amount1 по
    валюте (БЕЗ унарного минуса, который был в оригинальном коде). Легко
    свести к оригиналу: net_corrected[t] == -net_original[t] для КАЖДОГО t
    -- чистая глобальная инверсия знака, структура (какие токены нулевые)
    не меняется. Возвращает (net, incomplete) -- incomplete=True, если
    хотя бы одно плечо не декодировано (Initialize не найден -- пул мог
    быть V3, не V4, или иная причина, честно зафиксированная в исходном
    прогоне)."""
    net: dict[str, int] = {}
    incomplete = False
    for leg in legs:
        if leg.get("currency0") is None:
            incomplete = True
            continue
        c0, c1 = leg["currency0"], leg["currency1"]
        net[c0] = net.get(c0, 0) + leg["amount0"]
        net[c1] = net.get(c1, 0) + leg["amount1"]
    return net, incomplete


def detect_weth_eth_mix(row: dict, corrected_net: dict[str, int]) -> bool:
    """Смешанный WETH<->ETH: если в ПОЛНОМ списке ERC20-Transfer этого
    рецепта фигурирует WETH, а исправленный net-flow при этом либо вообще
    не включает WETH как валюту плеча (V4 Swap решил только часть маршрута,
    обёртку/разворачивание WETH<->ETH решил отдельный вызов вне
    PoolManager), либо содержит NATIVE ETH как отдельную "плечевую" валюту
    -- оба признака означают, что чистого V4 Swap-разбора НЕДОСТАТОЧНО для
    полной картины этого маршрута."""
    transfers = row.get("all_transfers_in_receipt") or []
    weth_touched = any(t.get("token", "").lower() == WETH.lower() for t in transfers)
    if not weth_touched:
        return False
    # WETH участвует в Transfer-логах, но НЕ фигурирует как валюта ни
    # одного декодированного V4-плеча -- обёртка/разворот вне зоны охвата
    # чистого Swap-разбора.
    return WETH.lower() not in corrected_net


def main() -> None:
    d = json.loads(RESULT_FILE.read_text())
    ffc = d.get("fund_flow_checks") or []

    # --- Шаг 1: пересчёт знаков для 120 кандидатов с ОДНОЙ исходной
    # ненулевой валютой (по уже сохранённому "nonzero_net_flow_tokens") ---
    single_token_rows = [row for row in ffc if len(row.get("nonzero_net_flow_tokens") or {}) == 1]

    recomputed = []
    for row in single_token_rows:
        legs = row.get("legs") or []
        corrected_net, incomplete = recompute_corrected_net_flow(legs)
        nz_corrected = {t: v for t, v in corrected_net.items() if v != 0}
        weth_eth_mix = detect_weth_eth_mix(row, corrected_net)
        is_v3_v4_mixed_or_weth = incomplete or weth_eth_mix
        entry = {
            "tx_hash": row["tx_hash"], "block": row["block"],
            "original_nonzero_net_flow_tokens": row.get("nonzero_net_flow_tokens"),
            "corrected_nonzero_net_flow_tokens": nz_corrected,
            "incomplete_analyzer_coverage": is_v3_v4_mixed_or_weth,
            "incomplete_reason": (
                "Initialize не найден хотя бы для одного плеча (возможно, не-V4/непокрытый пул)" if incomplete
                else ("WETH участвует в Transfer, но не в декодированном V4-плече (обёртка/разворот ETH<->WETH "
                      "вне охвата чистого Swap-разбора)" if weth_eth_mix else None)
            ),
        }
        recomputed.append(entry)

    n_positive = sum(1 for e in recomputed if len(e["corrected_nonzero_net_flow_tokens"]) == 1
                      and next(iter(e["corrected_nonzero_net_flow_tokens"].values())) > 0
                      and not e["incomplete_analyzer_coverage"])
    n_negative = sum(1 for e in recomputed if len(e["corrected_nonzero_net_flow_tokens"]) == 1
                      and next(iter(e["corrected_nonzero_net_flow_tokens"].values())) < 0
                      and not e["incomplete_analyzer_coverage"])
    n_incomplete = sum(1 for e in recomputed if e["incomplete_analyzer_coverage"])

    step1_summary = {
        "n_single_token_candidates_total": len(recomputed),
        "n_positive_after_signfix": n_positive,
        "n_negative_after_signfix": n_negative,
        "n_incomplete_analyzer_coverage_v3_or_weth": n_incomplete,
    }

    # --- Шаг 2: первый (по блоку) положительный ПОСЛЕ исправления,
    # НЕ помеченный как неполное покрытие ---
    candidates_sorted = sorted(recomputed, key=lambda e: e["block"])
    first_positive = None
    for e in candidates_sorted:
        nz = e["corrected_nonzero_net_flow_tokens"]
        if e["incomplete_analyzer_coverage"]:
            continue
        if len(nz) == 1 and next(iter(nz.values())) > 0:
            first_positive = e
            break

    result = {"step1_signfix_summary": step1_summary, "recomputed_120": recomputed}

    if first_positive is None:
        result["step2_first_positive"] = None
        result["note"] = "после исправления знака положительных кандидатов (вне 'неполного покрытия') не найдено"
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        return

    tx_hash = first_positive["tx_hash"]
    row = next(r for r in ffc if r["tx_hash"] == tx_hash)
    legs = row.get("legs") or []
    token, corrected_value = next(iter(first_positive["corrected_nonzero_net_flow_tokens"].items()))

    # --- Сверка с Transfer-логами (уже сохранены в самом рецепте -- НЕ
    # новый RPC): исполнитель (tx_to, если контракт, иначе tx_from) и
    # получатель профита (positive_net_transfer_addresses, уже посчитан в
    # исходном прогоне -- честный net по КАЖДОМУ адресу И токену). ---
    all_transfers = row.get("all_transfers_in_receipt") or []
    token_transfers = [t for t in all_transfers if t.get("token", "").lower() == token.lower()]
    positive_addrs_for_token = {
        addr: toks[token] for addr, toks in (row.get("positive_net_transfer_addresses") or {}).items()
        if token in toks
    }
    executor_addr = row.get("executor_address_checked")
    other_token_spend = row.get("other_token_spend_by_executor")

    # Возможная комиссия хука: любой адрес хука из декодированных плеч,
    # который получил Transfer ЭТОГО ЖЕ токена -- показываем явно, не
    # молчим (нужно для "фактическая выплата" vs "исправленная сумма").
    hook_addrs = {leg.get("hooks") for leg in legs if leg.get("hooks") and leg.get("hooks") != NATIVE}
    hook_fee_transfers = [t for t in token_transfers if t.get("to", "").lower() in {h.lower() for h in hook_addrs}]

    step2 = {
        "tx_hash": tx_hash,
        "block": row["block"],
        "matches_user_cited_tx": tx_hash.lower() == USER_CITED_TX.lower(),
        "profit_token": token,
        "legs_raw_deltas": [
            {"pool_id": leg.get("pool_id"), "currency0": leg.get("currency0"), "currency1": leg.get("currency1"),
             "amount0_raw": leg.get("amount0"), "amount1_raw": leg.get("amount1"), "hooks": leg.get("hooks")}
            for leg in legs
        ],
        "original_buggy_value": (row.get("nonzero_net_flow_tokens") or {}).get(token),
        "corrected_value_raw": corrected_value,
        "executor_address": executor_addr,
        "other_token_spend_by_executor": other_token_spend,
        "all_transfers_of_profit_token_in_receipt": token_transfers,
        "positive_net_transfer_addresses_for_this_token": positive_addrs_for_token,
        "hook_addresses_in_route": list(hook_addrs),
        "hook_fee_transfers_of_profit_token": hook_fee_transfers,
    }
    result["step2_first_positive"] = step2
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
