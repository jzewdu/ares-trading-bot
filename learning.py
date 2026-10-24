"""The desk's memory: which kinds of trade have actually paid.

Every closed trade is filed under four nested buckets, from most to least
specific:

    trend|BTC|adx=24-32|vol=0.8-1.5
    trend|BTC
    trend|adx=24-32|vol=0.8-1.5
    trend

Each bucket keeps a decayed count, win count and sum of net R multiples. A
new trade is scored against the most specific bucket that has enough evidence,
falling back to its parents when it does not. Losers shrink the position or
block the bucket; winners raise it. Because the prior is a small positive
expectancy, a bucket has to build a real losing record before the desk refuses
to take that trade again.
"""
from __future__ import annotations

from datetime import datetime, timezone

import state

PRIOR_MEAN_R = 0.10     # what an average setup is expected to return, in R
PRIOR_WEIGHT = 6.0      # pseudo-trades behind that prior
MIN_SAMPLES = 5.0       # evidence needed before the learner adjusts size
AVOID_SAMPLES = 8.0     # evidence needed before it refuses a bucket
HALF_LIFE_DAYS = 30.0   # older trades matter exponentially less


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def adx_bucket(adx: float) -> str:
    if adx < 18:
        return "0-18"
    if adx < 24:
        return "18-24"
    if adx < 32:
        return "24-32"
    return "32+"


def vol_bucket(atr_pct: float) -> str:
    if atr_pct < 0.8:
        return "0-0.8"
    if atr_pct < 1.5:
        return "0.8-1.5"
    if atr_pct < 2.5:
        return "1.5-2.5"
    return "2.5+"


def keys_for(gear: str, coin: str, adx: float, atr_pct: float) -> list[str]:
    """Most specific key first."""
    ab, vb = adx_bucket(adx), vol_bucket(atr_pct)
    return [
        f"gear={gear}|coin={coin}|adx={ab}|vol={vb}",
        f"gear={gear}|coin={coin}",
        f"gear={gear}|adx={ab}|vol={vb}",
        f"gear={gear}",
    ]


def _decay(stamp: str) -> float:
    if not stamp:
        return 1.0
    try:
        then = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        days = max(0.0, (datetime.now(timezone.utc) - then).total_seconds() / 86400)
    except ValueError:
        return 1.0
    if days < 1 / 24:      # no decay for same-session trades
        return 1.0
    return 0.5 ** (days / HALF_LIFE_DAYS)


def stats(keys: list[str]) -> dict:
    row = state.learn_get(keys[0]) if keys else None
    # Evidence decays on read as well as on write, so a bucket that stops
    # trading slowly loses its authority instead of staying frozen forever.
    factor = _decay(str(row.get("updated_ts") or "")) if row else 1.0
    n = float(row["n"]) * factor if row else 0.0
    wins = float(row["wins"]) * factor if row else 0.0
    sum_r = float(row["sum_r"]) * factor if row else 0.0
    exp = (sum_r + PRIOR_WEIGHT * PRIOR_MEAN_R) / (n + PRIOR_WEIGHT)
    return {
        "key": keys[0] if keys else "",
        "n": n, "wins": wins, "sum_r": sum_r,
        "win_pct": (wins + 1) / (n + 2) * 100,
        "expectancy": exp,
    }


