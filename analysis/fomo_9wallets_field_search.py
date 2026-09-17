#!/usr/bin/env python3
"""Владелец (2026-09-17), Fomo НЕ закрывать -- искать правильное поле.

Прошлый раунд (fomo_9wallets_dune.py) дал 0 строк по tx_from на v3 и v4
за 7 дней. Находка ЭТОЙ ЖЕ сессии объясняет оба нуля: все 9 адресов
делегируют через EIP-7702 на контракт 0xe6cae83bde06e4c305530e199d
7217f42808555b, который сам оказался ERC-4337-совместимой smart-account
реализацией (entryPoint() реально вернул канонический EntryPoint v0.8).
При ERC-4337-исполнении в поле tx.from обычно стоит адрес bundler'а, не
адрес кошелька -- значит правило "фильтровать по tx_from, не taker" было
сформулировано ДО этого технического подтверждения и, возможно, неверно
именно для этих 9 адресов.

Три ДЕШЁВЫЕ проверки, строго по одной, с остановкой на первой давшей
строки (владелец: "не больше 3 запросов, стоп после первого
результативного"):

  1. Есть ли эти 9 адресов на Robinhood Chain вообще -- LIMIT 100 по
     robinhood.transactions, где "from" ИЛИ "to" совпадает с любым из 9,
     окно 7 дней (то же окно, что в прошлом раунде, для сопоставимости).
  2. Если строки есть: свопы, фильтр по taker/maker/sender (v4) и
     sender/recipient (v3, РЕАЛЬНЫЕ подтверждённые в прошлом раунде
     колонки -- не evt_sender/evt_recipient, которых в схеме НЕТ) --
     каждое поле проверяется отдельно (булевы matched_* колонки), чтобы
     явно назвать, в каком поле нашлись строки.
  3. Если и там ноль: бесплатный (0 кредитов Dune) прямой RPC/explorer
     probe одного адреса (берём адрес с максимальным известным nonce из
     прошлого раунда -- "ogle", nonce=15, значит у него больше всего
     шансов иметь видимую историю) -- пробуем Alchemy
     alchemy_getAssetTransfers (если ключ настроен) и публичный
     Blockscout-style explorer API robinscan.io как запасной путь;
     сообщаем честно, что реально ответило и что нашли, без выдумывания
     структуры, если оба пути не сработали.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "fomo_9wallets_mozila")
os.environ.setdefault("CREDIT_GUARD_FILE", "data/credits_spent_mozila.json")

import requests  # noqa: E402

from credit_guard import ensure_namespace, remaining_cycle_budget, load_state, BudgetGuardStop  # noqa: E402
from dune_client import DuneClient  # noqa: E402
from alchemy_fallback import _rpc_call as rpc_call  # noqa: E402
from config import CONFIG  # noqa: E402

OUT_PATH = Path("data/fomo_9wallets_field_search_result.json")
NAMESPACE_BUDGET = 200.0
WINDOW_DAYS = 7

WALLETS = {
    "SolSwizzle": "0x44fbe0006661d6d17188f1f6d42b32b5577179f7",
    "ether_monk": "0x2408ce75d217e3a70d6ca370c78c1b34d706f5a0",
    "remusofmars": "0x8ab8c0843d9738885d6273dfe3de86c56eea364c",
    "unipcs": "0x0a6EBEd0155EDB4b21D92AD02897A626CD90119E",
    "ogle": "0x1Bcc5f67CD17e13770F199fA03bC043b0cde1143",
    "avast": "0xcc0C581613DFd4ACe7c8686668427236f8BD5cC5",
    "frogman": "0x14AA2A71dbb5eF87b81F92205E2699AA4aa65794",
    "DumbCrayonEater": "0x8f62a08537cede87d511aca6436274ab4ca080a3",
    "vee": "0xa0670863bd5cd0d60022bab2eed78e81e1a06bce",
}
ADDR_LOWER_NO_0X = {name: a[2:].lower() for name, a in WALLETS.items()}
ADDR_TO_NAME = {a: name for name, a in ADDR_LOWER_NO_0X.items()}
# Известный из прошлого раунда own_nonce -- см.
# data/p3_guard_cache/fomo_forensics_new_leaders_crosscheck_result.json.
# "ogle" -- максимальный (15), значит наибольший шанс иметь видимую
# историю для бесплатного probe шага 3.
STEP3_PROBE_WALLET = "ogle"


def addrs_in_sql() -> str:
    return ", ".join(f"'{a}'" for a in ADDR_LOWER_NO_0X.values())


def query1_sql(limit_clause: str) -> str:
    return f"""select
    block_time,
    block_number,
    lower(to_hex(hash)) as tx_hash,
    lower(to_hex("from")) as tx_from,
    lower(to_hex("to")) as tx_to,
    (lower(to_hex("from")) in ({addrs_in_sql()})) as matched_from,
    (lower(to_hex("to")) in ({addrs_in_sql()})) as matched_to
