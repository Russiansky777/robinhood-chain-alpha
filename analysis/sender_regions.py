#!/usr/bin/env python3
"""III.14 -- РЕЕСТР РЕГИОНАЛЬНЫХ ТОЧЕК ВХОДА ОТПРАВИТЕЛЕЙ.

Списки имён НЕ пишутся по памяти: они вычитываются из сохранённой
документации в data/docs/senders/ (её качает прогон run_sender_pool_docs_nl.yml)
и из официального endpoints.json пакета nozomi. У каждого имени видно, из
какого файла оно взято, и незнакомое имя не выдумывается.

Схема и порт берутся из того же реестра отправителей (data/senders.json), где
адрес уже проверен работающей полосой: у 0slot и Helius Sender документация
прямо велит HTTP, у Jito и nozomi -- HTTPS.

Зачем это нужно. II.14 показал, что доля S+0 падает с удалением лидера слота от
нашего узла: EU 40.4 %, US-East 16.7 %, Asia 10.5 %, US-West 0 %. Отправлять
всегда в Амстердам -- значит терять S+0 на лидерах вне EU. Прежде чем что-то
менять, нужно знать ЦЕНУ: сколько стоит дотянуться с нашего хоста до точки
входа в чужом регионе. Этот реестр даёт список адресов для такого замера.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
ДОКИ = КОРЕНЬ / "data" / "docs" / "senders"
РЕЕСТР = КОРЕНЬ / "data" / "senders.json"

# Как искать имена точек входа у каждого отправителя и какой у них порт.
# Образец -- по домену самого отправителя, то есть ничего чужого не подхватит.
ПРАВИЛА = {
    "helius": {"образец": r"[a-z0-9-]+-sender\.helius-rpc\.com", "порт": 80,
               "схема": "http"},
    "jito": {"образец": r"[a-z0-9.-]*mainnet\.block-engine\.jito\.wtf",
             "порт": 443, "схема": "https"},
    "zeroslot": {"образец": r"[a-z0-9-]+\.0slot\.trade", "порт": 80,
                 "схема": "http"},
    "blockrazor": {"образец": r"[a-z0-9-]+\.solana\.blockrazor\.xyz",
                   "порт": 443, "схема": "http"},
    "nozomi": {"образец": r"[a-z0-9-]+\.nozomi\.temporal\.xyz", "порт": 443,
               "схема": "https"},
    "astralane": {"образец": r"[a-z0-9-]+\.gateway\.astralane\.io", "порт": 80,
                  "схема": "http"},
}

# Регион по имени точки. Только то, что читается из самого имени или названо в
# документации; незнакомое -- "не назван", а не догадка.
РЕГИОН_ПО_КЛЮЧУ = {
    "ams": "EU", "amsterdam": "EU", "fra": "EU", "frankfurt": "EU",
    "de": "EU", "lon": "EU", "london": "EU", "dublin": "EU", "fr": "EU",
    "ewr": "US-East", "newark": "US-East", "ny": "US-East",
    "newyork": "US-East", "ash": "US-East", "ashburn": "US-East",
    "pit": "US-East", "pittsburgh": "US-East", "toronto": "US-East",
    "slc": "US-West", "lax": "US-West", "la": "US-West",
    "losangeles": "US-West", "los-angeles": "US-West",
    "sg": "Asia", "sgp": "Asia", "singapore": "Asia", "tyo": "Asia",
    "tokyo": "Asia", "jp": "Asia",
    "lim": "прочее (Лима)",
}


def регион_имени(имя: str) -> str:
    """Регион по первой части имени. Цифра в конце -- номер точки, не регион:
    ams1 и ams -- один и тот же Амстердам, и считать их разными значило бы
    потерять половину списка nozomi."""
    первая = имя.split(".")[0]
    варианты = [первая, первая.split("-")[0],
                re.sub(r"\d+$", "", первая),
                re.sub(r"\d+$", "", первая.split("-")[0])]
    for кусок in варианты:
        if кусок in РЕГИОН_ПО_КЛЮЧУ:
            return РЕГИОН_ПО_КЛЮЧУ[кусок]
    return "не назван"


def точки() -> dict:
    """{отправитель: [ {host, port, схема, регион, откуда} ]} из документации."""
    из_: dict = {}
    файлы = sorted(ДОКИ.glob("*")) if ДОКИ.exists() else []
    for отправитель, правило in ПРАВИЛА.items():
        найдено: dict = {}
        for ф in файлы:
            if not ф.is_file():
                continue
            try:
                текст = ф.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for имя in re.findall(правило["образец"], текст):
                найдено.setdefault(имя, str(ф.relative_to(КОРЕНЬ)))
        # Адрес, которым полоса ходит СЕЙЧАС, тоже в списке: он проверен боем.
        if РЕЕСТР.exists():
            реестр = (json.loads(РЕЕСТР.read_text(encoding="utf-8"))
                      .get("senders") or {})
            url = (реестр.get(отправитель) or {}).get("url") or ""
            имя_url = re.sub(r"^[a-z]+://", "", url).split("/")[0].split(":")[0]
            if имя_url and re.fullmatch(правило["образец"], имя_url):
                найдено.setdefault(имя_url, "data/senders.json (рабочий адрес)")
        из_[отправитель] = [
            {"host": имя, "port": правило["порт"], "схема": правило["схема"],
             "регион": регион_имени(имя), "откуда": откуда}
            for имя, откуда in sorted(найдено.items())]
    return из_


def main() -> int:
    т = точки()
    всего = sum(len(v) for v in т.values())
    print(f"отправителей: {len(т)}; точек входа из документации: {всего}")
    for отправитель, список in т.items():
        по_регионам: dict = {}
        for з in список:
            по_регионам.setdefault(з["регион"], []).append(з["host"])
        print(f"\n{отправитель}: {len(список)} точек")
        for регион, имена in sorted(по_регионам.items()):
            print(f"  {регион}: {', '.join(имена)}")
    Path(КОРЕНЬ / "data" / "sender_regions.json").write_text(
        json.dumps({"точки": т, "всего": всего}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
