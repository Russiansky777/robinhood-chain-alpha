#!/usr/bin/env python3
"""Что признак жизни службы говорит о файле групп и о подписке.

ЗАЧЕМ. После замены файла групп надо увидеть ЖИВОЙ хэш -- тот, который держит
служба, а не тот, который мы записали. Совпал -- значит перечитывание по mtime
работает и перезапуск не нужен (п.1а финального плана). Здесь же видно, сколько
адресов подписано и не рвётся ли подписка: 585 адресов -- это новый предел,
которого раньше не было.

Только чтение одного файла состояния. Ни сети, ни записи.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--status", required=True, help="путь к detector_status.json")
    а = р.parse_args()
    п = Path(а.status)
    if not п.exists():
        print(json.dumps({"ok": False, "почему": f"нет файла {п}"},
                          ensure_ascii=False))
        return 1
    try:
        d = json.loads(п.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "почему": f"{type(exc).__name__}"},
                          ensure_ascii=False))
        return 1
    г = d.get("source_groups") or {}
    из_ = {"ok": True, "метка": d.get("updated_utc"),
            "живёт_с": d.get("alive_since_utc"),
            "hash": г.get("hash"), "mtime_utc": г.get("mtime_utc"),
            "тревога": г.get("alarm"), "why_not": г.get("why_not"),
            "в_файле": г.get("in_file"), "подписано": г.get("subscribed"),
            "источников": d.get("sources"),
            "сигналов": d.get("signals_seen"), "к_покупке": d.get("to_buy"),
            "обрывов_подписки": d.get("subscribe_drops"),
            "способ_подписки": d.get("subscribe_method"),
            "кредитов_за_сеанс": d.get("session_credits"),
            # ПОЧЕМУ НЕ ПОКУПАЕМ -- по кодам решений: без этого "сигналов 261,
            # к покупке 0" не отличить от "детектор не решает вовсе".
            "по_кодам": d.get("by_code"),
            "площадка_не_звана_по_группе": d.get("bloom_skipped_by_group"),
            "полоса": {к: (d.get("own_send") or {}).get(к)
                        for к in ("enabled", "live", "sent", "by_stage", "wallet",
                                   "balance_sol", "limits", "last",
                                   "balance_day_threshold", "lane_daily_stop")},
            # РУБИЛЬНИК И БАЛАНС ИСПОЛНИТЕЛЯ -- их спрашивает получасовая
            # проверка владельца ("стоп стоит/снят, балансы X / Y, позиций N").
            # Без них строку приходилось отдавать с прочерками, хотя служба эти
            # числа пишет.
            "рубильник": d.get("kill_active"),
            "рубильник_почему": d.get("kill_note"),
            "баланс_исполнителя_sol": d.get("balance_sol"),
            "рубильник_площадки": d.get("kill_bloom_active"),
            "кэш_ног": d.get("leg_cache"),
            "двухшаговый": d.get("two_step"),
            "площадка_торгует": bool((d.get("own_send") or {}).get("bloom_trades")),
            "телеграм": {к: (d.get("telegram") or {}).get(к)
                          for к in ("enabled", "format2", "format3", "sent",
                                     "failed", "last_error")}}
    print(json.dumps(из_, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