from robinhood.transactions
where block_time >= now() - interval '{WINDOW_DAYS}' day
    and (lower(to_hex("from")) in ({addrs_in_sql()}) or lower(to_hex("to")) in ({addrs_in_sql()}))
order by block_time desc
{limit_clause}"""


def query2_v4_sql(limit_clause: str) -> str:
    return f"""select
    block_time,
    block_number,
    lower(to_hex(taker)) as taker,
    lower(to_hex(maker)) as maker,
    lower(to_hex(sender)) as sender_caller,
    lower(to_hex(tx_hash)) as tx_hash,
    (lower(to_hex(taker)) in ({addrs_in_sql()})) as matched_taker,
    (lower(to_hex(maker)) in ({addrs_in_sql()})) as matched_maker,
    (lower(to_hex(sender)) in ({addrs_in_sql()})) as matched_sender
from uniswap_v4_robinhood.swaps
where block_time >= now() - interval '{WINDOW_DAYS}' day
    and (lower(to_hex(taker)) in ({addrs_in_sql()})
         or lower(to_hex(maker)) in ({addrs_in_sql()})
         or lower(to_hex(sender)) in ({addrs_in_sql()}))
order by block_time desc
{limit_clause}"""


def query3_v3_sql(limit_clause: str) -> str:
    # Таблица и колонки -- РЕАЛЬНО подтверждённые прошлым раундом
    # (fomo_9wallets_dune_result.json, schema_check): uniswap_v3_robinhood.
    # uniswapv3pool_evt_swap, колонки sender/recipient (НЕ evt_sender/
    # evt_recipient -- владелец предположил эти имена, реальная схема
    # другая, используем подтверждённую, не гадаем).
    return f"""select
    evt_block_time as block_time,
    evt_block_number as block_number,
    lower(to_hex(sender)) as sender,
    lower(to_hex(recipient)) as recipient,
    lower(to_hex(contract_address)) as pool_contract,
    lower(to_hex(evt_tx_hash)) as tx_hash,
    (lower(to_hex(sender)) in ({addrs_in_sql()})) as matched_sender,
    (lower(to_hex(recipient)) in ({addrs_in_sql()})) as matched_recipient
from uniswap_v3_robinhood.uniswapv3pool_evt_swap
where evt_block_time >= now() - interval '{WINDOW_DAYS}' day
    and (lower(to_hex(sender)) in ({addrs_in_sql()}) or lower(to_hex(recipient)) in ({addrs_in_sql()}))
