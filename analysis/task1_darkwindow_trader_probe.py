#!/usr/bin/env python3
"""Задача 1, п.3 владельца (2026-09-06): «линия остаётся, специфична для
Robinhood Chain — кто торгует эти пулы по выходным: те же боты, что в
P5, или ретейл». Дословно: «дешёвый запрос по адресам свопов в тёмном
окне, без Dune по dex.trades».

Метод (0 кредитов Dune, только `eth_getLogs` через уже готовую
инфраструктуру `alchemy_fallback.py` — тот же путь, что
`fomo_forensics_find_trading_venue.py`/`p3_dislocation_guard.py`):

1. Реальные v3-пулы по 33 из 42 тикеров, реально попавших в основной
   прогон `task1_weekend_gap.py` (N=197, `docs/PROJECT_STATE.md`,
   corr(X,Z)≈-0.36..-0.38) — адреса взяты НАПРЯМУЮ из уже оплаченного
   реального результата `task1_liquidity_probe2_result.json`
   (`pool_addresses_by_token`, version='3'), не изобретаются. 9
   тикеров (AMAT/ASML/BB/CRWV/META/ORCL/PENG/SKHY/USO) НЕ имеют
   известного v3-адреса в этом результате — честно исключены, не
   гадаем.
2. Для КАЖДОГО из 8 реальных выходных, реально вошедших в N=197
   (2026-07-10 .. 2026-08-28 — 2026-07-03 не дал ни одной строки в
   финале, см. паспорт п.1 ответа владельцу 2026-09-06 — Labor-Day-
   класс бага здесь ни при чём, ни один из этих 9 понедельников не
   праздник, проверено `datetime.date.weekday()`), берём ОБА окна:
   Z (вс 20:00 -> пн 9:30 ET, тёмное) И X (пт 20:00 -> вс 19:55 ET,
   контрольное/светлое) — сравнение адресов в двух окнах прямо отвечает
   на вопрос владельца: если концентрация в Z заметно выше, чем в X,
   это довод в пользу бот-специфичного паттерна именно в тёмном окне,
   а не просто общей активности тех же адресов всегда.
3. Границы окон (unix timestamp) -> номер блока Robinhood Chain через
   бинарный поиск (`find_block_by_timestamp`, тот же метод, что уже
   есть в `p4_lit_onchain.py` для Ethereum mainnet, здесь — свой RPC
   через `alchemy_fallback.get_block_number`/`get_block`).
4. `eth_getLogs` (topic0 = Uniswap V3 `Swap`, `address` = список 33
   реальных пулов) через `alchemy_fallback._chunked_get_logs` —
   встроенная бисекция диапазона на переполнение уже проверена и
   работает (см. её докстринг).
5. `Swap(address indexed sender, address indexed recipient, ...)` —
   ОБА параметра indexed (topic1/topic2), декодируем оба как адреса
   (последние 20 байт каждого topic). ЧЕСТНАЯ ОГОВОРКА (тот же урок,
   что в fomo-форензике этой сессии): `sender`/`recipient` — это
   вызывающий контракт пула (часто роутер), не обязательно EOA
   конечного трейдера — здесь считаем оба поля и явно помечаем
   self-trade (sender==recipient, признак бота, уже видели этот
   паттерн у `0x65050a9b...` в P5-пуле) отдельно от разнообразия
   контрагентов.
6. Известный P5-бот (`docs/PROJECT_STATE.md`, `0x65050a9b7e5075a2ba
   5ced7b1b64ee66262c40dc` — доминирующий self-trading адрес в
   пуле P5, 52.5% свопов, 0 Mint-событий, владелец подтвердил "не
   угроза") — явно ищем его присутствие в обеих выборках."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alchemy_fallback import (  # noqa: E402
    UNISWAP_V3_SWAP_SIG, _chunked_get_logs, get_block, get_block_number, topic0,
)
from sleeping_refs_metrics_lib import et_to_utc  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/task1_darkwindow_trader_probe_result.json")

# Источник: docs/PROJECT_STATE.md, P3-концентрация/фрагментация
# (dune_query1_volume_result.json) -- реальный доминирующий
# self-trading адрес пула P5 (52.5% свопов, 0 Mint-событий).
KNOWN_P5_BOT = "0x65050a9b7e5075a2ba5ced7b1b64ee66262c40dc"

# Источник: data/p3_guard_cache/task1_liquidity_probe2_result.json,
# pool_addresses_by_token, version='3' -- реальные адреса, самый
# объёмный v3-пул на тикер среди 33 из 42, реально вошедших в N=197
# (task1_weekend_gap_result.json). НЕ по памяти -- прямой пересчёт.
POOLS_BY_SYMBOL = {
    "AAPL": "0x19d55aba3e5d2c389b7011c634725136dfdcae33", "AMC": "0xaa34fea710a1a737840329051d81d3b0b7c564d5",
    "AMD": "0x48d284a2a4d3dc1b3da08231fe44317e7e7aa51f", "AMZN": "0x8ac92da74ab5f3b1d024dc1943ad7e15dc4179ef",
    "BE": "0x1bad145c8f06444e0df81c28257cd20231bd1f16", "COIN": "0x6707aeac7d0e519b083219d27bb427364363183a",
    "COST": "0x0a2121a50a09ed0796ae81f9c53ff9398355a398", "CRCL": "0x654e4143e82a5824445ade0824351c2a9acd95a8",
    "DELL": "0xc30c89cb7815a1488b7998d15eec73961707fc5a", "DJT": "0x31a89afd92f9397465649ad03226c52292fc1ae5",
    "GLD": "0x7a6a053eccf1446a2633e05aa6d40d09381997ec", "GME": "0xe2b46c905e12ab8e2f864e4821a4325884c1b126",
    "GOOGL": "0x34d0dc122cf9a8eb296fc5e0d3a233625d7d19b7", "HIMS": "0xc8c90d3a1c1a24967e773ac2ad0d456ba3e31f64",
    "INTC": "0x2e5a92f5013a64661a49312111be2e8abd33f56a", "MRNA": "0xa34d0667334074df2d5bfd259e79e6b9cf1fa8bf",
    "MSFT": "0x4230750b3c69ae6054a3f778d75adc2e9a27d70f", "MSTR": "0x70504a6fafdbfb75fe971faa4dd716e79ac5624c",
    "MU": "0xd057b1bc54917855bbee58ead58647f47cab35e5", "NFLX": "0x59895c0302f41aeaa129d2fa2442cec01e7ef45e",
    "NVDA": "0xd4eb21209c4d6093f80b5b84f5c45cc093ea14a3", "PLTR": "0x851680416a4f4e1c463d45171d61acddbc8554c0",
    "QQQ": "0xd60a5d14db690b7afad71f76b108071d7175597d", "RBLX": "0x1bdb8e3a79cb1a7f228808739311e23098d33d43",
    "RDDT": "0xa8744e76aed23b05f0126335e7bd38f7935d19fe", "SLV": "0x8cb787e6c315d464775289bad00fdd67d53ecb3d",
    "SNDK": "0x5ca3fe49388b244d66f60b03edd940cc52b77b2e", "SPCX": "0xc61284332117c3fb23a2a56cceffd07f7af60029",
    "SPY": "0xddcbba3666f578e3f09516f21ff85bfee859ab5e", "TSLA": "0xf4acdaeeb7022862a763c9b1b885e11191c889e3",
    "TSM": "0x07e8ea83d4c1340774c8965125e26e12bf943bf1", "TTWO": "0xd9ab4b7fae6dc2f7020134ec744a8f53ef3e5e24",
    "USAR": "0x04391780f519b7d3ba59c9590459d76e23d225c4",
}
MISSING_SYMBOLS = ["AMAT", "ASML", "BB", "CRWV", "META", "ORCL", "PENG", "SKHY", "USO"]  # нет известного v3-адреса

# 8 реальных выходных, реально вошедших в N=197 (task1_weekend_gap_result.json) -- 2026-07-03 дал 0 строк, исключён.
WEEKENDS = ["2026-07-10", "2026-07-17", "2026-07-24", "2026-07-31", "2026-08-07", "2026-08-14", "2026-08-21", "2026-08-28"]

SWAP_TOPIC0 = topic0(UNISWAP_V3_SWAP_SIG)


def find_block_by_timestamp(target_ts: int) -> int:
    """Бинарный поиск номера блока Robinhood Chain по unix-timestamp --
    тот же метод, что `p4_lit_onchain.find_block_by_timestamp` для
    Ethereum mainnet, здесь через `alchemy_fallback` (Robinhood Chain
    RPC)."""
    hi = get_block_number()
    lo = 0
    hi_block = get_block(hi)
    if int(hi_block["timestamp"], 16) <= target_ts:
        return hi
    while lo < hi:
        mid = (lo + hi) // 2
        blk = get_block(mid)
        ts = int(blk["timestamp"], 16)
        if ts < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def window_blocks(start_utc, end_utc) -> tuple[int, int]:
    return find_block_by_timestamp(int(start_utc.timestamp())), find_block_by_timestamp(int(end_utc.timestamp()))


def topic_to_address(topic_hex: str) -> str:
    return "0x" + topic_hex[-40:]


def fetch_window_swaps(from_block: int, to_block: int) -> list[dict]:
    addrs = list(POOLS_BY_SYMBOL.values())
    logs = list(_chunked_get_logs(from_block, to_block, [SWAP_TOPIC0], chunk_size=50_000, address=addrs))
    addr_to_symbol = {v.lower(): k for k, v in POOLS_BY_SYMBOL.items()}
    out = []
    for lg in logs:
        topics = lg.get("topics", [])
        if len(topics) < 3:
            continue  # честно пропускаем нестандартный лог, не гадаем формат
        out.append({
            "symbol": addr_to_symbol.get(lg["address"].lower(), "?"),
            "pool": lg["address"].lower(),
            "block": int(lg["blockNumber"], 16),
            "sender": topic_to_address(topics[1]),
            "recipient": topic_to_address(topics[2]),
        })
    return out


def summarize(swaps: list[dict], label: str) -> dict:
    from collections import Counter
    n = len(swaps)
    senders = Counter(s["sender"] for s in swaps)
    recipients = Counter(s["recipient"] for s in swaps)
    both = Counter()
    for s in swaps:
        both[s["sender"]] += 1
        both[s["recipient"]] += 1
    self_trade = sum(1 for s in swaps if s["sender"] == s["recipient"])
    p5_bot_hits = sum(1 for s in swaps if KNOWN_P5_BOT in (s["sender"], s["recipient"]))
    top10 = both.most_common(10)
    top_share = sum(c for _, c in both.most_common(3)) / (2 * n) if n else None
    return {
        "label": label, "n_swaps": n,
        "n_distinct_addresses": len(both),
        "self_trade_fraction": self_trade / n if n else None,
        "p5_bot_hits": p5_bot_hits,
        "top3_share_of_role_slots": top_share,
        "top10_addresses": [{"address": a, "n_role_slots": c} for a, c in top10],
    }


def run() -> int:
    print(f"[darkwindow] реальных v3-пулов в выборке: {len(POOLS_BY_SYMBOL)} (пропущено без известного адреса: {MISSING_SYMBOLS})")
    result: dict = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pools_by_symbol": POOLS_BY_SYMBOL, "missing_symbols": MISSING_SYMBOLS,
        "known_p5_bot": KNOWN_P5_BOT, "weekends": WEEKENDS, "per_weekend": {},
    }
    all_z_swaps: list[dict] = []
    all_x_swaps: list[dict] = []

    for friday in WEEKENDS:
        y, m, d = (int(x) for x in friday.split("-"))
        fri = datetime(y, m, d)
        sun = fri + timedelta(days=2)
        mon = fri + timedelta(days=3)
        x_start = et_to_utc(fri.year, fri.month, fri.day, 20, 0)
        x_end = et_to_utc(sun.year, sun.month, sun.day, 19, 55)
        z_start = et_to_utc(sun.year, sun.month, sun.day, 20, 0)
        z_end = et_to_utc(mon.year, mon.month, mon.day, 9, 30)

        print(f"\n=== {friday} ===")
        z_lo, z_hi = window_blocks(z_start, z_end)
        print(f"  Z (тёмное, вс20:00->пн9:30 ET): блоки [{z_lo};{z_hi}] ({z_hi - z_lo} блоков)")
        z_swaps = fetch_window_swaps(z_lo, z_hi)
        print(f"  Z: реальных свопов {len(z_swaps)}")

        x_lo, x_hi = window_blocks(x_start, x_end)
        print(f"  X (светлое, пт20:00->вс19:55 ET): блоки [{x_lo};{x_hi}] ({x_hi - x_lo} блоков)")
        x_swaps = fetch_window_swaps(x_lo, x_hi)
        print(f"  X: реальных свопов {len(x_swaps)}")

        all_z_swaps.extend(z_swaps)
        all_x_swaps.extend(x_swaps)
        result["per_weekend"][friday] = {
            "z_blocks": [z_lo, z_hi], "x_blocks": [x_lo, x_hi],
            "z_summary": summarize(z_swaps, f"{friday} Z"), "x_summary": summarize(x_swaps, f"{friday} X"),
        }

    print("\n=== ПУЛ ПО ВСЕМ 8 ВЫХОДНЫМ ===")
    z_pooled = summarize(all_z_swaps, "Z pooled (все 8 выходных)")
    x_pooled = summarize(all_x_swaps, "X pooled (все 8 выходных)")
    for k in ("n_swaps", "n_distinct_addresses", "self_trade_fraction", "p5_bot_hits", "top3_share_of_role_slots"):
        print(f"  Z: {k} = {z_pooled[k]}   |   X: {k} = {x_pooled[k]}")
    result["pooled"] = {"Z": z_pooled, "X": x_pooled}

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[darkwindow] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
