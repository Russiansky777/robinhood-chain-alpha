#!/usr/bin/env python3
"""Задача «форензика fomo» -- разведка ПЕРЕД любым платным Dune-запросом
(владелец, 2026-09-05, см. docs/PROJECT_STATE.md). Только чтение.

Спецификация задавалась только в переписке штаба и уже искажалась между
сессиями (предыдущий прогон перепутал адреса fomo.family с 0x65050a...
из P5-форензики -- ДРУГОЙ, уже закрытый вопрос). Адреса лаунч-
контрактов и topic0 из свежей спецификации владельца -- ПАМЯТНЫЕ, не
проверены. Этот скрипт проверяет их РЕАЛЬНО, тем же методом, что уже
применялся к pons.family в Sprint G1 (data/pons_family/SOURCE.md):
bytecode на цепи + реальное распределение topic0 по сырым логам --
НЕ подставляем topic0 в WHERE вслепую.

Дополнительно: разведка источников списка топ-10 адресов (Dune-
дашборды adam_tehc, robinscan.io/leaderboard) -- из песочницы эти
сайты не открываются, все фетчи через GH Actions."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "fomo_forensics")

from credit_guard import ensure_namespace, remaining_cycle_budget, load_state  # noqa: E402
from dune_client import DuneClient  # noqa: E402

HEADERS = {"User-Agent": "robinhood-chain-alpha-fomo-forensics-recon/1.0"}
ROBINHOOD_RPC = "https://rpc.mainnet.chain.robinhood.com"
OUT_PATH = Path("data/p3_guard_cache/fomo_forensics_recon_result.json")

BUDGET = 250.0

# Памятные, НЕ проверенные значения из спецификации владельца -- проверяем, не доверяем.
CANDIDATE_LAUNCH_CONTRACTS = [
    "0x0000ffffbe8efe702c8703ae3477ff5de3d319c0",
    "0x00004c4ccc709ef590f7c81102c0689f0263d4e9",
]
CANDIDATE_TOKEN_LAUNCHED_TOPIC0 = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"

DUNE_DASHBOARDS = [
    "https://dune.com/adam_tehc/fomo",
    "https://dune.com/adam_tehc/the-robinhood-trenches",
]


def rpc_call(method: str, params: list):
    r = requests.post(ROBINHOOD_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"{body['error']} (метод={method})")
    return body["result"]


def check_bytecode() -> dict:
    print("[recon] --- 1. bytecode лаунч-контрактов (бесплатно, RPC) ---")
    out = {}
    for addr in CANDIDATE_LAUNCH_CONTRACTS:
        try:
            code = rpc_call("eth_getCode", [addr, "latest"])
            has_code = code not in ("0x", "0x0", None)
            out[addr] = {"has_bytecode": has_code, "code_len_hex_chars": len(code) if code else 0}
            print(f"  {addr}: has_bytecode={has_code}")
        except Exception as exc:  # noqa: BLE001
            out[addr] = {"error": str(exc)[:300]}
            print(f"  {addr}: ОШИБКА {exc}")
        time.sleep(0.3)
    return out


def probe_dashboards() -> dict:
    print("\n[recon] --- 2. Dune-дашборды adam_tehc -- поиск встроенных query_id ---")
    out = {}
    for url in DUNE_DASHBOARDS:
        entry = {"url": url}
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            entry["status"] = r.status_code
            entry["content_length"] = len(r.text)
            # Next.js-приложения часто кладут начальное состояние в __NEXT_DATA__ или похожий тег -- ищем query id паттерны
            query_ids = sorted(set(re.findall(r'"query_id"\s*:\s*(\d+)', r.text) + re.findall(r'/queries/(\d+)', r.text)))
            entry["found_query_ids"] = query_ids[:30]
            entry["looks_like_js_shell"] = "<div id=\"__next\"" in r.text and len(r.text) < 20000
            print(f"  {url}: status={r.status_code}, len={len(r.text)}, query_ids_found={len(query_ids)}, "
                  f"js_shell={entry['looks_like_js_shell']}")
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)[:300]
            print(f"  {url}: ОШИБКА {exc}")
        out[url] = entry
        time.sleep(0.5)
    return out


def probe_robinscan_leaderboard() -> dict:
    print("\n[recon] --- 3. robinscan.io/leaderboard -- структура страницы ---")
    entry = {}
    try:
        r = requests.get("https://robinscan.io/leaderboard", headers=HEADERS, timeout=30)
        entry["status"] = r.status_code
        entry["content_length"] = len(r.text)
        entry["content_type"] = r.headers.get("content-type")
        entry["looks_like_js_shell"] = len(r.text) < 20000 and ("id=\"root\"" in r.text or "id=\"__next\"" in r.text)
        # Ищем очевидные адреса 0x... в теле ответа -- если сервер отдаёт данные без рендера, они будут видны
        addrs = sorted(set(re.findall(r'0x[a-fA-F0-9]{40}', r.text)))
        entry["n_hex_addresses_found_raw"] = len(addrs)
        entry["sample_addresses"] = addrs[:10]
        print(f"  status={r.status_code}, len={len(r.text)}, js_shell={entry['looks_like_js_shell']}, "
              f"addresses_in_raw_html={len(addrs)}")
    except Exception as exc:  # noqa: BLE001
        entry["error"] = str(exc)[:300]
        print(f"  ОШИБКА {exc}")
    return entry


def probe_topic0_real(client: DuneClient) -> dict:
    print("\n[recon] --- 4. РЕАЛЬНОЕ распределение topic0 по логам лаунч-контрактов (Dune, LIMIT 100) ---")
    # Окно 35 дней (не безусловный полный скан таблицы) -- та же дисциплина
    # контроля стоимости, что G1 (7-дневное окно) -- нам нужен именно
    # последний месяц активности (совпадает с окном выборки топ-10 адресов).
    addrs_sql = ", ".join(f"'{a[2:].lower()}'" for a in CANDIDATE_LAUNCH_CONTRACTS)
    sql = f"""select lower(to_hex(topic0)) as topic0, count(*) as n_logs, max(block_time) as last_seen
