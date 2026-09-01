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


def test_render_quote_distribution_report_shares_use_true_total_not_row_sum():
    """Регрессия на реальный инцидент (владелец, 2026-09-01): ETH 827 +
    USDG 735 + WETH 41 = 1603 при 896 градуациях -- токен может входить
    в несколько строк (торговался против >1 quote в разных сделках),
    это не баг. Доли ОБЯЗАНЫ считаться от истинного total_tokens (896),
    НЕ от суммы n_tokens по строкам (1603) -- иначе они отвечали бы на
    другой вопрос."""
    df = pd.DataFrame({
        "quote_symbol": ["ETH", "USDG", "WETH"],
        "n_trades": [2768205, 674846, 15396],
        "n_tokens": [827, 735, 41],  # сумма 1603, БОЛЬШЕ total_tokens=896
        "vol_usd": [3.260329e8, 7.186861e7, 8.93137e5],
    })
    report = render_quote_distribution_report(df, total_tokens=896)
    assert "ETH" in report and "USDG" in report and "WETH" in report
    assert "WETH/ETH" in report and "стейблкоин" in report
    # 827/896 = 92.3% (НЕ 827/1603=51.6%, что было бы неверной методикой)
    assert "92.3%" in report, report
    assert "51.6%" not in report
    # 735/896 = 82.0%
    assert "82.0%" in report
    # Явно называет сумму по строкам (1603) и объясняет, что она больше total_tokens
    assert "1603" in report and "896" in report
    print("[test_render_quote_distribution_report_shares_use_true_total_not_row_sum] OK")


def test_render_quote_distribution_report_empty():
    assert "не получено" in render_quote_distribution_report(None, 896)
    assert "не получено" in render_quote_distribution_report(pd.DataFrame(), 896)
    assert "не получено" in render_quote_distribution_report(pd.DataFrame({"quote_symbol": ["ETH"], "n_tokens": [1], "n_trades": [1], "vol_usd": [1.0]}), 0)
    print("[test_render_quote_distribution_report_empty] OK")


def main() -> int:
    tests = [
        test_categorize_quote_symbol_weth,
        test_categorize_quote_symbol_stable,
        test_categorize_quote_symbol_other,
        test_categorize_quote_symbol_unknown,
        test_render_quote_distribution_report_shares_use_true_total_not_row_sum,
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
