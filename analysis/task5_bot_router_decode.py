#!/usr/bin/env python3
"""Задача 5, живой бот -- декодер calldata роутеров (минимальный Путь А,
владелец 2026-09-12, "добавка"): "строим путь А в минимальном виде --
декодер одного-двух подтверждённых роутеров, без полного покрытия."

Чистая офлайн-логика, БЕЗ сети -- принимает уже декодированные `to`/`data`
(из `decode_l2_message`, `task5_bot_feed_client.py`) и возвращает список
`SwapIntent` (какой пул затронут, в какую сторону, каким входным
количеством) -- НЕ ABI-кодирует, НЕ подписывает, НЕ отправляет ничего.

Селекторы -- РЕАЛЬНО ВЫЧИСЛЕНЫ (keccak-256 первых 4 байт сигнатуры), не
подставлены по памяти/аналогии -- см. `_verify_selectors_self_check()`
внизу файла, запускается при импорте модуля (честная защита от опечатки
в сигнатуре, а не только "владелец назвал число"):

    exactInputSingle((address,address,uint24,address,uint256,uint256,uint160)) -> 0x04e45aaf   (SwapRouter02, БЕЗ deadline)
    exactInput((bytes,address,uint256,uint256))                                 -> 0xb858183f   (SwapRouter02, БЕЗ deadline)
    multicall(bytes[])                                                          -> 0xac9650d8
    multicall(uint256,bytes[])                                                  -> 0x5ae401dc   (с deadline)
    execute(bytes,bytes[],uint256)                                              -> 0x3593564c   (Universal Router, с deadline)
    execute(bytes,bytes[])                                                      -> 0x24856bc3   (Universal Router, БЕЗ deadline)

ЧЕСТНАЯ ОГОВОРКА про Universal Router: реализован ТОЛЬКО один командный
байт -- `V3_SWAP_EXACT_IN = 0x00` (реальное, публичное значение из
открытого исходника Uniswap Labs `UniversalRouter`/`Commands.sol`, тип
команды = `command & 0x3f`, верхние биты -- флаги, не часть типа). Любая
другая команда -- честно помечается `unhandled_command`, НЕ угадывается
(владелец сам просил минимальный вид -- "декодер одного-двух
подтверждённых роутеров", не полное покрытие всех команд роутера)."""
from __future__ import annotations

from dataclasses import dataclass

from Crypto.Hash import keccak

# Владелец, 2026-09-12: адрес, УЖЕ реально задокументированный в этой сессии
# как доминирующий self-trading-бот, вызывающий пулы НАПРЯМУЮ (не роутер) --
# см. FOMO-форензику/paspoрт и `asset_consolidation_rh_router_verify_result.json`
# ("известный адрес самоторговли -- не router"). Единственный адрес такого рода,
# реально найденный в этой сессии -- список НЕ придуман длиннее, чем есть.
KNOWN_SELF_TRADE_ADDRESSES = {"0x65050a9b7e5075a2ba5ced7b1b64ee66262c40dc"}


def _selector(signature: str) -> str:
    h = keccak.new(digest_bits=256)
    h.update(signature.encode())
    return "0x" + h.hexdigest()[:8]


SEL_EXACT_INPUT_SINGLE = _selector("exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")
SEL_EXACT_INPUT = _selector("exactInput((bytes,address,uint256,uint256))")
SEL_MULTICALL_NO_DEADLINE = _selector("multicall(bytes[])")
SEL_MULTICALL_WITH_DEADLINE = _selector("multicall(uint256,bytes[])")
SEL_UR_EXECUTE_WITH_DEADLINE = _selector("execute(bytes,bytes[],uint256)")
SEL_UR_EXECUTE_NO_DEADLINE = _selector("execute(bytes,bytes[])")

# Владелец дал эти же значения текстом ("0x04e45aaf exactInputSingle, 0xb858183f
# exactInput, 0xac9650d8 / 0x5ae401dc multicall, 0x3593564c UniversalRouter.execute")
# -- см. `_verify_selectors_self_check()` ниже, сверяет вычисленное с этим списком
# при импорте модуля, а не доверяет ни владельцу, ни себе молча.
_EXPECTED_FROM_OWNER = {
    "0x04e45aaf": SEL_EXACT_INPUT_SINGLE,
    "0xb858183f": SEL_EXACT_INPUT,
    "0xac9650d8": SEL_MULTICALL_NO_DEADLINE,
    "0x5ae401dc": SEL_MULTICALL_WITH_DEADLINE,
    "0x3593564c": SEL_UR_EXECUTE_WITH_DEADLINE,
}

