#!/usr/bin/env python3
"""Преемник 4vw54BmA: что делает источник после 04:00Z 10.10 и куда ушли его SOL.

Зачем. С 04:00Z 10.10 источник 4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9 перестал
покупать и шлёт около 85 транзакций в минуту через программу
DhpyNWkdxFh3DRPsBrwRwrK3TYC5t7Q4arnSvf3t84HY, у которой Code-1 инструкции кривой не нашёл.
Поиск по публичному вебу про этот адрес ничего не даёт -- значит ответ только по цепи.

Что делает.
  --chto   (пункт а) Читает САМУ программу: владелец (загрузчик), исполняемость, размер,
           и, если это обновляемый загрузчик, счёт ProgramData -- авторитет обновления и
           слот последней заливки. Дальше берёт свежие транзакции источника, оставляет те,
           что касаются программы, и разбирает их: дискриминатор и длина данных
           инструкции, роли счетов, журнал программы, расход вычислений, движение SOL.
  --perepis (для пункта б) Перепись подписей источника за окно: сколько транзакций в
           минуту, с какого момента темп сменился. Нужна, чтобы знать цену полного
           разбора окна ДО того, как его заказывать.
  --dengi   (пункт б) Движение SOL: по разобранным транзакциям -- кому ушло и от кого
           пришло, с суммами и первым появлением адреса-получателя.

Только чтение. Helius, темп -- PODB_HELIUS_RPS (потолок владельца 6 запросов в секунду на
все мои процессы вместе). Выход: data/podbivka/preemnik_*.json.
"""
from __future__ import annotations

import argparse
import base64
import collections
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
RPS = float(os.environ.get("PODB_HELIUS_RPS") or 6.0)
ГЕРОЙ = "4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9"
ПРОГРАММА = "DhpyNWkdxFh3DRPsBrwRwrK3TYC5t7Q4arnSvf3t84HY"
ЗАГРУЗЧИК_V3 = "BPFLoaderUpgradeab1e11111111111111111111111"
СИСТЕМНАЯ = "11111111111111111111111111111111"
LAMPORT = 1_000_000_000


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


def b58(данные: bytes) -> str:
    алф = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    н = int.from_bytes(данные, "big")
    из_ = ""
    while н:
        н, о = divmod(н, 58)
        из_ = алф[о] + из_
    for b in данные:
        if b:
            break
        из_ = "1" + из_
    return из_ or "1"


def данные_инструкции(стр: str) -> bytes:
    """Данные инструкции приходят base58 (jsonParsed) -- вернуть байтами."""
    алф = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    н = 0
    for c in стр:
        if c not in алф:
            return b""
        н = н * 58 + алф.index(c)
    длина = (н.bit_length() + 7) // 8
    нулей = len(стр) - len(стр.lstrip("1"))
    return b"\x00" * нулей + н.to_bytes(длина, "big")


def программа_данные(ключ: str) -> str:
    """PDA счёта ProgramData обновляемого загрузчика: seeds = [program_id]."""
    from solders.pubkey import Pubkey  # noqa: PLC0415
    пда, _ = Pubkey.find_program_address([bytes(Pubkey.from_string(ключ))],
                                         Pubkey.from_string(ЗАГРУЗЧИК_V3))
    return str(пда)


