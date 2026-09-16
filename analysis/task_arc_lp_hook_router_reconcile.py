#!/usr/bin/env python3
"""Владелец снял непроверенное допущение "хук забирает у LP" и попросил
установить механизм на 5 КОНКРЕТНЫХ tx (не агрегатом): куда именно идут
деньги хука 0x47e79...20cc -- сверх цены за счёт трейдера, из комиссии
пула за счёт LP, или иначе.

Метод: для каждой tx сравниваем ДВА независимых источника истины --
(1) что реально ФИЗИЧЕСКИ перевелось (Transfer-логи, PoolManager/хук/
роутер/трейдер) и (2) что говорит СОБСТВЕННАЯ бухгалтерия AMM (событие
Swap: amount0/amount1 -- чистое изменение резервов пула, знак = кто кому
должен, ДО любых доп. действий хука после свопа; fee -- реальный fee_pips,
применённый к этому конкретному свопу, может отличаться от статичного
fee из Initialize при dynamic-fee хуке).

LP fee считается СТАНДАРТНО (Uniswap v3/v4 конвенция, не наше
предположение): fee_pips/1e6 * abs(amount на ВХОДНОЙ стороне свопа) --
эта комиссия остаётся в резервах пула как часть входящего amount,
отдельного Transfer на неё нет по построению. Если входная сторона -- НЕ
USDC, конвертируем в USD-эквивалент по цене ЭТОЙ ЖЕ tx (usdc_leg/other_leg).

Расхождение между (1) и (2) на USDC-стороне -- это тест: если реально
вышло/вошло РОВНО столько, сколько говорит Swap-событие (хук берёт долю
уже ВНУТРИ этого потока, т.е. за счёт того, что иначе получил бы
трейдер/роутер) -- значит хук режет чужую долю, не добавляет новую. Если
реально вышло/вошло БОЛЬШЕ/МЕНЬШЕ, чем Swap-событие -- значит есть
дополнительный расчёт (взято сверх цены или добавлено трейдером
отдельно), и это должно проявиться как несходящийся баланс где-то ещё."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests
from Crypto.Hash import keccak

from arc_units import USDC_ERC20, USDC_NATIVE_WRAP, to_human, merge_mirrored_transfers

RPC = "https://rpc.mainnet.arc.io"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
PROFIT_RECEIVER = "0x47e7936ae9891e61c5123db720593c05de7120cc"
DATA_DIR = Path(__file__).parent.parent.joinpath("data")

TARGET_TXS = [
    "0xc06934c034d507921ba9fe5b59d378047ab0ddb811c1a75ff200ca018b5ed6c1",  # sell, 2.5% (референс владельца)
    "0xdbcf64abc7c9df19dd52de490df60eac020dff3a2472eb99c5069fc31f57bf43",  # sell, 2.5%
    "0x5b5c2a4c17fb00c93e36d08b7e139c46fe15b9da208b52b393bc912e57b0016b",  # sell, 1%
    "0xfb7298fd560aa283aa42a353ec80a720e3c0b3a45738ad407421abcc5dd8190c",  # buy, 2.5%
    "0xe19db714cd8f209c2666d97b79964e7682a07bd1753b5f9e484307151dcf71a2",  # buy, 1%
]


def keccak_topic0(sig: str) -> str:
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return "0x" + h.hexdigest()


INIT_TOPIC0 = keccak_topic0("Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)")
SWAP_TOPIC0 = keccak_topic0("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
TRANSFER_TOPIC0 = keccak_topic0("Transfer(address,address,uint256)")

_rpc_calls = [0]


def rpc(method: str, params: list, timeout: int = 25) -> dict:
    _rpc_calls[0] += 1
    for attempt in range(5):
        try:
            resp = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                  headers={"Content-Type": "application/json"}, timeout=timeout)
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            if attempt == 4:
                return {"error": {"message": f"{type(exc).__name__}: {exc}"}}
            time.sleep(2 * (attempt + 1))
            continue
        err = body.get("error")
        if err and "rate limit" in str(err.get("message", "")).lower():
            if attempt == 4:
                return body
            time.sleep(2 * (attempt + 1))
            continue
        return body
    return {"error": {"message": "unreachable"}}


def topic_to_addr(t: str) -> str:
    return "0x" + t[-40:]


def addr_topic(addr: str) -> str:
    return "0x" + addr.lower().replace("0x", "").rjust(64, "0")


def word(data_bytes: bytes, i: int) -> bytes:
    return data_bytes[i * 32:(i + 1) * 32]


def to_int_signed(w: bytes) -> int:
    v = int.from_bytes(w, "big")
    if v >= 2 ** 255:
        v -= 2 ** 256
    return v


def decode_transfer(log: dict) -> dict | None:
    if len(log.get("topics", [])) < 3:
        return None
    return {"token": log["address"].lower(), "from": topic_to_addr(log["topics"][1]).lower(),
            "to": topic_to_addr(log["topics"][2]).lower(),
            "value": int(log["data"], 16) if log.get("data") and log["data"] != "0x" else 0,
            "log_index": int(log["logIndex"], 16)}


def decode_swap(log: dict) -> dict:
    data = bytes.fromhex(log["data"][2:])
    return {
        "pool_id": log["topics"][1], "sender": topic_to_addr(log["topics"][2]),
        "amount0": to_int_signed(word(data, 0)), "amount1": to_int_signed(word(data, 1)),
        "sqrt_price_x96": int.from_bytes(word(data, 2), "big"),
        "liquidity": int.from_bytes(word(data, 3), "big"),
        "fee_pips": int.from_bytes(word(data, 5)[-3:], "big"),
        "log_index": int(log["logIndex"], 16),
    }


def decode_initialize(log: dict) -> dict:
    data = bytes.fromhex(log["data"][2:])
    return {"pool_id": log["topics"][1], "currency0": topic_to_addr(log["topics"][2]),
            "currency1": topic_to_addr(log["topics"][3]),
            "fee_static": int.from_bytes(word(data, 0)[-3:], "big"),
            "hooks": "0x" + word(data, 2)[-20:].hex(),
            "block_number": int(log["blockNumber"], 16)}


def find_initialize_for_pool(pool_id: str, before_block: int) -> dict | None:
    """Точечный обратный поиск Initialize для КОНКРЕТНОГО pool_id (индексированный
    topic -- ищем не по плотности, а по точному совпадению), адаптивный чанк
    от жёсткого лимита диапазона (см. task_arc_rpc_limits_probe_result.json)."""
    to_block = before_block
    chunk = 5000
    calls = 0
    while to_block > 0 and calls < 30:
        from_block = max(0, to_block - chunk)
        body = rpc("eth_getLogs", [{"fromBlock": hex(from_block), "toBlock": hex(to_block),
                                     "address": POOL_MANAGER, "topics": [INIT_TOPIC0, pool_id]}])
        calls += 1
        if "error" in body:
            err = body["error"]
            msg = str(err.get("message", "")).lower()
            if err.get("code") in (-32012, -32602) or "too large" in msg or "max results" in msg:
                chunk = max(500, chunk // 2)
                continue
            return None
        logs = body.get("result", [])
        if logs:
            return decode_initialize(logs[0])
        to_block = from_block - 1
    return None


def get_token_decimals(token: str, cache: dict) -> int | None:
    token = token.lower()
    if token in cache:
        return cache[token]
    if token in (USDC_ERC20.lower(), USDC_NATIVE_WRAP.lower()):
        cache[token] = {USDC_ERC20.lower(): 6, USDC_NATIVE_WRAP.lower(): 18}[token]
        return cache[token]
    body = rpc("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])  # decimals()
    res = body.get("result")
    dec = int(res, 16) if res and res != "0x" else None
    cache[token] = dec
    return dec


def get_code_is_contract(addr: str) -> bool:
    body = rpc("eth_getCode", [addr, "latest"])
    code = body.get("result", "0x")
    return bool(code and code != "0x")


def to_human_generic(token: str, raw: int, dec_cache: dict) -> float | None:
    t = token.lower()
    if t in (USDC_ERC20.lower(), USDC_NATIVE_WRAP.lower()):
        return to_human(t, raw)
    dec = get_token_decimals(t, dec_cache)
    return (raw / (10 ** dec)) if dec is not None else None


def analyze_tx(tx_hash: str, dec_cache: dict, init_cache: dict) -> dict:
    receipt = rpc("eth_getTransactionReceipt", [tx_hash]).get("result")
    if not receipt:
        return {"tx_hash": tx_hash, "error": "receipt не получен"}
    tx = rpc("eth_getTransactionByHash", [tx_hash]).get("result") or {}
    block_number = int(receipt["blockNumber"], 16)

    swap_logs = [decode_swap(l) | {"pool_id_raw": l["topics"][1]} for l in receipt.get("logs", [])
                 if l.get("topics", [None])[0] == SWAP_TOPIC0]
    transfers_raw = [t for t in (decode_transfer(l) for l in receipt.get("logs", [])
                                  if l.get("topics", [None])[0] == TRANSFER_TOPIC0) if t]
    transfers = merge_mirrored_transfers(transfers_raw)

    usdc_tokens = {USDC_ERC20.lower(), USDC_NATIVE_WRAP.lower()}
    # V4 использует address(0) как sentinel "нативной" валюты пула в
    # Initialize/Swap-событиях (currency0/1, amount0/1) -- на Arc нативная
    # валюта = USDC (газ-токен), это НЕ то же самое поле, что
    # 0xfff...ffe (тот адрес -- синтетический Transfer-лог нативных
    # переводов, для ERC20-совместимых инструментов). Оба относятся к
    # ОДНОМУ и тому же активу с одинаковым масштабом (18 знаков) --
    # подтверждено ранее (task_arc_usdc_addressing_check_result.json:
    # агрегатный нативный баланс PoolManager численно равен ERC20
    # балансу/1e6). Первый прогон этого скрипта нашёл currency0=address(0)
    # на всех 5 tx и НЕ распознал его как USDC -- реальный баг, здесь исправлен.
    NATIVE_SENTINEL = "0x0000000000000000000000000000000000000000"
    usdc_identity_tokens = usdc_tokens | {NATIVE_SENTINEL}

    # --- resolve currency0/1 for every pool touched (needed to know which
    # side of amount0/amount1 is USDC) ---
    swap_details = []
    for s in swap_logs:
        pid = s["pool_id_raw"]
        if pid not in init_cache:
            init_cache[pid] = find_initialize_for_pool(pid, block_number)
        meta = init_cache[pid]
        c0 = meta["currency0"].lower() if meta else None
        c1 = meta["currency1"].lower() if meta else None
        usdc_side = "currency0" if c0 in usdc_identity_tokens else ("currency1" if c1 in usdc_identity_tokens else None)
        other_token = (c1 if usdc_side == "currency0" else c0) if usdc_side else None
        if usdc_side == "currency0":
            amount_usdc_signed_raw = s["amount0"]
            amount_other_signed_raw = s["amount1"]
        elif usdc_side == "currency1":
            amount_usdc_signed_raw = s["amount1"]
            amount_other_signed_raw = s["amount0"]
        else:
            amount_usdc_signed_raw = amount_other_signed_raw = None

        usdc_token_addr_raw = (c0 if usdc_side == "currency0" else c1) if usdc_side else None
        # address(0) конвертируем ПО ШКАЛЕ native-обёртки (18 знаков) -- сам
        # to_human() адрес address(0) не узнает, поэтому подставляем
        # USDC_NATIVE_WRAP как эквивалент по масштабу, не как реальный адрес.
        usdc_token_addr = (USDC_NATIVE_WRAP if usdc_token_addr_raw == NATIVE_SENTINEL else usdc_token_addr_raw)
        amount_usdc_human = (to_human(usdc_token_addr, abs(amount_usdc_signed_raw))
                              if amount_usdc_signed_raw is not None else None)
        amount_other_human = (to_human_generic(other_token, abs(amount_other_signed_raw), dec_cache)
                               if amount_other_signed_raw is not None and other_token else None)

        # знак: amount>0 = токен ПРИШЁЛ В ПУЛ (вход трейдера), amount<0 = вышел (выход трейдеру)
        usdc_is_input = (amount_usdc_signed_raw is not None and amount_usdc_signed_raw > 0)

        if usdc_is_input and amount_usdc_human is not None:
            lp_fee_usd_equiv = s["fee_pips"] / 1_000_000 * amount_usdc_human
            lp_fee_native_amount, lp_fee_native_token = lp_fee_usd_equiv, "USDC (входная сторона)"
        elif amount_other_human is not None:
            lp_fee_native_amount = s["fee_pips"] / 1_000_000 * amount_other_human
            lp_fee_native_token = f"{other_token} (входная сторона, НЕ USDC)"
            price_this_swap = (amount_usdc_human / amount_other_human) if amount_other_human else None
            lp_fee_usd_equiv = (lp_fee_native_amount * price_this_swap) if price_this_swap is not None else None
        else:
            lp_fee_native_amount = lp_fee_native_token = lp_fee_usd_equiv = None

        swap_details.append({
            "pool_id": pid, "currency0": c0, "currency1": c1, "usdc_side": usdc_side,
            "other_token": other_token, "fee_pips": s["fee_pips"],
            "amm_amount_usdc_signed_human": (amount_usdc_human if amount_usdc_signed_raw is None or amount_usdc_signed_raw >= 0
                                              else -amount_usdc_human) if amount_usdc_human is not None else None,
            "amm_amount_other_signed_human": (amount_other_human if amount_other_signed_raw is None or amount_other_signed_raw >= 0
                                               else -amount_other_human) if amount_other_human is not None else None,
            "usdc_is_input_to_pool": usdc_is_input,
            "lp_fee_native_amount": lp_fee_native_amount, "lp_fee_native_token": lp_fee_native_token,
            "lp_fee_usd_equiv": lp_fee_usd_equiv,
            "hooks": meta["hooks"] if meta else None,
        })

    # --- реальные (settled) USDC-переводы, участвующие в PoolManager ---
    usdc_transfers = [t for t in transfers if t["token"] in usdc_tokens]
    pm = POOL_MANAGER.lower()
    real_usdc_into_pm = sum(to_human(t["token"], t["value"]) or 0 for t in usdc_transfers if t["to"] == pm)
    real_usdc_out_of_pm = sum(to_human(t["token"], t["value"]) or 0 for t in usdc_transfers if t["from"] == pm)
    to_receiver = sum(to_human(t["token"], t["value"]) or 0 for t in usdc_transfers if t["to"] == PROFIT_RECEIVER.lower())

    trader_eoa = (tx.get("from") or "").lower()
    known = {pm, PROFIT_RECEIVER.lower()}
    router_candidates = {}
    for t in usdc_transfers:
        for side in ("from", "to"):
            a = t[side]
            if a not in known and a != trader_eoa:
                router_candidates.setdefault(a, {"in": 0.0, "out": 0.0})
    for t in usdc_transfers:
        v = to_human(t["token"], t["value"]) or 0
        if t["from"] in router_candidates:
            router_candidates[t["from"]]["out"] += v
        if t["to"] in router_candidates:
            router_candidates[t["to"]]["in"] += v

    router_info = []
    for addr, flows in router_candidates.items():
        is_contract = get_code_is_contract(addr)
        router_info.append({"address": addr, "is_contract": is_contract,
                             "usdc_in": flows["in"], "usdc_out": flows["out"],
                             "net_kept_usdc": flows["in"] - flows["out"]})

    amm_usdc_leg_total = sum(abs(sd["amm_amount_usdc_signed_human"]) for sd in swap_details
                              if sd["amm_amount_usdc_signed_human"] is not None)
    real_usdc_leg_total = max(real_usdc_into_pm, real_usdc_out_of_pm)
    amm_vs_real_diff = (real_usdc_leg_total - amm_usdc_leg_total) if amm_usdc_leg_total else None

    total_lp_fee_usd = sum(sd["lp_fee_usd_equiv"] for sd in swap_details if sd["lp_fee_usd_equiv"] is not None)
    volume_usdc = real_usdc_leg_total

    # трейдер: что реально внёс / получил на руки (не-USDC нога отдельно)
    trader_usdc_transfers = [t for t in usdc_transfers if t["from"] == trader_eoa or t["to"] == trader_eoa]
    trader_non_usdc_transfers = [t for t in transfers if t["token"] not in usdc_tokens
                                  and (t["from"] == trader_eoa or t["to"] == trader_eoa)]

    result = {
        "tx_hash": tx_hash, "block_number": block_number, "trader_eoa": trader_eoa,
        "swap_details": swap_details,
        "amm_usdc_leg_total_pure_swap_math": amm_usdc_leg_total,
        "real_usdc_leg_total_settled_transfers": real_usdc_leg_total,
        "real_usdc_into_pool_manager": real_usdc_into_pm,
        "real_usdc_out_of_pool_manager": real_usdc_out_of_pm,
        "amm_vs_real_diff_usdc": amm_vs_real_diff,
        "amm_vs_real_interpretation": (
            "СХОДИТСЯ (|diff|<0.01% объёма) -- хук берёт долю ИЗНУТРИ потока, который иначе достался бы трейдеру/роутеру, а не добавляет деньги сверх AMM-математики"
            if amm_vs_real_diff is not None and amm_usdc_leg_total and abs(amm_vs_real_diff) < 0.0001 * amm_usdc_leg_total
            else ("РЕАЛЬНЫЙ ПОТОК БОЛЬШЕ AMM-расчёта -- есть доп. приток/расход сверх свопа (нужно смотреть не-USDC ногу трейдера)"
                  if amm_vs_real_diff is not None and amm_vs_real_diff > 0
                  else ("РЕАЛЬНЫЙ ПОТОК МЕНЬШЕ AMM-расчёта -- часть AMM-выхода не дошла как USDC (проверить не-USDC ногу)"
                        if amm_vs_real_diff is not None and amm_vs_real_diff < 0 else "не определено"))
        ),
        "hook_amount_to_receiver_usdc": to_receiver,
        "hook_share_of_volume": (to_receiver / volume_usdc) if volume_usdc else None,
        "lp_fee_total_usd_equiv": total_lp_fee_usd,
        "lp_fee_share_of_volume": (total_lp_fee_usd / volume_usdc) if volume_usdc else None,
        "router_candidates": router_info,
        "trader_usdc_transfers": [{"from": t["from"], "to": t["to"], "usdc": to_human(t["token"], t["value"])}
                                   for t in trader_usdc_transfers],
        "trader_non_usdc_transfers": [{"from": t["from"], "to": t["to"], "token": t["token"],
                                        "human": to_human_generic(t["token"], t["value"], dec_cache)}
                                       for t in trader_non_usdc_transfers],
        "all_transfers_deduped": [{"token": t["token"], "from": t["from"], "to": t["to"],
                                    "human": to_human_generic(t["token"], t["value"], dec_cache)} for t in transfers],
    }

    router_cut = sum(r["net_kept_usdc"] for r in router_info if r["net_kept_usdc"] > 0)
    result["router_cut_usdc"] = router_cut
    result["router_share_of_volume"] = (router_cut / volume_usdc) if volume_usdc else None

    accounted = total_lp_fee_usd + to_receiver + router_cut
    result["conservation_check"] = {
        "volume_usdc": volume_usdc,
        "lp_fee_usd": total_lp_fee_usd, "hook_usd": to_receiver, "router_usd": router_cut,
        "sum_lp_hook_router": accounted,
        "note": "LP fee остаётся ВНУТРИ резервов пула (не отдельный Transfer) -- поэтому сравнение здесь не 'сумма=объём', а информативное: сколько объёма формально ушло на fee/хук/роутер по трём каналам.",
    }

    return result


def main() -> None:
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    dec_cache: dict = {}
    init_cache: dict = {}

    rows = []
    one_liners = []
    for txh in TARGET_TXS:
        r = analyze_tx(txh, dec_cache, init_cache)
        rows.append(r)
        if "error" in r:
            one_liners.append(f"{txh[:12]}...: ОШИБКА -- {r['error']}")
            continue
        vol = r["real_usdc_leg_total_settled_transfers"]
        lp_pct = (r["lp_fee_share_of_volume"] or 0) * 100
        hook_pct = (r["hook_share_of_volume"] or 0) * 100
        router_pct = (r["router_share_of_volume"] or 0) * 100
        verdict = r["amm_vs_real_interpretation"]
        mechanism = ("ИЗ доли трейдера/роутера (сверх AMM-математики денег НЕ появилось)" if "СХОДИТСЯ" in verdict
                     else ("СВЕРХ цены (реальный поток БОЛЬШЕ AMM-расчёта)" if "БОЛЬШЕ" in verdict
                           else ("из выхода AMM (трейдер получил МЕНЬШЕ, чем считает AMM)" if "МЕНЬШЕ" in verdict else "не определено")))
        one_liners.append(
            f"объём {vol:.4f} USDC: LP получил {r['lp_fee_total_usd_equiv']:.4f} ({lp_pct:.3f}%), "
            f"хук {r['hook_amount_to_receiver_usdc']:.4f} ({hook_pct:.3f}%), "
            f"роутер {r['router_cut_usdc']:.4f} ({router_pct:.3f}%), "
            f"хук берёт {mechanism}"
        )

    result["rows"] = rows
    result["one_liners"] = one_liners
    result["total_rpc_calls"] = _rpc_calls[0]

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    DATA_DIR.joinpath("task_arc_lp_hook_router_reconcile_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
