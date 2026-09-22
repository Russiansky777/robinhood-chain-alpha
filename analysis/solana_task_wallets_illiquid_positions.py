#!/usr/bin/env python3
"""Владелец: проверить ВСЕ кошельки задач на позиции в токенах БЕЗ
прямой SOL/USDC-ликвидности. ТОЛЬКО ЧТЕНИЕ.

Ни одного вызова покупки/продажи: в файле нет ни одного POST в DBot
(единственное обращение к DBot -- GET /automation/follow_orders, чтобы
взять список кошельков живьём, а не хардкодом). Guard (dbot-sold-guard)
и зонд (dbot-detect-probe) не затрагиваются.

ПОЧЕМУ ЭТО ВАЖНО -- измерено в пункте 6 этой же сессии: из 11 зависших
продаж у 4 (36%) ликвидность живёт ТОЛЬКО в паре с другим токеном
(LEVERCAT/$1, STONKY/STONK, TSLAX, NEURALINK). DBot -- не агрегатор, он
торгует прямую пару mint->SOL, поэтому такую позицию он продать не
может в принципе. Здесь тот же тест применяется ко всем остаткам на
всех кошельках задач, а не только к уже зависшим.

КЛАССИФИКАЦИЯ каждой позиции:
  НЕТ_ПАР        -- DexScreener не знает ни одного пула с этим минтом
  ТОЛЬКО_ЧУЖАЯ   -- пулы есть, но ни один не против SOL/WSOL/USDC/USDT
  ПРЯМАЯ_ТОНКАЯ  -- прямая пара есть, но её ликвидность < 1 SOL
  ПРЯМАЯ_ОК      -- прямая пара с ликвидностью >= 1 SOL
Первые три -- это и есть "без прямой SOL/USDC-ликвидности".

Порог 1 SOL и набор котируемых -- те же, что в пункте 6
(solana_hung_liquidity_probe.py), чтобы числа были сопоставимы.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "data" / "solana_task_wallets_illiquid_positions.json"

DBOT_HOST = "https://api-bot-v1.dbotx.com"
TOKEN_PROG = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTE_MINTS = {WSOL, USDC, USDT}
QUOTE_SYMS = {"SOL", "WSOL", "USDC", "USDT"}
# ПОЧЕМУ НЕ ПАЧКАМИ. /latest/dex/tokens принимает до 30 адресов, но ответ
# ограничен по числу пулов, и хвост минтов возвращается ПУСТЫМ -- то есть
# "пулов нет" неотличимо от "не влезло". В первом прогоне это дало 11
# ложных НЕТ_ПАР, среди них PEPEqnuu..., у которого пул с ликвидностью
# $51k уже был измерен в пункте 6. Спрашиваем по одному минту.
DS_ONE_BY_ONE = True


def scrub(s: str) -> str:
    for name in ("HELIUS_API_KEY", "HELIUS_API", "DBOT_API_KEY", "DBOT_APIKEY"):
        v = os.environ.get(name)
        if v and len(v) > 6:
            s = s.replace(v, f"<{name}>")
    return s


# ---------- RPC ----------

def rpc(method: str, params: list, key: str) -> dict | None:
    url = f"https://mainnet.helius-rpc.com/?api-key={key}"
    last = None
    for a in range(5):
        try:
            r = requests.post(url, json={"jsonrpc": "2.0", "id": 1,
                                          "method": method, "params": params}, timeout=45)
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}"
            time.sleep(1.5 * (a + 1))
            continue
        if r.status_code == 429 or 500 <= r.status_code < 600:
            last = f"http={r.status_code}"
            time.sleep(2 * (a + 1))
            continue
        if not r.ok:
            raise RuntimeError(f"{method}: http={r.status_code} {scrub(r.text[:200])}")
        b = r.json()
        if "error" in b:
            raise RuntimeError(f"{method}: {scrub(json.dumps(b['error'])[:200])}")
        return b.get("result")
    raise RuntimeError(f"{method}: исчерпаны попытки, последнее: {last}")


def token_positions(owner: str, key: str) -> list[dict]:
    """Все токен-счета владельца по ОБЕИМ программам. Token-2022 живёт в
    отдельной программе, и запрос только по Tokenkeg... его не видит --
    это уже ловилось в этой сессии на STONKY."""
    out: dict[str, dict] = {}
    for prog in (TOKEN_PROG, TOKEN_2022):
        res = rpc("getTokenAccountsByOwner", [owner, {"programId": prog},
                                               {"encoding": "jsonParsed"}], key)
        for acc in (res or {}).get("value") or []:
            info = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            mint = info.get("mint")
            amt = (info.get("tokenAmount") or {})
            ui = amt.get("uiAmount")
            if not mint or not ui:
                continue
            row = out.setdefault(mint, {"mint": mint, "amount": 0.0, "raw": 0,
                                         "decimals": amt.get("decimals"),
                                         "program": "token-2022" if prog == TOKEN_2022 else "token",
                                         "accounts": 0})
            row["amount"] += float(ui)
            try:
                row["raw"] += int(amt.get("amount") or 0)
            except (TypeError, ValueError):
                pass
            row["accounts"] += 1
    return sorted(out.values(), key=lambda r: -r["amount"])


def mint_info(mint: str, key: str) -> dict:
    res = rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}], key)
    val = (res or {}).get("value") or {}
    info = ((val.get("data") or {}).get("parsed") or {}).get("info", {})
    fee_bps = None
    for e in info.get("extensions") or []:
        if isinstance(e, dict) and e.get("extension") == "transferFeeConfig":
            fee_bps = ((e.get("state") or {}).get("newerTransferFee") or {}).get("transferFeeBasisPoints")
    return {"token2022": val.get("owner") == TOKEN_2022, "transfer_fee_bps": fee_bps}


# ---------- DexScreener ----------

def ds_tokens(mints: list[str]) -> tuple[dict[str, list[dict]], str | None]:
    """Пулы по пачке минтов. Возвращает ({mint: [пары]}, ошибка).
    Пустой список для минта -- это ЧЕСТНЫЙ ноль только если сам запрос
    прошёл: при ошибке возвращаем её текстом и не выдаём ноль за факт."""
    url = "https://api.dexscreener.com/latest/dex/tokens/" + ",".join(mints)
    err = None
    for a in range(4):
        try:
            r = requests.get(url, timeout=40)
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}"
            time.sleep(2 * (a + 1))
            continue
        if r.status_code == 429 or 500 <= r.status_code < 600:
            err = f"http={r.status_code}"
            time.sleep(3 * (a + 1))
            continue
        if r.status_code != 200:
            return {}, f"http={r.status_code}"
        try:
            b = r.json()
        except ValueError:
            return {}, "не JSON"
        by: dict[str, list[dict]] = {m: [] for m in mints}
        for p in (b.get("pairs") or []):
            base = (p.get("baseToken") or {})
            quote = (p.get("quoteToken") or {})
            for side, other in ((base, quote), (quote, base)):
                addr = side.get("address")
                if addr in by:
                    by[addr].append({
                        "dex": p.get("dexId"),
                        "pair": p.get("pairAddress"),
                        "other_addr": other.get("address"),
                        "other_sym": (other.get("symbol") or "").upper(),
                        "sym": (side.get("symbol") or ""),
                        "liq_usd": float((p.get("liquidity") or {}).get("usd") or 0.0),
                        "vol24_usd": float((p.get("volume") or {}).get("h24") or 0.0),
                        "price_usd": float(p.get("priceUsd") or 0.0) if side is base else None,
                    })
        for m in by:
            by[m].sort(key=lambda x: -x["liq_usd"])
        return by, None
    return {}, err or "исчерпаны попытки"


def sol_usd() -> float | None:
    by, err = ds_tokens([WSOL])
    if err:
        return None
    ps = [p for p in by.get(WSOL, []) if p["other_sym"] in ("USDC", "USDT") and p["price_usd"]]
    ps.sort(key=lambda p: -p["liq_usd"])
    return ps[0]["price_usd"] if ps else None


def classify(pairs: list[dict], thin_usd: float | None) -> tuple[str, dict]:
    direct = [p for p in pairs if p["other_addr"] in QUOTE_MINTS or p["other_sym"] in QUOTE_SYMS]
    best_direct = max((p["liq_usd"] for p in direct), default=0.0)
    best_any = max((p["liq_usd"] for p in pairs), default=0.0)
    det = {"n_пар": len(pairs), "n_прямых": len(direct),
           "лучшая_прямая_liq_usd": round(best_direct, 2),
           "лучшая_любая_liq_usd": round(best_any, 2),
           "лучшая_пара": (pairs[0]["dex"] + "/" + pairs[0]["other_sym"]) if pairs else None}
    if not pairs:
        return "НЕТ_ПАР", det
    if not direct:
        return "ТОЛЬКО_ЧУЖАЯ", det
    if thin_usd is not None and best_direct < thin_usd:
        return "ПРЯМАЯ_ТОНКАЯ", det
    return "ПРЯМАЯ_ОК", det


# ---------- DBot: только список кошельков ----------

def fetch_wallets(api_key: str) -> dict[str, str]:
    for a in range(5):
        try:
            r = requests.get(f"{DBOT_HOST}/automation/follow_orders",
                             headers={"X-API-KEY": api_key}, timeout=30)
        except Exception as exc:  # noqa: BLE001
            time.sleep(2 * (a + 1))
            if a == 4:
                raise RuntimeError(f"follow_orders: {type(exc).__name__}") from exc
            continue
        if r.status_code == 429:
            time.sleep(3 * (a + 1))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"follow_orders вернул http={r.status_code}")
        b = r.json()
        items = b if isinstance(b, list) else next(
            (b.get(k) for k in ("res", "data", "results", "list", "items")
             if isinstance(b.get(k), list)), None)
        if items is None:
            raise RuntimeError("follow_orders: тело не похоже на список -- не выдаю это за пустой список")
        out = {}
        for row in items:
            addr = row.get("walletAddress")
            if addr:
                out[addr] = row.get("name") or addr
        if not out:
            raise RuntimeError("follow_orders вернул 0 задач -- пустой список кошельков, не продолжаю")
        return out
    raise RuntimeError("follow_orders: исчерпаны попытки")


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-usd", type=float, default=0.0,
                    help="не показывать позиции дешевле, USD (0 = все)")
    args = ap.parse_args()

    hel = os.environ.get("HELIUS_API_KEY") or os.environ.get("HELIUS_API") or ""
    dbot = os.environ.get("DBOT_API_KEY") or os.environ.get("DBOT_APIKEY") or ""
    if not hel:
        print("СТОП: нет HELIUS_API_KEY", file=sys.stderr)
        return 1
    if not dbot:
        print("СТОП: нет DBOT_API_KEY -- список кошельков берётся живьём, хардкодить не буду",
              file=sys.stderr)
        return 1

    wallets = fetch_wallets(dbot)
    print(f"кошельков задач (живьём из follow_orders): {len(wallets)}")

    sol = sol_usd()
    thin_usd = sol * 1.0 if sol else None
    print(f"SOL/USD = {sol}; порог 'тонкой' прямой пары = {thin_usd} USD (1 SOL)")

    positions: list[dict] = []
    errors: list[dict] = []
    for addr, name in sorted(wallets.items(), key=lambda kv: kv[1]):
        try:
            pos = token_positions(addr, hel)
        except RuntimeError as exc:
            errors.append({"кошелёк": addr, "имя": name, "ошибка": scrub(str(exc))[:200]})
            print(f"  {name:<10} {addr}: ОШИБКА {scrub(str(exc))[:120]}")
            continue
        print(f"  {name:<10} {addr}: ненулевых токен-позиций {len(pos)}")
        for p in pos:
            positions.append({**p, "wallet": addr, "task_name": name})

    mints = sorted({p["mint"] for p in positions})
    print(f"уникальных минтов: {len(mints)}")

    ds: dict[str, list[dict]] = {}
    ds_err: dict[str, str] = {}
    for m in mints:
        by, err = ds_tokens([m])
        if err:
            ds_err[m] = err
        else:
            ds[m] = by.get(m, [])
        time.sleep(0.35)

    info: dict[str, dict] = {}
    for m in mints:
        try:
            info[m] = mint_info(m, hel)
        except RuntimeError as exc:
            info[m] = {"ошибка": scrub(str(exc))[:120]}

    # Отдельная проверка: 11 минтов из зависших продаж (пункт 6) не попали
    # в перечень остатков. Это либо "уже продано", либо дыра в переборе
    # токен-счетов -- разница существенная, поэтому спрашиваем баланс
    # каждого такого минта у каждого кошелька НАПРЯМУЮ, с фильтром по
    # минту, а не делаем вывод из отсутствия в списке.
    hung_check = []
    hung_path = REPO / "data" / "solana_hung_liquidity.json"
    if hung_path.exists():
        try:
            hung = [t["mint"] for t in (json.loads(hung_path.read_text()).get("токены") or [])]
        except (ValueError, KeyError, TypeError):
            hung = []
        for m in hung:
            tot, per = 0.0, {}
            err = None
            for addr, name in wallets.items():
                try:
                    res = rpc("getTokenAccountsByOwner", [addr, {"mint": m},
                                                          {"encoding": "jsonParsed"}], hel)
                except RuntimeError as exc:
                    err = scrub(str(exc))[:120]
                    continue
                for acc in (res or {}).get("value") or []:
                    info = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
                    ui = float((info.get("tokenAmount") or {}).get("uiAmount") or 0.0)
                    if ui:
                        per[name] = per.get(name, 0.0) + ui
                        tot += ui
            hung_check.append({"mint": m, "остаток_всего": tot, "по_кошелькам": per,
                                "ошибка": err})
        z = sum(1 for h in hung_check if not h["остаток_всего"] and not h["ошибка"])
        print(f"зависшие минты: у {z} из {len(hung_check)} остаток сейчас НОЛЬ "
              f"(прямой запрос по минту, не вывод из отсутствия в списке)")

    rows = []
    for p in positions:
        m = p["mint"]
        if m in ds_err:
            verdict, det = "ОШИБКА_DEXSCREENER", {"ошибка": ds_err[m]}
            price = None
        else:
            pairs = ds.get(m, [])
            verdict, det = classify(pairs, thin_usd)
            pr = [x["price_usd"] for x in pairs if x.get("price_usd")]
            price = pr[0] if pr else None
        usd = round(p["amount"] * price, 2) if price else None
        sym = next((x["sym"] for x in ds.get(m, []) if x.get("sym")), None)
        rows.append({**p, "symbol": sym, "цена_usd": price, "стоимость_usd": usd,
                     "вердикт": verdict, "ликвидность": det,
                     "token2022": (info.get(m) or {}).get("token2022"),
                     "transfer_fee_bps": (info.get(m) or {}).get("transfer_fee_bps")})

    shown = [r for r in rows if args.min_usd <= 0 or (r["стоимость_usd"] or 0) >= args.min_usd]
    shown.sort(key=lambda r: (r["вердикт"] == "ПРЯМАЯ_ОК", -(r["стоимость_usd"] or 0)))

    bad = {"НЕТ_ПАР", "ТОЛЬКО_ЧУЖАЯ", "ПРЯМАЯ_ТОНКАЯ"}
    summary = {
        "кошельков": len(wallets),
        "позиций_всего": len(rows),
        "минтов_уникальных": len(mints),
        "по_вердикту": {v: sum(1 for r in rows if r["вердикт"] == v)
                        for v in sorted({r["вердикт"] for r in rows})},
        "позиций_без_прямой_ликвидности": sum(1 for r in rows if r["вердикт"] in bad),
        "стоимость_без_прямой_ликвидности_usd": round(
            sum(r["стоимость_usd"] or 0 for r in rows if r["вердикт"] in bad), 2),
        "стоимость_всех_позиций_usd": round(sum(r["стоимость_usd"] or 0 for r in rows), 2),
        "sol_usd": sol,
    }

    out = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "режим": "ТОЛЬКО ЧТЕНИЕ: ни одной покупки/продажи, ни одного POST в DBot",
        "порог_тонкой_прямой_пары_usd": thin_usd,
        "summary": summary,
        "wallets": wallets,
        "errors": errors,
        "зависшие_минты_остаток_сейчас": hung_check,
        "positions": rows,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))

    print()
    print("=" * 118)
    print("ПОЗИЦИИ КОШЕЛЬКОВ ЗАДАЧ: есть ли прямая пара с SOL/USDC")
    print(f"  {'задача':<10}{'символ':<12}{'минт':<46}{'кол-во':>16}{'$':>10}  {'вердикт':<18}"
          f"{'прям.liq$':>11}{'люб.liq$':>11}")
    for r in shown:
        print(f"  {str(r['task_name'])[:9]:<10}{str(r['symbol'] or '?')[:11]:<12}{r['mint']:<46}"
              f"{r['amount']:>16.4f}{(r['стоимость_usd'] if r['стоимость_usd'] is not None else -1):>10.2f}"
              f"  {r['вердикт']:<18}"
              f"{(r['ликвидность'].get('лучшая_прямая_liq_usd', -1)):>11.0f}"
              f"{(r['ликвидность'].get('лучшая_любая_liq_usd', -1)):>11.0f}")
    print()
    print("ИТОГО:", json.dumps(summary, ensure_ascii=False))
    if hung_check:
        print()
        print("ЗАВИСШИЕ МИНТЫ (пункт 6) -- остаток на кошельках задач СЕЙЧАС:")
        for h in hung_check:
            print(f"  {h['mint']:<46}{h['остаток_всего']:>20.6f}  "
                  f"{json.dumps(h['по_кошелькам'], ensure_ascii=False)}"
                  f"{'  ОШИБКА: ' + h['ошибка'] if h['ошибка'] else ''}")
    if errors:
        print("ОШИБКИ ЧТЕНИЯ:", json.dumps(errors, ensure_ascii=False))
    print(f"файл: {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
