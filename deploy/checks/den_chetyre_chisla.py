#!/usr/bin/env python3
"""Четыре числа дня плюс разбор несчитаемых по цепи (задание владельца 26.09).

ЗАЧЕМ. Учёт за 26.09 показал -0.572 SOL, а два кошелька за то же время
потеряли больше. Владелец велел закрыть разрыв четырьмя числами:
  1) сумма чаевых и приоритета по всем сделкам за день;
  2) сумма комиссий Bloom;
  3) рента, запертая сейчас: токен-счета по кошелькам, одним чтением по цепи;
  4) валовый ход цены суммой по позициям с итогом.
И отдельно: у 20 из 102 "несчитаемых" -- нативные дельты покупки и продажи ПО
ЦЕПИ и средний итог на позицию.

ДВА РЕЖИМА, потому что данные лежат в двух местах:
  --zhurnal  на ХОСТЕ: суммы из журнала позиций потоком + список подписей,
             которые надо посмотреть в цепи. Узел здесь не нужен.
  --cep      на бегунке: цепь через Helius -- рента по кошелькам, нативные
             дельты по подписям, получатели лампортов помимо пулов.

ЧЕГО ЗДЕСЬ НЕТ. Комиссию Bloom мы не запрашиваем и в журнал не пишем: её
берёт сама площадка внутри свопа. Поэтому число 2 считается ТОЛЬКО по цепи --
как лампорты, ушедшие с нашего кошелька не в пул, не на тариф сети и не нам.
Получатели печатаются адресами и числом попаданий: повторяющийся адрес и есть
счёт площадки, и это факт, а не догадка.

Только чтение. Ни подписей, ни отправок.
"""
import argparse
import calendar
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ЛАМПОРТОВ_В_SOL = 1_000_000_000
РЕНТА_ТОКЕН_СЧЁТА_ЛАМПОРТЫ = 2_039_280  # обычный счёт SPL-токена
МИН_ПОДПИСЬ = 80
ПАКЕТ = 25
_URL = re.compile(r"(?i)\b(?:https?|wss?)://\S+")
_КЛЮЧ = re.compile(r"(?i)(api[-_]?key|api[-_]?token|bearer)\s*[=:]\s*\S+")


def чисто(x: str) -> str:
    s = _URL.sub("<url-vycishcheno>", str(x))
    return _КЛЮЧ.sub("<klyuch-vycishcheno>", s)


def окно(момент: str) -> float:
    return calendar.timegm(time.strptime(момент, "%Y-%m-%dT%H:%M:%SZ"))


def строки(путь: str):
    откр = gzip.open if путь.endswith(".gz") else open
    with откр(путь, "rt", encoding="utf-8", errors="replace") as ф:
        for ln in ф:
            ln = ln.strip()
            if ln.startswith("{"):
                try:
                    yield json.loads(ln)
                except ValueError:
                    continue


def пути_журнала(каталог: str, имя: str) -> list:
    основной = os.path.join(каталог, имя)
    обороты = []
    try:
        for ф in sorted(os.listdir(каталог)):
            if ф.startswith(имя + ".") and ф.endswith(".gz"):
                обороты.append(os.path.join(каталог, ф))
    except OSError:
        pass
    return обороты + ([основной] if os.path.exists(основной) else [])


def подпись(з) -> str | None:
    if isinstance(з, str) and len(з) >= МИН_ПОДПИСЬ:
        return з
    return None


def подпись_покупки(п: dict) -> str | None:
    for поле in ("lane_landed_signature", "signature"):
        с = подпись(п.get(поле))
        if с:
            return с
    for з in (п.get("signatures") or []):
        с = подпись(з)
        if с:
            return с
    return подпись(п.get("lane_signature"))


def подпись_продажи(п: dict) -> str | None:
    с = подпись(п.get("closed_signature"))
    if с:
        return с
    о = п.get("last_sell_reported")
    if isinstance(о, dict):
        for поле in ("signature", "sig"):
            с = подпись(о.get(поле))
            if с:
                return с
    elif isinstance(о, str):
        с = подпись(о)
        if с:
            return с
    for з in (п.get("last_sell_signatures") or []):
        с = подпись(з)
        if с:
            return с
    и = п.get("last_sell_outcome")
    if isinstance(и, dict):
        return подпись(и.get("signature"))
    return None


