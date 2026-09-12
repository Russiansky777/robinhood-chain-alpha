#!/usr/bin/env bash
# test5_revert.sh — $5-тест отката: одна намеренно убыточная попытка executeCycle.
# Запуск на Ohio: curl -fsSL <raw>/scripts/test5_revert.sh | sudo -u bot bash
# Ворота: eth_call ДОЛЖЕН откатиться с InsufficientProfit (0xc39ba758) ИЛИ
# RepayShortfall (0xb95380e9) — иначе стоп, ничего не шлём. Владелец, 2026-09-12:
# RepayShortfall тоже валиден — цикл проходит обе своп-ноги, откатывается на
# проверке достаточности средств для repay (раньше проверки minProfit), капитал
# защищён так же, как при InsufficientProfit — то же семейство ожидаемого
# "безопасного отката", не баг.
# Ожидание: реальная tx со status=0, тот же селектор, баланс exitToken контракта не изменился.
set -euo pipefail

BRANCH="${BRANCH:-claude/nifty-sagan-r0polg}"
RAW="https://raw.githubusercontent.com/russiansky777/robinhood-chain-alpha/${BRANCH}"
CONTRACT="${CONTRACT:-0xeFBf06bCB9B0c6d0956c5bF149c0D9d9F34D5010}"
OWNER="${OWNER:-0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75}"
RPC_URL="${RPC_URL:-https://rpc.mainnet.chain.robinhood.com}"
SEQ_URL="${SEQ_URL:-https://sequencer.mainnet.chain.robinhood.com}"
CHAIN_ID="${CHAIN_ID:-4663}"
GAS_LIMIT="${GAS_LIMIT:-900000}"     # estimateGas на откатывающейся tx не работает — фикс с запасом
OUT="${OUT:-/home/bot/data/test5_revert_result.json}"

if [ -r /etc/bot/env ]; then set -a; . /etc/bot/env; set +a; fi
: "${PRIVATE_KEY_NOX:?PRIVATE_KEY_NOX не найден в /etc/bot/env}"

WORK="$(mktemp -d)"; cd "$WORK"
curl -fsSL "$RAW/data/task5_test_params.json" -o params.json
python3 -m venv v >/dev/null && . v/bin/activate && pip install -q web3 eth-account >/dev/null

PRIVATE_KEY_NOX="$PRIVATE_KEY_NOX" CONTRACT="$CONTRACT" OWNER="$OWNER" RPC_URL="$RPC_URL" SEQ_URL="$SEQ_URL" \
CHAIN_ID="$CHAIN_ID" GAS_LIMIT="$GAS_LIMIT" OUT="$OUT" python3 - <<'PY'
import json, os, time
from web3 import Web3
from eth_account import Account

SEL = {"0xc39ba758":"InsufficientProfit","0xc2221189":"UnexpectedCallback","0x37ed32e8":"ReentrantCall",
       "0x30cd7471":"NotOwner","0xb95380e9":"RepayShortfall"}
# Владелец, 2026-09-12: RepayShortfall принят как валидный результат наравне с
# InsufficientProfit — оба означают, что обе своп-ноги реально исполнились и
# откат случился только на защитной проверке (repay или minProfit), капитал
# не теряется сверх газа. Условие ворот/успеха теперь — принадлежность
# множеству, не равенство одному значению.
EXPECT = {"0xc39ba758", "0xb95380e9"}  # InsufficientProfit, RepayShortfall

rpc = Web3(Web3.HTTPProvider(os.environ["RPC_URL"], request_kwargs={"timeout": 15}))
seq = Web3(Web3.HTTPProvider(os.environ["SEQ_URL"], request_kwargs={"timeout": 10}))
acct = Account.from_key(os.environ["PRIVATE_KEY_NOX"])
contract = Web3.to_checksum_address(os.environ["CONTRACT"])
assert acct.address.lower() == os.environ["OWNER"].lower(), "ключ не совпадает с владельцем — СТОП"

