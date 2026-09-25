#!/usr/bin/env python3
"""Проба ключей отправителей: какую схему ждёт сервер -- и принимает ли ключ.

Владелец 25.09: "Секреты в GitHub, имена ровно такие: NOZOMI_API_KEY,
ASTRALANE_API_KEY, BLOCKRAZOR_AUTH_TOKEN... BlockRazor: проверь, какую схему
сервер ждёт для этого токена (заголовок apikey или auth token). Если ответ
'нужен платный план' -- доложи текст ответа, не покупать... Если ключ не
принимается -- сообщи, какой и с каким ответом сервера."

КАК ЭТО МОЖНО СПРАШИВАТЬ БЕЗ ДЕНЕГ. Сервер отвечает на запрос ДО того, как
что-то попадёт в цепь: сначала он смотрит на ключ, и только потом на тело. Мы
посылаем тело, которое транзакцией стать не может НИКОГДА: строка с символами
вне алфавитов base64 и base58. Если сервер жалуется на ключ -- ключ не принят;
если он жалуется на транзакцию -- ключ принят, а дальше он уже упёрся в
заведомо битое тело. Ни одного лампорта при этом не двигается, и подписи у
пробы нет вовсе (нечего подписывать).

ЧЕГО ЗДЕСЬ НЕТ.
  * Значений ключей в выводе. Каждый ответ сервера прогоняется через
    вычистить(): значение ключа заменяется на ***, и самопроверка это
    проверяет. Правило владельца по ключу Bloom -- "в чат, логи, коммиты,
    тела запросов в журнале -- никогда" -- действует и здесь.
  * Покупок платных планов. Ответ "нужен платный план" только докладывается
    словами сервера.
  * Догадок про адрес. Точка входа берётся из реестра data/senders.json, то
    есть из сохранённой документации. Нет адреса (Nozomi) -- проба не идёт.
"""
from __future__ import annotations

import json
import os
import sys
import time

try:
    import bloom_senders as SND
except ImportError:  # модуль зовут и из корня репозитория
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
    import bloom_senders as SND

# ТЕЛО ПРОБЫ. '!' нет ни в base64, ни в base58 -- значит ни один сервер не
# сможет превратить эту строку в транзакцию, даже если очень захочет.
ПРОБА_TX = "ETO_NE_TRANZAKTSIYA!!!"

АЛФАВИТ_BASE64 = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                      "0123456789+/=")
АЛФАВИТ_BASE58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
                      "abcdefghijkmnopqrstuvwxyz")

# СХЕМЫ ПОДСТАНОВКИ КЛЮЧА. Ровно те, которые встречаются в документации
# кандидатов: заголовок apikey (пример gRPC BlockRazor: meta.add('apikey',
# authKey)), заголовок Authorization с и без Bearer, x-api-key, и ключ в
# строке запроса (api-key у Astralane и 0slot, c у Nozomi).
СХЕМЫ = (
    "header:apikey",
    "header:Authorization",
    "header:Authorization:Bearer",
    "header:x-api-key",
    "query:apikey",
    "query:api-key",
    # Nozomi: "Path /?c=<YOUR_API_KEY>" -- его документация и слово владельца.
    "query:c",
    "БЕЗ КЛЮЧА",
)

# СЛОВА ОТВЕТА. Разделяем три разных ответа, потому что делать с ними надо
# разное: ключ не принят -- чинить схему или просить у владельца новый;
# платный план -- доложить и НЕ покупать; жалоба на транзакцию -- ключ принят.
СЛОВА_КЛЮЧА = ("unauthorized", "authentication", "authorization",
                "api key", "apikey", "api-key", "auth token", "authtoken",
                "invalid key", "invalid token", "no token", "missing token",
                "forbidden", "permission denied", "not authorized",
                "access denied", "invalid apikey", "token is required",
                "bad key", "wrong key", "invalid auth", "missing key",
                "key required", "key not found", "expired key")
