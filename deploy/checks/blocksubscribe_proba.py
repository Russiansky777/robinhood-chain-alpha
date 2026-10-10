#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ДАЁТ ЛИ НАШ ПЛАН HELIUS blockSubscribe -- проба, только чтение.

ЗАЧЕМ. Забору S+0 нужен blockhash КАЖДОЙ высоты: он подписывает покупку
хешем блока на 150 высот ниже блока источника, и если нужной высоты в кольце
нет, покупка отказывается. Кольцо сегодня наполняется из ответа
getLatestBlockhash раз в 12 с -- это одна высота из ~55, и проход 10.10 нашёл
ровно это: 0 попаданий из 1000.

Есть два способа наполнять кольцо каждым блоком, и выбор между ними -- это
деньги:

* `blockSubscribe` -- подписка по WebSocket. Платим за соединение, а не за
  каждый блок. Но метод у Solana помечен unstable, и у Helius он есть НЕ НА
  ВСЯКОМ плане. Угадывать нельзя: включить забор на плане, который его не
  даёт, значит остановить покупки группы.
* опрос `getLatestBlockhash` раз в слот -- 1 кредит за вызов (так и записано
  в bloom_detector.обновить_blockhash). При длине слота 0.2176 с это
  ~397 000 вызовов в сутки, то есть ~397 000 кредитов -- при суточном бюджете
  детектора 2 000 000 и расходе 1 900 782 кредита 09.10 это не влезает.

Проба отвечает на ОДИН вопрос: принимает ли наш ключ blockSubscribe и что
именно приходит в уведомлении (высота, хеш, слот) без транзакций.

Ничего не отправляет в цепь и ничего не меняет. Ключ берётся из окружения и
в отчёт не попадает.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# СКОЛЬКО СЛУШАЕМ. Три-четыре блока хватает, чтобы увидеть и подписку, и
# содержимое уведомления, и шаг высоты между ними; дольше слушать -- просто
# дольше держать прогон.
СЕКУНД_СЛУШАТЬ = 12.0
ТАЙМАУТ_ПОДПИСКИ_S = 10.0


def затереть(т: str) -> str:
    т = str(т or "")
    return т.split("api-key=")[0] + "api-key=…" if "api-key=" in т else т


def адрес_ws() -> str:
    """wss-адрес Helius из окружения. Ключ не печатается."""
    к = (os.environ.get("HELIUS_API_KEY") or "").strip()
    прямо = (os.environ.get("BLOOM_HELIUS_WS") or "").strip()
    if прямо:
        return прямо
    if not к:
        raise SystemExit("СТОП: нет HELIUS_API_KEY и нет BLOOM_HELIUS_WS")
    return f"wss://mainnet.helius-rpc.com/?api-key={к}"


