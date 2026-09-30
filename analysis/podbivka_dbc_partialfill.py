#!/usr/bin/env python3
"""DBC: 11 инструкций источников со swap_mode 1 (PartialFill) -- считает ли их порт ExactIn до единицы.

  --sbor   (облако, только чтение): для каждого из 11 свопов -- событие EvtSwap2 предыдущего свопа того же пула (последняя
           успешная транзакция пула до неё; сверяется первый своп пула в транзакции), пул и config сейчас (постоянные поля).
           -> data/podbivka/dbc_partialfill_sbor.json
  без флага (офлайн): состояние пула до свопа = пул сейчас с sqrt_price и quote_reserve из предыдущего события;
           котировка podbivka_dbc_quote.котировка_точный_вход на included_fee_input_amount события; сравнение с
           output_amount, next_sqrt_price и комиссиями. Частичное исполнение -- amount_left > 0 в событии.
           -> docs/podbivka_2026-09-30_dbc_partialfill.md
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import podbivka_dbc_sbor as DS  # noqa: E402

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"


def цели() -> list:
    d = json.loads((П / "dbc_sbor.json").read_text(encoding="utf-8"))
    из_ = []
    for x in d["сделки"]:
        for i in x["инструкции"]:
            if i.get("swap_mode") == 1:
                из_.append({"signature": x["signature"], "slot": x["slot"], "место": i["место"], "pool": i["роли"]["pool"],
                            "config": i["роли"]["config"]})
    return из_


def сбор() -> int:
    import podbivka_run as R  # noqa: PLC0415
    import podbivka_sim as S  # noqa: PLC0415
    уз = S.Узел()
    ряды, счета = [], {}
    with уз.на("helius"):
        for ц in цели():
            т = уз.tx(ц["signature"])
            ев = [e for e in DS.события(т) if e["pool"] == ц["pool"]] if т else []
            р = {**ц, "события_tx": ев}
            до = [з for з in уз.подписи(ц["pool"], до=ц["signature"], limit=100) if з.get("err") is None]
            for з in до:
                тт = уз.tx(з["signature"])
                ее = [e for e in DS.события(тт) if e["pool"] == ц["pool"]] if тт else []
                if ее:
                    р["предыдущая_транзакция"] = {"signature": з["signature"], "slot": тт.get("slot"), "событие": ее[-1],
                                                  "транзакций_между": до.index(з)}
                    break
            ряды.append(р)
            for a in (ц["pool"], ц["config"]):
                if a not in счета:
                    v = уз.вызов("getAccountInfo", [a, {"encoding": "base64", "commitment": "confirmed"}])
                    счета[a] = ((v or {}).get("value") or {}).get("data", [None])[0]
            print(ц["signature"][:8], bool(р.get("предыдущая_транзакция")), flush=True)
    out = П / "dbc_partialfill_sbor.json"
    out.write_text(json.dumps({"ряды": ряды, "счета_сейчас": счета, "расход": уз.расход()}, ensure_ascii=False), encoding="utf-8")
    R.записано(out)
    return 0


def разбор() -> int:
    import podbivka_dbc_quote as Q  # noqa: PLC0415
    д = json.loads((П / "dbc_partialfill_sbor.json").read_text(encoding="utf-8"))
    md = ["# DBC: PartialFill источников как ExactIn -- 11 инструкций (swap_mode 1)", "",
          "Состояние пула до свопа: пул и config сейчас (постоянные поля) + sqrt_price и quote_reserve из события EvtSwap2 "
          "предыдущего свопа того же пула (последняя успешная транзакция пула до сделки; сверяется первый своп пула в транзакции). "
          "Котировка -- podbivka_dbc_quote.котировка_точный_вход на included_fee_input_amount события; current_point -- "
          "слот сделки или current_timestamp события (по activation_type). Частичное исполнение -- amount_left > 0. "
          "Сбор и разбор -- analysis/podbivka_dbc_partialfill.py.", "",
          "| сделка | направление | вход | amount_left | факт выхода | модель | разница | цена после | комиссии | предыдущее событие |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    итог = {"до_единицы": 0, "сверено": 0, "частичных": 0}
    for р in д["ряды"]:
        ев = р["события_tx"]
        if not ев:
            md.append(f"| `{р['signature'][:8]}` | — | — | — | — | — | — | — | — | событие не найдено |")
            continue
        # какой по счёту своп пула в транзакции -- берём первый; для него предыдущее -- прошлая транзакция
        e = ев[0]
        итог["частичных"] += e["amount_left"] > 0
        пред = (р.get("предыдущая_транзакция") or {}).get("событие")
        if not пред:
            md.append(f"| `{р['signature'][:8]}` | {e['trade_direction']} | {e['included_fee_input_amount']} | {e['amount_left']} | "
                      f"{e['output_amount']} | — | — | — | — | нет |")
            continue
        pool = Q.разобрать_пул(base64.b64decode(д["счета_сейчас"][р["pool"]]))
        cfg = Q.разобрать_конфиг(base64.b64decode(д["счета_сейчас"][р["config"]]))
        pool.update(sqrt_price=пред["next_sqrt_price"], quote_reserve=пред["quote_reserve_amount"], has_swap=1)
        q2b = e["trade_direction"] == 1
        точка = р["slot"] if cfg["activation_type"] == 0 else e["current_timestamp"]
        прим = []
        if cfg["dynamic_fee"]["initialized"]:
            прим.append("динамическая комиссия -- накопитель из пула сейчас, не на момент свопа")
        try:
            q = Q.котировка_точный_вход(pool, cfg, e["included_fee_input_amount"], q2b, точка, e["has_referral"], False)
        except Q.ОшибкаDBC as exc:
            md.append(f"| `{р['signature'][:8]}` | {e['trade_direction']} | {e['included_fee_input_amount']} | {e['amount_left']} | "
                      f"{e['output_amount']} | {str(exc)[:40]} | — | — | — | есть |")
            continue
        итог["сверено"] += 1
        итог["до_единицы"] += q["output_amount"] == e["output_amount"]
        комис = (q["trading_fee"], q["protocol_fee"], q["referral_fee"]) == (e["trading_fee"], e["protocol_fee"], e["referral_fee"])
        md.append(f"| `{р['signature'][:8]}` | {'quote→base' if q2b else 'base→quote'} | {e['included_fee_input_amount']} | "
                  f"{e['amount_left']} | {e['output_amount']} | {q['output_amount']} | {q['output_amount'] - e['output_amount']} | "
                  f"{'=' if q['next_sqrt_price'] == e['next_sqrt_price'] else '≠'} | {'=' if комис else '≠'} | "
                  f"через {р['предыдущая_транзакция']['транзакций_между']} транз.{'; ' + '; '.join(прим) if прим else ''} |")
    md[2:2] = [f"**Итог: до единицы {итог['до_единицы']} из {итог['сверено']} посчитанных (всего 11); исполнено частично "
               f"(amount_left > 0) -- {итог['частичных']} из 11.**", ""]
    (КОРЕНЬ / "docs" / "podbivka_2026-09-30_dbc_partialfill.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    р_ = argparse.ArgumentParser()
    р_.add_argument("--sbor", action="store_true")
    raise SystemExit(сбор() if р_.parse_args().sbor else разбор())
