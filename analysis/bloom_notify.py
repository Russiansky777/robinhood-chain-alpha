#!/usr/bin/env python3
"""Короткие строки о событиях стенда в Telegram. ТОЛЬКО тестовые источники.

Зачем отдельный модуль. Строки нужны и детектору, и сторожу, а правила у
них общие и цена ошибки одинаковая:

  * ОТПРАВКА НЕ МЕШАЕТ ТОРГОВЛЕ. Ни одно исключение отсюда наружу не
    выходит: сбой Telegram -- это запись в признак жизни, а не потерянное
    решение. Поэтому у отправителя нет ни одного raise, а вызов из горячего
    пути идёт в фоновом потоке.
  * БОЕВЫЕ ИСТОЧНИКИ МОЛЧАТ. Владелец просил строки только по стенду;
    решение о боевом источнике в Telegram не уходит вообще, чтобы поток не
    утонул в 100 решениях в час.
  * КЛЮЧИ НЕ ПЕЧАТАЮТСЯ. Всё, что уходит, проходит через вычистку.
  * ПОЛУЧАТЕЛЬ ОДИН -- TELEGRAM_CHAT_ID владельца. Ни адреса, ни токена в
    репозитории нет: и то и другое только из окружения.

Строки короткие намеренно: их читают с телефона в момент сделки, а не
разбирают потом. Полная картина остаётся в журнале и в докладе.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

API = "https://api.telegram.org"
ПРЕДЕЛ_ТЕКСТА = 3500


def включено() -> bool:
    """Строки стенда включены? Выключено -- полная тишина, без ошибок."""
    return (os.environ.get("BLOOM_TELEGRAM_LIVE_TEST", "0").strip() == "1")


def настроен() -> tuple:
    токен = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    чат = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not токен or not чат:
        return False, "телеграм не настроен: нет токена или chat_id"
    return True, ""


def _секреты() -> list:
    return [v for v in (os.environ.get("TELEGRAM_BOT_TOKEN", ""),
                         os.environ.get("BLOOM_API_KEY", ""),
                         os.environ.get("HELIUS_API_KEY", ""),
                         os.environ.get("HELIUS_API", ""),
                         os.environ.get("DBOT_API_KEY", "")) if v]


def вычистить(текст: str) -> str:
    out = str(текст)
    for k in _секреты():
        if k:
            out = out.replace(k, "***")
    return out


def кратко(подпись: str | None, n: int = 10) -> str:
    s = str(подпись or "")
    return (s[:n] + "…") if len(s) > n else (s or "-")


def _время(ts: float | None = None) -> str:
    return time.strftime("%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))


# ------------------------------------------------------------------ строки

def строка_решения(row: dict) -> str:
    """Решение детектора по тестовому источнику."""
    код = row.get("code") or row.get("action") or "?"
    лаг = row.get("slot_lag")
    путь = "MSG" if (row.get("parsed_from") or "").endswith("MSG") else (
        "RPC" if row.get("parsed_from") else "?")
    версия = row.get("tx_version")
    трата = row.get("spend_sol_eq")
    трата_s = f"{float(трата):.4f} SOL" if isinstance(трата, (int, float)) else "трата ?"
    return (f"🔎 {_время()} стенд {код} · лаг {лаг if лаг is not None else '?'} сл · "
            f"{путь} v{версия if версия is not None else '?'} · {трата_s} · "
            f"{кратко(row.get('mint'), 6)} · src {кратко(row.get('signature'), 8)}")


def строка_покупки(*, exec_row: dict, наш_слот: int | None,
                    слот_источника: int | None, размер_sol, метки=None) -> str:
    """Наша покупка: подпись, S+N, куда покупали, размер."""
    подписи = exec_row.get("signatures") or []
    вид = exec_row.get("buy_address_kind") or "?"
    адрес = exec_row.get("buy_address")
    куда = (f"пул {кратко(адрес, 8)}" if вид == "pool" else "минт")
    if (isinstance(наш_слот, int) and isinstance(слот_источника, int)):
        дельта = f"S+{наш_слот - слот_источника}"
    else:
        дельта = "S+?"
    размер = (f"{float(размер_sol):.4f} SOL"
              if isinstance(размер_sol, (int, float)) else "размер ?")
    хвост = (" · " + ",".join(метки)) if метки else ""
    код = exec_row.get("exec_code") or "?"
    return (f"🟢 {_время()} покупка {код} {кратко(подписи[0] if подписи else None, 10)} · "
            f"{дельта} · по {куда} · {размер}{хвост}")


def строка_продажи(*, ok: bool, код: str | None, через: str, секунды,
                    sol_вернулось=None, подпись: str | None = None) -> str:
    """Продажа: успех или код ошибки, через что, секунды от покупки, SOL."""
    метка = "🔵" if ok else "🔴"
    итог = "продана" if ok else f"НЕ продана ({код or 'причина ?'})"
    сек = (f"{float(секунды):.0f} с от покупки"
           if isinstance(секунды, (int, float)) else "время от покупки ?")
    sol = (f"вернулось {float(sol_вернулось):+.6f} SOL"
           if isinstance(sol_вернулось, (int, float)) else "SOL ?")
    return (f"{метка} {_время()} продажа {итог} · через {через} · {сек} · {sol} · "
            f"{кратко(подпись, 10)}")


def строка_тревоги(что: str, подробно: str = "") -> str:
    """UNSOLD, автопауза, рубильник -- отдельной строкой с пометкой."""
    хвост = f" · {подробно}" if подробно else ""
    return f"⚠️ {_время()} {что}{хвост}"


# ------------------------------------------------------------------ отправка

def код_итога(итог: dict) -> str:
    return f"код {итог.get('code')}" if итог.get("code") else "без причины"


class Оповещатель:
    """Отправитель без исключений наружу и без задержки горячего пути."""

    def __init__(self, *, отправитель=None, в_фоне: bool = True) -> None:
        # Отправитель подменяется в самопроверке: сети в ней быть не должно.
        self._отправитель = отправитель
        self._в_фоне = в_фоне
        self.послано = 0
        self.сбоев = 0
        self.последняя_ошибка = ""
        self.выключен_почему = ""

    def _по_сети(self, текст: str) -> dict:
        if requests is None:
            return {"ok": False, "why_not": "requests недоступен"}
        токен = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        чат = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        try:
            r = requests.post(f"{API}/bot{токен}/sendMessage",
                               json={"chat_id": чат, "text": текст[:ПРЕДЕЛ_ТЕКСТА],
                                     "disable_web_page_preview": True}, timeout=10)
            if r.status_code == 200:
                return {"ok": True, "code": 200}
            return {"ok": False, "code": r.status_code,
                     "why_not": вычистить(r.text[:200])}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "why_not": вычистить(f"{type(exc).__name__}: {exc}")}

    def отправить(self, текст: str) -> dict:
        """Синхронно. Наружу не бросает НИЧЕГО -- это главное свойство."""
        try:
            if not включено():
                self.выключен_почему = "строки стенда выключены (BLOOM_TELEGRAM_LIVE_TEST)"
                return {"ok": False, "skipped": True, "why_not": self.выключен_почему}
            готов, почему = настроен()
            if not готов:
                self.выключен_почему = почему
                self.сбоев += 1
                self.последняя_ошибка = почему
                return {"ok": False, "why_not": почему}
            фн = self._отправитель or self._по_сети
            итог = фн(вычистить(текст))
            if итог.get("ok"):
                self.послано += 1
            else:
                self.сбоев += 1
                self.последняя_ошибка = str(итог.get("why_not") or код_итога(итог))[:200]
            return итог
        except Exception as exc:  # noqa: BLE001
            # Сюда попасть можно только на ошибке в самом этом коде -- и даже
            # она не имеет права остановить торговлю.
            self.сбоев += 1
            self.последняя_ошибка = вычистить(f"{type(exc).__name__}: {exc}")[:200]
            return {"ok": False, "why_not": self.последняя_ошибка}

    def послать(self, текст: str) -> dict:
        """Из горячего пути: в фоновом потоке, без ожидания ответа Telegram."""
        if not self._в_фоне:
            return self.отправить(текст)
        try:
            t = threading.Thread(target=self.отправить, args=(текст,), daemon=True)
            t.start()
            return {"ok": None, "queued": True}
        except Exception as exc:  # noqa: BLE001
            self.сбоев += 1
            self.последняя_ошибка = вычистить(f"{type(exc).__name__}: {exc}")[:200]
            return {"ok": False, "why_not": self.последняя_ошибка}

    def статус(self) -> dict:
        """В признак жизни: сколько послано, сколько сбоев, последняя ошибка."""
        готов, почему = настроен()
        return {"enabled": включено(), "configured": готов,
                 "sent": self.послано, "failed": self.сбоев,
                 "last_error": self.последняя_ошибка,
                 "off_reason": self.выключен_почему or почему}


# ------------------------------------------------------------------ самотест

def self_test() -> int:
    проверки = []

    def chk(имя, ок, факт=""):
        проверки.append((имя, bool(ок), факт))

    было = {k: os.environ.get(k) for k in ("BLOOM_TELEGRAM_LIVE_TEST",
                                            "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
    os.environ["BLOOM_TELEGRAM_LIVE_TEST"] = "1"
    os.environ["TELEGRAM_BOT_TOKEN"] = "СЕКРЕТНЫЙ_ТОКЕН"
    os.environ["TELEGRAM_CHAT_ID"] = "123"
    try:
        s = строка_решения({"code": "BUY", "slot_lag": 0, "parsed_from": "PARSE_VIA_MSG",
                             "tx_version": 0, "spend_sol_eq": 0.060658,
                             "mint": "FvhorDts9M8", "signature": "2ffNL1jtzpVo"})
        chk("в строке решения есть код, лаг, путь и версия",
            "BUY" in s and "лаг 0" in s and "MSG" in s and "v0" in s, s)
        chk("и трата в SOL", "0.0607 SOL" in s, s)
        s_rpc = строка_решения({"code": "NOT_A_BUY", "slot_lag": 2,
                                 "parsed_from": "PARSE_VIA_RPC", "tx_version": None})
        chk("путь RPC назван RPC", "RPC" in s_rpc, s_rpc)
        chk("неизвестная версия не выдумывается", "v?" in s_rpc, s_rpc)
        chk("и неизвестная трата тоже", "трата ?" in s_rpc, s_rpc)

        b = строка_покупки(exec_row={"exec_code": "SENT", "signatures": ["5ycBKDykfUDAT"],
                                      "buy_address_kind": "pool",
                                      "buy_address": "5N9DdF1w1Q6tae"},
                            наш_слот=449836346, слот_источника=449836345,
                            размер_sol=0.001, метки=["ROUTE_MISMATCH"])
        chk("в строке покупки есть S+1", "S+1" in b, b)
        chk("и сказано, что покупали по пулу", "по пул" in b, b)
        chk("и метка расхождения маршрутов видна", "ROUTE_MISMATCH" in b, b)
        b2 = строка_покупки(exec_row={"exec_code": "SENT", "signatures": [],
                                       "buy_address_kind": "mint"},
                            наш_слот=None, слот_источника=1, размер_sol=None)
        chk("неизвестный слот не выдумывается", "S+?" in b2, b2)
        chk("и неизвестный размер тоже", "размер ?" in b2, b2)

        p = строка_продажи(ok=False, код="ProgramFailedToComplete", через="сторож, минт",
                            секунды=44, sol_вернулось=0.0, подпись="4wFuEBkspads")
        chk("в строке продажи есть код ошибки и через что",
            "ProgramFailedToComplete" in p and "сторож, минт" in p, p)
        chk("и секунды от покупки", "44 с от покупки" in p, p)
        p2 = строка_продажи(ok=True, код=None, через="авто-ордер Bloom", секунды=28.8,
                             sol_вернулось=0.000912)
        chk("удачная продажа помечена иначе", p2.startswith("🔵") and "продана" in p2, p2)
        chk("неизвестный SOL не выдумывается",
            "SOL ?" in строка_продажи(ok=True, код=None, через="x", секунды=None), "")

        t = строка_тревоги("UNSOLD", "2 неудачных попыток, минт FvhorDts")
        chk("тревога помечена значком", t.startswith("⚠️") and "UNSOLD" in t, t)

        # --- отправка: ни одного исключения наружу
        посланное = []

        def приёмник(текст):
            посланное.append(текст)
            return {"ok": True, "code": 200}

        o = Оповещатель(отправитель=приёмник, в_фоне=False)
        chk("отправка проходит", o.отправить("проверка")["ok"] is True)
        chk("и считается", o.статус()["sent"] == 1, o.статус())

        def падающий(текст):
            raise RuntimeError("телеграм лежит")

        o2 = Оповещатель(отправитель=падающий, в_фоне=False)
        итог = o2.отправить("проверка")
        chk("падение отправителя НЕ выходит наружу", итог["ok"] is False, итог)
        chk("и попадает в статус, а не в исключение",
            o2.статус()["failed"] == 1 and "телеграм лежит" in o2.статус()["last_error"],
            o2.статус())

        def отказ(текст):
            return {"ok": False, "code": 403, "why_not": "bot was blocked by the user"}

        o3 = Оповещатель(отправитель=отказ, в_фоне=False)
        o3.отправить("x")
        chk("отказ Telegram виден в статусе с причиной",
            "blocked" in o3.статус()["last_error"], o3.статус())

        chk("токен в текст не попадает",
            вычистить("ключ СЕКРЕТНЫЙ_ТОКЕН внутри") == "ключ *** внутри")

        os.environ["BLOOM_TELEGRAM_LIVE_TEST"] = "0"
        o4 = Оповещатель(отправитель=приёмник, в_фоне=False)
        r4 = o4.отправить("не должно уйти")
        chk("выключенные строки не отправляются", r4.get("skipped") is True, r4)
        chk("и это не считается сбоем", o4.статус()["failed"] == 0, o4.статус())

        os.environ["BLOOM_TELEGRAM_LIVE_TEST"] = "1"
        os.environ.pop("TELEGRAM_CHAT_ID")
        o5 = Оповещатель(отправитель=приёмник, в_фоне=False)
        r5 = o5.отправить("нет получателя")
        chk("без получателя -- честный отказ, а не молчание",
            r5["ok"] is False and "chat_id" in r5["why_not"], r5)

        src = Path(__file__).read_text(encoding="utf-8")
        тело = src.split("def self_test")[0]
        chk("в модуле нет ни одного raise", "raise " not in тело, "")
        chk("все ключи статуса латинские",
            all(k.isascii() for k in Оповещатель(в_фоне=False).статус()))
    finally:
        for k, v in было.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    плохо = [c for c in проверки if not c[1]]
    for имя, ок, факт in проверки:
        print(f"{'OK ' if ок else 'НЕТ'} {имя}"
              f"{(' -- ' + str(факт)[:200]) if факт and not ок else ''}")
    print(f"самопроверка оповещений: {len(проверки) - len(плохо)}/{len(проверки)}")
    return 1 if плохо else 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--send", default="", help="послать строку (только с ключами в окружении)")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(self_test())
    if a.send:
        print(Оповещатель(в_фоне=False).отправить(a.send))
    else:
        print(Оповещатель(в_фоне=False).статус())
