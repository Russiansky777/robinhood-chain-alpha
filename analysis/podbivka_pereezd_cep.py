#!/usr/bin/env python3
"""Переезд кривой: сверка архива с цепью и темп переездов (задача владельца 09.10, п.1б-1в).

Зачем. Выводы по переезду посчитаны по архиву PumpApi, а решение владельца -- про деньги.
Прежде чем ставить билет, надо проверить архив цепью: что он видит в первых слотах нового
пула, чего не видит и совпадает ли цена нашего входа.

Контекст, который это объясняет. С 21.07.2026 у pump.fun режим BOOST: после переезда в
PumpSwap протокол пять минут выкупает токен TWAP-партиями (около 17.6 SOL на SOL-парах,
около 2516 USDC на USDC-парах) и сжигает купленное; переехавшим до отсечки и запущенным
через Mayhem выкупа нет (KuCoin «Pump.fun BOOST Mode Explained», The Block 29.07.2026,
плюс сторонний разбор «The 17.6 SOL Ghost», который считает настоящий выигрыш +2..+4 SOL
вместо +15). Собранное окно 230 слотов -- это 62 секунды, то есть ПЕРВАЯ минута из пяти.

Два режима.
  --rezhim sverka -- выборка переездов из собранных суток. По каждому: все подписи пула до
    створ+N слотов (подписи берутся прямым перебором getSignaturesForAddress до самой
    старой, то есть независимо от архива -- иначе проверка была бы круговой), затем
    getTransaction. Разбирается: кто покупал и на сколько, резервы пула после каждого
    события, программы в транзакции. Отсюда: сколько событий в створе и в +1/+2, цена по
    цепи против состояний архива, и адреса, которые встречаются почти во ВСЕХ переездах --
    это и есть агент BOOST (его имя не задаётся, а находится по частоте).
  --rezhim tempy -- темп переездов по цепи. Случайные слоты окна, getBlock, и в каждом
    блоке считаются транзакции, где инструкция программы pump.fun (её адрес начинается на
    6EF8 и находится по префиксу, а не вписывается) идёт вместе с созданием пула PumpSwap
    или DAMM v2. Отсюда переездов в сутки -- против 3757 createPool и 6051 «переезд кривой»
    из архива.

Только чтение. Helius, темп -- PODB_HELIUS_RPS (потолок владельца 6 запросов в секунду на
все мои процессы вместе). Выход: data/podbivka/pereezd_cep_<метка>.json и сводка.
"""
from __future__ import annotations

import argparse
import collections
import glob
import gzip
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
WSOL = "So11111111111111111111111111111111111111112"
ПРЕФИКС_PUMPFUN = "6EF8"          # адрес программы pump.fun; полный ищем по префиксу
ПУЛЫ_ПЕРЕЕЗДА = {"pump-amm", "meteora-damm-v2"}
РЕЗЕРВ_МИН = 10.0
СЛОТОВ_В_СУТКИ = 322693           # замерено по блокам собранных суток (0.2677 с на слот)
RPS = float(os.environ.get("PODB_HELIUS_RPS") or 6.0)


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


def переезды_из_архива(папка: Path) -> list:
    """Пулы переезда из собранных суток: тип пула, правило создания и резерв от 10 SOL."""
    из_ = []
    for ф in sorted(glob.glob(str(папка / "zap_*.json.gz"))):
        with gzip.open(ф, "rt", encoding="utf-8") as о:
            д = json.load(о)
        сутки = Path(ф).name.replace("zap_", "").split("T")[0]
        for x in д.get("ряды") or []:
            if x.get("quoteMint") != WSOL or x.get("пул") not in ПУЛЫ_ПЕРЕЕЗДА:
                continue
            if x.get("правило") not in ("создание:createPool", "создание:migrate"):
                continue
            вх = (x.get("вход") or {}).get("створ_дно")
            if not вх or вх[0] < РЕЗЕРВ_МИН:
                continue
            из_.append({"сутки": сутки, "минт": x["минт"], "poolId": x["poolId"],
                        "пул": x["пул"], "правило": x["правило"], "блок": x["блок"],
                        "ts": x["ts"], "событий": x.get("событий"),
                        "вход": x.get("вход") or {}, "выход": x.get("выход") or {},
                        "тариф": x.get("тариф"), "f": x.get("f"), "g": x.get("g"),
                        "в_блоке_создания": x.get("в_блоке_создания") or {}})
    return из_


