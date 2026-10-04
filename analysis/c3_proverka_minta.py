#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ПРОВЕРКА ТОКЕНА ДО ВХОДА: флаги минта из ОДНОГО чтения, без запретов.

ЗАЧЕМ. Перед покупкой полоса и так читает минт (bloom_detector: getMultipleAccounts
с encoding=jsonParsed, кеш на минт). Из ТОГО ЖЕ ответа видно гораздо больше,
чем берётся сейчас: сегодня из него достают только ставку налога и два
полномочия. Остальное -- молча выбрасывается.

ЧТО ЭТОТ МОДУЛЬ ДЕЛАЕТ. Берёт разобранный узлом минт и называет флаги:
полномочие выпуска, полномочие заморозки, налог на перевод, ХУК ПЕРЕВОДА,
ПОСТОЯННЫЙ ДЕЛЕГАТ, НЕПЕРЕДАВАЕМОСТЬ, замороженное состояние по умолчанию и
ПАУЗУ. Ничего не запрещает: пороги даст Code-2 (п.8 его пакета), а здесь --
числа и имена.

ЧИСЛА, А НЕ ВПЕЧАТЛЕНИЕ. Имена расширений взяты не по памяти, а из живого
замера репозитория: data/a2_mint_tax_fill.json -- 173 минта наших сделок,
из них Token-2022 153. Частоты: transferFeeConfig 104, metadataPointer 153,
permanentDelegate 21, defaultAccountState 21, pausableConfig 21,
confidentialTransferMint 21, transferHook 21, scaledUiAmountConfig 21,
confidentialTransferFeeConfig 2. То есть опасная четвёрка (хук, делегат,
заморозка по умолчанию, пауза) встречается у КАЖДОГО ВОСЬМОГО нашего минта.

ПОЧЕМУ ИМЕНА, А НЕ НОМЕРА TLV. Узел отдаёт расширения ИМЕНАМИ в jsonParsed.
Разбирать TLV байтами значило бы держать в коде таблицу номеров по памяти и
однажды принять решение на деньгах по неверному номеру. Имя приходит от узла.

НЕ РАЗОБРАН -- НЕ ЗНАЧИТ ЧИСТО. Это отдельный флаг и отдельная причина: слить
их в одно "опасного нет" значит купить вслепую и считать это проверкой.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
ПРОГ_ТОКЕНА = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ПРОГ_ТОКЕНА_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# ОПАСНЫЕ РАСШИРЕНИЯ -- И ЧЕМ ИМЕННО ОПАСНЫ. Словами, чтобы читающему отчёт не
# пришлось лезть в документацию.
ЧЕМ_ОПАСНО = {
    "transferHook": ("при КАЖДОМ переводе вызывается чужая программа -- она "
                     "может отказать в продаже или потратить вычислители"),
    "permanentDelegate": ("у эмитента постоянное право забрать токены С НАШЕГО "
                          "счёта без нашей подписи"),
    "nonTransferable": "токен нельзя передать вовсе -- продать его невозможно",
    "defaultAccountState": ("новые счета могут создаваться ЗАМОРОЖЕННЫМИ: "
                            "купить можно, продать нельзя"),
    "pausableConfig": "эмитент может ОСТАНОВИТЬ переводы в любой момент",
    "transferFeeConfig": "налог на перевод -- часть продажи уходит эмитенту",
}
# Эти расширения сами по себе не мешают продать.
БЕЗОБИДНЫЕ = ("metadataPointer", "tokenMetadata", "scaledUiAmountConfig",
              "confidentialTransferMint", "confidentialTransferFeeConfig",
              "interestBearingConfig", "mintCloseAuthority", "groupPointer",
              "groupMemberPointer", "tokenGroup", "tokenGroupMember")

WHY_НЕ_РАЗОБРАН = "минт не разобран узлом -- про флаги НЕ ИЗВЕСТНО НИЧЕГО"
WHY_НЕТ_РАСШИРЕНИЙ = ("поля расширений в ответе нет -- их отсутствие НЕ "
                      "доказано")
WHY_КРУПНЕЙШИЕ = ("доли крупнейших держателей в этом чтении нет: это другой "
                  "вызов (getTokenLargestAccounts)")


def _polnomochije(значение) -> dict:
    есть = bool(значение)
    return {"est": есть, "adres": (str(значение) if есть else None)}


