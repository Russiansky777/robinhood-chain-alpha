#!/usr/bin/env python3
"""Задача 5, владелец 2026-09-12: "Три проверки до продолжения бота, все
бесплатные" + "Локация секвенсера -- без VPS: whois/ipinfo по IP
feed/RPC, затем Globalping из 10+ европейских точек."

Все операции здесь -- ЧТЕНИЕ публичных бесплатных источников (Blockscout
API, openchain.xyz signature DB, whois/ipinfo, Globalping API, прямой
JSON-RPC eth_getCode) -- ноль Dune-кредитов, ноль VPS, ноль транзакций.
Требует полного доступа в интернет -- запускается ТОЛЬКО через GH
Actions (эта песочница сама блокирует egress к большинству внешних
доменов, см. PROJECT_STATE.md)."""
from __future__ import annotations

import json
import re
import time

import requests

RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
BLOCKSCOUT_API = "https://robinhoodchain.blockscout.com/api/v2"
OPENCHAIN_API = "https://api.openchain.xyz/signature-database/v1/lookup"
GLOBALPING_API = "https://api.globalping.io/v1/measurements"

FEED_HOST = "feed.mainnet.chain.robinhood.com"
RPC_HOST = "rpc.mainnet.chain.robinhood.com"

# Пункт 3 владельца: контракты, которые вызывают топ-3 исполнителя (реально
# из data/p3_guard_cache/task5_gas_reverts_top20_result.json).
TOP3_CALLED_CONTRACTS = {
    "824ABDA7...": "0x4136DA0962588710967033AC1127DA9C9E638C7D",  # топ-1, он же топ-2 (та же строка)
    "D83E60D2...": "0x4136DA0962588710967033AC1127DA9C9E638C7D",  # топ-2 -- ТОТ ЖЕ контракт, что топ-1
    "DA8EF690...": "0x0CF8C1789824523B82A0077007E19A97CA6D0E0B",  # топ-3
}

# Пункт 2 владельца: фондирование топ-5 исполнителей (реально из
# data/p3_guard_cache/task5_operators_result.json, funding_info).
TOP5_FUNDERS = {
    "824ABDA7E8BFCF47C543761C50BC7BF6756DEDA9": "432BDB9D229D2EDCB27029A1A71C41DE06C17D2E",
    "D83E60D273FD0CB3B05C2611DB940FECB7B6CA64": "919968A4238A54B381F022A360260EF5D7E015AC",
    "DA8EF690D9C9B6C7DB1A5F95943C838309306B03": "56C262027E0DE4AEA31D2489529CB25D23E58A8B",
    "FDE88016A65B2371F2C6E4F698A0C79598E46435": "C27A2B4BD5B374C3CB3C711E17901628824F8258",
    "FE68082A448F17A3D1700957B9F63176CECD334E": "C27A2B4BD5B374C3CB3C711E17901628824F8258",
}


