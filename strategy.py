"""Delta-neutral carry rules.

The book holds two legs of equal notional: long spot on Alpaca, short the
Hyperliquid perpetual. Price moves cancel; what remains is the funding the
short collects, minus the basis drift between the two venues, minus fees.

Research on 730 days of hourly funding (research/carry.py) put the edge at the
structural funding floor rather than at timing: rotating into whatever coin is
paying the most raised fees faster than it raised yield. So the rules hold a
small, fixed basket and only leave when funding actually turns against it.
"""
from __future__ import annotations

from typing import Any

HOURS_YEAR = 24 * 365


def trailing_apr(rates: list[tuple[int, float]], now_ms: int, hours: int) -> float | None:
    """Annualised funding over the trailing window, from settled hourly rates."""
    cutoff = now_ms - hours * 3600 * 1000
    window = [rate for ts, rate in rates if ts > cutoff]
    if len(window) < max(6, hours // 4):
        return None
    return sum(window) / len(window) * HOURS_YEAR * 100


def basis(spot: float, perp_mark: float) -> float:
    """Perp premium over the spot venue, as a fraction of spot."""
    if spot <= 0:
        return 0.0
    return (perp_mark - spot) / spot


def entry_check(ctx: dict[str, Any], apr: float | None, premium: float,
                enter_apr: float, max_premium: float,
                gap: float = 0.0, max_gap: float = 0.01,
                spread: float | None = None,
                max_spread: float = 0.0010) -> tuple[bool, str]:
    """Decide whether the hedge is worth opening, with the reason for the desk.

    `premium` is the perp's own premium over its index (the venue's basis), and
    `gap` is the difference between the spot venue and the perp mark. A short
    entered while the perp is cheap pays that difference back on convergence, so
    both are checked before entering.

    `spread` is Alpaca's quoted bid/ask on the spot pair. A market order pays it
    on entry and again on exit, so a pair whose book is too thin is skipped even
    when its funding looks attractive: on 2026-10-23 a LINK entry filled 0.47%
    above the Hyperliquid mark, which is two months of that pair's carry.
    """
    if ctx.get("mark", 0) <= 0:
        return False, "No perpetual mark on Hyperliquid"
    if apr is None:
        return False, "Not enough funding history to price the carry"
    if apr <= enter_apr:
        return False, f"Funding {apr:+.1f}% APR is below the {enter_apr:.1f}% entry floor"
    if premium >= max_premium:
        return False, f"Perp premium {premium * 100:+.2f}% is above the {max_premium * 100:.1f}% band"
    if premium < -0.0025:
        return False, (f"Perp trades {premium * 100:+.2f}% below its index; "
                       f"convergence would cost the short")
    if abs(gap) > max_gap:
        return False, f"Spot venue and perp are {gap * 100:+.2f}% apart; execution would leak the carry"
    if spread is None:
        return False, "No live Alpaca quote, so the spot leg's spread cannot be priced"
    if spread > max_spread:
        return False, (f"Alpaca spot spread {spread * 100:.2f}% is above the "
                       f"{max_spread * 100:.2f}% cost gate; the fill would eat the carry")
    return True, (f"Funding {apr:+.1f}% APR to the short, perp premium {premium * 100:+.3f}%, "
                  f"venue gap {gap * 100:+.3f}%, spot spread {spread * 100:.2f}%")


def exit_reason(apr72: float | None, premium: float, exit_apr: float, max_premium: float) -> str | None:
    """Reasons to unwind a hedged pair. None means keep holding."""
    if apr72 is not None and apr72 <= exit_apr:
        return f"Funding turned against the short ({apr72:+.1f}% APR over 72h)"
    if abs(premium) >= max_premium:
        return f"Perp premium blew out to {premium * 100:+.2f}%"
    return None