def razbor_minta(info: dict, *, programma: str | None = None) -> dict:
    """Флаги минта из разобранного узлом ответа. Чистая функция, без сети.

    `info` -- то, что лежит в data.parsed.info ответа getAccountInfo/
    getMultipleAccounts с encoding=jsonParsed.
    """
    из_ = {"razobran": False, "rasshirenija_est": False, "why_not": None,
            "token_2022": None, "decimals": None, "supply": None,
            "mint_authority": _polnomochije(None),
            "freeze_authority": _polnomochije(None),
            "nalog_bps": None, "nalog_max": None,
            "flagi": {}, "opasnyh": 0, "neizvestnyh_rasshirenij": [],
            "rasshirenija": [], "pochemu": []}
    if not isinstance(info, dict) or not info:
        из_["why_not"] = WHY_НЕ_РАЗОБРАН
        return из_
    из_["razobran"] = True
    из_["token_2022"] = (str(programma) == ПРОГ_ТОКЕНА_2022
                         if programma else None)
    из_["decimals"] = info.get("decimals")
    из_["supply"] = info.get("supply")
    из_["mint_authority"] = _polnomochije(info.get("mintAuthority"))
    из_["freeze_authority"] = _polnomochije(info.get("freezeAuthority"))
    расширения = info.get("extensions")
    из_["rasshirenija_est"] = расширения is not None
    if расширения is None:
        из_["pochemu"].append(WHY_НЕТ_РАСШИРЕНИЙ)
        расширения = []
    имена = []
    for р in расширения:
        имя = (р.get("extension") if isinstance(р, dict) else str(р))
        if not имя:
            continue
        имена.append(имя)
        состояние = (р.get("state") if isinstance(р, dict) else None) or {}
        if имя == "transferFeeConfig":
            новее = состояние.get("newerTransferFee") or {}
            из_["nalog_bps"] = новее.get("transferFeeBasisPoints")
            из_["nalog_max"] = новее.get("maximumFee")
            из_["flagi"][имя] = {"opasno": bool(из_["nalog_bps"]),
                                 "bps": из_["nalog_bps"],
                                 "max": из_["nalog_max"]}
            continue
        if имя == "transferHook":
            прог = состояние.get("programId")
            из_["flagi"][имя] = {"opasno": bool(прог), "programma": прог}
            continue
        if имя == "permanentDelegate":
            д = состояние.get("delegate")
            из_["flagi"][имя] = {"opasno": bool(д), "delegat": д}
            continue
        if имя == "defaultAccountState":
            сост = str(состояние.get("accountState") or "").lower()
            из_["flagi"][имя] = {"opasno": сост == "frozen",
                                 "sostojanije": сост or None}
            continue
        if имя in ("nonTransferable", "pausableConfig"):
            из_["flagi"][имя] = {"opasno": True, "sostojanije": состояние or None}
            continue
        if имя not in БЕЗОБИДНЫЕ:
            # НЕЗНАКОМОЕ РАСШИРЕНИЕ НЕ ВЫБРАСЫВАЕТСЯ МОЛЧА: его называют, и
            # решает тот, кто ставит пороги.
            из_["neizvestnyh_rasshirenij"].append(имя)
    из_["rasshirenija"] = имена
    из_["opasnyh"] = sum(1 for ф in из_["flagi"].values() if ф.get("opasno"))
    for имя, ф in из_["flagi"].items():
        if ф.get("opasno"):
            из_["pochemu"].append(f"{имя}: {ЧЕМ_ОПАСНО.get(имя, 'опасно')}")
    if из_["mint_authority"]["est"]:
        из_["pochemu"].append("mintAuthority: эмитент может допечатать токен")
    if из_["freeze_authority"]["est"]:
        из_["pochemu"].append("freezeAuthority: эмитент может заморозить наш счёт")
    return из_


def dolja_krupnejshih(krupnejshie: list | None, supply) -> dict:
    """Доля крупнейших держателей -- ЕСЛИ её дали. Иначе сказано, что её нет."""
    из_ = {"est": False, "why_not": WHY_КРУПНЕЙШИЕ, "dolja_pervogo_pct": None,
            "dolja_pjati_pct": None, "derzhatelej": None}
    try:
        всего = int(supply)
    except (TypeError, ValueError):
        всего = 0
    if not krupnejshie or всего <= 0:
        return из_
    суммы = []
    for з in krupnejshie:
        с = ((з.get("amount") if isinstance(з, dict) else None)
             or (з.get("uiAmountString") if isinstance(з, dict) else None))
        try:
            суммы.append(int(с))
        except (TypeError, ValueError):
            continue
    if not суммы:
        return из_
    суммы.sort(reverse=True)
    из_.update(est=True, why_not=None, derzhatelej=len(суммы),
               dolja_pervogo_pct=round(100.0 * суммы[0] / всего, 3),
               dolja_pjati_pct=round(100.0 * sum(суммы[:5]) / всего, 3))
    return из_


