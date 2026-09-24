#!/usr/bin/env python3
"""Раскладка наших покупок по времени и МЕСТУ В БЛОКЕ. Только чтение.

Вопрос владельца звучит буквально так: сколько миллисекунд съедает
площадка и в какой части блока S+1 мы садимся. Оба ответа считаются по
фактам, а не по ощущениям:

  * время -- из журнала решений и позиций: получено сообщение -> решение ->
    отправлен POST -> принят ответ. Замер площадки (bloom_ms) берётся из
    ответа клиента, где он снят вокруг ОДНОГО POST;
  * место в блоке -- из getBlock по слоту: индекс нашей подписи среди
    подписей блока и сколько их всего. Доля считается как индекс/всего, и
    "первая половина" -- это доля меньше 0.5, а не на глаз.

Слот посадки берётся из getTransaction, а не из ответа площадки: 200 у
Bloom означает только приём.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_detector as BD  # noqa: E402
import bloom_exec_state as ST  # noqa: E402


def позиция_в_блоке(helius, слот: int, подпись: str) -> dict:
    """Индекс подписи в блоке и сколько подписей в блоке всего."""
    if not isinstance(слот, int) or not подпись:
        return {"known": False, "why_not": "нет слота или подписи"}
    try:
        блок = helius.call("getBlock", [слот, {
            "encoding": "json", "transactionDetails": "signatures",
            "rewards": False, "maxSupportedTransactionVersion": BD.ПОТОЛОК_ВЕРСИИ_TX}])
    except Exception as exc:  # noqa: BLE001
        return {"known": False, "why_not": f"getBlock не отдался: {type(exc).__name__}"}
    подписи = (блок or {}).get("signatures") or []
    if not подписи:
        return {"known": False, "why_not": "в ответе getBlock нет списка подписей"}
    try:
        индекс = подписи.index(подпись)
    except ValueError:
        return {"known": False, "total": len(подписи),
                 "why_not": "нашей подписи в этом блоке нет"}
    return {"known": True, "index": индекс, "total": len(подписи),
             "share": round(индекс / len(подписи), 4),
             "half": ("первая" if индекс < len(подписи) / 2 else "вторая")}


def слот_подписи(helius, подпись: str) -> dict:
    try:
        tx = helius.транзакция(подпись)
    except Exception as exc:  # noqa: BLE001
        return {"known": False, "why_not": f"{type(exc).__name__}"}
    if not tx:
        return {"known": False, "why_not": "узел не отдал транзакцию"}
    return {"known": True, "slot": tx.get("slot"),
             "err": (tx.get("meta") or {}).get("err"),
             "block_time": tx.get("blockTime")}


def времена(позиция: dict, решение: dict | None) -> dict:
    """Что известно о времени по журналу. Неизвестное остаётся None."""
    t_recv = (решение or {}).get("t_recv_ts")
    t_decide = (решение or {}).get("t_decide_ts")
    t_sent = позиция.get("ts_sent")
    t_accept = позиция.get("ts_accepted")
    t_intent = позиция.get("ts_intent")

    def мс(a, b):
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return round((b - a) * 1000.0, 1)
        return None

    return {"t_recv_utc": (решение or {}).get("t_recv_utc"),
             "parse_ms": (решение or {}).get("parse_ms"),
             "gate_ms": (решение or {}).get("gate_ms"),
             "decide_latency_ms": (решение or {}).get("decide_latency_ms"),
             "decide_to_intent_ms": мс(t_decide, t_intent),
             "intent_to_sent_ms": мс(t_intent, t_sent),
             "bloom_ms": позиция.get("bloom_ms"),
             "bloom_ms_from_marks": мс(t_sent or t_intent, t_accept),
             "recv_to_accept_ms": мс(t_recv, t_accept),
             "note": ("bloom_ms снят вокруг POST; bloom_ms_from_marks -- по "
                       "меткам журнала (включает запись намерения на диск)")}


def разбор(state: ST.ExecState, helius, *, только: tuple = ()) -> list:
    решения = {}
    try:
        for line in state.decisions_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("signature") and r.get("action") == "buy":
                решения[r["signature"]] = r
    except OSError:
        pass

    вых = []
    for поз in state.positions().values():
        подписи = поз.get("signatures") or []
        наша = подписи[0] if подписи else None
        if только and not (наша in только
                            or (поз.get("client_order_id") or "") in только
                            or any((наша or "").startswith(x) for x in только)):
            continue
        реш = решения.get(поз.get("source_sig"))
        зап = {"client_order_id": поз.get("client_order_id"),
                "mint": поз.get("mint"), "mode": поз.get("mode"),
                "our_signature": наша, "source_signature": поз.get("source_sig"),
                "source_slot_from_journal": поз.get("source_slot"),
                "times": времена(поз, реш)}
        наш = слот_подписи(helius, наша) if наша else {"known": False}
        ист = слот_подписи(helius, поз.get("source_sig")) if поз.get("source_sig") else {}
        зап["our_tx"] = наш
        зап["source_tx"] = ист
        if наш.get("known"):
            зап["our_in_block"] = позиция_в_блоке(helius, наш.get("slot"), наша)
        if ист.get("known"):
            зап["source_in_block"] = позиция_в_блоке(helius, ист.get("slot"),
                                                     поз.get("source_sig"))
        if наш.get("known") and ист.get("known"):
            try:
                зап["slot_delta"] = int(наш["slot"]) - int(ист["slot"])
            except (TypeError, ValueError):
                зап["slot_delta"] = None
        вых.append(зап)
    вых.sort(key=lambda z: str((z.get("times") or {}).get("t_recv_utc")))
    return вых


def в_текст(строки: list) -> str:
    L = ["=== раскладка наших покупок: время и место в блоке ==="]
    for z in строки:
        t = z.get("times") or {}
        L.append("")
        L.append(f"покупка {str(z.get('client_order_id'))[:8]} минт {z.get('mint')} "
                 f"({z.get('mode')})")
        L.append(f"  подпись наша {str(z.get('our_signature'))[:16]} | "
                 f"источника {str(z.get('source_signature'))[:16]}")
        L.append(f"  время: получено {t.get('t_recv_utc')} | разбор {t.get('parse_ms')} мс "
                 f"| гейт {t.get('gate_ms')} мс | решение {t.get('decide_latency_ms')} мс")
        L.append(f"         решение->намерение {t.get('decide_to_intent_ms')} мс | "
                 f"намерение->POST {t.get('intent_to_sent_ms')} мс")
        L.append(f"         ПЛОЩАДКА {t.get('bloom_ms')} мс (по меткам "
                 f"{t.get('bloom_ms_from_marks')} мс) | всё вместе "
                 f"{t.get('recv_to_accept_ms')} мс")
        нб = z.get("our_in_block") or {}
        иб = z.get("source_in_block") or {}
        L.append(f"  слоты: источник {(z.get('source_tx') or {}).get('slot')} -> наш "
                 f"{(z.get('our_tx') or {}).get('slot')} (разница {z.get('slot_delta')})")
        L.append(f"  место в блоке: наше {нб.get('index')}/{нб.get('total')} "
                 f"(доля {нб.get('share')}, {нб.get('half')} половина)"
                 + (f" -- {нб.get('why_not')}" if нб.get("why_not") else ""))
        L.append(f"                 источника {иб.get('index')}/{иб.get('total')} "
                 f"(доля {иб.get('share')})"
                 + (f" -- {иб.get('why_not')}" if иб.get("why_not") else ""))
    return "\n".join(L)


def self_test() -> int:
    проверки = []

    def chk(имя, ок, факт=""):
        проверки.append((имя, bool(ок), факт))

    class HeliusБлок:
        def __init__(self, подписи, слот=100):
            self.подписи = подписи
            self.слот = слот

        def call(self, метод, параметры, **kw):
            assert метод == "getBlock"
            return {"signatures": self.подписи}

        def транзакция(self, подпись, **kw):
            return {"slot": self.слот, "meta": {"err": None}, "blockTime": 1}

    h = HeliusБлок(["A", "B", "C", "D"])
    r = позиция_в_блоке(h, 100, "B")
    chk("индекс в блоке и всего подписей", r["index"] == 1 and r["total"] == 4, r)
    chk("доля и половина посчитаны", r["share"] == 0.25 and r["half"] == "первая", r)
    r2 = позиция_в_блоке(h, 100, "D")
    chk("вторая половина названа второй", r2["half"] == "вторая", r2)
    r3 = позиция_в_блоке(h, 100, "НЕТ_ТАКОЙ")
    chk("нашей подписи в блоке нет -- сказано, а не подставлено число",
        r3["known"] is False and "нет" in r3["why_not"], r3)
    chk("без слота -- отказ", позиция_в_блоке(h, None, "A")["known"] is False)

    class HeliusПустой:
        def call(self, *a, **k):
            return {"signatures": []}

    chk("пустой ответ getBlock -- честный отказ",
        позиция_в_блоке(HeliusПустой(), 1, "A")["known"] is False)

    class HeliusПадает:
        def call(self, *a, **k):
            raise RuntimeError("узел молчит")

    chk("отказ узла не роняет разбор",
        позиция_в_блоке(HeliusПадает(), 1, "A")["known"] is False)

    t = времена({"ts_intent": 1000.0, "ts_sent": 1000.05, "ts_accepted": 1000.2,
                  "bloom_ms": 150.0},
                 {"t_recv_ts": 999.9, "t_decide_ts": 999.95, "parse_ms": 0.3,
                  "gate_ms": 0.01, "decide_latency_ms": 0.4,
                  "t_recv_utc": "2026-09-24T00:21:06Z"})
    chk("площадка берётся из замера вокруг POST", t["bloom_ms"] == 150.0, t)
    chk("и рядом есть оценка по меткам журнала",
        t["bloom_ms_from_marks"] == 150.0, t)
    chk("путь от сообщения до ответа посчитан", t["recv_to_accept_ms"] == 300.0, t)
    chk("неизвестное время остаётся неизвестным",
        времена({}, None)["bloom_ms"] is None)

    плохо = [c for c in проверки if not c[1]]
    for имя, ок, факт in проверки:
        print(f"{'OK ' if ок else 'НЕТ'} {имя}"
              f"{(' -- ' + json.dumps(факт, ensure_ascii=False, default=str)[:200]) if факт and not ок else ''}")
    print(f"самопроверка раскладки: {len(проверки) - len(плохо)}/{len(проверки)}")
    return 1 if плохо else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--only", action="append", default=[],
                     help="подпись или client_order_id (можно несколько)")
    ap.add_argument("--out", default="data/bloom_timing_breakdown.json")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    state = ST.ExecState()
    helius = BD.Helius(служба="")
    строки = разбор(state, helius, только=tuple(a.only or ()))
    текст = в_текст(строки)
    print(текст)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(строки, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    Path(a.out).with_suffix(".txt").write_text(текст + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
