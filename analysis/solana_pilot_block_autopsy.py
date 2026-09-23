#!/usr/bin/env python3
"""Поблочный разбор сделок пилота: что происходит за лидером.

Задача владельца: пилот (задача pointfarmcap, лидер Beqv6dz...) ушёл в
серию минусов. Нужно понять, ЧТО ИМЕННО изменилось на цепочке -- меньше
покупателей за лидером, больше продавцов, пик стал раньше нашего входа,
или лидер сам стал быстрее выходить.

Только чтение цепочки: ни одного вызова покупки/продажи.

Helius сейчас без квоты ("max usage reached"), поэтому по умолчанию весь
разбор идёт через ПУБЛИЧНЫЙ узел с более спокойным темпом. Это медленнее,
и потому у прогона есть бюджет времени и честный отчёт о том, что не
успели: недосчитанная сделка помечается, а не превращается в пустую
строку.

Метод для каждой сделки:
  1. находим покупку ЛИДЕРА того же минта в окне до нашей покупки;
  2. считаем влияние лидера на цену: последняя цена до его сделки против
     цены его собственной сделки;
  3. выписываем ВСЕ сделки по минту в слотах S..S+5 по порядку;
  4. строим путь цены на +5/+10/+20/+35 секунд от слота лидера и ищем пик;
  5. отмечаем крупные продажи в окне и момент, когда продал сам лидер;
  6. считаем нашу наценку ко входу лидера.

Цена берётся тем же способом, что и в ретро-сигнале
(analysis/solana_retro_signal.py): по дельтам балансов трейдера. В неё
входят проскальзывание и чаевые самого трейдера -- это ограничение
метода, оно одинаково для обеих выборок и потому не мешает их СРАВНИВАТЬ.
"""

from __future__ import annotations

import argparse
import calendar
import json
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from solana_crowd_scan import PUBLIC_RPC, Rpc, helius_key, now_utc, scrub  # noqa: E402
from solana_retro_signal import SlotTrades, log  # noqa: E402

TRADES_PATH = REPO_ROOT / "data" / "solana_trades_all.json"
CHAIN_CACHE_PATH = REPO_ROOT / "data" / "chain_tx_cache.json"
OUT_PATH = REPO_ROOT / "data" / "solana_pilot_block_autopsy.json"

LEADER = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
TASK_NAME = "pointfarmcap"

SLOT_MS = 250                 # с 18.09 слот 250 мс
BACK_SLOTS = 24               # сколько слотов смотрим ДО нашей покупки (6 с)
WINDOW_SLOTS = 5              # окно S..S+5 из задания
PATH_SECONDS = (5, 10, 20, 35)
MAX_PATH_SLOTS = 140          # 35 с при слоте 250 мс
BIG_SELL_SOL = 1.0            # что считаем крупной продажей в окне
LEADER_SELL_WINDOW_S = 300    # "продал ли лидер в пределах 5 минут"


# ---------- выборка ----------

def pilot_closed_trades() -> list[dict]:
    rows = json.loads(TRADES_PATH.read_text())
    out = [r for r in rows
           if r.get("task_name") == TASK_NAME
           and r.get("status") == "закрыта"
           and r.get("source_address") == LEADER
           and r.get("buy_block_time")]
    out.sort(key=lambda r: r["buy_block_time"])
    return out


