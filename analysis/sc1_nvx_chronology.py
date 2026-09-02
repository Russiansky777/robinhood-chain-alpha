#!/usr/bin/env python3
"""Параллельная задача (владелец, 2026-09-02): точная поблочная
хронология событий вокруг реального запуска NVX на Pons V2.

Токен `0xbc48F81Ca11FFe99ac9CD9EC711491FF8401d31F`, кривая (curve,
"pool_address" в реестре) `0xf19905b03f6e501aa3d89eab3e4912185bcd4df4`,
транзакция создания `0x7be6444d3415ac675fbdd87a73d69c70ee330357c1ea99852711d3b213ee8996`,
блок 52456131 (все три значения -- из `data/sc1_launches.json`,
восстановленного реальной транзакцией, не выдуманы).

Источники и семантика полей событий -- дословный запрос
`contractsV2/src/v2/PonsV2BondingCurve.sol` (см. docs/SC1_LAUNCHER.md
и коммит этого скрипта):

    event CurveBuy(address indexed buyer, address indexed recipient,
        uint256 quoteIn, uint256 tokensOut, uint256 fee, uint256 tax)
    event CurveSell(address indexed seller, address indexed recipient,
        uint256 tokensIn, uint256 quoteOut, uint256 fee, uint256 tax)

**quoteIn (buy) = ПОЛНЫЙ реальный расход покупателя** (переменная
`spent` в исходнике, эмиттится ПОСЛЕ вычета fee/tax из промежуточных
расчётов, но САМА `spent` -- это то, что покупатель реально потратил,
включая долю fee+tax, которая просто перечисляется отдельно
получателям, а не добавляется поверх). **quoteOut (sell) = ЧИСТАЯ
сумма, реально полученная продавцом** (`grossQuoteOut - fee - tax`,
переведена продавцу этой же суммой). Значит net P&L по адресу
считается ПРЯМО как `sum(quoteOut продаж) - sum(quoteIn покупок)` --
комиссии уже учтены внутри этих сумм, повторно вычитать их не нужно
(дословно проверено по коду buy()/sell(), не предположено).

**Цена** -- два независимых числа на каждую строку: (1)
`effective_trade_price_eth_per_token` -- эффективная цена ИМЕННО этой
сделки (`quoteIn/tokensOut` для buy, `quoteOut/tokensIn` для sell),
считается напрямую из полей события, доступна ВСЕГДА, не зависит от
archival state; (2) `price_after_block_eth_per_token` -- маржинальная
цена кривой (`getReserves()`, constant-product с фантомными резервами,
см. docs/SC1_LAUNCHER.md) ЧЕРЕЗ `eth_call` с блок-тегом = номер блока
события -- состояние ПОСЛЕ ВСЕХ транзакций этого блока, не после
конкретной транзакции внутри блока (точно для последнего события в
блоке, приблизительно для остальных при коллизии блока). **Честная
оговорка, найдено эмпирически 2026-09-02**: публичная нода этого чейна
НЕ архивная -- для части исторических блоков `eth_call` возвращает
`{'code': -32000, 'message': 'metadata is not found, <block>'}`; в
таком случае `price_after_block_eth_per_token = None`, помечено явно
(`price_after_block_note`), эффективная цена сделки в строке всё равно
присутствует.

Наружу -- полная построчная хронология (не агрегат) -- владелец
явно попросил таблицу по каждому событию, это не то же самое правило
"наружу только агрегаты", что у wash-slice (там -- анализ ЧУЖИХ
кластеров/кошельков сторонних пользователей; здесь -- наш собственный
единственный запущенный токен, разбор его же публичных ончейн-событий).
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alchemy_fallback import _chunked_get_logs, _rpc_call, topic0  # noqa: E402
from eth_abi import decode as abi_decode  # noqa: E402

TOKEN = "0xbc48F81Ca11FFe99ac9CD9EC711491FF8401d31F"
CURVE = "0xf19905b03f6e501aa3d89eab3e4912185bcd4df4"
CREATION_TX = "0x7be6444d3415ac675fbdd87a73d69c70ee330357c1ea99852711d3b213ee8996"
CREATION_BLOCK = 52456131

CURVE_BUY_SIG = "CurveBuy(address,address,uint256,uint256,uint256,uint256)"
CURVE_SELL_SIG = "CurveSell(address,address,uint256,uint256,uint256,uint256)"
GET_RESERVES_SELECTOR = topic0("getReserves()")[:10]

OUT_JSON = Path("data/p3_guard_cache/sc1_nvx_chronology.json")
OUT_DOC = Path("docs/SC1_NVX_CHRONOLOGY.md")

_block_time_cache: dict[int, int] = {}
_reserves_cache: dict[int, tuple[int, int]] = {}


def block_time(block_number: int) -> int:
    if block_number not in _block_time_cache:
        block = _rpc_call("eth_getBlockByNumber", [hex(block_number), False])
        _block_time_cache[block_number] = int(block["timestamp"], 16)
    return _block_time_cache[block_number]


def get_reserves_at(block_number: int) -> tuple[int, int] | None:
    """eth_call с историческим block-тегом -- НАЙДЕНО 2026-09-02 (первый
    прогон): публичная нода не архивная, для блоков вне какого-то
    недокументированного недавнего окна возвращает `{'code': -32000,
    'message': 'metadata is not found, <block>'}` -- вернуть None и
    ЯВНО пометить недоступность вместо падения всего скрипта (эффективная
    цена сделки из самого события -- см. `_effective_trade_price` --
    остаётся доступна всегда, не зависит от архивного state)."""
    if block_number in _reserves_cache:
        return _reserves_cache[block_number]
    try:
        result = _rpc_call("eth_call", [{"to": CURVE, "data": GET_RESERVES_SELECTOR}, hex(block_number)])
    except RuntimeError as e:
        print(f"[sc1_nvx_chronology] getReserves() на блоке {block_number} недоступен ({e}) -- "
              "нода не архивная, использую только эффективную цену сделки для этой строки.")
        _reserves_cache[block_number] = None
        return None
    quote_reserve, token_reserve = abi_decode(["uint256", "uint256"], bytes.fromhex(result[2:]))
    _reserves_cache[block_number] = (quote_reserve, token_reserve)
    return _reserves_cache[block_number]


def eth_usd_price() -> tuple[float, str]:
    import requests
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "ethereum", "vs_currencies": "usd"}, timeout=10,
        )
        resp.raise_for_status()
        return float(resp.json()["ethereum"]["usd"]), "coingecko (live, на момент отчёта -- НЕ историческая цена на момент каждой сделки)"
    except Exception as e:  # noqa: BLE001
        print(f"[sc1_nvx_chronology] CoinGecko недоступен ({e}) -- USD-конвертация пропущена, только ETH.")
        return 0.0, "недоступно"


def fetch_events() -> list[dict]:
    latest_block = int(_rpc_call("eth_blockNumber", []), 16)
    print(f"[sc1_nvx_chronology] диапазон: {CREATION_BLOCK} .. {latest_block} ({latest_block - CREATION_BLOCK + 1} блоков)")

    buy_topic0 = topic0(CURVE_BUY_SIG)
    sell_topic0 = topic0(CURVE_SELL_SIG)

    events = []
    n_calls = 0

    def _count(lo, hi, n):
        nonlocal n_calls
        n_calls += 1

    for log in _chunked_get_logs(
        CREATION_BLOCK, latest_block, [[buy_topic0, sell_topic0]],
        chunk_size=2000, address=CURVE, on_call=_count,
    ):
        is_buy = log["topics"][0].lower() == buy_topic0.lower()
        data = bytes.fromhex(log["data"][2:])
        v0, v1, fee, tax = abi_decode(["uint256", "uint256", "uint256", "uint256"], data)
        block_number = int(log["blockNumber"], 16)
        tx_index = int(log["transactionIndex"], 16)
        log_index = int(log["logIndex"], 16)
        actor = "0x" + log["topics"][1][-40:]
        recipient = "0x" + log["topics"][2][-40:]
        events.append({
            "type": "buy" if is_buy else "sell",
            "block_number": block_number,
            "tx_hash": log["transactionHash"],
            "tx_index": tx_index,
            "log_index": log_index,
            "actor": actor,  # buyer (buy) / seller (sell)
            "recipient": recipient,
            "quote_amount_wei": v0 if is_buy else v1,  # quoteIn (buy) / quoteOut (sell) -- см. докстринг про семантику
            "token_amount_wei": v1 if is_buy else v0,  # tokensOut (buy) / tokensIn (sell)
            "fee_wei": fee,
            "tax_wei": tax,
        })

    print(f"[sc1_nvx_chronology] eth_getLogs вызовов: {n_calls}, найдено событий: {len(events)}")
    events.sort(key=lambda e: (e["block_number"], e["tx_index"], e["log_index"]))
    return events


def run() -> int:
    creation_ts = block_time(CREATION_BLOCK)
    events = fetch_events()
    eth_usd, eth_usd_src = eth_usd_price()

    rows = []
    same_block_as_creation = []
    prev_ts = creation_ts
    prev_block = CREATION_BLOCK

    for i, e in enumerate(events):
        ts = block_time(e["block_number"])
        # Эффективная цена ЭТОЙ сделки -- ВСЕГДА доступна (считается из
        # самого события, не требует archival state): для buy это
        # quoteIn/tokensOut (сколько ETH заплачено за 1 токен в среднем
        # по этой сделке), для sell -- quoteOut/tokensIn.
        effective_price = (
            (e["quote_amount_wei"] / e["token_amount_wei"]) if e["token_amount_wei"] else float("nan")
        )
        reserves = get_reserves_at(e["block_number"])
        price_after_block = (reserves[0] / reserves[1]) if reserves and reserves[1] else None

        delta_blocks_prev = e["block_number"] - prev_block
        delta_seconds_prev = ts - prev_ts

        row = {
            "seq": i + 1,
            "block_number": e["block_number"],
            "block_timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
            "tx_index": e["tx_index"],
            "log_index": e["log_index"],
            "tx_hash": e["tx_hash"],
            "type": e["type"],
            "address": e["actor"],
            "recipient": e["recipient"] if e["recipient"].lower() != e["actor"].lower() else None,
            "token_amount": e["token_amount_wei"] / 1e18,
            "eth_amount": e["quote_amount_wei"] / 1e18,
            "eth_amount_usd": (e["quote_amount_wei"] / 1e18 * eth_usd) if eth_usd else None,
            "fee_eth": e["fee_wei"] / 1e18,
            "tax_eth": e["tax_wei"] / 1e18,
            "effective_trade_price_eth_per_token": effective_price,
            "price_after_block_eth_per_token": price_after_block,
            "price_after_block_note": (
                "недоступно -- нода не архивная (metadata not found на этом блоке), см. docstring"
                if price_after_block is None else
                "post-block getReserves() -- точно для последнего события в блоке, приблизительно для остальных при коллизии блока"
            ),
            "delta_blocks_since_prev_event": delta_blocks_prev,
            "delta_seconds_since_prev_event": delta_seconds_prev,
        }
        rows.append(row)

        if e["block_number"] == CREATION_BLOCK:
            same_block_as_creation.append(row)

        prev_ts, prev_block = ts, e["block_number"]

    # --- Дельта launchToken -> первый своп ---
    if events:
        first = events[0]
        first_ts = block_time(first["block_number"])
        delta_first_blocks = first["block_number"] - CREATION_BLOCK
        delta_first_seconds = first_ts - creation_ts
    else:
        delta_first_blocks = None
        delta_first_seconds = None

    # --- По адресам: холд, результат в ETH/USD с учётом комиссий ---
    per_address = defaultdict(lambda: {
        "n_buys": 0, "n_sells": 0,
        "tokens_bought": 0.0, "tokens_sold": 0.0,
        "eth_spent": 0.0, "eth_received": 0.0,
        "first_buy_block": None, "first_buy_ts": None,
        "last_sell_block": None, "last_sell_ts": None,
    })
    for e in events:
        addr = e["actor"].lower()
        a = per_address[addr]
        ts = block_time(e["block_number"])
        if e["type"] == "buy":
            a["n_buys"] += 1
            a["tokens_bought"] += e["token_amount_wei"] / 1e18
            a["eth_spent"] += e["quote_amount_wei"] / 1e18
            if a["first_buy_block"] is None:
                a["first_buy_block"] = e["block_number"]
                a["first_buy_ts"] = ts
        else:
            a["n_sells"] += 1
            a["tokens_sold"] += e["token_amount_wei"] / 1e18
            a["eth_received"] += e["quote_amount_wei"] / 1e18
            a["last_sell_block"] = e["block_number"]
            a["last_sell_ts"] = ts

    address_summary = []
    n_bought_never_sold = 0
    for addr, a in per_address.items():
        net_tokens = a["tokens_bought"] - a["tokens_sold"]
        net_eth = a["eth_received"] - a["eth_spent"]  # уже net-of-fees, см. докстринг
        still_holding = net_tokens > 1e-12
        if still_holding:
            n_bought_never_sold += 1
        holding_seconds = None
        holding_blocks = None
        holding_status = "не покупал(а)" if a["n_buys"] == 0 else ("ещё держит" if still_holding else "закрыл(а) позицию")
        if a["n_buys"] > 0 and a["last_sell_ts"] is not None:
            holding_seconds = a["last_sell_ts"] - a["first_buy_ts"]
            holding_blocks = a["last_sell_block"] - a["first_buy_block"]
        address_summary.append({
            "address": addr,
            "n_buys": a["n_buys"], "n_sells": a["n_sells"],
            "tokens_bought": a["tokens_bought"], "tokens_sold": a["tokens_sold"],
            "net_tokens_held": net_tokens,
            "eth_spent": a["eth_spent"], "eth_received": a["eth_received"],
            "net_eth_realized": net_eth,
            "net_usd_realized": (net_eth * eth_usd) if eth_usd else None,
            "status": holding_status,
            "holding_seconds": holding_seconds,
            "holding_blocks": holding_blocks,
            "first_buy_block": a["first_buy_block"],
        })
    address_summary.sort(key=lambda x: (x["first_buy_block"] is None, x["first_buy_block"]))

    # --- Дельты между соседними событиями (уже посчитаны построчно выше, агрегат для отчёта) ---
    deltas = [{"from_seq": r["seq"] - 1, "to_seq": r["seq"],
               "delta_blocks": r["delta_blocks_since_prev_event"],
               "delta_seconds": r["delta_seconds_since_prev_event"]} for r in rows]

    result = {
        "token": TOKEN, "curve": CURVE,
        "creation_tx": CREATION_TX, "creation_block": CREATION_BLOCK,
        "creation_block_timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(creation_ts)),
        "latest_block_checked": int(_rpc_call("eth_blockNumber", [])),
        "n_events": len(events),
        "delta_creation_to_first_swap_blocks": delta_first_blocks,
        "delta_creation_to_first_swap_seconds": delta_first_seconds,
        "purchases_same_block_as_creation": same_block_as_creation,
        "n_addresses_bought_never_sold": n_bought_never_sold,
        "eth_usd_price": eth_usd, "eth_usd_source": eth_usd_src,
        "events": rows,
        "event_deltas": deltas,
        "per_address": address_summary,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(result, indent=2, default=str))
    print(f"[sc1_nvx_chronology] записано {OUT_JSON}, событий: {len(events)}")
    _write_doc(result)
    return 0


def _write_doc(result: dict) -> None:
    lines = [
        "# SC1_NVX_CHRONOLOGY — поблочная хронология событий NVX",
        "",
        "Автогенерируется `analysis/sc1_nvx_chronology.py` — не редактировать руками.",
        "",
        f"Токен: `{result['token']}` · Кривая: `{result['curve']}`",
        f"Создание: tx `{result['creation_tx']}`, блок {result['creation_block']} ({result['creation_block_timestamp_utc']})",
        f"Проверено до блока {result['latest_block_checked']}.",
        f"Курс ETH/USD: {result['eth_usd_price']} ({result['eth_usd_source']})",
        "",
    ]
    if result["n_events"] == 0:
        lines.append("**За весь проверенный период — ни одной сделки (CurveBuy/CurveSell) не было.**")
        OUT_DOC.write_text("\n".join(lines) + "\n")
        return

    lines.append(f"**Дельта launchToken → первый своп: {result['delta_creation_to_first_swap_blocks']} блоков, "
                 f"{result['delta_creation_to_first_swap_seconds']} секунд.**")
    lines.append("")
    if result["purchases_same_block_as_creation"]:
        lines.append(f"**{len(result['purchases_same_block_as_creation'])} событие(й) в ТОМ ЖЕ блоке, что и создание** "
                      f"(блок {result['creation_block']}) — потенциальный снайпинг.")
    else:
        lines.append("Ни одной сделки в том же блоке, что создание токена.")
    lines.append("")
    lines.append(f"**Адресов купили и НЕ продали ни разу: {result['n_addresses_bought_never_sold']}.**")
    lines.append("")

    lines.append("## Полная хронология событий")
    lines.append("")
    lines.append("| # | блок | время (UTC) | tx_idx | тип | адрес | ETH | токены | комиссия ETH | цена сделки (ETH/токен) | цена после блока | Δблок | Δсек |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in result["events"]:
        after_block = f"{r['price_after_block_eth_per_token']:.10f}" if r["price_after_block_eth_per_token"] is not None else "н/д (не архивная нода)"
        lines.append(
            f"| {r['seq']} | {r['block_number']} | {r['block_timestamp_utc']} | {r['tx_index']} | {r['type']} | "
            f"`{r['address']}` | {r['eth_amount']:.6f} | {r['token_amount']:.4f} | {r['fee_eth']+r['tax_eth']:.6f} | "
            f"{r['effective_trade_price_eth_per_token']:.10f} | {after_block} | "
            f"{r['delta_blocks_since_prev_event']} | {r['delta_seconds_since_prev_event']} |"
        )
    lines.append("")
    lines.append(
        "**Цена сделки** — эффективная цена ЭТОЙ сделки (`quoteIn/tokensOut` для buy, `quoteOut/tokensIn` для sell), "
        "доступна всегда. **Цена после блока** — `getReserves()` на конкретном историческом блоке; публичная нода "
        "не архивная и для части блоков не хранит state (`metadata is not found`) — тогда «н/д»."
    )

    lines.append("## По адресам")
    lines.append("")
    lines.append("| адрес | покупок | продаж | куплено токенов | продано токенов | потрачено ETH | получено ETH | net ETH | net USD | статус | холд (с) |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for a in result["per_address"]:
        usd = f"{a['net_usd_realized']:.2f}" if a["net_usd_realized"] is not None else "н/д"
        hold = a["holding_seconds"] if a["holding_seconds"] is not None else "—"
        lines.append(
            f"| `{a['address']}` | {a['n_buys']} | {a['n_sells']} | {a['tokens_bought']:.4f} | {a['tokens_sold']:.4f} | "
            f"{a['eth_spent']:.6f} | {a['eth_received']:.6f} | {a['net_eth_realized']:.6f} | {usd} | {a['status']} | {hold} |"
        )
    lines.append("")
    lines.append(
        "**Оговорка про \"цену после сделки\":** это `getReserves()` кривой на конец БЛОКА, а не после конкретной "
        "транзакции — если в блоке несколько событий, точна только для последнего из них в этом блоке."
    )
    OUT_DOC.write_text("\n".join(lines) + "\n")
    print(f"[sc1_nvx_chronology] записано {OUT_DOC}")


if __name__ == "__main__":
    raise SystemExit(run())
