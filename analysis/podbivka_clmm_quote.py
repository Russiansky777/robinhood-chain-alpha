#!/usr/bin/env python3
"""Точная котировка exact_in для Raydium CLMM (CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK) -- офлайн, без сети.

Перенос из исходников raydium-io/raydium-clmm (programs/amm/src, коммит ed1eb41 от 29.09.2026):
  * instructions/swap.rs -- swap_internal: главный цикл по инициализированным тикам, SwapState (apply_swap_amounts,
    spilt_fees, get_spacing_bounded_price, get_total_fee_rate, compute_dynamic_fee_rate, update_dynamic_fee_index),
    лимитные ордера на тике границы;
  * libraries/swap_math.rs -- compute_swap; sqrt_price_math.rs; liquidity_math.rs (get_delta_amounts_for_swap,
    add_delta); tick_math.rs (get_sqrt_price_at_tick, get_tick_at_sqrt_price, get_price_from_sqrt_price);
  * libraries/tick_array_bit_map.rs, states/tickarray_bitmap_extension.rs, states/pool.rs -- поиск следующего
    инициализированного tick array (битовая карта пула 1024 бит и расширение);
  * states/tick_array.rs -- TickState, next_initialized_tick / first_initialized_tick, match_limit_order_with_sqrt_price;
  * states/pool_fee.rs -- DynamicFeeInfo (update_reference, update_volatility_accumulator), fee_on.
Арифметика целочисленная (int Python); деление Rust к нулю -- через _tdiv/_tmod; U1024/U512 -- маской ширины.
Раскладки счетов -- по структурам исходников (repr(C, packed)): PoolState 1544 байта, TickArrayState 10240,
TickState 168, TickArrayBitmapExtension 1832, AmmConfig -- borsh (#[account]).
Вход и выход -- в сырых единицах токенов пула (то, что получает и отдаёт хранилище; налог Token-2022 -- снаружи).
"""
from __future__ import annotations

import struct

ПРОГРАММА = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
MIN_TICK, MAX_TICK = -443636, 443636
MIN_SQRT_PRICE_X64 = 4295048016
MAX_SQRT_PRICE_X64 = 79226673521066979257578248091
Q64 = 1 << 64
U64_MAX, U128_MAX = (1 << 64) - 1, (1 << 128) - 1
FEE_RATE_DENOMINATOR_VALUE = 1_000_000
MAX_FEE_RATE_NUMERATOR = 100_000
VOLATILITY_ACCUMULATOR_SCALE = 10_000
REDUCTION_FACTOR_DENOMINATOR = 10_000
DYNAMIC_FEE_CONTROL_DENOMINATOR = 100_000
TICK_ARRAY_SIZE = 60
TICK_ARRAY_BITMAP_SIZE = 512
EXT_SIZE = 14


class ОшибкаCLMM(Exception):
    pass


class _Переполнение(ОшибкаCLMM):
    """MaxTokenOverflow: в compute_swap означает «цель недостижима», а не отказ."""


def _tdiv(a: int, b: int) -> int:
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def _tmod(a: int, b: int) -> int:
    return a - _tdiv(a, b) * b


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)


