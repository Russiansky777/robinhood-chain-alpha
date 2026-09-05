"""P6 -- честная проверка: тем же ли методом посчитан k=9.90 (скринер,
analysis/pool_screener_concentration.py::compute_k) и k=5.517/10.509
(dry-run перестановки, analysis/p6_reposition_dryrun.py::position_concentration_k)?

ОТВЕТ (реально прочитан код, не по памяти): НЕТ, это ДВЕ РАЗНЫЕ формулы,
отвечающие на РАЗНЫЕ вопросы:

  k_pool (скринер, снимок p6_hourly_snapshot.py::compute_k_concentration) =
      L_active_human / L_full_human, где
      L_active_human = РЕАЛЬНАЯ ончейн liquidity() пула (суммарная активная
        ликвидность ВСЕХ LP, чьи диапазоны сейчас покрывают текущую цену),
      L_full_human = sqrt(amount0_full * amount1_full) при гипотетическом
        депозите ВСЕГО TVL пула 50/50 НА ВЕСЬ диапазон цен.
      Отвечает: "насколько концентрирована ликвидность ВСЕГО ПУЛА прямо
      сейчас относительно наивного full-range депозита той же суммы?"
      НЕ параметризован диапазоном -- это одно число на пул, характеризующее
      совокупное поведение всех LP.

  k_range (dry-run перестановки, position_concentration_k) =
      1 / (1 - sqrt(Pa/Pb)), где Pa,Pb -- РЕАЛЬНЫЕ ценовые границы
      КОНКРЕТНОГО выбранного диапазона (например, моей позиции).
      Отвечает: "во сколько раз конкретный диапазон [Pa,Pb] эффективнее
      full-range депозита ДЛЯ ОДНОГО ДЕПОЗИТОРА, выбравшего именно этот
      диапазон?" Не требует знания TVL/liquidity() пула вообще -- чистая
      функция геометрии диапазона.

Это НЕ взаимозаменяемые метрики, и подставить одно вместо другого --
ошибка категории, а не просто иная нормировка (у пула НЕТ единого
диапазона [Pa,Pb] -- множество LP на разных диапазонах). Честный способ
перевести k_pool в ТУ ЖЕ нормировку, что k_range, -- РЕАЛЬНО прочитать
ончейн распределение ликвидности по тикам (TickBitmap + ticks()) и найти
РЕАЛЬНУЮ "полку" констатной активной ликвидности вокруг текущей цены
(ближайший инициализированный тик снизу и сверху от текущего) -- это
РЕАЛЬНЫЙ диапазон, для которого k_range уже параметризован корректно, а
НЕ придуманное обратное решение уравнения "какой % дал бы k=9.9".

Метод (стандартный Uniswap v3 TickBitmap.position(), сверено по реальному
исходнику github.com/Uniswap/v3-core, WebFetch 2026-09-05):
  compressed = floor(tick / tickSpacing)
  wordPos = compressed >> 8  (арифметический сдвиг, floor)
  bitPos = compressed % 256  (Python % с положительным модулем -- то же,
                               что uint8-обёртка в Solidity)
"""
import json
import time
from pathlib import Path

import requests

OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "p3_guard_cache" / "p6_pool_real_shelf_k_result.json"

BASE_RPC = "https://mainnet.base.org"
POOL_ADDRESS = "0x3e66e55e97ce60096f74b7c475e8249f2d31a9fb"
CBBTC_DECIMALS, USDC_DECIMALS = 8, 6

RPC_MIN_INTERVAL_S = 1.0
RPC_RETRY_BACKOFF_S = 15.0
RPC_MAX_RETRIES = 3
_last_rpc_call = 0.0


def _topic0(sig: str) -> str:
    from Crypto.Hash import keccak
    k = keccak.new(digest_bits=256)
    k.update(sig.encode())
    return "0x" + k.hexdigest()


def _selector(sig: str) -> str:
    return _topic0(sig)[:10]


def rpc(method: str, params: list):
    global _last_rpc_call
    wait = _last_rpc_call + RPC_MIN_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    for attempt in range(RPC_MAX_RETRIES + 1):
        _last_rpc_call = time.monotonic()
        r = requests.post(BASE_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
        if r.status_code == 429 and attempt < RPC_MAX_RETRIES:
            time.sleep(RPC_RETRY_BACKOFF_S)
            continue
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"{method} {params}: {body['error']}")
        return body["result"]
    raise RuntimeError("RPC 429 после ретраев")


def eth_call(to: str, data: str) -> str:
    return rpc("eth_call", [{"to": to, "data": data}, "latest"])


def read_pool_state() -> dict:
    slot0 = eth_call(POOL_ADDRESS, _selector("slot0()"))
    liquidity_raw = int(eth_call(POOL_ADDRESS, _selector("liquidity()")), 16)
    tick_spacing_raw = int(eth_call(POOL_ADDRESS, _selector("tickSpacing()")), 16)
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
    return (10 ** (CBBTC_DECIMALS - USDC_DECIMALS)) / (1.0001 ** tick)


