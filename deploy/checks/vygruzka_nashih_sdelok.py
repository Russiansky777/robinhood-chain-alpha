#!/usr/bin/env python3
"""Выгрузка НАШИХ сделок (Bloom и полоса) из журнала хоста -- для второй сессии.

Только чтение. Ничего не отправляет, ничего не меняет на хосте.

ЗАЧЕМ ОТДЕЛЬНЫЙ ФАЙЛ, А НЕ nashi_sdelki_iz_zhurnala.py: тот снимок сырой --
он отдаёт поля записи как они лежат, и в нём нет ни адреса источника, ни
группы, ни итога. Вторая сессия читает результат через git и должна получить
готовую таблицу: кошелёк, группа, источник, подписи, слот, вход/возврат/расход,
итог, причина закрытия.

ТРИ ВЕЩИ, КОТОРЫЕ ЗДЕСЬ НЕЛЬЗЯ ДЕЛАТЬ ИНАЧЕ:

1. Адрес источника в позиции НЕ ХРАНИТСЯ вовсе (bloom_exec_state.write_intent:
   там есть source_sig, но не адрес). Он берётся из decisions.jsonl по подписи
   источника -- и только по ней. Выдумывать адрес нельзя.
2. Итог считается ТОЙ ЖЕ функцией, что и суточный счёт службы
   (bloom_exec_state.итог_позиции), взятой с хоста по пути. Своя копия формулы
   разъехалась бы с боевой, и числа в отчёте перестали бы быть числами службы.
   Если запись уже содержит pnl_counted_sol -- он и идёт в поле как есть.
3. Ни ключей, ни URL в файле: каждая строка проходит через вычистку. Причина
   закрытия часто приходит из строки ошибки, а в ней бывал полный адрес узла
   с api-key -- один такой случай 26.09 уже был.

Журнал в память не читается: два потоковых прохода по positions.jsonl и один
по decisions.jsonl, и в памяти только последнее состояние по позиции.
"""
from __future__ import annotations

import argparse
import calendar
import gzip
import importlib.util
import json
import os
import re
import sys
import time

ЛАМПОРТОВ_В_SOL = 1_000_000_000
# Подпись Solana в base58 -- 86-88 знаков. Короче 80 не подпись, а метка вроде
# "ok"/"sent": такие в поля подписей писать нельзя (правка 26.09 по продавцу).
МИН_ПОДПИСЬ = 80

# ПОЛЯ ПОЗИЦИИ, которые нужны для таблицы. Всё остальное не держим.
ПОЛЯ = (
    "client_order_id", "mint", "wallet", "lane", "lane_group", "mode", "state",
    "source_sig", "source_slot", "sol_in", "ts_intent", "ts_intent_utc",
    "ts_closed", "signature", "signatures", "lane_signature",
    "lane_landed_signature", "lane_landed_slot", "our_slot", "slot",
    "lane_buy_native_sol", "lane_buy_fee_sol", "lane_tips_total_sol",
    "lane_priority_lamports", "qty", "chain_ok", "chain_err",
    "closed_sol_net", "closed_sol_delta", "closed_signature",
    "last_sell_reported", "last_sell_signatures", "last_sell_outcome",
    "pnl_counted", "pnl_counted_sol", "pnl_counted_spend_sol",
    "pnl_count_why_not", "result_uncountable", "uncountable_shared_mint",
    "closed_reason", "close_reason", "why_not", "sell_reason",
)

_URL = re.compile(r"(?i)\b(?:https?|wss?)://\S+")
_КЛЮЧ = re.compile(r"(?i)(api[-_]?key|api[-_]?token|bearer)\s*[=:]\s*\S+")


def чисто(x):
    """Строка без URL и без ключей. Числа и None проходят как есть."""
    if x is None or isinstance(x, (int, float, bool)):
        return x
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
    s = _URL.sub("<url-vycishcheno>", s)
    s = _КЛЮЧ.sub("<klyuch-vycishcheno>", s)
    return s


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


def пути_журнала(каталог: str, имя: str) -> list:
    """Живой журнал плюс его сжатые обороты, от старых к новым."""
    основной = os.path.join(каталог, имя)
    обороты = []
    try:
        for ф in sorted(os.listdir(каталог)):
            if ф.startswith(имя + ".") and ф.endswith(".gz"):
                обороты.append(os.path.join(каталог, ф))
    except OSError:
        pass
    return обороты + ([основной] if os.path.exists(основной) else [])


