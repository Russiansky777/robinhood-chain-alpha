#!/usr/bin/env python3
"""Владелец: разведка DBot Data API как альтернативы RPC-скану.

ИСПРАВЛЕНО (владелец, по официальной документации docs.dbotx.com/
reference/wallet-trades) -- первый прогон бил в неверный
хост/параметры (api-bot-v1/servapi.dbotx.com, walletAddress=) и
получил честный 404. Правильно:
  GET https://api-data-v1.dbotx.com/kline/wallet/trades
      ?account=<адрес>&chain=solana&type=buy
  Заголовок: X-API-KEY: <DBOT_API_KEY>
  Пагинация: cursor из тела ответа.
  Поля: slot, blockTime, txHash, type, solAmount, usdAmount, mint, dexName.

Один повторный запрос с этими параметрами (плюс вторая страница по
cursor, если есть) -- сверка с 8 известными покупками лидера по txHash,
solAmount/usdAmount в пределах ±2%, глубина пагинации, число потраченных
кредитов (если сервер отдаёт это в заголовках/теле -- честно фиксируем,
не выдумываем)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dbot_pilot_report import _ACTIVE_SECRETS, _scrub_all  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dbot_wallet_trades_probe_result.json"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
HOST = "https://api-data-v1.dbotx.com"
PATH = "/kline/wallet/trades"

KNOWN_LEADER_SIGNATURES = {}
for label in ["xzc5swuory", "egp1f5j9ld", "gtdzkaqvmz", "vaddewuhuy", "hgcxvs6kjh", "7runv1hjfc", "gpuxeqplff"]:
    p = REPO_ROOT / f"data/solana_entry_log_{label}.json"
    if p.exists():
        KNOWN_LEADER_SIGNATURES[json.loads(p.read_text())["leader_signature"]] = label
_jupcat = REPO_ROOT / "data/solana_entry_log_jupcat.json"
if _jupcat.exists():
    KNOWN_LEADER_SIGNATURES[json.loads(_jupcat.read_text())["leader_signature"]] = "jupcat"

# expected_sol -- независимый эталон (phase2_events_v3, Dune-верифицирован), см. solana_batch5_leader_validation.py
EXPECTED_SOL = {
    "xzc5swuory": 4.479160464074533, "egp1f5j9ld": 22.397419817237054, "gtdzkaqvmz": 22.303506111160672,
    "vaddewuhuy": 89.42944017170453, "hgcxvs6kjh": 44.239957529640776, "7runv1hjfc": 17.692852087756545,
    "gpuxeqplff": 22.018671833714993,
}


def dbot_get(params: dict, api_key: str) -> dict:
    if any(c in api_key for c in ("\n", "\r")):
        raise RuntimeError("DBOT_API_KEY содержит перевод строки -- не отправляю как есть.")
    try:
        resp = requests.get(f"{HOST}{PATH}", params=params, headers={"X-API-KEY": api_key}, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return {"exception": _scrub_all(f"{type(exc).__name__}: {exc}")}
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {"non_json_body": _scrub_all(resp.text[:1500])}
    credit_headers = {k: v for k, v in resp.headers.items() if "credit" in k.lower() or "quota" in k.lower() or "limit" in k.lower()}
    return {"http_status": resp.status_code, "body": body, "credit_related_headers": credit_headers}


def extract_items(body):
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in ("data", "results", "trades", "items", "list"):
            v = body.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                for kk in ("data", "results", "trades", "items", "list"):
                    if isinstance(v.get(kk), list):
                        return v[kk]
    return []


def find_cursor(body):
    if not isinstance(body, dict):
        return None
    for k in ("cursor", "nextCursor", "next_cursor"):
        v = body.get(k)
        if v:
            return v
        data = body.get("data")
        if isinstance(data, dict) and data.get(k):
            return data[k]
    return None


def main() -> None:
    out: dict = {"wallet": LEADER_WALLET, "host": HOST, "path": PATH}
    api_key = os.environ.get("DBOT_API_KEY", "")
    out["dbot_api_key_present"] = bool(api_key)
    if not api_key:
        out["HONEST_ANSWER"] = "DBOT_API_KEY пуст в окружении."
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print("[dbot_probe] " + out["HONEST_ANSWER"], flush=True)
        return
    if not any(c in api_key for c in ("\n", "\r")):
        _ACTIVE_SECRETS.append(api_key)

    params1 = {"account": LEADER_WALLET, "chain": "solana", "type": "buy"}
    r1 = dbot_get(params1, api_key)
    out["page1_http_status"] = r1.get("http_status")
    out["page1_credit_related_headers"] = r1.get("credit_related_headers")
    body1 = r1.get("body")
    out["page1_body_sample"] = _scrub_all(json.dumps(body1, default=str)[:3000])
    items1 = extract_items(body1)
    out["page1_n_items"] = len(items1)
    out["page1_item_keys_sample"] = list(items1[0].keys()) if items1 and isinstance(items1[0], dict) else None
    print(f"[dbot_probe] page1: http={r1.get('http_status')} n_items={len(items1)} "
          f"credit_headers={r1.get('credit_related_headers')}", flush=True)

    cursor = find_cursor(body1)
    out["page1_cursor_found"] = cursor
    all_items = list(items1)
    n_pages = 1
    if cursor:
        r2 = dbot_get({**params1, "cursor": cursor}, api_key)
        body2 = r2.get("body")
        items2 = extract_items(body2)
        out["page2_http_status"] = r2.get("http_status")
        out["page2_n_items"] = len(items2)
        all_items.extend(items2)
        n_pages = 2
        print(f"[dbot_probe] page2 (cursor): http={r2.get('http_status')} n_items={len(items2)}", flush=True)
    out["n_pages_fetched"] = n_pages
    out["n_items_total"] = len(all_items)

    # ---------- Сверка с 8 известными покупками по txHash ----------
    by_hash = {}
    for item in all_items:
        if isinstance(item, dict):
            h = item.get("txHash") or item.get("tx_hash") or item.get("signature")
            if h:
                by_hash[h] = item

    matches = []
    for sig, label in KNOWN_LEADER_SIGNATURES.items():
        item = by_hash.get(sig)
        row = {"label": label, "signature": sig, "found": item is not None}
        if item:
            sol_amount = item.get("solAmount")
            usd_amount = item.get("usdAmount")
            row["sol_amount_from_dbot"] = sol_amount
            row["usd_amount_from_dbot"] = usd_amount
            expected = EXPECTED_SOL.get(label)
            if expected is not None and sol_amount is not None:
                try:
                    row["match_within_2pct"] = abs(float(sol_amount) - expected) / expected < 0.02
                except (TypeError, ValueError):
                    row["match_within_2pct"] = None
            else:
                row["match_within_2pct"] = None
        matches.append(row)
    out["known_purchases_check"] = matches
    out["n_known_found"] = sum(1 for m in matches if m["found"])
    out["n_known_total"] = len(matches)
    out["n_known_matched_2pct"] = sum(1 for m in matches if m.get("match_within_2pct"))

    OUT_PATH.write_text(_scrub_all(json.dumps(out, ensure_ascii=False, indent=2, default=str)))
    print(f"[dbot_probe] известных покупок найдено: {out['n_known_found']}/{out['n_known_total']}, "
          f"совпало ±2%: {out['n_known_matched_2pct']}", flush=True)
    print(f"[dbot_probe] Записано {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
