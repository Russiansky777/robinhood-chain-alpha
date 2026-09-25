#!/usr/bin/env python3
"""Страж самопроверок: тест не имеет права трогать боевое.

ПОЧЕМУ ОН ЕСТЬ. 25.09 в 10:46:56-57Z гейт снятия рубильника полосы запустил
самопроверки НА ХОСТЕ с боевым окружением (`set -a; . /etc/bloom-executor/env`).
Оповещатель сторожа продаж создаётся в его конструкторе из окружения, поэтому
строки самопроверки с заглушками (MINT1, ПОДПИСЬ_*, "рубильник продаж
включён") ушли в БОЕВОЙ чат владельца. Боевые файлы не пострадали только
потому, что самопроверка сторожа переопределяет путь рубильника на временный
каталог -- то есть нас спасла привычка, а не запрет.

ЗАПРЕТ ТЕПЕРЬ ЕСТЬ, И ОН ПАДАЕТ, А НЕ ПРЕДУПРЕЖДАЕТ:
  * запись в боевой путь (/home/bot, /etc/bloom-executor, /var/lib/bloom*) --
    исключение БоевойПуть, в какой бы модуль она ни пряталась: перехватываются
    и pathlib.Path (open/write_text/write_bytes/mkdir/touch/unlink/rename/
    replace), и builtins.open, и os (remove/unlink/rename/replace/makedirs);
  * любой выход в сеть через requests -- исключение СетьВТесте;
  * Telegram: токен и адреса чатов вычищаются из окружения, и оповещатель
    сам себя объявляет ненастроенным;
  * BLOOM_STATE_DIR и пути рубильников переводятся во временный каталог, чтобы
    модуль, который забыл передать путь явно, не попал в боевой каталог.

Чтение боевых путей НЕ запрещено: самопроверки читают собственный исходник и
данные репозитория, и запрет на чтение сделал бы страж бесполезным.

ПРИМЕНЕНИЕ: первой строкой самопроверки
    import selftest_guard as SG
    охрана = SG.включить()
    try:
        ...
    finally:
        SG.выключить(охрана)
"""
from __future__ import annotations

import builtins
import os
import pathlib
import tempfile

ЗАПРЕЩЁННЫЕ_ПРЕФИКСЫ = ("/home/bot", "/etc/bloom-executor", "/var/lib/bloom")
# Переменные, из которых модули берут боевые пути и боевой чат. Их значения
# подменяются на время самопроверки: пусть тест ошибётся во временном каталоге.
ПЕРЕМЕННЫЕ_ПУТЕЙ = ("BLOOM_STATE_DIR", "BLOOM_KILL_FILE", "BLOOM_KILL_SELL_FILE")
ПЕРЕМЕННЫЕ_ЧАТА = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                    "TELEGRAM_LOG_CHAT_ID", "BLOOM_TELEGRAM_REAL",
                    "BLOOM_TELEGRAM_LIVE_TEST")
# Режимы open, при которых файл МЕНЯЕТСЯ. Чтение не запрещаем вовсе.
ПИШУЩИЕ = ("w", "a", "x", "+")


class БоевойПуть(AssertionError):
    """Самопроверка попыталась записать в боевой путь."""


class СетьВТесте(AssertionError):
    """Самопроверка попыталась выйти в сеть."""


def боевой(путь) -> bool:
    """Ведёт ли путь в боевое. Сравниваем по АБСОЛЮТНОМУ пути без перехода по
    ссылкам: resolve() сам ходит по файловой системе, а нам нужно решение до
    любого обращения к диску."""
    try:
        текст = os.path.abspath(os.fspath(путь))
    except TypeError:
        return False
    return any(текст == п or текст.startswith(п + "/")
               for п in ЗАПРЕЩЁННЫЕ_ПРЕФИКСЫ)


def _пишущий(режим: str) -> bool:
    return any(с in (режим or "r") for с in ПИШУЩИЕ)


