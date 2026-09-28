#!/usr/bin/env python3
"""DBot BATCH-3 и BATCH-5 за сутки по Мадриду: таблица по кошелькам.

Что просил владелец (26.09 вечером, только чтение): по каждому кошельку за
26.09 и 25.09 -- изменение баланса по цепи с 00:00, число закрытых сделок,
средний итог на сделку в % и в SOL, лучшая и худшая сделка, сумма издержек
(приоритет, чаевые, комиссия DBot). Без выводов, одна таблица. Отдельной
строкой -- те же источники у нашей полосы за 26.09.

ПОЧЕМУ ИТОГ СЧИТАЕТСЯ ПО ЦЕПИ, А НЕ ПО ЖУРНАЛУ DBOT. В журнале follow_trades
у BATCH-3/5 состоявшихся записей 191, и все они -- ПОКУПКИ: продажи там лежат
как отказы с причиной FOLLOW_ORDER_IS_ONLY_PNL_MODE, потому что закрывает
DBot своим режимом PnL, и в этот журнал они не попадают вовсе. Значит по
журналу итог сделки не собрать -- только по нативным дельтам кошелька.

КАК СЧИТАЕТСЯ СДЕЛКА. По каждому минту в сутках: все транзакции кошелька,
где двигался этот минт. Итог = сумма нативных дельт кошелька по ним (это
уже включает тариф сети, чаевые, комиссию площадки и возврат ренты -- ровно
как в учёте полосы). Закрытой считается сделка, у которой остаток минта на
конец суток вернулся в ноль. Вход = СКОЛЬКО УШЛО С КОШЕЛЬКА НА ПОКУПКЕ, то
есть модуль нативной дельты покупки; проценты считаются от него.

ЧТО СЧИТАЕТСЯ ИЗДЕРЖКАМИ. Первая версия складывала ВСЕХ получателей лампортов
и называла это чаевыми -- туда попадал сам пул, и выходило 2.5 SOL "чаевых" на
кошелёк и проценты в 10^14. Издержки -- это только ПОСТОЯННЫЕ получатели: те,
кто попадается не реже чем в четверти транзакций кошелька за сутки (чаевые
отправителей, сбор площадки). Разовый адрес -- это пул или наш новый счёт, и
издержкой он не является.

Только чтение. Ни подписей, ни отправок. Ключ не печатается.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ЛАМПОРТОВ_В_SOL = 1_000_000_000
МАДРИД = ZoneInfo("Europe/Madrid")
ПАКЕТ = 25
SOL_МИНТ = "So11111111111111111111111111111111111111112"
_URL = re.compile(r"(?i)\b(?:https?|wss?)://\S+")
_КЛЮЧ = re.compile(r"(?i)(api[-_]?key|api[-_]?token|bearer)\s*[=:]\s*\S+")


def чисто(x) -> str:
    s = _КЛЮЧ.sub("<klyuch>", _URL.sub("<url>", str(x)))
    return s


def сутки(день: str) -> tuple:
    """(начало, конец) суток по Мадриду в секундах UTC."""
    д = datetime.strptime(день, "%Y-%m-%d").replace(tzinfo=МАДРИД)
    return д.timestamp(), (д + timedelta(days=1)).timestamp()


def записи_журнала(путь: str):
    """Записи follow_trades по одной: файл на 31 МБ в память не читаем."""
    глуб, буф = 0, []
    with open(путь, encoding="utf-8") as ф:
        ф.readline()
        строка = ф.readline()
        while строка:
            if глуб == 0:
                if re.match(r'\s*"[^"]+":\s*\{', строка):
                    глуб, буф = 1, ["{"]
                строка = ф.readline()
                continue
            буф.append(строка)
            глуб += строка.count("{") - строка.count("}")
            if глуб <= 0:
                try:
                    з = json.loads("".join(буф).rstrip().rstrip(","))
                except ValueError:
                    з = None
                if з:
                    yield з.get("record") or {}
                глуб, буф = 0, []
            строка = ф.readline()


class Узел:
    def __init__(self, url: str):
        self.url = url
        self.вызовов = 0

    def пакет(self, запросы: list) -> list:
        тело = json.dumps(запросы).encode("utf-8")
        пауза = 0.4
        for попытка in range(6):
            зап = urllib.request.Request(self.url, data=тело,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(зап, timeout=45) as отв:
                    self.вызовов += len(запросы)
                    д = json.loads(отв.read().decode("utf-8"))
                    return д if isinstance(д, list) else [д]
            except urllib.error.HTTPError as ош:
                if ош.code != 429 or попытка == 5:
                    raise RuntimeError(f"HTTP {ош.code}: {чисто(ош)[:100]}") from None
            except Exception as ош:  # noqa: BLE001
                if попытка == 5:
                    raise RuntimeError(чисто(ош)[:140]) from None
            time.sleep(пауза)
            пауза *= 2
        return []

    def один(self, метод: str, параметры: list):
        о = self.пакет([{"jsonrpc": "2.0", "id": 1, "method": метод, "params": параметры}])
        return (о[0] or {}).get("result") if о else None


def подписи_за_окно(узел: Узел, адрес: str, с: float, по: float) -> list:
    """Подписи кошелька в окне: страницами по 1000, назад по времени."""
    собрано, до = [], None
    for _ in range(40):
        пар = {"limit": 1000}
        if до:
            пар["before"] = до
        рез = узел.один("getSignaturesForAddress", [адрес, пар]) or []
        if not рез:
            break
        for з in рез:
            т = з.get("blockTime")
            if т is None:
                continue
            if с <= т < по:
                собрано.append(з["signature"])
        до = рез[-1]["signature"]
        последний = рез[-1].get("blockTime")
        if последний is not None and последний < с:
            break
    return собрано


def разбор_tx(tx: dict, кошелёк: str) -> dict:
    """Нативная дельта кошелька, тариф, получатели и движение минтов."""
    из_ = {"delta": 0.0, "fee": 0.0, "err": None, "получатели": {},
           "минты": {}, "slot": None, "t": None}
    if not tx:
        return из_
    мета = tx.get("meta") or {}
    из_["err"] = мета.get("err")
    из_["fee"] = (мета.get("fee") or 0) / ЛАМПОРТОВ_В_SOL
    из_["slot"] = tx.get("slot")
    из_["t"] = tx.get("blockTime")
    ключи = (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or [])
    адреса = [(к.get("pubkey") if isinstance(к, dict) else к) for к in ключи]
    до, после = мета.get("preBalances") or [], мета.get("postBalances") or []
    for и, адрес in enumerate(адреса):
        if и >= len(до) or и >= len(после):
            break
        д = (после[и] - до[и]) / ЛАМПОРТОВ_В_SOL
        if адрес == кошелёк:
            из_["delta"] += д
        elif д > 0:
            из_["получатели"][адрес] = round(из_["получатели"].get(адрес, 0.0) + д, 9)
    # Движение токенов ТОЛЬКО по нашим счетам.
    было = {}
    for з in (мета.get("preTokenBalances") or []):
        if з.get("owner") == кошелёк:
            было[(з.get("mint"), з.get("accountIndex"))] = float(
                ((з.get("uiTokenAmount") or {}).get("amount")) or 0)
    стало = {}
    for з in (мета.get("postTokenBalances") or []):
        if з.get("owner") == кошелёк:
            стало[(з.get("mint"), з.get("accountIndex"))] = float(
                ((з.get("uiTokenAmount") or {}).get("amount")) or 0)
    for ключ in set(было) | set(стало):
        минт = ключ[0]
        if not минт or минт == SOL_МИНТ:
            continue
        д = стало.get(ключ, 0.0) - было.get(ключ, 0.0)
        if д:
            из_["минты"][минт] = из_["минты"].get(минт, 0.0) + д
        # Остаток на конец транзакции -- для признака "закрыта".
        из_.setdefault("остаток", {})[минт] = стало.get(ключ, 0.0)
    return из_


def главное() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--follow-file", default="data/dbot_follow_trades_raw.json")
    р.add_argument("--sdelki", default="data/podbivka/nashi_sdelki_host.json")
    р.add_argument("--days", default="2026-09-25,2026-09-26")
    р.add_argument("--configs", default="BATCH-3,BATCH-5")
    р.add_argument("--out", default="data/dbot_dni.json")
    а = р.parse_args()

    ключ = (os.environ.get("HELIUS_API_KEY") or "").strip()
    if not ключ:
        print("СТОП: HELIUS_API_KEY не задан", file=sys.stderr)
        return 2
    узел = Узел(f"https://mainnet.helius-rpc.com/?api-key={ключ}")
    дни = [д.strip() for д in а.days.split(",") if д.strip()]
    конфиги = {к.strip() for к in а.configs.split(",") if к.strip()}
    окна = {д: сутки(д) for д in дни}

    # --- ЖУРНАЛ DBOT: кошельки, комиссия площадки, источники.
    кошельки: dict = {}
    сборы: dict = {}
    источники: set = set()
    покупок_журнал: dict = {}
    for р_ in записи_журнала(а.follow_file):
        имя = р_.get("configName")
        if имя not in конфиги:
            continue
        к = р_.get("wallet")
        if к:
            кошельки.setdefault(к, set()).add(имя)
        ист = ((р_.get("follow") or {}).get("wallet"))
        if ист:
            источники.add(ист)
        if р_.get("state") != "done":
            continue
        т = (р_.get("createAt") or 0) / 1000.0
        for д, (с, по) in окна.items():
            if с <= т < по:
                сборы[(д, к)] = round(сборы.get((д, к), 0.0)
                                       + float(р_.get("dbotFee") or 0) / ЛАМПОРТОВ_В_SOL, 9)
                покупок_журнал[(д, к)] = покупок_журнал.get((д, к), 0) + 1

    таблица = []
    for кошелёк in sorted(кошельки):
        for д in дни:
            с, по = окна[д]
            подписи = подписи_за_окно(узел, кошелёк, с, по)
            txs = {}
            for н in range(0, len(подписи), ПАКЕТ):
                куски = подписи[н:н + ПАКЕТ]
                зап = [{"jsonrpc": "2.0", "id": и, "method": "getTransaction",
                        "params": [сг, {"encoding": "jsonParsed",
                                        "maxSupportedTransactionVersion": 1}]}
                       for и, сг in enumerate(куски)]
                for о in узел.пакет(зап):
                    и = о.get("id")
                    if isinstance(и, int) and и < len(куски):
                        txs[куски[и]] = о.get("result")
            разборы = []
            for сг in подписи:
                р2 = разбор_tx(txs.get(сг) or {}, кошелёк)
                if р2.get("t") is None:
                    continue
                р2["sig"] = сг
                разборы.append(р2)
            разборы.sort(key=lambda з: (з["t"], з["slot"] or 0))

            # ПОСТОЯННЫЕ ПОЛУЧАТЕЛИ -- это и есть издержки: чаевые и сбор
            # площадки повторяются на каждой сделке, пул у каждой свой.
            частота: dict = {}
            for р2 in разборы:
                for адрес in (р2["получатели"] or {}):
                    частота[адрес] = частота.get(адрес, 0) + 1
            порог_частоты = max(3, len(разборы) // 4)
            постоянные = {а_ for а_, н_ in частота.items() if н_ >= порог_частоты}

            по_минту: dict = {}
            тариф = 0.0
            дельта_всего = 0.0
            for р2 in разборы:
                тариф += р2["fee"]
                дельта_всего += р2["delta"]
                for минт, д_ток in (р2["минты"] or {}).items():
                    з = по_минту.setdefault(минт, {"delta": 0.0, "куплено": 0.0,
                                                   "продано": 0.0, "остаток": 0.0,
                                                   "tx": 0, "вход": 0.0,
                                                   "сборы": 0.0, "тариф": 0.0})
                    з["tx"] += 1
                    з["delta"] += р2["delta"]
                    з["тариф"] += р2["fee"]
                    if д_ток > 0:
                        з["куплено"] += д_ток
                        # ВХОД: сколько ушло с кошелька на покупке. Вычитать
                        # отсюда получателей нельзя -- среди них сам пул.
                        з["вход"] += max(0.0, -р2["delta"])
                    else:
                        з["продано"] += -д_ток
                    з["остаток"] = (р2.get("остаток") or {}).get(минт, з["остаток"])

            закрытые = []
            for минт, з in по_минту.items():
                if з["куплено"] <= 0:
                    continue
                if з["остаток"] > 0:
                    continue
                итог = з["delta"]
                процент = (итог / з["вход"] * 100.0) if з["вход"] > 0 else None
                закрытые.append({"mint": минт, "itog_sol": round(итог, 9),
                                 "vhod_sol": round(з["вход"], 9),
                                 "itog_pct": (round(процент, 2) if процент is not None
                                              else None),
                                 "tx": з["tx"]})
            # ВРЕМЯ ПЕРВОЙ ТРАНЗАКЦИИ ПО МИНТУ -- по нему потом ищется, что
            # делал наш детектор на том же сигнале.
            когда_минт = {}
            for р2 in разборы:
                for минт in (р2["минты"] or {}):
                    если_есть = когда_минт.get(минт)
                    if если_есть is None or (р2["t"] or 0) < если_есть:
                        когда_минт[минт] = р2["t"]
            for з in закрытые:
                т = когда_минт.get(з["mint"])
                з["utc"] = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(т))
                            if т else None)
                з["ts"] = т
            закрытые.sort(key=lambda з: з["itog_sol"])
            n = len(закрытые)
            сумма = sum(з["itog_sol"] for з in закрытые)
            проценты = [з["itog_pct"] for з in закрытые if з["itog_pct"] is not None]
            # ИЗМЕНЕНИЕ БАЛАНСА ЗА СУТКИ -- по цепи: сумма нативных дельт всех
            # транзакций кошелька в окне. Это честнее разницы балансов: она
            # включила бы переводы, которых в окне могло и не быть.
            таблица.append({
                "wallet": кошелёк,
                "configs": sorted(кошельки[кошелёк]),
                "day_madrid": д,
                "tx_v_okne": len(разборы),
                "izmenenie_balansa_sol": round(дельта_всего, 9),
                "zakrytyh_sdelok": n,
                "sredniy_itog_sol": (round(сумма / n, 9) if n else None),
                "sredniy_itog_pct": (round(sum(проценты) / len(проценты), 2)
                                     if проценты else None),
                "summa_itogov_sol": round(сумма, 9),
                "luchshaya": (закрытые[-1] if n else None),
                "hudshaya": (закрытые[0] if n else None),
                # ВСЕ закрытые сделки списком: по ним сверяется, что делал наш
                # детектор на тех же сигналах (задание владельца 26.09, 21:50).
                "sdelki": закрытые,
                "tarif_seti_sol": round(тариф, 9),
                "chaevye_i_sbory_sol": round(
                    sum(v for р2 in разборы
                        for а_, v in (р2["получатели"] or {}).items()
                        if а_ in постоянные), 9),
                "postoyannyh_poluchateley": len(постоянные),
                "porog_chastoty": порог_частоты,
                "komissiya_dbot_sol": сборы.get((д, кошелёк), 0.0),
                "pokupok_v_zhurnale_dbot": покупок_журнал.get((д, кошелёк), 0),
                "otkrytyh_na_konec": sum(1 for з in по_минту.values()
                                         if з["остаток"] > 0),
            })

    # --- НАША ПОЛОСА ПО ТЕМ ЖЕ ИСТОЧНИКАМ.
    полоса = {"file": а.sdelki, "sdelok": 0, "itog_sol": 0.0,
              "istochnikov_dbot": len(источники), "why_not": None}
    try:
        с_наши = json.load(open(а.sdelki, encoding="utf-8"))["sdelki"]
    except Exception as exc:  # noqa: BLE001
        полоса["why_not"] = f"{type(exc).__name__}: {чисто(exc)[:80]}"
        с_наши = []
    с26, по26 = окна.get("2026-09-26", (0, 0))
    свои = []
    for x in с_наши:
        if x.get("side") != "lane" or x.get("group") != "bloom_lane":
            continue
        if x.get("source") not in источники:
            continue
        т = x.get("ts_intent_utc") or ""
        try:
            тс = datetime.strptime(т, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=ZoneInfo("UTC")).timestamp()
        except ValueError:
            continue
        if с26 <= тс < по26:
            свои.append(x)
    полоса["sdelok"] = len(свои)
    полоса["itog_sol"] = round(sum(x.get("pnl_sol") or 0.0 for x in свои), 9)
    полоса["s_itogom"] = sum(1 for x in свои if x.get("pnl_sol") is not None)
    полоса["vhod_sol"] = round(sum(x.get("in_sol") or 0.0 for x in свои), 6)

    итог = {"snyato_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "configs": sorted(конфиги), "days_madrid": дни,
            "koshelkov": len(кошельки), "tablica": таблица,
            "polosa_po_tem_zhe_istochnikam_26_09": полоса,
            "vyzovov_uzla": узел.вызовов}
    with open(а.out, "w", encoding="utf-8") as ф:
        json.dump(итог, ф, ensure_ascii=False, indent=1)

    # Печать таблицей, без выводов.
    ш = ("кошелёк", "конфиг", "сутки", "баланс, SOL", "закрытых", "средний, SOL",
         "средний, %", "лучшая", "худшая", "тариф", "чаевые+сборы", "DBot")
    print(" | ".join(ш))
    for р2 in таблица:
        print(" | ".join(str(x) for x in (
            р2["wallet"][:6] + ".." + р2["wallet"][-4:],
            ",".join(р2["configs"]), р2["day_madrid"],
            р2["izmenenie_balansa_sol"], р2["zakrytyh_sdelok"],
            р2["sredniy_itog_sol"], р2["sredniy_itog_pct"],
            (р2["luchshaya"] or {}).get("itog_sol"),
            (р2["hudshaya"] or {}).get("itog_sol"),
            р2["tarif_seti_sol"], р2["chaevye_i_sbory_sol"],
            р2["komissiya_dbot_sol"])))
    print(json.dumps({"polosa_po_tem_zhe_istochnikam_26_09": полоса,
                      "vyzovov_uzla": узел.вызовов}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(главное())
