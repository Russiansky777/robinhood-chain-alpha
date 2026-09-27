#!/usr/bin/env python3
"""Подбивка п.4 (после A3): симулятор режима 2 -- первые покупки лидера и BATCH-5
в пулах с котировкой не SOL (GP, USDC, другое). Только чтение цепи.

Модель -- та, что сошлась в контроле A3 (прогон 5/6, реальные сделки DBot):
  * доля траты f пула токена -- по хранилищам сделки источника
    f = x0·dy / ((y0 − dy)·dx); ниже 0.95 -- по первым покупкам пула после
    источника (по хранилищам);
  * доля продажи g -- по настоящим продажам пула в окне (по хранилищам), нет
    продаж -- 0.985 (медиана продаж A3);
  * продажа -- x·g·t / (y + t) на настоящих резервах (без рамки X = x/f);
  * котировочный к SOL -- цена из сделки источника (плечо к WSOL / USD и курс),
    одна на входе и выходе; налог котировочного -- на двух переводах в каждую
    сторону; налог токена -- на получении и на продаже;
  * наша трата 0.5 SOL, минус 0.002 SOL на сделку.
Точки входа: S+0 потолок -- сразу после сделки источника; S+0 дно -- после всех
свопов слота s0; S+1 / S+2 -- конец слота s0+1 / s0+2. Выход +72 / +150 --
состояние после последней сделки со слотом <= s0+H−1, с нашей покупкой,
вставленной в резервы (как в основном симуляторе).
Наценка входа -- наша цена за токен (котировочный, ушедший в пул / токены до
налога) к спот-цене пула сразу после сделки источника. Путь цены -- спот в
конце s0, s0+1, s0+2 к споту после источника, п.п.

Список -- data/podbivka/rezhim2_spisok.json (офлайн). Выход по пакету --
data/podbivka/rezhim2/<с>_<по>.jsonl.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import podbivka_a3 as A3  # noqa: E402
import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
SOLы = (C.WSOL, C.NATIVE_QUOTE)
G_БЕЗ_ПРОДАЖ = 0.985
РАЗМЕР = S.РАЗМЕР_ЛАМ
ТОЧКИ = ("S0", "S0_дно", "S1", "S2")


БАЗА = ("signature", "mint", "wallet", "sol_экв", "группа", "вид", "слотов_от_предыдущей")
# x*y=k по хранилищам проверена в A3 на Raydium CP; Pump AMM и AMM v4 -- та же
# формула. LaunchLab -- виртуальные резервы из события (как в основном
# симуляторе). CLMM/DLMM/Whirlpool/DAMM v2/DBC -- хранилища цену не дают: отказ.
AMM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
ПРОГРАММЫ_XYK = {S.SB.CPMM, S.SB.PUMP_AMM, AMM_V4}
_КНИГА: dict = {}


def цена_котировочного(уз: S.Узел, tsrc: dict, q: str):
    """Лампорты за сырую единицу q: как в шаге 2 (плечо в сделке источника);
    иначе USDC/USDT -- 1 USD за единицу по курсу опорного пула на то же время."""
    import podbivka_lider_kotirovka as LK  # noqa: PLC0415
    цена, откуда = LK.цена_q_в_sol(tsrc, q, tsrc.get("blockTime"))
    if цена:
        return цена, откуда
    # нет курса USD/SOL в ряду детектора (эпоха Helius): курс опорного пула
    # SOL/USDC на то же время (КурсПулом через узел)
    if id(уз) not in _КНИГА:
        _КНИГА[id(уз)] = S.КурсПулом(S.КурсУзла(уз))
    try:
        курс = _КНИГА[id(уз)].rate_for(tsrc)[0]
    except Exception:  # noqa: BLE001
        курс = None
    if not курс:
        return None, откуда
    if q in (C.USDC, C.USDT):
        return 1e-6 / float(курс) * 1e9, "стейбл: 1 USD по курсу опорного пула"
    пл = LK.плечи(tsrc, q).get("USD")
    if пл:
        usd_за_q_raw = statistics.median([x["b_за_q_raw"] for x in пл]) / 10 ** пл[0]["dec_b"]
        return usd_за_q_raw / float(курс) * 1e9, "плечо к USD в той же сделке и курс опорного пула"
    return None, откуда


ГОРИЗОНТЫ = (72, 150)
# Прогон лидера 27.09 (вечер): максимум по свопам, продажи источника, SOL-пулы.
МАКС_ОКНА: tuple = ()          # (36, 150, 600, 1800) -- окна максимума; () -- выкл.
МАКС_ЧТЕНИЙ = 300              # состояний на покупку: все свопы, иначе конец слота, иначе шаг
ПРОДАЖИ = False                # слоты продаж источника (по его счёту минта) и выход по первой
SOL_ПУЛ = False                # котировка SOL: та же модель режима 2, цена q = 1


def одна(уз: S.Узел, п: dict) -> dict:
    из_ = {к: п.get(к) for к in БАЗА}
    tsrc = уз.tx(п["signature"])
    if not tsrc:
        return {**из_, "why_not": "узел не отдал сделку источника"}
    s0 = tsrc["slot"]
    пул = C.identify_pool(tsrc, п["wallet"], п["mint"])
    q = пул.get("quote_mint")
    из_.update(slot=s0, quote_mint=q, pool_vault=пул.get("pool_vault"), split=bool(пул.get("split")))
    if not пул.get("pool_vault") or not пул.get("quote_vault"):
        return {**из_, "why_not": "пул не опознан"}
    if q in SOLы and not SOL_ПУЛ:
        return {**из_, "why_not": "котировка SOL -- основной симулятор"}
    import c2_pool_programs as PP  # noqa: PLC0415
    прог = PP.pool_program(tsrc, пул["pool_vault"], PP.labels()).get("pool_program")
    из_["program"] = прог
    if прог == S.SB.LAUNCHLAB:
        режим = "launchlab"
    elif прог in ПРОГРАММЫ_XYK:
        режим = "xyk"
    else:
        return {**из_, "why_not": f"программа пула {прог}: хранилища не x*y=k-резервы, режим 2 не проверен"}
    из_["режим"] = режим
    ст0 = S.состояние(tsrc, режим, пул, п["mint"])
    if not ст0:
        return {**из_, "why_not": "состояние после источника не читается"}
    if q in SOLы:
        цена_q, откуда = 1.0, "котировка SOL"
    else:
        цена_q, откуда = цена_котировочного(уз, tsrc, q)
    из_["цена_q_откуда"] = откуда
    if not цена_q:
        return {**из_, "why_not": f"курс котировочного: {откуда}"}
    нал_т = S.налог_минта(уз, п["mint"])
    нал_q = {"bps": 0} if q in SOLы else S.налог_минта(уз, q)
    из_.update(налог_токена_bps=нал_т.get("bps"), налог_q_bps=нал_q.get("bps"))
    for чей, нал in (("токена", нал_т), ("котировочного", нал_q)):
        if нал.get("why_not"):
            return {**из_, "why_not": f"налог {чей} не прочитан: {нал['why_not'][:80]}"}
    ист = S.история_пула(уз, пул["pool_vault"], п["signature"], s0,
                         опора=(S.подпись_после_слота(уз, s0 + max(ГОРИЗОНТЫ) + 1) if уз.текущий == "helius" else None),
                         до_слота=s0 + max(ГОРИЗОНТЫ))
    if ист["why_not"] or ист["предел"]:
        return {**из_, "why_not": f"история пула: {ист['why_not'] or 'предел страниц'}"}
    сп = [з for з in ист["подписи"] if з["ok"]]
    из_["окно_полное"] = S.текущий_слот(уз) > s0 + max(ГОРИЗОНТЫ)
    из_["толпа_s0_2"] = sum(1 for з in сп if з["slot"] <= s0 + 2)
    кэш: dict = {}
    не_читаются: list = []

    def txi(i):
        if i not in кэш:
            try:
                кэш[i] = уз.tx(сп[i]["signature"])
            except RuntimeError:              # как ст_до основного симулятора
                кэш[i] = None
                не_читаются.append(сп[i]["signature"])
        return кэш[i]

    def ст(i):
        # не дальше ШАГОВ_НАЗАД назад, как в основном симуляторе
        for j in range(i, max(-1, i - S.ШАГОВ_НАЗАД - 1), -1):
            с = S.состояние(txi(j), режим, пул, п["mint"])
            if с:
                return с
        return None

    def ст_до(слот):
        канд = [i for i, з in enumerate(сп) if з["slot"] <= слот]
        return (ст(канд[-1]) if канд else ст0)

    # калибровки
    if режим == "launchlab":
        мо = S.SB.launchlab_min_out(tsrc, 10 ** 6, 0.0)
        f = мо.get("fee_rate") if мо.get("ok") else None      # доля КОМИССИИ (как в основном симуляторе)
        из_["f"], из_["f_откуда"] = f, "событие LaunchLab источника"
        if f is None:
            return {**из_, "why_not": "LaunchLab: нет события сделки источника"}
        g = None
    else:
        f_ист = A3.f_по_хранилищам(tsrc, пул)
        из_["f_источника"] = round(f_ист, 5) if f_ист else None
        f = f_ист if f_ист and 0.95 <= f_ист <= 1.0 else None
        if f:
            из_["f_откуда"] = "хранилища, сделка источника"
        else:
            # как в A3 (прогон 5): ближайшая покупка пула после источника, окно 0.5–1.0
            for i in range(min(len(сп), 30)):
                ff = A3.f_по_хранилищам(txi(i), пул)
                if ff and 0.5 <= ff <= 1.0:
                    f = ff
                    из_["f_откуда"] = "ближайшая покупка пула после источника"
                    break
        из_["f"] = round(f, 5) if f else None
        if not f:
            return {**из_, "why_not": "доля траты не калибруется"}
        доли_g = []
        for i in range(min(len(сп), 80)):
            т = txi(i)
            кв, тв = A3.дельта_счёта(т, пул["quote_vault"]), A3.дельта_счёта(т, пул["pool_vault"])
            if кв and тв and тв[1] > тв[0] and кв[1] < кв[0] and кв[0] > 0:
                dy, dx = тв[1] - тв[0], кв[0] - кв[1]
                g_ = dx * (тв[0] + dy) / (кв[0] * dy)
                if 0.5 <= g_ <= 1.0:
                    доли_g.append(g_)
            if len(доли_g) >= 8:
                break
        g = statistics.median(доли_g) if доли_g else G_БЕЗ_ПРОДАЖ
        из_["g"], из_["g_откуда"] = round(g, 5), (f"продажи пула ({len(доли_g)})" if доли_g else "продаж нет: 0.985")

    def спот(с):
        return с["x"] / с["y"] if режим == "xyk" else с["quote"] / с["base"]

    # наш вход
    q_raw = int(РАЗМЕР / цена_q)
    q_raw -= S.удержано(q_raw, нал_q)            # пул q/SOL -> мы
    q_raw -= S.удержано(q_raw, нал_q)            # мы -> пул токена
    p0 = спот(ст0)
    точки = {"S0": ст0, "S0_дно": ст_до(s0), "S1": ст_до(s0 + 1), "S2": ст_до(s0 + 2)}
    выходы = {H: ст_до(s0 + H - 1) for H in ГОРИЗОНТЫ}
    из_["путь_цены"] = {к: (round((спот(с) / p0 - 1) * 100, 3) if с else None)
                        for к, с in точки.items() if к != "S0"}
    из_["резерв_q_s0"] = ст0["x"] if режим == "xyk" else ст0["rq"]
    из_["резерв_s0_sol"] = round(из_["резерв_q_s0"] * цена_q / 1e9, 3)
    def выход(вх_, св, вставка=True):
        т_брутто, в_пул_вход, в_пул = вх_
        if режим == "xyk":
            X, Y = (св["x"] + в_пул_вход, св["y"] - т_брутто) if вставка else (св["x"], св["y"])
            q_out = int(X * g * в_пул / (Y + в_пул)) if Y > 0 else None
        else:
            покупка = {"в_пул": в_пул_вход, "tokens": т_брутто} if вставка else {"в_пул": 0, "tokens": 0}
            пр = S.наша_продажа("launchlab", св, покупка, в_пул, f=f, кривая_bps=(0, 0), g=None)
            q_out = пр["lamports"] if пр.get("ok") else None
        if q_out is None:
            return None
        q_out -= S.удержано(q_out, нал_q)      # пул токена -> мы
        q_out -= S.удержано(q_out, нал_q)      # мы -> пул q/SOL
        return S.чистый_пп(int(q_out * цена_q))

    res: dict = {}
    наши: dict = {}
    for к, с in точки.items():
        if not с:
            res[к] = None
            continue
        if режим == "xyk":
            x, y = с["x"], с["y"]
            т_брутто = int(y * f * q_raw / (x + f * q_raw))
            в_пул_вход = q_raw
        else:
            пок = S.наша_покупка("launchlab", с, f=f, кривая_bps=(0, 0), размер=q_raw)
            т_брутто = пок["tokens"] if пок.get("ok") else 0
            в_пул_вход = пок.get("в_пул") or 0
        if т_брутто <= 0:
            res[к] = None
            continue
        т = т_брутто - S.удержано(т_брутто, нал_т)
        в_пул = т - S.удержано(т, нал_т)
        наши[к] = (т_брутто, в_пул_вход, в_пул)
        р = {"наценка_пп": round(((q_raw / т_брутто) / p0 - 1) * 100, 3)}
        for H, св in выходы.items():
            for вид, вставка in (("потолок", True), ("дно", False)):
                р[f"{вид}_{H}"] = выход(наши[к], св, вставка) if св else None
        res[к] = р
    из_["входы"] = res
    ВХ_М = [к for к in ("S0", "S0_дно") if к in наши]
    if МАКС_ОКНА:
        # максимум по свопам пула в окне: состояние после каждого свопа (или
        # конец слота / шаг, если свопов больше МАКС_ЧТЕНИЙ) -- наш выход-потолок
        idx = [i for i, з in enumerate(сп) if з["slot"] <= s0 + max(МАКС_ОКНА) - 1]
        всего = len(idx)
        способ = "все свопы"
        if len(idx) > МАКС_ЧТЕНИЙ:
            посл: dict = {}
            for i in idx:
                посл[сп[i]["slot"]] = i
            idx = sorted(посл.values())
            способ = "конец слота"
            if len(idx) > МАКС_ЧТЕНИЙ:
                шаг = len(idx) / МАКС_ЧТЕНИЙ
                idx = sorted({idx[int(k * шаг)] for k in range(МАКС_ЧТЕНИЙ)} | {idx[-1]})
                способ = "конец слота, шаг"
        нужны = [сп[i]["signature"] for i in idx if i not in кэш]
        if нужны:
            пак = уз.пакет(нужны)
            for i in idx:
                if i not in кэш and сп[i]["signature"] in пак:
                    кэш[i] = пак[сп[i]["signature"]]
        макс: dict = {к: {} for к in ВХ_М}
        макс["спот_пп"] = {}
        прочитано = 0
        for i in idx:
            с = S.состояние(txi(i), режим, пул, п["mint"])
            if not с:
                continue
            прочитано += 1
            сл = сп[i]["slot"]
            сп_пп = (спот(с) / p0 - 1) * 100
            # окна: до +W (вкл. s0+W-1, как выход по таймеру); «после_150» --
            # слоты s0+150 .. s0+max-1 (то, что таймер +150 оставляет на столе)
            for W in list(МАКС_ОКНА) + ["после_150"]:
                if W == "после_150":
                    if not (s0 + 150 <= сл <= s0 + max(МАКС_ОКНА) - 1):
                        continue
                elif сл > s0 + W - 1:
                    continue
                м = макс["спот_пп"].get(str(W))
                if м is None or сп_пп > м["пп"]:
                    макс["спот_пп"][str(W)] = {"пп": round(сп_пп, 3), "слот": сл}
                for к in ВХ_М:
                    v = выход(наши[к], с)
                    м = макс[к].get(str(W))
                    if v is not None and (м is None or v > м["пп"]):
                        макс[к][str(W)] = {"пп": v, "слот": сл}
        из_["максимум"] = макс
        из_["максимум_чтение"] = {"свопов_в_окне": всего, "прочитано": прочитано, "способ": способ}
    if ПРОДАЖИ:
        из_["продажи"] = продажи_источника(уз, tsrc, п, s0, пул, режим, ВХ_М, наши, выход, спот, p0)
    из_["не_читаются"] = len(не_читаются)
    из_["флаги"] = ["курс котировочного к SOL на выходе = на входе (шаг 2)",
                    "проскальзывание нашей ноги SOL<->котировочный не моделируется"]
    return из_


def продажи_источника(уз, tsrc, п, s0, пул, режим, ВХ_М, наши, выход, спот, p0) -> dict:
    """Продажи источника по его счетам минта (после сделки источника): слоты,
    доля остатка; наш выход-потолок по состоянию пула сразу после первой продажи
    (сделка продажи, если она через этот пул, иначе последняя сделка пула до неё)."""
    счета = sorted({r["account"] for r in C.token_rows(tsrc).values()
                    if r["owner"] == п["wallet"] and r["mint"] == п["mint"] and r["account"]})
    if not счета:
        return {"why_not": "счёт минта источника в сделке не найден"}
    подп: dict = {}
    for сч in счета:
        до = None
        for _ in range(3):
            стр = уз.подписи(сч, до=до, по=п["signature"], limit=1000)
            for з in стр:
                if з.get("err") is None and (з.get("slot") or 0) >= s0:
                    подп[з["signature"]] = з.get("slot")
            if len(стр) < 1000:
                break
            до = стр[-1]["signature"]
    порядок = sorted(подп, key=lambda x: подп[x])[:60]
    txs = уз.пакет(порядок) if порядок else {}
    продажи = []
    for с_ in порядок:
        т = txs.get(с_)
        if not т or (т.get("meta") or {}).get("err") is not None:
            continue
        ряды = [r for r in C.token_rows(т).values() if r["owner"] == п["wallet"] and r["mint"] == п["mint"]]
        до_, после = sum(r["pre"] for r in ряды), sum(r["post"] for r in ряды)
        if после < до_:
            продажи.append({"slot": т.get("slot"), "signature": с_, "доля": round((до_ - после) / до_, 4) if до_ else None,
                            "через_пул": S.состояние(т, режим, пул, п["mint"]) is not None, "_tx": т})
        if len(продажи) >= 10:
            break
    из_ = {"счетов": len(счета), "подписей_счёта": len(подп), "всего_продаж_найдено": len(продажи),
           "слоты": [{к: v for к, v in x.items() if к != "_tx"} for x in продажи]}
    if not продажи:
        из_["why_not"] = "продаж после покупки не найдено (до времени скана)"
        return из_
    п1 = продажи[0]
    ст_ = S.состояние(п1["_tx"], режим, пул, п["mint"])
    if not ст_:
        пред = уз.подписи(пул["pool_vault"], до=п1["signature"], limit=5)
        for з in пред:
            if з.get("err") is None:
                ст_ = S.состояние(уз.tx(з["signature"]), режим, пул, п["mint"])
                if ст_:
                    break
    if not ст_:
        из_["why_not"] = "состояние пула у первой продажи не читается"
        return из_
    из_["первая"] = {"slot": п1["slot"], "слотов_от_s0": п1["slot"] - s0, "доля": п1["доля"],
                     "спот_пп": round((спот(ст_) / p0 - 1) * 100, 3),
                     "выход": {к: выход(наши[к], ст_) for к in ВХ_М}}
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--s", type=int, default=0)
    р.add_argument("--po", type=int, default=0)
    р.add_argument("--push", action="store_true")
    р.add_argument("--spisok", default="rezhim2_spisok.json")
    р.add_argument("--prefiks", default="")
    р.add_argument("--gorizonty", default="72,150")
    р.add_argument("--maks", default="", help="окна максимума по свопам, напр. 36,150,600,1800")
    р.add_argument("--prodazhi", action="store_true")
    р.add_argument("--sol-pul", action="store_true")
    а = р.parse_args()
    global ГОРИЗОНТЫ, МАКС_ОКНА, ПРОДАЖИ, SOL_ПУЛ
    ГОРИЗОНТЫ = tuple(int(x) for x in а.gorizonty.split(","))
    МАКС_ОКНА = tuple(int(x) for x in а.maks.split(",") if x)
    ПРОДАЖИ, SOL_ПУЛ = а.prodazhi, а.sol_pul
    if МАКС_ОКНА and max(МАКС_ОКНА) > max(ГОРИЗОНТЫ):
        ГОРИЗОНТЫ = tuple(sorted(set(ГОРИЗОНТЫ) | {max(МАКС_ОКНА)}))
    import podbivka_run as R  # noqa: PLC0415
    спис = json.loads((КОРЕНЬ / "data" / "podbivka" / а.spisok).read_text(encoding="utf-8"))["покупки"]
    спис = спис[а.s:(а.po or None)]
    out = КОРЕНЬ / "data" / "podbivka" / "rezhim2" / f"{а.prefiks}{а.s}_{а.po or 'end'}.jsonl"
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
    with open(out, "a", encoding="utf-8") as ф:
        for н, п in enumerate(спис):
            if п["signature"] in готово:
                continue
            with уз.на(S.узел_по_времени(п.get("blockTime"))):
                try:
                    рез = одна(уз, п)
                except Exception as exc:  # noqa: BLE001
                    рез = {**{к: п.get(к) for к in БАЗА}, "why_not": S.чисто(f"{type(exc).__name__}: {exc}")[:200]}
            уз._кэш.clear()  # noqa: SLF001
            рез["узел"] = S.узел_по_времени(п.get("blockTime"))
            ф.write(json.dumps(рез, ensure_ascii=False) + "\n")
            ф.flush()
            if а.push and time.time() - последний > 600:
                R.пуш(f"Podbivka-2: rezhim2 {а.s}+{н + 1} [automated]", [str(out)])
                последний = time.time()
    if а.push:
        R.пуш(f"Podbivka-2: rezhim2 {а.s}-{а.po or 'end'} gotovo [automated]", [str(out)])
    print(f"rezhim2: {len(спис)} покупок, расход {json.dumps(уз.расход(), ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
