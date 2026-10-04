#!/usr/bin/env python3
"""Живые ЧИСЛА политики полосы по группам: типы пулов и режим USDC-ноги.

ЗАЧЕМ (слово владельца 04.10, вечер, п.1 и п.2). Нужны два живых значения, а не
память:
  * какие типы пулов полоса берёт по группе -- чтобы дать krug_001 «пулы как у
    lane_s0» ТЕМ ЖЕ набором, а не списком из старого плана. Ни у одной группы
    поля lane_pools сейчас нет, значит набор складывается из постоянных двух и
    флагов окружения LANE_<ТИП>_GROUPS -- и узнать его можно только у живого
    процесса;
  * в каком режиме USDC-нога по группе (vykl / ten / boj) -- чтобы ответить,
    выключена она флагом или отказывает по другой причине.

ЧЕГО ЗДЕСЬ НЕТ. Ни одного ЗНАЧЕНИЯ переменной окружения наружу не уходит: env
живого процесса читается в память, а печатаются только ИМЕНА типов, имя режима
и логическое «список групп задан». Ни сети, ни правок, ни отправок.

ПОЧЕМУ env БЕРЁТСЯ ИЗ /proc, А НЕ ИЗ ФАЙЛА. Файл -- это то, что положили;
/proc/<pid>/environ -- то, с чем процесс ЖИВЁТ. Пакет B 04.10 стоил разбора
ровно на этой разнице, и с тех пор правда о переменных берётся из памяти
процесса.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Имя службы детектора: по нему ищется живой процесс.
ЕДИНИЦА_ПО_УМОЛЧАНИЮ = "bloom-detector"


def env_процесса(pid: int) -> dict:
    """{имя: значение} из /proc/<pid>/environ. Значения НАРУЖУ НЕ ИДУТ."""
    сырое = Path(f"/proc/{pid}/environ").read_bytes()
    из_: dict = {}
    for кусок in сырое.split(b"\0"):
        if not кусок or b"=" not in кусок:
            continue
        имя, _, знач = кусок.partition(b"=")
        из_[имя.decode("utf-8", "replace")] = знач.decode("utf-8", "replace")
    return из_


def типы_группы(OSW, B, группа: str | None) -> dict:
    """Имена типов пулов, которые полоса берёт по этой группе."""
    try:
        набор = OSW.типы_полосы(группа)
    except Exception as сбой:  # noqa: BLE001
        return {"ok": False, "why_not": f"{type(сбой).__name__}: {str(сбой)[:120]}"}
    имена = []
    for конст, имя in sorted(OSW.ИМЕНА_ТИПОВ.items(), key=lambda п: п[1]):
        прог = getattr(B, конст, None)
        if прог and прог in набор:
            имена.append(имя)
    двух = False
    try:
        двух = bool(OSW.двухшаговый_включён(группа))
    except Exception:  # noqa: BLE001
        двух = False
    if двух:
        имена.append(OSW.ИМЯ_ДВУХШАГОВОГО)
    # ПОЛЕ ФАЙЛА -- ОТДЕЛЬНО ОТ ИТОГА: по итогу не видно, он от файла или от
    # флагов, а правка пишется именно в поле.
    из_файла = None
    try:
        из_файла = OSW._типы_из_файла(группа)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "why_not": None, "имена": имена,
            "программ_в_наборе": len(набор),
            "поле_lane_pools_есть": из_файла is not None}


def usdc_режим(UN, группа: str | None) -> dict:
    """Режим USDC-ноги по группе. Наружу -- ИМЯ режима, не значение флага."""
    try:
        реж = UN.rezhim(группа)
    except Exception as сбой:  # noqa: BLE001
        return {"ok": False, "why_not": f"{type(сбой).__name__}: {str(сбой)[:120]}"}
    return {"ok": True, "rezhim": реж, "boj": bool(UN.boj(группа)),
            "flag": UN.FLAG, "flag_groups": UN.FLAG_GROUPS}


def путь_из_pythonpath(env: dict) -> list:
    """Каталоги из PYTHONPATH процесса -- СПИСКОМ, чтобы добавить их в sys.path.

    ЗАЧЕМ ОТДЕЛЬНО. Первый прогон этой проверки упал на ModuleNotFoundError:
    модули тени (c2_swap_build и родня) лежат НЕ рядом с bloom_own_send, а там,
    куда их кладёт PYTHONPATH службы. Положить PYTHONPATH в os.environ уже
    поздно: sys.path собирается при старте интерпретатора и от переменной
    потом не меняется. Значит каталоги надо добавить руками.
    """
    сырое = (env or {}).get("PYTHONPATH") or ""
    return [к for к in (ч.strip() for ч in сырое.split(os.pathsep)) if к]


def свод(группы: list, *, code_dir: str, env: dict) -> dict:
    """Живой свод. Чистая функция относительно env и пути к коду."""
    из_: dict = {"группы": {}, "why_not": None,
                 "flag_groups_zadan": None, "putej_iz_pythonpath": 0}
    if code_dir and code_dir not in sys.path:
        sys.path.insert(0, code_dir)
    for имя, знач in (env or {}).items():
        os.environ[имя] = знач
    # КАТАЛОГИ КОДА -- ИЗ PYTHONPATH ЖИВОГО ПРОЦЕССА. Это не значение секрета:
    # это пути на диске, и наружу уходит только ИХ ЧИСЛО.
    _пути = путь_из_pythonpath(env)
    из_["putej_iz_pythonpath"] = len(_пути)
    for к in _пути:
        if к not in sys.path:
            sys.path.insert(0, к)
    try:
        import bloom_own_send as OSW  # noqa: PLC0415
    except Exception as сбой:  # noqa: BLE001
        из_["why_not"] = f"bloom_own_send не загрузился: {type(сбой).__name__}"
        return из_
    try:
        _, _, _, B = OSW._модули()
    except Exception as сбой:  # noqa: BLE001
        из_["why_not"] = f"сборщик не загрузился: {type(сбой).__name__}"
        return из_
    try:
        import c2_usdc_noga as UN  # noqa: PLC0415
    except Exception as сбой:  # noqa: BLE001
        UN = None
        из_["usdc_why_not"] = f"c2_usdc_noga не загрузился: {type(сбой).__name__}"
    if UN is not None:
        # ЛОГИЧЕСКОЕ, А НЕ СОДЕРЖИМОЕ: задан ли список групп флага.
        из_["flag_groups_zadan"] = bool(
            (os.environ.get(UN.FLAG_GROUPS) or "").strip())
    for г in группы:
        г_ = None if г in ("", "-", "None") else г
        строка = {"типы": типы_группы(OSW, B, г_)}
        if UN is not None:
            строка["usdc"] = usdc_режим(UN, г_)
        из_["группы"][г or "-"] = строка
    return из_


def _самопроверка() -> int:
    проверок = прошло = 0

    def сверить(что, дано, ждали):
        nonlocal проверок, прошло
        проверок += 1
        if дано == ждали:
            прошло += 1
        else:
            print(f"НЕ ПРОШЛО: {что}: дано {дано!r}, ждали {ждали!r}")

    # --- env процесса: разбор НУЛЬ-разделённой строки ---
    класс_байт = type(b"")
    проба = b"A=1\0B=2=3\0\0MUSOR_BEZ_RAVNO\0"
    # Разбор повторяется здесь той же формулой, что и в env_процесса: читать
    # /proc в самопроверке нечем, а разбор проверить надо.
    разобрано = {}
    for кусок in проба.split(b"\0"):
        if not кусок or b"=" not in кусок:
            continue
        имя, _, знач = кусок.partition(b"=")
        разобрано[имя.decode()] = знач.decode()
    сверить("имена и значения env разбираются, мусор без '=' пропускается",
            разобрано, {"A": "1", "B": "2=3"})
    сверить("тип пробы -- байты, как в /proc", класс_байт, bytes)

    # --- типы группы: по поддельным модулям, без сети и без боевого кода ---
    class ПоддельныйB:  # noqa: D401
        BONDING = "ПРОГ_BONDING"
        PUMP_AMM = "ПРОГ_PUMP"
        CPMM = "ПРОГ_CPMM"

    class ПоддельныйOSW:
        ИМЕНА_ТИПОВ = {"BONDING": "bonding", "PUMP_AMM": "pump_amm",
                        "CPMM": "cpmm"}
        ИМЯ_ДВУХШАГОВОГО = "two_step"

        def __init__(self, набор, двух, из_файла):
            self._набор, self._двух, self._из_файла = набор, двух, из_файла

        def типы_полосы(self, группа=None):
            return self._набор

        def двухшаговый_включён(self, группа=None):
            return self._двух

        def _типы_из_файла(self, группа=None):
            return self._из_файла

    о = ПоддельныйOSW({"ПРОГ_BONDING", "ПРОГ_PUMP"}, True, None)
    т = типы_группы(о, ПоддельныйB, "g")
    сверить("имена типов отдаются по алфавиту и только те, что в наборе",
            т["имена"], ["bonding", "pump_amm", "two_step"])
    сверить("и сказано, что поля lane_pools у группы НЕТ",
            т["поле_lane_pools_есть"], False)
    о2 = ПоддельныйOSW({"ПРОГ_CPMM"}, False, {"cpmm"})
    т2 = типы_группы(о2, ПоддельныйB, "g")
    сверить("двухшаговый не приписывается, когда он выключен",
            т2["имена"], ["cpmm"])
    сверить("и поле lane_pools названо, когда оно есть",
            т2["поле_lane_pools_есть"], True)

    class ПадающийOSW(ПоддельныйOSW):
        def типы_полосы(self, группа=None):
            raise RuntimeError("набор не посчитан")

    сверить("падение набора -- ok=False с причиной, а не пустой список",
            типы_группы(ПадающийOSW(set(), False, None),
                        ПоддельныйB, "g")["ok"], False)

    # --- режим USDC: ИМЯ режима, не значение флага ---
    class ПоддельныйUN:
        FLAG = "BLOOM_USDC_NOGA"
        FLAG_GROUPS = "BLOOM_USDC_NOGA_GROUPS"

        def rezhim(self, gruppa=None):
            return "ten" if gruppa == "g" else "vykl"

        def boj(self, gruppa=None):
            return self.rezhim(gruppa) == "boj"

    у = usdc_режим(ПоддельныйUN(), "g")
    сверить("режим USDC отдаётся ИМЕНЕМ", у["rezhim"], "ten")
    сверить("и бой отдельно, и он False в тени", у["boj"], False)
    сверить("имя флага берётся У МОДУЛЯ, а не пишется строкой",
            у["flag"], "BLOOM_USDC_NOGA")
    сверить("незнакомая группа -- выключено",
            usdc_режим(ПоддельныйUN(), "чужая")["rezhim"], "vykl")
    # ЗНАЧЕНИЕ ФЛАГА НАРУЖУ НЕ УХОДИТ НИ ОДНИМ ПОЛЕМ.
    сверить("в ответе про USDC нет ни одного значения переменной",
            [к for к in у if к in ("znachenie", "value", "env")], [])

    # --- PYTHONPATH: каталоги кода из env процесса (первый прогон упал здесь) ---
    сверить("каталоги PYTHONPATH разбираются по разделителю, пустые отбрасываются",
            путь_из_pythonpath({"PYTHONPATH":
                                 f"/a{os.pathsep}{os.pathsep} /b "}),
            ["/a", "/b"])
    сверить("нет PYTHONPATH -- пустой список, а не падение",
            путь_из_pythonpath({}), [])
    сверить("наружу уходит только ЧИСЛО каталогов, не сами пути",
            sorted(к for к in свод([], code_dir="", env={})
                    if "pythonpath" in к.lower()),
            ["putej_iz_pythonpath"])
    print(f"проверок {проверок}, прошло {прошло}, "
          f"не прошло {проверок - прошло}")
    return 0 if прошло == проверок else 1


def main(довод: list) -> int:
    code_dir = довод[0] if довод else "/home/bot/bloom_executor"
    группы = довод[1].split(",") if len(довод) > 1 and довод[1] else ["lane_s0"]
    pid = None
    if len(довод) > 2 and довод[2]:
        pid = int(довод[2])
    env = {}
    почему_env = None
    if pid:
        try:
            env = env_процесса(pid)
        except Exception as сбой:  # noqa: BLE001
            почему_env = f"{type(сбой).__name__}: {str(сбой)[:120]}"
    из_ = свод(группы, code_dir=code_dir, env=env)
    из_["env_iz_processa"] = bool(env)
    из_["env_imyon"] = len(env)
    из_["env_why_not"] = почему_env
    print(json.dumps(из_, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_самопроверка())
    sys.exit(main([а for а in sys.argv[1:] if not а.startswith("--")]))
