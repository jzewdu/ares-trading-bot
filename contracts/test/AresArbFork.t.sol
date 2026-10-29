// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {AresArb} from "../src/AresArb.sol";

/// @notice Integration tests against real Base state: the real Balancer V2
///         Vault, real Uniswap V3 and Aerodrome Slipstream pools, real tokens.
///
/// These tests answer the questions unit tests cannot: does the Vault lend,
/// do these pools actually call `uniswapV3SwapCallback`, does repayment clear,
/// and does the profit guard fire on a route that loses money.
///
///     forge test --match-path test/AresArbFork.t.sol --fork-url https://mainnet.base.org
contract AresArbForkTest is Test {
    address constant USDC = 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913;
    address constant WETH = 0x4200000000000000000000000000000000000006;
    address constant VAULT = 0xBA12222222228d8Ba445958a75a0704d566BF2C8;

    address constant SLIPSTREAM_15 = 0xb2cc224c1c9feE385f8ad6a55b4d94E92359DC59; // 1.5bp
    address constant SLIPSTREAM_285 = 0x4A7af46bC8aBc00048c6c7920a1488DAF568366c; // 2.85bp
    address constant UNISWAP_V3_5 = 0xd0b53D9277642d899DF5C87A3966A349A798F224;   // 5bp

    uint256 constant LOAN = 10_000e6;

    AresArb arb;
    address treasury = address(uint160(uint256(keccak256("ares.treasury"))));

    function setUp() public {
        vm.createSelectFork("https://mainnet.base.org");
        arb = new AresArb(address(this), VAULT);
        vm.label(USDC, "USDC");
        vm.label(WETH, "WETH");
        vm.label(VAULT, "Balancer Vault");
        vm.label(SLIPSTREAM_15, "Slipstream 1.5bp");
        vm.label(SLIPSTREAM_285, "Slipstream 2.85bp");
        vm.label(UNISWAP_V3_5, "Uniswap V3 5bp");
    }

    function _route(address buyPool, address sellPool) internal view returns (AresArb.Hop[] memory hops) {
        hops = new AresArb.Hop[](2);
        hops[0] = AresArb.Hop(buyPool, USDC, WETH, LOAN, 0, 0, false);
        hops[1] = AresArb.Hop(sellPool, WETH, USDC, 0, 0, 0, true);
    }

    /// The whole path against live state: borrow, swap on two real pools,
    /// measure, and either pay out or revert. Asking for zero profit means the
    /// only acceptable outcomes are "made money" or "refused".
    function test_fork_round_trip_is_either_profitable_or_refused() public {
        try arb.execute(USDC, LOAN, _route(SLIPSTREAM_15, SLIPSTREAM_285), 0, treasury) returns (
            uint256 profit
        ) {
            assertGt(profit, 0, "executed without profit");
        } catch (bytes memory reason) {
            assertTrue(
                bytes4(reason) == AresArb.Unprofitable.selector,
                string(abi.encodePacked("unexpected revert: ", _hex(reason)))
            );
        }
    }

    /// Proves repayment and profit delivery through the real Vault by covering
    /// the round-trip cost with a subsidy. The route still loses its fees, so
    /// this is a plumbing test, not a claim of profit.
    function test_fork_vault_repayment_and_profit_delivery() public {
        vm.prank(VAULT);
        require(IERC20Fork(USDC).transfer(address(arb), 200e6), "subsidy failed");

        uint256 vaultBefore = IERC20Fork(USDC).balanceOf(VAULT);
        uint256 loanBefore = IERC20Fork(USDC).balanceOf(address(this));
        uint256 treasuryBefore = IERC20Fork(USDC).balanceOf(treasury);

        arb.execute(USDC, LOAN, _route(SLIPSTREAM_15, SLIPSTREAM_285), 0, treasury);

        uint256 profit = arb.lastProfit();
        assertGt(profit, 0, "subsidy should leave a remainder");
        assertEq(IERC20Fork(USDC).balanceOf(VAULT), vaultBefore, "vault not repaid");
        assertEq(IERC20Fork(USDC).balanceOf(treasury) - treasuryBefore, profit, "profit not delivered");
        assertEq(IERC20Fork(USDC).balanceOf(address(arb)), 0, "executor kept funds");
        assertEq(IERC20Fork(USDC).balanceOf(address(this)), loanBefore, "caller charged");
    }

    /// Same interface on a different venue: Uniswap V3 router-less swap.
    function test_fork_uniswap_v3_pool_accepts_the_callbacks() public {
        try arb.execute(USDC, LOAN, _route(UNISWAP_V3_5, SLIPSTREAM_15), 0, treasury) returns (
            uint256 profit
        ) {
            assertGt(profit, 0, "executed without profit");
        } catch (bytes memory reason) {
            assertTrue(bytes4(reason) == AresArb.Unprofitable.selector, "unexpected revert");
        }
    }

    function test_fork_pools_report_the_tokens_we_route() public {
        assertEq(IERC20Fork(WETH).balanceOf(SLIPSTREAM_15) > 0, true, "slipstream empty");
        assertEq(IERC20Fork(USDC).balanceOf(SLIPSTREAM_15) > 0, true, "slipstream usdc empty");
        assertEq(IERC20Fork(USDC).balanceOf(VAULT) > LOAN, true, "vault cannot lend");
    }

    function _hex(bytes memory data) internal pure returns (string memory) {
        bytes memory alphabet = "0123456789abcdef";
        bytes memory out = new bytes(data.length * 2);
        for (uint256 i = 0; i < data.length; i++) {
            out[i * 2] = alphabet[uint8(data[i]) >> 4];
            out[i * 2 + 1] = alphabet[uint8(data[i]) & 0x0f];
        }
        return string(out);
    }
}

interface IERC20Fork {
    function balanceOf(address) external view returns (uint256);
    function transfer(address, uint256) external returns (bool);
}
