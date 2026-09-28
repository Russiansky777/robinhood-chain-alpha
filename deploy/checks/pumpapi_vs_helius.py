#!/usr/bin/env python3
"""Поток PumpApi против Helius WS: кто раньше по ОДНИМ транзакциям источников.

ЗАЧЕМ (п.7 владельца 28.09). Полоса копирует сделку источника, и всё решает
момент, когда мы о ней узнали. Сейчас узнаём из Helius WS. Прогон меряет, что
даёт бесплатный поток wss://stream.pumpapi.io: раньше он или позже, на сколько
миллисекунд и на сколько слотов.

ЧТО СКАЗАНО В ДОКУМЕНТАЦИИ (прочитано с хоста 28.09, https://pumpapi.io/stream
и https://pumpapi.io/FAQ):
  * адрес wss://stream.pumpapi.io/ ; ни ключа, ни учётной записи не нужно,
    поток бесплатный;
  * сервер присылает ВСЕ события, отбор -- на стороне клиента;
  * одно соединение на клиента, его и переиспользовать; обрывы возможны,
    переподключение -- забота клиента;
  * сообщение -- JSON (в примере на Rust кадр приходит двоичным), образец
    события в документации показан как {'action': 'buy', 'pool': 'pump', ...};
  * серверы во Франкфурте (наш хост в Нидерландах).

ЧЕГО В ДОКУМЕНТАЦИИ НЕТ: полного перечня полей события. Поэтому прогон НЕ
угадывает имена: он ищет наши адреса ПО ВСЕМ строковым значениям сообщения, а
подпись и слот -- по смыслу значения (подпись base58 длиной 86-88, слот --
целое под ключом со словом slot). Заодно пишется перепись ключей и два образца
сообщения целиком: по ним имена полей станут известны точно, а не по памяти.

СТОРОНА HELIUS -- ИЗ ЖУРНАЛА РЕШЕНИЙ ДЕТЕКТОРА, поле t_recv_ts: это момент,
когда сообщение Helius пришло на ЭТОТ ЖЕ хост. Двое часов одного хоста, без
поправок и без второй подписки -- прогон не тратит ни одного кредита.

ТОЛЬКО ЧТЕНИЕ. Одно соединение, приём сообщений, чтение журнала. Ни подписи,
ни отправки, ни ордера.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

БАЗА58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
АДРЕС_ПОТОКА = "wss://stream.pumpapi.io/"
# Сколько первых сообщений держим для переписи ключей и образцов. Весь поток в
# память не берём: он идёт сотнями сообщений в секунду.
ОБРАЗЦОВ = 2
# Действия, которые считаются СДЕЛКОЙ источника. Имена -- из переписи ключей
# живого потока (поле action), а не из головы: у переводов там "transfer".
ДЕЙСТВИЯ_СДЕЛКИ = ("buy", "sell", "swap")
# Слот Solana -- 400 мс. В слотах разницу считаем по нему, а не на глаз.
СЛОТ_МС = 400.0


def похоже_на_подпись(з) -> bool:
    return (isinstance(з, str) and 86 <= len(з) <= 88
            and all(с in БАЗА58 for с in з))


def строки_значений(о, глубина: int = 6):
    """Все строковые значения дерева. Имена полей не угадываем."""
    if глубина < 0:
        return
    if isinstance(о, str):
        yield о
    elif isinstance(о, dict):
        for з in о.values():
            yield from строки_значений(з, глубина - 1)
    elif isinstance(о, (list, tuple)):
        for з in о:
            yield from строки_значений(з, глубина - 1)


def подпись_события(о) -> str | None:
    """Подпись транзакции: сперва по ключу со словом sig, иначе по виду."""
    if isinstance(о, dict):
        for к, з in о.items():
            if "sig" in str(к).lower() and похоже_на_подпись(з):
                return з
    for з in строки_значений(о):
        if похоже_на_подпись(з):
            return з
    return None


def слот_события(о) -> int | None:
    if not isinstance(о, dict):
        return None
    for к, з in о.items():
        if "slot" in str(к).lower() and isinstance(з, int) and з > 1_000_000:
            return з
    for з in о.values():
        if isinstance(з, dict):
            в = слот_события(з)
            if в is not None:
                return в
    return None


def адреса_из_файла(путь: Path) -> dict:
    """Адреса источников из файла групп: адрес -> имя группы.

    Берём ТОЛЬКО ПОДПИСАННЫЕ группы (subscribe не False): по неподписанным
    сравнивать нечего -- их сделок в нашем журнале нет вовсе, и пара вышла бы
    односторонней.

    Виды разделов те же, что читает сам детектор (bloom_source_groups): адреса
    лежат словарём {адрес: метки}, списком строк или списком объектов с полем
    address, плюс разделы by_signal и snipers. Четвёртый вид не изобретаем.
    """
    д = json.loads(путь.read_text(encoding="utf-8"))
    из_ = {}
    for имя, г in (д.get("groups") or {}).items():
        г = г or {}
        if г.get("subscribe") is False:
            continue
        куски = [г.get("addresses"), г.get("by_signal"), г.get("snipers")]
        for кусок in куски:
            if isinstance(кусок, dict):
                for а in кусок:
                    if а:
                        из_[а] = имя
            elif isinstance(кусок, list):
                for з in кусок:
                    а = з.get("address") if isinstance(з, dict) else з
                    if а:
                        из_[а] = имя
    return из_


def кванти(значения: list, доля: float):
    if not значения:
        return None
    з = sorted(значения)
    if len(з) == 1:
        return з[0]
    место = доля * (len(з) - 1)
    низ = int(место)
    верх = min(низ + 1, len(з) - 1)
    вес = место - низ
    return з[низ] * (1 - вес) + з[верх] * вес


def из_журнала(путь: Path, *, с: float, до: float) -> dict:
    """Подпись источника -> самый РАННИЙ t_recv_ts по журналу решений.

    Журнал читается потоком: он идёт на сотни тысяч строк в сутки.
    """
    из_ = {}
    if not путь.exists():
        return из_
    with путь.open(encoding="utf-8", errors="replace") as ф:
        for строка in ф:
            строка = строка.strip()
            if not строка.startswith("{") or "t_recv_ts" not in строка:
                continue
            try:
                з = json.loads(строка)
            except ValueError:
                continue
            т = з.get("t_recv_ts")
            подпись = з.get("signature") or з.get("source_sig")
            if not подпись or not isinstance(т, (int, float)):
                continue
            if not (с <= float(т) <= до):
                continue
            ранее = из_.get(подпись)
            if ранее is None or float(т) < ранее[0]:
                из_[подпись] = (float(т), з.get("source_slot"), з.get("source"),
                                 з.get("source_task"))
    return из_


def свести(пары: list) -> dict:
    """Сводка по парам: кто раньше, на сколько мс и слотов.

    Знак: положительная разница -- ПОТОК раньше Helius.
    """
    if not пары:
        return {"пар": 0}
    разницы = [п["раньше_мс"] for п in пары]
    поток_раньше = sum(1 for з in разницы if з > 0)
    return {
        "пар": len(пары),
        "поток_раньше_доля": round(поток_раньше / len(пары), 4),
        "медиана_мс": round(кванти(разницы, 0.5), 1),
        "p10_мс": round(кванти(разницы, 0.1), 1),
        "p90_мс": round(кванти(разницы, 0.9), 1),
        "макс_мс": round(max(разницы), 1),
        "мин_мс": round(min(разницы), 1),
        "медиана_слотов": round(кванти(разницы, 0.5) / СЛОТ_МС, 2),
        "p90_слотов": round(кванти(разницы, 0.9) / СЛОТ_МС, 2)}


async def слушать(*, до_ts: float, адреса: dict, итог: dict) -> None:
    import websockets  # noqa: PLC0415

    первые = []
    ключи = Counter()
    попаданий = {}
    сообщений = 0
    байтов = 0
    обрывов = 0
    while time.time() < до_ts:
        осталось = до_ts - time.time()
        if осталось <= 1.0:
            break
        try:
            async with websockets.connect(АДРЕС_ПОТОКА, ping_interval=20,
                                           ping_timeout=20,
                                           max_queue=4096) as соединение:
                итог.setdefault("соединений", 0)
                итог["соединений"] += 1
                while True:
                    осталось = до_ts - time.time()
                    if осталось <= 0:
                        break
                    try:
                        сообщение = await asyncio.wait_for(соединение.recv(),
                                                            timeout=осталось)
                    except asyncio.TimeoutError:
                        break
                    пришло = time.time()
                    сообщений += 1
                    байтов += len(сообщение) if isinstance(
                        сообщение, (bytes, bytearray, str)) else 0
                    try:
                        событие = json.loads(сообщение)
                    except ValueError:
                        continue
                    if isinstance(событие, dict):
                        ключи.update(событие.keys())
                    if len(первые) < ОБРАЗЦОВ:
                        первые.append(событие)
                    # ОТБОР ДВУХ ВИДОВ. По всем строковым значениям -- чтобы
                    # ничего не пропустить (имён полей документация не даёт), и
                    # ОТДЕЛЬНО по txSigner с действием сделки -- потому что
                    # первый вид ловит и переводы, где наш адрес просто
                    # упомянут в postBalances. Замер 28.09 показал ровно это:
                    # 809 "только в потоке" оказались в основном упоминаниями,
                    # и полнотой их считать нельзя.
                    свои = [а for а in строки_значений(событие) if а in адреса]
                    if not свои:
                        continue
                    подписал = (событие.get("txSigner")
                                if isinstance(событие, dict) else None)
                    действие = (str(событие.get("action") or "")
                                if isinstance(событие, dict) else "")
                    сделка_источника = bool(
                        подписал in адреса and действие in ДЕЙСТВИЯ_СДЕЛКИ)
                    подпись = подпись_события(событие)
                    if not подпись:
                        continue
                    if подпись in попаданий:
                        continue
                    попаданий[подпись] = {
                        "ts": пришло,
                        "сделка_источника": сделка_источника,
                        "подписал_источник": подписал in адреса,
                        "действие": действие,
                        "slot": слот_события(событие),
                        "адрес": свои[0],
                        "группа": адреса.get(свои[0]),
                        "action": (событие.get("action")
                                    if isinstance(событие, dict) else None),
                        "pool": (событие.get("pool")
                                  if isinstance(событие, dict) else None)}
        except Exception as exc:  # noqa: BLE001
            обрывов += 1
            итог.setdefault("обрывы", []).append(
                f"{type(exc).__name__}: {str(exc)[:120]}")
            await asyncio.sleep(min(5.0, max(0.5, до_ts - time.time())))
    итог["сообщений"] = сообщений
    итог["байтов"] = байтов
    итог["обрывов"] = обрывов
    итог["ключи_событий"] = dict(ключи.most_common(40))
    итог["образцы"] = первые
    итог["попадания"] = попаданий


def прогон(*, минут: float, журнал: Path, группы: Path, куда: Path) -> dict:
    адреса = адреса_из_файла(группы)
    начало = time.time()
    до_ts = начало + минут * 60.0
    итог = {"начало_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(начало)),
             "минут": минут, "адресов_в_отборе": len(адреса),
             "поток": АДРЕС_ПОТОКА, "только_чтение": True}
    print(f"слушаем {АДРЕС_ПОТОКА} {минут:.0f} мин, адресов в отборе "
          f"{len(адреса)}", flush=True)
    asyncio.run(слушать(до_ts=до_ts, адреса=адреса, итог=итог))
    конец = time.time()
    итог["конец_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(конец))
    # Журнал читаем ПОСЛЕ окна: сообщение Helius могло прийти позже потока, и
    # запись решения появляется уже за краем окна.
    хелиус = из_журнала(журнал, с=начало - 5.0, до=конец + 60.0)
    итог["helius_подписей_в_окне"] = len(хелиус)
    пары = []
    только_поток = []
    сделок_источников = 0
    for подпись, п in (итог.get("попадания") or {}).items():
        if п.get("сделка_источника"):
            сделок_источников += 1
        х = хелиус.get(подпись)
        if not х:
            только_поток.append({"подпись": подпись, "группа": п.get("группа"),
                                  "сделка_источника": bool(п.get("сделка_источника")),
                                  "действие": п.get("действие")})
            continue
        пары.append({"подпись": подпись, "группа": п.get("группа"),
                      "адрес": п.get("адрес"),
                      "слот": п.get("slot") or х[1],
                      "поток_ts": round(п["ts"], 6),
                      "helius_ts": round(х[0], 6),
                      "раньше_мс": round((х[0] - п["ts"]) * 1000.0, 1)})
    только_хелиус = [п for п in хелиус
                      if п not in (итог.get("попадания") or {})]
    итог["пары"] = пары
    итог["свод"] = свести(пары)
    итог["сделок_источников_в_потоке"] = сделок_источников
    итог["только_в_потоке_сделок"] = sum(
        1 for з in только_поток if з.get("сделка_источника"))
    итог["только_в_потоке"] = только_поток[:50]
    итог["только_в_потоке_всего"] = len(только_поток)
    итог["только_в_helius"] = только_хелиус[:50]
    итог["только_в_helius_всего"] = len(только_хелиус)
    # Попадания целиком в отчёт не пишем: важны пары и свод.
    итог.pop("попадания", None)
    куда.write_text(json.dumps(итог, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    print(json.dumps(итог.get("свод"), ensure_ascii=False), flush=True)
    print(f"пар {len(пары)}, только в потоке {len(только_поток)}, "
          f"только в Helius {len(только_хелиус)}, сообщений "
          f"{итог.get('сообщений')}", flush=True)
    return итог


def самопроверка() -> int:
    сбоев = 0
    всего = 0

    def chk(имя: str, ок: bool, что=None) -> None:
        nonlocal сбоев, всего
        всего += 1
        print(f"  [{'ok  ' if ок else 'СБОЙ'}] {имя}")
        if not ок:
            сбоев += 1
            if что is not None:
                print(f"         {что!r}")

    подпись = "5bz8uNH1XctjLpZrbqKa5ZYtgLd61Vv2S6GAGwUGK6FmjUm5fe1oNPNiezWnL7woxyYZ7ALBGSJdV8nDe1yVUCzM"[:88]
    chk("подпись узнаётся по виду", похоже_на_подпись(подпись))
    chk("адрес кошелька за подпись не принимается",
        not похоже_на_подпись("4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x"))
    chk("ноль и не-base58 не подпись",
        not похоже_на_подпись("0" * 88) and not похоже_на_подпись(None))
    событие = {"action": "buy", "pool": "pump", "signature": подпись,
                "slot": 451311774,
                "data": {"trader": "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin",
                          "mint": "Cm6fNnMk7NfzStP9CZpsQA2v3jjzbcYGAxdJySmHpump"}}
    chk("подпись берётся по ключу со словом sig",
        подпись_события(событие) == подпись, подпись_события(событие))
    chk("слот берётся по ключу со словом slot",
        слот_события(событие) == 451311774, слот_события(событие))
    chk("наш адрес находится на любой глубине, имена полей не нужны",
        "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
        in set(строки_значений(событие)))
    # ПОДПИСЬ БЕЗ ИМЕНИ ПОЛЯ: документация полного перечня не даёт, и прогон
    # обязан обходиться без него.
    безымянное = {"a": ["x", подпись], "b": {"c": 451311999}}
    chk("подпись находится и без ключа sig", подпись_события(безымянное) == подпись)
    chk("слота без слова slot не выдумываем", слот_события(безымянное) is None)
    # СВОД: знак разницы -- поток раньше Helius, если она положительная.
    свод = свести([{"раньше_мс": 120.0}, {"раньше_мс": -30.0},
                    {"раньше_мс": 400.0}])
    chk(f"свод: медиана {свод['медиана_мс']} мс, доля потока "
        f"{свод['поток_раньше_доля']}",
        свод["пар"] == 3 and свод["медиана_мс"] == 120.0
        and свод["поток_раньше_доля"] == round(2 / 3, 4)
        and свод["медиана_слотов"] == 0.3, свод)
    chk("без пар свод не врёт нулями", свести([]) == {"пар": 0})
    # ЖУРНАЛ: берётся самый ранний t_recv_ts и только внутри окна.
    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as кат:
        ж = Path(кат) / "decisions.jsonl"
        ж.write_text("\n".join([
            json.dumps({"signature": "A", "t_recv_ts": 1000.5,
                         "source_slot": 7, "source": "S1"}),
            json.dumps({"signature": "A", "t_recv_ts": 1000.9}),
            json.dumps({"signature": "B", "t_recv_ts": 5000.0}),
            "битая строка",
            json.dumps({"t_recv_ts": 1000.0}),
        ]), encoding="utf-8")
        из_ = из_журнала(ж, с=900.0, до=1100.0)
        chk("из журнала взят самый ранний приход и только окно",
            set(из_) == {"A"} and из_["A"][0] == 1000.5 and из_["A"][1] == 7, из_)
        # ГРУППЫ: неподписанные группы в отбор не берём.
        г = Path(кат) / "sources.json"
        г.write_text(json.dumps({
            "groups": {
                "lane_s0": {"subscribe": True,
                             "addresses": {"АДР1": {"batch": 5}}},
                "sniper_src": {"snipers": [{"address": "АДР2"}]},
                "log_only": {"subscribe": False, "addresses": ["АДР3"]}}}),
            encoding="utf-8")
        адреса = адреса_из_файла(г)
        chk("в отбор попали только подписанные группы, все виды разделов",
            адреса == {"АДР1": "lane_s0", "АДР2": "sniper_src"}, адреса)
    print(f"самопроверка потока против Helius: {всего - сбоев}/{всего} пройдено")
    return 1 if сбоев else 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--minut", "--минут", dest="минут", default="60")
    р.add_argument("--zhurnal", dest="журнал", default="")
    р.add_argument("--gruppy", dest="группы", default="")
    р.add_argument("--out", dest="куда", default="")
    р.add_argument("--self-test", action="store_true")
    а = р.parse_args()
    if а.self_test:
        return самопроверка()
    if not (а.журнал and а.группы and а.куда):
        print("нужны --zhurnal, --gruppy и --out")
        return 2
    прогон(минут=float(а.минут), журнал=Path(а.журнал),
            группы=Path(а.группы), куда=Path(а.куда))
    return 0


if __name__ == "__main__":
    sys.exit(main())