order by evt_block_time desc
{limit_clause}"""


def run_smoke_query(client: DuneClient, out: dict, key: str, label: str, sql: str, expected_columns: int,
                     estimated_credits: float) -> list[dict]:
    print(f"\n[field_search] Запрос '{key}' ({label}), LIMIT 100:")
    print(sql)
    qid = client.create_query(key, sql)
    df = client.run_sql_cached(key, sql, query_id=qid, estimated_credits=estimated_credits,
                                expected_max_rows=100, expected_columns=expected_columns)
    rows = df.to_dict("records") if df is not None else []
    cost = next((e["credits"] for e in reversed(client.credit_ledger) if e["name"] == key), None)
    print(f"[field_search] {label}: {len(rows)} строк, реальная стоимость {cost}")
    out[key] = {"n_rows": len(rows), "cost": cost, "label": label,
                "sample_rows": rows[:10]}
    return rows


def free_step3_probe(out: dict) -> None:
    """Бесплатный (0 кредитов Dune) probe -- владелец, шаг 3: 'посмотреть
    по цепи бесплатно, что вообще делает один из адресов'."""
    addr = WALLETS[STEP3_PROBE_WALLET]
    print(f"\n[field_search] Шаг 3 (бесплатно): probe адреса {STEP3_PROBE_WALLET} ({addr}), "
          f"известный own_nonce=15 (максимальный из 9, наибольший шанс на видимую историю).")
    step3: dict = {"probed_wallet": STEP3_PROBE_WALLET, "address": addr}

    try:
        nonce_hex = rpc_call("eth_getTransactionCount", [addr, "latest"])
        step3["current_nonce_live_rpc"] = int(nonce_hex, 16)
        print(f"[field_search] eth_getTransactionCount (живой, latest): {step3['current_nonce_live_rpc']}")
    except Exception as exc:  # noqa: BLE001
        step3["current_nonce_live_rpc_error"] = str(exc)[:300]

    # Alchemy alchemy_getAssetTransfers -- расширенный (не стандартный
    # JSON-RPC) метод, бесплатный в рамках уже настроенного ключа, ЕСЛИ
    # он есть и поддерживается ЭТОЙ сетью -- честно пробуем, не гадаем
    # заранее об успехе.
    alchemy_url = CONFIG.alchemy_rpc_url or (
        f"https://robinhood-mainnet.g.alchemy.com/v2/{CONFIG.alchemy_api_key}" if CONFIG.alchemy_api_key else None
    )
    step3["alchemy_configured"] = bool(alchemy_url)
    if alchemy_url:
        for direction, param_key in (("from", "fromAddress"), ("to", "toAddress")):
            try:
                resp = requests.post(
                    alchemy_url,
                    json={
                        "jsonrpc": "2.0", "id": 1, "method": "alchemy_getAssetTransfers",
                        "params": [{
                            param_key: addr,
                            "category": ["external", "erc20"],
                            "maxCount": "0x64",
                            "order": "desc",
                        }],
                    },
                    timeout=20,
                )
                body = resp.json()
                if "error" in body:
                    step3[f"alchemy_asset_transfers_{direction}_error"] = str(body["error"])[:300]
                    print(f"[field_search] alchemy_getAssetTransfers ({direction}): ошибка {body['error']}")
                else:
                    transfers = body.get("result", {}).get("transfers", [])
                    step3[f"alchemy_asset_transfers_{direction}_n"] = len(transfers)
                    step3[f"alchemy_asset_transfers_{direction}_sample"] = transfers[:10]
                    print(f"[field_search] alchemy_getAssetTransfers ({direction}): {len(transfers)} переводов")
            except Exception as exc:  # noqa: BLE001
                step3[f"alchemy_asset_transfers_{direction}_exception"] = str(exc)[:300]
                print(f"[field_search] alchemy_getAssetTransfers ({direction}): исключение {exc}")
    else:
        print("[field_search] Alchemy не настроен (ни ALCHEMY_ROBINHOOD_RPC_URL, ни ALCHEMY_API_KEY) -- пропущено честно.")

    # Blockscout-style explorer API -- robinscan.io уже использовался в
    # этой сессии для /api/leaderboard (реальный публичный JSON API,
    # см. PROJECT_STATE.md) -- пробуем стандартный Blockscout v2 путь
    # для истории транзакций адреса.
    for path in (
        f"https://robinscan.io/api/v2/addresses/{addr}/transactions",
        f"https://robinscan.io/api?module=account&action=txlist&address={addr}",
    ):
        try:
            resp = requests.get(path, timeout=20, headers={"User-Agent": "robinhood-chain-alpha/1.0"})
            entry = {"status_code": resp.status_code}
            if resp.status_code == 200:
                try:
                    body = resp.json()
                    items = body.get("items") if isinstance(body, dict) else None
                    if items is None and isinstance(body, dict):
                        items = body.get("result")
                    entry["n_items"] = len(items) if isinstance(items, list) else None
                    entry["sample"] = items[:10] if isinstance(items, list) else body
                except ValueError:
                    entry["non_json_body_sample"] = resp.text[:500]
            else:
                entry["body_sample"] = resp.text[:300]
            step3.setdefault("robinscan_probes", {})[path] = entry
            print(f"[field_search] robinscan probe {path}: status={resp.status_code}, "
                  f"n_items={entry.get('n_items')}")
        except Exception as exc:  # noqa: BLE001
            step3.setdefault("robinscan_probes", {})[path] = {"exception": str(exc)[:300]}
            print(f"[field_search] robinscan probe {path}: исключение {exc}")

    out["step3_free_onchain_probe"] = step3


