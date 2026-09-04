#!/usr/bin/env python3
"""P5 LIVE -- первая реальная точка доходности живой позиции (tokenId
1000756) + первая точка почасового ряда комиссий (владелец, 2026-09-03,
две смежные задачи одним прогоном: "снять первую реальную точку
доходности" и "снимать [комиссии] раз в час... Минимум 24 точки до
запуска демона").

ТОЛЬКО ЧТЕНИЕ. Ни decreaseLiquidity, ни collect(), ни Lighter-ордера НЕ
отправляются -- ни одной подписанной транзакции, ни одного приватного
ключа не требуется.

Несобранные комиссии -- НЕ через `positions(tokenId).tokensOwed0/1`
(владелец, дословно: "они обновляются только после poke и покажут
нули"). Подтверждено реальным источником (Uniswap/v3-periphery
NonfungiblePositionManager.sol::collect): `tokensOwed` в сторадже
обновляется только явным mint/increaseLiquidity/decreaseLiquidity/
collect на САМОЙ позиции -- если с момента открытия не было ни одного
такого вызова, `positions()` покажет 0 даже при реально накопленных
комиссиях. `collect()` же ПЕРЕД возвратом суммы сам вызывает
`pool.burn(tickLower, tickUpper, 0)` (нулевой burn -- официальный
паттерн "poke" из того же контракта) и пересчитывает feeGrowthInside
СВЕЖИМ значением -- поэтому `eth_call` (staticcall-эквивалент: нода
выполняет транзакцию локально и возвращает результат, ничего не
подписывается и не публикуется в mempool) на `collect()` с
`amount0Max=amount1Max=type(uint128).max` даёт РЕАЛЬНУЮ актуальную
сумму несобранных комиссий на этот блок, без реальной отправки.

Пишет два файла:
  - data/p3_guard_cache/p5_live_snapshot_1000756_result.json -- полный
    отчёт этого прогона (цена/диапазон, хедж на Lighter, дельта-чек,
    экономика).
  - data/p5_fee_accrual.jsonl -- ОДНА строка добавляется КАЖДЫЙ прогон
    (append-only, схема владельца): timestamp, block, fees0, fees1,
    pool_price, in_range, free_margin_usd, margin_call_price,
    fee_capture_ratio, gas_spent_cumulative_usd. Предназначен для
    почасового запуска (см. .github/workflows/run_p5_live_position_
    snapshot.yml) -- минимум 24 строки нужны до включения демона
    ребаланса.

ОБНОВЛЕНО (владелец, 2026-09-03/04, задача расширения отчёта):
  - `min_base_amount`/`size_decimals`/`min_quote_amount` рынка ETH-перпа
    на Lighter -- реальное чтение `GET /api/v1/orderBookDetails`
    (то же, что `pc.lighter_eth_perp()` уже делал для mark_price), не
    из документации. Нужно для порога хеджа -- минимальный лот/шаг/
    нотионал ограничивают, насколько мелко можно ребалансировать.
  - `free_margin_usd` (абсолют) и `margin_call_price` -- цена ETH, при
    которой свободная маржа (initial margin) обнуляется. ЭТО НЕ
    `liquidation_price` (та считается по maintenance margin, более
    низкому порогу) -- выведена той же техникой, что формула §5
    PROJECT_STATE.md (текущий collateral/size/mark, без реконструкции
    цифр на момент входа): `P_margin_call = (collateral + size×P_now) /
    (size×(1 + 1/leverage))`.
  - `fee_capture_ratio_interval` (переименовано из `fee_capture_ratio`) =
    (наши комиссии за интервал / наш reserve_usd) ÷ (комиссии пула за
    интервал / TVL пула). НАЙДЕНО (владелец, 2026-09-04, реальные две
    точки дали 0.57 и 0.109 -- пятикратный разброс за час): накопление
    комиссий в коротком интервале (~0.27ч) пуассоновское, попал крупный
    своп в окно или нет решает всё -- поинтервальное отношение не
    сходится ни к чему, сколько его ни собирай. **Оставлено в отчёте
    как шумная диагностика (видно, чтобы не удивляться разбросу), для
    решений НЕ используется** -- см. `fee_capture_ratio_cumulative` ниже.
  - `fee_capture_ratio_cumulative` -- тот же принцип, но НАКОПИТЕЛЬНО с
    момента открытия позиции: числитель -- наши комиссии с открытия
    (сам `callStatic collect()` уже накопительный, суммировать не
    нужно) делить на текущую стоимость LP-ноги; знаменатель -- сумма
    РЕАЛЬНОГО почасового объёма пула (`ohlcv/hour?currency=usd`,
    свечи с `timestamp >= position_open_ts`, НЕ `volume_usd.h24` --
    это скользящее окно, соседние снятия перекрываются на 23ч и
    складывать их нельзя) × 0.0001, делить на СРЕДНИЙ TVL пула по всем
    точкам `data/p5_fee_accrual.jsonl` (не последний снимок -- TVL
    гуляет, числитель накоплен за весь период, знаменатель должен быть
    за тот же). Все входы -- отдельными полями для аудита.
  - `gas_spent_cumulative_usd` -- ОТДЕЛЬНОЕ поле, намеренно вынесено из
    блока `economics`, чтобы газ не "размазывался" внутри доходности
    (владелец, дословно). Пока = газ входа (`total_gas_spent_usd_est`
    из state) -- ребалансов ещё не было, демон не написан.

ОБНОВЛЕНО (задача LVR, 2026-09-04): дельта-хедж снимает только ПЕРВЫЙ
порядок риска (направление). LVR (loss-versus-rebalancing, Milionis et
al.) -- ВТОРОЙ порядок (гамма), хедж его не убирает вообще. Формулы
(для v3-позиции: `x(P)=L×(1/√P−1/√P_upper)`, `V(P)=x(P)×P+y(P)`):
  - Гамма `Γ = d²V/dP² = −L/(2×P^1.5)`.
  - LVR-темп (непрерывный ребаланс) = `(σ²/2)×P²×|Γ| = σ²×L×√P/4`
    (в $/год, той же формулой, что доказанная теория Milionis).
  - Бенчмарк статического хеджа (без ребаланса до этой точки) --
    квадратичное приближение по Тейлору вокруг `P_open`:
    `−(L/(4×P_open^1.5))×(P_t−P_open)²`. Должен совпадать с
    `combined_pnl_ex_fees` (LP P&L + хедж P&L, без комиссий) с
    точностью до фандинга/клиппинга на границах -- если разойдётся
    заметно, это баг в L/тиках/чтении, докладывается сразу.
`L` -- РЕАЛЬНАЯ ончейн-ликвидность позиции (`pos["liquidity"]`,
`uint128`, читается каждый прогон, не задаётся числом), переведена в
"человеческую" шкалу (та же, в которой `P` выражена в USD, а не в
raw-sqrt-Q96-единицах): `L_human = liquidity_raw / 10**((WETH_DECIMALS
+ USDG_DECIMALS)//2)` -- точное тождество (не аппроксимация), выведено
из `price_from_tick()`: `amount0_eth(P) = L_human×(1/√P−1/√P_upper)`
эквивалентно raw-формуле `amount0_raw = L_raw×(1/√P_raw−1/√Pb_raw)`
после деления на `10**WETH_DECIMALS` и подстановки `√P_raw=√P_usd×
10**-(WETH_DECIMALS-USDG_DECIMALS)/2` -- проверено численно (L_human
получается ≈40.19, совпадает по порядку с `computed_liquidity` из
предпрогонного плана открытия, ~39.82).

σ для LVR-темпа -- РЕАЛИЗОВАННАЯ волатильность по САМОМУ ряду
`data/p5_fee_accrual.jsonl` (log-returns между соседними точками,
квадратичная вариация / фактическое прошедшее время в годах -- НЕ
считаем шаги часовыми, там есть разрыв 2.91ч), НЕ историческая 45.5%
из отдельного 52-дневного OHLCV-скана (`p5_gt_pool_history.py`) -- та
измеряет другой период и другую (более широкую) выборку.

Фандинг хеджа на Lighter -- реальное поле позиции `total_funding_paid_out`
(вместе с `realized_pnl`) -- прочитаны РЯДОМ с уже используемым
`unrealized_pnl`/`liquidation_price` в том же ответе `GET /api/v1/account`,
раньше не извлекались. `cross_initial_margin_requirement` (реальное
поле account-уровня, cross-margin) заменяет ФОРМУЛЬНУЮ оценку требуемой
маржи в сверке `Δfree_margin` -- убирает источник невязки, а не
объясняет её постфактум.

Обратная сторона: поля `realized_pnl`/`total_funding_paid_out`/
`cross_initial_margin_requirement` НЕ читались до этого обновления --
для уже собранных точек ряда сверка маржи и фандинг честно `null`
(бэкфилл невозможен без выдумывания), новые точки получают их с этого
прогона. LVR/статический-хедж бэкфиллятся полностью (нужны только
liquidity/тики из git-history полного отчёта прошлых прогонов --
реальные, не переисполненные задним числом).
"""
from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests  # noqa: E402
from eth_abi import decode as abi_decode, encode as abi_encode  # noqa: E402
from eth_utils import to_checksum_address  # noqa: E402

