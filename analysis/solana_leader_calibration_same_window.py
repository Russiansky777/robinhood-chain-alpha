#!/usr/bin/env python3
"""Владелец, 2026-09-19: уточнить определения калибровки по 29
кандидатам.

Найдено: опорное число "~7.5 первых входов в сутки" (43 за 5.75 суток)
взято из selected_300.json -- это НЕ сырые первые входы, а count()
СТРОК СО STATUS=selected из classify() (solana_buyer200_select_extend.py):
кошелёк-подписант, РОВНО ОДИН положительный не-USDC/SOL минт, потрачено
>500 USDC, лог содержит "Instruction: Swap"/"Buy". zero_balance=True
среди ЭТИХ строк = 43. Это ФИЛЬТРОВАННОЕ определение, не "любой минт
пошёл с нуля в плюс" (тот наивный тест дал 255/сутки на калибровке
solana_29_candidates_flow.py -- аирдропы).

Законная калибровка -- НЕ "разные окна с одним и тем же фильтром", а
ОДНО И ТО ЖЕ окно дат (ровно 1789174317..1789671989, ~5.76 суток,
диапазон исходных 300 покупок) при ОДНОМ И ТОМ ЖЕ определении, но
пройденное НЕЗАВИСИМО НОВЫМ методом (свежее getSignaturesForAddress/
getTransaction на лидере, не переиспользование уже отобранного списка
транзакций из analysis.json) -- две независимые реализации должны
сойтись, если новому методу можно верить.

Два независимых прохода по ОДНОМУ И ТОМУ ЖЕ независимо найденному
набору транзакций лидера в этом окне (classify() дословный импорт --
фильтрованное определение, ожидаем ~43; и сигнатурная/is_signer-
проверка solana_29_candidates_flow.py -- сырое определение, БЕЗ фильтра
по сумме, для честного разрыва "сырое vs фильтрованное" на одном окне).

ВАЖНО (найдено на первом прогоне, реальный баг): полные транзакции
(jsonParsed) НЕЛЬЗЯ копить в state и коммитить в git целиком -- на
5183 транзакциях этого окна кэш вырос за 211МБ, GitHub отклонил push
(лимит 100МБ/файл) на ВСЕХ 5 попытках, промежуточные локальные коммиты
с гигантским блобом застряли в истории и заблокировали финальный push
даже после того, как файл был уменьшен. Обрабатываем КАЖДУЮ транзакцию
СРАЗУ по ходу сканирования (classify()+сырой тест), в state остаются
только счётчики и курсор пагинации -- никогда сама транзакция целиком."""
from __future__ import annotations

import json
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_buyer200_select_extend import classify, WALLET, USDC, SOL  # noqa: E402
from solana_29_candidates_flow import wallet_mint_deltas, is_signer  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_leader_calibration_same_window.json"

# Ровно диапазон исходных 300 покупок (см. selected_300.json: time
# 1789174317..1789671989) -- не "последние N суток от сейчас".
WINDOW_LO = 1789174317 - 60
WINDOW_HI = 1789671989 + 60
KNOWN_REFERENCE_FILTERED_COUNT = 43
KNOWN_REFERENCE_TOTAL_SELECTED = 300  # для честного напоминания -- 300 это ВСЕ отобранные (первые+докупки), не только первые

TOTAL_TIME_BUDGET_S = 20 * 60
COMMIT_INTERVAL_S = 90