KNOWN_SWAP_SELECTORS = {
    SEL_EXACT_INPUT_SINGLE: "exactInputSingle (SwapRouter02)",
    SEL_EXACT_INPUT: "exactInput (SwapRouter02)",
    SEL_MULTICALL_NO_DEADLINE: "multicall(bytes[])",
    SEL_MULTICALL_WITH_DEADLINE: "multicall(uint256,bytes[])",
    SEL_UR_EXECUTE_WITH_DEADLINE: "UniversalRouter.execute (with deadline)",
    SEL_UR_EXECUTE_NO_DEADLINE: "UniversalRouter.execute (no deadline)",
}

# Universal Router: тип команды = command_byte & 0x3f (верхние 2 бита --
# флаги ALLOW_REVERT/не используется, см. открытый исходник Commands.sol).
# Реализован ТОЛЬКО V3_SWAP_EXACT_IN -- реальное публичное значение 0x00.
_UR_COMMAND_TYPE_MASK = 0x3F
_UR_V3_SWAP_EXACT_IN = 0x00


@dataclass
class SwapIntent:
    token_in: str
    token_out: str
    fee: int | None  # None -- амаунт/токены известны, конкретный пул (fee-tier) выбрать нельзя без него
    amount_in: int | None  # None -- пул затронут, но точный входной объём этого хопа не восстановить (см. exactInput hop>=1)
    source_selector: str
    source_to: str
    hop_index: int = 0


def _word(data: bytes, i: int) -> int:
    """i-е 32-байтное слово (0-индексация) как беззнаковое целое."""
    start = i * 32
    return int.from_bytes(data[start:start + 32], "big")


def _addr_from_word(data: bytes, i: int) -> str:
    start = i * 32
    return "0x" + data[start + 12:start + 32].hex()


def _decode_v3_path(path: bytes) -> list[tuple[str, int]]:
    """Формат пути Uniswap V3: token(20 байт) + fee(3 байта, БЕЗ знака) +
    token(20 байт) + fee(3 байта) + ... + token(20 байт) -- N токенов, N-1 хопов.
    Возвращает список (token_in_hop_i, fee_hop_i) для каждого хопа по порядку
    (последний токен пути -- НЕ включён как отдельный элемент, он token_out
    последнего хопа, вызывающий код уже это знает по паре с соседним)."""
    hops: list[tuple[str, int]] = []
    if len(path) < 20:
        return hops
    pos = 0
    tokens: list[str] = []
    fees: list[int] = []
    while pos + 20 <= len(path):
        tokens.append("0x" + path[pos:pos + 20].hex())
        pos += 20
        if pos + 3 <= len(path):
            fees.append(int.from_bytes(path[pos:pos + 3], "big"))
            pos += 3
        else:
            break
    for i in range(len(tokens) - 1):
        hops.append((tokens[i], fees[i]))
    return hops


def _decode_exact_input_single(data_no_selector: bytes, to_addr: str, selector: str) -> list[SwapIntent]:
    # Все поля статические -- кортеж кодируется НАПРЯМУЮ (без офсет-указателя),
    # 7 слов по 32 байта: tokenIn, tokenOut, fee(uint24), recipient, amountIn,
    # amountOutMinimum, sqrtPriceLimitX96 (см. докстринг модуля).
    if len(data_no_selector) < 7 * 32:
        return [SwapIntent(None, None, None, None, selector, to_addr)]  # honesty: too short, can't decode
    token_in = _addr_from_word(data_no_selector, 0)
    token_out = _addr_from_word(data_no_selector, 1)
    fee = _word(data_no_selector, 2)
    amount_in = _word(data_no_selector, 4)
    return [SwapIntent(token_in, token_out, fee, amount_in, selector, to_addr)]


