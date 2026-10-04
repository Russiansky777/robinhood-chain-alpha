#!/usr/bin/env python3
"""ОДНА продажа названной позиции полосы ПРАВИЛОМ 4 (редакция владельца 05.10).

ЗАЧЕМ ОТДЕЛЬНЫЙ ПРОГОН, А НЕ ЖДАТЬ ДЕПЛОЯ. Правило 4 новой редакции лежит в
ветке и в службу попадёт ТОЛЬКО при нуле позиций -- так стоит гейт перезапуска
(pered_perezapuskom_sluzhby: открытых 0 и зависших 0). А единственная висящая
позиция -- это ровно та, которую новое правило и продало бы: D6eyhD2o, вход
0.5 SOL, зависла 04.10 в 19:52:10Z. Замкнутый круг: деплой ждёт продажи,
продажа ждёт деплоя. Этот прогон его разрывает -- он зовёт ТУ ЖЕ функцию
(Seller.правило_4_продажа) из ветки, один раз, по названной позиции.

ЧТО ЗДЕСЬ НЕ ПЕРЕПИСАНО ВТОРЫМ НАПИСАНИЕМ: ни выбор пути, ни котировки, ни
границы, ни сборка, ни подпись. Всё это делает сам продавец; прогон только
находит позицию, читает остаток и зовёт правило. Поэтому числа этого прогона и
числа службы после деплоя -- одни и те же по построению.

КЛЮЧ ЖИВЁТ В ОКРУЖЕНИИ СЛУЖБЫ, А НЕ В ПРОГОНЕ. Он берётся из /proc/<pid>/environ
живого продавца (прогон идёт тем же пользователем, что и служба) и попадает
только в os.environ своего процесса. Ни одно значение окружения наружу не
печатается -- ни в вывод, ни в файл: печатаются ТОЛЬКО имена и их число.

ЖИВОЙ РЕЖИМ -- ПО СВОЕМУ ФЛАГУ PRODAT_PRAVILO4_LIVE=1. По умолчанию прогон
доходит до двух котировок и останавливается ДО подписи: показывает, какой путь
выбрало бы правило и с какой границей.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent.parent


def _есть_каталог(п: Path) -> bool:
    """Есть ли каталог. ЧУЖОЙ КАТАЛОГ -- ЭТО "НЕТ", А НЕ ИСКЛЮЧЕНИЕ.

    Прогон запускается и бегунком (ghrunner, самопроверка), и пользователем
    службы (bot, живая продажа). У ghrunner нет права даже на stat
    /home/bot/bloom_executor, и Path.exists() там роняет PermissionError:
    самопроверка падала до первой проверки. Для выбора пути импорта "не вижу"
    и "нет" -- одно и то же.
    """
    try:
        return п.is_dir()
    except (OSError, PermissionError):
        return False


# ПОРЯДОК КАТАЛОГОВ -- ЭТО ВЕРСИЯ ДЕНЕЖНОГО МОДУЛЯ, И ОН НЕ МЕЛОЧЬ. Прогон
# кладут на хост рядом с ИЗМЕНЁННЫМИ модулями ветки и зовут с
# PYTHONPATH=<они>:<код службы>. Если вставить код службы в начало sys.path,
# он перекроет PYTHONPATH -- и прогон возьмёт СТАРЫЙ bloom_seller, в котором
# правила 4 нет вовсе: ровно так первый живой запуск 05.10 в 22:42:56Z упал на
# AttributeError 'Seller' object has no attribute 'правило_4_продажа'. Поэтому
# своё дерево -- в начало, код службы -- в КОНЕЦ, как запас.
_своё = КОРЕНЬ / "analysis"
if _есть_каталог(_своё) and str(_своё) not in sys.path:
    sys.path.insert(0, str(_своё))
_службы = Path("/home/bot/bloom_executor")
if _есть_каталог(_службы) and str(_службы) not in sys.path:
    sys.path.append(str(_службы))

ФЛАГ_ЖИВЬЁМ = "PRODAT_PRAVILO4_LIVE"
СЛУЖБА_ПО_УМОЛЧАНИЮ = "bloom_seller.py"


def живой_режим(окружение=None) -> bool:
    окр = os.environ if окружение is None else окружение
    return (окр.get(ФЛАГ_ЖИВЬЁМ) or "0").strip() == "1"


def окружение_службы(признак: str = СЛУЖБА_ПО_УМОЛЧАНИЮ, *,
                      корень: str = "/proc", применить: bool = True) -> dict:
    """Окружение ЖИВОЙ службы -- в свой процесс. Значений не печатаем никогда.

    Правда об окружении службы -- в /proc/<pid>/environ, а не в env-файле:
    файл мог быть изменён после запуска, а подписывает служба тем, что у неё в
    памяти. Имена наружу отдаются, значения -- нет: ключ кошелька полосы лежит
    ровно здесь.
    """
    из_ = {"ok": False, "pid": None, "imjon": 0, "why_not": None, "imena": []}
    кор = Path(корень)
    if not кор.exists():
        из_["why_not"] = f"{корень} недоступен"
        return из_
    пид = None
    for п in sorted(кор.iterdir()):
        if not п.name.isdigit():
            continue
        try:
            cmd = (п / "cmdline").read_bytes().decode("utf-8", "replace")
        except (OSError, PermissionError):
            continue
        if признак in cmd:
            пид = int(п.name)
            break
    if пид is None:
        из_["why_not"] = f"живого процесса с {признак} не найдено"
        return из_
    из_["pid"] = пид
    try:
        сырое = (кор / str(пид) / "environ").read_bytes()
    except (OSError, PermissionError) as exc:
        из_["why_not"] = f"окружение процесса не прочитано: {type(exc).__name__}"
        return из_
    пары = {}
    for кусок in сырое.split(b"\0"):
        if not кусок or b"=" not in кусок:
            continue
        имя, _, знач = кусок.partition(b"=")
        try:
            пары[имя.decode("utf-8")] = знач.decode("utf-8")
        except UnicodeDecodeError:
            continue
    if применить:
        for имя, знач in пары.items():
            os.environ.setdefault(имя, знач)
    из_.update(ok=True, imjon=len(пары), imena=sorted(пары))
    return из_


def найти_позицию(позиции: dict, *, cid: str | None = None,
                   mint: str | None = None, метка_полосы: str = "own_send",
                   закрытые: tuple = ("closed", "unsold", "spisana")) -> dict:
    """Та позиция, которую велели продать. Догадок нет: либо cid, либо минт.

    Минт выбирает САМУЮ СВЕЖУЮ незакрытую позицию этого минта: у одного минта
    их может быть несколько, и продать надо живую, а не вчерашнюю.
    """
    из_ = {"ok": False, "cid": None, "pos": None, "why_not": None, "kandidatov": 0}
    if not cid and not mint:
        из_["why_not"] = "не названо ни cid, ни минт"
        return из_
    канд = []
    for к, p in (позиции or {}).items():
        p = p or {}
        if cid and к != cid:
            continue
        if mint and p.get("mint") != mint:
            continue
        if p.get("lane") != метка_полосы:
            continue
        if not cid and p.get("state") in закрытые:
            continue
        канд.append((float(p.get("ts_intent") or 0), к, p))
    из_["kandidatov"] = len(канд)
    if not канд:
        из_["why_not"] = ("такой позиции полосы нет (или она уже закрыта)"
                           if not cid else f"позиции {cid} нет")
        return из_
    канд.sort(reverse=True)
    _, к, p = канд[0]
    из_.update(ok=True, cid=к, pos=p)
    return из_


def _баланс(rpc_call, кошелёк: str) -> int | None:
    r = rpc_call("getBalance", [кошелёк, {"commitment": "confirmed"}])
    зн = ((r.get("result") or {}).get("value") if r.get("ok") else None)
    return int(зн) if isinstance(зн, int) else None


def продать_раз(*, cid: str | None, mint: str | None, живьём: bool,
                 ждать_с: float = 60.0, продавец=None, состояние=None,
                 rpc=None, читатель_остатка=None) -> dict:
    """Найти позицию, спросить две котировки, продать правилом 4. Итог по цепи."""
    import bloom_exec_state as ST  # noqa: PLC0415
    import bloom_own_send as OSW  # noqa: PLC0415
    import bloom_seller as SL  # noqa: PLC0415

    из_: dict = {"ok": False, "why_not": None, "cid": None, "mint": None,
                  "zhivjom": bool(живьём), "pravilo_4": None,
                  "balans_do": None, "balans_posle": None,
                  "ostatok_minta_do": None, "ostatok_minta_posle": None,
                  "podpis": None, "put": None, "vyruchka_sol": None}
    rpc = rpc if rpc is not None else SL.rpc_call
    сост = состояние if состояние is not None else ST.ExecState()
    поз = найти_позицию(сост.positions(), cid=cid, mint=mint,
                         метка_полосы=ST.МЕТКА_ПОЛОСЫ)
    if not поз["ok"]:
        из_["why_not"] = поз["why_not"]
        return из_
    pos = поз["pos"]
    из_.update(cid=поз["cid"], mint=pos.get("mint"), sol_in=pos.get("sol_in"),
                lane_group=pos.get("lane_group"), program=pos.get("program"),
                state=pos.get("state"))
    кош = OSW.кошелёк_полосы()
    из_["koshelek"] = кош
    читатель = читатель_остатка if читатель_остатка is not None else SL.token_balance_raw
    bal = читатель(кош, pos.get("mint"))
    if not bal.get("ok"):
        из_["why_not"] = f"остаток минта не прочитан: {bal.get('why_not')}"
        return из_
    остаток = int(bal.get("raw") or 0)
    из_["ostatok_minta_do"] = остаток
    if остаток <= 0:
        из_["why_not"] = "остатка минта на кошельке полосы нет -- продавать нечего"
        return из_
    из_["balans_do"] = _баланс(rpc, кош)
    сл = продавец if продавец is not None else SL.Seller(state=сост, live=bool(живьём))
    # ТРЕВОГИ ЭТОГО ПРОГОНА НЕ ПОСЫЛАЕТ: строку о продаже пошлёт сама служба,
    # когда увидит подтверждённую подпись. Иначе владелец получил бы две.
    сл.оповещатель = None
    пр = сл.правило_4_продажа(pos, bal=bal, now=time.time(),
                               количество_raw=остаток)
    из_["pravilo_4"] = пр
    из_["put"] = пр.get("путь")
    из_["podpis"] = пр.get("signature")
    if not пр.get("ok"):
        из_["why_not"] = пр.get("why_not") or "правило 4 не продало"
        return из_
    из_["ok"] = True
    # ПОДТВЕРЖДЕНИЕ -- ПО ЦЕПИ, а не по ответу Jupiter. Ждём столько, сколько
    # велели, и честно говорим, если не дождались: "отправлено" и "село" -- это
    # разные слова, и в докладе они не должны слипаться.
    если_ждать = float(ждать_с or 0)
    ждали = 0.0
    while если_ждать > 0 and ждали < если_ждать:
        st = rpc("getSignatureStatuses", [[пр.get("signature")],
                                           {"searchTransactionHistory": True}])
        зн = (((st.get("result") or {}).get("value") or [None])[0]
              if st.get("ok") else None)
        if зн:
            из_["podtverzhdenie"] = {"slot": зн.get("slot"),
                                      "err": зн.get("err"),
                                      "status": зн.get("confirmationStatus")}
            if зн.get("err") is None:
                break
            из_["why_not"] = f"транзакция отклонена цепью: {зн.get('err')}"
            из_["ok"] = False
            return из_
        time.sleep(2.0)
        ждали += 2.0
    из_["zhdali_s"] = ждали
    bal2 = читатель(кош, pos.get("mint"))
    из_["ostatok_minta_posle"] = int(bal2.get("raw") or 0) if bal2.get("ok") else None
    из_["balans_posle"] = _баланс(rpc, кош)
    if из_["balans_do"] is not None and из_["balans_posle"] is not None:
        из_["vyruchka_sol"] = round(
            (из_["balans_posle"] - из_["balans_do"]) / 1e9, 9)
    return из_


def строка(свод: dict) -> str:
    """Одна строка для доклада. Числа как есть, без округлений в сторону денег."""
    if not свод.get("ok"):
        return (f"НЕ ПРОДАНО {str(свод.get('mint'))[:10]}: "
                 f"{свод.get('why_not')}")
    кот = (свод.get("pravilo_4") or {}).get("котировки") or {}
    return (f"ПРОДАНО {str(свод.get('mint'))[:10]} путём {свод.get('put')}: "
             f"подпись {свод.get('podpis')}, выручка {свод.get('vyruchka_sol')} SOL, "
             f"вход {свод.get('sol_in')} SOL, котировки пул "
             f"{кот.get('пул_ожидаемый')} / Jupiter {кот.get('jupiter_ожидаемый')} "
             f"лампортов, остаток минта после {свод.get('ostatok_minta_posle')}")


def самопроверка() -> int:
    проверки = []

    def chk(имя, усл, факт=""):
        проверки.append((имя, bool(усл), факт))

    # --- выбор позиции
    поз = {"a": {"lane": "own_send", "mint": "M1", "state": "bought",
                  "ts_intent": 100.0},
            "b": {"lane": "own_send", "mint": "M1", "state": "bought",
                  "ts_intent": 200.0},
            "c": {"lane": "own_send", "mint": "M1", "state": "closed",
                  "ts_intent": 300.0},
            "d": {"lane": None, "mint": "M2", "state": "bought",
                  "ts_intent": 400.0}}
    р = найти_позицию(поз, mint="M1")
    chk("по минту берётся САМАЯ СВЕЖАЯ незакрытая позиция",
        р["ok"] and р["cid"] == "b", р)
    chk("закрытая по минту не берётся, и их число названо",
        р["kandidatov"] == 2, р)
    chk("позиция не полосы не берётся вовсе",
        найти_позицию(поз, mint="M2")["ok"] is False, "")
    chk("списанная по минту не берётся: её убыток уже окнижен",
        найти_позицию({"s": {"lane": "own_send", "mint": "M9",
                               "state": "spisana", "ts_intent": 1.0}},
                       mint="M9")["ok"] is False, "")
    chk("по cid берётся названная, даже закрытая -- это осознанный выбор",
        найти_позицию(поз, cid="c")["ok"] is True, "")
    chk("ни cid, ни минта -- отказ словами, а не догадка",
        найти_позицию(поз)["ok"] is False
        and "ни cid" in (найти_позицию(поз)["why_not"] or ""), "")

    # --- живой режим только по своему флагу
    # ПРОВЕРКА СМОТРИТ ТОЛЬКО РАБОЧУЮ ЧАСТЬ ФАЙЛА. Со своим же текстом она была
    # бы красной всегда: строка-образец лежит в ней самой.
    _рабочее = Path(__file__).read_text(encoding="utf-8").split("def самопроверка")[0]
    chk("код службы в sys.path -- ПОСЛЕДНИМ, иначе он перекроет PYTHONPATH",
        "sys.path.append(str(_службы))" in _рабочее
        and "insert(0, str(_службы))" not in _рабочее, "")
    chk("чужой каталог при выборе пути импорта -- «нет», а не исключение",
        _есть_каталог(Path("/proc/1/root/etc/shadow_нет_такого")) is False
        and _есть_каталог(КОРЕНЬ) is True, "")
    chk("по умолчанию прогон НЕ живой", живой_режим({}) is False)
    chk("живой только по своему флагу=1",
        живой_режим({ФЛАГ_ЖИВЬЁМ: "1"}) is True
        and живой_режим({ФЛАГ_ЖИВЬЁМ: "yes"}) is False)

    # --- окружение службы: значения не печатаются
    тело = Path(__file__).read_text(encoding="utf-8").split("def самопроверка")[0]
    chk("окружение уходит в os.environ, а наружу -- только имена",
        'из_.update(ok=True, imjon=len(пары), imena=sorted(пары))' in тело
        and "sorted(пары.values())" not in тело
        and "пары.items()" in тело, "")
    chk("значения окружения не кладутся в вывод ни одним полем",
        '"znachenija"' not in тело and "знач)" not in тело.split("применить")[-1]
        or "os.environ.setdefault(имя, знач)" in тело, "")
    из_ок = окружение_службы("такого_процесса_нет_совсем", применить=False)
    chk("процесса нет -- отказ словами, без исключения",
        из_ок["ok"] is False and "не найдено" in (из_ок["why_not"] or ""), из_ок)

    # --- продажа зовёт ПРАВИЛО, а не своё написание выбора пути
    chk("прогон зовёт Seller.правило_4_продажа и ничего не решает сам",
        "сл.правило_4_продажа(" in тело
        and "продать_своим_в_пул" not in тело
        and "jupiter_sell" not in тело, "")
    chk("тревоги прогон не посылает -- строку о продаже пошлёт служба",
        "сл.оповещатель = None" in тело, "")

    class _Сост:
        def __init__(self, поз_):
            self._п = поз_

        def positions(self):
            return self._п

    class _Продавец:
        def __init__(self, ответ):
            self.ответ = ответ
            self.оповещатель = "будет снят"
            self.зовы = []

        def правило_4_продажа(self, pos, *, bal, now, количество_raw):
            self.зовы.append({"cid": pos.get("client_order_id"),
                               "количество": количество_raw})
            return self.ответ

    поз_ж = {"ж1": {"client_order_id": "ж1", "lane": "own_send", "mint": "MX",
                     "state": "bought", "ts_intent": 1.0, "sol_in": 0.5,
                     "lane_group": "cand1_03", "program": "6EF8"}}
    балансы = [1_000_000_000, 1_300_000_000]

    def _rpc(метод, парам, **кв):
        if метод == "getBalance":
            return {"ok": True, "result": {"value": балансы.pop(0)}}
        if метод == "getSignatureStatuses":
            return {"ok": True, "result": {"value": [{"slot": 7, "err": None,
                                                        "confirmationStatus": "confirmed"}]}}
        return {"ok": False, "why_not": "в самопроверке сети нет"}

    остатки = [5_000_000, 0]

    def _читатель(кош, минт):
        return {"ok": True, "raw": остатки.pop(0), "ui": 5.0,
                 "accounts": 1, "failures": []}

    пр = _Продавец({"ok": True, "путь": "свой_в_пул", "signature": "ПОДПИСЬ_1",
                     "котировки": {"пул_ожидаемый": 487_654_317,
                                    "jupiter_ожидаемый": 54_830_439}})
    р_ж = продать_раз(cid="ж1", mint=None, живьём=True, ждать_с=4.0,
                       продавец=пр, состояние=_Сост(поз_ж), rpc=_rpc,
                       читатель_остатка=_читатель)
    chk("правилу отдано ВСЁ, что лежит на кошельке, и cid названной позиции",
        пр.зовы == [{"cid": "ж1", "количество": 5_000_000}], пр.зовы)
    chk("оповещатель прогона снят",
        пр.оповещатель is None, пр.оповещатель)
    chk("итог по цепи: выручка из балансов кошелька, а не из ответа Jupiter",
        р_ж["ok"] is True and р_ж["vyruchka_sol"] == 0.3
        and р_ж["ostatok_minta_posle"] == 0, р_ж)
    chk("подтверждение по цепи названо отдельным полем",
        (р_ж.get("podtverzhdenie") or {}).get("slot") == 7, р_ж.get("podtverzhdenie"))
    chk("строка доклада несёт путь, подпись, выручку и обе котировки",
        all(ч in строка(р_ж) for ч in ("свой_в_пул", "ПОДПИСЬ_1", "0.3",
                                        "487654317", "54830439")), строка(р_ж))

    # отказ правила -- отказ прогона, и причина та же
    остатки2 = [5_000_000]
    балансы2 = [1_000_000_000]

    def _rpc2(метод, парам, **кв):
        if метод == "getBalance":
            return {"ok": True, "result": {"value": балансы2.pop(0)}}
        return {"ok": False, "why_not": "нет"}

    пр2 = _Продавец({"ok": False, "why_not": "оба пути отказали",
                      "котировки": {}})
    р_о = продать_раз(cid="ж1", mint=None, живьём=True, ждать_с=0,
                       продавец=пр2, состояние=_Сост(поз_ж), rpc=_rpc2,
                       читатель_остатка=lambda к, м: {"ok": True,
                                                        "raw": остатки2.pop(0),
                                                        "ui": 5.0, "accounts": 1,
                                                        "failures": []})
    chk("отказ правила -- отказ прогона с той же причиной",
        р_о["ok"] is False and р_о["why_not"] == "оба пути отказали", р_о)
    chk("и строка об этом говорит прямо",
        строка(р_о).startswith("НЕ ПРОДАНО"), строка(р_о))

    плохо = 0
    for имя, ок, факт in проверки:
        print(f"  [{'ok  ' if ок else 'СБОЙ'}] {имя}"
              + (f"  -> {факт}" if факт and not ок else ""))
        плохо += (not ок)
    print(f"самопроверка разовой продажи правилом 4: "
          f"{len(проверки) - плохо}/{len(проверки)} пройдено")
    return 1 if плохо else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--cid", default=None)
    ap.add_argument("--mint", default=None)
    ap.add_argument("--zhdat", type=float, default=90.0)
    ap.add_argument("--sluzhba", default=СЛУЖБА_ПО_УМОЛЧАНИЮ)
    ap.add_argument("--vyhod", default=None)
    а = ap.parse_args()
    if а.self_test:
        return самопроверка()
    окр = окружение_службы(а.sluzhba)
    print(f"окружение службы: pid {окр.get('pid')}, имён {окр.get('imjon')}, "
          f"why_not {окр.get('why_not')}")
    if not окр["ok"]:
        print("СТОП: окружения службы нет -- ключ брать негде")
        return 2
    # ИМЕНА КЛЮЧЕЙ -- ДА, ЗНАЧЕНИЯ -- НЕТ. По именам видно, что ключ полосы в
    # окружении есть; само значение не печатается ни здесь, ни в файле.
    print("ключ полосы в окружении: "
          + ("есть" if os.environ.get("OWN_SEND_WALLET_KEY") else "НЕТ"))
    свод = продать_раз(cid=а.cid, mint=а.mint, живьём=живой_режим(),
                        ждать_с=а.zhdat)
    свод["okruzhenie"] = {"pid": окр.get("pid"), "imjon": окр.get("imjon")}
    print(строка(свод))
    print(json.dumps({к: v for к, v in свод.items() if к != "pravilo_4"},
                      ensure_ascii=False)[:1200])
    if а.vyhod:
        Path(а.vyhod).write_text(json.dumps(свод, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
    return 0 if свод.get("ok") or not живой_режим() else 1


if __name__ == "__main__":
    raise SystemExit(main())
