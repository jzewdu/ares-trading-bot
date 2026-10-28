"""Replay the BTC trend core on real Alpaca bars.

Same rule the live engine runs: own BTC while the 4-hour close is above its
200-bar EMA, sell on a close more than `BTC_BRAKE_ATR` ATRs below it, and keep
an 8-ATR catastrophe stop. Fees and slippage are charged on both sides.

    python research/btc_bt.py --days 240
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from alpaca_client import AlpacaClient          # noqa: E402
from btc_trend import analyse, signal_for       # noqa: E402
from config import SETTINGS                     # noqa: E402
from regime import STAND_ASIDE, TREND, aggregate  # noqa: E402

FEE = 0.0025
SLIP = 0.0005
START = 100_000.0


def _epoch(stamp) -> int:
    from datetime import datetime
    text = str(stamp)
    if text.isdigit():
        return int(text)
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def load(days: int) -> list[dict]:
    client = AlpacaClient()
    raw = client.bars(["BTC/USD"], days=days)
    hourly = []
    for bar in raw.get("BTC/USD", []):
        try:
            hourly.append({"ts": _epoch(bar["t"]), "o": float(bar["o"]), "h": float(bar["h"]),
                           "l": float(bar["l"]), "c": float(bar["c"])})
        except (KeyError, TypeError, ValueError):
            continue
    return aggregate(hourly, 4)


def run(bars: list[dict], days: int) -> dict:
    rows = {row.ts: row for row in analyse(bars, SETTINGS)}
    by_ts = {bar["ts"]: bar for bar in bars}
    times = sorted(by_ts)

    cash = START
    peak = START
    maxdd = 0.0
    pos = None
    trades: list[dict] = []
    curve: list[tuple[int, float]] = []
    last_exit_ts = 0

    for ts in times:
        bar = by_ts[ts]
        row = rows.get(ts)
        if pos:
            exit_price = None
            reason = ""
            if bar["o"] <= pos["stop"]:
                exit_price, reason = bar["o"], "gap through stop"
            elif bar["l"] <= pos["stop"]:
                exit_price, reason = pos["stop"], "stop"
            elif row and row.gear == STAND_ASIDE and row.direction == "down":
                exit_price, reason = bar["c"], "trend brake"
            if exit_price:
                fill = exit_price * (1 - SLIP)
                proceeds = pos["qty"] * fill * (1 - FEE)
                cash += proceeds
                pnl = proceeds - pos["cost"]
                trades.append({"gear": TREND, "pnl": pnl, "r": pnl / pos["risk"],
                               "reason": reason, "fees": pos["fee_in"] + pos["qty"] * fill * FEE})
                pos = None
                last_exit_ts = ts
        if pos is None and row and ts != times[-1]:
            sig = signal_for(row, SETTINGS)
            if sig and row.ts > last_exit_ts:
                price = bar["c"]
                entry = price * (1 + SLIP)
                stop_dist = sig["stop_dist"]
                risk = cash * SETTINGS.risk_pct
                notional = min(risk / stop_dist * entry, cash * SETTINGS.max_position_pct)
                if notional >= SETTINGS.min_notional:
                    qty = notional / entry
                    cash -= notional * (1 + FEE)
                    pos = {"qty": qty, "entry": entry, "cost": notional * (1 + FEE),
                           "stop": entry - stop_dist, "risk": qty * stop_dist,
                           "fee_in": notional * FEE}
        value = cash + (pos["qty"] * bar["c"] if pos else 0.0)
        peak = max(peak, value)
        maxdd = max(maxdd, (peak - value) / peak * 100)
        curve.append((ts, value))

    if pos:
        last = bars[-1]
        fill = last["c"] * (1 - SLIP)
        proceeds = pos["qty"] * fill * (1 - FEE)
        cash += proceeds
        pnl = proceeds - pos["cost"]
        trades.append({"gear": TREND, "pnl": pnl, "r": pnl / pos["risk"],
                       "reason": "open at end", "fees": pos["fee_in"] + pos["qty"] * fill * FEE})

    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    equity = cash
    long_frac = sum(1 for _, v in curve if v > 0)  # placeholder replaced below
    held = 0
    holding = False
    for ts in times:
        row = rows.get(ts)
        holding = bool(row and row.gear == TREND)
        held += 1 if holding else 0
    return {
        "window": f"last {days} days · BTC/USD · 4h bars",
        "window_days": days,
        "trades": len(trades),
        "win_pct": (len(wins) / len(trades) * 100) if trades else 0.0,
        "avg_r": (sum(t["r"] for t in trades) / len(trades)) if trades else 0.0,
        "total_pct": (equity - START) / START * 100,
        "max_dd_pct": maxdd,
        "fees": sum(t["fees"] for t in trades),
        "time_long_pct": held / max(1, len(times)) * 100,
        "by_gear": [{
            "gear": TREND, "trades": len(trades),
            "win_pct": (len(wins) / len(trades) * 100) if trades else 0.0,
            "avg_r": (sum(t["r"] for t in trades) / len(trades)) if trades else 0.0,
            "pnl": sum(pnls),
        }],
        "note": ("same rule, stop and fees as the live desk: 200-bar 4h trend line, "
                 "2-ATR brake, 8-ATR stop, 0.25% per side plus 0.05% slippage, 2% risk budget"),
        "trades_detail": trades,
        "curve": curve,
    }


def main() -> None:
    days = 240
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    bars = load(days)
    print(f"loaded {len(bars)} 4h bars from {bars[0]['ts']} to {bars[-1]['ts']}")
    result = run(bars, days)
    path = pathlib.Path(__file__).resolve().parent.parent / "btc_summary.json"
    path.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"{result['trades']} trades · {result['win_pct']:.0f}% win · "
          f"avg {result['avg_r']:+.2f}R · {result['total_pct']:+.2f}% · "
          f"DD {result['max_dd_pct']:.2f}% · long {result['time_long_pct']:.0f}% of the time")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
