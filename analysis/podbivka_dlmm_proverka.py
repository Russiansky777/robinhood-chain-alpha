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


EVENT_IX_TAG = bytes.fromhex("e445a52e51cb9a1d")
DISC_SWAP = bytes([81, 108, 227, 190, 205, 208, 10, 196])
DISC_SWAP2 = bytes([46, 116, 82, 215, 148, 27, 84, 77])
DISC_IX = {bytes([248, 198, 158, 145, 225, 117, 135, 200]): "swap", bytes([65, 75, 63, 76, 235, 91, 91, 136]): "swap2"}


_АЛФ = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58(s_: str) -> bytes:
    n = 0
    for ch in s_:
        n = n * 58 + _АЛФ.index(ch)
    сырые = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s_) - len(s_.lstrip("1"))) + сырые


def _разбор_события(b: bytes) -> dict | None:
    import struct  # noqa: PLC0415
    if b[:8] == DISC_SWAP:
        d = b[8:]
        if len(d) < 129:              # обрезанный лог
            return None
        lb, fr = Q._pk(d[0:32]), Q._pk(d[32:64])
        start, end = struct.unpack_from("<ii", d, 64)
        a_in, a_out = struct.unpack_from("<QQ", d, 72)
        sfy = d[88] != 0
        fee, prot = struct.unpack_from("<QQ", d, 89)
        host = struct.unpack_from("<Q", d, 121)[0]
        return {"вид": "Swap", "lb_pair": lb, "from": fr, "start": start, "end": end, "amount_in": a_in, "amount_out": a_out,
                "swap_for_y": sfy, "fee": fee, "protocol_fee": prot, "host_fee": host}
    if b[:8] == DISC_SWAP2:
        d = b[8:]
        if len(d) < 147:
            return None
        lb, fr = Q._pk(d[0:32]), Q._pk(d[32:64])
        start, end = struct.unpack_from("<ii", d, 64)
        sfy = d[72] != 0
        a_in, a_left, a_out, mm_fee, prot, lo_fee, host = struct.unpack_from("<QQQQQQQ", d, 89)
        return {"вид": "Swap2Evt", "lb_pair": lb, "from": fr, "start": start, "end": end, "amount_in": a_in,
                "amount_left": a_left, "amount_out": a_out, "swap_for_y": sfy, "fee": mm_fee + lo_fee, "protocol_fee": prot,
                "host_fee": host, "fees_on_input": d[145] != 0}
    return None


def события_swap(tx: dict) -> list:
    """События Swap / Swap2Evt: самовызов программы (emit_cpi) и «Program data:» в логах."""
    import base64 as b64  # noqa: PLC0415
    из_, видел = [], set()
    for ix in инструкции(tx):
        if isinstance(ix, dict) and ix.get("programId") == Q.ПРОГРАММА and ix.get("data"):
            try:
                b = _b58(ix["data"])
            except Exception:  # noqa: BLE001
                continue
            if b[:8] == EVENT_IX_TAG:
                e = _разбор_события(b[8:])
                if e:
                    из_.append(e)
                    видел.add((e["start"], e["amount_in"], e["amount_out"]))
    for л in ((tx.get("meta") or {}).get("logMessages") or []):
        if л.startswith("Program data: "):
            try:
                e = _разбор_события(b64.b64decode(л[14:]))
            except Exception:  # noqa: BLE001
                continue
            if e and (e["start"], e["amount_in"], e["amount_out"]) not in видел:
                из_.append(e)
    return из_


def вид_инструкции(tx: dict, пул: str) -> list:
    out = []
    for ix in инструкции(tx):
        if isinstance(ix, dict) and ix.get("programId") == Q.ПРОГРАММА and (ix.get("accounts") or [None])[0] == пул:
            try:
                out.append(DISC_IX.get(_b58(ix["data"])[:8], "другая"))
            except Exception:  # noqa: BLE001
                out.append("?")
    return out


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


