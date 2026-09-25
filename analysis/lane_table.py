#!/usr/bin/env python3
"""Таблица покупок полосы: слот, место, кто довёз, время от сигнала до посадки.

Владелец 25.09: "По всем покупкам полосы с 15:13Z: слот относительно источника,
место в блоке, кто довёз, время от сигнала до посадки. Таблицей."

ЧТО БЕРЁМ И ОТКУДА. Только журнал позиций исполнителя (positions.jsonl) с хоста,
на чтение. Ни одного числа не считаем "по настройкам": слот источника, наш слот,
место в блоке и подпись -- это то, что записал детектор, увидев нашу же
транзакцию в подписке.

ЧЕСТНО ПРО "ПОСАДКУ". Момент включения в блок мы наблюдаем как появление
транзакции в подписке на уровне processed -- это ближайшее, что у нас есть, и
именно так подписан столбец. Разница со временем сигнала считается от t_recv
сигнала источника на нашем узле (signal_recv_ts): это тот же ноль, от которого
считается пара с Bloom.

КТО ДОВЁЗ. В обычном пуле это тот, чей ответ пришёл первым (одна подпись на
всех). В варианте на nonce -- тот, ЧЕЙ ВАРИАНТ СЕЛ: варианты отличаются только
чаевыми, значит севшая подпись однозначно называет сервис. Если режим не пул --
столбец пуст, и это не пропуск данных, а отсутствие пула.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def позиции_из_журнала(путь: str) -> dict:
    """Свёрнутые позиции: строки журнала дописываются, последняя правит поля."""
    из_: dict = {}
    п = Path(путь)
    if not п.exists():
        return из_
    for строка in п.read_text(encoding="utf-8", errors="replace").split("\n"):
        строка = строка.strip()
        if not строка.startswith("{"):
            continue
        try:
            з = json.loads(строка)
        except ValueError:
            continue
        cid = з.get("client_order_id")
        if not cid:
            continue
        из_.setdefault(cid, {}).update({к: v for к, v in з.items() if v is not None})
    return из_


def строки_таблицы(позиции: dict, *, с_utc: str = "", метка: str = "own_send") -> list:
    ряд = []
    for cid, п in позиции.items():
        if п.get("lane") != метка:
            continue
        когда = str(п.get("ts_intent_utc") or "")
        if с_utc and когда and когда < с_utc:
            continue
        их = п.get("source_slot")
        наш = п.get("own_tx_seen_slot") or п.get("our_slot")
        отставание = (наш - их) if isinstance(их, int) and isinstance(наш, int) else None
        сигнал_ts = п.get("signal_recv_ts")
        видно_ts = п.get("own_tx_seen_ts")
        от_сигнала_мс = (round((float(видно_ts) - float(сигнал_ts)) * 1000.0, 1)
                          if сигнал_ts and видно_ts else None)
        довёз = п.get("lane_pool_winner")
        ряд.append({
            "cid": cid, "utc": когда, "group": п.get("lane_group") or "bloom_lane",
            "mint": (п.get("mint") or "")[:10],
            "size_sol": п.get("sol_in"),
            "source_slot": их, "our_slot": наш, "slots_behind": отставание,
            "block_index": п.get("block_index"), "block_total": п.get("block_total"),
            "winner": довёз, "mode": п.get("lane_pool_mode"),
            "senders": п.get("lane_pool_ok_senders") or п.get("lane_pool_senders"),
            "from_signal_ms": от_сигнала_мс,
            "send_to_seen_ms": п.get("lane_send_to_seen_ms"),
            "state": п.get("state"), "chain_ok": п.get("chain_ok"),
            "signature": (п.get("lane_signature") or "")[:16],
            "accepted_first": (п.get("lane_signature_accepted_first") or "")[:16],
            "tips_sol": п.get("lane_tips_total_sol"),
            "uncountable": п.get("result_uncountable"),
        })
    ряд.sort(key=lambda з: з["utc"])
    return ряд


def _цепь_словами(з: dict) -> str:
    ц = з.get("chain") or {}
    if not ц:
        return "не проверяли"
    if ц.get("why_not"):
        return str(ц["why_not"])[:60]
    if ц.get("landed") is True:
        хвост = f", ошибка {ц['err']}" if ц.get("err") else ""
        return f"села {str(ц.get('signature'))[:12]} в слоте {ц.get('slot')}{хвост}"
    if ц.get("landed") is False:
        return f"не села ни одна из {ц.get('checked')} подписей"
    return "—"


def _токен_словами(з: dict) -> str:
    ц = з.get("chain") or {}
    if ц.get("token_ui") is None:
        return "—"
    return f"{ц['token_ui']} (счетов {ц.get('token_accounts')})"


def в_цели(з: dict, голова: int = 100) -> str:
    """Цель владельца 25.09: S+0 в любом месте ИЛИ голова S+1 (место <= 100)."""
    о = з.get("slots_behind")
    м = з.get("block_index")
    if о is None:
        return "—"
    if о == 0:
        return "ДА (S+0)"
    if о == 1:
        if not isinstance(м, int):
            return "не судим (места нет)"
        return f"ДА (S+1, место {м})" if м <= голова else f"нет (S+1, место {м})"
    return f"нет (S+{о})"


def проверить_по_цепи(ряды: list, позиции: dict, rpc_call,
                       кошелёк: str | None = None) -> list:
    """Что с этими покупками В ЦЕПИ: села ли подпись и держим ли токен.

    ЗАЧЕМ. Позиция в состоянии unsold без полей цепи (нет chain_ok, нет слота,
    нет купленного количества) значит одно из двух: либо транзакция не села
    вовсе -- и тогда терять нечего, кроме чаевых, -- либо она села, а мы её не
    узнали, и на кошельке полосы лежит токен, который никто не продаёт. Разница
    в деньгах, и решает её только цепь.
    """
    for з in ряды:
        п = позиции.get(з["cid"]) or {}
        кандидаты = [к for к in (п.get("lane_pool_candidates") or []) if к]
        если_одна = п.get("lane_signature")
        if если_одна and если_одна not in кандидаты:
            кандидаты.append(если_одна)
        з["chain"] = {"checked": len(кандидаты), "landed": None, "signature": None,
                       "slot": None, "err": None, "token_ui": None,
                       "token_accounts": 0, "why_not": None}
        if not кандидаты:
            з["chain"]["why_not"] = "подписей в позиции нет"
            continue
        try:
            от = rpc_call("getSignatureStatuses",
                           [кандидаты, {"searchTransactionHistory": True}])
        except Exception as exc:  # noqa: BLE001
            з["chain"]["why_not"] = f"узел не ответил: {type(exc).__name__}"
            continue
        значения = ((от or {}).get("value") or [])
        села = None
        for подпись, зн in zip(кандидаты, значения):
            if зн and зн.get("slot") is not None:
                села = (подпись, зн)
                break
        if села is None:
            з["chain"]["landed"] = False
        else:
            подпись, зн = села
            з["chain"].update(landed=True, signature=подпись, slot=зн.get("slot"),
                               err=json.dumps(зн.get("err"), ensure_ascii=False)
                               if зн.get("err") else None,
                               status=зн.get("confirmationStatus"))
        # ДЕРЖИМ ЛИ ТОКЕН. Это и есть ответ "лежат ли деньги в минте".
        минт = п.get("mint")
        # КОШЕЛЁК ПОЛОСЫ ЗАДАЁТСЯ СНАРУЖИ. В поле wallet записи позиции лежит
        # адрес ИСПОЛНИТЕЛЯ (его пишет write_intent всем позициям подряд), и
        # проверка токенов по нему смотрела не тот кошелёк: получалось "токенов
        # 0, счетов 0" при живой покупке.
        кош = кошелёк or п.get("lane_wallet") or п.get("wallet")
        if минт and кош:
            try:
                тк = rpc_call("getTokenAccountsByOwner",
                               [кош, {"mint": минт},
                                {"encoding": "jsonParsed", "commitment": "confirmed"}])
                счета = ((тк or {}).get("value") or [])
                з["chain"]["token_accounts"] = len(счета)
                сумма = 0.0
                for с in счета:
                    инфо = (((с.get("account") or {}).get("data") or {})
                            .get("parsed") or {}).get("info") or {}
                    сумма += float(((инфо.get("tokenAmount") or {})
                                     .get("uiAmount")) or 0.0)
                з["chain"]["token_ui"] = сумма
            except Exception as exc:  # noqa: BLE001
                з["chain"]["why_not"] = (з["chain"].get("why_not") or "") + \
                                         f" токены не прочитаны: {type(exc).__name__}"
    return ряды


# НАШИ АДРЕСА. Покупки с них -- не "чужие": ни наш кошелёк полосы, ни кошелёк
# исполнителя, ни кошельки задач DBot в счёт чужих покупок идти не должны,
# иначе "S+0 был возможен" подтверждался бы нашей же сделкой.
НАШИ_АДРЕСА = (
    "21DqHDDPEfMhK1dHRkV9E8v8KTTSKQGApJAr1irC9j7w",   # кошелёк полосы
    "4s87ZkKLXRGRFPWLHmCMXKqmKrUJCLXbfKMDYtLcTLkC",   # кошелёк исполнителя (Bloom)
)


def блок(rpc_call, слот: int, кэш: dict) -> dict:
    """Блок слота с составом транзакций. transactionDetails=accounts: приходят
    подписи, счета и балансы токенов -- всё, что нужно для места и покупок, и
    без тел инструкций, которые весили бы мегабайты."""
    if слот in кэш:
        return кэш[слот]
    из_: dict = {"ok": False, "why_not": None, "txs": []}
    try:
        от = rpc_call("getBlock", [слот, {"encoding": "json",
                                           "transactionDetails": "accounts",
                                           "rewards": False,
                                           "maxSupportedTransactionVersion": 0}])
    except Exception as exc:  # noqa: BLE001
        из_["why_not"] = f"узел не ответил: {type(exc).__name__}"
        кэш[слот] = из_
        return из_
    if not isinstance(от, dict):
        из_["why_not"] = "блока нет в ответе узла"
        кэш[слот] = из_
        return из_
    из_["ok"] = True
    из_["txs"] = от.get("transactions") or []
    кэш[слот] = из_
    return из_


def _место(блок_: dict, подпись: str) -> tuple:
    """Место транзакции в блоке: (индекс с 0, всего). Нет подписи -- (None, всего)."""
    всего = len(блок_.get("txs") or [])
    if not подпись:
        return None, всего
    for и, т in enumerate(блок_.get("txs") or []):
        подписи = ((т.get("transaction") or {}).get("signatures") or [])
        if подпись in подписи:
            return и, всего
    return None, всего


def _купил_минт(т: dict, минт: str, наши: tuple) -> bool:
    """Чужая ПОКУПКА этого минта: у владельца не из наших остаток минта вырос.

    Упавшие транзакции не считаются: покупки в них не было. Продажи тоже:
    у продавца остаток падает, а не растёт.
    """
    мета = т.get("meta") or {}
    if мета.get("err"):
        return False
    было: dict = {}
    for б in (мета.get("preTokenBalances") or []):
        if б.get("mint") == минт:
            было[(б.get("owner"), б.get("accountIndex"))] = \
                float(((б.get("uiTokenAmount") or {}).get("amount")) or 0)
    for б in (мета.get("postTokenBalances") or []):
        if б.get("mint") != минт:
            continue
        хозяин = б.get("owner")
        if хозяин in наши:
            continue
        стало = float(((б.get("uiTokenAmount") or {}).get("amount")) or 0)
        if стало > было.get((хозяин, б.get("accountIndex")), 0.0):
            return True
    return False


def разобрать_блоки(ряды: list, позиции: dict, rpc_call,
                     наши: tuple = НАШИ_АДРЕСА) -> list:
    """Место источника в его блоке и чужие покупки того же токена в S+0.

    Владелец 25.09: "место источника в его блоке (индекс / всего) и сколько
    чужих покупок того же токена село в S+0 после источника. Столбец «S+0 был
    возможен»: да, если после источника в его слоте сели чужие. Без этого «S+1»
    не читается."

    Читается ровно то, что в блоке: индекс источника, сколько транзакций стоит
    после него и сколько из них -- покупки нашего минта чужими кошельками. Наше
    место в блоке (столбец таблицы) добирается из ТОГО ЖЕ блока по нашей севшей
    подписи, если детектор его не записал.
    """
    кэш: dict = {}
    for з in ряды:
        п = позиции.get(з["cid"]) or {}
        минт = п.get("mint")
        слот_и = п.get("source_slot")
        подпись_и = п.get("source_sig")
        з["s0"] = {"source_index": None, "source_total": None,
                    "after_source": None, "foreign_buys_after": None,
                    "s0_possible": None, "why_not": None}
        if not isinstance(слот_и, int) or not подпись_и:
            з["s0"]["why_not"] = "слота или подписи источника в позиции нет"
        else:
            б = блок(rpc_call, слот_и, кэш)
            if not б.get("ok"):
                з["s0"]["why_not"] = б.get("why_not")
            else:
                и, всего = _место(б, подпись_и)
                з["s0"].update(source_index=и, source_total=всего)
                if и is None:
                    з["s0"]["why_not"] = "подписи источника в этом блоке нет"
                else:
                    хвост = (б.get("txs") or [])[и + 1:]
                    з["s0"]["after_source"] = len(хвост)
                    покупки = sum(1 for т in хвост
                                   if минт and _купил_минт(т, минт, наши))
                    з["s0"]["foreign_buys_after"] = покупки
                    з["s0"]["s0_possible"] = bool(покупки)
        # НАШЕ МЕСТО В БЛОКЕ -- добор по цепи (задача владельца 25.09 п. 4).
        if з.get("block_index") is None:
            ц = з.get("chain") or {}
            наша_подпись = ц.get("signature") or п.get("lane_signature")
            наш_слот = ц.get("slot") or з.get("our_slot")
            if наша_подпись and isinstance(наш_слот, int):
                бн = блок(rpc_call, наш_слот, кэш)
                if бн.get("ok"):
                    им, вс = _место(бн, наша_подпись)
                    if им is not None:
                        з["block_index"] = им
                        з["block_total"] = вс
                        з["block_index_from"] = "цепь"
    return ряды


def _s0_словами(з: dict) -> tuple:
    """Две клетки таблицы: место источника и "S+0 был возможен"."""
    с = з.get("s0") or {}
    if с.get("source_index") is None:
        причина = с.get("why_not") or "не смотрели"
        return "—", str(причина)[:40]
    место = f"{с['source_index']}/{с['source_total']}"
    if с.get("s0_possible") is None:
        return место, "—"
    к = с.get("foreign_buys_after")
    п = с.get("after_source")
    if с["s0_possible"]:
        return место, f"ДА ({к} чужих покупок после, всего tx после {п})"
    return место, f"нет (0 чужих покупок, всего tx после {п})"


def таблица(ряд: list) -> str:
    ряды = ["| время UTC | группа | минт | размер SOL | слот источника | место источника | "
             "наш слот | S+N | место в блоке | S+0 был возможен | кто довёз | режим | "
             "от сигнала до появления, мс | от отправки, мс | в цели | состояние | "
             "чаевые SOL | по цепи | токен на кошельке |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]

    def ч(з):
        return "—" if з is None or з == "" else str(з)

    for з in ряд:
        место = (f"{з['block_index']}/{з['block_total']}"
                 if з.get("block_index") is not None else "—")
        место_и, был_s0 = _s0_словами(з)
        ряды.append(
            f"| {ч(з['utc'])} | {ч(з['group'])} | {ч(з['mint'])} | {ч(з['size_sol'])} | "
            f"{ч(з['source_slot'])} | {место_и} | {ч(з['our_slot'])} | "
            f"{('S+' + str(з['slots_behind'])) if з['slots_behind'] is not None else '—'} | "
            f"{место} | {был_s0} | {ч(з['winner'])} | {ч(з['mode'])} | {ч(з['from_signal_ms'])} | "
            f"{ч(з['send_to_seen_ms'])} | {в_цели(з)} | "
            f"{ч(з['state'])}{' (цепь ok)' if з.get('chain_ok') else ''} | "
            f"{ч(з['tips_sol'])} | {_цепь_словами(з)} | {_токен_словами(з)} |")
    return "\n".join(ряды)


def main() -> int:
    import argparse

    р = argparse.ArgumentParser(description=__doc__)
    р.add_argument("--positions", default="/tmp/bloom_state/positions.jsonl")
    р.add_argument("--since", default="", help="только позиции с этого UTC, например 2026-09-25T15:13")
    р.add_argument("--out-md", default=None)
    р.add_argument("--out-json", default=None)
    р.add_argument("--wallet", default=None,
                    help="кошелёк полосы для проверки токенов (в позиции лежит адрес исполнителя)")
    р.add_argument("--no-blocks", action="store_true",
                    help="не читать блоки: без места источника и без S+0")
    р.add_argument("--chain", action="store_true",
                    help="проверить по цепи: села ли подпись и держим ли токен")
    р.add_argument("--raw-json", default=None,
                    help="полные записи позиций полосы окна (ключей в них нет)")
    а = р.parse_args()
    поз = позиции_из_журнала(а.positions)
    ряд = строки_таблицы(поз, с_utc=а.since)
    if а.chain:
        import solana_rpc_client as RPC  # noqa: PLC0415

        клиент = RPC.SolanaRpc(service="lane_table")

        def зов(метод, параметры):
            от = клиент.call(метод, параметры)
            return от.get("result") if isinstance(от, dict) and "result" in от else от

        ряд = проверить_по_цепи(ряд, поз, зов, кошелёк=а.wallet)
        if not а.no_blocks:
            # МЕСТО ИСТОЧНИКА И ЧУЖИЕ ПОКУПКИ В S+0 (задача владельца 25.09).
            # Блок берётся составом без тел инструкций, по одному разу на слот.
            ряд = разобрать_блоки(ряд, поз, зов)
    т = таблица(ряд)
    print(f"позиций в журнале: {len(поз)}, покупок полосы в окне: {len(ряд)}")
    print(т)
    if а.out_md:
        Path(а.out_md).write_text(
            f"# Покупки полосы с {а.since or 'начала журнала'}\n\n"
            f"Позиций в журнале: {len(поз)}; покупок полосы в окне: {len(ряд)}.\n\n"
            + т + "\n", encoding="utf-8")
        print(f"записано: {а.out_md}")
    if а.raw_json:
        # ПОЛНЫЕ ЗАПИСИ -- для разбора, когда таблицы мало: почему unsold, что
        # записал сторож, сколько купили. Ключей в позициях нет вовсе.
        свои = {к: v for к, v in поз.items()
                if v.get("lane") == "own_send"
                and (not а.since or str(v.get("ts_intent_utc") or "") >= а.since)}
        Path(а.raw_json).write_text(
            json.dumps(свои, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"записано: {а.raw_json} ({len(свои)} позиций)")
    if а.out_json:
        Path(а.out_json).write_text(
            json.dumps({"since": а.since, "rows": ряд}, ensure_ascii=False, indent=2)
            + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
