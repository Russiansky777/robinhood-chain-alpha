#!/usr/bin/env python3
"""Подбивка: симулятор копии во времени и отбор первых покупок кошелька.

ЧТО ДОБАВЛЕНО К podbivka.py (его код не переписан, здесь только вызывается):
  * вход S+0 / S+1 / S+2 и выход s0+H (H из ГОРИЗОНТЫ) -- по резервам пула после
    последней успешной транзакции пула в нужном слоте;
  * толпа -- успешные транзакции пула после источника в слотах s0..s0+2;
  * «без свопов до горизонта» -- выход по текущим резервам, доля отдельно;
  * «источник продал в окне 150 слотов» -- по его же истории;
  * первые покупки кошелька: пред-баланс минта 0, кошелёк подписант, трата
    2-5 и 5+ SOL (стейблы -- в SOL-экв по курсу из цепи);
  * налог Token-2022 из минта, возраст токена, ликвидность на входе, тип пула.

МОДЕЛЬ СДЕЛКИ (0.5 SOL). Покупка -- формулой пула на резервах точки входа с
комиссией, калиброванной на сделке источника (x*y=k), или формулой кривой
pump.fun с её комиссиями. Выход -- та же формула на резервах точки выхода, В
КОТОРЫЕ ВСТАВЛЕНА НАША ПОКУПКА (резерв котировки + наша трата, резерв токена
- наши токены): иначе круг «купил-продал» в пустом пуле платил бы проскальзывание
дважды. Сосредоточенная ликвидность и запасной путь -- цена исполнения
последнего свопа без проскальзывания, с флагом. Минус 0.002 SOL на сделку,
минус налог Token-2022 на получении и на продаже.

ЧЕСТНОСТЬ. Отказ -- всегда с причиной в поле why_not и в сводной доле.
Строки ошибок -- только через podbivka_1b.чисто (URL и ключи вырезаются).
Только чтение цепи.
"""
from __future__ import annotations

import base64
import json
import struct
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import c2_swap_build as SB  # noqa: E402
import podbivka as P  # noqa: E402
import podbivka_1b as B1  # noqa: E402

ЛАМПОРТОВ = 1_000_000_000
РАЗМЕР_ЛАМ = 500_000_000          # наша сделка 0.5 SOL
ИЗДЕРЖКИ_SOL = 0.002              # на сделку, сверх формулы пула
ВХОДЫ = (0, 1, 2)
ГОРИЗОНТЫ = (12, 25, 50, 72, 112, 150)
ОКНО_ПРОДАЖИ = 150
ПОРОГИ = (("2-5", 2.0, 5.0), ("5+", 5.0, None))
МИН_ПОРОГ_SOL = 2.0
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
КОТИРОВКИ = {C.WSOL, C.USDC, C.USDT}
ПРЕДЕЛ_СТРАНИЦ_ПУЛА = 40          # 40 000 подписей хранилища на одну покупку
ПРЕДЕЛ_СТРАНИЦ_МИНТА = 3          # возраст токена: не дальше 3 000 подписей назад
ШАГОВ_НАЗАД = 4                   # нечитаемая точка -- до 4 транзакций назад


def чисто(т) -> str:
    return B1.чисто(т)


def порог_покупки(sol: float | None) -> str | None:
    if sol is None:
        return None
    for имя, lo, hi in ПОРОГИ:
        if sol >= lo and (hi is None or sol < hi):
            return имя
    return None


# ================================================================ узел

