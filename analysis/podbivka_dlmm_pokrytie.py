#!/usr/bin/env python3
"""Подбивка: DLMM для Code-1 -- покрывают ли массивы корзин из транзакции источника (±1 сосед) активную корзину
через 1 / 2 / 3 слота; и разбор необъяснённого свопа 4pSQ2zbz.

1. Покупки источников в DLMM за 28.09 06Z → 29.09 06Z -- из архива (data/podbivka/arhiv_den/den_2026-09-28T06.json.gz,
   покупки_ист, pool meteora-dlmm), до --n штук, не больше 3 на источника.
   Транзакция источника: событие Swap/Swap2Evt по пулу от источника (start/end корзина), массивы корзин среди
   счетов транзакции (адреса PDA bin_array для индексов вокруг start). Покрытие = индексы массивов ±1.
   Активная корзина на конец слота s+k (k = 1, 2, 3) -- end_bin последнего успешного свопа пула в слотах (s, s+k]
   (подписи пула между сделкой источника и блоком s+4); если свопов не было -- end_bin источника.
2. Своп 4pSQ2zbz (сверка v6): полный разбор Swap2Evt (limit_order_fee, amount_left) и текущее содержимое корзины 34
   (исторического состояния на слот чтения RPC не отдаёт -- только сейчас; флаг).
Только чтение, Helius. Выход: data/podbivka/dlmm_pokrytie.json.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_dlmm_proverka as V  # noqa: E402
import podbivka_dlmm_quote as Q  # noqa: E402
import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent


def счета(tx: dict) -> list:
    msg = (tx.get("transaction") or {}).get("message") or {}
    ks = [k.get("pubkey") if isinstance(k, dict) else k for k in (msg.get("accountKeys") or [])]
    la = (tx.get("meta") or {}).get("loadedAddresses") or {}
    return ks + list(la.get("writable") or []) + list(la.get("readonly") or [])


def swap2_полный(tx: dict, пул: str) -> list:
    import base64 as b64  # noqa: PLC0415
    из_ = []
    for ix in V.инструкции(tx):
        if isinstance(ix, dict) and ix.get("programId") == Q.ПРОГРАММА and ix.get("data"):
            try:
                b = V._b58(ix["data"])  # noqa: SLF001
            except Exception:  # noqa: BLE001
                continue
            if b[:8] == V.EVENT_IX_TAG and b[8:16] == V.DISC_SWAP2:
                d = b[16:]
                if len(d) >= 146 and Q._pk(d[0:32]) == пул:  # noqa: SLF001
                    start, end = struct.unpack_from("<ii", d, 64)
                    a_in, a_left, a_out, mm, prot, lo, host = struct.unpack_from("<QQQQQQQ", d, 89)
                    из_.append({"start": start, "end": end, "swap_for_y": d[72] != 0, "amount_in": a_in, "amount_left": a_left,
                                "amount_out": a_out, "mm_fee": mm, "protocol_fee": prot, "limit_order_fee": lo,
                                "host_fee": host, "хвост_hex": d[145:].hex()})
    return из_


def одна(уз, п: dict) -> dict:
    ист, sig, s = п["trader"], п["signature"], п["block"]
    т = уз.tx(sig)
    if not т:
        return {"why_not": "нет транзакции источника"}
    ев = [e for e in V.события_swap(т) if e.get("from") == ист]
    if not ев:
        ев = V.события_swap(т)
    if not ев:
        return {"why_not": "нет события Swap в транзакции источника"}
    e = ев[0]
    пул = e["lb_pair"]
    вс = set(счета(т))
    i0 = Q.индекс_массива(e["start"])
    массивы = sorted(i for i in range(i0 - 12, i0 + 13) if Q.адрес_массива(пул, i) in вс)
    покрытие = {j for i in массивы for j in (i - 1, i, i + 1)}
    опора = S.подпись_после_слота(уз, s + 4)
    стр = уз.подписи(пул, до=опора, по=sig, limit=1000) if опора else []
    стр = [з for з in стр if з.get("err") is None and s < (з.get("slot") or 0) <= s + 3]
    txs = уз.пакет([з["signature"] for з in стр]) if стр else {}
    свопы = []                       # (слот, порядок в выдаче: новые первыми → больше = раньше)
    for n, з in enumerate(стр):
        тт = txs.get(з["signature"])
        ев_т = [ee for ee in (V.события_swap(тт) if тт else []) if ee["lb_pair"] == пул]
        ев_т = [ee for ee in ев_т if ee.get("канал") == "cpi"] or ев_т
        if ев_т:
            свопы.append((з["slot"], -n, ев_т[-1]["end"]))     # последний своп пула в транзакции
    свопы.sort()
    рез = {"источник": ист, "подпись": sig, "слот": s, "пул": пул, "start": e["start"], "end": e["end"],
           "массивы_в_tx": массивы, "свопов_после": len(свопы), "по_слотам": {}}
    for k in (1, 2, 3):
        до_k = [x for x in свопы if x[0] <= s + k]
        акт = до_k[-1][2] if до_k else e["end"]
        рез["по_слотам"][str(k)] = {"активная": акт, "массив": Q.индекс_массива(акт),
                                    "покрыта": Q.индекс_массива(акт) in покрытие, "покрыта_без_соседей": Q.индекс_массива(акт) in set(массивы)}
    return рез


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    р = argparse.ArgumentParser()
    р.add_argument("--n", type=int, default=40)
    р.add_argument("--tolko-svop", action="store_true", help="только разбор свопа 4pSQ2zbz (часть 2)")
    а = р.parse_args()
    д = json.loads(gzip.decompress((КОРЕНЬ / "data" / "podbivka" / "arhiv_den" / "den_2026-09-28T06.json.gz").read_bytes()))
    пок, по_ист, видел = [], {}, set()
    for п in sorted(д.get("покупки_ист") or [], key=lambda x: x.get("timestamp") or 0):
        if п.get("pool") != "meteora-dlmm" or п["signature"] in видел or по_ист.get(п["trader"], 0) >= 3:
            continue
        видел.add(п["signature"])
        по_ист[п["trader"]] = по_ист.get(п["trader"], 0) + 1
        пок.append(п)
        if len(пок) >= а.n:
            break
    уз = S.Узел()
    рез = []
    with уз.на("helius"):
        for п in ([] if а.tolko_svop else пок):
            try:
                рез.append(одна(уз, п))
            except (RuntimeError, Q.ОшибкаDLMM) as exc:
                рез.append({"подпись": п["signature"], "why_not": S.чисто(str(exc))[:160]})
        # 2. своп 4pSQ2zbz
        мн = json.loads((КОРЕНЬ / "data" / "podbivka" / "dlmm_proverka_mnogo4.json").read_text(encoding="utf-8"))
        x = next((x for x in мн["итог"] if x["пул"].startswith("4pSQ2zbz") and x["swap_for_y"] is False
                  and (x.get("расхождение_пп") or 0) < -1), None)          # необъяснённый: −2.74 %, 1 корзина против 5
        разбор = {}
        if x:
            т = уз.tx(x["сделка"])
            разбор["свап2"] = swap2_полный(т, x["пул"]) if т else None
            idx = Q.индекс_массива(34)
            r = уз.вызов("getMultipleAccounts", [[x["пул"], Q.адрес_массива(x["пул"], idx)], {"encoding": "base64"}])
            val = (r or {}).get("value") or []
            if len(val) > 1 and val[1]:
                ba = Q.разобрать_массив(base64.b64decode(val[1]["data"][0]))
                lo, _ = Q.границы_массива(idx)
                разбор["корзина_34_сейчас"] = ba["bins"][34 - lo] | {"price": str(ba["bins"][34 - lo]["price"])}
            if val and val[0]:
                lb = Q.разобрать_пул(base64.b64decode(val[0]["data"][0]))
                разбор["пул_сейчас"] = {"active_id": lb.get("active_id"), "function_type": lb["parameters"].get("function_type"),
                                       "collect_fee_mode": lb["parameters"].get("collect_fee_mode"),
                                       "поддержка_лимитных": Q.support_limit_order(lb)}
            разбор["сделка"] = x["сделка"]
            разбор["слот_чтения"] = x["slot_чтения"]
    вых = КОРЕНЬ / "data" / "podbivka" / ("dlmm_4pSQ2zbz.json" if а.tolko_svop else "dlmm_pokrytie.json")
    вых.write_text(json.dumps({"покупки": рез, "своп_4pSQ2zbz": разбор, "расход": уз.расход()}, ensure_ascii=False,
                              default=str), encoding="utf-8")
    R.записано(вых)
    R.пуш("Podbivka-2: DLMM pokrytie massivov korzin [automated]", [str(вых)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
