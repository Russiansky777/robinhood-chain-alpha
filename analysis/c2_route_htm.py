#!/usr/bin/env python3
"""Задача G (C2): котировка HTmQz7My... у 73 из 76 пулов CPMM -- что за
токен, где его пулы с SOL, сколько стоит второй шаг, и двухшаговая
покупка SOL -> HTm -> токен одной транзакцией с проверкой симуляцией.

Всё только чтение: getAccountInfo, getMultipleAccounts, getSignatures,
getTransaction, simulateTransaction (sigVerify=false,
replaceRecentBlockhash=true). Отправки в сеть нет.

Шаг 1 (SOL -> HTm) -- Meteora DLMM swap2. Массивы бинов в хвосте
инструкции зависят от текущей цены, поэтому шаблон берётся из САМОЙ
СВЕЖЕЙ чужой сделки этого пула WSOL -> HTm, прямо перед симуляцией.
Шаг 2 (HTm -> токен) -- Raydium CPMM swap_base_input, шаблон из сделки
источника (задача D). Симуляции:
  a) весь маршрут, пользователь -- новый ключ: ошибка баланса на шаге 1;
  b) один шаг 2, новый ключ: ошибка баланса на шаге 2;
  c) весь маршрут от лица кошелька с SOL (подпись не проверяется, ключ не
     нужен): сколько HTm и токена вышло, сколько вычислений занял каждый
     шаг -- это «цена и время» второго шага.
Служба c2_route.
"""
from __future__ import annotations

import json
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import c2_swap_build as B  # noqa: E402

from solders.hash import Hash  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.message import MessageV0  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.signature import Signature  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

SERVICE = "c2_route"
HTM = "HTmQz7My6MehV7bjhJ6jde8nDND1yvsz68d24LP7YgUQ"
SIM_OPTS = {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True,
            "commitment": "processed"}


def candidate_pools() -> dict:
    """Пулы DLMM с парой WSOL/HTm из настоящих транзакций образцов."""
    out = {}
    for f in sorted(B.SAMPLES_DIR.glob("*.json")):
        for x in json.loads(f.read_text(encoding="utf-8")):
            rows = {r["account"]: r for r in C.token_rows(x["tx"]).values()}
            for ix in B.all_instructions(x["tx"]):
                if ix.get("programId") != B.DLMM or len(ix["accounts"]) < 16:
                    continue
                a = ix["accounts"]
                mints = {a[6], a[7]}
                if mints == {HTM, C.WSOL}:
                    out.setdefault(a[0], {"reserves": (a[2], a[3]), "mints": (a[6], a[7])})
    return out


def tx_b64(ixs: list, payer: str) -> str:
    msg = MessageV0.try_compile(Pubkey.from_string(payer), ixs, [], Hash.default())
    vtx = VersionedTransaction.populate(msg, [Signature.default()] * msg.header.num_required_signatures)
    import base64  # noqa: PLC0415
    return base64.b64encode(bytes(vtx)).decode()


def simulate(rpc, b64: str, watch: list | None = None) -> dict:
    opts = dict(SIM_OPTS)
    if watch:
        opts["accounts"] = {"addresses": watch, "encoding": "jsonParsed"}
    return ((rpc.call("simulateTransaction", [b64, opts]) or {}).get("value") or {})


def ui_amount(acc: dict | None) -> int | None:
    try:
        return int(acc["data"]["parsed"]["info"]["tokenAmount"]["amount"])
    except (TypeError, KeyError, ValueError):
        return None


