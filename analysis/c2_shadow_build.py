#!/usr/bin/env python3
"""C2 -> Code-1: теневая сборка покупки на сигнал buy. БЕЗ ОТПРАВКИ.

Для встраивания в детектор «в тени»: вызывать ПАРАЛЛЕЛЬНО с Bloom (в своём
потоке/задаче), не перед ним и не задерживая его. Модуль сам ничего не
отправляет: метода отправки транзакции в нём нет, только
simulateTransaction с sigVerify=false и replaceRecentBlockhash=true.
Ключи кошелька не нужны и не используются -- пользователь транзакции это
АДРЕС нашего кошелька, подпись при симуляции не проверяется.

Интерфейс (всё в одном вызове, исключений наружу не бросает):

    import c2_shadow_build as SB
    res = SB.shadow_build(
        source_tx,            # jsonParsed getTransaction источника (как у детектора)
        source_wallet,        # адрес источника
        mint,                 # купленный минт
        our_wallet,           # адрес нашего кошелька (4s87RRC2...)
        amount_lamports,      # наша трата в SOL, лампорты
        rpc_call,             # функция (method, params) -> result; учёт кредитов -- на стороне вызывающего
        sol_usd=None,         # курс USD/SOL, если детектор его уже знает (для cap_usd)
        spend_sol_equiv=None, # трата источника в SOL-экв (детектор считает её для порога 2 SOL)
        slippage=0.35, cu_units=400_000, cu_price_micro=10_000)

    res -> {"ok", "pool_program", "pool_label", "quote_mint", "supported",
            "build_ms", "sim_ms", "sim_verdict" (would_pass | slippage | balance | other_error),
            "sim_err", "sim_units", "sim_logs_tail",
            "min_out", "expected_out", "min_out_method",
            "cap_usd", "would_skip_cap", "cap_why_not", "why_not"}

Покрытие v1: Pump AMM, Raydium CPMM, Meteora DAMM v2, Raydium Launchlab,
Meteora DLMM, Raydium CLMM -- один шаг, если котировка пула WSOL.

v2 -- два шага (SOL -> Q -> токен) одной транзакцией. ЗАПИСКА ДЛЯ CODE-1
-----------------------------------------------------------------------
Котировка пула источника -- не SOL, а промежуточный токен Q. Кэш держит
шаблон шага 1 (последняя сделка SOL<->Q) только для 4 котировок с долей
сигналов >= 2 % (решение владельца). Все 4 пула -- Meteora DLMM; адреса из
data/c2_leg_pools_2026-09-24.json:

  Q (котировка)                                 хранилище Q (q_vault) -- ПОДПИСЫВАТЬ   хранилище WSOL (w_vault)
  HTmQz7My6MehV7bjhJ6jde8nDND1yvsz68d24LP7YgUQ  6oYfJ1yWCuUfX6Q61AEUwEiF2rzii6t9REwykRmnpsL1  Fns6LdcfqNPox3gxoxp46C5NjZoEXbMcvzw489aUNMmT
  4rkGWJNSUPBcMicXMRAzohEyeJLFG8gUjwiWaz7Pddr3  FGNavW6RiiPe37n6evak1GtoesytosWJs9hEKwF82odD  AUn3Qw5FMAXooFjHsehNfTYGSWXSR39rR2gDRi3eNSoJ
  6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx  3AeDHaaCjxXZ6mZRkPooXjstZG5AFMtNQF7Ld4P6MH9a  3mZ7ftpz1XSNHKNQqk9a4bUvzWp95rC9qXsBC9VAXNPN
  Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh   FNaEXnGP3hUrcJSBXBKzzDRJmitNnVwwQ6pQx6LhAumF  HZgAwbRXeUSEjZL5nERN5mDeXTKiu6ZgbDMy4MoEHcd9

1. Создать кэш (сеть не нужна):
       pools = SB.load_leg_pools()          # 4 пула выше, из data/c2_leg_pools_*.json
       cache = SB.LegCache(pools, rpc_call) # rpc_call -- только для warm_luts и запасного опроса
2. Подписка: 4 адреса q_vault добавить в accountInclude той же
   transactionSubscribe, что у детектора (_подписка_транзакций: processed,
   jsonParsed, full). Каждое уведомление по этим адресам -- в
       cache.ingest(notification)           # сеть НЕ зовёт; уведомление целиком или {"transaction","meta"}
   ingest: своп WSOL->Q -- новый шаблон; продажа Q->WSOL -- тоже (DLMM,
   переворачивается); сделка без свопа (ликвидность, комиссии) --
   подтверждает шаблон; своп, из которого шаблон не собрать, -- шаблон
   НЕ подтверждается и через 30 с даёт отказ.
3. Прогрев таблиц адресов (вне горячего пути, 1 getMultipleAccounts):
       cache.load_luts(SB.warm_lut_keys(pools))   # при старте: 32 таблицы из data/c2_pool_samples/*.json
       cache.warm_luts()                          # периодически: таблицы новых шаблонов из ingest
   Без прогрева горячий путь сам дочитает недостающие таблицы
   (getMultipleAccounts, замер: сборка 82 мс вместо 0.65 мс).
4. На сигнал -- как в v1, плюс leg_cache:
       res = SB.shadow_build(source_tx, source_wallet, mint, our_wallet, amount_lamports,
                             rpc_call, leg_cache=cache, ...)
   res["route"] == "two_hop", leg1_template_age_s, leg1_flipped,
   leg2_amount_in (= ожидаемый выход шага 1 по цене шаблона - 5 %, он же
   минимум шага 1; остаток Q остаётся на кошельке). Шаблон старше 30 с
   (с последнего подтверждения) -- ok=False, why_not «шаблон шага 1 старше
   30 с (N с)». Котировки вне 4 -- why_not «шаблона SOL -> Q в кэше нет».
5. Запасной путь -- опрос (если подписка недоступна):
   LegCache(pools, rpc_call, allow_polling=True) и cache.run(stop_event) в
   своём потоке; без флага refresh_all()/run() бросают RuntimeError.
   Замер на 57 пулах: ~13.7 вызова/мин на пул.
Покрытие по выборке A (прогон 24.09): одни SOL-пулы 26.9 %; все пулы
SOL<->Q в кэше 59.2 %; только 4 котировки >= 2 % дают +58 сигналов (16.8 %).

Минимум: Pump AMM / CPMM -- x*y=k по резервам после сделки источника с
комиссией, калиброванной на его сделке; Launchlab -- кривая на
виртуальных резервах из его TradeEvent; DAMM v2 / DLMM -- не выдаётся
(сосредоточенная ликвидность), симуляция идёт с минимумом 1, и «прошла бы»
там значит только «собрано верно и хватает баланса».

cap_usd = цена сделки источника (spend_sol_equiv / его токены) * sol_usd *
предложение минта (getAccountInfo, 1 вызов). would_skip_cap = cap_usd >
200 000. Нет курса или траты -- cap_usd None с причиной, флаг None.
"""
from __future__ import annotations

