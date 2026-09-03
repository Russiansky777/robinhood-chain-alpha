#!/usr/bin/env python3
"""P5, продолжение разведки (владелец, 2026-09-03: "P5 — продолжать как
приоритет №1"). Из 4 вариантов, предложенных после mm_p5_setup.py,
владелец не выбрал явно один -- начинаем с рекомендованного (вариант 3):
2 дешёвые проверки, которые могут заметно удешевить основной 30-дневный
бэктест, ДО того как тратить время на варианты 1/2 (сузить окно /
разбить на несколько 90-минутных прогонов).

1. DefiLlama -- детальный дневной ряд объёма для конкретно "Robinhood
   Chain" (не просто факт присутствия в списке чейнов, как в
   mm_p5_setup.py) -- /overview/dexs/<chain>, даёт ли totalDataChart
   (историю) и разбивку по протоколу/пулу.
2. Архивный eth_call -- поддерживает ли публичный RPC чтение состояния
   пула НА ПРОШЛОМ блоке (не "latest"). Пробуем на ~3 часа назад и на
   ~2.4 дня назад (по калибровке mm_p5_setup.py: ~9.75 блоков/сек).
   Если работает -- почасовая выборка цены на 30 дней стоит ~720
   вызовов вместо полного лог-скана свопов.

Только чтение, ключ не используется, транзакций нет. Оценка: 2 HTTP +
~8 eth_call (2 блока x [liquidity(), slot0()] x 2 попытки) -- секунды.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests  # noqa: E402
from eth_abi import decode as abi_decode  # noqa: E402

from alchemy_fallback import _rpc_call, get_block_number, topic0  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/mm_p5_variant3_probe_result.json")
P5_POOL = "0x52e65B17fB6E5BA00Ed806f37Afcd2DaA50271Ca".lower()
CHAIN_NAME = "Robinhood Chain"  # дословно из allChains, data/p3_guard_cache/mm_p5_setup_result.json

_request_count = 0


def _count(n: int = 1) -> None:
    global _request_count
    _request_count += n


def _selector(sig: str) -> str:
    return topic0(sig)[:10]


def probe_defillama_detail() -> dict:
    url = f"https://api.llama.fi/overview/dexs/{urllib.parse.quote(CHAIN_NAME)}"
    try:
        r = requests.get(url, params={"excludeTotalDataChart": "false", "excludeTotalDataChartBreakdown": "false"},
                          timeout=20)
        out = {"url": url, "status": r.status_code, "reachable": True}
        if r.ok:
            body = r.json()
            chart = body.get("totalDataChart") or []
            out["has_total_data_chart"] = bool(chart)
            out["total_data_chart_points"] = len(chart)
            if chart:
                out["total_data_chart_sample_first"] = chart[0]
                out["total_data_chart_sample_last"] = chart[-1]
            out["protocols_n"] = len(body.get("protocols") or [])
            out["protocol_names"] = [p.get("name") for p in (body.get("protocols") or [])][:10]
            out["total24h"] = body.get("total24h")
            # per-protocol дневная разбивка -- если "Uniswap V3" на этом чейне
            # почти целиком состоит из пула P5, это даёт бесплатную дневную
            # историю ИМЕННО этого пула, не всей сети (owner, продолжение
            # разведки после первого запуска этого скрипта).
            breakdown = body.get("totalDataChartBreakdown") or []
            out["breakdown_points"] = len(breakdown)
            uni_v3_series = []
            if breakdown:
                for point in breakdown:
                    ts, by_protocol = point[0], point[1]
                    v = by_protocol.get("Uniswap V3")
                    if v is not None:
                        # значение может быть числом или {chain: значение} -- проверяем обе формы
                        val = v if isinstance(v, (int, float)) else sum(v.values()) if isinstance(v, dict) else None
                        uni_v3_series.append([ts, val])
                out["uniswap_v3_daily_series_points"] = len(uni_v3_series)
                out["uniswap_v3_daily_series_last5"] = uni_v3_series[-5:]
                out["breakdown_last_point_raw"] = breakdown[-1]
        else:
            out["body_snippet"] = r.text[:500]
        print(f"[mm_p5_variant3_probe] DefiLlama detail {url}: status={r.status_code}, "
              f"has_chart={out.get('has_total_data_chart')}")
        return out
    except Exception as e:  # noqa: BLE001
        print(f"[mm_p5_variant3_probe] DefiLlama detail недоступен: {e}")
        return {"url": url, "reachable": False, "error": str(e)}


def _eth_call_at_block(to: str, data: str, block_tag: str) -> str | None:
    _count()
    try:
        return _rpc_call("eth_call", [{"to": to, "data": data}, block_tag])
    except Exception as e:  # noqa: BLE001
        print(f"[mm_p5_variant3_probe]   eth_call@{block_tag} {to} {data[:10]} не удался: {e}")
        return None


def probe_archive_eth_call() -> dict:
    _count()
    latest = get_block_number()
    # Калибровка mm_p5_setup.py: ~9.75 блоков/сек (5000 блоков / 513с)
    blocks_per_sec = 5000 / 513
    targets = {
        "~3h_ago": latest - int(3 * 3600 * blocks_per_sec),
        "~2.4d_ago": latest - int(2.4 * 86400 * blocks_per_sec),
    }
    out = {"latest_block": latest, "blocks_per_sec_calibration": blocks_per_sec, "targets": {}}
    for label, block_num in targets.items():
        block_num = max(block_num, 1)
        tag = hex(block_num)
        entry = {"block_number": block_num, "block_tag": tag}
        liq = _eth_call_at_block(P5_POOL, _selector("liquidity()"), tag)
        slot0 = _eth_call_at_block(P5_POOL, _selector("slot0()"), tag)
        entry["liquidity_call_ok"] = bool(liq)
        entry["slot0_call_ok"] = bool(slot0)
        if liq:
            try:
                (liquidity_int,) = abi_decode(["uint128"], bytes.fromhex(liq[2:]))
                entry["liquidity_raw"] = liquidity_int
            except Exception as e:  # noqa: BLE001
                entry["liquidity_decode_error"] = str(e)
        if slot0:
            try:
                decoded = abi_decode(["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"],
                                      bytes.fromhex(slot0[2:]))
                entry["sqrt_price_x96"] = decoded[0]
            except Exception as e:  # noqa: BLE001
                entry["slot0_decode_error"] = str(e)
        out["targets"][label] = entry
        print(f"[mm_p5_variant3_probe] archive eth_call @ {label} (block {block_num}): "
              f"liquidity_ok={entry['liquidity_call_ok']} slot0_ok={entry['slot0_call_ok']}")
    out["archive_eth_call_supported"] = all(
        t["liquidity_call_ok"] and t["slot0_call_ok"] for t in out["targets"].values()
    )
    return out


def run() -> int:
    t0 = time.time()
    defillama = probe_defillama_detail()
    archive = probe_archive_eth_call()

    result = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "defillama_detail": defillama,
        "archive_eth_call": archive,
        "conclusion": {
            "defillama_has_historical_volume": bool(defillama.get("has_total_data_chart")),
            "archive_eth_call_works": archive.get("archive_eth_call_supported", False),
        },
        "requests_used": _request_count,
        "runtime_s": time.time() - t0,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"\n[mm_p5_variant3_probe] записано {OUT_PATH}, {_request_count} запросов, {time.time()-t0:.0f}с")
    print(f"[mm_p5_variant3_probe] ВЫВОД: DefiLlama даёт историю объёма = "
          f"{result['conclusion']['defillama_has_historical_volume']}; "
          f"архивный eth_call работает = {result['conclusion']['archive_eth_call_works']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
