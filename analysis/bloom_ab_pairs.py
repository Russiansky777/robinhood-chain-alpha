#!/usr/bin/env python3
"""Пары А/Б по цепи: мы против DBot на ОДНОМ событии источника. Только чтение.

Вопрос владельца звучит так: на пяти первых боевых сделках -- что получил
DBot на тех же событиях, и где мы стояли относительно него и относительно
толпы. Отвечается только фактами цепи:

  * НАШ вход и выход берутся из записи позиции (сколько SOL ушло и сколько
    вернулось) -- это уже сверено по цепи сторожем;
  * ВХОД DBot ищется в блоках вокруг сделки источника по его кошельку задачи
    (walletAddress из снимка конфига): транзакция, в которой у этого кошелька
    ВЫРОС тот же минт. Оттуда же берутся слот и индекс в блоке;
  * ВЫХОД DBot ищется по его подписям: транзакция, где тот же минт у него
    УМЕНЬШИЛСЯ. Результат считается как изменение нативного SOL, очищенное
    от комиссии -- тем же правилом, каким считается наш выход;
  * толпа между источником и нами -- число ЧУЖИХ покупок того же минта между
    индексом источника и нашим (bloom_block_position).

Чего здесь СПЕЦИАЛЬНО нет: пересчёта результатов на одинаковый размер входа.
Мы входим на 0.05 SOL, DBot на 0.2 -- проценты сравнивать можно, абсолютные
числа нельзя, и в таблице это помечено словами, а не оставлено на догадку.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_block_position as BP  # noqa: E402
import bloom_detector as BD  # noqa: E402
import bloom_exec_state as ST  # noqa: E402

ЛАМПОРТ = 10 ** 9


def кошельки_dbot(конфиг: Path | None = None) -> dict:
    """Кошельки задач DBot из снимка конфига: задача -> адрес."""
    if конфиг is None:
        return dict(BP.КОШЕЛЬКИ_DBOT_ПО_УМОЛЧАНИЮ)
    try:
        сырое = json.loads(Path(конфиг).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(BP.КОШЕЛЬКИ_DBOT_ПО_УМОЛЧАНИЮ)
    out = {}

    def обойти(o):
        if isinstance(o, dict):
            имя, адрес = o.get("name"), o.get("walletAddress")
            if имя and адрес:
                out[имя] = адрес
            for v in o.values():
                обойти(v)
        elif isinstance(o, list):
            for v in o:
                обойти(v)

    обойти(сырое)
    return out or dict(BP.КОШЕЛЬКИ_DBOT_ПО_УМОЛЧАНИЮ)


def sol_итог(tx: dict, кошелёк: str) -> dict:
    """Изменение нативного SOL у кошелька, очищенное от комиссии."""
    б = BD.балансы_кошелька(tx, кошелёк)
    return {"sol_delta": round(б.get("native_delta_sol") or 0.0, 9),
             "is_fee_payer": б.get("is_fee_payer")}


def минт_вырос(tx: dict, кошелёк: str, минт: str) -> bool:
    б = BD.балансы_кошелька(tx, кошелёк)
    з = (б.get("by_mint") or {}).get(минт) or {}
    return (з.get("delta_raw") or 0) > 0


def минт_упал(tx: dict, кошелёк: str, минт: str) -> bool:
    б = BD.балансы_кошелька(tx, кошелёк)
    з = (б.get("by_mint") or {}).get(минт) or {}
    return (з.get("delta_raw") or 0) < 0


def выход_dbot(helius, кошелёк: str, минт: str, *, после_слота: int,
                предел: int = 60) -> dict:
    """Продажа этого минта кошельком DBot: подпись, слот, вернувшийся SOL."""
    try:
        подписи = helius.call("getSignaturesForAddress",
                               [кошелёк, {"limit": предел}]) or []
    except Exception as exc:  # noqa: BLE001
        return {"known": False, "why_not": f"getSignaturesForAddress: {type(exc).__name__}"}
    кандидаты = [z for z in подписи
                  if isinstance(z, dict) and (z.get("slot") or 0) >= после_слота
                  and not z.get("err")]
    кандидаты.sort(key=lambda z: z.get("slot") or 0)
    for z in кандидаты:
        tx = helius.транзакция(z.get("signature"))
        if not tx:
            continue
        if минт_упал(tx, кошелёк, минт):
            и = sol_итог(tx, кошелёк)
            return {"known": True, "signature": z.get("signature"),
                     "slot": tx.get("slot"), "block_time": tx.get("blockTime"),
                     "sol_back": и["sol_delta"]}
    return {"known": False, "why_not": "продажи этого минта у кошелька DBot не нашлось",
             "checked": len(кандидаты)}


def пара(helius, поз: dict, *, кошелёк_dbot: str | None,
          наш_кошелёк: str) -> dict:
    """Одна пара А/Б: наши числа, числа DBot, место в блоке и толпа."""
    минт = поз.get("mint")
    подписи = поз.get("signatures") or []
    итог = {
        "mint": минт,
        "source_task": поз.get("source_task"),
        "source_slot": поз.get("source_slot"),
        "source_signature": поз.get("source_sig"),
        "our": {"signature": (подписи[0] if подписи else None),
                 "slot": поз.get("our_slot"),
                 "sol_in": поз.get("sol_in"),
                 "sol_back": (поз.get("closed_sol_delta")
                               if поз.get("closed_sol_delta") is not None
                               else (поз.get("last_sell_outcome") or {}).get("sol_delta")),
                 "closed_via": поз.get("closed_via")},
    }
    вх = итог["our"]["sol_in"] or 0
    из_ = итог["our"]["sol_back"]
    итог["our"]["result_sol"] = (round(из_ - вх, 9) if из_ is not None else None)
    итог["our"]["result_pct"] = (round((из_ - вх) / вх * 100, 2)
                                  if (из_ is not None and вх) else None)

    место = BP.место_относительно_источника(
        helius, минт=минт, слот_источника=поз.get("source_slot"),
        подпись_источника=поз.get("source_sig"),
        слот_наш=поз.get("our_slot"),
        подпись_наша=(подписи[0] if подписи else None),
        кошелёк_наш=наш_кошелёк, кошелёк_dbot=кошелёк_dbot)
    итог["place"] = {
        "slot_delta": место.get("slot_delta"),
        "our_index": место.get("our_index"),
        "source_index": место.get("source_index"),
        "index_delta_same_block": место.get("index_delta_same_block"),
        "crowd_between": (место.get("crowd_between") or {}).get("count"),
        "dbot": место.get("dbot"),
        "why_not": место.get("source_why_not") or место.get("our_why_not"),
    }

    if кошелёк_dbot:
        вход_d = None
        for имя in ("source_block", "our_block"):
            з = ((место.get("dbot") or {}).get(имя) or {})
            if з.get("known"):
                вход_d = {"slot": (поз.get("source_slot") if имя == "source_block"
                                    else поз.get("our_slot")),
                           "index": з.get("index"), "total": з.get("total"),
                           "signature": з.get("signature")}
                break
        итог["dbot"] = {"wallet": кошелёк_dbot, "entry": вход_d}
        if вход_d and вход_d.get("signature"):
            tx_вх = helius.транзакция(вход_d["signature"])
            if tx_вх:
                итог["dbot"]["sol_in"] = -sol_итог(tx_вх, кошелёк_dbot)["sol_delta"]
            вых = выход_dbot(helius, кошелёк_dbot, минт,
                              после_слота=int(вход_d["slot"] or 0))
            итог["dbot"]["exit"] = вых
            if вых.get("known") and итог["dbot"].get("sol_in"):
                вд = итог["dbot"]["sol_in"]
                итог["dbot"]["result_sol"] = round(вых["sol_back"] - вд, 9)
                итог["dbot"]["result_pct"] = round(
                    (вых["sol_back"] - вд) / вд * 100, 2) if вд else None
        итог["note"] = ("проценты сравнимы, абсолютные числа нет: у нас вход "
                         f"{итог['our']['sol_in']} SOL, у DBot свой размер")
    return итог


# ------------------------------------------------------------- самопроверка

def self_test() -> None:
    всего = [0, 0]

    def chk(имя, условие, факт=None):
        всего[0] += 1
        if условие:
            всего[1] += 1
            print(f"  [ok  ] {имя}")
        else:
            print(f"  [ПЛОХО] {имя} -- {факт}")

    МИНТ, МЫ, DBOT, ИСТ = "МИНТ", "МЫ", "DBOTW", "SRC"

    def бал(owner, raw, idx):
        return {"accountIndex": idx, "mint": МИНТ, "owner": owner,
                "programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                "uiTokenAmount": {"amount": str(raw), "decimals": 6,
                                   "uiAmount": raw / 1e6}}

    def tx(подпись, *, кто, было, стало, sol_до, sol_после, slot=100, fee=5000):
        return {"slot": slot, "blockTime": 1790000000,
                "transaction": {"signatures": [подпись],
                                 "message": {"accountKeys": [{"pubkey": кто}],
                                              "instructions": []}},
                "meta": {"err": None, "fee": fee,
                          "preBalances": [sol_до], "postBalances": [sol_после],
                          "preTokenBalances": [бал(кто, было, 1)],
                          "postTokenBalances": [бал(кто, стало, 1)],
                          "innerInstructions": []}}

    покупка_dbot = tx("DBOT_BUY", кто=DBOT, было=0, стало=1000,
                       sol_до=int(0.3 * ЛАМПОРТ), sol_после=int(0.1 * ЛАМПОРТ))
    продажа_dbot = tx("DBOT_SELL", кто=DBOT, было=1000, стало=0,
                       sol_до=int(0.1 * ЛАМПОРТ), sol_после=int(0.25 * ЛАМПОРТ),
                       slot=140)

    class Helius:
        def __init__(self):
            self.вызовы = []

        def call(self, метод, параметры):
            self.вызовы.append(метод)
            if метод == "getSignaturesForAddress":
                return [{"signature": "DBOT_SELL", "slot": 140, "err": None},
                        {"signature": "ЧУЖОЕ", "slot": 141, "err": None}]
            if метод == "getBlock":
                слот = параметры[0]
                if слот == 100:
                    return {"transactions": [
                        tx("ИСТОЧНИК", кто=ИСТ, было=0, стало=500,
                            sol_до=10 ** 9, sol_после=10 ** 9 - 1),
                        purchase := purchase_stub(),
                        покупка_dbot,
                    ]}
                if слот == 101:
                    return {"transactions": [tx("НАША", кто=МЫ, было=0, стало=200,
                                                 sol_до=10 ** 9, sol_после=10 ** 9 - 1,
                                                 slot=101)]}
            raise RuntimeError("нет данных")

        def транзакция(self, подпись, **kw):
            return {"DBOT_BUY": покупка_dbot, "DBOT_SELL": продажа_dbot,
                     "ЧУЖОЕ": tx("ЧУЖОЕ", кто="КТОТО", было=0, стало=5,
                                  sol_до=10 ** 9, sol_после=10 ** 9, slot=141)}.get(подпись)

    def purchase_stub():
        return tx("ЧУЖАЯ_ПОКУПКА", кто="ЧУЖОЙ", было=0, стало=7,
                   sol_до=10 ** 9, sol_после=10 ** 9 - 2)

    поз = {"mint": МИНТ, "source_task": "BATCH-3", "source_slot": 100,
            "source_sig": "ИСТОЧНИК", "our_slot": 101, "signatures": ["НАША"],
            "sol_in": 0.05, "closed_sol_delta": 0.0432, "closed_via": "авто-ордер Bloom"}
    h = Helius()
    р = пара(h, поз, кошелёк_dbot=DBOT, наш_кошелёк=МЫ)

    chk("наш результат посчитан", р["our"]["result_sol"] == round(0.0432 - 0.05, 9),
        р["our"])
    chk("и в процентах", abs(р["our"]["result_pct"] + 13.6) < 0.1, р["our"]["result_pct"])
    chk("вход DBot найден в блоке источника",
        (р.get("dbot") or {}).get("entry", {}).get("signature") == "DBOT_BUY", р.get("dbot"))
    # 0.2 SOL ушло, комиссия 5000 лампортов возвращена в расчёт (её платит
    # тот же кошелёк, и тратой на вход она не является) -> 0.199995.
    chk("вход DBot в SOL посчитан по цепи и очищен от комиссии",
        abs((р["dbot"].get("sol_in") or 0) - 0.199995) < 1e-6, р["dbot"].get("sol_in"))
    chk("выход DBot найден по его подписям",
        р["dbot"]["exit"]["known"] and р["dbot"]["exit"]["signature"] == "DBOT_SELL",
        р["dbot"]["exit"])
    chk("результат DBot посчитан",
        р["dbot"].get("result_sol") is not None and р["dbot"]["result_pct"] is not None,
        р["dbot"])
    chk("оговорка про разные размеры входа есть",
        "абсолютные числа нет" in (р.get("note") or ""), р.get("note"))
    chk("толпа между источником и нами посчитана",
        р["place"]["crowd_between"] == 1, р["place"])

    # Кошелёк DBot не задан -- блок dbot отсутствует, но пара всё равно строится
    р2 = пара(Helius(), поз, кошелёк_dbot=None, наш_кошелёк=МЫ)
    chk("без кошелька DBot пара строится без его блока",
        "dbot" not in р2 and р2["our"]["result_sol"] is not None, list(р2))

    # Выход DBot не нашёлся -- сказано, а не посчитано нулём
    class HeliusБезПродажи(Helius):
        def call(self, метод, параметры):
            if метод == "getSignaturesForAddress":
                return []
            return super().call(метод, параметры)

    р3 = пара(HeliusБезПродажи(), поз, кошелёк_dbot=DBOT, наш_кошелёк=МЫ)
    chk("выход DBot не найден -- причина названа, результата нет",
        р3["dbot"]["exit"]["known"] is False and "result_sol" not in р3["dbot"],
        р3["dbot"])

    # Кошельки задач читаются из снимка конфига
    к = кошельки_dbot(Path("data/final/20260923T145755Z/konfig.json"))
    chk("кошельки задач взяты из снимка конфига",
        к.get("BATCH-3") == "BmjAUDbwBMxR5shrmzBtKRwveVahFGFiEH3oTq7QTHnu"
        and к.get("BATCH-5") == "5Y8h877swoTzTdc8in9hU3SvXXVv1q9p19Y85tAsdqBv",
        {k: v for k, v in к.items() if k in ("BATCH-3", "BATCH-5")})

    print(f"самопроверка пар А/Б: {всего[1]}/{всего[0]}"
          f"{' пройдено' if всего[1] == всего[0] else ' ПРОВАЛ'}")
    if всего[1] != всего[0]:
        raise SystemExit(1)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--state-dir", default=None)
    p.add_argument("--config", default="data/final/20260923T145755Z/konfig.json")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--out", default="data/bloom_ab_pairs.json")
    a = p.parse_args()
    if a.self_test:
        self_test()
        return 0

    state = ST.ExecState(base=Path(a.state_dir)) if a.state_dir else ST.ExecState()
    helius = BD.Helius(служба="bloom_ab_pairs")
    кош = кошельки_dbot(Path(a.config) if a.config else None)
    позиции = [p2 for p2 in state.positions().values()
                if ST.is_real_mode(p2.get("mode"))]
    позиции.sort(key=lambda p2: float(p2.get("ts_intent") or 0))
    вых = []
    for поз in позиции[-a.limit:]:
        задача = поз.get("source_task")
        вых.append(пара(helius, поз, кошелёк_dbot=кош.get(задача or ""),
                         наш_кошелёк=поз.get("wallet") or ST.EXECUTOR_WALLET))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    ST.atomic_write_json(Path(a.out), {
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pairs": вых,
        "note": ("проценты сравнимы, абсолютные числа нет: размеры входа разные. "
                  "Пары ведутся нарастающим итогом с 24.09")})
    print(json.dumps(вых, ensure_ascii=False, indent=1)[:6000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
