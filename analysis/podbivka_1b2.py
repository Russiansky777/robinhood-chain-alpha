#!/usr/bin/env python3
"""Подбивка, п.1б заново: лидер и Brez 18-25.09 -- отбор симулятора против DBot.

ЧТО БЫЛО НЕ ТАК (26.09, первая сессия): у лидера совпало 2 из 37. Сопоставление
шло по «минт + наша покупка в 180 с», а покупкой источника считалась только
трата SOL. Сырые записи DBot (data/dbot_follow_trades_raw.json) показывают:
ВСЕ покупки лидера и Brez оплачены USDC (follow.send = USDC), и DBot пишет
точное число токенов, полученных источником (follow.receive.amount). Поэтому:
  * покупка источника -- любая трата котировки (SOL, WSOL, USDC, USDT);
    стейблы переводятся в SOL-экв по курсу из цепи (c2_common.RateBook);
  * запись DBot сопоставляется с покупкой источника по ТОЧНОМУ числу токенов
    того же минта, а если числа нет -- по ближайшему времени в пределах 120 с.

ТРИ СПИСКА (слово владельца):
  совпало                 -- первая покупка по нашему отбору и сделка DBot под неё;
  у_симулятора_без_DBot   -- наш отбор взял, DBot не купил: причина -- из его
                             же журнала (errorMessage / skipReason), а если
                             записи нет вовсе -- так и сказано;
  у_DBot_без_симулятора   -- DBot купил, наш отбор не взял: это ошибка отбора,
                             причина названа (не первая, ниже порога, нет курса,
                             покупки в цепи не нашлось).
ЧИСЛА по совпавшим: симулятор при ФАКТИЧЕСКОМ слоте нашей покупки и
фактическом размере (send.amount минус комиссия DBot) против токенов по цепи.

Только чтение. Строки ошибок -- через чисто().
"""
from __future__ import annotations

import argparse
import calendar
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import podbivka as P  # noqa: E402
import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
ОКНО_ВРЕМЕНИ_С = 120


def в_секунды(строка: str) -> int:
    return calendar.timegm(time.strptime(строка, "%Y-%m-%dT%H:%M:%SZ"))