def окно(момент: str) -> float:
    """UTC -> секунды. calendar.timegm, НЕ mktime: хост живёт по CEST и
    mktime сдвигает окно на час-два (ошибка 26.09 в дневных счётчиках)."""
    return calendar.timegm(time.strptime(момент, "%Y-%m-%dT%H:%M:%SZ"))


def учёт_хоста(корень: str):
    """Функции учёта с ХОСТА (итог, расход) или (None, None, почему)."""
    путь = os.path.join(корень, "analysis", "bloom_exec_state.py")
    if not os.path.exists(путь):
        return None, None, f"нет файла {путь}"
    try:
        спец = importlib.util.spec_from_file_location("bloom_exec_state_host", путь)
        м = importlib.util.module_from_spec(спец)
        спец.loader.exec_module(м)
        return м.итог_позиции, м.расход_отправки, None
    except Exception as exc:  # noqa: BLE001
        return None, None, f"{type(exc).__name__}: {чисто(str(exc))[:160]}"


def подпись_покупки(п: dict) -> tuple:
    """(подпись, откуда). Только севшая: у полосы это lane_landed_signature."""
    for поле in ("lane_landed_signature", "closed_buy_signature"):
        з = п.get(поле)
        if isinstance(з, str) and len(з) >= МИН_ПОДПИСЬ:
            return з, поле
    з = п.get("signature")
    if isinstance(з, str) and len(з) >= МИН_ПОДПИСЬ:
        return з, "signature"
    сп = п.get("signatures")
    if isinstance(сп, list):
        for з in сп:
            if isinstance(з, str) and len(з) >= МИН_ПОДПИСЬ:
                return з, "signatures"
    з = п.get("lane_signature")
    if isinstance(з, str) and len(з) >= МИН_ПОДПИСЬ:
        return з, "lane_signature"
    return None, None


def подпись_продажи(п: dict) -> tuple:
    """(подпись, откуда). Сторож полосы кладёт её в last_sell_*, а НЕ в
    closed_signature -- искать только там значит потерять все продажи полосы."""
    з = п.get("closed_signature")
    if isinstance(з, str) and len(з) >= МИН_ПОДПИСЬ:
        return з, "closed_signature"
    о = п.get("last_sell_reported")
    if isinstance(о, dict):
        for поле in ("signature", "sig"):
            з = о.get(поле)
            if isinstance(з, str) and len(з) >= МИН_ПОДПИСЬ:
                return з, f"last_sell_reported.{поле}"
    elif isinstance(о, str) and len(о) >= МИН_ПОДПИСЬ:
        return о, "last_sell_reported"
    сп = п.get("last_sell_signatures")
    if isinstance(сп, list):
        for з in сп:
            if isinstance(з, str) and len(з) >= МИН_ПОДПИСЬ:
                return з, "last_sell_signatures"
    и = п.get("last_sell_outcome")
    if isinstance(и, dict):
        з = и.get("signature")
        if isinstance(з, str) and len(з) >= МИН_ПОДПИСЬ:
            return з, "last_sell_outcome.signature"
    return None, None


def возврат(п: dict):
    з = п.get("closed_sol_net")
    if з is None:
        з = (п.get("last_sell_outcome") or {}).get("sol_delta_net")
    return None if з is None else float(з)


def причина_закрытия(п: dict):
    for поле in ("closed_reason", "close_reason", "sell_reason", "chain_err",
                 "why_not", "pnl_count_why_not"):
        з = п.get(поле)
        if з:
            return чисто(str(з))[:200], поле
    if п.get("state") and п["state"] != "closed":
        return None, None
    return None, None