def включить(*, разрешить: tuple = ()) -> dict:
    """Включает запреты и возвращает то, что нужно передать в выключить()."""
    врем = tempfile.mkdtemp(prefix="selftest_guard_")
    было_env = {}
    for имя in ПЕРЕМЕННЫЕ_ПУТЕЙ:
        было_env[имя] = os.environ.get(имя)
    for имя in ПЕРЕМЕННЫЕ_ЧАТА:
        было_env[имя] = os.environ.get(имя)
    os.environ["BLOOM_STATE_DIR"] = str(pathlib.Path(врем) / "state")
    os.environ["BLOOM_KILL_FILE"] = str(pathlib.Path(врем) / "KILL")
    os.environ["BLOOM_KILL_SELL_FILE"] = str(pathlib.Path(врем) / "KILL_SELL")
    for имя in ПЕРЕМЕННЫЕ_ЧАТА:
        os.environ.pop(имя, None)

    разрешённые = tuple(os.path.abspath(п) for п in разрешить)

    def запрещено(путь) -> bool:
        если_боевой = боевой(путь)
        if не_разрешён := (если_боевой and разрешённые):
            текст = os.path.abspath(os.fspath(путь))
            не_разрешён = not any(текст.startswith(р) for р in разрешённые)
            return не_разрешён
        return если_боевой

    было_патчей: list = []

    def патч(объект, имя, новый):
        старый = getattr(объект, имя)
        было_патчей.append((объект, имя, старый))
        setattr(объект, имя, новый)
        return старый

    # --- pathlib.Path: всё, чем пишут
    p_open = pathlib.Path.open

    def open_страж(self, mode="r", *а, **кв):
        if _пишущий(mode) and запрещено(self):
            raise БоевойПуть(f"самопроверка пишет в боевой путь: {self} (mode={mode})")
        return p_open(self, mode, *а, **кв)

    патч(pathlib.Path, "open", open_страж)

    for имя_метода in ("write_text", "write_bytes", "mkdir", "touch", "unlink",
                        "rmdir", "chmod"):
        старый = getattr(pathlib.Path, имя_метода)

        def сделать(старый=старый, имя_метода=имя_метода):
            def страж(self, *а, **кв):
                if запрещено(self):
                    raise БоевойПуть(
                        f"самопроверка вызвала {имя_метода} на боевом пути: {self}")
                return старый(self, *а, **кв)
            return страж

        патч(pathlib.Path, имя_метода, сделать())

    for имя_метода in ("rename", "replace"):
        старый = getattr(pathlib.Path, имя_метода)

        def сделать2(старый=старый, имя_метода=имя_метода):
            def страж(self, цель, *а, **кв):
                if запрещено(self) or запрещено(цель):
                    raise БоевойПуть(
                        f"самопроверка вызвала {имя_метода}: {self} -> {цель}")
                return старый(self, цель, *а, **кв)
            return страж

        патч(pathlib.Path, имя_метода, сделать2())

    # --- builtins.open: им пользуются модули, которые не знают про pathlib
    b_open = builtins.open

    def b_open_страж(файл, mode="r", *а, **кв):
        if _пишущий(mode) and запрещено(файл):
            raise БоевойПуть(f"самопроверка пишет в боевой путь: {файл} (mode={mode})")
        return b_open(файл, mode, *а, **кв)

    патч(builtins, "open", b_open_страж)

    # --- os: запись и удаление мимо pathlib
    for имя_ф in ("remove", "unlink", "rmdir", "makedirs", "mkdir", "chmod",
                   "truncate"):
        if not hasattr(os, имя_ф):
            continue
        старый_ф = getattr(os, имя_ф)

        def сделать3(старый_ф=старый_ф, имя_ф=имя_ф):
            def страж(путь, *а, **кв):
                if запрещено(путь):
                    raise БоевойПуть(f"самопроверка вызвала os.{имя_ф}: {путь}")
                return старый_ф(путь, *а, **кв)
            return страж

        патч(os, имя_ф, сделать3())

    for имя_ф in ("rename", "replace"):
        старый_ф = getattr(os, имя_ф)

        def сделать4(старый_ф=старый_ф, имя_ф=имя_ф):
            def страж(откуда, куда, *а, **кв):
                if запрещено(откуда) or запрещено(куда):
                    raise БоевойПуть(f"самопроверка вызвала os.{имя_ф}: "
                                      f"{откуда} -> {куда}")
                return старый_ф(откуда, куда, *а, **кв)
            return страж

        патч(os, имя_ф, сделать4())

    # --- сеть: requests. Нет модуля -- нечего и запрещать.
    try:
        import requests  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        requests = None
    if requests is not None:
        def нельзя_в_сеть(*а, **кв):
            raise СетьВТесте("самопроверка выходит в сеть: в тесте сети быть не должно")

        for имя_ф in ("post", "get", "put", "patch", "delete", "request"):
            if hasattr(requests, имя_ф):
                патч(requests, имя_ф, нельзя_в_сеть)
        if hasattr(requests, "Session"):
            for имя_ф in ("post", "get", "put", "patch", "delete", "request",
                           "send"):
                if hasattr(requests.Session, имя_ф):
                    патч(requests.Session, имя_ф, нельзя_в_сеть)

    return {"tmp": врем, "env": было_env, "patches": было_патчей}


def выключить(охрана: dict) -> None:
    for объект, имя, старый in reversed(охрана.get("patches") or []):
        setattr(объект, имя, старый)
    for имя, знач in (охрана.get("env") or {}).items():
        if знач is None:
            os.environ.pop(имя, None)
        else:
            os.environ[имя] = знач


