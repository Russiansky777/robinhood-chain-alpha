#!/usr/bin/env python3
"""Пул отправителей: что написано в документации КАЖДОГО кандидата.

Владелец 25.09: "По документации каждого: адрес в EU, нужен ли ключ,
минимальные чаевые и tip-счета (в data/docs/, не по памяти), принимает ли
сервис транзакцию с чаевыми другим сервисам".

ПОЧЕМУ РАЗБОР, А НЕ ПАМЯТЬ. Адрес чаевых -- это адрес, на который уходят
деньги. Ошибка в одном символе означает перевод неизвестно кому, а
"кажется, у них минимум 0.001" означает молча отброшенные транзакции. Поэтому
здесь нет ни одного значения, которого нет в сохранённой странице: модуль
только вытаскивает и цитирует. Не нашёл -- так и написано "в документации не
нашлось", и это не то же самое, что "нет".

Страницы качает прогон run_sender_pool_docs_nl.yml с NL-хоста: у штаба сеть
закрыта политикой окружения, и половина доменов кандидатов недоступна вовсе.

Кандидаты и что про них надо знать:
  * Helius Sender -- уже подключён, чаевые от 0.001 SOL;
  * Nozomi (Temporal), 0slot, BlockRazor, Astralane -- подключаются;
  * Jito -- нужен для того, чтобы НАЗВАТЬ чужие счета чаевых в разборе цепи.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Алфавит base58 -- без нуля, заглавной O, I и строчной l: ровно поэтому
# случайное слово из текста в адрес не превращается.
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
АДРЕС = re.compile(r"\b[" + B58 + r"]{32,44}\b")
# Минимум чаевых: и "0.001 SOL", и "100,000 lamports", и "10000 lamports".
МИНИМУМ = re.compile(
    r"(?:(?:minimum|min\.?|at least|не менее|от)\s+)?"
    r"([0-9][0-9,._]*)\s*(lamports?|sol\b)", re.IGNORECASE)
КЛЮЧ = re.compile(r"(api[\s_-]?key|apikey|x-api-key|authorization|auth[\s_-]?token|"
                   r"bearer)", re.IGNORECASE)
ЕВРОПА = re.compile(r"\b(frankfurt|fra\b|amsterdam|ams\b|london|lon\b|paris|"
                     r"europe|eu-central|eu-west)\b", re.IGNORECASE)
ССЫЛКА = re.compile(r"https?://[^\s\"'<>)\]]+")
# Принимает ли сервис транзакцию, где чаевые заплачены ДРУГОМУ сервису.
ЧУЖИЕ_ЧАЕВЫЕ = re.compile(
    r"(jito tip|other tip|third[\s-]?party tip|any tip account|"
    r"tip to (?:another|other)|must (?:include|contain) a (?:valid )?tip|"
    r"tips? below the minimum|silently drop)", re.IGNORECASE)

# ИМЯ СЕРВИСА ПО ИМЕНИ ФАЙЛА. Файлы кладёт прогон, и он же их называет: имя
# сервиса в имени файла -- единственная связь страницы с сервисом, которую
# нельзя перепутать.
СЕРВИСЫ = {
    "helius": "Helius Sender",
    "nozomi": "Nozomi (Temporal)",
    "zeroslot": "0slot.trade",
    "blockrazor": "BlockRazor",
    "astralane": "Astralane",
    "jito": "Jito (нужен для опознания чужих чаевых)",
}


def текст_страницы(сырое: str) -> str:
    """Голый текст из HTML или markdown. Скрипты и стили выкидываем целиком:
    в них полно случайных строк, похожих на адреса."""
    без_скриптов = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", сырое)
    без_тегов = re.sub(r"(?s)<[^>]+>", " ", без_скриптов)
    return html.unescape(re.sub(r"[ \t\r\f\v]+", " ", без_тегов))


def около(текст: str, позиция: int, окно: int = 160) -> str:
    """Цитата вокруг находки -- чтобы человек видел, откуда взято."""
    л = max(0, позиция - окно // 2)
    return re.sub(r"\s+", " ", текст[л:позиция + окно // 2]).strip()


def адреса_чаевых(текст: str, *, окно: int = 400) -> list:
    """Адреса, стоящие РЯДОМ со словом про чаевые.

    Просто все base58-строки страницы брать нельзя: там же лежат адреса
    программ, примеры подписей и мусор из скриптов. Берём только те, у
    которых в окне вокруг есть слово tip (или "чаевые").
    """
    низ = текст.lower()
    метки = [м.start() for м in re.finditer(r"tip|чаев", низ)]
    из_: list = []
    видели = set()
    for м in АДРЕС.finditer(текст):
        адрес = м.group(0)
        if адрес in видели:
            continue
        # Подписи тоже base58, но длиной 87-88 символов -- они не попадут в
        # 32-44. А вот адрес программы попасть может, поэтому и нужно окно.
        if not any(abs(м.start() - позиция) <= окно for позиция in метки):
            continue
        видели.add(адрес)
        из_.append({"address": адрес, "quote": около(текст, м.start())})
    return из_


def минимумы(текст: str) -> list:
    """Все упоминания минимальной суммы чаевых -- с цитатой.

    Выбор "какой из них настоящий" человеку оставлен нарочно: в документации
    рядом могут стоять минимум сети, минимум сервиса и рекомендация.
    """
    из_ = []
    низ = текст.lower()
    for м in МИНИМУМ.finditer(текст):
        начало = max(0, м.start() - 120)
        кусок = низ[начало:м.end()]
        if not any(с in кусок for с in ("tip", "чаев")):
            continue
        число, единица = м.group(1), м.group(2).lower()
        try:
            значение = float(число.replace(",", "").replace("_", ""))
        except ValueError:
            continue
        лампорты = (значение if единица.startswith("lamport")
                    else значение * 1_000_000_000)
        из_.append({"raw": м.group(0).strip(), "lamports": int(лампорты),
                    "sol": round(лампорты / 1_000_000_000, 9),
                    "quote": около(текст, м.start())})
    return из_


def ключ_нужен(текст: str) -> dict:
    """Нужен ли ключ и как он передаётся -- по цитате, а не по догадке."""
    находки = []
    for м in КЛЮЧ.finditer(текст):
        находки.append({"what": м.group(0), "quote": около(текст, м.start())})
        if len(находки) >= 5:
            break
    return {"needed": bool(находки), "evidence": находки}


# НЕ ЕВРОПА. Список нужен, чтобы токийская ссылка не попала в европейские
# только потому, что во ФРАЗЕ РЯДОМ упомянут Франкфурт: в таблице точек входа
# регионы стоят подряд, и окно текста цепляет соседей.
НЕ_ЕВРОПА = re.compile(r"\b(tokyo|newyork|new-york|singapore|losangeles|"
                        r"los-angeles|toronto|ohio|sydney|mumbai|seoul|"
                        r"virginia|slc|ewr|tyo|sg)\b", re.IGNORECASE)


def адреса_eu(текст: str) -> list:
    """Ссылки на европейские точки входа.

    Регион берётся ИЗ САМОЙ ССЫЛКИ, и только если в ней его нет -- из строки
    рядом, да ещё и при условии, что рядом не упомянут чужой регион. Иначе в
    списке европейских оказывались бы токийские адреса, а по такому списку
    отправляют деньги.
    """
    из_ = []
    видели = set()
    for м in ССЫЛКА.finditer(текст):
        ссылка = м.group(0).rstrip(".,;")
        рядом = около(текст, м.start(), окно=200)
        if НЕ_ЕВРОПА.search(ссылка):
            continue
        откуда = None
        if ЕВРОПА.search(ссылка):
            откуда = "ссылка"
        elif ЕВРОПА.search(рядом) and not НЕ_ЕВРОПА.search(рядом):
            откуда = "текст рядом"
        if not откуда or ссылка in видели:
            continue
        видели.add(ссылка)
        из_.append({"url": ссылка, "quote": рядом, "region_from": откуда})
    return из_


def чужие_чаевые(текст: str) -> list:
    из_ = []
    for м in ЧУЖИЕ_ЧАЕВЫЕ.finditer(текст):
        из_.append({"what": м.group(0), "quote": около(текст, м.start(), окно=240)})
        if len(из_) >= 5:
            break
    return из_


def разобрать_страницу(путь: Path) -> dict:
    сырое = путь.read_text(encoding="utf-8", errors="replace")
    текст = текст_страницы(сырое)
    сервис = next((к for к in СЕРВИСЫ if путь.name.startswith(к)), None)
    return {"file": путь.name, "service": сервис, "bytes": len(сырое),
            "tip_addresses": адреса_чаевых(текст),
            "minimums": минимумы(текст),
            "key": ключ_нужен(текст),
            "eu_endpoints": адреса_eu(текст),
            "foreign_tips": чужие_чаевые(текст)}


def разобрать_каталог(каталог: Path) -> dict:
    страницы = []
    не_скачано = []
    for путь in sorted(каталог.glob("*")):
        if путь.name.endswith(".why_not.txt"):
            не_скачано.append({"file": путь.name,
                               "why_not": путь.read_text(encoding="utf-8",
                                                          errors="replace").strip()})
            continue
        if путь.is_file():
            страницы.append(разобрать_страницу(путь))
    по_сервисам: dict = {}
    for с in страницы:
        имя = с.get("service") or "неопознанный файл"
        з = по_сервисам.setdefault(имя, {"pages": [], "tip_addresses": {},
                                          "minimums": [], "eu_endpoints": [],
                                          "key_needed": False, "foreign_tips": []})
        з["pages"].append(с["file"])
        for а in с["tip_addresses"]:
            з["tip_addresses"].setdefault(а["address"], а["quote"])
        з["minimums"] += с["minimums"]
        з["eu_endpoints"] += с["eu_endpoints"]
        з["foreign_tips"] += с["foreign_tips"]
        if с["key"]["needed"]:
            з["key_needed"] = True
            з.setdefault("key_evidence", с["key"]["evidence"])
    return {"pages": страницы, "not_downloaded": не_скачано,
            "by_service": по_сервисам}


def в_таблицу(из_: dict) -> str:
    строки = ["# Пул отправителей: кандидаты по их документации", "",
              "Всё ниже -- из страниц, сохранённых в `data/docs/senders/`. "
              "Ничего по памяти: у каждого значения есть цитата в "
              "`data/sender_pool_candidates.json`.", ""]
    не = из_.get("not_downloaded") or []
    if не:
        строки += ["## Что скачать не удалось", ""]
        for н in не:
            строки.append(f"* `{н['file']}`: {н['why_not']}")
        строки.append("")
    строки += ["| сервис | страниц | нужен ключ | минимум чаевых (из текста) | "
               "счетов чаевых | точки входа в EU |", "|---|---|---|---|---|---|"]
    for имя, з in sorted((из_.get("by_service") or {}).items()):
        мины = sorted({м["lamports"] for м in з["minimums"]})
        строки.append(
            f"| {СЕРВИСЫ.get(имя, имя)} | {len(з['pages'])} | "
            f"{'да' if з['key_needed'] else 'в тексте не нашлось'} | "
            + (", ".join(f"{м} лампортов" for м in мины[:4]) or "не нашлось")
            + f" | {len(з['tip_addresses'])} | "
            + (str(len(з['eu_endpoints'])) if з['eu_endpoints'] else "не нашлось")
            + " |")
    for имя, з in sorted((из_.get("by_service") or {}).items()):
        строки += ["", f"## {СЕРВИСЫ.get(имя, имя)}", "",
                   f"Страницы: {', '.join('`' + п + '`' for п in з['pages'])}"]
        if з["eu_endpoints"]:
            строки += ["", "Точки входа в EU:"]
            for т in з["eu_endpoints"][:10]:
                строки.append(f"* `{т['url']}`")
        if з["tip_addresses"]:
            строки += ["", f"Счета чаевых ({len(з['tip_addresses'])}):"]
            for а in list(з["tip_addresses"])[:20]:
                строки.append(f"* `{а}`")
        if з["minimums"]:
            строки += ["", "Минимум чаевых -- цитаты:"]
            for м in з["minimums"][:6]:
                строки.append(f"* {м['raw']} ({м['lamports']} лампортов): "
                              f"«{м['quote']}»")
        if з.get("key_evidence"):
            строки += ["", "Ключ -- цитаты:"]
            for к in з["key_evidence"][:3]:
                строки.append(f"* {к['what']}: «{к['quote']}»")
        if з["foreign_tips"]:
            строки += ["", "Про чужие чаевые и минимум -- цитаты:"]
            for ч in з["foreign_tips"][:4]:
                строки.append(f"* {ч['what']}: «{ч['quote']}»")
    return "\n".join(строки) + "\n"


def self_test() -> int:
    проверки = []

    def chk(имя, ок, факт=""):
        проверки.append((имя, bool(ок), факт))

    страница = """
    <html><head><style>.a{font:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA}</style>
    <script>var x="ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ";</script></head><body>
    <h1>Send Transaction</h1>
    <p>Endpoint for Frankfurt: <code>http://frankfurt.example.xyz:443/sendTransaction</code></p>
    <p>Tokyo: <code>http://tokyo.example.xyz:443/sendTransaction</code></p>
    <p>The tip transfer amount is at least 100,000 Lamports (0.0001 Sol). The account
    to receive Tip is: FjmZZrFvhnqqb9ThCuMVnENaM3JGVuGWNyCAxRJcFpg9 and
    Gywj98ophM7GmkDdaWs4isqZnDdFCW7B46TXmKfvyqSm</p>
    <p>Program id 11111111111111111111111111111111 is used elsewhere.</p>
    <p>Header: <code>apikey: $auth_token</code></p>
    <p>Transactions that tip below the minimum are silently dropped.</p>
    </body></html>
    """
    текст = текст_страницы(страница)
    chk("скрипты и стили выброшены целиком -- их строки не станут адресами",
        "ZZZZZZ" not in текст and "AAAAAA" not in текст, текст[:80])
    адреса = [а["address"] for а in адреса_чаевых(текст)]
    chk("адреса чаевых взяты рядом со словом tip",
        "FjmZZrFvhnqqb9ThCuMVnENaM3JGVuGWNyCAxRJcFpg9" in адреса
        and "Gywj98ophM7GmkDdaWs4isqZnDdFCW7B46TXmKfvyqSm" in адреса, адреса)
    chk("у каждого адреса есть цитата -- видно, откуда взят",
        all(а["quote"] for а in адреса_чаевых(текст)), "")
    мины = минимумы(текст)
    chk("минимум прочитан и переведён в лампорты обоими способами",
        any(м["lamports"] == 100_000 for м in мины)
        and any(м["lamports"] == 100_000 for м in мины if "Sol" in м["raw"]
                or "Lamports" in м["raw"]), мины)
    chk("минимум без слова про чаевые в список не идёт",
        not минимумы("just 5 SOL of volume, nothing about tips"), "")
    chk("ключ опознан по заголовку, и цитата есть",
        ключ_нужен(текст)["needed"] and ключ_нужен(текст)["evidence"][0]["quote"],
        ключ_нужен(текст))
    chk("ключ не выдумывается там, где о нём не сказано",
        ключ_нужен("no auth at all here")["needed"] is False, "")
    ев = [т["url"] for т in адреса_eu(текст)]
    chk("европейская точка входа найдена, а токийская -- нет",
        any("frankfurt" in у for у in ев) and not any("tokyo" in у for у in ев), ев)
    chk("про отброшенные без минимума чаевые сказано цитатой",
        any("silently drop" in ч["what"].lower() for ч in чужие_чаевые(текст)),
        чужие_чаевые(текст))

    import tempfile  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as врем:
        врем = Path(врем)
        (врем / "blockrazor_x.html").write_text(страница, encoding="utf-8")
        (врем / "nozomi_y.html.why_not.txt").write_text(
            "НЕ СКАЧАНО: https://use.temporal.xyz/... (HTTP 000)", encoding="utf-8")
        из_ = разобрать_каталог(врем)
        chk("файл отнесён к сервису по имени файла",
            "blockrazor" in из_["by_service"], list(из_["by_service"]))
        chk("нескачанная страница названа отдельно, а не пропала",
            из_["not_downloaded"] and "HTTP 000" in из_["not_downloaded"][0]["why_not"],
            из_["not_downloaded"])
        таблица = в_таблицу(из_)
        chk("в таблице есть и сервис, и минимум, и счёт чаевых",
            "BlockRazor" in таблица and "100000 лампортов" in таблица
            and "FjmZZrFvhnqqb9ThCuMVnENaM3JGVuGWNyCAxRJcFpg9" in таблица,
            таблица[:300])
        chk("и сказано, чего скачать не удалось",
            "Что скачать не удалось" in таблица, "")

    плохих = [(и, ф) for и, ок, ф in проверки if not ок]
    for имя, ок, факт in проверки:
        print(f"  [{'ok  ' if ок else 'нет '}] {имя}" + ("" if ок else f" -- {факт}"))
    print(f"самопроверка документации отправителей: "
          f"{len(проверки) - len(плохих)}/{len(проверки)} пройдено")
    return 1 if плохих else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--docs-dir", default="data/docs/senders")
    p.add_argument("--out", default="data/sender_pool_candidates.json")
    p.add_argument("--out-md", default="docs/sender_pool_candidates.md")
    a = p.parse_args()
    if a.self_test:
        return self_test()
    каталог = Path(a.docs_dir)
    if not каталог.is_dir():
        print(f"нет каталога {каталог}")
        return 2
    из_ = разобрать_каталог(каталог)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(из_, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    Path(a.out_md).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out_md).write_text(в_таблицу(из_), encoding="utf-8")
    for имя, з in sorted((из_.get("by_service") or {}).items()):
        print(f"{имя}: страниц {len(з['pages'])}, счетов чаевых "
              f"{len(з['tip_addresses'])}, минимумов {len(з['minimums'])}, "
              f"ключ {'да' if з['key_needed'] else 'не нашлось'}")
    for н in из_.get("not_downloaded") or []:
        print(f"не скачано: {н['file']}")
    print(f"записано: {a.out} и {a.out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
