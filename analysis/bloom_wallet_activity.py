#!/usr/bin/env python3
"""Кто хозяйничает в кошельке исполнителя. Разбор по цепи, поштучно.

Повод. Сверка при старте нашла на 4s87RRC2... 14 сделок за сутки, которых
нет в нашем журнале, самая свежая -- за 23 минуты до проверки. Владелец
спрашивал именно об этом: выключен ли TradeWiz. Ответить "там кто-то есть"
недостаточно: от того, КТО это, зависит, можно ли начинать стенд.

Решающий признак -- кто платит за транзакцию. Если плательщик сам кошелёк,
значит транзакцию подписал наш приватный ключ: им распоряжается ещё
кто-то, и это запрет на старт. Если плательщик чужой, а кошелёк только
получает, то это входящий перевод или рассылка пыли -- неприятно, но
деньгами кошелька не распоряжается никто.

Классы, которые скрипт различает:
  ПОДПИСАНО_НАШИМ_КЛЮЧОМ -- кошелёк плательщик: кто-то торгует нашим ключом;
  ВХОДЯЩИЙ -- нативный баланс вырос, платил не мы;
  ПОЛУЧЕН_ТОКЕН -- вырос токенный баланс, натив не менялся: рассылка;
  ИСХОДЯЩИЙ_БЕЗ_ПОДПИСИ -- баланс упал, но платил не кошелёк (редкость,
      требует разбора вручную);
  НЕ_НАЙДЕН_В_СЧЕТАХ -- кошелька нет в списке счетов транзакции.

Только чтение цепи. Ни одного ордера.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_exec_state as ST  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "bloom_wallet_activity.json"

КЛАСС_НАШ_КЛЮЧ = "ПОДПИСАНО_НАШИМ_КЛЮЧОМ"
КЛАСС_ВХОДЯЩИЙ = "ВХОДЯЩИЙ"
КЛАСС_ТОКЕН = "ПОЛУЧЕН_ТОКЕН"
КЛАСС_ИСХОДЯЩИЙ_БЕЗ_ПОДПИСИ = "ИСХОДЯЩИЙ_БЕЗ_ПОДПИСИ"
КЛАСС_НЕТ_В_СЧЕТАХ = "НЕ_НАЙДЕН_В_СЧЕТАХ"
КЛАСС_НЕИЗВЕСТНО = "НЕИЗВЕСТНО"


def все_счета(tx: dict) -> list:
    """Полный список счетов: ключи сообщения ПЛЮС подгруженные из таблиц.

    Нумерация pre/postBalances идёт по полному списку, и брать только
    message.accountKeys значило бы искать кошелёк не там, где он лежит.
    """
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    ключи = []
    for k in msg.get("accountKeys") or []:
        ключи.append(k.get("pubkey") if isinstance(k, dict) else k)
    подг = ((tx or {}).get("meta") or {}).get("loadedAddresses") or {}
    for имя in ("writable", "readonly"):
        for k in подг.get(имя) or []:
            ключи.append(k)
    return ключи


def программы(tx: dict) -> list:
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    out = []
    for i in msg.get("instructions") or []:
        pid = (i or {}).get("programId") or (i or {}).get("program")
        if pid and pid not in out:
            out.append(pid)
    for вн in ((tx or {}).get("meta") or {}).get("innerInstructions") or []:
        for i in (вн or {}).get("instructions") or []:
            pid = (i or {}).get("programId") or (i or {}).get("program")
            if pid and pid not in out:
                out.append(pid)
    return out


def разобрать(tx: dict, кошелёк: str) -> dict:
    """Что эта транзакция сделала с кошельком и кто её подписал."""
    meta = (tx or {}).get("meta") or {}
    счета = все_счета(tx)
    плательщик = счета[0] if счета else None
    итог = {"fee_payer": плательщик,
            "wallet_is_fee_payer": плательщик == кошелёк,
            "programs": программы(tx),
            "err": meta.get("err"),
            "fee_lamports": meta.get("fee"),
            "slot": (tx or {}).get("slot"),
            "block_time": (tx or {}).get("blockTime")}
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    try:
        i = счета.index(кошелёк)
    except ValueError:
        i = None
    if i is None or i >= len(pre) or i >= len(post):
        итог["native_delta_sol"] = None
        итог["class"] = КЛАСС_НЕТ_В_СЧЕТАХ
        итог["note"] = ("кошелька нет в списке счетов или списки балансов "
                        "короче списка счетов")
        return итог
    дельта = (post[i] - pre[i]) / 1_000_000_000
    итог["native_delta_sol"] = round(дельта, 9)

    токены = []
    for имя, сп in (("pre", meta.get("preTokenBalances") or []),
                    ("post", meta.get("postTokenBalances") or [])):
        for b in сп:
            if (b or {}).get("owner") != кошелёк:
                continue
            токены.append({"when": имя, "mint": b.get("mint"),
                           "amount": ((b.get("uiTokenAmount") or {})
                                      .get("uiAmountString"))})
    итог["token_balances"] = токены

    if итог["wallet_is_fee_payer"]:
        итог["class"] = КЛАСС_НАШ_КЛЮЧ
        итог["note"] = ("кошелёк платит за транзакцию -- её подписал наш "
                        "приватный ключ")
    elif дельта > 0:
        итог["class"] = КЛАСС_ВХОДЯЩИЙ
        итог["note"] = "нативный баланс вырос, платил не кошелёк"
    elif дельта == 0 and токены:
        итог["class"] = КЛАСС_ТОКЕН
        итог["note"] = ("нативный баланс не менялся, менялся токенный -- "
                        "похоже на рассылку или перевод токена")
    elif дельта < 0:
        итог["class"] = КЛАСС_ИСХОДЯЩИЙ_БЕЗ_ПОДПИСИ
        итог["note"] = ("баланс упал, но плательщик не кошелёк -- разобрать "
                        "вручную, это не обычный случай")
    else:
        итог["class"] = КЛАСС_НЕИЗВЕСТНО
        итог["note"] = "ни натив, ни токены кошелька не изменились"
    return итог


def сводка(разборы: list) -> dict:
    по_классам: dict = {}
    for r in разборы:
        по_классам[r["class"]] = по_классам.get(r["class"], 0) + 1
    наши_подписи = [r for r in разборы if r["class"] == КЛАСС_НАШ_КЛЮЧ]
    траты = round(sum(r["native_delta_sol"] for r in разборы
                      if isinstance(r.get("native_delta_sol"), float)
                      and r["native_delta_sol"] < 0), 9)
    return {"by_class": по_классам,
            "signed_by_our_key": len(наши_подписи),
            "net_outflow_sol": траты,
            "verdict": ("НАШИМ КЛЮЧОМ РАСПОРЯЖАЕТСЯ КТО-ТО ЕЩЁ: "
                        f"{len(наши_подписи)} транзакций подписано кошельком"
                        if наши_подписи else
                        "кошельком никто не распоряжается: все найденные "
                        "транзакции подписаны не нашим ключом")}


def self_test() -> int:
    checks = []

    def chk(имя, ок, факт=""):
        checks.append((имя, bool(ок), факт))

    К = "КОШЕЛЁК"

    def tx(*, ключи, pre, post, плательщик_первый=True, токены=(),
           подгруженные=None, err=None):
        сп = list(ключи)
        return {"slot": 7, "blockTime": 100,
                "transaction": {"message": {
                    "accountKeys": [{"pubkey": k} for k in сп],
                    "instructions": [{"programId": "PROG"}]}},
                "meta": {"preBalances": pre, "postBalances": post,
                         "err": err, "fee": 5000,
                         "loadedAddresses": подгруженные or {},
                         "preTokenBalances": [], "postTokenBalances": list(токены),
                         "innerInstructions": []}}

    # кошелёк -- плательщик: подписано нашим ключом
    r = разобрать(tx(ключи=[К, "ДРУГОЙ"], pre=[2_000_000_000, 0],
                     post=[1_900_000_000, 0]), К)
    chk("кошелёк-плательщик распознан как наш ключ", r["class"] == КЛАСС_НАШ_КЛЮЧ, r)
    chk("трата посчитана", r["native_delta_sol"] == -0.1, r["native_delta_sol"])

    # входящий перевод: платил другой, баланс вырос
    r = разобрать(tx(ключи=["ЧУЖОЙ", К], pre=[5_000_000_000, 0],
                     post=[4_000_000_000, 1_000_000_000]), К)
    chk("входящий перевод распознан", r["class"] == КЛАСС_ВХОДЯЩИЙ, r)
    chk("и он не считается нашей подписью", r["wallet_is_fee_payer"] is False, r)

    # рассылка токена: натив не менялся
    r = разобрать(tx(ключи=["ЧУЖОЙ", К], pre=[5_000_000_000, 1_000],
                     post=[4_999_000_000, 1_000],
                     токены=[{"owner": К, "mint": "ПЫЛЬ",
                              "uiTokenAmount": {"uiAmountString": "1000"}}]), К)
    chk("рассылка токена распознана", r["class"] == КЛАСС_ТОКЕН, r)
    chk("минт рассылки назван",
        r["token_balances"][0]["mint"] == "ПЫЛЬ", r["token_balances"])

    # кошелёк найден среди ПОДГРУЖЕННЫХ счетов, а не в ключах сообщения
    r = разобрать(tx(ключи=["ЧУЖОЙ"], pre=[5_000_000_000, 0],
                     post=[4_000_000_000, 1_000_000_000],
                     подгруженные={"writable": [К], "readonly": []}), К)
    chk("кошелёк из подгруженных счетов найден",
        r["class"] == КЛАСС_ВХОДЯЩИЙ and r["native_delta_sol"] == 1.0, r)

    # кошелька в транзакции нет -- честное "не найден", а не нулевая дельта
    r = разобрать(tx(ключи=["A", "B"], pre=[1, 2], post=[1, 2]), К)
    chk("отсутствие кошелька названо, а не выдано за ноль",
        r["class"] == КЛАСС_НЕТ_В_СЧЕТАХ and r["native_delta_sol"] is None, r)

    # списки балансов короче списка счетов -- тоже не выдумываем дельту
    r = разобрать(tx(ключи=["A", К], pre=[1], post=[1]), К)
    chk("короткие списки балансов не дают выдуманной дельты",
        r["class"] == КЛАСС_НЕТ_В_СЧЕТАХ, r)

    # сводка: одна наша подпись меняет вердикт
    с = сводка([
        разобрать(tx(ключи=[К], pre=[2_000_000_000], post=[1_900_000_000]), К),
        разобрать(tx(ключи=["ЧУЖОЙ", К], pre=[5_000_000_000, 0],
                     post=[4_000_000_000, 1_000_000_000]), К)])
    chk("одна подпись нашим ключом видна в сводке",
        с["signed_by_our_key"] == 1, с)
    chk("и вердикт называет это прямо",
        "РАСПОРЯЖАЕТСЯ" in с["verdict"], с["verdict"])
    chk("отток посчитан только по тратам", с["net_outflow_sol"] == -0.1, с)

    с2 = сводка([разобрать(tx(ключи=["ЧУЖОЙ", К], pre=[5_000_000_000, 0],
                              post=[4_000_000_000, 1_000_000_000]), К)])
    chk("без наших подписей вердикт спокойный",
        с2["signed_by_our_key"] == 0 and "никто не распоряжается" in с2["verdict"],
        с2["verdict"])

    прошло = sum(1 for _, ок, _ in checks if ок)
    for имя, ок, факт in checks:
        print(f"  [{'ok  ' if ок else 'ПЛОХО'}] {имя}" + (f" -- {факт}" if not ок else ""))
    print(f"самопроверка разбора кошелька: {прошло}/{len(checks)} пройдено")
    return 0 if прошло == len(checks) else 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--wallet", default=ST.EXECUTOR_WALLET)
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--hours", type=float, default=24.0,
                   help="окно, в котором разбирать сделки поштучно")
    a = p.parse_args()
    if a.self_test:
        return self_test()

    import bloom_detector as BD  # noqa: PLC0415
    helius = BD.Helius(служба="")
    порог = time.time() - a.hours * 3600

    подписи = helius.call("getSignaturesForAddress",
                          [a.wallet, {"limit": a.limit}]) or []
    свежие = [r for r in подписи
              if not isinstance(r.get("blockTime"), (int, float))
              or r["blockTime"] >= порог]
    разборы = []
    for r in свежие:
        sig = r.get("signature")
        if not sig:
            continue
        try:
            tx = helius.call("getTransaction",
                             [sig, {"encoding": "jsonParsed",
                                    "maxSupportedTransactionVersion": 0}])
        except Exception as exc:  # noqa: BLE001
            разборы.append({"signature": sig, "class": КЛАСС_НЕИЗВЕСТНО,
                            "note": f"getTransaction не отдался: {type(exc).__name__}"})
            continue
        разбор = разобрать(tx or {}, a.wallet)
        разбор["signature"] = sig
        разбор["utc"] = (time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                      time.gmtime(разбор["block_time"]))
                         if разбор.get("block_time") else None)
        разборы.append(разбор)

    итог = {ST.SCHEMA_VERSION_KEY: ST.SCHEMA_VERSION,
            "checked_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "wallet": a.wallet,
            "window_h": a.hours,
            "signatures_seen": len(подписи),
            "in_window": len(свежие),
            "truncated": len(подписи) >= a.limit,
            "transactions": разборы,
            "summary": сводка([r for r in разборы if r.get("class")])}
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(итог, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(json.dumps(итог, ensure_ascii=False, indent=2))
    print("--- коротко ---")
    print(f"кошелёк {a.wallet}, окно {a.hours} ч, подписей {len(подписи)} "
          f"(в окне {len(свежие)}, список урезан: {итог['truncated']})")
    for r in разборы:
        print(f"  {r.get('utc')} {r['class']}: дельта "
              f"{r.get('native_delta_sol')} SOL, плательщик "
              f"{str(r.get('fee_payer'))[:12]}, программы "
              f"{[str(x)[:12] for x in (r.get('programs') or [])][:3]}")
    print(f"вердикт: {итог['summary']['verdict']}")
    print(f"по классам: {итог['summary']['by_class']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
