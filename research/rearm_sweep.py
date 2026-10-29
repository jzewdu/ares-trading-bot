"""Parameter sweep for the BTC trend core, one bar load for every combination.

    python research/rearm_sweep.py --days 900
"""
from __future__ import annotations

import itertools
import pathlib
import sys
from dataclasses import replace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import research.btc_bt as bt          # noqa: E402
from config import SETTINGS            # noqa: E402

EMAS = (100, 150, 200, 300, 400)
BRAKES = (1.5, 2.0, 2.5, 3.0)
REARMS = (0.0, 1.0, 1.5, 2.0, 3.0)


CANDIDATES = ((400, 3.0, 2.0), (400, 2.5, 1.5), (300, 3.0, 3.0), (200, 2.0, 3.0),
              (200, 1.5, 3.0), (400, 2.5, 2.0), (200, 2.0, 1.0))


def hold_pct(bars: list[dict]) -> float:
    return (bars[-1]["c"] / bars[0]["c"] - 1) * 100


def check(bars: list[dict]) -> None:
    """Split the history in halves so a lucky window cannot pose as an edge."""
    half = len(bars) // 2
    slices = (("full", bars), ("first half", bars[:half]), ("second half", bars[half:]))
    original = bt.SETTINGS
    print(f"{'config':>16} {'window':>12} {'return':>9} {'maxDD':>7} {'trades':>7} {'hold':>8}")
    try:
        for ema, brake, rearm in CANDIDATES:
            bt.SETTINGS = replace(original, btc_ema_bars=ema, btc_brake_atr=brake,
                                  btc_rearm_atr=rearm)
            for label, chunk in slices:
                result = bt.run(chunk, len(chunk))
                print(f"{f'{ema}/{brake}/{rearm}':>16} {label:>12} {result['total_pct']:8.2f}% "
                      f"{result['max_dd_pct']:6.2f}% {result['trades']:7d} {hold_pct(chunk):7.2f}%")
    finally:
        bt.SETTINGS = original


def main() -> None:
    days = 900
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    bars = bt.load(days)
    if "--check" in sys.argv:
        check(bars)
        return
    print(f"{len(bars)} 4h bars · {(bars[-1]['ts'] - bars[0]['ts']) / 86400:.0f} days "
          f"· {len(EMAS) * len(BRAKES) * len(REARMS)} combinations")
    rows = []
    original = bt.SETTINGS
    try:
        for ema, brake, rearm in itertools.product(EMAS, BRAKES, REARMS):
            bt.SETTINGS = replace(original, btc_ema_bars=ema, btc_brake_atr=brake,
                                  btc_rearm_atr=rearm)
            result = bt.run(bars, days)
            rows.append((result["total_pct"], result["max_dd_pct"], result["trades"],
                         result["avg_r"], ema, brake, rearm))
    finally:
        bt.SETTINGS = original

    rows.sort(reverse=True)
    print(f"{'return':>9} {'maxDD':>7} {'trades':>7} {'avgR':>7}   ema  brake rearm")
    for total, dd, trades, avg, ema, brake, rearm in rows[:18]:
        print(f"{total:8.2f}% {dd:6.2f}% {trades:7d} {avg:+7.2f}   {ema:4d} {brake:5.1f} {rearm:5.1f}")
    print("--- baseline (200 / 2.0 / 1.0) ---")
    for total, dd, trades, avg, ema, brake, rearm in rows:
        if (ema, brake, rearm) == (200, 2.0, 1.0):
            print(f"{total:8.2f}% {dd:6.2f}% {trades:7d} {avg:+7.2f}   {ema} {brake} {rearm}")
    check(bars)


if __name__ == "__main__":
    main()
