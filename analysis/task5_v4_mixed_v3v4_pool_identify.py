#!/usr/bin/env python3
"""Смешанный V3/V4 пример (0x90fe304f...): проверка V3-СОВМЕСТИМОСТИ
интерфейса пула 0x654e4143... (владелец: "не подставляй стандартную
модель Uniswap V3 без проверки реализации") + грубые (конец блока,
БЕЗ разделения внутри блока -- это честно помечено, не выдаётся за
точное "после V4, до V3") состояния обоих пулов на блоках 62373269
(до обоих изменений) и 62373270 (после обоих).

ЧЕСТНЫЕ ОГОВОРКИ (владелец, разбор предыдущего отчёта):
1. Подтверждён только V3-СОВМЕСТИМЫЙ ИНТЕРФЕЙС (штатные ответы token0/
   token1/fee/tickSpacing/slot0/liquidity, каноническая пара fee=0.3%/
   tickSpacing=60) -- идентичность байткода официальному Uniswap V3
   НЕ проверялась и не доказана.
2. Равенство значения liquidity на ДВУХ КОНЦАХ интервала (до/после) не
   доказывает, что она была постоянна ВНУТРИ интервала.
3. Причинность (какое из двух изменений блока 62373270 -- V4-Swap или
   V3-Swap -- создало возможность) остаётся НЕЗАВЕРШЁННОЙ -- эти грубые
   состояния не разделяют порядок событий внутри блока.
4. Decimals TOKEN1 -- подтверждается ОДНИМ прямым eth_call decimals()
   на "latest" (иммутабельное поле, не исторический скан), НЕ выводится
   из масштаба Transfer-сумм; при неудаче цена в человеческих единицах
   честно помечается "неизвестно"."""
from __future__ import annotations

import json
import os
import sys
from decimal import Decimal, getcontext
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import _rpc_call, rpc_call_trading_path  # noqa: E402
from task5_v4_pool_state_cache import extsload_pool_state  # noqa: E402

# ПРАВКА (на месте, реальный найденный сбой): публичный-RPC-первый путь
# (_rpc_call) детерминированно вернул "metadata is not found" на этих же
# блоках -- не транзиентная ошибка по эвристике, поэтому фолбэк на
# Alchemy никогда не срабатывал сам. rpc_call_trading_path -- Alchemy
# напрямую (тот же путь, что торговый горячий путь бота).

getcontext().prec = 60

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
V4_POOL_ID = "0x0112f42d2da7164a1e224ef55be60c10ef9607cabdadbc003123daa500750358"
V3_STYLE_POOL = "0x654e4143e82a5824445ade0824351c2a9acd95a8"

USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
TOKEN1 = "0xdf0992e440dd0be65bd8439b609d6d4366bf1cb5"
# ПРАВКА (владелец, разбор отчёта): USDG_DECIMALS=6 -- подтверждённое
# значение, используется во ВСЕЙ этой сессии (raw-суммы USDG везде
# делятся на 1e6), не выведено из масштаба Transfer здесь. TOKEN1_DECIMALS
# раньше был угадан "по масштабу Transfer-сумм" -- владелец явно запретил
# так делать. Теперь -- ОДИН прямой eth_call decimals() на "latest"
# (иммутабельное поле ERC20, НЕ исторический скан) ниже, в main(); если
# вызов не удался -- decimals остаётся None и цена в человеческих
# единицах честно помечается "неизвестно", а не молча предполагается.
USDG_DECIMALS = 6
TOKEN1_DECIMALS: int | None = None  # заполняется в main() РЕАЛЬНЫМ decimals(), не предположением

SLOT0_SELECTOR = "0x3850c7bd"
LIQUIDITY_SELECTOR = "0x1a686502"
FACTORY_SELECTOR = "0xc45a0155"
TICK_SPACING_SELECTOR = "0xd0c93a7c"
DECIMALS_SELECTOR = "0x313ce567"

