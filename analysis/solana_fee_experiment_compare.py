#!/usr/bin/env python3
"""Тест низких комиссий на BATCH-5: сравнение ДО и ПОСЛЕ переключения.

Владелец переключает BATCH-5 с priority 0.0051 + tip 0.0051 на
priority 0.0005 + tip 0.0005. Вопрос: насколько это ухудшает исполнение.
Границу времени задаёт владелец (--switch-ts, секунды UTC).

ЧТО СРАВНИВАЕТСЯ (три величины из распоряжения владельца):
  1. ОПОЗДАНИЕ В СЛОТАХ -- offset_slots из data/solana_entry_log.json:
     на сколько слотов наша покупка отстала от покупки источника.
  2. ДОЛЯ НЕПОПАДАНИЙ В БЛОК -- записи follow_trades, где DBot пытался
     скопировать покупку, но транзакция не села. Считается как
     доля не-done записей типа buy от всех записей типа buy.
  3. СРЫВЫ ПО ПРОСКАЛЬЗЫВАНИЮ -- подмножество непопаданий, где причина
     содержит slippage (ExceededSlippage и родственные коды).
Дополнительно (проверка, что переключение вообще состоялось):
  4. ФАКТИЧЕСКАЯ НАДБАВКА = sol_out - gross_sol_in по каждой покупке.
     Это чаевые + приоритет + комиссия сети, измеренные ПО ЦЕПИ. Если
     после границы она не упала -- значит настройка не применилась, и
     сравнивать остальное бессмысленно. Скрипт говорит это явно.

ЧЕСТНОСТЬ:
  * ничего не усредняется через границу: окна строго до и строго после;
  * окна по умолчанию симметричны (одинаковой длины), иначе сравнение
    смешивает объём выборки с эффектом; длина задаётся --window-h;
  * если в каком-то окне меньше --min-n наблюдений, вывод помечается как
    недостаточный, а не выдаётся за результат;
  * доля непопаданий и опоздание считаются по РАЗНЫМ источникам
    (follow_trades и entry_log), это указано в выводе;
  * сравнение с контрольной задачей: те же три величины по остальным
    кошелькам за те же окна. Если у всех в "после" стало хуже -- дело не
    в комиссиях BATCH-5, а в рынке. Без контроля вывод не делается.

ТОЛЬКО ЧТЕНИЕ: ни одной покупки/продажи, ни одного POST в DBot.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
ENTRY_LOG = REPO / "data" / "solana_entry_log.json"
OUT = REPO / "data" / "solana_fee_experiment_compare.json"
DBOT_HOST = "https://api-bot-v1.dbotx.com"

SLIPPAGE_MARKERS = ("slippage", "SLIPPAGE", "ExceededSlippage", "EXCEEDED_SLIPPAGE")

_SECRETS: list[str] = []


def scrub(t: str) -> str:
    for s in _SECRETS:
        if s and len(s) > 6:
            t = t.replace(s, "<секрет>")
    return t


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S', time.gmtime())}Z] {scrub(m)}", flush=True)


def env(*names: str) -> tuple[str, str]:
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            if "\n" in v or "\r" in v:
                raise RuntimeError(f"{n} содержит перевод строки")
            _SECRETS.append(v)
            return v, n
    raise RuntimeError(f"нет переменных окружения: {', '.join(names)}")


def dbot_get(path: str, params: dict, key: str) -> tuple[int | None, dict]:
    for a in range(6):
        try:
            r = requests.get(f"{DBOT_HOST}{path}", params=params,
                             headers={"X-API-KEY": key}, timeout=30)
        except Exception:  # noqa: BLE001
            time.sleep(2 * (a + 1)); continue
        if r.status_code == 429:
            time.sleep(3 * (a + 1)); continue
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"non_json": scrub(r.text[:400])}
    return None, {}


def items(body) -> list:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in ("res", "data", "results", "list", "items"):
            if isinstance(body.get(k), list):
                return body[k]
    return []


def follow_orders(key: str) -> list[dict]:
    st, b = dbot_get("/automation/follow_orders", {}, key)
    if st != 200:
        raise RuntimeError(f"follow_orders http={st}")
    out = []
    for r in items(b):
        if r.get("id") and r.get("walletAddress"):
            out.append({"id": r["id"], "name": r.get("name") or r["id"],
                         "wallet": r["walletAddress"]})
    if not out:
        raise RuntimeError("follow_orders вернул 0 задач")
    return out


def follow_trades(task_id: str, key: str, pages: int = 40) -> tuple[list[dict], bool]:
    """Записи попыток копирования. Страницы с НУЛЯ (подтверждённый баг
    этой сессии: docs говорят page defaults to 0, а мы начинали с 1 и
    всегда теряли первую страницу). Неудачная страница -- НЕ конец
    списка: возвращаем complete=False, а не выдаём обрыв за конец."""
    out: list[dict] = []
    for page in range(pages):
        st, b = dbot_get("/account/follow_trades",
                          {"followOrderId": task_id, "page": page, "size": 100}, key)
        if st != 200:
            return out, False
        rows = items(b)
        out.extend(rows)
        if len(rows) < 100:
            return out, True
    return out, False


def rec_time_s(r: dict) -> float | None:
    """createAt у DBot в миллисекундах -- приводим к секундам. Поле
    отсутствует -- запись во временное окно не попадает вообще, и это
    честнее, чем подставить now()."""
    for k in ("createAt", "createdAt", "time", "timestamp"):
        v = r.get(k)
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        return v / 1000.0 if v > 1e11 else v
    return None


def fail_reason(r: dict) -> str:
    return " ".join(str(r.get(k) or "") for k in ("errorMessage", "errorCode", "skipReason")).strip()


def window_stats_trades(recs: list[dict], t0: float, t1: float) -> dict:
    """Непопадания и срывы по проскальзыванию из follow_trades."""
    buys = [r for r in recs
            if str(r.get("type") or "").lower() == "buy"
            and (rec_time_s(r) is not None) and t0 <= rec_time_s(r) < t1]
    done = [r for r in buys if str(r.get("state") or "").lower() == "done"]
    miss = [r for r in buys if str(r.get("state") or "").lower() != "done"]
    slip = [r for r in miss if any(m.lower() in fail_reason(r).lower() for m in SLIPPAGE_MARKERS)]
    reasons: dict[str, int] = {}
    for r in miss:
        reasons[fail_reason(r) or "(без текста)"] = reasons.get(fail_reason(r) or "(без текста)", 0) + 1
    return {"n_попыток_buy": len(buys), "n_прошло": len(done), "n_непопаданий": len(miss),
            "доля_непопаданий_%": round(len(miss) / len(buys) * 100, 2) if buys else None,
            "n_срывов_по_проскальзыванию": len(slip),
            "доля_срывов_по_проскальзыванию_%": round(len(slip) / len(buys) * 100, 2) if buys else None,
            "причины_топ": sorted(reasons.items(), key=lambda kv: -kv[1])[:6]}


def window_stats_entry(trades: list[dict], wallet: str, t0: float, t1: float) -> dict:
    """Опоздание в слотах и фактическая надбавка из entry_log."""
    rows = [t for t in trades
            if t.get("wallet") == wallet and t.get("our_block_time")
            and t0 <= t["our_block_time"] < t1]
    off = [t["offset_slots"] for t in rows if t.get("offset_slots") is not None]
    prem = [round(t["sol_out"] - t["gross_sol_in"], 9) for t in rows
            if t.get("sol_out") is not None and t.get("gross_sol_in")]
    sig = [t["signal_pct"] for t in rows if t.get("signal_pct") is not None]
    def med(v):
        return round(statistics.median(v), 4) if v else None
    return {"n_сделок_в_цепи": len(rows),
            "n_с_опозданием": len(off), "медиана_опоздания_слотов": med(off),
            "p75_опоздания_слотов": (round(sorted(off)[int(len(off) * 0.75)], 4) if off else None),
            "n_с_надбавкой": len(prem), "медиана_надбавки_SOL": med(prem),
            "медиана_signal_pct": med(sig)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--switch-ts", type=int, required=True,
                    help="момент переключения комиссий, unix-секунды UTC")
    ap.add_argument("--window-h", type=float, default=24.0,
                    help="длина каждого окна в часах (окна симметричны)")
    ap.add_argument("--task", default="BATCH-5", help="имя задачи эксперимента")
    ap.add_argument("--min-n", type=int, default=20,
                    help="меньше этого числа наблюдений в окне -- вывод помечается недостаточным")
    args = ap.parse_args()

    dbot_key, dn = env("DBOT_API_KEY", "DBOT_APIKEY")
    log(f"ключ DBot из {dn}")
    w = args.window_h * 3600
    before = (args.switch_ts - w, args.switch_ts)
    after = (args.switch_ts, args.switch_ts + w)
    now = time.time()
    log(f"граница: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(args.switch_ts))}, окно {args.window_h}ч")

    tasks = follow_orders(dbot_key)
    exp = [t for t in tasks if t["name"] == args.task]
    if not exp:
        raise RuntimeError(f"задачи {args.task} нет в follow_orders -- имя не выдумываю")
    control = [t for t in tasks if t["name"] != args.task]
    log(f"эксперимент: {args.task}; контроль: {', '.join(sorted(t['name'] for t in control))}")

    entry = json.loads(ENTRY_LOG.read_text()) if ENTRY_LOG.exists() else {"trades": []}
    entry_trades = entry.get("trades") or []
    entry_max_t = max((t.get("our_block_time") or 0 for t in entry_trades), default=0)

    report: dict = {
        "сформировано_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "режим": "ТОЛЬКО ЧТЕНИЕ: ни одной покупки/продажи, ни одного POST в DBot",
        "граница_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(args.switch_ts)),
        "окно_часов": args.window_h,
        "окно_до": [int(before[0]), int(before[1])],
        "окно_после": [int(after[0]), int(after[1])],
        "выгрузка_entry_log_свежесть_utc": (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(entry_max_t)) if entry_max_t else None),
        "задачи": {}, "предупреждения": [],
    }
    if after[1] > now:
        report["предупреждения"].append(
            f"окно 'после' ещё не закрыто: конец {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(after[1]))} "
            f"в будущем -- сравнение неполное, повторить позже")
    if entry_max_t and entry_max_t < after[1]:
        report["предупреждения"].append(
            "entry_log обрывается раньше конца окна 'после' -- опоздание в слотах посчитано не на всём окне; "
            "перед выводом перезапустить analysis/solana_entry_log_offsets.py")

    for t in tasks:
        recs, complete = follow_trades(t["id"], dbot_key)
        if not complete:
            report["предупреждения"].append(
                f"{t['name']}: выгрузка follow_trades НЕПОЛНАЯ (обрыв пагинации) -- доли занижены")
        row = {"кошелёк": t["wallet"], "роль": "эксперимент" if t["name"] == args.task else "контроль",
               "записей_follow_trades": len(recs), "выгрузка_полная": complete}
        for label, (a, b) in (("до", before), ("после", after)):
            row[label] = {**window_stats_trades(recs, a, b),
                          **window_stats_entry(entry_trades, t["wallet"], a, b)}
        for k in ("медиана_опоздания_слотов", "доля_непопаданий_%",
                   "доля_срывов_по_проскальзыванию_%", "медиана_надбавки_SOL"):
            x, y = row["до"].get(k), row["после"].get(k)
            row[f"Δ_{k}"] = round(y - x, 4) if (x is not None and y is not None) else None
        row["достаточно_данных"] = all(
            (row[lbl]["n_попыток_buy"] >= args.min_n) for lbl in ("до", "после"))
        report["задачи"][t["name"]] = row
        log(f"{t['name']:<14} до: попыток={row['до']['n_попыток_buy']} непоп={row['до']['доля_непопаданий_%']}% "
            f"опозд={row['до']['медиана_опоздания_слотов']} надб={row['до']['медиана_надбавки_SOL']} | "
            f"после: попыток={row['после']['n_попыток_buy']} непоп={row['после']['доля_непопаданий_%']}% "
            f"опозд={row['после']['медиана_опоздания_слотов']} надб={row['после']['медиана_надбавки_SOL']}")

    e = report["задачи"].get(args.task, {})
    prem_before = (e.get("до") or {}).get("медиана_надбавки_SOL")
    prem_after = (e.get("после") or {}).get("медиана_надбавки_SOL")
    if prem_before is not None and prem_after is not None:
        applied = prem_after < prem_before * 0.5
        report["настройка_применилась"] = {
            "надбавка_до_SOL": prem_before, "надбавка_после_SOL": prem_after, "применилась": applied,
            "пояснение": ("надбавка = sol_out - gross_sol_in, измерена по цепи; ожидание при переходе "
                           "0.0051+0.0051 -> 0.0005+0.0005 -- падение примерно на 0.0092 SOL. "
                           "Не упала -- настройка не применилась, остальные сравнения бессмысленны")}
        if not applied:
            report["предупреждения"].append(
                "надбавка по цепи НЕ упала -- переключение комиссий, похоже, не вступило в силу")
    else:
        report["настройка_применилась"] = {
            "применилась": None, "пояснение": "в одном из окон нет сделок с известной надбавкой"}

    # Контроль: стало ли хуже У ВСЕХ -- тогда дело в рынке, а не в комиссиях.
    worse_control = [n for n, r in report["задачи"].items()
                      if r["роль"] == "контроль" and (r.get("Δ_доля_непопаданий_%") or 0) > 0]
    report["контроль"] = {
        "задач_контроля": sum(1 for r in report["задачи"].values() if r["роль"] == "контроль"),
        "у_скольких_непопадания_выросли": len(worse_control), "кто": worse_control,
        "пояснение": ("если непопадания выросли у контрольных задач так же, как у эксперимента, "
                       "причина в рынке, а не в комиссиях -- вывод о комиссиях делать нельзя")}

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print()
    print("=" * 110)
    print(f"ТЕСТ НИЗКИХ КОМИССИЙ: {args.task}, граница {report['граница_utc']}, окно {args.window_h}ч")
    print(f"  {'задача':<15}{'роль':<12}{'n до':>6}{'n после':>8}{'непоп.до':>10}{'непоп.после':>12}"
          f"{'опозд.до':>10}{'опозд.после':>12}{'надб.до':>10}{'надб.после':>11}")
    for name, r in report["задачи"].items():
        print(f"  {name:<15}{r['роль']:<12}{r['до']['n_попыток_buy']:>6}{r['после']['n_попыток_buy']:>8}"
              f"{str(r['до']['доля_непопаданий_%']):>10}{str(r['после']['доля_непопаданий_%']):>12}"
              f"{str(r['до']['медиана_опоздания_слотов']):>10}{str(r['после']['медиана_опоздания_слотов']):>12}"
              f"{str(r['до']['медиана_надбавки_SOL']):>10}{str(r['после']['медиана_надбавки_SOL']):>11}")
    print()
    print("настройка применилась:", json.dumps(report["настройка_применилась"], ensure_ascii=False))
    print("контроль:", json.dumps(report["контроль"], ensure_ascii=False))
    for w_ in report["предупреждения"]:
        print("ВНИМАНИЕ:", w_)
    print(f"файл: {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
