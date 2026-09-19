#!/usr/bin/env python3
"""Владелец, задача 2 шаг A: справочник ботов-снайперов Solana из
dune spellbook (git, без Dune-кредитов) -- какие адреса собирают
комиссию известных копитрейд/снайпер-ботов (Trojan, Banana Gun, BonkBot
и т.д.), чтобы потом сверить с получателями SOL/WSOL-переводов в логах
трассы (шаг C).

Источник: https://github.com/duneanalytics/spellbook, каталог
dbt_subprojects/solana/models/_sector/dex/bot_trades/solana/ -- каждая
модель платформы (*_bot_trades.sql) объявляет один или несколько
Jinja-констант {% set *fee_receiver*/*wallet*/*sniper* = '<адрес>' %} и
фильтрует "WHERE trader_id != fee_receiver AND signer != fee_receiver" --
то есть это адрес, КУДА бот собирает свою комиссию, не адрес трейдера и
не program_id инструкции свопа. Извлечение регуляркой по исходникам,
без запуска dbt/без Dune API -- дёшево и повторяемо.

Требует локальный sparse-checkout spellbook (см. README-команду ниже),
путь передаётся аргументом или через SPELLBOOK_PATH."""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_bot_fee_addresses.json"

# Ручные фиксы там, где имя бота задано отдельной Jinja-переменной
# (bot_label), а не инлайн-строкой перед "as bot".
BOT_LABEL_FIXES = {
    "dbt_subprojects/solana/models/_sector/dex/bot_trades/solana/platforms/bonkbot/bonkbot_solana_bot_trades.sql": "BonkBot",
    "dbt_subprojects/solana/models/_sector/dex/bot_trades/solana/platforms/bonkbot/bonkbot_solana_fee_payments_raw.sql": "BonkBot",
}


def extract(spellbook_root: Path) -> dict:
    base = spellbook_root / "dbt_subprojects/solana/models/_sector/dex/bot_trades/solana"
    files = sorted(set(glob.glob(str(base / "platforms/**/*.sql"), recursive=True)) |
                    set(glob.glob(str(base / "*.sql"))))
    rows = []
    for f in files:
        text = Path(f).read_text(encoding="utf-8")
        m = re.search(r'["\']([^"\']+)["\']\s+[Aa][Ss]\s+bot\b', text)
        bot_name_inline = m.group(1) if m else None
        rel = os.path.relpath(f, spellbook_root)
        bot_name = BOT_LABEL_FIXES.get(rel, bot_name_inline)
        if bot_name is None or "{{" in bot_name:
            continue
        receivers = re.findall(
            r'set\s+(\w*(?:fee_receiver|wallet|sniper)\w*)\s*=\s*["\']([1-9A-HJ-NP-Za-km-z]{32,44})["\']',
            text, re.IGNORECASE)
        for var_name, addr in receivers:
            rows.append({"bot_name": bot_name, "address": addr, "role": "fee_receiver",
                         "var_name": var_name, "spellbook_path": rel})

    seen = set()
    deduped = []
    for row in rows:
        key = (row["bot_name"], row["address"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    deduped.sort(key=lambda r: (r["bot_name"], r["address"]))

    by_addr: dict[str, list[str]] = {}
    for row in deduped:
        by_addr.setdefault(row["address"], []).append(row["bot_name"])
    shared = {addr: bots for addr, bots in by_addr.items() if len(bots) > 1}

    return {
        "source": "https://github.com/duneanalytics/spellbook (main, dbt_subprojects/solana/models/_sector/dex/bot_trades)",
        "method": ("regex-извлечение Jinja {% set *fee_receiver*/*wallet*/*sniper* = '<адрес>' %} "
                    "из каждой *_bot_trades.sql модели платформы -- без запуска dbt, без Dune API."),
        "note": ("Адреса -- КОШЕЛЬКИ-ПОЛУЧАТЕЛИ КОМИССИИ бота (per spellbook: trader_id != fee_receiver "
                 "AND signer != fee_receiver), НЕ program_id инструкции и не адрес трейдера. "
                 "Unibot/ChainSwap/Nova/Bloom -- отдельные модели spellbook, не включены в "
                 "объединённую dex_solana_bot_trades (возможно новее/экспериментальные), но включены "
                 "здесь как есть."),
        "HONEST_NOTE_addresses_shared_by_multiple_bots": shared,
        "n_bots": len({r["bot_name"] for r in deduped}),
        "n_addresses": len(deduped),
        "rows": deduped,
    }


def main() -> None:
    spellbook_root = Path(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SPELLBOOK_PATH", "/tmp/spellbook_clone"))
    if not spellbook_root.exists():
        print(f"[bot_fee_addresses] {spellbook_root} не найден. Сначала:\n"
              f"  git clone --depth 1 --filter=blob:none --sparse https://github.com/duneanalytics/spellbook.git {spellbook_root}\n"
              f"  cd {spellbook_root} && git sparse-checkout set --no-cone "
              f"'/dbt_subprojects/solana/models/_sector/dex/bot_trades/**'", flush=True)
        sys.exit(1)
    result = extract(spellbook_root)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"n_bots={result['n_bots']} n_addresses={result['n_addresses']} -> {OUT_PATH}", flush=True)
    if result["HONEST_NOTE_addresses_shared_by_multiple_bots"]:
        print("Общие адреса у нескольких ботов:", result["HONEST_NOTE_addresses_shared_by_multiple_bots"], flush=True)


if __name__ == "__main__":
    main()