from alchemy_fallback import _rpc_call, get_block_number, topic0  # noqa: E402
import p5_live_precheck as pc  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/p5_live_snapshot_1000756_result.json")
ACCRUAL_LOG_PATH = Path("data/p5_fee_accrual.jsonl")
STATE_PATH = Path("data/p5_live_position_state.json")

TOKEN_ID = 1000756
NFPM = "0x73991a25c818bf1f1128deaab1492d45638de0d3"
WALLET = pc.WALLET
WETH_DECIMALS, USDG_DECIMALS = pc.WETH_DECIMALS, pc.USDG_DECIMALS
MAX_UINT128 = 2 ** 128 - 1
RANGE_PCT = pc.RANGE_PCT  # 0.10 -- тот же параметр, что использовался при открытии
POOL_FEE_FRACTION = 0.0001  # 0.01%, подтверждено ончейн (fee=100/1e6, docs/P5_HEDGED_LP.md §1)

GT_NETWORK = "robinhood"  # подтверждено реальным вызовом, p5_gt_pool_history.py, run 33814194154
GT_POOL_ADDRESS = "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca"
GT_RATE_LIMIT_BACKOFF_S = 65.0  # тот же ретрай, что p5_gt_pool_history.py -- реальный 429 словлен уже на ~6-м вызове/мин
GT_RATE_LIMIT_MAX_RETRIES = 2
GT_HOURLY_MAX_PAGES = 6  # 6000 часовых свечей ~= 250 дней -- с большим запасом на срок жизни позиции

L_HUMAN_DIVISOR = 10 ** ((WETH_DECIMALS + USDG_DECIMALS) // 2)  # 1e12 -- точное тождество, см. докстринг модуля

COLLECT_SIG = "collect((uint256,address,uint128,uint128))"


def _selector(sig: str) -> str:
    return topic0(sig)[:10]


def collect_static_call(token_id: int, recipient: str) -> tuple[int, int]:
    """eth_call (НЕ транзакция, ничего не подписывается и не
    отправляется) на NonfungiblePositionManager.collect() -- см.
    докстринг модуля почему это даёт РЕАЛЬНУЮ актуальную сумму, а не
    устаревший tokensOwed. from=WALLET нужен только чтобы пройти
    onlyAuthorizedForToken на стороне симуляции ноды."""
    selector = bytes.fromhex(_selector(COLLECT_SIG)[2:])
    data = selector + abi_encode(
        ["(uint256,address,uint128,uint128)"],
        [(token_id, to_checksum_address(recipient), MAX_UINT128, MAX_UINT128)],
    )
    raw = _rpc_call("eth_call", [{
        "to": to_checksum_address(NFPM), "from": to_checksum_address(WALLET),
        "data": "0x" + data.hex(),
    }, "latest"])
    amount0, amount1 = abi_decode(["uint256", "uint256"], bytes.fromhex(raw[2:]))
    return amount0, amount1


def price_from_tick(tick: int) -> float:
    """Та же decimals-поправка, что pc.price_from_sqrt() -- 1.0001**tick
    ЭКВИВАЛЕНТНО (sqrtPriceX96/2**96)**2 по определению Uniswap v3."""
    return (1.0001 ** tick) * (10 ** (WETH_DECIMALS - USDG_DECIMALS))


def fetch_gt_pool_snapshot_soft() -> dict | None:
    """Снимок /pools/{addr} с GeckoTerminal -- ТОЛЬКО для fee_capture_ratio
    (сравнение с пуловым средним), МЯГКИЙ отказ при любой проблеме (429,
    таймаут, сеть) -- это вспомогательная метрика, не должна ронять
    основной ончейн/Lighter-снимок позиции. Один вызов, без ретрая
    (см. p5_gt_pool_history.py для полноценного ретрая на 429 -- здесь
    сознательно проще, почасовой cadence сам по себе далеко от лимита
    30/мин на ОДИН вызов)."""
    try:
        r = requests.get(f"https://api.geckoterminal.com/api/v2/networks/{GT_NETWORK}/pools/{GT_POOL_ADDRESS}",
                          headers={"Accept": "application/json;version=20230302"}, timeout=20)
        if r.status_code != 200:
            print(f"[snapshot] GT /pools/{{addr}} вернул HTTP {r.status_code} -- fee_capture_ratio пропущен в этом прогоне")
            return None
        return r.json().get("data", {}).get("attributes", {})
    except Exception as e:  # noqa: BLE001
        print(f"[snapshot] GT /pools/{{addr}} недоступен ({e}) -- fee_capture_ratio пропущен в этом прогоне")
        return None


def _gt_get_with_retry(url: str, params: dict) -> tuple[int | None, dict | str | None]:
    """GET с ретраем на 429 (тот же бэкофф, что p5_gt_pool_history.py --
    реальный лимит оказался жёстче заявленных 30/мин). Используется ТОЛЬКО
    для кумулятивного fee_capture_ratio -- часового объёма пула."""
    status, body = None, None
    for attempt in range(GT_RATE_LIMIT_MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, headers={"Accept": "application/json;version=20230302"}, timeout=20)
            status, body = r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:300])
        except Exception as e:  # noqa: BLE001
            print(f"[snapshot] GT hourly OHLCV: сетевая ошибка {e}")
            return None, None
        if status == 429 and attempt < GT_RATE_LIMIT_MAX_RETRIES:
            print(f"[snapshot] GT hourly OHLCV 429 (попытка {attempt + 1}/{GT_RATE_LIMIT_MAX_RETRIES + 1}) -- жду {GT_RATE_LIMIT_BACKOFF_S:.0f}с")
            time.sleep(GT_RATE_LIMIT_BACKOFF_S)
            continue
        break
    return status, body


