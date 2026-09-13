#!/usr/bin/env python3
"""Задача 5, разбор повторяющихся ошибок торгового пути (владелец,
2026-09-13, доп.: "Расшифруй повторяющиеся ошибки из существующего
JSONL... не называй любой revert отсутствием ликвидности").

ФАКТИЧЕСКАЯ ПРИЧИНА бага, который эта правка устраняет: и
`quote_route_at_size()` (task5_v4_hotpath.py), и разбор "маршрут
исключён, перепроверка... подтвердила" в `evaluator_loop`, до этой
правки решали "это отсутствие ликвидности?" ПОДСТРОЧНЫМ поиском текста
"NotEnoughLiquidity" в строковом представлении исключения. Реальные
RPC-ошибки V4Quoter НИКОГДА не содержат этот текст читаемо -- причина
закодирована ABI (4-байтный селектор + аргументы) внутри поля `data`
JSON-RPC-ошибки. Подстрочный поиск поэтому НИКОГДА не совпадал (честно
подтверждено разбором сохранённого no_send_log.jsonl этой торговой
сессии: 43 записи с подтверждённым execution revert, 0 из них содержат
текст "NotEnoughLiquidity" читаемо) -- ВСЕ такие reverts либо тихо
проваливались в общую "ошибка расчёта", либо (в отдельной ветке
evaluator_loop про уже исключённые маршруты) БЕЗУСЛОВНО маркировались
"нет ликвидности" ЛЮБОЙ подтверждённый revert, какой бы ни была
настоящая причина -- сам разбор данных здесь ЭТО и обнаружил (см.
data_prefix записей 'ошибка расчёта': один и тот же внешний селектор
0x6190b2b0, но РАЗНЫЕ внутренние селекторы -- 40 из 43 дают
0x7a5ed734, 3 из 43 -- 0x7c9c6e8f, неопознанный).

Селекторы ниже -- РЕАЛЬНЫЕ, вычислены локально через keccak256 (pycryptodome,
тот же метод, что analysis/task5_v4_pool_math.py::pool_id) от СИГНАТУР,
взятых из настоящего исходного кода (WebFetch этой сессии,
raw.githubusercontent.com):
  - Uniswap/v4-periphery, src/base/BaseV4Quoter.sol
  - Uniswap/v4-core, src/libraries/Pool.sol
  - Uniswap/v4-core, src/interfaces/IPoolManager.sol
Ни одно имя/сигнатура здесь не придумано -- то, что не удалось найти
среди реально прочитанных файлов (см. `0x7c9c6e8f` ниже), честно
помечено неопознанным, а не подставлено правдоподобным именем."""
from __future__ import annotations

import re

# {селектор: (сигнатура, источник, source_independent_ok_to_stop_sizing)}
#
# `size_independent=True` -- ТОЛЬКО для ошибок, где ПОВТОРНАЯ котировка
# ТОГО ЖЕ маршрута НА ДРУГОМ РАЗМЕРЕ гарантированно не изменит исход:
#   - NotEnoughLiquidity(poolId) -- пул тот же, ликвидность та же;
#     существующий докстринг recompute_route() уже документирует
#     реальное свойство V4-пулов с концентрированной ликвидностью
#     (больший размер только хуже) -- этой правкой НЕ меняется, только
#     теперь опирается на ПОДТВЕРЖДЁННЫЙ селектор, а не на текстовый
#     поиск, который никогда не совпадал.
#   - PoolNotInitialized / PoolAlreadyInitialized / CurrenciesOutOfOrderOrEqual /
#     TickSpacingTooLarge / TickSpacingTooSmall -- ВСЕ они -- свойства
#     САМОГО PoolKey (currency0/currency1/fee/tickSpacing/hooks), НЕ
#     размера сделки -- тот же PoolKey используется на КАЖДОМ размере
#     сетки, так что подтверждённая ошибка здесь -- подтверждённо
#     неверная/несуществующая конфигурация пула для ВСЕХ размеров сразу
#     (ровно пример владельца: "подтверждённо неверный PoolKey").
# Всё остальное (NotSelf/UnexpectedCallSuccess/CurrencyNotSettled/
# ManagerLocked/AlreadyUnlocked и т.п.) -- внутренние инварианты
# Quoter/PoolManager, НЕ описывающие свойство ИМЕННО этого PoolKey при
# ИМЕННО этом размере -- размеро-независимость здесь НЕ подтверждена,
# ранний останов НЕ применяется.
KNOWN_SELECTORS: dict[str, tuple[str, str, bool]] = {
    "0x6190b2b0": ("UnexpectedRevertBytes(bytes)", "BaseV4Quoter.sol (Uniswap/v4-periphery)", False),
    "0x7a5ed734": ("NotEnoughLiquidity(bytes32)", "BaseV4Quoter.sol (Uniswap/v4-periphery)", True),
    "0x29c3b7ee": ("NotSelf()", "BaseV4Quoter.sol (Uniswap/v4-periphery)", False),
    "0xe0752a5a": ("UnexpectedCallSuccess()", "BaseV4Quoter.sol (Uniswap/v4-periphery)", False),
    "0x486aa307": ("PoolNotInitialized()", "Pool.sol / IPoolManager.sol (Uniswap/v4-core)", True),
    "0x7983c051": ("PoolAlreadyInitialized()", "Pool.sol (Uniswap/v4-core)", True),
    "0x5212cba1": ("CurrencyNotSettled()", "IPoolManager.sol (Uniswap/v4-core)", False),
    "0x54e3ca0d": ("ManagerLocked()", "IPoolManager.sol (Uniswap/v4-core)", False),
    "0x5090d6c6": ("AlreadyUnlocked()", "IPoolManager.sol (Uniswap/v4-core)", False),
    "0x6e6c9830": ("CurrenciesOutOfOrderOrEqual(address,address)", "IPoolManager.sol (Uniswap/v4-core)", True),
    "0xb70024f8": ("TickSpacingTooLarge(int24)", "IPoolManager.sol (Uniswap/v4-core)", True),
    "0xe9e90588": ("TickSpacingTooSmall(int24)", "IPoolManager.sol (Uniswap/v4-core)", True),
    "0x30d21641": ("UnauthorizedDynamicLPFeeUpdate()", "IPoolManager.sol (Uniswap/v4-core)", False),
    "0xbe8b8507": ("SwapAmountCannotBeZero()", "IPoolManager.sol (Uniswap/v4-core)", False),
    "0xb0ec849e": ("NonzeroNativeValue()", "IPoolManager.sol (Uniswap/v4-core)", False),
    "0xbda73abf": ("MustClearExactPositiveDelta()", "IPoolManager.sol (Uniswap/v4-core)", False),
}