import base64
import collections
import inspect
import sys
import threading
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import c2_swap_build as B  # noqa: E402

CAP_SKIP_USD = 200_000
SUPPORTED = {B.PUMP_AMM: "Pump AMM", B.CPMM: "Raydium CPMM", B.DAMM2: "Meteora DAMM v2",
             B.LAUNCHLAB: "Raydium Launchlab", B.DLMM: "Meteora DLMM", B.CLMM: "Raydium CLMM",
             B.BONDING: "Pump.fun bonding curve"}
PRICE_DEPENDENT = {B.DLMM, B.CLMM}    # счета инструкции (бины, тики) зависят от цены
LEG_MAX_AGE_S = 30
LEG_REFRESH_S = 10
LEG1_HAIRCUT = 0.05
TX_OPTS = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": C.TX_VERSION, "commitment": "confirmed"}
FLIP_OK = {B.DLMM}    # CLMM: перевёрнутый шаблон программа отвергла в 9 из 9 симуляций (0x1787)
SIM_OPTS = {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True,
            "commitment": "processed"}


def _labels() -> dict:
    import c2_pool_programs as PP  # noqa: PLC0415
    return PP.labels()


def classify_sim(value: dict) -> dict:
    logs = value.get("logs") or []
    err = value.get("err")
    joined = "\n".join(logs).lower()
    if err is None:
        v = "would_pass"
    elif "slippage" in joined or "exceeded" in joined or "minimum" in joined and "out" in joined:
        v = "slippage"
    elif "insufficient" in joined or ("requiregteviolated" in joined and "program log: left: 0" in joined):
        v = "balance"
    else:
        v = "other_error"
    return {"sim_verdict": v, "sim_err": err, "sim_units": value.get("unitsConsumed"),
            "sim_logs_tail": logs[-6:]}


def cap_usd(source_tx: dict, source_wallet: str, mint: str, spend_sol_equiv, sol_usd,
            rpc_call) -> dict:
    if not spend_sol_equiv or not sol_usd:
        return {"cap_usd": None, "would_skip_cap": None,
                "cap_why_not": "нет траты источника в SOL-экв или курса USD/SOL"}
    tok = C.owner_mint_delta(source_tx, mint).get(source_wallet)
    if not tok or tok <= 0:
        return {"cap_usd": None, "would_skip_cap": None,
                "cap_why_not": "источник не получил токен на свой кошелёк"}
    try:
        info = rpc_call("getAccountInfo", [mint, {"encoding": "jsonParsed"}]) or {}
        p = ((((info.get("value") or {}).get("data") or {}).get("parsed") or {}).get("info") or {})
        supply = D(p["supply"]) / D(10) ** int(p["decimals"])
    except Exception as exc:  # noqa: BLE001
        return {"cap_usd": None, "would_skip_cap": None,
                "cap_why_not": f"предложение минта не получено: {type(exc).__name__}"}
    cap = D(str(spend_sol_equiv)) * D(str(sol_usd)) / tok * supply
    return {"cap_usd": float(round(cap, 2)), "would_skip_cap": cap > CAP_SKIP_USD, "cap_why_not": None}


# ------------------------------------------------------------ кэш шаблонов шага 1

def _lut_accounts(keys: list, vals: list) -> dict:
    from solders.address_lookup_table_account import AddressLookupTableAccount  # noqa: PLC0415
    from solders.pubkey import Pubkey  # noqa: PLC0415
    out = {}
    for k, v in zip(keys, vals):
        addrs = ((((v or {}).get("data") or {}).get("parsed") or {}).get("info") or {}).get("addresses") or []
        if addrs:
            out[k] = AddressLookupTableAccount(Pubkey.from_string(k), [Pubkey.from_string(a) for a in addrs])
    return out


def _lut_keys(tx: dict) -> list:
    return [lk["accountKey"] for lk in (((tx.get("transaction") or {}).get("message") or {})
                                        .get("addressTableLookups") or []) if lk.get("accountKey")]


