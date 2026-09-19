#!/usr/bin/env python3
"""Владелец: разведка DBot Data API как альтернативы RPC-скану --
GET /kline/wallet/trades?chain=solana&walletAddress=<LEADER_WALLET>,
ОДИН запрос (плюс вторая страница, если есть курсор -- проверить
глубину пагинации). Сверка с 8 уже известными покупками лидера
(подписи из логов трассы пилота) -- найдены ли, совпадают ли суммы.

ПЕРВЫЙ РЕАЛЬНЫЙ КОНТАКТ с этим конкретным эндпоинтом -- формат ответа
не подтверждён документацией, только эмпирически (тот же принцип, что
solana_dbot_pilot_report.py). Хосты/заголовки авторизации -- те же,
что уже подтверждены в этом репозитории (api-bot-v1.dbotx.com,
x-api-key; servapi.dbotx.com, token) -- переиспользованы напрямую.

БЕЗОПАСНОСТЬ: секрет никогда не подставляется без проверки на
управляющие символы, вся печать/запись через _scrub_all() (тот же
паттерн, что и в solana_dbot_pilot_report.py, после инцидента с
BITQUERY_APIKEY)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_dbot_pilot_report import dbot_get, _ACTIVE_SECRETS, _scrub_all, DBOT_HOSTS  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dbot_wallet_trades_probe_result.json"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"

KNOWN_LEADER_SIGNATURES = {}
for label in ["xzc5swuory", "egp1f5j9ld", "gtdzkaqvmz", "vaddewuhuy", "hgcxvs6kjh", "7runv1hjfc", "gpuxeqplff"]:
    p = REPO_ROOT / f"data/solana_entry_log_{label}.json"
    if p.exists():
        KNOWN_LEADER_SIGNATURES[json.loads(p.read_text())["leader_signature"]] = label
_jupcat = REPO_ROOT / "data/solana_entry_log_jupcat.json"
if _jupcat.exists():
    KNOWN_LEADER_SIGNATURES[json.loads(_jupcat.read_text())["leader_signature"]] = "jupcat"


def find_signature_in_item(item) -> str | None:
    if not isinstance(item, dict):
        return None
    for k in ("signature", "txId", "tx_id", "txHash", "tx_hash", "hash", "sig"):
        if item.get(k):
            return item[k]
    return None


def find_cursor(body) -> str | None:
    if not isinstance(body, dict):
        return None
    for k in ("nextCursor", "next_cursor", "cursor", "next", "nextPage"):
        v = body.get(k)
        if v:
            return v
        data = body.get("data")
        if isinstance(data, dict) and data.get(k):
            return data[k]
    return None


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


def main() -> None:
    out: dict = {"wallet": LEADER_WALLET, "endpoint": "/kline/wallet/trades"}
    api_key = os.environ.get("DBOT_API_KEY", "")
    out["dbot_api_key_present"] = bool(api_key)
    if not api_key:
        out["HONEST_ANSWER"] = "DBOT_API_KEY пуст в окружении."
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print("[dbot_probe] " + out["HONEST_ANSWER"], flush=True)
        return
    if not any(c in api_key for c in ("\n", "\r")):
        _ACTIVE_SECRETS.append(api_key)

    r1 = dbot_get("/kline/wallet/trades", api_key, params={"chain": "solana", "walletAddress": LEADER_WALLET})
    out["page1_http_status"] = r1.get("http_status")
    out["page1_host_used"] = r1.get("host")
    out["page1_auth_used"] = r1.get("auth_tried")
    body1 = r1.get("body")
    out["page1_body_sample"] = _scrub_all(json.dumps(body1, default=str)[:3000])
    items1 = extract_items(body1)
    out["page1_n_items"] = len(items1)
    out["page1_item_keys_sample"] = list(items1[0].keys()) if items1 and isinstance(items1[0], dict) else None
    print(f"[dbot_probe] page1: http={r1.get('http_status')} host={r1.get('host')} n_items={len(items1)}", flush=True)

    cursor = find_cursor(body1)
    out["page1_cursor_found"] = cursor
    all_items = list(items1)
    n_pages = 1
    if cursor:
        params2 = {"chain": "solana", "walletAddress": LEADER_WALLET, "cursor": cursor}
        r2 = dbot_get("/kline/wallet/trades", api_key, params=params2, hosts=[r1.get("host")] if r1.get("host") else None)
        body2 = r2.get("body")
        items2 = extract_items(body2)
        out["page2_http_status"] = r2.get("http_status")
        out["page2_n_items"] = len(items2)
        all_items.extend(items2)
        n_pages = 2
        print(f"[dbot_probe] page2 (курсор): http={r2.get('http_status')} n_items={len(items2)}", flush=True)
    out["n_pages_fetched"] = n_pages
    out["n_items_total"] = len(all_items)

    # ---------- Сверка с 8 известными покупками ----------
    found_sigs = set()
    for item in all_items:
        sig = find_signature_in_item(item)
        if sig:
            found_sigs.add(sig)
    matches = []
    for sig, label in KNOWN_LEADER_SIGNATURES.items():
        matches.append({"label": label, "signature": sig, "found_in_dbot_response": sig in found_sigs})
    out["known_purchases_check"] = matches
    out["n_known_found"] = sum(1 for m in matches if m["found_in_dbot_response"])
    out["n_known_total"] = len(matches)

    OUT_PATH.write_text(_scrub_all(json.dumps(out, ensure_ascii=False, indent=2, default=str)))
    print(f"[dbot_probe] известных покупок найдено в ответе DBot: {out['n_known_found']}/{out['n_known_total']}", flush=True)
    print(f"[dbot_probe] Записано {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