def svodka(info: dict, *, programma: str | None = None,
           krupnejshie: list | None = None) -> dict:
    """ОДИН ОТВЕТ ДЛЯ ТОГО, КТО СТАВИТ ПОРОГИ. Ничего не запрещает сам."""
    р = razbor_minta(info, programma=programma)
    р["krupnejshie"] = dolja_krupnejshih(krupnejshie, р.get("supply"))
    # ЧЕСТНЫЙ ИТОГ: «чисто» говорится ТОЛЬКО когда минт разобран И список
    # расширений пришёл. Иначе -- «не известно», и это другое слово.
    if not р["razobran"]:
        р["itog"] = "не известно"
    elif not р["rasshirenija_est"] and р["token_2022"] is not False:
        р["itog"] = "не известно"
    elif р["opasnyh"] or р["mint_authority"]["est"] or р["freeze_authority"]["est"]:
        р["itog"] = "есть флаги"
    else:
        р["itog"] = "чисто"
    return р


# ------------------------------------------------------------- самопроверка

ZHDEM_PROVEROK = 16
ФАЙЛ_ЗАМЕРА = "a2_mint_tax_fill.json"
# ЗАМЕР ПО 173 ЖИВЫМ МИНТАМ НАШИХ СДЕЛОК (data/a2_mint_tax_fill.json, 26.09).
ЖДЁМ_ЗАМЕРА = {"mintov": 173, "token_2022": 153, "taksiruemyh": 104,
               "transferHook": 21, "permanentDelegate": 21,
               "defaultAccountState": 21, "pausableConfig": 21}
ПРЕДЕЛ_МС = 5.0


def _info(*, rasshirenija=None, mint_auth=None, freeze_auth=None,
          decimals=6, supply="1000000000000000") -> dict:
    из_ = {"decimals": decimals, "supply": supply,
            "mintAuthority": mint_auth, "freezeAuthority": freeze_auth}
    if rasshirenija is not None:
        из_["extensions"] = rasshirenija
    return из_


