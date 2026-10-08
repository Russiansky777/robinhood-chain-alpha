#!/usr/bin/env python3
"""Живые сделки полосы по цепи: один загрузчик для всех подбивок (только чтение).

Откуда. Файлы сделок пишет Code-1 на своей ветке (`data/sdelki_polosy_<дата>.json`,
`data/sdelki_polosy_vse_s_2709.json`); сюда они попадают выгрузкой `git show` в черновик
сессии -- см. `--papka`. Ряд сделки несёт и наш фактический вход (слот, место в блоке,
билет, посадка S+k), и итог по цепи, и разбор расхода -- этого хватает и на отрезки, и на
калибровку модели архива.

Правила, которые здесь зашиты (они уже стоили ошибок, поэтому в одном месте):
  * итог сделки -- кириллическое `итог_po_cepi_sol`; если его нет -- `итог_sol` с пометкой
    «из полей». Латинское `itog_po_cepi_sol` -- это ВОЗВРАТ от продажи, не итог;
  * вложенное -- `size_sol` (сколько SOL реально ушло в покупку), а НЕ билет группы
    (`bilet_sol`): при гибком билете и при обрезке по балансу они расходятся;
  * дубли по `cid`: окна файлов перекрываются, оставляем самый свежий снимок, у которого
    есть итог по цепи;
  * сутки -- по Мадриду на время покупки (так считает владелец), UTC -- для отрезков.
Только чтение.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
from pathlib import Path

МАДРИД = dt.timezone(dt.timedelta(hours=2))
ЧЕРНОВИК = Path(os.environ.get("PODB_C1") or "/tmp/c1")


def _ч(x):
    return float(x) if isinstance(x, (int, float)) else None


def сутки_мадрид(utc: str) -> str | None:
    if not utc:
        return None
    try:
        t = dt.datetime.strptime(utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    return t.astimezone(МАДРИД).strftime("%Y-%m-%d")


def итог_сделки(р: dict) -> tuple[float | None, str]:
    """Итог сделки в SOL и откуда он взят. Латинское itog_po_cepi_sol -- возврат, не итог."""
    v = р.get("итог_po_cepi_sol")
    if v is not None:
        return float(v), "цепь"
    v = р.get("итог_sol")
    if v is not None:
        return float(v), "из полей"
    return None, "нет"


ПОЛЯ = ("cid", "utc", "group", "source", "source_name", "mint", "token_name", "pool_program",
        "size_sol", "s_plus", "s_plus_why_not", "our_slot", "landed_slot", "source_slot",
        "our_block_index", "source_block_index", "our_block_total", "our_block_share",
        "hold_slots_plan", "hold_slots_fact", "hold_s_fact", "slot_len_at_buy_s",
        "state", "closed_reason", "chain_ok", "closed_sol_net", "возврат_sol",
        "расход_sol", "чаевые_sol", "приоритет_sol", "тариф_sol", "комиссия_sol",
        "рента_sol", "renta_zaperta_sol", "завёрнутое_sol", "bilet_sol", "nash_bilet_sol",
        "билет_sol", "bilet_ot_rezerva", "bilet_rezerv_sol", "pool_reserve_sol",
        "pool_reserve_kind", "nalog_tokena_bps", "nalog_marshruta_bps", "komissiya_pula_pct",
        "chuzhie_prodazhi_sol", "chuzhih_prodazh", "source_sig", "buy_sig", "sell_sig",
        "сходится", "sverka_ok", "итог_po_polyam_sol", "итог_po_cepi_chasti",
        "итог_po_cepi_pochemu_net")


def сделка(р: dict, файл: str, снято: str) -> dict:
    итог, откуда = итог_сделки(р)
    из_ = {k: р.get(k) for k in ПОЛЯ}
    из_.update({"итог": итог, "итог_откуда": откуда, "файл": файл, "снято": снято,
                "сутки": сутки_мадрид(р.get("utc") or ""),
                "вложено": _ч(р.get("size_sol"))})
    return из_


def сделки(папка: Path | None = None, s: str = "", do: str = "") -> list:
    """Все живые сделки из файлов Code-1 в папке; дубли по cid сняты. s/do -- рамки по utc."""
    п = папка or ЧЕРНОВИК
    файлы = sorted(glob.glob(str(Path(п) / "sdelki_polosy_*.json")))
    if not файлы:
        raise SystemExit(f"нет файлов сделок в {п}")
    из_: dict = {}
    for f in файлы:
        try:
            д = json.loads(Path(f).read_text(encoding="utf-8"))
        except ValueError:
            continue
        снято = д.get("снято_utc") or ""
        for р in д.get("ряды") or []:
            cid = р.get("cid")
            utc = р.get("utc") or ""
            if not cid or not utc:
                continue
            if s and utc < s:
                continue
            if do and utc >= do:
                continue
            с = сделка(р, Path(f).name, снято)
            б = из_.get(cid)
            # свежий снимок с итогом по цепи сильнее старого и сильнее «из полей»
            ключ = (с["итог_откуда"] == "цепь", с["снято"])
            if б is None or ключ > (б["итог_откуда"] == "цепь", б["снято"]):
                из_[cid] = с
    return sorted(из_.values(), key=lambda x: x["utc"])


if __name__ == "__main__":
    с = сделки()
    print(f"сделок {len(с)}: {с[0]['utc']} .. {с[-1]['utc']}, "
          f"по цепи {sum(1 for x in с if x['итог_откуда'] == 'цепь')}, "
          f"групп {len({x['group'] for x in с})}")
