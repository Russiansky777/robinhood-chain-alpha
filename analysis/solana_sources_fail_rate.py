#!/usr/bin/env python3
"""Доля упавших транзакций у наших источников -- по цепи, а не по ощущению.

Зачем. Поток из шредов (RabbitStream) показывает сделку ДО исполнения:
успела она или упала, по нему не узнать. Значит, торговать по нему -- это
покупать до подтверждения, и цена такого решения измеряется одним числом:
какая доля сделок наших источников вообще не доезжает до блока успешной.

Два числа, и оба честные, потому что считаются по-разному:

  * ПО ВСЕМ подписям источника -- дёшево и точно: getSignaturesForAddress
    отдаёт поле err без запроса самой транзакции. Это доля упавших среди
    ВСЕХ его транзакций;
  * ПО ПОКУПКАМ -- на ограниченной выборке: покупку видно только в самой
    транзакции (пришёл токен, ушёл котировочный), поэтому выборка берётся
    не больше заданного предела, и её размер печатается рядом с долей.

Окно. У быстрых источников тысяча последних подписей может не дотянуть до
семи суток. Скрипт НЕ делает вид, что окно семидневное: он печатает, какой
отрезок времени реально покрыт по каждому источнику.

Только чтение. Ни одного ордера.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_detector as BD  # noqa: E402

WSOL = "So11111111111111111111111111111111111111112"
СТАБИЛЬНЫЕ = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
               "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
КОТИРОВОЧНЫЕ = {WSOL} | СТАБИЛЬНЫЕ


def utc(ts) -> str | None:
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def подписи_источника(helius, адрес: str, *, страниц: int, на_странице: int,
                       с_utc: str | None) -> list:
    """Подписи источника постранично. err приходит прямо в списке."""
    из_: list = []
    before = None
    for _ in range(max(1, страниц)):
        параметры = {"limit": max(1, min(1000, на_странице))}
        if before:
            параметры["before"] = before
        пачка = helius.call("getSignaturesForAddress", [адрес, параметры]) or []
        if not пачка:
            break
        for з in пачка:
            t = utc(з.get("blockTime"))
            if с_utc and t and t < с_utc:
                return из_
            из_.append(з)
        before = пачка[-1].get("signature")
        if len(пачка) < параметры["limit"]:
            break
    return из_


def это_покупка(tx: dict, владелец: str) -> bool:
    """Покупка источника: пришёл НЕкотировочный токен, ушёл котировочный."""
    мета = tx.get("meta") or {}
    дельты: dict = {}
    for где, знак in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in (мета.get(где) or []):
            if not isinstance(b, dict) or b.get("owner") != владелец:
                continue
            сумма = (b.get("uiTokenAmount") or {}).get("uiAmount")
            if b.get("mint") is None or not isinstance(сумма, (int, float)):
                continue
            дельты[b["mint"]] = дельты.get(b["mint"], 0.0) + знак * float(сумма)
    пришло = [м for м, v in дельты.items() if v > 0 and м not in КОТИРОВОЧНЫЕ]
    ушло_кот = [м for м, v in дельты.items() if v < 0 and м in КОТИРОВОЧНЫЕ]
    if пришло and ушло_кот:
        return True
    # Покупка за нативный SOL: токен пришёл, нативный баланс упал.
    if пришло:
        ключи = (((tx.get("transaction") or {}).get("message") or {})
                  .get("accountKeys") or [])
        индекс = None
        for i, k in enumerate(ключи):
            адрес = k.get("pubkey") if isinstance(k, dict) else k
            if адрес == владелец:
                индекс = i
                break
        до = мета.get("preBalances") or []
        после = мета.get("postBalances") or []
        if индекс is not None and индекс < len(до) and индекс < len(после):
            return (после[индекс] - до[индекс]) < 0
    return False


def доля(упало: int, всего: int):
    return round(упало / всего, 4) if всего else None


def по_источнику(helius, адрес: str, *, страниц: int, на_странице: int,
                  с_utc: str | None, выборка_покупок: int) -> dict:
    подписи = подписи_источника(helius, адрес, страниц=страниц,
                                 на_странице=на_странице, с_utc=с_utc)
    времена = [utc(з.get("blockTime")) for з in подписи]
    времена = [t for t in времена if t]
    всего = len(подписи)
    упало = sum(1 for з in подписи if з.get("err") is not None)
    из_ = {"source": адрес, "signatures": всего, "failed": упало,
            "failed_share": доля(упало, всего),
            "from_utc": (min(времена) if времена else None),
            "to_utc": (max(времена) if времена else None),
            "window_covered_days": None}
    if времена:
        а = datetime.strptime(min(времена), "%Y-%m-%dT%H:%M:%SZ")
        б = datetime.strptime(max(времена), "%Y-%m-%dT%H:%M:%SZ")
        из_["window_covered_days"] = round((б - а).total_seconds() / 86400.0, 2)
    # Покупки -- на ограниченной выборке, и её размер честно рядом.
    осмотрено = покупок = покупок_упало = 0
    for з in подписи:
        if осмотрено >= выборка_покупок:
            break
        подпись = з.get("signature")
        if not подпись:
            continue
        try:
            tx = helius.транзакция(подпись)
        except Exception:  # noqa: BLE001
            continue
        if not tx:
            continue
        осмотрено += 1
        # У УПАВШЕЙ транзакции балансы не менялись, и "покупкой" по
        # остаткам её не опознать. Поэтому упавшая считается покупкой по
        # намерению: те же программы DEX в инструкциях. Иначе доля упавших
        # среди покупок была бы равна нулю по построению.
        упала = (tx.get("meta") or {}).get("err") is not None
        if упала:
            программы = {(и or {}).get("programId") for и in
                          (((tx.get("transaction") or {}).get("message") or {})
                            .get("instructions") or []) if isinstance(и, dict)}
            похоже = bool(программы & set(getattr(BD, "ПРОГРАММЫ_DEX", set())))
            if похоже:
                покупок += 1
                покупок_упало += 1
            continue
        if это_покупка(tx, адрес):
            покупок += 1
    из_.update(buys_checked=осмотрено, buys=покупок, buys_failed=покупок_упало,
                buys_failed_share=доля(покупок_упало, покупок))
    return из_


def self_test() -> None:
    всего = [0, 0]

    def chk(имя, условие, факт=None):
        всего[0] += 1
        if условие:
            всего[1] += 1
            print(f"  [ok  ] {имя}")
        else:
            print(f"  [ПЛОХО] {имя} -- {факт}")

    chk("доля на нуле -- None, а не ноль", доля(0, 0) is None)
    chk("доля считается", доля(3, 12) == 0.25)

    КОШ = "ИСТ"

    def Б(минт, сумма):
        return {"mint": минт, "owner": КОШ, "uiTokenAmount": {"uiAmount": сумма}}

    покупка = {"meta": {"preTokenBalances": [Б("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 100.0)],
                         "postTokenBalances": [Б("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 30.0),
                                                Б("НОВЫЙ", 500.0)]},
                "transaction": {"message": {"accountKeys": [{"pubkey": КОШ}]}}}
    chk("покупка за стейбл опознана", это_покупка(покупка, КОШ), покупка)

    продажа = {"meta": {"preTokenBalances": [Б("НОВЫЙ", 500.0)],
                         "postTokenBalances": [Б("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 70.0)]},
                "transaction": {"message": {"accountKeys": [{"pubkey": КОШ}]}}}
    chk("продажа покупкой не считается", not это_покупка(продажа, КОШ), продажа)

    за_sol = {"meta": {"preTokenBalances": [], "postTokenBalances": [Б("НОВЫЙ", 5.0)],
                        "preBalances": [2_000_000_000], "postBalances": [1_800_000_000]},
               "transaction": {"message": {"accountKeys": [{"pubkey": КОШ}]}}}
    chk("покупка за нативный SOL опознана", это_покупка(за_sol, КОШ), за_sol)

    чужая = {"meta": {"preTokenBalances": [],
                       "postTokenBalances": [{"mint": "НОВЫЙ", "owner": "ДРУГОЙ",
                                               "uiTokenAmount": {"uiAmount": 5.0}}],
                       "preBalances": [1], "postBalances": [1]},
              "transaction": {"message": {"accountKeys": [{"pubkey": КОШ}]}}}
    chk("чужой токен покупкой источника не считается",
        not это_покупка(чужая, КОШ), чужая)

    class HeliusСписок:
        def __init__(self, страницы):
            self.страницы = list(страницы)
            self.запросов = 0

        def call(self, метод, параметры):
            self.запросов += 1
            return self.страницы.pop(0) if self.страницы else []

        def транзакция(self, подпись, **kw):
            return None

    h = HeliusСписок([[{"signature": "A", "blockTime": 1790000000, "err": None},
                        {"signature": "B", "blockTime": 1789900000,
                         "err": {"InstructionError": [0, "X"]}}],
                       []])
    с = по_источнику(h, "ИСТ", страниц=2, на_странице=1000, с_utc=None,
                      выборка_покупок=0)
    chk("доля упавших среди всех подписей посчитана",
        с["signatures"] == 2 and с["failed"] == 1 and с["failed_share"] == 0.5, с)
    chk("окно покрытия названо в сутках",
        с["window_covered_days"] is not None and с["window_covered_days"] > 1, с)

    h2 = HeliusСписок([[{"signature": "A", "blockTime": 1790000000, "err": None},
                         {"signature": "СТАРОЕ", "blockTime": 1700000000, "err": None}]])
    с2 = по_источнику(h2, "ИСТ", страниц=1, на_странице=1000,
                       с_utc="2026-09-01T00:00:00Z", выборка_покупок=0)
    chk("подписи старше окна отброшены", с2["signatures"] == 1, с2)

    print(f"самопроверка доли упавших: {всего[1]}/{всего[0]}"
           f"{' пройдено' if всего[1] == всего[0] else ' ПРОВАЛ'}")
    if всего[1] != всего[0]:
        raise SystemExit(1)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--tasks", default="BATCH-5,BATCH-3")
    p.add_argument("--config", default="data/dbot_task_config_readonly.json")
    p.add_argument("--sources", default="", help="через запятую; пусто -- из задач DBot")
    p.add_argument("--since", default="", help="UTC, например 2026-09-17T00:00:00Z")
    p.add_argument("--pages", type=int, default=2)
    p.add_argument("--page-size", type=int, default=1000)
    p.add_argument("--buys-sample", type=int, default=60)
    p.add_argument("--out", default="data/solana_sources_fail_rate.json")
    a = p.parse_args()
    if a.self_test:
        self_test()
        return 0
    if a.sources.strip():
        источники = [x.strip() for x in a.sources.split(",") if x.strip()]
        откуда = "заданы входом"
    else:
        задачи = tuple(x.strip() for x in a.tasks.split(",") if x.strip())
        ист, откуда = BD.источники(задачи, Path(a.config))
        источники = sorted(ист)
    helius = BD.Helius(служба="solana_sources_fail_rate")
    строки = []
    for адрес in источники:
        с = по_источнику(helius, адрес, страниц=a.pages, на_странице=a.page_size,
                          с_utc=(a.since or None), выборка_покупок=a.buys_sample)
        строки.append(с)
        дол = с["failed_share"]
        дол_текст = "?" if дол is None else f"{дол * 100:.2f} %"
        print(f"{адрес[:12]}: подписей {с['signatures']}, упало {с['failed']} "
               f"({дол_текст}), окно {с['window_covered_days']} сут; "
               f"покупок в выборке {с['buys']}, из них упало {с['buys_failed']}")
    всего_п = sum(с["signatures"] for с in строки)
    всего_у = sum(с["failed"] for с in строки)
    всего_пок = sum(с["buys"] for с in строки)
    всего_пу = sum(с["buys_failed"] for с in строки)
    итог = {"built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "sources_from": откуда, "since": a.since,
             "sources": строки,
             "total": {"signatures": всего_п, "failed": всего_у,
                        "failed_share": доля(всего_у, всего_п),
                        "buys_checked": всего_пок, "buys_failed": всего_пу,
                        "buys_failed_share": доля(всего_пу, всего_пок)}}
    print(json.dumps(итог["total"], ensure_ascii=False))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(итог, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    print(f"записано: {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
