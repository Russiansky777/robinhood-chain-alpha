#!/usr/bin/env python3
"""II.14 -- ГЕОГРАФИЯ ЛИДЕРА СЛОТА. Только чтение, ни одной отправки.

Вопрос владельца: зависит ли доля S+0 от того, В КАКОМ РЕГИОНЕ стоял лидер
слота, в котором села транзакция источника. Если зависит -- есть смысл
выбирать точку входа отправителя по расписанию лидеров (III.14).

Откуда берутся числа, чтобы ничего не выдумывать:
  * покупки -- журнал позиций с хоста (positions.jsonl), поля source_slot,
    own_tx_seen_slot/our_slot, block_index/block_total уже посчитаны живым
    кодом; здесь они только читаются;
  * лидер слота -- getLeaderSchedule за нужную эпоху (расписание эпохи, а не
    догадка): ответ даёт для каждого лидера НОМЕРА СЛОТОВ ОТ НАЧАЛА ЭПОХИ;
  * адрес лидера -- getClusterNodes (gossip/tpu), то есть то, чем узел
    представляется сети;
  * регион по адресу -- публичный геосервис (ip-api.com/batch, запасной
    ipinfo.io/<ip>/json). Источник каждого ответа записывается рядом с
    ответом: "источник" в словаре гео.

Задержка "источник сел -> мы увидели" в журналах хранится не в миллисекундах:
есть слот источника, слот сети на момент решения и возраст этого слота
(net_slot_age_s). Поэтому считается ОЦЕНКА:

    задержка_мс = (слот_сети_при_решении - слот_источника) * 400 + возраст * 1000

400 мс -- номинальная длина слота Solana. Это честная оценка с точностью до
дрожания длины слота, и так она и подписана в отчёте.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

СЛОТ_МС = 400.0
СЛОТОВ_В_ЭПОХЕ = 432_000

# Страны Европы -- для раскладки по регионам. Список нужен только чтобы
# отнести ответ геосервиса к одному из четырёх вёдер владельца.
ЕВРОПА = {
    "AD", "AL", "AT", "AX", "BA", "BE", "BG", "BY", "CH", "CY", "CZ", "DE",
    "DK", "EE", "ES", "FI", "FO", "FR", "GB", "GG", "GI", "GR", "HR", "HU",
    "IE", "IM", "IS", "IT", "JE", "LI", "LT", "LU", "LV", "MC", "MD", "ME",
    "MK", "MT", "NL", "NO", "PL", "PT", "RO", "RS", "RU", "SE", "SI", "SK",
    "SM", "UA", "VA",
}
АЗИЯ = {
    "AE", "AM", "AZ", "BD", "BH", "BN", "BT", "CN", "GE", "HK", "ID", "IL",
    "IN", "IQ", "IR", "JO", "JP", "KG", "KH", "KP", "KR", "KW", "KZ", "LA",
    "LB", "LK", "MM", "MN", "MO", "MV", "MY", "NP", "OM", "PH", "PK", "PS",
    "QA", "SA", "SG", "SY", "TH", "TJ", "TM", "TR", "TW", "UZ", "VN", "YE",
}


# ------------------------------------------------------------------ покупки

def _наш_слот(п: dict):
    return п.get("own_tx_seen_slot") or п.get("our_slot")


def покупки(путь: Path, с_utc: str) -> list:
    """Покупки из журнала позиций с отметкой, чьи они: полосы или Bloom."""
    из_ = []
    # Журнал читается ПОТОКОМ, строка за строкой: правило владельца 25.09 --
    # журналы в память не грузить (635 МБ решений уже роняли хост).
    ф = путь.open(encoding="utf-8", errors="replace")
    for строка in ф:
        строка = строка.strip()
        if not строка:
            continue
        try:
            п = json.loads(строка)
        except json.JSONDecodeError:
            continue
        когда = (п.get("ts_intent_utc") or "")
        if с_utc and когда < с_utc:
            continue
        слот = п.get("source_slot")
        if not слот:
            continue
        из_.append({
            "чья": "полоса" if п.get("lane") == "own_send" else "bloom",
            "группа": п.get("lane_group") or п.get("source_task") or "",
            "когда": когда,
            "source_sig": п.get("source_sig"),
            "source_slot": int(слот),
            "our_slot": _наш_слот(п),
            "block_index": п.get("block_index"),
            "block_total": п.get("block_total"),
            "sol_in": п.get("sol_in"),
            "state": п.get("state"),
        })
    ф.close()
    # В журнале позиция дописывается много раз -- берём последнюю запись
    # по каждому client_order_id... но его тут уже нет, поэтому сворачиваем по
    # паре (подпись источника, чья): последняя запись самая полная.
    свёрнуто = {}
    for з in из_:
        свёрнуто[(з["source_sig"], з["чья"])] = з
    return sorted(свёрнуто.values(), key=lambda з: з["когда"])


def цель(з: dict) -> bool | None:
    """S+0 в любом месте ИЛИ голова S+1 (место <= 100). Иначе нет.

    None -- посчитать нечем: слот наш неизвестен, или это S+1 без места.
    """
    наш, их = з.get("our_slot"), з.get("source_slot")
    if not наш or not их:
        return None
    if наш == их:
        return True
    if наш == их + 1:
        место = з.get("block_index")
        if место is None:
            return None
        return место <= 100
    return False


# --------------------------------------------------------------------- RPC

class RPC:
    def __init__(self, url: str, пауза: float = 0.2):
        import requests  # noqa: PLC0415

        self._s = requests.Session()
        self.url = url
        self.пауза = пауза
        self.вызовов = 0

    def call(self, метод: str, параметры: list, таймаут: float = 120.0):
        тело = {"jsonrpc": "2.0", "id": self.вызовов + 1,
                 "method": метод, "params": параметры}
        ответ = self._s.post(self.url, json=тело, timeout=таймаут)
        self.вызовов += 1
        time.sleep(self.пауза)
        ответ.raise_for_status()
        д = ответ.json()
        if "error" in д:
            raise RuntimeError(f"{метод}: {д['error']}")
        return д.get("result")


def расписание_эпохи(rpc: RPC, слот: int) -> dict:
    """{абсолютный слот: лидер} для эпохи, в которую попадает слот."""
    начало = (слот // СЛОТОВ_В_ЭПОХЕ) * СЛОТОВ_В_ЭПОХЕ
    сырое = rpc.call("getLeaderSchedule", [начало])
    если_пусто = {}
    if not сырое:
        return если_пусто
    из_ = {}
    for лидер, индексы in сырое.items():
        for и in индексы:
            из_[начало + и] = лидер
    return из_


def узлы(rpc: RPC) -> dict:
    """{pubkey: адрес} по getClusterNodes. Берём gossip, иначе tpu."""
    из_ = {}
    for у in rpc.call("getClusterNodes", []) or []:
        точка = у.get("gossip") or у.get("tpu") or у.get("rpc")
        if not точка:
            continue
        адрес = точка.rsplit(":", 1)[0].strip("[]")
        из_[у.get("pubkey")] = адрес
    return из_


# ---------------------------------------------------------------------- гео

def гео_пакетом(адреса: list) -> dict:
    """ip-api.com/batch -- до 100 адресов за запрос, ключа не нужно."""
    import requests  # noqa: PLC0415

    из_ = {}
    поля = "status,message,country,countryCode,regionName,city,lat,lon,isp,as,query"
    for i in range(0, len(адреса), 100):
        кусок = адреса[i:i + 100]
        try:
            о = requests.post(f"http://ip-api.com/batch?fields={поля}",
                               json=[{"query": а} for а in кусок], timeout=30)
            о.raise_for_status()
            for запись in о.json():
                if запись.get("status") == "success":
                    запись["источник"] = "ip-api.com"
                    из_[запись.get("query")] = запись
        except Exception as exc:  # noqa: BLE001
            print(f"  ip-api не ответил по куску {i}-{i + len(кусок)}: "
                   f"{type(exc).__name__}: {exc}", file=sys.stderr)
        time.sleep(4.5)  # бесплатный тир: 15 запросов в минуту
    return из_


def гео_поштучно(адреса: list, уже: dict) -> dict:
    """Запасной путь: ipinfo.io/<ip>/json без ключа (так же, как в task5)."""
    import requests  # noqa: PLC0415

    из_ = {}
    for а in адреса:
        if а in уже:
            continue
        try:
            о = requests.get(f"https://ipinfo.io/{а}/json", timeout=10)
            if о.status_code != 200:
                continue
            д = о.json()
            шир, дол = (д.get("loc") or ",").split(",")[:2]
            из_[а] = {"query": а, "country": д.get("country"),
                       "countryCode": д.get("country"),
                       "regionName": д.get("region"), "city": д.get("city"),
                       "lat": float(шир) if шир else None,
                       "lon": float(дол) if дол else None,
                       "isp": д.get("org"), "as": д.get("org"),
                       "источник": "ipinfo.io"}
        except Exception:  # noqa: BLE001, S110
            pass
    return из_


def регион(г: dict | None) -> str:
    if not г:
        return "неизвестно"
    код = (г.get("countryCode") or "").upper()
    if код in ЕВРОПА:
        return "EU"
    if код in АЗИЯ:
        return "Asia"
    if код in ("US", "CA"):
        дол = г.get("lon")
        if дол is None:
            return "US-?"
        return "US-West" if float(дол) < -100.0 else "US-East"
    if not код:
        return "неизвестно"
    return f"прочее ({код})"


# ------------------------------------------------------- задержка по журналу

def решения(путь: Path) -> dict:
    """{подпись источника: запись решения} из отобранных строк журнала."""
    из_ = {}
    if not путь or not путь.exists():
        return из_
    with путь.open(encoding="utf-8", errors="replace") as ф:
        for строка in ф:
            строка = строка.strip()
            if not строка:
                continue
            try:
                д = json.loads(строка)
            except json.JSONDecodeError:
                continue
            подпись = д.get("signature")
            if подпись and д.get("source_slot"):
                из_.setdefault(подпись, д)
    return из_


def задержка_мс(д: dict) -> float | None:
    """Оценка "источник сел -> мы увидели" (см. докстринг модуля)."""
    сеть = д.get("net_slot_at_decision")
    ист = д.get("source_slot")
    возраст = д.get("net_slot_age_s")
    if сеть is None or ист is None or возраст is None:
        return None
    отставание = max(0, int(сеть) - int(ист))
    return отставание * СЛОТ_МС + float(возраст) * 1000.0


# ------------------------------------------------------------------- сводка

def сводка(ряды: list) -> dict:
    по_регионам = {}
    for з in ряды:
        р = з["регион"]
        в = по_регионам.setdefault(р, {"n": 0, "s0": 0, "цель_да": 0,
                                        "цель_считаемо": 0, "задержки": [],
                                        "лидеры": set()})
        в["n"] += 1
        if з.get("our_slot") and з["our_slot"] == з["source_slot"]:
            в["s0"] += 1
        ц = з.get("цель")
        if ц is not None:
            в["цель_считаемо"] += 1
            в["цель_да"] += 1 if ц else 0
        if з.get("задержка_мс") is not None:
            в["задержки"].append(з["задержка_мс"])
        if з.get("лидер"):
            в["лидеры"].add(з["лидер"])
    из_ = {}
    for р, в in по_регионам.items():
        из_[р] = {
            "покупок": в["n"],
            "лидеров": len(в["лидеры"]),
            "s0": в["s0"],
            "доля_s0": round(в["s0"] / в["n"], 4) if в["n"] else None,
            "в_цели": в["цель_да"],
            "цель_считаемо": в["цель_считаемо"],
            "доля_в_цели": (round(в["цель_да"] / в["цель_считаемо"], 4)
                             if в["цель_считаемо"] else None),
            "задержка_медиана_мс": (round(statistics.median(в["задержки"]), 1)
                                     if в["задержки"] else None),
            "задержек_посчитано": len(в["задержки"]),
        }
    return dict(sorted(из_.items(), key=lambda кв: -кв[1]["покупок"]))


def _проц(х) -> str:
    return "—" if х is None else f"{100 * float(х):.1f} %"


def таблица(св: dict, заголовок: str) -> str:
    строки = [f"### {заголовок}", "",
               "| регион лидера | покупок | лидеров | S+0 | доля S+0 | "
               "в цели | считаемо | доля в цели | "
               "медиана «сел → увидели», мс | задержек посчитано |",
               "|---|---|---|---|---|---|---|---|---|---|"]
    for р, в in св.items():
        строки.append(
            f"| {р} | {в['покупок']} | {в['лидеров']} | {в['s0']} | "
            f"{_проц(в['доля_s0'])} | {в['в_цели']} | {в['цель_считаемо']} | "
            f"{_проц(в['доля_в_цели'])} | "
            f"{'—' if в['задержка_медиана_мс'] is None else в['задержка_медиана_мс']} | "
            f"{в['задержек_посчитано']} |")
    return "\n".join(строки)


# -------------------------------------------------------------------- запуск

def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--positions", required=True)
    р.add_argument("--decisions", default="")
    р.add_argument("--since", default="2026-09-24T00:00")
    р.add_argument("--rpc", default="")
    р.add_argument("--out-md", default="data/leader_geo.md")
    р.add_argument("--out-json", default="data/leader_geo.json")
    а = р.parse_args()

    ряды = покупки(Path(а.positions), а.since)
    print(f"покупок с {а.since}: {len(ряды)} "
           f"(полоса {sum(1 for з in ряды if з['чья'] == 'полоса')}, "
           f"bloom {sum(1 for з in ряды if з['чья'] == 'bloom')})")
    if not ряды:
        print("нечего считать")
        return 1

    реш = решения(Path(а.decisions)) if а.decisions else {}
    print(f"решений подобрано по подписи: {len(реш)}")

    rpc = RPC(а.rpc)
    слоты = sorted({з["source_slot"] for з in ряды})
    эпохи = sorted({с // СЛОТОВ_В_ЭПОХЕ for с in слоты})
    расписание = {}
    for э in эпохи:
        часть = расписание_эпохи(rpc, э * СЛОТОВ_В_ЭПОХЕ)
        print(f"эпоха {э}: слотов в расписании {len(часть)}")
        расписание.update(часть)

    адреса_узлов = узлы(rpc)
    print(f"узлов в getClusterNodes: {len(адреса_узлов)}")

    for з in ряды:
        з["лидер"] = расписание.get(з["source_slot"])
        з["адрес_лидера"] = адреса_узлов.get(з["лидер"]) if з["лидер"] else None
        з["цель"] = цель(з)
        д = реш.get(з["source_sig"])
        з["задержка_мс"] = задержка_мс(д) if д else None

    нужны = sorted({з["адрес_лидера"] for з in ряды if з["адрес_лидера"]})
    print(f"разных адресов лидеров: {len(нужны)}")
    гео = гео_пакетом(нужны)
    гео.update(гео_поштучно(нужны, гео))
    print(f"гео получено по {len(гео)} адресам из {len(нужны)}")

    for з in ряды:
        з["гео"] = гео.get(з["адрес_лидера"])
        з["регион"] = регион(з["гео"])

    полоса = [з for з in ряды if з["чья"] == "полоса"]
    блум = [з for з in ряды if з["чья"] == "bloom"]
    итог = {
        "since": а.since,
        "покупок": len(ряды),
        "лидер_известен": sum(1 for з in ряды if з["лидер"]),
        "адрес_известен": sum(1 for з in ряды if з["адрес_лидера"]),
        "гео_известно": sum(1 for з in ряды if з["гео"]),
        "сводка_все": сводка(ряды),
        "сводка_полоса": сводка(полоса),
        "сводка_bloom": сводка(блум),
        "ряды": ряды,
    }
    Path(а.out_json).write_text(
        json.dumps(итог, ensure_ascii=False, default=str, indent=1),
        encoding="utf-8")

    куски = [f"# II.14 География лидера слота, покупки с {а.since}", "",
              f"Покупок: {len(ряды)} (полоса {len(полоса)}, Bloom {len(блум)}). "
              f"Лидер слота известен у {итог['лидер_известен']}, адрес узла у "
              f"{итог['адрес_известен']}, регион у {итог['гео_известно']}.", "",
              "«Сел → увидели» -- оценка: отставание слотов × 400 мс + возраст "
              "слота сети на момент решения. Миллисекунд в журнале нет.", "",
              таблица(итог["сводка_все"], "Все покупки"), "",
              таблица(итог["сводка_полоса"], "Только полоса"), "",
              таблица(итог["сводка_bloom"], "Только Bloom")]
    Path(а.out_md).write_text("\n".join(куски) + "\n", encoding="utf-8")
    print("\n".join(куски))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
