#!/usr/bin/env python3
"""Что двухшаговый путь полосы сделал В БОЮ -- по журналу решений хоста.

ЗАЧЕМ. Признак жизни показывает только СЧЁТЧИК стадий (by_stage) и последнее
решение. Когда в счётчике появляется two_step_build, надо знать не "сколько
раз", а ПОЧЕМУ не отправилось: нет шаблона первого шага в кэше, шаблон старый,
налог котировки неизвестен, минимум не выдаётся, размер пакета, комиссия пула
выше потолка. Без этого первая живая двухшаговая сделка ждётся наугад.

Только чтение, журналы читаются потоком, в память не грузятся.
"""
import argparse
import calendar
import collections
import gzip
import statistics
import json
import os
import sys
import time

# ОТКАЗЫ "БЕЗ ВТОРОЙ НОГИ" (слово владельца 29.09, п.4). Ровно две причины, с
# которыми полоса не идёт за источником в не-SOL котировке: у пула котировка не
# SOL (второй ноги нет вовсе) и тип пула вне полосы (строителя нет). Строки
# ищутся ПОДСТРОКОЙ: в журнале рядом с ними стоит пояснение, и точное
# совпадение теряло бы записи.
БЕЗ_ВТОРОЙ_НОГИ = (("котировка пула не SOL", "котировка не SOL"),
                    ("тип пула вне полосы", "строителя нет"))
# Символ котировки берём ТОЛЬКО там, где адрес канонический и его можно
# назвать. Остальные идут адресом: выдумывать символ по адресу нельзя.
ИЗВЕСТНЫЕ_КОТИРОВКИ = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "So11111111111111111111111111111111111111112": "WSOL",
}

# Поля решения, которые нужны для разбора двухшагового пути. Больше не берём:
# журнал большой, а в отчёт уходит только это.
ПОЛЯ = ("ts_utc", "ts", "stage", "ok", "why_not", "route", "code", "reason",
        "mint", "source", "group", "lane_group", "pool_program",
        "leg1_pool_program", "quote_mint", "two_step", "two_step_why_not",
        "min_out", "expected_out", "leg1_min_out", "leg2_amount_in",
        "quote_fee_bps", "size", "signature", "pool_fee_share",
        "leg1_template_age_s", "spend_sol_eq")


def строки(путь: str):
    откр = gzip.open if путь.endswith(".gz") else open
    with откр(путь, "rt", encoding="utf-8", errors="replace") as ф:
        for ln in ф:
            ln = ln.strip()
            if ln.startswith("{"):
                try:
                    yield json.loads(ln)
                except ValueError:
                    continue


def файлы(каталог: str, имя: str) -> list:
    из_ = [os.path.join(каталог, имя)]
    из_ += sorted(os.path.join(каталог, ф) for ф in os.listdir(каталог)
                  if ф.startswith(имя + ".") and ф.endswith(".gz"))
    return [п for п in из_ if os.path.exists(п)]


def в_секунды(s: str) -> int:
    return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))


