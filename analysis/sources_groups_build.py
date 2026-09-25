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
# Сколько снайперов из сохранённых данных берём сверх названных -- ДО
# окончательного отбора. Владелец 25.09 (вечер) поднял потолок speed_only до
# 1 SOL в сутки и попросил 30 кошельков, поэтому из кэша берётся заведомо
# больше кандидатов, а решает уже отбор по попаданиям в S+0 ниже. Годных
# подписантов в кэше 145, так что верхняя планка здесь не ограничивает выбор.
СКОЛЬКО_СНАЙПЕРОВ = 60

# СКОЛЬКО ВСЕГО АДРЕСОВ В ГРУППЕ СКОРОСТИ. Решение владельца 25.09 (вечер):
# "в список -- 30 кошельков с наибольшим числом попаданий в S+0 из оставшихся
# после чистки программных адресов (только подписанты, не наши). Цель -- 30+
# покупок в час". До этого было 8 -- по прежнему потолку кредитов 300 000;
# потолок детектора поднят до 1 000 000, а замер после снятия двух авторити
# пулов дал ~5 150 кредитов в час (прогноз ~124 000 в сутки), то есть место
# под тридцать источников есть с большим запасом.
#
# Отбор ИМЕННО ПО ПОПАДАНИЯМ В S+0: это и есть признак, ради которого группа
# заведена (скорость), и он посчитан по сохранённому кэшу З1, а не назначен.
СКОЛЬКО_СКОРОСТИ = 30


