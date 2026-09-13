#!/usr/bin/env python3
"""Задача 5, десятый раунд (пункт 3, продолжение по прямому указанию
владельца): отбор ПЕРВОГО (по блоку) из 120 кандидатов (уже сохранённых,
знак уже исправлен -- см. предыдущий коммит), который:
  - внутри окна пилота (это гарантировано самим фактом принадлежности к
    сохранённым 601 -- скан УЖЕ был ограничен окном);
  - 2-3 последовательных V4-свопа через НАШ PoolManager (все плечи
    декодированы, ни одно не пропущено из-за отсутствия Initialize);
  - начинается и заканчивается в USDG или native ETH;
  - НЕ требует V3, WETH<->ETH обёртки/разворота или иных неподдерживаемых
    действий;
  - плечи РЕАЛЬНО выстраиваются в цепочку (выход плеча N == вход плеча
    N+1, замыкание на стартовый токен) -- не только "сумма по токену
    нулевая", а действительно последовательный маршрут, пригодный для
    executeCycle.

ПРАВКА ЗНАКА (та же, что уже закоммичена): decoded amount0/amount1 из
V4 Swap-события -- это УЖЕ дельта САМОГО трейдера (положительное =
трейдер получил, отрицательное = трейдер заплатил), НЕ дельта пула.
Это меняет и вывод zero_for_one для реконструкции маршрута: раньше
`_build_route_from_fund_flow_legs()` использовала `zero_for_one =
amount0 > 0` (комментарий "пул получил currency0" -- то есть трейдер
ЗАПЛАТИЛ currency0) -- это было ПРАВИЛЬНО РОВНО ПРИ старом (неверном)
допущении "amount0 = дельта пула". При правильном (трейдера) знаке
"трейдер заплатил currency0" -- это `amount0 < 0`, НЕ `> 0`. Исправлено
здесь; будет исправлено и в реальном реплее (round10_reconstruct.py).

ТОЛЬКО чтение уже сохранённого JSON -- ни одного нового RPC-вызова,
никакого форка, никакого нового скана."""
import json
from pathlib import Path

RESULT_FILE = Path(__file__).parent.parent / "data" / "task5_v4_item3_in_window_control_trade_result.json"

USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
NATIVE = "0x0000000000000000000000000000000000000000"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"


def _leg_io(leg: dict) -> tuple[bool, str, str]:
    """ИСПРАВЛЕНО: amount0 -- уже дельта ТРЕЙДЕРА (не пула). zero_for_one
    (платим currency0) <=> amount0 ОТРИЦАТЕЛЬНО."""
    zero_for_one = leg["amount0"] < 0
    input_currency = leg["currency0"] if zero_for_one else leg["currency1"]
    output_currency = leg["currency1"] if zero_for_one else leg["currency0"]
    return zero_for_one, input_currency, output_currency


def _verify_chain(legs: list[dict], base_token: str) -> tuple[bool, str]:
    if not legs:
        return False, "нет плеч"
    _, in0, _ = _leg_io(legs[0])
    if in0 != base_token:
        return False, f"плечо 0 начинается с {in0}, ожидали базовый {base_token}"
    prev_out = _leg_io(legs[0])[2]
    for leg in legs[1:]:
        _, li, lo = _leg_io(leg)
        if li != prev_out:
            return False, f"разрыв цепочки: ожидали вход {prev_out}, получили {li}"
        prev_out = lo
    if prev_out != base_token:
        return False, f"маршрут не замыкается на {base_token} (последний выход {prev_out})"
    return True, "ok"


def evaluate_candidate(row: dict) -> dict:
    legs = row.get("legs") or []
    net: dict[str, int] = {}
    incomplete = False
    for leg in legs:
        if leg.get("currency0") is None:
            incomplete = True
            continue
        net[leg["currency0"]] = net.get(leg["currency0"], 0) + leg["amount0"]
        net[leg["currency1"]] = net.get(leg["currency1"], 0) + leg["amount1"]
    nz = {t: v for t, v in net.items() if v != 0}

    reasons = []
    if incomplete:
        reasons.append("несостоявшееся плечо (Initialize не найден -- возможно не-V4/непокрытый пул)")
    n_legs = len(legs)
    if n_legs not in (2, 3):
        reasons.append(f"число плеч={n_legs} (нужно 2-3)")
    weth_present = any(leg.get("currency0") == WETH or leg.get("currency1") == WETH for leg in legs)
    if weth_present:
        reasons.append("WETH участвует в маршруте (обёртка/разворот не поддерживается)")
    if len(nz) != 1:
        reasons.append(f"ненулевых токенов после исправления знака={len(nz)} (не 1)")

    base_token = None
    chain_ok = False
    if len(nz) == 1:
        base_token, base_val = next(iter(nz.items()))
        if base_token not in (USDG, NATIVE):
            reasons.append(f"базовый токен {base_token} не USDG/native ETH")
        if base_val <= 0:
            reasons.append("базовый остаток не положителен после исправления знака")
        if not incomplete and n_legs in (2, 3) and not weth_present:
            chain_ok, chain_reason = _verify_chain(legs, base_token)
            if not chain_ok:
                reasons.append(f"маршрут не выстраивается в цепочку: {chain_reason}")

    return {
        "tx_hash": row["tx_hash"], "block": row["block"],
        "n_legs": n_legs, "base_token": base_token,
        "corrected_nonzero": nz,
        "exclusion_reasons": reasons,
        "qualifies": len(reasons) == 0,
    }


def main() -> None:
    d = json.loads(RESULT_FILE.read_text())
    ffc = d.get("fund_flow_checks") or []
    single = [row for row in ffc if len(row.get("nonzero_net_flow_tokens") or {}) == 1]

    evaluated = [evaluate_candidate(row) for row in single]
    evaluated.sort(key=lambda e: e["block"])

    qualifying = [e for e in evaluated if e["qualifies"]]

    # Гистограмма причин исключения (по каждому кандидату может быть
    # НЕСКОЛЬКО причин -- считаем каждую отдельно, чтобы видеть полную
    # картину, а не только первую сработавшую).
    reason_histogram: dict[str, int] = {}
    for e in evaluated:
        for r in e["exclusion_reasons"]:
            # группируем по префиксу до конкретных чисел/адресов
            key = r.split(":")[0].split("=")[0].strip()
            reason_histogram[key] = reason_histogram.get(key, 0) + 1

    result = {
        "n_single_token_120": len(evaluated),
        "n_qualifying": len(qualifying),
        "first_qualifying": qualifying[0] if qualifying else None,
        "exclusion_reason_histogram": reason_histogram,
        "all_evaluated": evaluated,
    }
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
