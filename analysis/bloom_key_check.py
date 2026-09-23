#!/usr/bin/env python3
"""Шаг 0: проверка ключа Bloom. ТОЛЬКО ЧТЕНИЕ, до любого кода исполнителя.

Два запроса и ничего больше:
  GET /api/v1/ping     -- работает ли ключ, какой лимит запросов;
  GET /api/v1/wallets  -- есть ли в списке кошелёк исполнителя.

ГРАНИЦЫ, заданные владельцем и закреплённые в коде:
  * только GET: POST/PUT/PATCH/DELETE здесь физически не вызываются, и
    самопроверка доказывает это по исходному тексту;
  * ключ не печатается никогда и нигде -- ни в ошибке, ни в заголовках,
    ни в теле ответа: всё, что уходит наружу, прогоняется через scrub();
  * тела ответов целиком не печатаются: наружу идут имена полей и
    значения только тех, что не похожи на секрет.

Нет кошелька в списке -- скрипт говорит СТОП и выходит с ненулевым кодом:
дальше без владельца нельзя.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "bloom_key_check.json"

HOST = "https://eu.solana.bloombot.app"
PING_PATH = "/api/v1/ping"
WALLETS_PATH = "/api/v1/wallets"
EXECUTOR_WALLET = "4s87RRC2V2XAJD6R8U2dP8kQH99Z2wA6fg88ZVfV4j4N"

SECRET_LIKE = re.compile(r"key|secret|token|password|mnemonic|private|auth|bearer|seed",
                          re.I)
RATE_HEADERS = re.compile(r"rate.?limit|retry.?after|x-ratelimit|quota|remaining|reset", re.I)


def scrub(text: str, key: str) -> str:
    """Ключ не должен просочиться ни одним путём, включая текст ошибки."""
    if not key:
        return text
    out = text.replace(key, "<КЛЮЧ>")
    if len(key) > 8:
        out = out.replace(key[:8], "<КЛЮЧ>")
        out = out.replace(key[-8:], "<КЛЮЧ>")
    return out


def safe_fields(obj, глубина: int = 0) -> dict:
    """Имена полей всегда, значения -- только у непохожих на секрет."""
    if глубина > 2 or not isinstance(obj, dict):
        return {"тип": type(obj).__name__}
    out = {}
    for k, v in obj.items():
        if SECRET_LIKE.search(str(k)):
            out[k] = "<скрыто по имени поля>"
        elif isinstance(v, dict):
            out[k] = safe_fields(v, глубина + 1)
        elif isinstance(v, list):
            out[k] = f"список из {len(v)}"
        else:
            out[k] = v
    return out


def get(path: str, key: str, timeout: int = 30) -> dict:
    """ЕДИНСТВЕННЫЙ вид сетевого вызова во всём скрипте: GET, без тела."""
    url = HOST + path
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
    except requests.RequestException as exc:
        return {"путь": path, "код": None,
                "почему": scrub(f"{type(exc).__name__}: {exc}", key)[:300]}
    заголовки = {k: v for k, v in r.headers.items() if RATE_HEADERS.search(k)}
    out = {"путь": path, "код": r.status_code, "заголовки_про_лимит": заголовки}
    try:
        body = r.json()
    except ValueError:
        out["тело_не_json"] = scrub(r.text[:400], key)
        return out
    out["тело"] = body
    return out


def wallets_from(body) -> list:
    """Адреса кошельков из ответа, какой бы формы он ни был."""
    кандидаты = []
    if isinstance(body, list):
        кандидаты = body
    elif isinstance(body, dict):
        for k in ("wallets", "data", "result", "items"):
            v = body.get(k)
            if isinstance(v, list):
                кандидаты = v
                break
            if isinstance(v, dict) and isinstance(v.get("wallets"), list):
                кандидаты = v["wallets"]
                break
    адреса = []
    for it in кандидаты:
        if isinstance(it, str):
            адреса.append(it)
        elif isinstance(it, dict):
            for k in ("address", "publicKey", "pubkey", "wallet", "walletAddress"):
                if isinstance(it.get(k), str):
                    адреса.append(it[k])
                    break
    return адреса


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return

    key = os.environ.get("BLOOM_API_KEY", "").strip()
    отчёт = {"собрано_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "хост": HOST, "кошелёк_исполнителя": EXECUTOR_WALLET,
              "ГРАНИЦЫ": ["только GET", "ключ не печатается нигде",
                           "тела ответов целиком не печатаются"]}
    if not key:
        отчёт["итог"] = "СТОП: BLOOM_API_KEY пуст в окружении"
        OUT_PATH.write_text(json.dumps(отчёт, ensure_ascii=False, indent=2))
        print(json.dumps(отчёт, ensure_ascii=False, indent=2))
        raise SystemExit(2)
    отчёт["длина_ключа"] = len(key)

    ping = get(PING_PATH, key)
    отчёт["ping"] = {**{k: v for k, v in ping.items() if k != "тело"},
                      "поля_тела": safe_fields(ping.get("тело"))}
    print(f"[bloom] ping: HTTP {ping.get('код')}", flush=True)

    wl = get(WALLETS_PATH, key)
    адреса = wallets_from(wl.get("тело"))
    есть = EXECUTOR_WALLET in адреса
    отчёт["wallets"] = {**{k: v for k, v in wl.items() if k != "тело"},
                         "поля_тела": safe_fields(wl.get("тело")),
                         "кошельков_в_ответе": len(адреса),
                         "адреса": адреса,
                         "кошелёк_исполнителя_в_списке": есть}
    print(f"[bloom] wallets: HTTP {wl.get('код')}, кошельков {len(адреса)}, "
          f"наш в списке: {есть}", flush=True)

    ключ_работает = ping.get("код") == 200
    отчёт["ключ_работает"] = ключ_работает
    if not ключ_работает:
        отчёт["итог"] = (f"СТОП: ping ответил {ping.get('код')} -- ключ не подтверждён. "
                          "Дальше без владельца нельзя.")
    elif not есть:
        отчёт["итог"] = ("СТОП: ключ работает, но кошелька исполнителя в списке НЕТ. "
                          "Дальше без владельца нельзя.")
    else:
        отчёт["итог"] = "ок: ключ работает, кошелёк исполнителя в списке"
    OUT_PATH.write_text(json.dumps(отчёт, ensure_ascii=False, indent=2))
    print(json.dumps(отчёт, ensure_ascii=False, indent=2))
    if отчёт["итог"].startswith("СТОП"):
        raise SystemExit(3)


def self_test() -> None:
    checks = []

    def chk(n, ok, got=""):
        checks.append((n, bool(ok), got))

    src = Path(__file__).read_text()
    тело = src.split("def self_test")[0]
    for м in ("post", "put", "patch", "delete"):
        chk(f"нет requests.{м} в рабочей части", f"requests.{м}" not in тело.lower())
    chk("ровно один вид сетевого вызова -- requests.get",
        тело.count("requests.get") == 1, str(тело.count("requests.get")))
    chk("тело запроса не отправляется вообще",
        "json=" not in тело and "data=" not in тело)

    K = "abcdefghijklmnopqrstuvwxyz0123456789"
    chk("ключ вычищается из текста", K not in scrub(f"ошибка с ключом {K} внутри", K))
    chk("и начало ключа тоже", K[:8] not in scrub(f"хвост {K[:8]} тут", K))
    chk("и конец ключа тоже", K[-8:] not in scrub(f"хвост {K[-8:]} тут", K))
    chk("пустой ключ ничего не ломает", scrub("текст", "") == "текст")

    f = safe_fields({"plan": "pro", "apiKey": "СЕКРЕТ", "limits": {"rpm": 60},
                      "wallets": [1, 2, 3]})
    chk("значение секретного поля скрыто", f["apiKey"] == "<скрыто по имени поля>")
    chk("обычное значение видно", f["plan"] == "pro")
    chk("вложенное разбирается", f["limits"]["rpm"] == 60)
    chk("список сворачивается в длину", f["wallets"] == "список из 3")

    chk("адреса из списка строк", wallets_from(["A", "B"]) == ["A", "B"])
    chk("адреса из wallets/address",
        wallets_from({"wallets": [{"address": "A"}, {"publicKey": "B"}]}) == ["A", "B"])
    chk("адреса из data",
        wallets_from({"data": [{"walletAddress": "C"}]}) == ["C"])
    chk("незнакомая форма -- пусто, а не догадка", wallets_from({"что-то": 1}) == [])
    chk("наш кошелёк в константе тот самый",
        EXECUTOR_WALLET.startswith("4s87RRC2") and len(EXECUTOR_WALLET) == 44)

    bad = 0
    for n, ok, got in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {n}" + (f"  -> {got}" if got and not ok else ""))
        bad += (not ok)
    print(f"самопроверка проверки ключа Bloom: {len(checks) - bad}/{len(checks)} пройдено")
    if bad:
        raise SystemExit(f"самопроверка не пройдена: {bad} из {len(checks)}")


if __name__ == "__main__":
    main()
