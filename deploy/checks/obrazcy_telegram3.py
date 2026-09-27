#!/usr/bin/env python3
"""Три образца формата 3 ИЗ РЕАЛЬНЫХ данных: сделка, строка часа, тревога.

Владелец 27.09: "прислать по одному образцу сделки, строки часа и тревоги".
Числа берутся ТОЛЬКО из уже собранных файлов репозитория:
  * data/lane_table_raw.json -- позиции полосы целиком (вход, возврат, слоты,
    место в блоке, кто довёз, подписи, группа, время);
  * data/lane_table.json -- та же таблица разобранной, для места и S+N.
Чего в файлах нет -- в образце стоит чертой, а не выдуманным числом.

Тревога берётся только по РЕАЛЬНОМУ поводу из закрытого списка четырёх. Если
ни один за сутки не случился, так и написано -- образец тревоги не выдумывается.

Только чтение. Отправку делает прогон, этот файл лишь печатает строки.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(КОРЕНЬ / "analysis"))

import bloom_tg_format3 as F3  # noqa: E402


def прочитать(путь: Path):
    try:
        return json.loads(путь.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _путь(п: str) -> Path:
    п_ = Path(п)
    return п_ if п_.exists() else КОРЕНЬ / п


def сделка_образец(сырые: dict, таблица: dict) -> dict:
    """Одно сообщение сделки: строка покупки плюс дописанная строка продажи."""
    закрытые = [(cid, p) for cid, p in (сырые or {}).items()
                if p.get("closed_sol_net") is not None and p.get("sol_in")]
    if not закрытые:
        return {"ok": False, "почему": "закрытых сделок полосы в файле нет"}
    закрытые.sort(key=lambda кв: -float(кв[1].get("ts_closed") or 0))
    cid, p = закрытые[0]
    ряды = {р.get("cid"): р for р in (таблица or {}).get("rows") or []}
    ряд = ряды.get(cid) or {}
    вход = float(p["sol_in"])
    возврат = float(p["closed_sol_net"])
    чаевые = float(p.get("lane_tips_total_sol") or 0.0)
    приоритет = float(p.get("lane_priority_lamports") or 0) / 1e9
    стоимость = вход + чаевые + приоритет
    чистый = (возврат / стоимость - 1.0) * 100.0 if стоимость else None
    секунды = None
    if p.get("ts_closed") and p.get("ts_intent"):
        секунды = float(p["ts_closed"]) - float(p["ts_intent"])
    наш_s = None
    if isinstance(ряд.get("slots_behind"), int):
        наш_s = ряд["slots_behind"]
    buy = F3.строка_buy(
        время_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                 time.gmtime(float(p.get("ts_intent") or 0))),
        группа=p.get("lane_group"), площадка=F3.ПЛОЩАДКА_ПОЛОСА,
        размер_sol=вход, имя=None, минт=p.get("mint"),
        наш_s=наш_s, наш_индекс=ряд.get("block_index"),
        наш_всего=ряд.get("block_total"),
        процент_к_источнику=p.get("entry_premium_pct"),
        довёз=ряд.get("winner") or p.get("lane_winner"),
        подпись=((ряд.get("chain") or {}).get("signature")
                  or p.get("lane_signature")))
    # ВХОД В СТРОКЕ -- РАЗМЕР СДЕЛКИ (как в образце владельца: "0.200 -> 0.231
    # -- +15.3 %"), а процент -- ЧИСТЫЙ, от полной стоимости кошельку вместе с
    # чаевыми и приоритетом. Поэтому процент не сходится с делением двух чисел
    # строки, и так и задумано: числа -- размер и возврат, процент -- чистый.
    sell = F3.строка_sell(секунды=секунды, вход_sol=вход,
                           возврат_sol=возврат, процент=чистый,
                           подпись=(p.get("last_sell_reported")
                                     or (p.get("last_sell_signatures") or [None])[0]))
    return {"ok": True, "cid": cid,
             "текст": F3.сообщение_сделки(buy=buy, sell=sell),
             "числа": {"вход_sol": вход, "стоимость_sol": round(стоимость, 9),
                        "возврат_sol": возврат,
                        "процент_чистый": None if чистый is None else round(чистый, 2),
                        "секунды": None if секунды is None else round(секунды, 1)}}


def час_образец(сырые: dict, таблица: dict | None = None) -> dict:
    """Строка часа по РЕАЛЬНЫМ сделкам: группы, сделки, итог, доля S+0."""
    свои = [p for p in (сырые or {}).values() if p.get("sol_in")]
    if not свои:
        return {"ok": False, "почему": "сделок полосы в файле нет"}
    по_группам: dict = {}
    s0 = всего = 0
    for p in свои:
        гр = p.get("lane_group") or "?"
        з = по_группам.setdefault(гр, {"n": 0, "итог": 0.0})
        з["n"] += 1
        вход = float(p.get("sol_in") or 0.0)
        возврат = p.get("closed_sol_net")
        расход = (float(p.get("lane_tips_total_sol") or 0.0)
                  + float(p.get("lane_priority_lamports") or 0) / 1e9)
        з["итог"] += ((float(возврат) - вход - расход) if возврат is not None
                      else -расход)
        всего += 1
        # S+0 -- ИЗ РАЗОБРАННОЙ ТАБЛИЦЫ, где слот посадки взят по цепи; в сырой
        # позиции его может не быть, и тогда сделка молча выпала бы из числителя.
        ряд_с = ((таблица or {}).get("_по_cid") or {}).get(p.get("client_order_id"))
        сдвиг = (ряд_с or {}).get("slots_behind")
        if сдвиг is None:
            наш = p.get("lane_landed_slot") or p.get("our_slot")
            ист = p.get("source_slot")
            сдвиг = ((наш - ист) if isinstance(наш, int) and isinstance(ист, int)
                     else None)
        if сдвиг == 0:
            s0 += 1
    времена = [float(p.get("ts_intent") or 0) for p in свои if p.get("ts_intent")]
    окно = "—"
    if времена:
        окно = (time.strftime("%H", time.gmtime(min(времена))) + "–"
                + time.strftime("%H", time.gmtime(max(времена))))
    return {"ok": True,
             "текст": F3.строка_часа(
                 окно=окно,
                 по_группам=[(гр, з["n"], round(з["итог"], 9))
                             for гр, з in sorted(по_группам.items(),
                                                  key=lambda кв: -кв[1]["n"])],
                 s0=s0, сделок_всего=всего, dbot_sol=None,
                 стоп_группа=None),
             "числа": {гр: {"сделок": з["n"], "итог": round(з["итог"], 9)}
                        for гр, з in по_группам.items()}}


def тревога_образец(сырые: dict) -> dict:
    """Тревога -- только по РЕАЛЬНОМУ поводу из закрытого списка четырёх."""
    for p in (сырые or {}).values():
        итог = p.get("last_sell_outcome") or {}
        if итог.get("ok") is False:
            return {"ok": True, "повод": "продажа упала",
                     "текст": F3.строка_тревоги(
                         "продажа_упала",
                         f"минт {str(p.get('mint'))[:10]}, код "
                         f"{итог.get('error_code') or итог.get('why_not') or '?'}")}
    for p in (сырые or {}).values():
        if p.get("state") in ("bought", "selling") and p.get("ts_sent"):
            прошло = time.time() - float(p["ts_sent"])
            if прошло > 60 and p.get("closed_sol_net") is None:
                return {"ok": True, "повод": "покупка села, продажи нет 60 с",
                         "текст": F3.строка_тревоги(
                             "нет_продажи",
                             f"минт {str(p.get('mint'))[:10]}, прошло "
                             f"{прошло:.0f} с")}
    return {"ok": False,
             "почему": ("ни один из четырёх поводов тревоги за эти сутки не "
                         "случился -- образец тревоги не выдумываю")}


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--syrye", default="data/lane_table_raw.json")
    р.add_argument("--tablica", default="data/lane_table.json")
    р.add_argument("--out", default="")
    # ОТПРАВКА -- ТЕМ ЖЕ ОТПРАВИТЕЛЕМ, ЧТО И СДЕЛКИ (п.5 плана владельца:
    # "часовая строка и тревоги переведены на того же отправителя и формат 3,
    # что сделки"). Образец, посланный другим путём, ничего бы не проверил.
    # Токен берётся ИЗ ОКРУЖЕНИЯ СЛУЖБЫ на хосте и наружу не печатается.
    р.add_argument("--otpravit", action="store_true",
                   help="послать три образца в Telegram тем же отправителем")
    а = р.parse_args()
    сырые = прочитать(_путь(а.syrye)) or {}
    таблица = прочитать(_путь(а.tablica)) or {}
    таблица["_по_cid"] = {р.get("cid"): р for р in (таблица.get("rows") or [])}
    итог = {"снято_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "позиций_в_файле": len(сырые),
             "сделка": сделка_образец(сырые, таблица),
             "час": час_образец(сырые, таблица),
             "тревога": тревога_образец(сырые)}
    for имя in ("сделка", "час", "тревога"):
        часть = итог[имя]
        print(f"--- {имя} ---")
        print(часть.get("текст") or f"НЕТ: {часть.get('почему')}")
    if а.otpravit:
        итог["отправка"] = послать_образцы(итог)
        print(f"--- отправка --- {json.dumps(итог['отправка'], ensure_ascii=False)}")
    if а.out:
        Path(а.out).write_text(json.dumps(итог, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    return 0


def послать_образцы(итог: dict) -> dict:
    """Три образца -- через bloom_notify.Оповещатель, как сделки.

    Ждём ответа Telegram (в_фоне=False): прогон обязан сказать, дошло или нет,
    а не завершиться раньше фонового потока. Ни токена, ни чата в вывод не
    попадает -- только счётчики и причина отказа словами.
    """
    из_ = {"послано": 0, "сбоев": 0, "по_образцам": {}, "why_not": None}
    try:
        import bloom_notify as NT  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"отправитель не загружен: {type(exc).__name__}"
        return из_
    try:
        оп = NT.Оповещатель(в_фоне=False)
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"отправитель не создан: {type(exc).__name__}"
        return из_
    for имя in ("сделка", "час", "тревога"):
        текст = (итог.get(имя) or {}).get("текст")
        if not текст:
            из_["по_образцам"][имя] = "нечего послать: образца нет"
            continue
        # ПОМЕТКА "ОБРАЗЕЦ" -- обязательна. Без неё строка в чате неотличима от
        # настоящей сделки, и владелец решит, что полоса торгует.
        ответ = оп.послать(f"ОБРАЗЕЦ ФОРМАТА 3 ({имя})\n{текст}")
        ок = bool((ответ or {}).get("ok"))
        из_["по_образцам"][имя] = "послано" if ок else (
            (ответ or {}).get("why_not") or "ответ отправителя без признака")
        из_["послано"] += int(ок)
        из_["сбоев"] += int(not ок)
    из_["всего_отправитель"] = {"послано": getattr(оп, "послано", None),
                                 "сбоев": getattr(оп, "сбоев", None),
                                 "выключен_почему": getattr(оп, "выключен_почему", "")}
    return из_


if __name__ == "__main__":
    sys.exit(main())