class LegCache:
    """Шаблоны шага 1 (WSOL -> Q).

    Основной путь -- ingest(tx): транзакции пулов из подписки детектора,
    без сети. Опрос (refresh_all/run) -- запасной путь, только при
    allow_polling=True. Горячий путь только читает get()."""

    def __init__(self, pools: dict, rpc_call=None, *, clock=time.time, allow_polling: bool = False):
        self.pools = pools
        self.rpc_call = rpc_call
        self.clock = clock
        self.allow_polling = allow_polling
        self.pending_luts: set = set()   # таблицы адресов шаблонов, ещё не загруженные (warm_luts)
        self.ingest_stats = collections.Counter()
        self.entries: dict = {}
        self.luts: dict = {}
        self.lock = threading.Lock()
        self.calls = collections.Counter()
        self.last_status: dict = {}
        self.seen: dict = {}       # q -> подписи, уже разобранные (не шаблон)
        self.reject: dict = {}     # q -> Counter причин, почему сделка не стала шаблоном
        self.moved: dict = {}      # подпись -> двигала ли цену (своп)
        self.workers = 8

    def _rpc(self, method, params):
        if self.rpc_call is None:
            raise RuntimeError("LegCache: rpc_call не передан")
        self.calls[method] += 1
        return self.rpc_call(method, params)

    # ---------------------------------------------------- основной путь: подписка

    def ingest(self, tx: dict) -> dict:
        """Транзакция из transactionSubscribe (jsonParsed): уведомление целиком
        ({"signature", "slot", "transaction": {"transaction", "meta"}}) или
        уже развёрнутая {"transaction", "meta"}. Сеть НЕ зовётся.
        -> {Q: статус} по пулам кэша, чьи хранилища есть в транзакции."""
        raw = tx.get("transaction") if isinstance(tx.get("transaction"), dict) and "meta" in tx["transaction"] else tx
        if not isinstance(raw, dict) or "meta" not in raw:
            self.ingest_stats["не транзакция"] += 1
            return {}
        sig = tx.get("signature") or C.first_signature(raw)
        keys = set(C.account_keys(raw))
        now = self.clock()
        out = {}
        for q, p in self.pools.items():
            if p["q_vault"] not in keys:
                continue
            cur = self.entries.get(q)
            ent = self._template_from(raw, p, q)
            if isinstance(ent, dict):
                ent.update(sig=sig, checked_at=now)
                if not ent.get("trade_time"):
                    ent["trade_time"] = now       # в уведомлении подписки blockTime нет
                with self.lock:
                    self.entries[q] = ent
                self.pending_luts |= {k for k in _lut_keys(raw) if k not in self.luts}
                out[q] = "новый шаблон" + (" (из продажи, перевёрнут)" if ent["tpl"].get("flipped") else "")
            else:
                self.reject.setdefault(q, collections.Counter())[ent] += 1
                ev = C.pool_event(raw, {"pool_vault": p["q_vault"], "quote_vault": p.get("w_vault"),
                                        "quote_mint": C.WSOL}) if p.get("w_vault") else {"kind": "swap"}
                if cur and (p["program"] not in PRICE_DEPENDENT or ev.get("kind") not in ("swap", "absent")):
                    cur["checked_at"] = now
                    out[q] = "не своп / счета не зависят от цены -- шаблон подтверждён"
                else:
                    out[q] = "своп без шаблона -- шаблон не подтверждён" if cur else "шаблона нет"
            self.ingest_stats[out[q]] += 1
        return out

    def warm_luts(self) -> int:
        """Загрузить таблицы адресов шаблонов, накопленные ingest() (1 вызов
        getMultipleAccounts). Вызывать ВНЕ горячего пути."""
        need = sorted(self.pending_luts - set(self.luts))
        if need:
            self.load_luts(need)
        self.pending_luts -= set(self.luts)
        return len(need)

    def load_luts(self, keys: list, rpc=None) -> None:
        need = [k for k in dict.fromkeys(keys) if k not in self.luts]
        if not need:
            return
        vals = ((rpc or self._rpc)("getMultipleAccounts", [need, {"encoding": "jsonParsed"}]) or {}).get("value") or []
        got = _lut_accounts(need, vals)
        with self.lock:
            self.luts.update(got)

    def refresh_pool(self, q: str) -> str:
        if not self.allow_polling:
            raise RuntimeError("LegCache: опрос выключен (allow_polling=False) -- обновление через ingest()")
        p = self.pools[q]
        sigs = self._rpc("getSignaturesForAddress", [p["q_vault"], {"limit": 10, "commitment": "confirmed"}]) or []
        ok = [x["signature"] for x in sigs if x.get("err") is None]
        now = self.clock()
        cur = self.entries.get(q)
        if cur and ok and ok[0] == cur["sig"]:
            cur["checked_at"] = now
            return "без изменений"
        seen = self.seen.setdefault(q, collections.deque(maxlen=200))
        new = ok[:ok.index(cur["sig"])] if cur and cur["sig"] in ok else ok
        limit = 6 if cur else 10               # первое заполнение -- глубже
        moved = bool(cur) and (cur["sig"] not in ok or len(new) > limit)
        for sg in new[:limit]:
            if sg in seen:
                moved = moved or self.moved.get(sg, True)
                continue
            try:
                tx = self._rpc("getTransaction", [sg, TX_OPTS])
            except Exception:  # noqa: BLE001 -- одна нечитаемая tx не рвёт обновление пула
                seen.append(sg)
                self.calls["tx_errors"] += 1
                moved = True
                continue
            if not tx:
                moved = True
                continue
            seen.append(sg)
            ent = self._template_from(tx, p, q)
            if isinstance(ent, str):
                self.reject.setdefault(q, collections.Counter())[ent] += 1
                # цена двигается только свопом: по хранилищам пула
                ev = C.pool_event(tx, {"pool_vault": p["q_vault"], "quote_vault": p.get("w_vault"),
                                       "quote_mint": C.WSOL}) if p.get("w_vault") else {"kind": "swap"}
                self.moved[sg] = ev.get("kind") in ("swap", "absent")
                moved = moved or self.moved[sg]
                continue
            self.load_luts(_lut_keys(tx))
            ent.update(sig=sg, checked_at=now)
            with self.lock:
                self.entries[q] = ent
            return "новый шаблон" + (" (из продажи, перевёрнут)" if ent["tpl"].get("flipped") else "")
        if cur and p["program"] not in PRICE_DEPENDENT:
            cur["checked_at"] = now    # счета от цены не зависят -- шаблон годен
            return "новых покупок нет, шаблон годен (счета не зависят от цены)"
        if cur and not moved:
            cur["checked_at"] = now    # новые сделки -- не свопы (ликвидность, комиссии): цена та же
            return "новые сделки не свопы, шаблон годен"
        return "был своп без шаблона -- шаблон не подтверждён" if cur else "шаблона нет"

    @staticmethod
    def _template_from(tx: dict, p: dict, q: str):
        """Запись кэша или строка -- причина, почему сделка не шаблон."""
        if (tx.get("meta") or {}).get("err") is not None:
            return "сделка неуспешна"
        tpl = B.extract_template(tx, p["program"], p["q_vault"])
        if not tpl.get("ok"):
            return f"шаблон: {tpl.get('why_not')}"
        mv = B.mints_and_vaults(tpl, tx)
        if mv.get("quote_mint") == q and mv.get("base_mint") == C.WSOL:
            if p["program"] not in FLIP_OK:
                return "продажа Q -> SOL (переворот для этого типа выключен)"
            tpl = B.flip_template(tpl, C.WSOL)
            mv = B.mints_and_vaults(tpl, tx)
        if mv.get("quote_mint") != C.WSOL or mv.get("base_mint") != q:
            return "роли хранилищ не восстановились"
        ev = C.pool_event(tx, {"pool_vault": mv["base_vault"], "quote_vault": mv["quote_vault"],
                               "quote_mint": C.WSOL})
        if ev.get("kind") != "swap":
            return f"по хранилищам не своп: {ev.get('kind')}"
        dec = next((r["dec"] for r in C.token_rows(tx).values() if r["account"] == mv["base_vault"]), None)
        return {"tpl": tpl, "tx": tx, "mv": mv, "price_sol": ev["price"], "q_dec": dec,
                "program": p["program"], "trade_time": tx.get("blockTime")}

    def refresh_all(self) -> dict:
        if not self.allow_polling:
            raise RuntimeError("LegCache: опрос выключен (allow_polling=False) -- обновление через ingest()")
        from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

        def one(q):
            try:
                return q, self.refresh_pool(q)
            except Exception as exc:  # noqa: BLE001
                return q, f"сбой: {type(exc).__name__}: {str(exc)[:100]}"
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            st = dict(ex.map(one, list(self.pools)))
        self.last_status = st
        return st

    def run(self, stop: threading.Event, period: float = LEG_REFRESH_S) -> None:
        while not stop.is_set():
            t0 = self.clock()
            self.refresh_all()
            stop.wait(max(0.0, period - (self.clock() - t0)))

    def get(self, q: str):
        with self.lock:
            e = self.entries.get(q)
        if not e:
            return None, None
        return e, self.clock() - e["checked_at"]


