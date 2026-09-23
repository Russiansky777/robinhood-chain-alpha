#!/usr/bin/env python3
"""Сверка детектора v2 с триггерами DBot вхолостую (офлайн).

Что сверяется
-------------
Эталон -- записи follow_trades ЗАДАЧ BATCH-5 и BATCH-3 из снимка
data/final/20260923T145755Z/follow_trades.json: 864 покупочных сигнала
(589 + 275). Это именно ТРИГГЕРЫ, а не исполненные покупки: в выборку
входят и отказы (skipReason), включая те, где сделка не состоялась.

Что в этой сверке проверяется НЕЗАВИСИМО, а что нет
---------------------------------------------------
* Размер входа (targetMinAmountUI = 2 SOL-эквивалента) -- ПРОВЕРЯЕТСЯ
  НЕЗАВИСИМО. Источники платят почти всегда в USDC (824 из 864), поэтому
  сумма пересчитывается в SOL по РЕАЛЬНОМУ историческому курсу на момент
  сигнала: минутные свечи пула SOL/USDC 3ucNos4NbumP... (GeckoTerminal),
  линейная интерполяция -- тот же способ, что в
  solana_29_candidates_usdc_to_sol.py. Наш вердикт сравнивается с
  вердиктом DBot, и расхождения выписываются поштучно.
* «Первый вход или докупка» (skipTargetIncreasePosition) -- НЕЗАВИСИМО
  ОФЛАЙН НЕ ПРОВЕРЯЕТСЯ: в записи follow_trades нет ни подписи
  транзакции источника, ни его балансов до сделки. Единственный офлайн
  источник этого признака -- вердикт самого DBot, поэтому он подаётся на
  вход, а не перепроверяется. Живьём этот признак берётся из
  preTokenBalances транзакции источника (см. bloom_detector) и будет
  сверяться на боевых сутках.
Так и написано в отчёте: смешивать проверенное с принятым на слово
нельзя, иначе доля совпадений будет завышена.

Наши лимиты -- отдельной строкой
--------------------------------
Дедуп по минту и прочие наши тормоза -- это сознательное отклонение от
DBot (у DBot maxBuyTimesPerToken=2 и два источника на одном токене дают
две покупки). Такие сигналы идут строкой «пропущено по нашему лимиту,
DBot купил» и в числитель расхождений НЕ попадают.

Запуск: python3 analysis/bloom_detector_validate.py [--self-test]
Сеть: только GeckoTerminal (курс). Ни DBot, ни цепь не опрашиваются.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_detector as BD  # noqa: E402
import bloom_exec_state as ST  # noqa: E402

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

REPO_ROOT = Path(__file__).resolve().parent.parent
СНИМОК = REPO_ROOT / "data" / "final" / "20260923T145755Z" / "follow_trades.json"
ВЫХОД = REPO_ROOT / "data" / "bloom_detector_validation.json"
КЕШ = REPO_ROOT / "data" / "bloom_detector_cache"

GECKO_BASE = "https://api.geckoterminal.com/api/v2"
SOL_USDC_POOL = "3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv"
ЗАДАЧИ = ("BATCH-5", "BATCH-3")

# Наши лимиты в симуляции -- ровно те, что заданы по умолчанию в
# bloom_exec_state, чтобы сверка мерила то, что поедет в бой.
ГОРИЗОНТ_S = 28.8
ОТСРОЧКА_СТОРОЖА_S = 15.0
ДЕРЖИМ_S = ГОРИЗОНТ_S + ОТСРОЧКА_СТОРОЖА_S


# ------------------------------------------------------------------ курс

def gecko_get(path: str, params: dict) -> dict:
    КЕШ.mkdir(parents=True, exist_ok=True)
    ключ = f"{path}_{json.dumps(params, sort_keys=True)}"
    f = КЕШ / f"gecko_{hashlib.sha256(ключ.encode()).hexdigest()[:32]}.json"
    if f.exists():
        try:
            return {"http_status": 200, "body": json.loads(f.read_text())}
        except (ValueError, OSError):
            pass
    if requests is None:
        return {"http_status": None, "why_not": "нет requests"}
    backoff = 1.0
    for _ in range(8):
        try:
            r = requests.get(f"{GECKO_BASE}{path}", params=params, timeout=30,
                              headers={"Accept": "application/json"})
        except Exception as exc:  # noqa: BLE001
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            последняя = f"{type(exc).__name__}: {str(exc)[:120]}"
            continue
        if r.status_code == 429:
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        if not r.ok:
            return {"http_status": r.status_code}
        body = r.json()
        f.write_text(json.dumps(body))
        return {"http_status": 200, "body": body}
    return {"http_status": None, "why_not": locals().get("последняя")}


def свечи(lo: int, hi: int) -> list:
    """Минутные свечи SOL/USDC за период. Возвращает [[ts, o,h,l,c,v], ...]."""
    все: list = []
    курсор = hi + 120
    for _ in range(40):
        r = gecko_get(f"/networks/solana/pools/{SOL_USDC_POOL}/ohlcv/minute",
                       {"aggregate": 1, "before_timestamp": курсор,
                        "limit": 1000, "currency": "usd"})
        if r.get("http_status") != 200:
            break
        rows = (((r.get("body") or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        if not rows:
            break
        все.extend(rows)
        старейшая = min(row[0] for row in rows)
        if старейшая <= lo or старейшая >= курсор:
            break
        курсор = старейшая
        if len(rows) < 1000:
            break
    return sorted({row[0]: row for row in все}.values(), key=lambda r: r[0])


def цена_в(свечи_: list, t: int) -> float | None:
    """Линейная интерполяция цены закрытия между соседними минутами.

    Экстраполяции нет: за краями диапазона -- None, а не ближайшая цена,
    иначе одна старая свеча «покроет» сутки без данных.
    """
    if not свечи_:
        return None
    ts = [row[0] for row in свечи_]
    цены = [float(row[4]) for row in свечи_]
    if t < ts[0] or t > ts[-1]:
        return None
    i = bisect.bisect_left(ts, t)
    if i < len(ts) and ts[i] == t:
        return цены[i]
    if i == 0:
        return цены[0]
    t0, t1 = ts[i - 1], ts[i]
    c0, c1 = цены[i - 1], цены[i]
    if t1 == t0:
        return c0
    доля = (t - t0) / (t1 - t0)
    return c0 + (c1 - c0) * доля


# ------------------------------------------------- симуляция наших лимитов

class СимСостояние:
    """Повторяет ту часть can_open_detailed, которую можно проиграть
    офлайн: открытые позиции, дубль по минту, покупок на минт, кулдаун,
    уже виденные подписи. Лимиты, зависящие от сети и от денег (баланс,
    429, недельный бюджет), в офлайн-сверке не участвуют и это сказано
    в отчёте, а не замаскировано."""

    def __init__(self, *, max_open=ST.DEFAULT_MAX_OPEN,
                  max_buys_per_mint=ST.DEFAULT_MAX_BUYS_PER_MINT,
                  cooldown_s=ST.DEFAULT_MINT_COOLDOWN_S,
                  держим_s=ДЕРЖИМ_S) -> None:
        self.max_open = max_open
        self.max_buys_per_mint = max_buys_per_mint
        self.cooldown_s = cooldown_s
        self.держим_s = держим_s
        self.открытые: list = []          # [(минт, когда_закроется)]
        self.покупок: dict = {}
        self.последняя: dict = {}
        self.подписи: set = set()
        self.часы = 0.0

    def тик(self, now: float) -> None:
        self.часы = now
        self.открытые = [(m, t) for m, t in self.открытые if t > now]

    def can_open_detailed(self, *, mint, source_sig, balance_sol, now=None):
        now = self.часы if now is None else now
        if len(self.открытые) >= self.max_open:
            return False, f"открыто {len(self.открытые)} при лимите {self.max_open}", \
                ST.КОД_ЛИМИТ_ОТКРЫТЫХ
        if any(m == mint for m, _ in self.открытые):
            return False, f"по минту {mint[:10]} уже есть открытая позиция", ST.КОД_ДУБЛЬ_МИНТА
        if self.покупок.get(mint, 0) >= self.max_buys_per_mint > 0:
            return False, (f"по минту {mint[:10]} уже {self.покупок[mint]} покупок при "
                            f"лимите {self.max_buys_per_mint}"), ST.КОД_ПОКУПОК_НА_МИНТ
        t = self.последняя.get(mint)
        if t is not None and (now - t) < self.cooldown_s:
            return False, (f"по минту {mint[:10]} кулдаун: {now - t:.0f} с из "
                            f"{self.cooldown_s:.0f}"), ST.КОД_ДУБЛЬ_МИНТА
        if source_sig in self.подписи:
            return False, "подпись источника уже обработана", ST.КОД_ПОДПИСЬ_ВИДЕЛИ
        return True, "ок", ST.КОД_ОК

    def открыли(self, mint: str, source_sig: str, now: float) -> None:
        self.открытые.append((mint, now + self.держим_s))
        self.покупок[mint] = self.покупок.get(mint, 0) + 1
        self.последняя[mint] = now
        self.подписи.add(source_sig)


def граница(записи: list, свечи_: list, порог: float) -> dict:
    """Насколько сверка размера вообще различающая.

    Совпадение «мы и DBot одинаково судим о размере» стоит немного, если
    все суммы далеко от порога: тогда порог мог быть и 1, и 3. Поэтому
    считается, где на самом деле лежит граница между классами DBot:
    максимум у тех, кого DBot назвал слишком маленькими, и минимум у
    остальных. Если эти два числа лежат по разные стороны от нашего
    порога и близко к нему -- порог подтверждён данными, а не совпал
    случайно.
    """
    мало, хватает, без_курса = [], [], 0
    for r in записи:
        s = сигнал_из_записи(r)
        курс = цена_в(свечи_, int((r.get("createAt") or 0) / 1000))
        v, _ = BD.в_sol(s, курс)
        if v is None:
            без_курса += 1
            continue
        строка = {"sol_экв": round(v, 6), "id": r.get("id"),
                   "минт_траты": s["spend_mint"], "spend_ui": s["spend_ui"]}
        (мало if r.get("skipReason") == "TARGET_AMOUNT_OUT_OF_RANGE" else хватает).append(строка)
    мало.sort(key=lambda x: -x["sol_экв"])
    хватает.sort(key=lambda x: x["sol_экв"])
    верх_мало = мало[0]["sol_экв"] if мало else None
    низ_хватает = хватает[0]["sol_экв"] if хватает else None
    разделяет = (верх_мало is not None and низ_хватает is not None
                  and верх_мало < порог <= низ_хватает)
    return {
        "порог": порог,
        "у_dbot_слишком_мало": len(мало),
        "у_dbot_размер_в_порядке": len(хватает),
        "без_курса": без_курса,
        "самый_крупный_из_слишком_маленьких": мало[:5],
        "самый_мелкий_из_остальных": хватает[:5],
        "верхняя_граница_класса_мало": верх_мало,
        "нижняя_граница_класса_хватает": низ_хватает,
        "зазор_между_классами": (round(низ_хватает - верх_мало, 6)
                                  if (верх_мало is not None and низ_хватает is not None) else None),
        "порог_лежит_между_классами": разделяет,
        "note": ("порог подтверждён данными: класс «мало» кончается ниже порога, "
                       "класс «хватает» начинается не ниже него"
                       if разделяет else
                       "классы НЕ разделены нашим порогом -- совпадение по размеру "
                       "объясняется не порогом, и так и надо читать"),
    }


# ------------------------------------------------------------------ сверка

ОТКАЗЫ_DBOT_НЕ_ПОКУПКА = ("FOLLOW_ORDER_IS_ONLY_PNL_MODE",)
ОТКАЗЫ_DBOT_ДУБЛЬ = ("DUPLICATE_TOKEN_BUY", "MAX_BUY_TIMES_PER_TOKEN_REACHED")


def сигнал_из_записи(r: dict) -> dict:
    """Псевдосигнал в том же виде, что выдаёт bloom_detector, но из
    записи DBot. Признак первого входа берётся из вердикта DBot -- см.
    оговорку в заголовке файла."""
    f = r.get("follow") or {}
    send = (f.get("send") or {})
    recv = (f.get("receive") or {})
    si = send.get("info") or {}
    ri = recv.get("info") or {}
    dec = si.get("decimals")
    try:
        сырое = int(send.get("amount"))
    except (TypeError, ValueError):
        сырое = None
    ui = (сырое / 10 ** dec) if (сырое is not None and isinstance(dec, int)) else None
    контракт = si.get("contract")
    докупка = r.get("skipReason") == "SKIP_TARGET_INCREASE_POSITION"
    s = {"signature": r.get("id"), "source": f.get("wallet"),
          "mint": ri.get("contract"), "kind": "buy",
          "spend_mint": контракт, "spend_ui": ui,
          "spend": ui if контракт == BD.WSOL else None,
          "first_entry": (not докупка),
          "первый_вход_откуда": "вердикт DBot (офлайн иначе не установить)",
          "dex_programs": [], "token_program": ri.get("tokenProgram"),
          "slot": None, "createAt": r.get("createAt")}
    return s


def наш_вердикт_размера(s: dict, курс: float | None,
                         порог: float) -> tuple[str, float | None]:
    трата, _ = BD.в_sol(s, курс)
    if трата is None:
        return "нет_курса", None
    return ("мало" if трата < порог else "хватает"), трата


def сверить(записи: list, свечи_: list, *, порог: float,
             порядок: str = "размер_раньше_докупки") -> dict:
    """Один проход по записям в хронологическом порядке."""
    сим = СимСостояние()
    итог = {"всего": 0, "совпало_действие": 0,
             "dbot_купил_мы_нет": [], "мы_купили_dbot_нет": [],
             "пропущено_нашим_лимитом_dbot_купил": [],
             "нет_курса": [], "размер": {"совпало": 0, "разошлось": 0, "нет_курса": 0,
                                          "расхождения": []},
             "по_кодам_наш": {}, "по_кодам_dbot": {}}

    for r in sorted(записи, key=lambda x: x.get("createAt") or 0):
        s = сигнал_из_записи(r)
        t_s = (r.get("createAt") or 0) / 1000.0
        сим.тик(t_s)
        курс = цена_в(свечи_, int(t_s))
        вердикт_размера, трата = наш_вердикт_размера(s, курс, порог)

        dbot = r.get("skipReason") or "ПРОШЛО"
        итог["по_кодам_dbot"][dbot] = итог["по_кодам_dbot"].get(dbot, 0) + 1

        # --- независимая сверка размера
        dbot_говорит_мало = dbot == "TARGET_AMOUNT_OUT_OF_RANGE"
        if вердикт_размера == "нет_курса":
            итог["размер"]["нет_курса"] += 1
            итог["нет_курса"].append({"id": r.get("id"), "createAt": r.get("createAt"),
                                       "spend_ui": s["spend_ui"], "минт_траты": s["spend_mint"]})
        else:
            # Сравнивать можно только там, где DBot до размера дошёл:
            # если он отказал раньше по другому признаку, его молчание о
            # размере -- не «размер в порядке».
            сравнимо = dbot_говорит_мало or dbot in ("ПРОШЛО", "SKIP_TARGET_INCREASE_POSITION",
                                                      "DUPLICATE_TOKEN_BUY",
                                                      "MAX_BUY_TIMES_PER_TOKEN_REACHED")
            if сравнимо:
                мы_мало = вердикт_размера == "мало"
                if мы_мало == dbot_говорит_мало:
                    итог["размер"]["совпало"] += 1
                else:
                    итог["размер"]["разошлось"] += 1
                    итог["размер"]["расхождения"].append(
                        {"id": r.get("id"), "задача": r.get("configName"),
                         "source": s["source"], "mint": s["mint"],
                         "createAt": r.get("createAt"),
                         "spend_ui": s["spend_ui"], "минт_траты": s["spend_mint"],
                         "rate_usd_sol": курс, "наш_sol_экв": трата,
                         "наш_вердикт": вердикт_размера, "dbot": dbot})

        # --- сквозное действие
        if порядок == "докупка_раньше_размера" and s["first_entry"] is False:
            наш_код, наше_действие = BD.КОД_ДОКУПКА, "skip"
            наша_причина = "докупка"
        else:
            ок, наш_код, наша_причина = BD.фильтры_dbot(s, трата, порог_sol=порог)
            наше_действие = "buy" if ок else "skip"
        if наше_действие == "buy":
            можно, почему, код2 = сим.can_open_detailed(
                mint=s["mint"], source_sig=s["signature"], balance_sol=None)
            if можно:
                сим.открыли(s["mint"], s["signature"], t_s)
            else:
                наше_действие, наш_код, наша_причина = "skip", код2, почему

        итог["всего"] += 1
        итог["по_кодам_наш"][наш_код] = итог["по_кодам_наш"].get(наш_код, 0) + 1

        dbot_купил = dbot == "ПРОШЛО"
        мы_купили = наше_действие == "buy"
        карточка = {"id": r.get("id"), "задача": r.get("configName"),
                     "source": s["source"], "mint": s["mint"],
                     "createAt": r.get("createAt"), "spend_ui": s["spend_ui"],
                     "минт_траты": s["spend_mint"], "rate_usd_sol": курс,
                     "наш_sol_экв": трата, "наш_код": наш_код,
                     "наша_причина": наша_причина, "dbot": dbot,
                     "состояние_dbot": r.get("state"),
                     "ошибка_dbot": r.get("errorMessage") or None}

        if мы_купили == dbot_купил:
            итог["совпало_действие"] += 1
        elif dbot_купил and not мы_купили:
            if наш_код in (ST.КОД_ДУБЛЬ_МИНТА, ST.КОД_ПОКУПОК_НА_МИНТ,
                            ST.КОД_ЛИМИТ_ОТКРЫТЫХ, ST.КОД_ПОДПИСЬ_ВИДЕЛИ):
                итог["пропущено_нашим_лимитом_dbot_купил"].append(карточка)
            else:
                итог["dbot_купил_мы_нет"].append(карточка)
        else:
            итог["мы_купили_dbot_нет"].append(карточка)

    знаменатель = итог["всего"] - len(итог["пропущено_нашим_лимитом_dbot_купил"])
    расхождений = len(итог["dbot_купил_мы_нет"]) + len(итог["мы_купили_dbot_нет"])
    итог["знаменатель_без_наших_лимитов"] = знаменатель
    итог["расхождений"] = расхождений
    итог["доля_совпадений"] = (round((знаменатель - расхождений) / знаменатель, 4)
                                if знаменатель else None)
    р = итог["размер"]
    сравнено = р["совпало"] + р["разошлось"]
    р["сравнено"] = сравнено
    р["доля_совпадений"] = round(р["совпало"] / сравнено, 4) if сравнено else None
    return итог


def прогнать(путь: Path = СНИМОК, *, порог: float = 2.0) -> dict:
    d = json.loads(путь.read_text(encoding="utf-8"))
    записи = []
    по_задачам = {}
    for tid, t in (d.get("по_задачам") or {}).items():
        if t.get("задача") not in ЗАДАЧИ:
            continue
        b = [r for r in (t.get("записи") or []) if r.get("type") == "buy"]
        по_задачам[t.get("задача")] = {"покупочных_сигналов": len(b),
                                        "выкачано_полностью": t.get("выкачано_полностью")}
        записи.extend(b)
    if not записи:
        raise RuntimeError("в снимке нет покупочных сигналов нужных задач")

    lo = min(r["createAt"] for r in записи) // 1000
    hi = max(r["createAt"] for r in записи) // 1000
    св = свечи(lo, hi)
    # Без курса сверка размера превращается в 864 записи "нет курса" и
    # при этом выглядит выполненной. Это не результат, это отказ.
    if not св:
        raise RuntimeError("курс не получен ни одной свечой -- сверка размера "
                            "невозможна, а делать вид, что она прошла, нельзя")
    if св[0][0] > lo or св[-1][0] < hi:
        raise RuntimeError(
            f"свечи покрывают {св[0][0]}..{св[-1][0]}, а нужно {lo}..{hi}: "
            f"часть сигналов осталась бы без курса")

    оба = {}
    for порядок in ("размер_раньше_докупки", "докупка_раньше_размера"):
        оба[порядок] = сверить(записи, св, порог=порог, порядок=порядок)

    доли = {k: v["доля_совпадений"] for k, v in оба.items()}
    if len(set(доли.values())) == 1:
        порядок_вывод = ("не различается на этих данных: нет ни одной записи, где "
                          "сработали бы оба признака сразу, поэтому какой из фильтров "
                          "у DBot первый -- отсюда не установить")
    else:
        порядок_вывод = max(оба, key=lambda k: (оба[k]["доля_совпадений"] or 0))
    return {
        "источник_эталона": str(путь.relative_to(REPO_ROOT)),
        "tasks": по_задачам,
        "покупочных_сигналов_всего": len(записи),
        "порог_входа_sol": порог,
        "курс": {"пул": SOL_USDC_POOL, "source": "GeckoTerminal, минутные свечи",
                  "свечей": len(св),
                  "покрытие_utc": ([time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(св[0][0])),
                                     time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(св[-1][0]))]
                                    if св else None),
                  "нужно_utc": [time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(lo)),
                                 time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(hi))]},
        "порядок_фильтров": порядок_вывод,
        "различающая_сила_порога": граница(записи, св, порог),
        "прогоны": оба,
        "оговорки": [
            "Признак «первый вход или докупка» офлайн взят из вердикта DBot: "
            "в follow_trades нет ни подписи транзакции источника, ни его балансов. "
            "Независимо он проверяется только живьём, по preTokenBalances.",
            "Лимиты, зависящие от сети и денег (баланс кошелька, 429, недельный "
            "бюджет Bloom, дневной лимит потерь), в офлайн-сверке не участвуют.",
            "Пропуски по нашему дедупу вынесены отдельной строкой и в числитель "
            "расхождений не входят -- это сознательное отклонение от DBot.",
            "Сравнение размера ведётся только по записям, где DBot до проверки "
            "размера дошёл: его молчание о размере после отказа по другому "
            "признаку не значит «размер в порядке».",
        ],
    }


# ------------------------------------------------------------ самопроверка

def self_test() -> int:
    проверки = []

    def chk(имя, ок, факт=""):
        проверки.append((имя, bool(ок), факт))

    св = [[1000, 1, 1, 1, 200.0, 0], [1060, 1, 1, 1, 220.0, 0]]
    chk("цена в точке свечи", цена_в(св, 1000) == 200.0, цена_в(св, 1000))
    chk("цена посередине интерполируется", abs(цена_в(св, 1030) - 210.0) < 1e-9, цена_в(св, 1030))
    chk("за левым краем -- None", цена_в(св, 900) is None, цена_в(св, 900))
    chk("за правым краем -- None", цена_в(св, 2000) is None, цена_в(св, 2000))
    chk("пустые свечи -- None", цена_в([], 1000) is None)

    зап = {"id": "X", "createAt": 1000_000, "configName": "BATCH-5",
            "skipReason": None, "state": "done",
            "follow": {"wallet": "W",
                        "send": {"info": {"contract": BD.USDC, "decimals": 6},
                                  "amount": "500000000"},
                        "receive": {"info": {"contract": "M", "tokenProgram": BD.TOKEN_2022},
                                     "amount": "1"}}}
    s = сигнал_из_записи(зап)
    chk("трата 500 USDC разобрана", abs(s["spend_ui"] - 500) < 1e-9, s["spend_ui"])
    chk("стейбл не считается SOL", s["spend"] is None, s["spend"])
    chk("первый вход по умолчанию True", s["first_entry"] is True)
    chk("источник признака честно помечен", "DBot" in s["первый_вход_откуда"])
    зап2 = dict(зап, skipReason="SKIP_TARGET_INCREASE_POSITION")
    chk("докупка из вердикта DBot", сигнал_из_записи(зап2)["first_entry"] is False)

    зап3 = {"id": "Y", "createAt": 1000_000, "configName": "BATCH-5",
             "skipReason": None, "state": "done",
             "follow": {"wallet": "W",
                         "send": {"info": {"contract": BD.WSOL, "decimals": 9},
                                   "amount": "3000000000"},
                         "receive": {"info": {"contract": "M2"}, "amount": "1"}}}
    s3 = сигнал_из_записи(зап3)
    chk("трата в SOL не требует курса", abs(s3["spend"] - 3.0) < 1e-9, s3["spend"])
    chk("вердикт размера без курса работает для SOL",
        наш_вердикт_размера(s3, None, 2.0)[0] == "хватает")
    chk("вердикт размера для стейбла без курса -- нет_курса",
        наш_вердикт_размера(s, None, 2.0)[0] == "нет_курса")
    chk("500 USDC при 200 -- хватает", наш_вердикт_размера(s, 200.0, 2.0)[0] == "хватает")
    chk("500 USDC при 300 -- мало", наш_вердикт_размера(s, 300.0, 2.0)[0] == "мало")

    # симуляция лимитов
    сим = СимСостояние()
    сим.тик(0.0)
    можно, _, код = сим.can_open_detailed(mint="M", source_sig="s1", balance_sol=None)
    chk("сначала можно", можно and код == ST.КОД_ОК, код)
    сим.открыли("M", "s1", 0.0)
    можно, _, код = сим.can_open_detailed(mint="M", source_sig="s2", balance_sol=None)
    chk("тот же минт -- дубль", (not можно) and код == ST.КОД_ДУБЛЬ_МИНТА, код)
    сим.тик(ДЕРЖИМ_S + 1)
    можно, _, код = сим.can_open_detailed(mint="M", source_sig="s2", balance_sol=None)
    chk("после закрытия позиции всё равно кулдаун", (not можно) and код == ST.КОД_ДУБЛЬ_МИНТА, код)
    сим.тик(ST.DEFAULT_MINT_COOLDOWN_S + 1)
    можно, _, код = сим.can_open_detailed(mint="M", source_sig="s2", balance_sol=None)
    chk("после кулдауна можно снова", можно, код)
    сим.открыли("M", "s2", сим.часы)
    сим.тик(сим.часы + ST.DEFAULT_MINT_COOLDOWN_S + 1)
    можно, _, код = сим.can_open_detailed(mint="M", source_sig="s3", balance_sol=None)
    chk("третья покупка минта запрещена", (not можно) and код == ST.КОД_ПОКУПОК_НА_МИНТ, код)
    можно, _, код = сим.can_open_detailed(mint="M2", source_sig="s2", balance_sol=None)
    chk("виденная подпись отсекается", (not можно) and код == ST.КОД_ПОДПИСЬ_ВИДЕЛИ, код)

    сим2 = СимСостояние()
    сим2.тик(0.0)
    for i in range(ST.DEFAULT_MAX_OPEN):
        сим2.открыли(f"M{i}", f"s{i}", 0.0)
    можно, _, код = сим2.can_open_detailed(mint="MX", source_sig="sx", balance_sol=None)
    chk("лимит открытых срабатывает", (not можно) and код == ST.КОД_ЛИМИТ_ОТКРЫТЫХ, код)

    # сквозной проход на трёх записях с искусственными свечами
    свечи_t = [[1000, 1, 1, 1, 200.0, 0], [4000, 1, 1, 1, 200.0, 0]]
    записи = [
        dict(зап, id="A", createAt=1500_000, skipReason=None),                 # 2.5 SOL, проходит
        dict(зап, id="B", createAt=1600_000, skipReason="TARGET_AMOUNT_OUT_OF_RANGE",
              follow={"wallet": "W",
                      "send": {"info": {"contract": BD.USDC, "decimals": 6}, "amount": "21000000"},
                      "receive": {"info": {"contract": "M3"}, "amount": "1"}}),
        dict(зап, id="C", createAt=1700_000, skipReason="SKIP_TARGET_INCREASE_POSITION"),
    ]
    r = сверить(записи, свечи_t, порог=2.0)
    chk("сверено три записи", r["всего"] == 3, r["всего"])
    chk("размер совпал по всем сравнимым", r["размер"]["разошлось"] == 0, r["размер"])
    chk("расхождений нет", r["расхождений"] == 0, r)
    chk("доля совпадений 1.0", r["доля_совпадений"] == 1.0, r["доля_совпадений"])

    # тот же минт дважды подряд -> наш дедуп, отдельной строкой
    записи2 = [dict(зап, id="A", createAt=1500_000, skipReason=None),
                dict(зап, id="A2", createAt=1505_000, skipReason=None)]
    r2 = сверить(записи2, свечи_t, порог=2.0)
    chk("дубль минта вынесен отдельной строкой",
        len(r2["пропущено_нашим_лимитом_dbot_купил"]) == 1, r2["пропущено_нашим_лимитом_dbot_купил"])
    chk("дубль не попал в расхождения", r2["расхождений"] == 0, r2["расхождений"])
    chk("знаменатель уменьшен на дубль", r2["знаменатель_без_наших_лимитов"] == 1,
        r2["знаменатель_без_наших_лимитов"])
    chk("доля совпадений не пострадала", r2["доля_совпадений"] == 1.0, r2["доля_совпадений"])

    # запись вне покрытия свечей -> нет_курса, не выдуманная цена
    r3 = сверить([dict(зап, id="D", createAt=9_000_000, skipReason=None)], свечи_t, порог=2.0)
    chk("вне покрытия курса -- нет_курса", len(r3["нет_курса"]) == 1, r3["нет_курса"])
    chk("без курса покупки не было", r3["по_кодам_наш"].get(BD.КОД_НЕТ_КУРСА) == 1,
        r3["по_кодам_наш"])

    # различающая сила порога
    def зп(i, amt, sr):
        return {"id": i, "createAt": 1500_000, "configName": "BATCH-5", "skipReason": sr,
                 "state": "x",
                 "follow": {"wallet": "W",
                             "send": {"info": {"contract": BD.USDC, "decimals": 6},
                                       "amount": str(amt)},
                             "receive": {"info": {"contract": "M" + i}, "amount": "1"}}}
    г = граница([зп("a", 398_000000, "TARGET_AMOUNT_OUT_OF_RANGE"),
                  зп("b", 402_000000, None)], свечи_t, 2.0)
    chk("верх класса «мало» ниже порога", г["верхняя_граница_класса_мало"] == 1.99,
        г["верхняя_граница_класса_мало"])
    chk("низ класса «хватает» не ниже порога", г["нижняя_граница_класса_хватает"] == 2.01,
        г["нижняя_граница_класса_хватает"])
    chk("порог лежит между классами", г["порог_лежит_между_классами"] is True)
    chk("узкий зазор посчитан", abs(г["зазор_между_классами"] - 0.02) < 1e-9,
        г["зазор_между_классами"])
    г2 = граница([зп("a", 900_000000, "TARGET_AMOUNT_OUT_OF_RANGE"),
                   зп("b", 402_000000, None)], свечи_t, 2.0)
    chk("перепутанные классы не объявляются подтверждением порога",
        г2["порог_лежит_между_классами"] is False, г2["порог_лежит_между_классами"])
    chk("в таком случае это прямо сказано", "НЕ разделены" in г2["note"], г2["note"])

    прошло = sum(1 for _, ок, _ in проверки if ок)
    for имя, ок, факт in проверки:
        print(f"  [{'ok  ' if ок else 'ПЛОХО'}] {имя}" + (f" -- {факт}" if not ок else ""))
    print(f"самопроверка сверки: {прошло}/{len(проверки)} пройдено")
    return 0 if прошло == len(проверки) else 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--snapshot", default=str(СНИМОК))
    p.add_argument("--min-sol", type=float, default=2.0)
    a = p.parse_args()
    if a.self_test:
        return self_test()
    отчёт = прогнать(Path(a.snapshot), порог=a.min_sol)
    ВЫХОД.parent.mkdir(parents=True, exist_ok=True)
    ST.atomic_write_json(ВЫХОД, отчёт)
    краткое = {k: v for k, v in отчёт.items() if k != "прогоны"}
    пр = отчёт["прогоны"]["размер_раньше_докупки"]
    краткое["итог_лучшего_прогона"] = {
        "всего_сигналов": пр["всего"],
        "знаменатель_без_наших_лимитов": пр["знаменатель_без_наших_лимитов"],
        "расхождений": пр["расхождений"],
        "доля_совпадений": пр["доля_совпадений"],
        "dbot_купил_мы_нет": len(пр["dbot_купил_мы_нет"]),
        "мы_купили_dbot_нет": len(пр["мы_купили_dbot_нет"]),
        "пропущено_нашим_лимитом_dbot_купил": len(пр["пропущено_нашим_лимитом_dbot_купил"]),
        "размер_независимо": пр["размер"] | {"расхождения": len(пр["размер"]["расхождения"])},
        "нет_курса": len(пр["нет_курса"]),
        "по_кодам_наш": пр["по_кодам_наш"],
        "по_кодам_dbot": пр["по_кодам_dbot"],
    }
    print(json.dumps(краткое, ensure_ascii=False, indent=2))
    print(f"\nполный отчёт: {ВЫХОД.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
