#!/usr/bin/env python3
"""Подбивка: кто теряет свопы -- архив PumpApi или наша история пула (Helius).

По расхождениям data/podbivka/arhiv_den/svopy_rashozhdenie.json (архив меньше m4):
история хранилища пула в слотах s0+1..s0+2 по цепи (подписи, внешняя программа
сделки, есть ли инструкция Pump AMM на верхнем уровне), и есть ли эти подписи в
часовом файле архива. Только чтение.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--n", type=int, default=20)
    а = р.parse_args()
    import podbivka_run as R  # noqa: PLC0415
    import podbivka_sim as S  # noqa: PLC0415
    import podbivka_tablicy as T  # noqa: PLC0415
    расх = json.loads((КОРЕНЬ / "data" / "podbivka" / "arhiv_den" / "svopy_rashozhdenie.json").read_text(encoding="utf-8"))
    пулы = {}
    for кат in ("koshelki", "koshelki_dos", "nashi"):
        for к in T.читать(T.каталог_задачи(кат)):
            for п in к.get("покупки") or []:
                с = п.get("sim") or {}
                if с.get("pool_vault"):
                    пулы[п["signature"]] = (с["pool_vault"], п.get("slot"), п.get("blockTime"), с.get("program"))
    уз = S.Узел()
    выбор, видел = [], set()
    for x in расх:
        if x["signature"] in пулы and x["signature"] not in видел and len(выбор) < а.n:
            видел.add(x["signature"])
            выбор.append(x)
    ряды = []
    for x in выбор:
        vault, s0, bt, прог = пулы[x["signature"]]
        with уз.на(S.узел_по_времени(bt)):
            ист = S.история_пула(уз, vault, x["signature"], s0, опора=S.подпись_после_слота(уз, s0 + 3), до_слота=s0 + 2)
            сп = [з for з in ист["подписи"] if з["ok"] and s0 + 1 <= з["slot"] <= s0 + 2]
            txs = уз.пакет([з["signature"] for з in сп]) if сп else {}
        уз._кэш.clear()  # noqa: SLF001
        сделки = []
        for з in сп:
            т = txs.get(з["signature"]) or {}
            msg = ((т.get("transaction") or {}).get("message") or {})
            внеш = [ix.get("programId") for ix in msg.get("instructions") or [] if isinstance(ix, dict)]
            сделки.append({"signature": з["signature"], "slot": з["slot"], "внешние": внеш,
                           "pump_amm_сверху": PUMP_AMM in внеш})
        ряды.append({**x, "vault": vault, "s0": s0, "blockTime": bt, "program": прог, "сделки_цепи": сделки})
    # архив: часы этих сделок
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "zstandard"], check=True)
    import requests  # noqa: PLC0415
    import zstandard  # noqa: PLC0415
    нужные = {д["signature"] for р_ in ряды for д in р_["сделки_цепи"]}
    часы = sorted({time.strftime("%Y/%m/%d/%H", time.gmtime(р_["blockTime"])) for р_ in ряды if р_.get("blockTime")})
    найдено = {}
    р_sig = re.compile(r'"signature":\s*"([1-9A-HJ-NP-Za-km-z]{64,90})"')
    р_pool = re.compile(r'"pool":\s*"([a-z0-9\-_]+)"')
    for ч in часы:
        with requests.get(f"https://replay.pumpapi.io/{ч}.jsonl.zst", stream=True, timeout=120) as о:
            for стр in io.TextIOWrapper(zstandard.ZstdDecompressor().stream_reader(о.raw), encoding="utf-8", errors="replace"):
                m = р_sig.search(стр)
                if m and m.group(1) in нужные:
                    п = р_pool.search(стр)
                    найдено.setdefault(m.group(1), []).append(п.group(1) if п else None)
    for р_ in ряды:
        for д in р_["сделки_цепи"]:
            д["в_архиве"] = найдено.get(д["signature"])
    out = КОРЕНЬ / "data" / "podbivka" / "arhiv_den" / "svopy_kto_teryaet.json"
    out.write_text(json.dumps({"ряды": ряды, "расход": уз.расход()}, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(out)
    R.пуш("Podbivka-2: svopy -- kto teryaet [automated]", [str(out)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
