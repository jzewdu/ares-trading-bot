"""Delta-neutral carry study: long spot, short perp, on real hourly funding.

P&L per hour for a hedged pair of notional N:
  + N * funding_rate        (short perp receives when funding is positive)
  - N * d(premium)          (long spot vs short perp, difference is basis)
  - fees on entry/exit

Capital = N * (1 + margin_ratio): the spot leg plus margin posted on the short.
"""
import gzip
import json
import os
import statistics as st
from collections import defaultdict

SPOT_FEE = 0.0025      # Alpaca crypto taker, spot leg, per side
PERP_FEE = 0.00045     # Hyperliquid taker, perp leg, per side
ROUND_TRIP = 2 * (SPOT_FEE + PERP_FEE)
MARGIN_RATIO = 0.20    # margin posted for the short leg, as fraction of notional
HOURS_YEAR = 24 * 365


def load():
    path = "hl_funding.json"
    if not os.path.exists(path) and os.path.exists(path + ".gz"):
        path += ".gz"
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        raw = json.load(fh)
    series = {}
    for coin, rows in raw.items():
        series[coin] = [(int(t), float(f), float(p)) for t, f, p in rows]
    return series


def apr(rows, window):
    """Annualised funding from the trailing `window` hours."""
    if len(rows) < window:
        return None
    return sum(r[1] for r in rows[-window:]) / window * HOURS_YEAR * 100


def simulate(series, coins, lookback=168, enter=6.0, exit_=0.0, slots=3,
             rebalance_hours=24, always_on=False):
    """Equal-notional top-N carry book, evaluated hourly on real funding."""
    timeline = sorted({t for c in coins for t, _, _ in series[c]})
    idx = {c: {t: (f, p) for t, f, p in series[c]} for c in coins}
    hist = {c: [] for c in coins}

    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    curve = []
    held = {}          # coin -> notional
    last_premium = {}
    fees_paid = 0.0
    funding_paid = 0.0
    basis_pnl = 0.0
    switches = 0
    hours_in = 0
    last_rebalance = None

    for t in timeline:
        for c in coins:
            row = idx[c].get(t)
            if row:
                hist[c].append((t, row[0], row[1]))

        # settle funding and basis on open positions
        for c in list(held):
            row = idx[c].get(t)
            if not row:
                continue
            f, p = row
            n = held[c]
            funding_paid += n * f
            basis_pnl += -n * (p - last_premium.get(c, p))
            last_premium[c] = p

        should = (last_rebalance is None or (t - last_rebalance) >= rebalance_hours * 3600 * 1000)
        if should:
            last_rebalance = t
            if always_on:
                want = list(coins)[:slots]
            else:
                scored = []
                for c in coins:
                    a = apr(hist[c], lookback)
                    if a is not None:
                        scored.append((a, c))
                scored.sort(reverse=True)
                want = [c for a, c in scored if a > enter][:slots]
                for c in held:
                    trailing = apr(hist[c], 72)
                    if trailing is not None and trailing > exit_ and c not in want:
                        want.append(c)
                want = want[:slots]
            if set(want) != set(held):
                switches += 1
                for c in list(held):
                    if c not in want:
                        n = held.pop(c)
                        fee = n * (SPOT_FEE + PERP_FEE)
                        fees_paid += fee
                        equity -= fee
                weight = 1.0 / slots
                for c in want:
                    row = idx[c].get(t)
                    if row is None:
                        continue
                    if c not in held:
                        n = weight
                        fee = n * (SPOT_FEE + PERP_FEE)
                        fees_paid += fee
                        equity -= fee
                        held[c] = n
                        last_premium[c] = row[1]

        equity += sum(held[c] * idx[c][t][0] for c in held if t in idx[c])
        if held:
            hours_in += 1
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
        if t % (6 * 3600 * 1000) < 3600 * 1000:
            curve.append((t, equity))

    # equity is a multiple of hedged notional; capital also carries short margin
    ret = (equity - 1.0) / (1 + MARGIN_RATIO)
    span_days = (timeline[-1] - timeline[0]) / 86400000
    ann = ((1 + ret) ** (365 / span_days) - 1) * 100 if span_days > 0 else 0
    rets = []
    prev = None
    for _, e in curve:
        if prev is not None:
            rets.append(e / prev - 1)
        prev = e
    vol = st.pstdev(rets) * (24 * 365 / 6) ** 0.5 * 100 if len(rets) > 2 else 0
    return {
        "net_pct": ret * 100, "annual_pct": ann, "max_dd_pct": max_dd * 100,
        "sharpe": (ann / vol) if vol else 0, "vol_pct": vol,
        "funding_pct": funding_paid * 100, "fees_pct": -fees_paid * 100,
        "basis_pct": basis_pnl * 100, "switches": switches,
        "days": span_days, "time_in_market_pct": 100 * hours_in / max(1, len(timeline)),
    }


