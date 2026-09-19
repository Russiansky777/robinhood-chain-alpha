#!/usr/bin/env python3
"""Владелец, 2026-09-19: с ~14:05 по Мадриду (12:05 UTC, CEST=UTC+2) во
всех трёх батчах включена галочка Custom, Priority Fee 0.0051, Bribery
Tip 0.0051 (раньше Turbo + High 0.01 без галочки); пилот без изменений.
Задача -- на ПЕРВОЙ ЖЕ сделке ЛЮБОГО батча после смены честно снять с
цепи: сетевую комиссию (meta.fee), чаевые (System Program transfer(ы) от
кошелька батча, БЕЗ предположения о конкретной сумме -- прежняя функция
tips_and_fee() в solana_dbot_realized_ledger.py матчит по жёстко
зашитым лампорт-маркерам 4_950_000/9_950_000, это НОВОЕ значение под
них не подходит, поэтому здесь отчёт по ВСЕМ system-transfer со
кошелька, без фильтра по сумме), получателя чаевых, compute unit limit
и price (ComputeBudget111111111111111111111111111111, parsed
instructions setComputeUnitLimit/setComputeUnitPrice).

Опрос вживую (событие ещё не произошло на момент написания скрипта) --
без искусственного тайм-аута внутри скрипта, воркфлоу сам ограничен по
времени; при отсутствии находки просто честно пишет текущий статус,
без выдумывания."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_batch_fee_change_first_trade.json"

CHANGE_TIME_UTC = 1789819500  # 2026-09-19 12:05:00 UTC = 14:05 Europe/Madrid (CEST, UTC+2)
BATCH_WALLETS = {
    "BATCH-1": "GYPzYfSP3htyfRCti5Wp6XTnUQh7zkwqTv6j7r4kUFrq",
    "BATCH-2": "HMQG8xXoVBWZTkEqNye5WcfttvdZwVb522EggFD5AWZ6",
    "BATCH-3": "BmjAUDbwBMxR5shrmzBtKRwveVahFGFiEH3oTq7QTHnu",
    # BATCH-4 (2026-09-19, старт ~15:35 Мадрид -- уже НА новом конфиге
    # 0.0051/0.0051, не "смена" для неё, а точка сравнения): кошелёк из
    # data/dbot_sieve_baseline.json.
    "BATCH-4": "EjeXrxabRKmwLxvfQdXYN3oD3d3uWe2fuqqA5Qda2p8N",
}
POLL_INTERVAL_S = 20
MAX_POLL_MINUTES = 170  # запас под лимит job (180 мин)


def find_new_sig_after(wallet: str, cutoff: int) -> dict | None:
    """Последние подписи кошелька (свежие, без кэша) -- если самая
    свежая уже >= cutoff, это кандидат. Берём caмую РАННЮЮ среди тех,
    что >= cutoff (первая сделка ПОСЛЕ смены, не последняя)."""
    sigs = fp.get_signatures_for_address(wallet, limit=20)
    candidates = [s for s in sigs if s.get("blockTime") and s["blockTime"] >= cutoff and s.get("err") is None]
    if not candidates:
        return None
    return min(candidates, key=lambda s: s["blockTime"])


def extract_compute_budget(tx: dict) -> dict:
    instrs = tx.get("transaction", {}).get("message", {}).get("instructions", [])
    out = {"compute_unit_limit": None, "compute_unit_price_microlamports": None}
    for ix in instrs:
        if not isinstance(ix, dict):
            continue
        if ix.get("program") != "compute-budget":
            continue
        parsed = ix.get("parsed") or {}
        if not isinstance(parsed, dict):
            continue
        t = parsed.get("type")
        info = parsed.get("info", {})
        if t == "setComputeUnitLimit":
            out["compute_unit_limit"] = info.get("units")
        elif t == "setComputeUnitPrice":
            out["compute_unit_price_microlamports"] = info.get("microLamports")
    return out


def extract_all_system_transfers(tx: dict, wallet: str) -> list[dict]:
    instrs = tx.get("transaction", {}).get("message", {}).get("instructions", [])
    inner = (tx.get("meta") or {}).get("innerInstructions") or []
    out = []

    def scan(ix_list, source_label):
        for ix in ix_list:
            if not isinstance(ix, dict):
                continue
            if ix.get("program") != "system":
                continue
            parsed = ix.get("parsed") or {}
            if not isinstance(parsed, dict) or parsed.get("type") != "transfer":
                continue
            info = parsed.get("info", {})
            if info.get("source") != wallet:
                continue
            out.append({"source_ix": source_label, "destination": info.get("destination"),
                        "lamports": info.get("lamports"), "sol": (info.get("lamports") or 0) / 1e9})

    scan(instrs, "top_level")
    for grp in inner:
        if isinstance(grp, dict):
            scan(grp.get("instructions", []), "inner")
    return out


def analyze(wallet: str, task: str, sig_info: dict) -> dict:
    tx = fp.get_transaction(sig_info["signature"])
    if tx is None:
        return {"task": task, "wallet": wallet, "signature": sig_info["signature"],
                "block_time": sig_info["blockTime"], "HONEST_STATUS": "getTransaction вернул null"}
    meta = tx.get("meta") or {}
    cb = extract_compute_budget(tx)
    transfers = extract_all_system_transfers(tx, wallet)
    return {
        "task": task, "wallet": wallet, "signature": sig_info["signature"],
        "block_time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(sig_info["blockTime"])),
        "network_fee_sol": (meta.get("fee") or 0) / 1e9,
        "network_fee_lamports": meta.get("fee"),
        "compute_unit_limit": cb["compute_unit_limit"],
        "compute_unit_price_microlamports": cb["compute_unit_price_microlamports"],
        "all_system_transfers_from_wallet": transfers,
        "note": "чаевые -- среди all_system_transfers_from_wallet; прежний детектор "
                "(tips_and_fee в solana_dbot_realized_ledger.py) матчит по старым "
                "жёстко зашитым лампорт-маркерам 4_950_000/9_950_000, под новую "
                "сумму (Bribery Tip 0.0051) не подходит -- здесь без фильтра по сумме, "
                "весь список переводов от кошелька батча, отличить чаевые от иных "
                "переводов (напр. wrap SOL->WSOL на СОБСТВЕННЫЙ токен-аккаунт кошелька) "
                "нужно вручную по получателю/сумме.",
    }


def main() -> None:
    result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "change_time_utc": CHANGE_TIME_UTC,
                     "change_time_human": "2026-09-19 12:05:00 UTC = 14:05 Europe/Madrid (CEST)",
                     "batch_wallets": BATCH_WALLETS, "poll_log": []}
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    deadline = time.monotonic() + MAX_POLL_MINUTES * 60
    found = None
    while time.monotonic() < deadline:
        best = None
        for task, wallet in BATCH_WALLETS.items():
            try:
                cand = find_new_sig_after(wallet, CHANGE_TIME_UTC)
            except Exception as exc:  # noqa: BLE001
                result["poll_log"].append({"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                            "task": task, "exception": str(exc)})
                continue
            if cand and (best is None or cand["blockTime"] < best[2]["blockTime"]):
                best = (task, wallet, cand)
        if best:
            task, wallet, cand = best
            found = analyze(wallet, task, cand)
            result["FOUND"] = found
            OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            print(f"[fee_change] НАЙДЕНО: {task} {wallet} sig={cand['signature']} "
                  f"network_fee_sol={found['network_fee_sol']} "
                  f"cu_limit={found['compute_unit_limit']} cu_price={found['compute_unit_price_microlamports']} "
                  f"transfers={found['all_system_transfers_from_wallet']}", flush=True)
            break
        result["poll_log"].append({"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "status": "no_new_tx_yet"})
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print(f"[fee_change] пока новых сделок после смены нет, жду {POLL_INTERVAL_S}с", flush=True)
        time.sleep(POLL_INTERVAL_S)

    if found is None:
        result["FINAL_ANSWER"] = "За время опроса (170 мин) ни в одном из трёх батчей не появилось сделки после времени смены. Не выдумываю -- честно пусто."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[fee_change] " + result["FINAL_ANSWER"], flush=True)


if __name__ == "__main__":
    main()
