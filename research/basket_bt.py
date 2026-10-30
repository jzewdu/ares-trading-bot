"""The production rule, fed daily bars, across a basket of majors.

Everything here calls the *live* rule (`btc_trend.analyse`, `signal_for`) and the
*live* brake/re-arm logic, so the numbers cannot drift from what the desk does.
The only things this file adds are a bar feed, a cost model and accounting.

Why daily bars: the live desk reads a 200-bar line on 4-hour candles, which is a
33-day EMA. On an instrument that charges 25bp per side, a 33-day trend is fast
enough to hand most of the edge to the exchange. A 200-*day* line holds the same
kind of trend but crosses a handful of times a year instead of a handful of
times a quarter.

    python research/basket_bt.py
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from btc_trend import analyse, signal_for  # noqa: E402
from config import SETTINGS  # noqa: E402
from regime import STAND_ASIDE, TREND  # noqa: E402

DATA = pathlib.Path(__file__).with_name("data")
COINS = ("BTC", "ETH", "SOL", "LINK", "AVAX", "DOGE", "LTC", "BCH")
COST_PER_SIDE = 0.0030          # 25bp taker + 5bp slippage


def load(coin: str) -> list[dict]:
    path = DATA / f"{coin}USD_1d.json"
    if not path.exists():
        return []
    rows = []
    for r in json.loads(path.read_text()):
        try:
            rows.append({"ts": int(__import__("datetime").datetime.fromisoformat(
                r["t"].replace("Z", "+00:00")).timestamp()),
                "o": float(r["o"]), "h": float(r["h"]), "l": float(r["l"]),
                "c": float(r["c"])})
        except (KeyError, TypeError, ValueError):
            continue
    rows.sort(key=lambda r: r["ts"])
    return rows


def run_coin(bars: list[dict], settings, coin: str = "") -> dict:
    """One sleeve, following the live desk's own decisions."""
    rows = analyse(bars, settings)
    if not rows:
        return {"trades": [], "curve": [], "equity": 1.0}
    by_ts = {r.ts: r for r in rows}
    equity, pos, entry_px, stop, entry_ts = 1.0, 0, 0.0, 0.0, 0
    entry_atr_pct = 0.0
    last_exit_ts, last_exit_brake = 0, False
    curve, trades = [], []
    peak, max_dd = 1.0, 0.0

    for bar in bars:
        row = by_ts.get(bar["ts"])
        if row is None:
            continue
        price = bar["c"]
        # ---- exits (evaluated on the close, like the engine's manage pass)
        if pos:
            reason = None
            if price <= stop:
                reason = "stop"
            elif row.gear == STAND_ASIDE and row.direction == "down":
                reason = "brake"
            if reason:
                equity *= (1.0 + (price / entry_px - 1.0)) * (1.0 - COST_PER_SIDE)
                trades.append({"coin": coin, "entry_ts": entry_ts,
                               "exit_ts": bar["ts"], "entry": entry_px, "exit": price,
                               "pnl_pct": (price / entry_px - 1.0) * 100,
                               "atr_pct": entry_atr_pct, "reason": reason})
                pos, last_exit_ts, last_exit_brake = 0, bar["ts"], (reason == "brake")
        # ---- entries
        if not pos and signal_for(row, settings) is not None:
            blocked = False
            if last_exit_brake and settings.btc_rearm_atr > 0:
                need = row.ema_slow + settings.btc_rearm_atr * row.atr
                blocked = price < need
            if not blocked and not (last_exit_ts and bar["ts"] == last_exit_ts):
                pos, entry_px, entry_ts = 1, price, bar["ts"]
                entry_atr_pct = row.atr / price * 100 if price else 0.0
                stop = price - settings.trend_stop_atr * row.atr
                equity *= 1.0 - COST_PER_SIDE
        mark = equity * (1.0 + (price / entry_px - 1.0)) if pos else equity
        peak = max(peak, mark)
        max_dd = max(max_dd, 1.0 - mark / peak)
        curve.append((bar["ts"], mark))
    return {"trades": trades, "curve": curve, "equity": equity, "max_dd": max_dd}


def stats(result: dict) -> dict:
    trades = result["trades"]
    wins = sum(1 for t in trades if t["pnl_pct"] > 0)
    return {
        "ret_pct": (result["equity"] - 1.0) * 100,
        "max_dd_pct": result["max_dd"] * 100,
        "trades": len(trades),
        "win_pct": (wins / len(trades) * 100) if trades else 0.0,
        "avg_pct": (sum(t["pnl_pct"] for t in trades) / len(trades)) if trades else 0.0,
    }


