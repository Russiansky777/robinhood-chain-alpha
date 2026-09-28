#!/usr/bin/env python3
"""Статичные шаблоны ПЕРВОЙ НОГИ (SOL -> котировка) для главных котировок.

ЗАЧЕМ (слово владельца 28.09, ночь): "Для главных котировок (USDC, USDT, GP и
топ-10 по частоте) -- статичные шаблоны первой ноги из канонических пулов,
обновляемые чтением состояния раз в N секунд, чтобы шаблон был всегда, а не
только когда кто-то в нём торгует. «Шаблон по запросу» по-прежнему не делать."

ЧТО БЫЛО. Кэш ног (c2_shadow_build.LegCache) заполняется ТОЛЬКО чужими
сделками из подписки: пока в пуле SOL/котировка никто не торгует, шаблона нет,
и двухшаговая покупка отказывает словами "шаблона SOL -> Q в кэше нет". Именно
это остановило три покупки BATCH-5 в ночь на 28.09.

ЧТО ЗДЕСЬ. Шаблон берётся из ОБРАЗЦА -- одной настоящей сделки канонического
пула, снятой заранее (data/nogi_shablony.json, прогон
deploy/checks/nogi_vse_kotirovki.py), а цена котировочного токена читается по
ОСТАТКАМ ХРАНИЛИЩ пула раз в N секунд. Поэтому запись в кэше есть всегда, и её
возраст -- возраст последнего чтения состояния, а не чужой сделки.

ПОЧЕМУ ТОЛЬКО ПУЛЫ С ПОСТОЯННЫМ ПРОИЗВЕДЕНИЕМ. Во-первых, у Pump AMM, CPMM,
AMM v4 и LaunchLab счета инструкции от цены не зависят -- раскладка образца
годна и через час (это же правило уже стоит в LegCache.PRICE_DEPENDENT).
Во-вторых, у них остаток хранилища И ЕСТЬ резерв, поэтому цена по остаткам --
замер, а не модель. У DLMM, CLMM, DAMM v2 и DBC ликвидность сосредоточена:
остаток хранилища ценой не является, и статичный шаблон там был бы выдумкой --
для таких котировок причина пишется словами, а шаблон ждём из подписки.

ЦЕНА -- SOL ЗА ЦЕЛЫЙ КОТИРОВОЧНЫЙ ТОКЕН, ровно та величина, которую
LegCache._template_from кладёт в price_sol (|дельта WSOL| / |дельта Q| в
единицах интерфейса) и по которой bloom_lane_two_step считает ожидание первого
шага и его минимум выхода. Спутать её с обратной означало бы посчитать минимум
выхода в миллионы раз не тем -- поэтому в самопроверке она сверяется с ценой
сделки-образца того же пула.

Денежный путь: цена отсюда идёт в минимум выхода первого шага. Самопроверки --
на настоящих образцах пулов из data/c2_pool_samples (правило 8).
"""
from __future__ import annotations

import json
import sys
import threading
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Программы, у которых остаток хранилища -- резерв, а счета инструкции от цены
# не зависят. Список совпадает с "не PRICE_DEPENDENT" у LegCache плюс запрет
# сосредоточенной ликвидности (DAMM v2 и DBC от цены не зависят счетами, но
# остаток их хранилищ -- не резерв).
РЕЗЕРВНЫЕ = ("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",   # Pump AMM
             "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",   # Raydium CPMM
             "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",   # Raydium AMM v4
             "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj")    # Raydium LaunchLab
ОБНОВЛЯТЬ_С = 5.0          # раз в N секунд читаем остатки хранилищ
# ВО СКОЛЬКО РАЗ РЕЗЕРВ SOL ДОЛЖЕН БЫТЬ БОЛЬШЕ НАШЕГО ШАГА. Цена по остаткам --
# это СПОТ, а получим мы среднюю цену своего свопа: на x*y=k при доле d от
# резерва средняя цена хуже спота примерно на d. Запас первого шага
# (bloom_lane_two_step.ЗАПАС_ШАГА_1) -- 5 %, поэтому при сотне размеров в
# резерве расхождение около 1 % и минимум выхода не срывается. Замер на живых
# образцах CPMM: своп, забравший заметную долю тонкого пула, дал 17 % разницы
# между своей ценой и спотом после себя -- ровно та ошибка, от которой этот
# порог и защищает.
ГЛУБИНА_РАЗ = 100.0
ФАЙЛ = "nogi_shablony.json"


