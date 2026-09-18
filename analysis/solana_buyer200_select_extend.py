#!/usr/bin/env python3
"""Solana buyer_200 -- расширение выборки со 100 до 300 покупок.

Тот же критерий отбора, что их select.py (НЕ меняется, порог $500 НЕ
растягивается): wallet_signed, ровно один положительный не-USDC/SOL
минт, потрачено >500 USDC, лог содержит "Instruction: Swap"/"Buy".
Продолжаем историю кошелька Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit
С ТОГО МЕСТА, где остановился другой ИИ (768 проверенных, 100 отобрано,
дальше в прошлое не ходили) -- see selection_audit.json['-1'] как якорь
before=.

Маршрут для новых покупок строится ТЕМ ЖЕ алгоритмом, что их routes.py
(BFS до USDC, максимум 3 хопа), но без их локального events.sqlite/tx-
кэша (тот -- часть исключённых "сырых" архивов): вместо этого пулы
берём из СОБСТВЕННОГО decode_tx() покупки (multi-hop свопы почти всегда
атомарны -- все ноги маршрута видны в одной транзакции, как мы уже
наблюдали на sig5 из Шага 1/2) + предоставленный pool_meta.json как
малый доп.источник + накопленный route_meta.json из прошлых прогонов.
Если BFS не находит путь -- честно route_decoder_missing, порог не
растягиваем ради заполнения дыр."""
from __future__ import annotations

import json
import sys
import time
from collections import deque
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from solana_buyer200_fast_price import (  # noqa: E402
    PRIOR_ROOT, OUT_ROOT, alchemy_available, get_signatures_for_address,
    get_transaction, _git_commit_progress,
)
import solana_buyer200_fast_price as fp  # noqa: E402  (для доступа к живому RPC_CALLS -- имя-интовый счётчик, копия при from-import не обновлялась бы)

sys.path.insert(0, str(PRIOR_ROOT))
import engine  # noqa: E402

WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL = "So11111111111111111111111111111111111111112"
TARGET_TOTAL = 300
MIN_SPEND = 500  # НЕ растягивать

POOL_META_PATH = PRIOR_ROOT.parent / "solana_three_check" / "pool_meta.json"
ROUTE_META_PATH = PRIOR_ROOT / "route_meta.json"

SEL_OUT = OUT_ROOT / "selected_300.json"
AUDIT_OUT = OUT_ROOT / "selection_audit_300.json"
ROUTES_OUT = OUT_ROOT / "routes_300.json"


def classify(tx: dict, h: dict) -> tuple[dict | None, dict]:
    """Дословный порт select.py:classify() -- см. docstring модуля."""
    m = tx["meta"]
    keys = tx["transaction"]["message"]["accountKeys"]
    sig = tx["transaction"]["signatures"][0]
    signed = any(k.get("signer") and k["pubkey"] == WALLET for k in keys)

    def bals(tag: str) -> dict:
        a: dict = {}
        for b in m[tag]:
            if b.get("owner") == WALLET:
                a[b["mint"]] = a.get(b["mint"], D(0)) + D(b["uiTokenAmount"]["amount"]) / D(10) ** b["uiTokenAmount"]["decimals"]
        return a

    pre, post = bals("preTokenBalances"), bals("postTokenBalances")
    delta = {k: post.get(k, D(0)) - pre.get(k, D(0)) for k in set(pre) | set(post)}
    positive = [k for k, v in delta.items() if v > 0 and k not in (USDC, SOL)]
    spent = -delta.get(USDC, D(0))
    all_ix = tx["transaction"]["message"]["instructions"] + [ix for g in (m.get("innerInstructions") or []) for ix in g["instructions"]]
    programs = sorted({x.get("programId", "") for x in all_ix})
    check = dict(signature=sig, slot=tx["slot"], time=tx["blockTime"], wallet_signed=signed,
                 usdc_spent=str(spent), positive_mints=positive, status="not_selected")
    if not signed:
        check["status"] = "wallet_not_signer"
        return None, check
    if len(positive) == 1 and spent > MIN_SPEND and any(
            "Instruction: Swap" in x or "Instruction: Buy" in x for x in (m.get("logMessages") or [])):
        mint = positive[0]
        row = dict(signature=sig, slot=tx["slot"], transaction_index=h.get("transactionIndex", tx.get("transactionIndex")),
                   time=tx["blockTime"], mint=mint, usdc_spent=str(spent), tokens_received=str(delta[mint]),
                   balance_before=str(pre.get(mint, D(0))), zero_balance=pre.get(mint, D(0)) == 0,
                   entry_usdc=str(spent / delta[mint]), programs=programs,
                   network_fee_sol=str(D(m["fee"]) / 10**9))
        check["status"] = "selected"
        return row, check
    if positive and spent > MIN_SPEND:
        check["status"] = "ambiguous_buy"
    elif positive and any(v < 0 for k, v in delta.items() if k != USDC) and spent <= 0:
        check["status"] = "non_usdc_exchange_needs_valuation"
    return None, check


