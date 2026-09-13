#!/usr/bin/env python3
"""Задача 5, быстрый предварительный отбор: кэш состояния известных
пулов в памяти + дешёвый фильтр по направленным предельным ценам,
ДЛЯ ТОГО ЧТОБЫ перестать запускать полную сетку котировок для каждого
маршрута, задетого свопом.

Источник обновлений -- РЕАЛЬНЫЕ Swap-события PoolManager, уже
получаемые poll_once() (`data` события содержит sqrtPriceX96/liquidity/
tick/fee ПОСЛЕ свопа, `decode_v4_swap_log_data`, проверено побайтово).
Источник НАЧАЛЬНОГО состояния (владелец, "убери молчаливое ожидание
первого свопа") -- `extsload_pool_state()`: формула слота StateLibrary
(`stateSlot = keccak256(poolId || uint256(6))`, liquidity по
`stateSlot+3`) сверена WebFetch реального src/libraries/StateLibrary.sol
Uniswap/v4-core И перепроверена на реальном пуле (task5_v4_extsload_
slot0_pointcheck_result.json: extsload vs независимая котировка,
расхождение = 2.0001% -- ровно известный ским хука, не шум формулы).

ВАЖНО (владелец, п.6 уточнения): "долго не было свопа" и "есть пропуск
в наших данных" -- РАЗНЫЕ вещи. Редко торгуемый пул с состоянием
недельной давности МОЖЕТ быть полностью корректен (никто его не
трогал); мы бы ошибочно исключили его из фильтра по одному лишь
возрасту. Свежесть здесь определяется НЕ количеством блоков, а
"поколением" (`generation`) -- монотонный счётчик, растущий ТОЛЬКО
когда обнаружен ПОДТВЕРЖДЁННЫЙ пропуск сканирования логов (см.
`note_scan_gap`). Запись кэша пригодна для дешёвого фильтра, если её
собственное поколение (записанное в момент последнего обновления)
СОВПАДАЕТ с текущим -- т.е. с момента этого обновления НИ ОДНОГО
подтверждённого пропуска не произошло, сколько бы блоков ни прошло.

Активная ликвидность МОЖЕТ измениться без свопа (ModifyLiquidity,
события которого этот кэш пока НЕ сканирует -- честно НЕ решено в
этой правке). Поэтому `liquidity` в `PoolState` СОХРАНЯЕТСЯ (полезно
для диагностики/будущего), но `cheap_filter_route()` ниже её
СОЗНАТЕЛЬНО НЕ использует ни в одном условии допуска/отказа -- решение
принимается ТОЛЬКО по sqrtPriceX96/fee (эти двое ПОЛНОСТЬЮ и корректно
обновляются каждым Swap-событием, ModifyLiquidity на них не влияет).
Когда ModifyLiquidity начнёт отслеживаться -- это единственное место,
которое нужно будет пересмотреть."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

NATIVE_HOOKS = "0x0000000000000000000000000000000000000000"
POOLS_SLOT = 6
LIQUIDITY_OFFSET = 3


def _keccak256(data: bytes) -> bytes:
    from Crypto.Hash import keccak
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def pool_state_slot(pool_id_hex: str) -> bytes:
    """stateSlot = keccak256(abi.encodePacked(poolId, uint256(6))) --
    StateLibrary.sol, POOLS_SLOT=6, сверено WebFetch реального
    исходника Uniswap/v4-core в этой же сессии."""
    return _keccak256(bytes.fromhex(pool_id_hex[2:]) + POOLS_SLOT.to_bytes(32, "big"))


def extsload_pool_state(pool_id_hex: str, block_hex: str, rpc_call, pool_manager_address: str) -> dict:
    """Один холодный (cold) снимок slot0+liquidity РЕАЛЬНОГО пула через
    extsload -- та же формула, что уже точечно проверена
    (task5_v4_extsload_slot0_pointcheck.py) на реальном блоке против
    независимой котировки. `rpc_call` -- инъекция (обычно фоновый
    `_rpc_call`, НЕ торговый быстрый путь -- это фоновая, не торговая
    операция)."""
    selector = _keccak256(b"extsload(bytes32)")[:4].hex()
    state_slot = pool_state_slot(pool_id_hex)
    state_slot_hex = "0x" + state_slot.hex()
    liquidity_slot_hex = "0x" + (int.from_bytes(state_slot, "big") + LIQUIDITY_OFFSET).to_bytes(32, "big").hex()

    def _extsload(slot_hex: str) -> str:
        calldata = "0x" + selector + slot_hex[2:].rjust(64, "0")
        return rpc_call("eth_call", [{"to": pool_manager_address, "data": calldata}, block_hex])

    slot0_raw = _extsload(state_slot_hex)
    liq_raw = _extsload(liquidity_slot_hex)
    data = int(slot0_raw, 16)
    sqrt_price_x96 = data & ((1 << 160) - 1)
    tick_raw = (data >> 160) & ((1 << 24) - 1)
    tick = tick_raw - (1 << 24) if tick_raw >= (1 << 23) else tick_raw
    lp_fee = (data >> 208) & ((1 << 24) - 1)
    liquidity = int(liq_raw, 16) & ((1 << 128) - 1)
    return {"sqrt_price_x96": sqrt_price_x96, "tick": tick, "fee": lp_fee, "liquidity": liquidity}


# ПРАВКА (владелец, "используй ТОЛЬКО подтверждённую модель"): ЕДИНСТВЕННАЯ
# модель, отвечающая стандарту "не один пример" -- ТРОЙНОЙ независимой
# проверкой:
#   1) tx 0x658c2ab8... (block 0x3a694cb): hook_direct_transfer/amount1_gross = 2.0000%
#   2) tx 0x9976a38f... (block 0x3a69507): hook_direct_transfer/amount1_gross = 2.0000%
#   3) extsload(slot0) vs V4Quoter.quoteExactInputSingle на latest -- relative_diff=0.020001
# ВСЕ ТРИ -- ОДИН пул (ETH/MOSIAI, 0xf6562daa...), ОДНО направление
# (zero_for_one=True). Ключ -- (pool_id, zero_for_one), НЕ адрес хука
# (см. ниже -- тот же адрес хука на ДРУГОМ пуле даёт ДРУГУЮ ставку).
CONFIRMED_HOOK_MODELS: dict[tuple[str, bool], dict] = {
    ("0xf6562daa10e734d41846562b5f418f7833849643bf6c937eeeb0147b8ea94c2f", True): {
        "post_swap_output_skim_fraction": 0.02,
        "n_independent_confirmations": 3,
        "note": "2.00% скимается хуком 0xe5e70264... ПОСЛЕ свопа с amount1 (MOSIAI-выход), "
                "направление ETH->MOSIAI, пул ETH/MOSIAI.",
    },
    # ПРАВКА (владелец, п.4 предыдущего уточнения: "сравнение 2% от выхода
    # плеча с 40% от прибыли цикла не доказывает различие моделей --
    # посчитай комиссию ОТНОСИТЕЛЬНО ВАЛОВОГО ВЫХОДА именно хукнутого
    # плеча"): исправленный расчёт по УЖЕ сохранённым данным round11 для
    # tx 0x878fb998... (пул 0xfaf0d409..., ETH/0x35a79120, ТОТ ЖЕ адрес
    # хука 0xe5e70264..., направление token-in/ETH-out, zero_for_one=False):
    #   leg_gross_output (amount0, ETH-выход ЭТОГО плеча) = 4097638988924600
    #   hook_share_wei (competitor_profit_breakdown)      = 118831530678813
    #   118831530678813 / 4097638988924600 = 0.029000 -- ПРОВЕРЕНО согласием:
    #   amount0 - hook_share_wei = 3978807458245787 == our_bot_at_competitor_
    #   size.quote.amount_out (реальная котировка ТЕКУЩИМ V4Quoter).
    # ВТОРОЕ, НЕЗАВИСИМОЕ подтверждение (task5_v4_faf0d409_hook_second_
    # confirmation_result.json): extsload(slot0) vs V4Quoter на latest,
    # ТО ЖЕ направление (token->ETH) -- implied_skim_fraction=0.0290000019504,
    # т.е. те же 2.9000% другим методом на другом блоке. Два независимых
    # метода сошлись -- тот же стандарт, что и у модели выше (не один пример).
    ("0xfaf0d4093602eb2d7f80ce7ba50cffaeece9c5d546b6df363107779e5ff553aa", False): {
        "post_swap_output_skim_fraction": 0.029,
        "n_independent_confirmations": 2,
        "note": "2.90% скимается ТЕМ ЖЕ адресом хука 0xe5e70264..., но на ДРУГОМ пуле (ETH/0x35a79120) "
                "и в направлении token->ETH -- ставка ДРУГАЯ, чем у модели ETH/MOSIAI выше (2.00%); "
                "подтверждено 1 реальным tx (0x878fb998...) + 1 независимым extsload/quote на latest.",
    },
}


@dataclass
class PoolState:
    sqrt_price_x96: int
    liquidity: int
    tick: int
    realized_fee: int  # честный fee ИЗ Swap-события/extsload (может отличаться от статического PoolKey.fee)
    last_block: int
    last_log_index: int  # -1 -- холодная инициализация (extsload), НЕ реальное событие
    confirmed_through_generation: int


class PoolStateCache:
    """Свежие Swap-события применяются пакетом (владелец, п.2: "сначала
    примени ВЕСЬ упорядоченный пакет событий к кэшу, затем ОДИН раз
    оцени затронутые маршруты") -- apply_swap_batch сортирует пакет по
    (blockNumber, logIndex) и применяет целиком до всякой оценки.

    Холодная инициализация (extsload, `seed_cold`) применяется ЧЕРЕЗ
    ТОТ ЖЕ (blockNumber, logIndex=-1) механизм упорядочивания -- если
    к моменту, когда фон дочитал холодный снимок пула X на блоке B,
    детектор УЖЕ успел применить РЕАЛЬНОЕ более новое Swap-событие
    (задача 1 владельца: "торговый старт не блокируй" -- фон и детектор
    работают ОДНОВРЕМЕННО), холодный снимок просто НЕ перезапишет более
    свежие данные (та же проверка `(block, log_index) <= prev`, что и
    для обычных событий) -- "корректное применение последующих
    событий" гарантируется тем, что это ОДИН и тот же код, а не два
    параллельных, которые могут разъехаться.

    `generation` (п.6 владельца, "долго не было свопа" != "есть
    пропуск") -- растёт ТОЛЬКО по note_scan_gap(); is_fresh_for_fast_
    filter не участвует -- вместо неё PoolState.confirmed_through_
    generation сравнивается с cache.generation напрямую в
    cheap_filter_route()."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, PoolState] = {}
        self.generation = 0

    def note_scan_gap(self, reason: str) -> None:
        """Вызывается ТОЛЬКО когда сканирование логов реально не
        удалось/не гарантировано непрерывно (см. HotPath.poll_once) --
        НЕ на каждый пустой eth_getLogs (владелец, п.5: "успешный
        пустой ответ сам по себе не является пропуском")."""
        with self._lock:
            self.generation += 1
            print(f"[pool-cache] обнаружен пропуск сканирования (поколение -> {self.generation}): {reason}")

    def apply_swap_batch(self, decoded_events: list[dict]) -> set[str]:
        touched = set()
        with self._lock:
            gen = self.generation
            for ev in sorted(decoded_events, key=lambda e: (e["block_number"], e["log_index"])):
                pid = ev["pool_id"].lower()
                prev = self._states.get(pid)
                if prev is not None and (ev["block_number"], ev["log_index"]) <= (prev.last_block, prev.last_log_index):
                    continue
                self._states[pid] = PoolState(
                    sqrt_price_x96=ev["sqrt_price_x96"], liquidity=ev["liquidity"], tick=ev["tick"],
                    realized_fee=ev["fee"], last_block=ev["block_number"], last_log_index=ev["log_index"],
                    confirmed_through_generation=gen,
                )
                touched.add(pid)
        return touched

    def seed_cold(self, pool_id: str, block_number: int, state: dict) -> bool:
        """Холодная инициализация одного пула из extsload (см.
        `extsload_pool_state`). Возвращает True, если реально применено
        (не было более свежих данных)."""
        return bool(self.apply_swap_batch([{
            "pool_id": pool_id, "block_number": block_number, "log_index": -1,
            "sqrt_price_x96": state["sqrt_price_x96"], "liquidity": state["liquidity"],
            "tick": state["tick"], "fee": state["fee"],
        }]))

    def mark_stale(self, pool_ids: list[str]) -> None:
        with self._lock:
            for pid in pool_ids:
                self._states.pop(pid.lower(), None)

    def get(self, pool_id: str) -> PoolState | None:
        with self._lock:
            return self._states.get(pool_id.lower())

    def is_initialized(self, pool_id: str) -> bool:
        return self.get(pool_id) is not None

    def is_fresh(self, pool_id: str) -> bool:
        """п.6: свежесть = "с момента последнего обновления НЕ было
        подтверждённого пропуска сканирования" -- НЕ функция возраста в
        блоках. Редко торгуемый пул, чьё состояние не менялось 10000
        блоков БЕЗ единого пропуска, -- свеж; пул, обновлённый минуту
        назад, но ПОСЛЕ которого случился пропуск, -- не свеж."""
        state = self.get(pool_id)
        if state is None:
            return False
        with self._lock:
            return state.confirmed_through_generation == self.generation


@dataclass
class CheapFilterResult:
    verdict: str  # "passed" | "rejected_by_price" | "not_initialized" | "hook_model_absent"
    covered: bool
    implied_product: float | None = None
    uncovered_pool_ids: list[str] = field(default_factory=list)
    detail: str = ""


def cheap_filter_route(route, cache: PoolStateCache, current_block: int) -> CheapFilterResult:
    """Дешёвый фильтр: произведение направленных предельных цен с
    комиссиями по всем плечам маршрута -- ОЦЕНКА расхождения на границе,
    НЕ обещание прибыли на конечном размере.

    Решение использует ТОЛЬКО sqrt_price_x96 и realized_fee -- `liquidity`
    из PoolState здесь НЕ читается ни разу (см. докстринг модуля, п.6
    владельца) -- ModifyLiquidity пока не отслеживается, значит доверять
    liquidity в РЕШЕНИИ было бы нечестно."""
    uncovered = []
    product = 1.0
    for leg in route.legs:
        pid = leg.pool_id_hex.lower()
        if not cache.is_fresh(pid):
            return CheapFilterResult(verdict="not_initialized", covered=False, uncovered_pool_ids=[pid],
                                      detail=f"пул {pid} не инициализирован или с последнего обновления "
                                             f"был подтверждённый пропуск сканирования логов")
        state = cache.get(pid)

        is_hookless = leg.hooks.lower() == NATIVE_HOOKS
        model = None if is_hookless else CONFIRMED_HOOK_MODELS.get((pid, leg.zero_for_one))
        if not is_hookless and model is None:
            uncovered.append(pid)
            continue

        raw_price = (state.sqrt_price_x96 / (2 ** 96)) ** 2
        fee_frac = state.realized_fee / 1_000_000.0
        if leg.zero_for_one:
            leg_rate = raw_price * (1.0 - fee_frac)
        else:
            leg_rate = (1.0 / raw_price) * (1.0 - fee_frac) if raw_price else 0.0
        if model is not None:
            leg_rate *= (1.0 - model["post_swap_output_skim_fraction"])
        product *= leg_rate

    if uncovered:
        return CheapFilterResult(verdict="hook_model_absent", covered=False, uncovered_pool_ids=uncovered,
                                  detail="хотя бы одно плечо -- хук без подтверждённой модели именно для "
                                         "этого пула/направления")

    passed = product > 1.0
    return CheapFilterResult(
        verdict="passed" if passed else "rejected_by_price", covered=True, implied_product=product,
        detail=f"произведение направленных предельных цен с комиссией = {product:.6f} "
               f"({'>' if passed else '<='} 1 -- {'похоже на расхождение' if passed else 'похоже, невыгодно'}, "
               f"НЕ гарантия прибыли на конечном размере)")
