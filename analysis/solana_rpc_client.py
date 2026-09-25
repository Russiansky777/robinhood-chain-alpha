#!/usr/bin/env python3
"""Общий клиент узла Solana: темп, запасной путь и учёт кредитов Helius.

Один клиент на все службы -- ledger, зонд, прогоны. До него у каждого
скрипта был свой темп и своё (или никакое) поведение при 429, и когда у
Helius кончилась квота, половина репозитория встала, а причину пришлось
выкапывать из логов заданий.

Тариф Developer (владелец, 23.09): 10 млн кредитов в месяц, 50 запросов в
секунду к узлу, 10 в секунду к расширенным API, 150 подключений WebSocket,
отправка транзакций 5 в секунду.

Мы держим темп НИЖЕ купленного -- 40 и 8 вместо 50 и 10: лимит считается
на стороне провайдера по своим окнам, и упираться в потолок значит
собирать 429 на ровном месте.

Цены (docs.helius.dev): обычный вызов, getTransaction, getBlock -- 1
кредит; getProgramAccounts -- 10; Enhanced Transactions API -- 100;
подписка на логи по WebSocket -- 2 кредита за 0.1 МБ.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
USAGE_PATH = REPO_ROOT / "data" / "helius_usage.json"
# Учёт ведётся ОТДЕЛЬНЫМ файлом на службу. Общий файл не годится: каждый
# прогон в Actions работает в своей копии репозитория, и при коммите
# второй прогон затирал бы цифры первого. По этой причине в репозитории
# на 13:07Z оказалась только одна служба из четырёх.
USAGE_DIR = REPO_ROOT / "data" / "helius_usage"


def usage_path_for(service: str, base: Path = USAGE_DIR) -> Path:
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in service)
    return base / f"{safe}.json"


def usage_shards(base: Path = USAGE_DIR) -> list[Path]:
    """Все осколки учёта. Старый общий файл подхватывается только для
    настоящего каталога -- в тестах он не должен подмешиваться."""
    out = sorted(base.glob("*.json")) if base.exists() else []
    if base == USAGE_DIR and USAGE_PATH.exists():
        out.append(USAGE_PATH)
    return out


def merge_usage(paths: list[Path]) -> dict:
    """Слить осколки учёта в одну картину. Складываем, а не перезаписываем:
    одна и та же служба может писать из разных прогонов."""
    merged: dict = {"дни": {}}
    for p in paths:
        try:
            data = json.loads(p.read_text())
        except (ValueError, OSError):
            continue
        for day, svcs in (data.get("дни") or {}).items():
            dd = merged["дни"].setdefault(day, {})
            for name, v in (svcs or {}).items():
                if not isinstance(v, dict):
                    continue
                cur = dd.setdefault(name, {"кредитов_за_день": 0, "байт_за_день": 0,
                                            "по_часам": {}})
                cur["кредитов_за_день"] += v.get("кредитов_за_день", 0)
                cur["байт_за_день"] += v.get("байт_за_день", 0)
                for hour, h in (v.get("по_часам") or {}).items():
                    ch = cur["по_часам"].setdefault(hour, {"кредитов": 0, "байт": 0})
                    ch["кредитов"] += h.get("кредитов", 0)
                    ch["байт"] += h.get("байт", 0)
        for k in ("обновлено_utc", "тариф"):
            if data.get(k):
                merged[k] = max(merged.get(k, ""), data[k]) if k == "обновлено_utc" else data[k]
    merged["осколков"] = len(paths)
    merged["источники"] = [p.name for p in paths]
    return merged

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
HELIUS_RPC_HOST = "https://mainnet.helius-rpc.com"
HELIUS_API_HOST = "https://api.helius.xyz"

# Темп: ниже купленного, чтобы не собирать 429 на ровном месте.
NODE_RPS = 40.0
ENHANCED_RPS = 8.0

# 429: пауза от 1 с с удвоением до 30 с плюс случайная добавка.
BACKOFF_START_S = 1.0
BACKOFF_CAP_S = 30.0
BACKOFF_JITTER_S = 0.5

# Публичный узел -- ТОЛЬКО запасной.
DEMOTE_AFTER_429 = 3
DEMOTE_FOR_S = 600.0

# Сколько запросов кладём в одну пачку.
BATCH_GET_TRANSACTION = 100
BATCH_OTHER = 10

CREDITS_DEFAULT = 1
CREDITS_BY_METHOD = {"getProgramAccounts": 10}
CREDITS_ENHANCED = 100
CREDITS_PER_01MB_WS = 2

# Дневной бюджет кредитов (владелец, уточнено 23.09).
#
# Службы названы поимённо, а не одним словом "прогоны": на замере надо
# видеть, КТО именно ест кредиты, иначе цифра "прогоны: столько-то"
# ничего не подсказывает. Бюджет при этом общий на всю группу прогонов --
# они не идут одновременно постоянно, и делить его поштучно значило бы
# душить тот прогон, который сегодня нужнее.
# Владелец, 23.09: суточный ориентир тарифа ~333 000 кредитов, группа
# "прогоны" 200 000, детектор 60 000, сторож продаж 15 000 -- итого
# 275 000, то есть с запасом.
# grpc_feed_probe -- зонд гонки потоков. Правило владельца 24.09: его расход
# по Helius за сутки НЕ БОЛЬШЕ расхода детектора (тот тратит около 9 000), и
# при 70 % своего предела зонд сам сбрасывает разгонные адреса. Детектор при
# этом не трогается ни при каких условиях -- у него свой бюджет ниже.
# Владелец 24.09, 19:40: двухшаговой тени разрешено 100 000 кредитов в
# сутки, чтобы она работала круглые сутки. Эти кредиты тратит ДЕТЕКТОР --
# подписка на четыре котировочных пула идёт его соединением. Значит его
# суточный бюджет обязан быть больше: 60 000 оставили бы сторожа расхода
# кричать на то, что владелец сам и разрешил. 150 000 = 100 000 на замер
# плюс запас на саму торговлю (она берёт около 10 000 в сутки).
# Синтетический тест гонки отправок: свой небольшой бюджет. Он тратит
# getSignaturesForAddress и getTransaction на шаблоны пула (около 8 вызовов
# на пару) плюс getBlock на слоты -- при тридцати парах это сотни вызовов, а
# не тысячи. 20 000 -- с запасом на повторный прогон в те же сутки.
# c2_followers_growth (задача F, C2): на сделку источника -- один getBlock
# (блок источника) плюс обычно один-два дальше, пока не наберётся 5
# копировщиков (разведка: на этих минтах за 30 с медиана 88 покупателей,
# то есть блоков сверх источника обычно нужно немного), плюс getBlock на
# якорь и getSignaturesForAddress/getTransaction на окно "цена через 30 с"
# у каждой сделки. На 150 сделок это, по грубой прикидке, порядка нескольких
# тысяч вызовов -- 40 000 берёт с большим запасом на повторный прогон в те
# же сутки. Общий потолок C2 (см. c2_common.C2_DAILY_BUDGET) при этом
# остаётся главным пределом -- эта строка только для отдельной строки в
# общем отчёте по службам.
# ПОТОЛОК ДЕТЕКТОРА -- 1 000 000 В СУТКИ. Решение владельца 25.09: поднять с
# 300 000, потому что 60 боевых источников дают трафик подписки в сотни МБ в
# час (замерено: 44 445 кредитов и 1281 МБ за час с 80 источниками), а сигналы
# нужнее кредитов. Сторож расхода детектор не глушит (он в НЕ_ОСТАНАВЛИВАТЬ) и
# батчи по порогу НЕ РЕЖЕТ -- прежнее правило "при 70 % снять BATCH-1 и BATCH-4"
# тем же решением отменено.
DAILY_BUDGET = {"зонд": 150_000, "ledger": 50_000,
                 "bloom_detector": 1_000_000, "bloom_seller": 15_000,
                 "grpc_feed_probe": 300_000, "bloom_send_race": 20_000,
                 "c2_followers_growth": 40_000}

# Владелец 24.09, поправка к пределам: допустимо до 500 000 кредитов в сутки
# (перерасход сверх плана оплачивается по 5 долларов за миллион). Рабочий
# потолок на всё -- 400 000, из них зонду гонки потоков до 300 000. Сторож с
# порогом 70 % и прогнозом остаётся, но смысл у него теперь другой: он
# защищает не от нехватки, а от СЛУЧАЙНОЙ подписки на адрес с тысячами
# транзакций в час.
# Общий потолок поднят вместе с детекторским: 1 000 000 детектору плюс прежние
# ~200 000 остальным. Иначе две настройки противоречили бы друг другу, и та,
# что строже, молча резала бы то, что владелец разрешил.
ОБЩИЙ_ПОТОЛОК_СУТКИ = 1_200_000

# Службы, которые НЕЛЬЗЯ глушить по порогу, даже если когда-нибудь
# включат общий STOP_AT_WARN. Владелец про детектор прямо: в live он
# важнее кредитов, при превышении -- доклад, а не останов. Зонд в том же
# списке потому, что он и есть источник данных о расходе.
# Зонд гонки потоков себя не глушит целиком: он ужимается до 19 источников
# сам, внутри себя, и продолжает мерить. Полный останов означал бы потерю
# замера там, где достаточно снять разгонные адреса.
НЕ_ОСТАНАВЛИВАТЬ = ("зонд", "bloom_detector", "bloom_seller", "grpc_feed_probe")
BUDGET_GROUPS = {"прогоны": 200_000}
# Службы исполнителя Bloom в группы не входят: у них СВОИ бюджеты
# (см. DAILY_BUDGET), а не доля общего котла прогонов.
GROUP_OF = {
    "горизонты": "прогоны",
    "разбор_пилота": "прогоны",
    "ретро": "прогоны",
    "скан_толпы": "прогоны",
    "порог_влияния": "прогоны",
    "прогоны": "прогоны",
}
WARN_AT = 0.70
# Что делать при 70%. Владелец: горизонты НЕ останавливать, только
# доложить. Поэтому флаг -- сигнальный, а не тормоз: ни одна служба здесь
# сама себя не глушит.
STOP_AT_WARN = False


def helius_key() -> tuple[str, str]:
    """Ключ и имя переменной, из которой он взят. Сам ключ не печатаем."""
    for name in ("HELIUS_API_KEY", "HELIUS_API"):
        v = (os.environ.get(name) or "").strip()
        if v:
            return v, name
    return "", "нет"


def key_tail(key: str) -> str:
    """Последние 4 символа -- для сверки ключей без их раскрытия."""
    return f"...{key[-4:]}" if len(key) >= 4 else "(пусто)"


def scrub(text: str, key: str = "") -> str:
    """Убрать ключ из любого текста, который может попасть в лог."""
    out = text
    k = key or helius_key()[0]
    if k:
        out = out.replace(k, "<КЛЮЧ>")
    return out


class RateLimiter:
    """Ведро токенов. Общее на процесс, потокобезопасное."""

    def __init__(self, rps: float) -> None:
        self.rps = rps
        self._lock = threading.Lock()
        self._allowance = rps
        self._last = time.monotonic()

    def take(self) -> float:
        """Дождаться права на один запрос. Возвращает, сколько ждали."""
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._allowance = min(self.rps,
                                       self._allowance + (now - self._last) * self.rps)
                self._last = now
                if self._allowance >= 1.0:
                    self._allowance -= 1.0
                    return waited
                need = (1.0 - self._allowance) / self.rps
            time.sleep(need)
            waited += need


class CreditMeter:
    """Учёт кредитов по службам: за час и накопительно за день.

    Пишется в data/helius_usage.json. Служба, перевалившая за 70% своего
    дневного бюджета, помечается -- по этой пометке прогоны ставятся на
    паузу, а зонд не трогается (он и есть источник данных)."""

    def __init__(self, service: str, base: Path | None = None) -> None:
        self.service = service
        self.base = Path(base) if base else USAGE_DIR
        self.path = usage_path_for(service, self.base)
        self.base.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.session_credits = 0

    @staticmethod
    def _now_keys(ts: float | None = None) -> tuple[str, str]:
        t = time.gmtime(ts if ts is not None else time.time())
        return (time.strftime("%Y-%m-%d", t), time.strftime("%Y-%m-%dT%HZ", t))

    def add(self, credits: int, *, bytes_in: int = 0, ts: float | None = None) -> dict:
        with self._lock:
            self.session_credits += credits
            day, hour = self._now_keys(ts)
            data = {}
            if self.path.exists():
                try:
                    data = json.loads(self.path.read_text())
                except (ValueError, OSError):
                    data = {}
            days = data.setdefault("дни", {})
            d = days.setdefault(day, {})
            svc = d.setdefault(self.service, {"кредитов_за_день": 0, "байт_за_день": 0,
                                               "по_часам": {}})
            svc["кредитов_за_день"] += credits
            svc["байт_за_день"] += bytes_in
            h = svc["по_часам"].setdefault(hour, {"кредитов": 0, "байт": 0})
            h["кредитов"] += credits
            h["байт"] += bytes_in
            group = GROUP_OF.get(self.service)
            svc["группа"] = group
            own = DAILY_BUDGET.get(self.service)
            svc["бюджет_за_день"] = own if own else BUDGET_GROUPS.get(group)
            # У службы из группы бюджет ОБЩИЙ: считаем потраченное всей
            # группой, иначе каждая по отдельности вечно будет "в норме",
            # а вместе они уже вышли за предел.
            spent = svc["кредитов_за_день"]
            if group:
                # Соседние службы группы пишут в СВОИ осколки, поэтому
                # тратой группы считается сумма по всем осколкам, а не
                # только по своему файлу.
                all_d = (merge_usage(usage_shards(self.base)).get("дни") or {}).get(day) or {}
                all_d = dict(all_d)
                all_d[self.service] = svc
                spent = sum(v.get("кредитов_за_день", 0) for k, v in all_d.items()
                            if isinstance(v, dict) and GROUP_OF.get(k) == group)
                svc["потрачено_группой"] = spent
            if svc["бюджет_за_день"]:
                svc["доля_бюджета"] = round(spent / svc["бюджет_за_день"], 4)
                svc["выше_70_процентов"] = svc["доля_бюджета"] >= WARN_AT
                svc["останавливать_при_пороге"] = (
                    STOP_AT_WARN and self.service not in НЕ_ОСТАНАВЛИВАТЬ)
            data["обновлено_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            data["тариф"] = "Developer: 10 млн кредитов/мес, 50 rps узел, 10 rps расширенные API"
            data["бюджеты_за_день"] = DAILY_BUDGET
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            tmp.replace(self.path)
            return svc

    @staticmethod
    def over_budget(service: str, base: Path | None = None) -> dict:
        """Состояние бюджета службы: доля и флаг 70%."""
        group = GROUP_OF.get(service)
        own = DAILY_BUDGET.get(service)
        out = {"служба": service, "группа": group,
               "бюджет": own if own else BUDGET_GROUPS.get(group),
               "потрачено": 0, "потрачено_группой": 0, "доля": 0.0,
               "выше_70_процентов": False, "останавливать_при_пороге": STOP_AT_WARN}
        data = merge_usage(usage_shards(Path(base) if base else USAGE_DIR))
        if not data.get("дни"):
            return out
        day = time.strftime("%Y-%m-%d", time.gmtime())
        d = (data.get("дни") or {}).get(day) or {}
        svc = d.get(service) or {}
        out["потрачено"] = svc.get("кредитов_за_день", 0)
        spent = out["потрачено"]
        if group:
            spent = sum(v.get("кредитов_за_день", 0) for k, v in d.items()
                        if isinstance(v, dict) and GROUP_OF.get(k) == group)
        out["потрачено_группой"] = spent
        if out["бюджет"]:
            out["доля"] = round(spent / out["бюджет"], 4)
            out["выше_70_процентов"] = out["доля"] >= WARN_AT
        return out

    @staticmethod
    def report(base: Path | None = None, day: str | None = None) -> dict:
        """Расход за день по КАЖДОЙ службе отдельно + по группам.

        Нужен ровно для замера: без разбивки поимённо не видно, кто ест."""
        out = {"день": day or time.strftime("%Y-%m-%d", time.gmtime()),
               "по_службам": {}, "по_группам": {}, "всего_кредитов": 0}
        paths = usage_shards(Path(base) if base else USAGE_DIR)
        if not paths:
            out["почему_пусто"] = "осколков учёта нет -- ни одна служба ещё не писала"
            return out
        out["осколки"] = [x.name for x in paths]
        data = merge_usage(paths)
        if False:
            pass
        d = (data.get("дни") or {}).get(out["день"]) or {}
        for name, v in sorted(d.items()):
            if not isinstance(v, dict):
                continue
            hours = v.get("по_часам") or {}
            out["по_службам"][name] = {
                "кредитов_за_день": v.get("кредитов_за_день", 0),
                "байт_за_день": v.get("байт_за_день", 0),
                "часов_с_активностью": len(hours),
                "кредитов_в_час_в_среднем": (round(v.get("кредитов_за_день", 0) / len(hours))
                                              if hours else None),
                "пик_за_час": max((h.get("кредитов", 0) for h in hours.values()), default=0),
                "группа": GROUP_OF.get(name),
            }
            out["всего_кредитов"] += v.get("кредитов_за_день", 0)
        for g, budget in BUDGET_GROUPS.items():
            spent = sum(x["кредитов_за_день"] for n, x in out["по_службам"].items()
                        if GROUP_OF.get(n) == g)
            out["по_группам"][g] = {"потрачено": spent, "бюджет": budget,
                                     "доля": round(spent / budget, 4) if budget else None}
        for n, budget in DAILY_BUDGET.items():
            spent = out["по_службам"].get(n, {}).get("кредитов_за_день", 0)
            out["по_группам"][n] = {"потрачено": spent, "бюджет": budget,
                                     "доля": round(spent / budget, 4) if budget else None}
        return out


def credits_for(method: str, n_in_batch: int = 1, *, enhanced: bool = False) -> int:
    """Сколько кредитов стоит вызов. Пачка считается по числу запросов в ней:
    провайдер берёт за каждый, а не за HTTP-обёртку."""
    if enhanced:
        return CREDITS_ENHANCED * n_in_batch
    return CREDITS_BY_METHOD.get(method, CREDITS_DEFAULT) * n_in_batch


def credits_for_ws_bytes(n_bytes: int) -> int:
    """2 кредита за 0.1 МБ; неполная порция считается как полная."""
    chunk = 100_000
    return CREDITS_PER_01MB_WS * ((n_bytes + chunk - 1) // chunk)


class SolanaRpc:
    """Клиент с общим темпом, запасным путём и учётом кредитов."""

    def __init__(self, service: str, key: str | None = None, *,
                  node_rps: float = NODE_RPS, enhanced_rps: float = ENHANCED_RPS,
                  usage_dir: Path = USAGE_DIR, allow_public: bool = True) -> None:
        self.service = service
        self.key = key if key is not None else helius_key()[0]
        self.node_limiter = RateLimiter(node_rps)
        self.enhanced_limiter = RateLimiter(enhanced_rps)
        self.meter = CreditMeter(service, usage_dir)
        self.allow_public = allow_public
        self._lock = threading.Lock()
        self._429_streak = 0
        self._demoted_until = 0.0
        self.stats = {"helius_ok": 0, "helius_429": 0, "helius_ошибки": 0,
                       "публичный_ok": 0, "публичный_429": 0, "публичный_ошибки": 0,
                       "helius_понижен": False, "первый_ответ_429_от_helius": None,
                       "кредитов": 0, "пачек": 0, "запросов_в_пачках": 0}
        self.deadline: float | None = None

    # ---------- выбор узла ----------

    def helius_url(self) -> str:
        return f"{HELIUS_RPC_HOST}/?api-key={self.key}"

    def helius_available(self) -> bool:
        if not self.key:
            return False
        with self._lock:
            return time.monotonic() >= self._demoted_until

    def pick_url(self) -> str:
        if self.helius_available():
            return self.helius_url()
        if not self.allow_public:
            return self.helius_url() if self.key else PUBLIC_RPC
        return PUBLIC_RPC

    def _demote(self, body: str) -> None:
        with self._lock:
            self._demoted_until = time.monotonic() + DEMOTE_FOR_S
            if not self.stats["helius_понижен"]:
                self.stats["helius_понижен"] = True
                print(f"[rpc] Helius понижен на {DEMOTE_FOR_S:.0f}с "
                      f"({self._429_streak} ответов 429 подряд): "
                      f"{scrub(body, self.key)[:120]}", flush=True)

    def _note_429(self, url: str, body: str) -> None:
        with self._lock:
            if url == PUBLIC_RPC:
                self.stats["публичный_429"] += 1
                return
            self.stats["helius_429"] += 1
            if self.stats["первый_ответ_429_от_helius"] is None:
                # Тело нужно, чтобы отличить нехватку кредитов
                # ("max usage reached") от превышения темпа.
                self.stats["первый_ответ_429_от_helius"] = scrub(body, self.key)[:200]
            self._429_streak += 1
            streak = self._429_streak
        if streak >= DEMOTE_AFTER_429 and self.allow_public:
            self._demote(body)

    def _note_ok(self, url: str) -> None:
        with self._lock:
            if url == PUBLIC_RPC:
                self.stats["публичный_ok"] += 1
            else:
                self.stats["helius_ok"] += 1
                self._429_streak = 0

    def expired(self) -> bool:
        return self.deadline is not None and time.monotonic() > self.deadline

    # ---------- вызовы ----------

    def _charge(self, method: str, n: int, *, enhanced: bool = False,
                bytes_in: int = 0) -> None:
        c = credits_for(method, n, enhanced=enhanced)
        self.stats["кредитов"] += c
        self.meter.add(c, bytes_in=bytes_in)

    def call(self, method: str, params: list, *, url: str | None = None,
             attempts: int = 8, enhanced: bool = False):
        """Один вызов. Возвращает result; бросает RuntimeError, если не вышло."""
        pinned = url
        last = "попыток не было"
        backoff = 0.0
        for _ in range(attempts):
            if self.expired():
                raise RuntimeError(f"{method}: бюджет времени прогона истёк")
            target = pinned or self.pick_url()
            (self.enhanced_limiter if enhanced else self.node_limiter).take()
            try:
                resp = requests.post(target, json={"jsonrpc": "2.0", "id": 1,
                                                    "method": method, "params": params},
                                      timeout=45)
            except Exception as exc:  # noqa: BLE001
                last = f"сеть: {type(exc).__name__}"
                self.stats["helius_ошибки" if target != PUBLIC_RPC else "публичный_ошибки"] += 1
                backoff = _next_backoff(backoff)
                time.sleep(backoff)
                continue
            if target != PUBLIC_RPC:
                self._charge(method, 1, enhanced=enhanced)
            if resp.status_code == 429:
                self._note_429(target, resp.text)
                last = "HTTP 429"
                # Если Helius только что понижен -- следующая попытка уйдёт
                # на публичный узел, ждать незачем.
                if target != PUBLIC_RPC and not pinned and not self.helius_available():
                    backoff = 0.0
                    continue
                backoff = _next_backoff(backoff)
                time.sleep(backoff)
                continue
            if not resp.ok:
                last = f"HTTP {resp.status_code}"
                self.stats["helius_ошибки" if target != PUBLIC_RPC else "публичный_ошибки"] += 1
                if target != PUBLIC_RPC and not pinned and self.allow_public:
                    self._demote(resp.text)
                    continue
                raise RuntimeError(f"{method}: {last}: {scrub(resp.text[:200], self.key)}")
            body = resp.json()
            if "error" in body:
                raise RuntimeError(f"{method}: RPC error {str(body['error'])[:200]}")
            self._note_ok(target)
            return body.get("result")
        raise RuntimeError(f"{method}: исчерпаны попытки, последняя причина: {last}")

    def batch(self, reqs: list[tuple[str, list]], *, chunk: int | None = None) -> list:
        """Пачка вызовов. getTransaction -- до 100 в запросе, прочее -- до 10.

        Пачка не должна тихо терять элементы: на любой сбой элемент падает
        обратно в одиночный вызов, а не превращается в None молча."""
        if not reqs:
            return []
        if chunk is None:
            chunk = (BATCH_GET_TRANSACTION
                     if all(m == "getTransaction" for m, _ in reqs) else BATCH_OTHER)
        out: list = [None] * len(reqs)
        for start in range(0, len(reqs), chunk):
            part = reqs[start:start + chunk]
            if self.expired():
                break
            target = self.pick_url()
            self.node_limiter.take()
            payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p}
                       for i, (m, p) in enumerate(part)]
            ok = False
            try:
                resp = requests.post(target, json=payload, timeout=90)
                if target != PUBLIC_RPC:
                    self._charge(part[0][0], len(part))
                if resp.status_code == 429:
                    self._note_429(target, resp.text)
                elif resp.ok:
                    body = resp.json()
                    if isinstance(body, list):
                        by_id = {r.get("id"): r for r in body if isinstance(r, dict)}
                        if len(by_id) == len(part) and all(
                                "error" not in by_id[i] for i in range(len(part))):
                            for i in range(len(part)):
                                out[start + i] = by_id[i].get("result")
                            ok = True
                            self._note_ok(target)
                            self.stats["пачек"] += 1
                            self.stats["запросов_в_пачках"] += len(part)
            except Exception:  # noqa: BLE001
                ok = False
            if not ok:
                for i, (m, p) in enumerate(part):
                    try:
                        out[start + i] = self.call(m, p)
                    except RuntimeError:
                        out[start + i] = None
        return out

    def get_transactions(self, signatures: list[str], **opts) -> list:
        """До 100 getTransaction в одном запросе."""
        o = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1, **opts}
        return self.batch([("getTransaction", [s, o]) for s in signatures],
                           chunk=BATCH_GET_TRANSACTION)

    def note_ws_bytes(self, n_bytes: int) -> int:
        """Учесть трафик подписки WebSocket: 2 кредита за 0.1 МБ."""
        c = credits_for_ws_bytes(n_bytes)
        self.stats["кредитов"] += c
        self.meter.add(c, bytes_in=n_bytes)
        return c


def _next_backoff(prev: float) -> float:
    """1 с, удвоение до 30 с, плюс случайная добавка."""
    base = BACKOFF_START_S if prev <= 0 else min(prev * 2, BACKOFF_CAP_S)
    return base + random.uniform(0, BACKOFF_JITTER_S)


def self_test() -> None:
    checks: list[tuple[str, bool, str]] = []

    def chk(name: str, ok: bool, got: str = "") -> None:
        checks.append((name, bool(ok), got))

    chk("ключ не печатаем, только хвост", key_tail("abcdef1234") == "...1234",
        key_tail("abcdef1234"))
    chk("пустой ключ виден как пустой", key_tail("") == "(пусто)")
    chk("ключ вычищается из текста", scrub("url?api-key=SEKRET", "SEKRET") == "url?api-key=<КЛЮЧ>")

    chk("обычный вызов -- 1 кредит", credits_for("getBlock") == 1)
    chk("getProgramAccounts -- 10", credits_for("getProgramAccounts") == 10)
    chk("расширенный API -- 100", credits_for("x", enhanced=True) == 100)
    chk("пачка считается по числу запросов", credits_for("getTransaction", 100) == 100)
    chk("WebSocket: 2 кредита за 0.1 МБ", credits_for_ws_bytes(100_000) == 2,
        str(credits_for_ws_bytes(100_000)))
    chk("неполная порция считается как полная", credits_for_ws_bytes(1) == 2)
    chk("1 МБ -- 20 кредитов", credits_for_ws_bytes(1_000_000) == 20,
        str(credits_for_ws_bytes(1_000_000)))

    b = 0.0
    seq = []
    for _ in range(7):
        b = _next_backoff(b)
        seq.append(b)
    chk("пауза стартует с 1 с", 1.0 <= seq[0] <= 1.0 + BACKOFF_JITTER_S, str(seq[0]))
    chk("пауза удваивается", seq[1] > seq[0] and seq[2] > seq[1])
    chk("и упирается в 30 с", all(x <= BACKOFF_CAP_S + BACKOFF_JITTER_S for x in seq),
        str(max(seq)))
    chk("добавка случайная, не константа", len({round(x, 6) for x in seq}) == len(seq))

    rl = RateLimiter(50.0)
    t0 = time.monotonic()
    for _ in range(60):
        rl.take()
    dt = time.monotonic() - t0
    chk("ограничитель держит темп", dt >= (60 - 50) / 50.0 * 0.9, f"{dt:.3f}с на 60 вызовов")

    import tempfile  # noqa: PLC0415
    tmp = Path(tempfile.mkdtemp()) / "учёт"
    m = CreditMeter("зонд", tmp)
    svc = m.add(1000, bytes_in=200_000)
    chk("учёт пишет кредиты", svc["кредитов_за_день"] == 1000)
    chk("и байты", svc["байт_за_день"] == 200_000)
    chk("бюджет службы подставлен", svc["бюджет_за_день"] == 150_000)
    # Потолок детектора -- решение владельца 25.09, и он должен быть виден
    # числом: занижение здесь молча режет боевые источники.
    chk("потолок детектора 1 000 000 в сутки",
        DAILY_BUDGET["bloom_detector"] == 1_000_000,
        DAILY_BUDGET["bloom_detector"])
    chk("общий потолок не ниже детекторского",
        ОБЩИЙ_ПОТОЛОК_СУТКИ >= DAILY_BUDGET["bloom_detector"],
        (ОБЩИЙ_ПОТОЛОК_СУТКИ, DAILY_BUDGET["bloom_detector"]))
    chk("порог 70% пока не сработал", svc["выше_70_процентов"] is False)
    m.add(150_000 * 7 // 10 - 1000)
    st = CreditMeter.over_budget("зонд", tmp)
    chk("порог 70% срабатывает", st["выше_70_процентов"] is True, str(st))
    chk("другая служба не задета",
        CreditMeter.over_budget("ledger", tmp)["потрачено"] == 0)

    m2 = CreditMeter("горизонты", tmp)
    m2.add(50_000)
    m3 = CreditMeter("разбор_пилота", tmp)
    svc3 = m3.add(100_000)
    chk("бюджет группы прогонов -- 200 тыс.", svc3["бюджет_за_день"] == 200_000,
        str(svc3["бюджет_за_день"]))
    chk("считается потраченное ВСЕЙ группой", svc3["потрачено_группой"] == 150_000,
        str(svc3["потрачено_группой"]))
    chk("порог 70% от группы, а не от службы", svc3["выше_70_процентов"] is True,
        str(svc3["доля_бюджета"]))
    chk("при пороге НЕ останавливаемся", svc3["останавливать_при_пороге"] is False)
    rep = CreditMeter.report(tmp)
    chk("в отчёте службы видны поимённо",
        set(rep["по_службам"]) >= {"зонд", "горизонты", "разбор_пилота"}, str(list(rep["по_службам"])))
    chk("каждая служба в своём файле, не в общем",
        {x.name for x in usage_shards(tmp)} ==
        {"зонд.json", "горизонты.json", "разбор_пилота.json"},
        str(sorted(x.name for x in usage_shards(tmp))))
    chk("слияние осколков складывает, а не затирает",
        merge_usage(usage_shards(tmp))["дни"][time.strftime("%Y-%m-%d", time.gmtime())]
        ["зонд"]["кредитов_за_день"] == 150_000 * 7 // 10)
    chk("и группа посчитана отдельно", rep["по_группам"]["прогоны"]["потрачено"] == 150_000,
        str(rep["по_группам"]["прогоны"]))
    chk("зонд в группу прогонов не попал", rep["по_службам"]["зонд"]["группа"] is None)

    r = SolanaRpc("прогоны", key="KEY", usage_dir=tmp)
    chk("пока Helius жив -- Helius", r.pick_url().startswith(HELIUS_RPC_HOST))
    for _ in range(DEMOTE_AFTER_429):
        r._note_429(r.helius_url(), '{"error":"max usage reached"}')
    chk("после 3x429 -- публичный", r.pick_url() == PUBLIC_RPC)
    chk("тело первого 429 сохранено",
        "max usage reached" in (r.stats["первый_ответ_429_от_helius"] or ""))
    chk("ключ в сохранённом теле не светится",
        "KEY" not in (r.stats["первый_ответ_429_от_helius"] or "").replace("<КЛЮЧ>", ""))
    r2 = SolanaRpc("прогоны", key="KEY", usage_dir=tmp, allow_public=False)
    for _ in range(DEMOTE_AFTER_429):
        r2._note_429(r2.helius_url(), "x")
    chk("с запретом публичного не уходим на него",
        r2.pick_url().startswith(HELIUS_RPC_HOST))

    chk("пачка getTransaction -- 100", BATCH_GET_TRANSACTION == 100)
    chk("прочие исторические -- 10", BATCH_OTHER == 10)
    chk("темп ниже купленного", NODE_RPS < 50 and ENHANCED_RPS < 10,
        f"{NODE_RPS}/{ENHANCED_RPS}")

    bad = 0
    for name, ok, got in checks:
        print(f"  [{'ok  ' if ok else 'СБОЙ'}] {name}" + (f"  -> {got}" if got and not ok else ""))
        bad += (not ok)
    print(f"самопроверка клиента узла: {len(checks) - bad}/{len(checks)} пройдено")
    if bad:
        raise SystemExit(f"самопроверка не пройдена: {bad} из {len(checks)}")


if __name__ == "__main__":
    import sys  # noqa: PLC0415
    if "--self-test" in sys.argv:
        self_test()
    else:
        print(__doc__)