СЛОВА_ПЛАНА = ("subscription", "upgrade your", "paid plan", "purchase",
                "billing", "payment required", "buy a plan", "plan required",
                "insufficient balance", "no active plan", "trial expired")
# Слова про ТЕЛО. Сюда входит и просто "transaction": BlockRazor на принятый
# ключ отвечает "rpc error: code = Unknown desc = illegal transaction", то есть
# ровно жалобой на тело, и узкий список это пропускал (поймано живой пробой
# 25.09). Опасность обратная -- сервер повторяет в отказе свой путь
# /sendTransaction; она снята порядком в вердикт_ответа: слова про КЛЮЧ
# сильнее слов про тело.
СЛОВА_ТРАНЗАКЦИИ = ("transaction", "base64", "base58", "decode", "deserial",
                     "encoding", "signature verification", "sanitize",
                     "invalid param", "invalid request", "parse error",
                     "failed to parse", "malformed")

ВЕРДИКТ_ПРИНЯТ = "ключ принят: сервер жалуется на тело пробы, не на ключ"
ВЕРДИКТ_НЕ_ПРИНЯТ = "ключ НЕ принят: сервер жалуется на ключ"
ВЕРДИКТ_ПЛАН = "нужен платный план (не покупать, доложить владельцу)"
ВЕРДИКТ_НЕПОНЯТНО = "непонятно: ответ не про ключ и не про тело"
ВЕРДИКТ_СЕТЬ = "до сервера не дошли"


def проба_безопасна(проба: str = ПРОБА_TX) -> dict:
    """Проба НЕ МОЖЕТ быть транзакцией. Это не рассуждение, а проверка: в
    строке обязан быть символ вне обоих алфавитов кодирования."""
    вне64 = [с for с in проба if с not in АЛФАВИТ_BASE64]
    вне58 = [с for с in проба if с not in АЛФАВИТ_BASE58]
    общие = [с for с in вне64 if с in вне58]
    return {"ok": bool(общие), "outside_both": "".join(sorted(set(общие))),
            "why_not": None if общие else
            "проба состоит только из символов base64 или base58 -- "
            "теоретически её можно принять за транзакцию"}


def вычистить(текст, ключи) -> str:
    """Значение ключа наружу не выходит ни в каком виде.

    Вычищаются и регистровые варианты: сервер может вернуть ключ приведённым
    к нижнему или верхнему регистру, и тогда прямая замена его пропустит.
    """
    из_ = "" if текст is None else str(текст)
    for к in ключи:
        if not к or len(str(к)) < 4:
            continue
        for вар in (str(к), str(к).lower(), str(к).upper()):
            из_ = из_.replace(вар, "***")
    return из_


def запрос_схемы(имя: str, схема: str, ключ: str | None, *,
                  проба: str = ПРОБА_TX, путь: str | None = None) -> dict:
    """Адрес, тело и заголовки для одной схемы. Тело -- формата сервиса из
    реестра: спрашивать чужой сервер чужим протоколом бессмысленно."""
    з = (SND.реестр(путь) or {}).get(имя) or {}
    url = з.get("url") or ""
    из_ = {"sender": имя, "scheme": схема, "url": url, "headers": {},
            "body": None, "why_not": None}
    if not url:
        из_["why_not"] = (з.get("url_note")
                           or f"у {имя} в реестре нет адреса точки входа")
        return из_
    заголовки = {"Content-Type": "application/json"}
    if схема != "БЕЗ КЛЮЧА":
        if not ключ:
            из_["why_not"] = "секрет не задан в окружении"
            return из_
        часть = схема.split(":")
        if часть[0] == "header":
            значение = f"{часть[2]} {ключ}" if len(часть) > 2 else ключ
            заголовки[часть[1]] = значение
        elif часть[0] == "query":
            соединитель = "&" if "?" in url else "?"
            url = f"{url}{соединитель}{часть[1]}={ключ}"
        else:
            из_["why_not"] = f"схема {схема} не поддержана"
            return из_
    формат = з.get("format") or SND.ФОРМАТ_JSONRPC
    if формат == SND.ФОРМАТ_BLOCKRAZOR:
        тело = {"transaction": проба, "mode": "fast"}
    else:
        тело = {"jsonrpc": "2.0", "id": "проба", "method": "sendTransaction",
                 "params": [проба, {"encoding": "base64",
                                     "skipPreflight": True, "maxRetries": 0}]}
    из_.update(url=url, headers=заголовки, body=тело)
    return из_


