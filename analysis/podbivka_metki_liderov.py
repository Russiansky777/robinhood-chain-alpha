#!/usr/bin/env python3
"""Метки лидеров «BAM / Harmonic» для свода зонда шредов (analysis/podbivka_zond_deshred.py --metki-liderov). Только чтение.

Источники -- только наблюдаемые, без списков по памяти:
  1. Jito BAM Boost API: GET https://kobe.mainnet.jito.network/api/v1/bam_boost_validators?epoch=E (документация Jito
     «BAM Boost API»): валидаторы, получившие BAM Boost в эпохе E, поле identity_account. Это получатели boost, а не
     обязательно все валидаторы с BAM -- метка «BAM (boost)».
  2. Наши журналы преконфов Triton (data/triton/preconfs_bam_*.jsonl, preconfs_harmonic_*.jsonl, 25–26.09): слоты, по
     которым поток BAM отдал транзакции / поток Harmonic отдал рамки слота; лидер слота -- getSlotLeaders (Helius).
     Harmonic виден только в окне нашего теста (~2 ч) -- список неполный.
Выход: data/podbivka/metki_liderov.json: {"по_лидеру": {identity: "BAM" | "Harmonic" | "BAM+Harmonic"}, "откуда": …}.
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
JITO = "https://kobe.mainnet.jito.network/api/v1/bam_boost_validators?epoch={}"


def слоты_журналов(шаблон: str, виды: tuple) -> set:
    слоты = set()
    for f in glob.glob(str(КОРЕНЬ / "data" / "triton" / шаблон)):
        for с in open(f, encoding="utf-8"):
            try:
                д = json.loads(с)
            except ValueError:
                continue
            if д.get("kind") in виды and д.get("slot"):
                слоты.add(int(д["slot"]))
    return слоты


def лидеры_слотов(уз, слоты: set) -> tuple:
    из_, ошибки = {}, []
    for с in sorted(слоты):
        if с in из_:
            continue
        try:
            р = уз.вызов("getSlotLeaders", [с, 1])
            if р:
                из_[с] = р[0]
        except RuntimeError as exc:
            ошибки.append(S.чисто(str(exc))[:120])
            if len(ошибки) > 5:
                break
    return из_, ошибки


def main() -> int:
    import requests  # noqa: PLC0415
    уз = S.Узел()
    итог: dict = {"откуда": {}, "по_лидеру": {}}
    with уз.на("helius"):
        эп = (уз.вызов("getEpochInfo", []) or {}).get("epoch")
        итог["эпоха_сейчас"] = эп
        bam: dict = {}
        for e in ([эп, эп - 1, эп - 2] if эп else []):
            try:
                о = requests.get(JITO.format(e), timeout=30)
                д = о.json() if о.status_code == 200 else {}
                сп = д.get("bam_boost_validators") or []
                ид = [v.get("identity_account") for v in сп if isinstance(v, dict) and v.get("identity_account")]
                итог["откуда"][f"jito_bam_boost_epoch_{e}"] = {"http": о.status_code, "валидаторов": len(ид),
                                                              "поля_первого": sorted(сп[0]) if сп and isinstance(сп[0], dict) else None}
                for и in ид:
                    bam.setdefault(и, set()).add(f"boost {e}")
            except Exception as exc:  # noqa: BLE001
                итог["откуда"][f"jito_bam_boost_epoch_{e}"] = {"why_not": S.чисто(f"{type(exc).__name__}: {exc}")[:160]}
        сл_bam = слоты_журналов("preconfs_bam_*.jsonl", ("transaction",))
        сл_h = слоты_журналов("preconfs_harmonic_*.jsonl", ("slot_start", "slot_end", "transaction"))
        л_bam, ош1 = лидеры_слотов(уз, сл_bam)
        л_h, ош2 = лидеры_слотов(уз, сл_h)
        итог["откуда"]["журнал_bam"] = {"слотов": len(сл_bam), "лидеров_найдено": len(set(л_bam.values())), "ошибки": ош1}
        итог["откуда"]["журнал_harmonic"] = {"слотов": len(сл_h), "лидеров_найдено": len(set(л_h.values())), "ошибки": ош2}
        for и in set(л_bam.values()):
            bam.setdefault(и, set()).add("журнал BAM")
        harm = set(л_h.values())
    for и in set(bam) | harm:
        итог["по_лидеру"][и] = "BAM+Harmonic" if и in bam and и in harm else ("BAM" if и in bam else "Harmonic")
    итог["доказательства"] = {и: sorted(bam.get(и, set())) + (["журнал Harmonic"] if и in harm else []) for и in итог["по_лидеру"]}
    итог["расход"] = уз.расход()
    out = КОРЕНЬ / "data" / "podbivka" / "metki_liderov.json"
    out.write_text(json.dumps(итог, ensure_ascii=False, indent=1), encoding="utf-8")
    import podbivka_run as R  # noqa: PLC0415
    R.записано(out)
    print(json.dumps({к: v for к, v in итог.items() if к not in ("по_лидеру", "доказательства")}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
