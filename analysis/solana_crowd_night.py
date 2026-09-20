#!/usr/bin/env python3
"""Владелец: п.2 больше не зависит от внешних пробуждений -- самоходный
процесс на GitHub Actions (cron */10 мин, concurrency-группа не даёт
запустить второй экземпляр, пока идёт первый). Каждый запуск читает
data/night_status.json и продолжает с места остановки, работает не
дольше TIME_BUDGET_S, затем сохраняет статус и выходит -- следующий
тик cron продолжит.

Стадии: control -> scan -> done|stopped. control -- ОДНА попытка на
одних и тех же 15 реальных исторических покупках лидера из фазы 3
(data/solana_crowd_control_set.json), не на свежих живых -- владелец
явно просил не тратить вторую попытку в старом виде. Прошёл -> сразу
scan (без остановки); не прошёл -> stopped с полной таблицей сравнения
(подпись/Dune/новый метод/разница) в night_status.json. scan --
кошельки из data/solana_fomo_passed.json по одному, результаты
дописываются в data/solana_fomo_crowd.json по ходу (один процесс,
конкурентного доступа нет -- concurrency-группа гарантирует это)."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solana_buyer200_fast_price as fp  # noqa: E402
from solana_crowd_common import (  # noqa: E402
    analyze_purchase, analyze_wallet, median, purchase_age_minutes, wallet_purchases,
    MAX_PURCHASES_PER_WALLET_SCAN,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
STATUS_PATH = REPO_ROOT / "data" / "night_status.json"
CROWD_PATH = REPO_ROOT / "data" / "solana_fomo_crowd.json"
PASSED_PATH = REPO_ROOT / "data" / "solana_fomo_passed.json"
CONTROL_SET_PATH = REPO_ROOT / "data" / "solana_crowd_control_set.json"
BREZ_CONTROL_SET_PATH = REPO_ROOT / "data" / "solana_crowd_brez_control_set.json"

LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
BREZ = "Fvkc2thk1YcAASdR2gi8uf9n67JW9Dqqr9iRd99MDhoB"
BREZ_MIN_SOL = 2.0
BREZ_MAX_PURCHASES = 8
TIME_BUDGET_S = 20 * 60
MIN_PRICED_EVENTS = 10
MIN_SIGN_AGREEMENT = 0.8
MAX_MEDIAN_ABS_DIFF_PP = 10.0
BREZ_CONTROL_V2_TIME_SLICE_S = 5 * 60  # владелец, п.5: параллельно, не блокирует скан --
# небольшой срез бюджета за тик, не более, пока все 17 подписей не будут обработаны


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run_url() -> str | None:
    server, repo, rid = (os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{server}/{repo}/actions/runs/{rid}" if server and repo and rid else None


def load_status() -> dict:
    if STATUS_PATH.exists():
        try:
            return json.loads(STATUS_PATH.read_text())
        except (ValueError, OSError):
            pass
    return {"stage": "control", "attempts": 0, "control": {}, "done_wallets": 0,
            "total_wallets": 0, "updated_utc": None, "last_error": None, "run_url": None}


def save_status(status: dict) -> None:
    status["updated_utc"] = now_utc()
    status["run_url"] = run_url()
    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2, default=str))


def sign(x: float) -> int:
    return (x > 0) - (x < 0)


FOLLOW_ORDERS_PATH = REPO_ROOT / "data" / "dbot_follow_orders_raw.json"
LIVE_TASK_NAMES = ("BATCH-3", "BATCH-5", "BATCH-6", "BATCH-7")


def load_live_task_sources() -> dict[str, str]:
    """Адрес источника -> имя задачи, только для живых задач BATCH-3/5/6/7
    (владелец, п.3: эти сканируются первыми). Файл обновляется отдельным
    почасовым конвейером учёта (ledger_hourly) в этой же рабочей ветке --
    здесь просто читается, без обращения к DBot API."""
    if not FOLLOW_ORDERS_PATH.exists():
        return {}
    try:
        data = json.loads(FOLLOW_ORDERS_PATH.read_text())
    except (ValueError, OSError):
        return {}
    out: dict[str, str] = {}
    for t in data.get("tasks", []):
        if t.get("name") not in LIVE_TASK_NAMES:
            continue
        for s in t.get("sources") or []:
            addr = s.get("address")
            if addr:
                out.setdefault(addr, t["name"])
    return out


def load_ranked_wallets() -> list[dict]:
    rows = []
    for line in PASSED_PATH.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    live_sources = load_live_task_sources()
    for r in rows:
        r["stands_in"] = live_sources.get(r["address"], "очередь")
    # Владелец, п.3: сначала кошельки, стоящие в живых задачах BATCH-3/5/6/7
    # (в порядке ранга Fomo внутри этой группы), затем остальные -- тоже
    # по рангу Fomo.
    rows.sort(key=lambda r: (0 if r["stands_in"] != "очередь" else 1, r.get("fomo_rank", 10**9)))
    return rows


def load_control_set() -> list[dict]:
    return json.loads(CONTROL_SET_PATH.read_text())["events"]


def run_brez_diagnostic(ctrl: dict, deadline: float) -> None:
    """Владелец, п.3: сырые 8 покупок Brez -- токен, возраст токена на
    момент покупки, цена входа, цена через 30с, число чужих покупок в
    окне. Диагностика, НЕ участвует в принятии/отклонении контроля."""
    diag = ctrl.setdefault("brez_raw", {"rows": [], "partial": False, "done": False})
    if diag.get("done"):
        return
    purchases = wallet_purchases(BREZ, BREZ_MIN_SOL, BREZ_MAX_PURCHASES)
    seen = {r["signature"] for r in diag["rows"]}
    for e in purchases:
        if e["signature"] in seen:
            continue
        if time.monotonic() > deadline:
            diag["partial"] = True
            print("[crowd_night] Brez-диагностика: бюджет исчерпан, таблица будет неполной", flush=True)
            return
        r = analyze_purchase(e, BREZ)
        age_min = purchase_age_minutes(e)
        price_plus30s = None
        if not r.get("empty") and not r.get("decode_fail") and r.get("growth_pct_30s") is not None and r.get("price_source"):
            price_plus30s = round(r["price_source"] * (1 + r["growth_pct_30s"] / 100.0), 12)
        row = {
            "signature": e["signature"], "mint": e.get("mint"),
            "token_age_min_at_purchase": age_min,
            "price_source": r.get("price_source"),
            "price_plus30s": price_plus30s,
            "n_other_buys": r.get("n_other_buys"),
            "empty": r.get("empty"), "decode_fail": r.get("decode_fail", False),
            "not_a_purchase": r.get("not_a_purchase", False),
            "HONEST_NOTE": r.get("HONEST_NOTE"),
        }
        diag["rows"].append(row)
        print(f"[crowd_night] Brez: {e['signature'][:12]}.. возраст={age_min}мин "
              f"цена_входа={row['price_source']} цена+30с={row['price_plus30s']} "
              f"n_чужих_покупок={row['n_other_buys']}", flush=True)
    diag["done"] = True


def run_control(status: dict, deadline: float) -> None:
    """Владелец: контроль на ОДНИХ И ТЕХ ЖЕ 15 реальных исторических
    покупках лидера (data/solana_crowd_control_set.json -- подпись и
    рост к +30с по Dune, фаза 3), не на свежих живых покупках. Новый
    метод (analyze_purchase) прогоняется на тех же подписях. Принято при
    n_приценённых>=MIN_PRICED_EVENTS, доле совпадения знака
    >=MIN_SIGN_AGREEMENT и медиане |разницы| <=MAX_MEDIAN_ABS_DIFF_PP
    п.п. Одна попытка -- владелец явно просил не тратить вторую в старом
    виде: не прошла -> сразу stage=stopped с таблицей целиком."""
    ctrl = status.setdefault("control", {})
    rows = ctrl.setdefault("rows", {})
    events = load_control_set()
    for ev in events:
        sig = ev["signature"]
        if sig in rows:
            continue
        if time.monotonic() > deadline:
            print("[crowd_night] контроль: бюджет исчерпан, продолжу со следующего тика", flush=True)
            return
        print(f"[crowd_night] контроль: {sig[:12]}.. ({ev['day']}, Dune={ev['dune_growth_pct_30s']}%)...", flush=True)
        entry = {"signature": sig, "mint": ev["mint"], "slot": ev["slot"],
                 "spend_sol_equiv": ev.get("spend_sol_equiv")}
        r = analyze_purchase(entry, LEADER_WALLET)
        new_growth = r.get("growth_pct_30s")
        rows[sig] = {
            "signature": sig, "day": ev["day"], "mint": ev["mint"],
            "dune_growth_pct_30s": ev["dune_growth_pct_30s"],
            "new_method_growth_pct_30s": new_growth,
            "diff_pp": round(new_growth - ev["dune_growth_pct_30s"], 4) if new_growth is not None else None,
            "decode_fail": r.get("decode_fail", False),
            "not_a_purchase": r.get("not_a_purchase", False),
            "empty": r.get("empty"),
            "HONEST_NOTE": r.get("HONEST_NOTE"),
        }
        print(f"[crowd_night]   Dune={rows[sig]['dune_growth_pct_30s']}% новый={rows[sig]['new_method_growth_pct_30s']} "
              f"diff={rows[sig]['diff_pp']}", flush=True)

    if len(rows) < len(events):
        return

    priced = [r for r in rows.values() if r["new_method_growth_pct_30s"] is not None]
    n_priced = len(priced)
    matches = sum(1 for r in priced if sign(r["new_method_growth_pct_30s"]) == sign(r["dune_growth_pct_30s"]))
    sign_agreement = (matches / n_priced) if n_priced else 0.0
    diffs = [abs(r["diff_pp"]) for r in priced]
    median_abs_diff = median(diffs)

    ok = (n_priced >= MIN_PRICED_EVENTS and sign_agreement >= MIN_SIGN_AGREEMENT
          and median_abs_diff is not None and median_abs_diff <= MAX_MEDIAN_ABS_DIFF_PP)

    ctrl["n_priced"] = n_priced
    ctrl["n_total_events"] = len(events)
    ctrl["sign_agreement"] = round(sign_agreement, 4)
    ctrl["median_abs_diff_pp"] = round(median_abs_diff, 4) if median_abs_diff is not None else None
    ctrl["table"] = sorted(rows.values(), key=lambda r: (r["day"], r["signature"]))
    status["attempts"] = status.get("attempts", 0) + 1

    print(f"[crowd_night] КОНТРОЛЬ: n_приценённых={n_priced}/{len(events)} "
          f"совпадение_знака={sign_agreement:.2%} медиана|разницы|={ctrl['median_abs_diff_pp']}п.п.", flush=True)

    run_brez_diagnostic(ctrl, deadline)

    if ok:
        status["stage"] = "scan"
        status["last_error"] = None
        print("[crowd_night] КОНТРОЛЬ ПРОЙДЕН (новая методика, одна попытка) -> stage=scan", flush=True)
    else:
        status["stage"] = "stopped"
        status["last_error"] = (
            f"контроль не прошёл: n_приценённых={n_priced} (нужно >={MIN_PRICED_EVENTS}), "
            f"совпадение_знака={sign_agreement:.2%} (нужно >={MIN_SIGN_AGREEMENT:.0%}), "
            f"медиана|разницы|={ctrl['median_abs_diff_pp']}п.п. (нужно <={MAX_MEDIAN_ABS_DIFF_PP}п.п.)")
        print(f"[crowd_night] СТОП: {status['last_error']}", flush=True)


def load_brez_control_set() -> list[dict]:
    return json.loads(BREZ_CONTROL_SET_PATH.read_text())["events"]


def run_brez_control_v2(status: dict, deadline: float) -> None:
    """Владелец, п.5: дополнительный контроль ПАРАЛЛЕЛЬНО скану, не
    блокирующий -- те же 17 подписей Brez из фазы 3 (first_entry=true,
    приценённые Dune, data/solana_crowd_brez_control_set.json), новым
    методом. НЕ влияет на прошёл/не прошёл (контроль уже засчитан
    владельцем) -- только таблица подпись/Dune/новый метод/разница,
    прикладывается к итогу. Небольшой срез бюджета за тик
    (BREZ_CONTROL_V2_TIME_SLICE_S), чтобы не задерживать скан 90
    кошельков -- честная имитация "параллельно": один процесс, но скан
    получает основную часть бюджета каждый тик."""
    bc = status.setdefault("brez_control_v2", {"rows": {}, "done": False})
    if bc.get("done"):
        return
    rows = bc.setdefault("rows", {})
    events = load_brez_control_set()
    slice_deadline = min(deadline, time.monotonic() + BREZ_CONTROL_V2_TIME_SLICE_S)
    # Владелец, найдено на реальном прогоне: один зависший RPC-вызов внутри
    # analyze_purchase раньше мог утянуть за собой ВЕСЬ 20-минутный бюджет
    # тика (мягкий дедлайн rpc_call был привязан к общему deadline). Здесь
    # -- собственный, более тесный дедлайн на срез Brez-контроля, чтобы
    # зависшая подпись стоила не больше BREZ_CONTROL_V2_TIME_SLICE_S, а не
    # всего тика; после среза дедлайн RPC возвращается к общему -- скану
    # достаётся вся оставшаяся часть бюджета.
    fp.set_soft_deadline(slice_deadline)
    try:
        for ev in events:
            sig = ev["signature"]
            if sig in rows:
                continue
            if time.monotonic() > slice_deadline:
                print("[crowd_night] Brez-контроль (п.5): срез бюджета на этот тик исчерпан, продолжу позже", flush=True)
                return
            entry = {"signature": sig, "mint": ev["mint"], "slot": ev["slot"],
                     "spend_sol_equiv": ev.get("spend_sol_equiv")}
            r = analyze_purchase(entry, BREZ)
            new_growth = r.get("growth_pct_30s")
            rows[sig] = {
                "signature": sig, "day": ev["day"], "mint": ev["mint"],
                "dune_growth_pct_30s": ev["dune_growth_pct_30s"],
                "new_method_growth_pct_30s": new_growth,
                "diff_pp": round(new_growth - ev["dune_growth_pct_30s"], 4) if new_growth is not None else None,
                "decode_fail": r.get("decode_fail", False),
                "not_a_purchase": r.get("not_a_purchase", False),
                "empty": r.get("empty"),
                "HONEST_NOTE": r.get("HONEST_NOTE"),
            }
            print(f"[crowd_night] Brez-контроль (п.5): {sig[:12]}.. Dune={rows[sig]['dune_growth_pct_30s']}% "
                  f"новый={rows[sig]['new_method_growth_pct_30s']} diff={rows[sig]['diff_pp']}", flush=True)
    finally:
        fp.set_soft_deadline(deadline)

    if len(rows) >= len(events):
        priced = [r for r in rows.values() if r["new_method_growth_pct_30s"] is not None]
        bc["n_priced"] = len(priced)
        bc["n_total_events"] = len(events)
        bc["table"] = sorted(rows.values(), key=lambda r: (r["day"], r["signature"]))
        bc["done"] = True
        print(f"[crowd_night] Brez-контроль (п.5) завершён: n_приценённых={len(priced)}/{len(events)}", flush=True)


def run_scan(status: dict, deadline: float) -> None:
    ranked = load_ranked_wallets()
    status["total_wallets"] = len(ranked)
    crowd = json.loads(CROWD_PATH.read_text()) if CROWD_PATH.exists() else {}
    for row in ranked:
        addr = row["address"]
        if addr in crowd:
            continue
        if time.monotonic() > deadline:
            print("[crowd_night] scan: бюджет исчерпан, продолжу со следующего тика", flush=True)
            break
        r = analyze_wallet(addr, row.get("name"), min_sol=2.0, max_purchases=MAX_PURCHASES_PER_WALLET_SCAN,
                            deadline=deadline)
        if r.get("wallet_budget_cut"):
            if time.monotonic() > deadline:
                print(f"[crowd_night] {row.get('name')} ({addr[:10]}..): бюджет кончился на этом кошельке "
                      f"(n_приценено={r['n_priced']}, n_decode_fail={r['n_decode_fail']}) -- не сохраняю, "
                      f"продолжу со следующего тика", flush=True)
                break
            # Владелец: RPC-сбой на ЭТОМ кошельке (не исчерпание бюджета
            # тика, время ещё есть) -- не бросаем весь тик, пропускаем
            # кошелёк и идём дальше, next тик подхватит его снова.
            print(f"[crowd_night] {row.get('name')} ({addr[:10]}..): RPC-сбой при получении покупок "
                  f"(бюджет ещё есть) -- пропускаю, перехожу к следующему кошельку", flush=True)
            continue
        r["fomo_rank"] = row.get("fomo_rank")
        r["stands_in"] = row.get("stands_in")
        crowd[addr] = r
        CROWD_PATH.write_text(json.dumps(crowd, ensure_ascii=False, indent=2, default=str))
        print(f"[crowd_night] {row.get('name')} ({addr[:10]}.., {row.get('stands_in')}): "
              f"n_всего={r['n_purchases_total']} n_приценено={r['n_priced']} n_decode_fail={r['n_decode_fail']} "
              f"медиана_роста={r['median_growth_pct_30s']} empty_share={r['empty_share']} "
              f"({len(crowd)}/{len(ranked)})", flush=True)
    status["done_wallets"] = len(crowd)
    if len(crowd) >= len(ranked):
        status["stage"] = "done"
        print("[crowd_night] ВСЕ 90 ГОТОВЫ -- stage=done", flush=True)


def main() -> None:
    status = load_status()
    if status["stage"] in ("done", "stopped"):
        print(f"[crowd_night] stage={status['stage']} -- работа не требуется, выхожу без изменений", flush=True)
        save_status(status)
        return

    # Найдено при разборе зависаний: без привилегированного RPC (публичный
    # узел, ~8 запросов/с целевой темп + троттлинг) один прогон не
    # укладывался ни в TIME_BUDGET_S, ни в 25-минутный таймаут job'а --
    # HELIUS_API уже подтверждён рабочим и быстрым в этом репозитории.
    privileged_rpc = fp.alchemy_available()
    print(f"[crowd_night] привилегированный RPC активен: {privileged_rpc} "
          f"(HELIUS_API={'есть' if os.environ.get('HELIUS_API') else 'нет'}, "
          f"ALCHEMY_API_KEY={'есть' if os.environ.get('ALCHEMY_API_KEY') else 'нет'})", flush=True)

    deadline = time.monotonic() + TIME_BUDGET_S
    # Владелец, найдено на реальном прогоне 2026-09-20: без этого один
    # зависший RPC-вызов (rpc_call: до 20 попыток x потолок 45с = до 25
    # минут) убивал ВЕСЬ job таймаутом раньше, чем внутренний 20-минутный
    # цикл успевал заметить дедлайн -- ни одна покупка/кошелёк даже не
    # сохранялись. Теперь rpc_call сам обрывается по этому дедлайну.
    fp.set_soft_deadline(deadline)
    try:
        if status["stage"] == "control":
            run_control(status, deadline)
        if status["stage"] == "scan" and time.monotonic() < deadline:
            run_brez_control_v2(status, deadline)  # владелец, п.5: не блокирует scan -- маленький срез, потом scan
        if status["stage"] == "scan" and time.monotonic() < deadline:
            run_scan(status, deadline)
    except Exception as exc:  # noqa: BLE001
        status["last_error"] = f"{type(exc).__name__}: {exc}"
        print(f"[crowd_night] ОШИБКА в этом тике (не критично, следующий тик продолжит): {status['last_error']}", flush=True)
    save_status(status)


if __name__ == "__main__":
    main()
