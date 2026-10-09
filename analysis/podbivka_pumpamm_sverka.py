#!/usr/bin/env python3
"""Сверка резервов архива с цепью на свопах pump-amm: почему продажи расходятся на 7--8 %.

Зачем. На крошечных сделках (меньше 0.02 % пула) цена исполнения почти равна предельной, и
отношение (x/y) / (кв/ток) должно быть около единицы. По собранным окнам вышло:

  pump-amm / покупка  -- медиана 1.00000 (сходится ровно),
  pump-amm / продажа  -- медиана 0.92606 (p25 0.910, p75 0.933), то есть продавец получил
                         на 8 % больше SOL за токен, чем следует из пары резервов,
  meteora-damm-v2, pump, raydium-v4 -- сходятся на всех действиях.

Если пара резервов у продаж не та, то и наша модель круга на pump-amm неверна, а на нём
держится весь сегмент (а) переезда и строка «создатель купил сам». Поэтому по цепи берутся
сами транзакции: из pre/postTokenBalances считаются НАСТОЯЩИЕ резервы пула до и после
свопа, и они сравниваются с тем, что отдал архив.

Только чтение. Helius, темп -- PODB_HELIUS_RPS (потолок владельца 6 запросов в секунду на
все мои процессы вместе). Выход: data/podbivka/pumpamm_sverka.json.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
RPS = float(os.environ.get("PODB_HELIUS_RPS") or 6.0)
WSOL = "So11111111111111111111111111111111111111112"


class Темп:
    """Не больше RPS запросов в секунду -- потолок владельца на все мои процессы."""

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


def резервы_пула(tx: dict, pool: str) -> dict:
    """Настоящие резервы пула до и после свопа -- из pre/postTokenBalances его счетов.

    Хранилища пула -- это токен-счета, владелец которых и есть адрес пула. Баланс WSOL
    такого счёта -- квотное плечо, баланс минта -- токенное.
    """
    мета = tx.get("meta") or {}
    из_: dict = {"до": {}, "после": {}}
    for имя, поле in (("до", "preTokenBalances"), ("после", "postTokenBalances")):
        for б in (мета.get(поле) or []):
            if б.get("owner") != pool:
                continue
            сум = ((б.get("uiTokenAmount") or {}).get("uiAmount"))
            if сум is None:
                continue
            из_[имя][б.get("mint")] = float(сум)
    return из_


def разбор(rpc, темп: Темп, ряд: dict) -> dict:
    темп.ждать()
    о = rpc.call("getTransaction", [ряд["подпись"], {"encoding": "jsonParsed",
                                                     "maxSupportedTransactionVersion": 0}])
    если = {"подпись": ряд["подпись"], "poolId": ряд["poolId"], "минт": ряд.get("минт"),
            "блок": ряд.get("блок"), "x_архива": ряд["x_архива"],
            "y_архива": ряд["y_архива"], "ток": ряд["ток"], "кв": ряд["кв"],
            "цена_сделки": ряд["цена_сделки"], "цена_резервов": ряд["цена_резервов"],
            "отношение_архива": ряд["отношение"]}
    if not о:
        если["ошибка"] = "транзакция не найдена"
        return если
    р = резервы_пула(о, ряд["poolId"])
    если["слот_цепи"] = о.get("slot")
    минт = ряд.get("минт")
    for имя in ("до", "после"):
        x = р[имя].get(WSOL)
        y = р[имя].get(минт) if минт else None
        если[f"x_цепи_{имя}"] = x
        если[f"y_цепи_{имя}"] = y
        если[f"цена_цепи_{имя}"] = (x / y) if (x and y) else None
    # Главное число: отношение цены по цепи к цене исполнения. Если оно около единицы, а у
    # архива 0.93 -- значит пара резервов архива на продажах не та.
    for имя in ("до", "после"):
        ц = если.get(f"цена_цепи_{имя}")
        если[f"отношение_цепи_{имя}"] = (round(ц / ряд["цена_сделки"], 5)
                                         if ц and ряд["цена_сделки"] else None)
    for имя in ("до", "после"):
        x = если.get(f"x_цепи_{имя}")
        если[f"x_цепи_к_архиву_{имя}"] = (round(x / ряд["x_архива"], 5)
                                          if x and ряд["x_архива"] else None)
        y = если.get(f"y_цепи_{имя}")
        если[f"y_цепи_к_архиву_{имя}"] = (round(y / ряд["y_архива"], 5)
                                          if y and ряд["y_архива"] else None)
    return если


def свод(ряды: list) -> dict:
    из_: dict = {}
    for имя in ("отношение_цепи_до", "отношение_цепи_после", "отношение_архива",
                "x_цепи_к_архиву_до", "x_цепи_к_архиву_после",
                "y_цепи_к_архиву_до", "y_цепи_к_архиву_после"):
        v = [r[имя] for r in ряды if r.get(имя) is not None]
        из_[имя] = ({"n": len(v), "медиана": round(statistics.median(v), 5),
                     "p25": round(sorted(v)[len(v) // 4], 5),
                     "p75": round(sorted(v)[3 * len(v) // 4], 5)} if v else {"n": 0})
    return из_


def main() -> int:
    import c2_common as C  # noqa: PLC0415
    import podbivka_run as R  # noqa: PLC0415
    р_ = argparse.ArgumentParser()
    р_.add_argument("--celi", default=str(П / "pumpamm_sverka_celi.json"))
    р_.add_argument("--skolko", type=int, default=40)
    р_.add_argument("--metka", default="")
    а = р_.parse_args()
    ключ = (os.environ.get("HELIUS_API_KEY2") or os.environ.get("HELIUS_API_KEY")
            or os.environ.get("HELIUS_API") or "").strip()
    if not ключ:
        print("нет ключа Helius -- проход только облачный", flush=True)
        return 1
    цели = json.loads(Path(а.celi).read_text(encoding="utf-8"))
    rpc = C.C2Rpc(service="c2_pumpamm_sverka", key=ключ)
    темп = Темп(RPS)
    из_: dict = {}
    for вид in ("продажи", "покупки"):
        ряды = (цели.get(вид) or [])[:а.skolko]
        готово = []
        for i, ряд in enumerate(ряды, 1):
            try:
                готово.append(разбор(rpc, темп, ряд))
            except Exception as exc:  # noqa: BLE001
                готово.append({"подпись": ряд["подпись"],
                               "ошибка": f"{type(exc).__name__}"})
            if i % 10 == 0 or i == len(ряды):
                print(f"  {вид}: {i}/{len(ряды)}", flush=True)
        из_[вид] = {"ряды": готово, "свод": свод(готово)}
    тело = {"что": "сверка резервов архива с цепью на свопах pump-amm",
            "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "параметры": vars(а), **из_,
            "запросов": dict(getattr(rpc, "calls_by_method", {}) or {})}
    ф = П / f"pumpamm_sverka{('_' + а.metka) if а.metka else ''}.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(ф)
    for вид in ("продажи", "покупки"):
        print(f"{вид}: {json.dumps(из_[вид]['свод'], ensure_ascii=False)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
