#!/usr/bin/env python3
"""Сбор для Raydium AMM v4 (675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8): только чтение, Helius.

Как podbivka_clmm_sbor.py / podbivka_dbc_sbor.py:
1. Сделки источников в AMM v4 за окно (--s … --po): подписи кошельков источников (группы Code-1, кандидаты, снайперы --
   data/podbivka/istochniki_code1_kandidaty.json), транзакции с инструкцией программы (внешней или внутренней, через
   агрегатор), успешные и с ошибкой -- у каждой час суток UTC и исход. Инструкции -- по исходникам raydium-io/raydium-amm
   (program/src/instruction.rs): первый байт -- номер варианта AmmInstruction; SwapBaseIn = 9 и SwapBaseOut = 11
   (17 счетов без target_orders или 18 с ним), SwapBaseInV2 = 16 и SwapBaseOutV2 = 17 (8 счетов, без книги заявок),
   аргументы u64 + u64. Событие -- «Program log: ray_log: <base64>» (program/src/log.rs: SwapBaseInLog / SwapBaseOutLog;
   direction 1 -- вход pc, выход coin; 2 -- вход coin, выход pc).
2. Версия программы: слот последнего развёртывания.
3. Снимки (--cel): пул (AmmInfo, 752 байта) и два его хранилища одним getMultipleAccounts (слот S), затем первая успешная
   транзакция пула со слотом > S -- для офлайн-сверки котировки.
Выход: data/podbivka/amm4_sbor.json.
"""
from __future__ import annotations

import argparse
import base64
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
ПРОГРАММА = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
ВИДЫ = {9: "swap_base_in", 11: "swap_base_out", 16: "swap_base_in_v2", 17: "swap_base_out_v2"}
_КНИГА = ["market_program", "market", "market_bids", "market_asks", "market_event_queue", "market_coin_vault",
          "market_pc_vault", "market_vault_signer", "user_source", "user_destination", "user_owner"]
РОЛИ = {17: ["token_program", "amm", "amm_authority", "amm_open_orders", "amm_coin_vault", "amm_pc_vault"] + _КНИГА,
        18: ["token_program", "amm", "amm_authority", "amm_open_orders", "amm_target_orders", "amm_coin_vault",
             "amm_pc_vault"] + _КНИГА,
        8: ["token_program", "amm", "amm_authority", "amm_coin_vault", "amm_pc_vault", "user_source", "user_destination",
            "user_owner"]}
# AmmInfo (program/src/state.rs), смещения полей
ПОЛЯ_ПУЛА = {"status": 0, "coin_decimals": 32, "pc_decimals": 40, "trade_fee_numerator": 144,
             "trade_fee_denominator": 152, "swap_fee_numerator": 176, "swap_fee_denominator": 184,
             "need_take_pnl_coin": 192, "need_take_pnl_pc": 200, "pool_open_time": 224}
КЛЮЧИ_ПУЛА = {"coin_vault": 336, "pc_vault": 368, "coin_vault_mint": 400, "pc_vault_mint": 432, "open_orders": 496,
              "market": 528, "market_program": 560}


def пул_поля(data: bytes) -> dict:
    из_ = {к: struct.unpack_from("<Q", data, o)[0] for к, o in ПОЛЯ_ПУЛА.items()}
    из_.update({к: SB._b58e(data[o:o + 32]) for к, o in КЛЮЧИ_ПУЛА.items()})  # noqa: SLF001
    из_["длина"] = len(data)
    return из_


def разобрать_ix(ix: dict) -> dict | None:
    if not isinstance(ix, dict) or ix.get("programId") != ПРОГРАММА or not ix.get("data"):
        return None
    try:
        d = SB._b58d(ix["data"])  # noqa: SLF001
    except ValueError:
        return None
    if not d:
        return None
    вид = ВИДЫ.get(d[0])
    сч = list(ix.get("accounts") or [])
    if not вид:
        return {"вид": "другая", "тег": d[0], "длина_данных": len(d), "счетов": len(сч)}
    из_ = {"вид": вид, "тег": d[0], "длина_данных": len(d), "счетов": len(сч)}
    if len(d) >= 17:
        а, б = struct.unpack_from("<QQ", d, 1)
        из_.update({"amount_in": а, "minimum_amount_out": б} if вид.startswith("swap_base_in")
                   else {"max_amount_in": а, "amount_out": б})
    роли = РОЛИ.get(len(сч))
    if роли:
        из_["роли"] = {р: сч[i] for i, р in enumerate(роли)}
    else:
        из_["счета"] = сч
    return из_