def самые_старые_подписи(rpc, темп: Темп, адрес: str, предел_страниц: int = 20) -> tuple:
    """Все подписи адреса, перебором назад до самой старой. Возвращает (список, страниц)."""
    всё: list = []
    before = None
    for стр in range(предел_страниц):
        темп.ждать(1)
        часть = rpc.signatures(адрес, before=before, limit=1000)
        if not часть:
            return всё, стр + 1
        всё.extend(часть)
        if len(часть) < 1000:
            return всё, стр + 1
        before = часть[-1]["signature"]
    return всё, предел_страниц


def резервы_пула(tx: dict, pool: str) -> dict:
    """Остатки токенных счетов пула после транзакции: {минт: количество}."""
    из_: dict = {}
    for r in ((tx.get("meta") or {}).get("postTokenBalances") or []):
        if r.get("owner") != pool:
            continue
        м = r.get("mint")
        с = ((r.get("uiTokenAmount") or {}).get("uiAmountString")
             or (r.get("uiTokenAmount") or {}).get("uiAmount"))
        try:
            из_[м] = float(с)
        except (TypeError, ValueError):
            continue
    return из_


def дельта_пула(tx: dict, pool: str) -> dict:
    """Изменение остатков счетов пула: {минт: после - до}."""
    до: dict = {}
    for r in ((tx.get("meta") or {}).get("preTokenBalances") or []):
        if r.get("owner") == pool:
            с = ((r.get("uiTokenAmount") or {}).get("uiAmountString")
                 or (r.get("uiTokenAmount") or {}).get("uiAmount"))
            try:
                до[r.get("mint")] = float(с)
            except (TypeError, ValueError):
                pass
    после = резервы_пула(tx, pool)
    return {м: после.get(м, 0.0) - до.get(м, 0.0) for м in set(после) | set(до)}


def программы(tx: dict) -> set:
    """Адреса программ во всех инструкциях, включая внутренние."""
    из_: set = set()
    сообщ = (tx.get("transaction") or {}).get("message") or {}
    for и in (сообщ.get("instructions") or []):
        if и.get("programId"):
            из_.add(и["programId"])
    for гр in ((tx.get("meta") or {}).get("innerInstructions") or []):
        for и in (гр.get("instructions") or []):
            if и.get("programId"):
                из_.add(и["programId"])
    return из_