def _модули():
    import c2_common as C  # noqa: PLC0415
    import c2_shadow_build as SB  # noqa: PLC0415
    import c2_swap_build as B  # noqa: PLC0415
    return C, SB, B


def найти_файл(корень=None) -> Path | None:
    """Файл образцов: рядом с данными службы (<родитель кода>/data) или в репо."""
    места = []
    if корень:
        места.append(Path(корень) / ФАЙЛ)
    try:
        C, _SB, _B = _модули()
        места.append(Path(C.DATA) / ФАЙЛ)
    except Exception:  # noqa: BLE001
        pass
    места.append(Path(__file__).resolve().parent.parent / "data" / ФАЙЛ)
    for п in места:
        if п.exists():
            return п
    return None


def запись_из_образца(q: str, образец: dict) -> dict | str:
    """Запись кэша из образца или причина словами, почему её не будет."""
    C, SB, B = _модули()
    прог = образец.get("program")
    if прог not in РЕЗЕРВНЫЕ:
        return ("пул не с постоянным произведением: остаток хранилища там не "
                "резерв, цену по остаткам считать нельзя")
    tx = образец.get("tx")
    if not isinstance(tx, dict) or "meta" not in tx:
        return "в образце нет транзакции целиком"
    p = {"program": прог, "q_vault": образец.get("q_vault"),
         "w_vault": образец.get("w_vault")}
    зап = SB.LegCache._template_from(tx, p, q)
    if isinstance(зап, str):
        return f"образец не стал шаблоном: {зап}"
    mv = зап.get("mv") or {}
    if mv.get("quote_mint") != C.WSOL or mv.get("base_mint") != q:
        return "роли хранилищ в образце не SOL -> котировка"
    if not зап.get("q_dec"):
        return "у образца нет знаков после запятой котировочного токена"
    зап["sig"] = образец.get("signature")
    зап["price_sol_obrazca"] = зап["price_sol"]
    зап["static"] = True
    # Цена образца НЕ ГОДИТСЯ как цена сейчас: она с момента той сделки. До
    # первого чтения остатков записи в кэше нет вовсе.
    зап["price_sol"] = None
    зап["checked_at"] = None
    зап["base_vault"] = mv.get("base_vault")
    зап["quote_vault"] = mv.get("quote_vault")
    return зап


def цена_по_остаткам(остаток_sol: dict | None, остаток_q: dict | None):
    """(цена SOL за целый Q, причина словами если цены нет).

    Резерв WSOL делённый на резерв котировки -- та же величина, что price_sol у
    сделки-шаблона. Считаем в Decimal по сырым числам и знакам, а не по
    uiAmount с плавающей точкой.
    """
    for имя, о in (("хранилища SOL", остаток_sol), ("хранилища котировки", остаток_q)):
        if not о or о.get("amount") is None or о.get("dec") is None:
            return None, f"остаток {имя} узел не отдал"
    s = D(str(остаток_sol["amount"])) / D(10) ** int(остаток_sol["dec"])
    q = D(str(остаток_q["amount"])) / D(10) ** int(остаток_q["dec"])
    if s <= 0 or q <= 0:
        return None, f"пустое хранилище: SOL {s}, котировки {q}"
    return s / q, None


