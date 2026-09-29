#!/usr/bin/env python3
"""Опознание программ по цепи (только чтение, Helius): что это за программа, если имени нет -- так и записать.

Для каждого адреса (--programmy):
1. Счёт программы (jsonParsed): загрузчик, programData; programData -- слот последнего развёртывания, upgrade authority
   (или её нет -- программа неизменяемая); владелец счёта authority (System -- обычный кошелёк; иначе -- программа,
   например мультиподпись). История programData (getSignaturesForAddress) -- первое развёртывание и число апгрейдов.
2. IDL Anchor на цепи: адрес create_with_seed(find_program_address([], программа), "anchor:idl", программа); если счёт
   есть -- authority, распакованный zlib JSON, имя (metadata.name или name), инструкции и счета.
3. Program Metadata (ProgM6JCCvbYkfKqJYHePx4xxSUSqJp7rh8Lyv7nk7S): счета этой программы с полем program = наш адрес
   (getProgramAccounts, memcmp со смещения 1) -- сырые данные и попытка прочесть текст.
4. Сам ELF из programData: security.txt (метка =======BEGIN SECURITY.TXT V1=======), строки Anchor «Instruction: …»,
   пути исходников (programs/<имя>/src, src/…rs), версии крейтов (anchor-lang-x.y.z и т. п.); размер и sha256 кода.
5. Живые вызовы (последние --podpisey подписей программы): для каждого вызова -- первый байт и первые 8 байт данных,
   длина данных, число счетов, внешний / внутренний и кто вызвал, строки «Program log:» внутри вызова, CU, час суток UTC
   и исход транзакции; сверка 8 байт с sha256("global:<имя>") по именам из логов/ELF (признак Anchor).
6. Счета, которыми владеет программа (из тех же вызовов, до 100): длина данных и первые 8 байт (дискриминатор счёта).
Выход: data/podbivka/opoznanie_programm.json.
"""
from __future__ import annotations

import argparse
import base64
import collections
import hashlib
import json
import re
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_clmm_sbor as SB  # noqa: E402
import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
ПМП = "ProgM6JCCvbYkfKqJYHePx4xxSUSqJp7rh8Lyv7nk7S"
СИСТЕМА = "11111111111111111111111111111111"
МЕТКА_ST = b"=======BEGIN SECURITY.TXT V1=======\x00"
КОНЕЦ_ST = b"=======END SECURITY.TXT V1=======\x00"
ШАБЛОНЫ = {"anchor_instruction": re.compile(rb"Instruction: ([A-Za-z0-9_]{2,64})"),
           "пути_programs": re.compile(rb"programs/[A-Za-z0-9_\-]{2,64}/src/[A-Za-z0-9_/\-]{1,120}\.rs"),
           "пути_src": re.compile(rb"(?<![A-Za-z0-9_\-/])src/[A-Za-z0-9_/\-]{1,120}\.rs"),
           "крейты": re.compile(rb"[a-z][a-z0-9_\-]{1,40}-\d+\.\d+\.\d+(?:-[a-z0-9.]+)?/src"),
           "anchor_ошибки": re.compile(rb"AnchorError[^\x00]{0,60}")}


