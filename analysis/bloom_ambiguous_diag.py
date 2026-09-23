#!/usr/bin/env python3
"""Почему разбор назвал сделку неоднозначной. Диагностика по цепи.

Повод. В первые же минуты боевого dry-run пять сигналов от ОДНОГО
источника (покупки токенов *pump на Pump.fun) получили AMBIGUOUS_TX с
причиной "минт вырос, но ни SOL, ни стейблов не потрачено". Токенная
сторона разобралась, нативная -- нет. Если причина в разборе, а не в
данных, мы слепы ко всем покупкам этого источника, и назвать это надо ДО
live.

Рабочая гипотеза, которую скрипт проверяет, а не принимает на веру:
нативный баланс берётся по индексу кошелька в message.accountKeys, а
pre/postBalances нумерованы по ПОЛНОМУ списку счетов, включая
подгруженные из таблиц адресов (address lookup tables). Если источник
попал в подгруженные, его индекс не находится, и трата в SOL выходит
нулевой -- при том что postTokenBalances.owner его прекрасно видит.

Только чтение цепи (getTransaction).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_detector as BD  # noqa: E402
import bloom_exec_state as ST  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
ВЫХОД = REPO_ROOT / "data" / "bloom_ambiguous_diag.json"

# Записи follow не несут подписи транзакции источника, поэтому вердикт
# DBot сопоставляется по тройке "источник + купленный минт + время".
# Это ограничение ДАННЫХ, а не выбор удобства, и в отчёте оно названо.
ОКНО_СОПОСТАВЛЕНИЯ_С = 180.0


def вердикт_dbot(записи: list, *, источник: str, минт: str,
                  время_сделки: float, окно_с: float = ОКНО_СОПОСТАВЛЕНИЯ_С) -> dict:
    """Что DBot сделал на той же сделке источника.

    Совпадением считается запись той же задачи с тем же источником и тем
    же купленным минтом, ближайшая по времени в пределах окна. Несколько
    кандидатов -- берём ближайший и говорим, сколько их было: молча
    выбирать один из нескольких нельзя.
    """
    кандидаты = []
    for r in записи:
        f = r.get("follow") or {}
        if f.get("wallet") != источник:
            continue
        м = (((f.get("receive") or {}).get("info")) or {}).get("contract")
        if м != минт:
            continue
        t = (r.get("createAt") or 0) / 1000.0
        d = abs(t - время_сделки)
        if d <= окно_с:
            кандидаты.append((d, r))
    if not кандидаты:
        return {"result": "не_видел",
                 "note": (f"записи DBot по этому источнику и минту в пределах "
                               f"{окно_с:.0f} с не найдено")}
    кандидаты.sort(key=lambda x: x[0])
    d, r = кандидаты[0]
    причина = r.get("skipReason")
    return {"result": "купил" if not причина else "отказал",
             "код_dbot": причина or "ПРОШЛО",
             "состояние": r.get("state"),
             "error": r.get("errorMessage") or None,
             "расхождение_по_времени_с": round(d, 1),
             "кандидатов_в_окне": len(кандидаты),
             "id_записи": r.get("id")}


def записи_dbot(ключ: str, задачи: tuple, конфиг: Path) -> list:
    """Свежие записи follow нужных задач. Только GET."""
    from solana_ledger_run import fetch_follow_trades_for_task  # noqa: PLC0415
    конф = json.loads(конфиг.read_text(encoding="utf-8"))
    # Через BD.тело_ответа: снимок хранит тело под ключом "тело", и
    # прямое чтение "body" давало пустой список -- то есть записи DBot не
    # загружались ВООБЩЕ, а сверка честно писала "записи не нашлось".
    res = BD.тело_ответа(конф).get("res") or []
    out = []
    for t in res:
        if t.get("name") in задачи and t.get("id"):
            записи, полностью = fetch_follow_trades_for_task(t["id"], ключ)
            for r in записи:
                r["_полностью"] = полностью
            out.extend(записи)
    return out


def ключи_сообщения(tx: dict) -> list:
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    out = []
    for k in msg.get("accountKeys") or []:
        out.append(k.get("pubkey") if isinstance(k, dict) else k)
    return out


def подгруженные(tx: dict) -> dict:
    la = ((tx or {}).get("meta") or {}).get("loadedAddresses") or {}
    return {"writable": la.get("writable") or [], "readonly": la.get("readonly") or []}


def разбор(tx: dict, источник: str) -> dict:
    meta = (tx or {}).get("meta") or {}
    ключи = ключи_сообщения(tx)
    la = подгруженные(tx)
    полный = list(ключи) + list(la["writable"]) + list(la["readonly"])
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []

    def дельта(список, адрес):
        if адрес not in список:
            return None, None
        i = список.index(адрес)
        if i >= len(pre) or i >= len(post):
            return None, i
        d = int(post[i]) - int(pre[i])
        if i == 0:
            d += int(meta.get("fee") or 0)
        return d / BD.LAMPORT, i

    d_ключи, i_ключи = дельта(ключи, источник)
    d_полный, i_полный = дельта(полный, источник)

    владельцы = {b.get("owner") for b in (meta.get("postTokenBalances") or [])
                 if isinstance(b, dict)}
    б = BD.балансы_кошелька(tx, источник)
    сиг = BD.сигнал_из_транзакции(tx, источник, подпись="?", слот=(tx or {}).get("slot"))
    return {
        "версия": (tx or {}).get("version"),
        "счетов_в_сообщении": len(ключи),
        "подгружено_writable": len(la["writable"]),
        "подгружено_readonly": len(la["readonly"]),
        "счетов_всего": len(полный),
        "длина_preBalances": len(pre),
        "длина_postBalances": len(post),
        "индексы_сходятся_с_сообщением": len(pre) == len(ключи),
        "индексы_сходятся_с_полным": len(pre) == len(полный),
        "источник_в_сообщении": источник in ключи,
        "источник_в_подгруженных": источник in (la["writable"] + la["readonly"]),
        "источник_владелец_токенсчёта": источник in владельцы,
        "индекс_по_сообщению": i_ключи,
        "индекс_по_полному": i_полный,
        "натив_дельта_по_сообщению_sol": d_ключи,
        "натив_дельта_по_полному_sol": d_полный,
        "нынешний_разбор_натив_sol": б["native_delta_sol"],
        "нынешний_тип": сиг.get("kind"),
        "нынешняя_причина": сиг.get("decide_reason"),
        "mint": сиг.get("mint"),
        "токенные_дельты": {m: з.get("delta_ui") for m, з in б["by_mint"].items()},
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--sig", action="append", default=[],
                    help="подпись; можно повторять")
    p.add_argument("--source", required=False, help="адрес источника")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--with-dbot", action="store_true",
                    help="сопоставить с вердиктом DBot по записям follow")
    p.add_argument("--tasks", default="BATCH-5,BATCH-3")
    p.add_argument("--config", default=str(REPO_ROOT / "data" / "final" /
                                            "20260923T145755Z" / "konfig.json"))
    a = p.parse_args()

    if a.self_test:
        проверки = []

        def chk(имя, ок, факт=""):
            проверки.append((имя, bool(ок), факт))

        # Транзакция с таблицей адресов: источник ТОЛЬКО в подгруженных.
        tx = {"version": 0,
               "transaction": {"message": {"accountKeys": [
                   {"pubkey": "ПЛАТЕЛЬЩИК"}, {"pubkey": "ПРОГРАММА"}]}},
               "meta": {"fee": 5000,
                         "loadedAddresses": {"writable": ["ИСТ"], "readonly": []},
                         "preBalances": [3_000_000_000, 1, 2_000_000_000],
                         "postBalances": [2_999_995_000, 1, 1_000_000_000],
                         "preTokenBalances": [],
                         "postTokenBalances": [{"accountIndex": 5, "mint": "М",
                                                 "owner": "ИСТ",
                                                 "uiTokenAmount": {"amount": "7", "decimals": 0}}],
                         "innerInstructions": []}}
        р = разбор(tx, "ИСТ")
        chk("источник не найден в accountKeys", р["источник_в_сообщении"] is False)
        chk("источник найден в подгруженных", р["источник_в_подгруженных"] is True)
        chk("по сообщению нативной дельты нет",
            р["натив_дельта_по_сообщению_sol"] is None, р["натив_дельта_по_сообщению_sol"])
        chk("по полному списку дельта находится",
            abs(р["натив_дельта_по_полному_sol"] + 1.0) < 1e-9,
            р["натив_дельта_по_полному_sol"])
        chk("длина preBalances сходится с полным списком",
            р["индексы_сходятся_с_полным"] is True and р["индексы_сходятся_с_сообщением"] is False)
        chk("нынешний разбор даёт ноль по нативу",
            р["нынешний_разбор_натив_sol"] == 0.0, р["нынешний_разбор_натив_sol"])
        chk("и потому зовёт её получением, а не покупкой",
            р["нынешний_тип"] == "received", р["нынешний_тип"])
        chk("токенный рост при этом виден", р["токенные_дельты"].get("М") == 7,
            р["токенные_дельты"])

        # сопоставление с вердиктом DBot
        зап = [
            {"id": "A", "createAt": 1_000_000, "skipReason": None, "state": "done",
             "follow": {"wallet": "ИСТ", "receive": {"info": {"contract": "М"}}}},
            {"id": "B", "createAt": 1_010_000, "skipReason": "DUPLICATE_TOKEN_BUY",
             "state": "fail",
             "follow": {"wallet": "ИСТ", "receive": {"info": {"contract": "М"}}}},
            {"id": "C", "createAt": 1_000_000, "skipReason": None, "state": "done",
             "follow": {"wallet": "ДРУГОЙ", "receive": {"info": {"contract": "М"}}}},
        ]
        в = вердикт_dbot(зап, источник="ИСТ", минт="М", время_сделки=1000.0)
        chk("вердикт DBot найден по источнику и минту", в["result"] == "купил", в)
        chk("чужой источник не подхвачен", в["id_записи"] == "A", в)
        chk("второй кандидат в окне посчитан", в["кандидатов_в_окне"] == 2, в)
        в2 = вердикт_dbot(зап, источник="ИСТ", минт="М", время_сделки=1010.0)
        chk("ближайшая по времени -- это отказ", в2["код_dbot"] == "DUPLICATE_TOKEN_BUY", в2)
        в3 = вердикт_dbot(зап, источник="ИСТ", минт="ДРУГОЙ_МИНТ", время_сделки=1000.0)
        chk("по другому минту -- не видел", в3["result"] == "не_видел", в3)
        в4 = вердикт_dbot(зап, источник="ИСТ", минт="М", время_сделки=99_000.0)
        chk("вне окна -- не видел", в4["result"] == "не_видел", в4)

        прошло = sum(1 for _, ок, _ in проверки if ок)
        for имя, ок, факт in проверки:
            print(f"  [{'ok  ' if ок else 'ПЛОХО'}] {имя}" + (f" -- {факт}" if not ок else ""))
        print(f"самопроверка диагностики: {прошло}/{len(проверки)} пройдено")
        return 0 if прошло == len(проверки) else 1

    if not a.sig or not a.source:
        p.print_help()
        return 2
    import os  # noqa: PLC0415
    h = BD.Helius(служба="")
    задачи = tuple(x.strip() for x in a.tasks.split(",") if x.strip())
    записи = []
    предупреждение = None
    if a.with_dbot:
        ключ = (os.environ.get("DBOT_API_KEY") or "").strip()
        if not ключ:
            предупреждение = "DBOT_API_KEY не задан -- вердикт DBot не сопоставлялся"
        else:
            try:
                записи = записи_dbot(ключ, задачи, Path(a.config))
            except Exception as exc:  # noqa: BLE001
                предупреждение = f"записи DBot не получены: {type(exc).__name__}: {exc}"
    итог = {"source": a.source, "записей_dbot": len(записи),
             "предупреждение": предупреждение,
             "окно_сопоставления_с": ОКНО_СОПОСТАВЛЕНИЯ_С,
             "оговорка_сопоставления": (
                 "В записях follow НЕТ подписи транзакции источника, поэтому "
                 "вердикт DBot сопоставлен по тройке источник+минт+время, а не "
                 "точным ключом. Это ограничение данных."),
             "сделки": []}
    for s in a.sig:
        tx = h.транзакция(s, попыток=5, пауза_s=0.3)
        if tx is None:
            итог["сделки"].append({"signature": s, "why_not": "getTransaction не отдал"})
            continue
        р = разбор(tx, a.source)
        if записи and р.get("mint"):
            р["dbot"] = вердикт_dbot(записи, источник=a.source, минт=р["mint"],
                                      время_сделки=(tx.get("blockTime") or 0))
        итог["сделки"].append({"signature": s, **р})
    живые = [c for c in итог["сделки"]
              if (c.get("dbot") or {}).get("result") == "купил"]
    итог["живых_расхождений"] = len(живые)
    итог["вывод"] = (
        f"DBot купил на {len(живые)} из {len(итог['сделки'])} -- только их правило "
        f"разбора и надо менять; остальные остаются пропуском"
        if живые else
        "DBot ни на одной из этих сделок не купил: правило разбора менять не на "
        "чем, пропуск остаётся, причина названа")
    ВЫХОД.parent.mkdir(parents=True, exist_ok=True)
    ST.atomic_write_json(ВЫХОД, итог)
    print(json.dumps(итог, ensure_ascii=False, indent=2)[:7000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
