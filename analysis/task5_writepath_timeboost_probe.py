#!/usr/bin/env python3
"""Задача 5, живой бот -- владелец 2026-09-12 (пересмотр): "На Arbitrum
нет мемпула... гонка блока 0 -- это скорость ЗАПИСИ, не чтения." Проверки
1 и 4 из этого раунда -- пути записи транзакции и точная перепроверка
Timeboost для chain 4663.

Всё ниже -- ТОЛЬКО чтение / безопасная интроспекция: DNS-резолв, ipinfo,
JSON-RPC методы, которые не требуют подписи/баланса (`web3_clientVersion`,
`rpc_modules`, `eth_chainId`) и один JSON-RPC вызов с ЗАВЕДОМО невалидными
параметрами к `timeboost_sendExpressLaneTransaction` -- это НЕ отправка
транзакции, это просто чтение текста ошибки метода (существует метод или
нет), реальных средств/подписи это не требует и не потребляет. Ноль
транзакций, ноль трат, ноль риска бана (публичные JSON-RPC интроспекции).
"""
from __future__ import annotations

import json
import socket
import time

import requests

RPC_MAINNET = "https://rpc.mainnet.chain.robinhood.com"
RPC_TESTNET = "https://rpc.testnet.chain.robinhood.com"
# Документированный (WebSearch, blockrazor.io -- цитата: "The public
# Sequencer endpoint is available at https://sequencer.testnet.chain.robinhood.com
# for testnet") тестнет sequencer-эндпоинт -- РЕАЛЬНЫЙ, найден в открытых
# источниках, не выдуман.
SEQUENCER_TESTNET = "https://sequencer.testnet.chain.robinhood.com"
# Мейннет-аналог по паттерну имени -- НЕ подтверждён документацией,
# это ДОГАДКА по аналогии с testnet; ниже явно помечено как unconfirmed.
SEQUENCER_MAINNET_GUESS = "https://sequencer.mainnet.chain.robinhood.com"

CLOUDFLARE_KNOWN_IPS_PREFIX = ("104.20.", "172.66.", "104.16.", "104.17.", "104.18.", "104.19.", "104.21.")


def dns_resolve(hostname: str) -> dict:
    try:
        infos = socket.getaddrinfo(hostname, 443)
        ips = sorted({info[4][0] for info in infos})
        return {"resolved": True, "ips": ips}
    except Exception as exc:
        return {"resolved": False, "error": str(exc)}


def ipinfo_lookup(ip: str) -> dict:
    try:
        resp = requests.get(f"https://ipinfo.io/{ip}/json", timeout=15)
        if resp.status_code == 200:
            return resp.json()
        return {"http_status": resp.status_code}
    except Exception as exc:
        return {"error": str(exc)}


def rpc_post(url: str, method: str, params: list, timeout: int = 12) -> dict:
    try:
        resp = requests.post(url, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1}, timeout=timeout)
        out = {"http_status": resp.status_code}
        try:
            out["json"] = resp.json()
        except Exception:
            out["text"] = resp.text[:500]
        return out
    except Exception as exc:
        return {"error": str(exc)}


def probe_endpoint(label: str, url: str) -> dict:
    out: dict = {"url": url}
    out["web3_clientVersion"] = rpc_post(url, "web3_clientVersion", [])
    out["rpc_modules"] = rpc_post(url, "rpc_modules", [])
    out["eth_chainId"] = rpc_post(url, "eth_chainId", [])
    # ЧЕСТНО: заведомо невалидный вызов express-lane метода -- НЕ
    # отправка транзакции, только чтение текста ошибки. Если метод не
    # зарегистрирован (Timeboost выключен/не задеплоен) -- вернётся
    # стандартная JSON-RPC ошибка "method not found" (-32601). Если
    # метод СУЩЕСТВУЕТ -- вернётся другая ошибка (невалидная подпись/раунд).
    out["timeboost_sendExpressLaneTransaction_probe"] = rpc_post(
        url, "timeboost_sendExpressLaneTransaction", [{"probe": "invalid_params_intentionally"}]
    )
    return out


def run() -> dict:
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    print("=== 1. Путь записи -- DNS/ASN сравнение RPC vs документированный testnet sequencer-эндпоинт ===")
    rpc_mainnet_host = "rpc.mainnet.chain.robinhood.com"
    rpc_testnet_host = "rpc.testnet.chain.robinhood.com"
    sequencer_testnet_host = "sequencer.testnet.chain.robinhood.com"
    sequencer_mainnet_guess_host = "sequencer.mainnet.chain.robinhood.com"

    dns_results = {}
    for label, host in [
        ("rpc_mainnet", rpc_mainnet_host),
        ("rpc_testnet", rpc_testnet_host),
        ("sequencer_testnet_documented", sequencer_testnet_host),
        ("sequencer_mainnet_UNCONFIRMED_GUESS", sequencer_mainnet_guess_host),
    ]:
        d = dns_resolve(host)
        if d.get("resolved"):
            d["ipinfo"] = {ip: ipinfo_lookup(ip) for ip in d["ips"][:2]}
            d["looks_like_cloudflare"] = any(ip.startswith(CLOUDFLARE_KNOWN_IPS_PREFIX) for ip in d["ips"])
        dns_results[label] = d
    out["write_path_dns"] = dns_results

    print("=== Живая интроспекция эндпоинтов (read-only JSON-RPC, без транзакций) ===")
    endpoint_probes = {
        "rpc_mainnet": probe_endpoint("rpc_mainnet", RPC_MAINNET),
        "rpc_testnet": probe_endpoint("rpc_testnet", RPC_TESTNET),
        "sequencer_testnet_documented": probe_endpoint("sequencer_testnet", SEQUENCER_TESTNET),
    }
    # Мейннет sequencer-эндпоинт -- пробуем ТОЛЬКО если DNS вообще
    # разрешился (иначе даже не пытаемся стучаться в несуществующий хост).
    if dns_results.get("sequencer_mainnet_UNCONFIRMED_GUESS", {}).get("resolved"):
        endpoint_probes["sequencer_mainnet_UNCONFIRMED_GUESS"] = probe_endpoint(
            "sequencer_mainnet_guess", SEQUENCER_MAINNET_GUESS
        )
    else:
        endpoint_probes["sequencer_mainnet_UNCONFIRMED_GUESS"] = {
            "note": "DNS не разрешился -- хост под этим именем не существует или не выставлен публично"
        }
    out["endpoint_probes"] = endpoint_probes

    print("=== 4. Timeboost для chain 4663 -- прямая проверка через rpc_modules + express-lane метод ===")
    out["timeboost_conclusion_note"] = (
        "Смотреть out.endpoint_probes.*.rpc_modules -- если ключ 'timeboost' присутствует "
        "в списке модулей, express lane ВКЛЮЧЁН на этом эндпоинте. Отдельно смотреть "
        "out.endpoint_probes.*.timeboost_sendExpressLaneTransaction_probe.json.error.code: "
        "-32601 ('method not found'/'the method ... does not exist') = модуль ОТСУТСТВУЕТ, "
        "Timeboost выключен на этом эндпоинте. Любая другая ошибка (невалидная подпись, "
        "неверный раунд и т.п.) = метод СУЩЕСТВУЕТ, Timeboost включён."
    )

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