def main() -> int:
    key = C.RC.helius_key()[0]
    if not key:
        print("СТОП: ключ Helius не задан", file=sys.stderr)
        return 2
    rpc = C.C2Rpc(SERVICE, key=key)
    out: dict = {"generated_utc": C.utc(time.time()), "mint": HTM}
    # 1. что за токен
    info = rpc.call("getAccountInfo", [HTM, {"encoding": "jsonParsed"}]) or {}
    v = (info.get("value") or {})
    parsed = ((v.get("data") or {}).get("parsed") or {}).get("info") or {}
    meta = next((e.get("state") for e in parsed.get("extensions") or []
                 if e.get("extension") == "tokenMetadata"), None)
    out["token"] = {"program_owner": v.get("owner"), "decimals": parsed.get("decimals"),
                    "supply": parsed.get("supply"), "mint_authority": parsed.get("mintAuthority"),
                    "freeze_authority": parsed.get("freezeAuthority"),
                    "name": (meta or {}).get("name"), "symbol": (meta or {}).get("symbol"),
                    "uri": (meta or {}).get("uri"),
                    "extensions": [e.get("extension") for e in parsed.get("extensions") or []]}
    print("токен:", json.dumps(out["token"], ensure_ascii=False))
    # 2. пулы с SOL и их глубина сейчас
    pools = candidate_pools()
    res_addrs = [r for p in pools.values() for r in p["reserves"]]
    accs = (rpc.call("getMultipleAccounts", [res_addrs, {"encoding": "jsonParsed"}]) or {}).get("value") or []
    bal = {a: acc for a, acc in zip(res_addrs, accs)}
    out["pools"] = []
    for pool, p in pools.items():
        amounts = {}
        for r in p["reserves"]:
            inf = ((((bal.get(r) or {}).get("data") or {}).get("parsed") or {}).get("info") or {})
            amounts[inf.get("mint")] = (inf.get("tokenAmount") or {}).get("uiAmount")
        sigs = rpc.signatures(pool, limit=100)
        recent = [x for x in sigs if x.get("blockTime") and x["blockTime"] >= time.time() - 3600]
        out["pools"].append({"pool": pool, "program": "Meteora DLMM", "sol_reserve": amounts.get(C.WSOL),
                             "htm_reserve": amounts.get(HTM), "tx_last_hour": len(recent),
                             "tx_seen": len(sigs)})
    out["pools"].sort(key=lambda x: -(x["sol_reserve"] or 0))
    print("пулы SOL/HTm:", json.dumps(out["pools"], ensure_ascii=False))
    if not out["pools"]:
        out["why_not"] = "пулов DLMM WSOL/HTm в образцах нет"
        return finish(out, rpc)
    # 3. свежий шаблон шага 1: последняя чужая сделка WSOL -> HTm в лучшем пуле
    tpl1 = tx1 = None
    for pinfo in out["pools"]:
        pool = pinfo["pool"]
        sigs = [x["signature"] for x in rpc.signatures(pool, limit=40) if x.get("err") is None]
        got = rpc.get_txs(sigs[:25])
        for sgn in sigs[:25]:
            tx = got.get(sgn)
            if not tx:
                continue
            for ix in B.all_instructions(tx):
                if ix.get("programId") != B.DLMM or not ix["accounts"] or ix["accounts"][0] != pool:
                    continue
                cand = B.extract_template(tx, B.DLMM, ix["accounts"][2])
                if cand.get("ok"):
                    mv = B.mints_and_vaults(cand, tx)
                    if mv.get("quote_mint") == C.WSOL and mv.get("base_mint") == HTM:
                        tpl1, tx1 = cand, tx
                        out["step1_template"] = {"pool": pool, "signature": sgn,
                                                 "block_time_utc": C.utc(tx.get("blockTime"))}
                        break
            if tpl1:
                break
        if tpl1:
            break
    if not tpl1:
        out["why_not"] = "свежей сделки WSOL -> HTm в пулах DLMM не нашлось"
        return finish(out, rpc)
    # 4. шаблон шага 2: самая свежая сделка источника в CPMM с котировкой HTm
    cpmm = [x for x in B.load_samples(B.CPMM) if x.get("quote_mint") == HTM]
    cpmm.sort(key=lambda x: x["tx"].get("blockTime") or 0, reverse=True)
    tpl2 = tx2 = None
    for x in cpmm:
        t = B.extract_template(x["tx"], B.CPMM, x["pool_vault"])
        if t.get("ok"):
            tpl2, tx2 = t, x["tx"]
            out["step2_template"] = {"signature": C.first_signature(x["tx"]), "pool_vault": x["pool_vault"],
                                     "block_time_utc": C.utc(x["tx"].get("blockTime"))}
            break
    if not tpl2:
        out["why_not"] = "нет шаблона CPMM HTm -> токен"
        return finish(out, rpc)
    mv1, mv2 = B.mints_and_vaults(tpl1, tx1), B.mints_and_vaults(tpl2, tx2)
    amount_sol = 10_000_000

    def route(user: str, payer: str, htm_in: int, wrap: bool, legs=(1, 2)) -> list:
        ixs = [B.cu_limit(800_000), B.cu_price(10_000),
               B.ata_idempotent(payer, user, C.WSOL, mv1["quote_program"]),
               B.ata_idempotent(payer, user, HTM, mv1["base_program"]),
               B.ata_idempotent(payer, user, mv2["base_mint"], mv2["base_program"])]
        if wrap:
            w = B.ata(user, C.WSOL, mv1["quote_program"])
            ixs += [B.sol_transfer(user, w, amount_sol), B.sync_native(w)]
        if 1 in legs:
            ixs.append(B.swap_instruction(tpl1, tx1, user, amount_sol, 1))
        if 2 in legs:
            ixs.append(B.swap_instruction(tpl2, tx2, user, htm_in, 1))
        return ixs
    t0 = time.perf_counter()
    kp = str(Keypair().pubkey())
    b_full = tx_b64(route(kp, tpl1["signers"][0], 1_000_000, False), tpl1["signers"][0])
    out["build_ms_two_hop"] = round((time.perf_counter() - t0) * 1000, 3)
    import c2_swap_sim as SIM  # noqa: PLC0415
    va = simulate(rpc, b_full)
    out["sim_a_route_fresh_key"] = {**SIM.classify(va), "units": va.get("unitsConsumed")}
    vb = simulate(rpc, tx_b64(route(kp, tpl1["signers"][0], 1_000_000, False, legs=(2,)), tpl1["signers"][0]))
    out["sim_b_step2_fresh_key"] = {**SIM.classify(vb), "units": vb.get("unitsConsumed")}
    # c) от лица кошелька с SOL: подписант свежей сделки шага 1 (он сам менял
    # SOL на HTm), подпись не проверяется, ключ его не нужен.
    user = tpl1["signers"][0]
    lam = ((rpc.call("getBalance", [user]) or {}).get("value") or 0)
    out["funded_user"] = {"address": user, "sol": lam / 1e9}
    if lam > 3 * amount_sol:
        htm_ata = B.ata(user, HTM, mv1["base_program"])
        tok_ata = B.ata(user, mv2["base_mint"], mv2["base_program"])
        pre = rpc.call("getMultipleAccounts", [[htm_ata, tok_ata], {"encoding": "jsonParsed"}]) or {}
        pre_v = pre.get("value") or [None, None]
        h0, k0 = ui_amount(pre_v[0]) or 0, ui_amount(pre_v[1]) or 0
        v1 = simulate(rpc, tx_b64(route(user, user, 0, True, legs=(1,)), user), [htm_ata])
        h1 = ui_amount((v1.get("accounts") or [None])[0])
        got_htm = (h1 - h0) if h1 is not None else None
        out["sim_c1_step1_only"] = {**SIM.classify(v1), "units": v1.get("unitsConsumed"),
                                    "htm_out_raw": got_htm}
        if got_htm and got_htm > 0:
            leg2_in = int(got_htm * 0.99)
            v2 = simulate(rpc, tx_b64(route(user, user, leg2_in, True), user), [htm_ata, tok_ata])
            accs = v2.get("accounts") or [None, None]
            k2 = ui_amount(accs[1])
            out["sim_c2_full_route"] = {**SIM.classify(v2), "units": v2.get("unitsConsumed"),
                                        "htm_in_step2_raw": leg2_in,
                                        "token_out_raw": (k2 - k0) if k2 is not None else None}
            # цена шага 1: HTm за SOL в симуляции против последней сделки пула
            ev = C.pool_event(tx1, {"pool_vault": mv1["base_vault"], "quote_vault": mv1["quote_vault"],
                                    "quote_mint": C.WSOL})
            if ev.get("kind") == "swap":
                sim_price = D(amount_sol) / D(10) ** 9 / (D(got_htm) / D(10) ** 6)
                out["step1_cost_vs_last_trade_pct"] = round(float((sim_price / ev["price"] - 1) * 100), 4)
    else:
        out["sim_c"] = "у кошелька меньше 0.03 SOL -- полный маршрут не симулирован"
    return finish(out, rpc)


def finish(out: dict, rpc) -> int:
    out["credits_this_run"] = rpc.stats.get("кредитов")
    out["c2_usage"] = C.c2_usage_report()
    (C.DATA / f"c2_route_htm_{C.today_utc()}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    for k in ("step1_template", "step2_template", "build_ms_two_hop", "sim_a_route_fresh_key",
              "sim_b_step2_fresh_key", "funded_user", "sim_c1_step1_only", "sim_c2_full_route",
              "step1_cost_vs_last_trade_pct", "sim_c", "why_not"):
        if k in out:
            print(k, json.dumps(out[k], ensure_ascii=False, default=str))
    print(f"кредитов {rpc.stats.get('кредитов')}; C2 сегодня {C.c2_spent_today()}")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        p = candidate_pools()
        ok = len(p) >= 1
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] пулы DLMM WSOL/HTm найдены в настоящих транзакциях: {list(p)[:3]}")
        print(f"самопроверка c2_route_htm: {int(ok)}/1 пройдено")
        raise SystemExit(0 if ok else 1)
    raise SystemExit(main())
