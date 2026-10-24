"""Pick the live configuration: per-coin carry, and a low-churn fixed book."""
import json
import statistics as st

from carry import load, apr, simulate, HOURS_YEAR

SPOT_FEE = 0.0025
PERP_FEE = 0.00045
RT = 2 * (SPOT_FEE + PERP_FEE)


def per_coin(series, coin, margin=0.25):
    rows = series[coin]
    funding = sum(r[1] for r in rows)
    fees = RT  # one entry, held to the end
    net = funding - fees
    days = (rows[-1][0] - rows[0][0]) / 86400000
    ann = ((1 + net / (1 + margin)) ** (365 / days) - 1) * 100
    # equity curve on 1.0 notional, marked every 6h
    eq, curve = 1.0, []
    for i, (t, f, p) in enumerate(rows):
        eq += f
        if i % 6 == 0:
            curve.append(eq)
    peak, dd = curve[0], 0.0
    for e in curve:
        peak = max(peak, e)
        dd = max(dd, (peak - e) / peak)
    return {"funding": funding * 100, "net": net * 100, "ann": ann, "dd": dd * 100, "days": days}


def rolling_negative(series, coin, window=24 * 7):
    rows = series[coin]
    spells, run = [], 0
    for _, f, _ in rows:
        if f < 0:
            run += 1
            if run == window:
                spells.append(1)
        else:
            run = 0
    return len(spells)


def main():
    series = load()
    coins = sorted(series)
    print("=== hold one hedged pair for the full two years (capital = notional x 1.25) ===")
    print(f"{'coin':7}{'funding%':>10}{'net%':>8}{'ann%':>8}{'maxDD%':>9}{'7d neg spells':>15}")
    stats = {}
    for c in coins:
        s = per_coin(series, c)
        stats[c] = s
        print(f"{c:7}{s['funding']:9.1f}{s['net']:8.1f}{s['ann']:8.2f}{s['dd']:9.2f}"
              f"{rolling_negative(series, c):15d}")

    # low-churn book: fixed universe, rebalance only when a coin's 30d funding goes negative
    print("\n=== low-churn book: fixed top-N, drop only on sustained negative funding ===")
    ranked = sorted(coins, key=lambda c: stats[c]["ann"], reverse=True)
    for n in (2, 3, 4, 5):
        pick = ranked[:n]
        r = simulate(series, pick, always_on=True, slots=n, rebalance_hours=24 * 30)
        print(f"  top{n} {','.join(pick):38} ann {r['annual_pct']:6.2f}%  dd {r['max_dd_pct']:5.2f}%  "
              f"sharpe {r['sharpe']:5.2f}  switches {r['switches']}")

    print("\n=== churn sensitivity: how much switching the fee budget allows ===")
    always = simulate(series, ["BTC", "ETH", "SOL"], always_on=True, slots=3, rebalance_hours=24 * 30)
    for switches in (12, 24, 52, 104):
        fee = switches * RT * (1 / 3)
        print(f"  {switches:4d} rebalances/2y -> fees {fee * 100:5.2f}% of notional  "
              f"(gross carry {always['funding_pct']:.1f}%)  net {(always['funding_pct'] / 100 - fee) * 100:5.1f}%")

    with open("deploy_stats.json", "w") as fh:
        json.dump({"per_coin": stats, "ranked": ranked}, fh, indent=1)


if __name__ == "__main__":
    main()
