#!/usr/bin/env bash
# Задача 5, живой бот -- владелец 2026-09-13: "чистый" замер времени
# включения, спецификация -- docs/TASK5_WRITEPATH_CLEAN_SPEC.md.
# Отличия от scripts/writepath_test.sh (см. спецификацию для полного
# разбора):
#   1. Момент включения берётся из фида секвенсера (wss://feed.mainnet...,
#      Nitro broadcaster, sequenceNumber == номер L2-блока по документации
#      chainstacklabs/robinhood-chain-sequencer-feed), не из локального
#      поллинга eth_getTransactionReceipt через Cloudflare-RPC.
#   2. Строго одна транзакция в полёте: nonce инкрементируется ТОЛЬКО
#      после подтверждённого включения текущей попытки; любая ошибка
#      отправки/таймаут/revert -- скрипт останавливается, не продолжает
#      слепо со старым nonce.
#   3. "Блок на момент отправки" -- последний реально виденный на фиде
#      номер блока непосредственно перед t_send, без интерполяции.
#
# Разовый диагностический запуск, НЕ часть бота -- ничего не запускается
# автоматически повторно, нет цикла. Реальная отправка -- копеечные
# (~$0.01) переводы владельца самому себе, тот же протокол/тот же
# приватный ключ, что уже используется writepath_test.sh -- не новый
# механизм подписи, не требует отдельного разрешения сверх уже данного.
#
# Запуск: bash scripts/writepath_test_clean.sh
# Переменные окружения (все опциональны):
#   BOT_ENV_FILE      -- путь к файлу с PRIVATE_KEY_NOX (по умолчанию /etc/bot/env)
#   N_ATTEMPTS        -- число попыток (по умолчанию 10)
#   VALUE_WEI         -- сумма перевода в wei (по умолчанию ~$0.01 при ETH~$2460)
#   LOCATION_LABEL    -- метка локации в результате (по умолчанию "nl")
#   RECEIPT_TIMEOUT_S -- таймаут ожидания рецепта, секунды (по умолчанию 30)
#   FEED_URL          -- WS фид секвенсера (по умолчанию wss://feed.mainnet.chain.robinhood.com)
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
export RECEIPT_TIMEOUT_S="${RECEIPT_TIMEOUT_S:-30}"
export FEED_URL="${FEED_URL:-wss://feed.mainnet.chain.robinhood.com}"

pip install -q --break-system-packages eth-account requests websockets 2>/dev/null \
    || pip3 install -q eth-account requests websockets 2>/dev/null \
    || pip install -q eth-account requests websockets

python3 - <<'PYEOF'
import asyncio
import json
import os
import sys
import threading
import time

RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
# Та же находка/оговорка, что writepath_test.sh: реально существует,
# принимает eth_sendRawTransaction, имя по аналогии с документированным
# testnet-эндпоинтом, не за Cloudflare (AWS us-east-2).
SEQUENCER_URL = "https://sequencer.mainnet.chain.robinhood.com"
FEED_URL = os.environ.get("FEED_URL", "wss://feed.mainnet.chain.robinhood.com")
CHAIN_ID = 4663
N_ATTEMPTS = int(os.environ.get("N_ATTEMPTS", "10"))
VALUE_WEI = int(os.environ.get("VALUE_WEI", "4065000000000"))
RECEIPT_TIMEOUT_S = float(os.environ.get("RECEIPT_TIMEOUT_S", "30"))
RECEIPT_POLL_INTERVAL_S = 0.3  # грубый опрос -- его собственное время НЕ входит в метрику
LOCATION_LABEL = os.environ.get("LOCATION_LABEL", "nl")

try:
    from eth_account import Account
    import requests
    import websockets
except ImportError as exc:
    print(json.dumps({"error": f"нужны пакеты eth-account/requests/websockets: {exc}"}))
    sys.exit(1)

PRIVATE_KEY = os.environ.get("PRIVATE_KEY_TASK5_WRITEPATH_TEST", "")
if not PRIVATE_KEY:
    print(json.dumps({"error": "PRIVATE_KEY_TASK5_WRITEPATH_TEST не задан в окружении -- отправка невозможна"}))
    sys.exit(1)