QUOTES_GE_2PCT = ("HTmQz7My6MehV7bjhJ6jde8nDND1yvsz68d24LP7YgUQ", "4rkGWJNSUPBcMicXMRAzohEyeJLFG8gUjwiWaz7Pddr3",
                  "6GmAFSYs4gk3FDao5FzzySQpPZaWsa4rUJHacpMpUNgx", "Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh")


def load_leg_pools(path: str | None = None, quotes=QUOTES_GE_2PCT) -> dict:
    """{Q: {"program", "q_vault", "w_vault", "sol_depth"}} из data/c2_leg_pools_<дата>.json."""
    import json  # noqa: PLC0415
    f = Path(path) if path else sorted(C.DATA.glob("c2_leg_pools_2*.json"))[-1]
    pools = json.loads(f.read_text(encoding="utf-8"))["pools"]
    return {q: pools[q] for q in quotes if q in pools}


def warm_lut_keys(pools: dict, top: int = 32) -> list:
    """Таблицы адресов для прогрева: те, что встречаются в настоящих
    транзакциях образцов, где есть хранилища этих пулов, и в транзакциях
    с котировкой Q (шаг 2), по частоте. Только чтение файлов репо."""
    import json  # noqa: PLC0415
    vaults = {p["q_vault"] for p in pools.values()}
    cnt = collections.Counter()
    for f in sorted(B.SAMPLES_DIR.glob("*.json")):
        for x in json.loads(f.read_text(encoding="utf-8")):
            tx = x.get("tx") or {}
            if x.get("quote_mint") in pools or vaults & set(C.account_keys(tx)):
                cnt.update(_lut_keys(tx))
    return [k for k, _ in cnt.most_common(top)]


def _two_hop(res: dict, source_tx: dict, tpl2: dict, q: str, our_wallet: str, amount_lamports: int,
             rpc_call, leg_cache: LegCache, slippage: float, cu_units: int, cu_price_micro: int,
             t0: float):
    """Сборка SOL -> Q -> токен одной транзакцией. None -- отказ (причина в res)."""
    from solders.hash import Hash  # noqa: PLC0415
    from solders.message import MessageV0  # noqa: PLC0415
    from solders.pubkey import Pubkey  # noqa: PLC0415
    from solders.signature import Signature  # noqa: PLC0415
    from solders.transaction import VersionedTransaction  # noqa: PLC0415
    e, age = leg_cache.get(q)
    if e is None:
        res["why_not"] = "котировка не SOL: шаблона SOL -> Q в кэше нет"
        return None
    res.update(route="two_hop", leg1_pool_program=e["program"], leg1_template_age_s=round(age, 1),
               leg1_flipped=bool(e["tpl"].get("flipped")),
               leg1_trade_age_s=round(time.time() - e["trade_time"], 1) if e.get("trade_time") else None)
    if age > LEG_MAX_AGE_S:
        res["why_not"] = f"шаблон шага 1 старше {LEG_MAX_AGE_S} с ({age:.0f} с)"
        return None
    if not e.get("q_dec") or not e.get("price_sol"):
        res["why_not"] = "у шаблона шага 1 нет цены"
        return None
    q_exp = D(amount_lamports) / D(10) ** 9 / e["price_sol"] * D(10) ** int(e["q_dec"])
    leg2_in = int(q_exp * D(1 - LEG1_HAIRCUT))
    if leg2_in <= 0:
        res["why_not"] = "ожидаемый выход шага 1 нулевой"
        return None
    mv1, mv2 = e["mv"], B.mints_and_vaults(tpl2, source_tx)
    mo = B.min_out_from_reserves(tpl2, source_tx, leg2_in, slippage)
    if mo.get("ok"):
        res.update(min_out=mo["min_out"], expected_out=mo["expected_out"], min_out_method="xyk_reserves_after_source")
    else:
        res["min_out_method"] = f"не выдаётся: {mo.get('why_not')}"
    res.update(leg1_min_out=leg2_in, leg2_amount_in=leg2_in)
    w = B.ata(our_wallet, C.WSOL, mv1["quote_program"])
    ixs = [B.cu_limit(cu_units), B.cu_price(cu_price_micro),
           B.ata_idempotent(our_wallet, our_wallet, C.WSOL, mv1["quote_program"]),
           B.ata_idempotent(our_wallet, our_wallet, q, mv1["base_program"]),
           B.ata_idempotent(our_wallet, our_wallet, mv2["base_mint"], mv2["base_program"]),
           B.sol_transfer(our_wallet, w, amount_lamports), B.sync_native(w),
           B.swap_instruction(e["tpl"], e["tx"], our_wallet, amount_lamports, leg2_in),
           B.swap_instruction(tpl2, source_tx, our_wallet, leg2_in, res["min_out"] or 1)]
    miss = [k for k in _lut_keys(e["tx"]) + _lut_keys(source_tx) if k not in leg_cache.luts]
    if miss:
        leg_cache.load_luts(miss, rpc=rpc_call)
        res["hot_lut_calls"] = 1
    keys = list(dict.fromkeys(_lut_keys(e["tx"]) + _lut_keys(source_tx)))
    luts = [leg_cache.luts[k] for k in keys if k in leg_cache.luts]
    msg = MessageV0.try_compile(Pubkey.from_string(our_wallet), ixs, luts, Hash.default())
    raw = bytes(VersionedTransaction.populate(msg, [Signature.default()] * msg.header.num_required_signatures))
    res["build_ms"] = round((time.perf_counter() - t0) * 1000, 3)
    res["tx_size"] = len(raw)
    if len(raw) > 1232:
        res["why_not"] = f"двухшаговая транзакция {len(raw)} байт > 1232"
        return None
    return {"tx_base64": base64.b64encode(raw).decode(), "size": len(raw)}


