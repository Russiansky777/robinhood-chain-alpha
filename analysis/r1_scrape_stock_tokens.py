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
    # docs.robinhood.com/chain/stock-token-apis (run #2 разведки) описал
    # ЖИВОЙ read-only REST API -- https://api.robinhood.com/rhj/assets --
    # это лучше, чем парсить документацию: полный актуальный реестр
    # сток-токенов с реальными адресами, а не пример из документации.
    "assets_api_probe": "https://api.robinhood.com/rhj/assets",
}

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


def main() -> int:
    probe = "--probe" in sys.argv
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(timezone.utc).isoformat()

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
        results[name] = {
            "url": url,
            "status": status,
            "fetched_at": fetched_at,
            "text_len": len(text),
            "candidate_addresses": candidates[:200],  # кап, чтобы не раздувать JSON зонда
        }
        if probe:
            results[name]["text_dump"] = text[:150000]

    out_file = CACHE_DIR / ("r1_stock_tokens_probe.json" if probe else "r1_stock_tokens_raw.json")
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
