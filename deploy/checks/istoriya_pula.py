#!/usr/bin/env python3
"""История пула вокруг НАШЕЙ покупки: кто, когда, каким слотом и индексом.

ЗАЧЕМ (вопрос владельца 27.09, ночь). По одной живой сделке нужно видеть всё
окружение: покупки и продажи того же токена рядом с нашей, их слоты и индексы в
блоке, наше время приёма сигнала источника и слот нашей отправки. По этому
видно, кто успел раньше нас и на сколько -- и настоящий ли получатель токенов в
транзакции роутера (у OKX DEX Router плательщик газа и получатель токенов --
разные адреса).

КАК. Подписи берутся по МИНТУ (getSignaturesForAddress) в окне вокруг нашего
слота; каждая читается getTransaction; индекс в блоке -- из getBlock со списком
подписей (по одному вызову на слот, не на транзакцию). Наши числа -- из журнала
решений: время приёма сигнала, слот сети в момент решения, слот отправки.

Только чтение. Ни одной отправки в цепь. Измерительный код (правило 8).
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

WSOL = "So11111111111111111111111111111111111111112"
ЛАМПОРТОВ_В_SOL = 1_000_000_000


def узел() -> str:
    к = (os.environ.get("HELIUS_API") or os.environ.get("HELIUS_API_KEY") or "").strip()
    if not к:
        raise SystemExit("СБОЙ: ключ Helius не задан в окружении")
    return f"https://mainnet.helius-rpc.com/?api-key={к}"


def rpc(метод: str, параметры: list, *, повторов: int = 3):
    тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                       "params": параметры}).encode()
    пауза, последняя = 0.35, None
    for _ in range(повторов):
        req = urllib.request.Request(узел(), data=тело,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                о = json.loads(r.read().decode())
            if "error" in о:
                последняя = str(о["error"])[:200]
            else:
                return о.get("result")
        except Exception as exc:  # noqa: BLE001
            последняя = type(exc).__name__
        time.sleep(пауза)
        пауза *= 2
    return {"__почему_нет": последняя}


def строки(путь: str):
    открыть = gzip.open if путь.endswith(".gz") else open
    try:
        with открыть(путь, "rt", encoding="utf-8", errors="replace") as f:
            for с in f:
                с = с.strip()
                if not с:
                    continue
                try:
                    yield json.loads(с)
                except Exception:  # noqa: BLE001
                    continue
    except OSError:
        return


def файлы(каталог: str, имя: str) -> list:
    return [x for x in sorted(glob.glob(str(Path(каталог) / f"{имя}*")))
            if not x.endswith(".tmp")]


def наши_числа(state_dir: str, подпись_источника: str) -> dict:
    """Приём сигнала и отправка -- из журнала решений и позиций."""
    из_ = {"подпись_источника": подпись_источника}
    for путь in файлы(state_dir, "decisions.jsonl"):
        for з in строки(путь):
            п = str(з.get("signature") or "")
            ист = str(з.get("source_sig") or "")
            if not (п.startswith(подпись_источника) or ист.startswith(подпись_источника)):
                continue
            for поле in ("t_recv_utc", "t_recv_ts", "slot_lag",
                          "net_slot_at_decision", "source_slot",
                          "decide_latency_ms", "parse_ms", "gate_ms",
                          "seen_lag_ms", "net_slot_age_s"):
                if з.get(поле) is not None and из_.get(поле) is None:
                    из_[поле] = з[поле]
            if з.get("stage") == "sent":
                из_["наша_подпись_отправки"] = з.get("signature")
    for путь in файлы(state_dir, "positions.jsonl"):
        for з in строки(путь):
            if not str(з.get("source_sig") or "").startswith(подпись_источника):
                continue
            for поле in ("ts_intent", "ts_sent", "ts_accepted",
                          "lane_landed_slot", "lane_landed_signature",
                          "lane_pool_winner", "signal_recv_ts", "seen_lag_ms"):
                if з.get(поле) is not None:
                    из_[поле] = з[поле]
    return из_


def разбор(tx: dict, минт: str) -> dict:
    """Кто, сколько SOL и кто получил токен. Только по meta, без догадок."""
    meta = (tx or {}).get("meta") or {}
    сооб = ((tx or {}).get("transaction") or {}).get("message") or {}
    ключи = [(к.get("pubkey") if isinstance(к, dict) else к)
             for к in (сооб.get("accountKeys") or [])]
    плательщик = ключи[0] if ключи else None
    до = {}
    for б in meta.get("preTokenBalances") or []:
        if б.get("mint") == минт:
            до[(б.get("owner"), б.get("accountIndex"))] = float(
                ((б.get("uiTokenAmount") or {}).get("uiAmount")) or 0.0)
    после = {}
    for б in meta.get("postTokenBalances") or []:
        if б.get("mint") == минт:
            после[(б.get("owner"), б.get("accountIndex"))] = float(
                ((б.get("uiTokenAmount") or {}).get("uiAmount")) or 0.0)
    дельты = {}
    for ключ in set(до) | set(после):
        д = после.get(ключ, 0.0) - до.get(ключ, 0.0)
        if abs(д) > 0:
            дельты[ключ[0]] = round(дельты.get(ключ[0], 0.0) + д, 9)
    получатели = sorted([(в, д) for в, д in дельты.items() if д > 0],
                        key=lambda x: -x[1])
    отдали = sorted([(в, д) for в, д in дельты.items() if д < 0],
                    key=lambda x: x[1])
    # SOL: по нативным балансам плательщика и по WSOL, если он был
    до_л = (meta.get("preBalances") or [None])[0]
    после_л = (meta.get("postBalances") or [None])[0]
    натив = (None if до_л is None or после_л is None
             else round((после_л - до_л) / ЛАМПОРТОВ_В_SOL, 9))
    wsol = 0.0
    for список, знак in ((meta.get("preTokenBalances") or [], -1),
                          (meta.get("postTokenBalances") or [], 1)):
        for б in список:
            if б.get("mint") == WSOL:
                wsol += знак * float(((б.get("uiTokenAmount") or {})
                                      .get("uiAmount")) or 0.0)
    return {"плательщик_газа": плательщик,
             "получатели_токена": получатели[:4],
             "отдали_токен": отдали[:4],
             "натив_дельта_плательщика_sol": натив,
             "wsol_дельта_всех_sol": round(wsol, 9),
             "вид": ("покупка" if получатели else ("продажа" if отдали else "неясно")),
             "программы": sorted({(и.get("programId") or "")
                                  for и in (сооб.get("instructions") or [])
                                  if isinstance(и, dict)}),
             "ошибка": meta.get("err")}


def индексы_блока(слот: int) -> dict:
    """{подпись: индекс в блоке} -- один вызов на слот."""
    б = rpc("getBlock", [слот, {"transactionDetails": "signatures",
                                 "rewards": False,
                                 "maxSupportedTransactionVersion": 0}])
    if not isinstance(б, dict) or not б.get("signatures"):
        return {}
    return {п: и for и, п in enumerate(б["signatures"])}


def главное() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--mint", required=True)
    р.add_argument("--nasha-podpis", required=True)
    р.add_argument("--istochnik-podpis", default="")
    р.add_argument("--podpisi", default="",
                    help="начала подписей, которые назвал владелец")
    р.add_argument("--okno-slotov", type=int, default=40)
    р.add_argument("--predel-podpisey", type=int, default=200)
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--gruppy", default="/home/bot/data/sources_2026-09-25.json")
    р.add_argument("--out", default="/tmp/istoriya_pula.json")
    а = р.parse_args()

    итог = {"mint": а.mint, "снято_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                        time.gmtime())}
    наша = rpc("getTransaction", [а.nasha_podpis,
                                   {"encoding": "jsonParsed",
                                    "maxSupportedTransactionVersion": 0}])
    if not isinstance(наша, dict) or not наша.get("meta"):
        итог["почему_нет"] = "нашу транзакцию узел не отдал"
        print(json.dumps(итог, ensure_ascii=False, indent=1))
        return 1
    наш_слот = наша.get("slot")
    итог["наш_слот"] = наш_слот
    итог["наше_время_utc"] = (time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                             time.gmtime(наша.get("blockTime")))
                               if наша.get("blockTime") else None)

    # Имена из файла групп -- чтобы назвать получателя словом, а не адресом.
    имена = {}
    try:
        д = json.loads(Path(а.gruppy).read_text(encoding="utf-8"))
        for имя_г, тело in (д.get("groups") or {}).items():
            for адр, метка in (тело.get("addresses") or {}).items():
                имена[адр] = {"группа": имя_г,
                               "имя": (метка or {}).get("name") if isinstance(метка, dict) else None}
    except Exception as exc:  # noqa: BLE001
        итог["почему_нет_имён"] = f"файл групп не прочитан: {type(exc).__name__}"

    подписи = rpc("getSignaturesForAddress",
                   [а.mint, {"limit": а.predel_podpisey}])
    if not isinstance(подписи, list):
        итог["почему_нет"] = "подписи по минту узел не отдал"
        print(json.dumps(итог, ensure_ascii=False, indent=1))
        return 1
    в_окне = [з for з in подписи
              if isinstance(з.get("slot"), int)
              and abs(з["slot"] - наш_слот) <= а.okno_slotov]
    итог["подписей_по_минту"] = len(подписи)
    итог["в_окне"] = len(в_окне)
    искомые = tuple(x.strip() for x in (а.podpisi or "").split(",") if x.strip())

    кэш_блоков: dict = {}
    ряды = []
    for з in sorted(в_окне, key=lambda x: (x["slot"], x.get("signature") or "")):
        п = з.get("signature")
        tx = rpc("getTransaction", [п, {"encoding": "jsonParsed",
                                         "maxSupportedTransactionVersion": 0}])
        if not isinstance(tx, dict) or not tx.get("meta"):
            ряды.append({"подпись": п, "слот": з.get("slot"),
                          "почему_нет": "транзакцию узел не отдал"})
            continue
        слот = tx.get("slot")
        if слот not in кэш_блоков:
            кэш_блоков[слот] = индексы_блока(слот)
        р_ = разбор(tx, а.mint)
        получатели = []
        for адр, д in р_["получатели_токена"]:
            свед = имена.get(адр) or {}
            получатели.append({"адрес": адр, "получил_ui": д,
                                "имя": свед.get("имя"),
                                "группа": свед.get("группа")})
        ряды.append({
            "подпись": п, "слот": слот,
            "индекс_в_блоке": кэш_блоков[слот].get(п),
            "utc": (time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                   time.gmtime(tx.get("blockTime")))
                     if tx.get("blockTime") else None),
            "наша": (п == а.nasha_podpis),
            "названа_владельцем": bool([x for x in искомые if п.startswith(x)]),
            "вид": р_["вид"], "плательщик_газа": р_["плательщик_газа"],
            "плательщик_имя": (имена.get(р_["плательщик_газа"]) or {}).get("имя"),
            "получатели_токена": получатели,
            "отдали_токен": [{"адрес": а_, "отдал_ui": д} for а_, д in р_["отдали_токен"]],
            "натив_дельта_плательщика_sol": р_["натив_дельта_плательщика_sol"],
            "wsol_дельта_всех_sol": р_["wsol_дельта_всех_sol"],
            "программы": р_["программы"], "ошибка": р_["ошибка"]})
    итог["ряды"] = ряды
    if а.istochnik_podpis:
        итог["наши_числа"] = наши_числа(а.state_dir, а.istochnik_podpis)
    текст = json.dumps(итог, ensure_ascii=False, indent=1)
    print(текст)
    if а.out:
        Path(а.out).write_text(текст, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(главное())
