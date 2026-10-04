#!/usr/bin/env python3
"""Общее для пунктов пакета 04.10: чтение кэша сигналов, билеты групп, статистика, окна.

Кэш строит `podbivka_paket_kesh.py` (один тяжёлый проход по суточным файлам архива). Здесь --
только чтение кэша и общие мерки, чтобы у всех пунктов пакета были одни и те же числа:
  * `сигналы()` -- ряды кэша с полями модели (состояния входа и выхода, удержание, рисунок);
  * `пп()` -- п.п. при любом билете, месте входа и горизонте: формула модели архива через
    `podbivka_bilet.пп_по_состояниям` (своих копий формулы нет);
  * `БИЛЕТ_ГРУППЫ`, `ПОЛИТИКА` -- билеты и политика групп из живой выгрузки Code-1
    (`data/podbivka/code1_sources_live.json`, только чтение);
  * `группы_адреса()` -- группы адреса по реестру `data/podbivka/arhiv_adresa.json`;
  * `окна()` -- подбор и проверка: первые сутки против последних, и две половины.
Только чтение.
"""
from __future__ import annotations

import collections
import gzip
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_bilet as B  # noqa: E402
import podbivka_cand2 as C2  # noqa: E402

КОРЕНЬ = C2.КОРЕНЬ
П = C2.П
КЭШ = Path(os.environ.get("PODB_PAKET_KESH") or (П / "paket")) / "signaly.jsonl.gz"
WSOL = "So11111111111111111111111111111111111111112"
ВЫХОД_ГРУППЫ = 108          # удержание живой полосы lane_s0 в слотах
БИЛЕТЫ_СЕТКА = (0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0)
КИЛЛ_СУТКИ = 2.0            # LANE_KILL_DROP_SOL: рубильник при −2.0 SOL за сутки


def политика() -> dict:
    """Политика групп из живой выгрузки Code-1: билет, допуск, open_max, удержание."""
    ф = П / "code1_sources_live.json"
    если_нет = {}
    if not ф.exists():
        return если_нет
    д = json.loads(ф.read_text(encoding="utf-8"))
    из_ = {}
    for г, v in (д.get("groups") or {}).items():
        из_[г] = {"билет": v.get("lane_sol") if v.get("lane_sol") is not None else v.get("lane_size"),
                  "торгует": v.get("lane_trades"), "допуск": v.get("slippage"),
                  "допуск_тонкий": v.get("slippage_thin_pool"),
                  "тонкий_ниже_sol": v.get("thin_pool_below_sol"),
                  "open_max": v.get("lane_open_max"), "удержание_слотов": v.get("hold_slots"),
                  "порог_источника_sol": v.get("min_target_sol"),
                  "адресов": len(v.get("addresses") or {})}
    return из_


ПОЛИТИКА = политика()
БИЛЕТ_ГРУППЫ = {г: v["билет"] for г, v in ПОЛИТИКА.items() if v.get("билет")}
# группы, которые реально торгуют (по той же выгрузке)
ТОРГУЮЩИЕ_ГРУППЫ = tuple(г for г, v in ПОЛИТИКА.items() if v.get("торгует"))


def реестр() -> dict:
    return json.loads((П / "arhiv_adresa.json").read_text(encoding="utf-8"))["адреса"]


def группа_адреса(адр: dict, a: str) -> str | None:
    """Торговая группа адреса: первая из его групп, у которой в политике есть билет."""
    for г in (адр.get(a) or {}).get("группы") or []:
        if г in БИЛЕТ_ГРУППЫ:
            return г
    return None


def сигналы(фильтр=None, путь: Path | None = None):
    """Ряды кэша по одному; `фильтр(ряд) -> bool` отсекает ненужное до разбора модели."""
    п = путь or КЭШ
    if not п.exists():
        raise SystemExit(f"нет кэша {п}: сначала podbivka_paket_kesh.py")
    with gzip.open(п, "rt", encoding="utf-8") as ф:
        for стр in ф:
            try:
                р = json.loads(стр)
            except ValueError:
                continue
            if фильтр is None or фильтр(р):
                yield р


def как_сигнал(р: dict) -> dict:
    """Ряд кэша -> вид, который понимает `podbivka_bilet.пп_по_состояниям`."""
    return {"pool": р.get("пул"),
            "модель": {"f": р.get("f"), "g": р.get("g"), "fee": р.get("fee"),
                       "состояния": {"вход": р.get("вход") or {}, "выход": р.get("выход") or {},
                                     "масштаб": р.get("масштаб")}}}


def пп(р: dict, билет: float, место: str = "S1", выход: int = ВЫХОД_ГРУППЫ):
    return B.пп_по_состояниям(как_сигнал(р), билет, место, выход)


def сутки(р: dict) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime((р.get("ts") or 0) / 1000))


def стат(значения: list, знаков: int = 3) -> dict:
    v = [x for x in значения if x is not None]
    if not v:
        return {"n": 0}
    з = sorted(v, reverse=True)
    return {"n": len(v), "медиана": round(statistics.median(v), знаков),
            "среднее": round(statistics.mean(v), знаков),
            "в_плюсе": round(100 * sum(1 for x in v if x > 0) / len(v)),
            "сумма": round(sum(v), 6),
            "сумма_без_верхних_2": round(sum(з[2:]), 6) if len(з) > 2 else None}


def окна(дни: list, подбор_суток: int = 8) -> dict:
    """Подбор -- ранние сутки, проверка -- последние; плюс две половины окна."""
    д = sorted(set(дни))
    подбор = д[:min(подбор_суток, max(len(д) - 1, 1))]
    проверка = [x for x in д if x not in подбор]
    return {"все": д, "подбор": подбор, "проверка": проверка,
            "половина_1": д[:len(д) // 2], "половина_2": д[len(д) // 2:]}


def по_суткам_сумма(ряды: list, ключ: str = "sol") -> dict:
    из_: dict = collections.defaultdict(float)
    for р in ряды:
        из_[р["сутки"]] += р[ключ]
    return dict(sorted(из_.items()))
