#!/usr/bin/env python3
"""Выгрузка сделок полосы с ПОЛНЫМИ минтами и подписями (только чтение).

ЗАЧЕМ. В таблице полосы (data/lane_table.json) минты и подписи урезаны до
десяти-шестнадцати знаков -- по ним в цепь не сходишь. Замер толпы за источником
требует полных значений, поэтому они берутся из журнала позиций на хосте.

Журнал читается ПОТОКОМ, включая ротированные .gz.

СЕТЬ. По умолчанию выгрузка ДОЗАПОЛНЯЕТ наше место в блоке там, где в записи
позиции его нет: два вызова только для чтения (getSignatureStatuses пакетом на
все сделки и getBlock уровня signatures на слот посадки). Источник тот же, что у
строки владельца «мы: S+N, место» -- севшая подпись и блок её слота, тот же
потолок версии транзакции. Отключается --bez-seti. Ключей в файл не попадает.
"""
from __future__ import annotations

import argparse
import calendar
import glob
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request

МЕТКА_ПОЛОСЫ = "own_send"


def метка(текст: str) -> float:
    т = текст.replace("Z", "").replace("z", "")
    for формат in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return calendar.timegm(time.strptime(т, формат))
        except ValueError:
            continue
    raise SystemExit(f"СТОП: метку {текст!r} не разобрать")


def строки(путь: str):
    откр = gzip.open if путь.endswith(".gz") else open
    try:
        with откр(путь, "rt", encoding="utf-8", errors="replace") as ф:
            for с in ф:
                с = с.strip()
                if с:
                    yield с
    except OSError as exc:
        print(f"ПРЕДУПРЕЖДЕНИЕ: {путь} ({type(exc).__name__})", file=sys.stderr)


def файлы(каталог: str) -> list:
    из_ = sorted(glob.glob(os.path.join(каталог, "positions.jsonl*")))
    осн = os.path.join(каталог, "positions.jsonl")
    if осн in из_:
        из_.remove(осн)
        из_.append(осн)
    return из_


def удержание_в_слотах(п: dict):
    """Слотов от посадки покупки до посадки продажи -- оба слота от цепи."""
    покупка = (п.get("lane_landed_slot") or п.get("block_slot")
               or п.get("own_tx_seen_slot") or п.get("our_slot"))
    исход = п.get("last_sell_outcome") or {}
    продажа = исход.get("slot") if isinstance(исход, dict) else None
    if not isinstance(покупка, int) or not isinstance(продажа, int):
        return None
    return (продажа - покупка) if продажа >= покупка else None


def s_plus(ряд: dict) -> dict:
    """S+N: наш слот посадки минус слот источника. {s_plus, why_not}.

    Слово владельца 29.09 ночью (п.2): метрика "S+0 до/после региональной
    отправки по не-EU лидерам" пишется в экспорт. Без этого столбца её не
    восстановить по выгрузке вовсе.

    Считается ровно та же разность, что в deploy/checks/pravilo11_po_cepi.py:
    посадка нашей покупки минус посадка источника. our_slot НЕ используется: в
    выгрузке 29.09 он пуст у всех девяти сделок, и по нему вышло бы "нет" там,
    где число есть. Нет одного из слотов -- None и причина словами.
    """
    наш = (ряд or {}).get("landed_slot")
    ист = (ряд or {}).get("source_slot")
    if not isinstance(наш, int) or наш <= 0:
        return {"s_plus": None, "why_not": "слота нашей посадки нет"}
    if not isinstance(ист, int) or ист <= 0:
        return {"s_plus": None, "why_not": "слота источника нет"}
    return {"s_plus": int(наш) - int(ист), "why_not": None}


def карта_лидеров(путь: str) -> dict:
    """Карта "личность лидера -> регион" из data/leader_regions.json.

    Возвращает {по_лидеру, эпоха, начало_эпохи, слотов_в_эпохе, why_not}.
    Карты нет или в ней нет эпохи -- отказ словами: region у сделок останется
    пустым с причиной, а не догадкой.
    """
    if not путь or not os.path.exists(путь):
        return {"по_лидеру": {}, "why_not": f"карты регионов нет: {путь}"}
    try:
        with open(путь, encoding="utf-8") as ф:
            д = json.load(ф)
    except Exception as e:  # noqa: BLE001
        return {"по_лидеру": {}, "why_not": f"карта не читается: {type(e).__name__}"}
    по_лидеру = д.get("по_лидеру") or {}
    нач, длина = д.get("начало_эпохи"), д.get("слотов_в_эпохе")
    if not по_лидеру:
        return {"по_лидеру": {}, "why_not": "в карте нет по_лидеру"}
    if not isinstance(нач, int) or not isinstance(длина, int):
        return {"по_лидеру": по_лидеру,
                "why_not": "в карте нет границ эпохи -- слот к лидеру не привязать"}
    return {"по_лидеру": по_лидеру, "эпоха": д.get("эпоха"),
            "начало_эпохи": нач, "слотов_в_эпохе": длина,
            "собрано_utc": д.get("собрано_utc"), "why_not": None}


def лидер_слота(слот, расписание: dict, начало: int):
    """Личность лидера слота по расписанию эпохи. {лидер, why_not}.

    В getLeaderSchedule ключ -- личность, значение -- ИНДЕКСЫ слотов от начала
    эпохи. Разворачиваем в индекс -> личность один раз на прогон.
    """
    if not isinstance(слот, int):
        return {"лидер": None, "why_not": "слота нет"}
    и = слот - начало
    if и < 0 or и not in расписание:
        return {"лидер": None, "why_not": "слот вне расписания этой эпохи"}
    return {"лидер": расписание[и], "why_not": None}


def удержание_в_секундах(п: dict):
    """Секунд от отправки покупки до отправки продажи. То же, что в строке SELL."""
    начало = п.get("ts_sent") or п.get("ts_accepted") or п.get("ts_intent")
    конец = (п.get("ts_last_sell_attempt") or п.get("ts_jup_attempt")
             or п.get("sell_landed_ts"))
    if not isinstance(начало, (int, float)) or not isinstance(конец, (int, float)):
        return None
    return round(max(0.0, float(конец) - float(начало)), 3)


def наши_подписи(п: dict) -> list:
    """Все подписи, под которыми наша покупка могла сесть: вариант на nonce даёт
    по подписи на отправителя, а сядет ровно одна."""
    из_ = []
    for поле in ("lane_signature", "lane_signature_local"):
        зн = п.get(поле)
        if isinstance(зн, str) and зн:
            из_.append(зн)
    for зн in (п.get("lane_pool_candidates") or []):
        if isinstance(зн, str) and зн:
            из_.append(зн)
    for зн in (п.get("signatures") or []):
        if isinstance(зн, str) and зн:
            из_.append(зн)
    видели, ответ = set(), []
    for зн in из_:
        if зн not in видели:
            видели.add(зн)
            ответ.append(зн)
    return ответ


def код_ошибки(err) -> tuple:
    """(короткий код, вид словами) по ошибке цепи. ("", "села без ошибки") -- ok.

    ЗАЧЕМ ЗДЕСЬ, А НЕ ИМПОРТОМ. Это ТО ЖЕ правило, что в
    analysis/nesevshie_po_cepi.код_ошибки (6040 и 6001 -- отказ пула по
    минимуму выхода, то есть по допуску). Выгрузка уезжает на хост одним
    файлом и из дерева ничего не импортирует, поэтому правило повторено
    здесь, а самопроверка ниже держит оба куска в согласии по кодам.

    ЗАЧЕМ ВООБЩЕ (слово владельца 09.10, п.4): ежедневная выгрузка должна
    везти не только севшие сделки, но и ОТКАЗЫ ПО ДОПУСКУ с подписью упавшей
    транзакции -- иначе Code-2 сверяет модель только по тем сделкам, которые
    прошли, и наценка входа у него всегда выглядит проходимой.
    """
    if err is None:
        return "", "села без ошибки"
    текст = json.dumps(err, ensure_ascii=False)
    if "InstructionError" in текст:
        try:
            подробно = err["InstructionError"][1]
        except Exception:  # noqa: BLE001
            подробно = текст
        код = (json.dumps(подробно, ensure_ascii=False)
               if not isinstance(подробно, str) else подробно)
        вид = ("проскальзывание или минимум пула"
               if "6040" in код or "6001" in код else "ошибка инструкции")
        return код[:60], вид
    if "BlockhashNotFound" in текст:
        return "BlockhashNotFound", "blockhash"
    if "Nonce" in текст:
        return текст[:60], "nonce"
    if "AlreadyProcessed" in текст:
        return "AlreadyProcessed", "дубль подписи"
    return текст[:60], "другое"


def отказ_ряда(п: dict) -> dict:
    """Отказ покупки по записи позиции: код, вид, по допуску ли, чья подпись.

    ПОДПИСЬ УПАВШЕЙ -- ТА, ЧТО СЕЛА И ОТКАТИЛАСЬ (lane_landed_signature), а
    при её отсутствии -- подпись отправленного варианта. Пустая подпись при
    ошибке называется словами: по "" в цепь не сходишь.
    """
    err = п.get("chain_err") or ((п.get("meta") or {}).get("err"))
    код, вид = код_ошибки(err)
    подпись = (п.get("lane_landed_signature") or п.get("lane_signature")
               or п.get("lane_signature_local") or None)
    return {
        "chain_err": err,
        "otkaz_kod": код or None,
        "otkaz_vid": вид if err is not None else None,
        "otkaz_po_dopusku": (вид == "проскальзывание или минимум пула"
                             if err is not None else None),
        "otkaz_podpis": подпись if err is not None else None,
        "otkaz_podpis_why_not": ("подписи упавшей транзакции в записи нет"
                                 if err is not None and not подпись else None),
    }


