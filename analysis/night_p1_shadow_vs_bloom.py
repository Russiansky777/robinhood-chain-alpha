#!/usr/bin/env python3
"""Ночная задача P1 владельца: тень против Bloom по истории. ТОЛЬКО ЧТЕНИЕ.

Вопрос владельца: по сделке PICKAXE (подпись нашей покупки Bloom
2Vs5qv9V2HVhbNzd5sMzgfqLFZ37SxbbMDUBjm6a6PaFkX6N6m8BGgzEePAp4uvdiwQvV8E2UaCorAktn2Lo5fCS)
и по ВСЕМ покупкам Bloom с 24.09 -- собрала ли тень эту же сделку сама
(c2_shadow_build.shadow_build, запускается детектором ПАРАЛЛЕЛЬНО Bloom на
каждый сигнал buy), каким маршрутом, какой расчётный выход и min-out,
прошла бы симуляция, и если тень не собрала -- точная причина.

Модуль НЕ ходит в сеть и НЕ строит ничего сам: он читает уже написанные
журналы исполнителя -- decisions.jsonl (строки stage=shadow, которые пишет
Детектор.запустить_тень/_тень_внутри в bloom_detector.py) и positions.jsonl
(что реально купил Bloom, формат и функция чтения -- bloom_exec_state.py).
Свой собственный, а не общий ридер: ExecState создаёт каталоги при
инициализации, а этому скрипту даже для чтения ничего создавать на диске
службы нельзя -- он только читает.

Честность важнее полноты. Если поля нет -- в таблице стоит None и
почему (why_not), а не ноль и не догадка:
  * позиции нет / decisions.jsonl нет -- сказано прямо, а не "сделок нет";
  * тень не найдена по подписи источника -- "тени нет в журнале", а не
    "тень провалилась";
  * тень провалилась (ok=false/supported=false) -- её собственный why_not
    дословно (например "тип пула не покрыт сборщиком: ..." или "котировка
    не SOL: шаблона SOL -> Q в кэше нет" -- ровно то, что пишет
    c2_shadow_build.shadow_build);
  * у тени нет expected_out (DAMM2/DLMM/CLMM -- сосредоточенная
    ликвидность, min_out_from_reserves отвечает ok=false) -- сравнение по
    % не считается вовсе, а не считается от несуществующего числа;
  * у покупки Bloom нет bought_raw (круг ещё не разобран или транзакция не
    пришла в поток) -- то же самое: % не считается, причина названа;
  * покупка Bloom упала по цепи (chain_ok=false) -- факта выхода нет, это
    тоже причина, а не "наша хуже на 100 %".

Сравнение выхода. Тень считает expected_out/min_out в СЫРЫХ единицах
токена по резервам пула (c2_swap_build.min_out_from_reserves) -- это чистая
цена пула, БЕЗ комиссии Bloom и без произвольного налога токена на
перевод. Bloom бьёт по цепи bought_raw -- тоже сырые единицы токена
(bloom_own_send.купленное_raw), но это УЖЕ ПОСЛЕ всего: комиссии
площадки, налога токена, проскальзывания его маршрута. Поэтому их разница
в процентах -- это как раз "во сколько тень выигрывает/проигрывает после
налога и комиссий Bloom", а не сравнение яблок с яблоками по промежуточным
числам; отдельно об этом сказано в pct_note каждой строки.

Интерфейс:
    python3 analysis/night_p1_shadow_vs_bloom.py --state-dir PATH \
        --since 2026-09-24T00:00:00Z --out data/night_p1.json
    python3 analysis/night_p1_shadow_vs_bloom.py --self-test
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal as D
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Подпись НАШЕЙ покупки Bloom по сделке PICKAXE -- дословно из вопроса
# владельца, а не найдена скриптом (сравнение идёт по позиции, у которой
# эта подпись есть в поле "signatures").
ПОДПИСЬ_PICKAXE = ("2Vs5qv9V2HVhbNzd5sMzgfqLFZ37SxbbMDUBjm6a6PaFkX6N6m8BGgzEePAp4"
                    "uvdiwQvV8E2UaCorAktn2Lo5fCS")

# Тот же порог, что владелец задал для боевых покупок 24.09 (c2_common.OURS_FROM_UTC).
С_ДАТЫ_ПО_УМОЛЧАНИЮ = "2026-09-24T00:00:00Z"
КАТАЛОГ_ПО_УМОЛЧАНИЮ = "/home/bot/bloom_executor_live_data"

# Режим позиции, за которым НЕТ настоящих денег -- как в bloom_exec_state.py
# (MODE_DRY): такие покупки не идут ни в одну сводку никого в репозитории,
# и здесь по той же причине не идут в таблицу "покупки Bloom".
РЕЖИМ_СУХОЙ = "dry-run"


# ------------------------------------------------------------ чтение журналов

def прочитать_jsonl(путь: Path) -> tuple[list, str | None]:
    """Строки JSONL целиком в память. Битая строка пропускается, а не рушит
    разбор -- ровно так же, как читает исполнитель (bloom_exec_state.positions,
    bloom_report.читать_решения). Причина возвращается, а не проглатывается."""
    if not путь.exists():
        return [], f"файла нет: {путь}"
    try:
        текст = путь.read_text(encoding="utf-8")
    except OSError as exc:
        return [], f"файл не читается: {type(exc).__name__}: {str(exc)[:160]}"
    строки, битых = [], 0
    for line in текст.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            строки.append(json.loads(line))
        except ValueError:
            битых += 1
    if not строки:
        return [], "файл пуст" + (f" ({битых} битых строк)" if битых else "")
    return строки, (f"{битых} битых строк пропущено" if битых else None)


def позиции_bloom(state_dir: Path) -> tuple[dict, str | None]:
    """Текущее состояние всех позиций -- переигрыш positions.jsonl построчно
    (та же логика, что ExecState.positions(): каждая новая строка ДОПОЛНЯЕТ
    словарь по client_order_id, а не заменяет его). Своя реализация, а не
    вызов ExecState, потому что тот при создании делает mkdir -- скрипту,
    который обещал только читать, ничего создавать на диске службы нельзя."""
    строки, почему = прочитать_jsonl(state_dir / "positions.jsonl")
    by: dict = {}
    for row in строки:
        cid = row.get("client_order_id")
        if not cid:
            continue
        by.setdefault(cid, {}).update(row)
    return by, (почему if not by else None)


def тени_по_источнику(state_dir: Path) -> tuple[dict, str | None]:
    """{подпись источника: последняя запись stage=shadow по ней}.

    Источник ключа -- поле "signature" записи (в _тень_внутри это ВСЕГДА
    подпись сигнала-источника, а не наша). Последняя запись побеждает: если
    тень почему-то собиралась дважды на одну подпись, доклад владельца
    (bloom_report.таблица_кругов) точно так же берёт последнюю."""
    строки, почему = прочитать_jsonl(state_dir / "decisions.jsonl")
    out = {}
    for r in строки:
        if r.get("stage") == "shadow" and r.get("signature"):
            out[r["signature"]] = r
    if not out:
        return out, (почему or "в decisions.jsonl нет ни одной строки stage=shadow")
    return out, None


def это_покупка_bloom(поз: dict) -> bool:
    """Покупка САМОГО Bloom, а не полосы своей отправки и не выдумка dry-run.

    У позиций полосы поле lane == "own_send" (bloom_exec_state.МЕТКА_ПОЛОСЫ);
    у покупок Bloom поля lane нет вовсе. Сравнивать тень есть смысл только с
    Bloom: полоса собирает и отправляет САМА, ей тень не при чём."""
    return not поз.get("lane") and поз.get("mode") != РЕЖИМ_СУХОЙ


def после_даты(ts_utc, since: str) -> bool:
    """Строки "%Y-%m-%dT%H:%M:%SZ" сравнимы лексикографически как даты --
    тот же приём, что и у читающих decisions.jsonl в bloom_report.py, только
    там сравнение идёт по числу секунд, а здесь по строке уже готового поля
    ts_intent_utc (эпоха в позиции есть не всегда, а строка -- всегда)."""
    return isinstance(ts_utc, str) and bool(ts_utc) and ts_utc >= since


def медиана(числа: list) -> float | None:
    з = sorted(x for x in числа if isinstance(x, (int, float)))
    if not з:
        return None
    n = len(з)
    return з[n // 2] if n % 2 else (з[n // 2 - 1] + з[n // 2]) / 2


# ------------------------------------------------------------ сравнение одной сделки

def маршрут_bloom(поз: dict) -> dict:
    """Маршрут САМОГО Bloom по факту цепи -- пишет bloom_detector.разобрать_нашу_покупку
    в позицию (our_route_programs, our_pool_direct); поля нет -- маршрут не разобран."""
    return {"programs": поз.get("our_route_programs"),
            "direct": поз.get("our_pool_direct"),
            "hops_by_mints": поз.get("our_route_hops")}


def сверить_сделку(поз: dict, тень: dict | None) -> dict:
    """Одна строка итоговой таблицы по одной покупке Bloom.

    "собрала" здесь значит РОВНО то, что означает ok+supported у самого
    shadow_build: тень собрала транзакцию и её симулировала (независимо от
    вердикта симуляции -- would_pass или slippage/balance/other_error, это
    отдельное поле). "не собрала" -- когда тень вообще не смогла построить
    покупку (пул не найден/не покрыт, второй шаг без шаблона и т. п.), и
    тогда why_not -- причина самой тени, без пересказа своими словами.
    """
    итог = {
        "client_order_id": поз.get("client_order_id"),
        "mint": поз.get("mint"),
        "ts_intent_utc": поз.get("ts_intent_utc"),
        "source_sig": поз.get("source_sig"),
        "bloom_signature": next(iter(поз.get("signatures") or []), None),
        "chain_ok": поз.get("chain_ok"),
        "bloom_route": маршрут_bloom(поз),
        "collected": False,
        "why_not": None,
        "shadow_route": None,
        "shadow_pool_program": None,
        "shadow_pool_label": None,
        "shadow_sim_verdict": None,
        "shadow_build_ms": None,
        "shadow_expected_out_raw": None,
        "shadow_min_out_raw": None,
        "shadow_why_not": None,
        "route_agrees_with_bloom": None,
        "bloom_bought_raw": поз.get("bought_raw"),
        "diff_pct": None,
        "verdict_ours": None,
        "pct_note": ("тень посчитана без комиссии Bloom и без налога токена на "
                     "перевод -- Bloom bought_raw их уже несёт, поэтому diff_pct "
                     "показывает разницу С УЧЁТОМ них, а не чистую разницу цены пула"),
    }
    if поз.get("chain_ok") is False:
        итог["why_not"] = "покупка Bloom упала по цепи (chain_ok=false): факта выхода нет"

    if тень is None:
        итог["why_not"] = (итог["why_not"] or
                            "тени нет: в decisions.jsonl нет строки stage=shadow "
                            "по подписи источника этой покупки")
        return итог

    итог.update(
        shadow_route=тень.get("route"),
        shadow_pool_program=тень.get("pool_program"),
        shadow_pool_label=тень.get("pool_label"),
        shadow_sim_verdict=тень.get("sim_verdict"),
        shadow_build_ms=тень.get("build_ms"),
        shadow_expected_out_raw=тень.get("expected_out"),
        shadow_min_out_raw=тень.get("min_out"),
        shadow_why_not=тень.get("why_not"))

    if тень.get("ok") is not True or тень.get("supported") is not True:
        итог["collected"] = False
        итог["why_not"] = (итог["why_not"] or тень.get("why_not") or
                            "тень отметила ok/supported=false без пояснения в журнале")
        return итог

    итог["collected"] = True
    прямой_bloom = поз.get("our_pool_direct")
    if прямой_bloom is None:
        итог["route_agrees_with_bloom"] = None
    else:
        одним_прыжком_тень = тень.get("route") == "one_hop"
        итог["route_agrees_with_bloom"] = (одним_прыжком_тень == bool(прямой_bloom))

    if итог["why_not"]:
        return итог  # покупка Bloom упала по цепи -- сравнивать выход не с чем

    if поз.get("bought_raw") is None:
        итог["why_not"] = ("у покупки Bloom нет bought_raw в позиции (круг ещё не "
                            "разобран bloom_detector.own_tx_seen, либо мета транзакции "
                            "не пришла в поток)")
        return итог
    if тень.get("expected_out") is None:
        итог["why_not"] = (f"у тени нет expected_out: "
                            f"{тень.get('min_out_method') or 'метод не выдаётся'}")
        return итог

    bloom_raw = D(str(поз["bought_raw"]))
    if bloom_raw <= 0:
        итог["why_not"] = "Bloom купил 0 сырых единиц по цепи -- делить не на что"
        return итог
    shadow_raw = D(str(тень["expected_out"]))
    diff = (shadow_raw - bloom_raw) / bloom_raw * D(100)
    diff_f = float(diff)
    итог["diff_pct"] = round(diff_f, 3)
    if diff_f > 1e-9:
        итог["verdict_ours"] = f"наша лучше на {abs(round(diff_f, 2))}%"
    elif diff_f < -1e-9:
        итог["verdict_ours"] = f"наша хуже на {abs(round(diff_f, 2))}%"
    else:
        итог["verdict_ours"] = "совпадает"
    return итог


# ------------------------------------------------------------ сводная таблица

def таблица_и_сводка(state_dir: Path, since: str) -> dict:
    позиции, поч_поз = позиции_bloom(state_dir)
    тени, поч_тен = тени_по_источнику(state_dir)

    все = list(поз for поз in позиции.values())
    исключено_не_bloom = sum(1 for p in все if not это_покупка_bloom(p))
    кандидаты = [p for p in все if это_покупка_bloom(p)]
    исключено_до_даты = sum(1 for p in кандидаты if not после_даты(p.get("ts_intent_utc"), since))
    отобрано = [p for p in кандидаты if после_даты(p.get("ts_intent_utc"), since)]

    строки = [сверить_сделку(p, тени.get(p.get("source_sig"))) for p in отобрано]
    строки.sort(key=lambda r: r.get("ts_intent_utc") or "")

    собрано = sum(1 for r in строки if r["collected"])
    доля = round(собрано / len(строки), 4) if строки else None
    разницы = [r["diff_pct"] for r in строки if r["diff_pct"] is not None]
    return {
        "since": since,
        "state_dir": str(state_dir),
        "positions_why_not": поч_поз,
        "shadow_why_not": поч_тен,
        "excluded_not_bloom_lane_or_dry": исключено_не_bloom,
        "excluded_before_since": исключено_до_даты,
        "total_bloom_trades": len(строки),
        "collected": собрано,
        "collected_share": доля,
        "with_diff_pct": len(разницы),
        "median_diff_pct": (round(медиана(разницы), 3) if разницы else None),
        "rows": строки,
    }


def сделка_pickaxe(state_dir: Path, подпись: str = ПОДПИСЬ_PICKAXE) -> dict:
    """Разбор ровно одной сделки владельца -- PICKAXE."""
    позиции, поч_поз = позиции_bloom(state_dir)
    поз = next((p for p in позиции.values() if подпись in (p.get("signatures") or [])), None)
    if поз is None:
        return {"ok": False, "signature": подпись,
                "why_not": (f"позиции с подписью Bloom {подпись} нет в positions.jsonl "
                            f"({поч_поз or 'каталог прочитан, такой подписи нет'})")}
    тени, поч_тен = тени_по_источнику(state_dir)
    тень = тени.get(поз.get("source_sig"))
    строка = сверить_сделку(поз, тень)
    if тень is None and поч_тен:
        строка["why_not"] = f"{строка['why_not']} ({поч_тен})"
    return {"ok": True, **строка}


# ------------------------------------------------------------------ самопроверка

def self_test() -> int:
    checks: list = []

    def chk(name, ok, got=""):
        checks.append((name, bool(ok), got))

    import tempfile
    tmp = Path(tempfile.mkdtemp())

    def записать(имя: str, строки: list) -> None:
        with (tmp / имя).open("w", encoding="utf-8") as f:
            for r in строки:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 1. Каталога вообще нет -- честная причина, без падения.
    нет_каталога = Path(tempfile.mkdtemp()) / "нет_такого"
    п, почему = позиции_bloom(нет_каталога)
    chk("нет каталога позиций -- пустой словарь и причина, а не молчание",
        п == {} and почему and "файла нет" in почему, почему)

    # 2. positions.jsonl пуст.
    записать("positions.jsonl", [])
    п2, почему2 = позиции_bloom(tmp)
    chk("пустой positions.jsonl -- причина названа", п2 == {} and почему2, почему2)

    # 3. Позиции есть, decisions.jsonl нет вовсе -- тени пустые с причиной.
    ОБЫЧНАЯ = {
        "client_order_id": "c1", "mint": "MINT1", "source_sig": "SRC1",
        "ts_intent_utc": "2026-09-24T10:00:00Z", "mode": "live",
        "signatures": ["BLOOMSIG1"], "chain_ok": True, "bought_raw": 1_000_000,
        "our_pool_direct": True, "our_route_programs": "PUMP_AMM",
    }
    записать("positions.jsonl", [ОБЫЧНАЯ])
    тени0, почему0 = тени_по_источнику(tmp)
    chk("нет decisions.jsonl -- тени пустые, причина названа",
        тени0 == {} and почему0 and "файла нет" in почему0, почему0)
    итог0 = таблица_и_сводка(tmp, С_ДАТЫ_ПО_УМОЛЧАНИЮ)
    chk("без тени сделка Bloom всё равно попадает в таблицу как несобранная",
        итог0["total_bloom_trades"] == 1 and итог0["rows"][0]["collected"] is False
        and "тени нет" in итог0["rows"][0]["why_not"], итог0)

    # 4. Тень собрала и посчитала выход лучше Bloom (наглядное число).
    ТЕНЬ_УСПЕХ = {"stage": "shadow", "signature": "SRC1", "mint": "MINT1",
                  "ok": True, "supported": True, "route": "one_hop",
                  "pool_program": "PUMP_AMM", "pool_label": "Pump AMM",
                  "sim_verdict": "would_pass", "build_ms": 3.21,
                  "expected_out": 1_050_000, "min_out": 900_000, "why_not": None}
    записать("decisions.jsonl", [ТЕНЬ_УСПЕХ])
    итог1 = таблица_и_сводка(tmp, С_ДАТЫ_ПО_УМОЛЧАНИЮ)
    r1 = итог1["rows"][0]
    ожид_diff = float((D("1050000") - D("1000000")) / D("1000000") * 100)
    chk("собрана, маршрут совпал с Bloom (одним прыжком у обоих), diff и вердикт верны",
        r1["collected"] is True and r1["route_agrees_with_bloom"] is True
        and abs(r1["diff_pct"] - round(ожид_diff, 3)) < 1e-6
        and r1["verdict_ours"] == "наша лучше на 5.0%", r1)
    chk("сводка: доля собранных 1.0, медиана = diff единственной сделки",
        итог1["collected_share"] == 1.0 and итог1["median_diff_pct"] == r1["diff_pct"], итог1)

    # 5. Тень провалилась с честной причиной (тип пула не покрыт) -- сравнения нет.
    ПОЗ_2 = dict(ОБЫЧНАЯ, client_order_id="c2", source_sig="SRC2",
                 signatures=["BLOOMSIG2"], mint="MINT2")
    ТЕНЬ_ПРОВАЛ = {"stage": "shadow", "signature": "SRC2", "mint": "MINT2",
                    "ok": False, "supported": False, "route": "one_hop",
                    "why_not": "тип пула не покрыт сборщиком: Whirlpool"}
    записать("positions.jsonl", [ОБЫЧНАЯ, ПОЗ_2])
    записать("decisions.jsonl", [ТЕНЬ_УСПЕХ, ТЕНЬ_ПРОВАЛ])
    итог2 = таблица_и_сводка(tmp, С_ДАТЫ_ПО_УМОЛЧАНИЮ)
    r2 = next(r for r in итог2["rows"] if r["client_order_id"] == "c2")
    chk("тень не собрала пул -- причина дословно из тени, diff не считается",
        r2["collected"] is False and r2["why_not"] == "тип пула не покрыт сборщиком: Whirlpool"
        and r2["diff_pct"] is None, r2)

    # 6. Тень собрала, но не покрытие даёт expected_out (DAMM2 и т.п.).
    ПОЗ_3 = dict(ОБЫЧНАЯ, client_order_id="c3", source_sig="SRC3",
                 signatures=["BLOOMSIG3"], mint="MINT3")
    ТЕНЬ_БЕЗ_ЦЕНЫ = {"stage": "shadow", "signature": "SRC3", "mint": "MINT3",
                      "ok": True, "supported": True, "route": "one_hop",
                      "pool_program": "DAMM2", "sim_verdict": "would_pass",
                      "expected_out": None, "min_out": None,
                      "min_out_method": "не выдаётся: сосредоточенная ликвидность: резервы цену не дают"}
    записать("positions.jsonl", [ОБЫЧНАЯ, ПОЗ_2, ПОЗ_3])
    записать("decisions.jsonl", [ТЕНЬ_УСПЕХ, ТЕНЬ_ПРОВАЛ, ТЕНЬ_БЕЗ_ЦЕНЫ])
    итог3 = таблица_и_сводка(tmp, С_ДАТЫ_ПО_УМОЛЧАНИЮ)
    r3 = next(r for r in итог3["rows"] if r["client_order_id"] == "c3")
    chk("тень собрала, но выход не выдаётся (DAMM2) -- собрана=True, diff всё равно None",
        r3["collected"] is True and r3["diff_pct"] is None
        and "не выдаётся" in r3["why_not"], r3)

    # 7. Покупка Bloom упала по цепи -- факта выхода нет, даже если тень отличная.
    ПОЗ_4 = dict(ОБЫЧНАЯ, client_order_id="c4", source_sig="SRC1",
                 signatures=["BLOOMSIG4"], mint="MINT4", chain_ok=False, bought_raw=None)
    записать("positions.jsonl", [ОБЫЧНАЯ, ПОЗ_2, ПОЗ_3, ПОЗ_4])
    итог4 = таблица_и_сводка(tmp, С_ДАТЫ_ПО_УМОЛЧАНИЮ)
    r4 = next(r for r in итог4["rows"] if r["client_order_id"] == "c4")
    chk("покупка Bloom упала по цепи -- причина именно об этом, diff не считается",
        "упала по цепи" in r4["why_not"] and r4["diff_pct"] is None, r4)

    # 8. Полоса своей отправки (lane=own_send) и dry-run -- в таблицу Bloom не идут.
    ПОЗ_ПОЛОСА = dict(ОБЫЧНАЯ, client_order_id="c5", lane="own_send")
    ПОЗ_СУХАЯ = dict(ОБЫЧНАЯ, client_order_id="c6", mode="dry-run")
    записать("positions.jsonl", [ОБЫЧНАЯ, ПОЗ_2, ПОЗ_3, ПОЗ_4, ПОЗ_ПОЛОСА, ПОЗ_СУХАЯ])
    итог5 = таблица_и_сводка(tmp, С_ДАТЫ_ПО_УМОЛЧАНИЮ)
    chk("полоса и dry-run исключены из таблицы покупок Bloom",
        итог5["total_bloom_trades"] == 4 and итог5["excluded_not_bloom_lane_or_dry"] == 2,
        (итог5["total_bloom_trades"], итог5["excluded_not_bloom_lane_or_dry"]))

    # 9. Фильтр по дате: сделка раньше --since не попадает в таблицу.
    ПОЗ_РАНЬШЕ = dict(ОБЫЧНАЯ, client_order_id="c7", ts_intent_utc="2026-09-01T00:00:00Z")
    записать("positions.jsonl",
             [ОБЫЧНАЯ, ПОЗ_2, ПОЗ_3, ПОЗ_4, ПОЗ_ПОЛОСА, ПОЗ_СУХАЯ, ПОЗ_РАНЬШЕ])
    итог6 = таблица_и_сводка(tmp, С_ДАТЫ_ПО_УМОЛЧАНИЮ)
    chk("сделка до --since исключена, счётчик исключений верный",
        итог6["total_bloom_trades"] == 4 and итог6["excluded_before_since"] == 1,
        (итог6["total_bloom_trades"], итог6["excluded_before_since"]))

    # 10. Тень хуже Bloom -- проверяем знак вердикта в обратную сторону.
    ТЕНЬ_ХУЖЕ = {"stage": "shadow", "signature": "SRC_HUJE", "mint": "MINTH",
                 "ok": True, "supported": True, "route": "two_hop",
                 "pool_program": "DLMM", "sim_verdict": "would_pass",
                 "expected_out": 900_000, "min_out": 800_000, "why_not": None}
    ПОЗ_ХУЖЕ = dict(ОБЫЧНАЯ, client_order_id="c8", source_sig="SRC_HUJE",
                     signatures=["BLOOMSIGH"], mint="MINTH", bought_raw=1_000_000,
                     our_pool_direct=False)
    записать("positions.jsonl",
             [ОБЫЧНАЯ, ПОЗ_2, ПОЗ_3, ПОЗ_4, ПОЗ_ПОЛОСА, ПОЗ_СУХАЯ, ПОЗ_РАНЬШЕ, ПОЗ_ХУЖЕ])
    записать("decisions.jsonl", [ТЕНЬ_УСПЕХ, ТЕНЬ_ПРОВАЛ, ТЕНЬ_БЕЗ_ЦЕНЫ, ТЕНЬ_ХУЖЕ])
    итог7 = таблица_и_сводка(tmp, С_ДАТЫ_ПО_УМОЛЧАНИЮ)
    r8 = next(r for r in итог7["rows"] if r["client_order_id"] == "c8")
    chk("тень хуже Bloom -- отрицательный diff и вердикт «хуже», маршрут (two_hop против непрямого) совпал",
        r8["diff_pct"] == -10.0 and r8["verdict_ours"] == "наша хуже на 10.0%"
        and r8["route_agrees_with_bloom"] is True, r8)

    # 11. Медиана по нескольким сделкам считается по всем diff_pct, а не по первой.
    жду_медиану = медиана([r["diff_pct"] for r in итог7["rows"] if r["diff_pct"] is not None])
    chk("медиана сводки использует ВСЕ diff_pct таблицы",
        итог7["median_diff_pct"] == round(жду_медиану, 3), (итог7["median_diff_pct"], жду_медиану))

    # 12. bought_raw отсутствует у иначе успешной сделки -- собрана, но diff не считается.
    ПОЗ_БЕЗ_КОЛ = dict(ОБЫЧНАЯ, client_order_id="c9", source_sig="SRC1",
                        signatures=["BLOOMSIG9"], mint="MINT9", bought_raw=None)
    записать("positions.jsonl",
             [ОБЫЧНАЯ, ПОЗ_2, ПОЗ_3, ПОЗ_4, ПОЗ_ПОЛОСА, ПОЗ_СУХАЯ, ПОЗ_РАНЬШЕ, ПОЗ_ХУЖЕ,
              ПОЗ_БЕЗ_КОЛ])
    итог8 = таблица_и_сводка(tmp, С_ДАТЫ_ПО_УМОЛЧАНИЮ)
    r9 = next(r for r in итог8["rows"] if r["client_order_id"] == "c9")
    chk("тень успешна, но у Bloom нет bought_raw -- собрана=True, diff=None, причина об этом",
        r9["collected"] is True and r9["diff_pct"] is None and "bought_raw" in r9["why_not"], r9)

    # 13. Сделка PICKAXE: не найдена в позициях -- честная причина, адрес не выдуман.
    итог_pickaxe_нет = сделка_pickaxe(tmp, "НЕТУ_ТАКОЙ_ПОДПИСИ")
    chk("PICKAXE: подписи нет в позициях -- ok=False с причиной",
        итог_pickaxe_нет["ok"] is False and "НЕТУ_ТАКОЙ_ПОДПИСИ" in итог_pickaxe_нет["why_not"],
        итог_pickaxe_нет)

    # 14. Сделка PICKAXE: найдена, разобрана тем же кодом, что и общая таблица.
    ПОЗ_PICKAXE = dict(ОБЫЧНАЯ, client_order_id="cP", source_sig="SRC1",
                        signatures=[ПОДПИСЬ_PICKAXE], mint="PICKAXE_MINT")
    записать("positions.jsonl",
             [ОБЫЧНАЯ, ПОЗ_2, ПОЗ_3, ПОЗ_4, ПОЗ_ПОЛОСА, ПОЗ_СУХАЯ, ПОЗ_РАНЬШЕ, ПОЗ_ХУЖЕ,
              ПОЗ_БЕЗ_КОЛ, ПОЗ_PICKAXE])
    итог_pickaxe = сделка_pickaxe(tmp)
    chk("PICKAXE: найдена по подписи Bloom, маршрут и выход разобраны",
        итог_pickaxe["ok"] is True and итог_pickaxe["collected"] is True
        and итог_pickaxe["shadow_pool_program"] == "PUMP_AMM", итог_pickaxe)

    # 15. Позиция без client_order_id в positions.jsonl не ломает разбор (пропускается).
    п_без_cid, _ = позиции_bloom(tmp)
    записать("positions.jsonl", [{"mint": "БЕЗ_CID"}])
    п_мусор, _ = позиции_bloom(tmp)
    chk("строка без client_order_id пропускается, а не падает",
        п_мусор == {}, п_мусор)

    # 16. Строка decisions.jsonl без stage=shadow не путается с тенью.
    записать("decisions.jsonl", [{"stage": "exec_result", "signature": "SRC1", "mint": "MINT1"}])
    тени_чужие, поч_чужие = тени_по_источнику(tmp)
    chk("строки других stage не считаются тенью", тени_чужие == {} and bool(поч_чужие),
        поч_чужие)

    bad = 0
    for name, ok, got in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {name}" + (f"  -> {str(got)[:300]}" if not ok else ""))
        bad += (not ok)
    print(f"самопроверка night_p1_shadow_vs_bloom: {len(checks) - bad}/{len(checks)} пройдено")
    return 0 if bad == 0 else 1


# ------------------------------------------------------------------------- CLI

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state-dir", default=КАТАЛОГ_ПО_УМОЛЧАНИЮ,
                   help="каталог журналов исполнителя (decisions.jsonl, positions.jsonl)")
    p.add_argument("--since", default=С_ДАТЫ_ПО_УМОЛЧАНИЮ,
                   help="нижняя граница ts_intent_utc, включительно (UTC, как в позиции)")
    p.add_argument("--out", default="data/night_p1.json")
    p.add_argument("--pickaxe-signature", default=ПОДПИСЬ_PICKAXE,
                   help="подпись нашей покупки Bloom по сделке PICKAXE")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test:
        return self_test()

    state_dir = Path(a.state_dir)
    сводка = таблица_и_сводка(state_dir, a.since)
    pickaxe = сделка_pickaxe(state_dir, a.pickaxe_signature)
    итог = {
        "ok": True,
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pickaxe": pickaxe,
        "summary": {k: сводка[k] for k in (
            "since", "state_dir", "positions_why_not", "shadow_why_not",
            "excluded_not_bloom_lane_or_dry", "excluded_before_since",
            "total_bloom_trades", "collected", "collected_share",
            "with_diff_pct", "median_diff_pct")},
        "rows": сводка["rows"],
    }
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(итог, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"PICKAXE ({a.pickaxe_signature[:16]}...): "
          + (f"собрала={pickaxe.get('collected')}, why_not={pickaxe.get('why_not')}"
             if pickaxe.get("ok") else f"не найдена: {pickaxe.get('why_not')}"))
    print(f"покупок Bloom с {a.since}: {сводка['total_bloom_trades']}, "
          f"собрано тенью: {сводка['collected']} "
          f"(доля {сводка['collected_share']}), медиана разницы: {сводка['median_diff_pct']}%")
    print(f"позиции: {сводка['positions_why_not'] or 'прочитаны'}; "
          f"тени: {сводка['shadow_why_not'] or 'прочитаны'}")
    print(f"записано в {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
