#!/usr/bin/env python3
"""Посредник: кто РЕАЛЬНЫЙ покупатель в транзакциях, которые подписывает один адрес.

ЗАЧЕМ. По записям штаба AgmLJBMD -- сервер Fomo: он первый подписант у пользователей
Fomo (25.09 в PICKAXE он платил газ покупки лидера через OKX DEX Router). Тогда его
«покупки впереди наших» -- это сделки РАЗНЫХ людей под одним подписантом, и считать его
как одного трейдера нельзя. Проверяется числом: в каждой его пуловой транзакции берётся
владелец токен-счёта, на который пришёл токен (это и есть покупатель -- то же правило,
которым считается лидер Beqv6dzT, покупающий через посредника).

Выход: data/podbivka/posrednik_<адрес8>.json и docs/podbivka_<дата>_posrednik_<адрес8>.md:
сколько транзакций, сколько РАЗНЫХ покупателей, их верх по числу покупок и по объёму,
есть ли среди них заданные адреса (--iskat), размер покупки в SOL-экв и тип пула.
Только чтение цепи.
"""
from __future__ import annotations

import argparse
import calendar
import collections
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import podbivka_lider_po_cepi as L  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
SOLы = {"So11111111111111111111111111111111111111112", "native"}


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    import podbivka_sim as S  # noqa: PLC0415
    import podbivka_rezhim2 as R2  # noqa: PLC0415
    р = argparse.ArgumentParser()
    р.add_argument("--adres", required=True)
    р.add_argument("--s", required=True, help="с какого UTC: YYYY-MM-DDTHH:MM:SSZ")
    р.add_argument("--do", default="")
    р.add_argument("--stranic", type=int, default=30)
    р.add_argument("--iskat", default="", help="через запятую: кого искать среди покупателей")
    р.add_argument("--metka", default="2026-10-03")
    а = р.parse_args()
    искать = {x for x in а.iskat.split(",") if x}
    с_ts = calendar.timegm(time.strptime(а.s, "%Y-%m-%dT%H:%M:%SZ"))
    до_ts = calendar.timegm(time.strptime(а.do, "%Y-%m-%dT%H:%M:%SZ")) if а.do else None
    уз = S.Узел()
    подписи, до, страниц, сбои = [], None, 0, []
    with уз.на("helius"):
        while страниц < а.stranic:
            try:
                стр = уз.подписи(а.adres, до=до, limit=1000)
            except Exception as exc:  # noqa: BLE001
                сбои.append(f"страница {страниц}: {type(exc).__name__}: {S.чисто(str(exc))[:160]}")
                break
            страниц += 1
            if not стр:
                break
            подписи += [з for з in стр if (з.get("blockTime") or 0) >= с_ts
                        and (до_ts is None or (з.get("blockTime") or 0) < до_ts)]
            if (стр[-1].get("blockTime") or 0) < с_ts or len(стр) < 1000:
                break
            до = стр[-1]["signature"]
        удачных = [з for з in подписи if з.get("err") is None]
        счёт = collections.Counter()
        покупатели: dict = {}
        курсы: dict = {}
        программы = collections.Counter()
        примеры: list = []
        for и in range(0, len(удачных), 100):
            кусок = удачных[и:и + 100]
            try:
                пак = уз.пакет([з["signature"] for з in кусок],
                               {з["signature"]: з.get("blockTime") for з in кусок})
            except Exception as exc:  # noqa: BLE001
                сбои.append(f"пакет {и}: {type(exc).__name__}: {S.чисто(str(exc))[:160]}")
                счёт["не_прочитано"] += len(кусок)
                continue
            for з in кусок:
              try:
                т = пак.get(з["signature"])
                if not т:
                    счёт["не_прочитано"] += 1
                    continue
                прог = L.программы(т) & L.ПУЛОВЫЕ
                if not прог:
                    счёт["без_пула"] += 1
                    continue
                счёт["пуловых"] += 1
                к = L.разбор_упоминания(т, а.adres)
                if not к["подписант"]:
                    счёт["не_его_подпись"] += 1
                # получатели токена: владельцы токен-счетов с приходом (не котировка)
                ряды = [r for r in C.token_rows(т).values()
                        if r["owner"] and r["post"] > r["pre"] and r["mint"] not in SOLы]
                if not ряды:
                    счёт["нет_прихода_токена"] += 1
                    continue
                ряды.sort(key=lambda r: r["post"] - r["pre"], reverse=True)
                # хранилище пула тоже получает токен при продаже -- берём владельца с
                # самым большим приходом, у которого НЕ стоит встречная отдача котировки
                верх = ряды[0]
                минт, покупатель = верх["mint"], верх["owner"]
                пул = C.identify_pool(т, покупатель, минт)
                if not пул.get("pool_vault"):
                    счёт["пул_не_опознан"] += 1
                    continue
                q = пул.get("quote_mint")
                все_ряды = {r["account"]: r for r in C.token_rows(т).values() if r["account"]}
                кв = все_ряды.get(пул.get("quote_vault"))
                sol_экв = None
                if кв is not None:
                    сырое = abs(int(кв["post"]) - int(кв["pre"]))
                    if q in SOLы:
                        sol_экв = round(сырое / 1e9, 6)
                    else:
                        if q not in курсы:
                            try:
                                ц, откуда = R2.цена_котировочного(уз, т, q)
                            except Exception as exc:  # noqa: BLE001
                                ц, откуда = None, type(exc).__name__
                            курсы[q] = (ц, откуда)
                        ц, _ = курсы[q]
                        sol_экв = round(сырое * ц / 1e9, 6) if ц else None
                elif пул.get("quote_delta") is not None:
                    try:
                        sol_экв = round(abs(float(пул["quote_delta"])), 6)
                    except (TypeError, ValueError):
                        sol_экв = None
                import c2_pool_programs as PP  # noqa: PLC0415
                прг = PP.pool_program(т, пул["pool_vault"], PP.labels()).get("pool_program")
                программы[L.ИМЕНА.get(прг) or (прг or "")[:8]] += 1
                счёт["покупок"] += 1
                з_ = покупатели.setdefault(покупатель, {"покупок": 0, "sol": [], "минтов": set(),
                                                        "сам_подписант": 0, "котировки": collections.Counter()})
                з_["покупок"] += 1
                if sol_экв is not None:
                    з_["sol"].append(sol_экв)
                з_["минтов"].add(минт)
                з_["котировки"][q or "?"] += 1
                if покупатель == а.adres:
                    з_["сам_подписант"] += 1
                if покупатель in искать and len(примеры) < 10:
                    примеры.append({"signature": з["signature"], "покупатель": покупатель,
                                    "mint": минт, "sol_экв": sol_экв, "slot": т.get("slot")})
              except Exception as exc:  # noqa: BLE001
                сбои.append(f"подпись {з['signature'][:10]}: {type(exc).__name__}: {str(exc)[:120]}")
                счёт["разбор_упал"] += 1
            del пак
    ряды_в = sorted(покупатели.items(), key=lambda kv: -kv[1]["покупок"])
    из_ = {"адрес": а.adres, "с_utc": а.s, "до_utc": а.do or "сейчас", "страниц": страниц,
           "подписей": len(подписи), "упавших": len(подписи) - len(удачных),
           "счёт": dict(счёт), "сбоев": len(сбои), "сбои": сбои[:30],
           "покупателей_разных": len(покупатели),
           "искали": sorted(искать), "найдены": sorted(искать & set(покупатели)),
           "примеры_найденных": примеры,
           "программы": dict(программы),
           "покупатели": {a: {"покупок": v["покупок"], "минтов": len(v["минтов"]),
                              "sol_медиана": (round(statistics.median(v["sol"]), 4) if v["sol"] else None),
                              "sol_сумма": (round(sum(v["sol"]), 4) if v["sol"] else None),
                              "сам_подписант": v["сам_подписант"],
                              "котировки": dict(v["котировки"])}
                          for a, v in ряды_в},
           "расход": уз.расход()}
    out = П / f"posrednik_{а.adres[:8]}.json"
    out.write_text(json.dumps(из_, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(out)

    всего_пок = счёт["покупок"]
    md = [f"# Посредник `{а.adres[:8]}`: кто реальный покупатель под его подписью", "",
          f"Окно {а.s} -- {а.do or 'сейчас'}, страниц подписей {страниц}. Подписей в окне "
          f"**{len(подписи)}** (упавших {len(подписи) - len(удачных)}), из них с пуловой программой "
          f"{счёт.get('пуловых', 0)}, с приходом токена и опознанным пулом **{всего_пок}**. "
          f"Покупатель -- владелец токен-счёта, на который пришёл токен (то же правило, которым "
          f"считается лидер Beqv6dzT: он покупает через посредника и подписантом не бывает). "
          f"Отброшено: без пуловой программы {счёт.get('без_пула', 0)}, нет прихода токена "
          f"{счёт.get('нет_прихода_токена', 0)}, пул не опознан {счёт.get('пул_не_опознан', 0)}, "
          f"не прочитано {счёт.get('не_прочитано', 0)}, сбоев разбора {счёт.get('разбор_упал', 0)}.", "",
          f"**Разных покупателей: {len(покупатели)}.** Его собственных покупок (он сам владелец "
          f"получившего счёта): {(покупатели.get(а.adres) or {}).get('покупок', 0)}.", ""]
    if искать:
        md += [f"Искали среди покупателей: {', '.join('`' + x[:8] + '`' for x in sorted(искать))} -- "
               + ("найдены: " + ", ".join(f"`{x[:8]}` ({покупатели[x]['покупок']} покупок)"
                                          for x in sorted(искать & set(покупатели)))
                  if (искать & set(покупатели)) else "не найдены ни разу") + ".", ""]
    md += ["| покупатель | покупок | доля | минтов | размер SOL-экв: медиана | сумма | котировки |",
           "|---|---|---|---|---|---|---|"]
    for a, v in ряды_в[:40]:
        д = из_["покупатели"][a]
        md.append(f"| `{a[:8]}`{' (сам)' if a == а.adres else ''} | {д['покупок']} | "
                  f"{д['покупок'] / max(1, всего_пок):.0%} | {д['минтов']} | "
                  + (f"{д['sol_медиана']:.4f}" if д["sol_медиана"] is not None else "—") + " | "
                  + (f"{д['sol_сумма']:.3f}" if д["sol_сумма"] is not None else "—") + " | "
                  + ", ".join(f"{(k or '?')[:4]} {n}" for k, n in list(д["котировки"].items())[:3]) + " |")
    md += ["", "| программа пула | покупок |", "|---|---|"]
    for k, n in программы.most_common():
        md.append(f"| {k} | {n} |")
    md += ["", "Ничего не рекомендуется.", ""]
    стр = КОРЕНЬ / "docs" / f"podbivka_{а.metka}_posrednik_{а.adres[:8]}.md"
    стр.write_text("\n".join(md) + "\n", encoding="utf-8")
    R.записано(стр)
    print(f"{стр.name}: подписей {len(подписи)}, покупок {всего_пок}, разных покупателей "
          f"{len(покупатели)}, найдены {из_['найдены']}, сбоев {len(сбои)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
