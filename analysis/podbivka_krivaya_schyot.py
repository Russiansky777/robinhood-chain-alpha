#!/usr/bin/env python3
"""Счёт кривой pump.fun по цепи: чем «нулевая порода» отличается от обычных монет.

Зачем. На широкой выборке (4 022 минта, 30 000 сделок) модель круга совпадает с архивом
ровно у 86.1 % сделок и расходится у 13.9 %. По цепи расходящиеся опознаются однозначно:
в событии программы у них тариф протокола и тариф создателя РАВНЫ НУЛЮ (120 из 120), тогда
как у контрольных -- 95 и 30 б.п. (20 из 20). Отношение постоянного произведения у них
0.850406 (p10 0.741) -- значит это кривая с другим правилом цены, а не ошибка чтения:
резервы и суммы архива сходятся с цепью ровно (1.0) у обеих групп.

Гипотеза владельца: это монеты Mayhem. Проверяется прямо -- в счёте кривой (PDA
"bonding-curve" + mint программы 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P) по IDL есть
поле is_mayhem_mode, а рядом is_cashback_coin, is_holder_reward, depth, creator_fee_bps и
quote_mint. Счёт читается у ВСЕХ минтов обеих групп и раскладывается по этим полям; если
расходящиеся -- это Mayhem, флаг будет стоять у них и не стоять у контрольных.

Раскладка счёта (после 8 байт дискриминатора [23,183,248,55,96,216,172,96]), из
data/idl/pump.json: virtual_token/virtual_quote/real_token/real_quote/token_total_supply
по u64, complete bool, creator pubkey, is_mayhem_mode bool, is_cashback_coin bool,
quote_mint pubkey, creator_fee_bps u64, can_edit_creator_fee bool, is_holder_reward bool,
creator_fee u64, protocol_fees u64, depth u8, initial_virtual_quote_reserves u64,
post_complete_base_out u64, post_complete_quote_in u64.

Только чтение. Helius, темп -- PODB_HELIUS_RPS (потолок владельца 6 запросов в секунду на
все мои процессы вместе). Выход: data/podbivka/krivaya_schyot.json.
"""
from __future__ import annotations

import argparse
import base64
import collections
import json
import os
import statistics
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
RPS = float(os.environ.get("PODB_HELIUS_RPS") or 6.0)
ПРОГРАММА = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
ДИСКР = bytes([23, 183, 248, 55, 96, 216, 172, 96])
WSOL = "So11111111111111111111111111111111111111112"
ПАЧКА = 100          # getMultipleAccounts берёт до 100 счетов за запрос


class Темп:
    """Не больше RPS запросов в секунду -- потолок владельца на все мои процессы."""

    def __init__(self, rps: float) -> None:
        self.rps = max(0.5, rps)
        self._окно: list = []

    def ждать(self, запросов: int = 1) -> None:
        now = time.time()
        self._окно = [(t, n) for t, n in self._окно if now - t < 1.0]
        while sum(n for _, n in self._окно) + запросов > self.rps and self._окно:
            time.sleep(max(0.02, 1.0 - (now - self._окно[0][0])))
            now = time.time()
            self._окно = [(t, n) for t, n in self._окно if now - t < 1.0]
        self._окно.append((time.time(), запросов))


def адрес_кривой(минт: str) -> str:
    from solders.pubkey import Pubkey  # noqa: PLC0415
    пда, _ = Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(минт))],
                                         Pubkey.from_string(ПРОГРАММА))
    return str(пда)


def разобрать(сырое: bytes) -> dict | None:
    """Счёт BondingCurve по раскладке IDL. None, если дискриминатор не тот."""
    if len(сырое) < 142 or сырое[:8] != ДИСКР:
        return None
    from solders.pubkey import Pubkey  # noqa: PLC0415
    о = 8
    вирт_ток, вирт_кв, реал_ток, реал_кв, всего = struct.unpack_from("<5Q", сырое, о)
    о += 40
    завершена = сырое[о]; о += 1
    создатель = str(Pubkey.from_bytes(сырое[о:о + 32])); о += 32
    mayhem = сырое[о]; о += 1
    кэшбэк = сырое[о]; о += 1
    квотный = str(Pubkey.from_bytes(сырое[о:о + 32])); о += 32
    тариф_создателя_бп, = struct.unpack_from("<Q", сырое, о); о += 8
    правка_тарифа = сырое[о]; о += 1
    награда = сырое[о]; о += 1
    тариф_создателя, = struct.unpack_from("<Q", сырое, о); о += 8
    тарифы_протокола, = struct.unpack_from("<Q", сырое, о); о += 8
    глубина = сырое[о]; о += 1
    нач_вирт_кв = струк(сырое, о); о += 8
    базы_после = струк(сырое, о); о += 8
    квоты_после = струк(сырое, о); о += 8
    return {"вирт_токен": вирт_ток, "вирт_квота": вирт_кв,
            "реал_токен": реал_ток, "реал_квота": реал_кв, "всего": всего,
            "завершена": bool(завершена), "создатель": создатель,
            "is_mayhem_mode": bool(mayhem), "is_cashback_coin": bool(кэшбэк),
            "quote_mint": квотный, "квота_sol": квотный == WSOL,
            "creator_fee_bps": тариф_создателя_бп,
            "can_edit_creator_fee": bool(правка_тарифа),
            "is_holder_reward": bool(награда),
            "creator_fee": тариф_создателя, "protocol_fees": тарифы_протокола,
            "depth": глубина, "initial_virtual_quote_reserves": нач_вирт_кв,
            "post_complete_base_out": базы_после, "post_complete_quote_in": квоты_после,
            "байт": len(сырое)}