def plan_route_for_purchase(mint: str, tx: dict, meta: dict) -> tuple[list[dict] | None, dict]:
    """Дословный порт routes.py:plan_routes() внутреннего цикла -- BFS до
    USDC, максимум 3 хопа, пулы -- из ЭТОЙ ЖЕ транзакции (multi-hop свопы
    почти всегда атомарны) + переданный meta (pool_meta.json + накопленный
    route_meta.json) как fallback."""
    ev = engine.decode_tx(tx)
    local_meta = dict(meta)
    for e in ev:
        if e.get("m0") and e.get("m1"):
            local_meta[e["pool"]] = {**local_meta.get(e["pool"], {}),
                                      **{k: e[k] for k in ("kind", "m0", "m1", "d0", "d1", "config") if k in e}}
    target = [e for e in ev if mint in (e.get("m0"), e.get("m1"))]

    def output_size(e: dict) -> int:
        if e["kind"] == "cp":
            return e["event"]["output_amount"] if e["event"]["output_mint"] == mint else 0
        return 0

    target.sort(key=output_size, reverse=True)
    poolids = list(dict.fromkeys([e["pool"] for e in ev] + list(local_meta)))
    for primary in target:
        quote = primary["m1"] if primary["m0"] == mint else primary["m0"]
        p0 = [{"pool": primary["pool"], "from": mint, "to": quote}]
        q = deque([(quote, p0, {mint, quote})])
        while q:
            cur, p, seen = q.popleft()
            if cur == USDC:
                return p, local_meta
            if len(p) >= 3:
                continue
            for pool in poolids:
                pm = local_meta.get(pool, {})
                if cur not in (pm.get("m0"), pm.get("m1")):
                    continue
                other = pm["m1"] if pm["m0"] == cur else pm["m0"]
                if other in seen:
                    continue
                q.append((other, p + [{"pool": pool, "from": cur, "to": other}], seen | {other}))
    return None, local_meta