def что_за_программа(rpc, темп: Темп) -> dict:
    """Пункт а, часть первая: сама программа по цепи."""
    темп.ждать()
    о = rpc.call("getAccountInfo", [ПРОГРАММА, {"encoding": "base64"}])
    v = (о or {}).get("value")
    из_: dict = {"адрес": ПРОГРАММА, "счёт_есть": bool(v)}
    if not v:
        из_["почему"] = "счёта нет: адрес не программа и не счёт"
        return из_
    сырое = base64.b64decode((v.get("data") or ["", ""])[0])
    из_.update({"владелец": v.get("owner"), "исполняемая": v.get("executable"),
                "байт": len(сырое), "лампортов": v.get("lamports"),
                "аренда_эпоха": v.get("rentEpoch")})
    if v.get("owner") == ЗАГРУЗЧИК_V3 and len(сырое) >= 36:
        from solders.pubkey import Pubkey  # noqa: PLC0415
        вид = int.from_bytes(сырое[:4], "little")
        из_["вид_счёта_загрузчика"] = вид          # 2 = Program
        из_["ссылка_на_programdata"] = str(Pubkey.from_bytes(сырое[4:36]))
        пд = программа_данные(ПРОГРАММА)
        из_["programdata_pda"] = пд
        темп.ждать()
        о2 = rpc.call("getAccountInfo", [пд, {"encoding": "base64"}])
        v2 = (о2 or {}).get("value")
        if v2:
            с2 = base64.b64decode((v2.get("data") or ["", ""])[0])
            из_["programdata_байт"] = len(с2)
            if len(с2) >= 45:
                из_["залито_в_слоте"] = int.from_bytes(с2[4:12], "little")
                есть_авт = с2[12]
                из_["авторитет_обновления"] = (
                    str(Pubkey.from_bytes(с2[13:45])) if есть_авт else None)
                из_["обновление_закрыто"] = not есть_авт
    return из_

def разбор_транзакции(подпись: str, tx: dict) -> dict:
    """Что транзакция делает: инструкции, журнал, расход вычислений, движение SOL."""
    м = (tx or {}).get("meta") or {}
    сообщение = ((tx or {}).get("transaction") or {}).get("message") or {}
    ключи = [k.get("pubkey") if isinstance(k, dict) else k
             for k in (сообщение.get("accountKeys") or [])]
    инстр = []
    for и in (сообщение.get("instructions") or []):
        д = и.get("data") or ""
        б = данные_инструкции(д) if д else b""
        инстр.append({"программа": и.get("programId"), "байт_данных": len(б),
                      "дискриминатор_hex": б[:8].hex() if len(б) >= 8 else None,
                      "данные_hex": б[:32].hex() if б else None,
                      "счетов": len(и.get("accounts") or []),
                      "разобрано": (и.get("parsed") or {}).get("type")
                      if isinstance(и.get("parsed"), dict) else None})
    внутр = collections.Counter()
    for гр in (м.get("innerInstructions") or []):
        for и in (гр.get("instructions") or []):
            т = (и.get("parsed") or {}).get("type") if isinstance(и.get("parsed"), dict) else None
            внутр[f"{и.get('programId')}/{т or 'сырая'}"] += 1
    до = м.get("preBalances") or []
    после = м.get("postBalances") or []
    дельты = {}
    for i, к in enumerate(ключи):
        if i < len(до) and i < len(после) and после[i] != до[i]:
            дельты[к] = (после[i] - до[i]) / LAMPORT
    return {"подпись": подпись, "слот": (tx or {}).get("slot"),
            "ts": (tx or {}).get("blockTime"),
            "ошибка": м.get("err"), "плата_sol": (м.get("fee") or 0) / LAMPORT,
            "вычислений": м.get("computeUnitsConsumed"),
            "инструкции": инстр, "внутренние": dict(внутр),
            "журнал": (м.get("logMessages") or [])[:14],
            "дельты_sol": дельты,
            "счетов_всего": len(ключи)}


def подписи(rpc, темп: Темп, адрес: str, с_ts: int, предел: int) -> list:
    """Все подписи адреса не старше с_ts, страницами по 1000."""
    из_: list = []
    до = None
    while len(из_) < предел:
        пар = {"limit": 1000}
        if до:
            пар["before"] = до
        темп.ждать()
        о = rpc.call("getSignaturesForAddress", [адрес, пар])
        стр = о or []
        if not стр:
            break
        for x in стр:
            if (x.get("blockTime") or 0) < с_ts:
                return из_
            из_.append(x)
        до = стр[-1]["signature"]
        print(f"  подписей {len(из_)}, последняя "
              f"{time.strftime('%H:%M:%SZ', time.gmtime(стр[-1].get('blockTime') or 0))}",
              flush=True)
    return из_


