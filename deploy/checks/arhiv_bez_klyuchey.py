#!/usr/bin/env python3
"""Архив хоста без ключей: проверка ГОТОВОГО архива перед выкладкой.

Исключения при упаковке -- это только половина дела: файл с ключом мог лежать
под другим именем. Поэтому здесь проверяется сам архив: имена файлов по
образцам И содержимое текстовых файлов небольшого размера на признаки ключей.
Нашлось подозрительное -- код возврата 1, и прогон артефакт не выкладывает.

Только чтение. Содержимое найденного файла НЕ печатается -- только путь.
"""
import re
import sys
import tarfile

ИМЕНА = (
    (r"(?:^|/)env(?:\.|$)", "файл окружения службы"),
    (r"\.key$", "файл ключа"),
    (r"\.pem$", "файл ключа"),
    (r"(?:^|/)id_(?:rsa|ed25519|ecdsa)", "ключ ssh"),
    (r"(?i)wallet.*\.json$", "кошельковый json"),
    (r"(?i)secret", "имя со словом secret"),
    (r"(?i)keypair", "keypair"),
)
ВНУТРИ = (
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "приватный ключ в теле файла"),
    (r"(?i)api[-_]?key\s*[=:]\s*[A-Za-z0-9._-]{12,}", "api-key со значением"),
    (r"(?i)TELEGRAM_BOT_TOKEN\s*=\s*\S{10,}", "токен Telegram со значением"),
    (r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b", "токен бота"),
    (r"(?i)(EXEC|BLOOM|OWN_SEND|RESCUE)_WALLET_KEY\s*=\s*\S{20,}", "ключ кошелька"),
)
ПРЕДЕЛ_ЧТЕНИЯ = 512 * 1024


def main() -> int:
    if len(sys.argv) < 2:
        print("нужен путь к архиву", file=sys.stderr)
        return 2
    путь = sys.argv[1]
    плохо, файлов, прочитано = [], 0, 0
    with tarfile.open(путь, "r:gz") as т:
        for член in т:
            if not член.isfile():
                continue
            файлов += 1
            for обр, что in ИМЕНА:
                if re.search(обр, член.name):
                    плохо.append((член.name, что))
                    break
            if член.size > ПРЕДЕЛ_ЧТЕНИЯ:
                continue
            if not re.search(r"(?i)\.(json|txt|env|conf|cfg|service|sh|py|yml|yaml|jsonl|md)$",
                             член.name):
                continue
            try:
                данные = т.extractfile(член)
                текст = данные.read(ПРЕДЕЛ_ЧТЕНИЯ).decode("utf-8", "replace") if данные else ""
            except Exception:  # noqa: BLE001
                continue
            прочитано += 1
            for обр, что in ВНУТРИ:
                if re.search(обр, текст):
                    плохо.append((член.name, что))
                    break
    print(f"файлов в архиве {файлов}, текстовых прочитано {прочитано}")
    if плохо:
        print("СБОЙ: в архиве есть то, чего быть не должно:")
        for имя, что in плохо[:40]:
            print(f"  {имя}: {что}")
        print(f"всего находок: {len(плохо)}")
        return 1
    print("ключей и файлов окружения в архиве нет")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
