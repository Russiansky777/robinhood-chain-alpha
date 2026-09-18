#!/usr/bin/env python3
"""Владелец, 2026-09-18, смена подхода: не пересечение 7д/30д, а ОБЪЕДИНЕНИЕ
всех четырёх окон рейтинга Fomo (24ч/7д/30д/всё время), уже собранных
ранее (data/fomo_leaderboard_raw.json, solana_fomo_leaderboard_collect.py)
-- локально, без новых вызовов Fomo API. Дубликаты по userId схлопываются,
с пометкой, в каких окнах и на каком месте кошелёк присутствует.

Два дешёвых фильтра, оба честно объяснены владельцем как причина смены
подхода (подсчёт первых входов за неделю съедал минуты на гиперактивный
кошелёк -- убран целиком, сито сработает и без него):
  1. followers > 100 -- уже есть в данных рейтинга, без сети.
  2. Активность на Solana за последние 3 суток -- ОДИН вызов
     getSignaturesForAddress на кошелёк (без пагинации, без разбора
     транзакций) -- честно НЕ подтверждает, что это именно свопы (для
     этого нужен getTransaction на каждую подпись, что уже не "один
     запрос на кошелёк"), только что кошелёк недавно был активен на
     Solana. Секунды на кошелёк, не минуты.

Результат -- таблица: ник, Solana-адрес, подписчики, окна+места, дата
последней сделки на Solana, отсортирована по подписчикам."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_LEADERBOARD_PATH = REPO_ROOT / "data" / "fomo_leaderboard_raw.json"
CANDIDATES_PATH = REPO_ROOT / "data" / "fomo_leaderboard_candidates.json"

WINDOWS = ["24h", "7d", "30d", "all"]
MIN_FOLLOWERS = 100
ACTIVITY_LOOKBACK_DAYS = 3
TIME_BUDGET_S = 18 * 60
COMMIT_INTERVAL_S = 60


def build_union(raw: dict) -> list[dict]:
    by_user: dict[str, dict] = {}
    for window in WINDOWS:
        items = ((raw.get("windows") or {}).get(window) or {}).get("items") or []
        for item in items:
            w = item.get("wallets") or {}
            sol = w.get("solana")
            user_id = item.get("userId")
            if not sol or not user_id:
                continue
            row = by_user.setdefault(user_id, {
                "user_id": user_id, "solana_address": sol, "evm_address": w.get("evm"),
                "nickname": item.get("displayName") or item.get("handle"),
                "followers": item.get("followers"), "windows_present": [],
            })
            row["windows_present"].append({"window": window, "rank": item.get("rank"),
                                            "pnlUsd": item.get("pnlUsd"), "volumeUsd": item.get("volumeUsd")})
    return list(by_user.values())


def check_wallet_activity(address: str) -> dict:
    """ОДИН вызов getSignaturesForAddress, limit=1000, без пагинации --
    честно приближённо: "недавняя активность", не "недавний своп"
    (см. докстринг модуля)."""
    batch = fp.get_signatures_for_address(address, limit=1000)
    successful = [s for s in batch if s.get("err") is None and s.get("blockTime")]
    if not successful:
        return {"has_recent_activity_3d": False, "last_trade_date_utc": None, "n_sigs_fetched": len(batch)}
    last_bt = max(s["blockTime"] for s in successful)
    cutoff = int(time.time()) - ACTIVITY_LOOKBACK_DAYS * 86400
    return {
        "has_recent_activity_3d": last_bt >= cutoff,
        "last_trade_date_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(last_bt)),
        "n_sigs_fetched": len(batch),
    }


def main() -> None:
    if not RAW_LEADERBOARD_PATH.exists():
        print("[fomo_onchain] нет fomo_leaderboard_raw.json -- сначала solana_fomo_leaderboard_collect.py.", flush=True)
        return
    raw = json.loads(RAW_LEADERBOARD_PATH.read_text())

    # Резюмируемость: если candidates-файл уже содержит union с прошлого
    # (возможно, оборванного) прохода -- продолжаем его, не начинаем заново.
    if CANDIDATES_PATH.exists():
        existing = json.loads(CANDIDATES_PATH.read_text())
        if existing.get("approach") == "union_all_windows":
            union_rows = existing["union_candidates"]
            print(f"[fomo_onchain] продолжаем предыдущий union-проход: {len(union_rows)} кошельков", flush=True)
        else:
            union_rows = build_union(raw)
            print(f"[fomo_onchain] новый union-проход (старый файл был другого формата): {len(union_rows)} кошельков", flush=True)
    else:
        union_rows = build_union(raw)
        print(f"[fomo_onchain] union всех 4 окон: {len(union_rows)} уникальных кошельков", flush=True)

    n_ge_followers = sum(1 for r in union_rows if (r.get("followers") or 0) > MIN_FOLLOWERS)
    print(f"[fomo_onchain] followers>{MIN_FOLLOWERS}: {n_ge_followers}/{len(union_rows)}", flush=True)

    started_at = time.monotonic()
    last_commit_at = started_at
    n_done_this_run = 0
    for row in union_rows:
        if (row.get("followers") or 0) <= MIN_FOLLOWERS:
            continue  # дешёвый фильтр 2 -- без сети, пропускаем совсем
        if row.get("has_recent_activity_3d") is not None:
            continue  # уже проверен в прошлом проходе
        if time.monotonic() - started_at > TIME_BUDGET_S:
            print("[fomo_onchain] бюджет времени исчерпан -- остальное на следующий прогон", flush=True)
            break
        try:
            result = check_wallet_activity(row["solana_address"])
        except RuntimeError as exc:
            row["has_recent_activity_3d"] = "error"
            row["_error"] = str(exc)[:300]
            print(f"[fomo_onchain] {row['solana_address'][:12]}.. ошибка: {exc}", flush=True)
            continue
        row.update(result)
        n_done_this_run += 1
        print(f"[fomo_onchain] {row['solana_address'][:12]}.. active_3d={result['has_recent_activity_3d']} "
              f"last_trade={result['last_trade_date_utc']}", flush=True)

        if time.monotonic() - last_commit_at > COMMIT_INTERVAL_S:
            out = {"approach": "union_all_windows", "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "union_candidates": union_rows}
            CANDIDATES_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
            fp._git_commit_progress("fomo_onchain_filter", [CANDIDATES_PATH])
            last_commit_at = time.monotonic()

    n_checked = sum(1 for r in union_rows if r.get("has_recent_activity_3d") is not None)
    n_active = sum(1 for r in union_rows if r.get("has_recent_activity_3d") is True)
    final_table = sorted(
        [r for r in union_rows if r.get("has_recent_activity_3d") is True],
        key=lambda r: r.get("followers") or 0, reverse=True,
    )
    out = {
        "approach": "union_all_windows",
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_union_total": len(union_rows),
        "n_followers_gt_100": n_ge_followers,
        "n_activity_checked": n_checked,
        "n_activity_checked_pending": n_ge_followers - n_checked,
        "n_active_last_3d": n_active,
        "final_table_sorted_by_followers": final_table,
        "union_candidates": union_rows,
    }
    CANDIDATES_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"[fomo_onchain] прогон: {n_done_this_run} проверено сейчас; всего активность проверена "
          f"{n_checked}/{n_ge_followers} (followers>100); активных за 3д={n_active}", flush=True)


if __name__ == "__main__":
    main()
