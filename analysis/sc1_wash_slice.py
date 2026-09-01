"""SC1, дозапрос владельца (2026-09-01, п.3): срез через RPC (Alchemy/
Blockscout eth_getLogs) для применения критерия «доля объёма от
кошельков-повторников», пре-регистрированного в `docs/SC1_NOTE.md`
(«Пре-регистрация: критерий концентрации кошельков-повторников...»,
2026-09-01T21:28:48Z, ДО какого-либо измерения):

    >50% -> KILL; <20% -> главная линия; между 20% и 50% -> второй
    кластер (см. SC1_NOTE.md для полной формулировки).

СТАТУС: ЗАГОТОВКА. НЕ ЗАПУСКАЛСЯ. Требует ALCHEMY_API_KEY ИЛИ
BLOCKSCOUT_API_KEY (тот же блокер, что у P3 -- см. analysis/
alchemy_fallback.py, docs/P3_GUARD.md) -- подключён к тому же
push-триггеру `data/p3_guard_cache/STAGE_REQUEST`, что и P3
(.github/workflows/run_sc1_wash_slice.yml, ОТДЕЛЬНЫЙ файл от
run_p3_guard.yml -- P3 не тронут), поэтому реально стартует, как
только владелец добавит секрет и в следующий раз изменится тот же
маркер (тот же механизм, что уже "срабатывает вхолостую" для P3 при
каждом пуше маркера -- ключа пока нет ни у одного).

Область (по заданию владельца -- ЯВНО СУЖЕНО, не весь кластер):
топ-20 токенов кластера `0x0eaced04ec017ea0d9985b6bcd16657b5b2dac78`
ПО ОБЪЁМУ (те же 20, что в `docs/SC1_NOTE.md`, "Оценка стоимости
среза...") -- $1 181 698 их суммарного 24ч-объёма против
$31 748 336 объёма ВСЕГО кластера (1641 токен) -- то есть эта
заготовка покрывает ~3.7% объёма кластера, честно НЕ полный кластер.
Расширение на больше токенов -- отдельное решение владельца (кратно
больше eth_getLogs-вызовов, см. оценку в docstring ниже и в
SC1_NOTE.md).

============================================================
ОЦЕНКА СТОИМОСТИ (доложено владельцу, без запуска, тот же паспортный
принцип "калибровка узким срезом x2.5", что и Dune-оценка выше)
============================================================

Блочный диапазон -- по факту таймстампов/номеров блоков ЭТОГО
кластера (0 доп. запросов, из уже оплаченного
`sc1_august_launches_decoded.csv`): блоки [24 592 957; 34 788 433] --
10 195 476 блоков, что соответствует 11.82 дня активности (эмпирический
блоктайм Robinhood Chain из ЭТИХ ЖЕ данных: dt=1 021 194с /
dblocks=10 195 476 = ровно ~0.1002 с/блок, ~10 блоков/с -- согласуется
с оценкой "~100мс блоктайм", уже записанной в analysis/
alchemy_fallback.py до этого дозапроса).

Число вызовов eth_getLogs -- при chunk_size=2000 блоков/запрос (тот же
дефолт, что уже в `analysis/alchemy_fallback.py::_chunked_get_logs`):
ceil(10 195 476 / 2000) = **5 098 базовых вызовов** (нижняя граница,
ДО учёта бисекции при плотных диапазонах).

Оценка плотности событий (0 доп. запросов -- из уже оплаченного
`n_trades_24h` этих же 20 токенов, 117 768 сделок за первые 24ч,
`docs/SC1_NOTE.md`): 117 768 сделок / (86400с x 10 блоков/с = 864 000
блоков/сутки) = ~0.136 сделки/блок В СРЕДНЕМ за самые горячие первые
сутки -> ~272 события на чанк в 2000 блоков -- НИЖЕ лимита Blockscout
в 1000 записей/вызов (WebSearch, blog.blockscout.com, 2026-09-01), то
есть бисекция ожидается РЕДКО (только в отдельных особо плотных
чанках сразу после запуска, если активность там локально в разы выше
средней по суткам) -- честная оговорка: это оценка по СРЕДНЕЙ
плотности первых суток, не гарантия, что НИ ОДИН чанк не превысит
1000; активность после первых суток, по общему наблюдению для
мем-подобных токенов (см. ту же оговорку в SC1_NOTE.md про Dune-срез),
типично НИЖЕ первого дня -- то есть оценка консервативна (скорее
занижает риск бисекции, чем завышает).

**Итоговая оценка: порядка 5 000-10 000 вызовов eth_getLogs**
(нижняя граница 5098 + запас на редкую бисекцию в пиковых чанках).

Проверка по документации (WebSearch, 2026-09-01, обе -- не
дословный WebFetch, домены заблокированы для прямого фетча в этой
сессии, см. `docs/P4_RECON.md` про тот же блокер):
- **Alchemy free tier**: 30 000 000 CU/месяц, 500 CUPS
  (`alchemy.com/support/what-are-compute-units...`). `eth_getLogs`
  базово ~60 CU/вызов, но реальная стоимость растёт с объёмом
  ответа/диапазоном ("could cost thousands of CU для тяжёлых
  запросов" -- WebSearch, точная формула не найдена). Пессимистичный
  сценарий (500 CU/вызов вместо базовых 60) x 10 000 вызовов =
  5 000 000 CU = **~16.7% месячного лимита free tier** -- укладывается
  с запасом даже в пессимистичном случае. Оптимистичный сценарий
  (60 CU x 10 000) = 600 000 CU = **~2% лимита**.
- **Blockscout free tier (PRO API, dev.blockscout.com)**: 5
  запросов/сек, 1000 записей/`eth_getLogs`-вызов (WebSearch,
  blog.blockscout.com, "Blockscout vs Etherscan API... 2026"). При
  5-10к вызовах и лимите 5/с -- **~17-33 минуты** только на
  rate-limit ожидание (без параллелизации), без явного месячного
  количественного капа в найденных источниках (в отличие от Alchemy)
  -- вписывается в один прогон GH Actions (лимит job'а по умолчанию
  6 часов).

**Вывод: срез технически укладывается в бесплатный тир ЛЮБОГО из двух
источников** (Alchemy с большим запасом по CU; Blockscout -- по
времени, не по счётчику) -- ни то, ни другое не требует платного
плана. Оценка не идеальна (плотность после первых суток не измерена,
только предположена по общему паттерну) -- при реальном прогоне
скрипт сам считает фактическое число вызовов и печатает его в лог,
чтобы это предположение можно было проверить постфактум.

============================================================
Методологические оговорки (честно, не додумано):
============================================================
1. **"Кошелёк" = `recipient` из Swap-события** (второй indexed
   параметр, topics[2]) -- НЕ обязательно исходный EOA-трейдер: если
   фронтенд/роутер вызывает `pool.swap()` от своего имени, `recipient`
   может быть контрактом роутера, а не кошельком пользователя, что
   занижало бы число уникальных кошельков и искусственно завышало бы
   долю "повторников" (ложный сигнал в сторону KILL). Разрешение
   (доп. точность, доп. стоимость) -- резолвить `tx.from` через
   `eth_getTransactionByHash` по каждому уникальному `transactionHash`
   из логов; НЕ включено в базовую оценку выше (могло бы кратно
   увеличить число вызовов, вплоть до дублирования всего eth_getLogs
   бюджета) -- явно вынесено в TODO, не сделано молча.
2. **Валюта пары** -- ВСЕ 39 680 V1-запусков используют ОДИН И ТОТ ЖЕ
   `pair_token` (`0x0bd7d308f8e1639fab988df18a8011f41eacad73`, 100%
   совпадение, проверено локально по кэшу) -- по косвенным признакам
   (единый адрес на весь V1, согласуется с уже установленным
   ETH/USD-курсом через WETH-пары в `docs/SC1_NOTE.md`) это,
   по всей видимости, WETH, но **НЕ подтверждено прямым вызовом**
   (`symbol()`/`decimals()`) -- скрипт при реальном запуске делает ОДИН
   `eth_call` на этот адрес перед стартом, чтобы подтвердить/опровергнуть
   допущение явно (см. `_verify_pair_token_is_weth`), а не молчаливо
   предполагает.
3. **token0/token1 порядок** в Swap-событии определяется адресной
   сортировкой Uniswap V3 (меньший uint256-адрес = token0) --
   вычисляется локально (0 доп. запросов) сравнением `token` и
   `pair_token` как целых чисел, не запрашивается отдельно.
4. Выгружаются и коммитятся **ТОЛЬКО агрегаты** (число уникальных
   кошельков, доля объёма повторников, применённый вердикт по
   пре-регистрированному порогу) -- построчные Swap-логи/адреса
   кошельков остаются только в локальном JSON-кэше
   (`data/p3_guard_cache/sc1_wash_slice_result.json`, коммитится как
   агрегированный JSON, НЕ построчный дамп -- см. `run()`), тот же
   принцип, что и `p4_lighter_markets.py`.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alchemy_fallback import (  # noqa: E402  (см. sys.path.insert выше)
    UNISWAP_V3_SWAP_SIG,
    _auth_headers,
    _rpc_call,
    _rpc_url,
    topic0,
)
from config import CONFIG  # noqa: E402

CACHE_DIR = Path("data/p3_guard_cache")  # тот же кэш-каталог/маркер, что P3 -- см. докстринг
OUT_PATH = CACHE_DIR / "sc1_wash_slice_result.json"

CLUSTER_ID = "0x0eaced04ec017ea0d9985b6bcd16657b5b2dac78"
FROM_BLOCK = 24_592_957
TO_BLOCK = 34_788_433
PAIR_TOKEN = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"  # см. докстринг, п.2 -- предположительно WETH
ETH_USD_PRICE = 1895.565143603286  # sc1_eth_usd_price.csv, тот же источник, что весь SC1_NOTE.md

# Топ-20 токенов кластера 0x0eaced... по vol_usd_24h (docs/SC1_NOTE.md,
# "Оценка стоимости среза..."), (token, pool) -- из уже оплаченного
# sc1_august_launches_decoded.csv, 0 доп. Dune-кредитов на их получение.
TOP20_TOKEN_POOL = [
    ("0x329e6cfb85ffd0b56c3b8033d56668c4cc686950", "0xf5f8447620c1ab53108db068de8a895d7ba096ba"),
    ("0x5d37a506cb95598f939637cae1a4ced423b7a100", "0x66efc46e2e12161e61fef1f12a1418b37a3486b9"),
    ("0x988968faf4bcc51cc9758a53208f268da9318865", "0xcbfdac6c2a58cd5fe34769b5c82beb0279e26b69"),
    ("0x26f3d3f872fb461730436db5fe60083072a35bcc", "0x5e5125723dfcabda539c0e508286cb7077ac3dd1"),
    ("0x4726c8ff5f7e594464a13387a64bf217aef6e4b4", "0x32828ba1cea1b7e8795ff2d97f77a80f71a3be56"),
    ("0x48df55e5d982a770461f8ea0d77c7e6c8a136384", "0x958f22d3b5b24ce38b67d197eda1c0bd2b929af9"),
    ("0x3f06f58ba8fede100f25a597b3330def9ed1af9c", "0xc67ce041ded53bfa255feae72b829a58186c2319"),
    ("0xe40290374b7eaf3c3f2d58a2d6d84a0ee1d8d64d", "0x21bb864a8018a184ba6c6912c99f221fc176fa92"),
    ("0xe1fc3a93168934e09441fac17cc87355fcbd9697", "0x63357870edf5e732eccab5b579735cca03085f56"),
    ("0x2972f52d5dd4d060c7fca5e3c7db5f9938fed407", "0x93673c8eb73ec3805703f678aca0331ec074b5a5"),
    ("0x4846d7487dd934173f5294f1e744bffd568b7463", "0x37deb20cd4f841c40a326002c132d4b07f8f75ba"),
    ("0xeccfefa3cee624462fce8980f315121d2c036b06", "0x81b5d5e45fbbb4527b9715de54ddecbe48908853"),
    ("0xbb8bfc928e659c38e055f7e1c5868ec26d3082db", "0x6096f464ffaaba1ce51ceefa20c06d7f617ac18b"),
    ("0xd26b746ab68c7519a0c6d10dc637e0d448202d7f", "0x8bddbfaf41de7906b69dc4fa799978acd866c47a"),
    ("0x69e2c83c44fde5f323e4a3caf6ca6861b71fa79e", "0xa7e24863bb05dfd1671d37d6e1d7e1df51b3edc6"),
    ("0xed0b2a253c79c36e0fdecdc2c1cffe0a290d3f7e", "0x28450dd62469da02f8989a3f62ebd36d62be1581"),
    ("0x4d8e89bb6ec022ff256718c2eab2a07febf2b0c3", "0x206b4ca4bae9bd1e9c79bafa5a789df39fb36529"),
    ("0x69179599b459ae2773dd241aaf8bbe0fe7c57a78", "0xa0d55ca51878e021f3ea0dc0bdd24719773cac2f"),
    ("0x45d698d37765b6c4073fab0ba1c3b90dc200d7a1", "0xf28317fbc7dc18abbfac4a204a8aedfe2cfd385b"),
    ("0x13a951810efd653dcd9800880d99b385ab1fcc00", "0x7e0aacf748071491a4b4f1dd1be0c45d430a457b"),
]

# Пре-регистрированный порог (docs/SC1_NOTE.md, "Пре-регистрация:
# критерий концентрации кошельков-повторников...", 2026-09-01T21:28:48Z,
# ДО этого запуска) -- НЕ менять здесь без обновления того раздела.
KILL_THRESHOLD = 0.50
MAIN_LINE_THRESHOLD = 0.20


def _verify_pair_token_is_weth() -> str | None:
    """Один eth_call (symbol()) -- подтверждает допущение docstring п.2
    вместо молчаливого предположения. Возвращает символ или None, если
    вызов не удался (не блокирует остальной прогон -- допущение тогда
    остаётся явно НЕподтверждённым в выводе)."""
    try:
        # symbol() selector = 0x95d89b41, без аргументов
        result = _rpc_call("eth_call", [{"to": PAIR_TOKEN, "data": "0x95d89b41"}, "latest"])
        raw = bytes.fromhex(result[2:])
        # ABI-декодирование string: offset(32) + length(32) + data
        length = int.from_bytes(raw[32:64], "big")
        return raw[64 : 64 + length].decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001 -- диагностика, не критично для остального прогона
        print(f"[sc1_wash_slice] symbol() на pair_token не удался (не блокирует прогон): {e}")
        return None


def _decode_swap_data(data_hex: str) -> tuple[int, int]:
    """int256 amount0, int256 amount1 -- первые два 32-байтовых слова
    non-indexed данных Swap(...). Остальные поля (sqrtPriceX96,
    liquidity, tick) не нужны для объёма -- не декодируются."""
    raw = bytes.fromhex(data_hex[2:])
    def to_signed(word: bytes) -> int:
        v = int.from_bytes(word, "big")
        return v - (1 << 256) if v >= (1 << 255) else v
    amount0 = to_signed(raw[0:32])
    amount1 = to_signed(raw[32:64])
    return amount0, amount1


def _fetch_logs_chunked(addresses: list[str], from_block: int, to_block: int, chunk_size: int = 2000):
    """Тот же паттерн бисекции при >=1000 результатах, что
    `alchemy_fallback._chunked_get_logs`, но с явным счётчиком реальных
    вызовов (для проверки постфактум оценки выше) -- не переиспользует
    приватную функцию напрямую, т.к. там нет счётчика."""
    url = _rpc_url()
    import requests

    n_calls = 0

    def _get_range(lo: int, hi: int):
        nonlocal n_calls
        n_calls += 1
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getLogs",
            "params": [{
                "fromBlock": hex(lo), "toBlock": hex(hi),
                "address": addresses, "topics": [topic0(UNISWAP_V3_SWAP_SIG)],
            }],
        }
        resp = requests.post(url, json=payload, headers=_auth_headers(), timeout=30)
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RuntimeError(f"eth_getLogs error: {body['error']}")
        result = body.get("result", [])
        if len(result) >= 1000 and hi > lo:
            mid = (lo + hi) // 2
            yield from _get_range(lo, mid)
            yield from _get_range(mid + 1, hi)
        else:
            yield from result

    block = from_block
    while block <= to_block:
        end = min(block + chunk_size - 1, to_block)
        yield from _get_range(block, end)
        block = end + 1

    print(f"[sc1_wash_slice] фактическое число вызовов eth_getLogs: {n_calls} "
          f"(оценка в докстрине: 5000-10000)")


def run() -> int:
    token_by_pool = {pool.lower(): token.lower() for token, pool in TOP20_TOKEN_POOL}
    addresses = list(token_by_pool.keys())

    symbol = _verify_pair_token_is_weth()
    print(f"[sc1_wash_slice] pair_token {PAIR_TOKEN} symbol() = {symbol!r} "
          f"({'подтверждено WETH' if symbol and symbol.upper() in ('WETH','ETH') else 'НЕ подтверждено как WETH -- см. докстринг п.2, цифры ниже условны'})")

    # wallet -> {pool: volume_usd}, чтобы посчитать и "в скольких разных
    # токенах кластера участвовал", и объём одним проходом.
    wallet_pool_volume: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    total_volume_usd = 0.0
    n_logs = 0

    for log in _fetch_logs_chunked(addresses, FROM_BLOCK, TO_BLOCK):
        n_logs += 1
        pool = log["address"].lower()
        token = token_by_pool.get(pool)
        if token is None:
            continue
        recipient = "0x" + log["topics"][2][-40:]  # topics[2] = recipient (indexed), см. докстринг п.1
        amount0, amount1 = _decode_swap_data(log["data"])

        # token0/token1 по адресной сортировке (см. докстринг п.3) --
        # WETH-нога всегда одна и та же сторона для КОНКРЕТНОГО пула,
        # определяется сравнением token vs PAIR_TOKEN как целых чисел.
        weth_is_token0 = int(PAIR_TOKEN, 16) < int(token, 16)
        weth_amount_wei = amount0 if weth_is_token0 else amount1
        volume_usd = abs(weth_amount_wei) / 1e18 * ETH_USD_PRICE

        wallet_pool_volume[recipient][pool] += volume_usd
        total_volume_usd += volume_usd

    n_wallets = len(wallet_pool_volume)
    repeat_wallets_volume = 0.0
    n_repeat_wallets = 0
    for wallet, pools in wallet_pool_volume.items():
        if len(pools) >= 5:
            n_repeat_wallets += 1
            repeat_wallets_volume += sum(pools.values())

    share = (repeat_wallets_volume / total_volume_usd) if total_volume_usd > 0 else float("nan")
    if share > KILL_THRESHOLD:
        verdict = "KILL"
    elif share < MAIN_LINE_THRESHOLD:
        verdict = "главная линия"
    else:
        verdict = "второй кластер (неоднозначно, нужна сверка)"

    result = {
        "cluster_id": CLUSTER_ID,
        "scope": "топ-20 токенов по объёму (~3.7% объёма всего кластера, см. докстринг)",
        "from_block": FROM_BLOCK,
        "to_block": TO_BLOCK,
        "pair_token_symbol_verified": symbol,
        "n_logs": n_logs,
        "n_unique_wallets": n_wallets,
        "n_repeat_wallets_ge5_tokens": n_repeat_wallets,
        "total_volume_usd": total_volume_usd,
        "repeat_wallets_volume_usd": repeat_wallets_volume,
        "share_volume_from_repeat_wallets": share,
        "kill_threshold": KILL_THRESHOLD,
        "main_line_threshold": MAIN_LINE_THRESHOLD,
        "verdict": verdict,
    }
    print(json.dumps(result, indent=2, default=str))

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str))
    print(f"[sc1_wash_slice] записано {OUT_PATH} (только агрегаты, без построчных кошельков/логов)")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