def собрать(поз: dict, итог_фн, расход_фн) -> dict:
    б, откуда_б = подпись_покупки(поз)
    пр, откуда_пр = подпись_продажи(поз)
    прич, откуда_прич = причина_закрытия(поз)
    вход = поз.get("sol_in")
    вход = None if вход is None else float(вход)
    вз = возврат(поз)

    итог = поз.get("pnl_counted_sol")
    расход = поз.get("pnl_counted_spend_sol")
    источник_итога = "запись (pnl_counted)" if итог is not None else None
    if итог is None and итог_фн is not None:
        try:
            и_, р_ = итог_фн(поз)
        except Exception as exc:  # noqa: BLE001
            и_, р_ = None, None
            источник_итога = f"схема учёта отказала: {type(exc).__name__}"
        else:
            if и_ is not None:
                итог, источник_итога = и_, "itog_pozicii с хоста"
            if расход is None and р_ is not None:
                расход = р_
    if расход is None and расход_фн is not None:
        try:
            расход = расход_фн(поз)
        except Exception:  # noqa: BLE001
            расход = None

    return {
        "cid": поз.get("client_order_id"),
        "wallet": поз.get("wallet"),
        "group": поз.get("lane_group") or поз.get("_group_iz_resheniy"),
        "side": "lane" if поз.get("lane") else "bloom",
        "mint": поз.get("mint"),
        "source": поз.get("_source_iz_resheniy"),
        "source_sig": поз.get("source_sig") or None,
        "source_slot": поз.get("source_slot"),
        "entry_slot": (поз.get("lane_landed_slot") or поз.get("our_slot")
                       or поз.get("slot")),
        "buy_sig": б,
        "buy_sig_field": откуда_б,
        "buy_landed": поз.get("chain_ok"),
        "sell_sig": пр,
        "sell_sig_field": откуда_пр,
        "in_sol": None if вход is None else round(вход, 9),
        "back_sol": None if вз is None else round(вз, 9),
        "spend_sol": None if расход is None else round(float(расход), 9),
        "pnl_sol": None if итог is None else round(float(итог), 9),
        "pnl_source": источник_итога,
        "close_reason": прич,
        "close_reason_field": откуда_прич,
        "state": поз.get("state"),
        "mode": поз.get("mode"),
        "ts_intent_utc": поз.get("ts_intent_utc"),
        "ts_closed_utc": (time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                        time.gmtime(float(поз["ts_closed"])))
                          if поз.get("ts_closed") else None),
        "qty": поз.get("qty"),
    }


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default=os.environ.get("BLOOM_STATE_DIR")
                   or "/home/bot/bloom_executor_live_data")
    р.add_argument("--repo-root", default="/home/bot/robinhood-chain-alpha",
                   help="откуда брать боевую схему учёта (itog_pozicii)")
    р.add_argument("--since-utc", default="2026-09-24T00:00:00Z")
    р.add_argument("--out", default="/tmp/nashi_sdelki_host.json")
    р.add_argument("--self-test", action="store_true")
    р.add_argument("--proverit", default=None,
                   help="проверить готовый файл выгрузки и напечатать числа")
    а = р.parse_args()
    if а.self_test:
        return самопроверка()
    if а.proverit:
        return проверить(а.proverit)

    порог = окно(а.since_utc)
    итог_фн, расход_фн, почему_нет_схемы = учёт_хоста(а.repo_root)

    # ПРОХОД 1: позиции потоком, по cid -- последнее состояние.
    по_cid: dict = {}
    строк = 0
    for путь in пути_журнала(а.state_dir, "positions.jsonl"):
        for з in строки(путь):
            строк += 1
            cid = з.get("client_order_id")
            if not cid:
                continue
            по_cid.setdefault(cid, {}).update(
                {к: v for к, v in з.items() if к in ПОЛЯ and v is not None})

    в_окне = {cid: п for cid, п in по_cid.items()
              if float(п.get("ts_intent") or 0) >= порог}
    нужные_подписи = {п.get("source_sig") for п in в_окне.values() if п.get("source_sig")}

    # ПРОХОД 2: решения -- только адрес источника и группа, и только по нашим
    # подписям. Держать весь журнал решений в памяти незачем.
    по_подписи: dict = {}
    строк_решений = 0
    for путь in пути_журнала(а.state_dir, "decisions.jsonl"):
        for з in строки(путь):
            строк_решений += 1
            п = з.get("signature")
            if not п or п not in нужные_подписи:
                continue
            зп = по_подписи.setdefault(п, {})
            if з.get("source") and not зп.get("source"):
                зп["source"] = з["source"]
            if з.get("group") and not зп.get("group"):
                зп["group"] = з["group"]

    сделки = []
    for cid, п in в_окне.items():
        нашли = по_подписи.get(п.get("source_sig") or "", {})
        п["_source_iz_resheniy"] = нашли.get("source")
        п["_group_iz_resheniy"] = нашли.get("group")
        сделки.append(собрать(п, итог_фн, расход_фн))
    сделки.sort(key=lambda с: (с.get("ts_intent_utc") or ""))

    с_итогом = sum(1 for с in сделки if с.get("pnl_sol") is not None)
    свод = {
        "state_dir": а.state_dir,
        "since_utc": а.since_utc,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "positions_rows_read": строк,
        "decisions_rows_read": строк_решений,
        "positions_total": len(по_cid),
        "exported": len(сделки),
        "with_pnl": с_итогом,
        "lane": sum(1 for с in сделки if с["side"] == "lane"),
        "bloom": sum(1 for с in сделки if с["side"] == "bloom"),
        "with_source_address": sum(1 for с in сделки if с.get("source")),
        "with_group": sum(1 for с in сделки if с.get("group")),
        "with_buy_sig": sum(1 for с in сделки if с.get("buy_sig")),
        "with_sell_sig": sum(1 for с in сделки if с.get("sell_sig")),
        "closed": sum(1 for с in сделки if с.get("state") == "closed"),
        "pnl_schema": "хост" if итог_фн is not None else "нет",
        "pnl_schema_why_not": почему_нет_схемы,
    }
    with open(а.out, "w", encoding="utf-8") as ф:
        json.dump({"svod": свод, "sdelki": сделки}, ф, ensure_ascii=False, indent=1)
    print(json.dumps(свод, ensure_ascii=False, indent=1))
    return 0


