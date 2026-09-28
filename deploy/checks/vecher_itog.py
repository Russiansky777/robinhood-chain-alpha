#!/usr/bin/env python3
"""Вечерний итог: утренние числа плюс то, что владелец просил к вечеру.

ЗАЧЕМ. Утренний итог отвечает на "что было за ночь". К вечеру владелец просил
(28.09, п.4) ещё четыре вещи:
 * кредиты Helius ПО ДНЯМ после снятия подписки, а не только за сегодня;
 * сделки полосы по строителям и sniper_src ОТДЕЛЬНО;
 * метрики сторожа продаж;
 * CU по строителям (p50/p99), падения по вычислительному лимиту и, если
   строитель у предела, предложение предела ПО СТРОИТЕЛЮ (p99 + 10 %);
 * отказы по потолку комиссии пула BLOOM_LANE_MAX_POOL_FEE за сутки -- числом;
 * ответ про freeze authority: проверяет ли его полоса перед покупкой.

Числа не считаются второй раз своей формулой там, где их уже считает служба:
утренние блоки берутся из utro_itog, CU -- из cu_po_stroitelyam, метрики
сторожа -- из его признака жизни.

Только чтение: журналы потоком, учёт кредитов, признак жизни, и по цепи --
getSignatureStatuses с getTransaction ради CU. Ни подписи, ни отправки.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import utro_itog as UI  # noqa: PLC0415
except Exception as exc:  # noqa: BLE001
    print(f"SBOY: utro_itog ne zagruzhen: {type(exc).__name__}: {exc}")
    UI = None
try:
    import cu_po_stroitelyam as CU  # noqa: PLC0415
except Exception as exc:  # noqa: BLE001
    print(f"PREDUPREZHDENIE: cu_po_stroitelyam ne zagruzhen: {type(exc).__name__}")
    CU = None

# Текст отказа по потолку комиссии пула -- ровно тот, что пишет сборка полосы
# (bloom_own_send: "комиссия пула X % выше потолка Y % -- покупка не берётся").
ОТКАЗ_КОМИССИИ = "покупка не берётся"
СЛОВО_КОМИССИИ = "комиссия пула"


def сырые_строки(путь: str):
    """СЫРЫЕ строки журнала. utro_itog.строки отдаёт уже разобранные записи, а
    здесь нужна именно строка: проверка подстроки до разбора JSON -- это разница
    между секундами и минутами на файле в сотни мегабайт."""
    import gzip  # noqa: PLC0415

    открыть = gzip.open if путь.endswith(".gz") else open
    try:
        with открыть(путь, "rt", encoding="utf-8", errors="replace") as ф:
            for с in ф:
                с = с.strip()
                if с:
                    yield с
    except OSError:
        return


def кредиты_по_дням(usage_dir: str, дней: int = 7) -> dict:
    """Кредиты Helius по ДНЯМ и по службам: виден и день отписки, и после него.

    Формат файла -- как его пишет служба: {"дни": {"ГГГГ-ММ-ДД": {"<служба>":
    {"кредитов_за_день": N, "байт_за_день": M, ...}}}}.
    """
    из_ = {"по_дням": {}, "why_not": None}
    кат = Path(usage_dir)
    try:
        файлы = sorted(кат.glob("*.json"))
    except OSError as exc:
        из_["why_not"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        return из_
    if not файлы:
        из_["why_not"] = f"файлов учёта нет: {кат}"
        return из_
    for ф in файлы:
        try:
            д = json.loads(ф.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for день, за_день in (д.get("дни") or {}).items():
            if not isinstance(за_день, dict):
                continue
            стр = из_["по_дням"].setdefault(день, {"всего": 0, "по_службам": {}})
            for служба, з in за_день.items():
                if not isinstance(з, dict):
                    continue
                try:
                    кред = int(з.get("кредитов_за_день"))
                except (TypeError, ValueError):
                    continue
                стр["по_службам"][служба] = кред
                стр["всего"] += кред
    # Последние N дней: раньше владельца интересует не история, а тренд после
    # отписки. Дни -- строки ГГГГ-ММ-ДД, сортируются как есть.
    дни = sorted(из_["по_дням"])[-max(1, int(дней)):]
    из_["по_дням"] = {д_: из_["по_дням"][д_] for д_ in дни}
    return из_


def отказы_комиссии_пула(state_dir: str, с_ts: float | None) -> dict:
    """Сколько раз потолок комиссии пула отказал в покупке -- ЧИСЛОМ.

    Текст отказа лежит не в верхнем why_not записи решения, а во вложенном
    ответе сборки, поэтому ищем его по всей строке: дешёвая проверка подстроки
    до разбора JSON. Окно -- по t_recv_ts записи; записи без времени считаются
    отдельно, чтобы не выдавать историю за сутки.
    """
    из_ = {"отказов": 0, "без_времени": 0, "строк_просмотрено": 0,
            "по_минтам": {}, "why_not": None}
    пути = sorted(glob.glob(os.path.join(state_dir, "decisions.jsonl*")))
    if not пути:
        из_["why_not"] = f"журнала решений нет: {state_dir}"
        return из_
    for путь in пути:
        # Ротированные файлы старше окна не разжимаем: журнал идёт на сотни
        # мегабайт, а хост в это время торгует.
        try:
            if с_ts and os.path.getmtime(путь) < float(с_ts) - 86400:
                continue
        except OSError:
            pass
        for с in сырые_строки(путь):
            из_["строк_просмотрено"] += 1
            if ОТКАЗ_КОМИССИИ not in с or СЛОВО_КОМИССИИ not in с:
                continue
            try:
                з = json.loads(с)
            except ValueError:
                continue
            if not isinstance(з, dict):
                continue
            т = з.get("t_recv_ts")
            if с_ts and isinstance(т, (int, float)):
                if float(т) < float(с_ts):
                    continue
            elif с_ts:
                из_["без_времени"] += 1
                continue
            из_["отказов"] += 1
            минт = з.get("mint") or "минта в записи нет"
            из_["по_минтам"][минт] = int(из_["по_минтам"].get(минт, 0)) + 1
    # В отчёт -- десять самых частых: список всех минтов был бы простынёй.
    из_["по_минтам"] = dict(sorted(из_["по_минтам"].items(),
                                    key=lambda т_: -т_[1])[:10])
    return из_


def место_в_блоке_заполнено(state_dir: str, с_ts: float | None) -> dict:
    """У скольких сделок полосы наше место в блоке ЕСТЬ, а у скольких пусто.

    Это тот же столбец, что владелец видит в строке «мы: S+N, место», и та же
    величина, которую выгрузка теперь дозаполняет по цепи (п.4в).
    """
    из_ = {"сделок": 0, "с_местом": 0, "без_места": 0, "причины": {}}
    по_cid: dict = {}
    for путь in sorted(glob.glob(os.path.join(state_dir, "positions.jsonl*"))):
        for r in (UI.строки(путь) if UI is not None else []):
            cid = r.get("client_order_id")
            if not cid:
                continue
            в = по_cid.setdefault(cid, {})
            for к, зн in r.items():
                if зн is not None:
                    в[к] = зн
    for п in по_cid.values():
        if not п.get("lane"):
            continue
        т = п.get("ts_sent") or п.get("ts_intent")
        if с_ts and (not isinstance(т, (int, float)) or float(т) < float(с_ts)):
            continue
        из_["сделок"] += 1
        if isinstance(п.get("block_index"), int):
            из_["с_местом"] += 1
        else:
            из_["без_места"] += 1
            причина = str(п.get("block_why_not") or "причины в записи нет")[:80]
            из_["причины"][причина] = int(из_["причины"].get(причина, 0)) + 1
    return из_


def cu_по_строителям(state_dir: str, с_ts: float, предел: int) -> dict:
    """CU по строителям: p50/p99 и падения по лимиту. Тем же модулем, что прогон."""
    if CU is None:
        return {"why_not": "модуль CU не загружен"}
    ключ = os.environ.get("HELIUS_API_KEY") or ""
    if not ключ:
        return {"why_not": "HELIUS_API_KEY не задан -- CU по цепи не спросить"}
    поз = CU.позиции_полосы(state_dir, с_ts)
    if not поз:
        return {"why_not": "сделок полосы в окне нет", "сделок": 0}
    узел = CU.Узел(f"https://mainnet.helius-rpc.com/?api-key={ключ}")
    подписи, чьи = [], {}
    for cid, п in поз.items():
        for с in CU.варианты_подписей(п):
            if с not in чьи:
                чьи[с] = cid
                подписи.append(с)
    севшие: dict = {}
    for н in range(0, len(подписи), 256):
        кусок = подписи[н:н + 256]
        try:
            значения = узел.статусы(кусок)
        except Exception:  # noqa: BLE001
            значения = []
        for подпись, з in zip(кусок, значения):
            if isinstance(з, dict):
                севшие.setdefault(чьи[подпись], подпись)
    по_строителям: dict = {}
    for cid, п in поз.items():
        строитель = CU.имя_строителя(п)
        запись = {"cu": None, "по_лимиту": False}
        подпись = севшие.get(cid)
        if подпись:
            try:
                meta = (узел.транзакция(подпись) or {}).get("meta") or {}
            except Exception:  # noqa: BLE001
                meta = {}
            запись["cu"] = meta.get("computeUnitsConsumed")
            текст = json.dumps({"err": meta.get("err"),
                                 "logs": meta.get("logMessages") or []},
                                ensure_ascii=False)
            запись["по_лимиту"] = any(сл in текст for сл in CU.СЛЕДЫ_ЛИМИТА)
        по_строителям.setdefault(строитель, []).append(запись)
    из_ = {"предел_cu": предел, "вызовов_узла": узел.вызовов,
            "по_строителям": {к: CU.свод(v, предел)
                               for к, v in sorted(по_строителям.items())}}
    у_предела = [к for к, с in из_["по_строителям"].items() if с.get("у_предела")]
    из_["у_предела"] = у_предела
    из_["предложение"] = {к: из_["по_строителям"][к]["предложить_предел"]
                           for к in у_предела}
    return из_


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--state-dir", default="/home/bot/bloom_executor_live_data")
    р.add_argument("--since-utc", default="",
                    help="начало окна, например 2026-09-28T00:21:00Z")
    р.add_argument("--gruppy", default="lane_s0,batch5,sniper_src")
    р.add_argument("--usage-dir", dest="usage_dir",
                    default="/home/bot/bloom_executor_live_data/helius_usage")
    р.add_argument("--dnej-kreditov", dest="dnej", type=int, default=7)
    р.add_argument("--predel-cu", dest="predel_cu", type=int,
                    default=int(os.environ.get("BLOOM_LANE_CU_LIMIT") or 140000))
    р.add_argument("--bez-seti", action="store_true",
                    help="не спрашивать цепь: CU останутся без чисел")
    р.add_argument("--out", default="/tmp/vecher_itog.json")
    а = р.parse_args()
    if UI is None:
        return 2
    since_ts = UI.разобрать_время(а.since_utc)
    о = UI.итог(а.state_dir, since_ts)
    гр = [x.strip() for x in а.gruppy.split(",") if x.strip()] or [None]
    о["строители_групп"] = {(г or "по окружению"): UI.строители(г) for г in гр}
    о["метрики_сторожа"] = UI.метрики_сторожа(а.state_dir)
    о["кредиты_за_сутки"] = UI.кредиты(а.usage_dir)
    о["кредиты_по_дням"] = кредиты_по_дням(а.usage_dir, а.dnej)
    о["сделки_полосы"] = UI.сделки_полосы(а.state_dir, since_ts)
    # СНАЙПЕР -- ОТДЕЛЬНОЙ СТРОКОЙ (слово владельца): у него свой размер, своё
    # удержание и свой порог, и в общей сумме его видно не было.
    по_группам = (о["сделки_полосы"] or {}).get("по_группам") or {}
    о["сделки_полосы"]["sniper_src_отдельно"] = по_группам.get("sniper_src")
    о["место_в_блоке"] = место_в_блоке_заполнено(а.state_dir, since_ts)
    о["отказы_по_комиссии_пула"] = отказы_комиссии_пула(а.state_dir, since_ts)
    # FREEZE AUTHORITY -- ОТВЕТ ФАКТОМ, А НЕ ЗАМЕРОМ. Гейт полосы его не
    # смотрит: ни одной проверки freeze authority на пути покупки нет. С 28.09
    # поле попадает в запись решения и в выгрузку (route_tax.freeze_authority),
    # чтобы по нему можно было считать, не меняя решения.
    о["freeze_authority"] = {
        "проверяет_ли_полоса_перед_покупкой": False,
        "где_теперь_лежит": "route_tax.freeze_authority в записи решения и "
                             "выгрузке сделок полосы",
        "лишних_вызовов_сети": 0,
    }
    if not а.bez_seti:
        о["cu_по_строителям"] = cu_по_строителям(
            а.state_dir, since_ts or (time.time() - 86400), а.predel_cu)
    else:
        о["cu_по_строителям"] = {"why_not": "сеть выключена (--bez-seti)"}
    о["окно_с"] = а.since_utc or None
    отст = о.pop("отставание", [])
    возр = о.pop("возраст_с", [])
    покуп = о.pop("отставание_полосы", [])
    о["возраст_сигналов"] = {
        "замеров_отставания": len(отст),
        "медиана_слотов": UI.кванти(отст, 0.5),
        "p90_слотов": UI.кванти(отст, 0.9),
        "медиана_возраста_с": UI.кванти(возр, 0.5),
        "p90_возраста_с": UI.кванти(возр, 0.9),
        "замеров_у_полосы": len(покуп),
        "медиана_слотов_у_полосы": UI.кванти(покуп, 0.5),
        "p90_слотов_у_полосы": UI.кванти(покуп, 0.9),
    }
    о["по_кодам"] = dict(sorted((о.get("по_кодам") or {}).items(),
                                 key=lambda t: -t[1])[:12])
    Path(а.out).write_text(json.dumps(о, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    краткое = {к: о.get(к) for к in
                ("окно_с", "сделки_полосы", "метрики_сторожа", "cu_по_строителям",
                 "место_в_блоке", "отказы_по_комиссии_пула", "кредиты_по_дням",
                 "freeze_authority")}
    print(json.dumps(краткое, ensure_ascii=False, indent=1)[:6000])
    return 0


def self_test() -> int:
    """Только счётная часть: денег здесь нет (правило 8)."""
    пройдено = провалено = 0

    def chk(что, ок, факт=""):
        nonlocal пройдено, провалено
        if ок:
            пройдено += 1
            print(f"  [ok  ] {что}")
        else:
            провалено += 1
            print(f"  [ПРОВАЛ] {что}: {факт}")

    chk("утренний модуль подключён", UI is not None)
    chk("модуль CU подключён", CU is not None)
    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "u.json").write_text(json.dumps({"дни": {
            "2026-09-26": {"detector": {"кредитов_за_день": 10}},
            "2026-09-27": {"detector": {"кредитов_за_день": 20},
                            "shred": {"кредитов_за_день": 5}},
            "2026-09-28": {"detector": {"кредитов_за_день": 30}}}},
            ensure_ascii=False), encoding="utf-8")
        к = кредиты_по_дням(d, дней=2)
        chk(f"кредиты по дням: взяты последние два ({sorted(к['по_дням'])})",
            sorted(к["по_дням"]) == ["2026-09-27", "2026-09-28"], к)
        chk("сумма дня складывается по службам",
            к["по_дням"]["2026-09-27"]["всего"] == 25, к["по_дням"])
        chk("каталога без файлов не боимся",
            кредиты_по_дням(str(Path(d) / "нет"))["why_not"] is not None)

    with tempfile.TemporaryDirectory() as d:
        п = Path(d) / "decisions.jsonl"
        строки_ = [
            json.dumps({"t_recv_ts": 2000.0, "mint": "М1", "own_send": {
                "why_not": "комиссия пула 7.64 % выше потолка 5.00 % -- "
                            "покупка не берётся"}}, ensure_ascii=False),
            json.dumps({"t_recv_ts": 500.0, "mint": "М2", "own_send": {
                "why_not": "комиссия пула 9.00 % выше потолка 5.00 % -- "
                            "покупка не берётся"}}, ensure_ascii=False),
            json.dumps({"t_recv_ts": 2100.0, "mint": "М3",
                         "why_not": "предел полосы"}, ensure_ascii=False),
        ]
        п.write_text("\n".join(строки_) + "\n", encoding="utf-8")
        о = отказы_комиссии_пула(d, 1000.0)
        chk(f"отказ по комиссии пула сосчитан один ({о['отказов']}), "
            f"старый вне окна не взят", о["отказов"] == 1 and о["по_минтам"] == {"М1": 1}, о)
        chk("чужой отказ не считается за комиссию пула",
            "М3" not in о["по_минтам"], о["по_минтам"])

    with tempfile.TemporaryDirectory() as d:
        п = Path(d) / "positions.jsonl"
        строки_ = [
            json.dumps({"client_order_id": "a", "lane": "own_send",
                         "ts_sent": 2000.0, "block_index": 5}, ensure_ascii=False),
            json.dumps({"client_order_id": "b", "lane": "own_send",
                         "ts_sent": 2000.0,
                         "block_why_not": "слот посадки не определился"},
                        ensure_ascii=False),
            json.dumps({"client_order_id": "c", "lane": "own_send",
                         "ts_sent": 10.0}, ensure_ascii=False),
        ]
        п.write_text("\n".join(строки_) + "\n", encoding="utf-8")
        м = место_в_блоке_заполнено(d, 1000.0)
        chk(f"место в блоке: 1 с местом, 1 без, старая вне окна ({м})",
            м["сделок"] == 2 and м["с_местом"] == 1 and м["без_места"] == 1, м)
        chk("причина отсутствия места названа",
            "слот посадки не определился" in "".join(м["причины"]), м["причины"])
    print(f"самопроверка вечернего итога: {пройдено}/{пройдено + провалено} пройдено")
    return 0 if провалено == 0 else 1


if __name__ == "__main__":
    raise SystemExit(self_test() if "--self-test" in sys.argv else main())