def position_concentration_k(pa_usd: float, pb_usd: float) -> float:
    ratio = pa_usd / pb_usd
    return 1.0 / (1.0 - (ratio ** 0.5))


def tick_bitmap_word(word_pos: int) -> int:
    # int16 wordPos -> abi-encode как int256 (со знаком)
    enc = word_pos & ((1 << 256) - 1)
    data = "0x" + _selector("tickBitmap(int16)")[2:] + format(enc, "064x")
    raw = eth_call(POOL_ADDRESS, data)
    return int(raw, 16)


def word_bits_set(word: int) -> list[int]:
    return [b for b in range(256) if (word >> b) & 1]


def tick_liquidity_net(real_tick: int) -> int:
    """ticks(int24) -- берём только liquidityNet (второе поле int128)."""
    enc = real_tick & ((1 << 256) - 1)
    data = "0x" + _selector("ticks(int24)")[2:] + format(enc, "064x")
    raw = eth_call(POOL_ADDRESS, data)
    # ticks() возвращает несколько полей; liquidityNet -- 2-е слово (int128, упаковано в 32 байта)
    word2 = raw[2:][64:128]
    val = int(word2, 16)
    return val - (1 << 256) if val >= (1 << 255) else val


def main():
    result = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    pool = read_pool_state()
    ts = pool["tick_spacing"]
    current_tick = pool["tick"]
    p0 = price_cbbtc_usd(pool["sqrtPriceX96"])
    print(f"[shelf_k] реальный пул: tick={current_tick} tickSpacing={ts} price=${p0:.2f} liquidity_raw={pool['liquidity_raw']}")

    compressed_now = current_tick // ts
    word_pos_now = compressed_now >> 8
    print(f"[shelf_k] compressed_now={compressed_now} word_pos_now={word_pos_now}")

    initialized_compressed: list[int] = []
    for wp in (word_pos_now - 1, word_pos_now, word_pos_now + 1):
        word = tick_bitmap_word(wp)
        bits = word_bits_set(word)
        for b in bits:
            initialized_compressed.append(wp * 256 + b)
        print(f"[shelf_k] word {wp}: {len(bits)} инициализированных бит")

    initialized_ticks_real = sorted(c * ts for c in initialized_compressed)
    print(f"[shelf_k] реальные инициализированные тики рядом (compressed*tickSpacing): {initialized_ticks_real}")
    result["initialized_ticks_real"] = initialized_ticks_real

    tick_below = max((t for t in initialized_ticks_real if t <= current_tick), default=None)
    tick_above = min((t for t in initialized_ticks_real if t > current_tick), default=None)
    print(f"[shelf_k] реальная 'полка' постоянной активной ликвидности вокруг текущей цены: [{tick_below}, {tick_above})")

    if tick_below is None or tick_above is None:
        result["error"] = "не нашли обе границы полки в 3 проверенных словах -- нужно расширить окно поиска (не реализовано в этой версии)."
        print(f"[shelf_k] {result['error']}")
    else:
        pa_shelf = tick_to_usd_price(tick_above)  # выше tick -> ниже цена (см. usd_price_to_tick)
        pb_shelf = tick_to_usd_price(tick_below)
        k_shelf = position_concentration_k(pa_shelf, pb_shelf)
        print(f"[shelf_k] РЕАЛЬНАЯ полка: границы тиков [{tick_below},{tick_above}] -> цены ${pa_shelf:.2f}..${pb_shelf:.2f} "
              f"-> k_range(той же формулой, что 5.517/10.509) = {k_shelf:.4f}")
        result["shelf"] = {
            "tick_below": tick_below, "tick_above": tick_above,
            "pa_usd": pa_shelf, "pb_usd": pb_shelf,
            "k_range_same_formula": k_shelf,
        }

        # Реальная сверка: liquidityNet в tick_below должен объяснять
        # (хотя бы частично) переход к текущей liquidity() -- диагностика,
        # не входит в k, просто показывает реальные данные тика.
        net_below = tick_liquidity_net(tick_below)
        net_above = tick_liquidity_net(tick_above)
        result["shelf"]["liquidity_net_at_tick_below"] = net_below
        result["shelf"]["liquidity_net_at_tick_above"] = net_above
        print(f"[shelf_k] liquidityNet(tick_below={tick_below})={net_below} liquidityNet(tick_above={tick_above})={net_above}")

    result["pool_level_k_from_tvl_formula"] = "см. p6_hourly_snapshot.py::compute_k_concentration -- реально наблюдался 9.88-10.10 на снимках 2026-09-05"
    result["note"] = ("k_pool (TVL-формула) и k_range (геометрия диапазона) -- РАЗНЫЕ метрики, отвечающие на разные вопросы "
                       "(см. докстринг файла). 'shelf.k_range_same_formula' выше -- честный перевод РЕАЛЬНОГО текущего состояния "
                       "пула (не гипотезы, а реально прочитанного ближайшего диапазона constant-liquidity вокруг текущей цены) "
                       "в ТУ ЖЕ нормировку, что 5.517/10.509/10.475.")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[shelf_k] результат записан в {OUT_PATH}")


if __name__ == "__main__":
    main()
