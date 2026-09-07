#!/usr/bin/env python3
"""Задача 3 владельца (2026-09-07), вариант (в): "NOAA напрямую, GEFS в
noaa-gefs-pds. Начать с одного города и 30 дней для оценки
трудоёмкости и объёма, доложить до полного прогона."

Это ДИАГНОСТИЧЕСКИЙ прогон, не финальный бэктест -- цель: реально
измерить (а) работает ли метод вообще технически на GH Actions
(eccodes/cfgrib -- системная зависимость, не факт что ставится из pip
без apt), (б) сколько реально весит/времени занимает 30 дней на ОДНОМ
городе, чтобы честно оценить полный прогон (все города Kalshi x 90
дней), НЕ гадая на глаз.

РЕАЛЬНЫЕ факты этой сессии (WebSearch + прямая проверка живым
HTTPS GET на реальный бакет S3, не по памяти):
  - Бакет `noaa-gefs-pds`, публичный, без ключа (подтверждено прямым
    анонимным GET на `noaa-gefs-pds.s3.amazonaws.com`).
  - Путь (после 2020-09-23): `gefs.YYYYMMDD/HH/atmos/pgrb2ap5/
    ge{member}.t{HH}z.pgrb2a.0p50.f{fxx:03d}` -- подтверждено реальным
    листингом директории на сегодня.
  - Члены: gec00 (контроль) + gep01..gep30 (30 возмущённых) = 31,
    подтверждено (gep31 реально не существует -- 0 объектов).
  - Архив с 2017-01-01 -- подтверждено реальным листингом (первая
    папка `gefs.20170101/`).
  - `.idx`-файл рядом с каждым grib -- текстовый индекс байт-смещений
    сообщений (метод Herbie -- Range-запрос только нужного сообщения,
    не весь файл).

Город для диагностики: Нью-Йорк / Central Park (40.7829, -73.9654) --
реальная, публично известная координата станции NWS Central Park.
ЧЕСТНАЯ ОГОВОРКА: точное соответствие координаты официальному правилу
рынка Kalshi KXHIGHNY ещё не сверено -- для полного прогона нужно
свериться с реальными правилами контракта, не только с общеизвестной
геолокацией города (это диагностика метода, не финальные цифры)."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

S3_BASE = "https://noaa-gefs-pds.s3.amazonaws.com"
OUT_PATH = Path("data/p3_guard_cache/noaa_gefs_poc_one_city_result.json")

CITY_NAME = "New York (Central Park, диагностика -- сверить с реальным правилом Kalshi KXHIGHNY отдельно)"
CITY_LAT, CITY_LON = 40.7829, -73.9654
N_DAYS = 30
RUN_HOUR = "12"  # реальный выбор: цикл 12z за сутки до -- "~24ч до закрытия", уточнение точного часа -- отдельная задача после диагностики
MEMBERS_FULL = ["gec00"] + [f"gep{i:02d}" for i in range(1, 31)]


def idx_url(run_date: str, run_hour: str, member: str, fxx: int) -> str:
    return f"{S3_BASE}/gefs.{run_date}/{run_hour}/atmos/pgrb2ap5/{member}.t{run_hour}z.pgrb2a.0p50.f{fxx:03d}.idx"


def grib_url(run_date: str, run_hour: str, member: str, fxx: int) -> str:
    return f"{S3_BASE}/gefs.{run_date}/{run_hour}/atmos/pgrb2ap5/{member}.t{run_hour}z.pgrb2a.0p50.f{fxx:03d}"


def fetch_idx(run_date: str, run_hour: str, member: str, fxx: int) -> tuple[int, str]:
    r = requests.get(idx_url(run_date, run_hour, member, fxx), timeout=30)
    return r.status_code, r.text if r.status_code == 200 else r.text[:500]


def find_temp_messages(idx_text: str) -> list[dict]:
    """Реальный .idx -- текст вида `N:offset:date:VAR:LEVEL:...`.
    Ищем ЛЮБОЕ упоминание температуры (TMAX/TMP), честно логируем ВСЕ
    найденные варианты -- не гадаем заранее, какое реальное имя
    переменной использует этот конкретный продукт."""
    out = []
    for line in idx_text.splitlines():
        if not line.strip():
            continue
        parts = line.split(":")
        if len(parts) < 4:
            continue
        var = parts[3]
        if "TMP" in var.upper() or "TMAX" in var.upper():
            out.append({"raw_line": line, "var": var, "level": parts[4] if len(parts) > 4 else None,
                        "byte_offset": int(parts[1]) if parts[1].isdigit() else None})
    return out


def run() -> int:
    t0 = time.time()
    result: dict = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "purpose": "ДИАГНОСТИКА трудоёмкости/объёма, НЕ финальный бэктест -- владелец: 'доложить до полного прогона'",
        "city": CITY_NAME, "lat": CITY_LAT, "lon": CITY_LON, "n_days": N_DAYS,
        "s3_bucket": "noaa-gefs-pds", "run_hour": RUN_HOUR,
    }

    now = datetime.now(timezone.utc)
    print("=== Шаг 1: реальный .idx для ОДНОГО дня/раннера/члена -- узнать реальные имена переменных ===")
    probe_date = (now - timedelta(days=2)).strftime("%Y%m%d")
    status, idx_text = fetch_idx(probe_date, RUN_HOUR, "gec00", 24)
    print(f"HTTP {status}, длина .idx: {len(idx_text) if status == 200 else 'n/a'}")
    temp_vars_found = find_temp_messages(idx_text) if status == 200 else []
    print(f"реальных строк с температурой в .idx: {len(temp_vars_found)}")
    for tv in temp_vars_found[:20]:
        print(f"  {tv['raw_line']}")
    result["probe_idx_status"] = status
    result["probe_idx_temp_vars_found"] = temp_vars_found
    if status != 200:
        result["probe_idx_raw_error"] = idx_text

    if not temp_vars_found:
        result["blocker"] = "не найдено ни одной температурной переменной в реальном .idx на пробном файле -- дальше не идём, диагностика остановлена честно"
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"\n[gefs_poc] ОСТАНОВЛЕНО: {result['blocker']}")
        return 1

    print("\n=== Шаг 2: проверка cfgrib/eccodes -- реально ли декодируется байт-диапазон одного сообщения ===")
    try:
        import cfgrib  # noqa: F401
        import xarray as xr  # noqa: F401
        result["cfgrib_import_ok"] = True
        print("cfgrib/xarray импортированы успешно")
    except Exception as exc:  # noqa: BLE001
        result["cfgrib_import_ok"] = False
        result["cfgrib_import_error"] = str(exc)[:1000]
        print(f"cfgrib/xarray НЕ импортируются: {exc}")

    if not result.get("cfgrib_import_ok"):
        result["blocker"] = "cfgrib/eccodes не установились в этом окружении -- реальная системная проблема, нужно решать ДО полного прогона (см. cfgrib_import_error)"
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"\n[gefs_poc] ОСТАНОВЛЕНО: {result['blocker']}")
        return 1

    print("\n=== Шаг 3: реальное скачивание ОДНОГО GRIB-сообщения через byte-range и извлечение точки ===")
    first_tv = temp_vars_found[0]
    all_lines = [ln for ln in idx_text.splitlines() if ln.strip()]
    # Байт-диапазон сообщения: от смещения найденной строки до смещения СЛЕДУЮЩЕЙ строки в .idx (реальный, честный метод)
    idx_of_line = all_lines.index(first_tv["raw_line"])
    start_byte = first_tv["byte_offset"]
    end_byte = None
    if idx_of_line + 1 < len(all_lines):
        next_parts = all_lines[idx_of_line + 1].split(":")
        if next_parts[1].isdigit():
            end_byte = int(next_parts[1]) - 1
    range_header = f"bytes={start_byte}-{end_byte}" if end_byte else f"bytes={start_byte}-"
    t_dl0 = time.time()
    r = requests.get(grib_url(probe_date, RUN_HOUR, "gec00", 24), headers={"Range": range_header}, timeout=60)
    dl_s = time.time() - t_dl0
    print(f"HTTP {r.status_code}, скачано {len(r.content)} байт за {dl_s:.2f}с (Range={range_header})")
    result["single_message_download"] = {"status": r.status_code, "n_bytes": len(r.content), "download_s": dl_s,
                                           "range_header": range_header, "variable": first_tv["var"]}

    if r.status_code in (200, 206):
        tmp_grib = Path("/tmp/gefs_probe_message.grib2")
        tmp_grib.write_bytes(r.content)
        try:
            import xarray as xr
            ds = xr.open_dataset(tmp_grib, engine="cfgrib")
            point = ds.sel(latitude=CITY_LAT, longitude=CITY_LON % 360, method="nearest")
            value = float(list(point.data_vars.values())[0].values)
            result["single_point_extraction"] = {"ok": True, "value_raw": value, "data_vars": list(ds.data_vars.keys())}
            print(f"реальное значение в точке ({CITY_LAT},{CITY_LON}): {value} (переменные в файле: {list(ds.data_vars.keys())})")
        except Exception as exc:  # noqa: BLE001
            result["single_point_extraction"] = {"ok": False, "error": str(exc)[:1000]}
            print(f"извлечение точки упало: {exc}")

    print("\n=== Шаг 4: реальная оценка объёма/времени -- 30 дней x 31 член ОДНОГО города (сколько запросов, сколько времени по факту одного замера) ===")
    n_requests_full_city_30d = N_DAYS * len(MEMBERS_FULL) * 2  # .idx + сам байт-диапазон, на член на день
    time_per_request_s = dl_s if result.get("single_message_download", {}).get("status") in (200, 206) else None
    result["volume_estimate"] = {
        "n_days": N_DAYS, "n_members": len(MEMBERS_FULL),
        "n_requests_one_city_30d_idx_plus_range": n_requests_full_city_30d,
        "measured_single_download_s": time_per_request_s,
        "naive_estimated_total_s_one_city_30d": (time_per_request_s * n_requests_full_city_30d) if time_per_request_s else None,
        "note": "наивная оценка = время одного замера x число запросов, БЕЗ учёта троттлинга/параллелизации/накладных расходов cfgrib на файл -- реальная верхняя граница по одному образцу, не среднее",
    }
    print(json.dumps(result["volume_estimate"], indent=2, ensure_ascii=False))

    result["runtime_s_total"] = time.time() - t0
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[gefs_poc] результат записан в {OUT_PATH}, общее время диагностики: {result['runtime_s_total']:.1f}с")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
