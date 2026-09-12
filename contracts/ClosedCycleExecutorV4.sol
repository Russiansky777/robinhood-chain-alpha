// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/*
 * ClosedCycleExecutorV4 -- V4-совместимый атомарный арбитраж по замкнутому
 * циклу из >=2 плеч через единый Uniswap V4 PoolManager (синглтон).
 *
 * НЕ АУДИРОВАН ВНЕШНЕ. НЕ РАЗВЁРНУТ. Написан для локальной fork-симуляции
 * и код-ревью (Задача 5, стадия 2 -- "адаптировать бота под подтверждённые
 * атомарные маршруты", 2026-09-12). Ни разу не подписывал и не отправлял
 * реальную транзакцию с этим байткодом -- деплой (если будет решено его
 * делать) отдельное действие владельца, с отдельным подтверждением.
 *
 * Что изменилось относительно ClosedCycleExecutorV3 (V3<->V3) и почему --
 * все пункты сверены с реальным исходником Uniswap/v4-core в этой же
 * сессии (WebFetch на raw.githubusercontent.com, не по памяти):
 *
 *  1) V3: каждый пул -- отдельный контракт, своп сам вызывает обратно
 *     uniswapV3SwapCallback за оплатой -- отсюда "различаем poolA/poolB
 *     по msg.sender" в V3-версии. V4: ОДИН синглтон-контракт PoolManager
 *     хранит все пулы; своп -- прямой вызов `poolManager.swap(...)`,
 *     ничего не зовёт нас обратно за каждое отдельное плечо. Вместо этого
 *     ВСЯ цепочка свопов идёт ВНУТРИ ОДНОГО `unlockCallback`, вызванного
 *     ОДИН раз в начале через `poolManager.unlock(data)`
 *     (IUnlockCallback.unlockCallback, interfaces/callback/IUnlockCallback.sol).
 *     Это на самом деле УПРОЩАЕТ многоногие циклы -- не нужно различать
 *     "внешний/внутренний" вызов, просто цикл по плечам в одной функции.
 *
 *  2) ЗНАК amountSpecified ИНВЕРТИРОВАН относительно V3 (реальная,
 *     легко пропускаемая ловушка, найденная в стадии-1 аудите этой
 *     сессии): V3 -- положительное = точный вход; V4 SwapParams
 *     (types/PoolOperation.sol) -- ОТРИЦАТЕЛЬНОЕ = точный вход,
 *     положительное = точный выход. Все places ниже используют V4-знак.
 *
 *  3) Оплата/получение -- не прямой ERC20-transfer, а flash-accounting:
 *     `sync(currency)` ПЕРЕД ERC20-переводом внутрь пула (пропускается
 *     для нативного ETH), затем сам transfer, затем `settle()`
 *     (payable, для ETH -- `settle{value: X}()` без предварительного
 *     transfer); получение -- `take(currency, to, amount)`. Каждая
 *     затронутая валюта ДОЛЖНА быть сведена в ноль до возврата из
 *     `unlockCallback`, иначе `unlock()` ревертит `CurrencyNotSettled`
 *     (проверено по реальному PoolManager.sol).
 *
 *  4) Многоногая цепочка СЧИТАЕТСЯ НА ЧЕЙНЕ, а не подставляется оффчейн:
 *     как и в V3-версии ("внутренний своп продаёт РОВНО то, что получено
 *     от pool A, а не весь баланс") -- amountSpecified для ВТОРОГО и
 *     последующих плеч вычисляется ВНУТРИ callback'а из РЕАЛЬНОЙ дельты
 *     предыдущего плеча (`-полученное`, точный вход), а не из
 *     заранее посчитанной оффчейн цифры. Оффчейн-квота (наш
 *     analysis/task5_v4_quote_replay.py) используется ТОЛЬКО чтобы
 *     решить, стоит ли вообще запускать цикл и с каким входным размером
 *     первого плеча -- не как источник истины для промежуточных сумм
 *     (между квотой и исполнением состояние пулов может сдвинуться).
 *
 *  5) Хуки: контракт передаёт `hookData` как есть (по умолчанию пусто --
 *     эмпирически подтверждено в этой сессии, что хук ETH/MOSIAI
 *     0xe5e702641ea86f4ae6cc3cdaed2b886f976be044 (AFTER_SWAP +
 *     AFTER_SWAP_RETURNS_DELTA, БЕЗ BEFORE_SWAP) срабатывает
 *     автоматически при пустых hookData -- реальные исторические своп-
 *     логи разложены и сверены, hook_data в квотере тоже пуст). Скальп
 *     хука (реально измерено -- ровно 2.00% в обеих контрольных
 *     транзакциях маршрута) УЖЕ отражён в BalanceDelta, которую вернёт
 *     `swap()` -- отдельно вычитать его не нужно (то же самое сошлось
 *     и в V4Quoter, см. data/task5_v4_hook_quote_check_result.json).
 *
 * Инвентарь (ERC20 ИЛИ нативный ETH) пополняется заранее (обычный
 * transfer / send на адрес контракта) -- executeCycle НЕ принимает
 * msg.value, использует уже имеющийся баланс, как и V3-версия.
 */

