#!/usr/bin/env python3
"""Подбивка: проба RPC Shyft ДО любого счёта.

ЗАЧЕМ. Решение владельца 26.09 -- вся цепь подбивки через Shyft, Helius у
детектора. Ключа RPC Shyft под отдельным именем в прогонах нет; 24.09 Shyft
ходил под секретами gRPC-фида (GRPC_FEED_TOKEN, второй канал GRPC_FEED2_TOKEN).
Здесь проверяется, принимает ли RPC Shyft этот ключ, и что узел умеет из
того, на чём стоит подбивка: пакетный JSON-RPC, старые транзакции (18.09),
getSignaturesForAddress с before из чужой подписи, getBlock только подписями.

Значения ключей не печатаются никогда: имя секрета, да/нет и вычищенный текст.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from podbivka_1b import чисто  # noqa: E402

КАНДИДАТЫ = ("SHYFT_API_KEY",)
АДРЕС = "https://rpc.shyft.to?api_key={}"
BREZ = "Fvkc2thk1YcAASdR2gi8uf9n67JW9Dqqr9iRd99MDhoB"
ОПЦИИ_TX = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
            "commitment": "confirmed"}


def пост(ключ: str, тело, срок: float = 30.0) -> dict:
    import requests  # noqa: PLC0415
    н = time.time()
    try:
        от = requests.post(АДРЕС.format(ключ), json=тело, timeout=срок)
        мс = round((time.time() - н) * 1000)
        try:
            j = от.json()
        except ValueError:
            j = None
        return {"http": от.status_code, "мс": мс, "json": j,
                "текст": чисто(от.text[:200]) if от.status_code != 200 else None}
    except Exception as exc:  # noqa: BLE001
        return {"http": None, "мс": round((time.time() - н) * 1000), "json": None,
                "текст": чисто(f"{type(exc).__name__}: {exc}")[:200]}


def ошибка(о: dict):
    j = о.get("json")
    if isinstance(j, dict) and "error" in j:
        return чисто(str(j["error"]))[:200]
    return о.get("текст")


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--out", default="data/podbivka/shyft_proba.json")
    р.add_argument("--github-env", default=os.environ.get("GITHUB_ENV", ""))
    а = р.parse_args()
    итог: dict = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "адрес_rpc": "https://rpc.shyft.to?api_key=<ключ>", "кандидаты": {}}
    значения = {имя: (os.environ.get(имя) or "").strip() for имя in КАНДИДАТЫ}
    выбран = None
    for имя in КАНДИДАТЫ:
        к = значения[имя]
        if not к:
            итог["кандидаты"][имя] = {"задан": False}
            continue
        о = пост(к, {"jsonrpc": "2.0", "id": 1, "method": "getSlot"})
        ok = isinstance(о.get("json"), dict) and isinstance(о["json"].get("result"), int)
        итог["кандидаты"][имя] = {"задан": True, "getSlot_ok": ok, "http": о["http"],
                                  "мс": о["мс"], "ошибка": None if ok else ошибка(о)}
        if ok and not выбран:
            выбран = имя
    итог["выбран"] = выбран
    if выбран:
        к = значения[выбран]
        пр: dict = {}
        о = пост(к, [{"jsonrpc": "2.0", "id": 0, "method": "getSlot"},
                     {"jsonrpc": "2.0", "id": 1, "method": "getSlot"}])
        пр["пакет_из_2"] = {"ok": isinstance(о.get("json"), list) and len(о["json"]) == 2,
                            "http": о["http"], "ошибка": ошибка(о) if not isinstance(о.get("json"), list) else None}
        контроль = json.loads(Path("data/podbivka/kontrol_ceny.json").read_text(encoding="utf-8"))
        sig24 = контроль["ряды"][0]["source_sig"]
        p1b = json.loads(Path("data/podbivka/p1b_sverka.json").read_text(encoding="utf-8"))
        ранняя = min(p1b["по_источнику"]["leader"]["список_источник_без_нас"],
                     key=lambda x: x["blockTime"])
        for метка, sig in (("tx_24_09", sig24), ("tx_18_09", ранняя["signature"])):
            о = пост(к, {"jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                         "params": [sig, ОПЦИИ_TX]}, срок=40.0)
            рез = (о.get("json") or {}).get("result") if isinstance(о.get("json"), dict) else None
            пр[метка] = {"ok": bool(рез), "slot": (рез or {}).get("slot"), "мс": о["мс"],
                         "ошибка": None if рез else ошибка(о) or "result пуст"}
        о = пост(к, {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                     "params": [BREZ, {"limit": 3}]})
        рез = (о.get("json") or {}).get("result") if isinstance(о.get("json"), dict) else None
        пр["подписи_brez"] = {"ok": isinstance(рез, list), "штук": len(рез or []), "мс": о["мс"],
                              "ошибка": None if isinstance(рез, list) else ошибка(о)}
        # before -- ЧУЖАЯ подпись (покупка лидера 18.09): отдаёт ли узел подписи
        # Brez строго раньше её слота. На этом стоит окно истории пула.
        о = пост(к, {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                     "params": [BREZ, {"limit": 3, "before": ранняя["signature"]}]})
        рез = (о.get("json") or {}).get("result") if isinstance(о.get("json"), dict) else None
        слоты = [з.get("slot") for з in (рез or [])]
        пр["before_чужая_подпись"] = {
            "ok": isinstance(рез, list) and bool(рез) and all((s or 0) <= ранняя["slot"] for s in слоты),
            "штук": len(рез or []), "слот_опоры": ранняя["slot"], "слоты": слоты,
            "ошибка": None if isinstance(рез, list) else ошибка(о)}
        о = пост(к, {"jsonrpc": "2.0", "id": 1, "method": "getBlock",
                     "params": [ранняя["slot"], {"transactionDetails": "signatures", "rewards": False,
                                                 "maxSupportedTransactionVersion": 0}]}, срок=40.0)
        рез = (о.get("json") or {}).get("result") if isinstance(о.get("json"), dict) else None
        пр["getBlock_подписи"] = {"ok": bool(рез), "подписей": len((рез or {}).get("signatures") or []),
                                  "мс": о["мс"], "ошибка": None if рез else ошибка(о)}
        итог["проверки"] = пр
    # Формат адреса -- из документации Shyft, а не из памяти.
    try:
        import requests  # noqa: PLC0415
        т = requests.get("https://docs.shyft.to/llms.txt", timeout=20).text
        итог["документация_rpc"] = [s.strip()[:200] for s in т.splitlines()
                                    if "rpc" in s.lower()][:25]
    except Exception as exc:  # noqa: BLE001
        итог["документация_rpc"] = [чисто(f"{type(exc).__name__}: {exc}")[:200]]
    Path(а.out).parent.mkdir(parents=True, exist_ok=True)
    Path(а.out).write_text(json.dumps(итог, ensure_ascii=False, indent=1), encoding="utf-8")
    if выбран and а.github_env:
        with open(а.github_env, "a", encoding="utf-8") as ф:
            ф.write(f"SHYFT_KEY_NAME={выбран}\n")
    print("shyft: выбран {} | кандидаты {} | проверки {}".format(
        выбран, json.dumps(итог["кандидаты"], ensure_ascii=False),
        json.dumps({к: v.get("ok") for к, v in (итог.get("проверки") or {}).items()}, ensure_ascii=False)))
    return 0 if выбран else 2


if __name__ == "__main__":
    raise SystemExit(main())
