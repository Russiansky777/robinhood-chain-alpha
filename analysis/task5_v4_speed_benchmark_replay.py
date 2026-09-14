#!/usr/bin/env python3
"""Component B (владелец, п.3): replay потока с исходными интервалами
поступления и очередью -- ЛОКАЛЬНЫЙ, БЕЗ СЕТИ анализ уже сохранённого
захвата data/task5_bot_router_capture/dump.jsonl.gz (реальный, 2026-09-12,
300с, ws://127.0.0.1:9642 -- см. data/task5_bot_router_capture_summary.
json).

ЧЕСТНО О НЕДОСТАЮЩИХ ПОЛЯХ (владелец: "если данных не хватает --
перечисли конкретные отсутствующие поля"): каждая запись этого захвата
содержит ТОЛЬКО {"sequence_number", "to", "data"} -- НЕТ поля реального
времени прихода отдельного сообщения/кадра (ни monotonic, ни wall).
Поэтому НАСТОЯЩИЙ replay "с исходными интервалами поступления" (как
буквально запрошено) из ЭТИХ данных НЕВОЗМОЖЕН -- отсутствует именно
per-message arrival timestamp. Единственное, что известно точно из
summary.json: общая длительность захвата (300.0с) и общее число
WS-кадров (3600) и декодированных tx (41695).

ЧТО СДЕЛАНО ВМЕСТО ЭТОГО (явно помечено как ПРИБЛИЖЕНИЕ, не измерение):
интервалы прихода МОДЕЛИРУЮТСЯ как равномерные по known duration_s --
"scenario", не "measured".

ЧЕСТНО О ПРЕДСТАВИТЕЛЬНОСТИ: этот захват -- ВЕСЬ трафик роутеров чейна
(общий call traffic из sequencer feed), НЕ отфильтрован по
PoolManager.Swap на ОТСЛЕЖИВАЕМЫХ пулах (реальный триггер горячего
пути). Реальная частота сигналов бота (Swap на конкретных pool_id из
живого RouteRegistry) НА ПОРЯДКИ реже общего трафика роутеров. Поэтому
результат ниже -- ВЕРХНЯЯ ГРАНИЦА/стресс-сценарий общей пропускной
способности инфраструктуры приёма, НЕ оценка реальной частоты сигналов
бота -- нельзя путать одно с другим."""
from __future__ import annotations

import argparse
import gzip
import json
import statistics
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
DUMP_PATH = REPO_ROOT / "data" / "task5_bot_router_capture" / "dump.jsonl.gz"
SUMMARY_PATH = REPO_ROOT / "data" / "task5_bot_router_capture_summary.json"


def _pctl(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)


def load_capture() -> tuple[dict, list[dict]]:
    summary = json.loads(SUMMARY_PATH.read_text())
    records = []
    with gzip.open(DUMP_PATH, "rt") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return summary, records


def analyze_arrival_structure(summary: dict, records: list[dict]) -> dict:
    by_seq: dict[int, int] = {}
    for r in records:
        seq = r.get("sequence_number")
        if seq is not None:
            by_seq[seq] = by_seq.get(seq, 0) + 1
    seqs_sorted = sorted(by_seq)
    tx_per_seq = [by_seq[s] for s in seqs_sorted]
    return {
        "capture_duration_s": summary.get("duration_s"),
        "n_ws_frames": summary.get("n_messages"),
        "n_tx_decoded_total": summary.get("n_tx_decoded"),
        "n_distinct_sequence_numbers": len(seqs_sorted),
        "tx_per_sequence_number": {
            "min": min(tx_per_seq) if tx_per_seq else None,
            "median": statistics.median(tx_per_seq) if tx_per_seq else None,
            "max": max(tx_per_seq) if tx_per_seq else None,
        },
        "missing_fields_for_true_arrival_replay": [
            "per-record/per-frame monotonic or wall-clock arrival timestamp",
            "which records correspond to a PoolManager.Swap on a TRACKED pool_id "
            "(this capture is unfiltered general router traffic, not the bot's real trigger)",
        ],
        "representativeness_caveat": (
            "Этот захват -- ВЕСЬ трафик роутеров, не отфильтрованный по реальному триггеру "
            "бота (Swap на отслеживаемых pool_id). Частота ниже -- верхняя граница общей "
            "инфраструктуры приёма, НЕ оценка реальной частоты сигналов бота."
        ),
    }


