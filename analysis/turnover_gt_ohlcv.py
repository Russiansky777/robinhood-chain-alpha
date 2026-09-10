#!/usr/bin/env python3
"""Задача владельца (2026-09-10), обход ограничения DefiLlama для
ретро-сторожка: "Универсум: все пулы с TVL >= $2M по DefiLlama (все
сети, где есть GT-слаг). По каждому -- суточный объём за 90 дней из
GeckoTerminal OHLCV (day, limit=90), объём / ТЕКУЩИЙ TVL. Эпизод = >=3
дня подряд с оборотом > 20x TVL."

РЕАЛЬНАЯ трудность, честно проверяемая живьём, не гадается: DefiLlama
`/yields/pools` даёт `pool` как СВОЙ внутренний UUID (подтверждено
ранее -- 747c1d2a-c668-4682-b9f9-296708a3dd90 для Lido stETH), НЕ
адрес пула в сети и НЕ GT pool-адрес. Чтобы получить суточный объём с
GeckoTerminal, нужен (network, pool_address) для GT. Единственный
реальный мост -- если DefiLlama-запись содержит адреса ТОКЕНОВ пула
(ищем любое поле-список из hex-адресов вида 0x...40hex, имя поля
заранее не угадываем), тогда можно спросить GT "какие пулы содержат
этот токен" (`/networks/{net}/tokens/{addr}/pools`) и найти среди
результатов пул, где ОБА токена совпадают -- это и есть GT pool_address.

Поэтому скрипт СНАЧАЛА (Шаг 0-3, ноль реальных GT-вызовов на полную
выборку) честно проверяет: (а) есть ли вообще такое поле-адрес в
реальной записи DefiLlama, (б) сколько сетей из реальной выборки
сопоставляются с реальным списком сетей GT (`/networks`), (в) на
МАЛОЙ выборке (DIAG_SAMPLE_SIZE) -- реально ли получается по этой
цепочке найти pool_address на GT и вытащить суточный OHLCV. Только
если реальный match rate на выборке выше порога -- переходит к
полному скану всех пулов (с резюмируемым чекпоинтом, ноль кредитов,
только время). Если нет -- честно останавливается и докладывает
реальную причину, не притворяется, что метод работает."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests

import task1_pool_liquidity as gt  # переиспользуем общий gt_get() с ретраями (2026-09-10 рефакторинг)  # noqa: E402

YIELDS_BASE = "https://yields.llama.fi"
GT_BASE = "https://api.geckoterminal.com/api/v2"
TVL_MIN_USD = 2_000_000.0
TURNOVER_MULT_MIN = 20.0
EPISODE_MIN_DAYS = 3
OHLCV_LIMIT_DAYS = 90
DIAG_SAMPLE_SIZE = 15
DIAG_MIN_MATCH_RATE = 0.20  # честный, явно названный порог решения "метод вообще работает"
CHECKPOINT_EVERY = 20
OUT_PATH = Path("data/p3_guard_cache/turnover_gt_ohlcv_result.json")
HEX_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def dl_get(url: str, **kw) -> requests.Response:
    return requests.get(url, timeout=30, **kw)


def fetch_defillama_pools() -> list[dict]:
    r = dl_get(f"{YIELDS_BASE}/pools")
    r.raise_for_status()
    return r.json().get("data", [])


def fetch_gt_networks() -> list[dict]:
    """Реальный список сетей GT, постранично, пока страница не пуста.
    2026-09-10, реальная находка (run 34492555244): GT на странице ЗА
    ПРЕДЕЛАМИ реального диапазона отдаёт не пустой список и не 404, а
    HTTP 400 (наблюдалось на странице 4) -- трактуем как конец пагинации,
    как и 404; любая ДРУГАЯ ошибка (не 400/404) по-прежнему падает
    по-настоящему, не глушим её здесь."""
    out: list[dict] = []
    page = 1
    while True:
        try:
            r = gt.gt_get(f"{GT_BASE}/networks", params={"page": page})
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 400:
                break
            raise
        if r is None or r.status_code == 404:
            break
        data = r.json().get("data", [])
        if not data:
            break
        out.extend(data)
        page += 1
        if page > 20:  # реальная защита от зацикливания, не ожидается на практике
            break
    return out


def find_address_list_field(pool: dict) -> list[str] | None:
    """Ищем В ЛЮБОМ поле записи DefiLlama список из >=2 hex-адресов --
    не угадываем конкретное имя поля заранее (см. модульный docstring)."""
    for key, val in pool.items():
        if isinstance(val, list) and len(val) >= 2:
            addrs = [v for v in val if isinstance(v, str) and HEX_ADDR_RE.match(v)]
            if len(addrs) >= 2:
                return addrs[:2]
    return None


def normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def build_chain_to_gt_network(defillama_chains: set[str], gt_networks: list[dict]) -> dict[str, str]:
    gt_by_norm_name = {normalize(n["attributes"]["name"]): n["id"] for n in gt_networks if n.get("attributes", {}).get("name")}
    gt_by_norm_id = {normalize(n["id"]): n["id"] for n in gt_networks}
    mapping = {}
    for chain in defillama_chains:
        norm = normalize(chain)
        if norm in gt_by_norm_name:
            mapping[chain] = gt_by_norm_name[norm]
        elif norm in gt_by_norm_id:
            mapping[chain] = gt_by_norm_id[norm]
    return mapping


def resolve_pool_address(gt_network_id: str, token_addrs: list[str], defillama_tvl_usd: float) -> tuple[str, float] | None:
    """Ищем пул на GT, реально содержащий ОБА адреса токенов, через
    /tokens/{addr}/pools для первого токена. Если несколько кандидатов
    содержат оба токена -- берём того, чей reserve_in_usd ближе всего
    (по log-шкале) к текущему TVL DefiLlama (честная эвристика выбора
    среди дублей/разных версий пула одной пары, не точная дедупликация)."""
    r = gt.gt_get(f"{GT_BASE}/networks/{gt_network_id}/tokens/{token_addrs[0]}/pools")
    if r is None or r.status_code == 404:
        return None
    candidates = r.json().get("data", [])
    target = token_addrs[1].lower()
    best = None
    best_dist = None
    for c in candidates:
        rel = c.get("relationships", {})
        ids = set()
        for side in ("base_token", "quote_token"):
            tid = rel.get(side, {}).get("data", {}).get("id", "")
            if "_" in tid:
                ids.add(tid.split("_", 1)[1].lower())
        if target not in ids:
            continue
        attrs = c.get("attributes", {})
        addr = attrs.get("address")
        reserve = attrs.get("reserve_in_usd")
        if not addr or not reserve:
            continue
        reserve = float(reserve)
        dist = abs(__import__("math").log((reserve + 1) / (defillama_tvl_usd + 1)))
        if best is None or dist < best_dist:
            best, best_dist = (addr, reserve), dist
    return best


def fetch_ohlcv_day(gt_network_id: str, pool_address: str, limit: int = OHLCV_LIMIT_DAYS) -> list[list] | None:
    r = gt.gt_get(f"{GT_BASE}/networks/{gt_network_id}/pools/{pool_address}/ohlcv/day", params={"aggregate": 1, "limit": limit})
    if r is None or r.status_code == 404:
        return None
    return r.json().get("data", {}).get("attributes", {}).get("ohlcv_list")


def compute_episodes(daily_turnover: list[tuple[str, float]]) -> list[dict]:
    """daily_turnover: [(date_iso, turnover_mult), ...] отсортировано по дате.
    Эпизод = >=EPISODE_MIN_DAYS дней ПОДРЯД с turnover_mult > TURNOVER_MULT_MIN."""
    episodes = []
    run_start = None
    run_peak = 0.0
    run_len = 0
    for date_iso, mult in daily_turnover:
        if mult > TURNOVER_MULT_MIN:
            if run_start is None:
                run_start = date_iso
                run_len = 0
                run_peak = 0.0
            run_len += 1
            run_peak = max(run_peak, mult)
        else:
            if run_start is not None and run_len >= EPISODE_MIN_DAYS:
                episodes.append({"start": run_start, "duration_days": run_len, "peak_turnover_mult": run_peak, "still_alive": False})
            run_start = None
    if run_start is not None and run_len >= EPISODE_MIN_DAYS:
        episodes.append({"start": run_start, "duration_days": run_len, "peak_turnover_mult": run_peak, "still_alive": True})
    return episodes


def _git_checkpoint(message: str) -> None:
    """2026-09-10, реальная находка (run 34493254959): голый `git push`
    без ретрая -- 10/10 реальных чекпоинтов (20..200/516 кандидатов,
    77 минут реальной работы) закоммитились ЛОКАЛЬНО, но НИ ОДИН не
    запушился (`! [rejected] ... fetch first` -- конкурентные пуши от
    параллельного воркфлоу Задачи 4 на ту же ветку), и всё было
    потеряно при отмене джоба. Тот же pull-rebase-retry, что уже
    используется в финальных шагах воркфлоу -- 5 попыток."""
    try:
        subprocess.run(["git", "add", str(OUT_PATH)], check=False)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
        if diff.returncode == 0:
            return
        subprocess.run(["git", "commit", "-m", message], check=False)
        for attempt in range(5):
            push = subprocess.run(["git", "push"], check=False)
            if push.returncode == 0:
                return
            print(f"[turnover_gt_ohlcv] push чекпоинта отклонён, попытка {attempt+1}/5 -- git pull --rebase и повтор")
            subprocess.run(["git", "pull", "--rebase"], check=False)
            time.sleep(3)
        print("[turnover_gt_ohlcv] чекпоинт НЕ запушился после 5 попыток -- прогресс остаётся только локально в раннере, "
              "риск потери при отмене/таймауте (зафиксировано честно, не молчим).")
    except Exception as exc:  # noqa: BLE001
        print(f"[turnover_gt_ohlcv] чекпоинт-коммит не удался (не критично, продолжаем): {exc}")


def run() -> int:
    t0 = time.time()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    result: dict = json.loads(OUT_PATH.read_text()) if OUT_PATH.exists() else {}
    result.setdefault("purpose", "Ретро-сторожок через GT OHLCV (обход отсутствия volume в DefiLlama chart): эпизоды TVL>=$2M пулов с оборотом >20x TVL >=3 дня подряд, 90 дней, 0 кредитов")
    result["generated_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    result["tvl_min_usd"] = TVL_MIN_USD
    result["turnover_mult_min"] = TURNOVER_MULT_MIN
    result["episode_min_days"] = EPISODE_MIN_DAYS

    print("=== Шаг 0: реальный список сетей GeckoTerminal (/networks) ===")
    gt_networks = fetch_gt_networks()
    print(f"[turnover_gt_ohlcv] реальных сетей на GT: {len(gt_networks)}")
    result["n_gt_networks"] = len(gt_networks)

    print("\n=== Шаг 1: реальный снимок DefiLlama /yields/pools, TVL>=$2M ===")
    pools = fetch_defillama_pools()
    big = [p for p in pools if (p.get("tvlUsd") or 0) >= TVL_MIN_USD]
    print(f"[turnover_gt_ohlcv] всего пулов: {len(pools)}, TVL>=${TVL_MIN_USD/1e6:.0f}M: {len(big)}")
    result["n_pools_total_snapshot"] = len(pools)
    result["n_pools_tvl_ge_2m_now"] = len(big)
    result["sample_raw_pool_record"] = big[0] if big else None
    print(f"[turnover_gt_ohlcv] реальные поля первой записи: {sorted(big[0].keys()) if big else 'НЕТ ПУЛОВ'}")

    print("\n=== Шаг 2: поиск поля с адресами токенов в реальной записи DefiLlama ===")
    with_addrs = [(p, find_address_list_field(p)) for p in big]
    n_with_addrs = sum(1 for _, a in with_addrs if a)
    print(f"[turnover_gt_ohlcv] пулов, где нашлось поле-список из >=2 hex-адресов: {n_with_addrs}/{len(big)}")
    result["n_pools_with_address_field"] = n_with_addrs
    if n_with_addrs == 0:
        result["blocker"] = ("РЕАЛЬНОЕ ОГРАНИЧЕНИЕ: ни одна запись /yields/pools (TVL>=$2M) не содержит поля-списка "
                              "из адресов токенов -- мост DefiLlama-пул -> GT pool_address через токены построить "
                              "нечем на этих данных. Полный скан не запускается, не гадаю на суррогате.")
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"\n[turnover_gt_ohlcv] ОСТАНОВЛЕНО: {result['blocker']}")
        return 1

    print("\n=== Шаг 3: реальное сопоставление сетей DefiLlama -> GT network id ===")
    distinct_chains = {p["chain"] for p in big if p.get("chain")}
    chain_map = build_chain_to_gt_network(distinct_chains, gt_networks)
    print(f"[turnover_gt_ohlcv] сетей в выборке: {len(distinct_chains)}, сопоставлено с GT: {len(chain_map)}")
    print(f"[turnover_gt_ohlcv] несопоставленные сети (честно, не гадаем): {sorted(distinct_chains - set(chain_map))[:30]}")
    result["n_distinct_chains"] = len(distinct_chains)
    result["n_chains_mapped_to_gt"] = len(chain_map)
    result["chain_to_gt_network"] = chain_map
    result["chains_unmapped"] = sorted(distinct_chains - set(chain_map))

    candidates = []
    for p, addrs in with_addrs:
        if not addrs or p.get("chain") not in chain_map:
            continue
        candidates.append({"pool_uuid": p["pool"], "project": p.get("project"), "symbol": p.get("symbol"),
                            "chain": p["chain"], "gt_network": chain_map[p["chain"]],
                            "tvl_usd_now": p.get("tvlUsd"), "token_addrs": addrs})
    print(f"\n[turnover_gt_ohlcv] реальных кандидатов (адреса найдены + сеть сопоставлена): {len(candidates)}/{len(big)}")
    result["n_candidates"] = len(candidates)

    if not candidates:
        result["blocker"] = "РЕАЛЬНОЕ ОГРАНИЧЕНИЕ: 0 кандидатов после фильтра адреса+сети -- метод не применим на этой выборке."
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"\n[turnover_gt_ohlcv] ОСТАНОВЛЕНО: {result['blocker']}")
        return 1

    print(f"\n=== Шаг 4 (диагностический гейт, {DIAG_SAMPLE_SIZE} пулов): реально работает ли мост токены->GT-пул->OHLCV? ===")
    diag_sample = candidates[:DIAG_SAMPLE_SIZE]
    diag_ok = 0
    diag_detail = []
    for c in diag_sample:
        try:
            resolved = resolve_pool_address(c["gt_network"], c["token_addrs"], c["tvl_usd_now"])
            if not resolved:
                diag_detail.append({**c, "diag_status": "не найден пул на GT с обоими токенами"})
                continue
            addr, reserve = resolved
            ohlcv = fetch_ohlcv_day(c["gt_network"], addr)
            if not ohlcv:
                diag_detail.append({**c, "diag_status": "пул найден, но OHLCV пуст/недоступен", "gt_pool_address": addr})
                continue
            diag_ok += 1
            diag_detail.append({**c, "diag_status": "OK", "gt_pool_address": addr, "gt_reserve_usd": reserve, "n_ohlcv_days": len(ohlcv)})
        except Exception as exc:  # noqa: BLE001
            diag_detail.append({**c, "diag_status": f"ошибка: {str(exc)[:200]}"})
    match_rate = diag_ok / len(diag_sample)
    print(f"[turnover_gt_ohlcv] реальный match rate на диагностической выборке: {diag_ok}/{len(diag_sample)} = {match_rate:.1%}")
    result["diag_sample_size"] = len(diag_sample)
    result["diag_match_rate"] = match_rate
    result["diag_detail"] = diag_detail
    if match_rate < DIAG_MIN_MATCH_RATE:
        result["blocker"] = (f"РЕАЛЬНЫЙ match rate {match_rate:.1%} ниже порога {DIAG_MIN_MATCH_RATE:.0%} -- метод "
                              "токены->GT-пул ненадёжен на практике, полный скан НЕ запускается (не тратим часы "
                              "на метод, который реально не работает).")
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"\n[turnover_gt_ohlcv] ОСТАНОВЛЕНО: {result['blocker']}")
        return 1

    print(f"\n=== Шаг 5: гейт пройден ({match_rate:.1%} >= {DIAG_MIN_MATCH_RATE:.0%}) -- полный скан {len(candidates)} кандидатов, резюмируемый чекпоинт ===")
    episodes_by_pool = result.get("episodes_by_pool", {})
    processed_ids = set(result.get("processed_pool_uuids", []))
    errors = result.get("resolve_errors", {})
    n_new_processed = 0
    for i, c in enumerate(candidates):
        if c["pool_uuid"] in processed_ids:
            continue
        try:
            resolved = resolve_pool_address(c["gt_network"], c["token_addrs"], c["tvl_usd_now"])
            if resolved:
                addr, reserve = resolved
                ohlcv = fetch_ohlcv_day(c["gt_network"], addr)
                if ohlcv:
                    daily = []
                    for bar in ohlcv:
                        ts, _o, _h, _l, _cl, vol = bar[0], bar[1], bar[2], bar[3], bar[4], bar[5]
                        date_iso = time.strftime("%Y-%m-%d", time.gmtime(ts))
                        mult = (float(vol) / c["tvl_usd_now"]) if c["tvl_usd_now"] else 0.0
                        daily.append((date_iso, mult))
                    daily.sort(key=lambda x: x[0])
                    eps = compute_episodes(daily)
                    if eps:
                        episodes_by_pool[c["pool_uuid"]] = {"project": c["project"], "symbol": c["symbol"], "chain": c["chain"],
                                                             "gt_pool_address": addr, "tvl_usd_now": c["tvl_usd_now"], "episodes": eps}
        except Exception as exc:  # noqa: BLE001
            errors[c["pool_uuid"]] = str(exc)[:200]
        processed_ids.add(c["pool_uuid"])
        n_new_processed += 1
        if n_new_processed % CHECKPOINT_EVERY == 0:
            result["episodes_by_pool"] = episodes_by_pool
            result["processed_pool_uuids"] = sorted(processed_ids)
            result["resolve_errors"] = errors
            result["n_processed_so_far"] = len(processed_ids)
            OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
            _git_checkpoint(f"Ретро-сторожок GT OHLCV: чекпоинт {len(processed_ids)}/{len(candidates)} [automated]")
            print(f"[turnover_gt_ohlcv] чекпоинт: {len(processed_ids)}/{len(candidates)}, эпизодов найдено у {len(episodes_by_pool)} пулов")

    result["episodes_by_pool"] = episodes_by_pool
    result["processed_pool_uuids"] = sorted(processed_ids)
    result["resolve_errors"] = errors
    result["n_processed_so_far"] = len(processed_ids)

    if len(processed_ids) < len(candidates):
        result["status"] = f"НЕЗАВЕРШЕНО: обработано {len(processed_ids)}/{len(candidates)} -- запустить ещё раз, резюмируется с чекпоинта"
        OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"\n[turnover_gt_ohlcv] {result['status']}")
        return 0

    all_episodes = [{"pool_uuid": pid, **e, **{k: v for k, v in info.items() if k != "episodes"}}
                     for pid, info in episodes_by_pool.items() for e in info["episodes"]]
    durations = [e["duration_days"] for e in all_episodes]
    result["summary"] = {
        "n_episodes_total": len(all_episodes),
        "median_duration_days": sorted(durations)[len(durations) // 2] if durations else None,
        "n_still_alive": sum(1 for e in all_episodes if e.get("still_alive")),
    }
    result["status"] = "ЗАВЕРШЕНО"
    result["runtime_s_this_call"] = time.time() - t0
    OUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    print(f"\n[turnover_gt_ohlcv] ИТОГО: {json.dumps(result['summary'], ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