# ------------------------------------------------------------------ проверка
# ЗАЧЕМ ОТДЕЛЬНЫМ РЕЖИМОМ, А НЕ ВСТАВКОЙ В YAML: многострочный питон внутри
# прогона (heredoc) ломает разбор workflow_dispatch -- на этом уже стояли.
ЗАПРЕЩЕНО = ((r"(?i)https?://", "URL"), (r"(?i)wss?://", "URL"),
             (r"(?i)api[-_]?key", "api-key"), (r"(?i)PRIVATE KEY", "ключ"),
             (r"(?i)helius|shyft", "имя узла"))


def проверить(путь: str) -> int:
    """Файл выгрузки: числа на месте, ключей и URL нет. Иначе не коммитить."""
    текст = open(путь, encoding="utf-8").read()
    д = json.loads(текст)
    с = д["svod"]
    плохо = sorted({что for обр, что in ЗАПРЕЩЕНО if re.search(обр, текст)})
    if плохо:
        print("СБОЙ: в файле нашлось " + ", ".join(плохо))
        return 1
    print(json.dumps(с, ensure_ascii=False, indent=1))
    print(f"ЧИСЛО: позиций выгружено {с['exported']}, из них с итогом {с['with_pnl']}")
    if not с["exported"]:
        print("СБОЙ: выгружено ноль позиций")
        return 1
    return 0