def simulate_queue_scenario(structure: dict, calc_duration_samples_s: list[float] | None,
                             assumed_arrival_model: str = "uniform_by_sequence_number") -> dict:
    """СЦЕНАРИЙ (не измерение реального продакшена): равномерное
    распределение прихода sequence_number-групп по всей длительности
    захвата (единственное честно доступное приближение при отсутствии
    per-message timestamp), + РЕАЛЬНЫЕ (если переданы) медиана/p95
    calc_duration_s из фактического прогона task5_v4_speed_benchmark.py
    на Ohio -- иначе явно помечено как "нет данных"."""
    duration_s = structure["capture_duration_s"]
    n_groups = structure["n_distinct_sequence_numbers"]
    if not duration_s or not n_groups:
        return {"error": "нет duration_s/n_distinct_sequence_numbers в структуре захвата"}
    mean_inter_arrival_s = duration_s / n_groups

    result = {
        "assumed_arrival_model": assumed_arrival_model,
        "assumption_caveat": "Равномерное распределение -- ПРЕДПОЛОЖЕНИЕ (нет per-message "
                              "timestamp в захвате), не измеренный реальный интервал.",
        "mean_inter_arrival_s_between_sequence_groups": mean_inter_arrival_s,
    }
    if not calc_duration_samples_s:
        result["queue_utilization"] = None
        result["note"] = ("Нет реальных calc_duration_s (передайте --calc-duration-samples-json "
                           "с результатом task5_v4_speed_benchmark.py) -- утилизация очереди не "
                           "вычислена, чтобы не подставлять выдуманное значение времени обслуживания.")
        return result

    median_calc_s = statistics.median(calc_duration_samples_s)
    p95_calc_s = _pctl(calc_duration_samples_s, 0.95)
    utilization_median = median_calc_s / mean_inter_arrival_s if mean_inter_arrival_s else None
    utilization_p95 = p95_calc_s / mean_inter_arrival_s if mean_inter_arrival_s else None
    result.update({
        "measured_calc_duration_median_s": median_calc_s,
        "measured_calc_duration_p95_s": p95_calc_s,
        "queue_utilization_median": utilization_median,
        "queue_utilization_p95": utilization_p95,
        "interpretation": (
            "utilization > 1 означает: под ЭТИМ (стрессовым, верхняя граница) сценарием прихода "
            "однопоточный evaluator НЕ успевал бы обрабатывать кандидатов по мере поступления -- "
            "очередь росла бы. utilization < 1 -- успевал бы В СРЕДНЕМ (не гарантия для пиков)."
            if utilization_median is not None else ""
        ),
    })
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--calc-duration-samples-json", type=str, default=None,
                     help="Путь к result_*.json от task5_v4_speed_benchmark.py -- реальные "
                          "calc_duration-подобные измерения (route_search_and_sizing + "
                          "simulate_and_gas_estimate суммарно) для честной утилизации очереди.")
    args = ap.parse_args()

    summary, records = load_capture()
    structure = analyze_arrival_structure(summary, records)

    calc_samples: list[float] | None = None
    if args.calc_duration_samples_json:
        data = json.loads(Path(args.calc_duration_samples_json).read_text())
        calc_samples = []
        for variant_block in data.get("direct_probes", {}).values():
            for probe in variant_block.get("raw", []):
                total = sum(e["wall_s"] for e in probe.get("trace", []))
                if total > 0:
                    calc_samples.append(total)

    scenario = simulate_queue_scenario(structure, calc_samples)

    result = {"capture_structure": structure, "queue_scenario": scenario}
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    out_path = REPO_ROOT / "data" / "task5_v4_speed_benchmark_replay_result.json"
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[replay] записано: {out_path}")


if __name__ == "__main__":
    main()
