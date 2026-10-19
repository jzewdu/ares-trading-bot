"""Simple long-only momentum breakout signal; research candidate, not a guarantee."""
from __future__ import annotations

from statistics import mean
from typing import Any


def signal_for(bars: list[dict[str, Any]]) -> str | None:
    """Return BUY when a 15m close breaks out with trend and volume confirmation.

    Requires 55 bars. Exits are handled by the engine using a conservative
    fixed loss threshold; this intentionally avoids leverage and shorting.
    """
    if len(bars) < 55:
        return None
    bars = sorted(bars, key=lambda b: b["t"])
    closed = bars[:-1]  # ignore the potentially incomplete current interval
    closes = [float(b["c"]) for b in closed]
    highs = [float(b["h"]) for b in closed]
    volumes = [float(b.get("v", 0)) for b in closed]
    if any(value <= 0 for value in closes[-55:]):
        return None

    fast = mean(closes[-20:])
    slow = mean(closes[-50:])
    breakout_level = max(highs[-21:-1])
    volume_base = mean(volumes[-21:-1])
    last_volume = volumes[-1]

    if closes[-1] > breakout_level and fast > slow and volume_base > 0 and last_volume >= volume_base:
        return "BUY"
    return None