from robinhood.logs
where lower(to_hex(contract_address)) in ({addrs_sql})
    and block_time >= now() - interval '35' day
group by to_hex(topic0)
order by n_logs desc
limit 100"""
    qid = client.create_query("fomo_forensics_topic0_probe", sql)
    df = client.run_sql_cached("fomo_forensics_topic0_probe", sql, query_id=qid,
                                estimated_credits=5.0, expected_max_rows=100, expected_columns=3)
    rows = df.to_dict("records") if df is not None else []
    matches_candidate = any(r["topic0"].lower() == CANDIDATE_TOKEN_LAUNCHED_TOPIC0.lower() for r in rows)
    for r in rows[:10]:
        flag = " <-- СОВПАДАЕТ с памятным topic0" if r["topic0"].lower() == CANDIDATE_TOKEN_LAUNCHED_TOPIC0.lower() else ""
        print(f"  {r['topic0']}: {r['n_logs']} логов{flag}")
    return {"rows": rows, "candidate_topic0_confirmed": matches_candidate, "n_rows_total": len(rows)}


def run() -> int:
    ensure_namespace("fomo_forensics", BUDGET)
    remaining = remaining_cycle_budget(load_state())
    print(f"[recon] остаток общего цикла Dune: {remaining:.1f} кредитов")

    out = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    out["bytecode_check"] = check_bytecode()
    out["dashboard_probe"] = probe_dashboards()
    out["robinscan_probe"] = probe_robinscan_leaderboard()

    any_bytecode = any(v.get("has_bytecode") for v in out["bytecode_check"].values())
    if any_bytecode:
        client = DuneClient()
        out["topic0_probe"] = probe_topic0_real(client)
    else:
        out["topic0_probe"] = {"skipped": "ни один из кандидатных адресов не имеет bytecode на цепи -- "
                                            "не тратим Dune-кредиты на заведомо неверные адреса"}
        print("\n[recon] ПРОПУСК topic0-проверки -- ни один адрес не имеет кода на Robinhood Chain")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[recon] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
