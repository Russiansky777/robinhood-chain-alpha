#!/usr/bin/env python3
"""Почему 13.9 % сделок кривой не сходятся: архив против СОБЫТИЯ самой программы.

Что уже исключено (всё -- замерами, не рассуждением):
  * налог на перевод -- 0 из 1 991 минта по цепи (TransferFeeConfig нет ни у одного);
  * кратность сделок в блоке -- у кривой отношение ровно 1.000000 и при 5+ в блоке;
  * соглашение «состояние до / после» -- у несошедшихся оба варианта дают почти одно
    (0.8461 против 0.8433);
  * виртуальный сдвиг по SOL (+30) -- починил 1 сделку из 3 522;
  * виртуальный сдвиг по токену (+2.799e8) -- починил 39 из 3 522;
  * гросс вместо нетто -- подобранный тариф выходит в 4.5--32 раза больше тарифа потока
    (медиана 17.9 % против 1.25 %), значит дело не в тарифе.

Остаётся спросить программу. У кривой pump.fun событие сделки разобрано и проверено Code-1
(`c2_swap_build.pump_trade_event`): минт, суммы, ВИРТУАЛЬНЫЕ резервы до и после, тариф
протокола и тариф создателя. Этот проход берёт сами транзакции по подписям и сравнивает:
резервы архива против виртуальных резервов события, и выход сделки против формулы кривой на
событийных резервах. Что разойдётся -- то и есть причина.

Для контроля берутся и сошедшиеся сделки: если у них всё совпадает, а у несошедшихся нет --
причина названа.

Только чтение. Выход: data/podbivka/krivaya_rashod.json.
"""
from __future__ import annotations

import argparse
import base64
import collections
import json
import os
import statistics
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
RPS = float(os.environ.get("PODB_HELIUS_RPS") or 6.0)


class Темп:
    def __init__(self, rps: float) -> None:
        self.rps = max(0.5, rps)
        self._окно: list = []

    def ждать(self, запросов: int = 1) -> None:
        now = time.time()
        self._окно = [(t, n) for t, n in self._окно if now - t < 1.0]
        while sum(n for _, n in self._окно) + запросов > self.rps and self._окно:
            time.sleep(max(0.02, 1.0 - (now - self._окно[0][0])))
            now = time.time()
            self._окно = [(t, n) for t, n in self._окно if now - t < 1.0]
        self._окно.append((time.time(), запросов))


def событие_кривой(tx: dict) -> dict | None:
    """Событие сделки кривой из логов. Раскладка -- проверенная Code-1.

    Смещения внутри события (после 8 байт дискриминатора): минт 0..32, sol 32, токен 40,
    признак покупки 48, кошелёк 49..81, виртуальные и реальные резервы с 89 четырьмя u64,
    тариф протокола и его сумма с 153, тариф создателя и его сумма с 201.
    """
    import hashlib      # noqa: PLC0415
    диск = hashlib.sha256(b"event:TradeEvent").digest()[:8]
    for стр in ((tx or {}).get("meta") or {}).get("logMessages") or []:
        if not стр.startswith("Program data: "):
            continue
        try:
            сыр = base64.b64decode(стр[len("Program data: "):].strip())
        except ValueError:
            continue
        if сыр[:8] != диск or len(сыр) < 8 + 217:
            continue
        b = сыр[8:]
        try:
            sol, tok = struct.unpack_from("<QQ", b, 32)
            покупка = b[48]
            vs, vt, rs, rt = struct.unpack_from("<QQQQ", b, 89)
            fee_bps, fee = struct.unpack_from("<QQ", b, 153)
            cr_bps, cr = struct.unpack_from("<QQ", b, 201)
        except struct.error:
            continue
        return {"sol": sol, "токен": tok, "покупка": bool(покупка),
                "вирт_sol_после": vs, "вирт_токен_после": vt,
                "реал_sol_после": rs, "реал_токен_после": rt,
                "тариф_бп": fee_bps, "тариф_сумма": fee,
                "тариф_создателя_бп": cr_bps, "тариф_создателя_сумма": cr}
    return None