def shadow_build(source_tx: dict, source_wallet: str, mint: str, our_wallet: str,
                 amount_lamports: int, rpc_call, *, sol_usd=None, spend_sol_equiv=None,
                 slippage: float = 0.35, cu_units: int = 400_000,
                 cu_price_micro: int = 10_000, leg_cache: LegCache | None = None) -> dict:
    res = {"ok": False, "pool_program": None, "pool_label": None, "quote_mint": None,
           "supported": False, "build_ms": None, "sim_ms": None, "sim_verdict": None,
           "sim_err": None, "sim_units": None, "sim_logs_tail": None, "min_out": None,
           "expected_out": None, "min_out_method": None, "why_not": None, "route": "one_hop"}
    try:
        res.update(cap_usd(source_tx, source_wallet, mint, spend_sol_equiv, sol_usd, rpc_call))
    except Exception as exc:  # noqa: BLE001
        res.update(cap_usd=None, would_skip_cap=None, cap_why_not=f"{type(exc).__name__}")
    t0 = time.perf_counter()
    try:
        pool = C.identify_pool(source_tx, source_wallet, mint)
        import c2_pool_programs as PP  # noqa: PLC0415
        prog = (PP.pool_program(source_tx, pool.get("pool_vault"), _labels())["pool_program"]
                if pool.get("pool_vault") else None)
        if not pool["ok"]:
            # У кривой pump.fun котировка НАТИВНАЯ по устройству программы:
            # токенового счёта котировки в её buy нет вовсе (проверено на 5
            # настоящих покупках). Поэтому «неоднозначная котировка» -- когда
            # сделка источника шла роутером и задела USDC -- для кривой не
            # причина отказываться: хранилище пула определено, а котировку
            # угадывать не нужно. Для остальных типов отказ как был.
            if prog != B.BONDING:
                res["why_not"] = f"пул: {pool['why_not']}"
                return res
            pool = dict(pool, ok=True, quote_mint=C.NATIVE_QUOTE, quote_vault="CURVE",
                        quote_why=f"кривая pump.fun: котировка нативная ({pool['why_not']})")
        res.update(pool_program=prog, pool_label=SUPPORTED.get(prog), quote_mint=pool["quote_mint"])
        if prog not in SUPPORTED:
            res["why_not"] = f"тип пула не покрыт сборщиком: {prog}"
            return res
        tpl = B.extract_template(source_tx, prog, pool["pool_vault"])
        if not tpl.get("ok"):
            res["why_not"] = f"шаблон: {tpl.get('why_not')}"
            return res
        mv = B.mints_and_vaults(tpl, source_tx)
        if not mv:
            res["why_not"] = "минты/хранилища пула не восстановились"
            return res
        # Нативный SOL -- такой же один шаг, как WSOL: оборачивать нечего.
        if mv.get("quote_mint") not in (C.WSOL, C.NATIVE_QUOTE):
            if leg_cache is None:
                res["why_not"] = "котировка не SOL: нужен второй шаг (кэш шаблонов не передан)"
                return res
            built = _two_hop(res, source_tx, tpl, mv["quote_mint"], our_wallet, amount_lamports, rpc_call,
                             leg_cache, slippage, 800_000 if cu_units < 800_000 else cu_units,
                             cu_price_micro, t0)
            if built is None:
                return res
            res["supported"] = True
            return _simulate(res, built, rpc_call)
        res["supported"] = True
        mo = B.min_out_from_reserves(tpl, source_tx, amount_lamports, slippage)
        if mo.get("ok"):
            метод = ("launchlab_virtual_reserves" if prog == B.LAUNCHLAB
                     else "pumpfun_curve_after_source" if prog == B.BONDING
                     else "xyk_reserves_after_source")
            res.update(min_out=mo["min_out"], expected_out=mo["expected_out"], min_out_method=метод)
            if prog == B.BONDING:
                res.update(sol_to_curve=mo["sol_to_curve"], max_sol_cost=mo["max_sol_cost"])
        else:
            res["min_out_method"] = f"не выдаётся: {mo.get('why_not')}"
            # На кривой pump.fun минимум -- это САМ АРГУМЕНТ покупки (сколько
            # токенов взять). Без него сборка не «без защиты», а бессмысленна:
            # с min_out=1 транзакция купит один сырой токен и заплатит комиссии.
            # Поэтому здесь отказ, а не сборка на удачу.
            if B.SPECS[prog].get("exact_out"):
                res["why_not"] = f"кривая: минимум не посчитан ({mo.get('why_not')})"
                return res
        built = B.build_buy(tpl, source_tx, user=our_wallet, payer=our_wallet,
                            amount_in=amount_lamports, min_out=res["min_out"] or 1,
                            cu_units=cu_units, cu_price_micro=cu_price_micro, wrap_sol=True)
        res["build_ms"] = round((time.perf_counter() - t0) * 1000, 3)
        res["tx_size"] = built["size"]
    except Exception as exc:  # noqa: BLE001
        res["why_not"] = f"сборка: {type(exc).__name__}: {str(exc)[:160]}"
        return res
    return _simulate(res, built, rpc_call)


