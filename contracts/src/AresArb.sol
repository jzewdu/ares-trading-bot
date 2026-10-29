// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title AresArb
/// @notice Atomic cross-pool arbitrage on Base, funded by a zero-fee Balancer V2
///         flash loan. The owner submits a route; the contract either finishes
///         with strictly more of the loan asset than it owes and forwards the
///         difference to the treasury, or the whole transaction reverts.
///
/// Design notes that matter:
///
///  * **Pools, not routers.** Every venue Ares measured (Uniswap V3, Pancake V3,
///    Aerodrome Slipstream) speaks the Uniswap V3 pool interface. Calling
///    `swap()` directly removes a router address per venue, removes ERC20
///    approvals to third-party contracts, and makes the price limit per hop
///    explicit. V2-style pools are rejected by the builder, not guessed at.
///  * **Balancer, not Aave.** Aave V3 charges 5bp on the borrowed notional.
///    The edges Ares sees are 3-20bp, so a 5bp loan fee would eat most of them.
///    Balancer V2's flash loan fee is 0.
///  * **The route is validated, not trusted.** The first hop must spend the
///    loan token, every hop must chain, and the last hop must return the loan
///    token. A route that cannot repay is rejected before any state changes.
///  * **Slippage is bounded per hop**, so a sandwich or a stale quote reverts
///    instead of executing at a worse price. A revert costs gas; a bad fill
///    costs capital.
contract AresArb {
    // ------------------------------------------------------------------ types

    struct Hop {
        address pool;        // Uniswap-V3-interface pool
        address tokenIn;
        address tokenOut;
        uint256 amountIn;    // exact input, raw units (ignored if spendBalance)
        uint256 minOut;      // revert if the hop returns less than this
        uint160 priceLimit;  // sqrtPriceLimitX96; 0 means "no limit"
        bool spendBalance;   // spend the whole balance of tokenIn this hop holds
    }

    // --------------------------------------------------------------- storage

    address public immutable owner;
    address public immutable vault;

    uint256 public constant MAX_HOPS = 3;

    /// Last realised profit, in loan-token raw units. Set inside the flash
    /// callback so `execute` can return it (a Balancer flash loan returns
    /// nothing useful).
    uint256 public lastProfit;

    bool private locked;
    address private activePool;
    address private activeTokenIn;

    // ---------------------------------------------------------------- events

    event Arb(
        address indexed loanToken,
        uint256 loanAmount,
        uint256 profit,
        address indexed profitTo,
        uint256 hops
    );
    event Swept(address indexed token, address indexed to, uint256 amount);

    // ---------------------------------------------------------------- errors

    error NotOwner();
    error NotVault();
    error BadRoute();
    error Reentrancy();
    error HopOutTooSmall(uint256 index, uint256 received, uint256 minOut);
    error Unprofitable(uint256 finalBalance, uint256 required);
    error TransferFailed();
    error CallbackFromUnknownPool();

    // ----------------------------------------------------------- construction

    constructor(address owner_, address vault_) {
        require(owner_ != address(0) && vault_ != address(0), "zero address");
        owner = owner_;
        vault = vault_;
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    // ------------------------------------------------------------------ entry

    /// @notice Borrow `loanAmount` of `loanToken`, run `hops`, keep the profit.
    /// @param minProfit raw units of `loanToken` that must remain after the
    ///        loan and its fee are repaid. Use 0 only for simulations.
    /// @param profitTo where the profit is sent; usually the treasury.
    /// @return profit raw units forwarded to `profitTo`.
    function execute(
        address loanToken,
        uint256 loanAmount,
        Hop[] calldata hops,
        uint256 minProfit,
        address profitTo
    ) external onlyOwner returns (uint256 profit) {
        if (locked) revert Reentrancy();
        if (loanAmount == 0 || profitTo == address(0)) revert BadRoute();
        if (hops.length == 0 || hops.length > MAX_HOPS) revert BadRoute();
        if (hops[0].tokenIn != loanToken) revert BadRoute();
        if (hops[0].amountIn != loanAmount) revert BadRoute();
        if (hops[0].spendBalance) revert BadRoute();
        if (hops[hops.length - 1].tokenOut != loanToken) revert BadRoute();
        for (uint256 i = 1; i < hops.length; i++) {
            if (hops[i].tokenIn != hops[i - 1].tokenOut) revert BadRoute();
            if (!hops[i].spendBalance && hops[i].amountIn == 0) revert BadRoute();
        }

        locked = true;

        address[] memory tokens = new address[](1);
        tokens[0] = loanToken;
        uint256[] memory amounts = new uint256[](1);
        amounts[0] = loanAmount;

        IBalancerVault(vault).flashLoan(
            address(this),
            tokens,
            amounts,
            abi.encode(hops, minProfit, profitTo)
        );

        locked = false;
        profit = lastProfit;
        emit Arb(loanToken, loanAmount, profit, profitTo, hops.length);
    }

    // ------------------------------------------------------------- flash path

    function receiveFlashLoan(
        address[] calldata tokens,
        uint256[] calldata amounts,
        uint256[] calldata feeAmounts,
        bytes calldata userData
    ) external {
        if (msg.sender != vault) revert NotVault();

        (Hop[] memory hops, uint256 minProfit, address profitTo) =
            abi.decode(userData, (Hop[], uint256, address));

        address loanToken = tokens[0];
        uint256 loanAmount = amounts[0];
        uint256 fee = feeAmounts[0];

        for (uint256 i = 0; i < hops.length; i++) {
            _swap(hops[i], i);
        }

        uint256 balance = _balanceOf(loanToken);
        uint256 required = loanAmount + fee + minProfit;
        // This single check is the strategy's entire risk control: if the
        // route did not produce enough to repay and clear the floor, nothing
        // after this line runs and the transaction reverts.
        if (balance < required) revert Unprofitable(balance, required);

        uint256 profit = balance - loanAmount - fee;

        // Balancer verifies its own balance after this callback returns, so
        // repay by pushing the principal back. The fee (0 for most tokens on
        // Balancer V2) is pulled afterwards, so it needs an allowance.
        _safeTransfer(loanToken, vault, loanAmount);
        if (fee > 0) {
            _safeApprove(loanToken, vault, fee);
        }
        if (profit > 0) {
            _safeTransfer(loanToken, profitTo, profit);
        }

        lastProfit = profit;
    }

    // ------------------------------------------------------------------ swaps

    function _swap(Hop memory hop, uint256 index) private {
        uint256 before = _balanceOf(hop.tokenOut);

        // Intermediate hops spend exactly what the previous hop produced, so
        // no dust is stranded and no amount can be short by a few wei when the
        // next block prices the pool slightly differently.
        uint256 amountIn = hop.spendBalance ? _balanceOf(hop.tokenIn) : hop.amountIn;

        // Pools order token0 < token1, so the direction is a comparison.
        bool zeroForOne = hop.tokenIn < hop.tokenOut;
        uint160 limit = hop.priceLimit == 0
            ? (zeroForOne ? MIN_SQRT_RATIO + 1 : MAX_SQRT_RATIO - 1)
            : hop.priceLimit;

        activePool = hop.pool;
        activeTokenIn = hop.tokenIn;

        IUniswapV3Pool(hop.pool).swap(
            address(this),
            zeroForOne,
            int256(amountIn),
            limit,
            ""
        );

        activePool = address(0);
        activeTokenIn = address(0);

        uint256 received = _balanceOf(hop.tokenOut) - before;
        if (received < hop.minOut) revert HopOutTooSmall(index, received, hop.minOut);
    }

    /// @notice Uniswap V3 / Pancake V3 / Aerodrome Slipstream all call this.
    ///         The pool has already sent us the output; we owe it the input.
    function uniswapV3SwapCallback(
        int256 amount0Delta,
        int256 amount1Delta,
        bytes calldata
    ) external {
        if (activePool == address(0) || msg.sender != activePool) {
            revert CallbackFromUnknownPool();
        }
        uint256 owed = amount0Delta > 0 ? uint256(amount0Delta) : uint256(amount1Delta);
        _safeTransfer(activeTokenIn, msg.sender, owed);
    }

    /// @dev Pancake V3 pools on some chains use this name; harmless to expose.
    function pancakeV3SwapCallback(
        int256 amount0Delta,
        int256 amount1Delta,
        bytes calldata
    ) external {
        if (activePool == address(0) || msg.sender != activePool) {
            revert CallbackFromUnknownPool();
        }
        uint256 owed = amount0Delta > 0 ? uint256(amount0Delta) : uint256(amount1Delta);
        _safeTransfer(activeTokenIn, msg.sender, owed);
    }

    // ----------------------------------------------------------------- rescue

    /// @notice Recover tokens left behind by a partially funded route.
    function sweep(address token, address to, uint256 amount) external onlyOwner {
        _safeTransfer(token, to, amount);
        emit Swept(token, to, amount);
    }

    // ------------------------------------------------------------------ utils

    function _balanceOf(address token) private view returns (uint256) {
        (bool ok, bytes memory data) =
            token.staticcall(abi.encodeWithSelector(IERC20.balanceOf.selector, address(this)));
        if (!ok || data.length < 32) revert TransferFailed();
        return abi.decode(data, (uint256));
    }

    function _safeTransfer(address token, address to, uint256 amount) private {
        (bool ok, bytes memory data) =
            token.call(abi.encodeWithSelector(IERC20.transfer.selector, to, amount));
        if (!ok || (data.length > 0 && !abi.decode(data, (bool)))) revert TransferFailed();
    }

    function _safeApprove(address token, address spender, uint256 amount) private {
        (bool ok, bytes memory data) = token.call(
            abi.encodeWithSelector(IERC20.approve.selector, spender, 0)
        );
        if (!ok || (data.length > 0 && !abi.decode(data, (bool)))) revert TransferFailed();
        (ok, data) = token.call(
            abi.encodeWithSelector(IERC20.approve.selector, spender, amount)
        );
        if (!ok || (data.length > 0 && !abi.decode(data, (bool)))) revert TransferFailed();
    }

    uint160 internal constant MIN_SQRT_RATIO = 4295128739;
    uint160 internal constant MAX_SQRT_RATIO =
        1461446703485210103287273052203988822378723970342;
}

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function transfer(address, uint256) external returns (bool);
    function approve(address, uint256) external returns (bool);
}

interface IBalancerVault {
    function flashLoan(
        address recipient,
        address[] calldata tokens,
        uint256[] calldata amounts,
        bytes calldata userData
    ) external;
}

interface IUniswapV3Pool {
    function swap(
        address recipient,
        bool zeroForOne,
        int256 amountSpecified,
        uint160 sqrtPriceLimitX96,
        bytes calldata data
    ) external returns (int256 amount0, int256 amount1);
}
