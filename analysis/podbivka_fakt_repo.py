#!/usr/bin/env python3
"""Подбивка, п.2а: наши сделки Bloom/полосы -- ТОЛЬКО из выгрузок в репозитории.

На хост не ходим (слово владельца 26.09). Источники:
  * data/podbivka/kontrol_ceny.json -- 258 наших сделок с 24.09 (Bloom и полоса)
    с source_sig и нашей подписью; итога по SOL в этой выгрузке НЕТ;
  * data/podbivka/lane_table_raw_iz_vetki1.json -- 47 позиций полосы за 26.09
    (копия data/lane_table_raw.json ветки первой сессии) с closed_sol_net.
Адреса источника в выгрузках нет: берётся подписант-плательщик сделки
источника (данные с 24.09 -- узел Shyft). Выход в формате, который читает
podbivka_run.Факт: data/podbivka/nashi_sdelki_host.json.
"""
from __future__ import annotations

import calendar
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent


def ts(utc: str | None) -> float | None:
    return calendar.timegm(time.strptime(utc, "%Y-%m-%dT%H:%M:%SZ")) if utc else None


def main() -> int:
    сделки: dict = {}
    к = json.loads((КОРЕНЬ / "data" / "podbivka" / "kontrol_ceny.json").read_text(encoding="utf-8"))
    for r in к["ряды"] + к["отказы"]:
        if not r.get("source_sig"):
            continue
        сделки[r["cid"]] = {"client_order_id": r["cid"], "source_sig": r["source_sig"], "mint": r.get("mint"),
                            "lane": bool(r.get("lane")), "ts_intent": ts(r.get("ts")), "sol_in": r.get("sol_in"),
                            "closed_sol_net": None, "state": None, "выгрузка": "kontrol_ceny.json"}
    л = json.loads((КОРЕНЬ / "data" / "podbivka" / "lane_table_raw_iz_vetki1.json").read_text(encoding="utf-8"))
    for cid, v in л.items():
        сделки[cid] = {"client_order_id": cid, "source_sig": v.get("source_sig"), "mint": v.get("mint"),
                       "lane": True, "lane_group": v.get("lane_group"), "ts_intent": v.get("ts_intent"),
                       "sol_in": v.get("sol_in"), "closed_sol_net": v.get("closed_sol_net"),
                       "chain_ok": v.get("chain_ok"), "state": v.get("state"),
                       "выгрузка": "lane_table_raw (ветка первой сессии)"}
    уз = S.Узел()
    сиг = sorted({с["source_sig"] for с in сделки.values() if с.get("source_sig")})
    txs = уз.пакет(сиг)
    нет = 0
    for с in сделки.values():
        tx = txs.get(с["source_sig"])
        ключи = C.account_keys(tx) if tx else []
        с["source"] = ключи[0] if ключи else None
        нет += 0 if ключи else 1
    итог = {"откуда": "выгрузки в репозитории, без хоста", "сделок": len(сделки),
            "источник_не_восстановлен": нет, "сделки": list(сделки.values())}
    (КОРЕНЬ / "data" / "podbivka" / "nashi_sdelki_host.json").write_text(
        json.dumps(итог, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"факт из репозитория: сделок {len(сделки)}, с итогом по SOL "
          f"{sum(1 for с in сделки.values() if с['closed_sol_net'] is not None)}, источник не восстановлен {нет}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
