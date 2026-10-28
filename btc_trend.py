"""BTC trend core: own bitcoin by default, step aside only when the trend actually breaks.

Why this shape
--------------
A spot crypto account cannot short, and Alpaca charges 0.25% per side. On real
BTC bars a 12-month test of breakout, pullback and mean-reversion rules turned
over dozens of times and lost more to fees and whipsaw than the entries earned.
The one rule that survived the same costs was the slow one: own BTC while it is
above its 200-bar (4-hour) trend line, and step aside when price closes well
below it. This module is that rule and nothing else.

* **long** - the 4-hour close is not past the brake. The desk owns BTC. Being
  a little below the trend line is allowed: entering only on a reclaim cost
  more in missed upside and round trips than it saved in the test window.
* **brake** - the 4-hour close is more than `btc_brake_atr` ATRs below that
  line. The desk sells and waits for the close to come back above the brake.
* the brake is the only exit besides the catastrophe stop, so the desk trades
  a handful of times a year instead of a handful of times a day.

Every decision uses completed 4-hour bars only, so a signal can never be built
from a candle that is still forming.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from regime import STAND_ASIDE, TREND, adx_series, atr_series, efficiency, ema, rsi_series

HOURS = 4


@dataclass
class Row:
    ts: int
    gear: str
    direction: str
    close: float
    adx: float
    er: float
    atr: float
    atr_pct: float
    ema_fast: float
    ema_slow: float
    trend_spread_pct: float
    rsi: float
    bb_low: float
    bb_mid: float
    bb_up: float
    bb_width_pct: float
    don_high: float
    don_low: float
    stable: int
    reason: str

    def as_dict(self) -> dict:
        return asdict(self)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _bands(closes: list[float], period: int = 20, mult: float = 2.0):
    out = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1:i + 1]
        mid = sum(window) / period
        sd = (sum((x - mid) ** 2 for x in window) / period) ** 0.5
        out[i] = (mid - mult * sd, mid, mid + mult * sd)
    return out


def _donchian(highs: list[float], lows: list[float], period: int = 20):
    out = [None] * len(highs)
    for i in range(period, len(highs)):
        out[i] = (max(highs[i - period:i]), min(lows[i - period:i]))
    return out


def analyse(bars: list[dict], settings) -> list[Row]:
    """One row per completed 4-hour bar, using only bars up to that bar."""
    if not bars:
        return []
    highs = [float(b["h"]) for b in bars]
    lows = [float(b["l"]) for b in bars]
    closes = [float(b["c"]) for b in bars]
    trend_ema = ema(closes, int(settings.btc_ema_bars))
    atr = atr_series(highs, lows, closes, 14)
    adx = adx_series(highs, lows, closes, 14)
    rsi = rsi_series(closes, 14)
    er = efficiency(closes, 20)
    bands = _bands(closes, 20, 2.0)
    don = _donchian(highs, lows, 20)

    warmup = int(settings.btc_ema_bars) + 10
    out: list[Row] = []
    previous = ""
    stable = 0
    for i in range(len(bars)):
        if i < warmup or None in (trend_ema[i], atr[i], adx[i], rsi[i], er[i], bands[i], don[i]):
            continue
        close = closes[i]
        line = float(trend_ema[i])
        atr_v = float(atr[i])
        atr_pct = atr_v / close * 100 if close else 0.0
        above = (close - line) / close * 100 if close else 0.0
        brake_level = line - settings.btc_brake_atr * atr_v
        bb_low, bb_mid, bb_up = bands[i]
        width = (bb_up - bb_low) / bb_mid * 100 if bb_mid else 0.0

        if close < brake_level:
            gear, direction = STAND_ASIDE, "down"
            reason = (f"Brake: BTC closed {abs(above):.2f}% below the trend line, past the "
                      f"{settings.btc_brake_atr:.1f}-ATR band ({brake_level:,.0f}); in cash")
        elif close > line:
            gear, direction = TREND, "up"
            reason = (f"BTC {above:+.2f}% above its {int(settings.btc_ema_bars)}-bar trend line "
                      f"({line:,.0f}); holding the core long")
        else:
            gear, direction = TREND, "flat"
            reason = (f"BTC is {abs(above):.2f}% below its {int(settings.btc_ema_bars)}-bar line "
                      f"but inside the brake band ({brake_level:,.0f}); still holding the core")
        exit_level = brake_level
        # Keep the brake visible to callers without adding a field.
        key = f"{gear}:{direction}"
        stable = stable + 1 if key == previous else 1
        previous = key
        out.append(Row(
            ts=int(bars[i]["ts"]), gear=gear, direction=direction, close=close,
            adx=float(adx[i]), er=float(er[i]), atr=atr_v, atr_pct=atr_pct,
            ema_fast=line, ema_slow=exit_level, trend_spread_pct=above,
            rsi=float(rsi[i]), bb_low=float(bb_low), bb_mid=float(bb_mid), bb_up=float(bb_up),
            bb_width_pct=width, don_high=float(don[i][0]), don_low=float(don[i][1]),
            stable=stable, reason=reason,
        ))
    return out


def signal_for(row, settings) -> dict | None:
    """The one setup: hold BTC while the trend line is intact."""
    if row.gear != TREND or row.atr <= 0:
        return None
    stop_dist = settings.trend_stop_atr * row.atr
    target_dist = settings.trend_target_atr * row.atr
    extension = _clamp(abs(row.trend_spread_pct) / 8.0)
    score = 62 + 22 * (1 - extension) + 10 * _clamp((row.adx - 15) / 20)
    return {
        "setup": "trend core",
        "gear": TREND,
        "entry_ref": row.close,
        "stop": row.close - stop_dist,
        "target": row.close + target_dist,
        "stop_dist": stop_dist,
        "rr": target_dist / stop_dist if stop_dist else 0.0,
        "score": round(min(100.0, score), 1),
        "features": {
            "adx": round(row.adx, 2), "er": round(row.er, 3),
            "atr_pct": round(row.atr_pct, 3), "rsi": round(row.rsi, 1),
        },
        "why": (f"BTC is {row.trend_spread_pct:+.2f}% above its "
                f"{int(settings.btc_ema_bars)}-bar trend line; stop "
                f"{settings.trend_stop_atr:.1f} ATR below, brake at the line minus "
                f"{settings.btc_brake_atr:.1f} ATR"),
    }
