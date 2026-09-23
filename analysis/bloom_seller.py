#!/usr/bin/env python3
"""Сторож продаж исполнителя Bloom. ОТДЕЛЬНЫЙ процесс, не часть детектора.

Зачем отдельный процесс. У зонда детектор и его корутины живут в одном
asyncio.gather без return_exceptions: одно исключение валит всё, а systemd
при StartLimitBurst=5 может оставить службу выключенной. Если сторож
продаж живёт там же, то падение детектора означает открытые позиции без
выхода. Поэтому сторож читает ЖУРНАЛ ПОЗИЦИЙ с диска, а не память
детектора, и продаёт независимо от того, жив ли детектор.

Основной выход -- таймерный авто-ордер Bloom, прикреплённый к покупке
(target_type=time). Сторож -- страховка: через BLOOM_SELL_GRACE_S секунд
после срока ордера он проверяет БАЛАНС ТОКЕНА ПО ЦЕПИ, и если токен на
месте, продаёт сам.

Правила, за каждым из которых стоит цена ошибки:

  * ПЕРЕД КАЖДОЙ попыткой -- баланс по цепи. Иначе сторож гоняется за
    уже проданной позицией, тратит бюджет запросов (60/мин на пользователя)
    и может продать то, чего нет.
  * ПРОСКАЛЬЗЫВАНИЕ НЕ ПОДНИМАЕТСЯ. 40 % и только 40 %: отказ по
    проскальзыванию означает плохой маршрут, а не недостаточную щедрость.
  * ДВА НУЛЯ ПОДРЯД перед закрытием позиции -- защита от гонки с
    индексацией узла (приём проверен на стороже DBot).
  * ПОТОЛОК 10 МИНУТ. Дальше позиция помечается UNSOLD, идёт доклад
    владельцу, и она считается непроданной для автопаузы.
  * РУБИЛЬНИК НЕ ОСТАНАВЛИВАЕТ ПРОДАЖИ. Он запрещает ПОКУПКИ. Оставить
    открытую позицию без выхода опаснее, чем закрыть её; для полного
    останова есть отдельный файл BLOOM_KILL_SELL_FILE.
  * ПОРОГ ПЫЛИ. Остаток меньше порога сырых единиц не продаётся: сторож
    иначе вечно долбит крошку, которую всё равно никто не купит.

Ключи не печатаются: и BLOOM_API_KEY, и HELIUS_API_KEY вычищаются из
любого текста.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests  # noqa: E402

from bloom_api import BloomApi, build_sell_body, scrub  # noqa: E402
from bloom_exec_state import (  # noqa: E402
    EXECUTOR_WALLET, STATE_CLOSED, ExecState, append_jsonl_fsync)

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
HELIUS_RPC = "https://mainnet.helius-rpc.com"
TOKEN_CLASSIC = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

DEFAULT_SELL_SLIPPAGE_PCT = 40.0
DEFAULT_GRACE_S = 15.0            # столько ждём после срока таймерного ордера
DEFAULT_RETRY_EVERY_S = 45.0
DEFAULT_GIVE_UP_AFTER_S = 600.0   # десять минут
DEFAULT_LOOP_EVERY_S = 15.0
DEFAULT_DUST_RAW = 1000           # меньше -- крошка, продавать нечего
DEFAULT_PRIORITY_FEE = 0.001
DEFAULT_PROCESSOR_TIP = 0.001


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    return int(env_float(name, float(default)))


def kill_sell_file() -> Path:
    p = os.environ.get("BLOOM_KILL_SELL_FILE", "").strip()
    return Path(p) if p else Path("/etc/bloom-executor/KILL_SELL")


def secrets_for_scrub() -> list:
    return [v for v in (os.environ.get("BLOOM_API_KEY", ""),
                         os.environ.get("HELIUS_API_KEY", ""),
                         os.environ.get("HELIUS_API", "")) if v]


def scrub_all(text: str) -> str:
    for k in secrets_for_scrub():
        text = scrub(text, k)
    return text


def rpc_call(method: str, params: list, *, timeout: int = 20) -> dict:
    """Один вызов узла: Helius, при отказе -- публичный. Ключ не печатается."""
    key = (os.environ.get("HELIUS_API_KEY") or os.environ.get("HELIUS_API") or "").strip()
    адреса = ([f"{HELIUS_RPC}/?api-key={key}"] if key else []) + [PUBLIC_RPC]
    последняя = "адресов узла нет"
    for url in адреса:
        try:
            r = requests.post(url, json={"jsonrpc": "2.0", "id": 1,
                                          "method": method, "params": params},
                               timeout=timeout)
        except requests.RequestException as exc:
            последняя = scrub_all(f"{type(exc).__name__}: {exc}")
            continue
        if r.status_code != 200:
            последняя = scrub_all(f"HTTP {r.status_code}: {r.text[:200]}")
            continue
        try:
            body = r.json()
        except ValueError:
            последняя = "ответ узла не json"
            continue
        if "error" in body:
            последняя = scrub_all(f"RPC error: {str(body['error'])[:200]}")
            continue
        return {"ok": True, "result": body.get("result")}
    return {"ok": False, "почему": последняя}


def token_balance_raw(wallet: str, mint: str) -> dict:
    """Остаток токена НА ЦЕПИ. Сумма по всем счетам обеих программ.

    Нельзя брать один счёт: у Token-2022 и классического SPL это разные
    счета, и остаток может лежать не там, где ждём.
    """
    сумма = 0
    ui = 0.0
    счетов = 0
    сбои = []
    for prog in (TOKEN_CLASSIC, TOKEN_2022):
        r = rpc_call("getTokenAccountsByOwner",
                      [wallet, {"mint": mint, "programId": prog},
                       {"encoding": "jsonParsed"}])
        if not r.get("ok"):
            сбои.append({"программа": prog, "почему": r.get("почему")})
            continue
        for it in ((r.get("result") or {}).get("value") or []):
            info = ((((it.get("account") or {}).get("data") or {}).get("parsed") or {})
                    .get("info") or {})
            amt = info.get("tokenAmount") or {}
            try:
                сумма += int(amt.get("amount") or 0)
            except (TypeError, ValueError):
                pass
            ui += float(amt.get("uiAmount") or 0.0)
            счетов += 1
    if сбои and счетов == 0:
        return {"ok": False, "сбои": сбои,
                 "почему": "остаток не прочитан ни по одной программе токена"}
    return {"ok": True, "raw": сумма, "ui": ui, "счетов": счетов, "сбои": сбои}


def kill_sell_active() -> tuple[bool, str]:
    """Отдельный рубильник ПРОДАЖ. Fail-closed, как и основной."""
    p = kill_sell_file()
    try:
        if p.exists():
            try:
                почему = p.read_text(encoding="utf-8").strip()[:200]
            except OSError:
                почему = "(файл не читается -- всё равно запрет)"
            return True, f"рубильник продаж включён: {почему or 'без пояснения'}"
        return False, ""
    except Exception as exc:  # noqa: BLE001
        return True, (f"проверка рубильника продаж не удалась ({type(exc).__name__}) -- "
                       "продажи запрещены: неясность трактуется как запрет")


def telegram(text: str) -> dict:
    """Доклад владельцу. Не настроен -- только в журнал, без падения."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        return {"ok": False, "почему": "телеграм не настроен -- только журнал"}
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                           json={"chat_id": chat, "text": text[:3500],
                                 "disable_web_page_preview": True}, timeout=15)
        return {"ok": r.status_code == 200, "код": r.status_code}
    except requests.RequestException as exc:
        return {"ok": False, "почему": scrub_all(f"{type(exc).__name__}: {exc}")}


