#!/usr/bin/env python3
"""Задача 5, замер эффекта "быстрого RPC-пути" (владелец, "измерь эффект
на нескольких сохранённых маршрутах"). ЧИСТО ЧИТАЮЩИЙ -- только eth_call
(котировки), ничего не пишет и не отправляет, новых LIVE/широких сканов
не запускает.

Берёт 3 РЕАЛЬНЫХ маршрута из сохранённого реестра пилота
(task5_v4_route_registry_state.json, тот же файл, что читает
bootstrap_registry()):
  - С хуком (пул, где hooks != NATIVE);
  - Без хука (все плечи hooks == NATIVE);
  - С реально наблюдавшейся повторяющейся ошибкой (route_id из
    сохранённого session-only no_send_log.jsonl этой сессии, где
    _classify_quote_error() дал НЕопознанный внутренний селектор --
    см. task5_v4_pilot_session_stats.py/round этого разбора).

Для КАЖДОГО -- ДВА замера НА ОДНОМ И ТОМ ЖЕ block_number (сначала
пробуем `signal_block` из сохранённой записи лога; если RPC отвечает
ошибкой, похожей на отсутствие исторического state -- честно
переключаемся на "latest" И ЯВНО отмечаем эту замену в результате, ОДИН
раз, общую для обоих путей этого маршрута, чтобы сравнение оставалось
на ОДНОМ состоянии):
  - "old": ПОСЛЕДОВАТЕЛЬНЫЕ quote_exact_input_single БЕЗ инъекции
    rpc_call (дефолтный _rpc_call -- публичный-RPC-первый, 0.5с
    троттлинг, тот путь, которым ДО этой правки шли ВСЕ hook-маршруты);
  - "new": ТО ЖЕ, но с rpc_call=rpc_call_trading_path (тот код,
    который теперь реально используется в quote_route_at_size).
Для hookless-маршрута "old" и "new" -- ОДИН И ТОТ ЖЕ код (уже был на
быстром пути ДО этой правки) -- измеряется дважды для полноты таблицы,
но расхождения не ожидается и это явно отмечено.

Инструментация (без изменения самого кода): монки-патч `requests.post`
(считает КАЖДЫЙ реальный HTTP POST + его wall-time) и `time.sleep`
(считает КАЖДЫЙ вызов + суммарное время сна -- это троттлинг-паузы
_throttle()/rpc_call_trading_path, НЕ имитация, реальный time.sleep
по-прежнему вызывается)."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

import requests  # noqa: E402

import alchemy_fallback as af  # noqa: E402
from alchemy_fallback import _rpc_call, rpc_call_trading_path  # noqa: E402
from task5_v4_pool_math import PoolKey  # noqa: E402
from task5_v4_quote_replay import quote_exact_input_single  # noqa: E402
from task5_v4_revert_decode import decode_v4_revert_detail  # noqa: E402

REGISTRY_STATE_FILE = Path("/home/bot/data/task5_v4_route_registry_state.json")
SESSION_LOG_FILE = Path("/home/bot/data/task5_v4_pilot_session_only_no_send_log.jsonl")


class _Instrumentation:
    """Счётчик РЕАЛЬНЫХ HTTP-запросов (requests.post) и РЕАЛЬНЫХ пауз
    (time.sleep) -- монки-патчит оба на время `with`, восстанавливает
    оригиналы на выходе. Не подменяет поведение -- оборачивает."""

    def __init__(self) -> None:
        self.n_http_calls = 0
        self.http_time_s = 0.0
        self.n_sleeps = 0
        self.sleep_time_s = 0.0
        self._orig_post = requests.post
        self._orig_sleep = time.sleep

    def __enter__(self) -> "_Instrumentation":
        def wrapped_post(*args, **kwargs):
            t0 = time.monotonic()
            try:
                return self._orig_post(*args, **kwargs)
            finally:
                self.n_http_calls += 1
                self.http_time_s += time.monotonic() - t0
        def wrapped_sleep(seconds):
            self.n_sleeps += 1
            self.sleep_time_s += seconds
            self._orig_sleep(seconds)
        requests.post = wrapped_post
        time.sleep = wrapped_sleep
        return self

    def __exit__(self, *exc) -> None:
        requests.post = self._orig_post
        time.sleep = self._orig_sleep

    def as_dict(self) -> dict:
        return {"n_http_calls": self.n_http_calls, "http_time_s": round(self.http_time_s, 4),
                "n_sleep_calls": self.n_sleeps, "sleep_time_s": round(self.sleep_time_s, 4)}


def _load_registry_routes() -> dict:
    return json.loads(REGISTRY_STATE_FILE.read_text())


def _pool_key_from_leg(leg: dict) -> PoolKey:
    return PoolKey(leg["currency0"], leg["currency1"], leg["fee"], leg["tick_spacing"], leg["hooks"])


def _pick_routes(registry: dict, session_log_rows: list[dict]) -> dict:
    """Возвращает {"hooked": (route_id, legs, size), "hookless": (...),
    "error_repeat": (...)} -- РЕАЛЬНЫЕ маршруты из сохранённого реестра,
    не выдуманные."""
    routes = registry.get("routes", {})
    picked: dict = {}

    # error_repeat -- маршрут из журнала сессии, где _classify_quote_error
    # дал НЕопознанный внутренний селектор (0x7c9c6e8f, см. round этого
    # разбора) -- используем ЕГО РЕАЛЬНЫЙ route_id и последний известный size_in_raw.
    for row in session_log_rows:
        if row.get("reason") != "ошибка расчёта":
            continue
        decoded = decode_v4_revert_detail(row.get("detail") or "")
        if decoded["recognized_outer"] and decoded["inner_selector"] and not decoded["size_independent"] \
                and decoded["inner_name"] is None:
            rid = row["route_id"]
            if rid in routes:
                picked["error_repeat"] = (rid, routes[rid].get("legs", []), row.get("size_in_raw") or 1_000_000)
                break

    for rid, r in routes.items():
        legs = r.get("legs", [])
        if not legs:
            continue
        has_hook = any(leg["hooks"].lower() != "0x0000000000000000000000000000000000000000" for leg in legs)
        if has_hook and "hooked" not in picked and rid != picked.get("error_repeat", (None,))[0]:
            picked["hooked"] = (rid, legs, 1_000_000)
        if not has_hook and "hookless" not in picked:
            picked["hookless"] = (rid, legs, 1_000_000)
        if len(picked) == 3:
            break
    return picked


def _quote_sequential(legs: list[dict], amount_in: int, block_number: int, rpc_call=None) -> dict:
    cur = amount_in
    kwargs = {} if rpc_call is None else {"rpc_call": rpc_call}
    for leg in legs:
        key = _pool_key_from_leg(leg)
        cur = quote_exact_input_single(key, leg["zero_for_one"], cur, block_number, **kwargs)
    return {"amount_out": cur}


def _measure(label: str, legs: list[dict], amount_in: int, block_number: int, rpc_call) -> dict:
    with _Instrumentation() as instr:
        t0 = time.monotonic()
        try:
            res = _quote_sequential(legs, amount_in, block_number, rpc_call=rpc_call)
            ok, amount_out, error = True, res["amount_out"], None
        except Exception as exc:  # noqa: BLE001
            ok, amount_out, error = False, None, str(exc)
        total_s = time.monotonic() - t0
    result = {"label": label, "ok": ok, "amount_out": amount_out, "error": error,
              "total_calc_duration_s": round(total_s, 4)}
    result.update(instr.as_dict())
    return result


def main() -> None:
    result: dict = {}
    registry = _load_registry_routes()
    session_rows = [json.loads(l) for l in SESSION_LOG_FILE.read_text().splitlines() if l.strip()] \
        if SESSION_LOG_FILE.exists() else []
    picked = _pick_routes(registry, session_rows)
    result["picked_routes"] = {k: {"route_id": v[0], "n_legs": len(v[1]), "amount_in": v[2]}
                                for k, v in picked.items()}

    latest = int(_rpc_call("eth_blockNumber", []), 16)
    result["latest_block_fallback"] = latest

    comparisons = []
    for kind, (route_id, legs, amount_in) in picked.items():
        # Пытаемся честно найти исторический signal_block из журнала сессии для ЭТОГО route_id.
        historical_block = None
        for row in session_rows:
            if row.get("route_id") == route_id and row.get("signal_block"):
                historical_block = row["signal_block"]
                break
        block_used = historical_block or latest
        substitution_note = None
        if historical_block is not None:
            # Пробный вызов -- если исторический блок недоступен (missing state),
            # честно переключаемся на latest ОДИН раз для ОБОИХ путей этого маршрута.
            try:
                _quote_sequential(legs[:1], amount_in, historical_block)
            except Exception as exc:  # noqa: BLE001
                decoded = decode_v4_revert_detail(str(exc))
                if not decoded["recognized_outer"] and ("missing" in str(exc).lower()
                                                          or "pruned" in str(exc).lower()
                                                          or "not found" in str(exc).lower()):
                    substitution_note = (f"исторический блок {historical_block} недоступен ({exc}) -- "
                                          f"ЗАМЕНЁН на latest={latest} для ОБОИХ путей этого маршрута")
                    block_used = latest

        old = _measure("old (default _rpc_call, публичный-RPC-первый)", legs, amount_in, block_used, rpc_call=None)
        new = _measure("new (rpc_call_trading_path, Alchemy напрямую)", legs, amount_in, block_used,
                        rpc_call=rpc_call_trading_path)
        quotes_match = (old["ok"] == new["ok"]) and (
            old["amount_out"] == new["amount_out"] if old["ok"] else
            decode_v4_revert_detail(old["error"] or "")["outer_selector"] ==
            decode_v4_revert_detail(new["error"] or "")["outer_selector"]
        )
        comparisons.append({
            "kind": kind, "route_id": route_id, "n_legs": len(legs), "amount_in": amount_in,
            "block_used": block_used, "historical_block_attempted": historical_block,
            "block_substitution_note": substitution_note,
            "old": old, "new": new, "quotes_or_errors_match": quotes_match,
        })
        print(json.dumps(comparisons[-1], indent=2, default=str, ensure_ascii=False))

    result["comparisons"] = comparisons
    result["ok"] = True
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
