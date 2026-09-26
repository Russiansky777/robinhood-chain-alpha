#!/usr/bin/env python3
"""Через какие программы НАШИ сделки ночи покупали и продавали -- по цепи.

ЗАЧЕМ. Сборщик полосы теперь умеет кривую pump.fun (I.2б), и следующий вопрос
денежный: если полосу на кривую включить, сможем ли мы то, что купили, ПРОДАТЬ.
Утверждение "площадка Bloom и Jupiter умеют pump.fun" -- предположение, пока
нет числа. Число даёт цепь: у каждого токен-счёта исполнителя есть транзакция
создания (это покупка) и последняя транзакция (для пустого счёта это продажа).
По программам в них видно, что мы уже покупали и продавали на кривой, а что на
Pump AMM.

КАК СЧИТАЕМ. Берём счета из data/token_accounts_age_ispolnitel.json (там у
каждого уже есть подпись создания и время). Для каждого счёта: getTransaction
подписи создания и getSignaturesForAddress(limit=1) -- последняя подпись, затем
её getTransaction. В каждой транзакции собираем programId всех инструкций и
оставляем только известные программы пулов (ярлыки c2_pool_programs).

Только чтение. Ни одной подписи, ни одной отправки.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

КРИВАЯ = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
ОПЦИИ_TX = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
            "commitment": "finalized"}


def программы_пулов(tx: dict, ярлыки: dict) -> list:
    """Известные программы пулов, встреченные в транзакции (по порядку)."""
    сообщение = ((tx or {}).get("transaction") or {}).get("message") or {}
    все = list(сообщение.get("instructions") or [])
    for г in ((tx or {}).get("meta") or {}).get("innerInstructions") or []:
        все += list(г.get("instructions") or [])
    из_ = []
    for ix in все:
        pid = ix.get("programId") if isinstance(ix, dict) else None
        if pid in ярлыки and pid not in из_:
            из_.append(pid)
    return из_


def последняя_подпись(rpc, адрес: str) -> str | None:
    try:
        стр = rpc("getSignaturesForAddress", [адрес, {"limit": 1, "commitment": "finalized"}]) or []
    except Exception:  # noqa: BLE001
        return None
    return (стр[0] or {}).get("signature") if стр else None


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--accounts", default=str(КОРЕНЬ / "data" / "token_accounts_age_ispolnitel.json"))
    р.add_argument("--out", default=str(КОРЕНЬ / "data" / "night_pool_programs_of_ours.json"))
    р.add_argument("--limit", type=int, default=30, help="сколько счетов разобрать (0 -- все)")
    р.add_argument("--only-window", type=int, default=1,
                   help="1 -- только счета, созданные в окне ночи (в файле уже отмечено временем)")
    р.add_argument("--since-utc", default="2026-09-25T20:06:44Z")
    а = р.parse_args()

    д = json.loads(Path(а.accounts).read_text(encoding="utf-8"))
    счета = list(д.get("счета") or [])
    порог = time.mktime(time.strptime(а.since_utc, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    if а.only_window:
        счета = [с for с in счета if с.get("created_block_time")
                 and float(с["created_block_time"]) >= порог]
    счета = [с for с in счета if с.get("created_signature")]
    if а.limit:
        счета = счета[:а.limit]

    import c2_pool_programs as PP  # noqa: PLC0415
    from solana_crowd_scan import Rpc, helius_key  # noqa: PLC0415
    ярлыки = PP.labels()
    ключ, имя = helius_key()
    print(f"ключ Helius из {имя}; счетов к разбору: {len(счета)}")
    rpc = Rpc(ключ, service="night_pool_programs")

    ряд, начало = [], time.time()
    for и, с in enumerate(счета, 1):
        строка = {"account": с["account"], "mint": с.get("mint"),
                  "amount_raw": с.get("amount_raw"),
                  "created_signature": с["created_signature"],
                  "buy_programs": None, "sell_signature": None, "sell_programs": None,
                  "why_not": None}
        try:
            tx = rpc.call("getTransaction", [с["created_signature"], ОПЦИИ_TX])
            строка["buy_programs"] = программы_пулов(tx, ярлыки) if tx else []
            п = последняя_подпись(rpc.call, с["account"])
            строка["sell_signature"] = п
            if п and п != с["created_signature"]:
                tx2 = rpc.call("getTransaction", [п, ОПЦИИ_TX])
                строка["sell_programs"] = программы_пулов(tx2, ярлыки) if tx2 else []
            elif п == с["created_signature"]:
                строка["sell_programs"] = []
                строка["why_not"] = "последняя подпись -- та же покупка: счёт после покупки не двигался"
        except Exception as exc:  # noqa: BLE001
            строка["why_not"] = f"{type(exc).__name__}: {str(exc)[:100]}"
        ряд.append(строка)
        if и % 10 == 0 or и == len(счета):
            print(f"  {и}/{len(счета)}, вызовов {rpc.calls}, {time.time()-начало:.0f} с")

    def счёт_по_программам(ключ_поля, строки=None):
        из_: dict = {}
        for с in (ряд if строки is None else строки):
            for pid in (с.get(ключ_поля) or []):
                из_[ярлыки.get(pid, pid)] = из_.get(ярлыки.get(pid, pid), 0) + 1
        return dict(sorted(из_.items(), key=lambda x: -x[1]))

    на_кривой = [с for с in ряд if КРИВАЯ in (с.get("buy_programs") or [])]
    продано_с_кривой = [с for с in на_кривой if с.get("sell_programs")]
    итог = {
        "кошелёк": д.get("кошелёк"),
        "окно_с": а.since_utc,
        "счетов_разобрано": len(ряд),
        "вызовов_rpc": rpc.calls,
        "секунд": round(time.time() - начало, 1),
        "покупки_по_программам": счёт_по_программам("buy_programs"),
        "продажи_по_программам": счёт_по_программам("sell_programs"),
        "кривая_pump_fun": {
            "покупок": len(на_кривой),
            "из_них_есть_продажа": len(продано_с_кривой),
            "продажи_шли_через": счёт_по_программам("sell_programs", на_кривой),
        },
        "ряды": ряд,
    }
    Path(а.out).write_text(json.dumps(итог, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({к: v for к, v in итог.items() if к != "ряды"}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