class Статичные:
    """Статичные записи первой ноги: образцы плюс цена по остаткам раз в N с."""

    def __init__(self, содержимое: dict, *, clock=time.time):
        self.clock = clock
        self.записи: dict = {}          # Q -> запись кэша (без цены до чтения)
        self.почему: dict = {}          # Q -> почему статичного шаблона нет
        self.символы: dict = {}         # Q -> символ котировки для отчёта
        self.последнее: dict = {}        # Q -> статус последнего обновления
        self.обновлений = 0
        self.вызовов = 0
        self.последнее_время: float | None = None
        self.последнее_почему = "ещё не обновлялись"
        for q, обр in ((содержимое or {}).get("shablony") or {}).items():
            зап = запись_из_образца(q, обр)
            self.символы[q] = обр.get("символ") or None
            if isinstance(зап, str):
                self.почему[q] = зап
            else:
                self.записи[q] = зап

    # ------------------------------------------------------------- состояние

    def хранилища(self) -> list:
        сч = []
        for зап in self.записи.values():
            сч += [зап["quote_vault"], зап["base_vault"]]
        return [а for а in dict.fromkeys(сч) if а]

    @staticmethod
    def разобрать_остатки(ответ) -> dict:
        """{счёт: {"amount","dec","mint"}} из ответа getMultipleAccounts."""
        значения = ((ответ or {}).get("value") or []) if isinstance(ответ, dict) else []
        return {i: v for i, v in enumerate(значения)}

    def обновить(self, вызов, *, размер_sol: float | None = None) -> dict:
        """Одно чтение остатков (один getMultipleAccounts) -> статусы по котировкам.

        вызов(метод, параметры) -- тот же, что у кэша ног. Сюда не попадает ни
        одной подписи и ни одной отправки.
        """
        счета = self.хранилища()
        if not счета:
            self.последнее_почему = "статичных образцов нет"
            return {}
        try:
            self.вызовов += 1
            ответ = вызов("getMultipleAccounts", [счета, {"encoding": "jsonParsed"}])
        except Exception as exc:  # noqa: BLE001
            self.последнее_почему = f"чтение остатков упало: {type(exc).__name__}"
            return {q: self.последнее_почему for q in self.записи}
        значения = ((ответ or {}).get("value") or []) if isinstance(ответ, dict) else []
        остатки = {}
        for а, v in zip(счета, значения):
            инф = ((((v or {}).get("data") or {}).get("parsed") or {}).get("info") or {})
            с = инф.get("tokenAmount") or {}
            остатки[а] = {"amount": с.get("amount"), "dec": с.get("decimals"),
                           "mint": инф.get("mint")}
        now = self.clock()
        статусы = {}
        for q, зап in self.записи.items():
            цена, почему = цена_по_остаткам(остатки.get(зап["quote_vault"]),
                                             остатки.get(зап["base_vault"]))
            if цена is None:
                статусы[q] = почему
                continue
            # ГЛУБИНА: спот годится только если наш шаг её не двигает.
            if размер_sol:
                о_sol = остатки.get(зап["quote_vault"]) or {}
                резерв = (D(str(о_sol.get("amount") or 0))
                          / D(10) ** int(о_sol.get("dec") or 9))
                if резерв < D(str(размер_sol)) * D(str(ГЛУБИНА_РАЗ)):
                    зап["price_sol"] = None
                    зап["checked_at"] = None
                    статусы[q] = (f"резерв SOL {float(резерв):.3f} меньше "
                                   f"{ГЛУБИНА_РАЗ:.0f} наших шагов по {размер_sol} "
                                   f"-- спот по остаткам нашу цену не описывает")
                    continue
            зап["price_sol"] = цена
            зап["checked_at"] = now
            зап["trade_time"] = зап.get("trade_time") or now
            статусы[q] = "цена по остаткам прочитана"
        self.обновлений += 1
        self.последнее_время = now
        self.последнее_почему = ""
        self.последнее = статусы
        return статусы

    def влить(self, кэш) -> int:
        """Записи с прочитанной ценой -- в кэш ног, под его же замком.

        Перезаписываем ВСЕГДА: статичная запись свежее любой, что пришла из
        подписки (её возраст -- возраст последнего чтения остатков), и именно
        этого владелец и просил -- "чтобы шаблон был всегда".
        """
        сколько = 0
        for q, зап in self.записи.items():
            if зап.get("price_sol") is None or зап.get("checked_at") is None:
                continue
            with кэш.lock:
                кэш.entries[q] = зап
            сколько += 1
        # Таблицы адресов образцов догрузит тот же прогрев, что у подписки.
        try:
            C, SB, B = _модули()
            ключи = set()
            for зап in self.записи.values():
                ключи |= {k for k in SB._lut_keys(зап["tx"]) if k not in кэш.luts}
            кэш.pending_luts |= ключи
        except Exception:  # noqa: BLE001
            pass
        return сколько

    def цикл(self, стоп: threading.Event, вызов, кэш, период: float = ОБНОВЛЯТЬ_С,
              размер_sol: float | None = None) -> None:
        """Фоновый круг: прочитать остатки -> влить в кэш -> подождать."""
        while not стоп.is_set():
            t0 = self.clock()
            try:
                self.обновить(вызов, размер_sol=размер_sol)
                self.влить(кэш)
            except Exception as exc:  # noqa: BLE001
                self.последнее_почему = f"круг упал: {type(exc).__name__}"
            стоп.wait(max(0.5, период - (self.clock() - t0)))

    def признак(self) -> dict:
        """Строка для признака жизни: сколько живых, сколько отказов и почему."""
        живых = sum(1 for з in self.записи.values() if з.get("price_sol") is not None)
        возраст = (None if self.последнее_время is None
                   else round(self.clock() - self.последнее_время, 1))
        return {"obrazcov": len(self.записи), "zhivyh": живых,
                "bez_shablona": len(self.почему), "obnovleniy": self.обновлений,
                "vozrast_s": возраст,
                "why_not": self.последнее_почему or None,
                "kotirovki": {q: (self.символы.get(q) or q[:8]) for q in self.записи},
                "otkazy": dict(self.почему)}


