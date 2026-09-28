#!/usr/bin/env python3
"""ПЕРЕСБОРКА: уже закрытые сделки полосы -- новым докладчиком.

ЗАЧЕМ (слово владельца 27.09, вечер, п.6): "по двум сегодняшним сделкам полосы
прислать полные строки покупка+продажа новой функцией с пометкой ПЕРЕСБОРКА".
Строки рисует ТОТ ЖЕ bloom_doklad, которым теперь докладывают живые сделки, и по
тем же записям позиций -- поэтому образец показывает ровно то, что придёт в чат
по следующей живой сделке, а не переписанный от руки текст.

Только чтение журнала позиций. Отправка -- по явному --otpravit, одним
сообщением на сделку, через тот же Оповещатель, которым говорит служба.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import sys
import time
from pathlib import Path

КОД = os.environ.get("BLOOM_CODE_DIR", "/home/bot/bloom_executor")
if КОД and Path(КОД).exists():
    sys.path.insert(0, КОД)
КОРЕНЬ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(КОРЕНЬ / "analysis"))

import bloom_doklad as DK  # noqa: E402

try:
    import bloom_notify as NT
except Exception:  # noqa: BLE001
    NT = None


def строки(путь: str):
    открыть = gzip.open if путь.endswith(".gz") else open
    try:
        with открыть(путь, "rt", encoding="utf-8", errors="replace") as f:
            for с in f:
                с = с.strip()
                if not с:
                    continue
                try:
                    yield json.loads(с)
                except Exception:  # noqa: BLE001
                    continue
    except OSError:
        return


def позиции(state_dir: str) -> dict:
    """Позиции склеиваются по client_order_id: строки дописываются частями."""
    из_: dict = {}
    пути = sorted(glob.glob(str(Path(state_dir) / "positions.jsonl*")))
    for путь in пути:
        for з in строки(путь):
            cid = з.get("client_order_id")
            if not cid:
                continue
            из_.setdefault(cid, {}).update({к: v for к, v in з.items()
                                             if v is not None})
    return из_


def главное() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--cid", default="", help="client_order_id через запятую")
    р.add_argument("--since-utc", default="",
                    help="взять все сделки полосы позже этого времени")
    р.add_argument("--pometka", default="ПЕРЕСБОРКА")
    р.add_argument("--otpravit", action="store_true")
    р.add_argument("--out", default="/tmp/peresborka_sdelok.json")
    а = р.parse_args()

    все = позиции(а.state_dir)
    нужные = []
    если_cid = [x.strip() for x in (а.cid or "").split(",") if x.strip()]
    порог = None
    if а.since_utc:
        порог = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(
            __import__("calendar").timegm(time.strptime(
                а.since_utc.replace("Z", "").replace("T", " ")[:19],
                "%Y-%m-%d %H:%M:%S"))))
    for cid, поз in все.items():
        if если_cid:
            if not any(cid.startswith(x) or x == cid for x in если_cid):
                continue
        else:
            if not поз.get("lane"):
                continue
            if порог and str(поз.get("ts_intent_utc") or "") < порог:
                continue
        нужные.append((cid, поз))
    нужные.sort(key=lambda п: str(п[1].get("ts_intent_utc") or ""))

    итог = {"snyato_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "state_dir": а.state_dir, "pometka": а.pometka,
             "pozitsiy_vsego": len(все), "vzyato": len(нужные),
             "otpravka": bool(а.otpravit), "sdelki": []}
    оповещатель = None
    if а.otpravit:
        if NT is None:
            итог["pochemu_net_otpravki"] = "модуль оповещений не загрузился"
        else:
            оповещатель = NT.Оповещатель(в_фоне=False)
    for cid, поз in нужные:
        п = DK.поля(поз)
        текст = DK.сообщение(п, пометка=а.pometka)
        зап = {"cid": cid, "tekst": текст,
                "polya": {к: v for к, v in п.items() if к != "издержки"},
                "izderzhki": п.get("издержки")}
        if оповещатель is not None:
            # КЛЮЧ ДРУГОЙ, ЧЕМ У ЖИВОЙ СДЕЛКИ: пересборка не должна править
            # сообщение настоящей сделки, если оно ещё помнится службой.
            зап["otpravleno"] = оповещатель.послать(текст,
                                                     ключ=f"peresborka-{cid}")
        итог["sdelki"].append(зап)
    текст_итога = json.dumps(итог, ensure_ascii=False, indent=1)
    print(текст_итога)
    if а.out:
        Path(а.out).write_text(текст_итога, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(главное())
