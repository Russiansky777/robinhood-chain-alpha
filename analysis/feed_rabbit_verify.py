#!/usr/bin/env python3
"""Проверка потока RabbitStream по цепи: наши ли это транзакции вообще.

Зачем. За 20 секунд RabbitStream дал 103 сообщения против 3 у Yellowstone
на одном и том же списке адресов. Для потока, который показывает те же
транзакции чуть раньше (плюс упавшие и повторы), разница в тридцать раз
невозможна. Значит, либо сервер применяет account_include иначе, либо в
поток попадает лишнее -- и верить его фильтру на слово нельзя.

Проверка идёт ПО ЦЕПИ и ничему не верит на слово:

  * сколько сообщений, сколько РАЗНЫХ подписей, сколько повторов;
  * в скольких транзакциях наш адрес ДЕЙСТВИТЕЛЬНО есть среди счетов --
    и отдельно считается, находится он в обычных ключах сообщения или
    только в загруженных из таблицы адресов (ALT). Это и есть ответ на
    вопрос, как сервер применяет account_include;
  * сколько пришло по фильтру источников, а сколько по фильтру разгонных
    (имя фильтра лежит в самой записи журнала);
  * сколько подписей узел не отдаёт вовсе (не попали в блок) и сколько
    село с ошибкой -- это цена раннего срабатывания.

Только чтение. Ни одного ордера.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_detector as BD  # noqa: E402


def счета_транзакции(tx: dict) -> tuple:
    """(обычные счета, счета из таблиц адресов). Оба множества -- строки.

    Транзакция версии 0 может держать часть счетов в ALT: в accountKeys их
    нет, они приходят отдельным полем meta.loadedAddresses. Считать только
    accountKeys значит объявить "нашего адреса нет" там, где он есть.
    """
    сообщение = ((tx.get("transaction") or {}).get("message") or {})
    обычные = set()
    for k in (сообщение.get("accountKeys") or []):
        адрес = k.get("pubkey") if isinstance(k, dict) else k
        if isinstance(адрес, str):
            обычные.add(адрес)
    из_таблиц = set()
    загруженные = (tx.get("meta") or {}).get("loadedAddresses") or {}
    if isinstance(загруженные, dict):
        for ключ in ("writable", "readonly"):
            for a in (загруженные.get(ключ) or []):
                if isinstance(a, str):
                    из_таблиц.add(a)
    # jsonParsed кладёт загруженные и прямо в accountKeys с пометкой source.
    for k in (сообщение.get("accountKeys") or []):
        if isinstance(k, dict) and k.get("source") == "lookupTable":
            адрес = k.get("pubkey")
            if isinstance(адрес, str):
                из_таблиц.add(адрес)
                обычные.discard(адрес)
    return обычные, из_таблиц


def читать_журнал(путь: Path, каналы: set) -> dict:
    """канал -> {подпись: {"сообщений": n, "фильтры": {имя: n}}}."""
    из_: dict = {}
    with open(путь, encoding="utf-8", errors="replace") as f:
        for строка in f:
            строка = строка.strip()
            if not строка:
                continue
            try:
                з = json.loads(строка)
            except ValueError:
                continue
            канал = з.get("channel")
            подпись = з.get("signature")
            if not канал or not подпись:
                continue
            if каналы and канал not in каналы:
                continue
            по_каналу = из_.setdefault(канал, {})
            е = по_каналу.setdefault(подпись, {"сообщений": 0, "фильтры": {}})
            е["сообщений"] += 1
            имя_ф = з.get("filter") or з.get("address") or "?"
            е["фильтры"][имя_ф] = е["фильтры"].get(имя_ф, 0) + 1
    return из_


def выборка_по_группам(подписи: dict, сколько: int) -> list:
    """Подписи ПО КАЖДОМУ фильтру отдельно, а не первые подряд.

    Первые подряд -- это почти целиком разгонный адрес: он даёт сотни
    сообщений против единиц у источников. Такая выборка отвечает только
    про разгонный и молчит про источники, ради которых всё и делается.
    """
    по_фильтрам: dict = {}
    for подпись, е in подписи.items():
        имя = max(е["фильтры"], key=lambda k: е["фильтры"][k]) if е["фильтры"] else "?"
        по_фильтрам.setdefault(имя, []).append(подпись)
    из_: list = []
    for имя in sorted(по_фильтрам):
        из_.extend((имя, п) for п in по_фильтрам[имя][:max(0, сколько)])
    return из_


def проверить_канал(helius, подписи: dict, наши: set, *, выборка: int,
                     прочие: set | None = None) -> dict:
    """Сколько из подписей канала РЕАЛЬНО содержат наш адрес среди счетов.

    "наши" -- адреса источников. "прочие" -- разгонные: их сообщения
    законно не содержат ни одного источника, и считать их "чужими" значит
    самому себе соврать. Поэтому они считаются отдельной строкой.
    """
    всего_сообщений = sum(v["сообщений"] for v in подписи.values())
    повторов = всего_сообщений - len(подписи)
    по_фильтрам: dict = {}
    for v in подписи.values():
        for имя, n in v["фильтры"].items():
            по_фильтрам[имя] = по_фильтрам.get(имя, 0) + n
    прочие = прочие or set()
    итог = {"messages": всего_сообщений, "unique": len(подписи),
             "duplicates": повторов, "by_filter": по_фильтрам,
             "checked": 0, "with_our": 0, "only_via_alt": 0,
             "with_booster": 0, "without_any": 0,
             "without_our": 0, "not_on_chain": 0, "failed": 0,
             "by_group": {}, "examples_without_any": []}
    for имя_ф, подпись in выборка_по_группам(подписи, выборка):
        гр = итог["by_group"].setdefault(
            имя_ф, {"checked": 0, "with_our": 0, "with_booster": 0,
                     "without_any": 0, "failed": 0, "not_on_chain": 0})
        try:
            tx = helius.транзакция(подпись, попыток=2, пауза_s=0.1)
        except Exception:  # noqa: BLE001
            continue
        итог["checked"] += 1
        гр["checked"] += 1
        if not tx:
            # Узел не отдаёт -- транзакция в блок не попала.
            итог["not_on_chain"] += 1
            гр["not_on_chain"] += 1
            continue
        if (tx.get("meta") or {}).get("err") is not None:
            итог["failed"] += 1
            гр["failed"] += 1
        обычные, из_таблиц = счета_транзакции(tx)
        все_счета = обычные | из_таблиц
        есть_обычно = bool(обычные & наши)
        есть_в_alt = bool(из_таблиц & наши)
        if есть_обычно or есть_в_alt:
            итог["with_our"] += 1
            гр["with_our"] += 1
            if есть_в_alt and not есть_обычно:
                итог["only_via_alt"] += 1
        elif все_счета & прочие:
            # Разгонный адрес: сообщение законное, просто не про источники.
            итог["with_booster"] += 1
            гр["with_booster"] += 1
        else:
            итог["without_any"] += 1
            итог["without_our"] += 1
            гр["without_any"] += 1
            if len(итог["examples_without_any"]) < 5:
                # Что это за транзакции вообще -- плательщик и программы.
                # Без них "ни одного нашего адреса" остаётся загадкой.
                сообщение = ((tx.get("transaction") or {}).get("message") or {})
                ключи = сообщение.get("accountKeys") or []
                первый = ключи[0] if ключи else None
                плательщик = (первый.get("pubkey")
                               if isinstance(первый, dict) else первый)
                программы = []
                for и in (сообщение.get("instructions") or []):
                    pid = и.get("programId") if isinstance(и, dict) else None
                    if pid and pid not in программы:
                        программы.append(pid)
                итог["examples_without_any"].append(
                    {"signature": подпись, "filter": имя_ф,
                      "slot": tx.get("slot"),
                      "accounts": len(все_счета),
                      "fee_payer": плательщик,
                      "programs": программы[:4],
                      "err": (tx.get("meta") or {}).get("err") is not None})
    n = итог["checked"]
    итог["share_with_our"] = round(итог["with_our"] / n, 4) if n else None
    итог["share_with_any"] = (round((итог["with_our"] + итог["with_booster"]) / n, 4)
                               if n else None)
    итог["share_without_any"] = (round(итог["without_any"] / n, 4) if n else None)
    итог["share_not_on_chain"] = (round(итог["not_on_chain"] / итог["checked"], 4)
                                   if итог["checked"] else None)
    итог["share_failed"] = (round(итог["failed"] / итог["checked"], 4)
                             if итог["checked"] else None)
    return итог


def self_test() -> None:
    всего = [0, 0]

    def chk(имя, условие, факт=None):
        всего[0] += 1
        if условие:
            всего[1] += 1
            print(f"  [ok  ] {имя}")
        else:
            print(f"  [ПЛОХО] {имя} -- {факт}")

    tx_обычная = {"transaction": {"message": {"accountKeys": [
        {"pubkey": "НАШ"}, {"pubkey": "ЧУЖОЙ"}]}}, "meta": {"err": None}}
    о, а = счета_транзакции(tx_обычная)
    chk("обычные счета собраны", о == {"НАШ", "ЧУЖОЙ"} and а == set(), (о, а))

    tx_alt = {"transaction": {"message": {"accountKeys": [{"pubkey": "ПЛАТЕЛЬЩИК"}]}},
               "meta": {"err": None, "loadedAddresses": {
                   "writable": ["НАШ"], "readonly": ["ЕЩЁ"]}}}
    о2, а2 = счета_транзакции(tx_alt)
    chk("счета из таблицы адресов собраны отдельно",
        о2 == {"ПЛАТЕЛЬЩИК"} and а2 == {"НАШ", "ЕЩЁ"}, (о2, а2))

    tx_помеченный = {"transaction": {"message": {"accountKeys": [
        {"pubkey": "ПЛАТЕЛЬЩИК"},
        {"pubkey": "НАШ", "source": "lookupTable"}]}}, "meta": {"err": None}}
    о3, а3 = счета_транзакции(tx_помеченный)
    chk("помеченный source=lookupTable не считается обычным счётом",
        о3 == {"ПЛАТЕЛЬЩИК"} and а3 == {"НАШ"}, (о3, а3))

    chk("пустая транзакция не роняет разбор", счета_транзакции({}) == (set(), set()))

    # Журнал: повторы одной подписи по одному каналу -- это дубли.
    import tempfile
    с_каталог = Path(tempfile.mkdtemp())
    ж = с_каталог / "feed.jsonl"
    ж.write_text("\n".join([
        json.dumps({"channel": "rabbit_ams", "signature": "A", "filter": "src0"}),
        json.dumps({"channel": "rabbit_ams", "signature": "A", "filter": "src0"}),
        json.dumps({"channel": "rabbit_ams", "signature": "B", "filter": "boost0"}),
        json.dumps({"channel": "grpc", "signature": "A", "filter": "src0"}),
        "битая строка",
    ]) + "\n", encoding="utf-8")
    прочитано = читать_журнал(ж, {"rabbit_ams"})
    chk("читается только запрошенный канал", set(прочитано) == {"rabbit_ams"},
        set(прочитано))
    chk("повтор подписи посчитан как повтор",
        прочитано["rabbit_ams"]["A"]["сообщений"] == 2, прочитано)
    chk("битая строка пропущена, а не уронила разбор",
        len(прочитано["rabbit_ams"]) == 2, прочитано)

    class HeliusЗаглушка:
        def __init__(self, ответы):
            self.ответы = ответы

        def транзакция(self, подпись, **kw):
            return self.ответы.get(подпись)

    h = HeliusЗаглушка({"A": tx_обычная, "B": None})
    р = проверить_канал(h, прочитано["rabbit_ams"], {"НАШ"}, выборка=10)
    chk("сообщений и уникальных посчитано раздельно",
        р["messages"] == 3 and р["unique"] == 2 and р["duplicates"] == 1, р)
    chk("наш адрес в счетах найден", р["with_our"] == 1, р)
    chk("подпись, которой нет в цепи, посчитана отдельно",
        р["not_on_chain"] == 1 and р["share_not_on_chain"] == 0.5, р)
    chk("фильтры разложены: источники и разгонные",
        р["by_filter"] == {"src0": 2, "boost0": 1}, р["by_filter"])

    прочитано2 = читать_журнал(ж, {"rabbit_ams"})
    h2 = HeliusЗаглушка({"A": tx_alt, "B": tx_обычная})
    р2 = проверить_канал(h2, прочитано2["rabbit_ams"], {"НАШ"}, выборка=10)
    chk("адрес только из таблицы адресов посчитан отдельно",
        р2["only_via_alt"] == 1 and р2["with_our"] == 2, р2)

    h3 = HeliusЗаглушка({"A": {"transaction": {"message": {"accountKeys": [
        {"pubkey": "ЧУЖОЙ"}]}}, "meta": {"err": None}, "slot": 7}, "B": None})
    р3 = проверить_канал(h3, читать_журнал(ж, {"rabbit_ams"})["rabbit_ams"],
                          {"НАШ"}, выборка=10)
    chk("транзакция без нашего адреса названа и показана примером",
        р3["without_any"] == 1 and р3["examples_without_any"][0]["slot"] == 7, р3)

    # Разгонный адрес: сообщение законное, но к источникам не относится.
    tx_разгон = {"transaction": {"message": {"accountKeys": [
        {"pubkey": "РАЗГОН"}, {"pubkey": "ЧУЖОЙ"}]}}, "meta": {"err": None},
        "slot": 11}
    h4 = HeliusЗаглушка({"A": tx_разгон, "B": tx_обычная})
    р4 = проверить_канал(h4, читать_журнал(ж, {"rabbit_ams"})["rabbit_ams"],
                          {"НАШ"}, выборка=10, прочие={"РАЗГОН"})
    chk("сообщение разгонного не считается чужим",
        р4["with_booster"] == 1 and р4["without_any"] == 0, р4)
    chk("и разложено по фильтрам отдельно",
        р4["by_group"]["src0"]["with_booster"] == 1
        and р4["by_group"]["boost0"]["with_our"] == 1, р4["by_group"])
    chk("выборка берётся по каждому фильтру, а не первые подряд",
        {и for и, _ in выборка_по_группам(
            читать_журнал(ж, {"rabbit_ams"})["rabbit_ams"], 1)} == {"src0", "boost0"},
        выборка_по_группам(читать_журнал(ж, {"rabbit_ams"})["rabbit_ams"], 1))

    print(f"самопроверка проверки RabbitStream: {всего[1]}/{всего[0]}"
           f"{' пройдено' if всего[1] == всего[0] else ' ПРОВАЛ'}")
    if всего[1] != всего[0]:
        raise SystemExit(1)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--journal", default="")
    p.add_argument("--channels", default="rabbit_ams,rabbit_fra,grpc,grpc2")
    p.add_argument("--tasks", default="BATCH-5,BATCH-3")
    p.add_argument("--config", default="data/final/20260923T145755Z/konfig.json")
    p.add_argument("--sample", type=int, default=40,
                    help="сколько подписей проверять НА КАЖДЫЙ фильтр")
    p.add_argument("--boosters", default="",
                    help="разгонные адреса через запятую: их сообщения "
                          "законны, но к источникам отношения не имеют")
    p.add_argument("--out", default="data/feed_rabbit_verify.json")
    a = p.parse_args()
    if a.self_test:
        self_test()
        return 0
    if not a.journal:
        print("нужен --journal", file=sys.stderr)
        return 2
    задачи = tuple(x.strip() for x in a.tasks.split(",") if x.strip())
    ист, откуда = BD.источники(задачи, Path(a.config))
    наши = set(ист)
    разгонные = {x.strip() for x in a.boosters.split(",") if x.strip()}
    print(f"адресов источников: {len(наши)}, откуда: {откуда}; "
           f"разгонных: {len(разгонные)}")
    каналы = {x.strip() for x in a.channels.split(",") if x.strip()}
    журнал = читать_журнал(Path(a.journal), каналы)
    helius = BD.Helius(служба="feed_rabbit_verify")
    итог = {"built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "sources_from": откуда, "our_addresses": len(наши),
             "sample": a.sample, "channels": {}}
    for канал in sorted(журнал):
        р = проверить_канал(helius, журнал[канал], наши, выборка=a.sample,
                             прочие=разгонные)
        итог["channels"][канал] = р
        print(f"{канал}: сообщений {р['messages']}, разных подписей "
               f"{р['unique']}, повторов {р['duplicates']}")
        print(f"   по фильтрам (все сообщения): "
               f"{json.dumps(р['by_filter'], ensure_ascii=False)}")
        for имя_ф, гр in sorted(р["by_group"].items()):
            n = гр["checked"] or 1
            print(f"   фильтр {имя_ф}: проверено {гр['checked']}, "
                   f"с адресом источника {гр['with_our']} "
                   f"({гр['with_our'] / n * 100:.1f} %), с разгонным "
                   f"{гр['with_booster']}, ни с одним нашим "
                   f"{гр['without_any']}, упало {гр['failed']} "
                   f"({гр['failed'] / n * 100:.1f} %), нет в цепи "
                   f"{гр['not_on_chain']}")
        print(f"   всего по каналу: только через ALT {р['only_via_alt']}, "
               f"ни с одним нашим адресом {р['without_any']}")
        for пример in р["examples_without_any"]:
            print(f"   ни одного нашего адреса: {пример['signature'][:14]} "
                   f"(фильтр {пример['filter']}), слот {пример['slot']}, "
                   f"счетов {пример['accounts']}, упала {пример.get('err')}")
            print(f"      плательщик {str(пример.get('fee_payer'))[:12]}, "
                   f"программы {[p[:10] for p in (пример.get('programs') or [])]}")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(итог, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    print(f"записано: {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