# ПУТЬ ПРОДАЖИ -- ПО ПОЛЮ, КОТОРОЕ СТАВИТ САМ ПРОДАВЕЦ. Выбор пути делает
# сторож и записывает его в sell_address_kind рядом с подписью (bloom_seller:
# 4122 свой_в_пул, 4868 jupiter, 3615 two_step). Выводить путь из набора
# программ сделки было бы вторым источником правды и стоило бы чтения цепи на
# каждую сделку; поле ставит тот, кто решал, и оно дешевле и точнее.
ПУТЬ_ПРОДАЖИ_ПО_ВИДУ = {
    "свой_в_пул": "наш",          # своя одна нога в пул (c3_prodavec_sborka)
    "two_step": "наш",            # своя двухшаговая продажа
    "jupiter": "Jupiter",
    "mint": "Jupiter",            # прежние виды адреса попытки Jupiter
    "pool": "Jupiter",
}
WHY_НЕТ_ПУТИ_ПРОДАЖИ = ("вида адреса продажи в записи нет -- позиция не "
                         "продавалась или продана до появления поля")


def put_prodazhi(п: dict) -> dict:
    """Чем продана позиция: наш путь или Jupiter. Незнание называется словами.

    Отдаётся И сырой вид адреса, И его перевод в два слова: по переводу считают
    доли, а по сырому виду видно, какой именно наш путь сработал (одна нога в
    пул или двухшаговая) -- склеивать их в одно слово значит терять замер.
    """
    вид = (п.get("sell_address_kind") or "").strip() or None
    виды = [в for в in (п.get("sell_address_kinds") or "").split(",") if в]
    путь = ПУТЬ_ПРОДАЖИ_ПО_ВИДУ.get(вид or "")
    из_ = {"put_prodazhi": путь,
            "put_prodazhi_vid": вид,
            "put_prodazhi_vidy_popytok": (",".join(виды) or None),
            "put_prodazhi_why_not": None}
    if вид and путь is None:
        # НОВЫЙ ВИД АДРЕСА -- НЕ «Jupiter ПО УМОЛЧАНИЮ». Молчаливое отнесение
        # незнакомого вида к одной из сторон испортило бы долю на следующей же
        # правке продавца, и заметить это было бы нечем.
        из_["put_prodazhi_why_not"] = (
            f"вид адреса продажи {вид!r} выгрузке не известен -- "
            "к «нашему» и к «Jupiter» он не отнесён")
    elif not вид:
        из_["put_prodazhi_why_not"] = WHY_НЕТ_ПУТИ_ПРОДАЖИ
    return из_


def vhod_vyhod_po_cepi(п: dict) -> dict:
    """Вход и выход сделки В SOL ПО ЦЕПИ -- из частей, которые уже посчитаны.

    ОТКУДА ЧИСЛА. Итог по цепи (c2_itog_po_cepi.итог_по_двум) складывает
    изменения ВСЕХ наших счетов по двум подписям и кладёт в запись слагаемые по
    ногам: части["покупка"]["все_sol"] и части["продажа"]["все_sol"]. Отдельных
    чтений цепи здесь не делается ВОВСЕ -- иначе у одного числа было бы два
    источника, и расхождение между ними никто бы не искал.

    ЗНАКИ. На цепи покупка -- отрицательная дельта, продажа -- положительная.
    Наружу отдаются ПОЛОЖИТЕЛЬНЫЕ "ушло" и "вернулось": именно их просил
    владелец, и по ним Code-2 считает валовой итог сам.

    ЛИКВИДНОЕ -- ОТДЕЛЬНОЙ ПАРОЙ. все_sol включает ренту токеновых счетов: она
    наша, но до закрытия счёта её не потратить. Две пары рядом дают увидеть
    запертую ренту, а одна пара её спрятала бы.
    """
    из_ = {"vhod_sol_po_cepi": None, "vyhod_sol_po_cepi": None,
            "vhod_sol_likvid_po_cepi": None, "vyhod_sol_likvid_po_cepi": None,
            "vhod_vyhod_why_not": None}
    ч = п.get("lane_chain_pnl_parts")
    if not isinstance(ч, dict) or not ч:
        из_["vhod_vyhod_why_not"] = (
            п.get("lane_chain_pnl_why_not")
            or "слагаемых итога по цепи в записи нет -- вход и выход не считаем")
        return из_
    нет = []
    for роль, куда, знак in (("покупка", "vhod", -1), ("продажа", "vyhod", 1)):
        нога = ч.get(роль)
        if not isinstance(нога, dict):
            нет.append(роль)
            continue
        for ключ, имя in (("все_sol", f"{куда}_sol_po_cepi"),
                           ("кошелёк_sol", f"{куда}_sol_likvid_po_cepi")):
            зн = нога.get(ключ)
            if isinstance(зн, (int, float)):
                из_[имя] = round(знак * float(зн), 9)
    if нет:
        из_["vhod_vyhod_why_not"] = (
            f"в слагаемых итога по цепи нет ноги: {', '.join(нет)}")
    return из_


def из_решений(state_dir: str, cids: set, *, с_ts: float = 0.0) -> dict:
    """Налог токена и комиссия пула по журналу решений: cid -> два числа.

    ЗАЧЕМ ОТДЕЛЬНЫМ ПРОХОДОМ. В записи позиции этих чисел нет: налог маршрута
    считает разбор сигнала (route_tax.token_fee_bps), а комиссию пула --
    сборка полосы (pool_fee_share). Владелец просил их в выгрузке ОТДЕЛЬНЫМИ
    полями (28.09, п.4б), в решение они не идут.

    Журнал решений на хосте идёт на сотни мегабайт, поэтому фильтр дешёвый:
    строка читается целиком только если в ней вообще есть cid полосы.
    """
    из_: dict = {}
    if not cids:
        return из_
    for путь in sorted(glob.glob(os.path.join(state_dir, "decisions.jsonl*"))):
        # РОТИРОВАННЫЕ ФАЙЛЫ СТАРШЕ ОКНА НЕ ЧИТАЕМ. Журнал решений идёт на
        # сотни мегабайт, и разжать весь архив ради суточной выгрузки -- это
        # минуты процессора на хосте, который в это время торгует.
        try:
            if с_ts and os.path.getmtime(путь) < с_ts - 86400:
                continue
        except OSError:
            pass
        for с in строки(путь):
            if "lane-own-" not in с:
                continue
            try:
                з = json.loads(с)
            except ValueError:
                continue
            if not isinstance(з, dict):
                continue
            cid = з.get("cid") or з.get("client_order_id")
            if not cid or cid not in cids:
                continue
            в = из_.setdefault(cid, {})
            нал = з.get("route_tax") or {}
            if isinstance(нал, dict) and нал.get("token_fee_bps") is not None:
                в["nalog_tokena_bps"] = нал["token_fee_bps"]
            # FREEZE AUTHORITY -- В ВЫГРУЗКУ. Полоса его перед покупкой не
            # проверяет (ответ на вопрос владельца 28.09, п.4б); число попадает
            # в журнал решений тем же чтением счёта минта, что и налог.
            if isinstance(нал, dict) and "freeze_authority" in нал:
                в["freeze_authority"] = нал.get("freeze_authority")
                в["freeze_authority_otozvan"] = нал.get("freeze_authority_revoked")
            if isinstance(нал, dict) and нал.get("route_transfer_fee_bps") is not None:
                в["nalog_marshruta_bps"] = нал["route_transfer_fee_bps"]
            if з.get("pool_fee_share") is not None:
                в["komissiya_pula_pct"] = round(float(з["pool_fee_share"]) * 100, 4)
            # РЕЗЕРВ ПУЛА И БИЛЕТ -- В ВЫГРУЗКУ (та самая строка, которую назвал
            # Code-3 в docs/bilet_ot_rezerva_kak_vstroit.md, п.3: без неё замер
            # билета по НАШИМ сделкам считать нечем -- резерва в выгрузке не было
            # ни у одного ряда из 79, и это свойство выгрузки, а не пулов).
            if з.get("pool_reserve_sol") is not None:
                в["pool_reserve_sol"] = з["pool_reserve_sol"]
                в["pool_reserve_kind"] = з.get("pool_reserve_kind")
            if з.get("pool_reserve_sol_eq") is not None and в.get("pool_reserve_sol") is None:
                в["pool_reserve_sol"] = з["pool_reserve_sol_eq"]
                в["pool_reserve_kind"] = з.get("pool_reserve_kind") or "двухшаговый"
            for поле_б in ("bilet_sol", "bilet_dolya", "bilet_ot_rezerva",
                            "bilet_rezerv_sol", "bilet_rezerv_otkuda",
                            "bilet_rezerv_vid", "bilet_rezerv_why_not"):
                if з.get(поле_б) is not None:
                    в[поле_б] = з[поле_б]
            # СТРОИТЕЛЬ -- ВТОРЫМ ИСТОЧНИКОМ (слово владельца 29.09, п.5: в
            # sdelki_polosy строитель должен быть заполнен везде). В записи
            # позиции он лежит в program, но у сделок до правки брони поля там
            # нет вовсе -- тогда берём программу пула из строки решения.
            for поле_ж in ("pool_program", "program", "тип_пула"):
                if з.get(поле_ж) and not в.get("stroitel"):
                    в["stroitel"] = з[поле_ж]
                    в["stroitel_otkuda"] = f"журнал решений: {поле_ж}"
    return из_


