#!/usr/bin/env python3
"""Владелец, задача 2, шаги B+C: кто входит быстрее нас (позиции +1..+5
после источника, раньше нашего входа) и через какой сервис -- по уже
построенным логам трассы (8 сделок пилота + CC, источник = jg), БЕЗ
нового скана. Единственные новые RPC-вызовы -- getTransaction по
подписям, УЖЕ известным из логов (не новый поиск, прямой lookup)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_entry_log import tx_signers  # noqa: E402
from solana_batch_fee_change_first_trade import extract_all_system_transfers, extract_compute_budget  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_fast_buyers_services.json"
BOT_ADDR_PATH = REPO_ROOT / "data" / "solana_bot_fee_addresses.json"

LOG_FILES = {
    "jupcat": "data/solana_entry_log_jupcat.json",
    "xzc5swuory": "data/solana_entry_log_xzc5swuory.json",
    "egp1f5j9ld": "data/solana_entry_log_egp1f5j9ld.json",
    "gtdzkaqvmz": "data/solana_entry_log_gtdzkaqvmz.json",
    "vaddewuhuy": "data/solana_entry_log_vaddewuhuy.json",
    "hgcxvs6kjh": "data/solana_entry_log_hgcxvs6kjh.json",
    "7runv1hjfc": "data/solana_entry_log_7runv1hjfc.json",
    "gpuxeqplff": "data/solana_entry_log_gpuxeqplff.json",
    "akbot_cc": "data/solana_entry_log_akbot_cc.json",  # источник = jg
}

WSOL = "So11111111111111111111111111111111111111112"

INFRA_PROGRAMS = {
    "11111111111111111111111111111111": "System",
    "ComputeBudget111111111111111111111111111111": "ComputeBudget",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "Token",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb": "Token-2022",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL": "ATA",
}
# dex_labels.json -- уже собранный (прежней сессией) справочник DEX/AMM
# program_id -> имя, реально встречающихся на цепи (не выдумано здесь).
DEX_LABELS_PATH = REPO_ROOT / "data" / "solana_buyer_200" / "prior" / "current" / "buyer_100" / "dex_labels.json"
DEX_PROGRAMS = json.loads(DEX_LABELS_PATH.read_text()) if DEX_LABELS_PATH.exists() else {}
# dex_labels.json не содержит главный роутер-агрегатор Jupiter v6 --
# подтверждён на цепи этой же сессией (лог "Instruction: SharedAccountsRouteV2"
# у programId JUP6Lkb... в решённых ранее транзакциях), добавляем явно,
# иначе он лишним шумом лезет в "прочие программы".
DEX_PROGRAMS.setdefault("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4", "Jupiter Aggregator v6")
# Аналогично: собственный вспомогательный fee-калькулятор Pump.fun AMM
# (наблюдался в этой сессии -- "Program log: Instruction: GetFeesWithQuoteMint",
# CPI внутри самого AMM-инструкции) -- часть протокола DEX, не сервис.
DEX_PROGRAMS.setdefault("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ", "Pump.fun AMM fee calculator")

# Известные заранее (даны владельцем).
KNOWN_SERVICES = {
    "AKbotZVF3zr9i4Cz2Lv34s6SydytjdidruoGTnvwPxFh": "AKBot",
    "F7F8QYPCc3zDYNeAh4UUE2wJ7Mq1huid5MeMo7PPzsqB": "DBot",
}
# Релеи, уже найдены НА ЦЕПИ этой же сессией (solana_akbot_wallet_probe.py,
# 20 последних покупок DT8hib...) -- не эталонный список Jito/0slot,
# только то, что реально наблюдалось + подходит под префиксы, которые
# дал владелец (astra/AsTra/ste11/LandX).
KNOWN_RELAYS = {
    "DiTmWENJsHQdawVUUKnUXkconcpW4Jv52TnMWhkncF6t": "релей (наблюдался у AKBot, не идентифицирован конкретно)",
    "AsTRAEoyMofR3vUPpf9k68Gsfb6ymTZttEtsAbv8Bk4d": "релей (Astralane, префикс AsTra)",
    "astra9xWY93QyfG6yM8zwsKsRodscjQ2uU2HKNL5prk": "релей (Astralane, префикс astra)",
    "ste11p5x8tJ53H1NbNQsRBg1YNRd4GcVpxtDw8PBpmb": "релей (префикс ste11)",
    "ste11eZvF5bebo6EVyGMJHV6LtPtx7e6TzNZtxPfXGy": "релей (префикс ste11)",
    "LandX6RsjjnfaxfYAFTZX1TW9Cgfe9HFStj9nRDd5SK": "релей (префикс LandX)",
}
RELAY_PREFIXES = ("astra", "AsTra", "ste11", "LandX")

BOT_FEE_ADDRESSES: dict[str, list[str]] = {}
if BOT_ADDR_PATH.exists():
    for row in json.loads(BOT_ADDR_PATH.read_text())["rows"]:
        BOT_FEE_ADDRESSES.setdefault(row["address"], []).append(row["bot_name"])


def classify_address(addr: str) -> str:
    if addr in KNOWN_SERVICES:
        return KNOWN_SERVICES[addr]
    if addr in KNOWN_RELAYS:
        return KNOWN_RELAYS[addr]
    if any(addr.startswith(p) for p in RELAY_PREFIXES):
        return f"релей (неопознанный, префикс совпадает: {addr[:6]}..)"
    if addr in BOT_FEE_ADDRESSES:
        bots = BOT_FEE_ADDRESSES[addr]
        return "/".join(bots) + " (spellbook fee_receiver)"
    return "не опознан"


def wsol_token_transfers(tx: dict, wallet: str) -> list[dict]:
    """SPL-переводы (не своп) минта WSOL с участием wallet как source --
    transfer/transferChecked ВНЕ инструкций основного свопа (тот же
    принцип, что extract_all_system_transfers, но для токен-программы)."""
    instrs = tx.get("transaction", {}).get("message", {}).get("instructions", [])
    inner = (tx.get("meta") or {}).get("innerInstructions") or []
    out = []

    def scan(ix_list):
        for ix in ix_list:
            if not isinstance(ix, dict) or ix.get("program") not in ("spl-token", "spl-token-2022"):
                continue
            parsed = ix.get("parsed") or {}
            if not isinstance(parsed, dict) or parsed.get("type") not in ("transfer", "transferChecked"):
                continue
            info = parsed.get("info", {})
            mint = info.get("mint")
            if mint is not None and mint != WSOL:
                continue
            if info.get("authority") != wallet and info.get("source") != wallet:
                continue
            amt = info.get("tokenAmount", {}).get("uiAmount") if "tokenAmount" in info else info.get("amount")
            out.append({"destination": info.get("destination"), "amount_raw": amt, "mint": mint})

    scan(instrs)
    for g in inner:
        scan(g.get("instructions", []))
    return out


def other_program_ids(tx: dict) -> list[str]:
    instrs = tx.get("transaction", {}).get("message", {}).get("instructions", [])
    inner = (tx.get("meta") or {}).get("innerInstructions") or []
    ids = {ix.get("programId") for ix in instrs if isinstance(ix, dict)}
    for g in inner:
        for ix in g.get("instructions", []):
            if isinstance(ix, dict):
                ids.add(ix.get("programId"))
    return sorted(i for i in ids if i and i not in INFRA_PROGRAMS and i not in DEX_PROGRAMS)


def main() -> None:
    all_fast_rows = []
    for label, path in LOG_FILES.items():
        p = REPO_ROOT / path
        if not p.exists():
            print(f"[fast_buyers] {label}: файл лога не найден, пропуск", flush=True)
            continue
        d = json.loads(p.read_text())
        our_seq = (d.get("summary") or {}).get("our_buy_global_seq")
        rows = d.get("rows") or []
        fast = [r for r in rows if r.get("positions_from_leader") is not None
                and 1 <= r["positions_from_leader"] <= 5
                and (our_seq is None or r["global_seq"] < our_seq)
                and r.get("wallet_label") not in ("ЛИДЕР",)]
        print(f"[fast_buyers] {label}: {len(fast)} быстрых покупателей (позиции +1..+5, раньше нас)", flush=True)
        for r in fast:
            sig = r["signature"]
            tx = fp.get_transaction(sig)  # lookup по УЖЕ известной подписи из лога, не новый поиск
            if tx is None:
                continue
            wallet = r.get("wallet")
            transfers_sol = extract_all_system_transfers(tx, wallet) if wallet else []
            transfers_wsol = wsol_token_transfers(tx, wallet) if wallet else []
            cb = extract_compute_budget(tx)
            other_pids = other_program_ids(tx)
            size_sol = r.get("size_sol")

            transfer_rows = []
            for t in transfers_sol:
                pct = round(t["sol"] / size_sol * 100, 3) if size_sol else None
                transfer_rows.append({"kind": "SOL", "destination": t["destination"], "amount_sol": t["sol"],
                                       "pct_of_trade_size": pct, "service": classify_address(t["destination"])})
            for t in transfers_wsol:
                transfer_rows.append({"kind": "WSOL", "destination": t["destination"], "amount_raw": t["amount_raw"],
                                       "service": classify_address(t["destination"]) if t["destination"] else None})

            pid_rows = [{"program_id": pid, "service": classify_address(pid)} for pid in other_pids]

            all_fast_rows.append({
                "trade_label": label, "signature": sig, "signer": wallet, "slot": r.get("slot"),
                "position_from_source": r.get("positions_from_leader"), "size_sol": size_sol,
                "compute_unit_price_microlamports": cb.get("compute_unit_price_microlamports"),
                "compute_unit_limit": cb.get("compute_unit_limit"),
                "transfers": transfer_rows, "other_program_ids": pid_rows,
            })

    # ---------- Сводная таблица: получатель/программа x встречаемость ----------
    agg: dict[str, dict] = {}
    for row in all_fast_rows:
        for t in row["transfers"]:
            dest = t.get("destination")
            if not dest:
                continue
            e = agg.setdefault(dest, {"kind": "recipient", "service": t["service"], "n_seen": 0,
                                       "positions": [], "amounts_sol": []})
            e["n_seen"] += 1
            e["positions"].append(row["position_from_source"])
            if t.get("kind") == "SOL":
                e["amounts_sol"].append(t["amount_sol"])
        for p in row["other_program_ids"]:
            pid = p["program_id"]
            e = agg.setdefault(pid, {"kind": "program", "service": p["service"], "n_seen": 0,
                                      "positions": [], "amounts_sol": []})
            e["n_seen"] += 1
            e["positions"].append(row["position_from_source"])

    summary_table = []
    for addr, e in agg.items():
        summary_table.append({
            "address_or_program": addr, "kind": e["kind"], "service": e["service"], "n_seen": e["n_seen"],
            "positions_seen": sorted(set(e["positions"])),
            "typical_amount_sol": (round(sum(e["amounts_sol"]) / len(e["amounts_sol"]), 5) if e["amounts_sol"] else None),
        })
    summary_table.sort(key=lambda r: -r["n_seen"])

    n_pos1to3 = sum(1 for r in all_fast_rows if r["position_from_source"] in (1, 2, 3))
    n_pos1to3_known_bot = 0
    for r in all_fast_rows:
        if r["position_from_source"] not in (1, 2, 3):
            continue
        services = {t["service"] for t in r["transfers"]} | {p["service"] for p in r["other_program_ids"]}
        if any(s not in ("не опознан",) and not s.startswith("релей") for s in services):
            n_pos1to3_known_bot += 1
    share_known = round(n_pos1to3_known_bot / n_pos1to3, 4) if n_pos1to3 else None

    result = {
        "note": ("Только по уже построенным логам трассы (8 сделок пилота + CC), getTransaction "
                 "по уже известным подписям из этих логов -- новый скан/поиск не выполнялся."),
        "n_fast_buyer_rows": len(all_fast_rows),
        "fast_buyers": all_fast_rows,
        "summary_table_top": summary_table[:10],
        "n_positions_1_to_3_total": n_pos1to3,
        "n_positions_1_to_3_via_identified_public_bot": n_pos1to3_known_bot,
        "share_positions_1_to_3_via_identified_public_bot": share_known,
        "share_positions_1_to_3_unidentified": round(1 - share_known, 4) if share_known is not None else None,
    }
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"[fast_buyers] записано {OUT_PATH}, n_fast_buyer_rows={len(all_fast_rows)}", flush=True)
    print(json.dumps(summary_table[:10], ensure_ascii=False, indent=2), flush=True)
    print(f"доля позиций +1..+3 через опознанные публичные боты: {share_known}", flush=True)


if __name__ == "__main__":
    main()
