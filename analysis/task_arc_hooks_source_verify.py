#!/usr/bin/env python3
"""Владелец (2026-09-17), Шаг 0 подготовка: перед декодом битов разрешений
хука и симуляцией modifyLiquidity нужно ЗНАТЬ реальный битовый макет
флагов (Hooks.sol) и реальный механизм добавления ликвидности в V4
(PoolManager.sol -- unlock/modifyLiquidity) -- НЕ из памяти, а из
реального исходника Uniswap/v4-core на GitHub (сессия -- песочница
блокирует прямой WebFetch на github.com, у VPS есть реальный интернет).
Чисто HTTP GET исходников, без блокчейн-вызовов."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

REPO_ROOT_CANDIDATES = [Path("/home/bot/robinhood-chain-alpha"), Path(__file__).parent.parent]
URLS = {
    "hooks_sol": "https://raw.githubusercontent.com/Uniswap/v4-core/main/src/libraries/Hooks.sol",
    "pool_manager_sol": "https://raw.githubusercontent.com/Uniswap/v4-core/main/src/PoolManager.sol",
}


def find_repo_root() -> Path:
    for r in REPO_ROOT_CANDIDATES:
        if r.joinpath("data").exists():
            return r
    return REPO_ROOT_CANDIDATES[-1]


def main() -> None:
    root = find_repo_root()
    data_dir = root.joinpath("data")
    result: dict = {"probed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sources": {}}

    for name, url in URLS.items():
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            text = r.text
            entry = {"url": url, "http_status": r.status_code, "content_len": len(text)}
            if name == "hooks_sol":
                flags = re.findall(r"uint160\s+internal\s+constant\s+(\w+_FLAG)\s*=\s*(1\s*<<\s*\d+|0x[0-9a-fA-F]+);", text)
                entry["flag_constants_raw"] = flags
                mask = re.search(r"ALL_HOOK_MASK\s*=\s*([^;]+);", text)
                entry["all_hook_mask_raw"] = mask.group(1).strip() if mask else None
            if name == "pool_manager_sol":
                unlock_fn = re.search(r"function unlock\([^)]*\)[^{]*\{", text)
                entry["unlock_signature"] = unlock_fn.group(0) if unlock_fn else None
                modify_liq_fn = re.search(r"function modifyLiquidity\([^)]*\)[^{]*\{", text)
                entry["modify_liquidity_signature"] = modify_liq_fn.group(0) if modify_liq_fn else None
                # ищем модификатор/require про то, что вызывать можно только из unlock-колбэка
                only_when_unlocked = re.findall(r"(onlyWhenUnlocked|NotUnlocked|require\(.*unlock.*\))", text, re.IGNORECASE)
                entry["unlock_gating_hints"] = only_when_unlocked[:10]
            result["sources"][name] = entry
        except Exception as exc:  # noqa: BLE001
            result["sources"][name] = {"exception": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    data_dir.joinpath("task_arc_hooks_source_verify_result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str)
    )


if __name__ == "__main__":
    main()
