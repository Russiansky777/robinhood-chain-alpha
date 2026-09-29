#!/usr/bin/env python3
"""Карта "валидатор -> регион": то, по чему выбирается региональная точка.

ЗАЧЕМ. Региональная отправка (III.14) обязана знать, где сидит лидер слота.
Держать это знание в горячем пути нельзя: getClusterNodes отдаёт три тысячи
узлов, а гео по адресу -- это внешний запрос. Поэтому карта строится заранее
отдельным прогоном и кладётся в data/leader_regions.json, а полоса читает её
файлом.

Регион считается ТОЙ ЖЕ функцией, что и в разборе II.14
(analysis/leader_geo.регион): EU / US-East / US-West / Asia / прочее. Адрес
берётся из gossip узла, иначе tpu, иначе rpc -- как в II.14.

Только чтение: getClusterNodes и ip-api.com. Ни ключей, ни отправок.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(КОРЕНЬ / "analysis"))

import leader_geo as LG  # noqa: E402


def узел_вызов(url: str, метод: str, парам: list):
    тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                       "params": парам}).encode("utf-8")
    зап = urllib.request.Request(url, data=тело,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(зап, timeout=60) as отв:
        return json.loads(отв.read().decode("utf-8")).get("result")


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--out", default="data/leader_regions.json")
    р.add_argument("--only-schedule", action="store_true",
                   help="брать только лидеров текущей эпохи, а не весь кластер")
    а = р.parse_args()
    ключ = (os.environ.get("HELIUS_API_KEY") or os.environ.get("HELIUS_API") or "").strip()
    url = (f"https://mainnet.helius-rpc.com/?api-key={ключ}" if ключ
           else "https://api.mainnet-beta.solana.com")

    узлы = {}
    for у in (узел_вызов(url, "getClusterNodes", []) or []):
        точка = у.get("gossip") or у.get("tpu") or у.get("rpc")
        if not точка or not у.get("pubkey"):
            continue
        узлы[у["pubkey"]] = точка.rsplit(":", 1)[0].strip("[]")
    нужные = set(узлы)
    расписание = {}
    # ЭПОХА -- В КАРТУ, И БЕРЁТСЯ ОНА У УЗЛА, А НЕ ДЕЛЕНИЕМ СЛОТА. Найдено
    # 29.09 ночью: карта не несла эпохи вовсе, и проверить "свежая ли она для
    # нынешней эпохи" было нечем -- вывод из собрано_utc был бы догадкой.
    # absoluteSlot - slotIndex -- ровно то же начало эпохи, что считает
    # region_lidera_sdelok.границы_эпохи; деление на 432 000 врёт, когда длина
    # эпохи не круглая.
    эпоха = {"эпоха": None, "начало_эпохи": None, "слотов_в_эпохе": None,
              "why_not": None}
    инфо = узел_вызов(url, "getEpochInfo", [])
    if isinstance(инфо, dict) and isinstance(инфо.get("absoluteSlot"), int):
        нач = int(инфо["absoluteSlot"]) - int(инфо.get("slotIndex") or 0)
        эпоха.update(эпоха=инфо.get("epoch"), начало_эпохи=нач,
                      слотов_в_эпохе=инфо.get("slotsInEpoch"))
    else:
        эпоха["why_not"] = "getEpochInfo не отдался -- эпоха карты неизвестна"
    if а.only_schedule:
        начало = эпоха.get("начало_эпохи")
        if начало is None:
            слот = узел_вызов(url, "getSlot", []) or 0
            начало = (int(слот) // LG.СЛОТОВ_В_ЭПОХЕ) * LG.СЛОТОВ_В_ЭПОХЕ
        сырое = узел_вызов(url, "getLeaderSchedule", [начало]) or {}
        расписание = {л: len(и) for л, и in сырое.items()}
        нужные = set(расписание) & set(узлы)

    адреса = sorted({узлы[п] for п in нужные})
    print(f"узлов в кластере {len(узлы)}, спрашиваем гео по {len(адреса)} адресам",
          flush=True)
    гео = LG.гео_пакетом(адреса)
    по_лидеру, по_региону = {}, {}
    без_гео = 0
    for п in sorted(нужные):
        г = гео.get(узлы[п])
        if not г:
            без_гео += 1
            continue
        рег = LG.регион(г)
        по_лидеру[п] = рег
        по_региону[рег] = по_региону.get(рег, 0) + 1

    итог = {"собрано_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "эпоха": эпоха.get("эпоха"),
            "начало_эпохи": эпоха.get("начало_эпохи"),
            "слотов_в_эпохе": эпоха.get("слотов_в_эпохе"),
            "почему_нет_эпохи": эпоха.get("why_not"),
            "узлов_в_кластере": len(узлы), "лидеров_в_карте": len(по_лидеру),
            "без_гео": без_гео, "по_региону": по_региону,
            "слотов_у_лидера": расписание or None,
            "источник_гео": "ip-api.com (как в разборе II.14)",
            "по_лидеру": по_лидеру}
    путь = Path(а.out)
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_text(json.dumps(итог, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8")
    печать = {к: v for к, v in итог.items() if к not in ("по_лидеру", "слотов_у_лидера")}
    print(json.dumps(печать, ensure_ascii=False, indent=1))
    return 0 if по_лидеру else 1


if __name__ == "__main__":
    raise SystemExit(main())
