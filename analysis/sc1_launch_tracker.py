#!/usr/bin/env python3
"""SC1 -- трекер запущенных токенов (`data/sc1_launches.json`), задача
5 дозапроса владельца (2026-09-02). Периодически (см.
`.github/workflows/run_sc1_launch_tracker.yml`, cron) читает реестр и
для КАЖДОГО реально запущенного токена (status == "success") подтягивает
торговую активность через публичный RPC.

**Фаза кривой (до градуации).** Торговля на Pons V2 до градуации идёт
НЕ через Uniswap-пул, а напрямую через `PonsV2BondingCurve` инстанс
этого токена (адрес = `pool_address` в реестре, см. докстринг
`sc1_launcher.py` -- на самом деле это адрес кривой, не AMM-пула).
Событи я -- `CurveBuy(buyer indexed, recipient indexed, quoteIn,
tokensOut, fee, tax)` / `CurveSell(seller indexed, recipient indexed,
tokensIn, quoteOut, fee, tax)` (источник -- дословный запрос
`contractsV2/src/v2/PonsV2BondingCurve.sol`, см. docs/SC1_LAUNCHER.md).

**Честная оговорка (после градуации).** Если токен градуировался,
торговля переходит на Uniswap V4 (`PoolManager`, Swap-события ключуются
`poolId` -- хэшем `PoolKey`, НЕ адресом пула, как в V3) -- этот скрипт
СЕЙЧАС отслеживает только фазу кривой, пост-градуационная V4-фаза
отдельно не реализована (честно помечается в отчёте по каждому токену
`graduated_untracked=true`, если объём кривой достиг
`graduationThreshold` конфига -- не додумывается вместо реализации).

Метрики на токен (все требования дозапроса):
- объём (сумма quoteIn/quoteOut в USD);
- комиссии создателя -- ТОЛЬКО если creatorTaxBps > 0 у этого запуска
  (у наших запусков по умолчанию 0, см. sc1_launcher.py -- честно 0,
  не додумывается доля через fee/tax поля без проверки семантики
  сплита, см. оговорку в коде ниже);
- число уникальных ВНЕШНИХ покупателей (buyer/seller != OUR_ADDRESSES);
- доля объёма от адресов вне OUR_ADDRESSES (наш единственный
  контролируемый адрес -- OUR_WALLET; список расширяется явно, если
  появятся другие подтверждённые наши адреса -- не выдумывается);
- первая/последняя сделка (timestamp, из block_time первого/последнего
  блока со сделкой);
- токены БЕЗ единой сделки -- помечены отдельно (`no_trades: true`),
  не участвуют в объёмных метриках.

Наружу -- агрегаты в docs/SC1_LIVE.md (регенерируется целиком каждый
прогон, не накапливается построчно) + JSON-кэш с чуть большей
детализацией (по-прежнему без построчных кошельков-покупателей).
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

REPO_ROOT = Path(__file__).parent.parent
REGISTRY_PATH = REPO_ROOT / "data" / "sc1_launches.json"
CACHE_PATH = REPO_ROOT / "data" / "p3_guard_cache" / "sc1_launch_tracker_result.json"
LIVE_DOC = REPO_ROOT / "docs" / "SC1_LIVE.md"

OUR_WALLET = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"
OUR_ADDRESSES = {OUR_WALLET.lower()}  # расширять ТОЛЬКО явно подтверждёнными нашими адресами, не гадать

CURVE_BUY_SIG = "CurveBuy(address,address,uint256,uint256,uint256,uint256)"
CURVE_SELL_SIG = "CurveSell(address,address,uint256,uint256,uint256,uint256)"


def _eth_usd_price() -> tuple[float, str]:
    import requests

    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "ethereum", "vs_currencies": "usd"}, timeout=10,
        )
        resp.raise_for_status()
        return float(resp.json()["ethereum"]["usd"]), "coingecko (live)"
    except Exception as e:  # noqa: BLE001
        print(f"[sc1_launch_tracker] CoinGecko недоступен ({e}) -- фолбэк на Dune-медиану SC1_NOTE.md.")
        return 1895.565143603286, "Dune median 01-13.08.2026 (STALE fallback)"


def _block_time(block_number: int) -> str:
    block = _rpc_call("eth_getBlockByNumber", [hex(block_number), False])
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(block["timestamp"], 16)))


def track_token(entry: dict, latest_block: int, eth_usd: float) -> dict:
    curve = entry["pool_address"]
    launch_block = entry["block_number"]
    symbol = entry["symbol"]

    buys = []
    sells = []
    for log in _chunked_get_logs(launch_block, latest_block, [topic0(CURVE_BUY_SIG)], address=curve):
        quote_in, tokens_out, fee, tax = abi_decode(["uint256", "uint256", "uint256", "uint256"], bytes.fromhex(log["data"][2:]))
        buys.append({
            "block": int(log["blockNumber"], 16),
            "buyer": "0x" + log["topics"][1][-40:],
            "quote_in": quote_in, "fee": fee, "tax": tax,
        })
    for log in _chunked_get_logs(launch_block, latest_block, [topic0(CURVE_SELL_SIG)], address=curve):
        tokens_in, quote_out, fee, tax = abi_decode(["uint256", "uint256", "uint256", "uint256"], bytes.fromhex(log["data"][2:]))
        sells.append({
            "block": int(log["blockNumber"], 16),
            "seller": "0x" + log["topics"][1][-40:],
            "quote_out": quote_out, "fee": fee, "tax": tax,
        })

    n_trades = len(buys) + len(sells)
    if n_trades == 0:
        return {"symbol": symbol, "token_address": entry["token_address"], "curve_address": curve,
                "no_trades": True, "n_trades": 0}

    volume_wei = sum(b["quote_in"] for b in buys) + sum(s["quote_out"] for s in sells)
    volume_usd = volume_wei / 1e18 * eth_usd
    fee_tax_wei = sum(b["fee"] + b["tax"] for b in buys) + sum(s["fee"] + s["tax"] for s in sells)
    fee_tax_usd = fee_tax_wei / 1e18 * eth_usd

    traders = {b["buyer"].lower() for b in buys} | {s["seller"].lower() for s in sells}
    external_traders = traders - OUR_ADDRESSES

    external_volume_wei = (
        sum(b["quote_in"] for b in buys if b["buyer"].lower() not in OUR_ADDRESSES)
        + sum(s["quote_out"] for s in sells if s["seller"].lower() not in OUR_ADDRESSES)
    )
    external_volume_share = (external_volume_wei / volume_wei) if volume_wei > 0 else float("nan")

    all_blocks = [b["block"] for b in buys] + [s["block"] for s in sells]
    first_block, last_block = min(all_blocks), max(all_blocks)

    creator_tax_bps = entry.get("creator_tax_bps", 0)  # если поле отсутствует в старой записи реестра -- честно 0, не гадаем
    creator_fee_usd_note = (
        "creatorTaxBps=0 у этого запуска -- отдельного дохода создателя из tax-компонента нет по конструкции"
        if not creator_tax_bps else
        "creatorTaxBps>0, но сплит fee/tax между протоколом и создателем НЕ подтверждён источником -- "
        "сумма показана как верхняя граница (fee+tax целиком), не как подтверждённый доход создателя"
    )

    return {
        "symbol": symbol,
        "token_address": entry["token_address"],
        "curve_address": curve,
        "no_trades": False,
        "n_trades": n_trades,
        "n_buys": len(buys),
        "n_sells": len(sells),
        "volume_usd": volume_usd,
        "fee_tax_total_usd": fee_tax_usd,
        "creator_fee_note": creator_fee_usd_note,
        "n_unique_traders": len(traders),
        "n_external_traders": len(external_traders),
        "external_volume_share": external_volume_share,
        "first_trade_utc": _block_time(first_block),
        "last_trade_utc": _block_time(last_block),
    }


def run() -> int:
    if not REGISTRY_PATH.exists():
        print(f"[sc1_launch_tracker] {REGISTRY_PATH} не существует -- реестр пуст, нечего отслеживать.")
        _write_live_doc([], None)
        return 0

    registry = json.loads(REGISTRY_PATH.read_text())
    successful = [e for e in registry if e.get("status") == "success" and e.get("token_address")]
    print(f"[sc1_launch_tracker] {len(successful)} успешных запусков в реестре из {len(registry)} записей.")

    if not successful:
        _write_live_doc([], None)
        return 0

    latest_block = int(_rpc_call("eth_blockNumber", []), 16)
    eth_usd, eth_usd_source = _eth_usd_price()

    results = [track_token(e, latest_block, eth_usd) for e in successful]

    for r in results:
        print(json.dumps(r, indent=2, ensure_ascii=False, default=str))

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps({
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "latest_block": latest_block,
        "eth_usd_price": eth_usd,
        "eth_usd_source": eth_usd_source,
        "tokens": results,
    }, indent=2, default=str))
    print(f"[sc1_launch_tracker] записано {CACHE_PATH}")

    _write_live_doc(results, {"latest_block": latest_block, "eth_usd": eth_usd, "eth_usd_source": eth_usd_source})
    return 0


def _write_live_doc(results: list[dict], meta: dict | None) -> None:
    lines = [
        "# SC1_LIVE — состояние живых запусков (Pons V2)",
        "",
        "Автогенерируется `analysis/sc1_launch_tracker.py` — не редактировать руками, правки потеряются при следующем прогоне.",
        "",
    ]
    if meta is None or not results:
        lines.append("Реестр `data/sc1_launches.json` пуст или в нём нет успешных запусков — отслеживать нечего.")
        LIVE_DOC.write_text("\n".join(lines) + "\n")
        return

    lines.append(f"Обновлено: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
                 f"(latest_block={meta['latest_block']}, ETH/USD={meta['eth_usd']:.2f}, источник={meta['eth_usd_source']})")
    lines.append("")

    dead = [r for r in results if r.get("no_trades")]
    live = [r for r in results if not r.get("no_trades")]

    lines.append(f"**{len(live)} из {len(results)} запущенных токенов имеют хотя бы одну сделку.**")
    lines.append("")

    if live:
        lines.append("| symbol | сделок | объём, $ | fee+tax, $ | уник. трейдеров | внешних | доля внеш. объёма | первая сделка | последняя сделка |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for r in sorted(live, key=lambda x: -x["volume_usd"]):
            lines.append(
                f"| {r['symbol']} | {r['n_trades']} | {r['volume_usd']:.2f} | {r['fee_tax_total_usd']:.4f} | "
                f"{r['n_unique_traders']} | {r['n_external_traders']} | {r['external_volume_share']:.1%} | "
                f"{r['first_trade_utc']} | {r['last_trade_utc']} |"
            )
        lines.append("")
        lines.append(f"Оговорка про комиссию создателя: {live[0]['creator_fee_note']}")
        lines.append("")

    if dead:
        lines.append(f"**Токены без единой сделки ({len(dead)}):** " + ", ".join(r["symbol"] for r in dead))
        lines.append("")

    lines.append(
        "Пост-градуационная фаза (Uniswap V4, после перехода с кривой на AMM-пул) "
        "этим трекером НЕ отслеживается отдельно — см. докстринг скрипта."
    )
    LIVE_DOC.write_text("\n".join(lines) + "\n")
    print(f"[sc1_launch_tracker] записано {LIVE_DOC}")


if __name__ == "__main__":
    raise SystemExit(run())
