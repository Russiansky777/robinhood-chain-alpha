#!/usr/bin/env python3
"""Задача 5, быстрый предварительный отбор (владелец, уточнение перед
реализацией): кэш состояния известных пулов в памяти + дешёвый
фильтр по направленным предельным ценам, ДЛЯ ТОГО ЧТОБЫ перестать
запускать полную сетку котировок для каждого маршрута, задетого
свопом.

Источник состояния -- РЕАЛЬНЫЕ Swap-события PoolManager, уже
получаемые poll_once() (`_chunked_get_logs`) -- `data` этого события
УЖЕ содержит sqrtPriceX96/liquidity/tick/fee ПОСЛЕ свопа
(`decode_v4_swap_log_data`, task5_v4_pool_math.py, проверено
побайтово на реальном событии) -- раньше это поле просто
выбрасывалось (`poll_once` брал только `topics[1]`).

ВАЖНО (владелец, пункт 5 этого уточнения): Swap-события НЕ покрывают
ВСЕ изменения состояния -- активная ликвидность пула может измениться
БЕЗ свопа (ModifyLiquidity). Этот модуль СЕЙЧАС отслеживает только
Swap-события (ModifyLiquidity НЕ сканируется -- честно ограничение,
см. `PoolState.is_fresh_for_fast_filter`, а НЕ "кэш = истина"). Чтобы
не выдавать потенциально устаревшую liquidity за актуальную без
компенсации, у каждой записи есть `max_age_blocks` -- ПОСЛЕ этого
кэш считается непригодным для дешёвого фильтра (маршрут уходит в
полную сетку/очередь пересмотра, НЕ отфильтровывается по устаревшим
числам). Сам порог -- консервативная защита, НЕ замена реальному
отслеживанию ModifyLiquidity (это отдельная, ещё не сделанная работа
-- см. докстринг класса ниже).

Пустой ответ eth_getLogs НЕ считается подтверждением "состояние не
изменилось" (владелец, пункт 5) -- он лишь означает "свопов не было";
он НЕ продлевает свежесть кэша сам по себе -- продлевает её ТОЛЬКО
реальное новое Swap-событие ЛИБО отдельный, помеченный явно
"подтверждено фоном" полный пересчёт (сейчас не реализован, см. TODO
в PoolStateCache.mark_confirmed_fresh)."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

NATIVE_HOOKS = "0x0000000000000000000000000000000000000000"

# ПРАВКА (владелец, "используй ТОЛЬКО подтверждённую модель, процент по
# одному примеру не считай постоянной моделью"): confirmed ТРОЙНОЙ
# независимой проверкой --
#   1) tx 0x658c2ab8... (block 0x3a694cb): hook_direct_transfer/amount1_gross = 2.0000%
#   2) tx 0x9976a38f... (block 0x3a69507): hook_direct_transfer/amount1_gross = 2.0000%
#   3) свежий extsload(slot0) К vs V4Quoter.quoteExactInputSingle на latest --
#      relative_difference = 0.020001 (см. task5_v4_extsload_slot0_pointcheck_result.json)
# ВСЕ ТРИ -- ОДИН И ТОТ ЖЕ пул (ETH/MOSIAI, 0xf6562daa...) и ОДНО И ТО ЖЕ
# направление (zero_for_one=True, ETH-in/MOSIAI-out). Модель НЕ переносится
# ни на другое направление ЭТОГО ЖЕ пула (не проверено), ни на другой пул
# С ТЕМ ЖЕ адресом хука -- контрольный пример (round11, tx 0x878fb998...)
# показал на пуле 0xfaf0d409... (ETH/0x35a79120, тот же хук 0xe5e70264...)
# СОВЕРШЕННО ИНУЮ экономику (~40% доли от прибыли цикла, НЕ 2% от объёма
# плеча) -- один и тот же адрес хука ведёт себя по-разному на разных
# пулах. Ключ ниже -- (pool_id, zero_for_one), НЕ адрес хука.
CONFIRMED_HOOK_MODELS: dict[tuple[str, bool], dict] = {
    ("0xf6562daa10e734d41846562b5f418f7833849643bf6c937eeeb0147b8ea94c2f", True): {
        "post_swap_output_skim_fraction": 0.02,
        "n_independent_confirmations": 3,
        "note": "2.00% скимается хуком 0xe5e70264... ПОСЛЕ свопа с amount1 (MOSIAI-выход) в "
                "направлении ETH->MOSIAI на пуле ETH/MOSIAI. Другое направление/другой пул -- НЕ покрыты.",
    },
}


@dataclass
class PoolState:
    sqrt_price_x96: int
    liquidity: int
    tick: int
    realized_fee: int  # честный fee ИЗ Swap-события (может отличаться от статического PoolKey.fee -- см. ниже)
    last_block: int
    last_log_index: int
    # ПРАВКА (найдено этой же сессией, task5_v4_control_tx_pool_hooks_lookup.py /
    # аудит control tx 0x878fb998...): реализованный fee ETH/USDG-пула (0x24107d15...,
    # hookless) в Swap-событии = 125 -- СТАТИЧЕСКИЙ PoolKey.fee из Initialize-события
    # = 100. Расхождение подтверждено ТРЁМЯ независимыми свопами (2 control tx
    # аудита хука + сама tx 0x878fb998...), не единичный шум. Причина не установлена
    # (protocolFee? другая компонента?) -- ПОЭТОМУ дешёвый фильтр обязан использовать
    # realized_fee ИЗ КЭША (когда есть), а НЕ PoolKey.fee -- см. cheap_filter_route().

    def is_fresh_for_fast_filter(self, current_block: int, max_age_blocks: int) -> bool:
        """Консервативная защита п.5 (владелец): НЕ полноценное отслеживание
        ModifyLiquidity (см. докстринг модуля) -- просто отказ доверять
        дешёвому фильтру, если с последнего РЕАЛЬНОГО Swap-события по
        этому пулу прошло слишком много блоков (за это время liquidity
        могла измениться add/removeLiquidity-действием, событие которого
        этот кэш пока не видит)."""
        return (current_block - self.last_block) <= max_age_blocks


# Порог заведомо консервативный (не откалиброван по реальной частоте
# ModifyLiquidity этой цепи -- отдельная задача); лучше слишком часто
# уйти в полную сетку, чем один раз довериться устаревшей liquidity.
DEFAULT_MAX_AGE_BLOCKS = 200


class PoolStateCache:
    """Свежие Swap-события применяются пакетом (владелец, п.2:
    "сначала примени весь полученный упорядоченный пакет событий к
    кэшу, затем один раз оцени затронутые маршруты") -- apply_swap_batch
    принимает уже отсортированный (по blockNumber, logIndex) список
    декодированных событий ОДНОГО вызова eth_getLogs и применяет их
    ВСЕ, прежде чем что-либо возвращает; отдельные mark() на один лог
    здесь намеренно НЕТ, чтобы не создать соблазн оценивать маршрут
    между применением логов одной и той же обработанной пачки (то же
    самое требование, что "не создавай кандидатов по промежуточному
    состоянию между плечами уже завершённой транзакции" -- несколько
    Swap-логов ОДНОЙ tx входят в ОДНУ пачку одного вызова eth_getLogs)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, PoolState] = {}

    def apply_swap_batch(self, decoded_events: list[dict]) -> set[str]:
        """decoded_events: [{"pool_id", "block_number", "log_index",
        "sqrt_price_x96", "liquidity", "tick", "fee"}, ...] -- НЕ
        обязательно отсортирован на входе, сортируется здесь. Возвращает
        множество pool_id, чьё состояние реально обновилось (для вызывающего
        кода -- какие маршруты стоит переоценить)."""
        touched = set()
        with self._lock:
            for ev in sorted(decoded_events, key=lambda e: (e["block_number"], e["log_index"])):
                pid = ev["pool_id"].lower()
                prev = self._states.get(pid)
                if prev is not None and (ev["block_number"], ev["log_index"]) <= (prev.last_block, prev.last_log_index):
                    continue  # событие старше/равно уже применённому -- игнор (защита от повторной доставки)
                self._states[pid] = PoolState(
                    sqrt_price_x96=ev["sqrt_price_x96"], liquidity=ev["liquidity"], tick=ev["tick"],
                    realized_fee=ev["fee"], last_block=ev["block_number"], last_log_index=ev["log_index"],
                )
                touched.add(pid)
        return touched

    def mark_stale(self, pool_ids: list[str]) -> None:
        """Пропуск/реорг диапазона (владелец, п.2: "затронутое состояние
        нужно восстановить, а не считать актуальным") -- УДАЛЯЕТ запись
        (не оставляет старое значение под видом текущего); следующее
        обращение cheap_filter_route увидит "не инициализирован" и уйдёт
        в очередь пересмотра, не в дешёвый фильтр по устаревшим числам."""
        with self._lock:
            for pid in pool_ids:
                self._states.pop(pid.lower(), None)

    def get(self, pool_id: str) -> PoolState | None:
        with self._lock:
            return self._states.get(pool_id.lower())

    def is_initialized(self, pool_id: str) -> bool:
        return self.get(pool_id) is not None