def main() -> None:
    alchemy_ok = alchemy_available()
    print(f"[select300] alchemy_available={alchemy_ok} (ключ не печатается)", flush=True)

    existing_rows = json.loads((PRIOR_ROOT / "selected.json").read_text())
    existing_audit = json.loads((PRIOR_ROOT / "selection_audit.json").read_text())
    existing_routes = {r["signature"]: r for r in json.loads((PRIOR_ROOT / "routes.json").read_text())}
    global_meta = json.loads(POOL_META_PATH.read_text()) if POOL_META_PATH.exists() else {}
    global_meta.update(json.loads(ROUTE_META_PATH.read_text()) if ROUTE_META_PATH.exists() else {})

    rows: list[dict] = list(existing_rows)
    audit: list[dict] = list(existing_audit)
    routes_out: list[dict] = [existing_routes[r["signature"]] for r in existing_rows if r["signature"] in existing_routes]

    if SEL_OUT.exists():
        prior = json.loads(SEL_OUT.read_text())
        if len(prior) > len(rows):
            rows = prior
            audit = json.loads(AUDIT_OUT.read_text()) if AUDIT_OUT.exists() else audit
            routes_out = json.loads(ROUTES_OUT.read_text()) if ROUTES_OUT.exists() else routes_out
            print(f"[select300] возобновление: уже {len(rows)} покупок с прошлого прогона", flush=True)

    known_sigs = {r["signature"] for r in rows}
    before = audit[-1]["signature"] if audit else None
    print(f"[select300] стартуем с {len(rows)}/{TARGET_TOTAL}, курсор before={before[:12] if before else None}..", flush=True)

    started_at = time.monotonic()
    last_commit_at = started_at
    checked_this_run = 0
    exhausted = False

    while len(rows) < TARGET_TOTAL and not exhausted:
        page = get_signatures_for_address(WALLET, before=before, limit=1000)
        if not page:
            print("[select300] история кошелька исчерпана (пустая страница) -- честно останавливаемся", flush=True)
            exhausted = True
            break
        before = page[-1]["signature"]
        eligible = [h for h in page if h.get("err") is None and h["signature"] not in known_sigs]
        for h in eligible:
            tx = get_transaction(h["signature"])
            if tx is None:
                audit.append(dict(signature=h["signature"], slot=h.get("slot"), time=h.get("blockTime"),
                                   status="null_transaction"))
                continue
            row, check = classify(tx, h)
            audit.append(check)
            checked_this_run += 1
            if row:
                route, updated_meta = plan_route_for_purchase(row["mint"], tx, global_meta)
                global_meta = updated_meta
                rows.append(row)
                known_sigs.add(row["signature"])
                routes_out.append(dict(signature=row["signature"], route=route,
                                        status="planned" if route else "route_decoder_missing"))
                print(f"[select300] +selected {len(rows)}/{TARGET_TOTAL}: {row['signature'][:12]}.. "
                      f"mint={row['mint'][:10]}.. spent={row['usdc_spent']} route={'ok' if route else 'MISSING'}",
                      flush=True)

            if checked_this_run % 50 == 0:
                elapsed = time.monotonic() - started_at
                print(f"[select300] прогресс: проверено {checked_this_run} новых транзакций в этом прогоне, "
                      f"отобрано {len(rows)}/{TARGET_TOTAL}, RPC-запросов={fp.RPC_CALLS}, elapsed={elapsed:.0f}с",
                      flush=True)

            SEL_OUT.write_text(json.dumps(rows, indent=2))
            AUDIT_OUT.write_text(json.dumps(audit, indent=2))
            ROUTES_OUT.write_text(json.dumps(routes_out, indent=2))
            ROUTE_META_PATH.write_text(json.dumps(global_meta, indent=2))

            if len(rows) >= TARGET_TOTAL:
                break

            if time.monotonic() - last_commit_at >= fp.COMMIT_INTERVAL_S:
                _git_commit_progress("select300", [SEL_OUT, AUDIT_OUT, ROUTES_OUT, ROUTE_META_PATH])
                last_commit_at = time.monotonic()

        if len(page) < 1000:
            print("[select300] последняя неполная страница обработана -- история кошелька исчерпана", flush=True)
            exhausted = True

    SEL_OUT.write_text(json.dumps(rows, indent=2))
    AUDIT_OUT.write_text(json.dumps(audit, indent=2))
    ROUTES_OUT.write_text(json.dumps(routes_out, indent=2))
    ROUTE_META_PATH.write_text(json.dumps(global_meta, indent=2))

    n_zero = sum(1 for r in rows if r["zero_balance"])
    n_route_ok = sum(1 for r in routes_out if r["status"] == "planned")
    from collections import Counter
    mint_counts = Counter(r["mint"] for r in rows)
    print(json.dumps({
        "n_total_selected": len(rows),
        "n_new_this_run": len(rows) - len(existing_rows),
        "reached_target": len(rows) >= TARGET_TOTAL,
        "exhausted_wallet_history": exhausted,
        "n_zero_balance": n_zero,
        "n_add": len(rows) - n_zero,
        "n_route_ok": n_route_ok,
        "n_route_missing": len(rows) - n_route_ok,
        "top_mints": mint_counts.most_common(5),
        "time_span_seconds": (max(r["time"] for r in rows) - min(r["time"] for r in rows)) if rows else 0,
        "rpc_calls": fp.RPC_CALLS,
    }, indent=2))


if __name__ == "__main__":
    main()
