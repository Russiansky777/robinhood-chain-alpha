// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/*
 * Задача 5, владелец 2026-09-11/12: атомарный исполнитель закрытого
 * цикла между ДВУМЯ Uniswap V3 пулами одной пары токенов (пункт 4
 * спецификации: "атомарная транзакция с откатом при убытке -- в самой
 * транзакции, не в боте").
 *
 * ЧЕСТНАЯ ОГОВОРКА (не скрывать): этот контракт поддерживает ТОЛЬКО
 * V3-V3 циклы (flash-swap между двумя обычными пулами). Uniswap V4 на
 * этой цепи -- singleton PoolManager с callback-архитектурой
 * (unlock/unlockCallback, settle/take) -- ЗНАЧИТЕЛЬНО больший объём
 * кода, НЕ реализовано в этой версии. Первая неделя dry-run (см. план
 * в PROJECT_STATE.md) фокусируется на V3-V3 парах -- это уже покрывает
 * реальную часть Задачи 5 (canonical v3 пулы, WETH/USDG). V4-поддержка
 * -- отдельная, более поздняя итерация, если V3-V3 покажет себя на
 * тесте.
 *
 * НЕ АУДИРОВАН. Перед реальной отправкой ЛЮБЫХ денег (даже $2-3k) --
 * ручной код-ревью и тестнет-прогон (chain id 46630), как и написано в
 * плане по неделям.
 *
 * Механизм: владелец инициирует flash-swap в pool A (заимствуя token0
 * или token1), в колбэке V3 (`uniswapV3SwapCallback`) исполняется
 * встречный своп в pool B, из выручки которого гасится долг перед pool
 * A. После возврата из колбэка проверяется, что баланс контракта в
 * exitToken вырос как минимум на minProfit -- если нет, вся
 * транзакция откатывается целиком (EVM revert), инвентарь контракта
 * не может быть потерян сверх газа неудачной попытки.
 */

interface IUniswapV3PoolMinimal {
    function swap(
        address recipient,
        bool zeroForOne,
        int256 amountSpecified,
        uint160 sqrtPriceLimitX96,
        bytes calldata data
    ) external returns (int256 amount0, int256 amount1);

    function token0() external view returns (address);
    function token1() external view returns (address);
}

interface IERC20Minimal {
    function balanceOf(address account) external view returns (uint256);
    function transfer(address to, uint256 amount) external returns (bool);
    function approve(address spender, uint256 amount) external returns (bool);
}

contract ClosedCycleExecutorV3 {
    address public immutable owner;

    error NotOwner();
    error InsufficientProfit(uint256 balanceBefore, uint256 balanceAfter, uint256 required);
    error UnexpectedCallback(address caller);
    error ReentrantCall();

    // Заполняется непосредственно перед flash-swap, обнуляется после --
    // uniswapV3SwapCallback обязан прийти ТОЛЬКО от ожидаемого pool A,
    // иначе произвольный контракт мог бы подделать колбэк и вытащить
    // средства.
    address private expectedCallbackPool;
    bool private inCycle;

    struct CycleParams {
        address poolA;          // пул, где занимаем (flash-swap leg)
        address poolB;          // пул, где закрываем встречным свопом
        bool zeroForOneA;       // направление свопа в pool A
        int256 amountSpecifiedA; // отрицательное = exact output заимствования
        address exitToken;      // токен, в котором меряем итоговую прибыль (WETH или USDG)
        uint256 minProfit;      // минимальный прирост баланса exitToken, иначе revert
        uint160 sqrtPriceLimitA;
        uint160 sqrtPriceLimitB;
    }

    // передаётся через data в колбэк -- какой pool B и как в нём торговать,
    // чтобы закрыть долг перед pool A.
    struct CallbackData {
        address poolB;
        address tokenOwedToPoolA;   // токен, который нужно вернуть pool A
        uint256 amountOwedToPoolA;  // абсолютная величина долга (из amount0/amount1 колбэка)
        bool zeroForOneB;
        uint160 sqrtPriceLimitB;
    }

    event CycleExecuted(address indexed poolA, address indexed poolB, address exitToken, uint256 profit);

    constructor(address _owner) {
        owner = _owner;
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    /// @notice Владелец пополняет инвентарь контракта (WETH/USDG) обычным ERC20-переводом на этот адрес
    /// заранее -- explicit deposit-функции не нужно, transfer() на адрес контракта достаточен.

    function withdraw(address token, uint256 amount, address to) external onlyOwner {
        IERC20Minimal(token).transfer(to, amount);
    }

    /// @notice Основная точка входа бота -- один атомарный вызов на попытку.
    function executeCycle(CycleParams calldata p) external onlyOwner {
        if (inCycle) revert ReentrantCall();
        inCycle = true;

        uint256 balanceBefore = IERC20Minimal(p.exitToken).balanceOf(address(this));

        expectedCallbackPool = p.poolA;

        address tokenOwed = p.zeroForOneA
            ? IUniswapV3PoolMinimal(p.poolA).token1()
            : IUniswapV3PoolMinimal(p.poolA).token0();

        CallbackData memory cbData = CallbackData({
            poolB: p.poolB,
            tokenOwedToPoolA: tokenOwed,
            amountOwedToPoolA: 0, // заполнится реальной величиной внутри колбэка
            zeroForOneB: !p.zeroForOneA, // закрываем цикл встречным направлением в pool B
            sqrtPriceLimitB: p.sqrtPriceLimitB
        });

        // amountOwedToPoolA неизвестен ДО вызова swap() -- V3 сообщает точную
        // величину долга внутри самого колбэка через amount0/amount1.
        // Передаём остальные поля через data, величину долга колбэк возьмёт
        // из собственных параметров вызова (см. uniswapV3SwapCallback).
        IUniswapV3PoolMinimal(p.poolA).swap(
            address(this),
            p.zeroForOneA,
            p.amountSpecifiedA,
            p.sqrtPriceLimitA,
            abi.encode(cbData)
        );

        expectedCallbackPool = address(0);

        uint256 balanceAfter = IERC20Minimal(p.exitToken).balanceOf(address(this));
        if (balanceAfter < balanceBefore + p.minProfit) {
            revert InsufficientProfit(balanceBefore, balanceAfter, p.minProfit);
        }

        emit CycleExecuted(p.poolA, p.poolB, p.exitToken, balanceAfter - balanceBefore);
        inCycle = false;
    }

    /// @notice Вызывается pool A внутри executeCycle(). Здесь исполняется
    /// встречный своп в pool B и погашается долг перед pool A.
    function uniswapV3SwapCallback(int256 amount0, int256 amount1, bytes calldata data) external {
        if (msg.sender != expectedCallbackPool) revert UnexpectedCallback(msg.sender);

        CallbackData memory cb = abi.decode(data, (CallbackData));

        // Реальная величина долга перед pool A -- положительная сторона
        // (V3: положительный amount = токен, который контракт ДОЛЖЕН пулу).
        uint256 amountOwed = amount0 > 0 ? uint256(amount0) : uint256(amount1);

        // Закрывающий своп в pool B: отдаём токен, полученный из pool A,
        // exact-input, ожидаем получить обратно tokenOwedToPoolA (или больше --
        // разница и есть прибыль, проверяется в executeCycle после возврата).
        address tokenIn = cb.zeroForOneB
            ? IUniswapV3PoolMinimal(cb.poolB).token0()
            : IUniswapV3PoolMinimal(cb.poolB).token1();

        uint256 amountInAvailable = IERC20Minimal(tokenIn).balanceOf(address(this));

        IUniswapV3PoolMinimal(cb.poolB).swap(
            address(this),
            cb.zeroForOneB,
            int256(amountInAvailable), // exact input -- тратим всё, что получили из pool A
            cb.sqrtPriceLimitB,
            abi.encode(uint8(0)) // pool B колбэк-данные не используются (не flash, обычный своп с предоплатой)
        );

        // Возвращаем долг перед pool A. Если полученного из pool B не хватило
        // на погашение -- этот transfer сам по себе не откатит транзакцию по
        // ERC20-стандарту молча, поэтому явная проверка ниже.
        require(
            IERC20Minimal(cb.tokenOwedToPoolA).balanceOf(address(this)) >= amountOwed,
            "insufficient funds to repay poolA -- cycle unprofitable, revert"
        );
        IERC20Minimal(cb.tokenOwedToPoolA).transfer(expectedCallbackPool, amountOwed);
    }
}
