"""Replay the gear-shift rules on real Alpaca 4h bars.

This is the same regime code and the same signal code the live engine runs,
with fees and slippage charged on both sides. It is deliberately static: the
live learner starts neutral, so the replay shows what the rules do before any
trade memory exists.

    .venv/bin/python research/gears_bt.py                 # default window
    .venv/bin/python research/gears_bt.py --days 180
    .venv/bin/python research/gears_bt.py --grid          # threshold sensitivity
"""
from __future__ import annotations

import json
import pathlib
import sys
from dataclasses import replace
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from alpaca_client import AlpacaClient          # noqa: E402
from config import SETTINGS                     # noqa: E402
from gears import signal_for                    # noqa: E402
from regime import STAND_ASIDE, TREND, aggregate, classify  # noqa: E402

FEE = 0.0025         # Alpaca crypto taker, each side
SLIP = 0.0005        # market-order slippage, each side
START = 100_000.0


def _epoch(stamp) -> int:
    text = str(stamp)
    if text.isdigit():
        return int(text)
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def load(days: int) -> dict[str, list[dict]]:
    client = AlpacaClient()
    symbols = [SETTINGS.spot_symbol(c) for c in SETTINGS.universe]
    raw = client.bars(symbols, days=days)
    out: dict[str, list[dict]] = {}
    for coin in SETTINGS.universe:
        rows = []
        for bar in raw.get(SETTINGS.spot_symbol(coin), []):
            try:
                rows.append({"ts": _epoch(bar["t"]), "o": float(bar["o"]), "h": float(bar["h"]),
                             "l": float(bar["l"]), "c": float(bar["c"])})
            except (KeyError, TypeError, ValueError):
                continue
        bars = aggregate(rows, 4)
        if len(bars) > 150:
            out[coin] = bars
    return out


def run(data: dict[str, list[dict]], settings, days: int) -> dict:
    regimes: dict[str, dict[int, object]] = {}
    bars_at: dict[str, dict[int, dict]] = {}
    timeline: set[int] = set()
    for coin, bars in data.items():
        regimes[coin] = {row.ts: row for row in classify(bars, settings)}
        bars_at[coin] = {bar["ts"]: bar for bar in bars}
        timeline |= {bar["ts"] for bar in bars}
    times = sorted(timeline)

    cash = START
    positions: dict[str, dict] = {}
    trades: list[dict] = []
    pending: dict[str, object] = {}
    curve: list[tuple[int, float]] = []

    def equity_now(ts: int) -> float:
        value = cash
        for coin, pos in positions.items():
            bar = bars_at[coin].get(ts)
            if bar:
                value += pos["qty"] * bar["c"]
        return value

    for ts in times:
        # --- exits on this bar, stops before targets (conservative)
        for coin in list(positions):
            pos = positions[coin]
            bar = bars_at[coin].get(ts)
            if not bar:
                continue
            if bar["ts"] <= pos["entry_ts"]:
                continue
            exit_price = None
            reason = ""
            if bar["o"] <= pos["stop"]:
                exit_price, reason = bar["o"], "gap through stop"
            elif bar["l"] <= pos["stop"]:
                exit_price, reason = pos["stop"], "stop"
            elif bar["o"] >= pos["target"]:
                exit_price, reason = bar["o"], "gap through target"
            elif bar["h"] >= pos["target"]:
                exit_price, reason = pos["target"], "target"
            else:
                pos["high"] = max(pos["high"], bar["h"])
                reg = regimes[coin].get(ts)
                if pos["gear"] == TREND:
                    if reg and reg.gear == STAND_ASIDE and reg.direction == "down" and reg.stable >= 2:
                        exit_price, reason = bar["c"], "trend reversed"
                    elif (ts - pos["entry_ts"]) / 86400 >= settings.trend_max_days:
                        exit_price, reason = bar["c"], "time stop"
                    elif reg:
                        trail = pos["high"] - settings.trail_atr * reg.atr
                        pos["stop"] = max(pos["stop"], trail)
                else:
                    if reg and reg.gear != "range" and reg.stable >= 2:
                        exit_price, reason = bar["c"], "range broke"
                    elif (ts - pos["entry_ts"]) / 86400 >= settings.range_max_days:
                        exit_price, reason = bar["c"], "time stop"
            if exit_price:
                exit_fill = exit_price * (1 - SLIP)
                gross = pos["qty"] * exit_fill
                proceeds = gross * (1 - FEE)
                cash += proceeds
                pnl = proceeds - pos["cost"]
                trades.append({"coin": coin, "gear": pos["gear"], "setup": pos["setup"],
                               "pnl": pnl, "r": pnl / pos["risk"], "reason": reason,
                               "opened": pos["entry_ts"], "closed": ts,
                               "fees": pos["fee_in"] + gross * FEE,
                               "score": pos["score"], "adx": pos["adx"], "atr_pct": pos["atr_pct"]})
                del positions[coin]

        # --- entries from the previous close, filled at this bar's open
        for coin, reg in list(pending.items()):
            pending.pop(coin, None)
            if coin in positions or len(positions) >= settings.max_positions:
                continue
            bar = bars_at[coin].get(ts)
            if not bar:
                continue
            sig = signal_for(reg, settings)
            if not sig or sig["score"] < 50:
                continue
            entry = bar["o"] * (1 + SLIP)
            stop_dist = entry - sig["stop"]
            if stop_dist <= 0:
                continue
            risk = equity_now(ts) * settings.risk_pct
            qty = risk / stop_dist
            notional = qty * entry
            notional = min(notional, equity_now(ts) * settings.max_position_pct,
                           max(0.0, equity_now(ts) * settings.max_exposure_pct - sum(
                               p["qty"] * bars_at[c].get(ts, {}).get("c", p["entry"]) for c, p in positions.items())))
            if notional < settings.min_notional or notional <= 0:
                continue
            qty = notional / entry
            cost = notional + notional * FEE
            if cost > cash:
                continue
            cash -= cost
            positions[coin] = {
                "qty": qty, "entry": entry, "cost": cost, "risk": qty * stop_dist,
                "stop": entry - stop_dist, "target": entry + (sig["target"] - sig["entry_ref"]),
                "gear": sig["gear"], "setup": sig["setup"], "high": entry,
                "entry_ts": ts, "score": sig["score"], "fee_in": notional * FEE,
                "adx": sig["features"]["adx"], "atr_pct": sig["features"]["atr_pct"],
            }

        # --- signals at this close, executed at the next bar's open
        for coin, reg in regimes.items():
            row = reg.get(ts)
            if row is None or coin in positions or coin in pending:
                continue
            if signal_for(row, settings):
                pending[coin] = row

        curve.append((ts, equity_now(ts)))

    # close whatever is still open at the last mark
    for coin, pos in list(positions.items()):
        bars = data[coin]
        last = bars[-1]
        exit_fill = last["c"] * (1 - SLIP)
        gross = pos["qty"] * exit_fill
        proceeds = gross * (1 - FEE)
        cash += proceeds
        pnl = proceeds - pos["cost"]
        trades.append({"coin": coin, "gear": pos["gear"], "setup": pos["setup"],
                       "pnl": pnl, "r": pnl / pos["risk"], "reason": "end of window",
                       "opened": pos["entry_ts"], "closed": last["ts"],
                       "fees": pos["fee_in"] + gross * FEE,
                       "score": pos["score"], "adx": pos["adx"], "atr_pct": pos["atr_pct"]})
        del positions[coin]

    closes = [t["pnl"] for t in trades]
    wins = [p for p in closes if p > 0]
    total = sum(closes)
    peak, dd = START, 0.0
    for _, value in curve:
        peak = max(peak, value)
        dd = max(dd, (peak - value) / peak * 100)
    by_gear = {}
    for gear in (TREND, "range"):
        group = [t for t in trades if t["gear"] == gear]
        pnls = [t["pnl"] for t in group]
        by_gear[gear] = {
            "gear": gear, "trades": len(group),
            "win_pct": (sum(1 for p in pnls if p > 0) / len(pnls) * 100) if pnls else 0.0,
            "avg_r": (sum(t["r"] for t in group) / len(group)) if group else 0.0,
            "pnl": sum(pnls),
            "dd_pct": 0.0,
        }
    fees = sum(t["fees"] for t in trades)
    return {
        "window_days": days,
        "window": f"last {days} days · {len(data)} coins · 4h bars",
        "trades": len(trades),
        "win_pct": (len(wins) / len(trades) * 100) if trades else 0.0,
        "avg_r": (sum(t["r"] for t in trades) / len(trades)) if trades else 0.0,
        "total_pct": total / START * 100,
        "max_dd_pct": dd,
        "fees": fees,
        "by_gear": list(by_gear.values()),
        "note": ("same regime, signals, stops and exits as the live engine; "
                 "0.25% per side plus 0.05% slippage; static learner (1.0x)"),
        "trades_detail": trades,
        "curve": curve,
    }