acct = Account.from_key(PRIVATE_KEY)
addr = acct.address
print(json.dumps({"phase": "start", "address": addr}), file=sys.stderr)


def rpc(method, params, url=RPC_URL):
    r = requests.post(url, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1}, timeout=15)
    r.raise_for_status()
    d = r.json()
    if "error" in d:
        raise RuntimeError(f"{method} -> {d['error']}")
    return d["result"]


class FeedListener:
    """Фоновая подписка на фид секвенсера в отдельном потоке со своим
    event loop -- держит t_wall_first_seen по каждому sequenceNumber
    (== номер L2-блока, см. docs/TASK5_WRITEPATH_CLEAN_SPEC.md, честная
    оговорка про источник этого утверждения). Не декодирует содержимое
    сообщений -- декодер L2-сообщений подтверждённо ненадёжен (0/2286 в
    прошлом раунде), здесь он и не нужен."""

    def __init__(self, feed_url: str):
        self.feed_url = feed_url
        self.t_wall_first_seen: dict[int, float] = {}
        self.diag = {"n_messages": 0, "n_reconnects": 0, "connected": False, "error": None}
        self._lock = threading.Lock()
        self._loop = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        for _ in range(100):  # ждём подключения до 10с
            if self.diag["connected"]:
                return
            time.sleep(0.1)

    def latest_block_before(self, t_epoch: float) -> int | None:
        with self._lock:
            seen = [(t, b) for b, t in self.t_wall_first_seen.items() if t <= t_epoch]
        if not seen:
            return None
        return max(seen)[1]

    def t_first_seen(self, block_number: int) -> float | None:
        with self._lock:
            return self.t_wall_first_seen.get(block_number)

    def _run(self):
        asyncio.run(self._listen_forever())

    async def _listen_forever(self):
        while True:
            try:
                async with websockets.connect(self.feed_url, open_timeout=10, close_timeout=5) as ws:
                    self.diag["connected"] = True
                    async for raw in ws:
                        t_wall = time.time()
                        self.diag["n_messages"] += 1
                        try:
                            payload = json.loads(raw)
                        except Exception:
                            continue
                        msgs = payload.get("messages") if isinstance(payload, dict) else None
                        if not msgs:
                            continue
                        for m in msgs:
                            seq = m.get("sequenceNumber")
                            if seq is None:
                                continue
                            with self._lock:
                                if seq not in self.t_wall_first_seen:
                                    self.t_wall_first_seen[seq] = t_wall
            except Exception as exc:
                self.diag["error"] = str(exc)
                self.diag["connected"] = False
                self.diag["n_reconnects"] += 1
                time.sleep(1.0)  # честная пауза перед реконнектом, не спамим


def wait_for_receipt(tx_hash: str, timeout_s: float):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            r = rpc("eth_getTransactionReceipt", [tx_hash], url=RPC_URL)
        except Exception:
            r = None
        if r is not None:
            return r
        time.sleep(RECEIPT_POLL_INTERVAL_S)
    return None


feed = FeedListener(FEED_URL)
feed.start()
if not feed.diag["connected"]:
    print(json.dumps({"error": "не удалось подключиться к фиду секвенсера за 10с", "feed_diag": feed.diag}))
    sys.exit(1)

nonce = int(rpc("eth_getTransactionCount", [addr, "pending"], url=RPC_URL), 16)
attempts = []