def _змея(имя: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", имя).lower()


def счёт(уз, адрес: str, кодировка: str = "base64", срез: dict | None = None):
    парам: dict = {"encoding": кодировка, "commitment": "confirmed"}
    if срез:
        парам["dataSlice"] = срез
    return ((уз.вызов("getAccountInfo", [адрес, парам], срок=60) or {}).get("value"))


def строки_elf(elf: bytes) -> dict:
    из_: dict = {}
    for к, ш in ШАБЛОНЫ.items():
        сч = collections.Counter(m.group(1 if ш.groups else 0).decode("latin-1") for m in ш.finditer(elf))
        из_[к] = sorted(сч)[:300]
    i = elf.find(МЕТКА_ST)
    if i >= 0:
        j = elf.find(КОНЕЦ_ST, i)
        части = elf[i + len(МЕТКА_ST):j if j > i else i + 4000].split(b"\x00")
        из_["security_txt"] = {части[k].decode("utf-8", "replace"): части[k + 1].decode("utf-8", "replace")
                               for k in range(0, len(части) - 1, 2) if части[k]}
    else:
        из_["security_txt"] = None
    return из_


def idl_anchor(уз, программа: str) -> dict:
    from solders.pubkey import Pubkey  # noqa: PLC0415
    p = Pubkey.from_string(программа)
    база, _ = Pubkey.find_program_address([], p)
    адрес = str(Pubkey.create_with_seed(база, "anchor:idl", p))
    v = счёт(уз, адрес)
    из_: dict = {"адрес": адрес, "есть": bool(v)}
    if not v:
        return из_
    d = base64.b64decode(v["data"][0])
    из_.update(владелец=v.get("owner"), длина=len(d), disc=d[:8].hex(), authority=SB._b58e(d[8:40]))  # noqa: SLF001
    try:
        n = int.from_bytes(d[40:44], "little")
        j = json.loads(zlib.decompress(d[44:44 + n]))
        из_["имя"] = (j.get("metadata") or {}).get("name") or j.get("name")
        из_["версия_idl"] = (j.get("metadata") or {}).get("version") or j.get("version")
        из_["инструкции"] = [(x.get("name"), bytes(x["discriminator"]).hex() if x.get("discriminator") else None)
                             for x in j.get("instructions") or []]
        из_["счета"] = [x.get("name") for x in j.get("accounts") or []]
        из_["idl"] = j
    except (ValueError, zlib.error) as exc:
        из_["why_not"] = f"не распаковался: {S.чисто(str(exc))[:100]}"
    return из_


def метаданные(уз, программа: str) -> dict:
    try:
        r = уз.вызов("getProgramAccounts", [ПМП, {"encoding": "base64", "commitment": "confirmed",
                                                  "filters": [{"memcmp": {"offset": 1, "bytes": программа}}]}], срок=60)
    except RuntimeError as exc:
        return {"why_not": S.чисто(str(exc))[:160]}
    из_ = []
    for x in r or []:
        d = base64.b64decode(((x.get("account") or {}).get("data") or [""])[0])
        зап = {"адрес": x.get("pubkey"), "длина": len(d), "authority": SB._b58e(d[33:65]),  # noqa: SLF001
               "seed": d[67:83].rstrip(b"\x00").decode("latin-1"), "сырые_первые_120": d[:120].hex()}
        хвост = d[96:]
        for распак in (lambda b: b, zlib.decompress, lambda b: zlib.decompress(b, 16 + zlib.MAX_WBITS)):
            try:
                т = распак(хвост).decode("utf-8")
                зап["текст"] = т[:4000]
                break
            except (UnicodeDecodeError, zlib.error):
                continue
        из_.append(зап)
    return {"счетов": len(из_), "счета": из_}


def вызовы(уз, программа: str, подписей: int) -> dict:
    сп, до = [], None
    while len(сп) < подписей:
        стр = уз.подписи(программа, до=до, limit=min(1000, подписей - len(сп)))
        if not стр:
            break
        сп += стр
        if len(стр) < 1000:
            break
        до = стр[-1]["signature"]
    ряды, владельцы = [], collections.Counter()
    for и in range(0, len(сп), 100):
        txs = уз.пакет([з["signature"] for з in сп[и:и + 100]])
        for s_, т in txs.items():
            if not т:
                continue
            meta = т.get("meta") or {}
            msg = ((т.get("transaction") or {}).get("message") or {})
            внешние = msg.get("instructions") or []
            логи = журнал(meta.get("logMessages") or [], программа)
            bt = т.get("blockTime")
            k = 0
            for м, ix in SB.инструкции(т):
                if not isinstance(ix, dict) or ix.get("programId") != программа:
                    continue
                d = SB._b58d(ix.get("data") or "") if ix.get("data") else b""  # noqa: SLF001
                внутр = м.startswith("внутренняя")
                родитель = None
                if внутр:
                    н = int(м.split()[1].split(".")[0])
                    родитель = внешние[н].get("programId") if н < len(внешние) else None
                сч = list(ix.get("accounts") or [])
                for а in сч:
                    владельцы[а] += 1
                ряды.append({"signature": s_, "slot": т.get("slot"), "час_utc": time.gmtime(bt).tm_hour if bt else None,
                             "исход": "успех" if meta.get("err") is None else S.чисто(json.dumps(meta.get("err")))[:100],
                             "место": м, "вызвал": родитель, "байт0": d[:1].hex(), "disc8": d[:8].hex(),
                             "длина_данных": len(d), "счетов": len(сч), "данные_hex": d[:64].hex(),
                             "логи": (логи[k] if k < len(логи) else {})})
                k += 1
    return {"подписей": len(сп), "ряды": ряды, "частые_счета": [а for а, _ in владельцы.most_common(100)]}


def журнал(логи: list, программа: str) -> list:
    """По вызовам программы (в порядке появления): строки «Program log:» внутри, CU, вложенные вызовы."""
    из_, стек = [], []
    for л in логи:
        m = re.match(r"Program (\w+) invoke \[(\d+)\]", л)
        if m:
            if m.group(1) == программа:
                из_.append({"лог": [], "cpi": [], "cu": None})
                стек.append(len(из_) - 1)
            else:
                if стек and стек[-1] is not None:
                    из_[стек[-1]]["cpi"].append(m.group(1))
                стек.append(None)
            continue
        m = re.match(r"Program (\w+) consumed (\d+) of", л)
        if m and m.group(1) == программа and стек and стек[-1] is not None:
            из_[стек[-1]]["cu"] = int(m.group(2))
            continue
        if re.match(r"Program \w+ (success|failed)", л):
            if стек:
                стек.pop()
            continue
        if стек and стек[-1] is not None and (л.startswith("Program log: ") or л.startswith("Program data: ")):
            if len(из_[стек[-1]]["лог"]) < 12:
                из_[стек[-1]]["лог"].append(л[:200])
    return из_


def свод(р: list) -> dict:
    сч = collections.Counter
    имена = сч()
    for x in р:
        for л in (x.get("логи") or {}).get("лог") or []:
            m = re.match(r"Program log: Instruction: (\w+)", л)
            if m:
                имена[m.group(1)] += 1
    return {"вызовов": len(р), "внешних": sum(1 for x in р if x["место"].startswith("внешняя")),
            "кто_вызвал": dict(сч(x["вызвал"] for x in р if x["вызвал"]).most_common(10)),
            "байт0": dict(сч(x["байт0"] for x in р).most_common(20)),
            "disc8": dict(сч(x["disc8"] for x in р).most_common(20)),
            "длина_данных": dict(сч(x["длина_данных"] for x in р).most_common(10)),
            "счетов": dict(сч(x["счетов"] for x in р).most_common(10)),
            "исход": dict(сч("успех" if x["исход"] == "успех" else "ошибка" for x in р)),
            "ошибки": dict(сч(x["исход"] for x in р if x["исход"] != "успех").most_common(8)),
            "час_utc": dict(sorted(сч(x["час_utc"] for x in р if x["час_utc"] is not None).items())),
            "cpi": dict(сч(c for x in р for c in (x.get("логи") or {}).get("cpi") or []).most_common(12)),
            "anchor_имена_в_логах": dict(имена.most_common(30))}


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--programmy", nargs="+", required=True)
    р.add_argument("--podpisey", type=int, default=300)
    а = р.parse_args()
    уз = S.Узел()
    out = КОРЕНЬ / "data" / "podbivka" / "opoznanie_programm.json"
    import podbivka_run as R  # noqa: PLC0415
    итог: dict = {"программы": {}}

    def записать():
        итог["расход"] = уз.расход()
        out.write_text(json.dumps(итог, ensure_ascii=False), encoding="utf-8")
        R.записано(out)
    with уз.на("helius"):
        for пр in а.programmy:
            з: dict = {}
            итог["программы"][пр] = з
            try:
                v = счёт(уз, пр, "jsonParsed")
                з["счёт"] = {"есть": bool(v), "владелец": (v or {}).get("owner"), "executable": (v or {}).get("executable")}
                pd = ((((v or {}).get("data") or {}) if isinstance((v or {}).get("data"), dict) else {}).get("parsed") or {}).get("info", {}).get("programData")
                з["счёт"]["programData"] = pd
                if pd:
                    pdi = счёт(уз, pd, "jsonParsed", {"offset": 0, "length": 0})
                    info = ((((pdi or {}).get("data") or {}).get("parsed") or {}).get("info") or {})
                    з["версия"] = {"слот_развёртывания": info.get("slot"), "upgrade_authority": info.get("authority")}
                    if info.get("slot"):
                        бт = уз.вызов("getBlockTime", [info["slot"]])
                        з["версия"]["время_развёртывания_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(бт)) if бт else None
                    ua = info.get("authority")
                    if ua:
                        va = счёт(уз, ua, "base64", {"offset": 0, "length": 0})
                        з["версия"]["authority_владелец"] = (va or {}).get("owner")
                        з["версия"]["authority_обычный_кошелёк"] = (va or {}).get("owner") in (None, СИСТЕМА)
                    ист = уз.подписи(pd, limit=1000)
                    з["история_programData"] = {"подписей": len(ист),
                                                "первая_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ист[-1]["blockTime"])) if ист and ист[-1].get("blockTime") else None,
                                                "последняя_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ист[0]["blockTime"])) if ист and ист[0].get("blockTime") else None,
                                                "упёрлись_в_предел": len(ист) >= 1000}
                    полный = счёт(уз, pd)
                    if полный:
                        сырые = base64.b64decode(полный["data"][0])
                        elf = сырые[45:]
                        з["elf"] = {"длина_programData": len(сырые), "sha256_кода": hashlib.sha256(elf.rstrip(b"\x00")).hexdigest(),
                                    **строки_elf(elf)}
            except RuntimeError as exc:
                з["why_not_счёт"] = S.чисто(str(exc))[:160]
            try:
                з["idl_anchor"] = idl_anchor(уз, пр)
            except RuntimeError as exc:
                з["idl_anchor"] = {"why_not": S.чисто(str(exc))[:160]}
            з["program_metadata"] = метаданные(уз, пр)
            try:
                в = вызовы(уз, пр, а.podpisey)
                з["вызовы"] = {"подписей": в["подписей"], "свод": свод(в["ряды"]), "ряды": в["ряды"][:400]}
                имена = set(свод(в["ряды"])["anchor_имена_в_логах"]) | set((з.get("elf") or {}).get("anchor_instruction") or [])
                карта = {hashlib.sha256(f"global:{_змея(n)}".encode()).digest()[:8].hex(): n for n in имена}
                з["вызовы"]["anchor_сверка"] = {d8: карта.get(d8) for d8 in з["вызовы"]["свод"]["disc8"]}
                сч_ = в["частые_счета"]
                if сч_:
                    r = уз.вызов("getMultipleAccounts", [сч_, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 8},
                                                               "commitment": "confirmed"}])
                    своих = []
                    for а_, x in zip(сч_, (r or {}).get("value") or []):
                        if x and x.get("owner") == пр:
                            своих.append({"адрес": а_, "disc8": base64.b64decode(x["data"][0]).hex(), "space": x.get("space")})
                    з["счета_программы"] = своих
                    имена_сч = (з.get("idl_anchor") or {}).get("счета") or []
                    ка = {hashlib.sha256(f"account:{n}".encode()).digest()[:8].hex(): n for n in имена_сч}
                    з["счета_программы_виды"] = dict(collections.Counter(
                        f"{x['disc8']}/{x['space']}/{ка.get(x['disc8'], '?')}" for x in своих))
            except RuntimeError as exc:
                з["вызовы"] = {"why_not": S.чисто(str(exc))[:160]}
            записать()
            print(пр[:8], json.dumps({к: з.get(к) for к in ("счёт", "версия")}, ensure_ascii=False)[:400], flush=True)
    записать()
    R.пуш("Podbivka-2: opoznanie programm [automated]", [str(out)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
