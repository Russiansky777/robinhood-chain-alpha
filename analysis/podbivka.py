#!/usr/bin/env python3
"""Подбивка (задание 2): симулятор копии и контроль цены по цепи.

ЧТО ЭТО. Симулятор отвечает на один вопрос: сколько токенов мы получили бы,
войдя размером X в тот же пул сразу после сделки источника. Ответ он берёт из
РЕЗЕРВОВ ПУЛА, восстановленных по pre/post-балансам хранилищ той самой
транзакции источника, и из формулы этого типа пула. Контроль цены (пункт 1
задания) сверяет симулятор с ФАКТОМ по цепи на наших же сделках: если
расхождение велико, всем дальнейшим числам верить нельзя.

ЧЕСТНОСТЬ. Ничего не достраивается: сделка без опознанного пула, без резервов
или без нашей севшей транзакции попадает в отказы С ПРИЧИНОЙ, а не в среднее.
Концентрированная ликвидность (DLMM, CLMM, Whirlpool) резервами цену не даёт --
там берётся цена исполнения последнего свопа и ставится флаг.

Только чтение цепи. Ни подписи, ни отправки, ключей в модуле нет.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import c2_swap_build as B  # noqa: E402

# Версия 1 (слово владельца 26.09): на 0 узел отвечает -32015 на транзакциях
# версии 1 -- это вероятная причина 15 отказов «узел: RuntimeError» контроля.
ОПЦИИ_TX = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1,
            "commitment": "confirmed"}
СОСРЕДОТОЧЕННЫЕ = {B.DLMM, B.CLMM, "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"}
ЛАМПОРТОВ = 1_000_000_000


def узел() -> str:
    # Решение владельца 26.09: RPC всей цепи для подбивки -- Shyft, Helius
    # оставлен детектору и сторожу. Прогон кладёт ключ Shyft в SHYFT_API_KEY
    # (секрет -- тот, под которым Shyft ходил 24.09); без него -- прежний путь,
    # чтобы не сломать старые прогоны первой сессии.
    shyft = (os.environ.get("SHYFT_API_KEY") or "").strip()
    if shyft:
        return f"https://rpc.shyft.to?api_key={shyft}"
    ключ = (os.environ.get("HELIUS_API_KEY") or os.environ.get("HELIUS_API") or "").strip()
    if not ключ:
        raise RuntimeError("ключа узла нет в окружении (HELIUS_API_KEY)")
    return f"https://mainnet.helius-rpc.com/?api-key={ключ}"


class Узел:
    """Счётчик вызовов рядом с самим вызовом: расход виден в отчёте."""

    def __init__(self) -> None:
        self.вызовов = 0
        self.ошибок = 0
        self._кэш: dict = {}

    def вызов(self, метод: str, парам: list, *, срок: float = 25.0):
        import requests  # noqa: PLC0415
        self.вызовов += 1
        от = requests.post(узел(), json={"jsonrpc": "2.0", "id": 1, "method": метод,
                                         "params": парам}, timeout=срок)
        от.raise_for_status()
        тело = от.json()
        if "error" in тело:
            self.ошибок += 1
            raise RuntimeError(f"узел: {str(тело['error'])[:140]}")
        return тело.get("result")

    def транзакция(self, подпись: str):
        if подпись in self._кэш:
            return self._кэш[подпись]
        tx = self.вызов("getTransaction", [подпись, ОПЦИИ_TX])
        self._кэш[подпись] = tx
        return tx


# --------------------------------------------------------- факт по цепи

def факт_покупки(tx: dict, кошелёк: str, минт: str | None = None) -> dict:
    """Что реально дала НАША транзакция: токенов получено, SOL ушло.

    Токены -- прирост наших токен-счетов (post - pre) по минту; SOL -- нативная
    дельта кошелька со снятой платой за подпись. Если прироста токенов нет,
    так и пишем: сделка села, а токен не пришёл (внутренняя ошибка свопа).
    """
    из_ = {"ok": False, "why_not": None, "tokens_raw": None, "decimals": None,
           "mint": минт, "sol_out": None, "fee_sol": None, "err": None, "slot": (tx or {}).get("slot")}
    if not tx:
        из_["why_not"] = "узел не отдал транзакцию"
        return из_
    мета = tx.get("meta") or {}
    из_["err"] = мета.get("err")
    из_["fee_sol"] = (мета.get("fee") or 0) / ЛАМПОРТОВ
    ряды = C.token_rows(tx)
    наши = [r for r in ряды.values() if r.get("owner") == кошелёк
            and (минт is None or r.get("mint") == минт)]
    прирост = [(r["post"] - r["pre"], r) for r in наши if (r["post"] - r["pre"]) > 0
               and r.get("mint") != C.WSOL]
    if прирост:
        д, r = max(прирост, key=lambda x: x[0])
        из_.update(tokens_raw=д, decimals=r.get("dec"), mint=r.get("mint"))
    else:
        из_["tokens_raw"] = 0
        из_["why_not"] = "токен на наш счёт не пришёл"
    ключи = C.account_keys(tx)
    try:
        и = ключи.index(кошелёк)
        до = (мета.get("preBalances") or [])[и]
        после = (мета.get("postBalances") or [])[и]
        из_["sol_out"] = round((до - после) / ЛАМПОРТОВ, 9)
    except (ValueError, IndexError):
        из_["why_not"] = (из_["why_not"] or "") + "; нашего кошелька нет в счетах транзакции"
    из_["ok"] = из_["tokens_raw"] not in (None,) and из_["sol_out"] is not None
    return из_


# --------------------------------------------------------- симулятор копии

def резервы_после(tx_источника: dict, источник: str, минт: str) -> dict:
    """Резервы пула ПОСЛЕ сделки источника -- по pre/post хранилищ этой же tx."""
    из_ = {"ok": False, "why_not": None, "program": None, "quote_mint": None,
           "base_after": None, "quote_after": None, "pool_vault": None,
           "concentrated": False, "curve": False, "price_last": None}
    пул = C.identify_pool(tx_источника, источник, минт)
    if not пул.get("ok") and not пул.get("pool_vault"):
        из_["why_not"] = f"пул не опознан: {пул.get('why_not')}"
        return из_
    из_["pool_vault"] = пул.get("pool_vault")
    из_["quote_mint"] = пул.get("quote_mint")
    try:
        import c2_pool_programs as PP  # noqa: PLC0415
        прог = PP.pool_program(tx_источника, пул.get("pool_vault"), PP.labels())["pool_program"]
    except Exception as exc:  # noqa: BLE001
        прог = None
        из_["why_not"] = f"программа пула не определена ({type(exc).__name__})"
    из_["program"] = прог
    из_["curve"] = (прог == B.BONDING)
    из_["concentrated"] = (прог in СОСРЕДОТОЧЕННЫЕ)
    ряды = {r["account"]: r for r in C.token_rows(tx_источника).values()}
    бв = ряды.get(пул.get("pool_vault"))
    кв = ряды.get(пул.get("quote_vault"))
    if бв:
        из_["base_after"] = бв["post"]
        из_["base_dec"] = бв.get("dec")
    if кв:
        из_["quote_after"] = кв["post"]
        из_["quote_dec"] = кв.get("dec")
    # Цена исполнения последнего свопа -- она нужна и как запасной путь, и
    # для сосредоточенной ликвидности, где резервы цену не дают.
    if бв and кв:
        дб = бв["pre"] - бв["post"]
        дк = кв["post"] - кв["pre"]
        if дб > 0 and дк > 0:
            из_["price_last"] = дк / дб
    из_["ok"] = bool(из_["base_after"] is not None or из_["curve"] or из_["price_last"])
    if not из_["ok"]:
        из_["why_not"] = из_["why_not"] or "хранилищ пула нет в балансах транзакции источника"
    return из_


# Доля траты, доходящая до пула: у настоящих пулов это 0.9-1.0 (комиссия
# 0.25-1 %, иногда плюс доля платформы). Значение вне этой рамки означает, что
# калибровка поймала не то направление или не то хранилище -- такие сделки
# идут в отказ с числом, а не в среднее. 26.09 ровно на этом симулятор давал
# расхождения до 1.6e10 п.п.
РАМКА_КОМИССИИ = (0.80, 1.0)


def наша_покупка_по_симулятору(tx_источника: dict, источник: str, минт: str,
                               лампорты: int, *, проскальзывание: float = 0.0) -> dict:
    """Сколько токенов дал бы пул на нашу трату сразу после сделки источника."""
    из_ = {"ok": False, "why_not": None, "tokens_raw": None, "method": None,
           "concentrated": False, "curve": False, "program": None, "quote_mint": None,
           "fee_factor": None}
    рез = резервы_после(tx_источника, источник, минт)
    из_.update(program=рез.get("program"), concentrated=рез["concentrated"], curve=рез["curve"],
               quote_mint=рез.get("quote_mint"))
    if not рез["ok"]:
        из_["why_not"] = рез["why_not"]
        return из_
    # КОТИРОВКА ОБЯЗАНА БЫТЬ SOL. Иначе наши лампорты попадают в резерв другого
    # токена с другими decimals, и число выходит бессмысленным: 26.09 это дало
    # медиану 5.9 п.п. и выбросы до 1.6e10 на пулах CPMM с котировкой не SOL.
    котировка = рез.get("quote_mint")
    if not рез["curve"] and котировка not in (C.WSOL, getattr(C, "NATIVE_QUOTE", "native_sol"), None):
        из_["why_not"] = f"котировка пула не SOL ({котировка[:12]})"
        return из_
    прог = рез.get("program")
    # 1. Кривая pump.fun -- своя формула по событию сделки (как в I.2б).
    if прог == B.BONDING:
        мо = B.bonding_min_out(tx_источника, минт, лампорты, проскальзывание)
        if мо.get("ok"):
            из_.update(ok=True, tokens_raw=int(мо["expected_out"]),
                       method="кривая pump.fun по событию сделки")
            return из_
        из_["why_not"] = f"кривая: {мо.get('why_not')}"
        return из_
    # 2. Сосредоточенная ликвидность -- цена последнего свопа, без резервов.
    if рез["concentrated"]:
        if рез.get("price_last"):
            из_.update(ok=True, tokens_raw=int(лампорты / рез["price_last"]),
                       method="цена исполнения последнего свопа (сосредоточенная ликвидность)")
            return из_
        из_["why_not"] = "сосредоточенная ликвидность и цены последнего свопа нет"
        return из_
    # 3. Обычный x*y=k по резервам ПОСЛЕ сделки источника, комиссия пула
    #    калибруется на этой же сделке (так же, как в сборщике).
    tpl = B.extract_template(tx_источника, прог, рез["pool_vault"]) if прог else {"ok": False,
                                                                                 "why_not": "нет программы"}
    if tpl.get("ok"):
        мо = B.min_out_from_reserves(tpl, tx_источника, лампорты, проскальзывание)
        if мо.get("ok"):
            f = мо.get("fee_factor")
            из_["fee_factor"] = round(float(f), 6) if f is not None else None
            if f is not None and not (РАМКА_КОМИССИИ[0] <= float(f) <= РАМКА_КОМИССИИ[1]):
                из_["why_not"] = (f"калиброванная доля траты {float(f):.4f} вне рамки "
                                  f"{РАМКА_КОМИССИИ} -- направление или хранилище не то")
                return из_
            из_.update(ok=True, tokens_raw=int(мо["expected_out"]),
                       method="x*y=k по резервам после источника, комиссия калибрована")
            return из_
        из_["why_not"] = f"резервы: {мо.get('why_not')}"
    else:
        из_["why_not"] = f"шаблон пула: {tpl.get('why_not')}"
    # 4. Запасной путь -- цена исполнения его же свопа. Флаг ставим.
    if рез.get("price_last") and котировка in (C.WSOL, getattr(C, "NATIVE_QUOTE", "native_sol")):
        из_.update(ok=True, tokens_raw=int(лампорты / рез["price_last"]),
                   method="цена исполнения свопа источника (запасной путь)")
    elif рез.get("price_last"):
        из_["why_not"] = (из_.get("why_not") or "") + "; запасной путь тоже не годится: котировка не SOL"
    return из_


# --------------------------------------------------------- пункт 1: контроль цены

def контроль_цены(сделки: list, уз: Узел, *, кошелёк_площадки: str,
                  кошелёк_полосы: str, предел: int = 0) -> dict:
    ряды, отказы = [], []
    for и, с in enumerate(сделки, 1):
        if предел and и > предел:
            break
        подпись_наша = (с.get("lane_landed_signature") or с.get("signature")
                        or (с.get("signatures") or [None])[0])
        cid = с.get("client_order_id")
        строка = {"cid": cid, "mint": с.get("mint"), "lane": bool(с.get("lane")),
                  "sol_in": с.get("sol_in"), "ts": с.get("ts_intent_utc"),
                  "source_sig": с.get("source_sig"), "our_sig": подпись_наша}
        if not с.get("source_sig") or not подпись_наша:
            строка["why_not"] = ("нет подписи источника" if not с.get("source_sig")
                                 else "нет нашей подписи")
            отказы.append(строка)
            continue
        кошелёк = кошелёк_полосы if с.get("lane") else кошелёк_площадки
        try:
            tx_ист = уз.транзакция(с["source_sig"])
            tx_наша = уз.транзакция(подпись_наша)
        except Exception as exc:  # noqa: BLE001
            строка["why_not"] = f"узел: {type(exc).__name__}"
            отказы.append(строка)
            continue
        факт = факт_покупки(tx_наша, кошелёк, с.get("mint"))
        строка.update(факт_токенов=факт.get("tokens_raw"), факт_sol=факт.get("sol_out"),
                      факт_err=факт.get("err"), факт_why=факт.get("why_not"))
        if not факт.get("tokens_raw"):
            строка["why_not"] = факт.get("why_not") or "токенов по факту нет"
            отказы.append(строка)
            continue
        лампорты = int(round(float(с.get("sol_in") or 0) * ЛАМПОРТОВ))
        сим = наша_покупка_по_симулятору(tx_ист, с.get("source") or "", строка["mint"] or факт["mint"],
                                         лампорты)
        строка.update(сим_токенов=сим.get("tokens_raw"), сим_метод=сим.get("method"),
                      сим_программа=сим.get("program"), сосредоточенный=сим.get("concentrated"),
                      кривая=сим.get("curve"))
        if not сим.get("ok") or not сим.get("tokens_raw"):
            строка["why_not"] = сим.get("why_not") or "симулятор не дал числа"
            отказы.append(строка)
            continue
        расх = (факт["tokens_raw"] / сим["tokens_raw"] - 1.0) * 100.0
        строка["расхождение_пп"] = round(расх, 3)
        ряды.append(строка)
    абс = sorted(abs(r["расхождение_пп"]) for r in ряды)
    def квантиль(доля):
        if not абс:
            return None
        и = min(len(абс) - 1, int(round(доля * (len(абс) - 1))))
        return round(абс[и], 3)
    по_методу: dict = {}
    for r in ряды:
        м = r.get("сим_метод") or "без метода"
        б = по_методу.setdefault(м, [])
        б.append(abs(r["расхождение_пп"]))
    методы = {м: {"сделок": len(л), "медиана_пп": round(statistics.median(л), 3),
                  "макс_пп": round(max(л), 3)} for м, л in по_методу.items()}
    свод = {
        "сделок_на_входе": len(сделки), "сверено": len(ряды), "отказов": len(отказы),
        "по_методу": методы,
        "медиана_расхождения_пп": round(statistics.median(абс), 3) if абс else None,
        "p90_расхождения_пп": квантиль(0.9),
        "макс_расхождения_пп": абс[-1] if абс else None,
        "вызовов_rpc": уз.вызовов,
        "причины_отказов": {},
    }
    for о in отказы:
        к = (о.get("why_not") or "без причины")[:80]
        свод["причины_отказов"][к] = свод["причины_отказов"].get(к, 0) + 1
    return {"свод": свод, "ряды": ряды, "отказы": отказы}


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--sdelki", default="/tmp/nashi_sdelki.json")
    р.add_argument("--out", default=str(КОРЕНЬ / "data" / "podbivka" / "kontrol_ceny.json"))
    р.add_argument("--limit", type=int, default=0)
    р.add_argument("--lane-wallet", default="21DqHDDPEfMhK1dHRkV9E8v8KTTSKQGApJAr1irC9j7w")
    а = р.parse_args()

    д = json.loads(Path(а.sdelki).read_text(encoding="utf-8"))
    сделки = д.get("сделки") or []
    уз = Узел()
    начало = time.time()
    итог = контроль_цены(сделки, уз, кошелёк_площадки=C.EXECUTOR_WALLET,
                         кошелёк_полосы=а.lane_wallet, предел=а.limit)
    итог["свод"]["секунд"] = round(time.time() - начало, 1)
    итог["свод"]["utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    Path(а.out).parent.mkdir(parents=True, exist_ok=True)
    Path(а.out).write_text(json.dumps(итог, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(итог["свод"], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
