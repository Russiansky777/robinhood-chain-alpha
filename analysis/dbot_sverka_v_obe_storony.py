#!/usr/bin/env python3
"""Сверка с DBot в обе стороны: каждая его сделка и каждая наша.

ЗАЧЕМ. Владелец 27.09, пункт 4: "Ежедневная сверка с DBot в обе стороны
(измерительный код, без тестов): по кошелькам DBot BATCH-3 и BATCH-5 по цепи за
сутки -- каждая сделка DBot: видели ли, купили ли, причина; и каждая наша: взял
ли DBot, итог. Свод по причинам и SOL в Telegram, 07:00 Мадрид и по запросу".

ДВЕ СТОРОНЫ, И ИХ НЕЛЬЗЯ ПУТАТЬ.
  Сторона DBot -> мы: берём покупки кошельков задач ПО ЦЕПИ (а не из отчётов
  DBot: отчёт может умолчать, цепь не может) и по каждой ищем в нашем журнале
  решений строку с той же подписью источника. Нашли -- видели; есть покупка --
  купили; иначе причина = код решения, слово в слово из журнала.
  Сторона мы -> DBot: берём НАШИ покупки полосы и площадки за то же окно и по
  каждой смотрим, есть ли у кошельков задач покупка того же минта в окне
  допуска. Нет -- значит мы взяли то, чего DBot не брал, и это отдельная
  строка, а не ошибка.

ПОЧЕМУ АДРЕСА БЕРУТСЯ ЖИВЬЁМ. Задачи владелец правит в DBot, а не в
репозитории, поэтому зашитый список со временем разойдётся с боем -- и
разойдётся молча. Адреса спрашиваются тем же GET, которым их берёт детектор
(bloom_detector.источники_живьём), и это ТОЛЬКО чтение.

ЖУРНАЛЫ ЧИТАЮТСЯ ПОТОКОМ. Правило владельца: в память их не грузим.

Измерительный код: самопроверок нет (правило 8). Ни одной отправки в цепь.
"""
from __future__ import annotations

import argparse
import calendar
import gzip
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ЛАМПОРТОВ_В_SOL = 1_000_000_000
ЗАДАЧИ_ПО_УМОЛЧАНИЮ = ("BATCH-3", "BATCH-5")
# Допуск по времени, в котором покупка DBot и наша считаются "по одному сигналу".
ДОПУСК_S = 180.0


def в_эпоху(с: str) -> int:
    с = str(с).replace("Z", "").replace("T", " ")
    return calendar.timegm(time.strptime(с[:19], "%Y-%m-%d %H:%M:%S"))


def узел() -> str:
    к = (os.environ.get("HELIUS_API") or os.environ.get("HELIUS_API_KEY") or "").strip()
    if not к:
        raise SystemExit("СБОЙ: HELIUS_API не задан")
    return f"https://mainnet.helius-rpc.com/?api-key={к}"


def rpc(метод: str, параметры: list, *, повторов: int = 4):
    тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                       "params": параметры}).encode()
    пауза, последняя = 0.4, None
    for _ in range(повторов):
        req = urllib.request.Request(узел(), data=тело,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                о = json.loads(r.read().decode())
            if "error" in о:
                последняя = str(о["error"])[:200]
            else:
                return о.get("result")
        except Exception as exc:  # noqa: BLE001
            последняя = type(exc).__name__
        time.sleep(пауза)
        пауза *= 2
    raise RuntimeError(f"{метод} не ответил: {последняя}")


# ------------------------------------------------------- адреса задач живьём

def адреса_задач(задачи: tuple) -> dict:
    """{адрес: имя задачи}. Только GET, ключ DBot из окружения службы."""
    ключ = (os.environ.get("DBOT_API_KEY") or "").strip()
    if not ключ:
        return {}
    try:
        import bloom_detector as BD  # noqa: PLC0415

        return BD.источники_живьём(ключ, задачи)
    except Exception:  # noqa: BLE001
        return {}


# ------------------------------------------------------------ наши журналы

def строки_журнала(путь: Path):
    """Поток строк журнала и его .gz-ротаций. В память ничего не грузим."""
    файлы = [путь] + sorted(путь.parent.glob(путь.name + ".*"), reverse=True)
    for ф in файлы:
        if not ф.exists():
            continue
        try:
            откр = (gzip.open if ф.suffix == ".gz" else open)
            with откр(ф, "rt", encoding="utf-8", errors="replace") as fh:
                for с in fh:
                    с = с.strip()
                    if not с:
                        continue
                    try:
                        yield json.loads(с)
                    except ValueError:
                        continue
        except OSError:
            continue


def решения_по_подписям(путь: Path, от: int, до: int) -> dict:
    """{подпись источника: (код, причина, действие, минт)} за окно."""
    из_ = {}
    for з in строки_журнала(путь):
        п = з.get("signature")
        if not п:
            continue
        т = з.get("ts_utc") or з.get("utc") or з.get("ts")
        вр = None
        if isinstance(т, (int, float)):
            вр = float(т)
        elif isinstance(т, str) and len(т) >= 19:
            try:
                вр = в_эпоху(т)
            except ValueError:
                вр = None
        if вр is not None and not (от <= вр <= до):
            continue
        из_[п] = {"код": з.get("code"), "причина": з.get("reason"),
                   "действие": з.get("action"), "минт": з.get("mint"),
                   "источник": з.get("source"),
                   "трата_источника_sol": з.get("spend_sol_eq")}
    return из_


def наши_покупки(путь: Path, от: int, до: int) -> list:
    """Наши покупки за окно: два прохода, потому что строки дописываются."""
    поз: dict = {}
    for з in строки_журнала(путь):
        cid = з.get("client_order_id")
        if not cid:
            continue
        поз.setdefault(cid, {}).update({k: v for k, v in з.items() if v is not None})
    из_ = []
    for cid, p in поз.items():
        т = p.get("ts_intent") or p.get("ts_utc")
        вр = None
        if isinstance(т, (int, float)):
            вр = float(т)
        elif isinstance(т, str) and len(т) >= 19:
            try:
                вр = в_эпоху(т)
            except ValueError:
                вр = None
        if вр is None or not (от <= вр <= до):
            continue
        из_.append({"cid": cid, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                      time.gmtime(вр)),
                     "ts": вр, "минт": p.get("mint"),
                     "полоса": bool(p.get("lane")),
                     "источник": p.get("source"),
                     "подпись_источника": p.get("source_sig"),
                     "вход_sol": p.get("sol_in"),
                     "вернулось_sol": p.get("closed_sol_net"),
                     "подпись": (p.get("lane_signature")
                                  or (p.get("signatures") or [None])[0])})
    из_.sort(key=lambda x: x["ts"])
    return из_


