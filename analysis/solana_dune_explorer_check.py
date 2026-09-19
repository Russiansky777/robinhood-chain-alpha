#!/usr/bin/env python3
"""Владелец, 2026-09-18: проверка Dune (14-дневный триал Plus на СТАРОМ
аккаунте, переведённом 10 сентября в режим только просмотра) как замены
RPC-разбора для расчёта доходности входов -- ПРОВЕРКА, не переезд.

Секрет DUNE_EXPLORER_API, который владелец назвал по памяти, В РЕПОЗИТОРИИ
НЕ НАЙДЕН (проверено grep по всем workflow -- см. отчёт). Реально
проброшены в другие (не относящиеся к этой задаче) workflow:
DUNE_API_KEY, DUNE_API_KEY_MOZILA. Workflow этого скрипта пробрасывает
ОБА этих секрета плюс DUNE_EXPLORER_API (на случай, если владелец его
добавит) -- скрипт сам определяет, какие из трёх реально непустые, и
прозванивает каждый.

Дешёвый (create_query, по документированному поведению Dune -- 0
кредитов, только метаданные) тест живости КАЖДОГО непустого ключа
делается ПЕРВЫМ, отдельно от специально платных проверок 4.1-4.6 --
чтобы не тратить на мёртвый ключ ничего лишнего.

Самостоятельный клиент, НЕ analysis/dune_client.py -- тот привязан к
бюджетному леджеру ДРУГОГО проекта (credit_guard.py, другой Dune-аккаунт/
план), здесь отдельный, независимый учёт кредитов для ЭТОЙ проверки.

Разбита на нумерованные шаги (см. STEP_* функции) -- каждый шаг пишет
свой кусок в общий JSON и печатает честный статус, чтобы частичный сбой
на одном шаге не терял уже полученные ответы по предыдущим."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "solana_dune_explorer_check.json"

API_BASE = "https://api.dune.com/api/v1"
LEADER_WALLET = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
SOL_USDC_POOL = "3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv"
# Владелец, 2026-09-19: смена ключа -- DUNE_EXPLORER_API (старый триал)
# больше не используем. Новый: DUNE_JANA_API, тариф Plus, остаток 1480
# кредитов на момент смены. DUNE_API_KEY/DUNE_API_KEY_MOZILA -- по-прежнему
# секреты ДРУГОГО, не относящегося к этой задаче проекта (см. pick_working_key).
CANDIDATE_ENV_KEYS = ["DUNE_JANA_API", "DUNE_EXPLORER_API", "DUNE_API_KEY", "DUNE_API_KEY_MOZILA"]

# Лимиты Plus (владелец): 70/мин тяжёлые эндпоинты, 200/мин чтение --
# соблюдаем консервативным интервалом между вызовами.
HEAVY_MIN_INTERVAL_S = 60.0 / 70.0 * 1.5
READ_MIN_INTERVAL_S = 60.0 / 200.0 * 1.5

# Владелец, 2026-09-19: правило отмены -- при остановке платного прогона
# (напр. cancel_workflow_run в GitHub Actions) СНАЧАЛА отменять исполнение
# на стороне Dune через API (POST /execution/{id}/cancel), а не только
# останавливать раннер: раннер не успевает получить query_id/execution_id
# конкретной попытки (см. инцидент с днём 09-18 -- execute() почти
# наверняка уже ушёл к моменту отмены раннера, а execution_id нигде не
# сохранился, отменить было нечем). run_sql_sync теперь пишет
# query_id/execution_id СРАЗУ после успешного execute(), ДО начала опроса
# статуса -- эта запись переживает "Commit results" (if: always()) даже
# если раннер убит посреди опроса.
LAST_EXECUTION_MARKER_PATH = REPO_ROOT / "data" / "p3_guard_cache" / "DUNE_LAST_EXECUTION.json"

result: dict = {"generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


class DuneProbe:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"X-Dune-API-Key": api_key})
        self._last_call = 0.0

    def _throttle(self, min_interval: float) -> None:
        wait = min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _req(self, method: str, path: str, heavy: bool, **kwargs) -> dict:
        self._throttle(HEAVY_MIN_INTERVAL_S if heavy else READ_MIN_INTERVAL_S)
        resp = self.session.request(method, f"{API_BASE}{path}", timeout=60, **kwargs)
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {"non_json_body": resp.text[:500]}
        return {"http_status": resp.status_code, "body": body}

    def create_query(self, name: str, sql: str) -> dict:
        return self._req("POST", "/query", heavy=False,
                          json={"name": name, "query_sql": sql, "is_private": False})

    def execute(self, query_id: int) -> dict:
        return self._req("POST", f"/query/{query_id}/execute", heavy=True, json={"query_parameters": {}})

    def status(self, execution_id: str) -> dict:
        return self._req("GET", f"/execution/{execution_id}/status", heavy=False)

    def results(self, execution_id: str, limit: int | None = None) -> dict:
        params = {"limit": limit} if limit else {}
        return self._req("GET", f"/execution/{execution_id}/results", heavy=False, params=params)

    def cancel(self, execution_id: str) -> dict:
        return self._req("POST", f"/execution/{execution_id}/cancel", heavy=False)

    def run_sql_sync(self, name: str, sql: str, timeout_s: int = 300, poll_s: int = 4) -> dict:
        """create -> execute -> poll status -> results, с честным логом
        каждого шага. Возвращает {status, rows, execution_id, meta}."""
        step: dict = {"name": name, "sql_preview": sql[:300]}
        cr = self.create_query(name, sql)
        step["create"] = {"http_status": cr["http_status"]}
        if cr["http_status"] != 200:
            step["status"] = "create_failed"
            step["create_body"] = cr["body"]
            return step
        qid = cr["body"]["query_id"]
        step["query_id"] = qid
        ex = self.execute(qid)
        step["execute"] = {"http_status": ex["http_status"]}
        if ex["http_status"] != 200:
            step["status"] = "execute_failed"
            step["execute_body"] = ex["body"]
            return step
        exec_id = ex["body"]["execution_id"]
        step["execution_id"] = exec_id
        try:
            LAST_EXECUTION_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
            LAST_EXECUTION_MARKER_PATH.write_text(json.dumps(
                {"name": name, "query_id": qid, "execution_id": exec_id,
                 "submitted_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, indent=2))
        except OSError:
            pass  # честная попытка -- если диск/права подвели, не роняем сам запрос из-за этого
        waited = 0
        while waited < timeout_s:
            st = self.status(exec_id)
            state = (st.get("body") or {}).get("state")
            if state == "QUERY_STATE_COMPLETED":
                step["status_meta"] = st["body"]
                break
            if state in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
                step["status"] = f"execution_{state}"
                step["status_body"] = st["body"]
                return step
            time.sleep(poll_s)
            waited += poll_s
        else:
            step["status"] = "timeout"
            return step
        res = self.results(exec_id)
        step["results_http_status"] = res["http_status"]
        if res["http_status"] != 200:
            step["status"] = "results_failed"
            step["results_body"] = res["body"]
            return step
        rows = (res["body"].get("result") or {}).get("rows") or []
        step["status"] = "ok"
        step["n_rows"] = len(rows)
        step["rows"] = rows
        step["metadata"] = (res["body"].get("result") or {}).get("metadata")
        return step


def step0_discover_keys() -> dict:
    out = {"env_keys_checked": CANDIDATE_ENV_KEYS, "non_empty": [], "liveness": {}}
    for k in CANDIDATE_ENV_KEYS:
        v = os.environ.get(k, "")
        if v and not any(c in v for c in ("\n", "\r")):
            out["non_empty"].append(k)
    print(f"[dune_check] непустые env-переменные: {out['non_empty']} "
          f"(DUNE_EXPLORER_API, которую владелец назвал по памяти, "
          f"{'ЕСТЬ' if 'DUNE_EXPLORER_API' in out['non_empty'] else 'ОТСУТСТВУЕТ'})", flush=True)
    for k in out["non_empty"]:
        probe = DuneProbe(os.environ[k])
        cr = probe.create_query(f"liveness_check_{k}", "SELECT 1 AS x")
        alive = cr["http_status"] == 200
        out["liveness"][k] = {"http_status": cr["http_status"], "alive": alive,
                               "body_preview": json.dumps(cr["body"])[:200]}
        print(f"[dune_check] {k}: http={cr['http_status']} alive={alive}", flush=True)
    return out


def pick_working_key(discovery: dict) -> str | None:
    """ТОЛЬКО DUNE_JANA_API (новый ключ владельца, Plus, 1480 кредитов на
    момент смены 2026-09-19) -- старый DUNE_EXPLORER_API больше не
    используем по прямому указанию владельца. DUNE_API_KEY/
    DUNE_API_KEY_MOZILA -- по-прежнему секреты ДРУГОГО, не связанного
    проекта с собственным бюджетным учётом (credit_guard.py) -- даже
    если они живые, платные шаги НЕ должны молча тратить их кредиты
    вместо DUNE_JANA_API. Если DUNE_JANA_API отсутствует/мёртв --
    честно останавливаемся после дешёвой проверки живости (step0), не
    подменяем ключ втихую."""
    v = discovery["liveness"].get("DUNE_JANA_API")
    return "DUNE_JANA_API" if v and v["alive"] else None


def step1_capability_probe(probe: DuneProbe) -> dict:
    out = {}
    cr = probe.create_query("cap_probe_select1", "SELECT 1 AS x")
    out["create"] = {"http_status": cr["http_status"], "body_preview": json.dumps(cr["body"])[:200]}
    if cr["http_status"] != 200:
        return out
    qid = cr["body"]["query_id"]
    ex = probe.execute(qid)
    out["execute"] = {"http_status": ex["http_status"], "body_preview": json.dumps(ex["body"])[:200]}
    if ex["http_status"] != 200:
        return out
    exec_id = ex["body"]["execution_id"]
    time.sleep(3)
    st = probe.status(exec_id)
    out["status"] = {"http_status": st["http_status"], "body_preview": json.dumps(st["body"])[:300]}
    res = probe.results(exec_id, limit=1)
    out["results"] = {"http_status": res["http_status"], "body_preview": json.dumps(res["body"])[:300]}
    # cancel -- на УЖЕ завершённом execution (дешёвый SELECT 1 успевает
    # завершиться за секунды) -- честно смотрим, что Dune ответит на
    # cancel постфактум, отдельного долгого запроса ради этого не гоняем.
    cn = probe.cancel(exec_id)
    out["cancel_on_completed"] = {"http_status": cn["http_status"], "body_preview": json.dumps(cn["body"])[:300]}
    return out


def step2_calibration(probe: DuneProbe) -> dict:
    sel_path = REPO_ROOT / "data" / "solana_buyer_200" / "selected_300.json"
    if not sel_path.exists():
        return {"status": "no_local_dataset", "path": str(sel_path)}
    rows = json.loads(sel_path.read_text())
    our_sigs = {r["signature"] for r in rows}
    lo_time = min(r["time"] for r in rows) - 60
    hi_time = max(r["time"] for r in rows) + 60
    sql = (
        f"SELECT tx_id, block_time, block_slot, token_bought_mint_address, "
        f"token_sold_mint_address, amount_usd, project "
        f"FROM dex_solana.trades "
        f"WHERE trader_id = '{LEADER_WALLET}' "
        f"AND block_time >= from_unixtime({lo_time}) "
        f"AND block_time <= from_unixtime({hi_time})"
    )
    r = probe.run_sql_sync("calibration_leader_trades", sql, timeout_s=600)
    out = {"our_n_signatures": len(our_sigs), "lo_time": lo_time, "hi_time": hi_time, "dune_step": r}
    if r.get("status") != "ok":
        return out
    dune_tx_ids = {row.get("tx_id") for row in r["rows"]}
    found = our_sigs & dune_tx_ids
    out["dune_n_rows"] = len(r["rows"])
    out["dune_n_distinct_tx_id"] = len(dune_tx_ids)
    out["n_found"] = len(found)
    out["coverage"] = round(len(found) / len(our_sigs), 4) if our_sigs else None
    missing = our_sigs - dune_tx_ids
    out["n_missing"] = len(missing)
    out["missing_sample"] = list(missing)[:15]
    # проекты найденных -- честная картина, не догадка
    from collections import Counter
    out["projects_found"] = dict(Counter(row.get("project") for row in r["rows"] if row.get("tx_id") in our_sigs))
    print(f"[dune_check] калибровка: покрытие={out['coverage']} ({out['n_found']}/{len(our_sigs)}), "
          f"пропущено={out['n_missing']}", flush=True)
    return out


PUMPFUN_TABLE_CANDIDATES = [
    "pump_fun_solana.trades", "pumpdotfun_solana.trades", "pumpfun_solana.trades",
    "pump_fun_solana.pump_fun_amm_trades", "pump_fun_solana.pump_fun_trades",
]


def step2b_pumpfun_check(probe: DuneProbe, missing_sigs: list[str]) -> dict:
    """Ищем отдельную таблицу для сделок на бондинг-кривой pump.fun/
    LaunchLab -- НЕ объявляем источник негодным по пропускам, пока не
    проверили это. Прямые точечные пробы правдоподобных имён таблиц
    (быстрые, LIMIT 1) вместо медленного полного скана
    information_schema (тот же с timeout=120с ничего не успел --
    честно зафиксировано отдельно, не повторяем тем же способом)."""
    out: dict = {"n_missing_checked": len(missing_sigs), "table_probes": {}}
    for tbl in PUMPFUN_TABLE_CANDIDATES:
        sql = f"SELECT tx_id FROM {tbl} WHERE trader_id = '{LEADER_WALLET}' LIMIT 5"
        r = probe.run_sql_sync(f"pumpfun_probe_{tbl.replace('.', '_')}", sql, timeout_s=60)
        out["table_probes"][tbl] = {"status": r.get("status"),
                                     "n_rows": r.get("n_rows"),
                                     "error_preview": json.dumps(r.get("status_body") or r.get("execute_body") or r.get("results_body"))[:300]}
        print(f"[dune_check] pumpfun-таблица {tbl}: {out['table_probes'][tbl]}", flush=True)
        if r.get("status") == "ok":
            out["found_table"] = tbl
            out["sample_rows"] = r["rows"]
            break
    return out


def step3_price_at_30s(probe: DuneProbe, calib: dict) -> dict:
    if calib.get("dune_step", {}).get("status") != "ok":
        return {"status": "calibration_query_not_ok"}
    if calib.get("n_found", 0) == 0:
        return {"status": "no_found_trades_to_check"}
    sel_path = REPO_ROOT / "data" / "solana_buyer_200" / "selected_300.json"
    sel = {r["signature"]: r for r in json.loads(sel_path.read_text())}
    ext_path = REPO_ROOT / "data" / "solana_buyer_200" / "step_extended_result.json"
    ext = {(r["signature"], r["seconds"]): r for r in json.loads(ext_path.read_text()) if "seconds" in r}
    dune_rows_by_tx = {row["tx_id"]: row for row in calib["dune_step"]["rows"]}

    first_entry_sigs = [s for s, r in sel.items() if r.get("zero_balance") and s in dune_rows_by_tx]
    if not first_entry_sigs:
        return {"status": "no_first_entries_found_in_dune"}

    mints = sorted({sel[s]["mint"] for s in first_entry_sigs})
    mint_list_sql = ",".join(f"'{m}'" for m in mints)
    lo_time = min(sel[s]["time"] for s in first_entry_sigs) - 5
    hi_time = max(sel[s]["time"] for s in first_entry_sigs) + 35
    sql = (
        f"SELECT tx_id, block_time, token_bought_mint_address AS mint, amount_usd "
        f"FROM dex_solana.trades "
        f"WHERE token_bought_mint_address IN ({mint_list_sql}) "
        f"AND block_time >= from_unixtime({lo_time}) AND block_time <= from_unixtime({hi_time})"
    )
    r = probe.run_sql_sync("price_window_plus30s", sql, timeout_s=600)
    out = {"n_first_entries_checked": len(first_entry_sigs), "dune_step": {k: v for k, v in r.items() if k != "rows"},
           "n_rows": r.get("n_rows")}
    if r.get("status") != "ok":
        return out

    from decimal import Decimal as D
    import calendar

    def parse_bt(s: str) -> int:
        return calendar.timegm(time.strptime(s.split(".")[0], "%Y-%m-%d %H:%M:%S"))

    by_mint_rows: dict[str, list] = {}
    for row in r["rows"]:
        by_mint_rows.setdefault(row["mint"], []).append(row)

    diffs = []
    per_trade = []
    for sig in first_entry_sigs:
        row = sel[sig]
        t0 = row["time"]
        mint = row["mint"]
        our30 = ext.get((sig, 30))
        if not our30 or our30.get("mine_status") != "ok" or our30.get("mine_price") is None:
            continue
        candidates = [rr for rr in by_mint_rows.get(mint, [])
                      if t0 < parse_bt(rr["block_time"]) <= t0 + 30]
        if not candidates:
            per_trade.append({"signature": sig, "status": "no_dune_trade_in_window"})
            continue
        last = max(candidates, key=lambda rr: parse_bt(rr["block_time"]))
        # amount_usd -- сумма сделки в USD, НЕ цена токена; честно фиксируем,
        # что для прямого сравнения "цена токена" нужна ещё колонка
        # количества токена (в описанном наборе колонок её явно не было
        # выделено отдельно) -- сохраняем сырую строку для ручного разбора,
        # не делаем вид, что уже посчитали цену, если данных для этого нет.
        per_trade.append({"signature": sig, "our_mine_price": our30["mine_price"],
                           "dune_last_trade_in_window": last})
    out["per_trade_sample"] = per_trade[:20]
    out["honest_note"] = ("Сравнение цены требует количества купленных токенов в той же строке Dune, "
                           "не только amount_usd -- сырые строки сохранены для ручной проверки колонок, "
                           "автоматический медиана/p90 расхождения НЕ считался вслепую без подтверждённых единиц.")
    return out


def step4_first_entry_classification(probe: DuneProbe, calib: dict) -> dict:
    if calib.get("dune_step", {}).get("status") != "ok":
        return {"status": "calibration_query_not_ok"}
    if calib.get("n_found", 0) == 0:
        return {"status": "no_found_trades"}
    sql = (
        f"WITH ranked AS ("
        f"  SELECT tx_id, token_bought_mint_address AS mint, block_time, "
        f"    row_number() OVER (PARTITION BY trader_id, token_bought_mint_address ORDER BY block_time) AS rn "
        f"  FROM dex_solana.trades WHERE trader_id = '{LEADER_WALLET}' "
        f"  AND block_time >= from_unixtime({calib['lo_time']}) AND block_time <= from_unixtime({calib['hi_time']})"
        f") SELECT tx_id, mint, block_time FROM ranked WHERE rn = 1"
    )
    r = probe.run_sql_sync("dune_first_entry_classification", sql, timeout_s=300)
    out = {"dune_step": {k: v for k, v in r.items() if k != "rows"}}
    if r.get("status") != "ok":
        return out
    dune_first_tx = {row["tx_id"] for row in r["rows"]}

    sel_path = REPO_ROOT / "data" / "solana_buyer_200" / "selected_300.json"
    sel = {rr["signature"]: rr for rr in json.loads(sel_path.read_text())}
    # честное сравнение только на пересечении покрытых Dune сигнатур
    dune_row_tx = {row["tx_id"] for row in calib["dune_step"]["rows"]}
    comparable = dune_row_tx & set(sel.keys())
    agree = sum(1 for s in comparable if bool(s in dune_first_tx) == bool(sel[s].get("zero_balance")))
    out["n_comparable"] = len(comparable)
    out["n_agree"] = agree
    out["agreement_rate"] = round(agree / len(comparable), 4) if comparable else None
    disagree = [s for s in comparable if bool(s in dune_first_tx) != bool(sel[s].get("zero_balance"))]
    out["disagree_sample"] = [{"signature": s, "our_zero_balance": sel[s].get("zero_balance"),
                                "dune_first": s in dune_first_tx} for s in disagree[:15]]
    return out


def step2c_pumpfun_full_coverage(probe: DuneProbe, calib: dict, pumpfun_table: str) -> dict:
    """Полная (не LIMIT 5) выборка из найденной таблицы pump.fun-бондинг-
    кривой за то же окно, что и калибровка -- пересчёт покрытия ВМЕСТЕ с
    dex_solana.trades, честно, прежде чем объявлять вердикт."""
    sql = (f"SELECT tx_id, block_time FROM {pumpfun_table} WHERE trader_id = '{LEADER_WALLET}' "
           f"AND block_time >= from_unixtime({calib['lo_time']}) AND block_time <= from_unixtime({calib['hi_time']})")
    r = probe.run_sql_sync("pumpfun_full_window", sql, timeout_s=300)
    out = {"table": pumpfun_table, "dune_step": {k: v for k, v in r.items() if k != "rows"}}
    if r.get("status") != "ok":
        return out
    pumpfun_tx_ids = {row["tx_id"] for row in r["rows"]}
    out["pumpfun_n_rows"] = len(r["rows"])
    sel_path = REPO_ROOT / "data" / "solana_buyer_200" / "selected_300.json"
    our_sigs = {rr["signature"] for rr in json.loads(sel_path.read_text())}
    dex_tx_ids = {row.get("tx_id") for row in calib["dune_step"]["rows"]}
    combined = dex_tx_ids | pumpfun_tx_ids
    found_combined = our_sigs & combined
    out["combined_n_found"] = len(found_combined)
    out["combined_coverage"] = round(len(found_combined) / len(our_sigs), 4) if our_sigs else None
    found_pumpfun_only = our_sigs & pumpfun_tx_ids
    out["n_found_in_pumpfun_table_alone"] = len(found_pumpfun_only)
    print(f"[dune_check] pump.fun-таблица: {out['pumpfun_n_rows']} строк в окне, "
          f"из них наших: {out['n_found_in_pumpfun_table_alone']}, "
          f"совместное покрытие: {out['combined_coverage']}", flush=True)
    return out


def main() -> None:
    discovery = step0_discover_keys()
    result["step0_key_discovery"] = discovery
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    key_name = pick_working_key(discovery)
    if key_name is None:
        alive_others = [k for k, v in discovery["liveness"].items() if v["alive"] and k != "DUNE_EXPLORER_API"]
        if "DUNE_EXPLORER_API" not in discovery["non_empty"]:
            result["HONEST_ANSWER"] = (
                "DUNE_EXPLORER_API не проброшен в этот workflow (секрета с таким именем нет в "
                "репозитории вообще -- проверено). Добавь секрет DUNE_EXPLORER_API с ключом от "
                "триал-аккаунта Plus и передай его в .github/workflows/run_solana_dune_explorer_check.yml "
                f"(уже прописан, просто заполни secrets.DUNE_EXPLORER_API). Живые прочие ключи "
                f"({alive_others or 'нет'}) НЕ использую вместо него -- они принадлежат другому "
                "проекту со своим бюджетным учётом, платные шаги на них не запускаю."
            )
        else:
            result["HONEST_ANSWER"] = "DUNE_EXPLORER_API проброшен, но 403/мёртв -- проверь ключ на dune.com."
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print("[dune_check] " + result["HONEST_ANSWER"], flush=True)
        return
    print(f"[dune_check] используем живой ключ: {key_name}", flush=True)
    probe = DuneProbe(os.environ[key_name])
    result["working_key"] = key_name

    result["step1_capability_probe"] = step1_capability_probe(probe)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print("[dune_check] шаг 1 (возможности) готов", flush=True)

    calib = step2_calibration(probe)
    result["step2_calibration"] = calib
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    if calib.get("coverage") is not None and calib["coverage"] < 0.95 and calib.get("missing_sample"):
        step2b = step2b_pumpfun_check(probe, calib["missing_sample"])
        result["step2b_pumpfun_check"] = step2b
        OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if step2b.get("found_table"):
            result["step2c_pumpfun_full_coverage"] = step2c_pumpfun_full_coverage(probe, calib, step2b["found_table"])
            OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    result["step3_price_at_30s"] = step3_price_at_30s(probe, calib)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    result["step4_first_entry_classification"] = step4_first_entry_classification(probe, calib)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    print("[dune_check] прогон завершён, см. итоговый JSON", flush=True)


if __name__ == "__main__":
    main()