for i in range(N_ATTEMPTS):
    submit_url = RPC_URL if i % 2 == 0 else SEQUENCER_URL
    submit_label = "rpc_mainnet" if i % 2 == 0 else "sequencer_mainnet_guess"

    try:
        base_fee_hex = rpc("eth_getBlockByNumber", ["latest", False], url=RPC_URL)["baseFeePerGas"]
        base_fee = int(base_fee_hex, 16)
    except Exception:
        base_fee = int(rpc("eth_gasPrice", [], url=RPC_URL), 16)
    max_priority = int(1e8)
    max_fee = base_fee * 2 + max_priority

    tx = {
        "type": 2, "chainId": CHAIN_ID, "nonce": nonce, "to": addr, "value": VALUE_WEI,
        "gas": 21000, "maxFeePerGas": max_fee, "maxPriorityFeePerGas": max_priority, "data": b"",
    }
    signed = acct.sign_transaction(tx)
    raw_hex = signed.raw_transaction.hex() if hasattr(signed, "raw_transaction") else signed.rawTransaction.hex()
    if not raw_hex.startswith("0x"):
        raw_hex = "0x" + raw_hex

    t_send = time.time()
    block_before_send = feed.latest_block_before(t_send)
    try:
        tx_hash = rpc("eth_sendRawTransaction", [raw_hex], url=submit_url)
    except Exception as exc:
        # ОСТАНАВЛИВАЕМСЯ -- не отправляем следующую попытку тем же/угаданным
        # nonce поверх непризнанной ошибки (это и есть источник "очереди
        # транзакций одного nonce", которую этот скрипт обязан не повторить).
        attempts.append({"attempt": i, "submit_endpoint": submit_label, "error": f"send failed: {exc}"})
        break

    receipt = wait_for_receipt(tx_hash, RECEIPT_TIMEOUT_S)
    if receipt is None:
        attempts.append({
            "attempt": i, "submit_endpoint": submit_label, "tx_hash": tx_hash,
            "error": f"receipt не получен за {RECEIPT_TIMEOUT_S}с",
        })
        break  # честно останавливаемся -- не знаем, будет ли эта tx включена позже

    if int(receipt.get("status", "0x1"), 16) == 0:
        attempts.append({
            "attempt": i, "submit_endpoint": submit_label, "tx_hash": tx_hash,
            "error": "receipt status=0 (revert) -- останов, не трогаем nonce дальше",
        })
        break

    block_number_included = int(receipt["blockNumber"], 16)
    t_feed_seen = feed.t_first_seen(block_number_included)
    feed_lookup_status = "ok" if t_feed_seen is not None else "block_not_seen_on_feed"

    row = {
        "attempt": i,
        "submit_endpoint": submit_label,
        "tx_hash": tx_hash,
        "block_before_send": block_before_send,
        "block_number_included": block_number_included,
        "blocks_to_inclusion": (block_number_included - block_before_send) if block_before_send is not None else None,
        "t_send": t_send,
        "t_feed_seen_inclusion": t_feed_seen,
        "clean_inclusion_latency_ms": ((t_feed_seen - t_send) * 1000.0) if t_feed_seen is not None else None,
        "feed_lookup_status": feed_lookup_status,
    }
    attempts.append(row)
    nonce += 1  # инкремент ТОЛЬКО здесь, после подтверждённого включения
    time.sleep(0.5)


def summarize(rows, field):
    vals = [a[field] for a in rows if a.get(field) is not None]
    s = sorted(vals)
    if not s:
        return {"n": 0}
    return {
        "n": len(s),
        "median": s[len(s) // 2],
        "p90": s[int(len(s) * 0.9)] if len(s) > 1 else s[0],
        "min": min(s),
        "max": max(s),
    }


by_endpoint = {}
for ep in ("rpc_mainnet", "sequencer_mainnet_guess"):
    rows = [a for a in attempts if a.get("submit_endpoint") == ep]
    by_endpoint[ep] = {
        "clean_inclusion_latency_ms": summarize(rows, "clean_inclusion_latency_ms"),
        "blocks_to_inclusion": summarize(rows, "blocks_to_inclusion"),
    }

result = {
    "location_label": LOCATION_LABEL,
    "address": addr,
    "n_attempts_planned": N_ATTEMPTS,
    "n_attempts_completed": len(attempts),
    "attempts": attempts,
    "overall": {
        "clean_inclusion_latency_ms": summarize(attempts, "clean_inclusion_latency_ms"),
        "blocks_to_inclusion": summarize(attempts, "blocks_to_inclusion"),
    },
    "by_submit_endpoint": by_endpoint,
    "feed_diag": feed.diag,
    "note": (
        "clean_inclusion_latency_ms и blocks_to_inclusion заменяют старую lag_ms -- "
        "методология и честные оговорки в docs/TASK5_WRITEPATH_CLEAN_SPEC.md. "
        "feed_lookup_status='block_not_seen_on_feed' -- WS пропустил ровно нужный блок "
        "(реконнект), для этой попытки clean_inclusion_latency_ms=null, значение НЕ подставлено."
    ),
}
print(json.dumps(result, indent=2))
PYEOF
