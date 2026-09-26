#!/usr/bin/env python3
"""Подбивка: первые покупки лидера с котировкой пула «другое» -- чем котируются
и что дала бы наша 0.5 SOL через котировочный токен.

Шаг 1 (этот файл, --shag 1): по каждой первой покупке лидера от 2 SOL-экв из
data/podbivka/p1b2_sverka.json -- транзакция, пул (c2_common.identify_pool),
минт котировки; для «другого» -- какие встречные плечи этого минта есть в той
же транзакции (к WSOL, к USDC/USDT): это цена котировочного на тот же слот.
Выход: data/podbivka/lider_kotirovka.json, топ-5 котировочных минтов.
Узел -- по скользящей грани (старше «сейчас - 2.4 суток» -- Helius).
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
ЛИДЕР = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"


def плечи(tx: dict, q: str) -> dict:
    """Встречные плечи минта q в инструкциях DEX (счета не подписантов)."""
    sg = C.signers(tx)
    ряды = [r for r in C.token_rows(tx).values() if r["account"] and r["owner"] not in sg]
    из_: dict = {}
    for s in C.instruction_account_sets(tx):
        qq = [r for r in ряды if r["mint"] == q and r["account"] in s and r["post"] != r["pre"]]
        for a in qq:
            for b in ряды:
                if b["account"] not in s or b["mint"] == q or b["post"] == b["pre"]:
                    continue
                da, db = a["post"] - a["pre"], b["post"] - b["pre"]
                if (da > 0) == (db > 0):
                    continue
                if b["mint"] in (C.WSOL, C.USDC, C.USDT):
                    к = "WSOL" if b["mint"] == C.WSOL else "USD"
                    # цена q в котировке b: |db|/|da| в сырых единицах
                    из_.setdefault(к, []).append({"b_за_q_raw": abs(db) / abs(da), "dec_q": a.get("dec"),
                                                   "dec_b": b.get("dec")})
    return из_


def main() -> int:
    д = json.loads((КОРЕНЬ / "data" / "podbivka" / "p1b2_sverka.json").read_text(encoding="utf-8"))
    L = д["по_источнику"]["leader"]
    покупки = [{"sig": x["source_sig"], "mint": x["mint"], "utc": x["utc"]} for x in L["список_совпало"]] + \
              [{"sig": x["source_sig"], "mint": x["mint"], "utc": x["utc"]} for x in L["список_у_симулятора_без_DBot"]]
    уз = S.Узел()
    ряды, счёт = [], collections.Counter()
    import calendar
    import time
    for п in покупки:
        bt = calendar.timegm(time.strptime(п["utc"], "%Y-%m-%dT%H:%M:%SZ"))
        with уз.на(S.узел_по_времени(bt)):
            try:
                tx = уз.tx(п["sig"])
            except RuntimeError as exc:
                ряды.append({**п, "why_not": S.чисто(str(exc))[:120]})
                continue
        уз._кэш.clear()  # noqa: SLF001
        if not tx:
            ряды.append({**п, "why_not": "узел не отдал"})
            continue
        пул = C.identify_pool(tx, ЛИДЕР, п["mint"])
        q = пул.get("quote_mint")
        р = {**п, "quote_mint": q, "pool_vault": пул.get("pool_vault"), "slot": tx.get("slot"),
             "узел": S.узел_по_времени(bt)}
        if q and q not in (C.WSOL, C.NATIVE_QUOTE, C.USDC, C.USDT):
            счёт[q] += 1
            пл = плечи(tx, q)
            р["плечи_котировочного"] = {к: len(v) for к, v in пл.items()}
        ряды.append(р)
    топ = [{"quote_mint": q, "покупок": n} for q, n in счёт.most_common(5)]
    со_плечом = collections.Counter()
    for р in ряды:
        for к in (р.get("плечи_котировочного") or {}):
            со_плечом[к] += 1
    итог = {"покупок": len(покупки), "другое": sum(счёт.values()), "топ5": топ,
            "с_плечом_в_той_же_сделке": dict(со_плечом), "ряды": ряды, "расход": уз.расход()}
    (КОРЕНЬ / "data" / "podbivka" / "lider_kotirovka.json").write_text(json.dumps(итог, ensure_ascii=False, indent=1),
                                                                        encoding="utf-8")
    print("лидер, котировка «другое»:", json.dumps({к: итог[к] for к in ("покупок", "другое", "топ5",
                                                                           "с_плечом_в_той_же_сделке")}, ensure_ascii=False))
    return 0


if __name__ == "__main__" and "--shag2" not in sys.argv:
    raise SystemExit(main())


# ================================================================ шаг 2: 0.5 SOL через котировочный

def цена_q_в_sol(tx: dict, q: str, bt: int) -> tuple:
    """Лампорты за сырую единицу q на тот же слот: плечо q-WSOL в той же сделке,
    иначе плечо q-USD и курс детектора (ряд kurs_sol_usd.json)."""
    пл = плечи(tx, q)
    if пл.get("WSOL"):
        return S.медиана([x["b_за_q_raw"] for x in пл["WSOL"]]), "плечо к WSOL в той же сделке"
    if пл.get("USD"):
        курс = S.КурсПулом(None).rate_for({"blockTime": bt})[0] if S.узел_по_времени(bt) == "shyft" else None
        if курс is None:
            ряд = S.ряд_курса()
            if ряд:
                бл = min(ряд, key=lambda x: abs(x[0] - bt))
                курс = бл[1] if abs(бл[0] - bt) <= 12 * 3600 else None
        if not курс:
            return None, "нет курса USD/SOL на это время"
        x = пл["USD"][0]
        usd_за_q_raw = S.медиана([y["b_за_q_raw"] for y in пл["USD"]]) / 10 ** x["dec_b"]
        return usd_za(usd_за_q_raw, float(курс)), "плечо к USD в той же сделке и курс детектора"
    return None, "в сделке нет плеча котировочного к WSOL/USD"


def usd_za(usd_за_q_raw: float, курс: float) -> float:
    return usd_за_q_raw / курс * S.ЛАМПОРТОВ


def через_котировочный(уз: S.Узел, р: dict, bt: int) -> dict:
    """S+1 -> s0+72 / s0+150 для покупки с котировкой q (x*y=k по хранилищам)."""
    import c2_swap_build as SB  # noqa: PLC0415
    из_ = {"sig": р["sig"], "quote_mint": р["quote_mint"]}
    tx0 = уз.tx(р["sig"])
    q = р["quote_mint"]
    пул = C.identify_pool(tx0, ЛИДЕР, р["mint"])
    if not пул.get("pool_vault") or not пул.get("quote_vault"):
        return {**из_, "why_not": "пул не опознан"}
    import c2_pool_programs as PP  # noqa: PLC0415
    прог = PP.pool_program(tx0, пул["pool_vault"], PP.labels())["pool_program"]
    из_["program"] = прог
    tpl = SB.extract_template(tx0, прог, пул["pool_vault"]) if прог else {"ok": False, "why_not": "нет программы"}
    if not tpl.get("ok"):
        return {**из_, "why_not": f"шаблон: {tpl.get('why_not')}"}
    мо = SB.min_out_from_reserves(tpl, tx0, 10 ** 6, 0.0)
    f = мо.get("fee_factor")
    if not мо.get("ok") or not f or not (0.80 <= f <= 1.0):
        return {**из_, "why_not": f"x*y=k не калибруется ({мо.get('why_not') or f})"}
    цена, откуда = цена_q_в_sol(tx0, q, bt)
    из_["цена_q_откуда"] = откуда
    if not цена:
        return {**из_, "why_not": откуда}
    нал_q = S.налог_минта(уз, q)
    нал_т = S.налог_минта(уз, р["mint"])
    из_["налог_q_bps"], из_["налог_токена_bps"] = нал_q.get("bps"), нал_т.get("bps")
    # Вход: 0.5 SOL -> q по цене того же слота (без проскальзывания в пуле q/SOL,
    # флаг), q пул->мы и мы->пул токена -- два перевода q с налогом.
    q_raw = int(S.РАЗМЕР_ЛАМ / цена)
    q_raw -= S.удержано(q_raw, нал_q)
    q_raw -= S.удержано(q_raw, нал_q)
    s0 = tx0["slot"]
    ист = S.история_пула(уз, пул["pool_vault"], р["sig"], s0, опора=(S.подпись_после_слота(уз, s0 + 151)
                                                                    if уз.текущий == "helius" else None),
                         до_слота=s0 + 150)
    if ист["why_not"] or ист["предел"]:
        return {**из_, "why_not": "история пула не собрана"}
    усп = [з for з in ист["подписи"] if з["ok"]]
    ст0 = S.состояние(tx0, "xyk", пул, р["mint"])

    def ст_до(слот):
        for з in reversed([з for з in усп if з["slot"] <= слот][-5:]):
            ст = S.состояние(уз.tx(з["signature"]), "xyk", пул, р["mint"])
            if ст:
                return ст
        return ст0
    ст1 = ст_до(s0 + 1)
    пок = S.наша_покупка("xyk", ст1, f=f, кривая_bps=(0, 0), размер=q_raw)
    if not пок.get("ok"):
        return {**из_, "why_not": пок.get("why_not")}
    т = пок["tokens"] - S.удержано(пок["tokens"], нал_т)       # пул -> мы
    т -= S.удержано(т, нал_т)                                  # мы -> пул на выходе
    for H in (72, 150):
        пр = S.наша_продажа("xyk", ст_до(s0 + H - 1), пок, т, f=f, кривая_bps=(0, 0), g=f)
        if not пр.get("ok"):
            из_[f"S1_{H}"] = None
            continue
        q_out = пр["lamports"]                                 # в сырых единицах q
        q_out -= S.удержано(q_out, нал_q)                      # пул -> мы
        q_out -= S.удержано(q_out, нал_q)                      # мы -> пул q/SOL
        из_[f"S1_{H}"] = S.чистый_пп(int(q_out * цена))
    из_["флаги"] = ["цена q к SOL на выходе = на входе", "без проскальзывания в пуле q/SOL",
                    "доля продажи = f покупки"]
    return из_


def шаг2() -> int:
    import calendar  # noqa: PLC0415
    import statistics  # noqa: PLC0415
    import time  # noqa: PLC0415
    д = json.loads((КОРЕНЬ / "data" / "podbivka" / "lider_kotirovka.json").read_text(encoding="utf-8"))
    уз = S.Узел()
    рез = []
    for р in д["ряды"]:
        if not р.get("quote_mint") or р["quote_mint"] in (C.WSOL, C.NATIVE_QUOTE, C.USDC, C.USDT):
            continue
        bt = calendar.timegm(time.strptime(р["utc"], "%Y-%m-%dT%H:%M:%SZ"))
        with уз.на(S.узел_по_времени(bt)):
            try:
                рез.append(через_котировочный(уз, р, bt))
            except Exception as exc:  # noqa: BLE001
                рез.append({"sig": р["sig"], "why_not": S.чисто(f"{type(exc).__name__}: {exc}")[:160]})
        уз._кэш.clear()  # noqa: SLF001
    свод = {}
    for H in (72, 150):
        v = [x[f"S1_{H}"] for x in рез if x.get(f"S1_{H}") is not None]
        свод[f"S1_{H}"] = {"n": len(v), "медиана": round(statistics.median(v), 2) if v else None,
                           "среднее": round(sum(v) / len(v), 2) if v else None,
                           "в_плюс": round(sum(1 for x in v if x > 0) / len(v), 3) if v else None}
    отк = collections.Counter((x.get("why_not") or "")[:60] for x in рез if x.get("why_not"))
    д["шаг2"] = {"покупок": len(рез), "свод": свод, "отказы": dict(отк), "ряды": рез, "расход": уз.расход()}
    (КОРЕНЬ / "data" / "podbivka" / "lider_kotirovka.json").write_text(json.dumps(д, ensure_ascii=False, indent=1),
                                                                        encoding="utf-8")
    print("лидер через котировочный:", json.dumps({"покупок": len(рез), "свод": свод, "отказы": dict(отк)},
                                                   ensure_ascii=False))
    return 0


if __name__ == "__main__" and "--shag2" in sys.argv:
    raise SystemExit(шаг2())
