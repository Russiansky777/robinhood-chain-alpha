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


def вход_актора(актор: str, адреса: list, с_даты: str, по_даты: str,
                предел: int) -> dict:
    """Вход под схему актора: поля окна у них разные, и это замер, а не догадка.

    kaito...pay-per-result: since_time/until_time -- unix-секунды, queryType Latest.
    apidojo/twitter-scraper-lite и tweet-scraper: start/end -- даты, sort Latest, и есть
    includeSearchTerms -- он помечает пост поисковой строкой, по которой нашёлся.
    """
    если_unix = "kaito" in актор
    вход = {"searchTerms": адреса, "maxItems": предел}
    if если_unix:
        вход["queryType"] = "Latest"
        вход["since_time"] = str(int(time.mktime(time.strptime(с_даты, "%Y-%m-%d"))))
        вход["until_time"] = str(int(time.mktime(time.strptime(по_даты, "%Y-%m-%d"))))
    else:
        вход["sort"] = "Latest"
        вход["start"] = с_даты
        вход["end"] = по_даты
        вход["includeSearchTerms"] = True
    return вход


def вход_точный(актор: str, адрес: str, с_мс: int, по_мс: int, предел: int) -> dict:
    """Вход с окном в СЕКУНДАХ: для «по одному адресу» нужна минутная точность.

    Адрес минта не существует до создания пула, поэтому окно начинается от создания:
    упоминание раньше этого момента физически невозможно (кроме заранее подобранных
    красивых адресов -- таких в выборке не ищем).
    """
    если_unix = "kaito" in актор
    вход = {"searchTerms": [адрес], "maxItems": предел}
    if если_unix:
        вход.update({"queryType": "Latest",
                     "since_time": str(int(с_мс / 1000)),
                     "until_time": str(int(по_мс / 1000))})
    else:
        вход.update({"sort": "Latest",
                     "start": time.strftime("%Y-%m-%d", time.gmtime(с_мс / 1000)),
                     "end": time.strftime("%Y-%m-%d", time.gmtime(по_мс / 1000)),
                     "includeSearchTerms": True})
    return вход


def один_прогон(а, ид: str, вход: dict, т: str) -> tuple:
    """(посты, статус, стоимость) одного прогона актора."""
    пуск = (запрос(f"/acts/{ид}/runs?waitForFinish=60", вход, "POST", т)
            or {}).get("data") or {}
    run_id = пуск.get("id")
    print(f"  прогон {run_id}: {пуск.get('status')}", flush=True)
    нач = time.time()
    сост = пуск
    while сост.get("status") in ("READY", "RUNNING") and time.time() - нач < а.zhdat_s:
        time.sleep(10)
        сост = (запрос(f"/actor-runs/{run_id}", т=т) or {}).get("data") or {}
    посты = []
    дс = сост.get("defaultDatasetId")
    if дс:
        сдвиг = 0
        while True:
            часть = запрос(f"/datasets/{дс}/items?offset={сдвиг}&limit=1000&clean=true",
                           т=т)
            if not isinstance(часть, list) or not часть:
                break
            посты += часть
            сдвиг += len(часть)
            if len(часть) < 1000:
                break
    ст = сост.get("usageTotalUsd")
    print(f"  прогон {run_id}: {сост.get('status')}, постов {len(посты)}, "
          f"стоимость {ст} USD", flush=True)
    return (посты, сост.get("status"), ст or 0.0, run_id)


