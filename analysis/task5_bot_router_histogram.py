#!/usr/bin/env python3
"""Задача 5, живой бот -- владелец 2026-09-12 ('добавка', Путь А):
"Гистограмма ... to x selector -> топ-20 по транзакциям и, если calldata
позволяет, по сумме. Отметить известные селекторы свопов... Частые
неизвестные -- показать отдельно. Исключить известные self-trade адреса.
Ответ: какой роутер несёт пользовательские свопы. Проверяем гистограммой,
не гадаем."

ПОЛНОСТЬЮ ОФЛАЙН (не открывает НИ ОДНОГО сетевого соединения) -- читает
JSONL-дамп, написанный `task5_bot_router_capture.py` на диск (реальный,
новый захват -- см. докстринг того скрипта: сырых сообщений прошлого
dry-run на диске НЕ было, честно проверено перед тем, как писать этот
код). Единственные "внесетевые" константы ниже -- уже известные пулы
(`TASK5_KNOWN_PROFITABLE_POOLS`, реальные адреса из прошлого Dune-запроса
этой сессии) и последняя реально измеренная цена ETH/USDG (для
приблизительной оценки $-эквивалента, честно помечена как оценка, не
факт)."""
from __future__ import annotations

import gzip
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from task5_bot_config import TASK5_KNOWN_PROFITABLE_POOLS, USDG, WETH
from task5_bot_router_decode import (
    KNOWN_SELF_TRADE_ADDRESSES,
    KNOWN_SWAP_SELECTORS,
    decode_calldata,
)

# Владелец, 2026-09-10 (task5_latency_signature_result.json, "измерение 1",
# реальный Dune-запрос этой сессии) -- последняя реально измеренная медиана
# цены ETH/USDG. ТОЛЬКО для приблизительного $-пересчёта в этой офлайн-
# гистограмме, не для торговых решений (там цена берётся из реестра пулов
# в реальном времени, RPC/фид, не отсюда) -- честно помечено в выводе.
REFERENCE_ETH_USD_PRICE_20260910 = 2459.6549436821965
WETH_DECIMALS, USDG_DECIMALS = 18, 6

KNOWN_POOL_ADDRESSES = {e["pool_key"].lower() for e in TASK5_KNOWN_PROFITABLE_POOLS
                        if e.get("pool_key")}

DEFAULT_DUMP = "data/task5_bot_router_capture/dump.jsonl.gz"


def _open_dump(path: str):
    if path.endswith(".gz"):
        return gzip.open(path, "rt")
    return open(path, "r")


# Честный защитный порог: РЕАЛЬНО найден (2026-09-12, разбор этого же
# захвата) случай, где Universal Router кладёт в поле amountIn протокольный
# сентинел `CONTRACT_BALANCE = 2**255` (см. task5_bot_router_decode.py) --
# декодер теперь сам превращает его в `amount_in=None`, но это ВТОРАЯ
# линия защиты на случай ещё не опознанного сентинела/битых данных: любая
# $-сумма за ОДНУ транзакцию свыше этого порога -- физически невозможна
# для реального свопа на этой цепи (см. реальные объёмы этой сессии --
# крупнейший обнаруженный "крупный своп" в замерах Задачи 5 был $5k), не
# включаем её молча в сумму, честно фиксируем как аномалию.
MAX_PLAUSIBLE_SINGLE_SWAP_USD = 100_000_000.0


def _amount_to_usd_if_priced(token_addr: str | None, amount_raw: int | None,
                              anomalies: list | None = None, to_addr: str | None = None) -> float | None:
    if token_addr is None or amount_raw is None:
        return None
    t = token_addr.lower()
    usd = None
    if t == WETH.lower():
        usd = amount_raw / (10 ** WETH_DECIMALS) * REFERENCE_ETH_USD_PRICE_20260910
    elif t == USDG.lower():
        usd = amount_raw / (10 ** USDG_DECIMALS)
    else:
        return None
    if usd > MAX_PLAUSIBLE_SINGLE_SWAP_USD:
        if anomalies is not None:
            anomalies.append({"to": to_addr, "token": token_addr, "amount_raw": amount_raw,
                               "usd_would_be": usd})
        return None
    return usd


