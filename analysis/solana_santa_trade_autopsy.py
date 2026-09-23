#!/usr/bin/env python3
"""Разбор живой сделки SANTA кошелька TEST1: откуда взялись -13.4%.

Вопросы владельца, по порядку:
  1. Слоты и время обеих транзакций; пул (адреса хранилищ, программа) в
     покупке и в продаже -- один или разные; резервы до и после каждой;
     заложенное проскальзывание (minOut) в продаже.
  2. Все сделки по SANTA во всех пулах между нашей покупкой и продажей:
     время, кошелёк, направление, объём в SOL. Сделки лидера -- отдельно.
  3. Сэндвич: в блоке нашей продажи -- чужая продажа НЕПОСРЕДСТВЕННО
     перед нашей и покупка сразу после от ТОГО ЖЕ кошелька. То же для
     блока покупки.
  4. Цена последней сделки в пуле за 5 секунд до нашей продажи против
     нашей средней цены выхода -- разница в процентах.
  5. Вывод одной строкой.

Окно между покупкой и продажей читается ПОБЛОЧНО: только так видно
порядок транзакций внутри блока, без которого сэндвич не доказать.

Только чтение цепочки. Ни одной сделки не отправляется.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

OUT_PATH = REPO_ROOT / "data" / "solana_santa_trade_autopsy.json"

WALLET = "E1qAJBmrJDhBvm2sV8kfMXFAmgzHuSRNRosPEgMkKiWS"
LEADER = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
MINT = "3c7mmVSyEH8jfZXgxvpLsETtko1Y16DyRJ5XYB4snhGt"
BUY_SIG = ("3LDstzbddt923pr8An42vCqxbaJJxgiSFUvij2Uo9kg8nGXfZGtycUkDb2o88q1ViYAgRTRV5MozfT3mEoqaF9Ru")
SELL_SIG = ("3Tcs6C4R5yNBAp4PLipqYyCSSQXcqxQw3hqxCtYFny9doQZNJrhtws3jaKheQnd96PWB9UMfhBS9fw2fR1A1Mqj1")

WSOL = "So11111111111111111111111111111111111111112"
SLOT_MS = 400          # реальный темп слотов ближе к 400 мс, а не к 250
MAX_SLOTS = 4000       # потолок окна: больше -- честно скажем, что обрезали

# Разбор данных инструкции обмена: где в ней лежит минимальный выход.
# Смещения -- от конца или от начала, по раскладке конкретной программы.
RAYDIUM_AMM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1"
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMP_CURVE = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
JUPITER_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


def u64(b: bytes, off: int) -> int | None:
    return int.from_bytes(b[off:off + 8], "little") if len(b) >= off + 8 else None


def min_out_from(program: str, data_b58: str) -> dict:
    """Минимальный выход, заложенный в инструкцию обмена.

    Раскладки разные у каждой программы, и выдумывать их нельзя: что не
    разобрали -- так и пишем, с длиной данных, чтобы было видно почему.
    """
    out = {"программа": program, "длина_данных": None}
    try:
        d = b58decode(data_b58)
    except (ValueError, IndexError):
        out["почему"] = "данные инструкции не разобрались из base58"
        return out
    out["длина_данных"] = len(d)
    if program == RAYDIUM_AMM_V4 and len(d) >= 17 and d[0] == 9:
        out.update(вид="Raydium AMM v4 swapBaseIn", вход=u64(d, 1), минимальный_выход=u64(d, 9))
    elif program == RAYDIUM_CPMM and len(d) >= 24:
        out.update(вид="Raydium CPMM swap_base_input", вход=u64(d, 8),
                   минимальный_выход=u64(d, 16))
    elif program == PUMP_AMM and len(d) >= 24:
        # sell: base_amount_in, min_quote_amount_out
        out.update(вид="pump AMM", первое_число=u64(d, 8), второе_число=u64(d, 16))
    elif program == PUMP_CURVE and len(d) >= 24:
        out.update(вид="кривая pump.fun", первое_число=u64(d, 8), второе_число=u64(d, 16))
    elif program == JUPITER_V6 and len(d) >= 19:
        # Хвост маршрута фиксированный: in u64, quoted_out u64, slippage u16, fee u8
        tail = d[-19:]
        slippage = int.from_bytes(tail[16:18], "little")
        out.update(вид="Jupiter v6 route", вход=u64(tail, 0), ожидаемый_выход=u64(tail, 8),
                   проскальзывание_bps=slippage)
        q = u64(tail, 8)
        if q is not None:
            out["минимальный_выход"] = int(q * (10_000 - slippage) / 10_000)
    else:
        out["почему"] = "раскладка данных этой программы не разобрана -- не выдумываем"
    return out


def keys_of(tx: dict) -> list[str]:
    txn = (tx or {}).get("transaction") or {}
    raw = txn.get("accountKeys") or ((txn.get("message") or {}).get("accountKeys")) or []
    return [k.get("pubkey") if isinstance(k, dict) else k for k in raw]


def signers_of(tx: dict) -> set:
    txn = (tx or {}).get("transaction") or {}
    raw = txn.get("accountKeys") or ((txn.get("message") or {}).get("accountKeys")) or []
    return {k.get("pubkey") for k in raw if isinstance(k, dict) and k.get("signer")}


def bal_map(entries) -> dict:
    out = {}
    for b in entries or []:
        o, m = b.get("owner"), b.get("mint")
        if not o or not m:
            continue
        out[(o, m)] = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0.0)
    return out


def native_delta(tx: dict, pubkey: str) -> float:
    meta = (tx or {}).get("meta") or {}
    keys = keys_of(tx)
    if pubkey not in keys:
        return 0.0
    i = keys.index(pubkey)
    pb, po = meta.get("preBalances") or [], meta.get("postBalances") or []
    if i >= len(pb) or i >= len(po):
        return 0.0
    d = (po[i] - pb[i]) / 1e9
    if i == 0:
        d += (meta.get("fee") or 0) / 1e9    # комиссия -- не часть сделки
    return d


def trade_of(tx: dict, mint: str) -> dict | None:
    """Кто торговал, в какую сторону и на сколько SOL. Пулы отсеиваются:
    торговец -- тот, кто ПОДПИСАЛ транзакцию."""
    meta = (tx or {}).get("meta") or {}
    pre, post = bal_map(meta.get("preTokenBalances")), bal_map(meta.get("postTokenBalances"))
    signers = signers_of(tx)
    best = None
    for (o, m) in set(pre) | set(post):
        if m != mint or o not in signers:
            continue
        d = post.get((o, mint), 0.0) - pre.get((o, mint), 0.0)
        if d and (best is None or abs(d) > abs(best[1])):
            best = (o, d)
    if best is None:
        return None
    owner, dt = best
    dw = post.get((owner, WSOL), 0.0) - pre.get((owner, WSOL), 0.0)
    sol = dw + native_delta(tx, owner)
    return {"кошелёк": owner, "направление": "покупка" if dt > 0 else "продажа",
            "токенов": abs(dt), "sol": abs(sol) if sol else None,
            "цена": (abs(sol) / abs(dt)) if sol and dt else None}


def pool_of(tx: dict, mint: str, trader: str | None) -> dict:
    from solana_pool_price_recompute import pool_reserves  # noqa: PLC0415
    return pool_reserves(tx, mint, trader or "")


def swap_instruction(tx: dict, vaults: set) -> dict | None:
    """Инструкция, которая трогает хранилища пула -- внешняя, с данными."""
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    best = None
    for ins in (msg.get("instructions") or []):
        accs = set(ins.get("accounts") or [])
        hit = len(accs & vaults)
        if hit and ins.get("data") and (best is None or hit > best[0]):
            best = (hit, ins)
    return best[1] if best else None


def scan_window(rpc, from_slot: int, to_slot: int, mint: str, note: dict) -> list[dict]:
    """Все сделки по минту в окне, ПОБЛОЧНО и по порядку внутри блока."""
    rows = []
    n = to_slot - from_slot + 1
    if n > MAX_SLOTS:
        note["окно_обрезано"] = (f"окно {n} слотов больше потолка {MAX_SLOTS}: "
                                  f"прочитаны первые {MAX_SLOTS}")
        to_slot = from_slot + MAX_SLOTS - 1
    прочитано = пусто = 0
    for slot in range(from_slot, to_slot + 1):
        try:
            blk = rpc.call("getBlock", [slot, {
                "encoding": "jsonParsed", "transactionDetails": "accounts",
                "maxSupportedTransactionVersion": 1, "rewards": False}])
        except RuntimeError as exc:
            note.setdefault("слоты_без_ответа", []).append({"слот": slot, "почему": str(exc)[:120]})
            continue
        if not blk:
            пусто += 1
            continue
        прочитано += 1
        bt = blk.get("blockTime")
        for pos, tx in enumerate(blk.get("transactions") or []):
            if ((tx.get("meta") or {}).get("err")):
                continue
            tr = trade_of(tx, mint)
            if not tr:
                continue
            p = pool_of(tx, mint, tr["кошелёк"])
            rows.append({**tr, "слот": slot, "позиция_в_блоке": pos, "время_utc": bt,
                          "подпись": ((tx.get("transaction") or {}).get("signatures") or [None])[0],
                          "ключ_пула": p.get("ключ_пула") if p.get("ок") else None,
                          "программа": p.get("программа") if p.get("ок") else None,
                          "цена_пула_после": p.get("цена_после") if p.get("ок") else None,
                          "цена_пула_до": p.get("цена_до") if p.get("ок") else None})
    note["слотов_прочитано"] = прочитано
    note["слотов_пустых_или_пропущенных"] = пусто
    return rows


def sandwich(rows: list[dict], our: dict) -> dict:
    """Чужая продажа прямо перед нашей и покупка сразу после -- один кошелёк."""
    same = [r for r in rows if r["слот"] == our["слот"] and r["ключ_пула"] == our["ключ_пула"]]
    same.sort(key=lambda r: r["позиция_в_блоке"])
    idx = next((i for i, r in enumerate(same) if r["подпись"] == our["подпись"]), None)
    out = {"сделок_в_блоке_на_этом_пуле": len(same), "наша_позиция": idx,
            "до_нас": [], "после_нас": []}
    if idx is None:
        out["почему"] = "нашей сделки нет среди сделок блока по этому пулу"
        return out
    out["до_нас"] = [{k: r[k] for k in ("кошелёк", "направление", "sol", "позиция_в_блоке")}
                      for r in same[:idx]]
    out["после_нас"] = [{k: r[k] for k in ("кошелёк", "направление", "sol", "позиция_в_блоке")}
                         for r in same[idx + 1:]]
    пары = []
    for a in same[:idx]:
        for b in same[idx + 1:]:
            if a["кошелёк"] == b["кошелёк"] and a["направление"] != b["направление"]:
                пары.append({"кошелёк": a["кошелёк"], "перед_нами": a["направление"],
                              "после_нас": b["направление"],
                              "sol_перед": a["sol"], "sol_после": b["sol"]})
    out["сэндвич_найден"] = bool(пары)
    out["пары"] = пары
    if not пары:
        out["вывод"] = ("сэндвича нет: в блоке на этом пуле не нашлось кошелька, который "
                         "торговал и перед нами, и после нас в противоположные стороны")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-interval-s", type=float, default=0.05)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return

    from solana_crowd_scan import Rpc, helius_key  # noqa: PLC0415
    key, _ = helius_key()
    rpc = Rpc(key, min_interval_s=args.min_interval_s, workers=1, service="разбор_пилота")

    txs = rpc.transactions([BUY_SIG, SELL_SIG])
    rep = {"собрано_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "кошелёк": WALLET, "минт": MINT, "ЧЕСТНЫЕ_ОГОВОРКИ": [
                "Только чтение цепочки: ни одной сделки не отправлено.",
                "Окно между покупкой и продажей читается поблочно -- иначе порядок "
                "транзакций внутри блока неизвестен и сэндвич недоказуем.",
                "Что не разобрано (раскладка данных инструкции, курс, пропущенный "
                "слот) -- помечено явно, а не подставлено.",
            ], "стороны": {}}

    стороны = {}
    for label, sig in (("покупка", BUY_SIG), ("продажа", SELL_SIG)):
        tx = txs.get(sig)
        s = {"подпись": sig}
        if not tx:
            s["почему"] = "транзакция не отдалась узлом"
            стороны[label] = s
            rep["стороны"][label] = s
            continue
        s["слот"] = tx.get("slot")
        s["время_utc"] = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(tx["blockTime"]))
                           if tx.get("blockTime") else None)
        s["blockTime"] = tx.get("blockTime")
        p = pool_of(tx, MINT, WALLET)
        s["пул"] = p
        tr = trade_of(tx, MINT)
        s["наша_нога"] = tr
        if p.get("ок"):
            vaults = {x for x in (p.get("хранилище_токена"), p.get("хранилище_котировки"),
                                   p.get("владелец_хранилищ")) if x}
            ins = swap_instruction(tx, vaults)
            s["инструкция_обмена"] = ({"программа": ins.get("programId"),
                                        "разбор": min_out_from(ins.get("programId"), ins.get("data"))}
                                       if ins else {"почему": "внешняя инструкция с данными, "
                                                              "трогающая хранилища, не найдена"})
        стороны[label] = s
        rep["стороны"][label] = s

    buy, sell = стороны["покупка"], стороны["продажа"]
    if buy.get("пул", {}).get("ок") and sell.get("пул", {}).get("ок"):
        rep["один_и_тот_же_пул"] = buy["пул"]["ключ_пула"] == sell["пул"]["ключ_пула"]
        rep["пул_покупки"] = buy["пул"]["ключ_пула"]
        rep["пул_продажи"] = sell["пул"]["ключ_пула"]
    if buy.get("наша_нога") and sell.get("наша_нога"):
        vin, vout = buy["наша_нога"]["sol"], sell["наша_нога"]["sol"]
        if vin and vout:
            rep["итог_сделки_pct"] = round((vout / vin - 1) * 100, 3)
            rep["вошли_sol"], rep["вышли_sol"] = vin, vout
            rep["наша_средняя_цена_входа"] = buy["наша_нога"]["цена"]
            rep["наша_средняя_цена_выхода"] = sell["наша_нога"]["цена"]

    if buy.get("слот") and sell.get("слот"):
        note = {}
        rows = scan_window(rpc, buy["слот"], sell["слот"], MINT, note)
        rep["окно"] = note
        rep["сделок_по_санта_в_окне"] = len(rows)
        rep["все_сделки_в_окне"] = [
            {**r, "время_utc": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["время_utc"]))
                                 if r.get("время_utc") else None)} for r in rows]
        rep["сделки_лидера_в_окне"] = [r for r in rep["все_сделки_в_окне"]
                                        if r["кошелёк"] == LEADER]
        rep["лидер_в_окне"] = ("лидер в окне не торговал" if not rep["сделки_лидера_в_окне"]
                                else None)
        по_пулам = {}
        for r in rows:
            k = r.get("ключ_пула") or "пул не опознан"
            g = по_пулам.setdefault(k, {"сделок": 0, "куплено_sol": 0.0, "продано_sol": 0.0,
                                         "программа": r.get("программа")})
            g["сделок"] += 1
            if r["направление"] == "покупка":
                g["куплено_sol"] += r["sol"] or 0.0
            else:
                g["продано_sol"] += r["sol"] or 0.0
        rep["по_пулам_в_окне"] = по_пулам

        наша_продажа = next((r for r in rows if r["подпись"] == SELL_SIG), None)
        наша_покупка = next((r for r in rows if r["подпись"] == BUY_SIG), None)
        rep["сэндвич_в_блоке_продажи"] = (sandwich(rows, наша_продажа) if наша_продажа
                                           else {"почему": "наша продажа не найдена в окне"})
        rep["сэндвич_в_блоке_покупки"] = (sandwich(rows, наша_покупка) if наша_покупка
                                           else {"почему": "наша покупка не найдена в окне"})

        # Цена последней сделки в пуле за 5 секунд до нашей продажи.
        if наша_продажа is not None and sell.get("blockTime"):
            t0 = sell["blockTime"] - 5
            до = [r for r in rows
                  if r["ключ_пула"] == наша_продажа["ключ_пула"]
                  and (r["слот"] < наша_продажа["слот"]
                       or (r["слот"] == наша_продажа["слот"]
                           and r["позиция_в_блоке"] < наша_продажа["позиция_в_блоке"]))
                  and (r.get("время_utc") or 0) >= t0]
            rep["сделок_в_пуле_за_5с_до_продажи"] = len(до)
            if до:
                last = до[-1]
                rep["последняя_сделка_до_продажи"] = {
                    "подпись": last["подпись"], "кошелёк": last["кошелёк"],
                    "направление": last["направление"], "sol": last["sol"],
                    "цена_пула_после": last["цена_пула_после"]}
                наша = sell["наша_нога"]["цена"] if sell.get("наша_нога") else None
                кпосле = last.get("цена_пула_после")
                if наша and кпосле:
                    rep["наша_цена_выхода_против_последней_цены_пула_pct"] = round(
                        (наша / кпосле - 1) * 100, 3)
            else:
                rep["последняя_сделка_до_продажи"] = {
                    "почему": "за 5 секунд до нашей продажи в этом пуле сделок не было"}

    OUT_PATH.write_text(json.dumps(rep, ensure_ascii=False, indent=2))
    краткое = {k: v for k, v in rep.items()
               if k not in ("все_сделки_в_окне", "сделки_лидера_в_окне", "стороны")}
    print(json.dumps(краткое, ensure_ascii=False, indent=2)[:6000])
    print(f"\nвыгрузка -> {OUT_PATH}")


def self_test() -> None:
    checks = []

    def chk(n, ok, got=""):
        checks.append((n, bool(ok), got))

    chk("base58 декодируется", b58decode("1") == b"\x00")
    chk("и многобайтовое тоже", b58decode("21") == b"\x3a", str(b58decode("21")))

    # Raydium AMM v4 swapBaseIn: тег 9, вход, минимальный выход
    import base64  # noqa: PLC0415
    raw = bytes([9]) + (1000).to_bytes(8, "little") + (950).to_bytes(8, "little")
    n = int.from_bytes(raw, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = B58[r] + s
    got = min_out_from(RAYDIUM_AMM_V4, s)
    chk("minOut Raydium v4 разобран", got.get("минимальный_выход") == 950, str(got))
    chk("и вход тоже", got.get("вход") == 1000, str(got))
    chk("незнакомая программа -- честное «не разобрано»",
        "не разобрана" in (min_out_from("XXX", s).get("почему") or ""))
    assert base64

    TX = lambda pre, post, keys, pb, po, fee=0: {  # noqa: E731
        "meta": {"preTokenBalances": pre, "postTokenBalances": post,
                  "preBalances": pb, "postBalances": po, "fee": fee},
        "transaction": {"message": {"accountKeys": keys}, "signatures": ["SIG"]}}

    def B(owner, mint, amt):
        return {"owner": owner, "mint": mint, "uiTokenAmount": {"uiAmount": amt}}

    keys = [{"pubkey": "TRADER", "signer": True}, {"pubkey": "POOL", "signer": False}]
    tx = TX([B("TRADER", MINT, 0), B("POOL", MINT, 1000)],
            [B("TRADER", MINT, 100), B("POOL", MINT, 900)],
            keys, [2_000_000_000, 5_000_000_000], [1_000_000_000, 6_000_000_000])
    tr = trade_of(tx, MINT)
    chk("торговец -- подписант, а не пул", tr["кошелёк"] == "TRADER", str(tr))
    chk("направление -- покупка", tr["направление"] == "покупка")
    chk("объём в SOL -- 1", abs(tr["sol"] - 1.0) < 1e-9, str(tr["sol"]))
    chk("цена = SOL/токен", abs(tr["цена"] - 0.01) < 1e-12, str(tr["цена"]))

    keys2 = [{"pubkey": "POOL", "signer": False}]
    chk("без подписанта с минтом сделки нет", trade_of(TX([B("POOL", MINT, 1)],
        [B("POOL", MINT, 2)], keys2, [1], [1]), MINT) is None)

    # Сэндвич: чужая продажа перед нами и покупка после -- один кошелёк
    rows = [
        {"слот": 5, "позиция_в_блоке": 1, "ключ_пула": "P", "подпись": "A",
         "кошелёк": "ATT", "направление": "продажа", "sol": 3.0},
        {"слот": 5, "позиция_в_блоке": 2, "ключ_пула": "P", "подпись": "OUR",
         "кошелёк": WALLET, "направление": "продажа", "sol": 1.7},
        {"слот": 5, "позиция_в_блоке": 3, "ключ_пула": "P", "подпись": "B",
         "кошелёк": "ATT", "направление": "покупка", "sol": 3.1},
    ]
    sw = sandwich(rows, rows[1])
    chk("сэндвич опознан", sw["сэндвич_найден"] is True, str(sw))
    chk("и назван кошелёк", sw["пары"][0]["кошелёк"] == "ATT")
    rows2 = [rows[0], rows[1]]
    sw2 = sandwich(rows2, rows[1])
    chk("без обратной ноги сэндвича нет", sw2["сэндвич_найден"] is False)
    chk("и сказано почему", "сэндвича нет" in sw2["вывод"])
    chk("сделки чужого пула в сэндвич не идут",
        sandwich(rows + [{"слот": 5, "позиция_в_блоке": 4, "ключ_пула": "ДРУГОЙ",
                           "подпись": "C", "кошелёк": "ATT", "направление": "покупка",
                           "sol": 9.0}], rows[1])["сделок_в_блоке_на_этом_пуле"] == 3)

    bad = 0
    for n, ok, got in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {n}" + (f"  -> {got}" if got and not ok else ""))
        bad += (not ok)
    print(f"самопроверка разбора SANTA: {len(checks) - bad}/{len(checks)} пройдено")
    if bad:
        raise SystemExit(f"самопроверка не пройдена: {bad} из {len(checks)}")


if __name__ == "__main__":
    main()