def разбор(rpc, темп: Темп, ряд: dict) -> dict:
    темп.ждать()
    о = rpc.call("getTransaction", [ряд["подпись"], {"encoding": "jsonParsed",
                                                     "maxSupportedTransactionVersion": 0}])
    из_ = {k: ряд.get(k) for k in ("подпись", "минт", "действие", "x_архива", "y_архива",
                                   "ток", "кв", "тариф", "отношение")}
    if not о:
        из_["ошибка"] = "транзакция не найдена"
        return из_
    е = событие_кривой(о)
    if not е:
        из_["ошибка"] = "события кривой в логах нет"
        return из_
    из_["событие"] = е
    # Виртуальные резервы события -- ПОСЛЕ сделки. Приводим к тем же единицам, что архив:
    # SOL в лампортах -> SOL, токен в своих единицах -> с 6 десятичными.
    vs = е["вирт_sol_после"] / 1e9
    vt = е["вирт_токен_после"] / 1e6
    из_["вирт_sol_после"] = vs
    из_["вирт_токен_после"] = vt
    из_["x_цепи_к_архиву"] = round(vs / ряд["x_архива"], 5) if ряд.get("x_архива") else None
    из_["y_цепи_к_архиву"] = round(vt / ряд["y_архива"], 5) if ряд.get("y_архива") else None
    # Воспроизводит ли формула кривой выход сделки на СОБЫТИЙНЫХ резервах
    sol, tok = е["sol"], е["токен"]
    if е["покупка"]:
        vs0, vt0 = е["вирт_sol_после"] - sol, е["вирт_токен_после"] + tok
        пред = (vt0 * sol // (vs0 + sol)) if vs0 + sol else None
        факт = tok
    else:
        vs0, vt0 = е["вирт_sol_после"] + sol, е["вирт_токен_после"] - tok
        пред = (vs0 * tok // (vt0 + tok)) if vt0 + tok else None
        факт = sol
    из_["формула_на_событии"] = (round(пред / факт, 6)
                                 if (пред and факт) else None)
    из_["суммы_цепи_к_архиву"] = {
        "sol": round((sol / 1e9) / ряд["кв"], 5) if ряд.get("кв") else None,
        "токен": round((tok / 1e6) / ряд["ток"], 5) if ряд.get("ток") else None}
    return из_


def свод(ряды: list) -> dict:
    из_: dict = {"n": len(ряды), "ошибок": sum(1 for r in ряды if r.get("ошибка"))}
    for имя in ("x_цепи_к_архиву", "y_цепи_к_архиву", "формула_на_событии"):
        v = [r[имя] for r in ряды if r.get(имя) is not None]
        из_[имя] = ({"n": len(v), "медиана": round(statistics.median(v), 6),
                     "p10": round(sorted(v)[len(v) // 10], 6),
                     "p90": round(sorted(v)[9 * len(v) // 10], 6)} if v else {"n": 0})
    for нога in ("sol", "токен"):
        v = [(r.get("суммы_цепи_к_архиву") or {}).get(нога) for r in ряды]
        v = [q for q in v if q is not None]
        из_[f"сумма_{нога}_цепи_к_архиву"] = (
            {"n": len(v), "медиана": round(statistics.median(v), 6),
             "p10": round(sorted(v)[len(v) // 10], 6),
             "p90": round(sorted(v)[9 * len(v) // 10], 6)} if v else {"n": 0})
    тарифы = collections.Counter()
    for r in ряды:
        е = r.get("событие") or {}
        if е:
            тарифы[(е.get("тариф_бп"), е.get("тариф_создателя_бп"))] += 1
    из_["тарифы_события_бп"] = {f"{a}+{b}": n for (a, b), n in тарифы.most_common(8)}
    return из_


def main() -> int:
    import c2_common as C  # noqa: PLC0415
    import podbivka_run as R  # noqa: PLC0415
    р_ = argparse.ArgumentParser()
    р_.add_argument("--celi", default=str(П / "krivaya_rashod_celi.json"))
    р_.add_argument("--skolko", type=int, default=120)
    р_.add_argument("--metka", default="")
    а = р_.parse_args()
    ключ = (os.environ.get("HELIUS_API_KEY2") or os.environ.get("HELIUS_API_KEY")
            or os.environ.get("HELIUS_API") or "").strip()
    if not ключ:
        print("нет ключа Helius -- проход только облачный", flush=True)
        return 1
    цели = json.loads(Path(а.celi).read_text(encoding="utf-8"))
    rpc = C.C2Rpc(service="c2_krivaya_rashod", key=ключ)
    темп = Темп(RPS)
    из_: dict = {}
    for вид in ("не_сошлись", "сошлись_контроль"):
        ряды = (цели.get(вид) or [])[:а.skolko]
        готово = []
        for i, ряд in enumerate(ряды, 1):
            try:
                готово.append(разбор(rpc, темп, ряд))
            except Exception as exc:   # noqa: BLE001
                готово.append({"подпись": ряд.get("подпись"),
                               "ошибка": type(exc).__name__})
            if i % 20 == 0 or i == len(ряды):
                print(f"  {вид}: {i}/{len(ряды)}", flush=True)
        из_[вид] = {"ряды": готово, "свод": свод(готово)}
    тело = {"что": "почему сделки кривой не сходятся: архив против события программы",
            "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "параметры": vars(а),
            "свод": {k: v["свод"] for k, v in из_.items()}, **из_,
            "запросов": dict(getattr(rpc, "calls_by_method", {}) or {})}
    ф = П / f"krivaya_rashod{('_' + а.metka) if а.metka else ''}.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(ф)
    for вид, v in из_.items():
        print(f"{вид}: {json.dumps(v['свод'], ensure_ascii=False)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
