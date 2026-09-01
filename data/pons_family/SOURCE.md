# pons.family factory: адреса, ABI, источники (Sprint G1)

**Зафиксировано 2026-09-01, до любых Dune-запросов по адресу.**

## Адреса фабрик (chain id 4663, Robinhood Chain)

| Поколение | Контракт | Адрес |
|---|---|---|
| V1 | `PonsLaunchFactory` | `0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB` |
| V2 | `PonsV2LaunchFactory` | `0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e` |

**Источник:** [github.com/ponsdotdev/ponsfamily](https://github.com/ponsdotdev/ponsfamily),
README.md, таблица "Deployed factories". Оба адреса подтверждены ДВУМЯ
независимыми запросами (рендер страницы репозитория и сырой файл
`raw.githubusercontent.com/ponsdotdev/ponsfamily/main/README.md`) — оба
дали идентичные значения.

**Не использован:** WebSearch (агрегированный ответ, не прямая страница)
отдельно назвал третий адрес как «legacy factory»
(`0x0c37a24F5D23A486FA692d1500881d698B1F77a4`), который НЕ подтвердился
на первоисточнике — сознательно не используется нигде в пайплайне.

## ABI (событие градуации)

`PonsLaunchFactory_v1_abi.json` в этой директории — источник:
[raw.githubusercontent.com/ponsdotdev/ponsfamily/main/abi.json](https://raw.githubusercontent.com/ponsdotdev/ponsfamily/main/abi.json),
запрошен явно как "верните сырой файл дословно, без суммаризации".
**Абревиатура:** секции `function` с крупными вложенными tuple-параметрами
(`launchToken`, `predictTokenAddress`, `predictVanityTokenAddress`,
`getDexConfig`, `getLaunchConfig`, `getLaunchedToken`, `addDexConfig`,
`addLaunchConfig`, `updateLaunchConfig`, `hasVanitySuffix`) сокращены до
сигнатуры без вложенных структур ради читаемости файла в репозитории —
**все `event` и `error` записи сохранены полностью**, это единственное,
что нужно для детекции по логам. Полный файл — по ссылке выше.

Ключевое событие (эмитится фабрикой в момент градуации — переход с
bonding curve на AMM-пул):

```
event TokenLaunched(
    address indexed token,
    address indexed deployer,
    address indexed dexFactory,
    address pairToken,
    address pool,               -- адрес созданного AMM-пула
    uint256 dexId,               -- см. DexConfigAdded -- запуски МОГУТ идти
                                  --  в разные DEX, не хардкодить uniswap
    uint256 launchConfigId,
    uint256 positionId,
    uint256 restrictionsEndBlock,-- см. распределение в docs/G1_DESIGN.md §2.9
    uint256 initialBuyAmount
)
```

Предшествующее событие (создание бондинг-кривой, ДО градуации):
```
event TokenDeployed(
    address indexed token,
    address indexed deployer,
    address indexed dexFactory,
    address pairToken,
    uint256 dexId,
    uint256 launchConfigId
)
```

## topic0 (Keccak-256 сигнатуры события)

Посчитаны локально (`Crypto.Hash.keccak`, `analysis/requirements.txt` →
`pycryptodome`), НЕ угаданы и НЕ взяты из внешнего источника —
детерминированная функция от точной сигнатуры типов выше:

```python
from Crypto.Hash import keccak
h = keccak.new(digest_bits=256)
h.update(b"TokenLaunched(address,address,address,address,address,uint256,uint256,uint256,uint256,uint256)")
h.hexdigest()
```

- `TokenLaunched(...)` → `0xdb51ea9ad51ab453a65a4cb7e60c3cb378c9501bb002609f8f97778fb6c4235a` (64 hex-символа после `0x`, проверено `len(hexdigest)==64`)
- `TokenDeployed(...)` → `0x1461370115e1c2be79cb529f8cfcbd11316e789d9c6099fc83417b0b4c48c62a` (то же)

**ВАЖНО:** значения выше используются ТОЛЬКО как гипотеза для эмпирической
проверки — они НЕ подставляются вслепую в WHERE topic0=... на Dune. Шаг
1 recon-пробника (`sql/g1/g1_factory_logs_topic0_probe.sql`) группирует
СЫРЫЕ логи фабрик по topic0 БЕЗ фильтра по конкретному значению и
сверяет, какой topic0 фактически доминирует и соответствует ли он
посчитанному здесь хэшу — так подтверждение идёт и по адресу (есть ли
вообще логи), и по сигнатуре (совпадает ли посчитанный хэш с реально
наблюдаемым), independently.

## Честная оценка надёжности источника

`WebFetch` обрабатывает страницу через LLM-суммаризатор — даже запрос
"верните дословно" не даёт гарантии побайтовой точности, только снижает
риск. Два независимых фетча (рендер репозитория и сырой README) дали
идентичные адреса — это сильнее одного ответа, но не эквивалентно
проверке верифицированного bytecode на эксплорере (заблокирован
сетевым прокси из этой сессии, см. `docs/DATA_ACCESS.md`). Финальная,
самая сильная проверка — ончейн: сырые логи с `contract_address` в
списке выше ЕСТЬ на chain 4663 (не ноль строк) И среди них есть строки с
topic0, совпадающим с посчитанным здесь хэшем И у одного из этих логов
декодированный параметр `pool` действительно имеет своп-активность в
`dex.trades`/`query_02_swaps_raw_july` — см. `docs/G1_DESIGN.md`,
"Механика детекции", результат этой проверки.
