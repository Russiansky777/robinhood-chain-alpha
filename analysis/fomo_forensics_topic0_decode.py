#!/usr/bin/env python3
"""Задача «форензика fomo» -- структурное определение реального topic0
`TokenLaunched` (владелец: "не гадай, какое событие -- какое, проверяй
структурно"). Только чтение, дёшево (LIMIT 20 на кандидата).

Два кандидата по частоте логов (fomo_forensics_recon_result.json,
35-дневное окно): 0x67226bacc... (82473 логов), 0x2e2b3f61b7...
(63450 логов) -- ЧАСТОТА НЕ ДОКАЗЫВАЕТ СЕМАНТИКУ, деплой-событие может
быть реже, чем, например, Transfer/Swap с тех же контрактов, если
контракты сами roll-up несколько типов активности.

Метод (тот же принцип, что Sprint G1 применил к pons.family,
data/pons_family/SOURCE.md): для каждого кандидата -- тянем сырые
логи (topics + data), смотрим:
1. Число indexed-параметров (наличие topic1/topic2/topic3).
2. Длину data (не-indexed параметры) -- событие деплоя обычно несёт
   МНОГО данных (адрес токена, пула, конфиг), событие свопа/трансфера
   -- мало (обычно 1-2 uint256).
3. Реальная проверка: если предполагаемый "адрес токена" (topic1 или
   первые 32 байта data) имеет РЕАЛЬНЫЕ последующие Transfer/Swap-логи
   на цепи вскоре после этого события -- это подтверждает, что адрес
   в этом поле -- только что созданный контракт токена (см. Sprint G1,
   "круг адрес->событие->pool->свопы замкнут")."""
from __future__ import annotations

import json
import os
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
OUT_PATH = Path("data/p3_guard_cache/fomo_forensics_topic0_decode_result.json")

BUDGET = 250.0

LAUNCH_CONTRACTS = [
    "0x0000ffffbe8efe702c8703ae3477ff5de3d319c0",
    "0x00004c4ccc709ef590f7c81102c0689f0263d4e9",
]
CANDIDATES = [
    "67226bacccef969dab310a9e55dc1cf821363658e433fd330344f5cc00c79ac8",
    "2e2b3f61b70d2d131b2a807371103cc98d51adcaa5e9a8f9c32658ad8426e74e",
]


def rpc_call(method: str, params: list):
    r = requests.post(ROBINHOOD_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"{body['error']} (метод={method})")
    return body["result"]


def check_bytecode(addr: str) -> bool:
    code = rpc_call("eth_getCode", [addr, "latest"])
    return code not in ("0x", "0x0", None)


def sample_logs(client: DuneClient, topic0: str) -> list[dict]:
    addrs_sql = ", ".join(f"'{a[2:].lower()}'" for a in LAUNCH_CONTRACTS)
    sql = f"""select lower(to_hex(tx_hash)) as tx_hash, block_number, block_time,
    lower(to_hex(topic0)) as topic0, lower(to_hex(topic1)) as topic1,
    lower(to_hex(topic2)) as topic2, lower(to_hex(topic3)) as topic3,
    lower(to_hex(data)) as data
from robinhood.logs
where lower(to_hex(contract_address)) in ({addrs_sql})
    and lower(to_hex(topic0)) = '{topic0.lower()}'
    and block_time >= now() - interval '35' day
order by block_time desc
limit 20"""
    qid = client.create_query(f"fomo_forensics_topic0_sample_{topic0[:8]}", sql)
    df = client.run_sql_cached(f"fomo_forensics_topic0_sample_{topic0[:8]}", sql, query_id=qid,
                                estimated_credits=5.0, expected_max_rows=20, expected_columns=8)
    return df.to_dict("records") if df is not None else []


