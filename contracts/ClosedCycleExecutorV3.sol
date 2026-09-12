// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/*
 * ClosedCycleExecutorV3 — атомарный V3<->V3 арбитраж одной пары.
 *
 * Ревизия владельца 2026-09-12 после код-ревью. Исправлены два фатальных бага
 * предыдущей версии:
 *  1) Uniswap V3 ВСЕГДА вызывает uniswapV3SwapCallback для оплаты свопа —
 *     "своп с предоплатой" в V3 не существует. Внутренний своп в pool B
 *     вызывал колбэк, который отвергался (msg.sender != poolA) → revert на
 *     каждой попытке. Теперь колбэк различает внешний (pool A) и внутренний
 *     (pool B) вызов и оплачивает внутренний.
 *  2) zeroForOne == true означает "отдаю token0, получаю token1" → долг перед
 *     pool A это token0. Прежняя версия считала наоборот. Теперь токен и
 *     величина долга берутся из дельт amount0/amount1 прямо в колбэке —
 *     это не может ошибиться.
 *
 * Также: внутренний своп продаёт РОВНО то, что получено от pool A, а не весь
 * баланс контракта (прежняя версия могла проторговать инвентарь).
 *
 * Механизм: flash-swap в pool A (exact-output заём) → в колбэке продаём
 * полученное в pool B → гасим долг pool A → после возврата проверяем, что
 * баланс exitToken вырос ≥ minProfit, иначе revert всего. Инвентарь не может
 * быть потерян сверх газа неудачной попытки.
 *
 * НЕ АУДИРОВАН. Только V3<->V3. V4 — отдельная итерация.
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
}

contract ClosedCycleExecutorV3 {
    address public immutable owner;

    error NotOwner();
    error InsufficientProfit(uint256 balanceBefore, uint256 balanceAfter, uint256 required);
    error UnexpectedCallback(address caller);
    error ReentrantCall();
    error RepayShortfall(uint256 have, uint256 need);

    address private expectedPoolA;
    address private expectedPoolB;
    bool private inCycle;

    /// ABI этой структуры совпадает с прежней версией — executor не меняется.
    struct CycleParams {
        address poolA;           // пул, где занимаем (flash-swap)
        address poolB;           // пул, где закрываем встречным свопом
        bool zeroForOneA;        // направление в pool A
        int256 amountSpecifiedA; // отрицательное = exact output заимствования
        address exitToken;       // токен, в котором меряем прибыль
        uint256 minProfit;       // минимальный прирост exitToken, иначе revert
        uint160 sqrtPriceLimitA;
        uint160 sqrtPriceLimitB;
    }

    struct CallbackData {
        address poolB;
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

    /// Инвентарь пополняется обычным ERC20-переводом на адрес контракта.
    function withdraw(address token, uint256 amount, address to) external onlyOwner {
        require(IERC20Minimal(token).transfer(to, amount), "transfer failed");
    }

    function executeCycle(CycleParams calldata p) external onlyOwner {
        if (inCycle) revert ReentrantCall();
        inCycle = true;

        uint256 balanceBefore = IERC20Minimal(p.exitToken).balanceOf(address(this));

        expectedPoolA = p.poolA;
        IUniswapV3PoolMinimal(p.poolA).swap(
            address(this),
            p.zeroForOneA,
            p.amountSpecifiedA,
            p.sqrtPriceLimitA,
            abi.encode(CallbackData({
                poolB: p.poolB,
                zeroForOneB: !p.zeroForOneA,
                sqrtPriceLimitB: p.sqrtPriceLimitB
            }))
        );
        expectedPoolA = address(0);

        uint256 balanceAfter = IERC20Minimal(p.exitToken).balanceOf(address(this));
        if (balanceAfter < balanceBefore + p.minProfit) {
            revert InsufficientProfit(balanceBefore, balanceAfter, p.minProfit);
        }

        emit CycleExecuted(p.poolA, p.poolB, p.exitToken, balanceAfter - balanceBefore);
        inCycle = false;
    }

    /// Вызывается ДВАЖДЫ за цикл: pool A (внешний flash-swap) и pool B (внутренний своп).
    function uniswapV3SwapCallback(int256 amount0, int256 amount1, bytes calldata data) external {
        // --- внутренний вызов: pool B требует оплату за встречный своп ---
        if (msg.sender == expectedPoolB && expectedPoolB != address(0)) {
            (address tokenIn, uint256 amountIn) = _owed(msg.sender, amount0, amount1);
            require(IERC20Minimal(tokenIn).transfer(msg.sender, amountIn), "pay poolB failed");
            return;
        }

        // --- внешний вызов: pool A выдал заём, надо закрыть цикл и вернуть долг ---
        if (msg.sender != expectedPoolA || expectedPoolA == address(0)) revert UnexpectedCallback(msg.sender);

        CallbackData memory cb = abi.decode(data, (CallbackData));

        (address tokenOwedA, uint256 amountOwedA) = _owed(msg.sender, amount0, amount1);
        uint256 amountReceived = amount0 < 0 ? uint256(-amount0) : uint256(-amount1);

        // Продаём в pool B ровно полученное от pool A (exact input).
        expectedPoolB = cb.poolB;
        IUniswapV3PoolMinimal(cb.poolB).swap(
            address(this),
            cb.zeroForOneB,
            int256(amountReceived),
            cb.sqrtPriceLimitB,
            ""
        );
        expectedPoolB = address(0);

        uint256 have = IERC20Minimal(tokenOwedA).balanceOf(address(this));
        if (have < amountOwedA) revert RepayShortfall(have, amountOwedA);
        require(IERC20Minimal(tokenOwedA).transfer(msg.sender, amountOwedA), "repay poolA failed");
    }

    /// Положительная дельта = токен, который контракт ДОЛЖЕН пулу.
    function _owed(address pool, int256 amount0, int256 amount1) private view returns (address token, uint256 amount) {
        if (amount0 > 0) return (IUniswapV3PoolMinimal(pool).token0(), uint256(amount0));
        return (IUniswapV3PoolMinimal(pool).token1(), uint256(amount1));
    }
}
