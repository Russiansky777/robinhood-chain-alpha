#!/usr/bin/env python3
"""Раскладка нашей двухшаговой сборки против РЕАЛЬНЫХ сделок лидера.

ЗАЧЕМ (условие владельца 27.09 перед боем, п.3): "раскладку инструкций сверить
с реальными транзакциями лидера по его GP-парам (например CRACKER) до совпадения
программ и счетов; затем три живые сделки по 0.01".

ЧТО ДЕЛАЕТ. Берёт последние транзакции лидера по цепи, оставляет его покупки с
котировкой НЕ SOL, и по каждой:
  * находит пул SOL <-> котировочный токен (сначала в самой транзакции лидера,
    потом по свежим сделкам котировочного минта) и берёт из него шаблон первого
    шага -- ровно тем же кодом, которым это делает служба (c2_shadow_build);
  * собирает НАШУ двухшаговую транзакцию на наш адрес;
  * сравнивает ИНСТРУКЦИЮ ВТОРОГО ШАГА с инструкцией самого лидера: программа,
    число счетов и список счетов позиция за позицией. Расхождение не
    "похоже/непохоже", а названный номер счёта и два адреса.

Ни подписи, ни отправки. Наш адрес нужен только как плательщик в сборке.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(КОРЕНЬ / "analysis"))

WSOL = "So11111111111111111111111111111111111111112"


def узел() -> str:
    к = (os.environ.get("HELIUS_API_KEY2")
         or os.environ.get("HELIUS_API")
         or os.environ.get("HELIUS_API_KEY") or "").strip()
    if not к:
        raise SystemExit("СБОЙ: HELIUS_API не задан")
    return f"https://mainnet.helius-rpc.com/?api-key={к}"


def rpc(метод: str, параметры: list, *, повторов: int = 4):
    тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                       "params": параметры}).encode()
    пауза, последняя = 0.4, None
    for _ in range(повторов):
        req = urllib.request.Request(узел(), data=тело,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                о = json.loads(r.read().decode())
            if "error" in о:
                последняя = str(о["error"])[:200]
            else:
                return о.get("result")
        except Exception as exc:  # noqa: BLE001
            последняя = type(exc).__name__
        time.sleep(пауза)
        пауза *= 2
    raise RuntimeError(f"{метод} не ответил: {последняя}")


def транзакция(подпись: str):
    return rpc("getTransaction", [подпись, {"encoding": "jsonParsed",
                                             "maxSupportedTransactionVersion": 1}])


def сделки(адрес: str, предел: int) -> list:
    п = rpc("getSignaturesForAddress", [адрес, {"limit": предел}]) or []
    return [з["signature"] for з in п if not з.get("err")]


def налог_минта(минт: str) -> dict:
    """Налог на перевод у минта Token-2022: {"taxed", "fee_bps"}.

    Читается из расширений самого минта (transferFeeConfig), а не берётся на
    глаз: это число прямо уменьшает то, что доходит до пула.
    """
    из_ = {"taxed": False, "fee_bps": 0}
    try:
        о = rpc("getAccountInfo", [минт, {"encoding": "jsonParsed"}]) or {}
    except Exception as exc:  # noqa: BLE001
        return {"taxed": None, "fee_bps": None, "why_not": type(exc).__name__}
    инфо = ((((о.get("value") or {}).get("data") or {}).get("parsed") or {})
            .get("info") or {})
    for расш in инфо.get("extensions") or []:
        if расш.get("extension") != "transferFeeConfig":
            continue
        сост = расш.get("state") or {}
        for ключ in ("newerTransferFee", "olderTransferFee"):
            bps = ((сост.get(ключ) or {}).get("transferFeeBasisPoints"))
            if isinstance(bps, int):
                return {"taxed": bps > 0, "fee_bps": bps}
    return из_


def пулы_sol_q(tx: dict, C, B, программы: set) -> list:
    """(программа, хранилище Q, хранилище WSOL, Q) для пулов SOL<->Q в транзакции.

    Тот же разбор, что у c2_twohop второй сессии: одно хранилище WSOL, другое --
    котировочный токен, и владелец обоих не подписант.
    """
    ряды = {r["account"]: r for r in C.token_rows(tx).values()}
    из_ = []
    for ix in B.all_instructions(tx):
        if ix.get("programId") not in программы:
            continue
        свои = [ряды[a] for a in ix["accounts"]
                if a in ряды and ряды[a]["owner"] not in C.signers(tx)]
        w = [r for r in свои if r["mint"] == C.WSOL]
        q = [r for r in свои if r["mint"] != C.WSOL]
        for qr in q:
            for wr in w:
                if qr["owner"] == wr["owner"] or ix["programId"] in (B.DLMM, B.DAMM2, B.CLMM):
                    из_.append((ix["programId"], qr["account"], wr["account"], qr["mint"]))
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--lider", required=True, help="адрес лидера")
    р.add_argument("--nash", required=True, help="наш адрес (плательщик в сборке)")
    р.add_argument("--predel", type=int, default=60, help="сколько подписей лидера смотреть")
    р.add_argument("--lamporty", type=int, default=10_000_000)
    р.add_argument("--out", default="")
    а = р.parse_args()

    import bloom_lane_two_step as TS
    import c2_common as C
    import c2_pool_programs as PP
    import c2_shadow_build as SB
    import c2_swap_build as B

    итог = {"лидер": а.lider, "наш": а.nash, "снято_utc": time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "лампорты": а.lamporty, "ряды": []}
    подписи = сделки(а.lider, а.predel)
    итог["подписей_лидера"] = len(подписи)
    # РЕЕСТР ПУЛОВ НОГ -- тот же, что читает служба (data/c2_leg_pools_*.json).
    try:
        реестр_ног = SB.load_leg_pools()
    except Exception as exc:  # noqa: BLE001
        реестр_ног = {}
        итог["реестр_ног_почему"] = type(exc).__name__
    итог["реестр_ног"] = sorted(реестр_ног)
    ноги_программы = {B.PUMP_AMM, B.CPMM, B.DAMM2, B.LAUNCHLAB, B.DLMM, B.CLMM}
    совпало = разошлось = не_собрано = 0
    for подпись in подписи:
        tx = транзакция(подпись)
        if not tx or (tx.get("meta") or {}).get("err"):
            continue
        сиг = None
        try:
            import bloom_detector as BD
            сиг = BD.сигнал_из_транзакции(tx, а.lider, подпись=подпись,
                                           слот=tx.get("slot"))
        except Exception:  # noqa: BLE001
            сиг = None
        if (сиг or {}).get("kind") != "buy" or not сиг.get("mint"):
            continue
        пул = C.identify_pool(tx, а.lider, сиг["mint"])
        if not пул.get("ok"):
            continue
        q = пул.get("quote_mint")
        if q in (C.WSOL, getattr(C, "NATIVE_QUOTE", "native_sol")):
            continue          # это одношаговая покупка, не наш случай
        ряд = {"подпись": подпись, "слот": tx.get("slot"), "минт": сиг["mint"],
                "котировка": q, "трата_источника": сиг.get("spend_ui")}
        прог2 = PP.pool_program(tx, пул["pool_vault"], SB._labels())["pool_program"]
        ряд["программа_пула"] = прог2
        # ШАБЛОН ПЕРВОГО ШАГА: сначала ГОТОВЫЙ реестр пулов ног (его же читает
        # служба), потом сама сделка лидера, потом свежие сделки котировочного
        # минта -- как c2_twohop.
        кандидаты = []
        готовый = (реестр_ног or {}).get(q)
        if готовый and готовый.get("q_vault") and готовый.get("w_vault"):
            кандидаты = [(готовый["program"], готовый["q_vault"],
                          готовый["w_vault"], q)]
            ряд["шаг_1_из"] = "реестр ног"
        if not кандидаты:
            кандидаты = [к for к in пулы_sol_q(tx, C, B, ноги_программы) if к[3] == q]
            if кандидаты:
                ряд["шаг_1_из"] = "сделка лидера"
        if not кандидаты:
            ряд["шаг_1_из"] = "свежие сделки котировочного минта"
            for п2 in сделки(q, 12):
                tx2 = транзакция(п2)
                if not tx2:
                    continue
                кандидаты = [к for к in пулы_sol_q(tx2, C, B, ноги_программы)
                             if к[3] == q]
                if кандидаты:
                    break
        if not кандидаты:
            ряд["почему"] = "пула SOL <-> котировочный не нашли"
            не_собрано += 1
            итог["ряды"].append(ряд)
            continue
        прог1, qv, wv, _ = кандидаты[0]
        ряд["программа_шага_1"] = прог1
        кэш = SB.LegCache({q: {"program": прог1, "q_vault": qv, "w_vault": wv}},
                          lambda м, п: rpc(м, п), allow_polling=True)
        try:
            состояние_кэша = кэш.refresh_all().get(q)
        except Exception as exc:  # noqa: BLE001
            состояние_кэша = f"{type(exc).__name__}"
        ряд["кэш_шага_1"] = состояние_кэша
        наша = TS.собрать(tx_источника=tx, источник=а.lider, минт=сиг["mint"],
                          наш_кошелёк=а.nash, лампорты=а.lamporty,
                          проскальзывание=0.35, cu_units=800_000,
                          приоритет_лампорты=1_000_000, чаевые_лампорты=0,
                          кэш_ног=кэш, rpc_call=lambda м, п: rpc(м, п),
                          налог_минта=налог_минта)
        ряд["наша_сборка"] = {к: наша.get(к) for к in
                               ("ok", "why_not", "quote_fee_bps", "leg1_min_out",
                                "leg2_amount_in", "leg2_to_pool", "min_out",
                                "expected_out", "size", "build_ms")}
        if not наша.get("ok"):
            не_собрано += 1
            итог["ряды"].append(ряд)
            continue
        # СРАВНЕНИЕ РАСКЛАДКИ: инструкция второго шага у нас и у лидера.
        tpl2 = B.extract_template(tx, прог2, пул["pool_vault"])
        его = next((ix for ix in B.all_instructions(tx)
                    if ix.get("programId") == прог2
                    and len(ix["accounts"]) == len(tpl2["accounts"])), None)
        наша_ix = B.swap_instruction(tpl2, tx, а.nash, наша["leg2_amount_in"],
                                      наша["min_out"])
        наши_счета = [str(m.pubkey) for m in наша_ix.accounts]
        ряд["раскладка"] = {
            "программа_у_нас": str(наша_ix.program_id),
            "программа_у_лидера": (его or {}).get("programId"),
            "счетов_у_нас": len(наши_счета),
            "счетов_у_лидера": len((его or {}).get("accounts") or []),
        }
        расхождения = []
        if его:
            for и, (наш_с, его_с) in enumerate(zip(наши_счета, его["accounts"])):
                if наш_с != его_с:
                    расхождения.append({"номер": и, "у_нас": наш_с, "у_лидера": его_с})
        ряд["раскладка"]["расхождений"] = len(расхождения)
        # РАСХОЖДЕНИЯ ОЖИДАЕМЫ ТОЛЬКО ТАМ, ГДЕ СЧЁТ НАШ: кошелёк и его токен-счета.
        наши_ожидаемые = {а.nash}
        try:
            mv2 = B.mints_and_vaults(tpl2, tx)
            наши_ожидаемые |= {B.ata(а.nash, mv2["base_mint"], mv2["base_program"]),
                               B.ata(а.nash, mv2["quote_mint"], mv2["quote_program"])}
        except Exception:  # noqa: BLE001
            pass
        чужие = [р for р in расхождения if р["у_нас"] not in наши_ожидаемые]
        ряд["раскладка"]["расхождений_не_наших"] = len(чужие)
        ряд["раскладка"]["не_наши"] = чужие[:5]
        ряд["раскладка"]["совпало"] = (
            его is not None
            and str(наша_ix.program_id) == его["programId"]
            and len(наши_счета) == len(его["accounts"])
            and not чужие)
        if ряд["раскладка"]["совпало"]:
            совпало += 1
        else:
            разошлось += 1
        итог["ряды"].append(ряд)

    итог["свод"] = {"совпало": совпало, "разошлось": разошлось,
                     "не_собрано": не_собрано, "рядов": len(итог["ряды"])}
    print(json.dumps(итог["свод"], ensure_ascii=False, indent=1))
    for р in итог["ряды"][:10]:
        print(json.dumps({к: р.get(к) for к in
                           ("подпись", "котировка", "программа_пула",
                            "программа_шага_1", "раскладка", "почему")},
                          ensure_ascii=False)[:420])
    if а.out:
        Path(а.out).write_text(json.dumps(итог, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
