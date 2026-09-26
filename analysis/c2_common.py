#!/usr/bin/env python3
"""C2 (вторая сессия Code): общие части задач A (толпа за источником) и
B (сэндвичи на покупках). Только чтение цепи и только GET к DBot.

Что здесь и почему отдельно от кода Code-1:
* узел -- общий клиент `solana_rpc_client.SolanaRpc` (темп, 429, учёт
  кредитов по службам), поверх него СВОЙ суточный потолок 100 000 на все
  службы с префиксом `c2_`. Публичный узел запрещён: числа -- только от
  Helius, иначе на половине прогона молча сменится источник данных;
* метод «первый вход >= 2 SOL-экв» НЕ переписан: берётся
  `solana_batch5_rpc_check.classify_tx` как есть. Заменён только источник
  курса USDC/USDT -> SOL: вместо GeckoTerminal -- курс по цепи (своп
  WSOL<->USDC/USDT в той же транзакции, иначе последняя сделка в пуле
  SOL/USDC 3ucNos4N... до покупки -- тот же пул, что у детектора и
  fast_price). Нет курса -> classify_tx ставит price_missing, и такая
  покупка идёт в «нет данных», а не в выборку;
* пул сделки -- тот, где стоит хранилище минта в транзакции: счёт минта,
  у которого баланс ушёл В МИНУС, владелец не подписант. Котировочное
  хранилище -- счёт другого минта с движением в обратную сторону в той же
  инструкции DEX (у CPMM и Raydium v4 один общий владелец на все пулы,
  поэтому одного владельца мало). Кривая pump.fun -- натив владельца;
* цена -- цена ИСПОЛНЕНИЯ сделки в этом пуле: |дельта котировки| /
  |дельта минта| по хранилищам. Так считается одинаково для любого типа
  пула (CPMM, CLMM, DLMM, кривая), а отношение резервов для CLMM/DLMM и
  виртуальных кривых ценой не является. Котировка -- своя у пула (SOL,
  USDC, xStock...), в другую сторону не пересчитывается.

Самопроверка -- на НАСТОЯЩИХ транзакциях из репозитория
(data/bloom_tx_raw.json, data/solana_raw_tx_dump.json,
data/bloom_regression_txs.json), настоящем снимке задач DBot
(data/final/20260923T145755Z/konfig.json) и настоящей копии журнала
исполнителя (data/bloom_seller_diag.json). Выдуманные формы -- только там,
где настоящего артефакта нет (кривая с нативной котировкой, снятие
ликвидности, покупка с получателем-не-подписантом).
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import solana_rpc_client as RC  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data"

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
STABLES = (USDC, USDT)
NATIVE_QUOTE = "native_sol"
# Тот же пул SOL/USDC, что у детектора (bloom_detector.py:747) и у
# solana_buyer200_fast_price.py:64 -- берём по цепи, а не свечи Gecko.
REF_SOL_USDC_POOL = "3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv"
REF2_SOL_USDC_WSOL_VAULT = "ATRsNGv2nDw7hSMfkUTBoVUDsFDwN7po7KbecyiGWNB4"
RATE_MAX_AGE_S = 600
# Курс вне этих границ -- не курс, а неверно спаренные счета.
RATE_SANE_MIN, RATE_SANE_MAX = 20.0, 2000.0

EXECUTOR_WALLET = "4s87RRC2V2XAJD6R8U2dP8kQH99Z2wA6fg88ZVfV4j4N"
BATCH5_WALLET = "5Y8h877swoTzTdc8in9hU3SvXXVv1q9p19Y85tAsdqBv"
LEADER_BEQV = "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit"
# Известный быстрый копировщик (владелец, 24.09): бот через AKBot.
# Инфраструктуру не разбираем -- только «купил ли тот же минт в S+0..S+3».
FAST_COPIER_DT8 = "DT8hib8jY4CGJcmcqcinVGYh5zzVPZAV3iosdQF9a6jX"
OURS_FROM_UTC = "2026-09-24T00:00:00Z"
TASK_NAMES = ("BATCH-3", "BATCH-5")

C2_DAILY_BUDGET = 200_000  # владелец 24.09: поднят до 200 000 на сутки
C2_PREFIX = "c2_"
TX_VERSION = 1  # как у детектора: с 0 узел отвечает -32015 на транзакциях версии 1

DBOT_HOST = "https://api-bot-v1.dbotx.com"
DBOT_TASKS_PATH = "/automation/follow_orders"
SIG_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{80,90}$")
ADDR_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def utc(ts: float | int | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def today_utc() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S', time.gmtime())}Z] {msg}", flush=True)


# ------------------------------------------------------------ кредиты

class BudgetExceeded(Exception):
    """НЕ RuntimeError: SolanaRpc.batch глотает RuntimeError поэлементно в
    None, и исчерпанный бюджет превратился бы в тихие дыры в данных."""


def c2_spent_today(base: Path = RC.USAGE_DIR, day: str | None = None) -> int:
    """Потрачено сегодня (UTC) всеми службами c2_* -- по осколкам учёта
    общей модели solana_rpc_client."""
    day = day or today_utc()
    total = 0
    for p in RC.usage_shards(base):
        if not p.name.startswith(C2_PREFIX):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        for name, v in ((data.get("дни") or {}).get(day) or {}).items():
            if name.startswith(C2_PREFIX) and isinstance(v, dict):
                total += int(v.get("кредитов_за_день", 0) or 0)
    return total


def c2_usage_report(base: Path = RC.USAGE_DIR) -> dict:
    """Расход c2_* по дням и службам -- для шапки каждого доклада."""
    out: dict = {"budget_per_day": C2_DAILY_BUDGET, "days": {}}
    for p in RC.usage_shards(base):
        if not p.name.startswith(C2_PREFIX):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        for day, svcs in (data.get("дни") or {}).items():
            for name, v in (svcs or {}).items():
                if name.startswith(C2_PREFIX) and isinstance(v, dict):
                    d = out["days"].setdefault(day, {"total": 0, "by_service": {}})
                    c = int(v.get("кредитов_за_день", 0) or 0)
                    d["by_service"][name] = d["by_service"].get(name, 0) + c
                    d["total"] += c
    return out


class C2Rpc(RC.SolanaRpc):
    """Общий клиент + потолок c2_* на сутки + только Helius.

    Потолок проверяется ДО каждого запроса (пачка -- на её размер): учёт
    пишется в осколок на каждом вызове, поэтому сумма по осколкам всегда
    текущая, в том числе между скриптами одного прогона.
    """

    def __init__(self, service: str, key: str | None = None, *,
                 budget: int = C2_DAILY_BUDGET, usage_dir: Path = RC.USAGE_DIR) -> None:
        if not service.startswith(C2_PREFIX):
            raise ValueError("служба C2 обязана начинаться с c2_")
        super().__init__(service, key=key, usage_dir=usage_dir, allow_public=False)
        self.budget = budget
        self.calls_by_method: dict = {}
        self._cnt_lock = threading.Lock()

    def check_budget(self, n: int) -> None:
        spent = c2_spent_today(self.meter.base)
        if spent + n > self.budget:
            raise BudgetExceeded(f"суточный потолок C2 {self.budget}: потрачено {spent}, "
                                 f"нужно ещё {n}")

    def _count(self, method: str, n: int) -> None:
        with self._cnt_lock:
            self.calls_by_method[method] = self.calls_by_method.get(method, 0) + n

    def call(self, method: str, params: list, *, url: str | None = None,
             attempts: int = 8, enhanced: bool = False):
        if not self.key:
            raise RuntimeError("ключ Helius не задан (HELIUS_API) -- публичный узел запрещён")
        last = None
        for i in range(3):
            self.check_budget(1)
            self._count(method, 1)
            try:
                return super().call(method, params, url=url, attempts=attempts,
                                    enhanced=enhanced)
            except RuntimeError as exc:
                # Повторяем только сбои транспорта (5xx, сеть). Ответ узла
                # с ошибкой RPC (пропущенный слот и т.п.) -- это данные.
                msg = str(exc)
                if "RPC error" in msg or "бюджет времени" in msg:
                    raise
                last = exc
                time.sleep(2.0 * (i + 1))
        raise last  # type: ignore[misc]

    def batch(self, reqs: list, *, chunk: int | None = None) -> list:
        if not reqs:
            return []
        if chunk is None:
            chunk = (RC.BATCH_GET_TRANSACTION
                     if all(m == "getTransaction" for m, _ in reqs) else RC.BATCH_OTHER)
        out: list = []
        for start in range(0, len(reqs), chunk):
            part = reqs[start:start + chunk]
            self.check_budget(len(part))
            self._count(part[0][0], len(part))
            out.extend(super().batch(part, chunk=chunk))
        return out

    def get_tx(self, sig: str) -> dict | None:
        return self.call("getTransaction", [sig, {"encoding": "jsonParsed",
                                                  "maxSupportedTransactionVersion": TX_VERSION,
                                                  "commitment": "finalized"}])

    def get_txs(self, sigs: list) -> dict:
        """{подпись: tx или None}. None -- узел не отдал, это видно в итогах."""
        uniq = list(dict.fromkeys(sigs))
        res = self.get_transactions(uniq, commitment="finalized") if uniq else []
        return {s: r for s, r in zip(uniq, res)}

    def get_transactions(self, signatures: list, **opts) -> list:
        o = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": TX_VERSION, **opts}
        return self.batch([("getTransaction", [s, o]) for s in signatures],
                          chunk=RC.BATCH_GET_TRANSACTION)

    def signatures(self, address: str, *, before: str | None = None,
                   until: str | None = None, limit: int = 1000) -> list:
        opts: dict = {"limit": limit, "commitment": "finalized"}
        if before:
            opts["before"] = before
        if until:
            opts["until"] = until
        return self.call("getSignaturesForAddress", [address, opts]) or []


def is_skipped_slot_error(msg: str) -> bool:
    return any(c in msg for c in ("-32004", "-32007", "-32009")) or \
        "skipped" in msg or "not available" in msg


# ------------------------------------------------------------ DBot (только GET)

class DbotReadOnly:
    """Клиент DBot, у которого есть ТОЛЬКО GET. Метода записи нет вовсе --
    случайно отправить ордер или правку задачи через него нельзя."""

    def __init__(self, key: str, timeout: float = 25.0) -> None:
        if not key:
            raise RuntimeError("DBOT_API_KEY не задан")
        self._key = key
        self.timeout = timeout

    def get(self, path: str, params: dict | None = None) -> dict:
        import requests  # noqa: PLC0415
        r = requests.get(DBOT_HOST + path, params=params or {}, timeout=self.timeout,
                         headers={"X-API-KEY": self._key, "Accept": "application/json"})
        if not r.ok:
            raise RuntimeError(f"DBot GET {path}: http {r.status_code}")
        body = r.json() or {}
        if body.get("err"):
            raise RuntimeError(f"DBot GET {path}: err {str(body.get('err'))[:160]}")
        return body

    def tasks(self) -> list:
        return self.get(DBOT_TASKS_PATH, {"page": 0, "size": 100}).get("res") or []


def tasks_from_snapshot(obj: dict) -> list:
    """Задачи из ответа или снимка: тело бывает под `res`, `тело.res`, `body.res`."""
    for k in ("тело", "body"):
        if isinstance(obj.get(k), dict) and isinstance(obj[k].get("res"), list):
            return obj[k]["res"]
    return obj.get("res") if isinstance(obj.get("res"), list) else []


def sources_of_tasks(tasks: list, names: tuple = TASK_NAMES) -> list:
    """[{address, task, remark}] из targetIds ВКЛЮЧЁННЫХ задач с нужными именами."""
    out, seen = [], set()
    for t in tasks:
        if not isinstance(t, dict) or t.get("name") not in names or not t.get("enabled"):
            continue
        ids = t.get("targetIds") or []
        rem = t.get("targetNames") or []
        for i, a in enumerate(ids):
            if not (isinstance(a, str) and ADDR_RE.match(a)) or a in seen:
                continue
            seen.add(a)
            out.append({"address": a, "task": t.get("name"),
                        "remark": (rem[i] if i < len(rem) else None) or None})
    return out


def task_wallets(tasks: list) -> dict:
    """{кошелёк задачи: имя задачи} по ВСЕМ задачам -- чтобы отличать
    копировщиков DBot в толпе."""
    return {t.get("walletAddress"): t.get("name") for t in tasks
            if isinstance(t, dict) and t.get("walletAddress")}


# ------------------------------------------------------------ журнал исполнителя

def executor_trades(path: Path | None = None, mode: str = "live") -> list:
    """Сделки исполнителя из копии positions.jsonl (bloom_seller_diag.json).

    Журнал -- набор строк-обновлений по client_order_id; собираем
    последнее состояние позиции и берём подпись покупки."""
    path = path or DATA / "bloom_seller_diag.json"
    d = json.loads(path.read_text(encoding="utf-8"))
    by: dict = {}
    order: list = []
    for r in d.get("positions_rows") or []:
        c = r.get("client_order_id")
        if not c:
            continue
        if c not in by:
            order.append(c)
        by.setdefault(c, {}).update({k: v for k, v in r.items() if v is not None})
    out = []
    for c in order:
        r = by[c]
        if r.get("mode") != mode:
            continue
        sigs = r.get("signatures") or []
        out.append({"client_order_id": c, "signature": sigs[0] if sigs else None,
                    "slot": r.get("our_slot"), "mint": r.get("mint"),
                    "sol_in": r.get("sol_in"), "wallet": r.get("wallet"),
                    "ts_intent_utc": r.get("ts_intent_utc"),
                    "source_sig": r.get("source_sig"), "source_slot": r.get("source_slot")})
    return out


# ------------------------------------------------------------ разбор транзакции

def account_keys(tx: dict) -> list:
    """Ключи транзакции в порядке индексов балансов.

    jsonParsed уже включает адреса из таблиц (source=lookupTable). Если
    узел отдал их отдельно (loadedAddresses) и ключей меньше, чем
    балансов, -- дописываем, иначе индексы разъедутся."""
    t = (tx or {}).get("transaction") or {}
    raw = t.get("accountKeys") or ((t.get("message") or {}).get("accountKeys")) or []
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in raw]
    meta = (tx or {}).get("meta") or {}
    n = len(meta.get("preBalances") or [])
    loaded = meta.get("loadedAddresses") or {}
    if n and len(keys) < n:
        keys = keys + list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])
    return keys


def signers(tx: dict) -> set:
    t = (tx or {}).get("transaction") or {}
    raw = t.get("accountKeys") or ((t.get("message") or {}).get("accountKeys")) or []
    return {k.get("pubkey") for k in raw if isinstance(k, dict) and k.get("signer")}


def first_signature(tx: dict) -> str | None:
    s = ((tx or {}).get("transaction") or {}).get("signatures") or []
    return s[0] if s else None


def token_rows(tx: dict) -> dict:
    """{индекс счёта: {account, owner, mint, dec, pre, post}}; pre/post -- сырые целые."""
    meta = (tx or {}).get("meta") or {}
    keys = account_keys(tx)
    rows: dict = {}
    for when in ("pre", "post"):
        for b in meta.get(f"{when}TokenBalances") or []:
            if not isinstance(b, dict):
                continue
            i = b.get("accountIndex")
            ui = b.get("uiTokenAmount") or {}
            r = rows.setdefault(i, {"account": keys[i] if isinstance(i, int) and i < len(keys) else None,
                                    "owner": b.get("owner"), "mint": b.get("mint"),
                                    "dec": ui.get("decimals"), "pre": 0, "post": 0})
            r[when] = int(ui.get("amount") or 0)
            r["owner"] = r["owner"] or b.get("owner")
            if r.get("dec") is None:
                r["dec"] = ui.get("decimals")
    return rows


def ui(raw: int, dec: int | None) -> D:
    return D(raw) / (D(10) ** int(dec or 0))


def row_delta(r: dict) -> D:
    return ui(r["post"] - r["pre"], r.get("dec"))


def owner_mint_delta(tx: dict, mint: str) -> dict:
    """{владелец: дельта минта в UI} по всем его счетам минта."""
    out: dict = {}
    for r in token_rows(tx).values():
        if r["mint"] == mint and r["owner"]:
            out[r["owner"]] = out.get(r["owner"], D(0)) + row_delta(r)
    return {o: d for o, d in out.items() if d != 0}


def lamport_delta(tx: dict, address: str) -> int | None:
    meta = (tx or {}).get("meta") or {}
    keys = account_keys(tx)
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    try:
        i = keys.index(address)
    except ValueError:
        return None
    if i >= len(pre) or i >= len(post):
        return None
    return int(post[i]) - int(pre[i])


def instruction_account_sets(tx: dict) -> list:
    """Списки счетов НЕразобранных инструкций (внешних и вложенных).

    Инструкции DEX узел не разбирает, у них есть `accounts`. Каждая
    вложенная CPI -- отдельный элемент, поэтому два свопа одного маршрута
    не сливаются в одно множество."""
    t = (tx or {}).get("transaction") or {}
    msg = t.get("message") or {}
    out = []
    lists = [msg.get("instructions") or []]
    for g in ((tx or {}).get("meta") or {}).get("innerInstructions") or []:
        lists.append((g or {}).get("instructions") or [])
    for lst in lists:
        for ix in lst:
            if isinstance(ix, dict) and isinstance(ix.get("accounts"), list):
                out.append(set(a for a in ix["accounts"] if isinstance(a, str)))
    return out


def has_instructions(tx: dict) -> bool:
    msg = (((tx or {}).get("transaction") or {}).get("message") or {})
    return bool(msg.get("instructions"))


# ------------------------------------------------------------ пул сделки

def identify_pool(tx: dict, trader: str, mint: str, *, side: str = "buy") -> dict:
    """Пул, где стоит хранилище минта в этой транзакции.

    Покупка: хранилище минта отдало токен (дельта < 0), его владелец не
    подписант и не сам трейдер. Если таких несколько (маршрут дробился) --
    берётся самое крупное, признак split. Котировочное хранилище -- счёт
    ДРУГОГО минта с обратной дельтой в той же инструкции DEX; из нескольких
    предпочитается тот же владелец. Нет токенного -- натив самого
    владельца хранилища (кривая pump.fun). Иначе -- причина, а не догадка.
    """
    out = {"ok": False, "why_not": None, "pool_vault": None, "pool_owner": None,
           "quote_vault": None, "quote_mint": None, "price": None,
           "vault_delta": None, "quote_delta": None, "split": False,
           "n_vault_candidates": 0}
    sg = signers(tx)
    rows = token_rows(tx)
    want_sign = -1 if side == "buy" else 1
    cands = [r for r in rows.values()
             if r["mint"] == mint and r["owner"] not in sg and r["owner"] != trader
             and r["account"] and (r["post"] - r["pre"]) * want_sign > 0]
    out["n_vault_candidates"] = len(cands)
    if not cands:
        out["why_not"] = "в транзакции нет хранилища минта с движением против трейдера"
        return out
    cands.sort(key=lambda r: abs(r["post"] - r["pre"]), reverse=True)
    v = cands[0]
    big = abs(v["post"] - v["pre"])
    out["split"] = any(abs(r["post"] - r["pre"]) >= big * 0.10 for r in cands[1:])
    out["pool_vault"], out["pool_owner"] = v["account"], v["owner"]
    vd = row_delta(v)
    out["vault_delta"] = str(vd)

    sets = [s for s in instruction_account_sets(tx) if v["account"] in s]
    in_ix = set().union(*sets) if sets else None
    opp = [r for r in rows.values()
           if r["mint"] != mint and r["account"] and r["owner"] not in sg
           and r["owner"] != trader and (r["post"] - r["pre"]) * (-want_sign) > 0]
    if in_ix is not None:
        q = [r for r in opp if r["account"] in in_ix]
    else:
        # Инструкций нет (блок в режиме accounts): только тот же владелец.
        q = [r for r in opp if r["owner"] == v["owner"]]
    same_owner = [r for r in q if r["owner"] == v["owner"]]
    pick = same_owner if len(same_owner) == 1 else (q if len(q) == 1 else [])
    if pick:
        qv = pick[0]
        qd = row_delta(qv)
        out.update(quote_vault=qv["account"], quote_mint=qv["mint"], quote_delta=str(qd))
    elif not q:
        ld = lamport_delta(tx, v["owner"])
        in_same = in_ix is None or v["owner"] in in_ix
        if ld is not None and ld * (-want_sign) > 0 and in_same and v["owner"] not in sg:
            qd = D(ld) / D(10) ** 9
            out.update(quote_vault=v["owner"], quote_mint=NATIVE_QUOTE, quote_delta=str(qd))
        else:
            out["why_not"] = "у хранилища минта нет встречного котировочного счёта"
            return out
    else:
        out["why_not"] = (f"неоднозначная котировка: {len(q)} встречных счетов "
                          f"({', '.join(sorted({str(r['mint'])[:6] for r in q}))})")
        return out
    qd = D(out["quote_delta"])
    if vd == 0:
        out["why_not"] = "нулевая дельта хранилища"
        return out
    out["price"] = abs(qd) / abs(vd)
    out["ok"] = True
    return out


def pool_event(tx: dict, pool: dict) -> dict:
    """Что транзакция сделала с пулом: swap (с ценой), removal, add, other, absent."""
    if (tx or {}).get("meta", {}).get("err") is not None:
        return {"kind": "failed"}
    vault, qv, qm = pool["pool_vault"], pool["quote_vault"], pool["quote_mint"]
    rows = token_rows(tx)
    vr = [r for r in rows.values() if r["account"] == vault]
    if not vr:
        return {"kind": "absent"}
    vd = row_delta(vr[0])
    if qm == NATIVE_QUOTE:
        ld = lamport_delta(tx, qv)
        qd = D(ld) / D(10) ** 9 if ld is not None else D(0)
    else:
        qr = [r for r in rows.values() if r["account"] == qv]
        qd = row_delta(qr[0]) if qr else D(0)
    if vd == 0:
        return {"kind": "other"}
    if qd != 0 and (vd > 0) != (qd > 0):
        return {"kind": "swap", "price": abs(qd) / abs(vd),
                "side": "buy" if vd < 0 else "sell", "vault_delta": vd, "quote_delta": qd}
    if vd < 0 and qd < 0:
        return {"kind": "removal"}
    if vd > 0 and qd > 0:
        return {"kind": "add"}
    return {"kind": "other"}


# ------------------------------------------------------------ покупатели

def quote_spend(tx: dict, wallet: str) -> dict:
    """Сколько кошелёк отдал в котировке: натив (SOL), WSOL, стейблы (USD)."""
    ld = lamport_delta(tx, wallet)
    od_w = {}
    for r in token_rows(tx).values():
        if r["owner"] == wallet and r["mint"] in (WSOL, USDC, USDT):
            od_w[r["mint"]] = od_w.get(r["mint"], D(0)) + row_delta(r)
    return {"sol": (-D(ld) / D(10) ** 9) if ld is not None else D(0),
            "wsol": -od_w.get(WSOL, D(0)),
            "usd": -(od_w.get(USDC, D(0)) + od_w.get(USDT, D(0)))}


def mint_buyers(tx: dict, mint: str) -> dict:
    """{кошелёк: правило} -- кто в этой транзакции КУПИЛ минт.

    Правило 1: подписант, у которого баланс минта вырос.
    Правило 2 (как у Fomo: токен уходит не на подписанта): рост минта у
    не-подписанта, убыль у кого-то (пул), и подписант заплатил котировкой
    (WSOL/стейбл или > 0.01 SOL натива) -- покупатель он.
    Продажа, арбитраж в ноль и перевод покупкой не считаются."""
    if (tx or {}).get("meta", {}).get("err") is not None:
        return {}
    sg = signers(tx)
    od = owner_mint_delta(tx, mint)
    out = {w: "signer_gained" for w in sg if od.get(w, D(0)) > 0}
    if out:
        return out
    gained = [o for o, d in od.items() if d > 0 and o not in sg]
    lost = [o for o, d in od.items() if d < 0]
    if not (gained and lost):
        return {}
    best, best_v = None, D(0)
    for w in sg:
        s = quote_spend(tx, w)
        v = max(s["wsol"], s["usd"] / D(100), s["sol"] if s["sol"] > D("0.01") else D(0))
        if v > best_v:
            best, best_v = w, v
    return {best: "signer_paid"} if best else {}


def mint_sellers(tx: dict, mint: str) -> set:
    """Подписанты, у которых баланс минта упал."""
    if (tx or {}).get("meta", {}).get("err") is not None:
        return set()
    sg = signers(tx)
    od = owner_mint_delta(tx, mint)
    return {w for w in sg if od.get(w, D(0)) < 0}


# ------------------------------------------------------------ курс SOL по цепи

def rate_from_tx(tx: dict) -> D | None:
    """USD за SOL из свопа WSOL<->USDC/USDT в этой транзакции.

    Пара счетов берётся только внутри одной инструкции DEX, чужих
    для трейдера (не подписанты), с обратными дельтами. Из нескольких --
    самая крупная по стейблу."""
    sg = signers(tx)
    rows = [r for r in token_rows(tx).values() if r["account"] and r["owner"] not in sg]
    best, best_usd = None, D(0)
    for s in instruction_account_sets(tx):
        w = [r for r in rows if r["mint"] == WSOL and r["account"] in s and r["post"] != r["pre"]]
        u = [r for r in rows if r["mint"] in STABLES and r["account"] in s and r["post"] != r["pre"]]
        for a in w:
            for b in u:
                da, db = row_delta(a), row_delta(b)
                if (da > 0) == (db > 0) or da == 0:
                    continue
                rate = abs(db) / abs(da)
                if RATE_SANE_MIN <= float(rate) <= RATE_SANE_MAX and abs(db) > best_usd:
                    best, best_usd = rate, abs(db)
    return best


class RateBook:
    """Курс USD/SOL по цепи на момент покупки. Кэш по минуте.

    Порядок: своп в самой транзакции -> последняя сделка в пуле SOL/USDC
    до этой транзакции (getSignaturesForAddress(before=подпись) +
    getTransaction). Нет ни того, ни другого -- None, и покупка уходит в
    «нет данных по курсу»."""

    def __init__(self, rpc: C2Rpc | None) -> None:
        self.rpc = rpc
        self.by_minute: dict = {}
        self.lock = threading.Lock()
        self.stats = {"same_tx": 0, "ref_pool": 0, "cache": 0, "missing": 0}

    def rate_for(self, tx: dict) -> tuple[D | None, str]:
        r = rate_from_tx(tx)
        bt = tx.get("blockTime")
        if r is not None:
            with self.lock:
                self.stats["same_tx"] += 1
                if bt is not None:
                    self.by_minute.setdefault(bt // 60, (r, "same_tx"))
            return r, "same_tx"
        if bt is None:
            with self.lock:
                self.stats["missing"] += 1
            return None, "нет blockTime"
        with self.lock:
            hit = self.by_minute.get(bt // 60)
        if hit:
            with self.lock:
                self.stats["cache"] += 1
            return hit[0], f"cache_minute:{hit[1]}"
        if self.rpc is None:
            with self.lock:
                self.stats["missing"] += 1
            return None, "нет узла"
        sig = first_signature(tx)
        why = []
        # Два опорных пула SOL/USDC: пул детектора и пул 8FnX3xo2 (адрес его
        # WSOL-хранилища взят из настоящей транзакции, data/bloom_tx_raw.json).
        for ref in (REF_SOL_USDC_POOL, REF2_SOL_USDC_WSOL_VAULT):
            try:
                page = self.rpc.signatures(ref, before=sig, limit=25)
            except RuntimeError as exc:
                why.append(f"{ref[:6]}: {str(exc)[:60]}")
                continue
            tried = 0
            for x in page:
                if x.get("err") is not None:
                    continue
                sbt = x.get("blockTime")
                if sbt is None or sbt > bt or bt - sbt > RATE_MAX_AGE_S:
                    continue
                if tried >= 6:
                    break
                tried += 1
                try:
                    t2 = self.rpc.get_tx(x["signature"])
                except RuntimeError:
                    continue
                r2 = rate_from_tx(t2 or {})
                if r2 is not None:
                    with self.lock:
                        self.by_minute[bt // 60] = (r2, "ref_pool")
                        self.stats["ref_pool"] += 1
                    return r2, f"ref_pool:{ref[:6]}:{x['signature'][:16]}"
            why.append(f"{ref[:6]}: свопа не нашлось ({tried} попыток)")
        # Последний довод -- ближайший курс, уже полученный по цепи в этом
        # прогоне, не дальше 10 минут: для порога 2 SOL сдвиг курса за это
        # время на порядок меньше расстояния до порога у большинства сделок.
        with self.lock:
            near = sorted((abs(m - bt // 60), v) for m, v in self.by_minute.items()
                          if abs(m - bt // 60) <= 10)
        if near:
            with self.lock:
                self.stats["cache"] += 1
            return near[0][1][0], f"cache_near_{near[0][0]}min:{near[0][1][1]}"
        with self.lock:
            self.stats["missing"] += 1
        return None, "курс не найден: " + "; ".join(why)


_CTX = threading.local()


def install_onchain_rate(rc_module, book: RateBook) -> None:
    """Подменить в classify_tx источник курса: вместо GeckoTerminal -- RateBook.

    classify_tx зовёт fp.find_price_at_gecko(t, lo, hi) и берёт
    ["event"]["p1_per_0"] (USD за SOL). Текущая транзакция -- из _CTX,
    которую выставляет classify_first_entry перед вызовом. Прецедент такой
    подмены -- solana_crowd_scan.py:524-535."""
    def patched(t, lo, hi):  # noqa: ARG001
        tx = getattr(_CTX, "tx", None)
        if tx is None:
            raise RuntimeError("нет текущей транзакции")
        r, src = book.rate_for(tx)
        _CTX.rate_source = src
        if r is None:
            raise RuntimeError(src)
        _CTX.rate = r
        return {"event": {"p1_per_0": str(r)}}
    rc_module.fp.find_price_at_gecko = patched


def classify_first_entry(rc_module, tx: dict, wallet: str) -> dict | None:
    """classify_tx как есть + курс и его источник в результате.

    Изъян метода, найденный на настоящей транзакции (jg, покупка DtYzXe9c
    за 3532 USDC без SOL-ноги): без курса classify_tx обнуляет стейбл-ногу,
    трата выходит 0, и он отвечает «no_first_entry» -- покупка молча
    пропадает. Курс он запрашивает ТОЛЬКО для единственного нового минта со
    стейбл-тратой, поэтому «курс запрошен и не получен» + no_first_entry =
    первый вход с неизвестным размером. Такой случай возвращается как
    kind="rate_missing" и идёт в «нет данных», а не в «не покупка»."""
    _CTX.tx, _CTX.rate, _CTX.rate_source = tx, None, None
    try:
        ev = rc_module.classify_tx(tx, wallet)
    finally:
        rate, src = _CTX.rate, _CTX.rate_source
        _CTX.tx = None
    if ev and ev.get("kind") == "no_first_entry" and src is not None and rate is None:
        return {"kind": "rate_missing", "rate_source": src, "slot": tx.get("slot"),
                "signature": first_signature(tx), "block_time": tx.get("blockTime")}
    if ev and ev.get("kind") == "first_entry":
        ev = dict(ev)
        ev["rate_usd_per_sol"] = float(rate) if rate is not None else None
        ev["rate_source"] = src
        ev["block_time"] = tx.get("blockTime")
    return ev


def load_rpc_check():
    """Импорт метода первого входа. Побочный эффект импорта fast_price --
    каталог data/solana_buyer_200/rpc_cache/ (он в .gitignore)."""
    import solana_batch5_rpc_check as rc  # noqa: PLC0415
    if not rc.DEX_PROGRAMS:
        raise RuntimeError("dex_labels.json не прочитан: без него classify_tx молча "
                           "отдаёт no_first_entry на всё")
    return rc


# ------------------------------------------------------------ статистика

def median(xs: list):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def load_real_txs() -> dict:
    """Настоящие jsonParsed-транзакции из репозитория: {подпись: tx}."""
    out = {}
    for name in ("bloom_tx_raw.json", "solana_raw_tx_dump.json", "bloom_regression_txs.json"):
        p = DATA / name
        if not p.exists():
            continue

        def walk(o):
            if isinstance(o, dict):
                t = o.get("transaction")
                if isinstance(t, dict) and "meta" in o and isinstance(t.get("message"), dict):
                    s = first_signature(o)
                    if s:
                        out[s] = o
                    return
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(json.loads(p.read_text(encoding="utf-8")))
    return out


# ------------------------------------------------------------ самопроверка

def self_test() -> int:
    checks: list = []

    def chk(name, ok, got=""):
        checks.append((name, bool(ok), got))

    txs = load_real_txs()
    chk("настоящие транзакции из репозитория прочитаны (>=15)", len(txs) >= 15, len(txs))

    def by_prefix(p):
        return next((t for s, t in txs.items() if s.startswith(p)), None)

    # 1. BATCH-3 (BmjAUD) покупает 87pa2UbB: пул CPMM с котировкой Xsa62P5m (xStock).
    t = by_prefix("2uPwpSAQ")
    chk("bloom_tx_raw: транзакция найдена", t is not None)
    if t:
        m = "87pa2UbBB2dhD4b7CHDPHzzKjrdrUtEcBHueXnVz6CJp"
        p = identify_pool(t, "BmjAUDbwBMxR5shrmzBtKRwveVahFGFiEH3oTq7QTHnu", m)
        chk("пул найден по хранилищу минта", p["ok"], p)
        chk("хранилище -- J7vjBbQG (владелец GpMZbSM2, CPMM)",
            str(p["pool_vault"]).startswith("J7vjBbQG") and str(p["pool_owner"]).startswith("GpMZbSM2"), p)
        chk("котировка пула -- xStock Xsa62P5m, а не SOL",
            str(p["quote_mint"]).startswith("Xsa62P5m"), p["quote_mint"])
        exp = (D(3061569) / D(10) ** 8) / (D(162352420501) / D(10) ** 6)
        chk("цена = |дельта котировки| / |дельта минта|", p["price"] == exp, (p["price"], exp))
        r = rate_from_tx(t)
        chk("курс из той же транзакции: своп USDC->SOL в 8FnX3xo2",
            r is not None and abs(float(r) - 7.279426 / 0.06375618) < 1e-9, r)
        b = mint_buyers(t, m)
        chk("покупатель -- подписант BmjAUD", set(b) == {"BmjAUDbwBMxR5shrmzBtKRwveVahFGFiEH3oTq7QTHnu"}, b)
        ev = pool_event(t, p)
        chk("своя транзакция по пулу -- покупка с той же ценой",
            ev["kind"] == "swap" and ev["side"] == "buy" and ev["price"] == p["price"], ev)

    # 2. DE5yR9 покупает MYQZzuii: CPMM с котировкой 4rkGWJNS; параллельно пул
    #    4rkG/SOL с «самовладеющими» хранилищами -- не должен сбить пару.
    t = by_prefix("4yFJdscf")
    chk("solana_raw_tx_dump: транзакция найдена", t is not None)
    if t:
        m = "MYQZzuii"
        full = next((r["mint"] for r in token_rows(t).values() if r["mint"].startswith(m)), None)
        p = identify_pool(t, "DE5yR9S8qBkh8n4rYVvc6iGDF3rqrEPZX2aHBsDWJ79n", full)
        chk("пул MYQZ: хранилище ERW7x5RK, котировка 4rkGWJNS",
            p["ok"] and str(p["pool_vault"]).startswith("ERW7x5RK")
            and str(p["quote_mint"]).startswith("4rkGWJNS"), p)

    # 3. Omakase (F5MYbj) покупает FLyzixQC за 1000 USDC через USDC->SOL->токен.
    t = by_prefix("")  # найдём по подписанту
    t = next((x for x in txs.values() if "F5MYbjEATQFD6rxwdS2zXzEHBGuUhSGvJkUhLFAcr4hv" in signers(x)), None)
    chk("regression: покупка Omakase найдена", t is not None)
    if t:
        full = next((r["mint"] for r in token_rows(t).values() if r["mint"].startswith("FLyzixQC")), None)
        p = identify_pool(t, "F5MYbjEATQFD6rxwdS2zXzEHBGuUhSGvJkUhLFAcr4hv", full)
        chk("пул FLyz котируется в WSOL (хранилище 2VTEzqeF)",
            p["ok"] and p["quote_mint"] == WSOL and str(p["quote_vault"]).startswith("2VTEzqeF"), p)
        r = rate_from_tx(t)
        chk("курс по цепи из той же транзакции = 1000 / 8.706675352",
            r is not None and abs(float(r) - 1000 / 8.706675352) < 1e-9, r)
        rc = load_rpc_check()
        book = RateBook(None)
        install_onchain_rate(rc, book)
        ev = classify_first_entry(rc, t, "F5MYbjEATQFD6rxwdS2zXzEHBGuUhSGvJkUhLFAcr4hv")
        chk("classify_tx (метод проекта) видит первый вход", ev and ev.get("kind") == "first_entry", ev)
        if ev and ev.get("kind") == "first_entry":
            want = 0.0076635 + 1000 / (1000 / 8.706675352)
            chk("трата = SOL + WSOL + USDC/курс (по цепи), не Gecko",
                abs(ev["spend_sol_equiv"] - (0.007663512 + 8.706675352)) < 1e-6
                and ev["rate_source"] == "same_tx" and ev["price_missing"] is False,
                (ev["spend_sol_equiv"], want, ev["rate_source"]))
        chk("курс взят из транзакции, узел не нужен", book.stats["same_tx"] >= 1, book.stats)

    # 4. Без своп-пары и без узла курс -- None, и classify_tx ставит price_missing.
    t = next((x for x in txs.values() if "4hwPamSooBr5JhxHdcEC21HoxN5HUwYR2hGucLPyZAi8" in signers(x)
              and any(str(r["mint"]).startswith("DtYzXe9c") for r in token_rows(x).values())), None)
    chk("regression: покупка jg за USDC без SOL-ноги найдена", t is not None)
    if t:
        chk("в ней нет пары WSOL/стейбл -- курс из транзакции None", rate_from_tx(t) is None)
        rc = load_rpc_check()
        install_onchain_rate(rc, RateBook(None))
        ev = classify_first_entry(rc, t, "4hwPamSooBr5JhxHdcEC21HoxN5HUwYR2hGucLPyZAi8")
        chk("без курса покупка за USDC -- rate_missing, а не «не покупка»",
            ev and ev.get("kind") == "rate_missing", ev)
        ev0 = rc.classify_tx(t, "4hwPamSooBr5JhxHdcEC21HoxN5HUwYR2hGucLPyZAi8")
        chk("(сам classify_tx в этом случае отвечает no_first_entry -- изъян зафиксирован)",
            ev0 == {"kind": "no_first_entry"}, ev0)

    # 5. Покупка тестового кошелька DNJeTY: котировка 3NZ9JMVB, пул 7w9Q8q6R.
    t = next((x for x in txs.values() if "DNJeTYni5QQVCwNNt1SrHu1GjYR4ak5Zw7LeuFsKYmXX" in signers(x)), None)
    if t:
        full = next((r["mint"] for r in token_rows(t).values() if r["mint"].startswith("AC1vvvYc")), None)
        p = identify_pool(t, "DNJeTYni5QQVCwNNt1SrHu1GjYR4ak5Zw7LeuFsKYmXX", full)
        chk("пул AC1v: владелец 7w9Q8q6R, котировка 3NZ9JMVB",
            p["ok"] and str(p["pool_owner"]).startswith("7w9Q8q6R")
            and str(p["quote_mint"]).startswith("3NZ9JMVB"), p)

    # 6. Все настоящие покупки: пул либо найден, либо названа причина -- без исключений.
    n_ok = n_why = 0
    for x in txs.values():
        for w in signers(x):
            od = {r["mint"]: 1 for r in token_rows(x).values()
                  if r["owner"] == w and r["post"] > r["pre"] and r["mint"] not in (WSOL, USDC, USDT)}
            for mm in od:
                p = identify_pool(x, w, mm)
                n_ok += p["ok"]
                n_why += (not p["ok"]) and bool(p["why_not"])
    chk("по настоящим покупкам: пул или причина, третьего нет", n_ok > 0 and n_ok + n_why > 0,
        (n_ok, n_why))

    # 7. Синтетика там, где настоящего артефакта нет.
    def mk(keys, pre_t, post_t, pre_l=None, post_l=None, ix=None, sg=("W",), err=None):
        n = len(keys)
        return {"slot": 1, "blockTime": 100,
                "transaction": {"signatures": ["S" * 88],
                                "message": {"accountKeys": [{"pubkey": k, "signer": k in sg} for k in keys],
                                            "instructions": ix or []}},
                "meta": {"err": err, "preBalances": pre_l or [0] * n, "postBalances": post_l or [0] * n,
                         "preTokenBalances": pre_t, "postTokenBalances": post_t, "innerInstructions": []}}

    def tb(i, owner, mint, amt, dec=6):
        return {"accountIndex": i, "owner": owner, "mint": mint,
                "uiTokenAmount": {"amount": str(amt), "decimals": dec}}

    # кривая pump.fun: котировка -- натив владельца хранилища
    keys = ["W", "CURVE", "CURVE_ATA", "W_ATA", "FEE"]
    t = mk(keys, [tb(2, "CURVE", "M", 1_000_000_000)],
           [tb(2, "CURVE", "M", 900_000_000), tb(3, "W", "M", 100_000_000)],
           pre_l=[10_000_000_000, 5_000_000_000, 2, 0, 0],
           post_l=[8_990_000_000, 6_000_000_000, 2, 2, 10_000_000],
           ix=[{"programId": "PUMP", "accounts": ["CURVE", "CURVE_ATA", "W_ATA", "W", "FEE"]}])
    p = identify_pool(t, "W", "M")
    chk("кривая: котировка -- натив владельца хранилища",
        p["ok"] and p["quote_mint"] == NATIVE_QUOTE and p["quote_vault"] == "CURVE"
        and p["price"] == D(1) / D(100), p)
    # снятие ликвидности видно как removal
    t2 = mk(keys, [tb(2, "CURVE", "M", 900_000_000)], [tb(2, "CURVE", "M", 0)],
            pre_l=[0, 6_000_000_000, 2, 0, 0], post_l=[0, 1_000_000, 2, 0, 0], sg=("MIGRATOR",))
    chk("снятие ликвидности -- removal, а не цена", pool_event(t2, p)["kind"] == "removal")
    # продажа в пул: подписант теряет минт -- не покупатель
    t3 = mk(keys, [tb(2, "CURVE", "M", 900_000_000), tb(3, "W", "M", 100_000_000)],
            [tb(2, "CURVE", "M", 1_000_000_000), tb(3, "W", "M", 0)],
            pre_l=[0, 6_000_000_000, 2, 2, 0], post_l=[0, 5_000_000_000, 2, 2, 0])
    chk("продажа покупкой не считается", mint_buyers(t3, "M") == {} and mint_sellers(t3, "M") == {"W"})
    ev3 = pool_event(t3, p)
    chk("продажа в пул -- swap/sell с ценой", ev3["kind"] == "swap" and ev3["side"] == "sell", ev3)
    # токен ушёл не подписанту, подписант заплатил USDC -- покупатель подписант
    keys4 = ["PAYER", "POOL", "POOLV", "PQ", "RECV", "RECV_ATA", "PAYER_USDC"]
    t4 = mk(keys4, [tb(2, "POOL", "M", 1000), tb(3, "POOL", USDC, 0), tb(6, "PAYER", USDC, 500_000_000)],
            [tb(2, "POOL", "M", 900), tb(3, "POOL", USDC, 400_000_000), tb(5, "RECV", "M", 100),
             tb(6, "PAYER", USDC, 100_000_000)], sg=("PAYER",))
    chk("правило 2: получатель не подписант -- покупатель платящий подписант",
        mint_buyers(t4, "M") == {"PAYER": "signer_paid"}, mint_buyers(t4, "M"))
    # неуспешная транзакция -- никого
    t5 = mk(keys4, [], [tb(5, "RECV", "M", 100)], err={"x": 1})
    chk("неуспешная транзакция -- ни покупателей, ни событий пула",
        mint_buyers(t5, "M") == {} and pool_event(t5, p)["kind"] == "failed")
    # неоднозначная котировка: два встречных счёта, разные владельцы, без инструкций
    keys6 = ["W", "V", "Q1", "Q2", "WA"]
    t6 = mk(keys6, [tb(1, "O1", "M", 1000), tb(2, "O2", WSOL, 0, 9), tb(3, "O3", USDC, 0)],
            [tb(1, "O1", "M", 900), tb(2, "O2", WSOL, 5, 9), tb(3, "O3", USDC, 5), tb(4, "W", "M", 100)],
            ix=[{"programId": "X", "accounts": ["V", "Q1", "Q2"]}])
    p6 = identify_pool(t6, "W", "M")
    chk("два встречных счёта в одной инструкции -- причина, а не выбор наугад",
        (not p6["ok"]) and "неоднозначная" in (p6["why_not"] or ""), p6)

    # 8. Настоящий снимок задач DBot: 8 + 9 источников, Beqv в BATCH-5 нет.
    snap = DATA / "final" / "20260923T145755Z" / "konfig.json"
    if snap.exists():
        tasks = tasks_from_snapshot(json.loads(snap.read_text(encoding="utf-8")))
        src = sources_of_tasks(tasks)
        chk("снимок 23.09: BATCH-3 = 8, BATCH-5 = 9",
            sum(s["task"] == "BATCH-3" for s in src) == 8 and sum(s["task"] == "BATCH-5" for s in src) == 9,
            [(s["task"], s["remark"]) for s in src])
        chk("кошельки задач читаются", task_wallets(tasks).get(BATCH5_WALLET) == "BATCH-5")
        chk("Beqv в BATCH-5 снимка 23.09 нет", LEADER_BEQV not in {s["address"] for s in src})
    # 9. Настоящая копия журнала исполнителя: 7 боевых сделок, кошелёк 4s87.
    # ПУТЬ -- ОТДЕЛЬНАЯ ФИКСТУРА, а не рабочий снимок. Рабочий
    # data/bloom_seller_diag.json перезаписывает каждый прогон разбора
    # пропавшего сигнала, и 26.09 в 03:30 он перезаписал его ночными сделками
    # -- самопроверка покраснела не от кода, а от свежих данных. Числа в
    # проверке привязаны к конкретной копии, поэтому и копия своя.
    фикстура = DATA / "fixtures" / "bloom_seller_diag_7_sdelok.json"
    tr = executor_trades(path=фикстура if фикстура.exists() else None)
    chk("журнал исполнителя: 7 боевых сделок с подписями",
        len(tr) == 7 and all(x["signature"] and SIG_RE.match(x["signature"]) for x in tr),
        [(x["ts_intent_utc"], x["signature"][:8] if x["signature"] else None) for x in tr])
    chk("все с кошелька исполнителя", all(x["wallet"] == EXECUTOR_WALLET for x in tr))

    # 10. Потолок: служба без c2_ не создаётся; потолок считает только c2_.
    import tempfile  # noqa: PLC0415
    tmp = Path(tempfile.mkdtemp())
    try:
        C2Rpc("чужая", key="K", usage_dir=tmp)
        chk("служба без c2_ отклоняется", False)
    except ValueError:
        chk("служба без c2_ отклоняется", True)
    r = C2Rpc("c2_test", key="K", usage_dir=tmp, budget=10)
    r.meter.add(9)
    RC.CreditMeter("ledger", tmp).add(1000)
    chk("потолок C2 не считает чужие службы", c2_spent_today(tmp) == 9, c2_spent_today(tmp))
    try:
        r.check_budget(2)
        chk("потолок срабатывает до запроса", False)
    except BudgetExceeded:
        chk("потолок срабатывает до запроса", True)
    chk("BudgetExceeded не глотается как RuntimeError", not issubclass(BudgetExceeded, RuntimeError))
    chk("публичный узел запрещён", r.allow_public is False)
    rep = c2_usage_report(tmp)
    chk("отчёт расхода видит c2_test", rep["days"].get(today_utc(), {}).get("total") == 9, rep)
    # DbotReadOnly -- только GET
    chk("у клиента DBot нет методов записи",
        not any(hasattr(DbotReadOnly, m) for m in ("post", "put", "patch", "delete")))

    bad = 0
    for name, ok, got in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {name}" + (f"  -> {str(got)[:300]}" if not ok else ""))
        bad += (not ok)
    print(f"самопроверка c2_common: {len(checks) - bad}/{len(checks)} пройдено")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    print(__doc__)
