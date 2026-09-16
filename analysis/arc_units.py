#!/usr/bin/env python3
"""ЕДИНОЕ место конвертации raw -> человеческие единицы на Arc. Раньше
множители (1e18/1e6) были разбросаны по нескольким скриптам и в одном
месте (ручная запись data/task_arc_speed_probe_result.json) была
допущена ошибка -- делили на 1e15 вместо 1e18, завышение ровно в 1000
раз. Больше не хардкодить множители по месту -- только через это.

decimals подтверждены реальными eth_call (не из документации):
- 0x3600...0000 -- ERC20 USDC, decimals()=6 (task_arc_currency0_identity_result.json)
- 0xfff...ffe -- нативная USDC-обёртка, 18 decimals (соответствие
  установлено через совпадение АГРЕГАТНЫХ балансов PoolManager --
  eth_getBalance/1e18 == balanceOf(0x3600)/1e6, task_arc_usdc_addressing_check_result.json)."""
from __future__ import annotations

USDC_ERC20 = "0x3600000000000000000000000000000000000000"
USDC_NATIVE_WRAP = "0xfffffffffffffffffffffffffffffffffffffffe"

TOKEN_DECIMALS = {
    USDC_ERC20: 6,
    USDC_NATIVE_WRAP: 18,
}


def to_human(token: str, raw: int) -> float | None:
    dec = TOKEN_DECIMALS.get(token.lower())
    if dec is None:
        return None
    return raw / (10 ** dec)


def merge_mirrored_transfers(transfers: list[dict], eps: float = 1e-9) -> list[dict]:
    """Нативный (0xfff...ffe) и ERC20 (0x3600...) Transfer-лог ОДНОГО И
    ТОГО ЖЕ перевода на Arc эмитятся ПАРОЙ с совпадающими from/to и
    совпадающей величиной после нормировки в человеческие единицы
    (реально проверено на tx 0xcc9a463b...: 700103133000000000000/1e18
    == 700103133/1e6 == 700.103133, совпадение не случайное -- решение
    протокола отражать нативное движение зеркальным ERC20-Transfer).
    Считать одним переводом: оставляем ERC20-запись (decimals
    подтверждён прямым eth_call, канонический адрес), выбрасываем
    нативный дубликат. Немирроренные нативные переводы (без парного
    ERC20-лога с тем же from/to/суммой) остаются как есть -- это
    реальные отдельные движения (газ, донат, тип и т.п.)."""
    used = [False] * len(transfers)
    out = []
    for i, t in enumerate(transfers):
        if used[i]:
            continue
        if t["token"].lower() != USDC_NATIVE_WRAP.lower():
            out.append(t)
            continue
        native_amt = to_human(t["token"], t["value"])
        matched = False
        for j, t2 in enumerate(transfers):
            if i == j or used[j]:
                continue
            if t2["token"].lower() != USDC_ERC20.lower():
                continue
            if t2["from"].lower() != t["from"].lower() or t2["to"].lower() != t["to"].lower():
                continue
            erc20_amt = to_human(t2["token"], t2["value"])
            if erc20_amt is not None and native_amt is not None and abs(erc20_amt - native_amt) <= eps * max(1.0, abs(erc20_amt)):
                used[i] = True  # выбрасываем нативный дубликат, ERC20-запись добавится в своей итерации
                matched = True
                break
        if not matched:
            out.append(t)  # немирроренный нативный перевод -- реальный, оставляем
    return out