class Узел:
    """Минимальный клиент JSON-RPC: только чтение, только два метода."""

    ПОТОЛОК_ВЕРСИИ = int(os.environ.get("BLOOM_MAX_TX_VERSION") or 1)

    def __init__(self, ключ: str) -> None:
        self.url = f"https://mainnet.helius-rpc.com/?api-key={ключ}"
        self.вызовов = 0

    def зов(self, метод: str, параметры: list) -> dict:
        тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                            "params": параметры}).encode()
        зап = urllib.request.Request(
            self.url, data=тело, headers={"Content-Type": "application/json"})
        self.вызовов += 1
        with urllib.request.urlopen(зап, timeout=30) as о:
            ответ = json.loads(о.read().decode())
        if "error" in ответ:
            raise RuntimeError(f"{метод}: {str(ответ['error'])[:160]}")
        return ответ.get("result") or {}

    def севшие(self, подписи: list) -> list:
        return (self.зов("getSignatureStatuses",
                          [подписи[:256], {"searchTransactionHistory": True}])
                or {}).get("value") or []

    def подписи_адреса(self, адрес: str, *, предел: int = 200,
                        до: str | None = None) -> list:
        п: list = [адрес, {"limit": int(предел)}]
        if до:
            п[1]["before"] = до
        return self.зов("getSignaturesForAddress", п) or []

    def транзакция(self, подпись: str) -> dict:
        return self.зов("getTransaction", [подпись, {
            "encoding": "jsonParsed", "commitment": "confirmed",
            "maxSupportedTransactionVersion": self.ПОТОЛОК_ВЕРСИИ}]) or {}

    def подписи_блока(self, слот: int) -> list:
        блок = self.зов("getBlock", [слот, {
            "encoding": "json", "transactionDetails": "signatures",
            "rewards": False,
            "maxSupportedTransactionVersion": self.ПОТОЛОК_ВЕРСИИ}]) or {}
        return блок.get("signatures") or []


WSOL_МИНТ = "So11111111111111111111111111111111111111112"


def кошелёк_полосы_из_ряда(ряд: dict) -> str:
    """Кошелёк полосы: из ряда, из записи позиции или из окружения службы."""
    for ключ in ("wallet", "lane_wallet"):
        зн = (ряд or {}).get(ключ)
        if зн:
            return зн
    зап = (ряд or {}).get("zapis") or {}
    for ключ in ("wallet", "lane_wallet", "owner"):
        if зап.get(ключ):
            return зап[ключ]
    return (os.environ.get("OWN_SEND_WALLET") or "").strip()


def хранилище_пула_из_покупки(узел, *, подпись_покупки: str, минт: str,
                               наш_кошелёк: str) -> dict:
    """Адрес хранилища пула по НАШЕЙ покупке: счёт нашего минта, чей владелец
    не мы. Именно на него потом смотрим, чтобы увидеть чужие продажи.

    Адреса пула в записи позиции нет, а угадывать его по программе нельзя.
    Зато в нашей же покупке хранилище видно точно: туда ушёл SOL и оттуда
    пришёл токен."""
    из_ = {"хранилище": None, "why_not": None, "слот_покупки": None}
    if not подпись_покупки or not минт:
        из_["why_not"] = "нет подписи покупки или минта"
        return из_
    try:
        tx = узел.транзакция(подпись_покупки)
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"покупка не прочитана: {type(exc).__name__}"
        return из_
    из_["слот_покупки"] = (tx or {}).get("slot")
    мета = (tx or {}).get("meta") or {}
    for б in (мета.get("postTokenBalances") or []):
        if б.get("mint") != минт:
            continue
        вл = б.get("owner")
        if вл and вл != наш_кошелёк:
            из_["хранилище"] = _счёт_по_месту(tx, б.get("accountIndex"))
            if из_["хранилище"]:
                return из_
    из_["why_not"] = "в покупке не нашлось счёта нашего минта с чужим владельцем"
    return из_


def _счёт_по_месту(tx: dict, место) -> str | None:
    """Адрес счёта по его месту в списке счетов транзакции (jsonParsed)."""
    if место is None:
        return None
    try:
        ключи = (((tx or {}).get("transaction") or {}).get("message") or {}).get(
            "accountKeys") or []
        зн = ключи[int(место)]
        return зн.get("pubkey") if isinstance(зн, dict) else зн
    except (IndexError, TypeError, ValueError):
        return None


def чужие_продажи_в_окне(узел, *, пул: str, минт: str, слот_покупки: int,
                          слот_продажи: int, наши_подписи_: set,
                          предел_подписей: int = 300) -> dict:
    """Сколько SOL ЧУЖИЕ продавцы вынули из пула, пока мы держали позицию.

    ЗАЧЕМ (слово владельца 29.09, п.3): "с первой сделки писать в экспорт:
    чужие продажи в окне нашего удержания / наш билет". Это мера давления: если
    пока мы держим, другие успели продать на несколько наших билетов, цена
    ушла не от нашей покупки, а от толпы, и винить в убытке проскальзывание
    нашей сделки нельзя.

    КАК СЧИТАЕТСЯ. По ПУЛУ, а не по минту: продажа -- это транзакция, где наш
    (некотировочный) минт ПРИШЁЛ в хранилище пула, а SOL из пула УШЁЛ. Берём
    её величину в SOL. Наши собственные подписи исключаются по списку -- иначе
    наша же продажа попала бы в "чужие".

    ОКНО -- ПО СЛОТАМ, включительно: от слота нашей покупки до слота нашей
    продажи. Транзакции вне окна не считаются вовсе.

    ТОЛЬКО ЧТЕНИЕ. Незнание не превращается в ноль: не прочитали -- так и
    сказано в why_not, а поля остаются None.
    """
    из_ = {"чужих_продаж": None, "чужие_продажи_sol": None,
            "просмотрено_транзакций": 0, "why_not": None,
            "окно_слотов": None, "окно_покрыто": None,
            "подписей_пула_прочитано": 0, "самая_старая_подпись_слот": None}
    if not пул or not минт or not слот_покупки or not слот_продажи:
        из_["why_not"] = "нет пула, минта или границ окна"
        return из_
    низ, верх = int(min(слот_покупки, слот_продажи)), int(max(слот_покупки, слот_продажи))
    из_["окно_слотов"] = верх - низ
    подписи: list = []
    до = None
    # ОКНО СЧИТАЕТСЯ ПОКРЫТЫМ ТОЛЬКО ТОГДА, КОГДА ОБХОД СПУСТИЛСЯ НИЖЕ ЕГО ДНА.
    # Обход идёт от НОВЫХ к старым и упирается в предел подписей. У активного
    # пула 300 свежих подписей покрывают минуты, а окно нашей сделки лежит
    # часами раньше -- тогда в окно не попадает НИ ОДНА подпись, и прежний код
    # всё равно писал "чужих продаж 0" и "чужие продажи 0.0 SOL". Это ноль не о
    # рынке, а о том, что мы досюда не дочитали: ровно поэтому в выгрузке
    # chuzhie_prosmotreno был 0 на всех сделках.
    окно_покрыто = False
    try:
        while len(подписи) < предел_подписей:
            пачка = узел.подписи_адреса(пул, предел=100, до=до)
            if not пачка:
                # Подписи кончились -- значит видели всю историю пула, включая окно.
                окно_покрыто = True
                break
            подписи.extend(пачка)
            до = пачка[-1].get("signature")
            # Дошли ниже окна -- дальше только старее, можно останавливаться.
            if int(пачка[-1].get("slot") or 0) < низ:
                окно_покрыто = True
                break
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"подписи пула не прочитаны: {type(exc).__name__}"
        return из_
    из_["подписей_пула_прочитано"] = len(подписи)
    из_["окно_покрыто"] = окно_покрыто
    if подписи:
        из_["самая_старая_подпись_слот"] = int(подписи[-1].get("slot") or 0)
    if not окно_покрыто:
        из_["why_not"] = (
            f"окно не покрыто: {len(подписи)} подписей пула при пределе "
            f"{предел_подписей} не дошли до слота покупки {низ} "
            f"(самая старая прочитанная -- слот {из_['самая_старая_подпись_слот']}); "
            "ноль чужих продаж здесь означал бы не рынок, а недочитанную историю")
        return из_
    в_окне = [з for з in подписи
               if низ <= int(з.get("slot") or -1) <= верх
               and not з.get("err")
               and з.get("signature") not in наши_подписи_]
    сумма = 0.0
    сколько = 0
    for з in в_окне:
        try:
            tx = узел.транзакция(з["signature"])
        except Exception:  # noqa: BLE001
            continue
        из_["просмотрено_транзакций"] += 1
        мета = (tx or {}).get("meta") or {}
        пред = {(б.get("accountIndex")): б for б in (мета.get("preTokenBalances") or [])}
        пост = {(б.get("accountIndex")): б for б in (мета.get("postTokenBalances") or [])}
        наш_пришёл = False
        sol_ушёл = 0.0
        for и, б in пост.items():
            до_ = пред.get(и) or {}
            было = float(((до_.get("uiTokenAmount") or {}).get("uiAmount")) or 0.0)
            стало = float(((б.get("uiTokenAmount") or {}).get("uiAmount")) or 0.0)
            if б.get("mint") == минт and стало > было:
                наш_пришёл = True
            if б.get("mint") == WSOL_МИНТ and стало < было:
                sol_ушёл += (было - стало)
        if наш_пришёл and sol_ушёл > 0:
            сумма += sol_ушёл
            сколько += 1
    из_.update(чужих_продаж=сколько, чужие_продажи_sol=round(сумма, 9))
    return из_


