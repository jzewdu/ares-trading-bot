"""Does the trend rule survive being run short as well as long, and does the
vehicle matter more than the rule?

The live desk is long-only spot BTC at 0.25% per side. That has two problems in
a bear market: a spot crypto account cannot short, so the desk sits in cash, and
the fee bill is large enough that every extra round trip has to be paid for out
of the edge. This harness tests the same rule on daily bars three ways:

  * long-only  - what the desk does now
  * two-sided  - long above the trend line, short below it
  * both, on IBIT instead of spot BTC - a shortable, commission-free vehicle

Execution model, deliberately conservative:

  * a signal is read from a *completed* daily bar and acted on at the next
    bar's open, never at the close that produced it;
  * costs are charged on every change of position, at the vehicle's own rate
    (crypto 25bp/side + 5bp slippage; equities 3bp/side, no commission);
  * a short pays a borrow fee per day;
  * no leverage: one unit of capital.

    python research/twoside_bt.py
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from regime import atr_series, ema  # noqa: E402

DATA = pathlib.Path(__file__).with_name("data")

VEHICLES = {
    # name: (cost per side, daily short borrow, label)
    "crypto": (0.0030, 0.0, "spot BTC, 25bp/side + slip"),
    "equity": (0.0003, 0.00002, "IBIT, 3bp/side, borrow 0.7%/yr"),
}


def load(name: str) -> list[dict]:
    rows = json.loads((DATA / f"{name}_1d.json").read_text())
    out = []
    for r in rows:
        try:
            out.append({"t": r["t"], "o": float(r["o"]), "h": float(r["h"]),
                        "l": float(r["l"]), "c": float(r["c"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def indicators(bars: list[dict], ema_bars: int):
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    closes = [b["c"] for b in bars]
    return ema(closes, ema_bars), atr_series(highs, lows, closes, 14)


def signal(bars, line, atr, i, *, brake_atr, entry_atr, rearm_atr, sides):
    """Target position for the open that follows bar i.

    A mirror image around the trend line: long above it, short below it, and a
    position is only surrendered once the close is a full band past the line.
    After an exit the close has to travel back through the band before the desk
    pays another round trip, which is the live desk's re-arm gate made
    symmetric.
    """
    close, atr_v = bars[i]["c"], atr[i]
    if not atr_v or not line[i]:
        return None
    above = (close - line[i]) / atr_v          # distance from the line in ATRs
    if above > entry_atr:
        return 1
    if sides == "two" and above < -entry_atr:
        return -1
    return 0


def run(bars, *, ema_bars=200, brake_atr=3.0, entry_atr=0.0, rearm_atr=1.5,
        sides="two", cost=0.003, borrow=0.0):
    line, atr = indicators(bars, ema_bars)
    warmup = ema_bars + 15
    equity, peak, max_dd = 1.0, 1.0, 0.0
    pos, last_exit, trades, wins = 0, 0, 0, 0
    entry_px, entry_side, exposure_days = 0.0, 0, 0
    curve = []
    r_multiples = []

    for i in range(warmup, len(bars) - 1):
        # 1. mark the position to the next open
        nxt_open = bars[i + 1]["o"]
        if pos != 0:
            ret = nxt_open / bars[i]["o"] - 1.0 if bars[i]["o"] else 0.0
            equity *= 1.0 + pos * ret
            if pos < 0:
                equity *= 1.0 - borrow
            exposure_days += 1
        # 2. decide the target from this completed bar
        target = signal(bars, line, atr, i, brake_atr=brake_atr,
                        entry_atr=entry_atr, rearm_atr=rearm_atr, sides=sides)
        if target is None:
            target = pos
        # 3. the band: only surrender a position past the brake
        if pos == 1 and target != 1:
            if bars[i]["c"] > line[i] - brake_atr * atr[i]:
                target = 1
        if pos == -1 and target != -1:
            if bars[i]["c"] < line[i] + brake_atr * atr[i]:
                target = -1
        # 4. re-arm: after an exit, require the band to be reclaimed
        if target != 0 and target != pos and last_exit == target:
            need = line[i] + target * rearm_atr * atr[i]
            if (target > 0 and bars[i]["c"] < need) or (target < 0 and bars[i]["c"] > need):
                target = 0
        # 5. pay to change
        if target != pos:
            if pos != 0 and entry_px:
                pnl = pos * (nxt_open / entry_px - 1.0)
                r_multiples.append(pnl / max(brake_atr * atr[i] / bars[i]["c"], 1e-9))
                trades += 1
                wins += 1 if pnl > 0 else 0
                last_exit = pos
            equity *= 1.0 - cost
            if target != 0:
                equity *= 1.0 - cost
                entry_px = nxt_open
            pos = target
        peak = max(peak, equity)
        max_dd = max(max_dd, 1.0 - equity / peak)
        curve.append((bars[i + 1]["t"], equity))

    days = (len(curve) or 1)
    years = days / 365.0
    total = equity - 1.0
    cagr = (equity ** (1 / years) - 1) if years > 0 and equity > 0 else 0.0
    rets = []
    for k in range(1, len(curve)):
        prev = curve[k - 1][1]
        rets.append(curve[k][1] / prev - 1 if prev else 0.0)
    mean = sum(rets) / len(rets) if rets else 0.0
    sd = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5 if rets else 0.0
    sharpe = (mean / sd * (365 ** 0.5)) if sd else 0.0
    return {
        "total_pct": total * 100, "cagr_pct": cagr * 100, "max_dd_pct": max_dd * 100,
        "trades": trades, "win_pct": (wins / trades * 100) if trades else 0.0,
        "avg_r": (sum(r_multiples) / len(r_multiples)) if r_multiples else 0.0,
        "sharpe": sharpe, "exposure_pct": exposure_days / max(days, 1) * 100,
        "days": days, "curve": curve,
    }


def buy_hold(bars, start_frac=0.15):
    start = int(len(bars) * start_frac)
    first, last = bars[start]["o"], bars[-1]["c"]
    return (last / first - 1.0) * 100


def halves(bars, **kw):
    mid = len(bars) // 2
    a = run(bars[:mid], **kw)
    b = run(bars[mid:], **kw)
    return a, b


def main() -> None:
    btc, ibit = load("BTCUSD"), load("IBIT")
    print(f"BTC daily {len(btc)} bars {btc[0]['t'][:10]}..{btc[-1]['t'][:10]}")
    print(f"IBIT daily {len(ibit)} bars {ibit[0]['t'][:10]}..{ibit[-1]['t'][:10]}")
    print(f"buy & hold BTC {buy_hold(btc):+.1f}%   IBIT {buy_hold(ibit):+.1f}%")

    grid = []
    for sides in ("one", "two"):
        for ema_b in (100, 150, 200):
            for brake in (2.0, 3.0, 4.0):
                for entry in (0.0, 0.5, 1.0):
                    for vehicle, (cost, borrow, _) in VEHICLES.items():
                        kw = dict(ema_bars=ema_b, brake_atr=brake, entry_atr=entry,
                                  rearm_atr=1.5, sides=sides, cost=cost, borrow=borrow)
                        bars = ibit if vehicle == "equity" else btc
                        r = run(bars, **kw)
                        h1, h2 = halves(bars, **kw)
                        grid.append((vehicle, sides, ema_b, brake, entry, r, h1, h2))

    print("\nsame rule, two vehicles, one side or two "
          "(daily bars, signals acted on at the next open)")
    print(f"{'vehicle':7s} {'sides':5s} {'ema':>4s} {'brake':>5s} {'entry':>5s} "
          f"{'ret':>8s} {'maxDD':>7s} {'Sharpe':>6s} {'trades':>6s} {'win':>5s} {'avgR':>6s}")
    for vehicle, sides, ema_b, brake, entry, r, h1, h2 in sorted(
            grid, key=lambda g: -g[5]["total_pct"])[:24]:
        print(f"{vehicle:7s} {sides:5s} {ema_b:4d} {brake:5.1f} {entry:5.1f} "
              f"{r['total_pct']:7.1f}% {r['max_dd_pct']:6.1f}% {r['sharpe']:6.2f} "
              f"{r['trades']:6d} {r['win_pct']:4.0f}% {r['avg_r']:+6.2f}")

    print("\nrobustness: both halves must make money (same settings)")
    print(f"{'vehicle':7s} {'sides':5s} {'ema':>4s} {'brake':>5s} {'entry':>5s} "
          f"{'half1':>8s} {'half2':>8s} {'ret':>8s} {'maxDD':>7s}")
    robust = [g for g in grid if g[6]["total_pct"] > 0 and g[7]["total_pct"] > 0]
    for vehicle, sides, ema_b, brake, entry, r, h1, h2 in sorted(
            robust, key=lambda g: -(g[6]["total_pct"] + g[7]["total_pct"]))[:20]:
        print(f"{vehicle:7s} {sides:5s} {ema_b:4d} {brake:5.1f} {entry:5.1f} "
              f"{h1['total_pct']:7.1f}% {h2['total_pct']:7.1f}% "
              f"{r['total_pct']:7.1f}% {r['max_dd_pct']:6.1f}%")
    print(f"\n{len(robust)} of {len(grid)} combinations are positive in both halves")


if __name__ == "__main__":
    main()