def _decode_exact_input(data_no_selector: bytes, to_addr: str, selector: str) -> list[SwapIntent]:
    # Единственный аргумент -- динамический кортеж (bytes path, address
    # recipient, uint256 amountIn, uint256 amountOutMinimum) -> голова
    # содержит офсет на кортеж, внутри кортежа -- офсет на path + recipient +
    # amountIn + amountOutMinimum, хвост -- длина path + сами байты пути.
    if len(data_no_selector) < 32:
        return []
    outer_offset = _word(data_no_selector, 0)
    tuple_region = data_no_selector[outer_offset:]
    if len(tuple_region) < 4 * 32:
        return []
    path_offset = _word(tuple_region, 0)
    amount_in = _word(tuple_region, 2)
    path_len = int.from_bytes(tuple_region[path_offset:path_offset + 32], "big")
    path = tuple_region[path_offset + 32:path_offset + 32 + path_len]
    hops = _decode_v3_path(path)
    intents: list[SwapIntent] = []
    for i, (token_in, fee) in enumerate(hops):
        token_out = hops[i + 1][0] if i + 1 < len(hops) else None
        # ЧЕСТНО: token_out последнего хопа -- последний токен пути, не
        # хранится в `hops` как отдельная запись (см. _decode_v3_path) --
        # восстанавливаем прямым срезом path, а не гадаем.
        if token_out is None:
            token_out = "0x" + path[-20:].hex()
        intents.append(SwapIntent(
            token_in, token_out, fee,
            amount_in if i == 0 else None,  # честно: amountIn известен ТОЛЬКО для первого хопа
            selector, to_addr, hop_index=i,
        ))
    return intents


def _decode_multicall(data_no_selector: bytes, to_addr: str, selector: str, has_deadline: bool,
                       _depth: int) -> list[SwapIntent]:
    # multicall(bytes[]) -- один динамический аргумент (массив bytes);
    # multicall(uint256,bytes[]) -- deadline(static) + bytes[] (dynamic) ->
    # голова: [deadline][офсет на bytes[]], под-вызовы декодируются РЕКУРСИВНО
    # тем же диспетчером (могут содержать exactInputSingle/exactInput и т.д.).
    if len(data_no_selector) < 32:
        return []
    array_offset_word_index = 1 if has_deadline else 0
    if len(data_no_selector) < (array_offset_word_index + 1) * 32:
        return []
    array_offset = _word(data_no_selector, array_offset_word_index)
    array_region = data_no_selector[array_offset:]
    if len(array_region) < 32:
        return []
    n_items = _word(array_region, 0)
    intents: list[SwapIntent] = []
    for i in range(min(n_items, 64)):  # честный предохранитель от аномального n_items на битых данных
        item_offset = _word(array_region, 1 + i)
        item_region = array_region[32 + item_offset:]
        if len(item_region) < 32:
            continue
        item_len = _word(item_region, 0)
        item_bytes = item_region[32:32 + item_len]
        intents.extend(decode_calldata(to_addr, "0x" + item_bytes.hex(), _depth=_depth + 1))
    return intents