def run() -> int:
    ensure_namespace("fomo_9wallets_mozila", NAMESPACE_BUDGET)
    remaining = remaining_cycle_budget(load_state())
    print(f"[field_search] остаток общего цикла Dune (Mozila): {remaining:.1f} кредитов, "
          f"бюджет пространства 'fomo_9wallets_mozila': {NAMESPACE_BUDGET}")

    client = DuneClient()
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "wallets": WALLETS, "window_days": WINDOW_DAYS,
                 "context": "Предыдущий раунд (fomo_9wallets_dune_result.json): 0 строк по tx_from на v3+v4 -- "
                             "эта задача ищет ПРАВИЛЬНОЕ поле, не закрывает линию."}

    # --- Запрос 1: есть ли адреса на цепи вообще (from ИЛИ to) ---
    try:
        rows1 = run_smoke_query(client, out, "field_search_q1_raw_txns", "robinhood.transactions (from OR to)",
                                 query1_sql("limit 100"), expected_columns=7, estimated_credits=20.0)
    except BudgetGuardStop:
        out["STOPPED"] = "Запрос 1 остановлен гардом бюджета -- см. лог выше."
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0

    if not rows1:
        print("[field_search] Запрос 1: 0 строк -- адресов НЕТ в сырых транзакциях за 7 дней. "
              "Переходим сразу к бесплатному шагу 3 (может закрыть 7-дневный горизонт ограничения).")
        free_step3_probe(out)
        out["answer_line"] = ("их нет на этой цепи (в пределах 7-дневного окна robinhood.transactions "
                               "по from И to одновременно) -- см. step3_free_onchain_probe на предмет "
                               "видимой истории ЗА ПРЕДЕЛАМИ этого окна")
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        print(f"\n[field_search] ОТВЕТ: {out['answer_line']}")
        return 0

    matched_from = sum(1 for r in rows1 if r.get("matched_from"))
    matched_to = sum(1 for r in rows1 if r.get("matched_to"))
    print(f"[field_search] Запрос 1: {len(rows1)} строк -- matched_from={matched_from}, matched_to={matched_to}")
    out["query1_summary"] = {"n_rows": len(rows1), "matched_from": matched_from, "matched_to": matched_to}

    # --- Запрос 2: v4 свопы, taker/maker/sender ---
    try:
        rows2 = run_smoke_query(client, out, "field_search_q2_v4_taker_maker_sender",
                                 "uniswap_v4_robinhood.swaps (taker/maker/sender)",
                                 query2_v4_sql("limit 100"), expected_columns=9, estimated_credits=18.0)
    except BudgetGuardStop:
        out["STOPPED"] = "Запрос 2 остановлен гардом бюджета -- см. лог выше."
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0

    if rows2:
        n_taker = sum(1 for r in rows2 if r.get("matched_taker"))
        n_maker = sum(1 for r in rows2 if r.get("matched_maker"))
        n_sender = sum(1 for r in rows2 if r.get("matched_sender"))
        fields_found = [f for f, n in (("taker", n_taker), ("maker", n_maker), ("sender", n_sender)) if n > 0]
        print(f"[field_search] Запрос 2: {len(rows2)} строк -- taker={n_taker}, maker={n_maker}, sender={n_sender}")
        out["query2_summary"] = {"n_rows": len(rows2), "matched_taker": n_taker,
                                  "matched_maker": n_maker, "matched_sender": n_sender,
                                  "fields_found": fields_found}
        out["answer_line"] = f"адреса найдены в поле {'/'.join(fields_found)} (uniswap_v4_robinhood.swaps, окно {WINDOW_DAYS}д)"
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        print(f"\n[field_search] ОТВЕТ: {out['answer_line']}")
        return 0
    print("[field_search] Запрос 2: 0 строк по taker/maker/sender на v4 -- переходим к запросу 3 (v3).")

    # --- Запрос 3: v3 свопы, sender/recipient ---
    try:
        rows3 = run_smoke_query(client, out, "field_search_q3_v3_sender_recipient",
                                 "uniswap_v3_robinhood.uniswapv3pool_evt_swap (sender/recipient)",
                                 query3_v3_sql("limit 100"), expected_columns=8, estimated_credits=15.0)
    except BudgetGuardStop:
        out["STOPPED"] = "Запрос 3 остановлен гардом бюджета -- см. лог выше."
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0

    if rows3:
        n_sender = sum(1 for r in rows3 if r.get("matched_sender"))
        n_recipient = sum(1 for r in rows3 if r.get("matched_recipient"))
        fields_found = [f for f, n in (("sender", n_sender), ("recipient", n_recipient)) if n > 0]
        print(f"[field_search] Запрос 3: {len(rows3)} строк -- sender={n_sender}, recipient={n_recipient}")
        out["query3_summary"] = {"n_rows": len(rows3), "matched_sender": n_sender,
                                  "matched_recipient": n_recipient, "fields_found": fields_found}
        out["answer_line"] = f"адреса найдены в поле {'/'.join(fields_found)} (uniswap_v3_robinhood.uniswapv3pool_evt_swap, окно {WINDOW_DAYS}д)"
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        print(f"\n[field_search] ОТВЕТ: {out['answer_line']}")
        return 0

    print("[field_search] Запрос 3: 0 строк -- ни одно из полей (taker/maker/sender/recipient) не нашло "
          "адреса в свопах, но сами адреса ЕСТЬ в сырых транзакциях (запрос 1 дал строки) -- значит они "
          "участвуют в других типах транзакций, не в декодированных Uniswap-свопах напрямую. "
          "Бесплатный шаг 3: смотрим, что реально делает один из адресов.")
    free_step3_probe(out)
    out["answer_line"] = ("они не торгуют через декодированные Uniswap v3/v4 свопы (ни в одном из полей "
                           "taker/maker/sender/recipient) -- но ЕСТЬ в сырых транзакциях "
                           f"({out['query1_summary']}) -- см. step3_free_onchain_probe, что именно они делают")
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[field_search] ОТВЕТ: {out['answer_line']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