def вердикт_ответа(код, текст: str) -> str:
    """Что сервер сказал про ключ. Порядок важен: платный план -- частный
    случай отказа, и путать его с битой схемой нельзя."""
    низ = (текст or "").lower()
    if any(с in низ for с in СЛОВА_ПЛАНА):
        return ВЕРДИКТ_ПЛАН
    про_ключ = any(с in низ for с in СЛОВА_КЛЮЧА)
    про_тело = any(с in низ for с in СЛОВА_ТРАНЗАКЦИИ)
    # ПОРЯДОК: слова про ключ сильнее. Сервер часто повторяет в отказе свой
    # путь (/sendTransaction) или имя метода, и без этого порядка отказ по
    # ключу читался бы как жалоба на тело. Наоборот не бывает: жалуясь на
    # тело, сервер не пишет "authentication missing".
    if про_ключ:
        return ВЕРДИКТ_НЕ_ПРИНЯТ
    if про_тело:
        return ВЕРДИКТ_ПРИНЯТ
    if код in (401, 402, 403):
        return ВЕРДИКТ_НЕ_ПРИНЯТ
    if код == 200:
        # 200 на заведомо битое тело -- редкость, но это точно не отказ по
        # ключу: сервер нас впустил.
        return ВЕРДИКТ_ПРИНЯТ
    return ВЕРДИКТ_НЕПОНЯТНО


def одна_проба(имя: str, схема: str, ключ: str | None, *, отправитель=None,
                таймаут: float = 8.0, путь: str | None = None,
                проба: str = ПРОБА_TX) -> dict:
    зп = запрос_схемы(имя, схема, ключ, проба=проба, путь=путь)
    из_ = {"sender": имя, "scheme": схема, "http": None, "ms": None,
            "verdict": None, "server_text": None,
            # Адрес -- БЕЗ строки запроса: ключ мог уехать именно туда.
            "url": (зп["url"].split("?")[0] if зп.get("url") else None),
            "why_not": зп.get("why_not")}
    if зп.get("why_not"):
        из_["verdict"] = ВЕРДИКТ_СЕТЬ
        return из_
    б = проба_безопасна(проба)
    if not б["ok"]:
        из_.update(verdict=ВЕРДИКТ_СЕТЬ, why_not=б["why_not"])
        return из_
    if отправитель is None:
        def отправитель(адрес, данные, заг, таймаут_):  # noqa: E306
            import requests  # noqa: PLC0415

            r = requests.post(адрес, json=данные, headers=заг,
                              timeout=таймаут_)
            return r.status_code, r.text
    t0 = time.perf_counter()
    try:
        код, текст = отправитель(зп["url"], зп["body"], зп["headers"], таймаут)
    except Exception as exc:  # noqa: BLE001
        из_["ms"] = round((time.perf_counter() - t0) * 1000, 2)
        из_.update(verdict=ВЕРДИКТ_СЕТЬ,
                    why_not=вычистить(f"{type(exc).__name__}: {exc}", [ключ])[:200])
        return из_
    из_["ms"] = round((time.perf_counter() - t0) * 1000, 2)
    из_["http"] = код
    из_["server_text"] = вычистить(текст, [ключ])[:400]
    из_["verdict"] = вердикт_ответа(код, из_["server_text"])
    return из_


