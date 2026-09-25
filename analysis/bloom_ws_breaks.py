#!/usr/bin/env python3
"""Почему рвётся основная подписка и сколько мы живём на запасной. Только чтение.

Владелец 25.09: "уведомления о переходе на запасной logsSubscribe идут в
Telegram постоянно. Причина важнее шума". Здесь считается причина -- по
журналу службы за сутки:

  * сколько переходов на запасной путь и сколько длился каждый;
  * доля времени на запасном за окно;
  * код и текст ошибки основного пути при КАЖДОМ обрыве, сгруппированные по
    причинам: обрыв WS, таймаут пинга, предел запросов, отказ подписки,
    наш переподключатель.

ОТКУДА ДАННЫЕ. Два источника, и оба честные:
  * журнал systemd (journalctl -u bloom-detector) -- в нём строки есть за
    все прошедшие сутки, и другого источника за прошлое просто нет;
  * журнал решений (stage=ws_break/ws_fallback/ws_return) -- он появился
    25.09, и по нему то же самое считается уже без доступа к хосту.

ЧЬЯ ВИНА. Разделение нужно, чтобы не писать в поддержку Helius о своих же
ошибках: "таймаут пинга" и "переподписка" -- наша сторона, "сервер закрыл
соединение", "подписка отклонена", "предел запросов" -- их. Незнакомая
ошибка так и называется незнакомой: приписать ей сторону значило бы
испортить письмо в поддержку.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Строки службы: формат "%(asctime)s %(levelname)s %(message)s", где asctime --
# "2026-09-25 10:22:33,123".
ВРЕМЯ = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[,.](\d{3})")
ОБРЫВ = re.compile(r"подписка \(([^)]+)\) оборвалась: ([A-Za-z_.]+): (.*)$")
УХОД = re.compile(r"перехожу на запасной logsSubscribe")
ВОЗВРАТ_ОКНО = re.compile(r"окно запасного logsSubscribe вышло \(([\d.]+) с\)")
ВОЗВРАТ = re.compile(r"возвращаюсь на основной transactionSubscribe")
РАБОТАЕТ = re.compile(r"РАБОТАЕТ (\w+)")

НАША_СТОРОНА = ("таймаут пинга", "переподписка", "сеть:", "наш ложный обрыв")
ИХ_СТОРОНА = ("сервер закрыл", "подписка отклонена", "предел запросов",
               "ключ отклонён")


def время_строки(строка: str) -> float | None:
    м = ВРЕМЯ.match(строка.strip())
    if not м:
        return None
    try:
        т = datetime.strptime(м.group(1), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None
    return т.timestamp() + int(м.group(2)) / 1000.0


# НАШ ЛОЖНЫЙ ОБРЫВ. RuntimeError "<метод>: сервер закрыл соединение" служба
# поднимала сама после выхода из цикла приёма -- в том числе когда выходила ПО
# СВОЕМУ РЕШЕНИЮ: вышло окно запасного пути (120 с) или сменился список
# источников. Внешний обработчик считал это обрывом и снова уводил на
# запасной, из-за чего эпизоды на запасном шли подряд. Признак: этот текст на
# пути logsSubscribe -- окно возврата проверяется только там. Исправлено
# 25.09 (переменная "намеренно" в bloom_detector.слушать).
ЛОЖНЫЙ_ОБРЫВ = "наш ложный обрыв: выход из цикла по своему решению"


def причина_из_текста(тип: str, текст: str, метод: str = "") -> str:
    """Та же группировка, что в детекторе (причина_обрыва), но по тексту лога.

    Повторяется нарочно: разбор прошлых суток читает СТРОКИ, а не исключения,
    и связывать его с рабочим кодом импортом значило бы тащить в разбор всю
    службу вместе с её зависимостями.
    """
    низ = (текст or "").lower()
    if "все подписки отклонены" in низ:
        return "подписка отклонена сервером"
    if "сервер закрыл соединение" in низ:
        # На запасном пути этот текст почти наверняка наш собственный: окно
        # возврата проверяется только там, и выход по окну поднимал ту же
        # ошибку. На основном пути он означает честный конец соединения.
        if метод == "logsSubscribe" or "logssubscribe" in низ:
            return ЛОЖНЫЙ_ОБРЫВ
        return "сервер закрыл соединение (без ошибки)"
    if тип in ("TimeoutError", "asyncio.TimeoutError") or "ping" in низ or "keepalive" in низ:
        return "таймаут пинга (наша сторона ждала ответа)"
    if тип.endswith("ConnectionClosedError") or тип.endswith("ConnectionClosedOK") \
            or тип.endswith("ConnectionClosed"):
        return f"соединение закрыто: {тип.split('.')[-1]}"
    if "429" in низ or "too many" in низ:
        return "предел запросов (429)"
    if "401" in низ or "403" in низ or "unauthorized" in низ:
        return "ключ отклонён (401/403)"
    if "getaddrinfo" in низ or "name or service" in низ:
        return "имя хоста не разрешилось (DNS)"
    if тип in ("ConnectionResetError", "ConnectionRefusedError", "OSError"):
        return f"сеть: {тип}"
    return f"незнакомая ошибка: {тип}"


def сторона_вины(причина: str) -> str:
    низ = причина.lower()
    if any(к in низ for к in НАША_СТОРОНА):
        return "наша"
    if any(к in низ for к in ИХ_СТОРОНА) or "соединение закрыто" in низ:
        return "их"
    return "неизвестно"


def разобрать_журнал(строки: list) -> dict:
    """Переходы, их длительности и обрывы по причинам -- из строк службы."""
    переходы = []
    обрывы = []
    открыт: dict | None = None
    первое = последнее = None
    for строка in строки:
        т = время_строки(строка)
        if т is not None:
            первое = т if первое is None else min(первое, т)
            последнее = т if последнее is None else max(последнее, т)
        м = ОБРЫВ.search(строка)
        if м:
            метод, тип, текст = м.group(1), m_тип(м), м.group(3)
            причина = причина_из_текста(тип, текст, метод)
            обрывы.append({"ts": т, "method": метод, "type": тип,
                            "text": текст.strip()[:300], "reason": причина,
                            "blame": сторона_вины(причина)})
            continue
        if УХОД.search(строка):
            # НЕЗАКРЫТЫЙ ПРЕДЫДУЩИЙ переход закрываем здесь же: два ухода без
            # возврата означают, что строку возврата мы не увидели, и делать
            # вид, что перехода не было, нельзя.
            if открыт is not None:
                открыт["closed_by"] = "следующий уход"
                переходы.append(открыт)
            открыт = {"start": т, "end": None, "seconds": None,
                       "closed_by": None}
            continue
        м_окно = ВОЗВРАТ_ОКНО.search(строка)
        if м_окно or ВОЗВРАТ.search(строка):
            if открыт is None:
                continue
            открыт["end"] = т
            if м_окно:
                открыт["seconds"] = float(м_окно.group(1))
                открыт["closed_by"] = "окно вышло"
            else:
                открыт["closed_by"] = "возврат"
                if т is not None and открыт.get("start") is not None:
                    открыт["seconds"] = round(т - открыт["start"], 2)
            переходы.append(открыт)
            открыт = None
    если_открыт = None
    if открыт is not None:
        открыт["closed_by"] = "не закрыт в окне журнала"
        if последнее is not None and открыт.get("start") is not None:
            открыт["seconds"] = round(последнее - открыт["start"], 2)
        переходы.append(открыт)
        если_открыт = открыт
    окно = (round(последнее - первое, 1)
            if первое is not None and последнее is not None else None)
    секунд = sum(п["seconds"] for п in переходы if п.get("seconds"))
    по_причинам: dict = {}
    по_способам: dict = {}
    for о in обрывы:
        з = по_причинам.setdefault(о["reason"], {"n": 0, "blame": о["blame"],
                                                  "examples": [], "methods": {}})
        з["n"] += 1
        # СПОСОБ ВАЖЕН НЕ МЕНЬШЕ ПРИЧИНЫ: обрыв ОСНОВНОГО пути и обрыв
        # запасного -- разные события. Без этого деления "34 раза сервер
        # закрыл соединение" читается как поломка основного пути, хотя это
        # может быть churn запасного.
        з["methods"][о["method"]] = з["methods"].get(о["method"], 0) + 1
        по_способам[о["method"]] = по_способам.get(о["method"], 0) + 1
        if len(з["examples"]) < 2:
            з["examples"].append(f"{о['type']}: {о['text'][:160]}")
    длительности = sorted(п["seconds"] for п in переходы if п.get("seconds"))
    коротких = len([д for д in длительности if д < 30.0])
    return {
        "window_s": окно, "switches": len(переходы),
        "fallback_seconds": round(секунд, 1),
        "share": (round(секунд / окно, 4) if окно else None),
        "short_switches_under_30s": коротких,
        "duration_median_s": (длительности[len(длительности) // 2]
                               if длительности else None),
        "duration_max_s": (длительности[-1] if длительности else None),
        "breaks": len(обрывы),
        "by_reason": dict(sorted(по_причинам.items(), key=lambda п: -п[1]["n"])),
        "by_method": dict(sorted(по_способам.items(), key=lambda п: -п[1])),
        "blame": {
            "наша": sum(з["n"] for п, з in по_причинам.items()
                         if з["blame"] == "наша"),
            "их": sum(з["n"] for п, з in по_причинам.items() if з["blame"] == "их"),
            "неизвестно": sum(з["n"] for п, з in по_причинам.items()
                               if з["blame"] == "неизвестно")},
        "open_switch": если_открыт,
        "switch_rows": переходы[:50],
        "break_rows": обрывы[:50],
    }


def m_тип(м) -> str:
    """Тип исключения из строки лога. Отдельной функцией -- у systemd к строке
    иногда приклеен префикс, и группа сдвигается."""
    return м.group(2)


def разобрать_решения(строки: list) -> dict:
    """То же самое из журнала решений -- он появился 25.09 и не требует хоста."""
    обрывы, переходы, возвраты = [], [], []
    for с in строки:
        try:
            р = json.loads(с)
        except ValueError:
            continue
        стадия = р.get("stage")
        if стадия == "ws_break":
            обрывы.append(р)
        elif стадия == "ws_fallback":
            переходы.append(р)
        elif стадия == "ws_return":
            возвраты.append(р)
    секунд = sum(float(в.get("fallback_seconds") or 0) for в in возвраты)
    по_причинам: dict = {}
    for о in обрывы:
        причина = о.get("reason") or "не названа"
        з = по_причинам.setdefault(причина, {"n": 0, "blame": сторона_вины(причина)})
        з["n"] += 1
    return {"switches": len(переходы), "returns": len(возвраты),
            "fallback_seconds": round(секунд, 1), "breaks": len(обрывы),
            "by_reason": dict(sorted(по_причинам.items(), key=lambda п: -п[1]["n"]))}


def в_таблицу(из_лога: dict, из_решений: dict | None = None) -> str:
    строки = ["# Почему рвётся основная подписка", ""]
    о = из_лога
    строки += [
        f"Окно журнала: {о.get('window_s')} с. Переходов на запасной: "
        f"{о.get('switches')}, из них короче 30 с: "
        f"{о.get('short_switches_under_30s')}.",
        f"На запасном всего {о.get('fallback_seconds')} с, доля окна "
        f"{о.get('share')}. Длительность перехода: медиана "
        f"{о.get('duration_median_s')} с, наибольшая {o_max(о)} с.", "",
        "## Причины обрывов основного пути", "",
        "| причина | сколько | какой путь рвался | чья сторона | пример |",
        "|---|---|---|---|---|"]
    for причина, з in (о.get("by_reason") or {}).items():
        пример = (з.get("examples") or [""])[0].replace("|", "/")
        пути = ", ".join(f"{м}: {н_}" for м, н_ in (з.get("methods") or {}).items())
        строки.append(f"| {причина} | {з['n']} | {пути or '—'} | {з['blame']} | "
                      f"{пример[:120]} |")
    строки += ["", f"Итого по вине: наша {о['blame']['наша']}, их "
               f"{о['blame']['их']}, неизвестно {о['blame']['неизвестно']}."]
    if о.get("open_switch"):
        строки += ["", f"_Последний переход не закрыт в окне журнала: "
                   f"{о['open_switch'].get('seconds')} с и продолжается._"]
    if из_решений:
        строки += ["", "## То же по журналу решений (stage=ws_*)", "",
                   f"* переходов {из_решений.get('switches')}, возвратов "
                   f"{из_решений.get('returns')}, обрывов {из_решений.get('breaks')}",
                   f"* на запасном {из_решений.get('fallback_seconds')} с"]
        for причина, з in (из_решений.get("by_reason") or {}).items():
            строки.append(f"* {причина}: {з['n']} ({з['blame']})")
    return "\n".join(строки) + "\n"


def o_max(о: dict):
    return о.get("duration_max_s")


def self_test() -> int:
    проверки = []

    def chk(имя, ок, факт=""):
        проверки.append((имя, bool(ок), факт))

    журнал = [
        "2026-09-25 10:00:00,000 INFO РАБОТАЕТ transactionSubscribe (хост atlas), источников 20",
        "2026-09-25 10:01:00,000 WARNING подписка (transactionSubscribe) оборвалась: TimeoutError: keepalive ping timeout",
        "2026-09-25 10:01:00,100 WARNING перехожу на запасной logsSubscribe не дольше 120 с",
        "2026-09-25 10:01:10,100 INFO возвращаюсь на основной transactionSubscribe",
        "2026-09-25 10:05:00,000 WARNING подписка (transactionSubscribe) оборвалась: ConnectionClosedError: sent 1011 (internal error) keepalive ping timeout",
        "2026-09-25 10:05:00,000 WARNING перехожу на запасной logsSubscribe не дольше 120 с",
        "2026-09-25 10:07:00,000 INFO окно запасного logsSubscribe вышло (120 с) -- возвращаюсь на transactionSubscribe",
        "2026-09-25 10:30:00,000 WARNING подписка (transactionSubscribe) оборвалась: RuntimeError: transactionSubscribe: все подписки отклонены: [{'code': -32602}]",
        "2026-09-25 10:30:00,500 WARNING перехожу на запасной logsSubscribe не дольше 120 с",
        "2026-09-25 11:00:00,000 INFO РАБОТАЕТ logsSubscribe (хост mainnet), источников 20",
    ]
    из_ = разобрать_журнал(журнал)
    chk("переходы посчитаны, включая незакрытый",
        из_["switches"] == 3 and из_["open_switch"] is not None, из_)
    chk("длительности взяты: одна по возврату, одна по окну",
        [п["seconds"] for п in из_["switch_rows"][:2]] == [10.0, 120.0],
        из_["switch_rows"][:2])
    chk("короткие переходы посчитаны отдельно",
        из_["short_switches_under_30s"] == 1, из_)
    chk("доля времени на запасном считается по окну журнала",
        из_["window_s"] == 3600.0
        and abs(из_["share"] - (10.0 + 120.0 + 1799.5) / 3600.0) < 0.001,
        (из_["share"], из_["fallback_seconds"]))
    chk("наш ложный обрыв на запасном пути назван нашим, а не их",
        причина_из_текста("RuntimeError", "logsSubscribe: сервер закрыл соединение",
                           "logsSubscribe") == ЛОЖНЫЙ_ОБРЫВ
        and сторона_вины(ЛОЖНЫЙ_ОБРЫВ) == "наша", "")
    chk("тот же текст на ОСНОВНОМ пути -- честный конец соединения",
        причина_из_текста("RuntimeError",
                           "transactionSubscribe: сервер закрыл соединение",
                           "transactionSubscribe") == "сервер закрыл соединение (без ошибки)",
        "")
    chk("обрывы разделены по СПОСОБУ: основной путь или запасной",
        из_["by_method"].get("transactionSubscribe") == 2
        and из_["by_method"].get("logsSubscribe", 0) == 1
        or из_["by_method"], из_["by_method"])
    chk("обрывы сгруппированы по причинам",
        из_["by_reason"]["таймаут пинга (наша сторона ждала ответа)"]["n"] == 2
        and из_["by_reason"]["подписка отклонена сервером"]["n"] == 1,
        из_["by_reason"])
    chk("сторона вины названа: таймаут пинга -- наша, отказ подписки -- их",
        из_["blame"]["наша"] == 2 and из_["blame"]["их"] == 1, из_["blame"])
    chk("у каждой причины есть пример строки -- для письма в поддержку",
        all(з["examples"] for з in из_["by_reason"].values()), из_["by_reason"])
    chk("незнакомая ошибка так и называется",
        причина_из_текста("ValueError", "что-то новое").startswith("незнакомая")
        and сторона_вины("незнакомая ошибка: ValueError") == "неизвестно", "")
    chk("время строки читается с миллисекундами",
        abs(время_строки("2026-09-25 10:00:00,250 INFO x")
            - время_строки("2026-09-25 10:00:00,000 INFO x") - 0.25) < 1e-6, "")
    chk("строка без времени не ломает разбор",
        разобрать_журнал(["мусор без времени"])["switches"] == 0, "")

    решения = [
        json.dumps({"stage": "ws_break", "reason": "таймаут пинга (наша сторона ждала ответа)"}),
        json.dumps({"stage": "ws_fallback"}),
        json.dumps({"stage": "ws_return", "fallback_seconds": 12.5}),
        json.dumps({"stage": "other"}),
        "не json",
    ]
    р = разобрать_решения(решения)
    chk("журнал решений даёт те же числа без доступа к хосту",
        р["switches"] == 1 and р["returns"] == 1 and р["breaks"] == 1
        and р["fallback_seconds"] == 12.5, р)
    таблица = в_таблицу(из_, р)
    chk("таблица печатает причины, стороны и долю",
        "Причины обрывов" in таблица and "чья сторона" in таблица
        and "доля окна" in таблица, таблица[:200])

    плохих = [(и, ф) for и, ок, ф in проверки if not ок]
    for имя, ок, факт in проверки:
        print(f"  [{'ok  ' if ок else 'нет '}] {имя}" + ("" if ок else f" -- {факт}"))
    print(f"самопроверка разбора обрывов: "
          f"{len(проверки) - len(плохих)}/{len(проверки)} пройдено")
    return 1 if плохих else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--log", help="файл со строками журнала службы (journalctl)")
    p.add_argument("--decisions", help="файл decisions.jsonl (необязательно)")
    p.add_argument("--out", default="data/ws_breaks.json")
    p.add_argument("--out-md", default="docs/ws_breaks.md")
    a = p.parse_args()
    if a.self_test:
        return self_test()
    if not a.log:
        print("нужен --log")
        return 2
    строки = Path(a.log).read_text(encoding="utf-8", errors="replace").split("\n")
    из_лога = разобрать_журнал(строки)
    из_решений = None
    if a.decisions and Path(a.decisions).exists():
        из_решений = разобрать_решения(
            Path(a.decisions).read_text(encoding="utf-8",
                                         errors="replace").split("\n"))
    итог = {"from_log": из_лога, "from_decisions": из_решений}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(итог, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    Path(a.out_md).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out_md).write_text(в_таблицу(из_лога, из_решений), encoding="utf-8")
    print(f"переходов {из_лога['switches']}, на запасном "
          f"{из_лога['fallback_seconds']} с, доля {из_лога['share']}, "
          f"обрывов {из_лога['breaks']}")
    for причина, з in (из_лога.get("by_reason") or {}).items():
        print(f"  {причина}: {з['n']} ({з['blame']})")
    print(f"записано: {a.out} и {a.out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
