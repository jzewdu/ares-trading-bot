"""Market regime detection: which gear the market is offering right now.

The desk trades two very different behaviours, so the first job each cycle is
to decide which one the chart is actually offering:

* **trend** - directional persistence. ADX above 22, Kaufman efficiency above
  0.30, and the fast EMA separated from the slow one in the same direction.
* **range** - mean reversion. ADX below 18, efficiency below 0.22, and a band
  wide enough that a fade has somewhere to go.
* **stand_aside** - neither. Trending down also lands here because Alpaca's
  crypto book is spot-only: the desk has no way to be short.

Everything is measured on closed 4-hour bars built from hourly Alpaca bars.
The forming bar is dropped, so a signal can only use information that was
already complete when the desk acted on it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

HOURS = 4
TREND = "trend"
RANGE = "range"
STAND_ASIDE = "stand_aside"


@dataclass
class Regime:
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


def aggregate(hourly: list[dict], hours: int = HOURS) -> list[dict]:
    """Fold hourly bars into aligned 4-hour bars, dropping the forming one."""
    buckets: dict[int, dict] = {}
    for bar in hourly:
        try:
            stamp = bar.get("ts", bar.get("t"))
            ts = int(stamp.timestamp()) if hasattr(stamp, "timestamp") else _epoch(stamp)
            o, h, l, c = float(bar["o"]), float(bar["h"]), float(bar["l"]), float(bar["c"])
        except (KeyError, TypeError, ValueError):
            continue
        slot = ts - ts % (hours * 3600)
        row = buckets.get(slot)
        if row is None:
            buckets[slot] = {"ts": slot, "o": o, "h": h, "l": l, "c": c, "v": 1}
        else:
            row["h"] = max(row["h"], h)
            row["l"] = min(row["l"], l)
            row["c"] = c
            row["v"] += 1
    rows = [buckets[k] for k in sorted(buckets)]
    # A partial final bucket would leak an unfinished bar into the signal. The
    # very first bucket can also be partial when history starts mid-window.
    if rows and rows[-1]["v"] < hours:
        rows.pop()
    if len(rows) > 1 and rows[0]["v"] < hours:
        rows.pop(0)
    return rows


def _epoch(stamp) -> int:
    from datetime import datetime
    text = str(stamp)
    if text.isdigit():
        return int(text)
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2 / (period + 1)
    for i in range(period, len(values)):
        seed = values[i] * k + seed * (1 - k)
        out[i] = seed
    return out


def atr_series(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    tr = [0.0] * len(closes)
    for i in range(1, len(closes)):
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    value = sum(tr[1:period + 1]) / period
    out[period] = value
    for i in range(period + 1, len(closes)):
        value = (value * (period - 1) + tr[i]) / period
        out[i] = value
    return out


def adx_series(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float | None]:
    n = len(closes)
    out: list[float | None] = [None] * n
    if n <= period * 2:
        return out
    plus = [0.0] * n
    minus = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus[i] = up if up > down and up > 0 else 0.0
        minus[i] = down if down > up and down > 0 else 0.0
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    atr = sum(tr[1:period + 1]) / period
    sp = sum(plus[1:period + 1]) / period
    sm = sum(minus[1:period + 1]) / period
    dx_values: list[float] = []

    def dx_of(atr_v: float, sp_v: float, sm_v: float) -> float:
        di_plus = 100 * sp_v / atr_v if atr_v else 0.0
        di_minus = 100 * sm_v / atr_v if atr_v else 0.0
        total = di_plus + di_minus
        return 100 * abs(di_plus - di_minus) / total if total else 0.0

    adx = 0.0
    for i in range(period, n):
        if i > period:
            atr = (atr * (period - 1) + tr[i]) / period
            sp = (sp * (period - 1) + plus[i]) / period
            sm = (sm * (period - 1) + minus[i]) / period
        dx = dx_of(atr, sp, sm)
        dx_values.append(dx)
        if len(dx_values) == period:
            adx = sum(dx_values) / period
            out[i] = adx
        elif len(dx_values) > period:
            adx = (adx * (period - 1) + dx) / period
            out[i] = adx
    return out


def rsi_series(closes: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / period, losses / period

    def value() -> float:
        if avg_loss == 0:
            return 100.0
        return 100 - 100 / (1 + avg_gain / avg_loss)

    out[period] = value()
    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[i] = value()
    return out


def efficiency(closes: list[float], period: int = 20) -> list[float | None]:
    """Kaufman efficiency: net move divided by the path walked to get there."""
    out: list[float | None] = [None] * len(closes)
    for i in range(period, len(closes)):
        path = sum(abs(closes[j] - closes[j - 1]) for j in range(i - period + 1, i + 1))
        out[i] = abs(closes[i] - closes[i - period]) / path if path else 0.0
    return out


def _bands(closes: list[float], period: int = 20, mult: float = 2.0) -> list[tuple[float, float, float] | None]:
    out: list[tuple[float, float, float] | None] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1:i + 1]
        mid = sum(window) / period
        var = sum((x - mid) ** 2 for x in window) / period
        sd = var ** 0.5
        out[i] = (mid - mult * sd, mid, mid + mult * sd)
    return out


def _donchian(highs: list[float], lows: list[float], period: int = 20) -> list[tuple[float, float] | None]:
    out: list[tuple[float, float] | None] = [None] * len(highs)
    for i in range(period, len(highs)):
        out[i] = (max(highs[i - period:i]), min(lows[i - period:i]))
    return out


def classify(bars: list[dict], settings) -> list[Regime]:
    """One regime per completed bar, using only bars up to that bar."""
    if not bars:
        return []
    highs = [float(b["h"]) for b in bars]
    lows = [float(b["l"]) for b in bars]
    closes = [float(b["c"]) for b in bars]
    fast = ema(closes, 20)
    slow = ema(closes, 60)
    atr = atr_series(highs, lows, closes, 14)
    adx = adx_series(highs, lows, closes, 14)
    rsi = rsi_series(closes, 14)
    bands = _bands(closes, 20, 2.0)
    don = _donchian(highs, lows, 20)
    er = efficiency(closes, 20)

    warmup = int(max(60 * 2.2, 60))
    out: list[Regime] = []
    previous = ""
    stable = 0
    for i in range(len(bars)):
        if i < warmup or None in (fast[i], slow[i], atr[i], adx[i], rsi[i], bands[i], don[i], er[i]):
            continue
        close = closes[i]
        atr_v = float(atr[i])
        atr_pct = atr_v / close * 100 if close else 0.0
        spread = (float(fast[i]) - float(slow[i])) / close * 100 if close else 0.0
        bb_low, bb_mid, bb_up = bands[i]
        width = (bb_up - bb_low) / bb_mid * 100 if bb_mid else 0.0
        adx_v = float(adx[i])
        er_v = float(er[i])
        trending = adx_v >= settings.trend_adx and er_v >= settings.trend_er \
            and abs(spread) >= settings.trend_spread_pct
        ranging = adx_v <= settings.range_adx and er_v <= settings.range_er \
            and width >= settings.range_min_width

        if trending and spread > 0 and close > float(slow[i]):
            gear, direction = TREND, "up"
            reason = (f"ADX {adx_v:.1f}, efficiency {er_v:.2f}, EMA20 {spread:+.2f}% over EMA60 "
                      f"— trend gear, long side")
        elif trending and spread < 0 and close < float(slow[i]):
            gear, direction = STAND_ASIDE, "down"
            reason = (f"Downtrend: ADX {adx_v:.1f}, efficiency {er_v:.2f}, EMA20 {spread:+.2f}% "
                      f"— spot-only book stands aside")
        elif ranging:
            gear, direction = RANGE, "flat"
            reason = (f"ADX {adx_v:.1f}, efficiency {er_v:.2f}, band width {width:.1f}% "
                      f"— range gear, fade the edges")
        else:
            gear, direction = STAND_ASIDE, "flat"
            reason = (f"Mixed: ADX {adx_v:.1f}, efficiency {er_v:.2f}, spread {spread:+.2f}%, "
                      f"width {width:.1f}% — no clean gear")

        key = f"{gear}:{direction}"
        stable = stable + 1 if key == previous else 1
        previous = key
        out.append(Regime(
            ts=int(bars[i]["ts"]), gear=gear, direction=direction, close=close,
            adx=adx_v, er=er_v, atr=atr_v, atr_pct=atr_pct,
            ema_fast=float(fast[i]), ema_slow=float(slow[i]), trend_spread_pct=spread,
            rsi=float(rsi[i]), bb_low=float(bb_low), bb_mid=float(bb_mid), bb_up=float(bb_up),
            bb_width_pct=width, don_high=float(don[i][0]), don_low=float(don[i][1]),
            stable=stable, reason=reason,
        ))
    return out
