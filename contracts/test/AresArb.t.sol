// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {AresArb} from "../src/AresArb.sol";

// --------------------------------------------------------------------- mocks

contract MockERC20 {
    string public name;
    uint8 public immutable decimals;
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    constructor(string memory name_, uint8 decimals_) {
        name = name_;
        decimals = decimals_;
    }

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
        totalSupply += amount;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        return _move(msg.sender, to, amount);
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        require(allowed >= amount, "allowance");
        if (allowed != type(uint256).max) allowance[from][msg.sender] = allowed - amount;
        return _move(from, to, amount);
    }

    function _move(address from, address to, uint256 amount) private returns (bool) {
        require(balanceOf[from] >= amount, "balance");
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}

contract MockVault {
    uint256 public feePct; // 1e18 == 100%

    function setFeePct(uint256 pct) external {
        feePct = pct;
    }

    function flashLoan(
        address recipient,
        address[] calldata tokens,
        uint256[] calldata amounts,
        bytes calldata userData
    ) external {
        require(tokens.length == 1, "single asset only");
        MockERC20 token = MockERC20(tokens[0]);
        uint256 pre = token.balanceOf(address(this));
        uint256 fee = (amounts[0] * feePct) / 1e18;

        token.transfer(recipient, amounts[0]);

        uint256[] memory fees = new uint256[](1);
        fees[0] = fee;
        IFlashLoanRecipient(recipient).receiveFlashLoan(tokens, amounts, fees, userData);

        require(token.balanceOf(address(this)) >= pre, "principal not repaid");
        if (fee > 0) token.transferFrom(recipient, address(this), fee);
    }
}

interface IFlashLoanRecipient {
    function receiveFlashLoan(
        address[] calldata tokens,
        uint256[] calldata amounts,
        uint256[] calldata feeAmounts,
        bytes calldata userData
    ) external;
}

interface ISwapCallback {
    function uniswapV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata data) external;
}

/// @dev Uniswap-V3-shaped pool with a configurable fixed exchange rate.
///      Real pools price on a curve; this one quotes `amountIn * num / den`
///      so the tests are about route mechanics, not about re-deriving the
///      constant-product formula (which `mev/test_mev.py` already covers).
contract MockPool {
    address public immutable token0;
    address public immutable token1;
    mapping(address => uint256) public num;
    mapping(address => uint256) public den;

    constructor(address a, address b) {
        require(a != b, "same token");
        (token0, token1) = a < b ? (a, b) : (b, a);
    }

    function setRate(address tokenIn, uint256 num_, uint256 den_) external {
        num[tokenIn] = num_;
        den[tokenIn] = den_;
    }

    function swap(
        address recipient,
        bool zeroForOne,
        int256 amountSpecified,
        uint160,
        bytes calldata
    ) external returns (int256, int256) {
        require(amountSpecified > 0, "exact input only");
        uint256 amountIn = uint256(amountSpecified);
        address tokenIn = zeroForOne ? token0 : token1;
        address tokenOut = zeroForOne ? token1 : token0;

        uint256 out = (amountIn * num[tokenIn]) / den[tokenIn];
        uint256 poolBefore = MockERC20(tokenIn).balanceOf(address(this));

        MockERC20(tokenOut).transfer(recipient, out);

        int256 delta0 = zeroForOne ? int256(amountIn) : -int256(out);
        int256 delta1 = zeroForOne ? -int256(out) : int256(amountIn);
        ISwapCallback(msg.sender).uniswapV3SwapCallback(delta0, delta1, "");

        require(
            MockERC20(tokenIn).balanceOf(address(this)) == poolBefore + amountIn,
            "input not paid"
        );
        return (delta0, delta1);
    }
}

// ---------------------------------------------------------------------- test

