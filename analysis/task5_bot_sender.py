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

ПРАВКА 2026-09-13 (внешнее ревью V4-пилота, разрешено владельцем менять
именно этот файл для этих фиксов -- подпись/отправка ОСТАЮТСЯ здесь,
никакие внешние приблизительные расчёты их не обходят):

  "Подготовка транзакции, резерв и отправка должны быть согласованы...
  1. Подготовить конкретную транзакцию: chainId, адрес подписанта,
     nonce, to, calldata, value, gas limit и комиссии. 2. Проверить
     бюджет по её максимальной стоимости. 3. Подписать и сохранить
     локально вычисленный tx hash, nonce и данные попытки до первого
     сетевого обращения. 4. Отправить именно подготовленную
     транзакцию."

  Три новых метода (send_cycle ниже теперь построен НА НИХ, оставлен
  для обратной совместимости/самопроверки):
    - prepare_transaction_fields(...) -- строит ПОЛНОСТЬЮ конкретные
      поля транзакции (nonce читается ОДИН раз здесь, base_fee -- один
      реальный RPC-вызов), НЕ подписывает, НЕ шлёт. Вызывающий код
      (task5_v4_hotpath.py) проверяет бюджет по gas*maxFeePerGas ЭТИХ
      полей ПЕРЕД тем, как просить подписать -- нет смысла подписывать
      (и резервировать nonce) то, что бюджет не пропустит.
    - sign_prepared_transaction(...) -- подписывает уже построенные
      поля. tx_hash считается ЛОКАЛЬНО (signed.hash -- keccak RLP,
      БЕЗ единого сетевого обращения) -- известен ДО первой отправки,
      поэтому "нет tx_hash -- значит не ушла" для отправленной попытки
      больше НЕВОЗМОЖНО в принципе. ПРАВКА (третий раунд ревью,
      пункт 1): подпись САМА ПО СЕБЕ больше НЕ продвигает
      self.state.nonce (раньше продвигала сразу после подписи -- если
      что-то между подписью и надёжным сохранением попытки падало,
      nonce "сгорал" без единой сохранённой попытки на него). Теперь
      nonce продвигается ТОЛЬКО через confirm_nonce_used(), вызываемый
      ПОСЛЕ того, как рецепт подтвердил его реальный расход на цепи.
    - confirm_nonce_used(nonce) -- НОВОЕ: продвигает self.state.nonce
      ТОЛЬКО когда nonce реально подтверждён израсходованным (success
      ИЛИ revert -- оба тратят nonce). Идемпотентно.
    - describe_nonce_state() -- НОВОЕ: read-only сравнение локального
      счётчика nonce с реальным ончейн (pending и latest), БЕЗ
      автоматической резинхронизации -- расхождения объясняются
      вызывающим кодом, не скрываются.
    - submit_prepared(...) -- отправляет ИМЕННО эту уже подписанную
      raw-транзакцию (не строит и не подписывает заново). "already
      known"/"already imported" от узла -- НЕ ошибка, а подтверждение,
      что транзакция реально в мемпуле (например, с прошлого прогона
      процесса) -- считается отправленной, ждём рецепт по её (уже
      известному) хэшу так же, как в обычном пути.

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
# Пункт 6 (третий раунд): отдельный ключ отдельного кошелька пилота --
# НОВАЯ переменная окружения, ПРИОРИТЕТНАЯ, если задана; иначе -- ПРЕЖНЕЕ
# поведение (PRIVATE_KEY_NOX) БЕЗ ИЗМЕНЕНИЙ для всех остальных задач,
# которые её не задают. Ключ отдельного кошелька пилота хранится ТОЛЬКО
# на Ohio, в файле с ограниченными правами (см.
# task5_v4_prepare_pilot_wallet.py) -- никогда не в чате/коммитах/логах.
PRIVATE_KEY = os.environ.get("PRIVATE_KEY_TASK5_V4_PILOT") or os.environ.get("PRIVATE_KEY_NOX", "")

STOP_FILE = Path("/etc/bot/STOP")               # touch этот файл — бот перестаёт отправлять
# Пункт 6 (внешнее ревью, третий раунд): "отдельные файлы состояния
# nonce/бюджета/лока, чтобы не сталкивались с другими задачами". ПО
# УМОЛЧАНИЮ путь -- ТОТ ЖЕ, что и раньше (НИЧЕГО не меняется для уже
# существующих вызывающих кодов/других задач, использующих этот же
# адрес/ключ); НОВЫЙ отдельный кошелёк пилота получает СВОЙ путь через
# переменную окружения SENDER_STATE_FILE в СВОЁМ, отдельном launch-
# окружении -- не трогая общий sender_state.json остальных задач.
STATE_FILE = Path(os.environ.get("SENDER_STATE_FILE", "/home/bot/data/sender_state.json"))

