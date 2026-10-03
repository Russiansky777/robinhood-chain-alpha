#!/usr/bin/env python3
"""Постоянный расход сделки ПО ЦЕПИ, обе подписи: чаевые и комиссии покупки и продажи.

ЗАЧЕМ. В таблице масштаба есть столбец «сигнал до постоянных расходов» -- итог плюс
постоянный расход. Чаевые покупки лежат в выгрузке (`чаевые_sol`), комиссии обеих
подписей -- в `итог_po_cepi_chasti`, а вот платит ли полоса чаевые НА ПРОДАЖЕ, по
выгрузке не видно: такой перевод сидел бы внутри нативной дельты продажи. Здесь это
читается с цепи: переводы с нашего кошелька на известные счета чаевых
(data/senders.json, поле tip_accounts) в транзакции продажи и в транзакции покупки.

Выход: data/podbivka/rashod_prodazhi.json -- по сделке: комиссия, чаевые по счетам
сервисов, их сумма; сводка по билетам.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
ЛАМП = 1_000_000_000


def счета_чаевых() -> dict:
    д = json.loads((КОРЕНЬ / "data" / "senders.json").read_text(encoding="utf-8"))
    из_ = {}
    for имя, в in (д.get("senders") or {}).items():
        for с in (в.get("tip_accounts") or []):
            из_[с] = имя
    return из_


def сделки() -> list:
    """Сделки выгрузок, одна строка на cid (выгрузки перекрываются)."""
    по_cid: dict = {}
    for f in sorted((П / "sdelki").glob("sdelki_polosy_*.json")):
        for r in json.loads(f.read_text(encoding="utf-8")).get("ряды") or []:
            if r.get("buy_sig") and r.get("sell_sig"):
                по_cid[r["cid"]] = r
    return list(по_cid.values())


def разбор(тх: dict, кошельки: set, чаевые_счета: dict) -> dict:
    """Комиссия транзакции и переводы на счета чаевых (по дельтам балансов)."""
    из_ = {"ok": False, "комиссия_sol": None, "чаевые_sol": 0.0, "по_сервисам": {},
           "наш_натив_sol": None, "why_not": None}
    мета = (тх or {}).get("meta") or {}
    кл = ((тх or {}).get("transaction") or {}).get("accountKeys")
    pre, post = мета.get("preBalances"), мета.get("postBalances")
    if not кл or not pre or not post or len(pre) != len(post) or len(кл) != len(pre):
        из_["why_not"] = "в транзакции нет балансов или ключей"
        return из_
    из_["комиссия_sol"] = round(int(мета.get("fee") or 0) / ЛАМП, 9)
    наш = 0
    for i, k in enumerate(кл):
        адр = k.get("pubkey") if isinstance(k, dict) else k
        д = int(post[i]) - int(pre[i])
        if адр in кошельки:
            наш += д
        имя = чаевые_счета.get(адр)
        if имя and д > 0:
            из_["чаевые_sol"] = round(из_["чаевые_sol"] + д / ЛАМП, 9)
            из_["по_сервисам"][имя] = round(из_["по_сервисам"].get(имя, 0.0) + д / ЛАМП, 9)
    из_.update(ok=True, наш_натив_sol=round(наш / ЛАМП, 9))
    return из_


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    import podbivka_sim as S  # noqa: PLC0415
    р = argparse.ArgumentParser()
    р.add_argument("--predel", type=int, default=0)
    а = р.parse_args()
    сд = сделки()
    if а.predel:
        сд = сд[:а.predel]
    чс = счета_чаевых()
    кошельки = {r.get("wallet") for r in сд if r.get("wallet")} | {
        "4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x",
        "21DqHDDPEfMhK1dHRkV9E8v8KTTSKQGApJAr1irC9j7w"}
    уз = S.Узел()
    из_, счёт = [], {"сделок": len(сд), "прочитано": 0, "не_отдано": 0, "счетов_чаевых": len(чс)}
    with уз.на("helius"):
        for r in сд:
            строка = {"cid": r["cid"], "группа": r.get("group"), "билет_sol": r.get("size_sol"),
                      "sol_in": r.get("sol_in"), "источник": r.get("source"),
                      "итог_po_cepi_sol": r.get("итог_po_cepi_sol")}
            пак = уз.пакет([r["buy_sig"], r["sell_sig"]])
            for роль, п in (("покупка", r["buy_sig"]), ("продажа", r["sell_sig"])):
                тх = пак.get(п)
                if not тх:
                    строка[роль] = {"ok": False, "why_not": "транзакция не отдана узлом"}
                    continue
                строка[роль] = разбор(тх, кошельки, чс)
            if (строка.get("покупка") or {}).get("ok") and (строка.get("продажа") or {}).get("ok"):
                счёт["прочитано"] += 1
                строка["расход_всего_sol"] = round(
                    строка["покупка"]["комиссия_sol"] + строка["покупка"]["чаевые_sol"]
                    + строка["продажа"]["комиссия_sol"] + строка["продажа"]["чаевые_sol"], 9)
            else:
                счёт["не_отдано"] += 1
            из_.append(строка)
    out = П / "rashod_prodazhi.json"
    out.write_text(json.dumps({"счёт": счёт, "вызовов": уз.вызовов, "сделки": из_},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(out)
    гот = [x for x in из_ if x.get("расход_всего_sol") is not None]
    прод = [x["продажа"]["чаевые_sol"] for x in гот]
    print(out.name, "сделок", len(из_), "прочитано", счёт["прочитано"],
          "чаевые на продаже > 0:", sum(1 for v in прод if v > 0), "из", len(прод),
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