def main() -> None:
    from dataclasses import replace
    settings = replace(SETTINGS, btc_ema_bars=200, btc_brake_atr=3.0, btc_rearm_atr=1.5,
                       trend_stop_atr=8.0)

    print(f"rule: {settings.btc_ema_bars}-day EMA, {settings.btc_brake_atr:.1f}-ATR brake, "
          f"{settings.btc_rearm_atr:.1f}-ATR re-arm, {settings.trend_stop_atr:.0f}-ATR stop")
    print(f"costs: {COST_PER_SIDE * 100:.2f}% per side\n")
    print(f"{'coin':5s} {'bars':>5s} {'ret':>9s} {'maxDD':>7s} {'trades':>6s} {'win':>5s} "
          f"{'avg':>7s}  {'now':<11s} {'px':>10s} {'line':>10s} {'brake':>10s}")

    sleeves, now_long = {}, []
    for coin in COINS:
        bars = load(coin)
        if len(bars) < settings.btc_ema_bars + 30:
            print(f"{coin:5s} {len(bars):>5d}  (not enough history)")
            continue
        result = run_coin(bars, settings, coin)
        s = stats(result)
        rows = analyse(bars, settings)
        row = rows[-1]
        state = "LONG" if signal_for(row, settings) else "cash"
        if signal_for(row, settings):
            now_long.append(coin)
        sleeves[coin] = result
        print(f"{coin:5s} {len(bars):>5d} {s['ret_pct']:>8.1f}% {s['max_dd_pct']:>6.1f}% "
              f"{s['trades']:>6d} {s['win_pct']:>4.0f}% {s['avg_pct']:>+6.2f}%  "
              f"{state:<11s} {row.close:>10,.4g} {row.ema_fast:>10,.4g} {row.ema_slow:>10,.4g}")

    if not sleeves:
        return
    # Equal-weight sleeves: the portfolio is the mean of the sleeves' marks.
    marks: dict[int, list[float]] = {}
    for result in sleeves.values():
        for ts, value in result["curve"]:
            marks.setdefault(ts, []).append(value)
    stamps = sorted(marks)
    portfolio = [sum(marks[t]) / len(sleeves) for t in stamps]
    total = (portfolio[-1] - 1.0) * 100
    peak, dd = portfolio[0], 0.0
    for v in portfolio:
        peak = max(peak, v)
        dd = max(dd, 1.0 - v / peak)
    rets = [portfolio[i] / portfolio[i - 1] - 1 for i in range(1, len(portfolio))]
    mean = sum(rets) / len(rets)
    sd = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5
    years = len(portfolio) / 365.0
    all_trades = [t for r in sleeves.values() for t in r["trades"]]
    wins = sum(1 for t in all_trades if t["pnl_pct"] > 0)

    print(f"\nequal-weight basket of {len(sleeves)}: {total:+.1f}% "
          f"({years:.1f}y, CAGR {((1 + total / 100) ** (1 / years) - 1) * 100:+.1f}%), "
          f"maxDD {dd * 100:.1f}%, Sharpe {mean / sd * 365 ** 0.5:.2f}")
    print(f"trades {len(all_trades)} ({len(all_trades) / years:.1f}/yr), "
          f"win {wins / len(all_trades) * 100:.0f}%, "
          f"avg {sum(t['pnl_pct'] for t in all_trades) / len(all_trades):+.2f}%")

    # Half-sample stability: a rule that only works in one half is a curve fit.
    mid = len(portfolio) // 2
    print(f"  first half  {(portfolio[mid] / portfolio[0] - 1) * 100:+.1f}%")
    print(f"  second half {(portfolio[-1] / portfolio[mid] - 1) * 100:+.1f}%")

    # Buy and hold over the same window, equal weight.
    bh = []
    for coin in sleeves:
        bars = load(coin)
        by_ts = {b["ts"]: b["c"] for b in bars}
        window = [b for b in bars if b["ts"] >= stamps[0]]
        if len(window) > 2:
            bh.append(window[-1]["c"] / window[0]["c"])
    if bh:
        print(f"  buy and hold, same window: {(sum(bh) / len(bh) - 1) * 100:+.1f}%")

    print(f"\nlong today: {', '.join(now_long) if now_long else 'nothing — all in cash'}")


if __name__ == "__main__":
    main()
