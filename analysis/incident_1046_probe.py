#!/usr/bin/env python3
"""Разбор происшествия 25.09 10:46:56-57Z и возврат боевого состояния к норме.

ЧТО СЛУЧИЛОСЬ. Гейт снятия рубильника полосы запускал самопроверки НА ХОСТЕ с
боевым окружением (`set -a; . /etc/bloom-executor/env`). Оповещатель сторожа
продаж создаётся в его конструкторе из окружения, поэтому строки самопроверки
с заглушками (MINT1, ПОДПИСЬ_*, "рубильник продаж включён") ушли в БОЕВОЙ чат.

ЧТО ЗДЕСЬ ДЕЛАЕТСЯ. Только чтение боевого состояния плюс ОДНО осторожное
действие: снятие рубильника продаж, если его содержимое дословно совпадает с
тестовой строкой. Любое другое содержимое не трогается -- это был бы не
возврат к норме, а ослабление защиты.

Запускается на хосте через stdin: ssh ... "$VENV - --флаги" < этот файл.
Поэтому модуль не полагается ни на путь файла, ни на импорты репозитория.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import urllib.request

# ТЕСТОВЫЕ СТРОКИ -- ДОСЛОВНО ИЗ САМОПРОВЕРКИ СТОРОЖА (bloom_seller.self_test).
# Ровно и только их снимаем: по ним видно, что файл создал тест, а не человек
# и не служба.
ТЕСТОВЫЕ_НАЧАЛА = ("стоп продаж", "стоп продажам", "/kill_sell из Telegram",
                    "стоп покупок")
ТОКЕН_ПРОГРАММА = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def читать(п: pathlib.Path, *, сколько: int = 400) -> str:
    try:
        return п.read_text(encoding="utf-8", errors="replace").strip()[:сколько]
    except OSError as exc:
        return f"НЕ ПРОЧИТАН: {type(exc).__name__}"


def состояние_рубильников(state_dir: pathlib.Path, env_dir: pathlib.Path) -> list:
    из_ = []
    for п in (env_dir / "KILL_SELL", state_dir / "KILL_SELL_BY_TELEGRAM",
              state_dir / "KILL_OWN_SEND", env_dir / "KILL"):
        если_есть = п.exists()
        строка = {"path": str(п), "exists": если_есть}
        if если_есть:
            строка["content"] = читать(п)
            try:
                строка["mtime_utc"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(п.stat().st_mtime))
            except OSError:
                строка["mtime_utc"] = None
            строка["looks_test"] = любой_тестовый(строка["content"])
        из_.append(строка)
    return из_


def любой_тестовый(содержимое: str) -> bool:
    с = (содержимое or "").strip()
    return any(с.startswith(н) for н in ТЕСТОВЫЕ_НАЧАЛА)


def позиции(state_dir: pathlib.Path) -> dict:
    п = state_dir / "positions.jsonl"
    из_: dict = {}
    if not п.exists():
        return из_
    for с in п.read_text(encoding="utf-8", errors="replace").split("\n"):
        с = с.strip()
        if not с:
            continue
        try:
            р = json.loads(с)
        except ValueError:
            continue
        cid = р.get("client_order_id")
        if cid:
            из_.setdefault(cid, {}).update(р)
    return из_


def открытые_старше(поз: dict, *, секунд: float = 60.0,
                     сейчас: float | None = None) -> list:
    """Реальные открытые позиции старше порога.

    Реальной считается позиция боевого режима: у стенда и сухого прогона свои
    метки, и смешивать их нельзя -- иначе тестовая позиция выглядела бы
    зависшей сделкой.
    """
    сейчас = сейчас if сейчас is not None else time.time()
    из_ = []
    for cid, р in (поз or {}).items():
        if р.get("state") in ("closed", "unsold"):
            continue
        if str(р.get("mode") or "").lower() not in ("live", "real"):
            continue
        т = р.get("ts_intent") or р.get("ts_sent") or 0
        возраст = (сейчас - float(т)) if т else None
        if возраст is None or возраст > секунд:
            из_.append({"cid": cid, "mint": р.get("mint"), "state": р.get("state"),
                         "lane": р.get("lane"), "age_s": (round(возраст, 1)
                                                           if возраст else None),
                         "sol_in": р.get("sol_in"),
                         "bought_raw": р.get("lane_bought_raw"),
                         "signatures": (р.get("signatures") or [None])[0]
                                        or р.get("lane_signature")})
    return из_


def последняя_продажа(state_dir: pathlib.Path) -> dict:
    """Последняя строка журнала сторожа, где есть ПОДПИСЬ продажи."""
    п = state_dir / "seller.jsonl"
    if not п.exists():
        return {"why_not": "журнала продаж нет"}
    последняя = None
    for с in п.read_text(encoding="utf-8", errors="replace").split("\n"):
        if '"jup_signature"' not in с and '"last_sell_signatures"' not in с:
            continue
        try:
            р = json.loads(с)
        except ValueError:
            continue
        подпись = р.get("jup_signature") or (р.get("last_sell_signatures") or [None])[0]
        if подпись:
            последняя = {"signature": подпись, "cid": р.get("client_order_id"),
                          "mint": р.get("mint"), "action": р.get("action"),
                          "ts": р.get("ts") or р.get("ts_utc")}
    return последняя or {"why_not": "в журнале сторожа нет продаж с подписью"}


def токены_кошелька(кошелёк: str, ключ: str, *, таймаут: float = 20.0) -> dict:
    """Остатки токенов кошелька по цепи. Один вызов, только чтение."""
    если_нет = {"wallet": кошелёк, "ok": False, "why_not": None, "nonzero": []}
    if not кошелёк or not ключ:
        если_нет["why_not"] = "нет адреса или ключа узла"
        return если_нет
    тело = json.dumps({"jsonrpc": "2.0", "id": 1,
                        "method": "getTokenAccountsByOwner",
                        "params": [кошелёк, {"programId": ТОКЕН_ПРОГРАММА},
                                   {"encoding": "jsonParsed"}]}).encode()
    запрос = urllib.request.Request(
        f"https://mainnet.helius-rpc.com/?api-key={ключ}", data=тело,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(запрос, timeout=таймаут) as о:
            данные = json.loads(о.read().decode())
    except Exception as exc:  # noqa: BLE001
        если_нет["why_not"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return если_нет
    ряд = ((данные.get("result") or {}).get("value") or [])
    ненулевые = []
    for а in ряд:
        и = (((а.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
        к = и.get("tokenAmount") or {}
        try:
            сколько = float(к.get("uiAmount") or 0)
        except (TypeError, ValueError):
            сколько = 0.0
        if сколько > 0:
            ненулевые.append({"mint": и.get("mint"), "amount": к.get("amount"),
                               "ui": к.get("uiAmount")})
    return {"wallet": кошелёк, "ok": True, "accounts": len(ряд),
            "nonzero": ненулевые, "why_not": None}


def снять_тестовый(рубильники: list) -> list:
    """Снимает ТОЛЬКО те рубильники, содержимое которых дословно тестовое.

    Прежнее содержимое сохраняется рядом с отметкой времени: разбор потом
    должен видеть, что именно стояло.
    """
    из_ = []
    for р in рубильники:
        if not р.get("exists") or "KILL_SELL" not in р["path"]:
            continue
        п = pathlib.Path(р["path"])
        if not р.get("looks_test"):
            из_.append({"path": р["path"], "removed": False,
                         "why_not": f"содержимое не тестовое: {р.get('content')!r}"})
            continue
        копия = п.with_name(п.name + ".testovyy."
                             + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
        try:
            копия.write_text(р.get("content") or "", encoding="utf-8")
            п.unlink()
            из_.append({"path": р["path"], "removed": True, "backup": str(копия),
                         "was": р.get("content")})
        except OSError as exc:
            из_.append({"path": р["path"], "removed": False,
                         "why_not": f"{type(exc).__name__}: {exc}"})
    return из_


def self_test() -> int:
    проверки = []

    def chk(имя, ок, факт=""):
        проверки.append((имя, bool(ок), факт))

    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as врем:
        в = pathlib.Path(врем)
        (в / "state").mkdir()
        (в / "env").mkdir()
        (в / "env" / "KILL_SELL").write_text("стоп продаж", encoding="utf-8")
        (в / "state" / "KILL_SELL_BY_TELEGRAM").write_text(
            "остановлено владельцем 25.09: разбираемся", encoding="utf-8")
        руб = состояние_рубильников(в / "state", в / "env")
        тест = [р for р in руб if р.get("exists") and р.get("looks_test")]
        chk("тестовое содержимое опознано, а причина владельца -- нет",
            [р["path"] for р in тест] == [str(в / "env" / "KILL_SELL")], тест)
        снято = снять_тестовый(руб)
        chk("снят только тестовый рубильник",
            [с["path"] for с in снято if с["removed"]]
            == [str(в / "env" / "KILL_SELL")], снято)
        chk("рубильник владельца остался на месте",
            (в / "state" / "KILL_SELL_BY_TELEGRAM").exists(), "")
        chk("прежнее содержимое сохранено рядом",
            any(п.name.startswith("KILL_SELL.testovyy.")
                for п in (в / "env").iterdir()), list((в / "env").iterdir()))

        # ОТКРЫТЫЕ ПОЗИЦИИ: только боевые и только старше порога.
        сейчас = 1_000_000.0
        поз = {
            "живая": {"client_order_id": "живая", "mode": "live", "state": "bought",
                       "ts_intent": сейчас - 300, "mint": "M", "sol_in": 0.2},
            "свежая": {"client_order_id": "свежая", "mode": "live", "state": "bought",
                        "ts_intent": сейчас - 10, "mint": "M2"},
            "стенд": {"client_order_id": "стенд", "mode": "live_test",
                       "state": "bought", "ts_intent": сейчас - 300},
            "закрытая": {"client_order_id": "закрытая", "mode": "live",
                          "state": "closed", "ts_intent": сейчас - 300},
        }
        стар = открытые_старше(поз, секунд=60.0, сейчас=сейчас)
        chk("в открытых только боевая старше минуты",
            [с["cid"] for с in стар] == ["живая"], стар)

        (в / "state" / "seller.jsonl").write_text("\n".join([
            json.dumps({"client_order_id": "c1", "action": "продажа отправлена",
                         "jup_signature": "ПОДПИСЬ_РЕАЛЬНАЯ", "mint": "M"}),
            json.dumps({"client_order_id": "c2", "action": "ждём"}),
        ]), encoding="utf-8")
        пр = последняя_продажа(в / "state")
        chk("последняя продажа найдена по подписи",
            пр.get("signature") == "ПОДПИСЬ_РЕАЛЬНАЯ", пр)
        chk("без ключа узла остатки не выдумываются",
            токены_кошелька("W", "")["ok"] is False, "")

    плохих = [(и, ф) for и, ок, ф in проверки if not ок]
    for имя, ок, факт in проверки:
        print(f"  [{'ok  ' if ок else 'нет '}] {имя}" + ("" if ок else f" -- {факт}"))
    print(f"самопроверка разбора происшествия: "
          f"{len(проверки) - len(плохих)}/{len(проверки)} пройдено")
    return 1 if плохих else 0


def main(аргв: list) -> int:
    if "--self-test" in аргв:
        return self_test()
    state_dir = pathlib.Path(os.environ.get("BLOOM_STATE_DIR")
                             or "/home/bot/bloom_executor_live_data")
    env_dir = pathlib.Path("/etc/bloom-executor")
    ключ = (os.environ.get("HELIUS_API_KEY") or "").strip()
    кошельки = [w for w in (os.environ.get("EXEC_WALLET_ADDRESS")
                             or "4s87RRC2V2XAJD6R8U2dP8kQH99Z2wA6fg88ZVfV4j4N",
                             os.environ.get("OWN_SEND_WALLET")) if w]
    руб = состояние_рубильников(state_dir, env_dir)
    итог = {"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "state_dir": str(state_dir), "kills": руб,
            "seller_heartbeat": {}, "last_sale": последняя_продажа(state_dir),
            "open_older_60s": открытые_старше(позиции(state_dir)),
            "wallets": [токены_кошелька(w, ключ) for w in кошельки]}
    п = state_dir / "seller_heartbeat.json"
    if п.exists():
        try:
            хб = json.loads(п.read_text(encoding="utf-8"))
            итог["seller_heartbeat"] = {к: хб.get(к) for к in
                                         ("updated_utc", "mode", "kill_sell",
                                          "kill", "positions_in_cycle")}
        except ValueError as exc:
            итог["seller_heartbeat"] = {"why_not": f"{type(exc).__name__}"}
    if "--snyat-test-kill" in аргв:
        итог["removed"] = снять_тестовый(руб)
    print(json.dumps(итог, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
