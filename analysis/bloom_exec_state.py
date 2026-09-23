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

    # ------------------------------------------------------------- рубильник

    def kill_active(self) -> tuple[bool, str]:
        """Рубильник. ЛЮБАЯ неясность -- запрет.

        Файл есть -> запрет. Файла нет -> разрешено. Ошибка проверки
        (права, битый путь, что угодно) -> ЗАПРЕТ. Специально так: цена
        ложного запрета -- пропущенная сделка, цена ложного разрешения --
        неограниченный убыток.
        """
        try:
            if self.kill_path.exists():
                try:
                    причина = self.kill_path.read_text(encoding="utf-8").strip()[:200]
                except OSError:
                    причина = "(файл рубильника не читается -- всё равно запрет)"
                return True, f"рубильник включён: {причина or 'без пояснения'}"
            return False, ""
        except Exception as exc:  # noqa: BLE001
            return True, (f"проверка рубильника не удалась ({type(exc).__name__}) -- "
                           "торговля запрещена, потому что неясность трактуется как запрет")

    # ----------------------------------------------------------------- дедуп

    def db(self) -> sqlite3.Connection:
        if self._db is None:
            self._db = sqlite3.connect(str(self.db_path), timeout=30)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("CREATE TABLE IF NOT EXISTS seen "
                              "(sig TEXT PRIMARY KEY, ts REAL, source TEXT)")
            self._db.execute("CREATE TABLE IF NOT EXISTS mint_last "
                              "(mint TEXT PRIMARY KEY, ts REAL, buys INTEGER)")
            self._db.commit()
        return self._db

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

    def mark_mint_buy(self, mint: str) -> None:
        ts, buys = self.mint_state(mint)
        self.db().execute("INSERT OR REPLACE INTO mint_last(mint, ts, buys) VALUES(?,?,?)",
                           (mint, time.time(), buys + 1))
        self.db().commit()

    # -------------------------------------------------------------- позиции

    def positions(self) -> dict:
        """Текущее состояние позиций -- ПЕРЕИГРОМ журнала, а не из памяти."""
        out: dict = {}
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

    def open_positions(self) -> list:
        return [p for p in self.positions().values() if p.get("state") in STATES_OPEN]

    def new_client_order_id(self) -> str:
        return uuid.uuid4().hex

    def write_intent(self, *, client_order_id: str, mint: str, source_sig: str,
                      source_slot: int | None, sol_in: float, pool: str | None,
                      program: str | None, taxed: bool | None, tax_bps: int | None,
                      mode: str, sell_after_s: float) -> dict:
        """Намерение купить -- НА ДИСК ДО отправки запроса.

        Если запрос уйдёт и служба упадёт до записи ответа, позиция всё
        равно будет известна: сторож найдёт токен по цепи и продаст.
        """
        row = {"client_order_id": client_order_id, "state": "intent",
                "mint": mint, "source_sig": source_sig, "source_slot": source_slot,
                "sol_in": sol_in, "pool": pool, "program": program,
                "taxed": taxed, "tax_bps": tax_bps, "mode": mode,
                "sell_after_s": sell_after_s,
                "ts_intent": time.time(),
                "ts_intent_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "wallet": EXECUTOR_WALLET}
        append_jsonl_fsync(self.positions_path, row)
        return row

    def update_position(self, client_order_id: str, **поля) -> dict:
        row = {"client_order_id": client_order_id,
                "ts_update": time.time(),
                "ts_update_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                **поля}
        append_jsonl_fsync(self.positions_path, row)
        return row

    def log_decision(self, row: dict) -> None:
        append_jsonl_fsync(self.decisions_path,
                            {"ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                             **row})

    # -------------------------------------------------------------- счётчики

    def counters(self) -> dict:
        if not self.counters_path.exists():
            return {}
        try:
            return json.loads(self.counters_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            # Битые счётчики -- это не повод торговать без счётчиков.
            return {"повреждены": True, "api_error_streak": 10 ** 6}

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

    def note_sell_outcome(self, *, sold: bool) -> dict:
        c = self.counters()
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
                "часовой_пояс_недоступен": беда}
        if path.exists():
            try:
                out.update(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                out["повреждён"] = True
        out["часовой_пояс_недоступен"] = беда
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
                "порог_остановки": WEEK_STOP_AT}
        if remaining is None:
            out["израсходовано_доля"] = None
            out["стоп"] = False
            out["почему"] = "остаток недельного лимита ещё не известен из заголовков"
            return out
        израсходовано = 1.0 - (int(remaining) / limit if limit else 1.0)
        out["израсходовано_доля"] = round(израсходовано, 4)
        out["стоп"] = израсходовано >= WEEK_STOP_AT
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

        c = self.counters()
        if c.get("повреждены"):
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
        if wb.get("стоп"):
            return False, (f"недельный бюджет запросов Bloom израсходован на "
                            f"{wb['израсходовано_доля'] * 100:.0f}% при пороге "
                            f"{WEEK_STOP_AT * 100:.0f}%"), КОД_БЮДЖЕТ_НЕДЕЛИ

        открытые = self.open_positions()
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
            "каталог_состояния": str(self.base),
            "рубильник": {"путь": str(self.kill_path), "включён": убит, "почему": почему},
            "лимиты": {"макс_открытых": self.max_open,
                        "дневной_лимит_потерь_sol": self.daily_loss_sol,
                        "вход_sol": self.buy_sol,
                        "резерв_на_комиссии_sol": self.fee_reserve_sol,
                        "порог_ошибок_api": self.api_error_streak_max,
                        "порог_непроданных": self.unsold_streak_max,
                        "пауза_при_429_с": self.rate_limited_pause_s,
                        "кулдаун_минта_с": self.mint_cooldown_s,
                        "покупок_на_минт": self.max_buys_per_mint},
            "открытых_позиций": len(открытые),
            "минты_открытых": sorted({p_.get("mint") for p_ in открытые if p_.get("mint")}),
            "счётчики": self.counters(),
            "дневной_итог": p,
            "бюджет_запросов_bloom": self.week_budget_state(),
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
                     taxed=False, tax_bps=None, mode="dry-run", sell_after_s=28.8)
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
                          taxed=None, tax_bps=None, mode="dry-run", sell_after_s=29)
    ok, почему7 = st4.can_open(mint="MNEW", source_sig="SNEW", balance_sol=1.0)
    chk("потолок открытых позиций держится", ok is False and "при лимите" in почему7,
        почему7)

    # лимит по минту: открытая позиция по тому же минту
    st5 = ExecState(base=base / "state5", kill=kill)
    st5.write_intent(client_order_id="a", mint="SAME", source_sig="SA", source_slot=1,
                      sol_in=0.2, pool=None, program=None, taxed=None, tax_bps=None,
                      mode="dry-run", sell_after_s=29)
    ok, почему8 = st5.can_open(mint="SAME", source_sig="SB", balance_sol=1.0)
    chk("вторая позиция по тому же минту запрещена",
        ok is False and "уже есть открытая позиция" in почему8, почему8)

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
        st14.week_budget_state()["стоп"] is False
        and st14.week_budget_state()["израсходовано_доля"] is None)
    st14.note_rate_headers({"X-RateLimit-Limit-Week": "10000",
                             "X-RateLimit-Remaining-Week": "2500",
                             "x-ratelimit-remaining-minute": "59"})
    wb = st14.week_budget_state()
    chk("остаток недели прочитан из заголовка", wb["remaining_week"] == 2500, str(wb))
    chk("израсходовано 75% -- стоп при пороге 80% ещё нет", wb["стоп"] is False)
    st14.note_rate_headers({"X-RateLimit-Remaining-Week": "1500"})
    chk("израсходовано 85% -- стоп", st14.week_budget_state()["стоп"] is True)
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
                        mode="dry", sell_after_s=28.8)
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

    # --- кошельки
    chk("адрес исполнителя -- константа нужного вида",
        EXECUTOR_WALLET.startswith("4s87RRC2") and len(EXECUTOR_WALLET) == 44)
    chk("чужой кошелёк W1 известен и не равен нашему",
        FOREIGN_WALLET_W1 != EXECUTOR_WALLET)

    отчёт = st.report()
    chk("отчёт собирается", "лимиты" in отчёт and "рубильник" in отчёт)

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