async def _проба(секунд: float) -> dict:
    import websockets  # noqa: PLC0415

    из_ = {"ok": False, "why_not": None, "podpiska": None,
           "uvedomlenij": 0, "polya": None, "stroki": [],
           "adres": затереть(адрес_ws()), "sekund": секунд,
           "metod": "blockSubscribe"}
    запрос = {"jsonrpc": "2.0", "id": 1, "method": "blockSubscribe",
              # ВСЁ, ЧТО НУЖНО ЗАБОРУ, И НИЧЕГО БОЛЬШЕ: высота, хеш и слот.
              # transactionDetails=none убирает из уведомления транзакции --
              # это и трафик, и обработка, которых забору не нужно.
              "params": ["all", {"encoding": "json",
                                 "transactionDetails": "none",
                                 "showRewards": False,
                                 "commitment": "confirmed"}]}
    try:
        async with websockets.connect(адрес_ws(), ping_interval=20,
                                      max_size=4 * 1024 * 1024) as соед:
            await соед.send(json.dumps(запрос))
            # ОТВЕТ НА ПОДПИСКУ -- ПЕРВЫМ ДЕЛОМ: именно в нём отказ плана.
            try:
                сырое = await asyncio.wait_for(соед.recv(),
                                               timeout=ТАЙМАУТ_ПОДПИСКИ_S)
            except asyncio.TimeoutError:
                из_["why_not"] = (f"ответа на подписку нет за "
                                  f"{ТАЙМАУТ_ПОДПИСКИ_S} с")
                return из_
            ответ = json.loads(сырое)
            из_["podpiska"] = ответ
            if "error" in ответ:
                из_["why_not"] = ("план отказал в blockSubscribe: "
                                  + json.dumps(ответ["error"],
                                               ensure_ascii=False)[:300])
                return из_
            из_["podpiska_id"] = ответ.get("result")
            кончить = time.time() + float(секунд)
            while time.time() < кончить:
                try:
                    сырое = await asyncio.wait_for(
                        соед.recv(), timeout=max(0.5, кончить - time.time()))
                except asyncio.TimeoutError:
                    break
                try:
                    уведомление = json.loads(сырое)
                except ValueError:
                    continue
                знач = (((уведомление.get("params") or {}).get("result") or {}))
                блок = знач.get("block") or {}
                из_["uvedomlenij"] += 1
                if из_["polya"] is None:
                    из_["polya"] = sorted(блок.keys())
                из_["stroki"].append({
                    "slot": знач.get("slot"),
                    "blockHeight": блок.get("blockHeight"),
                    "blockhash": блок.get("blockhash"),
                    "parentSlot": блок.get("parentSlot"),
                    # ТРАНЗАКЦИЙ БЫТЬ НЕ ДОЛЖНО: мы их не просили, и если они
                    # пришли, уведомление весит в сотни раз больше нужного.
                    "transactions": (len(блок.get("transactions") or [])
                                     if блок.get("transactions") is not None
                                     else None),
                    "bajt": len(сырое)})
            из_["ok"] = из_["uvedomlenij"] > 0
            if not из_["ok"]:
                из_["why_not"] = ("подписка принята, но ни одного уведомления "
                                  f"за {секунд} с")
    except Exception as сбой:  # noqa: BLE001
        из_["why_not"] = f"{type(сбой).__name__}: {затереть(str(сбой))[:300]}"
    return из_


def свод(из_: dict) -> dict:
    """Что из этого следует для кольца забора -- числами, а не словами."""
    стр = из_.get("stroki") or []
    высоты = [с["blockHeight"] for с in стр if isinstance(с.get("blockHeight"), int)]
    хеши = [с["blockhash"] for с in стр if с.get("blockhash")]
    из_["vysot"] = len(высоты)
    из_["hashej"] = len(хеши)
    из_["vse_polya_est"] = bool(высоты) and len(высоты) == len(хеши) == len(стр)
    если_подряд = None
    if len(высоты) >= 2:
        шаги = [b - a for a, b in zip(высоты, высоты[1:])]
        из_["shagi_vysoty"] = шаги
        если_подряд = all(ш == 1 for ш in шаги)
    из_["vysoty_podryad"] = если_подряд
    из_["bajt_srednij"] = (round(sum(с["bajt"] for с in стр) / len(стр))
                           if стр else None)
    из_["s_transakciyami"] = sum(1 for с in стр if (с.get("transactions") or 0))
    # ВЫВОД ДЛЯ КОЛЬЦА. Подписка годится, только если в уведомлении есть ВСЁ
    # нужное забору и высоты идут подряд: дырявая подписка -- то же, что
    # нынешнее кольцо, только дороже.
    из_["godno_dlya_kolca"] = bool(из_.get("ok") and из_["vse_polya_est"]
                                   and (если_подряд is not False))
    return из_


