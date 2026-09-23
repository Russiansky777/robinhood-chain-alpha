#!/usr/bin/env python3
"""Пересчёт наценки от ЦЕНЫ ПУЛА, а не от средней цены исполнения лидера.

Замечание владельца, принятое полностью: точка отсчёта была неверная.
Для пула с x*y=k средняя цена исполнения ровно посередине между ценой до
и ценой после: avg = sqrt(до * после). Значит цена пула сразу после
сделки лидера примерно в (1+p) раз выше его средней цены, и прежние
"наценка к лидеру" и "(в) мы" включали вторую половину следа самого
лидера, то есть были завышены.

Как считаем теперь -- всё по РЕЗЕРВАМ ПУЛА, взятым из балансов хранилищ
в самих транзакциях (preTokenBalances / postTokenBalances):

    (а) лидер:  цена пула ДО его сделки      -> цена пула ПОСЛЕ его сделки
    (б) дрейф:  цена пула после лидера       -> цена пула ПЕРЕД нашей сделкой
    (в) мы:     цена пула перед нами         -> наша СРЕДНЯЯ цена исполнения

Средние цены исполнения лидера и наши остаются отдельными колонками для
справки -- они честные, просто не годятся как граница между вкладами.

Оценка резерва R ~ x/p больше не нужна и не используется: резерв читается
прямо. Если хранилища пула в транзакции не опознаются -- так и пишем, с
причиной по конкретной сделке, без подстановки.

Только чтение цепочки.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from solana_crowd_scan import PUBLIC_RPC, Rpc, helius_key, now_utc, scrub  # noqa: E402

CACHE_PATH = REPO_ROOT / "data" / "solana_pilot_block_autopsy_cache.json"
OUT_PATH = REPO_ROOT / "data" / "solana_pool_price_recompute.json"

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTES = (WSOL, USDC, USDT)
A_SIZE = 7

# Метки DEX-программ берутся из репозитория (107 штук), а не из головы.
# Незнакомая программа честно печатается как "другое (<id>)" -- ничего не
# приписывается молча.
DEX_LABELS_PATH = (REPO_ROOT / "data" / "solana_buyer_200" / "prior" / "current"
                    / "buyer_100" / "dex_labels.json")
DEX_LABELS: dict = (json.loads(DEX_LABELS_PATH.read_text())
                    if DEX_LABELS_PATH.exists() else {})
PUMP_CURVE = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def program_label(pid: str | None) -> str:
    if not pid:
        return "не определена"
    return DEX_LABELS.get(pid) or f"другое ({pid})"


def account_keys(tx: dict) -> list[str]:
    txn = (tx or {}).get("transaction") or {}
    raw = txn.get("accountKeys") or ((txn.get("message") or {}).get("accountKeys")) or []
    return [k.get("pubkey") if isinstance(k, dict) else k for k in raw]


def program_touching(tx: dict, accounts: set[str]) -> str | None:
    """Программа инструкции, которая трогает ИМЕННО эти счета хранилищ.

    Просто "какая DEX-программа есть в транзакции" не годится: на маршруте
    их несколько, и пул приписался бы не той."""
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    groups = [msg.get("instructions") or []]
    for g in ((tx or {}).get("meta") or {}).get("innerInstructions") or []:
        groups.append(g.get("instructions") or [])
    best = None
    for instrs in groups:
        for ins in instrs:
            accs = set(ins.get("accounts") or [])
            if not accs:
                pa = (ins.get("parsed") or {}).get("info") or {}
                accs = {v for v in pa.values() if isinstance(v, str)}
            hit = len(accs & accounts)
            if hit and (best is None or hit > best[0]):
                best = (hit, ins.get("programId"))
    return best[1] if best else None


def _bal_map(entries) -> dict:
    """{(owner, mint): (uiAmount, accountIndex)} из pre/postTokenBalances."""
    out = {}
    for b in entries or []:
        o, m = b.get("owner"), b.get("mint")
        if not o or not m:
            continue
        amt = ((b.get("uiTokenAmount") or {}).get("uiAmount"))
        out[(o, m)] = (float(amt or 0.0), b.get("accountIndex"))
    return out


def native_balance(tx: dict, pubkey: str) -> tuple[float | None, float | None]:
    """Нативный SOL счёта до и после -- по preBalances/postBalances."""
    meta = (tx or {}).get("meta") or {}
    txn = (tx or {}).get("transaction") or {}
    keys_raw = txn.get("accountKeys") or ((txn.get("message") or {}).get("accountKeys")) or []
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in keys_raw]
    if pubkey not in keys:
        return None, None
    i = keys.index(pubkey)
    pb, po = meta.get("preBalances") or [], meta.get("postBalances") or []
    if i >= len(pb) or i >= len(po):
        return None, None
    return pb[i] / 1e9, po[i] / 1e9


def pool_reserves(tx: dict, mint: str, trader: str) -> dict:
    """Резервы пула ДО и ПОСЛЕ сделки -- по балансам в самой транзакции.

    ПУЛ ОПОЗНАЁТСЯ ПО АДРЕСАМ ХРАНИЛИЩ, а не по их владельцу. У Raydium
    AMM v4 и CPMM владелец хранилищ -- одно общее PDA-полномочие на всю
    программу, поэтому по владельцу РАЗНЫЕ пулы склеились бы в один, и
    сравнение "тот же пул или другой" давало бы ложное "тот же".
    Для кривой pump.fun пул -- это сам счёт кривой.

    Два вида котировки, и оба нужны:
      1) токеном (WSOL/USDC/USDT) -- есть отдельный котировочный счёт;
      2) нативным SOL -- у кривых pump.fun SOL лежит прямо на счёте
         кривой, и в preTokenBalances котировки нет вовсе. На первом
         прогоне 11 сделок из 15 отвалились именно поэтому.
    """
    meta = (tx or {}).get("meta") or {}
    pre, post = _bal_map(meta.get("preTokenBalances")), _bal_map(meta.get("postTokenBalances"))
    keys = account_keys(tx)

    def addr(idx):
        return keys[idx] if isinstance(idx, int) and 0 <= idx < len(keys) else None

    # Дельта токена у трейдера: настоящий пул обязан отдать примерно
    # столько же, сколько трейдер получил. Без этой привязки в кандидаты
    # лезут любые счета, где просто лежит минт.
    tr_d = (post.get((trader, mint), (0.0, None))[0]
            - pre.get((trader, mint), (0.0, None))[0]) if trader else 0.0

    def годится(t0, t1, q0, q1) -> bool:
        """Пул -- это когда РЕЗЕРВЫ ДВИГАЮТСЯ, и навстречу друг другу.

        Первая версия принимала кандидата, у которого котировка не менялась
        вовсе (1274.643358201 -> 1274.643358201 при падении токена на 12 млн):
        это не пул, а посторонний счёт с минтом, и он давал влияние лидера
        -67% на ровном месте.
        """
        dt, dq = t1 - t0, q1 - q0
        if dt == 0 or dq == 0:
            return False
        if (dt > 0) == (dq > 0):
            return False                      # обе ноги в одну сторону -- не обмен
        if tr_d and (dt > 0) == (tr_d > 0):
            return False                      # пул должен двигаться ПРОТИВ трейдера
        if tr_d and abs(dt) < abs(tr_d) * 0.5:
            return False                      # отдал заметно меньше, чем трейдер получил
        return True

    owners_mint = {o for (o, m) in set(pre) | set(post) if m == mint and o != trader}
    best = None
    for o in owners_mint:
        t_pre, t_post = pre.get((o, mint)), post.get((o, mint))
        t0 = (t_pre or (0.0, None))[0]
        t1 = (t_post or (0.0, None))[0]
        if t0 <= 0 or t1 <= 0:
            continue
        token_vault = addr((t_post or t_pre)[1])
        for q in QUOTES:
            q_pre, q_post = pre.get((o, q)), post.get((o, q))
            q0 = (q_pre or (0.0, None))[0]
            q1 = (q_post or (0.0, None))[0]
            if q0 <= 0 or q1 <= 0 or not годится(t0, t1, q0, q1):
                continue
            quote_vault = addr((q_post or q_pre)[1])
            cand = {"хранилище_токена": token_vault, "хранилище_котировки": quote_vault,
                    "владелец_хранилищ": o, "котировка": q, "вид_котировки": "токен",
                    "резерв_токена_до": t0, "резерв_токена_после": t1,
                    "резерв_котировки_до": q0, "резерв_котировки_после": q1,
                    "дельта_токена": t1 - t0}
            if best is None or abs(cand["дельта_токена"]) > abs(best["дельта_токена"]):
                best = cand
        n0, n1 = native_balance(tx, o)
        # Нативная ветка опаснее токеновой: рента есть на любом счёте, и
        # без проверки движения сюда попадал бы любой счёт из транзакции.
        if n0 and n1 and n0 > 0 and n1 > 0 and годится(t0, t1, n0, n1):
            cand = {"хранилище_токена": token_vault, "хранилище_котировки": o,
                    "владелец_хранилищ": o, "котировка": "нативный SOL",
                    "вид_котировки": "нативный",
                    "резерв_токена_до": t0, "резерв_токена_после": t1,
                    "резерв_котировки_до": n0, "резерв_котировки_после": n1,
                    "дельта_токена": t1 - t0}
            if best is None or abs(cand["дельта_токена"]) > abs(best["дельта_токена"]):
                best = cand
    if best is None:
        return {"ок": False, "почему": "хранилища пула не опознаны: ни у одного владельца "
                                        "минта резервы не двигаются навстречу друг другу "
                                        "и против трейдера"}
    # Ключ пула -- ПАРА адресов хранилищ. Именно он различает пулы одной
    # программы, у которых владелец общий.
    best["ключ_пула"] = "|".join(sorted(x for x in (best["хранилище_токена"],
                                                     best["хранилище_котировки"]) if x))
    vaults = {x for x in (best["хранилище_токена"], best["хранилище_котировки"],
                          best["владелец_хранилищ"]) if x}
    pid = program_touching(tx, vaults)
    best["программа_id"] = pid
    best["программа"] = program_label(pid)
    best["кривая_pump_fun"] = (pid == PUMP_CURVE)
    best["цена_до"] = best["резерв_котировки_до"] / best["резерв_токена_до"]
    best["цена_после"] = best["резерв_котировки_после"] / best["резерв_токена_после"]
    best["ок"] = True
    return best


def exec_price(tx: dict, mint: str, trader: str) -> float | None:
    """Средняя цена исполнения трейдера: его котировочная нога / его токен."""
    meta = (tx or {}).get("meta") or {}
    pre, post = _bal_map(meta.get("preTokenBalances")), _bal_map(meta.get("postTokenBalances"))
    dt = post.get((trader, mint), (0.0, None))[0] - pre.get((trader, mint), (0.0, None))[0]
    if dt == 0:
        return None
    dq = 0.0
    for q in QUOTES:
        dq += post.get((trader, q), (0.0, None))[0] - pre.get((trader, q), (0.0, None))[0]
    if dq == 0:
        # Нога могла пройти нативным SOL -- берём дельту баланса подписанта.
        keys_raw = ((tx.get("transaction") or {}).get("accountKeys")
                    or ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or [])
        keys = [k.get("pubkey") if isinstance(k, dict) else k for k in keys_raw]
        if trader in keys:
            i = keys.index(trader)
            pb, po = meta.get("preBalances") or [], meta.get("postBalances") or []
            if i < len(pb) and i < len(po):
                dq = (po[i] - pb[i]) / 1e9
                if i == 0:
                    dq += (meta.get("fee") or 0) / 1e9
    return abs(dq) / abs(dt) if dq else None


MAX_GAP_S = 30.0
SLOT_MS = 250


def our_pool_price_at(rpc: Rpc, mint: str, pool: str, from_slot: int, to_slot: int,
                       t_leader: int | None) -> dict:
    """Цена НАШЕГО пула в слоте лидера -- по ближайшей сделке в нём.

    Берётся первая транзакция начиная со слота лидера, которая трогает
    именно этот пул: её резервы ДО -- и есть состояние пула на тот момент.
    Дальше 30 секунд не уходим: иначе это уже не "в слоте лидера", а
    "когда-нибудь потом", и цифра означала бы не то, чем выглядит.
    """
    for slot in range(from_slot, to_slot + 1):
        try:
            blk = rpc.call("getBlock", [slot, {
                "encoding": "jsonParsed", "transactionDetails": "accounts",
                "maxSupportedTransactionVersion": 1, "rewards": False}])
        except RuntimeError as exc:
            if any(c in str(exc) for c in ("-32004", "-32007", "-32009")):
                continue          # слот пропущен лидером -- это не ошибка
            return {"ок": False, "почему": f"getBlock {slot}: {scrub(str(exc))[:120]}"}
        if not blk:
            continue
        bt = blk.get("blockTime")
        if t_leader is not None and bt is not None and bt - t_leader > MAX_GAP_S:
            return {"ок": False, "почему": f"ближайшая сделка в нашем пуле дальше "
                                            f"{MAX_GAP_S:.0f}с от слота лидера"}
        for t in blk.get("transactions") or []:
            r = pool_reserves(t, mint, "")
            if r.get("ок") and r.get("ключ_пула") == pool:
                return {"ок": True, "цена": r["цена_до"], "слот": slot, "block_time": bt,
                        "резерв_котировки": r["резерв_котировки_до"]}
    return {"ок": False, "почему": "в просмотренных слотах не нашлось ни одной сделки "
                                    "в нашем пуле"}


def recompute_one(rpc: Rpc, row: dict, leader: str, wallet: str) -> dict:
    out = {"mint": row.get("mint"), "когда": row.get("наша_покупка_utc"),
           "лидер_sol": row.get("лидер_sol"), "наш_вход_sol": row.get("наш_вход_sol"),
           "итог_gross_pct": row.get("итог_gross_pct"),
           "средняя_цена_лидера_справочно": row.get("лидер_цена"),
           "средняя_цена_наша_справочно": row.get("наша_цена"),
           "прежняя_наценка_к_средней_лидера_pct": row.get("наша_наценка_к_лидеру_pct")}
    sig_lead, sig_ours = row.get("лидер_signature"), row.get("buy_signature")
    if not sig_lead or not sig_ours:
        out["не_удалось"] = "нет подписи лидера или нашей"
        return out
    opts = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}
    try:
        txs = rpc.transactions([sig_lead, sig_ours])
    except RuntimeError as exc:
        out["не_удалось"] = scrub(str(exc))[:160]
        return out
    tl, to = txs.get(sig_lead), txs.get(sig_ours)
    if not tl:
        out["не_удалось"] = "транзакция лидера не отдалась узлом"
        return out
    if not to:
        out["не_удалось"] = "наша транзакция не отдалась узлом"
        return out

    pl = pool_reserves(tl, row["mint"], leader)
    po = pool_reserves(to, row["mint"], wallet)
    out["пул_в_сделке_лидера"] = pl
    out["пул_в_нашей_сделке"] = po
    if not pl.get("ок"):
        out["не_удалось"] = "в сделке лидера: " + pl["почему"]
        return out
    if not po.get("ок"):
        out["не_удалось"] = "в нашей сделке: " + po["почему"]
        return out

    out["пул_лидера"] = {"хранилища": pl["ключ_пула"], "котировка": pl["котировка"],
                          "программа": pl["программа"], "владелец_хранилищ": pl["владелец_хранилищ"]}
    out["наш_пул"] = {"хранилища": po["ключ_пула"], "котировка": po["котировка"],
                       "программа": po["программа"], "владелец_хранилищ": po["владелец_хранилищ"]}
    same_pool = pl["ключ_пула"] == po["ключ_пула"]
    out["разные_пулы"] = not same_pool
    out["владелец_хранилищ_совпал"] = (pl["владелец_хранилищ"] == po["владелец_хранилищ"])
    if not same_pool:
        # Что именно за случай -- от этого зависит вывод.
        if pl.get("кривая_pump_fun") and not po.get("кривая_pump_fun"):
            out["случай_разных_пулов"] = (
                f"лидер купил на кривой pump.fun, мы -- уже в AMM "
                f"({po['программа']}): это переезд токена с кривой, а не две площадки")
        elif po.get("кривая_pump_fun") and not pl.get("кривая_pump_fun"):
            out["случай_разных_пулов"] = (
                f"лидер в AMM ({pl['программа']}), мы -- на кривой pump.fun: "
                "порядок обратный переезду, требует отдельного взгляда")
        else:
            out["случай_разных_пулов"] = (
                f"один токен на двух площадках: лидер в {pl['программа']}, "
                f"мы в {po['программа']}")

    p_before = pl["цена_до"]
    p_after_leader = pl["цена_после"]
    p_before_us = po["цена_до"]
    if not same_pool:
        # Лидер двигал ОДИН пул, а мы покупали в ДРУГОМ. Считать (б) от
        # цены чужого пула нельзя: это разные рынки, связанные только
        # арбитражём. Точка отсчёта для (б) -- состояние НАШЕГО пула на
        # момент сделки лидера.
        s_lead = row.get("слот_лидера_S")
        s_ours = row.get("наш_слот")
        t_lead = None
        if s_lead is None or s_ours is None:
            out["не_удалось"] = "разные пулы, но нет слотов лидера/нашего -- точку отсчёта не взять"
            return out
        at = our_pool_price_at(rpc, row["mint"], po["ключ_пула"], s_lead, s_ours, t_lead)
        out["наш_пул_в_слоте_лидера"] = at
        if not at.get("ок"):
            out["не_удалось"] = "разные пулы: " + at["почему"]
            return out
        p_after_leader = at["цена"]
    our_exec = exec_price(to, row["mint"], wallet)
    lead_exec = exec_price(tl, row["mint"], leader)
    out["наша_средняя_цена_исполнения"] = our_exec
    out["средняя_цена_исполнения_лидера"] = lead_exec
    out["резерв_котировки_до_лидера"] = pl["резерв_котировки_до"]
    out["резерв_котировки_после_лидера"] = pl["резерв_котировки_после"]
    out["покупка_лидера_к_резерву"] = (round(row["лидер_sol"] / pl["резерв_котировки_до"], 4)
                                        if row.get("лидер_sol") and pl["резерв_котировки_до"] else None)
    out["наш_вход_к_резерву_перед_нами"] = (round(row["наш_вход_sol"] / po["резерв_котировки_до"], 4)
                                             if row.get("наш_вход_sol") and po["резерв_котировки_до"] else None)

    a = p_after_leader / p_before - 1
    b = p_before_us / p_after_leader - 1
    c = (our_exec / p_before_us - 1) if our_exec else None
    out["а_влияние_лидера_pct"] = round(a * 100, 3)
    out["б_дрейф_до_нас_pct"] = round(b * 100, 3)
    out["в_наше_влияние_pct"] = round(c * 100, 3) if c is not None else None
    if c is not None:
        итог = our_exec / p_before - 1
        out["итог_от_цены_пула_до_лидера_pct"] = round(итог * 100, 3)
        out["произведение_частей_pct"] = round(((1 + a) * (1 + b) * (1 + c) - 1) * 100, 3)
        out["сходимость_пп"] = round(((1 + a) * (1 + b) * (1 + c) - 1 - итог) * 100, 9)
        out["простая_сумма_pct"] = round((a + b + c) * 100, 3)
        out["перекрёстные_члены_пп"] = round(((a + b + c) - итог) * 100, 3)
        # Наценка к ЦЕНЕ ПУЛА после лидера -- то, что раньше считалось от
        # его средней цены и было завышено на вторую половину его следа.
        out["наценка_к_цене_пула_после_лидера_pct"] = round(((1 + b) * (1 + c) - 1) * 100, 3)
        if out.get("разные_пулы"):
            # У разных пулов (б) -- это не "дрейф за лидером", а то,
            # насколько ЧУЖОЕ движение перетащили в наш пул арбитражёры.
            out["влияние_на_наш_пул_pct"] = out["б_дрейф_до_нас_pct"]
        old = row.get("наша_наценка_к_лидеру_pct")
        if old is not None:
            out["насколько_прежняя_наценка_была_завышена_пп"] = round(
                old - out["наценка_к_цене_пула_после_лидера_pct"], 3)
    return out


def _med(vals):
    v = [x for x in vals if x is not None]
    return round(statistics.median(v), 3) if v else None


def summarise(label: str, items: list[dict]) -> dict:
    good = [x for x in items if not x.get("не_удалось")]
    return {
        "выборка": label, "сделок": len(items), "пересчитано": len(good),
        "медиана_а_влияние_лидера_pct": _med([x.get("а_влияние_лидера_pct") for x in good]),
        "медиана_б_дрейф_pct": _med([x.get("б_дрейф_до_нас_pct") for x in good]),
        "медиана_в_наше_влияние_pct": _med([x.get("в_наше_влияние_pct") for x in good]),
        "медиана_наценки_к_пулу_pct": _med(
            [x.get("наценка_к_цене_пула_после_лидера_pct") for x in good]),
        "медиана_прежней_наценки_pct": _med(
            [x.get("прежняя_наценка_к_средней_лидера_pct") for x in good]),
        "медиана_завышения_прежней_наценки_пп": _med(
            [x.get("насколько_прежняя_наценка_была_завышена_пп") for x in good]),
        "медиана_резерва_котировки_до_лидера": _med(
            [x.get("резерв_котировки_до_лидера") for x in good]),
        "медиана_лидер_к_резерву": _med([x.get("покупка_лидера_к_резерву") for x in good]),
        "медиана_наш_вход_к_резерву": _med([x.get("наш_вход_к_резерву_перед_нами") for x in good]),
        "медиана_лидер_sol": _med([x.get("лидер_sol") for x in good]),
        "медиана_итог_gross_pct": _med([x.get("итог_gross_pct") for x in items]),
        "сделок_с_разными_пулами": sum(1 for x in good if x.get("разные_пулы")),
        "сделок_в_одном_пуле": sum(1 for x in good if x.get("разные_пулы") is False),
        "медиана_влияния_на_наш_пул_pct": _med(
            [x.get("влияние_на_наш_пул_pct") for x in good if x.get("разные_пулы")]),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--leader", default="Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit")
    ap.add_argument("--wallet", default="E1qAJBmrJDhBvm2sV8kfMXFAmgzHuSRNRosPEgMkKiWS")
    ap.add_argument("--a-size", type=int, default=A_SIZE)
    ap.add_argument("--min-interval-s", type=float, default=0.05)
    ap.add_argument("--time-budget-s", type=int, default=30 * 60)
    ap.add_argument("--public-only", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return

    if not CACHE_PATH.exists():
        raise SystemExit(f"нет {CACHE_PATH} -- пересчитывать нечего")
    cache = json.loads(CACHE_PATH.read_text())
    rows = list((cache.get("строки") or {}).values())

    key, key_name = helius_key()
    rpc = Rpc(key, min_interval_s=args.min_interval_s, workers=2, service="разбор_пилота")
    if args.public_only:
        rpc.url = PUBLIC_RPC
        rpc.allow_public = False
    rpc.deadline = time.monotonic() + args.time_budget_s
    print(f"[пересчёт] сделок {len(rows)}, ключ из {key_name}, "
          f"{'только публичный' if args.public_only else 'Helius основной'}", flush=True)

    items = []
    for i, r in enumerate(rows, 1):
        items.append(recompute_one(rpc, r, args.leader, args.wallet))
        print(f"  {i}/{len(rows)} {r.get('mint','')[:10]} "
              f"{'ok' if not items[-1].get('не_удалось') else items[-1]['не_удалось'][:60]}",
              flush=True)

    out = {
        "generated_at_utc": now_utc(),
        "источник": "data/solana_pilot_block_autopsy_cache.json",
        "ЧЕСТНЫЕ_ОГОВОРКИ": [
            "Только чтение цепочки.",
            "Точка отсчёта -- ЦЕНА ПУЛА, а не средняя цена исполнения лидера. Для x*y=k "
            "средняя цена ровно посередине (avg = sqrt(до*после)), поэтому прежняя "
            "наценка включала вторую половину следа самого лидера.",
            "Резервы пула читаются ПРЯМО из preTokenBalances/postTokenBalances хранилищ "
            "в самих транзакциях. Оценка R ~ x/p больше не используется.",
            "Хранилища опознаются как счета владельца, который держит и минт, и котировку; "
            "при маршруте через несколько пулов берётся тот, где дельта минта наибольшая.",
            "Части перемножаются точно: (1+а)(1+б)(1+в) = наша средняя цена / цена пула до "
            "лидера. Простая сумма расходится на перекрёстные члены -- это арифметика "
            "процентов, показано колонкой.",
        ],
        "A": items[:args.a_size], "Б": items[args.a_size:],
        "сводка_A": summarise("A -- серия минусов", items[:args.a_size]),
        "сводка_Б": summarise("Б -- плюсовые 19-21.09", items[args.a_size:]),
        "rpc": {"calls": rpc.calls, "retries": rpc.retries, "кредитов": rpc.credits,
                 "оговорка_по_кредитам": "запросы внутри пачек getTransaction теперь считаются; "
                                          "повторная попытка пачки считается заново, как её и "
                                          "тарифицирует провайдер"},
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps({"сводка_A": out["сводка_A"], "сводка_Б": out["сводка_Б"]},
                      ensure_ascii=False, indent=2))


def self_test() -> None:
    checks = []

    def chk(name, ok, got=""):
        checks.append((name, bool(ok), got))

    def TX(pre, post, keys=None, pre_b=None, post_b=None, fee=0):
        return {"meta": {"preTokenBalances": pre, "postTokenBalances": post,
                          "preBalances": pre_b, "postBalances": post_b, "fee": fee},
                "transaction": {"accountKeys": keys or []}}

    def B(owner, mint, amt, idx=0):
        return {"owner": owner, "mint": mint, "uiTokenAmount": {"uiAmount": amt},
                "accountIndex": idx}

    M = "MINT"
    # Пул: 1000 SOL и 1 000 000 токенов; лидер покупает на 100 SOL.
    pre = [B("POOL", M, 1_000_000), B("POOL", WSOL, 1000),
           B("LEADER", M, 0), B("LEADER", WSOL, 100)]
    post = [B("POOL", M, 909_090.909), B("POOL", WSOL, 1100),
            B("LEADER", M, 90_909.091), B("LEADER", WSOL, 0)]
    r = pool_reserves(TX(pre, post, keys=[{"pubkey": "POOL"}]), M, "LEADER")
    chk("хранилища пула опознаны", r["ок"] and r["владелец_хранилищ"] == "POOL", str(r)[:140])
    chk("цена пула до", abs(r["цена_до"] - 0.001) < 1e-9, str(r["цена_до"]))
    chk("цена пула после выросла", r["цена_после"] > r["цена_до"])
    chk("цена после = резерв/резерв", abs(r["цена_после"] - 1100 / 909_090.909) < 1e-12)

    ep = exec_price(TX(pre, post, keys=[{"pubkey": "POOL"}]), M, "LEADER")
    chk("средняя цена исполнения = нога/токен", abs(ep - 100 / 90_909.091) < 1e-12, str(ep))
    chk("средняя НИЖЕ цены пула после -- это и есть смещение точки отсчёта",
        ep < r["цена_после"], f"{ep} vs {r['цена_после']}")
    # Для x*y=k средняя ровно посередине: avg^2 = до*после
    chk("средняя = sqrt(до*после) с точностью до округления",
        abs(ep * ep - r["цена_до"] * r["цена_после"]) < 1e-12,
        str(ep * ep - r["цена_до"] * r["цена_после"]))

    chk("трейдер не путается с пулом",
        pool_reserves(TX(pre, post, keys=[{"pubkey": "POOL"}]), M, "POOL").get("ок") is not True)

    # Главное из правки владельца: у Raydium владелец хранилищ ОБЩИЙ на всю
    # программу, поэтому разные пулы обязаны различаться по адресам хранилищ.
    AUTH = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j"
    def RAY(vault_t, vault_q, t0, t1, q0, q1):
        keys = [{"pubkey": "TRADER"}, {"pubkey": vault_t}, {"pubkey": vault_q}]
        return TX([B(AUTH, M, t0, 1), B(AUTH, WSOL, q0, 2), B("TRADER", M, 0, 0)],
                   [B(AUTH, M, t1, 1), B(AUTH, WSOL, q1, 2), B("TRADER", M, t0 - t1, 0)],
                   keys=keys)
    r1 = pool_reserves(RAY("VAULT_T1", "VAULT_Q1", 1000, 900, 10, 11), M, "TRADER")
    r2 = pool_reserves(RAY("VAULT_T2", "VAULT_Q2", 2000, 1900, 20, 21), M, "TRADER")
    chk("владелец хранилищ у обоих пулов один -- как у Raydium",
        r1["владелец_хранилищ"] == r2["владелец_хранилищ"] == AUTH)
    chk("но ключи пулов РАЗНЫЕ -- склейки не будет",
        r1["ключ_пула"] != r2["ключ_пула"], f"{r1['ключ_пула']} vs {r2['ключ_пула']}")
    chk("ключ собран из адресов хранилищ",
        r1["ключ_пула"] == "|".join(sorted(["VAULT_T1", "VAULT_Q1"])), r1["ключ_пула"])
    # Главный отсев: котировка не двигалась -- это НЕ пул.
    frozen = TX([B("FAKE", M, 85_923_742.68, 1), B("TRADER", M, 0, 0)],
                 [B("FAKE", M, 73_673_515.47, 1), B("TRADER", M, 12_250_227.2, 0)],
                 keys=[{"pubkey": "TRADER"}, {"pubkey": "FAKE"}],
                 pre_b=[1_000_000_000, 1_274_643_358_201],
                 post_b=[1_000_000_000, 1_274_643_358_201])
    chk("счёт с неподвижной котировкой пулом не считается",
        pool_reserves(frozen, M, "TRADER").get("ок") is not True,
        str(pool_reserves(frozen, M, "TRADER"))[:140])
    # И встречный случай: резервы двигаются навстречу -- это пул.
    moving = TX([B("REAL", M, 1000, 1), B("REAL", WSOL, 10, 2), B("TRADER", M, 0, 0)],
                 [B("REAL", M, 900, 1), B("REAL", WSOL, 11, 2), B("TRADER", M, 100, 0)],
                 keys=[{"pubkey": "TRADER"}, {"pubkey": "V_T"}, {"pubkey": "V_Q"}])
    chk("пул с встречным движением резервов принимается",
        pool_reserves(moving, M, "TRADER").get("ок") is True)
    # Пул отдал сильно меньше, чем трейдер получил -- посторонний счёт.
    tiny = TX([B("REAL", M, 1000, 1), B("REAL", WSOL, 10, 2), B("TRADER", M, 0, 0)],
               [B("REAL", M, 999, 1), B("REAL", WSOL, 11, 2), B("TRADER", M, 100, 0)],
               keys=[{"pubkey": "TRADER"}, {"pubkey": "V_T"}, {"pubkey": "V_Q"}])
    chk("отдал меньше половины полученного трейдером -- не пул",
        pool_reserves(tiny, M, "TRADER").get("ок") is not True)

    chk("незнакомая программа не приписывается молча",
        program_label("НЕИЗВЕСТНАЯ").startswith("другое ("), program_label("НЕИЗВЕСТНАЯ"))
    chk("известная программа берётся из файла меток репозитория",
        program_label("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8") == "Raydium",
        program_label("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"))
    empty = pool_reserves(TX([B("X", M, 5)], [B("X", M, 4)]), M, "TRADER")
    chk("нет котировки -- честный отказ с причиной",
        empty["ок"] is False and "не опознаны" in empty["почему"])

    # Нативная нога SOL, без WSOL у трейдера
    keys = [{"pubkey": "TRADER"}]
    t2 = TX([B("POOL", M, 1000), B("POOL", WSOL, 10), B("TRADER", M, 0)],
            [B("POOL", M, 900), B("POOL", WSOL, 11), B("TRADER", M, 100)],
            keys=keys, pre_b=[2_000_000_000], post_b=[1_000_000_000], fee=0)
    chk("нативная нога подхватывается", abs(exec_price(t2, M, "TRADER") - 0.01) < 1e-9,
        str(exec_price(t2, M, "TRADER")))

    # Пул с НАТИВНЫМ SOL (кривая pump.fun): котировки в токенах нет вовсе.
    keys_n = [{"pubkey": "TRADER"}, {"pubkey": "CURVE"}]
    tn = TX([B("CURVE", M, 1_000_000), B("TRADER", M, 0)],
            [B("CURVE", M, 900_000), B("TRADER", M, 100_000)],
            keys=keys_n, pre_b=[5_000_000_000, 100_000_000_000],
            post_b=[4_000_000_000, 101_000_000_000])
    rn = pool_reserves(tn, M, "TRADER")
    chk("пул с нативным SOL опознан", rn.get("ок") and rn["владелец_хранилищ"] == "CURVE",
        str(rn)[:140])
    chk("котировка помечена нативной", rn.get("вид_котировки") == "нативный")
    chk("цена из нативных резервов", abs(rn["цена_до"] - 100.0 / 1_000_000) < 1e-12,
        str(rn.get("цена_до")))

    s = summarise("тест", [{"не_удалось": "нет"}])
    s2 = summarise("тест2", [{"разные_пулы": True, "влияние_на_наш_пул_pct": 5.0},
                              {"разные_пулы": False}])
    chk("разные пулы считаются в сводке",
        s2["сделок_с_разными_пулами"] == 1 and s2["сделок_в_одном_пуле"] == 1, str(s2))
    chk("влияние на наш пул -- только по разным пулам",
        s2["медиана_влияния_на_наш_пул_pct"] == 5.0)
    chk("нерасшифрованная сделка в медианы не идёт",
        s["пересчитано"] == 0 and s["медиана_а_влияние_лидера_pct"] is None)
    chk("но из счёта не пропадает", s["сделок"] == 1)

    bad = 0
    for n, ok, got in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {n}" + (f"  -> {got}" if got and not ok else ""))
        bad += (not ok)
    print(f"самопроверка пересчёта: {len(checks) - bad}/{len(checks)} пройдено")
    if bad:
        raise SystemExit(f"самопроверка не пройдена: {bad} из {len(checks)}")


if __name__ == "__main__":
    main()
