#!/usr/bin/env python3
"""Утренний доклад владельцу за ночь 25.09: боевое, разложение, исследование.

Слово владельца 25.09: "Файл docs/research_2026-09-25_edge.md + JSON и одно
сообщение. В сообщении и наверху файла: 1) БОЕВОЕ ... 2) P1 и P2 таблицами
... 3) Ответы на 4 вопроса ... 4) Абзац: что из данных НЕ следует."

ЧТО ЭТОТ МОДУЛЬ НЕ ДЕЛАЕТ. Он ничего не покупает, не продаёт и не меняет
настроек: только читает журналы службы, признаки жизни и результаты
исследования, считает по ним числа и пишет файл. Поэтому он безопасен на
боевом хосте и запускается там, где журналы и лежат.

ПРАВИЛО ЧИСЕЛ. Чего в данных нет -- в докладе стоит прочерк и причина, а не
ноль и не оценка. Ноль в такой таблице читается как измеренный результат, и
одна такая подмена стоит дороже целого доклада.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ЛАМПОРТОВ_В_SOL = 1_000_000_000

# Издержки на сторону, как их задал владелец и как они стоят в env службы.
# Здесь они нужны только для РАЗЛОЖЕНИЯ итога, а не для торговли: менять
# торговые параметры этот модуль не вправе.
КОМИССИЯ_BLOOM_ДОЛЯ = 0.01          # 1 % от траты на сторону
ПРИОРИТЕТ_SOL = 0.001               # приоритет валидатору, в комиссии транзакции
ЧАЕВЫЕ_ОБРАБОТЧИКУ_SOL = 0.001      # processor_tip, уходит вместе с комиссией Bloom

# ОТКУДА ИЗВЕСТНО, ЧТО ЭТО ИМЕННО ТАК. В нашей покупке PICKAXE (сверка P2 по
# цепи) один перевод на 0.00297688 SOL ушёл на B1dozJAUae1MfexMMoCzrLtTd4JKfJTHLrRfUQswQG5m
# -- это ровно 1 % от фактической траты 0.19769 SOL плюс processor_tip 0.001.
# То есть комиссия площадки и чаевые обработчику идут ОДНИМ переводом, а
# приоритет валидатору живёт отдельно, в комиссии транзакции. Складывать их в
# одну строку нельзя: тогда 0.001 чаевых посчитались бы дважды.
СБОРЩИК_BLOOM = "B1dozJAUae1MfexMMoCzrLtTd4JKfJTHLrRfUQswQG5m"


def читать_jsonl(путь: Path, *, байт: int | None = None) -> list:
    """Строки журнала. Битая строка пропускается -- она не должна ронять доклад."""
    if not путь.exists():
        return []
    try:
        if байт and путь.stat().st_size > байт:
            with путь.open("rb") as f:
                f.seek(путь.stat().st_size - байт)
                f.readline()
                текст = f.read().decode("utf-8", errors="replace")
        else:
            текст = путь.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [{"_читать_не_удалось": f"{type(exc).__name__}"}]
    из_ = []
    for с in текст.split("\n"):
        с = с.strip()
        if not с:
            continue
        try:
            из_.append(json.loads(с))
        except ValueError:
            continue
    return из_


def позиции_из_журнала(строки: list) -> dict:
    """Переигром журнала позиций, как это делает сама служба."""
    из_: dict = {}
    for р in строки:
        cid = р.get("client_order_id")
        if not cid:
            continue
        из_.setdefault(cid, {}).update(р)
    return из_


def _мед(значения: list):
    ч = [float(з) for з in значения if isinstance(з, (int, float))]
    return round(statistics.median(ч), 2) if ч else None


def боевое_полоса(позиции: dict) -> dict:
    """Что полоса своей отправки сделала за ночь -- числами по позициям."""
    свои = [п for п in позиции.values() if п.get("lane")]
    отправлено = [п for п in свои if п.get("lane_signature")]
    закрытые = [п for п in свои if п.get("state") == "closed"]
    непроданные = [п for п in свои if п.get("state") == "unsold"]
    итог_sol = 0.0
    известных = 0
    for п in свои:
        вх, наз = п.get("sol_in"), п.get("closed_sol_net")
        if вх and наз is not None:
            итог_sol += float(наз) - float(вх)
            известных += 1
    return {"positions": len(свои), "sent": len(отправлено),
            "closed": len(закрытые), "unsold": len(непроданные),
            "pnl_sol_known": round(итог_sol, 9) if известных else None,
            "pnl_counted_trades": известных,
            "send_to_seen_ms_median": _мед([п.get("lane_send_to_seen_ms")
                                             for п in свои]),
            "bought_raw_known": sum(1 for п in свои
                                     if isinstance(п.get("lane_bought_raw"), int)),
            "rows": [{"cid": п.get("client_order_id"), "mint": п.get("mint"),
                       "state": п.get("state"),
                       "signature": п.get("lane_signature"),
                       "slot": п.get("own_tx_seen_slot"),
                       "block_index": п.get("block_index"),
                       "block_total": п.get("block_total"),
                       "send_to_seen_ms": п.get("lane_send_to_seen_ms"),
                       "seen_from_decision_ms": п.get("own_tx_seen_ms"),
                       "bought_raw": п.get("lane_bought_raw"),
                       "sol_in": п.get("sol_in"),
                       "closed_sol_net": п.get("closed_sol_net"),
                       "pair_delta_ms": п.get("lane_pair_delta_ms"),
                       "chain_ok": п.get("chain_ok"),
                       "unsold_reason": п.get("unsold_reason")}
                      for п in sorted(свои, key=lambda x: x.get("ts_intent") or 0)]}


def боевое_пары(позиции: dict) -> dict:
    """Пары "Bloom против нашей": кто раньше и на сколько."""
    пары = []
    for п in позиции.values():
        if not п.get("lane") or п.get("lane_pair_delta_ms") is None:
            continue
        блум = None
        for д in позиции.values():
            if д.get("lane") or д.get("source_sig") != п.get("source_sig"):
                continue
            if д.get("own_tx_seen_ts"):
                блум = д
                break
        пары.append({
            "source_sig": п.get("source_sig"), "mint": п.get("mint"),
            "delta_ms": п.get("lane_pair_delta_ms"),
            "lane_slot": п.get("own_tx_seen_slot"),
            "lane_block_index": п.get("block_index"),
            "lane_send_to_seen_ms": п.get("lane_send_to_seen_ms"),
            "bloom_slot": (блум or {}).get("own_tx_seen_slot"),
            "bloom_block_index": (блум or {}).get("block_index"),
            "bloom_to_seen_ms": (блум or {}).get("bloom_to_seen_ms"),
            "bloom_ms": (блум or {}).get("bloom_ms")})
    раньше = [з for з in пары if isinstance(з.get("delta_ms"), (int, float))
              and з["delta_ms"] > 0]
    return {"count": len(пары), "we_were_earlier": len(раньше),
            "delta_ms_median": _мед([з.get("delta_ms") for з in пары]),
            "rows": пары}


def боевое_тень(решения: list) -> dict:
    """Тень за ночь по журналу: сколько собрала, что мешало."""
    тени = [р for р in решения if р.get("stage") == "shadow"]
    по_вердикту: dict = {}
    причины: dict = {}
    for р in тени:
        в = р.get("sim_verdict") or "нет вердикта"
        по_вердикту[в] = по_вердикту.get(в, 0) + 1
        if not р.get("sim_verdict"):
            причина = str(р.get("why_not") or "без причины")[:80]
            причины[причина] = причины.get(причина, 0) + 1
    return {"total": len(тени), "by_verdict": по_вердикту,
            "not_built_reasons": причины,
            "build_ms_median": _мед([р.get("build_ms") for р in тени]),
            "sim_ms_median": _мед([р.get("sim_ms") for р in тени])}


# Метка полосы в записи журнала. ЕДИНСТВЕННЫЙ надёжный признак: поле stage в
# записи полосы затирается стадией самой провести() ("build", "sim", "send"),
# и отбор по stage == "own_send" давал НОЛЬ путей при двух настоящих попытках
# в ночь 25.09 -- то есть доклад прятал ровно то, что должен был показать.
МЕТКА_ПОЛОСЫ = "own_send"


def полосные(решения: list) -> list:
    """Записи полосы: по метке lane, а не по затираемому stage."""
    return [р for р in решения
            if р.get("lane") == МЕТКА_ПОЛОСЫ or р.get("stage") == МЕТКА_ПОЛОСЫ]


def боевое_полоса_попытки(решения: list) -> dict:
    """Путь полосы по журналу: где она останавливалась."""
    свои = полосные(решения)
    по_стадиям: dict = {}
    причины: dict = {}
    for р in свои:
        # Стадия -- то, что положила провести(): build / sim / send / dry.
        с = р.get("stage") or "?"
        if с == МЕТКА_ПОЛОСЫ:
            с = "начало (стадия не записана)"
        по_стадиям[с] = по_стадиям.get(с, 0) + 1
        if р.get("why_not"):
            причины[str(р["why_not"])[:90]] = причины.get(str(р["why_not"])[:90], 0) + 1
    отправлено = [р for р in свои if р.get("sent")]
    сим = [р for р in свои if р.get("code") == "SKIP_SIM_FAIL"]
    return {"paths": len(свои), "sent": len(отправлено),
            "skip_sim_fail": len(сим), "by_stage": по_стадиям,
            "why_not": причины,
            "sim_ms_median": _мед([р.get("sim_ms") for р in свои]),
            "send_ms_median": _мед([р.get("send_ms") for р in свои])}


def решения_по_кодам(решения: list) -> dict:
    """Чем кончились сигналы: код -> сколько. Без этого не видно, ПОЧЕМУ
    полоса молчит: она идёт только там, где мы покупаем, и если покупок нет,
    причина лежит именно здесь."""
    коды: dict = {}
    покупок = 0
    for р in решения:
        д = р.get("action")
        if д not in ("buy", "skip"):
            continue
        код = р.get("code") or "?"
        коды[код] = коды.get(код, 0) + 1
        if д == "buy":
            покупок += 1
    return {"buys": покупок, "by_code": dict(sorted(коды.items(),
                                                     key=lambda x: -x[1]))}


def боевое_пропуски(решения: list) -> dict:
    """Пропуски узкого фильтра и их тень: сберегли или потеряли."""
    пропуски = [р for р in решения
                if р.get("action") == "skip" and р.get("code") == "SKIP_TAXED_ROUTE"]
    тени = {р.get("signature"): р for р in решения if р.get("stage") == "skip_price"}
    строки = []
    сберегли, потеряли, неизвестно = 0, 0, 0
    for р in пропуски:
        т = тени.get(р.get("signature")) or {}
        точки = {з.get("point"): з for з in (т.get("points") or [])}
        через = точки.get("+28.8 с") or {}
        pct = через.get("vs_entry_pct") if через.get("known") else None
        if pct is None:
            неизвестно += 1
        elif pct < 0:
            сберегли += 1
        else:
            потеряли += 1
        строки.append({"signature": р.get("signature"), "mint": р.get("mint"),
                        "source": р.get("source"),
                        "route_fee_bps": р.get("route_transfer_fee_bps"),
                        "token_fee_bps": р.get("token_fee_bps"),
                        "transfers_of_token": р.get("route_transfers_of_token"),
                        "taxed_intermediates": р.get("route_taxed_intermediates"),
                        "price_s1_pct": (точки.get("+1 блок") or {}).get("vs_entry_pct"),
                        "price_288_pct": pct,
                        "shadow_why_not": т.get("why_not")})
    # СБЕРЕЖЁННОЕ В SOL. Считается только по тем пропускам, где тень
    # измерила цену: остальные идут в "неизвестно", а не в ноль.
    размер = 0.2
    сумма = 0.0
    считано = 0
    for с in строки:
        if isinstance(с.get("price_288_pct"), (int, float)):
            сумма += -размер * float(с["price_288_pct"]) / 100.0
            считано += 1
    return {"count": len(пропуски), "shadow_measured": считано,
            "saved_better": сберегли, "lost_better": потеряли,
            "unknown": неизвестно,
            "saved_sol_estimate": round(сумма, 6) if считано else None,
            "assumed_size_sol": размер,
            "rows": строки}


def разложение_закрытых(позиции: dict, *, с_utc: str = "2026-09-24T00:00:00Z") -> dict:
    """Разложение итога закрытых сделок: налог, комиссии, приоритет, ход цены.

    Слово владельца 25.09 (пункт 3). Остаток (residual) -- это и есть ход
    цены: то, что осталось после вычета известных издержек. Он назван
    остатком, а не "ходом цены", там где часть издержек неизвестна: выдавать
    неизвестное за движение цены нельзя.
    """
    порог = с_utc
    строки = []
    for п in позиции.values():
        if п.get("state") != "closed":
            continue
        когда = п.get("ts_intent_utc") or ""
        if когда and когда < порог:
            continue
        вх = п.get("sol_in")
        наз = п.get("closed_sol_net")
        if not вх or наз is None:
            continue
        вх, наз = float(вх), float(наз)
        итог = наз - вх
        # НАЛОГ. Лучшее из того, что есть: сначала налог по НАШЕМУ маршруту
        # (считается с 25.09), потом по маршруту источника, и только потом --
        # налог самого токена из признака позиции. Третий вариант меньше
        # первого, если маршрут передавал токен дважды, и в записи прямо
        # сказано, какой источник взят: иначе по таблице нельзя понять, чего в
        # ней не хватает.
        налог_bps = п.get("our_route_transfer_fee_bps")
        откуда_налог = "our_route"
        if налог_bps is None:
            налог_bps = п.get("route_transfer_fee_bps")
            откуда_налог = "source_route"
        if налог_bps is None:
            налог_bps = п.get("tax_bps")
            откуда_налог = "token_only"
        if налог_bps is None:
            откуда_налог = None
        налог_sol = (вх * float(налог_bps) / 10_000.0
                     if isinstance(налог_bps, (int, float)) else None)
        комиссия_bloom = round((вх + abs(наз)) * КОМИССИЯ_BLOOM_ДОЛЯ, 9)
        чаевые = ЧАЕВЫЕ_ОБРАБОТЧИКУ_SOL * 2
        приоритет = ПРИОРИТЕТ_SOL * 2
        сеть = (п.get("last_sell_outcome") or {}).get("fee_sol")
        известные = (комиссия_bloom + чаевые + приоритет + (налог_sol or 0.0))
        строки.append({
            "cid": п.get("client_order_id"), "mint": п.get("mint"),
            "lane": п.get("lane"), "ts_utc": когда,
            "sol_in": round(вх, 9), "sol_back_net": round(наз, 9),
            "result_sol": round(итог, 9),
            "route_fee_bps": налог_bps, "fee_bps_source": откуда_налог,
            "tax_sol": round(налог_sol, 9) if налог_sol is not None else None,
            "bloom_fee_sol": комиссия_bloom,
            "processor_tips_sol": round(чаевые, 9),
            "priority_sol": round(приоритет, 9),
            "network_fee_sol": сеть,
            "pool_fee_sol": None,
            "residual_sol": round(итог + известные, 9),
            "residual_is_price_move": False,
            "why_not": ("комиссия пула не посчитана (ставки bps по программе пула "
                         "нет ни в кэше, ни в репозитории), поэтому остаток -- это "
                         "ход цены МИНУС комиссия пула"
                         + ("" if налог_sol is not None
                            else "; налог не известен вовсе"))})
    return {"count": len(строки),
            "result_sol_sum": round(sum(с["result_sol"] for с in строки), 9)
                               if строки else None,
            "result_sol_median": _мед([с["result_sol"] for с in строки]),
            "rows": строки}


def прочитать_json(путь: Path) -> dict:
    if not путь.exists():
        return {"missing": f"файла нет: {путь.name}"}
    try:
        return json.loads(путь.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"missing": f"{путь.name} не прочитан ({type(exc).__name__})"}


def собрать(*, state_dir: Path, data_dir: Path, since_utc: str) -> dict:
    решения = читать_jsonl(state_dir / "decisions.jsonl", байт=40_000_000)
    позиции = позиции_из_журнала(читать_jsonl(state_dir / "positions.jsonl"))
    признак = прочитать_json(state_dir / "bloom_detector_heartbeat.json")
    сторож = прочитать_json(state_dir / "seller_heartbeat.json")
    из_ = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "since_utc": since_utc,
        "state_dir": str(state_dir),
        "decisions_rows": len(решения),
        "positions": len(позиции),
        "lane": боевое_полоса(позиции),
        "lane_attempts": боевое_полоса_попытки(решения),
        "pairs": боевое_пары(позиции),
        "shadow": боевое_тень(решения),
        "skips": боевое_пропуски(решения),
        "decisions": решения_по_кодам(решения),
        "closed_breakdown": разложение_закрытых(позиции, с_utc=since_utc),
        # leg_cache -- состояние и ЦЕНА двухшагового замера. Решение владельца
        # про двухшаговый маршрут полосы опирается именно на этот замер,
        # поэтому его числа обязаны быть в докладе, а не только на хосте.
        "heartbeat": {k: признак.get(k) for k in
                       ("updated_utc", "own_send", "shadow", "route_tax_filter",
                        "leg_cache", "credits_day", "telegram",
                        "subscribe_main_path")},
        "seller": {k: сторож.get(k) for k in
                    ("updated_utc", "mode", "jupiter", "positions_in_cycle")},
        "research": {
            "p1": прочитать_json(data_dir / "night_p1.json"),
            "p2": прочитать_json(data_dir / "night_p2.json"),
            "edge": прочитать_json(data_dir / "night_edge.json"),
            "toxic": прочитать_json(data_dir / "night_toxic.json"),
            "leader": прочитать_json(data_dir / "night_leader.json")},
    }
    return из_


def в_текст(о: dict) -> str:
    """Человеческий доклад. Прочерк там, где числа нет."""
    def ч(з, ед=""):
        return "—" if з is None else f"{з}{ед}"

    л = о.get("lane") or {}
    п = о.get("pairs") or {}
    т = о.get("shadow") or {}
    пр = о.get("skips") or {}
    зак = о.get("closed_breakdown") or {}
    поп = о.get("lane_attempts") or {}
    реш = о.get("decisions") or {}
    # РУБИЛЬНИК ПОЛОСЫ -- в текст, а не только в JSON. Остановленная полоса
    # без этой строки читается как "полоса просто молчит", а это разные вещи.
    ноги = (о.get("heartbeat") or {}).get("leg_cache") or {}
    рубильник = ((о.get("heartbeat") or {}).get("own_send") or {}).get("lane_kill")
    полоса_стоит = bool(рубильник and рубильник[0])
    причина_стопа = (рубильник[1] if (рубильник and len(рубильник) > 1) else "") or ""
    строки = [
        f"# Ночь 25.09: боевое и исследование (доклад собран {о.get('generated_utc')})",
        "",
        "## 1. Боевое",
        "",
        # Оговорку про тишину полосы печатаем ТОЛЬКО когда путей ноль: при
        # двух путях она врала бы -- полосу позвали, и остановилась она не на
        # отсутствии покупок, а на своей причине (см. следующий абзац).
        (f"**Решения по сигналам.** Покупок {ч(реш.get('buys'))}; по кодам: "
          + json.dumps(реш.get("by_code") or {}, ensure_ascii=False) + "."
          + (" Полоса идёт только там, где мы покупаем, поэтому её тишина"
             " объясняется этой строкой." if not поп.get("paths") else "")),
        "",
        f"**Полоса своей отправки.** Путей {ч(поп.get('paths'))}, отправлено "
        f"{ч(поп.get('sent'))}, отказ по симуляции (SKIP_SIM_FAIL) "
        f"{ч(поп.get('skip_sim_fail'))}. Позиций полосы {ч(л.get('positions'))}, "
        f"закрыто {ч(л.get('closed'))}, UNSOLD {ч(л.get('unsold'))}. "
        f"Итог по закрытым: {ч(л.get('pnl_sol_known'), ' SOL')} "
        f"(по {ч(л.get('pnl_counted_trades'))} сделкам). "
        f"Медиана от отправки до появления в потоке: "
        f"{ч(л.get('send_to_seen_ms_median'), ' мс')}.",
        # РУБИЛЬНИК -- первой строкой после полосы: это главное о её
        # состоянии, и в утреннем докладе оно не должно быть спрятано в JSON.
        (f"**ПОЛОСА ОСТАНОВЛЕНА РУБИЛЬНИКОМ.** {причина_стопа} "
          "Снимать -- только владельцу; Bloom, сторож продаж и тень работают."
          if полоса_стоит else
          "Рубильник полосы не взведён: полоса отправляет, если сигнал подходит."),
        # ПОЧЕМУ путь не дошёл до отправки -- словами из журнала. Без этого
        # "путей 2, отправлено 0" выглядит как поломка, хотя это может быть
        # честный отказ (например, котировка пула не SOL).
        ("Где путь останавливался: "
          + (", ".join(f"{к} -- {в}" for к, в in
                        (поп.get("by_stage") or {}).items()) or "—")
          + ". Причины: "
          + ("; ".join(f"{к} ({в})" for к, в in
                       (поп.get("why_not") or {}).items()) or "—") + "."),
        "",
        f"**Пары «Bloom против нашей».** Пар {ч(п.get('count'))}, из них мы раньше "
        f"{ч(п.get('we_were_earlier'))}; медиана разницы "
        f"{ч(п.get('delta_ms_median'), ' мс')} (плюс -- мы раньше).",
        "",
        f"**Тень.** Записей {ч(т.get('total'))}, по вердиктам: "
        f"{json.dumps(т.get('by_verdict') or {}, ensure_ascii=False)}. "
        f"Медиана сборки {ч(т.get('build_ms_median'), ' мс')}, "
        f"симуляции {ч(т.get('sim_ms_median'), ' мс')}.",
        # КЭШ НОГ -- условие двухшагового замера И его цена. Без этой строки
        # "тень не собрала" и "замер выключен по бюджету" читаются одинаково.
        ("Кэш ног двухшаговой тени: "
          + ("включён" if ноги.get("enabled") else "выключен")
          + f", котировочных пулов {ч(ноги.get('pools'))}, "
          + f"шаблонов {len(ноги.get('templates') or {})}, "
          + f"скормлено {ч(ноги.get('fed'))}, кредитов {ч(ноги.get('credits'))} "
          + f"из {ч(ноги.get('credit_budget'))}"
          + (f"; выключен: {ноги.get('off_reason')}" if ноги.get("off_reason") else "")
          + "."),
        "",
        f"**Узкий фильтр по налогу маршрута.** Пропусков {ч(пр.get('count'))}, "
        f"тень измерила {ч(пр.get('shadow_measured'))}: цена через 28.8 с была "
        f"ниже входа в {ч(пр.get('saved_better'))} случаях (фильтр сберёг), выше -- "
        f"в {ч(пр.get('lost_better'))} (фильтр отнял), неизвестна в "
        f"{ч(пр.get('unknown'))}. Оценка сбережённого при размере "
        f"{ч(пр.get('assumed_size_sol'))} SOL: {ч(пр.get('saved_sol_estimate'), ' SOL')}.",
        "",
        f"**Разложение закрытых сделок с {о.get('since_utc')}.** Сделок "
        f"{ч(зак.get('count'))}, сумма итога {ч(зак.get('result_sol_sum'), ' SOL')}, "
        f"медиана {ч(зак.get('result_sol_median'), ' SOL')}. По каждой сделке в JSON: "
        "налог в SOL (и откуда взята ставка), комиссия Bloom, чаевые "
        "обработчику, приоритет, комиссия сети и остаток. Комиссия пула НЕ "
        "посчитана: ставки bps по программе пула нет ни в кэше, ни в "
        "репозитории, поэтому остаток -- это ход цены минус комиссия пула.",
        "",
    ]
    # ВЫВОДЫ ИССЛЕДОВАНИЯ -- человеческим текстом, если они подготовлены
    # отдельным файлом. Машинные JSON ниже остаются: по ним число можно
    # проверить, а по тексту -- прочитать. Файла нет -- так и сказано.
    выводы = Path("docs/research_2026-09-25_findings.md")
    строки.append("## Ответы на вопросы владельца (числа)")
    строки.append("")
    if выводы.exists():
        try:
            строки.append(выводы.read_text(encoding="utf-8").strip())
        except OSError as exc:
            строки.append(f"НЕ ПРОЧИТАНО: {type(exc).__name__}")
    else:
        строки.append("НЕ СОБРАНО: файла docs/research_2026-09-25_findings.md нет")
    строки.append("")
    for имя, ключ in (("P1 (тень против Bloom)", "p1"), ("P2 (сверка по цепи)", "p2"),
                       ("A/D (есть ли деньги, правила)", "edge"),
                       ("B/C (токсичность, манипуляции)", "toxic"),
                       ("E/F/G (лидер, GP, StonkFun)", "leader")):
        р = (о.get("research") or {}).get(ключ) or {}
        строки.append(f"## {имя}")
        if р.get("missing"):
            строки.append(f"НЕ СОБРАНО: {р['missing']}")
        else:
            строки.append("```json")
            строки.append(json.dumps(р, ensure_ascii=False, indent=2)[:4000])
            строки.append("```")
        строки.append("")
    строки += [
        "## Что из данных НЕ следует",
        "",
        "Прочерк в таблице -- это не ноль: там, где стоит «—», величина не "
        "измерена, и подставлять вместо неё ноль нельзя. Оценка сбережённого "
        "фильтром считается по цене пула через 28.8 с и при размере сделки "
        "0.2 SOL: это модель одного горизонта, а не факт нашей сделки, которой "
        "не было. Разница пары «Bloom против нашей» измерена по времени "
        "появления транзакций в подписке processed нашего узла; это наш узел, а "
        "не общее время сети.",
        "",
    ]
    return "\n".join(строки)


def self_test() -> int:
    всего = [0, 0]

    def chk(имя, условие, факт=None):
        всего[0] += 1
        if условие:
            всего[1] += 1
            print(f"  [ok  ] {имя}")
        else:
            print(f"  [ПЛОХО] {имя}" + (f" -- {факт}" if факт is not None else ""))

    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as d:
        база = Path(d)
        сост = база / "state"
        сост.mkdir()
        дата = база / "data"
        дата.mkdir()
        # Журнал позиций: одна полоса закрыта, одна покупка Bloom.
        позиции = [
            {"client_order_id": "l1", "state": "closed", "lane": "own_send",
             "mint": "MINTL", "sol_in": 0.01, "closed_sol_net": 0.0108,
             "lane_signature": "ПОДПИСЬ_ПОЛОСЫ", "own_tx_seen_slot": 900,
             "lane_send_to_seen_ms": 120.0, "lane_bought_raw": 8_880_000,
             "lane_pair_delta_ms": 430.0, "source_sig": "SRC1",
             "ts_intent": 1, "ts_intent_utc": "2026-09-25T01:00:00Z",
             "our_route_transfer_fee_bps": 0, "chain_ok": True},
            {"client_order_id": "b1", "state": "closed", "mint": "MINTL",
             "sol_in": 0.2, "closed_sol_net": 0.19, "source_sig": "SRC1",
             "own_tx_seen_ts": 100.0, "own_tx_seen_slot": 901,
             "bloom_to_seen_ms": 550.0, "bloom_ms": 33.8,
             "ts_intent": 1, "ts_intent_utc": "2026-09-25T01:00:00Z",
             "our_route_transfer_fee_bps": 300},
        ]
        (сост / "positions.jsonl").write_text(
            "\n".join(json.dumps(р, ensure_ascii=False) for р in позиции) + "\n",
            encoding="utf-8")
        решения = [
            {"stage": "shadow", "sim_verdict": "would_pass", "build_ms": 3.0,
             "sim_ms": 40.0},
            {"stage": "shadow", "why_not": "нет участка SOL->Q в кэше"},
            {"stage": "own_send", "sent": True, "sim_ms": 60.0, "send_ms": 30.0},
            {"stage": "own_send", "sent": False, "code": "SKIP_SIM_FAIL",
             "why_not": "симуляция не прошла: slippage"},
            # ТАК ЗАПИСЬ ВЫГЛЯДИТ НА ХОСТЕ: stage затёрт стадией провести(),
            # а полосу выдаёт только поле lane. Ровно эта запись и была
            # невидима в ночь 25.09.
            {"stage": "build", "lane": "own_send", "sent": False,
             "why_not": "котировка пула не SOL -- полоса только одношаговая",
             "pool_program": "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"},
            {"action": "skip", "code": "SKIP_TAXED_ROUTE", "signature": "ПРОПУСК1",
             "mint": "MINTP", "route_transfer_fee_bps": 900,
             "token_fee_bps": 300, "route_transfers_of_token": 2},
            {"stage": "skip_price", "signature": "ПРОПУСК1",
             "points": [{"point": "+1 блок", "known": True, "vs_entry_pct": 2.0},
                        {"point": "+28.8 с", "known": True, "vs_entry_pct": -15.0}]},
        ]
        (сост / "decisions.jsonl").write_text(
            "\n".join(json.dumps(р, ensure_ascii=False) for р in решения) + "\n",
            encoding="utf-8")
        (сост / "bloom_detector_heartbeat.json").write_text(
            json.dumps({"updated_utc": "2026-09-25T02:00:00Z",
                        "own_send": {"live": True,
                                      "lane_kill": [True, "полоса остановлена: расхождение учёта"]},
                        "leg_cache": {"enabled": True, "pools": 4, "fed": 1000,
                                       "templates": {"Q1": {}}, "credits": 9964,
                                       "credit_budget": 30000}},
                       ensure_ascii=False),
            encoding="utf-8")
        о = собрать(state_dir=сост, data_dir=дата, since_utc="2026-09-24T00:00:00Z")

        chk("позиции полосы посчитаны отдельно от покупок Bloom",
            о["lane"]["positions"] == 1 and о["lane"]["closed"] == 1,
            о["lane"])
        chk("итог полосы взят по её входу, а не по входу Bloom",
            abs((о["lane"]["pnl_sol_known"] or 0) - 0.0008) < 1e-9,
            о["lane"]["pnl_sol_known"])
        chk("пара собрана и разница взята из позиции полосы",
            о["pairs"]["count"] == 1 and о["pairs"]["rows"][0]["bloom_ms"] == 33.8
            and о["pairs"]["we_were_earlier"] == 1, о["pairs"])
        chk("тень разложена по вердиктам и причинам",
            о["shadow"]["by_verdict"].get("would_pass") == 1
            and о["shadow"]["not_built_reasons"], о["shadow"])
        chk("путь полосы: отправки и отказы по симуляции",
            о["lane_attempts"]["paths"] == 3 and о["lane_attempts"]["sent"] == 1
            and о["lane_attempts"]["skip_sim_fail"] == 1, о["lane_attempts"])
        chk("запись полосы с затёртым stage видна по метке lane",
            о["lane_attempts"]["by_stage"].get("build") == 1
            and any("котировка пула не SOL" in к
                    for к in о["lane_attempts"]["why_not"]),
            о["lane_attempts"])
        chk("пропуск и его тень связаны по подписи",
            о["skips"]["count"] == 1 and о["skips"]["shadow_measured"] == 1
            and о["skips"]["rows"][0]["price_288_pct"] == -15.0, о["skips"])
        chk("сбережённое считается только по измеренным пропускам",
            abs((о["skips"]["saved_sol_estimate"] or 0) - 0.03) < 1e-9,
            о["skips"]["saved_sol_estimate"])
        chk("разложение закрытых сделок посчитано по обеим сделкам",
            о["closed_breakdown"]["count"] == 2, о["closed_breakdown"])
        строка_bloom = [с for с in о["closed_breakdown"]["rows"] if с["cid"] == "b1"][0]
        chk("налог по маршруту переведён в SOL по входу сделки",
            abs(строка_bloom["tax_sol"] - 0.2 * 0.03) < 1e-9, строка_bloom)
        chk("источник ставки налога назван в записи",
            строка_bloom["fee_bps_source"] == "our_route", строка_bloom)
        chk("чаевые обработчику и приоритет стоят РАЗНЫМИ строками",
            abs(строка_bloom["processor_tips_sol"] - 0.002) < 1e-12
            and abs(строка_bloom["priority_sol"] - 0.002) < 1e-12, строка_bloom)
        chk("комиссия пула честно не посчитана и это сказано",
            строка_bloom["pool_fee_sol"] is None
            and "комиссия пула не посчитана" in (строка_bloom["why_not"] or ""),
            строка_bloom["why_not"])
        chk("остаток ходом цены не называется: в нём сидит комиссия пула",
            строка_bloom["residual_is_price_move"] is False, строка_bloom)
        chk("исследование отсутствует -- так и сказано, без нулей",
            all((о["research"][к] or {}).get("missing")
                for к in ("p1", "p2", "edge", "toxic", "leader")), о["research"])
        текст = в_текст(о)
        chk("кэш ног и его цена названы в тексте",
            "Кэш ног двухшаговой тени: включён" in текст
            and "кредитов 9964 из 30000" in текст, текст[:900])
        chk("остановленная полоса названа в ТЕКСТЕ доклада, а не только в JSON",
            "ПОЛОСА ОСТАНОВЛЕНА РУБИЛЬНИКОМ" in текст
            and "расхождение учёта" in текст, текст[:400])
        chk("в тексте есть все четыре раздела владельца",
            all(с in текст for с in ("## 1. Боевое", "P1 (тень против Bloom)",
                                      "Что из данных НЕ следует")), текст[:200])
        chk("прочерк в тексте стоит там, где числа нет",
            "—" in в_текст(собрать(state_dir=дата, data_dir=дата,
                                    since_utc="2026-09-24T00:00:00Z")), "")
        chk("пустой каталог не роняет сборку",
            собрать(state_dir=дата, data_dir=дата,
                     since_utc="2026-09-24T00:00:00Z")["decisions_rows"] == 0, "")
        chk("ключи доклада только ASCII",
            all(к.isascii() for к in о), [к for к in о if not к.isascii()])

    print(f"самопроверка утреннего доклада: {всего[1]}/{всего[0]} пройдено")
    return 0 if всего[1] == всего[0] else 1


def main() -> int:
    p = argparse.ArgumentParser(description="утренний доклад за ночь")
    p.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    p.add_argument("--data-dir", default="data")
    p.add_argument("--since", default="2026-09-24T00:00:00Z")
    p.add_argument("--out-json", default="data/night_report.json")
    p.add_argument("--out-md", default="docs/research_2026-09-25_edge.md")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test:
        return self_test()
    о = собрать(state_dir=Path(a.state_dir), data_dir=Path(a.data_dir),
                 since_utc=a.since)
    Path(a.out_json).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out_json).write_text(json.dumps(о, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    Path(a.out_md).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out_md).write_text(в_текст(о), encoding="utf-8")
    print(в_текст(о))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
