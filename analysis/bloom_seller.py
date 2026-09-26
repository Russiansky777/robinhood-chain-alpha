#!/usr/bin/env python3
"""Сторож продаж исполнителя Bloom. ОТДЕЛЬНЫЙ процесс, не часть детектора.

Зачем отдельный процесс. У зонда детектор и его корутины живут в одном
asyncio.gather без return_exceptions: одно исключение валит всё, а systemd
при StartLimitBurst=5 может оставить службу выключенной. Если сторож
продаж живёт там же, то падение детектора означает открытые позиции без
выхода. Поэтому сторож читает ЖУРНАЛ ПОЗИЦИЙ с диска, а не память
детектора, и продаёт независимо от того, жив ли детектор.

Основной выход -- таймерный авто-ордер Bloom, прикреплённый к покупке
(target_type=time). Сторож -- страховка: через BLOOM_SELL_GRACE_S секунд
после срока ордера он проверяет БАЛАНС ТОКЕНА ПО ЦЕПИ, и если токен на
месте, продаёт сам.

Правила, за каждым из которых стоит цена ошибки:

  * ПЕРЕД КАЖДОЙ попыткой -- баланс по цепи. Иначе сторож гоняется за
    уже проданной позицией, тратит бюджет запросов (60/мин на пользователя)
    и может продать то, чего нет.
  * ПРОСКАЛЬЗЫВАНИЕ НЕ ПОДНИМАЕТСЯ. 40 % и только 40 %: отказ по
    проскальзыванию означает плохой маршрут, а не недостаточную щедрость.
  * ДВА НУЛЯ ПОДРЯД перед закрытием позиции -- защита от гонки с
    индексацией узла (приём проверен на стороже DBot).
  * ПОТОЛОК 10 МИНУТ. Дальше позиция помечается UNSOLD, идёт доклад
    владельцу, и она считается непроданной для автопаузы.
  * РУБИЛЬНИК НЕ ОСТАНАВЛИВАЕТ ПРОДАЖИ. Он запрещает ПОКУПКИ. Оставить
    открытую позицию без выхода опаснее, чем закрыть её; для полного
    останова есть отдельный файл BLOOM_KILL_SELL_FILE.
  * ПОРОГ ПЫЛИ. Остаток меньше порога сырых единиц не продаётся: сторож
    иначе вечно долбит крошку, которую всё равно никто не купит.

Ключи не печатаются: и BLOOM_API_KEY, и HELIUS_API_KEY вычищаются из
любого текста.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests  # noqa: E402

from bloom_api import BloomApi, build_sell_body, scrub  # noqa: E402

try:
    import bloom_notify as NT
except ImportError:  # pragma: no cover
    NT = None

try:
    import bloom_jupiter_sell as JUP
except ImportError:  # pragma: no cover
    JUP = None

# ПОЛОСА ЖИВЁТ НА СВОЁМ КОШЕЛЬКЕ (решение владельца 25.09). Отсюда сторож
# берёт её адрес и её ключ: читать остаток и подписывать продажу надо тем
# кошельком, который покупал. Модуль может не загрузиться (у сторожа нет
# зависимостей полосы) -- тогда позиции полосы просто некому продавать, и это
# видно словами, а не тихой продажей с чужого адреса.
try:
    import bloom_own_send as OSW
except ImportError:  # pragma: no cover
    OSW = None
from bloom_exec_state import (  # noqa: E402
    EXECUTOR_WALLET, STATE_CLOSED, МЕТКА_ПОЛОСЫ, ExecState, append_jsonl_fsync)

# Сколько подписей спрашивать у узла в догоне расхода покупки. Вариантов на
# одном nonce до шести, садится один; трёх хватает, чтобы найти севшую, и это
# не превращается в перебор на каждой закрываемой позиции.
ПРЕДЕЛ_ДОГОНА_ПОДПИСЕЙ = 3


def кошелёк_позиции(pos: dict) -> str:
    """С какого адреса продаётся эта позиция.

    У полосы свой кошелёк, у Bloom -- кошелёк исполнителя. Спутать нельзя в
    обе стороны: читать остаток чужого адреса значит увидеть ноль и объявить
    "продавать нечего", а подписать продажу чужим ключом значит отправить
    транзакцию, которую сеть отвергнет, потратив приоритет и чаевые.
    """
    if pos.get("lane") == МЕТКА_ПОЛОСЫ:
        if OSW is not None:
            return OSW.кошелёк_полосы()
        # МОДУЛЬ ПОЛОСЫ МОГ НЕ ЗАГРУЗИТЬСЯ (у сторожа нет её зависимостей), но
        # адрес полосы лежит в том же окружении. Без этой ветки сторож читал бы
        # остаток с кошелька исполнителя и говорил "продавать нечего" при
        # полном кошельке полосы.
        свой = (os.environ.get("OWN_SEND_WALLET") or "").strip()
        if свой:
            return свой
    return EXECUTOR_WALLET


def ключ_позиции(pos: dict) -> str | None:
    """Секрет для подписи продажи. None -- значит ключ по умолчанию (ключ
    исполнителя из окружения); его подставляет сам bloom_jupiter_sell."""
    if pos.get("lane") == МЕТКА_ПОЛОСЫ and OSW is not None:
        return OSW.ключ_полосы()
    return None


def остаток_можно_продать_целиком(pos: dict) -> tuple:
    """Разрешено ли продать по этой позиции ВЕСЬ остаток минта. (можно, почему нет).

    Разрешение владельца 25.09: "Кошелёк отдельный, чужих токенов там нет,
    поэтому это безопасно". Отсюда и рамка -- целиком остаток продаётся только
    с ОТДЕЛЬНОГО кошелька полосы. С кошелька исполнителя нельзя никогда: там
    лежат покупки Bloom, и продажа всего остатка продала бы их, посчитав чужую
    выручку своей.

    Адрес берётся живой (кошелёк_позиции), а не поле "wallet" из записи: до
    25.09 write_intent писала всем подряд адрес исполнителя, и у покупок
    полосы в записи стоит не тот кошелёк. Но если в записи стоит СВОЙ адрес и
    он не совпадает с нынешним кошельком полосы -- кошелёк сменили после
    покупки, и остаток на новом адресе к этой позиции не относится.
    """
    if pos.get("lane") != МЕТКА_ПОЛОСЫ:
        return False, "позиция не полосы"
    кошелёк = кошелёк_позиции(pos)
    if not кошелёк:
        return False, "адрес кошелька полосы неизвестен"
    if кошелёк == EXECUTOR_WALLET:
        return False, ("у полосы нет своего кошелька: на кошельке исполнителя "
                        "лежат покупки Bloom")
    записан = (pos.get("wallet") or "").strip()
    if записан and записан != EXECUTOR_WALLET and записан != кошелёк:
        return False, (f"в записи кошелёк {записан}, а у полосы сейчас "
                        f"{кошелёк}: остаток не этой позиции")
    return True, ""


PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
HELIUS_RPC = "https://mainnet.helius-rpc.com"
TOKEN_CLASSIC = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ЛАМПОРТОВ_В_SOL = 1_000_000_000

DEFAULT_SELL_SLIPPAGE_PCT = 40.0
DEFAULT_GRACE_S = 15.0            # столько ждём после срока таймерного ордера
DEFAULT_RETRY_EVERY_S = 45.0
DEFAULT_GIVE_UP_AFTER_S = 600.0   # десять минут
DEFAULT_LOOP_EVERY_S = 15.0
DEFAULT_DUST_RAW = 1000           # меньше -- крошка, продавать нечего
DEFAULT_PRIORITY_FEE = 0.001
DEFAULT_PROCESSOR_TIP = 0.001

# СКОЛЬКО ЖДЁМ КОЛИЧЕСТВО, КУПЛЕННОЕ ПОЛОСОЙ. Его добирает пульс детектора
# вызовом getTransaction, и попыток у него три.
#
# БЫЛО 300 с, И ЭТО СТОИЛО ДЕНЕГ. Смысл прежнего ожидания был в том, что без
# количества продавать нечего: остаток минта якобы общий с покупкой Bloom. С
# 25.09 у полосы СВОЙ кошелёк, чужих токенов там нет, и владелец разрешил
# продавать весь остаток. Ожидание же осталось -- и замер 25.09 показал цену:
# от срока продажи (28.8 с) до попытки прошло 283.9 и 284.1 с, то есть токен
# держался почти пять минут вместо 28.8 с. За это время котировка одной
# позиции уехала с 11.8 % входа до 8.94 %.
#
# ДЕСЯТЬ СЕКУНД. Догон количества либо успевает за три попытки пульса, либо не
# успевает вовсе; ждать дольше нечего, а горизонт сравнения с Bloom (28.8 с)
# обязан остаться честным.
DEFAULT_LANE_AMOUNT_WAIT_S = 10.0


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    return int(env_float(name, float(default)))


def kill_sell_file() -> Path:
    p = os.environ.get("BLOOM_KILL_SELL_FILE", "").strip()
    return Path(p) if p else Path("/etc/bloom-executor/KILL_SELL")


def secrets_for_scrub() -> list:
    return [v for v in (os.environ.get("BLOOM_API_KEY", ""),
                         os.environ.get("HELIUS_API_KEY", ""),
                         os.environ.get("HELIUS_API", "")) if v]


def scrub_all(text: str) -> str:
    for k in secrets_for_scrub():
        text = scrub(text, k)
    return text


_МЕТР = None
_МЕТР_ПРОБОВАЛИ = False
_УЧЁТ_ПИШЕТСЯ = None
_УЧЁТ_ПОЧЕМУ = ""


def метр_кредитов():
    """Счётчик кредитов сторожа. Один на процесс, создаётся лениво.

    Модель кредитов берётся из solana_rpc_client -- та, что уже
    согласована. Пишется в каталог состояния: на хосте дерево репозитория
    службе недоступно (ProtectHome=read-only).
    """
    global _МЕТР, _МЕТР_ПРОБОВАЛИ  # noqa: PLW0603
    if _МЕТР_ПРОБОВАЛИ:
        return _МЕТР
    _МЕТР_ПРОБОВАЛИ = True
    try:
        from bloom_exec_state import state_dir  # noqa: PLC0415
        from solana_rpc_client import CreditMeter  # noqa: PLC0415
        каталог = state_dir() / "helius_usage"
        каталог.mkdir(parents=True, exist_ok=True)
        _МЕТР = CreditMeter("bloom_seller", base=каталог)
    except Exception:  # noqa: BLE001
        _МЕТР = None       # учёт не должен мешать продавать
    return _МЕТР


def rpc_call(method: str, params: list, *, timeout: int = 20) -> dict:
    """Один вызов узла: Helius, при отказе -- публичный. Ключ не печатается."""
    key = (os.environ.get("HELIUS_API_KEY") or os.environ.get("HELIUS_API") or "").strip()
    адреса = ([f"{HELIUS_RPC}/?api-key={key}"] if key else []) + [PUBLIC_RPC]
    последняя = "адресов узла нет"
    for url in адреса:
        try:
            r = requests.post(url, json={"jsonrpc": "2.0", "id": 1,
                                          "method": method, "params": params},
                               timeout=timeout)
        except requests.RequestException as exc:
            последняя = scrub_all(f"{type(exc).__name__}: {exc}")
            continue
        if url != PUBLIC_RPC:
            м = метр_кредитов()
            if м is not None:
                global _УЧЁТ_ПИШЕТСЯ, _УЧЁТ_ПОЧЕМУ  # noqa: PLW0603
                try:
                    from solana_rpc_client import (  # noqa: PLC0415
                        CREDITS_BY_METHOD, CREDITS_DEFAULT)
                    м.add(CREDITS_BY_METHOD.get(method, CREDITS_DEFAULT),
                          bytes_in=len(r.content or b""))
                    _УЧЁТ_ПИШЕТСЯ, _УЧЁТ_ПОЧЕМУ = True, ""
                except Exception as exc:  # noqa: BLE001
                    # Продавать учёт не мешает, но молчать он не должен:
                    # так уже потерялся весь расход детектора.
                    _УЧЁТ_ПИШЕТСЯ = False
                    _УЧЁТ_ПОЧЕМУ = f"{type(exc).__name__}: {str(exc)[:160]}"
        if r.status_code != 200:
            последняя = scrub_all(f"HTTP {r.status_code}: {r.text[:200]}")
            continue
        try:
            body = r.json()
        except ValueError:
            последняя = "ответ узла не json"
            continue
        if "error" in body:
            последняя = scrub_all(f"RPC error: {str(body['error'])[:200]}")
            continue
        return {"ok": True, "result": body.get("result")}
    return {"ok": False, "why_not": последняя}


def _счета_из_ответа(r: dict, mint: str) -> tuple:
    """Сумма, ui и число счетов из ответа getTokenAccountsByOwner."""
    сумма, ui, счетов = 0, 0.0, 0
    for it in ((r.get("result") or {}).get("value") or []):
        info = ((((it.get("account") or {}).get("data") or {}).get("parsed") or {})
                .get("info") or {})
        if info.get("mint") and info.get("mint") != mint:
            continue          # запасной путь берёт счета программы целиком
        amt = info.get("tokenAmount") or {}
        try:
            сумма += int(amt.get("amount") or 0)
        except (TypeError, ValueError):
            pass
        ui += float(amt.get("uiAmount") or 0.0)
        счетов += 1
    return сумма, ui, счетов


def token_balance_raw(wallet: str, mint: str) -> dict:
    """Остаток токена НА ЦЕПИ.

    Фильтр у getTokenAccountsByOwner -- РОВНО ОДИН: либо mint, либо
    programId. Оба вместе узел отвергает как неверные параметры, и это уже
    стоило живого стенда: сторож 23.09 не сделал ни одной попытки продажи
    при открытой позиции, потому что каждый круг получал отказ, а ветка
    "остаток не прочитан" была единственной, которая при этом молчала.

    Фильтра по минту достаточно и для Token-2022: минт принадлежит одной
    программе токена, и счета этого минта заводятся в ней же. Запасной
    путь (фильтр по программе с отбором по минту у нас) оставлен на случай,
    если узел по минту не ответит -- но он дороже и берёт лишние данные.
    """
    r = rpc_call("getTokenAccountsByOwner", [wallet, {"mint": mint},
                                              {"encoding": "jsonParsed"}])
    if r.get("ok"):
        сумма, ui, счетов = _счета_из_ответа(r, mint)
        return {"ok": True, "raw": сумма, "ui": ui, "accounts": счетов,
                 "filter": "mint", "failures": []}

    сбои = [{"filter": "mint", "why_not": r.get("why_not")}]
    сумма, ui, счетов = 0, 0.0, 0
    удалось = False
    for prog in (TOKEN_CLASSIC, TOKEN_2022):
        r2 = rpc_call("getTokenAccountsByOwner", [wallet, {"programId": prog},
                                                   {"encoding": "jsonParsed"}])
        if not r2.get("ok"):
            сбои.append({"filter": f"programId:{prog}", "why_not": r2.get("why_not")})
            continue
        удалось = True
        с, u, к = _счета_из_ответа(r2, mint)
        сумма += с
        ui += u
        счетов += к
    if not удалось:
        return {"ok": False, "failures": сбои,
                 "why_not": "остаток не прочитан ни по минту, ни по программам токена"}
    return {"ok": True, "raw": сумма, "ui": ui, "accounts": счетов,
             "filter": "programId", "failures": сбои}


def kill_sell_active(state=None) -> tuple[bool, str]:
    """Отдельный рубильник ПРОДАЖ. Fail-closed, как и основной.

    Путей ДВА. Первый -- /etc/bloom-executor/KILL_SELL, его ставит владелец
    или деплой. Второй -- файл в каталоге состояния, который служба может
    создать сама: именно через него работает команда /kill_sell из Telegram,
    потому что каталог /etc принадлежит root, а служба работает от bot и
    писать туда не может. Любой из двух файлов -- запрет.
    """
    пути = [kill_sell_file()]
    if state is not None and getattr(state, "kill_sell_path", None):
        пути.append(state.kill_sell_path)
    try:
        for p in пути:
            if p.exists():
                try:
                    почему = p.read_text(encoding="utf-8").strip()[:200]
                except OSError:
                    почему = "(файл не читается -- всё равно запрет)"
                return True, f"рубильник продаж включён: {почему or 'без пояснения'}"
        return False, ""
    except Exception as exc:  # noqa: BLE001
        return True, (f"проверка рубильника продаж не удалась ({type(exc).__name__}) -- "
                       "продажи запрещены: неясность трактуется как запрет")


def telegram(text: str) -> dict:
    """Доклад владельцу. Не настроен -- только в журнал, без падения."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        return {"ok": False, "why_not": "телеграм не настроен -- только журнал"}
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                           json={"chat_id": chat, "text": text[:3500],
                                 "disable_web_page_preview": True}, timeout=15)
        return {"ok": r.status_code == 200, "code": r.status_code}
    except requests.RequestException as exc:
        return {"ok": False, "why_not": scrub_all(f"{type(exc).__name__}: {exc}")}


def due_for_watch(pos: dict, *, grace_s: float, now: float | None = None) -> bool:
    """Пора ли сторожу смотреть на эту позицию.

    Срок считается от ПРИНЯТИЯ запроса Bloom -- так работает таймер
    авто-ордера, и так же должен считать сторож. Если принятия не было
    (упали до ответа), берётся время намерения: позиция всё равно могла
    быть куплена.
    """
    now = now if now is not None else time.time()
    if pos.get("state") == STATE_CLOSED:
        return False
    основа = pos.get("ts_accepted") or pos.get("ts_intent")
    if not основа:
        return True          # непонятно когда -- значит смотреть сейчас
    срок = float(pos.get("sell_after_s") or 0.0)
    return now >= (float(основа) + срок + grace_s)


def срок_полосы(pos: dict, *, now: float | None = None) -> bool:
    """Пора ли продавать позицию ПОЛОСЫ своей отправки.

    Срок считается от НАШЕЙ отправки и БЕЗ запаса: авто-ордера Bloom у полосы
    нет вовсе, значит после срока ждать нечего. Сам срок -- 28.8 с, как у
    боевых покупок: сравнение полосы с Bloom честно только на одинаковом
    горизонте.
    """
    now = now if now is not None else time.time()
    if pos.get("state") == STATE_CLOSED:
        return False
    основа = pos.get("ts_sent") or pos.get("ts_accepted") or pos.get("ts_intent")
    if not основа:
        return True          # непонятно когда -- значит смотреть сейчас
    return now >= (float(основа) + float(pos.get("sell_after_s") or 0.0))


ПРЕДЕЛ_НЕУДАЧ = env_int("BLOOM_SELL_MAX_ATTEMPTS", 2)

