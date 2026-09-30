#!/usr/bin/env python3
"""Точная котировка exact_in для Orca Whirlpool (whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc) -- офлайн, без сети.

Перенос из исходников orca-so/whirlpools (programs/whirlpool/src, коммит f4b99e79 от 29.09.2026):
  * manager/swap_manager.rs -- swap: цикл по инициализированным тикам последовательности из 3 tick arrays;
  * math/swap_math.rs -- compute_swap; math/token_math.rs -- get_amount_delta_a/b (try_*), get_next_sqrt_price;
    math/tick_math.rs -- sqrt_price_from_tick_index (положительные тики -- множители Q96, отрицательные -- Q64),
    tick_index_from_sqrt_price (14 бит точности);
  * manager/fee_rate_manager.rs, state/oracle.rs -- адаптивная комиссия (AdaptiveFeeConstants / Variables,
    update_reference, update_volatility_accumulator, get_bounded_sqrt_price_target, advance_tick_group[_after_skip]);
  * util/sparse_swap.rs -- последовательность массивов: 3 старта от текущего тика по направлению (для b→a со
    сдвигом, если tick + spacing ≥ конец массива); несозданный массив -- пустой (ZeroedTickArray);
  * util/swap_tick_sequence.rs, state/fixed_tick_array.rs, state/dynamic_tick_array.rs, state/tick.rs.
Раскладки (repr(C, packed) / borsh): Whirlpool -- tick_spacing @41, fee_rate @45, protocol_fee_rate @47,
liquidity @49, sqrt_price @65, tick_current_index @81, token_mint_a @101, token_vault_a @133, token_mint_b @181, token_vault_b @213;
фиксированный массив 9988 байт (88 тиков по 113), динамический -- битовая карта u128 и записи 1 или 113 байт;
Oracle -- trade_enable_timestamp @40, константы @48, переменные @82.
Вход и выход -- сырые единицы пула (налог Token-2022 -- снаружи).
"""
from __future__ import annotations

import struct

ПРОГРАММА = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
MIN_TICK, MAX_TICK = -443636, 443636
MIN_SQRT_PRICE_X64 = 4295048016
MAX_SQRT_PRICE_X64 = 79226673515401279992447579055
U64_MAX, U128_MAX = (1 << 64) - 1, (1 << 128) - 1
FEE_RATE_MUL_VALUE = 1_000_000
PROTOCOL_FEE_RATE_MUL_VALUE = 10_000
TICK_ARRAY_SIZE = 88
FEE_RATE_HARD_LIMIT = 100_000
VOLATILITY_ACCUMULATOR_SCALE_FACTOR = 10_000
REDUCTION_FACTOR_DENOMINATOR = 10_000
ADAPTIVE_FEE_CONTROL_FACTOR_DENOMINATOR = 100_000
MAX_REFERENCE_AGE = 3_600


class ОшибкаWP(Exception):
    pass


def _tdiv(a: int, b: int) -> int:
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def _tmod(a: int, b: int) -> int:
    return a - _tdiv(a, b) * b


