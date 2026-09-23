#!/usr/bin/env python3
"""Инвентаризация девяти кошельков задач: что на них лежит и что вернётся.

По каждому кошельку:
  * нативный SOL;
  * токен-счета с ненулевым остатком: минт, символ, количество, программа
    токена (классический SPL или Token-2022);
  * оценка позиции в SOL -- по цене НАШЕЙ последней сделки по этому
    токену, а не по текущей цене пула. Это честная верхняя граница:
    зависшая позиция потому и зависла, что по этой цене её не продать;
  * число пустых токен-счетов и запертая в них рента;
  * сколько SOL вернётся при закрытии пустых счетов.

Закрытие счёта возвращает владельцу ровно его лампорты -- поэтому
"вернётся" считается суммой лампортов пустых счетов, а не нормативом
ренты: у счетов Token-2022 с расширениями она больше.

Только чтение цепочки. Ни одной продажи, ни одного перевода.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

OUT_PATH = REPO_ROOT / "data" / "solana_wallet_inventory.json"
CONFIG_PATH = REPO_ROOT / "data" / "dbot_task_config_readonly.json"

TOKEN_CLASSIC = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
WSOL = "So11111111111111111111111111111111111111112"

# Белый список кошельков задач. Берётся из снимка конфигурации, а не из
# истории транзакций: в истории бывают поддельные адреса-двойники.
FALLBACK_WALLETS = {
    "BATCH-8": "4s87RRC2V2XAJD6R8U2dP8kQH99Z2wA6fg88ZVfV4j4N",
    "BATCH-7": "CqoAZUaTHxVYgTuDz5AoEZU4VYmfFPDEmTTcJAJoAWER",
    "BATCH-6": "DE5yR9S8qBkh8n4rYVvc6iGDF3rqrEPZX2aHBsDWJ79n",
    "BATCH-5": "5Y8h877swoTzTdc8in9hU3SvXXVv1q9p19Y85tAsdqBv",
    "BATCH-4": "EjeXrxabRKmwLxvfQdXYN3oD3d3uWe2fuqqA5Qda2p8N",
    "BATCH-3": "BmjAUDbwBMxR5shrmzBtKRwveVahFGFiEH3oTq7QTHnu",
    "BATCH-2": "HMQG8xXoVBWZTkEqNye5WcfttvdZwVb522EggFD5AWZ6",
    "BATCH-1": "GYPzYfSP3htyfRCti5Wp6XTnUQh7zkwqTv6j7r4kUFrq",
    "pointfarmcap": "E1qAJBmrJDhBvm2sV8kfMXFAmgzHuSRNRosPEgMkKiWS",
}


def wallets() -> dict:
    """Кошельки из снимка конфигурации задач; нет снимка -- из белого списка."""
    if CONFIG_PATH.exists():
        try:
            d = json.loads(CONFIG_PATH.read_text())
            out = {t["name"]: t["wallet"] for t in (d.get("задачи") or [])
                   if t.get("name") and t.get("wallet")}
            if out:
                return out
        except (ValueError, OSError, KeyError):
            pass
    return dict(FALLBACK_WALLETS)


def parse_accounts(resp: dict, program: str) -> list:
    """Разбор ответа getTokenAccountsByOwner в плоские записи."""
    out = []
    for it in ((resp or {}).get("value") or []):
        acc = it.get("account") or {}
        info = (((acc.get("data") or {}).get("parsed") or {}).get("info") or {})
        amt = info.get("tokenAmount") or {}
        out.append({
            "счёт": it.get("pubkey"),
            "минт": info.get("mint"),
            "количество": float(amt.get("uiAmount") or 0.0),
            "десятичных": amt.get("decimals"),
            "сырое_количество": amt.get("amount"),
            "лампорты_счёта": acc.get("lamports") or 0,
            "программа_токена_id": acc.get("owner") or program,
            "программа_токена": ("Token-2022" if (acc.get("owner") or program) == TOKEN_2022
                                  else "SPL Token (классический)"),
        })
    return out


def token_accounts(rpc, wallet: str) -> tuple[list, list]:
    """Счета обеих программ. Ошибка одной программы не прячет другую."""
    счета, сбои = [], []
    for prog in (TOKEN_CLASSIC, TOKEN_2022):
        try:
            r = rpc.call("getTokenAccountsByOwner",
                          [wallet, {"programId": prog}, {"encoding": "jsonParsed"}])
        except RuntimeError as exc:
            сбои.append({"программа": prog, "почему": str(exc)[:160]})
            continue
        счета.extend(parse_accounts(r, prog))
    return счета, сбои


def mint_symbol(rpc, mint: str, кэш: dict) -> dict:
    """Символ и программа минта. У классического SPL метаданные лежат в
    отдельном аккаунте Metaplex -- его мы не читаем, и символ честно
    остаётся пустым, а не выдумывается."""
    if mint in кэш:
        return кэш[mint]
    out = {"минт": mint, "символ": None, "программа_токена": None,
            "ставка_комиссии_bps": None}
    try:
        r = rpc.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    except RuntimeError as exc:
        out["почему"] = f"минт не прочитался: {str(exc)[:120]}"
        кэш[mint] = out
        return out
    val = (r or {}).get("value") or {}
    owner = val.get("owner")
    out["программа_токена"] = ("Token-2022" if owner == TOKEN_2022 else
                               "SPL Token (классический)" if owner == TOKEN_CLASSIC else
                               f"другая ({owner})")
    info = (((val.get("data") or {}).get("parsed") or {}).get("info") or {})
    for e in (info.get("extensions") or []):
        if not isinstance(e, dict):
            continue
        st = e.get("state") or {}
        if e.get("extension") == "tokenMetadata":
            out["символ"] = st.get("symbol")
            out["название"] = st.get("name")
        if e.get("extension") == "transferFeeConfig":
            out["ставка_комиссии_bps"] = (st.get("newerTransferFee") or {}).get(
                "transferFeeBasisPoints")
    if out["символ"] is None and out["программа_токена"].startswith("SPL"):
        out["почему_нет_символа"] = ("классический SPL: символ лежит в аккаунте "
                                      "метаданных Metaplex, который мы не читаем")
    кэш[mint] = out
    return out


def price_from_last_trade(rpc, счёт: str, wallet: str, mint: str) -> dict:
    """Цена по ПОСЛЕДНЕЙ транзакции, тронувшей этот токен-счёт.

    Это цена нашей сделки, а не текущая цена пула -- так и подписано.
    Два вызова на позицию: подписи счёта и сама транзакция.
    """
    from solana_santa_trade_autopsy import trade_of  # noqa: PLC0415
    try:
        sigs = rpc.call("getSignaturesForAddress", [счёт, {"limit": 1}]) or []
    except RuntimeError as exc:
        return {"почему": f"подписи счёта не отдались: {str(exc)[:120]}"}
    if not sigs:
        return {"почему": "у счёта нет транзакций"}
    sig = sigs[0].get("signature")
    tx = (rpc.transactions([sig]) or {}).get(sig)
    if not tx:
        return {"почему": "транзакция не отдалась"}
    tr = trade_of(tx, mint)
    if not tr or not tr.get("цена"):
        return {"подпись": sig, "почему": "в этой транзакции цена не выводится"}
    return {"подпись": sig, "цена_sol_за_токен": tr["цена"],
             "что_это": "цена НАШЕЙ последней сделки по токену, не текущая цена пула",
             "время_utc": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(tx["blockTime"]))
                            if tx.get("blockTime") else None)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-interval-s", type=float, default=0.05)
    ap.add_argument("--price-positions", type=int, default=250,
                     help="сколько позиций оценивать по последней сделке (0 -- не оценивать)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return

    from solana_crowd_scan import Rpc, helius_key  # noqa: PLC0415
    key, _ = helius_key()
    rpc = Rpc(key, min_interval_s=args.min_interval_s, workers=1, service="разбор_пилота")

    кошельки = wallets()
    кэш_минтов: dict = {}
    отчёт = {
        "собрано_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ЧЕСТНЫЕ_ОГОВОРКИ": [
            "Только чтение цепочки: ни продаж, ни переводов.",
            "Оценка позиции -- по цене НАШЕЙ последней сделки по токену, а не по "
            "текущей цене пула. Для зависших позиций это верхняя граница: они потому "
            "и зависли, что по этой цене их не продать.",
            "Возврат при закрытии пустых счетов -- сумма лампортов самих счетов; "
            "у счетов Token-2022 с расширениями она больше обычной ренты.",
            "Символ классического SPL-токена не читается: он лежит в отдельном "
            "аккаунте метаданных Metaplex. Пусто -- значит не прочитано, а не 'нет'.",
        ],
        "кошельки": {},
    }
    итог = {"sol": 0.0, "ненулевых_позиций": 0, "пустых_счетов": 0,
             "рента_в_пустых_sol": 0.0, "рента_в_ненулевых_sol": 0.0,
             "ненулевых_token2022": 0, "оценка_позиций_sol": 0.0,
             "позиций_без_оценки": 0}
    оценено = 0

    for имя, адрес in кошельки.items():
        w = {"адрес": адрес}
        try:
            bal = rpc.call("getBalance", [адрес])
            w["sol"] = ((bal or {}).get("value") or 0) / 1e9
        except RuntimeError as exc:
            w["sol"] = None
            w["почему_нет_sol"] = str(exc)[:160]
        счета, сбои = token_accounts(rpc, адрес)
        if сбои:
            w["сбои_чтения"] = сбои
        ненулевые = [a for a in счета if a["количество"] > 0]
        пустые = [a for a in счета if a["количество"] <= 0]
        w["всего_токен_счетов"] = len(счета)
        w["пустых_токен_счетов"] = len(пустые)
        w["рента_в_пустых_sol"] = round(sum(a["лампорты_счёта"] for a in пустые) / 1e9, 9)
        w["рента_в_ненулевых_sol"] = round(sum(a["лампорты_счёта"] for a in ненулевые) / 1e9, 9)
        w["вернётся_при_закрытии_пустых_sol"] = w["рента_в_пустых_sol"]
        позиции = []
        for a in sorted(ненулевые, key=lambda x: -x["количество"]):
            mi = mint_symbol(rpc, a["минт"], кэш_минтов)
            поз = {**a, "символ": mi.get("символ"), "название": mi.get("название"),
                    "ставка_комиссии_bps": mi.get("ставка_комиссии_bps")}
            if args.price_positions and оценено < args.price_positions:
                ц = price_from_last_trade(rpc, a["счёт"], адрес, a["минт"])
                оценено += 1
                поз["оценка"] = ц
                if ц.get("цена_sol_за_токен"):
                    поз["оценка_sol"] = ц["цена_sol_за_токен"] * a["количество"]
                    итог["оценка_позиций_sol"] += поз["оценка_sol"]
                else:
                    итог["позиций_без_оценки"] += 1
            else:
                поз["оценка"] = {"почему": "потолок числа оцениваемых позиций исчерпан"}
                итог["позиций_без_оценки"] += 1
            позиции.append(поз)
        w["позиции"] = позиции
        w["ненулевых_позиций"] = len(ненулевые)
        w["из_них_token2022"] = sum(1 for a in ненулевые
                                     if a["программа_токена"] == "Token-2022")
        отчёт["кошельки"][имя] = w
        итог["sol"] += w.get("sol") or 0.0
        итог["ненулевых_позиций"] += len(ненулевые)
        итог["пустых_счетов"] += len(пустые)
        итог["рента_в_пустых_sol"] += w["рента_в_пустых_sol"]
        итог["рента_в_ненулевых_sol"] += w["рента_в_ненулевых_sol"]
        итог["ненулевых_token2022"] += w["из_них_token2022"]
        print(f"[инвентарь] {имя}: SOL {w.get('sol')}, позиций {len(ненулевые)}, "
              f"пустых {len(пустые)}, рента в пустых {w['рента_в_пустых_sol']:.6f}",
              flush=True)

    for k in ("sol", "рента_в_пустых_sol", "рента_в_ненулевых_sol", "оценка_позиций_sol"):
        итог[k] = round(итог[k], 9)
    итог["вернётся_при_закрытии_пустых_sol"] = итог["рента_в_пустых_sol"]
    итог["вернётся_при_закрытии_ВСЕХ_счетов_sol"] = round(
        итог["рента_в_пустых_sol"] + итог["рента_в_ненулевых_sol"], 9)
    итог["оговорка_к_закрытию_всех"] = ("счёт с ненулевым остатком закрыть нельзя, "
                                         "пока остаток не выведен -- это верхняя граница")
    отчёт["итог"] = итог
    OUT_PATH.write_text(json.dumps(отчёт, ensure_ascii=False, indent=2))
    print(json.dumps(итог, ensure_ascii=False, indent=2))
    print(f"\nвыгрузка -> {OUT_PATH}")


def self_test() -> None:
    checks = []

    def chk(n, ok, got=""):
        checks.append((n, bool(ok), got))

    resp = {"value": [
        {"pubkey": "ACC1", "account": {"lamports": 2039280, "owner": TOKEN_CLASSIC,
          "data": {"parsed": {"info": {"mint": "M1", "tokenAmount": {
              "uiAmount": 12.5, "decimals": 6, "amount": "12500000"}}}}}},
        {"pubkey": "ACC2", "account": {"lamports": 2074080, "owner": TOKEN_2022,
          "data": {"parsed": {"info": {"mint": "M2", "tokenAmount": {
              "uiAmount": 0, "decimals": 6, "amount": "0"}}}}}},
    ]}
    acc = parse_accounts(resp, TOKEN_CLASSIC)
    chk("счета разобраны", len(acc) == 2)
    chk("программа берётся у самого счёта, а не у запроса",
        acc[1]["программа_токена"] == "Token-2022", acc[1]["программа_токена"])
    chk("ненулевой остаток прочитан", acc[0]["количество"] == 12.5)
    chk("пустой счёт опознан как пустой", acc[1]["количество"] == 0)
    chk("лампорты счёта сохранены", acc[1]["лампорты_счёта"] == 2074080)
    chk("пустой ответ не роняет разбор", parse_accounts({}, TOKEN_CLASSIC) == [])

    class Rpc2022:
        def call(self, method, params):
            return {"value": {"owner": TOKEN_2022, "data": {"parsed": {"info": {
                "extensions": [
                    {"extension": "tokenMetadata", "state": {"symbol": "GP",
                                                              "name": "RuneScape Gold"}},
                    {"extension": "transferFeeConfig", "state": {
                        "newerTransferFee": {"transferFeeBasisPoints": 300}}}]}}}}}

    кэш = {}
    mi = mint_symbol(Rpc2022(), "M2", кэш)
    chk("символ Token-2022 прочитан", mi["символ"] == "GP", str(mi))
    chk("ставка комиссии прочитана", mi["ставка_комиссии_bps"] == 300)
    chk("минт кэшируется", "M2" in кэш)

    class RpcClassic:
        def call(self, method, params):
            return {"value": {"owner": TOKEN_CLASSIC, "data": {"parsed": {"info": {}}}}}

    mc = mint_symbol(RpcClassic(), "M3", {})
    chk("у классического символ пуст", mc["символ"] is None)
    chk("и сказано почему", "Metaplex" in (mc.get("почему_нет_символа") or ""))

    class Dead:
        def call(self, method, params):
            raise RuntimeError("узел молчит")

    md = mint_symbol(Dead(), "M4", {})
    chk("минт не прочитался -- честная причина", "не прочитался" in (md.get("почему") or ""))
    сч, сб = token_accounts(Dead(), "W")
    chk("сбой чтения счетов не прячется", len(сб) == 2 and сч == [], str(сб)[:80])

    w = wallets()
    chk("кошельков девять", len(w) == 9, str(len(w)))
    chk("пилот в списке", "pointfarmcap" in w)

    bad = 0
    for n, ok, got in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {n}" + (f"  -> {got}" if got and not ok else ""))
        bad += (not ok)
    print(f"самопроверка инвентаризации: {len(checks) - bad}/{len(checks)} пройдено")
    if bad:
        raise SystemExit(f"самопроверка не пройдена: {bad} из {len(checks)}")


if __name__ == "__main__":
    main()
