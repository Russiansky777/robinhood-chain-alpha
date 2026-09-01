#!/usr/bin/env python3
"""Sprint R1, Шаг 2 (пред-условие): сопоставление токен -> Chainlink-фид.

Ни один декодированный на Dune ончейн-реестр (ни
`rwa_stock_factory_robinhood.factory_deployer_evt_deployed`, ни события
самого фида) не содержит прямой связи токен<->фид (см.
docs/R1_DESIGN.md, "Механика") -- фид не эмитит событие с привязкой к
токену. Рабочий путь: `description()` (селектор 0x7284e416, стандартный
Chainlink AggregatorV3Interface) на каждом из 31 фида, найденного на
Шаге 1 (`r1_feed_activity_*.csv`), обычно возвращает строку вида
"AAPL / USD" -- сопоставляем по тикеру со `symbol` из
`r1_stock_token_deployments_*.csv`. 0 кредитов Dune -- обычный
eth_call через Alchemy RPC (нужен обычный исходящий интернет, поэтому
запускается через GH Actions runner, как и HTML-скрейп на Шаге 1).

Использование: python analysis/r1_feed_match.py
"""
from __future__ import annotations

import glob
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import requests

from config import CONFIG

CACHE_DIR = Path(CONFIG.r1_cache_dir)
DESCRIPTION_SELECTOR = "0x7284e416"


def _latest(glob_pat: str) -> Path:
    matches = sorted(glob.glob(str(CACHE_DIR / glob_pat)))
    if not matches:
        raise FileNotFoundError(f"Не найден кэш по шаблону {glob_pat} в {CACHE_DIR}")
    return Path(matches[-1])


def _rpc_url() -> str:
    if CONFIG.alchemy_rpc_url:
        return CONFIG.alchemy_rpc_url
    if CONFIG.alchemy_api_key:
        return f"https://robinhood-mainnet.g.alchemy.com/v2/{CONFIG.alchemy_api_key}"
    raise RuntimeError("ALCHEMY_API_KEY / ALCHEMY_ROBINHOOD_RPC_URL не заданы.")


def eth_call_description(address: str) -> str | None:
    url = _rpc_url()
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": address, "data": DESCRIPTION_SELECTOR}, "latest"],
    }
    resp = requests.post(url, json=payload, timeout=20)
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        print(f"[r1_feed_match] {address}: RPC error {body['error']}")
        return None
    raw = body.get("result", "0x")
    if raw in ("0x", None):
        return None
    # ABI-декодирование string: пропускаем offset (32 байта) + length (32
    # байта), берём length байт содержимого.
    data = bytes.fromhex(raw[2:])
    if len(data) < 64:
        return None
    length = int.from_bytes(data[32:64], "big")
    content = data[64:64 + length]
    try:
        return content.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None


def main() -> int:
    feeds = pd.read_csv(_latest("r1_feed_activity_*.csv"))
    tokens = pd.read_csv(_latest("r1_stock_token_deployments_*.csv"))

    rows = []
    for addr in feeds["feed_address"]:
        desc = eth_call_description(addr)
        rows.append({"feed_address": addr, "description": desc})
        print(f"[r1_feed_match] {addr}: {desc!r}")

    desc_df = pd.DataFrame(rows)
    out_desc = CACHE_DIR / "r1_feed_descriptions.csv"
    desc_df.to_csv(out_desc, index=False)
    print(f"\n[r1_feed_match] Записано: {out_desc}")

    # "AAPL / USD" -> "AAPL"; допускаем варианты без пробелов/с иным
    # разделителем на всякий случай.
    def extract_ticker(desc: str | None) -> str | None:
        if not desc:
            return None
        m = re.match(r"\s*([A-Za-z.]+)\s*[/\-]", desc)
        return m.group(1).upper() if m else desc.strip().upper()

    desc_df["ticker"] = desc_df["description"].apply(extract_ticker)
    tokens["symbol_upper"] = tokens["symbol"].str.upper()

    merged = desc_df.merge(tokens, left_on="ticker", right_on="symbol_upper", how="inner")
    out_map = CACHE_DIR / "r1_feed_token_map.csv"
    merged[["token_address", "symbol", "feed_address", "description"]].to_csv(out_map, index=False)

    print(f"\n[r1_feed_match] Сопоставлено {len(merged)} из {len(feeds)} фидов "
          f"({len(feeds) - len(merged)} без совпадения по тикеру -- либо фид не сток-токена, "
          f"либо формат description() иной, см. {out_desc} для разбора).")
    print(f"[r1_feed_match] Записано: {out_map}")
    if len(merged):
        print(merged[["symbol", "feed_address", "description"]].to_string(index=False))
    return 0 if len(merged) else 1


if __name__ == "__main__":
    raise SystemExit(main())
