#!/usr/bin/env python3
"""Выгрузка сделок полосы с ПОЛНЫМИ минтами и подписями (только чтение).

ЗАЧЕМ. В таблице полосы (data/lane_table.json) минты и подписи урезаны до
десяти-шестнадцати знаков -- по ним в цепь не сходишь. Замер толпы за источником
требует полных значений, поэтому они берутся из журнала позиций на хосте.

Журнал читается ПОТОКОМ, включая ротированные .gz. Ни ключей, ни сети.
"""
from __future__ import annotations

import argparse
import calendar
import glob
import gzip
import json
import os
import sys
import time

МЕТКА_ПОЛОСЫ = "own_send"


def метка(текст: str) -> float:
    т = текст.replace("Z", "").replace("z", "")
    for формат in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return calendar.timegm(time.strptime(т, формат))
        except ValueError:
            continue
    raise SystemExit(f"СТОП: метку {текст!r} не разобрать")


def строки(путь: str):
    откр = gzip.open if путь.endswith(".gz") else open
    try:
        with откр(путь, "rt", encoding="utf-8", errors="replace") as ф:
            for с in ф:
                с = с.strip()
                if с:
                    yield с
    except OSError as exc:
        print(f"ПРЕДУПРЕЖДЕНИЕ: {путь} ({type(exc).__name__})", file=sys.stderr)


def файлы(каталог: str) -> list:
    из_ = sorted(glob.glob(os.path.join(каталог, "positions.jsonl*")))
    осн = os.path.join(каталог, "positions.jsonl")
    if осн in из_:
        из_.remove(осн)
        из_.append(осн)
    return из_