def _ceil(a: int, b: int) -> int:
    return -(-a // b)


def floor_division(a: int, b: int) -> int:
    return a // b                                   # как floor_division (int_division_math.rs)


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


# ------------------------------------------------------------------ счета

def разобрать_пул(d: bytes) -> dict:
    return {"tick_spacing": struct.unpack_from("<H", d, 41)[0], "fee_rate": struct.unpack_from("<H", d, 45)[0],
            "protocol_fee_rate": struct.unpack_from("<H", d, 47)[0], "liquidity": _u128(d, 49),
            "sqrt_price": _u128(d, 65), "tick_current_index": struct.unpack_from("<i", d, 81)[0],
            "token_mint_a": _pk(d[101:133]), "token_vault_a": _pk(d[133:165]),
            "token_mint_b": _pk(d[181:213]), "token_vault_b": _pk(d[213:245])}


def разобрать_оракул(d: bytes | None) -> dict | None:
    """None -- счёта нет (пул без адаптивной комиссии: OracleAccessor.get_adaptive_fee_info → None)."""
    if not d or len(d) < 126:
        return None
    fp, dp, rf, afcf, mva, tgs, mst = struct.unpack_from("<HHHIIHH", d, 48)
    lru, lms, vr, tgir, va = struct.unpack_from("<QQIiI", d, 82)
    return {"trade_enable_timestamp": struct.unpack_from("<Q", d, 40)[0],
            "c": {"filter_period": fp, "decay_period": dp, "reduction_factor": rf, "adaptive_fee_control_factor": afcf,
                  "max_volatility_accumulator": mva, "tick_group_size": tgs, "major_swap_threshold_ticks": mst},
            "v": {"last_reference_update_timestamp": lru, "last_major_swap_timestamp": lms, "volatility_reference": vr,
                  "tick_group_index_reference": tgir, "volatility_accumulator": va}}


def разобрать_массив(d: bytes) -> dict:
    """Фиксированный (9988 байт) или динамический; тики -- {смещение: (liquidity_net, liquidity_gross)}."""
    тики = {}
    if len(d) == 9988:
        start = struct.unpack_from("<i", d, 8)[0]
        for i in range(TICK_ARRAY_SIZE):
            o = 12 + 113 * i
            if d[o]:
                тики[i] = (_i128(d, o + 1), _u128(d, o + 17))
        return {"start_tick_index": start, "whirlpool": _pk(d[9956:9988]), "ticks": тики, "вид": "фиксированный"}
    start = struct.unpack_from("<i", d, 8)[0]
    bm = _u128(d, 8 + 36)
    o = 8 + 52
    for i in range(TICK_ARRAY_SIZE):
        if d[o]:
            тики[i] = (_i128(d, o + 1), _u128(d, o + 17))
            o += 113
        else:
            o += 1
    if set(тики) != {i for i in range(TICK_ARRAY_SIZE) if bm >> i & 1}:
        raise ОшибкаWP("динамический массив: битовая карта не сходится с записями")
    return {"start_tick_index": start, "whirlpool": _pk(d[12:44]), "ticks": тики, "вид": "динамический"}


def адрес_массива(пул: str, старт: int) -> str:
    from solders.pubkey import Pubkey  # noqa: PLC0415
    pda, _ = Pubkey.find_program_address([b"tick_array", bytes(Pubkey.from_string(пул)), str(старт).encode()],
                                         Pubkey.from_string(ПРОГРАММА))
    return str(pda)


def адрес_оракула(пул: str) -> str:
    from solders.pubkey import Pubkey  # noqa: PLC0415
    pda, _ = Pubkey.find_program_address([b"oracle", bytes(Pubkey.from_string(пул))], Pubkey.from_string(ПРОГРАММА))
    return str(pda)


# ------------------------------------------------------------------ tick_math

_ПОЛ = [79236085330515764027303304731, 79244008939048815603706035061, 79259858533276714757314932305,
        79291567232598584799939703904, 79355022692464371645785046466, 79482085999252804386437311141,
        79736823300114093921829183326, 80248749790819932309965073892, 81282483887344747381513967011,
        83390072131320151908154831281, 87770609709833776024991924138, 97234110755111693312479820773,
        119332217159966728226237229890, 179736315981702064433883588727, 407748233172238350107850275304,
        2098478828474011932436660412517, 55581415166113811149459800483533, 38992368544603139932233054999993551]
_ОТР = [18444899583751176498, 18443055278223354162, 18439367220385604838, 18431993317065449817,
        18417254355718160513, 18387811781193591352, 18329067761203520168, 18212142134806087854,
        17980523815641551639, 17526086738831147013, 16651378430235024244, 15030750278693429944,
        12247334978882834399, 8131365268884726200, 3584323654723342297, 696457651847595233, 26294789957452057,
        37481735321082]


def sqrt_price_from_tick_index(tick: int) -> int:
    if tick >= 0:
        r = 79232123823359799118286999567 if tick & 1 else 79228162514264337593543950336
        for i, c in enumerate(_ПОЛ, 1):
            if tick & (1 << i):
                r = (r * c) >> 96
                if r > U128_MAX:
                    raise ОшибкаWP("mul_shift_96: переполнение")
        return r >> 32
    a = -tick
    r = 18445821805675392311 if a & 1 else 18446744073709551616
    for i, c in enumerate(_ОТР, 1):
        if a & (1 << i):
            r = ((r * c) & U128_MAX) >> 64
    return r


def tick_index_from_sqrt_price(p: int) -> int:
    msb = p.bit_length() - 1
    li = (msb - 64) << 32
    bit, prec, frac = 0x8000_0000_0000_0000, 0, 0
    r = p >> (msb - 63) if msb >= 64 else p << (63 - msb)
    while bit > 0 and prec < 14:
        r = (r * r) & U128_MAX
        more = r >> 127
        r >>= 63 + more
        frac += bit * more
        bit >>= 1
        prec += 1
    lg = (li + (frac >> 32)) * 59543866431248
    lo = (lg - 184467440737095516) >> 64
    hi = (lg + 15793534762490258745) >> 64
    if lo == hi:
        return lo
    return hi if sqrt_price_from_tick_index(hi) <= p else lo


# ------------------------------------------------------------------ token_math / swap_math

def try_delta_a(p0: int, p1: int, L: int, up: bool):
    lo_, hi_ = min(p0, p1), max(p0, p1)
    num = L * (hi_ - lo_)
    if num >= 1 << 192:                              # checked_shift_word_left: U256 << 64 переполняется
        raise ОшибкаWP("MultiplicationOverflow")
    num <<= 64
    den = hi_ * lo_
    q, rem = divmod(num, den)
    if up and rem:
        q += 1
    if q > U128_MAX or q > U64_MAX:
        return None                                  # ExceedsMax
    return q


def try_delta_b(p0: int, p1: int, L: int, up: bool):
    lo_, hi_ = min(p0, p1), max(p0, p1)
    n1 = hi_ - lo_
    if L == 0 or n1 == 0:
        return 0
    p = L * n1
    if p > U128_MAX:
        return None
    res = (p >> 64) & U64_MAX
    rnd = up and (p & U64_MAX) > 0
    if rnd and res == U64_MAX:
        return None
    return res + 1 if rnd else res


def _delta(fn, *a):
    v = fn(*a)
    if v is None:
        raise ОшибкаWP("TokenMaxExceeded")
    return v


def next_sqrt_price(p: int, L: int, amount: int, is_input: bool, a_to_b: bool) -> int:
    if is_input == a_to_b:
        if amount == 0:
            return p
        prod = p * amount
        num = L * p
        if num >= 1 << 192:
            raise ОшибкаWP("MultiplicationOverflow")
        num <<= 64
        ls = L << 64
        if not is_input and ls <= prod:
            raise ОшибкаWP("DivideByZero")
        den = ls + prod if is_input else ls - prod
        price = _ceil(num, den)
        if price > U128_MAX:
            raise ОшибкаWP("переполнение цены")
        if price < MIN_SQRT_PRICE_X64:
            raise ОшибкаWP("TokenMinSubceeded")
        if price > MAX_SQRT_PRICE_X64:
            raise ОшибкаWP("TokenMaxExceeded")
        return price
    ax64 = amount << 64
    delta = ax64 // L if is_input else _ceil(ax64, L)
    v = p + delta if is_input else p - delta
    if v < 0 or v > U128_MAX:
        raise ОшибкаWP("SqrtPriceOutOfBounds")
    return v


def compute_swap(rem: int, fee_rate: int, L: int, cur: int, target: int, is_input: bool, a_to_b: bool) -> dict:
    fixed_is_a = a_to_b == is_input
    init_fixed = (try_delta_a if fixed_is_a else try_delta_b)(cur, target, L, is_input)
    calc = rem
    if is_input:
        calc = rem * (FEE_RATE_MUL_VALUE - fee_rate) // FEE_RATE_MUL_VALUE
    nxt = target if (init_fixed is not None and init_fixed <= calc) else next_sqrt_price(cur, L, calc, is_input, a_to_b)
    is_max = nxt == target
    unfixed = _delta(try_delta_b if fixed_is_a else try_delta_a, cur, nxt, L, not is_input)
    fixed = _delta(try_delta_a if fixed_is_a else try_delta_b, cur, nxt, L, is_input) \
        if (not is_max or init_fixed is None) else init_fixed
    ain, aout = (fixed, unfixed) if is_input else (unfixed, fixed)
    if not is_input and aout > rem:
        aout = rem
    fee = rem - ain if (is_input and not is_max) else _ceil(ain * fee_rate, FEE_RATE_MUL_VALUE - fee_rate)
    if fee > U64_MAX:
        raise ОшибкаWP("переполнение комиссии")
    return {"amount_in": ain, "amount_out": aout, "next_price": nxt, "fee_amount": fee}


# ------------------------------------------------------------------ адаптивная комиссия

class _Комиссия:
    def __init__(self, a_to_b: bool, tick: int, ts: int, static: int, ор: dict | None):
        self.a_to_b, self.static = a_to_b, static
        self.адапт = ор is not None
        if not self.адапт:
            return
        import copy  # noqa: PLC0415
        self.c = dict(ор["c"])
        self.v = copy.deepcopy(ор["v"])
        self.tgi = floor_division(tick, self.c["tick_group_size"])
        self._update_reference(self.tgi, ts)
        d = _ceil(self.c["max_volatility_accumulator"] - self.v["volatility_reference"], VOLATILITY_ACCUMULATOR_SCALE_FACTOR)
        lo_i, hi_i = self.v["tick_group_index_reference"] - d, self.v["tick_group_index_reference"] + d
        tgs = self.c["tick_group_size"]
        lo_t, hi_t = lo_i * tgs, hi_i * tgs + tgs
        self.lo = (lo_i, sqrt_price_from_tick_index(lo_t)) if lo_t > MIN_TICK else None
        self.hi = (hi_i, sqrt_price_from_tick_index(hi_t)) if hi_t < MAX_TICK else None

    def _update_reference(self, tgi: int, ts: int) -> None:
        v, c = self.v, self.c
        mx = max(v["last_reference_update_timestamp"], v["last_major_swap_timestamp"])
        if ts < mx:
            raise ОшибкаWP("InvalidTimestamp")
        if ts - v["last_reference_update_timestamp"] > MAX_REFERENCE_AGE:
            v.update(tick_group_index_reference=tgi, volatility_reference=0, last_reference_update_timestamp=ts)
            return
        el = ts - mx
        if el < c["filter_period"]:
            return
        if el < c["decay_period"]:
            v.update(tick_group_index_reference=tgi, last_reference_update_timestamp=ts,
                     volatility_reference=v["volatility_accumulator"] * c["reduction_factor"] // REDUCTION_FACTOR_DENOMINATOR)
        else:
            v.update(tick_group_index_reference=tgi, volatility_reference=0, last_reference_update_timestamp=ts)

    def _uva(self, tgi: int) -> None:
        v = self.v["volatility_reference"] + abs(self.v["tick_group_index_reference"] - tgi) * VOLATILITY_ACCUMULATOR_SCALE_FACTOR
        self.v["volatility_accumulator"] = min(v, self.c["max_volatility_accumulator"])

    def update_va(self) -> None:
        if self.адапт:
            self._uva(self.tgi)

    def fee(self) -> int:
        if not self.адапт:
            return self.static
        crossed = (self.v["volatility_accumulator"] * self.c["tick_group_size"]) & 0xFFFFFFFF
        f = _ceil(self.c["adaptive_fee_control_factor"] * crossed * crossed,
                  ADAPTIVE_FEE_CONTROL_FACTOR_DENOMINATOR * VOLATILITY_ACCUMULATOR_SCALE_FACTOR ** 2)
        return min(self.static + min(f, FEE_RATE_HARD_LIMIT), FEE_RATE_HARD_LIMIT)

    def bounded(self, p: int, L: int) -> tuple:
        if not self.адапт:
            return p, False
        if self.c["adaptive_fee_control_factor"] == 0 or L == 0:
            return p, True
        if self.lo and self.tgi < self.lo[0]:
            return (p, True) if self.a_to_b else (min(p, self.lo[1]), True)
        if self.hi and self.tgi > self.hi[0]:
            return (max(p, self.hi[1]), True) if self.a_to_b else (p, True)
        tgs = self.c["tick_group_size"]
        bt = self.tgi * tgs if self.a_to_b else self.tgi * tgs + tgs
        bp = sqrt_price_from_tick_index(min(max(bt, MIN_TICK), MAX_TICK))
        return (max(p, bp), False) if self.a_to_b else (min(p, bp), False)

    def advance(self, skipped: bool, p: int, next_tick_p: int, next_tick: int) -> None:
        if not self.адапт:
            return
        if skipped:
            tgs = self.c["tick_group_size"]
            if p == next_tick_p:
                ti, on_b = next_tick, _tmod(next_tick, tgs) == 0
            else:
                ti = tick_index_from_sqrt_price(p)
                on_b = _tmod(ti, tgs) == 0 and p == sqrt_price_from_tick_index(ti)
            last = _tdiv(ti, tgs) - 1 if (on_b and not self.a_to_b) else floor_division(ti, tgs)
            if (self.a_to_b and last < self.tgi) or (not self.a_to_b and last > self.tgi):
                self.tgi = last
                self._uva(self.tgi)
        self.tgi += -1 if self.a_to_b else 1


# ------------------------------------------------------------------ последовательность массивов

def старты_последовательности(pool: dict, a_to_b: bool) -> list:
    """get_start_tick_indexes (sparse_swap.rs)."""
    tc, sp = pool["tick_current_index"], pool["tick_spacing"]
    n = TICK_ARRAY_SIZE * sp
    base = floor_division(tc, n) * n
    if a_to_b:
        off = (0, -1, -2)
    else:
        off = (1, 2, 3) if tc + sp >= base + n else (0, 1, 2)
    из_ = []
    for o in off:
        s_ = base + o * n
        if MIN_TICK <= s_ <= MAX_TICK:
            if _tmod(s_, n) == 0:
                из_.append(s_)
        elif s_ <= MIN_TICK and s_ == MIN_TICK - (_tmod(MIN_TICK, n) + n):
            из_.append(s_)
    return из_


def _offset(tick: int, start: int, sp: int) -> int:
    lhs = tick - start
    d, r = _tdiv(lhs, sp), _tmod(lhs, sp)
    return d - 1 if r < 0 else d


def _next_init(arr: dict, tick: int, sp: int, a_to_b: bool):
    lo_, hi_ = arr["start_tick_index"], arr["start_tick_index"] + TICK_ARRAY_SIZE * sp
    if not a_to_b:
        lo_, hi_ = lo_ - sp, hi_ - sp
    if not lo_ <= tick < hi_:
        raise ОшибкаWP("InvalidTickArraySequence")
    o = _offset(tick, arr["start_tick_index"], sp)
    if not a_to_b:
        o += 1
    while 0 <= o < TICK_ARRAY_SIZE:
        if o in arr["ticks"]:
            return o * sp + arr["start_tick_index"]
        o += -1 if a_to_b else 1
    return None


def котировка_точный_вход(pool: dict, массивы: dict, оракул: dict | None, amount: int, a_to_b: bool, ts: int,
                          sqrt_price_limit: int = 0) -> dict:
    """swap (swap_manager.rs), exact_in. массивы -- {start_tick_index: разобранный массив}; несозданный -- пустой."""
    import copy  # noqa: PLC0415
    lim = sqrt_price_limit or (MIN_SQRT_PRICE_X64 if a_to_b else MAX_SQRT_PRICE_X64)
    if not MIN_SQRT_PRICE_X64 <= lim <= MAX_SQRT_PRICE_X64:
        raise ОшибкаWP("SqrtPriceOutOfBounds")
    if (a_to_b and lim >= pool["sqrt_price"]) or (not a_to_b and lim <= pool["sqrt_price"]):
        raise ОшибкаWP("InvalidSqrtPriceLimitDirection")
    if amount == 0:
        raise ОшибкаWP("ZeroTradableAmount")
    if оракул and оракул["trade_enable_timestamp"] > ts:
        raise ОшибкаWP("торговля ещё не открыта")
    sp = pool["tick_spacing"]
    посл = [copy.deepcopy(массивы.get(s_) or {"start_tick_index": s_, "ticks": {}, "вид": "несозданный"})
            for s_ in старты_последовательности(pool, a_to_b)]
    rem, calc = amount, 0
    p, tick, L = pool["sqrt_price"], pool["tick_current_index"], pool["liquidity"]
    prot, fee_sum = 0, 0
    ai = 0
    fm = _Комиссия(a_to_b, pool["tick_current_index"], ts, pool["fee_rate"], оракул)
    шагов = пересечено = 0
    while rem > 0 and lim != p:
        # get_next_initialized_tick_index
        si, idx = tick, ai
        while True:
            if idx >= len(посл):
                raise ОшибкаWP("TickArraySequenceInvalidIndex")
            arr = посл[idx]
            n = _next_init(arr, si, sp, a_to_b)
            if n is not None:
                nai, ntick = idx, n
                break
            if a_to_b and arr["start_tick_index"] <= MIN_TICK:
                nai, ntick = idx, MIN_TICK
                break
            if not a_to_b and arr["start_tick_index"] + TICK_ARRAY_SIZE * sp > MAX_TICK:
                nai, ntick = idx, MAX_TICK
                break
            if idx + 1 == len(посл):
                nai = idx
                ntick = arr["start_tick_index"] if a_to_b else arr["start_tick_index"] + TICK_ARRAY_SIZE * sp - 1
                break
            si = arr["start_tick_index"] - 1 if a_to_b else arr["start_tick_index"] + TICK_ARRAY_SIZE * sp - 1
            idx += 1
        ntp = sqrt_price_from_tick_index(ntick)
        target = max(lim, ntp) if a_to_b else min(lim, ntp)
        while True:
            шагов += 1
            fm.update_va()
            fr = fm.fee()
            bt, skipped = fm.bounded(target, L)
            sc = compute_swap(rem, fr, L, p, bt, True, a_to_b)
            rem -= sc["amount_in"]
            rem -= sc["fee_amount"]
            if rem < 0:
                raise ОшибкаWP("AmountRemainingOverflow")
            calc += sc["amount_out"]
            fee_sum += sc["fee_amount"]
            if pool["protocol_fee_rate"] > 0:
                prot += sc["fee_amount"] * pool["protocol_fee_rate"] // PROTOCOL_FEE_RATE_MUL_VALUE
            if sc["next_price"] == ntp:
                arr = посл[nai]
                usable = (arr["start_tick_index"] <= ntick < arr["start_tick_index"] + TICK_ARRAY_SIZE * sp
                          and MIN_TICK <= ntick <= MAX_TICK and _tmod(ntick, sp) == 0)
                off = _offset(ntick, arr["start_tick_index"], sp)
                if usable and off >= 0 and off in arr["ticks"]:
                    ln = arr["ticks"][off][0]
                    L = L + (-ln if a_to_b else ln)
                    if L < 0 or L > U128_MAX:
                        raise ОшибкаWP("LiquidityOverflow/Underflow")
                    пересечено += 1
                ai = nai + 1 if ((a_to_b and off == 0) or (not a_to_b and off == TICK_ARRAY_SIZE - 1)) else nai
                tick = ntick - 1 if a_to_b else ntick
            elif sc["next_price"] != p:
                tick = tick_index_from_sqrt_price(sc["next_price"])
            p = sc["next_price"]
            fm.advance(skipped, p, ntp, ntick)
            if rem == 0 or p == target:
                break
    return {"amount_out": calc, "amount_in_consumed": amount - rem, "остаток_входа": rem, "fee": fee_sum,
            "lp_fee": fee_sum - prot, "protocol_fee": prot, "sqrt_price_после": p, "tick_после": tick,
            "liquidity_после": L, "шагов": шагов, "тиков_пересечено": пересечено,
            "массивы": [a["start_tick_index"] for a in посл], "виды_массивов": [a["вид"] for a in посл],
            "адаптивная": fm.адапт, "fee_rate_последний": fm.fee()}


def self_test() -> int:
    ош = []

    def chk(имя, ок, факт=""):
        print(f"  [{'ok' if ок else 'СБОЙ'}] {имя}" + ("" if ок else f" -- {факт}"))
        if not ок:
            ош.append(имя)
    chk("sqrt_price(MIN_TICK) = MIN_SQRT_PRICE_X64", sqrt_price_from_tick_index(MIN_TICK) == MIN_SQRT_PRICE_X64,
        sqrt_price_from_tick_index(MIN_TICK))
    chk("sqrt_price(MAX_TICK) = MAX_SQRT_PRICE_X64", sqrt_price_from_tick_index(MAX_TICK) == MAX_SQRT_PRICE_X64,
        sqrt_price_from_tick_index(MAX_TICK))
    chk("sqrt_price(0) = 2^64", sqrt_price_from_tick_index(0) == 1 << 64)
    ок = all(tick_index_from_sqrt_price(sqrt_price_from_tick_index(t)) == t
             for t in list(range(-3000, 3000, 7)) + [MIN_TICK, MAX_TICK - 1, -443000, 443000, 22222, -77777])
    chk("tick(sqrt_price(t)) = t", ок)
    chk("старты: a→b и b→a со сдвигом", старты_последовательности({"tick_current_index": 0, "tick_spacing": 64}, True)
        == [0, -5632, -11264] and старты_последовательности({"tick_current_index": 5570, "tick_spacing": 64}, False)
        == [5632, 11264, 16896], старты_последовательности({"tick_current_index": 5570, "tick_spacing": 64}, False))
    print(f"самопроверка порта Whirlpool: {'СБОЙ ' + str(len(ош)) if ош else 'всё сошлось'}")
    return 1 if ош else 0


if __name__ == "__main__":
    raise SystemExit(self_test())
