#!/usr/bin/env python3
"""Подбивка: котировочные токены и пулы первой ноги SOL<->котировка для не-SOL
покупок 23 торгующих + кандидатов и лидера за 7 дней -- файл для Code-1
data/podbivka/kotirovki_grupp.json.

Офлайн (по умолчанию): покупки и котировка -- data/podbivka/rezhim2/*.jsonl и
списки покупок rezhim2_spisok*.json (время), группы -- копия конфигурации Code-1
(data/podbivka/gruppy_code1.json из data/sources_2026-09-25.json ветки claude/nifty-sagan-r0polg).
Частота котировок по кошельку и по программе пула токена.

--cep (облако, только чтение): по каждой котировке до --na-kotirovku сделок
источника -- плечи котировки к WSOL и к USDC/USDT в той же сделке (инструкция DEX,
где счёт q и счёт WSOL/USD не подписантов двигаются навстречу): программа
инструкции, хранилища обеих сторон, их владелец (authority пула); символ и имя --
DAS getAsset (Helius). Результат -- data/podbivka/kotirovki_nogi.json; офлайн-
сборка подхватывает его, если он есть.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
ЛИДЕР = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
КАНДИДАТЫ = {"BMgsHTvc": "DipWheeler", "CzU8MaRc": "Rowdy", "2L17Sw85": "User2L17", "5VRgqb2q": "figaro",
             "GZi5tmvZ": "GZi5tmvZ", "7txcAXw9": "7txcAXw9"}
SOL = ("So11111111111111111111111111111111111111112", "native_sol")
ПРОГ = {"CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
        "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "Pump AMM",
        "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "Raydium LaunchLab",
        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
        "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
        "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
        "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "Meteora DAMM v2",
        "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
        "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "Meteora DBC",
        "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump.fun кривая"}


def группы() -> dict:
    конф = json.loads((КОРЕНЬ / "data" / "podbivka" / "gruppy_code1.json").read_text(encoding="utf-8"))
    из_ = {}
    for гр in ("leader", "batch5", "lane_s0"):
        for а, x in конф["groups"][гр]["addresses"].items():
            из_[а] = {"группа": гр, "имя": x.get("name") or а[:8]}
    адреса = {x["address"] for x in json.loads((КОРЕНЬ / "data" / "podbivka" / "istochniki.json")
                                               .read_text(encoding="utf-8"))["источники"]}
    for пр, имя in КАНДИДАТЫ.items():
        а = next((a for a in адреса if a.startswith(пр)), None)
        if а and а not in из_:
            из_[а] = {"группа": "кандидат", "имя": имя}
    return из_


def покупки() -> list:
    """Лучшая строка режима 2 на подпись + время из списков."""
    вр = {}
    for f in glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "rezhim2_spisok*.json")):
        for x in json.loads(Path(f).read_text(encoding="utf-8"))["покупки"]:
            if x.get("blockTime"):
                вр[x["signature"]] = (x["blockTime"], x.get("вид"))
    по: dict = {}
    for f in sorted(glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "rezhim2" / "*.jsonl"))):
        if "proba" in f:
            continue
        for l in open(f, encoding="utf-8"):
            р = json.loads(l)
            п = по.get(р["signature"])
            if п is None or (п.get("why_not") and not р.get("why_not")) or (not п.get("quote_mint") and р.get("quote_mint")):
                по[р["signature"]] = р
    из_ = []
    for s, р in по.items():
        q = р.get("quote_mint")
        if not q or q in SOL:
            continue
        bt, вид = вр.get(s, (None, None))
        из_.append({"signature": s, "wallet": р.get("wallet"), "mint": р.get("mint"), "quote_mint": q,
                    "program": р.get("program"), "blockTime": bt, "вид": вид or р.get("вид"),
                    "с_числом": not р.get("why_not")})
    return из_


def сборка() -> int:
    гр = группы()
    пп = покупки()
    ноги = {}
    p = КОРЕНЬ / "data" / "podbivka" / "kotirovki_nogi.json"
    if p.exists():
        ноги = json.loads(p.read_text(encoding="utf-8"))["котировки"]
    сейчас = time.time()

    def блок(выбор):
        по_q = collections.defaultdict(lambda: {"покупок": 0, "с_числом": 0, "по_кошельку": collections.Counter(),
                                                 "по_программе_пула_токена": collections.Counter()})
        for x in выбор:
            д = по_q[x["quote_mint"]]
            д["покупок"] += 1
            д["с_числом"] += int(x["с_числом"])
            д["по_кошельку"][гр.get(x["wallet"], {}).get("имя", x["wallet"][:8]) + " " + x["wallet"][:8]] += 1
            д["по_программе_пула_токена"][ПРОГ.get(x["program"], x["program"] or "?")] += 1
        из_ = []
        for q, д in sorted(по_q.items(), key=lambda kv: -kv[1]["покупок"]):
            н = ноги.get(q) or {}
            из_.append({"quote_mint": q, "символ": н.get("символ"), "имя": н.get("имя"),
                        "покупок": д["покупок"], "с_числом_режима_2": д["с_числом"],
                        "по_кошельку": dict(д["по_кошельку"].most_common()),
                        "по_программе_пула_токена": dict(д["по_программе_пула_токена"].most_common()),
                        "первая_нога": н.get("ноги"), "первая_нога_откуда": н.get("откуда")})
        return из_

    группа_пп = [x for x in пп if x["wallet"] in гр]
    лидер7 = [x for x in пп if x["wallet"] == ЛИДЕР and x["blockTime"] and x["blockTime"] >= сейчас - 7 * 86400]
    по_кош = collections.defaultdict(collections.Counter)
    for x in группа_пп:
        по_кош[x["wallet"]][x["quote_mint"]] += 1
    итог = {
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "откуда": "подбивка режима 2 (data/podbivka/rezhim2/*.jsonl): первые не-SOL покупки от 2 SOL-экв. наших "
                  "источников (окно скана 18.09–26.09), лидер -- первые + докупки от 2 SOL (прогон l2_); группы -- "
                  "data/sources_2026-09-25.json Code-1 (leader / batch5 / lane_s0) + кандидаты. Первая нога -- плечи "
                  "котировки к WSOL / USDC / USDT в сделках источника (kotirovki_nogi.json), символ -- DAS getAsset.",
        "кошельки": {а: {**x, "не_SOL_покупок": sum(по_кош[а].values()),
                          "котировки": dict(по_кош[а].most_common())} for а, x in гр.items()},
        "котировки_группы": блок(группа_пп),
        "лидер_7_дней": {"с_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(сейчас - 7 * 86400)),
                         "покупок": len(лидер7), "котировки": блок(лидер7)},
    }
    (КОРЕНЬ / "data" / "podbivka" / "kotirovki_grupp.json").write_text(json.dumps(итог, ensure_ascii=False, indent=1),
                                                                         encoding="utf-8")
    print(f"группа: {len(группа_пп)} не-SOL покупок, котировок {len(итог['котировки_группы'])}; "
          f"лидер 7 дн.: {len(лидер7)} покупок, котировок {len(итог['лидер_7_дней']['котировки'])}; ног: {len(ноги)}")
    for x in итог["котировки_группы"][:15]:
        print(x["quote_mint"], x["символ"], x["покупок"], list(x["по_программе_пула_токена"].items())[:3])
    return 0


# ---------------------------------------------------------------- облако: ноги и символы

def ноги_сделки(tx: dict, q: str) -> list:
    import c2_common as C  # noqa: PLC0415
    sg = C.signers(tx)
    ряды = [r for r in C.token_rows(tx).values() if r["account"] and r["owner"] not in sg and r["post"] != r["pre"]]
    msg = ((tx.get("transaction") or {}).get("message") or {})
    инстр = [(ix.get("programId"), ix.get("accounts")) for ix in msg.get("instructions") or [] if isinstance(ix, dict)]
    for g in ((tx.get("meta") or {}).get("innerInstructions") or []):
        инстр += [(ix.get("programId"), ix.get("accounts")) for ix in (g or {}).get("instructions") or []
                  if isinstance(ix, dict)]
    из_ = []
    for прог, сч in инстр:
        if not isinstance(сч, list):
            continue
        s = set(сч)
        for a in [r for r in ряды if r["mint"] == q and r["account"] in s]:
            for b in [r for r in ряды if r["mint"] in (C.WSOL, C.USDC, C.USDT) and r["account"] in s]:
                if (a["post"] - a["pre"] > 0) == (b["post"] - b["pre"] > 0):
                    continue
                из_.append({"к": "WSOL" if b["mint"] == C.WSOL else ("USDC" if b["mint"] == C.USDC else "USDT"),
                            "программа": прог, "программа_имя": ПРОГ.get(прог), "хранилище_q": a["account"],
                            "хранилище_котировки": b["account"], "владелец_хранилищ": a["owner"]})
    return из_


def цепь(на_котировку: int) -> int:
    import podbivka_run as R  # noqa: PLC0415
    import podbivka_sim as S  # noqa: PLC0415
    гр = группы()
    сейчас = time.time()
    пп = [x for x in покупки() if x["wallet"] in гр and (x["wallet"] != ЛИДЕР or (x["blockTime"] or 0) >= сейчас - 8 * 86400)]
    по_q = collections.defaultdict(list)
    for x in sorted(пп, key=lambda x: -(x["blockTime"] or 0)):
        по_q[x["quote_mint"]].append(x)
    уз = S.Узел()
    итог = {}
    for q, xs in по_q.items():
        ноги = collections.Counter()
        образец = {}
        for x in xs[:на_котировку]:
            with уз.на(S.узел_по_времени(x["blockTime"])):
                try:
                    tx = уз.tx(x["signature"])
                except RuntimeError:
                    tx = None
            уз._кэш.clear()  # noqa: SLF001
            for н in ноги_сделки(tx, q) if tx else []:
                к = (н["к"], н["программа"], н["хранилище_q"], н["хранилище_котировки"])
                ноги[к] += 1
                образец[к] = н
        символ = имя = None
        try:
            with уз.на("helius"):
                а = уз.вызов("getAsset", {"id": q}) or {}
            мд = ((а.get("content") or {}).get("metadata") or {})
            символ, имя = мд.get("symbol"), мд.get("name")
        except RuntimeError:
            pass
        итог[q] = {"символ": символ, "имя": имя, "ноги": [{**образец[к], "сделок": n} for к, n in ноги.most_common()],
                   "откуда": f"сделки источника: {min(len(xs), на_котировку)} из {len(xs)}"}
    p = КОРЕНЬ / "data" / "podbivka" / "kotirovki_nogi.json"
    p.write_text(json.dumps({"котировки": итог, "расход": уз.расход()}, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(p)
    R.пуш("Podbivka-2: kotirovki -- nogi SOL<->kotirovka i simvoly [automated]", [str(p)])
    print(f"котировок {len(итог)}; расход {json.dumps(уз.расход(), ensure_ascii=False)}")
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--cep", action="store_true")
    р.add_argument("--na-kotirovku", type=int, default=5)
    а = р.parse_args()
    return цепь(а.na_kotirovku) if а.cep else сборка()


if __name__ == "__main__":
    raise SystemExit(main())