def записи_dbot(путь: Path, адреса: set, с_ts: int, до_ts: int) -> list:
    д = json.loads(путь.read_text(encoding="utf-8"))
    из_ = []
    for v in д.values():
        r = v.get("record") or {}
        сл = r.get("follow") or {}
        if r.get("type") != "buy" or сл.get("wallet") not in адреса:
            continue
        ts = int(r.get("timestamp") or (r.get("createAt") or 0) // 1000)
        if not (с_ts <= ts < до_ts):
            continue
        пол = (сл.get("receive") or {})
        отд = (сл.get("send") or {})
        из_.append({
            "id": r.get("id"), "task": r.get("configName"), "our_wallet": r.get("wallet"),
            "ts": ts, "state": r.get("state"), "error": (r.get("errorMessage") or "")[:120],
            "skip": r.get("skipReason"), "source": сл.get("wallet"),
            "mint": ((пол.get("info") or {}).get("contract")),
            "src_tokens": int(пол.get("amount") or 0) if str(пол.get("amount") or "").isdigit() else None,
            "src_quote": ((отд.get("info") or {}).get("symbol")),
            "src_quote_amount": отд.get("amount"),
            "our_send": int((r.get("send") or {}).get("amount") or 0)
            if str((r.get("send") or {}).get("amount") or "").isdigit() else None,
            "dbot_fee": int(r.get("dbotFee") or 0) if str(r.get("dbotFee") or "").isdigit() else 0,
            "our_tokens": (r.get("receive") or {}).get("amount"),
        })
    return из_


def журнал_сделок(путь: Path, адреса: set) -> list:
    return [т for т in json.loads(путь.read_text(encoding="utf-8"))
            if т.get("source_address") in адреса and т.get("buy_signature")]


def сопоставить(запись: dict, покупки: list) -> tuple:
    """(покупка источника, способ) для записи DBot или (None, причина)."""
    same = [п for п in покупки if п["mint"] == запись["mint"]]
    if not same:
        return None, "в цепи нет покупок источника этим минтом в окне"
    if запись.get("src_tokens"):
        точно = [п for п in same if п["tokens_raw"] == запись["src_tokens"]]
        if точно:
            return min(точно, key=lambda п: abs((п["blockTime"] or 0) - запись["ts"])), "по числу токенов"
    близко = [п for п in same if abs((п["blockTime"] or 0) - запись["ts"]) <= ОКНО_ВРЕМЕНИ_С]
    if близко:
        return min(близко, key=lambda п: abs((п["blockTime"] or 0) - запись["ts"])), "по времени"
    return None, "покупки этим минтом есть, но ни число токенов, ни время (120 с) не сходятся"


def доли_котировок(покупки: list) -> dict:
    из_: dict = {}
    for п in покупки:
        к = п.get("котировка_пула") or "нет данных"
        из_[к] = из_.get(к, 0) + 1
    n = sum(из_.values())
    return {к: {"n": v, "доля": round(v / n, 3)} for к, v in sorted(из_.items())} if n else {}


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--istochniki", default=("Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit=leader,"
                                            "Fvkc2thk1YcAASdR2gi8uf9n67JW9Dqqr9iRd99MDhoB=Brez"))
    р.add_argument("--s", default="2026-09-18T00:00:00Z")
    р.add_argument("--do", default="2026-09-26T00:00:00Z")
    р.add_argument("--dbot", default=str(КОРЕНЬ / "data" / "dbot_follow_trades_raw.json"))
    р.add_argument("--ledger", default=str(КОРЕНЬ / "data" / "solana_trades_all.json"))
    р.add_argument("--out", default=str(КОРЕНЬ / "data" / "podbivka" / "p1b2_sverka.json"))
    а = р.parse_args()
    адреса = {}
    for кус in а.istochniki.split(","):
        адрес, _, имя = кус.strip().partition("=")
        адреса[адрес] = имя or адрес[:6]
    с_ts, до_ts = в_секунды(а.s), в_секунды(а.do)
    уз = S.Узел()
    курс = S.КурсПулом(S.КурсУзла(уз))
    начало = time.time()
    dbot = записи_dbot(Path(а.dbot), set(адреса), с_ts, до_ts)
    журнал = журнал_сделок(Path(а.ledger), set(адреса))
    итог: dict = {"окно": {"с": а.s, "до": а.do}, "по_источнику": {}, "ряды_чисел": [],
                  "отказы_чисел": []}
    пары_для_чисел = []
    for адрес, имя in адреса.items():
        ск = S.скан_кошелька(уз, адрес, с_ts, до_ts, курс=курс, все_покупки=True)
        отбор = [п for п in ск["покупки"] if п.get("порог")]
        без_курса = [п for п in ск["покупки"] if not п.get("порог")]
        все = ск["все_покупки"]
        записи = [з for з in dbot if з["source"] == адрес]
        # запись DBot -> покупка источника в цепи
        по_подписи: dict = {}
        несопоставлены = []
        for з in записи:
            п, как = сопоставить(з, все)
            з["сопоставлено"] = как
            if п:
                з["source_sig"] = п["signature"]
                по_подписи.setdefault(п["signature"], []).append(з)
            else:
                несопоставлены.append(з)
        совпало, сим_без_dbot, dbot_без_сим = [], [], []
        отобраны = {п["signature"] for п in отбор}
        for п in отбор:
            зз = по_подписи.get(п["signature"]) or []
            сделки = [з for з in зз if з["state"] == "done"]
            if сделки:
                совпало.append({"source_sig": п["signature"], "mint": п["mint"], "slot": п["slot"],
                                "utc": S.utc(п["blockTime"]), "sol_экв": п["sol_экв"],
                                "порог": п["порог"], "dbot": сделки})
                пары_для_чисел.extend({"покупка": п, "запись": з} for з in сделки)
            else:
                причины = sorted({(з["error"] or з["skip"] or "без текста") for з in зз})
                сим_без_dbot.append({"source_sig": п["signature"], "mint": п["mint"],
                                     "utc": S.utc(п["blockTime"]), "sol_экв": п["sol_экв"],
                                     "порог": п["порог"],
                                     "причина": ("; ".join(f"DBot: {x}" for x in причины) if причины
                                                 else "записи DBot под эту покупку нет вовсе")})
        по_сиг_всех = {п["signature"]: п for п in все}
        for з in записи:
            if з["state"] != "done":
                continue
            сиг = з.get("source_sig")
            if сиг and сиг in отобраны:
                continue
            п = по_сиг_всех.get(сиг) if сиг else None
            if п is None:
                почему = f"покупка источника в цепи не найдена ({з['сопоставлено']})"
            elif not п["первая"]:
                почему = "покупка не первая: пред-баланс минта у источника не ноль"
            else:
                пк = next((x for x in ск["покупки"] if x["signature"] == сиг), None)
                if пк and not пк.get("порог") and пк.get("why_not"):
                    почему = пк["why_not"]
                else:
                    почему = (f"ниже порога 2 SOL-экв по нашему счёту (SOL {п['трата_sol']:.3f}, "
                              f"USD {п['трата_usd']:.2f})")
            dbot_без_сим.append({"id": з["id"], "task": з["task"], "mint": з["mint"],
                                 "utc": S.utc(з["ts"]), "src_quote": з["src_quote"],
                                 "src_quote_amount": з["src_quote_amount"],
                                 "сопоставлено": з["сопоставлено"], "почему": почему})
        причины_dbot: dict = {}
        for x in сим_без_dbot:
            причины_dbot[x["причина"][:90]] = причины_dbot.get(x["причина"][:90], 0) + 1
        причины_отбора: dict = {}
        for x in dbot_без_сим:
            к = x["почему"][:90]
            причины_отбора[к] = причины_отбора.get(к, 0) + 1
        итог["по_источнику"][имя] = {
            "адрес": адрес,
            "подписей_в_окне": ск["подписей"], "разобрано": ск["разобрано"],
            "покрытие_окна": ск["покрытие"], "узел_не_отдал": ск["не_отдал"],
            "покупок_всех": len(все), "первых_от_2_sol": len(отбор),
            "первых_без_курса": len(без_курса),
            "котировка_пула_первых_от_2": доли_котировок(отбор),
            "котировка_пула_всех_первых": доли_котировок(ск["покупки"]),
            "записей_dbot_buy": len(записи),
            "сделок_dbot_done": sum(1 for з in записи if з["state"] == "done"),
            "записей_dbot_не_сопоставлено": len(несопоставлены),
            "совпало": len(совпало), "у_симулятора_без_DBot": len(сим_без_dbot),
            "у_DBot_без_симулятора": len(dbot_без_сим),
            "причины_у_симулятора_без_DBot": причины_dbot,
            "причины_у_DBot_без_симулятора": причины_отбора,
            "список_совпало": совпало,
            "список_у_симулятора_без_DBot": сим_без_dbot,
            "список_у_DBot_без_симулятора": dbot_без_сим,
            "список_без_курса": без_курса,
            "скан_why_not": ск["why_not"],
        }
    # Числа: симулятор на фактическом слоте и размере против цепи.
    for пара in пары_для_чисел:
        п, з = пара["покупка"], пара["запись"]
        наши = [т for т in журнал if т.get("mint") == п["mint"]
                and т.get("wallet") == з["our_wallet"]
                and abs((т.get("buy_block_time") or 0) - з["ts"]) <= ОКНО_ВРЕМЕНИ_С]
        строка = {"source_sig": п["signature"], "mint": п["mint"], "task": з["task"],
                  "id": з["id"]}
        if not наши:
            строка["why_not"] = "нашей сделки под запись DBot в журнале сделок нет"
            итог["отказы_чисел"].append(строка)
            continue
        наша = min(наши, key=lambda т: abs((т.get("buy_block_time") or 0) - з["ts"]))
        строка["our_sig"] = наша["buy_signature"]
        try:
            with уз.на(S.узел_по_времени(наша.get("buy_block_time"))):
                tx_наша = уз.tx(наша["buy_signature"])
        except RuntimeError as exc:
            строка["why_not"] = S.чисто(str(exc))[:120]
            итог["отказы_чисел"].append(строка)
            continue
        факт = P.факт_покупки(tx_наша, з["our_wallet"], п["mint"])
        if not факт.get("tokens_raw"):
            строка["why_not"] = факт.get("why_not") or "токенов по факту нет"
            итог["отказы_чисел"].append(строка)
            continue
        размер = (з["our_send"] or 0) - (з["dbot_fee"] or 0)
        сим = S.симулировать(уз, {**п, "wallet": з["source"]}, наш_слот=tx_наша.get("slot"),
                             наша_трата_лам=размер, горизонты=(1,))
        уз._кэш.clear()  # noqa: SLF001
        фс = сим.get("факт_слот") or {}
        строка.update(наш_слот=tx_наша.get("slot"), слотов_после=(tx_наша.get("slot") or 0) - (п["slot"] or 0),
                      размер_sol=round(размер / S.ЛАМПОРТОВ, 6), факт_токенов=факт["tokens_raw"],
                      сим_токенов=фс.get("tokens_net"), режим=сим.get("режим"),
                      программа=сим.get("program"))
        if сим.get("why_not") or not фс.get("tokens_net"):
            строка["why_not"] = сим.get("why_not") or фс.get("why_not") or "симулятор не дал числа"
            итог["отказы_чисел"].append(строка)
            continue
        строка["расхождение_пп"] = round((факт["tokens_raw"] / фс["tokens_net"] - 1) * 100, 3)
        итог["ряды_чисел"].append(строка)
    абс = sorted(abs(r["расхождение_пп"]) for r in итог["ряды_чисел"])
    причины: dict = {}
    for о in итог["отказы_чисел"]:
        причины[о["why_not"][:90]] = причины.get(о["why_not"][:90], 0) + 1
    итог["числа"] = {
        "пар": len(пары_для_чисел), "сверено": len(абс), "отказов": len(итог["отказы_чисел"]),
        "медиана_пп": round(statistics.median(абс), 3) if абс else None,
        "p90_пп": round(абс[min(len(абс) - 1, int(round(0.9 * (len(абс) - 1))))], 3) if абс else None,
        "причины_отказов": причины}
    итог["расход_узла"] = уз.расход()
    итог["секунд"] = round(time.time() - начало, 1)
    итог["utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    Path(а.out).parent.mkdir(parents=True, exist_ok=True)
    Path(а.out).write_text(json.dumps(итог, ensure_ascii=False, indent=1), encoding="utf-8")
    for имя, и in итог["по_источнику"].items():
        print(f"1б {имя}: покрытие {и['покрытие_окна']} | первых от 2 SOL {и['первых_от_2_sol']} | "
              f"DBot done {и['сделок_dbot_done']} | совпало {и['совпало']} | у сим без DBot "
              f"{и['у_симулятора_без_DBot']} | у DBot без сим {и['у_DBot_без_симулятора']} | "
              f"котировка пула (первые от 2 SOL): {json.dumps(и['котировка_пула_первых_от_2'], ensure_ascii=False)}")
    ч = итог["числа"]
    print(f"1б числа: сверено {ч['сверено']}/{ч['пар']} медиана {ч['медиана_пп']} p90 {ч['p90_пп']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