def может_подписывать(адрес: str) -> bool:
    """Может ли адрес подписывать транзакции САМ.

    Владелец 25.09: "Из speed_only убрать GpMZbSM2... -- это авторити пулов
    Raydium CPMM, не трейдер; в генератор одно исключение: адреса, которые не
    подписывают транзакции сами".

    Проверка не по данным, а по МАТЕМАТИКЕ: подписывать может только адрес,
    лежащий на кривой ed25519, то есть у которого есть закрытый ключ.
    Программные адреса (PDA -- авторити пулов, хранилища, служебные счёта)
    выведены так, что на кривой их НЕТ, и подписать они не могут никогда. Это
    решается локально и не зависит от того, попал ли адрес в наши сохранённые
    выборки: по данным GpMZbSM2 выглядел самым быстрым покупателем (488 раз
    хозяин токен-счёта в блоке источника), а подписантом не был ни разу --
    потому что и не может.
    """
    try:
        from solders.pubkey import Pubkey  # noqa: PLC0415

        return bool(Pubkey.from_string(адрес).is_on_curve())
    except Exception:  # noqa: BLE001
        # Не смогли посчитать -- НЕ выбрасываем адрес молча: пусть остаётся, а
        # причина видна в отчёте генератора.
        return True


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
        if not может_подписывать(адрес):
            выкинуты.append({"address": адрес,
                              "why": "не на кривой ed25519: подписывать не может"})
            continue
        снайперы.append({"address": адрес, "s0_hits": поп.get(адрес, 0),
                          "why": "назван владельцем прямо"})
        занято.add(адрес)
    не_подписывают = []
    годные = []
    for а, n in поп.items():
        if n < 3 or а in занято or а.startswith(ПРЕФИКСЫ_НЕ_ДОБАВЛЯТЬ):
            continue
        if not может_подписывать(а):
            # ПРОГРАММНЫЙ АДРЕС -- НЕ ИСТОЧНИК. Он появляется в блоке как
            # хозяин токен-счёта пула, а сделку делает кто-то другой.
            не_подписывают.append({"address": а, "s0_hits": n,
                                    "why": ("не на кривой ed25519: программный "
                                             "адрес, подписывать не может -- это "
                                             "авторити/хранилище, а не трейдер")})
            continue
        годные.append((а, n))
    отобранные = sorted(годные, key=lambda п: (-п[1], п[0]))
    for адрес, n in отобранные[:СКОЛЬКО_СНАЙПЕРОВ]:
        снайперы.append({"address": адрес, "s0_hits": n,
                          "why": "из сохранённых данных З1: попаданий в S+0"})
    адреса_полосы = {}
    for партия, ряд in ГРУППЫ_ПОЛОСЫ.items():
        for а in ряд:
            адреса_полосы[а] = партия
    # СОКРАЩЕНИЕ ГРУППЫ СКОРОСТИ ПО КРЕДИТАМ. Из всех отобранных (и снятых по
    # сигналу, и снайперов) оставляем СКОЛЬКО_СКОРОСТИ адресов с наибольшим
    # числом попаданий в S+0. Кто не прошёл -- назван в файле с его числом
    # попаданий: молча выкинутый источник потом не объяснить.
    все_скорости = ([{"address": а, "why": "снят по сигналу, не за налог",
                       "s0_hits": поп.get(а, 0)} for а in СКОРОСТЬ_ПО_СИГНАЛУ]
                     + [dict(с) for с in снайперы])
    для_всех = {}
    for з in все_скорости:
        для_всех.setdefault(з["address"], з)
    по_попаданиям = sorted(для_всех.values(),
                            key=lambda з: (-int(з.get("s0_hits") or 0),
                                            з["address"]))
    оставлены = по_попаданиям[:СКОЛЬКО_СКОРОСТИ]
    сняты = по_попаданиям[СКОЛЬКО_СКОРОСТИ:]
    по_сигналу_ост = [з for з in оставлены if "снят по сигналу" in з["why"]]
    снайперы_ост = [з for з in оставлены if "снят по сигналу" not in з["why"]]
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
                # BLOOM ТОРГУЕТ ПО ЭТИМ 38 КОШЕЛЬКАМ (решение владельца 25.09
                # вечером): "Bloom включить на 38 источников lane_only с
                # размером 0.05 SOL (на BATCH-3/5 остаётся 0.2). Цель -- пары
                # «Bloom против полосы» на одних сигналах". До этого Bloom по
                # ним не торговал вовсе, и пары сравнивать было не с чем.
                "bloom_trades": True,
                "bloom_sol": 0.05,
                "fanout": True,
                "note": ("Пункт 1.1: 38 активных кошельков реестра, которых "
                          "детектор ещё не слушал. Полоса мерит слот, место и "
                          "кто довёз; в деньги входят с меткой группы."),
                "credits_rule": ("потолок детектора 1 000 000 кредитов в сутки "
                                  "(решение владельца 25.09, поднят с 300 000). "
                                  "Сторож расхода батчи НОЧЬЮ НЕ РЕЖЕТ -- прежнее "
                                  "правило про снятие BATCH-1 и BATCH-4 при 70 % "
                                  "отменено тем же решением"),
                "addresses": адреса_полосы,
            },
            "speed_only": {
                "lane_sol": 0.01,
                "bloom_trades": False,
                "fanout": False,
                # ПОТОЛОК И СТОП ГРУППЫ СКОРОСТИ (решение владельца 25.09
                # вечером): "потолок 1 SOL в сутки (покупки + чаевые), стоп
                # -0.5 SOL". Было 0.2 и -0.15 -- при 0.01 на сделку это всего
                # 20 сделок, а цель теперь 30+ покупок в час.
                "day_cap_sol": 1.0,
                "stop_loss_sol": 0.5,
                # ПОРОГ ВХОДА ИСТОЧНИКА (владелец 25.09): "для speed_only порог
                # покупки источника 0.5 SOL-экв (деньги там не считаются, нужны
                # только сигналы); для lane-only и Bloom -- 2 SOL как было".
                "min_target_sol": 0.5,
                "note": ("Пункт 1.2: только для скорости, через пул БЕЗ веера, "
                          "в деньги не входит. Потолок 1 SOL в сутки "
                          "(покупки плюс чаевые), стоп при -0.5 SOL. Цель -- "
                          "30+ покупок в час."),
                "credits_rule": ("30 адресов с наибольшим числом попаданий в "
                                  "S+0 (только подписанты, не наши) по слову "
                                  "владельца 25.09 вечером; расход детектора "
                                  "после снятия двух авторити пулов ~5 150 "
                                  "кредитов в час, прогноз ~124 000 в сутки "
                                  "против потолка 1 000 000"),
                "by_signal": по_сигналу_ост,
                "snipers": снайперы_ост,
                "dropped_by_credits": сняты,
            },
        },
        "not_added": [{"prefix": п, "why": "налоговый маршрут (слово владельца)"}
                       for п in ПРЕФИКСЫ_НЕ_ДОБАВЛЯТЬ]
                      + [{"address": а, "why": f"кошелёк нашей задачи {и}"}
                          for а, и in sorted(наши_задачи.items())]
                      + выкинуты + не_подписывают,
        "our_task_wallets_excluded": len(наши_задачи),
        "not_signers_excluded": len(не_подписывают),
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
