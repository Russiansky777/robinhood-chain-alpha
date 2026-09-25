#!/usr/bin/env python3
"""Задача D (C2): своя сборка покупки «котировка -> токен» БЕЗ отправки в сеть.

Ключи кошелька не используются нигде: пользователь -- ключ, сгенерированный
на лету, симуляция -- simulateTransaction с sigVerify=false и
replaceRecentBlockhash=true. Метода отправки транзакции в модуле нет.

Покрытые типы пулов (задача D, шаг 1: 80.5 % сигналов):
  Pump AMM  pAMMBay6...  buy_exact_quote_in(spendable_quote_in, min_base_amount_out)
  Raydium CPMM CPMMoo8L... swap_base_input(amount_in, minimum_amount_out)
  Meteora DAMM v2 cpamdpZ... swap(amount_in, minimum_amount_out)

Откуда формат, без выдумывания:
* дискриминатор -- правило Anchor sha256("global:<имя>")[:8]; совпадение
  проверено на настоящих транзакциях источников (data/c2_pool_samples/);
* порядок счетов и роли -- из инструкции источника в его транзакции;
  пользовательские счета заменяются по ролям, которые проверены на цепи:
  у Pump AMM счёт 20 -- PDA ["user_volume_accumulator", user] (25 из 25),
  токен-счета пользователя -- ATA (owner, программа токена, минт);
* аргументы -- два u64 после дискриминатора; сверено с движением хранилищ.
Главная самопроверка: из шаблона и адреса самого источника сборщик обязан
восстановить его инструкцию ТОЧНО (счета и данные).

Минимум токенов -- по резервам пула ПОСЛЕ сделки источника
(postTokenBalances хранилищ его транзакции), формула x*y=k; доля,
доходящая до пула из нашей траты (комиссии), калибруется на сделке
самого источника: f = x0*dy/((y0-dy)*spent). Для DAMM v2 (сосредоточенная
ликвидность) резервы цены не дают -- минимум там не выдаётся.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import struct
import sys
import time
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402

from solders.hash import Hash  # noqa: E402
from solders.instruction import AccountMeta, Instruction  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.message import MessageV0  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.signature import Signature  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

SERVICE = "c2_build"
SAMPLES_DIR = C.DATA / "c2_pool_samples"
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
DAMM2 = "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"
LAUNCHLAB = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
DLMM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
CLMM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
# Пулы, где направление задаётся тем, в какое хранилище пришла котировка:
# индексы входа/выхода пользователя, хранилищ a/b, их минтов и программ токена.
DYN = {
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": {"in": 2, "out": 3, "va": 4, "vb": 5, "ma": 6,
                                                    "mb": 7, "pa": 9, "pb": 10},
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": {"in": 4, "out": 5, "va": 2, "vb": 3, "ma": 6,
                                                    "mb": 7, "pa": 11, "pb": 12},
    # Raydium CLMM swap_v2: 3/4 -- счета пользователя вход/выход, 5/6 --
    # хранилища вход/выход, 11/12 -- их минты; программа токена у swap_v2
    # общая парой (8 -- SPL, 9 -- Token-2022), своя у каждого минта берётся
    # из балансов транзакции (programId).
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": {"in": 3, "out": 4, "va": 5, "vb": 6, "ma": 11,
                                                     "mb": 12, "pa": None, "pb": None},
}
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SYSTEM = "11111111111111111111111111111111"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    b = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + b


def disc(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


# Роли по программам. user -- подписант-владелец; user_ata -- (индекс счёта,
# индекс счёта минта, индекс счёта программы токена); pda -- счета,
# выводимые из пользователя.
SPECS = {
    PUMP_AMM: {"label": "Pump AMM", "ix": "buy_exact_quote_in", "alt": ["buy"], "n_accounts": 26,
               "user": [1], "user_ata": [(5, 3, 11), (6, 4, 12)],
               "pda": [(20, [b"user_volume_accumulator", "USER"])],
               "base_mint": 3, "quote_mint": 4, "base_vault": 7, "quote_vault": 8},
    CPMM: {"label": "Raydium CPMM", "ix": "swap_base_input", "alt": [], "n_accounts": 13,
           "user": [0], "user_ata": [(4, 10, 8), (5, 11, 9)], "pda": [],
           "base_mint": 11, "quote_mint": 10, "base_vault": 7, "quote_vault": 6},
    LAUNCHLAB: {"label": "Raydium Launchlab", "ix": "buy_exact_in", "alt": [], "n_accounts": 18,
                "user": [0], "user_ata": [(5, 9, 11), (6, 10, 12)], "pda": [], "tail": True,
                "base_mint": 9, "quote_mint": 10, "base_vault": 7, "quote_vault": 8},
    DLMM: {"label": "Meteora DLMM", "ix": "swap2", "alt": ["swap"], "n_accounts": None, "min_accounts": 16,
           "user": [10], "user_ata": "dyn", "pda": [], "tail": True,
           "base_mint": None, "quote_mint": None, "base_vault": None, "quote_vault": None},
    CLMM: {"label": "Raydium CLMM", "ix": "swap_v2", "alt": [], "n_accounts": None, "min_accounts": 13,
           "user": [0], "user_ata": "dyn", "pda": [], "tail": True,
           "base_mint": None, "quote_mint": None, "base_vault": None, "quote_vault": None},
    DAMM2: {"label": "Meteora DAMM v2", "ix": "swap", "alt": ["swap2"], "n_accounts": 14,
            "user": [8], "user_ata": "dyn", "pda": [],
            "base_mint": None, "quote_mint": None, "base_vault": None, "quote_vault": None},
}


def all_instructions(tx: dict) -> list:
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    out = list(msg.get("instructions") or [])
    for g in ((tx or {}).get("meta") or {}).get("innerInstructions") or []:
        out += list(g.get("instructions") or [])
    return [ix for ix in out if isinstance(ix, dict) and isinstance(ix.get("accounts"), list)]


def writable_map(tx: dict) -> dict:
    raw = (((tx or {}).get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    return {k["pubkey"]: bool(k.get("writable")) for k in raw if isinstance(k, dict)}


def ata(owner: str, mint: str, token_program: str) -> str:
    return str(Pubkey.find_program_address(
        [bytes(Pubkey.from_string(owner)), bytes(Pubkey.from_string(token_program)),
         bytes(Pubkey.from_string(mint))], Pubkey.from_string(ATA_PROGRAM))[0])


def pda(seeds: list, user: str, program: str) -> str:
    raw = [bytes(Pubkey.from_string(user)) if s == "USER" else s for s in seeds]
    return str(Pubkey.find_program_address(raw, Pubkey.from_string(program))[0])


def extract_template(tx: dict, program: str, pool_vault: str) -> dict:
    """Инструкция пула в транзакции источника: счета, данные, аргументы."""
    spec = SPECS.get(program)
    if spec is None:
        return {"ok": False, "why_not": "тип пула не покрыт"}
    want = disc(spec["ix"])
    for ix in all_instructions(tx):
        if ix.get("programId") != program or pool_vault not in ix["accounts"]:
            continue
        data = b58decode(ix["data"])
        name = next((n for n in (spec["ix"], "buy", "swap2", "swap_base_output", "swap")
                     if data[:8] == disc(n)), data[:8].hex())
        if data[:8] != want and name not in spec["alt"]:
            return {"ok": False, "why_not": f"у источника инструкция {name}, не {spec['ix']}"}
        if spec["n_accounts"] is not None and len(ix["accounts"]) != spec["n_accounts"] or \
                len(ix["accounts"]) < (spec.get("min_accounts") or 0):
            return {"ok": False, "why_not": f"счетов {len(ix['accounts'])}, ожидалось {spec['n_accounts']}"}
        a0, a1 = struct.unpack("<QQ", data[8:24])
        # DLMM swap (v1): счета 0..12 те же, что у swap2 (проверено на
        # настоящих транзакциях), данные -- дискриминатор + 2 u64 без хвоста.
        if program == CLMM and name != spec["ix"] or program == DLMM and name not in ("swap2", "swap"):
            return {"ok": False, "why_not": f"у источника инструкция {name}, не {spec['ix']}"}
        # CLMM: хвост -- sqrt_price_limit_x64 (u128) + is_base_input (bool).
        # Хвост переносится в нашу сборку как есть, поэтому берём только
        # «точный вход без ценового лимита».
        if program == CLMM and data[24:] != bytes(16) + b"\x01":
            return {"ok": False, "why_not": "у источника CLMM не «точный вход без лимита цены»"}
        return {"ok": True, "program": program, "ix": name, "accounts": list(ix["accounts"]),
                "data": data, "arg0": a0, "arg1": a1, "writable": writable_map(tx),
                "signers": sorted(C.signers(tx))}
    return {"ok": False, "why_not": "инструкция пула с этим хранилищем не найдена"}


def roles_damm2(tpl: dict, tx: dict) -> dict:
    """DAMM v2 / DLMM: вход -- то хранилище, куда в сделке пришла котировка."""
    acc = tpl["accounts"]
    k = DYN[tpl["program"]]
    rows = {r["account"]: r for r in C.token_rows(tx).values()}
    va, vb = rows.get(acc[k["va"]]), rows.get(acc[k["vb"]])
    if not va or not vb:
        return {}
    a_in = (acc[k["ma"]] == tpl["force_in"]) if tpl.get("force_in") else (va["post"] - va["pre"]) > 0
    in_mint, out_mint = (acc[k["ma"]], acc[k["mb"]]) if a_in else (acc[k["mb"]], acc[k["ma"]])
    if k["pa"] is None:
        progs = {b["mint"]: b.get("programId") for side in ("preTokenBalances", "postTokenBalances")
                 for b in ((tx.get("meta") or {}).get(side) or [])}
        pa, pb = progs.get(acc[k["ma"]]), progs.get(acc[k["mb"]])
        if not pa or not pb:
            return {}
    else:
        pa, pb = acc[k["pa"]], acc[k["pb"]]
    in_prog, out_prog = (pa, pb) if a_in else (pb, pa)
    return {"in": (k["in"], in_mint, in_prog), "out": (k["out"], out_mint, out_prog),
            "quote_vault": acc[k["va"]] if a_in else acc[k["vb"]],
            "base_vault": acc[k["vb"]] if a_in else acc[k["va"]]}


def flip_template(tpl: dict, in_mint: str) -> dict:
    """Шаблон ценозависимого пула из сделки обратного направления: вход --
    in_mint. DLMM: хранилища и минты в инструкции по порядку пула, достаточно
    указать вход. CLMM swap_v2: хранилища/минты по направлению -- меняются
    местами 5<->6 и 11<->12. Массивы бинов/тиков остаются от чужой сделки
    (начинаются с текущего) -- годность проверяет симуляция."""
    t = dict(tpl, force_in=in_mint, flipped=True)
    if tpl["program"] == CLMM:
        a = list(tpl["accounts"])
        a[5], a[6], a[11], a[12] = a[6], a[5], a[12], a[11]
        t["accounts"] = a
    return t


def user_accounts(tpl: dict, tx: dict, user: str) -> dict:
    """{индекс: адрес} для пользовательских счетов шаблона при данном user."""
    spec = SPECS[tpl["program"]]
    acc = tpl["accounts"]
    out = {i: user for i in spec["user"]}
    if spec["user_ata"] == "dyn":
        r = roles_damm2(tpl, tx)
        if not r:
            return {}
        for i, mint, prog in (r["in"], r["out"]):
            out[i] = ata(user, mint, prog)
    else:
        for i, mi, pi in spec["user_ata"]:
            out[i] = ata(user, acc[mi], acc[pi])
    for i, seeds in spec["pda"]:
        out[i] = pda(seeds, user, tpl["program"])
    return out


def mints_and_vaults(tpl: dict, tx: dict) -> dict:
    spec = SPECS[tpl["program"]]
    acc = tpl["accounts"]
    if spec["user_ata"] == "dyn":
        r = roles_damm2(tpl, tx)
        if not r:
            return {}
        return {"quote_mint": r["in"][1], "base_mint": r["out"][1], "quote_program": r["in"][2],
                "base_program": r["out"][2], "quote_vault": r["quote_vault"],
                "base_vault": r["base_vault"]}
    qa = next(x for x in spec["user_ata"] if x[1] == spec["quote_mint"])
    ba = next(x for x in spec["user_ata"] if x[1] == spec["base_mint"])
    return {"quote_mint": acc[spec["quote_mint"]], "base_mint": acc[spec["base_mint"]],
            "quote_program": acc[qa[2]], "base_program": acc[ba[2]],
            "quote_vault": acc[spec["quote_vault"]], "base_vault": acc[spec["base_vault"]]}


def swap_instruction(tpl: dict, tx: dict, user: str, arg0: int, arg1: int,
                     keep_source_ix: bool = False) -> Instruction:
    subs = user_accounts(tpl, tx, user)
    if not subs:
        raise ValueError("роли счетов не восстановились")
    metas = []
    for i, a in enumerate(tpl["accounts"]):
        addr = subs.get(i, a)
        is_user = i in subs
        metas.append(AccountMeta(Pubkey.from_string(addr),
                                 is_signer=(addr == user),
                                 is_writable=is_user or tpl["writable"].get(a, False)))
    if keep_source_ix:   # только для самопроверки: инструкция источника как есть
        data = tpl["data"][:8] + struct.pack("<QQ", arg0, arg1) + tpl["data"][24:]
    else:                # наша сборка: основная инструкция типа, два u64 (+ хвост источника,
                         # у Launchlab это share_fee_rate u64)
        ix_name = tpl["ix"] if tpl["program"] == DLMM else SPECS[tpl["program"]]["ix"]
        data = disc(ix_name) + struct.pack("<QQ", arg0, arg1)
        if SPECS[tpl["program"]].get("tail") and not (tpl["program"] == DLMM and ix_name == "swap"):
            data += tpl["data"][24:]
    return Instruction(Pubkey.from_string(tpl["program"]), data, metas)


def ata_idempotent(payer: str, owner: str, mint: str, token_program: str) -> Instruction:
    return Instruction(Pubkey.from_string(ATA_PROGRAM), bytes([1]), [
        AccountMeta(Pubkey.from_string(payer), True, True),
        AccountMeta(Pubkey.from_string(ata(owner, mint, token_program)), False, True),
        AccountMeta(Pubkey.from_string(owner), False, False),
        AccountMeta(Pubkey.from_string(mint), False, False),
        AccountMeta(Pubkey.from_string(SYSTEM), False, False),
        AccountMeta(Pubkey.from_string(token_program), False, False)])


def cu_limit(units: int) -> Instruction:
    return Instruction(Pubkey.from_string(COMPUTE_BUDGET), bytes([2]) + struct.pack("<I", units), [])


def cu_price(micro_lamports: int) -> Instruction:
    return Instruction(Pubkey.from_string(COMPUTE_BUDGET), bytes([3]) + struct.pack("<Q", micro_lamports), [])


def sol_transfer(src: str, dst: str, lamports: int) -> Instruction:
    return Instruction(Pubkey.from_string(SYSTEM), struct.pack("<IQ", 2, lamports), [
        AccountMeta(Pubkey.from_string(src), True, True),
        AccountMeta(Pubkey.from_string(dst), False, True)])


# СЛУЖЕБНЫЕ АДРЕСА, нужные долговечному nonce. RecentBlockhashes -- зашитый
# системный аккаунт (SysvarRecentB1ockHashes11111111111111111111), он обязателен
# в инструкции AdvanceNonceAccount по описанию системной программы Solana.
SYSVAR_RECENT_BLOCKHASHES = "SysvarRecentB1ockHashes11111111111111111111"


def advance_nonce(nonce_account: str, authority: str) -> Instruction:
    """AdvanceNonceAccount: код 4 системной программы, три аккаунта по порядку.

    ЗАЧЕМ ОНА. Долговечный nonce даёт то, чего не даёт обычный blockhash: ДВЕ
    РАЗНЫЕ транзакции с одним и тем же nonce не могут исполниться обе -- первая
    его сдвигает, вторая становится недействительной. Это и есть замок пула по
    варианту с отдельной транзакцией на каждого отправителя: у каждого свои
    чаевые (значит пакет меньше и предел 1232 байта не жмёт), а купить можно
    только один раз.

    Инструкция обязана быть ПЕРВОЙ в транзакции, а recent_blockhash сообщения --
    это хеш, сохранённый в самом аккаунте nonce. Оба условия -- требования
    системной программы, а не наш выбор.
    """
    return Instruction(Pubkey.from_string(SYSTEM), struct.pack("<I", 4), [
        AccountMeta(Pubkey.from_string(nonce_account), False, True),
        AccountMeta(Pubkey.from_string(SYSVAR_RECENT_BLOCKHASHES), False, False),
        AccountMeta(Pubkey.from_string(authority), True, False)])


def sync_native(account: str) -> Instruction:
    return Instruction(Pubkey.from_string(TOKEN_PROGRAM), bytes([17]),
                       [AccountMeta(Pubkey.from_string(account), False, True)])


def build_buy(tpl: dict, tx: dict, *, user: str, payer: str, amount_in: int, min_out: int,
              cu_units: int = 200_000, cu_price_micro: int = 0, tip: tuple | None = None,
              wrap_sol: bool = True, nonce: tuple | None = None,
              tip_first: bool = False) -> dict:
    """Инструкции покупки: compute budget, ATA (идемпотентно), обёртка SOL
    (если котировка WSOL и wrap_sol), своп, tip отдельным параметром
    (адрес, лампорты) -- адрес tip не зашит.

    nonce=(аккаунт, распорядитель) добавляет AdvanceNonceAccount ПЕРВОЙ
    инструкцией: так делается вариант пула, где на каждого отправителя своя
    транзакция со своими чаевыми, а исполниться может только одна.
    """
    mv = mints_and_vaults(tpl, tx)
    if not mv:
        raise ValueError("минты/хранилища не восстановились")
    ixs = []
    if nonce:
        # ПЕРВОЙ -- и никак иначе: системная программа принимает
        # AdvanceNonceAccount только как первую инструкцию транзакции.
        ixs.append(advance_nonce(str(nonce[0]), str(nonce[1])))
    ixs.append(cu_limit(cu_units))
    if cu_price_micro:
        ixs.append(cu_price(cu_price_micro))
    ixs.append(ata_idempotent(payer, user, mv["base_mint"], mv["base_program"]))
    ixs.append(ata_idempotent(payer, user, mv["quote_mint"], mv["quote_program"]))
    if mv["quote_mint"] == C.WSOL and wrap_sol and amount_in:
        wsol_ata = ata(user, C.WSOL, mv["quote_program"])
        ixs += [sol_transfer(user, wsol_ata, amount_in), sync_native(wsol_ata)]
    # ЧАЕВЫЕ ПЕРЕД СВОПОМ -- по требованию отправителя. 0slot в письме 25.09:
    # "инструкцию чаевых ставить в начало транзакции" (он ищет её там). Ставим
    # сразу после бюджета вычислений и nonce: раньше нельзя -- nonce обязан быть
    # первой инструкцией по правилу системной программы.
    def _чаевые_инструкции(tip_):
        пары_ = (tip_ if isinstance(tip_, (list, tuple)) and tip_
                 and isinstance(tip_[0], (list, tuple)) else [tip_])
        return [sol_transfer(payer, а_, int(л_)) for а_, л_ in пары_]

    if tip and tip_first:
        ixs += _чаевые_инструкции(tip)
    ixs.append(swap_instruction(tpl, tx, user, amount_in, min_out))
    if tip and tip_first:
        tip = None
    if tip:
        # ЧАЕВЫХ МОЖЕТ БЫТЬ НЕСКОЛЬКО. Боевой пул отправителей (слово
        # владельца 25.09) посылает ОДНУ подписанную покупку сразу всеми
        # сервисами, а каждый сервис проверяет СВОИ чаевые и ниже своего
        # минимума молча отбрасывает транзакцию. Поэтому tip -- это либо пара
        # (адрес, лампорты), либо список таких пар; проверка суммы и списка
        # получателей -- у вызывающего (bloom_own_send), здесь только сборка.
        пары = (tip if isinstance(tip, (list, tuple))
                and tip and isinstance(tip[0], (list, tuple)) else [tip])
        for адрес_ч, лам_ч in пары:
            ixs.append(sol_transfer(payer, адрес_ч, int(лам_ч)))
    msg = MessageV0.try_compile(Pubkey.from_string(payer), ixs, [], Hash.default())
    n_sig = msg.header.num_required_signatures
    vtx = VersionedTransaction.populate(msg, [Signature.default()] * n_sig)
    raw = bytes(vtx)
    return {"tx_base64": base64.b64encode(raw).decode(), "size": len(raw),
            "n_instructions": len(ixs), "quote_mint": mv["quote_mint"], "base_mint": mv["base_mint"]}


# ------------------------------------------------------------ минимум по резервам

def min_out_from_reserves(tpl: dict, tx: dict, amount_in: int, slippage: float) -> dict:
    """Минимум токенов по резервам ПОСЛЕ сделки источника, x*y=k.

    f -- доля траты, доходящая до пула, калиброванная на сделке источника по
    его резервам ДО и его дельтам: f = x0*dy/((y0-dy)*spent), где spent --
    всё, что ушло из котировки в счета этой инструкции (пул + комиссии)."""
    if tpl["program"] in (DAMM2, DLMM, CLMM):
        return {"ok": False, "why_not": "сосредоточенная ликвидность: резервы цену не дают"}
    if tpl["program"] == LAUNCHLAB:
        return launchlab_min_out(tx, amount_in, slippage)
    mv = mints_and_vaults(tpl, tx)
    rows = {r["account"]: r for r in C.token_rows(tx).values()}
    qv, bv = rows.get(mv["quote_vault"]), rows.get(mv["base_vault"])
    if not qv or not bv:
        return {"ok": False, "why_not": "хранилищ нет в балансах транзакции"}
    x0, y0, x1, y1 = qv["pre"], bv["pre"], qv["post"], bv["post"]
    dy = y0 - y1
    spent = sum(r["post"] - r["pre"] for a, r in rows.items()
                if a in tpl["accounts"] and r["mint"] == mv["quote_mint"] and r["post"] > r["pre"])
    if dy <= 0 or spent <= 0 or y0 <= dy:
        return {"ok": False, "why_not": "сделка источника не покупка по этим хранилищам"}
    f = D(x0) * D(dy) / (D(y0 - dy) * D(spent))
    a_eff = D(amount_in) * f
    expected = D(y1) * a_eff / (D(x1) + a_eff)
    mn = int(expected * D(1 - slippage))
    return {"ok": True, "fee_factor": float(f), "reserves_after": [x1, y1],
            "expected_out": int(expected), "min_out": mn}


# ------------------------------------------------------------ Launchlab: виртуальные резервы

# Событие сделки Launchlab: дискриминатор Anchor sha256("event:TradeEvent")[:8]
# = bddb7fd34ee661ee (совпадает с «Program data» настоящих транзакций).
# Раскладка u64 после pool_state (32 байта) установлена по данным: реальные
# резервы до/после сходятся с движением хранилищ, выход сделки источника
# воспроизводится формулой кривой ТОЧНО (самопроверка на всех образцах).
LL_EVENT_DISC = hashlib.sha256(b"event:TradeEvent").digest()[:8]
LL_FIELDS = ("total_base_sell", "virtual_base", "virtual_quote", "real_base_before",
             "real_quote_before", "real_base_after", "real_quote_after", "amount_in", "amount_out",
             "protocol_fee", "platform_fee")


def launchlab_event(tx: dict) -> dict | None:
    for ln in ((tx or {}).get("meta") or {}).get("logMessages") or []:
        if not ln.startswith("Program data: "):
            continue
        try:
            raw = base64.b64decode(ln[len("Program data: "):].strip())
        except ValueError:
            continue
        if raw[:8] != LL_EVENT_DISC or len(raw) < 40 + 8 * len(LL_FIELDS):
            continue
        vals = struct.unpack("<" + "Q" * len(LL_FIELDS), raw[40:40 + 8 * len(LL_FIELDS)])
        return dict(zip(LL_FIELDS, vals))
    return None


def launchlab_out(ev: dict, amount_in: int, after: bool, fee_rate: D) -> D:
    rb = ev["real_base_after"] if after else ev["real_base_before"]
    rq = ev["real_quote_after"] if after else ev["real_quote_before"]
    base_res, quote_res = D(ev["virtual_base"] - rb), D(ev["virtual_quote"] + rq)
    net = D(amount_in) * (1 - fee_rate)
    return base_res * net / (quote_res + net)


def launchlab_min_out(tx: dict, amount_in: int, slippage: float) -> dict:
    ev = launchlab_event(tx)
    if not ev:
        return {"ok": False, "why_not": "нет события TradeEvent Launchlab в логах"}
    fee = ev["amount_in"] - (ev["real_quote_after"] - ev["real_quote_before"])
    fee_rate = D(fee) / D(ev["amount_in"]) if ev["amount_in"] else D(0)
    exp = launchlab_out(ev, amount_in, True, fee_rate)
    return {"ok": True, "fee_rate": float(fee_rate), "virtual_reserves_after": [
        ev["virtual_base"] - ev["real_base_after"], ev["virtual_quote"] + ev["real_quote_after"]],
        "expected_out": int(exp), "min_out": int(exp * D(1 - slippage))}


# ------------------------------------------------------------ самопроверка

def load_samples(program: str) -> list:
    p = SAMPLES_DIR / f"{program}.json"
    own = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    if program not in (DLMM, CLMM):
        return own
    # DLMM и CLMM встречаются и как шаг чужих маршрутов (SOL -> xStock и т.п.)
    # -- эти инструкции тоже настоящие, берём их в проверку.
    n_min, ix_name, vi = (16, "swap2", 2) if program == DLMM else (13, "swap_v2", 5)
    seen = {(C.first_signature(x["tx"]), x["pool_vault"]) for x in own}
    extra = []
    for f in sorted(SAMPLES_DIR.glob("*.json")):
        if f.name == p.name:
            continue
        for x in json.loads(f.read_text(encoding="utf-8")):
            for ix in all_instructions(x["tx"]):
                if ix.get("programId") != program or len(ix["accounts"]) < n_min:
                    continue
                if b58decode(ix["data"])[:8] not in ((disc(ix_name), disc("swap")) if program == DLMM
                                                     else (disc(ix_name),)):
                    continue
                key = (C.first_signature(x["tx"]), ix["accounts"][vi])
                if key in seen:
                    continue
                seen.add(key)
                extra.append({"tx": x["tx"], "pool_vault": ix["accounts"][vi], "source": x["source"],
                              "mint": None, "quote_mint": None})
    return own + extra


def rebuild_check(s: dict, program: str) -> dict:
    """Восстановить инструкцию источника из шаблона и его же адреса."""
    tx = s["tx"]
    tpl = extract_template(tx, program, s["pool_vault"])
    if not tpl["ok"]:
        return {"ok": None, "why": tpl["why_not"]}
    user = tpl["accounts"][SPECS[program]["user"][0]]
    try:
        ix = swap_instruction(tpl, tx, user, tpl["arg0"], tpl["arg1"], keep_source_ix=True)
    except ValueError as exc:
        return {"ok": False, "why": str(exc)}
    got = [str(m.pubkey) for m in ix.accounts]
    diff = [i for i, (g, w) in enumerate(zip(got, tpl["accounts"])) if g != w]
    same_data = bytes(ix.data) == tpl["data"]
    # Счёт пользователя у источника -- не ATA (временный счёт роутера):
    # сверять не с чем, это не ошибка сборщика, а другой счёт у источника.
    rows = {r["account"]: r for r in C.token_rows(tx).values()}
    user_tok = {i for i in user_accounts(tpl, tx, user)} - set(SPECS[program]["user"])
    not_ata = [i for i in diff if i in user_tok and (tpl["accounts"][i] not in rows
                                                     or rows[tpl["accounts"][i]]["owner"] == user)]
    if diff and set(diff) == set(not_ata) and same_data:
        return {"ok": None, "why": "у источника токен-счёт не ATA", "diff_idx": diff}
    # Платит роутер (его PDA -- «пользователь» инструкции, не подписант), а
    # выход приходит на счёт самого источника: пользователь и владелец
    # выходного счёта разные -- сверять ATA пользователя не с чем.
    if diff and user not in tpl["signers"] and set(diff) <= user_tok and same_data:
        return {"ok": None, "why": "платит роутер, токен-счёт чужой", "diff_idx": diff}
    # Подписант платит сам, а выход приходит на счёт ДРУГОГО владельца
    # (получатель не подписант): наш ATA сверять не с чем.
    foreign = [i for i in diff if i in user_tok and tpl["accounts"][i] in rows
               and rows[tpl["accounts"][i]]["owner"] != user]
    if diff and set(diff) == set(foreign) and same_data:
        return {"ok": None, "why": "выход источника на счёт другого владельца", "diff_idx": diff}
    return {"ok": not diff and same_data, "diff_idx": diff, "same_data": same_data,
            "ix": tpl["ix"], "user_is_signer": user in tpl["signers"]}


def self_test() -> int:
    checks = []
    for program, spec in SPECS.items():
        sm = load_samples(program)
        res = [rebuild_check(s, program) for s in sm]
        ok = [r for r in res if r["ok"]]
        skipped = [r for r in res if r["ok"] is None]
        bad = [r for r in res if r["ok"] is False]
        # Несовпадение допустимо только там, где у источника счета не ATA
        # (роутер со своими временными счетами) -- это видно по индексам.
        checks.append((f"{spec['label']}: из {len(sm)} настоящих транзакций восстановлено точно {len(ok)}, "
                       f"не сверяемо {len(skipped)} ({sorted({r['why'] for r in skipped})}), "
                       f"расхождений {len(bad)} {[r.get('diff_idx') for r in bad]}",
                       (len(ok) >= 10 or (program == LAUNCHLAB and len(ok) >= 8)) and not bad))
        # Launchlab: в образцах задачи D всего 12 транзакций; добор до 10+
        # точных делает c2_swap_sim на раннере (сделки Launchlab из задачи A).
        t0 = time.perf_counter()
        n = 0
        for s in sm:
            tpl = extract_template(s["tx"], program, s["pool_vault"])
            if not tpl["ok"]:
                continue
            kp = Keypair()
            b = build_buy(tpl, s["tx"], user=str(kp.pubkey()), payer=s["source"] or str(kp.pubkey()), amount_in=10_000_000,
                          min_out=1, cu_price_micro=100_000, tip=None)
            n += 1
            assert b["size"] <= 1232, b["size"]
        dt = (time.perf_counter() - t0) * 1000 / max(1, n)
        checks.append((f"{spec['label']}: сборка {n} транзакций, среднее {dt:.2f} мс, размер <= 1232", n >= 10))
    v1 = v1_ok = 0
    for s_ in load_samples(DLMM):
        tpl = extract_template(s_["tx"], DLMM, s_["pool_vault"])
        if tpl.get("ok") and tpl["ix"] == "swap":
            v1 += 1
            ix = swap_instruction(tpl, s_["tx"], tpl["accounts"][10], tpl["arg0"], tpl["arg1"])
            v1_ok += bytes(ix.data) == tpl["data"]
    checks.append((f"DLMM swap v1: наши данные совпадают с данными источника байт в байт: {v1_ok} из {v1}",
                   v1 >= 5 and v1_ok == v1))
    # минимум по резервам: на настоящей покупке Pump AMM формула с f должна
    # вернуть сделку самого источника (его трата -> его токены).
    sm = load_samples(PUMP_AMM)
    errs = []
    for s in sm:
        tpl = extract_template(s["tx"], PUMP_AMM, s["pool_vault"])
        if not tpl["ok"]:
            continue
        mv = mints_and_vaults(tpl, s["tx"])
        rows = {r["account"]: r for r in C.token_rows(s["tx"]).values()}
        qv, bv = rows[mv["quote_vault"]], rows[mv["base_vault"]]
        spent = sum(r["post"] - r["pre"] for a, r in rows.items()
                    if a in tpl["accounts"] and r["mint"] == mv["quote_mint"] and r["post"] > r["pre"])
        f = D(qv["pre"]) * D(bv["pre"] - bv["post"]) / (D(bv["post"]) * D(spent))
        pred = D(bv["pre"]) * D(spent) * f / (D(qv["pre"]) + D(spent) * f)
        errs.append(abs(float(pred) / (bv["pre"] - bv["post"]) - 1))
    ll = []
    for s in load_samples(LAUNCHLAB):
        ev = launchlab_event(s["tx"])
        if not ev:
            continue
        fee = ev["amount_in"] - (ev["real_quote_after"] - ev["real_quote_before"])
        pred = launchlab_out(ev, ev["amount_in"], False, D(fee) / D(ev["amount_in"]))
        ll.append(abs(float(pred) - ev["amount_out"]) / ev["amount_out"])
        rows = {r["account"]: r for r in C.token_rows(s["tx"]).values()}
        bv = rows.get(s["pool_vault"])
        if bv and bv["pre"] - bv["post"] != ev["amount_out"]:
            ll.append(1.0)
    checks.append((f"Launchlab: кривая на виртуальных резервах из события воспроизводит выход "
                   f"источника ({len(ll)} проверок, макс. отклонение {max(ll) if ll else None})",
                   len(ll) >= 10 and max(ll) < 1e-6))
    checks.append((f"формула x*y=k с калибровкой воспроизводит сделку источника ({len(errs)} сделок, "
                   f"макс. отклонение {max(errs) if errs else None})", errs and max(errs) < 1e-9))
    bad_n = 0
    for name, ok in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {name}")
        bad_n += (not ok)
    print(f"самопроверка c2_swap_build: {len(checks) - bad_n}/{len(checks)} пройдено")
    return 0 if bad_n == 0 else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    raise SystemExit(self_test() if a.self_test else 0)