def self_test() -> int:  # noqa: C901, PLR0915
    было, плохо = 0, 0
    упавшие: list = []

    def chk(имя, усл, факт=None):
        nonlocal было, плохо
        было += 1
        if усл:
            print(f"  ok   {имя}")
        else:
            плохо += 1
            упавшие.append(имя)
            print(f" ПЛОХО {имя} -- {факт!r}")

    print("c3_proverka_minta: самопроверка")
    # ------------------------------------------- ЧИСТЫЙ МИНТ
    чистый = svodka(_info(rasshirenija=[]), programma=ПРОГ_ТОКЕНА)
    chk("минт без расширений и без полномочий -- «чисто», и это сказано словом",
        чистый["itog"] == "чисто" and чистый["opasnyh"] == 0
        and not чистый["pochemu"], чистый)
    # ------------------------------------------- ПОЛНОМОЧИЯ
    с_выпуском = svodka(_info(rasshirenija=[], mint_auth="Emitent11111"),
                        programma=ПРОГ_ТОКЕНА)
    chk("mintAuthority назван: эмитент может допечатать токен",
        с_выпуском["itog"] == "есть флаги"
        and с_выпуском["mint_authority"]["est"]
        and any("допечатать" in п for п in с_выпуском["pochemu"]), с_выпуском)
    с_заморозкой = svodka(_info(rasshirenija=[], freeze_auth="Emitent11111"),
                          programma=ПРОГ_ТОКЕНА)
    chk("freezeAuthority назван: эмитент может заморозить НАШ счёт",
        с_заморозкой["freeze_authority"]["est"]
        and any("заморозить" in п for п in с_заморозкой["pochemu"]), None)
    # ------------------------------------------- ЧЕТЫРЕ ОПАСНЫХ РАСШИРЕНИЯ
    хук = svodka(_info(rasshirenija=[
        {"extension": "transferHook", "state": {"programId": "Hook111"}}]),
        programma=ПРОГ_ТОКЕНА_2022)
    chk("ХУК ПЕРЕВОДА назван вместе с программой: при каждом переводе зовётся "
        "чужой код, и он может не дать продать",
        хук["flagi"]["transferHook"]["opasno"]
        and хук["flagi"]["transferHook"]["programma"] == "Hook111"
        and хук["opasnyh"] == 1, хук["flagi"])
    делегат = svodka(_info(rasshirenija=[
        {"extension": "permanentDelegate", "state": {"delegate": "Delegat1"}}]),
        programma=ПРОГ_ТОКЕНА_2022)
    chk("ПОСТОЯННЫЙ ДЕЛЕГАТ назван: у эмитента право забрать токены с нашего "
        "счёта без нашей подписи",
        делегат["flagi"]["permanentDelegate"]["opasno"]
        and делегат["flagi"]["permanentDelegate"]["delegat"] == "Delegat1", None)
    мороз = svodka(_info(rasshirenija=[
        {"extension": "defaultAccountState", "state": {"accountState": "frozen"}}]),
        programma=ПРОГ_ТОКЕНА_2022)
    тепло = svodka(_info(rasshirenija=[
        {"extension": "defaultAccountState",
         "state": {"accountState": "initialized"}}]), programma=ПРОГ_ТОКЕНА_2022)
    chk("ЗАМОРОЖЕННОЕ СОСТОЯНИЕ ПО УМОЛЧАНИЮ опасно, а обычное -- нет: "
        "купить можно, продать нельзя -- это разные вещи",
        мороз["flagi"]["defaultAccountState"]["opasno"] is True
        and тепло["flagi"]["defaultAccountState"]["opasno"] is False,
        (мороз["flagi"], тепло["flagi"]))
    пауза = svodka(_info(rasshirenija=[{"extension": "pausableConfig"}]),
                   programma=ПРОГ_ТОКЕНА_2022)
    chk("ПАУЗА названа: эмитент может остановить переводы в любой момент "
        "(у наших минтов она встречается у каждого восьмого)",
        пауза["flagi"]["pausableConfig"]["opasno"] is True, None)
    нельзя = svodka(_info(rasshirenija=[{"extension": "nonTransferable"}]),
                    programma=ПРОГ_ТОКЕНА_2022)
    chk("НЕПЕРЕДАВАЕМОСТЬ названа: такой токен продать невозможно вовсе",
        нельзя["flagi"]["nonTransferable"]["opasno"] is True, None)
    # ------------------------------------------- НАЛОГ
    налог = svodka(_info(rasshirenija=[
        {"extension": "transferFeeConfig",
         "state": {"newerTransferFee": {"transferFeeBasisPoints": 500,
                                        "maximumFee": "1000000"}}}]),
        programma=ПРОГ_ТОКЕНА_2022)
    chk("налог на перевод читается ставкой и потолком, а нулевая ставка "
        "опасной не считается",
        налог["nalog_bps"] == 500 and налог["nalog_max"] == "1000000"
        and налог["flagi"]["transferFeeConfig"]["opasno"] is True
        and svodka(_info(rasshirenija=[
            {"extension": "transferFeeConfig",
             "state": {"newerTransferFee": {"transferFeeBasisPoints": 0}}}])
        )["flagi"]["transferFeeConfig"]["opasno"] is False, налог["flagi"])
    # ------------------------------------------- ДОКАЗАННЫЕ КРАСНЫЕ
    нет = svodka({}, programma=ПРОГ_ТОКЕНА_2022)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: минт НЕ РАЗОБРАН -- итог «не известно», а не "
        "«чисто»: слить их значит купить вслепую и назвать это проверкой",
        нет["itog"] == "не известно" and нет["why_not"] == WHY_НЕ_РАЗОБРАН,
        нет)
    без_поля = svodka(_info(rasshirenija=None), programma=ПРОГ_ТОКЕНА_2022)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: поля расширений в ответе НЕТ -- тоже «не "
        "известно»: их отсутствие не доказано",
        без_поля["itog"] == "не известно"
        and any(WHY_НЕТ_РАСШИРЕНИЙ in п for п in без_поля["pochemu"]), без_поля)
    новое = svodka(_info(rasshirenija=[{"extension": "sovsemNovoeRasshirenije"}]),
                   programma=ПРОГ_ТОКЕНА_2022)
    chk("ДОКАЗАННЫЙ КРАСНЫЙ: незнакомое расширение НЕ выбрасывается молча -- "
        "оно названо по имени",
        новое["neizvestnyh_rasshirenij"] == ["sovsemNovoeRasshirenije"], новое)
    # ------------------------------------------- КРУПНЕЙШИЕ ДЕРЖАТЕЛИ
    без_кр = svodka(_info(rasshirenija=[]), programma=ПРОГ_ТОКЕНА)
    с_кр = svodka(_info(rasshirenija=[], supply="1000"), programma=ПРОГ_ТОКЕНА,
                  krupnejshie=[{"amount": "400"}, {"amount": "100"},
                               {"amount": "50"}])
    chk("доли крупнейших держателей в ТОМ ЖЕ чтении нет -- сказано словами; "
        "а когда их дают, доля считается числом (40 % у первого, 55 % у пяти)",
        без_кр["krupnejshie"]["est"] is False
        and WHY_КРУПНЕЙШИЕ in (без_кр["krupnejshie"]["why_not"] or "")
        and с_кр["krupnejshie"]["dolja_pervogo_pct"] == 40.0
        and с_кр["krupnejshie"]["dolja_pjati_pct"] == 55.0,
        (без_кр["krupnejshie"], с_кр["krupnejshie"]))
    # ------------------------------------------- ЗАМЕР ПО ЖИВЫМ МИНТАМ
    п = КОРЕНЬ / "data" / ФАЙЛ_ЗАМЕРА
    счёт = {"mintov": 0, "token_2022": 0, "taksiruemyh": 0}
    частоты: dict = {}
    if п.exists():
        д = json.loads(п.read_text(encoding="utf-8"))
        for м in (д.get("минты") or {}).values():
            счёт["mintov"] += 1
            счёт["token_2022"] += (м.get("программа_токена_id") == ПРОГ_ТОКЕНА_2022)
            счёт["taksiruemyh"] += bool(м.get("таксируемый"))
            for р in (м.get("расширения") or []):
                частоты[р] = частоты.get(р, 0) + 1
    chk(f"имена расширений взяты из ЖИВОГО замера: {ЖДЁМ_ЗАМЕРА['mintov']} "
        f"минтов наших сделок, Token-2022 {ЖДЁМ_ЗАМЕРА['token_2022']}, "
        f"таксируемых {ЖДЁМ_ЗАМЕРА['taksiruemyh']}",
        счёт["mintov"] == ЖДЁМ_ЗАМЕРА["mintov"]
        and счёт["token_2022"] == ЖДЁМ_ЗАМЕРА["token_2022"]
        and счёт["taksiruemyh"] == ЖДЁМ_ЗАМЕРА["taksiruemyh"], счёт)
    chk("опасная четвёрка встречается у каждого восьмого нашего минта: хук 21, "
        "делегат 21, заморозка по умолчанию 21, пауза 21 -- и все четыре имени "
        "модуль знает",
        all(частоты.get(и) == ЖДЁМ_ЗАМЕРА[и] for и in
            ("transferHook", "permanentDelegate", "defaultAccountState",
             "pausableConfig"))
        and all(и in ЧЕМ_ОПАСНО for и in
                ("transferHook", "permanentDelegate", "defaultAccountState",
                 "pausableConfig")), частоты)
    # ------------------------------------------- ВРЕМЯ
    тяжёлый = _info(rasshirenija=[
        {"extension": "transferFeeConfig",
         "state": {"newerTransferFee": {"transferFeeBasisPoints": 300}}},
        {"extension": "transferHook", "state": {"programId": "Hook111"}},
        {"extension": "permanentDelegate", "state": {"delegate": "D1"}},
        {"extension": "defaultAccountState", "state": {"accountState": "frozen"}},
        {"extension": "pausableConfig"}, {"extension": "metadataPointer"},
        {"extension": "tokenMetadata"}, {"extension": "scaledUiAmountConfig"}],
        mint_auth="M1", freeze_auth="F1")
    т0 = time.perf_counter()
    for _ in range(1000):
        svodka(тяжёлый, programma=ПРОГ_ТОКЕНА_2022)
    мс = (time.perf_counter() - т0) * 1000.0 / 1000.0
    chk(f"разбор самого тяжёлого минта (восемь расширений и оба полномочия) "
        f"занимает {мс:.4f} мс -- предел {ПРЕДЕЛ_МС} мс поверх сборки",
        мс < ПРЕДЕЛ_МС, мс)

    if упавшие:
        print("УПАЛИ: " + "; ".join(упавшие))
    print(f"\nпроверок {было}, ждали {ZHDEM_PROVEROK}, не прошло {плохо}")
    if было != ZHDEM_PROVEROK:
        print(" ПЛОХО число проверок разошлось с объявленным")
        return 1
    return 1 if плохо else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--otvet", default=None,
                   help="файл с ответом getAccountInfo (jsonParsed) по минту")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test:
        return self_test()
    if a.otvet:
        д = json.loads(Path(a.otvet).read_text(encoding="utf-8"))
        знач = (((д.get("result") or {}).get("value")) or д)
        инфо = (((знач.get("data") or {}).get("parsed") or {}).get("info") or {})
        прог = знач.get("owner")
        print(json.dumps(svodka(инфо, programma=прог), ensure_ascii=False,
                         indent=1))
        return 0
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