# --------------------------------------------------------------- самопроверка
def самопроверка() -> int:
    """Только денежный путь: подписи, суммы, итог, вычистка ключей."""
    пройдено = провалено = 0

    def ок(условие, что):
        nonlocal пройдено, провалено
        if условие:
            пройдено += 1
        else:
            провалено += 1
            print(f"ПРОВАЛ: {что}")

    длинная = "S" * 88
    другая = "T" * 88
    ок(подпись_покупки({"lane_landed_signature": длинная})[0] == длинная,
       "севшая подпись полосы берётся")
    ок(подпись_покупки({"signature": "ok"})[0] is None,
       "короткая метка не подпись")
    ок(подпись_покупки({"signatures": ["ok", длинная]})[0] == длинная,
       "подпись из списка")
    ок(подпись_продажи({"last_sell_reported": {"signature": длинная}})[0] == длинная,
       "продажа полосы из last_sell_reported")
    ок(подпись_продажи({"closed_signature": другая,
                        "last_sell_signatures": [длинная]})[0] == другая,
       "closed_signature приоритетнее")
    ок(подпись_продажи({})[0] is None, "нет продажи -- нет подписи")
    ок(возврат({"last_sell_outcome": {"sol_delta_net": 0.0123}}) == 0.0123,
       "возврат из last_sell_outcome")
    ок(возврат({"closed_sol_net": 0.5, "last_sell_outcome": {"sol_delta_net": 9}}) == 0.5,
       "closed_sol_net приоритетнее")
    ок(возврат({}) is None, "неизвестный возврат остаётся неизвестным")

    # Итог: если он уже посчитан службой -- берётся как есть, без пересчёта.
    с = собрать({"client_order_id": "c1", "sol_in": 0.05, "pnl_counted": True,
                 "pnl_counted_sol": -0.004, "pnl_counted_spend_sol": 0.0011,
                 "closed_sol_net": 0.047, "state": "closed"},
                lambda п: (999.0, 999.0), lambda п: 999.0)
    ок(с["pnl_sol"] == -0.004 and с["spend_sol"] == 0.0011,
       "посчитанный службой итог не пересчитывается")
    ок(с["in_sol"] == 0.05 and с["back_sol"] == 0.047, "вход и возврат на месте")

    # Итог не посчитан -- считает схема хоста, а не своя формула.
    с2 = собрать({"client_order_id": "c2", "sol_in": 0.05, "closed_sol_net": 0.047,
                  "state": "closed"},
                 lambda п: (-0.0041, 0.0011), lambda п: 0.0011)
    ок(с2["pnl_sol"] == -0.0041 and с2["pnl_source"] == "itog_pozicii с хоста",
       "итог из схемы хоста")
    с3 = собрать({"client_order_id": "c3", "sol_in": 0.05, "state": "bought"},
                 lambda п: (None, 0.0009), lambda п: 0.0009)
    ок(с3["pnl_sol"] is None and с3["spend_sol"] == 0.0009,
       "нет итога -- пусто, а расход известен")

    # Вычистка: ни ключа, ни URL в файле.
    грязь = ("HTTPError: https://mainnet.helius-rpc.com/?api-key=00000000-1111-"
             "2222-3333-444444444444 отдал 429")
    вычищено = чисто(грязь)
    ок("helius" not in вычищено and "api-key" not in вычищено,
       "URL с ключом вычищен")
    ок(чисто("api_key: abcdef") == "<klyuch-vycishcheno>", "ключ по имени вычищен")
    с4 = собрать({"client_order_id": "c4", "state": "closed", "chain_err": грязь},
                 None, None)
    ок("api-key" not in json.dumps(с4, ensure_ascii=False),
       "причина закрытия идёт через вычистку")
    ок(причина_закрытия({"closed_reason": "stop", "why_not": "x"})[0] == "stop",
       "причина закрытия -- первая известная")

    # Окно: UTC без сдвига локального часового пояса.
    ок(окно("2026-09-24T00:00:00Z") == 1790208000, "окно считается по UTC")

    # Проверка файла: грязный файл коммитить нельзя, чистый -- можно.
    import tempfile
    with tempfile.TemporaryDirectory() as кат:
        чистый = os.path.join(кат, "chisto.json")
        with open(чистый, "w", encoding="utf-8") as ф:
            json.dump({"svod": {"exported": 2, "with_pnl": 1}, "sdelki": []}, ф)
        ок(проверить(чистый) == 0, "чистый файл проходит проверку")
        пустой = os.path.join(кат, "pusto.json")
        with open(пустой, "w", encoding="utf-8") as ф:
            json.dump({"svod": {"exported": 0, "with_pnl": 0}, "sdelki": []}, ф)
        ок(проверить(пустой) == 1, "ноль позиций -- это сбой")
        грязный = os.path.join(кат, "gryaz.json")
        with open(грязный, "w", encoding="utf-8") as ф:
            json.dump({"svod": {"exported": 1, "with_pnl": 1},
                       "sdelki": [{"close_reason": "https://node/?api-key=X"}]}, ф)
        ок(проверить(грязный) == 1, "файл с URL и ключом не проходит")

    print(f"самопроверка выгрузки сделок: {пройдено}/{пройдено + провалено} пройдено")
    return 1 if провалено else 0


if __name__ == "__main__":
    raise SystemExit(main())
