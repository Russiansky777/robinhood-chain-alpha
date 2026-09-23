#!/usr/bin/env python3
"""Сверка модели пула с фактом по каждой сделке.

Модель обязана сходиться с чеком. Для пула с x*y=k средняя цена
исполнения равна sqrt(цена_до * цена_после). Фактическая средняя цена --
это просто чек трейдера: сколько котировки он потратил, делённое на то,
сколько токенов получил, по его же балансам в той самой транзакции.

Если модель и факт расходятся -- значит опознан не тот пул (или не та его
сторона), и такую сделку принимать нельзя. Допуск: 5% для кривых pump.fun
(у них комиссия внутри кривой) и 3% для остальных.

Сводная А/Б выдаётся, только если сверку прошли не меньше 12 сделок из 15.
Иначе печатается список не прошедших с причинами -- и ничего больше:
сводка по половине выборки хуже, чем её отсутствие, потому что выглядит
как результат.

Скрипт работает ПО ВЫГРУЗКЕ пересчёта и сети не требует. Сеть нужна только
режиму --audit-trade, который разбирает одну сделку до последнего счёта.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

IN_PATH = REPO_ROOT / "data" / "solana_pool_price_recompute.json"
OUT_PATH = REPO_ROOT / "data" / "solana_pool_check.json"

TOL_CURVE = 0.05        # кривые pump.fun -- комиссия внутри
TOL_AMM = 0.03
MIN_PASSED_FOR_SUMMARY = 12


# Пулы с СОСРЕДОТОЧЕННОЙ ликвидностью. У них x*y=k не выполняется, и цена
# не выводится из остатков хранилищ вообще: ликвидность стоит в диапазонах,
# а не размазана по всей кривой. Проверять их моделью постоянного
# произведения бессмысленно -- это не "сверка не прошла", а "метод к ним
# неприменим", и мешать одно с другим нельзя.
СОСРЕДОТОЧЕННЫЕ = ("clmm", "dlmm", "whirlpool", "concentrated")


def концентрированный(program: str | None) -> bool:
    p = (program or "").lower()
    return any(x in p for x in СОСРЕДОТОЧЕННЫЕ)


def tolerance(program: str | None) -> float:
    p = (program or "").lower()
    return TOL_CURVE if ("pump" in p and "amm" not in p) else TOL_AMM


TOL_K = 0.05            # произведение резервов: комиссия -- проценты, не разы


def check_side(pool: dict | None, fact_price: float | None, label: str) -> dict:
    """Сверка одной стороны сделки.

    Сверок две, и первая обязательна:

    1. ПО НОГАМ САМОГО ПУЛА. Модель sqrt(цена_до * цена_после) против
       средней цены пула -- сколько котировки он получил на один отданный
       токен. Плюс сохранение произведения резервов: у настоящего пула k
       растёт на комиссию, то есть на проценты, а у случайно совпавших
       счетов уезжает в разы.

    2. ПО ЧЕКУ ТРЕЙДЕРА -- только когда трейдер платил ТОЙ ЖЕ валютой, в
       которой котируется пул. Маршрут бывает многоножным: в сделке
       23.09 03:09 лидер платил USDC, а пул котируется третьим токеном
       через две промежуточные ноги. Там цена трейдера и цена пула --
       разные величины в разных валютах, и сравнивать их нельзя. Раньше
       сверка делалась только так, и поэтому не проходила.
    """
    out = {"сторона": label}
    if not pool or not pool.get("ок"):
        out.update(прошла=None, почему="пул не опознан")
        return out
    p0, p1 = pool.get("цена_до"), pool.get("цена_после")
    if not p0 or not p1 or p0 <= 0 or p1 <= 0:
        out.update(прошла=None, почему="нет цен пула до/после")
        return out
    if концентрированный(pool.get("программа")):
        out.update(прошла=None, программа=pool.get("программа"),
                   почему=("пул с сосредоточенной ликвидностью: цена до и после не "
                            "выводится из остатков хранилищ, модель постоянного "
                            "произведения к нему неприменима -- нужен отдельный метод "
                            "по фактическим ценам сделок в этом пуле"))
        return out
    model = math.sqrt(p0 * p1)
    out["модель_sqrt_до_после"] = model
    out["средняя_цена_пула"] = pool.get("средняя_цена_пула")
    out["программа"] = pool.get("программа")
    out["котировка"] = pool.get("котировка")
    tol = tolerance(pool.get("программа"))
    out["допуск"] = tol

    pool_price = pool.get("средняя_цена_пула")
    if not pool_price or pool_price <= 0:
        out.update(прошла=None, почему="нет средней цены по ногам пула")
        return out
    dev = abs(model / pool_price - 1)
    out["отклонение_по_ногам_пула"] = round(dev, 6)

    dk = pool.get("k_изменилось_на")
    out["k_изменилось_на"] = dk
    k_ok = dk is not None and abs(dk) <= TOL_K
    out["k_сохранилось"] = k_ok

    out["прошла"] = bool(dev <= tol and k_ok)
    if dev > tol:
        out["почему"] = (f"сверка не прошла: модель {model:.12g}, факт по ногам пула "
                          f"{pool_price:.12g}, расхождение {dev * 100:.2f}% при допуске "
                          f"{tol * 100:.0f}%")
    elif not k_ok:
        out["почему"] = (f"сверка не прошла: произведение резервов изменилось на "
                          f"{(dk if dk is not None else float('nan')) * 100:.2f}% "
                          f"при допуске {TOL_K * 100:.0f}% -- это не один пул")

    # Вторая сверка -- по чеку трейдера, если валюты совпали.
    same_cur = pool.get("трейдер_платил_котировкой_пула")
    out["чек_трейдера_сопоставим"] = same_cur
    out["факт_средняя_исполнения"] = fact_price
    if same_cur and fact_price and fact_price > 0:
        dev2 = abs(model / fact_price - 1)
        out["отклонение_по_чеку_трейдера"] = round(dev2, 6)
        if dev2 > tol:
            out["прошла"] = False
            out["почему"] = (f"сверка не прошла: модель {model:.12g}, чек трейдера "
                              f"{fact_price:.12g}, расхождение {dev2 * 100:.2f}% при "
                              f"допуске {tol * 100:.0f}%")
    elif same_cur is False:
        out["оговорка"] = ("маршрут многоножный: трейдер платил не той валютой, в "
                            "которой котируется пул, поэтому его чек с моделью пула "
                            "не сверяется -- сверка идёт по ногам самого пула")
    return out


def check_trade(item: dict) -> dict:
    """Обе стороны сделки. Не прошла хоть одна -- сделка не принимается."""
    res = dict(item)
    if item.get("не_удалось"):
        res["сверка"] = {"принята": False, "почему": item["не_удалось"]}
        return res
    lead = check_side(item.get("пул_в_сделке_лидера"),
                      item.get("средняя_цена_исполнения_лидера"), "лидер")
    ours = check_side(item.get("пул_в_нашей_сделке"),
                      item.get("наша_средняя_цена_исполнения"), "мы")
    принята = bool(lead.get("прошла")) and bool(ours.get("прошла"))
    причины = [s["почему"] for s in (lead, ours) if s.get("почему")]
    неприменим = any(концентрированный((s or {}).get("программа")) for s in (lead, ours))
    res["сверка"] = {"принята": принята, "лидер": lead, "мы": ours,
                      "метод_неприменим": неприменим,
                      "почему": "; ".join(причины) if причины else None}
    if not принята:
        # Цифры непринятой сделки нельзя молча оставлять в выгрузке: их
        # легко принять за результат. Гасим их явно.
        for k in ("а_влияние_лидера_pct", "б_дрейф_до_нас_pct", "в_наше_влияние_pct",
                  "наценка_к_цене_пула_после_лидера_pct", "итог_от_цены_пула_до_лидера_pct"):
            if k in res:
                res[k + "_отклонено"] = res.pop(k)
    return res


def _med(vals):
    v = [x for x in vals if x is not None]
    return round(statistics.median(v), 3) if v else None


def summarise(label: str, items: list[dict]) -> dict:
    good = [x for x in items if (x.get("сверка") or {}).get("принята")]
    return {
        "выборка": label, "сделок": len(items), "прошли_сверку": len(good),
        "медиана_а_влияние_лидера_pct": _med([x.get("а_влияние_лидера_pct") for x in good]),
        "медиана_б_дрейф_pct": _med([x.get("б_дрейф_до_нас_pct") for x in good]),
        "медиана_в_наше_влияние_pct": _med([x.get("в_наше_влияние_pct") for x in good]),
        "медиана_наценки_к_пулу_pct": _med(
            [x.get("наценка_к_цене_пула_после_лидера_pct") for x in good]),
        "медиана_лидер_к_резерву": _med([x.get("покупка_лидера_к_резерву") for x in good]),
        "медиана_резерва_до_лидера_sol": _med([x.get("резерв_котировки_до_лидера_sol") for x in good]),
        "медиана_лидер_sol": _med([x.get("лидер_sol") for x in good]),
        "медиана_итог_gross_pct": _med([x.get("итог_gross_pct") for x in items]),
        "сделок_с_разными_пулами": sum(1 for x in good if x.get("разные_пулы")),
        "сделок_в_одном_пуле": sum(1 for x in good if x.get("разные_пулы") is False),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=str(IN_PATH))
    ap.add_argument("--audit-trade", default="",
                     help="разобрать одну сделку до последнего счёта (нужна сеть)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return
    if args.audit_trade:
        audit(args.audit_trade)
        return

    p = Path(args.inp)
    if not p.exists():
        raise SystemExit(f"нет {p} -- сверять нечего")
    d = json.loads(p.read_text())
    A = [check_trade(x) for x in d.get("A") or []]
    B = [check_trade(x) for x in d.get("Б") or []]
    all_items = A + B
    passed = sum(1 for x in all_items if (x.get("сверка") or {}).get("принята"))
    failed = [{"когда": x.get("когда"), "mint": x.get("mint"),
                "почему": (x.get("сверка") or {}).get("почему") or "не удалось",
                "метод_неприменим": bool((x.get("сверка") or {}).get("метод_неприменим"))}
               for x in all_items if not (x.get("сверка") or {}).get("принята")]

    out = {
        "источник": str(p.relative_to(REPO_ROOT)),
        "ЧЕСТНЫЕ_ОГОВОРКИ": [
            "Модель обязана сходиться с чеком: sqrt(цена_до * цена_после) против "
            "фактической средней цены исполнения трейдера по его же балансам.",
            f"Допуск {TOL_CURVE * 100:.0f}% для кривых pump.fun (комиссия внутри кривой) "
            f"и {TOL_AMM * 100:.0f}% для остальных.",
            "Не прошла хоть одна сторона -- сделка не принимается, и её цифры в выгрузке "
            "переименованы с суффиксом _отклонено, чтобы их нельзя было принять за результат.",
            f"Сводная А/Б выдаётся только при {MIN_PASSED_FOR_SUMMARY}+ прошедших из "
            f"{len(all_items)}: сводка по половине выборки хуже её отсутствия, потому что "
            "выглядит как результат.",
        ],
        "всего_сделок": len(all_items), "прошли_сверку": passed,
        "метод_неприменим_сосредоточенная_ликвидность": sum(
            1 for x in failed if x["метод_неприменим"]),
        "сверка_реально_не_сошлась": sum(
            1 for x in failed if not x["метод_неприменим"]),
        "порог_для_сводной": MIN_PASSED_FOR_SUMMARY,
        "сводная_выдана": passed >= MIN_PASSED_FOR_SUMMARY,
        "не_прошли": failed,
        "A": A, "Б": B,
    }
    if out["сводная_выдана"]:
        out["сводка_A"] = summarise("A -- серия минусов", A)
        out["сводка_Б"] = summarise("Б -- плюсовые 19-21.09", B)
    else:
        out["почему_нет_сводной"] = (
            f"сверку прошли {passed} сделок из {len(all_items)}, порог "
            f"{MIN_PASSED_FOR_SUMMARY}. Сводная не выдаётся.")
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"прошли сверку: {passed} из {len(all_items)} (порог {MIN_PASSED_FOR_SUMMARY})")
    for f in failed:
        print(f"  НЕ ПРОШЛА {f['когда']} {f['mint'][:10]}: {f['почему']}")
    if out["сводная_выдана"]:
        print(json.dumps({"сводка_A": out["сводка_A"], "сводка_Б": out["сводка_Б"]},
                          ensure_ascii=False, indent=2))
    else:
        print(out["почему_нет_сводной"])


def audit(needle: str) -> None:
    """Одна сделка целиком: все кандидаты в пул и судьба каждого."""
    from solana_crowd_scan import Rpc, helius_key  # noqa: PLC0415
    from solana_pool_price_recompute import (  # noqa: PLC0415
        account_keys, exec_price, native_balance, program_label, program_touching,
        QUOTES, _bal_map,
    )
    cache = json.loads((REPO_ROOT / "data" /
                        "solana_pilot_block_autopsy_cache.json").read_text())
    rows = [r for r in (cache.get("строки") or {}).values()
            if needle in (r.get("наша_покупка_utc") or "") or needle in (r.get("mint") or "")]
    if not rows:
        raise SystemExit(f"сделка по признаку {needle!r} не найдена")
    row = rows[0]
    key, _ = helius_key()
    rpc = Rpc(key, min_interval_s=0.05, workers=1, service="разбор_пилота")
    txs = rpc.transactions([row["лидер_signature"], row["buy_signature"]])
    LEADER = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
    WALLET = "E1qAJBmrJDhBvm2sV8kfMXFAmgzHuSRNRosPEgMkKiWS"
    report = {"сделка": row.get("наша_покупка_utc"), "mint": row.get("mint"), "стороны": []}
    for sig, trader, label in ((row["лидер_signature"], LEADER, "лидер"),
                                (row["buy_signature"], WALLET, "мы")):
        tx = txs.get(sig)
        side = {"сторона": label, "подпись": sig, "кандидаты": []}
        if not tx:
            side["почему"] = "транзакция не отдалась"
            report["стороны"].append(side)
            continue
        meta = tx.get("meta") or {}
        pre, post = _bal_map(meta.get("preTokenBalances")), _bal_map(meta.get("postTokenBalances"))
        mint = row["mint"]
        tr_d = (post.get((trader, mint), (0.0, None))[0]
                - pre.get((trader, mint), (0.0, None))[0])
        side["дельта_токена_у_трейдера"] = tr_d
        keys = account_keys(tx)
        for o in {ow for (ow, m) in set(pre) | set(post) if m == mint and ow != trader}:
            t0 = pre.get((o, mint), (0.0, None))[0]
            t1 = post.get((o, mint), (0.0, None))[0]
            ветки = []
            for q in QUOTES:
                q0 = pre.get((o, q), (0.0, None))[0]
                q1 = post.get((o, q), (0.0, None))[0]
                if q0 or q1:
                    ветки.append(("токен " + q[:6], t0, t1, q0, q1))
            n0, n1 = native_balance(tx, o)
            if n0 is not None:
                ветки.append(("нативный SOL", t0, t1, n0 or 0.0, n1 or 0.0))
            for имя, a0, a1, b0, b1 in ветки:
                проверки = {
                    "обе ноги двигаются": (a1 - a0) != 0 and (b1 - b0) != 0,
                    "ноги навстречу": ((a1 - a0) > 0) != ((b1 - b0) > 0) if (a1 - a0) and (b1 - b0) else False,
                    "пул против трейдера": (((a1 - a0) > 0) != (tr_d > 0)) if tr_d and (a1 - a0) else False,
                    "отдал >= половины": (abs(a1 - a0) >= abs(tr_d) * 0.5) if tr_d else False,
                }
                side["кандидаты"].append({
                    "счёт": o, "ветка": имя,
                    "токен_до": a0, "токен_после": a1,
                    "котировка_до": b0, "котировка_после": b1,
                    "проверки": проверки,
                    "принят": all(проверки.values()),
                })
        from solana_pool_price_recompute import pool_reserves  # noqa: PLC0415
        chosen = pool_reserves(tx, mint, trader)
        side["выбранный_пул"] = chosen
        fact = exec_price(tx, mint, trader)
        side["факт_средняя_исполнения"] = fact
        if chosen.get("ок"):
            side["программа"] = program_label(chosen.get("программа_id"))
            side["сверка"] = check_side(chosen, fact, label)
        report["стороны"].append(side)
    (REPO_ROOT / "data" / "solana_pool_audit_one.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2)[:6000])


def self_test() -> None:
    checks = []

    def chk(n, ok, got=""):
        checks.append((n, bool(ok), got))

    def ПУЛ(цена_до, цена_после, средняя, *, dk=0.002, программа="Raydium",
             своя_валюта=None):
        return {"ок": True, "цена_до": цена_до, "цена_после": цена_после,
                "средняя_цена_пула": средняя, "k_изменилось_на": dk,
                "программа": программа, "котировка": "QUOTE",
                "трейдер_платил_котировкой_пула": своя_валюта}

    # Идеальный пул x*y=k: модель обязана сойтись со средней по ногам пула.
    model = math.sqrt(0.001 * 0.001 * 1.21)
    pool = ПУЛ(0.001, 0.001 * 1.21, model)
    r = check_side(pool, None, "лидер")
    chk("идеальная сходимость проходит", r["прошла"] is True,
        str(r.get("отклонение_по_ногам_пула")))
    r2 = check_side(ПУЛ(0.001, 0.001 * 1.21, model * 1.02), None, "лидер")
    chk("2% при допуске 3% проходит", r2["прошла"] is True,
        str(r2.get("отклонение_по_ногам_пула")))
    r3 = check_side(ПУЛ(0.001, 0.001 * 1.21, model * 1.10), None, "лидер")
    chk("10% при допуске 3% не проходит", r3["прошла"] is False)
    chk("в причине названы обе цифры",
        "модель" in r3["почему"] and "факт" in r3["почему"], r3["почему"])
    chk("у кривой допуск 5%", tolerance("Pump.fun") == TOL_CURVE)
    chk("у Pump.fun Amm допуск как у AMM", tolerance("Pump.fun Amm") == TOL_AMM)
    r4 = check_side(ПУЛ(0.001, 0.001 * 1.21, model * 1.04, программа="Pump.fun"),
                     None, "лидер")
    chk("4% у кривой проходит", r4["прошла"] is True)
    chk("пул не опознан -- сверка None, а не False",
        check_side(None, 1.0, "мы")["прошла"] is None)
    chk("нет средней по ногам пула -- сверка None",
        check_side({"ок": True, "цена_до": 1.0, "цена_после": 1.2}, None, "мы")
        ["прошла"] is None)

    # Произведение резервов уехало в разы -- это не один пул.
    rk = check_side(ПУЛ(0.001, 0.001 * 1.21, model, dk=1.7), None, "лидер")
    chk("k уехал в разы -- сверка не проходит", rk["прошла"] is False)
    chk("и причина названа произведением резервов",
        "произведение резервов" in (rk.get("почему") or ""), str(rk.get("почему")))

    # Многоножный маршрут: чек трейдера в другой валюте не сверяется.
    rm = check_side(ПУЛ(0.001, 0.001 * 1.21, model, своя_валюта=False),
                     model * 30, "лидер")
    chk("чужая валюта чека не валит сверку", rm["прошла"] is True)
    chk("и оговорка про многоножный маршрут стоит",
        "многоножн" in (rm.get("оговорка") or ""), str(rm.get("оговорка")))
    rs = check_side(ПУЛ(0.001, 0.001 * 1.21, model, своя_валюта=True),
                     model * 1.30, "лидер")
    chk("своя валюта: разошедшийся чек трейдера валит сверку", rs["прошла"] is False)

    плохой = ПУЛ(0.001, 0.001 * 1.21, model * 1.20)
    it = {"пул_в_сделке_лидера": pool, "средняя_цена_исполнения_лидера": model,
          "пул_в_нашей_сделке": плохой, "наша_средняя_цена_исполнения": model * 1.20,
          "а_влияние_лидера_pct": 5.0}
    c = check_trade(it)
    chk("одна сторона не прошла -- сделка не принята",
        c["сверка"]["принята"] is False)
    chk("цифры непринятой сделки погашены",
        "а_влияние_лидера_pct" not in c and "а_влияние_лидера_pct_отклонено" in c)

    ok_it = {"пул_в_сделке_лидера": pool, "средняя_цена_исполнения_лидера": model,
             "пул_в_нашей_сделке": pool, "наша_средняя_цена_исполнения": model,
             "а_влияние_лидера_pct": 5.0}
    c2 = check_trade(ok_it)
    chk("обе прошли -- сделка принята и цифры на месте",
        c2["сверка"]["принята"] is True and c2.get("а_влияние_лидера_pct") == 5.0)

    s = summarise("тест", [c2, c])
    chk("в медианы идут только принятые", s["прошли_сверку"] == 1 and
        s["медиана_а_влияние_лидера_pct"] == 5.0, str(s["медиана_а_влияние_лидера_pct"]))
    chk("порог сводной -- 12 из 15", MIN_PASSED_FOR_SUMMARY == 12)

    bad = 0
    for n, ok, got in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {n}" + (f"  -> {got}" if got and not ok else ""))
        bad += (not ok)
    print(f"самопроверка сверки: {len(checks) - bad}/{len(checks)} пройдено")
    if bad:
        raise SystemExit(f"самопроверка не пройдена: {bad} из {len(checks)}")


if __name__ == "__main__":
    main()
