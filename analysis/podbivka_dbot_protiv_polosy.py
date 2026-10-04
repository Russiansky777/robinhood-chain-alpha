#!/usr/bin/env python3
"""DBot против полосы на одних и тех же источниках (добавка владельца 04.10). Только чтение.

Вопрос владельца: кто исполняет источники BATCH-5 и BATCH-3 лучше -- задачи DBot или наша
полоса (группы `batch5` и `lane_s0`), и стоит ли переводить баланс DBot (~4.9 SOL по слову
владельца) в полосу. Решение -- владельца, здесь только числа.

ОКНО. Общее: с 27.09 00:00Z до конца, который есть у ОБЕИХ сторон -- берётся
min(последняя закрытая покупка DBot, последняя покупка полосы). Обе стороны режутся одним и
тем же окном, иначе сравнение нечестное.

СТОРОНА DBot. `data/solana_trades_all.json` с ветки Code-1 (копия в
`data/podbivka/dbot/`): задачи BATCH-5 и BATCH-3, статусы «закрыта» и «ручная продажа»
(у «срыва» итога нет вовсе -- он считается отдельным столбцом). Итог сделки -- `net_sol`,
вложено -- `sol_in`. Числа этого файла сходятся с документом Code-1
`docs/uchet_batch5_batch3_s_2309.md` до лампорта на его окне 23.09 -- 03.10 14:00Z
(107 сделок +1.634701141 и 108 сделок +1.476988353), поэтому своих поправок здесь нет.

СТОРОНА ПОЛОСЫ. Выгрузки Code-1: `sdelki_polosy_vse_s_2709.json` (578 строк, 27.09 -- 03.10
06:00) плюс суточные файлы после неё, склейка по `cid`. Группы `batch5` и `lane_s0`. Итог
сделки -- `podbivka_nedelya_bilet.итог_сделки` (кириллическое `итог_po_cepi_sol`, иначе
`итог_sol` с отметкой «из полей»; латинское `itog_po_cepi_sol` -- возврат и не берётся).
Источник -- поле `source` нашего журнала (слово владельца 04.10: «ближайший покупатель перед
нами» ошибается). Строки без `source` считаются отдельно и в таблицу источников не попадают.

Выход: `data/podbivka/dbot_protiv_polosy.json` и раздел
`data/podbivka/dbot_protiv_polosy_razdel.md` (его подклеивает понедельничный документ).
"""
from __future__ import annotations

import argparse
import collections
import datetime as дт
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_cand2 as C2  # noqa: E402
import podbivka_nedelya_bilet as N  # noqa: E402

КОРЕНЬ = C2.КОРЕНЬ
П = C2.П
ЗАДАЧИ = ("BATCH-5", "BATCH-3")
НАШИ_ГРУППЫ = ("batch5", "lane_s0")
ЗАКРЫТЫЕ = ("закрыта", "ручная продажа")
НАЧАЛО = "2026-09-27T00:00:00Z"


def ts(с: str) -> float:
    return дт.datetime.strptime(с, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=дт.timezone.utc).timestamp()