def funding_stats(series, coin):
    rows = series[coin]
    rates = [r[1] for r in rows]
    aprs = [r * HOURS_YEAR * 100 for r in rates]
    pos = 100 * sum(1 for r in rates if r > 0) / len(rates)
    # rolling 30d APR extremes
    w = 24 * 30
    rolls = [sum(rates[i - w:i]) / w * HOURS_YEAR * 100 for i in range(w, len(rates), 24)]
    return {
        "mean_apr": st.mean(aprs), "median_apr": st.median(aprs), "positive_pct": pos,
        "roll30_min": min(rolls), "roll30_max": max(rolls),
        "neg_days": sum(1 for r in rolls if r < 0),
    }


def main():
    series = load()
    coins = sorted(series)
    print("=== funding history: 2024-10 -> 2026-10 (hourly) ===")
    print(f"{'coin':6}{'mean APR':>10}{'med APR':>9}{'%pos':>7}{'30d min':>9}{'30d max':>9}{'neg 30d':>9}")
    for c in coins:
        s = funding_stats(series, c)
        print(f"{c:6}{s['mean_apr']:9.2f}%{s['median_apr']:8.2f}%{s['positive_pct']:6.0f}%"
              f"{s['roll30_min']:8.2f}%{s['roll30_max']:8.2f}%{s['neg_days']:9d}")

    print("\n=== always-on carry (top 3, rebalance daily) ===")
    r = simulate(series, coins, always_on=True, slots=3, rebalance_hours=24)
    for k, v in r.items():
        print(f"  {k:20} {v:10.2f}" if isinstance(v, float) else f"  {k:20} {v}")

    print("\n=== filtered carry sweep (enter on trailing 7d APR) ===")
    print(f"{'enter':>7}{'exit':>6}{'slots':>6}{'net%':>9}{'ann%':>8}{'dd%':>7}{'sharpe':>8}{'fees%':>8}{'in mkt':>8}")
    best = None
    for enter in (4, 6, 8, 10, 15):
        for exit_ in (0.0, 2.0):
            for slots in (2, 3):
                r = simulate(series, coins, enter=enter, exit_=exit_, slots=slots)
                print(f"{enter:7}{exit_:6.0f}{slots:6}{r['net_pct']:8.2f}{r['annual_pct']:8.2f}"
                      f"{r['max_dd_pct']:7.2f}{r['sharpe']:8.2f}{r['fees_pct']:8.2f}{r['time_in_market_pct']:7.0f}%")
                if best is None or r["annual_pct"] > best[1]["annual_pct"]:
                    best = (f"enter {enter} exit {exit_} slots {slots}", r)
    print(f"\nbest by annual return: {best[0]} -> {best[1]['annual_pct']:.2f}% ann, "
          f"{best[1]['max_dd_pct']:.2f}% dd, sharpe {best[1]['sharpe']:.2f}")
    with open("carry_results.json", "w") as fh:
        json.dump({c: funding_stats(series, c) for c in coins}, fh)


if __name__ == "__main__":
    main()
