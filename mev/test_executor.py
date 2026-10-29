"""Checks for route building and executor calldata. Run: python -m mev.test_executor"""
from __future__ import annotations

from eth_abi import decode

from .chain import PoolMeta, PoolState
from .executor import build_route, min_profit_raw
from .scanner import evaluate
from .venues import (EXECUTE_SELECTOR, EXECUTE_SIGNATURE, HOP_COMPONENTS, TOKEN_DECIMALS,
                     is_executable)

CHECKS = 0
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WETH = "0x4200000000000000000000000000000000000006"


def ok(label: str, condition: bool, detail: str = ""):
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"{label} failed {detail}")
    print(f"  ok  {label}")


def meta(address: str, fee_bp: float, dex: str = "aerodrome-slipstream",
         token0=USDC, token1=WETH, dec0=6, dec1=18) -> PoolMeta:
    return PoolMeta(address=address, token0=token0, token1=token1, dec0=dec0, dec1=dec1,
                    kind="v3", fee_ppm=int(fee_bp * 100), fee_bp=fee_bp, dex=dex,
                    symbol0="WETH" if token0 == WETH else "USDC",
                    symbol1="WETH" if token1 == WETH else "USDC")


def opportunity(fee_a=2.85, fee_b=1.5, weth_b=399.2):
    """Two USDC/WETH pools with a ~20bp gap, run through the real evaluator."""
    deep = PoolState(meta("0x" + "11" * 20, fee_a), 1_000_000.0, 400.0)      # 2500 USD
    rich = PoolState(meta("0x" + "22" * 20, fee_b), 1_000_000.0, weth_b)     # ~2505 USD
    prices = {USDC: 1.0, WETH: 2500.0}
    results, _ = evaluate([deep, rich], prices, gas_usd=0.01)
    assert results, "fixture produced no opportunity"
    return results[0], prices


def test_selector_matches_the_abi():
    data_sig = EXECUTE_SIGNATURE
    ok("selector is derived from the canonical signature",
       EXECUTE_SELECTOR == "0x" + __import__("eth_utils").keccak(text=data_sig)[:4].hex())
    ok("hop tuple is the seven-field struct",
       HOP_COMPONENTS == "(address,address,address,uint256,uint256,uint160,bool)")
    ok("USDC has a fixed decimal count", TOKEN_DECIMALS[USDC] == 6)


def test_route_that_builds_and_round_trips():
    opp, _ = opportunity()
    route, why = build_route(opp, {USDC: 6, WETH: 18}, slippage_bps=50,
                             min_profit_usd=0.30, gas_usd=0.01)
    ok("a real dislocation builds a route", route is not None, why)
    ok("the loan is the sized input", route.loan_amount == int(opp["sizing"].q * 1e6),
       f"{route.loan_amount} vs {opp['sizing'].q}")
    ok("the loan token is the pair's cheap side",
       route.loan_token in (USDC, WETH))
    ok("first hop spends the loan and is not balance-driven",
       route.hops[0].amount_in == route.loan_amount and not route.hops[0].spend_balance)
    ok("second hop spends whatever the first delivered",
       route.hops[1].spend_balance and route.hops[1].amount_in == 0)
    ok("second hop returns the loan token",
       route.hops[1].token_out == route.loan_token)
    ok("hop chain is continuous",
       route.hops[1].token_in == route.hops[0].token_out)
    ok("minimums are positive and below the quote",
       0 < route.hops[0].min_out and 0 < route.hops[1].min_out)

    data = route.calldata(min_profit=320_000, profit_to="0x" + "ab" * 20)
    ok("calldata starts with the execute selector", data[:10] == EXECUTE_SELECTOR)

    loan_token, loan_amount, hops, floor, profit_to = decode(
        ["address", "uint256", f"{HOP_COMPONENTS}[]", "uint256", "address"],
        bytes.fromhex(data[10:]),
    )
    ok("decoded loan token matches", loan_token == route.loan_token)
    ok("decoded loan amount matches", loan_amount == route.loan_amount)
    ok("decoded floor matches", floor == 320_000)
    ok("decoded treasury matches", profit_to == "0x" + "ab" * 20)
    ok("decoded hop count is two", len(hops) == 2)
    for encoded, hop in zip(hops, route.hops):
        ok("decoded hop matches the route",
           encoded == (hop.pool, hop.token_in, hop.token_out, hop.amount_in,
                       hop.min_out, hop.price_limit, hop.spend_balance),
           f"{encoded} vs {hop}")


