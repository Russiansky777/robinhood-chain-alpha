#!/usr/bin/env python3
"""Подбивка, контроль цены: выборка из сохранённого результата, без хоста.

ЗАЧЕМ. Передача (docs/podbivka_handoff.md) требует: чтобы получить ровно те же
258/80, брать выборку из data/podbivka/kontrol_ceny.json -- там сохранены все
ряды и отказы с подписями. Журнал на хосте растёт, и свежая выгрузка дала бы
другой набор; здесь -- тот же самый.

ЧЕГО В СОХРАНЁННОМ НЕТ. Поля source (адрес источника) в рядах нет. Симулятор
использует его в одном месте -- c2_common.identify_pool, как исключение «это
не хранилище, это сам трейдер»; подписанты транзакции исключаются там же
отдельно. Источник подписывает свою покупку, поэтому пустой source даёт то же
исключение. Если это где-то не так -- ряд разойдётся с сохранённым, и это
видно в сравнении по подписи (podbivka_kontrol_sravnenie.py).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--iz", default="data/podbivka/kontrol_ceny.json")
    р.add_argument("--out", default="/tmp/nashi_sdelki_258.json")
    а = р.parse_args()
    д = json.loads(Path(а.iz).read_text(encoding="utf-8"))
    сделки = []
    for r in list(д.get("ряды") or []) + list(д.get("отказы") or []):
        сделки.append({
            "client_order_id": r.get("cid"),
            "mint": r.get("mint"),
            "lane": bool(r.get("lane")),
            "sol_in": r.get("sol_in"),
            "ts_intent_utc": r.get("ts"),
            "source_sig": r.get("source_sig"),
            "signature": r.get("our_sig"),
            "source": "",
        })
    сделки.sort(key=lambda с: (с.get("ts_intent_utc") or "", с.get("client_order_id") or ""))
    Path(а.out).write_text(json.dumps({"сделки": сделки}, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"сделок": len(сделки),
                      "с_подписью_источника": sum(1 for с in сделки if с["source_sig"]),
                      "с_нашей_подписью": sum(1 for с in сделки if с["signature"])},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
