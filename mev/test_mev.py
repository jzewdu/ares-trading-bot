"""Checks for the arb math. Run with: python -m mev.test_mev"""
from __future__ import annotations

import math

from .chain import PoolMeta, Universe
from .math import amount_out, best_size, blend_fee, optimal_input, usd_gas

CHECKS = 0


def ok(label: str, condition: bool, detail: str = ""):
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"{label} failed {detail}")
    print(f"  ok  {label}")


def test_amount_out():
    out = amount_out(1000.0, 1_000_000.0, 1_000_000.0, 30.0)
    expected = 0.997 * 1000 * 1_000_000 / (1_000_000 + 0.997 * 1000)
    ok("v2 getAmountOut matches the 997/1000 formula", abs(out - expected) < 1e-9,
       f"{out} vs {expected}")
    ok("zero size returns zero", amount_out(0, 1e6, 1e6, 30) == 0.0)
    ok("fee is monotone", amount_out(1000, 1e6, 1e6, 5) > amount_out(1000, 1e6, 1e6, 30))


def test_closed_form():
    cases = [
        (2_000_000.0, 855.0, 855.0, 2_000_000.0 * 1.0025, 5.0, 1.0),
        (8_000_000.0, 3_418.0, 3_418.0, 8_000_000.0 * 1.0045, 5.0, 30.0),
        (250_000.0, 106.0, 106.0, 250_000.0 * 1.09, 100.0, 30.0),
    ]
    for a_in, a_out, b_in, b_out, f1, f2 in cases:
        q = optimal_input(a_in, a_out, b_in, b_out, f1, f2)
        ok("q* is positive when the edge clears fees", q > 0)

        def profit(x):
            return amount_out(amount_out(x, a_in, a_out, f1), b_in, b_out, f2) - x

        grid = [q * (0.80 + 0.004 * i) for i in range(101)]
        best = max(profit(x) for x in grid)
        ok("q* is at the numeric maximum", abs(profit(q) - best) < 1e-6,
           f"{profit(q):.8f} vs {best:.8f}")
        ok("profit is positive at q*", profit(q) > 0)


def test_no_arb():
    q = optimal_input(1_000_000, 427.0, 427.0, 1_000_000, 5.0, 5.0)
    ok("equal prices yield no trade", q == 0.0, f"q={q}")
    ok("fee-only gap yields no trade",
       optimal_input(1_000_000, 427.0, 427.0, 1_000_000 * 1.0004, 5.0, 5.0) == 0.0)


def test_regression():
    # 25bp dislocation on $20M pools, 5bp buy leg and 1bp sell leg.
    q = optimal_input(20_000_000, 20_000_000 / 2340, 20_000_000 / 2340,
                      20_000_000 * 1.0025, 5.0, 1.0)
    p = 20_000_000 * 1.0025
    profit = amount_out(amount_out(q, 20_000_000, 20_000_000 / 2340, 5.0),
                        20_000_000 / 2340, p, 1.0) - q
    ok("25bp on $20M pools pays about $9", 8.5 < profit < 9.6, f"${profit:.2f}")
    ok("optimal size is about $9.5k", 9_000 < q < 10_000, f"${q:,.0f}")
    s = best_size(20_000_000, 20_000_000 / 2340, 20_000_000 / 2340,
                  20_000_000 * 1.0025, 5.0, 1.0)
    ok("edge_bp reads the gap, not its reciprocal", 24.5 < s.edge_bp < 25.5,
       f"{s.edge_bp:.2f}bp")
    ok("net edge cannot exceed the gross gap minus fees",
       s.gross_bp < s.edge_bp - 6.0, f"{s.gross_bp:.2f}bp vs {s.edge_bp:.2f}bp")


def test_blend_and_gas():
    ok("blended fee stacks multiplicatively", abs(blend_fee([5.0, 30.0]) - 34.985) < 0.01)
    # L1 data fee for a ~1.2kB tx is a few billion wei, not a milli-ETH.
    usd = usd_gas(300_000, int(0.0067 * 1e9), 4_000_000_000, 2340.0)
    ok("gas estimate is cents", 0.001 < usd < 0.10, f"${usd:.5f}")


def test_v3_reserves():
    price = 2340.0                    # token1 per token0, both 18 decimals
    sp = math.sqrt(price)
    sqrt_x96 = int(sp * (1 << 96))
    liquidity = 10 ** 18
    meta = PoolMeta(address="0xpool", token0="0xweth", token1="0xusdc", dec0=18,
                    dec1=18, kind="v3", fee_ppm=500, fee_bp=5.0)
    state = Universe._parse(meta, ["0x" + format(sqrt_x96, "x").rjust(64, "0"),
                                   "0x" + format(liquidity, "x").rjust(64, "0")])
    ok("v3 virtual reserves imply the pool price",
       abs(state.reserve1 / state.reserve0 - price) / price < 1e-6,
       f"{state.reserve1 / state.reserve0:.3f}")
    ok("v3 virtual reserves have the right depth",
       abs(state.reserve0 - (liquidity / sp) / 1e18) < 1e-9)
    ok("liquidity identity holds",
       abs(state.reserve0 * state.reserve1 * 1e36 - liquidity ** 2) / liquidity ** 2 < 1e-6)


def test_drawdown_of_a_bad_fill():
    # If the sell leg is skipped, a naked buy on the same pool must lose money.
    buy = amount_out(10_000, 50_000, 427.0, 5.0)
    ok("a one-legged fill is a loss", buy < 10_000)


if __name__ == "__main__":
    for fn in (test_amount_out, test_closed_form, test_no_arb, test_regression,
               test_blend_and_gas, test_v3_reserves, test_drawdown_of_a_bad_fill):
        print(fn.__name__)
        fn()
    print(f"\n{CHECKS} checks passed")
