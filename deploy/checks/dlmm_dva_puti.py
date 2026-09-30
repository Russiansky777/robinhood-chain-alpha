#!/usr/bin/env python3
"""DLMM: два пути цены рядом -- одно чтение против двух. Живые покупки, без отправки.

ЗАЧЕМ. Владелец 30.09 (п.3): принять "DLMM одним чтением" Code-3 на живых покупках --
три числа и РАВЕНСТВО ЦЕН двух путей, плюс замер времени на хосте. Code-3 равенство цен
измерить не мог: сети у него нет, а снимка состояния пула DLMM того вида, что у CLMM и
Whirlpool, в репозитории нет. На хосте сеть есть -- значит меряем здесь.

КАК СРАВНИВАЕТСЯ ЧЕСТНО. Оба пути зовутся ПОДРЯД в одном процессе на одной и той же
живой покупке, и каждый сам говорит, из какого СЛОТА он прочитал состояние. Сравнение
цены считается строгим ТОЛЬКО когда слоты совпали: DLMM -- пул с активной корзиной,
между двумя чтениями цена меняется по-настоящему, и объявлять такое расхождение
"ошибкой пути" было бы ложью. Строки с разными слотами считаются отдельной графой.

ТОЛЬКО ЧТЕНИЕ. Ни подписи, ни отправки, ни симуляции: считается цена. Ключ Helius
берётся как у прочих проверок (свой ключ зондов, если задан).
"""
import argparse
import json
import os
import statistics as st
import sys
import time

КОД = os.environ.get("BLOOM_CODE_DIR") or "/home/bot/bloom_executor"
if КОД not in sys.path:
    sys.path.insert(0, КОД)
ПРОВЕРКИ = os.path.dirname(os.path.abspath(__file__))
if ПРОВЕРКИ not in sys.path:
    sys.path.insert(0, ПРОВЕРКИ)

DLMM_ПРОГРАММА = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
ЛАМПОРТОВ_В_SOL = 1_000_000_000


def сравнение(а: dict, б: dict) -> dict:
    """Что можно сказать о двух ответах. Строго -- только при равных слотах."""
    из_ = {"оба_ok": bool(а.get("ok") and б.get("ok")),
            "слот_одно": а.get("slot"), "слот_два": б.get("slot"),
            "слоты_совпали": None, "min_out_совпал": None,
            "expected_out_совпал": None, "разница_min_out": None,
            "разница_бп": None}
    if not из_["оба_ok"]:
        return из_
    из_["слоты_совпали"] = (а.get("slot") is not None
                             and а.get("slot") == б.get("slot"))
    м1, м2 = а.get("min_out"), б.get("min_out")
    о1, о2 = а.get("expected_out"), б.get("expected_out")
    if isinstance(м1, int) and isinstance(м2, int):
        из_["min_out_совпал"] = (м1 == м2)
        из_["разница_min_out"] = м1 - м2
        if м2:
            из_["разница_бп"] = round((м1 - м2) / м2 * 10_000, 2)
    if isinstance(о1, int) and isinstance(о2, int):
        из_["expected_out_совпал"] = (о1 == о2)
    return из_


