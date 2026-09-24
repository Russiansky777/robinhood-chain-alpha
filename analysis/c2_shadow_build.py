#!/usr/bin/env python3
"""C2 -> Code-1: теневая сборка покупки на сигнал buy. БЕЗ ОТПРАВКИ.

Для встраивания в детектор «в тени»: вызывать ПАРАЛЛЕЛЬНО с Bloom (в своём
потоке/задаче), не перед ним и не задерживая его. Модуль сам ничего не
отправляет: метода отправки транзакции в нём нет, только
simulateTransaction с sigVerify=false и replaceRecentBlockhash=true.
Ключи кошелька не нужны и не используются -- пользователь транзакции это
АДРЕС нашего кошелька, подпись при симуляции не проверяется.

Интерфейс (всё в одном вызове, исключений наружу не бросает):

    import c2_shadow_build as SB
    res = SB.shadow_build(
        source_tx,            # jsonParsed getTransaction источника (как у детектора)
        source_wallet,        # адрес источника
        mint,                 # купленный минт
        our_wallet,           # адрес нашего кошелька (4s87RRC2...)
        amount_lamports,      # наша трата в SOL, лампорты
        rpc_call,             # функция (method, params) -> result; учёт кредитов -- на стороне вызывающего
        sol_usd=None,         # курс USD/SOL, если детектор его уже знает (для cap_usd)
        spend_sol_equiv=None, # трата источника в SOL-экв (детектор считает её для порога 2 SOL)
        slippage=0.35, cu_units=400_000, cu_price_micro=10_000)

    res -> {"ok", "pool_program", "pool_label", "quote_mint", "supported",
            "build_ms", "sim_ms", "sim_verdict" (would_pass | slippage | balance | other_error),
            "sim_err", "sim_units", "sim_logs_tail",
            "min_out", "expected_out", "min_out_method",
            "cap_usd", "would_skip_cap", "cap_why_not", "why_not"}

Покрытие (задача D, 80.5 % + Launchlab 6 % сигналов): Pump AMM, Raydium
CPMM, Meteora DAMM v2, Raydium Launchlab, Meteora DLMM -- ОДИН шаг, только
если котировка пула WSOL. Пул в паре с другим токеном (xStock, GP и т.п.)
-> supported=False, why_not «котировка не SOL: нужен второй шаг» (в
задаче G второй шаг собран и проверен, но ему нужен свежий шаблон пула,
то есть лишние RPC-вызовы на сигнал -- в тень v1 не включён).

Минимум: Pump AMM / CPMM -- x*y=k по резервам после сделки источника с
комиссией, калиброванной на его сделке; Launchlab -- кривая на
виртуальных резервах из его TradeEvent; DAMM v2 / DLMM -- не выдаётся
(сосредоточенная ликвидность), симуляция идёт с минимумом 1, и «прошла бы»
там значит только «собрано верно и хватает баланса».

cap_usd = цена сделки источника (spend_sol_equiv / его токены) * sol_usd *
предложение минта (getAccountInfo, 1 вызов). would_skip_cap = cap_usd >
200 000. Нет курса или траты -- cap_usd None с причиной, флаг None.
"""
from __future__ import annotations

import base64
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import c2_swap_build as B  # noqa: E402

CAP_SKIP_USD = 200_000
SUPPORTED = {B.PUMP_AMM: "Pump AMM", B.CPMM: "Raydium CPMM", B.DAMM2: "Meteora DAMM v2",
             B.LAUNCHLAB: "Raydium Launchlab", B.DLMM: "Meteora DLMM"}
SIM_OPTS = {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True,
            "commitment": "processed"}


def _labels() -> dict:
    import c2_pool_programs as PP  # noqa: PLC0415
    return PP.labels()


def classify_sim(value: dict) -> dict:
    logs = value.get("logs") or []
    err = value.get("err")
    joined = "\n".join(logs).lower()
    if err is None:
        v = "would_pass"
    elif "slippage" in joined or "exceeded" in joined or "minimum" in joined and "out" in joined:
        v = "slippage"
    elif "insufficient" in joined or ("requiregteviolated" in joined and "program log: left: 0" in joined):
        v = "balance"
    else:
        v = "other_error"
    return {"sim_verdict": v, "sim_err": err, "sim_units": value.get("unitsConsumed"),
            "sim_logs_tail": logs[-6:]}