def пробы_сервиса(имя: str, *, схемы=СХЕМЫ, отправитель=None,
                   пауза: float = 0.4, путь: str | None = None,
                   окружение=None) -> dict:
    """Все схемы по одному сервису. Пауза между запросами -- потому что у
    BlockRazor в документации предел 3 запроса в секунду."""
    окр = os.environ if окружение is None else окружение
    з = (SND.реестр(путь) or {}).get(имя) or {}
    имя_секрета = з.get("key_env")
    ключ = (окр.get(имя_секрета) or "").strip() if имя_секрета else None
    из_ = {"sender": имя, "key_env": имя_секрета,
            "key_present": bool(ключ), "rows": [],
            "accepted_scheme": None, "plan_required": False,
            "url": з.get("url"), "why_not": None}
    if not з:
        из_["why_not"] = f"отправителя {имя} нет в реестре"
        return из_
    if not з.get("url"):
        из_["why_not"] = (з.get("url_note")
                           or "в реестре нет адреса точки входа")
        return из_
    for i, схема in enumerate(схемы):
        if схема != "БЕЗ КЛЮЧА" and not ключ:
            continue
        стр = одна_проба(имя, схема, ключ, отправитель=отправитель,
                          путь=путь)
        из_["rows"].append(стр)
        if стр["verdict"] == ВЕРДИКТ_ПЛАН:
            из_["plan_required"] = True
        if (стр["verdict"] == ВЕРДИКТ_ПРИНЯТ and схема != "БЕЗ КЛЮЧА"
                and из_["accepted_scheme"] is None):
            из_["accepted_scheme"] = схема
        if пауза and i + 1 < len(схемы):
            time.sleep(пауза)
    # Ключ не нужен вовсе: сервер впустил и БЕЗ него.
    без = [с for с in из_["rows"] if с["scheme"] == "БЕЗ КЛЮЧА"]
    из_["key_needed"] = not (без and без[0]["verdict"] == ВЕРДИКТ_ПРИНЯТ)
    return из_


def свод(итоги: list) -> str:
    """Строки для владельца. Только то, что сказал сервер."""
    строки = []
    for и in итоги:
        загл = f"{и['sender']} ({и.get('key_env') or 'ключ не нужен'})"
        if и.get("why_not"):
            строки.append(f"{загл}: проба не шла -- {и['why_not']}")
            continue
        if not и["key_present"] and и.get("key_env"):
            строки.append(f"{загл}: секрета в окружении нет -- проба без ключа")
        if и.get("plan_required"):
            план = [с for с in и["rows"] if с["verdict"] == ВЕРДИКТ_ПЛАН]
            строки.append(f"{загл}: НУЖЕН ПЛАТНЫЙ ПЛАН, слова сервера: "
                           f"{(план[0].get('server_text') or '')[:200]}")
        if и.get("accepted_scheme"):
            строки.append(f"{загл}: ключ принят схемой {и['accepted_scheme']}"
                           + ("" if и.get("key_needed")
                              else " (но сервер впускает и без ключа)"))
        elif и["key_present"]:
            плохо = [f"{с['scheme']} -> http {с['http']}: "
                      f"{(с.get('server_text') or с.get('why_not') or '')[:160]}"
                      for с in и["rows"]]
            строки.append(f"{загл}: НИ ОДНА схема не принята. " + " | ".join(плохо))
        for с in и["rows"]:
            строки.append(f"    {с['scheme']:26s} http {str(с['http']):>4s}  "
                           f"{с['verdict']}  "
                           f"{(с.get('server_text') or с.get('why_not') or '')[:160]}")
    return "\n".join(строки)


