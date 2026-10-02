#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ЧИСЛО: сборка продажи сходится с ЖИВЫМИ ПРОДАЖАМИ образцов, байт в байт.

ЗАЧЕМ. Раскладку продажи до сих пор приходилось выводить из нашей покупки, а
свериться было не с чем: живых продаж в репозитории не было. Code-2 выгрузил их
(data/samples/prodazhi/<тип>.json, полные тела getTransaction) -- теперь сборку
можно проверить не рассуждением, а равенством байтов.

КАК ПРОВЕРЯЕТСЯ -- ОТ ЖИВОЙ ПРОДАЖИ НАЗАД К ПОКУПКЕ И СНОВА ВПЕРЁД:
  1. из живой продажи восстанавливается ПОКУПКА того же пула -- обратной картой
     мест модуля; два счёта, которых у продажи нет, восстанавливаются ЧЕСТНО:
     global_volume_accumulator -- постоянный адрес, одинаковый во ВСЕХ живых
     покупках образцов, user_volume_accumulator -- PDA от подписанта (выводится
     семенами). Больше ничего не добавляется;
  2. восстановленная покупка кладётся в транзакцию той же продажи вместо её
     инструкции -- получается ровно то, что модуль видит на денежном пути: НАША
     покупка;
  3. модуль собирает продажу своим обычным путём (шаблон_продажи -> инструкция
     продажи) с теми же аргументами, что у живой;
  4. требуется РАВЕНСТВО БАЙТОВ с живой инструкцией продажи: счета в том же
     порядке, те же права записи и подписи, те же данные.
Шаг 1 не может «подогнать» проверку: он восстанавливает только те два счёта,
которых у продажи нет, а всё остальное берёт из самой живой продажи. Если карта
мест модуля неверна, шаг 3 поставит счёт не на то место, и шаг 4 это покажет.

КРОМЕ РАВЕНСТВА -- РОЛИ. У каждой живой продажи сверяются роли мест по данным
САМОЙ транзакции: минт, PDA кривой, хранилище кривой, наш токеновый счёт,
подписант, системная программа, программа минта из балансов, event_authority,
fee_config и программа комиссий (постоянные -- те же, что у живых покупок).

