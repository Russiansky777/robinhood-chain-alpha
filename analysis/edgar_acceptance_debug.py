#!/usr/bin/env python3
"""Диагностика: проверить фикс acceptance-datetime на ОДНОЙ реальной
заявке перед повторным прогоном всех 203 тикеров (последний прогон
занял 13 минут и дал 0/379 -- не гонять его вслепую снова)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from edgar_8k_fetch import HEADERS, get_cik_map, get_8k_filings, FILING_DATE_FROM
import requests

result = {}
cik_map = get_cik_map(["AAPL"])
result["cik_map"] = cik_map
cik = cik_map["AAPL"]["cik"]
filings = get_8k_filings(cik, FILING_DATE_FROM)
result["n_filings"] = len(filings)
result["sample_filing"] = filings[0] if filings else None

if filings:
    accession = filings[0]["accession"]
    accn_nodash = accession.replace("-", "")
    # Реальные конвенции EDGAR (пробуем несколько -- 404 на первой не
    # значит, что все неверны, могли перепутать вариант):
    candidates = {
        "flat_nodash": f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn_nodash}.txt",
        "flat_withdash": f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}.txt",
        "subdir_file_nodash": f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn_nodash}/{accn_nodash}.txt",
        "subdir_file_withdash": f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn_nodash}/{accession}.txt",
        "subdir_index_json": f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn_nodash}/index.json",
    }
    probe_results = {}
    for label, url in candidates.items():
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            probe_results[label] = {"url": url, "status": r.status_code, "len": len(r.content),
                                     "preview": r.text[:200] if r.status_code == 200 else None}
        except Exception as exc:  # noqa: BLE001
            probe_results[label] = {"url": url, "error": str(exc)}
    result["url_probe"] = probe_results

Path("data/p3_guard_cache/edgar_acceptance_debug_result.json").write_text(
    json.dumps(result, indent=2, ensure_ascii=False, default=str)
)
print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
