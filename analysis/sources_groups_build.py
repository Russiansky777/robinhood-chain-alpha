#!/usr/bin/env python3
"""Список источников по группам -- ИЗ СЛОВ ВЛАДЕЛЬЦА и из сохранённых данных.

Адреса групп bloom_lane (BATCH-3/5) сюда не вписываются: они приходят из DBot
живьём, и второй список означал бы два списка, которые разойдутся молча.
"""
import json
from collections import defaultdict
from pathlib import Path

# --- 1.1. Полоса 0.05 SOL, Bloom по ним НЕ торгует. Ровно 38 адресов.
ГРУППЫ_ПОЛОСЫ = {
    "BATCH-1": ["2heJbC32Tpfcb3nbUb5ER61K11FGZVfVGtVnDm6LDogF",
                 "HDixbrzwwLXczhDBk1JVrurPQsuLE8FUKnW2pucSXN3o",
                 "5FGoPPj1nL8LCnfVnpTmreqQtqLuMXXAwuS1uahMrp8V",
                 "4ugDhHJ8XDXAeABmrNmGffFaLbJb9BkPyiFGVSV9ocwo",
                 "2yXwy5Dsa1XtEXcsrkFVRJeyuWD3qKkMN3pP3p5VTW3V",
                 "H2QSGECp13sFLJgdTsDtayX3dk18Dm6sQMSQKcew7Xzk",
                 "jrbGz3zoBJfptXF967xp6h5TBRV55ArvGeWxTQVs5Ux",
                 "2xUbYAVq1oJGj45d6JjnaYHAke3NQecUcqWvvVbwmYw8",
                 "J9WiAZKf8JnCkHFL8fLCCXdEgdoLjLRqU2EGsDjdqYga",
                 "8f39XhhZoRD8sYb6K5K9N7iSHXkL7BDmFFQNDF3TtsEr"],
    "BATCH-2": ["F5hkYsi8JxjyA2JHN5CA7MbnnhWubkXB2ZQB7Gkaxqs6",
                 "DCeH3aCsstGUSxQqS72VBZwTydoor1nQ6dWaxrgGQk39",
                 "GFRjGNXY8JrGSPC46inqrH4XPdUFMDLkE1oNm1nXiPsJ",
                 "7iPPqPyrqcmfenRs4xZ72ab4pyuUofXB5YaQB83WJmT9",
                 "7sQJttJLutWjHkxbusTgE4GpSj5z4fegouv2USHDFN2H",
                 "9CNyLECt2j8tnDhqxtjYk5HUhZ2b8Nwnyb7sfYN7vND2",
                 "8RCEq8RrBJ1G6eji9vZqjtjQgkDjUHMysPtynZENWo7S",
                 "5dB6rj9CoXMLQCAymoC5UXCb1LtFjbM5rbut3MNuj9Q",
                 "33vFB2rtG9FDpJReNaJrBF4RZgri5HTVo5gSxP1XfVCr"],
    "BATCH-4": ["Xk9onqHkpULDEYYN9ZPyM7Q9AfTNYUrsCkzywyqdMeb",
                 "EURKJdQmbP2GqSBssKuhgLKz2XEwhUXnHL2oYd4eejMm",
                 "9mLaswxfkdjWcbfmotYRfBVSEfUddmbfebKEaNggfsSR",
                 "29omQei1ywcRzHMYXx3iEEgfZ7ccqB7TpwL3q4s3RWe3",
                 "5eiXSFxmZKKo8V6qTw72SBskCZa7ok8jyqutsvtCuE1i"],
    "BATCH-6": ["5VRgqb2qbVqaWVGsM2k1b2bnPJk7up2xYbn4ziEjFgNt",
                 "FWSPXhQAD57hXJBNsWRkYupwQ4grQrr75JiWM6CqzHva",
                 "5pHeNsWMVEi1cbMzLhgqABnhEUwRTSzy5vBfeGWyJfxS",
                 "FF6vsaUcp4s95Q5NNm7VZ7deZU6gBU2vZmaiyQ36Ymrc",
                 "6MwHc79vgWXeMVyZwgQ13bqqudRNCYkvjmgQzc294g8M",
                 "ChgLZt4oJsgT1JEde8vmT2EcSJSqF38aB9PzYNtNDact"],
    "BATCH-7": ["9djgawmgpGrzt7DQoJ6tA2YW4gQyt3yH19uVZ3e2T3JJ",
                 "AUYLj5kLGUadmhn8kM91KnTJYfHiXGzcQ7TViP8uLLQv",
                 "DPAghrNgkW4m5HMBL3kVsiCm9ywhrBtCutc6tSSvicx8",
                 "Dw3LY8NnG3BB1EEXCNJwAKtVoEE7faEYiygYNHfFPNb1",
                 "BBcvxEyptYWSxgse7ESKWRxMRkpp9uE7wpCxUm6YqEhN",
                 "4KFjw2xfH4cXJJKjG1jDZRNphctZPMoFz3K6r3bAVtmD",
                 "AMcHYqUj4HPUdkDkEatguugNSmKaQTdvxzexzqsXDU4r",
                 "B6e9vDURyhMsnHDVZQTDLDz1cvW298SpET5LeaLcA9sn"],
}

