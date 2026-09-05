"""P6 -- dry-run перестановки диапазона на один тик [-68000, -66000]
(владелец, 2026-09-05). ТОЛЬКО ЧТЕНИЕ И eth_call-СИМУЛЯЦИИ (staticcall) --
НИКАКИХ реальных транзакций, позиция НЕ трогается. Реальное исполнение --
только после отдельного явного "да" владельца по этому пункту.

Владелец: "eth_call (staticcall) самого mint с рассчитанными суммами --
не расчёт, а симуляция контракта. Именно её не хватало, чтобы поймать
PSC до реальных попыток." Это прямое следствие бага №13 (docs/
PROJECT_STATE.md) -- решение здесь и формализуется как обязательный шаг
протокола для ВСЕХ будущих mint (см. ПРОТОКОЛ в конце файла).

ЧЕСТНОЕ ОГРАНИЧЕНИЕ eth_call-симуляции mint здесь: реальный allowance
USDC/cbBTC->NFPM сейчас, скорее всего, ~0 (approve на прошлом входе был
выдан РОВНО на использованную сумму, NFPM списал её через transferFrom
целиком) -- eth_call мог упасть на STF (нет allowance), не дойдя до
проверки PSC. Это тоже РЕАЛЬНО проверяется и честно репортится, а не
скрывается. Полноценная проверка PSC симуляцией требует реального (но
дешёвого, ~$0.001) approve ПЕРЕД eth_call -- это и есть предлагаемый
протокольный шаг ("approve -> eth_call-simulate -> mint"), который
физически исполняется только на реальном будущем входе, не здесь.
"""
import json
import math
import time
from pathlib import Path

import requests
from eth_abi import decode as abi_decode, encode as abi_encode
from eth_utils import to_checksum_address
from Crypto.Hash import keccak

OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "p3_guard_cache" / "p6_reposition_dryrun_result.json"
STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "p6_live_position_state.json"

BASE_RPC = "https://mainnet.base.org"
WALLET = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"
NFPM = "0x827922686190790b37229fd06084350E74485b72"
POOL_ADDRESS = "0x3e66e55e97ce60096f74b7c475e8249f2d31a9fb"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CBBTC = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"
USDC_DECIMALS, CBBTC_DECIMALS = 6, 8
TOKEN_ID = 76445294

# Предлагаемая перестановка: один шаг tickSpacing (2000) уже, чем текущий
# диапазон [-68000,-64000] (2 шага) -- сдвиг верхней границы к -66000.
NEW_TICK_LOWER = -68000
NEW_TICK_UPPER = -66000
MINT_SLIPPAGE = 0.10  # тот же допуск, что реальный вход (docs/PROJECT_STATE.md #13)

LIGHTER_API_BASE = "https://api.rh.lighter.xyz"
LIGHTER_ACCOUNT_INDEX = 22012


def lighter_account_full() -> dict | None:
    try:
        r = requests.get(f"{LIGHTER_API_BASE}/api/v1/account", params={"by": "index", "value": str(LIGHTER_ACCOUNT_INDEX)}, timeout=20)
        r.raise_for_status()
        accounts = r.json().get("accounts", [])
        return accounts[0] if accounts else None
    except Exception as e:  # noqa: BLE001
        print(f"[reposition] Lighter account недоступен: {e}")
        return None


def lighter_btc_market() -> dict | None:
    try:
        r = requests.get(f"{LIGHTER_API_BASE}/api/v1/orderBookDetails", params={"filter": "all"}, timeout=20)
        r.raise_for_status()
        markets = r.json().get("order_book_details", [])
        exact = [m for m in markets if str(m.get("symbol", "")).upper() == "BTC"]
        return exact[0] if exact else None
    except Exception as e:  # noqa: BLE001
        print(f"[reposition] Lighter market недоступен: {e}")
        return None


def _topic0(sig: str) -> str:
    k = keccak.new(digest_bits=256)
    k.update(sig.encode())
    return "0x" + k.hexdigest()


def _selector(sig: str) -> str:
    return _topic0(sig)[:10]


RPC_MIN_INTERVAL_S = 1.0
RPC_RETRY_BACKOFF_S = 15.0
RPC_MAX_RETRIES = 3
_last_rpc_call = 0.0