def pick_samples(rows: list[dict], n: int = 8) -> tuple[list[dict], list[dict], dict]:
    """A -- последняя непрерывная серия минусов; Б -- плюсовые за 19-21.09.

    Серия берётся ФАКТИЧЕСКАЯ, а не по заданным часам: владелец назвал
    окно по местному времени, а в данных время UTC, и подгонять выборку
    под подпись нельзя. Сколько строк реально нашлось -- пишем честно.
    """
    losers: list[dict] = []
    for r in reversed(rows):
        if (r.get("net_sol") or 0) < 0:
            losers.append(r)
        else:
            break
    losers.reverse()
    a = losers[-n:] if len(losers) > n else losers

    # timegm, а не mktime: mktime трактует время как местное, и граница
    # выборки поехала бы на часовой пояс раннера.
    lo = calendar.timegm(time.strptime("2026-09-19T00:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
    hi = calendar.timegm(time.strptime("2026-09-22T00:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
    wins = [r for r in rows if lo <= r["buy_block_time"] < hi and (r.get("net_sol") or 0) > 0]
    wins.sort(key=lambda r: -(r.get("net_sol") or 0))
    b = sorted(wins[:n], key=lambda r: r["buy_block_time"])

    note = {
        "серия_минусов_подряд_найдено": len(losers),
        "в_выборку_A_взято": len(a),
        "плюсовых_19_21_найдено": len(wins),
        "в_выборку_Б_взято": len(b),
        "как_отобрано_Б": "8 крупнейших по net_sol среди плюсовых 19-21.09",
    }
    return a, b, note


# ---------- разбор одной сделки ----------

def our_buy_slot(sig: str, wallet: str, cache: dict) -> int | None:
    v = cache.get(f"{sig}:{wallet}")
    return (v or {}).get("slot")


def scan_window(st: SlotTrades, mint: str, first_slot: int, last_slot: int) -> list[dict]:
    """Все сделки по минту в диапазоне слотов, по возрастанию (слот, индекс)."""
    out = []
    for s in range(first_slot, last_slot + 1):
        blk = st.get(s, mint)
        if blk is None:
            out.append({"slot": s, "недоступен": True})
            continue
        for t in blk["trades"]:
            out.append({**t, "slot": s, "block_time": blk.get("block_time")})
    return out


def last_price_before(events: list[dict], slot: int, index: int) -> float | None:
    """Цена последней СОСТОЯВШЕЙСЯ сделки строго до (slot, index)."""
    best = None
    for e in events:
        if e.get("недоступен") or e.get("price_sol_per_token") is None:
            continue
        if (e["slot"], e["index"]) < (slot, index):
            best = e["price_sol_per_token"]
    return best


def analyse_trade(st: SlotTrades, row: dict, cache: dict, deep_slots: int) -> dict:
    mint = row["mint"]
    wallet = row["wallet"]
    our_slot = our_buy_slot(row["buy_signature"], wallet, cache)
    res: dict = {
        "mint": mint,
        "наша_покупка_utc": time.strftime("%d.%m %H:%M:%SZ", time.gmtime(row["buy_block_time"])),
        "наш_слот": our_slot,
        "наш_вход_sol": row.get("sol_in"),
        "итог_net_sol": row.get("net_sol"),
        "итог_gross_pct": row.get("gross_pct"),
        "держали_с": row.get("held_seconds"),
        "buy_signature": row.get("buy_signature"),
        "sell_signature": row.get("sell_signature"),
    }
    if our_slot is None:
        res["не_удалось"] = "нашей покупки нет в кэше цепочки -- слот неизвестен"
        return res

    # Окно: немного назад (найти лидера и цену до него) и вперёд до +35 с.
    first = our_slot - BACK_SLOTS
    last = our_slot + deep_slots
    events = scan_window(st, mint, first, last)
    live = [e for e in events if not e.get("недоступен")]
    res["блоков_недоступно"] = sum(1 for e in events if e.get("недоступен"))

    lead = next((e for e in live
                 if e["owner"] == LEADER and e["is_buy"] and e["slot"] <= our_slot), None)
    if lead is None:
        res["не_удалось"] = (f"покупку лидера по этому минту не нашли в слотах "
                              f"{first}..{our_slot} -- сравнивать не с чем")
        return res
    S = lead["slot"]
    res["слот_лидера_S"] = S
    res["лидер_sol"] = lead["sol_size"]
    res["лидер_цена"] = lead["price_sol_per_token"]
    res["лидер_signature"] = lead["signature"]
    res["наш_отрыв_слотов"] = our_slot - S

    before = last_price_before(live, S, lead["index"])
    res["цена_до_лидера"] = before
    if before and lead["price_sol_per_token"]:
        res["влияние_лидера_pct"] = round((lead["price_sol_per_token"] / before - 1) * 100, 3)
    else:
        res["влияние_лидера_pct"] = None
        res["почему_нет_влияния"] = ("до сделки лидера в просмотренном окне не было ни одной "
                                      "сделки с посчитанной ценой")

    # Окно S..S+5.
    win = [e for e in live if S <= e["slot"] <= S + WINDOW_SLOTS]
    win.sort(key=lambda e: (e["slot"], e["index"]))
    buys = [e for e in win if e["is_buy"]]
    sells = [e for e in win if not e["is_buy"]]
    res["окно"] = {
        "покупателей": len(buys),
        "продавцов": len(sells),
        "объём_покупок_sol": round(sum(e["sol_size"] for e in buys), 4),
        "объём_продаж_sol": round(sum(e["sol_size"] for e in sells), 4),
        "покупателей_кроме_лидера_и_нас": sum(
            1 for e in buys if e["owner"] not in (LEADER, wallet)),
    }
    # Через сколько слотов пришла основная масса покупателей: медиана
    # смещения по ОБЪЁМУ, а не по числу -- один крупный важнее трёх пылинок.
    others = [e for e in buys if e["owner"] not in (LEADER, wallet)]
    if others:
        vol = sum(e["sol_size"] for e in others)
        acc, half_at = 0.0, None
        for e in sorted(others, key=lambda x: (x["slot"], x["index"])):
            acc += e["sol_size"]
            if half_at is None and acc >= vol / 2:
                half_at = e["slot"] - S
        res["окно"]["половина_объёма_покупок_к_слоту"] = half_at
        res["окно"]["медиана_смещения_покупателя_слотов"] = round(
            statistics.median([e["slot"] - S for e in others]), 1)
    else:
        res["окно"]["половина_объёма_покупок_к_слоту"] = None
        res["окно"]["медиана_смещения_покупателя_слотов"] = None
    res["окно"]["крупные_продажи_sol"] = [
        {"слот_плюс": e["slot"] - S, "sol": round(e["sol_size"], 4),
         "кто": e["owner"][:8] + ".."}
        for e in sells if e["sol_size"] >= BIG_SELL_SOL]

    res["блоки"] = [
        {"слот_плюс": e["slot"] - S, "индекс": e["index"],
         "кто": ("ЛИДЕР" if e["owner"] == LEADER else
                  "МЫ" if e["owner"] == wallet else e["owner"][:6] + ".."),
         "сторона": "покупка" if e["is_buy"] else "продажа",
         "sol": round(e["sol_size"], 4),
         "цена": e["price_sol_per_token"],
         "брак_цены": e.get("price_note")}
        for e in win]

    # Путь цены и пик.
    lead_t = lead.get("block_time")
    priced = [e for e in live if e.get("price_sol_per_token") and e["slot"] >= S]
    priced.sort(key=lambda e: (e["slot"], e["index"]))
    base = lead["price_sol_per_token"]
    path = {}
    for sec in PATH_SECONDS:
        cutoff = S + int(sec * 1000 / SLOT_MS)
        upto = [e for e in priced if e["slot"] <= cutoff]
        path[f"+{sec}с"] = (round((upto[-1]["price_sol_per_token"] / base - 1) * 100, 3)
                             if upto and base else None)
    res["путь_цены_pct_от_лидера"] = path
    if priced and base:
        peak = max(priced, key=lambda e: e["price_sol_per_token"])
        res["пик"] = {
            "слот_плюс": peak["slot"] - S,
            "секунд_от_лидера": round((peak["slot"] - S) * SLOT_MS / 1000, 2),
            "выше_лидера_pct": round((peak["price_sol_per_token"] / base - 1) * 100, 3),
        }
        ours = next((e for e in priced if e["owner"] == wallet and e["is_buy"]), None)
        if ours:
            res["наша_наценка_к_лидеру_pct"] = round(
                (ours["price_sol_per_token"] / base - 1) * 100, 3)
            res["наша_цена"] = ours["price_sol_per_token"]
            res["опоздание_к_пику_слотов"] = ours["slot"] - peak["slot"]
        else:
            res["наша_наценка_к_лидеру_pct"] = None
            res["почему_нет_наценки"] = "нашу покупку в окне с посчитанной ценой не нашли"
    else:
        res["пик"] = None

    # Когда продал сам лидер -- в пределах просмотренного окна.
    lead_sell = next((e for e in live
                      if e["owner"] == LEADER and not e["is_buy"] and e["slot"] > S), None)
    if lead_sell:
        res["лидер_продал"] = {
            "слот_плюс": lead_sell["slot"] - S,
            "секунд_от_входа": round((lead_sell["slot"] - S) * SLOT_MS / 1000, 2),
            "sol": round(lead_sell["sol_size"], 4),
        }
    else:
        covered_s = round(deep_slots * SLOT_MS / 1000, 1)
        res["лидер_продал"] = None
        res["лидер_продал_оговорка"] = (
            f"в просмотренном окне (+{covered_s}с от входа лидера) его продажи нет. "
            f"Окно короче {LEADER_SELL_WINDOW_S}с из задания: глубже смотреть публичным "
            f"узлом в бюджет прогона не влезает, поэтому 'не продал за 5 минут' "
            f"утверждать НЕЛЬЗЯ -- проверено только первые {covered_s}с")
    return res


# ---------- сводка ----------

def _med(vals: list) -> float | None:
    v = [x for x in vals if x is not None]
    return round(statistics.median(v), 3) if v else None


def summarise(label: str, items: list[dict]) -> dict:
    good = [x for x in items if not x.get("не_удалось")]
    return {
        "выборка": label,
        "сделок": len(items),
        "разобрано": len(good),
        "медиана_влияния_лидера_pct": _med([x.get("влияние_лидера_pct") for x in good]),
        "медиана_лидер_sol": _med([x.get("лидер_sol") for x in good]),
        "медиана_покупателей_за_5_блоков": _med(
            [(x.get("окно") or {}).get("покупателей_кроме_лидера_и_нас") for x in good]),
        "медиана_объёма_покупок_sol": _med([(x.get("окно") or {}).get("объём_покупок_sol") for x in good]),
        "медиана_объёма_продаж_sol": _med([(x.get("окно") or {}).get("объём_продаж_sol") for x in good]),
        "медиана_продавцов_в_окне": _med([(x.get("окно") or {}).get("продавцов") for x in good]),
        "медиана_слота_пика": _med([(x.get("пик") or {}).get("слот_плюс") for x in good]),
        "медиана_высоты_пика_pct": _med([(x.get("пик") or {}).get("выше_лидера_pct") for x in good]),
        "медиана_нашей_наценки_pct": _med([x.get("наша_наценка_к_лидеру_pct") for x in good]),
        "медиана_пути_+35с_pct": _med(
            [(x.get("путь_цены_pct_от_лидера") or {}).get("+35с") for x in good]),
        "лидер_продал_в_окне_шт": sum(1 for x in good if x.get("лидер_продал")),
        "медиана_секунд_до_продажи_лидера": _med(
            [(x.get("лидер_продал") or {}).get("секунд_от_входа") for x in good]),
        "сделок_с_крупной_продажей_в_окне": sum(
            1 for x in good if (x.get("окно") or {}).get("крупные_продажи_sol")),
        "медиана_итога_gross_pct": _med([x.get("итог_gross_pct") for x in items]),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--time-budget-s", type=int, default=300 * 60)
    ap.add_argument("--min-interval-s", type=float, default=0.30,
                     help="темп публичного узла: он жёстче Helius")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--deep-slots", type=int, default=MAX_PATH_SLOTS)
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--use-helius", action="store_true",
                     help="ходить в Helius (по умолчанию нет: квота исчерпана)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return

    started = time.monotonic()
    key = ""
    try:
        key, key_name = helius_key()
    except Exception:  # noqa: BLE001
        key_name = "нет"
    rpc = Rpc(key, min_interval_s=args.min_interval_s, workers=args.workers,
              backoff_mult=1.5, backoff_cap=15.0)
    if not args.use_helius:
        rpc.url = PUBLIC_RPC
        log("узел: ПУБЛИЧНЫЙ (Helius без квоты) -- медленнее, темп "
            f"{args.min_interval_s}с")
    else:
        log(f"узел: Helius (ключ из {key_name})")
    rpc.deadline = started + args.time_budget_s
    st = SlotTrades(rpc, capacity=20000)

    rows = pilot_closed_trades()
    a_rows, b_rows, note = pick_samples(rows, args.n)
    log(f"сделок пилота закрытых: {len(rows)}; выборка A={len(a_rows)}, Б={len(b_rows)}")
    log("отбор: " + json.dumps(note, ensure_ascii=False))

    cache = json.loads(CHAIN_CACHE_PATH.read_text())

    out: dict = {
        "generated_at_utc": now_utc(),
        "лидер": LEADER,
        "задача": TASK_NAME,
        "узел": "публичный" if not args.use_helius else "helius",
        "ЧЕСТНЫЕ_ОГОВОРКИ": [
            "Только чтение цепочки: ни одного вызова покупки/продажи.",
            "Цена -- по дельтам балансов ТРЕЙДЕРА (как в ретро-сигнале): в неё входят "
            "проскальзывание и чаевые самого трейдера. Смещение одинаково для обеих "
            "выборок, поэтому сравнению А/Б оно не мешает, а абсолютные цены завышены.",
            f"Продажа лидера ищется только в просмотренном окне (+{round(args.deep_slots * SLOT_MS / 1000, 1)}с), "
            f"а не за {LEADER_SELL_WINDOW_S}с из задания: публичный узел глубже не тянет в бюджет. "
            "Поэтому 'лидер не продал' в этом отчёте НЕ означает 'не продал за 5 минут'.",
            "Слот считается за 250 мс (так с 18.09). Для более ранних сделок пересчёт секунд был бы другим.",
            "Недоступные блоки считаются и печатаются отдельно: пропуск нигде не становится нулём.",
        ],
        "отбор_выборок": note,
    }

    for label, rs, key_out in (("A -- серия минусов", a_rows, "A"),
                                ("Б -- плюсовые 19-21.09", b_rows, "Б")):
        items = []
        for r in rs:
            if rpc.expired():
                items.append({"mint": r["mint"], "не_удалось": "бюджет времени прогона истёк"})
                continue
            try:
                items.append(analyse_trade(st, r, cache, args.deep_slots))
            except RuntimeError as exc:
                items.append({"mint": r["mint"], "не_удалось": scrub(str(exc))[:200]})
            log(f"[{key_out}] {r['mint'][:10]} разобрана; блоков всего "
                f"fetched={st.fetched} skipped={st.skipped} failed={st.failed}")
            OUT_PATH.write_text(json.dumps({**out, "промежуточно": True,
                                             key_out: items}, ensure_ascii=False, indent=2))
        out[key_out] = items
        out[f"сводка_{key_out}"] = summarise(label, items)

    out["rpc"] = {"calls": rpc.calls, "retries": rpc.retries, "errors": rpc.errors,
                   "блоков_получено": st.fetched, "пропущенных_слотов": st.skipped,
                   "блоков_не_отдалось": st.failed, "выбраковано_цен": st.outliers,
                   "минут": round((time.monotonic() - started) / 60, 1)}
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    log("сводка A: " + json.dumps(out.get("сводка_A"), ensure_ascii=False))
    log("сводка Б: " + json.dumps(out.get("сводка_Б"), ensure_ascii=False))


def self_test() -> None:
    checks: list[tuple[str, bool, str]] = []

    def chk(name: str, ok: bool, got: str = "") -> None:
        checks.append((name, bool(ok), got))

    def R(bt, net):
        return {"task_name": TASK_NAME, "status": "закрыта", "source_address": LEADER,
                "buy_block_time": bt, "net_sol": net, "mint": f"m{bt}"}

    # Серия минусов -- ТОЛЬКО непрерывная с конца.
    rows = [R(1, -1.0), R(2, 1.0), R(3, -0.1), R(4, -0.2), R(5, -0.3)]
    a, b, note = pick_samples(rows, n=8)
    chk("серия минусов обрывается на плюсе", [r["buy_block_time"] for r in a] == [3, 4, 5],
        str([r["buy_block_time"] for r in a]))
    chk("длина серии посчитана", note["серия_минусов_подряд_найдено"] == 3)
    a2, _, _ = pick_samples(rows, n=2)
    chk("берём последние n из серии", [r["buy_block_time"] for r in a2] == [4, 5])
    rows_pos = [R(1, 1.0), R(2, 2.0)]
    a3, _, n3 = pick_samples(rows_pos, n=8)
    chk("нет минусов -- пустая выборка A, а не подстановка", a3 == [] and
        n3["серия_минусов_подряд_найдено"] == 0)

    lo_want = calendar.timegm(time.strptime("2026-09-19T00:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
    rows_b = [R(lo_want - 1, 5.0), R(lo_want, 1.0), R(lo_want + 10, 2.0)]
    _, b_edge, _ = pick_samples(rows_b, n=8)
    chk("граница Б по UTC, а не по поясу раннера",
        [r["buy_block_time"] for r in b_edge] == [lo_want, lo_want + 10],
        str([r["buy_block_time"] for r in b_edge]))

    ev = [{"slot": 10, "index": 1, "price_sol_per_token": 1.0},
          {"slot": 10, "index": 5, "price_sol_per_token": 2.0},
          {"slot": 11, "index": 0, "price_sol_per_token": 3.0}]
    chk("цена до -- строго раньше по (слот, индекс)", last_price_before(ev, 10, 5) == 1.0,
        str(last_price_before(ev, 10, 5)))
    chk("цена до включает весь предыдущий слот", last_price_before(ev, 11, 0) == 2.0)
    chk("до первой сделки цены нет", last_price_before(ev, 10, 0) is None)
    chk("брак цены не идёт в 'цену до'",
        last_price_before([{"slot": 1, "index": 0, "price_sol_per_token": None}], 5, 0) is None)

    chk("медиана игнорирует None", _med([1.0, None, 3.0]) == 2.0, str(_med([1.0, None, 3.0])))
    chk("медиана пустого -- None", _med([None, None]) is None)
    chk("+35с это 140 слотов по 250 мс", int(35 * 1000 / SLOT_MS) == MAX_PATH_SLOTS)

    s = summarise("пусто", [{"не_удалось": "нет данных"}])
    chk("нерасшифрованная сделка не попадает в медианы",
        s["разобрано"] == 0 and s["медиана_влияния_лидера_pct"] is None)
    chk("но из счёта не пропадает", s["сделок"] == 1)

    bad = 0
    for name, ok, got in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {name}" + (f"  -> {got}" if got and not ok else ""))
        bad += (not ok)
    print(f"самопроверка разбора: {len(checks) - bad}/{len(checks)} пройдено")
    if bad:
        raise SystemExit(f"самопроверка не пройдена: {bad} из {len(checks)}")


if __name__ == "__main__":
    main()
