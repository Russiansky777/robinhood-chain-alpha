#!/usr/bin/env python3
"""Команды владельца из Telegram: /kill, /kill_sell, /status, /resume.

Зачем отдельным модулем. Рубильник у нас есть с первого дня, но дотянуться
до него можно было только через прогон Actions -- то есть минуты. Команда в
чат -- это секунды, и в живой торговле разница в минуту это разница в деньгах.

Что важно в устройстве, и почему именно так:

  * КОМАНДЫ ПРИНИМАЮТСЯ ТОЛЬКО ОТ ID ВЛАДЕЛЬЦА. Сравнивается и chat.id, и
    from.id -- бот может быть добавлен куда угодно, и одного chat.id мало.
    Всё остальное молча считается в счётчик "чужих": отвечать незнакомцу
    нельзя, иначе бот сам сообщает, что он живой и что-то умеет;
  * рубильник пишется в КАТАЛОГ СОСТОЯНИЯ, а не в /etc: /etc/bloom-executor
    принадлежит root, служба работает от bot и создать там файл не может.
    Второй путь honoured исполнителем так же строго -- см. kill_active;
  * /resume снимает ТОЛЬКО наши файлы из Telegram. Рубильник владельца в
    /etc он не трогает никогда: снять чужой запрет по команде из чата --
    это ровно то, чего рубильник не должен позволять;
  * опрос идёт с offset на диске. Без него после рестарта служба
    выполнила бы старые команды заново -- в том числе /resume, снимающий
    рубильник, который владелец поставил осознанно;
  * сеть здесь не мешает торговле: опрос живёт в своём потоке, любая
    ошибка идёт в счётчик и в признак жизни, а не в исключение.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_exec_state as ST  # noqa: E402

try:
    import requests  # noqa: F401
except Exception:  # noqa: BLE001
    requests = None

API = "https://api.telegram.org/bot{token}/{method}"
КОМАНДЫ = ("/kill", "/kill_sell", "/status", "/resume", "/help")


def токен() -> str:
    return (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()


def владелец() -> str:
    return (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()


def настроено() -> tuple[bool, str]:
    if not токен():
        return False, "TELEGRAM_BOT_TOKEN не задан"
    if not владелец():
        return False, "TELEGRAM_CHAT_ID не задан -- некому доверять команды"
    if requests is None:
        return False, "нет библиотеки requests"
    return True, ""


class Команды:
    """Опрос getUpdates и исполнение команд владельца."""

    def __init__(self, состояние: ST.ExecState, *, статус_фн=None,
                  таймаут_опроса: int = 25) -> None:
        self.состояние = состояние
        self.статус_фн = статус_фн
        self.таймаут_опроса = таймаут_опроса
        self.offset_путь = состояние.base / "telegram_offset.json"
        self.принято = 0
        self.чужих = 0
        self.ошибок = 0
        self.последняя_ошибка = ""
        self.последняя_команда = ""
        self.последняя_когда = ""

    # --------------------------------------------------------------- offset

    def offset(self) -> int:
        try:
            return int(json.loads(self.offset_путь.read_text(encoding="utf-8"))["offset"])
        except (OSError, ValueError, KeyError, TypeError):
            return 0

    def запомнить_offset(self, значение: int) -> None:
        ST.atomic_write_json(self.offset_путь, {"offset": int(значение)})

    # ------------------------------------------------------------- действия

    def _написать(self, путь: Path, текст: str) -> str:
        путь.write_text(текст, encoding="utf-8")
        return str(путь)

    def kill(self, кто: str) -> str:
        п = self._написать(self.состояние.kill_tg_path,
                            f"/kill из Telegram от {кто} в "
                            f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
        return ("⛔ рубильник включён: новые покупки запрещены. Сторож продолжает "
                 f"продавать открытые позиции.\nфайл: {п}")

    def kill_sell(self, кто: str) -> str:
        п = self._написать(self.состояние.kill_sell_path,
                            f"/kill_sell из Telegram от {кто} в "
                            f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
        return ("⛔ НАШИ продажи остановлены. Важно: авто-ордер Bloom живёт на "
                 "стороне площадки и может продать позицию всё равно.\n"
                 f"файл: {п}")

    def resume(self, кто: str) -> str:
        снято = []
        for путь in (self.состояние.kill_tg_path, self.состояние.kill_sell_path):
            try:
                if путь.exists():
                    путь.unlink()
                    снято.append(путь.name)
            except OSError as exc:
                return f"не удалось снять {путь.name}: {type(exc).__name__}"
        свой, _ = self.состояние.kill_active()
        хвост = ""
        if свой:
            хвост = ("\n⚠️ торговля всё равно запрещена: стоит рубильник ВЛАДЕЛЬЦА "
                      f"({self.состояние.kill_path}). Командой из чата он не снимается.")
        if not снято:
            return "нечего снимать: запретов из Telegram не было." + хвост
        return "✅ снято: " + ", ".join(снято) + хвост

    def status(self) -> str:
        if self.статус_фн is not None:
            try:
                return self.статус_фн()
            except Exception as exc:  # noqa: BLE001
                return f"статус не собрался: {type(exc).__name__}: {str(exc)[:120]}"
        убит, почему = self.состояние.kill_active()
        стоп_прод, _ = self.состояние.sell_kill_active()
        открытых = len(self.состояние.open_positions())
        return (f"режим: {'ЗАПРЕТ' if убит else 'торговля разрешена'}"
                 + (f" ({почему})" if почему else "")
                 + f"\nнаши продажи: {'остановлены' if стоп_прод else 'разрешены'}"
                 + f"\nоткрытых позиций: {открытых}")

    # -------------------------------------------------------------- разбор

    def выполнить(self, команда: str, кто: str) -> str:
        к = (команда or "").split("@")[0].strip().lower()
        if к == "/kill":
            return self.kill(кто)
        if к == "/kill_sell":
            return self.kill_sell(кто)
        if к == "/resume":
            return self.resume(кто)
        if к == "/status":
            return self.status()
        if к == "/help":
            return ("/kill -- запретить новые покупки\n"
                     "/kill_sell -- остановить НАШИ продажи\n"
                     "/resume -- снять запреты, поставленные из чата\n"
                     "/status -- состояние одной строкой")
        return ""

    def свой(self, сообщение: dict) -> tuple[bool, str]:
        """Своё ли сообщение. Сверяются и chat.id, и from.id."""
        чат = str(((сообщение.get("chat") or {}).get("id")) or "")
        автор = str(((сообщение.get("from") or {}).get("id")) or "")
        нужен = владелец()
        if нужен and чат == нужен and (not автор or автор == нужен):
            return True, автор or чат
        return False, автор or чат

    def обработать_обновления(self, обновления: list, *, отправить=None) -> list:
        """Разбор пачки getUpdates. Возвращает список (кому, что ответили)."""
        ответы = []
        макс_id = self.offset() - 1
        for u in обновления or []:
            try:
                макс_id = max(макс_id, int(u.get("update_id") or 0))
            except (TypeError, ValueError):
                pass
            сообщение = u.get("message") or u.get("edited_message") or {}
            текст = (сообщение.get("text") or "").strip()
            if not текст.startswith("/"):
                continue
            свой, кто = self.свой(сообщение)
            if not свой:
                # Незнакомцу НЕ отвечаем вовсе: ответ сам сообщает, что бот
                # живой и что-то умеет.
                self.чужих += 1
                continue
            ответ = self.выполнить(текст, кто)
            if not ответ:
                continue
            self.принято += 1
            self.последняя_команда = текст.split("@")[0].strip().lower()
            self.последняя_когда = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            ответы.append((кто, ответ))
            if отправить is not None:
                try:
                    отправить(ответ)
                except Exception as exc:  # noqa: BLE001
                    self.ошибок += 1
                    self.последняя_ошибка = f"{type(exc).__name__}: {str(exc)[:120]}"
        if макс_id >= self.offset():
            self.запомнить_offset(макс_id + 1)
        return ответы

    # ---------------------------------------------------------------- сеть

    def опросить(self, *, сессия=None) -> list:
        ок, почему = настроено()
        if not ок:
            self.последняя_ошибка = почему
            return []
        s = сессия or requests
        try:
            r = s.get(API.format(token=токен(), method="getUpdates"),
                       params={"offset": self.offset(), "timeout": self.таймаут_опроса,
                                "allowed_updates": json.dumps(["message"])},
                       timeout=self.таймаут_опроса + 10)
            j = r.json() or {}
        except Exception as exc:  # noqa: BLE001
            self.ошибок += 1
            self.последняя_ошибка = f"{type(exc).__name__}: {str(exc)[:120]}"
            return []
        if not j.get("ok"):
            self.ошибок += 1
            self.последняя_ошибка = f"ответ не ok: {str(j)[:160]}"
            return []
        return j.get("result") or []

    def ответить(self, текст: str, *, сессия=None) -> None:
        s = сессия or requests
        s.post(API.format(token=токен(), method="sendMessage"),
               json={"chat_id": владелец(), "text": текст[:3500],
                     "disable_web_page_preview": True}, timeout=15)

    def круг(self, *, сессия=None) -> list:
        обновления = self.опросить(сессия=сессия)
        return self.обработать_обновления(
            обновления, отправить=lambda t: self.ответить(t, сессия=сессия))

    def запустить_в_потоке(self) -> threading.Thread | None:
        ок, почему = настроено()
        if not ок:
            self.последняя_ошибка = почему
            return None

        def цикл():
            while True:
                try:
                    self.круг()
                except Exception as exc:  # noqa: BLE001
                    self.ошибок += 1
                    self.последняя_ошибка = f"{type(exc).__name__}: {str(exc)[:120]}"
                    time.sleep(5)

        t = threading.Thread(target=цикл, name="telegram-cmd", daemon=True)
        t.start()
        return t

    def признак_жизни(self) -> dict:
        ок, почему = настроено()
        return {"enabled": ок, "why_not": почему, "accepted": self.принято,
                "from_strangers": self.чужих, "errors": self.ошибок,
                "last_error": self.последняя_ошибка,
                "last_command": self.последняя_команда,
                "last_command_utc": self.последняя_когда,
                "offset": self.offset()}


# ------------------------------------------------------------- самопроверка

def self_test() -> None:
    import tempfile
    всего = [0, 0]

    def chk(имя, условие, факт=None):
        всего[0] += 1
        if условие:
            всего[1] += 1
            print(f"  [ok  ] {имя}")
        else:
            print(f"  [ПЛОХО] {имя} -- {факт}")

    def обновление(текст, *, чат="555", автор=None, uid=1):
        сообщение = {"chat": {"id": чат}, "text": текст}
        if автор is not None:
            сообщение["from"] = {"id": автор}
        return {"update_id": uid, "message": сообщение}

    было = dict(os.environ)
    os.environ["TELEGRAM_BOT_TOKEN"] = "ТОКЕН"
    os.environ["TELEGRAM_CHAT_ID"] = "555"
    try:
        with tempfile.TemporaryDirectory() as d:
            st = ST.ExecState(base=Path(d) / "s", kill=Path(d) / "KILL")
            к = Команды(st)
            посланное = []

            # --- /kill от владельца
            ответы = к.обработать_обновления([обновление("/kill", автор="555")],
                                              отправить=посланное.append)
            chk("команда владельца принята", len(ответы) == 1, ответы)
            chk("рубильник из Telegram создан", st.kill_tg_path.exists())
            chk("и исполнитель считает торговлю запрещённой",
                st.kill_active()[0] is True, st.kill_active())
            chk("ответ владельцу отправлен", len(посланное) == 1 and "рубильник" in посланное[0],
                посланное)

            # --- /kill_sell
            к.обработать_обновления([обновление("/kill_sell", автор="555", uid=2)])
            chk("запрет продаж создан", st.kill_sell_path.exists())
            chk("покупки он не трогает отдельно от рубильника",
                st.sell_kill_active()[0] is True)

            # --- /resume снимает ТОЛЬКО файлы из Telegram
            (Path(d) / "KILL").write_text("рубильник владельца", encoding="utf-8")
            ответы_р = к.обработать_обновления([обновление("/resume", автор="555", uid=3)])
            chk("resume снял оба файла из Telegram",
                not st.kill_tg_path.exists() and not st.kill_sell_path.exists())
            chk("но рубильник владельца остался и об этом сказано",
                (Path(d) / "KILL").exists()
                and "владельца" in ответы_р[0][1].lower(),
                ответы_р[0][1])
            chk("и торговля всё равно запрещена", st.kill_active()[0] is True)
            (Path(d) / "KILL").unlink()

            # --- чужой не получает НИЧЕГО
            посланное.clear()
            до_принято = к.принято
            ответы_ч = к.обработать_обновления(
                [обновление("/kill", чат="999", автор="999", uid=4)],
                отправить=посланное.append)
            chk("чужая команда не исполнена и без ответа",
                ответы_ч == [] and посланное == [] and к.принято == до_принято
                and not st.kill_tg_path.exists(), (ответы_ч, посланное))
            chk("чужие считаются отдельно", к.чужих == 1, к.чужих)

            # --- свой чат, но чужой автор (бот в группе)
            ответы_г = к.обработать_обновления(
                [обновление("/kill", чат="555", автор="777", uid=5)])
            chk("свой чат с чужим автором -- тоже отказ",
                ответы_г == [] and not st.kill_tg_path.exists(), ответы_г)

            # --- offset двигается и старые команды не повторяются
            chk("offset запомнен больше последнего update_id", к.offset() == 6, к.offset())
            повтор = к.обработать_обновления([обновление("/kill", автор="555", uid=2)])
            chk("повторная старая команда всё равно исполнится только по своему id",
                len(повтор) == 1, повтор)   # getUpdates по offset их уже не отдаст
            st.kill_tg_path.unlink()

            # --- /status без внешней функции
            ст = к.обработать_обновления([обновление("/status", автор="555", uid=7)])
            chk("status отвечает состоянием",
                "открытых позиций" in ст[0][1], ст[0][1])

            # --- /status со своей функцией, которая падает
            к2 = Команды(st, статус_фн=lambda: (_ for _ in ()).throw(RuntimeError("нет")))
            ст2 = к2.обработать_обновления([обновление("/status", автор="555", uid=8)])
            chk("падение сборщика статуса -- сказано, а не исключение",
                "статус не собрался" in ст2[0][1], ст2[0][1])

            # --- не команда игнорируется
            пусто = к.обработать_обновления([обновление("привет", автор="555", uid=9)])
            chk("обычный текст не команда", пусто == [], пусто)

            # --- ошибка отправки не роняет разбор
            def падает(_):
                raise RuntimeError("сеть")

            к3 = Команды(st)
            ответы3 = к3.обработать_обновления(
                [обновление("/status", автор="555", uid=10)], отправить=падает)
            chk("ошибка отправки учтена, а разбор дожил",
                len(ответы3) == 1 and к3.ошибок == 1, (ответы3, к3.ошибок))
            st.kill_tg_path.unlink(missing_ok=True)

            # --- сеть: getUpdates не ok
            class СессияНеOk:
                def get(self, *a, **kw):
                    class О:
                        @staticmethod
                        def json():
                            return {"ok": False, "description": "unauthorized"}
                    return О()

            к4 = Команды(st)
            chk("ответ не ok -- пусто и причина названа",
                к4.опросить(сессия=СессияНеOk()) == []
                and "не ok" in к4.последняя_ошибка, к4.последняя_ошибка)

            # --- без TELEGRAM_CHAT_ID команды не принимаются вообще
            os.environ.pop("TELEGRAM_CHAT_ID")
            ок, почему = настроено()
            chk("без ID владельца команды выключены",
                ок is False and "TELEGRAM_CHAT_ID" in почему, почему)
            к5 = Команды(st)
            chk("и поток не запускается", к5.запустить_в_потоке() is None)
            свой5, _ = к5.свой({"chat": {"id": "555"}, "from": {"id": "555"}})
            chk("и даже прежний ID больше не свой", свой5 is False)
    finally:
        os.environ.clear()
        os.environ.update(было)

    print(f"самопроверка команд Telegram: {всего[1]}/{всего[0]}"
          f"{' пройдено' if всего[1] == всего[0] else ' ПРОВАЛ'}")
    if всего[1] != всего[0]:
        raise SystemExit(1)


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        print("модуль команд Telegram: запускается детектором, отдельного режима нет")
