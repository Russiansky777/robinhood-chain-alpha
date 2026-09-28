#!/usr/bin/env python3
"""Закрытые сделки полосы -- ОКОНЧАТЕЛЬНЫМ форматом владельца (28.09).

ЗАЧЕМ (слово владельца 28.09, п.6): "по трём вечерним сделкам (Omakase CvUX4G,
anon5pHe 7tEsDu x2) прислать BUY и SELL в этом формате один раз, затем следующая
живая сделка приходит уже так". Строки рисует ТОТ ЖЕ bloom_doklad, которым
докладывают живые сделки, и по тем же записям позиций -- поэтому образец
показывает ровно то, что придёт в чат, а не переписанный от руки текст.

ПОМЕТОК НЕТ. Прежняя "ПЕРЕСБОРКА" отменена владельцем: "Отдельных сообщений
«полный круг», «ПЕРЕСБОРКА», «образец» нет". BUY и SELL уходят двумя
сообщениями, как у живой сделки.

Выбор сделок: --cid, --podpisi (любая подпись сделки или её начало: источника,
покупки, продажи) или --since-utc.

Только чтение журнала позиций. Отправка -- по явному --otpravit, через тот же
Оповещатель, которым говорит служба.
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
    р.add_argument("--podpisi", default="",
                    help="подписи (или их начала) через запятую: источника, покупки или продажи")
    р.add_argument("--otpravit", action="store_true")
    р.add_argument("--out", default="/tmp/peresborka_sdelok.json")
    а = р.parse_args()

    все = позиции(а.state_dir)
    нужные = []
    если_cid = [x.strip() for x in (а.cid or "").split(",") if x.strip()]
    если_подписи = [x.strip() for x in (а.podpisi or "").split(",") if x.strip()]
    порог = None
    if а.since_utc:
        порог = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(
            __import__("calendar").timegm(time.strptime(
                а.since_utc.replace("Z", "").replace("T", " ")[:19],
                "%Y-%m-%d %H:%M:%S"))))
    def подходит_по_подписи(поз_: dict) -> bool:
        """Любая подпись сделки: источника, покупки (в т.ч. принятая) или продажи."""
        свои = [поз_.get("source_sig"), поз_.get("lane_landed_signature"),
                поз_.get("lane_signature"), поз_.get("closed_buy_signature")]
        свои += [x for x in (поз_.get("last_sell_signatures") or [])
                 if isinstance(x, str)]
        о = поз_.get("last_sell_reported")
        if isinstance(о, dict):
            свои.append(о.get("signature"))
        elif isinstance(о, str):
            свои.append(о)
        свои = [str(x) for x in свои if x]
        return any(s_.startswith(x) or x in s_ for s_ in свои for x in если_подписи)

    for cid, поз in все.items():
        if если_cid:
            if not any(cid.startswith(x) or x == cid for x in если_cid):
                continue
        elif если_подписи:
            if not подходит_по_подписи(поз):
                continue
        else:
            if not поз.get("lane"):
                continue
            if порог and str(поз.get("ts_intent_utc") or "") < порог:
                continue
        нужные.append((cid, поз))
    нужные.sort(key=lambda п: str(п[1].get("ts_intent_utc") or ""))

    итог = {"snyato_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "state_dir": а.state_dir,
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
        buy = DK.строка_buy(п)
        sell = DK.строка_sell(п) if п.get("продана") else None
        зап = {"cid": cid, "buy": buy, "sell": sell,
                "polya": dict(п),
                # ВСЕ КЛЮЧИ ЗАПИСИ -- на разбор, почему поле пусто. Только
                # имена ключей и простые значения: подписи и адреса в отчёт не
                # печатаем целиком.
                "klyuchi_zapisi": sorted(поз)}
        if оповещатель is not None:
            зап["otpravleno_buy"] = оповещатель.послать(buy)
            if sell:
                зап["otpravleno_sell"] = оповещатель.послать(sell)
        итог["sdelki"].append(зап)
    текст_итога = json.dumps(итог, ensure_ascii=False, indent=1)
    print(текст_итога)
    if а.out:
        Path(а.out).write_text(текст_итога, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(главное())
