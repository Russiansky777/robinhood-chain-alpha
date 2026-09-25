#!/usr/bin/env python3
"""Что происходило в пуле между НАШЕЙ покупкой и продажей. Только чтение.

Вопрос владельца 25.09 по сделке 571aBZbC (−61 % за 28.8 с): что случилось
по цепи между нашей покупкой и продажей -- продал ли источник, сняли ли
ликвидность, были ли чужие крупные продажи. Ответ должен быть таблицей
транзакций, а не рассуждением.

Как считается:

* Подписи берутся у САМОГО ПУЛА (getSignaturesForAddress) с `until` =
  подпись покупки источника: так в выборку попадает только то, что было
  ПОЗЖЕ неё, и только то, что трогало этот пул. Обход по кошельку сюда не
  годится -- он не покажет чужих.
* Каждая подпись раскрывается одним getTransaction. Направление сделки
  определяется знаком дельты минта у подписанта: минус -- продажа, плюс --
  покупка. Сколько SOL ушло или пришло -- по лампортам подписанта
  (c2_common.quote_spend со знаком).
* Остаток пула после каждой транзакции -- из postTokenBalances по владельцу
  пула: видно и слив ликвидности, и обычную торговлю.
* Кто это был -- по списку известных адресов (мы, источник, сборщик Bloom,
  чаевые Sender). Незнакомый адрес так и называется незнакомым; догадок
  про "снайпера" здесь нет.

Цена вызова: 1 кредит на страницу подписей + 1 на транзакцию. Предел задаётся
`--max-tx`, и при его достижении отчёт честно помечается partial.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C2  # noqa: E402

WSOL = "So11111111111111111111111111111111111111112"
# Сборщик платы Bloom -- подтверждён по цепи 25.09 (1 % траты + processor_tip).
BLOOM_FEE = "B1dozJAUae1MfexMMoCzrLtTd4JKfJTHLrRfUQswQG5m"


def r6(x):
    return None if x is None else round(float(x), 6)


def сторона(tx: dict, минт: str) -> dict:
    """Кто и в какую сторону торговал в этой транзакции.

    Берётся дельта минта у КАЖДОГО владельца, и выбирается наибольшая по
    модулю среди не-пулов: пул всегда меняется в противоход клиенту, и если
    брать первого попавшегося, направление будет перевёрнутым.
    """
    дельты = C2.owner_mint_delta(tx or {}, минт) or {}
    подписанты = C2.signers(tx or {}) or set()
    лучший, лучшее = None, None
    for кто, д in дельты.items():
        if кто not in подписанты:
            continue
        if лучшее is None or abs(float(д)) > abs(float(лучшее)):
            лучший, лучшее = кто, д
    if лучший is None:
        # Подписант не менял минт (например, снятие ликвидности ведёт себя
        # иначе): берём наибольшую дельту вообще, но говорим, что подписанта
        # среди владельцев нет.
        for кто, д in дельты.items():
            if лучшее is None or abs(float(д)) > abs(float(лучшее)):
                лучший, лучшее = кто, д
        return {"who": лучший, "delta": лучшее, "signer_matched": False}
    return {"who": лучший, "delta": лучшее, "signer_matched": True}


def остаток_пула(tx: dict, пул: str, минт: str) -> dict:
    """Сколько минта и котировки осталось у пула ПОСЛЕ транзакции."""
    из_ = {"mint_after": None, "wsol_after": None}
    for б in ((tx or {}).get("meta") or {}).get("postTokenBalances") or []:
        if б.get("owner") != пул:
            continue
        сумма = ((б.get("uiTokenAmount") or {}).get("uiAmount"))
        if б.get("mint") == минт:
            из_["mint_after"] = сумма
        elif б.get("mint") == WSOL:
            из_["wsol_after"] = сумма
    return из_


def имя(адрес: str | None, известные: dict) -> str:
    if not адрес:
        return "—"
    return известные.get(адрес) or "незнакомый"


def разобрать(rpc_call, *, минт: str, пул: str, подпись_источника: str,
               слот_источника: int, известные: dict | None = None,
               окно_слотов: int = 120, max_tx: int = 60,
               page_limit: int = 1000, max_pages: int = 40) -> dict:
    """Таблица событий пула после покупки источника.

    Без rpc_call -- честный отказ: пустая таблица неотличима от "ничего не
    происходило", а это ровно то, что мы и проверяем.
    """
    if rpc_call is None:
        return {"ok": False, "why_not": "нет rpc_call -- цепь не читалась", "rows": []}
    известные = dict(известные or {})
    известные.setdefault(BLOOM_FEE, "сбор Bloom")
    # ПОСТРАНИЧНО НАЗАД. API отдаёт подписи ТОЛЬКО от новых к старым и только
    # с курсором `before`; `until` лишь говорит, где остановиться. Если у пула
    # с момента нашей покупки прошло больше, чем помещается на страницу, одна
    # страница вернёт самые свежие -- и наше окно в неё не попадёт вовсе.
    # Ровно это и случилось на первом прогоне: 0 подписей в окне при живом пуле.
    страница: list = []
    видели: set = set()
    before = None
    страниц = 0
    for _ in range(max_pages):
        try:
            кусок = rpc_call("getSignaturesForAddress",
                              [пул, {"limit": page_limit, "until": подпись_источника,
                                      **({"before": before} if before else {}),
                                      "commitment": "finalized"}]) or []
        except Exception as exc:  # noqa: BLE001
            return {"ok": False,
                     "why_not": f"подписи пула не получены: {type(exc).__name__}: {exc}",
                     "rows": []}
        страниц += 1
        if not кусок:
            break
        # ДЕДУПЛИКАЦИЯ И ЗАЩИТА ОТ ТОПТАНИЯ НА МЕСТЕ: если узел вернул ту же
        # страницу (курсор не сдвинулся), дальше идти незачем -- иначе одна и
        # та же сделка попадёт в таблицу столько раз, сколько было страниц.
        новых = [с for с in кусок if с.get("signature") not in видели]
        for с in новых:
            видели.add(с.get("signature"))
        страница.extend(новых)
        следующий = кусок[-1].get("signature")
        if not новых or следующий == before:
            break
        before = следующий
        самый_старый = кусок[-1].get("slot")
        # Дошли до окна -- дальше назад идти незачем.
        if isinstance(самый_старый, int) and самый_старый <= слот_источника:
            break
    кандидаты = sorted((с for с in страница if с.get("signature")),
                        key=lambda x: x.get("slot") or 0)
    в_окне = [с for с in кандидаты
              if isinstance(с.get("slot"), int)
              and с["slot"] <= слот_источника + окно_слотов]
    # ПУЛ МОЖЕТ БЫТЬ ОГНЕМЁТОМ. У 571aBZbC в окне сотни транзакций, и читать
    # их все -- сотни кредитов. Берём КРАЯ окна: начало (сразу после нашей
    # покупки, там и происходит обвал) и конец (перед продажей). Пропущенная
    # середина названа числом, а не спрятана.
    пропущено = 0
    if len(в_окне) > max_tx:
        половина = max(1, max_tx // 2)
        пропущено = len(в_окне) - 2 * половина
        в_окне = в_окне[:половина] + в_окне[-половина:]
    строки = []
    вызовов = 0
    оборван = None
    for с in в_окне:
        if вызовов >= max_tx:
            оборван = f"предел --max-tx={max_tx}"
            break
        try:
            tx = rpc_call("getTransaction",
                           [с["signature"], {"encoding": "jsonParsed",
                                              "maxSupportedTransactionVersion": 1,
                                              "commitment": "finalized"}])
            вызовов += 1
        except Exception as exc:  # noqa: BLE001
            оборван = f"{type(exc).__name__}: {exc}"
            break
        if not tx:
            continue
        ст = сторона(tx, минт)
        кто = ст.get("who")
        квота = C2.quote_spend(tx, кто) if кто else {}
        # quote_spend отдаёт ПОТРАЧЕНО (минус лампортов). На продаже знак
        # обратный, поэтому пишем обе величины явно, без "минус-минус".
        sol = квота.get("sol")
        строки.append({
            "signature": с["signature"],
            "slot": с.get("slot"),
            "block_time": с.get("blockTime") or (tx or {}).get("blockTime"),
            "err": с.get("err") is not None,
            "who": кто,
            "who_name": имя(кто, известные),
            "signer_matched": ст.get("signer_matched"),
            "mint_delta": str(ст.get("delta")) if ст.get("delta") is not None else None,
            "side": ("продажа" if (ст.get("delta") is not None
                                    and float(ст["delta"]) < 0)
                      else "покупка" if ст.get("delta") is not None else "—"),
            "sol_spent": r6(sol) if sol is not None else None,
            **остаток_пула(tx, пул, минт),
        })
    из_ = {"ok": True, "pool": пул, "mint": минт,
            "source_signature": подпись_источника, "source_slot": слот_источника,
            # ВСЕГО подписей отдано узлом и сколько из них попало в окно -- две
            # разные величины: "ноль в окне" при сотнях отданных означает, что
            # окно не то, а не что в пуле было тихо.
            "n_signatures_total": len(кандидаты), "n_pages": страниц,
            "oldest_slot_seen": (кандидаты[0].get("slot") if кандидаты else None),
            "newest_slot_seen": (кандидаты[-1].get("slot") if кандидаты else None),
            "n_signatures_window": len(в_окне) + пропущено,
            "n_skipped_middle": пропущено,
            "n_getTransaction": вызовов,
            "partial": оборван is not None, "stopped_reason": оборван,
            "rows": строки}
    из_["summary"] = сводка(строки, известные)
    return из_


def сводка(строки: list, известные: dict) -> dict:
    """Числа, ради которых всё считалось: кто продал, сколько и когда."""
    продажи = [с for с in строки if с.get("side") == "продажа" and not с.get("err")]
    покупки = [с for с in строки if с.get("side") == "покупка" and not с.get("err")]
    остатки = [с.get("mint_after") for с in строки if с.get("mint_after") is not None]
    вода = [с.get("wsol_after") for с in строки if с.get("wsol_after") is not None]
    свои = [с for с in продажи if с.get("who_name") not in ("незнакомый", "—")]
    return {
        "n_rows": len(строки), "n_sells": len(продажи), "n_buys": len(покупки),
        "n_failed": sum(1 for с in строки if с.get("err")),
        "first_mint_after": остатки[0] if остатки else None,
        "last_mint_after": остатки[-1] if остатки else None,
        "first_wsol_after": вода[0] if вода else None,
        "last_wsol_after": вода[-1] if вода else None,
        "wsol_drop_pct": (round((вода[-1] - вода[0]) / вода[0] * 100.0, 2)
                           if вода and вода[0] else None),
        "named_sellers": sorted({с["who_name"] for с in свои}),
        "biggest_sell_sol": max((с["sol_spent"] for с in продажи
                                  if с.get("sol_spent") is not None), default=None),
    }


def в_таблицу(из_: dict) -> str:
    """Markdown-таблица для доклада. Прочерк там, где числа нет."""
    if not из_.get("ok"):
        return f"Разбор не сделан: {из_.get('why_not')}"
    шапка = ("| слот | кто | что | SOL | остаток минта в пуле | остаток WSOL | подпись |\n"
              "|---|---|---|---|---|---|---|")
    ряды = []
    for с in из_.get("rows") or []:
        ряды.append("| {slot} | {who_name} | {side}{err} | {sol} | {m} | {w} | `{sig}` |".format(
            slot=с.get("slot"), who_name=с.get("who_name"),
            side=с.get("side"), err=" (упала)" if с.get("err") else "",
            sol="—" if с.get("sol_spent") is None else с["sol_spent"],
            m="—" if с.get("mint_after") is None else с["mint_after"],
            w="—" if с.get("wsol_after") is None else с["wsol_after"],
            sig=(с.get("signature") or "")[:16]))
    return "\n".join([шапка] + ряды)


def продажи_кошелька(rpc_call, *, кошелёк: str, минт: str,
                      подпись_покупки: str, max_tx: int = 10) -> dict:
    """Продавал ли ЭТОТ кошелёк этот минт после своей покупки.

    Таблица по пулу отвечает "что было в пуле", но при 4881 транзакции в окне
    отдельного участника в выборке может не оказаться вовсе -- и молчание там
    ничего не значит. Здесь спрашивается адресно: токен-счёт кошелька по
    этому минту (из самой покупки, счёт может быть уже закрыт) и его подписи
    после покупки. 2-3 кредита на ответ.
    """
    if rpc_call is None:
        return {"ok": False, "why_not": "нет rpc_call"}
    from night_leader_stonkfun import хранилище_владельца  # noqa: PLC0415

    try:
        покупка = rpc_call("getTransaction",
                            [подпись_покупки, {"encoding": "jsonParsed",
                                                "maxSupportedTransactionVersion": 1,
                                                "commitment": "finalized"}])
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why_not": f"покупка не получена: {type(exc).__name__}"}
    хран = хранилище_владельца(покупка or {}, кошелёк, минт)
    if not хран:
        return {"ok": False, "why_not": "в покупке нет токен-счёта этого кошелька по минту"}
    try:
        подписи = rpc_call("getSignaturesForAddress",
                            [хран, {"limit": 100, "until": подпись_покупки,
                                     "commitment": "finalized"}]) or []
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why_not": f"подписи счёта не получены: {type(exc).__name__}"}
    строки = []
    for с in sorted(подписи, key=lambda x: x.get("slot") or 0)[:max_tx]:
        if с.get("err") is not None:
            continue
        try:
            tx = rpc_call("getTransaction",
                           [с["signature"], {"encoding": "jsonParsed",
                                              "maxSupportedTransactionVersion": 1,
                                              "commitment": "finalized"}])
        except Exception as exc:  # noqa: BLE001
            строки.append({"signature": с.get("signature"), "why_not": str(exc)[:80]})
            break
        if not tx:
            continue
        д = (C2.owner_mint_delta(tx, минт) or {}).get(кошелёк)
        квота = C2.quote_spend(tx, кошелёк) or {}
        строки.append({"signature": с.get("signature"), "slot": с.get("slot"),
                        "block_time": с.get("blockTime"),
                        "mint_delta": str(д) if д is not None else None,
                        "side": ("продажа" if (д is not None and float(д) < 0)
                                  else "покупка" if д is not None else "—"),
                        "sol_spent": r6(квота.get("sol"))})
    продажи = [с for с in строки if с.get("side") == "продажа"]
    return {"ok": True, "vault": хран, "n_signatures": len(подписи),
            "rows": строки, "n_sells": len(продажи),
            "first_sell_slot": (продажи[0].get("slot") if продажи else None)}


# ------------------------------------------------------------- самопроверка

def self_test() -> int:
    всего = [0, 0]

    def chk(имя_, условие, факт=None):
        всего[0] += 1
        if условие:
            всего[1] += 1
            print(f"  [ok  ] {имя_}")
        else:
            print(f"  [ПЛОХО] {имя_} -- {факт}")

    МИНТ, ПУЛ, ТРЕЙДЕР = "MNT", "POOL", "TRADER"

    def бал(owner, минт, ui, idx):
        return {"accountIndex": idx, "owner": owner, "mint": минт,
                "uiTokenAmount": {"amount": str(int(ui * 1e6)), "decimals": 6,
                                   "uiAmount": ui}}

    def tx(*, дельта_трейдера, пул_минт_после, пул_wsol_после, лампорты_до,
            лампорты_после, подписант=ТРЕЙДЕР):
        ключи = [подписант, ПУЛ]
        return {"slot": 10, "blockTime": 100,
                "transaction": {"signatures": ["S" * 88],
                                 "message": {"accountKeys": [
                                     {"pubkey": k, "signer": k == подписант} for k in ключи],
                                     "instructions": []}},
                "meta": {"err": None,
                          "preBalances": [лампорты_до, 0],
                          "postBalances": [лампорты_после, 0],
                          "preTokenBalances": [
                              бал(подписант, МИНТ, max(0.0, -dtrader(дельта_трейдера)), 0),
                              бал(ПУЛ, МИНТ, пул_минт_после - дельта_пула(дельта_трейдера), 1)],
                          "postTokenBalances": [
                              бал(подписант, МИНТ, max(0.0, dtrader(дельта_трейдера)), 0),
                              бал(ПУЛ, МИНТ, пул_минт_после, 1),
                              бал(ПУЛ, WSOL, пул_wsol_после, 1)],
                          "innerInstructions": []}}

    def dtrader(d):
        return d

    def дельта_пула(d):
        return -d

    продажа = tx(дельта_трейдера=-100.0, пул_минт_после=1000.0, пул_wsol_после=5.0,
                 лампорты_до=1_000_000_000, лампорты_после=1_400_000_000)
    ст = сторона(продажа, МИНТ)
    chk("направление: минус у подписанта -- это продажа",
        ст["who"] == ТРЕЙДЕР and float(ст["delta"]) < 0, ст)
    ост = остаток_пула(продажа, ПУЛ, МИНТ)
    chk("остаток пула по минту и по WSOL прочитан",
        ост["mint_after"] == 1000.0 and ост["wsol_after"] == 5.0, ост)

    вызовы = []

    def rpc(method, params):
        вызовы.append((method, params))
        if method == "getSignaturesForAddress":
            assert params[1].get("until") == "SRC", params
            return [{"signature": "B" * 88, "slot": 12, "err": None, "blockTime": 102},
                    {"signature": "A" * 88, "slot": 11, "err": None, "blockTime": 101},
                    {"signature": "Z" * 88, "slot": 999, "err": None, "blockTime": 999}]
        if method == "getTransaction":
            return продажа
        raise AssertionError(method)

    из_ = разобрать(rpc, минт=МИНТ, пул=ПУЛ, подпись_источника="SRC",
                     слот_источника=10, окно_слотов=5,
                     известные={ТРЕЙДЕР: "источник"})
    chk("повторная страница не задваивает строки (курсор не сдвинулся)",
        из_["n_signatures_total"] == 3 and из_["n_pages"] <= 2, из_["n_pages"])
    chk("подписи спрошены у ПУЛА и только после покупки источника",
        вызовы[0][0] == "getSignaturesForAddress" and вызовы[0][1][0] == ПУЛ, вызовы[0])
    chk("за окно слотов не выходим (999 отброшен)",
        из_["n_signatures_window"] == 2 and len(из_["rows"]) == 2, из_)
    chk("строки идут от старых к новым",
        [с["slot"] for с in из_["rows"]] == [11, 12], из_["rows"])
    chk("имя известного адреса подставлено, чужой назван незнакомым",
        из_["rows"][0]["who_name"] == "источник", из_["rows"][0])
    chk("сводка считает продажи и падение WSOL",
        из_["summary"]["n_sells"] == 2 and из_["summary"]["named_sellers"] == ["источник"],
        из_["summary"])
    chk("таблица собирается и содержит подпись",
        "| слот |" in в_таблицу(из_) and "BBBBBBBB" in в_таблицу(из_), в_таблицу(из_)[:200])

    # ПРОДАЖИ КОНКРЕТНОГО КОШЕЛЬКА -- адресно, а не выборкой по пулу.
    ВАУЛТ = "VAULT"

    def бал_счёта(owner, минт, ui, idx):
        return бал(owner, минт, ui, idx)

    покупка_тx = {"slot": 9, "blockTime": 99,
                   "transaction": {"signatures": ["P" * 88],
                                    "message": {"accountKeys": [
                                        {"pubkey": ТРЕЙДЕР, "signer": True},
                                        {"pubkey": ВАУЛТ, "signer": False}],
                                        "instructions": []}},
                   "meta": {"err": None, "preBalances": [0, 0], "postBalances": [0, 0],
                             "preTokenBalances": [бал_счёта(ТРЕЙДЕР, МИНТ, 0.0, 1)],
                             "postTokenBalances": [бал_счёта(ТРЕЙДЕР, МИНТ, 100.0, 1)],
                             "innerInstructions": []}}
    спрошено: list = []

    def rpc_кошелёк(method, params):
        спрошено.append((method, params[0]))
        if method == "getTransaction" and params[0] == "BUY":
            return покупка_тx
        if method == "getSignaturesForAddress":
            assert params[1].get("until") == "BUY", params
            return [{"signature": "S1" + "y" * 80, "slot": 15, "err": None}]
        return продажа

    прод = продажи_кошелька(rpc_кошелёк, кошелёк=ТРЕЙДЕР, минт=МИНТ,
                             подпись_покупки="BUY")
    chk("продажи кошелька: спрошен ЕГО токен-счёт из покупки",
        прод["ok"] and прод["vault"] == ВАУЛТ
        and ("getSignaturesForAddress", ВАУЛТ) in спрошено, (прод, спрошено))
    chk("продажа кошелька распознана по знаку дельты",
        прод["n_sells"] == 1 and прод["rows"][0]["side"] == "продажа", прод)

    без = разобрать(None, минт=МИНТ, пул=ПУЛ, подпись_источника="SRC", слот_источника=1)
    chk("без rpc_call -- честный отказ, а не пустая таблица",
        без["ok"] is False and "rpc_call" in (без["why_not"] or ""), без)

    предел = разобрать(rpc, минт=МИНТ, пул=ПУЛ, подпись_источника="SRC",
                        слот_источника=10, окно_слотов=5, max_tx=1)
    chk("предел --max-tx помечает отчёт частичным",
        предел["partial"] is True and len(предел["rows"]) == 1, предел)
    # КРАЯ ОКНА: при переполнении берём начало и конец, а пропущенное называем
    # числом. Иначе в busy-пуле таблица молча покажет только начало.
    много = [{"signature": f"S{i:02d}" + "x" * 80, "slot": 11 + i, "err": None}
             for i in range(10)]

    def rpc_много(method, params):
        if method == "getSignaturesForAddress":
            return много if not params[1].get("before") else []
        return продажа

    края = разобрать(rpc_много, минт=МИНТ, пул=ПУЛ, подпись_источника="SRC",
                      слот_источника=10, окно_слотов=100, max_tx=4)
    chk("края окна: взяты первые и последние, середина названа числом",
        len(края["rows"]) == 4 and края["n_skipped_middle"] == 6
        and края["rows"][0]["slot"] == 11 and края["rows"][-1]["slot"] == 20,
        {"rows": [с["slot"] for с in края["rows"]],
         "skipped": края["n_skipped_middle"]})

    print(f"самопроверка разбора сделки: {всего[1]}/{всего[0]} пройдено")
    return 0 if всего[0] == всего[1] else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--mint")
    p.add_argument("--pool")
    p.add_argument("--source-signature")
    p.add_argument("--source-slot", type=int)
    p.add_argument("--our-wallet", default="")
    p.add_argument("--source-wallet", default="")
    p.add_argument("--window-slots", type=int, default=120)
    p.add_argument("--max-tx", type=int, default=60)
    p.add_argument("--max-pages", type=int, default=40)
    p.add_argument("--credit-limit", type=int, default=300)
    p.add_argument("--out")
    p.add_argument("--out-md")
    a = p.parse_args()
    if a.self_test:
        return self_test()
    if not (a.mint and a.pool and a.source_signature and a.source_slot):
        print("нужны --mint --pool --source-signature --source-slot")
        return 2
    from night_leader_stonkfun import BudgetedRpc  # noqa: PLC0415
    import c2_common as C2m  # noqa: PLC0415

    rpc = BudgetedRpc(C2m.C2Rpc(service="c2_trade_forensics"), a.credit_limit)
    известные = {}
    if a.our_wallet:
        известные[a.our_wallet] = "мы"
    if a.source_wallet:
        известные[a.source_wallet] = "источник"
    из_ = разобрать(rpc, минт=a.mint, пул=a.pool,
                     подпись_источника=a.source_signature,
                     слот_источника=a.source_slot,
                     известные=известные, окно_слотов=a.window_slots,
                     max_tx=a.max_tx, max_pages=a.max_pages)
    # АДРЕСНО: продавал ли источник свой же токен в этом окне. В выборке по
    # пулу его может не быть просто потому, что в окне тысячи транзакций.
    if a.source_wallet:
        из_["source_sells"] = продажи_кошелька(
            rpc, кошелёк=a.source_wallet, минт=a.mint,
            подпись_покупки=a.source_signature)
    из_["chain_credits_used"] = getattr(rpc, "used", None)
    текст = в_таблицу(из_)
    print(json.dumps({k: v for k, v in из_.items() if k != "rows"}
                      if из_.get("ok") else из_, ensure_ascii=False, indent=1))
    print(текст)
    if a.out:
        Path(a.out).write_text(json.dumps(из_, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    if a.out_md:
        Path(a.out_md).write_text(текст + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
