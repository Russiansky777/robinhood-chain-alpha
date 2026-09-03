#!/usr/bin/env python3
"""P5 LIVE -- экстренное выравнивание остаточной позиции на Lighter
(владелец, 2026-09-03, дух того же правила: "голая экспозиция не
должна оставаться открытой").

НАЙДЕНО (реальный run 33784994487): create_market_order на шорт
0.036503 ETH ЧАСТИЧНО исполнился -- реально открылась позиция
sign=-1, position=0.0100 ETH (не 0, не полный размер) -- ниже 85%
порога _verify_hedge_filled(), LP закрыта автозакрытием, но САМА
позиция на Lighter осталась открытой (автозакрытие трогает только
ончейн-LP, не Lighter) -- реальная короткая экспозиция ~0.01 ETH без
какого-либо LP на другой стороне.

Читает РЕАЛЬНУЮ текущую позицию по ETH (market_id=0), если она
ненулевая -- отправляет reduce_only рыночный ордер в обратную сторону
на ТОЧНЫЙ текущий размер (не на исходно запрошенный), чтобы закрыть
именно то, что реально открыто. Проверяет результат тем же
поллингом, что и _verify_hedge_filled -- если после ордера позиция
всё ещё не нулевая (возможен ещё один частичный филл), делает ещё
один проход с ОСТАВШИМСЯ размером, до FLATTEN_MAX_ROUNDS попыток.

Ордеров без нужды не шлёт -- если позиция уже 0, ничего не делает.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import p5_live_precheck as pc  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/p5_live_flatten_lighter_result.json")
LIGHTER_API_KEY_INDEX = 4
FLATTEN_MAX_ROUNDS = 3
SLIPPAGE = 0.02  # шире обычного -- приоритет гарантированного закрытия


def eth_position() -> dict | None:
    positions = pc.lighter_positions()
    return next((p for p in positions if str(p.get("symbol", "")).upper() == "ETH"), None)


async def _close_round(size_eth: float, is_ask: bool, mark_price: float, size_decimals: int, price_decimals: int) -> dict:
    import lighter
    lighter_priv = os.environ["LIGHTER_API_KEY_PRIVATE"]
    client = lighter.SignerClient(url=pc.LIGHTER_API_BASE, account_index=pc.LIGHTER_ACCOUNT_INDEX,
                                   api_private_keys={LIGHTER_API_KEY_INDEX: lighter_priv})
    try:
        base_amount = round(size_eth * 10 ** size_decimals)
        # НАЙДЕНО (владелец, 2026-09-03): собственный расчёт от mark_price
        # -- та же проблема, что в p5_live_step1.py -- реальный референс
        # у SDK берётся из ЖИВОГО best_bid/best_ask (get_best_price()),
        # не mark. Переходим на сам SDK-хелпер (ideal_price=None).
        client_order_index = int(time.time() * 1000) % (2 ** 31)
        tx, resp, err = await client.create_market_order_limited_slippage(
            market_index=0, client_order_index=client_order_index, base_amount=base_amount,
            max_slippage=SLIPPAGE, is_ask=is_ask, reduce_only=True,
            api_key_index=LIGHTER_API_KEY_INDEX,
        )
        # НАЙДЕНО (проверка исходника signer_client.py, 2026-09-03):
        # create_order/create_market_order_limited_slippage возвращают
        # (tx_object, RespSendTx, error) -- второе значение НЕ строка-хэш,
        # это pydantic-модель с полями code/message/tx_hash/... Прежний
        # `"tx_hash": str(resp)` писал ВЕСЬ repr модели в это поле (реально
        # найдено в data/p3_guard_cache/p5_live_step1_result.json --
        # "tx_hash": "code=200 message=... tx_hash='...' ..."). Достаём
        # реальный хэш и код явно.
        return {
            "tx_hash": resp.tx_hash if resp is not None else None,
            "resp_code": resp.code if resp is not None else None,
            "resp_message": resp.message if resp is not None else None,
            "err": str(err) if err is not None else None,
            "base_amount": base_amount, "is_ask": is_ask, "max_slippage": SLIPPAGE,
        }
    finally:
        await client.close()


def flatten(max_rounds: int = FLATTEN_MAX_ROUNDS) -> dict:
    """Приводит ETH-позицию на Lighter к нулю (reduce_only рыночными
    ордерами на РЕАЛЬНЫЙ текущий размер, до max_rounds попыток). Ордеров
    без нужды не шлёт -- если позиция уже 0, ничего не делает. Возвращает
    result-словарь с ключом "flattened": bool. Импортируется как
    ПОСТОЯННАЯ предпосылка перед любым новым хеджем (владелец,
    2026-09-03, п.4: "Сделать это постоянной предпосылкой хеджа, не
    разовой проверкой") -- не только из CLI run() ниже."""
    t0 = time.time()
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rounds": []}

    pos = eth_position()
    result["initial_position"] = pos
    if pos is None or abs(float(pos.get("position", 0))) < 1e-6:
        result["note"] = "Позиция уже плоская (0) -- ничего не отправляю."
        result["flattened"] = True
        result["runtime_s"] = time.time() - t0
        print(f"[flatten] {result['note']}")
        return result

    eth_market = pc.lighter_eth_perp()
    mark_price = float(eth_market["mark_price"])
    size_decimals = eth_market["size_decimals"]
    price_decimals = eth_market["price_decimals"]

    for rnd in range(1, max_rounds + 1):
        pos = eth_position()
        if pos is None or abs(float(pos.get("position", 0))) < 1e-6:
            result["final_position"] = None
            result["flattened"] = True
            break
        size_eth = abs(float(pos["position"]))
        is_short = int(pos.get("sign", -1)) < 0 or float(pos["position"]) < 0
        is_ask = not is_short  # закрываем шорт покупкой (is_ask=False); закрываем лонг продажей (is_ask=True)
        print(f"[flatten] раунд {rnd}: текущая позиция={pos.get('position')} sign={pos.get('sign')} "
              f"-> отправляю reduce_only {'SELL' if is_ask else 'BUY'} {size_eth} ETH")
        order = asyncio.run(_close_round(size_eth, is_ask, mark_price, size_decimals, price_decimals))
        result["rounds"].append({"round": rnd, "position_before": pos, "order": order})
        if order.get("err") is not None:
            result["abort_reason"] = f"раунд {rnd}: ордер вернул ошибку: {order['err']}"
            print(f"[flatten] {result['abort_reason']}")
            break
        time.sleep(4)  # дать ордеру попасть в книгу/матчинг перед повторным чтением
    else:
        result["flattened"] = False

    final_pos = eth_position()
    result["final_position_check"] = final_pos
    result["flattened"] = final_pos is None or abs(float(final_pos.get("position", 0))) < 1e-6
    result["runtime_s"] = time.time() - t0
    print(f"\n[flatten] ИТОГ: flattened={result['flattened']} final_position={final_pos}")
    return result


def run() -> int:
    result = flatten()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[flatten] записано {OUT_PATH}")
    return 0 if result["flattened"] else 1


if __name__ == "__main__":
    raise SystemExit(run())
