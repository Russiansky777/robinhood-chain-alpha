#!/usr/bin/env python3
"""Подбивка: 54uaRuJE -- слот создания минта относительно его покупки (кандидат «общего триггера»).

Для каждой из 279 покупок: самая старая подпись минта (getSignaturesForAddress до его покупки, до
10 страниц по 1000) -- слот и индекс в блоке (Helius; Shyft getBlock отдаёт блок без транзакций v1).
Если листание не дошло до конца -- «старше», создание не найдено. Только чтение.
Выход: data/podbivka/snaipery/54ua_trigger.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_sim as S  # noqa: E402
import podbivka_snaipery_svod as SV  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka" / "snaipery"
КОШ = "54uaRuJEc9BHY7uVMXtcf9JWcYDtB75hUeFJWeCxEkBE"


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    д = json.loads((П / f"{КОШ}_168ч.json").read_text(encoding="utf-8"))
    сд, _ = SV.поправить(д["сделки"])
    пок = [с for с in сд if с["сторона"] == "buy" and not с.get("многоминтовая")]
    уз = S.Узел()
    рез = []
    with уз.на("helius"):
        for n, с in enumerate(пок):
            з = {"signature": с["signature"], "slot": с["slot"], "mint": с["mint"]}
            try:
                до, старейшая, страниц, конец = с["signature"], None, 0, False
                while страниц < 10:
                    стр = уз.подписи(с["mint"], до=до, limit=1000)
                    страниц += 1
                    if not стр:
                        конец = True
                        break
                    старейшая = стр[-1]
                    if len(стр) < 1000:
                        конец = True
                        break
                    до = стр[-1]["signature"]
                з.update(страниц=страниц, дошли_до_начала=конец)
                if старейшая and конец:
                    з.update(слот_создания=старейшая.get("slot"), подпись_создания=старейшая["signature"])
                    бл = уз.вызов("getBlock", [старейшая["slot"], {"transactionDetails": "signatures", "rewards": False,
                                                                   "maxSupportedTransactionVersion": 1,
                                                                   "commitment": "confirmed"}], срок=40.0)
                    сп = (бл or {}).get("signatures") or []
                    з["индекс_создания"] = сп.index(старейшая["signature"]) if старейшая["signature"] in сп else None
                elif not старейшая:
                    з["why_not"] = "до его покупки подписей минта нет"
            except RuntimeError as exc:
                з["why_not"] = S.чисто(str(exc))[:160]
            рез.append(з)
            if n % 25 == 0:
                print(f"{n}/{len(пок)}", flush=True)
    вых = П / "54ua_trigger.json"
    вых.write_text(json.dumps({"записи": рез, "расход": уз.расход()}, ensure_ascii=False, default=str), encoding="utf-8")
    R.записано(вых)
    R.пуш("Podbivka-2: 54uaRuJE slot sozdaniya minta [automated]", [str(вых)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
