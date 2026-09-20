#!/usr/bin/env python3
"""Владелец: ускорить скан 217 -- метод НЕ менять (RPC, уже дважды
провалидирован: LEADER_WALLET+Brez на step "первый вход", DBot V2-
пересчёт не сошёлся дважды на лидере -- см. data/solana_dbot_full_scan.json).

Разбивка оставшихся (после 10 приоритетных BATCH-5) кандидатов на 4
шарда чередованием по лучшему рангу Fomo: 1-й,5-й,9-й.. -> шард 0;
2-й,6-й.. -> шард 1; и т.д. -- в каждом шарде есть кошельки с верха
рейтинга. 4 шарда = 4 параллельных job'а одного workflow (matrix), у
каждого свой файл результата data/solana_batch5_shard_<id>.json --
никаких конфликтов коммитов между шардами.

Уже посчитанное предыдущим (последовательным, отменённым ради скорости)
прогоном -- data/solana_dbot_full_scan.json:remaining_scan -- берём как
готовый кэш, не пересканируем те же адреса заново.

Как только кошелёк проходит фильтр (>=3 покупки >=2 SOL за 72ч) --
СРАЗУ (не дожидаясь конца шарда) дозаписывается ОДНОЙ строкой в общий
data/solana_fomo_passed.json. Файл сознательно в формате JSON Lines
(один JSON-объект на строку), а не JSON-массив -- параллельные append'ы
от разных шардов такой формат сливает git rebase'ом без содержательных
конфликтов (у обычного JSON-массива при параллельной перезаписи всего
файла конфликт неизбежен)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_batch5_rpc_check import PRIORITY_10, scan_wallet  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CANDIDATES_PATH = REPO_ROOT / "data" / "fomo_leaderboard_candidates.json"
OLD_RESULT_PATH = REPO_ROOT / "data" / "solana_dbot_full_scan.json"
PASSED_PATH = REPO_ROOT / "data" / "solana_fomo_passed.json"

TOTAL_SHARDS = 4
TIME_BUDGET_S = 45 * 60
COMMIT_INTERVAL_S = 60
PASS_THRESHOLD_GE2SOL = 3


def build_ranked_remaining() -> tuple[list[dict], dict]:
    candidates = json.loads(CANDIDATES_PATH.read_text())
    rows = candidates.get("final_table_sorted_by_followers") or []
    priority_addrs = {a for a, _ in PRIORITY_10}
    remaining = [r for r in rows if r["solana_address"] not in priority_addrs]
    fomo_rank = {r["solana_address"]: min((w["rank"] for w in r.get("windows_present", [])), default=10**9)
                 for r in rows}
    remaining.sort(key=lambda r: fomo_rank.get(r["solana_address"], 10**9))
    return remaining, fomo_rank


def load_preseeded_cache() -> dict:
    """Уже посчитанные отменённым последовательным прогоном кошельки --
    переиспользуем, не жжём RPC-бюджет повторно."""
    if not OLD_RESULT_PATH.exists():
        return {}
    try:
        old = json.loads(OLD_RESULT_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}
    return old.get("remaining_scan") or {}


def load_already_passed() -> set[str]:
    if not PASSED_PATH.exists():
        return set()
    out = set()
    for line in PASSED_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.add(json.loads(line)["address"])
        except Exception:  # noqa: BLE001
            continue
    return out


def append_passed_with_retry(row: dict, shard_id: int) -> bool:
    """Дозапись ОДНОЙ строки в общий файл + коммит/push с ретраем на
    конфликт (несколько шардов пишут в один файл параллельно) -- своя
    ретрай-логика, т.к. общий _git_commit_progress не сигнализирует
    успех/неудачу вызывающему коду."""
    line = json.dumps(row, ensure_ascii=False, default=str)
    ref = os.environ.get("GITHUB_REF_NAME", "HEAD")
    for attempt in range(6):
        with PASSED_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        try:
            subprocess.run(["git", "add", str(PASSED_PATH)], check=True, cwd=REPO_ROOT)
            subprocess.run(["git", "commit", "-m",
                             f"Solana buyer_200: FOMO прошёл фильтр {row['address'][:10]} (шард {shard_id}) [automated]"],
                            check=True, cwd=REPO_ROOT)
            push = subprocess.run(["git", "push"], cwd=REPO_ROOT)
            if push.returncode == 0:
                return True
        except subprocess.CalledProcessError as exc:
            print(f"[shard {shard_id}] git ошибка при записи passed-строки: {exc}", flush=True)
        print(f"[shard {shard_id}] push passed-файла отклонён, попытка {attempt + 1}/6 -- pull --rebase и повтор", flush=True)
        subprocess.run(["git", "pull", "--rebase", "origin", ref], cwd=REPO_ROOT)
        time.sleep(2 * (attempt + 1))
    print(f"[shard {shard_id}] ПРЕДУПРЕЖДЕНИЕ: не удалось закоммитить passed-строку для {row['address']} после ретраев", flush=True)
    return False


def main() -> None:
    shard_id = int(os.environ.get("SHARD_ID", "0"))
    out_path = REPO_ROOT / "data" / f"solana_batch5_shard_{shard_id}.json"

    remaining, fomo_rank = build_ranked_remaining()
    shard_wallets = [r for idx, r in enumerate(remaining) if idx % TOTAL_SHARDS == shard_id]
    print(f"[shard {shard_id}] моих кошельков: {len(shard_wallets)} из {len(remaining)} остальных", flush=True)

    preseed = load_preseeded_cache()
    result = json.loads(out_path.read_text()) if out_path.exists() else {"shard_id": shard_id, "scanned": {}}
    scanned = result["scanned"]
    already_passed = load_already_passed()

    started_at = time.monotonic()
    last_commit_at = started_at
    n_done = 0
    for row in shard_wallets:
        addr = row["solana_address"]
        if addr in scanned:
            continue
        if time.monotonic() - started_at > TIME_BUDGET_S:
            print(f"[shard {shard_id}] бюджет времени исчерпан -- остальное на следующий прогон", flush=True)
            break

        if addr in preseed and "error" not in preseed[addr]:
            r = dict(preseed[addr])
            r["_source"] = "preseed_cancelled_sequential_run"
            n_ge2 = r.get("n_first_entries_ge_2sol")
            median_val = r.get("median_spend_sol_equiv")
        else:
            r = scan_wallet(addr)
            r["_source"] = "shard_scan"
            n_ge2 = r.get("n_first_entries_ge_2sol")
            median_val = r.get("median_spend_sol_equiv")
        r["name"] = row.get("nickname")
        r["fomo_rank"] = fomo_rank.get(addr, 10**9)
        scanned[addr] = r
        n_done += 1
        print(f"[shard {shard_id}] {row.get('nickname')} ({addr[:10]}..): >=2SOL={n_ge2} "
              f"coverage={r.get('coverage_status')} ({r.get('coverage_hours_actual')}ч)", flush=True)

        if n_ge2 is not None and n_ge2 >= PASS_THRESHOLD_GE2SOL and addr not in already_passed:
            passed_row = {"address": addr, "name": row.get("nickname"), "n_ge_2sol": n_ge2,
                          "median_spend_sol": median_val, "fomo_rank": fomo_rank.get(addr, 10**9),
                          "shard": shard_id}
            if append_passed_with_retry(passed_row, shard_id):
                already_passed.add(addr)
                print(f"[shard {shard_id}] ПРОШЁЛ ФИЛЬТР: {row.get('nickname')} ({addr[:10]}..) "
                      f">=2SOL={n_ge2} медиана={median_val}", flush=True)

        if time.monotonic() - last_commit_at > COMMIT_INTERVAL_S:
            out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            fp._git_commit_progress(f"batch5_shard_{shard_id}", [out_path])
            last_commit_at = time.monotonic()

    result["n_shard_total"] = len(shard_wallets)
    result["n_shard_scanned"] = len(scanned)
    result["shard_complete"] = len(scanned) >= len(shard_wallets)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    fp._git_commit_progress(f"batch5_shard_{shard_id}_final", [out_path])
    print(f"[shard {shard_id}] итог: {n_done} сейчас, всего {len(scanned)}/{len(shard_wallets)}, "
          f"complete={result['shard_complete']}", flush=True)


if __name__ == "__main__":
    main()