def свежие_из_журнала(state_dir: str, программа: str, *, ждать_с: float,
                       сколько: int, печать=print):
    """Ждать НОВЫХ строк журнала с этой программой и отдавать их по одной.

    ЗАЧЕМ. Строки, взятые из журнала как есть, могут быть старше на тысячи
    слотов, а массивы корзин берутся из сделки источника: на такой строке путь
    одного чтения обязан отказать, и это ничего не говорит о бою. Здесь ждём
    строк, ПОЯВИВШИХСЯ ПОСЛЕ старта, -- у них отставание такое же, как у полосы.

    Журнал читается ПОТОКОМ от запомненного смещения: в память он не берётся.
    """
    import glob  # noqa: PLC0415

    пути = sorted(glob.glob(os.path.join(state_dir, "decisions*.jsonl")),
                   reverse=True)
    if not пути:
        печать(f"журнала decisions*.jsonl в {state_dir} нет -- ждать нечего")
        return
    путь = пути[0]
    смещение = os.path.getsize(путь)
    печать(f"ждём новых строк {os.path.basename(путь)} со смещения {смещение}, "
            f"до {ждать_с:.0f} с или {сколько} строк")
    начало = time.time()
    видели = set()
    отдано = 0
    while time.time() - начало < ждать_с and отдано < сколько:
        time.sleep(2.0)
        try:
            размер = os.path.getsize(путь)
        except OSError:
            continue
        if размер <= смещение:
            continue
        with open(путь, "r", encoding="utf-8", errors="replace") as ф:
            ф.seek(смещение)
            куски = ф.read()
            смещение = ф.tell()
        for стр in куски.split("\n"):
            стр = стр.strip()
            if not стр or программа not in стр:
                continue
            try:
                зап = json.loads(стр)
            except ValueError:
                continue
            п = зап.get("signature") or зап.get("source_sig")
            if not п or п in видели:
                continue
            видели.add(п)
            отдано += 1
            yield {"подпись": п, "минт": зап.get("mint"),
                    "источник": зап.get("source"), "откуда": "свежее",
                    "кандидаты": [], "tx": None, "stage": зап.get("stage")}
            if отдано >= сколько:
                return
    печать(f"ожидание кончилось: новых строк с программой отдано {отдано}")


