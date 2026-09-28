#!/usr/bin/env python3
"""Сверка analysis/podbivka_dlmm_quote.py на живых свопах Meteora DLMM (только чтение).

1. Пулы: DLMM-пулы из сегодняшних сделок источников (группы Code-1 и кандидаты,
   data/podbivka/istochniki_code1_kandidaty.json) -- счёт 0 инструкции swap/swap2
   программы LBUZ... (lb_pair).
2. По каждому пулу по кругу: читаем пул, расширение битовой карты и по 3 массива
   корзин в обе стороны одним getMultipleAccounts (слот чтения S), затем ищем первую
   успешную транзакцию пула со слотом > S. Если в ней ровно одна инструкция DLMM по
   этому пулу -- вход = прирост хранилища входа, факт выхода = убыль хранилища выхода
   (по балансам токенов транзакции), время = blockTime; котировка модулем на этот вход
   в прочитанном состоянии; сравнение amount_out.
3. Цель -- 10 совпавших по условиям свопов (или предел времени).
Выход: data/podbivka/dlmm_proverka.json, docs/podbivka_dlmm_proverka.md.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import podbivka_dlmm_quote as Q  # noqa: E402
import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent


def инструкции(tx: dict) -> list:
    msg = ((tx.get("transaction") or {}).get("message") or {})
    ixs = list(msg.get("instructions") or [])
    for гр in ((tx.get("meta") or {}).get("innerInstructions") or []):
        ixs += гр.get("instructions") or []
    return ixs


def dlmm_пулы_tx(tx: dict) -> list:
    return [ix["accounts"][0] for ix in инструкции(tx)
            if isinstance(ix, dict) and ix.get("programId") == Q.ПРОГРАММА and len(ix.get("accounts") or []) >= 11]


def пулы_источников(уз, кошельки: list, часов: float) -> dict:
    до_ts = time.time() - часов * 3600
    пулы: dict = {}
    for w in кошельки:
        try:
            сп = [з for з in уз.подписи(w, limit=300) if з.get("err") is None and (з.get("blockTime") or 0) >= до_ts]
        except RuntimeError:
            continue
        txs = уз.пакет([з["signature"] for з in сп])
        for з in сп:
            т = txs.get(з["signature"])
            for p in dlmm_пулы_tx(т) if т else []:
                пулы.setdefault(p, []).append({"кошелёк": w, "signature": з["signature"], "slot": з.get("slot")})
    return пулы


def прочитать(уз, пул: str) -> dict:
    r = уз.вызов("getMultipleAccounts", [[пул, Q.адрес_расширения(пул)], {"encoding": "base64", "commitment": "confirmed"}])
    val = (r or {}).get("value") or []
    if not val or not val[0]:
        raise Q.ОшибкаDLMM("пул не читается")
    lb = Q.разобрать_пул(base64.b64decode(val[0]["data"][0]))
    ext = Q.разобрать_расширение(base64.b64decode(val[1]["data"][0])) if len(val) > 1 and val[1] else None
    idx = sorted(set(Q.нужные_массивы(lb, True, 3, ext)) | set(Q.нужные_массивы(lb, False, 3, ext)))
    адр = [Q.адрес_массива(пул, i) for i in idx]
    r2 = уз.вызов("getMultipleAccounts", [[пул] + адр, {"encoding": "base64", "commitment": "confirmed"}])
    v2 = (r2 or {}).get("value") or []
    slot = ((r2 or {}).get("context") or {}).get("slot")
    lb = Q.разобрать_пул(base64.b64decode(v2[0]["data"][0]))       # пул и массивы -- из одного чтения
    массивы = {}
    for i, a in zip(idx, v2[1:]):
        if a:
            ba = Q.разобрать_массив(base64.b64decode(a["data"][0]))
            массивы[ba["index"]] = ba
    return {"lb": lb, "ext": ext, "массивы": массивы, "slot": slot}


def дельты(tx: dict, lb: dict) -> dict:
    ряды = {r["account"]: r for r in C.token_rows(tx).values()}
    rx, ry = ряды.get(lb["reserve_x"]), ряды.get(lb["reserve_y"])
    if not rx or not ry:
        return {}
    return {"dx": int(rx["post"]) - int(rx["pre"]), "dy": int(ry["post"]) - int(ry["pre"])}


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--chasov", type=float, default=24)
    р.add_argument("--cel", type=int, default=10)
    р.add_argument("--minut", type=float, default=40)
    а = р.parse_args()
    сп = json.loads((КОРЕНЬ / "data" / "podbivka" / "istochniki_code1_kandidaty.json").read_text(encoding="utf-8"))
    кошельки = sorted(set(сп["группы_code1"]) | set(сп["кандидаты"]))
    уз = S.Узел()
    with уз.на("helius"):
        пулы = пулы_источников(уз, кошельки, а.chasov)
        print("пулов DLMM у источников:", len(пулы), flush=True)
        итог, отказы = [], {}
        конец = time.time() + а.minut * 60
        очередь = sorted(пулы, key=lambda p: -len(пулы[p]))
        while time.time() < конец and len(итог) < а.cel and очередь:
            for пул in list(очередь):
                if len(итог) >= а.cel or time.time() >= конец:
                    break
                try:
                    ст = прочитать(уз, пул)
                except (Q.ОшибкаDLMM, RuntimeError) as exc:
                    отказы[пул] = S.чисто(str(exc))[:100]
                    очередь.remove(пул)
                    continue
                след = None
                for _ in range(20):                     # ждём следующую сделку пула до ~60 с
                    time.sleep(3)
                    зз = [з for з in уз.подписи(пул, limit=20) if (з.get("slot") or 0) > (ст["slot"] or 0) and з.get("err") is None]
                    if зз:
                        след = min(зз, key=lambda з: з["slot"])
                        break
                if not след:
                    continue
                т = уз.tx(след["signature"])
                if not т or dlmm_пулы_tx(т).count(пул) != 1:
                    continue
                д = дельты(т, ст["lb"])
                if not д or not ((д["dx"] > 0 > д["dy"]) or (д["dy"] > 0 > д["dx"])):
                    continue
                sfy = д["dx"] > 0
                вход, факт = (д["dx"], -д["dy"]) if sfy else (д["dy"], -д["dx"])
                try:
                    q = Q.котировка_точный_вход(ст["lb"], пул, вход, sfy, ст["массивы"], т.get("blockTime") or int(time.time()),
                                                ст["ext"])
                except Q.ОшибкаDLMM as exc:
                    отказы[пул] = f"котировка: {exc}"[:100]
                    continue
                отн = (q["amount_out"] - факт) / факт * 100 if факт else None
                итог.append({"пул": пул, "slot_чтения": ст["slot"], "сделка": след["signature"], "slot_сделки": т["slot"],
                             "swap_for_y": sfy, "вход": вход, "факт_выход": факт, "модель_выход": q["amount_out"],
                             "расхождение_пп": round(отн, 6) if отн is not None else None, "корзин": q["корзин"],
                             "подписант": (C.account_keys(т) or [None])[0], "сделки_источников_в_пуле": len(пулы[пул])})
                print(пул[:8], sfy, вход, факт, q["amount_out"], отн, flush=True)
    out = КОРЕНЬ / "data" / "podbivka" / "dlmm_proverka.json"
    out.write_text(json.dumps({"итог": итог, "отказы": отказы, "пулов": len(пулы),
                               "пулы": {p: v[:5] for p, v in пулы.items()}, "расход": уз.расход()},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    md = ["# Сверка модуля DLMM на живых свопах", "",
          f"Пулов DLMM в сделках источников за {а.chasov:g} ч: {len(пулы)}. Сверено свопов: {len(итог)}. "
          "Состояние пула -- чтение getMultipleAccounts (пул + массивы корзин в одном запросе, слот S); сделка -- первая "
          "успешная транзакция пула со слотом > S и одной инструкцией DLMM по пулу; вход и факт выхода -- изменения "
          "хранилищ пула в этой транзакции.", "",
          "| пул | слот чтения → сделки | направление | вход (сырые) | факт выхода | модель | расхождение, % | корзин |",
          "|---|---|---|---|---|---|---|---|"]
    for x in итог:
        md.append(f"| `{x['пул'][:8]}` | {x['slot_чтения']} → {x['slot_сделки']} | {'X→Y' if x['swap_for_y'] else 'Y→X'} | "
                  f"{x['вход']} | {x['факт_выход']} | {x['модель_выход']} | {x['расхождение_пп']} | {x['корзин']} |")
    точно = sum(1 for x in итог if x["модель_выход"] == x["факт_выход"])
    md += ["", f"Совпало до единицы: {точно} из {len(итог)}.", "", "Отказы: " +
           ("; ".join(f"`{p[:8]}` {r}" for p, r in отказы.items()) if отказы else "нет") + "."]
    (КОРЕНЬ / "docs" / "podbivka_dlmm_proverka.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    import podbivka_run as R  # noqa: PLC0415
    R.записано(out)
    R.записано(КОРЕНЬ / "docs" / "podbivka_dlmm_proverka.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
