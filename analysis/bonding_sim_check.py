#!/usr/bin/env python3
"""Проверка сборки на кривой pump.fun СИМУЛЯЦИЕЙ по живым сигналам.

ЗАЧЕМ. Сборщик кривой (I.2б) проверен на настоящих транзакциях байт в байт, но
это проверка нашей арифметики. Остаётся вопрос, на который может ответить только
цепь: примет ли САМА ПРОГРАММА нашу собранную покупку. Отвечает
simulateTransaction: та же транзакция, тот же кошелёк, подпись не проверяется,
ничего не отправляется.

ВХОД -- строки решений детектора, где он отказался по типу пула
(pool_program = кривая): в каждой есть подпись сделки источника, его адрес и
минт. По ним теневой сборщик собирает нашу покупку на заданную трату и
симулирует.

Только чтение и симуляция. Ни подписи, ни отправки, ни ключа в прогоне нет.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

КРИВАЯ = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def строки_решений(путь: Path, *, предел: int) -> list:
    из_ = []
    with open(путь, encoding="utf-8", errors="replace") as ф:
        for ln in ф:
            if КРИВАЯ not in ln:
                continue
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if r.get("pool_program") != КРИВАЯ:
                continue
            if not (r.get("signature") and r.get("mint") and r.get("source")):
                continue
            из_.append({"signature": r["signature"], "mint": r["mint"],
                        "source": r["source"], "ts_utc": r.get("ts_utc")})
    # новые -- в конце файла; берём последние и убираем повторы по подписи
    видели, хвост = set(), []
    for r in reversed(из_):
        if r["signature"] in видели:
            continue
        видели.add(r["signature"])
        хвост.append(r)
        if len(хвост) >= предел:
            break
    return хвост


def форма_инструкции(tx: dict, минт: str) -> dict:
    """Как выглядит инструкция кривой у источника: дискриминатор, сколько счетов,
    сколько байт аргументов, есть ли в ней второй минт (котировка не SOL) и
    сколько программ токена. Ничего не решает -- только описывает."""
    import c2_swap_build as B  # noqa: PLC0415
    из_: dict = {"ix_disc": None, "ix_accounts": None, "ix_data_len": None,
                 "ix_args": None, "ix_token_programs": None, "ix_other_mint": None}
    лучший = None
    for ix in B.all_instructions(tx):
        if ix.get("programId") != КРИВАЯ:
            continue
        сч = ix.get("accounts") or []
        if len(сч) <= 1:          # событие Anchor -- не инструкция сделки
            continue
        if лучший is None or len(сч) > len(лучший.get("accounts") or []):
            лучший = ix
    if лучший is None:
        из_["ix_disc"] = "инструкции кривой в сделке нет"
        return из_
    данные = B.b58decode(лучший["data"])
    сч = лучший["accounts"]
    программы = [a for a in сч if a in (B.TOKEN_PROGRAM,
                                        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")]
    # "другой минт" -- счёт из инструкции, который в балансах транзакции
    # встречается как МИНТ и не равен нашему: признак котировки не в SOL.
    минты = {b.get("mint") for side in ("preTokenBalances", "postTokenBalances")
             for b in ((tx.get("meta") or {}).get(side) or [])}
    другой = [a for a in сч if a in минты and a != минт]
    из_.update(ix_disc=данные[:8].hex(), ix_accounts=len(сч), ix_data_len=len(данные),
               ix_token_programs=len(программы),
               ix_other_mint=(другой[0] if другой else None))
    if len(данные) >= 24:
        import struct as _s  # noqa: PLC0415
        из_["ix_args"] = list(_s.unpack("<QQ", данные[8:24]))
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--decisions", default=str(КОРЕНЬ / "data" / "triton" / "decisions_tail.jsonl"))
    р.add_argument("--limit", type=int, default=10)
    р.add_argument("--sol", type=float, default=0.01)
    р.add_argument("--wallet", default="")
    р.add_argument("--out", default=str(КОРЕНЬ / "data" / "bonding_sim_check.json"))
    а = р.parse_args()

    import c2_common as C  # noqa: PLC0415
    import c2_shadow_build as SB  # noqa: PLC0415
    from solana_crowd_scan import Rpc, helius_key  # noqa: PLC0415

    ряды = строки_решений(Path(а.decisions), предел=а.limit)
    кошелёк = а.wallet or C.EXECUTOR_WALLET
    ключ, имя = helius_key()
    rpc = Rpc(ключ, service="bonding_sim")
    print(f"ключ Helius из {имя}; сигналов кривой к проверке: {len(ряды)}; "
          f"кошелёк {кошелёк}; трата {а.sol} SOL")
    лампорты = int(round(а.sol * 1_000_000_000))

    итоги, начало = [], time.time()
    for и, r in enumerate(ряды, 1):
        строка = dict(r)
        try:
            tx = rpc.call("getTransaction", [r["signature"], {
                "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                "commitment": "confirmed"}])
        except Exception as exc:  # noqa: BLE001
            строка["why_not"] = f"узел не отдал сделку источника: {type(exc).__name__}"
            итоги.append(строка)
            continue
        if not tx:
            строка["why_not"] = "узел не отдал сделку источника"
            итоги.append(строка)
            continue
        # ФОРМА ИНСТРУКЦИИ -- ВСЕГДА, даже когда сборка откажется. Иначе отказ
        # "у источника инструкция <hex>, не buy" говорит только о том, что мы
        # её не знаем, и ничего о том, что это за инструкция.
        строка.update(форма_инструкции(tx, r["mint"]))
        res = SB.shadow_build(tx, r["source"], r["mint"], кошелёк, лампорты, rpc.call,
                              slippage=0.35)
        строка.update({к: res.get(к) for к in (
            "ok", "pool_label", "quote_mint", "supported", "route", "min_out",
            "expected_out", "min_out_method", "sol_to_curve", "max_sol_cost",
            "sim_verdict", "sim_err", "sim_units", "build_ms", "sim_ms", "why_not")})
        итоги.append(строка)
        print(f"  {и}/{len(ряды)} {r['mint'][:8]} verdict={строка.get('sim_verdict')} "
              f"min_out={строка.get('min_out')} why={строка.get('why_not')}")

    по_инструкции: dict = {}
    for с in итоги:
        д = с.get("ix_disc") or "нет данных"
        б = по_инструкции.setdefault(д, {"сигналов": 0, "счетов": set(), "байт": set(),
                                          "программ_токена": set(), "котировка_не_sol": 0})
        б["сигналов"] += 1
        б["счетов"].add(с.get("ix_accounts"))
        б["байт"].add(с.get("ix_data_len"))
        б["программ_токена"].add(с.get("ix_token_programs"))
        б["котировка_не_sol"] += bool(с.get("ix_other_mint"))
    по_инструкции = {к: {"сигналов": v["сигналов"],
                         "счетов": sorted(x for x in v["счетов"] if x is not None),
                         "байт": sorted(x for x in v["байт"] if x is not None),
                         "программ_токена": sorted(x for x in v["программ_токена"] if x is not None),
                         "котировка_не_sol": v["котировка_не_sol"]}
                     for к, v in sorted(по_инструкции.items(), key=lambda x: -x[1]["сигналов"])}
    по_вердикту: dict = {}
    for с in итоги:
        к = с.get("sim_verdict") or (с.get("why_not") or "нет ответа")
        по_вердикту[к] = по_вердикту.get(к, 0) + 1
    свод = {
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "кошелёк": кошелёк, "трата_sol": а.sol,
        "сигналов": len(итоги), "вызовов_rpc": rpc.calls,
        "секунд": round(time.time() - начало, 1),
        "по_вердикту": dict(sorted(по_вердикту.items(), key=lambda x: -x[1])),
        "по_инструкции": по_инструкции,
        "прошло_бы": sum(1 for с in итоги if с.get("sim_verdict") == "would_pass"),
        "ряды": итоги,
    }
    Path(а.out).write_text(json.dumps(свод, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({к: v for к, v in свод.items() if к != "ряды"}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
