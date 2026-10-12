#!/usr/bin/env python3
"""Архив хоста без ключей: проверка ГОТОВОГО архива перед выкладкой.

Исключения при упаковке -- это только половина дела: файл с ключом мог лежать
под другим именем. Поэтому здесь проверяется сам архив: имена файлов по
образцам И содержимое текстовых файлов небольшого размера на признаки ключей.
Нашлось подозрительное -- код возврата 1, и прогон артефакт не выкладывает.

Только чтение. Содержимое найденного файла НЕ печатается -- только путь.

ДВЕ ПРАВКИ 12.10, ПРИ ПОЛНОМ БЭКАПЕ КОНСЕРВАЦИИ.

(1) ДОБАВЛЕНЫ ТОКЕНЫ GITHUB И URL С ТОКЕНОМ. Полный бэкап кладёт в архив
    /home/bot/host_tajmery, а там у таймера учёта лежит РАБОЧАЯ КОПИЯ
    репозитория. В такой копии .git/config умеет хранить
    https://x-access-token:ТОКЕН@github.com -- настоящий секрет, который
    прежние образцы не ловили ВООБЩЕ: ни по имени файла (.git/config под
    исключения не попадает), ни по телу. Теперь ловится и то, и другое, а
    упаковщик вдобавок исключает .git целиком.

(2) ЗАКРЫТ ЛОЖНЫЙ ПОЛОЖИТЕЛЬНЫЙ НА КОДЕ. Образец api-key ловил строку
    `api_key = os.environ.get("DBOT_API_KEY", "")` -- "значением" у него
    выходил текст `os.environ.get`. Это не ключ, а чтение имени переменной, и
    такой отказ хуже пропуска: он приучает к тому, что проверка врёт, и её
    начинают обходить. Теперь значение отбрасывается, если это ЗОВ или
    известная идиома кода, и проверка на настоящий ключ при этом остаётся
    красной -- обе половины сторожит самопроверка.
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
    # РАБОЧАЯ КОПИЯ РЕПОЗИТОРИЯ В АРХИВЕ: её .git умеет хранить токен.
    (r"(?:^|/)\.git/config$", "config рабочей копии git -- там бывает токен"),
    (r"(?:^|/)\.git-credentials$", "хранилище паролей git"),
    (r"(?:^|/)\.netrc$", "netrc с паролем"),
)

# ОБРАЗЦЫ ТЕЛА. Третий элемент -- проверка значения: True значит "это правда
# похоже на ключ". Нужна там, где образец может поймать код, а не секрет.
_ИДИОМЫ_КОДА = re.compile(
    r"^(os|json|cfg|config|settings|self|env|environ|getenv|params|request|"
    r"dotenv|toml|yaml|ini|argparse|click|typer|secrets|keyring)\b")


def _похоже_на_ключ(значение: str, хвост: str) -> bool:
    """Значение -- настоящий ключ, а не чтение переменной из кода.

    ОТБРАСЫВАЕТСЯ:
      * зов функции: сразу после значения стоит '(' -- os.environ.get(...);
      * известная идиома кода в начале значения;
      * ИМЯ переменной окружения в ВЕРХНЕМ РЕГИСТРЕ (DBOT_API_KEY): имя -- не
        значение, и именно имена мы в бэкап кладём намеренно.
    """
    if хвост.startswith("("):
        return False
    if _ИДИОМЫ_КОДА.match(значение):
        return False
    if значение.isupper() and "_" in значение:
        return False
    return True


ВНУТРИ = (
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "приватный ключ в теле файла", None),
    (r"(?i)api[-_]?key\s*[=:]\s*[\"']?([A-Za-z0-9._-]{12,})",
     "api-key со значением", _похоже_на_ключ),
    (r"(?i)TELEGRAM_BOT_TOKEN\s*=\s*(\S{10,})", "токен Telegram со значением",
     _похоже_на_ключ),
    (r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b", "токен бота", None),
    (r"(?i)(?:EXEC|BLOOM|OWN_SEND|RESCUE)[A-Z0-9_]*_(?:WALLET|SENDER)_KEY\s*=\s*(\S{20,})",
     "ключ кошелька или отправителя", _похоже_на_ключ),
    # ТОКЕНЫ GITHUB -- по их собственным приставкам, их не спутать ни с чем.
    (r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b", "токен GitHub", None),
    (r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", "токен GitHub (fine-grained)", None),
    (r"https://[A-Za-z0-9._-]*:[^@/\s]{8,}@", "URL с паролем или токеном", None),
    (r"x-access-token:[^@/\s]{8,}", "токен доступа в URL git", None),
)
ПРЕДЕЛ_ЧТЕНИЯ = 512 * 1024


def находки_в_тексте(текст: str) -> list[str]:
    """Что в этом тексте похоже на секрет. Пусто -- чисто."""
    из_ = []
    for обр, что, проверка in ВНУТРИ:
        for m in re.finditer(обр, текст):
            if проверка is not None:
                значение = (m.group(1) if m.groups() else m.group(0)) or ""
                хвост = текст[m.end():m.end() + 1]
                if not проверка(значение, хвост):
                    continue
            из_.append(что)
            break
    return из_


def main() -> int:
    if len(sys.argv) < 2:
        print("нужен путь к архиву", file=sys.stderr)
        return 2
    if sys.argv[1] == "--self-test":
        return самопроверка()
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
            for что in находки_в_тексте(текст):
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


def самопроверка() -> int:
    """Обе половины правки 12.10: ложное не краснит, настоящее краснит."""
    проверки = []

    def ок(имя, усл, что=""):
        проверки.append((имя, bool(усл), что))

    # --- ЛОЖНОЕ НЕ ДОЛЖНО КРАСНИТЬ (на этом упал бэкап 12.10)
    чисто = [
        'api_key = os.environ.get("DBOT_API_KEY", "")',
        "api_key = os.getenv('HELIUS_API_KEY')",
        'API_KEY = "DBOT_API_KEY"',
        'TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]',
        'url = f"https://mainnet.helius-rpc.com/?api-key={kljuch}"',
        "BLOOM_SENDER_KEY = os.environ.get('BLOOM_SENDER_KEY')",
    ]
    for с in чисто:
        ок(f"не краснит на коде: {с[:46]}", not находки_в_тексте(с),
           находки_в_тексте(с))

    # --- НАСТОЯЩЕЕ ДОЛЖНО КРАСНИТЬ
    грязно = [
        ('api_key = "A1b2C3d4E5f6G7h8I9j0"', "api-key со значением"),
        ("api-key: 9f8e7d6c5b4a39281706", "api-key со значением"),
        ("-----BEGIN OPENSSH PRIVATE KEY-----", "приватный ключ в теле файла"),
        ("123456789:AAEhBOweik6ad9r_QwErTyUiOpAsDfGhJkLzXc", "токен бота"),
        ("ghp_" + "A" * 36, "токен GitHub"),
        ("github_pat_" + "B" * 30, "токен GitHub (fine-grained)"),
        ("url = https://x-access-token:ghs_SECRETVALUE1234@github.com/a/b.git",
         "URL с паролем или токеном"),
        ("BLOOM_LANE_WALLET_KEY=" + "C" * 44, "ключ кошелька или отправителя"),
    ]
    for с, ждём in грязно:
        н = находки_в_тексте(с)
        ок(f"краснит на настоящем: {ждём}", ждём in н, н)

    # --- ИМЕНА ФАЙЛОВ: .git/config теперь ловится
    for имя, ждём in ((".git/config", True), ("home/bot/x/.git/config", True),
                      ("home/bot/data/senders.json", False),
                      ("tmp/backup_meta/env_IMENA_tolko.txt", False)):
        поймано = any(re.search(о, имя) for о, _ in ИМЕНА)
        ок(f"имя {имя}: ловится={ждём}", поймано == ждём, поймано)

    плохо = sum(1 for _, у, _ in проверки if not у)
    for имя, у, что in проверки:
        print(f"  [{'ok  ' if у else 'СБОЙ'}] {имя}" + (f" -> {что}" if что and not у else ""))
    print(f"самопроверка архивной проверки: {len(проверки) - плохо}/{len(проверки)} пройдено")
    return 1 if плохо else 0


if __name__ == "__main__":
    raise SystemExit(main())
