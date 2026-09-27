#!/usr/bin/env python3
"""Что двухшаговый путь полосы сделал В БОЮ -- по журналу решений хоста.

ЗАЧЕМ. Признак жизни показывает только СЧЁТЧИК стадий (by_stage) и последнее
решение. Когда в счётчике появляется two_step_build, надо знать не "сколько
раз", а ПОЧЕМУ не отправилось: нет шаблона первого шага в кэше, шаблон старый,
налог котировки неизвестен, минимум не выдаётся, размер пакета, комиссия пула
выше потолка. Без этого первая живая двухшаговая сделка ждётся наугад.

Только чтение, журналы читаются потоком, в память не грузятся.
"""
import argparse
import calendar
import collections
import gzip
import statistics
import json
import os
import time

# Поля решения, которые нужны для разбора двухшагового пути. Больше не берём:
# журнал большой, а в отчёт уходит только это.
ПОЛЯ = ("ts_utc", "ts", "stage", "ok", "why_not", "route", "code", "reason",
        "mint", "source", "group", "lane_group", "pool_program",
        "leg1_pool_program", "quote_mint", "two_step", "two_step_why_not",
        "min_out", "expected_out", "leg1_min_out", "leg2_amount_in",
        "quote_fee_bps", "size", "signature", "pool_fee_share",
        "leg1_template_age_s", "spend_sol_eq")


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


def файлы(каталог: str, имя: str) -> list:
    из_ = [os.path.join(каталог, имя)]
    из_ += sorted(os.path.join(каталог, ф) for ф in os.listdir(каталог)
                  if ф.startswith(имя + ".") and ф.endswith(".gz"))
    return [п for п in из_ if os.path.exists(п)]


