#!/usr/bin/env python3
"""Подбивка: цена места в блоке для живых сделок полосы -- приоритет планировщика Agave.

Только чтение. Блоки -- Helius getBlock (json, transactionDetails full,
maxSupportedTransactionVersion 0). Формулы -- agave master (проверено 28.09):
  core/src/transaction_priority.rs::calculate_priority_and_cost
    приоритет = награда × 1_000_000 / (стоимость + 1)
  runtime/src/bank/fee_distribution.rs::calculate_reward_and_burn_fee_details
    награда = приоритетная + (базовая − базовая×50/100); базовая = 5000 × (подписи
    транзакции + подписи предкомпиляций ed25519/secp256k1/secp256r1)
  cost-model/src/cost_model.rs + block_cost_limits.rs, transaction_cost.rs::sum
    стоимость = 720×подписей (+6690 secp256k1, +2400 ed25519, +4800 secp256r1)
              + 300×записываемых счетов + floor(байт данных инструкций / 4)
              + лимит CU + 8 × ceil(лимит загруженных счетов / 32 КиБ)
  compute-budget-instruction/.../compute_budget_instruction_details.rs
    лимит CU: SetComputeUnitLimit (не больше 1_400_000); нет -- 3000 × инструкций
    встроенных программ (system, compute budget, загрузчики, предкомпиляции)
    + 200000 × прочих; лимит загруженных: SetLoadedAccountsDataSizeLimit, нет --
    64 МиБ (→ 8 × 2048 = 16384).
Приоритетная плата берётся из meta.fee − базовая (что транзакция заплатила).
Голосования (Vote111…) из счёта исключены -- у них отдельный путь.
Записываемые: заголовок сообщения + loadedAddresses.writable, минус понижение
Agave (счёт, вызванный как программа, без upgradeable-загрузчика в сообщении;
зарезервированные sysvar и встроенные программы).

Вход: сделки полосы Code-1 (data/podbivka/mesto/vhod_*.json -- копия
data/sdelki_polosy_*.json с ветки Code-1); выход: data/podbivka/mesto/<метка>.json,
docs/podbivka_<метка>_mesto_v_bloke.md.
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka" / "mesto"

CB = "ComputeBudget111111111111111111111111111111"
VOTE = "Vote111111111111111111111111111111111111111"
SYSTEM = "11111111111111111111111111111111"
ED25519 = "Ed25519SigVerify111111111111111111111111111"
SECP256K1 = "KeccakSecp256k11111111111111111111111111111"
SECP256R1 = "Secp256r1SigVerify1111111111111111111111111"
UPGR = "BPFLoaderUpgradeab1e11111111111111111111111"
ВСТРОЕННЫЕ = {SYSTEM, CB, UPGR, "BPFLoader1111111111111111111111111111111111",
              "BPFLoader2111111111111111111111111111111111", "LoaderV411111111111111111111111111111111111",
              SECP256K1, ED25519}
# зарезервированные (agave reserved_account_keys): встроенные и sysvar
ЗАРЕЗ = ВСТРОЕННЫЕ | {VOTE, SECP256R1, "Stake11111111111111111111111111111111111111",
                      "Config1111111111111111111111111111111111111", "AddressLookupTab1e1111111111111111111111111",
                      "NativeLoader1111111111111111111111111111111", "ZkTokenProof1111111111111111111111111111111",
                      "ZkE1Gama1Proof11111111111111111111111111111", "Feature111111111111111111111111111111111111",
                      "SysvarC1ock11111111111111111111111111111111", "SysvarEpochSchedu1e111111111111111111111111",
                      "SysvarFees111111111111111111111111111111111", "Sysvar1nstructions1111111111111111111111111",
                      "SysvarRecentB1ockHashes11111111111111111111", "SysvarRent111111111111111111111111111111111",
                      "SysvarRewards111111111111111111111111111111", "SysvarS1otHashes111111111111111111111111111",
                      "SysvarS1otHistory11111111111111111111111111", "SysvarStakeHistory1111111111111111111111111",
                      "SysvarEpochRewards1111111111111111111111111", "SysvarLastRestartS1ot1111111111111111111111",
                      "Sysvar1111111111111111111111111111111111111"}
ЛИМИТ_CU_МАКС = 1_400_000
ЗАГР_УМОЛЧ = 64 * 1024 * 1024
БИЛЕТЫ = (0.3, 0.5, 1.0, 3.0)
АЛФ = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + АЛФ.index(ch)
    сырые = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + сырые


def данные(ix: dict) -> bytes:
    d = ix.get("data") or ""
    if isinstance(d, list):              # ["base64", "..."] на всякий случай
        return base64.b64decode(d[0])
    return b58(d)


def разбор(tx: dict) -> dict | None:
    """Плата, стоимость и приоритет транзакции по формуле Agave."""
    t, meta = tx.get("transaction") or {}, tx.get("meta") or {}
    msg = t.get("message") or {}
    ключи = list(msg.get("accountKeys") or [])
    la = meta.get("loadedAddresses") or {}
    все = ключи + list(la.get("writable") or []) + list(la.get("readonly") or [])
    h = msg.get("header") or {}
    nsig = int(h.get("numRequiredSignatures") or len(t.get("signatures") or []))
    ro_s, ro_u = int(h.get("numReadonlySignedAccounts") or 0), int(h.get("numReadonlyUnsignedAccounts") or 0)
    ixs = msg.get("instructions") or []
    прог = [все[ix["programIdIndex"]] for ix in ixs]
    if VOTE in прог:
        return {"голос": True}
    вызваны = {ix["programIdIndex"] for ix in ixs}
    есть_upgr = UPGR in ключи
    запис = 0
    for i, к in enumerate(все):
        if i < len(ключи):
            w = (i < nsig - ro_s) if i < nsig else (i < len(ключи) - ro_u)
        else:
            w = i < len(ключи) + len(la.get("writable") or [])
        if w and ((i in вызваны and not есть_upgr) or к in ЗАРЕЗ):
            w = False
        запис += w
    лимит_cu = лимит_загр = цена = None
    байт, встр, прочих = 0, 0, 0
    п_ed = п_k1 = п_r1 = 0
    for ix, p in zip(ixs, прог):
        d = данные(ix)
        байт += len(d)
        if p == CB and d:
            if d[0] == 2 and len(d) >= 5:
                лимит_cu = int.from_bytes(d[1:5], "little")
            elif d[0] == 3 and len(d) >= 9:
                цена = int.from_bytes(d[1:9], "little")
            elif d[0] == 4 and len(d) >= 5:
                лимит_загр = int.from_bytes(d[1:5], "little")
        if p in ВСТРОЕННЫЕ:
            встр += 1
        else:
            прочих += 1
        if p == ED25519 and d:
            п_ed += d[0]
        elif p == SECP256K1 and d:
            п_k1 += d[0]
        elif p == SECP256R1 and d:
            п_r1 += d[0]
    кф = msg.get("transactionConfig")      # транзакция v1: бюджет в самом сообщении (образец 28.09)
    if isinstance(кф, dict):
        if кф.get("computeUnitLimit") is not None:
            лимит_cu = int(кф["computeUnitLimit"])
        if кф.get("loadedAccountsDataSizeLimit") is not None:
            лимит_загр = int(кф["loadedAccountsDataSizeLimit"])
    cu = min(лимит_cu if лимит_cu is not None else 3000 * встр + 200_000 * прочих, ЛИМИТ_CU_МАКС)
    загр = min(лимит_загр or ЗАГР_УМОЛЧ, ЗАГР_УМОЛЧ)
    стоим = (720 * nsig + 6690 * п_k1 + 2400 * п_ed + 4800 * п_r1 + 300 * запис + байт // 4
             + cu + 8 * math.ceil(загр / 32768))
    базовая = 5000 * (nsig + п_ed + п_k1 + п_r1)
    fee = int(meta.get("fee") or 0)
    приор_плата = max(0, fee - базовая)
    награда = приор_плата + (базовая - базовая * 50 // 100)
    # сверка стоимости с meta.costUnits (у валидатора -- по фактически потреблённым CU)
    cu_факт = meta.get("computeUnitsConsumed")
    стоим_факт = (стоим - cu + int(cu_факт)) if cu_факт is not None else None
    минты = sorted({b.get("mint") for b in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or [])
                    if b.get("mint")})
    return {"подпись": (t.get("signatures") or [None])[0], "версия": tx.get("version"), "подписей": nsig, "минты": минты,
            "costUnits": meta.get("costUnits"), "стоимость_по_факту_cu": стоим_факт,
            "v1_priorityFee": (кф or {}).get("priorityFee") if isinstance(кф, dict) else None, "записываемых": запис, "байт": байт,
            "лимит_cu": cu, "лимит_cu_задан": лимит_cu is not None, "цена_мкл": цена, "стоимость": стоим,
            "плата_приор": приор_плата, "базовая": базовая, "награда": награда,
            "приоритет": награда * 1_000_000 // (стоим + 1), "ошибка": meta.get("err") is not None,
            "чаевые_на": sorted({к for к in все if к in ЧАЕВЫЕ} & переводы(tx, все, прог)),
            "_параметры": (nsig, п_ed, п_k1, п_r1, запис, байт, загр)}


def разбор_б(tx: dict) -> dict | None:
    try:
        return разбор(tx)
    except Exception as exc:  # noqa: BLE001 -- формат не разобран: пометка, не падение
        return {"не_разобрана": f"{type(exc).__name__}: {exc}"[:120], "версия": tx.get("version"),
                "подпись": ((tx.get("transaction") or {}).get("signatures") or [None])[0]}


def переводы(tx: dict, все: list, прог: list) -> set:
    """Получатели системных переводов (внешних и вложенных) -- для чаевых."""
    из_ = set()
    ixs = list(((tx.get("transaction") or {}).get("message") or {}).get("instructions") or [])
    for гр in ((tx.get("meta") or {}).get("innerInstructions") or []):
        ixs += гр.get("instructions") or []
    for ix in ixs:
        if все[ix["programIdIndex"]] != SYSTEM:
            continue
        d = данные(ix)
        if len(d) >= 12 and d[:4] == b"\x02\x00\x00\x00" and len(ix.get("accounts") or []) >= 2:
            из_.add(все[ix["accounts"][1]])
    return из_


def загрузить_чаевые() -> dict:
    с = json.loads((КОРЕНЬ / "data" / "senders.json").read_text(encoding="utf-8"))["senders"]
    return {a: имя for имя, v in с.items() for a in (v.get("tip_accounts") or [])}


ЧАЕВЫЕ: dict = {}
ОБРАЗЦЫ: dict = {"версии": {}}


def стоимость_при(р: dict, cu: int) -> int:
    nsig, п_ed, п_k1, п_r1, запис, байт, загр = р["_параметры"]
    return (720 * nsig + 6690 * п_k1 + 2400 * п_ed + 4800 * п_r1 + 300 * запис + байт // 4
            + cu + 8 * math.ceil(загр / 32768))


def блок(уз, слот: int) -> list:
    б = уз.вызов("getBlock", [слот, {"encoding": "json", "transactionDetails": "full", "rewards": False,
                                     "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}], срок=60.0)
    тт = (б or {}).get("transactions") or []
    for т in тт:                         # образец транзакции v1 -- как есть, для проверки разбора
        if т.get("version") not in (None, "legacy", 0) and "v1" not in ОБРАЗЦЫ:
            ОБРАЗЦЫ["v1"] = т
        ОБРАЗЦЫ["версии"][str(т.get("version"))] = ОБРАЗЦЫ["версии"].get(str(т.get("version")), 0) + 1
    return тт


def сделка(уз, r: dict, кэш: dict) -> dict:
    из_ = {"cid": r.get("cid"), "utc": r.get("utc"), "группа": r.get("group"), "src_sig": r.get("source_sig"),
           "наша_sig": r.get("landed_sig") or r.get("buy_sig"), "s_src": r.get("source_slot"),
           "s_наш": r.get("landed_slot") or r.get("our_slot")}
    if not из_["наша_sig"] or not из_["s_наш"] or not из_["s_src"]:
        return {**из_, "why_not": "нет севшей подписи или слота"}
    for s in {из_["s_src"], из_["s_наш"]}:
        if s not in кэш:
            кэш[s] = [разбор_б(т) for т in блок(уз, s)]
    бс, бн = кэш[из_["s_src"]], кэш[из_["s_наш"]]
    i_src = next((i for i, x in enumerate(бс) if x and x.get("подпись") == из_["src_sig"]), None)
    i_наш = next((i for i, x in enumerate(бн) if x and x.get("подпись") == из_["наша_sig"]), None)
    из_.update(i_src=i_src, i_наш=i_наш, в_блоке_src=len(бс), в_блоке_наш=len(бн),
               слотов=из_["s_наш"] - из_["s_src"])
    if i_src is None or i_наш is None:
        return {**из_, "why_not": "подпись не найдена в блоке"}
    мы, ист = бн[i_наш], бс[i_src]
    if мы.get("не_разобрана") or ист.get("не_разобрана"):
        return {**из_, "why_not": "наша или источника транзакция не разобрана: " + str(мы.get("не_разобрана") or ист.get("не_разобрана"))}
    между = (бн[i_src + 1:i_наш] if из_["слотов"] == 0 else бс[i_src + 1:] + бн[:i_наш])
    из_["между_не_разобрано"] = sum(1 for x in между if x and x.get("не_разобрана"))
    между = [x for x in между if x and not x.get("голос") and not x.get("не_разобрана")]
    P = мы["приоритет"]
    ниже = [x for x in между if x["приоритет"] < P]
    выше = [x for x in между if x["приоритет"] > P]
    минт = r.get("mint")
    наш_пул = [x for x in между if минт in (x.get("минты") or [])]
    из_.update(свопов_нашего_минта=len(наш_пул),
               выше_нашего_минта=sum(1 for x in наш_пул if x["приоритет"] > P))
    из_.update(мы={k: v for k, v in мы.items() if not k.startswith("_")},
               источник={k: v for k, v in ист.items() if not k.startswith("_")},
               N=len(между), ниже=len(ниже), выше=len(выше), равно=len(между) - len(ниже) - len(выше))
    # 2. плата, ставящая нас выше всех перебиваемых (при нашем лимите CU)
    if выше:
        цель = max(x["приоритет"] for x in выше) + 1
        награда = -(-цель * (мы["стоимость"] + 1) // 1_000_000)
        плата = max(0, награда - (мы["базовая"] - мы["базовая"] * 50 // 100))
    else:
        плата = 0
    из_["плата_выше_всех_sol"] = плата / 1e9
    # 3. плата 0.0002 SOL, лимит 140k: сколько между новым и текущим нашим приоритетом
    ст = стоимость_при(мы, 140_000)
    P_нов = (200_000 + мы["базовая"] - мы["базовая"] * 50 // 100) * 1_000_000 // (ст + 1)
    из_["P_при_0.0002_140k"] = P_нов
    lo, hi = sorted((P_нов, P))
    из_["перешли_бы_вперёд"] = sum(1 for x in между if lo <= x["приоритет"] < hi) if P_нов < P else 0
    из_["перешли_бы_вперёд_нашего_минта"] = sum(1 for x in наш_пул if lo <= x["приоритет"] < hi) if P_нов < P else 0
    из_["ушли_бы_назад"] = sum(1 for x in между if lo < x["приоритет"] <= hi) if P_нов > P else 0
    # 5. чаевые на известные счета
    с_чаевыми = [x for x in между if x["чаевые_на"]]
    из_["между_с_чаевыми"] = len(с_чаевыми)
    из_["чаевые_по_отправителю"] = {}
    for x in с_чаевыми:
        for a in x["чаевые_на"]:
            из_["чаевые_по_отправителю"][ЧАЕВЫЕ[a]] = из_["чаевые_по_отправителю"].get(ЧАЕВЫЕ[a], 0) + 1
    из_["с_чаевыми_выше_нас"] = sum(1 for x in с_чаевыми if x["приоритет"] > P)
    из_["мы_с_чаевыми"] = [ЧАЕВЫЕ[a] for a in мы["чаевые_на"]]
    из_["между"] = [{k: x[k] for k in ("подпись", "версия", "приоритет", "плата_приор", "лимит_cu", "стоимость", "чаевые_на",
                                       "ошибка", "costUnits", "стоимость_по_факту_cu")} for x in между]
    return из_


def кв(v: list, q: float):
    v = sorted(v)
    return v[min(len(v) - 1, int(q * len(v)))] if v else None


def md(ряды: list, метка: str, контроль: dict | None) -> str:
    ок = [r for r in ряды if not r.get("why_not")]
    out = [f"# Место в блоке: приоритет планировщика Agave -- сделки полосы ({метка})", "",
           "Формула -- agave master (ссылки в analysis/podbivka_mesto_v_bloke.py). Приоритет = (приоритетная плата + 50 % "
           "базовой) × 10⁶ / (стоимость + 1). «Ниже» -- впереди нас в блоке с приоритетом ниже нашего (платой не перебить); "
           "«выше» -- впереди с приоритетом выше (перебиваемы). Голосования исключены. Порядок в блоке задаёт лидер; "
           "совпадает ли его планировщик с Agave, эти числа не проверяют.", ""]
    if контроль:
        out += ["## Контроль: блок KABUTSTR, слот 451134500", "",
                "| индекс | подпись | версия | приоритетная, SOL | лимит CU | цена, мкл/CU | стоимость | стоимость с потреблёнными CU / costUnits валидатора | приоритет |",
                "|---|---|---|---|---|---|---|---|---|"]
        for i, x in контроль.items():
            out.append(f"| {i} | {x['подпись'][:8]} | {x['версия']} | {x['плата_приор'] / 1e9:.6f} | {x['лимит_cu']} | "
                       f"{x['цена_мкл']} | {x['стоимость']} | {x['стоимость_по_факту_cu']} / {x['costUnits']} | "
                       f"{x['приоритет']:,} |" if x and "приоритет" in x
                       else f"| {i} | {(x or {}).get('не_разобрана', '--')} |||||| ")
        out.append("")
    out += ["## По сделкам", "",
            "| время | слот ист. | слотов до нас | i ист. | i наш | наш приоритет | N между | ниже | выше | "
            "плата выше всех, SOL | при 0.0002/140k вперёд | с чаевыми (выше нас) | нашего минта: всего / выше / вперёд при 0.0002 |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    нр = sum(r.get("между_не_разобрано") or 0 for r in ряды)
    if нр:
        out.insert(len(out) - 3, f"Транзакций между источником и нами, не разобранных (формат): {нр} -- в счёт не вошли.\n")
    for r in ряды:
        if r.get("why_not"):
            out.append(f"| {r['utc']} | {r.get('s_src')} | -- | | | | | | | | | {r['why_not']} |")
            continue
        out.append(f"| {r['utc']} | {r['s_src']} | {r['слотов']} | {r['i_src']} | {r['i_наш']} | "
                   f"{r['мы']['приоритет']:,} | {r['N']} | {r['ниже']} | {r['выше']} | {r['плата_выше_всех_sol']:.6f} | "
                   f"{r['перешли_бы_вперёд']} | {r['между_с_чаевыми']} ({r['с_чаевыми_выше_нас']}) | "
                   f"{r.get('свопов_нашего_минта')} / {r.get('выше_нашего_минта')} / {r.get('перешли_бы_вперёд_нашего_минта')} |")
    if ок:
        пл = [r["плата_выше_всех_sol"] for r in ок]
        out += ["", "## Сводка", "", f"Сделок с разбором: {len(ок)} из {len(ряды)}.", "",
                "| показатель | медиана | p80 |", "|---|---|---|"]
        for имя, v in (("N между источником и нами", [r["N"] for r in ок]),
                       ("из них ниже нашего приоритета", [r["ниже"] for r in ок]),
                       ("из них выше (перебиваемы)", [r["выше"] for r in ок]),
                       ("при 0.0002 SOL / 140k перешли бы вперёд", [r["перешли_бы_вперёд"] for r in ок]),
                       ("с переводом на известные счета чаевых", [r["между_с_чаевыми"] for r in ок]),
                       ("из них нашего минта (тот же токен)", [r.get("свопов_нашего_минта") or 0 for r in ок]),
                       ("нашего минта выше нашего приоритета", [r.get("выше_нашего_минта") or 0 for r in ок]),
                       ("нашего минта перешли бы вперёд при 0.0002 / 140k", [r.get("перешли_бы_вперёд_нашего_минта") or 0 for r in ок])):
            out.append(f"| {имя} | {statistics.median(v):g} | {кв(v, 0.8):g} |")
        out += ["", "Плата (приоритетная, при нашем лимите CU), ставящая нас выше всех перебиваемых:", "",
                "| | SOL | " + " | ".join(f"% от {б} SOL" for б in БИЛЕТЫ) + " |", "|---|---|" + "---|" * len(БИЛЕТЫ)]
        for имя, v in (("p50", statistics.median(пл)), ("p80", кв(пл, 0.8))):
            out.append(f"| {имя} | {v:.6f} | " + " | ".join(f"{100 * v / б:.3f}" for б in БИЛЕТЫ) + " |")
        s0 = [r for r in ок if r["слотов"] == 0]
        s1 = [r for r in ок if r["слотов"] > 0]
        out += ["", "## S+0 против S+1 и позже", "",
                "| | сделок | медиана нашего приоритета | медиана приоритета источника |", "|---|---|---|---|"]
        for имя, g in (("S+0", s0), ("S+1 и позже", s1)):
            if g:
                out.append(f"| {имя} | {len(g)} | {statistics.median(r['мы']['приоритет'] for r in g):,.0f} | "
                           f"{statistics.median(r['источник']['приоритет'] for r in g):,.0f} |")
        out.append("")
        out.append("Для S+1 и позже «между» -- остаток блока источника после него плюс наш блок до нас.")
        out.append(f"Сделок в S+1 и позже: {len(s1)} -- " + ("мало данных для вывода о связи с приоритетом."
                                                           if len(s1) < 10 else "см. таблицу."))
        по = {}
        for r in ок:
            for k, n in r["чаевые_по_отправителю"].items():
                по[k] = по.get(k, 0) + n
        out += ["", "Транзакции между источником и нами с переводом на известные счета чаевых (data/senders.json), "
                    "по отправителю: " + (", ".join(f"{k} {n}" for k, n in sorted(по.items(), key=lambda kv: -kv[1]))
                                          or "нет") + ". Чаевые в формулу приоритета не входят."]
    if ок and all("свопов_нашего_минта" in r for r in ок):
        вы = [r["выше_нашего_минта"] for r in ок]
        вп = [r["перешли_бы_вперёд_нашего_минта"] for r in ок]
        out += ["", f"Нашего пула (транзакции с нашим минтом в балансах токенов): из перебиваемых -- медиана "
                    f"{statistics.median(вы):g} (p80 {кв(вы, 0.8):g}, всего {sum(вы)} из {sum(r['выше'] for r in ок)}); "
                    f"из перешедших бы вперёд при 0.0002 SOL / 140k -- медиана {statistics.median(вп):g} "
                    f"(p80 {кв(вп, 0.8):g}, всего {sum(вп)} из {sum(r['перешли_бы_вперёд'] for r in ок)})."]
    св = [x for r in ряды for x in (r.get("между") or []) if x.get("costUnits") is not None]
    if св:
        равно = sum(1 for x in св if x["costUnits"] == x["стоимость_по_факту_cu"])
        откл = sorted(abs(x["costUnits"] - x["стоимость_по_факту_cu"]) for x in св)
        out += ["", f"Сверка формулы стоимости с meta.costUnits валидатора (стоимость с потреблёнными CU вместо лимита): "
                    f"совпало точно {равно} из {len(св)}; |разница| медиана {откл[len(откл) // 2]}, p90 {откл[int(0.9 * len(откл))]}, "
                    f"макс {откл[-1]}; кратно 8 и в пределах 0…16376: "
                    f"{sum(1 for x in св if (x['стоимость_по_факту_cu'] - x['costUnits']) % 8 == 0 and 0 <= x['стоимость_по_факту_cu'] - x['costUnits'] <= 16376)}. "
                    "costUnits валидатор считает после исполнения -- по фактически загруженным байтам счетов; "
                    "планировщик (calculate_priority_and_cost) -- по лимиту загружаемых данных (без лимита -- 64 МиБ, 16384). "
                    "Разница, кратная 8, -- это слагаемое; остальное (не кратное 8) не объяснено."]
    out += ["", "Версии транзакций в прочитанных блоках: " + ", ".join(f"{k} {n}" for k, n in ОБРАЗЦЫ["версии"].items()) + "."]
    return "\n".join(out) + "\n"


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--vhod", default=str(П / "vhod_*.json"))
    р.add_argument("--s", default="2026-09-28T00:21")
    р.add_argument("--metka", default="2026-09-28")
    р.add_argument("--kontrol", action="store_true")
    а = р.parse_args()
    import podbivka_sim as S  # noqa: PLC0415
    ЧАЕВЫЕ.update(загрузить_чаевые())
    ряды_вх, видел = [], set()
    for f in sorted(glob.glob(а.vhod)):
        for r in json.loads(Path(f).read_text(encoding="utf-8"))["ряды"]:
            if (r.get("utc") or "") >= а.s and r.get("cid") not in видел:
                видел.add(r.get("cid"))
                ряды_вх.append(r)
    ряды_вх.sort(key=lambda r: r["utc"])
    уз = S.Узел()
    кэш: dict = {}
    рез = []
    with уз.на("helius"):
        контроль = None
        if а.kontrol:
            б = [разбор_б(т) for т in блок(уз, 451134500)]
            контроль = {i: ({k: v for k, v in б[i].items() if not k.startswith("_")} if i < len(б) and б[i] else None)
                        for i in (648, 709, 720, 982)}
        for r in ряды_вх:
            try:
                x = сделка(уз, r, кэш)
            except Exception as exc:  # noqa: BLE001
                x = {"utc": r.get("utc"), "s_src": r.get("source_slot"), "why_not": S.чисто(f"{type(exc).__name__}: {exc}")[:160]}
            рез.append(x)
            print(x.get("utc"), x.get("i_src"), x.get("i_наш"), x.get("N"), x.get("выше"), x.get("why_not"), flush=True)
            if len(кэш) > 6:
                for k in list(кэш)[:-4]:
                    del кэш[k]
    П.mkdir(parents=True, exist_ok=True)
    out = П / f"mesto_{а.metka}.json"
    out.write_text(json.dumps({"контроль": контроль, "ряды": рез, "образцы": ОБРАЗЦЫ, "расход": уз.расход()}, ensure_ascii=False),
                   encoding="utf-8")
    doc = КОРЕНЬ / "docs" / f"podbivka_{а.metka}_mesto_v_bloke.md"
    doc.write_text(md(рез, а.metka, контроль), encoding="utf-8")
    import podbivka_run as R  # noqa: PLC0415
    R.записано(out)
    R.записано(doc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
