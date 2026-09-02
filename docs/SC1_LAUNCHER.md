# SC1_LAUNCHER — разведка фабрики Pons V2 и подготовка живого запуска

**Статус (2026-09-02):** разведка контракта завершена по первоисточнику
(GitHub, `ponsdotdev/ponsfamily`). Живые ончейн-параметры (launchFee,
launchEnabled, approvedPairTokens, launchConfig) **не подтверждены из
этой интерактивной сессии** — её egress блокирует всё, кроме
`github.com` (тот же блокер, что документирован для P3/SC1 весь этот
спринт, см. `docs/P3_GUARD.md`, `docs/PROJECT_STATE.md`). Подтверждение
идёт через `analysis/sc1_v2_recon.py` на GH Actions runner'е (публичный
RPC), тем же путём, что `sc1_wash_slice.py`. Результат подставляется в
этот документ по факту прогона, не выдумывается.

## Адрес контракта

| Поколение | Контракт | Адрес | Источник |
|---|---|---|---|
| V2 | `PonsV2LaunchFactory` | `0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e` | `data/pons_family/SOURCE.md` (Sprint G1), README `ponsdotdev/ponsfamily`, двойная сверка (рендер + сырой файл) |

Chain id 4663 (Robinhood Chain), тот же чейн, что весь остальной проект.

## Архитектура (важно для газа и рисков)

Фабрика построена на **Uniswap V4** (`IPoolManager`, `IPositionManager`,
hook-контракт `PonsV2MemeHook`), не V3 как V1. Bonding-curve фаза —
**фантомные резервы** (`phantomQuote`), реальная ликвидность не
требуется при запуске — см. «Требуется ли стартовая ликвидность» ниже.

## Функция создания токена — точная сигнатура

Три перегрузки `launchToken`/`launchTokenFor` (источник — дословный
запрос содержимого `contractsV2/src/v2/PonsV2LaunchFactory.sol`):

```solidity
function launchToken(TokenParams calldata params, uint256 launchConfigId, address pairToken)
    external payable nonReentrant returns (address token, address curve)

function launchToken(TokenParams calldata params, uint256 launchConfigId, address pairToken,
    address[] calldata snipeTaxExemptions)
    external payable nonReentrant returns (address token, address curve)

function launchTokenFor(TokenParams calldata params, uint256 launchConfigId, address pairToken,
    address originalDeployer, address[] calldata snipeTaxExemptions)
    external payable nonReentrant returns (address token, address curve)
```

Используем первую (2-параметровую) перегрузку — не нужен ни список
исключений из snipe-налога, ни запуск «от имени» другого адреса.

### `TokenParams` — как задаются имя, символ, изображение, описание

```solidity
struct TokenParams {
    string name;
    string symbol;
    string logo;                 // строка -- см. ниже про формат
    string description;
    Socials socials;              // struct { twitter, telegram, discord, website, farcaster -- все string }
    address creatorFeeRecipient;  // куда идёт доля создателя из торговых комиссий
    uint16 creatorTaxBps;         // <= maxCreatorTaxBps (ончейн-параметр, см. Открытые вопросы)
    bool buybackEnabled;
    bytes32 expectedEconomics;    // см. "Защита от фронтраннинга" ниже
    bytes32 salt;                 // CREATE2 salt, namespaced per-caller
}
```

**Ответ на вопрос задания:** имя/символ/изображение/описание передаются
**параметром прямо в транзакции** (calldata `TokenParams`), НЕ отдельным
вызовом и НЕ через бэкенд-API. Источник (дословный запрос):
`PonsV2LauncherToken.sol` — «These parameters are passed directly to the
ERC20 constructor and stored without validation» — **на уровне
контракта нет проверки длины/формата** ни у `name`, ни у `symbol`, ни у
`logo`, ни у `description`. Значит формат `logo` — обычная строка (URI),
контракт её не парсит и не валидирует — задача читателя/фронтенда
интерпретировать её как ссылку на изображение. Raw-ссылка GitHub на
`.png` **формально подходит** (см. «Изображения» ниже) — контракт её
примет как любую другую строку; корректность отображения на
UI/агрегаторах Pons — отдельный, не проверенный этой сессией вопрос
(не заявляем как факт то, что не проверили).