# --- 1.2а. Только скорость: снятые ПО СИГНАЛУ, а не за налог. 12 адресов.
СКОРОСТЬ_ПО_СИГНАЛУ = [
    "CzU8MaRcwvwUoNkwJFLbvtFWJugcEXAhDDQqNFE4ybb7",
    "498g1rVnFcnjBjpfw1xyqA1WvgQXUU8RWuELjxkjAayQ",
    "7xPDo97sb5Ueb7zuR1k4A7PBW5hLwhPGsPzj1nA8J7PQ",
    "ABNaeejNKoKa6Vd5SYyP8wgX8kkMYKtc7HGNWdFVi1PP",
    "7XFo82igqSfBjb6SXJigbkhdFGFmVrh46zj3rvCPBK9n",
    "CbtBvpGhqX7kJ6eXkNWcy7ES9B7xK2HTfbpMqLojCRd5",
    "HtuYE3nYd7y9vxqiUjZdjTscGiFKt5JCcjD41vFwuebT",
    "7PFdKg9HgcjXh9fodFp6BCSGxqXcDSHAizotW6umE7aN",
    "2L17Sw85Lh2Ca7ejEzWqyJsZ3yS4DxQhYr52X5H5xRns",
    "Bra5EH8vqm5bmGupdEHWcwNepetuvSuW2z4excQR5yNs",
    "BXLEfLN8bHsQCn81rFZebcavP1JSory1XbSDoeYQxJPc",
    "57aiHCweKvWmAspN26FVV6kKLY8opJGYgw5iN3nPESk9",
]
# --- 1.2б. Снайперы, названные владельцем прямо.
СНАЙПЕРЫ_НАЗВАНЫ = ["7JVQMwRj82STgsG57spj6vpE6XY3RqG8B64PczVc7jJr",
                      "GUiSJYdAs5nyPcTkyZbnnEsL4R6VJKzemcUQgnHWBCgq"]
# Налоговые маршруты -- НЕ добавлять (слово владельца).
НЕ_ДОБАВЛЯТЬ = ["Et9jbvxvwKrKW5ZYJudU6HxLpHfCBVR4dR3yzWDqH1Zo",
                 "BMgsHTvcVYoRqzXpRnXfFWDSPqWbQHCYSqJBkNNeKdKh",
                 "DmopudSGRTNQFzgqjQxGKvSHNGgVgUYCTLLLKNPVbHXJ"]
ПРЕФИКСЫ_НЕ_ДОБАВЛЯТЬ = ("Et9jbvxv", "BMgsHTvc", "DmopudSG")
НАШИ = ["4s87RRC2V2XAJD6R8U2dP8kQH99Z2wA6fg88ZVfV4j4N",
         "21DqHDDPEfMhK1dHRkV9E8v8KTTSKQGApJAr1irC9j7w"]
