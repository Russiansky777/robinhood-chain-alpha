#!/usr/bin/env bash
# Задача 5, живой бот -- владелец 2026-09-12: "Скрипт замера с NL -- упакуй
# в готовый bash-файл, который сам читает /etc/bot/env, сам подставляет
# PRIVATE_KEY_NOX, сам ставит зависимости и сам запускает замер."
#
# Измеряет реальное время до включения копеечной (~$0.01) транзакции
# самому себе, ЧЕРЕДУЯ отправку между публичным RPC
# (rpc.mainnet.chain.robinhood.com, Cloudflare) и найденным в этой сессии
# sequencer-эндпоинтом (sequencer.mainnet.chain.robinhood.com, AWS
# us-east-2, НЕ Cloudflare) -- см. docs/PROJECT_STATE.md, раздел
# "гонка блока 0 = скорость записи". Рецепт всегда читается через RPC
# (sequencer-эндпоинт read-запросы не обслуживает, подтверждено
# отдельной интроспекцией). Одноразовый диагностический запуск, НЕ часть
# бота -- ничего не запускается автоматически повторно, нет цикла.
#
# Запуск: bash scripts/writepath_test.sh
# Переменные окружения (все опциональны):
#   BOT_ENV_FILE   -- путь к файлу с PRIVATE_KEY_NOX (по умолчанию /etc/bot/env)
#   N_ATTEMPTS     -- число попыток (по умолчанию 10)
#   VALUE_WEI      -- сумма перевода в wei (по умолчанию ~$0.01 при ETH~$2460)
#   LOCATION_LABEL -- метка локации в результате (по умолчанию "nl")
set -euo pipefail

ENV_FILE="${BOT_ENV_FILE:-/etc/bot/env}"
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
fi

if [ -z "${PRIVATE_KEY_NOX:-}" ]; then
    echo "ОШИБКА: PRIVATE_KEY_NOX не найден в $ENV_FILE и не задан в окружении -- отправка невозможна." >&2
    exit 1
fi

export PRIVATE_KEY_TASK5_WRITEPATH_TEST="$PRIVATE_KEY_NOX"
export N_ATTEMPTS="${N_ATTEMPTS:-10}"
export VALUE_WEI="${VALUE_WEI:-4065000000000}"
export LOCATION_LABEL="${LOCATION_LABEL:-nl}"

pip install -q --break-system-packages eth-account requests 2>/dev/null \
    || pip3 install -q eth-account requests 2>/dev/null \
    || pip install -q eth-account requests

python3 - <<'PYEOF'
import json, os, sys, time

RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
# Реальная находка этой сессии (DNS + JSON-RPC интроспекция, GH Actions,
# 2026-09-12): этот хост существует, отвечает на eth_sendRawTransaction
# (та же ошибка парсинга RLP на заведомо мусорном hex, что и на публичном
# RPC -- метод реально зарегистрирован), НЕ отвечает на eth_getTransactionCount
# ("method does not exist") -- минимальный write-only приёмный эндпоинт,
# НЕ за Cloudflare (AWS EC2 us-east-2, Columbus OH, ASN 16509, не 13335
# Cloudflare). Имя по аналогии с документированным testnet sequencer-
# эндпоинтом (blockrazor.io) -- поведение подтверждено в этой сессии.
SEQUENCER_URL = "https://sequencer.mainnet.chain.robinhood.com"
CHAIN_ID = 4663
N_ATTEMPTS = int(os.environ.get("N_ATTEMPTS", "10"))
VALUE_WEI = int(os.environ.get("VALUE_WEI", "4065000000000"))  # ~$0.01 @ ETH~$2460, честно приблизительно
POLL_INTERVAL_S = 0.05  # 50мс -- блок ~120мс, чаще опрашивать бессмысленно
LOCATION_LABEL = os.environ.get("LOCATION_LABEL", "nl")

try:
    from eth_account import Account
    import requests
except ImportError:
    print(json.dumps({"error": "нужны пакеты: pip install --break-system-packages eth-account requests"}))
    sys.exit(1)

PRIVATE_KEY = os.environ.get("PRIVATE_KEY_TASK5_WRITEPATH_TEST", "")
if not PRIVATE_KEY:
    print(json.dumps({"error": "PRIVATE_KEY_TASK5_WRITEPATH_TEST не задан в окружении -- отправка невозможна"}))
    sys.exit(1)

acct = Account.from_key(PRIVATE_KEY)
addr = acct.address
print(json.dumps({"phase": "start", "address": addr, "note": "перевод самому себе, копеечная сумма -- убедитесь, что на адресе есть ETH на газ"}), file=sys.stderr)


def rpc(method, params, url=RPC_URL):
    r = requests.post(url, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1}, timeout=15)
    r.raise_for_status()
    d = r.json()
    if "error" in d:
        raise RuntimeError(f"{method} -> {d['error']}")
    return d["result"]


def wait_for_receipt(tx_hash, timeout_s=30.0):
    # Рецепт -- всегда через RPC_URL: sequencer-эндпоинт подтверждённо не
    # обслуживает eth_getTransactionCount, по аналогии не ждём, что
    # обслужит и eth_getTransactionReceipt.
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            r = rpc("eth_getTransactionReceipt", [tx_hash], url=RPC_URL)
        except Exception:
            r = None
        if r is not None:
            return r
        time.sleep(POLL_INTERVAL_S)
    return None


