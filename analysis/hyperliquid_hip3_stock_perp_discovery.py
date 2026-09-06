#!/usr/bin/env python3
"""«Спящие референсы», п.2 (Hyperliquid HIP-3 сток-перпы) -- РЕАЛЬНОЕ
обнаружение, не по памяти. HIP-3 позволяет билдерам разворачивать
изолированные perp-DEX'ы поверх Hyperliquid; список таких DEX'ов и их
универсум активов не были известны заранее в этой сессии -- узнаём
через реальный публичный `/info` эндпоинт (тот же хост, что уже
используется для фандинга -- `funding_historical_backfill.py`, POST
{"type": ...}, без ключей/аккаунта).

Стратегия (честная, без гадания форм ответа):
  1. `{"type": "perpDexs"}` -- предполагаемый реальный способ перечислить
     все развёрнутые perp-DEX'ы (основной + HIP-3). Если структура
     ответа не такая, как ожидалось -- сохраняем сырой ответ как есть,
     не подгоняем под ожидание.
  2. Для main-dex (`dex=""`) и КАЖДОГО имени из (1) -- `{"type": "meta",
     "dex": <name>}`, реальный универсум активов этого DEX (`universe`).
  3. Клиентская фильтрация универсума по ключевым словам/тикерам,
     похожим на акции (сверяем с уже известным реальным списком 37
     тикеров Lighter из `lighter_stock_perp_funding_result.json` --
     пересечение по имени, не одностороннее предположение) -- ТОЛЬКО
     как ориентир для человека при разборе результата, окончательное
     решение "это сток-перп или нет" -- за анализом сырых данных, не
     за этим скриптом.
  4. Если что-то из (1)-(3) не работает (400/404/неожиданная форма) --
     честно фиксируем это как отрицательный результат, не имитируем
     успех.

Ноль кредитов Dune -- чистый REST Hyperliquid API, только чтение."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

HYPERLIQUID_API_BASE = "https://api.hyperliquid.xyz"
HEADERS = {"User-Agent": "robinhood-chain-alpha-sleeping-refs/1.0", "Content-Type": "application/json"}
OUT_PATH = Path("data/p3_guard_cache/hyperliquid_hip3_stock_perp_discovery_result.json")
RETRY_ATTEMPTS = 4
RETRY_BACKOFF_S = 3.0

# Реальный список 37 тикеров Lighter -- для сверки пересечением, НЕ как источник истины
LIGHTER_SYMBOLS_PATH = Path("data/p3_guard_cache/lighter_stock_perp_funding_result.json")


def _post_info(body: dict) -> tuple[int, object]:
    last_exc = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            r = requests.post(f"{HYPERLIQUID_API_BASE}/info", headers=HEADERS,
                               data=json.dumps(body), timeout=30)
            if r.status_code >= 500:
                raise requests.exceptions.HTTPError(f"HTTP {r.status_code}")
            try:
                return r.status_code, r.json()
            except ValueError:
                return r.status_code, r.text[:2000]
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            time.sleep(RETRY_BACKOFF_S * (2 ** attempt))
    raise RuntimeError(f"не удалось получить ответ после {RETRY_ATTEMPTS} попыток: {last_exc}")


def load_lighter_symbols() -> set[str]:
    if not LIGHTER_SYMBOLS_PATH.exists():
        return set()
    d = json.loads(LIGHTER_SYMBOLS_PATH.read_text())
    return {r["symbol"].upper() for r in d.get("results", [])}


def run() -> int:
    out: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "steps": {}}
    lighter_symbols = load_lighter_symbols()
    print(f"[hip3_discovery] реальных тикеров Lighter для сверки пересечением: {len(lighter_symbols)}")

    # Шаг 1: перечислить все perp-DEX'ы (основной + HIP-3, если такой info-type реально существует)
    print("\n=== Шаг 1: {\"type\": \"perpDexs\"} ===")
    status, body = _post_info({"type": "perpDexs"})
    print(f"HTTP {status}")
    print(json.dumps(body, indent=2, ensure_ascii=False)[:3000])
    out["steps"]["perpDexs"] = {"status_code": status, "body": body}

    dex_names: list[str] = [""]  # "" -- основной (не-HIP3) perp dex, всегда пробуем
    if status == 200 and isinstance(body, list):
        for entry in body:
            if isinstance(entry, dict) and entry.get("name"):
                dex_names.append(entry["name"])
            elif entry is None:
                continue  # реально в ответе Hyperliquid первый элемент -- null для дефолтного dex
    else:
        print("!!! реальный ответ perpDexs НЕ является списком (или не HTTP 200) -- "
              "честно фиксируем, не гадаем структуру, работаем только с main-dex")
    dex_names = list(dict.fromkeys(dex_names))  # de-dup, сохраняя порядок
    print(f"\nреальных DEX'ов для дальнейшего опроса (включая main \"\"): {len(dex_names)} -- {dex_names}")

    # Шаг 2: универсум активов КАЖДОГО реального dex
    out["steps"]["meta_by_dex"] = {}
    stock_like_by_dex: dict[str, list[str]] = {}
    for dex in dex_names:
        label = dex if dex else "(main)"
        print(f"\n=== Шаг 2: {{\"type\": \"meta\", \"dex\": {dex!r}}} -- {label} ===")
        req_body = {"type": "meta"}
        if dex:
            req_body["dex"] = dex
        try:
            status, body = _post_info(req_body)
        except RuntimeError as e:
            print(f"ERROR: {e}")
            out["steps"]["meta_by_dex"][label] = {"error": str(e)}
            continue
        universe = body.get("universe", []) if isinstance(body, dict) else []
        names = [u.get("name") for u in universe if isinstance(u, dict) and u.get("name")]
        print(f"HTTP {status}, реальных активов в универсуме: {len(names)}")
        print(f"первые 20: {names[:20]}")
        out["steps"]["meta_by_dex"][label] = {"status_code": status, "n_assets": len(names),
                                               "asset_names": names}
        # Пересечение с реальным списком тикеров Lighter -- ориентир, не вердикт
        overlap = sorted(set(n.upper() for n in names) & lighter_symbols)
        if overlap:
            stock_like_by_dex[label] = overlap
            print(f"реальное пересечение с тикерами Lighter: {overlap}")
        time.sleep(0.3)

    out["stock_like_overlap_with_lighter"] = stock_like_by_dex

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[hip3_discovery] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