def место_в_блоке(подписи_блока: list, подпись: str) -> dict:
    """Место подписи в блоке. Молчание НЕ превращается в ноль: место 0 -- это
    первая транзакция блока, и путать её с «не знаем» нельзя."""
    из_ = {"index": None, "total": None, "share": None, "why_not": None}
    if not подписи_блока:
        из_["why_not"] = "в ответе getBlock нет подписей"
        return из_
    из_["total"] = len(подписи_блока)
    try:
        и = подписи_блока.index(подпись)
    except ValueError:
        из_["why_not"] = "нашей подписи в этом блоке нет"
        return из_
    из_.update(index=и, share=round(и / len(подписи_блока), 4))
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--s", default="2026-09-25T18:00", help="с какой метки UTC")
    р.add_argument("--do", default="", help="до какой метки UTC (пусто -- без предела)")
    р.add_argument("--polnye", action="store_true",
                   help="выложить ВСЕ поля записи позиции, а не отобранные "
                         "(слово владельца 28.09, п.5: выгрузка для Code-2)")
    р.add_argument("--out", default="")
    р.add_argument("--bez-seti", action="store_true",
                   help="не ходить в сеть: место в блоке останется пустым там, "
                         "где его нет в записи")
    р.add_argument("--karta-regionov", default="data/leader_regions.json",
                    help="карта личность лидера -> регион (только чтение)")
    # ПРЕДЕЛ ПОДПИСЕЙ -- ВХОДОМ, А НЕ КОНСТАНТОЙ. Обход идёт от новых к старым,
    # и для сделки, случившейся часы назад, 300 подписей активного пула до окна
    # не доходят: метрика честно отказывает "окно не покрыто". Чтобы посчитать
    # ретроспективно, предел нужно поднять НА ОДИН ПРОГОН и с названным числом --
    # поднимать его в коде навсегда значит жечь кредиты Helius на каждой выгрузке.
    р.add_argument("--predel-podpisey", type=int, default=300,
                   help="сколько подписей пула просмотреть назад (по умолчанию 300)")
    р.add_argument("--self-test", action="store_true")
    а = р.parse_args()
    if а.self_test:
        return самопроверка()
    с_ = метка(а.s)
    # ВЕРХНЯЯ ГРАНИЦА ОКНА. Суточная выгрузка просит ровно сутки, и без предела
    # сверху в файл за 27->28 попали бы и сделки следующего дня.
    до_ = метка(а.do) if а.do else None
    по_cid: dict = {}
    просмотрено = 0
    # ДВА ПРОХОДА, И ЭТО НЕ ЛИШНЕЕ. Дописки позиции (state=closed,
    # closed_sol_net, итог продажи) приходят ОТДЕЛЬНЫМИ строками, и в них нет
    # ни метки полосы, ни ts_intent -- только client_order_id и изменённые поля.
    # Один проход с фильтром по метке отбрасывал их, и в выгрузке 27.09 у всех
    # 152 сделок итог вышел пустым, хотя в журнале он есть. Первый проход
    # собирает cid наших сделок окна, второй -- все их поля.
    свои_cid: set = set()
    for путь in файлы(а.state_dir):
        for с in строки(путь):
            просмотрено += 1
            try:
                з = json.loads(с)
            except ValueError:
                continue
            if not isinstance(з, dict) or з.get("lane") != МЕТКА_ПОЛОСЫ:
                continue
            т = з.get("ts_intent") or з.get("ts_sent")
            if not isinstance(т, (int, float)) or float(т) < с_:
                continue
            if до_ is not None and float(т) >= до_:
                continue
            if з.get("client_order_id"):
                свои_cid.add(з["client_order_id"])
    for путь in файлы(а.state_dir):
        for с in строки(путь):
            try:
                з = json.loads(с)
            except ValueError:
                continue
            if not isinstance(з, dict):
                continue
            cid = з.get("client_order_id")
            if not cid or cid not in свои_cid:
                continue
            # Позиция дописывается много раз: накапливаем поля, а не заменяем --
            # часть полей приходит позже.
            в = по_cid.setdefault(cid, {})
            for к, зн in з.items():
                if зн is not None:
                    в[к] = зн
    ряды = []
    for cid, п in sorted(по_cid.items()):
        ряды.append({
            "cid": cid,
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                  time.gmtime(float(п.get("ts_intent") or 0))),
            "group": п.get("lane_group"),
            "mint": п.get("mint"),
            "size_sol": п.get("sol_in"),
            "source": п.get("source"),
            "source_sig": п.get("source_sig"),
            "source_slot": п.get("source_slot"),
            "our_slot": п.get("own_tx_seen_slot") or п.get("our_slot"),
            "our_signatures": наши_подписи(п),
            "our_block_index": п.get("block_index"),
            "source_block_index": п.get("source_block_index"),
            "wallet": п.get("lane_wallet"),
            "chain_ok": п.get("chain_ok"),
            # ОТКАЗ ПОКУПКИ -- РЯДОМ С САМОЙ СДЕЛКОЙ (слово владельца 09.10,
            # п.4). chain_ok=false говорит "не вышло", но не говорит ЧЕГО: по
            # допуску (минимум выхода не пропустил) или по чему-то другому.
            # Code-2 сверяет модель наценки входа, поэтому ему нужны и вид
            # отказа, и ПОДПИСЬ УПАВШЕЙ транзакции -- по ней он сходит в цепь.
            **отказ_ряда(п),
            # ПУТЬ ПРОДАЖИ И ВАЛОВЫЕ ВХОД/ВЫХОД ПО ЦЕПИ (слово владельца
            # 10.10, п.1). Путь -- из поля самого продавца; вход и выход -- из
            # уже посчитанных слагаемых итога по цепи, без единого нового
            # чтения узла.
            **put_prodazhi(п),
            **vhod_vyhod_po_cepi(п),
            # ДЕНЬГИ СДЕЛКИ -- сырыми полями плюс итог тем же счётом, каким
            # считает служба (ST.итог_позиции): иначе таблица толпы и все
            # прочие доклады считали бы итог по-разному.
            "sol_in": п.get("sol_in"),
            "closed_sol_net": п.get("closed_sol_net"),
            # УДЕРЖАНИЕ -- И В СЕКУНДАХ, И В СЛОТАХ ПО ФАКТУ (решение владельца
            # 28.09): длина слота по замеру 0.2659 с, а не 0.4, и одни секунды
            # скрывают, во сколько слотов уложилось удержание.
            "hold_slots_plan": п.get("hold_slots"),
            "hold_s_plan": п.get("sell_after_s"),
            "slot_len_at_buy_s": п.get("slot_len_at_buy_s"),
            "hold_slots_fact": удержание_в_слотах(п),
            # ТИП ПУЛА И ЧТЕНИЯ DLMM -- ПРОСТО ПЕРЕНОС ПОЛЕЙ ЗАПИСИ, без логики.
            # Без pool_program в выгрузке нельзя сказать, какого типа была сделка,
            # то есть нельзя дать строку "первая сделка нового типа" по фактам.
            # lane_dlmm_* пишет полоса в позицию при успешной отправке (п.1 ночи
            # 29->30.09); без них разложить 39-56 мс сборки DLMM нечем, а
            # lane_dlmm_odno_godilos -- это и есть "сошлось/нет" для одного чтения.
            "pool_program": п.get("pool_program"),
            "lane_dlmm_reads": п.get("lane_dlmm_reads"),
            "lane_dlmm_reads_ms": п.get("lane_dlmm_reads_ms"),
            "lane_dlmm_odno_godilos": п.get("lane_dlmm_odno_godilos"),
            "lane_dlmm_odno_why_not": п.get("lane_dlmm_odno_why_not"),
            # ТЕНЬ НУЛЯ ЧТЕНИЙ (утренний пакет 01.10, п.1). ОБЕ цены и ОБА
            # минимума идут в выгрузку рядом с годилось: по проценту одному
            # Code-2 не сможет пересчитать расхождение сам, а по двум парам
            # чисел -- сможет, и именно это и есть проверка.
            "lane_dlmm_nol_godilos": п.get("lane_dlmm_nol_godilos"),
            "lane_dlmm_nol_why_not": п.get("lane_dlmm_nol_why_not"),
            "lane_dlmm_nol_cena_polosy": п.get("lane_dlmm_nol_cena_polosy"),
            "lane_dlmm_nol_cena_teni": п.get("lane_dlmm_nol_cena_teni"),
            "lane_dlmm_nol_min_out_polosy": п.get("lane_dlmm_nol_min_out_polosy"),
            "lane_dlmm_nol_min_out_teni": п.get("lane_dlmm_nol_min_out_teni"),
            "lane_dlmm_nol_otklonenie_pct": п.get("lane_dlmm_nol_otklonenie_pct"),
            # S+0 И РЕГИОНАЛЬНАЯ ОТПРАВКА -- ДЛЯ МЕТРИКИ "ДО/ПОСЛЕ ПО НЕ-EU
            # ЛИДЕРАМ" (слово владельца 29.09 ночью, п.2). s_plus заполняется
            # ниже, после дозаполнения слота посадки по цепи: до него landed_slot
            # у части сделок пуст. Поля региона пишет отправка полосы; пока их в
            # записи нет, здесь стоит None и причина -- это честнее нуля.
            "s_plus": None,
            "s_plus_why_not": None,
            # РЕГИОН ЛИДЕРА И РЕГИОН ОТПРАВКИ -- ДВЕ РАЗНЫЕ ВЕЩИ, и мешать
            # их нельзя. region_lidera -- где сидел лидер слота, в который мы
            # СЕЛИ: он считается по расписанию эпохи и карте регионов ниже и
            # есть уже сейчас, до всякого включения флага. Именно он даёт "ДО"
            # в метрике "S+0 до/после по не-EU лидерам". region_otpravki --
            # какую региональную точку выбрала полоса; пока
            # BLOOM_REGION_SEND выключен, его в записи нет вовсе.
            "region_lidera": None,
            "region_lidera_why_not": None,
            "region_otpravki": п.get("lane_region"),
            # РЕГИОН ЛИДЕРА ПО ЗАПИСИ (слово владельца 01.10, п.5): его пишет
            # посадка покупки рядом с lane_region. region_lidera выше считается
            # ЗДЕСЬ по расписанию эпохи, и два числа рядом нужны именно затем,
            # чтобы расхождение между счётом выгрузки и записью было видно, а не
            # пряталось за одним полем.
            "lane_region_leader": п.get("lane_region_leader"),
            "lane_region_leader_why_not": п.get("lane_region_leader_why_not"),
            "lane_region_leader_slot": п.get("lane_region_leader_slot"),
            "region_point": п.get("lane_region_point"),
            "region_senders": п.get("lane_region_senders"),
            "region_otpravki_why_not": (
                п.get("lane_region_why_not") or "поля региона в записи нет:"
                " региональная отправка регион не писала"
                if п.get("lane_region") is None else None),
            "hold_s_fact": удержание_в_секундах(п),
            "tips_sol": п.get("lane_tips_total_sol"),
            "priority_lamports": п.get("lane_priority_lamports"),
            # НАЛОГ ТОКЕНА И КОМИССИЯ ПУЛА -- ОТДЕЛЬНЫМИ ПОЛЯМИ (владелец
            # 28.09, п.4б). В записи позиции их нет: дозаполняются из журнала
            # решений ниже. В РЕШЕНИЕ они не идут -- только в выгрузку.
            "nalog_tokena_bps": п.get("tax_bps"),
            "nalog_marshruta_bps": None,
            "komissiya_pula_pct": None,
            # РЕЗЕРВ ПУЛА НА ВХОДЕ И БИЛЕТ ОТ РЕЗЕРВА: в записи позиции их нет,
            # они живут в журнале решений и дозаполняются ниже.
            "pool_reserve_sol": None,
            "pool_reserve_kind": None,
            "bilet_sol": None,
            "bilet_dolya": None,
            "bilet_ot_rezerva": None,
            "bilet_rezerv_sol": None,
            "bilet_rezerv_otkuda": None,
            "bilet_rezerv_vid": None,
            "bilet_rezerv_why_not": None,
            # ИТОГ ПО ЦЕПИ БЕЗ РАСПАКОВКИ WSOL и прежнее нативное число рядом:
            # по ним видно, какие сделки сменили итог после правки учёта 01.10.
            "itog_po_cepi_sol": п.get("closed_sol_net"),
            "itog_po_cepi_nativ_sol": п.get("closed_sol_net_native"),
            "wsol_delta_sol": п.get("closed_wsol_delta"),
            "freeze_authority": None,
            "freeze_authority_otozvan": None,
            "state": п.get("state"),
            "closed_reason": п.get("closed_reason"),
            # СТРОИТЕЛЬ: программа пула, которой собрана покупка. В записи
            # позиции это program (ставится в БРОНИ, до отправки). Пусто --
            # дозаполняется из журнала решений ниже, и в отчёте видно, у
            # скольких рядов он так и остался пустым.
            "stroitel": п.get("program"),
            "stroitel_otkuda": ("запись позиции: program" if п.get("program") else None),
            "итог_sol": None, "расход_sol": None,
            # ПОЛЯ, КОТОРЫЕ ПРОСИЛ ВЛАДЕЛЕЦ ОТДЕЛЬНО (п.5, 28.09): подписи
            # покупки и продажи и СЕВШАЯ подпись со своим слотом. Севшая --
            # это та, что действительно легла в блок; в lane_signature может
            # стоять вариант, который не сел.
            "buy_sig": (п.get("lane_landed_signature")
                        or п.get("lane_signature")
                        or п.get("lane_signature_local")),
            "sell_sig": ((п.get("last_sell_signatures") or [None])[-1]
                          if isinstance(п.get("last_sell_signatures"), list)
                          else п.get("jup_signature")),
            "landed_sig": п.get("lane_landed_signature"),
            "landed_slot": п.get("lane_landed_slot"),
            "token_name": п.get("token_name"),
            "source_name": п.get("source_name"),
            # ВСЕ ПОЛЯ ЗАПИСИ -- по явному запросу. Без этого вторая сессия
            # видит только отобранное, и каждое новое поле требует правки
            # выгрузки.
            **({"zapis": dict(п)} if а.polnye else {}),
        })
    # НАЛОГ ТОКЕНА И КОМИССИЯ ПУЛА -- ИЗ ЖУРНАЛА РЕШЕНИЙ.
    решения = из_решений(а.state_dir, set(по_cid), с_ts=с_)
    добрано_чисел = 0
    for ряд in ряды:
        д = решения.get(ряд["cid"]) or {}
        for поле in ("nalog_tokena_bps", "nalog_marshruta_bps",
                      "komissiya_pula_pct", "freeze_authority",
                      "freeze_authority_otozvan", "stroitel", "stroitel_otkuda",
                      "pool_reserve_sol", "pool_reserve_kind",
                      "bilet_sol", "bilet_dolya", "bilet_ot_rezerva",
                      "bilet_rezerv_sol", "bilet_rezerv_otkuda",
                      "bilet_rezerv_vid", "bilet_rezerv_why_not"):
            if ряд.get(поле) is None and д.get(поле) is not None:
                ряд[поле] = д[поле]
                добрано_чисел += 1
    print(f"налог и комиссия пула: дозаполнено чисел {добрано_чисел} "
          f"по {len(решения)} сделкам из журнала решений")
    # СТРОИТЕЛЬ: сколько рядов знают его и откуда. Пустые называются поимённо --
    # "почти везде заполнено" не годится, нужен список тех, где его нет.
    с_строителем = [р for р in ряды if р.get("stroitel")]
    без_строителя = [р for р in ряды if not р.get("stroitel")]
    откуда = {}
    for р in с_строителем:
        откуда[р.get("stroitel_otkuda") or "не сказано"] = \
            откуда.get(р.get("stroitel_otkuda") or "не сказано", 0) + 1
    print(f"строитель: заполнен у {len(с_строителем)} из {len(ряды)}; откуда {откуда}")
    if без_строителя:
        print("строителя нет у cid: "
              + ", ".join(str(р.get("cid"))[-12:] for р in без_строителя[:20]))
    # НАШЕ МЕСТО В БЛОКЕ -- ДОЗАПОЛНИТЬ ПО ЦЕПИ. Слово владельца 28.09 (п.4в):
    # "наш индекс в блоке почти везде пуст -- заполнять из того же источника,
    # что и TG-строка «мы: S+N, место»; уже записанные сделки дозаполнить".
    # Источник тот же: СЕВШАЯ подпись (не принятая) и список подписей её блока.
    место_добрано = место_не_вышло = 0
    # УЗЕЛ ОБЪЯВЛЕН ДО ВЕТВЛЕНИЯ. Раньше он создавался только внутри одной
    # ветки, и счёт чужих продаж ниже падал бы NameError ровно в тех прогонах,
    # где место в блоке дозаполнять было не нужно.
    узел = None
    ключ = (os.environ.get("HELIUS_API_KEY2")
            or os.environ.get("HELIUS_API_KEY") or "")
    нужны = [р_ for р_ in ряды if р_.get("our_block_index") is None]
    if а.bez_seti:
        print(f"место в блоке: {len(нужны)} сделок без места, сеть выключена (--bez-seti)")
    elif not нужны:
        print("место в блоке: дозаполнять нечего")
    elif not ключ:
        print("место в блоке: HELIUS_API_KEY не задан -- дозаполнять нечем")
    else:
        узел = Узел(ключ)
        # Один getSignatureStatuses на 256 подписей: весь день -- один-два вызова.
        подписи, чей = [], {}
        for р_ in нужны:
            п_ = по_cid.get(р_["cid"]) or {}
            варианты = [п_.get("lane_landed_signature")] + наши_подписи(п_)
            for с in варианты:
                if isinstance(с, str) and с and с not in чей:
                    чей[с] = р_["cid"]
                    подписи.append(с)
        села: dict = {}
        for н in range(0, len(подписи), 256):
            кусок = подписи[н:н + 256]
            try:
                значения = узел.севшие(кусок)
            except Exception as exc:  # noqa: BLE001
                print(f"ПРЕДУПРЕЖДЕНИЕ: статусы не отдались "
                      f"({type(exc).__name__}: {str(exc)[:120]})", file=sys.stderr)
                значения = []
            for подпись, з in zip(кусок, значения):
                if isinstance(з, dict) and isinstance(з.get("slot"), int):
                    села.setdefault(чей[подпись], (подпись, з["slot"]))
        # Блок читается ОДИН РАЗ НА СЛОТ: в одном слоте могут сидеть две наши
        # сделки, и второй запрос был бы кредитом впустую.
        блоки: dict = {}
        for р_ in нужны:
            пара = села.get(р_["cid"])
            if not пара:
                р_["our_block_why_not"] = "севшей подписи в цепи нет"
                место_не_вышло += 1
                continue
            подпись, слот = пара
            if слот not in блоки:
                try:
                    блоки[слот] = узел.подписи_блока(слот)
                except Exception as exc:  # noqa: BLE001
                    блоки[слот] = []
                    р_["our_block_why_not"] = (f"getBlock не отдался: "
                                                f"{type(exc).__name__}")
            м = место_в_блоке(блоки.get(слот) or [], подпись)
            р_["landed_sig"] = р_.get("landed_sig") or подпись
            р_["landed_slot"] = р_.get("landed_slot") or слот
            if м["index"] is None:
                р_["our_block_why_not"] = р_.get("our_block_why_not") or м["why_not"]
                место_не_вышло += 1
                continue
            р_.update(our_block_index=м["index"], our_block_total=м["total"],
                       our_block_share=м["share"],
                       our_block_from="дозаполнено выгрузкой по цепи")
            место_добрано += 1
        print(f"место в блоке: дозаполнено {место_добрано}, не вышло "
              f"{место_не_вышло}, вызовов узла {узел.вызовов}")
    # S+0 СЧИТАЕТСЯ ПОСЛЕ ДОЗАПОЛНЕНИЯ: слот нашей посадки часть сделок
    # получает именно там, и посчитать раньше значило бы потерять число.
    с_плюсом = 0
    for р_ in ряды:
        сп = s_plus(р_)
        р_["s_plus"] = сп["s_plus"]
        р_["s_plus_why_not"] = сп["why_not"]
        с_плюсом += (сп["s_plus"] is not None)
    print(f"S+0 посчитан у {с_плюсом} из {len(ряды)} сделок")
    # РЕГИОН ЛИДЕРА СЛОТА ПОСАДКИ -- база "ДО" для метрики п.2. Расписание
    # эпохи спрашивается ОДИН раз: границы эпохи берутся из самой карты
    # (getEpochInfo лишним вызовом не нужен), а слоты чужих эпох честно
    # помечаются причиной.
    км = карта_лидеров(а.karta_regionov)
    if км.get("why_not"):
        for р_ in ряды:
            р_["region_lidera_why_not"] = км["why_not"]
        print(f"регион лидера: {км['why_not']}")
    else:
        нач, длина = км["начало_эпохи"], км["слотов_в_эпохе"]
        свои = [р_ for р_ in ряды
                 if isinstance(р_.get("landed_slot"), int)
                 and нач <= р_["landed_slot"] < нач + длина]
        чужие = len(ряды) - len(свои)
        расп: dict = {}
        причина = None
        if not свои:
            причина = f"ни один слот не из эпохи карты ({км.get('эпоха')})"
        elif а.bez_seti:
            причина = "расписание эпохи не спрошено (--bez-seti)"
        elif not ключ:
            причина = "HELIUS_API_KEY не задан -- расписание эпохи спросить нечем"
        else:
            у = узел if узел is not None else Узел(ключ)
            узел = у
            try:
                сырое = у.зов("getLeaderSchedule", [нач]) or {}
                for личность, инд in сырое.items():
                    for и in инд:
                        расп[и] = личность
            except Exception as e:  # noqa: BLE001
                причина = f"getLeaderSchedule не отдался: {type(e).__name__}"
        по_региону: dict = {}
        посчитано = 0
        for р_ in ряды:
            if причина:
                р_["region_lidera_why_not"] = причина
                continue
            слот = р_.get("landed_slot")
            if not (isinstance(слот, int) and нач <= слот < нач + длина):
                р_["region_lidera_why_not"] = (
                    f"слот посадки не из эпохи карты ({км.get('эпоха')})")
                continue
            л = лидер_слота(слот, расп, нач)
            if not л["лидер"]:
                р_["region_lidera_why_not"] = л["why_not"]
                continue
            рег = (км["по_лидеру"] or {}).get(л["лидер"])
            if not рег:
                р_["region_lidera_why_not"] = "лидера нет в карте регионов"
                continue
            р_["region_lidera"] = рег
            посчитано += 1
            по_региону[рег] = по_региону.get(рег, 0) + 1
        сводка = ", ".join(f"{к}={v}" for к, v in sorted(по_региону.items()))
        print(f"регион лидера посадки: посчитан у {посчитано} из {len(ряды)}"
              f" сделок; вне эпохи карты {чужие}; {сводка or 'по регионам пусто'}")
    # ЧУЖИЕ ПРОДАЖИ В ОКНЕ НАШЕГО УДЕРЖАНИЯ / НАШ БИЛЕТ (слово владельца
    # 29.09, п.3). Считается по цепи и только для ЗАКРЫТЫХ сделок, у которых
    # есть обе подписи и обе метки слотов: без них окна нет, а выдумывать его
    # нельзя. Незнание остаётся незнанием -- поля None и причина словами.
    if узел is None and ключ and not а.bez_seti:
        узел = Узел(ключ)
    if узел is not None:
        посчитано_чужих = 0
        for р_ in ряды:
            мнт = р_.get("mint")
            подп_п = р_.get("buy_sig") or р_.get("landed_sig")
            слот_п = р_.get("landed_slot")
            дер = р_.get("hold_slots_fact")
            билет = р_.get("sol_in") or р_.get("size_sol")
            if not (мнт and подп_п and слот_п and дер):
                р_["chuzhie_why_not"] = "нет минта, подписи покупки или удержания"
                continue
            хр = хранилище_пула_из_покупки(
                узел, подпись_покупки=подп_п, минт=мнт,
                наш_кошелёк=(р_.get("wallet") or кошелёк_полосы_из_ряда(р_)))
            if not хр.get("хранилище"):
                р_["chuzhie_why_not"] = хр.get("why_not")
                continue
            р_["pool_vault"] = хр["хранилище"]
            чп = чужие_продажи_в_окне(
                узел, пул=хр["хранилище"], минт=мнт,
                слот_покупки=int(слот_п), слот_продажи=int(слот_п) + int(дер),
                наши_подписи_=set(наши_подписи(по_cid.get(р_["cid"]) or {})),
                предел_подписей=int(а.predel_podpisey))
            if чп.get("чужие_продажи_sol") is None:
                р_["chuzhie_why_not"] = чп.get("why_not")
                continue
            р_["chuzhie_prodazhi_sol"] = чп["чужие_продажи_sol"]
            р_["chuzhih_prodazh"] = чп["чужих_продаж"]
            # НОЛЬ ЧУЖИХ ПРОДАЖ И НОЛЬ ПРОСМОТРЕННОГО -- РАЗНЫЕ ВЕЩИ. Без
            # этого числа "чужих продаж 0" читается как факт о рынке, а может
            # означать, что в окне вообще не оказалось ни одной транзакции.
            р_["chuzhie_prosmotreno"] = чп.get("просмотрено_транзакций")
            р_["chuzhie_okno_slotov"] = чп.get("окно_слотов")
            р_["chuzhie_okno_pokryto"] = чп.get("окно_покрыто")
            р_["chuzhih_podpisey_pula"] = чп.get("подписей_пула_прочитано")
            р_["nash_bilet_sol"] = билет
            if билет:
                р_["chuzhie_k_biletu"] = round(
                    float(чп["чужие_продажи_sol"]) / float(билет), 4)
            посчитано_чужих += 1
        print(f"чужие продажи в окне удержания: посчитано {посчитано_чужих} из "
              f"{len(ряды)}, вызовов узла всего {узел.вызовов}")
    # ИТОГ -- ТЕМ ЖЕ МОДУЛЕМ, ЧТО У СЛУЖБЫ. Порядок каталогов важен: служба
    # работает из /home/bot/bloom_executor, а рядом лежит возможно устаревшая
    # выписка репозитория.
    учёт = None
    for кат in ("/home/bot/bloom_executor",
                "/home/bot/robinhood-chain-alpha/analysis",
                os.path.join(os.path.dirname(os.path.dirname(
                    os.path.dirname(os.path.abspath(__file__)))), "analysis")):
        if not os.path.exists(os.path.join(кат, "bloom_exec_state.py")):
            continue
        if кат not in sys.path:
            sys.path.insert(0, кат)
        try:
            import importlib  # noqa: PLC0415

            учёт = importlib.import_module("bloom_exec_state")
            print(f"учёт из {учёт.__file__}")
            break
        except Exception as exc:  # noqa: BLE001
            print(f"учёт из {кат} не загружен: {type(exc).__name__}", file=sys.stderr)
    if учёт is not None and hasattr(учёт, "итог_позиции"):
        по_cid_все = по_cid
        for ряд in ряды:
            поз = по_cid_все.get(ряд["cid"]) or {}
            try:
                итог, расход = учёт.итог_позиции(поз)
            except Exception as exc:  # noqa: BLE001
                ряд["итог_почему"] = f"{type(exc).__name__}"
                continue
            ряд["итог_sol"] = (round(итог, 9) if итог is not None else None)
            ряд["расход_sol"] = round(расход, 9)
            # ПРАВИЛО 13 (слово владельца 01.10): КАНОНИЧЕСКОЕ число -- итог по
            # цепи, и в каждой строке лежат ОБЕ ПОДПИСИ, чтобы Code-2 пересчитал
            # независимо. Прежний итог остаётся рядом под своим именем
            # (итог_po_polyam_sol): по нему видно, где поля расходятся с цепью.
            try:
                import c2_itog_po_cepi as C13  # noqa: PLC0415

                ц = C13.из_записи(поз)
                с_ = C13.сверка(поз)
                ряд["итог_po_polyam_sol"] = ряд["итог_sol"]
                ряд["итог_po_cepi_sol"] = ц.get("итог_sol")
                ряд["итог_po_cepi_pochemu_net"] = ц.get("почему_нет")
                ряд["итог_po_cepi_chasti"] = ц.get("части") or None
                ряд["sverka_sol"] = с_.get("расхождение_sol")
                # РАЗНОСТЬ, ДВЕ НАЗВАННЫЕ ВЕЛИЧИНЫ И ОСТАТОК. По одной разности
                # Code-2 не отличит объяснённую ренту от настоящей дыры, а по
                # четырём числам отличит -- и пересчитает сам.
                ряд["renta_zaperta_sol"] = с_.get("рента_заперта_sol")
                ряд["zavernutoe_ostalos_sol"] = с_.get("завёрнутое_осталось_sol")
                ряд["sverka_ostatok_sol"] = с_.get("остаток_sol")
                ряд["sverka_ok"] = с_.get("сверено")
                ряд["buy_sig"] = (ряд.get("buy_sig")
                                   or поз.get(C13.ПОЛЕ_ПОДПИСЬ_ПОКУПКИ)
                                   or поз.get("lane_landed_signature"))
                ряд["sell_sig"] = (ряд.get("sell_sig")
                                    or поз.get(C13.ПОЛЕ_ПОДПИСЬ_ПРОДАЖИ)
                                    or поз.get("last_sell_reported"))
                if ц.get("есть"):
                    ряд["итог_sol"] = ц.get("итог_sol")
                    ряд["итог_откуда"] = "цепь"
                else:
                    ряд["итог_откуда"] = "поля"
            except Exception as exc:  # noqa: BLE001
                ряд["итог_po_cepi_pochemu_net"] = (
                    f"модуль итога по цепи не загружен: {type(exc).__name__}")
                ряд["итог_откуда"] = "поля"
        # СЛАГАЕМЫЕ ПО ИМЕНАМ -- ТЕМ ЖЕ МОДУЛЕМ СЛУЖБЫ (слово владельца 29.09
        # ночью, п.3: "назвать каждое слагаемое по имени -- билет, чаевые,
        # приоритет, комиссия, рента, завёрнутое, возврат -- в самой службе").
        # Итог тут НЕ считается заново: разложение_позиции берёт его у
        # итог_позиции, и поле сходится говорит, сошлась ли цепь до лампорта.
        # Модуль старый и такой функции не знает -- пишем причину словами.
        разложено = сошлось = 0
        if hasattr(учёт, "разложение_позиции"):
            for ряд in ряды:
                поз = по_cid_все.get(ряд["cid"]) or {}
                try:
                    рз = учёт.разложение_позиции(поз)
                except Exception as exc:  # noqa: BLE001
                    ряд["разложение_почему"] = f"{type(exc).__name__}"
                    continue
                for имя in ("билет_sol", "чаевые_sol", "приоритет_sol",
                             "тариф_sol", "комиссия_sol", "рента_sol",
                             "завёрнутое_sol", "возврат_sol",
                             "натив_покупки_sol", "неназванное_sol",
                             "сходится", "ветвь"):
                    ряд[имя] = рз.get(имя)
                разложено += 1
                сошлось += bool(рз.get("сходится"))
            print(f"слагаемые по именам: разложено {разложено} из {len(ряды)},"
                  f" цепь сошлась до лампорта у {сошлось}")
        else:
            for ряд in ряды:
                ряд["разложение_почему"] = (
                    "модуль учёта старше правки: разложение_позиции в нём нет")
            print("слагаемые по именам: модуль учёта их не знает")

    # ВЕРСИЯ ВЫГРУЗКИ (слово владельца 01.10, п.8): по ней Code-2 отличает
    # строки с каноническим итогом по цепи от прежних.
    по_цепи = sum(1 for р_ in ряды if р_.get("итог_откуда") == "цепь")
    не_сверено = sum(1 for р_ in ряды if р_.get("sverka_ok") is False)
    # ОТКАЗЫ -- ОТДЕЛЬНЫМИ ЧИСЛАМИ В СВОДКЕ, а не только полями в рядах: по
    # ним сразу видно, сколько сделок окна не прошло и сколько из них легло
    # именно на допуске (слово владельца 09.10, п.4).
    отказов = [р_ for р_ in ряды if р_.get("otkaz_vid")]
    по_допуску = [р_ for р_ in отказов if р_.get("otkaz_po_dopusku")]
    без_подписи = [р_ for р_ in отказов if not р_.get("otkaz_podpis")]
    print(f"отказы: {len(отказов)} из {len(ряды)}, из них по допуску "
          f"{len(по_допуску)}; без подписи упавшей {len(без_подписи)}")
    # ПУТЬ ПРОДАЖИ И ПОКРЫТИЕ ВХОД/ВЫХОД -- ЧИСЛАМИ В СВОДКЕ (слово владельца
    # 10.10, п.1). По рядам это видно поштучно, а доля пути за сутки -- вопрос
    # одной строки: сколько продал наш путь, сколько Jupiter, у скольких путь
    # не назван вовсе.
    путей = {}
    for р_ in ряды:
        путей[р_.get("put_prodazhi") or "не назван"] = (
            путей.get(р_.get("put_prodazhi") or "не назван", 0) + 1)
    неизв_вид = sorted({р_.get("put_prodazhi_vid") for р_ in ряды
                        if р_.get("put_prodazhi_vid")
                        and not р_.get("put_prodazhi")})
    с_входом = sum(1 for р_ in ряды if р_.get("vhod_sol_po_cepi") is not None)
    с_выходом = sum(1 for р_ in ряды if р_.get("vyhod_sol_po_cepi") is not None)
    print(f"путь продажи: {путей}; неизвестных видов адреса {неизв_вид}; "
          f"вход по цепи у {с_входом} из {len(ряды)}, выход у {с_выходом}")
    свод = {"снято_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             # ВЕРСИЯ ПОДНЯТА: в рядах появились put_prodazhi и вход/выход по
             # цепи. По прежнему имени Code-2 принял бы файл за старую схему.
             "версия": "pravilo14v",
             "put_prodazhi_po_sutkam": путей,
             "put_prodazhi_vidy_neizvestnye": неизв_вид or None,
             "strok_s_vhodom_po_cepi": с_входом,
             "strok_s_vyhodom_po_cepi": с_выходом,
             "отказов": len(отказов),
             "отказов_по_допуску": len(по_допуску),
             "отказов_без_подписи": len(без_подписи),
             "итог_канонический": "итог_po_cepi_sol (сумма изменений ВСЕХ наших "
                                   "счетов по двум подписям); итог_po_polyam_sol "
                                   "-- прежний счёт по полям",
             "строк_с_итогом_по_цепи": по_цепи,
             "строк_не_сверено": не_сверено,
             "подпись_сводки": ("не сверено" if (не_сверено or по_цепи < len(ряды))
                                 else "сверено"),
             "с": а.s, "до": а.do or None, "полные_поля": bool(а.polnye),
             "строк_просмотрено": просмотрено, "сделок": len(ряды),
             "с_севшей_подписью": sum(1 for р_ in ряды if р_.get("landed_sig")),
             "с_именем_токена": sum(1 for р_ in ряды if р_.get("token_name")),
             "с_минтом_и_слотом": sum(1 for р_ in ряды
                                       if р_["mint"] and р_["source_slot"]),
             "ряды": ряды}
    текст = json.dumps(свод, ensure_ascii=False, indent=1)
    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            ф.write(текст + "\n")
        print(json.dumps({к: свод[к] for к in
                           ("снято_utc", "с", "до", "полные_поля",
                            "строк_просмотрено", "сделок", "с_минтом_и_слотом",
                            "с_севшей_подписью", "с_именем_токена")}, ensure_ascii=False))
    else:
        print(текст)
    return 0


