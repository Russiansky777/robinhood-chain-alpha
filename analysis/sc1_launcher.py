#!/usr/bin/env python3
"""SC1 -- запуск токенов на Pons V2 (`PonsV2LaunchFactory`), задача 4
дозапроса владельца (2026-09-02).

**По умолчанию -- DRY-RUN.** Реальная отправка (подпись+broadcast)
происходит ТОЛЬКО при явном `--confirm-mainnet` И только в пределах
`--limit` токенов за один прогон (защита от случайного залпа по всем
20 сразу). Перед КАЖДОЙ реальной отправкой -- живая оценка газа
(`eth_estimateGas`) и остановка, если итоговая стоимость газа в USD
превышает `--gas-ceiling-usd` (потолок, не превышаем автоматически ни
при каких обстоятельствах -- явный exit, не понижение суммы).

Ключ -- ТОЛЬКО из переменной окружения `PRIVATE_KEY_NOX` (секрет,
никогда не хардкодится, никогда не логируется, никогда не пишется в
data/sc1_launches.json). Адрес кошелька, который ключ ДОЛЖЕН давать
(сверяется перед любой отправкой, а не предполагается) --
`0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75`.

Использование:
    # dry-run одного токена (по умолчанию) -- НИЧЕГО не отправляет:
    python analysis/sc1_launcher.py --symbols NVX

    # dry-run нескольких:
    python analysis/sc1_launcher.py --symbols NVX,QRT,BLZ

    # РЕАЛЬНАЯ отправка -- требует явного флага И лимита:
    python analysis/sc1_launcher.py --symbols NVX --confirm-mainnet --limit 1

Реестр: `data/sc1_launches.json` -- одна запись на КАЖДЫЙ реальный
запуск (не на dry-run), коммит сразу после добавления записи (та же
дисциплина, что `data/credits_spent.json` и `data/sprint*_cache/` по
всему проекту -- ничего не теряется, если job оборвётся на середине).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pons_v2_common as v2  # noqa: E402
from pons_v2_common import Socials, TokenParams  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
LOGOS_DIR = REPO_ROOT / "assets" / "logos"
REGISTRY_PATH = REPO_ROOT / "data" / "sc1_launches.json"

OUR_WALLET = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"

# Резервный курс -- медиана dex.trades 01-13.08.2026 (см. docs/SC1_NOTE.md,
# "Курс ETH/USD"), используется ТОЛЬКО если живой публичный источник
# недоступен (см. _eth_usd_price ниже) -- явно помечается как fallback
# в отчёте, не выдаётся молча за текущую цену.
_FALLBACK_ETH_USD = 1895.565143603286

FILENAME_RE = re.compile(r"^\d+_([A-Z0-9]+)\.png$")


def _git_head_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def load_catalog() -> dict[str, Path]:
    """symbol -> путь к PNG, из имени файла (вторая часть -- символ,
    см. docs/SC1_LAUNCHER.md, "Изображения"). Имя/описание НЕ
    придумываются здесь -- name=symbol, description="" по умолчанию
    (честно пусто, пока владелец не даст реальный текст -- см. правило
    "никогда не выдумывай данные")."""
    catalog = {}
    for p in sorted(LOGOS_DIR.glob("*.png")):
        m = FILENAME_RE.match(p.name)
        if not m:
            print(f"[sc1_launcher] пропускаю {p.name} -- не соответствует шаблону NN_SYMBOL.png")
            continue
        catalog[m.group(1)] = p
    return catalog


def _logo_url(image_path: Path, sha: str) -> str:
    rel = image_path.relative_to(REPO_ROOT).as_posix()
    return f"https://raw.githubusercontent.com/Russiansky777/robinhood-chain-alpha/{sha}/{rel}"


def _eth_usd_price() -> tuple[float, str]:
    """Живой курс ETH/USD (CoinGecko, публичный API без ключа) --
    GH Actions runner имеет обычный интернет (в отличие от этой
    интерактивной сессии, см. docs/PROJECT_STATE.md) -- реальный
    источник вместо переиспользования устаревшей Dune-медианы, где это
    возможно. Фолбэк на неё -- только если запрос не удался, явно
    помечено в возвращаемом источнике."""
    import requests

    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "ethereum", "vs_currencies": "usd"},
            timeout=10,
        )
        resp.raise_for_status()
        price = float(resp.json()["ethereum"]["usd"])
        return price, "coingecko (live)"
    except Exception as e:  # noqa: BLE001 -- сеть недоступна/формат ответа неожиданный, честный фолбэк
        print(f"[sc1_launcher] CoinGecko недоступен ({e}) -- использую последнюю Dune-медиану как фолбэк.")
        return _FALLBACK_ETH_USD, "Dune median 01-13.08.2026 (STALE fallback, docs/SC1_NOTE.md)"


def _pick_launch_config(pair_token: str) -> int:
    n = v2.launch_config_count()
    for i in range(n):
        cfg = v2.get_launch_config(i)
        if cfg.enabled:
            return i
    raise RuntimeError(f"Ни один из {n} launchConfig'ов не enabled -- запуск невозможен.")


def _build_params(symbol: str, image_path: Path, sha: str, description: str, creator_tax_bps: int, buyback_enabled: bool) -> TokenParams:
    logo_url = _logo_url(image_path, sha)
    # salt -- namespaced per-caller (см. docs/SC1_LAUNCHER.md), уникальности
    # достаточно в пределах НАШИХ собственных запусков -- символ + время.
    salt_input = f"sc1-launcher:{OUR_WALLET}:{symbol}:{int(time.time())}".encode()
    salt = hashlib.sha256(salt_input).digest()  # 32 байта, не обязательно keccak -- контракту всё равно, это просто соль
    return TokenParams(
        name=symbol,  # честно = symbol, маркетинговое имя не придумано (см. load_catalog)
        symbol=symbol,
        logo=logo_url,
        description=description,
        socials=Socials(),
        creator_fee_recipient=OUR_WALLET,
        creator_tax_bps=creator_tax_bps,
        buyback_enabled=buyback_enabled,
        expected_economics=b"\x00" * 32,  # подставляется реальным digest'ом ниже, до отправки
        salt=salt,
    )


def prepare_one(symbol: str, image_path: Path, args, sha: str) -> dict:
    """Общая подготовка (recon-проверки, calldata, оценка газа) --
    используется и в dry-run, и перед реальной отправкой. Ничего не
    подписывает и не отправляет."""
    report: dict = {"symbol": symbol, "image": str(image_path.relative_to(REPO_ROOT))}

    chain_id = v2.eth_chain_id()
    report["chain_id"] = chain_id
    if chain_id != 4663:
        report["abort_reason"] = f"chainId {chain_id} != 4663 (ожидаемый Robinhood Chain) -- СТОП"
        return report

    can_we = v2.can_launch(OUR_WALLET)
    report["can_launch"] = can_we
    if not can_we:
        report["abort_reason"] = (
            f"canLaunch({OUR_WALLET}) = False -- запуск заблокирован "
            "(launchEnabled=False и адрес не в whitelistedLaunchers). "
            "Отправка привела бы к revert NotWhitelisted()."
        )
        return report

    # По умолчанию -- нативный ETH (address(0)), см. pons_v2_common.py::
    # NATIVE_PAIR_TOKEN -- контракт ЦЕЛИКОМ пропускает approvedPairTokens
    # для address(0) (`pairToken != address(0) && !approvedPairTokens[...]`),
    # подтверждено live: CANDIDATE_WETH (гипотеза из V1) approvedPairTokens=False.
    pair_token = args.pair_token or v2.NATIVE_PAIR_TOKEN
    is_native = pair_token.lower() == v2.NATIVE_PAIR_TOKEN.lower()
    pair_ok = True if is_native else v2.approved_pair_tokens(pair_token)
    report["pair_token"] = pair_token
    report["pair_token_is_native_eth"] = is_native
    report["pair_token_approved"] = pair_ok
    if not pair_ok:
        report["abort_reason"] = f"approvedPairTokens[{pair_token}] = False -- revert PairTokenNotApproved()"
        return report

    launch_config_id = args.launch_config_id
    if launch_config_id is None:
        launch_config_id = _pick_launch_config(pair_token)
    report["launch_config_id"] = launch_config_id

    fee_wei = v2.launch_fee()
    report["launch_fee_wei"] = fee_wei
    report["launch_fee_eth"] = fee_wei / 1e18

    description = args.description or ""
    params = _build_params(symbol, image_path, sha, description, args.creator_tax_bps, args.buyback_enabled)
    economics = v2.preview_launch_economics(launch_config_id, pair_token)
    params = TokenParams(**{**asdict_shallow(params), "expected_economics": economics})

    calldata = v2.build_launch_calldata(params, launch_config_id, pair_token)
    report["calldata_len_bytes"] = len(calldata)
    report["token_params"] = {
        "name": params.name, "symbol": params.symbol, "logo": params.logo,
        "description": params.description, "has_description": bool(params.description),
        "socials": params.socials.as_tuple(),
        "creator_fee_recipient": params.creator_fee_recipient,
        "creator_tax_bps": params.creator_tax_bps,
        "buyback_enabled": params.buyback_enabled,
        "expected_economics": "0x" + params.expected_economics.hex(),
        "salt": "0x" + params.salt.hex(),
    }

    try:
        gas_units = v2.eth_estimate_gas(OUR_WALLET, calldata, fee_wei)
    except Exception as e:  # noqa: BLE001 -- ошибка оценки (в т.ч. revert) -- честно доложить, не гадать про причину
        report["abort_reason"] = f"eth_estimateGas упал: {e}"
        return report
    gas_price_wei = v2.eth_gas_price()
    eth_usd, eth_usd_source = _eth_usd_price()

    gas_cost_wei = gas_units * gas_price_wei
    gas_cost_eth = gas_cost_wei / 1e18
    gas_cost_usd = gas_cost_eth * eth_usd
    launch_fee_usd = report["launch_fee_eth"] * eth_usd
    total_cost_usd = gas_cost_usd + launch_fee_usd

    report.update({
        "gas_units_estimated": gas_units,
        "gas_price_wei": gas_price_wei,
        "gas_price_gwei": gas_price_wei / 1e9,
        "gas_cost_eth": gas_cost_eth,
        "gas_cost_usd": gas_cost_usd,
        "launch_fee_usd": launch_fee_usd,
        "total_cost_usd": total_cost_usd,
        "eth_usd_price": eth_usd,
        "eth_usd_source": eth_usd_source,
        "gas_ceiling_usd": args.gas_ceiling_usd,
        "under_gas_ceiling": gas_cost_usd <= args.gas_ceiling_usd,
    })
    if gas_cost_usd > args.gas_ceiling_usd:
        report["abort_reason"] = (
            f"Оценка газа ${gas_cost_usd:.4f} превышает потолок ${args.gas_ceiling_usd:.4f} -- СТОП, не отправляю."
        )
    report["_calldata_hex"] = "0x" + calldata.hex()  # для реальной отправки, не для reestr
    report["_params"] = params
    report["_pair_token"] = pair_token
    return report


def asdict_shallow(params: TokenParams) -> dict:
    d = asdict(params)
    d["socials"] = params.socials  # asdict() рекурсивно разворачивает dataclass -- нам нужен объект обратно
    return d


def send_one(report: dict, args) -> dict:
    """Реальная подпись + отправка. Вызывается ТОЛЬКО если
    --confirm-mainnet передан явно, лимит не исчерпан, и prepare_one()
    не выставил abort_reason."""
    import os

    from eth_account import Account

    priv_hex = os.environ.get("PRIVATE_KEY_NOX", "")
    if not priv_hex:
        raise RuntimeError("PRIVATE_KEY_NOX не задан в окружении -- реальная отправка невозможна.")
    if priv_hex.startswith("0x"):
        priv_hex = priv_hex[2:]
    account = Account.from_key(bytes.fromhex(priv_hex))
    if account.address.lower() != OUR_WALLET.lower():
        raise RuntimeError(
            f"PRIVATE_KEY_NOX даёт адрес {account.address}, ожидался {OUR_WALLET} -- "
            "СТОП, не отправляю с неожиданного адреса (защита от неверно заданного секрета)."
        )

    nonce = v2.eth_get_transaction_count(OUR_WALLET, "pending")
    gas_price_wei = report["gas_price_wei"]
    gas_limit = int(report["gas_units_estimated"] * 1.2)  # запас 20% на оценку -- estimateGas не гарантирует точность на исполнение

    tx = {
        "chainId": report["chain_id"],
        "nonce": nonce,
        "to": v2.V2_FACTORY,
        "value": report["launch_fee_wei"],
        "gas": gas_limit,
        "gasPrice": int(gas_price_wei * 1.1),  # +10% запас, legacy-тип транзакции -- совместимо с любым EVM L2 без проверки поддержки EIP-1559
        "data": report["_calldata_hex"],
    }
    signed = Account.sign_transaction(tx, account.key)
    tx_hash = v2._rpc_call("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])  # type: ignore[attr-defined]

    print(f"[sc1_launcher] ОТПРАВЛЕНО: {tx_hash}")
    receipt = _wait_for_receipt(tx_hash)
    return _decode_receipt(receipt, report, gas_price_wei)


def _wait_for_receipt(tx_hash: str, timeout_s: int = 300, poll_s: int = 5) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        receipt = v2._rpc_call("eth_getTransactionReceipt", [tx_hash])
        if receipt is not None:
            return receipt
        time.sleep(poll_s)
    raise RuntimeError(f"Транзакция {tx_hash} не замайнилась за {timeout_s}с -- проверить вручную, НЕ повторять отправку автоматически.")


TOKEN_LAUNCHED_TOPIC0 = None  # инициализируется при первом использовании, см. _decode_receipt


def _decode_receipt(receipt: dict, report: dict, gas_price_wei_estimated: int) -> dict:
    global TOKEN_LAUNCHED_TOPIC0
    if TOKEN_LAUNCHED_TOPIC0 is None:
        from pons_v2_common import topic0
        TOKEN_LAUNCHED_TOPIC0 = topic0(
            "TokenLaunched(address,address,address,address,uint256,uint256)"
        )

    status = int(receipt["status"], 16)
    gas_used = int(receipt["gasUsed"], 16)
    effective_gas_price = int(receipt.get("effectiveGasPrice", hex(gas_price_wei_estimated)), 16)
    actual_gas_cost_eth = gas_used * effective_gas_price / 1e18
    actual_gas_cost_usd = actual_gas_cost_eth * report["eth_usd_price"]

    token_addr = None
    curve_addr = None
    if status == 1:
        for log in receipt.get("logs", []):
            if log["address"].lower() == v2.V2_FACTORY.lower() and log["topics"][0].lower() == TOKEN_LAUNCHED_TOPIC0.lower():
                token_addr = "0x" + log["topics"][1][-40:]
                curve_addr = "0x" + log["topics"][2][-40:]
                break

    return {
        "status": "success" if status == 1 else "REVERTED",
        "tx_hash": receipt["transactionHash"],
        "block_number": int(receipt["blockNumber"], 16),
        "token_address": token_addr,
        "curve_address": curve_addr,
        "actual_gas_used": gas_used,
        "actual_gas_price_wei": effective_gas_price,
        "actual_gas_cost_eth": actual_gas_cost_eth,
        "actual_gas_cost_usd": actual_gas_cost_usd,
    }


def append_registry_and_commit(entry: dict) -> None:
    # НАЙДЕНО 2026-09-02 (первый реальный запуск, run 33612440836):
    # эта функция печаталась ПОСЛЕ git commit -- при реальном сбое
    # коммита (в этом случае: git identity не была настроена ДО этого
    # шага в самой джобе, `git commit` упал с exit 128) запись о УЖЕ
    # ОТПРАВЛЕННОЙ И ЗАМАЙНЕННОЙ транзакции терялась целиком -- в логах
    # оставался только tx_hash из send_one(), реальный token_address/
    # curve_address/gas ушли в никуда вместе с крашем процесса. Печать
    # entry -- ПЕРВЫМ действием, до любых git-операций, которые могут
    # упасть -- при повторном сбое запись минимум видна в логах джобы и
    # восстановима вручную (см. analysis/sc1_recover_tx.py).
    print(f"[sc1_launcher] запись реестра для {entry['symbol']} (пишу ДО git-операций, на случай их сбоя):")
    print(json.dumps(entry, indent=2, default=str, ensure_ascii=False))

    existing = []
    if REGISTRY_PATH.exists():
        existing = json.loads(REGISTRY_PATH.read_text())
    existing.append(entry)
    REGISTRY_PATH.write_text(json.dumps(existing, indent=2, default=str) + "\n")

    # Идентичность git -- НЕ полагаемся на то, что вызывающий workflow
    # уже её настроил ДО запуска этого скрипта (баг run 33612440836:
    # run_sc1_launcher.yml настраивал identity только в ПОСЛЕДНЕМ шаге
    # Push, а не перед Launcher -- git commit падал с exit 128,
    # "please tell me who you are"). Настраиваем defensively здесь же,
    # идемпотентно (--local, безвредно перезаписать тем же значением).
    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], cwd=REPO_ROOT, check=True)
    subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], cwd=REPO_ROOT, check=True)

    subprocess.run(["git", "add", str(REGISTRY_PATH.relative_to(REPO_ROOT))], cwd=REPO_ROOT, check=True)
    subprocess.run(
        ["git", "commit", "-m", f"sc1_launches.json: запись о запуске {entry['symbol']} [automated]"],
        cwd=REPO_ROOT, check=True,
    )
    print(f"[sc1_launcher] закоммичена запись реестра для {entry['symbol']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", type=str, default=None, help="Через запятую, напр. NVX,QRT. По умолчанию -- все 20 из каталога.")
    ap.add_argument("--limit", type=int, default=1, help="Максимум РЕАЛЬНЫХ отправок за прогон (не применяется к dry-run). По умолчанию 1.")
    ap.add_argument("--confirm-mainnet", action="store_true", help="БЕЗ этого флага -- всегда dry-run, ничего не отправляется.")
    ap.add_argument("--launch-config-id", type=int, default=None, help="По умолчанию -- первый enabled конфиг.")
    ap.add_argument("--pair-token", type=str, default=None, help=f"По умолчанию -- кандидат WETH ({v2.CANDIDATE_WETH}), проверяется approvedPairTokens.")
    ap.add_argument("--gas-ceiling-usd", type=float, default=5.0, help="Потолок стоимости газа в USD на один запуск. Превышение -- жёсткий стоп, не понижение суммы.")
    ap.add_argument("--creator-tax-bps", type=int, default=0, help="Доля создателя сверх hook-комиссии, bps.")
    ap.add_argument("--buyback-enabled", action="store_true")
    ap.add_argument("--description", type=str, default=None, help="Одно описание на все выбранные токены в этом прогоне (не придумывается автоматически -- пусто, если не задано).")
    args = ap.parse_args()

    catalog = load_catalog()
    if not catalog:
        print(f"[sc1_launcher] {LOGOS_DIR} пуст или не соответствует шаблону -- нечего запускать.")
        return 1

    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else list(catalog.keys())
    unknown = [s for s in symbols if s not in catalog]
    if unknown:
        print(f"[sc1_launcher] неизвестные символы (нет PNG в {LOGOS_DIR}): {unknown}")
        return 1

    sha = _git_head_sha()
    mode = "REAL MAINNET SEND" if args.confirm_mainnet else "DRY-RUN (ничего не отправляется)"
    print(f"[sc1_launcher] режим: {mode}. Символы: {symbols}. Лимит реальных отправок: {args.limit}.")

    n_sent = 0
    exit_code = 0
    for symbol in symbols:
        print(f"\n{'=' * 70}\n[sc1_launcher] === {symbol} ===")
        report = prepare_one(symbol, catalog[symbol], args, sha)
        printable = {k: v for k, v in report.items() if not k.startswith("_")}
        print(json.dumps(printable, indent=2, default=str, ensure_ascii=False))

        if not args.confirm_mainnet:
            # ВАЖНО: пишем отчёт ВСЕГДА в dry-run режиме, включая случай
            # abort_reason (напр. превышен потолок газа) -- НАЙДЕНО
            # 2026-09-02 (run 33576972884): раньше запись отчёта была
            # ПОСЛЕ проверки abort_reason с `continue` внутри неё --
            # самый информативный случай (реальная оценка газа, упёршаяся
            # в потолок) вообще не попадал в файл и, соответственно, не
            # коммитился воркфлоу.
            dryrun_path = REPO_ROOT / "data" / "p3_guard_cache" / f"sc1_launcher_dryrun_{symbol}.json"
            dryrun_path.parent.mkdir(parents=True, exist_ok=True)
            dryrun_path.write_text(json.dumps(printable, indent=2, default=str, ensure_ascii=False))
            print(f"[sc1_launcher] {symbol}: dry-run отчёт записан: {dryrun_path}")

        if report.get("abort_reason"):
            print(f"[sc1_launcher] {symbol}: ОСТАНОВЛЕНО -- {report['abort_reason']}")
            exit_code = 1
            continue

        if not args.confirm_mainnet:
            print(f"[sc1_launcher] {symbol}: dry-run завершён, отправка НЕ выполнялась (нет --confirm-mainnet).")
            continue

        if n_sent >= args.limit:
            print(f"[sc1_launcher] {symbol}: лимит {args.limit} реальных отправок за прогон исчерпан -- пропускаю (запустите снова для остальных).")
            continue

        result = send_one(report, args)
        n_sent += 1
        entry = {
            "symbol": symbol,
            "token_address": result["token_address"],
            "pool_address": result["curve_address"],
            "pool_address_note": "V2 pre-graduation: адрес бондинг-кривой (curve), не AMM-пул -- реальный пул появляется только после градуации (createGraduatedPool)",
            "tx_hash": result["tx_hash"],
            "block_number": result["block_number"],
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "image_file": str(catalog[symbol].relative_to(REPO_ROOT)),
            "has_description": bool(args.description),
            "status": result["status"],
            "actual_gas_used": result["actual_gas_used"],
            "actual_gas_price_wei": result["actual_gas_price_wei"],
            "actual_gas_cost_eth": result["actual_gas_cost_eth"],
            "actual_gas_cost_usd": result["actual_gas_cost_usd"],
            "launch_fee_eth": report["launch_fee_eth"],
            "launch_fee_usd": report["launch_fee_usd"],
        }
        append_registry_and_commit(entry)
        if result["status"] != "success":
            print(f"[sc1_launcher] {symbol}: транзакция замайнилась, но REVERTED -- см. запись реестра, разбор вручную.")
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