СНИМОК_ЗАДАЧ = "data/final/20260923T145755Z/konfig.json"


def кошельки_задач(путь: str = СНИМОК_ЗАДАЧ) -> dict:
    """{адрес кошелька задачи DBot: имя задачи}.

    ЗАЧЕМ ЭТО ЗДЕСЬ. В сохранённом кэше З1 (block_buyers) среди покупателей в
    блоке источника сидит НАШ ЖЕ бот: DBot покупает по сигналу, значит его
    кошелёк попадает в S+0 постоянно и оказывается в самом верху списка
    "снайперов". 25.09 так и вышло: пять из десяти отобранных адресов --
    кошельки задач BATCH-3, BATCH-4, BATCH-5, BATCH-6 и BATCH-7. Копировать их
    значит копировать самих себя: полоса покупала бы тот же токен вторым
    кошельком, а замер скорости мерил бы нас же.
    """
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    import bloom_detector as D  # noqa: PLC0415

    п_ = Path(путь)
    if not п_.exists():
        п_ = Path("..") / путь
    д = json.loads(п_.read_text(encoding="utf-8"))
    из_ = {}
    for з in (D.тело_ответа(д).get("res") or []):
        if isinstance(з, dict) and з.get("walletAddress"):
            из_[з["walletAddress"]] = з.get("name")
    return из_
# Сколько снайперов из сохранённых данных берём сверх названных. Предел не
# кредитами, а деньгами: потолок speed_only 0.2 SOL в сутки при 0.01 на сделку
# -- это 20 сделок, и сотня источников сожгла бы их за минуты.
СКОЛЬКО_СНАЙПЕРОВ = 8


def попадания_s0() -> dict:
    """{адрес: сколько раз покупал В ТОМ ЖЕ слоте, что источник}.

    Источник -- сохранённый кэш З1 (data/solana_entry_log_offsets_cache.json,
    ключ block_buyers): ключ там slot:mint самого входа источника, значит все
    покупатели в этом списке -- это ровно S+0.
    """
    д = json.loads(Path("data/solana_entry_log_offsets_cache.json")
                   .read_text(encoding="utf-8"))
    где = defaultdict(set)
    for ключ, ряд in (д.get("block_buyers") or {}).items():
        for з in ряд or []:
            если = з.get("owner")
            if если:
                где[если].add(ключ)
    return {а: len(к) for а, к in где.items()}


def служебные() -> set:
    сл = json.loads(Path("data/solana_fast_buyers_services.json")
                    .read_text(encoding="utf-8"))
    return {с.get("address_or_program") for с in (сл.get("summary_table_top") or [])}


