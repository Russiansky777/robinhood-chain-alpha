#!/usr/bin/env python3
"""Разовая: найти живой пример SellEvent на Pump.fun AMM
(pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA), чтобы проверить раскладку
полей по РЕАЛЬНОЙ транзакции -- BuyEvent уже реверс-инжинирен и сверен
с эталоном (20 WSOL -> 14 263 324.112826 CC), но для SellEvent пока
нет проверенного примера, а зеркальная раскладка полей -- только
предположение, не факт. Кандидаты -- прочие (не наши, не лидера)
сделки того же минта GtDZKAqvMZMnti46ZewMiXCa4oXF4bZxwQPoKzXPFxZn из уже
собранного лога (data/solana_entry_log_gtdzkaqvmz.json), для которых
старый decode_tx не нашёл своп-событие -- вероятная причина как раз
Pump.fun AMM."""
from __future__ import annotations

import base64
import hashlib
import json
import struct
import sys
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_pumpamm_sell_probe_result.json"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
BUY_DISC = hashlib.sha256(b"event:BuyEvent").digest()[:8]
SELL_DISC = hashlib.sha256(b"event:SellEvent").digest()[:8]

CANDIDATE_SIGS = [
    "6nkZPSmx12i95NDWFB2eLJkdpp7RTHyrDv6dKgwK1Wnr76VTyQbDkufgquuUMFUxxZVdVRTXPbuvpCFFnkEQLDV",
    "52AhfYv3eFJysrCLAAUmtnRwzRseWwT5AERGxojrmb8CdvcMdoJJ8j73A1ckBZ1a7TWihGJ9zTBT9cccj2irUNE1",
    "7V88QizrWV3KPqZaf72McrxoA7E9idA2JJxNev2hnH796Zv7nw1dZBvjLzdadhdFzSE1rG86FU6n67QZeYsFkgs",
    "4AZvTpfAPhmbeLLoDEXv25LZewNCUAfEPbavGE8mzh5iFdpY5KuHcsgoMZ6rs6jWG7GpSUgdUPGbWBuc8pzFpsbn",
    "4UskruPFaA2bt1CsTDJRk8r1w6g1a9zsGu1U1xG1G48UyZ5Fbq58VxgNf99KpSGdKSxTHaEUNbrT4izkJEu8ZdAz",
    "tUgK6peXW9gy2WTSPVG9jar1LErHxYNEeXaFBCGgG8uCgzp37vVy1Bu9taAqTQ89prFCRfjDgVsGNn4bZKwC3id",
    "4UcS762RbZ9BWFFpQHWkmtMyXUNMMsAB4ANR5uT2JceuCNMyemYrB5LeoE4NmS84NNf2GiC9Hdb6NdUX9oUXcmvT",
    "RM7rPEEZSVuKxTZyrTX39SSvMPvMGNQPBi7jEbeTF4ueHkDTXPDY1iQtxoQydWeGY6Sqv968GiFWLg9BsYGqmGa",
    "fTcV2V14MPmZjMBjF6SfBRm2FErfckTCLaD1fYHGb5RpuvLLfZGPhkoymVrtzyKZxxXBVegfQYWdTCK6cXt8QRJ",
    "r24hcaicLckQ3uddm99VVpjUmfLJep1ssERkC2wKNBQd21Nq9jznyiCz9oA1wPdWRpf5E2udY443RoMC3JKidu3",
    "4fXbc1svEUPe9KQzWt6fa1xJH2C4aoHZvYGfjBnox5pCjrQDieK4gBsyHfDXQFffVegRjVNw9sXRMhaZgRJfpa3L",
    "57zbi9rKRsejTJXUbsbU3txQj1siz4ugjhsLu4u4g4sosopJ9mi2oAqowgRbz7JXVCFCy8gXmgmH7zYEHdXozK59",
    "65A3VoDv1UTcE3BkFcJ6g31FGp79YGkEvVk4LLkyQ5GftzMSAn2iKpPrhBinEcReyipuSySHnzc6G9hoDnbcVcPE",
    "DQFprnqiKrKTZX7sRmJqnBJqA4fMyLutEus1nFZJJdhwLBWiCKGbguZksPTMi41TwAr5o5P9QV5AcvDtdw898U3",
    "4oA136iFHtBVAQULtw8LjcPUUAeebUJtJuAV42m1R2rZ4Y6Lp55qFWGnDN8AV3SmZXRVzrSBwgDsW5dkXM8xninx",
]


def b58decode(s: str) -> bytes:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    num = 0
    for ch in s:
        num = num * 58 + alphabet.index(ch)
    combined = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    n_pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * n_pad + combined


def b58encode(b: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    num = int.from_bytes(b, "big")
    out = ""
    while num > 0:
        num, rem = divmod(num, 58)
        out = alphabet[rem] + out
    n_pad = len(b) - len(b.lstrip(b"\x00"))
    return "1" * n_pad + out


def probe_tx(sig: str) -> dict:
    tx = fp.get_transaction(sig)
    if tx is None:
        return {"signature": sig, "HONEST_ANSWER": "getTransaction вернул null"}
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return {"signature": sig, "HONEST_ANSWER": "транзакция с ошибкой (err != null), пропущена"}
    logs = meta.get("logMessages") or []
    program_data = [ln for ln in logs if ln.startswith("Program data: ")]
    events = []
    for ln in program_data:
        raw = base64.b64decode(ln[14:])
        disc = raw[:8]
        if disc == BUY_DISC:
            kind = "BuyEvent"
        elif disc == SELL_DISC:
            kind = "SellEvent"
        else:
            continue
        ts, f1, f2 = struct.unpack_from("<qQQ", raw, 8)
        pool = b58encode(raw[120:152]) if len(raw) >= 152 else None
        user = b58encode(raw[152:184]) if len(raw) >= 184 else None
        events.append({"kind": kind, "raw_len": len(raw), "timestamp": ts,
                        "field_at_16": f1, "field_at_24": f2,
                        "pool_at_120": pool, "user_at_152": user})
    instr_logs = [ln for ln in logs if "Instruction:" in ln]
    return {"signature": sig, "slot": tx["slot"], "instruction_logs": instr_logs,
            "pump_amm_events": events,
            "all_top_level_program_ids": sorted({ix.get("programId") for ix in tx["transaction"]["message"]["instructions"] if isinstance(ix, dict)})}


def main() -> None:
    results = [probe_tx(sig) for sig in CANDIDATE_SIGS]
    n_sell = sum(1 for r in results for e in r.get("pump_amm_events", []) if e["kind"] == "SellEvent")
    out = {"n_candidates": len(CANDIDATE_SIGS), "n_sell_events_found": n_sell, "results": results}
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"n_candidates={len(CANDIDATE_SIGS)} n_sell_events_found={n_sell}", flush=True)
    for r in results:
        print(r.get("signature", "?")[:12], r.get("instruction_logs"), [e["kind"] for e in r.get("pump_amm_events", [])])


if __name__ == "__main__":
    main()