def fetch_hourly_volume_usd_since(since_ts_unix: int) -> tuple[float | None, int, int | None, int | None]:
    """Реальный почасовой объём пула в USD (`currency=usd`, НЕ `token` --
    нужен именно объём в долларах, не в ETH) с `timestamp >= since_ts_unix`
    (владелец: "снимки h24 -- скользящее окно, соседние точки
    перекрываются на 23 часа, суммировать нельзя -- берём фактический
    почасовой объём из OHLCV"). Возвращает (сумма_volume_usd,
    n_свечей_учтено, самый_ранний_учтённый_ts, самый_поздний_ts).
    (None, 0, None, None), если свечей с нужным timestamp ещё нет
    (позиция открыта меньше часа назад) -- НЕ придумываем частичную
    свечу, честный null (владелец, п.4 задачи)."""
    all_rows: dict[int, list] = {}
    before_ts: int | None = None
    hit_older_than_since = False
    for _ in range(GT_HOURLY_MAX_PAGES):
        params = {"aggregate": 1, "limit": 1000, "currency": "usd", "include_empty_intervals": "true"}
        if before_ts is not None:
            params["before_timestamp"] = before_ts
        status, body = _gt_get_with_retry(
            f"https://api.geckoterminal.com/api/v2/networks/{GT_NETWORK}/pools/{GT_POOL_ADDRESS}/ohlcv/hour", params)
        if status != 200 or not isinstance(body, dict):
            print(f"[snapshot] GT hourly OHLCV: HTTP {status} -- {str(body)[:200]} -- останов пагинации")
            break
        rows = body.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        if not rows:
            break
        for row in rows:
            ts = int(row[0])
            if ts >= since_ts_unix:
                all_rows[ts] = row
            else:
                hit_older_than_since = True
        oldest_ts = min(int(row[0]) for row in rows)
        if len(rows) < 1000 or hit_older_than_since:
            break
        before_ts = oldest_ts
    if not all_rows:
        return None, 0, None, None
    volume_sum = sum(float(row[5]) for row in all_rows.values())
    ts_sorted = sorted(all_rows.keys())
    return volume_sum, len(all_rows), ts_sorted[0], ts_sorted[-1]


def read_all_accrual_pool_tvls() -> list[float]:
    """Все реально сохранённые `pool_reserve_in_usd` из data/p5_fee_accrual.jsonl
    (пропуская строки без этого поля -- старые точки/сбои GT) -- для
    СРЕДНЕГО TVL пула за период накопления (владелец: "не последний
    снимок, среднее"). Не включает текущую точку -- та добавляется
    отдельно к списку перед усреднением (см. run())."""
    if not ACCRUAL_LOG_PATH.exists():
        return []
    tvls = []
    with ACCRUAL_LOG_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("pool_reserve_in_usd") is not None:
                tvls.append(float(row["pool_reserve_in_usd"]))
    return tvls


