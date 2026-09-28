#!/usr/bin/env python3
"""Meteora DLMM (LBUZKhRx...): чтение пула и массивов корзин, котировка свопа с
точным входом -- перенос на Python официального кода Meteora (github.com/MeteoraAg/
dlmm-sdk, commons/src/quote.rs::quote_exact_in и extensions/{lb_pair,bin,bin_array,
bin_array_bitmap}.rs, math/*; IDL программы 0.12.0, ts-client/src/dlmm/idl/idl.json).
Целочисленная арифметика -- как в Rust (u128/U256 через int Python, округления те же).

Раскладки (bytemuck, repr(C), без неявных отступов; размеры сверены с IDL):
  LbPair 8 + 896 = 904 байта, BinArray 8 + 10128 = 10136, Bin 144,
  BinArrayBitmapExtension 8 + 32 + 768 + 768.

Использование (Code-1): после сделки источника прочитать LbPair и массивы корзин
(getMultipleAccounts) и посчитать выход нашей покупки:
    lb = разобрать_пул(данные_пула)
    массивы = {индекс: разобрать_массив(данные)} -- индексы из нужные_массивы(...)
    q = котировка_точный_вход(lb, пул_адрес, сумма_в, swap_for_y, массивы, время_блока, ext)
swap_for_y = True -- продаём X за Y (платим token_x). Налог Token-2022 -- через
аргументы налог_вход / налог_выход (функции «сумма -> сумма без налога»).
"""
from __future__ import annotations

import struct

ПРОГРАММА = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
DISC_LB_PAIR = bytes([33, 11, 49, 98, 181, 101, 177, 13])
DISC_BIN_ARRAY = bytes([92, 142, 92, 220, 5, 148, 70, 181])
DISC_BITMAP_EXT = bytes([80, 111, 124, 113, 55, 237, 18, 5])

# commons/src/constants.rs, math/u64x64_math.rs
BASIS_POINT_MAX = 10_000
MAX_BIN_PER_ARRAY = 70
MIN_BIN_ID, MAX_BIN_ID = -443636, 443636
MAX_FEE_RATE = 100_000_000
FEE_PRECISION = 1_000_000_000
BIN_ARRAY_BITMAP_SIZE = 512
EXTENSION_BINARRAY_BITMAP_SIZE = 12
LIMIT_ORDER_FEE_SHARE = 5000
SCALE_OFFSET = 64
ONE = 1 << SCALE_OFFSET
U128_MAX = (1 << 128) - 1
MAX_EXPONENTIAL = 0x80000


class ОшибкаDLMM(Exception):
    pass


def _pk(b: bytes) -> str:
    from solders.pubkey import Pubkey  # noqa: PLC0415
    return str(Pubkey.from_bytes(bytes(b)))


# ------------------------------------------------------------------ раскладки
def разобрать_пул(data: bytes) -> dict:
    if len(data) < 904 or data[:8] != DISC_LB_PAIR:
        raise ОшибкаDLMM(f"не LbPair (длина {len(data)}, дискриминатор {data[:8].hex()})")
    d = data[8:]
    u16 = lambda o: struct.unpack_from("<H", d, o)[0]  # noqa: E731
    u32 = lambda o: struct.unpack_from("<I", d, o)[0]  # noqa: E731
    i32 = lambda o: struct.unpack_from("<i", d, o)[0]  # noqa: E731
    i64 = lambda o: struct.unpack_from("<q", d, o)[0]  # noqa: E731
    u64 = lambda o: struct.unpack_from("<Q", d, o)[0]  # noqa: E731
    p = {"base_factor": u16(0), "filter_period": u16(2), "decay_period": u16(4), "reduction_factor": u16(6),
         "variable_fee_control": u32(8), "max_volatility_accumulator": u32(12), "min_bin_id": i32(16),
         "max_bin_id": i32(20), "protocol_share": u16(24), "base_fee_power_factor": d[26], "function_type": d[27],
         "collect_fee_mode": d[28]}
    v = {"volatility_accumulator": u32(32), "volatility_reference": u32(36), "index_reference": i32(40),
         "last_update_timestamp": i64(48)}
    награды = [d[256 + 144 * k: 256 + 144 * k + 32] for k in range(2)]
    return {"parameters": p, "v_parameters": v, "pair_type": d[67], "active_id": i32(68), "bin_step": u16(72),
            "status": d[74], "activation_type": d[78], "token_x_mint": _pk(d[80:112]), "token_y_mint": _pk(d[112:144]),
            "reserve_x": _pk(d[144:176]), "reserve_y": _pk(d[176:208]),
            "награды_пусты": all(r == bytes(32) for r in награды),
            "bin_array_bitmap": [u64(576 + 8 * k) for k in range(16)], "activation_point": u64(808),
            "token_mint_x_program_flag": d[872], "token_mint_y_program_flag": d[873], "version": d[874]}