def возраст_свод(возрасты: list) -> dict:
    """Медиана, p90 и доля выше 3 слотов. Пустой список -- ноль и почему."""
    з = sorted(int(x) for x in возрасты)
    if not з:
        return {"решений": 0, "почему_нет": "поля slot_lag в решениях за окно нет"}

    def кв(q: float):
        и = min(len(з) - 1, max(0, int(round(q * (len(з) - 1)))))
        return з[и]

    выше3 = sum(1 for x in з if x > 3)
    выше10 = sum(1 for x in з if x > 10)
    return {"решений": len(з), "медиана": кв(0.5), "p90": кв(0.9),
             "p99": кв(0.99), "мин": з[0], "макс": з[-1],
             "выше_3_слотов": выше3, "доля_выше_3": round(выше3 / len(з), 6),
             "выше_10_слотов": выше10, "доля_выше_10": round(выше10 / len(з), 6)}


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default=os.environ.get("BLOOM_STATE_DIR")
                   or "/home/bot/bloom_executor_live_data")
    р.add_argument("--since-utc", required=True)
    р.add_argument("--podrobno", type=int, default=12)
    р.add_argument("--out", default="/tmp/dvuhshagovyy_v_zhurnale.json")
    р.add_argument("--kotirovki-s-cepi", type=int, default=1,
                    help="1 -- котировку отказанных сигналов читать с цепи")
    # ПОДПИСИ ЦЕЛИКОМ: по названной подписи печатаются ВСЕ строки журнала --
    # так видно, звали ли полосу вообще и на какой стадии она встала.
    р.add_argument("--podpisi", default="",
                    help="подписи источников через запятую (можно начало)")
    а = р.parse_args()
    порог = в_секунды(а.since_utc)

    искомые = tuple(x.strip() for x in (а.podpisi or "").split(",") if x.strip())
    по_подписям: dict = {}
    двухшаговые = []
    причины = collections.Counter()
    стадии = collections.Counter()
    по_группам_гейт = collections.Counter()
    коды = collections.Counter()
    # Пороги размера источника: по ним видно, ждём мы сигнала или он приходил
    # и не прошёл по размеру.
    вне_размера = []
    # Налоговые отказы: владелец просил время каждого и группу -- по ним видно,
    # действует ли allow_taxed_route после правки.
    налоговые = []
    # ВОЗРАСТ СИГНАЛА В СЛОТАХ (вопрос владельца 27.09, вечер): сколько слотов
    # между слотом сделки источника и нашим приёмом. Поле slot_lag пишет сам
    # детектор в момент решения -- тем же числом, которым сравнивает порог
    # STALE, поэтому отдельного счёта здесь нет.
    возрасты = []
    # БЕЗ ВТОРОЙ НОГИ: считаем по котировке, по типу пула источника и по типу
    # первой ноги, а трату источника складываем -- это масштаб пропущенного.
    без_ноги = collections.Counter()
    без_ноги_котировка = collections.Counter()
    без_ноги_пул = collections.Counter()
    без_ноги_первая_нога = collections.Counter()
    без_ноги_группа = collections.Counter()
    без_ноги_трата = 0.0
    без_ноги_трат_известно = 0
    без_ноги_минты = set()
    без_ноги_примеры = []
    строк = 0
    for путь in файлы(а.state_dir, "decisions.jsonl"):
        for з in строки(путь):
            строк += 1
            t = з.get("ts")
            if t is None and з.get("ts_utc"):
                try:
                    t = в_секунды(з["ts_utc"])
                except ValueError:
                    t = None
            if t is not None and float(t) < порог:
                continue
            ст = з.get("stage")
            if ст:
                стадии[ст] += 1
            if искомые:
                # ПОДПИСЬ ИСТОЧНИКА ЛЕЖИТ В ДВУХ ПОЛЯХ. В строке решения это
                # signature, а в строке ПОЛОСЫ -- source_sig: поле signature
                # там перезаписано НАШЕЙ подписью (или None до отправки).
                # Пока смотрели только signature, стадии полосы по сигналу
                # были невидимы, и выходило "полосу не звали".
                п_ = str(з.get("signature") or "")
                ист_ = str(з.get("source_sig") or "")
                for иск in искомые:
                    if п_.startswith(иск) or ист_.startswith(иск):
                        по_подписям.setdefault(иск, []).append(
                            {к: v for к, v in з.items()
                             if к in ("ts_utc", "stage", "ok", "action", "code",
                                       "source_sig", "size_sol", "lamports",
                                       "reason", "why_not", "group", "lane",
                                       "lane_allowed", "mint", "source",
                                       "spend_sol_eq", "dokupka", "dokupka_why",
                                       "pool_program", "signature", "two_step_why_not")})
            лаг = з.get("slot_lag")
            if isinstance(лаг, int) and з.get("action") is not None:
                возрасты.append(лаг)
            код = з.get("code")
            if код:
                коды[код] += 1
            if код == "SKIP_TAXED_ROUTE" and len(налоговые) < 60:
                налоговые.append({к: з.get(к) for к in
                                  ("ts_utc", "group", "lane_group", "source",
                                   "mint", "why_not", "reason") if з.get(к) is not None})
            if код == "TARGET_AMOUNT_OUT_OF_RANGE" and len(вне_размера) < 40:
                вне_размера.append({к: з.get(к) for к in
                                     ("ts_utc", "source", "group", "lane_group",
                                      "spend_sol_eq", "mint") if з.get(к) is not None})
            если_гейт = (ст == "gate" and з.get("ok") is False)
            if если_гейт:
                по_группам_гейт[str(з.get("why_not"))[:120]] += 1
            # --- БЕЗ ВТОРОЙ НОГИ
            текст = " ".join(str(з.get(к) or "") for к in
                              ("why_not", "reason", "two_step_why_not", "pool_why_not"))
            for подстрока, имя in БЕЗ_ВТОРОЙ_НОГИ:
                if подстрока not in текст:
                    continue
                без_ноги[имя] += 1
                кв = з.get("quote_mint") or з.get("spend_mint") or "не назван"
                без_ноги_котировка[f"{имя} | {ИЗВЕСТНЫЕ_КОТИРОВКИ.get(кв, кв)}"] += 1
                без_ноги_пул[f"{имя} | {з.get('pool_program') or 'не назван'}"] += 1
                без_ноги_первая_нога[
                    f"{имя} | {з.get('leg1_pool_program') or 'первая нога не выбрана'}"] += 1
                без_ноги_группа[f"{имя} | {з.get('lane_group') or з.get('group') or 'не названа'}"] += 1
                тр = з.get("spend_sol_eq")
                if isinstance(тр, (int, float)):
                    без_ноги_трата += float(тр)
                    без_ноги_трат_известно += 1
                if з.get("mint"):
                    без_ноги_минты.add(з["mint"])
                # ВСЕ ОТКАЗЫ, А НЕ ПЕРВЫЕ 15, и с подписью источника: по ней
                # котировка пула берётся С ЦЕПИ. В самой строке отказа поля
                # quote_mint нет -- отказ случается ДО того, как котировка
                # попадает в запись, и прогон 02:17Z честно показал "не назван"
                # у всех 52. Спрашивать журнал бесполезно, спрашиваем цепь.
                без_ноги_примеры.append({к: з.get(к) for к in
                                          ("ts_utc", "source", "mint", "quote_mint",
                                            "spend_mint", "pool_program",
                                            "leg1_pool_program", "lane_group",
                                            "group", "spend_sol_eq", "why_not",
                                            "reason", "signature", "source_sig",
                                            "source_pool") if з.get(к) is not None})
                break

            двух = (ст == "two_step_build" or з.get("route") == "two_step"
                    or з.get("two_step") is not None
                    or з.get("two_step_why_not") is not None)
            if not двух:
                continue
            зап = {к: з.get(к) for к in ПОЛЯ if з.get(к) is not None}
            двухшаговые.append(зап)
            почему = (зап.get("two_step_why_not")
                      or ((зап.get("two_step") or {}).get("why_not")
                          if isinstance(зап.get("two_step"), dict) else None)
                      or зап.get("why_not") or ("ОТПРАВЛЕНО" if зап.get("signature")
                                                 else "причина не названа"))
            причины[str(почему)[:200]] += 1

    # СКОРОСТЬ "СИГНАЛ -> ОТПРАВКА" ПО ЧАСАМ. Ноль -- приход транзакции
    # источника на наш узел (signal_recv_ts пишет детектор в позицию полосы),
    # конец -- наш sendTransaction (ts_sent). Разбивка по часам нужна, чтобы
    # видеть "до и после правок" без отдельного параметра: правки видны по
    # времени деплоя.
    по_часам: dict = collections.defaultdict(list)
    все_мс = []
    # КАКИЕ ПОЛЯ ВРЕМЕНИ ВООБЩЕ ЕСТЬ. Считаем присутствие каждого кандидата:
    # молча вернуть "0 сделок" и назвать это замером нельзя.
    поля_времени = collections.Counter()
    позиций_полосы = 0
    # Пары "ноль -> конец" по убыванию полноты замера. Первая, где есть оба
    # числа, и идёт в счёт; название пары -- в отчёт, чтобы было видно, ЧТО
    # именно замерено.
    ПАРЫ = (("signal_recv_ts", "ts_sent", "сигнал -> отправка"),
            ("t_recv_ts", "ts_sent", "сигнал -> отправка"),
            ("ts_intent", "ts_sent", "решение -> отправка"),
            ("ts_intent", "ts_accepted", "решение -> приём"))
    чем_мерили = collections.Counter()
    # СТРОКИ ЖУРНАЛА -- ЭТО ОБНОВЛЕНИЯ ОДНОЙ ПОЗИЦИИ, а не готовые записи:
    # ts_intent приходит одной строкой, ts_sent -- другой. Мерить по строке
    # значит не найти ни одной пары (первый прогон так и вышло: 0 сделок при
    # 205 строках с числами). Поэтому сначала сводим строки по cid.
    по_cid: dict = {}
    for путь in файлы(а.state_dir, "positions.jsonl"):
        for з in строки(путь):
            cid = з.get("client_order_id")
            if not cid:
                continue
            зап = по_cid.setdefault(cid, {})
            for к, v in з.items():
                if v is not None:
                    зап[к] = v
    for зап in по_cid.values():
        if not зап.get("lane"):
            continue
        позиций_полосы += 1
        for к in ("signal_recv_ts", "t_recv_ts", "ts_intent", "ts_sent",
                  "ts_accepted", "seen_lag_ms"):
            if isinstance(зап.get(к), (int, float)):
                поля_времени[к] += 1
        for ноль_к, конец_к, имя in ПАРЫ:
            ноль, ушло = зап.get(ноль_к), зап.get(конец_к)
            if not isinstance(ноль, (int, float)) or not isinstance(ушло, (int, float)):
                continue
            if float(ноль) < порог:
                break
            мс = (float(ушло) - float(ноль)) * 1000.0
            if not -1000 < мс < 600_000:
                break
            час = time.strftime("%Y-%m-%dT%HZ", time.gmtime(float(ноль)))
            по_часам[час].append(мс)
            все_мс.append(мс)
            чем_мерили[f"{ноль_к} -> {конец_к} ({имя})"] += 1
            break
    # ДИАГНОСТИКА: какие поля времени реально лежат у СВЕЖИХ позиций полосы.
    свежие = sorted((з for з in по_cid.values() if з.get("lane")),
                    key=lambda з: str(з.get("ts_intent_utc") or ""), reverse=True)[:3]
    примеры_времени = [{к: v for к, v in з.items()
                        if ("ts" in к or "signal" in к or "recv" in к
                            or "lag" in к or к == "lane_group")}
                       for з in свежие]

    скорость = {ч: {"сделок": len(v), "медиана_мс": round(statistics.median(v), 1),
                    "мин_мс": round(min(v), 1), "макс_мс": round(max(v), 1)}
                for ч, v in sorted(по_часам.items())}

    # --- КОТИРОВКА ОТКАЗАННЫХ СИГНАЛОВ -- С ЦЕПИ (слово владельца 29.09, п.4:
    # разбить по котировке). В журнале её нет, поэтому по подписи источника
    # читаем его транзакцию и берём минт котировки тем же кодом, которым это
    # делает служба. Только чтение; если узла или модуля нет -- честный отказ,
    # а не пустой разрез.
    котировки_с_цепи = collections.Counter()
    не_прочитано = collections.Counter()
    if а.kotirovki_s_cepi:
        зов_ = None
        try:
            import urllib.request  # noqa: PLC0415

            ключ = (os.environ.get("HELIUS_API_KEY")
                    or os.environ.get("HELIUS_API") or "").strip()
            урл_ = f"https://mainnet.helius-rpc.com/?api-key={ключ}"

            def зов_(метод, параметры, таймаут=20.0):  # noqa: F811
                тело = json.dumps({"jsonrpc": "2.0", "id": 1, "method": метод,
                                    "params": параметры}).encode()
                зпр = urllib.request.Request(
                    урл_, data=тело, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(зпр, timeout=таймаут) as отв:  # noqa: S310
                    о = json.loads(отв.read().decode())
                return о.get("result")
        except Exception as exc:  # noqa: BLE001
            не_прочитано[f"узел недоступен: {type(exc).__name__}"] += 1
        B = None
        try:
            for пт in (os.environ.get("BLOOM_CODE_DIR") or "", "/home/bot/bloom_executor"):
                if пт and os.path.isdir(пт) and пт not in sys.path:
                    sys.path.insert(0, пт)
            import c2_swap_build as B  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            не_прочитано[f"модуля сборки нет: {type(exc).__name__}"] += 1
        видели = set()
        for з in без_ноги_примеры:
            подпись = з.get("signature") or з.get("source_sig")
            прог = з.get("pool_program")
            имя = ("котировка не SOL" if "котировка пула не SOL" in
                   f"{з.get('why_not')} {з.get('reason')}" else "строителя нет")
            if not подпись or not прог or not зов_ or B is None:
                не_прочитано["подписи, программы или модуля нет"] += 1
                continue
            if подпись in видели:
                continue
            видели.add(подпись)
            try:
                tx = зов_("getTransaction",
                          [подпись, {"encoding": "jsonParsed",
                                      "maxSupportedTransactionVersion": 0,
                                      "commitment": "finalized"}])
            except Exception as exc:  # noqa: BLE001
                не_прочитано[f"транзакция не прочиталась: {type(exc).__name__}"] += 1
                continue
            if not tx:
                не_прочитано["узел вернул пусто"] += 1
                continue
            найдено = None
            try:
                инстр = [ix for ix in B.all_instructions(tx)
                         if ix.get("programId") == прог]
                for канд in (инстр[0]["accounts"] if инстр else []):
                    т = B.extract_template(tx, прог, канд)
                    if not т.get("ok"):
                        continue
                    mv = B.mints_and_vaults(т, tx)
                    if mv and mv.get("quote_mint"):
                        найдено = mv["quote_mint"]
                        break
            except Exception as exc:  # noqa: BLE001
                не_прочитано[f"шаблон не восстановился: {type(exc).__name__}"] += 1
                continue
            if not найдено:
                не_прочитано["котировка пула не восстановилась"] += 1
                continue
            з["quote_mint_s_cepi"] = найдено
            котировки_с_цепи[f"{имя} | {ИЗВЕСТНЫЕ_КОТИРОВКИ.get(найдено, найдено)}"] += 1

    двухшаговые.sort(key=lambda з: str(з.get("ts_utc") or ""))
    отчёт = {
        "since_utc": а.since_utc,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "строк_прочитано": строк,
        "двухшаговых_решений": len(двухшаговые),
        "причины_двухшагового": dict(причины.most_common()),
        "стадии": dict(стадии.most_common(12)),
        "коды": dict(коды.most_common(12)),
        "отказы_гейта": dict(по_группам_гейт.most_common(10)),
        "без_второй_ноги": {
            "всего_по_причине": dict(без_ноги.most_common()),
            "по_котировке": dict(без_ноги_котировка.most_common(20)),
            "по_типу_пула_источника": dict(без_ноги_пул.most_common(20)),
            "по_типу_первой_ноги": dict(без_ноги_первая_нога.most_common(20)),
            "по_группе": dict(без_ноги_группа.most_common(20)),
            "разных_минтов": len(без_ноги_минты),
            "трата_источников_sol_экв": round(без_ноги_трата, 9),
            "записей_с_тратой": без_ноги_трат_известно,
            "что_это_число": ("сумма трат ИСТОЧНИКОВ по отказанным сигналам в "
                               "SOL-эквиваленте. Это масштаб пропущенного, а НЕ "
                               "наша упущенная прибыль: итог по этим минтам "
                               "считается по цепи отдельно."),
            "по_котировке_с_цепи": dict(котировки_с_цепи.most_common(30)),
            "котировка_с_цепи_не_прочитана": dict(не_прочитано.most_common(10)),
            "примеры": без_ноги_примеры[:60],
        },
        "налоговые_отказы": налоговые,
        "возраст_сигнала_слотов": возраст_свод(возрасты),
        "по_подписям": {к: v[:20] for к, v in по_подписям.items()},
        "последние": двухшаговые[-а.podrobno:],
        "вне_размера_примеры": вне_размера[:12],
        "скорость_сигнал_отправка_по_часам": скорость,
        "скорость_медиана_мс": (round(statistics.median(все_мс), 1)
                                 if все_мс else None),
        "скорость_сделок": len(все_мс),
        "скорость_чем_мерили": dict(чем_мерили),
        "позиций_полосы_прочитано": позиций_полосы,
        "поля_времени_в_позициях": dict(поля_времени),
        "примеры_времени_свежих": примеры_времени,
    }
    with open(а.out, "w", encoding="utf-8") as ф:
        json.dump(отчёт, ф, ensure_ascii=False, indent=1)
    print(json.dumps({к: v for к, v in отчёт.items()
                      if к not in ("последние", "вне_размера_примеры")},
                     ensure_ascii=False, indent=1))
    for з in отчёт["последние"]:
        print("  ", json.dumps(з, ensure_ascii=False)[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
