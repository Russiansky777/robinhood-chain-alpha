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
    # ПОЧЕМУ НЕ "action == buy". Полоса пишет отправку в positions.jsonl, а в
    # журнале решений её видно по признаку lane_allowed: гейт полосы пропустил
    # сигнал. Прежний счёт по action давал ровно ноль покупок при семнадцати
    # живых сделках -- число было честно пустым, но бесполезным.
    из_ = {"решений": 0, "без_группы": 0, "без_кода": 0, "до_окна": 0,
            "по_группам": {}, "по_кодам": {}, "отставание": [], "возраст_с": [],
            "отставание_полосы": [], "прошло_гейт_полосы": 0, "покупок_bloom": 0}
    пути = sorted(glob.glob(str(Path(state_dir) / "decisions.jsonl*")))
    for путь in пути:
        for r in строки(путь):
            когда = r.get("ts") or r.get("t_recv_ts") or 0
            if since_ts is not None and когда and когда < since_ts:
                из_["до_окна"] += 1
                continue
            из_["решений"] += 1
            гр = r.get("source_task") or r.get("group")
            if not гр:
                из_["без_группы"] += 1
                гр = "поля нет"
            г = из_["по_группам"].setdefault(гр, {"решений": 0, "покупок": 0})
            г["решений"] += 1
            код = r.get("code") or r.get("action")
            if not код:
                из_["без_кода"] += 1
                код = "поля нет"
            из_["по_кодам"][код] = из_["по_кодам"].get(код, 0) + 1
            отст = r.get("slot_lag")
            if isinstance(отст, (int, float)):
                из_["отставание"].append(float(отст))
            возр = r.get("net_slot_age_s")
            if isinstance(возр, (int, float)):
                из_["возраст_с"].append(float(возр))
            if (r.get("action") or "") == "buy":
                из_["покупок_bloom"] += 1
                г["покупок"] += 1
            if r.get("lane_allowed"):
                из_["прошло_гейт_полосы"] += 1
                г["прошло_гейт_полосы"] = г.get("прошло_гейт_полосы", 0) + 1
                if isinstance(отст, (int, float)):
                    из_["отставание_полосы"].append(float(отст))

    return из_


def сделки_полосы(state_dir: str, since_ts: float | None) -> dict:
    """Сделки полосы за окно -- ПО ЖУРНАЛУ ПОЗИЦИЙ, а не по решениям.

    ПОЧЕМУ НЕ ПО РЕШЕНИЯМ. Признак lane_allowed в журнале решений -- это ранний
    гейт, а не сделка: за сутки 28.09 он стоял у 2332 решений при двадцати
    настоящих сделках полосы, и 2216 из них были по группе log_only, которой
    полоса не торгует вовсе. Сделка -- это ЗАПИСЬ ПОЗИЦИИ с меткой полосы.

    Тип пула берётся из поля program позиции: его пишет бронь при отправке.
    Старые позиции его не имеют -- они и считаются отдельной графой, а не
    приписываются какому-то строителю.
    """
    из_ = {"сделок": 0, "по_строителям": {}, "по_группам": {}, "закрыто": 0,
            "открытых": 0}
    состояния = {}
    for путь in sorted(glob.glob(str(Path(state_dir) / "positions.jsonl*"))):
        for r in строки(путь):
            cid = r.get("client_order_id")
            if not cid:
                continue
            з = состояния.setdefault(cid, {})
            з.update(r)
    for з in состояния.values():
        if з.get("lane") != "lane":
            continue
        т = з.get("ts_intent") or з.get("ts_sent")
        if since_ts is not None and т and float(т) < since_ts:
            continue
        из_["сделок"] += 1
        тип = з.get("program") or "поля нет"
        из_["по_строителям"][тип] = из_["по_строителям"].get(тип, 0) + 1
        гр = з.get("lane_group") or "поля нет"
        из_["по_группам"][гр] = из_["по_группам"].get(гр, 0) + 1
        if з.get("state") == "closed":
            из_["закрыто"] += 1
        else:
            из_["открытых"] += 1
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


