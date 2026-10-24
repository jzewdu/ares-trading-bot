"""The two gears: breakout continuation and range fade.

Both are long-only, because the spot account has no borrow and no perpetuals.
A trend signal buys strength that is still fresh; a range signal buys the lower
band and sells the middle. Each signal carries its own stop distance in ATR, so
the engine can size every trade to a fixed fraction of equity.
"""
from __future__ import annotations

from regime import RANGE, TREND, Regime


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def trend_signal(reg: Regime, settings) -> dict | None:
    """Buy a fresh break of the prior 20-bar high while the trend is intact."""
    if reg.gear != TREND or reg.direction != "up" or reg.stable < 2 or reg.atr <= 0:
        return None
    breakout = reg.close - reg.don_high
    if breakout <= 0:
        return None
    run_atr = breakout / reg.atr
    # A breakout that already travelled more than 0.75 ATR is a chase: the stop
    # would sit too far away for the reward that is left.
    if run_atr > 0.75:
        return None
    stop_dist = settings.trend_stop_atr * reg.atr
    target_dist = settings.trend_target_atr * reg.atr
    score = (45
             + 30 * _clamp((reg.er - settings.trend_er) / 0.35)
             + 15 * _clamp((reg.adx - settings.trend_adx) / 18)
             + 10 * (1 - _clamp(run_atr / 0.75)))
    return {
        "setup": "breakout",
        "gear": TREND,
        "entry_ref": reg.close,
        "stop": reg.close - stop_dist,
        "target": reg.close + target_dist,
        "stop_dist": stop_dist,
        "rr": target_dist / stop_dist if stop_dist else 0.0,
        "score": round(min(100.0, score), 1),
        "features": {
            "adx": round(reg.adx, 2), "er": round(reg.er, 3),
            "atr_pct": round(reg.atr_pct, 3), "rsi": round(reg.rsi, 1),
        },
        "why": (f"Broke the 20-bar high by {run_atr:.2f} ATR; ADX {reg.adx:.1f}, "
                f"efficiency {reg.er:.2f}, stop {settings.trend_stop_atr:.1f} ATR, "
                f"target {settings.trend_target_atr:.1f} ATR"),
    }


def range_signal(reg: Regime, settings) -> dict | None:
    """Buy the lower band in a quiet, wide range and sell the middle."""
    if reg.gear != RANGE or reg.atr <= 0:
        return None
    if reg.rsi > settings.range_rsi:
        return None
    depth = (reg.bb_low + 0.25 * reg.atr - reg.close) / reg.atr
    if depth < 0:
        return None
    if reg.bb_mid <= reg.close:
        return None
    stop_dist = settings.range_stop_atr * reg.atr
    target = (reg.bb_up - 0.25 * reg.atr) if settings.range_target == "upper" else reg.bb_mid
    target_dist = target - reg.close
    if target_dist < 1.2 * reg.atr:
        return None
    rr = target_dist / stop_dist
    if rr < 1.2:
        return None
    score = (45
             + 30 * _clamp((settings.range_rsi - reg.rsi) / 15)
             + 25 * _clamp(depth / 1.5))
    return {
        "setup": "band fade",
        "gear": RANGE,
        "entry_ref": reg.close,
        "stop": reg.close - stop_dist,
        "target": target,
        "stop_dist": stop_dist,
        "rr": rr,
        "score": round(min(100.0, score), 1),
        "features": {
            "adx": round(reg.adx, 2), "er": round(reg.er, 3),
            "atr_pct": round(reg.atr_pct, 3), "rsi": round(reg.rsi, 1),
        },
        "why": (f"RSI {reg.rsi:.1f} at the lower band ({depth:.2f} ATR through it); "
                f"target {target:.6g}, stop {settings.range_stop_atr:.1f} ATR"),
    }


def signal_for(reg: Regime, settings) -> dict | None:
    if reg.gear == TREND:
        return trend_signal(reg, settings)
    if reg.gear == RANGE:
        return range_signal(reg, settings)
    return None