p = json.load(open("params.json"))
calldata = p["calldata_hex"] if p["calldata_hex"].startswith("0x") else "0x"+p["calldata_hex"]
exit_token = Web3.to_checksum_address(p["cycle_params"]["exitToken"])
BAL_SEL = "0x70a08231" + "0"*24 + contract[2:].lower()   # balanceOf(contract)

def revert_selector(exc):
    d = getattr(exc, "data", None) or str(exc)
    if isinstance(d, dict): d = d.get("data", "") or ""
    d = str(d); i = d.find("0x")
    return d[i:i+10].lower() if i >= 0 else None

def bal():
    return int(rpc.eth.call({"to": exit_token, "data": BAL_SEL}).hex(), 16)

# --- ворота: симуляция ---
call = {"from": acct.address, "to": contract, "data": calldata, "gas": int(os.environ["GAS_LIMIT"])}
try:
    rpc.eth.call(call); sim_sel = None; sim = "NO_REVERT"
except Exception as e:
    sim_sel = revert_selector(e); sim = SEL.get(sim_sel, f"unknown:{sim_sel}")
print(json.dumps({"phase":"simulate","result":sim,"selector":sim_sel}), flush=True)
if sim_sel not in EXPECT:
    print(json.dumps({"ok":False,"stopped_before_send":True,
                       "reason":f"simulate returned {sim}, expected one of {sorted(EXPECT)} "
                                f"(InsufficientProfit/RepayShortfall)"}, indent=2))
    raise SystemExit(0)

# --- реальная отправка ---
bal_before = bal()
nonce = rpc.eth.get_transaction_count(acct.address, "pending")
base_fee = rpc.eth.get_block("latest").get("baseFeePerGas", rpc.eth.gas_price)
prio = int(1e8); max_fee = base_fee*2 + prio
tx = {"type":2,"chainId":int(os.environ["CHAIN_ID"]),"nonce":nonce,"to":contract,"value":0,
      "gas":int(os.environ["GAS_LIMIT"]),"maxFeePerGas":max_fee,"maxPriorityFeePerGas":prio,"data":calldata}
signed = acct.sign_transaction(tx)
raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction

t0 = time.time(); txh = None; endpoint = None
for label, w3 in (("sequencer", seq), ("rpc", rpc)):
    try: txh = w3.eth.send_raw_transaction(raw); endpoint = label; break
    except Exception as e: print(json.dumps({"phase":"send_fail","endpoint":label,"err":str(e)[:200]}), flush=True)
assert txh is not None, "отправка не удалась"

receipt = None; deadline = time.monotonic() + 60
while receipt is None and time.monotonic() < deadline:
    try: receipt = rpc.eth.get_transaction_receipt(txh)
    except Exception: pass
    if receipt is None: time.sleep(0.2)
assert receipt is not None, f"нет рецепта за 60с, tx={txh.hex()}"

# --- реплей причины на том же блоке ---
try:
    rpc.eth.call(call, block_identifier=receipt["blockNumber"]); rep_sel=None; rep="NO_REVERT_ON_REPLAY"
except Exception as e:
    rep_sel = revert_selector(e); rep = SEL.get(rep_sel, f"unknown:{rep_sel}")
bal_after = bal()

result = {
    "ok": receipt["status"]==0 and rep_sel in EXPECT and bal_after==bal_before,
    "tx_hash": txh.hex(), "block": receipt["blockNumber"], "status": receipt["status"],
    "gas_used": receipt["gasUsed"], "gas_cost_eth": receipt["gasUsed"]*receipt.get("effectiveGasPrice", max_fee)/1e18,
    "submit_endpoint": endpoint, "simulate_selector": sim, "replay_selector": rep,
    "exit_token": exit_token, "contract_balance_before": bal_before, "contract_balance_after": bal_after,
    "balance_unchanged": bal_after==bal_before, "t_total_s": round(time.time()-t0,3),
}
os.makedirs(os.path.dirname(os.environ["OUT"]), exist_ok=True)
open(os.environ["OUT"],"w").write(json.dumps(result, indent=2))
print(json.dumps(result, indent=2))
PY