# ПОЛОСА ПРОДАЁТСЯ ПРИ ЛЮБОЙ КОТИРОВКЕ. Решение владельца 25.09 (вечер):
# "позиции полосы (кошелёк 21DqHDDP..., размер <= 0.05 SOL) продавать по
# таймеру при любой котировке, без остановки и без запроса владельцу; правило
# «котировка < 30 % → стоп» остаётся для Bloom". Размер в рамке остаётся: это
# разрешение на маленькие сделки полосы, а не на любые.
РАЗМЕР_ПОЛОСЫ_БЕЗ_ПОЛА_SOL = 0.05


def продавать_при_любой_котировке(pos: dict) -> bool:
    if pos.get("lane") != МЕТКА_ПОЛОСЫ:
        return False
    try:
        вход = float(pos.get("sol_in") or 0.0)
    except (TypeError, ValueError):
        return False
    return 0.0 < вход <= РАЗМЕР_ПОЛОСЫ_БЕЗ_ПОЛА_SOL + 1e-9


def адрес_продажи(pos: dict, *, попытка: int) -> tuple:
    """Чем продаём: ID пула НАШЕЙ покупки или минт. Возвращает (адрес, вид).

    Первая попытка -- по пулу, но только если наша покупка прошла ОДНИМ
    пулом токен/WSOL. Причина из живого стенда: по минту маршрут выбирает
    Bloom, и на п. 1 он выбрал двухшаговый через пул без ликвидности в
    диапазоне -- продажа упала с "assertion failed: liquidity > 0".
    Если пул неизвестен или маршрут покупки был многохоповый -- по минту:
    одного пула нашей покупки в этом случае просто нет, и выдумывать его
    нельзя.

    Вторая попытка -- всегда по минту: если по пулу не вышло, пусть Bloom
    ищет маршрут сам. Третьей попытки нет, см. ПРЕДЕЛ_НЕУДАЧ.
    """
    пул = (pos.get("our_pool") or "").strip()
    прямой = bool(pos.get("our_pool_direct"))
    if попытка <= 1 and пул and прямой:
        return пул, "pool"
    return pos.get("mint"), "mint"


def сдаться_по_неудачам(pos: dict, *, предел: int = ПРЕДЕЛ_НЕУДАЧ) -> bool:
    """Две неудачные попытки -- дальше не тратим десять минут.

    Неудача считается по ЦЕПИ, а не по ответу Bloom: 200 означает только
    приём запроса. Если после попытки остаток токена на месте и пауза
    выждана, попытка не сработала. Поэтому предел считается по числу
    сделанных попыток при живом остатке.
    """
    return int(pos.get("sell_attempts") or 0) >= max(1, предел)


def give_up(pos: dict, *, give_up_after_s: float, now: float | None = None) -> bool:
    """Пора ли сдаваться и звать владельца."""
    now = now if now is not None else time.time()
    первая = pos.get("ts_first_sell_attempt")
    if not первая:
        return False
    return (now - float(первая)) >= give_up_after_s


def итог_продажи(tx: dict, wallet: str, mint: str) -> dict:
    """Что вышло из отправленной продажи -- ПО ЦЕПИ, а не по ответу Bloom.

    200 у Bloom означает только приём запроса. Единственный честный
    источник -- транзакция: ошибка инструкции или изменение остатков. SOL
    считается по нативной дельте нашего кошелька вместе с комиссией:
    владельцу нужен результат, а не выручка до вычета.
    """
    if not tx:
        return {"known": False, "why_not": "узел не отдал транзакцию продажи"}
    мета = (tx or {}).get("meta") or {}
    ошибка = мета.get("err")
    код = None
    if ошибка is not None:
        try:
            ошибки = ошибка.get("InstructionError")
            код = str(ошибки[1]) if ошибки else str(ошибка)[:80]
        except Exception:  # noqa: BLE001
            код = str(ошибка)[:80]
    sol = None
    минт_дельта = None
    try:
        import bloom_detector as BD  # noqa: PLC0415
        б = BD.балансы_кошелька(tx, wallet)
        sol = б.get("native_delta_sol")
        з = (б.get("by_mint") or {}).get(mint) or {}
        минт_дельта = з.get("delta_ui")
    except Exception as exc:  # noqa: BLE001
        return {"known": True, "ok": ошибка is None, "error_code": код,
                 "slot": tx.get("slot"),
                 "why_not": f"балансы не разобраны: {type(exc).__name__}"}
    # sol_delta -- движение SOL по сделке, ОЧИЩЕННОЕ от комиссии сети (так
    # же считает учёт: иначе своя комиссия выглядит как часть цены).
    # sol_delta_net -- то, что реально осело на балансе. Оба числа нужны:
    # первое сравнимо с ценой, второе -- с балансом.
    комиссия = (мета.get("fee") or 0) / 1e9
    return {"known": True, "ok": ошибка is None, "error_code": код,
             "slot": tx.get("slot"), "sol_delta": sol,
             "sol_delta_net": (round(sol - комиссия, 9) if isinstance(sol, (int, float))
                                else None),
             "fee_sol": комиссия, "mint_delta_ui": минт_дельта}


def счета_минта(wallet: str, mint: str) -> list:
    """Наши токен-счета этого минта -- ВКЛЮЧАЯ уже закрытые.

    Закрытый счёт узел в getTokenAccountsByOwner больше не отдаёт, поэтому
    адрес выводится: ATA от кошелька и минта для обеих программ токена. История
    закрытого счёта в цепи остаётся и отвечает на вопрос, чем он закрылся.
    """
    из_ = []
    try:
        import c2_swap_build as B  # noqa: PLC0415
        for программа in (B.TOKEN_PROGRAM, TOKEN_2022):
            адрес = B.ata(wallet, mint, программа)
            if адрес and адрес not in из_:
                из_.append(адрес)
    except Exception:  # noqa: BLE001
        pass
    return из_


def дельта_по_счёту(tx: dict, счёт: str):
    """Дельта остатка КОНКРЕТНОГО токен-счёта. ПРОПАВШАЯ СТРОКА -- ЭТО НОЛЬ.

    ЗАЧЕМ ОТДЕЛЬНО ОТ БАЛАНСОВ ПО ВЛАДЕЛЬЦУ. Продажа часто идёт вместе с
    закрытием токен-счёта одной транзакцией, и закрытый счёт из
    postTokenBalances исчезает совсем. Проверка "остаток уменьшился" по
    владельцу и минту такую продажу не видит вовсе: строки в post нет, и
    дельта выходит None -- "нет данных" вместо "ушёл в ноль". Позиция тогда
    остаётся открытой навсегда, а выручка не попадает в суточный счёт.
    """
    мета = (tx or {}).get("meta") or {}
    ключи = [k.get("pubkey") if isinstance(k, dict) else k
             for k in ((((tx or {}).get("transaction") or {}).get("message") or {})
                       .get("accountKeys") or [])]
    try:
        и = ключи.index(счёт)
    except ValueError:
        return None

    def взять(сторона):
        for b in мета.get(сторона) or []:
            if isinstance(b, dict) and b.get("accountIndex") == и:
                try:
                    return int((b.get("uiTokenAmount") or {}).get("amount"))
                except (TypeError, ValueError):
                    return None
        return 0            # строки нет -- остаток ноль

    до = взять("preTokenBalances")
    после = взять("postTokenBalances")
    if до is None:
        return None
    return после - до


def натив_кошелька(tx: dict, кошелёк: str):
    """Нативная дельта кошелька в этой транзакции, SOL (вместе с комиссией)."""
    мета = (tx or {}).get("meta") or {}
    ключи = [k.get("pubkey") if isinstance(k, dict) else k
             for k in ((((tx or {}).get("transaction") or {}).get("message") or {})
                       .get("accountKeys") or [])]
    try:
        и = ключи.index(кошелёк)
    except ValueError:
        return None
    до = (мета.get("preBalances") or [])
    после = (мета.get("postBalances") or [])
    if и >= len(до) or и >= len(после):
        return None
    return round((int(после[и]) - int(до[и])) / ЛАМПОРТОВ_В_SOL, 9)


def закрывающая_по_счёту(wallet: str, mint: str, *, предел: int = 25,
                          читатель_tx=None, подписи_фн=None, счета_фн=None) -> dict:
    """Закрывающая транзакция по истории САМОГО токен-счёта.

    Запасной путь к поиску по истории кошелька: у занятого кошелька продажа
    уходит за окно последних подписей, а у токен-счёта в истории ровно наши
    покупка и продажа. Здесь же считается дельта по строкам счёта -- то есть
    пропавшая строка читается как ноль.
    """
    из_ = {"found": False, "signature": None, "outcome": None, "looked": 0,
            "accounts": [], "why_not": None}
    if подписи_фн is None:
        def подписи_фн(адрес, лимит):  # noqa: E306
            r = rpc_call("getSignaturesForAddress", [адрес, {"limit": лимит}])
            return (r.get("result") or []) if r.get("ok") else []
    адреса = (счета_фн or счета_минта)(wallet, mint)
    if not адреса:
        из_["why_not"] = ("адрес токен-счёта не выведен (нет solders в окружении) -- "
                          "запасной путь по истории счёта недоступен")
        return из_
    for счёт in адреса:
        из_["accounts"].append(счёт)
        for зап in подписи_фн(счёт, предел) or []:
            подпись = (зап or {}).get("signature")
            if not подпись:
                continue
            tx = читатель_tx(подпись) if читатель_tx else None
            из_["looked"] += 1
            if not tx:
                continue
            исход = итог_продажи(tx, wallet, mint)
            дельта = исход.get("mint_delta_ui")
            if not isinstance(дельта, (int, float)) or дельта >= 0:
                сырая = дельта_по_счёту(tx, счёт)
                if сырая is not None and сырая < 0:
                    натив = натив_кошелька(tx, wallet)
                    исход = {**исход, "mint_delta_ui": None,
                              "mint_delta_raw": сырая,
                              "sol_delta": натив, "sol_delta_net": натив,
                              "why_not": ("остаток счёта ушёл в ноль вместе с закрытием "
                                           "счёта -- дельта посчитана по строкам счёта")}
                    дельта = -1.0
            if isinstance(дельта, (int, float)) and дельта < 0:
                из_.update(found=True, signature=подпись,
                            block_time=(зап or {}).get("blockTime"), outcome=исход)
                return из_
    из_["why_not"] = (f"в истории токен-счетов ({', '.join(из_['accounts']) or 'адрес не выведен'}) "
                       f"нет транзакции, уменьшившей остаток минта; осмотрено {из_['looked']}")
    return из_


def найти_закрывающую(wallet: str, mint: str, *, предел: int = 8,
                       читатель_tx=None, подписи_фн=None, счета_фн=None) -> dict:
    """Чем именно закрылась позиция, когда остаток стал нулём.

    Основной выход стенда -- таймерный авто-ордер Bloom, и его подпись нам
    НЕ сообщают: ответ /swap отдаёт подписи только по своему вызову. Поэтому
    закрытие по нулевому остатку раньше проходило молча: позиция закрывалась
    в журнале позиций, а владелец не получал ни строки. Ищем транзакцию по
    цепи: последние подписи кошелька, первая, в которой остаток НАШЕГО минта
    уменьшился -- она и закрыла позицию.
    """
    if подписи_фн is None:
        def подписи_фн(адрес, лимит):  # noqa: E306
            r = rpc_call("getSignaturesForAddress", [адрес, {"limit": лимит}])
            return (r.get("result") or []) if r.get("ok") else []
    строки = подписи_фн(wallet, предел) or []
    осмотрено = []
    for зап in строки:
        подпись = (зап or {}).get("signature")
        if not подпись:
            continue
        tx = читатель_tx(подпись) if читатель_tx else None
        if not tx:
            осмотрено.append({"signature": подпись, "why_not": "транзакция не прочитана"})
            continue
        итог = итог_продажи(tx, wallet, mint)
        дельта = итог.get("mint_delta_ui")
        if isinstance(дельта, (int, float)) and дельта < 0:
            return {"found": True, "signature": подпись,
                     "block_time": (зап or {}).get("blockTime"),
                     "outcome": итог}
        осмотрено.append({"signature": подпись, "mint_delta_ui": дельта})
    # ЗАПАСНОЙ ПУТЬ -- ПО ИСТОРИИ ТОКЕН-СЧЁТА. У занятого кошелька продажа
    # уходит за окно последних подписей, и тогда позиция закрывалась без
    # возврата: выручка не попадала в суточный счёт. Плюс там дельта считается
    # по строкам счёта, где пропавшая строка -- это ноль, а не "нет данных".
    по_счёту = закрывающая_по_счёту(wallet, mint, читатель_tx=читатель_tx,
                                     подписи_фн=подписи_фн, счета_фн=счета_фн)
    if по_счёту.get("found"):
        по_счёту["fallback"] = "история токен-счёта"
        return по_счёту
    return {"found": False, "checked": len(строки), "seen": осмотрено[:5],
             "why_not": (f"среди последних {len(строки)} подписей кошелька нет "
                          "транзакции, уменьшившей остаток этого минта; по истории "
                          f"токен-счёта тоже нет ({по_счёту.get('why_not')})")}


