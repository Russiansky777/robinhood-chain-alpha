#!/usr/bin/env python3
"""Юнит-тесты для чистых функций analysis/sprint_g1.py (владелец, п.5:
отчёт по распределению quote-активов) -- те, что не требуют сети.

Использование: python analysis/test_sprint_g1.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

from sprint_g1 import categorize_quote_symbol, render_quote_distribution_report


def test_categorize_quote_symbol_weth():
    assert categorize_quote_symbol("WETH") == "WETH/ETH"
    assert categorize_quote_symbol("ETH") == "WETH/ETH"
    assert categorize_quote_symbol("weth") == "WETH/ETH"  # регистронезависимо
    print("[test_categorize_quote_symbol_weth] OK")


def test_categorize_quote_symbol_stable():
    for sym in ["USDC", "USDT", "DAI", "USDG", "USDC.e", "fooUSDbar"]:
        assert categorize_quote_symbol(sym) == "стейблкоин", sym
    print("[test_categorize_quote_symbol_stable] OK")


def test_categorize_quote_symbol_other():
    assert categorize_quote_symbol("PONS-STOCK") == "прочее (вкл. сток-токены)"
    assert categorize_quote_symbol("AAPL-X") == "прочее (вкл. сток-токены)"
    print("[test_categorize_quote_symbol_other] OK")


def test_categorize_quote_symbol_unknown():
    assert categorize_quote_symbol("(NULL/unknown)") == "(не определён)"
    print("[test_categorize_quote_symbol_unknown] OK")


def test_render_quote_distribution_report_shares_and_buckets():
    df = pd.DataFrame({
        "quote_symbol": ["WETH", "USDC", "PONS-STOCK"],
        "n_trades": [900, 90, 10],
        "n_tokens": [720, 180, 90],  # сумма 990, не обязана быть 896 (токен может считаться дважды)
        "vol_usd": [9_000_000.0, 900_000.0, 90_000.0],
    })
    report = render_quote_distribution_report(df)
    assert "WETH" in report and "USDC" in report and "PONS-STOCK" in report
    assert "WETH/ETH" in report and "стейблкоин" in report and "вкл. сток-токены" in report
    # 720/990 = 72.7%
    assert "72.7%" in report
    print("[test_render_quote_distribution_report_shares_and_buckets] OK")


def test_render_quote_distribution_report_empty():
    assert "не получено" in render_quote_distribution_report(None)
    assert "не получено" in render_quote_distribution_report(pd.DataFrame())
    print("[test_render_quote_distribution_report_empty] OK")


def main() -> int:
    tests = [
        test_categorize_quote_symbol_weth,
        test_categorize_quote_symbol_stable,
        test_categorize_quote_symbol_other,
        test_categorize_quote_symbol_unknown,
        test_render_quote_distribution_report_shares_and_buckets,
        test_render_quote_distribution_report_empty,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"[FAIL] {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"[ERROR] {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    if failed:
        print(f"\n{failed}/{len(tests)} тестов упало.")
        return 1
    print(f"\nВсе {len(tests)} тестов прошли.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