def cap_usd(source_tx: dict, source_wallet: str, mint: str, spend_sol_equiv, sol_usd,
            rpc_call) -> dict:
    if not spend_sol_equiv or not sol_usd:
        return {"cap_usd": None, "would_skip_cap": None,
                "cap_why_not": "нет траты источника в SOL-экв или курса USD/SOL"}
    tok = C.owner_mint_delta(source_tx, mint).get(source_wallet)
    if not tok or tok <= 0:
        return {"cap_usd": None, "would_skip_cap": None,
                "cap_why_not": "источник не получил токен на свой кошелёк"}
    try:
        info = rpc_call("getAccountInfo", [mint, {"encoding": "jsonParsed"}]) or {}
        p = ((((info.get("value") or {}).get("data") or {}).get("parsed") or {}).get("info") or {})
        supply = D(p["supply"]) / D(10) ** int(p["decimals"])
    except Exception as exc:  # noqa: BLE001
        return {"cap_usd": None, "would_skip_cap": None,
                "cap_why_not": f"предложение минта не получено: {type(exc).__name__}"}
    cap = D(str(spend_sol_equiv)) * D(str(sol_usd)) / tok * supply
    return {"cap_usd": float(round(cap, 2)), "would_skip_cap": cap > CAP_SKIP_USD, "cap_why_not": None}


def shadow_build(source_tx: dict, source_wallet: str, mint: str, our_wallet: str,
                 amount_lamports: int, rpc_call, *, sol_usd=None, spend_sol_equiv=None,
                 slippage: float = 0.35, cu_units: int = 400_000,
                 cu_price_micro: int = 10_000) -> dict:
    res = {"ok": False, "pool_program": None, "pool_label": None, "quote_mint": None,
           "supported": False, "build_ms": None, "sim_ms": None, "sim_verdict": None,
           "sim_err": None, "sim_units": None, "sim_logs_tail": None, "min_out": None,
           "expected_out": None, "min_out_method": None, "why_not": None}
    try:
        res.update(cap_usd(source_tx, source_wallet, mint, spend_sol_equiv, sol_usd, rpc_call))
    except Exception as exc:  # noqa: BLE001
        res.update(cap_usd=None, would_skip_cap=None, cap_why_not=f"{type(exc).__name__}")
    t0 = time.perf_counter()
    try:
        pool = C.identify_pool(source_tx, source_wallet, mint)
        if not pool["ok"]:
            res["why_not"] = f"пул: {pool['why_not']}"
            return res
        import c2_pool_programs as PP  # noqa: PLC0415
        prog = PP.pool_program(source_tx, pool["pool_vault"], _labels())["pool_program"]
        res.update(pool_program=prog, pool_label=SUPPORTED.get(prog), quote_mint=pool["quote_mint"])
        if prog not in SUPPORTED:
            res["why_not"] = f"тип пула не покрыт сборщиком: {prog}"
            return res
        tpl = B.extract_template(source_tx, prog, pool["pool_vault"])
        if not tpl.get("ok"):
            res["why_not"] = f"шаблон: {tpl.get('why_not')}"
            return res
        mv = B.mints_and_vaults(tpl, source_tx)
        if mv.get("quote_mint") != C.WSOL:
            res["why_not"] = "котировка не SOL: нужен второй шаг (в тень v1 не включён)"
            return res
        res["supported"] = True
        mo = B.min_out_from_reserves(tpl, source_tx, amount_lamports, slippage)
        if mo.get("ok"):
            res.update(min_out=mo["min_out"], expected_out=mo["expected_out"],
                       min_out_method="launchlab_virtual_reserves" if prog == B.LAUNCHLAB
                       else "xyk_reserves_after_source")
        else:
            res["min_out_method"] = f"не выдаётся: {mo.get('why_not')}"
        built = B.build_buy(tpl, source_tx, user=our_wallet, payer=our_wallet,
                            amount_in=amount_lamports, min_out=res["min_out"] or 1,
                            cu_units=cu_units, cu_price_micro=cu_price_micro, wrap_sol=True)
        res["build_ms"] = round((time.perf_counter() - t0) * 1000, 3)
        res["tx_size"] = built["size"]
    except Exception as exc:  # noqa: BLE001
        res["why_not"] = f"сборка: {type(exc).__name__}: {str(exc)[:160]}"
        return res
    t1 = time.perf_counter()
    try:
        value = (rpc_call("simulateTransaction", [built["tx_base64"], SIM_OPTS]) or {}).get("value") or {}
        res.update(classify_sim(value))
        res["ok"] = True
    except Exception as exc:  # noqa: BLE001
        res["why_not"] = f"симуляция: {type(exc).__name__}: {str(exc)[:160]}"
    res["sim_ms"] = round((time.perf_counter() - t1) * 1000, 1)
    return res