@dataclass
class CheapFilterResult:
    verdict: str  # "passed" | "rejected_by_price" | "not_initialized" | "hook_model_absent"
    covered: bool
    implied_product: float | None = None
    uncovered_pool_ids: list[str] = field(default_factory=list)
    detail: str = ""


def cheap_filter_route(route, cache: PoolStateCache, current_block: int,
                        max_age_blocks: int = DEFAULT_MAX_AGE_BLOCKS) -> CheapFilterResult:
    """Дешёвый фильтр (владелец, п.3): произведение направленных
    предельных цен с комиссиями по всем плечам маршрута. Это ОЦЕНКА
    расхождения на границе (marginal price), НЕ обещание прибыли на
    конечном размере (реальный размер выбирает recompute_route по
    сетке, как и раньше -- этот фильтр только решает, стоит ли вообще
    её запускать).

    Для hookless-плеч (leg.hooks == NATIVE) -- используем закэшированные
    sqrtPriceX96/realized_fee. Для плеч, чей (pool_id, zero_for_one)
    есть в CONFIRMED_HOOK_MODELS -- применяем ПОДТВЕРЖДЁННУЮ поправку.
    Иначе (любой другой хук, включая ДРУГОЕ направление/пул ТОГО ЖЕ
    адреса хука) -- verdict="hook_model_absent", маршрут НЕ
    отфильтровывается (ни отклоняется, ни пропускается по цене) --
    решение о его судьбе -- у вызывающего кода (см. владелец: "не
    отправляй все непокрытые хук-маршруты автоматически в прежнюю
    очередь")."""
    uncovered = []
    product = 1.0
    for leg in route.legs:
        pid = leg.pool_id_hex.lower()
        state = cache.get(pid)
        if state is None or not state.is_fresh_for_fast_filter(current_block, max_age_blocks):
            return CheapFilterResult(verdict="not_initialized", covered=False, uncovered_pool_ids=[pid],
                                      detail=f"пул {pid} ещё не в кэше или кэш устарел "
                                             f"(> {max_age_blocks} блоков без Swap-события)")

        is_hookless = leg.hooks.lower() == NATIVE_HOOKS
        model = None if is_hookless else CONFIRMED_HOOK_MODELS.get((pid, leg.zero_for_one))
        if not is_hookless and model is None:
            uncovered.append(pid)
            continue  # честно продолжаем считать остальные плечи -- но маршрут в целом НЕ покрыт

        # price = token1-за-token0 из sqrtPriceX96 (Q96); direction решает, что тут "вход"/"выход".
        raw_price = (state.sqrt_price_x96 / (2 ** 96)) ** 2
        fee_frac = state.realized_fee / 1_000_000.0  # fee в сотых бип, 1e6 = 100% (см. PoolKey/Swap-событие)
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