# ----------------------------------------------------------------- журнал
def режим_журнала(а) -> int:
    порог = окно(а.since_utc)
    по_cid: dict = {}
    for путь in пути_журнала(а.state_dir, "positions.jsonl"):
        for з in строки(путь):
            cid = з.get("client_order_id")
            if not cid:
                continue
            по_cid.setdefault(cid, {}).update(
                {к: v for к, v in з.items() if v is not None})
    за_день = {cid: п for cid, п in по_cid.items()
               if float(п.get("ts_intent") or 0) >= порог}

    чаевые = приоритет = 0.0
    валовый = 0.0
    с_итогом = 0
    несчитаемые = []
    кошельки = {}
    полоса_сделок = bloom_сделок = 0
    for cid, п in за_день.items():
        (полоса_сделок := полоса_сделок + 1) if п.get("lane") else (
            bloom_сделок := bloom_сделок + 1)
        к = п.get("wallet")
        if к:
            кошельки[к] = кошельки.get(к, 0) + 1
        чаевые += float(п.get("lane_tips_total_sol") or 0.0)
        приоритет += float(п.get("lane_priority_lamports") or 0) / ЛАМПОРТОВ_В_SOL
        вход = п.get("sol_in")
        возврат = п.get("closed_sol_net")
        if возврат is None:
            возврат = (п.get("last_sell_outcome") or {}).get("sol_delta_net")
        if п.get("pnl_counted_sol") is not None:
            с_итогом += 1
            if вход is not None and возврат is not None:
                # ВАЛОВЫЙ ХОД ЦЕНЫ: возврат минус вход, БЕЗ расхода на отправку.
                валовый += float(возврат) - float(вход)
        если_несчит = (п.get("uncountable_shared_mint")
                       or п.get("result_uncountable")
                       or "не добрал" in str(п.get("result_uncountable_why") or ""))
        if если_несчит:
            несчитаемые.append({
                "cid": cid, "wallet": к, "mint": п.get("mint"),
                "group": п.get("lane_group"), "in_sol": вход,
                "back_sol": возврат,
                "pnl_zapis": п.get("pnl_counted_sol"),
                "spend_zapis": п.get("pnl_counted_spend_sol"),
                "buy_sig": подпись_покупки(п), "sell_sig": подпись_продажи(п),
                "why": чисто(str(п.get("result_uncountable_why") or ""))[:120]})

    несчитаемые.sort(key=lambda з: (з["buy_sig"] is None, з["sell_sig"] is None))

    # ОБРАЗЕЦ СДЕЛОК ПЛОЩАДКИ -- для числа 2. Комиссию Bloom мы не запрашиваем
    # и в журнал не пишем: она берётся внутри свопа. Единственный способ её
    # увидеть -- посмотреть, кому ушли лампорты с нашего кошелька в самих
    # транзакциях площадки.
    образец_bloom = []
    for cid, п in sorted(за_день.items(),
                         key=lambda кв: float(кв[1].get("ts_intent") or 0)):
        if п.get("lane"):
            continue
        б = подпись_покупки(п)
        if not б:
            continue
        образец_bloom.append({"cid": cid, "wallet": п.get("wallet"),
                              "mint": п.get("mint"), "in_sol": п.get("sol_in"),
                              "buy_sig": б, "sell_sig": подпись_продажи(п)})
    итог = {
        "since_utc": а.since_utc,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pozitsiy_za_den": len(за_день),
        "polosa": полоса_сделок, "bloom": bloom_сделок,
        "chislo_1_tips_priority_sol": round(чаевые + приоритет, 9),
        "chislo_1_tips_sol": round(чаевые, 9),
        "chislo_1_priority_sol": round(приоритет, 9),
        "chislo_4_valovyy_hod_sol": round(валовый, 9),
        "pozitsiy_s_itogom": с_итогом,
        "neschitaemyh": len(несчитаемые),
        "wallets": кошельки,
        "obrazets_neschitaemyh": несчитаемые[:а.skolko],
        "bloom_sdelok_s_podpisyu": len(образец_bloom),
        "obrazets_bloom": образец_bloom[:а.skolko_bloom],
    }
    with open(а.out, "w", encoding="utf-8") as ф:
        json.dump(итог, ф, ensure_ascii=False, indent=1)
    печать = dict(итог)
    печать["obrazets_neschitaemyh"] = f"{len(итог['obrazets_neschitaemyh'])} штук в файле"
    печать["obrazets_bloom"] = f"{len(итог['obrazets_bloom'])} штук в файле"
    print(json.dumps(печать, ensure_ascii=False, indent=1))
    return 0


