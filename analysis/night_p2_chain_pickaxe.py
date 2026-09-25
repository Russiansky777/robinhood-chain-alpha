#!/usr/bin/env python3
"""Ночная задача P2 владельца: сверка по цепи сделки PICKAXE. ТОЛЬКО ЧТЕНИЕ.

Пять подписей одной гонки (слоты 450191590-450191592):
  leader   -- источник сигнала;
  sniper1, sniper2 -- копировщики, купившие тот же минт;
  ours     -- НАША покупка Bloom (2Vs5qv9V...);
  dbot     -- копия того же сигнала у DBot.

По каждой: индекс в блоке, число транзакций между лидером и ею, приоритет
(микролампоры на CU, из инструкций ComputeBudget SetComputeUnitPrice/Limit).
Плюс: чьи это счета -- 2nyhqdwK.../4ACfpUFo... заявлены владельцем как
tip-аккаунты тарифа Helius Sender Max; здесь это ПРОВЕРЯЕТСЯ программно по
analysis/bloom_own_send.py (TIP_ACCOUNTS), а не принимается на слово. Третий
адрес (B1do...QG5m) в задаче дан не полностью -- он ищется по цепи в НАШЕЙ
транзакции по префиксу/суффиксу и печатается целиком, а не достраивается.

ЧТЕНИЕ ЦЕПИ -- ТОЛЬКО через параметр rpc_call (метод, параметры) -> ответ,
который передаёт вызывающий. В бою это будет analysis/c2_common.C2Rpc.call
(суточный потолок кредитов C2_DAILY_BUDGET, служба обязана начинаться с
c2_) -- модуль его не подменяет и не обходит: main() ниже, как и другие
клиенты c2_common (bloom_block_position.py, c2_followers_growth.py),
строит его сам ТОЛЬКО в CLI-обвязке, а вся разбирающая логика в файле
принимает rpc_call снаружи и ни разу не строит URL и не зовёт requests
напрямую -- это проверяет сама самопроверка (см. её последний пункт).
HELIUS_API_KEY в этом контейнере нет, поэтому боевой прогон здесь и не
делался: числа собираются самопроверкой на СИНТЕТИЧЕСКИХ блоках.

getBlock vs getTransaction -- по кредитам оба стоят 1 кредит за вызов
(docs.helius.dev, см. также CREDITS_BY_METHOD в solana_rpc_client.py: не
перечислен -- значит, CREDITS_DEFAULT = 1, независимо от transactionDetails
и encoding). Дешевле тот способ, где МЕНЬШЕ вызовов: getBlock -- один вызов
на СЛОТ (здесь 3 слота = 3 кредита), getTransaction -- один на ПОДПИСЬ
(здесь 5 подписей = 5 кредитов). Выбирается getBlock; функция выбора и
итоговый счёт кредитов -- в выводе (`read_method`, `credits_used`).

Честность. Не удалось прочитать блок/транзакцию, подписи нет ни в одном из
прочитанных блоков, кандидат на адрес неоднозначен -- везде причина в
`why_not`, а не догадка и не ноль вместо "неизвестно".
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402 -- только account_keys/lamport_delta/TX_VERSION, чтения
import bloom_own_send as OS  # noqa: E402 -- только TIP_ACCOUNTS, для проверки факта, не для отправки

COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
SYSTEM_PROGRAM = "11111111111111111111111111111111111111111"

СЛОТЫ_ПО_УМОЛЧАНИЮ = (450191590, 450191591, 450191592)
ПОДПИСИ = {
    "leader": "2Nm7Ef1QUsoAZ8AUvC4d34oNbtEuZqbLfL8vy8ihVSv9dRyPZbz4umEsY4TV9r1FDTbw4cdrapKs7pyP7Vj3qtcA",
    "sniper1": "2At3Drxm5xjwWHfFEEYid7S8dTu3T2V2JBuFFhsG3ywV46XskhXw9XpRpmY2S9xKdzdJN9qidY6ka9vEyQqd43w5",
    "sniper2": "4JK7Qg638rYNMeNiTqzaLYCT6qLdPC2r8oZXgGQUaLetoi5tEtbRYaSq9YkgAGx1ZCuDChGk98KMw6QC8RxGU5Li",
    "ours": "2Vs5qv9V2HVhbNzd5sMzgfqLFZ37SxbbMDUBjm6a6PaFkX6N6m8BGgzEePAp4uvdiwQvV8E2UaCorAktn2Lo5fCS",
    "dbot": "QhKVtVuZDCgNMahCcWpBoHkQySaPfjk3giFArTL2j5jQWk3LAK66za1Ciex98euH32BAmrL52uhvEovVESUs1WR",
}
ПОРЯДОК_ИМЁН = ("leader", "sniper1", "sniper2", "ours", "dbot")

# Аккаунты, названные владельцем -- сверяются программно, а не на слово.
TIP_2NYHQ = "2nyhqdwKcJZR2vcqCyrYsaPVdAnFoJjiksCXJ7hfEYgD"
TIP_4ACFP = "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE"
B1DO_ПРЕФИКС = "B1do"
B1DO_СУФФИКС = "QG5m"
B1DO_ОЖИДАЕМЫЕ_ЛАМПОРТЫ = 2_976_880          # 0.00297688 SOL, из вопроса владельца

С_ДАТЫ_ПО_УМОЛЧАНИЮ = "2026-09-24T00:00:00Z"
КАТАЛОГ_ПО_УМОЛЧАНИЮ = "/home/bot/bloom_executor_live_data"
ЛИМИТ_ИСТОРИИ_ПО_УМОЛЧАНИЮ = 500              # предохранитель кредитов на сумму "с 24.09"


# ------------------------------------------------------------ base58 (без внешних пакетов)

_B58_АЛФАВИТ = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s: str) -> bytes:
    """Тот же приём, что уже есть в репозитории (solana_batch_fee_change_first_trade.py):
    jsonParsed не всегда расшифровывает ComputeBudget, и тогда данные инструкции
    приходят сырым base58 -- декодируем сами, без пакета base58."""
    число = 0
    for ch in s:
        число = число * 58 + _B58_АЛФАВИТ.index(ch)
    тело = число.to_bytes((число.bit_length() + 7) // 8, "big") if число else b""
    ведущих_единиц = len(s) - len(s.lstrip("1"))
    return b"\x00" * ведущих_единиц + тело


def b58encode(data: bytes) -> str:
    """Только для самопроверки: собрать синтетическую ComputeBudget-инструкцию
    так же, как она приходит с узла, и проверить, что b58decode -- обратная операция."""
    число = int.from_bytes(data, "big")
    вых = ""
    while число > 0:
        число, остаток = divmod(число, 58)
        вых = _B58_АЛФАВИТ[остаток] + вых
    ведущих_нулей = len(data) - len(data.lstrip(b"\x00"))
    return "1" * ведущих_нулей + (вых or ("1" if data else ""))


# ------------------------------------------------------------ чтение блоков

def _подпись(t: dict) -> str | None:
    подписи = ((t or {}).get("transaction") or {}).get("signatures") or []
    return подписи[0] if подписи else None


def выбрать_способ(число_слотов: int, число_подписей: int) -> dict:
    """getBlock и getTransaction оба по 1 кредиту за вызов (см. докстринг
    модуля) -- дешевле тот, где меньше вызовов."""
    getblock = число_слотов
    gettx = число_подписей
    return {"getBlock_credits": getblock, "getTransactions_credits": gettx,
            "chosen": "getBlock" if getblock <= gettx else "getTransactions"}


def получить_блоки(rpc_call, слоты: list) -> dict:
    """{слот: {"known", "total", "transactions", "why_not"}}. Один вызов на
    слот (encoding=jsonParsed, transactionDetails=full -- нужны инструкции
    ComputeBudget, уровня accounts для этого недостаточно)."""
    out = {}
    for slot in слоты:
        try:
            блок = rpc_call("getBlock", [slot, {
                "encoding": "jsonParsed", "transactionDetails": "full",
                "rewards": False, "maxSupportedTransactionVersion": C.TX_VERSION}])
        except Exception as exc:  # noqa: BLE001
            out[slot] = {"known": False, "why_not": f"getBlock не отдался: {type(exc).__name__}: {str(exc)[:160]}"}
            continue
        txs = (блок or {}).get("transactions")
        if not txs:
            out[slot] = {"known": False, "why_not": "в ответе getBlock нет транзакций"}
            continue
        out[slot] = {"known": True, "total": len(txs), "transactions": txs}
    return out


def позиция_в_блоках(блоки: dict, подпись: str) -> dict | None:
    for slot in sorted(блоки):
        блок = блоки[slot]
        if not блок.get("known"):
            continue
        for i, t in enumerate(блок["transactions"]):
            if _подпись(t) == подпись:
                return {"slot": slot, "index": i, "total": блок["total"]}
    return None


def почему_нет_позиции(блоки: dict) -> str:
    неизв = [(s, блоки[s].get("why_not")) for s in sorted(блоки) if not блоки[s].get("known")]
    if неизв:
        return "; ".join(f"слот {s}: {w}" for s, w in неизв)
    return "подписи нет ни в одном из прочитанных блоков"


def транзакций_между(блоки: dict, a: dict | None, b: dict | None) -> dict:
    """Сколько транзакций стоит СТРОГО между двумя позициями (по слотам и
    индексам). Промежуточные блоки, которых нет среди прочитанных, делают
    счёт НЕизвестным целиком -- частичный счёт хуже отсутствующего, потому
    что выглядит как полный (тот же довод, что в bloom_block_position.py)."""
    if a is None or b is None:
        return {"known": False, "why_not": "позиция одной из двух транзакций не найдена в блоках"}
    if (a["slot"], a["index"]) == (b["slot"], b["index"]):
        return {"known": True, "count": 0}
    lo, hi = (a, b) if (a["slot"], a["index"]) < (b["slot"], b["index"]) else (b, a)
    if lo["slot"] == hi["slot"]:
        return {"known": True, "count": hi["index"] - lo["index"] - 1}
    if lo["slot"] not in блоки or not блоки[lo["slot"]].get("known"):
        return {"known": False, "why_not": f"слот {lo['slot']} не прочитан"}
    хвост = блоки[lo["slot"]]["total"] - lo["index"] - 1
    голова = hi["index"]
    середина, почему = 0, []
    for s in range(lo["slot"] + 1, hi["slot"]):
        блок = блоки.get(s)
        if not блок or not блок.get("known"):
            почему.append(f"слот {s} не прочитан")
            continue
        середина += блок["total"]
    if почему:
        return {"known": False, "why_not": "; ".join(почему)}
    return {"known": True, "count": хвост + голова + середина}


# ------------------------------------------------------------ приоритет (ComputeBudget)

def извлечь_приоритет(tx: dict) -> dict:
    """Микролампоры на CU и лимит CU из ComputeBudget-инструкций. Сканируются
    и верхнеуровневые, и внутренние инструкции (ComputeBudget на практике
    всегда верхнеуровневый, но лишний просмотр внутренних стоит ноль
    дополнительных запросов). Если jsonParsed instrukцию не расшифровал --
    декодируем сырой base58 сами (тот же случай уже встречался в репозитории,
    solana_batch_fee_change_first_trade.py)."""
    цена = лимит = цена_как = лимит_как = None
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    группы = [msg.get("instructions") or []]
    for g in (tx or {}).get("meta", {}).get("innerInstructions") or []:
        группы.append((g or {}).get("instructions") or [])
    for группа in группы:
        for ix in группа:
            if not isinstance(ix, dict) or ix.get("programId") != COMPUTE_BUDGET_PROGRAM:
                continue
            parsed = ix.get("parsed")
            if isinstance(parsed, dict) and parsed.get("type"):
                info = parsed.get("info") or {}
                if parsed["type"] == "setComputeUnitLimit" and лимит is None:
                    лимит, лимит_как = info.get("units"), "jsonParsed"
                elif parsed["type"] == "setComputeUnitPrice" and цена is None:
                    цена, цена_как = info.get("microLamports"), "jsonParsed"
                continue
            raw_b58 = ix.get("data") if isinstance(ix.get("data"), str) else None
            if not raw_b58:
                continue
            try:
                raw = b58decode(raw_b58)
            except Exception:  # noqa: BLE001
                continue
            if len(raw) >= 5 and raw[0] == 2 and лимит is None:
                лимит, лимит_как = int.from_bytes(raw[1:5], "little"), "raw_decode"
            elif len(raw) >= 9 and raw[0] == 3 and цена is None:
                цена, цена_как = int.from_bytes(raw[1:9], "little"), "raw_decode"
    приор_лампорт = (round(цена * лимит / 1_000_000) if цена is not None and лимит is not None else None)
    return {"compute_unit_price_micro": цена, "compute_unit_limit": лимит,
            "price_source": цена_как, "limit_source": лимит_как,
            "priority_fee_lamports": приор_лампорт}


# ------------------------------------------------------------ tip-аккаунты Sender Max

def проверить_tip_аккаунты() -> dict:
    """2nyhqdwK.../4ACfpUFo... -- tip-аккаунты Helius Sender Max: проверено
    программно по analysis/bloom_own_send.py (TIP_ACCOUNTS), а не принято на
    слово владельца. Второй источник -- снятая страница документации."""
    в_коде = {TIP_2NYHQ in OS.TIP_ACCOUNTS, TIP_4ACFP in OS.TIP_ACCOUNTS}
    страница = C.DATA / "docs" / "helius_sender_max.md"
    на_странице = None
    if страница.exists():
        текст = страница.read_text(encoding="utf-8", errors="replace")
        на_странице = (TIP_2NYHQ in текст) and (TIP_4ACFP in текст)
    return {"are_sender_max_tip_accounts": в_коде == {True},
            "found_in": "analysis/bloom_own_send.py:TIP_ACCOUNTS",
            "confirmed_on_saved_doc_page": на_странице,
            "doc_page": str(страница) if страница.exists() else None}


def найти_чаевые(блоки: dict, tip_accounts: tuple) -> list:
    """Все System-переводы на любой из tip-адресов Sender Max в прочитанных
    блоках -- по ЛЮБОЙ транзакции блока, не только по пяти отслеживаемым:
    чаевые берутся с каждой сделки через этот тариф, это и проверяет
    "берётся ли каждый раз" в вопросе владельца."""
    out = []
    for slot, блок in блоки.items():
        if not блок.get("known"):
            continue
        for i, t in enumerate(блок["transactions"]):
            msg = ((t.get("transaction") or {}).get("message") or {})
            группы = [msg.get("instructions") or []]
            for g in (t.get("meta") or {}).get("innerInstructions") or []:
                группы.append((g or {}).get("instructions") or [])
            for группа in группы:
                for ix in группа:
                    if not isinstance(ix, dict) or ix.get("program") != "system":
                        continue
                    parsed = ix.get("parsed") or {}
                    if not isinstance(parsed, dict) or parsed.get("type") != "transfer":
                        continue
                    info = parsed.get("info") or {}
                    dest = info.get("destination")
                    if dest in tip_accounts:
                        лампорт = info.get("lamports")
                        out.append({"slot": slot, "index": i, "signature": _подпись(t),
                                    "tip_account": dest, "source": info.get("source"),
                                    "lamports": лампорт,
                                    "sol": (лампорт / 1e9) if isinstance(лампорт, (int, float)) else None})
    return out


# ------------------------------------------------------------ адрес B1do...QG5m

def найти_b1do(tx: dict | None, *, префикс: str = B1DO_ПРЕФИКС, суффикс: str = B1DO_СУФФИКС,
                ожидаемые_лампорты: int = B1DO_ОЖИДАЕМЫЕ_ЛАМПОРТЫ) -> dict:
    """Полный адрес в вопросе владельца не дан -- ищем в НАШЕЙ транзакции
    получателя лампортов с адресом такой формы. Выбор по префиксу+суффиксу
    (устойчиво к округлению комиссии), сумма -- для СВЕРКИ с 0.00297688 SOL,
    а не условие отбора: несколько кандидатов -- причина, а не догадка."""
    if tx is None:
        return {"ok": False, "why_not": "нашей транзакции нет ни в одном из прочитанных блоков"}
    ключи = C.account_keys(tx)
    meta = tx.get("meta") or {}
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    кандидаты = []
    for i, addr in enumerate(ключи):
        if not (isinstance(addr, str) and addr.startswith(префикс) and addr.endswith(суффикс)):
            continue
        if i >= len(pre) or i >= len(post):
            continue
        d = int(post[i]) - int(pre[i])
        кандидаты.append({"address": addr, "lamports": d, "sol": d / 1e9, "account_index": i})
    if len(кандидаты) == 1:
        к = dict(кандидаты[0])
        к["matches_expected_amount"] = (к["lamports"] == ожидаемые_лампорты)
        return {"ok": True, "why_not": None, **к}
    if not кандидаты:
        return {"ok": False, "why_not": (f"на цепи нет счёта с префиксом {префикс} и суффиксом "
                                          f"{суффикс} среди счетов нашей транзакции")}
    return {"ok": False, "why_not": f"несколько кандидатов ({len(кандидаты)}) -- адрес не выбран наугад",
            "candidates": кандидаты}


# ------------------------------------------------------------ журналы исполнителя (позиции)

def прочитать_jsonl(путь: Path) -> tuple[list, str | None]:
    if not путь.exists():
        return [], f"файла нет: {путь}"
    try:
        текст = путь.read_text(encoding="utf-8")
    except OSError as exc:
        return [], f"файл не читается: {type(exc).__name__}: {str(exc)[:160]}"
    строки, битых = [], 0
    for line in текст.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            строки.append(json.loads(line))
        except ValueError:
            битых += 1
    if not строки:
        return [], "файл пуст" + (f" ({битых} битых строк)" if битых else "")
    return строки, (f"{битых} битых строк пропущено" if битых else None)


def все_позиции(state_dir: Path) -> tuple[dict, str | None]:
    строки, почему = прочитать_jsonl(state_dir / "positions.jsonl")
    by: dict = {}
    for row in строки:
        cid = row.get("client_order_id")
        if not cid:
            continue
        by.setdefault(cid, {}).update(row)
    return by, (почему if not by else None)


def наше_время_pickaxe(state_dir: Path, подпись: str = ПОДПИСИ["ours"]) -> dict:
    """Ответ Bloom и посадка нашей покупки -- из positions.jsonl, а не
    из блока: там же лежит bloom_ms, посчитанный исполнителем от решения
    до ответа площадки, которого по цепи не увидеть."""
    позиции, почему = все_позиции(state_dir)
    поз = next((p for p in позиции.values() if подпись in (p.get("signatures") or [])), None)
    if поз is None:
        return {"ok": False, "why_not": f"позиции с этой подписью нет в positions.jsonl "
                                          f"({почему or 'такой подписи нет'})"}
    return {"ok": True, "client_order_id": поз.get("client_order_id"),
            "bloom_ms": поз.get("bloom_ms"), "own_tx_seen_ms": поз.get("own_tx_seen_ms"),
            "our_slot": поз.get("our_slot"), "source_slot": поз.get("source_slot"),
            "source_sig": поз.get("source_sig"), "chain_ok": поз.get("chain_ok")}


def подписи_наших_сделок(state_dir: Path) -> tuple[list, str | None]:
    """Подписи ВСЕХ настоящих (не dry-run) наших транзакций: покупки Bloom
    (signatures[0]) и полосы своей отправки (lane_signature/_local) --
    в обеих может стоять чаевый перевод, если он берётся с каждой отправки."""
    позиции, почему = все_позиции(state_dir)
    out, seen = [], set()
    for p in позиции.values():
        if p.get("mode") == "dry-run":
            continue
        for sig in (next(iter(p.get("signatures") or []), None),
                    p.get("lane_signature"), p.get("lane_signature_local")):
            if sig and sig not in seen:
                seen.add(sig)
                out.append({"signature": sig, "client_order_id": p.get("client_order_id"),
                            "ts_intent_utc": p.get("ts_intent_utc"), "lane": bool(p.get("lane"))})
    return out, почему


def сумма_к_адресу_с_даты(rpc_call, подписи: list, адрес: str, *, since: str,
                           лимит: int | None = None) -> dict:
    """Идём по нашим подписям >= since, тянем getTransaction (jsonParsed),
    считаем сумму лампортов, полученных этим адресом. Честный счёт того, что
    не проверено: ошибки узла и пропуски по дате -- отдельными числами, а не
    молча выпадают из суммы."""
    учтено, ошибки, пропущено = [], 0, 0
    запросов = 0
    for row in подписи:
        if since and (row.get("ts_intent_utc") or "") < since:
            пропущено += 1
            continue
        if лимит is not None and запросов >= лимит:
            break
        try:
            tx = rpc_call("getTransaction", [row["signature"], {
                "encoding": "jsonParsed", "maxSupportedTransactionVersion": C.TX_VERSION,
                "commitment": "finalized"}])
            запросов += 1
        except Exception:  # noqa: BLE001 -- одна нечитаемая tx не должна рвать сумму по всем
            ошибки += 1
            continue
        if not tx:
            continue
        d = C.lamport_delta(tx, адрес)
        if d and d > 0:
            учтено.append({"signature": row["signature"], "client_order_id": row.get("client_order_id"),
                            "lamports": d, "sol": d / 1e9})
    всего = sum(x["lamports"] for x in учтено)
    return {"address": адрес, "since": since, "requests_made": запросов,
            "skipped_before_since": пропущено, "errors": ошибки,
            "count_matches": len(учтено), "matches": учтено,
            "total_lamports": всего, "total_sol": всего / 1e9,
            "taken_every_time": (len(учтено) == запросов) if запросов else None}


# ------------------------------------------------------------ счётчик вызовов rpc_call

def rpc_со_счётчиком(rpc_call):
    счёт = {"calls": 0, "by_method": {}}

    def обёртка(method, params):
        счёт["calls"] += 1
        счёт["by_method"][method] = счёт["by_method"].get(method, 0) + 1
        return rpc_call(method, params)
    return обёртка, счёт


# ------------------------------------------------------------ сборка полного разбора

def собрать_разбор(rpc_call, *, слоты: tuple = СЛОТЫ_ПО_УМОЛЧАНИЮ, подписи: dict = ПОДПИСИ,
                    state_dir: Path | None = None, since: str = С_ДАТЫ_ПО_УМОЛЧАНИЮ,
                    лимит_истории: int | None = ЛИМИТ_ИСТОРИИ_ПО_УМОЛЧАНИЮ) -> dict:
    способ = выбрать_способ(len(слоты), len(подписи))
    блоки = получить_блоки(rpc_call, list(слоты))

    строки = {}
    for имя in ПОРЯДОК_ИМЁН:
        sig = подписи[имя]
        поз = позиция_в_блоках(блоки, sig)
        приор = {}
        if поз is not None:
            tx = блоки[поз["slot"]]["transactions"][поз["index"]]
            приор = извлечь_приоритет(tx)
        строки[имя] = {"signature": sig, "position": поз,
                        "why_not": None if поз is not None else почему_нет_позиции(блоки),
                        **приор}

    между = {имя: транзакций_между(блоки, строки["leader"]["position"], строки[имя]["position"])
             for имя in ПОРЯДОК_ИМЁН if имя != "leader"}

    tip_проверка = проверить_tip_аккаунты()
    чаевые = найти_чаевые(блоки, (TIP_2NYHQ, TIP_4ACFP))

    наша_tx = None
    if строки["ours"]["position"] is not None:
        p = строки["ours"]["position"]
        наша_tx = блоки[p["slot"]]["transactions"][p["index"]]
    b1do = найти_b1do(наша_tx)

    if state_dir is None:
        наше_время = {"ok": False, "why_not": "state-dir не задан"}
        b1do_история = {"why_not": "state-dir не задан: история переводов на B1do не проверялась"}
    else:
        наше_время = наше_время_pickaxe(state_dir)
        if b1do.get("ok"):
            подписи_свои, почему_поз = подписи_наших_сделок(state_dir)
            b1do_история = сумма_к_адресу_с_даты(rpc_call, подписи_свои, b1do["address"],
                                                  since=since, лимит=лимит_истории)
            b1do_история["positions_why_not"] = почему_поз
        else:
            b1do_история = {"why_not": f"адрес B1do не найден в нашей транзакции: {b1do.get('why_not')}"}

    блоки_кратко = {s: {"known": b.get("known"), "total": b.get("total"), "why_not": b.get("why_not")}
                     for s, b in блоки.items()}
    return {
        "slots": list(слоты),
        "read_method": способ,
        "blocks": блоки_кратко,
        "signatures": строки,
        "transactions_between_leader_and": между,
        "tip_accounts_check": tip_проверка,
        "tip_transfers_found_in_blocks": чаевые,
        "b1do_account": b1do,
        "b1do_transfers_since": b1do_история,
        "our_bloom_timing": наше_время,
    }


# ------------------------------------------------------------------------- самопроверка

def _cb_parsed(тип: str, поле: str, значение) -> dict:
    return {"programId": COMPUTE_BUDGET_PROGRAM, "parsed": {"type": тип, "info": {поле: значение}}}


def _cb_raw(дискр: int, значение: int, байт: int) -> dict:
    data = bytes([дискр]) + int(значение).to_bytes(байт, "little")
    return {"programId": COMPUTE_BUDGET_PROGRAM, "data": b58encode(data)}


def _transfer(source: str, dest: str, lamports: int) -> dict:
    return {"program": "system", "programId": SYSTEM_PROGRAM,
            "parsed": {"type": "transfer", "info": {"source": source, "destination": dest,
                                                     "lamports": lamports}}}


def _синт_tx(sig: str, *, instrs: list | None = None, keys: list | None = None,
             pre: list | None = None, post: list | None = None) -> dict:
    keys = keys or []
    n = len(keys)
    return {"transaction": {"signatures": [sig],
                             "message": {"accountKeys": [{"pubkey": k} for k in keys],
                                         "instructions": instrs or []}},
            "meta": {"err": None, "preBalances": pre or [0] * n, "postBalances": post or [0] * n,
                     "innerInstructions": []}}


def self_test() -> int:
    checks: list = []

    def chk(name, ok, got=""):
        checks.append((name, bool(ok), got))

    # ---- b58: обратимость собственного кодера/декодера
    сырое = bytes([3]) + (777_777).to_bytes(8, "little")
    chk("b58decode(b58encode(x)) == x", b58decode(b58encode(сырое)) == сырое, сырое.hex())

    # ---- выбор способа чтения по кредитам
    chk("3 слота дешевле 5 подписей -- выбран getBlock",
        выбрать_способ(3, 5) == {"getBlock_credits": 3, "getTransactions_credits": 5, "chosen": "getBlock"},
        выбрать_способ(3, 5))
    chk("2 подписи дешевле 10 слотов -- выбран getTransactions",
        выбрать_способ(10, 2)["chosen"] == "getTransactions", выбрать_способ(10, 2))

    # ---- tip-аккаунты: факт владельца проверен по коду, а не принят на слово
    tip = проверить_tip_аккаунты()
    chk("2nyhqdwK.../4ACfpUFo... -- tip-аккаунты Sender Max по TIP_ACCOUNTS в bloom_own_send.py",
        tip["are_sender_max_tip_accounts"] is True, tip)
    chk("тот же факт подтверждён снятой страницей документации (data/docs/helius_sender_max.md)",
        tip["confirmed_on_saved_doc_page"] is True, tip)

    # ---- синтетическая гонка: три блока, пять отслеживаемых подписей + массовка
    ФИЛЛЕР = lambda s: _синт_tx(s)  # noqa: E731 -- транзакция без ComputeBudget/переводов, для заполнения

    b590 = [ФИЛЛЕР("F1"),
            _синт_tx(ПОДПИСИ["leader"], instrs=[_cb_parsed("setComputeUnitPrice", "microLamports", 5000),
                                                 _cb_parsed("setComputeUnitLimit", "units", 200_000)]),
            ФИЛЛЕР("F2"),
            _синт_tx(ПОДПИСИ["sniper1"], instrs=[_cb_raw(3, 8000, 8)]),
            ФИЛЛЕР("F3")]
    b591 = [ФИЛЛЕР("M1"), ФИЛЛЕР("M2"), ФИЛЛЕР("M3"), ФИЛЛЕР("M4")]
    b592 = [
        ФИЛЛЕР("G1"),
        _синт_tx(ПОДПИСИ["sniper2"]),  # без ComputeBudget вовсе
        _синт_tx("G2_TIP", instrs=[_transfer("GUiSxSourceWalletAAAAAAAAAAAAAAAAAAAAAAAAAA",
                                              TIP_4ACFP, 1_000_000_000)]),
        _синт_tx(ПОДПИСИ["ours"],
                 instrs=[_cb_raw(3, 10_000, 8), _cb_raw(2, 600_000, 4),
                         _transfer("OurWalletAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                                   "B1doMiddlePartOfTheAddressXXXXXXXXXXXQG5m", B1DO_ОЖИДАЕМЫЕ_ЛАМПОРТЫ),
                         _transfer("OurWalletAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                                   "SomeOtherDecoyAddressNotMatchingPattern1111", 12_345)],
                 keys=["OurWalletAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                       "B1doMiddlePartOfTheAddressXXXXXXXXXXXQG5m",
                       "SomeOtherDecoyAddressNotMatchingPattern1111"],
                 pre=[10_000_000_000, 0, 0],
                 post=[10_000_000_000 - B1DO_ОЖИДАЕМЫЕ_ЛАМПОРТЫ - 12_345, B1DO_ОЖИДАЕМЫЕ_ЛАМПОРТЫ, 12_345]),
        _синт_tx("G3_TIP", instrs=[_transfer("7JVQxSourceWalletBBBBBBBBBBBBBBBBBBBBBBBBBB",
                                              TIP_2NYHQ, 240_800_000)]),
        _синт_tx(ПОДПИСИ["dbot"], instrs=[_cb_parsed("setComputeUnitPrice", "microLamports", 100),
                                           _cb_parsed("setComputeUnitLimit", "units", 50_000)]),
    ]
    БЛОКИ_БД = {450191590: b590, 450191591: b591, 450191592: b592}

    вызовов = {"n": 0}

    def fake_rpc(method, params):
        вызовов["n"] += 1
        if method == "getBlock":
            slot = params[0]
            assert params[1]["encoding"] == "jsonParsed" and params[1]["transactionDetails"] == "full"
            if slot not in БЛОКИ_БД:
                return None
            return {"transactions": БЛОКИ_БД[slot]}
        raise AssertionError(f"неожиданный метод в разборе гонки: {method}")

    разбор = собрать_разбор(fake_rpc, слоты=(450191590, 450191591, 450191592))
    chk("прочитаны все три блока одним вызовом getBlock на слот (3 вызова)",
        вызовов["n"] == 3 and all(b["known"] for b in разбор["blocks"].values()), вызовов)
    chk("выбран способ getBlock (3 слота дешевле 5 подписей)",
        разбор["read_method"]["chosen"] == "getBlock", разбор["read_method"])

    s = разбор["signatures"]
    chk("leader найден в слоте 590 на индексе 1",
        s["leader"]["position"] == {"slot": 450191590, "index": 1, "total": 5}, s["leader"]["position"])
    chk("sniper1 найден в слоте 590 на индексе 3",
        s["sniper1"]["position"] == {"slot": 450191590, "index": 3, "total": 5}, s["sniper1"]["position"])
    chk("sniper2 найден в слоте 592 на индексе 1",
        s["sniper2"]["position"] == {"slot": 450191592, "index": 1, "total": 6}, s["sniper2"]["position"])
    chk("ours найден в слоте 592 на индексе 3",
        s["ours"]["position"] == {"slot": 450191592, "index": 3, "total": 6}, s["ours"]["position"])
    chk("dbot найден в слоте 592 на индексе 5",
        s["dbot"]["position"] == {"slot": 450191592, "index": 5, "total": 6}, s["dbot"]["position"])

    chk("приоритет leader из jsonParsed: 5000 микролампор, лимит 200000",
        s["leader"]["compute_unit_price_micro"] == 5000 and s["leader"]["compute_unit_limit"] == 200_000
        and s["leader"]["price_source"] == "jsonParsed", s["leader"])
    chk("приоритет sniper1 из НЕразобранного base58: цена 8000 декодирована вручную",
        s["sniper1"]["compute_unit_price_micro"] == 8000 and s["sniper1"]["price_source"] == "raw_decode"
        and s["sniper1"]["compute_unit_limit"] is None, s["sniper1"])
    chk("у sniper2 нет ComputeBudget вовсе -- оба поля None, а не 0",
        s["sniper2"]["compute_unit_price_micro"] is None and s["sniper2"]["compute_unit_limit"] is None,
        s["sniper2"])
    chk("приоритет ours: цена и лимит из raw_decode, лампорты приоритета посчитаны верно (10000*600000/1e6=6000)",
        s["ours"]["compute_unit_price_micro"] == 10_000 and s["ours"]["compute_unit_limit"] == 600_000
        and s["ours"]["priority_fee_lamports"] == 6000, s["ours"])
    chk("dbot: 100 микролампор на CU, лимит 50000 (самый низкий приоритет из пяти)",
        s["dbot"]["compute_unit_price_micro"] == 100 and s["dbot"]["compute_unit_limit"] == 50_000, s["dbot"])

    между = разбор["transactions_between_leader_and"]
    chk("между leader и sniper1 (тот же блок, indices 1 и 3): 1 транзакция",
        между["sniper1"] == {"known": True, "count": 1}, между["sniper1"])
    chk("между leader и sniper2 (через слот 590 хвост + весь 591 + голова 592): 3+4+1=8",
        между["sniper2"] == {"known": True, "count": 8}, между["sniper2"])
    chk("между leader и ours: 3+4+3=10",
        между["ours"] == {"known": True, "count": 10}, между["ours"])
    chk("между leader и dbot: 3+4+5=12",
        между["dbot"] == {"known": True, "count": 12}, между["dbot"])

    tips = разбор["tip_transfers_found_in_blocks"]
    подходит_2n = [t for t in tips if t["tip_account"] == TIP_2NYHQ]
    подходит_4a = [t for t in tips if t["tip_account"] == TIP_4ACFP]
    chk("найден чаёвый перевод 0.2408 SOL на 2nyhqdwK... от адреса 7JVQ...",
        len(подходит_2n) == 1 and подходит_2n[0]["sol"] == 0.2408
        and подходит_2n[0]["source"].startswith("7JVQ"), подходит_2n)
    chk("найден чаёвый перевод 1 SOL на 4ACfpUFo... от адреса GUiS...",
        len(подходит_4a) == 1 and подходит_4a[0]["sol"] == 1.0
        and подходит_4a[0]["source"].startswith("GUiS"), подходит_4a)

    b1do = разбор["b1do_account"]
    chk("адрес B1do...QG5m найден в нашей транзакции ЦЕЛИКОМ, сумма совпала с 0.00297688 SOL",
        b1do["ok"] is True and b1do["address"] == "B1doMiddlePartOfTheAddressXXXXXXXXXXXQG5m"
        and b1do["matches_expected_amount"] is True, b1do)

    # ---- b1do: неоднозначность и отсутствие -- честные причины
    tx_двое = _синт_tx("AMBIG", keys=["B1doAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQG5m",
                                       "B1doBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBQG5m"],
                        pre=[0, 0], post=[100, 200])
    b1do_двое = найти_b1do(tx_двое)
    chk("два кандидата на адрес B1do -- причина названа, ничего не выбрано наугад",
        b1do_двое["ok"] is False and "несколько кандидатов" in b1do_двое["why_not"], b1do_двое)
    tx_нет = _синт_tx("NOPE", keys=["ДругойАдрес1111111111111111111111111111111"], pre=[0], post=[500])
    b1do_нет = найти_b1do(tx_нет)
    chk("ни одного кандидата -- причина названа, адрес не выдуман",
        b1do_нет["ok"] is False and "нет счёта с префиксом" in b1do_нет["why_not"], b1do_нет)
    chk("наша транзакция не найдена в блоках -- причина об этом, а не про адрес",
        найти_b1do(None)["why_not"] == "нашей транзакции нет ни в одном из прочитанных блоков")

    # ---- транзакции между: неполные блоки честно помечаются
    неполные = {450191590: {"known": True, "total": 5, "transactions": b590},
                450191592: {"known": True, "total": 6, "transactions": b592}}
    между_дыра = транзакций_между(неполные, {"slot": 450191590, "index": 1, "total": 5},
                                   {"slot": 450191592, "index": 3, "total": 6})
    chk("промежуточный блок не прочитан -- счёт НЕ считается, а не занижается",
        между_дыра["known"] is False and "451" not in между_дыра.get("why_not", "")
        and "591" in между_дыра["why_not"], между_дыра)
    chk("позиции нет вовсе -- known=False с причиной",
        транзакций_между({}, None, {"slot": 1, "index": 1, "total": 2})["known"] is False)

    # ---- getBlock падает на одном слоте -- остальные читаются, причина честная
    def fake_rpc_упавший(method, params):
        if method == "getBlock" and params[0] == 450191591:
            raise RuntimeError("узел молчит")
        return fake_rpc(method, params)
    разбор2 = собрать_разбор(fake_rpc_упавший, слоты=(450191590, 450191591, 450191592))
    chk("один слот не прочитан -- блок помечен known=False с причиной, остальные читаются",
        разбор2["blocks"][450191591]["known"] is False
        and "узел молчит" in разбор2["blocks"][450191591]["why_not"]
        and разбор2["blocks"][450191590]["known"] is True, разбор2["blocks"])
    chk("из-за дыры в 591 счёт leader-sniper2 стал неизвестным, а не приблизительным",
        разбор2["transactions_between_leader_and"]["sniper2"]["known"] is False,
        разбор2["transactions_between_leader_and"]["sniper2"])

    # ---- журналы: подписи наших сделок, время PICKAXE, сумма к B1do с 24.09
    import tempfile
    tmp = Path(tempfile.mkdtemp())

    def записать(имя, строки):
        with (tmp / имя).open("w", encoding="utf-8") as f:
            for r in строки:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    ПОЗ_PICKAXE = {"client_order_id": "cP", "mode": "live", "mint": "M",
                   "signatures": [ПОДПИСИ["ours"]], "bloom_ms": 61.4, "our_slot": 450191592,
                   "source_slot": 450191590, "source_sig": "SRC_LEADER", "chain_ok": True,
                   "ts_intent_utc": "2026-09-25T09:00:00Z"}
    ПОЗ_РАННЯЯ = {"client_order_id": "cE", "mode": "live", "mint": "M2",
                  "signatures": ["EARLYSIG"], "ts_intent_utc": "2026-09-20T00:00:00Z"}
    ПОЗ_ПОЗДНЯЯ = {"client_order_id": "cL", "mode": "live", "mint": "M3",
                   "signatures": ["LATESIG"], "ts_intent_utc": "2026-09-25T10:00:00Z"}
    ПОЗ_СУХАЯ = {"client_order_id": "cD", "mode": "dry-run", "mint": "M4", "signatures": ["DRYSIG"],
                 "ts_intent_utc": "2026-09-25T10:00:00Z"}
    ПОЗ_ПОЛОСА = {"client_order_id": "cW", "mode": "live", "lane": "own_send",
                  "lane_signature": "LANESIG", "ts_intent_utc": "2026-09-25T10:00:00Z"}
    записать("positions.jsonl", [ПОЗ_PICKAXE, ПОЗ_РАННЯЯ, ПОЗ_ПОЗДНЯЯ, ПОЗ_СУХАЯ, ПОЗ_ПОЛОСА])

    время = наше_время_pickaxe(tmp)
    chk("наше время PICKAXE взято из positions.jsonl: bloom_ms и наш слот",
        время["ok"] is True and время["bloom_ms"] == 61.4 and время["our_slot"] == 450191592, время)
    chk("время для чужой подписи -- честная причина",
        наше_время_pickaxe(tmp, "НЕТУ")["ok"] is False)

    подписи_ист, _ = подписи_наших_сделок(tmp)
    имена_подписей = {r["signature"] for r in подписи_ист}
    chk("подписи наших сделок: Bloom + полоса, dry-run исключён",
        имена_подписей == {ПОДПИСИ["ours"], "EARLYSIG", "LATESIG", "LANESIG"}, имена_подписей)

    АДРЕС_B1DO_ТЕСТ = "B1doMiddlePartOfTheAddressXXXXXXXXXXXQG5m"

    def fake_gettx(method, params):
        assert method == "getTransaction"
        sig = params[0]
        if sig == ПОДПИСИ["ours"]:
            return _синт_tx(sig, keys=[АДРЕС_B1DO_ТЕСТ], pre=[0], post=[B1DO_ОЖИДАЕМЫЕ_ЛАМПОРТЫ])
        if sig == "LATESIG":
            return _синт_tx(sig, keys=[АДРЕС_B1DO_ТЕСТ], pre=[0], post=[B1DO_ОЖИДАЕМЫЕ_ЛАМПОРТЫ])
        if sig == "LANESIG":
            raise RuntimeError("узел не отдал")
        raise AssertionError(f"не должны были спрашивать {sig}: раньше --since")

    сумма = сумма_к_адресу_с_даты(fake_gettx, подписи_ист, АДРЕС_B1DO_ТЕСТ,
                                   since="2026-09-24T00:00:00Z")
    chk("EARLYSIG (до --since) не запрашивался вовсе",
        сумма["skipped_before_since"] == 1, сумма)
    chk("тик получен по двум сделкам (ours и LATESIG), сумма = 2*0.00297688 SOL, одна ошибка узла",
        сумма["count_matches"] == 2 and abs(сумма["total_sol"] - 2 * (B1DO_ОЖИДАЕМЫЕ_ЛАМПОРТЫ / 1e9)) < 1e-9
        and сумма["errors"] == 1, сумма)

    # ---- state_dir=None -- секции про B1do-историю и время честно пропущены
    разбор3 = собрать_разбор(fake_rpc, слоты=(450191590, 450191591, 450191592), state_dir=None)
    chk("без --state-dir время и история B1do явно помечены как непроверенные",
        разбор3["our_bloom_timing"]["why_not"] == "state-dir не задан"
        and "не задан" in разбор3["b1do_transfers_since"]["why_not"], разбор3["our_bloom_timing"])

    # ---- конец-в-конец со state_dir: полный разбор + счётчик кредитов
    rpc_общий, счётчик = rpc_со_счётчиком(lambda m, p: (fake_rpc(m, p) if m == "getBlock"
                                                         else fake_gettx(m, p)))
    разбор4 = собрать_разбор(rpc_общий, слоты=(450191590, 450191591, 450191592), state_dir=tmp,
                              since="2026-09-24T00:00:00Z")
    chk("сквозной прогон: 3 getBlock + 3 getTransaction (ours, LATESIG, LANESIG упавший; "
        "EARLYSIG пропущен по дате)",
        счётчик["by_method"].get("getBlock") == 3 and счётчик["by_method"].get("getTransaction") == 3
        and счётчик["calls"] == 6, счётчик)
    chk("в сквозном прогоне сумма к B1do и время PICKAXE тоже собраны",
        разбор4["b1do_transfers_since"]["count_matches"] == 2
        and разбор4["our_bloom_timing"]["ok"] is True, разбор4["b1do_transfers_since"])

    # ---- модуль не ходит в сеть напрямую: только через переданный rpc_call.
    # Смотрим ТОЛЬКО код до self_test: сама эта проверка ниже упоминает
    # искомые строки как текст, и поиск по всему файлу находил бы сам себя.
    исходник_модуля = Path(__file__).read_text(encoding="utf-8").split("def self_test")[0]
    chk("модуль (без кода самопроверки) не импортирует requests и не ходит к хосту Helius напрямую",
        "import requests" not in исходник_модуля and "helius-rpc.com" not in исходник_модуля.lower()
        and "api.helius" not in исходник_модуля.lower())
    import re
    вызовы_методов = set(re.findall(r'rpc_call\(\s*"([A-Za-z]+)"', исходник_модуля))
    chk(f"все вызовы узла в модуле идут через rpc_call(...): {sorted(вызовы_методов)}",
        вызовы_методов <= {"getBlock", "getTransaction"} and bool(вызовы_методов), вызовы_методов)

    bad = 0
    for name, ok, got in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {name}" + (f"  -> {str(got)[:300]}" if not ok else ""))
        bad += (not ok)
    print(f"самопроверка night_p2_chain_pickaxe: {len(checks) - bad}/{len(checks)} пройдено")
    return 0 if bad == 0 else 1


# ------------------------------------------------------------------------- CLI

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="data/night_p2.json")
    p.add_argument("--state-dir", default=КАТАЛОГ_ПО_УМОЛЧАНИЮ)
    p.add_argument("--since", default=С_ДАТЫ_ПО_УМОЛЧАНИЮ)
    p.add_argument("--slots", default=",".join(str(s) for s in СЛОТЫ_ПО_УМОЛЧАНИЮ))
    p.add_argument("--max-history-tx", type=int, default=ЛИМИТ_ИСТОРИИ_ПО_УМОЛЧАНИЮ,
                   help="предохранитель кредитов на подсчёт переводов к B1do с --since")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test:
        return self_test()

    слоты = tuple(int(x) for x in a.slots.split(",") if x.strip())
    try:
        rpc = C.C2Rpc("c2_night_p2_pickaxe")
    except Exception as exc:  # noqa: BLE001
        итог = {"ok": False, "why_not": f"клиент узла не создан: {type(exc).__name__}: {str(exc)[:200]}"}
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(итог, ensure_ascii=False, indent=2), encoding="utf-8")
        print(итог["why_not"])
        return 1

    rpc_call, счётчик = rpc_со_счётчиком(rpc.call)
    разбор = собрать_разбор(rpc_call, слоты=слоты, state_dir=Path(a.state_dir), since=a.since,
                             лимит_истории=a.max_history_tx)
    итог = {"ok": True, "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "credits_used": счётчик["calls"], "credits_by_method": счётчик["by_method"],
            **разбор}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(итог, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"способ чтения: {разбор['read_method']['chosen']}, потрачено кредитов: {счётчик['calls']}")
    for имя in ПОРЯДОК_ИМЁН:
        s = разбор["signatures"][имя]
        поз = s["position"]
        print(f"  {имя}: {'слот %s индекс %s из %s' % (поз['slot'], поз['index'], поз['total']) if поз else s['why_not']}"
              f", цена {s.get('compute_unit_price_micro')} мкл/CU")
    print(f"B1do: {разбор['b1do_account']}")
    print(f"tip-аккаунты Sender Max подтверждены: {разбор['tip_accounts_check']['are_sender_max_tip_accounts']}")
    print(f"записано в {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
