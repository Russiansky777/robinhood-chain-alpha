#!/usr/bin/env python3
"""Чем PumpSwap считает цену: события программы против балансов счетов пула.

Что уже известно по цепи (podbivka_pumpamm_sverka): на крошечных продажах pump-amm
исполнение на 7--10 % ЛУЧШЕ, чем даёт пара резервов из архива, а сама пара резервов
совпадает с балансами токен-счетов пула ровно (37 продаж, отношение 1.0). Значит
постоянное произведение у PumpSwap считается НЕ по балансам счетов: часть токена на счёте
в кривую не входит (похоже на накопленные сборы создателя).

Этот проход достаёт из транзакции событие самой программы. Anchor пишет его в логах строкой
«Program data: <base64>»; поля события -- u64 подряд. Скрипт не угадывает разметку: он
печатает все u64 и сам ищет, какие из них совпадают с балансами счетов, а какие дают ровно
ту цену, по которой сделка исполнилась. Что совпало -- то и есть резерв кривой.

Только чтение. Выход: data/podbivka/pumpamm_sobytie.json.
"""
from __future__ import annotations

import argparse
import base64
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
WSOL = "So11111111111111111111111111111111111111112"
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"


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


def поля_события(логи: list) -> list:
    """Все u64 из всех «Program data:» -- без догадок о разметке."""
    из_ = []
    for стр in логи or []:
        if "Program data:" not in стр:
            continue
        сырое = стр.split("Program data:", 1)[1].strip()
        try:
            байты = base64.b64decode(сырое)
        except Exception:      # noqa: BLE001
            continue
        поля = []
        # первые 8 байт -- дискриминатор события
        for i in range(8, len(байты) - 7, 8):
            поля.append(struct.unpack_from("<Q", байты, i)[0])
        из_.append({"байт": len(байты), "u64": поля})
    return из_


def балансы(tx: dict, pool: str) -> dict:
    """ВСЕ токен-счета пула, по одному на индекс счёта, а не по одному на минт.

    Прежняя проверка держала словарь «минт -> баланс» и потому ПЕРЕЗАТИРАЛА счета: если у
    пула два счёта одного минта (хранилище кривой и счёт накопленных сборов), в счёт шёл
    последний. Отсюда и могло взяться расхождение в 8 %: кривая считает по хранилищу, а
    архив отдаёт другое число.
    """
    мета = tx.get("meta") or {}
    ключи = (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or [])
    из_: dict = {"до": [], "после": []}
    for имя, поле in (("до", "preTokenBalances"), ("после", "postTokenBalances")):
        for б in (мета.get(поле) or []):
            if б.get("owner") != pool:
                continue
            сум = (б.get("uiTokenAmount") or {})
            if сум.get("amount") is None:
                continue
            и = б.get("accountIndex")
            адрес = None
            if isinstance(и, int) and и < len(ключи):
                к = ключи[и]
                адрес = к.get("pubkey") if isinstance(к, dict) else к
            из_[имя].append({"индекс": и, "счёт": адрес, "минт": б.get("mint"),
                             "сырое": int(сум["amount"]),
                             "десятичных": сум.get("decimals"), "ui": сум.get("uiAmount")})
    из_["счетов_до"] = len(из_["до"])
    из_["счетов_после"] = len(из_["после"])
    return из_


def разбор(rpc, темп: Темп, ряд: dict) -> dict:
    темп.ждать()
    о = rpc.call("getTransaction", [ряд["подпись"], {"encoding": "jsonParsed",
                                                     "maxSupportedTransactionVersion": 0}])
    если = {"подпись": ряд["подпись"], "poolId": ряд["poolId"], "минт": ряд.get("минт"),
            "x_архива": ряд["x_архива"], "y_архива": ряд["y_архива"],
            "ток": ряд["ток"], "кв": ряд["кв"], "отношение_архива": ряд["отношение"]}
    if not о:
        если["ошибка"] = "транзакция не найдена"
        return если
    лог = (о.get("meta") or {}).get("logMessages") or []
    если["события"] = поля_события(лог)
    если["балансы"] = балансы(о, ряд["poolId"])
    # Какая пара u64 из события даёт ровно цену исполнения? Ищем честно, перебором.
    цена = ряд["кв"] / ряд["ток"] if ряд["ток"] else None
    находки = []
    if цена:
        деся_ток = None
        for v in (если["балансы"].get("после") or []):
            if v.get("минт") == ряд.get("минт"):
                деся_ток = v.get("десятичных")
        for н, соб in enumerate(если["события"]):
            поля = соб["u64"]
            for i, a in enumerate(поля):
                for j, b in enumerate(поля):
                    if i == j or not a or not b:
                        continue
                    # a -- квотное плечо в лампортах, b -- токенное в своих единицах
                    ц = (a / 1e9) / (b / (10 ** (деся_ток if деся_ток is not None else 6)))
                    if цена * 0.999 <= ц <= цена * 1.001:
                        находки.append({"событие": н, "i": i, "j": j, "a": a, "b": b,
                                        "цена": ц})
    если["пары_дающие_цену_исполнения"] = находки[:8]
    return если


def main() -> int:
    import c2_common as C  # noqa: PLC0415
    import podbivka_run as R  # noqa: PLC0415
    р_ = argparse.ArgumentParser()
    р_.add_argument("--celi", default=str(П / "pumpamm_sverka_celi.json"))
    р_.add_argument("--skolko", type=int, default=12)
    р_.add_argument("--metka", default="")
    а = р_.parse_args()
    ключ = (os.environ.get("HELIUS_API_KEY2") or os.environ.get("HELIUS_API_KEY")
            or os.environ.get("HELIUS_API") or "").strip()
    if not ключ:
        print("нет ключа Helius -- проход только облачный", flush=True)
        return 1
    цели = json.loads(Path(а.celi).read_text(encoding="utf-8"))
    rpc = C.C2Rpc(service="c2_pumpamm_sobytie", key=ключ)
    темп = Темп(RPS)
    из_: dict = {}
    for вид in ("продажи", "покупки"):
        ряды = (цели.get(вид) or [])[:а.skolko]
        готово = []
        for i, ряд in enumerate(ряды, 1):
            try:
                готово.append(разбор(rpc, темп, ряд))
            except Exception as exc:   # noqa: BLE001
                готово.append({"подпись": ряд["подпись"], "ошибка": type(exc).__name__})
            print(f"  {вид}: {i}/{len(ряды)}", flush=True)
        из_[вид] = готово
    # Сколько раз одна и та же позиция поля дала цену исполнения -- это и есть разметка.
    счёт: dict = {}
    for вид, ряды in из_.items():
        c: dict = {}
        for r in ряды:
            for н in (r.get("пары_дающие_цену_исполнения") or []):
                k = f"событие{н['событие']}:i{н['i']}/j{н['j']}"
                c[k] = c.get(k, 0) + 1
        счёт[вид] = dict(sorted(c.items(), key=lambda kv: -kv[1])[:10])
    тело = {"что": "чем PumpSwap считает цену: события программы против балансов счетов",
            "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "параметры": vars(а), "разметка_по_частоте": счёт, **из_,
            "запросов": dict(getattr(rpc, "calls_by_method", {}) or {})}
    ф = П / f"pumpamm_sobytie{('_' + а.metka) if а.metka else ''}.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(ф)
    print(f"разметка по частоте: {json.dumps(счёт, ensure_ascii=False)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
