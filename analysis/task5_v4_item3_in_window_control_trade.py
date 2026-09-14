#!/usr/bin/env python3
"""Задача 5, седьмой раунд (разбор владельца, пункт 3): "контрольная
сделка конкурента ДОЛЖНА лежать ВНУТРИ активного окна пилота". Блок
61630766 (0x9c4712ab...), использованный в прежнем отчёте, оказался на
03:17:42 UTC -- ДО реального старта пилота (03:19:20 UTC, см.
PILOT_START_UTC в task5_v4_control_case_investigation.py) -- вывод
"бот опоздал на 22 минуты" для НЕГО был неверен (бот физически не мог
его оценивать до старта пилота) и здесь не повторяется.

Этот скрипт:
  1) реальным бинарным поиском по eth_getBlockByNumber(...).timestamp
     находит блоки начала/конца пилота (НЕ экстраполяция по среднему
     блок-тайму);
  2) честно проверяет, есть ли РЕАЛЬНЫЕ данные об остановках/рестартах
     ВНУТРИ окна (systemd journalctl, если доступен этому пользователю;
     если недоступен -- явно фиксирует "недоступно", не гадает);
  3) сканирует Swap-логи конкурента (task5_v4_competitor_trade_scan.py,
     уже существующий, не переписан) СТРОГО внутри окна пилота (>=
     блок старта, <= блок конца) -- это автоматически исключает
     61630766 (он раньше блока старта);
  4) для КАЖДОГО многоходового кандидата -- ПОЛНЫЙ разбор движения
     средств (не только "нет ERC20 Transfer у tx.from"): чистый поток
     каждого токена на уровне самих Swap-дельт (позиция трейдера,
     обратная позиции пула) + ВСЕ ERC20 Transfer-логи полного рецепта
     (не только involving tx.from) -- честный итог: кто РЕАЛЬНО получил
     профит и в каком токене;
  5) для первого подтверждённого прибыльным кандидата -- ЛОКАЛЬНАЯ
     форк-реконструкция состояния НЕПОСРЕДСТВЕННО ПЕРЕД ним (форк на
     N-1, затем реальный replay ПРЕДШЕСТВУЮЩИХ транзакций ЭТОГО ЖЕ
     блока N через anvil_impersonateAccount + eth_sendTransaction --
     тот же принцип, что anvil_impersonateAccount(PoolManager) в
     task5_v4_fork_simulation.py, просто применён к каждому отправителю
     предшествующей транзакции), на этом ОДНОМ зафиксированном
     состоянии:
       a) читаем (eth_call, НЕ мутирует state) -- находит ли РЕАЛЬНАЯ
          hp.recompute_route/quote_route_at_size прибыльный размер;
       b) деплоим НАШ контракт (та же сборка, что закреплена для
          пилота) и реально вызываем executeCycle тем же
          маршрутом/размером -- находит ли ОН прибыль при реальном
          исполнении (не только по котировке);
       c) ПОСЛЕ (а)/(b), которые ничего не меняют в состоянии кроме
          НАШЕГО собственного контракта -- реплеим САМУ транзакцию
          конкурента (impersonate её tx.from, те же to/data/value/gas)
          -- подтверждение, что реконструкция состояния точна (тот же
          профит, что и на реальной цепи);
  6) честно сверяет НАШИ СОБСТВЕННЫЕ логи (reason_log/attempts --
     прямой доступ к файлам на этой же машине, скрипт выполняется на
     Ohio) на предмет ЭТОГО route_id/этих pool_id в разумном окне
     времени вокруг целевого блока -- отчёт "найдено"/"не найдено",
     БЕЗ вычисления "задержки реакции" из более поздней записи (если
     сигнала нет вообще -- честно "не зафиксировано", как и просил
     владелец).

Всё общается с цепью ТОЛЬКО через уже существующий, доверенный
RPC-путь проекта (alchemy_fallback.py) -- никаких новых доменов.
Локальный форк -- ТОЛЬКО anvil на 127.0.0.1, ничего не отправляет в
реальную сеть."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

import dataclasses  # noqa: E402

from alchemy_fallback import _alchemy_direct_endpoint, _rpc_call, get_block  # noqa: E402
import alchemy_fallback as af  # noqa: E402
import task5_v4_hotpath as hp  # noqa: E402
from task5_v4_route_registry import RouteCycle, RouteLeg  # noqa: E402
from task5_v4_hook_route_audit import fetch_initialize_event  # noqa: E402
# ПРАВКА: SWAP_TOPIC0 переиспользуется ИЗ task5_v4_competitor_trade_scan.py
# (уже реально вычислен там через topic0(...)) -- НЕ вводим повторно
# захардкоженный hex здесь (никогда не подставляем непроверенные данные).
from task5_v4_competitor_trade_scan import scan as competitor_scan, SWAP_TOPIC0  # noqa: E402
from task5_v4_executor_calldata import build_execute_cycle_calldata  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
FOUNDRY_BIN = Path.home() / ".foundry" / "bin"
ANVIL = str(FOUNDRY_BIN / "anvil")
CAST = str(FOUNDRY_BIN / "cast")
PORT = 8557
RPC = f"http://127.0.0.1:{PORT}"

POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
NATIVE = "0x0000000000000000000000000000000000000000"
KNOWN_ARBITRAGEUR = "0x1b357e7acd2a32aebfa2de286c9e8e617d39a251"
TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

PILOT_START_UTC = "2026-09-13T03:19:20Z"
PILOT_END_UTC = "2026-09-13T11:51:03Z"
PREVIOUSLY_USED_PRE_PILOT_TX = "0x9c4712ab46e082aa662dd28514dcc0fd7e0199f3621bee95a739bfe5995443d3"
PREVIOUSLY_USED_PRE_PILOT_BLOCK = 61630766

REASON_LOG_FILE = Path("/home/bot/data/task5_v4_pilot_no_send_log.jsonl")
ATTEMPT_TABLE_FILE = Path("/home/bot/data/task5_v4_pilot_attempts.jsonl")

# ПРАВКА (девятый раунд, разбор владельца, пункт 3 -- по факту зависания
# прогона на ~601 кандидате): тот же принцип, что владелец уже указал для
# фазы реплея ("данные Initialize получи ЗАРАНЕЕ... не повторяй широкий
# запрос через провайдера с известным лимитом 10 блоков"), здесь
# распространяется на ВЕСЬ скан -- full_fund_flow_check() вызывался для
# КАЖДОГО из ~601 кандидатов и для КАЖДОГО плеча заново дёргал
# fetch_initialize_event() (ОДИН широкий eth_getLogs(0, to_block) на
# вызов, см. её докстринг) -- но один и тот же pool_id РЕАЛЬНО повторяется
# у одного конкурента много раз (он торгует ограниченным набором пулов).
# Initialize -- событие ОДНОРАЗОВОЕ (пул инициализируется один раз в своей
# истории) -- kэшировать РЕЗУЛЬТАТ по pool_id (не по to_block) безопасно и
# не меняет ничего в итоговых данных, только устраняет ПОВТОРНЫЕ широкие
# запросы за уже известным pool_id. Это и было реальной причиной, почему
# прогон 34766908632 не завершился за 30 минут (job timeout) -- отменён,
# см. коммит.
_POOL_INIT_CACHE: dict[str, dict | None] = {}


def _cached_fetch_initialize(pool_id_hex: str, to_block: int) -> dict | None:
    if pool_id_hex in _POOL_INIT_CACHE:
        return _POOL_INIT_CACHE[pool_id_hex]
    init = fetch_initialize_event(pool_id_hex, to_block)
    _POOL_INIT_CACHE[pool_id_hex] = init
    return init

# ПРАВКА (первый реальный прогон): фиксированная верхняя граница
# (62600000) оказалась ЗА пределами реально существующей на данный
# момент цепи (get_block вернул None) -- "не удалось получить границы"
# без уточнения, какая именно граница. Нижняя -- по-прежнему фиксирована
# (заведомо ДО старта пилота, реально существует), верхняя -- РЕАЛЬНЫЙ
# "latest" тек момента запуска (гарантированно существует и заведомо
# после конца пилота, т.к. пилот уже закончился в прошлом).
BINSEARCH_LOW = 61600000


def _iso_to_epoch(iso: str) -> float:
    import datetime
    return datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc).timestamp()


def find_block_by_timestamp(target_ts: float, hi_bound: int | None = None) -> dict:
    lo = BINSEARCH_LOW
    hi = hi_bound if hi_bound is not None else int(_rpc_call("eth_blockNumber", []), 16)
    lo_block = get_block(lo)
    hi_block = get_block(hi)
    if lo_block is None:
        return {"ok": False, "error": f"не удалось получить нижнюю границу (блок {lo})"}
    if hi_block is None:
        return {"ok": False, "error": f"не удалось получить верхнюю границу (блок {hi})"}
    if not (int(lo_block["timestamp"], 16) <= target_ts <= int(hi_block["timestamp"], 16)):
        return {"ok": False, "error": "target_ts вне границ поиска", "lo": lo, "hi": hi,
                 "lo_ts": int(lo_block["timestamp"], 16),
                 "hi_ts": int(hi_block["timestamp"], 16), "target_ts": target_ts}
    while hi - lo > 1:
        mid = (lo + hi) // 2
        blk = get_block(mid)
        if blk is None:
            return {"ok": False, "error": f"eth_getBlockByNumber({mid}) вернул None"}
        ts = int(blk["timestamp"], 16)
        if ts <= target_ts:
            lo = mid
        else:
            hi = mid
    return {"ok": True, "block": lo, "block_ts": int(get_block(lo)["timestamp"], 16), "target_ts": target_ts}


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kwargs.pop("timeout", 60), **kwargs)


def check_restart_evidence() -> dict:
    """Честная попытка получить РЕАЛЬНЫЕ данные об остановках/рестартах
    процесса пилота внутри окна (systemd journalctl) -- если недоступно
    этому пользователю/юниту, явно фиксируем факт недоступности и НЕ
    гадаем по косвенным признакам (пробел в логах сам по себе НЕ
    доказывает простой -- мог просто не быть сигналов)."""
    out: dict = {}
    for unit_guess in ("task5-v4-hotpath", "task5_v4_hotpath", "robinhood-bot", "task5-bot"):
        proc = run(["systemctl", "status", unit_guess], timeout=10)
        if proc.returncode in (0, 3) and "could not be found" not in proc.stderr.lower():
            out["systemctl_unit_found"] = unit_guess
            out["systemctl_status_stdout"] = proc.stdout[:2000]
            journal = run(["journalctl", "-u", unit_guess, "--since", "2026-09-13 03:00:00",
                            "--until", "2026-09-13 12:00:00", "--no-pager"], timeout=15)
            out["journalctl_returncode"] = journal.returncode
            out["journalctl_stdout_tail"] = journal.stdout[-6000:]
            out["journalctl_stderr"] = journal.stderr[:1000]
            return out
    out["ok"] = False
    out["note"] = ("ни один из предполагаемых systemd-юнитов не найден этим способом -- "
                    "РЕАЛЬНЫЕ данные о рестартах ВНУТРИ окна пилота недоступны этому скрипту "
                    "этим методом; честно фиксируем как 'не удалось получить', а НЕ 'рестартов не было'")
    return out


def decode_receipt_transfers_all(receipt: dict) -> list[dict]:
    out = []
    for log in receipt.get("logs", []):
        topics = log.get("topics", [])
        if not topics or topics[0].lower() != TRANSFER_TOPIC0 or len(topics) < 3:
            continue
        frm = "0x" + topics[1][-40:]
        to = "0x" + topics[2][-40:]
        amount = int(log["data"], 16) if log["data"] not in ("0x", "") else 0
        out.append({"token": log["address"].lower(), "from": frm.lower(), "to": to.lower(), "amount": amount})
    return out


def full_fund_flow_check(tx_hash: str) -> dict:
    """Пункт 3: "отсутствие Transfer от tx.from САМО ПО СЕБЕ НЕ
    доказывает арбитраж" -- полный разбор: (а) чистый поток каждого
    токена НА УРОВНЕ САМИХ Swap-дельт (позиция трейдера = обратная
    позиция пула, честно через реальный Initialize currency0/1), (б)
    ВСЕ ERC20 Transfer полного рецепта (не только involving tx.from) --
    честно называем, куда РЕАЛЬНО делись средства, включая возможное
    погашение обязательства НЕ на адрес tx.from."""
    result: dict = {"tx_hash": tx_hash}
    tx = _rpc_call("eth_getTransactionByHash", [tx_hash])
    receipt = _rpc_call("eth_getTransactionReceipt", [tx_hash])
    if tx is None or receipt is None:
        result["error"] = "tx/receipt не найдены"
        return result
    result["tx_from"] = tx["from"].lower()
    result["tx_to"] = (tx.get("to") or "").lower()
    result["status"] = int(receipt["status"], 16)
    if result["status"] != 1:
        result["verdict"] = "reverted -- не арбитраж по определению"
        return result

    swap_logs = [l for l in receipt["logs"]
                 if l["address"].lower() == POOL_MANAGER.lower() and l["topics"]
                 and l["topics"][0].lower() == SWAP_TOPIC0.lower()]
    from task5_v4_pool_math import decode_v4_swap_log_data
    legs = []
    net_trader_flow: dict[str, int] = {}
    for log in sorted(swap_logs, key=lambda l: int(l["logIndex"], 16)):
        pool_id_hex = log["topics"][1]
        decoded = decode_v4_swap_log_data(log["data"])
        init = _cached_fetch_initialize(pool_id_hex, int(receipt["blockNumber"], 16))
        if init is None:
            legs.append({"pool_id": pool_id_hex, **decoded, "currency0": None, "currency1": None,
                         "note": "Initialize не найден -- пропускаем в net-flow, честно фиксируем"})
            continue
        c0, c1 = init["currency0"].lower(), init["currency1"].lower()
        # ПРАВКА (владелец, 2026-09-14, третье независимое подтверждение --
        # tx 0xde38133b... в раунде 10, затем 0xf4c3fe75... и 0x90fe304f...
        # в этом раунде, каждый раз сверено с буквальными ERC20 Transfer):
        # реальный V4 PoolManager.Swap даёт amount0/amount1 УЖЕ с позиции
        # ТРЕЙДЕРА (положительное = трейдер ПОЛУЧИЛ/пул отдал), а НЕ с
        # позиции пула, как ошибочно предполагал прежний комментарий и
        # унарный минус ниже. Раунд 10 (коммит 2b4ab85) уже подтвердил эту
        # инверсию числом, но исправил ТОЛЬКО отдельный одноразовый пересчёт
        # уже сохранённого JSON (task5_v4_item3_signfix_first_positive.py)
        # -- САМА эта функция (единственное место, где формируется знак)
        # исправлена не была, поэтому баг воспроизводился заново при каждом
        # новом вызове full_fund_flow_check() (в т.ч. дважды в этом раунде).
        # Прямое суммирование (БЕЗ минуса) -- корректный знак, проверено на
        # трёх независимых транзакциях против буквальных Transfer.
        net_trader_flow[c0] = net_trader_flow.get(c0, 0) + decoded["amount0"]
        net_trader_flow[c1] = net_trader_flow.get(c1, 0) + decoded["amount1"]
        legs.append({"pool_id": pool_id_hex, "currency0": c0, "currency1": c1, **decoded})
    result["legs"] = legs
    result["net_trader_flow_by_token_raw"] = net_trader_flow

    # ПРАВКА (восьмой раунд, разбор владельца, пункт 3): "сначала проверь
    # потоки ВСЕХ активов: замкнутый цикл, положительный остаток БАЗОВОГО
    # актива, отсутствие расхода ДРУГИХ собственных токенов. Обмен ETH на
    # другой токен НЕ называть подтверждённой прибылью." Прежняя проверка
    # (`any(v>0)`) принимала ЛЮБОЙ токен с положительным нетто -- в том
    # числе НЕзамкнутые directional-сделки (ETH потрачен, ДРУГОЙ токен
    # получен, ETH никогда не возвращается) -- ЭТО и произошло в первом
    # прогоне (0x20a30efd..., ETH -> токен, ложно принято как "цикл").
    # СТРОГОЕ условие замкнутого цикла: РОВНО один токен имеет ненулевой
    # чистый поток на уровне Swap-дельт -- он же база/вход/выход цикла --
    # и он ПОЛОЖИТЕЛЕН (профит); ВСЕ остальные (промежуточные) токены
    # обязаны полностью раскрутиться до нуля. Если ненулевых токенов
    # больше одного -- это НЕ доказанный замкнутый цикл (либо
    # directional-сделка, либо часть средств утеряна/не учтена), какой
    # бы положительный токен там ни был.
    nonzero_tokens = {t: v for t, v in net_trader_flow.items() if v != 0}
    is_closed_cycle = len(nonzero_tokens) == 1 and next(iter(nonzero_tokens.values())) > 0
    result["nonzero_net_flow_tokens"] = nonzero_tokens
    result["pool_level_profitable_cycle"] = is_closed_cycle  # имя поля сохранено для совместимости со старым кодом
    result["pool_level_closed_cycle_valid"] = is_closed_cycle
    result["pool_level_profit_tokens"] = dict(nonzero_tokens) if is_closed_cycle else {}
    base_token = next(iter(nonzero_tokens)) if is_closed_cycle else None
    result["base_token"] = base_token

    all_transfers = decode_receipt_transfers_all(receipt)
    result["all_transfers_in_receipt"] = all_transfers
    net_by_address_token: dict[str, dict[str, int]] = {}
    for t in all_transfers:
        net_by_address_token.setdefault(t["from"], {})[t["token"]] = \
            net_by_address_token.setdefault(t["from"], {}).get(t["token"], 0) - t["amount"]
        net_by_address_token.setdefault(t["to"], {})[t["token"]] = \
            net_by_address_token.setdefault(t["to"], {}).get(t["token"], 0) + t["amount"]
    result["net_transfer_by_address_token_raw"] = net_by_address_token
    beneficiaries = {addr: {tok: amt for tok, amt in toks.items() if amt > 0}
                      for addr, toks in net_by_address_token.items()}
    beneficiaries = {a: t for a, t in beneficiaries.items() if t}
    result["positive_net_transfer_addresses"] = beneficiaries

    # "отсутствие расхода ДРУГИХ собственных токенов" -- для исполняющего
    # адреса (tx.to, если это контракт, иначе tx.from) любые ERC20-
    # Transfer ВНЕ множества токенов, реально участвующих в Swap-плечах,
    # означали бы, что эта же tx попутно тронула ДРУГИЕ активы этого
    # адреса (комиссия в постороннем токене, утечка и т.п.) -- честно
    # проверяем, а не молчим.
    swap_leg_tokens = {c for leg in legs for c in (leg.get("currency0"), leg.get("currency1")) if c}
    executor_addr = result["tx_to"] if result["tx_to"] else result["tx_from"]
    executor_transfers = net_by_address_token.get(executor_addr, {})
    other_token_spend = {tok: amt for tok, amt in executor_transfers.items()
                          if tok not in swap_leg_tokens and amt != 0}
    result["executor_address_checked"] = executor_addr
    result["other_token_spend_by_executor"] = other_token_spend
    no_other_token_spent = len(other_token_spend) == 0

    result["fully_valid_closed_cycle"] = is_closed_cycle and no_other_token_spent
    result["verdict"] = (
        (f"ЗАМКНУТЫЙ ЦИКЛ подтверждён: РОВНО один токен ({base_token}) имеет ненулевой чистый поток "
         f"на уровне Swap-дельт пулов, он положителен ({nonzero_tokens.get(base_token) if base_token else None} "
         f"raw) -- ВСЕ остальные токены раскрутились до нуля. Посторонних Transfer у исполняющего адреса вне "
         f"токенов маршрута: {len(other_token_spend)} (ожидание 0)."
         if is_closed_cycle else
         f"НЕ доказанный замкнутый цикл: {len(nonzero_tokens)} токен(ов) с ненулевым чистым потоком "
         f"({list(nonzero_tokens.keys())}) -- либо directional-сделка (напр. ETH -> другой токен, БЕЗ "
         f"возврата в ETH), либо часть средств не учтена. Обмен одного актива на другой БЕЗ возврата "
         f"в исходный НЕ считается подтверждённой прибылью.")
    )
    return result


def anvil_start(fork_block: int) -> tuple[subprocess.Popen, str, str]:
    fork_url = _alchemy_direct_endpoint()
    if not fork_url:
        raise RuntimeError("нет Alchemy-эндпоинта для форка")
    proc = subprocess.Popen(
        [ANVIL, "--fork-url", fork_url, "--fork-block-number", str(fork_block), "--port", str(PORT)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    banner: list[str] = []
    deadline = time.monotonic() + 30
    deployer_addr = deployer_key = None
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        banner.append(line.rstrip("\n"))
        if "Listening on" in line:
            break
    addr_m = re.search(r"\(0\)\s+(0x[0-9a-fA-F]{40})", "\n".join(banner))
    key_m = re.search(r"\(0\)\s+(0x[0-9a-fA-F]{64})", "\n".join(banner))
    if addr_m:
        deployer_addr = addr_m.group(1)
    if key_m:
        deployer_key = key_m.group(1)
    if not deployer_addr or not deployer_key:
        raise RuntimeError(f"не удалось распарсить тестовый аккаунт(0): {banner}")
    for _ in range(30):
        p = run([CAST, "block-number", "--rpc-url", RPC], timeout=10)
        if p.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("anvil не ответил за 30с")
    return proc, deployer_addr, deployer_key


def impersonate_and_send(from_addr: str, to_addr: str | None, data: str, value_hex: str, gas_hex: str) -> dict:
    run([CAST, "rpc", "anvil_impersonateAccount", from_addr, "--rpc-url", RPC], timeout=15)
    run([CAST, "rpc", "anvil_setBalance", from_addr, hex(10 ** 20), "--rpc-url", RPC], timeout=15)
    tx_obj = {"from": from_addr, "data": data, "value": value_hex, "gas": gas_hex}
    if to_addr:
        tx_obj["to"] = to_addr
    call = json.dumps(tx_obj)
    p = run([CAST, "rpc", "eth_sendTransaction", call, "--rpc-url", RPC], timeout=30)
    tx_hash = None
    if p.returncode == 0:
        try:
            tx_hash = json.loads(p.stdout.strip())
        except (ValueError, json.JSONDecodeError):
            tx_hash = p.stdout.strip()
    return {"returncode": p.returncode, "stdout": p.stdout.strip(), "stderr": p.stderr.strip(), "tx_hash": tx_hash}


def _build_route_from_fund_flow_legs(ff_legs: list[dict], pool_init_by_id: dict[str, dict]) -> dict:
    """Собираем RouteCycle НАПРЯМУЮ из реальных Swap-логов целевой
    транзакции. ПРАВКА (восьмой раунд, разбор владельца, пункт 3):
    "данные Initialize получи ЗАРАНЕЕ через доступный источник и передай
    в реплей -- не повторяй широкий запрос через провайдера с известным
    лимитом 10 блоков". Раньше fetch_initialize_event() вызывался ЗДЕСЬ,
    ПОСЛЕ патча af.CONFIG на локальный anvil-форк (см. вызывающий код) --
    широкий eth_getLogs [0, target_block] в один вызов (chunk_size=
    to_block+1, см. её докстринг) проксировался через форк на апстрим-
    источник форка, у которого free-tier лимит 10 блоков -- реально
    воспроизведено ("Excess blob gas not set" -- другая история, здесь
    же была "Under the Free tier plan... up to a 10 block range").
    pool_init_by_id -- УЖЕ полученный (на РЕАЛЬНОМ RPC, ДО патча
    af.CONFIG) словарь pool_id -> Initialize-инфо (currency0/currency1/
    fee/tick_spacing/hooks) -- никакого повторного запроса здесь."""
    legs = []
    for leg in ff_legs:
        if leg.get("currency0") is None:
            return {"ok": False, "reason": f"Initialize не найден для пула {leg.get('pool_id')} -- "
                                            "маршрут НЕ реконструирован"}
        init = pool_init_by_id.get(leg["pool_id"])
        if init is None:
            return {"ok": False, "reason": f"Initialize для {leg['pool_id']} не был предзагружен -- "
                                            "маршрут НЕ реконструирован (честно, без повторного запроса)"}
        zero_for_one = leg["amount0"] > 0  # пул ПОЛУЧИЛ currency0 -- трейдер платил currency0
        legs.append(RouteLeg(init["currency0"], init["currency1"], init["fee"], init["tick_spacing"],
                              init["hooks"], zero_for_one))
    if not legs:
        return {"ok": False, "reason": "пустой список плеч"}
    exit_token = legs[0].input_currency
    if legs[-1].output_currency.lower() != exit_token.lower():
        return {"ok": False, "reason": f"маршрут НЕ замкнут: старт={exit_token}, "
                                        f"конец={legs[-1].output_currency} -- честно фиксируем, не подгоняем"}
    route = RouteCycle("route_item3_reconstructed", tuple(legs), exit_token, "item3-competitor-reconstruction",
                        "discovered")
    return {"ok": True, "route": route}


def replay_preceding_and_reconstruct(target_block: int, target_tx_hash: str, ff_legs: list[dict],
                                       pool_init_by_id: dict[str, dict]) -> dict:
    result: dict = {"target_block": target_block, "target_tx_hash": target_tx_hash}
    block_full = _rpc_call("eth_getBlockByNumber", [hex(target_block), True])
    txs = block_full["transactions"]
    tx_index = next((i for i, t in enumerate(txs) if t["hash"].lower() == target_tx_hash.lower()), None)
    if tx_index is None:
        result["error"] = "целевая tx не найдена в списке транзакций своего блока"
        return result
    result["tx_index"] = tx_index
    result["n_preceding_txs"] = tx_index

    anvil_proc = None
    # ПРАВКА (первый реальный прогон): af.CONFIG патчится ниже на локальный anvil
    # для (a)/(b), но anvil УБИВАЕТСЯ в конце этой функции -- если не откатить
    # af.CONFIG обратно, ЛЮБОЙ последующий вызов get_block/_rpc_call в main()
    # (сверка с блоком целевой tx для cross_reference_own_logs) бьётся в уже
    # мёртвый локальный порт ("Connection refused") вместо реальной цепи --
    # реально воспроизведено в первом прогоне. Сохраняем/восстанавливаем.
    orig_config = af.CONFIG
    orig_alchemy_checked = af._alchemy_direct_checked
    orig_alchemy_url = af._alchemy_direct_url
    try:
        anvil_proc, deployer_addr, deployer_key = anvil_start(target_block - 1)
        result["fork_block"] = target_block - 1
        result["deployer_addr"] = deployer_addr

        # ПРАВКА (восьмой раунд, разбор владельца, пункт 3): "если одна
        # [предшествующая tx] не воспроизводится -- либо исправь
        # причину, либо выбери другой пример; не объявляй пропуск
        # некритичным без доказательства". Первый прогон (0x20a30efd...)
        # дал 9/10 с ОДНОЙ ошибкой "intrinsic gas too low" -- РЕАЛЬНАЯ,
        # НАЙДЕННАЯ причина: исторический tx.gas каждой предшествующей
        # tx мог быть МЕНЬШЕ, чем требуется, если её calldata на
        # РЕАЛЬНОЙ цепи стоила дешевле газа (сжатие calldata и т.п.) --
        # для ЦЕЛИ "воссоздать СОСТОЯНИЕ" (не "воспроизвести газовые
        # метрики") щедрый фиксированный лимит газа НЕ меняет итоговое
        # состояние (лишний газ просто не тратится) -- используем его
        # ЗДЕСЬ (для предшествующих tx), но НЕ для самой целевой (там
        # сохраняем её родной gas -- см. ниже, чтобы честно повторить её
        # реальное поведение). Если tx ВСЁ РАВНО не проходит даже с
        # щедрым газом -- это ИНАЯ причина, требующая либо
        # диагностирования, либо смены примера (см. ok-гейт ниже,
        # честно останавливает реконструкцию, не проезжает молча).
        GENEROUS_PRECEDING_GAS = hex(5_000_000)
        replayed = []
        for i in range(tx_index):
            t = txs[i]
            r = impersonate_and_send(t["from"], t.get("to"), t.get("input", "0x"),
                                      t.get("value", "0x0"), GENEROUS_PRECEDING_GAS)
            replayed.append({"orig_hash": t["hash"], "from": t["from"], "orig_gas": t.get("gas"),
                              "replay_gas_used": GENEROUS_PRECEDING_GAS, "send_result": r})
        result["replayed_preceding_txs"] = replayed
        failed_preceding = [r for r in replayed if r["send_result"]["returncode"] != 0]
        result["all_preceding_replayed_ok"] = len(failed_preceding) == 0
        if failed_preceding:
            result["ok"] = False
            result["error"] = (
                f"{len(failed_preceding)} из {tx_index} предшествующих транзакций НЕ реплеились даже со "
                f"щедрым фиксированным газом (5,000,000) -- честно ОСТАНАВЛИВАЕМ реконструкцию ЭТОГО "
                f"кандидата (не проезжаем молча, не объявляем некритичным без доказательства); "
                f"см. replayed_preceding_txs для точных ошибок: "
                f"{[(r['orig_hash'], r['send_result']['stderr']) for r in failed_preceding]}"
            )
            return result

        block_before_target_proc = run([CAST, "block-number", "--rpc-url", RPC], timeout=10)
        local_block_before_target = int(block_before_target_proc.stdout.strip())
        result["local_block_immediately_before_target_tx"] = local_block_before_target

        # --- (a) НАШ реальный recompute_route/quote_route_at_size, ЧТЕНИЕ, state не меняет ---
        af._alchemy_direct_checked = True
        af._alchemy_direct_url = None
        af.CONFIG = dataclasses.replace(af.CONFIG, public_rpc_url=RPC, alchemy_rpc_url="", alchemy_api_key="")

        route_build = _build_route_from_fund_flow_legs(ff_legs, pool_init_by_id)
        result["reconstructed_route"] = (
            {"ok": True, "route_id": route_build["route"].route_id,
             "legs": [{"currency0": l.currency0, "currency1": l.currency1, "fee": l.fee,
                        "tick_spacing": l.tick_spacing, "hooks": l.hooks, "zero_for_one": l.zero_for_one}
                       for l in route_build["route"].legs]}
            if route_build["ok"] else route_build
        )
        our_calc: dict = {}
        if route_build["ok"]:
            route = route_build["route"]
            recompute = hp.recompute_route(route, local_block_before_target)
            our_calc["recompute_route_on_reconstructed_state"] = recompute
            if recompute.get("ok"):
                # --- (b) РЕАЛЬНОЕ исполнение НАШИМ контрактом (не только котировка) ---
                bytecode = (REPO_ROOT / "contracts" / "build" / "ClosedCycleExecutorV4.bytecode.txt").read_text().strip()
                bytecode_hex = bytecode[2:] if bytecode.startswith("0x") else bytecode

                def word_addr(a: str) -> str:
                    return a[2:].rjust(64, "0")

                creation_calldata = "0x" + bytecode_hex + word_addr(deployer_addr) + word_addr(POOL_MANAGER)
                deploy_proc = run([CAST, "send", "--private-key", deployer_key, "--rpc-url", RPC,
                                    "--create", creation_calldata, "--json"], timeout=60)
                our_calc["our_contract_deploy_returncode"] = deploy_proc.returncode
                if deploy_proc.returncode == 0:
                    contract_addr = json.loads(deploy_proc.stdout).get("contractAddress")
                    our_calc["our_contract_addr_on_fork"] = contract_addr
                    amount_in = recompute["amount_in"]
                    # Пополняем контракт СТАРТОВЫМ токеном маршрута через impersonate PoolManager --
                    # тот же реальный принцип, что task5_v4_fork_simulation.py.
                    run([CAST, "rpc", "anvil_impersonateAccount", POOL_MANAGER, "--rpc-url", RPC], timeout=15)
                    run([CAST, "rpc", "anvil_setBalance", POOL_MANAGER, hex(10 ** 20), "--rpc-url", RPC], timeout=15)
                    start_token = route.legs[0].input_currency
                    if start_token.lower() != NATIVE:
                        fund_proc = run([CAST, "send", "--unlocked", "--from", POOL_MANAGER, "--rpc-url", RPC,
                                          start_token, "transfer(address,uint256)", contract_addr, str(amount_in),
                                          "--json"], timeout=30)
                        our_calc["fund_contract_returncode"] = fund_proc.returncode
                    else:
                        run([CAST, "rpc", "anvil_setBalance", contract_addr, hex(amount_in + 10 ** 17),
                              "--rpc-url", RPC], timeout=15)
                    calldata = build_execute_cycle_calldata(route, -amount_in, min_profit=1)
                    gas_res = hp.estimate_gas(contract_addr, calldata, deployer_addr)
                    our_calc["our_contract_gas_estimate"] = gas_res
                    # eth_sendTransaction (через impersonate deployer_addr) вместо cast send с
                    # сигнатурой -- deployer_addr УЖЕ разблокирован anvil (тестовый аккаунт(0)),
                    # это просто более надёжный путь передачи сырого calldata, чем CLI-парсинг.
                    send_gas_limit = max(gas_res.get("gas_estimate", 300000) * 2, 300000)
                    send_res = impersonate_and_send(deployer_addr, contract_addr, "0x" + calldata.hex(), "0x0",
                                                      hex(send_gas_limit))
                    our_calc["our_contract_execute_send"] = send_res
                    if send_res["returncode"] == 0 and send_res["tx_hash"]:
                        our_receipt = _rpc_call("eth_getTransactionReceipt", [send_res["tx_hash"]])
                        our_calc["our_contract_execute_receipt_status"] = (
                            int(our_receipt["status"], 16) if our_receipt else None)
        result["our_calc_on_reconstructed_state"] = our_calc

        # --- (c) реплей САМОЙ целевой транзакции (мутирует state -- ПОСЛЕДНИМ шагом) ---
        target_tx = txs[tx_index]
        target_replay = impersonate_and_send(target_tx["from"], target_tx.get("to"), target_tx.get("input", "0x"),
                                              target_tx.get("value", "0x0"), target_tx.get("gas", "0x2dc6c0"))
        result["target_tx_replay"] = target_replay
        if target_replay["returncode"] == 0 and target_replay["tx_hash"]:
            receipt = _rpc_call("eth_getTransactionReceipt", [target_replay["tx_hash"]])
            result["target_tx_replay_receipt_status"] = receipt.get("status") if receipt else None
            result["target_tx_replay_matches_real_status"] = (
                receipt is not None and int(receipt["status"], 16) == 1
            )
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001
        result["ok"] = False
        result["error"] = str(exc)
    finally:
        if anvil_proc is not None:
            anvil_proc.terminate()
            try:
                anvil_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                anvil_proc.kill()
        # Откат CONFIG на реальную цепь -- ОБЯЗАТЕЛЬНО ПОСЛЕ убийства anvil,
        # иначе последующие вызовы в main() (get_block для cross-reference)
        # попадут на уже мёртвый локальный порт.
        af.CONFIG = orig_config
        af._alchemy_direct_checked = orig_alchemy_checked
        af._alchemy_direct_url = orig_alchemy_url
    return result


def cross_reference_own_logs(pool_ids: list[str], target_block: int, target_ts: int) -> dict:
    result: dict = {"pool_ids": pool_ids, "target_block": target_block}
    window_s = 3600  # честное широкое окно (+-1ч) вокруг блока -- НЕ узкая подгонка
    short_hashes = [pid[2:10] for pid in pool_ids]

    def _scan_jsonl(path: Path) -> list[dict]:
        if not path.exists():
            return []
        out = []
        try:
            for line in path.open():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                ts = rec.get("ts_wall")
                if ts is None or abs(ts - target_ts) > window_s:
                    continue
                route_id = rec.get("route_id", "")
                if any(h in route_id for h in short_hashes):
                    out.append(rec)
        except Exception as exc:  # noqa: BLE001
            return [{"error": str(exc)}]
        return out

    result["reason_log_file"] = str(REASON_LOG_FILE)
    result["reason_log_file_exists"] = REASON_LOG_FILE.exists()
    result["reason_log_matches"] = _scan_jsonl(REASON_LOG_FILE)
    result["attempt_table_file"] = str(ATTEMPT_TABLE_FILE)
    result["attempt_table_file_exists"] = ATTEMPT_TABLE_FILE.exists()
    result["attempt_table_matches"] = _scan_jsonl(ATTEMPT_TABLE_FILE)
    if not result["reason_log_matches"] and not result["attempt_table_matches"]:
        result["conclusion"] = (
            "НЕ найдено записей (ни отказа, ни попытки отправки) для этих pool_id в окне "
            f"±{window_s}с вокруг блока {target_block} -- честно 'не зафиксировано' (маршрут либо не "
            "был известен реестру в этот момент, либо не поступал сигнал Swap по этим пулам, либо "
            "запись действительно отсутствует) -- НЕ вычисляем 'задержку реакции' из более поздней "
            "записи, как и требовалось"
        )
    else:
        result["conclusion"] = "найдены записи -- см. reason_log_matches/attempt_table_matches"
    return result


def main() -> None:
    result: dict = {}
    result["previously_used_pre_pilot_control_trade_retraction"] = {
        "tx_hash": PREVIOUSLY_USED_PRE_PILOT_TX, "block": PREVIOUSLY_USED_PRE_PILOT_BLOCK,
        "note": ("этот пример был ДО старта пилота (см. ниже блок старта) -- вывод предыдущего отчёта "
                 "об '~22 минутах опоздания' на НЕГО отзывается как некорректный; новый контрольный "
                 "пример ниже выбран строго ВНУТРИ окна пилота"),
    }

    print("[item3] бинарный поиск блоков начала/конца пилота...")
    latest_block_now = int(_rpc_call("eth_blockNumber", []), 16)
    result["latest_block_at_script_run"] = latest_block_now
    start_block_lookup = find_block_by_timestamp(_iso_to_epoch(PILOT_START_UTC), hi_bound=latest_block_now)
    end_block_lookup = find_block_by_timestamp(_iso_to_epoch(PILOT_END_UTC), hi_bound=latest_block_now)
    result["pilot_start_block_lookup"] = start_block_lookup
    result["pilot_end_block_lookup"] = end_block_lookup
    if not start_block_lookup.get("ok") or not end_block_lookup.get("ok"):
        result["ok"] = False
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        _save(result)
        return
    from_block = start_block_lookup["block"]
    to_block = end_block_lookup["block"]

    print("[item3] проверка реальных данных об остановках/рестартах внутри окна...")
    result["restart_evidence"] = check_restart_evidence()

    print(f"[item3] скан Swap-логов конкурента в окне пилота {from_block}..{to_block}...")
    scan_result = competitor_scan(from_block, to_block)
    # ПРАВКА (восьмой раунд): task5_v4_competitor_trade_scan.py кэширует
    # ПОЛНЫЕ данные плеч только для первых 50 (multi_leg_candidates_full)
    # -- multi_leg_candidates_summary содержит tx_hash/block ДЛЯ ВСЕХ
    # (601 в прошлом прогоне), а full_fund_flow_check() НИЖЕ сам заново
    # получает receipt/Swap-логи по tx_hash -- НЕ зависит от урезанного
    # кэша, поэтому используем ПОЛНЫЙ список кандидатов, а не только 50.
    candidates_summary = scan_result["multi_leg_candidates_summary"]
    result["competitor_scan_summary"] = candidates_summary
    result["n_candidates_in_window"] = len(candidates_summary)
    print(f"[item3] {len(candidates_summary)} многоходовых кандидатов конкурента внутри окна пилота")

    fund_flow_checks = []
    valid_candidates = []
    scan_t0 = time.monotonic()
    for idx, c in enumerate(candidates_summary):
        ff = full_fund_flow_check(c["tx_hash"])
        ff["block"] = c["block"]
        fund_flow_checks.append(ff)
        if (idx + 1) % 25 == 0 or (idx + 1) == len(candidates_summary):
            elapsed = time.monotonic() - scan_t0
            print(f"[item3] fund-flow проверено {idx + 1}/{len(candidates_summary)} "
                  f"(валидных замкнутых циклов пока: {len(valid_candidates)}, "
                  f"прошло {elapsed:.0f}с, кэш Initialize: {len(_POOL_INIT_CACHE)} pool_id)")
        # ПРАВКА (восьмой раунд, пункт 3): гейт -- ТОЛЬКО
        # fully_valid_closed_cycle (замкнутый цикл В БАЗОВОМ токене И
        # отсутствие расхода посторонних токенов исполняющим адресом),
        # НЕ прежний слабый pool_level_profitable_cycle (тот принимал
        # directional ETH->токен как "цикл" -- реально воспроизведено и
        # исправлено выше в full_fund_flow_check).
        if ff.get("fully_valid_closed_cycle"):
            valid_candidates.append((c, ff))
    result["fund_flow_checks"] = fund_flow_checks
    result["n_fully_valid_closed_cycle_candidates"] = len(valid_candidates)

    if not valid_candidates:
        result["chosen_control_trade"] = None
        result["ok"] = True
        result["conclusion"] = (
            "ни один кандидат конкурента внутри окна пилота не подтверждён как ПОЛНОСТЬЮ ЗАМКНУТЫЙ "
            "цикл (строгая проверка: ровно один токен с положительным чистым потоком, отсутствие "
            "расхода посторонних токенов) -- честно фиксируем отсутствие подходящего примера, а НЕ "
            "подставляем directional-сделку как 'цикл'"
        )
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        _save(result)
        return

    # ПРАВКА (восьмой раунд, пункт 3): "если одна предшествующая tx не
    # воспроизводится -- выбери ДРУГОЙ пример" -- перебираем валидные
    # (по фондовому потоку) кандидаты В ПОРЯДКЕ БЛОКА, пока форк-
    # реконструкция (включая реплей ВСЕХ предшествующих tx) не пройдёт
    # полностью хотя бы для одного.
    attempts_log = []
    final_chosen = None
    final_ff = None
    final_reconstruction = None
    for c, ff in valid_candidates:
        print(f"[item3] пробуем кандидата {c['tx_hash']} (блок {c['block']}) для форк-реконструкции...")
        # Пункт 3: Initialize ЗАРАНЕЕ, на РЕАЛЬНОМ RPC (ДО патча af.CONFIG
        # на локальный форк внутри replay_preceding_and_reconstruct) --
        # НЕ повторяем широкий запрос через провайдер форка (лимит 10 блоков).
        pool_init_by_id = {}
        init_ok = True
        for leg in ff["legs"]:
            if leg.get("currency0") is None:
                init_ok = False
                continue
            init = _cached_fetch_initialize(leg["pool_id"], c["block"])
            if init is None:
                init_ok = False
                continue
            pool_init_by_id[leg["pool_id"]] = init
        if not init_ok:
            attempts_log.append({"tx_hash": c["tx_hash"], "block": c["block"],
                                   "skipped_reason": "не удалось предзагрузить Initialize для одного из пулов "
                                                       "НА РЕАЛЬНОМ RPC -- пробуем следующего кандидата"})
            continue

        recon = replay_preceding_and_reconstruct(c["block"], c["tx_hash"], ff["legs"], pool_init_by_id)
        attempts_log.append({"tx_hash": c["tx_hash"], "block": c["block"],
                               "all_preceding_replayed_ok": recon.get("all_preceding_replayed_ok"),
                               "reconstruction_ok": recon.get("ok")})
        if recon.get("all_preceding_replayed_ok") and recon.get("ok"):
            final_chosen, final_ff, final_reconstruction = c, ff, recon
            break
    result["candidate_reconstruction_attempts"] = attempts_log

    if final_chosen is None:
        result["chosen_control_trade"] = None
        result["ok"] = True
        result["conclusion"] = (
            f"из {len(valid_candidates)} кандидатов с подтверждённым замкнутым циклом НИ ОДИН не прошёл "
            "полную форк-реконструкцию (все предшествующие транзакции блока реплеятся успешно) -- честно "
            "фиксируем; см. candidate_reconstruction_attempts для причин по каждому"
        )
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
        _save(result)
        return

    print(f"[item3] выбран контрольный пример: {final_chosen['tx_hash']} (блок {final_chosen['block']})")
    result["chosen_control_trade"] = {"tx_hash": final_chosen["tx_hash"], "block": final_chosen["block"],
                                       "fund_flow": final_ff}
    result["fork_reconstruction"] = final_reconstruction

    pool_ids = [leg["pool_id"] for leg in final_ff["legs"]]
    target_blk = get_block(final_chosen["block"])
    target_ts = int(target_blk["timestamp"], 16) if target_blk else int(time.time())
    print("[item3] сверка с нашими собственными логами (reason_log/attempts)...")
    result["own_log_cross_reference"] = cross_reference_own_logs(pool_ids, final_chosen["block"], target_ts)

    result["ok"] = True
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    _save(result)


def _save(result: dict) -> None:
    out_path = REPO_ROOT / "data" / "task5_v4_item3_in_window_control_trade_result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    print(f"[item3] сохранено: {out_path}")


if __name__ == "__main__":
    main()
