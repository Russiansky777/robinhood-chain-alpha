#!/usr/bin/env python3
"""Переиграть ОДИН потерянный сигнал: что детектор увидел бы в транзакции.

Зачем. 24.09 в 14:10:50Z разбор сигнала упал (HANDLER_CRASHED), решение
было потеряно целиком: ни записи, ни покупки, ни строки владельцу. Журнал
решений хранит только "тип: текст" исключения, а не то, ЧТО было в
транзакции. Чтобы ответить владельцу на вопросы "была ли это покупка
>= 2 SOL-эквивалента" и "какая именно инструкция сломала разбор", нужен
разбор той самой транзакции тем же кодом.

Скрипт делает ровно это и ничего больше:
  * тянет транзакцию по подписи (getTransaction, jsonParsed) -- ЧТЕНИЕ;
  * называет инструкции, у которых поле parsed НЕ объект (именно эта форма
    и роняла разбор: у memo разобранное значение -- строка);
  * прогоняет тот же сигнал_из_транзакции и ту же в_sol, что и служба, и
    печатает трату в SOL-эквиваленте против порога входа.

Ни одного ордера. Ключ берётся из окружения HELIUS_API_KEY и никуда не
печатается.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_detector as BD  # noqa: E402


def странные_инструкции(tx: dict) -> list:
    """Инструкции, у которых parsed не объект: они и ломали разбор."""
    из_ = []
    пачки = [("внешние", ((tx.get("transaction") or {}).get("message") or {})
               .get("instructions") or [])]
    for гр in ((tx.get("meta") or {}).get("innerInstructions") or []):
        пачки.append((f"внутренние[{(гр or {}).get('index')}]",
                       (гр or {}).get("instructions") or []))
    for где, пачка in пачки:
        for i, ins in enumerate(пачка):
            if not isinstance(ins, dict):
                из_.append({"где": где, "n": i, "программа": None,
                             "тип_parsed": type(ins).__name__,
                             "значение": str(ins)[:60]})
                continue
            разбор = ins.get("parsed")
            if разбор is not None and not isinstance(разбор, dict):
                из_.append({"где": где, "n": i,
                             "программа": ins.get("programId"),
                             "program": ins.get("program"),
                             "тип_parsed": type(разбор).__name__,
                             "значение": str(разбор)[:60]})
    return из_


def разобрать(tx: dict, источник: str, подпись: str, слот, курс_usd):
    """Тот же путь, что у службы: сигнал + трата в SOL-эквиваленте."""
    сиг = BD.сигнал_из_транзакции(tx, источник, подпись=подпись, слот=слот)
    if сиг is None:
        return None, None, "сигнал не собрался: транзакция не похожа на покупку"
    трата, пояснение = BD.в_sol(сиг, курс_usd)
    return сиг, трата, пояснение


def self_test() -> None:
    всего = [0, 0]

    def chk(имя, условие, факт=None):
        всего[0] += 1
        if условие:
            всего[1] += 1
            print(f"  [ok  ] {имя}")
        else:
            print(f"  [ПЛОХО] {имя} -- {факт}")

    tx = {"transaction": {"message": {"instructions": [
            {"programId": "P1", "parsed": {"type": "transferChecked",
                                            "info": {"mint": "M"}}},
            {"programId": "MemoSq4", "program": "spl-memo",
             "parsed": "текст заметки"}]}},
          "meta": {"innerInstructions": [
              {"index": 0, "instructions": [
                  {"programId": "P2", "parsed": ["список", "тоже не объект"]}]}]}}
    с = странные_инструкции(tx)
    chk("строка в parsed найдена и названа", any(
        x["тип_parsed"] == "str" and x["программа"] == "MemoSq4" for x in с), с)
    chk("список в parsed найден во внутренних", any(
        x["тип_parsed"] == "list" and x["где"].startswith("внутренние")
        for x in с), с)
    chk("нормальная инструкция странной не считается", len(с) == 2, с)
    chk("пустая транзакция не роняет разбор", странные_инструкции({}) == [])
    chk("инструкция-не-словарь тоже называется",
        странные_инструкции({"transaction": {"message": {
            "instructions": ["строка вместо инструкции"]}}})[0]["тип_parsed"]
        == "str",
        странные_инструкции({"transaction": {"message": {
            "instructions": ["строка вместо инструкции"]}}}))
    print(f"самопроверка переигровки сигнала: {всего[1]}/{всего[0]}"
           f"{' пройдено' if всего[1] == всего[0] else ' ПРОВАЛ'}")
    if всего[1] != всего[0]:
        raise SystemExit(1)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--signature", default="")
    p.add_argument("--source", default="")
    p.add_argument("--out", default="")
    a = p.parse_args()
    if a.self_test:
        self_test()
        return 0
    if not a.signature or not a.source:
        print("нужны --signature и --source", file=sys.stderr)
        return 2

    helius = BD.Helius(служба="bloom_signal_replay")
    r = helius.call("getTransaction", [a.signature, {
        "encoding": "jsonParsed", "commitment": "confirmed",
        "maxSupportedTransactionVersion": BD.ПОТОЛОК_ВЕРСИИ_TX}])
    tx = (r or {}) if isinstance(r, dict) else {}
    if not tx:
        print(f"транзакция {a.signature[:12]} не отдана узлом", file=sys.stderr)
        return 1

    странные = странные_инструкции(tx)
    print(f"подпись: {a.signature[:16]}..., слот: {tx.get('slot')}")
    print(f"инструкций с parsed не-объектом: {len(странные)}")
    for x in странные:
        print(f"  {x['где']}[{x['n']}] программа {x.get('программа')} "
               f"({x.get('program')}): parsed -- {x['тип_parsed']} "
               f"{x['значение']!r}")

    курс = None
    try:
        курс = BD.КурсSOL().получить()
    except Exception as exc:  # noqa: BLE001
        print(f"курс SOL/USD не получен: {type(exc).__name__}", file=sys.stderr)
    сиг, трата, пояснение = разобрать(tx, a.source, a.signature, tx.get("slot"),
                                       курс)
    итог = {"signature": a.signature, "source": a.source,
             "slot": tx.get("slot"), "odd_parsed": странные,
             "rate_usd": курс, "spend_sol_equiv": трата,
             "why": пояснение, "threshold_sol": BD.ПОРОГ_ВХОДА_SOL,
             "above_threshold": (трата is not None
                                  and трата >= BD.ПОРОГ_ВХОДА_SOL),
             "signal": сиг}
    print(f"курс SOL/USD: {курс}")
    if сиг is not None:
        print(f"покупка минта: {сиг.get('buy_mint')}, "
               f"трата: {сиг.get('spend_ui')} {сиг.get('spend_mint')}")
    print(f"трата в SOL-эквиваленте: {трата} ({пояснение})")
    print(f"порог входа: {BD.ПОРОГ_ВХОДА_SOL} SOL -- "
           f"{'ВЫШЕ порога' if итог['above_threshold'] else 'НИЖЕ порога'}")
    if a.out:
        Path(a.out).write_text(json.dumps(итог, ensure_ascii=False, indent=1),
                                encoding="utf-8")
        print(f"записано: {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
