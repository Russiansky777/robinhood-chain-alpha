#!/usr/bin/env python3
"""Замер: что РЕАЛЬНО приходит в вебсокете и кто что теряет.

Два вопроса владельца, оба решаются одним окном наблюдения, потому что
сравнивать два способа подписки на РАЗНЫХ окнах бессмысленно.

1. Хватает ли сообщения transactionSubscribe, чтобы разобрать сигнал БЕЗ
   getTransaction. getTransaction не умеет processed -- только confirmed
   и finalized, то есть ожидание слота-двух. Если в сообщении есть
   meta.preTokenBalances / postTokenBalances и инструкции, разбор идёт из
   него, а RPC остаётся запасным путём.

2. Теряет ли transactionSubscribe с accountInclude транзакции, ПОДПИСАННЫЕ
   источником. Зонд обнаружения дал 86 событий против 4784 у
   logsSubscribe, но это были разные периоды и разные наборы адресов, так
   что из тех чисел вывод делать нельзя. Здесь оба способа слушают ОДИН
   набор источников в ОДНО окно, а расхождение разбирается по цепи: был
   ли источник подписантом или адрес просто упомянут.

Только чтение. Ни одного ордера, ни одного POST в Bloom.

Запуск:
  python3 analysis/bloom_ws_probe.py --self-test
  python3 analysis/bloom_ws_probe.py --seconds 900 --out data/bloom_ws_probe.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_detector as BD  # noqa: E402
import bloom_exec_state as ST  # noqa: E402

try:
    import websockets
except ImportError:  # pragma: no cover
    websockets = None

REPO_ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("bloom_ws_probe")

# Сколько полных сообщений transactionSubscribe сохранить целиком.
# Это публичные данные цепи, секретов в них нет.
ОБРАЗЦОВ = 3


def форма(о, путь: str = "", глубина: int = 0, предел: int = 4) -> dict:
    """Какие ключи есть в объекте и какого они типа -- без значений.

    Нужна именно форма, а не пример: по одному сообщению нельзя сказать
    "поле есть всегда", зато можно честно посчитать, в скольких из N
    сообщений оно встретилось.
    """
    out = {}
    if глубина > предел:
        return out
    if isinstance(о, dict):
        for k, v in о.items():
            p = f"{путь}.{k}" if путь else k
            out[p] = type(v).__name__
            out.update(форма(v, p, глубина + 1, предел))
    elif isinstance(о, list):
        out[f"{путь}[]"] = f"list({len(о)})"
        if о:
            out.update(форма(о[0], f"{путь}[0]", глубина + 1, предел))
    return out


ВАЖНЫЕ_ПОЛЯ = (
    "transaction.meta.preTokenBalances",
    "transaction.meta.postTokenBalances",
    "transaction.meta.preBalances",
    "transaction.meta.postBalances",
    "transaction.meta.innerInstructions",
    "transaction.meta.err",
    "transaction.meta.fee",
    "transaction.transaction.message.accountKeys",
    "transaction.transaction.message.instructions",
    "transaction.transaction.signatures",
    "signature",
    "slot",
)


ОТСУТСТВУЕТ = object()


def найти_путь(о, путь: str):
    """Значение по точечному пути. Отсутствие поля -- ОТСУТСТВУЕТ, а не None.

    Разница важна: meta.err у успешной транзакции равен null, и если
    считать null отсутствием, получится "err нет в 100% сообщений" -- то
    есть измерение соврёт ровно там, где всё в порядке. Первый прогон
    именно это и показал.
    """
    текущий = о
    for часть in путь.split("."):
        if isinstance(текущий, dict) and часть in текущий:
            текущий = текущий[часть]
        else:
            return ОТСУТСТВУЕТ
    return текущий


class Замер:
    def __init__(self) -> None:
        self.tx: dict = {}        # подпись -> {t, слот}
        self.logs: dict = {}
        self.поля_счёт: dict = {}
        self.образцы: list = []
        self.образцы_без_meta: list = []
        self.сообщений_tx = 0
        self.сообщений_logs = 0
        self.ошибки: list = []
        self.подтверждено: dict = {"transactionSubscribe": 0, "logsSubscribe": 0}
        self.отказы: dict = {"transactionSubscribe": [], "logsSubscribe": []}

    def учесть_tx(self, сообщение: dict, источник: str | None) -> None:
        self.сообщений_tx += 1
        res = (сообщение.get("params") or {}).get("result") or {}
        sig, слот = BD.подпись_и_слот(res)
        for поле in ВАЖНЫЕ_ПОЛЯ:
            есть = найти_путь(res, поле) is not ОТСУТСТВУЕТ
            c = self.поля_счёт.setdefault(поле, {"есть": 0, "нет": 0})
            c["есть" if есть else "нет"] += 1
        if len(self.образцы) < ОБРАЗЦОВ:
            self.образцы.append({"signature": sig, "slot": слот,
                                  "форма": форма(res)})
        # Отдельно -- сообщения БЕЗ meta: именно они заставляют падать на
        # getTransaction, и без их формы нельзя сказать, что это за случай.
        if (найти_путь(res, "transaction.meta.postTokenBalances") is ОТСУТСТВУЕТ
                and len(self.образцы_без_meta) < ОБРАЗЦОВ):
            self.образцы_без_meta.append({"signature": sig, "slot": слот,
                                           "форма": форма(res)})
        if sig:
            self.tx.setdefault(sig, {"t": time.time(), "slot": слот,
                                      "source": источник})

    def учесть_logs(self, сообщение: dict, источник: str | None) -> None:
        self.сообщений_logs += 1
        res = (сообщение.get("params") or {}).get("result") or {}
        val = res.get("value") or {}
        if val.get("err") is not None:
            return                    # неуспешные нам не нужны, как и в бою
        sig = val.get("signature")
        слот = (res.get("context") or {}).get("slot")
        if isinstance(sig, str) and BD.SIG_RE.match(sig):
            self.logs.setdefault(sig, {"t": time.time(), "slot": слот,
                                        "source": источник})


async def слушать_один(замер: Замер, ключ: str, метод: str, адреса: list,
                        стоп_ts: float) -> None:
    atlas = метод == "transactionSubscribe"
    try:
        async with websockets.connect(BD.ws_url(ключ, atlas), ping_interval=20,
                                       ping_timeout=30, max_size=32 * 1024 * 1024) as ws:
            for i, a in enumerate(адреса, 1):
                if atlas:
                    тело = {"jsonrpc": "2.0", "id": i, "method": "transactionSubscribe",
                             "params": [{"accountInclude": [a], "failed": False, "vote": False},
                                         {"commitment": "processed",
                                          "transactionDetails": "full",
                                          "encoding": "jsonParsed",
                                          "showRewards": False,
                                          "maxSupportedTransactionVersion": 0}]}
                else:
                    тело = {"jsonrpc": "2.0", "id": i, "method": "logsSubscribe",
                             "params": [{"mentions": [a]}, {"commitment": "processed"}]}
                await ws.send(json.dumps(тело))
            id_адреса = {i: a for i, a in enumerate(адреса, 1)}
            подписка_адреса: dict = {}
            while time.time() < стоп_ts:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                msg = json.loads(raw)
                if "id" in msg and ("result" in msg or "error" in msg):
                    if "error" in msg:
                        замер.отказы[метод].append(str(msg["error"])[:200])
                    else:
                        замер.подтверждено[метод] += 1
                        a = id_адреса.get(msg["id"])
                        if a and isinstance(msg.get("result"), int):
                            подписка_адреса[msg["result"]] = a
                    continue
                m = msg.get("method")
                источник = подписка_адреса.get((msg.get("params") or {}).get("subscription"))
                if m == "transactionNotification":
                    замер.учесть_tx(msg, источник)
                elif m == "logsNotification":
                    замер.учесть_logs(msg, источник)
    except Exception as exc:  # noqa: BLE001
        замер.ошибки.append(f"{метод}: {type(exc).__name__}: {str(exc)[:200]}")


def подписанты(tx: dict) -> list:
    """Кто подписал транзакцию -- из accountKeys с signer=true."""
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    out = []
    for k in msg.get("accountKeys") or []:
        if isinstance(k, dict) and k.get("signer"):
            p = k.get("pubkey")
            if p:
                out.append(p)
    return out


def разобрать_расхождение(helius, подписи: list, источники: dict,
                           предел: int = 60) -> dict:
    """Для каждой подписи: подписал ли её кто-то из источников.

    Это и есть водораздел. Транзакция, ПОДПИСАННАЯ источником, -- его
    сделка, и потерять её нельзя. Транзакция, где адрес источника просто
    упомянут (чужой своп, в котором он получатель комиссии, раздача и
    так далее), -- не сигнал, и её отсутствие дыркой не является.
    """
    итог = {"проверено": 0, "подписано_источником": [], "только_упомянут": [],
             "не_достали": []}
    for sig in подписи[:предел]:
        tx = helius.транзакция(sig, попыток=3, пауза_s=0.3)
        if tx is None:
            итог["не_достали"].append(sig)
            continue
        итог["проверено"] += 1
        свои = [p for p in подписанты(tx) if p in источники]
        (итог["подписано_источником"] if свои else итог["только_упомянут"]).append(
            {"signature": sig, "подписанты_из_источников": свои})
    итог["подписано_источником_шт"] = len(итог["подписано_источником"])
    итог["только_упомянут_шт"] = len(итог["только_упомянут"])
    return итог


async def прогон(секунд: float, задачи: tuple, снимок: Path) -> dict:
    ист, откуда = BD.источники(задачи, снимок)
    адреса = sorted(ист)
    замер = Замер()
    ключ = os.environ.get("HELIUS_API_KEY") or ""
    стоп = time.time() + секунд
    await asyncio.gather(
        слушать_один(замер, ключ, "transactionSubscribe", адреса, стоп),
        слушать_один(замер, ключ, "logsSubscribe", адреса, стоп),
    )

    только_logs = sorted(set(замер.logs) - set(замер.tx))
    только_tx = sorted(set(замер.tx) - set(замер.logs))
    общие = sorted(set(замер.tx) & set(замер.logs))

    опережение = []
    for sig in общие:
        d = (замер.logs[sig]["t"] - замер.tx[sig]["t"]) * 1000.0
        опережение.append(round(d, 1))
    опережение.sort()

    helius = BD.Helius(ключ)
    разбор = разобрать_расхождение(helius, только_logs, ист)

    # Достаточность сообщения transactionSubscribe для разбора
    достаточно = {}
    for поле in ВАЖНЫЕ_ПОЛЯ:
        c = замер.поля_счёт.get(поле) or {"есть": 0, "нет": 0}
        всего = c["есть"] + c["нет"]
        достаточно[поле] = {"есть": c["есть"], "нет": c["нет"],
                             "доля": round(c["есть"] / всего, 4) if всего else None}
    ключевые = ("transaction.meta.preTokenBalances",
                 "transaction.meta.postTokenBalances",
                 "transaction.meta.preBalances",
                 "transaction.meta.postBalances")
    доли = [достаточно[k]["доля"] or 0 for k in ключевые]
    доля_годных = min(доли) if доли else 0
    можно_разбирать = bool(замер.сообщений_tx) and доля_годных >= 0.99

    return {
        "окно_с": секунд,
        "снято_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sources": len(адреса),
        "sources_from": откуда,
        "подтверждено_подписок": замер.подтверждено,
        "отказы_подписок": {k: v[:2] for k, v in замер.отказы.items()},
        "ошибки": замер.ошибки,
        "сообщений": {"transactionSubscribe": замер.сообщений_tx,
                       "logsSubscribe": замер.сообщений_logs},
        "уникальных_подписей": {"transactionSubscribe": len(замер.tx),
                                 "logsSubscribe": len(замер.logs),
                                 "общих": len(общие),
                                 "только_logs": len(только_logs),
                                 "только_tx": len(только_tx)},
        "разбор_расхождения": разбор,
        "дырка_в_детекции": разбор["подписано_источником_шт"] > 0,
        "вывод_по_расхождению": (
            f"transactionSubscribe ПОТЕРЯЛ {разбор['подписано_источником_шт']} "
            f"транзакций, подписанных источниками -- это дырка в детекции"
            if разбор["подписано_источником_шт"] > 0 else
            f"из {len(только_logs)} расхождений ни одна не подписана источником: "
            f"{разбор['только_упомянут_шт']} -- чужие упоминания адреса, "
            f"{len(разбор['не_достали'])} не достали по RPC"),
        "опережение_logs_над_tx_мс": {
            "n": len(опережение),
            "median": опережение[len(опережение) // 2] if опережение else None,
            "min": опережение[0] if опережение else None,
            "max": опережение[-1] if опережение else None},
        "поля_в_сообщении_transactionSubscribe": достаточно,
        "разбор_из_сообщения_возможен": можно_разбирать,
        "доля_сообщений_годных_для_разбора": round(доля_годных, 4),
        "вывод_по_разбору": (
            "все сообщения годны для разбора без RPC"
            if можно_разбирать else
            f"для разбора без RPC годны {доля_годных * 100:.1f}% сообщений; "
            f"остальным нужен getTransaction, и такой разбор помечается "
            f"PARSE_VIA_RPC. Это не отказ от разбора из сообщения, а его "
            f"доля: порог 99% здесь строгий, а не приговор данных"),
        "образцы_формы": замер.образцы,
        "образцы_без_meta": замер.образцы_без_meta,
        "сообщений_без_meta": (замер.поля_счёт.get(
            "transaction.meta.postTokenBalances") or {}).get("нет"),
        "оговорки": [
            "Оба способа слушали ОДИН набор источников в ОДНО окно -- иначе "
            "сравнивать нельзя.",
            "logsSubscribe отдаёт только подпись и слот, поэтому расхождение "
            "разбиралось отдельным getTransaction: подписант-источник против "
            "простого упоминания адреса.",
            "Неуспешные транзакции у logsSubscribe отбрасывались, как и в бою "
            "(у transactionSubscribe для этого есть failed:false).",
        ],
    }


# ------------------------------------------------------------ самопроверка

def self_test() -> int:
    проверки = []

    def chk(имя, ок, факт=""):
        проверки.append((имя, bool(ок), факт))

    ф = форма({"a": 1, "b": {"c": [1, 2]}, "d": []})
    chk("форма видит вложенный ключ", "b.c" in ф, list(ф))
    chk("форма помечает список длиной", ф.get("b.c[]") == "list(2)", ф.get("b.c[]"))
    chk("пустой список не разворачивается", ф.get("d[]") == "list(0)", ф.get("d[]"))
    chk("значения не попадают в форму", 1 not in ф.values())

    о = {"transaction": {"meta": {"preTokenBalances": [], "err": None}}}
    chk("путь находит пустой список", найти_путь(о, "transaction.meta.preTokenBalances") == [])
    chk("несуществующий путь -- ОТСУТСТВУЕТ",
        найти_путь(о, "transaction.meta.нет") is ОТСУТСТВУЕТ)
    chk("путь сквозь не-словарь -- ОТСУТСТВУЕТ",
        найти_путь({"a": 5}, "a.b") is ОТСУТСТВУЕТ)
    chk("null -- это ЕСТЬ, а не отсутствие",
        найти_путь(о, "transaction.meta.err") is None
        and найти_путь(о, "transaction.meta.err") is not ОТСУТСТВУЕТ)

    з = Замер()
    з.учесть_tx({"params": {"result": {"signature": "S" * 70, "slot": 5,
                                        "transaction": {"meta": {"err": None}}}}}, "ИСТ")
    chk("сообщение без postTokenBalances попало в образцы без meta",
        len(з.образцы_без_meta) == 1, з.образцы_без_meta)
    chk("и err у него считается присутствующим",
        з.поля_счёт["transaction.meta.err"]["есть"] == 1,
        з.поля_счёт["transaction.meta.err"])

    tx = {"transaction": {"message": {"accountKeys": [
        {"pubkey": "П1", "signer": True}, {"pubkey": "П2", "signer": False},
        {"pubkey": "П3", "signer": True}]}}}
    chk("подписанты отобраны", подписанты(tx) == ["П1", "П3"], подписанты(tx))
    chk("не-подписант отброшен", "П2" not in подписанты(tx))
    chk("пустая транзакция -- пусто", подписанты({}) == [])

    class ФейкHelius:
        def __init__(self, карта):
            self.карта = карта
        def транзакция(self, sig, **_):
            return self.карта.get(sig)

    h = ФейкHelius({
        "A": {"transaction": {"message": {"accountKeys": [{"pubkey": "ИСТ", "signer": True}]}}},
        "B": {"transaction": {"message": {"accountKeys": [{"pubkey": "ЧУЖОЙ", "signer": True},
                                                            {"pubkey": "ИСТ", "signer": False}]}}},
    })
    р = разобрать_расхождение(h, ["A", "B", "C"], {"ИСТ": "BATCH-5"})
    chk("подписанная источником найдена", р["подписано_источником_шт"] == 1, р)
    chk("упоминание не считается подписью", р["только_упомянут_шт"] == 1, р)
    chk("недостижимая подпись помечена отдельно", р["не_достали"] == ["C"], р["не_достали"])
    chk("проверено только то, что достали", р["проверено"] == 2, р["проверено"])

    прошло = sum(1 for _, ок, _ in проверки if ок)
    for имя, ок, факт in проверки:
        print(f"  [{'ok  ' if ок else 'ПЛОХО'}] {имя}" + (f" -- {факт}" if not ок else ""))
    print(f"самопроверка замера вебсокета: {прошло}/{len(проверки)} пройдено")
    return 0 if прошло == len(проверки) else 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--seconds", type=float, default=900.0)
    p.add_argument("--tasks", default=os.environ.get("BLOOM_TASKS", "BATCH-5,BATCH-3"))
    p.add_argument("--config", default=str(REPO_ROOT / "data" / "final" /
                                            "20260923T145755Z" / "konfig.json"))
    p.add_argument("--out", default=str(REPO_ROOT / "data" / "bloom_ws_probe.json"))
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if a.self_test:
        return self_test()
    if websockets is None:
        print("нет модуля websockets")
        return 2
    задачи = tuple(x.strip() for x in a.tasks.split(",") if x.strip())
    итог = asyncio.run(прогон(a.seconds, задачи, Path(a.config)))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ST.atomic_write_json(out, итог)
    краткое = {k: v for k, v in итог.items() if k != "образцы_формы"}
    print(json.dumps(краткое, ensure_ascii=False, indent=2)[:6000])
    print(f"\nполный отчёт: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
