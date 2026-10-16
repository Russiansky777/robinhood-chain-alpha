#!/usr/bin/env python3
"""Первое упоминание токена в X (Twitter) через Apify: сбор по адресам контрактов.

Зачем. Проверка «предсказывает ли первое упоминание в X путь цены и приходит ли оно раньше
пика». Сбор -- готовым актором поиска X из магазина Apify; тариф владельца оплачен, токен
приходит переменной окружения APIFY_TOKEN (в журнал не печатается никогда, в URL не
подставляется -- только заголовком Authorization).

Три режима, и первый ничего не тратит:
  --spisok            перечень акторов поиска X из магазина с их ценой (только чтение);
  --shema <актор>     схема входа и пример входа этого актора (только чтение);
  --sbor              собственно сбор: ОДИН прогон актора на все адреса выборки
                      (searchTerms), окно since/until = сутки токена ± 1, предел
                      результатов задан флагом -- это и есть предел расходов.

Почему один прогон на все адреса: у акторов поиска плата идёт за результат либо за время,
и 50 отдельных прогонов стоят кратно дороже одного с 50 поисковыми строками. Привязка
обратно к токену -- по вхождению адреса в текст поста.

На токен пишется: время первого поста с адресом, число постов в первый час после него,
подписчики автора первого поста, сколько постов всего нашлось. Рядом -- стоимость прогона
по данным Apify (usageTotalUsd) и стоимость на токен.

Выход: data/podbivka/x_upominania<метка>.json. Своих ключей не хранит, на диск не кладёт.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
БАЗА = "https://api.apify.com/v2"
ТАЙМАУТ = 120


def токен(нужен: bool = True) -> str:
    """Токен из окружения. В журнал уходит только ДЛИНА -- значение никогда."""
    т = os.environ.get("APIFY_TOKEN") or ""
    print(f"APIFY_TOKEN: {'есть, знаков ' + str(len(т)) if т else 'НЕТ в окружении'}",
          flush=True)
    if нужен and not т:
        raise SystemExit("нет APIFY_TOKEN: прогон должен давать его секретом репозитория")
    return т


def запрос(путь: str, тело=None, метод: str = "GET", т: str = "",
           нужен_токен: bool = True) -> dict:
    """Запрос к Apify. Токен идёт ЗАГОЛОВКОМ -- в URL его быть не должно.

    Магазин и метаданные актора читаются и без токена: перечень акторов не должен падать
    из-за ненастроенного секрета -- иначе не узнать даже, что выбирать.
    """
    url = путь if путь.startswith("http") else f"{БАЗА}{путь}"
    данные = json.dumps(тело).encode("utf-8") if тело is not None else None
    зап = urllib.request.Request(url, data=данные, method=метод)
    ключ = т if т else os.environ.get("APIFY_TOKEN") or ""
    if ключ:
        зап.add_header("Authorization", f"Bearer {ключ}")
    elif нужен_токен:
        raise SystemExit("нет APIFY_TOKEN: прогон должен давать его секретом репозитория")
    зап.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(зап, timeout=ТАЙМАУТ) as о:
            сырое = о.read()
    except urllib.error.HTTPError as e:
        тело_ош = e.read()[:400].decode("utf-8", "replace")
        raise SystemExit(f"Apify {метод} {url.split('?')[0]}: http {e.code}: {тело_ош}")
    except urllib.error.URLError as e:
        # Из контейнера подбивки api.apify.com закрыт сетевой политикой (403 на CONNECT):
        # сбор идёт заданием Actions, а не здесь. Пусть это будет сказано словами.
        raise SystemExit(f"сеть до {url.split('/v2')[0]} закрыта: {e.reason}; "
                         f"этот проход надо гонять заданием Actions")
    return json.loads(сырое.decode("utf-8", "replace")) if сырое else {}


def магазин(поиск: str, предел: int) -> int:
    """Перечень акторов магазина -- что есть и сколько стоит. Денег не тратит."""
    токен(нужен=False)      # печатает только наличие и длину
    д = запрос(f"/store?search={urllib.parse.quote(поиск)}&limit={предел}",
               нужен_токен=False)
    ряды = (д.get("data") or {}).get("items") or []
    print(f"найдено акторов: {len(ряды)}", flush=True)
    из_ = []
    for а in ряды:
        ц = а.get("currentPricingInfo") or {}
        из_.append({
            "актор": f"{(а.get('username') or '')}/{(а.get('name') or '')}",
            "id": а.get("id"), "название": (а.get("title") or "")[:70],
            "тариф": ц.get("pricingModel"),
            "цена_за_1000": ц.get("pricePerUnitUsd") or ц.get("unitPriceUsd"),
            "за_что": ц.get("unitName") or ц.get("apifyChargePerUnitUsd"),
            "пользователей": а.get("stats", {}).get("totalUsers"),
            "прогонов_30д": а.get("stats", {}).get("totalRuns"),
        })
        print("  %-42s %-14s %-10s польз. %s" % (
            из_[-1]["актор"][:42], str(из_[-1]["тариф"])[:14],
            str(из_[-1]["цена_за_1000"])[:10], из_[-1]["пользователей"]), flush=True)
    (П / "x_aktory.json").write_text(
        json.dumps({"что": "акторы поиска X в магазине Apify", "поиск": поиск,
                    "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "акторы": из_}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"записано {П / 'x_aktory.json'}", flush=True)
    return 0


def схема(актор: str) -> int:
    """Схема входа актора: по ней собирается вход сбора. Денег не тратит."""
    ид = актор.replace("/", "~")
    токен(нужен=False)
    мета = (запрос(f"/acts/{ид}", нужен_токен=False) or {}).get("data") or {}
    сб = (запрос(f"/acts/{ид}/builds/default", нужен_токен=False) or {}).get("data") or {}
    сх = сб.get("inputSchema")
    if isinstance(сх, str):
        try:
            сх = json.loads(сх)
        except ValueError:
            pass
    тело = {"актор": актор, "название": мета.get("title"),
            "тариф": мета.get("currentPricingInfo"),
            "пример_входа": мета.get("exampleRunInput"),
            "поля_входа": (list((сх or {}).get("properties", {}).keys())
                           if isinstance(сх, dict) else None),
            "схема": сх}
    ф = П / f"x_shema_{актор.replace('/', '_')}.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"актор {актор}: {мета.get('title')}", flush=True)
    print(f"тариф: {json.dumps(тело['тариф'], ensure_ascii=False)[:300]}", flush=True)
    print(f"поля входа: {тело['поля_входа']}", flush=True)
    print(f"записано {ф}", flush=True)
    return 0


def окно_суток(ряд: dict, запас_суток: int) -> tuple:
    """since/until = сутки токена ± запас, как заказано."""
    т = (ряд.get("ts") or 0) / 1000
    с = time.strftime("%Y-%m-%d", time.gmtime(т - 86400 * запас_суток))
    по = time.strftime("%Y-%m-%d", time.gmtime(т + 86400 * (запас_суток + 1)))
    return (с, по)


def сбор(а) -> int:
    выб = json.loads(Path(а.vyborka).read_text(encoding="utf-8"))
    ряды = выб["ряды"][: а.mintov] if а.mintov else выб["ряды"]
    адреса = [р["минт"] for р in ряды]
    с_даты = min(окно_суток(р, а.zapas)[0] for р in ряды)
    по_даты = max(окно_суток(р, а.zapas)[1] for р in ряды)
    вход = {"searchTerms": адреса, "maxItems": а.predel_rezultatov,
            "sort": "Latest", "start": с_даты, "end": по_даты,
            "tweetLanguage": None}
    if а.vhod_shablon:
        шаб = json.loads(Path(а.vhod_shablon).read_text(encoding="utf-8"))
        шаб.update({к: v for к, v in вход.items() if к in шаб or а.vhod_dopolnjat})
        вход = шаб
    вход = {к: v for к, v in вход.items() if v is not None}
    print(f"актор {а.aktor}: адресов {len(адреса)}, окно {с_даты}..{по_даты}, "
          f"предел результатов {а.predel_rezultatov}", flush=True)
    print(f"вход (без токена): {json.dumps({**вход, 'searchTerms': f'[{len(адреса)} адресов]'}, ensure_ascii=False)[:400]}",
          flush=True)
    if а.suho:
        print("сухой прогон: актор не запускался, денег не тратилось", flush=True)
        return 0

    т = токен()
    ид = а.aktor.replace("/", "~")
    пуск = (запрос(f"/acts/{ид}/runs?waitForFinish=60", вход, "POST", т) or {}).get("data") or {}
    run_id = пуск.get("id")
    print(f"прогон {run_id} пущен, состояние {пуск.get('status')}", flush=True)
    нач = time.time()
    сост = пуск
    while сост.get("status") in ("READY", "RUNNING") and time.time() - нач < а.zhdat_s:
        time.sleep(10)
        сост = (запрос(f"/actor-runs/{run_id}", т=т) or {}).get("data") or {}
        print(f"  {int(time.time() - нач)} с: {сост.get('status')}", flush=True)
    статус = сост.get("status")
    стоимость = сост.get("usageTotalUsd")
    посты = []
    дс = сост.get("defaultDatasetId")
    if дс:
        сдвиг = 0
        while True:
            часть = запрос(f"/datasets/{дс}/items?offset={сдвиг}&limit=1000&clean=true", т=т)
            if not isinstance(часть, list) or not часть:
                break
            посты += часть
            сдвиг += len(часть)
            if len(часть) < 1000:
                break
    print(f"статус {статус}, постов {len(посты)}, стоимость {стоимость} USD", flush=True)

    # привязка обратно к токену -- по вхождению адреса в текст поста
    по_адресу: dict = {а_: [] for а_ in адреса}
    без_адреса = 0
    for п_ in посты:
        текст = " ".join(str(п_.get(к) or "") for к in
                         ("text", "full_text", "fullText", "content", "rawContent"))
        нашёлся = False
        for а_ in адреса:
            if а_ in текст:
                по_адресу[а_].append(п_)
                нашёлся = True
        if not нашёлся:
            без_адреса += 1

    def метка(п_):
        for к in ("createdAt", "created_at", "date", "timestamp"):
            v = п_.get(к)
            if v:
                return str(v)
        return ""

    def подписчики(п_):
        for путь in (("author", "followers"), ("author", "followersCount"),
                     ("user", "followers_count"), ("author", "favouritesCount")):
            z = п_
            for к in путь:
                z = (z or {}).get(к) if isinstance(z, dict) else None
            if isinstance(z, (int, float)):
                return z
        return None

    из_ = []
    нашлось = 0
    for р in ряды:
        сп = по_адресу.get(р["минт"]) or []
        сп = sorted(сп, key=метка)
        первый = сп[0] if сп else None
        час = 0
        if первый:
            нашлось += 1
            т0 = метка(первый)
            час = sum(1 for п_ in сп if т0 <= метка(п_) <= _плюс_час(т0))
        из_.append({
            "минт": р["минт"], "группа": р["группа"], "сутки": р["сутки"],
            "создание_utc": р.get("utc"), "создание_ts": р.get("ts"),
            "постов_всего": len(сп),
            "первый_пост_utc": метка(первый) if первый else None,
            "постов_в_первый_час": час,
            "подписчиков_автора": подписчики(первый) if первый else None,
            "ссылка_первого": (первый.get("url") or первый.get("twitterUrl")
                               if первый else None),
        })
    тело = {
        "что": "первые упоминания адресов токенов в X через Apify",
        "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "актор": а.aktor, "прогон": run_id, "статус": статус,
        "окно_поиска": [с_даты, по_даты], "запас_суток": а.zapas,
        "предел_результатов": а.predel_rezultatov,
        "адресов": len(адреса), "постов_получено": len(посты),
        "постов_без_адреса_в_тексте": без_адреса,
        "нашлось_хоть_что_то": нашлось,
        "покрытие": round(нашлось / len(адреса), 4) if адреса else None,
        "стоимость_usd": стоимость,
        "стоимость_на_токен_usd": (round(стоимость / len(адреса), 5)
                                   if (стоимость and адреса) else None),
        "оговорка": "удалённые посты и неполнота поиска X выборку смещают к выжившим: "
                    "покрытие и доля «пост раньше пика» -- нижние границы",
        "ряды": из_,
    }
    ф = П / f"x_upominania{а.metka}.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"покрытие {тело['покрытие']}, стоимость на токен "
          f"{тело['стоимость_на_токен_usd']} USD; записано {ф}", flush=True)
    try:
        import sys  # noqa: PLC0415
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import podbivka_run as R  # noqa: PLC0415
        R.записано(ф)
    except Exception:  # noqa: BLE001, S110
        pass
    return 0


def _плюс_час(метка_: str) -> str:
    """Та же метка плюс час -- строкой, лексикографически сравнимой с ISO."""
    for ф in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%a %b %d %H:%M:%S %z %Y"):
        try:
            т = time.strptime(метка_[:26] if "." in метка_ else метка_, ф)
        except ValueError:
            continue
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.mktime(т) + 3600))
    return "9999"


def главное(а) -> int:
    if а.spisok:
        return магазин(а.poisk, а.predel_spiska)
    if а.shema:
        return схема(а.shema)
    if not а.aktor:
        raise SystemExit("для сбора нужен --aktor (см. --spisok и --shema)")
    return сбор(а)


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--spisok", action="store_true", help="перечень акторов магазина")
    р.add_argument("--poisk", default="twitter scraper")
    р.add_argument("--predel-spiska", type=int, default=30)
    р.add_argument("--shema", default="", help="актор вида owner/name: схема входа")
    р.add_argument("--aktor", default="", help="актор для сбора, owner/name")
    р.add_argument("--vyborka", default=str(П / "x_vyborka_pilot50.json"))
    р.add_argument("--mintov", type=int, default=0, help="взять первые N из выборки")
    р.add_argument("--zapas", type=int, default=1, help="сутки ± N")
    р.add_argument("--predel-rezultatov", type=int, default=500,
                   help="maxItems -- он же предел расходов")
    р.add_argument("--vhod-shablon", default="", help="json со своим входом актора")
    р.add_argument("--vhod-dopolnjat", action="store_true",
                   help="класть наши поля в шаблон, даже если их там нет")
    р.add_argument("--zhdat-s", type=int, default=1500, help="сколько ждать прогон")
    р.add_argument("--suho", action="store_true", help="показать вход и не запускать")
    р.add_argument("--metka", default="")
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
