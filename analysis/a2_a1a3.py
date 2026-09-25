#!/usr/bin/env python3
"""D: A1/A3 по всем сигналам кэша толпы при входах S+0/S+1/S+2 --
исправленной моделью издержек (analysis/a2_common.py).

Считается ПОСЛЕ того, как A2 сошёлся: модель издержек берётся из выгрузки
data/a2_model_vs_chain.json, то есть ровно та, что сведена с цепью, а не
подобранная здесь заново.

ЧЕМ ОТЛИЧАЕТСЯ ОТ ПРЕДЫДУЩЕГО ПРОГОНА (data/night_edge.json). Там входы
(i)/(ii)/(iii) численно СОВПАДАЛИ: цены на границе слотов S+1/S+2 в кэше
нет, а интерполировать запрещено. Теперь входы различаются не
интерполяцией, а ИЗМЕРЕННОЙ НАЦЕНКОЙ нашей фактической цены входа к цене
"сразу за источником" (spot_after), посчитанной по цепи по фактическим
ценам исполнения наших сделок и разложенной по смещению блока
(S+1/S+2/S+3). Числа и объёмы выборок -- в выгрузке; они маленькие (n=4 и
n=3), и это сказано прямо, а не спрятано.

S+0 (наша покупка в ТОМ ЖЕ блоке, что и сделка источника) измерить по
нашим сделкам не удалось: ни одна наша покупка с известной ценой
исполнения в блок источника не попала. Поэтому S+0 считается двумя
строками: наценка 0 (оптимистичная нижняя граница -- ровно то, что делала
исходная модель) и наценка ЧУЖИХ покупателей, попавших в блок источника за
ним (измерено по цепи, выгрузка data/a2_followers_exec_markup.json). Вторая
строка помечена "справочно": это чужие боты с чаевыми в десятки SOL,
которые двигают цену сами, а не мы.

ОКНО 7 СУТОК, А НЕ 14. Задача просит 14 дней. Сохранённый кэш толпы
покрывает 2026-09-17T16:02:53Z -- 2026-09-24T16:02:53Z, то есть 7 суток.
Расширить окно нечем: HELIUS_API_KEY в окружении отсутствует, публичные
RPC Solana закрыты сетевой политикой окружения, а другого сохранённого
сканирования толпы за более раннее время в репозитории нет. Вторая неделя
НЕ достраивается оценкой.

Статистика (свод / без N лучших / бутстрэп среднего) берётся функциями из
analysis/night_edge_model.py -- та же методика и тот же сид, что в
предыдущем прогоне, чтобы числа были сравнимы.

Запуск: python3 analysis/a2_a1a3.py [--crowd PATH] [--out data/a2_a1a3.json]
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from analysis import a2_common as A  # noqa: E402
from analysis.night_edge_model import (  # noqa: E402
    build_signal, свод, без_n_лучших, бутстрэп_среднего, вывод_по_интервалу,
    window_midpoint, split_half, SEED, N_BOOT,
)

DATA = REPO_ROOT / "data"
DEFAULT_OUT = DATA / "a2_a1a3.json"
РАЗМЕР_SOL = 0.2          # размер владельца из условия задачи
ГОРИЗОНТЫ = ("t28_8", "t60s", "t10s", "t5m", "until_source_first_sell")


def рост_горизонта(sig: dict, horizon: str) -> tuple[float | None, str | None]:
    if horizon == "t28_8":
        g = sig["growth_after_30s"]
        return g, (None if g is not None else
                   "нет growth_after_30s (ближайшая к 28.8 с реальная точка кэша)")
    if horizon == "t60s":
        g = sig["growth_after_60s"]
        return g, (None if g is not None else "нет growth_after_60s в кэше")
    if horizon == "t10s":
        return None, "в кэше нет цены на 10 с (только 0/30/60 с), интерполяция запрещена"
    if horizon == "t5m":
        return None, "в кэше нет цены на 5 мин (только 0/30/60 с), интерполяция запрещена"
    return None, "в кэше нет времени и цены первой продажи источника"


def a1(signals: list, входы: dict, изд: A.Издержки, скидка_выхода: float,
       переводов_налоговых: int) -> dict:
    """По каждому сигналу -- чистый результат на каждом горизонте и для
    каждого варианта входа. Строки в том же виде, что у compute_a1
    night_edge_model, чтобы статистику считал тот же код."""
    out = {}
    for имя, нац in входы.items():
        порча = (1.0 + нац["наценка"]) / (1.0 - скидка_выхода)
        изд_входа = A.Издержки(изд.фикс_покупки, изд.фикс_продажи, изд.доля_площадки,
                               порча, изд.комиссия_пула_bps, изд.название)
        rows = []
        for sig in signals:
            row = {"signature": sig["signature"], "source": sig["source_remark"],
                   "source_address": sig["source_address"],
                   "block_time": sig.get("block_time"), "block_time_utc": sig["block_time_utc"],
                   "mint": sig["mint"], "entry_price_spot_after": sig["spot_after"],
                   "наценка_входа": нац["наценка"]}
            известно, ставка = A.налог_сделки(sig["mint"], sig["quote_mint"],
                                              sig["_minty"])
            for h in ГОРИЗОНТЫ:
                g, почему = рост_горизонта(sig, h)
                if sig["spot_after"] is None:
                    row[h] = {"net_sol": None, "missing": True,
                              "why_not": "нет spot_after -- цена входа неизвестна"}
                    continue
                if g is None:
                    row[h] = {"net_sol": None, "missing": True, "why_not": почему}
                    continue
                if not известно:
                    row[h] = {"net_sol": None, "missing": True,
                              "why_not": "налоговый статус минта неизвестен "
                                         "(нет в transfer_fee_audit) -- это НЕ ноль"}
                    continue
                net, w = A.чистый_результат_sol(
                    g, РАЗМЕР_SOL, изд_входа, ставка_налога_доля=ставка,
                    налоговых_переводов=(переводов_налоговых if ставка else 0))
                row[h] = {"net_sol": (round(net, 8) if net is not None else None),
                          "missing": net is None, "why_not": w}
            rows.append(row)
        out[имя] = rows
    return out


def a3_блок(rows: list, horizon: str, mid: float | None, signals: list) -> dict:
    """Свод + бутстрэп + без 3/5 лучших + половины окна. Функции те же,
    что в night_edge_model, сид тот же."""
    блок = {"all": свод(rows, horizon), "bootstrap_mean": бутстрэп_среднего(rows, horizon)}
    блок["verdict"] = вывод_по_интервалу(блок["bootstrap_mean"])
    блок["excl_top3"] = свод(без_n_лучших(rows, horizon, 3), horizon)
    блок["excl_top5"] = свод(без_n_лучших(rows, horizon, 5), horizon)
    if mid is not None:
        by_sig = {r["signature"]: r for r in rows}
        первая, вторая = split_half(signals, mid)
        r1 = [by_sig[s["signature"]] for s in первая if s["signature"] in by_sig]
        r2 = [by_sig[s["signature"]] for s in вторая if s["signature"] in by_sig]
        блок["first_half_window"] = свод(r1, horizon)
        блок["first_half_bootstrap"] = бутстрэп_среднего(r1, horizon)
        блок["second_half_window"] = свод(r2, horizon)
        блок["second_half_bootstrap"] = бутстрэп_среднего(r2, horizon)
    return блок


def a3_по_источникам(rows: list, horizon: str, порог: int = 3) -> list:
    групп = collections.defaultdict(list)
    for r in rows:
        групп[r["source"] or r["source_address"]].append(r)
    out = []
    for имя, v in групп.items():
        s = свод(v, horizon)
        if s["n_trades"] < порог:
            out.append({"источник": имя, "сделок_с_числом": s["n_trades"],
                        "почему": f"меньше {порог} сделок с посчитанным результатом -- "
                                  "не выводим, это не ноль"})
            continue
        out.append({"источник": имя, **s, "bootstrap_mean": бутстрэп_среднего(v, horizon),
                    "excl_top3": свод(без_n_лучших(v, horizon, 3), horizon)})
    out.sort(key=lambda x: -(x.get("sum_sol") or -1e9))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--crowd", default=str(DATA / "a2_crowd_metric_2026-09-24.json"))
    ap.add_argument("--a2", default=str(DATA / "a2_model_vs_chain.json"))
    ap.add_argument("--followers", default=str(DATA / "a2_followers_exec_markup.json"))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()
    t0 = time.time()

    a2 = A.load_json(args.a2)
    if not a2:
        raise SystemExit(f"нет {args.a2} -- сначала A2 (analysis/a2_model_vs_chain.py)")
    crowd = A.load_json(args.crowd)
    if not crowd:
        raise SystemExit(f"нет кэша толпы {args.crowd}")
    aud = A.load_json(DATA / "solana_transfer_fee_audit.json") or {}
    minty = aud.get("минты") or {}
    leg = A.load_json(DATA / "c2_leg_pools_2026-09-24.json") or {}
    depth = {k: v.get("sol_depth") for k, v in (leg.get("pools") or {}).items()
             if isinstance(v, dict)}

    flat = A.плоский_кэш_толпы(crowd)
    signals = []
    for raw in flat:
        s = build_signal(raw, minty, depth)
        s["_minty"] = minty
        signals.append(s)

    изд_наши = a2["издержки_модели_после"]["наши"]
    изд = A.Издержки(изд_наши["фикс_покупки_sol"], изд_наши["фикс_продажи_sol"],
                     изд_наши["доля_площадки"], 1.0, изд_наши["комиссия_пула_bps"],
                     изд_наши["название"])
    исп = a2["исполнение_по_цепи"]
    скидка = исп["скидка_выхода_медиана"] or 0.0
    по_смещению = исп["по_смещению_блока"]
    переводов = 2
    шаги = a2.get("налоговых_переводов_по_шагам") or {}
    меды = [v["медиана"] for v in шаги.values() if v.get("медиана")]
    if меды:
        переводов = int(round(statistics.median(меды)))

    fol = A.load_json(args.followers) or {}
    наценка_чужих = fol.get("медиана_наценки")

    входы = {
        "S+0_нижняя_граница": {
            "наценка": 0.0,
            "откуда": "ни одной нашей покупки в блоке источника с известной ценой "
                      "исполнения нет -- вход по spot_after без наценки, это "
                      "оптимистичная нижняя граница (так считала исходная модель)"},
        "S+1": {
            "наценка": (по_смещению.get("1") or {}).get("медиана_наценки") or 0.0,
            "откуда": f"измерено по цепи на наших сделках, попавших в блок S+1, "
                      f"n={(по_смещению.get('1') or {}).get('n')}"},
        "S+2": {
            "наценка": (по_смещению.get("2") or {}).get("медиана_наценки") or 0.0,
            "откуда": f"измерено по цепи на наших сделках в блоке S+2, "
                      f"n={(по_смещению.get('2') or {}).get('n')}"},
    }
    if наценка_чужих is not None:
        входы["S+0_по_чужим_справочно"] = {
            "наценка": наценка_чужих,
            "откуда": f"СПРАВОЧНО: наценка ЧУЖИХ покупателей, попавших в блок источника "
                      f"за ним, n={fol.get('n')}; это не мы -- у них чаевые в десятки SOL "
                      f"и они двигают цену сами"}

    rows_by_entry = a1(signals, входы, изд, скидка, переводов)
    mid = window_midpoint({"window_from_utc": crowd.get("window_from_utc"),
                           "window_to_utc": crowd.get("window_to_utc")})

    a3 = {}
    по_ист = {}
    for имя, rows in rows_by_entry.items():
        a3[имя] = {h: a3_блок(rows, h, mid, signals) for h in ГОРИЗОНТЫ}
        по_ист[имя] = {h: a3_по_источникам(rows, h) for h in ("t28_8", "t60s")}

    out = {
        "schema_version": 1,
        "сформировано_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "секунд": round(time.time() - t0, 2),
        "КРЕДИТОВ_HELIUS": 0,
        "ЧЕСТНЫЕ_ОГОВОРКИ": [
            "Окно 7 суток, а не 14: сохранённый кэш толпы покрывает "
            f"{crowd.get('window_from_utc')} -- {crowd.get('window_to_utc')}. "
            "Расширить нечем: ключа Helius в окружении нет, публичные RPC закрыты "
            "сетевой политикой окружения. Вторая неделя НЕ достроена оценкой.",
            "Входы S+0/S+1/S+2 различаются ИЗМЕРЕННОЙ наценкой нашей цены входа к "
            "spot_after по смещению блока, а не интерполяцией цены. Выборки маленькие "
            "(S+1 n=4, S+2 n=3) -- это предел сохранённых данных.",
            "S+0 по нашим сделкам не измерен ни разу; дана нижняя граница (наценка 0) и "
            "отдельная справочная строка по чужим покупателям в блоке источника.",
            "Размер сделки 0.2 SOL -- число владельца. На этом размере фиксированные "
            "издержки (чаевые, приоритетная комиссия, аренда токен-аккаунта) весят "
            "заметно больше, чем доля площадки.",
            "Комиссия пула (swap fee AMM) не измерена и в модель не подставлена -- "
            "объявленный пробел; настоящий результат ниже посчитанного на её величину.",
            "Налоговый статус минта 'неизвестно' НЕ равен 'без налога': такие сигналы "
            "помечены missing и не входят ни в среднее, ни в сумму, ни в долю в плюс.",
            "Горизонты 10 с / 5 мин / до первой продажи источника в кэше отсутствуют "
            "целиком и помечены missing на всех входах.",
            "Число налоговых переводов на круг для будущего сигнала неизвестно; взята "
            f"медиана, измеренная по цепи на сведённых сделках: {переводов}. "
            "На многоножных маршрутах по цепи бывает до 8 -- там результат хуже.",
            "Издержки продажи для наших покупок взяты по измерению DBot: кэша цепи по "
            "нашим подписям продажи в репозитории нет. Это подстановка, а не измерение "
            "нашей площадки.",
        ],
        "источники": {"кэш_толпы": args.crowd, "A2": args.a2,
                      "наценка_чужих": args.followers,
                      "аудит_налога": "data/solana_transfer_fee_audit.json",
                      "глубина_ног": "data/c2_leg_pools_2026-09-24.json"},
        "окно": {k: crowd.get(k) for k in ("window_from_utc", "window_to_utc", "window_days")},
        "размер_sol": РАЗМЕР_SOL,
        "издержки_модели": изд.как_словарь() | {"скидка_выхода": скидка,
                                                "налоговых_переводов": переводов},
        "входы": входы,
        "сигналов_всего": len(signals),
        "бутстрэп": {"n_boot": N_BOOT, "seed": SEED},
        "A3": a3,
        "A3_по_источникам": по_ист,
        "A1": {имя: rows for имя, rows in rows_by_entry.items()},
    }
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    краткое = {"окно": out["окно"], "сигналов_всего": out["сигналов_всего"],
               "входы": {k: {"наценка": round(v["наценка"], 4)} for k, v in входы.items()},
               "A3_t28_8": {k: {"all": v["t28_8"]["all"],
                                "bootstrap": v["t28_8"]["bootstrap_mean"],
                                "excl_top3": v["t28_8"]["excl_top3"],
                                "excl_top5": v["t28_8"]["excl_top5"]}
                            for k, v in a3.items()}}
    print(json.dumps(краткое, ensure_ascii=False, indent=2))
    print(f"выгрузка: {args.out}")


if __name__ == "__main__":
    main()