def self_test() -> int:
    import selftest_guard as _SG  # noqa: PLC0415

    _охрана = _SG.включить()
    проверки = []

    def chk(имя, ок, факт=""):
        проверки.append((имя, bool(ок), факт))

    try:
        # 1. Проба не может оказаться транзакцией -- это главное свойство.
        б = проба_безопасна()
        chk("проба содержит символ вне base64 И вне base58", б["ok"], б)
        chk("в пробе нет ни одной подписи", "!" in ПРОБА_TX)
        chk("строка только из base64 признана опасной",
            not проба_безопасна("AAAABBBBCCCC")["ok"])
        chk("и причина названа словами",
            "base64" in (проба_безопасна("AAAABBBBCCCC")["why_not"] or ""))

        # 2. Ключ не выходит наружу ни в тексте, ни в адресе.
        КЛЮЧ = "sekretnyy-token-12345"
        chk("ключ вычищен из текста ответа",
            "***" in вычистить(f"invalid apikey {КЛЮЧ}", [КЛЮЧ])
            and КЛЮЧ not in вычистить(f"invalid apikey {КЛЮЧ}", [КЛЮЧ]))
        chk("короткий мусор не вычищается как ключ",
            вычистить("abc", ["ab"]) == "abc")
        chk("ключ в другом регистре тоже вычищен",
            КЛЮЧ.upper() not in вычистить(f"bad {КЛЮЧ.upper()}", [КЛЮЧ]))
        зп = запрос_схемы("blockrazor", "query:apikey", КЛЮЧ)
        chk("в строке запроса ключ действительно стоит", КЛЮЧ in зп["url"])
        стр_кл = одна_проба(
            "blockrazor", "query:apikey", КЛЮЧ,
            отправитель=lambda а, д, з, т: (401, f"bad key {КЛЮЧ} in {а}"))
        chk("в отчёт адрес уходит БЕЗ строки запроса",
            "?" not in (стр_кл["url"] or ""), стр_кл["url"])
        chk("и текст ответа без ключа",
            КЛЮЧ not in (стр_кл["server_text"] or ""), стр_кл["server_text"])
        chk("отказ по ключу назван отказом по ключу",
            стр_кл["verdict"] == ВЕРДИКТ_НЕ_ПРИНЯТ, стр_кл["verdict"])

        # 3. Схемы кладут ключ туда, куда сказано.
        зг = запрос_схемы("blockrazor", "header:apikey", КЛЮЧ)
        chk("header:apikey -- заголовок apikey",
            зг["headers"].get("apikey") == КЛЮЧ, зг["headers"])
        зб = запрос_схемы("blockrazor", "header:Authorization:Bearer", КЛЮЧ)
        chk("Bearer приписан перед ключом",
            зб["headers"].get("Authorization") == f"Bearer {КЛЮЧ}",
            зб["headers"])
        збк = запрос_схемы("blockrazor", "БЕЗ КЛЮЧА", КЛЮЧ)
        chk("без ключа -- ни одного заголовка с ключом и чистый адрес",
            КЛЮЧ not in json.dumps(збк["headers"]) and КЛЮЧ not in збк["url"])
        chk("схема без ключа не требует секрета",
            запрос_схемы("blockrazor", "БЕЗ КЛЮЧА", None)["why_not"] is None)
        chk("схема с ключом без секрета не идёт",
            запрос_схемы("blockrazor", "header:apikey", None)["why_not"]
            == "секрет не задан в окружении")

        # 4. Тело -- формата сервиса, а не одного на всех.
        chk("у BlockRazor тело своё (transaction/mode)",
            зг["body"].get("mode") == "fast"
            and зг["body"].get("transaction") == ПРОБА_TX, зг["body"])
        за = запрос_схемы("astralane", "query:api-key", КЛЮЧ)
        chk("у Astralane тело jsonrpc sendTransaction",
            за["body"].get("method") == "sendTransaction", за["body"])
        chk("и ключ ушёл в строку запроса как api-key",
            "api-key=" + КЛЮЧ in за["url"], за["url"])

        # 5. Вердикты: жалоба на тело -- это принятый ключ.
        chk("жалоба на base64 -- ключ принят",
            вердикт_ответа(400, "failed to decode base64 transaction")
            == ВЕРДИКТ_ПРИНЯТ)
        chk("403 без слов -- ключ не принят",
            вердикт_ответа(403, "") == ВЕРДИКТ_НЕ_ПРИНЯТ)
        chk("платный план отделён от отказа по ключу",
            вердикт_ответа(402, "unauthorized: please upgrade your plan")
            == ВЕРДИКТ_ПЛАН)
        chk("пустой ответ с кодом 500 -- непонятно, а не выдумка",
            вердикт_ответа(500, "internal") == ВЕРДИКТ_НЕПОНЯТНО)
        chk("200 на битое тело -- всё равно не отказ по ключу",
            вердикт_ответа(200, "{}") == ВЕРДИКТ_ПРИНЯТ)
        # ЖИВЫЕ ОТВЕТЫ 25.09 -- эталон, а не выдумка: строки из
        # docs/sender_key_probe.md, прогон run_sender_key_probe_nl.
        chk("BlockRazor: illegal transaction -- ключ ПРИНЯТ",
            вердикт_ответа(500, '{"signature":"","error":"rpc error: code = '
                            'Unknown desc = illegal transaction"}')
            == ВЕРДИКТ_ПРИНЯТ)
        chk("BlockRazor: auth token missing -- ключ НЕ принят",
            вердикт_ответа(403, '{"signature":"","error":"error: '
                            'Authentication information is missing. Please '
                            'provide a valid auth token"}')
            == ВЕРДИКТ_НЕ_ПРИНЯТ)
        chk("Astralane: failed to decode transaction -- ключ ПРИНЯТ",
            вердикт_ответа(200, '{"error":{"code":-32600,"message":"Invalid '
                            'Request: invalid transaction \\"failed to decode '
                            'transaction only base64 is supported\\""}}')
            == ВЕРДИКТ_ПРИНЯТ)
        chk("0slot без ключа: api-key does not exist -- ключ НЕ принят",
            вердикт_ответа(403, '{"error":{"code":403,"message":"api-key does '
                            'not exist"}}') == ВЕРДИКТ_НЕ_ПРИНЯТ)

        # 6. Сеть упала -- это не "ключ не принят".
        def падает(а, д, з, т):
            raise OSError("connection refused")

        стр_с = одна_проба("blockrazor", "header:apikey", КЛЮЧ,
                            отправитель=падает)
        chk("падение сети названо своим словом",
            стр_с["verdict"] == ВЕРДИКТ_СЕТЬ, стр_с["verdict"])
        chk("и в причине нет ключа",
            КЛЮЧ not in (стр_с["why_not"] or ""))

        # 7. Свод по сервису: принята вторая схема, первая -- нет.
        ответы = {}

        def сервер(адрес, данные, заг, таймаут):
            if заг.get("apikey") == КЛЮЧ:
                ответы["apikey"] = True
                return 400, "invalid transaction: base58 decode error"
            if заг.get("Authorization"):
                return 401, "unauthorized"
            return 401, "api key required"

        итог = пробы_сервиса("blockrazor", отправитель=сервер, пауза=0,
                              окружение={"BLOCKRAZOR_AUTH_TOKEN": КЛЮЧ})
        chk("имя секрета берётся из реестра",
            итог["key_env"] == "BLOCKRAZOR_AUTH_TOKEN", итог["key_env"])
        chk("принятая схема найдена",
            итог["accepted_scheme"] == "header:apikey",
            итог["accepted_scheme"])
        chk("сервер спрашивал ключ -- значит ключ нужен",
            итог["key_needed"] is True)
        chk("проб ровно столько, сколько схем", len(итог["rows"]) == len(СХЕМЫ),
            len(итог["rows"]))
        chk("в своде нет значения ключа", КЛЮЧ not in свод([итог]))
        chk("в своде названа принятая схема",
            "header:apikey" in свод([итог]))

        # 8. Без секрета в окружении идёт только проба без ключа.
        итог2 = пробы_сервиса("blockrazor", отправитель=сервер, пауза=0,
                               окружение={})
        chk("без секрета проба одна -- без ключа", len(итог2["rows"]) == 1,
            len(итог2["rows"]))
        chk("и это сказано словами", "секрета в окружении нет" in свод([итог2]))

        # 9. Сервер, который впускает без ключа: ключ не нужен.
        итог3 = пробы_сервиса(
            "blockrazor", пауза=0, окружение={"BLOCKRAZOR_AUTH_TOKEN": КЛЮЧ},
            отправитель=lambda а, д, з, т: (400, "bad transaction encoding"))
        chk("если пускают без ключа -- так и сказано",
            итог3["key_needed"] is False, итог3["key_needed"])

        # 10. Отправитель БЕЗ адреса точки входа: проба не идёт вовсе и
        # причина берётся из реестра, а не придумывается. Реестр для этой
        # проверки свой: у настоящих отправителей адреса есть (у Nozomi он
        # появился 25.09 -- Амстердам из его endpoints.json).
        import json as _jт  # noqa: PLC0415
        import tempfile as _tт  # noqa: PLC0415

        with _tт.TemporaryDirectory() as _вр:
            _путь = f"{_вр}/senders_test.json"
            with open(_путь, "w", encoding="utf-8") as _ф:
                _jт.dump({"senders": {"безадреса": {
                    "name": "Без адреса", "url": None,
                    "url_note": "точка входа неизвестна -- не подключаем",
                    "format": "jsonrpc_sendTransaction",
                    "key_env": "TEST_KEY", "key_in": "query:c",
                    "min_tip_lamports": 1000, "tip_accounts": []}}}, _ф)
            итог4 = пробы_сервиса("безадреса", пауза=0, путь=_путь,
                                   окружение={"TEST_KEY": КЛЮЧ},
                                   отправитель=lambda а, д, з, т: (200, "{}"))
        chk("без адреса точки входа проба не идёт", итог4["rows"] == [],
            итог4["rows"])
        chk("и причина -- та самая, что в реестре",
            "точка входа" in (итог4["why_not"] or ""), итог4["why_not"])
        SND.реестр(заново=True)  # вернуть боевой реестр после подменного
        chk("у Nozomi в боевом реестре есть Амстердам и параметр ?c=",
            (SND.реестр().get("nozomi") or {}).get("url")
            == "https://ams1.nozomi.temporal.xyz/"
            and (SND.реестр().get("nozomi") or {}).get("key_in") == "query:c",
            (SND.реестр().get("nozomi") or {}).get("url"))
        chk("платный план в своде докладывается словами сервера",
            "НУЖЕН ПЛАТНЫЙ ПЛАН" in свод([пробы_сервиса(
                "blockrazor", пауза=0,
                окружение={"BLOCKRAZOR_AUTH_TOKEN": КЛЮЧ},
                отправитель=lambda а, д, з, т: (
                    402, "please purchase a subscription"))]))
    finally:
        _SG.выключить(_охрана)

    плохо = [(и, ф) for и, ок, ф in проверки if not ок]
    for имя, ок, факт in проверки:
        print(f"  [{'ok  ' if ок else 'СБОЙ'}] {имя}"
              + ("" if ок else f" -- факт: {факт}"))
    print(f"самопроверка пробы ключей: {len(проверки) - len(плохо)}/"
          f"{len(проверки)} пройдено")
    return 1 if плохо else 0


def main() -> int:
    import argparse

    р = argparse.ArgumentParser(description=__doc__)
    р.add_argument("--self-test", action="store_true")
    р.add_argument("--senders", default="blockrazor,astralane,zeroslot",
                    help="кого пробовать, через запятую")
    р.add_argument("--registry", default=None)
    р.add_argument("--out", default=None)
    р.add_argument("--out-md", default=None)
    а = р.parse_args()
    if а.self_test:
        return self_test()

    итоги = []
    for имя in [и.strip() for и in а.senders.split(",") if и.strip()]:
        итоги.append(пробы_сервиса(имя, путь=а.registry))
    текст = свод(итоги)
    print(текст)
    if а.out:
        with open(а.out, "w", encoding="utf-8") as ф:
            json.dump({"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                             time.gmtime()),
                        "senders": итоги}, ф, ensure_ascii=False, indent=2)
        print(f"записано: {а.out}")
    if а.out_md:
        with open(а.out_md, "w", encoding="utf-8") as ф:
            ф.write("# Проба ключей отправителей\n\n"
                     "Ответы сервера дословно, значения ключей вычищены.\n\n"
                     "```\n" + текст + "\n```\n")
        print(f"записано: {а.out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
