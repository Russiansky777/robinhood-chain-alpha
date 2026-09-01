#!/usr/bin/env python3
"""Sprint G1 -- владелец, 2026-09-01: судьба фабрики после последнего
известного TokenLaunched (2026-08-12 19:42:33). Бесплатная проверка
докс/репозитория pons.family (github.com/ponsdotdev/ponsfamily) уже
сделана и migration/changelog не нашла (см. коммит и данное сообщение
владельцу) -- этот скрипт делает узкий ончейн-скан: тот же topic0, БЕЗ
фильтра по адресу фабрики, 3 дня после стопа, с жёстким LIMIT (второй
рубеж защиты вместе с обязывающим гейтом чтения в dune_client.py).

Использование: python analysis/g1_factory_fate.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("CREDIT_GUARD_NAMESPACE", "sprintG1")
sys.path.insert(0, str(Path(__file__).parent))

from config import CONFIG
from dune_client import DuneClient
from run_pipeline import read_sql

FACTORY_V1 = "0xa5aab3f0c6eeadf30ef1d3eb997108e976351feb"
FACTORY_V2 = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"


def main() -> int:
    client = DuneClient()
    sql = read_sql("g1/g1_factory_fate_scan")
    print("\n===== g1_factory_fate_scan (оценка 10.0) =====")
    qid = client.create_query("g1_factory_fate_scan", sql)
    df = client.run_sql_cached(
        "g1_factory_fate_scan", sql, query_id=qid, estimated_credits=10.0,
        expected_max_rows=50, expected_columns=4,
    )
    design_path = Path(CONFIG.g1_design_doc)
    text = design_path.read_text()
    marker = "## Судьба фабрики после 12.08.2026 (владелец, 2026-09-01)"

    if df is None or len(df) == 0:
        print(
            "\n[g1_factory_fate] Ноль строк -- topic0 TokenLaunched НЕ встречается НИГДЕ на "
            "чейне (любой адрес) за 3 дня после стопа. Сигнатура события не мигрировала на "
            "другой адрес -- согласуется с гипотезой конкурентного вытеснения (Pools Trade, "
            "запуск 2026-08-05, см. веб-источники), а не с дырой в выборке."
        )
        note = f"""

{marker}

**Бесплатная проверка (докс/репо pons.family):** github.com/ponsdotdev/ponsfamily
README/история коммитов не содержит упоминаний миграции, нового
поколения фабрики (V3+), депрекейшна или паузы. Таблица "Deployed
factories" ограничена V1/V2 (те же адреса, что используются в этом
Sprint).

**Ончейн-скан (та же сигнатура TokenLaunched, БЕЗ фильтра по адресу
фабрики, 2026-08-12 19:42:33 -> +3 дня):** 0 строк. Сигнатура события
не встречается НИГДЕ на чейне после стопа -- не миграция на другой
адрес, событие просто перестало происходить.

**Рабочая гипотеза (внешние источники, не ончейн-факт):** веб-поиск
указывает на запуск конкурирующего лаунчпада "Pools Trade" (официальный
лаунчпад Uniswap Labs) 2026-08-05 -- по времени близко к остановке
pons.family (2026-08-12). Согласуется с версией "продукт вытеснен
конкурентом", а не "ретро-выборка неполна". Эта конкретная деталь из
веб-поиска НЕ верифицирована ончейн (вне охвата данного Sprint) --
приводится как контекст, не как факт для вердикта.

**Вывод для дизайна:** ретро-выборка (01.07-29.08) полна относительно
подтверждённого адреса/события -- нет пропущенной миграции. Отсутствие
градуаций после 12.08 -- реальный факт, а не артефакт данных. Это
меняет вес всей линии graduation-momentum независимо от вердикта
ретро-теста: если запуски не возобновились, живая стратегия нацелена в
мёртвые контракты (см. доклад владельцу)."""
        if marker not in text:
            design_path.write_text(text + note)
        else:
            print(f"[g1_factory_fate] {design_path} уже содержит секцию -- не дублирую.")
        return 0

    print(df.to_string())
    known = {FACTORY_V1, FACTORY_V2}
    unknown_rows = df[~df["contract_address"].str.lower().isin(known)]
    if len(unknown_rows) > 0:
        print(
            f"\n[g1_factory_fate] НАЙДЕНО {len(unknown_rows)} других адресов, эмитирующих тот "
            "же topic0 после стопа -- кандидаты на новую фабрику/миграцию. Требует разбора "
            "вручную перед продолжением."
        )
    else:
        print("\n[g1_factory_fate] Все найденные строки -- от уже известных V1/V2 (частичное "
              "возобновление?). Разобрать вручную.")
    note = f"""

{marker}

**Бесплатная проверка (докс/репо):** github.com/ponsdotdev/ponsfamily
не содержит упоминаний миграции/нового поколения фабрики.

**Ончейн-скан (та же сигнатура, БЕЗ фильтра по адресу, +3 дня после
стопа):**
{df.to_string(index=False)}

Разбор вручную требуется -- см. RESULTS.md за интерпретацией."""
    if marker not in text:
        design_path.write_text(text + note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
