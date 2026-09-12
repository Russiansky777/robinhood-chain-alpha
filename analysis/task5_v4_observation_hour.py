#!/usr/bin/env python3
"""Задача 5, стадия 2, Этап 3: час наблюдения на Ohio БЕЗ отправки
реальных сделок. Владелец, 2026-09-12 (дословные требования):

  - Логировать: состояние/блок/id сообщения фида; локальное время
    получения; старт/конец расчёта; направление/размер; ожидаемый
    выход и прибыль до газа; оценку газа и прибыль после газа;
    время/результат полной симуляции; выжила ли возможность при
    реальной задержке обработки и на следующем доступном состоянии.
  - Монотонные часы для локальных длительностей.
  - НЕ называть разницу между временем блока и локальными часами
    точной задержкой фида без проверки синхронизации/валидности
    таймстемпов.
  - НЕ использовать в момент детекции данные из будущего (пост-хок
    проверка -- отдельно).
  - НЕ считать повторные сигналы одной и той же живущей возможности
    независимыми заработками.
  - Определить, что именно фид отдаёт декодеру и в каком порядке.

ЧЕСТНЫЙ АРХИТЕКТУРНЫЙ ВЫБОР (см. докстринг ниже "почему не декодируем
чужие calldata"): фид используется ТОЛЬКО как источник "новое
состояние/блок объявлено" (sequenceNumber == номер L2-блока, реальный
факт этого проекта) -- каждый новый sequenceNumber триггерит независимую
проверку РЕАЛЬНОГО состояния пулов на этом блоке через V4Quoter, а не
попытку декодировать намерение чужой транзакции. Это НЕ то же самое,
что "фид даёт нам заранее знать, что произойдёт" -- наоборот, мы
реагируем на уже объявленный блок и спрашиваем реальное состояние ПОСЛЕ
него, так же, как это делает eth_call с любым другим blockTag.

ПОЧЕМУ НЕ ДЕКОДИРУЕМ ЧУЖИЕ CALLDATA ДЛЯ V4 (реальное архитектурное
отличие от V3-версии бота, task5_bot_run.py): в V3 своп -- прямой вызов
`pool.swap(...)` с фиксированной, декодируемой сигнатурой. В V4 ВСЕ
свопы идут через ОДИН синглтон-PoolManager внутри `unlock(bytes data)`,
где `data` -- это ПРОИЗВОЛЬНЫЙ формат, который выбирает КАЖДЫЙ вызывающий
контракт сам (наш ClosedCycleExecutorV4 кодирует CycleParams, чужой
контракт -- что угодно своё). Обобщённо декодировать намерение свопа из
calldata пресвопа для V4 НЕВОЗМОЖНО без знания ABI конкретного
вызывающего контракта -- отсюда честный выбор: смотреть на РЕАЛЬНОЕ
состояние пула после каждого блока, а не пытаться предсказать его из
недекодируемой calldata."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("ALCHEMY_ROBINHOOD_RPC_URL", os.environ.get("RPC_URL_PROVIDER", ""))

from alchemy_fallback import _rpc_call  # noqa: E402
from task5_bot_config import SEQUENCER_FEED_URL_MAINNET  # noqa: E402
from task5_bot_feed_client import FeedMessage, SequencerFeedClient  # noqa: E402
from task5_v4_pool_math import PoolKey  # noqa: E402
from task5_v4_quote_replay import quote_exact_input_single  # noqa: E402 -- реюз RPC+Alchemy-фолбэк логики

REPO_ROOT = Path(__file__).parent.parent
FOUNDRY_BIN = Path.home() / ".foundry" / "bin"
ANVIL = str(FOUNDRY_BIN / "anvil")
CAST = str(FOUNDRY_BIN / "cast")

NATIVE = "0x0000000000000000000000000000000000000000"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
MOSIAI = "0xfb6d1a1860277c1399b3141f8b12a1b77257e57a"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
HOOK_ETH_MOSIAI = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"

POOL_A_USDG_MOSIAI = PoolKey(USDG, MOSIAI, 70000, 4)
POOL_B_USDG_MOSIAI = PoolKey(USDG, MOSIAI, 70000, 3)
POOL_ETH_MOSIAI = PoolKey(NATIVE, MOSIAI, 0, 200, HOOK_ETH_MOSIAI)
POOL_USDG_MOSIAI_75000 = PoolKey(USDG, MOSIAI, 75000, 2)
POOL_ETH_USDG = PoolKey(NATIVE, USDG, 100, 1)

SIZE_GRID_USDG_RAW = [1_000_000, 3_000_000, 5_000_000, 7_000_000, 10_000_000, 15_000_000, 20_000_000, 30_000_000]
SIZE_GRID_ETH_WEI = [int(x * 1e18) for x in (0.02, 0.05, 0.08, 0.1, 0.15, 0.2, 0.3)]

# Реально измеренный газ этой сессии (fork-симуляция, не оценка):
# data/task5_v4_fork_simulation_result.json (146655) и
# data/task5_v4_fork_simulation_hook_route_result.json (901350, 348566
# -- разные размеры/пути тиков, честно берём среднее как приблизительный
# ориентир, не как точный прогноз для КАЖДОГО размера).
GAS_USED_USDG_ROUTE = 146_655
GAS_USED_ETH_HOOK_ROUTE = round((901_350 + 348_566) / 2)

CHECK_INTERVAL_S = 3.0  # throttle -- не на КАЖДЫЙ блок (~100мс блоктайм), реальный компромисс
FULLSIM_INTERVAL_S = 600.0  # реальная fork-симуляция раз в ~10 минут -- дорогая (anvil), не на каждый тик


def usdg_route_profit(amount_in: int, block_number: int) -> tuple[int, int]:
    mosiai_out = quote_exact_input_single(POOL_A_USDG_MOSIAI, True, amount_in, block_number)
    usdg_out = quote_exact_input_single(POOL_B_USDG_MOSIAI, False, mosiai_out, block_number)
    return usdg_out - amount_in, usdg_out


def eth_hook_route_profit(amount_in_wei: int, block_number: int) -> tuple[int, int]:
    mosiai_out = quote_exact_input_single(POOL_ETH_MOSIAI, True, amount_in_wei, block_number)
    usdg_out = quote_exact_input_single(POOL_USDG_MOSIAI_75000, False, mosiai_out, block_number)
    eth_out = quote_exact_input_single(POOL_ETH_USDG, False, usdg_out, block_number)
    return eth_out - amount_in_wei, eth_out


def best_over_grid(profit_fn, grid: list[int], block_number: int) -> dict:
    """Возвращает лучший (по прибыли) размер из сетки. Если КАЖДЫЙ
    размер сетки упал -- честно возвращает причину последней ошибки
    (`all_failed_last_error`), а не молча пустой словарь: реальный
    случай этой сессии -- на текущем состоянии сети конкретный пул
    может быть полностью лишён ликвидности (NotEnoughLiquidity(poolId),
    см. data/task5_v4_diag_current_liquidity_result.json), и это
    само по себе значимый результат наблюдения, не сбой скрипта."""
    best = None
    last_error = None
    for amount in grid:
        try:
            profit, out = profit_fn(amount, block_number)
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)[:300]
            continue
        if best is None or profit > best["profit_raw"]:
            best = {"amount_in_raw": amount, "profit_raw": profit, "amount_out_raw": out}
    if best is None:
        return {"all_failed": True, "all_failed_last_error": last_error} if last_error else {}
    return best


def current_gas_price_wei() -> int | None:
    try:
        return int(_rpc_call("eth_gasPrice", []), 16)
    except Exception:  # noqa: BLE001
        return None


def run_fullsim_usdg_route(fork_block: int, amount_in_raw: int, port: int) -> dict:
    """Полная fork-симуляция (не котировка) 2-пуловского маршрута на
    ТЕКУЩЕМ блоке -- периодическая перекрёстная проверка "котировка vs
    реальное успешное исполнение", см. Этап 2 этой же сессии
    (task5_v4_fork_simulation.py) -- та же логика, параметризована по
    текущему блоку/размеру вместо фиксированной контрольной tx."""
    rpc = f"http://127.0.0.1:{port}"
    result: dict = {"fork_block": fork_block, "amount_in_raw": amount_in_raw}
    anvil_proc = None
    t0 = time.monotonic()
    try:
        from alchemy_fallback import _alchemy_direct_endpoint
        fork_url = _alchemy_direct_endpoint()
        if not fork_url:
            raise RuntimeError("нет Alchemy-эндпоинта для форка")
        anvil_proc = subprocess.Popen(
            [ANVIL, "--fork-url", fork_url, "--fork-block-number", str(fork_block), "--port", str(port)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        banner = []
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            line = anvil_proc.stdout.readline()
            if not line:
                if anvil_proc.poll() is not None:
                    break
                continue
            banner.append(line.rstrip("\n"))
            if "Listening on" in line:
                break
        import re
        text = "\n".join(banner)
        addr_m = re.search(r"\(0\)\s+(0x[0-9a-fA-F]{40})", text)
        key_m = re.search(r"\(0\)\s+(0x[0-9a-fA-F]{64})", text)
        if not addr_m or not key_m:
            raise RuntimeError("не распарсил тестовый аккаунт anvil")
        deployer_addr, deployer_key = addr_m.group(1), key_m.group(1)

        for _ in range(30):
            proc = subprocess.run([CAST, "block-number", "--rpc-url", rpc], capture_output=True, text=True, timeout=10)
            if proc.returncode == 0:
                break
            time.sleep(1)

        bytecode = (REPO_ROOT / "contracts" / "build" / "ClosedCycleExecutorV4.bytecode.txt").read_text().strip()
        bytecode_hex = bytecode[2:] if bytecode.startswith("0x") else bytecode

        def word_addr(a: str) -> str:
            return a[2:].rjust(64, "0")

        creation_calldata = "0x" + bytecode_hex + word_addr(deployer_addr) + word_addr(POOL_MANAGER)
        deploy_proc = subprocess.run([CAST, "send", "--private-key", deployer_key, "--rpc-url", rpc,
                                       "--create", creation_calldata, "--json"], capture_output=True, text=True, timeout=60)
        if deploy_proc.returncode != 0:
            raise RuntimeError(f"деплой упал: {deploy_proc.stderr}")
        contract_addr = json.loads(deploy_proc.stdout).get("contractAddress")

        subprocess.run([CAST, "rpc", "anvil_impersonateAccount", POOL_MANAGER, "--rpc-url", rpc],
                        capture_output=True, text=True, timeout=15)
        subprocess.run([CAST, "rpc", "anvil_setBalance", POOL_MANAGER, hex(10**19), "--rpc-url", rpc],
                        capture_output=True, text=True, timeout=15)
        fund_proc = subprocess.run([CAST, "send", "--unlocked", "--from", POOL_MANAGER, "--rpc-url", rpc,
                                     USDG, "transfer(address,uint256)", contract_addr, str(amount_in_raw), "--json"],
                                    capture_output=True, text=True, timeout=30)
        if fund_proc.returncode != 0:
            raise RuntimeError(f"пополнение упало: {fund_proc.stderr}")

        MIN_SQRT_PRICE, MAX_SQRT_PRICE = 4295128739, 1461446703485210103287273052203988822378723970342
        leg_a = f"({USDG},{MOSIAI},70000,4,{NATIVE},true,{MIN_SQRT_PRICE + 1},0x)"
        leg_b = f"({USDG},{MOSIAI},70000,3,{NATIVE},false,{MAX_SQRT_PRICE - 1},0x)"
        cycle_params = f"([{leg_a},{leg_b}],-{amount_in_raw},{USDG},0)"
        sig = "executeCycle(((address,address,uint24,int24,address,bool,uint160,bytes)[],int256,address,uint256))"
        exec_proc = subprocess.run([CAST, "send", "--private-key", deployer_key, "--rpc-url", rpc,
                                     contract_addr, sig, cycle_params, "--json"], capture_output=True, text=True, timeout=60)
        result["execute_cycle_returncode"] = exec_proc.returncode
        result["execute_cycle_stderr"] = exec_proc.stderr.strip()[-1000:]
        result["succeeded"] = exec_proc.returncode == 0
        if exec_proc.returncode == 0:
            receipt = json.loads(exec_proc.stdout)
            result["tx_hash"] = receipt.get("transactionHash")
            result["gas_used"] = int(receipt.get("gasUsed", "0x0"), 16)
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
    result["fullsim_wall_duration_s"] = time.monotonic() - t0
    return result


async def main_async(duration_s: float, feed_url: str, out_path: Path) -> None:
    latest = {"sequence_number": None, "sequencer_timestamp": None, "t_wall": None}
    n_feed_messages = [0]
    observations: list[dict] = []
    open_signals: dict[str, dict] = {}
    closed_signals: list[dict] = []
    fullsim_log: list[dict] = []
    last_fullsim_wall = [0.0]
    stop_event = asyncio.Event()

    def on_feed_message(msg: FeedMessage) -> None:
        n_feed_messages[0] += 1
        latest["sequence_number"] = msg.sequence_number
        latest["sequencer_timestamp"] = msg.sequencer_timestamp
        latest["t_wall"] = msg.t_wall

    async def periodic_checker() -> None:
        loop = asyncio.get_event_loop()
        last_checked_block = None
        start_wall = time.time()
        while not stop_event.is_set():
            await asyncio.sleep(CHECK_INTERVAL_S)
            block_number = latest["sequence_number"]
            if block_number is None or block_number == last_checked_block:
                continue
            last_checked_block = block_number
            recv_t_wall = latest["t_wall"]
            seq_ts = latest["sequencer_timestamp"]

            calc_start = time.monotonic()
            try:
                best_usdg, best_eth = await asyncio.gather(
                    loop.run_in_executor(None, best_over_grid, usdg_route_profit, SIZE_GRID_USDG_RAW, block_number),
                    loop.run_in_executor(None, best_over_grid, eth_hook_route_profit, SIZE_GRID_ETH_WEI, block_number),
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[obs_hour] ошибка котировки на блоке {block_number}: {exc}", file=sys.stderr)
                continue
            calc_end = time.monotonic()
            gas_price = current_gas_price_wei()

            obs = {
                "block_number": block_number,
                "feed_sequencer_timestamp": seq_ts,
                "local_recv_t_wall": recv_t_wall,
                "calc_start_monotonic": calc_start,
                "calc_end_monotonic": calc_end,
                "calc_duration_s": calc_end - calc_start,
                "gas_price_wei": gas_price,
                "routes": {},
            }
            for label, best, gas_used_estimate, decimals in (
                ("usdg_mosiai_2pool", best_usdg, GAS_USED_USDG_ROUTE, 6),
                ("eth_hook_3pool", best_eth, GAS_USED_ETH_HOOK_ROUTE, 18),
            ):
                if not best:
                    continue
                if best.get("all_failed"):
                    # Честно фиксируем ПОЧЕМУ маршрут недоступен на этом
                    # блоке (напр. NotEnoughLiquidity у конкретного пула) --
                    # это реальный результат наблюдения, не сбой скрипта.
                    # Если сигнал был открыт -- закрываем его (маршрут
                    # больше не исполним, живучесть кончилась), не
                    # оставляем висеть "открытым" вечно.
                    obs["routes"][label] = {"all_failed": True, "last_error": best.get("all_failed_last_error")}
                    if label in open_signals:
                        sig = open_signals.pop(label)
                        sig["closed_at_block"] = block_number
                        sig["closed_at_wall"] = time.time()
                        sig["closed_reason"] = "all_failed"
                        sig["duration_blocks"] = block_number - sig["opened_at_block"]
                        sig["duration_wall_s"] = sig["closed_at_wall"] - sig["opened_at_wall"]
                        closed_signals.append(sig)
                        print(f"[obs_hour][{label}] сигнал ЗАКРЫТ (маршрут перестал исполняться) на блоке {block_number}")
                    continue
                profit_raw = best["profit_raw"]
                profit_human = profit_raw / 10**decimals
                gas_cost_native = (gas_price * gas_used_estimate / 1e18) if gas_price else None
                row = {
                    "best_amount_in_raw": best["amount_in_raw"], "profit_raw": profit_raw,
                    "profit_human": profit_human, "gas_used_estimate": gas_used_estimate,
                    "gas_cost_native_estimate": gas_cost_native,
                    "profitable_pre_gas": profit_raw > 0,
                }
                obs["routes"][label] = row

                is_open = label in open_signals
                if row["profitable_pre_gas"]:
                    if not is_open:
                        open_signals[label] = {
                            "route": label, "opened_at_block": block_number, "opened_at_wall": time.time(),
                            "opened_at_monotonic": calc_start, "max_profit_human": profit_human,
                            "n_blocks_observed": 1,
                        }
                        print(f"[obs_hour][{label}] НОВЫЙ сигнал, блок {block_number}, "
                              f"размер_raw={best['amount_in_raw']}, прибыль={profit_human:.8f}")
                    else:
                        open_signals[label]["max_profit_human"] = max(open_signals[label]["max_profit_human"], profit_human)
                        open_signals[label]["n_blocks_observed"] += 1
                        open_signals[label]["last_seen_block"] = block_number
                else:
                    if is_open:
                        sig = open_signals.pop(label)
                        sig["closed_at_block"] = block_number
                        sig["closed_at_wall"] = time.time()
                        sig["duration_blocks"] = block_number - sig["opened_at_block"]
                        sig["duration_wall_s"] = sig["closed_at_wall"] - sig["opened_at_wall"]
                        closed_signals.append(sig)
                        print(f"[obs_hour][{label}] сигнал ЗАКРЫТ на блоке {block_number}: "
                              f"жил {sig['duration_blocks']} блоков / {sig['duration_wall_s']:.1f}с, "
                              f"макс. прибыль={sig['max_profit_human']:.8f}")

            observations.append(obs)
            print(f"[obs_hour] блок={block_number} calc={obs['calc_duration_s']*1000:.1f}мс "
                  f"usdg_profit={obs['routes'].get('usdg_mosiai_2pool', {}).get('profit_human')} "
                  f"eth_profit={obs['routes'].get('eth_hook_3pool', {}).get('profit_human')}")

            # Периодическая полная fork-симуляция -- перекрёстная проверка
            # "котировка vs реальное исполнение" (владелец: "время/результат
            # полной симуляции"), не на каждый тик -- дорого (anvil).
            if time.monotonic() - last_fullsim_wall[0] >= FULLSIM_INTERVAL_S and best_usdg:
                last_fullsim_wall[0] = time.monotonic()
                fs_t0 = time.monotonic()
                # fork_block = block_number (не -1): котировка выше была
                # взята с blockTag=block_number, что означает "состояние на
                # конец блока block_number" -- ровно то, что anvil
                # --fork-block-number block_number даёт нашей ПЕРВОЙ новой
                # tx (она становится частью block_number+1). Совпадение
                # точки отсчёта -- иначе сравнение "котировка vs факт"
                # было бы нечестным (разные состояния).
                fs_result = await loop.run_in_executor(
                    None, run_fullsim_usdg_route, block_number, best_usdg["amount_in_raw"], 8560,
                )
                fs_result["triggered_at_block"] = block_number
                fs_result["quote_profit_human"] = best_usdg["profit_raw"] / 1e6
                fullsim_log.append(fs_result)
                print(f"[obs_hour][fullsim] блок={block_number} succeeded={fs_result.get('succeeded')} "
                      f"wall={time.monotonic()-fs_t0:.1f}с")

            if time.time() - start_wall >= duration_s:
                stop_event.set()

    client = SequencerFeedClient(feed_url)
    checker_task = asyncio.create_task(periodic_checker())
    try:
        await asyncio.wait_for(client.listen(on_feed_message), timeout=duration_s)
    except asyncio.TimeoutError:
        pass
    finally:
        stop_event.set()
        checker_task.cancel()
        try:
            await checker_task
        except asyncio.CancelledError:
            pass

    result = {
        "duration_s_requested": duration_s,
        "n_feed_messages_total": n_feed_messages[0],
        "feed_diag": client.diag,
        "n_observations": len(observations),
        "observations": observations,
        "closed_signals": closed_signals,
        "still_open_signals_at_end": list(open_signals.values()),
        "fullsim_log": fullsim_log,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str))
    print(f"[obs_hour] сохранено: {out_path}")
    print(f"[obs_hour] наблюдений={len(observations)}, закрытых сигналов={len(closed_signals)}, "
          f"ещё открытых={len(open_signals)}, полных симуляций={len(fullsim_log)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration-seconds", type=float, default=3600.0)
    ap.add_argument("--feed-url", type=str, default=SEQUENCER_FEED_URL_MAINNET)
    ap.add_argument("--out", type=str, default="data/task5_v4_observation_hour_result.json")
    args = ap.parse_args()
    asyncio.run(main_async(args.duration_seconds, args.feed_url, REPO_ROOT / args.out))


if __name__ == "__main__":
    main()