def главное(а) -> int:
    import bloom_own_send as OS  # noqa: PLC0415
    import c2_common as C  # noqa: PLC0415
    import c2_dlmm_odno_chtenie as OD  # noqa: PLC0415
    import c2_dlmm_tochnaya_kotirovka as K  # noqa: PLC0415
    import c2_pool_programs as PP  # noqa: PLC0415
    # МЕТКИ ПУЛОВ -- ОТ ТОГО ЖЕ МОДУЛЯ, ЧТО У ПОЛОСЫ. Первый заход звал
    # PP.pool_program(tx, vault, {}) с пустыми метками, и программа выходила None
    # у всех 14 строк ("это не DLMM, а None"): без меток модуль тип не узнаёт.
    import c2_shadow_build as SB  # noqa: PLC0415
    import c2_swap_build as B  # noqa: PLC0415
    import priemka_stroitelya as P  # noqa: PLC0415

    if а.zhdat:
        найдено = {"строки": [], "по_источникам": {"свежее": 0}, "отказы": {}}
        for нч in свежие_из_журнала(а.state_dir, DLMM_ПРОГРАММА,
                                     ждать_с=float(а.zhdat), сколько=а.skolko):
            найдено["строки"].append(нч)
            найдено["по_источникам"]["свежее"] += 1
    else:
        найдено = P.живые_покупки(DLMM_ПРОГРАММА, сколько=а.skolko, откуда=а.otkuda,
                                   сборы=а.sbory, state_dir=а.state_dir,
                                   с_минтом=(а.tolko_s_mintom or ""))
    print(f"живых покупок DLMM: {len(найдено['строки'])} "
           + json.dumps(найдено.get("по_источникам") or {}, ensure_ascii=False))
    for к, v in (найдено.get("отказы") or {}).items():
        print(f"  источник {к} не дал строк: {v}")
    # ФЛАГ ВКЛЮЧАЕМ ТОЛЬКО ВНУТРИ ЭТОГО ПРОЦЕССА: окружение службы не трогаем.
    os.environ[OD.ИМЯ_ФЛАГА] = "1"
    os.environ.pop(OD.ИМЯ_ФЛАГА_ГРУППЫ, None)
    print(f"флаг {OD.ИМЯ_ФЛАГА}=1 только в этом процессе; окно индексов "
           f"{OD.ОКНО_ИНДЕКСОВ}; порт цены есть: {OD.порт_есть()}")

    строки, мс1, мс2 = [], [], []
    for и, нч in enumerate(найдено["строки"], 1):
        стр = {"подпись": нч.get("подпись"), "минт": нч.get("минт"),
                "источник": нч.get("источник"), "откуда": нч.get("откуда"),
                "why_not": None}
        def пропуск(причина: str):
            """Строка не дошла до двух путей -- сказать почему, а не промолчать."""
            стр["why_not"] = причина
            строки.append(стр)
            print(f"  {и}/{len(найдено['строки'])} {str(стр['подпись'])[:14]} "
                   f"[{стр['откуда']}] НЕ ДОШЛА: {причина[:140]}")

        tx = нч.get("tx")
        if not isinstance(tx, dict):
            о = P.зов("getTransaction", [нч["подпись"], {
                "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1,
                "commitment": "confirmed"}])
            tx = (о.get("result") if о.get("ok") else None)
            if not isinstance(tx, dict):
                пропуск(f"транзакция не прочиталась: {о.get('why_not')}")
                continue
        пул = C.identify_pool(tx, нч.get("источник") or "", нч.get("минт") or "")
        if not пул.get("ok"):
            пропуск(f"пул: {пул.get('why_not')}")
            continue
        прог = PP.pool_program(tx, пул["pool_vault"],
                                SB._labels()).get("pool_program")
        стр["pool_program"] = прог
        if прог != DLMM_ПРОГРАММА:
            пропуск(f"это не DLMM, а {прог}")
            continue
        шаб = None
        порядок = [x for x in (нч.get("кандидаты") or []) if isinstance(x, str)]
        порядок.append(пул["pool_vault"])
        for кандидат in порядок:
            т = B.extract_template(tx, DLMM_ПРОГРАММА, кандидат)
            if т.get("ok"):
                шаб = т
                break
        if шаб is None:
            пропуск("шаблон инструкции DLMM не восстановился")
            continue
        mv = B.mints_and_vaults(шаб, tx) or {}
        стр["база"] = mv.get("base_mint")
        стр["минт_котировки"] = mv.get("quote_mint")
        # ОТСТАВАНИЕ В СЛОТАХ -- ГЛАВНОЕ ЧИСЛО ЭТОЙ ПРОВЕРКИ. Массивы берутся из
        # сделки источника, и чем старше сделка, тем вернее активная корзина
        # ушла. В бою полоса считает цену через слот-два после источника; строка
        # из журнала может быть старше на тысячи слотов, и мерить по ней "село"
        # значит мерить не путь, а давность строки.
        стр["слот_источника"] = tx.get("slot")

        def зов_узла(метод, парам):
            return P.зов(метод, парам, таймаут=12.0)

        т0 = time.perf_counter()
        путь1 = OS.массивы_dlmm_сейчас(
            B, шаб, mv, лампорты=а.lamportov, проскальзывание=а.proskalzyvanie,
            rpc_call=зов_узла,
            котировщик=OD.котировщик_для(tx, откат=None, группа=None))
        мс_1 = round((time.perf_counter() - т0) * 1000, 1)
        т0 = time.perf_counter()
        путь2 = OS.массивы_dlmm_сейчас(
            B, шаб, mv, лампорты=а.lamportov, проскальзывание=а.proskalzyvanie,
            rpc_call=зов_узла, котировщик=K.котировка)
        мс_2 = round((time.perf_counter() - т0) * 1000, 1)
        стр["одно_чтение"] = {к: путь1.get(к) for к in (
            "ok", "why_not", "min_out", "expected_out", "чтений", "slot",
            "корзин", "active_id", "массивов_нужно", "не_влезло")}
        стр["два_чтения"] = {к: путь2.get(к) for к in (
            "ok", "why_not", "min_out", "expected_out", "чтений", "slot",
            "корзин", "active_id", "массивов_нужно", "не_влезло")}
        стр["мс_одно"] = мс_1
        стр["мс_два"] = мс_2
        стр["сравнение"] = сравнение(стр["одно_чтение"], стр["два_чтения"])
        прочит = (стр["одно_чтение"].get("slot")
                   or стр["два_чтения"].get("slot"))
        стр["отставание_слотов"] = (
            (int(прочит) - int(стр["слот_источника"]))
            if isinstance(прочит, int) and isinstance(стр.get("слот_источника"), int)
            else None)
        мс1.append(мс_1)
        мс2.append(мс_2)
        строки.append(стр)
        с = стр["сравнение"]
        print(f"  {и}/{len(найдено['строки'])} {str(стр['подпись'])[:14]} "
               f"одно: ok={стр['одно_чтение']['ok']} чтений="
               f"{стр['одно_чтение']['чтений']} slot={стр['одно_чтение']['slot']} "
               f"min_out={стр['одно_чтение']['min_out']} ({мс_1} мс) | "
               f"два: ok={стр['два_чтения']['ok']} чтений="
               f"{стр['два_чтения']['чтений']} slot={стр['два_чтения']['slot']} "
               f"min_out={стр['два_чтения']['min_out']} ({мс_2} мс) | "
               f"слоты_совпали={с['слоты_совпали']} min_out_совпал="
               f"{с['min_out_совпал']} разница_бп={с['разница_бп']} "
               f"отставание={стр['отставание_слотов']} слотов")
        for имя in ("одно_чтение", "два_чтения"):
            if стр[имя].get("why_not"):
                print(f"      {имя}: {str(стр[имя]['why_not'])[:150]}")

    село1 = sum(1 for с in строки if (с.get("одно_чтение") or {}).get("ok"))
    село2 = sum(1 for с in строки if (с.get("два_чтения") or {}).get("ok"))
    оба = [с for с in строки if (с.get("сравнение") or {}).get("оба_ok")]
    равные_слоты = [с for с in оба if с["сравнение"]["слоты_совпали"]]
    цена_сошлась = [с for с in равные_слоты if с["сравнение"]["min_out_совпал"]]
    итог = {
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "программа": DLMM_ПРОГРАММА, "трата_sol": а.lamportov / ЛАМПОРТОВ_В_SOL,
        "проскальзывание": а.proskalzyvanie,
        "окно_индексов": OD.ОКНО_ИНДЕКСОВ,
        "найдено_живых": len(найдено["строки"]),
        "источники": найдено.get("по_источникам"),
        "три_числа_одно_чтение": {
            "село": село1,
            "отказы": sum(1 for с in строки
                           if (с.get("одно_чтение") or {}).get("ok") is False),
            "не_дошло": sum(1 for с in строки if not с.get("одно_чтение"))},
        "три_числа_два_чтения": {
            "село": село2,
            "отказы": sum(1 for с in строки
                           if (с.get("два_чтения") or {}).get("ok") is False),
            "не_дошло": sum(1 for с in строки if not с.get("два_чтения"))},
        "по_отставанию": _по_отставанию(строки),
        "цены": {"оба_пути_дали_цену": len(оба),
                  "из_них_один_слот": len(равные_слоты),
                  "цена_совпала_до_единицы": len(цена_сошлась),
                  "разницы_бп_при_разных_слотах": [
                      с["сравнение"]["разница_бп"] for с in оба
                      if not с["сравнение"]["слоты_совпали"]][:12]},
        "мс": {"одно_p50": (round(st.median(мс1), 1) if мс1 else None),
                "два_p50": (round(st.median(мс2), 1) if мс2 else None),
                "одно_max": (max(мс1) if мс1 else None),
                "два_max": (max(мс2) if мс2 else None),
                "замеров": len(мс1)},
        "чтений": {
            "одно_p50": _p50([(с.get("одно_чтение") or {}).get("чтений")
                               for с in строки]),
            "два_p50": _p50([(с.get("два_чтения") or {}).get("чтений")
                              for с in строки])},
        "почему_не_дошли": _причины(строки),
        "строки": строки,
    }
    печать = {к: v for к, v in итог.items() if к != "строки"}
    print(json.dumps(печать, ensure_ascii=False, indent=1))
    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump(итог, ф, ensure_ascii=False, indent=1)
        print(f"отчёт записан: {а.out}")
    return 0


