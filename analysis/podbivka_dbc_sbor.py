#!/usr/bin/env python3
"""Сбор для Meteora Dynamic Bonding Curve (dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN): только чтение, Helius.

Как podbivka_clmm_sbor.py / podbivka_whirlpool_sbor.py:
1. Сделки источников в DBC за окно: инструкции swap / swap2 / swap2_with_transfer_hook (дискриминаторы Anchor), аргументы,
   счета по ролям (порядок SwapCtx из исходников MeteoraAg/dynamic-bonding-curve, instructions/swap/ix_swap.rs: pool_authority,
   config, pool, input_token_account, output_token_account, base_vault, quote_vault, base_mint, quote_mint, payer,
   token_base_program, token_quote_program, referral_token_account, + event_cpi: event_authority, program), события
   EvtSwap2 (emit_cpi -- внутренняя инструкция программы с тегом события).
2. Версия программы: слот последнего развёртывания.
3. Снимки: пул и его config одним getMultipleAccounts (слот S), затем первая успешная транзакция пула со слотом > S;
   данные счетов base64 и транзакция целиком -- для офлайн-сверки порта.
Выход: data/podbivka/dbc_sbor.json.
"""
from __future__ import annotations

import argparse
import calendar
import json
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_clmm_sbor as SB  # noqa: E402
import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
ПРОГРАММА = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
ВИДЫ = {SB._disc(v): v for v in ("swap", "swap2", "swap2_with_transfer_hook")}  # noqa: SLF001
EVENT_IX_TAG = bytes.fromhex("e445a52e51cb9a1d")
EVT_SWAP2 = SB._disc("EvtSwap2", "event")  # noqa: SLF001
EVT_SWAP2_TH = SB._disc("EvtSwap2WithTransferHook", "event")  # noqa: SLF001
РОЛИ = ["pool_authority", "config", "pool", "input_token_account", "output_token_account", "base_vault", "quote_vault",
        "base_mint", "quote_mint", "payer", "token_base_program", "token_quote_program", "referral_token_account",
        "event_authority", "program"]


def разобрать_ix(ix: dict) -> dict | None:
    if not isinstance(ix, dict) or ix.get("programId") != ПРОГРАММА or not ix.get("data"):
        return None
    try:
        d = SB._b58d(ix["data"])  # noqa: SLF001
    except ValueError:
        return None
    вид = ВИДЫ.get(d[:8])
    if not вид:
        return None                                   # события emit_cpi и прочие инструкции
    сч = list(ix.get("accounts") or [])
    из_ = {"вид": вид, "счетов": len(сч), "роли": {р: сч[i] for i, р in enumerate(РОЛИ) if i < len(сч)},
           "остаток": сч[len(РОЛИ):]}
    if вид == "swap" and len(d) >= 24:
        из_["amount_0"], из_["amount_1"] = struct.unpack_from("<QQ", d, 8)
        из_["swap_mode"] = 0
    elif len(d) >= 25:
        из_["amount_0"], из_["amount_1"] = struct.unpack_from("<QQ", d, 8)
        из_["swap_mode"] = d[24]
    return из_


def события(tx: dict) -> list:
    """EvtSwap2 / EvtSwap2WithTransferHook из внутренних инструкций программы (emit_cpi)."""
    из_ = []
    for _, ix in SB.инструкции(tx):
        if not isinstance(ix, dict) or ix.get("programId") != ПРОГРАММА or not ix.get("data"):
            continue
        try:
            b = SB._b58d(ix["data"])  # noqa: SLF001
        except ValueError:
            continue
        if b[:8] != EVENT_IX_TAG or b[8:16] not in (EVT_SWAP2, EVT_SWAP2_TH):
            continue
        d = b[16:]
        if len(d) < 32 + 32 + 1 + 1 + 17 + 64 + 24:
            continue
        e = {"длина": len(d), "pool": SB._b58e(d[0:32]), "config": SB._b58e(d[32:64]),  # noqa: SLF001
             "trade_direction": d[64], "has_referral": d[65] != 0}
        e["amount_0"], e["amount_1"] = struct.unpack_from("<QQ", d, 66)
        e["swap_mode"] = d[82]
        (e["included_fee_input_amount"], e["excluded_fee_input_amount"], e["amount_left"],
         e["output_amount"]) = struct.unpack_from("<QQQQ", d, 83)
        lo, hi = struct.unpack_from("<QQ", d, 115)
        e["next_sqrt_price"] = lo | (hi << 64)
        e["trading_fee"], e["protocol_fee"], e["referral_fee"] = struct.unpack_from("<QQQ", d, 131)
        e["quote_reserve_amount"], e["migration_threshold"], e["current_timestamp"] = struct.unpack_from("<QQQ", d, 155)
        из_.append(e)
    return из_


