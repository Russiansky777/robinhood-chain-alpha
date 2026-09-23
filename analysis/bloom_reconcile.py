#!/usr/bin/env python3
"""Сверка при старте: можно ли включать настоящие покупки.

Зачем. Между прогонами состояние на хосте живёт своей жизнью: остались
позиции прошлого режима, на кошельке появилась чужая активность, баланса
не хватает, журнал смешан из двух форматов. Включать деньги, не проверив
этого, значит начинать опыт с неизвестного исходного состояния -- и
любой результат потом нельзя будет никому предъявить.

Что проверяется и почему именно это:
 1. Позиции dry-run. Они существуют только в журнале, сделок за ними нет.
    При переходе в live-test или live они ОТКЛАДЫВАЮТСЯ с меткой -- так же,
    как журналы прежнего формата, -- а не удаляются: данные стенда ещё
    пригодятся для разбора.
 2. Открытые НАСТОЯЩИЕ позиции. Если такие есть, стенд начинать нельзя:
    сторож должен сперва их закрыть, иначе непонятно, чья это позиция.
 3. Чужая активность на кошельке исполнителя. TradeWiz выключен, и на
    4s87RRC2... не должно быть сделок, которых мы не делали. Если есть --
    мы не одни в кошельке, и приписывать себе его результат нельзя.
 4. Баланс не ниже порога.
 5. Рубильники читаются службой (не только root) и выключены.
 6. Смешанный журнал: записи прежнего формата рядом с новыми.

Только чтение цепи и локального состояния. Откладывание файлов -- по
явному --archive, и оно ничего не удаляет.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_exec_state as ST  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "bloom_reconcile.json"

# Владелец, 23.09: кошелёк пополнен на 0.35 SOL, порог считать 0.3.
MIN_BALANCE_SOL = ST.env_float("BLOOM_MIN_START_BALANCE_SOL", 0.3)
# Сколько последних подписей кошелька смотреть на чужую активность.
FOREIGN_SCAN_LIMIT = ST.env_int("BLOOM_FOREIGN_SCAN_LIMIT", 40)
# Окно, в котором чужая сделка блокирует старт. Кошелёк исполнителя не с
# чистого листа: им уже торговали до нас, и вся та история по определению
# "не наша". Вопрос владельца был про ДРУГОЕ -- выключен ли TradeWiz, то
# есть нет ли активности СЕЙЧАС. Поэтому блокирует свежая активность, а
# прежняя история называется отдельной строкой и старт не держит.
FOREIGN_WINDOW_S = ST.env_float("BLOOM_FOREIGN_WINDOW_H", 24.0) * 3600.0


def метка_старта(state: ST.ExecState) -> dict:
    """Момент, с которого активность кошелька считается нашей заботой.

    Ставится ОДИН раз и только по слову владельца (--mark-start). Всё, что
    было до неё, -- прежняя жизнь кошелька: ручная торговля, чистка пустых
    токен-счетов, переводы себе. Всё, что после, -- уже опыт, и там чужая
    подпись означает, что ключом распоряжается кто-то ещё.

    Метка ограничивает только ПРОШЛОЕ. Бот, который торгует сейчас, будет
    поймана следующей же проверкой -- метка его не прячет.
    """
    путь = state.base / "stand_start.json"
    if not путь.exists():
        return {"set": False, "path": str(путь)}
    try:
        j = json.loads(путь.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"set": False, "path": str(путь),
                "why": f"метка не читается: {type(exc).__name__}"}
    ts = j.get("stand_start_ts")
    if not isinstance(ts, (int, float)):
        return {"set": False, "path": str(путь),
                "why": "в метке нет stand_start_ts"}
    return {"set": True, "path": str(путь), "stand_start_ts": float(ts),
            "stand_start_utc": j.get("stand_start_utc"),
            "note": j.get("note")}


def поставить_метку(state: ST.ExecState, *, note: str = "",
                    now: float | None = None) -> dict:
    now = time.time() if now is None else now
    j = {ST.SCHEMA_VERSION_KEY: ST.SCHEMA_VERSION,
         "stand_start_ts": now,
         "stand_start_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
         "note": note or "метка поставлена по слову владельца"}
    ST.atomic_write_json(state.base / "stand_start.json", j)
    return j


# Где в записях лежат подписи НАШИХ отправок. Ключей два, и второй
# добавлен по факту: сторож пишет подписи продажи в last_sell_signatures, и
# без него его собственные продажи сверка считала чужими сделками и
# блокировала следующий запуск.
КЛЮЧИ_ПОДПИСЕЙ = ("signatures", "last_sell_signatures")


def _подписи_из_записи(r: dict) -> set:
    out = set()
    for ключ in КЛЮЧИ_ПОДПИСЕЙ:
        for s in (r or {}).get(ключ) or []:
            if isinstance(s, str):
                out.add(s)
    return out


def _подписи_из_файла(путь: Path) -> set:
    out = set()
    try:
        текст = путь.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in текст.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        out |= _подписи_из_записи(r)
    return out


def our_signatures(state: ST.ExecState) -> set:
    """Подписи, которые мы считаем своими: всё, что писал исполнитель.

    Отложенные журналы -- тоже НАШИ записи. После откладывания текущий
    журнал пуст, и без их чтения вся прежняя наша активность выглядела бы
    чужой: ровно то различие, которое стенд обязан делать.
    """
    out = set()
    for p in state.positions().values():
        out |= _подписи_из_записи(p)
    for путь in sorted(state.base.glob(f"{state.positions_path.name}.*")):
        out |= _подписи_из_файла(путь)
    return out


def foreign_activity(helius, wallet: str, ours: set, *,
                     limit: int = FOREIGN_SCAN_LIMIT,
                     window_s: float = FOREIGN_WINDOW_S,
                     since_ts: float | None = None,
                     now: float | None = None) -> dict:
    """Сделки кошелька, которых нет среди наших, с разделением по времени.

    Ошибку RPC нельзя выдавать за отсутствие чужой активности: если узел
    не ответил, результат -- "неизвестно", и это блокирует старт так же,
    как найденная свежая чужая сделка.

    Сделка без blockTime попадает в свежие: неизвестное время трактуется в
    сторону осторожности, а не в сторону удобного ответа.
    """
    now = time.time() if now is None else now
    try:
        res = helius.call("getSignaturesForAddress",
                          [wallet, {"limit": limit}])
    except Exception as exc:  # noqa: BLE001
        return {"known": False,
                "why": f"getSignaturesForAddress не отдался: {type(exc).__name__}",
                "recent": [], "recent_count": None, "older_count": None,
                "checked": 0}
    строки = res or []
    порог = (now - window_s) if window_s else None
    # Метка начала стенда сдвигает границу ВПЕРЁД, но никогда назад: она
    # может только сузить прошлое, а не расширить окно доверия.
    откуда_порог = "окно" if порог is not None else "без границы"
    if since_ts is not None and (порог is None or since_ts > порог):
        порог, откуда_порог = since_ts, "метка начала стенда"
    свежие, прежние = [], []
    for r in строки:
        sig = (r or {}).get("signature")
        if not sig or sig in ours:
            continue
        t = (r or {}).get("blockTime")
        зап = {"signature": sig, "blockTime": t,
               "utc": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
                       if isinstance(t, (int, float)) else None),
               "err": (r or {}).get("err")}
        if порог is None or not isinstance(t, (int, float)) or t >= порог:
            свежие.append(зап)
        else:
            прежние.append(зап)
    return {"known": True, "checked": len(строки),
            "window_h": (round(window_s / 3600.0, 2) if window_s else None),
            "boundary_from": откуда_порог,
            "boundary_utc": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(порог))
                             if порог else None),
            "recent": свежие[:20], "recent_count": len(свежие),
            "older_count": len(прежние),
            "newest_older_utc": (прежние[0]["utc"] if прежние else None),
            "truncated": len(строки) >= limit,
            "foreign_count": len(свежие) + len(прежние)}


def объяснить_свежие(helius, свежие: list, минты: set, wallet: str, *,
                      предел: int = 10) -> dict:
    """Какие из "чужих" свежих сделок -- выходы Bloom по НАШИМ позициям.

    Зачем это нужно. Основной выход стенда -- таймерный авто-ордер Bloom,
    прикреплённый к покупке. Его транзакцию подписывает наш кошелёк, но её
    подпись нам не сообщают: ответ /swap отдаёт подписи только по своему
    вызову. Поэтому продажа по авто-ордеру выглядит для сверки чужой
    сделкой -- и блокирует следующий запуск на ровном месте.

    Правило узкое НАМЕРЕННО: объясняется только сделка, в которой упал
    остаток минта НАШЕЙ позиции. Всё остальное остаётся чужим: списывать
    незнакомую активность на "наверное Bloom" -- это ровно тот подлог,
    из-за которого сверка и существует.
    """
    import bloom_detector as BD  # noqa: PLC0415
    объяснённые, необъяснённые, сбои = [], [], []
    for зап in свежие[:предел]:
        sig = зап.get("signature")
        try:
            tx = helius.транзакция(sig)
        except Exception as exc:  # noqa: BLE001
            сбои.append({"signature": sig, "why_not": f"{type(exc).__name__}"})
            необъяснённые.append(зап)
            continue
        if not tx:
            сбои.append({"signature": sig, "why_not": "узел не отдал транзакцию"})
            необъяснённые.append(зап)
            continue
        б = BD.балансы_кошелька(tx, wallet)
        упали = sorted({м for м, з in (б.get("by_mint") or {}).items()
                        if м in минты and (з.get("delta_raw") or 0) < 0})
        ошибка = ((tx.get("meta") or {}).get("err") is not None)
        ключи = BD._баланс_ключи(tx)
        платили_мы = bool(ключи) and ключи[0] == wallet
        наши_минты_в_tx = sorted({м for м in (б.get("by_mint") or {}) if м in минты})
        if упали:
            объяснённые.append({**зап, "mints": упали,
                                 "why": "выход по нашей позиции: остаток нашего "
                                        "минта уменьшился, подпись авто-ордера "
                                        "Bloom нам не сообщается"})
        elif ошибка and платили_мы and наши_минты_в_tx:
            # Упавшая транзакция баланс не меняет -- и по одному признаку
            # "остаток уменьшился" её не объяснить. Но это НЕУДАЧНАЯ ПОПЫТКА
            # ВЫХОДА по нашей позиции: платил наш кошелёк, и в счетах наш
            # минт. Ровно так упал авто-ордер по п. 1 стенда
            # (assertion failed: liquidity > 0), и он блокировал следующий
            # запуск как чужая сделка.
            объяснённые.append({**зап, "mints": наши_минты_в_tx,
                                 "why": "неудачная попытка выхода по нашей позиции: "
                                        "транзакция с ошибкой, платил наш кошелёк, "
                                        "в счетах наш минт"})
        else:
            необъяснённые.append(зап)
    if len(свежие) > предел:
        необъяснённые.extend(свежие[предел:])
    return {"explained": объяснённые, "unexplained": необъяснённые,
             "failures": сбои, "checked": min(len(свежие), предел),
             "limit": предел}


def mixed_journal(state: ST.ExecState) -> dict:
    """Есть ли в журнале записи прежнего формата рядом с новыми."""
    итог = {"decisions": {"v1": 0, "v2": 0, "unreadable": 0},
            "positions": {"v1": 0, "v2": 0, "unreadable": 0}}
    for имя, путь in (("decisions", state.decisions_path),
                      ("positions", state.positions_path)):
        if not путь.exists():
            continue
        for line in путь.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                итог[имя]["unreadable"] += 1
                continue
            v = r.get(ST.SCHEMA_VERSION_KEY)
            итог[имя]["v2" if v == ST.SCHEMA_VERSION else "v1"] += 1
    итог["mixed"] = any(v["v1"] and v["v2"] for v in
                        (итог["decisions"], итог["positions"]))
    return итог


def archive_dry(state: ST.ExecState, *, stamp: str | None = None) -> dict:
    """Отложить журналы с позициями dry-run. Ничего не удаляет.

    Откладывается ФАЙЛ ЦЕЛИКОМ, а не отдельные записи: журнал append-only,
    и вырезать из него строки значило бы переписать историю.
    """
    stamp = stamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    отложено = []
    for путь in (state.positions_path, state.decisions_path):
        if путь.exists() and путь.stat().st_size > 0:
            цель = путь.with_name(f"{путь.name}.dry.{stamp}")
            путь.replace(цель)
            отложено.append(str(цель))
    return {"stamp": stamp, "archived": отложено}


def reconcile(state: ST.ExecState, *, mode: str, helius=None,
              min_balance_sol: float = MIN_BALANCE_SOL,
              balance_sol: float | None = None,
              allow_open_live_test: bool = False) -> dict:
    """Сводка готовности. Ничего не меняет."""
    dry = state.dry_positions()
    real = state.open_positions()
    убит, почему_убит = state.kill_active()
    читается, почему_читается = state.kill_readable()

    ours = our_signatures(state)
    метка = метка_старта(state)
    чужая = {"known": None, "why": "проверка не запрашивалась"}
    if helius is not None:
        if balance_sol is None:
            balance_sol = helius.баланс_sol(ST.EXECUTOR_WALLET)
        метка = метка_старта(state)
        чужая = foreign_activity(helius, ST.EXECUTOR_WALLET, ours,
                                 since_ts=(метка.get("stand_start_ts")
                                           if метка.get("set") else None))
        if чужая.get("recent"):
            минты_наши = {p.get("mint") for p in state.positions().values()
                          if p.get("mint")}
            разбор = объяснить_свежие(helius, чужая["recent"], минты_наши,
                                       ST.EXECUTOR_WALLET)
            чужая["explained"] = разбор["explained"]
            чужая["unexplained"] = разбор["unexplained"]
            чужая["explain_failures"] = разбор["failures"]
            чужая["unexplained_count"] = len(разбор["unexplained"])

    журнал = mixed_journal(state)

    блокеры = []
    if mode in ST.MODES_REAL and dry:
        блокеры.append(f"в журнале {len(dry)} позиций dry-run -- отложить перед стендом")
    заметки = []
    только_стенд = bool(real) and all(p.get("mode") == ST.MODE_LIVE_TEST for p in real)
    if real and allow_open_live_test and только_стенд:
        # Исключение разрешено владельцем ПРЯМО и только для стенда: позиция
        # стенда стоит открытой именно потому, что её не удалось продать, и
        # ждать её закрытия -- значит не ставить правку, которая её закрывает.
        заметки.append(f"открыто {len(real)} позиций стенда (live-test) -- старт "
                        "разрешён исключением владельца, боевых позиций нет")
    elif real:
        блокеры.append(f"открыто {len(real)} настоящих позиций -- сначала должны закрыться")
    if not читается:
        блокеры.append(f"рубильник не читается службой: {почему_читается}")
    if убит:
        блокеры.append(f"рубильник включён: {почему_убит}")
    if чужая.get("known") is False:
        блокеры.append(f"чужая активность НЕИЗВЕСТНА: {чужая.get('why')}")
    elif чужая.get("unexplained_count") is not None:
        if чужая["unexplained_count"]:
            блокеры.append(f"на кошельке {чужая['unexplained_count']} сделок за "
                            f"последние {чужая.get('window_h')} ч, которых мы не "
                            "делали и объяснить не смогли -- мы в кошельке не одни")
        if чужая.get("explained"):
            заметки.append(f"{len(чужая['explained'])} свежих сделок объяснены как "
                            "выходы по нашим позициям (авто-ордер Bloom, его подпись "
                            "в ответе /swap не приходит): "
                            + ", ".join(f"{x['signature'][:12]} ({','.join(x['mints'])})"
                                        for x in чужая["explained"]))
        if чужая.get("explain_failures"):
            заметки.append(f"{len(чужая['explain_failures'])} свежих сделок разобрать "
                            "не удалось -- они считаются чужими")
    elif чужая.get("recent_count"):
        блокеры.append(f"на кошельке {чужая['recent_count']} сделок за последние "
                       f"{чужая.get('window_h')} ч, которых мы не делали -- "
                       "мы в кошельке не одни")
    if not метка.get("set"):
        заметки.append("метка начала стенда не поставлена: границей служит окно "
                       f"{round(FOREIGN_WINDOW_S / 3600.0, 2)} ч. Поставить "
                       "метку -- bloom_reconcile.py --mark-start, и только по "
                       "слову владельца: она объявляет всю прежнюю активность "
                       "кошелька его прежней жизнью")
    if чужая.get("older_count"):
        заметки.append(f"у кошелька есть прежняя история: {чужая['older_count']} "
                       f"сделок старше окна, самая свежая из них "
                       f"{чужая.get('newest_older_utc')}. Старт не блокирует, но "
                       "кошелёк не с чистого листа, и приписывать себе весь его "
                       "результат нельзя")
    if чужая.get("truncated"):
        заметки.append(f"список подписей уперся в лимит {FOREIGN_SCAN_LIMIT}: видно "
                       "не всю историю кошелька, только последние сделки")
    if balance_sol is None:
        блокеры.append("баланс кошелька неизвестен")
    elif balance_sol < min_balance_sol:
        блокеры.append(f"баланс {balance_sol:.4f} SOL ниже порога {min_balance_sol}")
    if журнал["mixed"]:
        блокеры.append("журнал смешан из записей прежнего и нового формата")

    return {ST.SCHEMA_VERSION_KEY: ST.SCHEMA_VERSION,
            "checked_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "mode": mode,
            "wallet": ST.EXECUTOR_WALLET,
            "balance_sol": balance_sol,
            "min_balance_sol": min_balance_sol,
            "dry_positions": len(dry),
            "real_open_positions": len(real),
            "real_open_mints": sorted({p.get("mint") for p in real if p.get("mint")}),
            "kill_readable": читается,
            "kill_active": убит,
            "foreign_activity": чужая,
            "stand_start": метка,
            "journal": журнал,
            "notes": заметки,
            "blockers": блокеры,
            "clean": not блокеры,
            "verdict": ("сверка чистая: настоящие покупки включать можно"
                        if not блокеры else
                        f"старт заблокирован, причин {len(блокеры)}")}


def self_test() -> int:
    import tempfile  # noqa: PLC0415
    checks = []

    def chk(имя, ок, факт=""):
        checks.append((имя, bool(ок), факт))

    class HeliusЗаглушка:
        def __init__(self, подписи, баланс=1.0, падать=False):
            self.подписи = подписи
            self.баланс = баланс
            self.падать = падать

        def call(self, метод, параметры, **kw):
            if self.падать:
                raise RuntimeError("узел молчит")
            return self.подписи

        def баланс_sol(self, адрес):
            return self.баланс

    def состояние(d, имя="s"):
        return ST.ExecState(base=Path(d) / имя, kill=Path(d) / f"k{имя}")

    # чистое состояние
    with tempfile.TemporaryDirectory() as d:
        st = состояние(d)
        h = HeliusЗаглушка([], баланс=0.35)
        r = reconcile(st, mode=ST.MODE_LIVE_TEST, helius=h)
        chk("пустое состояние -- сверка чистая", r["clean"], r["blockers"])
        chk("баланс взят с цепи", r["balance_sol"] == 0.35, r["balance_sol"])
        chk("порог по умолчанию 0.3", r["min_balance_sol"] == 0.3, r["min_balance_sol"])

    # баланс ниже порога
    with tempfile.TemporaryDirectory() as d:
        st = состояние(d)
        r = reconcile(st, mode=ST.MODE_LIVE_TEST, helius=HeliusЗаглушка([], баланс=0.1))
        chk("баланс 0.1 блокирует старт", not r["clean"])
        chk("и причина названа числом",
            any("0.1000" in b for b in r["blockers"]), r["blockers"])

    # позиции dry-run блокируют стенд, но НЕ блокируют сам dry-run
    with tempfile.TemporaryDirectory() as d:
        st = состояние(d)
        st.write_intent(client_order_id="d1", mint="M", source_sig="S",
                        source_slot=1, sol_in=0.2, pool=None, program=None,
                        taxed=None, tax_bps=None, mode=ST.MODE_DRY, sell_after_s=28.8)
        r = reconcile(st, mode=ST.MODE_LIVE_TEST, helius=HeliusЗаглушка([], баланс=0.35))
        chk("позиции dry-run блокируют переход на стенд", not r["clean"])
        chk("и сказано, что их надо отложить",
            any("отложить" in b for b in r["blockers"]), r["blockers"])
        r2 = reconcile(st, mode=ST.MODE_DRY, helius=HeliusЗаглушка([], баланс=0.35))
        chk("в самом dry-run они помехой не считаются", r2["clean"], r2["blockers"])
        chk("но их число показано", r2["dry_positions"] == 1, r2["dry_positions"])

    # откладывание ничего не удаляет
    with tempfile.TemporaryDirectory() as d:
        st = состояние(d)
        st.write_intent(client_order_id="d1", mint="M", source_sig="S",
                        source_slot=1, sol_in=0.2, pool=None, program=None,
                        taxed=None, tax_bps=None, mode=ST.MODE_DRY, sell_after_s=28.8)
        st.log_decision({"action": "buy", "mint": "M"})
        итог = archive_dry(st)
        chk("отложено два файла", len(итог["archived"]) == 2, итог)
        chk("исходные журналы пусты", not st.positions_path.exists()
            and not st.decisions_path.exists())
        chk("отложенные файлы на месте и не пусты",
            all(Path(x).exists() and Path(x).stat().st_size > 0 for x in итог["archived"]))
        r = reconcile(st, mode=ST.MODE_LIVE_TEST, helius=HeliusЗаглушка([], баланс=0.35))
        chk("после откладывания сверка чистая", r["clean"], r["blockers"])

    # настоящая открытая позиция блокирует в любом режиме
    with tempfile.TemporaryDirectory() as d:
        st = состояние(d)
        st.write_intent(client_order_id="lt", mint="LT", source_sig="LTS",
                        source_slot=1, sol_in=0.01, pool=None, program=None,
                        taxed=None, tax_bps=None, mode=ST.MODE_LIVE_TEST,
                        sell_after_s=28.8)
        r = reconcile(st, mode=ST.MODE_DRY, helius=HeliusЗаглушка([], баланс=0.35))
        chk("открытая настоящая позиция блокирует старт", not r["clean"])
        chk("минт открытой позиции назван", r["real_open_mints"] == ["LT"],
            r["real_open_mints"])

    # чужая активность: свежая блокирует, прежняя история -- нет
    сейчас = time.time()
    with tempfile.TemporaryDirectory() as d:
        st = состояние(d)
        st.write_intent(client_order_id="c", mint="M", source_sig="S", source_slot=1,
                        sol_in=0.01, pool=None, program=None, taxed=None,
                        tax_bps=None, mode=ST.MODE_LIVE_TEST, sell_after_s=28.8)
        st.update_position("c", state=ST.STATE_CLOSED, signatures=["НАША"])
        h = HeliusЗаглушка([{"signature": "НАША", "blockTime": int(сейчас - 60)},
                            {"signature": "ЧУЖАЯ", "blockTime": int(сейчас - 60)}],
                           баланс=0.35)
        r = reconcile(st, mode=ST.MODE_LIVE_TEST, helius=h)
        chk("свежая чужая подпись найдена",
            r["foreign_activity"]["recent_count"] == 1, r["foreign_activity"])
        chk("наша подпись чужой не считается",
            [x["signature"] for x in r["foreign_activity"]["recent"]] == ["ЧУЖАЯ"])
        chk("свежая чужая активность блокирует старт", not r["clean"], r["blockers"])
        chk("и в блокере названо окно",
            any("за последние" in b for b in r["blockers"]), r["blockers"])

    # выход по авто-ордеру Bloom: подпись чужая, сделка наша
    with tempfile.TemporaryDirectory() as d:
        st = состояние(d)
        st.write_intent(client_order_id="ao", mint="MINTX", source_sig="S",
                        source_slot=1, sol_in=0.001, pool=None, program=None,
                        taxed=None, tax_bps=None, mode=ST.MODE_LIVE_TEST,
                        sell_after_s=28.8)
        st.update_position("ao", state="bought", signatures=["НАША_ПОКУПКА"])

        class HeliusСПродажей(HeliusЗаглушка):
            def транзакция(self, подпись, **kw):
                # остаток НАШЕГО минта уменьшился -- это выход по позиции
                бал = lambda raw, idx: {  # noqa: E731
                    "accountIndex": idx, "mint": "MINTX",
                    "owner": ST.EXECUTOR_WALLET,
                    "programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                    "uiTokenAmount": {"amount": str(raw), "decimals": 6,
                                       "uiAmount": raw / 1e6}}
                return {"slot": 5,
                        "transaction": {"message": {
                            "accountKeys": [{"pubkey": ST.EXECUTOR_WALLET}],
                            "instructions": []}},
                        "meta": {"err": None, "fee": 5000,
                                  "preBalances": [10 ** 9], "postBalances": [10 ** 9],
                                  "preTokenBalances": [бал(92_000000, 1)],
                                  "postTokenBalances": [бал(0, 1)],
                                  "innerInstructions": []}}

        h = HeliusСПродажей([{"signature": "ПРОДАЖА_БЛУМА",
                              "blockTime": int(сейчас - 60)}], баланс=0.35)
        r = reconcile(st, mode=ST.MODE_LIVE_TEST, helius=h,
                      allow_open_live_test=True)
        ч = r["foreign_activity"]
        chk("выход по нашей позиции объяснён, а не записан в чужие",
            ч.get("unexplained_count") == 0 and len(ч.get("explained") or []) == 1, ч)
        chk("и объяснение названо в примечаниях",
            any("авто-ордер Bloom" in n for n in r["notes"]), r["notes"])
        chk("сверка при этом чистая", r["clean"], r["blockers"])

        class HeliusЧужая(HeliusСПродажей):
            def транзакция(self, подпись, **kw):
                tx = HeliusСПродажей.транзакция(self, подпись, **kw)
                for где in ("preTokenBalances", "postTokenBalances"):
                    for b in tx["meta"][где]:
                        b["mint"] = "НЕ_НАШ"
                return tx

        h2 = HeliusЧужая([{"signature": "ЧУЖАЯ_СДЕЛКА",
                           "blockTime": int(сейчас - 60)}], баланс=0.35)
        r2 = reconcile(st, mode=ST.MODE_LIVE_TEST, helius=h2,
                       allow_open_live_test=True)
        chk("сделка по чужому минту объяснением не считается",
            not r2["clean"] and r2["foreign_activity"]["unexplained_count"] == 1,
            r2["foreign_activity"])

    # подписи продаж сторожа -- наши, и упавшая попытка выхода -- тоже наша
    with tempfile.TemporaryDirectory() as d:
        st = состояние(d)
        st.write_intent(client_order_id="sp", mint="MINTX", source_sig="S",
                        source_slot=1, sol_in=0.001, pool=None, program=None,
                        taxed=None, tax_bps=None, mode=ST.MODE_LIVE_TEST,
                        sell_after_s=28.8)
        st.update_position("sp", state="selling", signatures=["НАША_ПОКУПКА"],
                           last_sell_signatures=["НАША_ПРОДАЖА"])
        chk("подпись продажи сторожа считается нашей",
            {"НАША_ПОКУПКА", "НАША_ПРОДАЖА"} <= our_signatures(st),
            sorted(our_signatures(st)))

        class HeliusУпавшая(HeliusЗаглушка):
            def транзакция(self, подпись, **kw):
                бал = {"accountIndex": 1, "mint": "MINTX",
                       "owner": ST.EXECUTOR_WALLET,
                       "programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                       "uiTokenAmount": {"amount": "92000000", "decimals": 6,
                                          "uiAmount": 92.0}}
                return {"slot": 7,
                        "transaction": {"message": {
                            "accountKeys": [{"pubkey": ST.EXECUTOR_WALLET}],
                            "instructions": []}},
                        "meta": {"err": {"InstructionError": [4, "ProgramFailedToComplete"]},
                                  "fee": 5000,
                                  "preBalances": [10 ** 9], "postBalances": [10 ** 9],
                                  "preTokenBalances": [бал], "postTokenBalances": [бал],
                                  "innerInstructions": []}}

        h = HeliusУпавшая([{"signature": "УПАВШИЙ_ВЫХОД",
                            "blockTime": int(сейчас - 60)}], баланс=0.35)
        r = reconcile(st, mode=ST.MODE_LIVE_TEST, helius=h, allow_open_live_test=True)
        chk("упавшая попытка выхода по нашему минту объяснена",
            r["foreign_activity"]["unexplained_count"] == 0
            and any("неудачная попытка выхода" in x["why"]
                    for x in r["foreign_activity"]["explained"]),
            r["foreign_activity"])

        class HeliusЧужойПлательщик(HeliusУпавшая):
            def транзакция(self, подпись, **kw):
                tx = HeliusУпавшая.транзакция(self, подпись, **kw)
                tx["transaction"]["message"]["accountKeys"] = [{"pubkey": "ЧУЖОЙ"}]
                return tx

        h2 = HeliusЧужойПлательщик([{"signature": "ЧУЖОЙ_СБОЙ",
                                     "blockTime": int(сейчас - 60)}], баланс=0.35)
        r2 = reconcile(st, mode=ST.MODE_LIVE_TEST, helius=h2, allow_open_live_test=True)
        chk("упавшая транзакция, которую платил не наш кошелёк, остаётся чужой",
            r2["foreign_activity"]["unexplained_count"] == 1, r2["foreign_activity"])

    # открытая позиция стенда: блокер по умолчанию, исключение -- по слову владельца
    with tempfile.TemporaryDirectory() as d:
        st = состояние(d)
        st.write_intent(client_order_id="lt", mint="M", source_sig="S", source_slot=1,
                        sol_in=0.001, pool=None, program=None, taxed=None,
                        tax_bps=None, mode=ST.MODE_LIVE_TEST, sell_after_s=28.8)
        st.update_position("lt", state="bought")
        h = HeliusЗаглушка([], баланс=0.35)
        r = reconcile(st, mode=ST.MODE_LIVE_TEST, helius=h)
        chk("по умолчанию открытая позиция стенда блокирует старт", not r["clean"],
            r["blockers"])
        r2 = reconcile(st, mode=ST.MODE_LIVE_TEST, helius=h, allow_open_live_test=True)
        chk("с разрешением владельца -- не блокирует", r2["clean"], r2["blockers"])
        chk("и сказано, что это исключение, а не норма",
            any("исключением владельца" in n for n in r2["notes"]), r2["notes"])
        st.write_intent(client_order_id="lv", mint="M2", source_sig="S2", source_slot=2,
                        sol_in=0.2, pool=None, program=None, taxed=None,
                        tax_bps=None, mode=ST.MODE_LIVE, sell_after_s=28.8)
        st.update_position("lv", state="bought")
        r3 = reconcile(st, mode=ST.MODE_LIVE_TEST, helius=h, allow_open_live_test=True)
        chk("боевую позицию исключение НЕ покрывает", not r3["clean"], r3["blockers"])

    # прежняя история кошелька старт не держит, но названа вслух
    with tempfile.TemporaryDirectory() as d:
        st = состояние(d)
        h = HeliusЗаглушка([{"signature": "СТАРАЯ",
                             "blockTime": int(сейчас - 53.6 * 3600)}], баланс=0.35)
        r = reconcile(st, mode=ST.MODE_LIVE_TEST, helius=h)
        chk("прежняя история не блокирует старт", r["clean"], r["blockers"])
        chk("прежняя история посчитана отдельно",
            r["foreign_activity"]["older_count"] == 1 and
            r["foreign_activity"]["recent_count"] == 0, r["foreign_activity"])
        chk("и о ней есть примечание",
            any("прежняя история" in n for n in r["notes"]), r["notes"])
        chk("время самой свежей из прежних названо",
            r["foreign_activity"]["newest_older_utc"] is not None,
            r["foreign_activity"])

    # сделка без времени считается свежей: осторожность важнее удобства
    with tempfile.TemporaryDirectory() as d:
        st = состояние(d)
        h = HeliusЗаглушка([{"signature": "БЕЗ_ВРЕМЕНИ", "blockTime": None}],
                           баланс=0.35)
        r = reconcile(st, mode=ST.MODE_LIVE_TEST, helius=h)
        chk("сделка без времени блокирует старт", not r["clean"], r["blockers"])

    # отложенный журнал -- это НАШИ подписи, а не чужие
    with tempfile.TemporaryDirectory() as d:
        st = состояние(d)
        st.write_intent(client_order_id="a", mint="M", source_sig="S", source_slot=1,
                        sol_in=0.01, pool=None, program=None, taxed=None,
                        tax_bps=None, mode=ST.MODE_LIVE_TEST, sell_after_s=28.8)
        st.update_position("a", state=ST.STATE_CLOSED, signatures=["БЫЛА_НАША"])
        archive_dry(st)
        h = HeliusЗаглушка([{"signature": "БЫЛА_НАША",
                             "blockTime": int(сейчас - 60)}], баланс=0.35)
        r = reconcile(st, mode=ST.MODE_LIVE_TEST, helius=h)
        chk("подпись из отложенного журнала своя, а не чужая",
            r["foreign_activity"]["recent_count"] == 0, r["foreign_activity"])
        chk("и старт не заблокирован", r["clean"], r["blockers"])

    # метка начала стенда объявляет прежнюю активность прежней жизнью
    with tempfile.TemporaryDirectory() as d:
        st = состояние(d)
        h = HeliusЗаглушка([{"signature": "ДО_МЕТКИ",
                             "blockTime": int(сейчас - 600)}], баланс=0.35)
        r = reconcile(st, mode=ST.MODE_LIVE_TEST, helius=h)
        chk("без метки свежая подпись блокирует", not r["clean"], r["blockers"])
        chk("и в примечаниях сказано, как поставить метку",
            any("--mark-start" in n for n in r["notes"]), r["notes"])
        поставить_метку(st, note="проверка")
        r2 = reconcile(st, mode=ST.MODE_LIVE_TEST, helius=h)
        chk("после метки прежняя подпись старт не держит", r2["clean"], r2["blockers"])
        chk("границей названа метка, а не окно",
            r2["foreign_activity"]["boundary_from"] == "метка начала стенда",
            r2["foreign_activity"]["boundary_from"])
        chk("метка видна в сводке",
            r2["stand_start"]["set"] is True, r2["stand_start"])
        # Метка не прячет того, кто торгует СЕЙЧАС.
        h2 = HeliusЗаглушка([{"signature": "ПОСЛЕ_МЕТКИ",
                              "blockTime": int(сейчас + 120)}], баланс=0.35)
        r3 = reconcile(st, mode=ST.MODE_LIVE_TEST, helius=h2)
        chk("подпись после метки блокирует старт", not r3["clean"], r3["blockers"])

    # битая метка -- это отсутствие метки, а не доверие ко всему прошлому
    with tempfile.TemporaryDirectory() as d:
        st = состояние(d)
        (st.base / "stand_start.json").write_text("{не json", encoding="utf-8")
        м = метка_старта(st)
        chk("нечитаемая метка не считается поставленной", м["set"] is False, м)
        chk("и причина названа", "не читается" in (м.get("why") or ""), м)

    # отказ узла -- это НЕ "чужой активности нет"
    with tempfile.TemporaryDirectory() as d:
        st = состояние(d)
        r = reconcile(st, mode=ST.MODE_LIVE_TEST,
                      helius=HeliusЗаглушка([], баланс=0.35, падать=True))
        chk("отказ узла не выдаётся за чистоту", not r["clean"], r["blockers"])
        chk("и назван неизвестностью, а не отсутствием",
            r["foreign_activity"]["known"] is False, r["foreign_activity"])
        chk("в блокерах это тоже сказано словом НЕИЗВЕСТНА",
            any("НЕИЗВЕСТНА" in b for b in r["blockers"]), r["blockers"])

    # смешанный журнал
    with tempfile.TemporaryDirectory() as d:
        st = состояние(d)
        st.log_decision({"action": "skip"})                    # v2
        with st.decisions_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"код": "СТАРЫЙ"}, ensure_ascii=False) + "\n")
        r = reconcile(st, mode=ST.MODE_DRY, helius=HeliusЗаглушка([], баланс=0.35))
        chk("смешанный журнал замечен", r["journal"]["mixed"] is True, r["journal"])
        chk("и он блокирует старт", not r["clean"], r["blockers"])
        chk("записи посчитаны по версиям",
            r["journal"]["decisions"]["v1"] == 1 and r["journal"]["decisions"]["v2"] == 1,
            r["journal"]["decisions"])

    # рубильник
    with tempfile.TemporaryDirectory() as d:
        st = состояние(d)
        st.kill_path.write_text("стоп", encoding="utf-8")
        r = reconcile(st, mode=ST.MODE_DRY, helius=HeliusЗаглушка([], баланс=0.35))
        chk("включённый рубильник блокирует старт", not r["clean"])
        chk("и это видно отдельным полем", r["kill_active"] is True)

    chk("все ключи сводки латинские",
        all(k.isascii() for k in reconcile(
            ST.ExecState(base=Path(tempfile.mkdtemp()) / "s",
                         kill=Path(tempfile.mkdtemp()) / "k"),
            mode=ST.MODE_DRY)))

    прошло = sum(1 for _, ок, _ in checks if ок)
    for имя, ок, факт in checks:
        print(f"  [{'ok  ' if ок else 'ПЛОХО'}] {имя}" + (f" -- {факт}" if not ок else ""))
    print(f"самопроверка сверки при старте: {прошло}/{len(checks)} пройдено")
    return 0 if прошло == len(checks) else 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--archive", action="store_true",
                   help="отложить журналы с позициями dry-run (ничего не удаляет)")
    p.add_argument("--mode", default=None,
                   help="режим, для которого проверяем готовность")
    p.add_argument("--mark-start", action="store_true",
                   help="поставить метку начала стенда (только по слову владельца)")
    p.add_argument("--allow-open-live-test", action="store_true",
                   help="не блокировать старт открытыми позициями стенда "
                        "(только по слову владельца; боевые позиции не покрывает)")
    p.add_argument("--note", default="",
                   help="пояснение к метке начала стенда")
    a = p.parse_args()
    if a.self_test:
        return self_test()

    import bloom_detector as BD  # noqa: PLC0415
    import bloom_executor as EX  # noqa: PLC0415

    state = ST.ExecState()
    mode = a.mode or EX.current_mode()
    helius = BD.Helius(служба="")

    if a.archive:
        итог = archive_dry(state)
        print(json.dumps({"archived": итог}, ensure_ascii=False, indent=2))

    if a.mark_start:
        м = поставить_метку(state, note=a.note)
        print(json.dumps({"stand_start": м}, ensure_ascii=False, indent=2))

    сводка = reconcile(state, mode=mode, helius=helius,
                        allow_open_live_test=a.allow_open_live_test)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ST.atomic_write_json(OUT_PATH, сводка)
    ST.atomic_write_json(state.base / "reconcile.json", сводка)
    print(json.dumps(сводка, ensure_ascii=False, indent=2))
    # Короткая сводка идёт ПОСЛЕ полного JSON: шаг деплоя оставляет от
    # вывода только хвост, и один раз это уже скрыло ровно те данные, по
    # которым принималось решение.
    ч = сводка["foreign_activity"]
    print("--- коротко ---")
    print(f"режим: {сводка['mode']}, вердикт: {сводка['verdict']}")
    print(f"кошелёк {сводка['wallet']}: баланс {сводка['balance_sol']} SOL "
          f"при пороге {сводка['min_balance_sol']}")
    print(f"чужая активность: известно={ч.get('known')}, свежих за "
          f"{ч.get('window_h')} ч {ч.get('recent_count')}, "
          f"прежних {ч.get('older_count')} (самая свежая "
          f"{ч.get('newest_older_utc')})")
    for x in (ч.get("recent") or [])[:5]:
        print(f"    свежая чужая: {x['signature']} {x['utc']}")
    print(f"граница чужой активности: {ч.get('boundary_from')} "
          f"({ч.get('boundary_utc')})")
    print(f"позиции: dry-run {сводка['dry_positions']}, "
          f"настоящих открытых {сводка['real_open_positions']}")
    for b in сводка["blockers"]:
        print(f"  БЛОКЕР: {b}")
    for n in сводка.get("notes") or []:
        print(f"  примечание: {n}")
    return 0 if сводка["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