Ни сети, ни подписи, ни отправки: читаются только файлы образцов.
"""
from __future__ import annotations

import hashlib
import json
import struct
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(КОРЕНЬ / "analysis"))

ОБРАЗЦЫ = КОРЕНЬ / "data" / "samples" / "prodazhi"
PROG_KRIVAYA = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
# ПОСТОЯННЫЕ СЧЕТА КРИВОЙ -- ЗАМЕР НА ЖИВЫХ ПОКУПКАХ ОБРАЗЦОВ (все 5 из 5):
# место 12 покупки -- global_volume_accumulator, место 10 -- event_authority,
# 14 -- fee_config, 15 -- программа комиссий. Здесь они нужны, чтобы восстановить
# покупку из продажи и чтобы сверить роли; выводятся они прогоном ниже.
НАКОПИТЕЛЬ_ОБЪЁМА = "Hq2wp8uJ9jCPsYgNHex8RtqdvMPfVGoYwjvF1ATiwn2Y"
СИСТЕМНАЯ = "11111111111111111111111111111111"


def _диск(имя: str) -> bytes:
    return hashlib.sha256(f"global:{имя}".encode()).digest()[:8]


def _b58(данные: bytes) -> str:
    import c2_swap_build as B  # noqa: PLC0415

    алф = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    н = int.from_bytes(данные, "big")
    из_ = ""
    while н:
        н, о = divmod(н, 58)
        из_ = алф[о] + из_
    for б in данные:
        if б:
            break
        из_ = алф[0] + из_
    assert B.b58decode(из_) == данные
    return из_


def _событие_продажи(tx: dict, минт: str) -> dict | None:
    """Событие продажи кривой (is_buy = 0) по той же раскладке, что у покупки.

    Раскладка не вводится заново: смещения -- из c2_swap_build (PF_EVENT_DISC и
    комментарий к pump_trade_event). Событие обязано ВОСПРОИЗВЕСТИ свою сделку --
    иначе не берётся, ровно как у покупки.
    """
    import base64  # noqa: PLC0415

    import c2_swap_build as B  # noqa: PLC0415
    from solders.pubkey import Pubkey  # noqa: PLC0415

    for ln in ((tx or {}).get("meta") or {}).get("logMessages") or []:
        if not ln.startswith("Program data: "):
            continue
        try:
            сыро = base64.b64decode(ln[len("Program data: "):].strip())
        except ValueError:
            continue
        if сыро[:8] != B.PF_EVENT_DISC or len(сыро) < B.PF_MIN_EVENT:
            continue
        b = сыро[8:]
        try:
            ev_минт = str(Pubkey(bytes(b[0:32])))
            sol, tok = struct.unpack_from("<QQ", b, 32)
            покупка = b[48]
            кош = str(Pubkey(bytes(b[49:81])))
            vs, vt, _rs, _rt = struct.unpack_from("<QQQQ", b, 89)
            fb, f = struct.unpack_from("<QQ", b, 153)
            cb, cf = struct.unpack_from("<QQ", b, 201)
        except (struct.error, ValueError):
            continue
        if ev_минт != минт or покупка != 0 or sol <= 0 or tok <= 0 or vt <= tok:
            continue
        # СОБЫТИЕ ВОСПРОИЗВОДИТ СВОЮ СДЕЛКУ: резервы ДО продажи -- vs+sol и
        # vt-tok, и кривая по ним обязана дать ровно sol.
        if vs * tok // (vt - tok + tok) != sol and (vs + sol) * tok // (vt - tok + tok) != sol:
            continue
        return {"минт": ev_минт, "кошелёк": кош, "sol": sol, "токенов": tok,
                 "vs": vs, "vt": vt, "fee_bps": fb, "комиссия": f,
                 "creator_bps": cb, "комиссия_создателя": cf}
    return None


def _программа_минта(tx: dict, минт: str) -> str | None:
    """Программа токена этого минта -- из балансов САМОЙ транзакции (узел пишет
    programId у каждого токенового баланса). Своей таблицы минтов не заводится."""
    мета = (tx or {}).get("meta") or {}
    for когда in ("post", "pre"):
        for b in мета.get(f"{когда}TokenBalances") or []:
            if isinstance(b, dict) and b.get("mint") == минт and b.get("programId"):
                return b["programId"]
    return None


def _живая_продажа(tx: dict, программа: str, имя: str) -> dict | None:
    import c2_swap_build as B  # noqa: PLC0415

    for ix in B.all_instructions(tx):
        if ix.get("programId") != программа:
            continue
        данные = B.b58decode(ix["data"])
        if данные[:8] != _диск(имя):
            continue
        return {"accounts": list(ix["accounts"]), "data": данные}
    return None


def _покупка_из_продажи(продажа: dict, подписант: str) -> dict | None:
    """Обратная карта: 18 счетов покупки из 16 счетов продажи. Два -- восстановлены."""
    import c3_prodavec_sborka as P  # noqa: PLC0415
    from solders.pubkey import Pubkey  # noqa: PLC0415

    пр = продажа["accounts"]
    карта = P.КРИВАЯ_МЕСТА_ПРОДАЖИ
    if len(пр) != len(карта):
        return None
    пок: list = [None] * 18
    for i, j in enumerate(карта):
        пок[j] = пр[i]
    пок[12] = НАКОПИТЕЛЬ_ОБЪЁМА
    пок[13] = str(Pubkey.find_program_address(
        [b"user_volume_accumulator", bytes(Pubkey.from_string(подписант))],
        Pubkey.from_string(PROG_KRIVAYA))[0])
    if any(а is None for а in пок):
        return None
    return {"accounts": пок, "data": _диск("buy") + bytes(16)}


def _транзакция_с_покупкой(tx: dict, покупка: dict) -> dict:
    """Та же транзакция, но вместо инструкции продажи -- восстановленная покупка."""
    новая = {"transaction": {"message": dict(
        ((tx.get("transaction") or {}).get("message") or {}))},
        "meta": dict(tx.get("meta") or {})}
    сооб = новая["transaction"]["message"]
    сооб["instructions"] = [{"programId": PROG_KRIVAYA,
                              "accounts": list(покупка["accounts"]),
                              "data": _b58(покупка["data"])}]
    новая["meta"]["innerInstructions"] = []
    return новая


def _байты(инстр) -> tuple:
    return (str(инстр.program_id), bytes(инстр.data),
            tuple((str(м.pubkey), bool(м.is_signer), bool(м.is_writable))
                  for м in инстр.accounts))


ЖДЁМ_ПРОВЕРОК = 34
# Замер: сколько живых продаж каждого типа есть и сколько из них -- та
# разновидность, которую модуль собирает. Числа сверяются файлами.
ЖДЁМ_КРИВОЙ = {"образцов": 6, "наша разновидность": 2,
                "иная разновидность": 4}


def self_test() -> int:  # noqa: C901, PLR0912, PLR0915
    import c2_common as C  # noqa: PLC0415
    import c2_swap_build as B  # noqa: PLC0415
    import c3_prodavec_sborka as P  # noqa: PLC0415
    from solders.instruction import AccountMeta, Instruction  # noqa: PLC0415
    from solders.pubkey import Pubkey  # noqa: PLC0415

    проверки = []

    def chk(что, ок, факт=None):
        проверки.append((что, bool(ок), факт))

    путь = ОБРАЗЦЫ / "krivaya_pump_fun.json"
    chk("файл живых продаж кривой на месте", путь.exists(), str(путь))
    if not путь.exists():
        print("ОБРАЗЦОВ НЕТ -- проверять нечего, это провал, а не пропуск")
        return 1
    д = json.loads(путь.read_text(encoding="utf-8"))
    образцы = д.get("образцы") or []
    chk(f"живых продаж кривой {ЖДЁМ_КРИВОЙ['образцов']}",
        len(образцы) == ЖДЁМ_КРИВОЙ["образцов"], len(образцы))

    # --------- постоянные счета покупки: берутся с ЖИВЫХ покупок, не из головы
    пок_обр = json.loads(
        (C.DATA / "c2_pool_samples" / f"{PROG_KRIVAYA}.json").read_text(encoding="utf-8"))
    счета_покупок = []
    for x in пок_обр:
        tx = x.get("tx")
        if not isinstance(tx, dict):
            continue
        for ix in B.all_instructions(tx):
            if ix.get("programId") != PROG_KRIVAYA:
                continue
            данные = B.b58decode(ix["data"])
            if данные[:8].hex() in P.КРИВАЯ_ПОКУПКИ_18 and len(ix["accounts"]) == 18:
                счета_покупок.append(list(ix["accounts"]))
            break
    chk("живых 18-счётных покупок кривой не меньше трёх", len(счета_покупок) >= 3,
        len(счета_покупок))
    пост = {i: {с[i] for с in счета_покупок} for i in range(18)}
    пост = {i: s.pop() for i, s in пост.items() if len(s) == 1}
    chk("global_volume_accumulator -- один и тот же во всех живых покупках",
        пост.get(12) == НАКОПИТЕЛЬ_ОБЪЁМА, пост.get(12))
    for i, имя in ((0, "global"), (7, "системная"), (10, "event_authority"),
                   (11, "сама программа"), (14, "fee_config"), (15, "программа комиссий")):
        chk(f"место {i} покупки постоянно во всех живых покупках ({имя})",
            i in пост, пост.get(i))

    # ------- КОТИРОВКА ПРОДАЖИ НА ВСЕХ ШЕСТИ: кривая по виртуальным резервам
    # обязана воспроизвести ЖИВОЙ выход до лампорта. Эта проверка не зависит от
    # раскладки счетов, поэтому идёт по всем образцам, а не только по нашей
    # разновидности: цена и сборка проверяются раздельно.
    import c3_kotirovka_prodazhi as KP  # noqa: PLC0415

    котировок, в_лампорт = 0, 0
    for о in образцы:
        ев = _событие_продажи(о["tx_jsonParsed"], о["mint"])
        chk(f"{о['signature'][:8]}: событие продажи разобрано и воспроизвело себя",
            ев is not None, None)
        if ев is None:
            continue
        котировок += 1
        # Резервы ДО продажи: событие несёт их ПОСЛЕ, а продано было `токенов`.
        к = KP.выход_кривой(токенов=ев["токенов"], вирт_sol=ев["vs"] + ев["sol"],
                            вирт_токены=ев["vt"] - ев["токенов"],
                            fee_bps=ев["fee_bps"], creator_bps=ев["creator_bps"])
        живой = ев["sol"] - ев["комиссия"] - ев["комиссия_создателя"]
        точно = (к.get("ok") and к["с_кривой"] == ев["sol"]
                 and к["комиссия"] == ев["комиссия"]
                 and к["комиссия_создателя"] == ев["комиссия_создателя"]
                 and к["выход"] == живой)
        chk(f"{о['signature'][:8]}: котировка продажи повторила живую ДО ЛАМПОРТА",
            точно, (к.get("выход"), живой, к.get("why_not")))
        if точно:
            в_лампорт += 1
    chk(f"котировка сошлась до лампорта на всех {ЖДЁМ_КРИВОЙ['образцов']} живых продажах",
        в_лампорт == котировок == ЖДЁМ_КРИВОЙ["образцов"], (в_лампорт, котировок))

    наша, иная, сошлось = 0, 0, 0
    почему_иная = set()
    for о in образцы:
        tx = о["tx_jsonParsed"]
        живая = _живая_продажа(tx, PROG_KRIVAYA, "sell")
        if живая is None or len(живая["accounts"]) != len(P.КРИВАЯ_МЕСТА_ПРОДАЖИ):
            иная += 1
            for ix in B.all_instructions(tx):
                if ix.get("programId") == PROG_KRIVAYA:
                    почему_иная.add(f"{B.b58decode(ix['data'])[:8].hex()}/"
                                     f"{len(ix['accounts'])} счетов")
                    break
            continue
        наша += 1
        сч = живая["accounts"]
        минт, кош = о["mint"], о["кошелёк"]
        строки = {r["account"]: r for r in C.token_rows(tx).values()}
        кривая_pda = str(Pubkey.find_program_address(
            [b"bonding-curve", bytes(Pubkey.from_string(минт))],
            Pubkey.from_string(PROG_KRIVAYA))[0])
        прог_минта = _программа_минта(tx, минт)
        роли = [
            ("место 2 -- минт продаваемого токена", сч[2] == минт, сч[2]),
            ("место 3 -- PDA кривой этого минта", сч[3] == кривая_pda, сч[3]),
            ("место 4 -- хранилище кривой (владелец -- PDA кривой)",
             (строки.get(сч[4]) or {}).get("owner") == кривая_pda, сч[4]),
            ("место 5 -- НАШ токеновый счёт (владелец -- подписант)",
             (строки.get(сч[5]) or {}).get("owner") == кош, сч[5]),
            ("место 6 -- наш кошелёк", сч[6] == кош, сч[6]),
            ("место 7 -- системная программа", сч[7] == СИСТЕМНАЯ, сч[7]),
            ("место 9 -- программа токена этого минта (из балансов)",
             прог_минта is not None and сч[9] == прог_минта, (сч[9], прог_минта)),
            ("место 8 -- НЕ программа токена (хранилище создателя)",
             сч[8] != сч[9] and сч[8] not in (СИСТЕМНАЯ, PROG_KRIVAYA), сч[8]),
            ("место 10 -- event_authority, тот же, что у живых покупок",
             сч[10] == пост.get(10), сч[10]),
            ("место 11 -- сама программа", сч[11] == PROG_KRIVAYA, сч[11]),
            ("место 12 -- fee_config, тот же, что у покупок на месте 14",
             сч[12] == пост.get(14), сч[12]),
            ("место 13 -- программа комиссий, та же, что у покупок на месте 15",
             сч[13] == пост.get(15), сч[13]),
            ("накопителей объёма у продажи нет ни одного",
             НАКОПИТЕЛЬ_ОБЪЁМА not in сч, сч),
            ("данные продажи -- ровно 24 байта: дискриминатор и два u64",
             len(живая["data"]) == 24, len(живая["data"])),
        ]
        плохо = [(что, факт) for что, ок, факт in роли if not ок]
        chk(f"{о['signature'][:8]}: роли всех 16 мест живой продажи сошлись",
            not плохо, плохо)
        # ---- байт в байт: назад к покупке и снова вперёд модулем
        а0, а1 = struct.unpack("<QQ", живая["data"][8:24])
        пок = _покупка_из_продажи(живая, кош)
        chk(f"{о['signature'][:8]}: покупка восстановлена из продажи обратной картой",
            пок is not None, None)
        if пок is None:
            continue
        синт = _транзакция_с_покупкой(tx, пок)
        ш = P.шаблон_продажи(синт, программа=PROG_KRIVAYA, хранилище=сч[4])
        chk(f"{о['signature'][:8]}: модуль собрал шаблон продажи из этой покупки",
            ш.get("ok"), ш.get("why_not"))
        if not ш.get("ok"):
            continue
        наш = P.инструкция_продажи(ш, наш_кошелёк=кош, база_в=а0,
                                   минимум_выхода=а1)
        права = dict(B.writable_map(tx))
        жив_инстр = Instruction(
            Pubkey.from_string(PROG_KRIVAYA), bytes(живая["data"]),
            [AccountMeta(Pubkey.from_string(а), а == кош, bool(права.get(а, False)))
             for а in сч])
        равно = _байты(наш) == _байты(жив_инстр)
        chk(f"{о['signature'][:8]}: наша инструкция продажи БАЙТ В БАЙТ как живая",
            равно, None if равно else (_байты(наш), _байты(жив_инстр)))
        if равно:
            сошлось += 1
    chk(f"нашей разновидности живых продаж {ЖДЁМ_КРИВОЙ['наша разновидность']}",
        наша == ЖДЁМ_КРИВОЙ["наша разновидность"], наша)
    chk(f"иной разновидности {ЖДЁМ_КРИВОЙ['иная разновидность']} -- отказ по имени",
        иная == ЖДЁМ_КРИВОЙ["иная разновидность"], (иная, sorted(почему_иная)))
    chk("каждая продажа нашей разновидности сошлась байт в байт",
        сошлось == наша, (сошлось, наша))

    плохих = [п for п in проверки if not п[1]]
    for что, ок, факт in проверки:
        print(("  ok  " if ок else " ПЛОХО") + f" {что}" + ("" if ок else f" -- {факт}"))
    print(f"\nпроверок {len(проверки)}, ждали {ЖДЁМ_ПРОВЕРОК}, не прошло {len(плохих)}")
    if len(проверки) != ЖДЁМ_ПРОВЕРОК:
        print("ЧИСЛО ПРОВЕРОК НЕ СОВПАЛО -- молчаливый пропуск считается провалом")
        return 1
    return 1 if плохих else 0


if __name__ == "__main__":
    raise SystemExit(self_test())