def сбор(а) -> int:
    выб = json.loads(Path(а.vyborka).read_text(encoding="utf-8"))
    ряды = выб["ряды"][: а.mintov] if а.mintov else выб["ряды"]
    адреса = [р["минт"] for р in ряды]
    с_даты = min(окно_суток(р, а.zapas)[0] for р in ряды)
    по_даты = max(окно_суток(р, а.zapas)[1] for р in ряды)
    # ПАРТИЯМИ, и это про деньги: плата у выбранного актора за РЕЗУЛЬТАТ, а не за прогон,
    # поэтому дробление на партии ничего не добавляет к цене, зато не даёт первым адресам
    # съесть весь предел результатов и оставить остальные без поиска.
    партия = а.partiya or len(адреса)
    пачки = [адреса[i: i + партия] for i in range(0, len(адреса), партия)]
    print(f"актор {а.aktor}: адресов {len(адреса)}, партий {len(пачки)} по {партия}, "
          f"окно {с_даты}..{по_даты}, предел результатов на партию "
          f"{а.predel_rezultatov}", flush=True)
    обр = вход_актора(а.aktor, пачки[0], с_даты, по_даты, а.predel_rezultatov)
    print(f"вход (без токена, образец): "
          f"{json.dumps({**обр, 'searchTerms': f'[{len(пачки[0])} адресов]'}, ensure_ascii=False)[:400]}",
          flush=True)
    потолок = round(а.predel_rezultatov * len(пачки) * а.cena_za_1000 / 1000, 4)
    print(f"потолок расходов по тарифу {а.cena_za_1000} USD/1000: {потолок} USD",
          flush=True)
    if а.suho:
        print("сухой прогон: актор не запускался, денег не тратилось", flush=True)
        return 0

    т = токен()
    ид = а.aktor.replace("/", "~")
    посты: list = []
    стоимость = 0.0
    статусы = []
    прогоны = []
    for н, пачка in enumerate(пачки, 1):
        print(f"партия {н} из {len(пачки)}: адресов {len(пачка)}", flush=True)
        вход = вход_актора(а.aktor, пачка, с_даты, по_даты, а.predel_rezultatov)
        п_, ст_, ц_, run_id = один_прогон(а, ид, вход, т)
        посты += п_
        стоимость += float(ц_ or 0)
        статусы.append(ст_)
        прогоны.append(run_id)
    статус = ",".join(sorted(set(x for x in статусы if x)))
    run_id = ",".join(x for x in прогоны if x)
    print(f"итого: постов {len(посты)}, стоимость {round(стоимость, 4)} USD", flush=True)

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
        "актор": а.aktor, "прогоны": run_id, "статус": статус,
        "партий": len(пачки), "партия": партия,
        "окно_поиска": [с_даты, по_даты], "запас_суток": а.zapas,
        "предел_результатов": а.predel_rezultatov,
        "адресов": len(адреса), "постов_получено": len(посты),
        "постов_без_адреса_в_тексте": без_адреса,
        "нашлось_хоть_что_то": нашлось,
        "покрытие": round(нашлось / len(адреса), 4) if адреса else None,
        "стоимость_usd": стоимость,
        "стоимость_на_токен_usd": (round(стоимость / len(адреса), 5)
                                   if (стоимость and адреса) else None),
        "сырые_посты": посты[: а.syroe] if а.syroe else [],
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