def utc(t: float) -> str:
    return дт.datetime.fromtimestamp(t, дт.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def стат(итоги: list, вложено: float | None = None) -> dict:
    """n, итог, медиана, доля в плюсе; п.п. -- итог на вложенный SOL, если вложенное дано."""
    if not итоги:
        return {"n": 0}
    с = round(sum(итоги), 9)
    д = {"n": len(итоги), "итог_sol": с, "медиана_sol": round(statistics.median(итоги), 9),
         "в_плюсе": round(100 * sum(1 for x in итоги if x > 0) / len(итоги)), }
    if вложено:
        д["вложено_sol"] = round(вложено, 6)
        д["пп"] = round(100 * с / вложено, 2)
    return д


def dbot_сделки(файл: Path) -> tuple[list, list]:
    """Закрытые сделки BATCH-5/BATCH-3 и отдельно срывы (у них итога нет)."""
    ряды = json.loads(файл.read_text(encoding="utf-8"))
    закрытые, срывы = [], []
    for x in ряды:
        if x.get("task_name") not in ЗАДАЧИ:
            continue
        if x.get("status") in ЗАКРЫТЫЕ and x.get("net_sol") is not None and x.get("buy_block_time"):
            закрытые.append(x)
        elif x.get("status") == "срыв":
            срывы.append(x)      # времени у срыва в учёте нет вовсе -- окном не режется
    return закрытые, срывы


def полоса_сделки() -> tuple[list, list, dict]:
    """Сделки полосы групп batch5 / lane_s0: (с источником, без источника, откуда взято)."""
    по_cid: dict = {}
    откуда: dict = {"файлы": []}
    для_склейки = []
    for к in (КОРЕНЬ / "data" / "sdelki_polosy_vse_s_2709.json", П / "sdelki" / "vse_s_2709.json"):
        if к.exists():
            для_склейки.append(к)
            break
    для_склейки += sorted((П / "sdelki").glob("sdelki_polosy_*.json"))
    для_склейки += sorted((П / "mesto").glob("vhod_*.json"))
    for f in для_склейки:
        try:
            д = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        ряды = д.get("ряды") if isinstance(д, dict) else д
        if not ряды:
            continue
        откуда["файлы"].append({"файл": f.name, "рядов": len(ряды)})
        for r in ряды:
            if r.get("cid"):
                по_cid[r["cid"]] = r
    с_ист, без_ист = [], []
    for r in по_cid.values():
        if r.get("group") not in НАШИ_ГРУППЫ:
            continue
        и, из_ = N.итог_сделки(r)
        if и is None or not r.get("utc"):
            continue
        r = dict(r)
        r["_итог"], r["_из"] = и, из_
        (с_ист if r.get("source") else без_ист).append(r)
    откуда["cid_всего"] = len(по_cid)
    return с_ист, без_ист, откуда


def main() -> int:
    import podbivka_run as R  # noqa: PLC0415
    р = argparse.ArgumentParser()
    р.add_argument("--metka", default="")
    р.add_argument("--dbot", default=str(П / "dbot" / "solana_trades_all_0410.json"))
    а = р.parse_args()

    закрытые, срывы = dbot_сделки(Path(а.dbot))
    полоса, полоса_без, откуда = полоса_сделки()
    if not закрытые or not полоса:
        print(f"нечего сравнивать: DBot {len(закрытые)}, полоса {len(полоса)}", flush=True)
        return 1

    # ---- общее окно: с 27.09 до конца, который есть у обеих сторон
    н = ts(НАЧАЛО)
    к_dbot = max(x["buy_block_time"] for x in закрытые)
    к_пол = max(ts(r["utc"]) for r in полоса)
    к = min(к_dbot, к_пол)
    окно = {"с": utc(н), "до": utc(к), "конец_dbot": utc(к_dbot), "конец_polosy": utc(к_пол),
            "суток": round((к - н) / 86400, 2)}

    закрытые = [x for x in закрытые if н <= x["buy_block_time"] <= к]
    полоса = [r for r in полоса if н <= ts(r["utc"]) <= к]
    полоса_без = [r for r in полоса_без if н <= ts(r["utc"]) <= к]

    # ---- по источникам
    d_по: dict = collections.defaultdict(list)
    d_вл: dict = collections.defaultdict(float)
    d_задача: dict = collections.defaultdict(set)
    for x in закрытые:
        a = x["source_address"]
        d_по[a].append(float(x["net_sol"]))
        d_вл[a] += float(x.get("sol_in") or 0)
        d_задача[a].add(x["task_name"])
    d_срывы = collections.Counter(x["source_address"] for x in срывы)      # без окна: времени нет
    причины_срывов = collections.Counter(x.get("dbot_fail_reason") for x in срывы)

    п_по: dict = collections.defaultdict(list)
    п_вл: dict = collections.defaultdict(float)
    п_группа: dict = collections.defaultdict(set)
    п_поля: dict = collections.Counter()
    for r in полоса:
        a = r["source"]
        п_по[a].append(float(r["_итог"]))
        п_вл[a] += float(r.get("sol_in") or 0)
        п_группа[a].add(r["group"])
        if r["_из"] == "поля":
            п_поля[a] += 1

    адр = json.loads((П / "arhiv_adresa.json").read_text(encoding="utf-8"))["адреса"]
    ряды = []
    for a in sorted(set(d_по) | set(п_по)):
        ряды.append({
            "источник": a,
            "имя": (адр.get(a) or {}).get("имя") or (адр.get(a) or {}).get("name"),
            "dbot": {**стат(d_по.get(a, []), d_вл.get(a)),
                     "задачи": sorted(d_задача.get(a, ())),
                     "срывов_за_всё_время": d_срывы.get(a, 0)},
            "polosa": {**стат(п_по.get(a, []), п_вл.get(a)),
                       "группы": sorted(п_группа.get(a, ())), "из_полей": п_поля.get(a, 0)},
        })

    общие = [р_ for р_ in ряды if р_["dbot"].get("n") and р_["polosa"].get("n")]
    свод = {
        "dbot_всё": стат([v for a in d_по for v in d_по[a]], sum(d_вл.values())),
        "dbot_срывов_за_всё_время": len(срывы),
        "dbot_причины_срывов": dict(причины_срывов.most_common()),
        "polosa_всё": {**стат([v for a in п_по for v in п_по[a]], sum(п_вл.values())),
                       "из_полей": sum(п_поля.values())},
        "dbot_общие_источники": стат([v for р_ in общие for v in d_по[р_["источник"]]],
                                     sum(d_вл[р_["источник"]] for р_ in общие)),
        "polosa_общие_источники": стат([v for р_ in общие for v in п_по[р_["источник"]]],
                                       sum(п_вл[р_["источник"]] for р_ in общие)),
        "общих_источников": len(общие),
        "только_dbot": sorted(a for a in d_по if a not in п_по),
        "только_polosa": sorted(a for a in п_по if a not in d_по),
        "polosa_bez_istochnika": стат([float(r["_итог"]) for r in полоса_без],
                                      sum(float(r.get("sol_in") or 0) for r in полоса_без)),
    }
    for к_, v in (("dbot_всё", свод["dbot_всё"]), ("polosa_всё", свод["polosa_всё"]),
                  ("dbot_общие_источники", свод["dbot_общие_источники"]),
                  ("polosa_общие_источники", свод["polosa_общие_источники"])):
        if v.get("n"):
            v["sol_в_сутки"] = round(v["итог_sol"] / окно["суток"], 6)

    итог = {"что": "DBot (BATCH-5, BATCH-3) против полосы (batch5, lane_s0) на одних источниках",
            "окно": окно, "источники_выгрузок": откуда, "ряды": ряды, "свод": свод}
    оut = П / "dbot_protiv_polosy.json"
    оut.write_text(json.dumps(итог, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(оut)

    # ---- раздел для понедельничного документа
    def ф(v: dict, ключ: str, формат: str = "+.4f") -> str:
        return "—" if v.get(ключ) is None else format(v[ключ], формат)

    md = ["## DBot против полосы на одних и тех же источниках", "",
          f"Окно общее: **{окно['с']} -- {окно['до']}** ({окно['суток']} суток). Конец -- по той "
          f"стороне, где данные кончаются раньше (DBot до {окно['конец_dbot']}, "
          f"полоса до {окно['конец_polosy']}); обе стороны режутся одним окном.", "",
          f"Сторона DBot -- задачи BATCH-5 и BATCH-3, только закрытые сделки "
          f"({свод['dbot_всё'].get('n', 0)}). Сторона полосы -- группы `batch5` и `lane_s0` "
          f"({свод['polosa_всё'].get('n', 0)} сделок), итог сделки по цепи, у "
          f"{свод['polosa_всё'].get('из_полей', 0)} строк кириллического поля нет -- взят "
          f"`итог_sol` («из полей»). Строк полосы без источника в журнале: "
          f"{свод['polosa_bez_istochnika'].get('n', 0)}.", "",
          "| источник | DBot: задача | n | итог SOL | медиана | в плюсе | вложено | п.п. "
          "| полоса: группа | n | итог SOL | медиана | в плюсе | вложено | п.п. | из полей |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for р_ in sorted(ряды, key=lambda r: -((r["dbot"].get("итог_sol") or 0)
                                           + (r["polosa"].get("итог_sol") or 0))):
        d, p = р_["dbot"], р_["polosa"]
        md.append(
            f"| `{р_['источник'][:8]}`{(' ' + р_['имя']) if р_.get('имя') else ''} "
            f"| {', '.join(d.get('задачи') or []) or '—'} | {d.get('n') or 0} | {ф(d, 'итог_sol')} "
            f"| {ф(d, 'медиана_sol', '+.6f')} | {ф(d, 'в_плюсе', 'd') if d.get('n') else '—'}"
            f"{'%' if d.get('n') else ''} | {ф(d, 'вложено_sol', '.4f')} | {ф(d, 'пп', '+.2f')} "
            f"| {d.get('срывов') or 0} "
            f"| {', '.join(p.get('группы') or []) or '—'} | {p.get('n') or 0} | {ф(p, 'итог_sol')} "
            f"| {ф(p, 'медиана_sol', '+.6f')} | {ф(p, 'в_плюсе', 'd') if p.get('n') else '—'}"
            f"{'%' if p.get('n') else ''} | {ф(p, 'вложено_sol', '.4f')} | {ф(p, 'пп', '+.2f')} "
            f"| {p.get('из_полей') or 0} |")

    def строка_свода(имя: str, v: dict) -> str:
        return (f"| {имя} | {v.get('n') or 0} | {ф(v, 'итог_sol')} | {ф(v, 'медиана_sol', '+.6f')} "
                f"| {ф(v, 'в_плюсе', 'd') if v.get('n') else '—'}{'%' if v.get('n') else ''} "
                f"| {ф(v, 'вложено_sol', '.4f')} | {ф(v, 'пп', '+.2f')} "
                f"| {ф(v, 'sol_в_сутки', '+.6f')} |")

    md += ["", f"### Свод (общих источников {свод['общих_источников']})", "",
           "| сторона | n | итог SOL | медиана | в плюсе | вложено | п.п. | SOL в сутки |",
           "|---|---|---|---|---|---|---|---|",
           строка_свода("DBot, все его источники", свод["dbot_всё"]),
           строка_свода("Полоса, все её источники", свод["polosa_всё"]),
           строка_свода("DBot, только общие источники", свод["dbot_общие_источники"]),
           строка_свода("Полоса, только общие источники", свод["polosa_общие_источники"]), "",
           f"Срывов (сигнал пришёл, сделки не вышло) у BATCH-5 и BATCH-3 за всё время учёта -- "
           f"{len(срывы)}; времени в этих записях нет вовсе, поэтому окном они не режутся и в "
           f"таблицу выше не входят. Причины: "
           + ", ".join(f"{к_ or 'без причины'} {в_}" for к_, в_ in причины_срывов.most_common(6))
           + ".", "",
           f"Только у DBot: {len(свод['только_dbot'])} источников; только у полосы: "
           f"{len(свод['только_polosa'])}. Баланс DBot (~4.9 SOL) -- число владельца, по цепи "
           f"здесь не проверялось. Ничего не рекомендуется -- решает владелец.", ""]
    оut_m = П / "dbot_protiv_polosy_razdel.md"
    оut_m.write_text("\n".join(md) + "\n", encoding="utf-8")
    R.записано(оut_m)
    if а.metka:
        стр = КОРЕНЬ / "docs" / f"podbivka_{а.metka}_dbot_protiv_polosy.md"
        стр.write_text("\n".join(md) + "\n", encoding="utf-8")
        R.записано(стр)
    print(f"окно {окно['с']} -- {окно['до']}: DBot {свод['dbot_всё'].get('n')} сделок "
          f"{свод['dbot_всё'].get('итог_sol'):+.6f}, полоса {свод['polosa_всё'].get('n')} сделок "
          f"{свод['polosa_всё'].get('итог_sol'):+.6f}; общих источников {свод['общих_источников']}",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
