#!/usr/bin/env python3
"""Подбивка: бегунок пакета кошельков (п.2б наши источники, п.3 чужие).

Один кошелёк = один файл data/podbivka/<задача>/<адрес>.json, пишется СРАЗУ
после кошелька (job может умереть по пределу 6 часов -- сделанное не теряется),
и каждые ПУШ_КАЖДЫЕ_С секунд уходит в git. Кошелёк, который не разобрался,
тоже пишется -- с причиной (через чисто()), а не пропадает.

Режим --sverka N: у первых N кошельков число первых покупок пересчитывается
ПРЯМЫМ разбором -- каждая транзакция окна одиночным getTransaction и свой,
независимый от podbivka_sim.разбор_сделки, разбор балансов.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import c2_common as C  # noqa: E402
import podbivka as P  # noqa: E402
import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
ПУШ_КАЖДЫЕ_С = 600


def в_секунды(строка: str) -> int:
    return calendar.timegm(time.strptime(строка, "%Y-%m-%dT%H:%M:%SZ"))


def список(имя: str) -> list:
    if имя == "wallets_csv":
        with open(КОРЕНЬ / "data" / "podbivka" / "wallets.csv", encoding="utf-8") as ф:
            return [{"address": r["address"], "name": r.get("name"), "group": r.get("group"),
                     "rank": r.get("rank")} for r in csv.DictReader(ф) if r.get("address")]
    if имя == "nashi":
        д = json.loads((КОРЕНЬ / "data" / "podbivka" / "istochniki.json").read_text(encoding="utf-8"))
        return [{"address": з["address"], "group": з["группа_п5"], "tasks": з["задачи"],
                 "снят": з["снят"]} for з in д["источники"]]
    raise SystemExit(f"неизвестный список {имя}")


def пуш(сообщение: str, пути: list) -> None:
    """git add/commit/push с повтором; ошибки git не валят прогон."""
    try:
        subprocess.run(["git", "add", *пути], check=False, capture_output=True)
        р = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
        if р.returncode == 0:
            return
        subprocess.run(["git", "commit", "-q", "-m", сообщение], check=False, capture_output=True)
        for _ in range(6):
            if subprocess.run(["git", "push", "-q", "origin", "HEAD:claude/podbivka"],
                              check=False, capture_output=True).returncode == 0:
                return
            # Клон мелкий: догружаем хвост ветки и перекладываем свой коммит
            # поверх. Файлы у пакетов разные, конфликтов по содержимому нет.
            subprocess.run(["git", "fetch", "-q", "--depth=50", "origin", "claude/podbivka"],
                           check=False, capture_output=True)
            if subprocess.run(["git", "rebase", "-q", "FETCH_HEAD"], check=False,
                              capture_output=True).returncode != 0:
                subprocess.run(["git", "rebase", "--abort"], check=False, capture_output=True)
            time.sleep(3)
    except Exception as exc:  # noqa: BLE001
        print("git:", S.чисто(str(exc))[:120])


# ------------------------------------------------------------ прямой разбор (сверка)

def прямой_счёт(уз: S.Узел, кош: str, подписи: list) -> dict:
    """Первые покупки по одиночным getTransaction и своему разбору балансов.

    Независимо от разбор_сделки: баланс минта кошелька до/после -- прямо из
    pre/postTokenBalances по owner; трата -- натив кошелька плюс WSOL/стейбл.
    Считается только ЧИСЛО первых покупок (любого размера) -- это проверка
    отбора и пакетного пути, а не курса.
    """
    первых, разобрано, не_отдал = 0, 0, 0
    for з, bt in подписи:
        try:
            tx = уз.вызов("getTransaction", [з, S.ОПЦИИ_TX], срок=40.0, узел=S.узел_по_времени(bt))
        except RuntimeError:
            tx = None
        if not tx:
            не_отдал += 1
            continue
        разобрано += 1
        мета = tx.get("meta") or {}
        if мета.get("err") is not None:
            continue
        ключи = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
        if not any(isinstance(к, dict) and к.get("pubkey") == кош and к.get("signer") for к in ключи):
            continue
        до: dict = {}
        после: dict = {}
        for имя, куда in (("preTokenBalances", до), ("postTokenBalances", после)):
            for b in мета.get(имя) or []:
                if b.get("owner") != кош:
                    continue
                куда[b["mint"]] = куда.get(b["mint"], 0) + int((b.get("uiTokenAmount") or {}).get("amount") or 0)
        котировки = {C.WSOL, C.USDC, C.USDT}
        выросли = [м for м in после if м not in котировки and после[м] > до.get(м, 0)]
        if not выросли:
            continue
        отдал = any(до.get(м, 0) > после.get(м, 0) for м in котировки)
        pubs = [к.get("pubkey") if isinstance(к, dict) else к for к in ключи]
        if кош in pubs:
            и = pubs.index(кош)
            пре, пост = (мета.get("preBalances") or [0])[и], (мета.get("postBalances") or [0])[и]
            плата = (мета.get("fee") or 0) if и == 0 else 0
            if пре - пост - плата > 0:
                отдал = True
        if not отдал:
            continue
        if any(до.get(м, 0) == 0 for м in выросли):
            первых += 1
    return {"первых_покупок_любого_размера": первых, "разобрано": разобрано, "не_отдал": не_отдал}


# ------------------------------------------------------------ факт по нашим сделкам (п.2а)

class Факт:
    """Наши сделки по источнику с 18.09: DBot (журнал + сырые записи) и Bloom/полоса.

    DBot: готовая покупка в сырых записях (follow.receive.amount -- точное
    число токенов источника) сопоставляется с покупкой источника в цепи, итог
    по цепи -- net_sol из журнала сделок. Bloom/полоса: у позиции есть
    source_sig, итог -- closed_sol_net (chain_ok рядом).
    """

    def __init__(self, с_ts: int, до_ts: int, host_path: str | None) -> None:
        import podbivka_1b2 as Q  # noqa: PLC0415
        self.Q = Q
        все = {з["address"] for з in список("nashi")}
        self.dbot = Q.записи_dbot(КОРЕНЬ / "data" / "dbot_follow_trades_raw.json", все, с_ts, до_ts)
        self.журнал = Q.журнал_сделок(КОРЕНЬ / "data" / "solana_trades_all.json", все)
        self.host: list = []
        if host_path and Path(host_path).exists():
            д = json.loads(Path(host_path).read_text(encoding="utf-8"))
            for с in д.get("сделки") or []:
                т = float(с.get("ts_intent") or 0)
                if с_ts <= т < до_ts and с.get("source") in все:
                    self.host.append(с)

    def по_источнику(self, адрес: str, все_покупки: list) -> list:
        из_ = []
        for з in self.dbot:
            if з["source"] != адрес or з["state"] != "done":
                continue
            п, как = self.Q.сопоставить(з, все_покупки)
            наши = [т for т in self.журнал if т.get("mint") == з["mint"]
                    and т.get("wallet") == з["our_wallet"]
                    and abs((т.get("buy_block_time") or 0) - з["ts"]) <= self.Q.ОКНО_ВРЕМЕНИ_С]
            наша = min(наши, key=lambda т: abs((т.get("buy_block_time") or 0) - з["ts"])) if наши else None
            из_.append({"канал": "DBot", "task": з["task"], "mint": з["mint"], "ts": з["ts"],
                        "source_sig": п["signature"] if п else None, "сопоставлено": как,
                        "наш_sol_in": наша.get("sol_in") if наша else None,
                        "факт_net_sol": наша.get("net_sol") if наша else None,
                        "статус": наша.get("status") if наша else "нет в журнале сделок"})
        for с in self.host:
            if с.get("source") != адрес:
                continue
            из_.append({"канал": "полоса" if с.get("lane") else "Bloom",
                        "task": с.get("lane_group") or с.get("source_task"), "mint": с.get("mint"),
                        "ts": int(float(с.get("ts_intent") or 0)), "source_sig": с.get("source_sig"),
                        "сопоставлено": "source_sig позиции", "наш_sol_in": с.get("sol_in"),
                        "факт_net_sol": с.get("closed_sol_net"), "chain_ok": с.get("chain_ok"),
                        "статус": с.get("state")})
        return из_


# ------------------------------------------------------------ один кошелёк

def кошелёк(уз: S.Узел, курс, строка: dict, с_ts: int, до_ts: int, *, предел_на_порог,
            предел_подписей, сверка: bool, факт: Факт | None = None) -> dict:
    t0 = time.time()
    адрес = строка["address"]
    ск = S.скан_кошелька(уз, адрес, с_ts, до_ts, предел_на_порог=предел_на_порог, курс=курс,
                         предел_подписей=предел_подписей, все_покупки=сверка or факт is not None)
    покупки = []
    for пк in ск["покупки"]:
        if not пк.get("порог"):
            покупки.append({**{к: v for к, v in пк.items() if к != "опора"}, "sim": None})
            continue
        сим = S.симулировать(уз, пк)
        уз._кэш.clear()  # noqa: SLF001
        возраст = S.возраст_токена(уз, пк["mint"], пк["signature"], пк.get("blockTime") or 0)
        покупки.append({**{к: v for к, v in пк.items() if к != "опора"}, "sim": сим,
                        "возраст": возраст})
    сделки_факт = []
    if факт is not None:
        по_сиг = {п["signature"]: п for п in ск["все_покупки"]}
        готовые = {п["signature"]: п.get("sim") for п in покупки if п.get("sim")}
        for ф in факт.по_источнику(адрес, ск["все_покупки"]):
            сиг = ф.get("source_sig")
            if сиг and сиг not in готовые:
                пк = по_сиг.get(сиг) or {"signature": сиг, "slot": None, "mint": ф["mint"]}
                try:
                    готовые[сиг] = S.симулировать(уз, {**пк, "wallet": адрес})
                except Exception as exc:  # noqa: BLE001
                    готовые[сиг] = {"why_not": S.чисто(f"{type(exc).__name__}: {exc}")[:160]}
                уз._кэш.clear()  # noqa: SLF001
            сделки_факт.append({**ф, "sim": готовые.get(сиг) if сиг else None,
                                "первая_от_2": any(п["signature"] == сиг for п in покупки
                                                   if п.get("порог"))})
    из_ = {"строка": строка, "окно": {"с": S.utc(с_ts), "до": S.utc(до_ts)},
           "факт": сделки_факт,
           "скан": {к: v for к, v in ск.items() if к not in ("покупки", "все_покупки", "_подписи_окна")},
           "все_покупки": [{к: п.get(к) for к in ("signature", "slot", "blockTime", "mint", "tokens_raw",
                                                  "первая", "трата_sol", "трата_usd")}
                           for п in ск["все_покупки"]] if факт is not None else None,
           "покупки": покупки, "минут": round((time.time() - t0) / 60, 2),
           "расход_узла": уз.расход()}
    if сверка:
        пакетных_первых = sum(1 for п in ск["все_покупки"] if п["первая"])
        # Те же подписи окна, что прошли пакетный путь, -- теперь по одной.
        подписи = list(ск.get("_подписи_окна") or [])
        прямой = прямой_счёт(уз, адрес, подписи)
        из_["сверка"] = {"пакетный_путь_первых_любого_размера": пакетных_первых, **прямой,
                         "подписей_сверено": len(подписи)}
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--zadacha", required=True, help="каталог в data/podbivka/")
    р.add_argument("--spisok", required=True, choices=("wallets_csv", "nashi"))
    р.add_argument("--s", type=int, default=0, help="с какого номера в списке")
    р.add_argument("--po", type=int, default=0, help="по какой номер (не включая), 0 -- до конца")
    р.add_argument("--do-utc", required=True, help="конец окна (одинаков у всех пакетов)")
    р.add_argument("--dney", type=float, default=7.0)
    р.add_argument("--predel-na-porog", type=int, default=0)
    р.add_argument("--predel-podpisey", type=int, default=0)
    р.add_argument("--sverka", type=int, default=0, help="у первых N кошельков -- прямой разбор")
    р.add_argument("--push", action="store_true")
    р.add_argument("--fakt", action="store_true", help="п.2а: наши сделки DBot/Bloom/полосы")
    р.add_argument("--tolko-shyft", action="store_true",
                   help="ни одного запроса в Helius: всё на Shyft (проход (а) чужих)")
    р.add_argument("--host-sdelki", default=str(КОРЕНЬ / "data" / "podbivka" / "nashi_sdelki_host.json"))
    а = р.parse_args()
    до_ts = в_секунды(а.do_utc)
    if а.tolko_shyft:
        S.ГРАНЬ_SHYFT = 0
    с_ts = int(до_ts - а.dney * 86400)
    строки = список(а.spisok)
    строки = строки[а.s:(а.po or None)]
    каталог = КОРЕНЬ / "data" / "podbivka" / а.zadacha
    каталог.mkdir(parents=True, exist_ok=True)
    уз = S.Узел()
    курс = S.КурсПулом(S.КурсУзла(уз))
    последний_пуш = time.time()
    итог = {"кошельков": len(строки), "готово": 0, "не_разобрались": []}
    факт = Факт(с_ts, до_ts, а.host_sdelki) if а.fakt else None
    for н, строка in enumerate(строки):
        путь = каталог / f"{строка['address']}.json"
        try:
            рез = кошелёк(уз, курс, строка, с_ts, до_ts,
                          предел_на_порог=а.predel_na_porog or None,
                          предел_подписей=а.predel_podpisey or None, сверка=н < а.sverka,
                          факт=факт)
            if рез["скан"].get("why_not"):
                итог["не_разобрались"].append({"address": строка["address"],
                                               "причина": рез["скан"]["why_not"]})
            итог["готово"] += 1
        except Exception as exc:  # noqa: BLE001
            рез = {"строка": строка, "why_not": S.чисто(f"{type(exc).__name__}: {exc}")[:200]}
            итог["не_разобрались"].append({"address": строка["address"], "причина": рез["why_not"]})
        путь.write_text(json.dumps(рез, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{а.zadacha} {н + 1}/{len(строки)}: {строка['address'][:8]} "
              f"покупок {len(рез.get('покупки') or [])} минут {рез.get('минут')} "
              f"{'ОТКАЗ ' + рез['why_not'][:60] if рез.get('why_not') else ''}", flush=True)
        if а.push and time.time() - последний_пуш > ПУШ_КАЖДЫЕ_С:
            пуш(f"Podbivka-2: {а.zadacha} {а.s}+{н + 1} [automated]", [str(каталог)])
            последний_пуш = time.time()
    свод_путь = каталог / f"_svod_{а.s}_{а.po or 'end'}.json"
    свод_путь.write_text(json.dumps({**итог, "расход_узла": уз.расход()}, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    if а.push:
        пуш(f"Podbivka-2: {а.zadacha} {а.s}-{а.po or 'end'} gotovo [automated]", [str(каталог)])
    print(json.dumps(итог, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