class Узел(B1.Пакетный):
    """Пакетный узел подбивки с отступом на 429 И 5xx И сбой сети.

    Кэш транзакций НЕ растёт сам: история кошелька в тысячи транзакций не
    должна жить в памяти -- пакет() отдаёт и забывает, кэш -- только явный.
    """

    ПОПЫТОК = 7

    def __init__(self) -> None:
        super().__init__()
        self.отказов_5xx = 0
        self.сбоев_сети = 0

    def _пост(self, тело, *, срок: float = 60.0):
        import requests  # noqa: PLC0415
        пауза = 1.0
        for попытка in range(self.ПОПЫТОК):
            self.обращений += 1
            try:
                от = requests.post(P.узел(), json=тело, timeout=срок)
            except requests.RequestException as exc:
                self.сбоев_сети += 1
                if попытка == self.ПОПЫТОК - 1:
                    raise RuntimeError(чисто(f"сеть: {type(exc).__name__}")) from None
                time.sleep(пауза)
                пауза = min(пауза * 2, 30.0)
                continue
            if от.status_code == 429 or от.status_code >= 500:
                if от.status_code == 429:
                    self.отказов_429 += 1
                else:
                    self.отказов_5xx += 1
                time.sleep(пауза)
                пауза = min(пауза * 2, 30.0)
                continue
            if от.status_code != 200:
                raise RuntimeError(f"узел: http {от.status_code}")
            return от.json()
        raise RuntimeError("узел: 429/5xx на всех попытках")

    def вызов(self, метод: str, парам: list, *, срок: float = 25.0):
        пауза = 1.0
        for попытка in range(self.ПОПЫТОК):
            self.вызовов += 1
            тело = self._пост({"jsonrpc": "2.0", "id": 1, "method": метод, "params": парам},
                              срок=срок)
            if isinstance(тело, dict) and "error" in тело:
                ош = str(тело["error"])
                if ("429" in ош or "rate" in ош.lower() or "Too Many" in ош) and \
                        попытка < self.ПОПЫТОК - 1:
                    self.отказов_429 += 1
                    time.sleep(пауза)
                    пауза = min(пауза * 2, 30.0)
                    continue
                self.ошибок += 1
                raise RuntimeError(чисто(f"узел: {ош[:160]}"))
            return тело.get("result") if isinstance(тело, dict) else None
        raise RuntimeError("узел: предел частоты на всех попытках")

    def пакет(self, подписи: list) -> dict:
        """{подпись: tx или None} без записи в кэш. Пропавшие -- по одной."""
        из_: dict = {}
        for и in range(0, len(подписи), self.РАЗМЕР_ПАКЕТА):
            кусок = подписи[и:и + self.РАЗМЕР_ПАКЕТА]
            тело = [{"jsonrpc": "2.0", "id": j, "method": "getTransaction",
                     "params": [п, P.ОПЦИИ_TX]} for j, п in enumerate(кусок)]
            self.запросов += len(кусок)
            self.вызовов += len(кусок)
            try:
                ответ = self._пост(тело)
            except RuntimeError:
                ответ = []
            по_id = {о.get("id"): о for о in (ответ if isinstance(ответ, list) else [ответ])
                     if isinstance(о, dict)}
            for j, п in enumerate(кусок):
                о = по_id.get(j) or {}
                if о and "error" not in о and о.get("result"):
                    из_[п] = о["result"]
                    continue
                try:
                    из_[п] = self.вызов("getTransaction", [п, P.ОПЦИИ_TX], срок=40.0)
                except RuntimeError:
                    из_[п] = None
            time.sleep(self.ПАУЗА_МЕЖДУ_ПАКЕТАМИ_С)
        return из_

    def tx(self, подпись: str):
        """Одна транзакция с кэшем (исходные и точки состояния пула)."""
        if подпись in self._кэш:
            return self._кэш[подпись]
        т = self.вызов("getTransaction", [подпись, P.ОПЦИИ_TX], срок=40.0)
        self._кэш[подпись] = т
        return т

    def подписи(self, адрес: str, *, до: str | None = None, по: str | None = None,
                limit: int = 1000) -> list:
        парам: dict = {"limit": limit}
        if до:
            парам["before"] = до
        if по:
            парам["until"] = по
        return self.вызов("getSignaturesForAddress", [адрес, парам]) or []

    def расход(self) -> dict:
        return {"вызовов": self.вызовов, "обращений": self.обращений,
                "запросов_в_пакетах": self.запросов, "ошибок": self.ошибок,
                "отказов_429": self.отказов_429, "отказов_5xx": self.отказов_5xx,
                "сбоев_сети": self.сбоев_сети}


class КурсУзла:
    """Мост для c2_common.RateBook: его .signatures/.get_tx поверх нашего узла."""

    def __init__(self, уз: Узел) -> None:
        self.уз = уз

    def signatures(self, адрес, before=None, limit=25):
        return self.уз.подписи(адрес, до=before, limit=limit)

    def get_tx(self, подпись):
        return self.уз.tx(подпись)


# ================================================================ налог минта

_НАЛОГ: dict = {}


def налог_минта(уз: Узел, минт: str) -> dict:
    """TransferFeeConfig из самого минта: {"program", "bps", "max", "why_not"}."""
    if минт in _НАЛОГ:
        return _НАЛОГ[минт]
    из_ = {"program": None, "bps": 0, "max": 0, "why_not": None}
    try:
        инфо = уз.вызов("getAccountInfo", [минт, {"encoding": "jsonParsed"}])
        знач = (инфо or {}).get("value") or {}
        из_["program"] = знач.get("owner")
        разбор = ((знач.get("data") or {}).get("parsed") or {}).get("info") or {}
        for расш in разбор.get("extensions") or []:
            if расш.get("extension") == "transferFeeConfig":
                с = (расш.get("state") or {})
                новый = с.get("newerTransferFee") or {}
                из_["bps"] = int(новый.get("transferFeeBasisPoints") or 0)
                из_["max"] = int(новый.get("maximumFee") or 0)
    except RuntimeError as exc:
        из_["why_not"] = чисто(str(exc))[:160]
    _НАЛОГ[минт] = из_
    return из_