def rpc(method: str, params: list):
    """НАЙДЕНО (реальный прогон): mainnet.base.org реально отдал 429 без
    троттлинга между несколькими eth_call подряд -- тот же троттлинг/ретрай,
    что уже используется в p6_hourly_snapshot.py."""
    global _last_rpc_call
    wait = _last_rpc_call + RPC_MIN_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    for attempt in range(RPC_MAX_RETRIES + 1):
        _last_rpc_call = time.monotonic()
        r = requests.post(BASE_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
        if r.status_code == 429 and attempt < RPC_MAX_RETRIES:
            print(f"[reposition] RPC 429 (попытка {attempt + 1}/{RPC_MAX_RETRIES + 1}) -- жду {RPC_RETRY_BACKOFF_S:.0f}с")
            time.sleep(RPC_RETRY_BACKOFF_S)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("RPC 429 после ретраев")


def eth_call(to: str, data: str, frm: str | None = None):
    params = {"to": to, "data": data}
    if frm:
        params["from"] = frm
    return rpc("eth_call", [params, "latest"])


def erc20_balance(token: str, holder: str) -> int:
    data = "0x" + _selector("balanceOf(address)")[2:] + holder[2:].rjust(64, "0").lower()
    resp = eth_call(token, data)
    return int(resp["result"], 16) if "result" in resp else 0


def erc20_allowance(token: str, owner: str, spender: str) -> int:
    data = "0x" + _selector("allowance(address,address)")[2:] + owner[2:].rjust(64, "0").lower() + spender[2:].rjust(64, "0").lower()
    resp = eth_call(token, data)
    return int(resp["result"], 16) if "result" in resp else 0


def read_pool_state() -> dict:
    slot0 = eth_call(POOL_ADDRESS, _selector("slot0()"))["result"]
    liquidity_raw = int(eth_call(POOL_ADDRESS, _selector("liquidity()"))["result"], 16)
    tick_spacing_raw = int(eth_call(POOL_ADDRESS, _selector("tickSpacing()"))["result"], 16)
    tick_spacing = tick_spacing_raw - (1 << 256) if tick_spacing_raw >= (1 << 255) else tick_spacing_raw
    hexdata = slot0[2:]
    sqrt_price_x96 = int(hexdata[0:64], 16)
    tick_word = int(hexdata[64:128], 16)
    tick = tick_word - (1 << 256) if tick_word >= (1 << 255) else tick_word
    return {"sqrtPriceX96": sqrt_price_x96, "tick": tick, "liquidity_raw": liquidity_raw, "tick_spacing": tick_spacing}


def price_cbbtc_usd(sqrt_price_x96: int) -> float:
    raw = (sqrt_price_x96 / (2 ** 96)) ** 2
    price_cbbtc_per_usdc = raw * (10 ** (USDC_DECIMALS - CBBTC_DECIMALS))
    return 1.0 / price_cbbtc_per_usdc


def tick_to_usd_price(tick: int) -> float:
    """Точная обратная к usd_price_to_tick -- см. analysis/p6_live_step1.py
    (фикс бага #13, docs/PROJECT_STATE.md) -- НЕ номинальный процент."""
    return (10 ** (CBBTC_DECIMALS - USDC_DECIMALS)) / (1.0001 ** tick)


def get_liquidity_for_amounts(sqrt_p, sqrt_pa, sqrt_pb, amount0, amount1) -> float:
    if sqrt_pa > sqrt_pb:
        sqrt_pa, sqrt_pb = sqrt_pb, sqrt_pa
    if sqrt_p <= sqrt_pa:
        return amount0 * (sqrt_pa * sqrt_pb) / (sqrt_pb - sqrt_pa)
    elif sqrt_p < sqrt_pb:
        l0 = amount0 * (sqrt_p * sqrt_pb) / (sqrt_pb - sqrt_p)
        l1 = amount1 / (sqrt_p - sqrt_pa)
        return min(l0, l1)
    else:
        return amount1 / (sqrt_pb - sqrt_pa)


def v3_amounts(liquidity, sqrt_p, sqrt_pa, sqrt_pb) -> tuple[float, float]:
    sqrt_p = min(max(sqrt_p, sqrt_pa), sqrt_pb)
    amount0 = liquidity * (1 / sqrt_p - 1 / sqrt_pb)
    amount1 = liquidity * (sqrt_p - sqrt_pa)
    return max(amount0, 0.0), max(amount1, 0.0)


def position_concentration_k(pa_usd: float, pb_usd: float) -> float:
    """Концентрация КОНКРЕТНОГО диапазона [pa,pb] относительно full-range
    той же $-стоимости при цене В СЕРЕДИНЕ диапазона -- стандартная формула
    Uniswap v3: L_range / L_full = 1 / (1 - sqrt(Pa/Pb)) (симметрично
    относительно выбора token0/token1, т.к. это отношение sqrt-цен).
    ИНАЯ формула, чем `compute_k_concentration` в p6_hourly_snapshot.py
    (та мерит концентрацию ВСЕГО пула по TVL/liquidity(), эта -- по
    геометрии ОДНОГО диапазона) -- обе реальные, но разные метрики,
    не путать напрямую."""
    ratio = pa_usd / pb_usd
    return 1.0 / (1.0 - math.sqrt(ratio))


def decode_revert(data_hex: str) -> str:
    if not data_hex or data_hex == "0x":
        return "(нет данных ревёрта)"
    if data_hex.startswith("0x08c379a0"):
        # Error(string)
        try:
            payload = bytes.fromhex(data_hex[10:])
            (s,) = abi_decode(["string"], payload)
            return f'Error(string): "{s}"'
        except Exception as e:  # noqa: BLE001
            return f"Error(string) не распарсился: {e}, raw={data_hex}"
    return f"кастомный селектор {data_hex[:10]} (не Error(string)), raw={data_hex}"


def main():
    result = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "mode": "DRY-RUN, только чтение/симуляция"}

    print("=== Реальное текущее состояние позиции и пула ===")
    pool = read_pool_state()
    p0 = price_cbbtc_usd(pool["sqrtPriceX96"])
    ts = pool["tick_spacing"]
    print(f"[reposition] пул: price=${p0:.2f} tick={pool['tick']} tickSpacing={ts}")

    positions_calldata = "0x" + _selector("positions(uint256)")[2:] + hex(TOKEN_ID)[2:].rjust(64, "0")
    pos_raw = eth_call(NFPM, positions_calldata)["result"]
    fields = abi_decode(
        ["uint96", "address", "address", "address", "int24", "int24", "int24",
         "uint128", "uint256", "uint256", "uint128", "uint128"],
        bytes.fromhex(pos_raw[2:]),
    )
    keys = ["nonce", "operator", "token0", "token1", "tick_spacing", "tick_lower", "tick_upper",
            "liquidity", "fee_growth0", "fee_growth1", "tokens_owed0", "tokens_owed1"]
    position = dict(zip(keys, fields))
    print(f"[reposition] реальная позиция {TOKEN_ID}: liquidity={position['liquidity']} "
          f"tick_lower={position['tick_lower']} tick_upper={position['tick_upper']}")
    result["current_position"] = {k: (v if not isinstance(v, bytes) else v.hex()) for k, v in position.items()}

    print("\n=== Реальная eth_call-симуляция decreaseLiquidity(ВСЯ liquidity) -- НЕ транзакция ===")
    deadline = int(time.time()) + 3600
    decrease_selector = bytes.fromhex(_selector("decreaseLiquidity((uint256,uint128,uint256,uint256,uint256))")[2:])
    decrease_calldata = decrease_selector + abi_encode(
        ["(uint256,uint128,uint256,uint256,uint256)"],
        [(TOKEN_ID, position["liquidity"], 0, 0, deadline)],
    )
    decrease_resp = rpc("eth_call", [{"to": NFPM, "from": WALLET, "data": "0x" + decrease_calldata.hex()}, "latest"])
    if "result" in decrease_resp:
        dec_amount0, dec_amount1 = abi_decode(["uint256", "uint256"], bytes.fromhex(decrease_resp["result"][2:]))
        print(f"[reposition] decreaseLiquidity(ALL) реально СИМУЛИРОВАН: вернёт amount0(USDC)={dec_amount0/10**USDC_DECIMALS} "
              f"amount1(cbBTC)={dec_amount1/10**CBBTC_DECIMALS}")
    else:
        dec_amount0, dec_amount1 = 0, 0
        print(f"[reposition] decreaseLiquidity(ALL) РЕАЛЬНО упал в симуляции: {decrease_resp.get('error')}")
    result["decrease_liquidity_simulated"] = {"raw_response": decrease_resp, "amount0_usdc": dec_amount0 / 10 ** USDC_DECIMALS,
                                               "amount1_cbbtc": dec_amount1 / 10 ** CBBTC_DECIMALS}

    print("\n=== Реальная eth_call-симуляция collect(MAX) -- накопленные, но не собранные комиссии ===")
    collect_selector = bytes.fromhex(_selector("collect((uint256,address,uint128,uint128))")[2:])
    collect_calldata = collect_selector + abi_encode(
        ["(uint256,address,uint128,uint128)"], [(TOKEN_ID, to_checksum_address(WALLET), 2 ** 128 - 1, 2 ** 128 - 1)],
    )
    collect_resp = rpc("eth_call", [{"to": NFPM, "from": WALLET, "data": "0x" + collect_calldata.hex()}, "latest"])
    if "result" in collect_resp:
        fee0, fee1 = abi_decode(["uint256", "uint256"], bytes.fromhex(collect_resp["result"][2:]))
        print(f"[reposition] collect(MAX) реально СИМУЛИРОВАН (только накопленные комиссии, ДО decrease): "
              f"fee0(USDC)={fee0/10**USDC_DECIMALS} fee1(cbBTC)={fee1/10**CBBTC_DECIMALS}")
    else:
        fee0, fee1 = 0, 0
        print(f"[reposition] collect(MAX) РЕАЛЬНО упал в симуляции: {collect_resp.get('error')}")
    result["collect_fees_simulated"] = {"raw_response": collect_resp, "fee0_usdc": fee0 / 10 ** USDC_DECIMALS, "fee1_cbbtc": fee1 / 10 ** CBBTC_DECIMALS}

    print("\n=== Реальный дребезг на кошельке (не в LP) ===")
    wallet_usdc_now = erc20_balance(USDC, WALLET)
    wallet_cbbtc_now = erc20_balance(CBBTC, WALLET)
    print(f"[reposition] на кошельке сейчас: USDC={wallet_usdc_now/10**USDC_DECIMALS} cbBTC={wallet_cbbtc_now/10**CBBTC_DECIMALS}")
    result["wallet_dust_now"] = {"usdc": wallet_usdc_now / 10 ** USDC_DECIMALS, "cbbtc": wallet_cbbtc_now / 10 ** CBBTC_DECIMALS}

    total_usdc_human = (dec_amount0 + fee0 + wallet_usdc_now) / 10 ** USDC_DECIMALS
    total_cbbtc_human = (dec_amount1 + fee1 + wallet_cbbtc_now) / 10 ** CBBTC_DECIMALS
    print(f"\n[reposition] ИТОГО доступно для перевхода (decrease+fees+дребезг): "
          f"USDC={total_usdc_human} cbBTC={total_cbbtc_human} (~${total_usdc_human + total_cbbtc_human * p0:.2f})")
    result["total_available_for_reposition"] = {"usdc_human": total_usdc_human, "cbbtc_human": total_cbbtc_human,
                                                   "total_usd_approx": total_usdc_human + total_cbbtc_human * p0}

    print(f"\n=== Оптимум для НОВОГО диапазона [{NEW_TICK_LOWER}, {NEW_TICK_UPPER}] (один tickSpacing={ts}, реальные тиковые границы) ===")
    pb_actual_usd = tick_to_usd_price(NEW_TICK_LOWER)
    pa_actual_usd = tick_to_usd_price(NEW_TICK_UPPER)
    print(f"[reposition] реальные границы нового диапазона: ${pa_actual_usd:.2f}..${pb_actual_usd:.2f} "
          f"({(pa_actual_usd/p0-1)*100:.2f}% .. {(pb_actual_usd/p0-1)*100:.2f}% от текущей цены)")

    def usd_to_domain(p_usd: float) -> float:
        return 1.0 / p_usd

    sqrt_p = usd_to_domain(p0) ** 0.5
    sqrt_pa, sqrt_pb = usd_to_domain(pb_actual_usd) ** 0.5, usd_to_domain(pa_actual_usd) ** 0.5

    # Владелец, 2026-09-05: "decreaseLiquidity(ALL)+collect -> СВОП USDC->cbBTC
    # на Base до оптимального соотношения для [NEW_TICK_LOWER,NEW_TICK_UPPER] ->
    # approve -> eth_call mint" -- цель >=95% размещённого капитала. Держать
    # ПОЛУЧЕННОЕ соотношение (как раньше) недостаточно -- нужно СНАЧАЛА
    # посчитать ЦЕЛЕВОЕ соотношение (независимо от того, что реально держим),
    # затем определить размер свопа, который к нему приводит.
    L_unit = get_liquidity_for_amounts(sqrt_p, sqrt_pa, sqrt_pb, 1.0, 1.0 / p0)  # L при равной $-стоимости обеих ног (пробный якорь)
    amount0_at_unit, amount1_at_unit = v3_amounts(L_unit, sqrt_p, sqrt_pa, sqrt_pb)
    value0_frac = amount0_at_unit / (amount0_at_unit + amount1_at_unit * p0)
    value1_frac = 1.0 - value0_frac
    total_usd_value = total_usdc_human + total_cbbtc_human * p0
    target_usdc_human = total_usd_value * value0_frac
    target_cbbtc_human = (total_usd_value * value1_frac) / p0
    print(f"[reposition] целевое соотношение (по $-доле для этого диапазона): USDC-доля={value0_frac*100:.2f}% cbBTC-доля={value1_frac*100:.2f}% "
          f"-> target USDC={target_usdc_human:.6f} target cbBTC={target_cbbtc_human:.8f}")

    swap_needed_usdc_to_cbbtc_human = 0.0
    swap_needed_cbbtc_to_usdc_human = 0.0
    if target_cbbtc_human > total_cbbtc_human:
        # не хватает cbBTC -- сворачиваем часть USDC в cbBTC (та же аппроксимация
        # "amountIn/price", что реальный вход, широкий допуск на проскальзывание)
        cbbtc_shortfall = target_cbbtc_human - total_cbbtc_human
        swap_needed_usdc_to_cbbtc_human = cbbtc_shortfall * p0
    elif target_usdc_human > total_usdc_human:
        usdc_shortfall = target_usdc_human - total_usdc_human
        swap_needed_cbbtc_to_usdc_human = usdc_shortfall / p0
    POOL_FEE_FRACTION = 0.00033  # docs/PROJECT_STATE.md, "Скринер пулов" -- 0.033%, тот же, что p6_hourly_snapshot.py
    if swap_needed_usdc_to_cbbtc_human > 0:
        expected_cbbtc_out = (swap_needed_usdc_to_cbbtc_human * (1 - POOL_FEE_FRACTION)) / p0
        post_swap_usdc_human = total_usdc_human - swap_needed_usdc_to_cbbtc_human
        post_swap_cbbtc_human = total_cbbtc_human + expected_cbbtc_out
        print(f"[reposition] СВОП (симуляция, не транзакция): USDC->cbBTC, amountIn={swap_needed_usdc_to_cbbtc_human:.6f} USDC "
              f"-> ожидаемо {expected_cbbtc_out:.8f} cbBTC (комиссия пула {POOL_FEE_FRACTION*100:.3f}%)")
    elif swap_needed_cbbtc_to_usdc_human > 0:
        expected_usdc_out = (swap_needed_cbbtc_to_usdc_human * (1 - POOL_FEE_FRACTION)) * p0
        post_swap_cbbtc_human = total_cbbtc_human - swap_needed_cbbtc_to_usdc_human
        post_swap_usdc_human = total_usdc_human + expected_usdc_out
        print(f"[reposition] СВОП (симуляция, не транзакция): cbBTC->USDC, amountIn={swap_needed_cbbtc_to_usdc_human:.8f} cbBTC "
              f"-> ожидаемо {expected_usdc_out:.6f} USDC (комиссия пула {POOL_FEE_FRACTION*100:.3f}%)")
    else:
        post_swap_usdc_human, post_swap_cbbtc_human = total_usdc_human, total_cbbtc_human
        print("[reposition] своп не требуется -- держим уже в целевом соотношении (маловероятно, но проверено).")

    result["swap_plan"] = {
        "target_usdc_human": target_usdc_human, "target_cbbtc_human": target_cbbtc_human,
        "swap_usdc_to_cbbtc_human": swap_needed_usdc_to_cbbtc_human, "swap_cbbtc_to_usdc_human": swap_needed_cbbtc_to_usdc_human,
        "post_swap_usdc_human": post_swap_usdc_human, "post_swap_cbbtc_human": post_swap_cbbtc_human,
        "note": "СИМУЛЯЦИЯ (математика с реальной ценой пула, широкий допуск -- своп сам НЕ eth_call-симулирован: тот же "
                "allowance=0 сейчас блокирует eth_call свопа так же, как mint (см. mint_simulation.note ниже), "
                "но amountIn/price-аппроксимация -- тот же метод, что реально использовался и подтвердился при входе.",
    }

    L_new = get_liquidity_for_amounts(sqrt_p, sqrt_pa, sqrt_pb, post_swap_usdc_human, post_swap_cbbtc_human)
    amount0_at_L, amount1_at_L = v3_amounts(L_new, sqrt_p, sqrt_pa, sqrt_pb)
    amount0_desired = min(int(post_swap_usdc_human * 10 ** USDC_DECIMALS), int(amount0_at_L * 10 ** USDC_DECIMALS))
    amount1_desired = min(int(post_swap_cbbtc_human * 10 ** CBBTC_DECIMALS), int(amount1_at_L * 10 ** CBBTC_DECIMALS))
    amount0_min = int(amount0_desired * (1 - MINT_SLIPPAGE))
    amount1_min = int(amount1_desired * (1 - MINT_SLIPPAGE))
    dust_usdc_after = post_swap_usdc_human - amount0_desired / 10 ** USDC_DECIMALS
    dust_cbbtc_after = post_swap_cbbtc_human - amount1_desired / 10 ** CBBTC_DECIMALS
    deployed_usd = amount0_desired / 10 ** USDC_DECIMALS + (amount1_desired / 10 ** CBBTC_DECIMALS) * p0
    deployed_pct = (deployed_usd / total_usd_value * 100) if total_usd_value else None
    print(f"[reposition] ДОЛЯ РАЗМЕЩЁННОГО КАПИТАЛА: ${deployed_usd:.4f} / ${total_usd_value:.4f} = {deployed_pct:.2f}% "
          f"(цель >=95%){' -- ДОСТИГНУТА' if deployed_pct and deployed_pct >= 95 else ' -- НЕ ДОСТИГНУТА'}")
    print(f"[reposition] оптимум: amount0(USDC)={amount0_desired/10**USDC_DECIMALS} amount1(cbBTC)={amount1_desired/10**CBBTC_DECIMALS} "
          f"(L={L_new:.4f}) -- дребезг ПОСЛЕ mint: USDC={dust_usdc_after:.6f} cbBTC={dust_cbbtc_after:.8f} (~${dust_usdc_after + dust_cbbtc_after*p0:.2f})")

    k_new_range = position_concentration_k(pa_actual_usd, pb_actual_usd)
    pa_old_actual = tick_to_usd_price(position["tick_upper"])
    pb_old_actual = tick_to_usd_price(position["tick_lower"])
    k_old_range = position_concentration_k(pa_old_actual, pb_old_actual)
    pa_nominal10, pb_nominal10 = p0 * 0.9, p0 * 1.1
    k_nominal10 = position_concentration_k(pa_nominal10, pb_nominal10)
    print(f"[reposition] k (концентрация ГЕОМЕТРИИ диапазона, формула 1/(1-sqrt(Pa/Pb))): "
          f"текущий реальный диапазон[{position['tick_lower']},{position['tick_upper']}]={k_old_range:.3f}, "
          f"новый предложенный[{NEW_TICK_LOWER},{NEW_TICK_UPPER}]={k_new_range:.3f}, "
          f"номинальный ±10% (недостижимый, для сравнения)={k_nominal10:.3f}")

    result["new_range_plan"] = {
        "tick_lower": NEW_TICK_LOWER, "tick_upper": NEW_TICK_UPPER,
        "pa_actual_usd": pa_actual_usd, "pb_actual_usd": pb_actual_usd,
        "pct_from_current_price": {"lower": (pa_actual_usd / p0 - 1) * 100, "upper": (pb_actual_usd / p0 - 1) * 100},
        "amount0_desired_usdc": amount0_desired / 10 ** USDC_DECIMALS, "amount1_desired_cbbtc": amount1_desired / 10 ** CBBTC_DECIMALS,
        "amount0_min_usdc": amount0_min / 10 ** USDC_DECIMALS, "amount1_min_cbbtc": amount1_min / 10 ** CBBTC_DECIMALS,
        "dust_after_usdc": dust_usdc_after, "dust_after_cbbtc": dust_cbbtc_after,
        "k_new_range_geometry": k_new_range, "k_current_range_geometry": k_old_range, "k_nominal_10pct_unreachable": k_nominal10,
    }

    print("\n=== Реальная проверка allowance USDC/cbBTC -> NFPM СЕЙЧАС ===")
    allowance_usdc = erc20_allowance(USDC, WALLET, NFPM)
    allowance_cbbtc = erc20_allowance(CBBTC, WALLET, NFPM)
    print(f"[reposition] allowance USDC->NFPM={allowance_usdc/10**USDC_DECIMALS} (нужно {amount0_desired/10**USDC_DECIMALS})")
    print(f"[reposition] allowance cbBTC->NFPM={allowance_cbbtc/10**CBBTC_DECIMALS} (нужно {amount1_desired/10**CBBTC_DECIMALS})")
    result["allowance_now"] = {"usdc_to_nfpm": allowance_usdc / 10 ** USDC_DECIMALS, "cbbtc_to_nfpm": allowance_cbbtc / 10 ** CBBTC_DECIMALS}

    print("\n=== Реальная eth_call-СИМУЛЯЦИЯ mint() с рассчитанными суммами (обязательный шаг протокола, владелец) ===")
    mint_selector = bytes.fromhex(_selector(
        "mint((address,address,int24,int24,int24,uint256,uint256,uint256,uint256,address,uint256,uint160))")[2:])
    mint_params = (to_checksum_address(USDC), to_checksum_address(CBBTC), ts, NEW_TICK_LOWER, NEW_TICK_UPPER,
                   amount0_desired, amount1_desired, amount0_min, amount1_min, to_checksum_address(WALLET), deadline, 0)
    mint_calldata = mint_selector + abi_encode(
        ["(address,address,int24,int24,int24,uint256,uint256,uint256,uint256,address,uint256,uint160)"], [mint_params])
    mint_sim_resp = rpc("eth_call", [{"to": NFPM, "from": WALLET, "data": "0x" + mint_calldata.hex()}, "latest"])
    if "result" in mint_sim_resp:
        liq, real_amount0, real_amount1 = abi_decode(["uint128", "uint256", "uint256"], bytes.fromhex(mint_sim_resp["result"][2:]))
        print(f"[reposition] mint() РЕАЛЬНО СИМУЛИРОВАН УСПЕШНО: liquidity={liq} amount0={real_amount0/10**USDC_DECIMALS} amount1={real_amount1/10**CBBTC_DECIMALS}")
        result["mint_simulation"] = {"success": True, "liquidity": liq, "amount0_usdc": real_amount0 / 10 ** USDC_DECIMALS, "amount1_cbbtc": real_amount1 / 10 ** CBBTC_DECIMALS}
    else:
        err = mint_sim_resp.get("error", {})
        decoded = decode_revert(err.get("data", ""))
        print(f"[reposition] mint() РЕАЛЬНО упал в симуляции: {err} -- декодировано: {decoded}")
        allowance_insufficient = allowance_usdc < amount0_desired or allowance_cbbtc < amount1_desired
        note = ("Реальный allowance СЕЙЧАС недостаточен (approve на прошлом входе был выдан ровно на "
                "использованную сумму и NFPM списал его целиком) -- эта симуляция, скорее всего, упала на STF "
                "(транзакция ПЕРЕД проверкой PSC), а не проверила PSC. Полноценная проверка PSC требует реального "
                "(дешёвого, ~$0.001) approve НЕПОСРЕДСТВЕННО перед этим же eth_call -- это и есть предлагаемый "
                "протокольный шаг, физически исполняется только на реальном входе (не здесь, только чтение)."
                if allowance_insufficient else
                "Allowance был реально достаточен -- этот revert РЕАЛЬНО про сам mint (возможно PSC), не про allowance.")
        print(f"[reposition] {note}")
        result["mint_simulation"] = {"success": False, "error": err, "decoded": decoded, "note": note}

    print("\n=== Реальный хедж ПОСЛЕ перестановки (размер, маржа, ликвидация) -- по свежим данным Lighter ===")
    account_full = lighter_account_full()
    market = lighter_btc_market()
    current_hedge_size_btc = 0.0
    leverage = None
    collateral_usd = None
    if account_full:
        collateral_usd = float(account_full.get("collateral", 0))
        btc_pos = next((p for p in account_full.get("positions", []) if str(p.get("symbol", "")).upper() == "BTC"
                         and abs(float(p.get("position", 0))) > 1e-9), None)
        if btc_pos:
            current_hedge_size_btc = abs(float(btc_pos.get("position", 0)))
            imf = float(btc_pos.get("initial_margin_fraction", 0))
            leverage = 100.0 / imf if imf else None
    mark_price_now = float(market["mark_price"]) if market else None
    mmf = float(market["maintenance_margin_fraction"]) / 10000 if market and market.get("maintenance_margin_fraction") is not None else None
    new_target_cbbtc = amount1_desired / 10 ** CBBTC_DECIMALS
    delta_short_btc = new_target_cbbtc - current_hedge_size_btc
    new_total_short_btc = new_target_cbbtc  # хедж = вся волатильная нога новой LP-позиции (тот же принцип, что вход)
    print(f"[reposition] реальный текущий шорт={current_hedge_size_btc} BTC, плечо={leverage}, mark_price=${mark_price_now}, "
          f"новая cbBTC-нога={new_target_cbbtc:.8f} -> ДЕЛЬТА шорта={delta_short_btc:+.8f} BTC -> новый ИТОГО шорт={new_total_short_btc:.8f} BTC")

    hedge_after_reposition = None
    if leverage and mark_price_now and mmf is not None and collateral_usd is not None:
        new_notional = new_total_short_btc * mark_price_now
        new_required_margin = new_notional / leverage
        new_available = collateral_usd - new_required_margin
        new_free_margin_pct = (new_available / collateral_usd * 100) if collateral_usd else None
        # Ликвидация -- ТА ЖЕ формула, что реальный вход (p6_live_step1.py::p_liq_formula),
        # avg_entry ЗДЕСЬ приближён текущим mark_price (реальный avg_entry появится только
        # после реального инкрементального ордера -- честно помечено как оценка).
        p_liq_formula_est = (collateral_usd + new_total_short_btc * mark_price_now) / (new_total_short_btc * (1 + mmf))
        print(f"[reposition] ОЦЕНКА после перестановки: notional=${new_notional:.4f} required_margin=${new_required_margin:.4f} "
              f"available=${new_available:.4f} free_margin={new_free_margin_pct:.2f}% ликвидация(оценка, avg_entry~=mark)=${p_liq_formula_est:.2f}")
        hedge_after_reposition = {
            "current_hedge_size_btc": current_hedge_size_btc, "delta_short_btc": delta_short_btc, "new_total_short_btc": new_total_short_btc,
            "leverage": leverage, "mark_price_usd": mark_price_now, "mmf": mmf, "collateral_usd": collateral_usd,
            "new_notional_usd": new_notional, "new_required_margin_usd": new_required_margin,
            "new_available_usd": new_available, "new_free_margin_pct": new_free_margin_pct,
            "liquidation_price_formula_usd_estimate": p_liq_formula_est,
            "note": "avg_entry_price приближён текущим mark_price для ОЦЕНКИ (реальный avg_entry после инкрементального ордера может отличаться).",
        }
    else:
        print("[reposition] не хватило реальных данных (leverage/mark_price/mmf/collateral) для оценки хеджа -- см. значения выше.")
    result["hedge_after_reposition_estimate"] = hedge_after_reposition

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[reposition] результат записан в {OUT_PATH}. ПОЗИЦИЯ НЕ ТРОНУТА -- это был только dry-run/симуляция.")


if __name__ == "__main__":
    main()
