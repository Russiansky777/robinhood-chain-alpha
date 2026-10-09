#!/usr/bin/env python3
"""Пункт 2а задачи: что за токен в прошедшем срезе createPool и можно ли его вообще продать.

По цепи, для каждого целевого пула:
  * минт: программа (Token или Token-2022), десятичные, выпуск, mint authority, freeze
    authority и расширения Token-2022 -- transfer hook, transfer fee, default account state,
    non-transferable, permanent delegate. Любое из них может сделать продажу невозможной или
    съесть выручку, и тогда вся арифметика среза ничего не стоит;
  * счёт пула: чья программа им владеет (pump-amm, DAMM v2 или иная);
  * откуда токен: самая старая подпись минта и программы её транзакции -- какой площадкой
    он создан. Пагинация идёт от свежих к старым, и если минт не дочитан до створа, это
    честно помечено (`обрезано`), а не выдано за отсутствие.

Только чтение. Helius, темп -- PODB_HELIUS_RPS (потолок владельца 6 запросов в секунду на
все мои процессы вместе).
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
RPS = float(os.environ.get("PODB_HELIUS_RPS") or 6.0)
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN22 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
# Расширения, которые мешают продать или съедают выручку. Имена -- как их отдаёт jsonParsed.
ОПАСНЫЕ = ("transferHook", "transferFeeConfig", "defaultAccountState", "nonTransferable",
           "permanentDelegate", "confidentialTransferMint", "pausable")
ПЛОЩАДКИ = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump.fun",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "Meteora DBC",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "Meteora DAMM v2",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "Raydium LaunchLab",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
    "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG": "Moonshot",
    "BSwp6bEBihVLdqJRKGgzjcGLHkcTuzmSo1TQkHepzH8p": "Bonkswap",
    "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM": "Tesla/Stake",
}


class Темп:
    """Не больше RPS запросов в секунду -- потолок владельца на все мои процессы."""

    def __init__(self, rps: float) -> None:
        self.rps = max(0.5, rps)
        self._окно: list = []

    def ждать(self, запросов: int = 1) -> None:
        now = time.time()
        self._окно = [(t, n) for t, n in self._окно if now - t < 1.0]
        while sum(n for _, n in self._окно) + запросов > self.rps and self._окно:
            time.sleep(max(0.02, 1.0 - (now - self._окно[0][0])))
            now = time.time()
            self._окно = [(t, n) for t, n in self._окно if now - t < 1.0]
        self._окно.append((time.time(), запросов))


def цели(путь: Path, разделы: tuple) -> list:
    д = json.loads(путь.read_text(encoding="utf-8"))
    из_, видел = [], set()
    for раздел in разделы:
        тело = д.get(раздел) or {}
        for имя, сп in тело.items():
            if not isinstance(сп, list):
                continue
            for e in сп:
                pid, м = e.get("poolId"), e.get("минт")
                if not pid or not м or (pid, м) in видел:
                    continue
                видел.add((pid, м))
                из_.append({**e, "зачем": f"{раздел}/{имя}"})
    return из_


def счёт_минта(rpc, темп: Темп, минт: str) -> dict:
    темп.ждать()
    о = rpc.call("getAccountInfo", [минт, {"encoding": "jsonParsed"}])
    v = ((о or {}).get("value") or {})
    п = ((v.get("data") or {}).get("parsed") or {}).get("info") or {}
    расш = п.get("extensions") or []
    имена = [(x.get("extension") if isinstance(x, dict) else str(x)) for x in расш]
    return {"владелец": v.get("owner"),
            "программа": ("Token-2022" if v.get("owner") == TOKEN22
                          else "Token" if v.get("owner") == TOKEN else v.get("owner")),
            "десятичных": п.get("decimals"), "выпуск": п.get("supply"),
            "mint_authority": п.get("mintAuthority"),
            "freeze_authority": п.get("freezeAuthority"),
            "расширения": имена,
            "опасные": [и for и in имена if и in ОПАСНЫЕ]}


def счёт_пула(rpc, темп: Темп, pid: str) -> dict:
    темп.ждать()
    о = rpc.call("getAccountInfo", [pid, {"encoding": "base64"}])
    v = ((о or {}).get("value") or {})
    вл = v.get("owner")
    return {"владелец": вл, "программа": ПЛОЩАДКИ.get(вл, вл),
            "лампортов": v.get("lamports")}


def откуда_минт(rpc, темп: Темп, минт: str, предел_страниц: int) -> dict:
    """Самая старая подпись минта и программы её транзакции -- какой площадкой он создан."""
    до, старая, страниц, всего = None, None, 0, 0
    while страниц < предел_страниц:
        пар = {"limit": 1000}
        if до:
            пар["before"] = до
        темп.ждать()
        сп = rpc.call("getSignaturesForAddress", [минт, пар]) or []
        страниц += 1
        всего += len(сп)
        if not сп:
            break
        старая = сп[-1]["signature"]
        до = старая
        if len(сп) < 1000:
            return {"подписей": всего, "страниц": страниц, "обрезано": False,
                    **разбор_tx(rpc, темп, старая)}
    return {"подписей": всего, "страниц": страниц, "обрезано": True,
            **(разбор_tx(rpc, темп, старая) if старая else {})}


def разбор_tx(rpc, темп: Темп, подпись: str) -> dict:
    темп.ждать()
    о = rpc.call("getTransaction", [подпись, {"encoding": "jsonParsed",
                                              "maxSupportedTransactionVersion": 0}])
    if not о:
        return {"создание_программы": [], "создание_слот": None}
    прог: set = set()
    соб = ((о.get("transaction") or {}).get("message") or {})
    for и in соб.get("instructions") or []:
        if и.get("programId"):
            прог.add(и["programId"])
    for гр in ((о.get("meta") or {}).get("innerInstructions") or []):
        for и in гр.get("instructions") or []:
            if и.get("programId"):
                прог.add(и["programId"])
    площадки = sorted({ПЛОЩАДКИ[p] for p in прог if p in ПЛОЩАДКИ})
    return {"создание_программы": sorted(прог), "создание_площадки": площадки,
            "создание_слот": о.get("slot"), "создание_подпись": подпись}


def main() -> int:
    import c2_common as C  # noqa: PLC0415
    import podbivka_run as R  # noqa: PLC0415
    р_ = argparse.ArgumentParser()
    р_.add_argument("--celi", default=str(П / "pereezd_celi.json"))
    р_.add_argument("--razdely", default="пункт_2",
                    help="разделы файла целей через запятую")
    р_.add_argument("--skolko", type=int, default=100)
    р_.add_argument("--predel-stranic", type=int, default=6,
                    help="страниц пагинации подписей минта (1000 на страницу)")
    р_.add_argument("--bez-istoka", action="store_true",
                    help="не искать, откуда минт -- только счета минта и пула")
    р_.add_argument("--metka", default="token")
    а = р_.parse_args()

    ключ = (os.environ.get("HELIUS_API_KEY2") or os.environ.get("HELIUS_API_KEY")
            or os.environ.get("HELIUS_API") or "").strip()
    if not ключ:
        print("нет ключа Helius -- проход только облачный", flush=True)
        return 1
    rpc = C.C2Rpc(service="c2_pereezd_token", key=ключ)
    темп = Темп(RPS)
    цел = цели(Path(а.celi), tuple(x.strip() for x in а.razdely.split(",") if x.strip()))
    цел = цел[:а.skolko]
    print(f"целей {len(цел)}, темп не выше {RPS} запросов в секунду", flush=True)

    ряды, свод = [], collections.Counter()
    for i, e in enumerate(цел, 1):
        try:
            м = счёт_минта(rpc, темп, e["минт"])
            пул = счёт_пула(rpc, темп, e["poolId"])
            ист = ({} if а.bez_istoka
                   else откуда_минт(rpc, темп, e["минт"], а.predel_stranic))
        except Exception as exc:  # noqa: BLE001
            свод[f"ошибка_{type(exc).__name__}"] += 1
            print(f"  {i}/{len(цел)} {e['минт'][:10]}.. ошибка {type(exc).__name__}",
                  flush=True)
            continue
        ряд = {**{k: e.get(k) for k in ("poolId", "минт", "сутки", "пул", "резерв_sol",
                                        "зачем", "блок")},
               "минт_счёт": м, "пул_счёт": пул, "исток": ист}
        ряды.append(ряд)
        свод[f"программа:{м['программа']}"] += 1
        свод["mint_authority_есть"] += 1 if м["mint_authority"] else 0
        свод["freeze_authority_есть"] += 1 if м["freeze_authority"] else 0
        свод["опасные_расширения"] += 1 if м["опасные"] else 0
        for и in м["опасные"]:
            свод[f"расширение:{и}"] += 1
        for пл in (ист.get("создание_площадки") or []):
            свод[f"исток:{пл}"] += 1
        if ист and not ист.get("создание_площадки") and not ист.get("обрезано"):
            свод["исток:площадка_не_опознана"] += 1
        if ист.get("обрезано"):
            свод["исток:обрезано"] += 1
        свод[f"пул_программа:{пул['программа']}"] += 1
        if i % 10 == 0 or i == len(цел):
            print(f"  {i}/{len(цел)}", flush=True)
    тело = {"что": "пункт 2а: токен и пул прошедшего среза createPool по цепи",
            "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "параметры": vars(а), "свод": dict(свод), "ряды": ряды,
            "запросов": dict(getattr(rpc, "calls_by_method", {}) or {})}
    ф = П / f"pereezd_token_{а.metka}.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(ф)
    print(f"готово: {ф}\nсвод: {json.dumps(dict(свод), ensure_ascii=False)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
