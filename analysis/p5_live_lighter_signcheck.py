#!/usr/bin/env python3
"""P5 LIVE -- проверка схемы ПОДПИСИ Lighter (elliottech/lighter-python,
`SignerClient`) ДО того, как она используется для реального хедж-ордера
в p5_live_step1.py. Ордеров не отправляет.

Зачем отдельный скрипт: SignerClient подписывает через скомпилированные
нативные библиотеки (ctypes, .so/.dylib/.dll внутри пакета) -- риск
поломки на конкретной ОС раннера GH Actions. Проверяем ДО того, как
эта же подпись понадобится сразу после реальной mint-транзакции (где
уже открыта незахеджированная позиция и retry дороже).

Проверка -- `SignerClient.check_client()`, реальный публичный метод SDK
(подтверждено WebFetch реального lighter/signer_client.py,
elliottech/lighter-python, 2026-09-03): по сигнатуре относится к
"Auth/Validation only" -- не создаёт и не отправляет ордер, только
проверяет валидность клиента/ключей (использует ту же нативную подпись,
что и create_market_order, так что успешный вызов -- реальное
доказательство, что подпись рабочая).

Секреты: LIGHTER_API_KEY_PUBLIC / LIGHTER_API_KEY_PRIVATE (владелец
добавил в GitHub Secrets) -- ТОЛЬКО из окружения, никогда не печатаются
и не пишутся в результат.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

OUT_PATH = Path("data/p3_guard_cache/p5_live_lighter_signcheck_result.json")
LIGHTER_API_BASE = "https://api.rh.lighter.xyz"
ACCOUNT_INDEX = 22012
API_KEY_INDEX = 4


def run() -> int:
    t0 = time.time()
    result: dict = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "account_index": ACCOUNT_INDEX, "api_key_index": API_KEY_INDEX,
        "orders_sent": False,
    }

    try:
        import lighter  # elliottech/lighter-python -- установлен через pip install git+... в workflow
        result["sdk_import_ok"] = True
        result["sdk_module_file"] = getattr(lighter, "__file__", None)
    except Exception as e:  # noqa: BLE001
        result["sdk_import_ok"] = False
        result["sdk_import_error"] = f"{type(e).__name__}: {e}"
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        print(f"[p5_live_lighter_signcheck] ИМПОРТ SDK НЕ УДАЛСЯ: {result['sdk_import_error']}")
        return 1

    priv = os.environ.get("LIGHTER_API_KEY_PRIVATE", "")
    pub = os.environ.get("LIGHTER_API_KEY_PUBLIC", "")
    if not priv or not pub:
        result["error"] = "LIGHTER_API_KEY_PRIVATE/PUBLIC не заданы в окружении"
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        print(f"[p5_live_lighter_signcheck] {result['error']}")
        return 1

    try:
        client = lighter.SignerClient(
            url=LIGHTER_API_BASE, account_index=ACCOUNT_INDEX,
            api_private_keys={API_KEY_INDEX: priv},
        )
        result["client_constructed_ok"] = True
    except Exception as e:  # noqa: BLE001
        result["client_constructed_ok"] = False
        result["construct_error"] = f"{type(e).__name__}: {e}"
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        print(f"[p5_live_lighter_signcheck] КОНСТРУКТОР SignerClient УПАЛ: {result['construct_error']}")
        return 1

    try:
        err = client.check_client()
        result["check_client_ok"] = err is None
        result["check_client_result"] = str(err) if err is not None else None
        print(f"[p5_live_lighter_signcheck] check_client() -> {err!r}")
    except Exception as e:  # noqa: BLE001
        result["check_client_ok"] = False
        result["check_client_error"] = f"{type(e).__name__}: {e}"
        print(f"[p5_live_lighter_signcheck] check_client() УПАЛ: {result['check_client_error']}")

    # Дополнительно -- create_auth_token_with_expiry: тоже [Auth/Validation
    # only] по сигнатуре реального SDK, реально прогоняет ту же нативную
    # подпись end-to-end (создаёт подписанный auth-токен), не отправляет
    # ордер и не требует сети (чисто локальная подпись).
    try:
        token = client.create_auth_token_with_expiry(api_key_index=API_KEY_INDEX)
        result["auth_token_created_ok"] = bool(token)
        result["auth_token_type"] = str(type(token))
        print(f"[p5_live_lighter_signcheck] create_auth_token_with_expiry() -> тип={type(token)}, "
              f"длина={len(token) if hasattr(token, '__len__') else 'n/a'}")
    except Exception as e:  # noqa: BLE001
        result["auth_token_created_ok"] = False
        result["auth_token_error"] = f"{type(e).__name__}: {e}"
        print(f"[p5_live_lighter_signcheck] create_auth_token_with_expiry() УПАЛ: {result['auth_token_error']}")

    try:
        client.close()
    except Exception:  # noqa: BLE001
        pass

    result["signing_scheme_verified"] = bool(
        result.get("check_client_ok") or result.get("auth_token_created_ok")
    )
    result["runtime_s"] = time.time() - t0
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[p5_live_lighter_signcheck] ИТОГ: signing_scheme_verified={result['signing_scheme_verified']}")
    print(f"[p5_live_lighter_signcheck] записано {OUT_PATH}")
    return 0 if result["signing_scheme_verified"] else 1


if __name__ == "__main__":
    raise SystemExit(run())
