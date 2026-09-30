#!/usr/bin/env python3
"""Точная котировка exact_in для Meteora Dynamic Bonding Curve (dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN) -- офлайн.

Перенос из исходников MeteoraAg/dynamic-bonding-curve (programs/dynamic-bonding-curve/src, коммит f552f20 от 09.09.2026):
  * state/virtual_pool.rs -- get_swap_result_from_exact_input, calculate_quote_to_base_from_amount_in (до migration_sqrt_price),
    calculate_base_to_quote_from_amount_in (кусочная кривая до 20 точек, нижняя граница sqrt_start_price);
  * curve.rs -- get_delta_amount_base/quote_unsigned, get_next_sqrt_price_from_input (ликвидность в Q64: Δquote = L·Δ√P >> 128);
  * state/config.rs -- PoolFeesConfig (get_fee_on_amount: комиссия с суммы вверх, 20 % протоколу, из них 20 % рефералу),
    DynamicFeeConfig.get_variable_fee_numerator (по накопителю волатильности из пула -- до свопа), MAX_FEE_NUMERATOR 99 %;
  * base_fee/fee_scheduler.rs (линейный / экспоненциальный спад по периодам от activation_point; pow -- как у DLMM Meteora),
    base_fee/fee_rate_limiter.rs (рост комиссии с размером покупки quote→base в окне max_limiter_duration);
  * state/fee.rs -- FeeMode (collect_fee_mode: 0 -- комиссия в quote, 1 -- в выходном токене).
Раскладки (zero_copy, размеры сверены с const_assert исходников): PoolState 416 байт (+8 дискриминатор), PoolConfig 1040 (+8).
Вход/выход -- сырые единицы; «первый своп с минимальной комиссией» (enable_first_swap_with_min_fee + создание пула в той же
транзакции) -- флагом, не угадывается.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ПРОГРАММА = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
FEE_DENOMINATOR = 1_000_000_000
MAX_FEE_NUMERATOR = 990_000_000
PROTOCOL_FEE_PERCENT = 20
HOST_FEE_PERCENT = 20
MAX_BASIS_POINT = 10_000
U64_MAX = (1 << 64) - 1
ONE_Q64 = 1 << 64


class ОшибкаDBC(Exception):
    pass


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


def разобрать_пул(data: bytes) -> dict:
    d = data[8:]
    if len(d) < 416:
        raise ОшибкаDBC(f"VirtualPool {len(data)} байт, ждём 424")
    return {"vt_last_update_timestamp": struct.unpack_from("<Q", d, 0)[0], "vt_sqrt_price_reference": _u128(d, 16),
            "vt_volatility_accumulator": _u128(d, 32), "vt_volatility_reference": _u128(d, 48),
            "config": _pk(d[64:96]), "creator": _pk(d[96:128]), "base_mint": _pk(d[128:160]),
            "base_vault": _pk(d[160:192]), "quote_vault": _pk(d[192:224]),
            "base_reserve": struct.unpack_from("<Q", d, 224)[0], "quote_reserve": struct.unpack_from("<Q", d, 232)[0],
            "sqrt_price": _u128(d, 272), "activation_point": struct.unpack_from("<Q", d, 288)[0],
            "pool_type": d[296], "is_migrated": d[297], "migration_progress": d[300], "has_swap": d[362]}


def разобрать_конфиг(data: bytes) -> dict:
    d = data[8:]
    if len(d) < 1040:
        raise ОшибкаDBC(f"PoolConfig {len(data)} байт, ждём ≥ 1048")
    cliff, second, third = struct.unpack_from("<QQQ", d, 96)
    first = struct.unpack_from("<H", d, 120)[0]
    mva, vfc = struct.unpack_from("<II", d, 136)
    bin_step, fp, dp, rf = struct.unpack_from("<HHHH", d, 144)
    кривая = []
    for i in range(20):
        o = 400 + 32 * i
        кривая.append((_u128(d, o), _u128(d, o + 16)))
    return {"quote_mint": _pk(d[0:32]),
            "base_fee": {"cliff_fee_numerator": cliff, "second_factor": second, "third_factor": third,
                         "first_factor": first, "base_fee_mode": d[122]},
            "dynamic_fee": {"initialized": d[128], "max_volatility_accumulator": mva, "variable_fee_control": vfc,
                            "bin_step": bin_step, "filter_period": fp, "decay_period": dp, "reduction_factor": rf,
                            "bin_step_u128": _u128(d, 160)},
            "collect_fee_mode": d[224], "migration_option": d[225], "activation_type": d[226], "token_decimal": d[227],
            "version": d[228], "token_type": d[229], "quote_token_flag": d[230],
            "migration_quote_threshold": struct.unpack_from("<Q", d, 256)[0],
            "migration_base_threshold": struct.unpack_from("<Q", d, 264)[0], "migration_sqrt_price": _u128(d, 272),
            "enable_first_swap_with_min_fee": d[357], "sqrt_start_price": _u128(d, 384), "curve": кривая}


# ------------------------------------------------------------------ математика

def _mdc(x: int, y: int, den: int, вверх: bool) -> int:
    p = x * y
    return -(-p // den) if вверх else p // den


def _u64(v: int) -> int:
    if v < 0 or v > U64_MAX:
        raise ОшибкаDBC("MathOverflow")
    return v


def delta_base(lo: int, hi: int, L: int, вверх: bool) -> int:
    den = lo * hi
    if den <= 0:
        raise ОшибкаDBC("знаменатель 0")
    return _mdc(L, hi - lo, den, вверх)


def delta_quote(lo: int, hi: int, L: int, вверх: bool) -> int:
    prod = L * (hi - lo)
    return -(-prod >> 128) if вверх else prod >> 128


def next_from_base_in(p: int, L: int, amount: int) -> int:
    if amount == 0:
        return p
    return _mdc(L, p, L + amount * p, True)


def next_from_quote_in(p: int, L: int, amount: int) -> int:
    return p + (amount << 128) // L


def fee_scheduler(bf: dict, current_point: int, activation_point: int) -> int:
    """fee_scheduler.rs: cliff − reduction·период (линейный) или cliff·(1 − reduction/10000)^период (экспоненциальный)."""
    import podbivka_dlmm_quote as DQ  # noqa: PLC0415 -- pow Meteora (math/u64x64_math.rs), сверен на DLMM
    freq, n = bf["second_factor"], bf["first_factor"]
    if freq == 0:
        return bf["cliff_fee_numerator"]
    if current_point < activation_point:
        raise ОшибкаDBC("current_point < activation_point")
    period = min((current_point - activation_point) // freq, n)
    if bf["base_fee_mode"] == 0:
        v = bf["cliff_fee_numerator"] - bf["third_factor"] * period
        if v < 0:
            raise ОшибкаDBC("MathOverflow")
        return v
    bps = (bf["third_factor"] << 64) // MAX_BASIS_POINT
    base = ONE_Q64 - bps
    r = DQ.pow_q64(base, period)
    return _u64((r * bf["cliff_fee_numerator"]) >> 64)


def rate_limiter_applied(bf: dict, current_point: int, activation_point: int, quote_to_base: bool) -> bool:
    if bf["third_factor"] == 0 and bf["second_factor"] == 0 and bf["first_factor"] == 0:
        return False
    if not quote_to_base:
        return False
    return current_point <= activation_point + bf["second_factor"]


def rate_limiter_fee(bf: dict, input_amount: int) -> int:
    """fee_rate_limiter.rs: get_fee_numerator_from_included_fee_amount."""
    c, x0 = bf["cliff_fee_numerator"], bf["third_factor"]
    if input_amount <= x0:
        return c
    i = bf["first_factor"] * FEE_DENOMINATOR // MAX_BASIS_POINT
    max_index = (MAX_FEE_NUMERATOR - c) // i
    a, b = divmod(input_amount - x0, x0)
    if a < max_index:
        num = x0 * (c + c * a + i * a * (a + 1) // 2) + b * (c + i * (a + 1))
    else:
        num = x0 * (c + c * max_index + i * max_index * (max_index + 1) // 2) + ((a - max_index) * x0 + b) * MAX_FEE_NUMERATOR
    fee = _u64(-(-num // FEE_DENOMINATOR))
    return _u64(_mdc(fee, FEE_DENOMINATOR, input_amount, True))


def variable_fee(df: dict, pool: dict) -> int:
    if not df["initialized"]:
        return 0
    sq = (pool["vt_volatility_accumulator"] * df["bin_step"]) ** 2
    return (sq * df["variable_fee_control"] + 99_999_999_999) // 100_000_000_000


def fee_on_amount(num: int, amount: int, has_referral: bool) -> dict:
    fee = _u64(_mdc(amount, num, FEE_DENOMINATOR, True))
    prot = fee * PROTOCOL_FEE_PERCENT // 100
    trading = fee - prot
    ref = prot * HOST_FEE_PERCENT // 100 if has_referral else 0
    return {"amount": amount - fee, "trading_fee": trading, "protocol_fee": prot - ref, "referral_fee": ref}


def котировка_точный_вход(pool: dict, cfg: dict, amount_in: int, quote_to_base: bool, current_point: int,
                          has_referral: bool = False, первый_своп_мин: bool = False) -> dict:
    """get_swap_result_from_exact_input. current_point -- слот или unix-время (cfg['activation_type']: 0 -- слот, 1 -- время)."""
    if amount_in <= 0:
        raise ОшибкаDBC("AmountIsZero")
    if pool["quote_reserve"] >= cfg["migration_quote_threshold"]:
        raise ОшибкаDBC("PoolIsCompleted")
    bf = cfg["base_fee"]
    if первый_своп_мин:
        base_num = bf["cliff_fee_numerator"] if bf["base_fee_mode"] == 2 else fee_scheduler(bf, 1 << 62, 0)
        num = base_num
    else:
        if bf["base_fee_mode"] == 2:
            base_num = rate_limiter_fee(bf, amount_in) if rate_limiter_applied(bf, current_point, pool["activation_point"],
                                                                               quote_to_base) else bf["cliff_fee_numerator"]
        else:
            base_num = fee_scheduler(bf, current_point, pool["activation_point"])
        num = min(variable_fee(cfg["dynamic_fee"], pool) + base_num, MAX_FEE_NUMERATOR)
    # FeeMode: QuoteToken (0) -- комиссия на входе только для quote→base; OutputToken (1) -- всегда на выходе
    fees_on_input = cfg["collect_fee_mode"] == 0 and quote_to_base
    сборы = {"trading_fee": 0, "protocol_fee": 0, "referral_fee": 0}
    вход = amount_in
    if fees_on_input:
        f = fee_on_amount(num, amount_in, has_referral)
        вход = f.pop("amount")
        сборы = f
    кр = cfg["curve"]
    p = pool["sqrt_price"]
    left = вход
    out = 0
    if quote_to_base:
        stop = cfg["migration_sqrt_price"]
        for sp_i, L_i in кр:
            if sp_i == 0 or L_i == 0:
                break
            ref = min(stop, sp_i)
            if ref > p:
                max_in = delta_quote(p, ref, L_i, True)
                if left < max_in:
                    nxt = next_from_quote_in(p, L_i, left)
                    out += delta_base(p, nxt, L_i, False)
                    p, left = nxt, 0
                    break
                out += delta_base(p, ref, L_i, False)
                p = ref
                left -= max_in
                if ref == stop:
                    break
    else:
        for i in range(len(кр) - 2, -1, -1):
            sp_i = кр[i][0]
            if sp_i == 0 or кр[i][1] == 0:
                continue
            if sp_i < p:
                L = кр[i + 1][1]
                max_in = delta_base(sp_i, p, L, True)
                if left < max_in:
                    nxt = next_from_base_in(p, L, left)
                    out += delta_quote(nxt, p, L, False)
                    p, left = nxt, 0
                    break
                out += delta_quote(sp_i, p, L, False)
                p = sp_i
                left -= max_in
        if left:
            L0 = кр[0][1]
            nxt = next_from_base_in(p, L0, left)
            if nxt < cfg["sqrt_start_price"]:
                nxt = cfg["sqrt_start_price"]
                left -= delta_base(nxt, p, L0, True)
            else:
                left = 0
            out += delta_quote(nxt, p, L0, False)
            p = nxt
    if left != 0:
        raise ОшибкаDBC("InsufficientLiquidity (amount_left ≠ 0)")
    out = _u64(out)
    if not fees_on_input:
        f = fee_on_amount(num, out, has_referral)
        out = f.pop("amount")
        сборы = f
    return {"output_amount": out, "next_sqrt_price": p, "excluded_fee_input_amount": вход, "fee_numerator": num,
            "base_fee_numerator": base_num, **сборы, "fees_on_input": fees_on_input}


def self_test() -> int:
    ош = []

    def chk(имя, ок, факт=""):
        print(f"  [{'ok' if ок else 'СБОЙ'}] {имя}" + ("" if ок else f" -- {факт}"))
        if not ок:
            ош.append(имя)
    # вход base → выход quote и обратно на одной ликвидности: округления в пользу пула
    L, p = 10 ** 24, 1 << 64
    b = delta_base(p // 2, p, L, True)
    chk("next_from_base_in(Δbase вверх) не выше нижней цены", next_from_base_in(p, L, b) <= p // 2 + 1)
    chk("Δquote вниз ≤ вверх", delta_quote(p // 2, p, L, False) <= delta_quote(p // 2, p, L, True))
    chk("комиссия: 20 % протоколу, 20 % от неё рефералу",
        fee_on_amount(10_000_000, 1_000_000_000, True) == {"amount": 990_000_000, "trading_fee": 8_000_000,
                                                            "protocol_fee": 1_600_000, "referral_fee": 400_000})
    bf = {"cliff_fee_numerator": 500_000_000, "first_factor": 10, "second_factor": 60, "third_factor": 1_000, "base_fee_mode": 1}
    chk("экспоненциальный спад: период 0 -- cliff", fee_scheduler(bf, 100, 100) == 500_000_000)
    chk("экспоненциальный спад убывает", fee_scheduler(bf, 100 + 60 * 3, 100) < fee_scheduler(bf, 100 + 60, 100))
    print(f"самопроверка порта DBC: {'СБОЙ ' + str(len(ош)) if ош else 'всё сошлось'}")
    return 1 if ош else 0


if __name__ == "__main__":
    raise SystemExit(self_test())
