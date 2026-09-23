#!/usr/bin/env python3
"""Сколько дали бы НАШИ РЕАЛЬНЫЕ сделки при более долгом удержании.

Вопрос владельца: мы держим ~35 секунд. А что было бы на 60 секундах,
2 и 5 минутах -- по тем же самым сделкам, что мы действительно сделали?

Вход НЕ симулируется: берётся наша фактическая цена покупки из учёта
(дельта SOL кошелька, делённая на дельту токена в той же транзакции).
Симулируется только ВЫХОД: цена первой сделки по минту начиная с момента
"наша покупка + горизонт". Способ поиска цены -- тот же, что в
ретро-сигнале (analysis/solana_retro_signal.py).

Только чтение цепочки: ни одного вызова покупки/продажи.

ОГОВОРКА, без которой цифры врут: на длинном горизонте токен может
обвалиться почти до нуля, и медиана это ПРЯЧЕТ -- она нечувствительна к
хвосту. Поэтому в отчёте рядом с медианой обязательно идут среднее и
доля обвалов (сигнал ниже -30%): иначе "медиана выросла" читалось бы как
"держать дольше выгодно", хотя несколько нулей съедали бы весь счёт.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from solana_crowd_scan import PUBLIC_RPC, Rpc, helius_key, now_utc, scrub  # noqa: E402
from solana_retro_signal import SlotTrades, log, tx_trades_for_mint  # noqa: E402

TRADES_PATH = REPO_ROOT / "data" / "solana_trades_all.json"
CHAIN_CACHE_PATH = REPO_ROOT / "data" / "chain_tx_cache.json"
OUT_PATH = REPO_ROOT / "data" / "solana_hold_horizons.json"
CACHE_PATH = REPO_ROOT / "data" / "solana_hold_horizons_cache.json"

SLOT_MS = 250
HORIZONS_S = (35, 60, 120, 300)
SEARCH_WINDOW_S = 30          # дальше целевой точки не ищем -- иначе no_exit
SEARCH_BACK_SLOTS = 8         # запас на пропущенные слоты перед целевой точкой
CRASH_PCT = -30.0             # что считаем обвалом
METHOD_VERSION = 1


def horizon_key(h: int) -> str:
    return f"{h}с"


# ---------- выборка и вход ----------

def our_entry(row: dict, cache: dict, rpc=None) -> tuple[float | None, int | None, int | None,
                                                          str | None, str | None]:
    """(цена входа SOL/токен, слот, block_time, причина отказа, откуда цена).

    Кэш цепочки собирается учётом только для записей DBot со state=done,
    и на первом прогоне 70 закрытых сделок из 353 оказались без покупки в
    кэше -- причём НЕ равномерно: BATCH-4 терял 52%, пилот 46%, а
    BATCH-5/6 почти ничего. Считать пилота по половине его сделок значило
    бы получить перекошенный ответ, поэтому недостающую транзакцию
    дотягиваем с цепочки поштучно (getTransaction -- один вызов, дёшево),
    а не списываем в "не удалось".
    """
    v = cache.get(f"{row['buy_signature']}:{row['wallet']}")
    if v:
        qty = (v.get("token_deltas") or {}).get(row["mint"])
        sol_in = row.get("sol_in")
        if qty and sol_in:
            return abs(sol_in) / abs(qty), v.get("slot"), v.get("blockTime"), None, "кэш_учёта"
    if rpc is None:
        return None, (v or {}).get("slot"), (v or {}).get("blockTime"), \
            "нашей покупки нет в кэше цепочки", None
    try:
        tx = rpc.call("getTransaction", [row["buy_signature"], {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}])
    except RuntimeError as exc:
        return None, None, None, f"getTransaction не отдался: {scrub(str(exc))[:120]}", None
    if not tx:
        return None, None, None, "транзакции нет на цепочке (узел вернул пусто)", None
    r = tx_trades_for_mint(tx, row["mint"], 0, tx.get("blockTime"))
    if not r or r.get("price_sol_per_token") is None:
        why = (r or {}).get("price_note") or "цена по транзакции не считается"
        return None, tx.get("slot"), tx.get("blockTime"), f"дотянули, но {why}", None
    return r["price_sol_per_token"], tx.get("slot"), tx.get("blockTime"), None, "дотянуто_с_цепочки"


def closed_trades() -> list[dict]:
    rows = json.loads(TRADES_PATH.read_text())
    return [r for r in rows
            if r.get("status") == "закрыта" and not r.get("is_hung")
            and r.get("buy_signature") and r.get("mint")]


# ---------- выход на горизонте ----------

def exit_at(st: SlotTrades, mint: str, buy_slot: int, buy_time: int, horizon_s: int,
            max_blocks: int) -> dict:
    """Первая сделка по минту начиная с buy_time + horizon_s.

    Не позже buy_time + horizon_s + SEARCH_WINDOW_S: иначе это уже не
    "держали столько-то", а "держали пока не нашлось", и цифра означала бы
    не то, чем выглядит.
    """
    t_from = buy_time + horizon_s
    t_to = t_from + SEARCH_WINDOW_S
    start = buy_slot + int(horizon_s * 1000 / SLOT_MS) - SEARCH_BACK_SLOTS
    seen_any_trade = False
    seen_braked = False
    blocks = 0
    slot = start
    while blocks < max_blocks:
        blk = st.get(slot, mint)
        slot += 1
        if blk is None:
            blocks += 1
            continue
        blocks += 1
        bt = blk.get("block_time")
        if bt is not None and bt > t_to:
            break
        for tr in blk["trades"]:
            if bt is None or bt < t_from:
                continue
            seen_any_trade = True
            if tr["price_sol_per_token"] is None:
                seen_braked = True
                continue
            return {"цена": tr["price_sol_per_token"], "слот": slot - 1,
                     "block_time": bt, "задержка_с": bt - buy_time,
                     "это_покупка": tr["is_buy"], "sol": round(tr["sol_size"], 4),
                     "блоков_просмотрено": blocks}
    if blocks >= max_blocks:
        why = "потолок блоков -- окно не досмотрено"
    elif seen_braked:
        why = "сделки были, но их цены забракованы (вырожденные)"
    elif seen_any_trade:
        why = "сделки были, но без посчитанной цены"
    else:
        why = "ни одной сделки по минту в окне -- токен встал"
    return {"цена": None, "no_exit": why, "блоков_просмотрено": blocks}


def analyse(st: SlotTrades, row: dict, cache: dict, max_blocks: int) -> dict:
    price, slot, bt, why, src = our_entry(row, cache, st.rpc)
    out: dict = {
        "task_name": row.get("task_name"), "wallet": row.get("wallet"),
        "source_address": row.get("source_address"), "mint": row["mint"],
        "buy_signature": row["buy_signature"], "buy_block_time": bt,
        "наш_вход_sol": row.get("sol_in"), "наш_итог_gross_pct": row.get("gross_pct"),
        "наш_итог_net_sol": row.get("net_sol"), "держали_с": row.get("held_seconds"),
        "цена_входа": price, "откуда_цена_входа": src,
    }
    if price is None or slot is None or bt is None:
        out["не_удалось"] = why or "нет слота/времени покупки"
        return out
    for h in HORIZONS_S:
        r = exit_at(st, row["mint"], slot, bt, h, max_blocks)
        k = horizon_key(h)
        if r.get("цена") is None:
            out[k] = {"сигнал_pct": None, "no_exit": r["no_exit"],
                       "блоков": r["блоков_просмотрено"]}
        else:
            out[k] = {"сигнал_pct": round((r["цена"] / price - 1) * 100, 4),
                       "задержка_с": r["задержка_с"], "цена": r["цена"],
                       "блоков": r["блоков_просмотрено"]}
    return out


# ---------- сводки ----------

def _stats(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0, "медиана_pct": None, "среднее_pct": None,
                "доля_в_плюс": None, "доля_обвалов": None}
    return {
        "n": len(vals),
        "медиана_pct": round(statistics.median(vals), 3),
        "среднее_pct": round(statistics.fmean(vals), 3),
        "доля_в_плюс": round(sum(1 for x in vals if x > 0) / len(vals), 4),
        "доля_обвалов": round(sum(1 for x in vals if x <= CRASH_PCT) / len(vals), 4),
        "худшая_pct": round(min(vals), 3),
        "лучшая_pct": round(max(vals), 3),
    }


PILOT_TASK = "pointfarmcap"


def failures_by_task(rows: list[dict], all_rows: list[dict]) -> dict:
    """Почему НЕ посчитали -- в разрезе задач, а не одним числом.

    Одно число ("70 сделок без цены входа") скрывает перекос: если выпала
    половина пилота, а у BATCH-5 почти ничего, то сравнение задач между
    собой уже нечестное. Поэтому доля потерь считается по каждой задаче.
    """
    total: dict = {}
    for r in all_rows:
        total[r.get("task_name")] = total.get(r.get("task_name"), 0) + 1
    out: dict = {}
    for r in rows:
        if not r.get("не_удалось"):
            continue
        t = r.get("task_name")
        d = out.setdefault(t, {"не_удалось": 0, "всего_в_задаче": total.get(t, 0),
                                "причины": {}})
        d["не_удалось"] += 1
        w = r["не_удалось"]
        d["причины"][w] = d["причины"].get(w, 0) + 1
    for t, d in out.items():
        d["доля_потерь"] = (round(d["не_удалось"] / d["всего_в_задаче"], 4)
                             if d["всего_в_задаче"] else None)
        d["пилот"] = (t == PILOT_TASK)
    return dict(sorted(out.items(), key=lambda kv: -(kv[1]["доля_потерь"] or 0)))


def entry_source_by_task(rows: list[dict]) -> dict:
    """Откуда взялась цена входа -- по задачам. Дотянутое с цепочки видно
    отдельно, чтобы не выдавать дотяжку за исходные данные учёта."""
    out: dict = {}
    for r in rows:
        if r.get("не_удалось"):
            continue
        d = out.setdefault(r.get("task_name"), {"кэш_учёта": 0, "дотянуто_с_цепочки": 0})
        k = r.get("откуда_цена_входа")
        if k in d:
            d[k] += 1
    return out


def group_table(rows: list[dict], key: str, min_n: int = 1) -> list[dict]:
    by: dict = {}
    for r in rows:
        if r.get("не_удалось"):
            continue
        by.setdefault(r.get(key), []).append(r)
    out = []
    for k, rs in by.items():
        if len(rs) < min_n:
            continue
        item = {key: k, "сделок": len(rs)}
        if key == "task_name":
            item["пилот"] = (k == PILOT_TASK)
        for h in HORIZONS_S:
            hk = horizon_key(h)
            vals = [r[hk]["сигнал_pct"] for r in rs
                    if r.get(hk) and r[hk].get("сигнал_pct") is not None]
            item[hk] = _stats(vals)
        out.append(item)
    out.sort(key=lambda x: -x["сделок"])
    return out


def longer_vs_short(rows: list[dict], short: int = 35, long: int = 300) -> dict:
    sk, lk = horizon_key(short), horizon_key(long)
    pairs = [(r[sk]["сигнал_pct"], r[lk]["сигнал_pct"]) for r in rows
             if not r.get("не_удалось")
             and r.get(sk) and r.get(lk)
             and r[sk].get("сигнал_pct") is not None and r[lk].get("сигнал_pct") is not None]
    if not pairs:
        return {"сравнимых_сделок": 0,
                "почему": "нет сделок, где посчитаны ОБА горизонта"}
    diffs = [l - s for s, l in pairs]
    better = [d for d in diffs if d > 0]
    worse = [d for d in diffs if d < 0]
    return {
        "сравнимых_сделок": len(pairs),
        "почему_только_они": "сравнение честно только там, где посчитаны ОБА горизонта",
        f"{long}с_лучше_{short}с_шт": len(better),
        f"{long}с_лучше_доля": round(len(better) / len(pairs), 4),
        "медиана_выигрыша_pct": round(statistics.median(better), 3) if better else None,
        f"{long}с_хуже_{short}с_шт": len(worse),
        f"{long}с_хуже_доля": round(len(worse) / len(pairs), 4),
        "медиана_проигрыша_pct": round(statistics.median(worse), 3) if worse else None,
        "медиана_разницы_pct": round(statistics.median(diffs), 3),
        "среднее_разницы_pct": round(statistics.fmean(diffs), 3),
    }


def no_exit_report(rows: list[dict]) -> dict:
    out: dict = {}
    for h in HORIZONS_S:
        hk = horizon_key(h)
        reasons: dict = {}
        n_ok = n_no = 0
        for r in rows:
            if r.get("не_удалось") or not r.get(hk):
                continue
            if r[hk].get("сигнал_pct") is None:
                n_no += 1
                why = r[hk].get("no_exit") or "без причины"
                reasons[why] = reasons.get(why, 0) + 1
            else:
                n_ok += 1
        out[hk] = {"посчитано": n_ok, "no_exit": n_no, "причины": reasons}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--time-budget-s", type=int, default=300 * 60)
    ap.add_argument("--min-interval-s", type=float, default=0.30)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--max-blocks", type=int, default=160)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--checkpoint-s", type=float, default=900.0)
    ap.add_argument("--use-helius", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return

    started = time.monotonic()
    try:
        key, key_name = helius_key()
    except Exception:  # noqa: BLE001
        key, key_name = "", "нет"
    rpc = Rpc(key, min_interval_s=args.min_interval_s, workers=args.workers,
              backoff_mult=1.5, backoff_cap=15.0)
    if not args.use_helius:
        rpc.url = PUBLIC_RPC
        log(f"узел: ПУБЛИЧНЫЙ (Helius без квоты), темп {args.min_interval_s}с")
    else:
        log(f"узел: Helius (ключ из {key_name})")
    rpc.deadline = started + args.time_budget_s
    st = SlotTrades(rpc, capacity=20000)

    rows = closed_trades()
    if args.limit:
        rows = rows[:args.limit]
    cache = json.loads(CHAIN_CACHE_PATH.read_text())

    done: dict = {}
    if CACHE_PATH.exists() and not args.no_cache:
        try:
            prev = json.loads(CACHE_PATH.read_text())
            if prev.get("версия_метода") == METHOD_VERSION:
                done = prev.get("строки") or {}
                log(f"возобновление: готовых строк {len(done)}")
        except (ValueError, OSError) as exc:
            log(f"кэш не читается ({type(exc).__name__}) -- считаю заново")

    log(f"закрытых сделок без зависших: {len(rows)}")
    results: list[dict] = []
    last_cp = time.monotonic()
    оборвано = 0
    for i, r in enumerate(rows, 1):
        sig = r["buy_signature"]
        if sig in done:
            results.append(done[sig])
            continue
        if rpc.expired():
            оборвано += 1
            results.append({"task_name": r.get("task_name"), "mint": r["mint"],
                             "buy_signature": sig,
                             "не_удалось": "бюджет времени прогона истёк"})
            continue
        try:
            item = analyse(st, r, cache, args.max_blocks)
        except RuntimeError as exc:
            item = {"task_name": r.get("task_name"), "mint": r["mint"],
                    "buy_signature": sig, "не_удалось": scrub(str(exc))[:200]}
        done[sig] = item
        results.append(item)
        if i % 10 == 0 or time.monotonic() - last_cp >= args.checkpoint_s:
            log(f"{i}/{len(rows)}; блоков получено {st.fetched}, пропущено {st.skipped}, "
                f"не отдалось {st.failed}; вызовов {rpc.calls}")
        if time.monotonic() - last_cp >= args.checkpoint_s:
            CACHE_PATH.write_text(json.dumps(
                {"версия_метода": METHOD_VERSION, "сохранено_utc": now_utc(),
                 "строки": done}, ensure_ascii=False))
            last_cp = time.monotonic()

    CACHE_PATH.write_text(json.dumps(
        {"версия_метода": METHOD_VERSION, "сохранено_utc": now_utc(), "строки": done},
        ensure_ascii=False))

    pilot = [r for r in results if r.get("task_name") == "pointfarmcap"]
    out = {
        "generated_at_utc": now_utc(),
        "узел": "публичный" if not args.use_helius else "helius",
        "горизонты_с": list(HORIZONS_S),
        "ЧЕСТНЫЕ_ОГОВОРКИ": [
            "Только чтение цепочки: ни одного вызова покупки/продажи.",
            "ВХОД НЕ СИМУЛИРУЕТСЯ: это наша фактическая цена покупки из учёта "
            "(дельта SOL кошелька / дельта токена в той же транзакции). Симулируется только выход.",
            "МЕДИАНА ПРЯЧЕТ ХВОСТ. На длинном горизонте токен может обвалиться почти до нуля, "
            "а медиана к этому нечувствительна. Поэтому рядом с медианой всегда идут среднее и "
            f"доля обвалов (сигнал <= {CRASH_PCT}%): без них 'медиана выросла' читалось бы как "
            "'держать дольше выгодно', хотя несколько нулей съедали бы весь счёт.",
            "Издержки не вычитаются: это уровень 'сигнал', как в ретро-сигнале.",
            f"Выход ищется не дальше +{SEARCH_WINDOW_S}с от целевой точки; не нашлось -- честный "
            "no_exit для этого горизонта, а не 'держали пока не продалось'.",
            "Цена выхода -- по дельтам балансов трейдера (как в ретро-сигнале): внутри "
            "проскальзывание и чаевые того трейдера. Смещение одинаково на всех горизонтах.",
            "Сделки, помеченные в учёте зависшими (is_hung), исключены.",
        ],
        "сделок_всего": len(results),
        "разобрано": sum(1 for r in results if not r.get("не_удалось")),
        "не_удалось": sum(1 for r in results if r.get("не_удалось")),
        "оборвано_бюджетом": оборвано,
        "по_задачам": group_table(results, "task_name"),
        "не_удалось_по_задачам": failures_by_task(results, results),
        "откуда_цена_входа_по_задачам": entry_source_by_task(results),
        "пилот_pointfarmcap": {"сделок": len(pilot), **{
            horizon_key(h): _stats([r[horizon_key(h)]["сигнал_pct"] for r in pilot
                                     if not r.get("не_удалось") and r.get(horizon_key(h))
                                     and r[horizon_key(h)].get("сигнал_pct") is not None])
            for h in HORIZONS_S}},
        "по_источникам_от_5_сделок": group_table(results, "source_address", min_n=5),
        "всего_вместе": {horizon_key(h): _stats(
            [r[horizon_key(h)]["сигнал_pct"] for r in results
             if not r.get("не_удалось") and r.get(horizon_key(h))
             and r[horizon_key(h)].get("сигнал_pct") is not None]) for h in HORIZONS_S},
        "5мин_против_35с": longer_vs_short(results, 35, 300),
        "60с_против_35с": longer_vs_short(results, 35, 60),
        "2мин_против_35с": longer_vs_short(results, 35, 120),
        "no_exit_по_горизонтам": no_exit_report(results),
        "rpc": {"calls": rpc.calls, "retries": rpc.retries, "errors": rpc.errors,
                 "блоков_получено": st.fetched, "пропущенных_слотов": st.skipped,
                 "блоков_не_отдалось": st.failed, "выбраковано_цен": st.outliers,
                 "минут": round((time.monotonic() - started) / 60, 1)},
        "строки": results,
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    log("вместе: " + json.dumps(out["всего_вместе"], ensure_ascii=False))
    log("5мин против 35с: " + json.dumps(out["5мин_против_35с"], ensure_ascii=False))
    log("no_exit: " + json.dumps(out["no_exit_по_горизонтам"], ensure_ascii=False))


def self_test() -> None:
    checks: list[tuple[str, bool, str]] = []

    def chk(name: str, ok: bool, got: str = "") -> None:
        checks.append((name, bool(ok), got))

    class FakeST:
        def __init__(self, by_slot):
            self.by_slot = by_slot
        def get(self, slot, mint):
            return self.by_slot.get(slot)

    def TR(price, buy=True, sol=1.0):
        return {"price_sol_per_token": price, "is_buy": buy, "sol_size": sol}

    # Выход берётся ПОСЛЕ целевой точки, а не раньше.
    bt, BUY_SLOT = 1000, 500
    # старт поиска = 500 + 140 - 8 = 632; блоки расставлены под него
    blocks = {
        632: {"trades": [TR(9.0)], "block_time": bt + 30},   # рано -- не годится
        640: {"trades": [TR(2.0)], "block_time": bt + 35},
    }
    r = exit_at(FakeST(blocks), "m", BUY_SLOT, bt, 35, 160)
    chk("сделку раньше горизонта не берём", r["цена"] != 9.0, str(r.get("цена")))
    chk("берём первую сделку не раньше горизонта", r["цена"] == 2.0, str(r))
    chk("задержка считается от покупки", r["задержка_с"] == 35, str(r.get("задержка_с")))

    # За окном поиска -- честный no_exit.
    late = {632 + i: {"trades": [], "block_time": bt + 27 + i} for i in range(0, 48)}
    late[700] = {"trades": [TR(5.0)], "block_time": bt + 35 + SEARCH_WINDOW_S + 5}
    r2 = exit_at(FakeST(late), "m", BUY_SLOT, bt, 35, 160)
    chk("позже окна -- no_exit, а не 'нашли'", r2["цена"] is None, str(r2))
    chk("причина названа", "no_exit" in r2)

    # Мёртвый токен и забракованная цена различаются.
    dead = {632 + i: {"trades": [], "block_time": bt + 27 + i} for i in range(0, 48)}
    r3 = exit_at(FakeST(dead), "m", BUY_SLOT, bt, 35, 160)
    chk("мёртвый токен назван мёртвым", "токен встал" in (r3.get("no_exit") or ""), str(r3))
    brk = {632 + i: {"trades": [], "block_time": bt + 27 + i} for i in range(0, 48)}
    brk[640] = {"trades": [TR(None)], "block_time": bt + 35}
    r4 = exit_at(FakeST(brk), "m", BUY_SLOT, bt, 35, 160)
    chk("забракованная цена -- отдельная причина",
        "забракован" in (r4.get("no_exit") or ""), str(r4))

    # Статистика: среднее и хвост не теряются за медианой.
    s = _stats([10.0, 12.0, 11.0, -95.0])
    chk("медиана не видит обвал", s["медиана_pct"] == 10.5, str(s["медиана_pct"]))
    chk("среднее видит", s["среднее_pct"] < 0, str(s["среднее_pct"]))
    chk("доля обвалов посчитана", s["доля_обвалов"] == 0.25, str(s["доля_обвалов"]))
    chk("пустая выборка не превращается в ноль", _stats([])["медиана_pct"] is None)

    # Сравнение горизонтов -- только по сделкам, где посчитаны ОБА.
    rr = [{"35с": {"сигнал_pct": 10.0}, "300с": {"сигнал_pct": 20.0}},
          {"35с": {"сигнал_pct": 10.0}, "300с": {"сигнал_pct": None, "no_exit": "x"}},
          {"35с": {"сигнал_pct": 5.0}, "300с": {"сигнал_pct": -95.0}}]
    c = longer_vs_short(rr, 35, 300)
    chk("в сравнение идут только полные пары", c["сравнимых_сделок"] == 2, str(c))
    chk("лучше и хуже считаются раздельно",
        c["300с_лучше_35с_шт"] == 1 and c["300с_хуже_35с_шт"] == 1, str(c))
    chk("медиана проигрыша отрицательная", c["медиана_проигрыша_pct"] == -100.0,
        str(c["медиана_проигрыша_pct"]))

    fb = failures_by_task(
        [{"task_name": "pointfarmcap", "не_удалось": "нет в кэше"},
         {"task_name": "pointfarmcap"},
         {"task_name": "BATCH-5", "не_удалось": "нет в кэше"}],
        [{"task_name": "pointfarmcap"}] * 2 + [{"task_name": "BATCH-5"}] * 10)
    chk("доля потерь считается по каждой задаче",
        fb["pointfarmcap"]["доля_потерь"] == 0.5 and fb["BATCH-5"]["доля_потерь"] == 0.1,
        str(fb))
    chk("пилот помечен в разбивке отказов", fb["pointfarmcap"]["пилот"] is True)
    chk("задачи без отказов в разбивку не попадают", "BATCH-9" not in fb)
    gt = group_table([{"task_name": "pointfarmcap", "35с": {"сигнал_pct": 1.0}},
                      {"task_name": "BATCH-5", "35с": {"сигнал_pct": 2.0}}], "task_name")
    chk("пилот -- отдельная помеченная строка таблицы задач",
        [r["пилот"] for r in gt if r["task_name"] == "pointfarmcap"] == [True], str(gt))
    es = entry_source_by_task([{"task_name": "T", "откуда_цена_входа": "дотянуто_с_цепочки"},
                                {"task_name": "T", "откуда_цена_входа": "кэш_учёта"},
                                {"task_name": "T", "не_удалось": "x"}])
    chk("дотянутое с цепочки видно отдельно от учёта",
        es["T"] == {"кэш_учёта": 1, "дотянуто_с_цепочки": 1}, str(es))

    ne = no_exit_report([{"35с": {"сигнал_pct": None, "no_exit": "токен встал"}},
                          {"35с": {"сигнал_pct": 1.0}}])
    chk("no_exit считается с причиной",
        ne["35с"]["no_exit"] == 1 and ne["35с"]["причины"]["токен встал"] == 1, str(ne))
    chk("посчитанные тоже считаются", ne["35с"]["посчитано"] == 1)

    bad = 0
    for name, ok, got in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {name}" + (f"  -> {got}" if got and not ok else ""))
        bad += (not ok)
    print(f"самопроверка горизонтов: {len(checks) - bad}/{len(checks)} пройдено")
    if bad:
        raise SystemExit(f"самопроверка не пройдена: {bad} из {len(checks)}")


if __name__ == "__main__":
    main()
