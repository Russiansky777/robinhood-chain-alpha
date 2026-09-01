#!/usr/bin/env python3
"""Sprint R1, Шаг 1: бесплатная разведка -- реестр сток-токенов и адреса
их Chainlink-фидов с docs.robinhood.com (см. docs/R1_DESIGN.md, §1
Шаг 1). НЕ трогает Dune, 0 кредитов -- чистое HTTP-чтение публичной
документации.

ВАЖНО: интерактивная песочница агента в этой сессии блокирует egress на
docs.robinhood.com/docs.chain.link (EGRESS_BLOCKED через WebFetch) --
поэтому этот скрипт запускается через GitHub Actions runner (обычный
исходящий интернет, не через ограничивающий прокси инструмента). Первый
прогон -- ЗОНД (--probe): сохраняет сырой текст страниц в кэш для
разбора глазами (структура таблиц заранее неизвестна), плюс best-effort
эвристическое извлечение пар (символ, 0x-адрес) через regex. Второй
прогон (после того как штаб/агент увидел реальную структуру по логу) --
уточнённый парсинг в r1_stock_tokens.json с полем "source" на каждую
запись.

Использование: python analysis/r1_scrape_stock_tokens.py [--probe]
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from config import CONFIG

CACHE_DIR = Path(CONFIG.r1_cache_dir)

URLS = {
    # SC1 (владелец, критерий): "цена газа сети ПОСЛЕ ОКОНЧАНИЯ ВЕЙВЕРА"
    # -- не наблюдаема ончейн сейчас (вейвер ещё активен до ~29.09.2026,
    # см. docs/PROJECT_STATE.md), нужен официально заявленный параметр.
    "gas_and_fees": "https://docs.robinhood.com/chain/gas-and-fees",
}

# run #7: таблица фидов -- Astro-остров component-export="FeedList",
# client="idle" -- данные приходят client-side (не в SSR HTML) через
# API, вызываемый ИЗНУТРИ бандла /_astro/index.CujZUUH3.js. Вместо
# исполнения JS (Playwright не установлен в GH Actions runner) --
# читаем сам бандл как текст и ищем в нём URL API (обычно строковый
# литерал в минифицированном JS).
JS_BUNDLE_URLS = {
    "feed_list_bundle": "https://docs.chain.link/_astro/index.CujZUUH3.js",
}
URL_IN_JS_RE = re.compile(r'https?://[a-zA-Z0-9\-\.]+(?:/[^\s"\'\\)]*)?')

ADDR_RE = re.compile(r"0x[a-fA-F0-9]{40}")
HTML_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(html: str) -> str:
    """Грубая, но достаточная для зонда конверсия HTML->текст: убирает
    теги, схлопывает пробелы. Не парсер разметки -- только для просмотра
    глазами в логе и regex-поиска адресов рядом с текстом."""
    text = HTML_TAG_RE.sub(" ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def fetch(url: str) -> tuple[int, str]:
    resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0 (r1-recon-bot)"})
    return resp.status_code, resp.text


def find_candidate_pairs(text: str, window: int = 80) -> list[dict]:
    """Эвристика зонда: для каждого найденного 0x-адреса берёт немного
    текста ДО него (обычно там тикер/название в табличной разметке) --
    НЕ окончательный парсинг, только чтобы увидеть в логе, похоже ли это
    на таблицу токенов."""
    out = []
    for m in ADDR_RE.finditer(text):
        start = max(0, m.start() - window)
        out.append({"address": m.group(0), "context_before": text[start:m.start()].strip()})
    return out


def git_commit(paths: list[Path], message: str) -> None:
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=False)
        subprocess.run(
            ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
            check=False,
        )
        subprocess.run(["git", "add", *[str(p) for p in paths]], check=False)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
        if diff.returncode == 0:
            print("[r1_scrape] Нечего коммитить.")
            return
        subprocess.run(["git", "commit", "-m", message], check=False)
        subprocess.run(["git", "push"], check=False)
    except Exception as exc:
        print(f"[r1_scrape] ПРЕДУПРЕЖДЕНИЕ: не удалось закоммитить: {exc}")


def find_astro_island_props(html: str, anchor_text: str, window: int = 4000) -> list[str]:
    """Зонд-хелпер (run #4 нашёл, что таблица фидов на docs.chain.link --
    Astro-остров: рендерится клиентским JS, в исходном HTML только тег
    <astro-island ... props="..."> с JSON-пропсами для гидрации (или
    без данных вовсе, если гидрация полностью клиентская). Ищет
    anchor_text в СЫРОМ HTML (до strip_html) и возвращает срез вокруг
    каждого вхождения -- для разбора структуры глазами."""
    out = []
    for m in re.finditer(re.escape(anchor_text), html):
        start = max(0, m.start() - 200)
        out.append(html[start:m.start() + window])
    return out


ASTRO_ISLAND_RE = re.compile(r'<astro-island\b[^>]*\bprops="([^"]*)"[^>]*>', re.I)
COMPONENT_URL_RE = re.compile(r'\bcomponent-url="([^"]*)"')


def dump_astro_island_props(html: str) -> list[dict]:
    """Точное извлечение props= из каждого <astro-island> тега (не окно
    текста вокруг, а сам JSON пропсов, который Astro использует для SSR
    -- если реестр фидов передан через пропсы при рендере, он будет
    здесь; если пусто/нет тикеров -- значит гидрация полностью
    клиентская без начальных данных, нужен другой источник."""
    out = []
    for m in re.finditer(r"<astro-island\b[^>]*>", html, re.I):
        tag = m.group(0)
        props_m = re.search(r'props="([^"]*)"', tag)
        comp_m = re.search(r'component-url="([^"]*)"', tag)
        props_raw = props_m.group(1) if props_m else ""
        import html as html_mod

        props_unescaped = html_mod.unescape(props_raw)
        out.append({
            "component_url": comp_m.group(1) if comp_m else None,
            "props_len": len(props_unescaped),
            "props_preview": props_unescaped[:2000],
        })
    return out


def scan_js_bundle_for_api_urls() -> dict:
    """Fetch a known JS bundle and extract candidate API URLs / relative
    fetch paths -- see JS_BUNDLE_URLS docstring above."""
    out = {}
    for name, url in JS_BUNDLE_URLS.items():
        print(f"\n===== {name} ({url}) =====")
        try:
            status, js = fetch(url)
        except requests.RequestException as exc:
            print(f"[r1_scrape] ОШИБКА сети для {url}: {exc}")
            out[name] = {"url": url, "error": str(exc)}
            continue
        print(f"status={status}, js_len={len(js)}")
        abs_urls = sorted(set(URL_IN_JS_RE.findall(js)))
        # Относительные пути вида fetch("/api/...") или похожие -- тоже
        # кандидаты (сервер тот же, что страница).
        rel_urls = sorted(set(re.findall(r'["\'](/[a-zA-Z0-9_\-/\.]{3,80}(?:feed|price|address|api)[a-zA-Z0-9_\-/\.]*)["\']', js, re.I)))
        print(f"абсолютных URL в бандле: {len(abs_urls)}, релевантных относительных путей: {len(rel_urls)}")
        for u in abs_urls[:50]:
            print("  ABS:", u)
        for u in rel_urls[:50]:
            print("  REL:", u)
        out[name] = {"url": url, "status": status, "js_len": len(js), "abs_urls": abs_urls[:200], "rel_urls": rel_urls[:200]}
    return out


def main() -> int:
    probe = "--probe" in sys.argv
    raw_probe = "--raw" in sys.argv
    island_dump = "--islands" in sys.argv
    jsbundle = "--jsbundle" in sys.argv
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(timezone.utc).isoformat()

    if jsbundle:
        out = scan_js_bundle_for_api_urls()
        out_file = CACHE_DIR / "r1_stock_tokens_jsbundle.json"
        out_file.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print(f"\n[r1_scrape] Записано: {out_file}")
        git_commit([out_file], "sprintR1_cache: URL-кандидаты из JS-бандла FeedList [automated, 0 кредитов -- не Dune]")
        return 0

    results: dict[str, dict] = {}
    for name, url in URLS.items():
        print(f"\n===== {name} ({url}) =====")
        try:
            status, html = fetch(url)
        except requests.RequestException as exc:
            print(f"[r1_scrape] ОШИБКА сети для {url}: {exc}")
            results[name] = {"url": url, "error": str(exc), "fetched_at": fetched_at}
            continue
        text = strip_html(html)
        print(f"status={status}, html_len={len(html)}, text_len={len(text)}")
        candidates = find_candidate_pairs(text)
        print(f"найдено 0x-адресов: {len(candidates)}")
        # Печатаем ПОЛНОСТЬЮ в лог для разбора глазами (зонд), но не
        # больше разумного -- страница документации, не мегабайты.
        if probe:
            print("---- TEXT DUMP (для разбора) ----")
            print(text[:150000])
            print("---- КОНЕЦ TEXT DUMP ----")
        if raw_probe:
            for anchor in ("astro-island", "Available Robinhood"):
                slices = find_astro_island_props(html, anchor)
                print(f"---- RAW HTML вокруг '{anchor}' ({len(slices)} вхождений) ----")
                for s in slices[:3]:
                    print(s)
                    print("... [срез] ...")
                print(f"---- КОНЕЦ RAW '{anchor}' ----")
        results[name] = {
            "url": url,
            "status": status,
            "fetched_at": fetched_at,
            "text_len": len(text),
            "candidate_addresses": candidates[:200],  # кап, чтобы не раздувать JSON зонда
        }
        if probe:
            results[name]["text_dump"] = text[:150000]
        if island_dump:
            islands = dump_astro_island_props(html)
            print(f"найдено <astro-island> тегов: {len(islands)}")
            results[name]["astro_islands"] = islands
            # run #6: ни один astro-island не похож на таблицу фидов
            # (только чехол UI -- NavBar/Header/TOC/...). Значит таблица
            # -- либо обычный <table> в статичном HTML, либо данные для
            # неё приходят отдельным client-side fetch(). Вырезаем сырой
            # срез между двумя якорями секции для разбора глазами.
            start_anchor = "The following table shows"
            end_anchor = "Total Return Value calculation"
            si = html.find(start_anchor)
            ei = html.find(end_anchor, si) if si >= 0 else -1
            if si >= 0:
                section = html[si:ei if ei > si else si + 20000]
                results[name]["feed_table_section_raw"] = section
                print(f"[r1_scrape] Секция таблицы фидов: {len(section)} символов сырого HTML.")
            else:
                print("[r1_scrape] Якорь 'The following table shows' не найден в сыром HTML.")

    if probe:
        out_name = "r1_stock_tokens_probe.json"
    elif island_dump:
        out_name = "r1_stock_tokens_islands.json"
    else:
        out_name = "r1_stock_tokens_raw.json"
    out_file = CACHE_DIR / out_name
    out_file.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\n[r1_scrape] Записано: {out_file}")
    git_commit(
        [out_file],
        f"sprintR1_cache: {'зонд' if probe else 'сырые данные'} по сток-токенам с docs.robinhood.com "
        "[automated, 0 кредитов -- не Dune]",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
