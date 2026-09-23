#!/usr/bin/env python3
"""Из-за чего сообщения приходят без meta: проверка потолка версии транзакции.

Повод. В первом замере разбор через RPC давал 11 % решений, в свежем окне
после перезапуска -- 65 % (11 из 17). Это не мелочь: разбор через RPC
структурно поздний, getTransaction поддерживает только confirmed, то есть
1-2 слота и ~300 мс вместо 0 слотов и 0.3 мс. Если большинство сигналов
идёт этим путём, весь смысл слушать transactionSubscribe пропадает.

Рабочая гипотеза. Подписка детектора просит
maxSupportedTransactionVersion: 0, а getTransaction -- 1, и это не
вкусовая разница: на прогоне Части A узел прямо отвечал
-32015 "Transaction version (1) is not supported" при потолке 0, потому
что в блоках есть транзакции версии 1. Значит сообщения о таких
транзакциях приходят без meta, и мы падаем на RPC.

Проверка -- не рассуждением, а замером: в ОДНОМ окне на ОДНИХ и тех же
источниках открываются две подписки, потолок 0 и потолок 1, и сравнивается
доля сообщений с meta. По подписям, которые пришли в обе, видно прямо:
несла ли вторая meta там, где первая не несла.

Только чтение. Ни одного ордера.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_detector as BD  # noqa: E402

try:
    import websockets
except ImportError:                     # noqa: BLE001
    websockets = None

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "bloom_txver_probe.json"
ПОТОЛКИ = (0, 1)
ОБРАЗЦОВ = 5


def есть_meta(res: dict) -> bool:
    """Сообщение годно для разбора на месте ровно в том же смысле, в каком
    его проверяет детектор: внутри res.transaction есть meta."""
    tx = res.get("transaction")
    return isinstance(tx, dict) and isinstance(tx.get("meta"), dict)


def версия(res: dict):
    """Версия транзакции, если узел её прислал. Отсутствие поля -- это
    отсутствие, а не ноль: путать их уже случалось."""
    tx = res.get("transaction")
    if isinstance(tx, dict):
        if "version" in tx:
            return tx["version"]
        внутр = tx.get("transaction")
        if isinstance(внутр, dict) and "version" in внутр:
            return внутр["version"]
    return "ПОЛЯ_НЕТ"


class Счёт:
    def __init__(self) -> None:
        self.по_потолку = {p: {"messages": 0, "with_meta": 0, "without_meta": 0,
                               "versions_with_meta": {}, "versions_without_meta": {},
                               "signatures": {}, "acked": 0,
                               "errors": [], "samples_without_meta": []}
                           for p in ПОТОЛКИ}
        self.сбои: list = []

    def учесть(self, потолок: int, res: dict) -> None:
        c = self.по_потолку[потолок]
        c["messages"] += 1
        sig, _ = BD.подпись_и_слот(res)
        м = есть_meta(res)
        в = str(версия(res))
        ключ = "versions_with_meta" if м else "versions_without_meta"
        c[ключ][в] = c[ключ].get(в, 0) + 1
        c["with_meta" if м else "without_meta"] += 1
        if not м and len(c["samples_without_meta"]) < ОБРАЗЦОВ:
            c["samples_without_meta"].append(
                {"signature": sig, "version": в,
                 "keys": sorted((res.get("transaction") or {}).keys())
                         if isinstance(res.get("transaction"), dict)
                         else sorted(res.keys())})
        if sig:
            # Одна и та же подпись может прийти дважды; meta хотя бы в
            # одном сообщении -- значит разбор на месте был возможен.
            было = c["signatures"].get(sig)
            c["signatures"][sig] = bool(было) or м


def сравнение(счёт: Счёт) -> dict:
    a, b = ПОТОЛКИ
    ca, cb = счёт.по_потолку[a], счёт.по_потолку[b]
    общие = set(ca["signatures"]) & set(cb["signatures"])
    починено = sorted(s for s in общие
                      if not ca["signatures"][s] and cb["signatures"][s])
    сломано = sorted(s for s in общие
                     if ca["signatures"][s] and not cb["signatures"][s])
    def доля(c):
        return (round(c["with_meta"] / c["messages"], 4) if c["messages"] else None)
    итог = {"ceilings": list(ПОТОЛКИ),
            "share_with_meta": {str(a): доля(ca), str(b): доля(cb)},
            "signatures_common": len(общие),
            "fixed_by_higher_ceiling": починено[:20],
            "fixed_count": len(починено),
            "broken_by_higher_ceiling": сломано[:20],
            "broken_count": len(сломано)}
    if not общие:
        итог["verdict"] = ("общих подписей нет -- за окно ни один источник не "
                           "сделал сделки, вывод делать не на чем")
    elif починено and not сломано:
        итог["verdict"] = (f"потолок {b} несёт meta там, где потолок {a} не несёт: "
                           f"{len(починено)} подписей из {len(общие)} общих. "
                           "Причина отставания найдена -- поднять потолок в подписке")
    elif not починено and not сломано:
        итог["verdict"] = (f"по общим подписям разницы нет: потолок версии не "
                           f"объясняет сообщения без meta, искать другую причину")
    else:
        итог["verdict"] = (f"разница в обе стороны: починено {len(починено)}, "
                           f"сломано {len(сломано)} -- разбирать поштучно")
    return итог


async def _слушать(счёт: Счёт, ключ: str, потолок: int, адреса: list,
                   стоп_ts: float) -> None:
    c = счёт.по_потолку[потолок]
    try:
        async with websockets.connect(BD.ws_url(ключ, True), ping_interval=20,
                                      ping_timeout=30,
                                      max_size=32 * 1024 * 1024) as ws:
            for i, a in enumerate(адреса, 1):
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": i, "method": "transactionSubscribe",
                    "params": [{"accountInclude": [a], "failed": False,
                                "vote": False},
                               {"commitment": "processed",
                                "transactionDetails": "full",
                                "encoding": "jsonParsed", "showRewards": False,
                                "maxSupportedTransactionVersion": потолок}]}))
            while time.time() < стоп_ts:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                msg = json.loads(raw)
                if "id" in msg and ("result" in msg or "error" in msg):
                    if "error" in msg:
                        c["errors"].append(str(msg["error"])[:200])
                    else:
                        c["acked"] += 1
                    continue
                if msg.get("method") != "transactionNotification":
                    continue
                счёт.учесть(потолок, (msg.get("params") or {}).get("result") or {})
    except Exception as exc:                        # noqa: BLE001
        счёт.сбои.append(f"потолок {потолок}: {type(exc).__name__}: {str(exc)[:200]}")


async def прогон(секунд: float, задачи: tuple, снимок: Path) -> dict:
    ключ = os.environ.get("HELIUS_API_KEY") or ""
    if not ключ:
        raise RuntimeError("HELIUS_API_KEY не задан")
    if websockets is None:
        raise RuntimeError("нет модуля websockets")
    конф = json.loads(снимок.read_text(encoding="utf-8"))
    источники = BD.источники_из_конфига(конф, задачи)
    адреса = sorted(источники)
    if not адреса:
        raise RuntimeError("источников не нашлось в снимке конфига")
    счёт = Счёт()
    стоп = time.time() + секунд
    # Жёсткий будильник поверх окна. Прогон уже висел дольше окна: если
    # закрытие соединения или recv застрянут, замер не должен висеть до
    # таймаута всего прогона -- лучше неполные данные с честной пометкой,
    # чем тишина на полчаса.
    задачи_ws = [asyncio.create_task(_слушать(счёт, ключ, p, адреса, стоп))
                 for p in ПОТОЛКИ]
    просрочено = False
    try:
        await asyncio.wait_for(asyncio.gather(*задачи_ws, return_exceptions=True),
                               timeout=секунд + 45)
    except asyncio.TimeoutError:
        просрочено = True
        счёт.сбои.append(f"замер не уложился в окно {секунд:.0f} с + 45 с -- "
                          "подписки сняты будильником, данные могут быть неполными")
        for t in задачи_ws:
            t.cancel()
        await asyncio.gather(*задачи_ws, return_exceptions=True)
    итог = {"checked_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "seconds": секунд, "sources": len(адреса), "tasks": list(задачи),
            "failures": счёт.сбои,
            "watchdog_fired": просрочено,
            "by_ceiling": {str(p): {k: v for k, v in счёт.по_потолку[p].items()
                                    if k != "signatures"}
                           for p in ПОТОЛКИ},
            "signatures_seen": {str(p): len(счёт.по_потолку[p]["signatures"])
                                for p in ПОТОЛКИ},
            "comparison": сравнение(счёт)}
    return итог


def self_test() -> int:
    checks = []

    def chk(имя, ок, факт=""):
        checks.append((имя, bool(ок), факт))

    def подпись(буква: str) -> str:
        """Подписи в тестах должны проходить тот же SIG_RE, что и в бою:
        иначе тест проверяет не то, что работает."""
        return (буква * 88)[:88]

    def res(*, sig=None, meta=True, версия_поле=None, слот=7):
        sig = sig or подпись("S")
        внутр = {"signatures": [sig], "message": {"accountKeys": []}}
        tx = {"transaction": внутр}
        if meta:
            tx["meta"] = {"err": None, "fee": 5000}
        if версия_поле is not None:
            tx["version"] = версия_поле
        return {"signature": sig, "slot": слот, "transaction": tx}

    chk("сообщение с meta распознано", есть_meta(res()) is True)
    chk("сообщение без meta распознано", есть_meta(res(meta=False)) is False)
    chk("meta не словарём не считается meta",
        есть_meta({"transaction": {"meta": "нет"}}) is False)
    chk("отсутствие поля версии названо отсутствием",
        версия(res()) == "ПОЛЯ_НЕТ", версия(res()))
    chk("версия 0 не путается с отсутствием",
        версия(res(версия_поле=0)) == 0, версия(res(версия_поле=0)))
    chk("версия legacy читается",
        версия(res(версия_поле="legacy")) == "legacy")

    # потолок 1 несёт meta там, где потолок 0 не несёт
    с = Счёт()
    с.учесть(0, res(sig=подпись("A"), meta=False, версия_поле=1))
    с.учесть(1, res(sig=подпись("A"), meta=True, версия_поле=1))
    с.учесть(0, res(sig=подпись("B"), meta=True, версия_поле=0))
    с.учесть(1, res(sig=подпись("B"), meta=True, версия_поле=0))
    ср = сравнение(с)
    chk("починенная подпись найдена",
        ср["fixed_by_higher_ceiling"] == [подпись("A")], ср["fixed_by_higher_ceiling"])
    chk("сломанных нет", ср["broken_count"] == 0, ср)
    chk("вердикт называет причину найденной",
        "Причина отставания найдена" in ср["verdict"], ср["verdict"])
    chk("доля с meta посчитана по сообщениям",
        ср["share_with_meta"] == {"0": 0.5, "1": 1.0}, ср["share_with_meta"])
    chk("версии без meta посчитаны отдельно",
        с.по_потолку[0]["versions_without_meta"] == {"1": 1},
        с.по_потолку[0]["versions_without_meta"])
    chk("образец без meta сохранён с версией",
        с.по_потолку[0]["samples_without_meta"][0]["version"] == "1",
        с.по_потолку[0]["samples_without_meta"])

    # разницы нет -- значит причина другая, и это сказано прямо
    с2 = Счёт()
    с2.учесть(0, res(sig=подпись("C"), meta=False))
    с2.учесть(1, res(sig=подпись("C"), meta=False))
    ср2 = сравнение(с2)
    chk("без разницы вердикт отправляет искать другую причину",
        "другую причину" in ср2["verdict"], ср2["verdict"])

    # пустое окно -- не вывод, а отсутствие данных
    ср3 = сравнение(Счёт())
    chk("пустое окно не выдаётся за результат",
        "вывод делать не на чем" in ср3["verdict"], ср3["verdict"])

    # два сообщения по одной подписи: meta хотя бы в одном -- разбор был возможен
    с4 = Счёт()
    с4.учесть(0, res(sig=подпись("D"), meta=False))
    с4.учесть(0, res(sig=подпись("D"), meta=True))
    chk("повтор с meta не затирается сообщением без meta",
        с4.по_потолку[0]["signatures"][подпись("D")] is True,
        с4.по_потолку[0]["signatures"])

    прошло = sum(1 for _, ок, _ in checks if ок)
    for имя, ок, факт in checks:
        print(f"  [{'ok  ' if ок else 'ПЛОХО'}] {имя}" + (f" -- {факт}" if not ок else ""))
    print(f"самопроверка замера потолка версий: {прошло}/{len(checks)} пройдено")
    return 0 if прошло == len(checks) else 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--seconds", type=float, default=600.0)
    p.add_argument("--tasks", default=os.environ.get("BLOOM_TASKS", "BATCH-5,BATCH-3"))
    p.add_argument("--config", default=str(REPO_ROOT / "data" / "final" /
                                           "20260923T145755Z" / "konfig.json"))
    a = p.parse_args()
    if a.self_test:
        return self_test()
    задачи = tuple(x.strip() for x in a.tasks.split(",") if x.strip())
    итог = asyncio.run(прогон(a.seconds, задачи, Path(a.config)))
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(итог, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(json.dumps(итог, ensure_ascii=False, indent=2))
    print("--- коротко ---")
    for потолок in ПОТОЛКИ:
        c = итог["by_ceiling"][str(потолок)]
        print(f"потолок {потолок}: сообщений {c['messages']}, с meta "
              f"{c['with_meta']}, без meta {c['without_meta']}, подписок "
              f"подтверждено {c['acked']}, отказов {len(c['errors'])}")
        print(f"    версии БЕЗ meta: {c['versions_without_meta']}")
        print(f"    версии С meta:   {c['versions_with_meta']}")
    print(f"вердикт: {итог['comparison']['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