def события(tx: dict) -> list:
    """ray_log свопа: SwapBaseInLog (log_type 3) / SwapBaseOutLog (4), в порядке логов."""
    из_ = []
    for стр in ((tx.get("meta") or {}).get("logMessages") or []):
        if not стр.startswith("Program log: ray_log: "):
            continue
        try:
            b = base64.b64decode(стр[len("Program log: ray_log: "):])
        except ValueError:
            continue
        if len(b) >= 57 and b[0] == 3:
            к = ("amount_in", "minimum_out", "direction", "user_source", "pool_coin", "pool_pc", "out_amount")
            из_.append({"log_type": 3, **dict(zip(к, struct.unpack_from("<7Q", b, 1)))})
        elif len(b) >= 57 and b[0] == 4:
            к = ("max_in", "amount_out", "direction", "user_source", "pool_coin", "pool_pc", "deduct_in")
            из_.append({"log_type": 4, **dict(zip(к, struct.unpack_from("<7Q", b, 1)))})
    return из_


def исход(tx: dict) -> str:
    err = (tx.get("meta") or {}).get("err")
    return "успех" if err is None else S.чисто(json.dumps(err, ensure_ascii=False))[:120]


def снимок(уз, пул: str, vaults: list) -> dict:
    адреса = [пул] + vaults
    r = уз.вызов("getMultipleAccounts", [адреса, {"encoding": "base64", "commitment": "confirmed"}])
    val = (r or {}).get("value") or []
    if not val or not val[0]:
        raise RuntimeError("пул не читается")
    return {"slot": ((r or {}).get("context") or {}).get("slot"), "пул": пул, "хранилища": vaults,
            "данные": {a: (x["data"][0] if x else None) for a, x in zip(адреса, val)}}


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--s", default="2026-09-28T00:00:00Z")
    р.add_argument("--po", default="2026-09-30T00:00:00Z")
    р.add_argument("--podpisey", type=int, default=3000)
    р.add_argument("--cel", type=int, default=30)
    р.add_argument("--minut", type=float, default=60)
    а = р.parse_args()
    t0 = calendar.timegm(time.strptime(а.s, "%Y-%m-%dT%H:%M:%SZ"))
    t1 = calendar.timegm(time.strptime(а.po, "%Y-%m-%dT%H:%M:%SZ"))
    сп = json.loads((КОРЕНЬ / "data" / "podbivka" / "istochniki_code1_kandidaty.json").read_text(encoding="utf-8"))
    кошельки = sorted(set(сп["группы_code1"]) | set(сп["кандидаты"]) | set(сп.get("снайперы") or []))
    уз = S.Узел()
    out = КОРЕНЬ / "data" / "podbivka" / "amm4_sbor.json"
    import podbivka_run as R  # noqa: PLC0415
    итог: dict = {"окно": [а.s, а.po], "кошельков": len(кошельки), "покрытие": {}, "сделки": [], "версия": {},
                  "снимки": [], "отсев": {}}

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
            итог["версия"] = {"programData": pd, "слот_развёртывания": info.get("slot"), "upgrade_authority": info.get("authority")}
            if info.get("slot"):
                бт = уз.вызов("getBlockTime", [info["slot"]])
                итог["версия"]["время_развёртывания_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(бт)) if бт else None
        except RuntimeError as exc:
            итог["версия"] = {"why_not": S.чисто(str(exc))[:160]}
        пулы: dict = {}
        хран: dict = {}
        for w in кошельки:
            сп_w, до = [], None
            while len(сп_w) < а.podpisey:
                try:
                    стр = уз.подписи(w, до=до, limit=1000)
                except RuntimeError:
                    break
                if not стр:
                    break
                сп_w += [з for з in стр if t0 <= (з.get("blockTime") or 0) < t1]
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
                    bt = т.get("blockTime")
                    итог["сделки"].append({"кошелёк": w, "signature": s_, "slot": т.get("slot"), "blockTime": bt,
                                           "час_utc": time.gmtime(bt).tm_hour if bt else None, "исход": исход(т),
                                           "alt": bool(((т.get("transaction") or {}).get("message") or {}).get("addressTableLookups")),
                                           "cu": (т.get("meta") or {}).get("computeUnitsConsumed"),
                                           "инструкции": [{"место": м, **x} for м, x in ixs], "события": события(т)})
                    if (т.get("meta") or {}).get("err") is not None:
                        continue
                    for _, x in ixs:
                        р_ = x.get("роли") or {}
                        if р_.get("amm"):
                            пулы[р_["amm"]] = пулы.get(р_["amm"], 0) + 1
                            хран[р_["amm"]] = [р_.get("amm_coin_vault"), р_.get("amm_pc_vault")]
            print(w[:8], "подписей", len(сп_w), "сделок AMM v4", len(итог["сделки"]), flush=True)
        итог["пулы_источников"] = пулы
        записать()
        очередь = sorted(пулы, key=lambda p: -пулы[p])
        конец = time.time() + а.minut * 60
        k = 0
        while очередь and time.time() < конец and len(итог["снимки"]) < а.cel:
            пул = очередь[k % len(очередь)]
            k += 1
            try:
                сн = снимок(уз, пул, хран[пул])
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
                очередь.remove(пул)
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
    R.пуш("Podbivka-2: AMM v4 sbor -- sdelki istochnikov, versiya, snimki [automated]", [str(out)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
