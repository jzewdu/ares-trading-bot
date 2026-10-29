"""Arbitrage sizing: closed-form optimum for two constant-product legs.

The whole point of this module is that it is pure and testable. Every number
the scanner reports goes through `best_size`, so a bug here would show up as
fake profit on the dashboard.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class Sizing:
    q: float            # optimal input, in units of the input token
    out: float          # output after both legs
    profit: float       # out - q, input-token units
    gross_bp: float     # profit / q, basis points
    edge_bp: float      # mid-price dislocation before fees
    impact: float       # q / input reserve, fraction of the pool consumed


def amount_out(q: float, r_in: float, r_out: float, fee_bp: float) -> float:
    """Uniswap V2 getAmountOut, in human units."""
    if q <= 0 or r_in <= 0 or r_out <= 0:
        return 0.0
    g = 1.0 - fee_bp / 10_000.0
    return g * q * r_out / (r_in + g * q)


def optimal_input(a_in: float, a_out: float, b_in: float, b_out: float,
                  fee_a_bp: float, fee_b_bp: float) -> float:
    """q* = (sqrt(g1*g2*a_in*a_out*b_in*b_out) - a_in*b_in) / (g1*(b_in + g2*a_out)).

    Buying on pool A, selling on pool B. Returns 0 when no opportunity exists
    after fees (the discriminant condition g1*g2*a_out*b_out <= a_in*b_in).
    """
    if min(a_in, a_out, b_in, b_out) <= 0:
        return 0.0
    g1 = 1.0 - fee_a_bp / 10_000.0
    g2 = 1.0 - fee_b_bp / 10_000.0
    base = a_in * b_in
    disc = g1 * g2 * a_in * a_out * b_in * b_out
    if disc <= base * base:
        return 0.0
    denom = g1 * (b_in + g2 * a_out)
    if denom <= 0:
        return 0.0
    return max(0.0, (math.sqrt(disc) - base) / denom)


def best_size(a_in: float, a_out: float, b_in: float, b_out: float,
              fee_a_bp: float, fee_b_bp: float) -> Sizing | None:
    q = optimal_input(a_in, a_out, b_in, b_out, fee_a_bp, fee_b_bp)
    if q <= 0:
        return None
    out = amount_out(amount_out(q, a_in, a_out, fee_a_bp), b_in, b_out, fee_b_bp)
    profit = out - q
    if profit <= 0:
        return None
    # Round-trip rate: buy leg pays token_out per token_in, sell leg pays
    # token_in per token_out. Their product is unitless and must exceed 1 for
    # an edge to exist. (Multiplying the two "mids" without inverting the sell
    # leg is the classic way to report a 5e10 bp "opportunity" that isn't one.)
    edge = (a_out * b_out) / (a_in * b_in) - 1.0
    return Sizing(q=q, out=out, profit=profit, gross_bp=profit / q * 10_000,
                  edge_bp=edge * 10_000, impact=q / a_in)


def blend_fee(pools: list[float]) -> float:
    """Fee a multi-pool route pays, in basis points."""
    total, factor = 0.0, 1.0
    for fee in pools:
        total += factor * fee
        factor *= 1.0 - fee / 10_000.0
    return total


def usd_gas(gas_units: int, gas_price_wei: int, l1_fee_wei: int, eth_usd: float) -> float:
    wei = gas_units * gas_price_wei + l1_fee_wei
    return wei / 1e18 * eth_usd
