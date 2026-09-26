#!/usr/bin/env python3
"""Что сделал НАШ детектор на тех же сигналах, где DBot закрыл сделку.

ЗАЧЕМ (задание владельца 26.09, 21:50). За 26.09 DBot по BATCH-3/5 закрыл 22
сделки с плюсом, а наша полоса по тем же источникам взяла одну. Вопрос не
"сколько мы потеряли", а "что именно нас остановило на каждом из 22 сигналов":
не увидели / рубильник / порог / STALE / повтор минта / предел позиций / отказ
сборки (тип пула, котировка) / купил Bloom вместо полосы / полоса купила.

КАК ИЩЕТСЯ. Подписи транзакции источника в журнале DBot нет (в links лежит
ЕГО транзакция, не источника), поэтому сигнал ищется по МИНТУ в нашем журнале
решений: минт за сутки встречается один-два раза, и путаницы это не даёт.
Рядом печатается время нашего решения и время сделки DBot -- видно, один это
сигнал или разные.

Только чтение, журналы читаются потоком.
"""
import argparse
import calendar
import gzip
import json
import os
import re
import sys
import time

МИН_ПОДПИСЬ = 80


def строки(путь: str):
    откр = gzip.open if путь.endswith(".gz") else open
    with откр(путь, "rt", encoding="utf-8", errors="replace") as ф:
        for ln in ф:
            ln = ln.strip()
            if ln.startswith("{"):
                try:
                    yield json.loads(ln)
                except ValueError:
                    continue


def пути(каталог: str, имя: str) -> list:
    основной = os.path.join(каталог, имя)
    обороты = []
    try:
        for ф in sorted(os.listdir(каталог)):
            if ф.startswith(имя + ".") and ф.endswith(".gz"):
                обороты.append(os.path.join(каталог, ф))
    except OSError:
        pass
    return обороты + ([основной] if os.path.exists(основной) else [])


# Причины отказа приводятся к КОРОТКИМ именам -- ровно те, что назвал владелец.
ПРИЧИНЫ = (
    (r"(?i)KILL|рубильник", "рубильник"),
    (r"(?i)порог|min_target|меньше цели|мелк", "порог источника"),
    (r"(?i)STALE|устар|возраст", "STALE"),
    (r"(?i)повтор|уже покупали|DUPLICATE|дубл", "повтор минта"),
    (r"(?i)предел полосы|открытых позиций|предел позиций", "предел позиций"),
    (r"(?i)котировка пула не SOL|котировка не SOL", "отказ сборки: котировка не SOL"),
    (r"(?i)тип пула|пула вне полосы|вне полосы", "отказ сборки: тип пула"),
    (r"(?i)lane_trades|полоса не торгует", "группа без полосы"),
    (r"(?i)потолок расхода|оборот", "потолок оборота"),
    (r"(?i)убыток полосы|стоп", "стоп убытка"),
    (r"(?i)по этой группе|группа источника", "группа"),
)


