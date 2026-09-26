#!/usr/bin/env python3
"""Отчёт прогона без ключей и токенов -- иначе не коммитить.

Правило владельца: "В чат, логи, коммиты, тела запросов в журнале -- никогда."
Один раз ключ Helius уже уехал в файл репозитория внутри строки ошибки
requests, поэтому проверка стоит отдельным шагом у каждого прогона, который
пишет отчёт с хоста.

Только чтение. Возвращает 1, если в файле видно значение ключа.
"""
import re
import sys

ОБРАЗЦЫ = (
    ("api-key", r"api[-_]?key[=:\s\"']{1,3}[A-Za-z0-9._\-]{8,}"),
    ("x-token", r"x-token[\"']?\s*[:=]\s*[A-Za-z0-9._\-]{8,}"),
    ("bearer", r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}"),
    ("PRIVATE KEY", r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # Ключ Solana в base58 рядом со словом key/secret: 64-88 знаков.
    ("секрет кошелька", r"(?i)(secret|private|wallet)[-_ ]?key[\"']?\s*[:=]\s*[1-9A-HJ-NP-Za-km-z]{64,}"),
    ("токен Telegram", r"\b\d{8,12}:[A-Za-z0-9_\-]{30,}\b"),
)


def проверить(путь: str) -> int:
    try:
        with open(путь, encoding="utf-8", errors="replace") as ф:
            текст = ф.read()
    except FileNotFoundError:
        print(f"файла {путь} нет -- проверять нечего")
        return 0
    плохо = [имя for имя, обр in ОБРАЗЦЫ if re.search(обр, текст)]
    if плохо:
        print("СБОЙ: в отчёте видны значения: " + ", ".join(sorted(set(плохо))))
        return 1
    print(f"ключей в отчёте нет ({len(текст)} знаков проверено)")
    return 0


def самопроверка() -> int:
    import tempfile
    пройдено = провалено = 0

    def ок(условие, что):
        nonlocal пройдено, провалено
        if условие:
            пройдено += 1
        else:
            провалено += 1
            print(f"ПРОВАЛ: {что}")

    случаи = (
        ("https://mainnet.helius-rpc.com/?api-key=00000000-1111-2222-3333-444444444444", 1),
        ("x-token: abcdefgh12345678", 1),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123", 1),
        ("-----BEGIN OPENSSH PRIVATE KEY-----", 1),
        ("SECRET_KEY=" + "1" * 70, 1),
        ("1234567890:AAH" + "x" * 32, 1),
        ("HELIUS_API_KEY\nDBOT_API_KEY\nимена переменных без значений", 0),
        ("api-key\nключ вычищен: <url-vycishcheno>", 0),
        ("подпись 4jPfUpVAWTd46yFaVHqKVxLkojPPA4YV7UN9JHaK2eh4", 0),
    )
    with tempfile.TemporaryDirectory() as кат:
        for i, (текст, ждём) in enumerate(случаи):
            п = f"{кат}/o{i}.txt"
            with open(п, "w", encoding="utf-8") as ф:
                ф.write(текст)
            ок(проверить(п) == ждём, f"случай {i}: {текст[:40]!r} -> ждали {ждём}")
        ок(проверить(f"{кат}/нет-такого.txt") == 0, "нет файла -- не сбой")
    print(f"самопроверка проверки отчётов: {пройдено}/{пройдено + провалено} пройдено")
    return 1 if провалено else 0


if __name__ == "__main__":
    арг = sys.argv[1:]
    if not арг or арг[0] == "--self-test":
        raise SystemExit(самопроверка())
    raise SystemExit(max(проверить(п) for п in арг))