ПОЛОСЫ_ОТСТАВАНИЯ = ((0, 3, "0-3 слота (как в бою)"), (4, 10, "4-10 слотов"),
                      (11, 100, "11-100 слотов"), (101, 10**9, "больше 100 слотов"))


def _по_отставанию(строки: list) -> dict:
    """Село/отказало по ВОЗРАСТУ строки. Полоса в бою считает цену через слот-два.

    Без этой разбивки "село 2 из 13" читалось бы как изъян пути, тогда как
    отказ на строке возрастом в тысячи слотов -- ровно то, для чего проверка
    активного массива и поставлена.
    """
    из_ = {}
    for н, в, имя in ПОЛОСЫ_ОТСТАВАНИЯ:
        ряд = [с for с in строки
               if isinstance(с.get("отставание_слотов"), int)
               and н <= с["отставание_слотов"] <= в]
        если_оба = [с for с in ряд if (с.get("сравнение") or {}).get("оба_ok")]
        из_[имя] = {
            "строк": len(ряд),
            "село_одно": sum(1 for с in ряд
                              if (с.get("одно_чтение") or {}).get("ok")),
            "село_два": sum(1 for с in ряд
                             if (с.get("два_чтения") or {}).get("ok")),
            "цена_совпала": sum(1 for с in если_оба
                                 if с["сравнение"].get("min_out_совпал")),
            "из_них_один_слот": sum(1 for с in если_оба
                                     if с["сравнение"].get("слоты_совпали"))}
    без = [с for с in строки if с.get("одно_чтение")
            and not isinstance(с.get("отставание_слотов"), int)]
    из_["отставание неизвестно"] = {"строк": len(без)}
    return из_


