#!/usr/bin/env python3
"""Владелец (2026-09-17): два несходящихся набора лидеров fomo -- три
адреса из передачи контекста (SolSwizzle/ether_monk/remusofmars) и шесть
адресов с fomo.family, присланные владельцем 06.09 (unipcs/ogle/avast/
frogman/DumbCrayonEater/vee), про которые УЖЕ установлено (см.
fomo_forensics_wallets_account_abstraction_check_result.json): все 6 --
смарт-кошельки-клоны из общей фабрики (одинаковая длина байткода 48 hex
символов), не EOA.

Задача -- бесплатно, ДО любых трат Dune, проверить: пересекаются ли два
множества, это одни люди в разных обозначениях или разные группы.

Метод (только точечные бесплатные RPC-вызовы, без сканов диапазонов):
  1. Буквальное совпадение адресов (тривиально, но явно).
  2. eth_getCode на все 9 -- реальный байткод, не только длина как в
     прошлый раз (тогда сохранялась только длина). Если байткод
     байт-в-байт совпадает у каких-то адресов -- сильный бесплатный
     сигнал общей фабрики/шаблона.
  3. eth_getStorageAt на двух стандартных proxy-слотах (EIP-1967
     implementation и beacon) для всех 9 -- если это тот же паттерн
     прокси, что использует терминал fomo.family, слот покажет общий
     implementation/beacon-адрес напрямую, без необходимости
     реверс-инжинерить нестандартный минимальный клон руками.
  4. eth_getTransactionCount (собственный nonce контракта) на все 9 --
     дёшево, показывает, инициировал ли адрес исходящие CREATE
     когда-либо (обычно 0 для чистого AA-кошелька без своих деплоев)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alchemy_fallback import _rpc_call  # noqa: E402

OUT_PATH = Path("data/p3_guard_cache/fomo_forensics_new_leaders_crosscheck_result.json")

KNOWN_6_FOMO_FAMILY = {
    "unipcs": "0x0a6EBEd0155EDB4b21D92AD02897A626CD90119E",
    "ogle": "0x1Bcc5f67CD17e13770F199fA03bC043b0cde1143",
    "avast": "0xcc0C581613DFd4ACe7c8686668427236f8BD5cC5",
    "frogman": "0x14AA2A71dbb5eF87b81F92205E2699AA4aa65794",
    "DumbCrayonEater": "0x8f62a08537cede87d511aca6436274ab4ca080a3",
    "vee": "0xa0670863bd5cd0d60022bab2eed78e81e1a06bce",
}

NEW_3_FROM_HANDOFF = {
    "SolSwizzle": "0x44fbe0006661d6d17188f1f6d42b32b5577179f7",
    "ether_monk": "0x2408ce75d217e3a70d6ca370c78c1b34d706f5a0",
    "remusofmars": "0x8ab8c0843d9738885d6273dfe3de86c56eea364c",
}

# Стандартные EIP-1967 слоты (bytes32(uint256(keccak256(<name>)) - 1)) --
# ОБЩЕИЗВЕСТНЫЕ КОНСТАНТЫ стандарта, не предположение конкретно про этот
# терминал; проверяем реально, не гадаем, что там лежит.
EIP1967_IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bb"
EIP1967_BEACON_SLOT = "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d5"


def strip_leading_zero_addr(word_hex: str) -> str:
    """32-байтное слово -> адрес, если младшие 20 байт ненулевые (иначе
    честно возвращаем None -- слот реально пуст, не выдумываем адрес)."""
    h = word_hex[2:] if word_hex.startswith("0x") else word_hex
    h = h.rjust(64, "0")
    tail = h[-40:]
    if int(tail, 16) == 0:
        return None
    return "0x" + tail


def run() -> int:
    out: dict = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "known_6_fomo_family": KNOWN_6_FOMO_FAMILY,
        "new_3_from_handoff": NEW_3_FROM_HANDOFF,
    }

    all_addrs = {**KNOWN_6_FOMO_FAMILY, **NEW_3_FROM_HANDOFF}

    # === 1. Буквальное пересечение множеств адресов ===
    lower_known = {a.lower() for a in KNOWN_6_FOMO_FAMILY.values()}
    lower_new = {a.lower() for a in NEW_3_FROM_HANDOFF.values()}
    literal_overlap = sorted(lower_known & lower_new)
    out["literal_address_overlap"] = literal_overlap
    out["n_literal_overlap"] = len(literal_overlap)

    # === 2, 3, 4: point RPC на каждый из 9 ===
    rows: dict = {}
    for name, addr in all_addrs.items():
        code = _rpc_call("eth_getCode", [addr, "latest"]) or "0x"
        impl_slot_word = _rpc_call("eth_getStorageAt", [addr, EIP1967_IMPLEMENTATION_SLOT, "latest"]) or "0x0"
        beacon_slot_word = _rpc_call("eth_getStorageAt", [addr, EIP1967_BEACON_SLOT, "latest"]) or "0x0"
        nonce_hex = _rpc_call("eth_getTransactionCount", [addr, "latest"]) or "0x0"
        row = {
            "address": addr,
            "group": "known_6_fomo_family" if name in KNOWN_6_FOMO_FAMILY else "new_3_from_handoff",
            "bytecode_hex": code,
            "bytecode_len_hex_chars": len(code),
            "has_bytecode": code not in ("0x", "0x0", None),
            "eip1967_implementation_slot_raw": impl_slot_word,
            "eip1967_implementation_addr": strip_leading_zero_addr(impl_slot_word),
            "eip1967_beacon_slot_raw": beacon_slot_word,
            "eip1967_beacon_addr": strip_leading_zero_addr(beacon_slot_word),
            "own_nonce": int(nonce_hex, 16) if isinstance(nonce_hex, str) else None,
        }
        print(f"[crosscheck] {name} ({addr}): bytecode_len={row['bytecode_len_hex_chars']}, "
              f"impl_slot={row['eip1967_implementation_addr']}, beacon_slot={row['eip1967_beacon_addr']}, "
              f"nonce={row['own_nonce']}")
        rows[name] = row

    out["rows"] = rows

    # === Реальные признаки общего происхождения (не гадаем, считаем по факту) ===
    bytecode_groups: dict[str, list[str]] = {}
    for name, row in rows.items():
        bytecode_groups.setdefault(row["bytecode_hex"], []).append(name)
    out["identical_bytecode_groups"] = {
        code: names for code, names in bytecode_groups.items() if len(names) > 1
    }

    impl_addrs = {name: row["eip1967_implementation_addr"] for name, row in rows.items()
                  if row["eip1967_implementation_addr"]}
    impl_groups: dict[str, list[str]] = {}
    for name, impl in impl_addrs.items():
        impl_groups.setdefault(impl, []).append(name)
    out["shared_eip1967_implementation_groups"] = {
        impl: names for impl, names in impl_groups.items() if len(names) > 1
    }

    cross_group_bytecode_match = any(
        any(n in KNOWN_6_FOMO_FAMILY for n in names) and any(n in NEW_3_FROM_HANDOFF for n in names)
        for names in bytecode_groups.values() if len(names) > 1
    )
    cross_group_impl_match = any(
        any(n in KNOWN_6_FOMO_FAMILY for n in names) and any(n in NEW_3_FROM_HANDOFF for n in names)
        for names in impl_groups.values() if len(names) > 1
    )
    out["cross_group_evidence_of_same_infra"] = {
        "identical_bytecode_across_groups": cross_group_bytecode_match,
        "shared_eip1967_implementation_across_groups": cross_group_impl_match,
    }

    if literal_overlap:
        conclusion = f"списки пересекаются буквально в {len(literal_overlap)} адресах"
    elif cross_group_bytecode_match or cross_group_impl_match:
        conclusion = ("буквального пересечения адресов нет, но найден общий признак инфраструктуры "
                      "(байткод и/или EIP-1967 implementation/beacon совпадает между группами) -- "
                      "похоже на ОДНУ платформу/фабрику под разными кошельками, не обязательно одних людей")
    else:
        conclusion = ("буквального пересечения нет, общих признаков инфраструктуры (байткод, "
                      "EIP-1967 слоты) между группами не найдено на этом бесплатном шаге -- "
                      "по имеющимся данным это ВЫГЛЯДИТ как разные группы, но НЕ доказано "
                      "окончательно (не проверялась общая история финансирования/деплоя)")
    out["conclusion"] = conclusion
    out["honest_caveat"] = (
        "Этот шаг НЕ проверяет общего деплоера/фандинг-адрес (нужен был бы поиск создающей "
        "транзакции каждого контракта -- trace/creation lookup, не гарантированно бесплатный "
        "на этом RPC) и не проверяет прямые Transfer-переводы между двумя группами (нужен был бы "
        "eth_getLogs-скан диапазона блоков -- на этой цепи, по прежнему опыту сессии, дорого при "
        "полном диапазоне). Сделаны только точечные бесплатные RPC-вызовы (eth_getCode, "
        "eth_getStorageAt на двух стандартных слотах, eth_getTransactionCount) -- 9 адресов x "
        "4 вызова = 36 вызовов, 0 Dune-кредитов."
    )

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\n[crosscheck] {conclusion}")
    print(f"[crosscheck] результат записан в {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
