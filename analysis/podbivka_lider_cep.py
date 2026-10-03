#!/usr/bin/env python3
"""Лидер Beqv6dzT по ЦЕПИ: его покупки, их размер и котировка, и наш результат за ним.

ЗАЧЕМ. Архив PumpApi этого адреса не видит: он покупает через посредника, и событие
привязано к подписанту, а не к владельцу получившего токен-счёта (слово владельца 03.10).
Поэтому покупки читаются с цепи: берутся ЕГО подписи окна, в каждой пуловой транзакции
проверяется, он ли владелец токен-счёта, на который пришёл токен (и подписант тоже
считается -- отдельной пометкой).

Три шага одним процессом:
  --sbor   : подписи окна -> покупки {signature, mint, wallet, sol_экв, котировка, программа};
  --model  : по каждой покупке -- режим 2 (podbivka_rezhim2.одна): наш вход «конец слота»
             (S0_дно) и «сразу за ним» (S0), выход +30 / +150, билет --razmer (по умолчанию
             0.3 SOL), п.п. чистыми (0.002 SOL на круг);
  --svod   : страница docs/podbivka_2026-10-03_lider.md -- размер p10/p50/p90, котировки,
             типы пулов, результат по корзинам размера (<1 / 1-3 / 3-15 / >=15 SOL-экв) и по
             котировке; n < 20 -- пишется n, без средних.

Ничего не рекомендуется: числа и то, чего по ним не видно.
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
import time
import calendar
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import podbivka_lider_po_cepi as L  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
ЛИДЕР = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
SOLы = {"So11111111111111111111111111111111111111112", "native"}
КОРЗИНЫ = ((0.0, 1.0, "<1"), (1.0, 3.0, "1-3"), (3.0, 15.0, "3-15"), (15.0, float("inf"), ">=15"))
N_МИН = 20
ИМЕНА_КВ = {"So11111111111111111111111111111111111111112": "SOL",
            "native": "SOL (натив)",
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT"}


def корзина(v: float | None) -> str | None:
    if v is None:
        return None
    for a, b, имя in КОРЗИНЫ:
        if a <= v < b:
            return имя
    return None


def имя_кв(q: str | None, имена: dict) -> str:
    if not q:
        return "неизвестно"
    return ИМЕНА_КВ.get(q) or имена.get(q) or q[:8]


def сбор(а) -> dict:
    """Покупки лидера по цепи в окне: он владелец получившего токен-счёта или подписант."""
    import podbivka_run as R  # noqa: PLC0415
    import podbivka_sim as S  # noqa: PLC0415
    import podbivka_rezhim2 as R2  # noqa: PLC0415
    с_ts = calendar.timegm(time.strptime(а.s, "%Y-%m-%dT%H:%M:%SZ"))
    до_ts = calendar.timegm(time.strptime(а.do, "%Y-%m-%dT%H:%M:%SZ")) if а.do else None
    уз = S.Узел()
    подписи, до, страниц = [], None, 0
    with уз.на("helius"):
        while страниц < а.stranic:
            стр = уз.подписи(ЛИДЕР, до=до, limit=1000)
            страниц += 1
            if not стр:
                break
            подписи += [з for з in стр if (з.get("blockTime") or 0) >= с_ts
                        and (до_ts is None or (з.get("blockTime") or 0) < до_ts)]
            if (стр[-1].get("blockTime") or 0) < с_ts or len(стр) < 1000:
                break
            до = стр[-1]["signature"]
        удачных = [з for з in подписи if з.get("err") is None]
        покупки, счёт = [], collections.Counter()
        курсы: dict = {}
        РАЗМЕР_ПАК = 100
        for и in range(0, len(удачных), РАЗМЕР_ПАК):
            кусок = удачных[и:и + РАЗМЕР_ПАК]
            пак = уз.пакет([з["signature"] for з in кусок],
                           {з["signature"]: з.get("blockTime") for з in кусок})
            for з in кусок:
                т = пак.get(з["signature"])
                if not т:
                    счёт["не_прочитано"] += 1
                    continue
                прог = L.программы(т) & L.ПУЛОВЫЕ
                if not прог:
                    счёт["без_пула"] += 1
                    continue
                к = L.разбор_упоминания(т, ЛИДЕР)
                if not (к["получил_токен"] or (к["подписант"] and к["владелец_токенсчёта"])):
                    счёт["не_покупатель"] += 1
                    continue
                ряды = [r for r in C.token_rows(т).values()
                        if r["owner"] == ЛИДЕР and r["post"] > r["pre"] and r["mint"] not in SOLы]
                if not ряды:
                    счёт["нет_прихода_токена"] += 1
                    continue
                ряды.sort(key=lambda r: r["post"] - r["pre"], reverse=True)
                минт = ряды[0]["mint"]
                пул = C.identify_pool(т, ЛИДЕР, минт)
                if not пул.get("pool_vault"):
                    счёт["пул_не_опознан"] += 1
                    continue
                q = пул.get("quote_mint")
                qd = пул.get("quote_delta")
                сырое = abs(int(qd)) if qd is not None else None
                sol_экв = None
                if сырое is not None:
                    if q in SOLы:
                        sol_экв = round(сырое / 1e9, 6)
                    else:
                        if q not in курсы:
                            ц, откуда = R2.цена_котировочного(уз, т, q)
                            курсы[q] = (ц, откуда)
                        ц, _ = курсы[q]
                        sol_экв = round(сырое * ц / 1e9, 6) if ц else None
                import c2_pool_programs as PP  # noqa: PLC0415
                прг = PP.pool_program(т, пул["pool_vault"], PP.labels()).get("pool_program")
                покупки.append({"signature": з["signature"], "mint": минт, "wallet": ЛИДЕР,
                                "blockTime": з.get("blockTime"), "slot": т.get("slot"),
                                "sol_экв": sol_экв, "quote_mint": q, "quote_raw": сырое,
                                "program": прг, "программа": L.ИМЕНА.get(прг) or (прг or "")[:8],
                                "подписант": bool(к["подписант"]), "группа": "лидер",
                                "вид": "первая", "слотов_от_предыдущей": None})
                счёт["покупок"] += 1
            del пак
    # вид: первая / докупка -- по предыдущей покупке того же минта в окне (1800 слотов)
    покупки.sort(key=lambda p: (p.get("slot") or 0))
    посл: dict = {}
    for p in покупки:
        сл = p.get("slot") or 0
        пред = посл.get(p["mint"])
        if пред is not None:
            p["слотов_от_предыдущей"] = сл - пред
            p["вид"] = "докупка" if сл - пред < 1800 else "первая"
        посл[p["mint"]] = сл
    из_ = {"адрес": ЛИДЕР, "с_utc": а.s, "до_utc": а.do or "сейчас",
           "подписей": len(подписи), "упавших": len(подписи) - len(удачных),
           "счёт": dict(счёт), "курсы_котировочных": {k: v[1] for k, v in курсы.items()},
           "покупки": покупки, "расход": уз.расход()}
    out = П / "lider_cep_pokupki.json"
    out.write_text(json.dumps(из_, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(out)
    print(f"сбор: подписей {len(подписи)}, покупок {счёт['покупок']}, "
          f"не покупатель {счёт['не_покупатель']}, без пула {счёт['без_пула']}", flush=True)
    return из_


def модель(а) -> int:
    """Режим 2 по каждой покупке: билет --razmer, выходы --gorizonty."""
    import podbivka_run as R  # noqa: PLC0415
    import podbivka_sim as S  # noqa: PLC0415
    import podbivka_rezhim2 as R2  # noqa: PLC0415
    спис = json.loads((П / "lider_cep_pokupki.json").read_text(encoding="utf-8"))["покупки"]
    S.РАЗМЕР_ЛАМ = int(round(а.razmer * 1e9))
    R2.РАЗМЕР = S.РАЗМЕР_ЛАМ
    R2.ГОРИЗОНТЫ = tuple(int(x) for x in а.gorizonty.split(","))
    R2.SOL_ПУЛ = True
    R2.МАКС_ОКНА = ()
    R2.ПРОДАЖИ = False
    out = П / "rezhim2" / f"lider3_{int(а.razmer * 100)}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    готово = set()
    if out.exists():
        for l in out.read_text(encoding="utf-8").splitlines():
            try:
                готово.add(json.loads(l)["signature"])
            except (ValueError, KeyError):
                pass
    R.записано(out)
    уз = S.Узел()
    последний = time.time()
    сделано = 0
    with open(out, "a", encoding="utf-8") as ф:
        for н, п in enumerate(спис):
            if п["signature"] in готово:
                continue
            with уз.на(S.узел_по_времени(п.get("blockTime"))):
                try:
                    рез = R2.одна(уз, п)
                except Exception as exc:  # noqa: BLE001
                    рез = {к: п.get(к) for к in R2.БАЗА}
                    рез["why_not"] = S.чисто(f"{type(exc).__name__}: {exc}")[:200]
            уз._кэш.clear()  # noqa: SLF001
            рез["sol_экв_его"] = п.get("sol_экв")
            рез["quote_mint_его"] = п.get("quote_mint")
            рез["программа"] = п.get("программа")
            ф.write(json.dumps(рез, ensure_ascii=False) + "\n")
            ф.flush()
            сделано += 1
            if time.time() - последний > 600:
                R.пуш(f"Podbivka-2: lider cep {сделано} iz {len(спис)} [automated]", [str(out)])
                последний = time.time()
    print(f"модель: {сделано} покупок посчитано, расход {json.dumps(уз.расход(), ensure_ascii=False)}",
          flush=True)
    return 0


def стат(v: list) -> dict:
    v = [x for x in v if x is not None]
    if not v:
        return {"n": 0}
    return {"n": len(v), "среднее": round(statistics.mean(v), 2),
            "медиана": round(statistics.median(v), 2),
            "в_плюсе": round(sum(1 for x in v if x > 0) / len(v), 3)}


def клетка(s: dict) -> str:
    if not s.get("n"):
        return "—"
    if s["n"] < N_МИН:
        return f"n={s['n']}"
    return f"{s['среднее']:+.2f} / {s['медиана']:+.2f} / {s['в_плюсе']:.0%}"


def квантили(v: list) -> tuple:
    v = sorted(x for x in v if x is not None)
    if not v:
        return (None, None, None)
    def q(p):
        return v[min(len(v) - 1, int(p * len(v)))]
    return (q(0.10), q(0.50), q(0.90))


def свод(а) -> int:
    import podbivka_run as R  # noqa: PLC0415
    сб = json.loads((П / "lider_cep_pokupki.json").read_text(encoding="utf-8"))
    пок = сб["покупки"]
    ф = П / "rezhim2" / f"lider3_{int(а.razmer * 100)}.jsonl"
    ряды = []
    if ф.exists():
        for l in ф.read_text(encoding="utf-8").splitlines():
            try:
                ряды.append(json.loads(l))
            except ValueError:
                pass
    по_подписи = {r.get("signature"): r for r in ряды}
    имена = {}
    H = tuple(int(x) for x in а.gorizonty.split(","))

    def пп(r: dict, вход: str, h: int) -> float | None:
        в = (r.get("входы") or {}).get(вход) or {}
        return в.get(f"потолок_{h}")

    размеры = [p.get("sol_экв") for p in пок]
    p10, p50, p90 = квантили(размеры)
    кв = collections.Counter(имя_кв(p.get("quote_mint"), имена) for p in пок)
    пр = collections.Counter(p.get("программа") or "неизвестно" for p in пок)
    вид = collections.Counter(p.get("вид") for p in пок)
    подп = sum(1 for p in пок if p.get("подписант"))

    md = [f"# Подбивка: лидер `{ЛИДЕР[:8]}` по цепи -- его покупки и наш результат за ним", "",
          f"Окно: {сб['с_utc']} -- {сб['до_utc']}. Читалась ЦЕПЬ (архив PumpApi этот адрес не видит: "
          f"событие привязано к подписанту, а он покупает через посредника). Подписей в окне "
          f"**{сб['подписей']}** (упавших {сб['упавших']}), из них покупок, где он владелец получившего "
          f"токен-счёта, **{len(пок)}**; сам подписант в {подп} из них. Отброшено: без пуловой программы "
          f"{сб['счёт'].get('без_пула', 0)}, не покупатель {сб['счёт'].get('не_покупатель', 0)}, "
          f"пул не опознан {сб['счёт'].get('пул_не_опознан', 0)}, не прочитано "
          f"{сб['счёт'].get('не_прочитано', 0)}.", "",
          f"Наш вход -- «конец слота» за ним (S0_дно) и «сразу за ним» (S0), билет **{а.razmer:g} SOL**, "
          f"выход +{H[0]} и +{H[-1]} слотов, п.п. чистыми (0.002 SOL на круг), модель режима 2 "
          f"(калибровка f по хранилищам сделки источника, g по настоящим продажам пула). Пулы вне "
          f"модели (DLMM / CLMM / DAMM v2 / Whirlpool) считаются не посчитанными и названы ниже. "
          f"Ничего не рекомендуется.", "",
          "## Его покупки", "",
          f"- Покупок **{len(пок)}**: первых {вид.get('первая', 0)}, докупок (тот же минт, меньше 1800 "
          f"слотов) {вид.get('докупка', 0)}.",
          f"- Размер, SOL-экв: p10 **{p10:.3f}**, p50 **{p50:.3f}**, p90 **{p90:.3f}**"
          if p50 is not None else "- Размер: нет чисел",
          f"- Размер без числа (курс котировочного не прочитан): "
          f"{sum(1 for p in пок if p.get('sol_экв') is None)}.", ""]
    md += ["| котировка | покупок | доля |", "|---|---|---|"]
    for q, n in кв.most_common():
        md.append(f"| {q} | {n} | {n / max(1, len(пок)):.0%} |")
    md += ["", "| программа пула | покупок | доля |", "|---|---|---|"]
    for q, n in пр.most_common():
        md.append(f"| {q} | {n} | {n / max(1, len(пок)):.0%} |")

    # результат по корзинам и котировкам
    md += ["", f"## Наш результат за ним (билет {а.razmer:g}, среднее / медиана / в плюсе)", ""]
    for вход, подпись in (("S0_дно", "конец слота"), ("S0", "сразу за ним")):
        md += [f"### Вход «{подпись}»", "",
               "| корзина размера | покупок | с числом | " + " | ".join(f"+{h}" for h in H) + " |",
               "|---|---|---|" + "---|" * len(H)]
        for a, b, имя in КОРЗИНЫ:
            гр = [p for p in пок if корзина(p.get("sol_экв")) == имя]
            с_ч = [по_подписи.get(p["signature"]) for p in гр]
            с_ч = [r for r in с_ч if r and (r.get("входы") or {}).get(вход)]
            строка = f"| {имя} | {len(гр)} | {len(с_ч)} | "
            строка += " | ".join(клетка(стат([пп(r, вход, h) for r in с_ч])) for h in H) + " |"
            md.append(строка)
        md += ["", "| котировка | покупок | с числом | " + " | ".join(f"+{h}" for h in H) + " |",
               "|---|---|---|" + "---|" * len(H)]
        for q, n in кв.most_common():
            гр = [p for p in пок if имя_кв(p.get("quote_mint"), имена) == q]
            с_ч = [по_подписи.get(p["signature"]) for p in гр]
            с_ч = [r for r in с_ч if r and (r.get("входы") or {}).get(вход)]
            md.append(f"| {q} | {n} | {len(с_ч)} | "
                      + " | ".join(клетка(стат([пп(r, вход, h) for r in с_ч])) for h in H) + " |")
        все = [r for r in ряды if (r.get("входы") or {}).get(вход)]
        md += ["", "| всё | покупок | с числом | " + " | ".join(f"+{h}" for h in H) + " |",
               "|---|---|---|" + "---|" * len(H),
               f"| всё | {len(пок)} | {len(все)} | "
               + " | ".join(клетка(стат([пп(r, вход, h) for r in все])) for h in H) + " |", ""]

    # почему без числа
    нет = collections.Counter(r.get("why_not") for r in ряды if r.get("why_not"))
    md += ["## Почему нет числа", "", "| причина | покупок |", "|---|---|"]
    for п_, n in нет.most_common(20):
        md.append(f"| {п_} | {n} |")
    md += ["", f"Посчитано {sum(1 for r in ряды if (r.get('входы') or {}).get('S0_дно'))} из "
           f"{len(ряды)} прочитанных покупок.", ""]
    out = КОРЕНЬ / "docs" / "podbivka_2026-10-03_lider.md"
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    R.записано(out)
    дт = П / "lider_cep_svod.json"
    дт.write_text(json.dumps({"покупок": len(пок), "размер": {"p10": p10, "p50": p50, "p90": p90},
                              "котировки": dict(кв), "программы": dict(пр), "вид": dict(вид),
                              "без_числа": dict(нет), "билет": а.razmer, "горизонты": list(H)},
                             ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(дт)
    print(out.name, "покупок", len(пок), "с числом",
          sum(1 for r in ряды if (r.get("входы") or {}).get("S0_дно")), flush=True)
    return 0


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--s", default="2026-09-26T12:00:00Z")
    р.add_argument("--do", default="")
    р.add_argument("--stranic", type=int, default=40)
    р.add_argument("--razmer", type=float, default=0.3)
    р.add_argument("--gorizonty", default="30,150")
    р.add_argument("--sbor", action="store_true")
    р.add_argument("--model", action="store_true")
    р.add_argument("--svod", action="store_true")
    а = р.parse_args()
    if а.sbor:
        сбор(а)
    if а.model:
        модель(а)
    if а.svod or not (а.sbor or а.model):
        свод(а)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