_DATA_RE = re.compile(r"'data':\s*'(0x[0-9a-fA-F]+)")


def decode_v4_revert_detail(detail: str) -> dict:
    """Разбирает ABI-закодированный `data` из строки RPC-ошибки (как
    сохраняется `str(exc)` в этом проекте) -- внешний селектор, и, если
    внешний -- обёртка Quoter'а (`UnexpectedRevertBytes(bytes)`),
    внутренний (настоящий) селектор.

    Возвращает {"recognized_outer": bool, "outer_selector": str|None,
    "outer_name": str|None, "inner_selector": str|None, "inner_name":
    str|None, "size_independent": bool, "note": str} -- НИКОГДА не
    подставляет имя для нераспознанного селектора (см. KNOWN_SELECTORS
    докстринг выше); `size_independent` False, если хоть что-то не
    опознано -- безопасный (консервативный) выбор по умолчанию."""
    m = _DATA_RE.search(detail or "")
    if not m:
        return {"recognized_outer": False, "outer_selector": None, "outer_name": None,
                "inner_selector": None, "inner_name": None, "size_independent": False,
                "note": "в detail не найдено поле 'data' -- не ABI-revert (или другой формат ошибки)"}
    data = m.group(1)
    outer_sel = data[:10]
    outer_info = KNOWN_SELECTORS.get(outer_sel)
    result = {"recognized_outer": outer_info is not None, "outer_selector": outer_sel,
              "outer_name": outer_info[0] if outer_info else None,
              "inner_selector": None, "inner_name": None, "size_independent": False,
              "note": ""}
    if outer_info is None:
        result["note"] = f"внешний селектор {outer_sel} не найден в KNOWN_SELECTORS (см. докстринг модуля)"
        return result
    if outer_info[0] != "UnexpectedRevertBytes(bytes)":
        # Внешний селектор САМ по себе -- окончательная причина (не обёртка).
        result["size_independent"] = outer_info[2]
        result["note"] = f"внешний селектор -- окончательная причина ({outer_info[1]})"
        return result
    # Обёртка Quoter'а -- разворачиваем один уровень: offset(32б) + length(32б) + payload.
    body = data[10:]
    if len(body) < 128:
        result["note"] = "UnexpectedRevertBytes(bytes), но данных недостаточно для разбора длины/payload " \
                          "(вероятно, обрезано вызывающим кодом -- см. detail[:300] в check_route_liveness)"
        return result
    try:
        length = int(body[64:128], 16)
    except ValueError:
        result["note"] = "UnexpectedRevertBytes(bytes) -- не удалось распарсить длину payload"
        return result
    inner_hex = body[128:128 + length * 2]
    if len(inner_hex) < 8:
        result["note"] = ("UnexpectedRevertBytes(bytes), внутренний payload короче 4 байт "
                           "(обрезан -- см. detail[:300] в check_route_liveness) -- селектор недоступен")
        return result
    inner_sel = "0x" + inner_hex[:8]
    inner_info = KNOWN_SELECTORS.get(inner_sel)
    result["inner_selector"] = inner_sel
    if inner_info is None:
        result["note"] = (f"внешняя обёртка опознана (UnexpectedRevertBytes), но внутренний селектор "
                           f"{inner_sel} НЕ найден в KNOWN_SELECTORS -- неопознанная причина, "
                           f"НЕ считаем размеро-независимой")
        return result
    result["inner_name"] = inner_info[0]
    result["size_independent"] = inner_info[2]
    result["note"] = f"обёртка UnexpectedRevertBytes развёрнута -- реальная причина: {inner_info[0]} ({inner_info[1]})"
    return result


if __name__ == "__main__":
    import sys
    for line in sys.stdin:
        print(decode_v4_revert_detail(line.rstrip("\n")))