def _причины(строки: list) -> dict:
    """Счётчик причин, по которым строка не дошла до двух путей."""
    из_ = {}
    for с in строки:
        if с.get("одно_чтение"):
            continue
        т = (с.get("why_not") or "без причины")[:120]
        из_[т] = из_.get(т, 0) + 1
    return dict(sorted(из_.items(), key=lambda x: -x[1]))


def _p50(ряд):
    ч = sorted(x for x in ряд if isinstance(x, (int, float)))
    return (st.median(ч) if ч else None)


def self_test() -> int:
    """Только арифметика сравнения: ни сети, ни модулей полосы."""
    всего = [0, 0]

    def chk(имя, условие, факт=None):
        всего[0] += 1
        if условие:
            всего[1] += 1
            print(f"  [ok  ] {имя}")
        else:
            print(f"  [ПЛОХО] {имя} -- {факт}")

    а = {"ok": True, "slot": 100, "min_out": 1000, "expected_out": 1500}
    б = {"ok": True, "slot": 100, "min_out": 1000, "expected_out": 1500}
    с = сравнение(а, б)
    chk("один слот и равная цена -- совпало",
        с["слоты_совпали"] and с["min_out_совпал"] and с["разница_бп"] == 0.0, с)
    # РАЗНЫЕ СЛОТЫ -- НЕ ОШИБКА ПУТИ. Между чтениями активная корзина уходит, и
    # называть это расхождением путей было бы ложью.
    с2 = сравнение(а, {"ok": True, "slot": 101, "min_out": 990,
                        "expected_out": 1490})
    chk("разные слоты помечены, разница названа в базисных пунктах",
        с2["слоты_совпали"] is False and с2["разница_бп"] == round(10/990*10000, 2),
        с2)
    chk("один путь не дал цены -- сравнения нет вовсе",
        сравнение(а, {"ok": False})["оба_ok"] is False)
    chk("слот неизвестен -- слоты НЕ считаются совпавшими",
        сравнение({"ok": True, "slot": None, "min_out": 1},
                   {"ok": True, "slot": None, "min_out": 1})["слоты_совпали"]
        is False)
    chk("медиана пустого ряда -- None, а не ноль", _p50([None, "x"]) is None)
    chk("медиана считается по числам", _p50([1, 3, 2]) == 2)
    print(f"самопроверка двух путей DLMM: {всего[1]}/{всего[0]}"
           f"{' пройдено' if всего[1] == всего[0] else ' ПРОВАЛ'}")
    return 0 if всего[1] == всего[0] else 1


def main() -> int:
    р = argparse.ArgumentParser(description=__doc__)
    р.add_argument("--skolko", type=int, default=12)
    р.add_argument("--lamportov", type=int, default=10_000_000)
    р.add_argument("--proskalzyvanie", type=float, default=0.35)
    р.add_argument("--otkuda", default="zhurnal,obrazcy,sbor")
    р.add_argument("--sbory", default="data/podbivka")
    р.add_argument("--state-dir", dest="state_dir",
                   default="/home/bot/bloom_executor_live_data")
    р.add_argument("--tolko-s-mintom", dest="tolko_s_mintom", default="")
    р.add_argument("--zhdat", type=float, default=0.0,
                   help="ждать СВЕЖИХ строк журнала столько секунд (0 -- брать "
                        "готовые строки; свежие мерят путь как в бою)")
    р.add_argument("--out", default=None)
    р.add_argument("--self-test", dest="self_test", action="store_true")
    а = р.parse_args()
    if а.self_test:
        return self_test()
    return главное(а)


if __name__ == "__main__":
    raise SystemExit(main())
