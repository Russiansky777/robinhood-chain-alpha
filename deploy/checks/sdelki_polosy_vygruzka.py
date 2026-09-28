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
            if isinstance(нал, dict) and нал.get("route_transfer_fee_bps") is not None:
                в["nalog_marshruta_bps"] = нал["route_transfer_fee_bps"]
            if з.get("pool_fee_share") is not None:
                в["komissiya_pula_pct"] = round(float(з["pool_fee_share"]) * 100, 4)
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

    def подписи_блока(self, слот: int) -> list:
        блок = self.зов("getBlock", [слот, {
            "encoding": "json", "transactionDetails": "signatures",
            "rewards": False,
            "maxSupportedTransactionVersion": self.ПОТОЛОК_ВЕРСИИ}]) or {}
        return блок.get("signatures") or []


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
            # ДЕНЬГИ СДЕЛКИ -- сырыми полями плюс итог тем же счётом, каким
            # считает служба (ST.итог_позиции): иначе таблица толпы и все
            # прочие доклады считали бы итог по-разному.
            "sol_in": п.get("sol_in"),
            "closed_sol_net": п.get("closed_sol_net"),
            "tips_sol": п.get("lane_tips_total_sol"),
            "priority_lamports": п.get("lane_priority_lamports"),
            # НАЛОГ ТОКЕНА И КОМИССИЯ ПУЛА -- ОТДЕЛЬНЫМИ ПОЛЯМИ (владелец
            # 28.09, п.4б). В записи позиции их нет: дозаполняются из журнала
            # решений ниже. В РЕШЕНИЕ они не идут -- только в выгрузку.
            "nalog_tokena_bps": п.get("tax_bps"),
            "nalog_marshruta_bps": None,
            "komissiya_pula_pct": None,
            "state": п.get("state"),
            "closed_reason": п.get("closed_reason"),
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
                      "komissiya_pula_pct"):
            if ряд.get(поле) is None and д.get(поле) is not None:
                ряд[поле] = д[поле]
                добрано_чисел += 1
    print(f"налог и комиссия пула: дозаполнено чисел {добрано_чисел} "
          f"по {len(решения)} сделкам из журнала решений")
    # НАШЕ МЕСТО В БЛОКЕ -- ДОЗАПОЛНИТЬ ПО ЦЕПИ. Слово владельца 28.09 (п.4в):
    # "наш индекс в блоке почти везде пуст -- заполнять из того же источника,
    # что и TG-строка «мы: S+N, место»; уже записанные сделки дозаполнить".
    # Источник тот же: СЕВШАЯ подпись (не принятая) и список подписей её блока.
    место_добрано = место_не_вышло = 0
    ключ = os.environ.get("HELIUS_API_KEY") or ""
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

    свод = {"снято_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
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
    chk("подписи варианта не двоятся",
        наши_подписи({"lane_signature": "A", "lane_pool_candidates": ["A", "B"]})
        == ["A", "B"])
    print(f"самопроверка выгрузки сделок полосы: {всего - сбоев}/{всего} пройдено")
    return 1 if сбоев else 0


if __name__ == "__main__":
    raise SystemExit(main())
