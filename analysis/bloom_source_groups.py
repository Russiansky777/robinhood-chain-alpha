#!/usr/bin/env python3
"""Группы источников: кто кому источник и на какие деньги.

Владелец 25.09 (сводный промпт 15:45 Madrid) развёл источники на три группы, и
разница между ними -- ДЕНЬГИ, а не пометка в отчёте:

  bloom_lane  -- BATCH-3/5 плюс лидер. Bloom торгует 0.2 SOL, полоса 0.05 SOL.
                 Список приходит ИЗ DBot живьём: владелец правит задачи там, и
                 второй список в репозитории разошёлся бы с боем молча.
  lane_only   -- 38 активных кошельков реестра (пункт 1.1). Полоса 0.05 SOL,
                 Bloom по ним НЕ торгует вовсе. В деньги входят с меткой группы.
  speed_only  -- только скорость (пункт 1.2). Полоса 0.01 SOL через пул БЕЗ
                 веера, Bloom не торгует, в деньги не входит. Свой суточный
                 потолок 0.2 SOL (покупки плюс чаевые) и свой стоп -0.15 SOL.

ПОЧЕМУ ОТДЕЛЬНЫЙ МОДУЛЬ. Группу спрашивают двое: детектор (звать ли Bloom) и
полоса (какой размер, нужен ли веер, какой потолок). Если каждый будет читать
файл по-своему, они когда-нибудь разойдутся -- и разойдутся на деньгах.

ЧЕГО ЗДЕСЬ НЕТ. Ни одного адреса в коде: всё из data/sources_2026-09-25.json,
который собирается из слов владельца и сохранённых данных
(analysis/sources_groups_build.py). Нет файла -- нет групп: все источники
считаются bloom_lane, то есть ведут себя как до 25.09.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ФАЙЛ_ПО_УМОЛЧАНИЮ = "data/sources_2026-09-25.json"


def файл() -> str:
    """Путь к файлу групп -- читается ПРИ ВЫЗОВЕ, а не при импорте.

    Прочитанный при импорте, он бы застыл: смена BLOOM_SOURCE_GROUPS после
    старта процесса (в том числе в самопроверке) молча не действовала бы, и
    проверка проверяла бы не то, что думает.
    """
    return os.environ.get("BLOOM_SOURCE_GROUPS") or ФАЙЛ_ПО_УМОЛЧАНИЮ
ГРУППА_ПО_УМОЛЧАНИЮ = "bloom_lane"
# Политика группы, которой нет в файле: так ведут себя BATCH-3/5 и лидер.
ПОЛИТИКА_ПО_УМОЛЧАНИЮ = {"lane_sol": None, "bloom_trades": True, "fanout": True,
                          # Размер покупки BLOOM по этой группе. None значит
                          # общий размер из окружения (BLOOM_BUY_SOL, у владельца
                          # 0.2 на BATCH-3/5). Решение владельца 25.09 вечером:
                          # по 38 кошелькам lane_only Bloom берёт 0.05.
                          "bloom_sol": None,
                          "day_cap_sol": None, "stop_loss_sol": None,
                          # Порог входа источника: None значит общий порог
                          # детектора (BLOOM_MIN_TARGET_SOL, сейчас 2 SOL --
                          # столько же стоит в задачах DBot у владельца).
                          "min_target_sol": None}
_КЭШ: dict | None = None


def _путь(путь: str | None = None) -> Path:
    п = Path(путь or файл())
    if п.exists():
        return п
    рядом = Path(__file__).resolve().parent.parent / (путь or файл())
    return рядом if рядом.exists() else п


def загрузить(путь: str | None = None, *, заново: bool = False) -> dict:
    """{"по_адресу": {адрес: группа}, "политики": {...}, "why_not": ...}."""
    global _КЭШ  # noqa: PLW0603
    if _КЭШ is not None and not заново and путь is None:
        return _КЭШ
    из_ = {"по_адресу": {}, "политики": {}, "why_not": None, "file": None}
    п = _путь(путь)
    if not п.exists():
        из_["why_not"] = f"файла групп {п} нет -- все источники как bloom_lane"
        if путь is None:
            _КЭШ = из_
        return из_
    try:
        д = json.loads(п.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"файл групп не прочитан ({type(exc).__name__})"
        if путь is None:
            _КЭШ = из_
        return из_
    из_["file"] = str(п)
    for имя, г in (д.get("groups") or {}).items():
        г = г or {}
        из_["политики"][имя] = {
            "lane_sol": г.get("lane_sol"),
            "bloom_trades": bool(г.get("bloom_trades")),
            "bloom_sol": г.get("bloom_sol"),
            "fanout": bool(г.get("fanout")),
            "day_cap_sol": г.get("day_cap_sol"),
            "stop_loss_sol": г.get("stop_loss_sol"),
            "min_target_sol": г.get("min_target_sol"),
        }
        # Адреса лежат двумя видами: словарь {адрес: партия} и списки
        # объектов с полем address. Оба читаем, третий вид не изобретаем.
        адреса = г.get("addresses")
        if isinstance(адреса, dict):
            for а in адреса:
                из_["по_адресу"][а] = имя
        elif isinstance(адреса, list):
            for з in адреса:
                а = з.get("address") if isinstance(з, dict) else з
                if а:
                    из_["по_адресу"][а] = имя
        for поле in ("by_signal", "snipers"):
            for з in г.get(поле) or []:
                а = з.get("address") if isinstance(з, dict) else з
                if а:
                    из_["по_адресу"][а] = имя
    if путь is None:
        _КЭШ = из_
    return из_


def группа(адрес: str | None, путь: str | None = None) -> str:
    """Группа адреса. Незнакомый адрес -- bloom_lane: он пришёл из DBot."""
    if not адрес:
        return ГРУППА_ПО_УМОЛЧАНИЮ
    return (загрузить(путь)["по_адресу"].get(адрес) or ГРУППА_ПО_УМОЛЧАНИЮ)


def политика(имя_группы: str | None, путь: str | None = None) -> dict:
    """Что группе можно. Неизвестной группе -- политика bloom_lane."""
    п = загрузить(путь)["политики"].get(имя_группы or "")
    return dict(п) if п else dict(ПОЛИТИКА_ПО_УМОЛЧАНИЮ)


def адреса_всех_групп(путь: str | None = None) -> dict:
    """{адрес: группа} -- то, на что детектор обязан подписаться сверх DBot."""
    return dict(загрузить(путь)["по_адресу"])


def свод(путь: str | None = None) -> dict:
    """Сколько адресов в каждой группе -- для признака жизни и доклада."""
    д = загрузить(путь)
    счёт: dict = {}
    for _, г in д["по_адресу"].items():
        счёт[г] = счёт.get(г, 0) + 1
    return {"file": д.get("file"), "why_not": д.get("why_not"),
            "by_group": счёт, "policies": д["политики"]}


def self_test() -> int:
    проверки = []

    def chk(имя, ок, факт=""):
        проверки.append((имя, bool(ок), факт))

    д = загрузить(заново=True)
    chk("файл групп прочитан", д["why_not"] is None, д["why_not"])
    с = свод()
    chk("в полосной группе ровно 38 адресов (слово владельца)",
        с["by_group"].get("lane_only") == 38, с["by_group"])
    chk("в группе скорости адреса есть и их не больше 30",
        3 <= с["by_group"].get("speed_only", 0) <= 30, с["by_group"])
    # РАЗМЕРЫ -- ЭТО ДЕНЬГИ: 0.05 полосе, 0.01 скорости.
    chk("размер полосной группы 0.05 SOL",
        политика("lane_only")["lane_sol"] == 0.05, политика("lane_only"))
    chk("размер группы скорости 0.01 SOL",
        политика("speed_only")["lane_sol"] == 0.01, политика("speed_only"))
    # РЕШЕНИЕ ВЛАДЕЛЬЦА 25.09 (вечер): по lane_only Bloom ТОРГУЕТ и берёт
    # 0.05 SOL -- ради пар "Bloom против полосы" на одних сигналах. По группе
    # скорости Bloom не торгует по-прежнему.
    chk("по lane_only Bloom торгует и размер 0.05",
        политика("lane_only")["bloom_trades"] is True
        and политика("lane_only")["bloom_sol"] == 0.05, политика("lane_only"))
    # РЕШЕНИЕ ВЛАДЕЛЬЦА 25.09 (ночь): по группе скорости Bloom тоже торгует и
    # берёт 0.01 -- ради пары на КАЖДОЙ покупке скорости. Порог входа источника
    # у группы свой, 0.5 SOL, и он общий для Bloom и полосы.
    chk("по группе скорости Bloom торгует и размер 0.01",
        политика("speed_only")["bloom_trades"] is True
        and политика("speed_only")["bloom_sol"] == 0.01,
        политика("speed_only"))
    chk("и порог входа у группы скорости 0.5 SOL -- один на Bloom и полосу",
        политика("speed_only")["min_target_sol"] == 0.5,
        политика("speed_only"))
    chk("у bloom_lane размер Bloom из окружения, а не из файла",
        политика("bloom_lane").get("bloom_sol") is None, политика("bloom_lane"))
    chk("веер идёт по полосной группе и НЕ идёт по скорости",
        политика("lane_only")["fanout"] is True
        and политика("speed_only")["fanout"] is False, "")
    # Потолок и стоп группы скорости -- решение владельца 25.09 (вечер):
    # 1 SOL в сутки (покупки плюс чаевые) и стоп -0.5 SOL. Было 0.2 и -0.15.
    chk("у скорости свой потолок 1 SOL и свой стоп -0.5",
        политика("speed_only")["day_cap_sol"] == 1.0
        and политика("speed_only")["stop_loss_sol"] == 0.5,
        политика("speed_only"))
    chk("в группе скорости ровно 30 адресов (слово владельца)",
        len([а for а, г in адреса_всех_групп().items()
             if г == "speed_only"]) == 30,
        len([а for а, г in адреса_всех_групп().items() if г == "speed_only"]))
    chk("незнакомый адрес -- bloom_lane, как было до 25.09",
        группа("НеизвестныйАдресКоторогоНетВФайле") == "bloom_lane", "")
    chk("и Bloom по нему торгует",
        политика(группа("НеизвестныйАдрес"))["bloom_trades"] is True, "")
    # НАЛОГОВЫЕ МАРШРУТЫ владелец запретил добавлять -- проверяем, что их нет.
    адреса = адреса_всех_групп()
    chk("налоговых маршрутов в группах нет",
        not [а for а in адреса
             if а.startswith(("Et9jbvxv", "BMgsHTvc", "DmopudSG"))],
        [а for а in адреса if а.startswith(("Et9jbvxv", "BMgsHTvc", "DmopudSG"))])
    # Один адрес -- одна группа: иначе размер сделки зависел бы от порядка
    # чтения файла.
    из_файла = json.loads(_путь().read_text(encoding="utf-8"))
    все = []
    for имя, г in (из_файла.get("groups") or {}).items():
        а_ = г.get("addresses")
        если_словарь = list(а_) if isinstance(а_, dict) else []
        все += [(а, имя) for а in если_словарь]
        for поле in ("by_signal", "snipers"):
            все += [(з["address"], имя) for з in (г.get(поле) or [])]
    имена = [а for а, _ in все]
    chk("ни один адрес не попал в две группы",
        len(имена) == len(set(имена)),
        [а for а in set(имена) if имена.count(а) > 1])
    chk("наших кошельков в источниках нет",
        not ({"4s87RRC2V2XAJD6R8U2dP8kQH99Z2wA6fg88ZVfV4j4N",
              "21DqHDDPEfMhK1dHRkV9E8v8KTTSKQGApJAr1irC9j7w"} & set(имена)), "")

    плохо = [(и, ф) for и, ок, ф in проверки if not ок]
    for имя, ок, факт in проверки:
        print(f"  [{'ok  ' if ок else 'СБОЙ'}] {имя}"
              + ("" if ок else f" -- факт: {факт}"))
    print(f"самопроверка групп источников: {len(проверки) - len(плохо)}/"
          f"{len(проверки)} пройдено")
    return 1 if плохо else 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    print(json.dumps(свод(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
