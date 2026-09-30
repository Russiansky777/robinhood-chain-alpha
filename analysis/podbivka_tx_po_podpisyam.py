#!/usr/bin/env python3
"""Чтение уже состоявшихся транзакций по списку подписей (только чтение, Helius) -- без нового сбора состояния.

Режимы:
  --teni      подписи 276 строк data/teni_ne_sobrany_po_programmam.json ветки Code-1 (копия в
              data/podbivka/teni_vhod.json) -> data/podbivka/teni_tx.json.gz (транзакции целиком, jsonParsed);
  --dbc       подписи 55 сделок источников data/podbivka/dbc_sbor.json -> data/podbivka/dbc_ist_tx.json.gz;
  --okno      две сделки 0.3 SOL (data/podbivka/dve_sdelki_vhod.json): все успешные транзакции минта и хранилища пула
              от слота нашей покупки до слота нашей продажи (+2) -> data/podbivka/dve_sdelki_tx.json.gz.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_sim as S  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"


def скачать(уз, подписи: list) -> dict:
    из_ = {}
    for и in range(0, len(подписи), 50):
        из_.update(уз.пакет(подписи[и:и + 50]))
    return из_


def окно(уз, адрес: str, от_слота: int, до_слота: int) -> list:
    """Подписи адреса со слотом в [от_слота, до_слота], от новых к старым, постранично."""
    сп, до = [], None
    while True:
        стр = уз.подписи(адрес, до=до, limit=1000)
        if not стр:
            break
        сп += [з for з in стр if от_слота <= (з.get("slot") or 0) <= до_слота]
        if (стр[-1].get("slot") or 0) < от_слота or len(стр) < 1000:
            break
        до = стр[-1]["signature"]
    return сп


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--teni", action="store_true")
    р.add_argument("--dbc", action="store_true")
    р.add_argument("--okno", action="store_true")
    а = р.parse_args()
    уз = S.Узел()
    import podbivka_run as R  # noqa: PLC0415
    with уз.на("helius"):
        if а.teni:
            строки = json.loads((П / "teni_vhod.json").read_text(encoding="utf-8"))["строки"]
            txs = скачать(уз, sorted({с["podpis"] for с in строки}))
            out = П / "teni_tx.json.gz"
        elif а.dbc:
            сд = json.loads((П / "dbc_sbor.json").read_text(encoding="utf-8"))["сделки"]
            txs = скачать(уз, sorted({с["signature"] for с in сд}))
            out = П / "dbc_ist_tx.json.gz"
        else:
            вход = json.loads((П / "dve_sdelki_vhod.json").read_text(encoding="utf-8"))["сделки"]
            txs, окна = {}, {}
            for с in вход:
                пара = скачать(уз, [с["buy_sig"], с["sell_sig"]])
                txs.update(пара)
                s0 = (пара.get(с["buy_sig"]) or {}).get("slot")
                s1 = (пара.get(с["sell_sig"]) or {}).get("slot")
                if not (s0 and s1):
                    окна[с["mint"]] = {"why_not": "покупка или продажа не прочитана"}
                    continue
                подп = {}
                for адрес in (с["mint"], с["pool_vault"]):
                    for з in окно(уз, адрес, s0, s1 + 2):
                        подп[з["signature"]] = з
                окна[с["mint"]] = {"slot_покупки": s0, "slot_продажи": s1, "подписей": len(подп),
                                   "упавших": sum(1 for з in подп.values() if з.get("err") is not None)}
                txs.update(скачать(уз, sorted(з for з, v in подп.items() if v.get("err") is None)))
            txs = {"окна": окна, "транзакции": txs}
            out = П / "dve_sdelki_tx.json.gz"
    тело = txs if isinstance(txs, dict) and "окна" in txs else {"транзакции": txs}
    тело["расход"] = уз.расход()
    out.write_bytes(gzip.compress(json.dumps(тело, ensure_ascii=False).encode()))
    R.записано(out)
    print(out.name, len(тело["транзакции"]), "транзакций", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