contract AresArbTest is Test {
    MockERC20 usdc;
    MockERC20 weth;
    MockVault vault;
    MockPool buyPool;
    MockPool sellPool;
    AresArb arb;

    address treasury = address(0xA11CE);
    address keeper = address(0xB0B);

    uint256 constant LOAN = 10_000e6;       // 10,000 USDC
    uint256 constant WETH_OUT = 4e18;       // buy leg: 2,500 USDC per WETH
    uint256 constant USDC_BACK = 10_320e6;  // sell leg: 2,580 USDC per WETH

    function setUp() public {
        usdc = new MockERC20("USD Coin", 6);
        weth = new MockERC20("Wrapped Ether", 18);
        vault = new MockVault();
        arb = new AresArb(keeper, address(vault));

        buyPool = new MockPool(address(weth), address(usdc)); // 10,000 USDC -> 4 WETH
        buyPool.setRate(address(usdc), 4e8, 1);

        sellPool = new MockPool(address(weth), address(usdc)); // 4 WETH -> 10,320 USDC
        sellPool.setRate(address(weth), 2_580, 1e12);

        usdc.mint(address(vault), 1_000_000e6);
        weth.mint(address(buyPool), 1_000e18);
        usdc.mint(address(sellPool), 1_000_000e6);
    }

    function _route() internal view returns (AresArb.Hop[] memory hops) {
        hops = new AresArb.Hop[](2);
        hops[0] = AresArb.Hop({
            pool: address(buyPool),
            tokenIn: address(usdc),
            tokenOut: address(weth),
            amountIn: LOAN,
            minOut: WETH_OUT,
            priceLimit: 0,
            spendBalance: false
        });
        hops[1] = AresArb.Hop({
            pool: address(sellPool),
            tokenIn: address(weth),
            tokenOut: address(usdc),
            amountIn: 0,
            minOut: USDC_BACK,
            priceLimit: 0,
            spendBalance: true
        });
    }

    // -------------------------------------------------------------- happy path

    function test_profitable_route_pays_treasury_and_repays_vault() public {
        uint256 vaultBefore = usdc.balanceOf(address(vault));
        AresArb.Hop[] memory hops = _route();

        vm.prank(keeper);
        uint256 profit = arb.execute(address(usdc), LOAN, hops, 0, treasury);

        assertEq(profit, 320e6, "profit");
        assertEq(usdc.balanceOf(treasury), 320e6, "treasury paid");
        assertEq(usdc.balanceOf(address(vault)), vaultBefore, "vault principal back");
        assertEq(usdc.balanceOf(address(arb)), 0, "no dust in executor");
        assertEq(weth.balanceOf(address(arb)), 0, "no weth stranded");
    }

    function test_intermediate_hop_spends_exactly_what_it_received() public {
        // `amountIn` on the second hop is deliberately wrong; spendBalance means
        // it is ignored, so the route still works and leaves no WETH behind.
        AresArb.Hop[] memory hops = _route();
        hops[1].amountIn = 1; // nonsense, must be ignored

        vm.prank(keeper);
        arb.execute(address(usdc), LOAN, hops, 0, treasury);

        assertEq(weth.balanceOf(address(arb)), 0, "weth stranded");
        assertEq(usdc.balanceOf(treasury), 320e6, "profit");
    }

    // ------------------------------------------------------------ profit floor

    function test_reverts_when_profit_below_floor() public {
        AresArb.Hop[] memory hops = _route();

        vm.prank(keeper);
        vm.expectRevert();
        arb.execute(address(usdc), LOAN, hops, 321e6, treasury);

        assertEq(usdc.balanceOf(treasury), 0, "nothing paid on revert");
    }

    function test_revert_reports_the_shortfall() public {
        AresArb.Hop[] memory hops = _route();

        vm.prank(keeper);
        vm.expectRevert(
            abi.encodeWithSelector(AresArb.Unprofitable.selector, 10_320e6, LOAN + 321e6)
        );
        arb.execute(address(usdc), LOAN, hops, 321e6, treasury);
    }

    // --------------------------------------------------------------- slippage

    function test_reverts_when_a_hop_pays_less_than_min_out() public {
        AresArb.Hop[] memory hops = _route();
        hops[1].minOut = 10_400e6; // pool only returns 10,320

        vm.prank(keeper);
        vm.expectRevert(
            abi.encodeWithSelector(AresArb.HopOutTooSmall.selector, 1, USDC_BACK, 10_400e6)
        );
        arb.execute(address(usdc), LOAN, hops, 0, treasury);
    }

    function test_reverts_when_the_first_hop_pays_less_than_min_out() public {
        AresArb.Hop[] memory hops = _route();
        hops[0].minOut = 5e18; // pool only returns 4 WETH

        vm.prank(keeper);
        vm.expectRevert(
            abi.encodeWithSelector(AresArb.HopOutTooSmall.selector, 0, WETH_OUT, 5e18)
        );
        arb.execute(address(usdc), LOAN, hops, 0, treasury);
    }

    // ----------------------------------------------------------------- route

    function test_rejects_route_that_does_not_return_the_loan_token() public {
        AresArb.Hop[] memory hops = new AresArb.Hop[](1);
        hops[0] = AresArb.Hop(address(buyPool), address(usdc), address(weth), LOAN, 0, 0, false);

        vm.prank(keeper);
        vm.expectRevert(AresArb.BadRoute.selector);
        arb.execute(address(usdc), LOAN, hops, 0, treasury);
    }

    function test_rejects_route_whose_first_hop_spends_a_different_token() public {
        AresArb.Hop[] memory hops = new AresArb.Hop[](1);
        hops[0] = AresArb.Hop(address(buyPool), address(weth), address(usdc), LOAN, 0, 0, false);

        vm.prank(keeper);
        vm.expectRevert(AresArb.BadRoute.selector);
        arb.execute(address(usdc), LOAN, hops, 0, treasury);
    }

    function test_rejects_route_that_spends_preexisting_balance_first() public {
        AresArb.Hop[] memory hops = _route();
        hops[0].spendBalance = true;

        vm.prank(keeper);
        vm.expectRevert(AresArb.BadRoute.selector);
        arb.execute(address(usdc), LOAN, hops, 0, treasury);
    }

    function test_rejects_more_than_three_hops() public {
        AresArb.Hop[] memory hops = new AresArb.Hop[](4);
        for (uint256 i = 0; i < 4; i++) {
            hops[i] = AresArb.Hop(address(buyPool), address(usdc), address(weth), LOAN, 0, 0, false);
        }

        vm.prank(keeper);
        vm.expectRevert(AresArb.BadRoute.selector);
        arb.execute(address(usdc), LOAN, hops, 0, treasury);
    }

    // ------------------------------------------------------------ access, fee

    function test_only_owner_can_execute() public {
        AresArb.Hop[] memory hops = _route();

        vm.prank(address(0xBAD));
        vm.expectRevert(AresArb.NotOwner.selector);
        arb.execute(address(usdc), LOAN, hops, 0, treasury);
    }

    function test_callback_from_a_stranger_reverts() public {
        vm.expectRevert(AresArb.CallbackFromUnknownPool.selector);
        arb.uniswapV3SwapCallback(1, -1, "");
    }

    function test_vault_fee_is_subtracted_from_profit() public {
        vault.setFeePct(5e14); // 5bp, what Aave charges
        uint256 vaultBefore = usdc.balanceOf(address(vault));
        AresArb.Hop[] memory hops = _route();

        vm.prank(keeper);
        uint256 profit = arb.execute(address(usdc), LOAN, hops, 0, treasury);

        uint256 fee = (LOAN * 5e14) / 1e18;
        assertEq(fee, 5e6, "5bp of 10k");
        assertEq(profit, 320e6 - fee, "profit net of loan fee");
        assertEq(usdc.balanceOf(address(vault)), vaultBefore + fee, "fee paid");
        assertEq(usdc.balanceOf(treasury), 320e6 - fee, "treasury");
    }

    function test_owner_can_sweep_stranded_tokens() public {
        usdc.mint(address(arb), 42e6);

        vm.prank(keeper);
        arb.sweep(address(usdc), treasury, 42e6);

        assertEq(usdc.balanceOf(treasury), 42e6);
        assertEq(usdc.balanceOf(address(arb)), 0);
    }

    function test_stranger_cannot_sweep() public {
        usdc.mint(address(arb), 42e6);

        vm.prank(address(0xBAD));
        vm.expectRevert(AresArb.NotOwner.selector);
        arb.sweep(address(usdc), address(0xBAD), 42e6);
    }

    // ------------------------------------------------------------------ fuzz

    function testFuzz_profit_floor_never_lets_a_bad_route_through(uint256 floor) public {
        floor = bound(floor, 321e6, 10_000e6);
        AresArb.Hop[] memory hops = _route();

        vm.prank(keeper);
        vm.expectRevert();
        arb.execute(address(usdc), LOAN, hops, floor, treasury);
    }

    function testFuzz_executor_never_keeps_the_profit(uint256 loan) public {
        loan = bound(loan, 1_000e6, 100_000e6);
        usdc.mint(address(vault), 1_000_000e6);

        AresArb.Hop[] memory hops = new AresArb.Hop[](2);
        hops[0] = AresArb.Hop(address(buyPool), address(usdc), address(weth), loan, 0, 0, false);
        hops[1] = AresArb.Hop(address(sellPool), address(weth), address(usdc), 0, 0, 0, true);

        vm.prank(keeper);
        arb.execute(address(usdc), loan, hops, 0, treasury);

        assertEq(usdc.balanceOf(address(arb)), 0);
        assertGt(usdc.balanceOf(treasury), 0);
    }
}