def в_секунды(s: str) -> int:
    return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default=os.environ.get("BLOOM_STATE_DIR")
                   or "/home/bot/bloom_executor_live_data")
    р.add_argument("--since-utc", required=True)
    р.add_argument("--podrobno", type=int, default=12)
    р.add_argument("--out", default="/tmp/dvuhshagovyy_v_zhurnale.json")
    а = р.parse_args()
    порог = в_секунды(а.since_utc)

    двухшаговые = []
    причины = collections.Counter()
    стадии = collections.Counter()
    по_группам_гейт = collections.Counter()
    коды = collections.Counter()
    # Пороги размера источника: по ним видно, ждём мы сигнала или он приходил
    # и не прошёл по размеру.
    вне_размера = []
    # Налоговые отказы: владелец просил время каждого и группу -- по ним видно,
    # действует ли allow_taxed_route после правки.
    налоговые = []
    строк = 0
    for путь in файлы(а.state_dir, "decisions.jsonl"):
        for з in строки(путь):
            строк += 1
            t = з.get("ts")
            if t is None and з.get("ts_utc"):
                try:
                    t = в_секунды(з["ts_utc"])
                except ValueError:
                    t = None
            if t is not None and float(t) < порог:
                continue
            ст = з.get("stage")
            if ст:
                стадии[ст] += 1
            код = з.get("code")
            if код:
                коды[код] += 1
            if код == "SKIP_TAXED_ROUTE" and len(налоговые) < 60:
                налоговые.append({к: з.get(к) for к in
                                  ("ts_utc", "group", "lane_group", "source",
                                   "mint", "why_not", "reason") if з.get(к) is not None})
            if код == "TARGET_AMOUNT_OUT_OF_RANGE" and len(вне_размера) < 40:
                вне_размера.append({к: з.get(к) for к in
                                     ("ts_utc", "source", "group", "lane_group",
                                      "spend_sol_eq", "mint") if з.get(к) is not None})
            если_гейт = (ст == "gate" and з.get("ok") is False)
            if если_гейт:
                по_группам_гейт[str(з.get("why_not"))[:120]] += 1
            двух = (ст == "two_step_build" or з.get("route") == "two_step"
                    or з.get("two_step") is not None
                    or з.get("two_step_why_not") is not None)
            if not двух:
                continue
            зап = {к: з.get(к) for к in ПОЛЯ if з.get(к) is not None}
            двухшаговые.append(зап)
            почему = (зап.get("two_step_why_not")
                      or ((зап.get("two_step") or {}).get("why_not")
                          if isinstance(зап.get("two_step"), dict) else None)
                      or зап.get("why_not") or ("ОТПРАВЛЕНО" if зап.get("signature")
                                                 else "причина не названа"))
            причины[str(почему)[:200]] += 1

    # СКОРОСТЬ "СИГНАЛ -> ОТПРАВКА" ПО ЧАСАМ. Ноль -- приход транзакции
    # источника на наш узел (signal_recv_ts пишет детектор в позицию полосы),
    # конец -- наш sendTransaction (ts_sent). Разбивка по часам нужна, чтобы
    # видеть "до и после правок" без отдельного параметра: правки видны по
    # времени деплоя.
    по_часам: dict = collections.defaultdict(list)
    все_мс = []
    # КАКИЕ ПОЛЯ ВРЕМЕНИ ВООБЩЕ ЕСТЬ. Считаем присутствие каждого кандидата:
    # молча вернуть "0 сделок" и назвать это замером нельзя.
    поля_времени = collections.Counter()
    позиций_полосы = 0
    # Пары "ноль -> конец" по убыванию полноты замера. Первая, где есть оба
    # числа, и идёт в счёт; название пары -- в отчёт, чтобы было видно, ЧТО
    # именно замерено.
    ПАРЫ = (("signal_recv_ts", "ts_sent", "сигнал -> отправка"),
            ("t_recv_ts", "ts_sent", "сигнал -> отправка"),
            ("ts_intent", "ts_sent", "решение -> отправка"),
            ("ts_intent", "ts_accepted", "решение -> приём"))
    чем_мерили = collections.Counter()
    # СТРОКИ ЖУРНАЛА -- ЭТО ОБНОВЛЕНИЯ ОДНОЙ ПОЗИЦИИ, а не готовые записи:
    # ts_intent приходит одной строкой, ts_sent -- другой. Мерить по строке
    # значит не найти ни одной пары (первый прогон так и вышло: 0 сделок при
    # 205 строках с числами). Поэтому сначала сводим строки по cid.
    по_cid: dict = {}
    for путь in файлы(а.state_dir, "positions.jsonl"):
        for з in строки(путь):
            cid = з.get("client_order_id")
            if not cid:
                continue
            зап = по_cid.setdefault(cid, {})
            for к, v in з.items():
                if v is not None:
                    зап[к] = v
    for зап in по_cid.values():
        if not зап.get("lane"):
            continue
        позиций_полосы += 1
        for к in ("signal_recv_ts", "t_recv_ts", "ts_intent", "ts_sent",
                  "ts_accepted", "seen_lag_ms"):
            if isinstance(зап.get(к), (int, float)):
                поля_времени[к] += 1
        for ноль_к, конец_к, имя in ПАРЫ:
            ноль, ушло = зап.get(ноль_к), зап.get(конец_к)
            if not isinstance(ноль, (int, float)) or not isinstance(ушло, (int, float)):
                continue
            if float(ноль) < порог:
                break
            мс = (float(ушло) - float(ноль)) * 1000.0
            if not -1000 < мс < 600_000:
                break
            час = time.strftime("%Y-%m-%dT%HZ", time.gmtime(float(ноль)))
            по_часам[час].append(мс)
            все_мс.append(мс)
            чем_мерили[f"{ноль_к} -> {конец_к} ({имя})"] += 1
            break
    # ДИАГНОСТИКА: какие поля времени реально лежат у СВЕЖИХ позиций полосы.
    свежие = sorted((з for з in по_cid.values() if з.get("lane")),
                    key=lambda з: str(з.get("ts_intent_utc") or ""), reverse=True)[:3]
    примеры_времени = [{к: v for к, v in з.items()
                        if ("ts" in к or "signal" in к or "recv" in к
                            or "lag" in к or к == "lane_group")}
                       for з in свежие]

    скорость = {ч: {"сделок": len(v), "медиана_мс": round(statistics.median(v), 1),
                    "мин_мс": round(min(v), 1), "макс_мс": round(max(v), 1)}
                for ч, v in sorted(по_часам.items())}

    двухшаговые.sort(key=lambda з: str(з.get("ts_utc") or ""))
    отчёт = {
        "since_utc": а.since_utc,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "строк_прочитано": строк,
        "двухшаговых_решений": len(двухшаговые),
        "причины_двухшагового": dict(причины.most_common()),
        "стадии": dict(стадии.most_common(12)),
        "коды": dict(коды.most_common(12)),
        "отказы_гейта": dict(по_группам_гейт.most_common(10)),
        "налоговые_отказы": налоговые,
        "последние": двухшаговые[-а.podrobno:],
        "вне_размера_примеры": вне_размера[:12],
        "скорость_сигнал_отправка_по_часам": скорость,
        "скорость_медиана_мс": (round(statistics.median(все_мс), 1)
                                 if все_мс else None),
        "скорость_сделок": len(все_мс),
        "скорость_чем_мерили": dict(чем_мерили),
        "позиций_полосы_прочитано": позиций_полосы,
        "поля_времени_в_позициях": dict(поля_времени),
        "примеры_времени_свежих": примеры_времени,
    }
    with open(а.out, "w", encoding="utf-8") as ф:
        json.dump(отчёт, ф, ensure_ascii=False, indent=1)
    print(json.dumps({к: v for к, v in отчёт.items()
                      if к not in ("последние", "вне_размера_примеры")},
                     ensure_ascii=False, indent=1))
    for з in отчёт["последние"]:
        print("  ", json.dumps(з, ensure_ascii=False)[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
