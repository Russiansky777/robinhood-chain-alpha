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

def у_порога(строки: list) -> dict:
    """Решения ВПЛОТНУЮ к порогу -- своей строкой.

    Владелец просил видеть такие случаи отдельно: "вход 0.007 при пороге 2" и
    "вход 1.95 при пороге 2" -- разные события, и второе может означать, что
    порог или курс нужно уточнять, а первое не означает ничего.
    """
    сп = [r for r in строки if r.get("code") == "THRESHOLD_EDGE"]
    return {"count": len(сп),
             "rows": [{"ts_utc": r.get("ts_utc"), "signature": r.get("signature"),
                        "source_task": r.get("source_task"), "mint": r.get("mint"),
                        "spend_sol_eq": r.get("spend_sol_eq"),
                        "spend_mint": r.get("spend_mint"),
                        "spend_ui": r.get("spend_ui"),
                        "rate_note": r.get("rate_note"),
                        "reason": r.get("reason")} for r in сп[-12:]],
             "note": ("код THRESHOLD_EDGE не покупает: он только называет случай. "
                       "Допуск к порогу не вводится без слова владельца -- это "
                       "прямое расширение того, что мы копируем")}


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
            # ГРУППА "ТОЛЬКО МЫ". Сигналы, по которым у DBot нет даже записи
            # -- ни покупки, ни отказа. Это не расхождение решений: сравнивать
            # не с чем, и в гейт "мы / DBot" такие сигналы не входят. Но и
            # прятать их в общем счётчике "записи не нашлось" нельзя: это
            # сделки, которые мы сделали в одиночку, и итог по ним -- наш.
            "only_us": {"count": 0, "rows": []},
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
            if намерение == "куплю" or r.get("action") == "buy":
                итог["only_us"]["count"] += 1
                итог["only_us"]["rows"].append({
                    "signature": r.get("signature"), "mint": r.get("mint"),
                    "source": r.get("source"), "our_code": r.get("code"),
                    "our_action": r.get("action"),
                    "ts_utc": r.get("ts_utc"),
                    "our_spend_sol_eq": r.get("spend_sol_eq")})
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
                   "match_delta_s": в.get("расхождение_по_времени_с"),
                   # Чем именно платил DBot по своей записи -- чтобы
                   # расхождение можно было разобрать, а не только заметить.
                   "dbot_paid": в.get("dbot_отдал"),
                   "dbot_got": в.get("dbot_получил"),
                   "dbot_time_utc": в.get("dbot_время_utc"),
                   "our_spend_sol_eq": r.get("spend_sol_eq"),
                   "our_spend_ui": r.get("spend_ui"),
                   "our_spend_mint": r.get("spend_mint"),
                   "our_rate_note": r.get("rate_note")}
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
    # ОБРАТНЫЙ ПРОХОД. Всё выше идёт ПО НАШИМ РЕШЕНИЯМ, а значит не видит
    # худший случай: DBot купил, а решения у нас нет ВООБЩЕ -- сигнал не
    # пришёл, разбор упал, служба была в рестарте. Такая покупка выглядела
    # как тишина и ни в одно число не попадала.
    #
    # Границы берём по нашему же журналу: за его пределами мы про сигналы
    # ничего не знаем и приписывать себе пропуск не имеем права.
    времена = [float(r.get("t_recv_ts") or r.get("ts") or 0)
                for r in боевые(строки)
                if (r.get("t_recv_ts") or r.get("ts"))]
    итог["dbot_bought_no_decision"] = []
    if времена:
        с_края, до_края = min(времена), max(времена)
        итог["window_utc"] = [
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(с_края)),
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(до_края))]
        наши = [(r.get("source"), r.get("mint"),
                  float(r.get("t_recv_ts") or r.get("ts") or 0))
                 for r in боевые(строки)]
        for z in записи:
            if z.get("skipReason"):
                continue
            f = z.get("follow") or {}
            кошелёк = f.get("wallet")
            минт = (((f.get("receive") or {}).get("info")) or {}).get("contract")
            t = (z.get("createAt") or 0) / 1000.0
            if not (кошелёк and минт) or not (с_края - окно_с <= t <= до_края + окно_с):
                continue
            есть = any(и == кошелёк and м == минт and abs(t - tt) <= окно_с
                        for и, м, tt in наши)
            if есть:
                continue
            отдал = ((z.get("pay") or {}).get("info")) or {}
            получил = ((z.get("receive") or {}).get("info")) or {}
            итог["dbot_bought_no_decision"].append({
                "source": кошелёк, "mint": минт,
                "dbot_record_id": z.get("id"),
                "dbot_time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)),
                "dbot_paid": {"минт": отдал.get("contract"),
                               "количество": отдал.get("amount") or отдал.get("amountUI"),
                               "символ": отдал.get("symbol")},
                "dbot_got": {"минт": получил.get("contract"),
                              "количество": получил.get("amount") or получил.get("amountUI"),
                              "символ": получил.get("symbol")},
                "our_decision": None,
                "why": ("покупка DBot внутри окна нашего журнала, а решения по "
                         "этому источнику и минту у нас нет ни одного")})

    расхождений = (len(итог["dbot_bought_we_did_not"])
                   + len(итог["we_would_buy_dbot_refused"])
                   + len(итог["dbot_bought_no_decision"]))
    итог["divergences"] = расхождений
    итог["agreement"] = (round(итог["agreed"] / итог["compared"], 4)
                         if итог["compared"] else None)
    итог["gate_signals_reached"] = итог["signals_dbot_would_buy"] >= ГЕЙТ_СИГНАЛОВ
    итог["gate_no_missed_buys"] = (len(итог["dbot_bought_we_did_not"]) == 0
                                    and len(итог["dbot_bought_no_decision"]) == 0)
    итог["gate_agreement"] = (итог["agreement"] is not None
                              and итог["agreement"] >= ГЕЙТ_СОВПАДЕНИЕ)
    итог["note"] = ("записи follow не несут подписи транзакции источника, "
                    f"поэтому сопоставление идёт по тройке источник+минт+время "
                    f"в окне {окно_с:.0f} с")
    return итог


# ------------------------------------------------------------------ пары А/Б