class Seller:
    def __init__(self, *, state: ExecState | None = None, live: bool | None = None,
                  api: BloomApi | None = None, подписи_читатель=None,
                  tx_читатель=None) -> None:
        # ЧИТАТЕЛЬ ПОДПИСЕЙ КОШЕЛЬКА -- передаваемый. По умолчанию None, и
        # тогда путь идёт в сеть, как в бою. Передают его, чтобы сеть НЕ
        # трогать: 25.09 страж самопроверок поймал, что самопроверка сторожа
        # ходила в публичный RPC через доложить_закрытие -- тихо, но ходила.
        self.подписи_читатель = подписи_читатель
        # ЧИТАТЕЛЬ ТРАНЗАКЦИЙ -- тоже передаваемый, и по той же причине:
        # доложить_прошлую_попытку зовёт его по умолчанию, а это getTransaction
        # в сеть. Передан -- затеняет метод, и тогда сети нет вовсе.
        if tx_читатель is not None:
            self.tx_читатель = tx_читатель
        self.state = state or ExecState()
        self.live = (os.environ.get("BLOOM_LIVE_SELL", "0").strip() == "1"
                     if live is None else bool(live))
        self.slippage = env_float("BLOOM_SELL_SLIPPAGE_PCT", DEFAULT_SELL_SLIPPAGE_PCT)
        self.grace_s = env_float("BLOOM_SELL_GRACE_S", DEFAULT_GRACE_S)
        self.retry_every_s = env_float("BLOOM_SELL_RETRY_EVERY_S", DEFAULT_RETRY_EVERY_S)
        self.give_up_after_s = env_float("BLOOM_SELL_GIVE_UP_AFTER_S",
                                          DEFAULT_GIVE_UP_AFTER_S)
        self.dust_raw = env_int("BLOOM_DUST_RAW", DEFAULT_DUST_RAW)
        self.предел_неудач = env_int("BLOOM_SELL_MAX_ATTEMPTS", ПРЕДЕЛ_НЕУДАЧ)
        self.жалоба_каждые_s = env_float("BLOOM_SELL_COMPLAIN_EVERY_S", 60.0)
        # Сколько ждём количество, купленное полосой, прежде чем признать
        # позицию UNSOLD. Продавать без него нельзя: см. DEFAULT_LANE_*.
        self.ждать_количество_s = env_float("BLOOM_LANE_AMOUNT_WAIT_S",
                                             DEFAULT_LANE_AMOUNT_WAIT_S)
        self._жалобы: dict = {}
        self.оповещатель = NT.Оповещатель() if NT is not None else None
        # Путь через Jupiter -- отдельным выключателем: он требует ключа
        # кошелька в окружении, и включать его молча нельзя.
        self.jupiter_включён = (os.environ.get("BLOOM_SELL_VIA_JUPITER", "0").strip() == "1")
        # Сверка кошелька один раз при старте -- чтобы несовпадение было
        # видно в признаке жизни сразу, а не только в момент продажи.
        self.jupiter_ключ = ({"ok": False, "why_not": "путь выключен"}
                              if not self.jupiter_включён or JUP is None else
                              JUP.ключ_от_нашего_кошелька(EXECUTOR_WALLET))
        self.priority_fee = env_float("BLOOM_PRIORITY_FEE", DEFAULT_PRIORITY_FEE)
        self.processor_tip = env_float("BLOOM_PROCESSOR_TIP", DEFAULT_PROCESSOR_TIP)
        self.api = api or BloomApi(os.environ.get("BLOOM_API_KEY", ""),
                                   dry_run=not self.live, state=self.state)

    def log(self, row: dict) -> None:
        append_jsonl_fsync(self.state.base / "seller.jsonl",
                            {"ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                             **row})

    def tx_читатель(self, подпись: str):
        """Транзакция по подписи. Потолок версии тот же, что у детектора:
        разные потолки уже прятали упавшую продажу версии 1."""
        r = rpc_call("getTransaction",
                      [подпись, {"encoding": "jsonParsed",
                                 "maxSupportedTransactionVersion":
                                     env_int("BLOOM_MAX_TX_VERSION", 1),
                                 "commitment": "confirmed"}])
        return r.get("result") if r.get("ok") else None

    def доложить_закрытие(self, pos: dict, *, now: float,
                           читатель_tx=None) -> dict:
        """Строка о закрытии позиции: чем продано, за сколько секунд, сколько SOL.

        Через что продано -- НЕ догадка: если закрывающая подпись совпала с
        нашей последней попыткой, значит продал сторож; иначе продал таймерный
        авто-ордер Bloom. Не нашли транзакцию -- так и сказано, без числа
        вместо неизвестного.
        """
        cid = pos.get("client_order_id")
        # КОШЕЛЁК ПОЗИЦИИ, А НЕ ИСПОЛНИТЕЛЯ. У полосы свой адрес, и поиск
        # закрывающей сделки по кошельку исполнителя не находил её вовсе.
        найдено = найти_закрывающую(кошелёк_позиции(pos), pos.get("mint"),
                                     читатель_tx=читатель_tx or self.tx_читатель,
                                     подписи_фн=self.подписи_читатель,
                                     счета_фн=getattr(self, "счета_читатель", None))
        наши = set(pos.get("last_sell_signatures") or [])
        if pos.get("jup_signature"):
            наши.add(pos["jup_signature"])
        подпись = найдено.get("signature")
        если_наша = bool(подпись and подпись in наши)
        через = ("сторож" if если_наша else "авто-ордер Bloom")
        основа = pos.get("ts_accepted") or pos.get("ts_intent")
        секунды = None
        if найдено.get("block_time") and основа:
            секунды = float(найдено["block_time"]) - float(основа)
        elif основа:
            секунды = now - float(основа)
        sol = (найдено.get("outcome") or {}).get("sol_delta")
        # СЛОВО "ПРОДАНА" -- ТОЛЬКО ПРИ ПОДТВЕРЖДЕНИИ ПО ЦЕПИ. 24.09 в
        # 14:11:16Z ушла строка "🔵 продажа продана · через авто-ордер Bloom ·
        # 68 с · SOL ?": покупка перед этим упала в цепи, токена не было
        # никогда, остаток был нулём с рождения позиции -- и ветка "ноль
        # дважды подряд" закрыла её как проданную. Закрывающей транзакции не
        # нашлось (подпись пустая), возврат неизвестен -- именно это и значило
        # "SOL ?". Подтверждением считаем: есть подпись закрывающей сделки И
        # возврат больше нуля.
        исход = найдено.get("outcome") or {}
        # Считаем по ТРЁМ признакам сразу, и каждый нужен:
        #   * транзакция села (meta.err == null) -- упавшая ничего не продала;
        #   * минт РЕАЛЬНО ушёл с нашего счёта (mint_delta_ui < 0);
        #   * вернулось больше комиссии (sol_delta_net > 0).
        # Последнее -- не придирка: sol_delta считается ВМЕСТЕ с комиссией, и
        # у сделки, вернувшей ровно ноль, он равен самой комиссии. Проверка
        # "sol_delta > 0" такую сделку назвала бы удачной продажей.
        чисто = исход.get("sol_delta_net")
        минт_дельта = исход.get("mint_delta_ui")
        почему_нет = ""
        if not подпись:
            почему_нет = "закрывающая транзакция не найдена"
        elif исход.get("known") is False:
            почему_нет = "транзакцию не разобрать"
        elif исход.get("ok") is False:
            почему_нет = f"закрывающая транзакция упала ({исход.get('error_code')})"
        elif not isinstance(минт_дельта, (int, float)) or минт_дельта >= 0:
            почему_нет = "минт с нашего счёта не уходил"
        elif not isinstance(чисто, (int, float)):
            почему_нет = "возврат по цепи не посчитан"
        elif чисто <= 0:
            почему_нет = f"возврат по цепи не больше комиссии (чисто {чисто})"
        подтверждена = not почему_нет
        запись = {"client_order_id": cid, "mint": pos.get("mint"),
                   "action": "позиция закрыта -- доклад", "via": через,
                   "signature": подпись, "seconds": (round(секунды, 1) if секунды else None),
                   "sol_delta": sol, "search": найдено,
                   "confirmed": подтверждена, "why_not": почему_нет}
        self.log(запись)
        self.state.update_position(cid, closed_via=через, closed_signature=подпись,
                                    closed_sol_delta=sol,
                                    closed_confirmed=подтверждена,
                                    closed_why_not=почему_нет,
                                    closed_sol_net=(найдено.get("outcome") or {}).get(
                                        "sol_delta_net"))
        if self.оповещатель is not None and NT is not None:
            # Уже доложенную нашу продажу второй строкой не повторяем.
            уже = (если_наша and pos.get("last_sell_reported") == подпись)
            if not уже:
                if подтверждена:
                    self.оповещатель.послать(NT.строка_продажи(
                        ok=True, код=None, через=через, секунды=секунды,
                        sol_вернулось=sol, подпись=подпись))
                else:
                    self.оповещатель.послать(NT.строка_продажи_не_подтверждена(
                        через=через, секунды=секунды, почему=почему_нет,
                        подпись=подпись))
            # ПОЛНЫЙ КРУГ -- отдельной строкой и ровно один раз на позицию.
            # Слово владельца: по живой покупке нужны все круги сразу, а не
            # по одному в разных строках. Числа берутся из позиции как есть;
            # чего не мерили -- печатается чертой, а не нулём.
            if not pos.get("circle_reported"):
                свежая = self.state.positions().get(cid) or pos
                self.оповещатель.послать(NT.строка_круга(
                    поз=свежая, секунды=секунды, sol_вернулось=sol,
                    подтверждена=подтверждена, через=через,
                    слот_продажи=(найдено.get("outcome") or {}).get("slot")))
                self.state.update_position(cid, circle_reported=True)
        return запись

    def доложить_прошлую_попытку(self, pos: dict, *, читатель_tx=None) -> dict:
        """Строка о ПРЕДЫДУЩЕЙ попытке продажи: успех или код ошибки.

        Вызывается раз на попытку: следующая попытка (или закрытие позиции)
        уже знает, чем кончилась прошлая. Отдельный флаг в записи позиции не
        даёт доложить дважды.
        """
        подписи = pos.get("last_sell_signatures") or []
        if not подписи or pos.get("last_sell_reported") == подписи[-1]:
            return {"skipped": True}
        cid = pos.get("client_order_id")
        подпись = подписи[-1]
        tx = None
        if читатель_tx is not None:
            tx = читатель_tx(подпись)
        # ВОЗВРАТ СЧИТАЕТСЯ ПО КОШЕЛЬКУ ПОЗИЦИИ. Здесь стоял кошелёк
        # исполнителя на всех позициях подряд, и у продаж полосы возврат
        # выходил ровно 0.000000 SOL: чужой кошелёк в транзакции не менялся.
        # Именно эти строки владелец 25.09 назвал "продажа продана +0.000000".
        итог = итог_продажи(tx, кошелёк_позиции(pos), pos.get("mint"))
        основа = pos.get("ts_accepted") or pos.get("ts_intent")
        секунды = None
        if основа and pos.get("ts_last_sell_attempt"):
            секунды = float(pos["ts_last_sell_attempt"]) - float(основа)
        # В данных вид адреса латиницей (ASCII), в строке для человека --
        # по-русски: это разные вещи, и смешивать их нельзя.
        вид = {"mint": "минт", "pool": "пул"}.get(pos.get("sell_address_kind"), "минт")
        через = f"сторож, {вид}"
        if self.оповещатель is not None and NT is not None:
            # СЛОВО "ПРОДАНА" ТОЛЬКО ПРИ РЕАЛЬНОМ ВОЗВРАТЕ. Транзакция может
            # сесть без ошибки и не продать ничего; строка с "+0.000000 SOL" и
            # словом "продана" -- это неправда о деньгах (владелец 25.09).
            чисто_ = итог.get("sol_delta_net")
            продана = bool(итог.get("ok")) and isinstance(чисто_, (int, float)) \
                and чисто_ > 0
            if продана:
                self.оповещатель.послать(NT.строка_продажи(
                    ok=True, код=None, через=через, секунды=секунды,
                    sol_вернулось=итог.get("sol_delta"), подпись=подпись))
            elif итог.get("ok") is False or итог.get("known") is False:
                # Транзакция упала или не разобрана -- это отказ с кодом.
                self.оповещатель.послать(NT.строка_продажи(
                    ok=False,
                    код=итог.get("error_code") or итог.get("why_not"),
                    через=через, секунды=секунды,
                    sol_вернулось=итог.get("sol_delta"), подпись=подпись))
            else:
                # Села, но денег не принесла: "продана" тут неправда, а кода
                # ошибки нет -- значит строка о НЕподтверждении.
                self.оповещатель.послать(NT.строка_продажи_не_подтверждена(
                    через=через, секунды=секунды,
                    почему=(f"возврат по цепи не больше комиссии "
                             f"(чисто {чисто_})"),
                    подпись=подпись))
        self.state.update_position(cid, last_sell_reported=подпись,
                                    last_sell_outcome=итог)
        self.log({"client_order_id": cid, "mint": pos.get("mint"),
                   "action": "итог прошлой попытки по цепи", "signature": подпись,
                   "outcome": итог})
        return итог

    def догнать_натив_покупки(self, pos: dict, *, читатель_tx=None) -> dict:
        """Дописать в позицию расход покупки ПО ЦЕПИ, если он не добрался.

        ЗАЧЕМ. Итог позиции считается один раз, при закрытии, и берёт расход из
        lane_buy_native_sol (нативная дельта кошелька на покупке). Догон этого
        поля живёт в детекторе, на пульсе и по три позиции за такт -- сделка,
        которая закрылась за 47 секунд, закрывается раньше догона, и тогда счёт
        видит расход по полям: чаевые плюс приоритет плюс тариф, без платы за
        СОЗДАНИЕ СЧЕТОВ. На живой сделке 26.09 это 0.0021 SOL спрятанного
        убытка при входе 0.01.

        ПОДПИСЬ СПРАШИВАЕТСЯ СЕВШАЯ, А НЕ ПРИНЯТАЯ. Вариантов на одном nonce до
        шести, садится один; берём первый, который узел отдал и по которому
        дельта считается. Не больше ПРЕДЕЛ_ДОГОНА_ПОДПИСЕЙ вызовов узла --
        это не горячий путь, но и не место для перебора.

        Ничего не продаёт и не закрывает: только дописывает два числа.
        """
        из_ = {"filled": False, "why_not": None, "signature": None}
        cid = pos.get("client_order_id")
        if not cid or pos.get("lane_buy_native_sol") is not None:
            из_["why_not"] = "расход покупки по цепи уже есть"
            return из_
        if OSW is None or читатель_tx is None:
            из_["why_not"] = ("модуль полосы не загружен" if OSW is None
                              else "читателя транзакций нет")
            return из_
        варианты = [pos.get("lane_landed_signature"), pos.get("lane_signature"),
                    pos.get("lane_signature_accepted_first")]
        варианты += list(pos.get("lane_pool_candidates") or [])
        варианты.append(pos.get("lane_signature_local"))
        варианты = list(dict.fromkeys([в for в in варианты if в]))[:ПРЕДЕЛ_ДОГОНА_ПОДПИСЕЙ]
        if not варианты:
            из_["why_not"] = "подписи покупки в записи нет"
            return из_
        кошелёк = кошелёк_позиции(pos)
        for подпись in варианты:
            try:
                tx = читатель_tx(подпись)
            except Exception as exc:  # noqa: BLE001
                из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:80]}"
                continue
            if not tx:
                из_["why_not"] = "узел не отдал покупку"
                continue
            натив = OSW.натив_покупки(tx, кошелёк)
            if not натив.get("ok"):
                из_["why_not"] = натив.get("why_not")
                continue
            self.state.update_position(
                cid, lane_buy_native_sol=натив["native_sol"],
                lane_buy_fee_sol=натив["fee_sol"],
                lane_buy_native_from="seller_before_close",
                lane_landed_signature=подпись)
            из_.update(filled=True, why_not=None, signature=подпись,
                        native_sol=натив["native_sol"], fee_sol=натив["fee_sol"])
            return из_
        return из_

    def handle(self, pos: dict, *, now: float | None = None,
                balance_reader=token_balance_raw, читатель_tx=None) -> dict:
        """Одна позиция за один круг. Возвращает, что сделано и почему."""
        now = now if now is not None else time.time()
        читатель_tx = читатель_tx if читатель_tx is not None else self.tx_читатель
        cid = pos.get("client_order_id")
        mint = pos.get("mint")
        итог = {"client_order_id": cid, "mint": mint, "state_before": pos.get("state")}

        # ПОЛОСА СВОЕЙ ОТПРАВКИ -- отдельной ветвью. У неё нет авто-ордера
        # Bloom, а весь остаток минта она продаёт только со своего отдельного
        # кошелька: путь Bloom ниже остаётся байт в байт таким, каким был.
        if pos.get("lane") == МЕТКА_ПОЛОСЫ:
            return self.обработать_полосу(pos, now=now, balance_reader=balance_reader,
                                           читатель_tx=читатель_tx)

        if not due_for_watch(pos, grace_s=self.grace_s, now=now):
            итог["action"] = "ждём срок таймерного ордера"
            return итог

        # Баланс по цепи ПЕРЕД любым действием.
        bal = balance_reader(EXECUTOR_WALLET, mint)
        if not bal.get("ok"):
            # МОЛЧАТЬ ЗДЕСЬ НЕЛЬЗЯ. Именно эта ветка 23.09 съела живой
            # стенд: позиция была открыта, круг проходил, кредиты
            # тратились, а в журнале не появилось ни строки -- потому что
            # "ничего не делаем" ничего и не писало. Пишем, но не каждый
            # круг: журнал раз в 15 с был бы шумом, который не читают.
            итог.update(action="остаток не прочитан -- ничего не делаем",
                         why_not=bal.get("why_not"), failures=bal.get("failures"))
            self.state.update_position(cid, balance_read_failed_at=now,
                                        balance_read_why_not=str(bal.get("why_not"))[:300])
            прошло = now - float(self._жалобы.get(cid) or 0.0)
            if прошло >= self.жалоба_каждые_s:
                self._жалобы[cid] = now
                self.log(итог)
            return итог
        итог["balance_raw"] = bal.get("raw")
        итог["balance_ui"] = bal.get("ui")

        # Итог ПРЕДЫДУЩЕЙ попытки -- по цепи и один раз на попытку. Стоит
        # здесь, а не после продажи: сразу после отправки транзакции ещё нет
        # в confirmed, и вопрос "получилось ли" честно закрывается только
        # следующим кругом.
        if pos.get("last_sell_signatures"):
            self.доложить_прошлую_попытку(pos, читатель_tx=читатель_tx)

        if int(bal.get("raw") or 0) <= 0:
            # Два нуля подряд перед закрытием: одиночный ноль бывает гонкой
            # с индексацией узла.
            серия = int(pos.get("zero_streak") or 0) + 1
            if серия >= 2:
                # ПОРЯДОК ВАЖЕН И ЭТО ДЕНЬГИ. Суточный счёт пишется РОВНО ОДИН
                # РАЗ -- в тот момент, когда позиция становится закрытой (I.1).
                # Пока доклад шёл ПОСЛЕ закрытия, возврат (closed_sol_net) в
                # записи появлялся позже, и счёт закрывал позицию без выручки:
                # итог не считался вовсе. Сначала доклад -- он же находит
                # закрывающую транзакцию и пишет возврат, -- и только потом
                # закрытие.
                # СТРОКА ОБЯЗАТЕЛЬНА НА ЛЮБОЕ ЗАКРЫТИЕ. Позиция KMNO 24.09
                # закрылась в 00:08:10 таймерным ордером Bloom -- и владелец
                # не получил ни строки: эта ветка молчала.
                доклад = self.доложить_закрытие(pos, now=now, читатель_tx=читатель_tx)
                self.state.update_position(cid, state=STATE_CLOSED, zero_streak=серия,
                                            ts_closed=now,
                                            closed_reason="остаток ноль дважды подряд")
                итог.update(action="позиция закрыта", zero_streak=серия)
                # Счётчик "продано" ставится ТОЛЬКО на подтверждённую продажу.
                # Раньше он ставился на любой нулевой остаток -- в том числе
                # на остаток, которого не было никогда (упавшая покупка), и
                # тогда ноль по цепи выдавался за успешный выход. При
                # неподтверждённом закрытии счётчик не трогается вовсе: это
                # не продажа, но и не неудачная попытка продать.
                if доклад.get("confirmed"):
                    self.state.note_sell_outcome(sold=True)
                итог.update(closed_confirmed=bool(доклад.get("confirmed")),
                             closed_why_not=доклад.get("why_not") or "")
            else:
                self.state.update_position(cid, zero_streak=серия)
                итог.update(action="ноль первый раз -- ещё не закрываю",
                             zero_streak=серия)
            return итог

        self.state.update_position(cid, zero_streak=0)

        if int(bal.get("raw") or 0) < self.dust_raw:
            self.state.update_position(
                cid, state=STATE_CLOSED,
                closed_reason=(f"крошка: остаток {bal.get('raw')} сырых единиц меньше "
                                f"порога {self.dust_raw}, продавать нечего"))
            итог.update(action="закрыта как крошка")
            return итог

        убит, почему = kill_sell_active(self.state)
        if убит:
            итог.update(action="продажа запрещена рубильником продаж", why_not=почему)
            if self.оповещатель is not None and NT is not None:
                self.оповещатель.послать(NT.строка_тревоги("рубильник продаж", почему))
            self.log(итог)
            return итог

        по_неудачам = сдаться_по_неудачам(pos, предел=self.предел_неудач)

        # Второй путь выхода: Jupiter Ultra. Включается ПОСЛЕ того, как путь
        # Bloom исчерпан -- маршрут там выбирает Bloom, и на токене п. 1 он
        # трижды подряд выбрал пул без ликвидности. Пол по выходу -- правило
        # 4 владельца, проверка в bloom_jupiter_sell: подпись только когда
        # минимум выхода не ниже 70 % котировки, а котировка ниже 30 % от
        # входа означает UNSOLD, а не продажу за бесценок.
        if по_неудачам and self.jupiter_включён and not pos.get("jup_attempts"):
            r = self.продать_через_jupiter(pos, bal=bal, now=now)
            if r.get("ok"):
                # Вторая запись в журнал не нужна: продать_через_jupiter уже
                # записал эту попытку. Строк на одно событие должно быть
                # ровно столько, сколько событий.
                итог.update(action="продажа через Jupiter отправлена", jupiter=r)
                return итог
            итог["jupiter"] = r
            # Не получилось -- идём в UNSOLD ниже, причина уже в журнале.

        if по_неудачам or give_up(pos, give_up_after_s=self.give_up_after_s, now=now):
            причина_сдачи = (f"{pos.get('sell_attempts')} неудачных попыток подряд"
                              if по_неудачам else
                              f"не продано за {self.give_up_after_s:.0f} с")
            if pos.get("state") != "unsold":
                self.state.update_position(cid, state="unsold",
                                            unsold_since=now,
                                            unsold_reason=причина_сдачи)
                self.state.note_sell_outcome(sold=False)
                текст = (f"Bloom: позиция НЕ ПРОДАНА -- {причина_сдачи}\n"
                          f"кошелёк {EXECUTOR_WALLET}\nминт {mint}\n"
                          f"остаток {bal.get('ui')} ({bal.get('raw')} сырых)\n"
                          f"попыток {pos.get('sell_attempts', 0)}, "
                          f"чем пробовали: {pos.get('sell_address_kinds') or '-'}\n"
                          f"продать руками через Phantom/Jupiter")
                if self.оповещатель is not None and NT is not None:
                    self.оповещатель.послать(NT.строка_тревоги(
                        "UNSOLD", f"{причина_сдачи}, минт {mint}, "
                                   f"остаток {bal.get('ui')}"))
                self.log({**итог, "action": "UNSOLD, доклад владельцу",
                           "why_not": причина_сдачи,
                           "telegram": telegram(текст)})
            итог["action"] = "UNSOLD -- ждём владельца"
            return итог

        последняя = pos.get("ts_last_sell_attempt")
        if последняя and (now - float(последняя)) < self.retry_every_s:
            итог["action"] = (f"пауза между попытками: прошло "
                                 f"{now - float(последняя):.0f} с из "
                                 f"{self.retry_every_s:.0f}")
            return итог

        # Продажа. Проскальзывание НЕ поднимается.
        попытка = int(pos.get("sell_attempts") or 0) + 1
        адрес, вид = адрес_продажи(pos, попытка=попытка)
        body = build_sell_body(address=адрес, percent=100, slippage_pct=self.slippage,
                               priority_fee=self.priority_fee,
                               processor_tip=self.processor_tip)
        res = self.api.swap(body, client_order_id=f"{cid}:sell{попытка}",
                             why=f"сторож, попытка {попытка} по {вид}")
        виды = [v for v in (pos.get("sell_address_kinds") or "").split(",") if v]
        виды.append(вид)
        поля = {"state": "selling", "sell_attempts": попытка,
                 "ts_last_sell_attempt": now,
                 "sell_address": адрес, "sell_address_kind": вид,
                 "sell_address_kinds": ",".join(виды)}
        if not pos.get("ts_first_sell_attempt"):
            поля["ts_first_sell_attempt"] = now
        if res.get("order_id"):
            поля["last_sell_order_id"] = res["order_id"]
        if res.get("signatures"):
            поля["last_sell_signatures"] = res["signatures"]
        if not res.get("ok"):
            поля["last_sell_error"] = res.get("error_code") or res.get("why_not")
        self.state.update_position(cid, **поля)
        итог.update(action=("продажа отправлена" if res.get("ok")
                               else "продажа не принята"),
                     attempt=попытка, sell_address=адрес, sell_address_kind=вид,
                     mode="dry-run" if self.api.dry_run else "live",
                     ответ={k: res.get(k) for k in
                             ("ok", "код", "код_ошибки", "order_id", "signatures",
                              "rate_limited", "retry_after_s", "dry_run")})
        self.log(итог)
        return итог

    def обработать_полосу(self, pos: dict, *, now: float,
                           balance_reader=token_balance_raw,
                           читатель_tx=None) -> dict:
        """Позиция ПОЛОСЫ своей отправки. Три отличия от пути Bloom, и все про деньги:

        1) срок -- от нашей отправки и без запаса: авто-ордера Bloom у полосы
           нет, ждать его нечего;
        2) продаётся количество, купленное полосой (lane_bought_raw). Если
           количество так и не добралось за срок ожидания -- продаётся ВЕСЬ
           остаток минта на кошельке полосы (разрешение владельца 25.09:
           кошелёк отдельный, чужих токенов там нет). Это уже не считаемая
           пара: сколько купили -- неизвестно, поэтому итог помечается
           несчитаемым, но токен не остаётся висеть в UNSOLD. Рамка одна и
           жёсткая -- остаток целиком продаётся ТОЛЬКО с отдельного кошелька
           полосы (остаток_можно_продать_целиком);
        3) путь один -- Jupiter Swap V2 с запасным Ultra и полом 70 %; неудача
           ведёт к UNSOLD со СВОИМ счётчиком полосы, который торговлю Bloom не
           останавливает.

        Читается и подписывается всё это КОШЕЛЬКОМ ПОЛОСЫ (кошелёк_позиции):
        остаток на чужом адресе -- ноль, а подпись чужим ключом -- отвергнутая
        сетью транзакция с потраченным приоритетом.

        Закрывается позиция полосы подтверждённой СВОЕЙ продажей, а не нулевым
        остатком минта: нулю можно верить только когда мы точно знаем, что
        никто больше этот минт с этого адреса не продавал.
        """
        читатель_tx = читатель_tx if читатель_tx is not None else self.tx_читатель
        cid = pos.get("client_order_id")
        mint = pos.get("mint")
        итог = {"client_order_id": cid, "mint": mint, "lane": МЕТКА_ПОЛОСЫ,
                 "state_before": pos.get("state"),
                 "lane_bought_raw": pos.get("lane_bought_raw")}
        if not срок_полосы(pos, now=now):
            итог["action"] = "ждём срок продажи полосы"
            return итог

        # ИТОГ ПРОШЛОЙ ПОПЫТКИ -- по цепи. Он же и закрывает позицию: своя
        # продажа села -- позиция закрыта своим результатом.
        исход = pos.get("last_sell_outcome") or {}
        if pos.get("last_sell_signatures"):
            свежий = self.доложить_прошлую_попытку(pos, читатель_tx=читатель_tx)
            if not свежий.get("skipped"):
                исход = свежий
        if исход.get("known") and исход.get("ok"):
            # РАСХОД ПОКУПКИ ПО ЦЕПИ -- ДО ЗАКРЫТИЯ. Суточный счёт пишется РОВНО
            # ОДИН РАЗ, при закрытии (правка I.1), и берёт расход из нативной
            # дельты покупки. Если дельта к этому моменту не добралась, итог
            # считается по полям -- а поля не видят платы за создание счетов.
            # Живой замер 26.09: сделка 11:39:10Z закрылась через 47 с, дельта не
            # успела, и запись показала -0.001494874 против -0.003597154 по цепи;
            # у сделки 11:45:47Z дельта успела, и запись совпала с цепью до
            # лампорта. Один вызов узла здесь дешевле спрятанного убытка.
            self.догнать_натив_покупки(pos, читатель_tx=читатель_tx)
            pos = (self.state.positions().get(cid) or pos) if cid else pos
            self.state.update_position(
                cid, state=STATE_CLOSED, ts_closed=now,
                closed_reason="продажа полосы подтверждена по цепи",
                closed_sol_net=исход.get("sol_delta_net"))
            self.state.note_sell_outcome(sold=True, lane=МЕТКА_ПОЛОСЫ)
            итог.update(action="позиция полосы закрыта подтверждённой продажей",
                         outcome=исход)
            self.log(итог)
            return итог

        убит, почему = kill_sell_active(self.state)
        if убит:
            итог.update(action="продажа запрещена рубильником продаж", why_not=почему)
            if self.оповещатель is not None and NT is not None:
                self.оповещатель.послать(NT.строка_тревоги("рубильник продаж", почему))
            self.log(итог)
            return итог

        # КОЛИЧЕСТВО. Обычный путь -- продать РОВНО купленное полосой. Пока
        # не истёк срок ожидания -- ждём догон с пульса.
        куплено = pos.get("lane_bought_raw")
        по_остатку = False
        if not isinstance(куплено, int) or куплено <= 0:
            основа = (pos.get("ts_sent") or pos.get("ts_accepted")
                       or pos.get("ts_intent"))
            ждём = (now - float(основа)) if основа else None
            if ждём is not None and ждём < self.ждать_количество_s:
                итог.update(action="ждём количество, купленное полосой",
                             waited_s=round(ждём, 1),
                             why_not=pos.get("lane_bought_why_not"))
                return итог
            # ПРОДАЖА ПО ОСТАТКУ (разрешение владельца 25.09). У полосы свой
            # кошелёк, покупок Bloom и чужих токенов там нет, поэтому остаток
            # минта на нём -- наша покупка целиком. Позиция без количества
            # после срока ожидания -- не UNSOLD, а продажа всего остатка.
            можно, почему = остаток_можно_продать_целиком(pos)
            if not можно:
                # Единственный случай, когда позиция без количества остаётся
                # UNSOLD: адрес не наш отдельный. Там могут лежать покупки
                # Bloom, и продажа всего остатка продала бы их.
                self._пометить_несчитаемым(
                    pos, "количество, купленное полосой, так и не добралось")
                return self._полоса_unsold(
                    pos, now=now, итог=итог,
                    причина=("количество полосы неизвестно, а весь остаток "
                              f"продавать нельзя: {почему}"))
            по_остатку = True
            итог["lane_sell_whole_remainder"] = True
            итог["lane_amount_source"] = "остаток минта по цепи"
            # ИТОГ НЕСЧИТАЕМ (уточнение владельца 25.09). Продать по остатку
            # можно, а сойтись по деньгам нечем: сколько купили -- неизвестно.
            # Это не утечка денег и не повод останавливать полосу. Замер
            # скорости у пары остаётся годным.
            self._пометить_несчитаемым(
                pos, "количество, купленное полосой, так и не добралось -- "
                      "продажа по остатку кошелька полосы")

        bal = balance_reader(кошелёк_позиции(pos), mint)
        if not bal.get("ok"):
            итог.update(action="остаток не прочитан -- ничего не делаем",
                         why_not=bal.get("why_not"))
            self.state.update_position(
                cid, balance_read_failed_at=now,
                balance_read_why_not=str(bal.get("why_not"))[:300])
            прошло = now - float(self._жалобы.get(cid) or 0.0)
            if прошло >= self.жалоба_каждые_s:
                self._жалобы[cid] = now
                self.log(итог)
            return итог
        остаток = int(bal.get("raw") or 0)
        итог["balance_raw"] = остаток
        количество = остаток if по_остатку else min(куплено, остаток)
        итог["lane_amount_raw"] = количество
        if количество < self.dust_raw:
            # Токена меньше, чем порог крошки: либо покупка не села, либо
            # остаток уже ушёл прошлой попыткой. И то и другое -- не продажа
            # полосы и не её UNSOLD.
            self.state.update_position(
                cid, state=STATE_CLOSED, ts_closed=now,
                closed_reason=((f"продавать нечего: остаток минта {остаток} "
                                 f"меньше порога крошки {self.dust_raw}")
                                if по_остатку else
                                (f"продавать нечего: купленное полосой {куплено}, "
                                 f"остаток минта {остаток}, порог крошки "
                                 f"{self.dust_raw}")))
            # КУПИЛИ, А ПРОДАВАТЬ НЕЧЕГО -- это расхождение учёта, а не
            # нулевой итог: токен ушёл не нашей продажей. Пара помечается
            # несчитаемой, иначе в отчёте она выглядела бы сделкой в ноль.
            if isinstance(куплено, int) and куплено > 0:
                self._пометить_несчитаемым(
                    pos, f"купленное {куплено}, а остаток {остаток}: "
                          "токен ушёл не нашей продажей")
            итог["action"] = "позиция полосы закрыта -- продавать нечего"
            self.log(итог)
            return итог

        попыток = int(pos.get("jup_attempts") or 0)
        # ПРЕДЕЛ ПОПЫТОК У ПОЛОСЫ СНЯТ (решение владельца 25.09 вечера):
        # "продавать по таймеру при любой котировке, без остановки и без
        # запроса владельцу". Пока на кошельке полосы лежит её токен, сторож
        # продолжает пытаться -- с той же паузой между попытками. Жалоба в
        # журнал раз в 10 попыток, чтобы бесконечная серия была видна словами,
        # а не только числом в позиции.
        if попыток and попыток % 10 == 0:
            итог["attempts_note"] = (f"{попыток} попыток продать позицию полосы: "
                                      "предел попыток у полосы снят, продолжаем")
        последняя = pos.get("ts_jup_attempt")
        if попыток and последняя and (now - float(последняя)) < self.retry_every_s:
            итог["action"] = (f"пауза между попытками полосы: прошло "
                               f"{now - float(последняя):.0f} с из "
                               f"{self.retry_every_s:.0f}")
            return итог
        if JUP is None or not self.jupiter_включён:
            итог.update(action="полосе продавать нечем: путь Jupiter выключен",
                         why_not=("BLOOM_SELL_VIA_JUPITER не равен 1"
                                   if JUP is not None
                                   else "модуль продажи через Jupiter не загружен"))
            self.log(итог)
            return итог

        r = self.продать_через_jupiter(pos, bal=bal, now=now,
                                       количество_raw=количество)
        итог["jupiter"] = r
        if r.get("ok"):
            self.state.update_position(cid, state="selling",
                                        ts_last_sell_attempt=now,
                                        sell_address_kind="jupiter")
            итог["action"] = "продажа полосы отправлена"
            return итог
        итог["action"] = "Jupiter полосе не продал"
        # UNSOLD по числу попыток у полосы больше НЕ ставится: позиция остаётся
        # в работе и продаётся следующим кругом. Причина отказа записана в
        # позицию (jup_why_not) и в журнал этой же попыткой.
        return итог

    def _пометить_несчитаемым(self, pos: dict, причина: str) -> dict:
        """Итог пары не сходится -- пометить и работать дальше.

        Правило владельца от 25.09: расхождение учёта на одной паре не
        рубильник; рубильник только при реальной утечке денег. Пометку ставит
        сам модуль полосы -- одно написание на весь репозиторий.
        """
        if OSW is None:
            return {"ok": False, "why_not": "модуль полосы не загружен"}
        р = OSW.отметить_итог_несчитаемым(self.state,
                                           pos.get("client_order_id"), причина)
        self.log({"client_order_id": pos.get("client_order_id"),
                   "mint": pos.get("mint"), "lane": pos.get("lane"),
                   "action": "итог пары несчитаем", "why_not": причина,
                   "marked": bool(р.get("ok"))})
        return р

    def _полоса_unsold(self, pos: dict, *, now: float, итог: dict, причина: str,
                        остаток: int | None = None) -> dict:
        """UNSOLD полосы: доклад владельцу и СВОЙ счётчик.

        Счётчик именно свой: три непроданных позиции полосы по 0.01 SOL не
        имеют права остановить торговлю Bloom на 0.2 SOL.
        """
        cid = pos.get("client_order_id")
        if pos.get("state") != "unsold":
            self.state.update_position(cid, state="unsold", unsold_since=now,
                                        unsold_reason=причина)
            self.state.note_sell_outcome(sold=False, lane=МЕТКА_ПОЛОСЫ)
            if self.оповещатель is not None and NT is not None:
                self.оповещатель.послать(NT.строка_тревоги(
                    "UNSOLD полосы",
                    f"{причина}; минт {pos.get('mint')}, купленное "
                    f"{pos.get('lane_bought_raw')}, остаток {остаток}"))
            self.log({**итог, "action": "UNSOLD полосы, доклад владельцу",
                       "why_not": причина})
        итог.update(action="UNSOLD полосы -- ждём владельца", why_not=причина)
        return итог


    def продать_через_jupiter(self, pos: dict, *, bal: dict, now: float,
                               количество_raw: int | None = None) -> dict:
        """Продажа через Ultra с полом по выходу. Одна попытка на позицию.

        Одна -- намеренно: если Ultra отказала или котировка ниже границы, то
        второй запрос через 45 с ответит тем же, а позиция должна дойти до
        UNSOLD и доклада, а не крутиться в цикле.
        """
        cid = pos.get("client_order_id")
        if JUP is None:
            return {"ok": False, "why_not": "модуль продажи через Jupiter не загружен"}
        остаток = int(bal.get("raw") or 0)
        # КОЛИЧЕСТВО. По умолчанию весь остаток минта -- так работает выход
        # позиции Bloom. Полоса передаёт своё количество явно и никогда не
        # продаёт больше. Потолок остатка стоит и здесь: продать больше, чем
        # есть, значит потратить подпись и приоритет на транзакцию, которую
        # сеть отвергнет.
        сколько = остаток if количество_raw is None else min(int(количество_raw), остаток)
        # ПОЛ ПО КОТИРОВКЕ. У Bloom правило 30 % от входа остаётся, у полосы
        # его нет вовсе (решение владельца 25.09 вечера): её позиция продаётся
        # по таймеру при любой котировке. Пол 70 % от котировки (минимум выхода
        # в подписанных байтах) не отменяется НИКОМУ -- он защищает не от
        # дешёвой котировки, а от подмены минимума на цепи.
        любая = продавать_при_любой_котировке(pos)
        r = JUP.продать(mint=pos.get("mint"), amount_raw=сколько,
                         taker=кошелёк_позиции(pos), вход_sol=pos.get("sol_in"),
                         живьём=self.live, секрет=ключ_позиции(pos),
                         мин_доля_от_входа=(0.0 if любая else None))
        поля = {"jup_amount_raw": сколько,
                 "jup_any_quote": любая,
                 "jup_attempts": int(pos.get("jup_attempts") or 0) + 1,
                 "ts_jup_attempt": now,
                 "jup_floor": (r.get("floor") or {}).get("checks"),
                 # Каким путём шли и каким вышло -- в позицию. Без этого по
                 # журналу не отличить продажу через Swap V2 от продажи через
                 # Ultra: тексты отказов у них одинаковые.
                 "jup_api": r.get("api"),
                 "jup_api_used": r.get("api_used"),
                 "jup_api_tried": r.get("api_tried"),
                 "jup_why_not": r.get("why_not")}
        if r.get("signature"):
            поля["jup_signature"] = r["signature"]
            поля["last_sell_signatures"] = [r["signature"]]
            поля["sell_address_kind"] = "jupiter"
        self.state.update_position(cid, **поля)
        self.log({"client_order_id": cid, "mint": pos.get("mint"),
                   "action": ("продажа через Jupiter отправлена" if r.get("ok")
                               else "Jupiter не продал"),
                   "why_not": r.get("why_not"), "jupiter": r})
        if self.оповещатель is not None and NT is not None:
            основа = pos.get("ts_accepted") or pos.get("ts_intent")
            секунды = (now - float(основа)) if основа else None
            порог = ((r.get("floor") or {}).get("checks") or {})
            вышло = порог.get("out_amount")
            self.оповещатель.послать(NT.строка_продажи(
                ok=bool(r.get("ok")), код=r.get("why_not"),
                через=f"Jupiter {r.get('api_used') or r.get('api') or '?'}",
                секунды=секунды,
                sol_вернулось=(float(вышло) / 1e9 if вышло else None),
                подпись=r.get("signature")))
        return r

    def _план_путей_для_признака(self) -> dict:
        """План путей Jupiter для признака жизни. Ни сети, ни решений."""
        if JUP is None or not hasattr(JUP, "план_путей"):
            return {"api_plan": None, "api_why": "модуль продажи не загружен"}
        try:
            п = JUP.план_путей()
            return {"api_plan": п.get("plan"), "api_why": п.get("why")}
        except Exception as exc:  # noqa: BLE001
            return {"api_plan": None, "api_why": f"{type(exc).__name__}"}

    def heartbeat(self, итог: dict) -> None:
        """Признак жизни на диск каждый круг.

        WatchdogSec у systemd требует sd_notify, а его в этой службе нет и
        ставить зависимость ради одного пинга не стоит. Поэтому живучесть
        проверяется извне по свежести этого файла -- и проверка честная:
        файл обновляется только при реально пройденном круге.
        """
        from bloom_exec_state import atomic_write_json  # noqa: PLC0415
        # Оба рубильника: отдельно "читается ли путь" и отдельно "включён".
        # Нечитаемый рубильник продаж опаснее нечитаемого рубильника
        # покупок: он оставляет открытую позицию без выхода, и при этом
        # снаружи выглядит как тишина.
        куп_доступен, куп_поч = self.state.kill_readable()
        куп_включён, _ = self.state.kill_active()
        прод_доступен, прод_поч = self.state.kill_readable(kill_sell_file())
        прод_включён, _ = kill_sell_active(self.state)
        from bloom_exec_state import (  # noqa: PLC0415
            SCHEMA_VERSION, SCHEMA_VERSION_KEY)
        atomic_write_json(self.state.base / "seller_heartbeat.json", {
            SCHEMA_VERSION_KEY: SCHEMA_VERSION,
            "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "updated_ts": time.time(),
            "mode": "live" if self.live else "dry-run",
            "positions_in_cycle": итог.get("positions"),
            # Что именно сделано с каждой позицией в ПОСЛЕДНЕМ круге.
            # Без этого "журнал пуст" снаружи не отличить от "сторож не
            # видел позиции" и от "остаток не читается": ветки ожидания в
            # журнал не пишут, и так уже потерялся весь п. 1 стенда.
            "last_cycle": [{"client_order_id": r.get("client_order_id"),
                            "mint": r.get("mint"),
                            "action": r.get("action"),
                            "why_not": r.get("why_not"),
                            "balance_raw": r.get("balance_raw"),
                            "attempt": r.get("attempt")}
                           for r in (итог.get("rows") or [])],
            "max_attempts": self.предел_неудач,
            "jupiter": {"enabled": self.jupiter_включён,
                         "key": (JUP.ключ_есть()[1] or "ключ есть")
                                 if JUP is not None else "модуль не загружен",
                         "key_matches_wallet": self.jupiter_ключ.get("ok"),
                         "key_pubkey": self.jupiter_ключ.get("pubkey"),
                         "key_why_not": self.jupiter_ключ.get("why_not"),
                         "floor_pct": (JUP.ПОЛ_ПРОЦЕНТОВ if JUP is not None else None),
                         "min_quote_share_pct": (JUP.МИН_ДОЛЯ_ОТ_ВХОДА
                                                  if JUP is not None else None),
                         # КАКИМ ПУТЁМ ПОЙДЁТ ПРОДАЖА -- видно СРАЗУ, без
                         # ожидания сделки. Раньше признак жизни об этом
                         # молчал, и "сторож продаёт через V2" нельзя было ни
                         # подтвердить, ни опровергнуть: ключ мог быть стёрт
                         # деплоем, а путь молча вернуться на Ultra.
                         **self._план_путей_для_признака()},
            "slippage_pct": self.slippage,
            "grace_s": self.grace_s,
            "give_up_after_s": self.give_up_after_s,
            "kill_buy": {"path": str(self.state.kill_path),
                                   "readable": куп_доступен, "active": куп_включён,
                                   "note": куп_поч},
            "telegram": (self.оповещатель.статус() if self.оповещатель is not None
                          else {"enabled": False,
                                "off_reason": "модуль оповещений не загружен"}),
            "credits_logged": _УЧЁТ_ПИШЕТСЯ,
            "credits_log_error": _УЧЁТ_ПОЧЕМУ,
            "kill_sell": {"path": str(kill_sell_file()),
                                  "readable": прод_доступен, "active": прод_включён,
                                  "note": прод_поч},
        })

    def cycle(self, *, now: float | None = None, balance_reader=token_balance_raw,
               читатель_tx=None) -> dict:
        now = now if now is not None else time.time()
        открытые = self.state.open_positions()
        строки = [self.handle(p, now=now, balance_reader=balance_reader,
                               читатель_tx=читатель_tx)
                  for p in открытые]
        итог = {"positions": len(открытые), "mode": "live" if self.live else "dry-run",
                 "rows": строки}
        self.heartbeat(итог)
        return итог

    def check_only(self, *, balance_reader=token_balance_raw) -> dict:
        """Что я сделал бы, ничего не делая. Для приёмки перед live."""
        план = []
        for p in self.state.open_positions():
            bal = balance_reader(EXECUTOR_WALLET, p.get("mint"))
            полоса = p.get("lane") == МЕТКА_ПОЛОСЫ
            срок = (срок_полосы(p) if полоса
                     else due_for_watch(p, grace_s=self.grace_s))
            # У полосы к продаже РОВНО купленное ею, а не весь остаток минта.
            куплено = p.get("lane_bought_raw") if полоса else None
            сколько = (min(int(куплено), int(bal.get("raw") or 0))
                        if полоса and isinstance(куплено, int) and bal.get("ok")
                        else (int(bal.get("raw") or 0) if bal.get("ok") else None))
            план.append({
                "client_order_id": p.get("client_order_id"), "mint": p.get("mint"),
                "state": p.get("state"),
                "lane": p.get("lane"),
                "balance_raw": bal.get("raw") if bal.get("ok") else None,
                "balance_read": bool(bal.get("ok")),
                "lane_bought_raw": куплено,
                "amount_raw": сколько,
                "due": срок,
                "would_sell": bool(сколько is not None and сколько >= self.dust_raw
                                   and срок
                                   and (not полоса
                                        or (isinstance(куплено, int) and куплено > 0))),
                "with_what": ("Jupiter Swap V2 (запасной Ultra), пол 70 %, "
                               "количество -- купленное полосой" if полоса
                               else f"/swap Sell 100 % slippage {self.slippage} "
                                     "auto_orders []"),
            })
        return {"mode": "live" if self.live else "dry-run",
                 "kill_buy": self.state.kill_active(),
                 "kill_sell": kill_sell_active(self.state),
                 "plan": план}


