#!/usr/bin/env python3
"""Свежий EXEC_WALLET_KEY: новый кошелёк, который НИКОГДА не пополняется.

СЛОВО ВЛАДЕЛЬЦА 01.10, вечер: "EXEC_WALLET_KEY не удалять, а заменить свежим
ключом (новый кошелёк, никогда не пополнять) в env и в секрете, перезапуск при
нуле позиций; адрес нового кошелька строкой".

ЗАЧЕМ ИМЕННО ЗАМЕНА, А НЕ УДАЛЕНИЕ. Прежний ключ -- ключ кошелька Bloom
4s87RRC2..., с которого 01.10 06:52:54Z ушли 0.024620982 SOL не владельцем.
Удаление переменной я уже проверял: все пять служб стартуют без неё. Но пустая
переменная -- это ещё и риск, что какой-то путь однажды попросит ключ и получит
отказ в неудобный момент. Свежий ключ пустого кошелька закрывает оба конца:
путь работает, а украсть по нему нечего, потому что кошелёк не пополняется
никогда.

ЧЕГО ЗДЕСЬ НЕТ. Приватный ключ НЕ печатается ни в поток, ни в отчёт, ни в
журнал прогона -- только адрес. Копии env не создаются: мы их только что
стёрли (24 файла env.bak.*), и плодить новые нельзя. Запись атомарная:
временный файл рядом с тем же владельцем и правами, потом os.replace.

ПРОВЕРКА ПОСЛЕ ЗАПИСИ -- ПО ФАКТУ, А НЕ ПО НАДЕЖДЕ. Файл перечитывается, из
строки EXEC_WALLET_KEY выводится публичный адрес, и он сверяется с тем, что
мы напечатали. Не совпал -- отказ прогона.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

ИМЯ = "EXEC_WALLET_KEY"


def адрес_из_секрета(секрет: str) -> dict:
    """Публичный адрес из приватного ключа. Сам ключ не возвращается никогда."""
    из_ = {"ok": False, "адрес": None, "why_not": None}
    s = (секрет or "").strip()
    if not s:
        из_["why_not"] = "секрет пуст"
        return из_
    try:
        from solders.keypair import Keypair  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"solders не загружен: {type(exc).__name__}"
        return из_
    try:
        k = (Keypair.from_bytes(bytes(json.loads(s))) if s.startswith("[")
             else Keypair.from_base58_string(s))
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"ключ не разобрался: {type(exc).__name__}"
        return из_
    из_.update(ok=True, адрес=str(k.pubkey()))
    return из_


def новый_ключ() -> dict:
    """Свежая пара. Секрет остаётся ВНУТРИ результата и наружу не печатается."""
    из_ = {"ok": False, "адрес": None, "секрет": None, "why_not": None}
    try:
        from solders.keypair import Keypair  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"solders не загружен: {type(exc).__name__}"
        return из_
    k = Keypair()
    из_.update(ok=True, адрес=str(k.pubkey()), секрет=str(k))
    return из_


def строки_env(путь: Path) -> list:
    return путь.read_text(encoding="utf-8").splitlines()


def заменить_строку(строки: list, имя: str, значение: str) -> dict:
    """Одна строка имя=значение. Нет такой строки -- дописывается в конец.

    Остальные строки не трогаются вовсе: порядок, пробелы и комментарии
    сохраняются как были. Это важно: env на хосте правили руками.
    """
    новые, нашли = [], 0
    for с in строки:
        if с.startswith(f"{имя}=") or с.startswith(f"export {имя}="):
            нашли += 1
            новые.append(f"{имя}={значение}")
        else:
            новые.append(с)
    if не_нашли := (нашли == 0):
        новые.append(f"{имя}={значение}")
    return {"строки": новые, "было_строк_с_именем": нашли, "дописали": не_нашли}


def записать_атомарно(путь: Path, строки: list) -> dict:
    """Временный файл рядом, те же права и владелец, потом os.replace."""
    из_ = {"ok": False, "why_not": None}
    try:
        ст = путь.stat()
        d = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(путь.parent),
                                         delete=False)
        врем = Path(d.name)
        with d:
            d.write("\n".join(строки) + "\n")
        os.chmod(врем, stat.S_IMODE(ст.st_mode))
        try:
            os.chown(врем, ст.st_uid, ст.st_gid)
        except PermissionError:
            pass
        os.replace(врем, путь)
        из_["ok"] = True
    except OSError as exc:
        из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    return из_


def позиций_открыто(state_dir: str | None = None) -> dict:
    """Сколько открытых позиций. ТОЛЬКО чтение состояния, без правок."""
    из_ = {"n": None, "why_not": None}
    try:
        sys.path.insert(0, os.environ.get("BLOOM_CODE_DIR") or "/home/bot/bloom_executor")
        import bloom_exec_state as ST  # noqa: PLC0415
        с = ST.ExecState() if not state_dir else ST.ExecState(base=Path(state_dir))
        из_["n"] = len(с.open_positions())
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--env", default="/etc/bloom-executor/env")
    р.add_argument("--podtverzhdenie", default="",
                    help="ровно SVEZHIJ -- иначе только показ, файл не трогается")
    р.add_argument("--trebovat-nol-pozicij", action="store_true",
                    help="отказаться, если у полосы или Bloom есть открытые позиции")
    р.add_argument("--state-dir", default=None)
    р.add_argument("--out", default=None)
    а = р.parse_args()
    путь = Path(а.env)
    итог: dict = {"файл": str(путь), "имя": ИМЯ, "записано": False,
                   "адрес_новый": None, "адрес_прежний": None,
                   "строк_до": None, "строк_после": None,
                   "было_строк_с_именем": None, "why_not": None}
    if not путь.exists():
        итог["why_not"] = f"нет файла {путь}"
        print(json.dumps(итог, ensure_ascii=False, indent=1))
        return 1
    строки = строки_env(путь)
    итог["строк_до"] = len(строки)
    # ПРЕЖНИЙ АДРЕС -- чтобы в докладе было видно, ЧТО именно заменили.
    for с in строки:
        if с.startswith(f"{ИМЯ}=") or с.startswith(f"export {ИМЯ}="):
            итог["адрес_прежний"] = адрес_из_секрета(с.split("=", 1)[1]).get("адрес")
    # НОЛЬ ПОЗИЦИЙ -- ГЕЙТ ПЕРЕД ЗАПИСЬЮ, А НЕ ПОСЛЕ. Перезапуск служб идёт
    # следом за заменой ключа, и начинать его при открытой позиции нельзя:
    # сторож продаж должен довести её до конца на том же коде.
    if а.trebovat_nol_pozicij:
        поз = позиций_открыто(а.state_dir)
        итог["позиций"] = поз
        if поз.get("n") is None:
            итог["why_not"] = (f"позиции не прочитаны ({поз.get('why_not')}) -- "
                                "неизвестность в сторону перезапуска не трактуется")
            print(json.dumps(итог, ensure_ascii=False, indent=1))
            return 1
        if поз["n"] > 0:
            итог["why_not"] = f"открытых позиций {поз['n']} -- ключ не меняем"
            print(json.dumps(итог, ensure_ascii=False, indent=1))
            return 1
    нов = новый_ключ()
    if not нов["ok"]:
        итог["why_not"] = нов["why_not"]
        print(json.dumps(итог, ensure_ascii=False, indent=1))
        return 1
    итог["адрес_новый"] = нов["адрес"]
    if а.podtverzhdenie != "SVEZHIJ":
        итог["why_not"] = ("показ без записи: нужен --podtverzhdenie SVEZHIJ. "
                            "Напечатанный адрес принадлежит ключу, который НЕ "
                            "записан и будет забыт.")
        print(json.dumps(итог, ensure_ascii=False, indent=1))
        return 0
    зам = заменить_строку(строки, ИМЯ, нов["секрет"])
    итог["было_строк_с_именем"] = зам["было_строк_с_именем"]
    итог["дописали_строку"] = зам["дописали"]
    зп = записать_атомарно(путь, зам["строки"])
    if not зп["ok"]:
        итог["why_not"] = f"запись не удалась: {зп['why_not']}"
        print(json.dumps(итог, ensure_ascii=False, indent=1))
        return 1
    # ПРОВЕРКА ПО ФАКТУ: перечитали файл и вывели адрес из записанного ключа.
    снова = строки_env(путь)
    итог["строк_после"] = len(снова)
    найден = None
    for с in снова:
        if с.startswith(f"{ИМЯ}="):
            найден = адрес_из_секрета(с.split("=", 1)[1]).get("адрес")
    итог["адрес_в_файле"] = найден
    итог["записано"] = (найден == нов["адрес"])
    if not итог["записано"]:
        итог["why_not"] = ("в файле лежит ключ другого адреса -- запись не "
                            "подтверждена")
    print(json.dumps(итог, ensure_ascii=False, indent=1))
    if а.out:
        Path(а.out).write_text(json.dumps(итог, ensure_ascii=False, indent=1) + "\n",
                                encoding="utf-8")
    return 0 if итог["записано"] else 1


def self_test() -> int:
    плохо = []

    def chk(имя, усл, факт=""):
        if not усл:
            плохо.append(f"{имя}: {факт}")
        print(("ok   " if усл else "ПЛОХО") + f" {имя}")

    н = новый_ключ()
    chk("новая пара выдана", н["ok"] and н["адрес"] and н["секрет"], н["why_not"])
    chk("адрес выводится из секрета той же парой",
        адрес_из_секрета(н["секрет"])["адрес"] == н["адрес"])
    н2 = новый_ключ()
    chk("две пары подряд -- разные адреса", н2["адрес"] != н["адрес"])
    chk("пустой секрет -- отказ словами",
        not адрес_из_секрета("")["ok"] and адрес_из_секрета("")["why_not"])
    chk("мусор вместо ключа -- отказ, а не адрес",
        not адрес_из_секрета("не-ключ-вовсе")["ok"])

    з = заменить_строку(["A=1", "EXEC_WALLET_KEY=старый", "B=2"],
                         ИМЯ, "новый")
    chk("строка заменена на месте, остальные целы",
        з["строки"] == ["A=1", "EXEC_WALLET_KEY=новый", "B=2"]
        and з["было_строк_с_именем"] == 1 and not з["дописали"], з)
    з = заменить_строку(["A=1"], ИМЯ, "новый")
    chk("нет строки -- дописали в конец, ничего не потеряв",
        з["строки"] == ["A=1", "EXEC_WALLET_KEY=новый"] and з["дописали"], з)
    з = заменить_строку(["EXEC_WALLET_KEY=1", "EXEC_WALLET_KEY=2"], ИМЯ, "н")
    chk("две строки с тем же именем -- заменены обе, расхождения не остаётся",
        з["строки"] == ["EXEC_WALLET_KEY=н", "EXEC_WALLET_KEY=н"]
        and з["было_строк_с_именем"] == 2, з)
    з = заменить_строку(["# EXEC_WALLET_KEY=коммент", "X=1"], ИМЯ, "н")
    chk("закомментированная строка не считается за переменную",
        з["строки"][0] == "# EXEC_WALLET_KEY=коммент" and з["дописали"], з)

    import tempfile as _т  # noqa: PLC0415
    with _т.TemporaryDirectory() as д:
        п = Path(д) / "env"
        п.write_text("A=1\nEXEC_WALLET_KEY=старый\nB=2\n", encoding="utf-8")
        os.chmod(п, 0o600)
        зп = записать_атомарно(п, ["A=1", "EXEC_WALLET_KEY=новый", "B=2"])
        chk("атомарная запись прошла", зп["ok"], зп["why_not"])
        chk("права файла не изменились",
            stat.S_IMODE(п.stat().st_mode) == 0o600,
            oct(stat.S_IMODE(п.stat().st_mode)))
        chk("строк столько же, и значение новое",
            строки_env(п) == ["A=1", "EXEC_WALLET_KEY=новый", "B=2"], строки_env(п))
        chk("копий рядом не появилось",
            sorted(x.name for x in Path(д).iterdir()) == ["env"],
            sorted(x.name for x in Path(д).iterdir()))

    тело = Path(__file__).read_text(encoding="utf-8").split("def self_test")[0]
    chk("секрет никуда не печатается: в рабочей части нет вывода секрета",
        'нов["секрет"]' in тело
        and 'print' not in тело.split('нов["секрет"]')[1].split("\n")[0], тело.count("print"))
    chk("в итог секрет не кладётся",
        '"секрет":' not in тело.split("def main")[1])

    print(f"\nитог: {'ВСЁ ОК' if not плохо else 'ОТКАЗ'}; проверок плохих {len(плохо)}")
    for с in плохо:
        print("  -", с)
    return 1 if плохо else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    raise SystemExit(main())
