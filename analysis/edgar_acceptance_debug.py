#!/usr/bin/env python3
"""Диагностика: проверить фикс acceptance-datetime на ОДНОЙ реальной
заявке перед повторным прогоном всех 203 тикеров (последний прогон
занял 13 минут и дал 0/379 -- не гонять его вслепую снова)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from edgar_8k_fetch import get_acceptance_datetime, get_cik_map, get_8k_filings, FILING_DATE_FROM

result = {}
cik_map = get_cik_map(["AAPL"])
result["cik_map"] = cik_map
cik = cik_map["AAPL"]["cik"]
filings = get_8k_filings(cik, FILING_DATE_FROM)
result["n_filings"] = len(filings)
result["sample_filing"] = filings[0] if filings else None

if filings:
    accession = filings[0]["accession"]
    dt = get_acceptance_datetime(cik, accession)
    result["acceptance_datetime"] = dt

Path("data/p3_guard_cache/edgar_acceptance_debug_result.json").write_text(
    json.dumps(result, indent=2, ensure_ascii=False, default=str)
)
print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