### Требуется ли стартовая ликвидность и в каком активе

**Нет обязательной стартовой ликвидности.** Дословно из разведки:
«No Initial Liquidity Requirement: Curve deploys with only phantom
reserves; real liquidity supplied during graduation phase» — реальная
ликвидность появляется только при градуации (переход на AMM-пул), не
при запуске. Единственный обязательный платёж — `launchFee`:

```solidity
if (msg.value != launchFee) revert LaunchFeeNotPaid();
```

**msg.value должен быть РОВНО равен `launchFee`** — в отличие от V1
(где остаток `msg.value - launchFee` был опциональной seed-покупкой),
V2 **не поддерживает** опциональный buy в самом `launchToken()` —
превышение или недостача `launchFee` откатывает транзакцию целиком.
Отдельная (опциональная) стартовая покупка после запуска потребовала
бы отдельного вызова к пулу/кривой — не входит в `launchToken()`.

`pairToken` — актив бондинг-кривой (аналог WETH-пары в V1), должен
быть в списке `approvedPairTokens[pairToken] == true`, иначе
`revert PairTokenNotApproved()`. Какой именно адрес одобрен —
эмпирический вопрос, см. «Открытые вопросы» ниже (скорее всего WETH,
как во всех 39680 V1-запусках, но не подтверждено вызовом для V2).

### Защита от фронтраннинга (`expectedEconomics`) — важно для скрипта

```solidity
bytes32 economics = _economicsDigest(config, policy, phantomQuote, graduationThreshold);
if (params.expectedEconomics != bytes32(0) && params.expectedEconomics != economics) {
    revert LaunchEconomicsMismatch(params.expectedEconomics, economics);
}
```

`expectedEconomics` — keccak256-дайджест текущих экономических
параметров конфига (phantomQuote, graduationThreshold, supply,
curveFeeBps, poolFee, tickSpacing, protocolFeeShareBps, buybackBurnBps,
hookFeeBps, maxInternalPriceImpactBps), получаемый вызовом
`previewLaunchEconomics(launchConfigId, pairToken)` **непосредственно
перед отправкой** — если передать `bytes32(0)`, проверка отключается
(риск: экономика конфига могла измениться между построением tx и её
включением в блок, но это не блокирует запуск техничеки). Скрипт
(`analysis/sc1_launcher.py`) вызывает `previewLaunchEconomics` перед
каждой отправкой и подставляет актуальный дайджест — не полагается на
`bytes32(0)`.

### Право на запуск — ВАЖНО, может блокировать всё

```solidity
function canLaunch(address launcher) public view returns (bool) {
    return launchEnabled || whitelistedLaunchers[launcher];
}
```

Запуск **не обязательно permissionless** — либо публичные запуски
включены глобально (`launchEnabled == true`), либо наш адрес должен
быть явно добавлен в `whitelistedLaunchers`. Это первое, что проверяет
`sc1_v2_recon.py`/`sc1_launcher.py` (`canLaunch(0x893f4a7e...)`) —
**до** любой попытки оценки газа, потому что при `false` любая
`eth_estimateGas`/реальная отправка откатится с `NotWhitelisted()`, и
дальнейшие шаги бессмысленны, пока это не подтверждено.

## Прочие обязательные/опциональные параметры

| поле | обязательность | смысл |
|---|---|---|
| `launchConfigId` | обязателен | индекс в `_launchConfigs[]` — определяет supply/curveFeeBps/poolFee/graduationThreshold. Нужно перечислить `launchConfigCount()`/`getLaunchConfig(i)` и выбрать `enabled == true` конфиг |
| `pairToken` | обязателен | должен быть в `approvedPairTokens` |
| `creatorFeeRecipient` | обязателен (адрес) | куда уходит доля создателя из торговых комиссий — используем наш кошелёк `0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75` |
| `creatorTaxBps` | обязателен, `<= maxCreatorTaxBps` | доля создателя сверх hook-комиссии, ончейн-потолок (см. открытые вопросы) — используем 0, если нет причин иначе (минимизирует трение для покупателей) |
| `buybackEnabled` | обязателен (bool) | включает автобайбек из `buybackVault` — используем `false` по умолчанию (не проверено, что это даёт создателю, не додумываем экономику без источника) |
| `salt` | обязателен | CREATE2 salt, namespaced per-caller — можно `keccak256(symbol+timestamp)`, уникальность нужна только в рамках НАШИХ собственных запусков |