def grid(data: dict[str, list[dict]], days: int) -> None:
    print(f"{'trend':>14} {'range':>10} {'trades':>7} {'win%':>6} {'avgR':>7} {'return':>9} {'dd':>7}")
    for adx in (20.0, 22.0, 25.0):
        for er in (0.25, 0.30, 0.35):
            for radx in (18.0, 22.0):
                s = replace(SETTINGS, trend_adx=adx, trend_er=er, range_adx=radx)
                r = run(data, s, days)
                print(f"{adx:>6.0f}/{er:<6.2f} {radx:>10.0f} {r['trades']:>7} "
                      f"{r['win_pct']:>5.0f}% {r['avg_r']:>+7.2f} {r['total_pct']:>+8.2f}% "
                      f"{r['max_dd_pct']:>6.2f}%")


def main() -> None:
    days = 120
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    print(f"fetching {days} days of hourly bars for {len(SETTINGS.universe)} coins…")
    data = load(days)
    print(f"loaded {len(data)} coins, {min(len(b) for b in data.values())}–{max(len(b) for b in data.values())} 4h bars")
    if "--grid" in sys.argv:
        grid(data, days)
        return
    result = run(data, SETTINGS, days)
    ares = pathlib.Path(__file__).resolve().parent.parent / "gear_summary.json"
    ares.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"\n{days}d replay · {result['trades']} trades · {result['win_pct']:.0f}% win · "
          f"avg {result['avg_r']:+.2f}R · {result['total_pct']:+.2f}% · DD {result['max_dd_pct']:.2f}%")
    for row in result["by_gear"]:
        print(f"  {row['gear']:<6} {row['trades']:>3} trades  {row['win_pct']:>3.0f}% win  "
              f"{row['avg_r']:+.2f}R  ${row['pnl']:+,.0f}")
    reasons: dict[str, int] = {}
    for t in result["trades_detail"]:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    print("  exits:", ", ".join(f"{k} {v}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])))
    print(f"wrote {ares}")


if __name__ == "__main__":
    main()