def струк(b: bytes, о: int) -> int | None:
    if len(b) < о + 8:
        return None
    return struct.unpack_from("<Q", b, о)[0]


def прочесть(rpc, темп: Темп, минты: list) -> dict:
    """Счёт кривой по каждому минту: пачками по 100, getMultipleAccounts."""
    из_: dict = {}
    адреса = {м: адрес_кривой(м) for м in минты}
    пары = list(адреса.items())
    for н in range(0, len(пары), ПАЧКА):
        кусок = пары[н:н + ПАЧКА]
        темп.ждать()
        о = rpc.call("getMultipleAccounts",
                     [[а for _, а in кусок], {"encoding": "base64"}])
        знач = ((о or {}).get("value") or [])
        for (м, а), v in zip(кусок, знач):
            if not v:
                из_[м] = {"адрес": а, "ошибка": "счёта нет"}
                continue
            д = v.get("data")
            сырое = base64.b64decode(д[0]) if isinstance(д, list) else b""
            р = разобрать(сырое)
            из_[м] = {"адрес": а, "владелец": v.get("owner"),
                      **(р or {"ошибка": f"не BondingCurve, байт {len(сырое)}"})}
        print(f"  счетов {min(н + ПАЧКА, len(пары))}/{len(пары)}", flush=True)
    return из_


def свод(карта: dict) -> dict:
    """Раскладка группы по флагам счёта."""
    поля = ("is_mayhem_mode", "is_cashback_coin", "is_holder_reward",
            "can_edit_creator_fee", "завершена", "квота_sol")
    т: dict = {"минтов": len(карта),
               "счёт_прочитан": sum(1 for v in карта.values() if "вирт_токен" in v),
               "ошибок": sum(1 for v in карта.values() if "ошибка" in v)}
    for п in поля:
        т[п] = sum(1 for v in карта.values() if v.get(п))
    for п in ("depth", "creator_fee_bps", "quote_mint",
              "initial_virtual_quote_reserves", "байт"):
        c = collections.Counter(v.get(п) for v in карта.values() if п in v)
        т[f"{п}_раскладка"] = {str(k): n for k, n in c.most_common(8)}
    ост = [v["всего"] for v in карта.values() if v.get("всего")]
    if ост:
        т["token_total_supply_медиана"] = statistics.median(ост)
    return т


def main() -> int:  # noqa: PLR0915
    import c2_common as C  # noqa: PLC0415
    import podbivka_run as R  # noqa: PLC0415
    р_ = argparse.ArgumentParser()
    р_.add_argument("--iz", default=str(П / "krivaya_rashod_p1.json"),
                    help="файл сверки сделок кривой с цепью")
    р_.add_argument("--metka", default="")
    а = р_.parse_args()
    ключ = (os.environ.get("HELIUS_API_KEY2") or os.environ.get("HELIUS_API_KEY")
            or os.environ.get("HELIUS_API") or "").strip()
    if not ключ:
        print("нет ключа Helius -- проход только облачный", flush=True)
        return 1
    д = json.loads(Path(а.iz).read_text(encoding="utf-8"))
    группы = {}
    for имя, ключ_г in (("не_сошлись", "не_сошлись"), ("контроль", "сошлись_контроль")):
        ряды = (д.get(ключ_г) or {}).get("ряды") or []
        группы[имя] = sorted({р["минт"] for р in ряды if р.get("минт")})
    if not any(группы.values()):
        print(f"в {а.iz} нет минтов -- сверять нечего", flush=True)
        return 2
    пересечение = set(группы["не_сошлись"]) & set(группы["контроль"])
    print(f"минтов: расходящихся {len(группы['не_сошлись'])}, "
          f"контрольных {len(группы['контроль'])}, в обеих {len(пересечение)}", flush=True)

    rpc = C.C2Rpc(service="c2_krivaya_schyot", key=ключ)
    темп = Темп(RPS)
    карты: dict = {}
    for имя, минты in группы.items():
        print(f"== {имя}: {len(минты)} минтов", flush=True)
        карты[имя] = прочесть(rpc, темп, минты)

    # сделки на минт -- чтобы связать флаг счёта с отношением модель/факт
    отн: dict = collections.defaultdict(list)
    for ключ_г in ("не_сошлись", "сошлись_контроль"):
        for р in (д.get(ключ_г) or {}).get("ряды") or []:
            if р.get("минт") and р.get("отношение") is not None:
                отн[р["минт"]].append(р["отношение"])
    по_флагу: dict = collections.defaultdict(list)
    for имя, карта in карты.items():
        for м, v in карта.items():
            if "is_mayhem_mode" not in v:
                continue
            к = (имя, "mayhem" if v["is_mayhem_mode"] else "не_mayhem")
            по_флагу[к].extend(отн.get(м) or [])
    отн_свод = {f"{и}/{ф}": {"сделок": len(v),
                             "медиана_отношения": round(statistics.median(v), 6)}
                for (и, ф), v in sorted(по_флагу.items()) if v}

    тело = {"что": "счёт кривой pump.fun по цепи: чем «нулевая порода» отличается",
            "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "источник": а.iz, "параметры": vars(а),
            "минтов_в_обеих_группах": sorted(пересечение),
            "свод": {имя: свод(карта) for имя, карта in карты.items()},
            "отношение_по_флагу": отн_свод,
            "счета": карты,
            "запросов": dict(getattr(rpc, "calls_by_method", {}) or {})}
    ф = П / f"krivaya_schyot{('_' + а.metka) if а.metka else ''}.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(ф)
    print(json.dumps({"свод": тело["свод"], "отношение_по_флагу": отн_свод},
                     ensure_ascii=False, indent=1), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
