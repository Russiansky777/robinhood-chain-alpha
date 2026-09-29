#!/usr/bin/env python3
"""Сбор для Raydium CLMM (программа CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK): только чтение, Helius.

1. Разбор живых сделок источников в CLMM за окно (--s … сейчас): подписи кошельков источников (группы Code-1 и
   кандидаты, data/podbivka/istochniki_code1_kandidaty.json), транзакции с инструкцией программы CLMM (внешней или
   внутренней, через агрегатор). Для каждой -- вид (swap / swap_v2 / swap_router_base_in по дискриминатору Anchor),
   аргументы, счета инструкции по ролям (порядок структур SwapSingle / SwapSingleV2 из исходников
   raydium-io/raydium-clmm), остаток счетов (tick arrays, расширение битовой карты), событие SwapEvent из логов
   (длина события -- признак версии программы: у новой есть trade_fee_0/1).
2. Версия программы в сети: слот последнего развёртывания (programData) -- для сверки с историей исходников.
3. Снимки для офлайн-сверки котировки: пул по кругу (пулы из п.1 по частоте) -- читаем одним getMultipleAccounts
   пул, amm_config, расширение битовой карты и tick arrays ±--massivov от текущего (слот чтения S, commitment
   confirmed), затем первая успешная транзакция пула со слотом > S. Сохраняются сырые данные счетов (base64) и
   транзакция целиком: котировка портом считается и сверяется офлайн, без сети.
Выход: data/podbivka/clmm_sbor.json.
"""
from __future__ import annotations

import argparse
import base64
import calendar
import hashlib
import json
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
ПРОГРАММА = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
TICK_ARRAY_SIZE = 60


def _disc(имя: str, пр: str = "global") -> bytes:
    return hashlib.sha256(f"{пр}:{имя}".encode()).digest()[:8]


ВИДЫ = {_disc("swap"): "swap", _disc("swap_v2"): "swap_v2", _disc("swap_router_base_in"): "swap_router_base_in"}
СОБЫТИЕ_SWAP = _disc("SwapEvent", "event")
# роли счетов -- порядок полей структур Accounts в исходниках (instructions/swap.rs, swap_v2.rs)
РОЛИ = {"swap": ["payer", "amm_config", "pool_state", "input_token_account", "output_token_account", "input_vault",
                 "output_vault", "observation_state", "token_program", "tick_array"],
        "swap_v2": ["payer", "amm_config", "pool_state", "input_token_account", "output_token_account", "input_vault",
                    "output_vault", "observation_state", "token_program", "token_program_2022", "memo_program",
                    "input_vault_mint", "output_vault_mint"]}
_АЛФ = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58d(s_: str) -> bytes:
    n = 0
    for ch in s_:
        n = n * 58 + _АЛФ.index(ch)
    сырые = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s_) - len(s_.lstrip("1"))) + сырые


