#!/usr/bin/env python3
"""Подбивка: ИМЕНА секретов репозитория и проба RPC Shyft по ним.

ЗАЧЕМ. Проба 26.09 14:36Z: RPC Shyft отвечает 401 "Invalid API key" на оба
секрета, под которыми Shyft ходил 24.09 (GRPC_FEED_TOKEN = GRPC_FEED2_TOKEN --
это токен gRPC-фида, не ключ RPC). Передача велит имя ключа брать из настроек
репозитория и не выдумывать -- здесь оно и берётся: прогон отдаёт секреты
одним JSON в ALL_SECRETS, печатаются ТОЛЬКО ИМЕНА.

Каждый секрет, в имени которого есть SHYFT, пробуется как ключ RPC одним
getSlot. Значения не печатаются никогда. Только стандартная библиотека: этот
шаг не запускает сторонний код рядом со всеми секретами.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request


def проба(ключ: str) -> str:
    тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getSlot"}).encode()
    зап = urllib.request.Request("https://rpc.shyft.to?api_key=" + ключ, data=тело,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(зап, timeout=20) as от:
            j = json.loads(от.read().decode() or "{}")
            return "ok getSlot" if isinstance(j.get("result"), int) else "http 200 без result"
    except urllib.error.HTTPError as e:
        текст = re.sub(r"https?://\S+", "<узел>", e.read().decode(errors="replace")[:80])
        return f"http {e.code}: {текст.strip()}"
    except Exception as e:  # noqa: BLE001
        return type(e).__name__


def main() -> int:
    все = json.loads(os.environ.get("ALL_SECRETS") or "{}")
    имена = sorted(к for к in все if к != "github_token")
    print("имена секретов:", ", ".join(имена))
    for имя in имена:
        if "SHYFT" in имя.upper():
            print(f"проба RPC Shyft по {имя}: {проба(str(все[имя]).strip())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