def разобрать_пул(rpc, темп: Темп, п: dict, слотов: int, длинно: int,
                  образцов: int) -> dict:
    """События пула по цепи: створ подробно, дальше -- гистограмма и выборка.

    Три вопроса сразу. (1) Переезд ли это: в транзакции СТВОРА должна быть инструкция
    программы pump.fun (адрес на 6EF8) -- она и делает migrate. (2) Торгуют ли в створе и
    в +1/+2. (3) Виден ли выкуп BOOST: он идёт пять минут партиями, поэтому мало смотреть
    первые слоты -- берём гистограмму подписей до створ+длинно и выборку транзакций из
    этого окна.
    """
    подписи, страниц = самые_старые_подписи(rpc, темп, п["poolId"])
    если_обрезано = страниц >= 20 and len(подписи) >= 20_000
    мин_слот = min((s.get("slot") or 0) for s in подписи) if подписи else None
    б0 = п["блок"]
    в_окне = sorted((s for s in подписи if б0 <= (s.get("slot") or 0) <= б0 + слотов),
                    key=lambda s: (s.get("slot") or 0))
    длинные = sorted((s for s in подписи
                      if б0 + слотов < (s.get("slot") or 0) <= б0 + длинно),
                     key=lambda s: (s.get("slot") or 0))
    # гистограмма по сотням слотов -- бесплатно, слот есть в самой подписи
    гист: collections.Counter = collections.Counter()
    for s in подписи:
        сдв = (s.get("slot") or 0) - б0
        if 0 <= сдв <= длинно:
            гист[сдв // 100 * 100] += 1
    # выборка из длинного окна, равномерно
    шаг = max(1, len(длинные) // max(1, образцов))
    образцы = длинные[::шаг][:образцов]
    брать = [s["signature"] for s in в_окне] + [s["signature"] for s in образцы]
    события: list = []
    for нач in range(0, len(брать), 20):
        часть = брать[нач:нач + 20]
        темп.ждать(len(часть))
        txs = rpc.get_txs(часть)
        for sig in часть:
            tx = txs.get(sig)
            if not tx:
                события.append({"подпись": sig, "нет_узла": True})
                continue
            if (tx.get("meta") or {}).get("err"):
                события.append({"подпись": sig, "слот": tx.get("slot"), "ошибка": True})
                continue
            d = дельта_пула(tx, п["poolId"])
            рез = резервы_пула(tx, п["poolId"])
            дтокен = d.get(п["минт"], 0.0)
            дsol = d.get(WSOL, 0.0)
            подп = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
            кто = next((a.get("pubkey") for a in подп if a.get("signer")), None)
            прог = sorted(программы(tx))
            события.append({
                "подпись": sig, "слот": tx.get("slot"),
                "сдвиг": (tx.get("slot") or 0) - б0,
                "кто": кто, "дельта_токен": round(дтокен, 6), "дельта_sol": round(дsol, 9),
                "сторона": ("buy" if дтокен < 0 and дsol > 0
                            else "sell" if дтокен > 0 and дsol < 0 else "иное"),
                "резерв_sol": round(рез.get(WSOL, 0.0), 9),
                "резерв_токен": round(рез.get(п["минт"], 0.0), 6),
                "pumpfun": any(x.startswith(ПРЕФИКС_PUMPFUN) for x in прог),
                "программы": прог})
    створ_события = [e for e in события if e.get("сдвиг") == 0]
    return {"подписей_всего": len(подписи), "страниц": страниц, "обрезано": если_обрезано,
            "самый_старый_слот": мин_слот, "в_окне": len(в_окне),
            "длинных_подписей": len(длинные), "образцов": len(образцы),
            "гистограмма_слотов": dict(sorted(гист.items())),
            "створ_с_pumpfun": any(e.get("pumpfun") for e in створ_события),
            "створ_программы": sorted({x for e in створ_события
                                       for x in (e.get("программы") or [])}),
            "события": события}


def по_слотам(события: list, слотов: int) -> dict:
    """Сколько покупок, кошельков и SOL в каждом слоте от створа."""
    из_: dict = {}
    for s in range(слотов + 1):
        в = [e for e in события if e.get("сдвиг") == s and not e.get("ошибка")
             and not e.get("нет_узла")]
        пок = [e for e in в if e.get("сторона") == "buy"]
        из_[s] = {"событий": len(в), "покупок": len(пок),
                  "кошельков": len({e["кто"] for e in пок if e.get("кто")}),
                  "sol": round(sum(e["дельта_sol"] for e in пок), 6)}
    return из_


def сверить_цену(п: dict, события: list) -> dict:
    """Состояния архива против резервов пула по цепи на тех же слотах."""
    из_: dict = {}
    for имя, сдвиг in (("створ_дно", 0), ("створ+1", 1), ("створ+2", 2)):
        а = (п["вход"] or {}).get(имя)
        посл = [e for e in события
                if e.get("сдвиг") is not None and e["сдвиг"] <= сдвиг
                and e.get("резерв_sol") and e.get("резерв_токен")]
        ц = посл[-1] if посл else None
        стр = {"архив": None, "цепь": None}
        if а:
            стр["архив"] = {"sol": round(а[0], 6), "токен": round(а[1], 3),
                            "цена": а[0] / а[1] if а[1] else None}
        if ц:
            стр["цепь"] = {"sol": ц["резерв_sol"], "токен": ц["резерв_токен"],
                           "цена": (ц["резерв_sol"] / ц["резерв_токен"]
                                    if ц["резерв_токен"] else None), "слот": ц["слот"]}
        if стр["архив"] and стр["цепь"] and стр["архив"]["цена"] and стр["цепь"]["цена"]:
            стр["разница_пп"] = round(
                100 * (стр["архив"]["цена"] / стр["цепь"]["цена"] - 1), 4)
        из_[имя] = стр
    return из_


def режим_сверка(rpc, темп: Темп, а) -> dict:
    """п.1б: выборка переездов, первые слоты по цепи против архива."""
    все = переезды_из_архива(П / "zapuski")
    if not все:
        return {"ошибка": "нет собранных суток запусков"}
    рнд = random.Random(а.seed)
    # по равной доле из каждых суток -- чтобы выборка не села на один день
    по_суткам: dict = collections.defaultdict(list)
    for x in все:
        по_суткам[x["сутки"]].append(x)
    выборка: list = []
    на_сутки = max(1, а.skolko // max(1, len(по_суткам)))
    for д in sorted(по_суткам):
        выборка.extend(рнд.sample(по_суткам[д], min(на_сутки, len(по_суткам[д]))))
    рнд.shuffle(выборка)
    выборка = выборка[:а.skolko]
    print(f"переездов в архиве {len(все)}, суток {len(по_суткам)}, "
          f"в выборке {len(выборка)}", flush=True)

    ряды, кошельки, программы_счёт = [], collections.Counter(), collections.Counter()
    сумма_кошелька: dict = collections.defaultdict(float)
    пулов_кошелька: collections.Counter = collections.Counter()
    в_длинном: collections.Counter = collections.Counter()
    sol_в_длинном: dict = collections.defaultdict(float)
    for i, п_ in enumerate(выборка, 1):
        try:
            из_ = разобрать_пул(rpc, темп, п_, а.slotov, а.dlinno, а.obrazcov)
        except Exception as exc:  # noqa: BLE001
            ряды.append({**{k: п_[k] for k in ("сутки", "минт", "poolId", "пул", "правило")},
                         "ошибка": f"{type(exc).__name__}: {str(exc)[:120]}"})
            print(f"  {i}/{len(выборка)} {п_['poolId'][:8]}: "
                  f"{type(exc).__name__}", flush=True)
            continue
        соб = из_["события"]
        сл = по_слотам(соб, а.slotov)
        цена = сверить_цену(п_, соб)
        пулов_кошелька.update({e["кто"] for e in соб
                               if e.get("сторона") == "buy" and e.get("кто")})
        долгие = [e for e in соб if (e.get("сдвиг") or 0) > а.slotov]
        for e in долгие:
            if e.get("сторона") == "buy" and e.get("кто"):
                в_длинном[e["кто"]] += 1
                sol_в_длинном[e["кто"]] += e["дельта_sol"]
        for e in соб:
            if e.get("сторона") == "buy" and e.get("кто"):
                кошельки[e["кто"]] += 1
                сумма_кошелька[e["кто"]] += e["дельта_sol"]
            for пр in (e.get("программы") or []):
                программы_счёт[пр] += 1
        ряды.append({
            **{k: п_[k] for k in ("сутки", "минт", "poolId", "пул", "правило", "блок")},
            "архив_событий_230": п_.get("событий"),
            "створ_с_pumpfun": из_["створ_с_pumpfun"],
            "створ_программы": из_["створ_программы"],
            "длинных_подписей": из_["длинных_подписей"], "образцов": из_["образцов"],
            "гистограмма_слотов": из_["гистограмма_слотов"],
            "архив_в_блоке_создания": п_["в_блоке_создания"].get("покупок"),
            "подписей_всего": из_["подписей_всего"], "страниц": из_["страниц"],
            "обрезано": из_["обрезано"], "самый_старый_слот": из_["самый_старый_слот"],
            "створ_самый_старый": (из_["самый_старый_слот"] == п_["блок"]
                                   if из_["самый_старый_слот"] else None),
            "в_окне": из_["в_окне"], "по_слотам": сл, "цена": цена,
            "покупок_в_окне": sum(v["покупок"] for v in сл.values()),
            "sol_в_окне": round(sum(v["sol"] for v in сл.values()), 6)})
        print(f"  {i}/{len(выборка)} {п_['poolId'][:8]} {п_['пул']}: подписей "
              f"{из_['подписей_всего']} (страниц {из_['страниц']}), в окне {из_['в_окне']}, "
              f"покупок {ряды[-1]['покупок_в_окне']}", flush=True)

    годные = [r for r in ряды if not r.get("ошибка")]
    # Агент BOOST ищется по ОХВАТУ пулов, а не по числу покупок: он должен покупать почти в
    # каждом переезде. Имя не задаётся заранее -- иначе это было бы подгонкой под догадку.
    всего = max(1, len(годные))
    частые = [{"кошелёк": k, "пулов": v, "доля_пулов": round(100 * v / всего, 1),
               "покупок": кошельки[k], "sol_всего": round(сумма_кошелька[k], 4),
               "покупок_в_длинном": в_длинном[k],
               "sol_в_длинном": round(sol_в_длинном[k], 4),
               "sol_на_пул": round(сумма_кошелька[k] / v, 4) if v else None}
              for k, v in пулов_кошелька.most_common(20)]
    return {"переездов_в_архиве": len(все), "в_выборке": len(выборка),
            "годных": len(годные), "ряды": ряды,
            "частые_кошельки": частые,
            "программы": dict(программы_счёт.most_common(20)),
            "створ_с_pumpfun": sum(1 for r in годные if r.get("створ_с_pumpfun")),
            "створ_без_pumpfun": sum(1 for r in годные
                                     if r.get("створ_с_pumpfun") is False)}


def блоки_окна(папка: Path) -> tuple:
    """Первый и последний слот собранного окна -- из самих файлов запусков."""
    нач, кон = None, None
    for ф in sorted(glob.glob(str(папка / "zap_*.json.gz"))):
        with gzip.open(ф, "rt", encoding="utf-8") as о:
            б = (json.load(о).get("блоки") or {})
        if б.get("первый"):
            нач = б["первый"] if нач is None else min(нач, б["первый"])
        if б.get("последний"):
            кон = б["последний"] if кон is None else max(кон, б["последний"])
    return нач, кон


def программы_переезда() -> list:
    """Программы пулов, замеренные на настоящих переездах прогоном сверки.

    Берутся из его файла, а не вписываются по памяти: адреса программ меняются, и
    догадка тут превратилась бы в выдуманные данные.
    """
    ф = П / "pereezd_cep_svod.json"
    if not ф.exists():
        return []
    try:
        д = json.loads(ф.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    из_: collections.Counter = collections.Counter()
    for тело in д.values():
        for пр, n in ((тело.get("сверка") or {}).get("программы") or {}).items():
            if not пр.startswith(ПРЕФИКС_PUMPFUN):
                из_[пр] += n
    # пулы PumpSwap и DAMM v2 -- самые частые программы в транзакциях переезда после
    # системных; системные (11111..., Token, ComputeBudget) отбрасываются по префиксу
    системные = ("1111", "Token", "Compute", "ATokenG", "Sysvar", "MemoSq")
    return [пр for пр, _ in из_.most_common(12)
            if not any(пр.startswith(s) for s in системные)][:6]


def режим_темпы(rpc, темп: Темп, а) -> dict:
    """п.1в: темп переездов по цепи -- выборка блоков, инструкции программы pump.fun."""
    нач, кон = блоки_окна(П / "zapuski")
    if not нач or not кон:
        return {"ошибка": "нет собранных суток запусков"}
    пулы_пр = программы_переезда()
    рнд = random.Random(а.seed)
    слоты = sorted(рнд.sample(range(нач, кон + 1), min(а.blokov, кон - нач)))
    print(f"окно слотов {нач}..{кон} ({кон - нач} слотов), в выборке {len(слоты)} блоков; "
          f"программы пулов из сверки: {пулы_пр or 'нет (сверка ещё не считалась)'}",
          flush=True)
    счёт: collections.Counter = collections.Counter()
    диск: collections.Counter = collections.Counter()
    с_пулом_по_блокам: list = []
    пары: collections.Counter = collections.Counter()
    for i, s in enumerate(слоты, 1):
        темп.ждать(1)
        try:
            б = rpc.call("getBlock", [s, {"encoding": "jsonParsed", "rewards": False,
                                          "transactionDetails": "full",
                                          "maxSupportedTransactionVersion": 1,
                                          "commitment": "finalized"}])
        except Exception as exc:  # noqa: BLE001
            счёт[f"блок_{type(exc).__name__}"] += 1
            continue
        if not б:
            счёт["блок_пропущен"] += 1
            continue
        счёт["блоков"] += 1
        с_пулом = 0
        for tx in (б.get("transactions") or []):
            счёт["транзакций"] += 1
            if (tx.get("meta") or {}).get("err"):
                continue
            пр = программы(tx)
            pf = [x for x in пр if x.startswith(ПРЕФИКС_PUMPFUN)]
            if not pf:
                continue
            счёт["с_pumpfun"] += 1
            сообщ = (tx.get("transaction") or {}).get("message") or {}
            for и in (сообщ.get("instructions") or []):
                if и.get("programId") in pf and и.get("data"):
                    диск[и["data"][:12]] += 1
            прочие = sorted(пр - set(pf))
            if пулы_пр and any(x in пр for x in пулы_пр):
                счёт["с_pumpfun_и_пулом"] += 1
                с_пулом += 1
                пары[" + ".join(x[:8] for x in прочие[:3])] += 1
        с_пулом_по_блокам.append(с_пулом)
        if i % 20 == 0:
            print(f"  блоков {i}/{len(слоты)}: с pump.fun {счёт['с_pumpfun']}, "
                  f"с пулом {счёт['с_pumpfun_и_пулом']}", flush=True)
    блоков = max(1, счёт["блоков"])
    в_сутки = счёт["с_pumpfun_и_пулом"] / блоков * СЛОТОВ_В_СУТКИ
    return {"слоты": {"первый": нач, "последний": кон, "в_выборке": len(слоты)},
            "счёт": dict(счёт), "программы_пулов_из_сверки": пулы_пр,
            "переездов_в_блоке_среднее": round(счёт["с_pumpfun_и_пулом"] / блоков, 4),
            "переездов_в_блоке_медиана": (statistics.median(с_пулом_по_блокам)
                                          if с_пулом_по_блокам else None),
            "переездов_в_сутки_по_цепи": round(в_сутки, 1),
            "слотов_в_сутки": СЛОТОВ_В_СУТКИ,
            "частые_дискриминаторы": dict(диск.most_common(10)),
            "частые_пары_программ": dict(пары.most_common(8))}


def main() -> int:
    import c2_common as C  # noqa: PLC0415
    import podbivka_run as R  # noqa: PLC0415
    р_ = argparse.ArgumentParser()
    р_.add_argument("--rezhim", choices=("sverka", "tempy"), required=True)
    р_.add_argument("--skolko", type=int, default=40, help="пулов в выборке (sverka)")
    р_.add_argument("--slotov", type=int, default=10,
                    help="сколько слотов от створа брать ПОДРЯД (sverka)")
    р_.add_argument("--dlinno", type=int, default=1121,
                    help="длинное окно в слотах (300 с) -- гистограмма и выборка")
    р_.add_argument("--obrazcov", type=int, default=40,
                    help="транзакций из длинного окна на пул")
    р_.add_argument("--blokov", type=int, default=120, help="блоков в выборке (tempy)")
    р_.add_argument("--seed", type=int, default=20261009)
    р_.add_argument("--metka", default="")
    а = р_.parse_args()
    метка = а.metka or time.strftime("%Y-%m-%d", time.gmtime())

    ключ = (os.environ.get("HELIUS_API_KEY2") or os.environ.get("HELIUS_API_KEY")
            or os.environ.get("HELIUS_API") or "").strip()
    if not ключ:
        print("нет ключа Helius (HELIUS_API_KEY2 / HELIUS_API) -- проход только облачный",
              flush=True)
        return 1
    rpc = C.C2Rpc(service="c2_pereezd", key=ключ)
    темп = Темп(RPS)
    print(f"темп не выше {RPS} запросов в секунду", flush=True)

    из_ = режим_сверка(rpc, темп, а) if а.rezhim == "sverka" else режим_темпы(rpc, темп, а)
    тело = {"что": f"переезд кривой: {а.rezhim}", "когда": time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "параметры": vars(а),
        а.rezhim: из_, "запросов": dict(getattr(rpc, "calls_by_method", {}) or {})}
    ф = П / f"pereezd_cep_{а.rezhim}_{метка}.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(ф)
    св = П / "pereezd_cep_svod.json"
    было = json.loads(св.read_text(encoding="utf-8")) if св.exists() else {}
    ключ_св = f"{а.rezhim}_{метка}"
    было[ключ_св] = {k: v for k, v in тело.items() if k != а.rezhim}
    было[ключ_св][а.rezhim] = ({k: v for k, v in из_.items() if k != "ряды"}
                               if isinstance(из_, dict) else из_)
    св.write_text(json.dumps(было, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(св)
    print(f"готово, запросов: {тело['запросов']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