# ------------------------------------------------------------------ разбор счетов

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _pk(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s_ = ""
    while n:
        n, r = divmod(n, 58)
        s_ = _B58[r] + s_
    return "1" * (len(b) - len(b.lstrip(b"\x00"))) + s_


def _u128(d: bytes, o: int) -> int:
    lo, hi = struct.unpack_from("<QQ", d, o)
    return lo | (hi << 64)


def _i128(d: bytes, o: int) -> int:
    v = _u128(d, o)
    return v - (1 << 128) if v >> 127 else v


def разобрать_пул(d: bytes) -> dict:
    if len(d) < 1544:
        raise ОшибкаCLMM(f"PoolState {len(d)} байт, ждём 1544")
    бм = struct.unpack_from("<16Q", d, 904)
    return {"amm_config": _pk(d[9:41]), "token_mint_0": _pk(d[73:105]), "token_mint_1": _pk(d[105:137]),
            "token_vault_0": _pk(d[137:169]), "token_vault_1": _pk(d[169:201]), "observation_key": _pk(d[201:233]),
            "mint_decimals_0": d[233], "mint_decimals_1": d[234],
            "tick_spacing": struct.unpack_from("<H", d, 235)[0], "liquidity": _u128(d, 237),
            "sqrt_price_x64": _u128(d, 253), "tick_current": struct.unpack_from("<i", d, 269)[0],
            "fee_growth_global_0_x64": _u128(d, 277), "fee_growth_global_1_x64": _u128(d, 293),
            "status": d[389], "fee_on": d[390],
            "tick_array_bitmap": sum(v << (64 * i) for i, v in enumerate(бм)),
            "open_time": struct.unpack_from("<Q", d, 1080)[0],
            "dynamic_fee_info": разобрать_dfi(d[1096:1176])}


def разобрать_dfi(d: bytes) -> dict | None:
    """DynamicFeeInfo (80 байт); всё нули -- динамической комиссии нет (get_dynamic_fee_info → None)."""
    if not any(d):
        return None
    fp, dp, rf, dfc, mva, tsir, vr, va, lut = struct.unpack_from("<HHHIIiIIQ", d, 0)
    return {"filter_period": fp, "decay_period": dp, "reduction_factor": rf, "dynamic_fee_control": dfc,
            "max_volatility_accumulator": mva, "tick_spacing_index_reference": tsir, "volatility_reference": vr,
            "volatility_accumulator": va, "last_update_timestamp": lut}


def разобрать_конфиг(d: bytes) -> dict:
    return {"index": struct.unpack_from("<H", d, 9)[0], "protocol_fee_rate": struct.unpack_from("<I", d, 43)[0],
            "trade_fee_rate": struct.unpack_from("<I", d, 47)[0], "tick_spacing": struct.unpack_from("<H", d, 51)[0],
            "fund_fee_rate": struct.unpack_from("<I", d, 53)[0]}


def разобрать_массив(d: bytes) -> dict:
    if len(d) < 10240:
        raise ОшибкаCLMM(f"TickArrayState {len(d)} байт, ждём 10240")
    тики = []
    for i in range(TICK_ARRAY_SIZE):
        o = 44 + 168 * i
        тики.append({"tick": struct.unpack_from("<i", d, o)[0], "liquidity_net": _i128(d, o + 4),
                     "liquidity_gross": _u128(d, o + 20), "order_phase": struct.unpack_from("<Q", d, o + 116)[0],
                     "orders_amount": struct.unpack_from("<Q", d, o + 124)[0],
                     "part_filled_orders_remaining": struct.unpack_from("<Q", d, o + 132)[0],
                     "unfilled_ratio_x64": _u128(d, o + 140)})
    return {"pool_id": _pk(d[8:40]), "start_tick_index": struct.unpack_from("<i", d, 40)[0], "ticks": тики,
            "initialized_tick_count": d[10124]}


def разобрать_расширение(d: bytes) -> dict:
    поз = [sum(v << (64 * j) for j, v in enumerate(struct.unpack_from("<8Q", d, 40 + 64 * i))) for i in range(EXT_SIZE)]
    нег = [sum(v << (64 * j) for j, v in enumerate(struct.unpack_from("<8Q", d, 40 + 64 * EXT_SIZE + 64 * i)))
           for i in range(EXT_SIZE)]
    return {"pool_id": _pk(d[8:40]), "positive": поз, "negative": нег}


def адрес_массива(пул: str, старт: int) -> str:
    from solders.pubkey import Pubkey  # noqa: PLC0415
    pda, _ = Pubkey.find_program_address([b"tick_array", bytes(Pubkey.from_string(пул)), struct.pack(">i", старт)],
                                         Pubkey.from_string(ПРОГРАММА))
    return str(pda)


def адрес_расширения(пул: str) -> str:
    from solders.pubkey import Pubkey  # noqa: PLC0415
    pda, _ = Pubkey.find_program_address([b"pool_tick_array_bitmap_extension", bytes(Pubkey.from_string(пул))],
                                         Pubkey.from_string(ПРОГРАММА))
    return str(pda)


# ------------------------------------------------------------------ tick_math

_ПОСТ = [0xfff97272373d4000, 0xfff2e50f5f657000, 0xffe5caca7e10f000, 0xffcb9843d60f7000, 0xff973b41fa98e800,
         0xff2ea16466c9b000, 0xfe5dee046a9a3800, 0xfcbe86c7900bb000, 0xf987a7253ac65800, 0xf3392b0822bb6000,
         0xe7159475a2caf000, 0xd097f3bdfd2f2000, 0xa9f746462d9f8000, 0x70d869a156f31c00, 0x31be135f97ed3200,
         0x9aa508b5b85a500, 0x5d6af8dedc582c, 0x2216e584f5fa]


def get_sqrt_price_at_tick(tick: int) -> int:
    if not MIN_TICK <= tick <= MAX_TICK:
        raise ОшибкаCLMM("TickUpperOverflow")
    a = abs(tick)
    ratio = 0xfffcb933bd6fb800 if a & 1 else Q64
    for i, c in enumerate(_ПОСТ, 1):
        if a & (1 << i):
            ratio = ((ratio * c) & U128_MAX) >> 64
    if tick > 0:
        ratio = U128_MAX // ratio
    return ratio & U128_MAX


def get_tick_at_sqrt_price(p: int) -> int:
    if not MIN_SQRT_PRICE_X64 <= p < MAX_SQRT_PRICE_X64:
        raise ОшибкаCLMM("SqrtPriceX64")
    msb = p.bit_length() - 1
    log2p_integer_x32 = (msb - 64) << 32
    bit = 0x8000_0000_0000_0000
    precision = 0
    frac = 0
    r = p >> (msb - 63) if msb >= 64 else p << (63 - msb)
    while bit > 0 and precision < 16:
        r = (r * r) & U128_MAX
        more = r >> 127
        r >>= 63 + more
        frac += bit * more
        bit >>= 1
        precision += 1
    log2p_x32 = log2p_integer_x32 + (frac >> 32)
    lg = log2p_x32 * 59543866431248
    tick_low = (lg - 184467440737095516) >> 64
    tick_high = (lg + 15793534762490258745) >> 64
    if tick_low == tick_high:
        return tick_low
    return tick_high if get_sqrt_price_at_tick(tick_high) <= p else tick_low


def get_price_from_sqrt_price(p: int, вверх: bool) -> int:
    prod = p * p
    if вверх:
        prod += Q64 - 1
    price = prod >> 64
    if price > U128_MAX:
        raise ОшибкаCLMM("CalculateOverflow")
    return price


# ------------------------------------------------------------------ sqrt_price_math / liquidity_math

def _next_from_amount_0_up(p: int, L: int, amount: int, add: bool) -> int:
    if amount == 0:
        return p
    n1 = L << 64
    if add:
        denom = n1 + amount * p                   # < 2^256 всегда: ветка с div_rounding_up не достигается
        return _ceil_div(n1 * p, denom) & U128_MAX
    prod = amount * p
    if prod >= 1 << 256 or n1 < prod:
        raise ОшибкаCLMM("CalculateOverflow")
    denom = n1 - prod
    if denom == 0:
        raise ОшибкаCLMM("CalculateOverflow")
    return _ceil_div(n1 * p, denom) & U128_MAX


def _next_from_amount_1_down(p: int, L: int, amount: int, add: bool) -> int:
    if amount == 0:
        return p
    if add:
        v = p + (amount << 64) // L
        if v > U128_MAX:
            raise ОшибкаCLMM("CalculateOverflow")
        return v
    q = _ceil_div(amount << 64, L)
    if q > p:
        raise ОшибкаCLMM("CalculateOverflow")
    return p - q


def get_next_sqrt_price_from_input(p: int, L: int, amount_in: int, zero_for_one: bool) -> int:
    if not (MIN_SQRT_PRICE_X64 <= p < MAX_SQRT_PRICE_X64):
        raise ОшибкаCLMM("SqrtPriceX64")
    if L <= 0:
        raise ОшибкаCLMM("ZeroLiquidity")
    return _next_from_amount_0_up(p, L, amount_in, True) if zero_for_one else _next_from_amount_1_down(p, L, amount_in, True)


def get_delta_amounts_for_swap(a: int, b: int, L: int, zero_for_one: bool) -> tuple:
    if a > b:
        a, b = b, a
    if a <= 0:
        raise ОшибкаCLMM("ZeroSqrtPrice")
    a1x64 = L * (b - a)
    prod = a * b
    if zero_for_one:
        amount_in = _ceil_div(a1x64 << 64, prod)
        amount_out = a1x64 >> 64
    else:
        amount_in = (a1x64 + U64_MAX) >> 64
        amount_out = (a1x64 << 64) // prod
    if amount_in > U64_MAX or amount_out > U64_MAX:
        raise _Переполнение("MaxTokenOverflow")
    return amount_in, amount_out


def add_delta(x: int, y: int) -> int:
    if y < 0:
        z = x - (-y)
        if not x > z or z < 0:
            raise ОшибкаCLMM("LiquiditySubValueErr")
        return z
    return x + y


def _mdf(v: int, n: int, d: int, предел: int = U64_MAX) -> int:
    r = v * n // d
    if r > предел:
        raise ОшибкаCLMM("CalculateOverflow")
    return r


def _mdc(v: int, n: int, d: int, предел: int = U64_MAX) -> int:
    r = _ceil_div(v * n, d)
    if r > предел:
        raise ОшибкаCLMM("CalculateOverflow")
    return r


# ------------------------------------------------------------------ swap_math

def compute_swap(cur: int, target: int, L: int, remaining: int, fee_rate: int, is_base_input: bool,
                 zero_for_one: bool, fee_on_input: bool) -> dict:
    """compute_swap (swap_math.rs), только exact_in (is_base_input) -- наш случай."""
    if not is_base_input:
        raise ОшибкаCLMM("exact_out не перенесён")
    D = FEE_RATE_DENOMINATOR_VALUE
    for_price = _mdf(remaining, D - fee_rate, D) if fee_on_input else remaining
    try:
        ain_t, aout_t = get_delta_amounts_for_swap(target, cur, L, zero_for_one)
        достижима = for_price >= ain_t
    except _Переполнение:
        достижима = False
    if достижима:
        nxt, ain, aout = target, ain_t, aout_t
    else:
        nxt = get_next_sqrt_price_from_input(cur, L, for_price, zero_for_one)
        ain, aout = get_delta_amounts_for_swap(nxt, cur, L, zero_for_one)
    if (zero_for_one and nxt < target) or (not zero_for_one and target < nxt):
        raise ОшибкаCLMM("compute_swap: цена за целью")
    if fee_on_input:
        fee = remaining - ain if nxt != target else _mdc(ain, fee_rate, D - fee_rate)
        if fee < 0:
            raise ОшибкаCLMM("CalculateOverflow")
    else:
        fee = _mdc(aout, fee_rate, D)
        aout -= fee
        if nxt != target:
            ain = remaining
    return {"sqrt_price_next_x64": nxt, "amount_in": ain, "amount_out": aout, "fee_amount": fee}


# ------------------------------------------------------------------ битовые карты

def _tick_count(spacing: int) -> int:
    return TICK_ARRAY_SIZE * spacing


def get_array_start_index(tick: int, spacing: int) -> int:
    n = _tick_count(spacing)
    s_ = _tdiv(tick, n)
    if tick < 0 and _tmod(tick, n) != 0:
        s_ -= 1
    return s_ * n


def _max_tick_in_bitmap(spacing: int) -> int:
    return spacing * TICK_ARRAY_SIZE * TICK_ARRAY_BITMAP_SIZE


def _check_valid_start(i: int, spacing: int) -> bool:
    if i < MIN_TICK or i > MAX_TICK:
        if i > MAX_TICK:
            return False
        return i == get_array_start_index(MIN_TICK, spacing)
    return _tmod(i, _tick_count(spacing)) == 0


def _start_index_range(spacing: int) -> tuple:
    mx = _max_tick_in_bitmap(spacing)
    mn = -mx
    if mx > MAX_TICK:
        mx = get_array_start_index(MAX_TICK, spacing) + _tick_count(spacing)
    if mn < MIN_TICK:
        mn = get_array_start_index(MIN_TICK, spacing)
    return mn, mx


def _overflow_default(tick: int, spacing: int) -> bool:
    mn, mx = _start_index_range(spacing)
    s_ = get_array_start_index(tick, spacing)
    return s_ >= mx or s_ < mn


_M1024 = (1 << 1024) - 1
_M512 = (1 << 512) - 1


def _check_current_default(bm: int, tick: int, spacing: int) -> tuple:
    mult = spacing * TICK_ARRAY_SIZE
    comp = _tdiv(tick, mult) + 512
    if tick < 0 and _tmod(tick, mult) != 0:
        comp -= 1
    return bool(bm >> abs(comp) & 1), (comp - 512) * mult


def _next_default(bm: int, last: int, spacing: int, zero_for_one: bool) -> tuple:
    if not _check_valid_start(last, spacing):
        raise ОшибкаCLMM("InvalidTickArrayBoundary")
    bound = _max_tick_in_bitmap(spacing)
    nxt = last - _tick_count(spacing) if zero_for_one else last + _tick_count(spacing)
    if nxt < -bound or nxt >= bound:
        return False, last
    mult = spacing * TICK_ARRAY_SIZE
    comp = _tdiv(nxt, mult) + 512
    if nxt < 0 and _tmod(nxt, mult) != 0:
        comp -= 1
    pos = abs(comp)
    if zero_for_one:
        off = (bm << (1024 - pos - 1)) & _M1024
        if off:
            lz = 1024 - off.bit_length()
            return True, (pos - lz - 512) * mult
        return False, -bound
    off = bm >> pos
    if off:
        tz = (off & -off).bit_length() - 1
        return True, (pos + tz - 512) * mult
    return False, bound - _tick_count(spacing)


def _ext_boundary(i: int, spacing: int) -> tuple:
    per = _max_tick_in_bitmap(spacing)
    m = abs(i) // per
    if i < 0 and abs(i) % per != 0:
        m += 1
    mn = per * m
    return (-mn, -mn + per) if i < 0 else (mn, mn + per)


def _ext_offset_in_bitmap(i: int, spacing: int) -> int:
    m = abs(i) % _max_tick_in_bitmap(spacing)
    off = m // _tick_count(spacing)
    if i < 0 and m != 0:
        off = TICK_ARRAY_BITMAP_SIZE - off
    return off


def _ext_bitmap(ext: dict, i: int, spacing: int) -> tuple:
    if not _check_valid_start(i, spacing):
        raise ОшибкаCLMM("InvalidTickIndex")
    per = _max_tick_in_bitmap(spacing)
    if -per <= i < per:
        raise ОшибкаCLMM("InvalidTickArrayBoundary")
    off = abs(i) // per - 1
    if i < 0 and abs(i) % per == 0:
        off -= 1
    return off, (ext["negative"] if i < 0 else ext["positive"])[off]


def _ext_check(ext: dict, i: int, spacing: int) -> tuple:
    _, bm = _ext_bitmap(ext, i, spacing)
    return bool(bm >> _ext_offset_in_bitmap(i, spacing) & 1), i


def _ext_next_from_one(ext: dict, last: int, spacing: int, zero_for_one: bool) -> tuple:
    mult = _tick_count(spacing)
    nxt = last - mult if zero_for_one else last + mult
    if nxt < get_array_start_index(MIN_TICK, spacing) or nxt > get_array_start_index(MAX_TICK, spacing):
        return False, nxt
    _, bm = _ext_bitmap(ext, nxt, spacing)
    lo, hi = _ext_boundary(nxt, spacing)
    off = _ext_offset_in_bitmap(nxt, spacing)
    if zero_for_one:
        x = (bm << (TICK_ARRAY_BITMAP_SIZE - 1 - off)) & _M512
        if x:
            return True, nxt - (512 - x.bit_length()) * mult
        return False, lo
    x = bm >> off
    if x:
        return True, nxt + ((x & -x).bit_length() - 1) * mult
    return False, hi - mult


def next_tick_array_index(pool: dict, ext: dict | None, last: int, zero_for_one: bool) -> int | None:
    sp = pool["tick_spacing"]
    last = get_array_start_index(last, sp)
    while True:
        ok, s_ = _next_default(pool["tick_array_bitmap"], last, sp, zero_for_one)
        if ok:
            return s_
        if sp >= 15:
            return None
        last = s_
        if ext is None:
            raise ОшибкаCLMM("MissingTickArrayBitmapExtensionAccount")
        ok, s_ = _ext_next_from_one(ext, last, sp, zero_for_one)
        if ok:
            return s_
        last = s_
        if last < MIN_TICK or last > MAX_TICK:
            return None


def first_tick_array_index(pool: dict, ext: dict | None, zero_for_one: bool) -> tuple:
    sp, tc = pool["tick_spacing"], pool["tick_current"]
    if _overflow_default(tc, sp):
        if ext is None:
            raise ОшибкаCLMM("MissingTickArrayBitmapExtensionAccount")
        ok, s_ = _ext_check(ext, get_array_start_index(tc, sp), sp)
    else:
        ok, s_ = _check_current_default(pool["tick_array_bitmap"], tc, sp)
    if ok:
        return True, s_
    n = next_tick_array_index(pool, ext, get_array_start_index(tc, sp), zero_for_one)
    if n is None:
        raise ОшибкаCLMM("InsufficientLiquidityForDirection")
    return False, n


def нужные_массивы(pool: dict, ext: dict | None, zero_for_one: bool, сколько: int = 3) -> list:
    """Старты tick arrays, которые своп пройдёт первыми (как их передаёт SDK: текущий и следующие по битовой карте)."""
    _, s_ = first_tick_array_index(pool, ext, zero_for_one)
    из_ = [s_]
    while len(из_) < сколько:
        n = next_tick_array_index(pool, ext, из_[-1], zero_for_one)
        if n is None:
            break
        из_.append(n)
    return из_


# ------------------------------------------------------------------ тики и лимитные ордера

def _is_init(t: dict) -> bool:
    return t["liquidity_gross"] > 0 or t["orders_amount"] > 0 or t["part_filled_orders_remaining"] > 0


def _has_lo(t: dict) -> bool:
    return t["orders_amount"] > 0 or t["part_filled_orders_remaining"] > 0


def _next_init_in_array(arr: dict, tick: int, spacing: int, zero_for_one: bool):
    if get_array_start_index(tick, spacing) != arr["start_tick_index"]:
        return None
    off = _tdiv(tick - arr["start_tick_index"], spacing)
    rng = range(off, -1, -1) if zero_for_one else range(off + 1, TICK_ARRAY_SIZE)
    for i in rng:
        if _is_init(arr["ticks"][i]):
            return i
    return None


def _first_init_in_array(arr: dict, zero_for_one: bool) -> int:
    rng = range(TICK_ARRAY_SIZE - 1, -1, -1) if zero_for_one else range(TICK_ARRAY_SIZE)
    for i in rng:
        if _is_init(arr["ticks"][i]):
            return i
    raise ОшибкаCLMM("InvalidTickArray")


def _lo_output(amount_in: int, price0: int, zfo: bool) -> int:
    return _mdf(amount_in, price0, Q64, U128_MAX) if zfo else _mdf(amount_in, Q64, price0, U128_MAX)


def _lo_input(amount_out: int, price0: int, zfo: bool) -> int:
    return _mdc(amount_out, price0, Q64, U128_MAX) if zfo else _mdc(amount_out, Q64, price0, U128_MAX)


def match_limit_order(t: dict, swap_amount: int, zfo: bool, fee_rate: int, fee_on_input: bool, sqrt_p: int) -> dict:
    """match_limit_order_with_sqrt_price (tick_array.rs), exact_in. Меняет t (копию тика)."""
    D = FEE_RATE_DENOMINATOR_VALUE
    r = {"amount_in": 0, "amount_out": 0, "amm_fee_amount": 0}
    total = t["orders_amount"] + t["part_filled_orders_remaining"]
    if swap_amount == 0 or total == 0:
        return r
    price0 = get_price_from_sqrt_price(sqrt_p, not zfo)
    if fee_on_input:
        r["amm_fee_amount"] = _mdc(swap_amount, fee_rate, D)
        r["amount_in"] = swap_amount - r["amm_fee_amount"]
    else:
        r["amount_in"] = swap_amount
    matched = _lo_output(r["amount_in"], price0, zfo)
    if matched > total:
        r["amount_out"] = total
        r["amount_in"] = _lo_input(total, price0, not zfo)
        if r["amount_in"] > U64_MAX:
            raise ОшибкаCLMM("CalculateOverflow")
        if fee_on_input:
            r["amm_fee_amount"] = _mdc(r["amount_in"], fee_rate, D - fee_rate)
    else:
        r["amount_out"] = matched
    part = 0
    if t["part_filled_orders_remaining"] > 0:
        part = min(t["part_filled_orders_remaining"], r["amount_out"])
        if part > 0:
            t["unfilled_ratio_x64"] = _mdf(t["unfilled_ratio_x64"], t["part_filled_orders_remaining"] - part,
                                           t["part_filled_orders_remaining"], U128_MAX)
        t["part_filled_orders_remaining"] -= part
    rest = max(0, r["amount_out"] - part)
    if rest > 0:
        if t["part_filled_orders_remaining"] != 0 or t["orders_amount"] < rest:
            raise ОшибкаCLMM("InvalidLimitOrderAmount")
        t["order_phase"] += 1
        t["unfilled_ratio_x64"] = _mdf(Q64, t["orders_amount"] - rest, t["orders_amount"], U128_MAX)
        t["part_filled_orders_remaining"] = t["orders_amount"] - rest
        t["orders_amount"] = 0
    if not fee_on_input:
        r["amm_fee_amount"] = _mdc(r["amount_out"], fee_rate, D)
        r["amount_out"] -= r["amm_fee_amount"]
    return r


# ------------------------------------------------------------------ динамическая комиссия

def _ts_index(tick: int, spacing: int) -> int:
    return _tdiv(tick, spacing) if (_tmod(tick, spacing) == 0 or tick >= 0) else _tdiv(tick, spacing) - 1


def _dfi_update_reference(dfi: dict, idx: int, ts: int) -> None:
    dt = max(0, ts - dfi["last_update_timestamp"])
    if dt < dfi["filter_period"]:
        return
    if dt < dfi["decay_period"]:
        dfi["tick_spacing_index_reference"] = idx
        dfi["volatility_reference"] = dfi["volatility_accumulator"] * dfi["reduction_factor"] // REDUCTION_FACTOR_DENOMINATOR
    else:
        dfi["tick_spacing_index_reference"] = idx
        dfi["volatility_reference"] = 0
    dfi["last_update_timestamp"] = ts


def _dfi_update_va(dfi: dict, idx: int) -> None:
    v = dfi["volatility_reference"] + abs(dfi["tick_spacing_index_reference"] - idx) * VOLATILITY_ACCUMULATOR_SCALE
    dfi["volatility_accumulator"] = min(v, dfi["max_volatility_accumulator"])


def _dynamic_fee_rate(dfi: dict, spacing: int) -> int:
    crossed = (dfi["volatility_accumulator"] * spacing) & 0xFFFFFFFF
    sq = crossed * crossed
    den = DYNAMIC_FEE_CONTROL_DENOMINATOR * VOLATILITY_ACCUMULATOR_SCALE * VOLATILITY_ACCUMULATOR_SCALE
    return min(_ceil_div(dfi["dynamic_fee_control"] * sq, den), MAX_FEE_RATE_NUMERATOR)


# ------------------------------------------------------------------ котировка

def котировка_точный_вход(pool: dict, config: dict, массивы: dict, ext: dict | None, amount_in: int,
                          zero_for_one: bool, ts: int, sqrt_price_limit_x64: int = 0) -> dict:
    """swap_internal (exact_in): массивы -- {start_tick_index: разобранный TickArrayState}; ts -- unix-время блока.
    Возвращает amount_out (сколько хранилище выхода отдаёт), потреблённый вход, комиссию, цену/тик после."""
    import copy  # noqa: PLC0415
    if amount_in == 0:
        raise ОшибкаCLMM("ZeroAmountSpecified")
    if pool["status"] & (1 << 4):
        raise ОшибкаCLMM("NotApproved: своп выключен")
    sp = pool["tick_spacing"]
    lim = sqrt_price_limit_x64 or (MIN_SQRT_PRICE_X64 + 1 if zero_for_one else MAX_SQRT_PRICE_X64 - 1)
    if zero_for_one and not (MIN_SQRT_PRICE_X64 < lim < pool["sqrt_price_x64"]):
        raise ОшибкаCLMM("SqrtPriceLimitOverflow")
    if not zero_for_one and not (pool["sqrt_price_x64"] < lim < MAX_SQRT_PRICE_X64):
        raise ОшибкаCLMM("SqrtPriceLimitOverflow")
    pool = dict(pool)
    first_contains, cur_start = first_tick_array_index(pool, ext, zero_for_one)
    if cur_start not in массивы:
        raise ОшибкаCLMM(f"нет tick array {cur_start}")
    арр = {k: copy.deepcopy(v) for k, v in массивы.items()}
    tac = арр[cur_start]
    fee_on = pool["fee_on"]
    fee_on_input = {0: True, 1: zero_for_one, 2: not zero_for_one}.get(fee_on, True)
    fee_on_token0 = {0: zero_for_one, 1: True, 2: False}.get(fee_on, zero_for_one)
    base_fee = config["trade_fee_rate"]
    pfr, ffr = config["protocol_fee_rate"], config["fund_fee_rate"]
    dfi = copy.deepcopy(pool["dynamic_fee_info"])
    st = {"rem": amount_in, "calc": 0, "p": pool["sqrt_price_x64"], "tick": pool["tick_current"],
          "L": pool["liquidity"], "lp": 0, "prot": 0, "fund": 0, "p_next": 0, "tick_next": 0, "tsi": 0}
    if dfi:
        st["tsi"] = _ts_index(st["tick"], sp)
        _dfi_update_reference(dfi, st["tsi"], ts)
    шагов, тиков_пересечено, массивов = 0, 0, {cur_start}
    лимитных = 0

    def total_fee() -> int:
        if dfi:
            return min(base_fee + _dynamic_fee_rate(dfi, sp), MAX_FEE_RATE_NUMERATOR)
        return base_fee

    def apply(ain, aout, fee):
        consumed = ain + fee if fee_on_input else ain
        if consumed > st["rem"]:
            raise ОшибкаCLMM("CalculateOverflow: потреблено больше остатка")
        st["rem"] -= consumed
        st["calc"] += aout
        pd = fee * pfr // FEE_RATE_DENOMINATOR_VALUE if pfr else 0
        fd = fee * ffr // FEE_RATE_DENOMINATOR_VALUE if ffr else 0
        st["prot"] += pd
        st["fund"] += fd
        rest = fee - pd - fd
        if st["L"] > 0:
            st["lp"] += rest
        else:
            st["prot"] += rest

    while st["rem"] != 0 and st["p"] != lim:
        i = _next_init_in_array(tac, st["tick"], sp, zero_for_one)
        if i is None:
            if not first_contains:
                first_contains = True
                i = _first_init_in_array(tac, zero_for_one)
            else:
                n = next_tick_array_index(pool, ext, cur_start, zero_for_one)
                if n is None:
                    raise ОшибкаCLMM("LiquidityInsufficient")
                if n not in арр:
                    raise ОшибкаCLMM(f"нет tick array {n}")
                tac, cur_start = арр[n], n
                массивов.add(n)
                i = _first_init_in_array(tac, zero_for_one)
        tk = tac["ticks"][i]
        if not _is_init(tk):
            raise ОшибкаCLMM("тик не инициализирован")
        # get_target_price_based_on_next_tick
        st["tick_next"] = min(max(tk["tick"], MIN_TICK), MAX_TICK)
        st["p_next"] = get_sqrt_price_at_tick(st["tick_next"])
        target = lim if ((zero_for_one and st["p_next"] < lim) or (not zero_for_one and st["p_next"] > lim)) else st["p_next"]
        if zero_for_one:
            if not (st["tick"] >= st["tick_next"] and st["p"] >= st["p_next"] and st["p"] >= target):
                raise ОшибкаCLMM("направление: zero_for_one")
        elif not (st["tick_next"] > st["tick"] and st["p_next"] >= st["p"] and target >= st["p"]):
            raise ОшибкаCLMM("направление: one_for_zero")
        L_next = st["L"]
        while True:
            шагов += 1
            if dfi:
                _dfi_update_va(dfi, st["tsi"])
            fr = total_fee()
            # get_spacing_bounded_price
            bounded_tick = None
            if dfi and not (st["L"] == 0 or dfi["volatility_accumulator"] == dfi["max_volatility_accumulator"]):
                skipped = False
                bt = st["tsi"] * sp if zero_for_one else (st["tsi"] + 1) * sp
                bt = min(max(bt, MIN_TICK), MAX_TICK)
                bp = get_sqrt_price_at_tick(bt)
                if zero_for_one:
                    bounded, bounded_tick = (target, None) if target > bp else (bp, bt)
                else:
                    bounded, bounded_tick = (target, None) if target < bp else (bp, bt)
            else:
                skipped, bounded = True, target
            if st["p"] != bounded:
                r = compute_swap(st["p"], bounded, st["L"], st["rem"], fr, True, zero_for_one, fee_on_input)
                apply(r["amount_in"], r["amount_out"], r["fee_amount"])
            else:
                r = {"sqrt_price_next_x64": bounded, "amount_in": 0, "amount_out": 0, "fee_amount": 0}
            unfilled_before = tk["orders_amount"] + tk["part_filled_orders_remaining"]
            if st["p_next"] == r["sqrt_price_next_x64"]:
                lo = match_limit_order(tk, st["rem"], zero_for_one, fr, fee_on_input, st["p_next"])
                if lo["amount_in"] or lo["amount_out"] or lo["amm_fee_amount"]:
                    лимитных += 1
                    apply(lo["amount_in"], lo["amount_out"], lo["amm_fee_amount"])
                if not _is_init(tk):
                    tac["initialized_tick_count"] -= 1
                    if tac["initialized_tick_count"] == 0:
                        # flip_tick_array_bit: только битовая карта пула (массивы вне неё -- в расширении, флаг)
                        if not _overflow_default(tac["start_tick_index"], sp):
                            off = _tdiv(tac["start_tick_index"], _tick_count(sp)) + TICK_ARRAY_BITMAP_SIZE
                            pool["tick_array_bitmap"] ^= 1 << off
                        elif ext is not None:
                            o, _ = _ext_bitmap(ext, tac["start_tick_index"], sp)
                            ext = copy.deepcopy(ext)
                            ключ = "negative" if tac["start_tick_index"] < 0 else "positive"
                            ext[ключ][o] ^= 1 << _ext_offset_in_bitmap(tac["start_tick_index"], sp)
                if tk["liquidity_gross"] > 0 and not _has_lo(tk):
                    ln = tk["liquidity_net"]
                    if zero_for_one:
                        ln = -ln
                    L_next = add_delta(st["L"], ln)
                    тиков_пересечено += 1
                has_lo = _has_lo(tk)
                st["tick"] = st["tick_next"] - 1 if ((zero_for_one and not has_lo) or (not zero_for_one and has_lo)) \
                    else st["tick_next"]
            elif st["p"] != r["sqrt_price_next_x64"]:
                if bounded_tick is not None and r["sqrt_price_next_x64"] == bounded:
                    st["tick"] = bounded_tick
                else:
                    st["tick"] = get_tick_at_sqrt_price(r["sqrt_price_next_x64"])
            st["p"] = r["sqrt_price_next_x64"]
            # update_dynamic_fee_index
            if dfi:
                if skipped:
                    ti = st["tick_next"] if st["p"] == st["p_next"] else st["tick"]
                    idx = _ts_index(ti, sp)
                    if not zero_for_one and _tmod(ti, sp) == 0:
                        idx -= 1
                    st["tsi"] = idx
                    if dfi["volatility_accumulator"] != dfi["max_volatility_accumulator"]:
                        _dfi_update_va(dfi, st["tsi"])
                st["tsi"] += -1 if zero_for_one else 1
            if st["rem"] == 0 or st["p"] == target:
                unfilled_after = tk["orders_amount"] + tk["part_filled_orders_remaining"]
                if st["rem"] != 0 and unfilled_after != unfilled_before and unfilled_after != 0:
                    raise ОшибкаCLMM("лимитный ордер: остаток при несъеденном входе")
                break
        st["L"] = L_next
    потреблено = amount_in - st["rem"]
    return {"amount_out": st["calc"], "amount_in_consumed": потреблено, "остаток_входа": st["rem"],
            "fee": st["lp"] + st["prot"] + st["fund"], "lp_fee": st["lp"], "protocol_fee": st["prot"],
            "fund_fee": st["fund"], "fee_on_token0": fee_on_token0, "fee_on_input": fee_on_input,
            "sqrt_price_x64_после": st["p"], "tick_после": st["tick"], "liquidity_после": st["L"],
            "шагов": шагов, "тиков_пересечено": тиков_пересечено, "массивов": sorted(массивов),
            "лимитных_ордеров": лимитных, "fee_rate_последний": total_fee(),
            "динамическая": dfi is not None}


def self_test() -> int:
    ош = []

    def chk(имя, ок, факт=""):
        print(f"  [{'ok' if ок else 'СБОЙ'}] {имя}" + ("" if ок else f" -- {факт}"))
        if not ок:
            ош.append(имя)
    # значения -- из тестов исходников (tick_math.rs / swap_math.rs raydium-clmm) и границы из констант
    chk("sqrt_price(MIN_TICK) = MIN_SQRT_PRICE_X64", get_sqrt_price_at_tick(MIN_TICK) == MIN_SQRT_PRICE_X64,
        get_sqrt_price_at_tick(MIN_TICK))
    chk("sqrt_price(MAX_TICK) = MAX_SQRT_PRICE_X64", get_sqrt_price_at_tick(MAX_TICK) == MAX_SQRT_PRICE_X64,
        get_sqrt_price_at_tick(MAX_TICK))
    chk("sqrt_price(0) = 2^64", get_sqrt_price_at_tick(0) == Q64)
    ок = all(get_tick_at_sqrt_price(get_sqrt_price_at_tick(t)) == t for t in
             list(range(-2000, 2000, 7)) + [MIN_TICK, MAX_TICK - 1, -443000, 443000, 12345, -98765])
    chk("tick(sqrt_price(t)) = t", ок)
    ок = all(get_tick_at_sqrt_price(get_sqrt_price_at_tick(t) + 1) == t and
             get_tick_at_sqrt_price(get_sqrt_price_at_tick(t) - 1) == t - 1 for t in range(-3000, 3000, 13))
    chk("tick на соседних ценах", ок)
    chk("_tdiv/_tmod как в Rust", (_tdiv(-7, 2), _tmod(-7, 2), _tdiv(7, -2)) == (-3, -1, -3))
    chk("get_array_start_index(-1, 10) = -600", get_array_start_index(-1, 10) == -600 and get_array_start_index(599, 10) == 0)
    print(f"самопроверка порта CLMM: {'СБОЙ ' + str(len(ош)) if ош else 'всё сошлось'}")
    return 1 if ош else 0


if __name__ == "__main__":
    raise SystemExit(self_test())
