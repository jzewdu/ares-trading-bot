"""Write `btc_summary.json` from the desk's own rule, so the dashboard cannot
show a replay of a strategy the desk no longer runs.

The panel used to quote a 4-hour test of a rule that lost money live. This
replays what ships: eight majors, daily bars, a 200-day line, a 3-ATR brake,
a 1.5-ATR re-arm and an 8-ATR catastrophe stop, at 0.30% a side.

    python research/desk_replay.py
"""
from __future__ import annotations

import json
import pathlib
import sys
from dataclasses import replace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import research.basket_bt as bt  # noqa: E402
from config import SETTINGS  # noqa: E402

CAPITAL = 100_000.0
OUT = pathlib.Path(__file__).resolve().parent.parent / "btc_summary.json"


def main() -> None:
    settings = replace(SETTINGS, btc_ema_bars=200, btc_brake_atr=3.0, btc_rearm_atr=1.5,
                       trend_stop_atr=8.0, bar_timeframe="1Day")
    sleeves, curves, detail, days = {}, {}, [], 0
    for coin in bt.COINS:
        bars = bt.load(coin)
        if len(bars) < settings.btc_ema_bars + 30:
            continue
        result = bt.run_coin(bars, settings, coin)
        sleeves[coin] = result
        curves[coin] = {ts: value for ts, value in result["curve"]}
        days = max(days, len(bars))
        sleeve = CAPITAL / len(bt.COINS)
        for t in result["trades"]:
            pnl = sleeve * t["pnl_pct"] / 100
            risk = sleeve * (settings.trend_stop_atr * t["atr_pct"] / 100)
            detail.append({
                "gear": "trend", "coin": coin, "reason": t["reason"],
                "pnl": round(pnl, 2), "r": round(pnl / risk, 4) if risk else 0.0,
                "fees": round(sleeve * bt.COST_PER_SIDE * 2, 2),
                "opened": t["entry_ts"], "closed": t["exit_ts"],
            })

    if not sleeves:
        raise SystemExit("no sleeves with enough history")

    # Equal-weight portfolio marks, the same construction as the basket test.
    marks: dict[int, list[float]] = {}
    for curve in curves.values():
        for ts, value in curve.items():
            marks.setdefault(ts, []).append(value)
    stamps = sorted(marks)
    values = [sum(marks[t]) / len(sleeves) * CAPITAL for t in stamps]
    total_pct = (values[-1] / CAPITAL - 1) * 100
    peak, max_dd = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        max_dd = max(max_dd, 1.0 - v / peak)

    rs = [d["r"] for d in detail]
    wins = sum(1 for d in detail if d["pnl"] > 0)
    fees = sum(d["fees"] for d in detail)
    summary = {
        "window": f"last {days} days · {len(sleeves)} majors · daily bars",
        "window_days": days,
        "trades": len(detail),
        "win_pct": wins / len(detail) * 100 if detail else 0.0,
        "avg_r": sum(rs) / len(rs) if rs else 0.0,
        "total_pct": total_pct,
        "max_dd_pct": max_dd * 100,
        "fees": fees,
        "time_long_pct": 0.0,
        "by_gear": [{
            "gear": "trend", "trades": len(detail),
            "win_pct": wins / len(detail) * 100 if detail else 0.0,
            "avg_r": sum(rs) / len(rs) if rs else 0.0,
            "pnl": sum(d["pnl"] for d in detail),
        }],
        "note": (f"the rule the desk actually runs: {settings.btc_ema_bars}-day trend line, "
                 f"{settings.btc_brake_atr:.1f}-ATR brake, {settings.btc_rearm_atr:.1f}-ATR "
                 f"re-arm, {settings.trend_stop_atr:.0f}-ATR stop, "
                 f"{bt.COST_PER_SIDE * 100:.2f}% per side and slippage, equal weight across "
                 f"{len(sleeves)} majors"),
        "trades_detail": detail,
        "curve": [[ts, round(v, 2)] for ts, v in zip(stamps, values)],
    }
    OUT.write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(f"{summary['window']}")
    print(f"  trades {summary['trades']}  win {summary['win_pct']:.0f}%  "
          f"avgR {summary['avg_r']:+.2f}  return {total_pct:+.2f}%  "
          f"maxDD {max_dd * 100:.2f}%  fees ${fees:,.0f}")
    print(f"wrote {OUT.name} ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
