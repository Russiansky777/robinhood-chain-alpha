#!/usr/bin/env python3
"""Подбивка: PumpApi (pumpapi.io) как источник данных -- только чтение, без ключей.

Шаг --docs: скачать документацию https://pumpapi.io/llms-full.txt (и llms.txt) в
data/podbivka/pumpapi/ -- из контейнера сессии домен закрыт политикой сети,
поэтому -- облаком.
Шаг --arhiv Y/M/D/HH[,...]: часовые файлы Historical Replay
(https://replay.pumpapi.io/Y/M/D/HH.jsonl.zst, событие на строку, формат потока)
потоком, без сохранения архива: события с подписью из data/podbivka/pumpapi/celi.json
(покупки наших источников 25.09, сделки полосы 27.09), все события минтов из целей с
резервом m4, все события с txSigner из наших 133; счёт событий по pool / action.
Шаг --potok N: N секунд потока wss://stream.pumpapi.io (одно соединение), счёт
событий, поля, задержка timestamp события -> приём на раннере.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
ПАПКА = КОРЕНЬ / "data" / "podbivka" / "pumpapi"


def docs() -> list:
    import requests  # noqa: PLC0415
    ПАПКА.mkdir(parents=True, exist_ok=True)
    из_ = []
    for url, имя in (("https://pumpapi.io/llms-full.txt", "llms-full.txt"), ("https://pumpapi.io/llms.txt", "llms.txt")):
        try:
            о = requests.get(url, timeout=60, headers={"User-Agent": "podbivka-readonly/1.0"})
            p = ПАПКА / имя
            p.write_bytes(о.content)
            из_.append(p)
            print(f"{url}: http {о.status_code}, {len(о.content)} байт")
        except Exception as exc:  # noqa: BLE001
            print(f"{url}: {type(exc).__name__}")
    (ПАПКА / "docs_meta.json").write_text(json.dumps({"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                                      "файлы": [p.name for p in из_]}, ensure_ascii=False),
                                          encoding="utf-8")
    return из_ + [ПАПКА / "docs_meta.json"]


def _pip(*пакеты):
    import subprocess  # noqa: PLC0415
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", *пакеты], check=True)


def arhiv(часы: list) -> list:
    import re  # noqa: PLC0415
    import requests  # noqa: PLC0415
    _pip("zstandard")
    import io  # noqa: PLC0415
    import zstandard  # noqa: PLC0415
    ц = json.loads((ПАПКА / "celi.json").read_text(encoding="utf-8"))["цели"]
    минты = {x["mint"] for x in ц.values() if x.get("резерв_s0_m4") and x.get("mint")}
    наши = {x["address"] for x in json.loads((КОРЕНЬ / "data" / "podbivka" / "istochniki.json")
                                             .read_text(encoding="utf-8"))["источники"]}
    р_sig = re.compile(r'"signature":\s*"([1-9A-HJ-NP-Za-km-z]{64,90})"')
    р_mint = re.compile(r'"mint":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
    р_signer = re.compile(r'"txSigner":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
    р_pool = re.compile(r'"pool":\s*"([a-z0-9\-_]+)"')
    р_act = re.compile(r'"action":\s*"([A-Za-z]+)"')
    ключи = ("signature", "action", "pool", "poolId", "mint", "quoteMint", "txSigner", "tokenAmount", "quoteAmount",
             "tokensInPool", "quoteInPool", "vTokensInBondingCurve", "vQuoteInBondingCurve", "price", "poolFeeRate",
             "priorityFee", "block", "timestamp", "localTimestamp", "breakdown")
    выход = []
    for ч in часы:
        url = f"https://replay.pumpapi.io/{ч}.jsonl.zst"
        итог = {"файл": ч, "строк": 0, "по_pool_action": {}, "цели": [], "минты_m4": [], "наши_кошельки": [],
                "ошибка": None, "секунд": 0}
        t0 = time.time()
        try:
            with requests.get(url, stream=True, timeout=120) as о:
                итог["http"] = о.status_code
                if о.status_code != 200:
                    итог["ошибка"] = f"http {о.status_code}"
                else:
                    чит = zstandard.ZstdDecompressor().stream_reader(о.raw)
                    for стр in io.TextIOWrapper(чит, encoding="utf-8", errors="replace"):
                        итог["строк"] += 1
                        п = р_pool.search(стр)
                        а = р_act.search(стр)
                        к = f"{п.group(1) if п else '-'}|{а.group(1) if а else '-'}"
                        итог["по_pool_action"][к] = итог["по_pool_action"].get(к, 0) + 1
                        сг = р_sig.search(стр)
                        m = р_mint.search(стр)
                        sn = р_signer.search(стр)
                        цель = сг and сг.group(1) in ц
                        по_минту = m and m.group(1) in минты
                        наш = sn and sn.group(1) in наши
                        if not (цель or по_минту or наш):
                            continue
                        try:
                            e = json.loads(стр)
                        except ValueError:
                            continue
                        e = {k: e.get(k) for k in ключи if k in e}
                        if цель:
                            итог["цели"].append(e)
                        if по_минту:
                            итог["минты_m4"].append(e)
                        if наш:
                            итог["наши_кошельки"].append(e)
        except Exception as exc:  # noqa: BLE001
            итог["ошибка"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        итог["секунд"] = round(time.time() - t0, 1)
        p = ПАПКА / "arhiv" / f"{ч.replace('/', '_')}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(итог, ensure_ascii=False), encoding="utf-8")
        print(f"{ч}: строк {итог['строк']}, целей {len(итог['цели'])}, минтов m4 {len(итог['минты_m4'])}, "
              f"наших {len(итог['наши_кошельки'])}, {итог['секунд']} с, {итог['ошибка']}")
        выход.append(p)
    return выход


def potok(секунд: int) -> list:
    _pip("websockets")
    import asyncio  # noqa: PLC0415
    import websockets  # noqa: PLC0415
    итог = {"секунд": секунд, "событий": 0, "по_pool_action": {}, "задержки_мс": [], "поля": {}, "ошибка": None,
            "образцы": []}

    async def идти():
        t_end = time.time() + секунд
        async with websockets.connect("wss://stream.pumpapi.io/", max_size=None) as ws:
            while time.time() < t_end:
                try:
                    м = await asyncio.wait_for(ws.recv(), timeout=max(1, t_end - time.time()))
                except asyncio.TimeoutError:
                    break
                приём = time.time() * 1000
                e = json.loads(м)
                итог["событий"] += 1
                к = f"{e.get('pool', '-')}|{e.get('action', '-')}"
                итог["по_pool_action"][к] = итог["по_pool_action"].get(к, 0) + 1
                for f in e:
                    итог["поля"][f] = итог["поля"].get(f, 0) + 1
                if e.get("timestamp") and итог["событий"] % 50 == 0:
                    итог["задержки_мс"].append(round(приём - float(e["timestamp"]), 1))
                if итог["событий"] <= 3 or (e.get("action") in ("buy", "sell") and len(итог["образцы"]) < 8):
                    итог["образцы"].append({k: e.get(k) for k in list(e)[:40]})
    try:
        asyncio.run(идти())
    except Exception as exc:  # noqa: BLE001
        итог["ошибка"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    p = ПАПКА / f"potok_{time.strftime('%Y%m%dT%H%M', time.gmtime())}.json"
    p.write_text(json.dumps(итог, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"поток: событий {итог['событий']} за {секунд} с, ошибка {итог['ошибка']}")
    return [p]


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--docs", action="store_true")
    р.add_argument("--arhiv", default="")
    р.add_argument("--potok", type=int, default=0)
    а = р.parse_args()
    import podbivka_run as R  # noqa: PLC0415
    файлы = docs() if а.docs else []
    if а.arhiv:
        файлы += arhiv(а.arhiv.split(","))
    if а.potok:
        файлы += potok(а.potok)
    for p in файлы:
        R.записано(p)
    if файлы:
        R.пуш("Podbivka-2: pumpapi docs [automated]", [str(p) for p in файлы])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
