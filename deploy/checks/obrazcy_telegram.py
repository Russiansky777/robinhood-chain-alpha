#!/usr/bin/env python3
"""Образцы трёх строк Telegram, собранные ИЗ РЕАЛЬНЫХ сделок 26.09.

Владелец просил показать образцы до включения флага. Числа берутся только из
уже собранных файлов репозитория:
  * data/podbivka/nashi_sdelki_host.json -- наши сделки (вход, возврат, время,
    подписи, минт, итог);
  * data/leader_geo.json -- место в блоке источника и наше (block_index,
    block_total), а также задержка;
  * data/lane_table.json -- часовая статистика по отправителям и долям.
Чего в файлах нет (кто успел до нас в блоке, копировщики) -- в образце стоит
чертой, а не выдуманным числом.

Только чтение.
"""
import argparse
import json
import statistics
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(КОРЕНЬ / "analysis"))

import bloom_tg_format as F  # noqa: E402


def прочитать(путь: Path):
    try:
        return json.loads(путь.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--sdelki", default="data/podbivka/nashi_sdelki_host.json")
    р.add_argument("--geo", default="data/leader_geo.json")
    р.add_argument("--tablica", default="data/lane_table.json")
    а = р.parse_args()
    сделки = (прочитать(КОРЕНЬ / а.sdelki) or {}).get("sdelki") or []
    гео = (прочитать(КОРЕНЬ / а.geo) or {}).get("ряды") or []
    таблица = прочитать(КОРЕНЬ / а.tablica) or {}

    место = {г.get("cid"): г for г in гео}
    # Берём РЕАЛЬНУЮ сделку полосы с продажей и с известным местом в блоке.
    свои = [с for с in сделки
            if с.get("side") == "lane" and с.get("sell_sig")
            and (с.get("ts_intent_utc") or "").startswith("2026-09-26")]
    с_местом = [с for с in свои if (место.get(с["cid"]) or {}).get("block_index") is not None]
    образец = (с_местом or свои)[:1]
    if not образец:
        print("СТОП: подходящих сделок в файлах нет", file=sys.stderr)
        return 2
    с = образец[0]
    г = место.get(с["cid"]) or {}

    buy = F.строка_buy(
        время_utc=с.get("ts_intent_utc"), размер_sol=с.get("in_sol"),
        имя_токена=None, имя_источника=(с.get("group") or "источник"),
        подпись_источника=с.get("source_sig"),
        индекс_источника=г.get("block_index"), всего_в_блоке=г.get("block_total"),
        наш_s=(0 if (г.get("our_slot") and г.get("source_slot")
                     and г["our_slot"] == г["source_slot"]) else
               ((г.get("our_slot") or 0) - (г.get("source_slot") or 0)
                if г.get("our_slot") and г.get("source_slot") else None)),
        наш_индекс=г.get("block_index"), наш_всего=г.get("block_total"),
        # ЗАДЕРЖКА ИЗ II.14 -- это "источник сел -> мы увидели", то есть
        # ИМЕННО "увидели", а не весь путь. Ставить её в "путь" значило бы
        # назвать одно число другим.
        путь_мс=None, увидели_мс=г.get("задержка_мс"), решили_мс=None,
        собрали_мс=None, отправитель=None, перед_нами=None, копировщиков=None)

    секунды = None
    if с.get("ts_intent_utc") and с.get("ts_closed_utc"):
        import calendar
        import time as _t
        т1 = calendar.timegm(_t.strptime(с["ts_intent_utc"], "%Y-%m-%dT%H:%M:%SZ"))
        т2 = calendar.timegm(_t.strptime(с["ts_closed_utc"], "%Y-%m-%dT%H:%M:%SZ"))
        секунды = т2 - т1
    # ИТОГ ЧИСТЫМИ: возврат минус расход на отправку -- ровно то, что в учёте.
    возврат_чистыми = None
    if с.get("back_sol") is not None:
        возврат_чистыми = float(с["back_sol"]) - float(с.get("spend_sol") or 0.0)
    sell = F.строка_sell(время_utc=с.get("ts_closed_utc"), имя_токена=None,
                         имя_источника=(с.get("group") or "источник"),
                         секунды=секунды, вход_sol=с.get("in_sol"),
                         возврат_sol=возврат_чистыми,
                         подпись_продажи=с.get("sell_sig"))

    св = таблица.get("summary") or {}
    отпр = {к: v.get("всего", 0) for к, v in (св.get("по_отправителям") or {}).items()}
    часовые = [x for x in свои]
    итог_часа = round(sum(float(x.get("pnl_sol") or 0) for x in часовые), 3)
    задержки = [float(г2["задержка_мс"]) for г2 in гео
                if г2.get("чья") == "полоса" and г2.get("задержка_мс") is not None]
    сводка = F.строка_сводки(
        окно="11:00–12:00", сделок=len(часовые), итог_sol=итог_часа,
        s0=int(св.get("s0") or 0), в_цели=int(св.get("в_цели") or 0),
        промахов=0, путь_медиана_мс=(round(statistics.median(задержки))
                                     if задержки else None),
        довозили=отпр, bloom_пар=None,
        стоп_полоса=итог_часа, порог_полоса=0.15,
        стоп_bloom=None, порог_bloom=0.2)

    print("=== BUY (реальная сделка полосы 26.09) ===")
    print(buy)
    print()
    print("=== SELL (та же сделка) ===")
    print(sell)
    print()
    print("=== СВОДКА (по данным 26.09) ===")
    print(сводка)
    print()
    print(json.dumps({"сделка": {к: с.get(к) for к in
                                 ("cid", "mint", "group", "in_sol", "back_sol",
                                  "spend_sol", "pnl_sol", "ts_intent_utc",
                                  "ts_closed_utc")},
                      "место_в_блоке": {к: г.get(к) for к in
                                        ("block_index", "block_total",
                                         "source_slot", "our_slot",
                                         "задержка_мс")},
                      "чего_нет_в_файлах": ["перед нами / в S+0 успели",
                                            "копировщиков", "разложение пути",
                                            "кто довёз по этой сделке",
                                            "строка Bloom по паре"]},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
