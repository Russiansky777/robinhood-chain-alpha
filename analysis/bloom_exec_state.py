#!/usr/bin/env python3
"""Состояние исполнителя Bloom: тормоза раньше газа.

Этот модуль не умеет отправлять ордера. Он умеет запрещать их отправку.
Всё, что здесь есть, существует потому, что без этого код, распоряжающийся
реальными деньгами, теряет их предсказуемым образом:

  * ЖУРНАЛ НАМЕРЕНИЙ с fsync ДО отправки. Служба перезапускается
    (Restart=always), и если позиция известна только процессу, то после
    падения между покупкой и продажей она остаётся без выхода навсегда.
  * ДНЕВНОЙ ЛИМИТ ПОТЕРЬ В ФАЙЛЕ с ключом по дате. Счётчик в памяти
    обнуляется рестартом: десять рестартов -- десять дневных лимитов.
  * KILL-SWITCH FAIL-CLOSED. Битый или недоступный файл рубильника
    означает ЗАПРЕТ торговли, а не разрешение: иначе он не сработает
    ровно тогда, когда его дёрнули в панике.
  * ДЕДУП ПО ПОДПИСИ И ПО МИНТУ на диске. Дедуп в памяти после рестарта
    считает старые подписи новыми; дедупа по минту в зонде нет вовсе, и
    без него два источника на одном токене дают двойную позицию, а лимит
    "не больше пяти позиций" обходится концентрацией.
  * СЧЁТЧИКИ ОТКАЗОВ отдельно для ошибок API и отдельно для 429. Наш же
    всплеск не должен закрывать торговлю автопаузой по "пять ошибок
    подряд": 429 -- это не отказ сервиса, а наш темп.

Сутки считаются по Мадриду -- так распорядился владелец.
Сети этот модуль не требует вообще.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Кошелёк исполнителя -- КОНСТАНТА. Второй кошелёк в аккаунте Bloom
# (AvFRdagi..., тестовый W1 владельца) торговать не должен ничем и никогда,
# поэтому адрес не берётся из конфига и сверяется перед каждой отправкой.
EXECUTOR_WALLET = "4s87RRC2V2XAJD6R8U2dP8kQH99Z2wA6fg88ZVfV4j4N"
FOREIGN_WALLET_W1 = "AvFRdagiRpjxGZnZcAThaMv6MLRtEaFGVZStKn3X2LwD"

DAY_TZ = "Europe/Madrid"

# Лимиты по умолчанию. Все переопределяются окружением, но ни один не
# выключается: значение 0 или отрицательное трактуется как "запрещено".
DEFAULT_MAX_OPEN = 5
DEFAULT_DAILY_LOSS_SOL = 1.0
DEFAULT_BUY_SOL = 0.2
DEFAULT_FEE_RESERVE_SOL = 0.02       # приоритетка + чаевые + рента счёта
DEFAULT_API_ERROR_STREAK = 5
DEFAULT_UNSOLD_STREAK = 3
DEFAULT_RATE_LIMITED_PAUSE_S = 300.0  # 429 дольше пяти минут подряд
DEFAULT_MINT_COOLDOWN_S = 900.0
DEFAULT_MAX_BUYS_PER_MINT = 2         # семантика maxBuyTimesPerToken=2
DEFAULT_SEEN_TTL_S = 24 * 3600

# Недельный бюджет запросов Bloom: 10 000 на пользователя, общий на все
# ключи и оба региона. Останавливаемся заранее, а не в момент отказа.
WEEK_BUDGET = 10_000
WEEK_STOP_AT = 0.80

STATES_OPEN = ("intent", "bought", "selling", "unsold")

# МЕТКА ПОЛОСЫ СВОЕЙ ОТПРАВКИ. Одна на весь репозиторий: по ней позиции
# полосы отделяются от покупок Bloom ВЕЗДЕ -- в лимите открытых, в запрете
# повторной покупки минта, в серии непроданных и в продаже. Слово владельца
# 25.09: полоса не должна ни занимать место Bloom, ни блокировать ему минт,
# ни останавливать его своим UNSOLD. Два разных написания этой метки в двух
# файлах означали бы, что где-то разделение молча не работает.
МЕТКА_ПОЛОСЫ = "own_send"

# Три режима позиции. Разделение не косметическое:
#   dry-run   -- позиции существуют только в журнале, за ними НЕТ сделок.
#                Они не учитываются НИГДЕ: ни в can_open, ни у сторожа, ни
#                в отчётах, ни в сверке. Иначе пять придуманных позиций
#                закроют гейт для настоящих, а сторож пойдёт искать по цепи
#                токены, которых никто не покупал.
#   live-test  -- настоящие деньги на стенде владельца. Продаются и
#                сторожатся по-настоящему, но в пары А/Б не идут.
#   live       -- боевые позиции по реальным источникам.
MODE_DRY = "dry-run"
MODE_LIVE_TEST = "live-test"
MODE_LIVE = "live"
MODES_REAL = (MODE_LIVE_TEST, MODE_LIVE)


def is_real_mode(mode) -> bool:
    """Настоящая ли позиция. Неизвестный режим считается НАСТОЯЩИМ.

    Специально так: если в журнале окажется запись с режимом, которого мы
    не знаем, безопаснее посторожить лишнее, чем пропустить открытую
    позицию с деньгами.
    """
    return mode != MODE_DRY

# Версия формата журналов и состояния. Ключи переведены на ASCII 23.09 по
# слову владельца; версия нужна, чтобы потребитель не гадал, кириллица
# перед ним или латиница, а СПРОСИЛ. Записи версии 1 (кириллические
# ключи) больше не пишутся; прочитать их можно только зная, что это v1.
SCHEMA_VERSION = 2
SCHEMA_VERSION_KEY = "schema_version"

# Машиночитаемые коды отказов can_open_detailed. Текст причины пишется
# людям, код -- в журнал и в сверку: по тексту сверка ломается от любой
# правки формулировки, а SKIPPED_DUP_MINT владелец просил считать
# отдельной строкой, а не расхождением.
КОД_ОК = "OK"
КОД_РУБИЛЬНИК = "KILL_SWITCH"
КОД_СЧЁТЧИКИ_БИТЫ = "COUNTERS_CORRUPT"
КОД_ПАУЗА_API = "PAUSED_API_ERRORS"
КОД_ПАУЗА_НЕПРОДАНО = "PAUSED_UNSOLD"
КОД_ПАУЗА_429 = "PAUSED_RATE_LIMITED"
КОД_БЮДЖЕТ_НЕДЕЛИ = "WEEK_BUDGET"
КОД_ЛИМИТ_ОТКРЫТЫХ = "MAX_OPEN_POSITIONS"
КОД_ДУБЛЬ_МИНТА = "SKIPPED_DUP_MINT"
КОД_ПОКУПОК_НА_МИНТ = "MAX_BUY_TIMES_PER_TOKEN_REACHED"
КОД_ПОДПИСЬ_ВИДЕЛИ = "SIGNATURE_SEEN"
КОД_ДНЕВНОЙ_УБЫТОК = "DAILY_LOSS_LIMIT"
КОД_БАЛАНС = "INSUFFICIENT_BALANCE"
STATE_CLOSED = "closed"

# Разобранные ротированные журналы позиций: {путь: (время_правки, размер, dict)}.
# Ротированный файл после ротации не меняется, поэтому кэш точен.
_КЭШ_РОТАЦИИ: dict = {}


def state_dir() -> Path:
    p = os.environ.get("BLOOM_STATE_DIR", "").strip()
    return Path(p) if p else (REPO_ROOT / "data" / "bloom_state")


def kill_file() -> Path:
    p = os.environ.get("BLOOM_KILL_FILE", "").strip()
    return Path(p) if p else Path("/etc/bloom-executor/KILL")


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[состояние] {name}={raw!r} не число -- беру умолчание {default}")
        return default


def env_int(name: str, default: int) -> int:
    return int(env_float(name, float(default)))


def day_key(ts: float | None = None) -> tuple[str, bool]:
    """Дата по Мадриду и признак, что часовой пояс недоступен.

    Молча падать на UTC нельзя: смена суток сдвинется на час или два, и
    дневной лимит потерь будет считаться не за те сутки. Поэтому признак
    возвращается наружу и попадает в отчёт.
    """
    t = ts if ts is not None else time.time()
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415
        import datetime as dt  # noqa: PLC0415
        return dt.datetime.fromtimestamp(t, ZoneInfo(DAY_TZ)).strftime("%Y-%m-%d"), False
    except Exception:  # noqa: BLE001 -- любая причина: нет tzdata, нет модуля
        return time.strftime("%Y-%m-%d", time.gmtime(t)), True


def atomic_write_json(path: Path, data) -> None:
    """Запись целиком или никак: .tmp + fsync + replace.

    Без этого читатель может поймать обрезанный JSON -- ровно то, из-за
    чего kill-switch не срабатывает в нужный момент.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def append_jsonl_fsync(path: Path, row: dict) -> None:
    """Дописать строку и ДОЖДАТЬСЯ диска.

    Именно fsync отличает журнал намерений от журнала пожеланий: без него
    запись о покупке может не дожить до перезапуска, и позиция потеряется.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


class ExecState:
    """Состояние на диске. Ни одного решения в памяти, которое нельзя
    восстановить после рестарта."""

    def __init__(self, base: Path | None = None, kill: Path | None = None) -> None:
        self.base = Path(base) if base else state_dir()
        self.base.mkdir(parents=True, exist_ok=True)
        self.kill_path = Path(kill) if kill else kill_file()
        # ВТОРОЙ рубильник -- в каталоге состояния. Первый лежит в
        # /etc/bloom-executor и принадлежит root: служба работает от bot и
        # создать его НЕ МОЖЕТ, только прочитать. Значит команда владельца из
        # Telegram через него не сработала бы вовсе. Этот путь служба пишет
        # сама, и он honoured так же строго: любой из двух файлов -- запрет.
        self.kill_tg_path = self.base / "KILL_BY_TELEGRAM"
        # РУБИЛЬНИК ТОЛЬКО BLOOM (слово владельца 25.09: "два отдельных файла
        # KILL: Bloom и полоса"). Общий KILL глушит ВСЁ, включая полосу: она
        # сверяется с ним раньше своего KILL_OWN_SEND. Чтобы остановить одну
        # площадку и не трогать полосу, нужен свой файл -- вот он. Лежит рядом
        # с общим, в каталоге env, и читается так же строго.
        self.kill_bloom_path = Path(
            os.environ.get("BLOOM_KILL_BUY_FILE")
            or (self.kill_path.parent / "KILL_BLOOM"))
        # Отдельный рубильник ТОЛЬКО на продажу: сторож перестаёт продавать,
        # покупки при этом решает первый рубильник. Нужен раздельно, потому
        # что "перестань продавать" и "перестань торговать" -- разные приказы.
        self.kill_sell_path = self.base / "KILL_SELL_BY_TELEGRAM"
        # РУБИЛЬНИК ТОЛЬКО ПОЛОСЫ своей отправки. Слово владельца 25.09:
        # любая денежная странность -- KILL полосы, а Bloom и тень продолжают.
        # Отдельный файл именно поэтому: общий рубильник остановил бы всё.
        self.kill_lane_path = self.base / "KILL_OWN_SEND"
        self.positions_path = self.base / "positions.jsonl"
        self.decisions_path = self.base / "decisions.jsonl"
        self.api_path = self.base / "api_calls.jsonl"
        self.counters_path = self.base / "counters.json"
        self.budget_path = self.base / "bloom_budget.json"
        self.db_path = self.base / "dedup.sqlite"
        self.max_open = env_int("BLOOM_MAX_OPEN", DEFAULT_MAX_OPEN)
        self.daily_loss_sol = env_float("BLOOM_DAILY_LOSS_SOL", DEFAULT_DAILY_LOSS_SOL)
        self.buy_sol = env_float("BLOOM_BUY_SOL", DEFAULT_BUY_SOL)
        self.fee_reserve_sol = env_float("BLOOM_FEE_RESERVE_SOL", DEFAULT_FEE_RESERVE_SOL)
        self.api_error_streak_max = env_int("BLOOM_API_ERROR_STREAK", DEFAULT_API_ERROR_STREAK)
        self.unsold_streak_max = env_int("BLOOM_UNSOLD_STREAK", DEFAULT_UNSOLD_STREAK)
        self.rate_limited_pause_s = env_float("BLOOM_RATE_LIMITED_PAUSE_S",
                                               DEFAULT_RATE_LIMITED_PAUSE_S)
        self.mint_cooldown_s = env_float("BLOOM_MINT_COOLDOWN_S", DEFAULT_MINT_COOLDOWN_S)
        self.max_buys_per_mint = env_int("BLOOM_MAX_BUYS_PER_MINT", DEFAULT_MAX_BUYS_PER_MINT)
        self.seen_ttl_s = env_float("BLOOM_SEEN_TTL_S", DEFAULT_SEEN_TTL_S)
        self._db = None
        # Соединения SQLite по потокам: см. db(). Делить одно соединение
        # между потоками нельзя -- это уже стоило пропущенного сигнала.
        self._потоковое = threading.local()

    # ------------------------------------------------------------- рубильник

    def kill_active(self) -> tuple[bool, str]:
        """Рубильник. ЛЮБАЯ неясность -- запрет.

        Файл есть -> запрет. Файла нет -> разрешено. Ошибка проверки
        (права, битый путь, что угодно) -> ЗАПРЕТ. Специально так: цена
        ложного запрета -- пропущенная сделка, цена ложного разрешения --
        неограниченный убыток.
        """
        try:
            for путь, чей in ((self.kill_path, "рубильник"),
                               (self.kill_tg_path, "рубильник из Telegram")):
                if путь.exists():
                    try:
                        причина = путь.read_text(encoding="utf-8").strip()[:200]
                    except OSError:
                        причина = "(файл рубильника не читается -- всё равно запрет)"
                    return True, f"{чей} включён: {причина or 'без пояснения'}"
            return False, ""
        except Exception as exc:  # noqa: BLE001
            return True, (f"проверка рубильника не удалась ({type(exc).__name__}) -- "
                           "торговля запрещена, потому что неясность трактуется как запрет")

    def kill_bloom_active(self) -> tuple[bool, str]:
        """Запрет покупок ТОЛЬКО через Bloom. Полосы не касается.

        Та же строгость, что у общего: файл есть -> запрет, ошибка проверки ->
        запрет. Полоса этот файл не читает вовсе -- в том и смысл: остановить
        площадку, не останавливая свою отправку.
        """
        try:
            if self.kill_bloom_path.exists():
                try:
                    причина = self.kill_bloom_path.read_text(
                        encoding="utf-8").strip()[:200]
                except OSError:
                    причина = "(файл не читается -- всё равно запрет)"
                return True, (f"рубильник Bloom включён: {причина or 'без пояснения'}")
            return False, ""
        except Exception as exc:  # noqa: BLE001
            return True, (f"проверка рубильника Bloom не удалась "
                           f"({type(exc).__name__}) -- покупки через площадку "
                           "запрещены: неясность трактуется как запрет")

    def sell_kill_active(self) -> tuple[bool, str]:
        """Запрет ТОЛЬКО на продажу. Та же строгость: неясность -- запрет.

        Останов продажи не останавливает авто-ордер Bloom: он живёт на
        стороне площадки. То есть это запрет НАШИМ попыткам продажи, и в
        докладе он называется именно так, чтобы никто не решил, что позиция
        защищена от продажи вообще.
        """
        try:
            if self.kill_sell_path.exists():
                try:
                    причина = self.kill_sell_path.read_text(encoding="utf-8").strip()[:200]
                except OSError:
                    причина = "(файл не читается -- всё равно запрет)"
                return True, f"продажи остановлены из Telegram: {причина or 'без пояснения'}"
            return False, ""
        except Exception as exc:  # noqa: BLE001
            return True, (f"проверка запрета продаж не удалась ({type(exc).__name__}) -- "
                           "считаем запретом")

    def lane_kill_active(self) -> tuple[bool, str]:
        """Запрет ТОЛЬКО полосе своей отправки. Неясность -- запрет.

        Ставится службой при денежной странности и снимается ЧЕЛОВЕКОМ:
        сама себя полоса не разблокирует. Общий рубильник и запрет продаж
        живут отдельно -- останавливать Bloom из-за замера нельзя.
        """
        try:
            if self.kill_lane_path.exists():
                try:
                    причина = self.kill_lane_path.read_text(encoding="utf-8").strip()[:300]
                except OSError:
                    причина = "(файл не читается -- всё равно запрет)"
                return True, f"полоса остановлена: {причина or 'без пояснения'}"
            return False, ""
        except Exception as exc:  # noqa: BLE001
            return True, (f"проверка рубильника полосы не удалась "
                           f"({type(exc).__name__}) -- считаем запретом")

    def set_lane_kill(self, причина: str) -> dict:
        """Остановить полосу и записать, почему. Повторный вызов не затирает
        первую причину: важна та, из-за которой остановились."""
        из_ = {"ok": False, "already": False, "why": причина}
        try:
            if self.kill_lane_path.exists():
                из_.update(ok=True, already=True)
                return из_
            self.kill_lane_path.write_text(
                f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {причина}\n",
                encoding="utf-8")
            из_["ok"] = True
        except Exception as exc:  # noqa: BLE001
            # Не смогли записать -- это само по себе странность, и молчать о
            # ней нельзя: причина уходит наружу вызывающему.
            из_["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return из_

    def kill_readable(self, path: Path | None = None) -> tuple[bool, str]:
        """Виден ли путь рубильника ТОМУ, кто спрашивает.

        Нужно отдельно от kill_active, потому что fail-closed маскирует
        поломку под нормальную работу: рубильник, который не читается,
        выглядит как включённый, и служба молча не торгует. Первый живой
        прогон детектора именно так и встал -- каталог /etc/bloom-executor
        был 750 root:root, а служба работает от bot и не могла в него даже
        войти. Проверка при деплое шла от root и ничего не заметила.

        Отсутствие файла -- это НЕ ошибка: так выглядит выключенный
        рубильник. Ошибка -- невозможность узнать, есть он или нет.
        """
        p = path or self.kill_path
        try:
            p.exists()
        except Exception as exc:  # noqa: BLE001
            return False, (f"путь рубильника {p} не проверяется "
                            f"({type(exc).__name__}): служба будет молча не торговать")
        try:
            родитель = p.parent
            if not родитель.exists():
                return False, f"каталог рубильника {родитель} не существует"
            os.listdir(родитель)
        except PermissionError:
            # Перечислять каталог не обязательно: хватает прохода в него.
            # Но если и stat самого файла не прошёл -- это уже поймано выше.
            pass
        except Exception as exc:  # noqa: BLE001
            return False, f"каталог рубильника {p.parent} недоступен ({type(exc).__name__})"
        return True, "путь рубильника доступен на чтение"

    # ----------------------------------------------------------------- дедуп

    def db(self) -> sqlite3.Connection:
        """Соединение SQLite -- СВОЁ НА КАЖДЫЙ ПОТОК.

        Цена ошибки измерена: детектор разбирает сообщения подписки в
        asyncio.to_thread, то есть в разных рабочих потоках. Одно общее
        соединение приводило к sqlite3.ProgrammingError "objects created in
        a thread can only be used in that same thread" -- и это исключение
        не просто теряло решение, а рвало подписку целиком. Ровно так 24.09
        в 01:51:17 пропал сигнал п. 3 (покупка RED): решения в журнале нет,
        покупки нет, строки в Telegram нет.

        Одно соединение на поток безопасно: база в режиме WAL, а записи
        короткие и с commit сразу.
        """
        conn = getattr(self._потоковое, "db", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=30)
            # РЕЖИМ WAL СТАВИТСЯ ОДИН РАЗ НА ФАЙЛ и в файле остаётся. Перевод
            # режима требует момента без чужой записи, и timeout соединения на
            # него не распространяется: восемь потоков, поднимающих соединения
            # разом, получали "database is locked" ровно на этой строке --
            # самопроверка так упала на облачном бегунке 25.09 в 16:46Z.
            # Поэтому режим сперва читается, ставится только при расхождении и
            # с короткими повторами. Тихо работать без WAL нельзя: он и есть
            # причина, по которой соединение на поток безопасно.
            try:
                режим = (conn.execute("PRAGMA journal_mode").fetchone() or [""])[0]
            except sqlite3.Error:
                режим = ""
            if str(режим).lower() != "wal":
                for попытка in range(6):
                    try:
                        conn.execute("PRAGMA journal_mode=WAL")
                        break
                    except sqlite3.OperationalError:
                        if попытка == 5:
                            raise
                        time.sleep(0.05 * (попытка + 1))
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("CREATE TABLE IF NOT EXISTS seen "
                          "(sig TEXT PRIMARY KEY, ts REAL, source TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS mint_last "
                          "(mint TEXT PRIMARY KEY, ts REAL, buys INTEGER)")
            conn.commit()
            self._потоковое.db = conn
            self._db = conn        # для совместимости со старым полем
        return conn

    def seen_signature(self, sig: str) -> bool:
        row = self.db().execute("SELECT ts FROM seen WHERE sig=?", (sig,)).fetchone()
        if not row:
            return False
        return (time.time() - float(row[0])) <= self.seen_ttl_s

    def mark_signature(self, sig: str, source: str = "") -> None:
        self.db().execute("INSERT OR REPLACE INTO seen(sig, ts, source) VALUES(?,?,?)",
                           (sig, time.time(), source))
        self.db().commit()

    def mint_state(self, mint: str) -> tuple[float | None, int]:
        row = self.db().execute("SELECT ts, buys FROM mint_last WHERE mint=?",
                                 (mint,)).fetchone()
        return (float(row[0]), int(row[1])) if row else (None, 0)

    def mark_mint_buy(self, mint: str, *, mode: str = MODE_LIVE) -> None:
        """Отметить покупку по минту. Для dry-run НЕ отмечаем: иначе
        придуманная покупка закроет минт для настоящей."""
        if not is_real_mode(mode):
            return
        self._mark_mint_buy(mint)

    def _mark_mint_buy(self, mint: str) -> None:
        ts, buys = self.mint_state(mint)
        self.db().execute("INSERT OR REPLACE INTO mint_last(mint, ts, buys) VALUES(?,?,?)",
                           (mint, time.time(), buys + 1))
        self.db().commit()

    # -------------------------------------------------------------- позиции

    def _ротированные_позиции(self) -> dict:
        """Позиции из ПОСЛЕДНЕГО ротированного журнала, через кэш.

        Найдено 25.09: на хосте стоит logrotate, и в 22:00Z сутки уезжают в
        positions.jsonl.1.gz. Читая только текущий файл, состояние теряло
        историю: в 22:25Z полоса показала "сделок сегодня 0" после 25 покупок
        за вечер, а её суточные пределы (50 сделок, стопы -0.3/-0.5, потолок
        расхода) считались по пустому журналу -- то есть стоп-лосс начинался
        заново каждую полночь по хосту.

        Берём ровно ОДИН, самый свежий ротированный файл: суточное окно им
        закрывается целиком, а тянуть всю историю в каждый вызов нельзя --
        positions() зовут гейты перед каждой сделкой. Разобранное держим в
        кэше по (путь, время правки, размер): ротированный файл больше не
        меняется, поэтому кэш точен, а не "почти точен".
        """
        свежий = None
        try:
            рот = sorted(self.base.glob(self.positions_path.name + ".*.gz"),
                         key=lambda п: п.stat().st_mtime, reverse=True)
            свежий = рот[0] if рот else None
        except OSError:
            return {}
        if свежий is None:
            return {}
        try:
            отметка = (str(свежий), свежий.stat().st_mtime, свежий.stat().st_size)
        except OSError:
            return {}
        ранее = _КЭШ_РОТАЦИИ.get(отметка[0])
        if ранее and ранее[0] == отметка[1] and ранее[1] == отметка[2]:
            return ранее[2]
        out: dict = {}
        try:
            import gzip  # noqa: PLC0415

            with gzip.open(свежий, "rt", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    cid = row.get("client_order_id")
                    if not cid:
                        continue
                    out.setdefault(cid, {}).update(row)
        except OSError:
            return {}
        _КЭШ_РОТАЦИИ[отметка[0]] = (отметка[1], отметка[2], out)
        return out

    def positions(self) -> dict:
        """Текущее состояние позиций -- ПЕРЕИГРОМ журнала, а не из памяти.

        Ротированный журнал читается ТОЖЕ и первым: строки текущего файла
        новее и правят поля поверх него.
        """
        out: dict = {}
        for cid, з in self._ротированные_позиции().items():
            out.setdefault(cid, {}).update(з)
        if not self.positions_path.exists():
            return out
        with self.positions_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue          # битая строка не должна рушить разбор
                cid = row.get("client_order_id")
                if not cid:
                    continue
                cur = out.setdefault(cid, {})
                cur.update(row)
        return out

    def open_positions(self, *, include_dry: bool = False,
                        lane: str | None = "any") -> list:
        """Открытые позиции. По умолчанию БЕЗ dry-run и ВСЕ полосы.

        include_dry=True нужен только отчётам, которые честно показывают
        оба раздела.

        lane задаёт, чьи позиции нужны:
          "any"            -- все (поведение как было; отчёты и сверка);
          None             -- только покупки Bloom, без полосы своей отправки;
          МЕТКА_ПОЛОСЫ     -- только позиции полосы.
        Гейт покупок Bloom обязан звать с lane=None: иначе одна позиция
        полосы на 0.01 SOL съедала бы место боевой покупки на 0.2 SOL.
        """
        out = [p for p in self.positions().values() if p.get("state") in STATES_OPEN]
        if not include_dry:
            out = [p for p in out if is_real_mode(p.get("mode"))]
        if lane == "any":
            return out
        if lane is None:
            return [p for p in out if not p.get("lane")]
        return [p for p in out if p.get("lane") == lane]

    def lane_positions(self, *, lane: str = МЕТКА_ПОЛОСЫ) -> list:
        """Все позиции полосы, включая закрытые: пределы полосы считаются по
        суткам, а не по открытым."""
        return [p for p in self.positions().values() if p.get("lane") == lane]

    def dry_positions(self) -> list:
        """Позиции dry-run отдельным списком: для отчёта и для откладывания
        при переходе в live-test."""
        return [p for p in self.positions().values()
                if p.get("state") in STATES_OPEN and not is_real_mode(p.get("mode"))]

    def new_client_order_id(self) -> str:
        return uuid.uuid4().hex

    def write_intent(self, *, client_order_id: str, mint: str, source_sig: str,
                      source_slot: int | None, sol_in: float, pool: str | None,
                      program: str | None, taxed: bool | None, tax_bps: int | None,
                      mode: str, sell_after_s: float,
                      lane: str | None = None,
                      lane_group: str | None = None,
                      lane_wallet: str | None = None) -> dict:
        """Намерение купить -- НА ДИСК ДО отправки запроса.

        Если запрос уйдёт и служба упадёт до записи ответа, позиция всё
        равно будет известна: сторож найдёт токен по цепи и продаст.
        """
        row = {SCHEMA_VERSION_KEY: SCHEMA_VERSION,
                "client_order_id": client_order_id, "state": "intent",
                "mint": mint, "source_sig": source_sig, "source_slot": source_slot,
                "sol_in": sol_in, "pool": pool, "program": program,
                "taxed": taxed, "tax_bps": tax_bps, "mode": mode,
                "sell_after_s": sell_after_s,
                "ts_intent": time.time(),
                "ts_intent_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                # КОШЕЛЁК ПОЗИЦИИ. У покупок Bloom это кошелёк исполнителя, у
                # полосы -- её СВОЙ кошелёк, если он задан. Писать всем подряд
                # адрес исполнителя значит врать в записи: разбор по цепи
                # (токены, баланс) пойдёт не по тому кошельку -- так и вышло
                # 25.09 при проверке трёх покупок полосы.
                "wallet": (lane_wallet or EXECUTOR_WALLET)}
        # Метка полосы пишется ТОЛЬКО когда она есть: у покупок Bloom поля
        # lane нет вовсе, и сравнение p.get("lane") == МЕТКА_ПОЛОСЫ у них
        # ложно без всяких оговорок.
        if lane:
            row["lane"] = lane
        # ГРУППА ИСТОЧНИКА (решение владельца 25.09): по ней считаются деньги
        # полосы -- у группы скорости свой потолок и свой стоп, и в общий итог
        # её сделки не идут. Пишется только когда есть, как и метка полосы.
        if lane_group:
            row["lane_group"] = lane_group
        append_jsonl_fsync(self.positions_path, row)
        return row

    def update_position(self, client_order_id: str, **поля) -> dict:
        row = {SCHEMA_VERSION_KEY: SCHEMA_VERSION,
                "client_order_id": client_order_id,
                "ts_update": time.time(),
                "ts_update_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                **поля}
        # СУТОЧНЫЙ СЧЁТ -- ЗДЕСЬ, А НЕ У ВЫЗЫВАЮЩЕГО. Найдено 25.09: гейт
        # дневного убытка сверял realized_sol с BLOOM_DAILY_LOSS_SOL, а писать
        # realized_sol умела только add_pnl, которую в боевом коде не звал
        # никто -- счёт всегда оставался 0.0 и стоп не срабатывал НИКОГДА.
        # Точка учёта одна и стоит там, где позиция закрывается: любой путь
        # (Bloom, полоса, продажа по остатку) проходит через update_position.
        учтено = self._учесть_закрытие(client_order_id, поля)
        if учтено:
            row.update(учтено)
        append_jsonl_fsync(self.positions_path, row)
        return row

    def _учесть_закрытие(self, cid: str, поля: dict) -> dict:
        """Записать итог закрытой позиции в суточный счёт. РОВНО ОДИН РАЗ.

        Повторный учёт был бы хуже отсутствия: стоп сработал бы по выдуманному
        убытку. Защита -- отметка в самой позиции (pnl_counted) плюс проверка
        уже учтённых по журналу, чтобы перезапуск службы не посчитал дважды.
        """
        if str(поля.get("state") or "") != STATE_CLOSED:
            return {}
        чисто = поля.get("closed_sol_net")
        if чисто is None:
            return {}
        try:
            прежние = self.positions().get(cid) or {}
        except Exception:  # noqa: BLE001
            прежние = {}
        if прежние.get("pnl_counted"):
            return {}
        # ИТОГ -- ЭТО ВОЗВРАТ МИНУС ВХОД МИНУС РАСХОД НА ОТПРАВКУ, а не
        # возврат сам по себе. Первая версия этой правки писала в счёт
        # closed_sol_net (сколько вернулось), и на живой сделке 26.09 в
        # 00:11:40Z это дало бы +0.012913 вместо честных +0.0009: вход 0.01 и
        # чаевые с приоритетом в счёт не попадали. Такой счёт не просто врёт --
        # он НИКОГДА не даст сработать стопу по убытку.
        слитое = dict(прежние)
        слитое.update(поля)
        итог, расход = self.итог_позиции(слитое)
        if итог is None:
            return {"pnl_counted": False,
                    "pnl_count_why_not": "вход или возврат неизвестны"}
        try:
            self.add_pnl(realized_sol=float(итог), spent_sol=float(расход), sells=1)
        except Exception as exc:  # noqa: BLE001
            # Счёт не должен ронять закрытие позиции: позиция закрыта по цепи,
            # и это факт. Но молчать тоже нельзя -- причина уходит в запись.
            return {"pnl_counted": False,
                    "pnl_count_why_not": f"{type(exc).__name__}"}
        return {"pnl_counted": True, "pnl_counted_sol": round(float(итог), 9),
                 "pnl_counted_spend_sol": round(float(расход), 9)}

    def итог_позиции(self, поз: dict) -> tuple:
        """Итог закрытой позиции и её расход на отправку: (итог, расход).

        Правило ровно то же, что у суточного счёта полосы
        (bloom_own_send.состояние_полосы): итог = возврат - вход, а расход на
        отправку (чаевые, приоритет, базовый тариф) вычитается ВСЕГДА, даже
        когда сама покупка не села: с кошелька эти деньги уже ушли.

        Возвращает (None, расход), если вход или возврат неизвестны: выдумывать
        ноль нельзя -- ноль читался бы как "вышли в ноль".
        """
        п = поз or {}
        расход = 0.0
        чаевые = п.get("lane_tips_total_sol")
        if чаевые:
            расход += float(чаевые)
        приоритет = п.get("lane_priority_lamports")
        if приоритет:
            расход += float(приоритет) / 1_000_000_000.0
        if п.get("lane_signature") or п.get("ts_sent") or п.get("signatures"):
            расход += 5000 / 1_000_000_000.0
        вход = п.get("sol_in")
        возврат = п.get("closed_sol_net")
        if возврат is None:
            возврат = (п.get("last_sell_outcome") or {}).get("sol_delta_net")
        # НЕСЧИТАЕМАЯ ПАРА: количество не добралось, продажа шла по остатку
        # кошелька. Разницу цен посчитать нечем -- в итог идёт только расход,
        # он-то известен точно.
        if п.get("result_uncountable"):
            return -расход, расход
        if not вход or возврат is None:
            return None, расход
        return float(возврат) - float(вход) - расход, расход

    def log_decision(self, row: dict) -> None:
        append_jsonl_fsync(self.decisions_path,
                            {SCHEMA_VERSION_KEY: SCHEMA_VERSION,
                             "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                             **row})

    # -------------------------------------------------------------- счётчики

    def counters(self) -> dict:
        if not self.counters_path.exists():
            return {}
        try:
            return json.loads(self.counters_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            # Битые счётчики -- это не повод торговать без счётчиков.
            return {"corrupt": True, "api_error_streak": 10 ** 6}

    def save_counters(self, c: dict) -> None:
        atomic_write_json(self.counters_path, c)

    def note_api_result(self, *, ok: bool, rate_limited: bool = False,
                         code: str | None = None) -> dict:
        c = self.counters()
        now = time.time()
        if rate_limited:
            # 429 -- это наш темп, а не отказ сервиса: отдельный счётчик,
            # иначе всплеск закроет торговлю автопаузой по ошибкам API.
            c["rate_limited_count"] = int(c.get("rate_limited_count", 0)) + 1
            c.setdefault("rate_limited_since", now)
            c["rate_limited_last"] = now
        else:
            c.pop("rate_limited_since", None)
            if ok:
                c["api_error_streak"] = 0
            else:
                c["api_error_streak"] = int(c.get("api_error_streak", 0)) + 1
                c["api_error_last_code"] = code
                c["api_error_last_ts"] = now
        self.save_counters(c)
        return c

    def note_sell_outcome(self, *, sold: bool, lane: str | None = None) -> dict:
        """Итог продажи в счётчики. У полосы СВОЙ счётчик.

        Слово владельца 25.09: UNSOLD полосы не останавливает Bloom. Полоса
        покупает на 0.01 SOL по своему решению, и её непроданный остаток не
        повод закрывать торговлю на 0.2 SOL. Счётчик полосы всё равно ведётся:
        он виден в докладе и в пределах полосы.
        """
        c = self.counters()
        if lane:
            ключ = f"unsold_streak_{lane}"
            c[ключ] = 0 if sold else int(c.get(ключ, 0)) + 1
        else:
            c["unsold_streak"] = 0 if sold else int(c.get("unsold_streak", 0)) + 1
        self.save_counters(c)
        return c

    # ------------------------------------------------------------ дневной PnL

    def pnl_path(self, ts: float | None = None) -> tuple[Path, bool]:
        день, беда = day_key(ts)
        return self.base / f"pnl_{день}.json", беда

    def pnl(self, ts: float | None = None) -> dict:
        path, беда = self.pnl_path(ts)
        out = {"realized_sol": 0.0, "spent_sol": 0.0, "buys": 0, "sells": 0,
                "tz_unavailable": беда}
        if path.exists():
            try:
                out.update(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                out["corrupt"] = True
        out["tz_unavailable"] = беда
        return out

    def add_pnl(self, *, realized_sol: float = 0.0, spent_sol: float = 0.0,
                 buys: int = 0, sells: int = 0, ts: float | None = None) -> dict:
        path, _ = self.pnl_path(ts)
        cur = self.pnl(ts)
        cur["realized_sol"] = round(float(cur.get("realized_sol", 0.0)) + realized_sol, 9)
        cur["spent_sol"] = round(float(cur.get("spent_sol", 0.0)) + spent_sol, 9)
        cur["buys"] = int(cur.get("buys", 0)) + buys
        cur["sells"] = int(cur.get("sells", 0)) + sells
        cur["updated_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        atomic_write_json(path, cur)
        return cur

    # --------------------------------------------------- бюджет запросов Bloom

    def note_rate_headers(self, headers: dict) -> dict:
        """Остатки лимита -- из заголовков ответа, а не из нашего счёта.

        Бюджет общий на пользователя, на все ключи и оба региона, поэтому
        свой счётчик врал бы: кто-то мог тратить из интерфейса.
        """
        low = {str(k).lower(): v for k, v in (headers or {}).items()}
        b = {}
        if self.budget_path.exists():
            try:
                b = json.loads(self.budget_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                b = {}
        for окно in ("minute", "hour", "week"):
            for вид in ("limit", "remaining"):
                k = f"x-ratelimit-{вид}-{окно}"
                if k in low:
                    try:
                        b[f"{вид}_{окно}"] = int(str(low[k]).strip())
                    except ValueError:
                        pass
        b["updated_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        atomic_write_json(self.budget_path, b)
        return b

    def week_budget_state(self) -> dict:
        b = {}
        if self.budget_path.exists():
            try:
                b = json.loads(self.budget_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                b = {}
        limit = int(b.get("limit_week") or WEEK_BUDGET)
        remaining = b.get("remaining_week")
        out = {"limit_week": limit, "remaining_week": remaining,
                "stop_at": WEEK_STOP_AT}
        if remaining is None:
            out["spent_share"] = None
            out["stop"] = False
            out["why_not"] = "остаток недельного лимита ещё не известен из заголовков"
            return out
        израсходовано = 1.0 - (int(remaining) / limit if limit else 1.0)
        out["spent_share"] = round(израсходовано, 4)
        out["stop"] = израсходовано >= WEEK_STOP_AT
        return out

    # ------------------------------------------------------------- главный гейт

    def can_open(self, *, mint: str, source_sig: str, balance_sol: float | None,
                  now: float | None = None) -> tuple[bool, str]:
        """Совместимая обёртка: (можно, причина). Код отказа -- в
        can_open_detailed."""
        можно, причина, _ = self.can_open_detailed(
            mint=mint, source_sig=source_sig, balance_sol=balance_sol, now=now)
        return можно, причина

    def can_open_detailed(self, *, mint: str, source_sig: str,
                           balance_sol: float | None,
                           now: float | None = None) -> tuple[bool, str, str]:
        """Можно ли открыть позицию. Вызывается НЕПОСРЕДСТВЕННО перед
        отправкой, а не при приёме сигнала: между этими моментами могло
        измениться всё."""
        now = now if now is not None else time.time()

        убит, почему = self.kill_active()
        if убит:
            return False, почему, КОД_РУБИЛЬНИК
        # ОТДЕЛЬНЫЙ РУБИЛЬНИК BLOOM. Этот гейт спрашивает только путь площадки:
        # полоса ходит своей дверью (можно_ещё в bloom_own_send) и файла
        # KILL_BLOOM не читает вовсе.
        убит_б, почему_б = self.kill_bloom_active()
        if убит_б:
            return False, почему_б, КОД_РУБИЛЬНИК

        c = self.counters()
        if c.get("corrupt"):
            return False, ("счётчики повреждены -- торговля запрещена, пока их не "
                            "починят: иначе автопауза не сработает"), КОД_СЧЁТЧИКИ_БИТЫ
        if int(c.get("api_error_streak", 0)) >= self.api_error_streak_max:
            return False, (f"автопауза: {c.get('api_error_streak')} ошибок API подряд "
                            f"при пороге {self.api_error_streak_max}"), КОД_ПАУЗА_API
        if int(c.get("unsold_streak", 0)) >= self.unsold_streak_max:
            return False, (f"автопауза: {c.get('unsold_streak')} непроданных позиций "
                            f"подряд при пороге {self.unsold_streak_max}"), КОД_ПАУЗА_НЕПРОДАНО
        с_каких = c.get("rate_limited_since")
        if с_каких and (now - float(с_каких)) >= self.rate_limited_pause_s:
            return False, (f"автопауза: 429 подряд дольше "
                            f"{self.rate_limited_pause_s:.0f} с"), КОД_ПАУЗА_429

        wb = self.week_budget_state()
        if wb.get("stop"):
            return False, (f"недельный бюджет запросов Bloom израсходован на "
                            f"{wb['spent_share'] * 100:.0f}% при пороге "
                            f"{WEEK_STOP_AT * 100:.0f}%"), КОД_БЮДЖЕТ_НЕДЕЛИ

        # ТОЛЬКО ПОКУПКИ BLOOM. Позиции полосы своей отправки здесь не
        # считаются ни в лимите, ни в запрете по минту: слово владельца
        # 25.09. Иначе замер на 0.01 SOL закрывал бы боевую покупку на 0.2.
        открытые = self.open_positions(lane=None)
        if self.max_open <= 0:
            return False, "лимит открытых позиций задан как 0 -- торговля запрещена", КОД_ЛИМИТ_ОТКРЫТЫХ
        if len(открытые) >= self.max_open:
            return False, (f"уже открыто {len(открытые)} позиций при лимите "
                            f"{self.max_open}"), КОД_ЛИМИТ_ОТКРЫТЫХ

        # Лимит считается ПО МИНТАМ, а не по записям: пять позиций в двух
        # токенах -- это не диверсификация, а концентрация.
        if any(p.get("mint") == mint for p in открытые):
            return False, (f"по минту {mint[:10]} уже есть открытая позиция"), КОД_ДУБЛЬ_МИНТА

        ts, buys = self.mint_state(mint)
        if buys >= self.max_buys_per_mint > 0:
            return False, (f"по минту {mint[:10]} уже {buys} покупок при лимите "
                            f"{self.max_buys_per_mint}"), КОД_ПОКУПОК_НА_МИНТ
        if ts is not None and (now - ts) < self.mint_cooldown_s:
            return False, (f"по минту {mint[:10]} кулдаун: прошло "
                            f"{now - ts:.0f} с из {self.mint_cooldown_s:.0f}"), КОД_ДУБЛЬ_МИНТА

        if self.seen_signature(source_sig):
            return False, (f"подпись источника {source_sig[:10]} уже обработана"), КОД_ПОДПИСЬ_ВИДЕЛИ

        p = self.pnl(now)
        if -float(p.get("realized_sol", 0.0)) >= self.daily_loss_sol:
            return False, (f"дневной лимит потерь: {p.get('realized_sol'):.4f} SOL "
                            f"при пределе -{self.daily_loss_sol} SOL "
                            f"(сутки по {DAY_TZ})"), КОД_ДНЕВНОЙ_УБЫТОК

        нужно = self.buy_sol + self.fee_reserve_sol
        if balance_sol is None:
            return False, "баланс кошелька неизвестен -- покупать нельзя", КОД_БАЛАНС
        if balance_sol < нужно:
            return False, (f"баланса не хватает: {balance_sol:.4f} SOL при нужных "
                            f"{нужно:.4f} (вход {self.buy_sol} + резерв на комиссии "
                            f"{self.fee_reserve_sol})"), КОД_БАЛАНС
        return True, "ок", КОД_ОК

    # ------------------------------------------------------------------ отчёт

    def report(self) -> dict:
        убит, почему = self.kill_active()
        открытые = self.open_positions()
        p = self.pnl()
        return {
            SCHEMA_VERSION_KEY: SCHEMA_VERSION,
            "state_dir": str(self.base),
            "kill_switch": {"path": str(self.kill_path), "active": убит, "why_not": почему,
                             "telegram_path": str(self.kill_tg_path),
                             "telegram_active": self.kill_tg_path.exists(),
                             "sell_stopped": self.sell_kill_active()[0],
                             "bloom_path": str(self.kill_bloom_path),
                             "bloom_active": self.kill_bloom_active()[0]},
            "limits": {"max_open": self.max_open,
                        "daily_loss_sol": self.daily_loss_sol,
                        "buy_sol": self.buy_sol,
                        "fee_reserve_sol": self.fee_reserve_sol,
                        "api_error_streak_max": self.api_error_streak_max,
                        "unsold_streak_max": self.unsold_streak_max,
                        "rate_limited_pause_s": self.rate_limited_pause_s,
                        "mint_cooldown_s": self.mint_cooldown_s,
                        "max_buys_per_mint": self.max_buys_per_mint},
            "open_positions": len(открытые),
            "open_mints": sorted({p_.get("mint") for p_ in открытые if p_.get("mint")}),
            "counters": self.counters(),
            "day_pnl": p,
            "bloom_request_budget": self.week_budget_state(),
        }


def self_test() -> None:
    import tempfile  # noqa: PLC0415
    checks = []

    def chk(n, ok, got=""):
        checks.append((n, bool(ok), got))

    base = Path(tempfile.mkdtemp())
    kill = base / "KILL"
    st = ExecState(base=base / "state", kill=kill)

    # --- рубильник
    chk("рубильника нет -- торговля разрешена", st.kill_active()[0] is False)
    kill.write_text("останов по просьбе владельца", encoding="utf-8")
    убит, почему = st.kill_active()
    chk("файл рубильника -- запрет", убит is True)
    chk("и причина из файла видна", "владельц" in почему, почему)
    kill.unlink()
    chk("файл убран -- снова разрешено", st.kill_active()[0] is False)

    # Рубильник из Telegram -- второй путь, и он запрещает так же строго.
    # Первый лежит в /etc и принадлежит root: служба от bot создать его не
    # может, поэтому команда владельца без второго пути не сработала бы.
    st.kill_tg_path.write_text("останов по команде /kill из Telegram", encoding="utf-8")
    убит_тг, почему_тг = st.kill_active()
    chk("рубильник из Telegram запрещает торговлю",
        убит_тг is True and "Telegram" in почему_тг, (убит_тг, почему_тг))
    chk("и он виден в отчёте состояния отдельно",
        st.report()["kill_switch"]["telegram_active"] is True, st.report()["kill_switch"])
    st.kill_tg_path.unlink()
    chk("снят -- снова разрешено", st.kill_active()[0] is False)

    # Запрет ТОЛЬКО продажи: покупки он не трогает.
    chk("без файла продажи разрешены", st.sell_kill_active()[0] is False)
    st.kill_sell_path.write_text("стоп продажам", encoding="utf-8")
    стоп_прод, почему_прод = st.sell_kill_active()
    chk("запрет продаж включается своим файлом",
        стоп_прод is True and "продажи остановлены" in почему_прод, почему_прод)
    chk("и покупкам он не мешает", st.kill_active()[0] is False)
    chk("и он виден в отчёте состояния",
        st.report()["kill_switch"]["sell_stopped"] is True, st.report()["kill_switch"])
    st.kill_sell_path.unlink()

    # --- РУБИЛЬНИК ТОЛЬКО BLOOM (слово владельца 25.09: два отдельных файла).
    chk("без файла Bloom покупки площадки разрешены",
        st.kill_bloom_active()[0] is False)
    st.kill_bloom_path.write_text("стоп только Bloom", encoding="utf-8")
    убит_б, почему_б = st.kill_bloom_active()
    chk("рубильник Bloom включается своим файлом",
        убит_б is True and "рубильник Bloom" in почему_б, почему_б)
    ок_б, почему_гейт, код_б = st.can_open_detailed(
        balance_sol=10.0, mint="MB", source_sig="SB")
    chk("и гейт покупок площадки его слушает",
        ок_б is False and код_б == КОД_РУБИЛЬНИК, (ок_б, код_б, почему_гейт))
    chk("а ОБЩИЙ рубильник при этом не включён -- полоса не остановлена",
        st.kill_active()[0] is False, st.kill_active())
    chk("оба рубильника видны в отчёте",
        st.report()["kill_switch"]["bloom_active"] is True
        and st.report()["kill_switch"]["active"] is False,
        st.report()["kill_switch"])
    st.kill_bloom_path.unlink()
    chk("снят -- покупки площадки снова разрешены",
        st.kill_bloom_active()[0] is False)

    # --- РОТАЦИЯ ЖУРНАЛА НЕ ОБНУЛЯЕТ СОСТОЯНИЕ (найдено 25.09 в 22:25Z).
    import gzip as _гз  # noqa: PLC0415

    рот = ExecState(base=base / "rotate", kill=base / "rotate" / "НЕТ")
    with _гз.open(рот.positions_path.parent / (рот.positions_path.name + ".1.gz"),
                   "wt", encoding="utf-8") as ф:
        ф.write(json.dumps({"client_order_id": "вчера", "state": STATE_CLOSED,
                             "mint": "MV", "sol_in": 0.01,
                             "lane": "own_send"}) + "\n")
    рот.write_intent(client_order_id="сегодня", mint="MS", source_sig="SS",
                      source_slot=2, sol_in=0.01, pool=None, program=None,
                      taxed=None, tax_bps=None, mode=MODE_LIVE, sell_after_s=28.8)
    поз_р = рот.positions()
    chk("после ротации в состоянии видны и вчерашние, и сегодняшние позиции",
        "вчера" in поз_р and "сегодня" in поз_р, sorted(поз_р))
    chk("поля ротированной позиции разобраны, а не потеряны",
        (поз_р.get("вчера") or {}).get("mint") == "MV", поз_р.get("вчера"))

    # --- СУТОЧНЫЙ СЧЁТ ИТОГА. Найдено 25.09: add_pnl не звал никто, и стоп по
    # дневному убытку не срабатывал никогда. Проверяем денежный путь целиком:
    # закрытая с убытком позиция -> счёт вырос -> гейт закрылся; и что повтор
    # той же записи счёт НЕ удваивает.
    сч = ExecState(base=base / "pnl_gate", kill=base / "pnl_gate" / "НЕТ")
    сч.daily_loss_sol = 0.01
    сч.write_intent(client_order_id="p1", mint="M1", source_sig="S1",
                    source_slot=1, sol_in=0.01, pool=None, program=None,
                    taxed=None, tax_bps=None, mode=MODE_LIVE, sell_after_s=28.8)
    chk("до закрытия суточный итог нулевой", сч.pnl()["realized_sol"] == 0.0)
    ок_до, _, _ = сч.can_open_detailed(balance_sol=10.0, mint="M9", source_sig="S9")
    chk("и гейт покупок открыт", ок_до is True)
    стр = сч.update_position("p1", state=STATE_CLOSED, closed_sol_net=0.0,
                              lane_tips_total_sol=0.005,
                              lane_priority_lamports=1_000_000,
                              lane_signature="ПОДПИСЬ",
                              closed_reason="самопроверка: продажа в минус")
    # Вход 0.01, вернулось 0.0, расход 0.005 чаевых + 0.001 приоритета +
    # 0.000005 тарифа: итог -0.016005, а НЕ "вернулось 0".
    chk("учтён итог, а не возврат: возврат минус вход минус расход",
        стр.get("pnl_counted") is True
        and abs(сч.pnl()["realized_sol"] + 0.016005) < 1e-9,
        (стр.get("pnl_counted_sol"), сч.pnl()))
    chk("расход записан отдельной строкой суточного счёта",
        abs(сч.pnl()["spent_sol"] - 0.006005) < 1e-9, сч.pnl())
    ок_после, почему_п, код_п = сч.can_open_detailed(
        balance_sol=10.0, mint="M9", source_sig="S9")
    chk("после убытка гейт закрыт дневным лимитом",
        ок_после is False and код_п == КОД_ДНЕВНОЙ_УБЫТОК, (код_п, почему_п))
    стр2 = сч.update_position("p1", state=STATE_CLOSED, closed_sol_net=0.0,
                               closed_reason="повтор той же записи")
    chk("повторная запись того же закрытия счёт НЕ удваивает",
        стр2.get("pnl_counted") is not True
        and abs(сч.pnl()["realized_sol"] + 0.016005) < 1e-9, сч.pnl())
    сч.update_position("p2", state=STATE_CLOSED,
                        closed_reason="закрыта без числа итога")
    chk("закрытие без числа итога счёт не трогает",
        abs(сч.pnl()["realized_sol"] + 0.016005) < 1e-9, сч.pnl())
    # ПРИБЫЛЬНАЯ ЖИВАЯ СДЕЛКА 26.09 00:11:40Z: вход 0.01, вернулось 0.012913,
    # чаевые 0.001, приоритет 0.001, тариф 0.000005 -> итог +0.000908, а не
    # +0.012913. Проверяем ровно эти числа: на них видно разницу между
    # "вернулось" и "заработали".
    сч2 = ExecState(base=base / "pnl_real", kill=base / "pnl_real" / "НЕТ")
    сч2.write_intent(client_order_id="ж1", mint="MZ", source_sig="SZ",
                     source_slot=3, sol_in=0.01, pool=None, program=None,
                     taxed=None, tax_bps=None, mode=MODE_LIVE, sell_after_s=28.8)
    стр_ж = сч2.update_position("ж1", state=STATE_CLOSED,
                                 closed_sol_net=0.012912956,
                                 lane_tips_total_sol=0.001,
                                 lane_priority_lamports=1_000_000,
                                 lane_signature="ПОДПИСЬ_Ж",
                                 closed_reason="продажа подтверждена по цепи")
    chk("на живых числах итог +0.000908, а не возврат +0.012913",
        abs((стр_ж.get("pnl_counted_sol") or 0) - 0.000907956) < 1e-9,
        стр_ж.get("pnl_counted_sol"))
    # НЕСЧИТАЕМАЯ ПАРА: разницу цен посчитать нечем, но чаевые ушли.
    сч2.write_intent(client_order_id="н1", mint="MN", source_sig="SN",
                     source_slot=4, sol_in=0.01, pool=None, program=None,
                     taxed=None, tax_bps=None, mode=MODE_LIVE, sell_after_s=28.8)
    стр_н = сч2.update_position("н1", state=STATE_CLOSED, closed_sol_net=0.0,
                                 result_uncountable=True,
                                 lane_tips_total_sol=0.001,
                                 lane_priority_lamports=1_000_000,
                                 lane_signature="ПОДПИСЬ_Н",
                                 closed_reason="продавать нечего")
    chk("у несчитаемой пары в счёт идёт только расход",
        abs((стр_н.get("pnl_counted_sol") or 0) + 0.002005) < 1e-9,
        стр_н.get("pnl_counted_sol"))

    class БросаетПриПроверке:
        """Путь, проверка которого падает: права, битый монтаж, что угодно."""

        def exists(self):
            raise OSError("нет прав на чтение файла рубильника")

    сл = ExecState(base=base / "state2", kill=kill)
    сл.kill_path = БросаетПриПроверке()
    убит2, почему2 = сл.kill_active()
    chk("ошибка проверки рубильника -- ЗАПРЕТ, а не разрешение", убит2 is True)
    chk("и сказано, что неясность трактуется как запрет",
        "неясность" in почему2, почему2)

    # --- атомарная запись
    p = base / "x.json"
    atomic_write_json(p, {"а": 1})
    chk("атомарная запись прочитывается", json.loads(p.read_text())["а"] == 1)
    chk("временного файла не осталось", not p.with_suffix(".json.tmp").exists())

    # --- журнал позиций переживает перезапуск
    cid = st.new_client_order_id()
    st.write_intent(client_order_id=cid, mint="MINT1", source_sig="SIG1",
                     source_slot=100, sol_in=0.2, pool="POOL", program="Raydium",
                     taxed=False, tax_bps=None, mode=MODE_LIVE_TEST, sell_after_s=28.8)
    chk("намерение записано и видно как открытая позиция",
        len(st.open_positions()) == 1)
    st2 = ExecState(base=st.base, kill=kill)
    chk("другой экземпляр видит ту же позицию (значит она на диске)",
        len(st2.open_positions()) == 1)
    st.update_position(cid, state="bought", buy_signature="OURSIG")
    chk("состояние обновилось переигром журнала",
        st.positions()[cid]["state"] == "bought")
    chk("и поля прежней записи не потеряны",
        st.positions()[cid]["mint"] == "MINT1")
    st.update_position(cid, state=STATE_CLOSED)
    chk("закрытая позиция из открытых уходит", st.open_positions() == [])

    # битая строка в журнале не должна ронять разбор
    with st.positions_path.open("a", encoding="utf-8") as f:
        f.write("{это не json\n")
    chk("битая строка журнала не роняет разбор", isinstance(st.positions(), dict))

    # --- дедуп
    chk("новая подпись не считается виденной", st.seen_signature("SIGX") is False)
    st.mark_signature("SIGX", "src")
    chk("отмеченная подпись считается виденной", st.seen_signature("SIGX") is True)
    st3 = ExecState(base=st.base, kill=kill)
    chk("дедуп переживает перезапуск (лежит в sqlite)",
        st3.seen_signature("SIGX") is True)

    # --- лимиты
    ok, почему3 = st.can_open(mint="M2", source_sig="S2", balance_sol=1.0)
    chk("при чистом состоянии открывать можно", ok is True, почему3)
    ok, почему4 = st.can_open(mint="M2", source_sig="S2", balance_sol=0.05)
    chk("баланса не хватает -- нельзя", ok is False)
    chk("и в причине названы обе цифры", "0.05" in почему4 and "резерв" in почему4, почему4)
    ok, _ = st.can_open(mint="M2", source_sig="S2", balance_sol=None)
    chk("неизвестный баланс -- нельзя", ok is False)
    ok, почему5 = st.can_open(mint="M2", source_sig="SIGX", balance_sol=1.0)
    chk("виденная подпись -- нельзя", ok is False and "уже обработана" in почему5)

    kill.write_text("стоп", encoding="utf-8")
    ok, почему6 = st.can_open(mint="M3", source_sig="S3", balance_sol=1.0)
    chk("рубильник перебивает всё остальное", ok is False and "рубильник" in почему6)
    kill.unlink()

    # лимит открытых позиций
    st4 = ExecState(base=base / "state4", kill=kill)
    for i in range(st4.max_open):
        st4.write_intent(client_order_id=f"c{i}", mint=f"M{i}", source_sig=f"S{i}",
                          source_slot=i, sol_in=0.2, pool=None, program=None,
                          taxed=None, tax_bps=None, mode=MODE_LIVE_TEST, sell_after_s=29)
    ok, почему7 = st4.can_open(mint="MNEW", source_sig="SNEW", balance_sol=1.0)
    chk("потолок открытых позиций держится", ok is False and "при лимите" in почему7,
        почему7)

    # лимит по минту: открытая позиция по тому же минту
    st5 = ExecState(base=base / "state5", kill=kill)
    st5.write_intent(client_order_id="a", mint="SAME", source_sig="SA", source_slot=1,
                      sol_in=0.2, pool=None, program=None, taxed=None, tax_bps=None,
                      mode=MODE_LIVE_TEST, sell_after_s=29)
    ok, почему8 = st5.can_open(mint="SAME", source_sig="SB", balance_sol=1.0)
    chk("вторая позиция по тому же минту запрещена",
        ok is False and "уже есть открытая позиция" in почему8, почему8)

    # ---- ПОЛОСА СВОЕЙ ОТПРАВКИ ОТДЕЛЕНА ОТ BLOOM (владелец 25.09) ----
    # Цена ошибки прямая: позиция полосы на 0.01 SOL не имеет права ни занять
    # место боевой покупки на 0.2 SOL, ни закрыть ей минт, ни остановить
    # торговлю своим UNSOLD.
    stп = ExecState(base=base / "state_lane", kill=kill)
    for i in range(stп.max_open):
        stп.write_intent(client_order_id=f"l{i}", mint=f"LM{i}", source_sig=f"LS{i}",
                          source_slot=i, sol_in=0.01, pool=None, program=None,
                          taxed=None, tax_bps=None, mode=MODE_LIVE,
                          sell_after_s=28.8, lane=МЕТКА_ПОЛОСЫ)
    ok_п, почему_п = stп.can_open(mint="MNEW", source_sig="SNEW", balance_sol=1.0)
    chk("позиции полосы НЕ занимают лимит открытых у Bloom", ok_п is True,
        почему_п)
    chk("метка полосы записана в позицию",
        stп.positions()["l0"].get("lane") == МЕТКА_ПОЛОСЫ,
        stп.positions()["l0"].get("lane"))
    chk("у покупки Bloom поля lane нет вовсе",
        "lane" not in st5.positions()["a"], st5.positions()["a"].get("lane"))
    ok_м, почему_м = stп.can_open(mint="LM0", source_sig="SNEW2", balance_sol=1.0)
    chk("минт, купленный полосой, для Bloom НЕ заблокирован", ok_м is True, почему_м)
    chk("фильтр открытых по полосе разделяет их",
        len(stп.open_positions(lane=None)) == 0
        and len(stп.open_positions(lane=МЕТКА_ПОЛОСЫ)) == stп.max_open
        and len(stп.open_positions()) == stп.max_open,
        (len(stп.open_positions(lane=None)),
         len(stп.open_positions(lane=МЕТКА_ПОЛОСЫ))))
    # UNSOLD полосы не поднимает счётчик Bloom, но свой -- поднимает.
    for _ in range(stп.unsold_streak_max):
        stп.note_sell_outcome(sold=False, lane=МЕТКА_ПОЛОСЫ)
    ok_у, почему_у = stп.can_open(mint="MNEW3", source_sig="SNEW3", balance_sol=1.0)
    chk("три UNSOLD полосы подряд НЕ ставят Bloom на автопаузу", ok_у is True,
        почему_у)
    chk("а свой счётчик полосы вырос",
        int(stп.counters().get(f"unsold_streak_{МЕТКА_ПОЛОСЫ}", 0))
        == stп.unsold_streak_max, stп.counters())
    stп.note_sell_outcome(sold=False)
    chk("UNSOLD Bloom по-прежнему считается своим счётчиком",
        int(stп.counters().get("unsold_streak", 0)) == 1, stп.counters())
    chk("успешная продажа полосы обнуляет только её счётчик",
        (stп.note_sell_outcome(sold=True, lane=МЕТКА_ПОЛОСЫ)
         .get(f"unsold_streak_{МЕТКА_ПОЛОСЫ}") == 0)
        and int(stп.counters().get("unsold_streak", 0)) == 1, stп.counters())
    chk("позиции полосы видны отдельным списком",
        len(stп.lane_positions()) == stп.max_open, len(stп.lane_positions()))
    # РУБИЛЬНИК ТОЛЬКО ПОЛОСЫ. Он обязан останавливать полосу и НЕ трогать
    # Bloom: цена ошибки в обе стороны -- либо замер продолжает тратить деньги
    # при странности, либо из-за замера встаёт боевая торговля.
    chk("полоса не остановлена, пока файла нет", stп.lane_kill_active()[0] is False,
        stп.lane_kill_active())
    пост = stп.set_lane_kill("сумма выше предела: проверка")
    chk("рубильник полосы поставлен и причина записана",
        пост.get("ok") and stп.lane_kill_active()[0] is True
        and "сумма выше предела" in stп.lane_kill_active()[1],
        (пост, stп.lane_kill_active()))
    chk("общий рубильник и запрет продаж при этом не тронуты",
        stп.kill_active()[0] is False and stп.sell_kill_active()[0] is False,
        (stп.kill_active(), stп.sell_kill_active()))
    ok_пк, почему_пк = stп.can_open(mint="MNEW4", source_sig="SNEW4", balance_sol=1.0)
    chk("и торговля Bloom из-за остановленной полосы не встала", ok_пк is True,
        почему_пк)
    второй = stп.set_lane_kill("другая причина")
    chk("повторная остановка не затирает первую причину",
        второй.get("already") is True
        and "сумма выше предела" in stп.lane_kill_active()[1],
        stп.lane_kill_active())

    # покупок на минт и кулдаун
    st6 = ExecState(base=base / "state6", kill=kill)
    st6.mark_mint_buy("MM")
    st6.mark_mint_buy("MM")
    ok, почему9 = st6.can_open(mint="MM", source_sig="S9", balance_sol=1.0)
    chk("две покупки по минту -- третья запрещена",
        ok is False and "при лимите" in почему9, почему9)

    st7 = ExecState(base=base / "state7", kill=kill)
    st7.mark_mint_buy("CD")
    ok, почему10 = st7.can_open(mint="CD", source_sig="S10", balance_sol=1.0)
    chk("кулдаун по минту держится", ok is False and "кулдаун" in почему10, почему10)

    # --- дневной лимит потерь
    st8 = ExecState(base=base / "state8", kill=kill)
    st8.add_pnl(realized_sol=-0.5)
    ok, _ = st8.can_open(mint="MX", source_sig="SX", balance_sol=1.0)
    chk("потеря 0.5 SOL ещё не закрывает торговлю", ok is True)
    st8.add_pnl(realized_sol=-0.5)
    ok, почему11 = st8.can_open(mint="MY", source_sig="SY", balance_sol=1.0)
    chk("ровно 1 SOL потерь -- торговля закрыта",
        ok is False and "дневной лимит потерь" in почему11, почему11)
    st9 = ExecState(base=st8.base, kill=kill)
    ok, _ = st9.can_open(mint="MZ", source_sig="SZ", balance_sol=1.0)
    chk("после 'перезапуска' лимит НЕ обнулился (лежит в файле)", ok is False)

    # --- счётчики и автопаузы
    st10 = ExecState(base=base / "state10", kill=kill)
    for _ in range(st10.api_error_streak_max):
        st10.note_api_result(ok=False, code="INTERNAL_ERROR")
    ok, почему12 = st10.can_open(mint="M", source_sig="S", balance_sol=1.0)
    chk("пять ошибок API подряд -- автопауза",
        ok is False and "ошибок API подряд" in почему12, почему12)
    st10.note_api_result(ok=True)
    ok, _ = st10.can_open(mint="M", source_sig="S", balance_sol=1.0)
    chk("успешный вызов обнуляет серию ошибок", ok is True)

    st11 = ExecState(base=base / "state11", kill=kill)
    for _ in range(20):
        st11.note_api_result(ok=False, rate_limited=True)
    ok, _ = st11.can_open(mint="M", source_sig="S", balance_sol=1.0)
    chk("429 сам по себе НЕ закрывает торговлю по счётчику ошибок API", ok is True)
    c = st11.counters()
    c["rate_limited_since"] = time.time() - (st11.rate_limited_pause_s + 1)
    st11.save_counters(c)
    ok, почему13 = st11.can_open(mint="M", source_sig="S", balance_sol=1.0)
    chk("429 дольше пяти минут подряд -- автопауза",
        ok is False and "429" in почему13, почему13)
    st11.note_api_result(ok=True)
    ok, _ = st11.can_open(mint="M", source_sig="S", balance_sol=1.0)
    chk("успешный ответ снимает серию 429", ok is True)

    st12 = ExecState(base=base / "state12", kill=kill)
    for _ in range(st12.unsold_streak_max):
        st12.note_sell_outcome(sold=False)
    ok, почему14 = st12.can_open(mint="M", source_sig="S", balance_sol=1.0)
    chk("три непроданных подряд -- автопауза",
        ok is False and "непроданных" in почему14, почему14)
    st12.note_sell_outcome(sold=True)
    ok, _ = st12.can_open(mint="M", source_sig="S", balance_sol=1.0)
    chk("удачная продажа снимает серию", ok is True)

    st13 = ExecState(base=base / "state13", kill=kill)
    st13.counters_path.write_text("{битый", encoding="utf-8")
    ok, почему15 = st13.can_open(mint="M", source_sig="S", balance_sol=1.0)
    chk("битые счётчики -- запрет, а не торговля без счётчиков",
        ok is False and "повреждены" in почему15, почему15)

    # --- бюджет запросов
    st14 = ExecState(base=base / "state14", kill=kill)
    chk("пока заголовков не было -- стопа нет, но и доли нет",
        st14.week_budget_state()["stop"] is False
        and st14.week_budget_state()["spent_share"] is None)
    st14.note_rate_headers({"X-RateLimit-Limit-Week": "10000",
                             "X-RateLimit-Remaining-Week": "2500",
                             "x-ratelimit-remaining-minute": "59"})
    wb = st14.week_budget_state()
    chk("остаток недели прочитан из заголовка", wb["remaining_week"] == 2500, str(wb))
    chk("израсходовано 75% -- стоп при пороге 80% ещё нет", wb["stop"] is False)
    st14.note_rate_headers({"X-RateLimit-Remaining-Week": "1500"})
    chk("израсходовано 85% -- стоп", st14.week_budget_state()["stop"] is True)
    ok, почему16 = st14.can_open(mint="M", source_sig="S", balance_sol=1.0)
    chk("и открывать нельзя", ok is False and "недельный бюджет" in почему16, почему16)

    # --- сутки по Мадриду
    день, беда = day_key(1790178120)
    chk("дата суток получена", len(день) == 10, день)
    chk("недоступность часового пояса не скрывается", isinstance(беда, bool))

    # --- машиночитаемые коды отказов
    with tempfile.TemporaryDirectory() as d:
        st = ExecState(base=Path(d) / "s", kill=Path(d) / "k")
        можно, _, код = st.can_open_detailed(mint="M1", source_sig="s1", balance_sol=3.0)
        chk("разрешение отдаёт код OK", можно and код == КОД_ОК, код)
        st.write_intent(client_order_id="c", mint="M1", source_sig="s0", source_slot=1,
                        sol_in=0.2, pool=None, program=None, taxed=None, tax_bps=None,
                        mode=MODE_LIVE_TEST, sell_after_s=28.8)
        можно, _, код = st.can_open_detailed(mint="M1", source_sig="s1", balance_sol=3.0)
        chk("дубль минта даёт SKIPPED_DUP_MINT",
            (not можно) and код == КОД_ДУБЛЬ_МИНТА, код)
        chk("код дубля -- ровно та метка, что просил владелец",
            КОД_ДУБЛЬ_МИНТА == "SKIPPED_DUP_MINT", КОД_ДУБЛЬ_МИНТА)
        можно, _, код = st.can_open_detailed(mint="M2", source_sig="s2", balance_sol=None)
        chk("неизвестный баланс даёт код баланса",
            (not можно) and код == КОД_БАЛАНС, код)
        st.kill_path.write_text("стоп", encoding="utf-8")
        можно, причина, код = st.can_open_detailed(mint="M3", source_sig="s3", balance_sol=3.0)
        chk("рубильник даёт свой код", (not можно) and код == КОД_РУБИЛЬНИК, код)
        can2 = st.can_open(mint="M3", source_sig="s3", balance_sol=3.0)
        chk("старая обёртка can_open возвращает пару", len(can2) == 2 and can2[0] is False, can2)
        chk("текст причины у обёртки тот же", can2[1] == причина)

    # --- читаемость рубильника отличается от его состояния
    with tempfile.TemporaryDirectory() as d:
        st = ExecState(base=Path(d) / "s", kill=Path(d) / "k" / "KILL")
        (Path(d) / "k").mkdir()
        ок, поч = st.kill_readable()
        chk("отсутствующий файл рубильника -- это доступно, а не ошибка", ок, поч)
        убит, _ = st.kill_active()
        chk("и при этом рубильник выключен", not убит)
        st.kill_path.write_text("стоп", encoding="utf-8")
        ок2, _ = st.kill_readable()
        chk("включённый рубильник тоже читается", ок2)
        chk("и он включён", st.kill_active()[0])
        # каталог, которого нет, -- это поломка пути, а не выключенный рубильник
        st2 = ExecState(base=Path(d) / "s2", kill=Path(d) / "нет_такого" / "KILL")
        ок3, поч3 = st2.kill_readable()
        chk("несуществующий каталог рубильника -- поломка", not ок3, поч3)
        chk("и она названа словами", "не существует" in поч3, поч3)

    # --- кошельки
    chk("адрес исполнителя -- константа нужного вида",
        EXECUTOR_WALLET.startswith("4s87RRC2") and len(EXECUTOR_WALLET) == 44)
    chk("чужой кошелёк W1 известен и не равен нашему",
        FOREIGN_WALLET_W1 != EXECUTOR_WALLET)

    отчёт = st.report()
    chk("отчёт собирается", "limits" in отчёт and "kill_switch" in отчёт)

    # --- ИЗОЛЯЦИЯ dry-run: придуманные позиции не должны мешать настоящим
    with tempfile.TemporaryDirectory() as d:
        st = ExecState(base=Path(d) / "s", kill=Path(d) / "k")
        # набиваем ПОЛНЫЙ потолок позициями dry-run
        for i in range(st.max_open + 2):
            st.write_intent(client_order_id=f"d{i}", mint=f"DM{i}",
                            source_sig=f"DS{i}", source_slot=i, sol_in=0.2,
                            pool=None, program=None, taxed=None, tax_bps=None,
                            mode=MODE_DRY, sell_after_s=28.8)
        chk("dry-run позиции не считаются открытыми",
            st.open_positions() == [], st.open_positions())
        chk("но в журнале они есть и видны отдельно",
            len(st.dry_positions()) == st.max_open + 2, len(st.dry_positions()))
        chk("include_dry показывает оба раздела",
            len(st.open_positions(include_dry=True)) == st.max_open + 2)
        ok, почему = st.can_open(mint="REAL", source_sig="RS", balance_sol=5.0)
        chk("гейт открыт: придуманные позиции его не закрыли", ok, почему)
        ok2, _ = st.can_open(mint="DM0", source_sig="RS2", balance_sol=5.0)
        chk("минт из dry-run не блокирует настоящую покупку", ok2)

        st.mark_mint_buy("DM0", mode=MODE_DRY)
        _, покупок = st.mint_state("DM0")
        chk("покупка в dry-run не занимает лимит покупок по минту",
            покупок == 0, покупок)
        st.mark_mint_buy("DM0", mode=MODE_LIVE_TEST)
        _, покупок2 = st.mint_state("DM0")
        chk("а настоящая -- занимает", покупок2 == 1, покупок2)

        # live-test считается настоящей
        st.write_intent(client_order_id="lt", mint="LTM", source_sig="LTS",
                        source_slot=1, sol_in=0.01, pool=None, program=None,
                        taxed=None, tax_bps=None, mode=MODE_LIVE_TEST,
                        sell_after_s=28.8)
        chk("live-test позиция считается открытой",
            [p["mint"] for p in st.open_positions()] == ["LTM"],
            [p.get("mint") for p in st.open_positions()])

    chk("неизвестный режим трактуется как настоящий", is_real_mode("что-то новое"))
    chk("и отсутствие режима тоже", is_real_mode(None))
    chk("только dry-run считается ненастоящим", not is_real_mode(MODE_DRY))

    # --- версия формата: читатель не должен гадать, какие перед ним ключи
    chk("версия формата -- 2", SCHEMA_VERSION == 2, SCHEMA_VERSION)
    chk("в отчёте есть версия формата",
        отчёт.get(SCHEMA_VERSION_KEY) == SCHEMA_VERSION, отчёт.get(SCHEMA_VERSION_KEY))
    with tempfile.TemporaryDirectory() as d:
        st = ExecState(base=Path(d) / "s", kill=Path(d) / "k")
        st.write_intent(client_order_id="c", mint="M", source_sig="s", source_slot=1,
                        sol_in=0.2, pool=None, program=None, taxed=None, tax_bps=None,
                        mode=MODE_LIVE_TEST, sell_after_s=28.8)
        st.update_position("c", state="bought")
        st.log_decision({"code": "BUY"})
        строки = [json.loads(x) for x in
                  st.positions_path.read_text(encoding="utf-8").strip().split("\n")]
        chk("версия в каждой записи позиций",
            all(r.get(SCHEMA_VERSION_KEY) == SCHEMA_VERSION for r in строки), строки)
        реш = [json.loads(x) for x in
               st.decisions_path.read_text(encoding="utf-8").strip().split("\n")]
        chk("версия в записи решения",
            реш[0].get(SCHEMA_VERSION_KEY) == SCHEMA_VERSION, реш[0])
        # --- дедуп из ДВУХ потоков: именно это рвало подписку детектора
    import concurrent.futures  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as d:
        stп = ExecState(base=Path(d) / "s", kill=Path(d) / "k")

        def в_потоке(i):
            stп.mark_signature(f"SIG{i}", "поток")
            return stп.seen_signature(f"SIG{i}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as пул:
            итоги = list(пул.map(в_потоке, range(8)))
        chk("дедуп работает из нескольких потоков, а не падает ProgrammingError",
            all(итоги) and len(итоги) == 8, итоги)
        chk("и записи видны из главного потока",
            all(stп.seen_signature(f"SIG{i}") for i in range(8)))

        def минт_в_потоке(i):
            stп.mark_mint_buy(f"MINT{i}", mode=MODE_LIVE)
            return stп.mint_state(f"MINT{i}")[1]

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as пул:
            минты = list(пул.map(минт_в_потоке, range(6)))
        chk("и отметка минтов тоже", all(x == 1 for x in минты), минты)

    chk("кириллических ключей в записях нет",
            all(k.isascii() for r in строки + реш for k in r),
            [k for r in строки + реш for k in r if not k.isascii()])

    bad = 0
    for n, ok_, got in checks:
        print(f"  [{'ok  ' if ok_ else 'СБОЙ'}] {n}" + (f"  -> {got}" if got and not ok_ else ""))
        bad += (not ok_)
    print(f"самопроверка состояния исполнителя: {len(checks) - bad}/{len(checks)} пройдено")
    if bad:
        raise SystemExit(f"самопроверка не пройдена: {bad} из {len(checks)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        self_test()
    elif a.report:
        print(json.dumps(ExecState().report(), ensure_ascii=False, indent=2))
    else:
        ap.print_help()
        sys.exit(1)
