#!/usr/bin/env python3
"""Аудит аккаунта Dune -- владелец, 2026-09-01: расхождение между внешней
правдой дашборда (2138.84 из 2500, зафиксировано 2026-09-01) и внутренним
леджером (1823.26) ~315 кредитов. Подозреваемый №1 (владелец) --
scheduled-обновления сохранённых запросов/материализаций Sprint 1/1.5/G1,
исполняющиеся вне видимости наших GH Actions прогонов.

ТОЛЬКО бесплатные операции: GET /query/{id} (метаданные, НЕ биллится --
см. DuneClient.get_query_definition) для каждого известного query_id, плюс
несколько defensively-пробуемых account/usage эндпоинтов. Ни одного
execute() здесь нет -- это должно быть безопасно гонять сколько угодно раз.

Точная форма полей schedule/trigger в Dune API v1 нам не задокументирована
(сетевой доступ к docs.dune.com заблокирован из песочницы), поэтому не
гадаем по конкретному имени поля -- сканируем ответ на любой ключ,
похожий на schedule/trigger/cron/refresh, и печатаем СЫРОЙ JSON для
человеческой проверки.

Использование: python analysis/audit_dune_account.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dune_client import DuneClient


def collect_known_query_ids(client: DuneClient) -> dict[str, int]:
    """client.query_id_map уже объединяет эфемерный кэш
    (analysis/output/cache/query_ids.json -- общий префикс 'dune-cache-'
    across ВСЕХ трёх workflow, см. .github/workflows/*.yml) и постоянный
    data/query_ids_recovered.json при инициализации DuneClient."""
    return dict(client.query_id_map)


def find_schedule_like_fields(meta, path: str = "", hits: dict | None = None) -> dict:
    if hits is None:
        hits = {}
    if isinstance(meta, dict):
        for k, v in meta.items():
            key_lower = str(k).lower()
            new_path = f"{path}.{k}" if path else str(k)
            if any(s in key_lower for s in ("schedule", "trigger", "cron", "refresh", "recur", "interval")):
                hits[new_path] = v
            find_schedule_like_fields(v, new_path, hits)
    elif isinstance(meta, list):
        for i, v in enumerate(meta[:3]):
            find_schedule_like_fields(v, f"{path}[{i}]", hits)
    return hits


def main() -> int:
    client = DuneClient()
    known = collect_known_query_ids(client)
    print(f"[audit] Известно {len(known)} query_id (эфемерный + постоянный кэш query_id_map).")
    if not known:
        print("[audit] ПУСТО -- эфемерный кэш (dune-cache-*) не восстановился или ещё не "
              "накопил записей в этом контейнере. Постоянный data/query_ids_recovered.json "
              "содержит только частично восстановленные id (см. analysis/recover_query_ids.py) "
              "-- полный аудит по всем запросам этим прогоном не гарантирован.")

    schedule_findings: dict[int, dict] = {}
    errors: dict[int, str] = {}
    for content_hash, qid in known.items():
        try:
            meta = client.get_query_definition(qid)
        except Exception as exc:
            errors[qid] = str(exc)[:200]
            continue
        hits = find_schedule_like_fields(meta)
        if hits:
            schedule_findings[qid] = {"name": meta.get("name"), "hits": hits}
        print(
            f"  query_id={qid:<10} name={str(meta.get('name', '?'))[:40]:<40} "
            f"is_private={meta.get('is_private', '?')} schedule_like_hits={bool(hits)}"
        )

    print(f"\n[audit] Ошибок при запросе метаданных: {len(errors)}.")
    for qid, err in list(errors.items())[:10]:
        print(f"  query_id={qid}: {err}")

    if schedule_findings:
        print(f"\n[audit] !!! НАЙДЕНЫ поля, похожие на schedule/trigger/cron, у {len(schedule_findings)} запросов:")
        print(json.dumps(schedule_findings, indent=2, ensure_ascii=False, default=str))
    else:
        print(
            "\n[audit] Полей, похожих на schedule/trigger/cron/refresh/interval, не найдено ни "
            "у одного известного query_id (по ключам ответа GET /query/{id}). ЭТО НЕ ДОКАЗЫВАЕТ "
            "отсутствие scheduled-обновлений -- Dune может хранить расписание отдельно от "
            "метаданных самого запроса, не видимо через этот эндпоинт (см. account/usage-пробы "
            "ниже и итоговую сводку)."
        )

    print("\n[audit] Пробую несколько правдоподобных account/usage эндпоинтов (жду часть 404 -- нормально):")
    endpoint_results = {}
    for path in ["/user", "/account", "/account/usage", "/billing", "/user/usage", "/team", "/subscription", "/user/credits"]:
        try:
            resp = client._get(path)
            print(f"  GET {path}: OK -- {json.dumps(resp, ensure_ascii=False, default=str)[:500]}")
            endpoint_results[path] = "ok"
        except Exception as exc:
            print(f"  GET {path}: {str(exc)[:150]}")
            endpoint_results[path] = "error"

    print("\n[audit] ИТОГ:")
    print(f"  - query_id проверено: {len(known)}, ошибок метаданных: {len(errors)}, "
          f"schedule-подобных находок: {len(schedule_findings)}")
    print(f"  - account/usage эндпоинтов доступно: {sum(1 for v in endpoint_results.values() if v == 'ok')}"
          f" из {len(endpoint_results)}")
    print(
        "  - ВЫВОД (честно, без натяжки): API v1 в том объёме, что нам доступен, "
        + ("похоже НЕ даёт прямого способа увидеть/отключить schedule программно -- "
           if sum(1 for v in endpoint_results.values() if v == 'ok') == 0 and not schedule_findings
           else "см. находки выше -- ")
        + "если реальный источник утечки в scheduled-обновлениях UI Dune, отключить их может "
          "только человек с доступом к dune.com/queries (Settings -> Trigger на каждом "
          "сохранённом запросе), это не API-операция в проверенной нами поверхности."
    )
    print("\n[audit] Готово. 0 кредитов потрачено (только метаданные/пробы).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