def main() -> None:
    state: dict = {}
    if OUT_PATH.exists():
        try:
            state = json.loads(OUT_PATH.read_text())
        except (ValueError, OSError):
            state = {}
    before = state.get("_pagination_cursor")
    scan_done = state.get("_scan_done", False)
    n_scanned = state.get("_n_scanned", 0)

    n_selected = state.get("_n_selected", 0)
    n_selected_first = state.get("_n_selected_first", 0)
    n_wallet_not_signer = state.get("_n_wallet_not_signer", 0)
    n_ambiguous = state.get("_n_ambiguous", 0)
    n_not_selected = state.get("_n_not_selected", 0)
    n_raw_first_entries = state.get("_n_raw_first_entries", 0)
    n_raw_swaps = state.get("_n_raw_swaps", 0)

    started_at = time.monotonic()
    last_commit_at = started_at

    def save_progress(done: bool) -> None:
        s = {
            "_pagination_cursor": before, "_scan_done": done, "_n_scanned": n_scanned,
            "_n_selected": n_selected, "_n_selected_first": n_selected_first,
            "_n_wallet_not_signer": n_wallet_not_signer, "_n_ambiguous": n_ambiguous,
            "_n_not_selected": n_not_selected, "_n_raw_first_entries": n_raw_first_entries,
            "_n_raw_swaps": n_raw_swaps,
        }
        OUT_PATH.write_text(json.dumps(s, ensure_ascii=False, indent=2, default=str))

    if not scan_done:
        print(f"[calib] независимое сканирование лидера в окне [{WINDOW_LO},{WINDOW_HI}] "
              f"(уже обработано {n_scanned} tx)", flush=True)
        while time.monotonic() - started_at < TOTAL_TIME_BUDGET_S:
            batch = fp.get_signatures_for_address(WALLET, before=before)
            if not batch:
                scan_done = True
                break
            stop = False
            for s in batch:
                bt = s.get("blockTime")
                if bt is None:
                    continue
                if bt < WINDOW_LO:
                    stop = True
                    break
                if bt > WINDOW_HI:
                    continue
                if s.get("err") is not None:
                    continue
                tx = fp.get_transaction(s["signature"])
                if tx is None:
                    continue
                n_scanned += 1

                h = {"transactionIndex": tx.get("transactionIndex")}
                row, check = classify(tx, h)
                if row is not None:
                    n_selected += 1
                    if row["zero_balance"]:
                        n_selected_first += 1
                elif check["status"] == "wallet_not_signer":
                    n_wallet_not_signer += 1
                elif check["status"] == "ambiguous_buy":
                    n_ambiguous += 1
                else:
                    n_not_selected += 1

                deltas = wallet_mint_deltas(tx, WALLET)
                touched = [m for m in deltas["increased"] + deltas["decreased"] if m not in (USDC, SOL)]
                if touched and not is_signer(tx, WALLET):
                    pass
                else:
                    if touched:
                        n_raw_swaps += 1
                    for m in deltas["increased"]:
                        if m in (USDC, SOL):
                            continue
                        if D(deltas["pre_balances"].get(m, "0")) != 0:
                            continue
                        n_raw_first_entries += 1
            before = batch[-1]["signature"]
            if time.monotonic() - last_commit_at > COMMIT_INTERVAL_S:
                save_progress(done=False)
                fp._git_commit_progress("leader_calib", [OUT_PATH])
                last_commit_at = time.monotonic()
                print(f"[calib] прогресс: {n_scanned} tx обработано, "
                      f"selected_first={n_selected_first}, raw_first={n_raw_first_entries}", flush=True)
            if stop:
                scan_done = True
                break
            if len(batch) < 1000:
                scan_done = True
                break
        else:
            print("[calib] бюджет времени исчерпан на этом прогоне -- продолжим со следующего", flush=True)

    save_progress(done=scan_done)

    if not scan_done:
        print(f"[calib] сканирование не завершено ({n_scanned} tx обработано) -- итоги считать рано, "
              f"перезапусти прогон", flush=True)
        return

    print(f"[calib] сканирование завершено: {n_scanned} транзакций лидера в окне", flush=True)

    days = (WINDOW_HI - WINDOW_LO) / 86400
    out = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "window_lo": WINDOW_LO, "window_hi": WINDOW_HI, "window_days": round(days, 3),
        "n_transactions_scanned": n_scanned,
        "known_reference": {"filtered_first_entries": KNOWN_REFERENCE_FILTERED_COUNT,
                             "total_selected_first_plus_addons": KNOWN_REFERENCE_TOTAL_SELECTED,
                             "source": "selected_300.json (classify(), MIN_SPEND=500, single positive mint, "
                                       "swap/buy log message) -- ЭТО фильтрованное определение, не сырое"},
        "pass1_classify_filtered": {
            "n_selected_total": n_selected, "n_selected_first_entries": n_selected_first,
            "n_wallet_not_signer": n_wallet_not_signer, "n_ambiguous_buy": n_ambiguous,
            "n_not_selected_other": n_not_selected,
            "first_entries_per_day": round(n_selected_first / days, 3),
            "matches_known_reference": n_selected_first == KNOWN_REFERENCE_FILTERED_COUNT,
            "diff_from_reference": n_selected_first - KNOWN_REFERENCE_FILTERED_COUNT,
        },
        "pass2_raw_signer_only": {
            "n_raw_first_entries": n_raw_first_entries, "n_raw_swaps": n_raw_swaps,
            "raw_first_entries_per_day": round(n_raw_first_entries / days, 3),
        },
        "verdict": None,
    }
    p1 = out["pass1_classify_filtered"]
    if p1["matches_known_reference"]:
        out["verdict"] = ("СОШЛОСЬ ТОЧНО: независимое сканирование через classify() на том же окне "
                           "воспроизводит 43 -- метод подтверждён на этом окне.")
    else:
        out["verdict"] = (f"НЕ совпало точно: {n_selected_first} вместо {KNOWN_REFERENCE_FILTERED_COUNT} "
                           f"(разница {p1['diff_from_reference']}) -- см. детали расхождения, не объявляем "
                           "метод подтверждённым автоматически.")
    print(f"[calib] {out['verdict']}", flush=True)
    print(f"[calib] raw (сырое, без фильтра по сумме) на ТОМ ЖЕ окне: "
          f"{n_raw_first_entries} первых входов = {out['pass2_raw_signer_only']['raw_first_entries_per_day']}/сутки "
          f"-- разница с фильтрованным определением честно показывает вклад фильтра по сумме", flush=True)

    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"[calib] записано в {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