def разобрать_массив(data: bytes) -> dict:
    if len(data) < 10136 or data[:8] != DISC_BIN_ARRAY:
        raise ОшибкаDLMM(f"не BinArray (длина {len(data)})")
    d = data[8:]
    index = struct.unpack_from("<q", d, 0)[0]
    корзины = []
    for k in range(MAX_BIN_PER_ARRAY):
        o = 48 + 144 * k
        ax, ay = struct.unpack_from("<QQ", d, o)
        lo, hi = struct.unpack_from("<QQ", d, o + 16)
        корзины.append({"amount_x": ax, "amount_y": ay, "price": lo | (hi << 64),
                        "open_order_amount": struct.unpack_from("<Q", d, o + 112)[0],
                        "processed_order_remaining_amount": struct.unpack_from("<Q", d, o + 128)[0],
                        "limit_order_ask_side": d[o + 140]})
    return {"index": index, "lb_pair": _pk(d[16:48]), "bins": корзины}


def разобрать_расширение(data: bytes) -> dict:
    if data[:8] != DISC_BITMAP_EXT:
        raise ОшибкаDLMM("не BinArrayBitmapExtension")
    d = data[8:]
    пол = [[struct.unpack_from("<Q", d, 32 + 64 * i + 8 * j)[0] for j in range(8)] for i in range(12)]
    отр = [[struct.unpack_from("<Q", d, 32 + 768 + 64 * i + 8 * j)[0] for j in range(8)] for i in range(12)]
    return {"positive": пол, "negative": отр}


# ------------------------------------------------------------------ математика
def _mul_div(x: int, y: int, den: int, вверх: bool) -> int:
    if den == 0:
        raise ОшибкаDLMM("деление на 0")
    q, r = divmod(x * y, den)
    q = q + 1 if (вверх and r) else q
    if q > U128_MAX:
        raise ОшибкаDLMM("переполнение u128")
    return q


def _to_u64(v: int) -> int:
    if v > (1 << 64) - 1:
        raise ОшибкаDLMM("переполнение u64")
    return v


def mul_shr(x, y, off, вверх):
    return _mul_div(x, y, 1 << off, вверх)


def shl_div(x, y, off, вверх):
    return _mul_div(x, 1 << off, y, вверх)


def pow_q64(base: int, exp: int) -> int:
    """math/u64x64_math.rs::pow -- битовое возведение в Q64.64 с теми же сдвигами."""
    invert = exp < 0
    if exp == 0:
        return 1 << 64
    e = -exp if invert else exp
    if e >= MAX_EXPONENTIAL:
        raise ОшибкаDLMM("показатель вне диапазона")
    sq = base
    result = ONE
    if sq >= result:
        sq = U128_MAX // sq
        invert = not invert
    for bit in range(19):                     # 0x1 .. 0x40000
        if bit > 0:
            sq = (sq * sq) >> SCALE_OFFSET
        if e & (1 << bit):
            result = (result * sq) >> SCALE_OFFSET
    if result == 0:
        raise ОшибкаDLMM("цена 0")
    if invert:
        result = U128_MAX // result
    return result


def цена_корзины(bin_id: int, bin_step: int) -> int:
    bps = (bin_step << SCALE_OFFSET) // BASIS_POINT_MAX
    return pow_q64(ONE + bps, bin_id)


def amount_out(amount_in: int, price: int, swap_for_y: bool, вверх: bool) -> int:
    return _to_u64(mul_shr(price, amount_in, SCALE_OFFSET, вверх) if swap_for_y
                   else shl_div(amount_in, price, SCALE_OFFSET, вверх))


def amount_in_для(amount_out_: int, price: int, swap_for_y: bool, вверх: bool) -> int:
    return _to_u64(shl_div(amount_out_, price, SCALE_OFFSET, вверх) if swap_for_y
                   else mul_shr(amount_out_, price, SCALE_OFFSET, вверх))


# ------------------------------------------------------------------ комиссии (lb_pair.rs)
def _base_fee(lb) -> int:
    p = lb["parameters"]
    return p["base_factor"] * lb["bin_step"] * 10 * (10 ** p["base_fee_power_factor"])


def _variable_fee(lb) -> int:
    p, v = lb["parameters"], lb["v_parameters"]
    if p["variable_fee_control"] > 0:
        sq = (v["volatility_accumulator"] * lb["bin_step"]) ** 2
        return (p["variable_fee_control"] * sq + 99_999_999_999) // 100_000_000_000
    return 0