def _decode_universal_router_execute(data_no_selector: bytes, to_addr: str, selector: str,
                                      has_deadline: bool, _depth: int) -> list[SwapIntent]:
    # execute(bytes commands, bytes[] inputs[, uint256 deadline]) -- commands
    # и inputs -- оба динамические -> голова: [офсет commands][офсет inputs]
    # (+ [deadline] в конце головы, если есть -- статический, порядок полей
    # головы не меняется от наличия deadline, он просто третье слово).
    if len(data_no_selector) < 2 * 32:
        return []
    commands_offset = _word(data_no_selector, 0)
    inputs_offset = _word(data_no_selector, 1)
    commands_len = int.from_bytes(data_no_selector[commands_offset:commands_offset + 32], "big")
    commands = data_no_selector[commands_offset + 32:commands_offset + 32 + commands_len]
    inputs_region = data_no_selector[inputs_offset:]
    if len(inputs_region) < 32:
        return []
    n_inputs = _word(inputs_region, 0)
    intents: list[SwapIntent] = []
    for i in range(min(n_inputs, len(commands), 64)):
        command_byte = commands[i]
        command_type = command_byte & _UR_COMMAND_TYPE_MASK
        item_offset = _word(inputs_region, 1 + i)
        item_region = inputs_region[32 + item_offset:]
        if len(item_region) < 32:
            continue
        item_len = _word(item_region, 0)
        item_data = item_region[32:32 + item_len]
        if command_type != _UR_V3_SWAP_EXACT_IN:
            # честно: команда не реализована в этом минимальном декодере --
            # НЕ угадываем формат, просто фиксируем факт для диагностики
            # вызывающим кодом через unhandled marker (см. decode_calldata).
            intents.append(SwapIntent(None, None, None, None, f"UR_CMD_0x{command_type:02x}_unhandled", to_addr,
                                       hop_index=i))
            continue
        # V3_SWAP_EXACT_IN inputs: (address recipient, uint256 amountIn,
        # uint256 amountOutMin, bytes path, bool payerIsUser) -- path
        # динамический -> голова 5 слов (последнее -- офсет на path, вернее
        # 4-е по порядку, т.к. payerIsUser статический и идёт ПОСЛЕ офсета
        # в объявлении, но abi.encode кладёт статические поля на свои места
        # в голове по объявленному порядку по принципу "офсет там, где сам
        # аргумент объявлен" -- порядок объявления: recipient, amountIn,
        # amountOutMin, path, payerIsUser -> слово3 = офсет на path,
        # слово4 = payerIsUser).
        if len(item_data) < 5 * 32:
            continue
        amount_in = _word(item_data, 1)
        path_offset = _word(item_data, 3)
        path_len = int.from_bytes(item_data[path_offset:path_offset + 32], "big")
        path = item_data[path_offset + 32:path_offset + 32 + path_len]
        hops = _decode_v3_path(path)
        for hop_i, (token_in, fee) in enumerate(hops):
            token_out = hops[hop_i + 1][0] if hop_i + 1 < len(hops) else "0x" + path[-20:].hex()
            intents.append(SwapIntent(
                token_in, token_out, fee,
                amount_in if hop_i == 0 else None,
                selector, to_addr, hop_index=hop_i,
            ))
    return intents


def decode_calldata(to_addr: str | None, data_hex: str, _depth: int = 0) -> list[SwapIntent]:
    """Главная точка входа -- принимает уже извлечённые (`decode_l2_message`)
    `to`/`data` ОДНОЙ транзакции, возвращает список затронутых свопов
    (может быть пуст -- calldata не распознана этим минимальным декодером,
    честно, не гадаем). `_depth` -- защита от аномальной рекурсии
    multicall-в-multicall (реально не наблюдалась, но не полагаемся на это)."""
    if _depth > 4 or not data_hex or len(data_hex) < 10 or to_addr is None:
        return []
    raw = bytes.fromhex(data_hex[2:] if data_hex.startswith("0x") else data_hex)
    selector = "0x" + raw[:4].hex()
    body = raw[4:]
    if selector == SEL_EXACT_INPUT_SINGLE:
        return _decode_exact_input_single(body, to_addr, selector)
    if selector == SEL_EXACT_INPUT:
        return _decode_exact_input(body, to_addr, selector)
    if selector == SEL_MULTICALL_NO_DEADLINE:
        return _decode_multicall(body, to_addr, selector, has_deadline=False, _depth=_depth)
    if selector == SEL_MULTICALL_WITH_DEADLINE:
        return _decode_multicall(body, to_addr, selector, has_deadline=True, _depth=_depth)
    if selector == SEL_UR_EXECUTE_WITH_DEADLINE:
        return _decode_universal_router_execute(body, to_addr, selector, has_deadline=True, _depth=_depth)
    if selector == SEL_UR_EXECUTE_NO_DEADLINE:
        return _decode_universal_router_execute(body, to_addr, selector, has_deadline=False, _depth=_depth)
    return []  # честно: неизвестный/нецелевой селектор -- не декодируем, не угадываем


def _verify_selectors_self_check() -> None:
    for owner_given, computed in _EXPECTED_FROM_OWNER.items():
        if owner_given != computed:
            raise AssertionError(
                f"task5_bot_router_decode: селектор, названный владельцем ({owner_given}), "
                f"НЕ совпадает с реально вычисленным keccak ({computed}) -- честная остановка, "
                f"не подставляем расхождение молча."
            )


_verify_selectors_self_check()
