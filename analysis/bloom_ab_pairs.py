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
import bloom_price_curve as PC  # noqa: E402

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


def задача_по_подписи(state: ST.ExecState, подпись: str) -> str | None:
    """Задача источника по подписи его транзакции -- из журнала решений.

    В записи позиции задачи нет (исполнитель её не пишет), и без неё кошелёк
    DBot не выбрать: по первому замеру все пять пар вышли без чисел DBot
    именно поэтому. Журнал решений её несёт -- source_task.
    """
    if not подпись:
        return None
    try:
        текст = state.decisions_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in текст.splitlines():
        if подпись not in line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("signature") == подпись and r.get("source_task"):
            return r["source_task"]
    return None


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


def не_нашли_dbot(p: dict) -> bool:
    вх = ((p.get("dbot") or {}).get("entry") or {})
    return not вх.get("signature")


def уровни(helius, *, минт: str, подпись_входа: str | None,
            подпись_выхода: str | None, кошелёк: str | None,
            нетто_вход: float | None = None,
            нетто_выход: float | None = None) -> dict:
    """Три уровня результата одной стороны, чтобы сравнение было честным.

    Зачем три, а не один. Наши комиссии на входе 0.05 SOL съедают около
    десятой части круга, у DBot на 0.2 SOL -- около двадцатой. Сравнивать
    только итог кошелька значит сравнивать размеры входа, а не сигнал.

      * "сигнал" -- курс ПУЛА на входе и на выходе: сколько котировки за
        токен. Считается по счетам той инструкции DEX, где стоит хранилище
        минта, поэтому в него не входят ни priority, ни tip, ни комиссия
        площадки, ни рента новых счетов. Это чистая цена события;
      * "цепь" -- сколько котировки реально ушло в пул на входе и пришло из
        пула на выходе. Отличие от сигнала -- проскальзывание и разница
        купленного и проданного количества;
      * "после комиссий площадки" -- изменение нативного SOL кошелька, то
        есть то, что действительно осталось. Отличие от цепи -- надбавки.

    Проценты сравнимы только внутри ОДНОЙ котировочной стороны, поэтому
    сторона возвращается именем, а при её смене между входом и выходом
    уровень честно отказывается считаться.
    """
    из_ = {"signal": {"known": False, "why_not": "подписи входа или выхода нет"},
            "chain": {"known": False, "why_not": "подписи входа или выхода нет"},
            "net": {"known": нетто_вход is not None and нетто_выход is not None}}
    if нетто_вход and нетто_выход is not None:
        из_["net"].update(sol_in=нетто_вход, sol_back=нетто_выход,
                           result_sol=round(нетто_выход - нетто_вход, 9),
                           result_pct=round((нетто_выход - нетто_вход)
                                             / нетто_вход * 100, 2))
    if not подпись_входа or not подпись_выхода:
        return из_
    tx_вх = helius.транзакция(подпись_входа)
    tx_вых = helius.транзакция(подпись_выхода)
    if not tx_вх or not tx_вых:
        нет = "узел не отдал транзакцию входа" if not tx_вх else "узел не отдал транзакцию выхода"
        из_["signal"] = {"known": False, "why_not": нет}
        из_["chain"] = {"known": False, "why_not": нет}
        return из_
    к_вх = PC.курс_по_пулу(tx_вх, минт, кошелёк=кошелёк)
    к_вых = PC.курс_по_пулу(tx_вых, минт, кошелёк=кошелёк)
    if not к_вх.get("known") or not к_вых.get("known"):
        нет = (к_вх.get("why_not") if not к_вх.get("known")
                else к_вых.get("why_not"))
        из_["signal"] = {"known": False, "why_not": нет}
        из_["chain"] = {"known": False, "why_not": нет}
        return из_
    if к_вх.get("quote") != к_вых.get("quote"):
        нет = (f"вход в {к_вх.get('quote')}, выход в {к_вых.get('quote')} -- "
                "это разные величины, процент между ними не считается")
        из_["signal"] = {"known": False, "why_not": нет}
        из_["chain"] = {"known": False, "why_not": нет}
        return из_
    из_["signal"] = {"known": True, "quote": к_вх.get("quote"),
                      "entry_rate": к_вх["rate"], "exit_rate": к_вых["rate"],
                      "result_pct": round((к_вых["rate"] - к_вх["rate"])
                                           / к_вх["rate"] * 100, 2)}
    вошло, вышло = к_вх.get("quote_ui"), к_вых.get("quote_ui")
    из_["chain"] = {"known": bool(вошло), "quote": к_вх.get("quote"),
                     "quote_in": вошло, "quote_out": вышло,
                     "token_in": к_вх.get("token_ui"),
                     "token_out": к_вых.get("token_ui"),
                     "result_pct": (round((вышло - вошло) / вошло * 100, 2)
                                     if вошло else None)}
    # Раскладка издержек между уровнями -- в процентных пунктах. Именно она
    # отвечает на вопрос "сколько съела площадка", а не сигнал.
    сигнал = из_["signal"].get("result_pct")
    цепь = из_["chain"].get("result_pct")
    нетто = из_["net"].get("result_pct")
    из_["costs_pp"] = {
        "slippage": (round(цепь - сигнал, 2)
                      if сигнал is not None and цепь is not None else None),
        "platform_and_fees": (round(нетто - цепь, 2)
                               if нетто is not None and цепь is not None else None),
        "total": (round(нетто - сигнал, 2)
                   if нетто is not None and сигнал is not None else None)}
    return из_


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

    # Три уровня по НАШЕЙ стороне. Подпись выхода -- та, которой позиция
    # закрыта (авто-ордер Bloom); без неё уровни сигнала и цепи честно пустые.
    итог["our"]["levels"] = уровни(
        helius, минт=минт,
        подпись_входа=(подписи[0] if подписи else None),
        подпись_выхода=(поз.get("closed_signature")
                         or (поз.get("last_sell_outcome") or {}).get("signature")),
        кошелёк=наш_кошелёк, нетто_вход=вх, нетто_выход=из_)

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
        if вход_d is None and поз.get("source_slot"):
            # DBot мог сесть НЕ в тот блок, где мы, и не в блок источника: на
            # четырёх парах из пяти его вход так и не нашёлся, пока смотрели
            # только два блока. Ищем по окну от блока источника и дальше.
            с = int(поз["source_slot"])
            до = max(int(поз.get("our_slot") or с), с) + 2
            окно = list(range(с, до + 1))
            п = BP.покупатели_минта(helius, минт, окно,
                                     известные={кошелёк_dbot: "DBot"})
            свои = [r for r in (п.get("rows") or [])
                     if r.get("owner") == кошелёк_dbot]
            if свои:
                свои.sort(key=lambda r: (r.get("slot") or 0, r.get("index") or 0))
                r0 = свои[0]
                вход_d = {"slot": r0.get("slot"), "index": r0.get("index"),
                           "total": r0.get("total"), "signature": r0.get("signature"),
                           "found_by": f"поиск по окну {окно[0]}-{окно[-1]}"}
        итог["dbot"] = {"wallet": кошелёк_dbot, "entry": вход_d}
        if вход_d and вход_d.get("signature"):
            tx_вх = helius.транзакция(вход_d["signature"])
            if tx_вх:
                итог["dbot"]["sol_in"] = -sol_итог(tx_вх, кошелёк_dbot)["sol_delta"]
            вых = выход_dbot(helius, кошелёк_dbot, минт,
                              после_слота=int(вход_d["slot"] or 0))
            итог["dbot"]["exit"] = вых
            if вых.get("known"):
                итог["dbot"]["levels"] = уровни(
                    helius, минт=минт, подпись_входа=вход_d.get("signature"),
                    подпись_выхода=вых.get("signature"), кошелёк=кошелёк_dbot,
                    нетто_вход=итог["dbot"].get("sol_in"),
                    нетто_выход=вых.get("sol_back"))
            if вых.get("known") and итог["dbot"].get("sol_in"):
                вд = итог["dbot"]["sol_in"]
                итог["dbot"]["result_sol"] = round(вых["sol_back"] - вд, 9)
                итог["dbot"]["result_pct"] = round(
                    (вых["sol_back"] - вд) / вд * 100, 2) if вд else None
        итог["note"] = (
            "проценты сравнимы, абсолютные числа нет: у нас вход "
            f"{итог['our']['sol_in']} SOL, у DBot свой размер. Сравнивать "
            "стороны следует на уровне signal: там нет ни priority, ни tip, "
            "ни комиссии площадки, ни ренты, поэтому разница размеров входа "
            "(у нас комиссии съедают около десятой части круга, у DBot около "
            "двадцатой) в него не попадает")
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

    # Вход DBot в ТРЕТЬЕМ блоке: ни блок источника, ни наш его не содержат.
    class HeliusDBotПозже(Helius):
        def call(self, метод, параметры):
            if метод == "getBlock" and параметры[0] == 102:
                return {"transactions": [покупка_dbot]}
            if метод == "getBlock" and параметры[0] in (100, 101):
                if параметры[0] == 100:
                    return {"transactions": [
                        tx("ИСТОЧНИК", кто=ИСТ, было=0, стало=500,
                            sol_до=10 ** 9, sol_после=10 ** 9 - 1)]}
                return {"transactions": [tx("НАША", кто=МЫ, было=0, стало=200,
                                             sol_до=10 ** 9, sol_после=10 ** 9 - 1,
                                             slot=101)]}
            return super().call(метод, параметры)

    р_п = пара(HeliusDBotПозже(), поз, кошелёк_dbot=DBOT, наш_кошелёк=МЫ)
    chk("вход DBot найден поиском по окну, а не потерян",
        (р_п.get("dbot") or {}).get("entry", {}).get("signature") == "DBOT_BUY"
        and "поиск по окну" in ((р_п["dbot"]["entry"] or {}).get("found_by") or ""),
        р_п.get("dbot"))

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

    # --- три уровня: сигнал / цепь / после комиссий площадки ---
    RAY_П = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
    СТЕЙБЛ = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    def своп(*, токенов, котировки, кв=BD.WSOL):
        """Свап в одном пуле: хранилища стоят в счетах инструкции DEX."""
        def б(минт, raw, idx, dec):
            return {"accountIndex": idx, "mint": минт, "owner": "ПУЛ",
                     "uiTokenAmount": {"amount": str(raw), "decimals": dec,
                                        "uiAmount": raw / 10 ** dec}}
        ключи = [{"pubkey": МЫ, "signer": True}, {"pubkey": "ХРАН-МИНТ"},
                  {"pubkey": "ХРАН-КВ"}]
        return {"slot": 100, "blockTime": 1790000000,
                 "transaction": {"signatures": ["S"], "message": {
                     "accountKeys": ключи,
                     "instructions": [{"programId": RAY_П,
                                        "accounts": ["ХРАН-МИНТ", "ХРАН-КВ"]}]}},
                 "meta": {"err": None, "fee": 5000,
                           "preBalances": [10 ** 9, 0, 0],
                           "postBalances": [10 ** 9, 0, 0],
                           "preTokenBalances": [б(МИНТ, int(токенов * 1e6), 1, 6),
                                                 б(кв, 0, 2, 9)],
                           "postTokenBalances": [б(МИНТ, 0, 1, 6),
                                                  б(кв, int(котировки * 1e9), 2, 9)]}}

    class HeliusУровни:
        def __init__(self, по_подписи):
            self.по_подписи = по_подписи

        def транзакция(self, подпись, **kw):
            return self.по_подписи.get(подпись)

    h_ур = HeliusУровни({"ВХОД": своп(токенов=1000, котировки=2.0),
                          "ВЫХОД": своп(токенов=1000, котировки=2.4)})
    у = уровни(h_ур, минт=МИНТ, подпись_входа="ВХОД", подпись_выхода="ВЫХОД",
                кошелёк=МЫ, нетто_вход=2.1, нетто_выход=2.35)
    chk("уровень сигнала: курс пула на входе и на выходе",
        у["signal"]["known"] and abs(у["signal"]["entry_rate"] - 0.002) < 1e-12
        and abs(у["signal"]["result_pct"] - 20.0) < 0.01, у["signal"])
    chk("уровень цепи: сколько котировки ушло в пул и пришло из пула",
        у["chain"]["known"] and abs(у["chain"]["quote_in"] - 2.0) < 1e-9
        and abs(у["chain"]["quote_out"] - 2.4) < 1e-9, у["chain"])
    chk("уровень после комиссий площадки -- итог кошелька",
        у["net"]["known"] and abs(у["net"]["result_pct"] - 11.9) < 0.01, у["net"])
    chk("издержки разложены в процентных пунктах, а не свалены в одно число",
        abs(у["costs_pp"]["slippage"]) < 0.01
        and abs(у["costs_pp"]["platform_and_fees"] + 8.1) < 0.01
        and abs(у["costs_pp"]["total"] + 8.1) < 0.01, у["costs_pp"])

    # Смена котировочной стороны между входом и выходом -- разные величины.
    h_см = HeliusУровни({"ВХОД": своп(токенов=1000, котировки=2.0),
                          "ВЫХОД": своп(токенов=1000, котировки=400.0, кв=СТЕЙБЛ)})
    у_см = уровни(h_см, минт=МИНТ, подпись_входа="ВХОД", подпись_выхода="ВЫХОД",
                   кошелёк=МЫ, нетто_вход=2.1, нетто_выход=2.35)
    chk("вход в SOL, выход в стейбле -- процент НЕ считается, сказано почему",
        у_см["signal"]["known"] is False
        and "разные величины" in у_см["signal"]["why_not"], у_см["signal"])
    chk("а итог кошелька при этом всё равно посчитан",
        у_см["net"]["known"] and у_см["net"]["result_pct"] is not None, у_см["net"])

    у_нет = уровни(HeliusУровни({}), минт=МИНТ, подпись_входа=None,
                    подпись_выхода=None, кошелёк=МЫ)
    chk("без подписей уровни честно пустые, а не нулевые",
        у_нет["signal"]["known"] is False and у_нет["net"]["known"] is False,
        у_нет)

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
        задача = поз.get("source_task") or задача_по_подписи(state, поз.get("source_sig"))
        кошелёк = кош.get(задача or "")
        если_нет = None
        if not кошелёк:
            # Задача не нашлась -- пробуем оба кошелька задач: лучше проверить
            # два адреса, чем отдать таблицу без чисел DBot.
            если_нет = f"задача источника не определена, пробуем все кошельки: {sorted(кош)}"
        p2 = пара(helius, {**поз, "source_task": задача},
                   кошелёк_dbot=кошелёк, наш_кошелёк=(поз.get("wallet")
                                                        or ST.EXECUTOR_WALLET))
        if не_нашли_dbot(p2) and not кошелёк:
            for имя, адрес in кош.items():
                if имя not in ("BATCH-3", "BATCH-5"):
                    continue
                проба = пара(helius, {**поз, "source_task": имя},
                              кошелёк_dbot=адрес,
                              наш_кошелёк=(поз.get("wallet") or ST.EXECUTOR_WALLET))
                if not не_нашли_dbot(проба):
                    p2 = проба
                    p2["dbot_wallet_guessed"] = имя
                    break
        if если_нет:
            p2["task_why_not"] = если_нет
        вых.append(p2)
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