def total_fee(lb) -> int:
    return min(_base_fee(lb) + _variable_fee(lb), MAX_FEE_RATE)


def compute_fee(lb, amount: int) -> int:
    r = total_fee(lb)
    den = FEE_PRECISION - r
    return _to_u64((amount * r + den - 1) // den)


def compute_fee_from_amount(lb, amount_with_fees: int) -> int:
    r = total_fee(lb)
    return _to_u64((amount_with_fees * r + FEE_PRECISION - 1) // FEE_PRECISION)


def update_references(lb, ts: int) -> None:
    p, v = lb["parameters"], lb["v_parameters"]
    elapsed = ts - v["last_update_timestamp"]
    if elapsed >= p["filter_period"]:
        v["index_reference"] = lb["active_id"]
        if elapsed < p["decay_period"]:
            v["volatility_reference"] = v["volatility_accumulator"] * p["reduction_factor"] // BASIS_POINT_MAX
        else:
            v["volatility_reference"] = 0


def update_volatility_accumulator(lb) -> None:
    p, v = lb["parameters"], lb["v_parameters"]
    delta = abs(v["index_reference"] - lb["active_id"])
    v["volatility_accumulator"] = min(v["volatility_reference"] + delta * BASIS_POINT_MAX, p["max_volatility_accumulator"])


def support_limit_order(lb) -> bool:
    ft = lb["parameters"]["function_type"]
    if ft == 2:        # LimitOrder
        return True
    if ft == 1:        # LiquidityMining
        return False
    if ft == 0:        # Undetermined
        return lb["награды_пусты"]
    return False


def fee_on_input(lb, swap_for_y: bool) -> bool:
    m = lb["parameters"]["collect_fee_mode"]
    if m == 0:         # InputOnly
        return True
    if m == 1:         # OnlyY
        return not swap_for_y
    return True


# ------------------------------------------------------------------ массивы корзин
def индекс_массива(bin_id: int) -> int:
    q = int(bin_id / MAX_BIN_PER_ARRAY)          # усечение к нулю, как div_rem
    if bin_id < 0 and bin_id % MAX_BIN_PER_ARRAY != 0:
        q -= 1
    return q


def границы_массива(index: int) -> tuple:
    lo = index * MAX_BIN_PER_ARRAY
    return lo, lo + MAX_BIN_PER_ARRAY - 1


def адрес_массива(пул: str, index: int) -> str:
    from solders.pubkey import Pubkey  # noqa: PLC0415
    pda, _ = Pubkey.find_program_address([b"bin_array", bytes(Pubkey.from_string(пул)), struct.pack("<q", index)],
                                         Pubkey.from_string(ПРОГРАММА))
    return str(pda)


def адрес_расширения(пул: str) -> str:
    from solders.pubkey import Pubkey  # noqa: PLC0415
    pda, _ = Pubkey.find_program_address([b"bitmap", bytes(Pubkey.from_string(пул))], Pubkey.from_string(ПРОГРАММА))
    return str(pda)


def _next_internal(lb, swap_for_y: bool, start: int) -> tuple:
    bm = 0
    for k, limb in enumerate(lb["bin_array_bitmap"]):
        bm |= limb << (64 * k)
    mask = (1 << 1024) - 1
    lo_id, hi_id = -BIN_ARRAY_BITMAP_SIZE, BIN_ARRAY_BITMAP_SIZE - 1
    off = start + BIN_ARRAY_BITMAP_SIZE
    if swap_for_y:
        sh = (bm << ((hi_id - lo_id) - off)) & mask
        if sh == 0:
            return lo_id - 1, False
        return start - (1024 - sh.bit_length()), True
    sh = bm >> off
    if sh == 0:
        return hi_id + 1, False
    return start + ((sh & -sh).bit_length() - 1), True


def _ext_iter(ext, start: int, end: int):
    def get_off(i):
        return (i // BIN_ARRAY_BITMAP_SIZE - 1) if i > 0 else ((-(i + 1)) // BIN_ARRAY_BITMAP_SIZE - 1)

    def off_in(i):
        return (i % BIN_ARRAY_BITMAP_SIZE) if i > 0 else ((-(i + 1)) % BIN_ARRAY_BITMAP_SIZE)

    def u512(limbs):
        return sum(l << (64 * k) for k, l in enumerate(limbs))

    def to_idx(o, bo, pos):
        return (o + 1) * BIN_ARRAY_BITMAP_SIZE + bo if pos else -((o + 1) * BIN_ARRAY_BITMAP_SIZE + bo) - 1

    m512 = (1 << 512) - 1
    lz = lambda x: 512 - x.bit_length()  # noqa: E731
    tz = lambda x: (x & -x).bit_length() - 1  # noqa: E731
    if start == end:
        o, bo = get_off(start), off_in(start)
        arr = ext["negative"] if start < 0 else ext["positive"]
        return start if (u512(arr[o]) >> bo) & 1 else None
    o, bo = get_off(start), off_in(start)
    pos = start >= 0
    arr = ext["positive"] if pos else ext["negative"]
    назад = (start < end) if not pos else not (start < end)
    rng = range(o, -1, -1) if назад else range(o, EXTENSION_BINARRAY_BITMAP_SIZE)
    for i in rng:
        b = u512(arr[i])
        if назад:
            if i == o:
                b = (b << (BIN_ARRAY_BITMAP_SIZE - bo - 1)) & m512
                if b == 0:
                    continue
                return to_idx(i, bo - lz(b), pos)
            if b == 0:
                continue
            return to_idx(i, BIN_ARRAY_BITMAP_SIZE - lz(b) - 1, pos)
        if i == o:
            b = b >> bo
            if b == 0:
                continue
            return to_idx(i, bo + tz(b), pos)
        if b == 0:
            continue
        return to_idx(i, tz(b), pos)
    return None


def _next_ext(ext, swap_for_y: bool, start: int) -> tuple:
    lo_id = -BIN_ARRAY_BITMAP_SIZE * (EXTENSION_BINARRAY_BITMAP_SIZE + 1)
    hi_id = BIN_ARRAY_BITMAP_SIZE * (EXTENSION_BINARRAY_BITMAP_SIZE + 1) - 1
    if start > 0:
        if swap_for_y:
            v = _ext_iter(ext, start, BIN_ARRAY_BITMAP_SIZE)
            return (v, True) if v is not None else (BIN_ARRAY_BITMAP_SIZE - 1, False)
        v = _ext_iter(ext, start, hi_id)
        if v is None:
            raise ОшибкаDLMM("нет массивов с ликвидностью")
        return v, True
    if swap_for_y:
        v = _ext_iter(ext, start, lo_id)
        if v is None:
            raise ОшибкаDLMM("нет массивов с ликвидностью")
        return v, True
    v = _ext_iter(ext, start, -BIN_ARRAY_BITMAP_SIZE - 1)
    return (v, True) if v is not None else (-BIN_ARRAY_BITMAP_SIZE, False)


def нужные_массивы(lb, swap_for_y: bool, сколько: int = 3, ext: dict | None = None) -> list:
    """quote.rs::get_bin_array_pubkeys_for_swap -- индексы массивов с ликвидностью по ходу свопа."""
    start = индекс_массива(lb["active_id"])
    out = []
    inc = -1 if swap_for_y else 1
    while len(out) < сколько:
        if start > BIN_ARRAY_BITMAP_SIZE - 1 or start < -BIN_ARRAY_BITMAP_SIZE:
            if ext is None:
                break
            try:
                nxt, есть = _next_ext(ext, swap_for_y, start)
            except ОшибкаDLMM:
                break
        else:
            nxt, есть = _next_internal(lb, swap_for_y, start)
        if есть:
            out.append(nxt)
            start = nxt + inc
        else:
            start = nxt
    return out


# ------------------------------------------------------------------ котировка
def _max_out(b, swap_for_y, lo) -> int:
    mm = b["amount_y"] if swap_for_y else b["amount_x"]
    if not lo:
        return mm
    ask = b["limit_order_ask_side"] != 0
    if (swap_for_y and not ask) or (not swap_for_y and ask):
        return mm + b["open_order_amount"] + b["processed_order_remaining_amount"]
    return mm


def _fill(b, amount, max_out, swap_for_y):
    if max_out == 0:
        return 0, amount, 0
    max_in = amount_in_для(max_out, b["price"], swap_for_y, True)
    if amount >= max_in:
        return max_in, amount - max_in, max_out
    return amount, 0, amount_out(amount, b["price"], swap_for_y, False)


def _fill_all(b, amount, swap_for_y, lo):
    mm = b["amount_y"] if swap_for_y else b["amount_x"]
    a_in, left, out = _fill(b, amount, mm, swap_for_y)
    mm_in, tot_in, tot_out = a_in, a_in, out
    if lo and left > 0:
        ask = b["limit_order_ask_side"] != 0
        open_, proc = ((b["open_order_amount"], b["processed_order_remaining_amount"])
                       if (swap_for_y and not ask) or (not swap_for_y and ask) else (0, 0))
        a2, left2, o2 = _fill(b, left, proc, swap_for_y)
        tot_in += a2
        tot_out += o2
        if left2 > 0:
            a3, _, o3 = _fill(b, left2, open_, swap_for_y)
            tot_in += a3
            tot_out += o3
    return tot_in, amount - tot_in, tot_out, mm_in


def _split_fee(fee, protocol_share, mm_in, tot_in):
    if tot_in == 0 or fee == 0:
        return 0, 0
    mm_fee = (fee * mm_in + tot_in - 1) // tot_in
    lo_total = fee - mm_fee
    lo_fee = lo_total * LIMIT_ORDER_FEE_SHARE // BASIS_POINT_MAX
    prot = (lo_total - lo_fee) + mm_fee * protocol_share // BASIS_POINT_MAX
    return fee - prot, prot


def _at_bin(b, lb, in_amount, swap_for_y, lo, foi):
    fee = 0
    excl_in = in_amount
    if foi:
        fee = compute_fee_from_amount(lb, in_amount)
        excl_in = in_amount - fee
    f_in, left, out, mm_in = _fill_all(b, excl_in, swap_for_y, lo)
    incl_in = in_amount
    if left > 0:
        excl_in -= left
        if foi:
            fee = compute_fee(lb, excl_in)
            incl_in = excl_in + fee
        else:
            incl_in = excl_in
    excl_out = out
    if not foi:
        fee = compute_fee_from_amount(lb, out)
        excl_out = out - fee
    _, prot = _split_fee(fee, lb["parameters"]["protocol_share"], mm_in, f_in)
    return incl_in, excl_out, fee, prot


def котировка_точный_вход(lb: dict, пул: str, сумма_в: int, swap_for_y: bool, массивы: dict, ts: int,
                          ext: dict | None = None, налог_вход=None, налог_выход=None) -> dict:
    """quote.rs::quote_exact_in. массивы: {индекс: разобрать_массив(...)}; ts -- unix-время
    сделки (для комиссии волатильности). Возвращает {amount_out, fee, protocol_fee, корзин}.
    Нехватка массива -- ОшибкаDLMM с индексом (дочитать и повторить)."""
    import copy  # noqa: PLC0415
    if lb["status"] != 0:
        raise ОшибкаDLMM("пул выключен")
    lb = copy.deepcopy(lb)
    update_references(lb, ts)
    lo = support_limit_order(lb)
    foi = fee_on_input(lb, swap_for_y)
    left = налог_вход(сумма_в) if налог_вход else сумма_в
    tot_out = tot_fee = tot_prot = 0
    корзин = 0
    while left > 0:
        idx = нужные_массивы(lb, swap_for_y, 1, ext)
        if not idx:
            raise ОшибкаDLMM("в пуле не хватает ликвидности")
        ba = массивы.get(idx[0])
        if ba is None:
            raise ОшибкаDLMM(f"нет массива корзин {idx[0]}")
        lo_id, hi_id = границы_массива(ba["index"])
        if индекс_массива(lb["active_id"]) != ba["index"]:
            lb["active_id"] = hi_id if swap_for_y else lo_id
        while lo_id <= lb["active_id"] <= hi_id and left > 0:
            b = dict(ba["bins"][lb["active_id"] - lo_id])
            if b["price"] == 0:
                b["price"] = цена_корзины(lb["active_id"], lb["bin_step"])
            if _max_out(b, swap_for_y, lo) > 0:
                update_volatility_accumulator(lb)
                a_in, a_out, fee, prot = _at_bin(b, lb, left, swap_for_y, lo, foi)
                if a_in > 0:
                    left -= a_in
                    tot_out += a_out
                    tot_fee += fee
                    tot_prot += prot
                    корзин += 1
            if left > 0:
                nid = lb["active_id"] + (-1 if swap_for_y else 1)
                if not (MIN_BIN_ID <= nid <= MAX_BIN_ID):
                    raise ОшибкаDLMM("недостаточно ликвидности")
                lb["active_id"] = nid
    out = налог_выход(tot_out) if налог_выход else tot_out
    return {"amount_out": out, "fee": tot_fee, "protocol_fee": tot_prot, "корзин": корзин,
            "active_id_после": lb["active_id"]}


def налог_2022(bps: int, max_fee: int | None):
    """Функция «сумма -> сумма без налога» для Token-2022 TransferFee (bps, максимум)."""
    def f(amount: int) -> int:
        if not bps:
            return amount
        fee = (amount * bps + BASIS_POINT_MAX - 1) // BASIS_POINT_MAX
        if max_fee is not None:
            fee = min(fee, max_fee)
        return amount - fee
    return f
