#!/usr/bin/env python3
"""Задача 5, живой бот -- пересчёт результатов
`task5_writepath_test_{nl,dallas}_result.json` (2026-09-12/13 сессия).

ПОЧЕМУ пересчёт нужен (честно, зафиксировано владельцем ДО этого
запуска): `lag_ms_send_to_local_poll_confirmation` в исходных файлах
измеряет время от `t_send` (локальный `time.time()` до сетевого вызова
`eth_sendRawTransaction`) до момента, когда ЛОКАЛЬНЫЙ поллинг
(`eth_getTransactionReceipt` каждые 50мс через `rpc.mainnet...`,
Cloudflare-хост) впервые увидел рецепт. Это верхняя граница реального
времени включения, загрязнённая (а) задержкой Cloudflare-anycast на
ЧТЕНИЕ рецепта (не связана с тем, ЧЕРЕЗ какой путь транзакция была
ОТПРАВЛЕНА), (б) джиттером самого 50мс-цикла поллинга, (в) тем, что обе
попытки (rpc/sequencer submit) читают рецепт ОДНИМ и тем же
Cloudflare-путём -- то есть `lag_ms` частично измеряет путь ЧТЕНИЯ,
общий для обеих серий, а не путь ЗАПИСИ, который и является предметом
сравнения. Прямое сравнение медиан `lag_ms` между `rpc_mainnet` и
`sequencer_mainnet_guess` поэтому НЕ отвечает на вопрос "какой путь
записи быстрее/стабильнее" настолько чисто, насколько хотелось бы.

ЧТО этот скрипт считает вместо этого -- ТОЛЬКО из `block_number` и
`block_timestamp` (данные, которые сеть подтверждает сама, не зависят от
локального поллинга):

1. Реальный темп блоков в окне замера (`s/block`) -- медиана
   `Δblock_timestamp / Δblock_number` между последовательными попытками,
   плюс кросс-чек тем же отношением, но по `Δt_send` (локальные часы,
   секундное разрешение `block_timestamp` не участвует) -- если два
   независимых метода сходятся, доверия больше.
2. Относительное сравнение путей `rpc_mainnet` vs
   `sequencer_mainnet_guess` В БЛОКАХ, не в мс: строится кусочно-линейная
   карта время->номер_блока по всем реальным парам
   `(block_timestamp, block_number)` серии, по ней оценивается номер
   блока, актуальный в момент `t_send` каждой попытки, и считается
   `blocks_to_inclusion = block_number_включения - оценка_блока_на_t_send`.
   Эта величина не зависит от Cloudflare-поллинга рецепта вообще -- она
   зависит только от `t_send` (чисто локальные часы, ДО сетевого вызова
   отправки) и от `block_number`/`block_timestamp` включения (данные
   цепи). Более отрицательное значение = транзакция попала в блок
   РАНЬШЕ, чем предсказывала линейная экстраполяция по соседним точкам
   (путь быстрее/агрессивнее); меньший разброс (stdev) = путь стабильнее.

ЧЕСТНЫЕ ОГОВОРКИ (не скрывать):
- `block_timestamp` -- секундное разрешение (подтверждено ранее в этом
  паспорте: `block_timestamp` совпадал с `int(t_send)` на n=1 замере).
  При темпе ~0.1 с/блок это ~9-10 блоков за одну секунду -- линейная
  интерполяция между двумя РЕАЛЬНЫМИ соседними точками сглаживает это
  (использует фактический номер блока на обоих концах интервала, не
  просто "секунда -> ×10 блоков"), но всё равно вносит по конструкции
  ошибку оценки на уровне единиц блоков, не долей блока.
- n=5 успешных попыток на путь на каждую локацию -- statistically мало,
  вывод ниже про НАПРАВЛЕНИЕ эффекта (какой путь стабильнее/раньше), не
  про точную величину преимущества в блоках.
- Кросс-чек "сырые блоки между последовательными включениями, по
  эндпоинту ПОЗДНЕЙ попытки" (`raw_blocks_between_inclusions_by_endpoint`
  в выводе) специально НЕ используется как основной вывод -- он
  загрязнён локальной паузой скрипта (0.5с + вызов `eth_getBlockByNumber`
  для base fee перед КАЖДОЙ отправкой, переменная длительность), поэтому
  показывает почти одинаковый разброс для обоих путей -- это ожидаемо и
  само по себе демонстрирует, ПОЧЕМУ нужен именно `blocks_to_inclusion`
  (привязанный к `t_send`, а не к предыдущему включению).
- Это диагностика путей ОТПРАВКИ на уже собранных данных, не новый
  запуск и не отправка транзакций -- скрипт только читает существующие
  JSON.

Запуск: python3 analysis/task5_writepath_block_recompute.py
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

RESULT_FILES = {
    "nl": "data/p3_guard_cache/task5_writepath_test_nl_result.json",
    "dallas": "data/p3_guard_cache/task5_writepath_test_dallas_result.json",
}


def _block_cadence(atts: list[dict]) -> dict:
    from_ts, from_wall = [], []
    for i in range(1, len(atts)):
        db = atts[i]["block_number"] - atts[i - 1]["block_number"]
        if db <= 0:
            continue
        from_ts.append((atts[i]["block_timestamp"] - atts[i - 1]["block_timestamp"]) / db)
        from_wall.append((atts[i]["t_send"] - atts[i - 1]["t_send"]) / db)
    return {
        "n_pairs": len(from_ts),
        "median_s_per_block_from_block_timestamp": statistics.median(from_ts) if from_ts else None,
        "median_s_per_block_from_t_send_wall_clock": statistics.median(from_wall) if from_wall else None,
    }


def _est_block_at(t_epoch: float, atts: list[dict], fallback_cadence: float) -> float | None:
    before = [a for a in atts if a["block_timestamp"] <= t_epoch]
    after = [a for a in atts if a["block_timestamp"] >= t_epoch]
    if before and after and before[-1] is not after[0]:
        b0, t0 = before[-1]["block_number"], before[-1]["block_timestamp"]
        b1, t1 = after[0]["block_number"], after[0]["block_timestamp"]
        if t1 == t0:
            return float(b0)
        frac = (t_epoch - t0) / (t1 - t0)
        return b0 + frac * (b1 - b0)
    if before:
        b0, t0 = before[-1]["block_number"], before[-1]["block_timestamp"]
        return b0 + (t_epoch - t0) / fallback_cadence
    if after:
        b1, t1 = after[0]["block_number"], after[0]["block_timestamp"]
        return b1 - (t1 - t_epoch) / fallback_cadence
    return None


def _summary(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0}
    out = {"n": len(vals), "median": statistics.median(vals), "mean": statistics.mean(vals),
           "min": min(vals), "max": max(vals)}
    if len(vals) > 1:
        out["stdev"] = statistics.pstdev(vals)
    return out


def analyze_one(path: str, location_label: str) -> dict:
    d = json.loads(Path(path).read_text())
    atts = sorted([a for a in d["attempts"] if "block_number" in a], key=lambda a: a["attempt"])
    cadence = _block_cadence(atts)
    fallback_cadence = cadence["median_s_per_block_from_block_timestamp"] or 0.12

    by_ep: dict[str, list[float]] = {"rpc_mainnet": [], "sequencer_mainnet_guess": []}
    for a in atts:
        est = _est_block_at(a["t_send"], atts, fallback_cadence)
        if est is None:
            continue
        by_ep.setdefault(a["submit_endpoint"], []).append(a["block_number"] - est)

    raw_between: dict[str, list[int]] = {"rpc_mainnet": [], "sequencer_mainnet_guess": []}
    for i in range(1, len(atts)):
        raw_between.setdefault(atts[i]["submit_endpoint"], []).append(
            atts[i]["block_number"] - atts[i - 1]["block_number"]
        )

    return {
        "location_label": location_label,
        "n_included": len(atts),
        "block_cadence": cadence,
        "blocks_to_inclusion_by_endpoint": {ep: _summary(v) for ep, v in by_ep.items()},
        "raw_blocks_between_inclusions_by_endpoint_CAUTION_confounded": {
            ep: _summary(v) for ep, v in raw_between.items()
        },
    }


def main() -> None:
    result = {loc: analyze_one(path, loc) for loc, path in RESULT_FILES.items()}

    verdict_lines = []
    for loc, r in result.items():
        rpc = r["blocks_to_inclusion_by_endpoint"].get("rpc_mainnet", {})
        seq = r["blocks_to_inclusion_by_endpoint"].get("sequencer_mainnet_guess", {})
        if rpc.get("n") and seq.get("n"):
            faster = "sequencer_mainnet_guess" if seq["median"] < rpc["median"] else "rpc_mainnet"
            tighter = "sequencer_mainnet_guess" if seq.get("stdev", 1e9) < rpc.get("stdev", 1e9) else "rpc_mainnet"
            verdict_lines.append(
                f"{loc}: median blocks_to_inclusion rpc={rpc['median']:.2f} seq={seq['median']:.2f} "
                f"(faster/earlier: {faster}); stdev rpc={rpc.get('stdev', float('nan')):.2f} "
                f"seq={seq.get('stdev', float('nan')):.2f} (tighter: {tighter})"
            )
    result["verdict"] = verdict_lines

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