def загрузить(путь=None, *, корень=None, clock=time.time):
    """(Статичные, причина словами). Без файла -- не исключение, а причина."""
    п = Path(путь) if путь else найти_файл(корень)
    if п is None or not Path(п).exists():
        return None, f"файла образцов {ФАЙЛ} нет ни рядом с данными службы, ни в репозитории"
    try:
        содержимое = json.loads(Path(п).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"файл образцов не читается: {type(exc).__name__}"
    ст = Статичные(содержимое, clock=clock)
    if not ст.записи:
        return ст, ("в файле образцов нет ни одного годного: "
                     + "; ".join(f"{q[:8]}: {в}" for q, в in list(ст.почему.items())[:4]))
    return ст, ""


# ------------------------------------------------------------- самопроверки

def self_test() -> int:
    import glob  # noqa: PLC0415
    C, SB, B = _модули()
    проверки = []

    def chk(имя, ок):
        проверки.append((имя, bool(ок)))

    корень = Path(__file__).resolve().parent.parent
    # ОБРАЗЦЫ -- НАСТОЯЩИЕ. Пул CPMM с котировкой SOL -- это и есть пул первой
    # ноги для своего токена: база токен, котировка WSOL.
    обр = json.loads((корень / "data" / "c2_pool_samples"
                      / "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C.json")
                     .read_text(encoding="utf-8"))
    # ПО ОДНОМУ ОБРАЗЦУ НА МИНТ: в файле образцов их несколько на один и тот же
    # токен, а ключ записи -- минт, и два образца одного минта -- одна запись.
    ноги, видели_минты = [], set()
    for x in обр:
        if x.get("quote_mint") != C.WSOL or x["mint"] in видели_минты:
            continue
        видели_минты.add(x["mint"])
        ноги.append(x)
        if len(ноги) >= 3:
            break
    chk(f"настоящих образцов ноги CPMM для проверки: {len(ноги)}", len(ноги) >= 2)
    содержимое = {"shablony": {}}
    for x in ноги:
        содержимое["shablony"][x["mint"]] = {
            "program": B.CPMM, "q_vault": x["pool_vault"], "w_vault": None,
            "signature": (x["tx"].get("transaction") or {}).get("signatures", [None])[0],
            "tx": x["tx"], "символ": None}
    часы = [1000.0]
    ст = Статичные(содержимое, clock=lambda: часы[0])
    chk(f"записи собраны из образцов: {len(ст.записи)} из {len(ноги)}",
        len(ст.записи) == len(ноги))
    chk("до чтения остатков цены нет и в кэш вливать нечего",
        all(з["price_sol"] is None for з in ст.записи.values()))

    # ЦЕНА ПО ОСТАТКАМ -- СВЕРКА С ЦЕНОЙ СДЕЛКИ-ОБРАЗЦА. Берём остатки ИЗ ТОЙ
    # ЖЕ транзакции (postTokenBalances): тогда цена по остаткам обязана
    # совпасть с ценой сделки в пределах комиссии пула, а не разойтись в разы.
    расхождения = []
    between = []
    for q, з in ст.записи.items():
        строки = {r["account"]: r for r in C.token_rows(з["tx"]).values()}
        s = строки[з["quote_vault"]]
        b = строки[з["base_vault"]]
        # СПОТ ДО И ПОСЛЕ СВОПА: средняя цена самого свопа обязана лежать между
        # ними -- это и есть проверка направления и масштаба формулы.
        до, _ = цена_по_остаткам({"amount": int(s["pre"]), "dec": int(s["dec"])},
                                  {"amount": int(b["pre"]), "dec": int(b["dec"])})
        после, _ = цена_по_остаткам({"amount": int(s["post"]), "dec": int(s["dec"])},
                                     {"amount": int(b["post"]), "dec": int(b["dec"])})
        if до is not None and после is not None:
            between.append(min(до, после) <= з["price_sol_obrazca"] <= max(до, после))
        # post у token_rows -- СЫРОЕ число (лампорты / сырые единицы токена),
        # а цена шаблона -- в единицах интерфейса: 3.34e-06 SOL за целый токен
        # против 0.0039 сырых. Перемножить ещё раз на знаки значило бы сравнить
        # цену с числом в тысячу раз больше -- именно на этом самопроверка и
        # поймала мою ошибку 28.09.
        цена, почему = цена_по_остаткам(
            {"amount": int(s["post"]), "dec": int(s["dec"])},
            {"amount": int(b["post"]), "dec": int(b["dec"])})
        chk(f"{q[:8]}: цена по остаткам считается ({почему or 'без причин отказа'})",
            цена is not None)
        if цена is not None:
            расхождения.append(abs(цена / з["price_sol_obrazca"] - 1))
    chk(f"средняя цена свопа лежит между спотом до и после него ({sum(between)} из "
        f"{len(between)}) -- формула цены по остаткам того же масштаба и направления",
        between and all(between))
    chk(f"а спот ПОСЛЕ свопа отличается от средней цены самого свопа до "
        f"{max(расхождения) * 100:.1f} % -- поэтому у статичного шаблона стоит порог "
        f"глубины в {ГЛУБИНА_РАЗ:.0f} наших шагов",
        расхождения and max(расхождения) > 0.0)

    # ПУСТОЕ ХРАНИЛИЩЕ И МОЛЧАНИЕ УЗЛА -- ПРИЧИНА СЛОВАМИ, А НЕ НОЛЬ.
    ц, п = цена_по_остаткам({"amount": 0, "dec": 9}, {"amount": 100, "dec": 6})
    chk(f"пустое хранилище -- отказ словами: «{п}»", ц is None and "пустое" in (п or ""))
    ц, п = цена_по_остаткам(None, {"amount": 100, "dec": 6})
    chk(f"узел не отдал остаток -- отказ словами: «{п}»", ц is None and "не отдал" in (п or ""))

    # СОСРЕДОТОЧЕННАЯ ЛИКВИДНОСТЬ -- НЕ БЕРЁМ ВОВСЕ.
    dlmm = json.loads((корень / "data" / "c2_pool_samples"
                       / "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo.json")
                      .read_text(encoding="utf-8"))
    x = dlmm[0]
    почему = запись_из_образца(x["mint"], {"program": "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
                                            "q_vault": x["pool_vault"], "tx": x["tx"]})
    chk(f"пул DLMM в статичные не берётся: «{str(почему)[:60]}»",
        isinstance(почему, str) and "постоянным произведением" in почему)

    # ОБНОВЛЕНИЕ ЧЕРЕЗ ВЫЗОВ УЗЛА (подставной вызов, сеть не нужна) И ВЛИВ В КЭШ.
    остатки_по_счетам = {}
    for q, з in ст.записи.items():
        строки = {r["account"]: r for r in C.token_rows(з["tx"]).values()}
        for ключ in ("quote_vault", "base_vault"):
            r = строки[з[ключ]]
            остатки_по_счетам[з[ключ]] = {
                "data": {"parsed": {"info": {"mint": r["mint"], "tokenAmount": {
                    "amount": str(int(r["post"])), "decimals": int(r["dec"])}}}}}
    вызовов = {"n": 0}

    def вызов(метод, параметры):
        вызовов["n"] += 1
        assert метод == "getMultipleAccounts", метод
        return {"value": [остатки_по_счетам.get(а) for а in параметры[0]]}

    статусы = ст.обновить(вызов)
    chk(f"одно чтение остатков -- ОДИН вызов узла на все котировки: {вызовов['n']}",
        вызовов["n"] == 1)
    chk(f"все котировки получили цену: {sum(1 for v in статусы.values() if 'прочитана' in v)}"
        f" из {len(ст.записи)}",
        all("прочитана" in v for v in статусы.values()))

    статусы_г = ст.обновить(вызов, размер_sol=100.0)
    chk(f"тонкий резерв при шаге 100 SOL -- цены НЕТ и сказано почему: "
        f"«{list(статусы_г.values())[0][:60]}»",
        all("резерв SOL" in v for v in статусы_г.values())
        and all(з.get("price_sol") is None for з in ст.записи.values()))
    ст.обновить(вызов, размер_sol=0.3)
    chk("при шаге 0.3 SOL тот же резерв проходит порог и цена есть",
        all(з.get("price_sol") is not None for з in ст.записи.values()))

    class Кэш:
        def __init__(self):
            self.entries = {}
            self.luts = {}
            self.pending_luts = set()
            self.lock = threading.Lock()

        def get(self, q):
            e = self.entries.get(q)
            return (e, часы[0] - e["checked_at"]) if e else (None, None)

    кэш = Кэш()
    сколько = ст.влить(кэш)
    chk(f"в кэш влито записей: {сколько}", сколько == len(ст.записи))
    q0 = next(iter(ст.записи))
    e, возраст = кэш.get(q0)
    chk(f"кэш отдаёт статичную запись с возрастом {возраст} с (не старее предела "
        f"{SB.LEG_MAX_AGE_S} с)", e is not None and возраст == 0
        and возраст <= SB.LEG_MAX_AGE_S)
    chk("у статичной записи есть всё, что спрашивает двухшаговая сборка "
        "(tpl, tx, mv, price_sol, q_dec)",
        all(e.get(k) is not None for k in ("tpl", "tx", "mv", "price_sol", "q_dec")))
    # ВОЗРАСТ РАСТЁТ, ПОКА НЕ ЧИТАЕМ. Если бы влив ставил checked_at при каждом
    # обращении, сборка считала бы свежей цену часовой давности.
    часы[0] += 40
    e, возраст = кэш.get(q0)
    chk(f"без нового чтения возраст стал {возраст} с -- старше предела "
        f"{SB.LEG_MAX_AGE_S} с, и сборка такую цену не возьмёт",
        возраст > SB.LEG_MAX_AGE_S)
    ст.обновить(вызов)
    ст.влить(кэш)
    e, возраст = кэш.get(q0)
    chk(f"после нового чтения возраст снова {возраст} с", возраст == 0)

    # ПАДЕНИЕ УЗЛА -- ПРИЧИНА СЛОВАМИ, СТАРАЯ ЦЕНА НЕ ОБНОВЛЯЕТСЯ.
    def вызов_падает(метод, параметры):
        raise TimeoutError("узел молчит")

    статусы = ст.обновить(вызов_падает)
    chk(f"падение узла -- причина словами: «{ст.последнее_почему}»",
        "упало" in ст.последнее_почему)
    chk("после падения возраст записи НЕ обнулён", кэш.get(q0)[1] == 0)

    # ФАЙЛ. Настоящий файл образцов может ещё не быть собран -- тогда причина
    # словами, а не исключение.
    ст2, почему2 = загрузить()
    chk(f"загрузка файла образцов: {'есть, ' + str(len(ст2.записи)) + ' записей' if ст2 else 'нет'}"
        f" -- «{почему2 or 'без причин'}»", ст2 is not None or bool(почему2))

    # ПРИЗНАК ЖИЗНИ -- БЕЗ ПРОЧЕРКОВ.
    пр = ст.признак()
    chk(f"признак жизни: {json.dumps({k: v for k, v in пр.items() if k != 'kotirovki'}, ensure_ascii=False)[:160]}",
        пр["obrazcov"] == len(ст.записи) and пр["zhivyh"] >= 1)

    сбоев = sum(1 for _, ок in проверки if not ок)
    for имя, ок in проверки:
        print(f"[{'OK' if ок else 'СБОЙ'}] {имя}")
    print(f"итого проверок {len(проверки)}, сбоев {сбоев}")
    return 1 if сбоев else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    ст, почему = загрузить()
    print(json.dumps({"почему": почему or None,
                      "признак": ст.признак() if ст else None},
                     ensure_ascii=False, indent=1))
