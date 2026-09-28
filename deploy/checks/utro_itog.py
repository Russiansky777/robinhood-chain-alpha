#!/usr/bin/env python3
"""Утренний итог полосы: сделки по группам, возраст сигналов, строители.

ЗАЧЕМ (слово владельца 28.09): "Утренний итог -- как договорено: сделки по
группам, место в слоте и свопы перед нами по каждой, возраст сигналов
(медиана, p90, доля > 3), строители в бою/в коде".

ЧТО СЧИТАЕТСЯ ЗДЕСЬ. Возраст сигнала -- это ОТСТАВАНИЕ В СЛОТАХ (slot_lag) и
возраст слота сети в секундах (net_slot_age_s) из записей решений. Доля > 3 --
доля сигналов, пришедших с отставанием больше трёх слотов: именно её владелец
просил видеть, потому что порог STALE стоит на этом же числе.

Место в блоке и свопы перед нами по каждой сделке здесь НЕ пересчитываются:
их даёт peresborka_sdelok.py по цепи, и дублировать разбор транзакций значит
получить второе число из того же места.

ЖУРНАЛ ЧИТАЕТСЯ ПОТОКОМ -- построчно, с поддержкой .gz. Правило появилось не
на пустом месте: журнал решений на хосте перевалил четверть миллиона записей, и
чтение целиком в память роняло прогоны.

Только чтение. Ни одной отправки, ни одной записи в состояние службы.
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
# ПРОВЕРКА СУЩЕСТВОВАНИЯ МОЖЕТ САМА УПАСТЬ. На раннере GitHub каталог службы
# принадлежит другому пользователю, и Path.exists() бросает PermissionError, а
# не возвращает False: самопроверка счётной части падала на этом до чтения
# любого журнала.
try:
    if КОД and Path(КОД).exists():
        sys.path.insert(0, КОД)
except OSError:
    pass
sys.path.insert(0, str(Path(__file__).resolve().parent))

ПОРОГ_СЛОТОВ = 3


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


def кванти(значения: list, доля: float):
    """Квантиль по отсортированному списку. Пусто -- None, а не ноль."""
    if not значения:
        return None
    з = sorted(значения)
    if len(з) == 1:
        return з[0]
    место = доля * (len(з) - 1)
    низ = int(место)
    верх = min(низ + 1, len(з) - 1)
    вес = место - низ
    return з[низ] * (1 - вес) + з[верх] * вес


def разобрать_время(текст: str):
    if not текст:
        return None
    try:
        return time.mktime(time.strptime(текст, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except Exception:  # noqa: BLE001
        return None


def итог(state_dir: str, since_ts: float | None) -> dict:
    из_ = {"решений": 0, "старого_вида": 0, "до_окна": 0, "по_группам": {},
            "по_кодам": {}, "отставание": [], "возраст_с": [],
            "отставание_покупок": [], "покупок": 0}
    пути = sorted(glob.glob(str(Path(state_dir) / "decisions.jsonl*")))
    for путь in пути:
        for r in строки(путь):
            когда = r.get("ts") or r.get("t_recv_ts") or 0
            if since_ts is not None and когда and когда < since_ts:
                из_["до_окна"] += 1
                continue
            из_["решений"] += 1
            гр = r.get("source_task") or r.get("group") or "?"
            г = из_["по_группам"].setdefault(гр, {"решений": 0, "покупок": 0})
            г["решений"] += 1
            код = r.get("code") or r.get("action") or "?"
            из_["по_кодам"][код] = из_["по_кодам"].get(код, 0) + 1
            отст = r.get("slot_lag")
            if isinstance(отст, (int, float)):
                из_["отставание"].append(float(отст))
            возр = r.get("net_slot_age_s")
            if isinstance(возр, (int, float)):
                из_["возраст_с"].append(float(возр))
            if (r.get("action") or "") == "buy":
                из_["покупок"] += 1
                г["покупок"] += 1
                if isinstance(отст, (int, float)):
                    из_["отставание_покупок"].append(float(отст))
    return из_


def строители(группа: str | None) -> dict:
    """Какие типы пулов полоса берёт сейчас и какие есть в коде."""
    из_ = {"в_бою": [], "в_коде": [], "почему_не": None}
    try:
        import bloom_own_send as OS  # noqa: PLC0415
        import c2_swap_build as B  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        из_["почему_не"] = f"{type(exc).__name__}: {exc}"
        return из_
    try:
        взятые = OS.типы_полосы(группа)
    except Exception as exc:  # noqa: BLE001
        из_["почему_не"] = f"типы_полосы: {type(exc).__name__}"
        взятые = set()
    for имя_конст, слово in sorted(OS.ИМЕНА_ТИПОВ.items()):
        адрес = getattr(B, имя_конст, None)
        if not адрес:
            continue
        (из_["в_бою"] if адрес in взятые else из_["в_коде"]).append(слово)
    try:
        if OS.двухшаговый_включён(группа):
            из_["в_бою"].append(OS.ИМЯ_ДВУХШАГОВОГО)
        else:
            из_["в_коде"].append(OS.ИМЯ_ДВУХШАГОВОГО)
    except Exception:  # noqa: BLE001
        pass
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--since-utc", default="",
                   help="начало окна, например 2026-09-27T20:00:00Z")
    р.add_argument("--gruppy", default="",
                   help="группы через запятую: по каким показать строителей")
    р.add_argument("--out", default="/tmp/utro_itog.json")
    а = р.parse_args()
    since_ts = разобрать_время(а.since_utc)
    о = итог(а.state_dir, since_ts)
    гр_список = [x.strip() for x in а.gruppy.split(",") if x.strip()] or [None]
    о["строители"] = {(г or "по окружению"): строители(г) for г in гр_список}
    о["окно_с"] = а.since_utc or None
    о["порог_слотов"] = ПОРОГ_СЛОТОВ
    отст = о.pop("отставание")
    возр = о.pop("возраст_с")
    покуп = о.pop("отставание_покупок")
    о["возраст_сигналов"] = {
        "замеров_отставания": len(отст),
        "медиана_слотов": кванти(отст, 0.5),
        "p90_слотов": кванти(отст, 0.9),
        f"доля_больше_{ПОРОГ_СЛОТОВ}": (round(sum(1 for x in отст if x > ПОРОГ_СЛОТОВ)
                                              / len(отст), 4) if отст else None),
        "медиана_возраста_с": кванти(возр, 0.5),
        "p90_возраста_с": кванти(возр, 0.9),
        "медиана_слотов_у_покупок": кванти(покуп, 0.5),
        "p90_слотов_у_покупок": кванти(покуп, 0.9),
    }
    о["по_кодам"] = dict(sorted(о["по_кодам"].items(), key=lambda t: -t[1])[:12])
    Path(а.out).write_text(json.dumps(о, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    print(json.dumps(о, ensure_ascii=False, indent=1)[:4000])
    return 0


def self_test() -> int:
    """Только счётная часть: квантили и порог. Денег здесь нет."""
    пройдено = провалено = 0

    def chk(что, ок, факт=""):
        nonlocal пройдено, провалено
        if ок:
            пройдено += 1
            print(f"  [ok  ] {что}")
        else:
            провалено += 1
            print(f"  [ПРОВАЛ] {что}: {факт}")

    chk("пусто -- None, а не ноль", кванти([], 0.5) is None)
    chk("одно значение -- оно само", кванти([7.0], 0.9) == 7.0)
    chk("медиана пяти", кванти([1, 2, 3, 4, 5], 0.5) == 3)
    chk("p90 десяти", abs(кванти(list(range(1, 11)), 0.9) - 9.1) < 1e-9,
        кванти(list(range(1, 11)), 0.9))
    chk("время разбирается", разобрать_время("2026-09-27T20:00:00Z") is not None)
    chk("мусорное время -- None", разобрать_время("вчера") is None)
    print(f"самопроверка утреннего итога: {пройдено}/{пройдено + провалено} пройдено")
    return 0 if провалено == 0 else 1


if __name__ == "__main__":
    raise SystemExit(self_test() if "--self-test" in sys.argv else main())