def короткая_причина(текст: str) -> str:
    т = str(текст or "")
    for обр, имя in ПРИЧИНЫ:
        if re.search(обр, т):
            return имя
    return т[:60] or "не названа"


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--dbot", default="data/dbot_dni.json")
    р.add_argument("--day", default="2026-09-26")
    р.add_argument("--out", default="/tmp/chto_sdelal_detektor.json")
    а = р.parse_args()

    д = json.load(open(а.dbot, encoding="utf-8"))
    цели = []
    for ряд in д.get("tablica") or []:
        if ряд.get("day_madrid") != а.day:
            continue
        for с in ряд.get("sdelki") or []:
            цели.append({"mint": с.get("mint"), "wallet_dbot": ряд.get("wallet"),
                         "config": ",".join(ряд.get("configs") or []),
                         "itog_dbot_sol": с.get("itog_sol"),
                         "vhod_dbot_sol": с.get("vhod_sol"),
                         "itog_dbot_pct": с.get("itog_pct"),
                         "utc_dbot": с.get("utc"), "ts_dbot": с.get("ts")})
    минты = {ц["mint"] for ц in цели if ц.get("mint")}
    if not минты:
        print("СТОП: в файле DBot нет закрытых сделок за этот день", file=sys.stderr)
        return 2

    # РЕШЕНИЯ по этим минтам -- потоком, только нужные строки.
    решения: dict = {м: [] for м in минты}
    строк = 0
    for путь in пути(а.state_dir, "decisions.jsonl"):
        for з in строки(путь):
            строк += 1
            м = з.get("mint")
            if м in решения:
                решения[м].append({к: з.get(к) for к in
                                   ("stage", "action", "why_not", "code", "signature",
                                    "source", "group", "net_slot_age_s", "t_recv_utc",
                                    "lane", "sent", "decision", "reason", "skip_code",
                                    "lane_allowed", "why")})
    # ПОЗИЦИИ по этим минтам: кто в итоге купил -- полоса или площадка.
    позиции: dict = {м: [] for м in минты}
    for путь in пути(а.state_dir, "positions.jsonl"):
        for з in строки(путь):
            м = з.get("mint")
            cid = з.get("client_order_id")
            if м in позиции and cid:
                позиции[м].append({"cid": cid, "lane": з.get("lane"),
                                   "state": з.get("state"),
                                   "sol_in": з.get("sol_in"),
                                   "pnl": з.get("pnl_counted_sol"),
                                   "group": з.get("lane_group"),
                                   "ts": з.get("ts_intent")})

    ряды = []
    for ц in цели:
        м = ц["mint"]
        реш = решения.get(м) or []
        поз = позиции.get(м) or []
        полоса = [п for п in поз if п.get("lane")]
        площадка = [п for п in поз if not п.get("lane")]
        # ПРИЧИНА ПОЛОСЫ ИЩЕТСЯ ВСЕГДА, даже когда минт купил Bloom: вопрос
        # владельца -- что мешало ПОЛОСЕ, а покупка площадки её не отменяет
        # (они идут по одному сигналу и не исключают друг друга).
        отказы_полосы = []
        for з in реш:
            стадия = str(з.get("stage") or "")
            текст = (з.get("why_not") or з.get("why") or з.get("reason") or "")
            if not текст:
                continue
            если_полоса = (стадия in ("gate", "build", "send", "sim", "own_send")
                           or "lane" in стадия or "полос" in текст.lower())
            if если_полоса:
                отказы_полосы.append((стадия, короткая_причина(текст)))
        причина_полосы = отказы_полосы[0][1] if отказы_полосы else None
        что, причина = None, None
        if not реш and not поз:
            что = "не увидел"
        elif полоса:
            что = "полоса купила"
        elif площадка:
            что = "купил Bloom вместо полосы"
            причина = причина_полосы or "у полосы отказа в журнале нет"
        else:
            причины = [короткая_причина(з.get("why_not") or з.get("why")
                                         or з.get("reason") or z_код(з))
                       for з in реш if (з.get("why_not") or з.get("why")
                                        or з.get("reason") or z_код(з))]
            что = "отказ"
            причина = причина_полосы or (причины[0] if причины
                                          else "причина в журнале не названа")
        ряды.append({**ц, "решений_в_журнале": len(реш), "позиций": len(поз),
                     "что_сделали": что, "причина": причина,
                     "отказы_полосы": отказы_полосы[:4],
                     "все_причины": sorted({короткая_причина(з.get("why_not")
                                                             or з.get("why") or "")
                                            for з in реш
                                            if (з.get("why_not") or з.get("why"))})[:6],
                     "наше_время": (реш[0].get("t_recv_utc") if реш else None),
                     "стадии": sorted({(з.get("stage") or "") for з in реш})[:6],
                     "группа": next((з.get("group") for з in реш if з.get("group")),
                                    None)})

    свод: dict = {}
    for р2 in ряды:
        ключ = р2["что_сделали"] + (f": {р2['причина']}" if р2.get("причина") else "")
        свод[ключ] = свод.get(ключ, 0) + 1
    # ЧТО МЕШАЛО ПОЛОСЕ -- отдельным счётом, независимо от того, купил ли Bloom.
    свод_полосы: dict = {}
    for р2 in ряды:
        если = ("взяла" if р2["что_сделали"] == "полоса купила"
                else (р2.get("причина") or "причина не названа"))
        свод_полосы[если] = свод_полосы.get(если, 0) + 1
    итог = {"снято_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "день": а.day, "сделок_dbot": len(ряды),
            "строк_решений_прочитано": строк, "свод": свод,
            "что_мешало_полосе": свод_полосы, "ряды": ряды}
    with open(а.out, "w", encoding="utf-8") as ф:
        json.dump(итог, ф, ensure_ascii=False, indent=1)
    print(json.dumps({к: v for к, v in итог.items() if к != "ряды"},
                     ensure_ascii=False, indent=1))
    print("--- по сделкам ---")
    for р2 in ряды:
        print(" | ".join(str(x) for x in (
            (р2["mint"] or "")[:10], р2["config"], р2["utc_dbot"],
            р2["itog_dbot_sol"], р2["что_сделали"], р2["причина"] or "",
            р2["решений_в_журнале"], р2["группа"] or "")))
    return 0


def z_код(з: dict):
    """Код решения, если он есть под любым из известных имён."""
    return з.get("code") or з.get("skip_code") or з.get("decision")


if __name__ == "__main__":
    raise SystemExit(main())