## Изображения (Задача 3)

20 PNG перенесены `01_NVX.png … 20_FRM.png` → `assets/logos/`. Символ
токена — вторая часть имени файла (`NVX`, `QRT`, `BLZ`, …, `FRM`).
Формат: **512×512, 8-bit RGBA PNG у всех 20 файлов без исключения**
(`file assets/logos/*.png`, проверено локально) — единообразный
квадратный формат, стандартный для токен-иконок; контракт сам формат
не проверяет (см. выше), риск несовместимости — только со стороны
UI Pons (не проверено этой сессией, не заявляем как факт).

Raw-ссылки GitHub (после пуша в `claude/sc1-pons-v2-launcher-efdbyt`):

```
https://raw.githubusercontent.com/Russiansky777/robinhood-chain-alpha/claude/sc1-pons-v2-launcher-efdbyt/assets/logos/01_NVX.png
...
https://raw.githubusercontent.com/Russiansky777/robinhood-chain-alpha/claude/sc1-pons-v2-launcher-efdbyt/assets/logos/20_FRM.png
```

**Оговорка о стабильности ссылки:** ссылка на ветку ломается, если
ветка будет удалена/переименована после мержа (обычная практика после
squash-merge PR). Для реального использования в `logo`-поле
транзакции безопаснее ссылка **на конкретный commit SHA**
(`.../<sha>/assets/logos/...` — не меняется, пока объект существует в
истории репозитория) или перенос файлов в дефолтную ветку. Скрипт
`sc1_launcher.py` формирует ссылку по SHA текущего HEAD на момент
запуска, не по имени ветки — см. `_logo_url()`.

## Открытые вопросы (эмпирические, требуют живого `eth_call`, не додуманы)

Заполняется `analysis/sc1_v2_recon.py` по факту прогона на GH Actions
(маркер `data/p3_guard_cache/SC1_V2_RECON_REQUEST`):

- `launchFee` (wei) — точное значение, аналог V1 `0.0005 ETH`, НЕ
  предполагается равным без вызова `launchFee()`.
- `launchEnabled` — публичные запуски открыты или нужен whitelist.
- `canLaunch(0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75)` — можем ли мы
  вообще запускать.
- `launchConfigCount()` и содержимое `getLaunchConfig(0..N-1)` — какие
  конфиги существуют, какой `enabled`.
- `approvedPairTokens(WETH)` — подтверждение, что WETH (или какой
  именно адрес) одобрен как `pairToken`.
- `maxCreatorTaxBps()` — потолок для `creatorTaxBps`.

<!-- SC1_V2_RECON_RESULT -->

## Результат живого прогона (`analysis/sc1_v2_recon.py`, GH Actions, публичный RPC)

Подтверждено вызовами `eth_call` к `0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e` (chainId=4663):

| параметр | значение |
|---|---|
| `launchFee()` | **500000000000000 wei = 0.00050000 ETH** |
| `launchEnabled()` | **True** |
| `whitelistedLaunchers(0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75)` | False |
| `canLaunch(0x893f4a7eADBa18c2f8aA1e0E23e11eCF66208e75)` | **True** |
| `maxCreatorTaxBps()` | 1000 |
| `launchConfigCount()` | 1 |
| `approvedPairTokens(0x0bd7d308f8e1639fab988df18a8011f41eacad73)` (кандидат WETH, из V1) | **False** |

### launchConfig'и

| id | supply | curveFeeBps | phantomQuote | graduationThreshold | poolFee | tickSpacing | enabled |
|---|---|---|---|---|---|---|---|
| 0 | 1000000000000000000000000000 | 100 | 1680000000000000000 | 4200000000000000000 | 0 | 200 | True |

Артефакт: `data/p3_guard_cache/sc1_v2_recon_result.json`.

