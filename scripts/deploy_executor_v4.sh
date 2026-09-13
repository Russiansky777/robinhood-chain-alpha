#!/usr/bin/env bash
# deploy_executor.sh — разовый деплой ClosedCycleExecutorV4 на Robinhood Chain.
# Запуск на Ohio: curl -fsSL <raw-url>/scripts/deploy_executor.sh | sudo -u bot bash
# Читает PRIVATE_KEY_NOX из /etc/bot/env. Байткод берёт из репозитория (raw GitHub).
# Шлёт ОДНУ транзакцию создания контракта, ждёт рецепт, печатает адрес.
set -euo pipefail

BRANCH="${BRANCH:-claude/nifty-sagan-r0polg}"
RAW="https://raw.githubusercontent.com/russiansky777/robinhood-chain-alpha/${BRANCH}"
OWNER="${OWNER:-0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75}"
POOL_MANAGER="${POOL_MANAGER:-0x8366a39cc670b4001a1121b8f6a443a643e40951}"
RPC_URL="${RPC_URL:-https://rpc.mainnet.chain.robinhood.com}"
SEQ_URL="${SEQ_URL:-https://sequencer.mainnet.chain.robinhood.com}"
CHAIN_ID="${CHAIN_ID:-4663}"
OUT="${OUT:-/home/bot/data/deploy_result_v4.json}"

if [ -r /etc/bot/env ]; then set -a; . /etc/bot/env; set +a; fi
# Пункт 6 (внешнее ревью, третий раунд): деплой ОТДЕЛЬНЫМ кошельком
# пилота -- его ключ лежит в СВОЁМ файле (task5_v4_prepare_pilot_wallet.py),
# НЕ в /etc/bot/env. Явно источаем этот файл, ЕСЛИ он существует, ДО
# проверки обязательных переменных -- /etc/bot/env (PRIVATE_KEY_NOX)
# остаётся НЕТРОНУТЫМ для всех остальных задач.
PILOT_WALLET_ENV="${PILOT_WALLET_ENV:-/home/bot/data/task5_v4_pilot_wallet.env}"
if [ -r "$PILOT_WALLET_ENV" ]; then set -a; . "$PILOT_WALLET_ENV"; set +a; fi
DEPLOY_PRIVATE_KEY="${PRIVATE_KEY_TASK5_V4_PILOT:-${PRIVATE_KEY_NOX:-}}"
: "${DEPLOY_PRIVATE_KEY:?ни PRIVATE_KEY_TASK5_V4_PILOT (см. $PILOT_WALLET_ENV), ни PRIVATE_KEY_NOX не найдены}"

WORK="$(mktemp -d)"; cd "$WORK"
curl -fsSL "$RAW/contracts/build/ClosedCycleExecutorV4.bytecode.txt" -o bytecode.txt
python3 -m venv v >/dev/null && . v/bin/activate && pip install -q web3 eth-account >/dev/null

DEPLOY_PRIVATE_KEY="$DEPLOY_PRIVATE_KEY" OWNER="$OWNER" POOL_MANAGER="$POOL_MANAGER" RPC_URL="$RPC_URL" SEQ_URL="$SEQ_URL" CHAIN_ID="$CHAIN_ID" OUT="$OUT" python3 - <<'PY'
import json, os, time
from web3 import Web3
from eth_account import Account
from eth_abi import encode

rpc = Web3(Web3.HTTPProvider(os.environ["RPC_URL"], request_kwargs={"timeout": 15}))
seq = Web3(Web3.HTTPProvider(os.environ["SEQ_URL"], request_kwargs={"timeout": 10}))
acct = Account.from_key(os.environ["DEPLOY_PRIVATE_KEY"])
owner = Web3.to_checksum_address(os.environ["OWNER"])
pool_manager = Web3.to_checksum_address(os.environ["POOL_MANAGER"])
chain_id = int(os.environ["CHAIN_ID"])

assert acct.address.lower() == owner.lower(), f"ключ даёт {acct.address}, ожидался {owner} — СТОП"

bytecode = open("bytecode.txt").read().strip()
data = bytecode + encode(["address", "address"], [owner, pool_manager]).hex()

bal = rpc.eth.get_balance(acct.address)
nonce = rpc.eth.get_transaction_count(acct.address, "pending")
gas_est = rpc.eth.estimate_gas({"from": acct.address, "data": data})
gas_limit = int(gas_est * 1.25)
base_fee = rpc.eth.get_block("latest").get("baseFeePerGas", rpc.eth.gas_price)
prio = int(1e8)
max_fee = base_fee * 2 + prio
cost_eth = gas_limit * max_fee / 1e18
print(json.dumps({"phase":"pre","address":acct.address,"balance_eth":bal/1e18,"nonce":nonce,
                  "gas_estimate":gas_est,"gas_limit":gas_limit,"max_cost_eth":cost_eth}), flush=True)
assert bal > gas_limit * max_fee, "недостаточно ETH на газ деплоя"

tx = {"type":2,"chainId":chain_id,"nonce":nonce,"to":None,"value":0,"gas":gas_limit,
      "maxFeePerGas":max_fee,"maxPriorityFeePerGas":prio,"data":data}
signed = acct.sign_transaction(tx)
raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction

t0 = time.time(); endpoint = None; txh = None
for label, w3 in (("sequencer", seq), ("rpc", rpc)):
    try:
        txh = w3.eth.send_raw_transaction(raw); endpoint = label; break
    except Exception as e:
        print(json.dumps({"phase":"send_fail","endpoint":label,"err":str(e)[:200]}), flush=True)
assert txh is not None, "отправка не удалась ни через один путь"

receipt = None; deadline = time.monotonic() + 60
while time.monotonic() < deadline and receipt is None:
    try: receipt = rpc.eth.get_transaction_receipt(txh)
    except Exception: pass
    if receipt is None: time.sleep(0.2)
assert receipt is not None, f"рецепт не получен за 60с, tx={txh.hex()}"

addr = receipt.get("contractAddress")
ok = receipt["status"] == 1 and addr
code_len = len(rpc.eth.get_code(addr)) if addr else 0
result = {"ok": bool(ok), "contract_address": addr, "tx_hash": txh.hex(), "block": receipt["blockNumber"],
          "gas_used": receipt["gasUsed"], "submit_endpoint": endpoint, "deployed_code_bytes": code_len,
          "owner": owner, "pool_manager": pool_manager, "chain_id": chain_id, "t_total_s": round(time.time()-t0, 3)}
os.makedirs(os.path.dirname(os.environ["OUT"]), exist_ok=True)
open(os.environ["OUT"], "w").write(json.dumps(result, indent=2))
print(json.dumps(result, indent=2))
PY
