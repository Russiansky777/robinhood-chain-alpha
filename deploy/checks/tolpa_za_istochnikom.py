#!/usr/bin/env python3
"""Толпа за источником на НАШИХ сделках: кто ещё брал тот же минт рядом с нами.

ЗАЧЕМ (задание владельца 27.09, пункт 1). По сделкам полосы -- по цепи:
  (а) для наших S+0: сколько чужих покупок того же минта стоит в блоке МЕЖДУ
      транзакцией источника и нашей и на сколько SOL;
  (б) для всех сделок: сколько чужих покупок того же минта после источника в его
      слоте, в слоте+1 и в слоте+2, и их SOL;
  (в) адреса покупателей в слоте и слоте+1, встречающиеся у трёх и более разных
      событий: их число и топ-20 по частоте.

КАК СЧИТАЕТСЯ ПОКУПКА. Прирост остатка ЭТОГО минта у кошелька в транзакции,
которая не упала. Свои кошельки (исполнитель, полоса) и кошелёк источника
исключаются: себя и того, за кем идём, в "толпе" быть не может.

КАК СЧИТАЕТСЯ SOL. Трата покупателя в этой же транзакции: убыль нативного SOL
плюс убыль WSOL по его счетам. Это ВЕРХНЯЯ оценка: в нативную убыль входят и
комиссия с приоритетом, и чаевые. Числа честнее не сделать, не разбирая
инструкции каждого свопа, поэтому оговорка стоит и в отчёте.

Блоки берутся с transactionDetails=accounts: балансы там есть, а инструкций и
логов нет -- это в разы меньше байт, чем full. Один слот запрашивается один раз.
Измерительный код, тестов нет (правило 8 владельца).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(КОРЕНЬ / "analysis"))

WSOL = "So11111111111111111111111111111111111111112"
ЛАМПОРТОВ_В_SOL = 1_000_000_000
ПОРОГ_ЧАСТОТЫ = 3          # "встречается у трёх и более разных событий"
ТОП = 20


def квантиль(значения: list, доля: float):
    if not значения:
        return None
    з = sorted(значения)
    и = min(len(з) - 1, max(0, int(round(доля * (len(з) - 1)))))
    return з[и]


def свод_чисел(значения: list) -> dict:
    чист = [x for x in значения if x is not None]
    return {"n": len(чист),
            "медиана": (round(statistics.median(чист), 6) if чист else None),
            "p90": (round(квантиль(чист, 0.9), 6) if чист else None),
            "сумма": (round(sum(чист), 6) if чист else None),
            "максимум": (round(max(чист), 6) if чист else None)}


def разобрать_блок(блок: dict) -> list:
    """[{index, signature, покупки: {минт: {владелец: прирост}}, трата: {владелец: лампорты}}]."""
    из_ = []
    for и, tx in enumerate((блок or {}).get("transactions") or []):
        мета = (tx or {}).get("meta") or {}
        подписи = ((tx or {}).get("transaction") or {}).get("signatures") or []
        строка = {"index": и, "signature": (подписи[0] if подписи else None),
                  "покупки": {}, "трата": {}, "упала": мета.get("err") is not None}
        if строка["упала"]:
            из_.append(строка)
            continue
        # transactionDetails=accounts кладёт ключи ПРЯМО в transaction, а
        # full -- в transaction.message. Читаем оба вида: иначе трата толпы
        # выйдет нулём и это будет выглядеть как "толпа не платит".
        тр = (tx or {}).get("transaction") or {}
        сырые_ключи = тр.get("accountKeys")
        if сырые_ключи is None:
            сырые_ключи = (тр.get("message") or {}).get("accountKeys") or []
        ключи = [(k.get("pubkey") if isinstance(k, dict) else k)
                 for k in сырые_ключи]
        # Нативная убыль по счетам транзакции -- по индексам ключей.
        до, после = мета.get("preBalances") or [], мета.get("postBalances") or []
        for и_к, ключ in enumerate(ключи):
            if и_к < len(до) and и_к < len(после):
                д = после[и_к] - до[и_к]
                if д < 0:
                    строка["трата"][ключ] = строка["трата"].get(ключ, 0) - д
        # Токеновые движения: прирост минта -- покупка, убыль WSOL -- трата.
        токены: dict = {}
        for поле in ("preTokenBalances", "postTokenBalances"):
            for з in (мета.get(поле) or []):
                ключ = (з.get("accountIndex"), з.get("mint"), з.get("owner"))
                сумма = ((з.get("uiTokenAmount") or {}).get("amount")) or "0"
                try:
                    токены.setdefault(ключ, {})[поле] = int(сумма)
                except (TypeError, ValueError):
                    continue
        for (_, минт, владелец), пара in токены.items():
            if not владелец or not минт:
                continue
            д = пара.get("postTokenBalances", 0) - пара.get("preTokenBalances", 0)
            if д > 0:
                по_минту = строка["покупки"].setdefault(минт, {})
                по_минту[владелец] = по_минту.get(владелец, 0) + д
            elif д < 0 and минт == WSOL:
                строка["трата"][владелец] = строка["трата"].get(владелец, 0) - д
        из_.append(строка)
    return из_


def чужие_покупки(разбор: list, *, минт: str, свои: set, с_индекса: int | None,
                   до_индекса: int | None) -> dict:
    """Покупки минта чужими кошельками в окне индексов (границы исключаются)."""
    покупок = 0
    sol = 0
    адреса: set = set()
    for с in разбор:
        if с["упала"]:
            continue
        if с_индекса is not None and с["index"] <= с_индекса:
            continue
        if до_индекса is not None and с["index"] >= до_индекса:
            continue
        владельцы = [в for в in (с["покупки"].get(минт) or {}) if в not in свои]
        if not владельцы:
            continue
        покупок += 1
        адреса.update(владельцы)
        for в in владельцы:
            sol += с["трата"].get(в, 0)
    return {"покупок": покупок, "sol": round(sol / ЛАМПОРТОВ_В_SOL, 6),
            "адреса": адреса}


def main() -> int:
    р = argparse.ArgumentParser()
    р.add_argument("--rows", required=True, help="выгрузка сделок полосы (JSON)")
    р.add_argument("--out", default=str(КОРЕНЬ / "data" / "tolpa_za_istochnikom.json"))
    р.add_argument("--limit", type=int, default=0, help="0 -- все сделки")
    р.add_argument("--credit-budget", type=int, default=250_000,
                   help="потолок кредитов Helius на прогон")
    # СВОИ КОШЕЛЬКИ -- СПИСКОМ, А НЕ ИЗ ПОЗИЦИИ. В позиции полосы поля
    # lane_wallet может не быть (первый прогон 27.09: наш кошелёк полосы попал
    # в "толпу" 148 раз из 152 и завысил числа s0 и s1).
    р.add_argument("--svoi", default="",
                   help="наши кошельки через запятую -- исключаются из толпы")
    а = р.parse_args()

    from solana_crowd_scan import Rpc, helius_key  # noqa: PLC0415

    д = json.loads(Path(а.rows).read_text(encoding="utf-8"))
    ряды = [r for r in (д.get("ряды") or д)
            if r.get("mint") and isinstance(r.get("source_slot"), int)]
    if а.limit:
        ряды = ряды[:а.limit]
    ключ, откуда = helius_key()
    rpc = Rpc(ключ, service="tolpa_za_istochnikom", workers=4)
    свои_общие = set()
    try:
        import bloom_exec_state as ST  # noqa: PLC0415
        свои_общие.add(ST.EXECUTOR_WALLET)
    except Exception:  # noqa: BLE001
        pass
    for r in ряды:
        if r.get("wallet"):
            свои_общие.add(r["wallet"])
    for адрес in (а.svoi or "").replace(";", ",").split(","):
        if адрес.strip():
            свои_общие.add(адрес.strip())
    print(f"ключ Helius из {откуда}; сделок к разбору: {len(ряды)}; "
          f"своих кошельков: {len(свои_общие)}")

    кэш: dict = {}

    def блок(слот: int):
        if слот in кэш:
            return кэш[слот]
        если_нет = {"известен": False, "разбор": []}
        try:
            # ВЕРСИЯ ТРАНЗАКЦИЙ -- 1, а не 0: с нулём узел отвечает ошибкой
            # -32015 на любом блоке, где есть версионная транзакция, то есть
            # практически на каждом. Первый прогон 27.09 так и упал -- все 152
            # слота отдали ошибку, и числа вышли пустыми.
            б = rpc.call("getBlock", [слот, {
                "encoding": "jsonParsed", "transactionDetails": "accounts",
                "rewards": False, "maxSupportedTransactionVersion": 1,
                "commitment": "confirmed"}])
        except Exception as exc:  # noqa: BLE001
            кэш[слот] = dict(если_нет, why_not=f"{type(exc).__name__}: {str(exc)[:120]}")
            return кэш[слот]
        if not б or б.get("transactions") is None:
            кэш[слот] = dict(если_нет, why_not="узел не отдал блок")
            return кэш[слот]
        кэш[слот] = {"известен": True, "разбор": разобрать_блок(б),
                     "всего": len(б.get("transactions") or [])}
        return кэш[слот]

    итоги = []
    начало = time.time()
    частота: dict = {}
    for и, r in enumerate(ряды, 1):
        s0 = int(r["source_slot"])
        минт = r["mint"]
        свои = set(свои_общие)
        if r.get("source"):
            свои.add(r["source"])
        строка = {"cid": r.get("cid"), "utc": r.get("utc"), "mint": минт,
                  "group": r.get("group"), "size_sol": r.get("size_sol"),
                  "sol_in": r.get("sol_in"), "closed_sol_net": r.get("closed_sol_net"),
                  "итог_sol": r.get("итог_sol"), "расход_sol": r.get("расход_sol"),
                  "state": r.get("state"),
                  "source_slot": s0, "our_slot": r.get("our_slot"),
                  "s0_равен_нашему": r.get("our_slot") == s0}
        б0 = блок(s0)
        if not б0["известен"]:
            строка["why_not"] = б0.get("why_not")
            итоги.append(строка)
            continue
        # Индексы источника и наш -- по подписям, а не по полям журнала: журнал
        # мог не успеть их записать, а блок знает точно.
        и_ист = next((с["index"] for с in б0["разбор"]
                      if с["signature"] == r.get("source_sig")), None)
        # КОШЕЛЁК ИСТОЧНИКА -- ИЗ ЕГО ЖЕ ТРАНЗАКЦИИ. В позиции полосы поля
        # "source" нет вовсе (write_intent его не пишет), а исключать источник
        # из толпы обязательно: иначе он попадёт в неё сам. Берём тех, кто
        # получил этот минт в транзакции источника.
        if и_ист is not None:
            своя_tx = next((с for с in б0["разбор"] if с["index"] == и_ист), None)
            покупатели_ист = sorted(((своя_tx or {}).get("покупки") or {}).get(минт, {}))
            строка["источник_адреса"] = покупатели_ист
            for владелец in покупатели_ист:
                свои.add(владелец)
        наши = set(r.get("our_signatures") or [])
        и_наш = next((с["index"] for с in б0["разбор"]
                      if с["signature"] in наши), None)
        строка.update(source_index=и_ист, our_index_in_s0=и_наш,
                      s0_всего=б0.get("всего"))
        if и_ист is None:
            строка["why_not"] = "транзакции источника в его слоте не нашли"
            итоги.append(строка)
            continue
        # (а) МЕЖДУ источником и нами -- только для S+0.
        if и_наш is not None:
            между = чужие_покупки(б0["разбор"], минт=минт, свои=свои,
                                   с_индекса=и_ист, до_индекса=и_наш)
            строка["между_покупок"] = между["покупок"]
            строка["между_sol"] = между["sol"]
        # ПОСЛЕ НАС в s0 -- отдельное число: "до нас" и "после нас" отвечают на
        # разные вопросы (кого мы не успели обогнать и кто пришёл за нами).
        if и_наш is not None:
            после_нас = чужие_покупки(б0["разбор"], минт=минт, свои=свои,
                                       с_индекса=и_наш, до_индекса=None)
            строка["после_нас_s0_покупок"] = после_нас["покупок"]
            строка["после_нас_s0_sol"] = после_нас["sol"]
        # (б) после источника в s0, затем s0+1 и s0+2 целиком.
        после0 = чужие_покупки(б0["разбор"], минт=минт, свои=свои,
                                с_индекса=и_ист, до_индекса=None)
        строка["s0_покупок"], строка["s0_sol"] = после0["покупок"], после0["sol"]
        адреса_окна = set(после0["адреса"])
        for сдвиг, имя in ((1, "s1"), (2, "s2")):
            б = блок(s0 + сдвиг)
            if not б["известен"]:
                строка[f"{имя}_покупок"] = None
                строка[f"{имя}_sol"] = None
                строка[f"{имя}_why_not"] = б.get("why_not")
                continue
            рез = чужие_покупки(б["разбор"], минт=минт, свои=свои,
                                 с_индекса=None, до_индекса=None)
            строка[f"{имя}_покупок"], строка[f"{имя}_sol"] = рез["покупок"], рез["sol"]
            if сдвиг == 1:
                адреса_окна |= рез["адреса"]
        # (в) частота адресов по РАЗНЫМ событиям (одно событие -- одна сделка).
        for адрес in адреса_окна:
            частота[адрес] = частота.get(адрес, 0) + 1
        итоги.append(строка)
        if и % 10 == 0 or и == len(ряды):
            print(f"  {и}/{len(ряды)} слотов в кэше {len(кэш)}, вызовов {rpc.calls}")
        if rpc.calls * 10 > а.credit_budget:
            print(f"СТОП по бюджету: вызовов {rpc.calls}", file=sys.stderr)
            break

    s0_равные = [с for с in итоги if с.get("между_покупок") is not None]
    свод = {
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "сделок_разобрано": len(итоги),
        "сделок_с_нашим_индексом_в_слоте_источника": len(s0_равные),
        "вызовов_rpc": rpc.calls, "слотов_прочитано": len(кэш),
        "секунд": round(time.time() - начало, 1),
        "после_нас": {
            "s0_покупок": свод_чисел([с.get("после_нас_s0_покупок") for с in итоги
                                      if с.get("после_нас_s0_покупок") is not None]),
            "s0_sol": свод_чисел([с.get("после_нас_s0_sol") for с in итоги
                                  if с.get("после_нас_s0_sol") is not None])},
        "а_между_источником_и_нами": {
            "покупок": свод_чисел([с["между_покупок"] for с in s0_равные]),
            "sol": свод_чисел([с["между_sol"] for с in s0_равные])},
        "б_после_источника": {
            "s0_покупок": свод_чисел([с.get("s0_покупок") for с in итоги]),
            "s0_sol": свод_чисел([с.get("s0_sol") for с in итоги]),
            "s1_покупок": свод_чисел([с.get("s1_покупок") for с in итоги]),
            "s1_sol": свод_чисел([с.get("s1_sol") for с in итоги]),
            "s2_покупок": свод_чисел([с.get("s2_покупок") for с in итоги]),
            "s2_sol": свод_чисел([с.get("s2_sol") for с in итоги])},
        "в_частые_адреса": {
            "порог_событий": ПОРОГ_ЧАСТОТЫ,
            "адресов_всего": len(частота),
            "адресов_от_порога": sum(1 for v in частота.values() if v >= ПОРОГ_ЧАСТОТЫ),
            "топ": [{"адрес": а_, "событий": n} for а_, n in
                     sorted(частота.items(), key=lambda x: (-x[1], x[0]))[:ТОП]]},
        "оговорка": ("SOL толпы -- верхняя оценка: в убыль нативного SOL входят "
                      "комиссия, приоритет и чаевые покупателя"),
        "ряды": итоги,
    }
    Path(а.out).write_text(json.dumps(свод, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    print(json.dumps({к: v for к, v in свод.items() if к != "ряды"},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
