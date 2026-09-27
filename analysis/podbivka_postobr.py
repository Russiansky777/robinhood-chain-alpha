#!/usr/bin/env python3
"""Подбивка, пост-обработка первой таблицы (404 кошелька прохода (а)).

Режимы (слово владельца 27.09):
  --m4  горизонты: +2/+4/+6/+8 (S+0, S+1; по первому свопу не раньше s0+H,
        фактический слот пишется), поздние +300/+600, вход на s0+72 с выходом
        +150/+300/+600 и резервом SOL на +72 и s0 -- по покупкам с числом;
  --m5  336 отказов «история пула > 150 страниц»: состояние на s0+1, s0+72..+75,
        s0+150..+153 -- прямо из блоков (getBlock, transactionDetails
        "accounts"), по счетам пула, без листания истории;
  --m6  219 отказов «у кривой нет события сделки»: дискриминаторы инструкций
        программы кривой в сделке источника (ищется c2ab1c46684d5b2f).
Список покупок -- data/podbivka/postobr_spisok.json (собирается --spisok,
без сети). Результат -- data/podbivka/postobr/<режим>_<с>_<по>.jsonl, строка
на покупку сразу. Узел -- по скользящей грани (podbivka_sim).
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import c2_swap_build as SB  # noqa: E402
import podbivka as P  # noqa: E402
import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
ДИСК_НОВЫЙ = "c2ab1c46684d5b2f"


def собрать_список() -> int:
    import podbivka_tablicy as T  # noqa: PLC0415
    m4, m5, m6 = [], [], []
    for ф in glob.glob(str(КОРЕНЬ / "data" / "podbivka" / "koshelki" / "*.json")):
        if Path(ф).name.startswith("_"):
            continue
        д = json.loads(Path(ф).read_text(encoding="utf-8"))
        адрес = д["строка"]["address"]
        for п in T.пересчёт_порога(д.get("покупки") or [], адрес):
            з = {"signature": п["signature"], "slot": п.get("slot"), "blockTime": п.get("blockTime"),
                 "mint": п["mint"], "wallet": адрес, "порог": п.get("порог")}
            почему = (п.get("sim") or {}).get("why_not") or ""
            if п.get("sim") and not почему:
                m4.append(з)
            elif почему.startswith("история пула не собрана"):
                m5.append(з)
            elif "нет события сделки кривой" in почему:
                m6.append(з)
    Path(КОРЕНЬ / "data" / "podbivka" / "postobr_spisok.json").write_text(
        json.dumps({"m4": m4, "m5": m5, "m6": m6}, ensure_ascii=False), encoding="utf-8")
    print(f"список: m4 {len(m4)}, m5 {len(m5)}, m6 {len(m6)}")
    return 0


# ------------------------------------------------------------ m6

def m6(уз: S.Узел, п: dict) -> dict:
    tx = уз.tx(п["signature"])
    if not tx:
        return {"why_not": "узел не отдал"}
    диски = []
    for ix in SB.all_instructions(tx):
        if ix.get("programId") == SB.BONDING and ix.get("data"):
            try:
                диски.append(SB.b58decode(ix["data"])[:8].hex())
            except Exception:  # noqa: BLE001
                pass
    return {"диски_кривой": диски, "новый_вариант": ДИСК_НОВЫЙ in диски}


# ------------------------------------------------------------ m5

def блок(уз: S.Узел, слот: int):
    return уз.вызов("getBlock", [слот, {"transactionDetails": "accounts", "rewards": False,
                                         "maxSupportedTransactionVersion": 1,
                                         "commitment": "confirmed"}], срок=60.0)


def счета_в_блоке(блок_: dict, пул: dict) -> dict | None:
    """Последняя успешная tx блока, где есть хранилище пула: post балансы счетов пула."""
    послед = None
    for t in (блок_ or {}).get("transactions") or []:
        мета = t.get("meta") or {}
        if мета.get("err") is not None:
            continue
        ключи = [k.get("pubkey") if isinstance(k, dict) else k
                 for k in ((t.get("transaction") or {}).get("accountKeys") or [])]
        if пул["pool_vault"] not in ключи:
            continue
        послед = (t, ключи)
    if not послед:
        return None
    t, ключи = послед
    мета = t.get("meta") or {}
    ряды: dict = {}
    for when in ("pre", "post"):
        for b in мета.get(f"{when}TokenBalances") or []:
            i = b.get("accountIndex")
            if isinstance(i, int) and i < len(ключи):
                ряды.setdefault(ключи[i], {})[when] = int((b.get("uiTokenAmount") or {}).get("amount") or 0)
    лам = {}
    if пул.get("quote_vault") in ключи:
        i = ключи.index(пул["quote_vault"])
        лам = {"pre": (мета.get("preBalances") or [0])[i], "post": (мета.get("postBalances") or [0])[i]}
    return {"ряды": ряды, "лам": лам}


def ст_из_блоков(уз, s_от: int, пул: dict, режим: str, база: dict, пропуск: int = 4):
    """Состояние по первому блоку из s_от..s_от+пропуск-1, где пул был в сделке."""
    for слот in range(s_от, s_от + пропуск):
        try:
            б = блок(уз, слот)
        except RuntimeError:
            continue
        сч = счета_в_блоке(б, пул)
        if not сч:
            continue
        бв = сч["ряды"].get(пул["pool_vault"], {})
        кв = сч["ряды"].get(пул["quote_vault"], {})
        if режим == "xyk" and "post" in бв and "post" in кв:
            return {"x": кв["post"], "y": бв["post"]}, слот
        if режим == "curve" and "post" in бв and сч["лам"]:
            # Виртуальные резервы кривой двигаются ровно как реальные.
            return {"vs": база["vs"] + (сч["лам"]["post"] - база["лам_sol"]),
                    "vt": база["vt"] + (бв["post"] - база["ток"]),
                    "rs": (база.get("rs") or 0) + (сч["лам"]["post"] - база["лам_sol"]), "rt": None}, слот
        if режим == "launchlab" and "post" in бв and "post" in кв:
            return {"quote": база["quote"] + (кв["post"] - база["кв"]), "base": база["base"] + (бв["post"] - база["бв"]),
                    "rq": кв["post"]}, слот
        if режим == "price" and "post" in бв and "post" in кв and "pre" in бв and "pre" in кв:
            дб, дк = бв["post"] - бв["pre"], кв["post"] - кв["pre"]
            if дб and дк and (дб > 0) != (дк > 0) and abs(дк) >= S.МИН_КОТИРОВКИ_СВОПА:
                return {"price": abs(дк) / abs(дб), "q_after": кв["post"]}, слот
    return None, None


def m5(уз: S.Узел, п: dict) -> dict:
    tx0 = уз.tx(п["signature"])
    if not tx0:
        return {"why_not": "узел не отдал сделку источника"}
    s0 = tx0["slot"]
    сим0 = P.наша_покупка_по_симулятору(tx0, п["wallet"], п["mint"], S.РАЗМЕР_ЛАМ)
    if not сим0.get("ok"):
        return {"why_not": f"S+0: {сим0.get('why_not')}"}
    режим = S.режим_из_метода(сим0["method"])
    f = сим0.get("fee_factor")
    пул = C.identify_pool(tx0, п["wallet"], п["mint"])
    ряды0 = {r["account"]: r for r in C.token_rows(tx0).values()}
    база: dict = {}
    кривая_bps = (0, 0)
    if режим == "xyk" and сим0.get("program") == SB.LAUNCHLAB:
        режим = "launchlab"
        f = SB.launchlab_min_out(tx0, S.РАЗМЕР_ЛАМ, 0.0).get("fee_rate")
    if режим == "curve":
        ев = SB.pump_trade_event(tx0, п["mint"])
        кривая_bps = (int(ев["fee_bps"]), int(ев["creator_fee_bps"])) if ев else (0, 0)
        лд = C.lamport_delta(tx0, пул["quote_vault"])
        # SOL кривой лежит на самом аккаунте кривой (владелец хранилища токена).
        пул = {**пул, "quote_vault": пул.get("quote_vault") or пул.get("pool_owner")}
        ключи = C.account_keys(tx0)
        if not ев or пул.get("quote_vault") not in ключи or пул.get("pool_vault") not in ряды0:
            return {"why_not": "кривая: счёт кривой или хранилище не опознаны"}
        i = ключи.index(пул["quote_vault"])
        база = {"vs": ев["virtual_sol_reserves"], "vt": ев["virtual_token_reserves"],
                "rs": ев["real_sol_reserves"], "лам_sol": (tx0["meta"]["postBalances"])[i],
                "ток": ряды0[пул["pool_vault"]]["post"]}
        ст0 = {"vs": база["vs"], "vt": база["vt"], "rs": база["rs"], "rt": ев["real_token_reserves"]}
        del лд
    elif режим == "launchlab":
        ст0 = S.состояние(tx0, "launchlab", пул, п["mint"])
        база = {**ст0, "кв": ряды0[пул["quote_vault"]]["post"], "бв": ряды0[пул["pool_vault"]]["post"]}
    elif режим == "price":
        ц = (P.резервы_после(tx0, п["wallet"], п["mint"]) or {}).get("price_last")
        ст0 = {"price": ц, "q_after": None}
    else:
        ст0 = S.состояние(tx0, "xyk", пул, п["mint"])
    if not ст0:
        return {"why_not": "состояние S+0 не читается"}
    налог = S.налог_минта(уз, п["mint"])
    ст1, сл1 = ст_из_блоков(уз, s0 + 1, пул, режим, база, пропуск=1)
    вход = S.наша_покупка(режим, ст1 or ст0, f=f, кривая_bps=кривая_bps)
    if not вход.get("ok"):
        return {"why_not": вход.get("why_not")}
    из_ = {"режим": режим, "S1_из_блока": bool(ст1)}
    for H in (72, 150):
        ст, сл = ст_из_блоков(уз, s0 + H, пул, режим, база)
        if not ст:
            из_[f"S1_{H}"] = None
            из_[f"слот_{H}"] = None
            continue
        получено = вход["tokens"] - S.удержано(вход["tokens"], налог)
        пр = S.наша_продажа(режим, ст, вход, получено - S.удержано(получено, налог), f=f,
                            кривая_bps=кривая_bps, g=f)
        из_[f"S1_{H}"] = S.чистый_пп(пр["lamports"]) if пр.get("ok") else None
        из_[f"слот_{H}"] = сл
    из_["флаги"] = ["доля продажи = f покупки (продажи пула не смотрелись)"]
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--spisok", action="store_true")
    р.add_argument("--rezhim", choices=("m4", "m5", "m6", "m7"))
    р.add_argument("--s", type=int, default=0)
    р.add_argument("--po", type=int, default=0)
    р.add_argument("--push", action="store_true")
    а = р.parse_args()
    if а.spisok:
        return собрать_список()
    имя_списка = "postobr_spisok_m7.json" if а.rezhim == "m7" else "postobr_spisok.json"
    спис = json.loads((КОРЕНЬ / "data" / "podbivka" / имя_списка).read_text(encoding="utf-8"))[а.rezhim]
    спис = спис[а.s:(а.po or None)]
    out = КОРЕНЬ / "data" / "podbivka" / "postobr" / f"{а.rezhim}_{а.s}_{а.po or 'end'}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    уз = S.Узел()
    последний = time.time()
    import podbivka_run as R  # noqa: PLC0415
    готово = set()
    if out.exists():
        for l in out.read_text(encoding="utf-8").splitlines():
            try:
                готово.add(json.loads(l)["signature"])
            except (ValueError, KeyError):
                pass
    R.записано(out)
    with open(out, "a", encoding="utf-8") as ф:
        for н, п in enumerate(спис):
            if п["signature"] in готово:
                continue
            with уз.на(S.узел_по_времени(п.get("blockTime"))):
                try:
                    if а.rezhim == "m4":
                        рез = S._симулировать(уз, п, горизонты=S.ГОРИЗОНТЫ_ДОП, доп=True)  # noqa: SLF001
                        рез = {к: рез.get(к) for к in ("why_not", "чистый_пп", "доп", "режим", "без_свопов")}
                    elif а.rezhim == "m7":
                        # п.23: наши speed_only с n >= 5, билеты S+0, выход +72.
                        рез = S._симулировать(уз, п, горизонты=(72,), билеты=(0.02, 0.05, 0.1, 0.2, 0.5))  # noqa: SLF001
                        рез = {к: рез.get(к) for к in ("why_not", "режим", "билеты_72", "резерв_s0", "ликвидность_sol")}
                    elif а.rezhim == "m5":
                        рез = m5(уз, п)
                    else:
                        рез = m6(уз, п)
                except Exception as exc:  # noqa: BLE001
                    рез = {"why_not": S.чисто(f"{type(exc).__name__}: {exc}")[:160]}
            уз._кэш.clear()  # noqa: SLF001
            рез.update(signature=п["signature"], wallet=п["wallet"], узел=S.узел_по_времени(п.get("blockTime")))
            ф.write(json.dumps(рез, ensure_ascii=False) + "\n")
            ф.flush()
            if а.push and time.time() - последний > 600:
                R.пуш(f"Podbivka-2: postobr {а.rezhim} {а.s}+{н + 1} [automated]", [str(out)])
                последний = time.time()
    if а.push:
        R.пуш(f"Podbivka-2: postobr {а.rezhim} {а.s}-{а.po or 'end'} gotovo [automated]", [str(out)])
    print(f"postobr {а.rezhim}: {len(спис)} покупок, расход {json.dumps(уз.расход(), ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