def rpc_call(method: str, params: list) -> dict:
    resp = requests.post(RPC_URL, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("result")


def blockscout_address_info(address: str) -> dict:
    try:
        resp = requests.get(f"{BLOCKSCOUT_API}/addresses/{address}", timeout=15)
        if resp.status_code == 200:
            return resp.json()
        return {"http_status": resp.status_code}
    except Exception as exc:
        return {"error": str(exc)}


def extract_push4_selectors(bytecode_hex: str) -> list[str]:
    """Реальная, известная эвристика: Solidity-компилятор для функций
    без модификаторов реализует dispatcher как последовательность
    `PUSH4 <selector> ... EQ ... JUMPI` -- ищем все PUSH4 (опкод 0x63)
    literal-байты, это НЕ идеальный декомпилятор, но реальный, честный
    способ извлечь вероятные селекторы без запуска тяжёлых инструментов
    типа Panoramix/Heimdall (недоступны в этой сессии)."""
    code = bytecode_hex[2:] if bytecode_hex.startswith("0x") else bytecode_hex
    selectors = set()
    i = 0
    raw = bytes.fromhex(code) if len(code) % 2 == 0 else b""
    j = 0
    while j < len(raw):
        op = raw[j]
        if op == 0x63 and j + 5 <= len(raw):  # PUSH4
            selectors.add(raw[j + 1:j + 5].hex())
            j += 5
        elif 0x60 <= op <= 0x7f:  # прочие PUSHx -- пропускаем их непосредственные байты
            push_len = op - 0x5f
            j += 1 + push_len
        else:
            j += 1
    return sorted(selectors)


def openchain_lookup(selectors: list[str]) -> dict:
    if not selectors:
        return {}
    try:
        params = {"function": ",".join(f"0x{s}" for s in selectors), "filter": "true"}
        resp = requests.get(OPENCHAIN_API, params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        return {"http_status": resp.status_code}
    except Exception as exc:
        return {"error": str(exc)}


def dns_resolve_via_rpc_host_lookup(hostname: str) -> dict:
    """Простое DNS-разрешение через socket (на GH Actions раннере есть
    полный доступ к интернету/DNS, в отличие от песочницы Claude)."""
    import socket
    try:
        infos = socket.getaddrinfo(hostname, 443)
        ips = sorted({info[4][0] for info in infos})
        return {"ips": ips}
    except Exception as exc:
        return {"error": str(exc)}


def ipinfo_lookup(ip: str) -> dict:
    try:
        resp = requests.get(f"https://ipinfo.io/{ip}/json", timeout=15)
        if resp.status_code == 200:
            return resp.json()
        return {"http_status": resp.status_code}
    except Exception as exc:
        return {"error": str(exc)}


EUROPEAN_LOCATIONS = [
    {"country": "DE"}, {"country": "FR"}, {"country": "GB"}, {"country": "NL"},
    {"country": "IE"}, {"country": "PL"}, {"country": "ES"}, {"country": "IT"},
    {"country": "SE"}, {"country": "CH"}, {"country": "AT"}, {"country": "BE"},
]


def globalping_measure(target: str, measurement_type: str = "ping") -> dict:
    try:
        body = {"type": measurement_type, "target": target, "locations": EUROPEAN_LOCATIONS, "limit": len(EUROPEAN_LOCATIONS)}
        resp = requests.post(GLOBALPING_API, json=body, timeout=15)
        if resp.status_code not in (200, 202):
            return {"http_status": resp.status_code, "body": resp.text[:500]}
        measurement_id = resp.json().get("id")
        if not measurement_id:
            return {"error": "no measurement id returned", "raw": resp.json()}
        for _ in range(30):
            time.sleep(2)
            r2 = requests.get(f"{GLOBALPING_API}/{measurement_id}", timeout=15)
            if r2.status_code != 200:
                continue
            data = r2.json()
            if data.get("status") == "finished":
                return data
        return {"error": "timed out waiting for measurement", "measurement_id": measurement_id}
    except Exception as exc:
        return {"error": str(exc)}


def run() -> dict:
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    print("=== 1. Timeboost / express lane auction ===")
    # Реальный ответ уже найден через WebSearch (владелец, доложено в
    # чате): "Robinhood Chain does not have an auction contract... every
    # transaction... paid exactly the network price". Здесь -- попытка
    # независимой проверки: поиск известного паттерна методов аукциона
    # (ArbitrumTimeboost, ExpressLaneAuction) среди верифицированных
    # контрактов НЕ делается (Blockscout не даёт полнотекстовый поиск по
    # исходникам через публичный API без индексатора) -- честно, не
    # изобретаем метод, которого нет.
    out["timeboost"] = {
        "found_via_websearch": False,
        "note": (
            "WebSearch (реальные источники, доложено владельцу в чате): 'Robinhood Chain "
            "does not have an auction contract... There is no express lane sold on the side "
            "either. Every transaction... paid exactly the network price, to the wei.' -- "
            "Timeboost/express lane аукцион на Robinhood Chain НЕ существует. Независимая "
            "on-chain проверка (поиск контракта-аукциона) не проводилась -- Blockscout "
            "публичный API не даёт полнотекстовый поиск по исходникам без индексатора."
        ),
    }

    print("=== 2. Фондирование топ-5 исполнителей блока 0 ===")
    funder_info = {}
    for executor, funder in TOP5_FUNDERS.items():
        funder_info[executor] = {
            "funder": funder,
            "blockscout": blockscout_address_info(funder),
        }
    out["top5_funders"] = funder_info

    print("=== 3. Контракты топ-3 исполнителей -- верификация + best-effort декомпиляция ===")
    contract_info = {}
    for label, addr in TOP3_CALLED_CONTRACTS.items():
        info: dict = {"address": addr, "blockscout": blockscout_address_info(addr)}
        code = rpc_call("eth_getCode", [addr, "latest"])
        if code and code != "0x":
            info["bytecode_len_bytes"] = (len(code) - 2) // 2
            selectors = extract_push4_selectors(code)
            info["n_push4_selectors_found"] = len(selectors)
            info["openchain_lookup"] = openchain_lookup(selectors[:50])  # ограничение -- не заваливать API
        else:
            info["bytecode_len_bytes"] = 0
        contract_info[label] = info
    out["top3_contracts"] = contract_info

    print("=== 4. Локация секвенсера -- DNS/whois/ipinfo + Globalping (без VPS) ===")
    feed_dns = dns_resolve_via_rpc_host_lookup(FEED_HOST)
    rpc_dns = dns_resolve_via_rpc_host_lookup(RPC_HOST)
    out["feed_dns"] = feed_dns
    out["rpc_dns"] = rpc_dns
    out["feed_ipinfo"] = {ip: ipinfo_lookup(ip) for ip in feed_dns.get("ips", [])[:3]}
    out["rpc_ipinfo"] = {ip: ipinfo_lookup(ip) for ip in rpc_dns.get("ips", [])[:3]}

    print("=== 5. Globalping -- RTT из 10+ европейских точек ===")
    out["globalping_feed_ping"] = globalping_measure(FEED_HOST, "ping")
    out["globalping_rpc_ping"] = globalping_measure(RPC_HOST, "ping")

    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=str, default=None)
    args = ap.parse_args()
    result = run()
    text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    print(text)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