def update(trade: dict) -> list[str]:
    """Fold one closed trade into every bucket it belongs to."""
    gear = str(trade.get("gear") or "")
    coin = str(trade.get("coin") or "")
    if not gear or not coin:
        return []
    r = float(trade.get("r_multiple") or 0.0)
    keys = keys_for(gear, coin, float(trade.get("adx") or 0.0), float(trade.get("atr_pct") or 0.0))
    won = 1.0 if r > 0 else 0.0
    lessons: list[str] = []
    for key in keys:
        row = state.learn_get(key)
        if row:
            factor = _decay(str(row.get("updated_ts") or ""))
            n = float(row["n"]) * factor
            wins = float(row["wins"]) * factor
            sum_r = float(row["sum_r"]) * factor
        else:
            n = wins = sum_r = 0.0
        n += 1.0
        wins += won
        sum_r += r
        state.learn_put(key, n, wins, sum_r)
        if key == keys[-1] or key == keys[0]:
            exp = (sum_r + PRIOR_WEIGHT * PRIOR_MEAN_R) / (n + PRIOR_WEIGHT)
            lessons.append(
                f"{key}: {r:+.2f}R filed — {n:.0f} trades, {wins / n * 100:.0f}% win, "
                f"{exp:+.2f}R learned expectancy"
            )
    return lessons


def advice(gear: str, coin: str, adx: float, atr_pct: float) -> dict:
    """Size multiplier and stance for a candidate, from the best evidence."""
    keys = keys_for(gear, coin, adx, atr_pct)
    chosen = None
    scope = "none"
    labels = ("coin + context", "coin", "context", "gear")
    for key, label in zip(keys, labels):
        row = stats([key])
        if row["n"] >= MIN_SAMPLES:
            chosen, scope = row, label
            break
    if chosen is None:
        chosen, scope = stats([keys[-1]]), "gear (thin)"
    n = chosen["n"]
    exp = chosen["expectancy"]
    if n >= AVOID_SAMPLES and exp <= 0.0:
        stance, mult = "avoid", 0.0
    elif n >= MIN_SAMPLES and exp >= 0.25:
        stance, mult = "lean_in", _clamp(0.75 + exp * 2.0, 0.35, 1.4)
    elif n >= MIN_SAMPLES and exp < 0.05:
        stance, mult = "cooling", _clamp(0.75 + exp * 2.0, 0.35, 1.4)
    elif n >= MIN_SAMPLES:
        stance, mult = "neutral", _clamp(0.75 + exp * 2.0, 0.35, 1.4)
    else:
        stance, mult = "learning", 1.0
    note = (f"{chosen['key']} — {n:.0f} trades, {chosen['win_pct']:.0f}% win, "
            f"{exp:+.2f}R expectancy, size x{mult:.2f}")
    return {
        "key": chosen["key"], "scope": scope, "n": n, "wins": chosen["wins"],
        "win_pct": chosen["win_pct"], "expectancy": exp,
        "stance": stance, "mult": mult, "note": note,
    }


def summary() -> dict:
    rows = state.learn_rows()
    trades = state.closed_trades(500)
    total = len(trades)
    wins = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
    r_sum = sum(float(t.get("r_multiple") or 0) for t in trades)
    buckets = []
    for row in rows:
        if not str(row["key"]).startswith("gear="):
            continue
        current = stats([str(row["key"])])
        n = current["n"]
        exp = current["expectancy"]
        buckets.append({
            "key": row["key"], "n": n, "wins": current["wins"],
            "win_pct": current["win_pct"],
            "expectancy": exp,
            "stance": ("avoid" if n >= AVOID_SAMPLES and exp <= 0 else
                       "lean_in" if n >= MIN_SAMPLES and exp >= 0.25 else
                       "cooling" if n >= MIN_SAMPLES and exp < 0.05 else
                       "neutral" if n >= MIN_SAMPLES else "learning"),
        })
    buckets.sort(key=lambda b: (b["n"], b["expectancy"]), reverse=True)
    return {
        "trades": total, "wins": wins,
        "win_pct": (wins / total * 100) if total else 0.0,
        "avg_r": (r_sum / total) if total else 0.0,
        "expectancy": (r_sum / total) if total else 0.0,
        "buckets": buckets[:40],
        "prior_mean_r": PRIOR_MEAN_R, "prior_weight": PRIOR_WEIGHT,
        "min_samples": MIN_SAMPLES, "avoid_samples": AVOID_SAMPLES,
    }