def self_test() -> None:
    import tempfile  # noqa: PLC0415
    # СТРАЖ САМОПРОВЕРОК -- ПЕРВОЙ СТРОКОЙ. 25.09 самопроверки, запущенные на
    # хосте с боевым окружением, отправили строки с заглушками в БОЕВОЙ чат
    # владельца. Теперь тест физически не может ни написать в боевой путь, ни
    # выйти в сеть: страж падает исключением, а не предупреждает.
    import selftest_guard as _SG  # noqa: PLC0415

    _охрана = _SG.включить()
    # СЕТИ В САМОПРОВЕРКЕ НЕТ ВООБЩЕ. Модульный rpc_call подменяется на честный
    # отказ: часть проверок подменяет его сама (их подмена действует поверх), а
    # остальные больше не уходят в публичный RPC. 25.09 страж поймал именно
    # это: доложить_закрытие и доложить_прошлую_попытку тихо ходили в сеть за
    # подписями РЕАЛЬНОГО кошелька, и часть проверок опиралась на живые данные.
    _глоб_сп = sys.modules[__name__].__dict__
    _старый_rpc_сп = _глоб_сп["rpc_call"]
    _глоб_сп["rpc_call"] = lambda метод, параметры, **кв: {
        "ok": False, "why_not": "сети в самопроверке нет"}
    checks = []

    def chk(n, ok, got=""):
        checks.append((n, bool(ok), got))

    src = Path(__file__).read_text(encoding="utf-8")
    тело = src.split("def self_test")[0]
    chk("сторож сам POST в Bloom не делает -- только через клиент",
        "session.post" not in тело and "requests.post(f\"https://api.telegram" in тело)
    chk("проскальзывание нигде не повышается",
        "slippage * " not in тело and "slippage +" not in тело)

    база = Path(tempfile.mkdtemp())
    st = ExecState(base=база / "s", kill=база / "KILL")
    os.environ["BLOOM_KILL_SELL_FILE"] = str(база / "KILL_SELL")

    class ЗапрещённаяСеть:
        def post(self, *a, **k):
            raise AssertionError("в самопроверке сети быть не должно")

        def get(self, *a, **k):
            raise AssertionError("в самопроверке сети быть не должно")

    api = BloomApi("КЛЮЧ", dry_run=True, state=st, session=ЗапрещённаяСеть())
    s = Seller(state=st, live=False, api=api)

    # --- срок ожидания
    поз = {"client_order_id": "c1", "mint": "M1", "state": "bought",
            "ts_accepted": 1000.0, "sell_after_s": 28.8}
    chk("до срока таймерного ордера сторож не лезет",
        due_for_watch(поз, grace_s=15.0, now=1000 + 28.8 + 14) is False)
    chk("после срока плюс запас -- лезет",
        due_for_watch(поз, grace_s=15.0, now=1000 + 28.8 + 15.1) is True)
    chk("закрытую позицию не смотрит",
        due_for_watch({**поз, "state": STATE_CLOSED}, grace_s=15.0, now=1e12) is False)
    chk("нет времени принятия -- смотрит сразу (могли упасть до ответа)",
        due_for_watch({"client_order_id": "c", "mint": "M", "state": "intent"},
                       grace_s=15.0) is True)

    # --- сдача через 10 минут
    chk("без первой попытки не сдаётся", give_up(поз, give_up_after_s=600) is False)
    chk("через 10 минут после первой попытки сдаётся",
        give_up({**поз, "ts_first_sell_attempt": 1000.0}, give_up_after_s=600,
                 now=1601.0) is True)
    chk("через 9 минут ещё нет",
        give_up({**поз, "ts_first_sell_attempt": 1000.0}, give_up_after_s=600,
                 now=1500.0) is False)

    # --- рубильник продаж отдельный от рубильника покупок
    (база / "KILL").write_text("стоп покупок", encoding="utf-8")
    chk("рубильник ПОКУПОК включён", st.kill_active()[0] is True)
    chk("а продажи им НЕ запрещены", kill_sell_active()[0] is False)
    # Второй путь рубильника продаж -- в каталоге состояния: именно через
    # него работает /kill_sell из Telegram (в /etc служба писать не может).
    st.kill_sell_path.write_text("/kill_sell из Telegram", encoding="utf-8")
    тг_убит, тг_почему = kill_sell_active(st)
    chk("рубильник продаж из Telegram запрещает продажи",
        тг_убит is True and "Telegram" in тг_почему, (тг_убит, тг_почему))
    chk("и без состояния этот путь не виден (совместимость сохранена)",
        kill_sell_active()[0] is False)
    st.kill_sell_path.unlink()
    chk("снят -- продажи снова разрешены", kill_sell_active(st)[0] is False)
    (база / "KILL_SELL").write_text("стоп продаж", encoding="utf-8")
    убит, почему = kill_sell_active()
    chk("отдельный рубильник продаж работает", убит is True)
    chk("и причина видна", "стоп продаж" in почему, почему)
    (база / "KILL_SELL").unlink()
    (база / "KILL").unlink()

    # --- поведение по остатку
    st.write_intent(client_order_id="p1", mint="MINT1", source_sig="S1", source_slot=1,
                     sol_in=0.2, pool=None, program=None, taxed=None, tax_bps=None,
                     mode="dry-run", sell_after_s=28.8)
    st.update_position("p1", state="bought", ts_accepted=time.time() - 100)

    def читатель(raw):
        def f(wallet, mint):
            return {"ok": True, "raw": raw, "ui": raw / 1e6, "accounts": 1, "failures": []}
        return f

    r = s.handle(st.positions()["p1"], balance_reader=читатель(0))
    chk("первый ноль -- позиция не закрывается", r["action"].startswith("ноль первый"))
    r = s.handle(st.positions()["p1"], balance_reader=читатель(0))
    chk("второй ноль подряд -- закрывается", r["action"] == "позиция закрыта")
    chk("и в журнале она уже не открыта", st.open_positions() == [])
    chk("удачная продажа снимает серию непроданных",
        int(st.counters().get("unsold_streak", 0)) == 0)

    st.write_intent(client_order_id="p2", mint="MINT2", source_sig="S2", source_slot=2,
                     sol_in=0.2, pool=None, program=None, taxed=None, tax_bps=None,
                     mode="dry-run", sell_after_s=28.8)
    st.update_position("p2", state="bought", ts_accepted=time.time() - 100)
    r = s.handle(st.positions()["p2"], balance_reader=читатель(500))
    chk("крошка не продаётся, а закрывается", r["action"] == "закрыта как крошка")

    st.write_intent(client_order_id="p3", mint="MINT3", source_sig="S3", source_slot=3,
                     sol_in=0.2, pool=None, program=None, taxed=None, tax_bps=None,
                     mode="dry-run", sell_after_s=28.8)
    st.update_position("p3", state="bought", ts_accepted=time.time() - 100)
    r = s.handle(st.positions()["p3"], balance_reader=читатель(5_000_000))
    chk("настоящий остаток -- продажа отправлена", r["action"] == "продажа отправлена")
    chk("и это dry-run, без сети", r["mode"] == "dry-run")
    chk("попытка посчитана", st.positions()["p3"]["sell_attempts"] == 1)
    r = s.handle(st.positions()["p3"], balance_reader=читатель(5_000_000))
    chk("сразу вторая попытка не делается -- держим паузу 45 с",
        r["action"].startswith("пауза между попытками"), r["action"])

    # --- не читается остаток -- ничего не делаем
    def нечитаемый(wallet, mint):
        return {"ok": False, "why_not": "узел молчит"}

    r = s.handle(st.positions()["p3"], balance_reader=нечитаемый)
    chk("остаток не прочитан -- продажи нет",
        r["action"].startswith("остаток не прочитан"))
    журнал = (st.base / "seller.jsonl")
    строк_после = len(журнал.read_text(encoding="utf-8").splitlines()) if журнал.exists() else 0
    chk("и нечитаемый остаток ПОПАЛ в журнал -- молчать здесь нельзя",
        строк_после >= 1, строк_после)
    s.handle(st.positions()["p3"], balance_reader=нечитаемый)
    chk("но не каждый круг: жалоба раз в минуту, а не раз в 15 с",
        len(журнал.read_text(encoding="utf-8").splitlines()) == строк_после)
    chk("и причина отказа осталась в записи позиции",
        "узел молчит" in str(st.positions()["p3"].get("balance_read_why_not")))

    # --- закрытие таймерным ордером Bloom: строка ОБЯЗАТЕЛЬНА
    def tx_продажа_минта(минт, sol=0.00081):
        def читатель(подпись):
            бал = lambda raw, idx: {  # noqa: E731
                "accountIndex": idx, "mint": минт, "owner": EXECUTOR_WALLET,
                "programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                "uiTokenAmount": {"amount": str(raw), "decimals": 6,
                                   "uiAmount": raw / 1e6}}
            if подпись == "ЧУЖАЯ_ДРУГОЙ_МИНТ":
                return {"slot": 5, "meta": {"err": None, "fee": 5000,
                        "preBalances": [10 ** 9], "postBalances": [10 ** 9],
                        "preTokenBalances": [], "postTokenBalances": [],
                        "innerInstructions": []},
                        "transaction": {"message": {
                            "accountKeys": [{"pubkey": EXECUTOR_WALLET}],
                            "instructions": []}}}
            return {"slot": 9, "meta": {
                        "err": None, "fee": 5000,
                        "preBalances": [10 ** 9],
                        "postBalances": [10 ** 9 + int(sol * 1e9)],
                        "preTokenBalances": [бал(1_000_000, 1)],
                        "postTokenBalances": [бал(0, 1)],
                        "innerInstructions": []},
                    "transaction": {"message": {
                        "accountKeys": [{"pubkey": EXECUTOR_WALLET}],
                        "instructions": []}}}
        return читатель

    st.write_intent(client_order_id="pz", mint="MINTZ", source_sig="SZ", source_slot=11,
                     sol_in=0.001, pool=None, program=None, taxed=None, tax_bps=None,
                     mode="dry-run", sell_after_s=28.8)
    st.update_position("pz", state="bought", ts_accepted=time.time() - 60)
    посланное_з = []
    s.оповещатель = type("О", (), {
        "послать": lambda self_, текст: посланное_з.append(текст) or {"ok": True},
        "статус": lambda self_: {"enabled": True}})()
    подписи_кошелька = [{"signature": "ЧУЖАЯ_ДРУГОЙ_МИНТ", "blockTime": 1},
                         {"signature": "ПРОДАЖА_ТАЙМЕРА", "blockTime": None}]
    глоб_s = sys.modules[__name__].__dict__
    старый_rpc2 = глоб_s["rpc_call"]
    глоб_s["rpc_call"] = lambda метод, параметры, **kw: (
        {"ok": True, "result": подписи_кошелька} if метод == "getSignaturesForAddress"
        else {"ok": False, "why_not": "не нужно"})
    try:
        r = s.handle(st.positions()["pz"], balance_reader=читатель(0),
                      читатель_tx=tx_продажа_минта("MINTZ"))
        chk("первый ноль -- закрытия ещё нет", r["action"].startswith("ноль первый"))
        chk("и строки о закрытии тоже нет", посланное_з == [], посланное_з)
        r = s.handle(st.positions()["pz"], balance_reader=читатель(0),
                      читатель_tx=tx_продажа_минта("MINTZ"))
        chk("второй ноль -- позиция закрыта", r["action"] == "позиция закрыта", r)
        # Строк теперь две: о продаже и полный круг. Круг -- отдельной
        # строкой, потому что в одну он не читается с телефона.
        chk("и строка о закрытии УШЛА", len(посланное_з) == 2, посланное_з)
        chk("в строке сказано: через авто-ордер Bloom",
            "авто-ордер Bloom" in посланное_з[0], посланное_з)
        chk("полный круг ушёл второй строкой",
            посланное_з[1].startswith("🔁") and "полный круг" in посланное_з[1],
            посланное_з[1][:80])
        chk("в круге есть все четыре замера кругов поимённо",
            all(s in посланное_з[1] for s in ("Bloom", "новых соединений",
                                               "от решения", "от ответа Bloom")),
            посланное_з[1])
        chk("несделанный замер в круге -- черта, а не ноль",
            "Bloom -, новых соединений -" in посланное_з[1], посланное_з[1])
        # Круг -- РОВНО ОДИН на позицию. Сторож ходит по позициям кругами,
        # и второй доклад о том же закрытии превратил бы телефон в ленту.
        было_строк = len(посланное_з)
        s.доложить_закрытие(st.positions()["pz"], now=time.time(),
                             читатель_tx=tx_продажа_минта("MINTZ"))
        chk("второй раз круг по той же позиции не уходит",
            not any(х.startswith("🔁") for х in посланное_з[было_строк:]),
            посланное_з[было_строк:])
        chk("и вернувшийся SOL -- числом из цепи (сделка, без комиссии сети)",
            "+0.000815" in посланное_з[0], посланное_з)
        поз_ч = st.positions()["pz"]
        chk("а чистое движение баланса записано отдельно",
            abs((поз_ч.get("closed_sol_net") or 0) - 0.00081) < 1e-9,
            поз_ч.get("closed_sol_net"))
        поз_з = st.positions()["pz"]
        chk("чем закрыта -- записано в позицию",
            поз_з.get("closed_via") == "авто-ордер Bloom"
            and поз_з.get("closed_signature") == "ПРОДАЖА_ТАЙМЕРА", поз_з)

        # закрывающая подпись -- НАША: значит продал сторож, а не Bloom
        st.write_intent(client_order_id="pw", mint="MINTW", source_sig="SW",
                         source_slot=12, sol_in=0.001, pool=None, program=None,
                         taxed=None, tax_bps=None, mode="dry-run", sell_after_s=28.8)
        st.update_position("pw", state="selling", ts_accepted=time.time() - 60,
                            last_sell_signatures=["ПРОДАЖА_ТАЙМЕРА"])
        посланное_з.clear()
        st.update_position("pw", zero_streak=1)
        r = s.handle(st.positions()["pw"], balance_reader=читатель(0),
                      читатель_tx=tx_продажа_минта("MINTW"))
        chk("наша подпись -- в строке сторож, а не Bloom",
            посланное_з and "через сторож" in посланное_з[0], посланное_з)

        # транзакцию не нашли -- строка всё равно уходит, без выдуманных чисел
        st.write_intent(client_order_id="pv", mint="MINTV", source_sig="SV",
                         source_slot=13, sol_in=0.001, pool=None, program=None,
                         taxed=None, tax_bps=None, mode="dry-run", sell_after_s=28.8)
        st.update_position("pv", state="bought", ts_accepted=time.time() - 60,
                            zero_streak=1)
        посланное_з.clear()
        r = s.handle(st.positions()["pv"], balance_reader=читатель(0),
                      читатель_tx=lambda подпись: None)
        # Прежде здесь ждали строку "продана ... SOL ?" -- и именно она ушла
        # владельцу 24.09 в 14:11:16Z по позиции, которой не существовало.
        # Закрывающей транзакции нет, возврат неизвестен: слова "продана" в
        # такой строке быть не может.
        chk("продажа без закрывающей транзакции НЕ зовётся проданной",
            посланное_з and "не подтверждена" in посланное_з[0]
            and "продана" not in посланное_з[0], посланное_з)
        chk("и причина названа словами",
            посланное_з and "закрывающая транзакция не найдена" in посланное_з[0],
            посланное_з)
        chk("неподтверждённое закрытие не считается продажей в счётчике",
            r.get("closed_confirmed") is False, r)
        chk("и счётчик непроданных не сбит неподтверждённым закрытием",
            int(st.counters().get("unsold_streak", 0)) == 0, st.counters())

        # ПРОДАЖА ВМЕСТЕ С ЗАКРЫТИЕМ СЧЁТА, а история кошелька её не достаёт.
        # Это правка (б) по слову владельца 26.09: исчезнувшая строка
        # postTokenBalances -- остаток 0, позиция закрывается, выручка в счёт.
        # Занятый кошелёк уводит продажу за окно последних подписей, и позиция
        # прежде закрывалась БЕЗ возврата: выручка не попадала в суточный счёт.
        # Адрес токен-счёта в самопроверке НЕ выводится через solders: у
        # системного питона бегунка деплоя его нет, и прежняя версия этой
        # проверки роняла самопроверку до доставки (26.09 13:32Z). Адрес
        # подставляется, а вывод настоящего адреса проверяется отдельно и
        # только если solders в окружении есть.
        МИНТ_ЗС = "So11111111111111111111111111111111111111113"
        СЧЁТ_ЗС = "ТОКЕН_СЧЁТ_ПОЗИЦИИ_ЗС"
        try:
            import c2_swap_build as B_з  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            B_з = None
        if B_з is not None:
            chk("адрес токен-счёта выводится для обеих программ токена",
                len(счета_минта(EXECUTOR_WALLET, МИНТ_ЗС)) == 2, 
                счета_минта(EXECUTOR_WALLET, МИНТ_ЗС))
        else:
            chk("без solders адрес счёта не выводится и путь честно недоступен",
                счета_минта(EXECUTOR_WALLET, МИНТ_ЗС) == []
                and "нет solders" in (закрывающая_по_счёту(
                    EXECUTOR_WALLET, МИНТ_ЗС,
                    читатель_tx=lambda п: None).get("why_not") or ""), "")
        tx_зс = {"slot": 77, "meta": {
                     "err": None, "fee": 5000,
                     "preBalances": [10 ** 9, 2_039_280],
                     "postBalances": [10 ** 9 + 9_039_280, 0],
                     # Строки этого счёта в postTokenBalances НЕТ -- счёт закрыт
                     # той же транзакцией. И owner в pre тоже нет: узел его не
                     # обязан отдавать, а сторож обязан это переживать.
                     "preTokenBalances": [{"accountIndex": 1, "mint": МИНТ_ЗС,
                                            "uiTokenAmount": {"amount": "1000000",
                                                              "decimals": 6}}],
                     "postTokenBalances": [], "innerInstructions": []},
                 "transaction": {"message": {
                     "accountKeys": [{"pubkey": EXECUTOR_WALLET}, {"pubkey": СЧЁТ_ЗС}],
                     "instructions": []}}}
        chk("пропавшая строка postTokenBalances читается как НОЛЬ, а не как нет данных",
            дельта_по_счёту(tx_зс, СЧЁТ_ЗС) == -1_000_000, дельта_по_счёту(tx_зс, СЧЁТ_ЗС))
        chk("а по владельцу и минту дельты нет вовсе -- прежний путь слеп",
            итог_продажи(tx_зс, EXECUTOR_WALLET, МИНТ_ЗС).get("mint_delta_ui") is None,
            итог_продажи(tx_зс, EXECUTOR_WALLET, МИНТ_ЗС).get("mint_delta_ui"))
        st.write_intent(client_order_id="pзс", mint=МИНТ_ЗС, source_sig="SЗС",
                         source_slot=16, sol_in=0.01, pool=None, program=None,
                         taxed=None, tax_bps=None, mode="dry-run", sell_after_s=28.8)
        st.update_position("pзс", state="bought", ts_accepted=time.time() - 60,
                            zero_streak=1)
        посланное_з.clear()
        # История КОШЕЛЬКА отдаёт чужие подписи (продажа ушла за окно), история
        # СЧЁТА -- ту самую транзакцию.
        подписи_по_адресу = {СЧЁТ_ЗС: [{"signature": "ПРОДАЖА_И_ЗАКРЫТИЕ",
                                         "blockTime": 1790000000}]}
        было_подписи = s.подписи_читатель
        s.подписи_читатель = lambda адрес, лимит: (
            подписи_по_адресу.get(адрес) or [{"signature": "ЧУЖАЯ1"},
                                              {"signature": "ЧУЖАЯ2"}])
        s.счета_читатель = lambda кошелёк, минт: [СЧЁТ_ЗС]
        try:
            r_зс = s.handle(st.positions()["pзс"], balance_reader=читатель(0),
                             читатель_tx=lambda подпись: (
                                 tx_зс if подпись == "ПРОДАЖА_И_ЗАКРЫТИЕ" else None))
        finally:
            s.подписи_читатель = было_подписи
            s.счета_читатель = None
        поз_зс = st.positions()["pзс"]
        chk("продажа с закрытием счёта найдена по истории токен-счёта",
            поз_зс.get("closed_signature") == "ПРОДАЖА_И_ЗАКРЫТИЕ"
            and поз_зс.get("state") == STATE_CLOSED, поз_зс)
        chk("и ВЫРУЧКА попала в позицию: возврат по нативной дельте кошелька",
            abs((поз_зс.get("closed_sol_net") or 0) - 0.00903928) < 1e-8,
            поз_зс.get("closed_sol_net"))
        chk("и суточный счёт её учёл ровно один раз",
            поз_зс.get("pnl_counted") is True
            and isinstance(поз_зс.get("pnl_counted_sol"), (int, float)),
            {к: поз_зс.get(к) for к in ("pnl_counted", "pnl_counted_sol")})

        # Транзакция НАЙДЕНА, но возврат нулевой: это тоже не продажа.
        st.write_intent(client_order_id="pz", mint="MINTZ", source_sig="SZ",
                         source_slot=14, sol_in=0.001, pool=None, program=None,
                         taxed=None, tax_bps=None, mode="dry-run", sell_after_s=28.8)
        st.update_position("pz", state="bought", ts_accepted=time.time() - 60,
                            zero_streak=1)
        посланное_з.clear()
        r0 = s.handle(st.positions()["pz"], balance_reader=читатель(0),
                       читатель_tx=tx_продажа_минта("MINTZ", sol=0.0))
        снимок_z = list(посланное_з)
        chk("нулевой возврат продажей не считается",
            снимок_z and "не подтверждена" in снимок_z[0], снимок_z)
        chk("и причина -- про возврат, а не про подпись",
            снимок_z and "возврат по цепи" in снимок_z[0], снимок_z)

        # А подтверждённая продажа по-прежнему зовётся проданной.
        st.write_intent(client_order_id="py", mint="MINTY", source_sig="SY",
                         source_slot=15, sol_in=0.001, pool=None, program=None,
                         taxed=None, tax_bps=None, mode="dry-run", sell_after_s=28.8)
        st.update_position("py", state="bought", ts_accepted=time.time() - 60,
                            zero_streak=1)
        посланное_з.clear()
        r1 = s.handle(st.positions()["py"], balance_reader=читатель(0),
                       читатель_tx=tx_продажа_минта("MINTY", sol=0.05))
        chk("подтверждённая продажа так и названа",
            посланное_з and "продана" in посланное_з[0]
            and "не подтверждена" not in посланное_з[0], посланное_з)
        chk("и она сбрасывает счётчик непроданных",
            r1.get("closed_confirmed") is True, r1)
    finally:
        глоб_s["rpc_call"] = старый_rpc2
        s.оповещатель = None

    # --- итог прошлой попытки по цепи и строка о нём
    def tx_упавшая(подпись):
        return {"slot": 777, "meta": {
            "err": {"InstructionError": [4, "ProgramFailedToComplete"]},
            "fee": 5000, "preBalances": [10 ** 9], "postBalances": [10 ** 9 - 1_000_000],
            "preTokenBalances": [], "postTokenBalances": [], "innerInstructions": []},
            "transaction": {"message": {"accountKeys": [{"pubkey": EXECUTOR_WALLET}],
                                          "instructions": []}}}

    и = итог_продажи(tx_упавшая("X"), EXECUTOR_WALLET, "MINTX")
    chk("упавшая продажа: код ошибки из инструкции",
        и["ok"] is False and и["error_code"] == "ProgramFailedToComplete", и)
    chk("и потраченный SOL со знаком минус", и["sol_delta"] < 0, и)
    chk("узел не отдал транзакцию -- сказано, а не выдумано",
        итог_продажи(None, EXECUTOR_WALLET, "M")["known"] is False)

    st.write_intent(client_order_id="p5", mint="MINT5", source_sig="S5", source_slot=5,
                     sol_in=0.001, pool=None, program=None, taxed=None, tax_bps=None,
                     mode="dry-run", sell_after_s=28.8)
    st.update_position("p5", state="selling", ts_accepted=time.time() - 100,
                        sell_attempts=1, sell_address_kind="mint",
                        ts_last_sell_attempt=time.time() - 50,
                        last_sell_signatures=["ПОДПИСЬ_ПРОДАЖИ"])
    посланное = []
    s.оповещатель = type("О", (), {
        "послать": lambda self_, текст: посланное.append(текст) or {"ok": True},
        "статус": lambda self_: {"enabled": True}})()
    r = s.доложить_прошлую_попытку(st.positions()["p5"], читатель_tx=tx_упавшая)
    chk("итог прошлой попытки посчитан", r.get("error_code") == "ProgramFailedToComplete", r)
    chk("и строка о продаже ушла", посланное and "НЕ продана" in посланное[0], посланное)
    chk("и в ней есть, через что продавали", "сторож, минт" in посланное[0], посланное)
    было_строк = len(посланное)
    s.доложить_прошлую_попытку(st.positions()["p5"], читатель_tx=tx_упавшая)
    chk("дважды об одной попытке не докладываем", len(посланное) == было_строк,
        посланное)
    s.оповещатель = None

    # --- чем продаём: пул нашей покупки или минт
    прямая = {"mint": "MINT9", "our_pool": "POOL9", "our_pool_direct": True}
    chk("первая попытка по пулу, если покупка шла одним пулом токен/WSOL",
        адрес_продажи(прямая, попытка=1) == ("POOL9", "pool"))
    chk("вторая попытка -- по минту, пусть Bloom ищет маршрут сам",
        адрес_продажи(прямая, попытка=2) == ("MINT9", "mint"))
    chk("многохоповая покупка -- сразу по минту, пул не выдумывается",
        адрес_продажи({"mint": "MINT9", "our_pool": "POOL9",
                        "our_pool_direct": False}, попытка=1) == ("MINT9", "mint"))
    chk("пула нет -- по минту",
        адрес_продажи({"mint": "MINT9"}, попытка=1) == ("MINT9", "mint"))

    # --- две неудачи вместо десяти минут
    chk("без попыток не сдаёмся", сдаться_по_неудачам({}, предел=2) is False)
    chk("после одной попытки ещё нет",
        сдаться_по_неудачам({"sell_attempts": 1}, предел=2) is False)
    chk("после двух -- да", сдаться_по_неудачам({"sell_attempts": 2}, предел=2) is True)

    st.write_intent(client_order_id="p4", mint="MINT4", source_sig="S4", source_slot=4,
                     sol_in=0.2, pool=None, program=None, taxed=None, tax_bps=None,
                     mode="dry-run", sell_after_s=28.8)
    st.update_position("p4", state="bought", ts_accepted=time.time() - 100,
                        our_pool="POOL4", our_pool_direct=True)
    r = s.handle(st.positions()["p4"], balance_reader=читатель(5_000_000))
    chk("попытка 1 по пулу нашей покупки", r.get("sell_address_kind") == "pool", r)
    st.update_position("p4", ts_last_sell_attempt=time.time() - 100)
    r = s.handle(st.positions()["p4"], balance_reader=читатель(5_000_000))
    chk("попытка 2 по минту", r.get("sell_address_kind") == "mint", r)
    st.update_position("p4", ts_last_sell_attempt=time.time() - 100)
    r = s.handle(st.positions()["p4"], balance_reader=читатель(5_000_000))
    chk("третьей попытки нет: две неудачи -- UNSOLD",
        st.positions()["p4"]["state"] == "unsold", r)
    chk("и причина -- неудачи, а не десять минут",
        "неудачных попыток" in str(st.positions()["p4"].get("unsold_reason")),
        st.positions()["p4"].get("unsold_reason"))
    chk("чем пробовали -- записано",
        st.positions()["p4"].get("sell_address_kinds") == "pool,mint",
        st.positions()["p4"].get("sell_address_kinds"))

    # --- фильтр остатка: mint и programId вместе узел не принимает
    тело_ф = Path(__file__).read_text(encoding="utf-8").split("def self_test")[0]
    chk("оба фильтра сразу больше не отправляются",
        '"mint": mint, "programId"' not in тело_ф)

    вызовы = []

    def поддельный_rpc(метод, параметры, **kw):
        вызовы.append((метод, параметры))
        фильтр = параметры[1] if len(параметры) > 1 else {}
        if "mint" in фильтр:
            return {"ok": True, "result": {"value": [
                {"pubkey": "ACC1", "account": {"data": {"parsed": {"info": {
                    "mint": "MINTX",
                    "tokenAmount": {"amount": "92278326", "uiAmount": 92.278326}}}}}}]}}
        return {"ok": False, "why_not": "не должен вызываться"}

    глоб = sys.modules[__name__].__dict__
    старый_rpc = глоб["rpc_call"]
    глоб["rpc_call"] = поддельный_rpc
    try:
        bal = token_balance_raw("W", "MINTX")
        chk("остаток читается фильтром по минту, одним запросом",
            bal["ok"] and bal["raw"] == 92278326 and len(вызовы) == 1, (bal, вызовы))
        chk("и в ответе сказано, каким фильтром", bal.get("filter") == "mint")

        вызовы.clear()

        def минт_падает(метод, параметры, **kw):
            вызовы.append((метод, параметры))
            фильтр = параметры[1] if len(параметры) > 1 else {}
            if "mint" in фильтр:
                return {"ok": False, "why_not": "RPC error: invalid params"}
            return {"ok": True, "result": {"value": [
                {"pubkey": "ACC1", "account": {"data": {"parsed": {"info": {
                    "mint": "MINTX",
                    "tokenAmount": {"amount": "5", "uiAmount": 5.0}}}}}},
                {"pubkey": "ACC2", "account": {"data": {"parsed": {"info": {
                    "mint": "ЧУЖОЙ",
                    "tokenAmount": {"amount": "999", "uiAmount": 999.0}}}}}}]}}

        глоб["rpc_call"] = минт_падает
        bal2 = token_balance_raw("W", "MINTX")
        chk("минт не сработал -- запасной путь по программам токена",
            bal2["ok"] and bal2.get("filter") == "programId", bal2)
        chk("и чужой минт в сумму не попал", bal2["raw"] == 10, bal2)
        chk("и отказ по минту не потерян",
            any("mint" == (f.get("filter")) for f in bal2.get("failures") or []), bal2)

        глоб["rpc_call"] = lambda *a, **k: {"ok": False, "why_not": "узел молчит"}
        bal3 = token_balance_raw("W", "MINTX")
        chk("всё отказало -- честный отказ, а не нулевой остаток",
            bal3["ok"] is False and "не прочитан" in bal3["why_not"], bal3)
    finally:
        глоб["rpc_call"] = старый_rpc

    # --- путь через Jupiter: после двух неудач Bloom, до UNSOLD
    st.write_intent(client_order_id="pj", mint="MINTJ", source_sig="SJ", source_slot=9,
                     sol_in=0.001, pool=None, program=None, taxed=None, tax_bps=None,
                     mode="dry-run", sell_after_s=28.8)
    st.update_position("pj", state="selling", ts_accepted=time.time() - 200,
                        sell_attempts=2, ts_last_sell_attempt=time.time() - 100)
    было_вкл = os.environ.get("BLOOM_SELL_VIA_JUPITER")
    os.environ["BLOOM_SELL_VIA_JUPITER"] = "1"
    try:
        sj = Seller(state=st, live=False, api=api)
        chk("выключатель Jupiter прочитан", sj.jupiter_включён is True)

        import bloom_jupiter_sell as JT  # noqa: PLC0415
        старый = JT.продать
        вызовы = []
        try:
            JT.продать = lambda **kw: (вызовы.append(kw) or
                                        {"ok": True, "signature": "ПОДПИСЬ_JUP",
                                         "floor": {"checks": {"out_amount": 900000}}})
            r = sj.handle(st.positions()["pj"], balance_reader=читатель(5_000_000))
            chk("после двух неудач Bloom идёт попытка через Jupiter",
                r["action"] == "продажа через Jupiter отправлена", r["action"])
            chk("и в неё передан остаток по цепи и вход позиции",
                вызовы and вызовы[0]["amount_raw"] == 5_000_000
                and вызовы[0]["вход_sol"] == 0.001, вызовы)
            chk("подпись Jupiter записана в позицию",
                st.positions()["pj"].get("jup_signature") == "ПОДПИСЬ_JUP",
                st.positions()["pj"].get("jup_signature"))
            chk("и UNSOLD при удаче не ставится",
                st.positions()["pj"].get("state") != "unsold",
                st.positions()["pj"].get("state"))

            # второй раз Jupiter не пробуем: позиция должна дойти до UNSOLD
            вызовы.clear()
            st.update_position("pj", ts_last_sell_attempt=time.time() - 100)
            r2 = sj.handle(st.positions()["pj"], balance_reader=читатель(5_000_000))
            chk("вторую попытку через Jupiter не делаем", вызовы == [], вызовы)
            chk("и позиция помечена UNSOLD",
                st.positions()["pj"].get("state") == "unsold", r2["action"])

            # отказ Jupiter не должен мешать UNSOLD
            st.write_intent(client_order_id="pk", mint="MINTK", source_sig="SK",
                             source_slot=10, sol_in=0.001, pool=None, program=None,
                             taxed=None, tax_bps=None, mode="dry-run", sell_after_s=28.8)
            st.update_position("pk", state="selling", ts_accepted=time.time() - 200,
                                sell_attempts=2, ts_last_sell_attempt=time.time() - 100)
            JT.продать = lambda **kw: {"ok": False, "unsold": True,
                                        "why_not": "котировка 12.0 % от входа ниже 30 %",
                                        "floor": {"checks": {"out_amount": 120000}}}
            r3 = sj.handle(st.positions()["pk"], balance_reader=читатель(5_000_000))
            chk("котировка ниже 30 % -- не продаём и идём в UNSOLD",
                st.positions()["pk"].get("state") == "unsold", r3["action"])
            chk("и причина отказа Jupiter сохранена в позиции",
                "ниже 30" in str(st.positions()["pk"].get("jup_why_not")),
                st.positions()["pk"].get("jup_why_not"))
        finally:
            JT.продать = старый
    finally:
        if было_вкл is None:
            os.environ.pop("BLOOM_SELL_VIA_JUPITER", None)
        else:
            os.environ["BLOOM_SELL_VIA_JUPITER"] = было_вкл

    # --- сдача и доклад
    st.update_position("p3", ts_first_sell_attempt=time.time() - 601,
                        ts_last_sell_attempt=time.time() - 601)
    r = s.handle(st.positions()["p3"], balance_reader=читатель(5_000_000))
    chk("через 10 минут позиция помечена UNSOLD",
        st.positions()["p3"]["state"] == "unsold", r["action"])
    # Серия считает ВСЕ непроданные: выше в самопроверке уже сдалась p4
    # по двум неудачам, поэтому проверяется рост, а не ровно единица.
    chk("и серия непроданных выросла",
        int(st.counters().get("unsold_streak", 0)) >= 2,
        st.counters().get("unsold_streak"))

    # --- ПОЛОСА СВОЕЙ ОТПРАВКИ. Проверяем то, что стоит денег: количество
    # (ровно купленное полосой, никогда весь остаток минта -- в нём покупка
    # Bloom на 0.2 SOL), срок без запаса, свой счётчик UNSOLD и закрытие
    # позиции подтверждённой СВОЕЙ продажей.
    st_л = ExecState(base=база / "lane_sell", kill=база / "НЕТ_РУБИЛЬНИКА")

    def полосу_в_состояние(cid, минт, *, куплено=None, отправлено_назад=100.0,
                            вход=0.01, поля=None):
        st_л.write_intent(client_order_id=cid, mint=минт, source_sig=f"S{cid}",
                           source_slot=1, sol_in=вход, pool=None, program=None,
                           taxed=None, tax_bps=None, mode="live", sell_after_s=28.8,
                           lane=МЕТКА_ПОЛОСЫ)
        обн = {"state": "bought", "lane_signature": f"ПОДПИСЬ_{cid}",
                "ts_sent": time.time() - отправлено_назад}
        if куплено is not None:
            обн["lane_bought_raw"] = куплено
        обн.update(поля or {})
        st_л.update_position(cid, **обн)
        return st_л.positions()[cid]

    было_вкл_л = os.environ.get("BLOOM_SELL_VIA_JUPITER")
    os.environ["BLOOM_SELL_VIA_JUPITER"] = "1"
    import bloom_jupiter_sell as JL  # noqa: PLC0415
    старый_л = JL.продать
    try:
        sl = Seller(state=st_л, live=False, api=api)
        зовы: list = []
        JL.продать = lambda **kw: (зовы.append(kw) or
                                    {"ok": True, "signature": "ПОДПИСЬ_ПОЛОСЫ_JUP",
                                     "api_used": "swap_v2",
                                     "floor": {"checks": {"out_amount": 11_000_000}}})

        # 1. СРОК. До 28.8 с от отправки полоса не продаёт, и запас Bloom к
        # ней не применяется: авто-ордера у неё нет.
        рано = полосу_в_состояние("l_рано", "MINT_L1", куплено=1_000_000,
                                   отправлено_назад=10.0)
        r_рано = sl.handle(рано, balance_reader=читатель(50_000_000))
        chk("до срока полоса не продаёт", зовы == []
            and r_рано["action"] == "ждём срок продажи полосы", r_рано)
        почти = полосу_в_состояние("l_почти", "MINT_L2", куплено=1_000_000,
                                    отправлено_назад=29.0)
        r_почти = sl.handle(почти, balance_reader=читатель(50_000_000))
        chk("через 28.8 с полоса продаёт БЕЗ запаса сторожа",
            r_почти["action"] == "продажа полосы отправлена" and len(зовы) == 1,
            (r_почти, зовы))

        # 2. КОЛИЧЕСТВО -- РОВНО КУПЛЕННОЕ ПОЛОСОЙ, а не весь остаток.
        chk("в Jupiter ушло количество полосы, а не остаток минта",
            зовы[0]["amount_raw"] == 1_000_000
            and зовы[0]["вход_sol"] == 0.01, зовы[0])
        chk("и это же количество записано в позицию",
            st_л.positions()["l_почти"].get("jup_amount_raw") == 1_000_000
            and st_л.positions()["l_почти"].get("state") == "selling",
            st_л.positions()["l_почти"])

        # 2а. ИТОГ НЕСЧИТАЕМ, А НЕ НОЛЬ (уточнение владельца 25.09). Купили,
        # а продавать нечего -- значит токен ушёл не нашей продажей: такую
        # пару надо помечать, иначе в отчёте она выглядит сделкой в ноль.
        зовы.clear()
        пусто = полосу_в_состояние("l_пусто", "MINT_L2А", куплено=1_000_000,
                                    отправлено_назад=40.0)
        r_пусто = sl.handle(пусто, balance_reader=читатель(10))
        поз_пусто = st_л.positions()["l_пусто"]
        chk("купили, а остатка нет -- позиция закрыта и помечена несчитаемой",
            поз_пусто.get("state") == "closed"
            and поз_пусто.get("result_uncountable") is True
            and "ушёл не нашей продажей" in (поз_пусто.get("result_uncountable_why") or ""),
            (r_пусто, поз_пусто.get("result_uncountable_why")))
        chk("и продавать при этом никто не пытался",
            зовы == [], зовы)
        # А ВОТ БЕЗ КОЛИЧЕСТВА -- тоже несчитаемо, но позиция идёт в UNSOLD:
        # токен может лежать на кошельке, и молча закрывать её нельзя.
        без_кол = полосу_в_состояние("l_безкол", "MINT_L2В", куплено=None,
                                      отправлено_назад=4000.0)
        r_бк = sl.handle(без_кол, balance_reader=читатель(50_000_000))
        поз_бк = st_л.positions()["l_безкол"]
        chk("без количества пара несчитаема и уходит в UNSOLD, а не закрывается",
            поз_бк.get("result_uncountable") is True
            and поз_бк.get("state") == "unsold", (r_бк, поз_бк))

        # 2б. КОШЕЛЁК И КЛЮЧ ПОЛОСЫ (решение владельца 25.09: у полосы свой
        # кошелёк). Это самое денежное место сторожа: остаток читается с того
        # адреса, который покупал, и подписывается тем ключом, который этим
        # адресом владеет. Ошибка в любую сторону -- либо "продавать нечего"
        # на полном кошельке, либо отвергнутая сетью транзакция.
        было_кош = os.environ.get("OWN_SEND_WALLET")
        было_кл = os.environ.get("OWN_SEND_WALLET_KEY")
        os.environ["OWN_SEND_WALLET"] = "КОШЕЛЁК_ПОЛОСЫ_ТЕСТ"
        os.environ["OWN_SEND_WALLET_KEY"] = "КЛЮЧ_ПОЛОСЫ_ТЕСТ"
        try:
            адреса_чтения: list = []

            def читатель_с_адресом(raw):
                def f(wallet, mint):
                    адреса_чтения.append(wallet)
                    return {"ok": True, "raw": raw, "ui": raw / 1e6,
                            "accounts": 1, "failures": []}
                return f

            зовы.clear()
            свой = полосу_в_состояние("l_свой", "MINT_L2Б", куплено=1_000_000,
                                       отправлено_назад=40.0)
            r_свой = sl.handle(свой, balance_reader=читатель_с_адресом(50_000_000))
            chk("остаток позиции полосы читается с КОШЕЛЬКА ПОЛОСЫ",
                адреса_чтения == ["КОШЕЛЁК_ПОЛОСЫ_ТЕСТ"], адреса_чтения)
            chk("продажа полосы идёт с её кошелька и её ключом",
                len(зовы) == 1 and зовы[0]["taker"] == "КОШЕЛЁК_ПОЛОСЫ_ТЕСТ"
                and зовы[0]["секрет"] == "КЛЮЧ_ПОЛОСЫ_ТЕСТ", (r_свой, зовы))

            # И ОБРАТНО: позиция Bloom при заданном кошельке полосы всё равно
            # продаётся кошельком исполнителя и ключом по умолчанию.
            адреса_чтения.clear()
            зовы.clear()
            st_л.write_intent(client_order_id="l_блум", mint="MINT_BL",
                               source_sig="SBL", source_slot=1, sol_in=0.2,
                               pool=None, program=None, taxed=None, tax_bps=None,
                               mode="live", sell_after_s=28.8)
            st_л.update_position("l_блум", state="bought",
                                 ts_first_sell_attempt=time.time() - 100,
                                 ts_last_sell_attempt=time.time() - 100)
            поз_бл = st_л.positions()["l_блум"]
            chk("позиция Bloom читается кошельком исполнителя, не полосы",
                кошелёк_позиции(поз_бл) == EXECUTOR_WALLET
                and ключ_позиции(поз_бл) is None, кошелёк_позиции(поз_бл))
        finally:
            for имя_п, знач_п in (("OWN_SEND_WALLET", было_кош),
                                   ("OWN_SEND_WALLET_KEY", было_кл)):
                if знач_п is None:
                    os.environ.pop(имя_п, None)
                else:
                    os.environ[имя_п] = знач_п

        # 3. ОСТАТОК МЕНЬШЕ КУПЛЕННОГО -- продаём остаток, не больше.
        зовы.clear()
        мало = полосу_в_состояние("l_мало", "MINT_L3", куплено=5_000_000)
        r_мало = sl.handle(мало, balance_reader=читатель(2_000_000))
        chk("продать больше, чем есть, полоса не пытается",
            зовы and зовы[0]["amount_raw"] == 2_000_000, зовы)

        # 4. ОСТАТКА ПРАКТИЧЕСКИ НЕТ -- позиция закрыта, но это НЕ UNSOLD:
        # токен мог уже продать путь Bloom, он выходит всем остатком.
        зовы.clear()
        пусто = полосу_в_состояние("l_пусто", "MINT_L4", куплено=5_000_000)
        было_серии = int(st_л.counters().get(f"unsold_streak_{МЕТКА_ПОЛОСЫ}", 0))
        r_пусто = sl.handle(пусто, balance_reader=читатель(10))
        chk("остатка нет -- позиция закрыта без продажи и без UNSOLD",
            зовы == [] and st_л.positions()["l_пусто"].get("state") == STATE_CLOSED
            and int(st_л.counters().get(f"unsold_streak_{МЕТКА_ПОЛОСЫ}", 0))
            == было_серии, (r_пусто, st_л.positions()["l_пусто"].get("state")))

        # 5. КОЛИЧЕСТВА НЕТ. Пока идёт срок ожидания -- ждём догон с пульса.
        # Срок ожидания по умолчанию (10 с) КОРОЧЕ срока продажи (28.8 с), то
        # есть в бою полоса не ждёт вовсе -- и это решение владельца: держать
        # мемкоин пять минут вместо 28.8 с стоило котировки. Ветку ожидания
        # проверяем при явно удлинённом сроке, как если бы его задали в env.
        зовы.clear()
        было_ждать = sl.ждать_количество_s
        sl.ждать_количество_s = 60.0
        try:
            без_числа = полосу_в_состояние("l_ждём", "MINT_L5", куплено=None,
                                            отправлено_назад=40.0,
                                            поля={"lane_bought_why_not": "узел молчит"})
            r_ждём = sl.handle(без_числа, balance_reader=читатель(50_000_000))
            chk("без количества полоса ждёт догон и ничего не продаёт",
                зовы == [] and "ждём количество" in r_ждём["action"], r_ждём)
        finally:
            sl.ждать_количество_s = было_ждать
        chk("срок ожидания количества по умолчанию короче срока продажи",
            DEFAULT_LANE_AMOUNT_WAIT_S < 28.8, DEFAULT_LANE_AMOUNT_WAIT_S)

        # 5а. БЕЗ СВОЕГО КОШЕЛЬКА -- по-прежнему UNSOLD. Это денежная рамка:
        # на кошельке исполнителя лежат покупки Bloom, и продать там весь
        # остаток минта значило бы продать их.
        просрочено = полосу_в_состояние(
            "l_нет_числа", "MINT_L6", куплено=None, отправлено_назад=40.0)
        было_блум = int(st_л.counters().get("unsold_streak", 0))
        r_нет = sl.handle(просрочено, balance_reader=читатель(50_000_000))
        chk("нет количества и нет своего кошелька -- UNSOLD, а не продажа остатка",
            зовы == [] and st_л.positions()["l_нет_числа"].get("state") == "unsold"
            and "нет своего кошелька" in (r_нет.get("why_not") or ""), r_нет)
        chk("UNSOLD полосы поднял СВОЙ счётчик, а счётчик Bloom не тронул",
            int(st_л.counters().get(f"unsold_streak_{МЕТКА_ПОЛОСЫ}", 0)) >= 1
            and int(st_л.counters().get("unsold_streak", 0)) == было_блум,
            st_л.counters())

        # 5б. ПРОДАЖА ПО ОСТАТКУ (разрешение владельца 25.09). У полосы свой
        # кошелёк -- количества нет, срок вышел, продаём ВЕСЬ остаток минта с
        # кошелька полосы. Позиция не UNSOLD, но пара помечена несчитаемой:
        # сколько купили -- неизвестно.
        было_кош5 = os.environ.get("OWN_SEND_WALLET")
        было_кл5 = os.environ.get("OWN_SEND_WALLET_KEY")
        os.environ["OWN_SEND_WALLET"] = "КОШЕЛЁК_ПОЛОСЫ_ТЕСТ"
        os.environ["OWN_SEND_WALLET_KEY"] = "КЛЮЧ_ПОЛОСЫ_ТЕСТ"
        try:
            адреса5: list = []

            def читатель5(raw):
                def f(wallet, mint):
                    адреса5.append(wallet)
                    return {"ok": True, "raw": raw, "ui": raw / 1e6,
                            "accounts": 1, "failures": []}
                return f

            зовы.clear()
            было_серии5 = int(st_л.counters().get(
                f"unsold_streak_{МЕТКА_ПОЛОСЫ}", 0))
            по_ост = полосу_в_состояние(
                "l_по_остатку", "MINT_L6Б", куплено=None, отправлено_назад=40.0)
            r_ост = sl.handle(по_ост, balance_reader=читатель5(7_777_777))
            поз_ост = st_л.positions()["l_по_остатку"]
            chk("без количества со своим кошельком продаётся ВЕСЬ остаток",
                len(зовы) == 1 and зовы[0]["amount_raw"] == 7_777_777
                and r_ост.get("action") == "продажа полосы отправлена", (r_ост, зовы))
            chk("остаток для продажи целиком читается с кошелька ПОЛОСЫ",
                адреса5 == ["КОШЕЛЁК_ПОЛОСЫ_ТЕСТ"]
                and зовы[0]["taker"] == "КОШЕЛЁК_ПОЛОСЫ_ТЕСТ"
                and зовы[0]["секрет"] == "КЛЮЧ_ПОЛОСЫ_ТЕСТ", (адреса5, зовы))
            chk("продажа по остатку помечена в позиции и пара несчитаема",
                поз_ост.get("result_uncountable") is True
                and "по остатку" in (поз_ост.get("result_uncountable_why") or "")
                and r_ост.get("lane_sell_whole_remainder") is True,
                (поз_ост.get("result_uncountable_why"), r_ост))
            chk("продажа по остатку -- не UNSOLD: счётчик полосы не тронут",
                поз_ост.get("state") == "selling"
                and int(st_л.counters().get(f"unsold_streak_{МЕТКА_ПОЛОСЫ}", 0))
                == было_серии5, (поз_ост.get("state"), st_л.counters()))

            # 5б-2. ВОЗВРАТ СЧИТАЕТСЯ ПО КОШЕЛЬКУ ПОЛОСЫ. Это деньги: с
            # кошельком исполнителя возврат выходил ровно 0.000000 SOL на
            # каждой продаже полосы, и строка владельцу говорила "продана
            # +0.000000".
            def tx_полосы(подпись):
                б = {"accountIndex": 1, "mint": "MINT_L6Г",
                      "owner": "КОШЕЛЁК_ПОЛОСЫ_ТЕСТ",
                      "programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                      "uiTokenAmount": {"amount": "0", "decimals": 6,
                                         "uiAmount": 0.0}}
                б0 = {**б, "uiTokenAmount": {"amount": "1000000", "decimals": 6,
                                              "uiAmount": 1.0}}
                return {"slot": 77, "meta": {
                            "err": None, "fee": 5000,
                            "preBalances": [10 ** 9, 0],
                            "postBalances": [10 ** 9 + 7_000_000, 0],
                            "preTokenBalances": [б0], "postTokenBalances": [б],
                            "innerInstructions": []},
                        "transaction": {"message": {
                            "accountKeys": [{"pubkey": "КОШЕЛЁК_ПОЛОСЫ_ТЕСТ"},
                                             {"pubkey": EXECUTOR_WALLET}],
                            "instructions": []}}}

            посл_п: list = []
            было_опов = sl.оповещатель
            sl.оповещатель = type("О", (), {
                "послать": lambda self_, текст: посл_п.append(текст) or {"ok": True},
                "статус": lambda self_: {"enabled": True}})()
            try:
                вернулась = полосу_в_состояние(
                    "l_возврат", "MINT_L6Г", куплено=1_000_000,
                    поля={"last_sell_signatures": ["ПОДПИСЬ_ПОЛОСЫ"],
                          "ts_last_sell_attempt": time.time() - 30})
                итог_в = sl.доложить_прошлую_попытку(вернулась,
                                                      читатель_tx=tx_полосы)
                chk("возврат продажи полосы считается по ЕЁ кошельку",
                    итог_в.get("ok") is True
                    and float(итог_в.get("sol_delta") or 0) > 0.006, итог_в)
                chk("и строка владельцу называет продажу проданной с суммой",
                    посл_п and "продана" in посл_п[0]
                    and "+0.000000" not in посл_п[0], посл_п)
            finally:
                sl.оповещатель = было_опов

            # 5б-3. РАСХОД ПОКУПКИ ПО ЦЕПИ ДОГОНЯЕТСЯ ПЕРЕД ЗАКРЫТИЕМ. Это
            # деньги: суточный счёт пишется один раз, при закрытии, и без
            # нативной дельты покупки считает расход по полям -- без платы за
            # создание счетов. Живой замер 26.09: 0.0021 SOL спрятанного убытка
            # на сделке 0.01. Севшая подпись при этом НЕ первая в списке.
            спрошено: list = []

            def tx_покупки_и_продажи(подпись):
                спрошено.append(подпись)
                if подпись == "НЕСЕВШАЯ_ПОКУПКА":
                    return None
                if подпись == "СЕВШАЯ_ПОКУПКА":
                    # Нативная дельта кошелька полосы -0.01410228 вместе с
                    # комиссией 0.001005: вход 0.01, чаевые 0.001, приоритет
                    # 0.000005 и 0.00210228 -- плата за создание счетов.
                    return {"slot": 91, "meta": {
                                "err": None, "fee": 1_005_000,
                                "preBalances": [10 ** 9, 0],
                                "postBalances": [10 ** 9 - 14_107_280, 0],
                                "preTokenBalances": [], "postTokenBalances": [],
                                "innerInstructions": []},
                            "transaction": {"message": {
                                "accountKeys": [{"pubkey": "КОШЕЛЁК_ПОЛОСЫ_ТЕСТ"},
                                                 {"pubkey": EXECUTOR_WALLET}],
                                "instructions": []}}}
                return tx_полосы(подпись)

            догон = полосу_в_состояние(
                "l_догон_натив", "MINT_L6Д", куплено=1_000_000,
                поля={"last_sell_signatures": ["ПОДПИСЬ_ПРОДАЖИ_Ц"],
                      "ts_last_sell_attempt": time.time() - 30,
                      "lane_signature": "НЕСЕВШАЯ_ПОКУПКА",
                      "lane_pool_candidates": ["СЕВШАЯ_ПОКУПКА"],
                      "lane_tips_total_sol": 0.001,
                      "lane_priority_lamports": 5000})
            r_догон = sl.обработать_полосу(догон, now=time.time(),
                                            balance_reader=читатель5(0),
                                            читатель_tx=tx_покупки_и_продажи)
            зп_догон = st_л.positions()["l_догон_натив"]
            chk("расход покупки догнан ПО ЦЕПИ перед закрытием, и подпись севшая",
                abs((зп_догон.get("lane_buy_native_sol") or 0) + 0.01310228) < 1e-9
                and abs((зп_догон.get("lane_buy_fee_sol") or 0) - 0.001005) < 1e-9
                and зп_догон.get("lane_landed_signature") == "СЕВШАЯ_ПОКУПКА",
                {к: зп_догон.get(к) for к in ("lane_buy_native_sol",
                                               "lane_buy_fee_sol",
                                               "lane_landed_signature")})
            # Итог по цепи = возврат + натив - комиссия = 0.007 - 0.01310228 -
            # 0.001005 = -0.00710728. По полям (вход 0.01, чаевые 0.001,
            # приоритет 0.000005, тариф 0.000005) вышло бы -0.00401: разница
            # 0.00309728 -- плата за создание счетов плюс комиссия, которой поля
            # не видят.
            chk("и суточный счёт записан по цепи, а не по полям",
                зп_догон.get("pnl_counted") is True
                and abs((зп_догон.get("pnl_counted_sol") or 0) + 0.00710728) < 1e-9,
                {к: зп_догон.get(к) for к in ("pnl_counted", "pnl_counted_sol",
                                               "pnl_counted_spend_sol",
                                               "closed_sol_net")})
            chk("узел спрошен не больше трёх раз и позиция закрыта",
                len([п for п in спрошено if п in ("НЕСЕВШАЯ_ПОКУПКА",
                                                   "СЕВШАЯ_ПОКУПКА")]) <= 3
                and зп_догон.get("state") == STATE_CLOSED, (спрошено, r_догон))
            # Повторный вызов догона НЕ идёт к узлу: поле уже есть.
            спрошено.clear()
            r_повтор = sl.догнать_натив_покупки(зп_догон,
                                                 читатель_tx=tx_покупки_и_продажи)
            chk("догон не ходит к узлу второй раз: расход по цепи уже в записи",
                r_повтор.get("filled") is False and спрошено == []
                and "уже есть" in (r_повтор.get("why_not") or ""), r_повтор)

            # 5в. ЧУЖОЙ АДРЕС В ЗАПИСИ. Кошелёк полосы сменили после покупки:
            # остаток на новом адресе к этой позиции не относится -- UNSOLD.
            зовы.clear()
            чужой = полосу_в_состояние(
                "l_чужой_адрес", "MINT_L6В", куплено=None, отправлено_назад=40.0,
                поля={"wallet": "ДРУГОЙ_КОШЕЛЁК_ПОЛОСЫ"})
            r_чужой = sl.handle(чужой, balance_reader=читатель5(9_000_000))
            chk("кошелёк сменили -- остаток целиком не продаём, UNSOLD",
                зовы == []
                and st_л.positions()["l_чужой_адрес"].get("state") == "unsold"
                and "остаток не этой позиции" in (r_чужой.get("why_not") or ""),
                r_чужой)
        finally:
            for имя5, знач5 in (("OWN_SEND_WALLET", было_кош5),
                                 ("OWN_SEND_WALLET_KEY", было_кл5)):
                if знач5 is None:
                    os.environ.pop(имя5, None)
                else:
                    os.environ[имя5] = знач5

        # 6. РУБИЛЬНИК ПРОДАЖ останавливает и полосу.
        зовы.clear()
        под_килл = полосу_в_состояние("l_kill", "MINT_L7", куплено=1_000_000)
        st_л.kill_sell_path.write_text("стоп продажам", encoding="utf-8")
        r_kill = sl.handle(под_килл, balance_reader=читатель(50_000_000))
        chk("рубильник продаж останавливает полосу до всякой продажи",
            зовы == [] and "рубильник" in r_kill["action"], r_kill)
        st_л.kill_sell_path.unlink()

        # 7. ПРЕДЕЛ ПОПЫТОК У ПОЛОСЫ СНЯТ (решение владельца 25.09 вечера):
        # неудачная попытка НЕ уводит позицию в UNSOLD -- сторож пробует
        # следующим кругом, пока токен лежит на кошельке полосы.
        зовы.clear()
        JL.продать = lambda **kw: (зовы.append(kw) or
                                    {"ok": False, "why_not": "Ultra отказала",
                                     "api_tried": "swap_v2,ultra"})
        отказ = полосу_в_состояние("l_отказ", "MINT_L8", куплено=1_000_000,
                                    поля={"jup_attempts": sl.предел_неудач + 3,
                                          "ts_jup_attempt": time.time() - 100})
        было_серии7 = int(st_л.counters().get(f"unsold_streak_{МЕТКА_ПОЛОСЫ}", 0))
        r_отказ = sl.handle(отказ, balance_reader=читатель(50_000_000))
        chk("попытки сверх предела полосу НЕ останавливают: пробуем снова",
            len(зовы) == 1
            and st_л.positions()["l_отказ"].get("state") != "unsold"
            and int(st_л.counters().get(f"unsold_streak_{МЕТКА_ПОЛОСЫ}", 0))
            == было_серии7, (r_отказ, st_л.positions()["l_отказ"].get("state")))
        chk("причина отказа записана в позицию, а не потеряна",
            "Ultra отказала" in
            (st_л.positions()["l_отказ"].get("jup_why_not") or ""),
            st_л.positions()["l_отказ"].get("jup_why_not"))

        # 7а. ПОЛОСА ПРОДАЁТСЯ ПРИ ЛЮБОЙ КОТИРОВКЕ, Bloom -- нет. Это денежное
        # место: у полосы 0.01-0.05 SOL, и правило 30 % от входа держало её
        # позицию в UNSOLD с токеном на кошельке.
        зовы.clear()
        JL.продать = lambda **kw: (зовы.append(kw) or
                                    {"ok": True, "signature": "ПОДПИСЬ_ЛЮБАЯ"})
        любая_п = полосу_в_состояние("l_любая", "MINT_L8А", куплено=1_000_000)
        sl.handle(любая_п, balance_reader=читатель(50_000_000))
        chk("позиции полосы пол по котировке отменён (мин_доля_от_входа = 0)",
            len(зовы) == 1 and зовы[0].get("мин_доля_от_входа") == 0.0, зовы)
        chk("и в позицию записано, что продавали при любой котировке",
            st_л.positions()["l_любая"].get("jup_any_quote") is True,
            st_л.positions()["l_любая"].get("jup_any_quote"))
        chk("порог размера у разрешения есть: 0.2 SOL под него не попадает",
            продавать_при_любой_котировке({"lane": МЕТКА_ПОЛОСЫ, "sol_in": 0.2})
            is False
            and продавать_при_любой_котировке({"lane": МЕТКА_ПОЛОСЫ,
                                                "sol_in": 0.05}) is True
            and продавать_при_любой_котировке({"sol_in": 0.01}) is False,
            "рамка размера")

        # 8. СВОЯ ПРОДАЖА СЕЛА -- позиция закрыта СВОИМ результатом, а не по
        # нулевому остатку минта: остаток общий с Bloom.
        зовы.clear()
        села = полосу_в_состояние(
            "l_села", "MINT_L9", куплено=1_000_000,
            поля={"state": "selling", "last_sell_signatures": ["ПРОДАЖА_ПОЛОСЫ"],
                  "jup_attempts": 1, "ts_jup_attempt": time.time() - 100})

        def читатель_продажи(подпись):
            return {"slot": 5, "meta": {
                "err": None, "fee": 5000,
                "preBalances": [1_000_000_000], "postBalances": [1_011_000_000],
                "preTokenBalances": [], "postTokenBalances": [],
                "logMessages": []}, "transaction": {"message": {
                    "accountKeys": [{"pubkey": EXECUTOR_WALLET, "signer": True}]}}}

        r_села = sl.handle(села, balance_reader=читатель(50_000_000),
                           читатель_tx=читатель_продажи)
        поз_села = st_л.positions()["l_села"]
        chk("подтверждённая продажа полосы закрывает её позицию своим итогом",
            зовы == [] and поз_села.get("state") == STATE_CLOSED
            and поз_села.get("closed_sol_net") is not None
            and "подтверждена" in (поз_села.get("closed_reason") or ""),
            (r_села.get("action"), поз_села.get("closed_reason"),
             поз_села.get("closed_sol_net")))
        chk("и удачная продажа полосы обнулила её счётчик непроданных",
            int(st_л.counters().get(f"unsold_streak_{МЕТКА_ПОЛОСЫ}", 0)) == 0,
            st_л.counters())

        # 9. ПУТЬ JUPITER ВЫКЛЮЧЕН -- полоса не продаёт и говорит почему.
        зовы.clear()
        os.environ["BLOOM_SELL_VIA_JUPITER"] = "0"
        sl2 = Seller(state=st_л, live=False, api=api)
        выкл = полосу_в_состояние("l_выкл", "MINT_LA", куплено=1_000_000)
        r_выкл = sl2.handle(выкл, balance_reader=читатель(50_000_000))
        chk("без пути Jupiter полоса не продаёт и причина названа",
            зовы == [] and "продавать нечем" in r_выкл["action"], r_выкл)
        os.environ["BLOOM_SELL_VIA_JUPITER"] = "1"

        # 10. check_only честно показывает полосу: количество и чем продаём.
        план_л = sl.check_only(balance_reader=читатель(50_000_000))
        ряд = [з for з in план_л["plan"] if з["client_order_id"] == "l_рано"]
        chk("в плане у позиции полосы своё количество и путь Jupiter",
            ряд and ряд[0]["amount_raw"] == 1_000_000
            and "Jupiter" in ряд[0]["with_what"] and ряд[0]["lane"] == МЕТКА_ПОЛОСЫ,
            ряд)
    finally:
        JL.продать = старый_л
        if было_вкл_л is None:
            os.environ.pop("BLOOM_SELL_VIA_JUPITER", None)
        else:
            os.environ["BLOOM_SELL_VIA_JUPITER"] = было_вкл_л

    # --- тело продажи, которое сторож реально отправляет
    body = build_sell_body(address="MINT3", percent=100, slippage_pct=40.0,
                           priority_fee=0.001, processor_tip=0.001)
    chk("сторож продаёт 100 %", body["wallets"][0]["amount"] == "100")
    chk("проскальзывание ровно 40", body["slippage"] == 40.0)
    chk("auto_orders пустой -- чужая стратегия не подмешивается",
        body["auto_orders"] == [])
    chk("кошелёк -- исполнителя", body["wallets"][0]["address"] == EXECUTOR_WALLET)

    # --- признак жизни
    hb = st.base / "seller_heartbeat.json"
    было = hb.exists()
    s.cycle(balance_reader=читатель(0))
    chk("круг пишет признак жизни", hb.exists() and not было or hb.exists())
    hb_data = json.loads(hb.read_text())
    chk("в признаке жизни есть время и режим",
        "updated_ts" in hb_data and hb_data["mode"] == "dry-run", str(hb_data)[:120])
    chk("в признаке жизни есть версия формата", hb_data.get("schema_version") == 2,
        hb_data.get("schema_version"))
    chk("все ключи признака жизни латинские",
        all(k.isascii() for k in hb_data), [k for k in hb_data if not k.isascii()])

    # --- check-only ничего не меняет
    до = st.positions_path.stat().st_size
    план = s.check_only(balance_reader=читатель(5_000_000))
    chk("check-only даёт план", isinstance(план.get("plan"), list))
    chk("и ничего не пишет в журнал позиций",
        st.positions_path.stat().st_size == до)

    chk("в признаке жизни есть оба рубильника",
        "kill_buy" in hb_data and "kill_sell" in hb_data, list(hb_data))
    chk("и отдельно сказано, читаются ли они",
        hb_data["kill_buy"].get("readable") is True
        and hb_data["kill_sell"].get("readable") is True, hb_data)
    chk("и что оба выключены",
        not hb_data["kill_buy"]["active"]
        and not hb_data["kill_sell"]["active"], hb_data)

    # нечитаемый рубильник продаж не должен выглядеть как тишина
    было_ks = os.environ.get("BLOOM_KILL_SELL_FILE")
    os.environ["BLOOM_KILL_SELL_FILE"] = str(st.base / "нет_каталога" / "KILL_SELL")
    try:
        s.heartbeat({"positions": 0})
        hb3 = json.loads(hb.read_text())
        chk("нечитаемый рубильник продаж помечен недоступным",
            hb3["kill_sell"]["readable"] is False, hb3["kill_sell"])
        chk("и причина названа словами",
            "не существует" in hb3["kill_sell"]["note"],
            hb3["kill_sell"]["note"])
    finally:
        if было_ks is None:
            os.environ.pop("BLOOM_KILL_SELL_FILE", None)
        else:
            os.environ["BLOOM_KILL_SELL_FILE"] = было_ks

    chk("ключи вычищаются", "СЕКРЕТ" not in scrub_all("текст СЕКРЕТ")
        if os.environ.get("BLOOM_API_KEY") == "СЕКРЕТ" else True)

    bad = 0
    for n, ok_, got in checks:
        print(f"  [{'ok  ' if ok_ else 'СБОЙ'}] {n}" + (f"  -> {got}" if got and not ok_ else ""))
        bad += (not ok_)
    print(f"самопроверка сторожа продаж: {len(checks) - bad}/{len(checks)} пройдено")
    if bad:
        raise SystemExit(f"самопроверка не пройдена: {bad} из {len(checks)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop-every-s", type=float,
                     default=env_float("BLOOM_SELLER_LOOP_S", DEFAULT_LOOP_EVERY_S))
    a = ap.parse_args()
    if a.self_test:
        self_test()
        return
    s = Seller()
    if a.check_only:
        print(json.dumps(s.check_only(), ensure_ascii=False, indent=2))
        return
    print(f"[сторож] режим {'LIVE' if s.live else 'DRY-RUN'}, "
          f"проскальзывание {s.slippage} %, запас {s.grace_s} с, "
          f"повтор {s.retry_every_s} с, потолок {s.give_up_after_s} с", flush=True)
    while True:
        начало = time.monotonic()
        try:
            итог = s.cycle()
            if итог["positions"]:
                print(json.dumps(итог, ensure_ascii=False)[:2000], flush=True)
        except Exception as exc:  # noqa: BLE001 -- круг не должен валить процесс
            print(scrub_all(f"[сторож] круг упал: {type(exc).__name__}: {exc}"), flush=True)
        if a.once:
            return
        пауза = max(1.0, a.loop_every_s - (time.monotonic() - начало))
        time.sleep(пауза)


if __name__ == "__main__":
    main()