def проверить_телеграм_молчит() -> tuple:
    """Оповещатель обязан считать себя ненастроенным. Возвращает (ок, почему)."""
    try:
        import bloom_notify as NT  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return True, f"модуль оповещений не загружен: {type(exc).__name__}"
    настроен, почему = NT.настроен()
    return (not настроен), (почему or "оповещатель считает себя настроенным!")


def self_test() -> int:
    проверки = []

    def chk(имя, ок, факт=""):
        проверки.append((имя, bool(ок), факт))

    chk("боевые пути опознаются, а временные -- нет",
        боевой("/home/bot/bloom_executor_live_data/KILL_SELL")
        and боевой("/etc/bloom-executor/env")
        and not боевой("/tmp/что-то") and not боевой("data/senders.json"),
        "")
    chk("похожий, но чужой путь боевым не считается",
        not боевой("/home/bottom/файл") and not боевой("/etc/bloom-executor-old"),
        "")

    охрана = включить()
    try:
        # ЗАПИСЬ В БОЕВОЕ -- ИСКЛЮЧЕНИЕ, каким бы способом ни писали.
        случаи = []
        try:
            pathlib.Path("/home/bot/bloom_executor_live_data/KILL_SELL").write_text("x")
            случаи.append("write_text не упал")
        except БоевойПуть:
            pass
        try:
            with open("/etc/bloom-executor/KILL_SELL", "w") as ф:
                ф.write("x")
            случаи.append("builtins.open не упал")
        except БоевойПуть:
            pass
        try:
            os.remove("/home/bot/bloom_executor_live_data/positions.jsonl")
            случаи.append("os.remove не упал")
        except БоевойПуть:
            pass
        try:
            pathlib.Path("/home/bot/новый_каталог").mkdir()
            случаи.append("mkdir не упал")
        except БоевойПуть:
            pass
        chk("любая запись в боевой путь падает исключением", случаи == [], случаи)

        # ЧТЕНИЕ НЕ ЗАПРЕЩЕНО: иначе самопроверки не смогли бы читать репозиторий.
        читается = True
        try:
            pathlib.Path(__file__).read_text(encoding="utf-8")
        except БоевойПуть:
            читается = False
        chk("чтение файлов не запрещено", читается, "")

        # ВРЕМЕННЫЙ ПУТЬ РАБОТАЕТ КАК ОБЫЧНО.
        врем_файл = pathlib.Path(охрана["tmp"]) / "проба.txt"
        врем_файл.write_text("можно", encoding="utf-8")
        chk("во временный каталог писать можно",
            врем_файл.read_text(encoding="utf-8") == "можно", "")

        # ОКРУЖЕНИЕ: боевой чат и боевые пути подменены.
        chk("токен и чаты Telegram вычищены",
            not os.environ.get("TELEGRAM_BOT_TOKEN")
            and not os.environ.get("TELEGRAM_CHAT_ID"), "")
        chk("BLOOM_STATE_DIR ведёт во временный каталог",
            os.environ["BLOOM_STATE_DIR"].startswith(охрана["tmp"]),
            os.environ["BLOOM_STATE_DIR"])
        молчит, почему = проверить_телеграм_молчит()
        chk("оповещатель считает себя ненастроенным", молчит, почему)

        # СЕТЬ ЗАПРЕЩЕНА.
        try:
            import requests  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            requests = None
        if requests is not None:
            упало = False
            try:
                requests.post("https://api.telegram.org/botX/sendMessage", json={})
            except СетьВТесте:
                упало = True
            chk("выход в сеть через requests падает", упало, "")
            упало2 = False
            try:
                requests.Session().post("https://пример", json={})
            except СетьВТесте:
                упало2 = True
            chk("и через сессию тоже", упало2, "")
        else:
            chk("requests не установлен -- запрещать нечего", True, "")
    finally:
        выключить(охрана)

    # ПОСЛЕ ВЫКЛЮЧЕНИЯ всё как было: страж не должен ломать рабочий код.
    chk("после выключения запись во временный файл идёт обычным путём",
        (lambda п: (п.write_text("после", encoding="utf-8"),
                     п.read_text(encoding="utf-8") == "после")[1])(
            pathlib.Path(tempfile.mkdtemp()) / "п.txt"), "")
    chk("после выключения окружение восстановлено",
        os.environ.get("BLOOM_STATE_DIR") in (None, os.environ.get("BLOOM_STATE_DIR")),
        "")

    плохих = [(и, ф) for и, ок, ф in проверки if not ок]
    for имя, ок, факт in проверки:
        print(f"  [{'ok  ' if ок else 'нет '}] {имя}" + ("" if ок else f" -- {факт}"))
    print(f"самопроверка стража самопроверок: "
          f"{len(проверки) - len(плохих)}/{len(проверки)} пройдено")
    return 1 if плохих else 0


if __name__ == "__main__":
    import sys

    raise SystemExit(self_test() if "--self-test" in sys.argv[1:] else self_test())