def собрать() -> dict:
    поп = попадания_s0()
    служ = служебные()
    наши_задачи = кошельки_задач()
    занято = (set(НАШИ) | set(НЕ_ДОБАВЛЯТЬ) | служ | set(СКОРОСТЬ_ПО_СИГНАЛУ)
              | set(наши_задачи))
    for ряд in ГРУППЫ_ПОЛОСЫ.values():
        занято |= set(ряд)
    снайперы = []
    выкинуты = []
    for адрес in СНАЙПЕРЫ_НАЗВАНЫ:
        если_наш = наши_задачи.get(адрес)
        if если_наш:
            # Названный владельцем адрес оказался НАШИМ кошельком задачи -- в
            # источники он не идёт, и об этом сказано, а не замолчано.
            выкинуты.append({"address": адрес, "why": f"кошелёк нашей задачи {если_наш}"})
            continue
        снайперы.append({"address": адрес, "s0_hits": поп.get(адрес, 0),
                          "why": "назван владельцем прямо"})
        занято.add(адрес)
    отобранные = sorted(((а, n) for а, n in поп.items()
                          if n >= 3 and а not in занято
                          and not а.startswith(ПРЕФИКСЫ_НЕ_ДОБАВЛЯТЬ)),
                         key=lambda п: (-п[1], п[0]))
    for адрес, n in отобранные[:СКОЛЬКО_СНАЙПЕРОВ]:
        снайперы.append({"address": адрес, "s0_hits": n,
                          "why": "из сохранённых данных З1: попаданий в S+0"})
    адреса_полосы = {}
    for партия, ряд in ГРУППЫ_ПОЛОСЫ.items():
        for а in ряд:
            адреса_полосы[а] = партия
    return {
        "generated_utc": "2026-09-25",
        "decision": ("Сводный промпт владельца 25.09 15:45 Madrid, пункты 1.1 и "
                      "1.2. Полоса слушает всех; Bloom торгует ТОЛЬКО по "
                      "BATCH-3/5, которые приходят из DBot живьём и в этот файл "
                      "не вписаны нарочно: два списка разошлись бы молча."),
        "sources_s0_data": ("data/solana_entry_log_offsets_cache.json "
                             "(block_buyers: ключ slot:mint входа источника, "
                             "значит его покупатели -- ровно S+0); служебные "
                             "адреса исключены по "
                             "data/solana_fast_buyers_services.json"),
        "groups": {
            "bloom_lane": {
                "lane_sol": 0.05,
                "bloom_trades": True,
                "fanout": True,
                "note": ("BATCH-3/5 плюс лидер: Bloom торгует 0.2 SOL, полоса "
                          "0.05 SOL (пункты 2 и 3). Адресов здесь нет нарочно -- "
                          "они приходят из DBot живьём; это группа по умолчанию "
                          "для всякого источника, которого нет в других группах."),
                "addresses": {},
            },
            "lane_only": {
                "lane_sol": 0.05,
                "bloom_trades": False,
                "fanout": True,
                "note": ("Пункт 1.1: 38 активных кошельков реестра, которых "
                          "детектор ещё не слушал. Полоса мерит слот, место и "
                          "кто довёз; в деньги входят с меткой группы."),
                "credits_rule": ("потолок детектора 300 000 кредитов в сутки; "
                                  "при 70 % снять BATCH-1 и BATCH-4 -- у них "
                                  "почти нет крупных покупок"),
                "drop_first_at_70pct": ["BATCH-1", "BATCH-4"],
                "addresses": адреса_полосы,
            },
            "speed_only": {
                "lane_sol": 0.01,
                "bloom_trades": False,
                "fanout": False,
                "day_cap_sol": 0.2,
                "stop_loss_sol": 0.15,
                "note": ("Пункт 1.2: только для скорости, через пул БЕЗ веера, "
                          "в деньги не входит. Потолок 0.2 SOL в сутки "
                          "(покупки плюс чаевые), стоп при -0.15 SOL."),
                "by_signal": [{"address": а, "why": "снят по сигналу, не за налог"}
                               for а in СКОРОСТЬ_ПО_СИГНАЛУ],
                "snipers": снайперы,
            },
        },
        "not_added": [{"prefix": п, "why": "налоговый маршрут (слово владельца)"}
                       for п in ПРЕФИКСЫ_НЕ_ДОБАВЛЯТЬ]
                      + [{"address": а, "why": f"кошелёк нашей задачи {и}"}
                          for а, и in sorted(наши_задачи.items())]
                      + выкинуты,
        "our_task_wallets_excluded": len(наши_задачи),
    }


if __name__ == "__main__":
    итог = собрать()
    Path("data/sources_2026-09-25.json").write_text(
        json.dumps(итог, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    г = итог["groups"]
    print("lane_only адресов:", len(г["lane_only"]["addresses"]))
    print("speed_only по сигналу:", len(г["speed_only"]["by_signal"]),
          "снайперов:", len(г["speed_only"]["snipers"]))
    for с in г["speed_only"]["snipers"]:
        print(f"  {с['address']}  S+0 {с['s0_hits']}  -- {с['why']}")