def read_all_accrual_price_series() -> list[tuple[datetime, float]]:
    """Все (timestamp, pool_price_usd) из data/p5_fee_accrual.jsonl -- для
    реализованной sigma по самому ряду (см. sigma_realized_annualized_from_series)."""
    if not ACCRUAL_LOG_PATH.exists():
        return []
    series = []
    with ACCRUAL_LOG_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            ts = datetime.strptime(row["timestamp_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            series.append((ts, row["pool_price_usd"]))
    return series


def read_last_accrual_entry() -> dict | None:
    """Последняя строка data/p5_fee_accrual.jsonl ДО добавления текущей --
    нужна для расчёта комиссий/интервала (fee_capture_ratio). None на
    первом прогоне (нет предыдущей точки для интервала)."""
    if not ACCRUAL_LOG_PATH.exists():
        return None
    last_line = None
    with ACCRUAL_LOG_PATH.open() as f:
        for line in f:
            line = line.strip()
            if line:
                last_line = line
    return json.loads(last_line) if last_line else None


def lp_value_usd(L_human: float, tick_lower: int, tick_upper: int, P_usd: float) -> float:
    """V(P) в USDG для v3-позиции в "человеческой" шкале (P в USD, L_human
    из liquidity_raw/L_HUMAN_DIVISOR, см. докстринг модуля) -- используется
    и для V(P_t) (сверка с our_reserve_usd_now), и для V(P_open) (LVR).
    Клиппинг на границах диапазона -- позиция вне диапазона держит
    единственный актив, оценённый по РЕАЛЬНОЙ (не клиппированной) цене."""
    Pa, Pb = price_from_tick(tick_lower), price_from_tick(tick_upper)
    if Pa > Pb:
        Pa, Pb = Pb, Pa
    P_clamped = min(max(P_usd, Pa), Pb)
    x = max(L_human * (1 / math.sqrt(P_clamped) - 1 / math.sqrt(Pb)), 0.0)
    y = max(L_human * (math.sqrt(P_clamped) - math.sqrt(Pa)), 0.0)
    return x * P_usd + y


def sigma_realized_annualized_from_series(points: list[tuple[datetime, float]]) -> dict:
    """Реализованная годовая sigma по РЯДУ (timestamp, pool_price_usd),
    log-returns + квадратичная вариация / фактическое прошедшее время в
    годах -- НЕ считаем шаги часовыми (владелец: "там есть разрыв 2.91ч").
    Нужно >=2 точки для одного return, >=3 для содержательной оценки
    (одна точка данных даёт технически валидную, но крайне шумную sigma
    -- честно помечается малым n, не скрывается)."""
    pts = sorted(points, key=lambda p: p[0])
    if len(pts) < 2:
        return {"sigma_realized_annualized": None, "n_returns": 0, "note": "меньше 2 точек -- sigma не считается."}
    log_returns = []
    total_seconds = 0.0
    for i in range(1, len(pts)):
        t0, p0 = pts[i - 1]
        t1, p1 = pts[i]
        dt_s = (t1 - t0).total_seconds()
        if dt_s <= 0 or p0 <= 0 or p1 <= 0:
            continue
        log_returns.append(math.log(p1 / p0))
        total_seconds += dt_s
    if not log_returns or total_seconds <= 0:
        return {"sigma_realized_annualized": None, "n_returns": 0, "note": "нет валидных интервалов."}
    quadratic_variation = sum(r ** 2 for r in log_returns)
    total_years = total_seconds / (365.25 * 24 * 3600)
    annualized_variance = quadratic_variation / total_years
    sigma = math.sqrt(annualized_variance)
    return {
        "sigma_realized_annualized": sigma, "n_returns": len(log_returns),
        "total_hours_covered": total_seconds / 3600, "quadratic_variation": quadratic_variation,
        "note": ("ОЧЕНЬ малая выборка -- статистически ненадёжно, только диагностика" if len(log_returns) < 20 else None),
    }


def raw_sqrt_from_tick(tick: int) -> float:
    """СЫРОЙ (без decimals-поправки) sqrt(1.0001**tick) -- нужен для
    amount0/1 формулы в raw wei/raw-unit, ТА ЖЕ математика, что
    p5_live_close.py::close_position() (см. её докстринг про
    зафиксированный баг 78 млрд ETH у human-adjusted версии в
    p5_live_precheck.py::v3_amounts -- здесь сознательно НЕ переиспользуется)."""
    return (1.0001 ** tick) ** 0.5


def run() -> int:
    t0 = time.time()
    now_utc = datetime.now(timezone.utc)

    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    if not state:
        print("[snapshot] ПРЕДУПРЕЖДЕНИЕ: data/p5_live_position_state.json не найден -- "
              "экономика (часы/APR) не будет посчитана, только фактическое чтение.")

    # === 1. Свежая NFT-позиция + callStatic collect (несобранные комиссии) ===
    print("=== 1. NFPM.positions(1000756) + callStatic collect() (только чтение) ===")
    pos = pc.nfpm_position(TOKEN_ID, NFPM)
    if not pos.get("found"):
        result = {"generated_at_utc": now_utc.isoformat(), "abort_reason": "nfpm_position() не нашла tokenId 1000756."}
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        print(f"[snapshot] {result['abort_reason']}")
        return 1
    print(f"[snapshot] positions(): liquidity={pos['liquidity']} tokensOwed0={pos['tokens_owed0']} "
          f"tokensOwed1={pos['tokens_owed1']} tick_lower={pos['tick_lower']} tick_upper={pos['tick_upper']}")

    collect_amount0_raw, collect_amount1_raw = collect_static_call(TOKEN_ID, WALLET)
    fees0_eth = collect_amount0_raw / 10 ** WETH_DECIMALS
    fees1_usdg = collect_amount1_raw / 10 ** USDG_DECIMALS
    print(f"[snapshot] callStatic collect() -> НЕСОБРАННЫЕ КОМИССИИ: {fees0_eth:.10f} ETH + {fees1_usdg:.6f} USDG "
          f"(tokensOwed0/1 в positions() выше -- заведомо устаревшие/нулевые, не факт)")

    # === 2. Свежая цена пула против диапазона ===
    print("\n=== 2. Цена пула против диапазона ===")
    pool = pc.read_pool_state()
    pool_price_now = pc.price_from_sqrt(pool["sqrt_price_x96"])
    current_tick = pool["tick"]
    range_lower = price_from_tick(pos["tick_lower"])
    range_upper = price_from_tick(pos["tick_upper"])
    in_range = pos["tick_lower"] <= current_tick < pos["tick_upper"]
    pos_pct_in_range = (pool_price_now - range_lower) / (range_upper - range_lower) * 100 if range_upper != range_lower else None
    pct_drop_to_lower = (pool_price_now - range_lower) / pool_price_now * 100
    pct_rise_to_upper = (range_upper - pool_price_now) / pool_price_now * 100
    nearest_edge = "lower" if pct_drop_to_lower <= pct_rise_to_upper else "upper"

    entry_price = state.get("pool_price_usd_entry")
    pct_of_10pct_path_to_nearest_edge = None
    move_from_entry_pct = None
    if entry_price:
        move_from_entry_pct = (pool_price_now - entry_price) / entry_price * 100
        edge_price = range_lower if move_from_entry_pct < 0 else range_upper
        denom = abs(edge_price - entry_price)
        pct_of_10pct_path_to_nearest_edge = (abs(pool_price_now - entry_price) / denom * 100) if denom else None

    print(f"[snapshot] pool_price_now=${pool_price_now:.4f} диапазон=[${range_lower:.2f}, ${range_upper:.2f}] "
          f"in_range={in_range} позиция_в_диапазоне={pos_pct_in_range:.2f}% ближайшая_граница={nearest_edge}")
    if move_from_entry_pct is not None:
        print(f"[snapshot] движение от входа (${entry_price:.4f}): {move_from_entry_pct:+.4f}%, "
              f"пройдено {pct_of_10pct_path_to_nearest_edge:.2f}% пути ±10% до {nearest_edge}-границы")

    # === 3. Lighter -- текущее состояние хеджа ===
    print("\n=== 3. Lighter, аккаунт 22012 -- текущее состояние хеджа (только чтение, публичный API) ===")
    account_full = pc.lighter_account_full()
    eth_pos = None
    if account_full:
        eth_pos = next((p for p in account_full.get("positions", []) if str(p.get("symbol", "")).upper() == "ETH"), None)
    eth_market = pc.lighter_eth_perp()
    lighter_mark_price_now = float(eth_market["mark_price"]) if eth_market else None
    real_leverage = pc.real_eth_leverage(account_full)

    # Метаданные рынка (лот/шаг/минимальный нотионал) -- реальное чтение
    # GET /api/v1/orderBookDetails (то же eth_market), НЕ из документации
    # (владелец, задача расширения отчёта, 2026-09-04). Единственная
    # цифра, которой не хватало для порога хеджа -- насколько мелко
    # можно ребалансировать.
    lighter_market_meta = None
    if eth_market:
        min_base_amount = float(eth_market["min_base_amount"]) if eth_market.get("min_base_amount") is not None else None
        size_decimals = eth_market.get("size_decimals")
        lot_step = (10 ** -size_decimals) if size_decimals is not None else None
        min_quote_amount = float(eth_market["min_quote_amount"]) if eth_market.get("min_quote_amount") is not None else None
        lighter_market_meta = {
            "min_base_amount_eth": min_base_amount, "size_decimals": size_decimals,
            "lot_step_eth": lot_step, "min_quote_amount_usd": min_quote_amount,
            "price_decimals": eth_market.get("price_decimals"),
        }
        print(f"[snapshot] Lighter ETH market meta (реально, orderBookDetails): "
              f"min_base_amount={min_base_amount} ETH, шаг лота=10^-{size_decimals}={lot_step} ETH, "
              f"min_quote_amount=${min_quote_amount}")

    lighter_hedge_now: dict = {"found": eth_pos is not None}
    if eth_pos:
        real_hedge_size_eth = abs(float(eth_pos.get("position", 0)))
        liq_price = float(eth_pos["liquidation_price"]) if eth_pos.get("liquidation_price") not in (None, "") else None
        unrealized_pnl = float(eth_pos.get("unrealized_pnl", 0))
        avg_entry_price = float(eth_pos.get("avg_entry_price", 0))
        dist_to_liq_pct_now = ((liq_price / lighter_mark_price_now - 1) * 100) if (liq_price and lighter_mark_price_now) else None
        collateral_usd = float(account_full.get("collateral", 0))
        available_usd = float(account_full.get("available_balance", 0))
        free_margin_pct_now = (available_usd / collateral_usd * 100) if collateral_usd else None
        free_margin_usd = available_usd  # то же самое поле биржи, явное имя по просьбе владельца -- абсолют, не только %

        # Реальные поля позиции/аккаунта для сверки маржи и фандинга (задача
        # LVR, 2026-09-04) -- РЯДОМ с уже читаемыми unrealized_pnl/
        # liquidation_price в том же ответе, раньше просто не извлекались.
        realized_pnl_usd = float(eth_pos["realized_pnl"]) if eth_pos.get("realized_pnl") not in (None, "") else None
        total_funding_paid_out_usd = (float(eth_pos["total_funding_paid_out"])
                                       if eth_pos.get("total_funding_paid_out") not in (None, "") else None)
        cross_initial_margin_requirement_usd = (float(account_full["cross_initial_margin_requirement"])
                                                 if account_full.get("cross_initial_margin_requirement") not in (None, "") else None)
        cross_maintenance_margin_requirement_usd = (float(account_full["cross_maintenance_margin_requirement"])
                                                     if account_full.get("cross_maintenance_margin_requirement") not in (None, "") else None)

        # margin_call_price -- цена ETH, при которой initial-margin буфер
        # обнуляется (НЕ liquidation_price -- та считается по maintenance
        # margin, более низкому порогу, см. §5 PROJECT_STATE.md). Та же
        # техника вывода, что и там: только ТЕКУЩИЕ наблюдаемые величины
        # (collateral/size/mark/leverage), без реконструкции состояния на
        # момент входа. Вывод (шорт, cross-margin):
        #   collateral(P) = collateral_now - size×(P - P_now)
        #   required_initial_margin(P) = size×P / leverage
        #   collateral(P) = required_initial_margin(P)  =>
        #   P_margin_call = (collateral_now + size×P_now) / (size×(1 + 1/leverage))
        leverage_val = real_leverage.get("leverage") if real_leverage.get("found") else None
        margin_call_price = None
        if leverage_val and real_hedge_size_eth and lighter_mark_price_now:
            margin_call_price = ((collateral_usd + real_hedge_size_eth * lighter_mark_price_now) /
                                  (real_hedge_size_eth * (1 + 1 / leverage_val)))
        margin_call_move_pct_from_current = (
            (margin_call_price / lighter_mark_price_now - 1) * 100
        ) if (margin_call_price and lighter_mark_price_now) else None

        # Кросс-проверка формулой §5 паспорта (P0/size ФИКСИРОВАНЫ при
        # входе позиции, collateral -- ТЕКУЩИЙ, cross-margin пулит по
        # всему аккаунту) -- сверка с реальным полем liquidation_price,
        # не замена ему.
        mmf_formula_check = None
        if eth_market and eth_market.get("maintenance_margin_fraction") is not None:
            mmf = float(eth_market["maintenance_margin_fraction"]) / 10000
            p0 = avg_entry_price
            p_liq_formula = (collateral_usd + real_hedge_size_eth * p0) / (real_hedge_size_eth * (1 + mmf)) if real_hedge_size_eth else None
            mmf_formula_check = {"mmf": mmf, "p_liq_formula": p_liq_formula,
                                  "p_liq_real": liq_price, "diff_abs": (p_liq_formula - liq_price) if (p_liq_formula and liq_price) else None}

        lighter_hedge_now.update({
            "symbol": eth_pos.get("symbol"), "sign": eth_pos.get("sign"),
            "position_size_eth": real_hedge_size_eth, "avg_entry_price_usd": avg_entry_price,
            "unrealized_pnl_usd": unrealized_pnl, "liquidation_price_usd": liq_price,
            "lighter_mark_price_now_usd": lighter_mark_price_now,
            "distance_to_liquidation_pct_from_current_price": dist_to_liq_pct_now,
            "collateral_usd": collateral_usd, "available_balance_usd": available_usd,
            "free_margin_pct_now": free_margin_pct_now, "free_margin_usd": free_margin_usd,
            "margin_call_price_usd": margin_call_price,
            "margin_call_move_pct_from_current": margin_call_move_pct_from_current,
            "current_leverage": real_leverage,
            "liquidation_formula_cross_check": mmf_formula_check,
            "realized_pnl_usd": realized_pnl_usd, "total_funding_paid_out_usd": total_funding_paid_out_usd,
            "cross_initial_margin_requirement_usd": cross_initial_margin_requirement_usd,
            "cross_maintenance_margin_requirement_usd": cross_maintenance_margin_requirement_usd,
        })
        print(f"[snapshot] Lighter ETH-позиция: size={real_hedge_size_eth} avg_entry=${avg_entry_price} "
              f"unrealized_pnl=${unrealized_pnl} liquidation_price=${liq_price} mark_now=${lighter_mark_price_now}")
        print(f"[snapshot] расстояние до ликвидации СЕЙЧАС (от текущей mark price): {dist_to_liq_pct_now}%")
        print(f"[snapshot] margin: collateral=${collateral_usd} available(=free_margin_usd)=${free_margin_usd} "
              f"свободно={free_margin_pct_now}%")
        print(f"[snapshot] margin_call_price=${margin_call_price} ({margin_call_move_pct_from_current:+.4f}% от текущей цены) "
              f"-- НЕ liquidation_price, порог обнуления initial-margin буфера" if margin_call_price else "")
    else:
        print("[snapshot] ETH-позиция на Lighter НЕ найдена -- голая LP-экспозиция, если это неожиданно, см. флаг ниже.")

    # === 4. Дельта: формула против реального размера хеджа ===
    print("\n=== 4. Дельта -- amount_ETH(P) по РЕАЛЬНОЙ ончейн-ликвидности против реального шорта ===")
    sqrt_p_raw = pool["sqrt_price_x96"] / (2 ** 96)
    sqrt_pa_raw = raw_sqrt_from_tick(pos["tick_lower"])
    sqrt_pb_raw = raw_sqrt_from_tick(pos["tick_upper"])
    if sqrt_pa_raw > sqrt_pb_raw:
        sqrt_pa_raw, sqrt_pb_raw = sqrt_pb_raw, sqrt_pa_raw
    sqrt_p_clamped = min(max(sqrt_p_raw, sqrt_pa_raw), sqrt_pb_raw)
    amount0_required_raw = max(pos["liquidity"] * (1 / sqrt_p_clamped - 1 / sqrt_pb_raw), 0.0)
    amount0_eth_required_now = amount0_required_raw / 10 ** WETH_DECIMALS
    # amount1 (USDG-нога) -- та же raw sqrt-математика, что amount0 выше
    # (p5_live_close.py::close_position()) -- нужна для рыночной стоимости
    # НАШЕЙ LP-позиции СЕЙЧАС (our_reserve_usd, см. fee_capture_ratio ниже).
    amount1_required_raw = max(pos["liquidity"] * (sqrt_p_clamped - sqrt_pa_raw), 0.0)
    amount1_usdg_now = amount1_required_raw / 10 ** USDG_DECIMALS
    our_reserve_usd_now = amount0_eth_required_now * pool_price_now + amount1_usdg_now

    delta_check: dict = {"amount_eth_required_now_formula": amount0_eth_required_now}
    if eth_pos:
        real_hedge_size_eth = lighter_hedge_now["position_size_eth"]
        net_delta_eth_now = amount0_eth_required_now - real_hedge_size_eth
        net_delta_usd_now = net_delta_eth_now * pool_price_now
        drift_pct_of_hedge = (net_delta_eth_now / real_hedge_size_eth * 100) if real_hedge_size_eth else None
        hours_since_open_for_drift = None
        if state.get("opened_at_utc"):
            opened_at = datetime.fromisoformat(state["opened_at_utc"].replace("Z", "+00:00"))
            hours_since_open_for_drift = (now_utc - opened_at).total_seconds() / 3600
        drift_pct_per_hour = (drift_pct_of_hedge / hours_since_open_for_drift) if (drift_pct_of_hedge is not None and hours_since_open_for_drift) else None
        delta_check.update({
            "real_hedge_size_eth": real_hedge_size_eth,
            "net_delta_eth_now": net_delta_eth_now, "net_delta_usd_now": net_delta_usd_now,
            "drift_pct_of_hedge_size": drift_pct_of_hedge,
            "hours_since_open": hours_since_open_for_drift,
            "drift_pct_of_hedge_size_per_hour": drift_pct_per_hour,
        })
        print(f"[snapshot] требуется по формуле сейчас: {amount0_eth_required_now:.8f} ETH, реальный шорт: {real_hedge_size_eth:.8f} ETH")
        print(f"[snapshot] рассинхрон: {net_delta_eth_now:+.8f} ETH (${net_delta_usd_now:+.4f}), "
              f"{drift_pct_of_hedge:+.4f}% от размера хеджа" + (f", {drift_pct_per_hour:+.4f}%/час" if drift_pct_per_hour is not None else ""))
    else:
        print("[snapshot] нет реальной Lighter-позиции для сравнения -- дельта-чек неполный.")

    # === 5. Экономика: часы, комиссии/час, годовые против kill-порога ===
    print("\n=== 5. Экономика на реальных данных ===")
    economics: dict = {}
    fees_usd_unclaimed = fees0_eth * pool_price_now + fees1_usdg
    economics["fees_earned_usd_unclaimed_now"] = fees_usd_unclaimed
    if state.get("opened_at_utc") and state.get("capital_at_risk_usd_entry"):
        opened_at = datetime.fromisoformat(state["opened_at_utc"].replace("Z", "+00:00"))
        hours_elapsed = (now_utc - opened_at).total_seconds() / 3600
        capital_at_risk = state["capital_at_risk_usd_entry"]
        gas_spent_so_far_usd = state.get("total_gas_spent_usd_est", 0.0)  # только вход -- выход ещё не происходил, позиция не закрыта
        net_usd_so_far = fees_usd_unclaimed - gas_spent_so_far_usd
        fees_per_hour_usd = fees_usd_unclaimed / hours_elapsed if hours_elapsed > 0 else None
        annualized_pct_fees_gross = (fees_per_hour_usd * 24 * 365 / capital_at_risk * 100) if fees_per_hour_usd is not None else None
        annualized_pct_net_of_gas = ((net_usd_so_far / hours_elapsed) * 24 * 365 / capital_at_risk * 100) if hours_elapsed > 0 else None
        kill_threshold_pct = state.get("kill_threshold_annual", 0.30) * 100
        economics.update({
            "hours_elapsed": hours_elapsed, "capital_at_risk_usd_entry": capital_at_risk,
            "gas_spent_so_far_usd": gas_spent_so_far_usd, "net_usd_so_far": net_usd_so_far,
            "fees_per_hour_usd": fees_per_hour_usd,
            "annualized_pct_fees_only_gross": annualized_pct_fees_gross,
            "annualized_pct_net_of_gas_spent_so_far": annualized_pct_net_of_gas,
            "kill_threshold_annual_pct": kill_threshold_pct,
            "passes_kill_threshold_gross": (annualized_pct_fees_gross >= kill_threshold_pct) if annualized_pct_fees_gross is not None else None,
            "passes_kill_threshold_net_of_gas": (annualized_pct_net_of_gas >= kill_threshold_pct) if annualized_pct_net_of_gas is not None else None,
            "CAUTION": ("Экстраполяция в годовые с выборки в считанные часы -- статистически ненадёжна "
                        "(один выброс/затишье комиссий даёт кратные искажения APR). Не решение по kill-порогу, "
                        "только первая точка ряда -- см. data/p5_fee_accrual.jsonl, нужно минимум 24 часовых точки."),
        })
        print(f"[snapshot] прожито {hours_elapsed:.4f}ч, несобранные комиссии=${fees_usd_unclaimed:.6f}, "
              f"${fees_per_hour_usd:.6f}/час" if fees_per_hour_usd else "")
        print(f"[snapshot] годовых (комиссии брутто)={annualized_pct_fees_gross:.4f}% "
              f"годовых (за вычетом уже потраченного газа входа ${gas_spent_so_far_usd})={annualized_pct_net_of_gas:.4f}% "
              f"против kill-порога {kill_threshold_pct}%")
    else:
        print("[snapshot] нет data/p5_live_position_state.json с opened_at_utc/capital_at_risk_usd_entry -- экономика не посчитана.")

    # === gas_spent_cumulative_usd -- ОТДЕЛЬНО от economics (владелец: "чтобы
    # газ не размазывался внутри доходности"). Пока = газ входа -- ребалансов
    # ещё не было (демон не написан, см. docs/PROJECT_STATE.md §8). ===
    gas_spent_cumulative_usd = state.get("total_gas_spent_usd_est", 0.0)

    # === fee_capture_ratio_interval (переименовано из fee_capture_ratio):
    # наш fee yield за интервал против пулового среднего за тот же интервал.
    # НАЙДЕНО (владелец, 2026-09-04): две реальные точки дали 0.57 и 0.109 --
    # пуассоновский шум короткого интервала, не сходится ни к чему. Оставлено
    # как диагностика, для решений НЕ используется (см. cumulative ниже). ===
    prev_entry = read_last_accrual_entry()
    gt_pool = fetch_gt_pool_snapshot_soft()
    fee_capture_ratio_interval = None
    fee_capture_detail: dict = {
        "CAUTION": "поинтервальное значение шумное (пуассоновское накопление на коротком окне) -- для решений не используется, оставлено как диагностика разброса.",
    }
    if prev_entry and prev_entry.get("token_id") == TOKEN_ID:
        prev_fees_usd = prev_entry["fees0_eth"] * prev_entry["pool_price_usd"] + prev_entry["fees1_usdg"]
        prev_ts = datetime.strptime(prev_entry["timestamp_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        interval_hours = (now_utc - prev_ts).total_seconds() / 3600
        interval_fees_usd = fees_usd_unclaimed - prev_fees_usd
        fee_capture_detail["interval_hours"] = interval_hours
        fee_capture_detail["interval_fees_usd"] = interval_fees_usd
        if interval_fees_usd < 0:
            fee_capture_detail["note"] = "отрицательный интервал комиссий -- вероятно collect() был вызван между снимками, база сброшена."
        elif gt_pool and gt_pool.get("volume_usd", {}).get("h1") and gt_pool.get("reserve_in_usd") and our_reserve_usd_now:
            pool_volume_h1_usd = float(gt_pool["volume_usd"]["h1"])
            pool_reserve_usd = float(gt_pool["reserve_in_usd"])
            pool_fees_interval_usd = pool_volume_h1_usd * POOL_FEE_FRACTION
            our_yield = interval_fees_usd / our_reserve_usd_now
            pool_yield = pool_fees_interval_usd / pool_reserve_usd if pool_reserve_usd else None
            fee_capture_ratio_interval = (our_yield / pool_yield) if pool_yield else None
            fee_capture_detail.update({
                "our_reserve_usd_now": our_reserve_usd_now, "our_yield_interval": our_yield,
                "pool_volume_h1_usd_proxy_for_interval": pool_volume_h1_usd,
                "pool_fees_interval_usd_est": pool_fees_interval_usd, "pool_reserve_usd": pool_reserve_usd,
                "pool_yield_interval": pool_yield,
                "NOTE": "pool_volume_h1 -- проxy пулового объёма 'за тот же интервал' (rolling last-hour из GT), "
                        "не точное совпадение с фактическим интервалом между снимками -- приближение, не точный факт.",
            })
        else:
            fee_capture_detail["note"] = "GT-снимок пула недоступен в этом прогоне ИЛИ our_reserve_usd_now=0 -- ratio не посчитан."
    else:
        fee_capture_detail["note"] = "нет предыдущей точки ряда (первый прогон или сменился token_id) -- интервал не определён."
    print(f"\n[snapshot] fee_capture_ratio_interval={fee_capture_ratio_interval} (шумно, диагностика) детали={fee_capture_detail}")

    # === fee_capture_ratio_cumulative (владелец, 2026-09-04, замена
    # интервального): числитель -- НАШИ комиссии С ОТКРЫТИЯ (callStatic
    # collect() уже накопительный) / текущая стоимость LP-ноги. Знаменатель
    # -- сумма РЕАЛЬНОГО почасового объёма пула (ohlcv/hour?currency=usd,
    # свечи с timestamp>=position_open_ts, НЕ volume_usd.h24 -- то
    # скользящее окно, соседние снятия перекрываются на 23ч) × 0.0001,
    # делённая на СРЕДНИЙ TVL пула по всем точкам jsonl (не последний
    # снимок). ===
    our_fees_usd_cum = fees_usd_unclaimed  # уже накопительно с открытия -- сам callStatic collect() так устроен
    our_reserve_usd = our_reserve_usd_now
    our_yield_cum = (our_fees_usd_cum / our_reserve_usd) if our_reserve_usd else None

    position_open_ts_unix = None
    hours_covered = None
    if state.get("opened_at_utc"):
        opened_at = datetime.fromisoformat(state["opened_at_utc"].replace("Z", "+00:00"))
        position_open_ts_unix = int(opened_at.timestamp())
        hours_covered = (now_utc - opened_at).total_seconds() / 3600

    pool_volume_usd_sum, n_hourly_candles, earliest_ts, latest_ts = (None, 0, None, None)
    if position_open_ts_unix is not None:
        pool_volume_usd_sum, n_hourly_candles, earliest_ts, latest_ts = fetch_hourly_volume_usd_since(position_open_ts_unix)

    pool_fees_usd_cum = (pool_volume_usd_sum * POOL_FEE_FRACTION) if pool_volume_usd_sum is not None else None

    # Средний TVL: реально сохранённые pool_reserve_in_usd из ПРОШЛЫХ точек
    # jsonl + текущий снимок (если GT доступен сейчас) -- НЕ придумываем
    # значение для точек, где GT не был снят в своё время (см. read_all_
    # accrual_pool_tvls()).
    pool_reserve_now = float(gt_pool["reserve_in_usd"]) if (gt_pool and gt_pool.get("reserve_in_usd")) else None
    tvl_samples = read_all_accrual_pool_tvls()
    if pool_reserve_now is not None:
        tvl_samples = tvl_samples + [pool_reserve_now]
    avg_pool_tvl_usd = (sum(tvl_samples) / len(tvl_samples)) if tvl_samples else None
    n_accrual_points = len(tvl_samples)

    pool_yield_cum = (pool_fees_usd_cum / avg_pool_tvl_usd) if (pool_fees_usd_cum is not None and avg_pool_tvl_usd) else None
    fee_capture_ratio_cumulative = (our_yield_cum / pool_yield_cum) if (our_yield_cum is not None and pool_yield_cum) else None

    fee_capture_cumulative_detail = {
        "our_fees_usd_cum": our_fees_usd_cum, "our_reserve_usd": our_reserve_usd, "our_yield_cum": our_yield_cum,
        "position_open_ts_unix": position_open_ts_unix, "hours_covered": hours_covered,
        "pool_volume_usd_sum_since_open": pool_volume_usd_sum, "n_hourly_candles": n_hourly_candles,
        "earliest_hourly_candle_ts": earliest_ts, "latest_hourly_candle_ts": latest_ts,
        "pool_fees_usd_cum": pool_fees_usd_cum,
        "avg_pool_tvl_usd": avg_pool_tvl_usd, "n_accrual_points": n_accrual_points,
        "pool_yield_cum": pool_yield_cum,
        "note": (None if n_hourly_candles else
                 "0 часовых свечей с timestamp>=position_open_ts (позиция открыта <1ч назад на момент первых точек) -- "
                 "pool_fees_usd_cum/avg_pool_tvl_usd/ratio честно null, не выдумываем частичную свечу."),
    }
    print(f"[snapshot] fee_capture_ratio_cumulative={fee_capture_ratio_cumulative} детали={fee_capture_cumulative_detail}")

    # === 6. LVR (loss-versus-rebalancing) -- второй порядок риска, хедж
    # его НЕ снимает (задача владельца, 2026-09-04). См. докстринг модуля
    # для полного вывода формул. ===
    print("\n=== 6. LVR -- реализованный убыток конструкции против теоретического (непрерывный ребаланс) ===")
    L_human = pos["liquidity"] / L_HUMAN_DIVISOR
    # Сверка: та же L_human должна воспроизводить amount0_eth_required_now
    # (уже посчитанный ВЫШЕ raw-sqrt-методом, §4) -- независимая проверка
    # тождества L_human=liquidity_raw/1e12 на реальных числах этого прогона,
    # не только в докстринге. Та же клипп-логика, что §4 (min/max в диапазон).
    p_now_clamped = min(max(pool_price_now, range_lower), range_upper)
    l_human_cross_check_amount0 = L_human * (1 / math.sqrt(p_now_clamped) - 1 / math.sqrt(range_upper))
    l_human_cross_check_diff = l_human_cross_check_amount0 - amount0_eth_required_now

    lvr_block: dict = {"L_human": L_human, "l_human_cross_check_diff_eth": l_human_cross_check_diff}
    if entry_price and eth_pos:
        V_lp_now_usd = our_reserve_usd_now
        V_lp_open_usd = lp_value_usd(L_human, pos["tick_lower"], pos["tick_upper"], entry_price)
        lp_pnl_ex_fees_usd = V_lp_now_usd - V_lp_open_usd
        hedge_pnl_ex_funding_usd = unrealized_pnl + (realized_pnl_usd or 0.0)
        hedge_pnl_incl_funding_usd = hedge_pnl_ex_funding_usd + (total_funding_paid_out_usd or 0.0)
        combined_pnl_ex_fees_usd = lp_pnl_ex_fees_usd + hedge_pnl_incl_funding_usd

        # Бенчмарк статического хеджа = гамма-член (LVR-квадратичное
        # приближение) + БАЗИСНЫЙ член. НАЙДЕНО численно (реальные данные
        # этого прогона): наивная формула −(L/(4P^1.5))(P_t−P_open)² даёт
        # отклонение −$0.119 при гамма-члене всего −$0.024 -- это НЕ баг в
        # L/тиках, а известный, уже задокументированный факт паспорта: LP
        # заминчена по ончейн-цене пула (`entry_price`=$2509.9047), а хедж
        # исполнен по СВОЕЙ цене на Lighter (`avg_entry_price`=$2506.43,
        # см. data/p5_live_position_state.json) -- секунды между двумя
        # исполнениями дают реальный базис. Хедж-PnL считается биржей ОТ
        # СВОЕЙ цены входа (не от entry_price LP), поэтому корректный
        # бенчмарк ДОЛЖЕН включать линейный член `size×(avg_entry_price−
        # entry_price)` -- без него сравнение сравнивает разные вещи.
        # С поправкой отклонение падает до ~$0.012 (в пределах ожидаемого
        # шума третьего порядка) -- проверено, не осталось необъяснённым.
        delta_p = pool_price_now - entry_price
        gamma_term_usd = -(L_human / (4 * entry_price ** 1.5)) * (delta_p ** 2)
        basis_term_usd = real_hedge_size_eth * (avg_entry_price - entry_price)
        static_hedge_benchmark_usd = gamma_term_usd + basis_term_usd
        static_hedge_deviation_usd = combined_pnl_ex_fees_usd - static_hedge_benchmark_usd

        # sigma реализованная по САМОМУ ряду (не 45.5% из отдельного 52-дневного
        # OHLCV-скана) -- добавляем ТЕКУЩУЮ точку к уже сохранённым, т.к. эта
        # точка ещё не записана в jsonl на момент этого вычисления.
        price_series = read_all_accrual_price_series() + [(now_utc, pool_price_now)]
        sigma_info = sigma_realized_annualized_from_series(price_series)
        sigma_realized = sigma_info.get("sigma_realized_annualized")

        delta_t_years = (hours_covered / (365.25 * 24)) if hours_covered else None
        continuous_lvr_theoretical_usd = (
            (sigma_realized ** 2) * L_human * math.sqrt(pool_price_now) / 4 * delta_t_years
        ) if (sigma_realized is not None and delta_t_years is not None) else None
        fee_lvr_ratio = (our_fees_usd_cum / continuous_lvr_theoretical_usd) if continuous_lvr_theoretical_usd else None

        lvr_block.update({
            "V_lp_open_usd": V_lp_open_usd, "V_lp_now_usd": V_lp_now_usd,
            "lp_pnl_ex_fees_usd": lp_pnl_ex_fees_usd,
            "hedge_unrealized_pnl_usd": unrealized_pnl, "hedge_realized_pnl_usd": realized_pnl_usd,
            "hedge_funding_paid_out_usd": total_funding_paid_out_usd,
            "hedge_pnl_incl_funding_usd": hedge_pnl_incl_funding_usd,
            "combined_pnl_ex_fees_usd": combined_pnl_ex_fees_usd,
            "static_hedge_benchmark_usd": static_hedge_benchmark_usd,
            "static_hedge_benchmark_gamma_term_usd": gamma_term_usd,
            "static_hedge_benchmark_basis_term_usd": basis_term_usd,
            "static_hedge_deviation_usd": static_hedge_deviation_usd,
            "sigma_realized_info": sigma_info,
            "delta_t_years": delta_t_years,
            "continuous_lvr_theoretical_usd": continuous_lvr_theoretical_usd,
            "fee_lvr_ratio": fee_lvr_ratio,
            "CAUTION": ("static_hedge_deviation_usd, если заметно ненулевой (не объясним фандингом/клиппингом), -- "
                        "сигнал бага в L/тиках/чтении, не просто шум. sigma_realized на малой выборке крайне шумная "
                        "-- continuous_lvr_theoretical_usd/fee_lvr_ratio ненадёжны до накопления точек."),
        })
        print(f"[snapshot] L_human={L_human:.6f} (сверка с §4: diff={l_human_cross_check_diff})")
        print(f"[snapshot] V_lp(P_open)=${V_lp_open_usd:.6f} V_lp(P_now)=${V_lp_now_usd:.6f} lp_pnl_ex_fees=${lp_pnl_ex_fees_usd:+.6f}")
        print(f"[snapshot] hedge_pnl (unrealized+realized+funding) = ${hedge_pnl_incl_funding_usd:+.6f}")
        print(f"[snapshot] combined_pnl_ex_fees=${combined_pnl_ex_fees_usd:+.6f} vs static_hedge_benchmark=${static_hedge_benchmark_usd:+.6f} "
              f"(отклонение ${static_hedge_deviation_usd:+.6f})")
        print(f"[snapshot] sigma_realized={sigma_realized} ({sigma_info.get('n_returns')} returns, {sigma_info.get('note')})")
        print(f"[snapshot] continuous_lvr_theoretical_usd=${continuous_lvr_theoretical_usd} fee_lvr_ratio={fee_lvr_ratio}")
    else:
        lvr_block["note"] = "нет entry_price или реальной Lighter-позиции -- LVR-блок неполный."

    # === 7. Сверка свободной маржи -- Δ раскладывается на РЕАЛЬНЫЕ поля
    # (unrealized_pnl, realized_pnl, funding, требование маржи), не на
    # формульную оценку (задача владельца, 2026-09-04). ===
    print("\n=== 7. Сверка свободной маржи (реальные поля, не формула) ===")
    margin_recon: dict = {}
    if eth_pos and prev_entry and prev_entry.get("token_id") == TOKEN_ID:
        prev_free_margin = prev_entry.get("free_margin_usd")
        prev_unrealized = prev_entry.get("hedge_unrealized_pnl_usd")
        prev_realized = prev_entry.get("hedge_realized_pnl_usd")
        prev_funding = prev_entry.get("hedge_funding_paid_out_usd")
        prev_cross_imr = prev_entry.get("cross_initial_margin_requirement_usd")
        if None not in (prev_free_margin, prev_unrealized, prev_realized, prev_funding, prev_cross_imr,
                        cross_initial_margin_requirement_usd):
            d_free_margin = free_margin_usd - prev_free_margin
            d_unrealized = unrealized_pnl - prev_unrealized
            d_realized = (realized_pnl_usd or 0.0) - prev_realized
            d_funding = (total_funding_paid_out_usd or 0.0) - prev_funding
            d_cross_imr = cross_initial_margin_requirement_usd - prev_cross_imr
            d_collateral_implied = d_free_margin + d_cross_imr  # тождество: free_margin = collateral - cross_imr
            explained = d_unrealized + d_realized + d_funding
            residual_usd = d_collateral_implied - explained
            margin_recon = {
                "d_free_margin_usd": d_free_margin, "d_cross_initial_margin_requirement_usd": d_cross_imr,
                "d_collateral_implied_usd": d_collateral_implied,
                "d_unrealized_pnl_usd": d_unrealized, "d_realized_pnl_usd": d_realized, "d_funding_usd": d_funding,
                "explained_usd": explained, "residual_usd": residual_usd,
                "note": None,
            }
            print(f"[snapshot] Δfree_margin=${d_free_margin:+.6f} = Δcollateral(${d_collateral_implied:+.6f}) − ΔIMR(${d_cross_imr:+.6f})")
            print(f"[snapshot] Δcollateral объяснено PnL+funding: ${explained:+.6f}, остаток=${residual_usd:+.6f}")
        else:
            margin_recon["note"] = ("нет полного набора полей на предыдущей точке (funding/realized_pnl/cross_imr "
                                     "начали читаться только с этого обновления кода) -- сверка недоступна для этой пары точек.")
            print(f"[snapshot] {margin_recon['note']}")
    else:
        margin_recon["note"] = "нет предыдущей точки или Lighter-позиции -- сверка маржи недоступна."
        print(f"[snapshot] {margin_recon['note']}")

    # === Запись почасового ряда (append-only) ===
    block_now = get_block_number()
    accrual_entry = {
        "timestamp_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "block": block_now, "token_id": TOKEN_ID,
        "fees0_eth": fees0_eth, "fees1_usdg": fees1_usdg,
        "pool_price_usd": pool_price_now, "in_range": in_range,
        "free_margin_usd": lighter_hedge_now.get("free_margin_usd"),
        "margin_call_price": lighter_hedge_now.get("margin_call_price_usd"),
        "fee_capture_ratio_interval": fee_capture_ratio_interval,
        "gas_spent_cumulative_usd": gas_spent_cumulative_usd,
        "pool_reserve_in_usd": pool_reserve_now,
        "our_fees_usd_cum": our_fees_usd_cum, "our_reserve_usd": our_reserve_usd,
        "pool_fees_usd_cum": pool_fees_usd_cum, "avg_pool_tvl_usd": avg_pool_tvl_usd,
        "hours_covered": hours_covered, "n_hourly_candles": n_hourly_candles,
        "n_accrual_points": n_accrual_points,
        "fee_capture_ratio_cumulative": fee_capture_ratio_cumulative,
        # -- поля для сверки маржи следующей точки (задача LVR, 2026-09-04) --
        "hedge_unrealized_pnl_usd": lighter_hedge_now.get("unrealized_pnl_usd"),
        "hedge_realized_pnl_usd": lighter_hedge_now.get("realized_pnl_usd"),
        "hedge_funding_paid_out_usd": lighter_hedge_now.get("total_funding_paid_out_usd"),
        "cross_initial_margin_requirement_usd": lighter_hedge_now.get("cross_initial_margin_requirement_usd"),
        # -- LVR-блок --
        "L_human": lvr_block.get("L_human"),
        "lp_pnl_ex_fees_usd": lvr_block.get("lp_pnl_ex_fees_usd"),
        "combined_pnl_ex_fees_usd": lvr_block.get("combined_pnl_ex_fees_usd"),
        "static_hedge_benchmark_usd": lvr_block.get("static_hedge_benchmark_usd"),
        "static_hedge_deviation_usd": lvr_block.get("static_hedge_deviation_usd"),
        "sigma_realized_annualized": (lvr_block.get("sigma_realized_info") or {}).get("sigma_realized_annualized"),
        "continuous_lvr_theoretical_usd": lvr_block.get("continuous_lvr_theoretical_usd"),
        "fee_lvr_ratio": lvr_block.get("fee_lvr_ratio"),
        "margin_recon_residual_usd": margin_recon.get("residual_usd"),
    }
    ACCRUAL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ACCRUAL_LOG_PATH.open("a") as f:
        f.write(json.dumps(accrual_entry, default=str, ensure_ascii=False) + "\n")
    print(f"\n[snapshot] добавлена строка в {ACCRUAL_LOG_PATH}: {accrual_entry}")

    result = {
        "generated_at_utc": now_utc.isoformat(), "token_id": TOKEN_ID, "block": block_now,
        "nfpm_positions_raw": pos,
        "uncollected_fees_callStatic": {"fees0_eth": fees0_eth, "fees1_usdg": fees1_usdg, "fees_usd": fees_usd_unclaimed},
        "price_vs_range": {
            "pool_price_now_usd": pool_price_now, "range_lower_usd": range_lower, "range_upper_usd": range_upper,
            "current_tick": current_tick, "in_range": in_range, "position_pct_in_range": pos_pct_in_range,
            "pct_drop_to_lower_edge": pct_drop_to_lower, "pct_rise_to_upper_edge": pct_rise_to_upper,
            "nearest_edge": nearest_edge, "move_from_entry_pct": move_from_entry_pct,
            "pct_of_10pct_path_to_nearest_edge": pct_of_10pct_path_to_nearest_edge,
        },
        "lighter_hedge_now": lighter_hedge_now,
        "lighter_market_meta": lighter_market_meta,
        "delta_check": {**delta_check, "our_reserve_usd_now": our_reserve_usd_now, "amount1_usdg_now": amount1_usdg_now},
        "economics": economics,
        "gas_spent_cumulative_usd": gas_spent_cumulative_usd,
        "fee_capture_ratio_interval": fee_capture_ratio_interval, "fee_capture_interval_detail": fee_capture_detail,
        "fee_capture_ratio_cumulative": fee_capture_ratio_cumulative,
        "fee_capture_cumulative_detail": fee_capture_cumulative_detail,
        "lvr": lvr_block,
        "margin_reconciliation": margin_recon,
        "runtime_s": time.time() - t0,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[snapshot] записано {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
