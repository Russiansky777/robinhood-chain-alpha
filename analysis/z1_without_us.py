#!/usr/bin/env python3
"""З1 без нас: кто стоял впереди и чем брал -- скоростью или платой.

ЗАЧЕМ. Владелец 25.09: "пересчитать З1 (кто стоял перед нами, доли
«скоростью/платой»), исключив все наши кошельки (задачи DBot, 4s87…, 21Dq…).
Одной строкой: держится ли вывод «ПУТЬ»".

Причина спросить -- живая: в сохранённом кэше З1 среди покупателей в блоке
источника сидит наш же бот (DBot покупает по сигналу), и в списке "снайперов"
25.09 пять из десяти верхних оказались кошельками наших задач. Если тем же
образом наши строки попали в разбор "кто впереди нас", то вывод считался по нам
же.

ЧТО СЧИТАЕМ. Берём сохранённые 42 транзакции З1
(data/solana_fast_buyers_priority2_result.json, локальный пересчёт без новых
вызовов RPC) и строки нашей покупки Bloom из того же файла. Для каждой сделки
берём НАШУ плату (приоритет плюс чаевые) и сравниваем с платой тех, кто стоял
ВПЕРЕДИ нас:
  * заплатил БОЛЬШЕ нас -- взял платой;
  * заплатил столько же или меньше -- взял путём (скоростью): при меньшей плате
    он оказался впереди не деньгами.
Плата, которой в дампе нет (priority_fee_sol=None), в доли НЕ идёт: это
"неизвестно", а не ноль.

ЧЕГО НЕ ДЕЛАЕМ. Не ходим в сеть и не достраиваем недостающее оценкой.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ФАЙЛ = "data/solana_fast_buyers_priority2_result.json"
СНИМОК_ЗАДАЧ = "data/final/20260923T145755Z/konfig.json"
НАШИ_ПРЯМО = ["4s87RRC2V2XAJD6R8U2dP8kQH99Z2wA6fg88ZVfV4j4N",
               "21DqHDDPEfMhK1dHRkV9E8v8KTTSKQGApJAr1irC9j7w"]


def _путь(имя: str) -> Path:
    п = Path(имя)
    return п if п.exists() else Path("..") / имя


def наши_кошельки() -> dict:
    """{адрес: чей} -- кошельки задач DBot плюс названные владельцем прямо."""
    из_ = {а: "кошелёк исполнителя/полосы" for а in НАШИ_ПРЯМО}
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import bloom_detector as D  # noqa: PLC0415

    д = json.loads(_путь(СНИМОК_ЗАДАЧ).read_text(encoding="utf-8"))
    for з in (D.тело_ответа(д).get("res") or []):
        if isinstance(з, dict) and з.get("walletAddress"):
            из_[з["walletAddress"]] = f"кошелёк задачи {з.get('name')}"
    return из_


# НАША ПЛАТА ЗА МЕСТО -- ПО ЦЕПИ, а не по настройкам. Замер агента A2 25.09 на
# наших живых покупках: сверх заявки у нас уходит 0.005594 SOL на покупке
# (приоритет 0.005105 плюс базовый тариф и прочее). Полоса платит меньше:
# приоритет 0.001 плюс чаевые Sender 0.001 = 0.002 SOL. Считаем по обоим.
НАША_ПЛАТА_BLOOM_SOL = 0.005594
НАША_ПЛАТА_ПОЛОСЫ_SOL = 0.002


def пересчитать(*, файл: str = ФАЙЛ,
                 наша_плата_sol: float = НАША_ПЛАТА_BLOOM_SOL,
                 позиции_впереди: tuple = (1, 2, 3)) -> dict:
    """Кто стоял впереди и чем брал -- при НАШЕЙ плате из замера по цепи.

    ВАЖНОЕ ОГРАНИЧЕНИЕ, найденное при пересчёте: наших кошельков в этом наборе
    НЕТ ВОВСЕ. Ни кошелёк исполнителя (4s87...), ни кошелёк полосы (21Dq...), ни
    кошельки задач DBot в 42 строках не встречаются, а строки с пометкой
    is_bloom принадлежат ЧЕТЫРЁМ ЧУЖИМ подписантам -- это другие пользователи
    Bloom, опознанные по его счёту сборов, а не мы. Значит исключать из набора
    нечего, и прежний вывод считался не по нам. Проверка исключения всё равно
    делается и её итог виден числом: так это перестаёт быть утверждением на
    слово.
    """
    д = json.loads(_путь(файл).read_text(encoding="utf-8"))
    наши = наши_кошельки()
    все = д.get("all_rows") or []
    итог = {"file": файл, "n_rows": len(все),
             "our_pay_sol": наша_плата_sol,
             "our_pay_source": ("замер по цепи на наших покупках (агент A2, "
                                 "25.09): 0.005594 SOL сверх заявки"),
             "excluded_our_rows": [], "bloom_marked_rows": [],
             "ahead": []}
    for с in все:
        подпись = с.get("signer") or ""
        if подпись in наши:
            итог["excluded_our_rows"].append(
                {"signer": подпись, "trade": с.get("trade_label"),
                  "whose": наши[подпись]})
        if с.get("is_bloom"):
            итог["bloom_marked_rows"].append(
                {"signer": подпись, "trade": с.get("trade_label"),
                  "position": с.get("position_from_source"),
                  "note": "пользователь Bloom, опознан по счёту сборов -- НЕ мы"})
    итог["our_rows_found"] = len(итог["excluded_our_rows"])
    скоростью = платой = неизвестно = 0
    by_pos: dict = {}
    for с in все:
        подпись = с.get("signer") or ""
        поз = с.get("position_from_source")
        if подпись in наши or not isinstance(поз, int):
            continue
        if поз not in позиции_впереди:
            continue
        их = с.get("total_priority_plus_tips_sol")
        строка = {"trade": с.get("trade_label"), "signer": подпись,
                   "position": поз, "their_pay_sol": их}
        if их is None:
            неизвестно += 1
            строка["verdict"] = "плата неизвестна"
        elif float(их) > float(наша_плата_sol):
            платой += 1
            строка["verdict"] = "платил больше нас"
        else:
            скоростью += 1
            строка["verdict"] = "платил не больше нас -- взял путём"
        итог["ahead"].append(строка)
        by_pos.setdefault(поз, []).append(их)
    известных = скоростью + платой
    медианы = {}
    for поз, ряд in sorted(by_pos.items()):
        числа = sorted(з for з in ряд if з is not None)
        медианы[поз] = {
            "n": len(ряд),
            "n_known": len(числа),
            "median_pay_sol": (числа[len(числа) // 2] if числа else None),
        }
    итог.update(
        positions_counted=list(позиции_впереди),
        n_ahead=len(итог["ahead"]), n_by_speed=скоростью, n_by_pay=платой,
        n_unknown=неизвестно,
        share_by_speed=(round(скоростью / известных, 4) if известных else None),
        share_by_pay=(round(платой / известных, 4) if известных else None),
        medians_by_position=медианы,
        verdict=(("ПУТЬ держится: из тех, кто стоял впереди, "
                   f"{скоростью} из {известных} платили НЕ больше нас")
                  if известных and скоростью >= платой else
                  (("ПУТЬ не держится: из тех, кто стоял впереди, "
                     f"{платой} из {известных} платили больше нас")
                    if известных else "судить нечем: плата неизвестна у всех")))
    return итог


def poz_none(поз) -> bool:
    return поз is None or not isinstance(поз, int)


def main() -> int:
    итог = пересчитать()
    печать = {к: v for к, v in итог.items()
              if к not in ("ahead", "bloom_marked_rows")}
    # И тот же счёт при плате ПОЛОСЫ: она платит меньше Bloom, и вывод для неё
    # может быть другим -- это надо видеть, а не выводить из общего.
    полоса = пересчитать(наша_плата_sol=НАША_ПЛАТА_ПОЛОСЫ_SOL)
    печать["lane_pay_sol"] = НАША_ПЛАТА_ПОЛОСЫ_SOL
    печать["lane_verdict"] = полоса["verdict"]
    печать["lane_share_by_speed"] = полоса["share_by_speed"]
    print(json.dumps(печать, ensure_ascii=False, indent=2))
    Path("data/z1_without_us.json").write_text(
        json.dumps(итог, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("записано: data/z1_without_us.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
