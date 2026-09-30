#!/usr/bin/env python3
"""DLMM «без чтений»: для 46 сверенных свопов (data/podbivka/dlmm_proverka_mnogo4.json, 38/46 до единицы) -- событие
последнего успешного свопа того же пула ПЕРЕД сделкой (история транзакций пула), сама сделка целиком и постоянные поля
пула (сейчас: bin_step, параметры комиссии -- не меняются от сделки к сделке). Только чтение, Helius.

По этим данным офлайн (analysis/podbivka_dlmm_bez_chteniy.py) считается котировка без getMultipleAccounts: корзина --
end_bin_id предыдущего события, ставка комиссии -- его fee_bps, одна корзина (содержимого корзин строитель не знает).
Выход: data/podbivka/dlmm_bez_chteniy_sbor.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_dlmm_proverka as P  # noqa: E402
import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent


def main() -> int:
    сверка = json.loads((КОРЕНЬ / "data" / "podbivka" / "dlmm_proverka_mnogo4.json").read_text(encoding="utf-8"))["итог"]
    сп = json.loads((КОРЕНЬ / "data" / "podbivka" / "istochniki_code1_kandidaty.json").read_text(encoding="utf-8"))
    источники = set(сп["группы_code1"]) | set(сп["кандидаты"]) | set(сп.get("снайперы") or [])
    уз = S.Узел()
    out = КОРЕНЬ / "data" / "podbivka" / "dlmm_bez_chteniy_sbor.json"
    import podbivka_run as R  # noqa: PLC0415
    ряды, пулы = [], {}
    with уз.на("helius"):
        for x in сверка:
            пул, сделка = x["пул"], x["сделка"]
            р: dict = {"пул": пул, "сделка": сделка, "slot_сделки": x["slot_сделки"], "повод": x["повод"]}
            try:
                р["транзакция"] = уз.tx(сделка)
                до = [з for з in уз.подписи(пул, до=сделка, limit=100) if з.get("err") is None]
                пред = None
                for i in range(0, len(до), 50):
                    txs = уз.пакет([з["signature"] for з in до[i:i + 50]])
                    for з in до[i:i + 50]:                   # от новых к старым
                        т = txs.get(з["signature"])
                        ев = [e for e in P.события_swap(т) if e["lb_pair"] == пул] if т else []
                        if ев:
                            пред = {"signature": з["signature"], "slot": т.get("slot"), "blockTime": т.get("blockTime"),
                                    "события": ев, "подписант_источник": any(e.get("from") in источники for e in ев),
                                    "транзакций_между": до.index(з)}
                            break
                    if пред:
                        break
                р["предыдущий_своп"] = пред
                if not пред:
                    р["why_not"] = f"нет свопа пула среди {len(до)} успешных транзакций до сделки"
                if пул not in пулы:
                    v = уз.вызов("getAccountInfo", [пул, {"encoding": "base64", "commitment": "confirmed"}])
                    пулы[пул] = ((v or {}).get("value") or {}).get("data", [None])[0]
            except RuntimeError as exc:
                р["why_not"] = S.чисто(str(exc))[:160]
            ряды.append(р)
            print(пул[:8], сделка[:8], (р.get("предыдущий_своп") or {}).get("slot"), р.get("why_not"), flush=True)
    out.write_text(json.dumps({"что": "46 свопов сверки DLMM v6: предыдущий своп пула, сделка, пул (base64, прочитан при сборе -- для постоянных полей)",
                               "ряды": ряды, "пулы_сейчас": пулы, "расход": уз.расход()}, ensure_ascii=False), encoding="utf-8")
    R.записано(out)
    R.пуш("Podbivka-2: DLMM bez chteniy -- predydushchie svopy 46 sdelok [automated]", [str(out)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