def self_test() -> int:
    checks = []

    def fake_rpc(method, params):
        if method == "getAccountInfo":
            return {"value": {"data": {"parsed": {"info": {"supply": "1000000000000000", "decimals": 6}}}}}
        if method == "simulateTransaction":
            raw = base64.b64decode(params[0])
            assert len(raw) <= 1232
            assert params[1]["sigVerify"] is False and params[1]["replaceRecentBlockhash"] is True
            return {"value": {"err": None, "logs": ["Program log: ok"], "unitsConsumed": 123456}}
        raise RuntimeError(method)
    n_ok = 0
    tried = 0
    for prog in (B.PUMP_AMM, B.LAUNCHLAB):
        for s in B.load_samples(prog):
            if s.get("quote_mint") != C.WSOL or not s.get("mint"):
                continue
            tried += 1
            r = shadow_build(s["tx"], s["source"], s["mint"], C.EXECUTOR_WALLET, 50_000_000, fake_rpc,
                             sol_usd=115.0, spend_sol_equiv=2.5)
            n_ok += bool(r["ok"] and r["sim_verdict"] == "would_pass" and r["min_out"])
            if tried == 1:
                first = r
    checks.append((f"Pump AMM/Launchlab с котировкой SOL: собрано и «симулировано» {n_ok} из {tried}",
                   tried >= 10 and n_ok == tried))
    checks.append(("время сборки измерено, мс", first.get("build_ms") is not None and first["build_ms"] < 50))
    checks.append(("cap_usd посчитан и флаг выставлен", first.get("cap_usd") is not None
                   and isinstance(first.get("would_skip_cap"), bool)))
    # пул с котировкой не SOL -- честный отказ
    cp = next(s for s in B.load_samples(B.CPMM) if s.get("quote_mint") and s["quote_mint"] != C.WSOL)
    r = shadow_build(cp["tx"], cp["source"], cp["mint"], C.EXECUTOR_WALLET, 50_000_000, fake_rpc)
    checks.append(("котировка не SOL -- supported=False с причиной, без исключения",
                   r["supported"] is False and "не SOL" in (r["why_not"] or "")))
    checks.append(("без курса -- cap_usd None с причиной", r["cap_usd"] is None and r["cap_why_not"]))
    # симуляция с ошибкой узла -- не исключение
    def bad_rpc(method, params):
        raise RuntimeError("HTTP 503")
    s = next(x for x in B.load_samples(B.PUMP_AMM) if x.get("quote_mint") == C.WSOL)
    r = shadow_build(s["tx"], s["source"], s["mint"], C.EXECUTOR_WALLET, 50_000_000, bad_rpc)
    checks.append(("сбой узла -- ok=False с причиной, исключение наружу не выходит",
                   r["ok"] is False and "симуляция" in (r["why_not"] or "")))
    checks.append(("классификатор: проскальзывание / баланс / успех",
                   classify_sim({"err": {"x": 1}, "logs": ["Program log: Error Code: ExceededSlippage"]})["sim_verdict"] == "slippage"
                   and classify_sim({"err": {"x": 1}, "logs": ["Program log: Error: insufficient funds"]})["sim_verdict"] == "balance"
                   and classify_sim({"err": None, "logs": []})["sim_verdict"] == "would_pass"))
    src = Path(__file__).read_text(encoding="utf-8")
    import re  # noqa: PLC0415
    calls = set(re.findall(r'rpc_call\(\s*"([A-Za-z]+)"', src))
    checks.append((f"модуль зовёт у узла только чтение и симуляцию: {sorted(calls)}",
                   calls <= {"getAccountInfo", "simulateTransaction"}))
    bad = 0
    for name, ok in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {name}")
        bad += (not ok)
    print(f"самопроверка c2_shadow_build: {len(checks) - bad}/{len(checks)} пройдено")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(self_test() if "--self-test" in sys.argv else 0)
