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


def our_signatures(state: ST.ExecState) -> set:
    """Подписи, которые мы считаем своими: всё, что писал исполнитель."""
    out = set()
    for p in state.positions().values():
        for s in p.get("signatures") or []:
            if isinstance(s, str):
                out.add(s)
    return out


def foreign_activity(helius, wallet: str, ours: set, *,
                     limit: int = FOREIGN_SCAN_LIMIT,
                     since_ts: float | None = None) -> dict:
    """Сделки кошелька, которых нет среди наших.

    Ошибку RPC нельзя выдавать за отсутствие чужой активности: если узел
    не ответил, результат -- "неизвестно", и это блокирует старт так же,
    как найденная чужая активность.
    """
    try:
        res = helius.call("getSignaturesForAddress",
                          [wallet, {"limit": limit}])
    except Exception as exc:  # noqa: BLE001
        return {"known": False,
                "why": f"getSignaturesForAddress не отдался: {type(exc).__name__}",
                "foreign": [], "checked": 0}
    строки = res or []
    чужие = []
    for r in строки:
        sig = (r or {}).get("signature")
        if not sig or sig in ours:
            continue
        t = (r or {}).get("blockTime")
        if since_ts is not None and t is not None and t < since_ts:
            continue
        чужие.append({"signature": sig, "blockTime": t, "err": (r or {}).get("err")})
    return {"known": True, "checked": len(строки), "foreign": чужие,
            "foreign_count": len(чужие)}


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
              balance_sol: float | None = None) -> dict:
    """Сводка готовности. Ничего не меняет."""
    dry = state.dry_positions()
    real = state.open_positions()
    убит, почему_убит = state.kill_active()
    читается, почему_читается = state.kill_readable()

    ours = our_signatures(state)
    чужая = {"known": None, "why": "проверка не запрашивалась"}
    if helius is not None:
        if balance_sol is None:
            balance_sol = helius.баланс_sol(ST.EXECUTOR_WALLET)
        чужая = foreign_activity(helius, ST.EXECUTOR_WALLET, ours)

    журнал = mixed_journal(state)

    блокеры = []
    if mode in ST.MODES_REAL and dry:
        блокеры.append(f"в журнале {len(dry)} позиций dry-run -- отложить перед стендом")
    if real:
        блокеры.append(f"открыто {len(real)} настоящих позиций -- сначала должны закрыться")
    if not читается:
        блокеры.append(f"рубильник не читается службой: {почему_читается}")
    if убит:
        блокеры.append(f"рубильник включён: {почему_убит}")
    if чужая.get("known") is False:
        блокеры.append(f"чужая активность НЕИЗВЕСТНА: {чужая.get('why')}")
    elif чужая.get("foreign_count"):
        блокеры.append(f"на кошельке {чужая['foreign_count']} сделок, которых мы "
                       f"не делали -- мы в кошельке не одни")
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
            "journal": журнал,
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

    # чужая активность
    with tempfile.TemporaryDirectory() as d:
        st = состояние(d)
        st.write_intent(client_order_id="c", mint="M", source_sig="S", source_slot=1,
                        sol_in=0.01, pool=None, program=None, taxed=None,
                        tax_bps=None, mode=ST.MODE_LIVE_TEST, sell_after_s=28.8)
        st.update_position("c", state=ST.STATE_CLOSED, signatures=["НАША"])
        h = HeliusЗаглушка([{"signature": "НАША", "blockTime": 1},
                            {"signature": "ЧУЖАЯ", "blockTime": 2}], баланс=0.35)
        r = reconcile(st, mode=ST.MODE_LIVE_TEST, helius=h)
        chk("чужая подпись найдена", r["foreign_activity"]["foreign_count"] == 1,
            r["foreign_activity"])
        chk("наша подпись чужой не считается",
            [x["signature"] for x in r["foreign_activity"]["foreign"]] == ["ЧУЖАЯ"])
        chk("чужая активность блокирует старт", not r["clean"], r["blockers"])

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

    сводка = reconcile(state, mode=mode, helius=helius)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ST.atomic_write_json(OUT_PATH, сводка)
    print(json.dumps(сводка, ensure_ascii=False, indent=2))
    return 0 if сводка["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
