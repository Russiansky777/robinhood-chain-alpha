#!/usr/bin/env python3
"""Владелец: контроль зачтён вручную, Brez-контроль (п.5) закрыт --
больше не гоняем ни то, ни другое (см. ARCHIVE_PATH для истории). С
этого момента crowd_night -- ЧИСТЫЙ шардированный скан 90 кошельков,
без стадий control/scan-then-control.

Самоходный процесс на GitHub Actions (cron */10 мин), но теперь в 4
параллельных шардах (strategy.matrix, см. .github/workflows/
crowd_night.yml) -- владелец: скорость 1 кошелёк/тик была неприемлема,
цель 90 за 1-1.5ч. Каждый шард обрабатывает КАЖДЫЙ 4-й кошелёк из
общего списка (idx % 4 == SHARD_ID, интерливинг, как в скане 217/
BATCH-5) -- свой файл статуса data/night_status_shard_N.json и свой
файл результатов data/solana_fomo_crowd_shard_N.json, чтобы 4
параллельных job'а никогда не писали в один и тот же файл. Отдельный
шаг агрегации (analysis/solana_crowd_night_aggregate.py) после всех 4
шардов собирает общий data/night_status.json и объединённый
data/solana_fomo_crowd.json.

Каждый тик работает не дольше TIME_BUDGET_S, продолжает с места
остановки (crowd-файл уже содержит все готовые кошельки, ranked-список
пересчитывается заново и уже готовые просто пропускаются)."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_crowd_common import analyze_wallet, MAX_PURCHASES_PER_WALLET_SCAN  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PASSED_PATH = REPO_ROOT / "data" / "solana_fomo_passed.json"
FOLLOW_ORDERS_PATH = REPO_ROOT / "data" / "dbot_follow_orders_raw.json"
LIVE_TASK_NAMES = ("BATCH-3", "BATCH-5", "BATCH-6", "BATCH-7")

TOTAL_SHARDS = 4
SHARD_ID = int(os.environ.get("SHARD_ID", "-1"))  # -1 = без шардирования (весь список -- ручной прогон)

if SHARD_ID >= 0:
    STATUS_PATH = REPO_ROOT / "data" / f"night_status_shard_{SHARD_ID}.json"
    CROWD_PATH = REPO_ROOT / "data" / f"solana_fomo_crowd_shard_{SHARD_ID}.json"
else:
    STATUS_PATH = REPO_ROOT / "data" / "night_status.json"
    CROWD_PATH = REPO_ROOT / "data" / "solana_fomo_crowd.json"

TIME_BUDGET_S = 20 * 60


def shard_tag() -> str:
    return f" шард {SHARD_ID}" if SHARD_ID >= 0 else ""


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
    return {"stage": "scan", "shard_id": SHARD_ID, "done_wallets": 0, "total_wallets": 0,
            "updated_utc": None, "last_error": None, "run_url": None}


def save_status(status: dict) -> None:
    status["updated_utc"] = now_utc()
    status["run_url"] = run_url()
    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2, default=str))


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
    # по рангу Fomo. Шардирование -- интерливинг ПОСЛЕ сортировки, чтобы
    # каждый шард получил свою долю и приоритетных, и очередных кошельков.
    rows.sort(key=lambda r: (0 if r["stands_in"] != "очередь" else 1, r.get("fomo_rank", 10**9)))
    if SHARD_ID >= 0:
        rows = [r for idx, r in enumerate(rows) if idx % TOTAL_SHARDS == SHARD_ID]
    return rows


def run_scan(status: dict, deadline: float) -> None:
    ranked = load_ranked_wallets()
    status["total_wallets"] = len(ranked)
    crowd = json.loads(CROWD_PATH.read_text()) if CROWD_PATH.exists() else {}
    for row in ranked:
        addr = row["address"]
        if addr in crowd:
            continue
        if time.monotonic() > deadline:
            print(f"[crowd_night{shard_tag()}] scan: бюджет исчерпан, продолжу со следующего тика", flush=True)
            break
        r = analyze_wallet(addr, row.get("name"), min_sol=2.0, max_purchases=MAX_PURCHASES_PER_WALLET_SCAN,
                            deadline=deadline)
        if r.get("wallet_budget_cut"):
            if time.monotonic() > deadline:
                print(f"[crowd_night{shard_tag()}] {row.get('name')} ({addr[:10]}..): бюджет кончился на этом "
                      f"кошельке (n_приценено={r['n_priced']}, n_decode_fail={r['n_decode_fail']}) -- не сохраняю, "
                      f"продолжу со следующего тика", flush=True)
                break
            print(f"[crowd_night{shard_tag()}] {row.get('name')} ({addr[:10]}..): RPC-сбой при получении покупок "
                  f"(бюджет ещё есть) -- пропускаю, перехожу к следующему кошельку", flush=True)
            continue
        r["fomo_rank"] = row.get("fomo_rank")
        r["stands_in"] = row.get("stands_in")
        crowd[addr] = r
        CROWD_PATH.write_text(json.dumps(crowd, ensure_ascii=False, indent=2, default=str))
        print(f"[crowd_night{shard_tag()}] {row.get('name')} ({addr[:10]}.., {row.get('stands_in')}): "
              f"n_всего={r['n_purchases_total']} n_приценено={r['n_priced']} n_decode_fail={r['n_decode_fail']} "
              f"медиана_роста={r['median_growth_pct_30s']} empty_share={r['empty_share']} "
              f"({len(crowd)}/{len(ranked)})", flush=True)
    status["done_wallets"] = len(crowd)
    if len(crowd) >= len(ranked):
        status["stage"] = "done"
        print(f"[crowd_night{shard_tag()}] ВСЕ {len(ranked)} (мой шард) ГОТОВЫ -- stage=done", flush=True)


def main() -> None:
    status = load_status()
    if status["stage"] == "done":
        print(f"[crowd_night{shard_tag()}] stage=done -- работа не требуется, выхожу без изменений", flush=True)
        save_status(status)
        return

    privileged_rpc = fp.alchemy_available()
    print(f"[crowd_night{shard_tag()}] привилегированный RPC активен: {privileged_rpc} "
          f"(HELIUS_API={'есть' if os.environ.get('HELIUS_API') else 'нет'}, "
          f"ALCHEMY_API_KEY={'есть' if os.environ.get('ALCHEMY_API_KEY') else 'нет'})", flush=True)

    deadline = time.monotonic() + TIME_BUDGET_S
    # Владелец, найдено на реальном прогоне 2026-09-20: без этого один
    # зависший RPC-вызов (rpc_call: до 20 попыток x потолок 45с = до 25
    # минут) убивал ВЕСЬ job таймаутом раньше, чем внутренний 20-минутный
    # цикл успевал заметить дедлайн -- ни одна покупка/кошелёк даже не
    # сохранялись. Теперь rpc_call сам обрывается по этому дедлайну.
    fp.set_soft_deadline(deadline)
    try:
        run_scan(status, deadline)
    except Exception as exc:  # noqa: BLE001
        status["last_error"] = f"{type(exc).__name__}: {exc}"
        print(f"[crowd_night{shard_tag()}] ОШИБКА в этом тике (не критично, следующий тик продолжит): "
              f"{status['last_error']}", flush=True)
    save_status(status)


if __name__ == "__main__":
    main()
