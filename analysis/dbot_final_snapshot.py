#!/usr/bin/env python3
"""Финальный снимок фазы DBot ДО любых удалений. ТОЛЬКО ЧТЕНИЕ.

Что кладётся в data/final/<метка времени>/:
  * konfig.json      -- полная конфигурация всех задач (тем же скриптом
                        только для чтения, что и раньше: единственный
                        сетевой вызов -- GET списка задач);
  * follow_trades.json -- ВСЕ записи follow по всем задачам за всё время,
                        постранично до пустой страницы;
  * balances.json    -- нативный SOL девяти кошельков задач;
  * MANIFEST.json    -- что откуда взято, когда и чем, плюс контрольные
                        суммы файлов.

ГРАНИЦЫ: только GET. POST/PUT/PATCH/DELETE в этом скрипте не вызываются,
и самопроверка доказывает это по исходному тексту. Ничего не удаляется и
не меняется ни в задачах, ни на кошельках.

Ключ API нигде не печатается.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

FINAL_DIR = REPO_ROOT / "data" / "final"

WALLET_FALLBACK = {
    "BATCH-8": "4s87RRC2V2XAJD6R8U2dP8kQH99Z2wA6fg88ZVfV4j4N",
    "BATCH-7": "CqoAZUaTHxVYgTuDz5AoEZU4VYmfFPDEmTTcJAJoAWER",
    "BATCH-6": "DE5yR9S8qBkh8n4rYVvc6iGDF3rqrEPZX2aHBsDWJ79n",
    "BATCH-5": "5Y8h877swoTzTdc8in9hU3SvXXVv1q9p19Y85tAsdqBv",
    "BATCH-4": "EjeXrxabRKmwLxvfQdXYN3oD3d3uWe2fuqqA5Qda2p8N",
    "BATCH-3": "BmjAUDbwBMxR5shrmzBtKRwveVahFGFiEH3oTq7QTHnu",
    "BATCH-2": "HMQG8xXoVBWZTkEqNye5WcfttvdZwVb522EggFD5AWZ6",
    "BATCH-1": "GYPzYfSP3htyfRCti5Wp6XTnUQh7zkwqTv6j7r4kUFrq",
    "pointfarmcap": "E1qAJBmrJDhBvm2sV8kfMXFAmgzHuSRNRosPEgMkKiWS",
}


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def write(path: Path, data) -> dict:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return {"файл": path.name, "байт": path.stat().st_size, "sha256": sha256(path)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-interval-s", type=float, default=0.05)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return

    метка = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = FINAL_DIR / метка
    out.mkdir(parents=True, exist_ok=True)
    манифест = {"метка_utc": метка, "чем_собрано": "analysis/dbot_final_snapshot.py",
                 "границы": ["только HTTP GET по DBot и чтение цепочки",
                              "ни одна задача не изменена и не удалена",
                              "ни одной продажи и ни одного перевода"],
                 "файлы": [], "проблемы": []}

    api_key = os.environ.get("DBOT_API_KEY", "")
    if not api_key:
        манифест["проблемы"].append("DBOT_API_KEY пуст -- разделы DBot не собраны")

    # 1. Полная конфигурация задач -- тем же скриптом только для чтения.
    if api_key:
        from dbot_task_config_readonly import read_tasks  # noqa: PLC0415
        status, body = read_tasks(api_key)
        манифест["файлы"].append(write(out / "konfig.json",
                                        {"код_ответа": status, "метод": "GET",
                                         "путь": "/automation/follow_orders", "тело": body}))
        print(f"[снимок] конфигурация задач: HTTP {status}", flush=True)

    # 2. Все записи follow по всем задачам за всё время.
    if api_key:
        from solana_ledger_run import (  # noqa: PLC0415
            fetch_follow_orders, fetch_follow_trades_for_task)
        tasks, http = fetch_follow_orders(api_key)
        все = {}
        неполные = []
        for t in tasks:
            tid = t.get("id")
            if not tid:
                continue
            records, complete = fetch_follow_trades_for_task(tid, api_key,
                                                              my_wallet=t.get("wallet"))
            все[tid] = {"задача": t.get("name"), "кошелёк": t.get("wallet"),
                         "записей": len(records), "выкачано_полностью": complete,
                         "записи": records}
            if not complete:
                неполные.append(t.get("name"))
            print(f"[снимок] follow {t.get('name')}: {len(records)} записей, "
                  f"полностью={complete}", flush=True)
        if неполные:
            манифест["проблемы"].append(
                f"follow выкачан НЕ полностью по задачам: {', '.join(map(str, неполные))}")
        манифест["файлы"].append(write(out / "follow_trades.json", {
            "код_ответа_списка_задач": http,
            "всего_записей": sum(v["записей"] for v in все.values()),
            "по_задачам": все}))

    # 3. Балансы девяти кошельков задач.
    кошельки = dict(WALLET_FALLBACK)
    балансы = {"собрано_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "кошельки": {}}
    try:
        from solana_crowd_scan import Rpc, helius_key  # noqa: PLC0415
        key, _ = helius_key()
        rpc = Rpc(key, min_interval_s=args.min_interval_s, workers=1,
                   service="разбор_пилота")
        for имя, адрес in кошельки.items():
            try:
                r = rpc.call("getBalance", [адрес])
                балансы["кошельки"][имя] = {"адрес": адрес,
                                             "sol": ((r or {}).get("value") or 0) / 1e9}
            except RuntimeError as exc:
                балансы["кошельки"][имя] = {"адрес": адрес, "sol": None,
                                             "почему": str(exc)[:160]}
        балансы["итого_sol"] = round(
            sum(v["sol"] or 0.0 for v in балансы["кошельки"].values()), 9)
    except (RuntimeError, ImportError) as exc:
        манифест["проблемы"].append(f"балансы не собраны: {str(exc)[:160]}")
    манифест["файлы"].append(write(out / "balances.json", балансы))

    # 4. Уже собранные выгрузки фазы -- копией, чтобы снимок был самодостаточным.
    for имя in ("solana_wallet_inventory.json", "solana_tax_groups.json",
                 "solana_transfer_fee_audit.json", "solana_tax_robustness.json",
                 "solana_trades_all.json", "ledger_status.json"):
        src = REPO_ROOT / "data" / имя
        if src.exists():
            (out / имя).write_bytes(src.read_bytes())
            манифест["файлы"].append({"файл": имя, "байт": (out / имя).stat().st_size,
                                       "sha256": sha256(out / имя),
                                       "откуда": f"data/{имя}"})
        else:
            манифест["проблемы"].append(f"нет data/{имя} -- в снимок не попал")

    (out / "MANIFEST.json").write_text(json.dumps(манифест, ensure_ascii=False, indent=2))
    print(json.dumps({"каталог": str(out.relative_to(REPO_ROOT)),
                       "файлов": len(манифест["файлы"]),
                       "проблемы": манифест["проблемы"]}, ensure_ascii=False, indent=2))


def self_test() -> None:
    checks = []

    def chk(n, ok, got=""):
        checks.append((n, bool(ok), got))

    src = Path(__file__).read_text()
    тело = src.split("def self_test")[0]
    for м in ("post", "put", "patch", "delete"):
        chk(f"нет requests.{м} в рабочей части",
            f"requests.{м}" not in тело.lower())
    chk("прямых вызовов requests тут нет вообще -- чтение идёт чужими "
        "проверенными функциями", "requests." not in тело)
    chk("удаления файлов нет", "unlink(" not in тело and "rmtree" not in тело)
    chk("метка времени в UTC", "gmtime()" in тело)
    chk("манифест пишется", "MANIFEST.json" in тело)

    import tempfile  # noqa: PLC0415
    d = Path(tempfile.mkdtemp())
    m = write(d / "x.json", {"а": 1})
    chk("файл записан и посчитан", m["байт"] > 0 and len(m["sha256"]) == 64, str(m))
    chk("контрольная сумма повторяема", sha256(d / "x.json") == m["sha256"])

    chk("кошельков девять", len(WALLET_FALLBACK) == 9)
    chk("адреса не повторяются", len(set(WALLET_FALLBACK.values())) == 9)
    chk("пилот на месте", WALLET_FALLBACK["pointfarmcap"].startswith("E1qAJBmr"))
    assert re and json

    bad = 0
    for n, ok, got in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {n}" + (f"  -> {got}" if got and not ok else ""))
        bad += (not ok)
    print(f"самопроверка финального снимка: {len(checks) - bad}/{len(checks)} пройдено")
    if bad:
        raise SystemExit(f"самопроверка не пройдена: {bad} из {len(checks)}")


if __name__ == "__main__":
    main()