def analyze_candidate(topic0: str, logs: list[dict]) -> dict:
    if not logs:
        return {"topic0": topic0, "n_logs_sampled": 0}
    n_topics = []
    data_lens = []
    for log in logs:
        nt = 1 + sum(1 for k in ("topic1", "topic2", "topic3") if log.get(k) and log[k] not in (None, "", "0x"))
        n_topics.append(nt)
        data_hex = (log.get("data") or "").removeprefix("0x")
        data_lens.append(len(data_hex) // 2)  # байты

    # Кандидат в "адрес токена" -- последние 20 байт topic1 (если это адрес, а не число)
    candidate_addrs = []
    for log in logs:
        t1 = log.get("topic1")
        if t1 and len(t1.removeprefix("0x")) == 64:
            addr_guess = "0x" + t1.removeprefix("0x")[-40:]
            candidate_addrs.append(addr_guess)

    return {
        "topic0": topic0,
        "n_logs_sampled": len(logs),
        "n_indexed_topics_mode": max(set(n_topics), key=n_topics.count) if n_topics else None,
        "n_indexed_topics_distribution": sorted(set(n_topics)),
        "data_len_bytes_min": min(data_lens) if data_lens else None,
        "data_len_bytes_max": max(data_lens) if data_lens else None,
        "sample_tx_hashes": [log["tx_hash"] for log in logs[:3]],
        "sample_topic1_as_address_guess": candidate_addrs[:5],
        "sample_block_range": [min(log["block_number"] for log in logs), max(log["block_number"] for log in logs)],
    }


def discover_evt_transfer_schema(client: DuneClient) -> list[str]:
    """`erc20_robinhood.evt_transfer` реально НЕ запрашивалась раньше в
    этом коде (только упомянута в комментарии sql/04) -- реальные
    названия колонок не известны заранее, не угадываем (contract_address
    может называться иначе, например `contract_address` vs `address`).
    Разведка схемы -- бесплатно/дёшево, information_schema."""
    sql = """select column_name
from information_schema.columns
where table_schema = 'erc20_robinhood' and table_name = 'evt_transfer'
order by ordinal_position
limit 100"""
    qid = client.create_query("fomo_forensics_evt_transfer_schema", sql)
    df = client.run_sql_cached("fomo_forensics_evt_transfer_schema", sql, query_id=qid,
                                estimated_credits=1.0, expected_max_rows=100, expected_columns=1)
    cols = df["column_name"].tolist() if df is not None and "column_name" in df.columns else []
    print(f"[decode] реальные колонки erc20_robinhood.evt_transfer: {cols}")
    return cols


def check_addr_has_subsequent_activity(client: DuneClient, addr_hex_no_0x: str, contract_col: str, time_col: str) -> dict:
    """Реальная проверка: есть ли у предполагаемого 'адреса токена'
    свопы/трансферы вскоре после его появления -- подтверждает, что
    это реально созданный токен, не что-то ещё (например, адрес
    получателя комиссии). `contract_col`/`time_col` -- реальные имена,
    найденные через discover_evt_transfer_schema(), не угаданы."""
    sql = f"""select count(*) as n_transfers, min({time_col}) as first_seen, max({time_col}) as last_seen
from erc20_robinhood.evt_transfer
where lower(to_hex({contract_col})) = '{addr_hex_no_0x.lower()}'
limit 1"""
    qid = client.create_query(f"fomo_forensics_addr_activity_{addr_hex_no_0x[:10]}", sql)
    df = client.run_sql_cached(f"fomo_forensics_addr_activity_{addr_hex_no_0x[:10]}", sql, query_id=qid,
                                estimated_credits=3.0, expected_max_rows=1, expected_columns=3)
    rows = df.to_dict("records") if df is not None else []
    return rows[0] if rows else {"n_transfers": 0}


def run() -> int:
    ensure_namespace("fomo_forensics", BUDGET)
    remaining = remaining_cycle_budget(load_state())
    print(f"[decode] остаток общего цикла Dune: {remaining:.1f} кредитов")

    client = DuneClient()
    out = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "candidates": {}}

    print("[decode] --- разведка реальной схемы erc20_robinhood.evt_transfer (не угадываем колонки) ---")
    evt_transfer_cols = discover_evt_transfer_schema(client)
    out["evt_transfer_columns"] = evt_transfer_cols
    contract_col = next((c for c in evt_transfer_cols if "contract" in c.lower() or c.lower() == "address"), None)
    time_col = next((c for c in evt_transfer_cols if "block_time" in c.lower()), None)
    if not contract_col or not time_col:
        print(f"[decode] ВНИМАНИЕ: не нашли ожидаемые колонки (contract_col={contract_col}, time_col={time_col}) "
              f"среди реальных {evt_transfer_cols} -- пропускаем проверку последующей активности, не гадаем имя колонки")

    for topic0 in CANDIDATES:
        print(f"\n[decode] --- сэмплирую topic0={topic0[:16]}... ---")
        logs = sample_logs(client, topic0)
        analysis = analyze_candidate(topic0, logs)
        print(json.dumps(analysis, indent=2, ensure_ascii=False, default=str))

        # Проверяем ПЕРВЫЙ candidate_addr на реальную последующую активность как ERC-20
        if analysis.get("sample_topic1_as_address_guess") and contract_col and time_col:
            test_addr = analysis["sample_topic1_as_address_guess"][0][2:]
            has_code = check_bytecode("0x" + test_addr)
            activity = check_addr_has_subsequent_activity(client, test_addr, contract_col, time_col) if has_code else {"skipped": "нет bytecode -- не токен-контракт"}
            analysis["token_address_guess_has_bytecode"] = has_code
            analysis["token_address_guess_activity"] = activity
            print(f"  guess-адрес {('0x' + test_addr)}: has_bytecode={has_code}, activity={activity}")
            time.sleep(0.3)

        out["candidates"][topic0] = analysis

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[decode] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