MAX_DAILY_LOSS_USD = float(os.environ.get("SENDER_MAX_DAILY_LOSS_USD", "50"))
MAX_CONSECUTIVE_LOSSES = int(os.environ.get("SENDER_MAX_CONSECUTIVE_LOSSES", "3"))
RECEIPT_TIMEOUT_S = 15.0
RECEIPT_POLL_S = 0.05
PRIORITY_FEE_WEI = int(1e8)                     # 0.1 gwei — FCFS, приоритет не покупается

# Селекторы custom errors контракта (ClosedCycleExecutorV3.sol) -- реально
# вычислены (keccak-256, первые 4 байта сигнатуры), см.
# docs/TASK5_BOT_EXECUTOR_SPEC.md и CUSTOM_ERROR_SELECTORS в
# task5_bot_executor.py (тот же словарь, значения побайтово совпадают).
REVERT_SELECTORS: dict[str, str] = {
    "0xc39ba758": "InsufficientProfit",  # InsufficientProfit(uint256,uint256,uint256)
    "0xc2221189": "UnexpectedCallback",  # UnexpectedCallback(address)
    "0x37ed32e8": "ReentrantCall",       # ReentrantCall()
    "0x30cd7471": "NotOwner",            # NotOwner()
    "0xb95380e9": "RepayShortfall",      # RepayShortfall(uint256,uint256) -- ревизия владельца
    # 2026-09-12 после код-ревью (фикс UnexpectedCallback/zeroForOne). Селектор
    # перепроверен независимо (keccak256("RepayShortfall(uint256,uint256)")[:4]).
}


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
    # НОВОЕ: True, если рецепт НЕ был получен здесь (таймаут/сетевой сбой
    # ПОСЛЕ отправки в сеть) -- судьба транзакции ГЕНУИННО неизвестна
    # этому вызову, НЕ "не удалось" (внешнее ревью, пункт 3: "ответ
    # может потеряться после принятия"). tx_hash при этом ВСЕГДА известен
    # (посчитан локально при подписании, см. PreparedTx) -- вызывающий
    # код обязан доразрешить её по этому хэшу, не считать неотправленной.
    unresolved: bool = False