def замер_места(путь: Path | None = None) -> dict:
    """Замер места в блоке относительно источника -- из отдельного прогона.

    Считает его analysis/bloom_block_position.py (ему нужен getBlock, а
    докладу узел не нужен вовсе). Здесь только чтение готового файла, и
    возраст замера называется прямо: старый замер -- не свежий факт.
    """
    путь = путь or (Path(__file__).resolve().parents[1] / "data"
                     / "bloom_block_position.json")
    try:
        d = json.loads(путь.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"known": False, "why": f"замер не прочитан: {type(exc).__name__}"}
    строки = d.get("rows") or []
    кратко = []
    for r in строки:
        м = r.get("measure") or {}
        кратко.append({
            "mint": r.get("mint"),
            "source_task": r.get("source_task"),
            "slot_delta": м.get("slot_delta"),
            "our_index": м.get("our_index"),
            "source_index": м.get("source_index"),
            "index_delta_same_block": м.get("index_delta_same_block"),
            "ahead_of_source": м.get("ahead_of_source"),
            "crowd_between": (м.get("crowd_between") or {}).get("count"),
            "dbot_index_delta_same_block": м.get("dbot_index_delta_same_block"),
            "why_not": м.get("source_why_not") or м.get("our_why_not"),
        })
    return {"known": True, "built_utc": d.get("built_utc"), "rows": кратко,
            "note": ("место в блоке считает отдельный прогон "
                      "run_bloom_block_position; один слот с источником "
                      "(S+0) не значит одну цену -- порядок в блоке решает "
                      "валидатор")}


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
        # Исполнитель пишет вход как sol_in. Поле spend_sol не писал никто,
        # и сумма потраченного была ровным нулём при живых покупках: старый
        # доклад показывал spent_sol 0.0 при позиции на 0.001 SOL.
        # Оба ключа читаются, чтобы прежние записи не потерялись.
        def вход(x):
            for к in ("sol_in", "spend_sol"):
                try:
                    v = float(x.get(к) or 0)
                except (TypeError, ValueError):
                    v = 0.0
                if v:
                    return v
            return 0.0
        # УПАВШИЕ ПО ЦЕПИ ПОКУПКИ НЕ СЧИТАЮТСЯ ПОТРАЧЕННЫМ. 24.09 покупка
        # на 0.2 SOL упала с ExceededSlippage: по цепи ушла только комиссия
        # 0.001, а sol_in в записи позиции остался 0.2 -- намерением, а не
        # фактом. Складывать намерения в "потрачено" значит завышать расход
        # и занижать остаток.
        упало = [x for x in p if x.get("chain_ok") is False]
        села = [x for x in p if x.get("chain_ok") is not False]
        return {"count": len(p),
                "open": sum(1 for x in p if x.get("state") in ST.STATES_OPEN),
                "failed_on_chain": len(упало),
                "intent_sol_failed": round(sum(вход(x) for x in упало), 6),
                "spent_sol": round(sum(вход(x) for x in села), 6)}
    # ПОЛОСА СВОЕЙ ОТПРАВКИ -- ОТДЕЛЬНЫМ РАЗДЕЛОМ. Её сделки по 0.01 SOL
    # нельзя складывать с боевыми по 0.2 SOL: в разделе live тогда растёт
    # число сделок и потраченное, а читается это как торговля Bloom.
    return {"live": срез(lambda x: x.get("mode") == ST.MODE_LIVE
                          and not x.get("lane")),
            "live_test": срез(lambda x: x.get("mode") == ST.MODE_LIVE_TEST
                               and not x.get("lane")),
            "lane": срез(lambda x: x.get("lane") == ST.МЕТКА_ПОЛОСЫ),
            "dry_run": срез(lambda x: not ST.is_real_mode(x.get("mode"))),
            "open_real": len(state.open_positions(lane=None)),
            "open_lane": len(state.open_positions(lane=ST.МЕТКА_ПОЛОСЫ)),
            "note": ("позиции dry-run не учитываются ни в гейтах, ни в парах "
                      "А/Б; раздел lane -- полоса своей отправки, её сделки в "
                      "live не входят")}


def найти_подпись(строки: list, подписи: tuple) -> list:
    """Все записи журнала по указанным подписям, целиком.

    Нужно для разбора "сигнал был, решения нет": без поиска по журналу
    нельзя отличить пропущенный сигнал от сигнала, записанного под другим
    источником или с другим кодом.
    """
    если = {s for s in подписи if s}
    вых = []
    for r in строки:
        for ключ in ("signature", "source_sig"):
            v = r.get(ключ)
            if v and (v in если or any(v.startswith(x) or x.startswith(v)
                                        for x in если)):
                вых.append(r)
                break
    return вых


def найти_по_минту(строки: list, минты: tuple, *, предел: int = 24) -> list:
    """Все решения по минту, свежие первыми.

    Нужно, когда известна покупка DBot, а наша запись неизвестна: искать по
    подписи нечего -- запись DBot подписи источника не несёт.
    """
    если = {m for m in минты if m}
    вых = [r for r in строки if r.get("mint") in если]
    вых.sort(key=lambda r: str(r.get("ts_utc") or ""), reverse=True)
    return вых[:предел]


def найти_по_коду(строки: list, коды: tuple, *, предел: int = 24) -> list:
    """Записи журнала с указанными кодами, целиком и свежие первыми.

    Нужно, когда подписи неизвестны: разбор начинается с "покажи те решения,
    которые владелец видел в Telegram", а не с угадывания подписей.
    """
    если = {c.upper() for c in коды if c}
    вых = [r for r in строки if str(r.get("code") or "").upper() in если]
    вых.sort(key=lambda r: str(r.get("ts_utc") or ""), reverse=True)
    return вых[:предел]


def позиции_таблица(state: ST.ExecState) -> list:
    """По каждой НАСТОЯЩЕЙ позиции: чем покупали, каким маршрутом, чем продавали.

    Владельцу нужно в чек-лист: где покупал Bloom (пул и программа) и через
    какой пул шла продажа. Всё это лежит в записях позиций -- отдельными
    полями, а не в свободном тексте.
    """
    строки = []
    for p in state.positions().values():
        if not ST.is_real_mode(p.get("mode")):
            continue
        # Наша подпись, слот посадки и вернувшийся SOL нужны в чек-лист по
        # каждому пункту. Раньше таблица их не несла, и приходилось искать
        # их отдельным прогоном раскладки.
        подписи = p.get("signatures") or []
        строки.append({
            "client_order_id": p.get("client_order_id"),
            "mint": p.get("mint"),
            "mode": p.get("mode"),
            "state": p.get("state"),
            # Села ли покупка по цепи и подтверждена ли продажа -- в таблице
            # обязаны быть видны. Без них отчёт считает упавшую транзакцию
            # состоявшейся покупкой, а закрытие по нулевому остатку --
            # продажей: ровно так 24.09 и вышло.
            "chain_ok": p.get("chain_ok"),
            "closed_confirmed": p.get("closed_confirmed"),
            "closed_why_not": p.get("closed_why_not"),
            "our_signature": (подписи[0] if подписи else None),
            "source_signature": p.get("source_sig"),
            "source_slot": p.get("source_slot"),
            "our_slot": p.get("our_slot"),
            "slot_delta": ((p.get("our_slot") - p.get("source_slot"))
                            if isinstance(p.get("our_slot"), int)
                            and isinstance(p.get("source_slot"), int) else None),
            "sol_in": p.get("sol_in"),
            "sol_back": (p.get("closed_sol_delta")
                          if p.get("closed_sol_delta") is not None
                          else (p.get("last_sell_outcome") or {}).get("sol_delta")),
            "sol_back_net": (p.get("closed_sol_net")
                              if p.get("closed_sol_net") is not None
                              else (p.get("last_sell_outcome") or {}).get("sol_delta_net")),
            "closed_via": p.get("closed_via"),
            "closed_signature": p.get("closed_signature"),
            "buy_address_kind": p.get("buy_address_kind"),
            "buy_address": p.get("buy_address") or p.get("pool"),
            "source_program": p.get("program"),
            "our_pool": p.get("our_pool"),
            "our_pool_direct": p.get("our_pool_direct"),
            "our_route_programs": p.get("our_route_programs"),
            "our_route_hops": p.get("our_route_hops"),
            "flags": p.get("flags"),
            "sell_address_kinds": p.get("sell_address_kinds"),
            "sell_attempts": p.get("sell_attempts"),
            "unsold_reason": p.get("unsold_reason"),
            "balance_read_why_not": p.get("balance_read_why_not"),
            "jup_attempts": p.get("jup_attempts"),
            "jup_signature": p.get("jup_signature"),
            "jup_why_not": p.get("jup_why_not"),
            "jup_floor": p.get("jup_floor"),
            "closed_reason": p.get("closed_reason"),
        })
    строки.sort(key=lambda r: str(r.get("client_order_id")))
    return строки