def удержано(сумма: int, налог: dict) -> int:
    """Комиссия перевода Token-2022 с суммы (вверх, не больше maximumFee)."""
    bps = int(налог.get("bps") or 0)
    if bps <= 0 or сумма <= 0:
        return 0
    к = -(-сумма * bps // 10_000)
    мх = int(налог.get("max") or 0)
    return min(к, мх) if мх > 0 else к


# ================================================================ кривая pump.fun: событие любой стороны

def событие_кривой(tx: dict, минт: str) -> dict | None:
    """TradeEvent кривой pump.fun -- покупка ИЛИ продажа, резервы ПОСЛЕ сделки.

    Раскладка та же, что в c2_swap_build.pump_trade_event (там берутся только
    покупки); здесь проверка сторон своя: событие обязано воспроизвести свою
    сделку формулой кривой, иначе оно не берётся (неверная раскладка даёт
    расхождение на порядки).
    """
    from solders.pubkey import Pubkey  # noqa: PLC0415
    последнее = None
    for ln in ((tx or {}).get("meta") or {}).get("logMessages") or []:
        if not ln.startswith("Program data: "):
            continue
        try:
            raw = base64.b64decode(ln[len("Program data: "):].strip())
        except ValueError:
            continue
        if raw[:8] != SB.PF_EVENT_DISC or len(raw) < SB.PF_MIN_EVENT:
            continue
        b = raw[8:]
        try:
            ev_mint = str(Pubkey(bytes(b[0:32])))
            sol, tok = struct.unpack_from("<QQ", b, 32)
            is_buy = b[48]
            vs, vt, rs, rt = struct.unpack_from("<QQQQ", b, 89)
            fee_bps, fee = struct.unpack_from("<QQ", b, 153)
            cr_bps, cr_fee = struct.unpack_from("<QQ", b, 201)
        except (struct.error, ValueError):
            continue
        if ev_mint != минт or sol <= 0 or tok <= 0:
            continue
        if is_buy == 1:
            vs0, vt0 = vs - sol, vt + tok
            пред_tok = vt0 * sol // (vs0 + sol) if vs0 + sol > 0 else -1
            if abs(пред_tok - tok) > max(1, tok // 1_000_000):
                continue
        elif is_buy == 0:
            vs0, vt0 = vs + sol, vt - tok
            пред_sol = vs0 * tok // (vt0 + tok) if vt0 + tok > 0 else -1
            if abs(пред_sol - sol) > max(1, sol // 1_000_000):
                continue
        else:
            continue
        последнее = {"vs": vs, "vt": vt, "rs": rs, "rt": rt, "fee_bps": fee_bps,
                     "cr_bps": cr_bps, "is_buy": bool(is_buy)}
    return последнее


# ================================================================ состояние пула по транзакции

def состояние(tx: dict, режим: str, пул: dict, минт: str) -> dict | None:
    """Резервы/цена пула ПОСЛЕ транзакции tx. None -- эта tx состояния не даёт.

    режим xyk   -- {"x": котировка, "y": токен} из post хранилищ;
    режим curve -- {"vs", "vt", "rs", "rt"} из события кривой;
    режим price -- {"price": котировки за единицу токена} по дельтам хранилищ.
    """
    if not tx or ((tx.get("meta") or {}).get("err") is not None):
        return None
    if режим == "curve":
        ев = событие_кривой(tx, минт)
        return dict(ев) if ев else None
    if режим == "launchlab":
        ев = SB.launchlab_event(tx)
        if not ев:
            return None
        return {"quote": int(ев["virtual_quote"] + ев["real_quote_after"]),
                "base": int(ев["virtual_base"] - ев["real_base_after"]),
                "rq": int(ев["real_quote_after"])}
    ряды = {r["account"]: r for r in C.token_rows(tx).values()}
    бв, кв = ряды.get(пул["pool_vault"]), ряды.get(пул["quote_vault"])
    if режим == "xyk":
        if not бв or not кв:
            return None
        return {"x": int(кв["post"]), "y": int(бв["post"])}
    if not бв or not кв:
        return None
    дб, дк = бв["post"] - бв["pre"], кв["post"] - кв["pre"]
    if дб == 0 or дк == 0 or (дб > 0) == (дк > 0):
        return None
    return {"price": abs(дк) / abs(дб), "q_after": int(кв["post"])}


# ================================================================ формулы нашей сделки

def наша_покупка(режим: str, ст: dict, *, f: float | None, кривая_bps: tuple,
                 размер: int = РАЗМЕР_ЛАМ) -> dict:
    """Сколько токенов даёт пул на трату `размер` лампортов в состоянии ст."""
    if режим == "xyk":
        # Рамка X = x/f: y*a*f/(x+a*f) == y*a/(x/f+a) -- та же формула, что у
        # сделки S+0 в podbivka.py, но резерв котировки «эффективный». В этой
        # рамке наша трата входит в пул целиком, и круг купил-продал в пустом
        # пуле возвращает трату (минус доля продажи g), а не теряет f дважды.
        x, y = ст["x"], ст["y"]
        if x <= 0 or y <= 0 or not f:
            return {"ok": False, "why_not": "нулевой резерв на входе"}
        X = D(x) / D(str(f))
        ток = int(D(y) * размер / (X + размер))
        return {"ok": ток > 0, "tokens": ток, "в_пул": размер,
                "why_not": None if ток > 0 else "ноль токенов"}
    if режим == "launchlab":
        нетто = int(D(размер) * (1 - D(str(f))))
        q, b = ст["quote"], ст["base"]
        if q <= 0 or b <= 0:
            return {"ok": False, "why_not": "launchlab: нулевой резерв"}
        ток = int(D(b) * нетто / (D(q) + нетто))
        return {"ok": ток > 0, "tokens": ток, "в_пул": нетто, "why_not": None if ток > 0 else "ноль токенов"}
    if режим == "curve":
        fee_bps, cr_bps = кривая_bps
        нетто = SB.pump_net_to_curve(размер, fee_bps, cr_bps)
        vs, vt = ст["vs"], ст["vt"]
        if нетто <= 0 or vs <= 0 or vt <= 0:
            return {"ok": False, "why_not": "кривая: нулевой резерв или трата меньше комиссий"}
        ток = vt * нетто // (vs + нетто)
        if ст.get("rt") is not None and ток > ст["rt"]:
            ток = int(ст["rt"])
        return {"ok": ток > 0, "tokens": ток, "в_пул": нетто, "why_not": None if ток > 0 else "ноль токенов"}
    цена = ст.get("price")
    if not цена:
        return {"ok": False, "why_not": "нет цены последнего свопа"}
    ток = int(размер / цена)
    return {"ok": ток > 0, "tokens": ток, "в_пул": размер, "why_not": None}


def наша_продажа(режим: str, ст: dict, покупка: dict, продаём: int, *, f: float | None,
                 кривая_bps: tuple, g: float | None = None) -> dict:
    """Лампорты за продажу `продаём` токенов в состоянии ст со вставленной покупкой.

    x*y=k: рамка X = x/f (как у покупки), доля продажи g -- калибрована на
    настоящих продажах в пуле (доля_продажи)."""
    if продаём <= 0:
        return {"ok": True, "lamports": 0}
    if режим == "xyk":
        X = D(ст["x"]) / D(str(f)) + покупка["в_пул"]
        y = ст["y"] - покупка["tokens"]
        if y <= 0:
            return {"ok": False, "why_not": "резерв токена на выходе меньше нашей доли"}
        вал = X * продаём / (D(y) + продаём)
        return {"ok": True, "lamports": int(вал * D(str(g if g is not None else f)))}
    if режим == "launchlab":
        q = ст["quote"] + покупка["в_пул"]
        b = ст["base"] - покупка["tokens"]
        if b <= 0:
            return {"ok": False, "why_not": "launchlab: резерв меньше нашей доли"}
        вал = D(q) * продаём / (D(b) + продаём)
        return {"ok": True, "lamports": int(вал * (1 - D(str(f))))}
    if режим == "curve":
        fee_bps, cr_bps = кривая_bps
        vs = ст["vs"] + покупка["в_пул"]
        vt = ст["vt"] - покупка["tokens"]
        if vt <= 0:
            return {"ok": False, "why_not": "кривая: резерв меньше нашей доли"}
        вал = vs * продаём // (vt + продаём)
        нетто = вал - (-(-вал * fee_bps // 10_000)) - (-(-вал * cr_bps // 10_000))
        return {"ok": True, "lamports": max(0, нетто)}
    return {"ok": True, "lamports": int(продаём * ст["price"])}


def чистый_пп(лампорты_выхода: int) -> float:
    return round((лампорты_выхода / ЛАМПОРТОВ - РАЗМЕР_ЛАМ / ЛАМПОРТОВ - ИЗДЕРЖКИ_SOL)
                 / (РАЗМЕР_ЛАМ / ЛАМПОРТОВ) * 100.0, 3)


# ================================================================ доля продажи (x*y=k)

# Доли продажи, увиденные за прогон, по программе пула: запасная калибровка,
# когда в самом пуле в окне продаж не нашлось.
ДОЛИ_ПРОДАЖИ: dict = {}
ДОЛЯ_ПРОДАЖИ_РАМКА = (0.5, 1.05)


def доля_продажи(tx: dict, пул: dict, минт: str, f: float) -> float | None:
    """g по настоящей продаже в пуле: что продавец получил / формула рамки X.

    Продавец -- единственный подписант, у которого минт убыл; получено --
    прирост его SOL (натив с возвратом платы за подпись, плюс WSOL). Вне рамки
    ДОЛЯ_ПРОДАЖИ_РАМКА (дробленый маршрут, чужая котировка) -- не берётся.
    """
    if not tx or (tx.get("meta") or {}).get("err") is not None:
        return None
    ряды = {r["account"]: r for r in C.token_rows(tx).values()}
    бв, кв = ряды.get(пул["pool_vault"]), ряды.get(пул["quote_vault"])
    if not бв or not кв:
        return None
    t_in = бв["post"] - бв["pre"]
    if t_in <= 0 or кв["post"] >= кв["pre"] or бв["pre"] <= 0:
        return None
    продавцы = C.mint_sellers(tx, минт)
    if len(продавцы) != 1:
        return None
    продавец = next(iter(продавцы))
    q = C.quote_spend(tx, продавец)
    плата = D((tx.get("meta") or {}).get("fee") or 0) / D(ЛАМПОРТОВ)
    ключи = C.account_keys(tx)
    получено = -q["sol"] + (плата if ключи and ключи[0] == продавец else D(0)) - q["wsol"]
    if получено <= 0:
        return None
    X0 = D(кв["pre"]) / D(str(f))
    формула = X0 * t_in / (D(бв["pre"]) + t_in)
    if формула <= 0:
        return None
    g = float(получено * ЛАМПОРТОВ / формула)
    return g if ДОЛЯ_ПРОДАЖИ_РАМКА[0] < g < ДОЛЯ_ПРОДАЖИ_РАМКА[1] else None


def медиана(xs: list):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


# ================================================================ история пула в окне

def подпись_после_слота(уз: Узел, слот: int) -> str | None:
    """Любая подпись из блока не раньше слота -- опора для before.

    before у getSignaturesForAddress -- любая подпись в журнале, не обязательно
    этого адреса (узел находит её слот и идёт вниз). None -- слота ещё нет.
    """
    try:
        блоки = уз.вызов("getBlocksWithLimit", [слот, 4]) or []
    except RuntimeError:
        return None
    for б in блоки:
        try:
            блок = уз.вызов("getBlock", [б, {"transactionDetails": "signatures", "rewards": False,
                                             "maxSupportedTransactionVersion": 0,
                                             "commitment": "confirmed"}], срок=40.0)
        except RuntimeError:
            continue
        подп = (блок or {}).get("signatures") or []
        if подп:
            return подп[0]
    return None


def история_пула(уз: Узел, хранилище: str, подпись_ист: str, s0: int, *,
                 опора: str | None, до_слота: int) -> dict:
    """Подписи хранилища ПОСЛЕ сделки источника, до слота до_слота включительно.

    Возврат в порядке цепи (старые -> новые). Если опоры нет -- листаем от
    вершины; предел страниц пишется в причину, а не молча.
    """
    из_ = {"подписи": [], "страниц": 0, "предел": False, "why_not": None, "с_опорой": bool(опора)}
    до = опора
    собрано: list = []
    while True:
        if из_["страниц"] >= ПРЕДЕЛ_СТРАНИЦ_ПУЛА:
            из_["предел"] = True
            break
        try:
            стр = уз.подписи(хранилище, до=до, по=подпись_ист, limit=1000)
        except RuntimeError as exc:
            из_["why_not"] = чисто(str(exc))[:160]
            break
        из_["страниц"] += 1
        if not стр:
            break
        собрано.extend(з for з in стр if (з.get("slot") or 0) <= до_слота
                       and з.get("signature") != подпись_ист)
        if len(стр) < 1000:
            break
        до = стр[-1].get("signature")
        if (стр[-1].get("slot") or 0) < s0:
            break
    собрано.reverse()
    из_["подписи"] = [{"signature": з["signature"], "slot": з.get("slot"),
                       "ok": з.get("err") is None} for з in собрано
                      if (з.get("slot") or 0) >= s0]
    return из_


_СЛОТ: dict = {}


def текущий_слот(уз: Узел) -> int:
    """Слот вершины, раз в минуту: окно выхода ещё не закрыто -- это видно."""
    if not _СЛОТ or time.time() - _СЛОТ["t"] > 60:
        try:
            _СЛОТ.update(s=int(уз.вызов("getSlot", [])), t=time.time())
        except RuntimeError:
            _СЛОТ.update(s=_СЛОТ.get("s", 0), t=time.time())
    return _СЛОТ["s"]


# ================================================================ одна покупка источника

def режим_из_метода(метод: str | None) -> str | None:
    if not метод:
        return None
    if метод.startswith("x*y=k"):
        return "xyk"
    if метод.startswith("кривая"):
        return "curve"
    return "price"


def симулировать(уз: Узел, покупка: dict, *, опора: str | None = None,
                 горизонты: tuple = ГОРИЗОНТЫ, наш_слот: int | None = None,
                 наша_трата_лам: int | None = None) -> dict:
    """Вход S+0/S+1/S+2, выход s0+H для одной первой покупки источника.

    покупка: signature, slot, blockTime, mint, wallet (источник).
    наш_слот / наша_трата_лам -- для сверки 1б: ещё одна точка входа «по
    фактическому слоту и размеру».
    """
    подп, s0, минт, кош = покупка["signature"], покупка["slot"], покупка["mint"], покупка["wallet"]
    из_: dict = {"signature": подп, "slot": s0, "mint": минт, "wallet": кош, "why_not": None}
    try:
        tx0 = уз.tx(подп)
    except RuntimeError as exc:
        из_["why_not"] = f"узел не отдал сделку источника: {чисто(str(exc))[:120]}"
        return из_
    if not tx0:
        из_["why_not"] = "узел не отдал сделку источника"
        return из_
    s0 = tx0.get("slot") or s0
    из_["slot"] = s0
    # S+0 -- существующий симулятор, без изменений.
    сим0 = P.наша_покупка_по_симулятору(tx0, кош, минт, РАЗМЕР_ЛАМ)
    пул = C.identify_pool(tx0, кош, минт)
    из_.update(program=сим0.get("program"), method=сим0.get("method"),
               concentrated=bool(сим0.get("concentrated")), curve=bool(сим0.get("curve")),
               quote_mint=сим0.get("quote_mint"), fee_factor=сим0.get("fee_factor"),
               pool_vault=пул.get("pool_vault"), quote_vault=пул.get("quote_vault"))
    if not сим0.get("ok") or not сим0.get("tokens_raw"):
        из_["why_not"] = f"S+0: {сим0.get('why_not') or 'симулятор не дал числа'}"
        return из_
    режим = режим_из_метода(сим0.get("method"))
    f = сим0.get("fee_factor")
    if режим == "xyk" and сим0.get("program") == SB.LAUNCHLAB:
        # LaunchLab: резервы виртуальные, из события; f здесь -- ДОЛЯ КОМИССИИ.
        режим = "launchlab"
        мо = SB.launchlab_min_out(tx0, РАЗМЕР_ЛАМ, 0.0)
        f = мо.get("fee_rate") if мо.get("ok") else None
        if f is None:
            из_["why_not"] = "launchlab: нет события сделки источника"
            return из_
    из_["режим"] = режим
    из_["флаг_без_проскальзывания"] = режим == "price"
    if режим == "xyk" and f is None:
        из_["why_not"] = "x*y=k без калиброванной комиссии"
        return из_
    кривая_bps = (0, 0)
    if режим == "curve":
        ев0 = SB.pump_trade_event(tx0, минт)
        if not ев0:
            из_["why_not"] = "кривая: нет события сделки источника"
            return из_
        кривая_bps = (int(ев0["fee_bps"]), int(ев0["creator_fee_bps"]))
    ст0 = состояние(tx0, режим, пул, минт) if режим != "price" else None
    if режим == "price":
        цена = (P.резервы_после(tx0, кош, минт) or {}).get("price_last")
        ст0 = {"price": цена, "q_after": None} if цена else None
    if not ст0:
        из_["why_not"] = f"S+0: состояние пула по сделке источника не читается ({режим})"
        return из_
    налог = налог_минта(уз, минт)
    из_["налог_bps"] = налог.get("bps")
    из_["token_2022"] = налог.get("program") == TOKEN_2022

    # История пула в окне.
    макс_слот = s0 + max(горизонты)
    if наш_слот:
        макс_слот = max(макс_слот, наш_слот)
    # Shyft (проба 26.09) НЕ принимает before с чужой подписью: 0 подписей даже
    # на свежей. Опора отключена -- листаем от вершины до сделки источника
    # (until); предел страниц пишется в причину.
    опора = None
    ист = история_пула(уз, пул["pool_vault"], подп, s0, опора=опора, до_слота=макс_слот)
    из_["история"] = {"подписей": len(ист["подписи"]), "страниц": ист["страниц"],
                      "с_опорой": ист["с_опорой"], "предел": ист["предел"],
                      "why_not": ист["why_not"]}
    if ист["why_not"] or ист["предел"]:
        из_["why_not"] = ("история пула не собрана: "
                          + (ист["why_not"] or f"предел {ПРЕДЕЛ_СТРАНИЦ_ПУЛА} страниц"))
        return из_
    успешные = [з for з in ист["подписи"] if з["ok"]]
    из_["толпа_s0_2"] = sum(1 for з in успешные if з["slot"] <= s0 + 2)
    из_["окно_полное"] = текущий_слот(уз) > s0 + max(горизонты)

    кэш_ст: dict = {}
    взятые: set = {подп}

    def ст_до(слот_вкл: int):
        """Состояние после последней успешной tx пула со слотом <= слот_вкл."""
        канд = [з for з in успешные if з["slot"] <= слот_вкл]
        if not канд:
            return ст0, "S+0", True
        for з in reversed(канд[-(ШАГОВ_НАЗАД + 1):]):
            п = з["signature"]
            if п not in кэш_ст:
                взятые.add(п)
                try:
                    кэш_ст[п] = состояние(уз.tx(п), режим, пул, минт)
                except RuntimeError:
                    кэш_ст[п] = None
            if кэш_ст[п]:
                return кэш_ст[п], п, False
        return ст0, "S+0 (точки не читаются)", False

    # Точки входа и выхода.
    миграция = False
    входы: dict = {}
    for e in ВХОДЫ:
        ст, откуда, без = (ст0, "S+0", True) if e == 0 else ст_до(s0 + e)
        пок = наша_покупка(режим, ст, f=f, кривая_bps=кривая_bps)
        if e == 0 and пок.get("ok") and режим != "price":
            пок["tokens_sim0"] = int(сим0["tokens_raw"])
        входы[e] = {"ст": ст, "пок": пок, "откуда": откуда}
    ст1 = входы[1]["ст"]
    if режим == "xyk":
        из_["ликвидность_sol"] = round(ст1["x"] / ЛАМПОРТОВ, 3)
    elif режим == "curve":
        из_["ликвидность_sol"] = round((ст1.get("rs") or 0) / ЛАМПОРТОВ, 3)
    elif режим == "launchlab":
        из_["ликвидность_sol"] = round((ст1.get("rq") or 0) / ЛАМПОРТОВ, 3)
    else:
        из_["ликвидность_sol"] = round((ст1.get("q_after") or 0) / ЛАМПОРТОВ, 3) if ст1.get("q_after") else None
    # Состояния выхода -- сначала все, потом доля продажи по увиденным продажам.
    выходы: dict = {}
    без_свопов: dict = {}
    for H in горизонты:
        ст, откуда, без = ст_до(s0 + H - 1)
        выходы[H] = ст
        без_свопов[H] = без
        if режим == "curve" and (ст.get("rt") == 0):
            миграция = True
    g, g_откуда = None, None
    if режим == "xyk":
        # Продажи в самом пуле: уже взятые точки состояния плюс до 4 равномерно
        # взятых транзакций окна. Нет продаж -- медиана по программе за прогон
        # (не меньше 5 наблюдений), иначе f с флагом.
        доли = [доля_продажи(уз._кэш.get(п), пул, минт, f) for п in list(кэш_ст)]  # noqa: SLF001
        шаг = max(1, len(успешные) // 4)
        for з in успешные[::шаг][:4]:
            if з["signature"] in кэш_ст:
                continue
            try:
                доли.append(доля_продажи(уз.tx(з["signature"]), пул, минт, f))
            except RuntimeError:
                pass
            взятые.add(з["signature"])
        доли = [d for d in доли if d]
        прог = сим0.get("program")
        if доли:
            g, g_откуда = медиана(доли), f"продажи пула ({len(доли)})"
            ДОЛИ_ПРОДАЖИ.setdefault(прог, []).extend(доли)
        elif len(ДОЛИ_ПРОДАЖИ.get(прог) or []) >= 5:
            g, g_откуда = медиана(ДОЛИ_ПРОДАЖИ[прог]), f"медиана программы ({len(ДОЛИ_ПРОДАЖИ[прог])})"
        else:
            g, g_откуда = f, "продаж не видно: доля покупки f (флаг)"
        из_["доля_продажи"] = round(g, 5) if g else None
        из_["доля_продажи_откуда"] = g_откуда
    чистые: dict = {f"S{e}": {} for e in ВХОДЫ}
    for H in горизонты:
        ст = выходы[H]
        for e in ВХОДЫ:
            пок = входы[e]["пок"]
            if not пок.get("ok"):
                чистые[f"S{e}"][H] = None
                continue
            получено = пок["tokens"] - удержано(пок["tokens"], налог)
            в_пул = получено - удержано(получено, налог)
            пр = наша_продажа(режим, ст, пок, в_пул, f=f, кривая_bps=кривая_bps, g=g)
            чистые[f"S{e}"][H] = чистый_пп(пр["lamports"]) if пр.get("ok") else None
    if режим == "curve":
        for п, ст in кэш_ст.items():
            if ст and ст.get("rt") == 0:
                миграция = True
    из_["миграция_в_окне"] = миграция
    из_["без_свопов"] = без_свопов
    из_["чистый_пп"] = чистые
    из_["вход_откуда"] = {f"S{e}": входы[e]["откуда"] for e in ВХОДЫ}
    из_["отказ_входа"] = {f"S{e}": входы[e]["пок"].get("why_not") for e in ВХОДЫ
                          if not входы[e]["пок"].get("ok")}
    # Точка сверки 1б: фактический слот и фактический размер.
    if наш_слот and наша_трата_лам:
        ст, откуда, _ = ст_до(наш_слот - 1)
        пок = наша_покупка(режим, ст, f=f, кривая_bps=кривая_bps, размер=int(наша_трата_лам))
        из_["факт_слот"] = {"ok": bool(пок.get("ok")), "tokens_gross": пок.get("tokens"),
                            "tokens_net": (пок["tokens"] - удержано(пок["tokens"], налог))
                            if пок.get("ok") else None,
                            "откуда": откуда, "why_not": пок.get("why_not")}
    # Кэш узла не копится от покупки к покупке: история пула -- разовая.
    for п in взятые:
        уз._кэш.pop(п, None)  # noqa: SLF001
    return из_


# ================================================================ возраст токена

def возраст_токена(уз: Узел, минт: str, подпись_ист: str, bt_ист: int) -> dict:
    """Минуты от первой подписи минта до сделки источника (или нижняя граница)."""
    до = подпись_ист
    старейшая = None
    for _ in range(ПРЕДЕЛ_СТРАНИЦ_МИНТА):
        try:
            стр = уз.подписи(минт, до=до, limit=1000)
        except RuntimeError as exc:
            return {"минут": None, "точно": False, "why_not": чисто(str(exc))[:120]}
        if not стр:
            break
        старейшая = стр[-1]
        бт = старейшая.get("blockTime") or bt_ист
        if bt_ист - бт > 86400:
            return {"минут": round((bt_ист - бт) / 60, 1), "точно": False, "старше_суток": True}
        if len(стр) < 1000:
            return {"минут": round((bt_ист - бт) / 60, 1), "точно": True}
        до = старейшая.get("signature")
    if старейшая is None:
        return {"минут": 0.0, "точно": True}
    return {"минут": round((bt_ист - (старейшая.get("blockTime") or bt_ист)) / 60, 1),
            "точно": False, "why_not": f"предел {ПРЕДЕЛ_СТРАНИЦ_МИНТА} страниц"}


# ================================================================ первые покупки кошелька

def разбор_сделки(tx: dict, кош: str) -> dict | None:
    """Что кошелёк сделал в tx: покупка (первая или нет), продажа, ничего.

    Покупка: кошелёк подписант, токен (не котировка) пришёл, котировка ушла.
    Первая: пред-баланс минта на всех счетах кошелька -- ноль.
    """
    if not tx or (tx.get("meta") or {}).get("err") is not None:
        return None
    if кош not in C.signers(tx):
        return None
    по_минту: dict = {}
    for r in C.token_rows(tx).values():
        if r.get("owner") != кош or not r.get("mint"):
            continue
        м = по_минту.setdefault(r["mint"], {"pre": 0, "post": 0, "dec": r.get("dec")})
        м["pre"] += r["pre"]
        м["post"] += r["post"]
    q = C.quote_spend(tx, кош)
    мета = tx.get("meta") or {}
    ключи = C.account_keys(tx)
    нат = q["sol"]
    if ключи and ключи[0] == кош:
        нат -= D(мета.get("fee") or 0) / D(ЛАМПОРТОВ)
    трата_sol = max(D(0), нат) + max(D(0), q["wsol"])
    трата_usd = max(D(0), q["usd"])
    приход_sol = max(D(0), -нат) + max(D(0), -q["wsol"])
    приход_usd = max(D(0), -q["usd"])
    рост = [(м, v) for м, v in по_минту.items() if м not in КОТИРОВКИ and v["post"] > v["pre"]]
    убыль = [(м, v) for м, v in по_минту.items() if м not in КОТИРОВКИ and v["post"] < v["pre"]]
    из_ = {"slot": tx.get("slot"), "blockTime": tx.get("blockTime"), "покупка": None,
           "продажи": []}
    if убыль and (приход_sol > 0 or приход_usd > 0):
        из_["продажи"] = [м for м, _ in убыль]
    if рост and (трата_sol > 0 or трата_usd > 0):
        м, v = max(рост, key=lambda x: x[1]["post"] - x[1]["pre"])
        из_["покупка"] = {"mint": м, "tokens_raw": int(v["post"] - v["pre"]), "dec": v.get("dec"),
                          "первая": v["pre"] == 0, "трата_sol": float(трата_sol),
                          "трата_usd": float(трата_usd), "минтов_выросло": len(рост)}
    return из_


def котировка_пула(tx: dict, кош: str, минт: str) -> str:
    """SOL / USDC / USDT / другое(минт) / не опознан -- по встречному хранилищу пула."""
    try:
        п = C.identify_pool(tx, кош, минт)
    except Exception:  # noqa: BLE001
        return "пул не опознан"
    q = п.get("quote_mint")
    if not q:
        return "пул не опознан"
    if q in (C.WSOL, C.NATIVE_QUOTE):
        return "SOL"
    if q == C.USDC:
        return "USDC"
    if q == C.USDT:
        return "USDT"
    return "другое"


def скан_кошелька(уз: Узел, кош: str, с_ts: float, до_ts: float, *,
                  предел_на_порог: int | None = None, курс=None,
                  предел_подписей: int | None = None, все_покупки: bool = False) -> dict:
    """Первые покупки кошелька в окне [с_ts, до_ts), от свежих к старым.

    Покрытие -- по РАЗОБРАННЫМ транзакциям (узел отдал), а не по подписям.
    Продажи копятся по минту, чтобы ответить «источник продал в окне».
    """
    из_ = {"адрес": кош, "подписей": 0, "разобрано": 0, "не_отдал": 0, "страниц": 0,
           "покупки": [], "не_первых_покупок": 0, "ниже_порога": 0, "нет_курса": 0,
           "why_not": None, "остановка": None, "самый_свежий_слот": None,
           "самый_старый_слот": None, "все_покупки": [], "_подписи_окна": []}
    продажи: dict = {}
    слоты_подписей: list = []
    до = None
    набрано = {имя: 0 for имя, *_ in ПОРОГИ}
    while True:
        try:
            стр = уз.подписи(кош, до=до, limit=1000)
        except RuntimeError as exc:
            из_["why_not"] = чисто(str(exc))[:160]
            break
        из_["страниц"] += 1
        if not стр:
            break
        окно, конец = [], False
        for з in стр:
            bt = з.get("blockTime") or 0
            if bt and bt >= до_ts:
                continue
            if bt and bt < с_ts:
                конец = True
                break
            слоты_подписей.append((з.get("slot") or 0, з["signature"]))
            if з.get("err"):
                continue
            окно.append(з)
        txs = уз.пакет([з["signature"] for з in окно]) if окно else {}
        for з in окно:
            из_["подписей"] += 1
            if все_покупки:
                из_["_подписи_окна"].append(з["signature"])
            tx = txs.get(з["signature"])
            if not tx:
                из_["не_отдал"] += 1
                continue
            из_["разобрано"] += 1
            р = разбор_сделки(tx, кош)
            if not р:
                continue
            for м in р["продажи"]:
                продажи.setdefault(м, []).append(р["slot"])
            пк = р["покупка"]
            if not пк:
                continue
            if все_покупки:
                # Для сопоставления с журналом DBot: КАЖДАЯ покупка, с числом
                # токенов, -- DBot пишет ровно его в follow.receive.amount.
                из_["все_покупки"].append({**пк, "signature": з["signature"], "slot": р["slot"],
                                           "blockTime": р["blockTime"]})
            if not пк["первая"]:
                из_["не_первых_покупок"] += 1
                continue
            пк["котировка_пула"] = котировка_пула(tx, кош, пк["mint"])
            курс_usd, откуда_курса = None, None
            sol_экв = пк["трата_sol"]
            if пк["трата_usd"] > 0:
                if курс is not None:
                    try:
                        курс_usd, откуда_курса = курс.rate_for(tx)
                    except Exception as exc:  # noqa: BLE001
                        курс_usd, откуда_курса = None, чисто(str(exc))[:80]
                if курс_usd is None:
                    из_["нет_курса"] += 1
                    из_["покупки"].append({**пк, "signature": з["signature"], "slot": р["slot"],
                                           "blockTime": р["blockTime"], "wallet": кош,
                                           "sol_экв": None, "порог": None, "котировка": "USD",
                                           "why_not": f"трата в стейблах, курса нет ({откуда_курса})"})
                    continue
                sol_экв += пк["трата_usd"] / float(курс_usd)
            порог = порог_покупки(sol_экв)
            if порог is None:
                из_["ниже_порога"] += 1
                continue
            if предел_на_порог and набрано[порог] >= предел_на_порог:
                continue
            набрано[порог] += 1
            из_["покупки"].append({**пк, "signature": з["signature"], "slot": р["slot"],
                                   "blockTime": р["blockTime"], "wallet": кош,
                                   "sol_экв": round(sol_экв, 4), "порог": порог,
                                   "котировка": ("USD" if пк["трата_usd"] > 0 and пк["трата_sol"] <= 0.01
                                                 else "SOL" if пк["трата_usd"] <= 0 else "SOL+USD"),
                                   "курс": float(курс_usd) if курс_usd else None,
                                   "курс_откуда": откуда_курса})
        if конец or len(стр) < 1000:
            break
        if предел_на_порог and all(v >= предел_на_порог for v in набрано.values()):
            из_["остановка"] = f"набрано по {предел_на_порог} на порог"
            break
        if предел_подписей and из_["подписей"] >= предел_подписей:
            из_["остановка"] = f"предел {предел_подписей} подписей"
            break
        до = стр[-1]["signature"]
    слоты_подписей.sort()
    if слоты_подписей:
        из_["самый_старый_слот"] = слоты_подписей[0][0]
        из_["самый_свежий_слот"] = слоты_подписей[-1][0]
    из_["покрытие"] = round(из_["разобрано"] / из_["подписей"], 4) if из_["подписей"] else None
    # «Источник продал в окне» и опора для истории пула -- для каждой покупки.
    for пк in из_["покупки"]:
        s0 = пк.get("slot") or 0
        сл = [s for s in продажи.get(пк["mint"], []) if s0 < s <= s0 + ОКНО_ПРОДАЖИ]
        пк["продал_в_окне"] = bool(сл)
        пк["окно_продажи_полное"] = bool(из_["самый_свежий_слот"]) and \
            из_["самый_свежий_слот"] > s0 + ОКНО_ПРОДАЖИ
        пк["опора"] = next((п for s, п in слоты_подписей if s > s0 + max(ГОРИЗОНТЫ) + 1), None)
    return из_


def utc(ts) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts))) if ts else ""


if __name__ == "__main__":
    print(json.dumps({"модуль": "podbivka_sim", "горизонты": ГОРИЗОНТЫ, "входы": ВХОДЫ},
                     ensure_ascii=False))