def _simulate(res: dict, built: dict, rpc_call) -> dict:
    t1 = time.perf_counter()
    try:
        value = (rpc_call("simulateTransaction", [built["tx_base64"], SIM_OPTS]) or {}).get("value") or {}
        res.update(classify_sim(value))
        res["ok"] = True
    except Exception as exc:  # noqa: BLE001
        res["why_not"] = f"симуляция: {type(exc).__name__}: {str(exc)[:160]}"
    res["sim_ms"] = round((time.perf_counter() - t1) * 1000, 1)
    return res


def self_test() -> int:
    checks = []

    def fake_rpc(method, params):
        if method == "getAccountInfo":
            return {"value": {"data": {"parsed": {"info": {"supply": "1000000000000000", "decimals": 6}}}}}
        if method == "simulateTransaction":
            raw = base64.b64decode(params[0])
            assert len(raw) <= 1232
            assert params[1]["sigVerify"] is False and params[1]["replaceRecentBlockhash"] is True
            return {"value": {"err": None, "logs": ["Program log: ok"], "unitsConsumed": 123456}}
        raise RuntimeError(method)
    n_ok = 0
    tried = 0
    for prog in (B.PUMP_AMM, B.LAUNCHLAB):
        for s in B.load_samples(prog):
            if s.get("quote_mint") != C.WSOL or not s.get("mint"):
                continue
            tried += 1
            r = shadow_build(s["tx"], s["source"], s["mint"], C.EXECUTOR_WALLET, 50_000_000, fake_rpc,
                             sol_usd=115.0, spend_sol_equiv=2.5)
            n_ok += bool(r["ok"] and r["sim_verdict"] == "would_pass" and r["min_out"])
            if tried == 1:
                first = r
    checks.append((f"Pump AMM/Launchlab с котировкой SOL: собрано и «симулировано» {n_ok} из {tried}",
                   tried >= 10 and n_ok == tried))
    # Кривая pump.fun: котировка НАТИВНАЯ, поэтому проверяем отдельно -- это
    # один шаг, минимум считается по кривой, а трата ограничена аргументом.
    pf_n = pf_ok = 0
    for s in B.load_samples(B.BONDING):
        if not s.get("mint"):
            continue
        pf_n += 1
        r = shadow_build(s["tx"], s["source"], s["mint"], C.EXECUTOR_WALLET, 10_000_000, fake_rpc,
                         sol_usd=115.0, spend_sol_equiv=2.5)
        pf_ok += bool(r["ok"] and r["route"] == "one_hop" and r["quote_mint"] == C.NATIVE_QUOTE
                      and r["min_out_method"] == "pumpfun_curve_after_source"
                      and 0 < r["min_out"] < r["expected_out"]
                      and r.get("max_sol_cost") == 10_000_000
                      and 0 < r.get("sol_to_curve", 0) < 10_000_000)
    checks.append((f"кривая pump.fun: один шаг, минимум по кривой, предел траты = наша трата "
                   f"({pf_ok} из {pf_n})", pf_n >= 4 and pf_ok >= pf_n - 1))
    checks.append(("время сборки измерено, мс", first.get("build_ms") is not None and first["build_ms"] < 50))
    checks.append(("cap_usd посчитан и флаг выставлен", first.get("cap_usd") is not None
                   and isinstance(first.get("would_skip_cap"), bool)))
    # пул с котировкой не SOL -- честный отказ
    cp = next(s for s in B.load_samples(B.CPMM) if s.get("quote_mint") and s["quote_mint"] != C.WSOL)
    r = shadow_build(cp["tx"], cp["source"], cp["mint"], C.EXECUTOR_WALLET, 50_000_000, fake_rpc)
    checks.append(("котировка не SOL -- supported=False с причиной, без исключения",
                   r["supported"] is False and "не SOL" in (r["why_not"] or "")))
    checks.append(("без курса -- cap_usd None с причиной", r["cap_usd"] is None and r["cap_why_not"]))
    # симуляция с ошибкой узла -- не исключение
    def bad_rpc(method, params):
        raise RuntimeError("HTTP 503")
    s = next(x for x in B.load_samples(B.PUMP_AMM) if x.get("quote_mint") == C.WSOL)
    r = shadow_build(s["tx"], s["source"], s["mint"], C.EXECUTOR_WALLET, 50_000_000, bad_rpc)
    checks.append(("сбой узла -- ok=False с причиной, исключение наружу не выходит",
                   r["ok"] is False and "симуляция" in (r["why_not"] or "")))
    checks.append(("классификатор: проскальзывание / баланс / успех",
                   classify_sim({"err": {"x": 1}, "logs": ["Program log: Error Code: ExceededSlippage"]})["sim_verdict"] == "slippage"
                   and classify_sim({"err": {"x": 1}, "logs": ["Program log: Error: insufficient funds"]})["sim_verdict"] == "balance"
                   and classify_sim({"err": None, "logs": []})["sim_verdict"] == "would_pass"))
    # ---- v2: два шага. Шаг 1 -- настоящая сделка WSOL -> HTm (DLMM) из
    # образцов, шаг 2 -- настоящая покупка источника с котировкой HTm.
    import json  # noqa: PLC0415
    htm = "HTmQz7My6MehV7bjhJ6jde8nDND1yvsz68d24LP7YgUQ"
    allx = [x for f in sorted(B.SAMPLES_DIR.glob("*.json")) for x in json.loads(f.read_text(encoding="utf-8"))]
    leg1_tx = leg1_pool = None
    for x in allx:
        for ix in B.all_instructions(x["tx"]):
            if ix.get("programId") != B.DLMM or len(ix["accounts"]) < 16:
                continue
            for qv in ix["accounts"][2:4]:
                tpl = B.extract_template(x["tx"], B.DLMM, qv)
                mv = B.mints_and_vaults(tpl, x["tx"]) if tpl.get("ok") else {}
                if mv.get("quote_mint") == C.WSOL and mv.get("base_mint") == htm and mv["base_vault"] == qv:
                    leg1_tx, leg1_pool = x["tx"], {"program": B.DLMM, "q_vault": qv}
            if leg1_tx:
                break
        if leg1_tx:
            break
    leg2 = [x for x in allx if x.get("quote_mint") == htm and x.get("mint")]
    clock = [1000.0]
    sim_sizes = []
    # таблица адресов ТОЛЬКО для проверки сборки со сжатием (содержимое --
    # счета самих инструкций, это не данные цепи и никуда не пишется)
    lut_addrs = []

    def rpc2(method, params):
        if method == "getSignaturesForAddress":
            return [{"signature": C.first_signature(leg1_tx), "err": None}]
        if method == "getTransaction":
            return leg1_tx
        if method == "getMultipleAccounts":
            return {"value": [{"data": {"parsed": {"info": {"addresses": lut_addrs}}}} for _ in params[0]]}
        if method == "getAccountInfo":
            return fake_rpc(method, params)
        if method == "simulateTransaction":
            sim_sizes.append(len(base64.b64decode(params[0])))
            return {"value": {"err": None, "logs": [], "unitsConsumed": 1}}
        raise RuntimeError(method)
    two_ok = two_n = 0
    if leg1_tx and leg2:
        cache = LegCache({htm: leg1_pool}, rpc2, clock=lambda: clock[0], allow_polling=True)
        st1 = cache.refresh_all()[htm]
        st2 = cache.refresh_all()[htm]
        checks.append((f"кэш: первое обновление -- «{st1}», повтор без новых сделок -- «{st2}», "
                       f"вызовы {dict(cache.calls)}",
                       st1 == "новый шаблон" and st2 == "без изменений"
                       and cache.calls["getTransaction"] == 1 and cache.calls["getSignaturesForAddress"] == 2))
        # новая сделка в пуле -- не своп (по хранилищам пула ничего не сдвинулось):
        # шаблон DLMM остаётся подтверждённым; своп без шаблона -- не подтверждён
        clock[0] += 5
        mv1 = B.mints_and_vaults(B.extract_template(leg1_tx, B.DLMM, leg1_pool["q_vault"]), leg1_tx)
        leg1_pool["w_vault"] = mv1["quote_vault"]

        def synth(dq, dw):     # синтетическая tx: только балансы двух хранилищ пула
            keys = [{"pubkey": leg1_pool["q_vault"]}, {"pubkey": leg1_pool["w_vault"]}]
            bal = [(0, htm, 10**12), (1, C.WSOL, 10**12)]
            pre = [{"accountIndex": i, "mint": m, "owner": "X", "uiTokenAmount": {"amount": str(a), "decimals": 6}}
                   for i, m, a in bal]
            post = [{"accountIndex": i, "mint": m, "owner": "X",
                     "uiTokenAmount": {"amount": str(a + (dq if i == 0 else dw)), "decimals": 6}} for i, m, a in bal]
            return {"meta": {"err": None, "preTokenBalances": pre, "postTokenBalances": post, "innerInstructions": []},
                    "transaction": {"message": {"instructions": [], "accountKeys": keys}}}
        nonswap = synth(0, 0)
        orig = rpc2
        other = [None]

        def rpc3(method, params):
            if method == "getSignaturesForAddress":
                return [{"signature": "N" + str(clock[0]), "err": None},
                        {"signature": C.first_signature(leg1_tx), "err": None}]
            if method == "getTransaction":
                return other[0]
            return orig(method, params)
        cache.rpc_call = rpc3
        other[0] = nonswap
        st3 = cache.refresh_pool(htm)
        age3 = cache.get(htm)[1]
        clock[0] += 5
        other[0] = synth(-5000, 7000)     # своп по хранилищам, но инструкции-шаблона нет
        st4 = cache.refresh_pool(htm)
        age4 = cache.get(htm)[1]
        cache.rpc_call = rpc2
        checks.append((f"не-своп в пуле оставляет шаблон подтверждённым («{st3}», возраст {age3:.0f} с); "
                       f"неизвестная сделка -- нет («{st4}», возраст {age4:.0f} с)",
                       age3 == 0 and age4 == 5 and "годен" in st3 and "не подтверждён" in st4))
        clock[0] -= 10
        cache.entries[htm]["checked_at"] = clock[0]
        keys = {a for x in leg2 for ix in B.all_instructions(x["tx"]) for a in ix["accounts"]}
        keys |= {a for ix in B.all_instructions(leg1_tx) for a in ix["accounts"]}
        lut_addrs.extend(sorted(keys)[:256])
        cache.luts.clear()
        cache.load_luts(["AddressLookupTab1e1111111111111111111111111"])
        for x in leg2:
            r = shadow_build(x["tx"], x["source"], x["mint"], C.EXECUTOR_WALLET, 50_000_000, rpc2,
                             leg_cache=cache)
            two_n += 1
            two_ok += bool(r["ok"] and r["route"] == "two_hop" and r["leg2_amount_in"] and r["tx_size"] <= 1232)
        clock[0] += LEG_MAX_AGE_S + 1
        r_old = shadow_build(leg2[0]["tx"], leg2[0]["source"], leg2[0]["mint"], C.EXECUTOR_WALLET, 50_000_000,
                             rpc2, leg_cache=cache)
        checks.append((f"шаблон старше {LEG_MAX_AGE_S} с -- отказ с причиной: «{r_old['why_not']}»",
                       r_old["ok"] is False and "старше" in (r_old["why_not"] or "")))
    checks.append((f"два шага SOL -> HTm -> токен одной транзакцией: собрано и «симулировано» "
                   f"{two_ok} из {two_n}, размеры {sorted(set(sim_sizes))[:3]}...",
                   two_n >= 5 and two_ok == two_n))
    # ---- ingest: транзакции из подписки, без сети
    if leg1_tx and leg2:
        def no_net(method, params):
            raise AssertionError(f"ingest позвал сеть: {method}")
        ck = [5000.0]
        ic = LegCache({htm: dict(leg1_pool)}, no_net, clock=lambda: ck[0])
        try:
            ic.refresh_all()
            poll_refused = False
        except RuntimeError:
            poll_refused = True
        checks.append(("опрос без allow_polling=True -- отказ", poll_refused))
        note = {"signature": C.first_signature(leg1_tx), "slot": 1,
                "transaction": {k: v for k, v in leg1_tx.items() if k != "blockTime"}}
        st_a = ic.ingest(note).get(htm)
        e, age = ic.get(htm)
        ok_a = st_a == "новый шаблон" and age == 0 and e["trade_time"] == ck[0] and e["sig"] == note["signature"]
        ck[0] += 20
        st_b = ic.ingest(synth(0, 0)).get(htm)                 # не своп -> подтверждён
        age_b = ic.get(htm)[1]
        ck[0] += 20
        st_c = ic.ingest(synth(-5000, 7000)).get(htm)          # своп без шаблона -> не подтверждён
        age_c = ic.get(htm)[1]
        st_d = ic.ingest({"transaction": {"message": {"accountKeys": []}}, "meta": {}})
        r_i = shadow_build(leg2[0]["tx"], leg2[0]["source"], leg2[0]["mint"], C.EXECUTOR_WALLET, 50_000_000,
                           lambda m, p_: rpc2(m, p_) if m in ("simulateTransaction", "getMultipleAccounts",
                                                              "getAccountInfo") else no_net(m, p_),
                           leg_cache=ic)
        checks.append((f"ingest без сети: уведомление подписки -> «{st_a}»; не своп -> «{st_b}» (возраст "
                       f"{age_b:.0f} с); своп без шаблона -> «{st_c}» (возраст {age_c:.0f} с); чужая tx -> {st_d}; "
                       f"сборка из кэша ingest: ok={r_i['ok']}, возраст шаблона {r_i.get('leg1_template_age_s')} с",
                       ok_a and "подтверждён" in st_b and age_b == 0 and "не подтверждён" in st_c
                       and age_c == 20 and st_d == {} and r_i["ok"] and r_i["route"] == "two_hop"))
        ck[0] += 11
        r_o = shadow_build(leg2[0]["tx"], leg2[0]["source"], leg2[0]["mint"], C.EXECUTOR_WALLET, 50_000_000,
                           rpc2, leg_cache=ic)
        checks.append((f"после 31 с без подтверждения -- отказ: «{r_o['why_not']}»",
                       r_o["ok"] is False and "старше" in (r_o["why_not"] or "")))
        pools4 = load_leg_pools()
        wk = warm_lut_keys(pools4)
        checks.append((f"4 пула котировок >= 2 % из data/c2_leg_pools: {len(pools4)}; таблиц для прогрева "
                       f"из образцов: {len(wk)}", len(pools4) == 4 and all(p_["q_vault"] for p_ in pools4.values())))
    # ---- переворот: настоящие продажи Q -> WSOL в DLMM/CLMM дают шаблон покупки
    n_fl = n_fl_ok = 0
    for x in allx + [{"tx": t} for t in C.load_real_txs().values()]:
        for ix in B.all_instructions(x["tx"]):
            prog = ix.get("programId")
            if prog not in FLIP_OK:
                continue
            vi = (2, 3) if prog == B.DLMM else (5, 6)
            for qv in [ix["accounts"][i] for i in vi if i < len(ix["accounts"])]:
                tpl = B.extract_template(x["tx"], prog, qv)
                mv = B.mints_and_vaults(tpl, x["tx"]) if tpl.get("ok") else {}
                if mv.get("base_mint") != C.WSOL or not mv.get("quote_mint"):
                    continue
                q = mv["quote_mint"]
                e = LegCache._template_from(x["tx"], {"program": prog, "q_vault": mv["quote_vault"]}, q)
                n_fl += 1
                if isinstance(e, dict) and e["tpl"].get("flipped"):
                    m2 = e["mv"]
                    ixo = B.swap_instruction(e["tpl"], x["tx"], C.EXECUTOR_WALLET, 1000, 1)
                    acc = [str(a.pubkey) for a in ixo.accounts]
                    in_i, out_i = B.DYN[prog]["in"], B.DYN[prog]["out"]
                    n_fl_ok += (m2["quote_mint"] == C.WSOL and m2["base_mint"] == q
                                and acc[in_i] == B.ata(C.EXECUTOR_WALLET, C.WSOL, m2["quote_program"])
                                and acc[out_i] == B.ata(C.EXECUTOR_WALLET, q, m2["base_program"])
                                and (prog != B.CLMM or (acc[5] == m2["quote_vault"] and acc[11] == C.WSOL)))
                break
    checks.append((f"продажа Q -> SOL в DLMM перевёрнута в покупку (вход WSOL, выход Q): {n_fl_ok} из {n_fl}",
                   n_fl >= 5 and n_fl_ok == n_fl))
    # ---- какие методы узла модуль вообще зовёт: только чтение и симуляция
    import re  # noqa: PLC0415
    src = Path(__file__).read_text(encoding="utf-8")
    calls = set(re.findall(r'"((?:get|send|simulate|request)[A-Z][A-Za-z]+)"', src))
    hot = "".join(inspect.getsource(f) for f in (shadow_build, _two_hop, _simulate, cap_usd))
    hot_calls = set(re.findall(r'rpc_call\(\s*"([A-Za-z]+)"', hot)) | \
        ({"getMultipleAccounts"} if "load_luts(miss, rpc=rpc_call)" in hot else set())
    checks.append((f"модуль зовёт у узла только чтение и симуляцию: {sorted(calls)}",
                   calls <= {"getAccountInfo", "getMultipleAccounts", "getSignaturesForAddress",
                             "getTransaction", "simulateTransaction"}))
    checks.append((f"горячий путь: {sorted(hot_calls)}",
                   hot_calls <= {"getAccountInfo", "getMultipleAccounts", "simulateTransaction"}))
    bad = 0
    for name, ok in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {name}")
        bad += (not ok)
    print(f"самопроверка c2_shadow_build: {len(checks) - bad}/{len(checks)} пройдено")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(self_test() if "--self-test" in sys.argv else 0)