def итог_группы_только_мы(state: ST.ExecState, сверка: dict) -> dict:
    """Итог по сигналам, где у DBot нет даже записи.

    Считается по НАШИМ позициям, а не по журналу решений: решение говорит,
    что мы хотели купить, а сколько вернулось -- знает только позиция.
    Сделки без закрытия в итог не идут и считаются отдельно, иначе открытая
    позиция читалась бы как нулевой результат.
    """
    группа = (сверка or {}).get("only_us") or {}
    строки = группа.get("rows") or []
    если_нет = {"count": len(строки), "with_result": 0, "open_or_unknown": 0,
                 "sum_pct": None, "median_pct": None, "sum_sol": None,
                 "note": ("сигналы, где у DBot нет ни покупки, ни отказа: "
                           "сравнивать не с чем, в гейт мы/DBot они не входят")}
    if not строки:
        return если_нет
    по_минту = {}
    for p_ in state.positions().values():
        # Позиция ПОЛОСЫ на том же минте не имеет права подменить боевую:
        # вход у них разный (0.01 против 0.2 SOL), и процент вышел бы чужой.
        if p_.get("lane"):
            continue
        if ST.is_real_mode(p_.get("mode")) and p_.get("mint"):
            по_минту.setdefault(p_["mint"], p_)
    проценты, сумма_sol = [], 0.0
    for r in строки:
        поз = по_минту.get(r.get("mint"))
        вх = (поз or {}).get("sol_in")
        наз = ((поз or {}).get("closed_sol_net")
                if (поз or {}).get("closed_sol_net") is not None
                else (((поз or {}).get("last_sell_outcome") or {}).get("sol_delta_net")))
        if поз is None or not вх or наз is None:
            если_нет["open_or_unknown"] += 1
            r["result_pct"] = None
            continue
        pct = round((наз - вх) / вх * 100, 2)
        r["result_pct"] = pct
        r["sol_in"] = вх
        r["sol_back_net"] = наз
        проценты.append(pct)
        сумма_sol += (наз - вх)
    если_нет["with_result"] = len(проценты)
    if проценты:
        проценты_с = sorted(проценты)
        середина = len(проценты_с) // 2
        если_нет["median_pct"] = (проценты_с[середина] if len(проценты_с) % 2
                                   else round((проценты_с[середина - 1]
                                                + проценты_с[середина]) / 2, 2))
        если_нет["sum_pct"] = round(sum(проценты), 2)
        если_нет["sum_sol"] = round(сумма_sol, 9)
    return если_нет