def run(dump_path: str, top_n: int = 20, unknown_selector_min_count: int = 20) -> dict:
    n_rows = 0
    n_excluded_self_trade = 0
    by_to_selector: Counter = Counter()
    by_to_total: Counter = Counter()
    by_to_notional_usd: defaultdict = defaultdict(float)
    by_to_known_swap_tx: Counter = Counter()  # к-во tx у этого `to`, где calldata реально распозналась как своп
    by_to_examples: dict = {}
    selector_totals: Counter = Counter()
    decode_errors_sample: list = []
    amount_anomalies: list = []

    with _open_dump(dump_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            n_rows += 1
            to_addr = row["to"].lower()
            data_hex = row.get("data") or "0x"
            selector = data_hex[2:10] if len(data_hex) >= 10 else ""
            selector = ("0x" + selector) if selector else "0x(short)"

            if to_addr in KNOWN_SELF_TRADE_ADDRESSES:
                n_excluded_self_trade += 1
                continue

            by_to_selector[(to_addr, selector)] += 1
            by_to_total[to_addr] += 1
            selector_totals[selector] += 1
            if to_addr not in by_to_examples:
                by_to_examples[to_addr] = {"is_known_pool": to_addr in KNOWN_POOL_ADDRESSES}

            if selector in KNOWN_SWAP_SELECTORS:
                try:
                    intents = decode_calldata(to_addr, data_hex)
                except Exception as exc:  # noqa: BLE001
                    if len(decode_errors_sample) < 10:
                        decode_errors_sample.append(f"{to_addr} {selector}: {exc}")
                    intents = []
                if intents:
                    by_to_known_swap_tx[to_addr] += 1
                for intent in intents:
                    if intent.amount_in is None:
                        continue
                    usd = _amount_to_usd_if_priced(intent.token_in, intent.amount_in,
                                                    anomalies=amount_anomalies, to_addr=to_addr)
                    if usd is not None:
                        by_to_notional_usd[to_addr] += usd

    n_priced_addresses = len(by_to_notional_usd)

    top20_by_tx = []
    for to_addr, n_tx in by_to_total.most_common(top_n):
        selectors_for_to = {sel: n for (t, sel), n in by_to_selector.items() if t == to_addr}
        known_sel_n = sum(n for sel, n in selectors_for_to.items() if sel in KNOWN_SWAP_SELECTORS)
        top20_by_tx.append({
            "to": to_addr,
            "n_tx": n_tx,
            "is_known_pool_direct_call": by_to_examples.get(to_addr, {}).get("is_known_pool", False),
            "n_tx_known_swap_selector": known_sel_n,
            "n_tx_known_swap_decoded_ok": by_to_known_swap_tx.get(to_addr, 0),
            "notional_usd_approx_ref_2026_09_10_price": round(by_to_notional_usd.get(to_addr, 0.0), 2),
            "top_selectors": dict(sorted(selectors_for_to.items(), key=lambda kv: -kv[1])[:8]),
            "selectors_known_as": {sel: KNOWN_SWAP_SELECTORS[sel] for sel in selectors_for_to
                                    if sel in KNOWN_SWAP_SELECTORS},
        })

    top20_by_notional = sorted(
        ({"to": t, "notional_usd_approx": round(v, 2), "n_tx": by_to_total.get(t, 0)}
         for t, v in by_to_notional_usd.items()),
        key=lambda r: -r["notional_usd_approx"],
    )[:top_n]

    # Владелец: "частые неизвестные -- показать отдельно"
    frequent_unknown_selectors = [
        {"selector": sel, "n_tx": n, "sample_to": [t for (t, s), c in by_to_selector.items() if s == sel][:5]}
        for sel, n in selector_totals.most_common(50)
        if sel not in KNOWN_SWAP_SELECTORS and n >= unknown_selector_min_count
    ]

    # Ответ на прямой вопрос владельца: какой `to`-адрес несёт БОЛЬШЕ ВСЕГО
    # реально декодированных (не просто "похожих на") пользовательских
    # свопов известными селекторами, и НЕ является known pool / self-trade.
    candidates = [r for r in top20_by_tx if r["n_tx_known_swap_decoded_ok"] > 0
                  and not r["is_known_pool_direct_call"]]
    candidates.sort(key=lambda r: -r["n_tx_known_swap_decoded_ok"])
    answer = candidates[0] if candidates else None

    return {
        "dump_path": dump_path,
        "n_rows_total": n_rows,
        "n_rows_excluded_self_trade_known": n_excluded_self_trade,
        "excluded_self_trade_addresses": sorted(KNOWN_SELF_TRADE_ADDRESSES),
        "n_distinct_to_addresses": len(by_to_total),
        "n_addresses_with_priced_notional": n_priced_addresses,
        "reference_eth_usd_price_note": (
            f"{REFERENCE_ETH_USD_PRICE_20260910} -- реальная медиана ETH/USDG за 2026-09-10 "
            f"(task5_latency_signature_result.json), НЕ живая цена -- $-суммы здесь ПРИБЛИЗИТЕЛЬНЫЕ"
        ),
        "top20_by_tx_count": top20_by_tx,
        "top20_by_notional_usd_approx": top20_by_notional,
        "known_swap_selector_totals": {
            sel: {"label": label, "n_tx": selector_totals.get(sel, 0)}
            for sel, label in KNOWN_SWAP_SELECTORS.items() if selector_totals.get(sel, 0) > 0
        },
        "frequent_unknown_selectors": frequent_unknown_selectors,
        "decode_errors_sample": decode_errors_sample,
        "amount_anomalies_excluded_from_notional": amount_anomalies[:20],
        "n_amount_anomalies_total": len(amount_anomalies),
        "answer_which_router_carries_user_swaps": answer,
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dump", type=str, default=DEFAULT_DUMP)
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument("--out", type=str, default="data/task5_bot_router_histogram_result.json")
    args = ap.parse_args()

    if not Path(args.dump).exists():
        print(f"[router_histogram] ЧЕСТНО: дамп {args.dump} не найден -- сначала запустить "
              f"task5_bot_router_capture.py (реальный захват через relay, диск), не гадаем на пустом месте.",
              file=sys.stderr)
        raise SystemExit(1)

    result = run(args.dump, top_n=args.top_n)
    text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    print(text)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(text)
    print(f"[router_histogram] результат записан в {args.out}")