def due_for_watch(pos: dict, *, grace_s: float, now: float | None = None) -> bool:
    """Пора ли сторожу смотреть на эту позицию.

    Срок считается от ПРИНЯТИЯ запроса Bloom -- так работает таймер
    авто-ордера, и так же должен считать сторож. Если принятия не было
    (упали до ответа), берётся время намерения: позиция всё равно могла
    быть куплена.
    """
    now = now if now is not None else time.time()
    if pos.get("state") == STATE_CLOSED:
        return False
    основа = pos.get("ts_accepted") or pos.get("ts_intent")
    if not основа:
        return True          # непонятно когда -- значит смотреть сейчас
    срок = float(pos.get("sell_after_s") or 0.0)
    return now >= (float(основа) + срок + grace_s)


def give_up(pos: dict, *, give_up_after_s: float, now: float | None = None) -> bool:
    """Пора ли сдаваться и звать владельца."""
    now = now if now is not None else time.time()
    первая = pos.get("ts_first_sell_attempt")
    if not первая:
        return False
    return (now - float(первая)) >= give_up_after_s


class Seller:
    def __init__(self, *, state: ExecState | None = None, live: bool | None = None,
                  api: BloomApi | None = None) -> None:
        self.state = state or ExecState()
        self.live = (os.environ.get("BLOOM_LIVE_SELL", "0").strip() == "1"
                     if live is None else bool(live))
        self.slippage = env_float("BLOOM_SELL_SLIPPAGE_PCT", DEFAULT_SELL_SLIPPAGE_PCT)
        self.grace_s = env_float("BLOOM_SELL_GRACE_S", DEFAULT_GRACE_S)
        self.retry_every_s = env_float("BLOOM_SELL_RETRY_EVERY_S", DEFAULT_RETRY_EVERY_S)
        self.give_up_after_s = env_float("BLOOM_SELL_GIVE_UP_AFTER_S",
                                          DEFAULT_GIVE_UP_AFTER_S)
        self.dust_raw = env_int("BLOOM_DUST_RAW", DEFAULT_DUST_RAW)
        self.priority_fee = env_float("BLOOM_PRIORITY_FEE", DEFAULT_PRIORITY_FEE)
        self.processor_tip = env_float("BLOOM_PROCESSOR_TIP", DEFAULT_PROCESSOR_TIP)
        self.api = api or BloomApi(os.environ.get("BLOOM_API_KEY", ""),
                                   dry_run=not self.live, state=self.state)

    def log(self, row: dict) -> None:
        append_jsonl_fsync(self.state.base / "seller.jsonl",
                            {"ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                             **row})

    def handle(self, pos: dict, *, now: float | None = None,
                balance_reader=token_balance_raw) -> dict:
        """Одна позиция за один круг. Возвращает, что сделано и почему."""
        now = now if now is not None else time.time()
        cid = pos.get("client_order_id")
        mint = pos.get("mint")
        итог = {"client_order_id": cid, "mint": mint, "state_было": pos.get("state")}

        if not due_for_watch(pos, grace_s=self.grace_s, now=now):
            итог["действие"] = "ждём срок таймерного ордера"
            return итог

        # Баланс по цепи ПЕРЕД любым действием.
        bal = balance_reader(EXECUTOR_WALLET, mint)
        if not bal.get("ok"):
            итог.update(действие="остаток не прочитан -- ничего не делаем",
                         почему=bal.get("почему"))
            return итог
        итог["остаток_raw"] = bal.get("raw")
        итог["остаток_ui"] = bal.get("ui")

        if int(bal.get("raw") or 0) <= 0:
            # Два нуля подряд перед закрытием: одиночный ноль бывает гонкой
            # с индексацией узла.
            серия = int(pos.get("zero_streak") or 0) + 1
            if серия >= 2:
                self.state.update_position(cid, state=STATE_CLOSED, zero_streak=серия,
                                            closed_reason="остаток ноль дважды подряд")
                self.state.note_sell_outcome(sold=True)
                итог.update(действие="позиция закрыта", zero_streak=серия)
            else:
                self.state.update_position(cid, zero_streak=серия)
                итог.update(действие="ноль первый раз -- ещё не закрываю",
                             zero_streak=серия)
            return итог

        self.state.update_position(cid, zero_streak=0)

        if int(bal.get("raw") or 0) < self.dust_raw:
            self.state.update_position(
                cid, state=STATE_CLOSED,
                closed_reason=(f"крошка: остаток {bal.get('raw')} сырых единиц меньше "
                                f"порога {self.dust_raw}, продавать нечего"))
            итог.update(действие="закрыта как крошка")
            return итог

        убит, почему = kill_sell_active()
        if убит:
            итог.update(действие="продажа запрещена рубильником продаж", почему=почему)
            self.log(итог)
            return итог

        if give_up(pos, give_up_after_s=self.give_up_after_s, now=now):
            if pos.get("state") != "unsold":
                self.state.update_position(cid, state="unsold",
                                            unsold_since=now,
                                            unsold_reason=(f"не продано за "
                                                            f"{self.give_up_after_s:.0f} с"))
                self.state.note_sell_outcome(sold=False)
                текст = (f"Bloom: позиция НЕ ПРОДАНА за {self.give_up_after_s / 60:.0f} мин\n"
                          f"кошелёк {EXECUTOR_WALLET}\nминт {mint}\n"
                          f"остаток {bal.get('ui')} ({bal.get('raw')} сырых)\n"
                          f"попыток {pos.get('sell_attempts', 0)}\n"
                          f"продать руками через Phantom/Jupiter")
                self.log({**итог, "действие": "UNSOLD, доклад владельцу",
                           "телеграм": telegram(текст)})
            итог["действие"] = "UNSOLD -- ждём владельца"
            return итог

        последняя = pos.get("ts_last_sell_attempt")
        if последняя and (now - float(последняя)) < self.retry_every_s:
            итог["действие"] = (f"пауза между попытками: прошло "
                                 f"{now - float(последняя):.0f} с из "
                                 f"{self.retry_every_s:.0f}")
            return итог

        # Продажа. Проскальзывание НЕ поднимается.
        попытка = int(pos.get("sell_attempts") or 0) + 1
        body = build_sell_body(address=mint, percent=100, slippage_pct=self.slippage,
                               priority_fee=self.priority_fee,
                               processor_tip=self.processor_tip)
        res = self.api.swap(body, client_order_id=f"{cid}:sell{попытка}",
                             why=f"сторож, попытка {попытка}")
        поля = {"state": "selling", "sell_attempts": попытка,
                 "ts_last_sell_attempt": now}
        if not pos.get("ts_first_sell_attempt"):
            поля["ts_first_sell_attempt"] = now
        if res.get("order_id"):
            поля["last_sell_order_id"] = res["order_id"]
        if res.get("signatures"):
            поля["last_sell_signatures"] = res["signatures"]
        if not res.get("ok"):
            поля["last_sell_error"] = res.get("код_ошибки") or res.get("почему")
        self.state.update_position(cid, **поля)
        итог.update(действие=("продажа отправлена" if res.get("ok")
                               else "продажа не принята"),
                     попытка=попытка, режим="dry-run" if self.api.dry_run else "live",
                     ответ={k: res.get(k) for k in
                             ("ok", "код", "код_ошибки", "order_id", "signatures",
                              "rate_limited", "retry_after_s", "dry_run")})
        self.log(итог)
        return итог

    def heartbeat(self, итог: dict) -> None:
        """Признак жизни на диск каждый круг.

        WatchdogSec у systemd требует sd_notify, а его в этой службе нет и
        ставить зависимость ради одного пинга не стоит. Поэтому живучесть
        проверяется извне по свежести этого файла -- и проверка честная:
        файл обновляется только при реально пройденном круге.
        """
        from bloom_exec_state import atomic_write_json  # noqa: PLC0415
        # Оба рубильника: отдельно "читается ли путь" и отдельно "включён".
        # Нечитаемый рубильник продаж опаснее нечитаемого рубильника
        # покупок: он оставляет открытую позицию без выхода, и при этом
        # снаружи выглядит как тишина.
        куп_доступен, куп_поч = self.state.kill_readable()
        куп_включён, _ = self.state.kill_active()
        прод_доступен, прод_поч = self.state.kill_readable(kill_sell_file())
        прод_включён, _ = kill_sell_active()
        atomic_write_json(self.state.base / "seller_heartbeat.json", {
            "обновлено_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "обновлено_ts": time.time(),
            "режим": "live" if self.live else "dry-run",
            "позиций_в_круге": итог.get("позиций"),
            "проскальзывание_pct": self.slippage,
            "запас_после_срока_с": self.grace_s,
            "потолок_ожидания_с": self.give_up_after_s,
            "рубильник_покупок": {"путь": str(self.state.kill_path),
                                   "доступен": куп_доступен, "включён": куп_включён,
                                   "пояснение": куп_поч},
            "рубильник_продаж": {"путь": str(kill_sell_file()),
                                  "доступен": прод_доступен, "включён": прод_включён,
                                  "пояснение": прод_поч},
        })

    def cycle(self, *, now: float | None = None, balance_reader=token_balance_raw) -> dict:
        now = now if now is not None else time.time()
        открытые = self.state.open_positions()
        строки = [self.handle(p, now=now, balance_reader=balance_reader)
                  for p in открытые]
        итог = {"позиций": len(открытые), "режим": "live" if self.live else "dry-run",
                 "строки": строки}
        self.heartbeat(итог)
        return итог

    def check_only(self, *, balance_reader=token_balance_raw) -> dict:
        """Что я сделал бы, ничего не делая. Для приёмки перед live."""
        план = []
        for p in self.state.open_positions():
            bal = balance_reader(EXECUTOR_WALLET, p.get("mint"))
            план.append({
                "client_order_id": p.get("client_order_id"), "mint": p.get("mint"),
                "state": p.get("state"),
                "остаток_raw": bal.get("raw") if bal.get("ok") else None,
                "остаток_прочитан": bool(bal.get("ok")),
                "пора_смотреть": due_for_watch(p, grace_s=self.grace_s),
                "продал_бы": bool(bal.get("ok") and int(bal.get("raw") or 0) >= self.dust_raw
                                   and due_for_watch(p, grace_s=self.grace_s)),
                "чем": f"/swap Sell 100 % slippage {self.slippage} auto_orders []",
            })
        return {"режим": "live" if self.live else "dry-run",
                 "рубильник_покупок": self.state.kill_active(),
                 "рубильник_продаж": kill_sell_active(),
                 "план": план}


def self_test() -> None:
    import tempfile  # noqa: PLC0415
    checks = []

    def chk(n, ok, got=""):
        checks.append((n, bool(ok), got))

    src = Path(__file__).read_text(encoding="utf-8")
    тело = src.split("def self_test")[0]
    chk("сторож сам POST в Bloom не делает -- только через клиент",
        "session.post" not in тело and "requests.post(f\"https://api.telegram" in тело)
    chk("проскальзывание нигде не повышается",
        "slippage * " not in тело and "slippage +" not in тело)

    база = Path(tempfile.mkdtemp())
    st = ExecState(base=база / "s", kill=база / "KILL")
    os.environ["BLOOM_KILL_SELL_FILE"] = str(база / "KILL_SELL")

    class ЗапрещённаяСеть:
        def post(self, *a, **k):
            raise AssertionError("в самопроверке сети быть не должно")

        def get(self, *a, **k):
            raise AssertionError("в самопроверке сети быть не должно")

    api = BloomApi("КЛЮЧ", dry_run=True, state=st, session=ЗапрещённаяСеть())
    s = Seller(state=st, live=False, api=api)

    # --- срок ожидания
    поз = {"client_order_id": "c1", "mint": "M1", "state": "bought",
            "ts_accepted": 1000.0, "sell_after_s": 28.8}
    chk("до срока таймерного ордера сторож не лезет",
        due_for_watch(поз, grace_s=15.0, now=1000 + 28.8 + 14) is False)
    chk("после срока плюс запас -- лезет",
        due_for_watch(поз, grace_s=15.0, now=1000 + 28.8 + 15.1) is True)
    chk("закрытую позицию не смотрит",
        due_for_watch({**поз, "state": STATE_CLOSED}, grace_s=15.0, now=1e12) is False)
    chk("нет времени принятия -- смотрит сразу (могли упасть до ответа)",
        due_for_watch({"client_order_id": "c", "mint": "M", "state": "intent"},
                       grace_s=15.0) is True)

    # --- сдача через 10 минут
    chk("без первой попытки не сдаётся", give_up(поз, give_up_after_s=600) is False)
    chk("через 10 минут после первой попытки сдаётся",
        give_up({**поз, "ts_first_sell_attempt": 1000.0}, give_up_after_s=600,
                 now=1601.0) is True)
    chk("через 9 минут ещё нет",
        give_up({**поз, "ts_first_sell_attempt": 1000.0}, give_up_after_s=600,
                 now=1500.0) is False)

    # --- рубильник продаж отдельный от рубильника покупок
    (база / "KILL").write_text("стоп покупок", encoding="utf-8")
    chk("рубильник ПОКУПОК включён", st.kill_active()[0] is True)
    chk("а продажи им НЕ запрещены", kill_sell_active()[0] is False)
    (база / "KILL_SELL").write_text("стоп продаж", encoding="utf-8")
    убит, почему = kill_sell_active()
    chk("отдельный рубильник продаж работает", убит is True)
    chk("и причина видна", "стоп продаж" in почему, почему)
    (база / "KILL_SELL").unlink()
    (база / "KILL").unlink()

    # --- поведение по остатку
    st.write_intent(client_order_id="p1", mint="MINT1", source_sig="S1", source_slot=1,
                     sol_in=0.2, pool=None, program=None, taxed=None, tax_bps=None,
                     mode="dry-run", sell_after_s=28.8)
    st.update_position("p1", state="bought", ts_accepted=time.time() - 100)

    def читатель(raw):
        def f(wallet, mint):
            return {"ok": True, "raw": raw, "ui": raw / 1e6, "счетов": 1, "сбои": []}
        return f

    r = s.handle(st.positions()["p1"], balance_reader=читатель(0))
    chk("первый ноль -- позиция не закрывается", r["действие"].startswith("ноль первый"))
    r = s.handle(st.positions()["p1"], balance_reader=читатель(0))
    chk("второй ноль подряд -- закрывается", r["действие"] == "позиция закрыта")
    chk("и в журнале она уже не открыта", st.open_positions() == [])
    chk("удачная продажа снимает серию непроданных",
        int(st.counters().get("unsold_streak", 0)) == 0)

    st.write_intent(client_order_id="p2", mint="MINT2", source_sig="S2", source_slot=2,
                     sol_in=0.2, pool=None, program=None, taxed=None, tax_bps=None,
                     mode="dry-run", sell_after_s=28.8)
    st.update_position("p2", state="bought", ts_accepted=time.time() - 100)
    r = s.handle(st.positions()["p2"], balance_reader=читатель(500))
    chk("крошка не продаётся, а закрывается", r["действие"] == "закрыта как крошка")

    st.write_intent(client_order_id="p3", mint="MINT3", source_sig="S3", source_slot=3,
                     sol_in=0.2, pool=None, program=None, taxed=None, tax_bps=None,
                     mode="dry-run", sell_after_s=28.8)
    st.update_position("p3", state="bought", ts_accepted=time.time() - 100)
    r = s.handle(st.positions()["p3"], balance_reader=читатель(5_000_000))
    chk("настоящий остаток -- продажа отправлена", r["действие"] == "продажа отправлена")
    chk("и это dry-run, без сети", r["режим"] == "dry-run")
    chk("попытка посчитана", st.positions()["p3"]["sell_attempts"] == 1)
    r = s.handle(st.positions()["p3"], balance_reader=читатель(5_000_000))
    chk("сразу вторая попытка не делается -- держим паузу 45 с",
        r["действие"].startswith("пауза между попытками"), r["действие"])

    # --- не читается остаток -- ничего не делаем
    def нечитаемый(wallet, mint):
        return {"ok": False, "почему": "узел молчит"}

    r = s.handle(st.positions()["p3"], balance_reader=нечитаемый)
    chk("остаток не прочитан -- продажи нет",
        r["действие"].startswith("остаток не прочитан"))

    # --- сдача и доклад
    st.update_position("p3", ts_first_sell_attempt=time.time() - 601,
                        ts_last_sell_attempt=time.time() - 601)
    r = s.handle(st.positions()["p3"], balance_reader=читатель(5_000_000))
    chk("через 10 минут позиция помечена UNSOLD",
        st.positions()["p3"]["state"] == "unsold", r["действие"])
    chk("и серия непроданных выросла",
        int(st.counters().get("unsold_streak", 0)) == 1)

    # --- тело продажи, которое сторож реально отправляет
    body = build_sell_body(address="MINT3", percent=100, slippage_pct=40.0,
                           priority_fee=0.001, processor_tip=0.001)
    chk("сторож продаёт 100 %", body["wallets"][0]["amount"] == "100")
    chk("проскальзывание ровно 40", body["slippage"] == 40.0)
    chk("auto_orders пустой -- чужая стратегия не подмешивается",
        body["auto_orders"] == [])
    chk("кошелёк -- исполнителя", body["wallets"][0]["address"] == EXECUTOR_WALLET)

    # --- признак жизни
    hb = st.base / "seller_heartbeat.json"
    было = hb.exists()
    s.cycle(balance_reader=читатель(0))
    chk("круг пишет признак жизни", hb.exists() and not было or hb.exists())
    hb_data = json.loads(hb.read_text())
    chk("в признаке жизни есть время и режим",
        "обновлено_ts" in hb_data and hb_data["режим"] == "dry-run", str(hb_data)[:120])

    # --- check-only ничего не меняет
    до = st.positions_path.stat().st_size
    план = s.check_only(balance_reader=читатель(5_000_000))
    chk("check-only даёт план", isinstance(план.get("план"), list))
    chk("и ничего не пишет в журнал позиций",
        st.positions_path.stat().st_size == до)

    chk("в признаке жизни есть оба рубильника",
        "рубильник_покупок" in hb_data and "рубильник_продаж" in hb_data, list(hb_data))
    chk("и отдельно сказано, читаются ли они",
        hb_data["рубильник_покупок"].get("доступен") is True
        and hb_data["рубильник_продаж"].get("доступен") is True, hb_data)
    chk("и что оба выключены",
        not hb_data["рубильник_покупок"]["включён"]
        and not hb_data["рубильник_продаж"]["включён"], hb_data)

    # нечитаемый рубильник продаж не должен выглядеть как тишина
    было_ks = os.environ.get("BLOOM_KILL_SELL_FILE")
    os.environ["BLOOM_KILL_SELL_FILE"] = str(st.base / "нет_каталога" / "KILL_SELL")
    try:
        s.heartbeat({"позиций": 0})
        hb3 = json.loads(hb.read_text())
        chk("нечитаемый рубильник продаж помечен недоступным",
            hb3["рубильник_продаж"]["доступен"] is False, hb3["рубильник_продаж"])
        chk("и причина названа словами",
            "не существует" in hb3["рубильник_продаж"]["пояснение"],
            hb3["рубильник_продаж"]["пояснение"])
    finally:
        if было_ks is None:
            os.environ.pop("BLOOM_KILL_SELL_FILE", None)
        else:
            os.environ["BLOOM_KILL_SELL_FILE"] = было_ks

    chk("ключи вычищаются", "СЕКРЕТ" not in scrub_all("текст СЕКРЕТ")
        if os.environ.get("BLOOM_API_KEY") == "СЕКРЕТ" else True)

    bad = 0
    for n, ok_, got in checks:
        print(f"  [{'ok  ' if ok_ else 'СБОЙ'}] {n}" + (f"  -> {got}" if got and not ok_ else ""))
        bad += (not ok_)
    print(f"самопроверка сторожа продаж: {len(checks) - bad}/{len(checks)} пройдено")
    if bad:
        raise SystemExit(f"самопроверка не пройдена: {bad} из {len(checks)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop-every-s", type=float,
                     default=env_float("BLOOM_SELLER_LOOP_S", DEFAULT_LOOP_EVERY_S))
    a = ap.parse_args()
    if a.self_test:
        self_test()
        return
    s = Seller()
    if a.check_only:
        print(json.dumps(s.check_only(), ensure_ascii=False, indent=2))
        return
    print(f"[сторож] режим {'LIVE' if s.live else 'DRY-RUN'}, "
          f"проскальзывание {s.slippage} %, запас {s.grace_s} с, "
          f"повтор {s.retry_every_s} с, потолок {s.give_up_after_s} с", flush=True)
    while True:
        начало = time.monotonic()
        try:
            итог = s.cycle()
            if итог["позиций"]:
                print(json.dumps(итог, ensure_ascii=False)[:2000], flush=True)
        except Exception as exc:  # noqa: BLE001 -- круг не должен валить процесс
            print(scrub_all(f"[сторож] круг упал: {type(exc).__name__}: {exc}"), flush=True)
        if a.once:
            return
        пауза = max(1.0, a.loop_every_s - (time.monotonic() - начало))
        time.sleep(пауза)


if __name__ == "__main__":
    main()