def проба(а) -> int:
    """Три варианта входа на двух адресах по 10 результатов: что актор вообще отдаёт.

    Пилот на 50 адресах вернул 75 постов, и НИ В ОДНОМ не было адреса в тексте -- значит
    либо фильтр окна не понят актором, либо поиск идёт не по тем полям, либо текст лежит
    в другом поле. Это проба, а не вердикт: стоит меньше цента и отвечает числом.
    """
    выб = json.loads(Path(а.vyborka).read_text(encoding="utf-8"))
    адреса = [р["минт"] for р in выб["ряды"][:2]]
    с_даты, по_даты = окно_суток(выб["ряды"][0], а.zapas)
    варианты = [
        ("как в пилоте", {"searchTerms": адреса, "maxItems": 10, "queryType": "Latest",
                          "since_time": str(int(time.mktime(time.strptime(с_даты, "%Y-%m-%d")))),
                          "until_time": str(int(time.mktime(time.strptime(по_даты, "%Y-%m-%d"))))}),
        ("без окна", {"searchTerms": адреса, "maxItems": 10, "queryType": "Latest"}),
        ("строкой запроса", {"twitterContent": f"{адреса[0]} since:{с_даты} until:{по_даты}",
                             "maxItems": 10, "queryType": "Latest"}),
    ]
    т = токен()
    ид = а.aktor.replace("/", "~")
    отчёт = []
    for имя, вход in варианты:
        print(f"== проба «{имя}»: {json.dumps({k: (v if k != 'searchTerms' else f'[{len(адреса)}]') for k, v in вход.items()}, ensure_ascii=False)}",
              flush=True)
        посты, ст, ц, run_id = один_прогон(а, ид, вход, т)
        первый = посты[0] if посты else {}
        текст = " ".join(str(первый.get(к) or "") for к in
                         ("text", "full_text", "fullText", "content", "rawContent"))
        есть_адрес = sum(1 for п_ in посты
                         if any(а_ in json.dumps(п_, ensure_ascii=False) for а_ in адреса))
        print(f"   постов {len(посты)}, с адресом где-либо в посте {есть_адрес}, "
              f"стоимость {ц}", flush=True)
        print(f"   поля первого поста: {sorted(первый.keys())[:25]}", flush=True)
        print(f"   текст первого (200): {текст[:200]!r}", flush=True)
        отчёт.append({"вариант": имя, "вход": {k: v for k, v in вход.items()
                                               if k != "searchTerms"},
                      "адресов": len(адреса), "постов": len(посты),
                      "с_адресом_в_посте": есть_адрес, "стоимость_usd": ц,
                      "прогон": run_id,
                      "поля_первого": sorted(первый.keys()),
                      "сырые": посты[: а.syroe or 2]})
    ф = П / f"x_proba{а.metka}.json"
    ф.write_text(json.dumps({"что": "проба входов актора X", "актор": а.aktor,
                             "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                             "варианты": отчёт}, ensure_ascii=False, indent=1),
                 encoding="utf-8")
    print(f"записано {ф}", flush=True)
    try:
        import sys  # noqa: PLC0415
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import podbivka_run as R  # noqa: PLC0415
        R.записано(ф)
    except Exception:  # noqa: BLE001, S110
        pass
    return 0


def контроль(а) -> int:
    """Контроль: заведомо живой запрос тем же входом -- работает ли сбор вообще.

    Проба на наших адресах вернула 15 служебных записей актора ({id, text, type} с текстом
    «From KaitoEasyAPI, a reminder: ... minimum charge ...») и ни одного твита. Это может
    значить и «постов нет вовсе», и «вход не понят». Различает только контроль: тот же
    вход со словом, у которого твиты есть наверняка, и с двумя окнами -- старым (наши
    сутки) и свежим (последние двое суток).
    """
    слово = а.kontrol or "solana"
    т = токен()
    ид = а.aktor.replace("/", "~")
    сейчас = time.time()
    окна = [("старое окно (сутки выборки)", а.okno_s, а.okno_po),
            ("свежее окно (двое суток)",
             time.strftime("%Y-%m-%d", time.gmtime(сейчас - 2 * 86400)),
             time.strftime("%Y-%m-%d", time.gmtime(сейчас + 86400)))]
    отчёт = []
    for имя, с_, по_ in окна:
        вход = вход_актора(а.aktor, [слово], с_, по_, 10)
        print(f"== контроль «{слово}», {имя}: {с_}..{по_}", flush=True)
        посты, ст, ц, run_id = один_прогон(а, ид, вход, т)
        первый = посты[0] if посты else {}
        текст = " ".join(str(первый.get(к) or "") for к in
                         ("text", "full_text", "fullText", "content", "rawContent"))
        служебных = sum(1 for п_ in посты
                        if "KaitoEasyAPI" in json.dumps(п_, ensure_ascii=False)
                        or set(п_.keys()) <= {"id", "text", "type"})
        print(f"   постов {len(посты)}, служебных записей {служебных}, "
              f"стоимость {ц}", flush=True)
        print(f"   поля первого: {sorted(первый.keys())[:20]}", flush=True)
        print(f"   текст первого (160): {текст[:160]!r}", flush=True)
        отчёт.append({"окно": имя, "с": с_, "по": по_, "слово": слово,
                      "постов": len(посты), "служебных": служебных,
                      "стоимость_usd": ц, "прогон": run_id,
                      "поля_первого": sorted(первый.keys()),
                      "сырые": посты[: а.syroe or 2]})
    ф = П / f"x_kontrol{а.metka}.json"
    ф.write_text(json.dumps({"что": "контроль сбора X на заведомо живом запросе",
                             "актор": а.aktor, "слово": слово,
                             "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                             "окна": отчёт}, ensure_ascii=False, indent=1),
                 encoding="utf-8")
    print(f"записано {ф}", flush=True)
    try:
        import sys  # noqa: PLC0415
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import podbivka_run as R  # noqa: PLC0415
        R.записано(ф)
    except Exception:  # noqa: BLE001, S110
        pass
    return 0


def по_одному(а) -> int:
    """Один адрес -- один прогон, окно от создания до +N минут, с денежным стопом.

    Зачем так. Поиск отдаёт результаты сортировкой Latest, то есть при общем прогоне на
    десять адресов один болтливый адрес съедает весь предел, а у остальных не остаётся
    ничего. Отдельный прогон на адрес это снимает, а узкое окно (создание ... +N минут)
    делает ЛЮБОЙ найденный пост ранним: значит минимум из найденных -- это верхняя оценка
    времени первого упоминания, а не «самый свежий из множества».

    Расходы ограничены дважды: предел результатов на прогон и ПОТОЛОК В ДОЛЛАРАХ на весь
    проход -- по его достижении проход останавливается и говорит, сколько адресов успел.
    """
    выб = json.loads(Path(а.vyborka).read_text(encoding="utf-8"))
    ряды = выб["ряды"][а.s: (а.s + а.mintov) if а.mintov else None]
    т = токен()
    ид = а.aktor.replace("/", "~")
    итог = []
    стоимость = 0.0
    остановлен = None
    нашлось = 0
    for н, р in enumerate(ряды, 1):
        if а.potolok_usd and стоимость >= а.potolok_usd:
            остановлен = f"потолок {а.potolok_usd} USD достигнут на {н - 1} адресах"
            print(f"СТОП: {остановлен}", flush=True)
            break
        с_мс = int(р["ts"])
        по_мс = с_мс + int(а.okno_minut * 60_000)
        вход = вход_точный(а.aktor, р["минт"], с_мс, по_мс, а.predel_rezultatov)
        посты, ст, ц, run_id = один_прогон(а, ид, вход, т)
        стоимость += float(ц or 0)
        # служебные записи актора -- не посты: {id, text, type} с его же напоминанием
        твиты = [п_ for п_ in посты
                 if not (set(п_.keys()) <= {"id", "text", "type"}
                         or "KaitoEasyAPI" in json.dumps(п_, ensure_ascii=False))]
        свои = [п_ for п_ in твиты
                if р["минт"] in json.dumps(п_, ensure_ascii=False)]
        метки = sorted(x for x in (_метка_поста(п_) for п_ in свои) if x)
        if свои:
            нашлось += 1
        итог.append({
            "минт": р["минт"], "группа": р["группа"], "сутки": р["сутки"],
            "создание_utc": р.get("utc"), "создание_ts": с_мс,
            "оборот_60с_sol": р.get("оборот_60с_sol"),
            "окно_минут": а.okno_minut,
            "постов_в_окне": len(свои), "твитов_получено": len(твиты),
            "предел_уперся": len(твиты) >= а.predel_rezultatov,
            "первый_пост_utc": метки[0] if метки else None,
            "последний_в_окне_utc": метки[-1] if метки else None,
            "подписчиков_автора": (_подписчики(min(свои, key=lambda x: _метка_поста(x) or "9"))
                                   if метки else None),
            "прогон": run_id, "стоимость_usd": ц,
        })
        print(f"  {н}/{len(ряды)} {р['минт'][:10]}… постов {len(свои)} "
              f"первый {метки[0] if метки else '—'} стоимость всего "
              f"{round(стоимость, 4)}", flush=True)
    тело = {
        "что": "первые упоминания в X, по одному адресу на прогон",
        "когда": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "актор": а.aktor, "выборка": Path(а.vyborka).name,
        "окно_минут_от_создания": а.okno_minut,
        "предел_результатов_на_адрес": а.predel_rezultatov,
        "потолок_usd": а.potolok_usd, "остановлен": остановлен,
        "адресов_пройдено": len(итог), "нашлось_хоть_что_то": нашлось,
        "покрытие": round(нашлось / len(итог), 4) if итог else None,
        "стоимость_usd": round(стоимость, 4),
        "стоимость_на_токен_usd": (round(стоимость / len(итог), 5) if итог else None),
        "оговорка": "окно от создания до +N минут: любой найденный пост ранний, а минимум "
                    "из найденных -- верхняя оценка времени первого упоминания; "
                    "удалённые посты и неполнота поиска X смещают к выжившим",
        "ряды": итог,
    }
    ф = П / f"x_upominania{а.metka}.json"
    ф.write_text(json.dumps(тело, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"пройдено {len(итог)}, покрытие {тело['покрытие']}, "
          f"стоимость {тело['стоимость_usd']} USD "
          f"({тело['стоимость_на_токен_usd']} на токен); записано {ф}", flush=True)
    try:
        import sys  # noqa: PLC0415
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import podbivka_run as R  # noqa: PLC0415
        R.записано(ф)
    except Exception:  # noqa: BLE001, S110
        pass
    return 0


def _метка_поста(п_: dict):
    for к in ("createdAt", "created_at", "date", "timestamp"):
        v = п_.get(к)
        if v:
            return str(v)
    return None


def _подписчики(п_: dict):
    for путь in (("author", "followers"), ("author", "followersCount"),
                 ("user", "followers_count")):
        z = п_
        for к in путь:
            z = (z or {}).get(к) if isinstance(z, dict) else None
        if isinstance(z, (int, float)):
            return z
    return None


def главное(а) -> int:
    if а.spisok:
        return магазин(а.poisk, а.predel_spiska)
    if а.kontrol_rezhim:
        if not а.aktor:
            raise SystemExit("для контроля нужен --aktor")
        return контроль(а)
    if а.proba:
        if not а.aktor:
            raise SystemExit("для пробы нужен --aktor")
        return проба(а)
    if а.shema:
        return схема(а.shema)
    if not а.aktor:
        raise SystemExit("для сбора нужен --aktor (см. --spisok и --shema)")
    if а.po_odnomu:
        return по_одному(а)
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
    р.add_argument("--predel-rezultatov", type=int, default=100,
                   help="maxItems на партию -- он же предел расходов")
    р.add_argument("--partiya", type=int, default=10,
                   help="адресов в одной партии (0 -- все одной)")
    р.add_argument("--cena-za-1000", type=float, default=0.18,
                   help="тариф актора за 1000 результатов, для потолка расходов")
    р.add_argument("--vhod-shablon", default="", help="json со своим входом актора")
    р.add_argument("--vhod-dopolnjat", action="store_true",
                   help="класть наши поля в шаблон, даже если их там нет")
    р.add_argument("--zhdat-s", type=int, default=1500, help="сколько ждать прогон")
    р.add_argument("--suho", action="store_true", help="показать вход и не запускать")
    р.add_argument("--syroe", type=int, default=0,
                   help="сохранить N сырых постов в выход -- для разбора полей")
    р.add_argument("--kontrol-rezhim", action="store_true",
                   help="контроль: заведомо живой запрос тем же входом")
    р.add_argument("--kontrol", default="solana", help="слово контроля")
    р.add_argument("--okno-s", default="2026-09-20")
    р.add_argument("--okno-po", default="2026-09-26")
    р.add_argument("--proba", action="store_true",
                   help="проба входов: три варианта окна на двух адресах по 10 "
                        "результатов -- чтобы понять, почему привязка не нашлась")
    р.add_argument("--po-odnomu", action="store_true",
                   help="один адрес -- один прогон, окно от создания")
    р.add_argument("--okno-minut", type=float, default=15.0,
                   help="окно от создания, минут (режим --po-odnomu)")
    р.add_argument("--potolok-usd", type=float, default=0.0,
                   help="денежный потолок на весь проход (0 -- без потолка)")
    р.add_argument("--s", type=int, default=0, help="начать с N-го ряда выборки")
    р.add_argument("--metka", default="")
    return главное(р.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
