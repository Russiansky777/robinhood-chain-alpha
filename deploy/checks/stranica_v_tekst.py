#!/usr/bin/env python3
"""Страница HTML со stdin -- в текст на stdout. Только чтение, без сети.

Нужен ради ОДНОГО: документацию стороннего потока читаем глазами с хоста, а
не пересказываем по памяти. Своего кода в прогонах это не касается.
"""
from __future__ import annotations

import html
import re
import sys


def в_текст(страница: str) -> str:
    т = re.sub(r"(?is)<(script|style|svg|noscript)[^>]*>.*?</\1>", " ", страница)
    т = re.sub(r"(?s)<[^>]+>", "\n", т)
    т = html.unescape(т)
    т = re.sub(r"[ \t]+", " ", т)
    return "\n".join(с.strip() for с in т.split("\n") if с.strip())


def _самопроверка() -> int:
    сбоев = 0

    def chk(имя: str, ок: bool) -> None:
        nonlocal сбоев
        print(f"  [{'ok  ' if ок else 'СБОЙ'}] {имя}")
        if not ок:
            сбоев += 1

    т = в_текст("<html><head><style>a{}</style></head><body><h1>Поток</h1>"
                 "<p>wss://stream.pumpapi.io&nbsp;&mdash; один сокет</p>"
                 "<script>var x=1</script></body></html>")
    chk("теги убраны, текст остался", "Поток" in т and "wss://stream.pumpapi.io" in т)
    chk("скрипты и стили выброшены", "var x" not in т and "a{}" not in т)
    chk("мнемоники раскрыты", "—" in т and "\xa0" not in т.replace("\xa0", ""))
    print(f"самопроверка страницы в текст: {3 - сбоев}/3 пройдено")
    return 1 if сбоев else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(_самопроверка())
    sys.stdout.write(в_текст(sys.stdin.read()) + "\n")