def таблица_кругов(state: ST.ExecState, строки: list, место: dict) -> list:
    """Полный круг по каждой боевой сделке: решение -> Bloom -> блок -> продажа.

    Слово владельца: у сделок, сделанных ДО появления замера, поля должны
    быть ПУСТЫМИ, а не нулевыми. Ноль здесь читался бы как "круг ноль
    миллисекунд", а это неправда -- круга просто не мерили.

    Что откуда:
      bloom_ms          -- позиция, пишет исполнитель (решение -> ответ Bloom);
      new_connections   -- позиция, сколько НОВЫХ соединений к Bloom
                           понадобилось на эту покупку (прогрев работает,
                           если ноль);
      own_tx_seen_ms    -- позиция, решение -> наша транзакция в потоке;
      bloom_to_seen_ms  -- позиция, ответ Bloom -> та же точка;
      S+N и место       -- слоты позиции и отдельный прогон места в блоке;
      продажа           -- журнал решений (секунды) и последний итог
                           продажи (слот), закрытие -- позиция;
      тень              -- журнал решений, запись stage=shadow по подписи
                           источника.
    """
    тени = {}
    закрытия = {}
    for r in строки:
        if r.get("stage") == "shadow" and r.get("signature"):
            тени[r["signature"]] = r
        if r.get("action") == "позиция закрыта -- доклад" and r.get("client_order_id"):
            закрытия[r["client_order_id"]] = r
    места = {}
    for r in (место.get("rows") or []) if место.get("known") else []:
        if r.get("client_order_id"):
            места[r["client_order_id"]] = r
        elif r.get("mint"):
            места.setdefault(("минт", r["mint"]), r)

    вых = []
    for p_ in state.positions().values():
        if not ST.is_real_mode(p_.get("mode")):
            continue
        cid = p_.get("client_order_id")
        тень = тени.get(p_.get("source_sig")) or {}
        закр = закрытия.get(cid) or {}
        м = (места.get(cid) or места.get(("минт", p_.get("mint"))) or {})
        итог = p_.get("last_sell_outcome") or {}
        вых.append({
            "client_order_id": cid,
            "mint": p_.get("mint"),
            "ts_intent_utc": (time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                             time.gmtime(p_["ts_intent"]))
                               if isinstance(p_.get("ts_intent"), (int, float)) else None),
            "bloom_ms": p_.get("bloom_ms"),
            "new_connections": p_.get("new_connections"),
            "own_tx_seen_ms": p_.get("own_tx_seen_ms"),
            "bloom_to_seen_ms": p_.get("bloom_to_seen_ms"),
            "slot_delta": ((p_.get("our_slot") - p_.get("source_slot"))
                            if isinstance(p_.get("our_slot"), int)
                            and isinstance(p_.get("source_slot"), int) else None),
            "block_index_delta": м.get("index_delta_same_block"),
            "ahead_of_source": м.get("ahead_of_source"),
            "crowd_between": (м.get("crowd_between") or {}).get("count")
                              if isinstance(м.get("crowd_between"), dict)
                              else м.get("crowd_between"),
            # ПЛОЩАДКА И ЧИСЛО ШАГОВ МАРШРУТА BLOOM. Владелец 25.09 спросил
            # их рядом с кругами: без них непонятно, сравниваем ли мы
            # одинаковые сделки. Пишутся разбором нашей покупки.
            "pool_programs": p_.get("our_route_programs"),
            "route_hops": p_.get("our_route_hops"),
            "pool_direct": p_.get("our_pool_direct"),
            # АБСОЛЮТНОЕ МЕСТО В БЛОКЕ. Его добирает сам детектор на пульсе
            # (один getBlock уровня signatures), поэтому берётся из позиции, а
            # не из отдельного прогона. Пусто -- значит ещё не добрано или
            # блок недоступен, а не "первое место".
            "block_index": p_.get("block_index"),
            "block_total": p_.get("block_total"),
            "block_share": p_.get("block_share"),
            "block_why_not": p_.get("block_why_not"),
            # ПОЛОСА СВОЕЙ ОТПРАВКИ. Пусто в этих полях -- покупка Bloom.
            # Своя отправка меряется от ОТПРАВКИ (у неё нет ответа площадки),
            # поэтому её число стоит отдельным столбцом, а не в
            # bloom_to_seen_ms: сложить их значило бы сравнить разные пути.
            "lane": p_.get("lane"),
            "lane_send_to_seen_ms": p_.get("lane_send_to_seen_ms"),
            "lane_bought_raw": p_.get("lane_bought_raw"),
            "lane_pair_delta_ms": p_.get("lane_pair_delta_ms"),
            "sell_after_s_plan": p_.get("sell_after_s"),
            "sell_seconds": закр.get("seconds"),
            "sell_slot": итог.get("slot"),
            "closed_via": p_.get("closed_via"),
            "chain_ok": p_.get("chain_ok"),
            "closed_confirmed": p_.get("closed_confirmed"),
            "sol_in": p_.get("sol_in"),
            "sol_back_net": (p_.get("closed_sol_net")
                              if p_.get("closed_sol_net") is not None
                              else итог.get("sol_delta_net")),
            # Тень -- пометка, торговля от неё не зависит ни в одном байте.
            "shadow_route": тень.get("route"),
            "shadow_would_pass": ((тень.get("sim_verdict") == "would_pass")
                                   if тень.get("sim_verdict") else None),
            "shadow_verdict": тень.get("sim_verdict"),
            "shadow_why_not": тень.get("why_not"),
            "shadow_build_ms": тень.get("build_ms"),
            "would_skip_cap": тень.get("would_skip_cap"),
            "cap_usd": тень.get("cap_usd"),
        })
    вых.sort(key=lambda r: str(r.get("ts_intent_utc") or ""))
    return вых


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
           найти: tuple = (), коды: tuple = (), минты: tuple = (), почему_нет_dbot: str | None = None,
          записи_dbot: list | None = None, статус: dict | None = None,
          n_таблицы: int = 15, n_стенда: int = 10) -> dict:
    строки, счёт = читать_решения(state.decisions_path, since_ts=since_ts)
    б = боевые(строки)
    с = стенд(строки)
    св = (сверка_с_dbot(строки, записи_dbot)
          if записи_dbot is not None
          else {"known": False,
                "why": (почему_нет_dbot
                        or "записи DBot не переданы (сверка не запрашивалась)")})
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
        # Стенд идёт по тестовым источникам, и его записи в таблицу боевых
        # не попадают. Для прохода чек-листа нужны они ЦЕЛИКОМ: ожидаемый
        # код против фактического, путь разбора, версия, исполнение.
        "stand_slot_table": таблица_слотов(с, n_таблицы),
        "stand_rows": с[-n_стенда:] if n_стенда else [],
        "routes": маршруты(б),
        "our_brakes": наши_тормоза(б),
        "threshold_edge": у_порога(строки),
        "reconciliation": св,
        "pairs": пары(строки, св),
        "block_position": замер_места(),
        "positions": поз,
        "found_by_signature": найти_подпись(строки, найти or ()),
        "found_by_code": найти_по_коду(строки, коды or ()),
        "found_by_mint": найти_по_минту(строки, минты or ()),
        "position_rows": позиции_таблица(state),
        "circles_table": таблица_кругов(state, строки, замер_места()),
        "only_us": итог_группы_только_мы(state, св),
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
    if о.get("stand_slot_table"):
        L.append(f"--- стенд: решения по тестовым источникам "
                 f"({len(о['stand_slot_table'])}) ---")
        L.append("подпись               задача   слот источника  получено             "
                 "решение,мс  слот сети  возраст,с  отставание  разбор         версия  итог")
        for r in о["stand_slot_table"]:
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
        L.append("--- стенд: записи целиком ---")
        for r in о.get("stand_rows") or []:
            L.append(json.dumps(r, ensure_ascii=False))
        L.append("")
    м = о["routes"]
    L.append(f"--- маршруты наших покупок --- покупок {м['buys']}, маршрут известен "
             f"{м['route_known']}, многохоповых {м['multihop']} (доля {м['multihop_share']}), "
             f"отброшено за промежуточный токен {м['skipped_intermediate']}")
    уп = о.get("threshold_edge") or {}
    if уп.get("count"):
        L.append("")
        L.append(f"--- вплотную к порогу (THRESHOLD_EDGE): {уп['count']} ---")
        for r in уп.get("rows") or []:
            L.append(f"  {r.get('ts_utc')} {r.get('source_task')} "
                     f"{str(r.get('mint'))[:12]} вход {r.get('spend_sol_eq')} "
                     f"SOL-экв ({r.get('spend_ui')} {str(r.get('spend_mint'))[:8]}) "
                     f"-- {r.get('reason')}")
        L.append(f"  {уп.get('note')}")
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
        L.append(f"  DBot купил, а решения у нас НЕТ ВООБЩЕ: "
                 f"{len(св.get('dbot_bought_no_decision') or [])} (должно быть 0)")
        for x in (св.get("dbot_bought_no_decision") or [])[:10]:
            L.append(f"      {x['dbot_time_utc']} источник {x['source'][:12]} "
                     f"минт {x['mint'][:12]} запись {x['dbot_record_id']}")
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
    ом = о.get("only_us") or {}
    L.append("")
    L.append(f"--- только мы --- сигналов {ом.get('count', 0)}: у DBot нет ни "
             f"покупки, ни отказа. В гейт мы/DBot НЕ входят")
    L.append(f"  с известным итогом {ом.get('with_result', 0)}, без итога "
             f"{ом.get('open_or_unknown', 0)}; медиана "
             f"{ом.get('median_pct') if ом.get('median_pct') is not None else '-'} %, "
             f"сумма {ом.get('sum_pct') if ом.get('sum_pct') is not None else '-'} п.п., "
             f"{ом.get('sum_sol') if ом.get('sum_sol') is not None else '-'} SOL")
    for r in ((о.get("reconciliation") or {}).get("only_us") or {}).get("rows") or []:
        L.append(f"    {r.get('ts_utc') or '-'} минт {str(r.get('mint'))[:12]} "
                 f"код {r.get('our_code')} итог "
                 f"{r.get('result_pct') if r.get('result_pct') is not None else '-'} %")
    круги = о.get("circles_table") or []
    L.append("")
    L.append(f"--- таблица кругов --- сделок {len(круги)}; пустое поле значит "
             "«не мерили», а не ноль")
    for r in круги:
        def ч(значение, единица=""):
            return "-" if значение is None else f"{значение}{единица}"
        L.append(f"  {ч(r.get('ts_intent_utc'))} {str(r.get('client_order_id'))[:8]} "
                 f"минт {str(r.get('mint'))[:10]}")
        L.append(f"      круги: bloom {ч(r.get('bloom_ms'), ' мс')}, новых "
                 f"соединений {ч(r.get('new_connections'))}, наша тх в потоке "
                 f"{ч(r.get('own_tx_seen_ms'), ' мс')} от решения и "
                 f"{ч(r.get('bloom_to_seen_ms'), ' мс')} от ответа Bloom")
        L.append(f"      блок: S+{ч(r.get('slot_delta'))}, место против "
                 f"источника {ч(r.get('block_index_delta'))}, впереди "
                 f"{ч(r.get('ahead_of_source'))}, толпа между "
                 f"{ч(r.get('crowd_between'))}")
        # Площадка, шаги и абсолютное место -- отдельной строкой: по ним
        # видно, сравнимы ли сделки между собой и с синтетическим тестом.
        доля = r.get("block_share")
        L.append(f"      маршрут: площадка {ч(r.get('pool_programs'))}, шагов "
                 f"{ч(r.get('route_hops'))}, напрямую {ч(r.get('pool_direct'))}"
                 f" · место в блоке {ч(r.get('block_index'))} из "
                 f"{ч(r.get('block_total'))}"
                 + (f" (доля {доля})" if доля is not None else "")
                 + (f" · {r['block_why_not']}" if r.get("block_why_not") else ""))
        L.append(f"      продажа: план {ч(r.get('sell_after_s_plan'), ' с')}, факт "
                 f"{ч(r.get('sell_seconds'), ' с')}, слот {ч(r.get('sell_slot'))}, "
                 f"через {ч(r.get('closed_via'))}")
        L.append(f"      итог: по цепи {ч(r.get('chain_ok'))}, продажа "
                 f"подтверждена {ч(r.get('closed_confirmed'))}, вход "
                 f"{ч(r.get('sol_in'))} SOL, вернулось чисто "
                 f"{ч(r.get('sol_back_net'))} SOL")
        тень_прошла = r.get("shadow_would_pass")
        L.append(f"      тень: наша сборка прошла бы -- "
                 f"{'да' if тень_прошла else ('нет' if тень_прошла is False else '-')}"
                 f" ({ч(r.get('shadow_verdict'))}), маршрут "
                 f"{ч(r.get('shadow_route'))}, сборка "
                 f"{ч(r.get('shadow_build_ms'), ' мс')}, дорогая по "
                 f"капитализации {ч(r.get('would_skip_cap'))}")
    поз = о["positions"]
    L.append("")
    L.append(f"--- позиции --- live {поз['live']}, live-test {поз['live_test']}, "
             f"dry-run {поз['dry_run']} (не учитывается)")
    for r in о.get("position_rows") or []:
        L.append(f"  позиция {str(r.get('client_order_id'))[:8]} {r.get('mode')}/"
                 f"{r.get('state')} минт {r.get('mint')}")
        L.append(f"      покупка: по {r.get('buy_address_kind')} "
                 f"{r.get('buy_address')}, программа источника "
                 f"{r.get('source_program')}")
        L.append(f"      наш маршрут: {r.get('our_route_programs')}, хопов "
                 f"{r.get('our_route_hops')}, пул {r.get('our_pool')} "
                 f"(прямой: {r.get('our_pool_direct')}), метки {r.get('flags') or '-'}")
        if r.get("jup_attempts"):
            пол = r.get("jup_floor") or {}
            L.append(f"      Jupiter: попыток {r.get('jup_attempts')}, подпись "
                     f"{r.get('jup_signature') or '-'}, котировка "
                     f"{пол.get('out_amount')} лампортов при поле "
                     f"{пол.get('floor_needed')} ({пол.get('slippage_bps')} bps, "
                     f"маршрут {пол.get('router')}), доля от входа "
                     f"{пол.get('quote_share_of_entry_pct')} %"
                     + (f", отказ: {r.get('jup_why_not')}" if r.get("jup_why_not") else ""))
        if r.get("closed_reason"):
            L.append(f"      закрыта: {r.get('closed_reason')}")
        L.append(f"      продажа: чем пробовали {r.get('sell_address_kinds') or '-'}, "
                 f"попыток {r.get('sell_attempts') or 0}"
                 + (f", не продано: {r.get('unsold_reason')}"
                    if r.get("unsold_reason") else "")
                 + (f", остаток не читался: {r.get('balance_read_why_not')}"
                    if r.get("balance_read_why_not") else ""))
    зм = о.get("block_position") or {}
    L.append("--- место в блоке относительно источника ---")
    if not зм.get("known"):
        L.append(f"  замера нет: {зм.get('why')}")
    else:
        L.append(f"  замер от {зм.get('built_utc')}")
        for r in зм.get("rows") or []:
            толпа = r.get("crowd_between")
            L.append(f"  {str(r.get('mint'))[:12]} {r.get('source_task') or ''} "
                     f"S+{r.get('slot_delta')}: наш индекс {r.get('our_index')}, "
                     f"источник {r.get('source_index')}, разница "
                     f"{r.get('index_delta_same_block')}, чужих покупок между "
                     f"нами {толпа if толпа is not None else '?'}, "
                     f"DBot к источнику {r.get('dbot_index_delta_same_block')}"
                     + (f" | {r['why_not']}" if r.get("why_not") else ""))
        L.append(f"  примечание: {зм.get('note')}")
    L.append("")
    по_коду = о.get("found_by_code") or []
    if по_коду:
        L.append("")
        L.append(f"--- записи по искомым кодам: {len(по_коду)} ---")
        for r in по_коду:
            L.append(json.dumps(r, ensure_ascii=False)[:1600])
        L.append("")
    нашлось = о.get("found_by_signature") or []
    if нашлось:
        L.append("")
        L.append(f"--- записи по искомым подписям: {len(нашлось)} ---")
        for r in нашлось:
            L.append(json.dumps(r, ensure_ascii=False)[:1500])
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

        # --- ГРУППА "ТОЛЬКО МЫ". Сигнал, который мы купили, а у DBot по нему
        # нет ни покупки, ни отказа. Сравнивать не с чем: в гейт не идёт, но
        # и потеряться в общем счётчике "записи не нашлось" не должен.
        chk("наша покупка без записи DBot попала в группу 'только мы'",
            св5["only_us"]["count"] == 1
            and св5["only_us"]["rows"][0]["our_code"] == "BUY", св5["only_us"])
        chk("и в сопоставленные она по-прежнему не идёт",
            св5["compared"] == 0, св5["compared"])
        св6 = сверка_с_dbot([строка(action="skip", code="TOKEN_RECEIVED_NOT_BOUGHT")],
                            [запись_dbot(минт="ДРУГОЙ")])
        chk("сигнал, который мы НЕ покупали, в группу 'только мы' не идёт",
            св6["only_us"]["count"] == 0, св6["only_us"])

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

        # --- ХУДШИЙ СЛУЧАЙ: DBot купил, а решения у нас нет ВООБЩЕ.
        # Прямой проход идёт по нашим решениям и такого не видит: пропущенная
        # покупка выглядит как тишина. Здесь она обязана всплыть.
        св_нет = сверка_с_dbot(
            [строка(kind="buy", action="buy", code="BUY", mint="ДРУГОЙ_МИНТ", ts=1000.0)],
            [запись_dbot(минт="ПРОПУЩЕННЫЙ", ts=1000.0)])
        chk("покупка DBot без нашего решения видна отдельным списком",
            len(св_нет["dbot_bought_no_decision"]) == 1,
            св_нет["dbot_bought_no_decision"])
        chk("и в ней названы источник, минт и запись DBot",
            св_нет["dbot_bought_no_decision"][0]["mint"] == "ПРОПУЩЕННЫЙ"
            and св_нет["dbot_bought_no_decision"][0]["source"] == "SRC"
            and св_нет["dbot_bought_no_decision"][0]["dbot_record_id"] == "rec",
            св_нет["dbot_bought_no_decision"][0])
        chk("она идёт в расхождения и рубит гейт пропущенных покупок",
            св_нет["divergences"] >= 1 and св_нет["gate_no_missed_buys"] is False,
            (св_нет["divergences"], св_нет["gate_no_missed_buys"]))
        with tempfile.TemporaryDirectory() as d_св:
            основа = отчёт(state=ST.ExecState(base=Path(d_св) / "s",
                                               kill=Path(d_св) / "k"),
                            записи_dbot=[])
            chk("и в тексте доклада эта строка есть",
                "решения у нас НЕТ ВООБЩЕ" in в_текст(
                    {**основа, "reconciliation": св_нет}))

        # Запись DBot ВНЕ окна нашего журнала пропуском не считается: за
        # пределами журнала мы про сигналы ничего не знаем.
        св_вне = сверка_с_dbot(
            [строка(kind="buy", action="buy", code="BUY", mint="ДРУГОЙ_МИНТ", ts=1000.0)],
            [запись_dbot(минт="ПРОПУЩЕННЫЙ", ts=1000.0 + 10_000)])
        chk("запись вне окна журнала пропуском не считается",
            св_вне["dbot_bought_no_decision"] == [], св_вне["dbot_bought_no_decision"])

        # --- поиск по минту: когда подпись источника неизвестна
        по_м = найти_по_минту([строка(mint="M1", ts_utc="2026-09-24T02:00:00Z"),
                                строка(mint="M2", ts_utc="2026-09-24T02:01:00Z"),
                                строка(mint="M1", ts_utc="2026-09-24T02:02:00Z")],
                               ("M1",))
        chk("поиск по минту нашёл оба решения и свежее первым",
            len(по_м) == 2 and по_м[0]["ts_utc"] == "2026-09-24T02:02:00Z", по_м)
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

        # --- вход считается по тому полю, которое пишет исполнитель
        st.positions_path.write_text("\n".join([
            json.dumps({"client_order_id": "c", "state": "bought",
                        "mode": ST.MODE_LIVE_TEST, "sol_in": 0.001,
                        "mint": "MINTX", "buy_address": "POOLX",
                        "buy_address_kind": "pool", "program": "Meteora DLMM",
                        "our_pool": None, "our_pool_direct": False,
                        "our_route_programs": "Raydium CLMM,Raydium CPMM",
                        "our_route_hops": 2, "flags": "ROUTE_MISMATCH",
                        "sell_address_kinds": "pool,mint", "sell_attempts": 2,
                        "unsold_reason": "2 неудачных попыток подряд",
                        "signatures": ["НАША_ПОДПИСЬ"], "source_sig": "ПОДПИСЬ_ИСТОЧНИКА",
                        "source_slot": 100, "our_slot": 101,
                        "closed_via": "авто-ордер Bloom",
                        "closed_signature": "ПОДПИСЬ_ЗАКРЫТИЯ",
                        "closed_sol_delta": 0.000810693,
                        ST.SCHEMA_VERSION_KEY: 2}, ensure_ascii=False),
        ]) + "\n", encoding="utf-8")
        поз2 = позиции_срез(st)
        chk("вход берётся из sol_in, а не из несуществующего spend_sol",
            поз2["live_test"]["spent_sol"] == 0.001, поз2["live_test"])
        тб = позиции_таблица(st)
        chk("в таблице позиций одна настоящая", len(тб) == 1, тб)
        chk("в таблице видно, села ли покупка по цепи",
            "chain_ok" in тб[0] and "closed_confirmed" in тб[0], тб[0])

        # --- ТАБЛИЦА КРУГОВ. Главное правило владельца: у старых сделок
        # поля ПУСТЫЕ, а не нулевые -- ноль читался бы как "круг ноль мс".
        круги = таблица_кругов(st, [], {"known": False})
        chk("круг по старой сделке -- пустые поля, а не нули",
            len(круги) == 1 and круги[0]["bloom_ms"] is None
            and круги[0]["own_tx_seen_ms"] is None
            and круги[0]["bloom_to_seen_ms"] is None
            and круги[0]["new_connections"] is None, круги)
        chk("а слоты, которые ЕСТЬ, в круге посчитаны",
            круги[0]["slot_delta"] == 1, круги[0]["slot_delta"])
        chk("тени по этой сделке не было -- так и сказано, а не «нет»",
            круги[0]["shadow_would_pass"] is None
            and круги[0]["shadow_verdict"] is None, круги[0])

        реш_кр = [
            {"stage": "shadow", "signature": "ПОДПИСЬ_ИСТОЧНИКА",
             "sim_verdict": "would_pass", "route": "two_hop", "build_ms": 0.7,
             "would_skip_cap": False, "cap_usd": 12345.0},
            {"action": "позиция закрыта -- доклад", "client_order_id": "c",
             "seconds": 29.1, "via": "авто-ордер Bloom"},
        ]
        место_кр = {"known": True, "rows": [{"client_order_id": "c",
                                              "index_delta_same_block": -3,
                                              "ahead_of_source": True,
                                              "crowd_between": 2}]}
        круги2 = таблица_кругов(st, реш_кр, место_кр)
        chk("тень подцепилась по подписи источника",
            круги2[0]["shadow_would_pass"] is True
            and круги2[0]["shadow_route"] == "two_hop"
            and круги2[0]["would_skip_cap"] is False, круги2[0])
        chk("секунды продажи взяты из журнала, место -- из отдельного замера",
            круги2[0]["sell_seconds"] == 29.1
            and круги2[0]["block_index_delta"] == -3
            and круги2[0]["crowd_between"] == 2, круги2[0])
        # ---- ПЛОЩАДКА, ШАГИ И АБСОЛЮТНОЕ МЕСТО (владелец 25.09) ----
        # Без них непонятно, сравнимы ли боевые сделки с синтетическим тестом:
        # у теста был один шаг на Pump AMM, а Bloom водит и через DLMM.
        chk("площадка и число шагов маршрута Bloom в круге",
            круги2[0]["pool_programs"] == "Raydium CLMM,Raydium CPMM"
            and круги2[0]["route_hops"] == 2, круги2[0])
        chk("места в блоке ещё нет -- поле пустое, а не нулевое",
            круги2[0]["block_index"] is None and круги2[0]["block_total"] is None
            and круги2[0]["block_share"] is None, круги2[0])
        st.update_position("c", block_index=187, block_total=1204,
                            block_share=0.1553)
        круги3 = таблица_кругов(st, реш_кр, место_кр)
        chk("добранное место в блоке попадает в круг",
            круги3[0]["block_index"] == 187 and круги3[0]["block_total"] == 1204
            and круги3[0]["block_share"] == 0.1553, круги3[0])
        st.update_position("c", block_why_not="нашей подписи в этом блоке нет")
        круги4 = таблица_кругов(st, реш_кр, место_кр)
        chk("причина, по которой места нет, тоже видна",
            круги4[0]["block_why_not"] == "нашей подписи в этом блоке нет",
            круги4[0].get("block_why_not"))
        текст_кр = в_текст(отчёт(state=st))
        chk("таблица кругов печатается и пустое поле видно чертой",
            "таблица кругов" in текст_кр and "не мерили" in текст_кр, текст_кр[:200])
        chk("строка маршрута и места печатается",
            "маршрут: площадка" in текст_кр and "место в блоке" in текст_кр,
            [с for с in текст_кр.split("\n") if "маршрут:" in с][:1])

        # --- ИТОГ ГРУППЫ "ТОЛЬКО МЫ" считается по позициям, а не по журналу:
        # журнал говорит, что мы хотели купить, а сколько вернулось -- знает
        # только позиция. Открытая позиция не должна читаться как нулевой итог.
        st.positions_path.write_text("\n".join([
            json.dumps({"client_order_id": "om1", "state": "closed",
                        "mode": ST.MODE_LIVE, "mint": "МИНТ_ОДИН", "sol_in": 0.2,
                        "closed_sol_net": 0.18, ST.SCHEMA_VERSION_KEY: 2},
                       ensure_ascii=False),
            json.dumps({"client_order_id": "om2", "state": "bought",
                        "mode": ST.MODE_LIVE, "mint": "МИНТ_ДВА", "sol_in": 0.2,
                        ST.SCHEMA_VERSION_KEY: 2}, ensure_ascii=False),
        ]) + "\n", encoding="utf-8")
        гр = итог_группы_только_мы(st, {"only_us": {"count": 3, "rows": [
            {"mint": "МИНТ_ОДИН", "our_code": "BUY", "ts_utc": "2026-09-24T10:00:00Z"},
            {"mint": "МИНТ_ДВА", "our_code": "BUY", "ts_utc": "2026-09-24T10:01:00Z"},
            {"mint": "МИНТ_ТРИ", "our_code": "BUY", "ts_utc": "2026-09-24T10:02:00Z"},
        ]}})
        chk("итог группы считается только по закрытым сделкам",
            гр["count"] == 3 and гр["with_result"] == 1
            and гр["open_or_unknown"] == 2, гр)
        chk("и процент взят из позиции, а не из намерения",
            abs(гр["median_pct"] - (-10.0)) < 0.01
            and abs(гр["sum_sol"] - (-0.02)) < 1e-9, гр)
        гр0 = итог_группы_только_мы(st, {"only_us": {"count": 0, "rows": []}})
        chk("пустая группа -- нули по счёту и пустые проценты, а не нулевые",
            гр0["count"] == 0 and гр0["median_pct"] is None
            and гр0["sum_pct"] is None, гр0)

        # УПАВШАЯ ПО ЦЕПИ ПОКУПКА: намерение 0.2 SOL не есть потраченные
        # 0.2 SOL. 24.09 по цепи ушла только комиссия.
        st.positions_path.write_text("\n".join([
            json.dumps({"client_order_id": "c", "state": "bought",
                        "mode": ST.MODE_LIVE_TEST, "sol_in": 0.001,
                        "mint": "MINTX", ST.SCHEMA_VERSION_KEY: 2},
                        ensure_ascii=False),
            json.dumps({"client_order_id": "f", "state": "closed",
                        "mode": ST.MODE_LIVE_TEST, "sol_in": 0.2,
                        "mint": "MINTF", "chain_ok": False,
                        "closed_reason": "покупка упала по цепи: ExceededSlippage",
                        ST.SCHEMA_VERSION_KEY: 2}, ensure_ascii=False),
        ]) + "\n", encoding="utf-8")
        # --- ПОЛОСА СВОЕЙ ОТПРАВКИ В ДОКЛАДЕ. Её сделки нельзя складывать с
        # боевыми: вход 0.01 против 0.2 SOL, и в разделе live это читалось бы
        # как торговля Bloom.
        было_позиции = st.positions_path.read_text(encoding="utf-8")
        st.positions_path.write_text(было_позиции + "\n".join([
            json.dumps({"client_order_id": "lane_r", "state": "bought",
                        "mode": ST.MODE_LIVE, "sol_in": 0.01, "mint": "MINTX",
                        "lane": ST.МЕТКА_ПОЛОСЫ, "chain_ok": True,
                        "lane_bought_raw": 8880000,
                        "lane_send_to_seen_ms": 120.5,
                        "lane_pair_delta_ms": 430.2,
                        ST.SCHEMA_VERSION_KEY: 2}, ensure_ascii=False),
        ]) + "\n", encoding="utf-8")
        поз_л = позиции_срез(st)
        chk("полоса в докладе -- своим разделом, в live её траты не идут",
            поз_л["lane"]["count"] == 1 and поз_л["lane"]["spent_sol"] == 0.01
            and поз_л["live"]["count"] == 0, (поз_л["lane"], поз_л["live"]))
        chk("открытые полосы считаются отдельно от боевых",
            поз_л["open_lane"] == 1, (поз_л["open_lane"], поз_л["open_real"]))
        круги_л = таблица_кругов(st, [], {"known": False})
        ряд_л = [р for р in круги_л if р["client_order_id"] == "lane_r"]
        chk("в таблице кругов у полосы своя метка и свои числа",
            ряд_л and ряд_л[0]["lane"] == ST.МЕТКА_ПОЛОСЫ
            and ряд_л[0]["lane_send_to_seen_ms"] == 120.5
            and ряд_л[0]["lane_bought_raw"] == 8880000
            and ряд_л[0]["bloom_to_seen_ms"] is None, ряд_л)
        ряд_б = [р for р in круги_л if р["client_order_id"] == "c"]
        chk("у покупки Bloom поля полосы пустые, а не нулевые",
            ряд_б and ряд_б[0]["lane"] is None
            and ряд_б[0]["lane_send_to_seen_ms"] is None, ряд_б)
        st.positions_path.write_text(было_позиции, encoding="utf-8")

        поз3 = позиции_срез(st)
        chk("упавшая покупка не попадает в потраченное",
            поз3["live_test"]["spent_sol"] == 0.001, поз3["live_test"])
        chk("но она посчитана отдельно и с суммой намерения",
            поз3["live_test"]["failed_on_chain"] == 1
            and поз3["live_test"]["intent_sol_failed"] == 0.2,
            поз3["live_test"])
        chk("видно, чем покупали", тб[0]["buy_address_kind"] == "pool"
            and тб[0]["buy_address"] == "POOLX", тб[0])
        chk("видно наш маршрут и метку расхождения",
            тб[0]["our_route_hops"] == 2 and "ROUTE_MISMATCH" in тб[0]["flags"], тб[0])
        chk("в таблице позиций есть наша подпись и слот посадки",
            тб[0]["our_signature"] == "НАША_ПОДПИСЬ" and тб[0]["our_slot"] == 101
            and тб[0]["slot_delta"] == 1, тб[0])
        chk("и сколько SOL вернулось при закрытии",
            abs((тб[0]["sol_back"] or 0) - 0.000810693) < 1e-9
            and тб[0]["closed_via"] == "авто-ордер Bloom", тб[0])
        # причина пропуска сверки видна В ДОКЛАДЕ, а не только в логе
        о_без = отчёт(state=st, почему_нет_dbot="DBOT_API_KEY пуст в окружении прогона")
        chk("причина пропуска сверки названа в докладе",
            "DBOT_API_KEY пуст" in (о_без["reconciliation"].get("why") or ""),
            о_без["reconciliation"])
        chk("и в тексте доклада она тоже есть",
            "DBOT_API_KEY пуст" in в_текст(о_без), "")

        # поиск по коду: разбор начинается с того, что владелец видел
        по_к = найти_по_коду([строка(code="INTERMEDIATE_ROUTE", ts_utc="2026-09-24T00:18:07Z"),
                               строка(code="NOT_A_BUY", ts_utc="2026-09-24T00:16:53Z"),
                               строка(code="INTERMEDIATE_ROUTE", ts_utc="2026-09-24T00:18:52Z")],
                              ("intermediate_route",))
        chk("поиск по коду находит оба решения и не путает регистр",
            len(по_к) == 2, по_к)
        chk("и свежие идут первыми",
            по_к[0]["ts_utc"] > по_к[1]["ts_utc"], [r["ts_utc"] for r in по_к])
        chk("предел выборки соблюдается",
            len(найти_по_коду([строка(code="X") for _ in range(30)], ("X",),
                               предел=5)) == 5)

        # поиск по подписи: сигнал был, решения нет -- это надо уметь показать
        нашлись = найти_подпись(
            [строка(signature="ПОДПИСЬ_A"), строка(signature="ПОДПИСЬ_B"),
             {"source_sig": "ПОДПИСЬ_A", "stage": "exec_result"}],
            ("ПОДПИСЬ_A",))
        chk("поиск по подписи находит и решение, и запись исполнителя",
            len(нашлись) == 2, нашлись)
        chk("чужую подпись не приносит",
            найти_подпись([строка(signature="ПОДПИСЬ_B")], ("ПОДПИСЬ_A",)) == [])
        chk("сокращённая подпись тоже находится",
            len(найти_подпись([строка(signature="ПОДПИСЬ_ДЛИННАЯ_XYZ")],
                               ("ПОДПИСЬ_ДЛИННАЯ",))) == 1)
        chk("пустой список подписей ничего не приносит",
            найти_подпись([строка(signature="A")], ()) == [])

        # запись с путём Jupiter должна попасть и в таблицу, и в текст
        st.positions_path.write_text("\n".join([
            json.dumps({"client_order_id": "j1", "state": "closed",
                        "mode": ST.MODE_LIVE_TEST, "sol_in": 0.001, "mint": "MINTJ",
                        "jup_attempts": 1, "jup_signature": "ПОДПИСЬ_JUP",
                        "jup_floor": {"out_amount": 810693, "floor_needed": 567485,
                                       "slippage_bps": 3000, "router": "metis",
                                       "quote_share_of_entry_pct": 81.07},
                        "closed_reason": "остаток ноль дважды подряд",
                        ST.SCHEMA_VERSION_KEY: 2}, ensure_ascii=False),
        ]) + "\n", encoding="utf-8")
        тбj = позиции_таблица(st)
        chk("путь Jupiter виден в таблице позиций",
            тбj[0]["jup_signature"] == "ПОДПИСЬ_JUP"
            and тбj[0]["jup_floor"]["router"] == "metis", тбj[0])
        chk("и причина закрытия тоже",
            "ноль дважды" in тбj[0]["closed_reason"], тбj[0])

        st.positions_path.write_text("\n".join([
            json.dumps({"client_order_id": "c", "state": "bought",
                        "mode": ST.MODE_LIVE_TEST, "sol_in": 0.001,
                        "mint": "MINTX", "buy_address": "POOLX",
                        "buy_address_kind": "pool", "program": "Meteora DLMM",
                        "our_pool": None, "our_pool_direct": False,
                        "our_route_programs": "Raydium CLMM,Raydium CPMM",
                        "our_route_hops": 2, "flags": "ROUTE_MISMATCH",
                        "sell_address_kinds": "pool,mint", "sell_attempts": 2,
                        "unsold_reason": "2 неудачных попыток подряд",
                        ST.SCHEMA_VERSION_KEY: 2}, ensure_ascii=False),
        ]) + "\n", encoding="utf-8")
        тб = позиции_таблица(st)
        chk("видно, чем продавали и почему не продано",
            тб[0]["sell_address_kinds"] == "pool,mint"
            and "неудачных" in тб[0]["unsold_reason"], тб[0])

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

        # --- записи стенда идут отдельным разделом и ЦЕЛИКОМ
        p.write_text("\n".join([
            json.dumps(строка(action="buy", code="BUY", test_source=True,
                              tx_version=0, signature="СТЕНД1"), ensure_ascii=False),
            json.dumps(строка(code="NOT_A_BUY"), ensure_ascii=False),
        ]) + "\n", encoding="utf-8")
        о2 = отчёт(state=st)
        chk("решение стенда в свою таблицу попало",
            len(о2["stand_slot_table"]) == 1, о2["stand_slot_table"])
        chk("боевое решение в таблицу стенда не попало",
            о2["stand_slot_table"][0]["signature"].startswith("СТЕНД1"),
            о2["stand_slot_table"][0]["signature"])
        chk("запись стенда отдана целиком",
            о2["stand_rows"] and о2["stand_rows"][0].get("code") == "BUY",
            о2["stand_rows"])
        т2 = в_текст(о2)
        chk("в тексте есть раздел стенда", "стенд: записи целиком" in т2)
        chk("и запись стенда в тексте видна", "СТЕНД1" in т2)

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
    p.add_argument("--stand-rows", type=int, default=10,
                   help="сколько последних решений стенда печатать целиком")
    p.add_argument("--out", default=None, help="куда положить текст отчёта")
    p.add_argument("--find-code", action="append", default=[],
                   help="показать записи журнала с этим кодом целиком")
    p.add_argument("--find-sig", action="append", default=[],
                   help="показать записи журнала по подписи целиком (можно несколько)")
    p.add_argument("--find-mint", action="append", default=[],
                   help="показать все решения по этому минту (можно несколько)")
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
    почему_нет_dbot = None
    if a.dbot:
        import os  # noqa: PLC0415
        ключ = os.environ.get("DBOT_API_KEY") or ""
        конфиг = Path(a.config) if a.config else None
        # Причина пропуска идёт В ДОКЛАД, а не в stderr прогона: "записи не
        # переданы" без причины выглядело как "сверка невозможна вообще", и
        # гейт live стоял на этом трое суток.
        if not ключ:
            почему_нет_dbot = "DBOT_API_KEY пуст в окружении прогона"
        elif not конфиг or not конфиг.exists():
            почему_нет_dbot = f"снимка конфига нет: {конфиг}"
        else:
            from bloom_ambiguous_diag import записи_dbot  # noqa: PLC0415
            try:
                записи = записи_dbot(ключ, tuple(x.strip() for x in a.tasks.split(",")),
                                     конфиг)
            except Exception as exc:  # noqa: BLE001
                записи = None
                почему_нет_dbot = f"запрос к DBot не удался: {type(exc).__name__}: {str(exc)[:200]}"
            else:
                неполные = sum(1 for r in записи if r.get("_полностью") is False)
                print(f"записи DBot получены: {len(записи)}, из них из неполной "
                      f"выгрузки {неполные}")
                if not записи:
                    почему_нет_dbot = ("DBot ответил, но записей follow по задачам "
                                        f"{a.tasks} за окно нет")

    о = отчёт(state=state, since_ts=since_ts, записи_dbot=записи,
              статус=статус, n_таблицы=a.rows, n_стенда=a.stand_rows,
              найти=tuple(a.find_sig or ()), коды=tuple(a.find_code or ()),
              минты=tuple(a.find_mint or ()),
              почему_нет_dbot=почему_нет_dbot)
    текст = в_текст(о)
    print(текст)
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    ST.atomic_write_json(OUT_JSON, о)
    (Path(a.out) if a.out else OUT_TEXT).write_text(текст + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
