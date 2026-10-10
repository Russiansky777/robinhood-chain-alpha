#!/usr/bin/env python3
"""Запуски мемкоинов: сбор первых секунд жизни токена из архива PumpApi (задача владельца 08.10, В).

Зачем отдельный скрипт, а не правка `podbivka_arhiv_den.py`: тот строит сигнал вокруг сделки
ИСТОЧНИКА и ночью собирает данные по расписанию -- ломать его нельзя. Здесь свой проход по тем
же часовым файлам, но пул берётся с его ПЕРВОГО события в окне, и пишутся первые --slotov
слотов его жизни.

Что пишется на каждый запуск:
  * минт, пул, тип пула, блок и время создания, создатель (txSigner первого события);
  * в блоке создания: сколько покупок, какими кошельками, на сколько SOL, покупал ли создатель;
  * состояния пула для НАШЕГО входа: конец блока создания, блок +1, блок +2 (как S0_дно/S1/S2
    у сделки источника) -- по ним калиброванная модель считает любой билет офлайн;
  * выходы: состояние пула на +20/+40/+60/+90/+120/+160/+230 слотах;
  * волна: разные кошельки-покупатели и объём их покупок за 5/15/30/60 с от создания (по
    времени события, не по слотам), плюс пик цены и слот пика, первая продажа.

Словарь действий потока (`action`) считается всегда: по нему видно, есть ли в архиве событие
создания или пул опознаётся только по первому событию. Это ответ на вопрос «сколько запусков
в сутки по цепи против того, что видит проход».

Только чтение. Выход: data/podbivka/zapuski/<метка>.json.gz и сводка в data/podbivka/zapuski_svod.json.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import io
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent
П = КОРЕНЬ / "data" / "podbivka"
WSOL = "So11111111111111111111111111111111111111112"
# Полоса годности подогнанной наценки. Прежняя 0.5..1.0 пропускала мусор: у pump-amm
# подгонка давала 0.77-0.81 вместо структурной 1.0 (тариф там берётся с выхода), и это
# съедало 20-24 п.п. на круге. Настоящая наценка не может быть ниже 1 - тарифа - запас, а
# тарифы пулов тут не больше 5 %, поэтому ниже 0.9 значение просто неверно.
ГОДНО_НАЦЕНКЕ = (0.9, 1.0)
# Тариф больше этого -- не дробь (у DAMM v2 встречается 0.5 и даже 1.188): как дробь он
# даёт отрицательную выручку на продаже. Такие пулы в счёт не идут.
ТАРИФ_ПРЕДЕЛ = 0.3
# Подгонка годна только по СОСЕДНИМ событиям: иначе предыдущее состояние старое, и
# формула занижает наценку.
СЛОТОВ_ДЛЯ_ПОДГОНКИ = 2
ВЫХОДЫ = (20, 40, 60, 90, 120, 160, 230)
ОКНА_С = (5, 15, 30, 60)
КРИВЫЕ = {"pump", "raydium-launchpad"}
# переезд в потоке размечен дважды: настоящий пул около 85 SOL и пылевая засевка на 1e-5 SOL
РЕЗЕРВ_ПЕРЕЕЗДА = 10.0
# пулы, у которых модель берёт наценку входа/выхода из подогнанных f и g, а не из тарифа
XYK = {"raydium-cpmm", "meteora-damm-v1", "pump-amm"}

р_sig = re.compile(r'"signature":\s*"([1-9A-HJ-NP-Za-km-z]{64,90})"')
р_pool = re.compile(r'"poolId":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_tip = re.compile(r'"pool":\s*"([a-z0-9\-]+)"')
р_signer = re.compile(r'"txSigner":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_trader = re.compile(r'"trader":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_block = re.compile(r'"block":\s*(\d+)')
р_ts = re.compile(r'"timestamp":\s*(\d+)')
р_mint = re.compile(r'"mint":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_qmint = re.compile(r'"quoteMint":\s*"([1-9A-HJ-NP-Za-km-z]{32,44})"')
р_action = re.compile(r'"action":\s*"([a-zA-Z_]+)"')
р_qam = re.compile(r'"quoteAmount":\s*"?([0-9.eE+-]+)')
р_q = re.compile(r'"(quoteInPool|tokensInPool|vQuoteInBondingCurve|vTokensInBondingCurve)":\s*"?([0-9.eE+-]+)')
р_fee = re.compile(r'"poolFeeRate":\s*"?([0-9.eE+-]+)')
р_tam = re.compile(r'"tokenAmount":\s*"?([0-9.eE+-]+)')


def часы(с: str, сколько: int) -> list:
    т = time.strptime(с, "%Y-%m-%dT%H")
    н = int(time.mktime(т)) - time.timezone
    return [time.strftime("%Y/%m/%d/%H", time.gmtime(н + 3600 * k)) for k in range(сколько)]


def число(м) -> float | None:
    """Первая группа совпадения как float; None, если совпадения нет или это не число."""
    if not м:
        return None
    try:
        return float(м.group(1))
    except ValueError:
        return None


def состояние(стр: str) -> tuple | None:
    поля = dict(р_q.findall(стр))
    x = поля.get("vQuoteInBondingCurve") or поля.get("quoteInPool")
    y = поля.get("vTokensInBondingCurve") or поля.get("tokensInPool")
    try:
        x, y = float(x), float(y)
    except (TypeError, ValueError):
        return None
    return (x, y) if x > 0 and y > 0 else None


def запись(з: dict) -> dict:
    """Готовый ряд запуска: состояния входа и выхода, волна, пик."""
    ряд = з["ряд"]
    б0 = з["блок"]
    вход: dict = {}
    for имя, сдвиг in (("створ_дно", 0), ("створ+1", 1), ("створ+2", 2)):
        посл = [e for e in ряд if e["блок"] == б0 + сдвиг and e["сост"]]
        if посл:
            вход[имя] = [посл[-1]["сост"][0], посл[-1]["сост"][1], посл[-1]["блок"]]
    выход: dict = {}
    for H in ВЫХОДЫ:
        до = [e for e in ряд if e["блок"] <= б0 + H - 1 and e["сост"]]
        if до:
            выход[str(H)] = [до[-1]["сост"][0], до[-1]["сост"][1], до[-1]["блок"]]
    волна: dict = {}
    for т in ОКНА_С:
        до_ts = з["ts"] + т * 1000
        пок = [e for e in ряд if e["ts"] <= до_ts and e["действие"] == "buy"]
        волна[f"{т}с"] = {"покупок": len(пок),
                          "кошельков": len({e["кто"] for e in пок if e["кто"]}),
                          "sol": round(sum(e["sol"] or 0 for e in пок), 4)}
    # Наценка для модели. Пулам из XYK `сделка_по_состояниям` требует подогнанные f и g,
    # остальным -- тариф пула. Подгонка та же, что в podbivka_arhiv_den.модель(): по парам
    # соседних событий, у которых известно предыдущее состояние и оба объёма свопа.
    тариф = next((e["тариф"] for e in ряд if e.get("тариф") is not None), None)
    f_, g_ = None, None
    if з["пул"] in XYK:
        fs, gs = [], []
        for пред, e in zip(ряд, ряд[1:]):
            if not пред["сост"] or not e.get("ток") or not e.get("кв"):
                continue
            x0, y0 = пред["сост"]
            dy, dx = e["ток"], e["кв"]
            if e["действие"] == "buy" and y0 > dy > 0 and dx > 0:
                v = x0 * dy / ((y0 - dy) * dx)
                if ГОДНО_НАЦЕНКЕ[0] <= v <= ГОДНО_НАЦЕНКЕ[1]:
                    fs.append(v)
            elif e["действие"] == "sell" and dy > 0 and x0 > 0:
                v = dx * (y0 + dy) / (x0 * dy)
                if ГОДНО_НАЦЕНКЕ[0] <= v <= ГОДНО_НАЦЕНКЕ[1]:
                    gs.append(v)
        # у pump-amm тариф берётся с выхода: f = 1, g = 1 - тариф (замерено на цепи,
        # медиана f ровно 1.00000 по 3884 пулам). Запас 1 - тариф для f занижал покупку.
        запас_f = 1.0 if з["пул"] == "pump-amm" else (
            1 - тариф if тариф is not None else None)
        запас_g = 1 - тариф if тариф is not None else None
        f_ = statistics.median(fs) if fs else запас_f
        g_ = statistics.median(gs) if gs else запас_g
    # Кто покупает в окне и сколько раз. Нужно, чтобы опознать агента BOOST: с 21.07.2026
    # pump.fun пять минут после переезда выкупает токен TWAP-партиями (около 17.6 SOL на
    # SOL-парах) и сжигает купленное. Агент должен быть виден как кошелёк, который покупает
    # во ВСЕХ переездах много раз подряд. Имя не задаётся -- оно находится по частоте, уже
    # офлайн, по этим записям.
    покуп: dict = {}
    for e in ряд:
        if e["действие"] != "buy" or not e["кто"]:
            continue
        к = покуп.setdefault(e["кто"], {"покупок": 0, "sol": 0.0, "первый_слот": None,
                                        "последний_слот": None})
        к["покупок"] += 1
        к["sol"] += e["sol"] or 0
        с_ = e["блок"] - б0
        к["первый_слот"] = с_ if к["первый_слот"] is None else min(к["первый_слот"], с_)
        к["последний_слот"] = (с_ if к["последний_слот"] is None
                               else max(к["последний_слот"], с_))
    верх_покуп = sorted(покуп.items(), key=lambda kv: -kv[1]["покупок"])[:10]
    цены = [(e["блок"], e["сост"][0] / e["сост"][1]) for e in ряд if e["сост"]]
    пик = max(цены, key=lambda x: x[1]) if цены else None
    п0 = цены[0][1] if цены else None
    в_блоке = [e for e in ряд if e["блок"] == б0 and e["действие"] == "buy"]
    продажи = [e for e in ряд if e["действие"] == "sell"]
    return {
        "минт": з["минт"], "poolId": з["poolId"], "пул": з["пул"], "блок": б0, "ts": з["ts"],
        "создатель": з["создатель"], "правило": з["правило"], "quoteMint": з["quoteMint"],
        "событий": len(ряд), "тариф": тариф, "f": f_, "g": g_,
        "вход": вход, "выход": выход, "волна": волна,
        "в_блоке_создания": {"покупок": len(в_блоке),
                             "кошельков": len({e["кто"] for e in в_блоке if e["кто"]}),
                             # сами адреса, до восьми: без них на вопрос «кто снайпит
                             # переезд» ответить нечем -- счётчик имён не называет
                             "кто": sorted({e["кто"] for e in в_блоке if e["кто"]})[:8],
                             "sol": round(sum(e["sol"] or 0 for e in в_блоке), 4),
                             "создатель_купил": any(e["кто"] == з["создатель"] for e in в_блоке)},
        "пик": {"слотов": пик[0] - б0, "рост_пп": round(100 * (пик[1] / п0 - 1), 2)}
        if пик and п0 else None,
        "первая_продажа_слотов": (продажи[0]["блок"] - б0) if продажи else None,
        "продаж": len(продажи),
        "покупатели": [{"кто": k, **{kk: (round(vv, 4) if isinstance(vv, float) else vv)
                                     for kk, vv in v.items()}} for k, v in верх_покуп],
        "покупателей_всего": len(покуп)}


def main() -> int:  # noqa: PLR0912, PLR0915
    global ВЫХОДЫ  # noqa: PLW0603
    import subprocess  # noqa: PLC0415
    import requests  # noqa: PLC0415
    # в venv облачного прогона стоят только requests и solders; zstandard проход
    # доустанавливает себе сам (podbivka_arhiv_den, строка 435) -- делаем так же
    try:
        import zstandard  # noqa: PLC0415
    except ModuleNotFoundError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "zstandard"], check=True)
        import zstandard  # noqa: PLC0415
    import podbivka_arhiv_den as AD  # noqa: PLC0415
    import podbivka_run as R  # noqa: PLC0415

    р_ = argparse.ArgumentParser()
    р_.add_argument("--s", required=True, help="начало окна, YYYY-MM-DDTHH")
    р_.add_argument("--chasov", type=int, default=24)
    р_.add_argument("--slotov", type=int, default=max(ВЫХОДЫ))
    р_.add_argument("--metka", default="")
    р_.add_argument("--tolko-slovar", action="store_true",
                    help="только словарь действий потока, без сбора запусков")
    р_.add_argument("--vyhody", default="",
                    help="горизонты выхода в слотах через запятую; пусто -- как по умолчанию")
    р_.add_argument("--poryadok-nezavisimo", action="store_true",
                    help="регистрировать пул независимо от порядка строк в потоке. Зачем: "
                         "у pump.fun создание и покупка создателя идут ОДНОЙ транзакцией, в "
                         "потоке это две строки, и если строка покупки пришла первой, пул "
                         "регистрировался по действию buy, правило выходило None и запуск "
                         "отбрасывался навсегда. Так терялось 41 213 пулов в сутки -- ровно "
                         "та доля, у которой создатель покупает в створе (по живому журналу "
                         "Code-1 41 100 в сутки из 61 000 созданий). С флагом строки первого "
                         "блока придерживаются в стадии, и пул становится запуском, если в "
                         "ЕГО ПЕРВОМ БЛОКЕ есть действие создания -- в каком бы порядке "
                         "строки ни пришли. Память на это не растёт: стадия живёт один блок.")
    р_.add_argument("--tolko-pereezd", action="store_true",
                    help="держать только пулы переезда кривой (createPool/migrate) -- так "
                         "можно взять окно в тысячу слотов, не выедая память на всех запусках")
    а = р_.parse_args()
    метка = а.metka or f"zap_{а.s}"
    if а.vyhody:
        ВЫХОДЫ = tuple(sorted(int(x) for x in а.vyhody.split(",") if x.strip()))
        а.slotov = max(а.slotov, max(ВЫХОДЫ))

    словарь: collections.Counter = collections.Counter()
    счёт: collections.Counter = collections.Counter()
    видел_пул: set = set()
    молодые: dict = {}
    стадия: dict = {}        # pid -> {"блок": b, "строки": [...]} на один блок
    готовые: list = []
    первый_блок = None
    последний_блок = None

    def читать_час(поток) -> None:
        """Тело часа отдельной функцией -- чтобы обрыв потока ловился и час повторялся.

        08.10 сутки 30.09 упали на urllib3 IncompleteRead (прочитано 4.4 МБ из 439): поток
        часа рвётся, и без повтора из-за одного часа теряются целые сутки. Что не
        дочиталось после трёх попыток, честно считается в «обрыв_часов».
        """
        nonlocal первый_блок, последний_блок

        def создать_запуск(стр, д, блок, pid, e, сост, ts, кто, предсобытия) -> None:
            """Регистрация запуска, когда строка создания уже найдена.

            Одно место на два пути: обычный (строка создания пришла первой) и путь
            --poryadok-nezavisimo (сначала пришла сделка того же блока, она ждала в
            стадии). Предсобытия -- отложенные сделки блока создания; без них
            «создатель купил сам» в связке create+dev buy не виден.
            """
            правило = f"создание:{д}"
            if а.tolko_pereezd and правило not in ("создание:createPool",
                                                   "создание:migrate"):
                счёт["мимо_среза_переезда"] += 1
                return
            if а.tolko_pereezd and (сост is None or сост[0] < РЕЗЕРВ_ПЕРЕЕЗДА):
                счёт["переезд_без_резерва"] += 1
                return
            счёт[правило] += 1
            мм = р_mint.search(стр)
            тип = р_tip.search(стр)
            qmм = р_qmint.search(стр)
            молодые[pid] = {"poolId": pid, "минт": мм.group(1) if мм else None,
                            "пул": тип.group(1) if тип else None, "блок": блок, "ts": ts,
                            "создатель": кто, "правило": правило,
                            "quoteMint": qmм.group(1) if qmм else None,
                            "ряд": [e, *предсобытия]}

        for стр in io.TextIOWrapper(
                zstandard.ZstdDecompressor().stream_reader(поток.raw),
                encoding="utf-8", errors="ignore"):
            счёт["строк"] += 1
            ам = р_action.search(стр)
            д = ам.group(1) if ам else "<нет>"
            словарь[д] += 1
            бм = р_block.search(стр)
            if not бм:
                continue
            блок = int(бм.group(1))
            if первый_блок is None:
                первый_блок = блок
            последний_блок = блок
            if а.tolko_slovar:
                continue
            пм = р_pool.search(стр)
            if not пм:
                continue
            pid = пм.group(1)
            # закрываем молодые пулы, чьё окно кончилось
            if счёт["строк"] % 50000 == 0:
                for k in [k for k, v in молодые.items() if блок > v["блок"] + а.slotov]:
                    готовые.append(запись(молодые.pop(k)))
            сост = состояние(стр)
            tsм = р_ts.search(стр)
            ts = int(tsм.group(1)) if tsм else 0
            кто = None
            тм = р_trader.search(стр)
            sм = р_signer.search(стр)
            кто = (тм.group(1) if тм else None) or (sм.group(1) if sм else None)
            sol = None
            qм = р_qam.search(стр)
            qmм = р_qmint.search(стр)
            if qм and qmм and qmм.group(1) == WSOL:
                try:
                    sol = float(qм.group(1))
                except ValueError:
                    sol = None
            e = {"блок": блок, "ts": ts, "действие": д, "кто": кто, "sol": sol,
                 "сост": сост, "тариф": число(р_fee.search(стр)),
                 "ток": число(р_tam.search(стр)), "кв": число(qм)}
            if pid in молодые:
                if блок <= молодые[pid]["блок"] + а.slotov:
                    молодые[pid]["ряд"].append(e)
                else:
                    готовые.append(запись(молодые.pop(pid)))
                continue
            if pid in видел_пул:
                continue
            if а.poryadok_nezavisimo:
                ст = стадия.get(pid)
                if ст is None and д in ("buy", "sell"):
                    # сделка раньше строки создания: придержать на один блок. Строка
                    # создания в стадию не идёт -- она регистрирует пул сразу по
                    # обычному пути ниже, и сделки того же блока попадут в ряд как
                    # всегда (ветка «pid in молодые»).
                    if len(стадия) > 20000:
                        for k in [k for k, v in стадия.items() if v["блок"] < блок]:
                            стадия.pop(k, None)
                            счёт["стадия_истекла"] += 1
                    стадия[pid] = {"блок": блок, "строки": [(стр, д, e)]}
                    continue
            if а.poryadok_nezavisimo and (ст := стадия.get(pid)) is not None:
                if ст["блок"] != блок:
                    стадия.pop(pid, None)
                    видел_пул.add(pid)
                    счёт["пул_не_запуск"] += 1
                    continue
                if len(ст["строки"]) < 200:
                    ст["строки"].append((стр, д, e))
                if д in ("buy", "sell"):
                    continue
                # пришло действие создания -- этот пул запуск, поднимаем стадию целиком
                стадия.pop(pid, None)
                видел_пул.add(pid)
                счёт["поднято_из_стадии"] += 1
                создать_запуск(стр, д, блок, pid, e, сост, ts, кто,
                               [x[2] for x in ст["строки"] if x[1] in ("buy", "sell")])
                continue
            видел_пул.add(pid)
            # первый раз видим пул. Запуск -- если это событие создания, иначе (если в потоке
            # создания нет) -- первое событие пула-кривой в окне, но не в первый час окна:
            # иначе под «запуск» попадут пулы, жившие до начала окна.
            if д not in ("buy", "sell"):
                создать_запуск(стр, д, блок, pid, e, сост, ts, кто, [])
                continue
            if (первый_блок is not None and блок > первый_блок + 3 * а.slotov
                    and (р_tip.search(стр).group(1) if р_tip.search(стр) else "") in КРИВЫЕ):
                правило = "первое_событие_кривой"
            else:
                счёт["пул_не_запуск"] += 1
                continue
            if а.tolko_pereezd:
                счёт["мимо_среза_переезда"] += 1
                continue
            счёт[правило] += 1
            мм = р_mint.search(стр)
            тип = р_tip.search(стр)
            молодые[pid] = {"poolId": pid, "минт": мм.group(1) if мм else None,
                            "пул": тип.group(1) if тип else None, "блок": блок, "ts": ts,
                            "создатель": кто, "правило": правило,
                            "quoteMint": qmм.group(1) if qmм else None, "ряд": [e]}

    for ч in часы(а.s, а.chasov):
        url = f"https://replay.pumpapi.io/{ч}.jsonl.zst"
        for попытка in range(3):
            try:
                о = AD.открыть_час(requests, url, ч)
            except Exception as exc:  # noqa: BLE001
                счёт[f"ошибка_{type(exc).__name__}"] += 1
                break
            with о as поток:
                код = getattr(поток, "status_code", 200)
                if код != 200:
                    счёт[f"http_{код}"] += 1
                    break
                try:
                    читать_час(поток)
                except Exception as exc:  # noqa: BLE001
                    счёт[f"обрыв_{type(exc).__name__}"] += 1
                    if попытка < 2:
                        time.sleep(5 * (попытка + 1))
                        continue
                    счёт["обрыв_часов"] += 1
                    print(f"  час {ч}: поток рвался трижды, идём дальше", flush=True)
                    break
                счёт["часов"] += 1
                break

    for k in list(молодые):
        готовые.append(запись(молодые.pop(k)))

    пап = П / "zapuski"
    пап.mkdir(parents=True, exist_ok=True)
    вых = пап / f"{метка}.json.gz"
    тело = {"что": "запуски мемкоинов: первые слоты жизни пула", "окно": {"с": а.s, "часов": а.chasov},
            "слотов": а.slotov, "словарь_действий": dict(словарь.most_common()),
            "счёт": dict(счёт), "блоки": {"первый": первый_блок, "последний": последний_блок},
            "запусков": len(готовые), "ряды": готовые}
    with gzip.open(вых, "wt", encoding="utf-8") as ф:
        json.dump(тело, ф, ensure_ascii=False)
    свод = П / "zapuski_svod.json"
    было = {}
    if свод.exists():
        try:
            было = json.loads(свод.read_text(encoding="utf-8"))
        except ValueError:
            было = {}
    было[метка] = {k: v for k, v in тело.items() if k != "ряды"}
    свод.write_text(json.dumps(было, ensure_ascii=False, indent=1), encoding="utf-8")
    R.записано(вых)
    R.записано(свод)
    print(f"{вых.name}: строк {счёт.get('строк', 0)}, часов {счёт.get('часов', 0)}, "
          f"запусков {len(готовые)}, словарь {dict(словарь.most_common(6))}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