@dataclass
class PreparedTx:
    """Полностью подготовленная и ПОДПИСАННАЯ транзакция -- tx_hash
    посчитан ЛОКАЛЬНО (keccak RLP подписанной транзакции), БЕЗ единого
    сетевого обращения (внешнее ревью, пункт 3: "сохранить локально
    вычисленный tx hash, nonce и данные попытки до первого сетевого
    обращения"). Вызывающий код обязан сохранить to_context() ДО
    вызова submit_prepared -- это и есть "данные попытки", по которым
    восстанавливается судьба транзакции после рестарта."""
    tx_fields: dict
    raw_transaction: bytes
    tx_hash: str
    nonce: int
    chain_id: int
    from_address: str
    to: str
    gas_limit: int
    max_fee_per_gas: int
    max_priority_fee_per_gas: int
    value_wei: int

    def to_context(self) -> dict:
        """Сериализуемый контекст попытки -- ИМЕННО то, что нужно
        сохранить на диск до первого сетевого обращения (nonce, tx_hash,
        комиссии) -- НЕ включает raw_transaction/приватный ключ (внешнее
        ревью: "приватные ключи и подписанные raw-транзакции не выводить
        в отчёты и не коммитить")."""
        return {
            "tx_hash": self.tx_hash, "nonce": self.nonce, "chain_id": self.chain_id,
            "from_address": self.from_address, "to": self.to, "gas_limit": self.gas_limit,
            "max_fee_per_gas": self.max_fee_per_gas, "max_priority_fee_per_gas": self.max_priority_fee_per_gas,
            "value_wei": self.value_wei,
        }


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

    # ---------- отправка (двухфазно: подготовить -> [бюджет проверяет
    # вызывающий код] -> подписать -> отправить, см. докстринг модуля) ----------

    def prepare_transaction_fields(self, to: str, calldata: bytes, gas_limit: int, value_wei: int = 0) -> dict:
        """Шаг 1: строит ПОЛНОСТЬЮ конкретные поля транзакции (nonce,
        to, calldata, value, gas limit, комиссии) -- НЕ подписывает, НЕ
        шлёт. Единственный сетевой вызов здесь -- чтение base_fee (для
        честной оценки maxFeePerGas, не гадаем). Вызывающий код обязан
        проверить бюджет по gas_limit*maxFeePerGas ЭТИХ полей (шаг 2)
        ПЕРЕД тем, как звать sign_prepared_transaction (шаг 3, ниже)."""
        ok, why = self.can_send()
        if not ok:
            raise RuntimeError(f"blocked: {why}")
        try:
            base_fee = self.rpc.eth.get_block("latest")["baseFeePerGas"]
        except Exception:
            base_fee = self.rpc.eth.gas_price
        max_fee = base_fee * 2 + PRIORITY_FEE_WEI
        return {
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

    def sign_prepared_transaction(self, tx: dict) -> PreparedTx:
        """Шаг 3 (ПОСЛЕ проверки бюджета вызывающим кодом на полях из
        prepare_transaction_fields, шаг 2): подписывает. tx_hash --
        ЛОКАЛЬНО (signed.hash, keccak RLP), БЕЗ сетевого обращения --
        известен и сохраняем ДО первого сетевого обращения.

        ПРАВКА (внешнее ревью, третий раунд, пункт 1): подпись САМА ПО
        СЕБЕ НЕ продвигает self.state.nonce. Раньше это происходило
        здесь же -- если ЧТО-ТО между подписью и надёжным сохранением
        попытки (task5_v4_hotpath.py::begin_attempt) падало (например,
        чтение баланса), nonce оказывался молча "сожжён" без единой
        сохранённой попытки, ссылающейся на него. Теперь nonce
        продвигается ТОЛЬКО через confirm_nonce_used() -- вызывается
        ПОСЛЕ того, как ончейн-рецепт подтвердил, что именно этот nonce
        реально израсходован (успех ИЛИ откат -- оба тратят nonce на
        Ethereum). Реальная попытка ссылается на prepared.nonce и,
        пока она не подтверждена, self.state.nonce остаётся ТЕМ ЖЕ --
        повторная подготовка (после рестарта, до подтверждения) даст
        ТОТ ЖЕ nonce, не следующий, что и нужно для повторной отправки
        ТЕХ ЖЕ данных (см. sign_prepared_transaction, вызванный повторно
        с теми же tx_fields, детерминированно даёт тот же tx_hash)."""
        signed = self.account.sign_transaction(tx)
        raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
        tx_hash = (signed.hash.hex() if hasattr(signed, "hash") else Web3.keccak(raw).hex())
        if not tx_hash.startswith("0x"):
            tx_hash = "0x" + tx_hash
        return PreparedTx(
            tx_fields=tx, raw_transaction=raw, tx_hash=tx_hash, nonce=tx["nonce"], chain_id=tx["chainId"],
            from_address=self.address, to=tx["to"], gas_limit=tx["gas"], max_fee_per_gas=tx["maxFeePerGas"],
            max_priority_fee_per_gas=tx["maxPriorityFeePerGas"], value_wei=tx["value"],
        )

    def confirm_nonce_used(self, nonce: int) -> None:
        """Вызывать ТОЛЬКО когда РЕАЛЬНЫЙ рецепт подтвердил, что nonce
        действительно израсходован на цепи (status 0 ИЛИ 1 -- оба
        тратят nonce). Идемпотентно и защищено от отката назад:
        повторный вызов (после рестарта, тот же nonce уже учтён) ничего
        не меняет."""
        if nonce >= self.state.nonce:
            self.state.nonce = nonce + 1
            self.state.save()

    def describe_nonce_state(self) -> dict:
        """Пункт 1 (внешнее ревью, третий раунд): "проверять актуальность
        nonce при старте и объяснять обнаруженные расхождения" --
        read-only сравнение локального счётчика с реальным ончейн-nonce
        (НЕ резинхронизирует автоматически -- решение о резинхронизации
        осознанно оставлено вызывающему коду/владельцу)."""
        onchain_pending = self.rpc.eth.get_transaction_count(self.address, "pending")
        onchain_latest = self.rpc.eth.get_transaction_count(self.address, "latest")
        local = self.state.nonce
        return {
            "address": self.address, "local_nonce": local,
            "onchain_nonce_pending": onchain_pending, "onchain_nonce_latest": onchain_latest,
            "matches_pending": local == onchain_pending, "matches_latest": local == onchain_latest,
        }

    def submit_prepared(self, prepared: PreparedTx) -> SendResult:
        """Шаг 4: отправляет ИМЕННО подготовленную (prepared.raw_transaction)
        транзакцию -- не строит и не подписывает заново. tx_hash уже
        известен локально (prepared.tx_hash) -- SendResult несёт его
        ВСЕГДА, включая случай, когда сама сеть не подтвердила отправку
        (внешнее ревью, пункт 3: "локально вычисленный хеш должен быть
        известен до отправки"; "ответ может потеряться после принятия
        транзакции" -- нет понятия "не ушла", есть неопределённость)."""
        result = SendResult(ok=False, tx_hash=prepared.tx_hash, t_send=time.time())

        for label, w3 in (("sequencer", self.seq), ("rpc", self.rpc)):
            try:
                w3.eth.send_raw_transaction(prepared.raw_transaction)
                result.submit_endpoint = label
                break
            except Exception as exc:
                msg = str(exc).lower()
                if any(m in msg for m in ("already known", "already imported", "known transaction",
                                           "alreadyknown", "transaction already exists")):
                    # Узел уже видел ЭТУ ЖЕ (по хэшу) транзакцию -- НЕ
                    # ошибка, подтверждение, что она реально в мемпуле
                    # (например, с прошлого, прерванного прогона процесса).
                    result.submit_endpoint = label
                    result.error = f"{label}: already known -- реально в мемпуле, ждём рецепт по её хэшу"
                    break
                result.error = f"{label} send failed: {exc}"
                continue

        # tx_hash ИЗВЕСТЕН ВСЕГДА -- ждём рецепт по нему НЕЗАВИСИМО от
        # того, вернул ли send_raw_transaction ошибку на ОБОИХ путях:
        # сетевой сбой ПОСЛЕ того, как узел принял транзакцию, неотличим
        # локально от сбоя ДО принятия -- в обоих случаях транзакция
        # МОГЛА уйти в мемпул, единственный честный способ узнать --
        # спросить по хэшу.
        deadline = time.monotonic() + RECEIPT_TIMEOUT_S
        receipt = None
        while time.monotonic() < deadline:
            try:
                receipt = self.rpc.eth.get_transaction_receipt(prepared.tx_hash)
                if receipt is not None:
                    break
            except Exception:
                pass
            time.sleep(RECEIPT_POLL_S)

        result.t_receipt = time.time()
        if receipt is None:
            result.unresolved = True  # НЕИЗВЕСТНО -- не "провалилось"; вызывающий код обязан доразрешить
            result.error = result.error or "receipt timeout"
            return result

        result.block_number = receipt["blockNumber"]
        result.status = receipt["status"]
        result.gas_used = receipt["gasUsed"]
        result.ok = receipt["status"] == 1
        if not result.ok:
            result.revert_reason = self._classify_revert(prepared.tx_fields, receipt["blockNumber"])
        return result

    def send_cycle(self, to: str, calldata: bytes, gas_limit: int, value_wei: int = 0) -> SendResult:
        """Обратная совместимость (task5_bot_executor.py, V3-путь) --
        собирает три новых шага БЕЗ отдельной проверки бюджета между
        ними (та проверка -- ответственность вызывающего кода в
        task5_v4_hotpath.py, который использует
        prepare_transaction_fields/sign_prepared_transaction/
        submit_prepared напрямую и продвигает nonce через
        confirm_nonce_used ПОСЛЕ подтверждения, а не этот метод).

        ЭТОТ метод -- ради обратной совместимости с уже существующим
        вызывающим кодом (task5_bot_executor.py), который НЕ знает о
        confirm_nonce_used -- воспроизводит СТАРОЕ поведение (nonce
        продвигается сразу после подписи, до отправки/подтверждения),
        не меняем его логику молча."""
        try:
            tx = self.prepare_transaction_fields(to, calldata, gas_limit, value_wei)
        except RuntimeError as exc:
            return SendResult(ok=False, error=str(exc))
        prepared = self.sign_prepared_transaction(tx)
        self.state.nonce = prepared.nonce + 1
        self.state.save()
        return self.submit_prepared(prepared)

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