def наши_подписи(п: dict) -> list:
    """Все подписи, под которыми наша покупка могла сесть: вариант на nonce даёт
    по подписи на отправителя, а сядет ровно одна."""
    из_ = []
    for поле in ("lane_signature", "lane_signature_local"):
        зн = п.get(поле)
        if isinstance(зн, str) and зн:
            из_.append(зн)
    for зн in (п.get("lane_pool_candidates") or []):
        if isinstance(зн, str) and зн:
            из_.append(зн)
    for зн in (п.get("signatures") or []):
        if isinstance(зн, str) and зн:
            из_.append(зн)
    видели, ответ = set(), []
    for зн in из_:
        if зн not in видели:
            видели.add(зн)
            ответ.append(зн)
    return ответ


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--s", default="2026-09-25T18:00", help="с какой метки UTC")
    р.add_argument("--do", default="", help="до какой метки UTC (пусто -- без предела)")
    р.add_argument("--polnye", action="store_true",
                   help="выложить ВСЕ поля записи позиции, а не отобранные "
                         "(слово владельца 28.09, п.5: выгрузка для Code-2)")
    р.add_argument("--out", default="")
    а = р.parse_args()
    с_ = метка(а.s)
    # ВЕРХНЯЯ ГРАНИЦА ОКНА. Суточная выгрузка просит ровно сутки, и без предела
    # сверху в файл за 27->28 попали бы и сделки следующего дня.
    до_ = метка(а.do) if а.do else None
    по_cid: dict = {}
    просмотрено = 0
    # ДВА ПРОХОДА, И ЭТО НЕ ЛИШНЕЕ. Дописки позиции (state=closed,
    # closed_sol_net, итог продажи) приходят ОТДЕЛЬНЫМИ строками, и в них нет
    # ни метки полосы, ни ts_intent -- только client_order_id и изменённые поля.
    # Один проход с фильтром по метке отбрасывал их, и в выгрузке 27.09 у всех
    # 152 сделок итог вышел пустым, хотя в журнале он есть. Первый проход
    # собирает cid наших сделок окна, второй -- все их поля.
    свои_cid: set = set()
    for путь in файлы(а.state_dir):
        for с in строки(путь):
            просмотрено += 1
            try:
                з = json.loads(с)
            except ValueError:
                continue
            if not isinstance(з, dict) or з.get("lane") != МЕТКА_ПОЛОСЫ:
                continue
            т = з.get("ts_intent") or з.get("ts_sent")
            if not isinstance(т, (int, float)) or float(т) < с_:
                continue
            if до_ is not None and float(т) >= до_:
                continue
            if з.get("client_order_id"):
                свои_cid.add(з["client_order_id"])
    for путь in файлы(а.state_dir):
        for с in строки(путь):
            try:
                з = json.loads(с)
            except ValueError:
                continue
            if not isinstance(з, dict):
                continue
            cid = з.get("client_order_id")
            if not cid or cid not in свои_cid:
                continue
            # Позиция дописывается много раз: накапливаем поля, а не заменяем --
            # часть полей приходит позже.
            в = по_cid.setdefault(cid, {})
            for к, зн in з.items():
                if зн is not None:
                    в[к] = зн
    ряды = []
    for cid, п in sorted(по_cid.items()):
        ряды.append({
            "cid": cid,
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                  time.gmtime(float(п.get("ts_intent") or 0))),
            "group": п.get("lane_group"),
            "mint": п.get("mint"),
            "size_sol": п.get("sol_in"),
            "source": п.get("source"),
            "source_sig": п.get("source_sig"),
            "source_slot": п.get("source_slot"),
            "our_slot": п.get("own_tx_seen_slot") or п.get("our_slot"),
            "our_signatures": наши_подписи(п),
            "our_block_index": п.get("block_index"),
            "source_block_index": п.get("source_block_index"),
            "wallet": п.get("lane_wallet"),
            "chain_ok": п.get("chain_ok"),
            # ДЕНЬГИ СДЕЛКИ -- сырыми полями плюс итог тем же счётом, каким
            # считает служба (ST.итог_позиции): иначе таблица толпы и все
            # прочие доклады считали бы итог по-разному.
            "sol_in": п.get("sol_in"),
            "closed_sol_net": п.get("closed_sol_net"),
            "tips_sol": п.get("lane_tips_total_sol"),
            "priority_lamports": п.get("lane_priority_lamports"),
            "state": п.get("state"),
            "closed_reason": п.get("closed_reason"),
            "итог_sol": None, "расход_sol": None,
            # ПОЛЯ, КОТОРЫЕ ПРОСИЛ ВЛАДЕЛЕЦ ОТДЕЛЬНО (п.5, 28.09): подписи
            # покупки и продажи и СЕВШАЯ подпись со своим слотом. Севшая --
            # это та, что действительно легла в блок; в lane_signature может
            # стоять вариант, который не сел.
            "buy_sig": (п.get("lane_landed_signature")
                        or п.get("lane_signature")
                        or п.get("lane_signature_local")),
            "sell_sig": ((п.get("last_sell_signatures") or [None])[-1]
                          if isinstance(п.get("last_sell_signatures"), list)
                          else п.get("jup_signature")),
            "landed_sig": п.get("lane_landed_signature"),
            "landed_slot": п.get("lane_landed_slot"),
            "token_name": п.get("token_name"),
            "source_name": п.get("source_name"),
            # ВСЕ ПОЛЯ ЗАПИСИ -- по явному запросу. Без этого вторая сессия
            # видит только отобранное, и каждое новое поле требует правки
            # выгрузки.
            **({"zapis": dict(п)} if а.polnye else {}),
        })
    # ИТОГ -- ТЕМ ЖЕ МОДУЛЕМ, ЧТО У СЛУЖБЫ. Порядок каталогов важен: служба
    # работает из /home/bot/bloom_executor, а рядом лежит возможно устаревшая
    # выписка репозитория.
    учёт = None
    for кат in ("/home/bot/bloom_executor",
                "/home/bot/robinhood-chain-alpha/analysis",
                os.path.join(os.path.dirname(os.path.dirname(
                    os.path.dirname(os.path.abspath(__file__)))), "analysis")):
        if not os.path.exists(os.path.join(кат, "bloom_exec_state.py")):
            continue
        if кат not in sys.path:
            sys.path.insert(0, кат)
        try:
            import importlib  # noqa: PLC0415

            учёт = importlib.import_module("bloom_exec_state")
            print(f"учёт из {учёт.__file__}")
            break
        except Exception as exc:  # noqa: BLE001
            print(f"учёт из {кат} не загружен: {type(exc).__name__}", file=sys.stderr)
    if учёт is not None and hasattr(учёт, "итог_позиции"):
        по_cid_все = по_cid
        for ряд in ряды:
            поз = по_cid_все.get(ряд["cid"]) or {}
            try:
                итог, расход = учёт.итог_позиции(поз)
            except Exception as exc:  # noqa: BLE001
                ряд["итог_почему"] = f"{type(exc).__name__}"
                continue
            ряд["итог_sol"] = (round(итог, 9) if итог is not None else None)
            ряд["расход_sol"] = round(расход, 9)

    свод = {"снято_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "с": а.s, "до": а.do or None, "полные_поля": bool(а.polnye),
             "строк_просмотрено": просмотрено, "сделок": len(ряды),
             "с_севшей_подписью": sum(1 for р_ in ряды if р_.get("landed_sig")),
             "с_именем_токена": sum(1 for р_ in ряды if р_.get("token_name")),
             "с_минтом_и_слотом": sum(1 for р_ in ряды
                                       if р_["mint"] and р_["source_slot"]),
             "ряды": ряды}
    текст = json.dumps(свод, ensure_ascii=False, indent=1)
    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            ф.write(текст + "\n")
        print(json.dumps({к: свод[к] for к in
                           ("снято_utc", "с", "до", "полные_поля",
                            "строк_просмотрено", "сделок", "с_минтом_и_слотом",
                            "с_севшей_подписью", "с_именем_токена")}, ensure_ascii=False))
    else:
        print(текст)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