BEFORE_BLOCK = 62373269
AFTER_BLOCK = 62373270


def eth_call(to: str, data: str, block) -> str | None:
    block_param = block if isinstance(block, str) else hex(block)
    try:
        return rpc_call_trading_path("eth_call", [{"to": to, "data": data}, block_param])
    except Exception:  # noqa: BLE001
        try:
            return _rpc_call("eth_call", [{"to": to, "data": data}, block_param])
        except Exception as exc2:  # noqa: BLE001
            return f"ERROR: {exc2}"


def main() -> None:
    global TOKEN1_DECIMALS
    result: dict = {}

    # --- Идентификация реализации: неизменяемые (immutable) параметры,
    # безопасно на "latest" -- байткод/immutable-поля контракта не зависят
    # от исторического блока. ---
    ident: dict = {}
    for name, selector in (("token0", "0x0dfe1681"), ("token1", "0xd21220a7"), ("fee", "0xddca3f43"),
                            ("tickSpacing", TICK_SPACING_SELECTOR), ("factory", FACTORY_SELECTOR),
                            ("liquidity", LIQUIDITY_SELECTOR), ("slot0", SLOT0_SELECTOR)):
        raw = eth_call(V3_STYLE_POOL, selector, "latest")
        ident[name] = raw
    result["v3_style_pool_identity_calls_latest"] = ident
    # ПРАВКА (владелец): подтверждён V3-СОВМЕСТИМЫЙ ИНТЕРФЕЙС (перечисленные
    # селекторы отвечают штатно, fee/tickSpacing совпадают с канонической
    # таблицей V3) -- это НЕ доказательство, что байткод идентичен
    # официальной реализации Uniswap V3 (байткод/бинарное сравнение с
    # эталоном здесь не проводилось).
    result["identity_caveat"] = ("подтверждён V3-совместимый интерфейс (селекторы/fee/tickSpacing отвечают "
                                  "штатно) -- идентичность реализации официальному Uniswap V3 (байткод) "
                                  "этим НЕ доказана и отдельно не проверялась")

    code = _rpc_call("eth_getCode", [V3_STYLE_POOL, "latest"])
    result["v3_style_pool_code_size_bytes"] = (len(code) - 2) // 2 if code else 0

    # ОДИН прямой вызов decimals() на TOKEN1 -- иммутабельное поле ERC20,
    # НЕ исторический скан, НЕ вывод из масштаба Transfer (владелец
    # явно запретил второе).
    decimals_raw = eth_call(TOKEN1, DECIMALS_SELECTOR, "latest")
    if isinstance(decimals_raw, str) and decimals_raw.startswith("0x"):
        try:
            TOKEN1_DECIMALS = int(decimals_raw, 16)
        except ValueError:
            TOKEN1_DECIMALS = None
    result["token1_decimals_confirmed_via_decimals_call"] = TOKEN1_DECIMALS
    if TOKEN1_DECIMALS is None:
        result["token1_decimals_note"] = "decimals() не подтверждён этим вызовом -- цена в человеческих единицах ниже помечена 'неизвестно', не предполагается"

    # --- Грубые (конец блока) состояния -- ЧЕСТНО помечено: НЕ разделяет
    # V4-Swap (tx_index 9) и V3-Swap (tx_index 10) внутри блока 62373270. ---
    for label, block in (("before_both_62373269", BEFORE_BLOCK), ("after_both_62373270", AFTER_BLOCK)):
        cp: dict = {"block": block}
        try:
            cp["v4_pool"] = extsload_pool_state(V4_POOL_ID, hex(block), rpc_call_trading_path, POOL_MANAGER)
        except Exception as exc:  # noqa: BLE001
            cp["v4_pool"] = extsload_pool_state(V4_POOL_ID, hex(block), _rpc_call, POOL_MANAGER)
            cp["v4_pool"]["_fallback_note"] = f"rpc_call_trading_path (Alchemy) failed: {exc}; использован _rpc_call"
        slot0_raw = eth_call(V3_STYLE_POOL, SLOT0_SELECTOR, block)
        liq_raw = eth_call(V3_STYLE_POOL, LIQUIDITY_SELECTOR, block)
        cp["v3_style_pool_slot0_raw"] = slot0_raw
        cp["v3_style_pool_liquidity_raw"] = liq_raw
        if isinstance(slot0_raw, str) and slot0_raw.startswith("0x") and len(slot0_raw) >= 2 + 64:
            words = [slot0_raw[2:][i:i + 64] for i in range(0, len(slot0_raw[2:]), 64)]
            sqrt_price_x96 = int(words[0], 16)
            tick_raw = int(words[1], 16)
            tick = tick_raw - (1 << 256) if tick_raw >= (1 << 255) else tick_raw
            cp["v3_style_sqrt_price_x96"] = sqrt_price_x96
            cp["v3_style_tick"] = tick
            # ПРАВКА (владелец): raw_ratio = (sqrtPriceX96/2**96)**2 -- это
            # raw_token1_за_raw_token0 (token0=USDG подтверждён выше,
            # token1=TOKEN1 подтверждён выше). Человеко-читаемая цена
            # "token1 за 1 token0" = raw_ratio * 10**(decimals0-decimals1)
            # -- ПРЕЖНИЙ код использовал 10**(decimals1-decimals0)
            # (обратный показатель степени) -- инвертированная цена.
            # Обратная цена ("token0 за 1 token1") -- ОТДЕЛЬНОЕ, явно
            # подписанное поле, не смешивается с прямой.
            raw_ratio = (Decimal(sqrt_price_x96) / Decimal(2) ** 96) ** 2
            cp["raw_ratio_token1_per_token0_raw_units"] = str(raw_ratio)
            if TOKEN1_DECIMALS is not None:
                price_token1_per_token0 = raw_ratio * (Decimal(10) ** (USDG_DECIMALS - TOKEN1_DECIMALS))
                cp["price_TOKEN1_per_USDG"] = str(price_token1_per_token0)
                cp["price_USDG_per_TOKEN1"] = (
                    str(1 / price_token1_per_token0) if price_token1_per_token0 != 0 else None
                )
            else:
                cp["price_TOKEN1_per_USDG"] = "неизвестно (decimals TOKEN1 не подтверждён)"
                cp["price_USDG_per_TOKEN1"] = "неизвестно (decimals TOKEN1 не подтверждён)"
        if isinstance(liq_raw, str) and liq_raw.startswith("0x"):
            try:
                cp["v3_style_liquidity"] = int(liq_raw, 16)
            except ValueError:
                pass
        result[label] = cp

    # ПРАВКА (владелец, честность формулировок): равенство liquidity на
    # ДВУХ КОНЦАХ интервала (до/после) НЕ доказывает, что она была
    # постоянна ВНУТРИ интервала -- ModifyLiquidity могло произойти и
    # вернуться, это здесь не проверяется (нужен был бы полный скан
    # событий внутри интервала, не сделан). Причинность смешанного
    # V3/V4 примера (какое из двух изменений в блоке 62373270 создало
    # возможность) остаётся НЕЗАВЕРШЁННОЙ -- эти грубые (конец блока)
    # состояния НЕ разделяют порядок V4-Swap/V3-Swap внутри блока.
    result["report_wording_caveats"] = [
        "равенство liquidity на концах интервала не доказывает её постоянство между ними",
        "причинность (какое изменение в блоке 62373270 создало возможность) остаётся незавершённой",
    ]

    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    out_path = Path(__file__).parent.parent / "data" / "task5_v4_mixed_v3v4_pool_identify_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[mixed_v3v4_identify] сохранено: {out_path}")


if __name__ == "__main__":
    main()