def сверить(уз, пул: str, ст: dict, повод: str, пулы: dict) -> dict | None:
    """Первая успешная транзакция пула со слотом > S; одна инструкция swap/swap2 и одно событие по пулу."""
    # первая УСПЕШНАЯ транзакция пула после чтения: упавшие состояние пула не меняют (как в v1)
    след = None
    for _ in range(25):
        time.sleep(2)
        зз = [з for з in уз.подписи(пул, limit=50) if (з.get("slot") or 0) > (ст["slot"] or 0) and з.get("err") is None]
        if зз:
            след = min(зз, key=lambda з: з["slot"])
            break
    if not след:
        return {"отсев": "нет успешной транзакции за 50 с"}
    т = уз.tx(след["signature"])
    if not т:
        return {"отсев": "транзакция не получена"}
    ев = [e for e in события_swap(т) if e["lb_pair"] == пул]
    виды = вид_инструкции(т, пул)
    if not виды:
        return {"отсев": "не swap: инструкции пула нет (ликвидность/иное)"}
    if len(виды) != 1 or виды[0] not in ("swap", "swap2"):
        return {"отсев": f"инструкций пула {len(виды)}: {','.join(sorted(set(виды)))}"}
    if len(ев) != 1:
        return {"отсев": f"событий Swap по пулу {len(ев)}"}
    e = ев[0]
    д = дельты(т, ст["lb"])
    try:
        q = Q.котировка_точный_вход(ст["lb"], пул, e["amount_in"], e["swap_for_y"], ст["массивы"],
                                    т.get("blockTime") or int(time.time()), ст["ext"])
        why = None
    except Q.ОшибкаDLMM as exc:
        q, why = None, str(exc)[:100]
    факт = e["amount_out"]
    return {"повод": повод, "пул": пул, "slot_чтения": ст["slot"], "сделка": след["signature"], "slot_сделки": т["slot"],
            "инструкция": виды[0], "swap_for_y": e["swap_for_y"], "вход": e["amount_in"], "факт_выход": факт,
            "модель_выход": q["amount_out"] if q else None,
            "расхождение_пп": round((q["amount_out"] - факт) / факт * 100, 6) if q and факт else None,
            "корзин_факт": abs(e["end"] - e["start"]) + 1, "start_факт": e["start"], "end_факт": e["end"],
            "active_id_чтения": ст["lb"]["active_id"], "end_модель": q["active_id_после"] if q else None,
            "корзин_модель": q["корзин"] if q else None, "host_fee": e.get("host_fee"), "fee_факт": e.get("fee"),
            "fee_модель": q["fee"] if q else None, "вход_по_хранилищу": д, "подписант": e.get("from"),
            "why_not": why, "сделки_источников_в_пуле": len(пулы.get(пул, []))}


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--chasov", type=float, default=24)
    р.add_argument("--cel", type=int, default=10, help="сколько многокорзинных (корзин ≥ 2)")
    р.add_argument("--minut", type=float, default=90)
    р.add_argument("--krupnaya", type=float, default=1.0, help="покупка источника от стольких SOL -- «наш случай»")
    р.add_argument("--metka", default="mnogo")
    а = р.parse_args()
    сп = json.loads((КОРЕНЬ / "data" / "podbivka" / "istochniki_code1_kandidaty.json").read_text(encoding="utf-8"))
    кошельки = sorted(set(сп["группы_code1"]) | set(сп["кандидаты"]))
    уз = S.Узел()
    итог, отказы = [], {}
    отсев: dict = {}
    out = КОРЕНЬ / "data" / "podbivka" / f"dlmm_proverka_{а.metka}.json"
    import podbivka_run as R  # noqa: PLC0415

    def записать():
        out.write_text(json.dumps({"итог": итог, "отказы": отказы, "отсев": отсев, "расход": уз.расход()}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        R.записано(out)
    with уз.на("helius"):
        пулы = пулы_источников(уз, кошельки, а.chasov)
        print("пулов DLMM у источников:", len(пулы), flush=True)
        последняя = {}
        for w in кошельки:
            try:
                зз = уз.подписи(w, limit=1)
                последняя[w] = зз[0]["signature"] if зз else None
            except RuntimeError:
                последняя[w] = None
        конец = time.time() + а.minut * 60
        очередь = sorted(пулы, key=lambda p: -len(пулы[p]))
        k, опрос = 0, 0.0
        многo = lambda: sum(1 for x in итог if x["повод"] == "поток" and (x["корзин_факт"] or 0) >= 2)  # noqa: E731
        while time.time() < конец and (многo() < а.cel or sum(1 for x in итог if x["повод"] != "поток") < 5):
            # 1. источники: новые покупки в DLMM -- сразу читаем пул («наш случай»)
            if time.time() - опрос > 15:
                опрос = time.time()
                for w in кошельки:
                    try:
                        зз = уз.подписи(w, по=последняя.get(w), limit=5) if последняя.get(w) else уз.подписи(w, limit=1)
                    except RuntimeError:
                        continue
                    if not зз:
                        continue
                    последняя[w] = зз[0]["signature"]
                    for з in зз:
                        if з.get("err") is not None:
                            continue
                        т = уз.tx(з["signature"])
                        for e in события_swap(т) if т else []:
                            if e["from"] != w:
                                continue
                            try:
                                ст = прочитать(уз, e["lb_pair"])
                            except (Q.ОшибкаDLMM, RuntimeError) as exc:
                                отказы[e["lb_pair"]] = S.чисто(str(exc))[:100]
                                continue
                            x = сверить(уз, e["lb_pair"], ст, f"после источника {w[:8]} ({e['amount_in']} сырых, "
                                        f"{abs(e['end'] - e['start']) + 1} корзин)", пулы)
                            if x and x.get("отсев"):
                                отсев[x["отсев"]] = отсев.get(x["отсев"], 0) + 1
                            elif x:
                                итог.append(x)
                                записать()
                                print("ИСТ", x["пул"][:8], x["корзин_факт"], x["расхождение_пп"], flush=True)
            # 2. поток: пул по кругу
            if not очередь:
                break
            пул = очередь[k % len(очередь)]
            k += 1
            try:
                ст = прочитать(уз, пул)
            except (Q.ОшибкаDLMM, RuntimeError) as exc:
                отказы[пул] = S.чисто(str(exc))[:100]
                очередь.remove(пул)
                continue
            x = сверить(уз, пул, ст, "поток", пулы)
            if x and x.get("отсев"):
                отсев[x["отсев"]] = отсев.get(x["отсев"], 0) + 1
                if k % 50 == 0:
                    print("отсев:", отсев, flush=True)
                    записать()
                continue
            if x and (x["корзин_факт"] >= 2 or not any(y["пул"] == пул for y in итог)):
                итог.append(x)
                записать()
                print(x["пул"][:8], x["корзин_факт"], x["расхождение_пп"], flush=True)
    записать()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
