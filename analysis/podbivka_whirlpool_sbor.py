#!/usr/bin/env python3
"""Сбор для Orca Whirlpool (whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc): только чтение, Helius. Как podbivka_clmm_sbor.py.

1. Сделки источников в Whirlpool за окно: инструкции swap / swap_v2 / two_hop_swap / two_hop_swap_v2 (дискриминаторы
   Anchor), аргументы, счета по ролям (порядок структур Swap / SwapV2 из исходников orca-so/whirlpools), остаток
   (remaining accounts), событие Traded из логов.
2. Версия программы: слот последнего развёртывания (programData).
3. Снимки: пул, oracle (адаптивная комиссия) и tick arrays ±--massivov (фиксированные и динамические -- одним
   getMultipleAccounts, слот S), затем первая успешная транзакция пула со слотом > S; данные счетов base64 и
   транзакция целиком -- для офлайн-сверки порта.
Выход: data/podbivka/whirlpool_sbor.json.
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
ПРОГРАММА = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
TICK_ARRAY_SIZE = 88
ВИДЫ = {SB._disc(v): v for v in ("swap", "swap_v2", "two_hop_swap", "two_hop_swap_v2")}  # noqa: SLF001
СОБЫТИЕ = SB._disc("Traded", "event")  # noqa: SLF001
РОЛИ = {"swap": ["token_program", "token_authority", "whirlpool", "token_owner_account_a", "token_vault_a",
                 "token_owner_account_b", "token_vault_b", "tick_array_0", "tick_array_1", "tick_array_2", "oracle"],
        "swap_v2": ["token_program_a", "token_program_b", "memo_program", "token_authority", "whirlpool", "token_mint_a",
                    "token_mint_b", "token_owner_account_a", "token_vault_a", "token_owner_account_b", "token_vault_b",
                    "tick_array_0", "tick_array_1", "tick_array_2", "oracle"]}


def разобрать_ix(ix: dict) -> dict | None:
    if not isinstance(ix, dict) or ix.get("programId") != ПРОГРАММА or not ix.get("data"):
        return None
    try:
        d = SB._b58d(ix["data"])  # noqa: SLF001
    except ValueError:
        return None
    вид = ВИДЫ.get(d[:8])
    if not вид:
        return {"вид": "другая", "disc": d[:8].hex()}
    сч = list(ix.get("accounts") or [])
    из_ = {"вид": вид, "счетов": len(сч)}
    if вид in РОЛИ and len(d) >= 8 + 8 + 8 + 16 + 1 + 1:
        amount, other, lo, hi = struct.unpack_from("<QQQQ", d, 8)
        из_.update(amount=amount, other_amount_threshold=other, sqrt_price_limit=lo | (hi << 64),
                   amount_specified_is_input=d[40] != 0, a_to_b=d[41] != 0)
        из_["роли"] = {р: сч[i] for i, р in enumerate(РОЛИ[вид]) if i < len(сч)}
        из_["остаток"] = сч[len(РОЛИ[вид]):]
    else:
        из_["счета"] = сч
    return из_


def события(tx: dict) -> list:
    из_ = []
    for стр in ((tx.get("meta") or {}).get("logMessages") or []):
        if not стр.startswith("Program data: "):
            continue
        try:
            b = base64.b64decode(стр[len("Program data: "):])
        except ValueError:
            continue
        if b[:8] != СОБЫТИЕ or len(b) < 8 + 32 + 1 + 32 + 48:
            continue
        d = b[8:]
        e = {"длина": len(d), "whirlpool": SB._b58e(d[0:32]), "a_to_b": d[32] != 0}  # noqa: SLF001
        lo, hi = struct.unpack_from("<QQ", d, 33)
        e["pre_sqrt_price"] = lo | (hi << 64)
        lo, hi = struct.unpack_from("<QQ", d, 49)
        e["post_sqrt_price"] = lo | (hi << 64)
        (e["input_amount"], e["output_amount"], e["input_transfer_fee"], e["output_transfer_fee"], e["lp_fee"],
         e["protocol_fee"]) = struct.unpack_from("<6Q", d, 65)
        из_.append(e)
    return из_


def пул_поля(data: bytes) -> dict:
    return {"tick_spacing": struct.unpack_from("<H", data, 41)[0], "tick_current": struct.unpack_from("<i", data, 81)[0]}


def старт_массива(tick: int, spacing: int) -> int:
    n = TICK_ARRAY_SIZE * spacing
    return (tick // n) * n


def адрес_массива(пул: str, старт: int) -> str:
    from solders.pubkey import Pubkey  # noqa: PLC0415
    pda, _ = Pubkey.find_program_address([b"tick_array", bytes(Pubkey.from_string(пул)), str(старт).encode()],
                                         Pubkey.from_string(ПРОГРАММА))
    return str(pda)


def адрес_оракула(пул: str) -> str:
    from solders.pubkey import Pubkey  # noqa: PLC0415
    pda, _ = Pubkey.find_program_address([b"oracle", bytes(Pubkey.from_string(пул))], Pubkey.from_string(ПРОГРАММА))
    return str(pda)


def снимок(уз, пул: str, массивов: int) -> dict:
    r = уз.вызов("getAccountInfo", [пул, {"encoding": "base64", "commitment": "confirmed"}])
    v = (r or {}).get("value")
    if not v:
        raise RuntimeError("пул не читается")
    п = пул_поля(base64.b64decode(v["data"][0]))
    ст0 = старт_массива(п["tick_current"], п["tick_spacing"])
    шаг = TICK_ARRAY_SIZE * п["tick_spacing"]
    старты = [ст0 + k * шаг for k in range(-массивов, массивов + 1)]
    адр = [пул, адрес_оракула(пул)] + [адрес_массива(пул, s) for s in старты]
    r2 = уз.вызов("getMultipleAccounts", [адр, {"encoding": "base64", "commitment": "confirmed"}])
    val = (r2 or {}).get("value") or []
    return {"slot": ((r2 or {}).get("context") or {}).get("slot"), "пул": пул, "оракул": адр[1],
            "массивы": dict(zip(map(str, старты), адр[2:])),
            "данные": {a: (x["data"][0] if x else None) for a, x in zip(адр, val)}}


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--s", default="2026-09-28T06:00:00Z")
    р.add_argument("--podpisey", type=int, default=1500)
    р.add_argument("--cel", type=int, default=40)
    р.add_argument("--massivov", type=int, default=3)
    р.add_argument("--minut", type=float, default=80)
    а = р.parse_args()
    t0 = calendar.timegm(time.strptime(а.s, "%Y-%m-%dT%H:%M:%SZ"))
    сп = json.loads((КОРЕНЬ / "data" / "podbivka" / "istochniki_code1_kandidaty.json").read_text(encoding="utf-8"))
    кошельки = sorted(set(сп["группы_code1"]) | set(сп["кандидаты"]) | set(сп.get("снайперы") or []))
    уз = S.Узел()
    out = КОРЕНЬ / "data" / "podbivka" / "whirlpool_sbor.json"
    import podbivka_run as R  # noqa: PLC0415
    итог: dict = {"окно_с": а.s, "кошельков": len(кошельки), "покрытие": {}, "сделки": [], "версия": {},
                  "снимки": [], "отсев": {}}

    def записать():
        итог["расход"] = уз.расход()
        out.write_text(json.dumps(итог, ensure_ascii=False), encoding="utf-8")
        R.записано(out)
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
                        p = (x.get("роли") or {}).get("whirlpool")
                        if p:
                            пулы[p] = пулы.get(p, 0) + 1
            print(w[:8], "подписей", len(сп_w), "сделок Whirlpool", len(итог["сделки"]), flush=True)
        итог["пулы_источников"] = пулы
        записать()
        очередь = sorted(пулы, key=lambda p: -пулы[p])
        конец = time.time() + а.minut * 60
        k = 0
        while очередь and time.time() < конец and len(итог["снимки"]) < а.cel:
            пул = очередь[k % len(очередь)]
            k += 1
            try:
                сн = снимок(уз, пул, а.massivov)
            except RuntimeError as exc:
                ключ = f"снимок: {S.чисто(str(exc))[:60]}"
                итог["отсев"][ключ] = итог["отсев"].get(ключ, 0) + 1
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
                итог["отсев"]["нет успешной транзакции за 60 с"] = итог["отсев"].get("нет успешной транзакции за 60 с", 0) + 1
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
    R.пуш("Podbivka-2: Whirlpool sbor -- sdelki istochnikov, versiya, snimki [automated]", [str(out)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
