#!/usr/bin/env python3
"""Сырая выгрузка транзакций: accountKeys + pre/postBalances + токен-балансы.

Нужна как ОСНОВАНИЕ, а не как догадка. Опознание пула провалилось на всех
15 сделках, и причина может быть либо в самих данных (пул устроен иначе),
либо в нашем чтении балансов (несовпадение индексов accountKeys и
preBalances). Отличить одно от другого можно только по сырому ответу узла.

Выгрузка кладётся в data/, чтобы дальше разбирать её БЕЗ сети.
Только чтение цепочки.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

OUT_PATH = REPO_ROOT / "data" / "solana_raw_tx_dump.json"
CACHE_PATH = REPO_ROOT / "data" / "solana_pilot_block_autopsy_cache.json"


def signatures_for(needle: str) -> list[tuple[str, str]]:
    """(подпись, чья) по признаку сделки из кэша разбора пилота."""
    cache = json.loads(CACHE_PATH.read_text())
    rows = [r for r in (cache.get("строки") or {}).values()
            if needle in (r.get("наша_покупка_utc") or "") or needle in (r.get("mint") or "")]
    if not rows:
        raise SystemExit(f"сделка по признаку {needle!r} не найдена")
    r = rows[0]
    print(f"сделка {r.get('наша_покупка_utc')} mint {r.get('mint')}")
    return [(r["лидер_signature"], "лидер"), (r["buy_signature"], "мы")]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trade", default="", help="признак сделки (время или mint)")
    ap.add_argument("--sig", action="append", default=[], help="подпись напрямую")
    ap.add_argument("--min-interval-s", type=float, default=0.05)
    args = ap.parse_args()

    from solana_crowd_scan import Rpc, helius_key  # noqa: PLC0415

    pairs = [(s, "прямо") for s in args.sig]
    if args.trade:
        pairs += signatures_for(args.trade)
    if not pairs:
        raise SystemExit("нечего выгружать: нужен --trade или --sig")

    key, _ = helius_key()
    rpc = Rpc(key, min_interval_s=args.min_interval_s, workers=1, service="разбор_пилота")
    txs = rpc.transactions([s for s, _ in pairs])
    out = {"выгружено": [], "транзакции": {}}
    for sig, who in pairs:
        tx = txs.get(sig)
        out["выгружено"].append({"подпись": sig, "чья": who, "отдалась": bool(tx)})
        if tx:
            out["транзакции"][sig] = tx
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"выгружено {len(out['транзакции'])} из {len(pairs)} -> {OUT_PATH}")


if __name__ == "__main__":
    main()
