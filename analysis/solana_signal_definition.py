#!/usr/bin/env python3
"""Владелец, 2026-09-19: КАНОНИЧЕСКОЕ определение сигнала (Task 2) --
заменяет classify() везде, где считаются сигналы (а не воспроизводится
контрольное число 43).

Сигнал = покупка, при которой:
  - у кошелька-источника НЕ было этого минта ДО транзакции (pre-баланс
    токена = 0, post > 0) -- проверяется по preTokenBalances/
    postTokenBalances САМОЙ транзакции, не по внешнему состоянию;
  - кошелёк -- подписант в ЛЮБОЙ позиции массива подписантов (accountKeys
    с флагом signer=true на нужном pubkey), не обязательно первый
    (плательщик комиссии может быть отдельным спонсором -- см. находку
    про AgmLJBM.../Fomo в этой сессии);
  - трата >= порога в SOL-эквиваленте (единственный параметр).

БЕЗ MIN_SPEND=500 USDC и БЕЗ условия "ровно один положительный минт" --
это специфичные ограничения classify() (solana_buyer200_select_extend.py),
который остаётся ТОЛЬКО для контрольного воспроизведения известного
числа 43 на историческом окне, но больше не является определением
сигнала для новых подсчётов."""
from __future__ import annotations

from decimal import Decimal as D

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"


def is_signer_anywhere(tx: dict, wallet: str) -> bool:
    keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    return any(isinstance(k, dict) and k.get("signer") and k.get("pubkey") == wallet for k in keys)


def wallet_token_pre_post(tx: dict, wallet: str) -> dict:
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return {}
    pre_tb, post_tb = meta.get("preTokenBalances") or [], meta.get("postTokenBalances") or []

    def bals(rows):
        a: dict[str, D] = {}
        for b in rows:
            if b.get("owner") == wallet:
                amt = b["uiTokenAmount"]
                a[b["mint"]] = a.get(b["mint"], D(0)) + D(amt["amount"]) / D(10) ** amt["decimals"]
        return a

    pre, post = bals(pre_tb), bals(post_tb)
    return {"pre": pre, "post": post}


def wallet_sol_delta(tx: dict, wallet: str) -> float | None:
    keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    idx = None
    for i, k in enumerate(keys):
        pk = k.get("pubkey") if isinstance(k, dict) else k
        if pk == wallet:
            idx = i
            break
    if idx is None:
        return None
    meta = tx.get("meta") or {}
    pre_list, post_list = meta.get("preBalances") or [], meta.get("postBalances") or []
    if idx >= len(pre_list) or idx >= len(post_list):
        return None
    return (post_list[idx] - pre_list[idx]) / 1e9


def find_canonical_signals(tx: dict, wallet: str, threshold_sol: float,
                            sol_usd_price_fn) -> list[dict]:
    """Возвращает список сигналов (обычно 0 или 1, редко больше при
    батч-покупках нескольких новых минтов в одной tx) для этой
    транзакции. sol_usd_price_fn(block_time) -> float|None -- функция
    конвертации USDC->SOL по реальному историческому курсу (внешняя,
    как и везде в этой сессии, через GeckoTerminal)."""
    if not is_signer_anywhere(tx, wallet):
        return []
    bals = wallet_token_pre_post(tx, wallet)
    if not bals:
        return []
    pre, post = bals["pre"], bals["post"]
    new_mints = [m for m in post if post[m] > 0 and pre.get(m, D(0)) == 0 and m not in (USDC_MINT, SOL_MINT)]
    if not new_mints:
        return []

    meta = tx.get("meta") or {}
    usdc_delta = None
    for b in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or []):
        if b.get("mint") == USDC_MINT and b.get("owner") == wallet:
            usdc_delta = True
            break
    usdc_pre = pre.get(USDC_MINT, D(0))
    usdc_post = post.get(USDC_MINT, D(0))
    usdc_spent = float(usdc_pre - usdc_post) if (usdc_pre or usdc_post) else 0.0

    sol_delta = wallet_sol_delta(tx, wallet) or 0.0
    wsol_pre, wsol_post = pre.get(SOL_MINT, D(0)), post.get(SOL_MINT, D(0))
    wsol_spent = float(wsol_pre - wsol_post) if (wsol_pre or wsol_post) else 0.0

    bt = tx.get("blockTime")
    sol_equivalent = None
    if usdc_spent > 0:
        sol_usd = sol_usd_price_fn(bt) if bt else None
        sol_equivalent = usdc_spent / sol_usd if sol_usd else None
    elif sol_delta < 0:
        sol_equivalent = -sol_delta
    elif wsol_spent > 0:
        sol_equivalent = wsol_spent

    if sol_equivalent is None or sol_equivalent < threshold_sol:
        return []

    return [{"mint": m, "sol_equivalent": sol_equivalent, "usdc_spent": usdc_spent or None,
              "block_time": bt} for m in new_mints]