# -------------------------------------------------------------------- цепь
class Узел:
    """JSON-RPC через urllib: на бегунке может не быть requests."""

    def __init__(self, url: str):
        self.url = url
        self.вызовов = 0

    def пакет(self, запросы: list) -> list:
        тело = json.dumps(запросы).encode("utf-8")
        задержка = 0.4
        for попытка in range(6):
            зап = urllib.request.Request(
                self.url, data=тело,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(зап, timeout=40) as отв:
                    self.вызовов += len(запросы)
                    д = json.loads(отв.read().decode("utf-8"))
                    return д if isinstance(д, list) else [д]
            except urllib.error.HTTPError as ош:
                if ош.code != 429 or попытка == 5:
                    raise RuntimeError(f"HTTP {ош.code}: {чисто(str(ош))[:120]}") from None
            except Exception as ош:  # noqa: BLE001
                if попытка == 5:
                    raise RuntimeError(чисто(str(ош))[:160]) from None
            time.sleep(задержка)
            задержка *= 2
        return []

    def один(self, метод: str, параметры: list):
        о = self.пакет([{"jsonrpc": "2.0", "id": 1, "method": метод,
                         "params": параметры}])
        return (о[0] or {}).get("result") if о else None


def нативная_дельта(tx: dict, кошелёк: str) -> dict:
    """Дельта нашего кошелька и тариф сети по этой транзакции."""
    из_ = {"delta_sol": None, "fee_sol": None, "err": None, "slot": None,
           "poluchateli": []}
    if not tx:
        return из_
    мета = tx.get("meta") or {}
    из_["err"] = мета.get("err")
    из_["slot"] = tx.get("slot")
    из_["fee_sol"] = (мета.get("fee") or 0) / ЛАМПОРТОВ_В_SOL
    ключи = (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys")
             or [])
    адреса = [(к.get("pubkey") if isinstance(к, dict) else к) for к in ключи]
    до, после = мета.get("preBalances") or [], мета.get("postBalances") or []
    for и, адрес in enumerate(адреса):
        if и >= len(до) or и >= len(после):
            break
        д = (после[и] - до[и]) / ЛАМПОРТОВ_В_SOL
        if адрес == кошелёк:
            из_["delta_sol"] = д
        elif д > 0:
            из_["poluchateli"].append({"address": адрес, "sol": round(д, 9)})
    из_["poluchateli"].sort(key=lambda з: -з["sol"])
    return из_


def режим_цепи(а) -> int:
    ключ = (os.environ.get("HELIUS_API_KEY") or "").strip()
    if not ключ:
        print("СТОП: HELIUS_API_KEY не задан", file=sys.stderr)
        return 2
    узел = Узел(f"https://mainnet.helius-rpc.com/?api-key={ключ}")
    with open(а.vhod, encoding="utf-8") as ф:
        вх = json.load(ф)

    # ЧИСЛО 3: рента, запертая в токен-счетах, по каждому кошельку -- по цепи.
    рента = {}
    for кошелёк in sorted(вх.get("wallets") or {}):
        всего_счетов = 0
        лампортов = 0
        пустых = 0
        for программа in ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                          "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
            рез = узел.один("getTokenAccountsByOwner",
                            [кошелёк, {"programId": программа},
                             {"encoding": "jsonParsed"}])
            for счёт in ((рез or {}).get("value") or []):
                всего_счетов += 1
                лампортов += int(счёт.get("account", {}).get("lamports") or 0)
                инфо = (((счёт.get("account") or {}).get("data") or {})
                        .get("parsed", {}).get("info", {}))
                сумма = ((инфо.get("tokenAmount") or {}).get("amount")) or "0"
                if сумма == "0":
                    пустых += 1
        рента[кошелёк] = {"token_accounts": всего_счетов,
                          "lamports": лампортов,
                          "sol": round(лампортов / ЛАМПОРТОВ_В_SOL, 9),
                          "pustyh": пустых,
                          "sol_v_pustyh_po_rente": round(
                              пустых * РЕНТА_ТОКЕН_СЧЁТА_ЛАМПОРТЫ
                              / ЛАМПОРТОВ_В_SOL, 9)}

    # НЕСЧИТАЕМЫЕ ПО ЦЕПИ: нативные дельты покупки и продажи.
    образец = (вх.get("obrazets_neschitaemyh") or [])[:а.skolko]
    подписи = []
    for з in образец:
        for поле in ("buy_sig", "sell_sig"):
            if з.get(поле):
                подписи.append(з[поле])
    txs = {}
    for н in range(0, len(подписи), ПАКЕТ):
        куски = подписи[н:н + ПАКЕТ]
        запросы = [{"jsonrpc": "2.0", "id": и, "method": "getTransaction",
                    "params": [с, {"encoding": "jsonParsed",
                                   "maxSupportedTransactionVersion": 0}]}
                   for и, с in enumerate(куски)]
        for о in узел.пакет(запросы):
            и = о.get("id")
            if isinstance(и, int) and и < len(куски):
                txs[куски[и]] = о.get("result")

    разобрано = []
    получатели = {}
    сумма_итогов = 0.0
    с_числом = 0
    for з in образец:
        к = з.get("wallet")
        п = нативная_дельта(txs.get(з.get("buy_sig")) or {}, к) if з.get("buy_sig") else {}
        пр = нативная_дельта(txs.get(з.get("sell_sig")) or {}, к) if з.get("sell_sig") else {}
        итог = None
        if п.get("delta_sol") is not None and пр.get("delta_sol") is not None:
            итог = п["delta_sol"] + пр["delta_sol"]
            сумма_итогов += итог
            с_числом += 1
        for сторона in (п, пр):
            for пол in (сторона.get("poluchateli") or [])[:6]:
                з_п = получатели.setdefault(пол["address"], {"raz": 0, "sol": 0.0})
                з_п["raz"] += 1
                з_п["sol"] = round(з_п["sol"] + пол["sol"], 9)
        разобрано.append({
            "cid": з.get("cid"), "mint": з.get("mint"), "group": з.get("group"),
            "in_sol": з.get("in_sol"), "pnl_zapis": з.get("pnl_zapis"),
            "buy_delta_sol": (None if п.get("delta_sol") is None
                              else round(п["delta_sol"], 9)),
            "buy_fee_sol": п.get("fee_sol"), "buy_err": п.get("err"),
            "sell_delta_sol": (None if пр.get("delta_sol") is None
                               else round(пр["delta_sol"], 9)),
            "sell_fee_sol": пр.get("fee_sol"), "sell_err": пр.get("err"),
            "itog_po_cepi": None if итог is None else round(итог, 9),
            "why": з.get("why"),
        })

    # СДЕЛКИ ПЛОЩАДКИ: кому ушли лампорты помимо нас. Пулы отличаются тем, что
    # адрес у каждой сделки свой; счёт площадки повторяется на каждой.
    образец_б = (вх.get("obrazets_bloom") or [])[:а.skolko_bloom]
    подписи_б = [з[поле] for з in образец_б for поле in ("buy_sig", "sell_sig")
                 if з.get(поле)]
    txs_б = {}
    for н in range(0, len(подписи_б), ПАКЕТ):
        куски = подписи_б[н:н + ПАКЕТ]
        запросы = [{"jsonrpc": "2.0", "id": и, "method": "getTransaction",
                    "params": [с, {"encoding": "jsonParsed",
                                   "maxSupportedTransactionVersion": 0}]}
                   for и, с in enumerate(куски)]
        for о in узел.пакет(запросы):
            и = о.get("id")
            if isinstance(и, int) and и < len(куски):
                txs_б[куски[и]] = о.get("result")
    получатели_б = {}
    тариф_б = 0.0
    дельта_б = 0.0
    tx_с_дельтой = 0
    for з in образец_б:
        к = з.get("wallet")
        for поле in ("buy_sig", "sell_sig"):
            if not з.get(поле):
                continue
            д = нативная_дельта(txs_б.get(з[поле]) or {}, к)
            if д.get("delta_sol") is not None:
                дельта_б += д["delta_sol"]
                tx_с_дельтой += 1
            тариф_б += float(д.get("fee_sol") or 0.0)
            for пол in (д.get("poluchateli") or []):
                з_п = получатели_б.setdefault(пол["address"],
                                              {"raz": 0, "sol": 0.0})
                з_п["raz"] += 1
                з_п["sol"] = round(з_п["sol"] + пол["sol"], 9)
    # ПОВТОРЯЮЩИЕСЯ получатели -- это и есть постоянные сборы (площадка,
    # чаевые, рента счетов). Разовые -- пулы и наши же новые счета.
    постоянные = {а_: в_ for а_, в_ in получатели_б.items()
                  if в_["raz"] >= max(3, len(образец_б) // 4)}
    верх_б = sorted(получатели_б.items(), key=lambda кв: -кв[1]["raz"])[:12]

    верх = sorted(получатели.items(), key=lambda кв: -кв[1]["sol"])[:12]
    итог = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "iz_zhurnala": {к: v for к, v in вх.items()
                        if к != "obrazets_neschitaemyh"},
        "chislo_3_renta_po_koshelkam": рента,
        "chislo_3_renta_vsego_sol": round(
            sum(з["sol"] for з in рента.values()), 9),
        "neschitaemye_po_cepi": разобрано,
        "neschitaemye_s_chislom": с_числом,
        "neschitaemye_sredniy_itog_sol": (round(сумма_итогов / с_числом, 9)
                                          if с_числом else None),
        "neschitaemye_summa_itogov_sol": round(сумма_итогов, 9),
        # ЧИСЛО 2 ЖИВЁТ ЗДЕСЬ: получатели лампортов помимо нас. Повторяющийся
        # адрес с крупной суммой -- счёт площадки или чаевые отправителя.
        "poluchateli_lamportov": [{"address": а_, **в_} for а_, в_ in верх],
        "chislo_2_bloom": {
            "sdelok_v_obrazce": len(образец_б),
            "tranzakciy_s_deltoy": tx_с_дельтой,
            "summa_nativnyh_delt_sol": round(дельта_б, 9),
            "tarif_seti_sol": round(тариф_б, 9),
            "postoyannye_poluchateli": [
                {"address": а_, **в_,
                 "sol_na_tranzakciyu": round(в_["sol"] / max(1, в_["raz"]), 9)}
                for а_, в_ in sorted(постоянные.items(),
                                     key=lambda кв: -кв[1]["sol"])],
            "summa_postoyannyh_sol": round(
                sum(в_["sol"] for в_ in постоянные.values()), 9),
            "verh_po_chastote": [{"address": а_, **в_} for а_, в_ in верх_б],
        },
        "vyzovov_uzla": узел.вызовов,
    }
    with open(а.out, "w", encoding="utf-8") as ф:
        json.dump(итог, ф, ensure_ascii=False, indent=1)
    печать = dict(итог)
    печать["neschitaemye_po_cepi"] = f"{len(разобрано)} штук в файле"
    print(json.dumps(печать, ensure_ascii=False, indent=1))
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--zhurnal", action="store_true")
    р.add_argument("--cep", action="store_true")
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--since-utc", default="2026-09-26T00:00:00Z")
    р.add_argument("--skolko", type=int, default=20)
    р.add_argument("--skolko-bloom", type=int, default=40,
                   help="сколько сделок площадки смотреть для числа 2")
    р.add_argument("--vhod", default="/tmp/den_chisla_zhurnal.json")
    р.add_argument("--out", default="/tmp/den_chisla.json")
    а = р.parse_args()
    if а.zhurnal:
        return режим_журнала(а)
    if а.cep:
        return режим_цепи(а)
    print("СТОП: нужен --zhurnal или --cep", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