# --------------------------------------------------- покупки задач по цепи

def покупки_кошелька(адрес: str, от: int, до: int, *, предел: int = 200) -> list:
    """Покупки кошелька по цепи за окно: минт, подпись, время, трата SOL."""
    подписи, курсор = [], None
    while len(подписи) < предел:
        пар = {"limit": min(1000, предел - len(подписи) + 100)}
        if курсор:
            пар["before"] = курсор
        пачка = rpc("getSignaturesForAddress", [адрес, пар]) or []
        if not пачка:
            break
        кончили = False
        for з in пачка:
            вр = з.get("blockTime")
            if вр is None or вр > до:
                continue
            if вр < от:
                кончили = True
                break
            подписи.append(з)
        курсор = пачка[-1].get("signature")
        if кончили or not курсор:
            break
    из_ = []
    for з in подписи:
        tx = rpc("getTransaction", [з["signature"],
                                     {"encoding": "jsonParsed",
                                      "maxSupportedTransactionVersion": 0}])
        if not tx:
            continue
        мета = tx.get("meta") or {}
        if мета.get("err"):
            continue
        кл = [(x.get("pubkey") if isinstance(x, dict) else x)
              for x in (((tx.get("transaction") or {}).get("message") or {})
                        .get("accountKeys") or [])]
        # Трата: уменьшение нативного SOL у кошелька.
        трата = None
        if адрес in кл:
            и = кл.index(адрес)
            до_б = (мета.get("preBalances") or [])
            по_б = (мета.get("postBalances") or [])
            if и < len(до_б) and и < len(по_б):
                д = (по_б[и] - до_б[и]) / ЛАМПОРТОВ_В_SOL
                трата = -д if д < 0 else 0.0
        # Минт: токен, которого у кошелька стало больше.
        минт, прирост = None, 0.0
        было = {(б.get("owner"), б.get("mint")):
                float((б.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                for б in (мета.get("preTokenBalances") or [])}
        for б in (мета.get("postTokenBalances") or []):
            if б.get("owner") != адрес:
                continue
            к = (б.get("owner"), б.get("mint"))
            п = float((б.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            д = п - было.get(к, 0.0)
            if д > прирост:
                минт, прирост = б.get("mint"), д
        if not минт:
            continue
        из_.append({"подпись": з["signature"], "слот": з.get("slot"),
                     "ts": з.get("blockTime"),
                     "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                           time.gmtime(з.get("blockTime") or 0)),
                     "кошелёк": адрес, "минт": минт,
                     "получено": прирост, "трата_sol": трата})
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--s", required=True, help="начало окна UTC")
    р.add_argument("--po", required=True, help="конец окна UTC")
    р.add_argument("--zadachi", default=",".join(ЗАДАЧИ_ПО_УМОЛЧАНИЮ))
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--predel-na-koshelek", type=int, default=120)
    р.add_argument("--out", default=None)
    а = р.parse_args()

    от, до_ = в_эпоху(а.s), в_эпоху(а.po)
    задачи = tuple(x.strip() for x in а.zadachi.split(",") if x.strip())
    итог = {"окно": [а.s, а.po], "задачи": list(задачи),
            "снято_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    адреса = адреса_задач(задачи)
    итог["адресов_задач"] = len(адреса)
    if not адреса:
        итог["почему_нет_адресов"] = ("DBOT_API_KEY не задан или задачи не "
                                       "вернулись: сверять не с чем")
        print(json.dumps(итог, ensure_ascii=False, indent=1))
        if а.out:
            Path(а.out).write_text(json.dumps(итог, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
        return 1
    итог["по_задачам"] = {}
    for имя in задачи:
        итог["по_задачам"][имя] = sum(1 for v in адреса.values() if v == имя)

    состояние = Path(а.state_dir)
    решения = решения_по_подписям(состояние / "decisions.jsonl", от, до_)
    наши = наши_покупки(состояние / "positions.jsonl", от, до_)
    итог["решений_в_окне"] = len(решения)
    итог["наших_покупок_в_окне"] = len(наши)

    # --- СТОРОНА DBot -> МЫ
    сторона_dbot = []
    по_причинам: dict = {}
    for адрес, задача in sorted(адреса.items()):
        for п in покупки_кошелька(адрес, от, до_, предел=а.predel_na_koshelek):
            реш = решения.get(п["подпись"])
            наша = next((н for н in наши
                          if н.get("подпись_источника") == п["подпись"]), None)
            видели = реш is not None
            купили = наша is not None
            причина = None
            if not видели:
                причина = "в журнале решений строки нет -- сигнал не дошёл"
            elif not купили:
                причина = f"{реш.get('код') or 'без кода'}: {str(реш.get('причина') or '')[:160]}"
            ключ = ("купили" if купили else (причина.split(":")[0] if причина else "?"))
            с = по_причинам.setdefault(ключ, {"n": 0, "трата_dbot_sol": 0.0})
            с["n"] += 1
            с["трата_dbot_sol"] += float(п.get("трата_sol") or 0.0)
            сторона_dbot.append({
                "задача": задача, "кошелёк": адрес, "utc": п["utc"],
                "подпись": п["подпись"], "минт": п["минт"],
                "трата_dbot_sol": п.get("трата_sol"),
                "видели": видели, "купили": купили, "причина": причина,
                "наш_вход_sol": (наша or {}).get("вход_sol"),
                "наш_итог_sol": (наша or {}).get("вернулось_sol")})

    # --- СТОРОНА МЫ -> DBot
    по_минту_dbot: dict = {}
    for з in сторона_dbot:
        по_минту_dbot.setdefault(з["минт"], []).append(з)
    сторона_наша = []
    for н in наши:
        похожие = [з for з in по_минту_dbot.get(н.get("минт") or "", [])
                   if abs((з.get("utc") and в_эпоху(з["utc"]) or 0) - н["ts"]) <= ДОПУСК_S]
        итог_ = None
        if н.get("вернулось_sol") is not None and н.get("вход_sol"):
            итог_ = round(float(н["вернулось_sol"]) - float(н["вход_sol"]), 9)
        сторона_наша.append({
            "utc": н["utc"], "cid": н["cid"], "минт": н.get("минт"),
            "полоса": н["полоса"], "вход_sol": н.get("вход_sol"),
            "вернулось_sol": н.get("вернулось_sol"), "итог_sol": итог_,
            "подпись": н.get("подпись"),
            "dbot_взял": bool(похожие),
            "dbot_задача": (похожие[0]["задача"] if похожие else None)})

    итог["сторона_dbot_к_нам"] = сторона_dbot
    итог["сторона_наша_к_dbot"] = сторона_наша
    итог["свод_по_причинам"] = {к: {"n": v["n"],
                                     "трата_dbot_sol": round(v["трата_dbot_sol"], 9)}
                                 for к, v in sorted(по_причинам.items(),
                                                     key=lambda кв: -кв[1]["n"])}
    итог["свод"] = {
        "сделок_dbot": len(сторона_dbot),
        "из_них_видели": sum(1 for з in сторона_dbot if з["видели"]),
        "из_них_купили": sum(1 for з in сторона_dbot if з["купили"]),
        "наших_сделок": len(сторона_наша),
        "из_них_dbot_тоже_взял": sum(1 for з in сторона_наша if з["dbot_взял"]),
        "наш_итог_sol": round(sum(float(з["итог_sol"] or 0.0)
                                   for з in сторона_наша), 9),
        "трата_dbot_sol": round(sum(float(з.get("трата_dbot_sol") or 0.0)
                                     for з in сторона_dbot), 9)}

    кратко = {k: итог[k] for k in ("окно", "задачи", "адресов_задач", "по_задачам",
                                    "решений_в_окне", "наших_покупок_в_окне",
                                    "свод", "свод_по_причинам") if k in итог}
    print(json.dumps(кратко, ensure_ascii=False, indent=1))
    if а.out:
        Path(а.out).write_text(json.dumps(итог, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
