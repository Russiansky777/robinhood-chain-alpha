#!/usr/bin/env python3
"""Когда были созданы незакрытые токен-счета -- и сколько денег заперла ИМЕННО ночь.

ЗАЧЕМ. По цепи у исполнителя 261 токен-счёт, из них 181 пустой, и вместе с
кошельком полосы в них заперто 0.709559 SOL (data/bloom_token_accounts.json).
Но 320 счетов накопились не за одну ночь, поэтому "70 % убытка ночи это рента"
-- утверждение, которое надо либо доказать, либо снять. Доказать можно только
датой рождения каждого счёта.

КАК СЧИТАЕМ. У каждого счёта берём САМУЮ СТАРУЮ подпись
(getSignaturesForAddress с листанием назад до конца) -- это и есть создание
счёта, потому что до создания у адреса истории нет. Время берём из blockTime
той же подписи. Ничего не достраиваем: счёт, по которому узел не отдал истории,
попадает в "дата неизвестна" и в суммы по окнам не идёт.

Только чтение цепи. Ни одной подписи, ни одного закрытия счёта.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

WSOL = "So11111111111111111111111111111111111111112"


def самая_старая(rpc, адрес: str, *, предел: int = 1000,
                 страниц: int = 5) -> dict:
    """Самая старая подпись счёта: листаем назад, пока страница не кончится."""
    из_ = {"signature": None, "block_time": None, "slot": None,
           "pages": 0, "capped": False, "why_not": None}
    курсор = None
    последняя = None
    for _ in range(страниц):
        парам = {"limit": предел, "commitment": "finalized"}
        if курсор:
            парам["before"] = курсор
        try:
            стр = rpc("getSignaturesForAddress", [адрес, парам]) or []
        except Exception as exc:  # noqa: BLE001
            из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:100]}"
            return из_
        из_["pages"] += 1
        if not стр:
            break
        последняя = стр[-1]
        if len(стр) < предел:
            break
        курсор = последняя.get("signature")
    else:
        из_["capped"] = True
    if последняя is None:
        из_["why_not"] = "истории по счёту нет вовсе"
        return из_
    из_.update(signature=последняя.get("signature"),
               block_time=последняя.get("blockTime"),
               slot=последняя.get("slot"))
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--accounts", default=str(КОРЕНЬ / "data" / "bloom_token_accounts.json"))
    р.add_argument("--since-utc", default="2026-09-25T20:06:44Z",
                   help="начало окна ночи -- по нему считается доля, запертая ночью")
    р.add_argument("--out", default=str(КОРЕНЬ / "data" / "token_accounts_age.json"))
    р.add_argument("--limit", type=int, default=0)
    а = р.parse_args()

    д = json.loads(Path(а.accounts).read_text(encoding="utf-8"))
    счета = (д.get("empty") or []) + (д.get("with_balance") or [])
    if а.limit:
        счета = счета[:а.limit]
    порог = time.mktime(time.strptime(а.since_utc, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone

    from solana_crowd_scan import Rpc, helius_key  # noqa: PLC0415
    ключ, имя = helius_key()
    print(f"ключ Helius из {имя}; счетов к разбору: {len(счета)}")
    rpc = Rpc(ключ, service="token_accounts_age")

    ряд = []
    начало = time.time()
    for и, с in enumerate(счета, 1):
        в = самая_старая(rpc.call, с["account"])
        ряд.append({**с, **{"created_signature": в["signature"],
                            "created_block_time": в["block_time"],
                            "created_slot": в["slot"],
                            "pages": в["pages"], "capped": в["capped"],
                            "why_not": в["why_not"]}})
        if и % 25 == 0 or и == len(счета):
            print(f"  {и}/{len(счета)}, вызовов {rpc.calls}, {time.time()-начало:.0f} с")

    def деньги(з):
        # ЗАПЕРТО НА СЧЁТЕ -- ЭТО ЛАМПОРТЫ САМОГО СЧЁТА, И ВСЁ. Поле rent_sol
        # приходит из bloom_token_accounts.py и содержит ПОЛНЫЙ баланс счёта в
        # лампортах: у обычного токен-счёта это 0.00148844, у счёта WSOL --
        # 0.00297688, где половина и есть завёрнутый SOL (amount_ui).
        #
        # ОШИБКА, КОТОРУЮ ЭТО ИСПРАВЛЯЕТ (26.09). Здесь к rent_sol прибавлялся
        # ещё и amount_ui счёта WSOL -- то есть завёрнутый SOL считался ДВАЖДЫ.
        # В ночном докладе из-за этого вышло "заперто 0.714136 SOL" вместо
        # настоящих 0.599526; закрытие счетов днём вернуло 0.594809 и показало
        # ошибку числом. Остаток прочих минтов деньгами не считаем вовсе: его
        # цена неизвестна, и выдумывать её нельзя.
        return float(з.get("rent_sol") or 0.0)

    ночью = [з for з in ряд if з.get("created_block_time")
             and float(з["created_block_time"]) >= порог]
    раньше = [з for з in ряд if з.get("created_block_time")
              and float(з["created_block_time"]) < порог]
    без_даты = [з for з in ряд if not з.get("created_block_time")]
    итог = {
        "кошелёк": д.get("wallet"),
        "since_utc": а.since_utc,
        "счетов": len(ряд),
        "вызовов_rpc": rpc.calls,
        "секунд": round(time.time() - начало, 1),
        "создано_в_окне": {"счетов": len(ночью),
                           "заперто_sol": round(sum(деньги(з) for з in ночью), 9)},
        "создано_раньше": {"счетов": len(раньше),
                           "заперто_sol": round(sum(деньги(з) for з in раньше), 9)},
        "дата_неизвестна": {"счетов": len(без_даты),
                            "заперто_sol": round(sum(деньги(з) for з in без_даты), 9)},
        "счета": ряд,
    }
    Path(а.out).write_text(json.dumps(итог, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    print(json.dumps({к: v for к, v in итог.items() if к != "счета"},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
