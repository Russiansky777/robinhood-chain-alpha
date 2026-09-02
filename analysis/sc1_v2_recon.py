#!/usr/bin/env python3
"""Разведка живых ончейн-параметров `PonsV2LaunchFactory` (только
чтение, `eth_call`) -- задача 2 дозапроса владельца (2026-09-02).

Контракт-статические факты (адрес, сигнатуры функций, структуры,
require-проверки) уже разобраны по первоисточнику (GitHub) и записаны
в `docs/SC1_LAUNCHER.md` -- ЭТОТ скрипт заполняет только то, что нельзя
узнать из исходников: текущие значения ончейн-состояния (launchFee,
launchEnabled, canLaunch для НАШЕГО кошелька, launchConfig'и,
approvedPairTokens). Публичный RPC -- эта интерактивная сессия egress
к нему не имеет (см. docs/PROJECT_STATE.md) -- запускается на GH
Actions runner'е (см. .github/workflows/run_sc1_v2_recon.yml).

Наружу -- только агрегаты/факты состояния контракта (публичная
ончейн-информация в любом случае, не приватные данные каких-либо
пользователей).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pons_v2_common as v2  # noqa: E402

CACHE_DIR = Path("data/p3_guard_cache")
OUT_PATH = CACHE_DIR / "sc1_v2_recon_result.json"
LAUNCHER_DOC = Path("docs/SC1_LAUNCHER.md")

OUR_WALLET = "0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75"


def run() -> int:
    print(f"[sc1_v2_recon] Factory = {v2.V2_FACTORY}")

    chain_id = v2.eth_chain_id()
    print(f"[sc1_v2_recon] chainId = {chain_id} ({'совпадает с ожидаемым 4663' if chain_id == 4663 else 'НЕ 4663 -- ОСТАНОВКА'})")
    if chain_id != 4663:
        print("[sc1_v2_recon] СТОП: chainId с публичного RPC не совпадает с ожидаемым Robinhood Chain (4663) -- не продолжаю вслепую.")
        return 1

    fee_wei = v2.launch_fee()
    enabled = v2.launch_enabled()
    can_we = v2.can_launch(OUR_WALLET)
    whitelisted = v2.whitelisted_launchers(OUR_WALLET)
    max_tax_bps = v2.max_creator_tax_bps()
    n_configs = v2.launch_config_count()
    weth_approved = v2.approved_pair_tokens(v2.CANDIDATE_WETH)

    print(f"[sc1_v2_recon] launchFee = {fee_wei} wei ({fee_wei / 1e18:.8f} ETH)")
    print(f"[sc1_v2_recon] launchEnabled (публичные запуски открыты) = {enabled}")
    print(f"[sc1_v2_recon] whitelistedLaunchers[{OUR_WALLET}] = {whitelisted}")
    print(f"[sc1_v2_recon] canLaunch({OUR_WALLET}) = {can_we} "
          f"({'МОЖЕМ запускать' if can_we else 'НЕ МОЖЕМ -- revert NotWhitelisted() при попытке'})")
    print(f"[sc1_v2_recon] maxCreatorTaxBps = {max_tax_bps}")
    print(f"[sc1_v2_recon] launchConfigCount = {n_configs}")
    print(f"[sc1_v2_recon] approvedPairTokens[{v2.CANDIDATE_WETH}] (кандидат WETH, из V1) = {weth_approved}")

    configs = []
    for i in range(n_configs):
        cfg = v2.get_launch_config(i)
        configs.append({
            "id": i, "supply": cfg.supply, "curve_fee_bps": cfg.curve_fee_bps,
            "phantom_quote": cfg.phantom_quote, "graduation_threshold": cfg.graduation_threshold,
            "pool_fee": cfg.pool_fee, "tick_spacing": cfg.tick_spacing, "enabled": cfg.enabled,
        })
        print(f"[sc1_v2_recon]   config[{i}]: supply={cfg.supply} curveFeeBps={cfg.curve_fee_bps} "
              f"phantomQuote={cfg.phantom_quote} graduationThreshold={cfg.graduation_threshold} "
              f"poolFee={cfg.pool_fee} tickSpacing={cfg.tick_spacing} enabled={cfg.enabled}")

    result = {
        "factory": v2.V2_FACTORY,
        "chain_id": chain_id,
        "our_wallet": OUR_WALLET,
        "launch_fee_wei": fee_wei,
        "launch_fee_eth": fee_wei / 1e18,
        "launch_enabled": enabled,
        "whitelisted": whitelisted,
        "can_we_launch": can_we,
        "max_creator_tax_bps": max_tax_bps,
        "launch_config_count": n_configs,
        "candidate_weth": v2.CANDIDATE_WETH,
        "candidate_weth_approved_as_pair_token": weth_approved,
        "launch_configs": configs,
    }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str))
    print(f"\n[sc1_v2_recon] записано {OUT_PATH}")

    _update_launcher_doc(result)
    return 0


def _update_launcher_doc(result: dict) -> None:
    if not LAUNCHER_DOC.exists():
        print(f"[sc1_v2_recon] {LAUNCHER_DOC} не найден -- пропускаю обновление документа.")
        return
    text = LAUNCHER_DOC.read_text()
    marker = "<!-- SC1_V2_RECON_RESULT -->"
    if marker not in text:
        print(f"[sc1_v2_recon] маркер {marker!r} не найден в {LAUNCHER_DOC} -- пропускаю (не дублирую вслепую).")
        return

    configs_table = "\n".join(
        f"| {c['id']} | {c['supply']} | {c['curve_fee_bps']} | {c['phantom_quote']} | "
        f"{c['graduation_threshold']} | {c['pool_fee']} | {c['tick_spacing']} | {c['enabled']} |"
        for c in result["launch_configs"]
    )
    section = f"""{marker}

## Результат живого прогона (`analysis/sc1_v2_recon.py`, GH Actions, публичный RPC)

Подтверждено вызовами `eth_call` к `{result['factory']}` (chainId={result['chain_id']}):

| параметр | значение |
|---|---|
| `launchFee()` | **{result['launch_fee_wei']} wei = {result['launch_fee_eth']:.8f} ETH** |
| `launchEnabled()` | **{result['launch_enabled']}** |
| `whitelistedLaunchers({result['our_wallet']})` | {result['whitelisted']} |
| `canLaunch({result['our_wallet']})` | **{result['can_we_launch']}** |
| `maxCreatorTaxBps()` | {result['max_creator_tax_bps']} |
| `launchConfigCount()` | {result['launch_config_count']} |
| `approvedPairTokens({result['candidate_weth']})` (кандидат WETH, из V1) | **{result['candidate_weth_approved_as_pair_token']}** |

### launchConfig'и

| id | supply | curveFeeBps | phantomQuote | graduationThreshold | poolFee | tickSpacing | enabled |
|---|---|---|---|---|---|---|---|
{configs_table}

Артефакт: `data/p3_guard_cache/sc1_v2_recon_result.json`.
"""
    LAUNCHER_DOC.write_text(text.replace(marker, section))
    print(f"[sc1_v2_recon] {LAUNCHER_DOC} обновлён.")


if __name__ == "__main__":
    raise SystemExit(run())
