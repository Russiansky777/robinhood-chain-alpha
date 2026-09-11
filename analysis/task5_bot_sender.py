"""
task5_bot_sender.py — модуль подписи и отправки для бота Задачи 5.

Подключение: в task5_bot_executor.py заменить NotImplementedError в _build_sign_send()
на вызов send_cycle(...) отсюда. Всё остальное (детекция, маршруты, телеметрия) — без изменений.

Что делает:
  1. Подписывает EIP-1559 транзакцию к контракту ClosedCycleExecutorV3 готовым calldata.
  2. Шлёт в sequencer-эндпоинт (прямой путь), при ошибке — в публичный RPC.
  3. Ведёт nonce локально, ресинхронизирует при расхождении.
  4. Ждёт рецепт через RPC (sequencer чтение не отдаёт).
  5. При status=0 делает eth_call-реплей на том же блоке, достаёт селектор custom error.
  6. Стоп-условия: файл /etc/bot/STOP, дневной убыток, N убытков подряд.

Зависимости: web3, eth-account (уже в venv бота).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from eth_account import Account
from web3 import Web3

# ---------- конфиг (переопределяется из task5_bot_config при импорте) ----------

CHAIN_ID = 4663
RPC_URL = os.environ.get("RH_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
SEQUENCER_URL = os.environ.get("RH_SEQUENCER_URL", "https://sequencer.mainnet.chain.robinhood.com")
PRIVATE_KEY = os.environ.get("PRIVATE_KEY_NOX", "")

STOP_FILE = Path("/etc/bot/STOP")               # touch этот файл — бот перестаёт отправлять
STATE_FILE = Path("/home/bot/data/sender_state.json")

MAX_DAILY_LOSS_USD = float(os.environ.get("SENDER_MAX_DAILY_LOSS_USD", "50"))
MAX_CONSECUTIVE_LOSSES = int(os.environ.get("SENDER_MAX_CONSECUTIVE_LOSSES", "3"))
RECEIPT_TIMEOUT_S = 15.0
RECEIPT_POLL_S = 0.05
PRIORITY_FEE_WEI = int(1e8)                     # 0.1 gwei — FCFS, приоритет не покупается

# Селекторы custom errors контракта. Заполнить из docs/TASK5_BOT_EXECUTOR_SPEC.md
# (там они уже вычислены). Формат: "0xabcdef12": "ИмяОшибки".
REVERT_SELECTORS: dict[str, str] = {}


@dataclass
class SendResult:
    ok: bool
    tx_hash: Optional[str] = None
    block_number: Optional[int] = None
    status: Optional[int] = None
    submit_endpoint: str = ""
    revert_reason: str = ""
    gas_used: Optional[int] = None
    t_send: float = 0.0
    t_receipt: float = 0.0
    error: str = ""


@dataclass
class SenderState:
    day: str = ""
    daily_loss_usd: float = 0.0
    consecutive_losses: int = 0
    nonce: Optional[int] = None
    halted: bool = False
    halt_reason: str = ""

    @classmethod
    def load(cls) -> "SenderState":
        if STATE_FILE.exists():
            try:
                return cls(**json.loads(STATE_FILE.read_text()))
            except Exception:
                pass
        return cls()

    def save(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(self.__dict__))


class Sender:
    def __init__(self) -> None:
        if not PRIVATE_KEY:
            raise RuntimeError("PRIVATE_KEY_NOX не задан в окружении")
        self.rpc = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 10}))
        self.seq = Web3(Web3.HTTPProvider(SEQUENCER_URL, request_kwargs={"timeout": 5}))
        self.account = Account.from_key(PRIVATE_KEY)
        self.address = self.account.address
        self.state = SenderState.load()
        self._roll_day()
        if self.state.nonce is None:
            self.state.nonce = self.rpc.eth.get_transaction_count(self.address, "pending")
            self.state.save()

    # ---------- стоп-условия ----------

    def _roll_day(self) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if self.state.day != today:
            self.state.day = today
            self.state.daily_loss_usd = 0.0
            self.state.save()

    def can_send(self) -> tuple[bool, str]:
        self._roll_day()
        if STOP_FILE.exists():
            return False, "STOP file present"
        if self.state.halted:
            return False, f"halted: {self.state.halt_reason}"
        if self.state.daily_loss_usd >= MAX_DAILY_LOSS_USD:
            return False, f"daily loss cap {MAX_DAILY_LOSS_USD} reached"
        if self.state.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            return False, f"{MAX_CONSECUTIVE_LOSSES} consecutive losses"
        return True, ""

    def record_outcome(self, pnl_usd: float) -> None:
        """Вызывать после каждой сделки с фактическим PnL (отрицательный = убыток, включая газ)."""
        if pnl_usd < 0:
            self.state.daily_loss_usd += -pnl_usd
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0
        ok, why = self.can_send()
        if not ok and not STOP_FILE.exists():
            self.state.halted = True
            self.state.halt_reason = why
        self.state.save()

    def reset_halt(self) -> None:
        self.state.halted = False
        self.state.halt_reason = ""
        self.state.consecutive_losses = 0
        self.state.save()

    # ---------- nonce ----------

    def _resync_nonce(self) -> None:
        self.state.nonce = self.rpc.eth.get_transaction_count(self.address, "pending")
        self.state.save()

    # ---------- отправка ----------

    def send_cycle(self, to: str, calldata: bytes, gas_limit: int, value_wei: int = 0) -> SendResult:
        ok, why = self.can_send()
        if not ok:
            return SendResult(ok=False, error=f"blocked: {why}")

        try:
            base_fee = self.rpc.eth.get_block("latest")["baseFeePerGas"]
        except Exception:
            base_fee = self.rpc.eth.gas_price
        max_fee = base_fee * 2 + PRIORITY_FEE_WEI

        tx = {
            "type": 2,
            "chainId": CHAIN_ID,
            "nonce": self.state.nonce,
            "to": Web3.to_checksum_address(to),
            "value": value_wei,
            "gas": gas_limit,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": PRIORITY_FEE_WEI,
            "data": calldata,
        }
        signed = self.account.sign_transaction(tx)
        raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction

        result = SendResult(ok=False, t_send=time.time())

        # 1) sequencer напрямую, 2) fallback на RPC
        tx_hash = None
        for label, w3 in (("sequencer", self.seq), ("rpc", self.rpc)):
            try:
                tx_hash = w3.eth.send_raw_transaction(raw)
                result.submit_endpoint = label
                break
            except Exception as exc:
                msg = str(exc).lower()
                if "nonce" in msg:
                    self._resync_nonce()
                    result.error = f"nonce mismatch, resynced to {self.state.nonce}"
                    return result
                result.error = f"{label} send failed: {exc}"
                continue

        if tx_hash is None:
            return result

        self.state.nonce += 1
        self.state.save()
        result.tx_hash = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)

        # ждём рецепт через RPC
        deadline = time.monotonic() + RECEIPT_TIMEOUT_S
        receipt = None
        while time.monotonic() < deadline:
            try:
                receipt = self.rpc.eth.get_transaction_receipt(tx_hash)
                if receipt is not None:
                    break
            except Exception:
                pass
            time.sleep(RECEIPT_POLL_S)

        result.t_receipt = time.time()
        if receipt is None:
            result.error = "receipt timeout"
            return result

        result.block_number = receipt["blockNumber"]
        result.status = receipt["status"]
        result.gas_used = receipt["gasUsed"]
        result.ok = receipt["status"] == 1

        if not result.ok:
            result.revert_reason = self._classify_revert(tx, receipt["blockNumber"])

        return result

    # ---------- разбор отката ----------

    def _classify_revert(self, tx: dict, block_number: int) -> str:
        """eth_call-реплей на том же блоке: рецепт даёт только status, селектор — отсюда."""
        call = {k: tx[k] for k in ("to", "data", "value") if k in tx}
        call["from"] = self.address
        try:
            self.rpc.eth.call(call, block_identifier=block_number)
            return "no_revert_on_replay"   # состояние уже изменилось — цена ушла
        except Exception as exc:
            data = getattr(exc, "data", None) or str(exc)
            if isinstance(data, dict):
                data = data.get("data", "") or ""
            data = str(data)
            hexpos = data.find("0x")
            if hexpos >= 0 and len(data) >= hexpos + 10:
                selector = data[hexpos:hexpos + 10].lower()
                return REVERT_SELECTORS.get(selector, f"unknown_selector:{selector}")
            return f"revert_unparsed:{data[:80]}"


# ---------- самопроверка на $0 (не шлёт, только подписывает) ----------

if __name__ == "__main__":
    s = Sender()
    ok, why = s.can_send()
    print(json.dumps({
        "address": s.address,
        "nonce": s.state.nonce,
        "can_send": ok,
        "why": why,
        "rpc_block": s.rpc.eth.block_number,
        "sequencer_reachable": bool(s.seq.provider),
    }, indent=2))