nonce = int(rpc("eth_getTransactionCount", [addr, "pending"], url=RPC_URL), 16)
attempts = []

for i in range(N_ATTEMPTS):
    # Чередуем путь отправки: чётные попытки -- публичный RPC
    # (Cloudflare), нечётные -- предполагаемый прямой sequencer-эндпоинт
    # (не Cloudflare). Прямой ответ на вопрос "путь записи -- тот же
    # прокси или другой".
    submit_url = RPC_URL if i % 2 == 0 else SEQUENCER_URL
    submit_label = "rpc_mainnet" if i % 2 == 0 else "sequencer_mainnet_guess"
    try:
        base_fee_hex = rpc("eth_getBlockByNumber", ["latest", False], url=RPC_URL)["baseFeePerGas"]
        base_fee = int(base_fee_hex, 16)
    except Exception:
        base_fee = int(rpc("eth_gasPrice", [], url=RPC_URL), 16)
    max_priority = int(1e8)  # 0.1 gwei -- скромный tip, не соревнуемся за приоритет
    max_fee = base_fee * 2 + max_priority

    tx = {
        "type": 2,
        "chainId": CHAIN_ID,
        "nonce": nonce,
        "to": addr,
        "value": VALUE_WEI,
        "gas": 21000,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": max_priority,
        "data": b"",
    }
    signed = acct.sign_transaction(tx)
    raw_hex = signed.raw_transaction.hex() if hasattr(signed, "raw_transaction") else signed.rawTransaction.hex()
    if not raw_hex.startswith("0x"):
        raw_hex = "0x" + raw_hex

    t_send = time.time()
    try:
        tx_hash = rpc("eth_sendRawTransaction", [raw_hex], url=submit_url)
    except Exception as exc:
        attempts.append({"attempt": i, "submit_endpoint": submit_label, "error": f"send failed: {exc}"})
        break  # не долбим сеть повторными попытками при системной ошибке (например, неверный nonce)

    receipt = wait_for_receipt(tx_hash, timeout_s=30.0)
    t_included_local = time.time()
    if receipt is None:
        attempts.append({"attempt": i, "submit_endpoint": submit_label, "tx_hash": tx_hash, "error": "receipt не получен за 30с"})
        nonce += 1
        continue

    block_number = int(receipt["blockNumber"], 16)
    try:
        block = rpc("eth_getBlockByNumber", [receipt["blockNumber"], False], url=RPC_URL)
        block_ts = int(block["timestamp"], 16)
    except Exception:
        block_ts = None

    lag_ms = (t_included_local - t_send) * 1000.0
    attempts.append({
        "attempt": i,
        "submit_endpoint": submit_label,
        "tx_hash": tx_hash,
        "block_number": block_number,
        "block_timestamp": block_ts,
        "t_send": t_send,
        "t_included_local_poll": t_included_local,
        "lag_ms_send_to_local_poll_confirmation": lag_ms,
    })
    nonce += 1
    time.sleep(0.5)  # пауза между попытками, не спамим сеть


def summarize(rows):
    lags = [a["lag_ms_send_to_local_poll_confirmation"] for a in rows if "lag_ms_send_to_local_poll_confirmation" in a]
    s = sorted(lags)
    return {
        "n_included": len(s),
        "median_lag_ms": s[len(s) // 2] if s else None,
        "p90_lag_ms": s[int(len(s) * 0.9)] if s else None,
        "min_lag_ms": min(s) if s else None,
        "max_lag_ms": max(s) if s else None,
    }


by_endpoint = {
    "rpc_mainnet": summarize([a for a in attempts if a.get("submit_endpoint") == "rpc_mainnet"]),
    "sequencer_mainnet_guess": summarize([a for a in attempts if a.get("submit_endpoint") == "sequencer_mainnet_guess"]),
}
result = {
    "location_label": LOCATION_LABEL,
    "address": addr,
    "n_attempts": N_ATTEMPTS,
    "attempts": attempts,
    "overall": summarize(attempts),
    "by_submit_endpoint": by_endpoint,
    "note": (
        "lag_ms измеряет время от отправки до получения рецепта локальным поллингом (не блок-таймстемп) -- "
        "верхняя граница реального времени включения (добавляет ~0-50мс задержки опроса). "
        "Попытки чередуются между submit_endpoint='rpc_mainnet' (публичный Cloudflare-хост) и "
        "'sequencer_mainnet_guess' (https://sequencer.mainnet.chain.robinhood.com -- реально существует и "
        "принимает eth_sendRawTransaction, подтверждено в этой сессии, но имя не найдено в официальной "
        "документации напрямую -- по аналогии с документированным testnet sequencer-эндпоинтом). Сравните "
        "by_submit_endpoint.rpc_mainnet.median_lag_ms и .sequencer_mainnet_guess.median_lag_ms -- прямой ответ, "
        "даёт ли sequencer-путь реальное преимущество."
    ),
}
print(json.dumps(result, indent=2))
PYEOF