def _b58e(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s_ = ""
    while n:
        n, r = divmod(n, 58)
        s_ = _АЛФ[r] + s_
    return "1" * (len(b) - len(b.lstrip(b"\x00"))) + s_


def инструкции(tx: dict) -> list:
    """(место, инструкция): место -- «внешняя k» или «внутренняя k.j»."""
    msg = ((tx.get("transaction") or {}).get("message") or {})
    из_ = [(f"внешняя {k}", ix) for k, ix in enumerate(msg.get("instructions") or [])]
    for гр in ((tx.get("meta") or {}).get("innerInstructions") or []):
        из_ += [(f"внутренняя {гр.get('index')}.{j}", ix) for j, ix in enumerate(гр.get("instructions") or [])]
    return из_


def разобрать_ix(ix: dict) -> dict | None:
    if not isinstance(ix, dict) or ix.get("programId") != ПРОГРАММА or not ix.get("data"):
        return None
    try:
        d = _b58d(ix["data"])
    except ValueError:
        return None
    вид = ВИДЫ.get(d[:8])
    if not вид:
        return {"вид": "другая", "disc": d[:8].hex()}
    сч = list(ix.get("accounts") or [])
    из_ = {"вид": вид, "счетов": len(сч)}
    if вид in ("swap", "swap_v2") and len(d) >= 8 + 8 + 8 + 16 + 1:
        amount, other, lim_lo, lim_hi = struct.unpack_from("<QQQQ", d, 8)
        из_.update(amount=amount, other_amount_threshold=other, sqrt_price_limit_x64=lim_lo | (lim_hi << 64),
                   is_base_input=d[40] != 0)
        роли = РОЛИ[вид]
        из_["роли"] = {р: сч[i] for i, р in enumerate(роли) if i < len(сч)}
        из_["остаток"] = сч[len(роли):]
    elif вид == "swap_router_base_in" and len(d) >= 24:
        из_["amount_in"], из_["amount_out_minimum"] = struct.unpack_from("<QQ", d, 8)
        из_["счета"] = сч
    return из_


def события(tx: dict) -> list:
    """SwapEvent из логов «Program data:» (emit!)."""
    из_ = []
    for стр in ((tx.get("meta") or {}).get("logMessages") or []):
        if not стр.startswith("Program data: "):
            continue
        try:
            b = base64.b64decode(стр[len("Program data: "):])
        except ValueError:
            continue
        if b[:8] != СОБЫТИЕ_SWAP:
            continue
        d = b[8:]
        if len(d) < 32 * 4 + 8 * 4 + 1 + 16 + 16 + 4:
            continue
        e = {"длина": len(d), "pool_state": _b58e(d[0:32]), "sender": _b58e(d[32:64])}
        o = 128
        e["amount_0"], e["transfer_fee_0"], e["amount_1"], e["transfer_fee_1"] = struct.unpack_from("<QQQQ", d, o)
        o += 32
        e["zero_for_one"] = d[o] != 0
        o += 1
        lo, hi = struct.unpack_from("<QQ", d, o)
        e["sqrt_price_x64"] = lo | (hi << 64)
        o += 16
        lo, hi = struct.unpack_from("<QQ", d, o)
        e["liquidity"] = lo | (hi << 64)
        o += 16
        e["tick"] = struct.unpack_from("<i", d, o)[0]
        o += 4
        if len(d) >= o + 16:
            e["trade_fee_0"], e["trade_fee_1"] = struct.unpack_from("<QQ", d, o)
        из_.append(e)
    return из_


def пул_поля(data: bytes) -> dict:
    """Минимум для выбора tick arrays; полный разбор -- в модуле котировки."""
    return {"amm_config": _b58e(data[9:41]), "tick_spacing": struct.unpack_from("<H", data, 235)[0],
            "tick_current": struct.unpack_from("<i", data, 269)[0]}


def старт_массива(tick: int, spacing: int) -> int:
    n = TICK_ARRAY_SIZE * spacing
    return (tick // n) * n                     # floor, как get_array_start_index


def адрес_массива(пул: str, старт: int) -> str:
    from solders.pubkey import Pubkey  # noqa: PLC0415
    pda, _ = Pubkey.find_program_address([b"tick_array", bytes(Pubkey.from_string(пул)), struct.pack(">i", старт)],
                                         Pubkey.from_string(ПРОГРАММА))
    return str(pda)


def адрес_расширения(пул: str) -> str:
    from solders.pubkey import Pubkey  # noqa: PLC0415
    pda, _ = Pubkey.find_program_address([b"pool_tick_array_bitmap_extension", bytes(Pubkey.from_string(пул))],
                                         Pubkey.from_string(ПРОГРАММА))
    return str(pda)


def снимок(уз, пул: str, массивов: int) -> dict:
    r = уз.вызов("getAccountInfo", [пул, {"encoding": "base64", "commitment": "confirmed"}])
    v = (r or {}).get("value")
    if not v:
        raise RuntimeError("пул не читается")
    п = пул_поля(base64.b64decode(v["data"][0]))
    ст0 = старт_массива(п["tick_current"], п["tick_spacing"])
    шаг = TICK_ARRAY_SIZE * п["tick_spacing"]
    старты = [ст0 + k * шаг for k in range(-массивов, массивов + 1)]
    адр = [пул, п["amm_config"], адрес_расширения(пул)] + [адрес_массива(пул, s) for s in старты]
    r2 = уз.вызов("getMultipleAccounts", [адр, {"encoding": "base64", "commitment": "confirmed"}])
    val = (r2 or {}).get("value") or []
    счета = {a: (x["data"][0] if x else None) for a, x in zip(адр, val)}
    return {"slot": ((r2 or {}).get("context") or {}).get("slot"), "пул": пул, "amm_config": п["amm_config"],
            "расширение": адр[2], "массивы": dict(zip(map(str, старты), адр[3:])), "данные": счета}


def снимок_по_карте(уз, пул: str, сколько: int = 3) -> dict:
    """Массивы -- по битовой карте пула (и расширению), как их выбирает программа: сколько по каждому направлению."""
    import podbivka_clmm_quote as Q  # noqa: PLC0415
    ext_адр = адрес_расширения(пул)
    r = уз.вызов("getMultipleAccounts", [[пул, ext_адр], {"encoding": "base64", "commitment": "confirmed"}])
    val = (r or {}).get("value") or []
    if not val or not val[0]:
        raise RuntimeError("пул не читается")
    pool = Q.разобрать_пул(base64.b64decode(val[0]["data"][0]))
    ext = Q.разобрать_расширение(base64.b64decode(val[1]["data"][0])) if len(val) > 1 and val[1] else None
    старты = []
    for zfo in (True, False):
        try:
            старты += Q.нужные_массивы(pool, ext, zfo, сколько)
        except Q.ОшибкаCLMM:
            pass
    старты = sorted(set(старты))
    адр = [пул, pool["amm_config"], ext_адр] + [адрес_массива(пул, s) for s in старты]
    r2 = уз.вызов("getMultipleAccounts", [адр, {"encoding": "base64", "commitment": "confirmed"}])
    val2 = (r2 or {}).get("value") or []
    return {"slot": ((r2 or {}).get("context") or {}).get("slot"), "пул": пул, "amm_config": pool["amm_config"],
            "расширение": ext_адр, "массивы": dict(zip(map(str, старты), адр[3:])),
            "данные": {a: (x["data"][0] if x else None) for a, x in zip(адр, val2)}}


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--s", default="2026-09-28T06:00:00Z", help="начало окна разбора сделок источников, UTC")
    р.add_argument("--podpisey", type=int, default=1500, help="предел подписей на кошелёк")
    р.add_argument("--cel", type=int, default=40, help="снимков «состояние → следующий своп»")
    р.add_argument("--massivov", type=int, default=4, help="tick arrays в каждую сторону от текущего")
    р.add_argument("--minut", type=float, default=80)
    р.add_argument("--po-karte", action="store_true", help="tick arrays по битовой карте пула, 3 в каждую сторону")
    р.add_argument("--puly-iz", default="", help="json прошлого сбора: пулы оттуда, часть 1 (сделки) не делать")
    р.add_argument("--metka", default="", help="суффикс файла выхода")
    а = р.parse_args()
    t0 = calendar.timegm(time.strptime(а.s, "%Y-%m-%dT%H:%M:%SZ"))
    сп = json.loads((КОРЕНЬ / "data" / "podbivka" / "istochniki_code1_kandidaty.json").read_text(encoding="utf-8"))
    кошельки = sorted(set(сп["группы_code1"]) | set(сп["кандидаты"]) | set(сп.get("снайперы") or []))
    уз = S.Узел()
    out = КОРЕНЬ / "data" / "podbivka" / f"clmm_sbor{('_' + а.metka) if а.metka else ''}.json"
    import podbivka_run as R  # noqa: PLC0415
    итог: dict = {"окно_с": а.s, "кошельков": len(кошельки), "покрытие": {}, "сделки": [], "версия": {},
                  "снимки": [], "отсев": {}}

    def записать():
        итог["расход"] = уз.расход()
        out.write_text(json.dumps(итог, ensure_ascii=False), encoding="utf-8")
        R.записано(out)
    with уз.на("helius"):
        # 2. версия программы
        try:
            pr = уз.вызов("getAccountInfo", [ПРОГРАММА, {"encoding": "jsonParsed"}])
            pd = ((((pr or {}).get("value") or {}).get("data") or {}).get("parsed") or {}).get("info", {}).get("programData")
            pdi = уз.вызов("getAccountInfo", [pd, {"encoding": "jsonParsed", "dataSlice": {"offset": 0, "length": 0}}]) if pd else None
            info = ((((pdi or {}).get("value") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            итог["версия"] = {"programData": pd, "слот_развёртывания": info.get("slot"), "authority": info.get("authority")}
            if info.get("slot"):
                бт = уз.вызов("getBlockTime", [info["slot"]])
                итог["версия"]["время_развёртывания_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(бт)) if бт else None
        except RuntimeError as exc:
            итог["версия"] = {"why_not": S.чисто(str(exc))[:160]}
        print("версия:", итог["версия"], flush=True)
        # 1. сделки источников
        пулы: dict = {}
        if а.puly_iz:
            пулы = json.loads((КОРЕНЬ / а.puly_iz).read_text(encoding="utf-8")).get("пулы_источников") or {}
            кошельки_1 = []
        else:
            кошельки_1 = кошельки
        for w in кошельки_1:
            сп_w, до = [], None
            while len(сп_w) < а.podpisey:
                try:
                    стр = уз.подписи(w, до=до, limit=1000)
                except RuntimeError:
                    break
                if not стр:
                    break
                сп_w += [з for з in стр if (з.get("blockTime") or 0) >= t0 and з.get("err") is None]
                if (стр[-1].get("blockTime") or 0) < t0 or len(стр) < 1000:
                    break
                до = стр[-1]["signature"]
            итог["покрытие"][w] = {"подписей": len(сп_w), "упёрлись_в_предел": len(сп_w) >= а.podpisey}
            for и in range(0, len(сп_w), 100):
                txs = уз.пакет([з["signature"] for з in сп_w[и:и + 100]])
                for s_, т in txs.items():
                    if not т:
                        continue
                    ixs = [(м, разобрать_ix(ix), ix) for м, ix in инструкции(т)]
                    ixs = [(м, x, ix) for м, x, ix in ixs if x]
                    if not ixs:
                        continue
                    ев = события(т)
                    итог["сделки"].append({"кошелёк": w, "signature": s_, "slot": т.get("slot"), "blockTime": т.get("blockTime"),
                                           "версия_tx": т.get("version"),
                                           "alt": bool(((т.get("transaction") or {}).get("message") or {}).get("addressTableLookups")),
                                           "инструкции": [{"место": м, **x} for м, x, _ in ixs], "события": ев})
                    for _, x, _ in ixs:
                        p = (x.get("роли") or {}).get("pool_state")
                        if p:
                            пулы[p] = пулы.get(p, 0) + 1
                    for e in ев:
                        пулы[e["pool_state"]] = пулы.get(e["pool_state"], 0) + 0
            print(w[:8], "подписей", len(сп_w), "сделок CLMM всего", len(итог["сделки"]), flush=True)
        итог["пулы_источников"] = пулы
        записать()
        # 3. снимки
        очередь = sorted(пулы, key=lambda p: -пулы[p])
        конец = time.time() + а.minut * 60
        k = 0
        while очередь and time.time() < конец and len(итог["снимки"]) < а.cel:
            пул = очередь[k % len(очередь)]
            k += 1
            try:
                сн = снимок_по_карте(уз, пул) if а.po_karte else снимок(уз, пул, а.massivov)
            except RuntimeError as exc:
                итог["отсев"][f"снимок: {S.чисто(str(exc))[:60]}"] = итог["отсев"].get(f"снимок: {S.чисто(str(exc))[:60]}", 0) + 1
                очередь.remove(пул)
                continue
            след = None
            for _ in range(20):
                time.sleep(3)
                зз = [з for з in уз.подписи(пул, limit=50) if (з.get("slot") or 0) > (сн["slot"] or 0) and з.get("err") is None]
                if зз:
                    след = min(зз, key=lambda з: (з["slot"],))
                    break
            if not след:
                итог["отсев"]["нет успешной транзакции за 60 с"] = итог["отсев"].get("нет успешной транзакции за 60 с", 0) + 1
                continue
            т = уз.tx(след["signature"])
            if not т:
                continue
            # в том же слоте раньше могла быть ещё успешная транзакция пула -- берём только если она одна в слоте
            в_слоте = [з for з in уз.подписи(пул, limit=50) if з.get("slot") == след["slot"] and з.get("err") is None]
            сн.update(сделка=след["signature"], slot_сделки=т.get("slot"), транзакция=т,
                      успешных_в_слоте_сделки=len(в_слоте))
            итог["снимки"].append(сн)
            записать()
            print("снимок", пул[:8], len(итог["снимки"]), flush=True)
    записать()
    R.пуш("Podbivka-2: CLMM sbor -- sdelki istochnikov, versiya, snimki [automated]", [str(out)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
