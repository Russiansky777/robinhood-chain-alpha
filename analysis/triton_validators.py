#!/usr/bin/env python3
"""Кто из валидаторов BAM, кто Harmonic -- и какая доля слотов у них.

ЗАЧЕМ (слово владельца 28.09, дополнение по преконфам, п.2): "Валидаторы
BAM/Harmonic: getClusterNodes -> строка версии -> пометить BAM / Harmonic /
прочие; x getVoteAccounts (стейк) x расписание лидеров текущей эпохи -> доля
слотов BAM, Harmonic, всего. Обновлять раз в час. Число в признак жизни."
Ответ Triton на письмо: списка валидаторов они не дают, ориентир -- только
getClusterNodes.

ЧТО ЗДЕСЬ ЕСТЬ И ЧЕГО НЕТ.
  * Пометка идёт ПО СТРОКЕ ВЕРСИИ и только по ней -- ничего про клиента в
    getClusterNodes больше нет (в ответе: pubkey, gossip, tpu, rpc, version,
    featureSet, shredVersion).
  * Образцов строк версии я не придумываю. Правило пометки задаётся снаружи:
    TRITON_VERSII_BAM и TRITON_VERSII_HARMONIC -- подстроки через запятую,
    регистр не важен. Пока они пусты, ВСЕ узлы идут в "прочие", а в ответе
    лежит таблица по_версиям с живыми строками, стейком и слотами: правило
    ставится по ней, а не по памяти.
  * Расписание лидеров за эпоху НЕ МЕНЯЕТСЯ, поэтому оно берётся один раз на
    эпоху и лежит в файле; раз в час обновляются узлы и стейк. Иначе часовой
    опрос тянул бы по десятку мегабайт на пустом месте.

ДЕНЕГ ЗДЕСЬ НЕТ: только чтение цепи. Ни подписи, ни отправки.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

МЕТКА_BAM = "bam"
МЕТКА_HARMONIC = "harmonic"
МЕТКА_ПРОЧИЕ = "прочие"

ФАЙЛ_СНИМКА = "triton_validatory.json"
ФАЙЛ_РАСПИСАНИЯ = "triton_leader_schedule.json"
ЧАС_S = 3600.0


def ключ_helius(окружение=None) -> str:
    """Ключ из окружения, а если задан файл -- из файла.

    ФАЙЛОМ -- ЧТОБЫ КЛЮЧ НЕ ПОПАЛ В КОМАНДНУЮ СТРОКУ временной службы
    systemd: --setenv со значением виден в argv любому, кто смотрит ps.
    """
    ок = окружение if окружение is not None else os.environ
    к = (ок.get("HELIUS_API_KEY") or "").strip()
    if not к:
        путь = (ок.get("HELIUS_API_KEY_FILE") or "").strip()
        if путь:
            try:
                к = Path(путь).read_text(encoding="utf-8").strip()
            except Exception:  # noqa: BLE001
                к = ""
    return к


def урл(ключ: str | None = None) -> str:
    к = ключ or ключ_helius()
    return f"https://mainnet.helius-rpc.com/?api-key={к}"


def позвать(метод: str, параметры=None, *, урл_: str | None = None,
             таймаут: float = 60.0) -> dict:
    """Один вызов JSON-RPC. Ошибка -- это ответ с why_not, а не исключение."""
    тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                        "params": параметры if параметры is not None else []}
                       ).encode()
    адрес = урл_ or урл()
    try:
        import urllib.request  # noqa: PLC0415

        зпр = urllib.request.Request(
            адрес, data=тело, headers={"content-type": "application/json"})
        with urllib.request.urlopen(зпр, timeout=таймаут) as отв:  # noqa: S310
            сырое = отв.read()
        д = json.loads(сырое)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why_not": f"{type(exc).__name__}: {str(exc)[:200]}",
                "result": None, "bytes": 0}
    if "error" in д:
        return {"ok": False, "why_not": str(д["error"])[:200], "result": None,
                "bytes": len(сырое)}
    return {"ok": True, "why_not": None, "result": д.get("result"),
             "bytes": len(сырое)}


# ----------------------------------------------------------------- пометка

def правило_пометки(окружение=None) -> dict:
    """Подстроки версий из окружения. Пусто -- значит пометки ещё нет."""
    ок = окружение if окружение is not None else os.environ

    def разобрать(имя):
        сырое = (ок.get(имя) or "").strip()
        return tuple(ч.strip().lower() for ч in сырое.split(",") if ч.strip())

    bam = разобрать("TRITON_VERSII_BAM")
    harm = разобрать("TRITON_VERSII_HARMONIC")
    return {МЕТКА_BAM: bam, МЕТКА_HARMONIC: harm,
             "задано": bool(bam or harm),
             "почему_нет": (None if (bam or harm) else
                             "TRITON_VERSII_BAM и TRITON_VERSII_HARMONIC пусты: "
                             "все узлы идут в прочие, правило ставится по "
                             "таблице по_версиям")}


def пометить(версия: str | None, правило: dict) -> str:
    в = (версия or "").lower()
    for метка in (МЕТКА_BAM, МЕТКА_HARMONIC):
        for обр in правило.get(метка) or ():
            if обр and обр in в:
                return метка
    return МЕТКА_ПРОЧИЕ


# ----------------------------------------------------------------- снимок

def снимок(*, урл_: str | None = None, каталог: str | None = None,
            правило: dict | None = None, сейчас: float | None = None) -> dict:
    """Узлы x стейк x расписание лидеров -> доли слотов по меткам."""
    т = сейчас if сейчас is not None else time.time()
    пр = правило if правило is not None else правило_пометки()
    из_: dict = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(т)),
                  "ok": False, "why_not": None, "pravilo": {
                      "bam": list(пр.get(МЕТКА_BAM) or ()),
                      "harmonic": list(пр.get(МЕТКА_HARMONIC) or ()),
                      "zadano": bool(пр.get("задано")),
                      "pochemu_net": пр.get("почему_нет")}, "bytes": 0}
    узлы = позвать("getClusterNodes", [], урл_=урл_)
    из_["bytes"] += узлы["bytes"]
    if not узлы["ok"]:
        из_["why_not"] = f"getClusterNodes: {узлы['why_not']}"
        return из_
    голоса = позвать("getVoteAccounts", [], урл_=урл_)
    из_["bytes"] += голоса["bytes"]
    эпоха = позвать("getEpochInfo", [], урл_=урл_)
    из_["bytes"] += эпоха["bytes"]
    расп = расписание(урл_=урл_, каталог=каталог,
                       эпоха=(эпоха["result"] or {}).get("epoch")
                       if эпоха["ok"] else None)
    из_["bytes"] += int(расп.get("bytes") or 0)
    return свести(узлы=узлы["result"] or [],
                   голоса=голоса["result"] if голоса["ok"] else None,
                   эпоха=эпоха["result"] if эпоха["ok"] else None,
                   расписание_=расп.get("schedule"), правило=пр,
                   основа=из_, почему_расписания=расп.get("why_not"))


def расписание(*, урл_: str | None = None, каталог: str | None = None,
                эпоха: int | None = None) -> dict:
    """Расписание лидеров эпохи: берём ОДИН раз на эпоху, дальше из файла."""
    путь = Path(каталог or os.environ.get("TRITON_STATE_DIR") or "/tmp") / ФАЙЛ_РАСПИСАНИЯ
    if путь.exists():
        try:
            лежит = json.loads(путь.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            лежит = None
        if isinstance(лежит, dict) and (эпоха is None
                                         or лежит.get("epoch") == эпоха):
            return {"schedule": лежит.get("schedule"), "bytes": 0,
                     "from": "файл", "why_not": None}
    отв = позвать("getLeaderSchedule", [None, {"commitment": "confirmed"}],
                   урл_=урл_, таймаут=120.0)
    if not отв["ok"]:
        return {"schedule": None, "bytes": отв["bytes"], "from": "сеть",
                 "why_not": f"getLeaderSchedule: {отв['why_not']}"}
    try:
        путь.parent.mkdir(parents=True, exist_ok=True)
        врем = путь.with_suffix(".tmp")
        врем.write_text(json.dumps({"epoch": эпоха, "schedule": отв["result"]}),
                         encoding="utf-8")
        врем.replace(путь)
    except Exception:  # noqa: BLE001
        pass
    return {"schedule": отв["result"], "bytes": отв["bytes"], "from": "сеть",
             "why_not": None}


def свести(*, узлы: list, голоса, эпоха, расписание_, правило: dict,
            основа: dict | None = None, почему_расписания=None) -> dict:
    """Счётная часть снимка: она же проверяется самопроверкой без сети."""
    из_ = dict(основа or {})
    стейк: dict = {}
    if isinstance(голоса, dict):
        for где in ("current", "delinquent"):
            for г in голоса.get(где) or []:
                к = г.get("nodePubkey")
                if к:
                    стейк[к] = стейк.get(к, 0) + int(г.get("activatedStake") or 0)
    слотов: dict = {}
    if isinstance(расписание_, dict):
        for к, ряд in расписание_.items():
            слотов[к] = len(ряд or [])
    метки: dict = {}
    по_версиям: dict = {}
    for у in узлы or []:
        к = у.get("pubkey")
        версия_ = у.get("version")
        м = пометить(версия_, правило)
        метки[к] = м
        стр = str(версия_)
        гр = по_версиям.setdefault(стр, {"uzlov": 0, "stejk": 0, "slotov": 0,
                                           "metka": м})
        гр["uzlov"] += 1
        гр["stejk"] += int(стейк.get(к) or 0)
        гр["slotov"] += int(слотов.get(к) or 0)
    всего_слотов = sum(слотов.values()) or 0
    всего_стейка = sum(стейк.values()) or 0
    по_меткам: dict = {}
    for м in (МЕТКА_BAM, МЕТКА_HARMONIC, МЕТКА_ПРОЧИЕ):
        ключи = [к for к, зн in метки.items() if зн == м]
        с_сл = sum(int(слотов.get(к) or 0) for к in ключи)
        с_ст = sum(int(стейк.get(к) or 0) for к in ключи)
        по_меткам[м] = {
            "uzlov": len(ключи),
            "slotov": с_сл,
            "dolya_slotov": (round(с_сл / всего_слотов, 6)
                              if всего_слотов else None),
            "stejk_sol": round(с_ст / 1e9, 3),
            "dolya_stejka": (round(с_ст / всего_стейка, 6)
                              if всего_стейка else None)}
    # Узлы расписания, которых нет в getClusterNodes, -- это отдельное число, а
    # не молчание: их слоты в знаменателе есть, а пометки у них нет.
    без_узла = [к for к in слотов if к not in метки]
    из_.update({
        "ok": True,
        "epoch": (эпоха or {}).get("epoch") if isinstance(эпоха, dict) else None,
        "absoluteSlot": ((эпоха or {}).get("absoluteSlot")
                          if isinstance(эпоха, dict) else None),
        "firstSlot": ((int(эпоха["absoluteSlot"]) - int(эпоха["slotIndex"]))
                       if isinstance(эпоха, dict)
                       and эпоха.get("absoluteSlot") is not None
                       and эпоха.get("slotIndex") is not None else None),
        "uzlov_vsego": len(метки),
        "slotov_vsego": всего_слотов,
        "stejka_sol_vsego": round(всего_стейка / 1e9, 3),
        "po_metkam": по_меткам,
        "po_versiyam": dict(sorted(по_версиям.items(),
                                    key=lambda т: -т[1]["slotov"])[:40]),
        "raspisaniya_net": почему_расписания,
        "v_raspisanii_bez_uzla": len(без_узла),
        "metki": метки})
    return из_


def доля_слотов(снимок_: dict) -> dict:
    """Три числа для признака жизни: доля слотов BAM, Harmonic, вместе."""
    по = (снимок_ or {}).get("po_metkam") or {}
    b = ((по.get(МЕТКА_BAM) or {}).get("dolya_slotov"))
    h = ((по.get(МЕТКА_HARMONIC) or {}).get("dolya_slotov"))
    вместе = None
    if b is not None or h is not None:
        вместе = round((b or 0.0) + (h or 0.0), 6)
    return {"bam": b, "harmonic": h, "vmeste": вместе,
             "epoch": (снимок_ or {}).get("epoch"),
             "utc": (снимок_ or {}).get("utc"),
             "pravilo_zadano": bool(((снимок_ or {}).get("pravilo") or {})
                                     .get("zadano"))}


def карта_слотов(расписание_: dict, первый_слот: int) -> dict:
    """Абсолютный слот -> ключ лидера. Нужна отчёту, а не признаку жизни."""
    из_: dict = {}
    for к, ряд in (расписание_ or {}).items():
        for и in ряд or []:
            из_[int(первый_слот) + int(и)] = к
    return из_


def метка_слота(слот: int, карта: dict, метки: dict) -> str | None:
    к = карта.get(int(слот))
    if not к:
        return None
    return метки.get(к) or МЕТКА_ПРОЧИЕ


def записать(снимок_: dict, каталог: str | None = None) -> str:
    путь = Path(каталог or os.environ.get("TRITON_STATE_DIR") or "/tmp") / ФАЙЛ_СНИМКА
    путь.parent.mkdir(parents=True, exist_ok=True)
    врем = путь.with_suffix(".tmp")
    # Пометки по узлам -- отдельный длинный словарь; в файл он нужен (отчёт по
    # нему метит слоты), но в печать не идёт.
    врем.write_text(json.dumps(снимок_, ensure_ascii=False), encoding="utf-8")
    врем.replace(путь)
    return str(путь)


def прочитать(каталог: str | None = None) -> dict | None:
    путь = Path(каталог or os.environ.get("TRITON_STATE_DIR") or "/tmp") / ФАЙЛ_СНИМКА
    if not путь.exists():
        return None
    try:
        return json.loads(путь.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def свежий(*, каталог: str | None = None, срок_s: float = ЧАС_S,
            сейчас: float | None = None) -> dict:
    """Снимок не старше часа: свежий из файла, иначе новый из сети."""
    т = сейчас if сейчас is not None else time.time()
    лежит = прочитать(каталог)
    if isinstance(лежит, dict) and лежит.get("ok"):
        try:
            import calendar  # noqa: PLC0415

            было = float(calendar.timegm(
                time.strptime(лежит["utc"], "%Y-%m-%dT%H:%M:%SZ")))
        except Exception:  # noqa: BLE001
            было = None
        if было is not None and т - было < срок_s:
            лежит["from"] = "файл"
            return лежит
    новый = снимок(каталог=каталог, сейчас=т)
    if новый.get("ok"):
        записать(новый, каталог)
    новый["from"] = "сеть"
    return новый


# ----------------------------------------------------------------- прогон

def main() -> int:
    import argparse  # noqa: PLC0415

    р = argparse.ArgumentParser(description=__doc__)
    р.add_argument("--katalog", default=os.environ.get("TRITON_STATE_DIR") or "/tmp")
    р.add_argument("--out", default="")
    а = р.parse_args()
    с = снимок(каталог=а.katalog)
    if с.get("ok"):
        записать(с, а.katalog)
    печать = {к: зн for к, зн in с.items() if к != "metki"}
    печать["dolya_slotov"] = доля_слотов(с)
    текст = json.dumps(печать, ensure_ascii=False, indent=1)
    if а.out:
        Path(а.out).write_text(текст, encoding="utf-8")
    print(текст[:8000])
    return 0 if с.get("ok") else 1


def self_test() -> int:
    """Счётная часть: пометка, доли, карта слотов. Сети здесь нет."""
    пройдено = провалено = 0

    def chk(что, ок, факт=""):
        nonlocal пройдено, провалено
        if ок:
            пройдено += 1
            print(f"  [ok  ] {что}")
        else:
            провалено += 1
            print(f"  [ПРОВАЛ] {что}: {факт}")

    import tempfile as _tк  # noqa: PLC0415

    with _tк.TemporaryDirectory() as _дк:
        _фк = Path(_дк) / "k"
        _фк.write_text("КЛЮЧ_ИЗ_ФАЙЛА\n", encoding="utf-8")
        chk("ключ Helius читается файлом и в урл не подставляется пустота",
            ключ_helius({"HELIUS_API_KEY_FILE": str(_фк)}) == "КЛЮЧ_ИЗ_ФАЙЛА"
            and ключ_helius({"HELIUS_API_KEY": "ИЗ_ОКРУЖЕНИЯ",
                              "HELIUS_API_KEY_FILE": str(_фк)}) == "ИЗ_ОКРУЖЕНИЯ")
        chk("файла нет -- ключ пуст, а не исключение",
            ключ_helius({"HELIUS_API_KEY_FILE": str(_фк) + ".нет"}) == "")
    пусто = правило_пометки({})
    chk("без окружения правила нет и это названо",
        (not пусто["задано"]) and bool(пусто["почему_нет"]), str(пусто))
    пр = правило_пометки({"TRITON_VERSII_BAM": "jito,BAM",
                           "TRITON_VERSII_HARMONIC": "tritonone"})
    chk("подстроки читаются и регистр не важен",
        пометить("2.3.6-jito", пр) == МЕТКА_BAM
        and пометить("2.3.6-TritonOne", пр) == МЕТКА_HARMONIC
        and пометить("2.3.6", пр) == МЕТКА_ПРОЧИЕ,
        f"{пометить('2.3.6-jito', пр)} / {пометить('2.3.6-TritonOne', пр)}")
    узлы = [{"pubkey": "A", "version": "2.3.6-jito"},
             {"pubkey": "B", "version": "2.3.6"},
             {"pubkey": "C", "version": "2.3.6-TritonOne"}]
    голоса = {"current": [{"nodePubkey": "A", "activatedStake": 2_000_000_000},
                           {"nodePubkey": "B", "activatedStake": 1_000_000_000}],
               "delinquent": [{"nodePubkey": "C", "activatedStake": 1_000_000_000}]}
    расп = {"A": [0, 1, 2, 3], "B": [4, 5], "C": [6, 7, 8, 9]}
    эп = {"epoch": 800, "absoluteSlot": 450_000_010, "slotIndex": 10}
    с = свести(узлы=узлы, голоса=голоса, эпоха=эп, расписание_=расп, правило=пр)
    chk("доля слотов BAM 4 из 10", с["po_metkam"][МЕТКА_BAM]["dolya_slotov"] == 0.4,
        str(с["po_metkam"][МЕТКА_BAM]))
    chk("Harmonic 4 из 10 и прочие 2 из 10",
        с["po_metkam"][МЕТКА_HARMONIC]["dolya_slotov"] == 0.4
        and с["po_metkam"][МЕТКА_ПРОЧИЕ]["dolya_slotov"] == 0.2,
        str(с["po_metkam"]))
    chk("стейк delinquent тоже считается (Harmonic 1 SOL)",
        с["po_metkam"][МЕТКА_HARMONIC]["stejk_sol"] == 1.0,
        str(с["po_metkam"][МЕТКА_HARMONIC]))
    chk("первый слот эпохи -- абсолютный минус индекс",
        с["firstSlot"] == 450_000_000, str(с["firstSlot"]))
    д = доля_слотов(с)
    chk("вместе -- сумма двух долей", д["vmeste"] == 0.8, str(д))
    карта = карта_слотов(расп, с["firstSlot"])
    chk("слот метится лидером этого слота",
        метка_слота(450_000_000, карта, с["metki"]) == МЕТКА_BAM
        and метка_слота(450_000_006, карта, с["metki"]) == МЕТКА_HARMONIC
        and метка_слота(450_000_099, карта, с["metki"]) is None,
        str(метка_слота(450_000_006, карта, с["metki"])))
    с2 = свести(узлы=узлы, голоса=голоса, эпоха=эп,
                 расписание_={**расп, "D": [10, 11]}, правило=пр)
    chk("лидер расписания без узла в getClusterNodes -- отдельное число",
        с2["v_raspisanii_bez_uzla"] == 1, str(с2["v_raspisanii_bez_uzla"]))
    chk("пустое правило: все слоты в прочих",
        свести(узлы=узлы, голоса=голоса, эпоха=эп, расписание_=расп,
                правило=пусто)["po_metkam"][МЕТКА_ПРОЧИЕ]["dolya_slotov"] == 1.0)
    chk("таблица по версиям несёт метку и слоты",
        с["po_versiyam"]["2.3.6-jito"]["slotov"] == 4
        and с["po_versiyam"]["2.3.6-jito"]["metka"] == МЕТКА_BAM,
        str(с["po_versiyam"]))
    print(f"самопроверка списка валидаторов: {пройдено}/{пройдено + провалено} пройдено")
    return 1 if провалено else 0


if __name__ == "__main__":
    raise SystemExit(self_test() if "--self-test" in sys.argv else main())
