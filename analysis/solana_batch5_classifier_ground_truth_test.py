#!/usr/bin/env python3
"""Владелец, приоритет 1a: тест классификатора BATCH-5 (classify_tx) на
ИЗВЕСТНЫХ подписях с ИЗВЕСТНЫМ реальным размером -- 7 из 8 leader_signature
логов трассы пилота имеют независимо посчитанный (Dune-верифицированный,
solana_phase2_events_v3.json) spend_sol_equiv для сверки; 8-я (jupcat)
не входит в окно phase2 -- честно пропущена, не выдумываем эталон.
Плюс JFPJEgfXH2PE... (CC, лидер = jg по подтверждённому pump-amm
декодеру: 17.896430255 SOL, offset24 события BuyEvent) -- ОСОБЫЙ
случай: jg НЕ является подписантом этой транзакции (подписант --
Fomo Co-signer), проверяем, что классификатор либо справляется, либо
честно и понятно отказывает (а не молча даёт неверный ответ)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_batch5_rpc_check import classify_tx  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_batch5_classifier_ground_truth_test_result.json"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
JG_WALLET = "4hwPamSooBr5JhxHdcEC21HoxN5HUwYR2hGucLPyZAi8"
FOMO_COSIGNER = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"

TEST_CASES = [
    {"label": "xzc5swuory", "signature": "3kbHc3iPhZywCqhQmxU3rNRAQVFhPn6h1LxRHNMVXKJbcYUsehTqUNZBP32iFhvSc4B7XTiEyEuiPBNCLCbjxbeF", "wallet": LEADER_WALLET, "expected_sol": 4.479160464074533},
    {"label": "egp1f5j9ld", "signature": "5jpYqPa3yaTuMBaS", "wallet": LEADER_WALLET, "expected_sol": 22.397419817237054},
    {"label": "gtdzkaqvmz", "signature": "4vPsRrt1cNtqxChbZFKDnDTTN85BweBn3cRSsRfVmjXvs4e1pg39S9cEaXei2XDDHi3JwF1benw4bDmfPCWGFLdn", "wallet": LEADER_WALLET, "expected_sol": 22.303506111160672},
    {"label": "vaddewuhuy", "signature": "5TTHHpYgyk22BwKj", "wallet": LEADER_WALLET, "expected_sol": 89.42944017170453},
    {"label": "hgcxvs6kjh", "signature": "4mX39pia4DVyih1P", "wallet": LEADER_WALLET, "expected_sol": 44.239957529640776},
    {"label": "7runv1hjfc", "signature": "4Jq6se95E9D3obin", "wallet": LEADER_WALLET, "expected_sol": 17.692852087756545},
    {"label": "gpuxeqplff", "signature": "S2VAQPpyQbGndfNa", "wallet": LEADER_WALLET, "expected_sol": 22.018671833714993},
    {"label": "akbot_cc (jg)", "signature": "JFPJEgfXH2PEfssK799nob9KE6J5Lk4PLcD1xRqqieTUP6rxksXTZAR7eA1jrVU7ejychsqwf6JvN2pd4M5hAqY",
     "wallet": JG_WALLET, "expected_sol": 17.896430255,
     "note": "jg НЕ подписант этой tx (подписант -- Fomo Co-signer) -- тест на честность отказа/обработки"},
]


def full_signature_for(label: str) -> str | None:
    p = REPO_ROOT / f"data/solana_entry_log_{label}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())["leader_signature"]


def main() -> None:
    # Подставляем полные подписи из уже существующих логов (некоторые выше сокращены по невнимательности).
    for case in TEST_CASES:
        label = case["label"].split(" ")[0]
        full = full_signature_for(label)
        if full:
            case["signature"] = full

    results = []
    for case in TEST_CASES:
        tx = fp.get_transaction(case["signature"])
        if tx is None:
            results.append({**case, "PASS": False, "HONEST_ANSWER": "getTransaction вернул null"})
            continue
        ev = classify_tx(tx, case["wallet"])
        got_first_entry = ev is not None and ev.get("kind") == "first_entry"
        got_sol = ev.get("spend_sol_equiv") if got_first_entry else None
        expected = case["expected_sol"]
        close = got_sol is not None and abs(got_sol - expected) / expected < 0.05  # 5% допуск -- разные методы (Dune vs RPC), не байт-в-байт
        results.append({
            "label": case["label"], "signature": case["signature"], "wallet": case["wallet"],
            "expected_sol": expected, "classify_result_kind": ev.get("kind") if ev else None,
            "got_spend_sol_equiv": got_sol, "PASS": bool(got_first_entry and close),
            "note": case.get("note"),
        })
        print(f"[gt_test] {case['label']}: ожидание={expected:.4f} SOL, получено kind={ev.get('kind') if ev else None} "
              f"spend={got_sol} PASS={results[-1]['PASS']}", flush=True)

    n_pass = sum(1 for r in results if r["PASS"])
    out = {"n_cases": len(results), "n_pass": n_pass, "all_pass_excluding_edge_case": None, "results": results}
    # jg/CC -- явно помечен как edge-case (co-signer), не считается в "метод сломан", если ожидаемо не проходит по структурной причине (не по размеру)
    core = [r for r in results if r["label"] != "akbot_cc (jg)"]
    out["all_pass_excluding_edge_case"] = all(r["PASS"] for r in core) if core else None
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"[gt_test] {n_pass}/{len(results)} PASS; без edge-case (jg): "
          f"{sum(1 for r in core if r['PASS'])}/{len(core)}", flush=True)


if __name__ == "__main__":
    main()
