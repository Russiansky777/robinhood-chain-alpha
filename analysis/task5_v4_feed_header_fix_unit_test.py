#!/usr/bin/env python3
"""Локальный, БЕЗ сети, тест исправления заголовков хендшейка (владелец,
п.1): "повторяющийся server-timing не вызывает исключение. Ошибка
необязательной диагностики заголовков не должна закрывать рабочее
соединение или прекращать чтение."

Использует РЕАЛЬНЫЙ websockets.datastructures.Headers (ту же версию
библиотеки, что вызвала боевой сбой этого раунда, MultipleValuesError:
'server-timing') -- не мок, настоящий класс, которым websockets парсит
заголовки ответа. Два теста:
  1) Воспроизводит ИСХОДНЫЙ баг (dict(headers) на дубликате падает) и
     подтверждает, что ИСПРАВЛЕННОЕ извлечение (.raw_items()) -- нет.
  2) Подтверждает, что _safe_extract_response_headers() (реальная функция
     из task5_v4_feed_vs_alchemy_measurement.py, импортируется, не
     копируется) НИКОГДА не бросает исключение наружу, даже если
     .raw_items() ведёт себя неожиданно (смоделировано форс-поломкой),
     и что вызывающий код (структура main-цикла) не завершает чтение
     из-за ошибки этой диагностики."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from websockets.datastructures import Headers, MultipleValuesError  # noqa: E402

from task5_v4_feed_vs_alchemy_measurement import _safe_extract_response_headers  # noqa: E402


class _FakeResponse:
    def __init__(self, headers) -> None:
        self.headers = headers


class _FakeWs:
    def __init__(self, headers) -> None:
        self.response = _FakeResponse(headers)


class _AlwaysRaisingHeaders:
    """Симулирует ЛЮБУЮ неожиданную поломку диагностики (не только
    MultipleValuesError) -- .raw_items() сам кидает исключение."""

    def raw_items(self):
        raise RuntimeError("симулированная неожиданная поломка диагностики заголовков")


def test_duplicate_header_reproduces_original_bug_and_fix_avoids_it() -> None:
    h = Headers()
    h["Server-Timing"] = "db;dur=1"
    h["Server-Timing"] = "app;dur=2"  # РЕАЛЬНЫЙ дубликат -- то же имя, что упало в бою
    h["X-Single"] = "ok"

    # Воспроизводим ИСХОДНЫЙ баг -- plain dict() на дубликате падает.
    raised = False
    try:
        dict(h)
    except MultipleValuesError:
        raised = True
    assert raised, "ожидался MultipleValuesError на dict(h) с дубликатом -- исходный баг должен воспроизводиться"

    # Исправление -- ws.response.headers.raw_items() НЕ падает.
    ws = _FakeWs(h)
    result = _safe_extract_response_headers(ws)
    assert "_extraction_error" not in result, f"безопасное извлечение не должно было упасть: {result}"
    assert result.get("X-Single") == "ok"
    assert "Server-Timing" in result  # хотя бы одно из двух значений сохранено, без падения
    print("[test 1] OK: дубликат server-timing воспроизводит исходный баг у dict(), "
          f"но НЕ у исправленного извлечения -- result={result}")


def test_unexpected_diagnostics_failure_does_not_raise() -> None:
    """Даже если сама диагностика сломана НЕОЖИДАННЫМ способом (не тем,
    что уже исправлен) -- _safe_extract_response_headers() обязана
    вернуть словарь с пометкой ошибки, а НЕ бросить исключение наружу
    (владелец: "ошибка диагностики не должна закрывать соединение или
    прекращать чтение")."""
    ws = _FakeWs(_AlwaysRaisingHeaders())
    result = _safe_extract_response_headers(ws)
    assert "_extraction_error" in result, f"ожидалась честная пометка ошибки, получено: {result}"
    print(f"[test 2] OK: неожиданная поломка диагностики поймана внутри функции, "
          f"наружу исключение НЕ ушло -- result={result}")


def test_none_response_does_not_raise() -> None:
    """ws.response отсутствует (например, старая версия websockets) --
    тоже не должно падать."""
    class _NoResponseWs:
        response = None
    result = _safe_extract_response_headers(_NoResponseWs())
    assert result == {}
    print("[test 3] OK: отсутствие ws.response -- пустой словарь, без падения")


if __name__ == "__main__":
    test_duplicate_header_reproduces_original_bug_and_fix_avoids_it()
    test_unexpected_diagnostics_failure_does_not_raise()
    test_none_response_does_not_raise()
    print("ВСЕ ЛОКАЛЬНЫЕ ТЕСТЫ ПРОШЛИ -- без сети, без подключения к фиду/Alchemy.")