interface IPoolManagerMinimal {
    // Currency и BalanceDelta -- user-defined value types поверх
    // address/int256 в реальном v4-core; для внешнего интерфейса
    // достаточно базового типа -- ABI-кодирование идентично.
    function unlock(bytes calldata data) external returns (bytes memory);

    function swap(PoolKeyMinimal memory key, SwapParamsMinimal memory params, bytes calldata hookData)
        external
        returns (int256 balanceDelta);

    function sync(address currency) external;
    function settle() external payable returns (uint256 paid);
    function take(address currency, address to, uint256 amount) external;
}

struct PoolKeyMinimal {
    address currency0;
    address currency1;
    uint24 fee;
    int24 tickSpacing;
    address hooks;
}

struct SwapParamsMinimal {
    bool zeroForOne;
    int256 amountSpecified; // V4: отрицательное = точный вход, положительное = точный выход
    uint160 sqrtPriceLimitX96;
}

interface IERC20Minimal {
    function balanceOf(address account) external view returns (uint256);
    function transfer(address to, uint256 amount) external returns (bool);
}

contract ClosedCycleExecutorV4 {
    address public immutable owner;
    IPoolManagerMinimal public immutable poolManager;

    address private constant NATIVE = address(0);

    error NotOwner();
    error ReentrantCall();
    error UnexpectedCallback(address caller);
    error LegProducedNoOutput(uint256 legIndex);
    error LegCurrencyMismatch(uint256 legIndex);
    error InsufficientProfit(uint256 balanceBefore, uint256 balanceAfter, uint256 required);
    error TooManyLegs();

    bool private inCycle;

    /// Одно плечо цикла. currency0/currency1/fee/tickSpacing/hooks --
    /// ПОЛНЫЙ PoolKey именно этого пула (проверяется PoolManager'ом
    /// внутри через вычисление PoolId = keccak256(poolKey) -- если
    /// поля не совпадают с реальным зарегистрированным пулом, своп
    /// на несуществующем/пустом пуле ревертит сам по себе).
    struct LegParams {
        address currency0;
        address currency1;
        uint24 fee;
        int24 tickSpacing;
        address hooks;
        bool zeroForOne; // направление ИМЕННО в этом плече: true = отдаём currency0, получаем currency1
        uint160 sqrtPriceLimitX96;
        bytes hookData;
    }

    /// legs[0].amountSpecified берётся из firstAmountSpecified (задаёт
    /// вызывающий, V4-знак: отрицательное = точный вход). Для
    /// legs[1..], amountSpecified ВСЕГДА вычисляется на чейне из
    /// реальной дельты предыдущего плеча -- см. докстринг контракта,
    /// пункт 4.
    struct CycleParams {
        LegParams[] legs;
        int256 firstAmountSpecified;
        address exitToken; // валюта, в которой меряем прибыль (NATIVE = нулевой адрес для ETH)
        uint256 minProfit;
    }

    event CycleExecuted(address exitToken, uint256 profit, uint256 nLegs);

    constructor(address _owner, address _poolManager) {
        owner = _owner;
        poolManager = IPoolManagerMinimal(_poolManager);
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    receive() external payable {}

    function withdraw(address token, uint256 amount, address to) external onlyOwner {
        if (token == NATIVE) {
            (bool ok,) = to.call{value: amount}("");
            require(ok, "native withdraw failed");
        } else {
            require(IERC20Minimal(token).transfer(to, amount), "transfer failed");
        }
    }

    function executeCycle(CycleParams calldata p) external onlyOwner {
        if (inCycle) revert ReentrantCall();
        require(p.legs.length >= 2, "cycle needs >=2 legs");
        inCycle = true;

        uint256 balanceBefore = _balanceOf(p.exitToken, address(this));
        poolManager.unlock(abi.encode(p));
        uint256 balanceAfter = _balanceOf(p.exitToken, address(this));

        if (balanceAfter < balanceBefore + p.minProfit) {
            revert InsufficientProfit(balanceBefore, balanceAfter, p.minProfit);
        }

        emit CycleExecuted(p.exitToken, balanceAfter - balanceBefore, p.legs.length);
        inCycle = false;
    }

    /// Вызывается PoolManager'ом РОВНО один раз за цикл (см. докстринг,
    /// пункт 1) -- вся цепочка свопов и всё сведение балансов происходит
    /// здесь.
    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        if (msg.sender != address(poolManager)) revert UnexpectedCallback(msg.sender);

        CycleParams memory p = abi.decode(data, (CycleParams));
        uint256 n = p.legs.length;

        // До 2 различных валют на плечо, максимум 2*n -- на практике
        // в замкнутом цикле различных валют куда меньше (совпадающие
        // схлопываются в _accumulate), но резервируем по потолку,
        // чтобы не гадать с размером динамического массива в памяти.
        if (n > 16) revert TooManyLegs(); // разумный потолок, не производственное ограничение архитектуры
        address[] memory touchedCurrencies = new address[](2 * n);
        int256[] memory touchedDeltas = new int256[](2 * n);
        uint256 nTouched = 0;

        int256 nextAmountSpecified = p.firstAmountSpecified;

        for (uint256 i = 0; i < n; i++) {
            LegParams memory leg = p.legs[i];

            int256 rawDelta = poolManager.swap(
                PoolKeyMinimal({
                    currency0: leg.currency0,
                    currency1: leg.currency1,
                    fee: leg.fee,
                    tickSpacing: leg.tickSpacing,
                    hooks: leg.hooks
                }),
                SwapParamsMinimal({
                    zeroForOne: leg.zeroForOne,
                    amountSpecified: nextAmountSpecified,
                    sqrtPriceLimitX96: leg.sqrtPriceLimitX96
                }),
                leg.hookData
            );

            // BalanceDelta -- упакованный int256: верхние 128 бит =
            // amount0, нижние 128 бит = amount1 (types/BalanceDelta.sol,
            // сверено реальным исходником: sar(128,.) и signextend(15,.)
            // -- эквивалентно приведению типов ниже с сохранением знака).
            int128 amount0 = int128(rawDelta >> 128);
            int128 amount1 = int128(rawDelta);

            nTouched = _accumulate(touchedCurrencies, touchedDeltas, nTouched, leg.currency0, amount0);
            nTouched = _accumulate(touchedCurrencies, touchedDeltas, nTouched, leg.currency1, amount1);

            if (i + 1 < n) {
                // Выход ЭТОГО плеча -- точный вход СЛЕДУЮЩЕГО (принцип
                // V3-версии: "продаём ровно то, что получили", перенесён
                // на V4 flash-accounting -- см. докстринг, пункт 4).
                address outputCurrency = leg.zeroForOne ? leg.currency1 : leg.currency0;
                int128 outputAmount = leg.zeroForOne ? amount1 : amount0;
                if (outputAmount <= 0) revert LegProducedNoOutput(i);

                LegParams memory nextLeg = p.legs[i + 1];
                address nextInputCurrency = nextLeg.zeroForOne ? nextLeg.currency0 : nextLeg.currency1;
                if (nextInputCurrency != outputCurrency) revert LegCurrencyMismatch(i + 1);

                nextAmountSpecified = -int256(outputAmount); // V4: отрицательное = точный вход
            }
        }

        for (uint256 i = 0; i < nTouched; i++) {
            address currency = touchedCurrencies[i];
            int256 delta = touchedDeltas[i];
            if (delta < 0) {
                uint256 owed = uint256(-delta);
                if (currency == NATIVE) {
                    poolManager.settle{value: owed}();
                } else {
                    poolManager.sync(currency); // ОБЯЗАТЕЛЬНО до ERC20-перевода (см. докстринг, пункт 3); для нативного ETH не нужен
                    require(IERC20Minimal(currency).transfer(address(poolManager), owed), "settle transfer failed");
                    poolManager.settle();
                }
            } else if (delta > 0) {
                poolManager.take(currency, address(this), uint256(delta));
            }
        }

        return "";
    }

    /// Складывает дельту в уже существующую запись той же валюты
    /// (валюты повторяются между соседними плечами замкнутого цикла),
    /// иначе добавляет новую. Линейный поиск -- оправдан при <=16
    /// плечах (см. потолок выше), не производственный масштаб.
    function _accumulate(address[] memory currencies, int256[] memory deltas, uint256 count, address currency, int256 delta)
        private
        pure
        returns (uint256)
    {
        for (uint256 i = 0; i < count; i++) {
            if (currencies[i] == currency) {
                deltas[i] += delta;
                return count;
            }
        }
        currencies[count] = currency;
        deltas[count] = delta;
        return count + 1;
    }

    function _balanceOf(address token, address account) private view returns (uint256) {
        if (token == NATIVE) return account.balance;
        return IERC20Minimal(token).balanceOf(account);
    }
}
