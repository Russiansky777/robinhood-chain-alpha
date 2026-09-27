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


БАЗА = ("signature", "mint", "wallet", "sol_экв", "группа")
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
    if q in SOLы:
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
    цена_q, откуда = цена_котировочного(уз, tsrc, q)
    из_["цена_q_откуда"] = откуда
    if not цена_q:
        return {**из_, "why_not": f"курс котировочного: {откуда}"}
    нал_т, нал_q = S.налог_минта(уз, п["mint"]), S.налог_минта(уз, q)
    из_.update(налог_токена_bps=нал_т.get("bps"), налог_q_bps=нал_q.get("bps"))
    for чей, нал in (("токена", нал_т), ("котировочного", нал_q)):
        if нал.get("why_not"):
            return {**из_, "why_not": f"налог {чей} не прочитан: {нал['why_not'][:80]}"}
    ист = S.история_пула(уз, пул["pool_vault"], п["signature"], s0,
                         опора=(S.подпись_после_слота(уз, s0 + 151) if уз.текущий == "helius" else None),
                         до_слота=s0 + 150)
    if ист["why_not"] or ист["предел"]:
        return {**из_, "why_not": f"история пула: {ист['why_not'] or 'предел страниц'}"}
    сп = [з for з in ист["подписи"] if з["ok"]]
    из_["окно_полное"] = S.текущий_слот(уз) > s0 + 150
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
    выходы = {H: ст_до(s0 + H - 1) for H in (72, 150)}
    из_["путь_цены"] = {к: (round((спот(с) / p0 - 1) * 100, 3) if с else None)
                        for к, с in точки.items() if к != "S0"}
    из_["резерв_q_s0"] = ст0["x"] if режим == "xyk" else ст0["rq"]
    из_["резерв_s0_sol"] = round(из_["резерв_q_s0"] * цена_q / 1e9, 3)
    res: dict = {}
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
        р = {"наценка_пп": round(((q_raw / т_брутто) / p0 - 1) * 100, 3)}
        for H, св in выходы.items():
            if not св:
                р[f"потолок_{H}"] = р[f"дно_{H}"] = None
                continue
            for вид, вставка in (("потолок", True), ("дно", False)):
                if режим == "xyk":
                    X, Y = (св["x"] + в_пул_вход, св["y"] - т_брутто) if вставка else (св["x"], св["y"])
                    q_out = int(X * g * в_пул / (Y + в_пул)) if Y > 0 else None
                else:
                    покупка = {"в_пул": в_пул_вход, "tokens": т_брутто} if вставка else {"в_пул": 0, "tokens": 0}
                    пр = S.наша_продажа("launchlab", св, покупка, в_пул, f=f, кривая_bps=(0, 0), g=None)
                    q_out = пр["lamports"] if пр.get("ok") else None
                if q_out is None:
                    р[f"{вид}_{H}"] = None
                    continue
                q_out -= S.удержано(q_out, нал_q)      # пул токена -> мы
                q_out -= S.удержано(q_out, нал_q)      # мы -> пул q/SOL
                р[f"{вид}_{H}"] = S.чистый_пп(int(q_out * цена_q))
        res[к] = р
    из_["входы"] = res
    из_["не_читаются"] = len(не_читаются)
    из_["флаги"] = ["курс котировочного к SOL на выходе = на входе (шаг 2)",
                    "проскальзывание нашей ноги SOL<->котировочный не моделируется"]
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--s", type=int, default=0)
    р.add_argument("--po", type=int, default=0)
    р.add_argument("--push", action="store_true")
    р.add_argument("--spisok", default="rezhim2_spisok.json")
    р.add_argument("--prefiks", default="")
    а = р.parse_args()
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
