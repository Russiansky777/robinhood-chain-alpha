#!/usr/bin/env python3
"""Суточный доклад и живая сверка: журналы детектора -> цифры владельцу.

Что здесь считается и почему именно так:

* Разбор из сообщения и разбор через RPC -- ДВЕ разные группы. Второй
  структурно поздний: getTransaction отдаёт только confirmed, это 1-2
  слота. Складывать их задержки в одну медиану значит прятать отставание
  в среднем, поэтому распределение слотов печатается по каждой группе
  отдельно.
* Наши собственные лимиты (дубль минта, предел открытых позиций, баланс)
  НЕ идут в числитель расхождений с DBot: у задач таких фильтров нет,
  сравнивать тут нечего. Они выносятся отдельной строкой "пропущено по
  нашему лимиту, DBot купил" -- ровно как просил владелец.
* Решения по тестовому источнику стенда не входят ни в сверку, ни в
  счётчик 30 сигналов, ни в пары А/Б: это наш собственный кошелёк, он
  не проверяет способность угадывать чужие сделки.
* Позиции dry-run не учитываются нигде, кроме одной строки "отложено":
  за ними нет ни одной сделки в цепи.
* Записи прежнего формата (schema_version 1, ключи кириллицей) в счёт не
  берутся и называются числом: смешать два формата в одной сводке значит
  посчитать половину и не заметить этого.

Скрипт только читает: журналы, состояние, и -- если дан ключ -- записи
follow у DBot методом GET. Ничего не пишет, кроме своего отчёта.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bloom_exec_state as ST  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_JSON = REPO_ROOT / "data" / "bloom_report.json"
OUT_TEXT = REPO_ROOT / "data" / "bloom_report.txt"

# Гейты владельца, заменившие календарь (23.09).
ГЕЙТ_СИГНАЛОВ = 30          # сигналов dbot_бы_купил в живой сверке
ГЕЙТ_СОВПАДЕНИЕ = 0.90      # доля совпадений с DBot
ГЕЙТ_БАЛАНС_SOL = 2.0       # кошелёк перед live на реальных источниках
ГЕЙТ_ПАР = 50               # пар для решения об эксперименте

# Размер входа, при котором пары идут в зачёт слотов, но не разницы
# результата: 0.05 SOL против 0.5 у DBot -- это разные объёмы, и разницу
# цены на них сравнивать нельзя.
РАЗМЕР_БЕЗ_РАЗНИЦЫ_SOL = 0.2

РАЗБОР_ГРУППЫ = ("PARSE_VIA_MSG", "PARSE_VIA_RPC")

# Коды, которые ставит НАШ лимит, а не воспроизведение фильтров задачи.
НАШИ_ЛИМИТЫ = {ST.КОД_ДУБЛЬ_МИНТА, ST.КОД_ПОКУПОК_НА_МИНТ,
               ST.КОД_ЛИМИТ_ОТКРЫТЫХ, ST.КОД_БАЛАНС, ST.КОД_РУБИЛЬНИК,
               ST.КОД_ПАУЗА_API, ST.КОД_ПАУЗА_НЕПРОДАНО, ST.КОД_ПАУЗА_429,
               ST.КОД_БЮДЖЕТ_НЕДЕЛИ, ST.КОД_ДНЕВНОЙ_УБЫТОК,
               ST.КОД_ПОДПИСЬ_ВИДЕЛИ, ST.КОД_СЧЁТЧИКИ_БИТЫ,
               "INTERMEDIATE_ROUTE"}


# ------------------------------------------------------------ чтение журнала

def читать_решения(путь: Path, *, since_ts: float | None = None) -> tuple[list, dict]:
    """Строки решений нового формата + честный счёт того, что не взято."""
    итог = {"v1": 0, "unreadable": 0, "before_since": 0, "v2": 0}
    строки = []
    if not путь.exists():
        return строки, итог
    for line in путь.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            итог["unreadable"] += 1
            continue
        if r.get(ST.SCHEMA_VERSION_KEY) != ST.SCHEMA_VERSION:
            итог["v1"] += 1
            continue
        итог["v2"] += 1
        if since_ts is not None and (r.get("ts") or r.get("t_recv_ts") or 0) < since_ts:
            итог["before_since"] += 1
            continue
        строки.append(r)
    return строки, итог


def боевые(строки: list) -> list:
    """Решения по реальным источникам -- без стенда."""
    return [r for r in строки if not r.get("test_source")]


def стенд(строки: list) -> list:
    return [r for r in строки if r.get("test_source")]


# -------------------------------------------------------- группы разбора

def _сводка_чисел(значения: list) -> dict | None:
    з = sorted(v for v in значения if isinstance(v, (int, float)))
    if not з:
        return None
    return {"n": len(з), "min": з[0], "median": statistics.median(з),
            "max": з[-1]}


def группы_разбора(строки: list) -> dict:
    """Разбор из сообщения против разбора через RPC, раздельно.

    Для каждой группы: сколько решений, доля, распределение отставания по
    слотам и задержки решения. Группа "неизвестно" не сливается с
    остальными -- иначе пропажа поля выглядела бы как успех.
    """
    итог = {}
    всего = len(строки)
    по_группам: dict = {}
    for r in строки:
        по_группам.setdefault(r.get("parsed_from") or "неизвестно", []).append(r)
    for имя, гр in sorted(по_группам.items()):
        отставание = [r.get("slot_lag") for r in гр]
        от_сети = [(r["net_slot_at_decision"] - r["source_slot"])
                   for r in гр
                   if isinstance(r.get("net_slot_at_decision"), int)
                   and isinstance(r.get("source_slot"), int)]
        итог[имя] = {
            "decisions": len(гр),
            "share": round(len(гр) / всего, 4) if всего else None,
            "to_buy": sum(1 for r in гр if r.get("action") == "buy"),
            "slot_lag": _сводка_чисел(отставание),
            "net_slot_minus_source": _сводка_чисел(от_сети),
            "decide_latency_ms": _сводка_чисел([r.get("decide_latency_ms") for r in гр]),
        }
    return {"total": всего, "by_group": итог,
            "note": ("PARSE_VIA_RPC структурно поздний: getTransaction "
                     "поддерживает только confirmed, это 1-2 слота")}


def таблица_слотов(строки: list, n: int = 15) -> list:
    """Первые n решений: слот источника, время получения, время решения,
    слот сети на момент решения. Ровно та таблица, что просил владелец."""
    out = []
    for r in строки[:n]:
        out.append({
            "signature": (r.get("signature") or "")[:20],
            "source_task": r.get("source_task"),
            "source_slot": r.get("source_slot"),
            "t_recv_utc": r.get("t_recv_utc"),
            "decide_latency_ms": r.get("decide_latency_ms"),
            "net_slot_at_decision": r.get("net_slot_at_decision"),
            "net_slot_age_s": r.get("net_slot_age_s"),
            "slot_lag": r.get("slot_lag"),
            "parsed_from": r.get("parsed_from"),
            "tx_version": r.get("tx_version"),
            "source": r.get("source"),
            "mint": r.get("mint"),
            "action": r.get("action"),
            "code": r.get("code"),
        })
    return out


def версии_и_разбор(строки: list) -> dict:
    """Версия транзакции против пути разбора -- проверка гипотезы.

    Гипотеза: сообщения без meta (а значит и медленный путь через
    getTransaction) -- это транзакции версии 1, которые подписка с потолком
    0 разобрать не может. Здесь она либо подтверждается числами, либо нет.
    Отсутствие поля версии считается отдельной строкой: старые записи
    журнала его не несут, и выдавать их за версию 0 нельзя.
    """
    таблица: dict = {}
    for r in строки:
        в = str(r.get("tx_version")) if "tx_version" in r else "НЕТ_ПОЛЯ"
        путь = r.get("parsed_from") or "неизвестно"
        таблица.setdefault(в, {}).setdefault(путь, 0)
        таблица[в][путь] += 1
    известные = {в: d for в, d in таблица.items() if в != "НЕТ_ПОЛЯ"}
    вывод = "данных о версиях пока нет: в записях нет поля tx_version"
    if известные:
        rpc_по_версиям = {в: d.get("PARSE_VIA_RPC", 0) for в, d in известные.items()}
        всего_rpc = sum(rpc_по_версиям.values())
        только_одна = [в for в, n in rpc_по_версиям.items() if n]
        if всего_rpc == 0:
            вывод = ("медленного пути в записях с версией нет -- проверять "
                     "гипотезу не на чем")
        elif len(только_одна) == 1:
            вывод = (f"весь медленный путь ({всего_rpc}) идёт на транзакциях "
                     f"версии {только_одна[0]} -- гипотеза подтверждается")
        else:
            вывод = (f"медленный путь встречается на версиях {sorted(только_одна)} "
                     "-- версия не единственная причина")
    return {"by_version": таблица, "verdict": вывод}


# ------------------------------------------------------------------ маршруты

def маршруты(строки: list) -> dict:
    """Доля многохоповых маршрутов среди наших покупок.

    Считается по покупкам (action=buy): вопрос владельца был именно про
    НАШИ покупки. Сигналы, отброшенные за промежуточный маршрут, идут
    отдельным числом -- они в покупки не попали по определению.
    """
    покупки = [r for r in строки if r.get("action") == "buy"]
    много = 0
    известно = 0
    for r in покупки:
        м = r.get("route") or {}
        h = м.get("hops_by_mints")
        if not isinstance(h, int):
            continue
        известно += 1
        if h > 1:
            много += 1
    отброшено = sum(1 for r in строки if r.get("code") == "INTERMEDIATE_ROUTE")
    return {"buys": len(покупки), "route_known": известно,
            "multihop": много,
            "multihop_share": (round(много / известно, 4) if известно else None),
            "skipped_intermediate": отброшено}


# ------------------------------------------------------------- наши тормоза

def наши_тормоза(строки: list) -> dict:
    """Сигналы, которые прошли фильтры задачи, но остановил НАШ лимит."""
    по_кодам: dict = {}
    for r in строки:
        if r.get("action") == "buy":
            continue
        if r.get("filter") != "наш лимит" and r.get("code") not in НАШИ_ЛИМИТЫ:
            continue
        по_кодам[r.get("code") or "?"] = по_кодам.get(r.get("code") or "?", 0) + 1
    return {"by_code": по_кодам, "total": sum(по_кодам.values())}


# --------------------------------------------------------------- сверка DBot

def _наше_намерение(r: dict) -> str:
    """"куплю" -- фильтры задачи пройдены; "не куплю" -- не пройдены.

    Наш собственный лимит не меняет намерения: воспроизведение фильтров
    DBot сказало "да", а остановили себя мы сами.
    """
    if r.get("action") == "buy" or r.get("dbot_бы_купил"):
        return "куплю"
    return "не куплю"


def _срез_записей(записи: list) -> dict:
    """Что вообще загружено из DBot. Без этого «записи не нашлось» нельзя
    отличить от «записи не загружены»."""
    времена = [(r.get("createAt") or 0) / 1000.0 for r in записи
               if isinstance(r.get("createAt"), (int, float))]
    источники = {((r.get("follow") or {}).get("wallet")) for r in записи}
    источники.discard(None)
    коды: dict = {}
    for r in записи:
        к = r.get("skipReason") or "ПРОШЛО"
        коды[к] = коды.get(к, 0) + 1
    return {"loaded": len(записи),
            "sources": len(источники),
            "with_follow": sum(1 for r in записи if r.get("follow")),
            "oldest_utc": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(min(времена)))
                           if времена else None),
            "newest_utc": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(max(времена)))
                           if времена else None),
            "by_skip_reason": dict(sorted(коды.items(), key=lambda x: -x[1])[:8])}


def _почему_не_сошлось(записи: list, решение: dict, время: float,
                        окно_с: float) -> dict:
    """Почему к решению не нашлось записи DBot: нет источника, нет минта или
    не сошлось время. «Не нашлось» без этого -- не факт, а отписка."""
    свои = [r for r in записи
            if ((r.get("follow") or {}).get("wallet")) == решение.get("source")]
    по_минту = [r for r in свои
                if ((((r.get("follow") or {}).get("receive")) or {})
                    .get("info") or {}).get("contract") == решение.get("mint")]
    def ближайшее(сп):
        д = [abs((r.get("createAt") or 0) / 1000.0 - время) for r in сп
             if isinstance(r.get("createAt"), (int, float))]
        return round(min(д), 1) if д else None

    def ближайшая_запись(сп):
        """Сама ближайшая запись источника -- чтобы отличить "DBot не
        отреагировал на эту сделку" от "мы извлекли не тот минт". Если у
        DBot в ту же секунду есть запись по ДРУГОМУ минту, ошибка наша."""
        пары = [(abs((r.get("createAt") or 0) / 1000.0 - время), r) for r in сп
                if isinstance(r.get("createAt"), (int, float))]
        if not пары:
            return None
        пары.sort(key=lambda x: x[0])
        д, r = пары[0]
        ф = r.get("follow") or {}
        return {"delta_s": round(д, 1),
                "dbot_mint": (((ф.get("receive") or {}).get("info") or {})
                              .get("contract")),
                "dbot_type": r.get("type"),
                "dbot_code": r.get("skipReason") or "ПРОШЛО",
                "same_second": д <= 5.0}
    вывод = ("источника нет в записях DBot" if not свои else
             "источник есть, но минта нет ни в одной его записи" if not по_минту else
             f"источник и минт есть, но ближайшая запись в "
             f"{ближайшее(по_минту)} с при окне {окно_с:.0f} с")
    близкая = ближайшая_запись(свои)
    if not по_минту and близкая and близкая["same_second"]:
        вывод = (f"в те же секунды у DBot есть запись источника, но по ДРУГОМУ "
                 f"минту ({близкая['dbot_mint']}, {близкая['dbot_type']}, "
                 f"{близкая['dbot_code']}) -- проверить наше извлечение минта")
    return {"signature": решение.get("signature"),
            "source": решение.get("source"),
            "mint": решение.get("mint"),
            "our_code": решение.get("code"),
            "nearest_same_source_record": близкая,
            "records_same_source": len(свои),
            "records_same_source_and_mint": len(по_минту),
            "nearest_same_source_s": ближайшее(свои),
            "nearest_same_mint_s": ближайшее(по_минту),
            "why": вывод}


def сверка_с_dbot(строки: list, записи: list, *, окно_с: float = 180.0) -> dict:
    """Живая сверка нарастающим итогом. Только боевые источники.

    Главное число -- "DBot купил, мы нет": это единственное расхождение,
    которое означает пропущенные деньги. Оно считается ТОЛЬКО там, где
    отказало воспроизведение фильтров задачи; наши лимиты выносятся
    отдельной строкой и в числитель не идут.
    """
    try:
        from bloom_ambiguous_diag import вердикт_dbot  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {"known": False,
                "why": f"сопоставление с DBot недоступно: {type(exc).__name__}"}
    итог = {"known": True,
            "signals_dbot_would_buy": 0,
            "compared": 0,
            "agreed": 0,
            "dbot_bought_we_did_not": [],
            "we_would_buy_dbot_refused": [],
            "skipped_by_our_limit_dbot_bought": [],
            "dbot_record_not_found": 0,
            "not_comparable_source_did_not_buy": 0,
            "by_dbot_code": {},
            "dbot_records": _срез_записей(записи),
            "unmatched_diag": []}
    for r in боевые(строки):
        # Продажа источника и «получен, не куплен» сравнению не подлежат:
        # у DBot по ним нет решения о покупке, и записывать это в
        # «записи не нашлось» значит прятать отсутствие вопроса за
        # отсутствием ответа.
        if r.get("kind") not in ("buy", None):
            итог["not_comparable_source_did_not_buy"] += 1
            continue
        намерение = _наше_намерение(r)
        if намерение == "куплю":
            итог["signals_dbot_would_buy"] += 1
        сиг_время = r.get("t_recv_ts") or r.get("ts")
        if not (r.get("source") and r.get("mint") and сиг_время):
            continue
        в = вердикт_dbot(записи, источник=r["source"], минт=r["mint"],
                         время_сделки=float(сиг_время), окно_с=окно_с)
        if в.get("result") not in ("купил", "отказал"):
            итог["dbot_record_not_found"] += 1
            if len(итог["unmatched_diag"]) < 5:
                итог["unmatched_diag"].append(
                    _почему_не_сошлось(записи, r, float(сиг_время), окно_с))
            continue
        итог["compared"] += 1
        код = в.get("код_dbot") or "?"
        итог["by_dbot_code"][код] = итог["by_dbot_code"].get(код, 0) + 1
        купил = в["result"] == "купил"
        краткое = {"signature": r.get("signature"), "mint": r.get("mint"),
                   "our_code": r.get("code"), "dbot_code": код,
                   "dbot_record_id": в.get("id_записи"),
                   "match_delta_s": в.get("расхождение_по_времени_с")}
        наш_лимит = (r.get("filter") == "наш лимит" or r.get("code") in НАШИ_ЛИМИТЫ)
        if намерение == "куплю" and купил:
            итог["agreed"] += 1
            if r.get("action") != "buy":
                итог["skipped_by_our_limit_dbot_bought"].append(краткое)
        elif намерение == "не куплю" and not купил:
            итог["agreed"] += 1
        elif намерение == "не куплю" and купил:
            if наш_лимит:
                # Наш лимит -- не расхождение фильтров: считаем совпадением
                # по фильтрам и называем отдельной строкой.
                итог["agreed"] += 1
                итог["skipped_by_our_limit_dbot_bought"].append(краткое)
            else:
                итог["dbot_bought_we_did_not"].append(краткое)
        else:
            итог["we_would_buy_dbot_refused"].append(краткое)
    расхождений = (len(итог["dbot_bought_we_did_not"])
                   + len(итог["we_would_buy_dbot_refused"]))
    итог["divergences"] = расхождений
    итог["agreement"] = (round(итог["agreed"] / итог["compared"], 4)
                         if итог["compared"] else None)
    итог["gate_signals_reached"] = итог["signals_dbot_would_buy"] >= ГЕЙТ_СИГНАЛОВ
    итог["gate_no_missed_buys"] = len(итог["dbot_bought_we_did_not"]) == 0
    итог["gate_agreement"] = (итог["agreement"] is not None
                              and итог["agreement"] >= ГЕЙТ_СОВПАДЕНИЕ)
    итог["note"] = ("записи follow не несут подписи транзакции источника, "
                    f"поэтому сопоставление идёт по тройке источник+минт+время "
                    f"в окне {окно_с:.0f} с")
    return итог


# ------------------------------------------------------------------ пары А/Б

def пары(строки: list, сверка: dict) -> dict:
    """Пары "наша покупка против покупки DBot".

    При входе 0.05 SOL пара идёт в зачёт слотов, но не разницы результата:
    объёмы разные. Это помечается в отчёте, а не молча учитывается.
    """
    наши = [r for r in строки
            if r.get("action") == "buy" and not r.get("test_source")
            and (r.get("exec") or {}).get("exec_code") in ("SENT", "SENT_AFTER_BUMP")]
    малый = sum(1 for r in наши
                if isinstance((r.get("exec") or {}).get("buy_sol"), (int, float))
                and r["exec"]["buy_sol"] < РАЗМЕР_БЕЗ_РАЗНИЦЫ_SOL)
    return {"our_sent": len(наши),
            "small_size_pairs": малый,
            "gate_pairs": ГЕЙТ_ПАР,
            "gate_reached": len(наши) >= ГЕЙТ_ПАР,
            "note": (f"пары с входом меньше {РАЗМЕР_БЕЗ_РАЗНИЦЫ_SOL} SOL идут в "
                     "зачёт слотов, но не разницы результата: объёмы разные")}


# ------------------------------------------------------------------ позиции

def позиции_срез(state: ST.ExecState) -> dict:
    """Три раздела: live, live-test и dry-run отдельной строкой."""
    все = list(state.positions().values())
    def срез(предикат):
        p = [x for x in все if предикат(x)]
        return {"count": len(p),
                "open": sum(1 for x in p if x.get("state") in ST.STATES_OPEN),
                "spent_sol": round(sum(float(x.get("spend_sol") or 0) for x in p), 6)}
    return {"live": срез(lambda x: x.get("mode") == ST.MODE_LIVE),
            "live_test": срез(lambda x: x.get("mode") == ST.MODE_LIVE_TEST),
            "dry_run": срез(lambda x: not ST.is_real_mode(x.get("mode"))),
            "open_real": len(state.open_positions()),
            "note": "позиции dry-run не учитываются ни в гейтах, ни в парах А/Б"}


# ------------------------------------------------------------------ кредиты

def кредиты(state: ST.ExecState) -> dict:
    try:
        from helius_usage_report import сводка  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {"known": False, "why": f"{type(exc).__name__}: {str(exc)[:120]}"}
    try:
        return {"known": True, "usage": сводка(state.base / "helius_usage")}
    except Exception as exc:  # noqa: BLE001
        return {"known": False, "why": f"{type(exc).__name__}: {str(exc)[:120]}"}


# -------------------------------------------------------------------- гейты

def гейты(*, сверка: dict, позиции: dict, статус: dict | None) -> dict:
    """Четыре условия владельца для live на реальных источниках."""
    баланс = (статус or {}).get("balance_sol")
    исполнитель = (статус or {}).get("executor") or {}
    круг = (позиции["live_test"]["count"] > 0
            and позиции["live_test"]["open"] == 0)
    return {
        "executor_on_host": {
            "ok": bool((статус or {}).get("executor_attached")),
            "mode": исполнитель.get("mode"),
            "sent": исполнитель.get("sent"),
            "refused": исполнитель.get("refused")},
        "reconciliation": {
            "ok": bool(сверка.get("gate_signals_reached")
                       and сверка.get("gate_no_missed_buys")
                       and сверка.get("gate_agreement")),
            "signals": сверка.get("signals_dbot_would_buy"),
            "need": ГЕЙТ_СИГНАЛОВ,
            "missed_buys": len(сверка.get("dbot_bought_we_did_not") or []),
            "agreement": сверка.get("agreement")},
        "balance": {"ok": isinstance(баланс, (int, float)) and баланс >= ГЕЙТ_БАЛАНС_SOL,
                    "balance_sol": баланс, "need": ГЕЙТ_БАЛАНС_SOL},
        "test_call_full_circle": {
            "ok": круг,
            "live_test_positions": позиции["live_test"]["count"],
            "still_open": позиции["live_test"]["open"]},
    }


# ------------------------------------------------------------------- отчёт

def отчёт(*, state: ST.ExecState, since_ts: float | None = None,
          записи_dbot: list | None = None, статус: dict | None = None,
          n_таблицы: int = 15) -> dict:
    строки, счёт = читать_решения(state.decisions_path, since_ts=since_ts)
    б = боевые(строки)
    с = стенд(строки)
    св = (сверка_с_dbot(строки, записи_dbot)
          if записи_dbot is not None
          else {"known": False, "why": "записи DBot не переданы (нужен ключ и сеть)"})
    поз = позиции_срез(state)
    по_кодам: dict = {}
    for r in б:
        по_кодам[r.get("code") or "?"] = по_кодам.get(r.get("code") or "?", 0) + 1
    по_кодам_стенда: dict = {}
    for r in с:
        по_кодам_стенда[r.get("code") or "?"] = по_кодам_стенда.get(r.get("code") or "?", 0) + 1
    return {
        ST.SCHEMA_VERSION_KEY: ST.SCHEMA_VERSION,
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "since_ts": since_ts,
        "journal": счёт,
        "decisions": {"total": len(строки), "real_sources": len(б),
                      "test_source": len(с),
                      "by_code": по_кодам,
                      "by_code_test_source": по_кодам_стенда},
        "parse_groups": группы_разбора(б),
        "version_vs_parse": версии_и_разбор(строки),
        "slot_table": таблица_слотов(б, n_таблицы),
        "routes": маршруты(б),
        "our_brakes": наши_тормоза(б),
        "reconciliation": св,
        "pairs": пары(строки, св),
        "positions": поз,
        "credits": кредиты(state),
        "gates": гейты(сверка=св, позиции=поз, статус=статус),
    }


def в_текст(о: dict) -> str:
    L = []
    L.append(f"=== Bloom: доклад {о['built_utc']} (формат {о[ST.SCHEMA_VERSION_KEY]}) ===")
    ж = о["journal"]
    L.append(f"журнал: новых записей {ж['v2']}, прежнего формата {ж['v1']} "
             f"(не учтены), нечитаемых {ж['unreadable']}, до начала окна {ж['before_since']}")
    d = о["decisions"]
    L.append(f"решений в окне: {d['total']} -- боевые источники {d['real_sources']}, "
             f"стенд {d['test_source']}")
    L.append("по кодам (боевые источники):")
    for k, v in sorted(d["by_code"].items(), key=lambda x: -x[1]):
        L.append(f"  {v:5d}  {k}")
    if d["by_code_test_source"]:
        L.append("по кодам (тестовый источник, в сверку НЕ идёт):")
        for k, v in sorted(d["by_code_test_source"].items(), key=lambda x: -x[1]):
            L.append(f"  {v:5d}  {k}")
    L.append("")
    L.append("--- откуда разбор (группы не смешиваются) ---")
    for имя, г in о["parse_groups"]["by_group"].items():
        L.append(f"{имя}: решений {г['decisions']}, доля {г['share']}, "
                 f"к покупке {г['to_buy']}")
        L.append(f"    отставание слотов: {г['slot_lag']}")
        L.append(f"    слот сети минус слот источника: {г['net_slot_minus_source']}")
        L.append(f"    задержка решения, мс: {г['decide_latency_ms']}")
    L.append(f"  примечание: {о['parse_groups']['note']}")
    вр = о.get("version_vs_parse") or {}
    L.append("")
    L.append("--- версия транзакции против пути разбора ---")
    for в, d in sorted((вр.get("by_version") or {}).items()):
        L.append(f"  версия {в}: " + ", ".join(f"{k}={n}" for k, n in sorted(d.items())))
    L.append(f"  вывод: {вр.get('verdict')}")
    L.append("")
    L.append(f"--- первые {len(о['slot_table'])} решений: слоты и время ---")
    L.append("подпись               задача   слот источника  получено             "
             "решение,мс  слот сети  возраст,с  отставание  разбор         версия  итог")
    for r in о["slot_table"]:
        L.append(f"{(r['signature'] or ''):22s}{str(r['source_task'] or '-'):9s}"
                 f"{str(r['source_slot'] or '-'):16s}{str(r['t_recv_utc'] or '-'):21s}"
                 f"{str(r['decide_latency_ms'] or '-'):12s}"
                 f"{str(r['net_slot_at_decision'] or '-'):11s}"
                 f"{str(r['net_slot_age_s'] if r['net_slot_age_s'] is not None else '-'):11s}"
                 f"{str(r['slot_lag'] if r['slot_lag'] is not None else '-'):12s}"
                 f"{str(r['parsed_from'] or '-'):15s}"
                 f"{str(r.get('tx_version', '-')):8s}"
                 f"{r['action'] or '-'}/{r['code'] or '-'}")
    L.append("")
    м = о["routes"]
    L.append(f"--- маршруты наших покупок --- покупок {м['buys']}, маршрут известен "
             f"{м['route_known']}, многохоповых {м['multihop']} (доля {м['multihop_share']}), "
             f"отброшено за промежуточный токен {м['skipped_intermediate']}")
    т = о["our_brakes"]
    L.append(f"--- остановлено НАШИМ лимитом: {т['total']} ---")
    for k, v in sorted(т["by_code"].items(), key=lambda x: -x[1]):
        L.append(f"  {v:5d}  {k}")
    L.append("")
    св = о["reconciliation"]
    L.append("--- живая сверка с DBot (нарастающим итогом) ---")
    if not св.get("known"):
        L.append(f"  не выполнена: {св.get('why')}")
    else:
        L.append(f"  сигналов, где фильтры задачи пройдены (dbot_бы_купил): "
                 f"{св['signals_dbot_would_buy']} из {ГЕЙТ_СИГНАЛОВ} нужных")
        L.append(f"  сопоставлено с записями DBot: {св['compared']}, "
                 f"записи не нашлось: {св['dbot_record_not_found']}")
        L.append(f"  совпадений: {св['agreed']}, совпадение {св['agreement']} "
                 f"(порог {ГЕЙТ_СОВПАДЕНИЕ})")
        L.append(f"  DBot купил, мы нет: {len(св['dbot_bought_we_did_not'])} "
                 "(должно быть 0)")
        for x in св["dbot_bought_we_did_not"][:10]:
            L.append(f"      {x['signature']} {x['mint']} наш код {x['our_code']}")
        L.append(f"  мы бы купили, DBot отказал: {len(св['we_would_buy_dbot_refused'])}")
        L.append(f"  пропущено по НАШЕМУ лимиту, DBot купил: "
                 f"{len(св['skipped_by_our_limit_dbot_bought'])} "
                 "(в числитель расхождений не идёт)")
        L.append(f"  не сравнивалось (источник не покупал): "
                 f"{св.get('not_comparable_source_did_not_buy')}")
        зап = св.get("dbot_records") or {}
        L.append(f"  записей DBot загружено: {зап.get('loaded')} "
                 f"(источников {зап.get('sources')}, с полем follow "
                 f"{зап.get('with_follow')}), окно записей "
                 f"{зап.get('oldest_utc')} -- {зап.get('newest_utc')}")
        if зап.get("by_skip_reason"):
            L.append(f"  коды DBot в загруженных записях: {зап['by_skip_reason']}")
        for d in св.get("unmatched_diag") or []:
            L.append(f"    не сошлось {str(d['signature'])[:16]} ({d['our_code']}): "
                     f"{d['why']}")
        L.append(f"  примечание: {св['note']}")
    L.append("")
    п = о["pairs"]
    L.append(f"--- пары А/Б --- наших отправленных покупок {п['our_sent']} из "
             f"{п['gate_pairs']}; из них малым размером {п['small_size_pairs']}")
    L.append(f"  {п['note']}")
    поз = о["positions"]
    L.append(f"--- позиции --- live {поз['live']}, live-test {поз['live_test']}, "
             f"dry-run {поз['dry_run']} (не учитывается)")
    L.append("")
    L.append("--- гейты live на реальных источниках ---")
    for имя, г in о["gates"].items():
        L.append(f"  [{'ДА ' if г.get('ok') else 'НЕТ'}] {имя}: "
                 + ", ".join(f"{k}={v}" for k, v in г.items() if k != "ok"))
    к = о["credits"]
    L.append("")
    if к.get("known"):
        L.append("--- расход Helius ---")
        L.append(json.dumps(к["usage"], ensure_ascii=False)[:2000])
    else:
        L.append(f"--- расход Helius не прочитан: {к.get('why')}")
    return "\n".join(L)


# -------------------------------------------------------------- самопроверка

def self_test() -> None:
    import tempfile  # noqa: PLC0415
    прошло = 0
    checks = []

    def chk(имя, ок, факт=""):
        checks.append((имя, ок, факт))

    def строка(**kw):
        r = {ST.SCHEMA_VERSION_KEY: ST.SCHEMA_VERSION, "ts": 1000.0,
             "t_recv_ts": 1000.0, "signature": "sig", "source": "SRC",
             "mint": "MINT", "action": "skip", "code": "NOT_A_BUY",
             "parsed_from": "PARSE_VIA_MSG", "test_source": False}
        r.update(kw)
        return r

    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        st = ST.ExecState(base=base, kill=base / "KILL")
        # --- чтение журнала: v1 не учитывается, битая строка названа
        p = st.decisions_path
        p.write_text("\n".join([
            json.dumps({"действие": "покупка"}, ensure_ascii=False),
            "{битая",
            json.dumps(строка(), ensure_ascii=False),
        ]) + "\n", encoding="utf-8")
        строки, счёт = читать_решения(p)
        chk("v1 не попал в строки", len(строки) == 1, len(строки))
        chk("v1 посчитан отдельно", счёт["v1"] == 1, счёт)
        chk("битая строка названа", счёт["unreadable"] == 1, счёт)

        # --- окно since отсекает старое, но не молча
        p.write_text("\n".join([
            json.dumps(строка(ts=10.0), ensure_ascii=False),
            json.dumps(строка(ts=2000.0), ensure_ascii=False),
        ]) + "\n", encoding="utf-8")
        строки, счёт = читать_решения(p, since_ts=1000.0)
        chk("since отсёк старое", len(строки) == 1, len(строки))
        chk("отсечённое названо числом", счёт["before_since"] == 1, счёт)

        # --- группы разбора не смешиваются
        гр = группы_разбора([
            строка(parsed_from="PARSE_VIA_MSG", slot_lag=0, decide_latency_ms=0.3,
                   source_slot=100, net_slot_at_decision=100),
            строка(parsed_from="PARSE_VIA_RPC", slot_lag=2, decide_latency_ms=450.0,
                   source_slot=100, net_slot_at_decision=102),
            строка(parsed_from=None),
        ])
        chk("три группы, неизвестное отдельно", set(гр["by_group"]) ==
            {"PARSE_VIA_MSG", "PARSE_VIA_RPC", "неизвестно"}, list(гр["by_group"]))
        chk("медиана RPC своя",
            гр["by_group"]["PARSE_VIA_RPC"]["decide_latency_ms"]["median"] == 450.0,
            гр["by_group"]["PARSE_VIA_RPC"]["decide_latency_ms"])
        chk("слот сети минус слот источника считается",
            гр["by_group"]["PARSE_VIA_RPC"]["net_slot_minus_source"]["median"] == 2,
            гр["by_group"]["PARSE_VIA_RPC"]["net_slot_minus_source"])

        # --- маршруты: доля многохоповых среди покупок
        м = маршруты([
            строка(action="buy", code="BUY", route={"hops_by_mints": 1}),
            строка(action="buy", code="BUY", route={"hops_by_mints": 3}),
            строка(action="buy", code="BUY", route={}),
            строка(code="INTERMEDIATE_ROUTE"),
        ])
        chk("покупок три", м["buys"] == 3, м)
        chk("маршрут известен у двух", м["route_known"] == 2, м)
        chk("доля многохоповых 0.5", м["multihop_share"] == 0.5, м)
        chk("отброшенные за промежуточный названы", м["skipped_intermediate"] == 1, м)

        # --- наши тормоза отделены от фильтров задачи
        т = наши_тормоза([
            строка(code=ST.КОД_ДУБЛЬ_МИНТА, filter="наш лимит"),
            строка(code="TARGET_AMOUNT_OUT_OF_RANGE", filter="задача DBot"),
        ])
        chk("в тормоза попал только наш лимит",
            т["by_code"] == {ST.КОД_ДУБЛЬ_МИНТА: 1}, т)

        # --- сверка: наш лимит НЕ расхождение, пропуск фильтром -- расхождение
        def запись_dbot(*, skip=None, ts=1000.0, минт="MINT"):
            return {"id": "rec", "createAt": int(ts * 1000), "skipReason": skip,
                    "state": 1, "follow": {"wallet": "SRC",
                                           "receive": {"info": {"contract": минт}}}}
        св = сверка_с_dbot(
            [строка(code=ST.КОД_ДУБЛЬ_МИНТА, filter="наш лимит", **{"dbot_бы_купил": True})],
            [запись_dbot()])
        chk("наш лимит не расхождение",
            св["dbot_bought_we_did_not"] == [] and
            len(св["skipped_by_our_limit_dbot_bought"]) == 1, св)
        chk("наш лимит считается совпадением по фильтрам",
            св["agreement"] == 1.0, св["agreement"])

        св2 = сверка_с_dbot(
            [строка(code="TARGET_AMOUNT_OUT_OF_RANGE", filter="задача DBot")],
            [запись_dbot()])
        chk("DBot купил, мы нет -- расхождение",
            len(св2["dbot_bought_we_did_not"]) == 1, св2)
        chk("гейт \"нет пропущенных покупок\" не пройден",
            св2["gate_no_missed_buys"] is False, св2["gate_no_missed_buys"])

        св3 = сверка_с_dbot([строка(action="buy", code="BUY")],
                            [запись_dbot(skip="DUPLICATE_TOKEN_BUY")])
        chk("мы бы купили, DBot отказал -- своя строка",
            len(св3["we_would_buy_dbot_refused"]) == 1, св3)

        св4 = сверка_с_dbot([строка(action="buy", code="BUY", test_source=True)],
                            [запись_dbot()])
        chk("стенд в сверку не идёт",
            св4["signals_dbot_would_buy"] == 0 and св4["compared"] == 0, св4)

        св5 = сверка_с_dbot([строка(action="buy", code="BUY")],
                            [запись_dbot(минт="ДРУГОЙ")])
        chk("записи DBot не нашлось -- не совпадение",
            св5["dbot_record_not_found"] == 1 and св5["compared"] == 0, св5)
        chk("совпадение без сопоставленных -- None",
            св5["agreement"] is None, св5["agreement"])

        # --- продажа источника не идёт в "записи не нашлось"
        св_прод = сверка_с_dbot(
            [строка(kind="sell", code="NOT_A_BUY")], [запись_dbot()])
        chk("продажа источника не считается пропавшей записью",
            св_прод["dbot_record_not_found"] == 0 and
            св_прод["not_comparable_source_did_not_buy"] == 1, св_прод)
        chk("и в сопоставленные она не попала",
            св_прод["compared"] == 0, св_прод["compared"])

        # --- диагностика несовпадения называет причину, а не "не нашлось"
        св_диаг = сверка_с_dbot(
            [строка(kind="buy", action="buy", code="BUY", source="ДРУГОЙ")],
            [запись_dbot()])
        chk("причина: источника нет в записях",
            св_диаг["unmatched_diag"][0]["why"].startswith("источника нет"),
            св_диаг["unmatched_diag"][0])
        # Минта нет ни в одной записи, и ближайшая запись источника далеко:
        # это "DBot не отреагировал", а не наша ошибка разбора. Различие
        # появилось после уточнения диагностики -- рядом по времени запись
        # по другому минту означает совсем другое.
        св_диаг2 = сверка_с_dbot(
            [строка(kind="buy", action="buy", code="BUY", mint="ДРУГОЙ_МИНТ")],
            [запись_dbot(ts=1000.0 + 600)])
        chk("причина: минта нет ни в одной записи источника",
            "минта нет" in св_диаг2["unmatched_diag"][0]["why"],
            св_диаг2["unmatched_diag"][0]["why"])
        св_диаг3 = сверка_с_dbot(
            [строка(kind="buy", action="buy", code="BUY")],
            [запись_dbot(ts=1000.0 + 600)])
        chk("причина: не сошлось время, и разница названа числом",
            "ближайшая запись" in св_диаг3["unmatched_diag"][0]["why"]
            and св_диаг3["unmatched_diag"][0]["nearest_same_mint_s"] == 600.0,
            св_диаг3["unmatched_diag"][0])

        # --- запись DBot в ту же секунду, но по другому минту: наша ошибка
        св_другой = сверка_с_dbot(
            [строка(kind="buy", action="buy", code="BUY", mint="НАШ_МИНТ")],
            [запись_dbot(минт="МИНТ_DBOT", ts=1000.0 + 2)])
        д = св_другой["unmatched_diag"][0]
        chk("запись в те же секунды по другому минту названа нашей ошибкой",
            "проверить наше извлечение минта" in д["why"], д["why"])
        chk("и минт DBot назван",
            д["nearest_same_source_record"]["dbot_mint"] == "МИНТ_DBOT", д)
        # а запись через десять минут -- это уже не та же сделка
        св_далеко = сверка_с_dbot(
            [строка(kind="buy", action="buy", code="BUY", mint="НАШ_МИНТ")],
            [запись_dbot(минт="МИНТ_DBOT", ts=1000.0 + 600)])
        chk("далёкая запись по другому минту нашей ошибкой не объявляется",
            "минта нет" in св_далеко["unmatched_diag"][0]["why"],
            св_далеко["unmatched_diag"][0]["why"])

        # --- срез загруженных записей отличает "не нашлось" от "не загружено"
        chk("пустой список записей виден как loaded 0",
            сверка_с_dbot([], [])["dbot_records"]["loaded"] == 0)
        срез = сверка_с_dbot([], [запись_dbot(), запись_dbot(skip="ПРОПУСК")])["dbot_records"]
        chk("в срезе видно источников и коды",
            срез["loaded"] == 2 and срез["sources"] == 1
            and срез["by_skip_reason"].get("ПРОПУСК") == 1, срез)

        # --- версия транзакции против пути разбора
        вр = версии_и_разбор([
            строка(parsed_from="PARSE_VIA_RPC", tx_version=1),
            строка(parsed_from="PARSE_VIA_RPC", tx_version=1),
            строка(parsed_from="PARSE_VIA_MSG", tx_version=0),
        ])
        chk("весь медленный путь на версии 1 -- гипотеза подтверждается",
            "гипотеза подтверждается" in вр["verdict"], вр["verdict"])
        вр2 = версии_и_разбор([
            строка(parsed_from="PARSE_VIA_RPC", tx_version=1),
            строка(parsed_from="PARSE_VIA_RPC", tx_version=0),
        ])
        chk("медленный путь на двух версиях -- версия не единственная причина",
            "не единственная причина" in вр2["verdict"], вр2["verdict"])
        вр3 = версии_и_разбор([строка(parsed_from="PARSE_VIA_MSG", tx_version=0)])
        chk("без медленного пути гипотезу проверять не на чем",
            "не на чем" in вр3["verdict"], вр3["verdict"])
        вр4 = версии_и_разбор([строка(parsed_from="PARSE_VIA_RPC")])
        chk("записи без поля версии считаются отдельно, а не версией 0",
            "НЕТ_ПОЛЯ" in вр4["by_version"] and
            "нет поля tx_version" in вр4["verdict"], вр4)

        # --- гейт по сигналам считает именно dbot_бы_купил
        много = [строка(action="buy", code="BUY", signature=f"s{i}")
                 for i in range(ГЕЙТ_СИГНАЛОВ)]
        св6 = сверка_с_dbot(много, [])
        chk("гейт сигналов достигнут на 30",
            св6["gate_signals_reached"] is True, св6["signals_dbot_would_buy"])
        св7 = сверка_с_dbot(много[:-1], [])
        chk("на 29 сигналах гейт не достигнут",
            св7["gate_signals_reached"] is False, св7["signals_dbot_would_buy"])

        # --- позиции: dry-run отдельно и не в open_real
        st.positions_path.write_text("\n".join([
            json.dumps({"client_order_id": "a", "state": "intent",
                        "mode": ST.MODE_DRY, "spend_sol": 0.01,
                        ST.SCHEMA_VERSION_KEY: 2}, ensure_ascii=False),
            json.dumps({"client_order_id": "b", "state": "intent",
                        "mode": ST.MODE_LIVE_TEST, "spend_sol": 0.01,
                        ST.SCHEMA_VERSION_KEY: 2}, ensure_ascii=False),
        ]) + "\n", encoding="utf-8")
        поз = позиции_срез(st)
        chk("dry-run в своём разделе", поз["dry_run"]["count"] == 1, поз["dry_run"])
        chk("live-test считается настоящей", поз["open_real"] == 1, поз["open_real"])

        # --- пары: малый размер помечен
        п = пары([строка(action="buy", code="BUY",
                         exec={"exec_code": "SENT", "buy_sol": 0.05})], {})
        chk("отправленная покупка стала парой", п["our_sent"] == 1, п)
        chk("малый размер помечен", п["small_size_pairs"] == 1, п)

        # --- гейты: баланс и круг тестового вызова
        г = гейты(сверка=св6, позиции=поз,
                  статус={"balance_sol": 2.5, "executor_attached": True,
                          "executor": {"mode": "live"}})
        chk("гейт баланса при 2.5 SOL пройден", г["balance"]["ok"] is True, г["balance"])
        г2 = гейты(сверка=св6, позиции=поз, статус={"balance_sol": 0.35})
        chk("гейт баланса при 0.35 SOL не пройден",
            г2["balance"]["ok"] is False, г2["balance"])
        chk("круг тестового вызова не закрыт, пока позиция открыта",
            г["test_call_full_circle"]["ok"] is False, г["test_call_full_circle"])

        # --- отчёт целиком собирается и печатается
        p.write_text(json.dumps(строка(action="buy", code="BUY"),
                                ensure_ascii=False) + "\n", encoding="utf-8")
        о = отчёт(state=st, статус={"balance_sol": 0.35, "executor_attached": True})
        текст = в_текст(о)
        chk("отчёт собрался", о["decisions"]["total"] == 1, о["decisions"])
        chk("сверка честно названа невыполненной",
            о["reconciliation"]["known"] is False, о["reconciliation"])
        chk("текст непустой и с заголовком", "Bloom: доклад" in текст, текст[:40])

    for имя, ок, факт in checks:
        if ок:
            прошло += 1
        print(f"  [{'ok  ' if ок else 'ПЛОХО'}] {имя}" + (f" -- {факт}" if not ок else ""))
    print(f"самопроверка доклада: {прошло}/{len(checks)} пройдено")
    if прошло != len(checks):
        sys.exit(1)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--since", default=None,
                   help="начало окна, UTC вида 2026-09-23T00:00:00Z")
    p.add_argument("--status", default=None,
                   help="файл detector_status.json (баланс, исполнитель)")
    p.add_argument("--dbot", action="store_true",
                   help="сопоставить с записями follow у DBot (нужен DBOT_API_KEY)")
    p.add_argument("--tasks", default="BATCH-5,BATCH-3")
    p.add_argument("--config", default=None, help="снимок конфига задач DBot")
    p.add_argument("--rows", type=int, default=15, help="строк в таблице слотов")
    p.add_argument("--out", default=None, help="куда положить текст отчёта")
    a = p.parse_args()
    if a.self_test:
        self_test()
        return 0

    state = ST.ExecState()
    since_ts = None
    if a.since:
        since_ts = time.mktime(time.strptime(a.since, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone

    статус = None
    путь_статуса = Path(a.status) if a.status else (state.base / "detector_status.json")
    if путь_статуса.exists():
        try:
            статус = json.loads(путь_статуса.read_text(encoding="utf-8"))
        except ValueError:
            статус = None

    записи = None
    if a.dbot:
        import os  # noqa: PLC0415
        ключ = os.environ.get("DBOT_API_KEY") or ""
        конфиг = Path(a.config) if a.config else None
        if not ключ or not конфиг or not конфиг.exists():
            print("сверка с DBot пропущена: нет ключа или снимка конфига",
                  file=sys.stderr)
        else:
            from bloom_ambiguous_diag import записи_dbot  # noqa: PLC0415
            записи = записи_dbot(ключ, tuple(x.strip() for x in a.tasks.split(",")),
                                 конфиг)

    о = отчёт(state=state, since_ts=since_ts, записи_dbot=записи,
              статус=статус, n_таблицы=a.rows)
    текст = в_текст(о)
    print(текст)
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    ST.atomic_write_json(OUT_JSON, о)
    (Path(a.out) if a.out else OUT_TEXT).write_text(текст + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