def снимок(уз, пул: str, config: str) -> dict:
    r = уз.вызов("getMultipleAccounts", [[пул, config], {"encoding": "base64", "commitment": "confirmed"}])
    val = (r or {}).get("value") or []
    if not val or not val[0]:
        raise RuntimeError("пул не читается")
    return {"slot": ((r or {}).get("context") or {}).get("slot"), "пул": пул, "config": config,
            "данные": {a: (x["data"][0] if x else None) for a, x in zip([пул, config], val)}}


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--s", default="2026-09-28T06:00:00Z")
    р.add_argument("--podpisey", type=int, default=1500)
    р.add_argument("--cel", type=int, default=40)
    р.add_argument("--minut", type=float, default=80)
    а = р.parse_args()
    t0 = calendar.timegm(time.strptime(а.s, "%Y-%m-%dT%H:%M:%SZ"))
    сп = json.loads((КОРЕНЬ / "data" / "podbivka" / "istochniki_code1_kandidaty.json").read_text(encoding="utf-8"))
    кошельки = sorted(set(сп["группы_code1"]) | set(сп["кандидаты"]) | set(сп.get("снайперы") or []))
    уз = S.Узел()
    out = КОРЕНЬ / "data" / "podbivka" / "dbc_sbor.json"
    import podbivka_run as R  # noqa: PLC0415
    итог: dict = {"окно_с": а.s, "кошельков": len(кошельки), "покрытие": {}, "сделки": [], "версия": {}, "снимки": [], "отсев": {}}

    def записать():
        итог["расход"] = уз.расход()
        out.write_text(json.dumps(итог, ensure_ascii=False), encoding="utf-8")
        R.записано(out)

    def отсев(к):
        итог["отсев"][к] = итог["отсев"].get(к, 0) + 1
    with уз.на("helius"):
        try:
            pr = уз.вызов("getAccountInfo", [ПРОГРАММА, {"encoding": "jsonParsed"}])
            pd = ((((pr or {}).get("value") or {}).get("data") or {}).get("parsed") or {}).get("info", {}).get("programData")
            pdi = уз.вызов("getAccountInfo", [pd, {"encoding": "jsonParsed", "dataSlice": {"offset": 0, "length": 0}}]) if pd else None
            info = ((((pdi or {}).get("value") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            итог["версия"] = {"programData": pd, "слот_развёртывания": info.get("slot")}
            if info.get("slot"):
                бт = уз.вызов("getBlockTime", [info["slot"]])
                итог["версия"]["время_развёртывания_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(бт)) if бт else None
        except RuntimeError as exc:
            итог["версия"] = {"why_not": S.чисто(str(exc))[:160]}
        пулы: dict = {}
        конфиг: dict = {}
        for w in кошельки:
            сп_w, до = [], None
            while len(сп_w) < а.podpisey:
                try:
                    стр = уз.подписи(w, до=до, limit=1000)
                except RuntimeError:
                    break
                if not стр:
                    break
                сп_w += [з for з in стр if (з.get("blockTime") or 0) >= t0 and з.get("err") is None]
                if (стр[-1].get("blockTime") or 0) < t0 or len(стр) < 1000:
                    break
                до = стр[-1]["signature"]
            итог["покрытие"][w] = {"подписей": len(сп_w), "упёрлись_в_предел": len(сп_w) >= а.podpisey}
            for и in range(0, len(сп_w), 100):
                txs = уз.пакет([з["signature"] for з in сп_w[и:и + 100]])
                for s_, т in txs.items():
                    if not т:
                        continue
                    ixs = [(м, разобрать_ix(ix)) for м, ix in SB.инструкции(т)]
                    ixs = [(м, x) for м, x in ixs if x]
                    if not ixs:
                        continue
                    итог["сделки"].append({"кошелёк": w, "signature": s_, "slot": т.get("slot"), "blockTime": т.get("blockTime"),
                                           "alt": bool(((т.get("transaction") or {}).get("message") or {}).get("addressTableLookups")),
                                           "инструкции": [{"место": м, **x} for м, x in ixs], "события": события(т)})
                    for _, x in ixs:
                        p = x["роли"].get("pool")
                        if p:
                            пулы[p] = пулы.get(p, 0) + 1
                            конфиг[p] = x["роли"].get("config")
            print(w[:8], "подписей", len(сп_w), "сделок DBC", len(итог["сделки"]), flush=True)
        итог["пулы_источников"] = пулы
        записать()
        очередь = sorted(пулы, key=lambda p: -пулы[p])
        конец = time.time() + а.minut * 60
        k = 0
        while очередь and time.time() < конец and len(итог["снимки"]) < а.cel:
            пул = очередь[k % len(очередь)]
            k += 1
            try:
                сн = снимок(уз, пул, конфиг[пул])
            except RuntimeError as exc:
                отсев(f"снимок: {S.чисто(str(exc))[:60]}")
                очередь.remove(пул)
                continue
            след = None
            for _ in range(20):
                time.sleep(3)
                зз = [з for з in уз.подписи(пул, limit=50) if (з.get("slot") or 0) > (сн["slot"] or 0) and з.get("err") is None]
                if зз:
                    след = min(зз, key=lambda з: з["slot"])
                    break
            if not след:
                отсев("нет успешной транзакции за 60 с")
                continue
            т = уз.tx(след["signature"])
            if not т:
                continue
            в_слоте = [з for з in уз.подписи(пул, limit=50) if з.get("slot") == след["slot"] and з.get("err") is None]
            сн.update(сделка=след["signature"], slot_сделки=т.get("slot"), транзакция=т, успешных_в_слоте_сделки=len(в_слоте))
            итог["снимки"].append(сн)
            записать()
            print("снимок", пул[:8], len(итог["снимки"]), flush=True)
    записать()
    R.пуш("Podbivka-2: DBC sbor -- sdelki istochnikov, versiya, snimki [automated]", [str(out)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