def test_min_out_leaves_room_for_a_fill_but_not_a_bad_one():
    opp, _ = opportunity()
    route, _ = build_route(opp, {USDC: 6, WETH: 18}, slippage_bps=50)
    dec_in = TOKEN_DECIMALS[route.loan_token]
    dec_out = TOKEN_DECIMALS[route.hops[0].token_out]

    ok("expected output clears the loan",
       route.expected_out > route.loan_amount / 10 ** dec_in)
    # A 50bp floor means the worst accepted fill still pays 99.5% of quote.
    quoted1 = route.hops[0].min_out / (1 - 0.0050)
    ok("first floor is exactly one slippage step under its quote",
       abs(route.hops[0].min_out / quoted1 - 0.995) < 1e-6,
       f"{route.hops[0].min_out / quoted1:.6f}")
    ok("first floor is positive", route.hops[0].min_out > 0)
    ok("first floor is in output-token units",
       route.hops[0].min_out / 10 ** dec_out < 1e6)
    quoted2 = route.hops[1].min_out / 0.995
    ok("second floor is one slippage step under its quote",
       abs(route.hops[1].min_out / quoted2 - 0.995) < 1e-6)
    ok("second floor is under the expected proceeds",
       route.hops[1].min_out < route.expected_out * 10 ** dec_in)


def test_refuses_what_it_cannot_swap():
    opp, _ = opportunity()
    bad = dict(opp)
    bad["buy"] = meta("0x" + "33" * 20, 30.0, dex="sushiswap-v2-base")
    bad["buy"].kind = "v2"
    route, why = build_route(bad, {USDC: 6, WETH: 18})
    ok("a V2 pool is refused", route is None and "V3" in why, why)

    unknown = dict(opp)
    unknown["buy"] = meta("0x" + "44" * 20, 30.0, dex="mystery-dex")
    route, why = build_route(unknown, {USDC: 6, WETH: 18})
    ok("an unverified venue is refused", route is None and "verified" in why, why)


def test_refuses_an_unprofitable_pair():
    # Same price on both pools, 30bp of fees each leg: the round trip loses.
    from types import SimpleNamespace
    opp = {
        "buy": meta("0x" + "11" * 20, 30.0),
        "sell": meta("0x" + "22" * 20, 30.0),
        "token_in": USDC,
        "usd_in": 1.0,
        "buy_reserves": [1_000_000.0, 400.0],
        "sell_reserves": [1_000_000.0, 400.0],
        "sizing": SimpleNamespace(q=10_000.0, edge_bp=0.0, impact=0.01),
    }
    route, why = build_route(opp, {USDC: 6, WETH: 18})
    ok("a pair whose fees exceed the gap is refused",
       route is None and "not profitable" in why, why)


def test_profit_floor_conversion():
    opp, _ = opportunity()
    route, _ = build_route(opp, {USDC: 6, WETH: 18})
    dec = TOKEN_DECIMALS[route.loan_token]
    raw = min_profit_raw(route, min_profit_usd=0.30, gas_usd=0.01, price_usd=1.0)
    ok("floor is the larger of the dollar target and the gas cost",
       abs(raw / 10 ** dec - 0.30) < 1e-9, f"{raw / 10 ** dec}")
    raw = min_profit_raw(route, min_profit_usd=0.30, gas_usd=0.90, price_usd=1.0)
    ok("a gas cost above the target becomes the floor",
       abs(raw / 10 ** dec - 0.90) < 1e-9, f"{raw / 10 ** dec}")
    ok("no floor when nothing is asked",
       min_profit_raw(route, 0.0, 0.0, 1.0) == 0)


def test_venue_gate():
    ok("verified slipstream venue is executable", is_executable(meta("0x" + "55" * 20, 1.5))[0])
    ok("unverified venue reports a reason",
       is_executable(meta("0x" + "66" * 20, 1.5, dex="quickswap-v4-base"))[0] is False)


if __name__ == "__main__":
    for fn in (test_selector_matches_the_abi, test_route_that_builds_and_round_trips,
               test_min_out_leaves_room_for_a_fill_but_not_a_bad_one,
               test_refuses_what_it_cannot_swap, test_refuses_an_unprofitable_pair,
               test_profit_floor_conversion, test_venue_gate):
        print(fn.__name__)
        fn()
    print(f"\n{CHECKS} checks passed")