def самопроверка() -> int:
    проверки: list = []

    def chk(имя: str, ок: bool, что=None) -> None:
        проверки.append((имя, bool(ок), что))

    # СВОД СЧИТАЕТСЯ, А НЕ ВЫДУМЫВАЕТСЯ -- проверяем на готовых строках, без сети.
    хорошо = свод({"ok": True, "stroki": [
        {"slot": 10, "blockHeight": 100, "blockhash": "A", "bajt": 300,
         "transactions": 0},
        {"slot": 11, "blockHeight": 101, "blockhash": "B", "bajt": 300,
         "transactions": 0}]})
    chk("подряд идущие высоты с хешами -- годится для кольца",
        хорошо["godno_dlya_kolca"] is True and хорошо["vysoty_podryad"] is True
        and хорошо["shagi_vysoty"] == [1], хорошо)
    дыра = свод({"ok": True, "stroki": [
        {"slot": 10, "blockHeight": 100, "blockhash": "A", "bajt": 300},
        {"slot": 13, "blockHeight": 103, "blockhash": "B", "bajt": 300}]})
    chk("ДЫРА в высотах -- для кольца НЕ годится: дырявая подписка это то же "
        "кольцо, только дороже",
        дыра["godno_dlya_kolca"] is False and дыра["vysoty_podryad"] is False,
        дыра)
    без_хеша = свод({"ok": True, "stroki": [
        {"slot": 10, "blockHeight": 100, "blockhash": None, "bajt": 300}]})
    chk("уведомление без хеша для кольца не годится -- подписывать нечем",
        без_хеша["godno_dlya_kolca"] is False and без_хеша["hashej"] == 0,
        без_хеша)
    отказ = свод({"ok": False, "why_not": "план отказал", "stroki": []})
    chk("отказ плана -- не годится, и причина остаётся в отчёте",
        отказ["godno_dlya_kolca"] is False and отказ["why_not"], отказ)
    chk("одна высота без второй -- про «подряд» не врём, а говорим «неизвестно»",
        свод({"ok": True, "stroki": [{"slot": 1, "blockHeight": 5,
                                      "blockhash": "A", "bajt": 1}]
              })["vysoty_podryad"] is None)
    chk("ключ в адресе затирается",
        затереть("wss://x/?api-key=СЕКРЕТ") == "wss://x/?api-key=…",
        затереть("wss://x/?api-key=СЕКРЕТ"))
    chk("транзакции в уведомлении считаются -- их просить мы не просили",
        хорошо["s_transakciyami"] == 0, хорошо["s_transakciyami"])
    плохо_т = свод({"ok": True, "stroki": [
        {"slot": 1, "blockHeight": 5, "blockhash": "A", "bajt": 900000,
         "transactions": 1200}]})
    chk("уведомление С транзакциями видно числом -- это сотни килобайт впустую",
        плохо_т["s_transakciyami"] == 1
        and плохо_т["bajt_srednij"] == 900000, плохо_т)
    # ЦЕНА ОПРОСА РАЗ В СЛОТ -- ЧИСЛОМ, чтобы выбор был не на слово.
    chk("цена опроса раз в слот названа числом в головном описании модуля",
        "397 000" in (__doc__ or "") and "0.2176" in (__doc__ or ""))
    плохих = [(и, ч) for и, ок, ч in проверки if not ок]
    for и, ок, ч in проверки:
        print(f"  [{'ok  ' if ок else 'ПРОВАЛ'}] {и}"
              + ("" if ок else f" -> {ч}"))
    print(f"самопроверка пробы blockSubscribe: "
          f"{len(проверки) - len(плохих)}/{len(проверки)} пройдено")
    return 1 if плохих else 0


def main() -> int:
    п = argparse.ArgumentParser()
    п.add_argument("--self-test", action="store_true")
    п.add_argument("--sekund", type=float, default=СЕКУНД_СЛУШАТЬ)
    п.add_argument("--out", default="")
    а = п.parse_args()
    if а.self_test:
        return самопроверка()
    из_ = свод(asyncio.run(_проба(а.sekund)))
    текст = json.dumps(из_, ensure_ascii=False, indent=1)
    print(текст)
    if а.out:
        Path(а.out).write_text(текст + "\n", encoding="utf-8")
    # ОТКАЗ ПЛАНА -- НЕ СБОЙ ПРОГОНА: это и есть ответ на вопрос.
    return 0


if __name__ == "__main__":
    sys.exit(main())