def кредиты(usage_dir: str, день_ключ: str | None = None) -> dict:
    """Кредиты Helius за сутки по службам -- из тех же файлов, что и на хосте.

    Слово владельца 28.09 к вечернему итогу: "кредиты за сутки после снятия
    подписки". Читается ГОТОВЫЙ учёт службы, а не считается второй свой.
    """
    из_ = {"по_службам": {}, "всего": 0, "why_not": None}
    кат = Path(usage_dir)
    try:
        файлы = sorted(кат.glob("*.json"))
    except OSError as exc:
        return {"по_службам": {}, "всего": 0,
                "why_not": f"{type(exc).__name__}: {str(exc)[:120]}"}
    if not файлы:
        из_["why_not"] = f"файлов учёта нет: {кат}"
        return из_
    # ВИД ФАЙЛА -- КАК ЕГО ПИШЕТ СЛУЖБА: {"дни": {"ГГГГ-ММ-ДД": {"<служба>":
    # {"кредитов_за_день": N, "байт_за_день": M, "по_часам": {...},
    # "бюджет_за_день": B}}}}. Первый заход гадал по ключам "за_сутки" и
    # "spent_today" и выдал пустоту -- поэтому здесь имена читаются те самые.
    for ф in файлы:
        try:
            д = json.loads(ф.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        дни = д.get("дни") or {}
        if not isinstance(дни, dict) or not дни:
            continue
        # ДЕНЬ -- СЕГОДНЯШНИЙ, а не последний в файле. Первый заход брал
        # последний ключ каждого файла, и в итог попадала чужая дата: у
        # остановленной службы последний день -- 23.09, и её числа выглядели
        # бы сегодняшними.
        день = день_ключ or time.strftime("%Y-%m-%d", time.gmtime())
        за_день = дни.get(день) or {}
        if not isinstance(за_день, dict) or not за_день:
            из_["без_записи_за_день"] = sorted(
                set(из_.get("без_записи_за_день") or []) | {ф.stem})
            continue
        for служба, з in за_день.items():
            if not isinstance(з, dict):
                continue
            кред = з.get("кредитов_за_день")
            try:
                кред = int(кред)
            except (TypeError, ValueError):
                continue
            байт = з.get("байт_за_день")
            запись = из_["по_службам"].setdefault(служба, {})
            запись["кредитов"] = кред
            запись["байт"] = байт if isinstance(байт, int) else None
            запись["бюджет"] = з.get("бюджет_за_день")
            часы = з.get("по_часам") or {}
            if isinstance(часы, dict) and часы:
                свежие = sorted(часы)[-3:]
                запись["последние_часы"] = {
                    ч: (часы[ч] or {}).get("кредитов") for ч in свежие}
            из_["всего"] += кред
        из_["день"] = день
    return из_


def метрики_сторожа(state_dir: str) -> dict:
    """Метрики сторожа продаж из его признака жизни (слово владельца 28.09).

    Читается ГОТОВОЕ число, а не считается второе своё: сторож уже считает эти
    метрики каждую минуту по всем позициям суток, и вторая формула рядом
    разъехалась бы с первой на округлении.
    """
    п = Path(state_dir) / "seller_heartbeat.json"
    if not п.exists():
        return {"why_not": f"признака жизни сторожа нет: {п}"}
    try:
        д = json.loads(п.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"why_not": f"{type(exc).__name__}: {str(exc)[:120]}"}
    м = д.get("metrics")
    if not isinstance(м, dict):
        return {"why_not": "в признаке жизни сторожа нет блока metrics"}
    м = dict(м)
    м["метка_признака"] = д.get("updated_utc")
    return м


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--since-utc", default="",
                   help="начало окна, например 2026-09-27T20:00:00Z")
    р.add_argument("--gruppy", default="",
                   help="группы через запятую: по каким показать строителей")
    р.add_argument("--usage-dir", dest="usage_dir",
                   default="/home/bot/bloom_executor_live_data/helius_usage",
                   help="каталог учёта кредитов Helius")
    р.add_argument("--out", default="/tmp/utro_itog.json")
    а = р.parse_args()
    since_ts = разобрать_время(а.since_utc)
    о = итог(а.state_dir, since_ts)
    гр_список = [x.strip() for x in а.gruppy.split(",") if x.strip()] or [None]
    о["строители"] = {(г or "по окружению"): строители(г) for г in гр_список}
    о["метрики_сторожа"] = метрики_сторожа(а.state_dir)
    о["кредиты_за_сутки"] = кредиты(а.usage_dir)
    # СДЕЛКИ ПОЛОСЫ -- ОТДЕЛЬНЫМ РАЗДЕЛОМ И ПО ПОЗИЦИЯМ: числа решений и числа
    # сделок путать нельзя, разница между ними -- два порядка.
    о["сделки_полосы"] = сделки_полосы(а.state_dir, since_ts)
    о["окно_с"] = а.since_utc or None
    о["порог_слотов"] = ПОРОГ_СЛОТОВ
    отст = о.pop("отставание")
    возр = о.pop("возраст_с")
    покуп = о.pop("отставание_полосы")
    о["возраст_сигналов"] = {
        "замеров_отставания": len(отст),
        "медиана_слотов": кванти(отст, 0.5),
        "p90_слотов": кванти(отст, 0.9),
        f"доля_больше_{ПОРОГ_СЛОТОВ}": (round(sum(1 for x in отст if x > ПОРОГ_СЛОТОВ)
                                              / len(отст), 4) if отст else None),
        "медиана_возраста_с": кванти(возр, 0.5),
        "p90_возраста_с": кванти(возр, 0.9),
        "замеров_у_полосы": len(покуп),
        "медиана_слотов_у_полосы": кванти(покуп, 0.5),
        "p90_слотов_у_полосы": кванти(покуп, 0.9),
        f"доля_больше_{ПОРОГ_СЛОТОВ}_у_полосы": (
            round(sum(1 for x in покуп if x > ПОРОГ_СЛОТОВ) / len(покуп), 4)
            if покуп else None),
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
