#!/usr/bin/env python3
"""Задача F (C2): сколько роста остаётся тем, кто заходит после первых
крупных. Только чтение цепи.

По каждой сделке источника (Beqv6.../Fvkc2..., список из задачи A --
data/crowd_metric_<дата>.json, per_source[].trades[]) ищутся первые 5
покупателей того же минта ПОСЛЕ неё, и для каждого считается:
  * слот и место в блоке (индекс, всего транзакций, доля);
  * объём его сделки в SOL-эквиваленте и отношение к объёму источника;
  * сдвиг цены от его сделки (цена исполнения к опорной цене ДО него --
    споту после предыдущего шага, если он есть, иначе цене исполнения
    предыдущего шага);
для первых ДВУХ дополнительно:
  * priority fee -- цена за единицу CU (SetComputeUnitPrice) и лимит
    (SetComputeUnitLimit) из инструкций ComputeBudget111...;
  * чаевые -- переводы SOL от подписанта на адреса, которые в транзакциях
    ЭТОГО прогона получают переводы от >= 5 разных кошельков (тот же
    эмпирический приём, что и в c2_block_position.py: внешнего списка
    "Jito"/"сендер" в репозитории нет, имена не приписываются);
  * отношение ВСЕЙ платы (priority + чаевые) к объёму его сделки -- это и
    есть числа для вопроса владельца "платят фиксированно или
    пропорционально объёму" (сам вывод -- по таблице, а не готовым словом:
    см. aggregate()/fee_model_first_two).
Итог по всей выборке: медиана отношения объёма 1-го и 2-го копировщика к
объёму источника; рост цены через 30 с ОТ ЦЕНЫ ПОСЛЕ 2-го копировщика (не
источника) -- медиана и доля сделок с ростом >= 1.2, только там, где обе
точки известны; сделки без обеих точек -- отдельной строкой "неизвестно",
в долю не входят.

Как ищутся копировщики -- и почему НЕ двухуровневая схема разведки.

Разведка (см. коммит-сообщение задачи) предложила двухуровневую схему:
дешёвый скан getBlock(transactionDetails="accounts") для поиска, потом
getTransaction поштучно на найденные подписи для деталей. Здесь схема
проще и, по кредитам, дешевле: getBlock читается СРАЗУ на уровне "full"
(jsonParsed) -- кредит Helius не зависит от transactionDetails (тариф
docs.helius.dev: getBlock = 1 кредит при любом уровне детализации), а раз
цена одна и та же, отдельный getTransaction на каждого найденного
покупателя просто не нужен: то же чтение блока уже дало и место в блоке,
и полную транзакцию для объёма/цены/платы. Плата -- в трафике и времени
на блок, а не в кредитах, поэтому у неё свой предохранитель:
--scan-blocks-cap (сколько блоков вперёд самое большее сканировать на одну
сделку) и общий --max-minutes (владелец: прогон не дольше часа) -- при
исчерпании второго сделка помечается "неизвестно: не успели", а не висит.
Оценка разведки (crowd_30s медиана 88 покупателей за 30 с на этих же
минтах) говорит, что блоков сверх блока источника обычно нужно немного,
но это ожидание, не гарантия, поэтому предохранители есть оба.

Цена "через 30 с после 2-го копировщика" считается по установленному в
репозитории методу c2_crowd_metric.py (здесь -- свой, независимый список
функций, потому что сам файл c2_crowd_metric.py на этой ветке не живёт):
  * "цена сразу после" его транзакции -- spot_after: остаток хранилища
    минта и хранилища котировки ПОСЛЕ этой транзакции, поделённые друг на
    друга (годится только для пулов вида x*y=k -- RESERVE_SPOT_PROGRAMS:
    Pump AMM, Raydium CPMM, Raydium AMM v4; для CLMM/DLMM/кривых -- "нет
    данных", резервы цены не задают);
  * "цена через 30 с" -- цена ПОСЛЕДНЕЙ сделки в том же пуле с blockTime
    <= t+30 (метод "цена берётся из сделок, а не из резервов" --
    bloom_price_curve.py); если после 2-го копировщика 30 секунд никто в
    пуле не торговал, последней сделкой оказывается его же собственная --
    и это не отдельный случай, а естественный результат того же поиска.

Только чтение: getBlock, getTransaction, getSignaturesForAddress через
c2_common.C2Rpc (служба c2_followers_growth, суточный потолок C2 общий
100 000 = 200 000, плюс отдельная строка в solana_rpc_client.DAILY_BUDGET
для отчёта по этой службе отдельно). Ни одной подписи, ни одного
sendTransaction, ни одного POST в торговые API.

--self-test не ходит в сеть: FakeRpc, синтетические и настоящие
транзакции из data/ (c2_common.load_real_txs), проверяется вся арифметика
(медианы, доли, отношение объёмов, порог x1.2, разбор ComputeBudget,
разбор чаевых) и то, что молчание узла даёт "неизвестно", а не 0.
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import c2_pool_programs as PP  # noqa: E402

SERVICE = "c2_followers_growth"
SOURCES = {C.LEADER_BEQV: "leader",
           "Fvkc2thk1YcAASdR2gi8uf9n67JW9Dqqr9iRd99MDhoB": "Brez"}
CB_PROGRAM = "ComputeBudget111111111111111111111111111111"
N_FOLLOWERS = 5
N_DETAILED = 2
GROWTH_WINDOW_S = 30
GROWTH_THRESHOLD = 1.2
TIP_MIN_WALLETS = 5
DEFAULT_SCAN_BLOCKS_CAP = 60
DEFAULT_MAX_MINUTES = 55.0

FULL_OPTS = {"encoding": "jsonParsed", "transactionDetails": "full", "rewards": False,
             "maxSupportedTransactionVersion": C.TX_VERSION, "commitment": "finalized"}
SIG_OPTS = {"transactionDetails": "signatures", "rewards": False,
            "maxSupportedTransactionVersion": C.TX_VERSION, "commitment": "finalized"}

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# Пулы вида x*y=k, где остаток хранилища -- это и есть резерв (спот =
# котировка/токен). Тот же список, что в c2_crowd_metric.py и
# c2_swap_build.py -- независимая копия, потому что сам файл
# c2_crowd_metric.py на этой ветке не живёт (см. docstring).
RESERVE_SPOT_PROGRAMS = {
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "Pump AMM",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
}

_LABELS: dict | None = None


def pool_labels() -> dict:
    global _LABELS  # noqa: PLW0603
    if _LABELS is None:
        _LABELS = PP.labels()
    return _LABELS


# ------------------------------------------------------------ ComputeBudget/чаевые

def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    b = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + b


def b58encode(raw: bytes) -> str:
    """Только для самопроверки -- строит тестовые data-поля инструкций."""
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    pad = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * pad + (out or "1")


def compute_budget(tx: dict) -> dict:
    """SetComputeUnitPrice/Limit из инструкций ComputeBudget111...; фактическая
    priority fee = meta.fee - 5000 * число подписей. Нет инструкции -- None,
    а не 0: молчание не должно превращаться в "бесплатно"."""
    price = limit = None
    for ix in (((tx.get("transaction") or {}).get("message") or {}).get("instructions") or []):
        if ix.get("programId") != CB_PROGRAM or not ix.get("data"):
            continue
        try:
            d = b58decode(ix["data"])
        except (ValueError, IndexError):
            continue
        if d[:1] == b"\x03" and len(d) >= 9:
            price = struct.unpack("<Q", d[1:9])[0]
        elif d[:1] == b"\x02" and len(d) >= 5:
            limit = struct.unpack("<I", d[1:5])[0]
    meta = tx.get("meta") or {}
    nsig = len((tx.get("transaction") or {}).get("signatures") or [])
    fee = meta.get("fee")
    return {"cu_price_micro": price, "cu_limit": limit,
            "cu_consumed": meta.get("computeUnitsConsumed"),
            "priority_lamports": (fee - 5000 * nsig) if fee is not None else None}


def sol_transfers(tx: dict) -> list:
    """(from, to, lamports) системных переводов -- внешних и вложенных."""
    msg = (tx.get("transaction") or {}).get("message") or {}
    lists = [msg.get("instructions") or []]
    for g in (tx.get("meta") or {}).get("innerInstructions") or []:
        lists.append(g.get("instructions") or [])
    out = []
    for lst in lists:
        for ix in lst:
            p = (ix or {}).get("parsed") or {}
            if (ix or {}).get("program") == "system" and p.get("type") == "transfer":
                i = p.get("info") or {}
                out.append((i.get("source"), i.get("destination"), int(i.get("lamports") or 0)))
    return out


def compute_tips(transfer_log: list) -> tuple[set, dict]:
    """Чаевый адрес -- тот, что в ЭТОМ прогоне получает переводы от >= 5
    разных кошельков. Тот же эмпирический приём, что в c2_block_position.py,
    только на выборке из собственных детальных транзакций (первые два
    копировщика по каждой сделке), а не по всем транзакциям блока -- это
    уже сузило бы список, а не расширило: вопрос в том, куда платят САМИ
    копировщики этих двух лидеров, а не общий список адресов сети."""
    recv: dict = {}
    for wallet, transfers in transfer_log:
        for to, lam in transfers:
            if lam > 0 and to:
                recv.setdefault(to, set()).add(wallet)
    tip_accounts = {a for a, ws in recv.items() if len(ws) >= TIP_MIN_WALLETS}
    return tip_accounts, recv


# ------------------------------------------------------------ цена

def spot_after(tx: dict, pool: dict) -> tuple:
    """(спот сразу после сделки по остаткам хранилищ, причина если нет)."""
    if not pool.get("pool_vault") or not pool.get("quote_vault"):
        return None, "пул не определён"
    rows = {r["account"]: r for r in C.token_rows(tx).values()}
    tv = rows.get(pool["pool_vault"])
    if tv is None or not tv["post"]:
        return None, "нет остатка хранилища токена после сделки"
    if pool.get("quote_mint") == C.NATIVE_QUOTE:
        return None, "котировка -- натив кривой, резервы виртуальные"
    qv = rows.get(pool["quote_vault"])
    if qv is None:
        return None, "нет остатка хранилища котировки после сделки"
    return C.ui(qv["post"], qv["dec"]) / C.ui(tv["post"], tv["dec"]), None


def pool_program_of(tx: dict, vault: str | None) -> str | None:
    if not vault:
        return None
    return PP.pool_program(tx, vault, pool_labels())["pool_program"]


def pool_supports_reserve_spot(tx: dict, pool: dict) -> bool:
    return pool_program_of(tx, pool.get("pool_vault")) in RESERVE_SPOT_PROGRAMS


def volume_sol_equiv(tx: dict, wallet: str, *, quote_mint: str | None = None,
                      rate_usd_per_sol: float | None) -> tuple:
    """Объём сделки кошелька в SOL-эквиваленте -- по ЕГО СОБСТВЕННЫМ балансам
    (quote_spend: sol + wsol + usd/курс), а не по котировке пула.

    Почему не по котировке пула. У реальной сделки (Omakase, FLyzixQC)
    пул котируется в WSOL, но сам кошелёк платит USDC через маршрут
    USDC->WSOL->токен -- на его СОБСТВЕННЫХ счетах WSOL не двигается вовсе,
    движется только USDC. Если считать объём по котировке пула, для такой
    сделки вышел бы 0 вместо настоящей траты. quote_spend уже устроен
    правильно (тот же метод, что у classify_tx/задачи A): считает то, что
    РЕАЛЬНО ушло со счетов кошелька, независимо от того, в каком пуле и с
    каким маршрутом это в итоге попало в целевой минт.

    "Неизвестно", а не 0, в двух случаях: (1) во всех трёх ногах (SOL,
    WSOL, USDC/USDT) движения нет вовсе -- значит платёж прошёл в чём-то,
    чего quote_spend не видит (например, xStock), а не что кошелёк
    заплатил ноль; (2) есть USD-нога, а курса SOL/USD нет -- перевести её
    в SOL-эквивалент нечем."""
    s = C.quote_spend(tx, wallet)
    if s["sol"] <= 0 and s["wsol"] <= 0 and s["usd"] <= 0:
        hint = f" (пул источника котируется в {quote_mint})" if quote_mint else ""
        return None, f"платёж не найден ни в SOL, ни в WSOL, ни в USDC/USDT{hint} -- в SOL-эквивалент не перевести"
    if s["usd"] > 0:
        if not rate_usd_per_sol:
            return None, "часть платежа в USDC/USDT, курс SOL/USD неизвестен"
        return float(s["sol"] + s["wsol"] + s["usd"] / D(str(rate_usd_per_sol))), None
    return float(s["sol"] + s["wsol"]), None


def is_skipped_slot_error(msg: str) -> bool:
    return C.is_skipped_slot_error(msg)


def block_signatures(rpc, slot: int) -> list | None:
    """Подписи блока в порядке исполнения; None -- слот пропущен узлом."""
    try:
        blk = rpc.call("getBlock", [slot, SIG_OPTS])
    except RuntimeError as exc:
        if is_skipped_slot_error(str(exc)):
            return None
        raise
    return (blk or {}).get("signatures") or []


def find_anchor(rpc, slot0: int, bt0: int, t_target: int, *, max_tries: int = 20) -> dict:
    """Первый блок с blockTime >= t_target (метод c2_crowd_metric.find_anchor:
    шаг оценивается по средней длительности слота ~0.4 с и уточняется по
    факту). Нужен как граница before= для чтения истории пула назад."""
    s = slot0 + max(10, int((t_target - bt0) / 0.4)) + 5
    tried = 0
    while tried < max_tries:
        tried += 1
        try:
            blk = rpc.call("getBlock", [s, SIG_OPTS])
        except RuntimeError as exc:
            if is_skipped_slot_error(str(exc)):
                s += 1
                continue
            raise
        bt = (blk or {}).get("blockTime")
        sigs = (blk or {}).get("signatures") or []
        if bt is None or not sigs:
            s += 1
            continue
        if bt >= t_target:
            return {"slot": s, "block_time": bt, "signature": sigs[-1], "tries": tried}
        rate = (s - slot0) / max(1, bt - bt0)
        s += max(3, int((t_target - bt) * rate) + 3)
    raise RuntimeError(f"якорный блок после {C.utc(t_target)} не найден за {max_tries} попыток")


def sig_window(rpc, address: str, before_sig: str, slot_floor: int, *, max_pages: int = 30) -> tuple:
    """Подписи адреса от якоря НАЗАД, пока не пройдём slot_floor. Возвращает
    (список, выкачано_полностью)."""
    out, cur = [], before_sig
    for _ in range(max_pages):
        page = rpc.signatures(address, before=cur, limit=1000)
        out.extend(page)
        if len(page) < 1000 or (page[-1].get("slot") or 0) < slot_floor:
            return out, True
        cur = page[-1]["signature"]
    return out, False


def order_in_slot(rpc, slot: int, sigs: list, cache: dict) -> dict:
    if slot not in cache:
        bs = block_signatures(rpc, slot)
        cache[slot] = {x: i for i, x in enumerate(bs or [])}
    return {x: cache[slot].get(x) for x in sigs}


def price_at_time(rpc, pool: dict, hist: list, t_point: int, fallback_price, fallback_sig: str | None,
                   tx_cache: dict, order_cache: dict) -> dict:
    """Цена ПОСЛЕДНЕЙ сделки пула с blockTime <= t_point; нет ни одной --
    запасная цена (обычно исполнение самой отправной транзакции: "никто не
    торговал -- цена та же, что оставил последний, кто торговал")."""
    cands = [s for s in hist if s.get("blockTime") is not None and s["blockTime"] <= t_point]
    slots = sorted({s["slot"] for s in cands}, reverse=True)
    for sl in slots:
        in_slot = [s["signature"] for s in cands if s["slot"] == sl]
        need = [x for x in in_slot if x not in tx_cache]
        if need:
            tx_cache.update(rpc.get_txs(need))
        if any(tx_cache.get(x) is None for x in in_slot):
            return {"price": None, "why": f"узел не отдал транзакцию пула в слоте {sl}"}
        if len(in_slot) > 1:
            pos = order_in_slot(rpc, sl, in_slot, order_cache)
            if any(v is None for v in pos.values()):
                return {"price": None, "why": f"нет порядка транзакций в слоте {sl}"}
            in_slot.sort(key=lambda x: pos[x], reverse=True)
        for x in in_slot:
            ev = C.pool_event(tx_cache[x], pool)
            if ev.get("kind") == "swap":
                return {"price": ev["price"], "sig": x, "slot": sl, "is_fallback": False}
            if ev.get("kind") == "removal":
                return {"price": None, "why": f"ликвидность пула снята до точки ({x[:12]})"}
    if fallback_price is None:
        return {"price": None, "why": "в истории пула до точки нет ни одной сделки"}
    return {"price": fallback_price, "sig": fallback_sig, "slot": None, "is_fallback": True}


# ------------------------------------------------------------ поиск копировщиков

def find_followers(rpc, *, source_wallet: str, mint: str, slot0: int, sig0: str,
                    need: int, max_blocks: int, deadline: float | None) -> tuple:
    """До `need` чужих покупателей минта после источника, блок за блоком.

    Каждый блок читается ПОЛНОСТЬЮ (getBlock, full/jsonParsed) -- см.
    docstring модуля про то, почему не двухуровневая схема поиска: кредит
    Helius от transactionDetails не зависит, а полный уровень сразу даёт и
    место в блоке, и готовую транзакцию для объёма/цены/платы, без
    отдельного getTransaction на каждого найденного. --scan-blocks-cap и
    --max-minutes -- предохранители по трафику/времени, а не по кредитам.
    """
    found: list = []
    slot = slot0
    scanned = 0
    while len(found) < need and scanned < max_blocks:
        if deadline is not None and time.time() > deadline:
            return found, f"остановлено общим пределом времени после {scanned} блоков поиска"
        try:
            blk = rpc.call("getBlock", [slot, FULL_OPTS]) or {}
        except RuntimeError as exc:
            if is_skipped_slot_error(str(exc)):
                slot += 1
                scanned += 1
                continue
            return found, f"getBlock({slot}) не отдался: {str(exc)[:150]}"
        btx = blk.get("transactions") or []
        start = 0
        if slot == slot0:
            idx0 = next((i for i, x in enumerate(btx) if C.first_signature(x) == sig0), None)
            if idx0 is None:
                return found, "подписи источника нет в его же блоке"
            start = idx0 + 1
        for j in range(start, len(btx)):
            x = btx[j]
            if (x.get("meta") or {}).get("err") is not None:
                continue
            buyers = [w for w in C.mint_buyers(x, mint) if w != source_wallet]
            if not buyers:
                continue
            found.append({"wallet": buyers[0], "signature": C.first_signature(x),
                          "slot": slot, "index_in_block": j, "block_total": len(btx), "tx": x})
            if len(found) >= need:
                break
        slot += 1
        scanned += 1
    if len(found) < need:
        return found, f"после {scanned} блоков поиска найдено {len(found)} из {need}"
    return found, None


# ------------------------------------------------------------ одна сделка источника

def analyze_trade(rpc, source_addr: str, trade: dict, *, deadline: float | None,
                   need: int = N_FOLLOWERS, need_detailed: int = N_DETAILED,
                   max_blocks: int = DEFAULT_SCAN_BLOCKS_CAP, transfer_log: list | None = None) -> dict:
    if transfer_log is None:
        transfer_log = []
    sig0, slot0, mint = trade.get("signature"), trade.get("slot"), trade.get("mint")
    row = {
        "source": SOURCES.get(source_addr, str(source_addr)[:8]), "source_address": source_addr,
        "signature": sig0, "slot": slot0, "mint": mint,
        "source_volume_sol_equiv": trade.get("spend_sol_equiv"),
        "source_price_0": trade.get("price_0"), "source_index_in_block": trade.get("index_in_block"),
        "rate_usd_per_sol": trade.get("rate_usd_per_sol"), "quote_mint": trade.get("quote_mint"),
        "pool_vault": trade.get("pool_vault"), "followers": [], "n_followers_found": 0,
        "why_missing_followers": None, "growth_after_2nd_30s": None, "reason_skipped": None,
    }
    if not (sig0 and isinstance(slot0, int) and mint):
        row["reason_skipped"] = "в данных задачи A нет подписи, слота или минта этой сделки"
        return row
    pool = {"pool_vault": trade.get("pool_vault"), "pool_owner": trade.get("pool_owner"),
            "quote_vault": trade.get("quote_vault"), "quote_mint": trade.get("quote_mint")}
    if not pool["pool_vault"]:
        row["reason_skipped"] = ("пул источника не определён в задаче A -- копировщики всё "
                                 "равно ищутся, но объём и сдвиг цены посчитать нечем")
    try:
        followers, why_missing = find_followers(
            rpc, source_wallet=source_addr, mint=mint, slot0=slot0, sig0=sig0,
            need=need, max_blocks=max_blocks, deadline=deadline)
    except RuntimeError as exc:
        row["why_missing_followers"] = f"узел: {str(exc)[:200]}"
        followers = []
    else:
        row["why_missing_followers"] = why_missing
    row["n_followers_found"] = len(followers)

    prev_price = D(str(trade["price_0"])) if trade.get("price_0") is not None else None
    prev_ref = "источник: цена исполнения"
    if trade.get("spot_after"):
        prev_price, prev_ref = D(str(trade["spot_after"])), "источник: спот после сделки"

    for i, f in enumerate(followers, start=1):
        tx = f["tx"]
        frow = {
            "rank": i, "wallet": f["wallet"], "signature": f["signature"], "slot": f["slot"],
            "index_in_block": f["index_in_block"], "block_total": f["block_total"],
            "share_in_block": (round(f["index_in_block"] / f["block_total"], 4)
                               if f["block_total"] else None),
            "same_block_as_source": f["slot"] == slot0, "blocks_after_source": f["slot"] - slot0,
        }
        vol, why_vol = volume_sol_equiv(tx, f["wallet"], quote_mint=pool.get("quote_mint"),
                                         rate_usd_per_sol=trade.get("rate_usd_per_sol"))
        frow["volume_sol_equiv"] = vol
        frow["why_no_volume"] = why_vol
        src_vol = trade.get("spend_sol_equiv")
        if vol is not None and src_vol:
            frow["volume_vs_source"] = round(vol / src_vol, 6)
            frow["why_no_volume_vs_source"] = None
        else:
            frow["volume_vs_source"] = None
            frow["why_no_volume_vs_source"] = why_vol or "объём источника неизвестен"

        ev = C.pool_event(tx, pool) if pool.get("pool_vault") else {"kind": "нет пула"}
        exec_price = ev.get("price") if ev.get("kind") == "swap" else None
        frow["price_exec"] = str(exec_price) if exec_price is not None else None
        frow["pool_event"] = ev.get("kind")
        if exec_price is not None and prev_price is not None:
            frow["price_shift_vs_prev"] = round(float(exec_price / prev_price), 6)
            frow["price_shift_ref"] = prev_ref
            frow["why_no_price_shift"] = None
        else:
            frow["price_shift_vs_prev"] = None
            frow["price_shift_ref"] = None
            frow["why_no_price_shift"] = ("нет цены исполнения этого копировщика" if exec_price is None
                                          else "нет опорной цены до этого копировщика")

        sp, why_sp = (spot_after(tx, pool) if pool.get("pool_vault") else (None, "пул не определён"))
        frow["spot_after"] = str(sp) if sp is not None else None
        frow["why_no_spot_after"] = None if sp is not None else why_sp

        if i <= need_detailed:
            cb = compute_budget(tx)
            frow.update(cu_price_micro=cb["cu_price_micro"], cu_limit=cb["cu_limit"],
                        cu_consumed=cb["cu_consumed"], priority_lamports=cb["priority_lamports"])
            sg = C.signers(tx)
            transfers = [(to, lam) for frm, to, lam in sol_transfers(tx) if frm in sg]
            frow["_transfers"] = transfers
            transfer_log.append((f["wallet"], transfers))

        row["followers"].append(frow)
        if sp is not None:
            prev_price, prev_ref = sp, f"копировщик {i}: спот после сделки"
        elif exec_price is not None:
            prev_price, prev_ref = exec_price, f"копировщик {i}: цена исполнения"

    # рост через 30 с ОТ ЦЕНЫ ПОСЛЕ 2-го копировщика
    if not pool.get("pool_vault"):
        row["growth_after_2nd_30s"] = {"known": False, "why_not": "пул источника не определён"}
    elif len(followers) < need_detailed:
        row["growth_after_2nd_30s"] = {"known": False,
            "why_not": f"{need_detailed}-й копировщик не найден ({len(followers)} из {need_detailed})"}
    else:
        f2 = followers[need_detailed - 1]
        tx2 = f2["tx"]
        bt2 = tx2.get("blockTime")
        ev2 = C.pool_event(tx2, pool)
        exec2 = ev2.get("price") if ev2.get("kind") == "swap" else None
        sp2, why_sp2 = spot_after(tx2, pool)
        p_after2 = sp2 if sp2 is not None else exec2
        if p_after2 is None or bt2 is None:
            row["growth_after_2nd_30s"] = {"known": False,
                "why_not": (why_sp2 or "нет цены сразу после 2-го копировщика") if bt2 is not None
                else "нет времени блока 2-го копировщика"}
        else:
            try:
                anchor = find_anchor(rpc, f2["slot"], bt2, bt2 + GROWTH_WINDOW_S + 1)
                hist, ok = sig_window(rpc, pool["pool_vault"], anchor["signature"], f2["slot"])
            except RuntimeError as exc:
                row["growth_after_2nd_30s"] = {"known": False,
                    "why_not": f"окно 30 с не построено: {str(exc)[:150]}"}
            else:
                if not ok:
                    row["growth_after_2nd_30s"] = {"known": False,
                        "why_not": "история пула в окне 30 с не выкачана полностью"}
                else:
                    tx_cache = {C.first_signature(tx2): tx2}
                    pa = price_at_time(rpc, pool, hist, bt2 + GROWTH_WINDOW_S, exec2,
                                       C.first_signature(tx2), tx_cache, {})
                    if pa.get("price") is None:
                        row["growth_after_2nd_30s"] = {"known": False, "why_not": pa.get("why")}
                    else:
                        ratio = float(D(str(pa["price"])) / D(str(p_after2)))
                        row["growth_after_2nd_30s"] = {
                            "known": True, "price_after_2nd": str(p_after2),
                            "price_after_2nd_is_spot": sp2 is not None,
                            "price_30s_later": str(pa["price"]), "ratio": round(ratio, 6),
                            "ge_1_2x": ratio >= GROWTH_THRESHOLD,
                            "no_later_trade_in_window": bool(pa.get("is_fallback"))}
    return row


def apply_tips(per_trade: list, transfer_log: list) -> dict:
    """Второй проход: чаевые считаются по ВСЕМ собранным переводам сразу
    (тот адрес чаевый, только если у него >= 5 разных отправителей по всей
    выборке), затем раскладываются обратно по копировщикам 1-2."""
    tip_accounts, recv = compute_tips(transfer_log)
    for t in per_trade:
        for f in t["followers"]:
            if "_transfers" not in f:
                continue
            tr = f.pop("_transfers")
            f["sol_transfers_out"] = [[to, lam] for to, lam in tr]
            f["tip_lamports"] = sum(lam for to, lam in tr if to in tip_accounts)
            f["tip_to"] = sorted({to for to, lam in tr if to in tip_accounts and lam > 0})
            pl = f.get("priority_lamports")
            f["total_fee_lamports"] = (pl + f["tip_lamports"]) if pl is not None else None
            vol = f.get("volume_sol_equiv")
            f["fee_to_volume_lamports_per_sol"] = (
                round(f["total_fee_lamports"] / vol, 2)
                if f.get("total_fee_lamports") is not None and vol else None)
    return {"tip_accounts": sorted({"address": a, "distinct_senders": len(recv[a])} for a in tip_accounts),
            "candidates_seen": len(recv)}


# ------------------------------------------------------------ сводка

def _cv(xs: list) -> float | None:
    """Коэффициент вариации (std/среднее) -- низкий у величины, которая
    держится около одних и тех же чисел, высокий у величины, разбросанной
    широко. Не вывод, а число для таблицы: см. fee_model_first_two.note."""
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    if m == 0:
        return None
    var = sum((x - m) ** 2 for x in xs) / len(xs)
    return round((var ** 0.5) / abs(m), 4)


def aggregate(per_trade: list) -> dict:
    def ratios_for_rank(rank):
        return [f["volume_vs_source"] for t in per_trade for f in t["followers"]
                if f["rank"] == rank and f["volume_vs_source"] is not None]

    r1, r2 = ratios_for_rank(1), ratios_for_rank(2)
    growth_rows = [t["growth_after_2nd_30s"] for t in per_trade if t.get("growth_after_2nd_30s")]
    known = [g for g in growth_rows if g.get("known")]
    unknown_reasons = [{"signature": t["signature"], "why_not": t["growth_after_2nd_30s"]["why_not"]}
                       for t in per_trade
                       if t.get("growth_after_2nd_30s") and not t["growth_after_2nd_30s"].get("known")]
    ratios_g = [g["ratio"] for g in known]
    share_ge = (sum(1 for g in known if g["ge_1_2x"]) / len(known)) if known else None

    fee_rows = [f for t in per_trade for f in t["followers"] if f["rank"] <= N_DETAILED]
    fees = [f["total_fee_lamports"] for f in fee_rows if f.get("total_fee_lamports") is not None]
    fee_ratios = [f["fee_to_volume_lamports_per_sol"] for f in fee_rows
                 if f.get("fee_to_volume_lamports_per_sol") is not None]
    tips = [f["tip_lamports"] for f in fee_rows if f.get("tip_lamports")]
    tip_hist: dict = {}
    for v in tips:
        tip_hist[v] = tip_hist.get(v, 0) + 1
    top_tip_values = sorted(tip_hist.items(), key=lambda kv: -kv[1])[:10]

    skipped = [{"signature": t.get("signature"), "reason": t["reason_skipped"]}
              for t in per_trade if t.get("reason_skipped")]
    missing_followers = [{"signature": t.get("signature"), "found": t.get("n_followers_found"),
                          "why_not": t.get("why_missing_followers")}
                         for t in per_trade if t.get("why_missing_followers")]

    return {
        "n_trades": len(per_trade),
        "n_with_pool": sum(1 for t in per_trade if t.get("pool_vault")),
        "n_skipped": len(skipped), "skipped_reasons": skipped,
        "n_followers_found_hist": {str(k): sum(1 for t in per_trade if t["n_followers_found"] == k)
                                   for k in range(N_FOLLOWERS + 1)},
        "trades_with_missing_followers": missing_followers,
        "volume_ratio_follower1": {"n": len(r1), "median": C.median(r1)},
        "volume_ratio_follower2": {"n": len(r2), "median": C.median(r2)},
        "growth_after_2nd_30s": {
            "n_known": len(known), "n_unknown": len(unknown_reasons),
            "median_ratio": C.median(ratios_g),
            "share_ge_1_2x": round(share_ge, 4) if share_ge is not None else None,
            "unknown_reasons": unknown_reasons},
        "fee_model_first_two": {
            "n_rows": len(fee_rows), "n_with_fee": len(fees), "n_with_ratio": len(fee_ratios),
            "total_fee_lamports_median": C.median(fees),
            "fee_to_volume_median_lamports_per_sol": C.median(fee_ratios),
            "total_fee_cv": _cv(fees), "fee_to_volume_cv": _cv(fee_ratios),
            "tip_lamports_top_values": top_tip_values,
            "note": ("низкий коэффициент вариации total_fee_cv при высоком "
                     "fee_to_volume_cv говорит в пользу фиксированной платы (сумма не "
                     "меняется, а её доля от объёма -- меняется вместе с объёмом); "
                     "обратная картина -- в пользу платы, пропорциональной объёму. "
                     "Это наблюдение по числам этого прогона, не готовый ответ владельцу.")},
    }


# ------------------------------------------------------------ вывод

def print_table(per_trade: list, agg: dict) -> None:
    print(f"{'source':8} {'signature':14} {'followers':9} {'vol1/vol0':9} {'vol2/vol0':9} "
          f"{'growth_2nd_30s':14} reason")
    for t in per_trade:
        v1 = next((f["volume_vs_source"] for f in t["followers"] if f["rank"] == 1), None)
        v2 = next((f["volume_vs_source"] for f in t["followers"] if f["rank"] == 2), None)
        g = t.get("growth_after_2nd_30s") or {}
        gr = f"{g['ratio']:.3f}" if g.get("known") else "unknown"
        reason = t.get("reason_skipped") or t.get("why_missing_followers") or \
            (g.get("why_not") if not g.get("known") else "")
        print(f"{str(t.get('source') or '')[:8]:8} {str(t.get('signature'))[:14]:14} "
              f"{t.get('n_followers_found', 0):9} "
              f"{('%.3f' % v1) if v1 is not None else '-':9} "
              f"{('%.3f' % v2) if v2 is not None else '-':9} {gr:14} {reason or ''}")
    print("--- сводка ---")
    print(json.dumps(agg, ensure_ascii=False, indent=1, default=str)[:6000])


# ------------------------------------------------------------ прогон

def load_trades(src_file: Path, want_sources: set) -> list:
    d = json.loads(src_file.read_text(encoding="utf-8"))
    out = []
    for ps in d.get("per_source") or []:
        addr = ps.get("address")
        if addr not in want_sources:
            continue
        for t in ps.get("trades") or []:
            out.append((addr, t))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--sources", default="", help="через запятую: только эти адреса источников")
    ap.add_argument("--limit", type=int, default=0, help="сколько сделок источника взять (0 = все)")
    ap.add_argument("--need-followers", type=int, default=N_FOLLOWERS)
    ap.add_argument("--need-detailed", type=int, default=N_DETAILED)
    ap.add_argument("--scan-blocks-cap", type=int, default=DEFAULT_SCAN_BLOCKS_CAP,
                    help="сколько блоков вперёд от источника сканировать на одну сделку")
    ap.add_argument("--max-minutes", type=float, default=DEFAULT_MAX_MINUTES,
                    help="жёсткий предел прогона (владелец: не дольше часа)")
    ap.add_argument("--crowd-metric-json", default="", help="файл задачи A (иначе -- последний в data/)")
    ap.add_argument("--out", default="", help="куда писать отчёт (иначе data/c2_followers_growth_<дата>.json)")
    a = ap.parse_args()
    if a.self_test:
        return self_test()

    t0 = time.time()
    deadline = t0 + a.max_minutes * 60
    key = C.RC.helius_key()[0]
    if not key:
        print("СТОП: ключ Helius не задан (HELIUS_API)", file=sys.stderr)
        return 2

    if a.crowd_metric_json:
        src_file = Path(a.crowd_metric_json)
    else:
        cands = sorted(C.DATA.glob("crowd_metric_2*.json"))
        if not cands:
            print("СТОП: data/crowd_metric_*.json (задача A) не найден на этой ветке -- "
                  "без него нет списка сделок источников", file=sys.stderr)
            return 2
        src_file = cands[-1]

    want_sources = {x.strip() for x in a.sources.split(",") if x.strip()} or set(SOURCES)
    trades = load_trades(src_file, want_sources)
    if a.limit:
        trades = trades[:a.limit]
    if not trades:
        print("СТОП: сделок источников в файле не нашлось", file=sys.stderr)
        return 2

    rpc = C.C2Rpc(SERVICE, key=key)
    per_trade: list = []
    transfer_log: list = []
    stopped_early = None
    for i, (addr, t) in enumerate(trades):
        if time.time() > deadline:
            stopped_early = f"остановлено пределом --max-minutes после {i} из {len(trades)} сделок"
            break
        try:
            row = analyze_trade(rpc, addr, t, deadline=deadline, need=a.need_followers,
                                need_detailed=a.need_detailed, max_blocks=a.scan_blocks_cap,
                                transfer_log=transfer_log)
        except C.BudgetExceeded as exc:
            stopped_early = f"остановлено суточным потолком C2: {exc}"
            break
        except RuntimeError as exc:
            row = {"source": SOURCES.get(addr, str(addr)[:8]), "source_address": addr,
                  "signature": t.get("signature"), "slot": t.get("slot"), "mint": t.get("mint"),
                  "followers": [], "n_followers_found": 0, "why_missing_followers": None,
                  "growth_after_2nd_30s": None, "reason_skipped": f"узел: {str(exc)[:200]}"}
        per_trade.append(row)

    tips_info = apply_tips(per_trade, transfer_log)
    agg = aggregate(per_trade)
    out_path = Path(a.out) if a.out else C.DATA / f"c2_followers_growth_{C.today_utc()}.json"
    report = {
        "generated_utc": C.utc(time.time()), "source_file": src_file.name,
        "sources": {v: k for k, v in SOURCES.items() if k in want_sources},
        "trades_total_in_source_file": len(trades), "trades_processed": len(per_trade),
        "stopped_early": stopped_early, "max_minutes": a.max_minutes,
        "need_followers": a.need_followers, "need_detailed": a.need_detailed,
        "scan_blocks_cap": a.scan_blocks_cap, "tip_accounts_this_run": tips_info["tip_accounts"],
        "per_trade": per_trade, "aggregate": agg,
        "credits_this_run": rpc.stats.get("кредитов"), "elapsed_s": round(time.time() - t0, 1),
        "c2_usage": C.c2_usage_report(),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print_table(per_trade, agg)
    print(f"отчёт: {out_path}")
    print(f"кредитов {rpc.stats.get('кредитов')}; C2 сегодня {C.c2_spent_today()}")
    if stopped_early:
        print("ВНИМАНИЕ:", stopped_early)
    return 0


# ------------------------------------------------------------ самопроверка

class FakeRpc:
    """Узел без сети: блоки и история подписей заданы словарями заранее."""

    def __init__(self, blocks: dict, sig_hist: dict, txs: dict, *, fail_slots: set | None = None):
        self.blocks = blocks
        self.sig_hist = sig_hist
        self.txs = txs
        self.fail_slots = fail_slots or set()
        self.calls: list = []

    def call(self, method: str, params: list, **_kw):
        self.calls.append((method, params[0] if params else None))
        if method != "getBlock":
            raise AssertionError(f"неожиданный метод {method}")
        slot, opts = params[0], params[1]
        if slot in self.fail_slots:
            raise RuntimeError("узел молчит (симуляция)")
        b = self.blocks.get(slot)
        if b is None:
            raise RuntimeError(f"нет блока {slot} (симуляция)")
        if opts.get("transactionDetails") == "signatures":
            return {"blockTime": b.get("blockTime"), "signatures": b.get("sig_list") or []}
        return {"transactions": b.get("transactions") or []}

    def signatures(self, address: str, *, before: str | None = None, until: str | None = None,
                  limit: int = 1000) -> list:
        lst = self.sig_hist.get(address, [])
        if before:
            bslot = next((x["slot"] for x in lst if x["signature"] == before), None)
            if bslot is not None:
                lst = [x for x in lst if x["slot"] < bslot]
        return lst[:limit]

    def get_txs(self, sigs: list) -> dict:
        self.calls.append(("getTransaction*", len(sigs)))
        return {s: self.txs.get(s) for s in sigs}


def _mk_token_row(idx, owner, mint, pre, post, dec=0):
    return (
        {"accountIndex": idx, "owner": owner, "mint": mint,
         "uiTokenAmount": {"amount": str(pre), "decimals": dec}},
        {"accountIndex": idx, "owner": owner, "mint": mint,
         "uiTokenAmount": {"amount": str(post), "decimals": dec}},
    )


def _mk_swap_tx(sig, slot, bt, trader, *, vault, quote_vault, pool_owner, quote_mint,
                vault_pre, vault_post, quote_pre, quote_post, dex="DEX",
                cb_price=None, cb_limit=None, fee=5000, transfers=None, buyer_ata="BUYER_ATA",
                buyer_gain=0, err=None):
    """Синтетическая jsonParsed-транзакция свопа: трейдер покупает минт у
    пула (vault отдаёт, quote_vault получает), decimals=0 для простоты
    арифметики -- код от decimals не зависит, только числа в тесте проще."""
    keys = [trader, vault, quote_vault, buyer_ata, "TRADER_QUOTE_ATA"]
    pre1, post1 = _mk_token_row(1, pool_owner, "MINT", vault_pre, vault_post)
    pre2, post2 = _mk_token_row(2, pool_owner, quote_mint, quote_pre, quote_post)
    pre_t, post_t = [pre1, pre2], [post1, post2]
    if buyer_gain:
        pre3, post3 = _mk_token_row(3, trader, "MINT", 0, buyer_gain)
        pre_t.append(pre3)
        post_t.append(post3)
    if quote_mint not in (None, C.NATIVE_QUOTE):
        # Трейдер платит котировкой (WSOL) из своего счёта -- ровно то, что
        # пул получил на quote_vault. Без этого quote_spend() честно видел бы
        # 0 (у трейдера в тесте иначе нет ни одного счёта в этой котировке),
        # и "неизвестно" тогда неотличимо бы смешалось с "потратил 0".
        paid = quote_post - quote_pre
        pre4, post4 = _mk_token_row(4, trader, quote_mint, paid, 0)
        pre_t.append(pre4)
        post_t.append(post4)
    instructions = [{"programId": dex, "accounts": [vault, quote_vault, buyer_ata]}]
    if cb_price is not None:
        instructions.append({"programId": CB_PROGRAM,
                             "data": b58encode(b"\x03" + struct.pack("<Q", cb_price))})
    if cb_limit is not None:
        instructions.append({"programId": CB_PROGRAM,
                             "data": b58encode(b"\x02" + struct.pack("<I", cb_limit))})
    inner = []
    for frm, to, lam in (transfers or []):
        inner.append({"program": "system", "parsed": {"type": "transfer",
                     "info": {"source": frm, "destination": to, "lamports": lam}}})
    return {
        "slot": slot, "blockTime": bt,
        "transaction": {"signatures": [sig], "message": {
            "accountKeys": [{"pubkey": k, "signer": k == trader} for k in keys],
            "instructions": instructions}},
        "meta": {"err": err, "fee": fee, "computeUnitsConsumed": 120000,
                "preBalances": [0] * len(keys), "postBalances": [0] * len(keys),
                "preTokenBalances": pre_t, "postTokenBalances": post_t,
                "innerInstructions": ([{"instructions": inner}] if inner else [])},
    }


def self_test() -> int:
    checks: list = []

    def chk(name, ok, got=""):
        checks.append((name, bool(ok), got))

    # -------------------- 1. ComputeBudget: цена за CU и лимит --------------------
    tx_cb = {
        "transaction": {"signatures": ["S" * 88], "message": {"instructions": [
            {"programId": CB_PROGRAM, "data": b58encode(b"\x03" + struct.pack("<Q", 123456))},
            {"programId": CB_PROGRAM, "data": b58encode(b"\x02" + struct.pack("<I", 200000))},
            {"programId": "OTHER", "data": b58encode(b"\xff\xff")},
        ]}},
        "meta": {"fee": 5000 + 777, "computeUnitsConsumed": 150000},
    }
    cb = compute_budget(tx_cb)
    chk("ComputeBudget: cu_price_micro и cu_limit разобраны из base58 data",
        cb["cu_price_micro"] == 123456 and cb["cu_limit"] == 200000, cb)
    chk("ComputeBudget: priority_lamports = fee - 5000*подписей",
        cb["priority_lamports"] == 777, cb)
    chk("ComputeBudget: cu_consumed берётся из meta", cb["cu_consumed"] == 150000, cb)
    cb_empty = compute_budget({"transaction": {"signatures": ["S" * 88], "message": {"instructions": []}},
                              "meta": {"fee": 5000}})
    chk("нет инструкции ComputeBudget -- цена и лимит None, а не 0",
        cb_empty["cu_price_micro"] is None and cb_empty["cu_limit"] is None
        and cb_empty["priority_lamports"] == 0, cb_empty)
    cb_nofee = compute_budget({"transaction": {"signatures": [], "message": {}}, "meta": {}})
    chk("нет meta.fee -- priority_lamports None (неизвестно), а не 0",
        cb_nofee["priority_lamports"] is None, cb_nofee)
    chk("b58encode/b58decode -- взаимно обратные на произвольных байтах",
        b58decode(b58encode(b"\x00\x01\x02\xff\x10")) == b"\x00\x01\x02\xff\x10")

    # -------------------- 2. Чаевые: адрес с >= 5 отправителей --------------------
    log = [(f"W{i}", [("TIP1", 20_000_000)]) for i in range(5)]
    log.append(("W5", [("RARE", 1_000_000)]))
    log.append(("W6", [("TIP1", 20_000_000), ("RARE", 500_000)]))
    tip_accounts, recv = compute_tips(log)
    chk("чаевый адрес -- >= 5 разных отправителей", tip_accounts == {"TIP1"}, tip_accounts)
    chk("адрес с 1-2 отправителями в чаевые не попадает", "RARE" not in tip_accounts, recv.get("RARE"))
    rows = [{"followers": [{"rank": 1, "_transfers": [("TIP1", 20_000_000), ("RARE", 500_000)],
                            "priority_lamports": 300_000, "volume_sol_equiv": 2.0}]}]
    apply_tips(rows, log)
    f0 = rows[0]["followers"][0]
    chk("чаевые считаются только по адресам-кандидатам (RARE не входит)",
        f0["tip_lamports"] == 20_000_000, f0)
    chk("вся плата = priority + чаевые", f0["total_fee_lamports"] == 20_300_000, f0)
    chk("плата/объём считается лампорт-в-SOL", abs(f0["fee_to_volume_lamports_per_sol"] - 10_150_000.0) < 1,
        f0)
    chk("_transfers из отчёта убран (не JSON-мусор)", "_transfers" not in f0, f0)

    # -------------------- 3. Объём: настоящие транзакции репозитория --------------------
    txs = C.load_real_txs()
    # Настоящая сделка BmjAUD/87pa2UbB: пул НАЙДЕННЫЙ по хранилищу минта
    # котируется в xStock, но это промежуточный хоп маршрута Jupiter (тот
    # же tx, что и в самопроверке c2_pool_programs -- top-level роутер там
    # JUP6Lk...); настоящий платёж кошелька -- в SOL (0.201025, лампорты
    # видно напрямую по preBalances/postBalances), и именно это число нужно
    # как объём, а не "неизвестно": quote_spend меряет то, что РЕАЛЬНО ушло
    # со счетов кошелька, а не котировку промежуточного пула на пути.
    t_xstock = next(x for s, x in txs.items() if s.startswith("2uPwpSAQ"))
    vol_x, why_x = volume_sol_equiv(t_xstock, "BmjAUDbwBMxR5shrmzBtKRwveVahFGFiEH3oTq7QTHnu",
                                    quote_mint="Xsa62P5mvPZbAB7uT8XCXbXjizM3aC1PB2y3nRhcvnA", rate_usd_per_sol=None)
    chk("маршрут через промежуточный пул xStock -- объём всё равно виден: настоящий платёж в SOL",
        vol_x is not None and abs(vol_x - 0.201025) < 1e-9, (vol_x, why_x))

    # Настоящая сделка Omakase (F5MYbj, FLyzixQC): пул котируется в WSOL, но
    # САМ кошелёк платит USDC через маршрут USDC->WSOL->токен -- на его
    # собственных счетах движется только USDC (usd=1000, wsol=0), поэтому
    # без курса это тоже "неизвестно", а с курсом -- sol + usd/курс.
    t_wsol = next(x for s, x in txs.items()
                 if "F5MYbjEATQFD6rxwdS2zXzEHBGuUhSGvJkUhLFAcr4hv" in C.signers(x))
    wallet_w = "F5MYbjEATQFD6rxwdS2zXzEHBGuUhSGvJkUhLFAcr4hv"
    vol_norate, why_norate = volume_sol_equiv(t_wsol, wallet_w, quote_mint=C.WSOL, rate_usd_per_sol=None)
    chk("платёж кошелька идёт через USDC, курса не дали -- объём 'неизвестно', а не 0 и не только SOL-нога",
        vol_norate is None and "курс" in (why_norate or ""), (vol_norate, why_norate))
    rate = C.rate_from_tx(t_wsol)
    vol_w, why_w = volume_sol_equiv(t_wsol, wallet_w, quote_mint=C.WSOL, rate_usd_per_sol=float(rate))
    exp_w = 0.007663512 + 8.706675352
    chk("с известным курсом -- объём = sol-нога + usd-нога/курс (настоящая сделка)",
        vol_w is not None and abs(vol_w - exp_w) < 1e-6, (vol_w, exp_w, why_w, rate))

    vol_usd_norate, why_usd = volume_sol_equiv(
        {"meta": {"preTokenBalances": [], "postTokenBalances": []}, "transaction": {"message": {}}},
        "W", quote_mint=C.USDC, rate_usd_per_sol=None)
    chk("во всех трёх ногах (SOL/WSOL/USD) движения нет -- 'неизвестно', а не 0",
        vol_usd_norate is None and "не перевести" in (why_usd or ""), (vol_usd_norate, why_usd))

    # -------------------- 4. Синтетический сценарий: источник + 2 копировщика + рост --------------------
    VAULT, QVAULT, OWNER = "VAULT", "QVAULT", "POOLOWNER"
    pool = {"pool_vault": VAULT, "pool_owner": OWNER, "quote_vault": QVAULT, "quote_mint": C.WSOL}
    SRC = "SRCWALLET"

    tx_src = _mk_swap_tx("SRC_SIG", 1000, 100_000, SRC, vault=VAULT, quote_vault=QVAULT,
                        pool_owner=OWNER, quote_mint=C.WSOL, vault_pre=100_000, vault_post=99_000,
                        quote_pre=1_000, quote_post=1_010, buyer_gain=1000)
    tx_f1 = _mk_swap_tx("F1_SIG", 1000, 100_000, "FOLLOWER1", vault=VAULT, quote_vault=QVAULT,
                       pool_owner=OWNER, quote_mint=C.WSOL, vault_pre=99_000, vault_post=98_000,
                       quote_pre=1_010, quote_post=1_025, buyer_gain=1000,
                       cb_price=50_000, cb_limit=180_000, fee=5000 + 90_000,
                       transfers=[("FOLLOWER1", "TIP1", 20_000_000)])
    tx_f2 = _mk_swap_tx("F2_SIG", 1001, 100_000 + 5, "FOLLOWER2", vault=VAULT, quote_vault=QVAULT,
                       pool_owner=OWNER, quote_mint=C.WSOL, vault_pre=98_000, vault_post=96_000,
                       quote_pre=1_025, quote_post=1_075, buyer_gain=2000,
                       cb_price=10_000, cb_limit=150_000, fee=5000 + 15_000,
                       transfers=[("FOLLOWER2", "TIP1", 20_000_000)])
    src_index_in_block = 0
    block_src = {"blockTime": 100_000,
                "transactions": [tx_src, tx_f1],
                "sig_list": ["SRC_SIG", "F1_SIG"]}
    block_next = {"blockTime": 100_005, "transactions": [tx_f2], "sig_list": ["F2_SIG"]}
    blocks = {1000: block_src, 1001: block_next}

    trade = {"signature": "SRC_SIG", "slot": 1000, "mint": "MINT", "spend_sol_equiv": 10.0,
             "price_0": "0.01", "index_in_block": src_index_in_block, "rate_usd_per_sol": None,
             "pool_vault": VAULT, "pool_owner": OWNER, "quote_vault": QVAULT, "quote_mint": C.WSOL,
             "spot_after": str(1_010 / 99_000)}

    # цена через 30с: LATER-сделка в том же пуле, найденная через якорь + историю подписей
    tx_later = _mk_swap_tx("LATER_SIG", 1050, 100_000 + 5 + 25, "WALLET_LATER", vault=VAULT,
                          quote_vault=QVAULT, pool_owner=OWNER, quote_mint=C.WSOL,
                          vault_pre=96_000, vault_post=93_000, quote_pre=1_075, quote_post=1_195,
                          buyer_gain=3000)
    anchor_slot = 1001 + max(10, int(31 / 0.4)) + 5  # см. find_anchor
    blocks[anchor_slot] = {"blockTime": 100_000 + 5 + 40, "transactions": [],
                          "sig_list": ["FILLER", "ANCHOR_SIG"]}
    sig_hist = {VAULT: [
        {"signature": "ANCHOR_SIG", "slot": anchor_slot, "blockTime": 100_000 + 5 + 40, "err": None},
        {"signature": "LATER_SIG", "slot": 1050, "blockTime": 100_000 + 5 + 25, "err": None},
        {"signature": "F2_SIG", "slot": 1001, "blockTime": 100_000 + 5, "err": None},
        {"signature": "F1_SIG", "slot": 1000, "blockTime": 100_000, "err": None},
        {"signature": "SRC_SIG", "slot": 1000, "blockTime": 100_000, "err": None},
    ]}
    all_txs = {"SRC_SIG": tx_src, "F1_SIG": tx_f1, "F2_SIG": tx_f2, "LATER_SIG": tx_later}
    fake = FakeRpc(blocks, sig_hist, all_txs)
    transfer_log: list = []
    row = analyze_trade(fake, SRC, trade, deadline=None, need=2, need_detailed=2,
                        transfer_log=transfer_log)

    chk("копировщики найдены оба в блоке источника + следующем", row["n_followers_found"] == 2, row)
    chk("нашли ровно сколько просили (need=2) -- причина нехватки не проставлена",
        row["why_missing_followers"] is None, row["why_missing_followers"])
    f1r = row["followers"][0]
    f2r = row["followers"][1]
    chk("копировщик 1: слот и место в блоке", f1r["slot"] == 1000 and f1r["index_in_block"] == 1, f1r)
    chk("копировщик 2: слот следующего блока и место 0", f2r["slot"] == 1001 and f2r["index_in_block"] == 0, f2r)
    chk("копировщик 1: объём -- сколько WSOL реально заплатил (15), а не сколько токена получил",
        f1r["volume_sol_equiv"] == 15.0, f1r["volume_sol_equiv"])
    chk("копировщик 1: объём/источник = 15/10 = 1.5", f1r["volume_vs_source"] == 1.5, f1r)
    chk("копировщик 2: объём/источник = 50/10 = 5.0", f2r["volume_vs_source"] == 5.0, f2r)
    exp_exec_f1 = 15 / 1000
    chk("копировщик 1: цена исполнения = |дельта котировки|/|дельта минта| = 15/1000",
        f1r["price_exec"] is not None and abs(float(f1r["price_exec"]) - exp_exec_f1) < 1e-9, f1r["price_exec"])
    exp_prev = 1_010 / 99_000  # спот после источника
    exp_shift1 = exp_exec_f1 / exp_prev
    chk("копировщик 1: сдвиг цены к споту ПОСЛЕ источника (не к price_0)",
        abs(f1r["price_shift_vs_prev"] - exp_shift1) < 1e-6, (f1r["price_shift_vs_prev"], exp_shift1))
    chk("копировщик 1: ComputeBudget разобран (cu_price_micro=50000, cu_limit=180000)",
        f1r["cu_price_micro"] == 50_000 and f1r["cu_limit"] == 180_000, f1r)
    chk("копировщик 1: priority_lamports = fee - 5000 = 90000", f1r["priority_lamports"] == 90_000, f1r)
    chk("у копировщика 3+ (тут их нет) ComputeBudget не считается -- поле просто отсутствует",
        "cu_price_micro" not in row["followers"][-1] if len(row["followers"]) > N_DETAILED else True)

    g = row["growth_after_2nd_30s"]
    exp_p_after2 = 1_075 / 96_000  # спот сразу после копировщика 2
    exp_p_30s = 120 / 3000         # цена исполнения LATER-сделки (её и найдёт price_at_time)
    exp_ratio = exp_p_30s / exp_p_after2
    chk("рост через 30с известен и опирается на спот ПОСЛЕ 2-го копировщика",
        g["known"] and g["price_after_2nd_is_spot"], g)
    chk("рост через 30с = цена LATER-сделки / спот после 2-го копировщика",
        abs(g["ratio"] - exp_ratio) < 1e-6, (g["ratio"], exp_ratio))
    chk(">= 1.2x посчитан по факту (в этом сценарии рост большой -> True)",
        g["ge_1_2x"] is True and exp_ratio >= 1.2, (g, exp_ratio))
    chk("сделка не помечена как 'нет более поздней в окне' -- LATER реально нашлась",
        g["no_later_trade_in_window"] is False, g)

    # -------------------- 5. Рост, если после 2-го копировщика 30с никто не торговал --------------------
    sig_hist_notrade = {VAULT: [
        {"signature": "ANCHOR_SIG", "slot": anchor_slot, "blockTime": 100_000 + 5 + 40, "err": None},
        {"signature": "F2_SIG", "slot": 1001, "blockTime": 100_000 + 5, "err": None},
        {"signature": "F1_SIG", "slot": 1000, "blockTime": 100_000, "err": None},
        {"signature": "SRC_SIG", "slot": 1000, "blockTime": 100_000, "err": None},
    ]}
    fake_nt = FakeRpc(blocks, sig_hist_notrade, all_txs)
    row_nt = analyze_trade(fake_nt, SRC, trade, deadline=None, need=2, need_detailed=2, transfer_log=[])
    g_nt = row_nt["growth_after_2nd_30s"]
    exp_exec_f2 = 50 / 2000
    exp_ratio_nt = exp_exec_f2 / exp_p_after2
    chk("никто не торговал 30с после 2-го копировщика -- 'цена через 30с' находится как "
        "последняя известная сделка в пуле, и это его же собственная",
        g_nt["known"] and abs(float(g_nt["price_30s_later"]) - exp_exec_f2) < 1e-9, g_nt)
    chk("в этом случае рост = цена исполнения самого 2-го копировщика / спот после него",
        abs(g_nt["ratio"] - exp_ratio_nt) < 1e-6, (g_nt["ratio"], exp_ratio_nt))

    # у price_at_time -- отдельно проверить и настоящий "запасной" путь: в
    # истории пула вообще нет ни одной сделки (сдвоенное значит именно
    # "нет данных", а не "0-я/своя" по умолчанию где-то в середине поиска)
    pa_empty = price_at_time(fake_nt, {"pool_vault": VAULT}, [], 100_100, exp_exec_f2, "F2_SIG", {}, {})
    chk("совсем пустая история пула -- запасная цена сработала (сама сделка 2-го копировщика)",
        pa_empty["price"] == exp_exec_f2 and pa_empty["is_fallback"] is True, pa_empty)
    pa_empty_none = price_at_time(fake_nt, {"pool_vault": VAULT}, [], 100_100, None, None, {}, {})
    chk("совсем пустая история и НЕТ даже запасной цены -- 'неизвестно', а не крах",
        pa_empty_none["price"] is None and "нет ни одной сделки" in pa_empty_none["why"], pa_empty_none)

    # -------------------- 6. Молчание узла -> 'неизвестно', а не 0/крах --------------------
    fake_dead = FakeRpc({1000: block_src}, {}, all_txs, fail_slots={1001})
    row_dead = analyze_trade(fake_dead, SRC, trade, deadline=None, transfer_log=[])
    chk("узел не отдал следующий блок -- копировщик 2 не найден, причина названа (не крах, не 0)",
        row_dead["n_followers_found"] == 1 and "getBlock" in (row_dead["why_missing_followers"] or ""),
        row_dead["why_missing_followers"])
    chk("рост через 30с -- 'неизвестно', раз 2-го копировщика нет, а не 0.0/1.0 по умолчанию",
        row_dead["growth_after_2nd_30s"]["known"] is False, row_dead["growth_after_2nd_30s"])

    # -------------------- 7. Предел времени -- останавливается, а не читает лишнее --------------------
    fake_slow = FakeRpc({1000: block_src, 1001: block_next}, {}, all_txs)
    followers_deadline, why_deadline = find_followers(
        fake_slow, source_wallet=SRC, mint="MINT", slot0=1000, sig0="SRC_SIG",
        need=5, max_blocks=60, deadline=time.time() - 1000)
    chk("дедлайн уже прошёл -- поиск копировщиков останавливается сразу, причина названа",
        followers_deadline == [] and "предел" in why_deadline, (followers_deadline, why_deadline))

    # -------------------- 8. Пул без резервного спота -> цена сразу после = исполнение --------------------
    tx_f2_other_dex = dict(tx_f2)
    tx_f2_other_dex["transaction"] = dict(tx_f2["transaction"])
    tx_f2_other_dex["transaction"]["message"] = dict(tx_f2["transaction"]["message"])
    tx_f2_other_dex["transaction"]["message"]["instructions"] = [
        {"programId": "CLMMProgramNotInReserveList111111111111111", "accounts": [VAULT, QVAULT, "BUYER_ATA"]}]
    sp_none, why_none = spot_after(tx_f2, {"pool_vault": VAULT, "quote_vault": QVAULT, "quote_mint": C.NATIVE_QUOTE})
    chk("котировка -- натив кривой -- спот 'неизвестно' по названной причине",
        sp_none is None and "натив кривой" in why_none, (sp_none, why_none))
    chk("нет пула вовсе у spot_after -- тоже 'неизвестно', а не крах",
        spot_after(tx_f2, {})[0] is None)

    # -------------------- 9. Агрегация: медианы, доли, CV -- без единого обращения к цепи --------------------
    synth_trades = [
        {"signature": "T1", "followers": [{"rank": 1, "volume_vs_source": 2.0},
                                          {"rank": 2, "volume_vs_source": 6.0}],
         "growth_after_2nd_30s": {"known": True, "ratio": 1.5, "ge_1_2x": True}, "n_followers_found": 2,
         "why_missing_followers": None, "reason_skipped": None, "pool_vault": "V"},
        {"signature": "T2", "followers": [{"rank": 1, "volume_vs_source": 4.0},
                                          {"rank": 2, "volume_vs_source": 10.0}],
         "growth_after_2nd_30s": {"known": True, "ratio": 1.0, "ge_1_2x": False}, "n_followers_found": 2,
         "why_missing_followers": None, "reason_skipped": None, "pool_vault": "V"},
        {"signature": "T3", "followers": [{"rank": 1, "volume_vs_source": 8.0}],
         "growth_after_2nd_30s": {"known": False, "why_not": "2-й копировщик не найден"},
         "n_followers_found": 1, "why_missing_followers": "1 из 5", "reason_skipped": None, "pool_vault": "V"},
    ]
    ag = aggregate(synth_trades)
    chk("медиана объёма 1-го копировщика к источнику = median(2,4,8) = 4.0",
        ag["volume_ratio_follower1"]["median"] == 4.0, ag["volume_ratio_follower1"])
    chk("медиана объёма 2-го копировщика к источнику = median(6,10) = 8.0 (T3 без 2-го не в счёте)",
        ag["volume_ratio_follower2"]["median"] == 8.0, ag["volume_ratio_follower2"])
    chk("рост: доля >= 1.2x = 1 из 2 ИЗВЕСТНЫХ (T3 неизвестна и в долю не входит)",
        ag["growth_after_2nd_30s"]["share_ge_1_2x"] == 0.5 and ag["growth_after_2nd_30s"]["n_known"] == 2
        and ag["growth_after_2nd_30s"]["n_unknown"] == 1, ag["growth_after_2nd_30s"])
    chk("рост: медиана = median(1.5, 1.0) = 1.25",
        ag["growth_after_2nd_30s"]["median_ratio"] == 1.25, ag["growth_after_2nd_30s"])
    chk("неизвестная сделка перечислена по подписи и причине, а не молча пропущена",
        ag["growth_after_2nd_30s"]["unknown_reasons"] == [{"signature": "T3", "why_not": "2-й копировщик не найден"}],
        ag["growth_after_2nd_30s"]["unknown_reasons"])
    chk("коэффициент вариации: постоянная величина даёт cv=0",
        _cv([10, 10, 10]) == 0.0)
    chk("коэффициент вариации: разброс даёт cv>0", _cv([1, 100]) is not None and _cv([1, 100]) > 0)
    chk("коэффициент вариации: меньше двух чисел -- None (не 0)", _cv([5]) is None)

    # -------------------- 10. Отчёт видит пропущенные сделки по имени, не молча --------------------
    trade_no_pool = dict(trade)
    trade_no_pool.update(signature="NOPOOL_SIG", pool_vault=None, pool_owner=None, quote_vault=None)
    row_np = analyze_trade(FakeRpc({}, {}, {}), SRC, trade_no_pool, deadline=None, transfer_log=[])
    chk("сделка без пула -- reason_skipped назван, но копировщиков всё равно пробуем искать",
        row_np["reason_skipped"] is not None and "пул источника не определён" in row_np["reason_skipped"], row_np)
    trade_bad = {"signature": None, "slot": None, "mint": None}
    row_bad = analyze_trade(FakeRpc({}, {}, {}), SRC, trade_bad, deadline=None, transfer_log=[])
    chk("сделка без подписи/слота/минта в исходных данных -- сразу пропуск с причиной",
        row_bad["reason_skipped"] is not None, row_bad)

    # -------------------- 11. Служба зарегистрирована как положено --------------------
    chk("имя службы начинается с c2_ (нужно C2Rpc)", SERVICE.startswith("c2_"), SERVICE)
    chk("служба вписана в DAILY_BUDGET (расход считается общим замером, не только сводкой C2)",
        isinstance(C.RC.DAILY_BUDGET.get(SERVICE), int) and C.RC.DAILY_BUDGET[SERVICE] > 0,
        C.RC.DAILY_BUDGET.get(SERVICE))

    bad = 0
    for name, ok, got in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {name}" + (f"  -> {str(got)[:400]}" if not ok else ""))
        bad += (not ok)
    print(f"самопроверка c2_followers_growth: {len(checks) - bad}/{len(checks)} пройдено")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(self_test() if "--self-test" in sys.argv else main())
