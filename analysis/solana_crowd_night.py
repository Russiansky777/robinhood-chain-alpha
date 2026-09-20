#!/usr/bin/env python3
"""Владелец: п.2 больше не зависит от внешних пробуждений -- самоходный
процесс на GitHub Actions (cron */10 мин, concurrency-группа не даёт
запустить второй экземпляр, пока идёт первый). Каждый запуск читает
data/night_status.json и продолжает с места остановки, работает не
дольше TIME_BUDGET_S, затем сохраняет статус и выходит -- следующий
тик cron продолжит.

Стадии: control -> scan -> done|stopped. control -- не более
MAX_ATTEMPTS попыток всего (счётчик в файле); прошёл -> scan; не прошёл
после MAX_ATTEMPTS -> stopped с сырыми данными по одной покупке.
scan -- кошельки из data/solana_fomo_passed.json по одному, результаты
дописываются в data/solana_fomo_crowd.json по ходу (один процесс,
конкурентного доступа нет -- concurrency-группа гарантирует это)."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_crowd_common import (  # noqa: E402
    analyze_wallet, MAX_PURCHASES_PER_WALLET_CONTROL, MAX_PURCHASES_PER_WALLET_SCAN,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
STATUS_PATH = REPO_ROOT / "data" / "night_status.json"
CROWD_PATH = REPO_ROOT / "data" / "solana_fomo_crowd.json"
PASSED_PATH = REPO_ROOT / "data" / "solana_fomo_passed.json"

LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
BREZ = "Fvkc2thk1YcAASdR2gi8uf9n67JW9Dqqr9iRd99MDhoB"
CONTROL_SPEC = {
    LEADER_WALLET: {"name": "LEADER", "min_sol": 15.0, "expected_pct": 17.0},
    BREZ: {"name": "Brez", "min_sol": 2.0, "expected_pct": 11.0},
}
TIME_BUDGET_S = 20 * 60
MAX_ATTEMPTS = 2


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run_url() -> str | None:
    server, repo, rid = (os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{server}/{repo}/actions/runs/{rid}" if server and repo and rid else None


def load_status() -> dict:
    if STATUS_PATH.exists():
        try:
            return json.loads(STATUS_PATH.read_text())
        except (ValueError, OSError):
            pass
    return {"stage": "control", "attempts": 0, "control": {}, "done_wallets": 0,
            "total_wallets": 0, "updated_utc": None, "last_error": None, "run_url": None}


def save_status(status: dict) -> None:
    status["updated_utc"] = now_utc()
    status["run_url"] = run_url()
    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2, default=str))


def same_sign_same_order(got: float, expected: float) -> bool:
    if got == 0 or expected == 0 or (got > 0) != (expected > 0):
        return False
    ratio = abs(got) / abs(expected)
    return 0.3 <= ratio <= 3.0


FOLLOW_ORDERS_PATH = REPO_ROOT / "data" / "dbot_follow_orders_raw.json"
LIVE_TASK_NAMES = ("BATCH-3", "BATCH-5", "BATCH-6", "BATCH-7")


def load_live_task_sources() -> dict[str, str]:
    """Адрес источника -> имя задачи, только для живых задач BATCH-3/5/6/7
    (владелец, п.3: эти сканируются первыми). Файл обновляется отдельным
    почасовым конвейером учёта (ledger_hourly) в этой же рабочей ветке --
    здесь просто читается, без обращения к DBot API."""
    if not FOLLOW_ORDERS_PATH.exists():
        return {}
    try:
        data = json.loads(FOLLOW_ORDERS_PATH.read_text())
    except (ValueError, OSError):
        return {}
    out: dict[str, str] = {}
    for t in data.get("tasks", []):
        if t.get("name") not in LIVE_TASK_NAMES:
            continue
        for s in t.get("sources") or []:
            addr = s.get("address")
            if addr:
                out.setdefault(addr, t["name"])
    return out


def load_ranked_wallets() -> list[dict]:
    rows = []
    for line in PASSED_PATH.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    live_sources = load_live_task_sources()
    for r in rows:
        r["stands_in"] = live_sources.get(r["address"], "очередь")
    # Владелец, п.3: сначала кошельки, стоящие в живых задачах BATCH-3/5/6/7
    # (в порядке ранга Fomo внутри этой группы), затем остальные -- тоже
    # по рангу Fomo.
    rows.sort(key=lambda r: (0 if r["stands_in"] != "очередь" else 1, r.get("fomo_rank", 10**9)))
    return rows


def run_control(status: dict, deadline: float) -> None:
    ctrl = status.setdefault("control", {})
    ctrl.setdefault("results", {})
    for addr, spec in CONTROL_SPEC.items():
        if addr in ctrl["results"]:
            continue
        if time.monotonic() > deadline:
            print("[crowd_night] контроль: бюджет исчерпан, продолжу со следующего тика", flush=True)
            return
        print(f"[crowd_night] контроль: считаю {spec['name']} ({addr[:10]}..)...", flush=True)
        r = analyze_wallet(addr, spec["name"], min_sol=spec["min_sol"],
                            max_purchases=MAX_PURCHASES_PER_WALLET_CONTROL, deadline=deadline)
        if r.get("wallet_budget_cut"):
            # Бюджет кончился НА этом кошельке -- неполный прогон не
            # считается попыткой (MAX_ATTEMPTS), продолжаем с него же
            # на следующем тике.
            print(f"[crowd_night] {spec['name']}: бюджет кончился на этом кошельке "
                  f"(n={r['n_purchases_analyzed']}/{MAX_PURCHASES_PER_WALLET_CONTROL}) -- "
                  f"не считается попыткой, продолжу со следующего тика", flush=True)
            return
        got = r.get("median_growth_pct_30s")
        ok = got is not None and same_sign_same_order(got, spec["expected_pct"])
        ctrl["results"][addr] = {**r, "expected_pct": spec["expected_pct"], "match_sign_and_order": ok}
        print(f"[crowd_night] {spec['name']}: n={r['n_purchases_analyzed']} "
              f"медиана={got} (ожидание~{spec['expected_pct']}%) match={ok}", flush=True)

    if len(ctrl["results"]) < len(CONTROL_SPEC):
        return

    all_ok = all(v["match_sign_and_order"] for v in ctrl["results"].values())
    status["attempts"] = status.get("attempts", 0) + 1
    if all_ok:
        status["stage"] = "scan"
        status["last_error"] = None
        print(f"[crowd_night] КОНТРОЛЬ ПРОЙДЕН (попытка {status['attempts']}/{MAX_ATTEMPTS}) -> stage=scan", flush=True)
    elif status["attempts"] < MAX_ATTEMPTS:
        print(f"[crowd_night] контроль не прошёл (попытка {status['attempts']}/{MAX_ATTEMPTS}) -- повтор на следующем тике", flush=True)
        ctrl["results"] = {}
    else:
        bad = next((v for v in ctrl["results"].values() if not v["match_sign_and_order"]), None)
        raw_purchase = (bad.get("purchases") or [None])[0] if bad else None
        medians = {v["name"]: v["median_growth_pct_30s"] for v in ctrl["results"].values()}
        status["stage"] = "stopped"
        status["last_error"] = f"контроль не сошёлся за {MAX_ATTEMPTS} попытки -- медианы: {medians}"
        status["control_raw_one_purchase"] = raw_purchase
        print(f"[crowd_night] СТОП: {status['last_error']}", flush=True)


def run_scan(status: dict, deadline: float) -> None:
    ranked = load_ranked_wallets()
    status["total_wallets"] = len(ranked)
    crowd = json.loads(CROWD_PATH.read_text()) if CROWD_PATH.exists() else {}
    for row in ranked:
        addr = row["address"]
        if addr in crowd:
            continue
        if time.monotonic() > deadline:
            print("[crowd_night] scan: бюджет исчерпан, продолжу со следующего тика", flush=True)
            break
        r = analyze_wallet(addr, row.get("name"), min_sol=2.0, max_purchases=MAX_PURCHASES_PER_WALLET_SCAN,
                            deadline=deadline)
        if r.get("wallet_budget_cut"):
            print(f"[crowd_night] {row.get('name')} ({addr[:10]}..): бюджет кончился на этом кошельке "
                  f"(n={r['n_purchases_analyzed']}/{MAX_PURCHASES_PER_WALLET_SCAN}) -- не сохраняю, "
                  f"продолжу со следующего тика", flush=True)
            break
        r["fomo_rank"] = row.get("fomo_rank")
        r["stands_in"] = row.get("stands_in")
        crowd[addr] = r
        CROWD_PATH.write_text(json.dumps(crowd, ensure_ascii=False, indent=2, default=str))
        print(f"[crowd_night] {row.get('name')} ({addr[:10]}.., {row.get('stands_in')}): "
              f"n={r['n_purchases_analyzed']} медиана_роста={r['median_growth_pct_30s']} "
              f"empty_share={r['empty_share']} ({len(crowd)}/{len(ranked)})", flush=True)
    status["done_wallets"] = len(crowd)
    if len(crowd) >= len(ranked):
        status["stage"] = "done"
        print("[crowd_night] ВСЕ 90 ГОТОВЫ -- stage=done", flush=True)


def main() -> None:
    status = load_status()
    if status["stage"] in ("done", "stopped"):
        print(f"[crowd_night] stage={status['stage']} -- работа не требуется, выхожу без изменений", flush=True)
        save_status(status)
        return

    # Найдено при разборе зависаний: без привилегированного RPC (публичный
    # узел, ~8 запросов/с целевой темп + троттлинг) один прогон не
    # укладывался ни в TIME_BUDGET_S, ни в 25-минутный таймаут job'а --
    # HELIUS_API уже подтверждён рабочим и быстрым в этом репозитории.
    privileged_rpc = fp.alchemy_available()
    print(f"[crowd_night] привилегированный RPC активен: {privileged_rpc} "
          f"(HELIUS_API={'есть' if os.environ.get('HELIUS_API') else 'нет'}, "
          f"ALCHEMY_API_KEY={'есть' if os.environ.get('ALCHEMY_API_KEY') else 'нет'})", flush=True)

    deadline = time.monotonic() + TIME_BUDGET_S
    try:
        if status["stage"] == "control":
            run_control(status, deadline)
        if status["stage"] == "scan" and time.monotonic() < deadline:
            run_scan(status, deadline)
    except Exception as exc:  # noqa: BLE001
        status["last_error"] = f"{type(exc).__name__}: {exc}"
        print(f"[crowd_night] ОШИБКА в этом тике (не критично, следующий тик продолжит): {status['last_error']}", flush=True)
    save_status(status)


if __name__ == "__main__":
    main()
