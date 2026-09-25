#!/usr/bin/env python3
"""Таблица покупок полосы: слот, место, кто довёз, время от сигнала до посадки.

Владелец 25.09: "По всем покупкам полосы с 15:13Z: слот относительно источника,
место в блоке, кто довёз, время от сигнала до посадки. Таблицей."

ЧТО БЕРЁМ И ОТКУДА. Только журнал позиций исполнителя (positions.jsonl) с хоста,
на чтение. Ни одного числа не считаем "по настройкам": слот источника, наш слот,
место в блоке и подпись -- это то, что записал детектор, увидев нашу же
транзакцию в подписке.

ЧЕСТНО ПРО "ПОСАДКУ". Момент включения в блок мы наблюдаем как появление
транзакции в подписке на уровне processed -- это ближайшее, что у нас есть, и
именно так подписан столбец. Разница со временем сигнала считается от t_recv
сигнала источника на нашем узле (signal_recv_ts): это тот же ноль, от которого
считается пара с Bloom.

КТО ДОВЁЗ. В обычном пуле это тот, чей ответ пришёл первым (одна подпись на
всех). В варианте на nonce -- тот, ЧЕЙ ВАРИАНТ СЕЛ: варианты отличаются только
чаевыми, значит севшая подпись однозначно называет сервис. Если режим не пул --
столбец пуст, и это не пропуск данных, а отсутствие пула.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def позиции_из_журнала(путь: str) -> dict:
    """Свёрнутые позиции: строки журнала дописываются, последняя правит поля."""
    из_: dict = {}
    п = Path(путь)
    if not п.exists():
        return из_
    for строка in п.read_text(encoding="utf-8", errors="replace").split("\n"):
        строка = строка.strip()
        if not строка.startswith("{"):
            continue
        try:
            з = json.loads(строка)
        except ValueError:
            continue
        cid = з.get("client_order_id")
        if not cid:
            continue
        из_.setdefault(cid, {}).update({к: v for к, v in з.items() if v is not None})
    return из_


def строки_таблицы(позиции: dict, *, с_utc: str = "", метка: str = "own_send") -> list:
    ряд = []
    for cid, п in позиции.items():
        if п.get("lane") != метка:
            continue
        когда = str(п.get("ts_intent_utc") or "")
        if с_utc and когда and когда < с_utc:
            continue
        их = п.get("source_slot")
        наш = п.get("own_tx_seen_slot") or п.get("our_slot")
        отставание = (наш - их) if isinstance(их, int) and isinstance(наш, int) else None
        сигнал_ts = п.get("signal_recv_ts")
        видно_ts = п.get("own_tx_seen_ts")
        от_сигнала_мс = (round((float(видно_ts) - float(сигнал_ts)) * 1000.0, 1)
                          if сигнал_ts and видно_ts else None)
        довёз = п.get("lane_pool_winner")
        ряд.append({
            "cid": cid, "utc": когда, "group": п.get("lane_group") or "bloom_lane",
            "mint": (п.get("mint") or "")[:10],
            "size_sol": п.get("sol_in"),
            "source_slot": их, "our_slot": наш, "slots_behind": отставание,
            "block_index": п.get("block_index"), "block_total": п.get("block_total"),
            "winner": довёз, "mode": п.get("lane_pool_mode"),
            "senders": п.get("lane_pool_ok_senders") or п.get("lane_pool_senders"),
            "from_signal_ms": от_сигнала_мс,
            "send_to_seen_ms": п.get("lane_send_to_seen_ms"),
            "state": п.get("state"), "chain_ok": п.get("chain_ok"),
            "signature": (п.get("lane_signature") or "")[:16],
            "accepted_first": (п.get("lane_signature_accepted_first") or "")[:16],
            "tips_sol": п.get("lane_tips_total_sol"),
            "uncountable": п.get("result_uncountable"),
        })
    ряд.sort(key=lambda з: з["utc"])
    return ряд


def в_цели(з: dict, голова: int = 100) -> str:
    """Цель владельца 25.09: S+0 в любом месте ИЛИ голова S+1 (место <= 100)."""
    о = з.get("slots_behind")
    м = з.get("block_index")
    if о is None:
        return "—"
    if о == 0:
        return "ДА (S+0)"
    if о == 1:
        if not isinstance(м, int):
            return "не судим (места нет)"
        return f"ДА (S+1, место {м})" if м <= голова else f"нет (S+1, место {м})"
    return f"нет (S+{о})"


def таблица(ряд: list) -> str:
    ряды = ["| время UTC | группа | минт | размер SOL | слот источника | наш слот | "
             "S+N | место в блоке | кто довёз | режим | от сигнала до появления, мс | "
             "от отправки, мс | в цели | состояние | чаевые SOL |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]

    def ч(з):
        return "—" if з is None or з == "" else str(з)

    for з in ряд:
        место = (f"{з['block_index']}/{з['block_total']}"
                 if з.get("block_index") is not None else "—")
        ряды.append(
            f"| {ч(з['utc'])} | {ч(з['group'])} | {ч(з['mint'])} | {ч(з['size_sol'])} | "
            f"{ч(з['source_slot'])} | {ч(з['our_slot'])} | "
            f"{('S+' + str(з['slots_behind'])) if з['slots_behind'] is not None else '—'} | "
            f"{место} | {ч(з['winner'])} | {ч(з['mode'])} | {ч(з['from_signal_ms'])} | "
            f"{ч(з['send_to_seen_ms'])} | {в_цели(з)} | "
            f"{ч(з['state'])}{' (цепь ok)' if з.get('chain_ok') else ''} | "
            f"{ч(з['tips_sol'])} |")
    return "\n".join(ряды)


def main() -> int:
    import argparse

    р = argparse.ArgumentParser(description=__doc__)
    р.add_argument("--positions", default="/tmp/bloom_state/positions.jsonl")
    р.add_argument("--since", default="", help="только позиции с этого UTC, например 2026-09-25T15:13")
    р.add_argument("--out-md", default=None)
    р.add_argument("--out-json", default=None)
    р.add_argument("--raw-json", default=None,
                    help="полные записи позиций полосы окна (ключей в них нет)")
    а = р.parse_args()
    поз = позиции_из_журнала(а.positions)
    ряд = строки_таблицы(поз, с_utc=а.since)
    т = таблица(ряд)
    print(f"позиций в журнале: {len(поз)}, покупок полосы в окне: {len(ряд)}")
    print(т)
    if а.out_md:
        Path(а.out_md).write_text(
            f"# Покупки полосы с {а.since or 'начала журнала'}\n\n"
            f"Позиций в журнале: {len(поз)}; покупок полосы в окне: {len(ряд)}.\n\n"
            + т + "\n", encoding="utf-8")
        print(f"записано: {а.out_md}")
    if а.raw_json:
        # ПОЛНЫЕ ЗАПИСИ -- для разбора, когда таблицы мало: почему unsold, что
        # записал сторож, сколько купили. Ключей в позициях нет вовсе.
        свои = {к: v for к, v in поз.items()
                if v.get("lane") == "own_send"
                and (not а.since or str(v.get("ts_intent_utc") or "") >= а.since)}
        Path(а.raw_json).write_text(
            json.dumps(свои, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"записано: {а.raw_json} ({len(свои)} позиций)")
    if а.out_json:
        Path(а.out_json).write_text(
            json.dumps({"since": а.since, "rows": ряд}, ensure_ascii=False, indent=2)
            + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