def самопроверка() -> int:
    сбоев = всего = 0

    def chk(имя: str, ок: bool, что=None) -> None:
        nonlocal сбоев, всего
        всего += 1
        print(f"  [{'ok  ' if ок else 'СБОЙ'}] {имя}")
        if not ок:
            сбоев += 1
            if что is not None:
                print(f"         {что!r}")

    м = место_в_блоке(["А", "Б", "В"], "А")
    chk("первая транзакция блока -- это место 0, а не «не знаем»",
        м["index"] == 0 and м["total"] == 3, м)
    м = место_в_блоке(["А", "Б"], "Я")
    chk("нашей подписи в блоке нет -- причина, а не ноль",
        м["index"] is None and м["why_not"], м)
    chk("пустой блок в ноль не превращается",
        место_в_блоке([], "А")["index"] is None)
    chk("метка UTC разбирается", метка("2026-09-28T00:21") > 0)
    chk("удержание в слотах: 172 - 100 = 72",
        удержание_в_слотах({"lane_landed_slot": 100,
                             "last_sell_outcome": {"slot": 172}}) == 72,
        удержание_в_слотах({"lane_landed_slot": 100,
                             "last_sell_outcome": {"slot": 172}}))
    chk("нет слота продажи -- None, а не ноль",
        удержание_в_слотах({"lane_landed_slot": 100}) is None)
    chk("удержание в секундах считается до отправки продажи",
        удержание_в_секундах({"ts_sent": 1000.0,
                               "ts_last_sell_attempt": 1028.7}) == 28.7,
        удержание_в_секундах({"ts_sent": 1000.0, "ts_last_sell_attempt": 1028.7}))
    chk("подписи варианта не двоятся",
        наши_подписи({"lane_signature": "A", "lane_pool_candidates": ["A", "B"]})
        == ["A", "B"])
    chk("S+0: 451748367 - 451748365 = 2",
        s_plus({"landed_slot": 451748367, "source_slot": 451748365})["s_plus"] == 2)
    chk("нет слота источника -- причина, а не ноль",
        s_plus({"landed_slot": 1})["s_plus"] is None
        and s_plus({"landed_slot": 1})["why_not"])
    chk("карты регионов нет -- отказ словами, а не пустая карта молча",
        карта_лидеров("/нет/такого/файла.json")["why_not"])
    chk("лидер слота берётся по индексу от начала эпохи",
        лидер_слота(451440007, {7: "Личность"}, 451440000)["лидер"] == "Личность")
    chk("слот вне расписания эпохи -- причина, а не чужой лидер",
        лидер_слота(451440007, {8: "Личность"}, 451440000)["лидер"] is None)
    # ЧУЖИЕ ПРОДАЖИ: НОЛЬ О РЫНКЕ И НОЛЬ О НЕДОЧИТАННОЙ ИСТОРИИ -- РАЗНОЕ.
    # Именно на этом chuzhie_prosmotreno был 0 на всех сделках 29-30.09: обход
    # подписей пула идёт от новых к старым и упирается в предел, а окно сделки
    # лежит часами раньше.
    class _УзелПодписей:
        """Поддельный узел: отдаёт подписи пачками по 100, как настоящий."""

        def __init__(self, слоты):
            self.слоты = list(слоты)
            self.выдано = 0

        def подписи_адреса(self, адрес, *, предел=100, до=None):  # noqa: ARG002
            пачка = self.слоты[self.выдано:self.выдано + предел]
            self.выдано += len(пачка)
            return [{"signature": f"П{с}", "slot": с} for с in пачка]

        def транзакция(self, подпись):  # noqa: ARG002
            return {"meta": {"preTokenBalances": [], "postTokenBalances": []}}

    # (а) Окно НЕ покрыто: 300 подписей все выше окна -- обязан быть отказ.
    у = _УзелПодписей(range(500_000, 500_000 - 400, -1))
    о = чужие_продажи_в_окне(у, пул="П", минт="М", слот_покупки=400_000,
                              слот_продажи=400_100, наши_подписи_=set())
    chk("окно не покрыто -- отказ с причиной, а не ноль чужих продаж",
        о["чужих_продаж"] is None and о["чужие_продажи_sol"] is None
        and о["окно_покрыто"] is False and "окно не покрыто" in (о["why_not"] or ""), о)
    # (б) Окно покрыто, в нём никого -- ноль ЧЕСТНЫЙ.
    у = _УзелПодписей([500_000, 499_999, 399_000])
    о = чужие_продажи_в_окне(у, пул="П", минт="М", слот_покупки=400_000,
                              слот_продажи=400_100, наши_подписи_=set())
    chk("окно покрыто и пусто -- ноль честный, отказа нет",
        о["чужих_продаж"] == 0 and о["чужие_продажи_sol"] == 0.0
        and о["окно_покрыто"] is True and о["why_not"] is None, о)
    # (в) Подписи пула кончились -- историю видели всю, значит покрыто.
    у = _УзелПодписей([500_000, 450_000])
    о = чужие_продажи_в_окне(у, пул="П", минт="М", слот_покупки=400_000,
                              слот_продажи=400_100, наши_подписи_=set())
    chk("подписи пула кончились -- окно считается покрытым",
        о["окно_покрыто"] is True and о["чужих_продаж"] == 0, о)
    # (г) Транзакции в окне есть -- они просмотрены, и это видно числом.
    у = _УзелПодписей([400_050, 400_040, 399_000])
    о = чужие_продажи_в_окне(у, пул="П", минт="М", слот_покупки=400_000,
                              слот_продажи=400_100, наши_подписи_=set())
    chk("транзакции в окне просмотрены, и просмотрено не ноль",
        о["просмотрено_транзакций"] == 2 and о["окно_покрыто"] is True, о)

    # ДОГОВОР ИМЁН С ПИШУЩЕЙ СТОРОНОЙ. Ровно на этом выгрузка и обожглась
    # 30.09: она читала lane_region*, а НИКТО в коде их не писал, и поле
    # region_otpravki было пустым ВСЕГДА -- при любом значении флага. Причём
    # печаталось "региональная отправка регион не писала", то есть проверка
    # называла причину, которой не было. Здесь имена сверяются с модулем
    # отправки: разошлись -- самопроверка красная, а не выгрузка молчаливая.
    имена_региона = ("lane_region", "lane_region_point", "lane_region_senders",
                      "lane_region_why_not")
    путь_модуля = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "analysis", "bloom_own_send.py")
    if os.path.exists(путь_модуля):
        with open(путь_модуля, encoding="utf-8") as ф:
            текст_модуля = ф.read()
        нет_в_модуле = [и for и in имена_региона if f"{и}=" not in текст_модуля]
        chk("имена полей региона, которые читает выгрузка, ПИШЕТ отправка полосы",
            not нет_в_модуле, нет_в_модуле)
    else:
        chk("модуль отправки найден для сверки имён полей региона", False, путь_модуля)
    # --- РЕЗЕРВ, БИЛЕТ И ИТОГ БЕЗ РАСПАКОВКИ: поля есть в ряду И переносятся
    # из журнала решений (та самая строка из дока Code-3, п.3).
    свой = open(os.path.abspath(__file__), encoding="utf-8").read()
    рабочая_в = свой.split("def self_test")[0]
    нужные_поля = ("pool_reserve_sol", "pool_reserve_kind", "bilet_sol",
                    "bilet_ot_rezerva", "itog_po_cepi_sol",
                    "itog_po_cepi_nativ_sol", "wsol_delta_sol")
    нет_поля = [и for и in нужные_поля if f'"{и}"' not in рабочая_в]
    chk("резерв, билет и итог без распаковки -- поля ряда выгрузки", not нет_поля, нет_поля)
    chk("резерв переносится из журнала решений, а не выдумывается",
        'з.get("pool_reserve_sol")' in рабочая_в
        and 'з.get("pool_reserve_sol_eq")' in рабочая_в, None)
    chk("версия выгрузки pravilo14v -- по ней Code-2 отличит файл с путём "
        "продажи и валовыми вход/выход",
        '"версия": "pravilo14v"' in рабочая_в, None)
    # --- ПУТЬ ПРОДАЖИ (слово владельца 10.10, п.1)
    chk("своя одна нога в пул -- это наш путь",
        put_prodazhi({"sell_address_kind": "свой_в_пул"})["put_prodazhi"] == "наш")
    chk("своя двухшаговая -- тоже наш путь",
        put_prodazhi({"sell_address_kind": "two_step"})["put_prodazhi"] == "наш")
    chk("jupiter -- Jupiter", put_prodazhi({"sell_address_kind": "jupiter"}
                                            )["put_prodazhi"] == "Jupiter")
    _нп = put_prodazhi({})
    chk("не продавалась -- путь None и причина словами, а не «Jupiter»",
        _нп["put_prodazhi"] is None and _нп["put_prodazhi_why_not"], _нп)
    _нов = put_prodazhi({"sell_address_kind": "новый_путь_2027"})
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: незнакомый вид адреса НЕ относится ни к нашему, ни "
        "к Jupiter -- иначе доля испортилась бы на первой правке продавца",
        _нов["put_prodazhi"] is None and "не известен" in
        (_нов["put_prodazhi_why_not"] or ""), _нов)
    chk("виды попыток переносятся строкой, а не теряются",
        put_prodazhi({"sell_address_kind": "jupiter",
                       "sell_address_kinds": "pool,mint"}
                      )["put_prodazhi_vidy_popytok"] == "pool,mint")
    chk("имена видов адреса, которые читает выгрузка, ПИШЕТ продавец",
        all(f'"{в}"' in open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), "analysis", "bloom_seller.py"),
            encoding="utf-8").read() or в == "свой_в_пул"
            for в in ПУТЬ_ПРОДАЖИ_ПО_ВИДУ)
        if os.path.exists(os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "analysis",
            "bloom_seller.py")) else True)
    # --- ВХОД И ВЫХОД ПО ЦЕПИ
    _ч = {"lane_chain_pnl_parts": {
        "покупка": {"все_sol": -0.30396504, "кошелёк_sol": -0.30548388},
        "продажа": {"все_sol": 0.324796815, "кошелёк_sol": 0.326315655}}}
    _вв = vhod_vyhod_po_cepi(_ч)
    chk("вход и выход отдаются ПОЛОЖИТЕЛЬНЫМИ: ушло 0.30396504, вернулось "
        "0.324796815 (сделка 6sQmeiwn 09.10)",
        _вв["vhod_sol_po_cepi"] == 0.30396504
        and _вв["vyhod_sol_po_cepi"] == 0.324796815, _вв)
    chk("их разность равна итогу по цепи до лампорта",
        abs((_вв["vyhod_sol_po_cepi"] - _вв["vhod_sol_po_cepi"])
            - 0.020831775) < 1e-9,
        _вв["vyhod_sol_po_cepi"] - _вв["vhod_sol_po_cepi"])
    chk("ликвидная пара рядом -- запертая рента видна, а не спрятана",
        _вв["vhod_sol_likvid_po_cepi"] == 0.30548388
        and _вв["vyhod_sol_likvid_po_cepi"] == 0.326315655, _вв)
    _бч = vhod_vyhod_po_cepi({"lane_chain_pnl_why_not": "продажа не читалась"})
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: слагаемых нет -- вход и выход None и причина из "
        "записи, а не ноль",
        _бч["vhod_sol_po_cepi"] is None
        and _бч["vhod_vyhod_why_not"] == "продажа не читалась", _бч)
    _одна = vhod_vyhod_po_cepi({"lane_chain_pnl_parts": {
        "покупка": {"все_sol": -0.01}}})
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: есть только покупка -- вход есть, выход None, и "
        "отсутствующая нога названа",
        _одна["vhod_sol_po_cepi"] == 0.01
        and _одна["vyhod_sol_po_cepi"] is None
        and "продажа" in (_одна["vhod_vyhod_why_not"] or ""), _одна)
    chk("путь продажи и вход/выход -- поля РЯДА выгрузки, а не только функций",
        "**put_prodazhi(п)" in рабочая_в
        and "**vhod_vyhod_po_cepi(п)" in рабочая_в, None)
    chk("доли путей продажи и покрытие идут в сводку",
        '"put_prodazhi_po_sutkam"' in рабочая_в
        and '"strok_s_vhodom_po_cepi"' in рабочая_в, None)
    # ОТКАЗЫ ПО ДОПУСКУ (слово владельца 09.10, п.4). Коды 6001 и 6040 -- то
    # же правило, что у analysis/nesevshie_po_cepi.код_ошибки; проверки ниже
    # держат оба куска в согласии и не дают "упала" превратиться в "села".
    chk("ошибки нет -- не отказ, и вид не выдумывается",
        отказ_ряда({"chain_ok": True})["otkaz_vid"] is None
        and отказ_ряда({"chain_ok": True})["otkaz_po_dopusku"] is None,
        отказ_ряда({"chain_ok": True}))
    _о = отказ_ряда({"chain_err": {"InstructionError": [3, {"Custom": 6001}]},
                      "lane_landed_signature": "ПОДПИСЬ"})
    chk("Custom 6001 -- отказ ПО ДОПУСКУ, с подписью упавшей",
        _о["otkaz_po_dopusku"] is True and _о["otkaz_podpis"] == "ПОДПИСЬ"
        and "6001" in (_о["otkaz_kod"] or ""), _о)
    chk("Custom 6040 -- тоже допуск (живой случай 26.09 00:04:23Z)",
        отказ_ряда({"chain_err": {"InstructionError": [2, {"Custom": 6040}]}}
                    )["otkaz_po_dopusku"] is True)
    chk("чужая ошибка инструкции допуском НЕ зовётся",
        отказ_ряда({"chain_err": {"InstructionError": [1, {"Custom": 17}]}}
                    )["otkaz_po_dopusku"] is False)
    chk("blockhash -- свой вид, а не допуск",
        отказ_ряда({"chain_err": "BlockhashNotFound"})["otkaz_vid"] == "blockhash")
    chk("err в meta тоже читается: запись пишет его туда",
        отказ_ряда({"meta": {"err": {"InstructionError": [3, {"Custom": 6001}]}}}
                    )["otkaz_po_dopusku"] is True)
    _бп = отказ_ряда({"chain_err": {"InstructionError": [3, {"Custom": 6001}]}})
    chk("нет подписи упавшей -- причина словами, а не пустая строка",
        _бп["otkaz_podpis"] is None and _бп["otkaz_podpis_why_not"], _бп)
    chk("подпись берётся и из отправленного варианта, когда севшей нет",
        отказ_ряда({"chain_err": "иное", "lane_signature": "В"}
                    )["otkaz_podpis"] == "В")
    chk("поля отказа есть в ряду выгрузки, а не только в функции",
        "**отказ_ряда(п)" in рабочая_в and '"otkaz_po_dopusku"' in рабочая_в, None)
    chk("числа отказов идут в сводку",
        '"отказов_по_допуску"' in рабочая_в, None)

    print(f"самопроверка выгрузки сделок полосы: {всего - сбоев}/{всего} пройдено")
    return 1 if сбоев else 0


if __name__ == "__main__":
    raise SystemExit(main())
