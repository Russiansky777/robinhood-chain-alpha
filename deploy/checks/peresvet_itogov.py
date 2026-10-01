#!/usr/bin/env python3
"""Пересчёт итога по цепи у закрытых позиций по правилу «распаковка WSOL -- не выручка».

ЗАЧЕМ. Итог сделки считался нативной дельтой кошелька в закрывающей транзакции.
Если в той же транзакции закрывался счёт WSOL с остатком, этот остаток попадал в
итог как выручка. Измерено 01.10: продажа DBT d1hXygBzo дала нативных
+0.104711046 при дельте WSOL -0.1 -- те 0.1 обёрнула и не распаковала ЧУЖАЯ
покупка. Правило новое: выручка = нативная дельта ПЛЮС дельта WSOL.

ЧТО ДЕЛАЕТ. Берёт закрытые позиции с метки, по подписи закрывающей транзакции
перечитывает её из цепи, считает итог ЗАНОВО тем же модулем, которым считает
служба (bloom_seller.итог_продажи), и сравнивает с тем, что лежит в записи.

ПО УМОЛЧАНИЮ ТОЛЬКО СЧИТАЕТ И ПОКАЗЫВАЕТ. Запись в журнал позиций -- по явному
слову (--pisat PERESCHITAT), и пишутся только те поля, которые меняет новое
правило: closed_sol_net, closed_wsol_delta, closed_sol_net_native. Прежнее
нативное число не теряется -- оно и становится closed_sol_net_native.

НЕИЗВЕСТНОЕ ЧИСЛО НЕ СТАНОВИТСЯ НУЛЁМ. Транзакцию не отдали, подписи нет,
дельта WSOL неизвестна -- позиция попадает в раздел «не пересчитано» с причиной
словами, а её прежний итог остаётся как был.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ.get("BLOOM_CODE_DIR") or "/home/bot/bloom_executor")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))


def _узел(урл: str):
    import urllib.request  # noqa: PLC0415

    def зов(метод, параметры):
        тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                            "params": параметры}).encode()
        з = urllib.request.Request(урл, data=тело,
                                   headers={"content-type": "application/json"})
        with urllib.request.urlopen(з, timeout=30) as о:  # noqa: S310
            return json.loads(о.read())

    return зов


def подпись_закрытия(поз: dict) -> str | None:
    """Чем позиция закрылась. Порядок тот же, что у службы."""
    for имя in ("closed_signature", "last_sell_reported"):
        з = (поз or {}).get(имя)
        if з:
            return str(з)
    сп = (поз or {}).get("last_sell_signatures") or []
    return str(сп[0]) if сп else None


def кошелёк_позиции(поз: dict, *, полоса: str, исполнитель: str) -> str:
    return полоса if (поз or {}).get("lane") else исполнитель


def разбор(поз: dict, tx: dict, *, кошелёк: str, итог_фн) -> dict:
    """Новый итог против старого. Ни записи, ни сети."""
    из_ = {"cid": поз.get("client_order_id"), "группа": поз.get("lane_group"),
            "минт": поз.get("mint"), "было": поз.get("closed_sol_net"),
            "стало": None, "wsol": None, "разница": None, "why_not": None}
    исход = итог_фн(tx, кошелёк, поз.get("mint"))
    if not исход.get("known"):
        из_["why_not"] = исход.get("why_not") or "транзакция не разобрана"
        return из_
    из_["wsol"] = исход.get("wsol_delta_sol")
    новое = исход.get("sol_delta_net_swap")
    if not isinstance(новое, (int, float)):
        из_["why_not"] = "выручка свопа не посчитана (дельта WSOL неизвестна)"
        return из_
    из_["стало"] = новое
    было = из_["было"]
    if isinstance(было, (int, float)):
        из_["разница"] = round(новое - было, 9)
    else:
        из_["why_not"] = "прежнего итога в записи нет -- сравнивать не с чем"
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--positions", default=None)
    р.add_argument("--s", default="2026-09-29T14:00:00Z", help="метка начала")
    р.add_argument("--koshelek-polosy", default="")
    р.add_argument("--url", default=None)
    р.add_argument("--pisat", default="", help="ровно PERESCHITAT -- дописать в журнал")
    р.add_argument("--vernut", default="",
                    help="ровно VERNUT -- вернуть closed_sol_net из closed_sol_net_native")
    р.add_argument("--predel", type=int, default=0, help="сколько позиций максимум (0 -- все)")
    р.add_argument("--out", default=None)
    а = р.parse_args()
    try:
        import bloom_exec_state as ST  # noqa: PLC0415
        import bloom_seller as SL  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False,
                           "why_not": f"модули не загружены: {type(exc).__name__}: {exc}"},
                          ensure_ascii=False))
        return 1
    сост = ST.ExecState()
    позиции = сост.positions()
    полоса = (а.koshelek_polosy or os.environ.get("OWN_SEND_WALLET") or "").strip()
    урл = а.url
    if not урл:
        ключ = (os.environ.get("HELIUS_API_KEY") or os.environ.get("HELIUS_API") or "").strip()
        урл = (f"https://mainnet.helius-rpc.com/?api-key={ключ}" if ключ
               else "https://api.mainnet-beta.solana.com")
    зов = _узел(урл)
    итог = {"метка_с": а.s, "позиций_всего": len(позиции), "взято": 0,
             "пересчитано": 0, "сменили_итог": 0, "сумма_разниц": 0.0,
             "не_пересчитано": [], "по_группам": {}, "примеры": [],
             "записано": False, "why_not": None}
    отобраны = []
    for cid, п in позиции.items():
        если_закрыта = str(п.get("state") or "") in ("closed", "unsold")
        метка = str(п.get("ts_intent_utc") or п.get("ts_sent_utc") or "")
        if не_годен := (not если_закрыта or (метка and метка < а.s)):
            continue
        отобраны.append((метка, cid, п))
    отобраны.sort()
    if а.predel:
        отобраны = отобраны[-а.predel:]
    итог["взято"] = len(отобраны)
    правки = []
    for метка, cid, п in отобраны:
        подп = подпись_закрытия(п)
        if not подп:
            итог["не_пересчитано"].append({"cid": cid, "why_not": "подписи закрытия нет"})
            continue
        try:
            о = зов("getTransaction", [подп, {"encoding": "jsonParsed",
                                              "maxSupportedTransactionVersion": 1}])
        except Exception as exc:  # noqa: BLE001
            итог["не_пересчитано"].append({"cid": cid,
                                            "why_not": f"сеть: {type(exc).__name__}"})
            continue
        if о.get("error") or not о.get("result"):
            итог["не_пересчитано"].append({"cid": cid,
                                            "why_not": "узел не отдал транзакцию"})
            continue
        р_ = разбор(п, о["result"],
                     кошелёк=кошелёк_позиции(п, полоса=polosa_или(полоса),
                                              исполнитель=SL.EXECUTOR_WALLET),
                     итог_фн=SL.итог_продажи)
        if р_.get("why_not") and р_.get("стало") is None:
            итог["не_пересчитано"].append({"cid": cid, "why_not": р_["why_not"]})
            continue
        итог["пересчитано"] += 1
        г = р_.get("группа") or "без_группы"
        св = итог["по_группам"].setdefault(г, {"n": 0, "сменили": 0, "сумма_было": 0.0,
                                                "сумма_стало": 0.0})
        св["n"] += 1
        if isinstance(р_["было"], (int, float)):
            св["сумма_было"] = round(св["сумма_было"] + р_["было"], 9)
        св["сумма_стало"] = round(св["сумма_стало"] + р_["стало"], 9)
        if isinstance(р_.get("разница"), (int, float)) and abs(р_["разница"]) > 1e-9:
            итог["сменили_итог"] += 1
            св["сменили"] += 1
            итог["сумма_разниц"] = round(итог["сумма_разниц"] + р_["разница"], 9)
            if len(итог["примеры"]) < 12:
                итог["примеры"].append(р_)
            правки.append((cid, р_))
    # ВОЗВРАТ ПРЕЖНЕГО ЧИСЛА. Понадобился сразу: на closed_sol_net стоит
    # тождество сверки (c2_itog_po_cepi.объяснённое), и выручка свопа в этом поле
    # делает остаток тождества равным перешедшему завёрнутому -- то есть сверка
    # начинает звать владельца на объяснённое. Числа не теряются: прежнее лежит в
    # closed_sol_net_native, выручка свопа -- в closed_sol_net_swap.
    if а.vernut == "VERNUT":
        возвращено = 0
        for cid, п in позиции.items():
            родное = п.get("closed_sol_net_native")
            if родное is None or п.get("closed_itog_peresvet") is None:
                continue
            try:
                сост.update_position(cid, closed_sol_net=родное,
                                     closed_sol_net_swap=п.get("closed_sol_net"),
                                     closed_itog_peresvet=None)
                возвращено += 1
            except Exception as exc:  # noqa: BLE001
                итог["не_пересчитано"].append({"cid": cid,
                                                "why_not": f"возврат: {type(exc).__name__}"})
        итог["возвращено"] = возвращено
        текст_в = json.dumps(итог, ensure_ascii=False, indent=1)
        print(текст_в)
        if а.out:
            Path(а.out).write_text(текст_в + "\n", encoding="utf-8")
        return 0
    if а.pisat == "PERESCHITAT" and правки:
        for cid, р_ in правки:
            try:
                сост.update_position(
                    cid, closed_sol_net=р_["стало"],
                    closed_wsol_delta=р_["wsol"],
                    closed_sol_net_native=р_["было"],
                    closed_itog_peresvet="распаковка WSOL исключена из выручки")
            except Exception as exc:  # noqa: BLE001
                итог["не_пересчитано"].append({"cid": cid,
                                                "why_not": f"запись: {type(exc).__name__}"})
        итог["записано"] = True
    текст = json.dumps(итог, ensure_ascii=False, indent=1)
    print(текст)
    if а.out:
        Path(а.out).write_text(текст + "\n", encoding="utf-8")
    return 0


def polosa_или(знач: str) -> str:
    """Адрес полосы: передали -- он, нет -- спросим у модуля полосы."""
    if знач:
        return знач
    try:
        import bloom_own_send as OSW  # noqa: PLC0415
        return OSW.кошелёк_полосы()
    except Exception:  # noqa: BLE001
        return ""


def self_test() -> int:
    плохо = []

    def chk(имя, усл, факт=""):
        if not усл:
            плохо.append(f"{имя}: {факт}")
        print(("ok   " if усл else "ПЛОХО") + f" {имя}")

    chk("подпись закрытия -- из closed_signature",
        подпись_закрытия({"closed_signature": "A"}) == "A")
    chk("иначе из доложенной продажи",
        подпись_закрытия({"last_sell_reported": "B"}) == "B")
    chk("иначе из списка подписей",
        подпись_закрытия({"last_sell_signatures": ["C"]}) == "C")
    chk("нет ни одной -- None, а не выдуманная строка",
        подпись_закрытия({}) is None)
    chk("кошелёк полосы берётся по метке позиции",
        кошелёк_позиции({"lane": "own_send"}, полоса="ПОЛОСА", исполнитель="ИСП") == "ПОЛОСА"
        and кошелёк_позиции({}, полоса="ПОЛОСА", исполнитель="ИСП") == "ИСП")

    def _итог(tx, кошелёк, минт):
        return dict(tx)

    р = разбор({"client_order_id": "c1", "closed_sol_net": 0.1047, "mint": "М"},
                {"known": True, "wsol_delta_sol": -0.1, "sol_delta_net_swap": 0.0047},
                кошелёк="К", итог_фн=_итог)
    chk("новый итог взят из выручки свопа", р["стало"] == 0.0047, р)
    chk("разница посчитана и она отрицательная",
        р["разница"] == -0.1 and р["why_not"] is None, р)
    р2 = разбор({"client_order_id": "c2", "closed_sol_net": 0.5, "mint": "М"},
                 {"known": True, "wsol_delta_sol": None, "sol_delta_net_swap": None},
                 кошелёк="К", итог_фн=_итог)
    chk("выручка не посчитана -- не пересчитываем и говорим почему",
        р2["стало"] is None and "не посчитана" in р2["why_not"], р2)
    р3 = разбор({"client_order_id": "c3", "mint": "М"},
                 {"known": True, "wsol_delta_sol": 0.0, "sol_delta_net_swap": 0.2},
                 кошелёк="К", итог_фн=_итог)
    chk("прежнего итога нет -- сравнивать не с чем, и это сказано",
        р3["стало"] == 0.2 and "не с чем" in р3["why_not"], р3)
    р4 = разбор({"client_order_id": "c4", "closed_sol_net": 0.2, "mint": "М"},
                 {"known": False, "why_not": "узел не отдал"},
                 кошелёк="К", итог_фн=_итог)
    chk("транзакция не разобрана -- итог не трогаем",
        р4["стало"] is None and р4["why_not"] == "узел не отдал", р4)

    тело = Path(__file__).read_text(encoding="utf-8").split("def self_test")[0]
    chk("запись только по явному слову PERESCHITAT",
        'а.pisat == "PERESCHITAT"' in тело)
    chk("в рабочей части нет ни покупки, ни продажи, ни подписи",
        "sendTransaction" not in тело and "Keypair" not in тело
        and "продать" not in тело, None)

    print(f"\nитог: {'ВСЁ ОК' if not плохо else 'ОТКАЗ'}; проверок плохих {len(плохо)}")
    for с in плохо:
        print("  -", с)
    return 1 if плохо else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    raise SystemExit(main())