def перепись(стр: list) -> dict:
    """Темп по минутам: когда сменился почерк."""
    по_мин: collections.Counter = collections.Counter()
    ошибок = 0
    for x in стр:
        ts = x.get("blockTime") or 0
        по_мин[time.strftime("%Y-%m-%dT%H:%M", time.gmtime(ts - ts % 60))] += 1
        if x.get("err"):
            ошибок += 1
    мин = sorted(по_мин.items())
    по_часу: collections.Counter = collections.Counter()
    for k, n in мин:
        по_часу[k[:13]] += n
    return {"подписей": len(стр), "с_ошибкой": ошибок,
            "минут": len(мин), "по_часу": dict(sorted(по_часу.items())),
            "первая_минута": мин[0][0] if мин else None,
            "последняя_минута": мин[-1][0] if мин else None,
            "темп_в_минуту_медиана": (sorted(n for _, n in мин)[len(мин) // 2]
                                      if мин else None),
            "по_минутам": dict(мин[:400])}


def главное(а) -> int:  # noqa: PLR0912, PLR0915
    import c2_common as C  # noqa: PLC0415
    ключ = (os.environ.get("HELIUS_API_KEY2") or os.environ.get("HELIUS_API_KEY")
            or os.environ.get("HELIUS_API") or "").strip()
    if not ключ:
        print("нет ключа Helius -- проход только облачный", flush=True)
        return 1
    rpc = C.C2Rpc(service="c2_preemnik", key=ключ)
    темп = Темп(RPS)
    тело: dict = {"что": "преемник 4vw54BmA: программа, деньги, перепись подписей",
                  "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "источник": ГЕРОЙ, "программа": ПРОГРАММА, "параметры": vars(а)}

    if а.chto:
        print("== программа по цепи", flush=True)
        тело["программа_по_цепи"] = что_за_программа(rpc, темп)
        print(json.dumps(тело["программа_по_цепи"], ensure_ascii=False, indent=1),
              flush=True)

    с_ts = int(time.mktime(time.strptime(а.s, "%Y-%m-%dT%H:%M"))) - time.timezone
    стр = []
    if а.perepis or а.dengi or а.chto:
        print(f"== подписи источника с {а.s}Z", flush=True)
        стр = подписи(rpc, темп, ГЕРОЙ, с_ts, а.predel_podpisey)
        тело["перепись"] = перепись(стр)
        print(json.dumps({k: v for k, v in тело["перепись"].items()
                          if k != "по_минутам"}, ensure_ascii=False, indent=1), flush=True)

    # Разбор транзакций: свежие (для пункта а) и выборка по окну (для пункта б).
    разобрано: list = []
    план: list = []
    if а.chto and стр:
        план += [x["signature"] for x in стр[:а.svezhih]]
    if а.dengi and стр:
        # Все подписи окна, если их не больше предела разбора; иначе равномерная выборка,
        # и в выходе честно сказано, сколько осталось непрочитанным.
        все = [x["signature"] for x in стр]
        if len(все) <= а.predel_razbora:
            план += все
            тело["разбор_окна"] = {"полный": True, "подписей": len(все)}
        else:
            шаг = len(все) / а.predel_razbora
            выб = [все[int(i * шаг)] for i in range(а.predel_razbora)]
            план += выб
            тело["разбор_окна"] = {"полный": False, "подписей_всего": len(все),
                                   "разобрано": len(выб), "шаг": round(шаг, 2),
                                   "чем_грозит": "движение SOL могло пройти в "
                                                 "непрочитанной транзакции"}
    план = list(dict.fromkeys(план))
    for i, п_ in enumerate(план, 1):
        темп.ждать()
        try:
            о = rpc.call("getTransaction", [п_, {"encoding": "jsonParsed",
                                                 "maxSupportedTransactionVersion": 0}])
        except Exception as exc:  # noqa: BLE001
            разобрано.append({"подпись": п_, "ошибка_запроса": type(exc).__name__})
            continue
        if о:
            разобрано.append(разбор_транзакции(п_, о))
        if i % 25 == 0 or i == len(план):
            print(f"  разобрано {i}/{len(план)}", flush=True)

    # Сводка по инструкциям и программам
    прогр: collections.Counter = collections.Counter()
    диск: collections.Counter = collections.Counter()
    журн: collections.Counter = collections.Counter()
    выч: list = []
    for т in разобрано:
        for и in т.get("инструкции") or []:
            прогр[и["программа"]] += 1
            if и["программа"] == ПРОГРАММА:
                диск[f"{и['дискриминатор_hex']}|байт {и['байт_данных']}|счетов {и['счетов']}"] += 1
        for л in т.get("журнал") or []:
            if "Instruction:" in л or "invoke" in л:
                журн[л[:120]] += 1
        if т.get("вычислений"):
            выч.append(т["вычислений"])
    тело["разобрано_транзакций"] = len(разобрано)
    тело["программы_в_инструкциях"] = dict(прогр.most_common(12))
    тело["дискриминаторы_целевой_программы"] = dict(диск.most_common(12))
    тело["журнал_частое"] = dict(журн.most_common(14))
    if выч:
        выч.sort()
        тело["вычислений"] = {"n": len(выч), "медиана": выч[len(выч) // 2],
                              "мин": выч[0], "макс": выч[-1]}

    # Движение SOL
    ушло: collections.Counter = collections.Counter()
    пришло: collections.Counter = collections.Counter()
    платы = 0.0
    for т in разобрано:
        д = т.get("дельты_sol") or {}
        свой = д.get(ГЕРОЙ)
        платы += т.get("плата_sol") or 0
        if свой is None:
            continue
        if свой < 0:
            for к, v in д.items():
                if к != ГЕРОЙ and v > 0:
                    ушло[к] += v
        elif свой > 0:
            for к, v in д.items():
                if к != ГЕРОЙ and v < 0:
                    пришло[к] += -v
    тело["sol_ушло_кому"] = {k: round(v, 6) for k, v in ушло.most_common(20)}
    тело["sol_пришло_от"] = {k: round(v, 6) for k, v in пришло.most_common(20)}
    тело["плат_sol_на_разобранных"] = round(платы, 6)
    тело["транзакции"] = разобрано[:a_предел(а)]
    тело["запросов"] = dict(getattr(rpc, "calls_by_method", {}) or {})

    ф = П / f"preemnik{а.metka}.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        import podbivka_run as R  # noqa: PLC0415
        R.записано(ф)
    except Exception:  # noqa: BLE001, S110
        pass
    print(json.dumps({k: v for k, v in тело.items()
                      if k not in ("транзакции", "перепись")},
                     ensure_ascii=False, indent=1), flush=True)
    return 0


def a_предел(а) -> int:
    return 60 if а.chto else 12


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--s", default="2026-10-10T03:00", help="начало окна, YYYY-MM-DDTHH:MM")
    р.add_argument("--chto", action="store_true", help="пункт а: программа и её инструкции")
    р.add_argument("--perepis", action="store_true", help="перепись подписей по минутам")
    р.add_argument("--dengi", action="store_true", help="пункт б: движение SOL")
    р.add_argument("--svezhih", type=int, default=40,
                   help="сколько свежих транзакций разобрать для пункта а")
    р.add_argument("--predel-podpisey", type=int, default=60000)
    р.add_argument("--predel-razbora", type=int, default=1200,
                   help="сколько транзакций окна разобрать максимум")
    р.add_argument("--metka", default="")
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
