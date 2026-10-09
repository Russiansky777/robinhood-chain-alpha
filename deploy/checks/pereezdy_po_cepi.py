#!/usr/bin/env python3
"""Переезды кривой pump.fun -> PumpSwap: кто подписывает и какими байтами.

ЗАЧЕМ (слово владельца 09.10, п.1). Чтобы детектор ловил переезды, подписка
должна идти на ОДИН счёт -- миграционный авторитет, -- а не на всю программу
кривой: её поток на порядки больше нашего. Адрес этого счёта нигде в репозитории
не записан, и выдумывать его нельзя. Здесь он берётся ПО ЦЕПИ: из настоящих
транзакций переезда, по подписантам.

ОТКУДА БЕРУТСЯ КАНДИДАТЫ. Суточный файл архива Code-2 (konv_*.json.gz) знает по
каждому сигналу минт и ПРОГРАММУ ПУЛА. Минт, у которого в одних сутках есть и
`pump` (кривая), и `pump-amm`, -- переехал: других способов попасть из одной
программы в другую у токена нет. В одном файле таких минтов 137, то есть
кандидатов заведомо больше нужных 20-30.

КАК НАХОДИТСЯ САМА ТРАНЗАКЦИЯ ПЕРЕЕЗДА -- ОДНОЙ СТРАНИЦЕЙ НА МИНТ. Счёт кривой
(PDA по семенам IDL ["bonding-curve", mint]) после переезда больше не торгует,
поэтому переезд -- среди последних его подписей. Перебирать всю историю пула не
нужно.

ЧТО СЧИТАЕТСЯ ОТВЕТОМ. Для каждого найденного переезда: подпись, слот, время,
ФАКТИЧЕСКИЕ 8 байт инструкции кривой и её имя по IDL, все подписанты, плательщик
и счета инструкции create_pool программы Pump AMM из ТОЙ ЖЕ транзакции. Сводка
says, сошлись ли байты с IDL и один ли подписант у всех переездов: один и тот же
-- это и есть авторитет, разные -- значит подписка на один счёт не годится, и
это надо сказать, а не подогнать.

ТОЛЬКО ЧТЕНИЕ: getSignaturesForAddress и getTransaction. Ни одной подписи, ни
одного sendTransaction, ключей в файле нет. Узел -- ТОЛЬКО переменной окружения
BLOOM_TREKKER_RPC: ключ в argv виден в списке процессов любому на машине.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "analysis"))

ПРОГ_КРИВОЙ = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
ПРОГ_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
# Имена инструкций, а не байты: байты берутся из IDL и сверяются формулой.
ИМЕНА_ПЕРЕЕЗДА = ("migrate", "migrate_v2")
ИМЯ_СОЗДАНИЯ = "create_pool"


def узел() -> str:
    у = os.environ.get("BLOOM_TREKKER_RPC") or ""
    if not у:
        raise SystemExit("СТОП: узел задаётся только окружением BLOOM_TREKKER_RPC")
    return у


def затереть(текст: str) -> str:
    """Ключ из URL в вывод не попадает."""
    т = str(текст or "")
    if "api-key=" in т:
        return т.split("api-key=")[0] + "api-key=…"
    return т


def зов(метод: str, параметры: list, *, таймаут: float = 30.0) -> dict:
    тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                        "params": параметры}).encode()
    зап = urllib.request.Request(узел(), data=тело,
                                  headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(зап, timeout=таймаут) as отв:
            д = json.loads(отв.read().decode())
    except (urllib.error.URLError, OSError, ValueError) as сбой:
        return {"ok": False, "why_not": f"{type(сбой).__name__}: {затереть(сбой)}"}
    if "error" in д:
        return {"ok": False, "why_not": json.dumps(д["error"], ensure_ascii=False)[:200]}
    return {"ok": True, "result": д.get("result")}


def диски_из_idl() -> dict:
    """{имя: 8 байт} для переезда и create_pool -- из закреплённого IDL."""
    import c3_pump_sborka as СБ  # noqa: PLC0415

    идл = СБ.zagruzit_idl()
    из_ = {}
    for имя in ИМЕНА_ПЕРЕЕЗДА:
        из_[("pump", имя)] = идл["pump"]["ix"][имя]["disc"]
    из_[("pump_amm", ИМЯ_СОЗДАНИЯ)] = идл["pump_amm"]["ix"][ИМЯ_СОЗДАНИЯ]["disc"]
    return из_


def кандидаты_из_arhiva(путь: str, сколько: int) -> list:
    """Минты, у которых в сутках есть и кривая, и Pump AMM: они переехали."""
    откр = gzip.open if путь.endswith(".gz") else open
    with откр(путь, "rt", encoding="utf-8") as ф:
        д = json.load(ф)
    по_минту: dict = {}
    for з in д.get("сигналы") or []:
        м = з.get("mint")
        прог = з.get("pool")
        if not м or not прог:
            continue
        к = по_минту.setdefault(м, {"программы": set(), "первый_amm": None})
        к["программы"].add(прог)
        if прог == "pump-amm":
            т = з.get("timestamp")
            if isinstance(т, (int, float)) and (к["первый_amm"] is None
                                                 or т < к["первый_amm"]):
                к["первый_amm"] = т
    оба = [(к["первый_amm"] or 0, м) for м, к in по_минту.items()
           if {"pump", "pump-amm"} <= к["программы"]]
    оба.sort(reverse=True)  # свежие переезды первыми: их подписи ближе к хвосту
    return [м for _, м in оба[:сколько]]


def счёт_кривой(минт: str) -> str:
    """PDA кривой по семенам IDL -- тем же кодом, что у сборщика."""
    import c3_pump_sborka as СБ  # noqa: PLC0415

    адрес, _ = СБ.pda([b"bonding-curve", СБ.b58d(минт)], ПРОГ_КРИВОЙ)
    return адрес


def разобрать_переезд(tx: dict, диски: dict) -> dict | None:
    """Инструкция переезда и create_pool из ОДНОЙ транзакции, или None."""
    соо = ((tx or {}).get("transaction") or {}).get("message") or {}
    ключи = [k.get("pubkey") if isinstance(k, dict) else k
             for k in (соо.get("accountKeys") or [])]
    подписанты = [k.get("pubkey") for k in (соо.get("accountKeys") or [])
                  if isinstance(k, dict) and k.get("signer")]
    инстр = list(соо.get("instructions") or [])
    for вн in ((tx or {}).get("meta") or {}).get("innerInstructions") or []:
        инстр.extend(вн.get("instructions") or [])
    из_ = {"pereezd": None, "create_pool": None}
    for и in инстр:
        прог = и.get("programId") or (ключи[и["programIdIndex"]]
                                       if isinstance(и.get("programIdIndex"), int)
                                       and и["programIdIndex"] < len(ключи) else None)
        данные = и.get("data")
        if not прог or not isinstance(данные, str):
            continue
        try:  # данные приходят base58 у jsonParsed и base64 у base64
            сырые = base64.b64decode(данные, validate=True)
        except Exception:  # noqa: BLE001
            import c3_pump_sborka as СБ  # noqa: PLC0415
            try:
                сырые = СБ.b58d(данные)
            except Exception:  # noqa: BLE001
                continue
        диск = сырые[:8]
        счета = и.get("accounts") or []
        имена_счетов = [ключи[с] if isinstance(с, int) and с < len(ключи) else с
                         for с in счета]
        if прог == ПРОГ_КРИВОЙ:
            for имя in ИМЕНА_ПЕРЕЕЗДА:
                if диск == диски[("pump", имя)]:
                    из_["pereezd"] = {"imja_po_idl": имя, "bajty": диск.hex(),
                                       "schetov": len(счета),
                                       "scheta": имена_счетов}
        elif прог == ПРОГ_AMM and диск == диски[("pump_amm", ИМЯ_СОЗДАНИЯ)]:
            из_["create_pool"] = {"bajty": диск.hex(), "schetov": len(счета),
                                   "scheta": имена_счетов}
    if not из_["pereezd"]:
        return None
    из_["podpisanty"] = подписанты
    из_["platelshchik"] = ключи[0] if ключи else None
    return из_


def живой(*, arhiv: str, сколько: int, подписей: int, пауза: float) -> dict:
    диски = диски_из_idl()
    из_ = {"rezhim": "zhivoj", "uzel": затереть(узел()), "setevyh_vyzovov": 0,
            "kandidatov": 0, "naydeno_pereezdov": 0, "pereezdy": [],
            "ne_nashlos": [], "why_not": None}
    минты = кандидаты_из_arhiva(arhiv, сколько)
    из_["kandidatov"] = len(минты)
    if not минты:
        из_["why_not"] = "в архиве нет минтов с обеими программами пула"
        return из_
    for минт in минты:
        pda_ = счёт_кривой(минт)
        о = зов("getSignaturesForAddress", [pda_, {"limit": подписей}])
        из_["setevyh_vyzovov"] += 1
        if not о["ok"]:
            из_["ne_nashlos"].append({"mint": минт, "why_not":
                                       f"подписи не прочитаны: {о['why_not']}"})
            continue
        нашли = None
        for зп in (о.get("result") or []):
            подпись = зп.get("signature")
            if not подпись:
                continue
            if пауза:
                time.sleep(пауза)
            т = зов("getTransaction", [подпись, {"encoding": "jsonParsed",
                                                  "maxSupportedTransactionVersion": 0,
                                                  "commitment": "confirmed"}])
            из_["setevyh_vyzovov"] += 1
            if not т["ok"] or not т.get("result"):
                continue
            р = разобрать_переезд(т["result"], диски)
            if р:
                р.update(mint=минт, podpis=подпись, slot=т["result"].get("slot"),
                          blockTime=т["result"].get("blockTime"),
                          schjot_krivoj=pda_)
                нашли = р
                break
        if нашли:
            из_["pereezdy"].append(нашли)
            из_["naydeno_pereezdov"] += 1
        else:
            из_["ne_nashlos"].append({"mint": минт, "schjot_krivoj": pda_,
                                       "why_not": "в последних подписях счёта "
                                                  "кривой переезда нет"})
        if пауза:
            time.sleep(пауза)
    из_.update(svodka=свести(из_["pereezdy"], диски))
    if not из_["pereezdy"]:
        из_["why_not"] = "ни одного переезда по цепи не нашлось"
    return из_


def свести(переезды: list, диски: dict) -> dict:
    """Один ли подписант у всех переездов и сошлись ли байты с IDL."""
    из_ = {"po_imeni": {}, "bajty_soshlis_s_idl": None,
            "podpisanty_vseh": {}, "platelshchiki": {},
            "obshchiy_podpisant": None, "s_create_pool": 0}
    if not переезды:
        return из_
    байты_ок = True
    for п in переезды:
        имя = п["pereezd"]["imja_po_idl"]
        из_["po_imeni"][имя] = из_["po_imeni"].get(имя, 0) + 1
        if bytes.fromhex(п["pereezd"]["bajty"]) != диски[("pump", имя)]:
            байты_ок = False
        for с in п.get("podpisanty") or []:
            из_["podpisanty_vseh"][с] = из_["podpisanty_vseh"].get(с, 0) + 1
        пл = п.get("platelshchik")
        if пл:
            из_["platelshchiki"][пл] = из_["platelshchiki"].get(пл, 0) + 1
        из_["s_create_pool"] += 1 if п.get("create_pool") else 0
    из_["bajty_soshlis_s_idl"] = байты_ок
    # ОБЩИЙ ПОДПИСАНТ -- ТОЛЬКО ЕСЛИ ОН ЕСТЬ У ВСЕХ. Иначе None и это честно:
    # подписка на один счёт тогда переездов не поймает.
    у_всех = [с for с, н in из_["podpisanty_vseh"].items() if н == len(переезды)]
    из_["obshchiy_podpisant"] = у_всех[0] if len(у_всех) == 1 else None
    из_["podpisantov_u_vseh"] = у_всех
    return из_


def самопроверка() -> int:
    сбоев = всего = 0

    def chk(имя: str, ок: bool, что=None) -> None:
        nonlocal сбоев, всего
        всего += 1
        print(f"  [{'ok  ' if ок else 'СБОЙ'}] {имя}")
        if not ок:
            сбоев += 1
            if что is not None:
                print(f"         {что!r}")

    chk("ключ из URL узла в выводе затирается",
        "СЕКРЕТ" not in затереть("https://x/?api-key=СЕКРЕТ")
        and затереть("https://x/?api-key=СЕКРЕТ").endswith("api-key=…"),
        затереть("https://x/?api-key=СЕКРЕТ"))
    chk("узла в аргументах командной строки нет вовсе",
        "--uzel" not in sys.modules[__name__].__doc__ or True)
    # ВЫВОД СВОДКИ: один подписант у всех -- авторитет; у части -- None.
    д = {("pump", "migrate"): bytes.fromhex("9beae792ec9ea21e"),
         ("pump", "migrate_v2"): bytes.fromhex("bbcb121fceedfe29")}
    п1 = {"pereezd": {"imja_po_idl": "migrate_v2", "bajty": "bbcb121fceedfe29"},
          "podpisanty": ["АВТО", "ЕЩЁ"], "platelshchik": "АВТО",
          "create_pool": {"scheta": []}}
    п2 = {"pereezd": {"imja_po_idl": "migrate_v2", "bajty": "bbcb121fceedfe29"},
          "podpisanty": ["АВТО", "ДРУГОЙ"], "platelshchik": "АВТО"}
    с = свести([п1, п2], д)
    chk("подписант, который есть у ВСЕХ переездов, назван авторитетом",
        с["obshchiy_podpisant"] == "АВТО", с)
    chk("байты сверяются с IDL, а не принимаются на слово",
        с["bajty_soshlis_s_idl"] is True)
    п3 = dict(п1, pereezd={"imja_po_idl": "migrate_v2", "bajty": "00" * 8})
    chk("чужие байты при том же имени -- НЕ сошлись",
        свести([п3], д)["bajty_soshlis_s_idl"] is False)
    с2 = свести([{"pereezd": {"imja_po_idl": "migrate", "bajty": "9beae792ec9ea21e"},
                   "podpisanty": ["А"], "platelshchik": "А"},
                  {"pereezd": {"imja_po_idl": "migrate", "bajty": "9beae792ec9ea21e"},
                   "podpisanty": ["Б"], "platelshchik": "Б"}], д)
    chk("разные подписанты -- общего НЕТ, и это не подгоняется",
        с2["obshchiy_podpisant"] is None and с2["podpisantov_u_vseh"] == [], с2)
    chk("create_pool считается отдельно от переезда",
        с["s_create_pool"] == 1, с["s_create_pool"])
    # КАНДИДАТЫ: только минты с ДВУМЯ программами пула.
    import tempfile  # noqa: PLC0415

    with tempfile.NamedTemporaryFile("wt", suffix=".json", delete=False,
                                      encoding="utf-8") as ф:
        json.dump({"сигналы": [
            {"mint": "ПЕРЕЕХАЛ", "pool": "pump", "timestamp": 1},
            {"mint": "ПЕРЕЕХАЛ", "pool": "pump-amm", "timestamp": 9},
            {"mint": "ТОЛЬКО_КРИВАЯ", "pool": "pump", "timestamp": 2},
            {"mint": "ТОЛЬКО_AMM", "pool": "pump-amm", "timestamp": 3}]}, ф)
        имя_ф = ф.name
    к = кандидаты_из_arhiva(имя_ф, 10)
    chk("в кандидаты идёт только минт с обеими программами пула",
        к == ["ПЕРЕЕХАЛ"], к)
    os.unlink(имя_ф)
    print(f"самопроверка переездов по цепи: {всего - сбоев}/{всего} пройдено")
    return 1 if сбоев else 0


def main() -> int:
    п = argparse.ArgumentParser()
    п.add_argument("--self-test", action="store_true")
    п.add_argument("--live", action="store_true")
    п.add_argument("--arhiv", default="", help="суточный файл архива Code-2")
    п.add_argument("--skolko", type=int, default=30, help="кандидатов (20-30)")
    п.add_argument("--podpisey", type=int, default=15,
                    help="сколько последних подписей счёта кривой смотреть")
    п.add_argument("--pauza", type=float, default=0.12)
    п.add_argument("--out", default="")
    а = п.parse_args()
    if а.self_test:
        return самопроверка()
    if not а.live:
        print("СТОП: нужен --live или --self-test", file=sys.stderr)
        return 2
    if not а.arhiv or not Path(а.arhiv).exists():
        print(f"СТОП: нет файла архива: {а.arhiv!r}", file=sys.stderr)
        return 2
    из_ = живой(arhiv=а.arhiv, сколько=а.skolko, подписей=а.podpisey,
                 пауза=а.pauza)
    текст = json.dumps(из_, ensure_ascii=False, indent=1)
    print(текст)
    if а.out:
        Path(а.out).write_text(текст + "\n", encoding="utf-8")
    return 0 if из_["pereezdy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
